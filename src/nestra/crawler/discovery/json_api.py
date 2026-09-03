"""JSON API discovery with dot-path field mapping."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ...core.config import JsonApiDiscovery, SiteConfig
from ...core.models import DiscoveredItem
from ...core.time import parse_flexible
from ..fetcher import Fetcher

_MISSING = object()


def _get(value: Any, path: str, default: Any = _MISSING) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def _query_page(url: str, param: str, page: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[param] = str(page)
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _text(value: Any) -> str | None:
    return str(value) if value is not _MISSING and value is not None else None


def parse_items(data: Any, page_url: str, config: JsonApiDiscovery) -> list[DiscoveredItem]:
    values = _get(data, config.items_path)
    if not isinstance(values, list):
        raise ValueError(f"items_path 未指向数组: {config.items_path}")

    mapping = config.field_map
    found: list[DiscoveredItem] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        raw_url = _get(value, mapping.get("url", ""))
        if raw_url is _MISSING or raw_url is None or not str(raw_url).strip():
            continue
        published = (
            _text(_get(value, mapping["published_at"])) if "published_at" in mapping else None
        )
        found.append(
            DiscoveredItem(
                url=urljoin(page_url, str(raw_url)),
                title=_text(_get(value, mapping["title"])) if "title" in mapping else None,
                published_at=parse_flexible(published) if published else None,
                summary=_text(_get(value, mapping["summary"])) if "summary" in mapping else None,
                content_html=(
                    _text(_get(value, mapping["content"])) if "content" in mapping else None
                ),
                source_page=page_url,
            )
        )
    return found


async def discover(site: SiteConfig, fetcher: Fetcher) -> list[DiscoveredItem]:
    config = site.discovery
    if not isinstance(config, JsonApiDiscovery):
        raise TypeError("json_api discoverer 收到非 json_api 配置")

    pagination = config.pagination
    current = config.endpoint.format(page=1) if "{page}" in config.endpoint else config.endpoint
    found: list[DiscoveredItem] = []
    seen: set[str] = set()
    for index in range(pagination.max_pages):
        result = await fetcher.fetch(current)
        if result.status_code == 304:
            break
        try:
            data = json.loads(result.html)
            page_items = parse_items(data, result.final_url or current, config)
        except (TypeError, ValueError):
            fetcher.invalidate_conditional(current)
            raise
        for item in page_items:
            if item.url not in seen:
                seen.add(item.url)
                found.append(item)
        if not page_items or index + 1 >= pagination.max_pages or pagination.mode == "none":
            break

        page = index + 2
        if pagination.mode == "url_template":
            if pagination.order == "desc_index":
                page = (pagination.max_page or 1) - index - 1
                if page < 1:
                    break
            current = pagination.template.format(page=page)  # type: ignore[union-attr]
        elif pagination.mode == "query_param":
            current = _query_page(config.endpoint, pagination.param or "page", page)
        else:
            next_url = _get(data, pagination.next_selector or "")
            if next_url is _MISSING or not next_url:
                break
            current = urljoin(result.final_url or current, str(next_url))
    return found
