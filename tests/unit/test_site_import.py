"""YAML 站点首次导入 DB、幂等与 DB 优先语义测试。"""

from __future__ import annotations

import json

import pytest

from nestra.core.config import Settings
from nestra.storage.db import Database
from nestra.storage.repositories.sites import get_site, import_yaml_sites

pytestmark = pytest.mark.unit


def settings(*, site_name: str = "教务处", base_url: str = "https://jwc.example/") -> Settings:
    return Settings(
        tagset_groups=[
            {
                "slug": "campus",
                "name": "校园教务",
                "description": "课程、考试和学籍",
                "build_mode": "llm",
            }
        ],
        sites=[
            {
                "slug": "jwc",
                "name": site_name,
                "base_url": base_url,
                "tagset_group": "campus",
                "enabled": True,
                "crawl_interval_sec": 1800,
                "discovery_mode": "html_list",
                "config": {
                    "list_urls": [f"{base_url}index/tzgg.htm"],
                    "item_selector": 'li[id^="line_"]',
                    "fields": {"url": "a@href", "title": "a@title"},
                },
            }
        ],
    )


def test_import_inserts_group_and_site(db: Database) -> None:
    result = import_yaml_sites(db, settings())
    assert result.changed
    assert result.groups == ("campus",)
    assert result.sites == ("jwc",)

    group = db.query_one("SELECT * FROM tagset_groups WHERE slug='campus'")
    assert group["status"] == "draft"
    assert group["build_mode"] == "llm"

    site = db.query_one("SELECT * FROM sites WHERE slug='jwc'")
    assert site["source"] == "yaml"
    assert site["discovery_mode"] == "html_list"
    assert site["tagset_group_id"] == group["id"]
    snapshot = json.loads(site["config_json"])
    assert snapshot["tagset_group"] == "campus"
    assert snapshot["config"]["fields"]["title"] == "a@title"


def test_import_is_idempotent(db: Database) -> None:
    first = import_yaml_sites(db, settings())
    second = import_yaml_sites(db, settings())
    assert first.changed
    assert not second.changed
    assert second.groups == ()
    assert second.sites == ()
    assert db.query_one("SELECT COUNT(*) FROM tagset_groups")[0] == 1
    assert db.query_one("SELECT COUNT(*) FROM sites")[0] == 1


def test_existing_db_site_is_never_overwritten(db: Database) -> None:
    import_yaml_sites(db, settings())
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sites SET name='向导修改名', base_url='https://db-truth.example/' "
            "WHERE slug='jwc'"
        )

    result = import_yaml_sites(
        db,
        settings(site_name="旧 YAML 名", base_url="https://stale-yaml.example/"),
    )
    assert not result.changed
    site = db.query_one("SELECT name, base_url FROM sites WHERE slug='jwc'")
    assert dict(site) == {
        "name": "向导修改名",
        "base_url": "https://db-truth.example/",
    }


def test_existing_group_metadata_is_never_overwritten(db: Database) -> None:
    import_yaml_sites(db, settings())
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tagset_groups SET name='已冻结组', status='frozen', tagset_version='v1' "
            "WHERE slug='campus'"
        )

    import_yaml_sites(db, settings())
    group = db.query_one(
        "SELECT name, status, tagset_version FROM tagset_groups WHERE slug='campus'"
    )
    assert dict(group) == {"name": "已冻结组", "status": "frozen", "tagset_version": "v1"}


def test_disabled_site_is_imported_disabled(db: Database) -> None:
    value = settings()
    value.sites[0].enabled = False
    import_yaml_sites(db, value)
    assert db.query_one("SELECT enabled FROM sites WHERE slug='jwc'")[0] == 0


def test_runtime_site_loader_uses_db_columns_over_yaml_snapshot(db: Database) -> None:
    import_yaml_sites(db, settings())
    db.execute("UPDATE sites SET name='DB 真值', base_url='https://db.example/' WHERE slug='jwc'")
    stored = get_site(db, "jwc")
    assert stored is not None
    assert stored.config.name == "DB 真值"
    assert stored.config.base_url == "https://db.example"
    assert stored.config.config["list_urls"] == ["https://jwc.example/index/tzgg.htm"]
