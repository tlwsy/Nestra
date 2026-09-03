# ruff: noqa: E501
"""Secure private FastAPI Web UI."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nestra.core.config import load_settings
from nestra.core.crypto import Crypto, hash_password, new_token
from nestra.core.errors import StorageError, TagsetNotReady
from nestra.core.logging import configure_logging, get_logger
from nestra.core.time import now_iso
from nestra.scheduler import PipelineScheduler
from nestra.scheduler.jobs import build_dependencies
from nestra.storage.db import Database
from nestra.storage.files import ensure_private_directory
from nestra.storage.repositories.sites import import_yaml_sites
from nestra.tagger.bootstrap.freeze import recover_pending_tagset
from nestra.tagger.tagset import load_tagset

from .api.admin import router as admin_router
from .api.auth import router as auth_router
from .api.user import router as user_router
from .middleware import RequestBodyLimitMiddleware
from .security import RateLimiter, client_ip, request_is_https, validate_password

DEFAULT_CONFIG_PATH = Path("config/config.yaml")
WEB_DIR = Path(__file__).parent


def _bootstrap_admin(app: FastAPI) -> None:
    db = app.state.db
    settings = app.state.settings
    if db.query_one("SELECT id FROM users LIMIT 1"):
        app.state.setup_token = None
        return
    if settings.admin_password:
        password = validate_password(settings.admin_password)
        timestamp = now_iso()
        cursor = db.execute(
            "INSERT INTO users (username,password_hash,role,created_at,updated_at) VALUES ('admin',?,'admin',?,?)",
            (hash_password(password), timestamp, timestamp),
        )
        db.execute(
            "INSERT INTO audit_log (user_id,action,target_type,target_id,created_at) VALUES (?,?,?,?,?)",
            (cursor.lastrowid, "auth.bootstrap_env", "user", cursor.lastrowid, timestamp),
        )
        app.state.setup_token = None
        return
    token = app.state.crypto.sign_payload(
        {"kind": "setup", "nonce": new_token(24)}, ttl_sec=24 * 60 * 60, purpose="setup"
    )
    app.state.setup_token = token
    get_logger(__name__).warning("initial_admin_setup_required", setup_token=token)


def create_app(config_path: Path | str | None = None, *, strict_config: bool = True) -> FastAPI:
    explicit_path = Path(config_path) if config_path is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        path = explicit_path or Path(os.environ.get("NESTRA_CONFIG", DEFAULT_CONFIG_PATH))
        settings, warnings = load_settings(path, strict=strict_config)
        configure_logging(level=settings.app.log_level, fmt=settings.app.log_format)
        log = get_logger(__name__)
        for warning in warnings:
            log.warning("configuration_warning", warning=warning)
        database = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
        scheduler: PipelineScheduler | None = None
        ready = False
        try:
            applied = database.migrate()
            imported = import_yaml_sites(database, settings)
            for group in database.query(
                "SELECT id,slug,status,tagset_version FROM tagset_groups ORDER BY id"
            ):
                if group["status"] != "frozen":
                    log.warning("tagset_group_not_frozen", group=group["slug"])
                    continue
                tagset_path = settings.tagset_path(group["slug"])
                recover_pending_tagset(tagset_path, database, group["slug"])
                tagset = load_tagset(tagset_path, group=group["slug"])
                database_entries = {}
                try:
                    for row in database.query(
                        "SELECT slug,name,description,keywords,threshold FROM tags "
                        "WHERE group_id=? AND tagset_version=?",
                        (group["id"], group["tagset_version"]),
                    ):
                        database_entries[row["slug"]] = (
                            row["name"],
                            row["description"] or "",
                            tuple(json.loads(row["keywords"] or "[]")),
                            float(row["threshold"]),
                        )
                except (TypeError, ValueError) as exc:
                    raise TagsetNotReady(f"标签集 {group['slug']!r} 的数据库记录无效") from exc
                artifact_entries = {
                    entry.slug: (
                        entry.name,
                        entry.description,
                        entry.keywords,
                        entry.threshold,
                    )
                    for entry in tagset.entries
                }
                if (
                    tagset.version != group["tagset_version"]
                    or artifact_entries != database_entries
                ):
                    raise TagsetNotReady(f"标签集 {group['slug']!r} 的文件与数据库版本不一致")
                if settings.tagger.local.enabled and not tagset.has_centroids:
                    log.warning("local_tagger_centroids_missing", group=group["slug"])
            database.healthcheck()
            if settings.attachments.enabled:
                ensure_private_directory(settings.storage.attachment_dir.resolve())
            app.state.settings = settings
            app.state.db = database
            app.state.crypto = Crypto(settings.secret_key)
            app.state.started_at = now_iso()
            app.state.config_path = path
            app.state.limiter = RateLimiter()
            app.state.probe_tasks = {}
            app.state.tagset_tasks = {}
            app.state.fake_password_hash = hash_password(new_token())
            _bootstrap_admin(app)
            try:
                scheduler = PipelineScheduler(build_dependencies(settings, database))
                scheduler.start()
                app.state.scheduler = scheduler
            except ImportError:
                scheduler = None
                log.warning("scheduler_unavailable", reason="install notify extra")
            ready = True
            log.info(
                "application_started",
                config_path=str(path),
                migrations=applied,
                imported_groups=imported.groups,
                imported_sites=imported.sites,
            )
            yield
        finally:
            try:
                if scheduler is not None:
                    await scheduler.aclose()
            finally:
                database.close()
            log.info("application_stopped" if ready else "application_startup_aborted")

    application = FastAPI(
        title="Nestra",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(RequestBodyLimitMiddleware)
    application.state.templates = Jinja2Templates(directory=WEB_DIR / "templates")
    application.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        settings = getattr(application.state, "settings", None)
        trusted = settings.web.trusted_proxies if settings else []
        request.state.client_ip = client_ip(request, trusted)
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'",
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request_is_https(request, trusted):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        database: Database | None = getattr(application.state, "db", None)
        if database is None:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        try:
            database.healthcheck()
        except (StorageError, sqlite3.Error) as exc:
            get_logger(__name__).error("healthcheck_failed", error_type=type(exc).__name__)
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse({"status": "ok"})

    application.include_router(auth_router)
    application.include_router(admin_router)
    application.include_router(user_router)
    return application


app = create_app()
