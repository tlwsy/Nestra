"""Bounded sitemap and sitemap-index discovery."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

from defusedxml import ElementTree

from ...core.config import SiteConfig, SitemapDiscovery
from ...core.models import DiscoveredItem
from ...core.time import parse_flexible
from ..fetcher import Fetcher

MAX_INDEX_DEPTH = 5
MAX_SITEMAPS = 100


def _name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    child = next((item for item in element if _name(item) == name), None)
    return child.text.strip() if child is not None and child.text and child.text.strip() else None


def _lastmod(value: str | None) -> datetime | None:
    return parse_flexible(value) if value else None


async def discover(site: SiteConfig, fetcher: Fetcher) -> list[DiscoveredItem]:
    config = site.discovery
    if not isinstance(config, SitemapDiscovery):
        raise TypeError("sitemap discoverer 收到非 sitemap 配置")

    pattern = config.url_allow_pattern or config.url_pattern
    allow = re.compile(pattern) if pattern else None
    cutoff = (
        datetime.now(UTC) - timedelta(days=config.lastmod_within_days)
        if config.lastmod_within_days
        else None
    )
    pending = [(config.sitemap_url, 0)]
    visited: set[str] = set()
    seen_urls: set[str] = set()
    found: list[DiscoveredItem] = []

    while pending and len(visited) < MAX_SITEMAPS:
        sitemap_url, depth = pending.pop(0)
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        # Index bodies contain the child list; without a persisted body cache, a 304
        # would make independently changed children unreachable.
        result = await fetcher.fetch(sitemap_url, use_conditional=False)
        if result.status_code == 304:
            continue
        root = ElementTree.fromstring(result.html)
        kind = _name(root)
        base_url = result.final_url or sitemap_url

        if kind == "sitemapindex":
            if depth >= MAX_INDEX_DEPTH:
                continue
            for node in root:
                if _name(node) == "sitemap" and (location := _child_text(node, "loc")):
                    child_url = urljoin(base_url, location)
                    if child_url not in visited:
                        pending.append((child_url, depth + 1))
            continue
        if kind != "urlset":
            raise ValueError(f"不支持的 sitemap 根元素: {kind}")

        for node in root:
            if _name(node) != "url" or not (location := _child_text(node, "loc")):
                continue
            url = urljoin(base_url, location)
            modified = _lastmod(_child_text(node, "lastmod"))
            if url in seen_urls or (allow and not allow.search(url)):
                continue
            if cutoff and modified and modified < cutoff:
                continue
            seen_urls.add(url)
            found.append(DiscoveredItem(url=url, published_at=modified, source_page=base_url))
    return found
