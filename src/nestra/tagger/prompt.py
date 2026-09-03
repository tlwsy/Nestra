"""Prompt construction, article truncation, and frozen-whitelist parsing."""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.models import ArticleText, TagAssignment, Tagset
from .base import OutputInvalidError

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + text[-(limit - head) :]


def build_messages(
    article: ArticleText,
    tagset: Tagset,
    *,
    max_input_chars: int,
    correction: bool = False,
) -> list[dict[str, str]]:
    labels = "\n".join(
        f"- slug: {entry.slug}\n  名称: {entry.name}\n  判定: {entry.description}"
        for entry in tagset.entries
    )
    correction_text = (
        "\n上一次输出不符合格式。只输出一个 JSON 对象，不要 Markdown 或解释。" if correction else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "你是文章分类器。只能从给定标签列表中选择，不得创造新标签。"
                '输出 JSON: {"tags":[{"slug":"...","confidence":0.0}]}。'
                f'若均不符合，输出 {{"tags":[]}}。{correction_text}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"可用标签：\n{labels}\n\n文章标题：{article.title}\n"
                f"文章正文：\n{truncate_text(article.content_text, max_input_chars)}"
            ),
        },
    ]


def _json_object(text: str) -> dict[str, Any]:
    if match := _FENCE.match(text):
        text = match.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise OutputInvalidError("LLM 输出不是 JSON") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OutputInvalidError("LLM 输出不是 JSON") from exc
    if not isinstance(value, dict):
        raise OutputInvalidError("LLM 输出根节点不是对象")
    return value


def parse_response(
    text: str,
    tagset: Tagset,
    *,
    backend: str,
    top_k: int,
    min_confidence: float = 0,
) -> tuple[TagAssignment, ...]:
    """Parse output and silently discard hallucinated or low-confidence labels."""
    raw_tags = _json_object(text).get("tags")
    if not isinstance(raw_tags, list):
        raise OutputInvalidError("LLM 输出缺少 tags 数组")

    accepted: dict[str, float] = {}
    for item in raw_tags:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            raise OutputInvalidError("tags 项结构非法")
        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise OutputInvalidError("confidence 不是数字")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise OutputInvalidError("confidence 必须在 0..1")
        entry = tagset.get(item["slug"])
        if entry is None or confidence < max(min_confidence, entry.threshold):
            continue
        accepted[entry.slug] = max(confidence, accepted.get(entry.slug, 0))

    return tuple(
        TagAssignment(slug, confidence, backend)
        for slug, confidence in sorted(accepted.items(), key=lambda item: item[1], reverse=True)[
            :top_k
        ]
    )
