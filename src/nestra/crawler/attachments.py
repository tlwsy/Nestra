"""Independent bounded attachment download job."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..core.config import Settings
from ..core.errors import Fatal, ResponseTooLarge, StorageError
from ..core.logging import safe_error
from ..notifier.attachment import filename_from_content_disposition, mime_allowed, sniff_mime
from ..storage.db import Database
from ..storage.files import attachment_path, ensure_private_directory
from ..storage.repositories.sites import get_site
from .fetcher import Fetcher


@dataclass(slots=True)
class AttachmentStats:
    downloaded: int = 0
    reused: int = 0
    skipped: int = 0
    failed: int = 0


def _fallback_name(url: str, configured: str | None) -> str:
    return configured or unquote(Path(urlsplit(url).path).name) or "attachment"


async def download_pending(settings: Settings, db: Database, *, limit: int = 20) -> AttachmentStats:
    if not settings.attachments.enabled:
        return AttachmentStats()
    rows = db.query(
        "SELECT at.*,a.url AS article_url,s.slug AS site_slug FROM attachments at "
        "JOIN articles a ON a.id=at.article_id JOIN sites s ON s.id=a.site_id "
        "WHERE at.status IN ('pending','failed') AND at.attempts < ? ORDER BY at.id LIMIT ?",
        (settings.politeness.retry.max_attempts, limit),
    )
    stats = AttachmentStats()
    quota = int(settings.attachments.total_quota_gb * 1024**3)
    root = settings.storage.attachment_dir.resolve()
    try:
        ensure_private_directory(root)
    except (OSError, StorageError) as exc:
        raise StorageError(f"无法准备附件目录: {root}") from exc
    fetchers: dict[str, Fetcher] = {}
    try:
        for row in rows:
            site = get_site(db, row["site_slug"])
            if site is None or not site.config.attachments.enabled:
                db.execute(
                    "UPDATE attachments SET status='skipped',skip_reason='attachments disabled' "
                    "WHERE id=?",
                    (row["id"],),
                )
                stats.skipped += 1
                continue
            fetcher = fetchers.get(row["site_slug"])
            if fetcher is None:
                fetcher = fetchers[row["site_slug"]] = Fetcher(
                    settings.politeness,
                    max_concurrency=site.config.politeness.max_concurrency,
                    delay_sec=site.config.politeness.delay_sec,
                    conditional_requests=False,
                    max_bytes=settings.attachments.max_size_mb * 1024**2,
                )
            headers = (
                {"Referer": row["article_url"]} if site.config.attachments.send_referer else {}
            )
            cleanup_path: Path | None = None
            reused_file = False
            try:
                response = await fetcher.fetch_bytes(row["source_url"], headers=headers)
                mime = sniff_mime(response.content)
                if not mime_allowed(mime, settings.attachments.allow_mime):
                    db.execute(
                        "UPDATE attachments SET status='skipped',mime_type=?,"
                        "skip_reason='mime not allowed' WHERE id=?",
                        (mime, row["id"]),
                    )
                    stats.skipped += 1
                    continue
                digest = hashlib.sha256(response.content).hexdigest()
                filename = filename_from_content_disposition(
                    response.headers.get("content-disposition"),
                    _fallback_name(row["source_url"], row["filename"]),
                )
                with db.transaction() as conn:
                    existing = conn.execute(
                        "SELECT local_path FROM attachments WHERE sha256=? "
                        "AND status='downloaded' AND local_path IS NOT NULL LIMIT 1",
                        (digest,),
                    ).fetchone()
                    reused = (
                        attachment_path(existing["local_path"], root, require_file=True)
                        if existing
                        else None
                    )
                    if reused is not None:
                        destination = reused
                        reused_file = True
                    else:
                        used = conn.execute(
                            "SELECT COALESCE(SUM(size_bytes),0) FROM ("
                            "SELECT MAX(size_bytes) AS size_bytes FROM attachments "
                            "WHERE status='downloaded' GROUP BY "
                            "CASE WHEN sha256 IS NULL THEN 'id:'||id ELSE sha256 END)"
                        ).fetchone()[0]
                        if used + len(response.content) > quota:
                            conn.execute(
                                "UPDATE attachments SET status='skipped',"
                                "skip_reason='quota exceeded' WHERE id=?",
                                (row["id"],),
                            )
                            stats.skipped += 1
                            continue
                        destination = root / digest[:2] / digest[2:4] / digest
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination_was_new = not destination.exists()
                        fd, temporary = tempfile.mkstemp(
                            dir=destination.parent, prefix=".download-"
                        )
                        try:
                            with os.fdopen(fd, "wb") as handle:
                                handle.write(response.content)
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(temporary, destination)
                            if destination_was_new:
                                cleanup_path = destination
                        except BaseException:
                            Path(temporary).unlink(missing_ok=True)
                            raise
                    conn.execute(
                        "UPDATE attachments SET filename=?,mime_type=?,size_bytes=?,sha256=?,"
                        "local_path=?,status='downloaded',skip_reason=NULL,attempts=attempts+1 "
                        "WHERE id=?",
                        (
                            filename,
                            mime,
                            len(response.content),
                            digest,
                            str(destination.resolve()),
                            row["id"],
                        ),
                    )
                if reused_file:
                    stats.reused += 1
                else:
                    stats.downloaded += 1
            except Exception as exc:
                if cleanup_path is not None:
                    try:
                        referenced = db.query_one(
                            "SELECT 1 FROM attachments WHERE local_path=? LIMIT 1",
                            (str(cleanup_path.resolve()),),
                        )
                        if referenced is None:
                            cleanup_path.unlink(missing_ok=True)
                    except Exception as cleanup_error:
                        exc.add_note(f"orphan cleanup failed: {safe_error(cleanup_error)}")
                terminal = isinstance(exc, (Fatal, ResponseTooLarge))
                db.execute(
                    "UPDATE attachments SET status=?,attempts=attempts+1,skip_reason=? WHERE id=?",
                    (
                        "skipped" if terminal else "failed",
                        safe_error(exc),
                        row["id"],
                    ),
                )
                if terminal:
                    stats.skipped += 1
                else:
                    stats.failed += 1
    finally:
        for fetcher in fetchers.values():
            await fetcher.close()
    return stats
