# ruff: noqa: E501
"""Boring admin CRUD, system views, audit log and safe onboarding entrypoints."""

from __future__ import annotations

import dataclasses
import hashlib
import html
import ipaddress
import json
import os
import re
import resource
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from nestra.core.config import ProviderConfig, SiteConfig
from nestra.core.crypto import Crypto, hash_password, new_token
from nestra.core.logging import get_logger, safe_error
from nestra.core.time import now_iso
from nestra.crawler.fetcher import Fetcher
from nestra.crawler.renderer import Renderer
from nestra.crawler.service import crawl_site
from nestra.onboarding.dryrun import DryRunLimits, preview_site
from nestra.onboarding.probe import ProbeLimits, probe_site
from nestra.onboarding.ssrf import resolve_url
from nestra.storage.files import attachment_path
from nestra.storage.repositories.providers import runtime_providers
from nestra.storage.repositories.sites import get_site
from nestra.tagger.bootstrap import BootstrapOptions, NativeLLMInducer, bootstrap_tagset
from nestra.tagger.bootstrap.freeze import freeze_tagset

from ..deps import AdminUser, flag, integer, request_data, wants_json, write_guard
from ..security import ADVANCED_COOKIE, CSRF_COOKIE, audit, validate_password, validate_username

router = APIRouter(prefix="/admin")
log = get_logger(__name__)


def _page(
    request: Request,
    title: str,
    rows: list[dict],
    user: dict,
    *,
    template: str = "list.html",
    **context,
):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "title": title,
            "rows": rows,
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
            "advanced": request.cookies.get(ADVANCED_COOKIE) == "1",
            **context,
        },
    )


def _last_admin(db, user_id: int) -> bool:
    row = db.query_one("SELECT role,is_active FROM users WHERE id=?", (user_id,))
    count = db.query_one("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1")[0]
    return bool(row and row["role"] == "admin" and row["is_active"] and count <= 1)


@router.get("/users")
async def users(request: Request, user: AdminUser):
    rows = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT id,username,email,role,is_active,failed_logins,locked_until,created_at,updated_at,"
            "(totp_secret IS NOT NULL) AS totp_enabled FROM users ORDER BY id"
        )
    ]
    return (
        rows
        if wants_json(request)
        else _page(request, "Users", rows, user, template="admin_users.html")
    )


@router.post("/users")
async def create_user(request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    try:
        username = validate_username(data.get("username"))
        password = validate_password(data.get("password") or new_token(12))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    role = data.get("role", "user")
    if role not in ("admin", "user"):
        raise HTTPException(400, "invalid role")
    timestamp = now_iso()
    try:
        cursor = request.app.state.db.execute(
            "INSERT INTO users (username,email,password_hash,role,is_active,must_change_password,"
            "created_at,updated_at) VALUES (?,?,?,?,1,1,?,?)",
            (
                username,
                str(data.get("email", "")).strip() or None,
                hash_password(password),
                role,
                timestamp,
                timestamp,
            ),
        )
    except Exception as exc:
        raise HTTPException(409, "username or email already exists") from exc
    audit(
        request.app.state.db,
        "admin.user_created",
        request=request,
        user_id=user["id"],
        target_type="user",
        target_id=cursor.lastrowid,
    )
    result = {"id": cursor.lastrowid, "initial_password": password}
    if wants_json(request):
        return result
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin_initial_password.html",
        context={"user": user, **result},
    )


@router.post("/users/{user_id}")
async def update_user(user_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    try:
        username = validate_username(data.get("username"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    role = data.get("role", "user")
    active = flag(data.get("is_active", True))
    if role not in ("admin", "user"):
        raise HTTPException(400, "invalid role")
    if _last_admin(request.app.state.db, user_id) and (role != "admin" or not active):
        raise HTTPException(400, "cannot disable the last admin")
    cursor = request.app.state.db.execute(
        "UPDATE users SET username=?,email=?,role=?,is_active=?,updated_at=? WHERE id=?",
        (username, str(data.get("email", "")).strip() or None, role, active, now_iso(), user_id),
    )
    if cursor.rowcount != 1:
        raise HTTPException(404)
    if not active:
        request.app.state.db.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now_iso(), user_id),
        )
    audit(
        request.app.state.db,
        "admin.user_updated",
        request=request,
        user_id=user["id"],
        target_type="user",
        target_id=user_id,
    )
    return {"ok": True}


@router.post("/users/{user_id}/password")
async def reset_password(user_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    try:
        password = validate_password(data.get("password") or new_token(12))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with request.app.state.db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash=?,failed_logins=0,locked_until=NULL,"
            "must_change_password=1,updated_at=? WHERE id=?",
            (hash_password(password), now_iso(), user_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(404)
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now_iso(), user_id),
        )
    audit(
        request.app.state.db,
        "admin.password_reset",
        request=request,
        user_id=user["id"],
        target_type="user",
        target_id=user_id,
    )
    result = {"ok": True, "initial_password": password}
    if wants_json(request):
        return result
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin_initial_password.html",
        context={"user": user, **result},
    )


@router.post("/users/{user_id}/totp/reset")
async def reset_user_totp(user_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    with request.app.state.db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE users SET totp_secret=NULL,updated_at=? WHERE id=?",
            (now_iso(), user_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(404)
        conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now_iso(), user_id),
        )
    audit(
        request.app.state.db,
        "admin.totp_reset",
        request=request,
        user_id=user["id"],
        target_type="user",
        target_id=user_id,
    )
    return (
        {"ok": True} if wants_json(request) else RedirectResponse("/admin/users", status_code=303)
    )


