from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from nestra.core.config import ProviderConfig
from nestra.core.errors import TagsetNotReady
from nestra.storage.db import Database
from nestra.tagger.bootstrap import BootstrapOptions, NativeLLMInducer, bootstrap_tagset
from nestra.tagger.bootstrap.freeze import freeze_tagset, recover_pending_tagset

pytestmark = pytest.mark.unit
NOW = "2026-01-01T00:00:00+00:00"


def _seed(db: Database, count: int = 4) -> None:
    with db.transaction() as conn:
        old_group = conn.execute(
            "INSERT INTO tagset_groups(slug,name,status,created_at) VALUES ('old','Old','draft',?)",
            (NOW,),
        ).lastrowid
        conn.execute(
            "INSERT INTO tags(id,group_id,slug,name,tagset_version,frozen_at) "
            "VALUES (41,?,'old-tag','Old','v1',?)",
            (old_group, NOW),
        )
        group_id = conn.execute(
            "INSERT INTO tagset_groups(slug,name,status,created_at) "
            "VALUES ('campus','Campus','draft',?)",
            (NOW,),
        ).lastrowid
        site_id = conn.execute(
            "INSERT INTO sites(slug,name,base_url,discovery_mode,tagset_group_id,config_json,"
            "created_at,updated_at) VALUES ('site','Site','https://test','rss',?,'{}',?,?)",
            (group_id, NOW, NOW),
        ).lastrowid
        for index in range(count):
            conn.execute(
                "INSERT INTO articles(site_id,url,url_hash,title,summary,content_text,status,"
                "discovered_at) VALUES (?,?,?,?,?,?,'EXTRACTED',?)",
                (
                    site_id,
                    f"https://test/{index}",
                    f"hash-{index}",
                    f"Title {index}",
                    f"Summary {index}",
                    f"Content {index}",
                    NOW,
                ),
            )


def _tag(slug: str, name: str, ids: list[int]) -> dict[str, object]:
    return {
        "slug": slug,
        "name": name,
        "description": f"Include {name}; exclude unrelated notices.",
        "keywords": [name],
        "article_ids": ids,
        "coverage": len(ids),
        "representative_titles": [f"Title {ids[0] - 1}"],
    }


class _Inducer:
    def __init__(self) -> None:
        self.calls = 0

    async def induce(self, _prompt: str) -> object:
        self.calls += 1
        if self.calls == 1:
            return {"tags": [_tag("course", "Courses", [1, 2])]}
        if self.calls == 2:
            return {"tags": [_tag("exam", "Exams", [3, 4])]}
        return {"tags": [_tag("course", "Courses", [1, 2]), _tag("exam", "Exams", [3, 4])]}


async def test_batches_curates_freezes_and_uses_global_tag_ids(
    db: Database, tmp_path: Path
) -> None:
    _seed(db)
    inducer = _Inducer()
    result = await bootstrap_tagset(
        db,
        tmp_path,
        BootstrapOptions("campus", batch_size=2, min_tags=2, max_tags=2, min_cluster_docs=1),
        inducer=inducer,
    )

    assert inducer.calls == 3
    assert result.frozen and result.tagset_path == tmp_path / "campus" / "tags.json"
    assert [tag["id"] for tag in result.document["tags"]] == [42, 43]
    assert db.query_one("SELECT COUNT(*) FROM tags WHERE group_id != 1")[0] == 2
    assert result.report_path.read_text(encoding="utf-8").startswith("# Tagset bootstrap report")


def test_freeze_rejects_runtime_invalid_tagset(tmp_path: Path) -> None:
    path = tmp_path / "tags.json"
    with pytest.raises(TagsetNotReady, match="threshold"):
        freeze_tagset(
            {
                "group": "campus",
                "tagset_version": "v1",
                "build_mode": "llm",
                "tags": [{"id": 1, "slug": "exam", "name": "Exam", "threshold": 2}],
            },
            path,
        )
    assert not path.exists()


