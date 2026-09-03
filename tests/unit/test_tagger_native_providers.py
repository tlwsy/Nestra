from __future__ import annotations

import json

import httpx
import pytest
import respx

from nestra.core.config import TaggerConfig
from nestra.core.errors import AllBackendsFailed
from nestra.core.models import ArticleText, Tagset, TagsetEntry
from nestra.storage.db import Database
from nestra.tagger.chain import TaggerChain

pytestmark = pytest.mark.unit


def _article() -> ArticleText:
    return ArticleText("选课", "开始选课", "<p>开始选课</p>")


def _tagset() -> Tagset:
    return Tagset("campus", "v1", "llm", "x", (TagsetEntry("course", "选课"),))


def _config(kind: str, base: str) -> TaggerConfig:
    return TaggerConfig(
        llm={
            "max_retries_per_model": 0,
            "providers": [
                {
                    "name": kind.replace("_", "-"),
                    "type": kind,
                    "base_url": base,
                    "api_key_env": f"{kind.upper()}_KEY",
                    "models": ["model"],
                }
            ],
        }
    )


@respx.mock
async def test_native_gemini_adapter(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_KEY", "secret")

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["x-goog-api-key"] == "secret"
        assert "选课" in body["contents"][0]["parts"][0]["text"]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": '{"tags":[{"slug":"course","confidence":0.9}]}'},
                            ]
                        }
                    }
                ]
            },
        )

    respx.post("https://gemini.test/models/model:generateContent").mock(side_effect=answer)
    async with TaggerChain(_config("gemini", "https://gemini.test"), db) as chain:
        result = await chain.tag(_article(), _tagset())
    assert result.backend == "llm:gemini:model"


@respx.mock
async def test_native_anthropic_adapter(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_KEY", "secret")

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["x-api-key"] == "secret"
        assert body["model"] == "model"
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"tags":[]}'}]},
        )

    respx.post("https://anthropic.test/messages").mock(side_effect=answer)
    async with TaggerChain(_config("anthropic", "https://anthropic.test"), db) as chain:
        result = await chain.tag(_article(), _tagset())
    assert result.backend == "llm:anthropic:model"
    assert result.assignments == ()


@pytest.mark.parametrize(
    ("kind", "base", "endpoint"),
    [
        (
            "openai_compatible",
            "https://openai.test/v1",
            "https://openai.test/v1/chat/completions",
        ),
        ("gemini", "https://gemini.test", "https://gemini.test/models/model:generateContent"),
        ("anthropic", "https://anthropic.test", "https://anthropic.test/messages"),
    ],
)
@respx.mock
async def test_transport_errors_degrade_to_next_backend(
    kind: str,
    base: str,
    endpoint: str,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{kind.upper()}_KEY", "secret")
    respx.post(endpoint).mock(side_effect=httpx.RemoteProtocolError("disconnected"))
    async with TaggerChain(_config(kind, base), db) as chain:
        with pytest.raises(AllBackendsFailed):
            await chain.tag(_article(), _tagset())
