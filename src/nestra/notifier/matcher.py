"""Subscription matching and atomic delivery creation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..core.models import ArticleStatus
from ..core.time import next_active_time, now, to_iso
from ..storage.db import Database


@dataclass(frozen=True, slots=True)
class MatchedDelivery:
    subscription_id: int
    article_id: int
    target_id: int
    scheduled_at: datetime


def simhash_distance(left: str, right: str) -> int:
    """Hamming distance for the hexadecimal storage format."""
    return (int(left.strip(), 16) ^ int(right.strip(), 16)).bit_count()


class Matcher:
    def __init__(
        self,
        db: Database,
        *,
        timezone: str = "UTC",
        dedupe_window_days: int = 7,
    ) -> None:
        self.db = db
        self.timezone = timezone
        self.dedupe_window_days = dedupe_window_days

    @staticmethod
    def _site_allowed(raw_filter: str | None, site_id: int) -> bool:
        if not raw_filter:
            return True
        try:
            values = json.loads(raw_filter)
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(values, list) and site_id in values

    @staticmethod
    def _matches(mode: str, selected: set[int], article_tags: set[int]) -> bool:
        if not selected:
            return False
        return bool(selected & article_tags) if mode == "any" else selected <= article_tags

    def _duplicate_of(
        self,
        conn,
        subscription_id: int,
        target_id: int,
        article_id: int,
        simhash: str | None,
        at: datetime,
    ) -> int | None:
        if not simhash or self.dedupe_window_days <= 0:
            return None
        cutoff = to_iso(at - timedelta(days=self.dedupe_window_days))
        rows = conn.execute(
            "SELECT DISTINCT a.id, a.simhash FROM deliveries d "
            "JOIN articles a ON a.id=d.article_id "
            "WHERE d.subscription_id=? AND d.target_id=? "
            "AND d.status IN ('pending','sent') AND a.id<>? "
            "AND a.simhash IS NOT NULL "
            "AND datetime(COALESCE(d.sent_at,d.created_at))>=datetime(?) "
            "ORDER BY datetime(COALESCE(d.sent_at,d.created_at)) DESC, a.id DESC",
            (subscription_id, target_id, article_id, cutoff),
        )
        for row in rows:
            try:
                if simhash_distance(simhash, row["simhash"]) <= 3:
                    return row["id"]
            except ValueError:
                continue
        return None

    def match(self, article_id: int, *, at: datetime | None = None) -> list[MatchedDelivery]:
        """Match once, insert idempotently, and mark the article NOTIFIED atomically."""
        at = at or now()
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        at = at.astimezone(UTC)
        created_at = to_iso(at)
        matched: list[MatchedDelivery] = []
        with self.db.transaction() as conn:
            article = conn.execute(
                "SELECT id,site_id,simhash,status FROM articles WHERE id=?", (article_id,)
            ).fetchone()
            if article is None or article["status"] != ArticleStatus.TAGGED:
                return []

            subscriptions = conn.execute(
                "SELECT * FROM subscriptions WHERE enabled=1 ORDER BY id"
            ).fetchall()
            for subscription in subscriptions:
                if not self._site_allowed(subscription["site_filter"], article["site_id"]):
                    continue
                selected = {
                    row["tag_id"]
                    for row in conn.execute(
                        "SELECT tag_id FROM subscription_tags WHERE subscription_id=?",
                        (subscription["id"],),
                    )
                }
                article_tags = {
                    row["tag_id"]
                    for row in conn.execute(
                        "SELECT at.tag_id FROM article_tags at "
                        "JOIN tags t ON t.id=at.tag_id "
                        "JOIN sites s ON s.id=? AND s.tagset_group_id=t.group_id "
                        "WHERE at.article_id=? AND at.confidence>=?",
                        (article["site_id"], article_id, subscription["min_confidence"]),
                    )
                }
                if not self._matches(subscription["match_mode"], selected, article_tags):
                    continue

                scheduled = (
                    next_active_time(at, subscription["quiet_hours"], self.timezone)
                    if subscription["quiet_hours"]
                    else at
                )
                targets = conn.execute(
                    "SELECT t.id FROM notify_targets t "
                    "WHERE t.user_id=? AND t.enabled=1 AND ("
                    "EXISTS (SELECT 1 FROM subscription_targets st "
                    " WHERE st.subscription_id=? AND st.target_id=t.id) OR "
                    "NOT EXISTS (SELECT 1 FROM subscription_targets st "
                    " WHERE st.subscription_id=?)) ORDER BY t.id",
                    (subscription["user_id"], subscription["id"], subscription["id"]),
                ).fetchall()
                for target in targets:
                    duplicate = self._duplicate_of(
                        conn,
                        subscription["id"],
                        target["id"],
                        article_id,
                        article["simhash"],
                        at,
                    )
                    status = "skipped" if duplicate is not None else "pending"
                    reason = f"duplicate_of:{duplicate}" if duplicate is not None else None
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO deliveries "
                        "(subscription_id,article_id,target_id,status,next_attempt_at,"
                        "last_error,created_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            subscription["id"],
                            article_id,
                            target["id"],
                            status,
                            to_iso(scheduled),
                            reason,
                            created_at,
                        ),
                    )
                    if cursor.rowcount and status == "pending":
                        matched.append(
                            MatchedDelivery(subscription["id"], article_id, target["id"], scheduled)
                        )
            conn.execute(
                "UPDATE articles SET status=? WHERE id=? AND status=?",
                (ArticleStatus.NOTIFIED, article_id, ArticleStatus.TAGGED),
            )
        return matched