@router.post("/users/{user_id}/status")
async def set_user_status(user_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    active = flag(data.get("is_active", False))
    if _last_admin(request.app.state.db, user_id) and not active:
        raise HTTPException(400, "cannot disable the last admin")
    cursor = request.app.state.db.execute(
        "UPDATE users SET is_active=?,updated_at=? WHERE id=?",
        (active, now_iso(), user_id),
    )
    if cursor.rowcount != 1:
        raise HTTPException(404)
    if not active:
        request.app.state.db.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now_iso(), user_id),
        )
    audit(
        request.app.state.db,
        "admin.user_updated",
        request=request,
        user_id=user["id"],
        target_type="user",
        target_id=user_id,
    )
    if wants_json(request):
        return {"ok": True}
    return RedirectResponse("/admin/users", 303)


@router.delete("/users/{user_id}")
@router.post("/users/{user_id}/delete")
async def delete_user(user_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    if user_id == user["id"] or _last_admin(request.app.state.db, user_id):
        raise HTTPException(400, "cannot delete this admin")
    cursor = request.app.state.db.execute("DELETE FROM users WHERE id=?", (user_id,))
    if cursor.rowcount != 1:
        raise HTTPException(404)
    audit(
        request.app.state.db,
        "admin.user_deleted",
        request=request,
        user_id=user["id"],
        target_type="user",
        target_id=user_id,
    )
    return {"ok": True}


def _site_data(data: dict) -> SiteConfig:
    try:
        if "candidate" in data:
            candidate = data["candidate"]
            if isinstance(candidate, str):
                candidate = json.loads(candidate)
            if not isinstance(candidate, dict):
                raise ValueError
            data = {
                **candidate,
                **{
                    key: data[key]
                    for key in (
                        "slug",
                        "name",
                        "base_url",
                        "tagset_group",
                        "enabled",
                        "crawl_interval_sec",
                        "crawl_interval_minutes",
                        "render_js",
                        "discovery_mode",
                        "config",
                        "url_canonical",
                        "extract",
                        "attachments",
                        "politeness",
                        "render",
                        "item_selector",
                        "link_selector",
                        "title_selector",
                        "published_at_selector",
                        "content_selector",
                    )
                    if key in data
                },
            }
        config = data.get("config", {})
        if isinstance(config, str):
            config = json.loads(config)
        config = dict(config)
        fields = dict(config.get("fields", {}))
        for form_key, field_key in (
            ("link_selector", "url"),
            ("title_selector", "title"),
            ("published_at_selector", "published_at"),
        ):
            if value := str(data.get(form_key, "")).strip():
                fields[field_key] = value
        if fields:
            config["fields"] = fields
        if value := str(data.get("item_selector", "")).strip():
            config["item_selector"] = value
        extract = data.get("extract", {})
        if isinstance(extract, str):
            extract = json.loads(extract)
        extract = dict(extract)
        extract_selectors = dict(extract.get("selectors", {}))
        if value := str(data.get("content_selector", "")).strip():
            extract_selectors["content"] = value
        if extract_selectors:
            extract["selectors"] = extract_selectors
        document = {
            "slug": data.get("slug"),
            "name": data.get("name"),
            "base_url": data.get("base_url"),
            "tagset_group": data.get("tagset_group"),
            "enabled": bool(flag(data.get("enabled", False))),
            "crawl_interval_sec": (
                integer(data["crawl_interval_minutes"], "crawl interval") * 60
                if "crawl_interval_minutes" in data
                else integer(data.get("crawl_interval_sec", 1800), "crawl_interval_sec")
            ),
            "render_js": bool(flag(data.get("render_js", False))),
            "discovery_mode": data.get("discovery_mode"),
            "config": config,
        }
        for key in ("url_canonical", "attachments", "politeness", "render"):
            if key in data:
                document[key] = data[key]
        if extract:
            document["extract"] = extract
        return SiteConfig.model_validate(document)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "invalid site configuration") from exc


@router.get("/sites")
async def sites(request: Request, user: AdminUser):
    rows = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT s.id,s.slug,s.name,s.base_url,s.discovery_mode,g.slug AS tagset_group,s.enabled,"
            "s.crawl_interval_sec,s.last_crawled_at,s.last_error,s.consecutive_failures,"
            "(SELECT COUNT(*) FROM articles a WHERE a.site_id=s.id AND a.status='FAILED') "
            "AS article_failures,"
            "(SELECT a.last_error FROM articles a WHERE a.site_id=s.id AND a.status='FAILED' "
            "ORDER BY a.id DESC LIMIT 1) AS article_last_error FROM sites s "
            "JOIN tagset_groups g ON g.id=s.tagset_group_id ORDER BY s.id"
        )
    ]
    for row in rows:
        row["crawl_task"] = request.app.state.crawl_tasks.get(row["id"])
        row["backfill_max_pages"] = None
        try:
            stored = get_site(request.app.state.db, row["slug"])
            pagination = getattr(stored.config.discovery, "pagination", None) if stored else None
            if pagination is not None and pagination.mode != "none":
                row["backfill_max_pages"] = min(pagination.max_page or 500, 500)
                row["incremental_pages"] = pagination.max_pages
        except Exception as exc:
            log.warning("site_backfill_unavailable", site=row["slug"], error=safe_error(exc))
    if wants_json(request):
        return rows
    groups = [
        dict(row)
        for row in request.app.state.db.query("SELECT slug,name FROM tagset_groups ORDER BY name")
    ]
    response = _page(
        request,
        "Sites",
        rows,
        user,
        template="admin_sites.html",
        groups=groups,
        onboarding=False,
    )
    if any(row["crawl_task"] and row["crawl_task"]["status"] in {"queued", "running"} for row in rows):
        response.headers["Refresh"] = "2; url=/admin/sites"
    return response


@router.get("/sites/new")
async def new_site_page(request: Request, user: AdminUser):
    groups = [
        dict(row)
        for row in request.app.state.db.query("SELECT slug,name FROM tagset_groups ORDER BY name")
    ]
    return _page(
        request,
        "Add site",
        [],
        user,
        template="admin_sites.html",
        groups=groups,
        onboarding=True,
    )


