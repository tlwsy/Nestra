from __future__ import annotations

import json
from collections.abc import Iterable

import httpx
import pytest
import respx

from nestra.core.config import TaggerConfig
from nestra.core.errors import AllBackendsFailed
from nestra.core.models import ArticleText, TagAssignment, Tagset, TagsetEntry
from nestra.storage.db import Database
from nestra.tagger.base import TagResult
from nestra.tagger.chain import TaggerChain

pytestmark = pytest.mark.unit
NOW = "2026-01-01T00:00:00Z"


def _tagset() -> Tagset:
    return Tagset(
        "campus",
        "v1",
        "llm",
        "sum",
        (TagsetEntry("course", "选课", threshold=0.3),),
    )


def _article() -> ArticleText:
    return ArticleText("选课通知", "本学期选课开始", "<p>本学期选课开始</p>")


def _config(
    providers: Iterable[tuple[str, list[str]]],
    *,
    retries: int = 0,
    strategy: str = "llm_chain_with_local_fallback",
) -> TaggerConfig:
    return TaggerConfig(
        strategy=strategy,
        llm={
            "max_retries_per_model": retries,
            "backoff_base_sec": 0,
            "providers": [
                {
                    "name": name,
                    "type": "openai_compatible",
                    "base_url": f"https://{name}.test/v1",
                    "api_key_env": f"{name.upper()}_API_KEY",
                    "models": models,
                }
                for name, models in providers
            ],
            "circuit_breaker": {"failure_threshold": 5, "cooldown_sec": 600},
        },
        local={"enabled": False},
    )


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"tags":[{"slug":"course","confidence":0.9}]}'}}]
        },
    )


