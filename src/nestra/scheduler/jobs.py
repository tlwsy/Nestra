"""Small scheduler jobs that delegate all business work to existing modules."""

from __future__ import annotations

import inspect
import shutil
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.crypto import Crypto
from ..core.logging import get_logger, safe_error
from ..core.models import ArticleText, Tagset
from ..core.time import from_iso, now, to_iso
from ..crawler.attachments import download_pending
from ..crawler.service import crawl_site
from ..notifier.apprise_client import AppriseClient
from ..notifier.dispatcher import Dispatcher
from ..notifier.matcher import Matcher
from ..storage.db import Database
from ..storage.files import attachment_path
from ..storage.repositories.sites import StoredSite, get_site
from ..tagger.chain import TaggerChain
from ..tagger.tagset import load_tagset

log = get_logger(__name__)

Crawler = Callable[[Settings, Database, StoredSite], Awaitable[Any]]
SiteLoader = Callable[[Database, str], StoredSite | None]
TagsetLoader = Callable[..., Tagset]
Unlink = Callable[[Path], None]
AttachmentDownloader = Callable[..., Awaitable[Any]]


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


@dataclass(slots=True)
class JobDependencies:
    """Concrete services, replaceable with fakes without touching the network."""

    settings: Settings
    db: Database
    tagger: Any
    matcher: Any
    dispatcher: Any
    crawler: Crawler = crawl_site
    site_loader: SiteLoader = get_site
    tagset_loader: TagsetLoader = load_tagset
    attachment_downloader: AttachmentDownloader = download_pending
    unlink: Unlink = _unlink

    async def aclose(self) -> None:
        close = getattr(self.tagger, "aclose", None)
        if close is not None and inspect.isawaitable(result := close()):
            await result


def build_dependencies(settings: Settings, db: Database) -> JobDependencies:
    """Build production services; constructors perform no network I/O."""
    notify = settings.notify
    crypto = Crypto(settings.secret_key)
    return JobDependencies(
        settings=settings,
        db=db,
        tagger=TaggerChain(settings.tagger, db),
        matcher=Matcher(
            db,
            timezone=settings.app.timezone,
            dedupe_window_days=notify.dedupe_window_days,
        ),
        dispatcher=Dispatcher(
            db,
            AppriseClient(crypto),
            timezone=settings.app.timezone,
            body_format=notify.body_format,
            include_full_content=notify.include_full_content,
            max_body_chars=notify.max_body_chars,
            attachment_mode=notify.attachment_mode,
            attachment_inline_max_mb=notify.attachment_inline_max_mb,
            max_attempts=notify.retry.max_attempts,
            backoff_base_sec=notify.retry.backoff_base_sec,
            target_auto_disable_after_failures=notify.target_auto_disable_after_failures,
            crypto=crypto,
            base_url=settings.web.base_url,
            signed_link_ttl_hours=notify.signed_link_ttl_hours,
        ),
    )


