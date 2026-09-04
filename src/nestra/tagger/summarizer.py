"""One-shot AI article summarization through an explicitly selected backend."""

from __future__ import annotations

import httpx

from ..core.config import ProviderConfig
from ..core.models import ArticleText
from .base import FatalConfigError, OutputInvalidError
from .bootstrap.llm_induct import request_json
from .prompt import _json_object, truncate_text


def _prompt(article: ArticleText, max_input_chars: int) -> str:
    return (
        "Summarize the article faithfully in its primary language. Preserve important facts, "
        "numbers, dates, and conclusions. Do not add facts or commentary. Return only a JSON "
        'object in the form {"summary":"..."}. Keep it to one concise paragraph, no more than '
        "300 Chinese characters or 600 English characters.\nTitle: "
        + truncate_text(article.title, 500)
        + "\nArticle:\n"
        + truncate_text(article.content_text, max_input_chars)
    )


async def summarize(
    article: ArticleText,
    provider: ProviderConfig,
    model: str,
    client: httpx.AsyncClient,
) -> str:
    if not provider.api_key:
        raise FatalConfigError(f"provider {provider.name} has no API key")
    raw = await request_json(
        provider,
        model,
        _prompt(article, provider.max_input_chars),
        client,
        max_tokens=1024,
    )
    if not isinstance(raw, str):
        raise OutputInvalidError("summary response is not text")
    value = _json_object(raw).get("summary")
    if not isinstance(value, str) or not value.strip():
        raise OutputInvalidError("summary response must contain a non-empty summary string")
    return truncate_text(value.strip(), 2000)
