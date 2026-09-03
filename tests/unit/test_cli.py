"""M0 CLI 参数位置、退出码与数据库命令测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import uvicorn
import yaml

from nestra.cli import build_parser, main

pytestmark = pytest.mark.unit


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    data = {
        "app": {"log_format": "console"},
        "web": {
            "host": "127.0.0.1",
            "port": 8123,
            "base_url": "http://localhost:8123",
            "cookie_secure": False,
        },
        "storage": {"db_path": str(tmp_path / "db" / "nestra.db")},
        "tagset_groups": [{"slug": "g", "name": "G"}],
        "tagger": {
            "llm": {
                "providers": [
                    {
                        "name": "mock",
                        "type": "openai_compatible",
                        "base_url": "https://llm.example.test/v1",
                        "api_key_env": "CLI_TEST_API_KEY",
                        "models": ["mock"],
                    }
                ]
            }
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLI_TEST_API_KEY", "sk-test")


def test_config_flag_works_before_and_after_subcommands(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    before = build_parser().parse_args(["--config", str(path), "db", "migrate"])
    after = build_parser().parse_args(["db", "migrate", "--config", str(path)])
    assert before.config == path
    assert after.config == path


def test_config_check_success(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "check", "--config", str(config_file)]) == 0
    assert "配置校验通过" in capsys.readouterr().out


def test_config_path_can_come_from_environment(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NESTRA_CONFIG", str(config_file))
    assert main(["config", "check"]) == 0


def test_explicit_config_path_overrides_environment(
    config_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NESTRA_CONFIG", str(tmp_path / "missing.yaml"))
    assert main(["config", "check", "--config", str(config_file)]) == 0


def test_config_check_warns_when_no_tagger_is_configured(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CLI_TEST_API_KEY")
    assert main(["config", "check", "--config", str(config_file)]) == 0
    assert "停在 EXTRACTED" in capsys.readouterr().out


def test_lenient_check_skips_missing_key(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLI_TEST_API_KEY")
    assert main(["config", "check", "--lenient", "--config", str(config_file)]) == 0


def test_db_migrate_and_stats(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "migrate", "--config", str(config_file)]) == 0
    first = capsys.readouterr().out
    assert "001" in first

    assert main(["db", "migrate", "--config", str(config_file)]) == 0
    assert "无需迁移" in capsys.readouterr().out

    assert main(["db", "stats", "--config", str(config_file)]) == 0
    stats = capsys.readouterr().out
    assert "schema_migrations" in stats
    assert "users" in stats


def test_site_sync_explicitly_updates_db_snapshot(
    config_file: Path,
) -> None:
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["sites"] = [
        {
            "slug": "demo",
            "name": "First",
            "base_url": "https://example.com",
            "tagset_group": "g",
            "discovery_mode": "rss",
            "config": {"feed_url": "https://example.com/feed"},
        }
    ]
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["db", "migrate", "--config", str(config_file)]) == 0
    assert main(["site", "sync", "--site", "demo", "--config", str(config_file)]) == 0

    from nestra.core.config import load_settings
    from nestra.storage.db import Database

    settings, _ = load_settings(config_file)
    db = Database(settings.storage.db_path)
    assert db.query_one("SELECT name FROM sites WHERE slug='demo'")[0] == "First"
    data["sites"][0]["name"] = "Updated"
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["site", "sync", "--site", "demo", "--config", str(config_file)]) == 0
    assert db.query_one("SELECT name FROM sites WHERE slug='demo'")[0] == "Updated"


def test_crawl_dry_run_uses_db_site_and_does_not_import(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nestra.core.config import load_settings
    from nestra.crawler.service import CrawlStats
    from nestra.storage.db import Database
    from nestra.storage.repositories.sites import import_yaml_sites

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["sites"] = [
        {
            "slug": "demo",
            "name": "YAML name",
            "base_url": "https://example.com",
            "tagset_group": "g",
            "discovery_mode": "html_list",
            "config": {
                "list_urls": ["https://example.com/list"],
                "item_selector": "a",
            },
        }
    ]
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings, _ = load_settings(config_file)
    db = Database(settings.storage.db_path)
    db.migrate()
    import_yaml_sites(db, settings)
    db.execute("UPDATE sites SET name='DB name' WHERE slug='demo'")

    captured: dict[str, object] = {}

    async def fake_crawl(settings, db, stored, *, dry_run=False, fetcher=None):
        captured.update(stored=stored, dry_run=dry_run)
        return CrawlStats(discovered=2, extracted=2)

    monkeypatch.setattr("nestra.crawler.service.crawl_site", fake_crawl)
    assert main(["crawl", "--site", "demo", "--dry-run", "--config", str(config_file)]) == 0
    assert captured["stored"].config.name == "DB name"
    assert captured["dry_run"] is True
    assert db.query_one("SELECT COUNT(*) FROM articles")[0] == 0
    assert "discovered=2" in capsys.readouterr().out


def test_serve_honors_yaml_host_and_port(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        called["app"] = app
        called.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert main(["serve", "--config", str(config_file)]) == 0
    assert called["app"] == "nestra.web.app:app"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8123
    assert called["proxy_headers"] is False
    assert os.environ["NESTRA_CONFIG"] == str(config_file.resolve())


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert "nestra 1.0.0" in capsys.readouterr().out
