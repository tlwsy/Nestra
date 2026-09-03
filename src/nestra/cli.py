"""命令行入口。

M0 提供 `config check`、`db migrate`、`db stats`、`serve`、`version`。
后续里程碑挂 `probe`、`build-tagset`、`crawl` 等子命令。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sqlite3
import sys
from pathlib import Path

import yaml

from . import __version__
from .core.config import load_settings
from .core.errors import ConfigValidationError, NestraError
from .core.logging import configure_logging, get_logger

log = get_logger("nestra.cli")

DEFAULT_CONFIG = Path("config/config.yaml")


def _cmd_config_check(args: argparse.Namespace) -> int:
    settings, warnings = load_settings(args.config, strict=not args.lenient)

    print(f"✓ 配置校验通过: {args.config}")
    print(
        f"  标签集分组 {len(settings.tagset_groups)} 个: {[g.slug for g in settings.tagset_groups]}"
    )
    print(f"  站点 {len(settings.sites)} 个（YAML 中）: {[s.slug for s in settings.sites]}")

    usable = [p.name for p in settings.tagger.llm.providers if p.api_key]
    total = [p.name for p in settings.tagger.llm.providers]
    print(f"  LLM provider {len(usable)}/{len(total)} 可用: {usable or '无'}")
    print(f"  本地兜底: {'开启' if settings.tagger.local.enabled else '关闭'}")

    if warnings:
        print(f"\n⚠ {len(warnings)} 条警告:")
        for w in warnings:
            print(f"  - {w}")
    return 0


def _cmd_db_migrate(args: argparse.Namespace) -> int:
    from .storage.db import Database

    settings, _ = load_settings(args.config, strict=False)
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    applied = db.migrate()
    if applied:
        print(f"✓ 已应用迁移: {', '.join(applied)}")
    else:
        print("✓ 数据库已是最新，无需迁移")
    return 0


def _cmd_db_stats(args: argparse.Namespace) -> int:
    from .storage.db import Database

    settings, _ = load_settings(args.config, strict=False)
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    db.healthcheck()
    for key, value in db.stats().items():
        print(f"  {key}: {value}")

    tables = db.query(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    print(f"\n  表 {len(tables)} 个:")
    for row in tables:
        count = db.query_one(f'SELECT COUNT(*) AS n FROM "{row["name"]}"')  # noqa: S608
        print(f"    {row['name']:<24} {count['n'] if count else 0:>8} 行")
    return 0


def _cmd_site_sync(args: argparse.Namespace) -> int:
    from .storage.db import Database
    from .storage.repositories.sites import import_yaml_sites, sync_yaml_site

    settings, _ = load_settings(args.config)
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    try:
        db.migrate()
        import_yaml_sites(db, settings)
        sync_yaml_site(db, settings, args.site)
    finally:
        db.close()
    print(f"✓ 已用 YAML 显式更新站点 {args.site}")
    return 0


def _cmd_crawl(args: argparse.Namespace) -> int:
    from .crawler.service import crawl_site
    from .storage.db import Database
    from .storage.repositories.sites import StoredSite, get_site, import_yaml_sites

    settings, _ = load_settings(args.config)
    yaml_site = next((site for site in settings.sites if site.slug == args.site), None)
    db: Database | None = None
    stored = None

    if args.dry_run:
        # dry-run 不迁移、不导入；已有 DB 配置仍优先于 YAML。
        if settings.storage.db_path.exists():
            db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
            try:
                stored = get_site(db, args.site)
            except sqlite3.Error:
                stored = None
        if stored is None and yaml_site is not None:
            stored = StoredSite(0, yaml_site)
    else:
        db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
        db.migrate()
        import_yaml_sites(db, settings)
        stored = get_site(db, args.site)

    if stored is None:
        raise ConfigValidationError([f"站点 {args.site!r} 不存在"])
    stats = asyncio.run(crawl_site(settings, db, stored, dry_run=args.dry_run))
    print(
        f"✓ {stored.config.slug}: discovered={stats.discovered} "
        f"extracted={stats.extracted} duplicates={stats.duplicates} "
        f"unchanged={stats.unchanged} skipped={stats.skipped} failed={stats.failed}"
    )
    return 1 if stats.failed else 0


def _cmd_tag(args: argparse.Namespace) -> int:
    from .scheduler.jobs import build_dependencies, tag_articles
    from .storage.db import Database

    settings, _ = load_settings(args.config)
    if not 1 <= args.limit <= 500:
        raise ConfigValidationError(["--limit 必须在 1..500"])
    settings.tagger.tagset.batch_size = args.limit
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    try:
        db.migrate()
        dependencies = build_dependencies(settings, db)

        async def run() -> int:
            try:
                return await tag_articles(dependencies)
            finally:
                await dependencies.aclose()

        count = asyncio.run(run())
    finally:
        db.close()
    print(f"✓ 已打标 {count} 篇")
    return 0


def _cmd_run_once(args: argparse.Namespace) -> int:
    from .scheduler.jobs import build_dependencies, run_pipeline_once
    from .storage.db import Database
    from .storage.repositories.sites import import_yaml_sites

    settings, _ = load_settings(args.config)
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    try:
        db.migrate()
        import_yaml_sites(db, settings)
        dependencies = build_dependencies(settings, db)

        async def run() -> dict[str, int]:
            try:
                return await run_pipeline_once(dependencies)
            finally:
                await dependencies.aclose()

        result = asyncio.run(run())
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    from .onboarding.probe import ProbeLimits, probe_site

    settings, _ = load_settings(args.config, strict=False)
    probe = settings.onboarding.probe
    report = probe_site(
        args.url,
        limits=ProbeLimits(
            max_pages=probe.max_pages,
            max_bytes_per_page=probe.max_bytes_per_page,
            max_duration_sec=probe.max_duration_sec,
            sample_articles=probe.sample_articles,
            delay_sec=probe.delay_sec,
        ),
    )
    document = dataclasses.asdict(report)
    output = (
        json.dumps(document, ensure_ascii=False, indent=2)
        if args.format == "json"
        else yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    )
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """按 YAML 中的监听参数启动 Uvicorn。"""
    import uvicorn

    settings, _ = load_settings(args.config)
    # web.app 的 lifespan 在 worker 进程中通过这些变量找到同一配置与日志覆盖。
    os.environ["NESTRA_CONFIG"] = str(args.config.resolve())
    if cli_level := getattr(args, "log_level", None):
        os.environ["NESTRA__APP__LOG_LEVEL"] = cli_level
    if cli_format := getattr(args, "log_format", None):
        os.environ["NESTRA__APP__LOG_FORMAT"] = cli_format
    configure_logging(
        level=cli_level or settings.app.log_level,
        fmt=cli_format or settings.app.log_format,
    )
    uvicorn.run(
        "nestra.web.app:app",
        host=settings.web.host,
        port=settings.web.port,
        workers=settings.runtime.web_workers,
        log_config=None,
        access_log=True,
        proxy_headers=False,
    )
    return 0


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    """全局参数同时挂在根与子命令上。

    argparse 默认只认 `nestra --config X db migrate`，而多数人会写
    `nestra db migrate --config X`。两处都挂，子命令的值优先。
    """
    # SUPPRESS 很关键：子命令未提供时不能用 None 覆盖根解析器已读到的值。
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="配置文件路径",
    )
    parser.add_argument(
        "--log-level",
        default=argparse.SUPPRESS,
        help="覆盖日志级别",
    )
    parser.add_argument(
        "--log-format",
        default=argparse.SUPPRESS,
        choices=["json", "console"],
        help="日志格式",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nestra", description="Nestra 命令行工具")
    _add_global_flags(parser)
    parser.add_argument("--version", action="version", version=f"nestra {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    p_config = sub.add_parser("config", help="配置相关").add_subparsers(
        dest="subcommand", required=True
    )
    p_check = p_config.add_parser("check", help="校验配置文件")
    _add_global_flags(p_check)
    p_check.add_argument("--lenient", action="store_true", help="只校验结构，跳过密钥与可用性检查")
    p_check.set_defaults(func=_cmd_config_check)

    p_db = sub.add_parser("db", help="数据库相关").add_subparsers(dest="subcommand", required=True)
    for name, help_text, fn in (
        ("migrate", "应用迁移", _cmd_db_migrate),
        ("stats", "显示库信息", _cmd_db_stats),
    ):
        p = p_db.add_parser(name, help=help_text)
        _add_global_flags(p)
        p.set_defaults(func=fn)

    p_site = sub.add_parser("site", help="站点配置管理").add_subparsers(
        dest="subcommand", required=True
    )
    p_sync = p_site.add_parser("sync", help="显式用 YAML 覆盖一个 DB 站点快照")
    _add_global_flags(p_sync)
    p_sync.add_argument("--site", required=True)
    p_sync.set_defaults(func=_cmd_site_sync)

    p_crawl = sub.add_parser("crawl", help="抓取并提取站点文章")
    _add_global_flags(p_crawl)
    p_crawl.add_argument("--site", required=True, help="站点 slug")
    p_crawl.add_argument("--dry-run", action="store_true", help="仅打印预览，不写数据库")
    p_crawl.set_defaults(func=_cmd_crawl)

    p_tag = sub.add_parser("tag", help="打标一批 EXTRACTED 文章")
    _add_global_flags(p_tag)
    p_tag.add_argument("--limit", type=int, default=50)
    p_tag.set_defaults(func=_cmd_tag)

    p_run = sub.add_parser("run-once", help="依次运行一次完整流水线")
    _add_global_flags(p_run)
    p_run.set_defaults(func=_cmd_run_once)

    p_probe = sub.add_parser("probe", help="安全检测新站点并输出配置候选")
    _add_global_flags(p_probe)
    p_probe.add_argument("url")
    p_probe.add_argument("--format", choices=["json", "yaml"], default="json")
    p_probe.add_argument("--output", type=Path)
    p_probe.set_defaults(func=_cmd_probe)

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    _add_global_flags(p_serve)
    p_serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_config = os.environ.get("NESTRA_CONFIG")
    explicit_config = getattr(args, "config", None)
    args.config = explicit_config or (Path(env_config) if env_config else DEFAULT_CONFIG)
    log_level = getattr(args, "log_level", None) or "INFO"
    log_format = getattr(args, "log_format", None) or "console"
    configure_logging(level=log_level, fmt=log_format)

    try:
        return int(args.func(args))
    except ConfigValidationError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    except NestraError as exc:
        print(f"✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
