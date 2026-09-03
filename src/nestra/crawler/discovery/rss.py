"""RSS/Atom feed discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from time import struct_time
from urllib.parse import urljoin

import feedparser

from ...core.config import RssDiscovery, SiteConfig
from ...core.models import DiscoveredItem
from ...core.time import parse_flexible
from ..fetcher import Fetcher

_AUTO_CONTENT_MIN_LENGTH = 500


def _published(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if isinstance(value, struct_time):
            return datetime(*value[:6], tzinfo=UTC)
    for key in ("published", "updated", "created"):
        if (value := entry.get(key)) and (parsed := parse_flexible(str(value))):
            return parsed
    return None


def _content(entry: feedparser.FeedParserDict, mode: str) -> str | None:
    if mode == "never":
        return None
    values = entry.get("content") or []
    content = next((str(value.get("value", "")) for value in values if value.get("value")), "")
    if not content:
        content = str(entry.get("content_encoded", ""))
    if not content:
        return None
    return content if mode == "always" or len(content) >= _AUTO_CONTENT_MIN_LENGTH else None


async def discover(site: SiteConfig, fetcher: Fetcher) -> list[DiscoveredItem]:
    config = site.discovery
    if not isinstance(config, RssDiscovery):
        raise TypeError("rss discoverer 收到非 rss 配置")
    result = await fetcher.fetch(config.feed_url)
    if result.status_code == 304:
        return []

    feed = feedparser.parse(result.html)
    if feed.bozo and not feed.entries:
        fetcher.invalidate_conditional(config.feed_url)
        raise ValueError("RSS/Atom 解析失败")
    found: list[DiscoveredItem] = []
    seen: set[str] = set()
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            link = next(
                (
                    item.get("href")
                    for item in entry.get("links", [])
                    if item.get("href") and item.get("rel", "alternate") == "alternate"
                ),
                None,
            )
        if not link:
            continue
        url = urljoin(result.final_url or config.feed_url, str(link))
        if url in seen:
            continue
        seen.add(url)
        found.append(
            DiscoveredItem(
                url=url,
                title=str(entry.title) if entry.get("title") else None,
                published_at=_published(entry),
                summary=str(entry.summary) if entry.get("summary") else None,
                content_html=_content(entry, config.content_from_feed),
                source_page=result.final_url or config.feed_url,
            )
        )
    return found
