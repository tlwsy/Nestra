"""站点选择器优先、trafilatura 兜底的文章提取。"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urljoin

import trafilatura
from selectolax.parser import HTMLParser, Node

from ..core.config import ExtractConfig, SiteAttachmentConfig
from ..core.errors import ContentRejected, ContentTooShort, SelectorMiss
from ..core.models import ArticleText, AttachmentRef
from ..core.time import parse_flexible
from .sanitize import sanitize_html

_PDF_WIDGET = re.compile(r"showVsbpdfIframe\(\s*(['\"])([^'\"]+?)\1", re.I)


def _select(tree: HTMLParser, expression: str) -> str | None:
    selector, separator, attr = expression.rpartition("@")
    if not separator or not re.fullmatch(r"[\w:-]+", attr):
        selector, attr = expression, ""
    node = tree.css_first(selector)
    if node is None:
        return None
    value = node.attributes.get(attr) if attr else node.text(separator=" ", strip=True)
    return value.strip() if value and value.strip() else None


def _selector_content(tree: HTMLParser, config: ExtractConfig) -> tuple[str, str] | None:
    selector = config.selectors.get("content")
    if not selector:
        return None
    node = tree.css_first(selector)
    if node is None:
        return None
    for strip_selector in [*config.strip_selectors, "script", "style", "template"]:
        for child in node.css(strip_selector):
            child.decompose()
    return node.text(separator="\n", strip=True), node.html


def _metadata(tree: HTMLParser, key: str, config: ExtractConfig) -> str | None:
    expression = config.selectors.get(key)
    return _select(tree, expression) if expression else None


def _attachments(
    tree: HTMLParser,
    content_root: Node | None,
    url: str,
    config: SiteAttachmentConfig,
    max_attachments: int,
) -> list[AttachmentRef]:
    link_patterns = [re.compile(pattern, re.I) for pattern in config.link_patterns]
    text_patterns = [re.compile(pattern, re.I) for pattern in config.anchor_text_patterns]
    inline_patterns = [re.compile(pattern, re.I) for pattern in config.inline_image_patterns]
    found: list[AttachmentRef] = []
    seen: set[str] = set()

    def add(href: str, filename: str | None, *, is_body: bool = False) -> None:
        source_url = urljoin(url, href)
        if source_url in seen or len(found) >= max_attachments:
            return
        seen.add(source_url)
        found.append(AttachmentRef(source_url, filename, filename, is_body))

    if content_root is not None:
        for script in content_root.css("script"):
            for match in _PDF_WIDGET.finditer(script.text()):
                add(match.group(2), "正文.pdf", is_body=True)

    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href", "").strip()
        if not href or not any(pattern.search(href) for pattern in link_patterns):
            continue
        absolute = urljoin(url, href)
        if not any(pattern.search(absolute) for pattern in inline_patterns):
            add(href, anchor.text(separator=" ", strip=True) or None)

    if content_root is not None:
        for anchor in content_root.css("a[href]"):
            text = anchor.text(separator=" ", strip=True)
            href = anchor.attributes.get("href", "").strip()
            if href and any(pattern.search(text) for pattern in text_patterns):
                add(href, text or None)
    return found


def extract_article(
    html: str,
    url: str,
    config: ExtractConfig,
    *,
    title_hint: str | None = None,
    published_hint=None,
    last_modified=None,
    attachment_config: SiteAttachmentConfig | None = None,
    max_attachments: int = 10,
) -> ArticleText:
    tree = HTMLParser(html)
    content_root = tree.css_first(config.selectors.get("content", "body"))
    attachments = (
        _attachments(tree, content_root, url, attachment_config, max_attachments)
        if attachment_config and attachment_config.enabled
        else []
    )
    selected = _selector_content(tree, config)
    document = None
    if selected is not None:
        content_text, content_html = selected
    else:
        document = trafilatura.bare_extraction(
            html,
            url=url,
            include_links=True,
            include_formatting=True,
            with_metadata=True,
        )
        content_text = (document.text or "").strip() if document else ""
        content_html = (
            trafilatura.extract(
                html,
                url=url,
                output_format="html",
                include_links=True,
                include_formatting=True,
            )
            or ""
        )

    title = _metadata(tree, "title", config)
    if not title and document:
        title = document.title
    title = title or title_hint
    if not title:
        raise SelectorMiss(f"无法提取标题: {url}")
    if len(content_text) < config.min_content_length and attachments:
        links = "".join(
            f'<li><a href="{escape(item.source_url, quote=True)}">'
            f"{escape(item.filename or '附件')}</a></li>"
            for item in attachments
        )
        content_text = f"{content_text}\n正文见附件：" + "、".join(
            item.filename or "附件" for item in attachments
        )
        content_html += f"<p>正文见附件：</p><ul>{links}</ul>"
    if any(re.search(pattern, title, re.I) for pattern in config.reject_title_patterns) or any(
        re.search(pattern, content_text, re.I) for pattern in config.reject_content_patterns
    ):
        raise ContentRejected(f"页面命中拒绝规则: {url}")
    if len(content_text) < config.min_content_length and not attachments:
        raise ContentTooShort(
            f"正文仅 {len(content_text)} 字符，低于 {config.min_content_length}: {url}"
        )

    author = _metadata(tree, "author", config) or (document.author if document else None)
    html_node = tree.css_first("html")
    lang = html_node.attributes.get("lang", "").split("-", 1)[0] if html_node else None
    published = _metadata(tree, "published_at", config)
    page_text = tree.text(separator=" ", strip=True)
    if (regex := config.selectors.get("published_at_regex")) and (
        match := re.search(regex, page_text)
    ):
        published = match.group(1) if match.lastindex else match.group(0)
    document_date = parse_flexible(document.date) if document and document.date else None
    published_at = parse_flexible(published) if published else published_hint or document_date
    return ArticleText(
        title=title,
        author=author,
        published_at=published_at or last_modified,
        content_text=content_text,
        content_html=sanitize_html(content_html, base_url=url),
        lang=lang or None,
        attachments=tuple(attachments),
    )
