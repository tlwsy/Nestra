"""抓取流水线所需的文章持久化。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ...core.errors import Retryable
from ...core.logging import safe_error
from ...core.models import ArticleText, DiscoveredItem
from ...core.time import from_iso, now, now_iso, to_iso
from ...extractor.dedupe import hamming_distance, url_hash
from ..db import Database


@dataclass(frozen=True, slots=True)
class DiscoveredRow:
    id: int
    created: bool
    status: str
    next_attempt_at: str | None


class ArticleRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def discover(self, site_id: int, item: DiscoveredItem, canonical_url: str) -> DiscoveredRow:
        timestamp = now_iso()
        digest = url_hash(canonical_url)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(site_id, url, url_hash, title, published_at, summary, status, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'DISCOVERED', ?)",
                (
                    site_id,
                    canonical_url,
                    digest,
                    item.title,
                    to_iso(item.published_at) if item.published_at else None,
                    item.summary,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT id,status,next_attempt_at FROM articles WHERE url_hash=?", (digest,)
            ).fetchone()
        return DiscoveredRow(row["id"], cursor.rowcount == 1, row["status"], row["next_attempt_at"])

    def pending(self, site_id: int) -> list[DiscoveredItem]:
        """Return unfinished rows even when an unchanged list page yields no items."""
        return [
            DiscoveredItem(
                url=row["url"],
                title=row["title"],
                published_at=from_iso(row["published_at"]),
                summary=row["summary"],
            )
            # ponytail: 100 retries/crawl fits the <100/day target; make configurable if needed.
            for row in self.db.query(
                "SELECT url,title,published_at,summary FROM articles WHERE site_id=? "
                "AND (status IN ('DISCOVERED','FETCHED') OR "
                "(status='FAILED' AND next_attempt_at<=?)) "
                "ORDER BY attempts,id LIMIT 100",
                (site_id, now_iso()),
            )
        ]

    def update_reachable_url(self, article_id: int, url: str) -> None:
        """Keep the canonical hash but expose a source URL that actually responds."""
        self.db.execute("UPDATE articles SET url=? WHERE id=?", (url, article_id))

    def mark_fetched(self, article_id: int) -> None:
        self.db.execute(
            "UPDATE articles SET status='FETCHED', fetched_at=?, next_attempt_at=NULL, "
            "last_error=NULL WHERE id=?",
            (now_iso(), article_id),
        )

    def save_extracted(
        self, article_id: int, article: ArticleText, fingerprint: str, *, max_distance: int = 3
    ) -> int | None:
        """保存提取结果；近重复保留记录但置 SKIPPED，返回原文章 ID。"""
        with self.db.transaction() as conn:
            duplicate_id = next(
                (
                    row["id"]
                    for row in conn.execute(
                        "SELECT id, simhash FROM articles "
                        "WHERE id != ? AND simhash IS NOT NULL AND status != 'FAILED'",
                        (article_id,),
                    )
                    if hamming_distance(fingerprint, row["simhash"]) <= max_distance
                ),
                None,
            )
            status = "SKIPPED" if duplicate_id else "EXTRACTED"
            error = f"正文近重复: article_id={duplicate_id}" if duplicate_id else None
            conn.execute(
                "UPDATE articles SET title=?, author=?, published_at=?, summary=?, "
                "content_text=?, content_html=?, lang=?, simhash=?, word_count=?, "
                "status=?, attempts=0, next_attempt_at=NULL, last_error=? WHERE id=?",
                (
                    article.title,
                    article.author,
                    to_iso(article.published_at) if article.published_at else None,
                    article.summary,
                    article.content_text,
                    article.content_html,
                    article.lang,
                    fingerprint,
                    article.word_count,
                    status,
                    error,
                    article_id,
                ),
            )
            if duplicate_id is None:
                conn.executemany(
                    "INSERT OR IGNORE INTO attachments "
                    "(article_id,source_url,filename,status,created_at) VALUES (?,?,?,'pending',?)",
                    [
                        (article_id, item.source_url, item.filename, now_iso())
                        for item in article.attachments
                    ],
                )
        return duplicate_id

    def mark_skipped(self, article_id: int, reason: Exception) -> None:
        self.db.execute(
            "UPDATE articles SET status='SKIPPED',last_error=? WHERE id=?",
            (safe_error(reason), article_id),
        )

    def mark_failed(
        self,
        article_id: int,
        error: Exception,
        *,
        max_attempts: int,
        backoff_base_sec: float,
    ) -> None:
        row = self.db.query_one("SELECT attempts FROM articles WHERE id=?", (article_id,))
        attempts = (row["attempts"] if row else 0) + 1
        retry_at = None
        if isinstance(error, Retryable) and attempts < max_attempts:
            advertised = getattr(error, "retry_after_sec", None)
            delay = advertised if advertised is not None else backoff_base_sec * 2 ** (attempts - 1)
            retry_at = to_iso(now() + timedelta(seconds=min(float(delay), 86_400)))
        self.db.execute(
            "UPDATE articles SET status='FAILED', attempts=?, next_attempt_at=?, "
            "last_error=? WHERE id=?",
            (attempts, retry_at, safe_error(error), article_id),
        )