def test_interrupted_freeze_is_recovered_after_database_commit(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db, 1)
    path = tmp_path / "campus" / "tags.json"
    real_replace = os.replace

    def interrupt_final_replace(source, destination):
        if Path(destination) == path:
            raise OSError("injected interruption")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", interrupt_final_replace)
    with pytest.raises(OSError, match="injected interruption"):
        freeze_tagset(
            {
                "group": "campus",
                "tagset_version": "v1",
                "build_mode": "llm",
                "tags": [_tag("exam", "Exams", [1])],
            },
            path,
            db=db,
        )
    assert db.query_one("SELECT tagset_version FROM tagset_groups WHERE slug='campus'")[0] == "v1"
    assert not path.exists() and path.with_name(".tags.json.pending").exists()

    monkeypatch.setattr(os, "replace", real_replace)
    assert recover_pending_tagset(path, db, "campus")
    assert path.exists() and not path.with_name(".tags.json.pending").exists()


async def test_refreeze_cannot_remove_a_subscribed_tag(db: Database, tmp_path: Path) -> None:
    _seed(db)
    result = await bootstrap_tagset(
        db,
        tmp_path,
        BootstrapOptions("campus", batch_size=2, min_tags=2, max_tags=2, min_cluster_docs=1),
        inducer=_Inducer(),
    )
    exam_id = db.query_one("SELECT id FROM tags WHERE slug='exam'")[0]
    with db.transaction() as conn:
        user_id = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at,updated_at) "
            "VALUES ('reader','hash','user',?,?)",
            (NOW, NOW),
        ).lastrowid
        subscription_id = conn.execute(
            "INSERT INTO subscriptions(user_id,name,created_at,updated_at) VALUES (?,'Exam',?,?)",
            (user_id, NOW, NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO subscription_tags(subscription_id,tag_id) VALUES (?,?)",
            (subscription_id, exam_id),
        )
    changed = {
        **result.document,
        "tagset_version": "v2",
        "tags": [tag for tag in result.document["tags"] if tag["slug"] == "course"],
    }
    with pytest.raises(TagsetNotReady, match="exam"):
        freeze_tagset(changed, tmp_path / "campus" / "tags.json", db=db)
    assert db.query_one("SELECT tagset_version FROM tagset_groups WHERE slug='campus'")[0] != "v2"


async def test_review_gate_writes_draft_without_persisting(db: Database, tmp_path: Path) -> None:
    _seed(db, 2)
    result = await bootstrap_tagset(
        db,
        tmp_path,
        BootstrapOptions(
            "campus",
            batch_size=2,
            min_tags=1,
            max_tags=2,
            min_cluster_docs=1,
            require_manual_review=True,
        ),
        inducer=lambda _prompt: _async_value({"tags": [_tag("course", "Courses", [1, 2])]}),
    )
    assert not result.frozen
    assert not (tmp_path / "campus" / "tags.json").exists()
    assert (tmp_path / "campus" / "tags.draft.json").exists()
    assert db.query_one("SELECT COUNT(*) FROM tags")[0] == 1


async def _async_value(value: object) -> object:
    return value


@respx.mock
async def test_native_inducer_rejects_invalid_output_and_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P1_KEY", "key")
    monkeypatch.setenv("P2_KEY", "key")
    providers = [
        ProviderConfig(
            name=name,
            type="openai_compatible",
            base_url=f"https://{name}.test/v1",
            api_key_env=key,
            models=["model"],
        )
        for name, key in (("p1", "P1_KEY"), ("p2", "P2_KEY"))
    ]
    respx.post("https://p1.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    valid = json.dumps({"tags": [_tag("course", "Courses", [1])]})
    second = respx.post("https://p2.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": valid}}]})
    )
    async with httpx.AsyncClient() as client:
        tags = await NativeLLMInducer(providers, client).induce("prompt")
    assert [tag.slug for tag in tags] == ["course"]
    assert second.called
