"""Extractor-backed, read-only onboarding preview."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nestra.core.config import SiteConfig
from nestra.core.models import ArticleText, AttachmentRef, DiscoveredItem, FetchResult
from nestra.crawler.discovery.html_list import discover as discover_html_list
from nestra.crawler.discovery.json_api import discover as discover_json_api
from nestra.crawler.discovery.rss import discover as discover_rss
from nestra.crawler.discovery.sitemap import discover as discover_sitemap
from nestra.crawler.url_canonical import canonicalize_url
from nestra.extractor.article import extract_article

from .probe import ProbeLimitExceeded, ProbeLimits, ProbeTimedOut, probe_site


class PreviewFetcher(Protocol):
    async def fetch(self, url: str, *, use_conditional: bool = True) -> FetchResult: ...


Discoverer = Callable[[SiteConfig, PreviewFetcher], Awaitable[list[DiscoveredItem]]]
Extractor = Callable[..., ArticleText]


@dataclass(frozen=True, slots=True)
class DryRunLimits:
    sample_size: int = 10
    max_pages: int = 40
    max_duration_sec: float = 120


@dataclass(frozen=True, slots=True)
class PreviewItem:
    url: str
    success: bool
    title: str | None = None
    published_at: datetime | None = None
    content_length: int = 0
    summary: str = ""
    attachments: tuple[AttachmentRef, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DryRunReport:
    items: tuple[PreviewItem, ...]
    discovered: int
    succeeded: int
    failed: int
    duration_ms: int


class _BudgetedFetcher:
    def __init__(
        self,
        fetcher: PreviewFetcher,
        limits: DryRunLimits,
        clock: Callable[[], float],
    ) -> None:
        self.fetcher = fetcher
        self.limits = limits
        self.clock = clock
        self.started = clock()
        self.pages = 0

    def remaining(self) -> float:
        remaining = self.limits.max_duration_sec - (self.clock() - self.started)
        if remaining <= 0:
            raise ProbeTimedOut(f"dry run exceeded {self.limits.max_duration_sec}s")
        return remaining

    async def fetch(self, url: str, *, use_conditional: bool = True) -> FetchResult:
        if self.pages >= self.limits.max_pages:
            raise ProbeLimitExceeded(f"dry run exceeded {self.limits.max_pages} page requests")
        remaining = self.remaining()
        self.pages += 1
        try:
            async with asyncio.timeout(remaining):
                return await self.fetcher.fetch(url, use_conditional=use_conditional)
        except TimeoutError as exc:
            raise ProbeTimedOut(f"dry run exceeded {self.limits.max_duration_sec}s") from exc


async def preview_site(
    site: SiteConfig,
    *,
    fetcher: PreviewFetcher,
    discoverer: Discoverer | None = None,
    extractor: Extractor = extract_article,
    limits: DryRunLimits | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DryRunReport:
    """Discover and extract a bounded preview without opening a database or persisting data."""
    if not isinstance(site, SiteConfig):
        raise TypeError("site must be a validated SiteConfig")
    limits = limits or DryRunLimits()
    if limits.sample_size < 1 or limits.max_pages < 1 or limits.max_duration_sec <= 0:
        raise ValueError("dry-run limits must be positive")
    started = clock()
    budgeted = _BudgetedFetcher(fetcher, limits, clock)
    discover = (
        discoverer
        or {
            "html_list": discover_html_list,
            "rss": discover_rss,
            "sitemap": discover_sitemap,
            "json_api": discover_json_api,
        }[site.discovery_mode]
    )
    try:
        async with asyncio.timeout(budgeted.remaining()):
            items = await discover(site, budgeted)
    except TimeoutError as exc:
        raise ProbeTimedOut(f"dry run exceeded {limits.max_duration_sec}s") from exc
    previews: list[PreviewItem] = []
    for item in items[: limits.sample_size]:
        canonical = item.url
        try:
            canonical = canonicalize_url(item.url, site.base_url, site.url_canonical)
            budgeted.remaining()
            if item.content_html:
                html = item.content_html
                final_url = canonical
                last_modified = None
            else:
                result = await budgeted.fetch(canonical, use_conditional=False)
                html = result.html
                final_url = canonicalize_url(result.final_url, site.base_url, site.url_canonical)
                last_modified = result.last_modified
            article = extractor(
                html,
                final_url,
                site.extract,
                title_hint=item.title,
                published_hint=item.published_at,
                last_modified=last_modified,
                attachment_config=site.attachments,
            )
            previews.append(
                PreviewItem(
                    url=canonical,
                    success=True,
                    title=article.title,
                    published_at=article.published_at,
                    content_length=article.word_count,
                    summary=article.content_text[:240],
                    attachments=article.attachments,
                )
            )
        except Exception as exc:  # one bad article must not hide successful previews
            previews.append(
                PreviewItem(
                    url=canonical,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    succeeded = sum(item.success for item in previews)
    return DryRunReport(
        items=tuple(previews),
        discovered=len(items),
        succeeded=succeeded,
        failed=len(previews) - succeeded,
        duration_ms=int((clock() - started) * 1000),
    )


# Compatibility: the original standalone probe remains available under its old name.
dry_run = probe_site
dry_run_preview = preview_site

__all__ = [
    "DryRunLimits",
    "DryRunReport",
    "PreviewFetcher",
    "PreviewItem",
    "ProbeLimits",
    "dry_run",
    "dry_run_preview",
    "preview_site",
]