async def _admin_alert(deps: JobDependencies, kind: str, message: str) -> None:
    if not deps.settings.alerts.enabled:
        return
    if deps.db.query_one(
        "SELECT 1 FROM audit_log WHERE action='system.alert' AND detail=? "
        "AND datetime(created_at)>datetime(?,'-1 hour') LIMIT 1",
        (kind, to_iso(now())),
    ):
        return
    deps.db.execute(
        "INSERT INTO audit_log (action,target_type,detail,created_at) "
        "VALUES ('system.alert','system',?,?)",
        (kind, to_iso(now())),
    )
    log.warning("system_alert", kind=kind)
    client = getattr(deps.dispatcher, "client", None)
    if client is None:
        return
    for row in deps.db.query(
        "SELECT n.apprise_url_enc FROM notify_targets n JOIN users u ON u.id=n.user_id "
        "WHERE u.role='admin' AND u.is_active=1 AND n.enabled=1"
    ):
        try:
            result = client.notify(
                row["apprise_url_enc"],
                body=message,
                title="Nestra alert",
                body_format="text",
                attachments=(),
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _failed("admin_alert", kind, exc)


def _failed(job: str, item: str, exc: Exception) -> None:
    log.error(
        "scheduler_item_failed",
        job=job,
        item=item,
        error=f"{type(exc).__name__}: {exc}",
        exc_info=True,
    )


async def crawl_sites(deps: JobDependencies, *, at: datetime | None = None) -> int:
    """Crawl enabled sites whose site-specific interval has elapsed."""
    rows = deps.db.query(
        "SELECT slug,consecutive_failures FROM sites WHERE enabled=1 "
        "AND (last_crawled_at IS NULL OR datetime(last_crawled_at, "
        "printf('+%d seconds',crawl_interval_sec))<=datetime(?)) "
        "ORDER BY id",
        (to_iso(at or now()),),
    )
    completed = 0
    for row in rows:
        slug = row["slug"]
        try:
            site = deps.site_loader(deps.db, slug)
            if site is not None:
                await deps.crawler(deps.settings, deps.db, site)
                completed += 1
        except Exception as exc:  # one broken site must not block the others
            _failed("crawl_sites", slug, exc)
            current = deps.db.query_one(
                "SELECT consecutive_failures FROM sites WHERE slug=?", (slug,)
            )
            if current and current["consecutive_failures"] <= row["consecutive_failures"]:
                deps.db.execute(
                    "UPDATE sites SET consecutive_failures=consecutive_failures+1,"
                    "last_error=? WHERE slug=?",
                    (safe_error(exc), slug),
                )
        failed = deps.db.query_one("SELECT consecutive_failures FROM sites WHERE slug=?", (slug,))
        threshold = deps.settings.alerts.on_site_consecutive_failures
        if failed and failed["consecutive_failures"] >= threshold:
            await _admin_alert(
                deps,
                f"site:{slug}",
                f"Site {slug} failed {failed['consecutive_failures']} consecutive crawls.",
            )
    return completed


async def tag_articles(deps: JobDependencies) -> int:
    """Tag one configured batch for every frozen tagset group."""
    groups = deps.db.query("SELECT slug FROM tagset_groups WHERE status='frozen' ORDER BY id")
    completed = 0
    for group_row in groups:
        group = group_row["slug"]
        try:
            tagset = deps.tagset_loader(deps.settings.tagset_path(group), group=group)
        except Exception as exc:
            _failed("tag_articles", group, exc)
            continue
        rows = deps.db.query(
            "SELECT a.id,a.title,a.content_text,a.content_html,a.summary,a.author,"
            "a.published_at,a.lang FROM articles a JOIN sites s ON s.id=a.site_id "
            "JOIN tagset_groups g ON g.id=s.tagset_group_id "
            "WHERE a.status='EXTRACTED' AND g.slug=? ORDER BY a.id LIMIT ?",
            (group, deps.settings.tagger.tagset.batch_size),
        )
        for row in rows:
            try:
                article = ArticleText(
                    title=row["title"] or "无标题",
                    content_text=row["content_text"] or "",
                    content_html=row["content_html"] or "",
                    summary=row["summary"],
                    author=row["author"],
                    published_at=from_iso(row["published_at"]),
                    lang=row["lang"],
                )
                await deps.tagger.tag_article(row["id"], article, tagset)
                completed += 1
            except Exception as exc:  # backend/article errors stay EXTRACTED for the next run
                _failed("tag_articles", str(row["id"]), exc)
    providers = deps.settings.tagger.llm.providers
    if providers and deps.settings.alerts.on_all_providers_down:
        query = (
            "SELECT COUNT(*) FROM provider_health WHERE provider IN ("  # noqa: S608
            + ",".join("?" for _ in providers)
            + ") AND datetime(cooldown_until)>datetime(?)"
        )
        cooling = deps.db.query_one(
            query, (*[provider.name for provider in providers], to_iso(now()))
        )[0]
        if cooling == len(providers):
            await _admin_alert(deps, "providers:all-down", "All LLM providers are cooling down.")
    return completed


async def download_attachments(deps: JobDependencies) -> int:
    """Download a bounded attachment batch independently from article tagging."""
    stats = await deps.attachment_downloader(deps.settings, deps.db, limit=20)
    return stats.downloaded + stats.reused


async def dispatch_notifications(deps: JobDependencies) -> int:
    """Match TAGGED articles; Matcher atomically creates pending deliveries."""
    rows = deps.db.query("SELECT id FROM articles WHERE status='TAGGED' ORDER BY id LIMIT 100")
    completed = 0
    for row in rows:
        try:
            deps.matcher.match(row["id"])
            completed += 1
        except Exception as exc:  # one malformed article/subscription cannot stall matching
            _failed("dispatch_notifications", str(row["id"]), exc)
    return completed


async def retry_deliveries(deps: JobDependencies) -> int:
    """Send due pending deliveries; Dispatcher owns retry/backoff state transitions."""
    return len(await deps.dispatcher.send_due(limit=100))


async def housekeeping(deps: JobDependencies, *, at: datetime | None = None) -> dict[str, int]:
    """Apply configured retention, remove attachment files, then refresh DB statistics."""
    at = at or now()
    retention = deps.settings.retention
    counts: dict[str, int] = {}
    if retention.session_cleanup:
        counts["sessions"] = deps.db.execute(
            "DELETE FROM sessions WHERE datetime(expires_at)<=datetime(?)", (to_iso(at),)
        ).rowcount

    article_cutoff = to_iso(at - timedelta(days=retention.article_days))
    counts["articles"] = deps.db.execute(
        "UPDATE articles SET content_html=NULL WHERE status='NOTIFIED' "
        "AND content_html IS NOT NULL "
        "AND datetime(COALESCE(published_at,discovered_at))<datetime(?)",
        (article_cutoff,),
    ).rowcount

    attachment_cutoff = to_iso(at - timedelta(days=retention.attachment_days))
    attachments = deps.db.query(
        "SELECT a.id,a.local_path FROM attachments a WHERE a.local_path IS NOT NULL "
        "AND datetime(a.created_at)<datetime(?) AND NOT EXISTS ("
        "SELECT 1 FROM deliveries d WHERE d.article_id=a.article_id AND d.status='pending') "
        "ORDER BY a.id",
        (attachment_cutoff,),
    )
    removed = 0
    root = deps.settings.storage.attachment_dir.resolve()
    paths = Counter(
        path
        for row in deps.db.query("SELECT local_path FROM attachments WHERE local_path IS NOT NULL")
        if (path := attachment_path(row["local_path"], root)) is not None
    )
    for attachment in attachments:
        path = attachment_path(attachment["local_path"], root)
        deps.db.execute("UPDATE attachments SET local_path=NULL WHERE id=?", (attachment["id"],))
        if path is not None and paths[path] <= 1:
            try:
                deps.unlink(path)
            except OSError as exc:
                deps.db.execute(
                    "UPDATE attachments SET local_path=? WHERE id=?",
                    (attachment["local_path"], attachment["id"]),
                )
                _failed("housekeeping", str(attachment["id"]), exc)
                continue
        if path is not None:
            paths[path] -= 1
        removed += 1
    counts["attachments"] = removed

    audit_cutoff = to_iso(at - timedelta(days=retention.audit_days))
    counts["audit_log"] = deps.db.execute(
        "DELETE FROM audit_log WHERE datetime(created_at)<datetime(?)", (audit_cutoff,)
    ).rowcount
    deps.db.analyze()
    deps.db.execute("PRAGMA incremental_vacuum(1000)")
    usage = shutil.disk_usage(Path(deps.settings.storage.db_path).parent)
    usage_pct = round(usage.used * 100 / usage.total) if usage.total else 0
    if usage_pct >= deps.settings.alerts.on_disk_usage_pct:
        await _admin_alert(
            deps,
            "disk:high",
            f"Disk usage is {usage_pct}% (threshold {deps.settings.alerts.on_disk_usage_pct}%).",
        )
    return counts


async def run_pipeline_once(deps: JobDependencies) -> dict[str, int]:
    """Run the operational pipeline once, in dependency order."""
    return {
        "crawl_sites": await crawl_sites(deps),
        "download_attachments": await download_attachments(deps),
        "tag_articles": await tag_articles(deps),
        "dispatch_notifications": await dispatch_notifications(deps),
        "retry_deliveries": await retry_deliveries(deps),
    }
