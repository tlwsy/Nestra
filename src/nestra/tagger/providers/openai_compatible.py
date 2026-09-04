"""OpenAI-compatible chat-completions tagger."""

from __future__ import annotations

import json

import httpx

from ...core.config import ProviderConfig
from ...core.models import ArticleText, Tagset
from ..base import (
    FatalConfigError,
    OutputInvalidError,
    QuotaError,
    TagResult,
    TransientError,
)
from ..prompt import build_messages, parse_response

_QUOTA_WORDS = ("quota", "balance", "billing", "credit", "insufficient", "余额", "额度")
_MAX_RETRY_AFTER_SEC = 300.0


class OpenAICompatibleTagger:
    def __init__(
        self,
        provider: ProviderConfig,
        model: str,
        *,
        client: httpx.AsyncClient,
        top_k: int,
        min_confidence: float,
    ) -> None:
        self.provider = provider
        self.model = model
        self.client = client
        self.top_k = top_k
        self.min_confidence = min_confidence
        self.backend = f"llm:{provider.name}:{model}"

    async def tag(
        self, article: ArticleText, tagset: Tagset, *, correction: bool = False
    ) -> TagResult:
        if not self.provider.api_key:
            raise FatalConfigError(f"provider {self.provider.name} 缺少 API 密钥")
        try:
            response = await self.client.post(
                f"{self.provider.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.provider.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": build_messages(
                        article,
                        tagset,
                        max_input_chars=self.provider.max_input_chars,
                        correction=correction,
                    ),
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "max_tokens": 1024,
                },
            )
        except httpx.TransportError as exc:
            raise TransientError(f"{self.backend}: {exc}") from exc

        if response.status_code >= 400:
            self._raise_http_error(response)
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise OutputInvalidError(f"{self.backend}: 响应结构非法") from exc
        if not isinstance(content, str):
            raise OutputInvalidError(f"{self.backend}: message.content 不是字符串")
        return TagResult(
            parse_response(
                content,
                tagset,
                backend=self.backend,
                top_k=self.top_k,
                min_confidence=self.min_confidence,
            ),
            self.backend,
        )

    def _raise_http_error(self, response: httpx.Response) -> None:
        detail = response.text[:500]
        message = f"{self.backend}: HTTP {response.status_code}"
        if response.status_code == 429:
            retry_after = _retry_after(response.headers.get("Retry-After"))
            if (retry_after is not None and retry_after >= 60) or any(
                word in detail.lower() for word in _QUOTA_WORDS
            ):
                raise QuotaError(message)
            raise TransientError(message, retry_after_sec=retry_after)
        if response.status_code == 408:
            raise TransientError(message)
        if response.status_code in {401, 403, 404} or 400 <= response.status_code < 500:
            raise FatalConfigError(message)
        if response.status_code >= 500:
            raise TransientError(message)
        raise FatalConfigError(message)


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return min(_MAX_RETRY_AFTER_SEC, max(0, float(value)))
    except ValueError:
        return None
