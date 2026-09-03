"""M1 的发现 → 抓取 → 提取流水线。"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.config import Settings
from ..core.errors import ContentRejected, CrawlError, NotFound
from ..core.time import from_iso, now
from ..extractor.article import extract_article
from ..extractor.dedupe import simhash
from ..storage.db import Database
from ..storage.repositories.articles import ArticleRepository
from ..storage.repositories.fetch_cache import FetchCacheRepository
from ..storage.repositories.sites import StoredSite, record_crawl
from .discovery.html_list import discover as discover_html_list
from .discovery.json_api import discover as discover_json_api
from .discovery.rss import discover as discover_rss
from .discovery.sitemap import discover as discover_sitemap
from .fetcher import Fetcher
from .renderer import Renderer
from .url_canonical import canonicalize_url


@dataclass(slots=True)
class CrawlStats:
    discovered: int = 0
    extracted: int = 0
    duplicates: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0


async def crawl_site(
    settings: Settings,
    db: Database | None,
    stored: StoredSite,
    *,
    dry_run: bool = False,
    fetcher: Fetcher | None = None,
) -> CrawlStats:
    site = stored.config
    if not site.enabled:
        raise CrawlError(f"站点已停用: {site.slug}")
    discover = {
        "html_list": discover_html_list,
        "rss": discover_rss,
        "sitemap": discover_sitemap,
        "json_api": discover_json_api,
    }[site.discovery_mode]

    cache = FetchCacheRepository(db) if db is not None and not dry_run else None
    owned = fetcher is None
    if fetcher is None:
        fetcher = (
            Renderer(settings.politeness, site.render)
            if site.render_js
            else Fetcher(
                settings.politeness,
                max_concurrency=site.politeness.max_concurrency,
                delay_sec=site.politeness.delay_sec,
                conditional_requests=site.politeness.conditional_requests,
                cache=cache,
            )
        )
    articles = ArticleRepository(db) if db is not None and not dry_run else None
    stats = CrawlStats()
    try:
        try:
            items = await discover(site, fetcher)
        except Exception as exc:
            if db is not None and not dry_run:
                record_crawl(db, stored.id, exc)
            raise
        if articles:
            seen = set()
            for item in items:
                try:
                    seen.add(canonicalize_url(item.url, site.base_url, site.url_canonical))
                except ValueError:
                    continue
            items.extend(item for item in articles.pending(stored.id) if item.url not in seen)
        if dry_run:
            items = items[: settings.onboarding.dryrun.sample_size]
        stats.discovered = len(items)
        for item in items:
            canonical = item.url
            row = None
            try:
                canonical = canonicalize_url(item.url, site.base_url, site.url_canonical)
                row = articles.discover(stored.id, item, canonical) if articles else None
                completed = {"EXTRACTED", "TAGGED", "NOTIFIED", "SKIPPED"}
                if row and not row.created:
                    retry_at = from_iso(row.next_attempt_at)
                    if row.status in completed or (
                        row.status == "FAILED" and (retry_at is None or retry_at > now())
                    ):
                        stats.unchanged += 1
                        continue
                # RSS/JSON 可携带正文；此时不重复请求详情页。
                if item.content_html:
                    html = item.content_html
                    final_url = canonical
                    last_modified = None
                else:
                    # 未完成文章不能用 304：上次可能已下载但提取失败，且不缓存响应正文。
                    try:
                        fetched = await fetcher.fetch(canonical, use_conditional=False)
                    except NotFound:
                        if item.url == canonical:
                            raise
                        fetched = await fetcher.fetch(item.url, use_conditional=False)
                        if row:
                            articles.update_reachable_url(row.id, fetched.final_url)
                    if fetched.status_code == 304:
                        stats.unchanged += 1
                        continue
                    html = fetched.html
                    final_url = fetched.final_url
                    last_modified = fetched.last_modified
                if row:
                    articles.mark_fetched(row.id)
                article = extract_article(
                    html,
                    final_url,
                    site.extract,
                    title_hint=item.title,
                    published_hint=item.published_at,
                    last_modified=last_modified,
                    attachment_config=(site.attachments if settings.attachments.enabled else None),
                    max_attachments=settings.attachments.max_per_article,
                )
                fingerprint = simhash(article.content_text)
                duplicate = articles.save_extracted(row.id, article, fingerprint) if row else None
                if duplicate:
                    stats.duplicates += 1
                else:
                    stats.extracted += 1
                if dry_run:
                    print(
                        f"{canonical}\n  {article.title} | {article.word_count} chars | "
                        f"simhash={fingerprint}"
                    )
            except ContentRejected as exc:
                stats.skipped += 1
                if row:
                    articles.mark_skipped(row.id, exc)
                if dry_run:
                    print(f"{canonical}\n  SKIPPED {exc}")
            except Exception as exc:  # 单篇失败不能中断整个站点批次
                stats.failed += 1
                if row:
                    articles.mark_failed(
                        row.id,
                        exc,
                        max_attempts=settings.politeness.retry.max_attempts,
                        backoff_base_sec=settings.politeness.retry.backoff_base_sec,
                    )
                if dry_run:
                    print(f"{canonical}\n  ERROR {type(exc).__name__}: {exc}")
        if db is not None and not dry_run:
            error = CrawlError(f"{stats.failed} article(s) failed") if stats.failed else None
            record_crawl(db, stored.id, error)
    finally:
        if owned:
            await fetcher.close()
    return stats