@router.post("/sites")
async def create_site(request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    site = _site_data(data)
    group = request.app.state.db.query_one(
        "SELECT id FROM tagset_groups WHERE slug=?", (site.tagset_group,)
    )
    if group is None:
        raise HTTPException(400, "unknown tagset group")
    timestamp = now_iso()
    try:
        cursor = request.app.state.db.execute(
            "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,enabled,"
            "crawl_interval_sec,render_js,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'wizard',?,?)",
            (
                site.slug,
                site.name,
                site.base_url,
                site.discovery_mode,
                group["id"],
                json.dumps(site.model_dump(mode="json"), separators=(",", ":")),
                int(site.enabled),
                site.crawl_interval_sec,
                int(site.render_js),
                timestamp,
                timestamp,
            ),
        )
    except Exception as exc:
        raise HTTPException(409, "site slug already exists") from exc
    audit(
        request.app.state.db,
        "admin.site_created",
        request=request,
        user_id=user["id"],
        target_type="site",
        target_id=cursor.lastrowid,
    )
    return {"id": cursor.lastrowid}


@router.post("/sites/{site_id:int}")
async def update_site(site_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    site = _site_data(data)
    group = request.app.state.db.query_one(
        "SELECT id FROM tagset_groups WHERE slug=?", (site.tagset_group,)
    )
    if group is None:
        raise HTTPException(400, "unknown tagset group")
    with request.app.state.db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE sites SET slug=?,name=?,base_url=?,discovery_mode=?,tagset_group_id=?,"
            "config_json=?,enabled=?,crawl_interval_sec=?,render_js=?,updated_at=? WHERE id=?",
            (
                site.slug,
                site.name,
                site.base_url,
                site.discovery_mode,
                group["id"],
                json.dumps(site.model_dump(mode="json"), separators=(",", ":")),
                int(site.enabled),
                site.crawl_interval_sec,
                int(site.render_js),
                now_iso(),
                site_id,
            ),
        )
        if cursor.rowcount != 1:
            raise HTTPException(404)
        conn.execute(
            "UPDATE articles SET status='DISCOVERED',attempts=0,next_attempt_at=NULL,"
            "last_error=NULL WHERE site_id=? AND status='FAILED'",
            (site_id,),
        )
    audit(
        request.app.state.db,
        "admin.site_updated",
        request=request,
        user_id=user["id"],
        target_type="site",
        target_id=site_id,
    )
    return {"ok": True}


@router.delete("/sites/{site_id:int}")
@router.post("/sites/{site_id:int}/delete")
async def delete_site(site_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    paths = [
        row["local_path"]
        for row in request.app.state.db.query(
            "SELECT x.local_path FROM attachments x JOIN articles a ON a.id=x.article_id "
            "WHERE a.site_id=? AND x.local_path IS NOT NULL",
            (site_id,),
        )
    ]
    cursor = request.app.state.db.execute("DELETE FROM sites WHERE id=?", (site_id,))
    if cursor.rowcount != 1:
        raise HTTPException(404)
    root = request.app.state.settings.storage.attachment_dir.resolve()
    remaining = {
        resolved
        for row in request.app.state.db.query(
            "SELECT local_path FROM attachments WHERE local_path IS NOT NULL"
        )
        if (resolved := attachment_path(row["local_path"], root)) is not None
    }
    for value in set(paths):
        path = attachment_path(value, root, require_file=True)
        if path is not None and path not in remaining:
            path.unlink(missing_ok=True)
    audit(
        request.app.state.db,
        "admin.site_deleted",
        request=request,
        user_id=user["id"],
        target_type="site",
        target_id=site_id,
    )
    return {"ok": True}


@router.post("/sites/{site_id:int}/status")
async def set_site_status(site_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    cursor = request.app.state.db.execute(
        "UPDATE sites SET enabled=?,updated_at=? WHERE id=?",
        (flag(data.get("enabled", False)), now_iso(), site_id),
    )
    if cursor.rowcount != 1:
        raise HTTPException(404)
    audit(
        request.app.state.db,
        "admin.site_status",
        request=request,
        user_id=user["id"],
        target_type="site",
        target_id=site_id,
    )
    if wants_json(request):
        return {"ok": True}
    return RedirectResponse("/admin/sites", 303)


@router.post("/sites/{site_id:int}/crawl-interval")
async def set_site_crawl_interval(site_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    seconds = integer(data.get("crawl_interval_minutes"), "crawl interval") * 60
    cursor = request.app.state.db.execute(
        "UPDATE sites SET crawl_interval_sec=?,updated_at=? WHERE id=?",
        (seconds, now_iso(), site_id),
    )
    if cursor.rowcount != 1:
        raise HTTPException(404)
    audit(
        request.app.state.db,
        "admin.site_crawl_interval",
        request=request,
        user_id=user["id"],
        target_type="site",
        target_id=site_id,
        detail=f"{seconds}s",
    )
    if wants_json(request):
        return {"ok": True, "crawl_interval_sec": seconds}
    return RedirectResponse("/admin/sites", 303)


async def _crawl_site_now(app, site_id: int, slug: str, pages: int | None = None) -> None:
    task = app.state.crawl_tasks[site_id]
    task["status"] = "running"
    try:
        stored = get_site(app.state.db, slug)
        if stored is None:
            raise RuntimeError("site no longer exists")
        if pages is not None:
            pagination = getattr(stored.config.discovery, "pagination", None)
            if pagination is None or pagination.mode == "none":
                raise ValueError("site discovery has no pagination")
            pagination.max_pages = pages
            stored.config.politeness.conditional_requests = False
        stats = await crawl_site(app.state.settings, app.state.db, stored)
        task.update(status="done", result=dataclasses.asdict(stats))
    except Exception as exc:
        task.update(status="failed", error=safe_error(exc))
        log.warning("manual_crawl_failed", site=slug, error=safe_error(exc), exc_info=True)
    finally:
        task["finished_at"] = now_iso()


@router.post("/sites/{site_id:int}/crawl")
async def crawl_site_now(
    site_id: int,
    background: BackgroundTasks,
    request: Request,
    user: AdminUser,
):
    data = await request_data(request)
    write_guard(request, data, user)
    row = request.app.state.db.query_one("SELECT slug,enabled FROM sites WHERE id=?", (site_id,))
    if row is None:
        raise HTTPException(404)
    if not row["enabled"]:
        raise HTTPException(409, "site is disabled")
    pages = integer(data["pages"], "pages") if str(data.get("pages", "")).strip() else None
    if pages is not None:
        if pages > 500:
            raise HTTPException(400, "pages must be between 1 and 500")
        stored = get_site(request.app.state.db, row["slug"])
        pagination = getattr(stored.config.discovery, "pagination", None) if stored else None
        if pagination is None or pagination.mode == "none":
            raise HTTPException(409, "site discovery has no pagination")
        if pagination.max_page and pages > pagination.max_page:
            raise HTTPException(400, f"site has at most {pagination.max_page} pages")
    current = request.app.state.crawl_tasks.get(site_id)
    if current and current["status"] in {"queued", "running"}:
        if wants_json(request):
            raise HTTPException(409, "crawl already running")
        return RedirectResponse("/admin/sites", 303)
    request.app.state.limiter.check("site-crawl", str(user["id"]), 3, 60)
    task = {
        "status": "queued",
        "kind": "backfill" if pages is not None else "crawl",
        "created_at": now_iso(),
    }
    if pages is not None:
        task["pages"] = pages
    request.app.state.crawl_tasks[site_id] = task
    background.add_task(_crawl_site_now, request.app, site_id, row["slug"], pages)
    audit(
        request.app.state.db,
        "admin.site_backfill" if pages is not None else "admin.site_crawl",
        request=request,
        user_id=user["id"],
        target_type="site",
        target_id=site_id,
        detail=f"pages={pages}" if pages is not None else None,
    )
    if wants_json(request):
        return {"queued": True, "status": "queued", "kind": task["kind"]}
    return RedirectResponse("/admin/sites", 303)


def _prune_tasks(app) -> None:
    cutoff = time.time() - 15 * 60
    app.state.probe_tasks = {
        key: value
        for key, value in app.state.probe_tasks.items()
        if value.get("created_at", 0) >= cutoff
    }


def _run_probe(app, task_id: str, url: str, user_id: int) -> None:
    task = app.state.probe_tasks[task_id]
    task["status"] = "running"
    configured = app.state.settings.onboarding.probe
    try:
        task.update(
            status="done",
            result=dataclasses.asdict(
                probe_site(
                    url,
                    limits=ProbeLimits(
                        max_pages=configured.max_pages,
                        max_bytes_per_page=configured.max_bytes_per_page,
                        max_duration_sec=configured.max_duration_sec,
                        sample_articles=configured.sample_articles,
                        delay_sec=configured.delay_sec,
                    ),
                )
            ),
        )
    except Exception as exc:
        task.update(status="failed", error=safe_error(exc))
        log.warning("site_probe_failed", error=safe_error(exc), exc_info=True)
    task["user_id"] = user_id


def _config_hash(site: SiteConfig) -> str:
    document = site.model_copy(update={"enabled": False}).model_dump(mode="json")
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _run_dryrun(app, task_id: str, site: SiteConfig, user_id: int) -> None:
    task = app.state.probe_tasks[task_id]
    task["status"] = "running"
    probe = app.state.settings.onboarding.probe
    fetcher = (
        Renderer(app.state.settings.politeness, site.render)
        if site.render_js
        else Fetcher(
            app.state.settings.politeness,
            max_concurrency=site.politeness.max_concurrency,
            delay_sec=site.politeness.delay_sec,
            conditional_requests=False,
            max_bytes=probe.max_bytes_per_page,
        )
    )
    try:
        report = await preview_site(
            site.model_copy(update={"enabled": True}),
            fetcher=fetcher,
            limits=DryRunLimits(
                sample_size=app.state.settings.onboarding.dryrun.sample_size,
                max_pages=probe.max_pages,
                max_duration_sec=probe.max_duration_sec,
            ),
        )
        task.update(status="done", result=dataclasses.asdict(report))
    except Exception as exc:
        task.update(status="failed", error=safe_error(exc))
        log.warning("site_dryrun_failed", error=safe_error(exc), exc_info=True)
    finally:
        await fetcher.close()
    task["user_id"] = user_id


def _validate_probe_url(url: str) -> None:
    if not 1 <= len(url) <= 4096:
        raise HTTPException(400, "invalid probe URL")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return
        resolve_url(url)
    except Exception as exc:
        raise HTTPException(400, "unsafe probe URL") from exc


@router.post("/sites/probe")
async def probe(background: BackgroundTasks, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    request.app.state.limiter.check("site-probe", str(user["id"]), 3, 60)
    url = str(data.get("url", ""))
    _validate_probe_url(url)
    _prune_tasks(request.app)
    task_id = new_token(12)
    request.app.state.probe_tasks[task_id] = {
        "status": "queued",
        "user_id": user["id"],
        "created_at": time.time(),
    }
    background.add_task(_run_probe, request.app, task_id, url, user["id"])
    audit(request.app.state.db, "admin.site_probe", request=request, user_id=user["id"])
    if wants_json(request):
        return {"task_id": task_id}
    return RedirectResponse(f"/admin/sites/picker?task_id={task_id}", 303)


def _site_task(request: Request, task_id: str, user: dict) -> dict:
    _prune_tasks(request.app)
    task = request.app.state.probe_tasks.get(task_id)
    if task is None or task.get("user_id") != user["id"]:
        raise HTTPException(404)
    return task


@router.get("/sites/task")
async def site_task(task_id: str, request: Request, user: AdminUser):
    """Browser-friendly task output with bounded, scrubbed failure details."""
    task = _site_task(request, task_id, user)
    response = {"status": task.get("status")}
    if task.get("status") == "failed":
        response["error"] = task.get("error", "task failed")
    if task.get("status") != "done":
        return response
    if task.get("kind") == "dryrun":
        response["result"] = task.get("result", {})
        response["config_hash"] = task.get("config_hash")
        return response
    findings = task.get("result", {}).get("findings", [])
    candidate = next(
        (item.get("value") for item in findings if item.get("key") == "config_candidate"),
        None,
    )
    if candidate is not None:
        response["candidate"] = candidate
    return response


@router.get("/sites/probe/{task_id}")
async def probe_result(task_id: str, request: Request, user: AdminUser):
    return _site_task(request, task_id, user)


@router.post("/sites/dryrun")
async def dryrun(background: BackgroundTasks, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    request.app.state.limiter.check("site-dryrun", str(user["id"]), 3, 60)
    site = _site_data(data)
    task_id = new_token(12)
    digest = _config_hash(site)
    _prune_tasks(request.app)
    request.app.state.probe_tasks[task_id] = {
        "status": "queued",
        "user_id": user["id"],
        "created_at": time.time(),
        "config_hash": digest,
        "config": site.model_dump(mode="json"),
        "kind": "dryrun",
    }
    background.add_task(_run_dryrun, request.app, task_id, site, user["id"])
    if wants_json(request):
        return {"task_id": task_id, "config_hash": digest}
    return RedirectResponse(f"/admin/sites/picker?task_id={task_id}", 303)


@router.post("/sites/confirm")
async def confirm_site(request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    _prune_tasks(request.app)
    task = request.app.state.probe_tasks.get(str(data.get("task_id", "")))
    site = _site_data(data)
    digest = _config_hash(site)
    if (
        task is None
        or task.get("user_id") != user["id"]
        or task.get("kind") != "dryrun"
        or task.get("status") != "done"
        or task.get("config_hash") != digest
        or data.get("config_hash") != digest
    ):
        raise HTTPException(409, "completed dry-run required")
    created = await create_site(request, user)
    disabled = site.model_copy(update={"enabled": False})
    request.app.state.db.execute(
        "UPDATE sites SET enabled=0,config_json=?,updated_at=? WHERE id=?",
        (
            json.dumps(disabled.model_dump(mode="json"), separators=(",", ":")),
            now_iso(),
            created["id"],
        ),
    )
    if wants_json(request):
        return created
    return RedirectResponse("/admin/sites", 303)


@router.get("/sites/picker")
async def picker(request: Request, user: AdminUser):
    summary = {"Status": "No task selected"}
    task_id = request.query_params.get("task_id", "")
    task = _site_task(request, task_id, user) if task_id else None
    selectors: list[dict] = []
    candidate: dict | None = None
    dryrun_done = False
    if task:
        summary["Status"] = str(task.get("status", "unknown"))
        if task.get("status") == "failed":
            summary["Error"] = str(task.get("error", "probe failed"))
        if task.get("status") == "done" and task.get("kind") == "dryrun":
            dryrun_done = True
            candidate = task.get("config")
            result = task.get("result", {})
            for key in ("discovered", "succeeded", "failed", "duration_ms"):
                if isinstance(result.get(key), int):
                    summary[key.replace("_", " ").title()] = str(result[key])
        elif task.get("status") == "done":
            findings = {
                finding.get("key"): finding
                for finding in task.get("result", {}).get("findings", [])
            }
            selectors = list(findings.get("item_selector", {}).get("candidates", []))
            value = findings.get("config_candidate", {}).get("value")
            if isinstance(value, dict):
                candidate = json.loads(json.dumps(value))
                try:
                    selected = int(request.query_params.get("selector", "0"))
                    selector = selectors[selected] if selected >= 0 else None
                except (ValueError, IndexError):
                    selector = None
                if selector and candidate.get("discovery_mode") == "html_list":
                    candidate["config"]["item_selector"] = selector["item_selector"]
                    fields = {
                        "url": selector["link_selector"],
                        "title": selector["title_selector"],
                    }
                    if selector.get("published_at_selector"):
                        fields["published_at"] = selector["published_at_selector"]
                    candidate["config"]["fields"] = fields
    preview = "<!doctype html><html lang='en'><meta charset='utf-8'><title>Preview</title><h1>Preview</h1><dl>"
    preview += "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
        for label, value in summary.items()
    )
    preview += "</dl>"
    if task and task.get("status") == "done" and task.get("kind") == "dryrun":
        for item in task.get("result", {}).get("items", []):
            title = html.escape(str(item.get("title") or item.get("url") or "Article"))
            sample = html.escape(str(item.get("summary") or item.get("error") or ""))
            preview += f"<article><h2>{title}</h2><p>{sample}</p></article>"
    preview += "</html>"
    groups = request.app.state.db.query("SELECT slug,name FROM tagset_groups ORDER BY name")
    response_headers = {
        "Content-Security-Policy": "default-src 'none'; frame-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'"
    }
    if task and task.get("status") in {"queued", "running"}:
        response_headers["Refresh"] = f"2; url=/admin/sites/picker?task_id={task_id}"
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="picker.html",
        context={
            "title": "Site picker",
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
            "preview": preview,
            "task_id": task_id,
            "selectors": selectors,
            "candidate": json.dumps(candidate, ensure_ascii=False, indent=2) if candidate else "",
            "candidate_data": candidate or {},
            "dryrun_done": dryrun_done,
            "config_hash": task.get("config_hash", "") if task else "",
            "summary_status": summary["Status"],
            "task_error": summary.get("Error"),
            "groups": groups,
        },
        headers=response_headers,
    )


async def _run_tagset_build(app, task_id: str, options: BootstrapOptions) -> None:
    task = app.state.tagset_tasks[task_id]
    task["status"] = "running"
    try:
        timeout = httpx.Timeout(app.state.settings.tagger.llm.request_timeout_sec)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            providers = runtime_providers(
                app.state.settings.tagger.llm.providers,
                app.state.db,
                Crypto(app.state.settings.secret_key),
            )
            inducer = NativeLLMInducer(providers, client)
            result = await bootstrap_tagset(
                app.state.db,
                app.state.settings.tagger.tagset_dir,
                options,
                inducer=inducer,
            )
        task.update(
            status="done",
            frozen=result.frozen,
            report=str(result.report_path),
            tagset=str(result.tagset_path) if result.tagset_path else None,
        )
    except Exception as exc:
        task.update(status="failed", error=safe_error(exc))


@router.post("/tagset/build")
async def build_tagset(background: BackgroundTasks, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    request.app.state.limiter.check("tagset-build", str(user["id"]), 1, 300)
    slug = str(data.get("group", ""))
    row = request.app.state.db.query_one(
        "SELECT slug,name,build_mode FROM tagset_groups WHERE slug=?", (slug,)
    )
    if row is None:
        raise HTTPException(404)
    configured = request.app.state.settings.group(slug)
    mode = str(data.get("mode") or row["build_mode"] or "llm")
    if mode not in {"llm", "embedding"}:
        raise HTTPException(400, "invalid build mode")
    options = BootstrapOptions(
        group=slug,
        group_name=row["name"],
        mode=mode,
        batch_size=request.app.state.settings.tagger.tagset.batch_size,
        max_tags=request.app.state.settings.tagger.tagset.auto_curate.max_tags,
        min_cluster_docs=request.app.state.settings.tagger.tagset.auto_curate.min_cluster_docs,
        min_documents=configured.min_docs_for_build if configured else 200,
        require_manual_review=(
            flag(data["require_manual_review"])
            if "require_manual_review" in data
            else (configured.require_manual_review if configured else False)
        ),
    )
    cutoff = time.time() - 24 * 60 * 60
    request.app.state.tagset_tasks = {
        key: task
        for key, task in request.app.state.tagset_tasks.items()
        if task.get("created_at", 0) >= cutoff
    }
    if any(
        task.get("group") == slug and task.get("status") in {"queued", "running"}
        for task in request.app.state.tagset_tasks.values()
    ):
        raise HTTPException(409, "tagset build already running")
    task_id = new_token(12)
    request.app.state.tagset_tasks[task_id] = {
        "status": "queued",
        "group": slug,
        "user_id": user["id"],
        "created_at": time.time(),
    }
    background.add_task(_run_tagset_build, request.app, task_id, options)
    audit(request.app.state.db, "admin.tagset_build", request=request, user_id=user["id"])
    if wants_json(request):
        return {"task_id": task_id}
    return RedirectResponse(f"/admin/tagset/build/{task_id}?view=html", 303)


@router.get("/tagset/build/{task_id}")
async def tagset_build_result(task_id: str, request: Request, user: AdminUser):
    task = request.app.state.tagset_tasks.get(task_id)
    if task is None or task.get("user_id") != user["id"]:
        raise HTTPException(404)
    result = {key: task[key] for key in ("status", "group", "frozen", "error") if key in task}
    if request.query_params.get("view") != "html":
        return result
    response = _page(request, "Tagset build", [result], user)
    if task.get("status") in {"queued", "running"}:
        response.headers["Refresh"] = f"2; url=/admin/tagset/build/{task_id}?view=html"
    return response


def _tagset_group_path(request: Request, group: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", group):
        raise HTTPException(404)
    return request.app.state.settings.tagger.tagset_dir / group


@router.get("/tagset/{group}/report")
async def tagset_report(group: str, request: Request, _user: AdminUser):
    path = _tagset_group_path(request, group) / "tagset_report.md"
    if not path.is_file():
        raise HTTPException(404)
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@router.post("/tagset/{group}/freeze")
async def freeze_tagset_draft(group: str, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    if not flag(data.get("confirm", False)):
        raise HTTPException(400, "explicit confirmation required")
    path = _tagset_group_path(request, group) / "tags.draft.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(404, "draft not found") from exc
    frozen = freeze_tagset(
        document,
        request.app.state.settings.tagset_path(group),
        db=request.app.state.db,
    )
    audit(request.app.state.db, "admin.tagset_frozen", request=request, user_id=user["id"])
    if wants_json(request):
        return {"checksum": frozen["checksum"]}
    return RedirectResponse("/admin/tagset", 303)


@router.post("/tagset/groups")
async def create_tagset_group(request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    slug = str(data.get("slug", "")).strip()
    name = str(data.get("name", "")).strip()
    mode = str(data.get("build_mode", "llm"))
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug)
        or not 1 <= len(name) <= 100
        or mode not in {"llm", "embedding"}
    ):
        raise HTTPException(400, "invalid tagset group")
    try:
        cursor = request.app.state.db.execute(
            "INSERT INTO tagset_groups (slug,name,description,build_mode,status,created_at) "
            "VALUES (?,?,?,?, 'draft',?)",
            (
                slug,
                name,
                str(data.get("description", "")).strip() or None,
                mode,
                now_iso(),
            ),
        )
    except Exception as exc:
        raise HTTPException(409, "tagset group already exists") from exc
    audit(
        request.app.state.db,
        "admin.tagset_group_created",
        request=request,
        user_id=user["id"],
        target_type="tagset_group",
        target_id=cursor.lastrowid,
    )
    if wants_json(request):
        return {"id": cursor.lastrowid}
    return RedirectResponse("/admin/tagset", 303)


@router.get("/tagset")
@router.get("/tagset/groups")
async def tagsets(request: Request, user: AdminUser):
    rows = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT g.id,g.slug,g.name,g.description,g.tagset_version,g.build_mode,g.status,g.frozen_at,"
            "COUNT(t.id) AS tags FROM tagset_groups g LEFT JOIN tags t ON t.group_id=g.id "
            "AND t.tagset_version=g.tagset_version GROUP BY g.id ORDER BY g.id"
        )
    ]
    return (
        rows
        if wants_json(request)
        else _page(request, "Tagsets", rows, user, template="admin_tagsets.html")
    )


@router.get("/providers")
async def providers(request: Request, user: AdminUser):
    health = {}
    for row in request.app.state.db.query(
        "SELECT provider,consecutive_failures,cooldown_until,last_error,total_calls,total_failures "
        "FROM provider_health"
    ):
        item = dict(row)
        health[item.pop("provider")] = item
    empty_health = {
        "consecutive_failures": 0,
        "cooldown_until": None,
        "last_error": None,
        "total_calls": 0,
        "total_failures": 0,
    }
    stored = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT id,name,type,base_url,models_json,max_input_chars,created_at "
            "FROM llm_providers ORDER BY id"
        )
    ]
    stored_names = {item["name"] for item in stored}
    rows = [
        {
            "id": None,
            "name": provider.name,
            "type": provider.type,
            "base_url": provider.base_url,
            "models": provider.models,
            "max_input_chars": provider.max_input_chars,
            "created_at": None,
            "source": "configuration",
            "api_key_env": provider.api_key_env,
            "api_key_configured": bool(provider.api_key),
            **empty_health,
            **health.get(provider.name, {}),
        }
        for provider in request.app.state.settings.tagger.llm.providers
        if provider.name not in stored_names
    ]
    for item in stored:
        item["models"] = json.loads(item.pop("models_json"))
        item.update(
            {
                "source": "web",
                "api_key_configured": True,
                **empty_health,
                **health.get(item["name"], {}),
            }
        )
        rows.append(item)
    if wants_json(request):
        return rows

    summary = dict(
        request.app.state.db.query_one(
            "SELECT enabled,provider,model FROM ai_summary_settings WHERE id=1"
        )
    )
    summary_backends = [
        {
            "provider": provider.name,
            "model": model,
            "value": f"{provider.name}|{model}",
        }
        for provider in runtime_providers(
            request.app.state.settings.tagger.llm.providers,
            request.app.state.db,
            request.app.state.crypto,
        )
        if provider.api_key
        for model in provider.models
    ]
    selected = (
        f"{summary['provider']}|{summary['model']}"
        if summary["provider"] and summary["model"]
        else ""
    )
    return _page(
        request,
        "Providers",
        rows,
        user,
        template="admin_providers.html",
        summary=summary,
        summary_backends=summary_backends,
        summary_selected=selected,
    )


@router.post("/providers/summarization")
async def update_summarization(request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    enabled = flag(data.get("enabled"))
    backend = str(data.get("backend", "")).strip()
    provider_name, separator, model = backend.partition("|")
    available = runtime_providers(
        request.app.state.settings.tagger.llm.providers,
        request.app.state.db,
        request.app.state.crypto,
    )
    selected = next(
        (
            provider
            for provider in available
            if provider.name == provider_name and model in provider.models and provider.api_key
        ),
        None,
    )
    if (enabled or backend) and (not separator or selected is None):
        raise HTTPException(400, "invalid summary provider/model")
    request.app.state.db.execute(
        "UPDATE ai_summary_settings SET enabled=?,provider=?,model=?,updated_at=? WHERE id=1",
        (
            enabled,
            provider_name if selected else None,
            model if selected else None,
            now_iso(),
        ),
    )
    audit(
        request.app.state.db,
        "admin.ai_summary_updated",
        request=request,
        user_id=user["id"],
        target_type="ai_summary_settings",
        target_id=1,
        detail=json.dumps(
            {"enabled": bool(enabled), "provider": provider_name, "model": model}
        ),
    )
    return (
        {"ok": True, "enabled": bool(enabled), "provider": provider_name, "model": model}
        if wants_json(request)
        else RedirectResponse("/admin/providers", 303)
    )


def _provider_config(data: dict, key: str | None = None) -> ProviderConfig:
    models = [part.strip() for part in str(data.get("models", "")).split(",") if part.strip()]
    if not models or any(len(model) > 256 for model in models):
        raise HTTPException(400, "invalid models")
    try:
        return ProviderConfig(
            name=str(data.get("name", "")).strip(),
            type=str(data.get("type", "")),
            base_url=str(data.get("base_url", "")).strip() or None,
            models=models,
            max_input_chars=data.get("max_input_chars", 8000),
            api_key_value=key,
        )
    except ValidationError as exc:
        raise HTTPException(400, "invalid provider configuration") from exc


@router.post("/providers")
async def create_provider(request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    key = data.get("api_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(ord(char) < 32 for char in key):
        raise HTTPException(400, "invalid API key")
    provider = _provider_config(data, key)
    try:
        with request.app.state.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO llm_providers "
                "(name,type,base_url,models_json,max_input_chars,api_key_enc,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    provider.name,
                    provider.type,
                    provider.base_url,
                    json.dumps(provider.models),
                    provider.max_input_chars,
                    request.app.state.crypto.encrypt(key),
                    now_iso(),
                    now_iso(),
                ),
            )
            summary = conn.execute(
                "SELECT model FROM ai_summary_settings WHERE id=1 AND enabled=1 AND provider=?",
                (provider.name,),
            ).fetchone()
            if summary and summary["model"] not in provider.models:
                conn.execute(
                    "UPDATE ai_summary_settings SET enabled=0,updated_at=? WHERE id=1",
                    (now_iso(),),
                )
    except Exception as exc:
        raise HTTPException(409, "provider name already exists") from exc
    audit(
        request.app.state.db,
        "admin.provider_created",
        request=request,
        user_id=user["id"],
        target_type="provider",
        target_id=cursor.lastrowid,
    )
    return (
        {"id": cursor.lastrowid, "name": provider.name}
        if wants_json(request)
        else RedirectResponse("/admin/providers", 303)
    )


@router.get("/providers/{provider_id:int}/edit")
async def edit_provider(provider_id: int, request: Request, user: AdminUser):
    row = request.app.state.db.query_one(
        "SELECT id,name,type,base_url,models_json,max_input_chars FROM llm_providers WHERE id=?",
        (provider_id,),
    )
    if row is None:
        raise HTTPException(404)
    provider = dict(row)
    provider["models"] = ", ".join(json.loads(provider.pop("models_json")))
    return _page(
        request,
        "Edit provider",
        [provider],
        user,
        template="admin_provider_edit.html",
        provider=provider,
    )


@router.post("/providers/{provider_id:int}")
async def update_provider(provider_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    existing = request.app.state.db.query_one(
        "SELECT name,api_key_enc FROM llm_providers WHERE id=?", (provider_id,)
    )
    if existing is None:
        raise HTTPException(404)
    key = data.get("api_key")
    if key is None or key == "":
        key = None
        encrypted_key = existing["api_key_enc"]
    elif not isinstance(key, str) or len(key) > 4096 or any(ord(char) < 32 for char in key):
        raise HTTPException(400, "invalid API key")
    else:
        encrypted_key = request.app.state.crypto.encrypt(key)
    provider = _provider_config(data, key)
    try:
        with request.app.state.db.transaction() as conn:
            conn.execute(
                "UPDATE llm_providers SET name=?,type=?,base_url=?,models_json=?,max_input_chars=?,"
                "api_key_enc=?,updated_at=? WHERE id=?",
                (
                    provider.name,
                    provider.type,
                    provider.base_url,
                    json.dumps(provider.models),
                    provider.max_input_chars,
                    encrypted_key,
                    now_iso(),
                    provider_id,
                ),
            )
            summary = conn.execute(
                "SELECT enabled,model FROM ai_summary_settings WHERE id=1 AND provider=?",
                (existing["name"],),
            ).fetchone()
            if summary:
                conn.execute(
                    "UPDATE ai_summary_settings SET provider=?,enabled=?,updated_at=? WHERE id=1",
                    (
                        provider.name,
                        int(bool(summary["enabled"]) and summary["model"] in provider.models),
                        now_iso(),
                    ),
                )
    except Exception as exc:
        raise HTTPException(409, "provider name already exists") from exc
    audit(
        request.app.state.db,
        "admin.provider_updated",
        request=request,
        user_id=user["id"],
        target_type="provider",
        target_id=provider_id,
    )
    return (
        {"ok": True, "name": provider.name}
        if wants_json(request)
        else RedirectResponse("/admin/providers", 303)
    )


@router.delete("/providers/{provider_id:int}")
@router.post("/providers/{provider_id:int}/delete")
async def delete_provider(provider_id: int, request: Request, user: AdminUser):
    data = await request_data(request)
    write_guard(request, data, user)
    if request.method == "POST" and not flag(data.get("confirm")):
        raise HTTPException(400, "deletion must be confirmed")
    row = request.app.state.db.query_one(
        "SELECT name FROM llm_providers WHERE id=?", (provider_id,)
    )
    if row is None:
        raise HTTPException(404)
    with request.app.state.db.transaction() as conn:
        conn.execute("DELETE FROM llm_providers WHERE id=?", (provider_id,))
        conn.execute(
            "UPDATE ai_summary_settings SET enabled=0,provider=NULL,model=NULL,updated_at=? "
            "WHERE provider=?",
            (now_iso(), row["name"]),
        )
    configured_names = {
        provider.name for provider in request.app.state.settings.tagger.llm.providers
    }
    if row["name"] not in configured_names:
        request.app.state.db.execute(
            "DELETE FROM provider_health WHERE provider=?", (row["name"],)
        )
    audit(
        request.app.state.db,
        "admin.provider_deleted",
        request=request,
        user_id=user["id"],
        target_type="provider",
        target_id=provider_id,
    )
    return {"ok": True} if wants_json(request) else RedirectResponse("/admin/providers", 303)


@router.get("/system")
async def system(request: Request, user: AdminUser):
    stats = request.app.state.db.stats()
    stats.pop("path", None)
    usage = shutil.disk_usage(Path(request.app.state.settings.storage.db_path).parent)
    provider_stats = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT provider,consecutive_failures,cooldown_until,total_calls,total_failures,"
            "updated_at FROM provider_health ORDER BY provider"
        )
    ]
    for provider in provider_stats:
        calls = provider["total_calls"]
        provider["success_rate"] = (
            round((calls - provider["total_failures"]) / calls, 4) if calls else None
        )
    process = resource.getrusage(resource.RUSAGE_SELF)
    result = {
        "database": stats,
        "articles": {
            row["status"]: row["count"]
            for row in request.app.state.db.query(
                "SELECT status,COUNT(*) AS count FROM articles GROUP BY status"
            )
        },
        "deliveries": {
            row["status"]: row["count"]
            for row in request.app.state.db.query(
                "SELECT status,COUNT(*) AS count FROM deliveries GROUP BY status"
            )
        },
        "attachment_bytes": request.app.state.db.query_one(
            "SELECT COALESCE(SUM(size_bytes),0) FROM (SELECT MAX(size_bytes) AS size_bytes "
            "FROM attachments WHERE status='downloaded' GROUP BY "
            "CASE WHEN sha256 IS NULL THEN 'id:'||id ELSE sha256 END)"
        )[0],
        "providers": provider_stats,
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
        "process": {
            "cpu_user_sec": round(process.ru_utime, 3),
            "cpu_system_sec": round(process.ru_stime, 3),
            "load_1m": round(os.getloadavg()[0], 3),
            "max_rss_kib": process.ru_maxrss,
        },
    }
    return result if wants_json(request) else _page(request, "System", [result], user)


@router.get("/audit")
async def audit_log(request: Request, user: AdminUser):
    rows = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT a.id,a.user_id,u.username,a.action,a.target_type,a.target_id,a.detail,a.ip,a.created_at "
            "FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 500"
        )
    ]
    return rows if wants_json(request) else _page(request, "Audit", rows, user)
