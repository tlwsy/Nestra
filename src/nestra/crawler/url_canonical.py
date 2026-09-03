"""站点规则与通用 URL 规范化。"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..core.config import UrlCanonicalConfig


def canonicalize_url(url: str, base_url: str, config: UrlCanonicalConfig) -> str:
    """先应用站点重写，再去 fragment、排序 query、统一路径。"""
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 4096
        or any(ord(char) <= 32 or ord(char) == 127 for char in url)
    ):
        raise ValueError("URL 为空、过长或含控制字符")
    absolute = urljoin(base_url, url)
    parsed = urlsplit(absolute)

    for rule in config.rules:
        if not re.search(rule.match, absolute):
            continue
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if not all(name in params for name in rule.extract_params):
            continue
        try:
            path = rule.rewrite.format_map(params)
        except KeyError:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}/"
        parsed = urlsplit(urljoin(origin, path))
        break

    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in config.strip_params
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ValueError("URL 主机名非法") from exc
    port = parsed.port
    default_port = {"http": 80, "https": 443}.get(parsed.scheme)
    authority = f"[{host}]" if ":" in host else host
    netloc = f"{authority}:{port}" if port and port != default_port else authority
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))
