"""Full-content notification rendering with channel-safe truncation."""

from __future__ import annotations

import html
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from ..core.time import format_local
from .capabilities import body_format as supported_body_format
from .capabilities import body_limit


@dataclass(frozen=True, slots=True)
class MessageAttachment:
    filename: str
    size_bytes: int | None = None
    local_path: str | None = None
    url: str | None = None
    mime_type: str | None = None


class RenderedMessage(NamedTuple):
    title: str
    body: str
    body_format: str


def _size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            shown = f"{value:.1f}".rstrip("0").rstrip(".")
            return f" ({shown} {unit})"
        value /= 1024
    return ""


def _safe_prefix(text: str, length: int) -> str:
    prefix = text[: max(0, length)]
    while prefix and (unicodedata.combining(prefix[-1]) or prefix[-1] in "\ufe0e\ufe0f\u200d"):
        prefix = prefix[:-1]
    if prefix.endswith("\r") and text[len(prefix) :].startswith("\n"):
        prefix = prefix[:-1]
    return prefix


def truncate_unicode(text: str, limit: int, suffix: str = "…（全文见原文链接）") -> str:
    """Truncate by Unicode code points without cutting combining tails or CRLF."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return _safe_prefix(suffix, limit)
    return _safe_prefix(text, limit - len(suffix)).rstrip() + suffix


def render_message(
    *,
    title: str,
    site_name: str,
    url: str,
    published_at: datetime | None,
    tags: list[tuple[str, float]],
    content: str,
    summary: str | None = None,
    attachments: list[MessageAttachment] | None = None,
    timezone: str = "UTC",
    requested_format: str = "markdown",
    include_full_content: bool = True,
    max_body_chars: int = 8000,
    channel: str = "",
) -> RenderedMessage:
    """Render one article. ``channel`` may be a URL fingerprint (scheme only is read)."""
    fmt = supported_body_format(channel, requested_format)
    limit = body_limit(channel, max_body_chars)
    attachments = attachments or []
    date = format_local(published_at, timezone) if published_at else "未知时间"
    tag_text = " · ".join(f"{name} ({confidence:.2f})" for name, confidence in tags) or "无"
    attachment_lines = "\n".join(
        f"- {item.filename}{_size(item.size_bytes)}" + (f": {item.url}" if item.url else "")
        for item in attachments
    )
    full_content = content if include_full_content else ""
    summary = summary.strip() if summary else ""

    if fmt == "html":
        body = (
            f"<b>标签</b>：{html.escape(tag_text)}<br>"
            f"<b>来源</b>：{html.escape(site_name)} · {html.escape(date)}<br>"
            f'<b>原文</b>：<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'
        )
        if summary:
            body += f"<hr><b>AI 总结</b>：<br>{html.escape(summary).replace(chr(10), '<br>')}"
        if attachments:
            body += f"<hr>附件（{len(attachments)}）：<br>{html.escape(attachment_lines)}"
        if full_content:
            body += f"<hr><pre>{html.escape(full_content)}</pre>"
    elif fmt == "markdown":
        body = (
            f"**标签**：{tag_text}\n**来源**：{site_name} · {date}\n**原文**：{url}"
            + (f"\n\n---\n**AI 总结**：\n{summary}" if summary else "")
            + (f"\n\n---\n附件（{len(attachments)}）：\n{attachment_lines}" if attachments else "")
            + (f"\n\n---\n{full_content}" if full_content else "")
        )
    else:
        body = (
            f"标签：{tag_text}\n来源：{site_name} · {date}\n原文：{url}"
            + (f"\n\nAI 总结：\n{summary}" if summary else "")
            + (f"\n\n附件（{len(attachments)}）：\n{attachment_lines}" if attachments else "")
            + (f"\n\n{full_content}" if full_content else "")
        )
    return RenderedMessage(f"[{site_name}] {title}", truncate_unicode(body, limit), fmt)


build_message = render_message
