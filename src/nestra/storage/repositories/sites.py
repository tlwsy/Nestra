"""标签集分组与站点的首次 YAML 导入。

运行期 DB 是唯一事实来源：YAML 只负责引导不存在的 slug。这里绝不更新、删除
已有记录，否则用户在 Web 向导中的修改会在重启后被旧 YAML 静默覆盖。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from nestra.core.config import Settings, SiteConfig
from nestra.core.errors import StorageError
from nestra.core.logging import safe_error
from nestra.core.time import now_iso
from nestra.storage.db import Database


@dataclass(frozen=True, slots=True)
class ImportResult:
    groups: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.groups or self.sites)


@dataclass(frozen=True, slots=True)
class StoredSite:
    id: int
    config: SiteConfig


def get_site(db: Database, slug: str) -> StoredSite | None:
    """从 DB 快照加载运行期站点配置；DB 顶层列覆盖旧快照。"""
    row = db.query_one(
        "SELECT s.*, g.slug AS tagset_group FROM sites s "
        "JOIN tagset_groups g ON g.id=s.tagset_group_id WHERE s.slug=?",
        (slug,),
    )
    if row is None:
        return None
    try:
        raw = json.loads(row["config_json"])
        raw.update(
            slug=row["slug"],
            name=row["name"],
            base_url=row["base_url"],
            discovery_mode=row["discovery_mode"],
            tagset_group=row["tagset_group"],
            enabled=bool(row["enabled"]),
            crawl_interval_sec=row["crawl_interval_sec"],
            render_js=bool(row["render_js"]),
        )
        return StoredSite(row["id"], SiteConfig.model_validate(raw))
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise StorageError(f"站点 {slug!r} 的 DB 配置无效: {exc}") from exc


def sync_yaml_site(db: Database, settings: Settings, slug: str) -> None:
    """Explicitly replace one DB site snapshot from YAML; never called at startup."""
    site = next((item for item in settings.sites if item.slug == slug), None)
    if site is None:
        raise StorageError(f"YAML 中不存在站点 {slug!r}")
    group = db.query_one("SELECT id FROM tagset_groups WHERE slug=?", (site.tagset_group,))
    if group is None:
        raise StorageError(f"标签集分组不存在: {site.tagset_group}")
    snapshot = json.dumps(
        site.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE sites SET name=?,base_url=?,discovery_mode=?,tagset_group_id=?,"
            "config_json=?,enabled=?,crawl_interval_sec=?,render_js=?,updated_at=? WHERE slug=?",
            (
                site.name,
                site.base_url,
                site.discovery_mode,
                group["id"],
                snapshot,
                int(site.enabled),
                site.crawl_interval_sec,
                int(site.render_js),
                now_iso(),
                slug,
            ),
        )
        if cursor.rowcount != 1:
            raise StorageError(f"DB 中不存在站点 {slug!r}；请先启动一次完成导入")
        conn.execute(
            "UPDATE articles SET status='DISCOVERED',attempts=0,next_attempt_at=NULL,"
            "last_error=NULL WHERE site_id=(SELECT id FROM sites WHERE slug=?) "
            "AND status='FAILED'",
            (slug,),
        )


def record_crawl(db: Database, site_id: int, error: Exception | None = None) -> None:
    """记录站点级发现结果；单篇提取失败不算站点不可达。"""
    timestamp = now_iso()
    if error is None:
        db.execute(
            "UPDATE sites SET last_crawled_at=?, last_error=NULL, consecutive_failures=0, "
            "updated_at=? WHERE id=?",
            (timestamp, timestamp, site_id),
        )
    else:
        db.execute(
            "UPDATE sites SET last_crawled_at=?, last_error=?, "
            "consecutive_failures=consecutive_failures+1, updated_at=? WHERE id=?",
            (timestamp, safe_error(error), timestamp, site_id),
        )


def _import_yaml_sites_unchecked(db: Database, settings: Settings) -> ImportResult:
    """把 YAML 中 DB 尚不存在的 group/site 插入一次。

    返回本轮插入的 slug，便于启动日志与测试。函数可在每次启动安全调用。
    """
    timestamp = now_iso()
    inserted_groups: list[str] = []
    inserted_sites: list[str] = []

    with db.transaction() as conn:
        for group in settings.tagset_groups:
            exists = conn.execute(
                "SELECT id FROM tagset_groups WHERE slug = ?",
                (group.slug,),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO tagset_groups "
                "(slug, name, description, build_mode, status, created_at) "
                "VALUES (?, ?, ?, ?, 'draft', ?)",
                (group.slug, group.name, group.description, group.build_mode, timestamp),
            )
            inserted_groups.append(group.slug)

        group_ids = {
            row["slug"]: row["id"]
            for row in conn.execute("SELECT id, slug FROM tagset_groups").fetchall()
        }

        for site in settings.sites:
            exists = conn.execute("SELECT id FROM sites WHERE slug = ?", (site.slug,)).fetchone()
            if exists:
                continue
            # SiteConfig 不含机密；完整快照让后续 crawler 不必再合并 YAML 子字段。
            config_json = json.dumps(
                site.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            conn.execute(
                "INSERT INTO sites "
                "(slug, name, base_url, discovery_mode, tagset_group_id, config_json, "
                " enabled, crawl_interval_sec, render_js, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'yaml', ?, ?)",
                (
                    site.slug,
                    site.name,
                    site.base_url,
                    site.discovery_mode,
                    group_ids[site.tagset_group],
                    config_json,
                    int(site.enabled),
                    site.crawl_interval_sec,
                    int(site.render_js),
                    timestamp,
                    timestamp,
                ),
            )
            inserted_sites.append(site.slug)

    return ImportResult(tuple(inserted_groups), tuple(inserted_sites))


def import_yaml_sites(db: Database, settings: Settings) -> ImportResult:
    """错误归一化后的公开入口。"""
    try:
        return _import_yaml_sites_unchecked(db, settings)
    except StorageError:
        raise
    except sqlite3.Error as exc:
        raise StorageError(f"导入 YAML 站点失败: {exc}") from exc
    except KeyError as exc:
        raise StorageError(f"站点引用了不存在的标签集分组: {exc}") from exc
