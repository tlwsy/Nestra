"""站点选择器优先、trafilatura 兜底的文章提取。"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import trafilatura
from selectolax.parser import HTMLParser

from ..core.config import ExtractConfig, SiteAttachmentConfig
from ..core.errors import ContentRejected, ContentTooShort, SelectorMiss
from ..core.models import ArticleText, AttachmentRef
from ..core.time import parse_flexible
from .sanitize import sanitize_html


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
    selected = _selector_content(tree, config)
    document = None
    if selected:
        content_text, content_html = selected
        if len(content_text) < config.min_content_length:
            # A stale selector may still match navigation or a placeholder. Give the
            # generic extractor one chance before surfacing a configuration failure.
            document = trafilatura.bare_extraction(
                html,
                url=url,
                include_links=True,
                include_formatting=True,
                with_metadata=True,
            )
            fallback_text = (document.text or "").strip() if document else ""
            if len(fallback_text) > len(content_text):
                content_text = fallback_text
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
    if any(re.search(pattern, title, re.I) for pattern in config.reject_title_patterns) or any(
        re.search(pattern, content_text, re.I) for pattern in config.reject_content_patterns
    ):
        raise ContentRejected(f"页面命中拒绝规则: {url}")
    if len(content_text) < config.min_content_length:
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
    attachments: list[AttachmentRef] = []
    if attachment_config and attachment_config.enabled:
        root = tree.css_first(config.selectors.get("content", "body")) or tree
        link_patterns = [re.compile(pattern, re.I) for pattern in attachment_config.link_patterns]
        text_patterns = [
            re.compile(pattern, re.I) for pattern in attachment_config.anchor_text_patterns
        ]
        inline_patterns = [
            re.compile(pattern, re.I) for pattern in attachment_config.inline_image_patterns
        ]
        for anchor in root.css("a[href]"):
            href = urljoin(url, anchor.attributes.get("href", ""))
            text = anchor.text(separator=" ", strip=True) or None
            is_inline = any(pattern.search(href) for pattern in inline_patterns)
            is_attachment = any(pattern.search(href) for pattern in link_patterns) or any(
                pattern.search(text or "") for pattern in text_patterns
            )
            if href and is_attachment and not is_inline:
                attachments.append(AttachmentRef(href, text, text))
                if len(attachments) >= max_attachments:
                    break
    return ArticleText(
        title=title,
        author=author,
        published_at=published_at or last_modified,
        content_text=content_text,
        content_html=sanitize_html(content_html, base_url=url),
        lang=lang or None,
        attachments=tuple(attachments),
    )
