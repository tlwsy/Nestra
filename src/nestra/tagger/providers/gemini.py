"""Native Gemini generateContent adapter."""

from __future__ import annotations

import json

import httpx

from ...core.config import ProviderConfig
from ...core.models import ArticleText, Tagset
from ..base import FatalConfigError, OutputInvalidError, QuotaError, TagResult, TransientError
from ..prompt import build_messages, parse_response
from .openai_compatible import _QUOTA_WORDS, _retry_after


class GeminiTagger:
    def __init__(
        self,
        provider: ProviderConfig,
        model: str,
        *,
        client: httpx.AsyncClient,
        top_k: int,
        min_confidence: float,
    ) -> None:
        self.provider, self.model, self.client = provider, model, client
        self.top_k, self.min_confidence = top_k, min_confidence
        self.backend = f"llm:{provider.name}:{model}"

    async def tag(
        self, article: ArticleText, tagset: Tagset, *, correction: bool = False
    ) -> TagResult:
        if not (key := self.provider.api_key):
            raise FatalConfigError(f"provider {self.provider.name} 缺少环境变量")
        messages = build_messages(
            article,
            tagset,
            max_input_chars=self.provider.max_input_chars,
            correction=correction,
        )
        base = (
            self.provider.base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        try:
            response = await self.client.post(
                f"{base}/models/{self.model}:generateContent",
                headers={"x-goog-api-key": key},
                json={
                    "systemInstruction": {"parts": [{"text": messages[0]["content"]}]},
                    "contents": [{"role": "user", "parts": [{"text": messages[1]["content"]}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "responseMimeType": "application/json",
                        "maxOutputTokens": 1024,
                    },
                },
            )
        except httpx.TransportError as exc:
            raise TransientError(f"{self.backend}: {exc}") from exc
        if response.status_code >= 400:
            _raise_http(response, self.backend)
        try:
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise OutputInvalidError(f"{self.backend}: 响应结构非法") from exc
        if not isinstance(text, str):
            raise OutputInvalidError(f"{self.backend}: text 不是字符串")
        return TagResult(
            parse_response(
                text,
                tagset,
                backend=self.backend,
                top_k=self.top_k,
                min_confidence=self.min_confidence,
            ),
            self.backend,
        )


def _raise_http(response: httpx.Response, backend: str) -> None:
    detail = response.text[:500]
    message = f"{backend}: HTTP {response.status_code}"
    retry_after = _retry_after(response.headers.get("Retry-After"))
    if response.status_code == 429:
        if (retry_after is not None and retry_after >= 60) or any(
            word in detail.lower() for word in _QUOTA_WORDS
        ):
            raise QuotaError(message)
        raise TransientError(message, retry_after_sec=retry_after)
    if response.status_code in {408} or response.status_code >= 500:
        raise TransientError(message, retry_after_sec=retry_after)
    raise FatalConfigError(message)