def _seed_article(db: Database) -> int:
    with db.transaction() as conn:
        group_id = conn.execute(
            "INSERT INTO tagset_groups "
            "(slug, name, tagset_version, status, frozen_at, created_at) "
            "VALUES ('campus','校园','v1','frozen',?,?)",
            (NOW, NOW),
        ).lastrowid
        site_id = conn.execute(
            "INSERT INTO sites "
            "(slug,name,base_url,discovery_mode,tagset_group_id,config_json,created_at,updated_at) "
            "VALUES ('site','站','https://site.test','rss',?,'{}',?,?)",
            (group_id, NOW, NOW),
        ).lastrowid
        article_id = conn.execute(
            "INSERT INTO articles "
            "(site_id,url,url_hash,title,content_text,content_html,status,discovered_at) "
            "VALUES (?,'https://site.test/a','hash','t','body','<p>body</p>','EXTRACTED',?)",
            (site_id, NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO tags "
            "(id,group_id,slug,name,tagset_version,frozen_at) "
            "VALUES (1,?,'course','选课','v1',?)",
            (group_id, NOW),
        )
    return article_id


@respx.mock
async def test_ordered_provider_model_fallback_fatal_and_transient(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("P1_API_KEY", "key")
    monkeypatch.setenv("P2_API_KEY", "key")
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return {"m1": httpx.Response(401), "m2": httpx.Response(503)}.get(model, _ok())

    respx.post(url__regex=r"https://p[12]\.test/v1/chat/completions").mock(side_effect=answer)
    chain = TaggerChain(_config([("p1", ["m1", "m2"]), ("p2", ["m3"])]), db)
    try:
        result = await chain.tag(_article(), _tagset())
    finally:
        await chain.aclose()

    assert calls == ["m1", "m2", "m3"]
    assert result.backend == "llm:p2:m3"
    assert db.query_one("SELECT total_calls FROM provider_health WHERE provider='p1'")[0] == 2


@respx.mock
async def test_quota_skips_remaining_models_in_provider(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("P1_API_KEY", "key")
    monkeypatch.setenv("P2_API_KEY", "key")
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(429, headers={"Retry-After": "120"}) if model == "m1" else _ok()

    respx.post(url__regex=r"https://p[12]\.test/v1/chat/completions").mock(side_effect=answer)
    chain = TaggerChain(_config([("p1", ["m1", "never"]), ("p2", ["m2"])]), db)
    try:
        result = await chain.tag(_article(), _tagset())
    finally:
        await chain.aclose()

    assert calls == ["m1", "m2"]
    assert result.backend == "llm:p2:m2"
    assert (
        db.query_one("SELECT cooldown_until FROM provider_health WHERE provider='p1'")[0]
        is not None
    )


@respx.mock
async def test_transient_retries_same_model(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P_API_KEY", "key")
    route = respx.post("https://p.test/v1/chat/completions").mock(
        side_effect=[httpx.Response(503), _ok()]
    )
    chain = TaggerChain(_config([("p", ["m"])], retries=1), db)
    try:
        result = await chain.tag(_article(), _tagset())
    finally:
        await chain.aclose()
    assert route.call_count == 2
    assert result.backend == "llm:p:m"


@respx.mock
async def test_invalid_output_gets_one_correction_then_falls_back(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("P_API_KEY", "key")
    calls: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "bad":
            return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
        return _ok()

    respx.post("https://p.test/v1/chat/completions").mock(side_effect=answer)
    chain = TaggerChain(_config([("p", ["bad", "good"])]), db)
    try:
        result = await chain.tag(_article(), _tagset())
    finally:
        await chain.aclose()

    assert [call["model"] for call in calls] == ["bad", "bad", "good"]
    assert "上一次输出不符合格式" in calls[1]["messages"][0]["content"]
    assert result.backend == "llm:p:good"


@respx.mock
async def test_summary_uses_selected_backend_before_tagging(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    article_id = _seed_article(db)
    monkeypatch.setenv("P1_API_KEY", "tag-key")
    monkeypatch.setenv("P2_API_KEY", "summary-key")
    db.execute(
        "UPDATE ai_summary_settings SET enabled=1,provider='p2',model='summary-model' WHERE id=1"
    )
    calls: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.host, body["model"]))
        if body["model"] == "summary-model":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"summary":"AI 摘要"}'}}]},
            )
        return _ok()

    respx.post(url__regex=r"https://p[12]\.test/v1/chat/completions").mock(side_effect=answer)
    chain = TaggerChain(
        _config([("p1", ["tag-model"]), ("p2", ["summary-model"])]), db
    )
    try:
        await chain.tag_article(article_id, _article(), _tagset())
    finally:
        await chain.aclose()

    assert calls == [("p2.test", "summary-model"), ("p1.test", "tag-model")]
    row = db.query_one(
        "SELECT status,summary,summary_backend,summarized_at FROM articles WHERE id=?",
        (article_id,),
    )
    assert tuple(row[:3]) == ("TAGGED", "AI 摘要", "p2:summary-model")
    assert row["summarized_at"]


@respx.mock
async def test_all_fail_keeps_article_extracted(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    article_id = _seed_article(db)
    monkeypatch.setenv("P_API_KEY", "key")
    respx.post("https://p.test/v1/chat/completions").mock(return_value=httpx.Response(401))
    chain = TaggerChain(_config([("p", ["m"])]), db)
    try:
        with pytest.raises(AllBackendsFailed):
            await chain.tag_article(article_id, _article(), _tagset())
    finally:
        await chain.aclose()
    assert db.query_one("SELECT status FROM articles WHERE id=?", (article_id,))[0] == "EXTRACTED"
    assert db.query("SELECT * FROM article_tags") == []


class _Local:
    async def tag(self, _article: ArticleText, _tagset: Tagset) -> TagResult:
        return TagResult((TagAssignment("course", 0.77, "local:test"),), "local:test")


async def test_missing_enabled_local_model_degrades_without_importing_heavy_deps(
    db: Database,
) -> None:
    config = _config([], strategy="local_only")
    config.local.enabled = True
    tagset = Tagset(
        "campus",
        "v1",
        "embedding",
        "sum",
        (TagsetEntry("course", "选课", centroid=(1.0, 0.0)),),
    )
    async with TaggerChain(config, db) as chain:
        with pytest.raises(AllBackendsFailed):
            await chain.tag(_article(), tagset)


async def test_local_backend_and_assignment_backend_are_persisted(db: Database) -> None:
    article_id = _seed_article(db)
    config = _config([], strategy="local_only")
    chain = TaggerChain(config, db, local=_Local())
    try:
        await chain.tag_article(article_id, _article(), _tagset())
    finally:
        await chain.aclose()

    row = db.query_one(
        "SELECT a.status, at.backend, at.confidence FROM articles a "
        "JOIN article_tags at ON at.article_id=a.id WHERE a.id=?",
        (article_id,),
    )
    assert tuple(row) == ("TAGGED", "local:test", 0.77)
