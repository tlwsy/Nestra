"""M0 FastAPI 启动链与 `/healthz` 集成测试。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from nestra.core.errors import (
    ConfigValidationError,
    MigrationError,
    StorageError,
    TagsetNotReady,
)
from nestra.storage.db import Database
from nestra.tagger.tagset import write_frozen
from nestra.web.app import create_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def write_config(tmp_path: Path, *, valid: bool = True) -> Path:
    data = {
        "app": {"log_format": "console"},
        "web": {"base_url": "http://localhost:8080", "cookie_secure": False},
        "storage": {
            "db_path": str(tmp_path / "data" / "nestra.db"),
            "attachment_dir": str(tmp_path / "data" / "attachments"),
        },
        "tagset_groups": [{"slug": "campus", "name": "校园"}],
        "tagger": {
            "llm": {
                "providers": [
                    {
                        "name": "test",
                        "type": "openai_compatible",
                        "base_url": "https://llm.example.test/v1",
                        "api_key_env": "TEST_WEB_LLM_API_KEY",
                        "models": ["mock"],
                    }
                ]
            }
        },
    }
    if not valid:
        data["tagset_groups"] = []

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WEB_LLM_API_KEY", "sk-test-not-real")


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def test_startup_migrates_and_health_returns_200(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path))
    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert "users" in app.state.db.stats()["tables"]

    assert (tmp_path / "data" / "nestra.db").is_file()


async def test_restart_is_idempotent(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    for _ in range(2):
        app = create_app(path)
        async with app.router.lifespan_context(app), client_for(app) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert app.state.db.query_one("SELECT COUNT(*) FROM schema_migrations")[0] == 5


async def test_tampered_frozen_tagset_aborts_startup(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    app = create_app(path)
    async with app.router.lifespan_context(app):
        tagset_path = app.state.settings.tagset_path("campus")
        write_frozen(
            tagset_path,
            {
                "group": "campus",
                "tagset_version": "v1",
                "build_mode": "llm",
                "tags": [],
            },
        )
        app.state.db.execute(
            "UPDATE tagset_groups SET status='frozen',tagset_version='v1' WHERE slug='campus'"
        )
    document = tagset_path.read_text(encoding="utf-8").replace('"v1"', '"v2"')
    tagset_path.write_text(document, encoding="utf-8")
    restarted = create_app(path)
    with pytest.raises(TagsetNotReady, match="checksum"):
        async with restarted.router.lifespan_context(restarted):
            pass


async def test_frozen_tagset_database_mismatch_aborts_startup(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    app = create_app(path)
    async with app.router.lifespan_context(app):
        write_frozen(
            app.state.settings.tagset_path("campus"),
            {
                "group": "campus",
                "tagset_version": "v1",
                "build_mode": "llm",
                "tags": [],
            },
        )
        app.state.db.execute(
            "UPDATE tagset_groups SET status='frozen',tagset_version='v2' WHERE slug='campus'"
        )
    restarted = create_app(path)
    with pytest.raises(TagsetNotReady, match="文件与数据库版本不一致"):
        async with restarted.router.lifespan_context(restarted):
            pass


async def test_bad_config_aborts_startup(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path, valid=False))
    with pytest.raises(ConfigValidationError, match="tagset_groups"):
        async with app.router.lifespan_context(app):
            pass


async def test_database_closes_when_startup_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    class FailingDatabase(Database):
        def migrate(self) -> list[str]:
            raise MigrationError("injected migration failure")

        def close(self) -> None:
            nonlocal closed
            closed = True
            super().close()

    monkeypatch.setattr("nestra.web.app.Database", FailingDatabase)
    app = create_app(write_config(tmp_path))
    with pytest.raises(MigrationError, match="injected"):
        async with app.router.lifespan_context(app):
            pass
    assert closed


async def test_health_returns_503_without_exposing_error(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path))
    async with app.router.lifespan_context(app), client_for(app) as client:

        def fail() -> None:
            raise StorageError("sensitive path /srv/private/nestra.db")

        app.state.db.healthcheck = fail
        response = await client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body == {"status": "unavailable"}
    assert "sensitive" not in response.text
    assert "/srv/private" not in response.text


async def test_api_documentation_is_not_public_in_m0(tmp_path: Path) -> None:
    app = create_app(write_config(tmp_path))
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
