"""Native Anthropic Messages adapter."""

from __future__ import annotations

import json

import httpx

from ...core.config import ProviderConfig
from ...core.models import ArticleText, Tagset
from ..base import FatalConfigError, OutputInvalidError, TagResult, TransientError
from ..prompt import build_messages, parse_response
from .gemini import _raise_http


class AnthropicTagger:
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
            raise FatalConfigError(f"provider {self.provider.name} 缺少 API 密钥")
        messages = build_messages(
            article,
            tagset,
            max_input_chars=self.provider.max_input_chars,
            correction=correction,
        )
        base = (self.provider.base_url or "https://api.anthropic.com/v1").rstrip("/")
        try:
            response = await self.client.post(
                f"{base}/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1024,
                    "temperature": 0,
                    "system": messages[0]["content"],
                    "messages": [{"role": "user", "content": messages[1]["content"]}],
                },
            )
        except httpx.TransportError as exc:
            raise TransientError(f"{self.backend}: {exc}") from exc
        if response.status_code >= 400:
            _raise_http(response, self.backend)
        try:
            text = response.json()["content"][0]["text"]
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
