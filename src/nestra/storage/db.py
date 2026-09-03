"""连接管理、pragma、事务、迁移。

`sqlite3` 而非 aiosqlite：单进程 + WAL 下 SQLite 写是串行的，异步驱动
只是把等待挪到另一个地方。用线程局部连接 + `asyncio.to_thread` 更简单。

每个线程一条连接。SQLite 连接不是线程安全的，跨线程复用会随机报
"objects created in a thread can only be used in that same thread"。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from ..core.errors import MigrationError, StorageError
from ..core.logging import get_logger
from .files import ensure_private_directory

log = get_logger(__name__)

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class Database:
    """SQLite 门面。

    用法：
        db = Database(Path("data/db/nestra.db"), cache_mb=32)
        db.migrate()
        with db.transaction() as conn:
            conn.execute("INSERT ...")
    """

    def __init__(self, path: Path, *, cache_mb: int = 32, timeout: float = 30.0) -> None:
        self.path = path
        self.cache_mb = cache_mb
        self.timeout = timeout
        self._local = threading.local()
        self._migrate_lock = threading.Lock()
        try:
            ensure_private_directory(path.parent)
        except (OSError, StorageError) as exc:
            raise StorageError(f"无法创建数据库目录 {path.parent}: {exc}") from exc

    # ── 连接 ──────────────────────────────────────────────────────

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")  # 读写不互斥
        conn.execute("PRAGMA foreign_keys = ON")  # SQLite 默认关闭
        conn.execute("PRAGMA synchronous = NORMAL")  # WAL 下足够安全
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute(f"PRAGMA cache_size = -{self.cache_mb * 1024}")  # 负数=KiB
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 134217728")

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            opened: sqlite3.Connection | None = None
            try:
                opened = sqlite3.connect(
                    self.path,
                    timeout=self.timeout,
                    isolation_level=None,  # 自己管事务，避免隐式 BEGIN
                    check_same_thread=False,
                )
                self.path.chmod(0o600)
                self._configure(opened)
            except (sqlite3.Error, OSError) as exc:
                if opened is not None:
                    opened.close()
                raise StorageError(f"无法打开数据库 {self.path}: {exc}") from exc
            self._local.conn = opened
            conn = opened
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── 事务 ──────────────────────────────────────────────────────

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """事务上下文。

        默认 `BEGIN IMMEDIATE`：立刻拿写锁，避免升级锁时才发现冲突
        而抛 SQLITE_BUSY。只读场景传 `immediate=False`。
        """
        conn = self.conn
        if conn.in_transaction:
            # 嵌套事务必须用 SAVEPOINT。若只是直接 yield，内层失败被外层捕获后，
            # 内层已经执行的写入会随外层一起提交，违背事务边界。
            depth = getattr(self._local, "savepoint_depth", 0) + 1
            self._local.savepoint_depth = depth
            name = f"nestra_sp_{depth}"
            try:
                conn.execute(f"SAVEPOINT {name}")
            except sqlite3.Error as exc:
                self._local.savepoint_depth = depth - 1
                raise StorageError(f"无法创建数据库 savepoint: {exc}") from exc
            try:
                yield conn
            except BaseException as original:
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    conn.execute(f"RELEASE SAVEPOINT {name}")
                except sqlite3.Error as cleanup_error:
                    original.add_note(f"savepoint 回滚也失败: {cleanup_error}")
                raise
            else:
                try:
                    conn.execute(f"RELEASE SAVEPOINT {name}")
                except sqlite3.Error as exc:
                    raise StorageError(f"无法提交数据库 savepoint: {exc}") from exc
            finally:
                self._local.savepoint_depth = depth - 1
            return

        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        except sqlite3.Error as exc:
            raise StorageError(f"无法开始数据库事务: {exc}") from exc
        try:
            yield conn
        except BaseException as original:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as cleanup_error:
                original.add_note(f"数据库回滚也失败: {cleanup_error}")
            raise
        else:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                error = StorageError(f"无法提交数据库事务: {exc}")
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error as cleanup_error:
                    error.add_note(f"提交失败后的回滚也失败: {cleanup_error}")
                raise error from exc

    # ── 查询辅助 ──────────────────────────────────────────────────

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    # ── 迁移 ──────────────────────────────────────────────────────

    def _applied(self, conn: sqlite3.Connection) -> set[str]:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}

    @staticmethod
    def _discover() -> list[tuple[str, str]]:
        files: list[tuple[str, str]] = []
        try:
            pkg = resources.files(__package__).joinpath("migrations")
            for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
                m = _MIGRATION_RE.match(entry.name)
                if m:
                    files.append((m.group(1), entry.read_text(encoding="utf-8")))
        except (OSError, UnicodeError) as exc:
            raise MigrationError(f"无法读取迁移文件: {exc}") from exc
        if not files:
            raise MigrationError("未找到任何迁移文件")
        return files

    @staticmethod
    def _split_statements(script: str) -> list[str]:
        """把迁移脚本切成单条语句。

        不用 `executescript`：它会隐式 COMMIT 掉当前事务，迁移就失去了
        整体回滚能力。`complete_statement` 累积到语句完整才切，含分号的
        BEGIN…END 触发器体也不会被切断。
        """
        statements: list[str] = []
        buffer = ""
        for line in script.splitlines(keepends=True):
            if not buffer and not line.strip().lstrip("-").strip():
                continue
            buffer += line
            if sqlite3.complete_statement(buffer):
                if stmt := buffer.strip():
                    statements.append(stmt)
                buffer = ""
        if leftover := buffer.strip():
            raise MigrationError(f"迁移脚本末尾存在不完整语句: {leftover[:80]!r}")
        return statements

    def migrate(self) -> list[str]:
        """应用未执行的迁移。幂等。返回本次应用的版本号。"""
        try:
            with self._migrate_lock, self.transaction() as conn:
                done = self._applied(conn)
                newly: list[str] = []
                for version, sql in self._discover():
                    if version in done:
                        continue
                    log.info("applying_migration", version=version)
                    for stmt in self._split_statements(sql):
                        try:
                            conn.execute(stmt)
                        except sqlite3.Error as exc:
                            raise MigrationError(
                                f"迁移 {version} 失败: {exc}\n语句: {stmt[:200]}"
                            ) from exc
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                    newly.append(version)

                # 必须在同一事务内检查。提交后检查会留下“报错但版本已记录”的坏库。
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise MigrationError(f"迁移后外键完整性检查失败: {[dict(r) for r in broken]}")
        except (MigrationError, StorageError):
            raise
        except (sqlite3.Error, OSError) as exc:
            raise MigrationError(f"迁移框架失败: {exc}") from exc

        try:
            if self.query_one("PRAGMA auto_vacuum")[0] != 2:
                log.info("enabling_incremental_vacuum")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                self.conn.execute("VACUUM")
                if self.query_one("PRAGMA auto_vacuum")[0] != 2:
                    raise MigrationError("无法启用 SQLite incremental auto_vacuum")
        except MigrationError:
            raise
        except sqlite3.Error as exc:
            raise MigrationError(f"启用 SQLite incremental auto_vacuum 失败: {exc}") from exc

        if newly:
            log.info("migrations_applied", versions=newly)
        return newly

    def _check_integrity(self) -> None:
        broken = self.query("PRAGMA foreign_key_check")
        if broken:
            raise MigrationError(f"迁移后外键完整性检查失败: {[dict(r) for r in broken]}")

    # ── 运维 ──────────────────────────────────────────────────────

    def vacuum(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")

    def analyze(self) -> None:
        self.conn.execute("ANALYZE")

    def stats(self) -> dict[str, Any]:
        page_count = self.query_one("PRAGMA page_count")
        page_size = self.query_one("PRAGMA page_size")
        size = (page_count[0] * page_size[0]) if page_count and page_size else 0
        tables: dict[str, int] = {}
        for row in self.query(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            name = row["name"]
            # 表名来自 sqlite_master，非外部输入；仍加引号防御异常标识符
            counted = self.query_one(f'SELECT COUNT(*) FROM "{name}"')  # noqa: S608
            tables[name] = counted[0] if counted else 0
        return {
            "path": str(self.path),
            "size_bytes": size,
            "journal_mode": (self.query_one("PRAGMA journal_mode") or [None])[0],
            "foreign_keys": bool((self.query_one("PRAGMA foreign_keys") or [0])[0]),
            "tables": tables,
        }

    def healthcheck(self) -> None:
        try:
            self.conn.execute("SELECT 1")
        except sqlite3.Error as exc:
            raise StorageError(f"数据库不可用: {exc}") from exc
