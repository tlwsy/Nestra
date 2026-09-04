"""storage.db 的迁移、事务与约束测试。

约束断言直接对着 001_init.sql 的真实定义写：投递去重、外键级联、
url_hash 全局唯一、tags 的 (group_id, slug) 组内唯一。
"""

from __future__ import annotations

import sqlite3

import pytest

from nestra.core.errors import MigrationError, StorageError
from nestra.storage.db import Database

pytestmark = pytest.mark.unit

NOW = "2026-01-01T00:00:00Z"


# ── 迁移 ──────────────────────────────────────────────────────


def test_database_directory_creation_error_is_wrapped(tmp_path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file")
    with pytest.raises(StorageError, match="无法创建数据库目录"):
        Database(blocker / "nestra.db")


def test_database_rejects_permissive_existing_parent_without_chmodding_it(tmp_path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    with pytest.raises(StorageError, match="权限必须为 0700"):
        Database(parent / "nestra.db")
    assert parent.stat().st_mode & 0o777 == 0o755


def test_database_open_error_is_wrapped(tmp_path) -> None:
    directory = tmp_path / "is-a-directory"
    directory.mkdir()
    db = Database(directory)
    with pytest.raises(StorageError, match="无法打开数据库"):
        db.healthcheck()


def test_migrate_creates_expected_tables(db: Database) -> None:
    tables = db.stats()["tables"]
    for name in (
        "schema_migrations",
        "users",
        "sessions",
        "tagset_groups",
        "sites",
        "articles",
        "attachments",
        "tags",
        "article_tags",
        "tag_vectors",
        "subscriptions",
        "subscription_tags",
        "notify_targets",
        "subscription_targets",
        "deliveries",
        "provider_health",
        "llm_providers",
        "audit_log",
    ):
        assert name in tables, f"缺少表 {name}"


def test_migrate_is_idempotent(db: Database) -> None:
    assert db.migrate() == [], "第二次 migrate 不应重复应用"


def test_migrate_upgrades_legacy_auto_vacuum(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_marker(id INTEGER)")
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    database = Database(path)
    try:
        database.migrate()
        assert database.query_one("PRAGMA auto_vacuum")[0] == 2
    finally:
        database.close()


def test_migrate_records_version(db: Database) -> None:
    versions = [r["version"] for r in db.query("SELECT version FROM schema_migrations")]
    assert "001" in versions


def test_migrate_rolls_back_on_bad_statement(tmp_path, monkeypatch) -> None:
    """迁移必须整体回滚：失败后不能留下半个 schema。

    这是不用 executescript 的理由——它会隐式提交，让回滚失效。
    """
    fresh = Database(tmp_path / "bad.db")
    monkeypatch.setattr(
        Database,
        "_discover",
        staticmethod(lambda: [("001", "CREATE TABLE ok_marker (id INTEGER); CREATE TABLE bad (;")]),
    )
    with pytest.raises(MigrationError, match="001"):
        fresh.migrate()

    leftover = fresh.query("SELECT name FROM sqlite_master WHERE type='table' AND name='ok_marker'")
    assert leftover == [], "失败的迁移把前面的语句留在库里了"
    # schema_migrations 是在同一事务内建的，也应一并回滚
    assert fresh.query("SELECT name FROM sqlite_master WHERE name='schema_migrations'") == []
    fresh.close()


def test_integrity_check_failure_rolls_back_migration(tmp_path, monkeypatch) -> None:
    fresh = Database(tmp_path / "broken-fk.db")
    script = """
    CREATE TABLE parent (id INTEGER PRIMARY KEY);
    CREATE TABLE child (
        parent_id INTEGER,
        FOREIGN KEY (parent_id) REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED
    );
    INSERT INTO child (parent_id) VALUES (999);
    """
    monkeypatch.setattr(Database, "_discover", staticmethod(lambda: [("001", script)]))

    with pytest.raises(MigrationError, match="外键完整性"):
        fresh.migrate()

    names = {
        row["name"] for row in fresh.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "parent" not in names
    assert "child" not in names
    assert "schema_migrations" not in names


def test_split_statements_keeps_trigger_body_intact() -> None:
    script = (
        "CREATE TABLE t (id INTEGER);\n"
        "CREATE TRIGGER tr AFTER INSERT ON t BEGIN\n"
        "  UPDATE t SET id = id + 1;\n"
        "  DELETE FROM t WHERE id > 9;\n"
        "END;\n"
    )
    stmts = Database._split_statements(script)
    assert len(stmts) == 2, "含分号的 BEGIN…END 触发器体被切断了"
    assert stmts[1].count(";") == 3


def test_split_statements_rejects_incomplete_tail() -> None:
    with pytest.raises(MigrationError, match="不完整"):
        Database._split_statements("CREATE TABLE t (id INTEGER)")


# ── PRAGMA 与运维 ─────────────────────────────────────────────


def test_wal_and_foreign_keys_enabled(db: Database) -> None:
    stats = db.stats()
    assert stats["journal_mode"] == "wal"
    assert stats["foreign_keys"] is True
    assert stats["size_bytes"] > 0


def test_close_then_reuse_reconnects(db: Database) -> None:
    """连接是线程局部 + 惰性的，close() 后再用会自动重连。

    调度器长期运行，不该因为一次主动关连就要重建 Database。
    """
    db.close()
    db.healthcheck()
    assert "users" in db.stats()["tables"]


# ── 事务 ──────────────────────────────────────────────────────


def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
            ("rollback-me", "回滚组", NOW),
        )
        raise RuntimeError("boom")

    assert db.query_one("SELECT id FROM tagset_groups WHERE slug='rollback-me'") is None


def test_transaction_commits_on_success(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
            ("kept", "保留组", NOW),
        )
    assert db.query_one("SELECT id FROM tagset_groups WHERE slug='kept'") is not None


def test_nested_transaction_uses_savepoint(db: Database) -> None:
    """内层失败即使被外层捕获，内层写入也不能随外层提交。"""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
            ("outer", "外层", NOW),
        )
        try:
            with db.transaction() as nested:
                nested.execute(
                    "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
                    ("inner", "内层", NOW),
                )
                raise RuntimeError("rollback inner")
        except RuntimeError:
            pass
        conn.execute(
            "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
            ("outer-two", "外层二", NOW),
        )

    slugs = {row["slug"] for row in db.query("SELECT slug FROM tagset_groups")}
    assert {"outer", "outer-two"} <= slugs
    assert "inner" not in slugs


# ── 约束 ──────────────────────────────────────────────────────


def _seed_site(db: Database) -> int:
    with db.transaction() as conn:
        gid = conn.execute(
            "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
            ("g", "组", NOW),
        ).lastrowid
        return conn.execute(
            "INSERT INTO sites (slug, name, base_url, discovery_mode, tagset_group_id,"
            " config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s", "站", "https://e.test/", "html_list", gid, "{}", NOW, NOW),
        ).lastrowid


def test_username_is_lowercase_ascii_and_unique(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?,?,?,?)",
            ("admin", "hash", NOW, NOW),
        )
    for invalid in ("Admin", "ädmin", "管理员", "has space"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) "
                "VALUES (?,?,?,?)",
                (invalid, "hash", NOW, NOW),
            )
    with pytest.raises(sqlite3.IntegrityError, match="username"):
        db.execute(
            "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?,?,?,?)",
            ("admin", "hash", NOW, NOW),
        )


