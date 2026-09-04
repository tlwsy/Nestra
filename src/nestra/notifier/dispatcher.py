"""One-delivery-at-a-time notification dispatch and retry state transitions."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.crypto import Crypto, new_token
from ..core.errors import Fatal, Retryable, TargetRejected
from ..core.models import DeliveryStatus
from ..core.time import backoff_delay, from_iso, now, to_iso
from ..storage.db import Database
from .capabilities import capabilities_for
from .message import MessageAttachment, render_message


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivery_id: int
    status: DeliveryStatus
    attempts: int
    next_attempt_at: datetime | None = None
    error: str | None = None


def _safe_error(exc: BaseException) -> str:
    text = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "[redacted-url]", str(exc), flags=re.I)
    return (text or type(exc).__name__)[:500]


class Dispatcher:
    def __init__(
        self,
        db: Database,
        client: Any,
        *,
        timezone: str = "UTC",
        body_format: str = "markdown",
        include_full_content: bool = True,
        max_body_chars: int = 8000,
        attachment_mode: str = "both",
        attachment_inline_max_mb: int = 10,
        max_attempts: int = 5,
        backoff_base_sec: float = 30,
        target_auto_disable_after_failures: int = 10,
        crypto: Crypto | None = None,
        base_url: str = "",
        signed_link_ttl_hours: int = 72,
    ) -> None:
        self.db = db
        self.client = client
        self.timezone = timezone
        self.body_format = body_format
        self.include_full_content = include_full_content
        self.max_body_chars = max_body_chars
        self.attachment_mode = attachment_mode
        self.attachment_inline_max_bytes = attachment_inline_max_mb * 1024 * 1024
        self.max_attempts = max_attempts
        self.backoff_base_sec = backoff_base_sec
        self.disable_after = target_auto_disable_after_failures
        self.crypto = crypto
        self.base_url = base_url.rstrip("/")
        self.signed_link_ttl_sec = signed_link_ttl_hours * 3600

    def _load(self, delivery_id: int):
        return self.db.query_one(
            "SELECT d.*,s.include_attachments,s.user_id,a.title,a.content_text,a.summary,"
            "a.summary_backend,a.url,a.published_at,"
            "si.name AS site_name,t.apprise_url_enc,t.url_fingerprint,"
            "t.enabled AS target_enabled,t.consecutive_failures AS target_failures "
            "FROM deliveries d JOIN subscriptions s ON s.id=d.subscription_id "
            "JOIN articles a ON a.id=d.article_id JOIN sites si ON si.id=a.site_id "
            "JOIN notify_targets t ON t.id=d.target_id WHERE d.id=?",
            (delivery_id,),
        )

    def _message(self, row) -> tuple[Any, list[str]]:
        tags = [
            (tag["name"], tag["confidence"])
            for tag in self.db.query(
                "SELECT t.name,at.confidence FROM article_tags at "
                "JOIN tags t ON t.id=at.tag_id WHERE at.article_id=? "
                "ORDER BY at.confidence DESC,t.id",
                (row["article_id"],),
            )
        ]
        attachment_rows = self.db.query(
            "SELECT id,source_url,filename,size_bytes,mime_type,local_path,status FROM attachments "
            "WHERE article_id=? AND status IN ('pending','failed','downloaded') ORDER BY id",
            (row["article_id"],),
        )
        attachments = []
        for item in attachment_rows:
            link = item["source_url"] if item["status"] != "downloaded" else None
            if (
                item["status"] == "downloaded"
                and self.crypto is not None
                and self.base_url
                and self.attachment_mode in {"link", "both"}
            ):
                token = self.crypto.sign_payload(
                    {"attachment_id": item["id"], "user_id": row["user_id"]},
                    ttl_sec=self.signed_link_ttl_sec,
                    purpose="link",
                )
                link = f"{self.base_url}/shared/attachments/{item['id']}?token={token}"
            attachments.append(
                MessageAttachment(
                    item["filename"] or "attachment",
                    item["size_bytes"],
                    item["local_path"] if item["status"] == "downloaded" else None,
                    url=link,
                    mime_type=item["mime_type"],
                )
            )
        fingerprint = row["url_fingerprint"] or ""
        caps = capabilities_for(fingerprint)
        attach_limit = min(
            self.attachment_inline_max_bytes,
            caps.max_attachment_bytes or self.attachment_inline_max_bytes,
        )
        files = [
            item.local_path
            for item in attachments
            if row["include_attachments"]
            and self.attachment_mode != "link"
            and caps.supports_attachments
            and item.local_path
            and (item.size_bytes is None or item.size_bytes <= attach_limit)
            and Path(item.local_path).is_file()
        ]
        message = render_message(
            title=row["title"] or "无标题",
            site_name=row["site_name"],
            url=row["url"],
            published_at=from_iso(row["published_at"]),
            tags=tags,
            content=row["content_text"] or "",
            summary=row["summary"] if row["summary_backend"] else None,
            attachments=attachments if row["include_attachments"] else [],
            timezone=self.timezone,
            requested_format=self.body_format,
            include_full_content=self.include_full_content,
            max_body_chars=self.max_body_chars,
            channel=fingerprint,
        )
        return message, files

    def _claim(self, delivery_id: int, at: datetime) -> str | None:
        token = new_token(18)
        cursor = self.db.execute(
            "UPDATE deliveries SET claim_token=?,claim_until=? WHERE id=? AND status='pending' "
            "AND (next_attempt_at IS NULL OR datetime(next_attempt_at)<=datetime(?)) "
            "AND (claim_until IS NULL OR datetime(claim_until)<=datetime(?))",
            (
                token,
                to_iso(at + timedelta(hours=1)),
                delivery_id,
                to_iso(at),
                to_iso(at),
            ),
        )
        return token if cursor.rowcount == 1 else None

    def _record_success(self, row, at: datetime, claim: str) -> DeliveryOutcome:
        stamp = to_iso(at)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE deliveries SET status='sent',sent_at=?,next_attempt_at=NULL,"
                "last_error=NULL,claim_token=NULL,claim_until=NULL "
                "WHERE id=? AND status='pending' AND claim_token=?",
                (stamp, row["id"], claim),
            )
            if cursor.rowcount != 1:
                return DeliveryOutcome(row["id"], DeliveryStatus.PENDING, row["attempts"])
            conn.execute(
                "UPDATE notify_targets SET consecutive_failures=0,last_ok_at=?,last_error=NULL "
                "WHERE id=?",
                (stamp, row["target_id"]),
            )
        return DeliveryOutcome(row["id"], DeliveryStatus.SENT, row["attempts"])

    def _record_failure(self, row, exc: BaseException, at: datetime, claim: str) -> DeliveryOutcome:
        attempts = row["attempts"] + 1
        error = _safe_error(exc)
        disable_target = row["target_failures"] + 1 >= self.disable_after
        fatal = isinstance(exc, Fatal) or attempts >= self.max_attempts or disable_target
        retry_at: datetime | None = None
        if not fatal:
            requested = exc.retry_after_sec if isinstance(exc, Retryable) else None
            delay = (
                requested
                if requested is not None
                else backoff_delay(attempts, self.backoff_base_sec, jitter=False)
            )
            retry_at = at + timedelta(seconds=max(0, delay))
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE deliveries SET status=?,attempts=?,next_attempt_at=?,last_error=?,"
                "claim_token=NULL,claim_until=NULL WHERE id=? AND status='pending' "
                "AND claim_token=?",
                (
                    "failed" if fatal else "pending",
                    attempts,
                    to_iso(retry_at) if retry_at else None,
                    error,
                    row["id"],
                    claim,
                ),
            )
            if cursor.rowcount != 1:
                return DeliveryOutcome(row["id"], DeliveryStatus.PENDING, row["attempts"])
            conn.execute(
                "UPDATE notify_targets SET consecutive_failures=consecutive_failures+1,"
                "last_error=?,enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE id=?",
                (error, disable_target, row["target_id"]),
            )
            if fatal and not disable_target and not isinstance(exc, Fatal):
                conn.execute(
                    "UPDATE deliveries SET status='pending',next_attempt_at=?,last_error=NULL "
                    "WHERE id=(SELECT id FROM deliveries WHERE subscription_id=? "
                    "AND target_id=? AND status='skipped' AND last_error=? "
                    "ORDER BY datetime(created_at) DESC,id DESC LIMIT 1)",
                    (
                        to_iso(at),
                        row["subscription_id"],
                        row["target_id"],
                        f"duplicate_of:{row['article_id']}",
                    ),
                )

        return DeliveryOutcome(
            row["id"],
            DeliveryStatus.FAILED if fatal else DeliveryStatus.PENDING,
            attempts,
            retry_at,
            error,
        )

    async def send(self, delivery_id: int, *, at: datetime | None = None) -> DeliveryOutcome | None:
        """Attempt one due pending delivery; return None when it is absent/not due."""
        at = at or now()
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        at = at.astimezone(UTC)
        row = self._load(delivery_id)
        if row is None or row["status"] != DeliveryStatus.PENDING:
            return None
        due = from_iso(row["next_attempt_at"])
        if due and due > at:
            return DeliveryOutcome(row["id"], DeliveryStatus.PENDING, row["attempts"], due)
        claim = self._claim(delivery_id, at)
        if claim is None:
            return None
        row = self._load(delivery_id)
        if row is None:
            return None

        if not row["target_enabled"]:
            return self._record_failure(row, TargetRejected("target is disabled"), at, claim)
        try:
            message, attachments = self._message(row)
            result = self.client.notify(
                row["apprise_url_enc"],
                body=message.body,
                title=message.title,
                body_format=message.body_format,
                attachments=attachments,
            )
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                raise RuntimeError("notification was not accepted")
        except Exception as exc:
            return self._record_failure(row, exc, at, claim)
        return self._record_success(row, at, claim)

    async def send_due(
        self, *, limit: int = 100, at: datetime | None = None
    ) -> list[DeliveryOutcome]:
        at = at or now()
        ids = self.db.query(
            "SELECT id FROM deliveries WHERE status='pending' "
            "AND (next_attempt_at IS NULL OR datetime(next_attempt_at)<=datetime(?)) "
            "AND (claim_until IS NULL OR datetime(claim_until)<=datetime(?)) "
            "ORDER BY next_attempt_at,id LIMIT ?",
            (to_iso(at), to_iso(at), limit),
        )
        outcomes = []
        for item in ids:  # Deliberately one Apprise call per article.
            if outcome := await self.send(item["id"], at=at):
                outcomes.append(outcome)
        return outcomes
