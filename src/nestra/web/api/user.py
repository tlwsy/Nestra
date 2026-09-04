# ruff: noqa: E501
"""User-owned subscriptions, targets, delivered articles and attachments."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from nestra.core.crypto import fingerprint
from nestra.core.logging import safe_error
from nestra.core.time import now_iso, parse_quiet_hours
from nestra.extractor.sanitize import sanitize_html
from nestra.notifier.apprise_client import (
    ALLOWED_APPRISE_SCHEMES,
    AppriseClient,
    validate_target_url,
)
from nestra.notifier.attachment import sanitize_filename
from nestra.storage.files import attachment_path

from ..deps import (
    CurrentUser,
    current_user,
    flag,
    integer,
    number,
    request_data,
    wants_json,
    write_guard,
)
from ..security import CSRF_COOKIE, audit

router = APIRouter()


def _page(request: Request, title: str, rows: list[dict[str, Any]], user: dict):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="list.html",
        context={
            "title": title,
            "rows": rows,
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
        },
    )


async def _user_data(request: Request) -> dict[str, Any]:
    data = await request_data(request)
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        == "application/x-www-form-urlencoded"
    ):
        values = parse_qs((await request.body()).decode(), keep_blank_values=True)
        for name in ("tag_ids", "site_ids", "target_ids"):
            if name in values:
                data[name] = values[name]
    return data


def _form_response(request: Request, value: dict[str, Any], location: str):
    if (
        request.method == "POST"
        and request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")
        and not wants_json(request)
    ):
        return RedirectResponse(location, 303)
    return value


def _ids(value: object, name: str) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.lstrip().startswith("[") else value.split(",")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"invalid {name}") from exc
    if not isinstance(value, list):
        raise HTTPException(400, f"invalid {name}")
    result = sorted({integer(item, name) for item in value})
    if len(result) > 500:
        raise HTTPException(400, f"too many {name}")
    return result


def _subscription_values(data: dict[str, Any]) -> tuple:
    name = str(data.get("name", "")).strip()
    if not 1 <= len(name) <= 100:
        raise HTTPException(400, "invalid name")
    mode = data.get("match_mode", "any")
    if mode not in ("any", "all"):
        raise HTTPException(400, "invalid match_mode")
    quiet = str(data.get("quiet_hours", "")).strip() or None
    if quiet:
        try:
            parse_quiet_hours(quiet)
        except ValueError as exc:
            raise HTTPException(400, "invalid quiet_hours") from exc
    sites = _ids(data.get("site_ids") or data.get("site_filter"), "site_ids")
    return (
        name,
        mode,
        number(data.get("min_confidence", 0.5), "min_confidence"),
        json.dumps(sites) if sites else None,
        flag(data.get("include_attachments", True)),
        quiet,
        flag(data.get("enabled", True)),
    )


def _replace_links(conn, subscription_id: int, user_id: int, data: dict[str, Any]) -> None:
    tag_ids = _ids(data.get("tag_ids"), "tag_ids")
    site_ids = _ids(data.get("site_ids") or data.get("site_filter"), "site_ids")
    target_ids = _ids(data.get("target_ids"), "target_ids")
    if site_ids:
        marks = ",".join("?" for _ in site_ids)
        count = conn.execute(
            f"SELECT COUNT(*) FROM sites WHERE id IN ({marks})",  # noqa: S608
            site_ids,
        ).fetchone()[0]
        if count != len(site_ids):
            raise HTTPException(400, "unknown site")
    if tag_ids:
        marks = ",".join("?" for _ in tag_ids)
        count = conn.execute(
            f"SELECT COUNT(*) FROM tags t JOIN tagset_groups g ON g.id=t.group_id "  # noqa: S608
            f"WHERE t.id IN ({marks}) AND g.status='frozen' "
            "AND t.tagset_version=g.tagset_version",
            tag_ids,
        ).fetchone()[0]
        if count != len(tag_ids):
            raise HTTPException(400, "unknown tag")
    if target_ids:
        marks = ",".join("?" for _ in target_ids)
        count = conn.execute(
            f"SELECT COUNT(*) FROM notify_targets WHERE user_id=? AND id IN ({marks})",  # noqa: S608
            (user_id, *target_ids),
        ).fetchone()[0]
        if count != len(target_ids):
            raise HTTPException(400, "unknown target")
    tag_groups = {
        row[0]
        for tag_id in tag_ids
        for row in conn.execute("SELECT group_id FROM tags WHERE id=?", (tag_id,))
    }
    site_groups = {
        row[0]
        for site_id in site_ids
        for row in conn.execute("SELECT tagset_group_id FROM sites WHERE id=?", (site_id,))
    }
    if (
        len(tag_groups) > 1
        or len(site_groups) > 1
        or (tag_groups and site_groups and tag_groups != site_groups)
    ):
        raise HTTPException(400, "subscription must stay within one tagset group")
    conn.execute("DELETE FROM subscription_tags WHERE subscription_id=?", (subscription_id,))
    conn.execute("DELETE FROM subscription_targets WHERE subscription_id=?", (subscription_id,))
    conn.executemany(
        "INSERT INTO subscription_tags (subscription_id,tag_id) VALUES (?,?)",
        [(subscription_id, item) for item in tag_ids],
    )
    conn.executemany(
        "INSERT INTO subscription_targets (subscription_id,target_id) VALUES (?,?)",
        [(subscription_id, item) for item in target_ids],
    )


@router.get("/")
async def dashboard(request: Request):
    try:
        user = await current_user(request)
    except HTTPException as exc:
        if exc.status_code != 401 or wants_json(request):
            raise
        if not request.app.state.db.query_one("SELECT id FROM users LIMIT 1"):
            token = request.app.state.setup_token
            if token:
                return RedirectResponse(f"/setup?token={token}", 303)
        return RedirectResponse("/login", 303)
    rows = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT DISTINCT a.id,a.title,a.published_at,si.name AS site "
            "FROM articles a JOIN sites si ON si.id=a.site_id JOIN deliveries d ON d.article_id=a.id "
            "JOIN subscriptions s ON s.id=d.subscription_id "
            "WHERE s.user_id=? AND d.status='sent' ORDER BY a.published_at DESC LIMIT 10",
            (user["id"],),
        )
    ]
    if wants_json(request):
        return {"user": user["username"], "recent_articles": rows}
    return _page(request, "Dashboard", rows, user)


@router.get("/subscriptions")
async def subscriptions(request: Request, user: CurrentUser):
    db = request.app.state.db
    rows = [
        dict(row)
        for row in db.query(
            "SELECT id,name,match_mode,min_confidence,site_filter,include_attachments,quiet_hours,enabled "
            "FROM subscriptions WHERE user_id=? ORDER BY id",
            (user["id"],),
        )
    ]
    if wants_json(request):
        return rows
    for row in rows:
        row["site_ids"] = set(json.loads(row["site_filter"] or "[]"))
        row["tag_ids"] = {
            value["tag_id"]
            for value in db.query(
                "SELECT tag_id FROM subscription_tags WHERE subscription_id=?", (row["id"],)
            )
        }
        row["target_ids"] = {
            value["target_id"]
            for value in db.query(
                "SELECT target_id FROM subscription_targets WHERE subscription_id=?", (row["id"],)
            )
        }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={
            "subscriptions": rows,
            "sites": db.query("SELECT id,name FROM sites ORDER BY name,id"),
            "tags": db.query(
                "SELECT t.id,t.name FROM tags t JOIN tagset_groups g ON g.id=t.group_id "
                "WHERE g.status='frozen' AND t.tagset_version=g.tagset_version "
                "ORDER BY t.name,t.id"
            ),
            "targets": db.query(
                "SELECT id,name FROM notify_targets WHERE user_id=? ORDER BY name,id", (user["id"],)
            ),
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
        },
    )


@router.post("/subscriptions")
async def create_subscription(request: Request, user: CurrentUser):
    data = await _user_data(request)
    write_guard(request, data, user)
    values = _subscription_values(data)
    timestamp = now_iso()
    with request.app.state.db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO subscriptions (user_id,name,match_mode,min_confidence,site_filter,"
            "include_attachments,quiet_hours,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user["id"], *values, timestamp, timestamp),
        )
        _replace_links(conn, cursor.lastrowid, user["id"], data)
    audit(
        request.app.state.db,
        "subscription.created",
        request=request,
        user_id=user["id"],
        target_type="subscription",
        target_id=cursor.lastrowid,
    )
    return (
        {"id": cursor.lastrowid} if wants_json(request) else RedirectResponse("/subscriptions", 303)
    )


@router.post("/subscriptions/{subscription_id}")
async def update_subscription(subscription_id: int, request: Request, user: CurrentUser):
    data = await _user_data(request)
    write_guard(request, data, user)
    values = _subscription_values(data)
    with request.app.state.db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET name=?,match_mode=?,min_confidence=?,site_filter=?,"
            "include_attachments=?,quiet_hours=?,enabled=?,updated_at=? WHERE id=? AND user_id=?",
            (*values, now_iso(), subscription_id, user["id"]),
        )
        if cursor.rowcount != 1:
            raise HTTPException(404)
        _replace_links(conn, subscription_id, user["id"], data)
    audit(
        request.app.state.db,
        "subscription.updated",
        request=request,
        user_id=user["id"],
        target_type="subscription",
        target_id=subscription_id,
    )
    return _form_response(request, {"ok": True}, "/subscriptions")


@router.post("/subscriptions/{subscription_id}/delete")
@router.delete("/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: int, request: Request, user: CurrentUser):
    data = await _user_data(request)
    write_guard(request, data, user)
    cursor = request.app.state.db.execute(
        "DELETE FROM subscriptions WHERE id=? AND user_id=?",
        (subscription_id, user["id"]),
    )
    if cursor.rowcount != 1:
        raise HTTPException(404)
    audit(
        request.app.state.db,
        "subscription.deleted",
        request=request,
        user_id=user["id"],
        target_type="subscription",
        target_id=subscription_id,
    )
    return _form_response(request, {"ok": True}, "/subscriptions")


@router.get("/targets")
async def targets(request: Request, user: CurrentUser):
    rows = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT id,name,url_fingerprint,enabled,consecutive_failures,last_ok_at,last_error,created_at "
            "FROM notify_targets WHERE user_id=? ORDER BY id",
            (user["id"],),
        )
    ]
    if wants_json(request):
        return rows
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="targets.html",
        context={
            "targets": rows,
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
        },
    )


async def _target_values(data: dict[str, Any], request: Request) -> tuple[str, bytes, str, int]:
    name = str(data.get("name", "")).strip()
    url = str(data.get("apprise_url", "")).strip()
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "invalid target") from exc
    if (
        not 1 <= len(name) <= 100
        or not 4 <= len(url) <= 4096
        or not re.match(r"^[a-z][a-z0-9+.-]*://\S+$", url, re.I)
    ):
        raise HTTPException(400, "invalid target")
    if parsed.scheme.lower() not in ALLOWED_APPRISE_SCHEMES:
        raise HTTPException(400, "unsupported target scheme")
    try:
        validate_target_url(url)
    except Exception as exc:
        raise HTTPException(400, "unsupported target scheme") from exc
    return (
        name,
        request.app.state.crypto.encrypt(url),
        fingerprint(url),
        flag(data.get("enabled", True)),
    )


@router.post("/targets")
async def create_target(request: Request, user: CurrentUser):
    data = await _user_data(request)
    write_guard(request, data, user)
    values = await _target_values(data, request)
    cursor = request.app.state.db.execute(
        "INSERT INTO notify_targets (user_id,name,apprise_url_enc,url_fingerprint,enabled,created_at) VALUES (?,?,?,?,?,?)",
        (user["id"], *values, now_iso()),
    )
    audit(
        request.app.state.db,
        "target.created",
        request=request,
        user_id=user["id"],
        target_type="target",
        target_id=cursor.lastrowid,
    )
    return _form_response(request, {"id": cursor.lastrowid, "fingerprint": values[2]}, "/targets")


@router.post("/targets/{target_id}")
async def update_target(target_id: int, request: Request, user: CurrentUser):
    data = await _user_data(request)
    write_guard(request, data, user)
    if (
        request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")
        and not str(data.get("apprise_url", "")).strip()
    ):
        name = str(data.get("name", "")).strip()
        if not 1 <= len(name) <= 100:
            raise HTTPException(400, "invalid target")
        cursor = request.app.state.db.execute(
            "UPDATE notify_targets SET name=?,enabled=? WHERE id=? AND user_id=?",
            (name, flag(data.get("enabled")), target_id, user["id"]),
        )
        fingerprint_value = None
    else:
        values = await _target_values(data, request)
        cursor = request.app.state.db.execute(
            "UPDATE notify_targets SET name=?,apprise_url_enc=?,url_fingerprint=?,enabled=?,"
            "consecutive_failures=0,last_ok_at=NULL,last_error=NULL WHERE id=? AND user_id=?",
            (*values, target_id, user["id"]),
        )
        fingerprint_value = values[2]
    if cursor.rowcount != 1:
        raise HTTPException(404)
    audit(
        request.app.state.db,
        "target.updated",
        request=request,
        user_id=user["id"],
        target_type="target",
        target_id=target_id,
    )
    result = {"ok": True}
    if fingerprint_value is not None:
        result["fingerprint"] = fingerprint_value
    return _form_response(request, result, "/targets")


@router.post("/targets/{target_id}/delete")
@router.delete("/targets/{target_id}")
async def delete_target(target_id: int, request: Request, user: CurrentUser):
    data = await _user_data(request)
    write_guard(request, data, user)
    cursor = request.app.state.db.execute(
        "DELETE FROM notify_targets WHERE id=? AND user_id=?", (target_id, user["id"])
    )
    if cursor.rowcount != 1:
        raise HTTPException(404)
    audit(
        request.app.state.db,
        "target.deleted",
        request=request,
        user_id=user["id"],
        target_type="target",
        target_id=target_id,
    )
    return _form_response(request, {"ok": True}, "/targets")


@router.post("/targets/{target_id}/test")
async def test_target(target_id: int, request: Request, user: CurrentUser):
    data = await _user_data(request)
    write_guard(request, data, user)
    request.app.state.limiter.check("target-test", str(user["id"]), 5, 60)
    row = request.app.state.db.query_one(
        "SELECT apprise_url_enc FROM notify_targets WHERE id=? AND user_id=? AND enabled=1",
        (target_id, user["id"]),
    )
    if row is None:
        raise HTTPException(404)
    try:
        await AppriseClient(request.app.state.crypto).notify(
            bytes(row["apprise_url_enc"]),
            body="Nestra test notification",
            title="Nestra",
            body_format="text",
        )
    except Exception as exc:
        request.app.state.db.execute(
            "UPDATE notify_targets SET consecutive_failures=consecutive_failures+1,"
            "last_error=? WHERE id=? AND user_id=?",
            (safe_error(exc), target_id, user["id"]),
        )
        raise HTTPException(502, "test notification failed") from exc
    request.app.state.db.execute(
        "UPDATE notify_targets SET consecutive_failures=0,last_ok_at=?,last_error=NULL "
        "WHERE id=? AND user_id=?",
        (now_iso(), target_id, user["id"]),
    )
    audit(
        request.app.state.db,
        "target.tested",
        request=request,
        user_id=user["id"],
        target_type="target",
        target_id=target_id,
    )
    return _form_response(request, {"ok": True}, "/targets")


@router.get("/articles")
async def articles(request: Request, user: CurrentUser):
    site = request.query_params.get("site")
    tag = request.query_params.get("tag")
    since = request.query_params.get("since")
    admin_view = user["role"] == "admin"
    params: list[Any] = [] if admin_view else [user["id"]]
    filters = [] if admin_view else ["s.user_id=?", "d.status='sent'"]
    if site:
        filters.append("si.slug=?")
        params.append(site)
    if tag:
        filters.append(
            "EXISTS (SELECT 1 FROM article_tags at JOIN tags t ON t.id=at.tag_id WHERE at.article_id=a.id AND t.slug=?)"
        )
        params.append(tag)
    if since:
        filters.append("a.published_at>=?")
        params.append(since)
    joins = (
        "JOIN sites si ON si.id=a.site_id"
        if admin_view
        else "JOIN sites si ON si.id=a.site_id JOIN deliveries d ON d.article_id=a.id "
        "JOIN subscriptions s ON s.id=d.subscription_id"
    )
    where = " WHERE " + " AND ".join(filters) if filters else ""
    sql = (
        "SELECT DISTINCT a.id,a.title,a.summary,a.published_at,a.status,a.last_error,"  # noqa: S608 -- fixed clauses only
        f"si.name AS site FROM articles a {joins}{where} "
        "ORDER BY a.published_at DESC,a.id DESC LIMIT 200"
    )
    db = request.app.state.db
    rows = [dict(row) for row in db.query(sql, params)]
    if wants_json(request):
        return rows
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="articles.html",
        context={
            "articles": rows,
            "sites": db.query("SELECT slug,name FROM sites ORDER BY name,id"),
            "tags": db.query("SELECT slug,name FROM tags ORDER BY name,id"),
            "selected_site": site or "",
            "selected_tag": tag or "",
            "since": since or "",
            "admin_view": admin_view,
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
        },
    )


@router.get("/articles/{article_id}")
async def article(article_id: int, request: Request, user: CurrentUser):
    if user["role"] == "admin":
        row = request.app.state.db.query_one(
            "SELECT a.*,si.name AS site FROM articles a JOIN sites si ON si.id=a.site_id WHERE a.id=?",
            (article_id,),
        )
    else:
        row = request.app.state.db.query_one(
            "SELECT DISTINCT a.*,si.name AS site FROM articles a JOIN sites si ON si.id=a.site_id "
            "JOIN deliveries d ON d.article_id=a.id JOIN subscriptions s ON s.id=d.subscription_id "
            "WHERE a.id=? AND s.user_id=? AND d.status='sent'",
            (article_id, user["id"]),
        )
    if row is None:
        raise HTTPException(404)
    item = dict(row)
    item["content_html"] = sanitize_html(item.get("content_html") or "", base_url=item["url"])
    item["attachments"] = [
        dict(value)
        for value in request.app.state.db.query(
            "SELECT id,filename,size_bytes,mime_type FROM attachments "
            "WHERE article_id=? AND status='downloaded' ORDER BY id",
            (article_id,),
        )
    ]
    if wants_json(request):
        return item
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="article.html",
        context={
            "article": item,
            "admin_view": user["role"] == "admin",
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
        },
    )


def _attachment_file(request: Request, row) -> FileResponse:
    if row is None or not row["local_path"]:
        raise HTTPException(404)
    root = request.app.state.settings.storage.attachment_dir
    path = attachment_path(row["local_path"], root, require_file=True)
    if path is None:
        raise HTTPException(404)
    return FileResponse(
        path,
        media_type=row["mime_type"] or "application/octet-stream",
        filename=sanitize_filename(row["filename"] or "attachment"),
    )


@router.get("/shared/attachments/{attachment_id}")
async def shared_attachment(attachment_id: int, request: Request, token: str = ""):
    try:
        payload = request.app.state.crypto.verify_payload(token, purpose="link")
        user_id = int(payload["user_id"])
        if int(payload["attachment_id"]) != attachment_id:
            raise ValueError
    except Exception as exc:
        raise HTTPException(404) from exc
    row = request.app.state.db.query_one(
        "SELECT DISTINCT x.local_path,x.filename,x.mime_type FROM attachments x "
        "JOIN deliveries d ON d.article_id=x.article_id "
        "JOIN subscriptions s ON s.id=d.subscription_id "
        "WHERE x.id=? AND x.status='downloaded' AND d.status='sent' AND s.user_id=?",
        (attachment_id, user_id),
    )
    return _attachment_file(request, row)


@router.get("/attachments/{attachment_id}")
async def attachment(attachment_id: int, request: Request, user: CurrentUser):
    if user["role"] == "admin":
        row = request.app.state.db.query_one(
            "SELECT local_path,filename,mime_type FROM attachments "
            "WHERE id=? AND status='downloaded'",
            (attachment_id,),
        )
    else:
        row = request.app.state.db.query_one(
            "SELECT DISTINCT x.local_path,x.filename,x.mime_type FROM attachments x "
            "JOIN deliveries d ON d.article_id=x.article_id JOIN subscriptions s ON s.id=d.subscription_id "
            "WHERE x.id=? AND x.status='downloaded' AND d.status='sent' AND s.user_id=?",
            (attachment_id, user["id"]),
        )
    return _attachment_file(request, row)
