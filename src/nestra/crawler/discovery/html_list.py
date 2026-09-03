"""CSS 选择器驱动的 HTML 列表发现。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser, Node

from ...core.config import HtmlListDiscovery, SiteConfig
from ...core.errors import SelectorMiss
from ...core.models import DiscoveredItem
from ...core.time import parse_flexible
from ..fetcher import Fetcher


def _value(node: Node, expression: str) -> str | None:
    selector, separator, attr = expression.rpartition("@")
    if not separator or not re.fullmatch(r"[\w:-]+", attr):
        selector, attr = expression, ""
    selected = node.css_first(selector) if selector else node
    if selected is None:
        return None
    value = selected.attributes.get(attr) if attr else selected.text(separator=" ", strip=True)
    return value.strip() if value and value.strip() else None


def parse_list(html: str, page_url: str, config: HtmlListDiscovery) -> list[DiscoveredItem]:
    tree = HTMLParser(html)
    nodes = tree.css(config.item_selector)
    if not nodes:
        raise SelectorMiss(f"列表选择器未匹配: {config.item_selector}")
    allow = re.compile(config.url_allow_pattern) if config.url_allow_pattern else None
    fields = config.fields
    items: list[DiscoveredItem] = []
    seen: set[str] = set()
    for node in nodes:
        href = _value(node, fields.get("url", "@href"))
        if not href:
            # 兼容 item_selector 直接选中链接的简写。
            href = node.attributes.get("href")
        if not href:
            continue
        url = urljoin(page_url, href)
        if (allow and not allow.search(url)) or url in seen:
            continue
        seen.add(url)
        published = _value(node, fields["published_at"]) if "published_at" in fields else None
        if published and config.date_format:
            try:
                published_at = datetime.strptime(published, config.date_format).replace(tzinfo=UTC)
            except ValueError:
                published_at = None
        else:
            published_at = parse_flexible(published) if published else None
        items.append(
            DiscoveredItem(
                url=url,
                title=_value(node, fields["title"]) if "title" in fields else None,
                published_at=published_at,
                summary=_value(node, fields["summary"]) if "summary" in fields else None,
                source_page=page_url,
            )
        )
    return items


def _query_page(url: str, param: str, page: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[param] = str(page)
    return urlunsplit(parsed._replace(query=urlencode(query)))


async def _pages(
    entry_url: str, config: HtmlListDiscovery, fetcher: Fetcher
) -> AsyncIterator[tuple[str, str]]:
    pagination = config.pagination
    current = entry_url
    for index in range(pagination.max_pages):
        result = await fetcher.fetch(current)
        unchanged = result.status_code == 304
        if not unchanged:
            yield current, result.html
        if index + 1 >= pagination.max_pages or pagination.mode == "none":
            return
        if pagination.mode == "url_template":
            if pagination.order == "desc_index":
                page = (pagination.max_page or 1) - index - 1
                if page < 1:
                    return
            else:
                page = index + 2
            current = pagination.template.format(page=page)  # type: ignore[union-attr]
        elif pagination.mode == "query_param":
            current = _query_page(entry_url, pagination.param or "page", index + 2)
        else:
            if unchanged:  # next-link traversal needs the cached page body.
                return
            node = HTMLParser(result.html).css_first(pagination.next_selector or "")
            href = node.attributes.get("href") if node else None
            if not href:
                return
            current = urljoin(current, href)


async def discover(site: SiteConfig, fetcher: Fetcher) -> list[DiscoveredItem]:
    config = site.discovery
    if not isinstance(config, HtmlListDiscovery):
        raise TypeError("html_list discoverer 收到非 html_list 配置")
    found: list[DiscoveredItem] = []
    seen: set[str] = set()
    for entry in config.list_urls:
        async for page_url, html in _pages(entry, config, fetcher):
            try:
                items = parse_list(html, page_url, config)
            except Exception:
                fetcher.invalidate_conditional(page_url)
                raise
            for item in items:
                if item.url not in seen:
                    seen.add(item.url)
                    found.append(item)
    return found
