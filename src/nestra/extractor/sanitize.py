"""第三方正文 HTML 的 XSS 清洗。"""

from __future__ import annotations

import nh3

_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


def sanitize_html(html: str, *, base_url: str | None = None) -> str:
    """移除脚本、事件属性、样式和危险 URL scheme，并将相对 URL 绝对化。"""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        clean_content_tags={"script", "style", "iframe", "object", "template"},
        url_schemes={"http", "https", "mailto"},
        url_relative=("rewrite_with_base", base_url) if base_url else "deny",
    )