def test_foreign_key_is_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "INSERT INTO articles (site_id, url, url_hash, discovered_at) VALUES (?,?,?,?)",
            (999, "https://e.test/a", "hash-orphan", NOW),
        )


def test_cascade_delete_removes_articles(db: Database) -> None:
    site_id = _seed_site(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO articles (site_id, url, url_hash, discovered_at) VALUES (?,?,?,?)",
            (site_id, "https://e.test/a", "hash-cascade", NOW),
        )
    with db.transaction() as conn:
        conn.execute("DELETE FROM sites WHERE id=?", (site_id,))
    assert db.query("SELECT id FROM articles") == []


def test_url_hash_is_globally_unique(db: Database) -> None:
    site_id = _seed_site(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO articles (site_id, url, url_hash, discovered_at) VALUES (?,?,?,?)",
            (site_id, "https://e.test/info/1/2.htm", "same-hash", NOW),
        )
    # content.jsp 与 /info 两种形式规范化后同一 hash，第二次必须被拒
    with pytest.raises(sqlite3.IntegrityError, match="url_hash"):
        db.execute(
            "INSERT INTO articles (site_id, url, url_hash, discovered_at) VALUES (?,?,?,?)",
            (site_id, "https://e.test/content.jsp?wbnewsid=2", "same-hash", NOW),
        )


def test_article_status_check_rejects_unknown_state(db: Database) -> None:
    site_id = _seed_site(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.execute(
            "INSERT INTO articles (site_id, url, url_hash, status, discovered_at)"
            " VALUES (?,?,?,?,?)",
            (site_id, "https://e.test/b", "hash-status", "WEIRD", NOW),
        )


def test_tag_slug_unique_per_group_but_shared_across_groups(db: Database) -> None:
    with db.transaction() as conn:
        g1 = conn.execute(
            "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
            ("g1", "组一", NOW),
        ).lastrowid
        g2 = conn.execute(
            "INSERT INTO tagset_groups (slug, name, created_at) VALUES (?,?,?)",
            ("g2", "组二", NOW),
        ).lastrowid
        for gid in (g1, g2):
            conn.execute(
                "INSERT INTO tags (group_id, slug, name, tagset_version, frozen_at)"
                " VALUES (?,?,?,?,?)",
                (gid, "xuanke", "选课", "v1", NOW),
            )
    assert len(db.query("SELECT id FROM tags WHERE slug='xuanke'")) == 2

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO tags (group_id, slug, name, tagset_version, frozen_at) VALUES (?,?,?,?,?)",
            (g1, "xuanke", "选课重复", "v1", NOW),
        )


def test_delivery_dedup_is_enforced_by_db(db: Database) -> None:
    """同一 (订阅, 文章, 目标) 只能有一条投递记录。

    去重靠 DB 唯一约束而非应用逻辑，避免重启或并发时重复推送。
    """
    site_id = _seed_site(db)
    with db.transaction() as conn:
        uid = conn.execute(
            "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?,?,?,?)",
            ("owner", "argon2-placeholder", NOW, NOW),
        ).lastrowid
        aid = conn.execute(
            "INSERT INTO articles (site_id, url, url_hash, discovered_at) VALUES (?,?,?,?)",
            (site_id, "https://e.test/c", "hash-deliver", NOW),
        ).lastrowid
        sub = conn.execute(
            "INSERT INTO subscriptions (user_id, name, created_at, updated_at) VALUES (?,?,?,?)",
            (uid, "我的订阅", NOW, NOW),
        ).lastrowid
        tgt = conn.execute(
            "INSERT INTO notify_targets (user_id, name, apprise_url_enc, created_at)"
            " VALUES (?,?,?,?)",
            (uid, "tg", b"enc-blob", NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO deliveries (subscription_id, article_id, target_id, created_at)"
            " VALUES (?,?,?,?)",
            (sub, aid, tgt, NOW),
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO deliveries (subscription_id, article_id, target_id, created_at)"
            " VALUES (?,?,?,?)",
            (sub, aid, tgt, NOW),
        )
