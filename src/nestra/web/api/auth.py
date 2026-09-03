# ruff: noqa: E501
"""Setup, login, sessions, password changes and stdlib TOTP."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from nestra.core.crypto import (
    hash_password,
    hash_token,
    new_token,
    password_needs_rehash,
    verify_password,
)
from nestra.core.time import from_iso, now, to_iso

from ..deps import CurrentUser, request_data, require_csrf, wants_json, write_guard
from ..security import (
    CSRF_COOKIE,
    audit,
    clear_auth_cookies,
    csrf_token,
    new_session,
    new_totp_secret,
    provisioning_uri,
    set_auth_cookies,
    validate_password,
    validate_username,
    verify_totp,
)

router = APIRouter()


def _csrf_page(request: Request, template: str, context: dict | None = None) -> HTMLResponse:
    token = csrf_token(request.app.state.crypto)
    response = request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        context={"csrf": token, **(context or {})},
    )
    response.set_cookie(
        CSRF_COOKIE,
        token,
        secure=request.app.state.settings.web.cookie_secure,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return response


def _login_failure(request: Request, row, *, reason: str) -> None:
    db = request.app.state.db
    user_id = row["id"] if row else None
    if row and reason in {"credentials", "two_factor"}:
        with db.transaction() as conn:
            current = conn.execute(
                "SELECT failed_logins FROM users WHERE id=?", (row["id"],)
            ).fetchone()
            failures = (current["failed_logins"] if current else 0) + 1
            locked_until = None
            if failures >= 5:
                minutes = (1, 5, 15, 60)[min(failures - 5, 3)]
                locked_until = to_iso(now() + timedelta(minutes=minutes))
            conn.execute(
                "UPDATE users SET failed_logins=?,locked_until=?,updated_at=? WHERE id=?",
                (failures, locked_until, to_iso(now()), row["id"]),
            )
    audit(
        db,
        "auth.login_failed",
        request=request,
        user_id=user_id,
        target_type="user",
        target_id=user_id,
        detail=json.dumps({"reason": reason}),
    )


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    return _csrf_page(request, "login.html")


@router.post("/login")
async def login(request: Request):
    data = await request_data(request)
    require_csrf(request, data)
    request.app.state.limiter.check("login-global", "*", 120, 60)
    request.app.state.limiter.check("login-client", request.state.client_ip, 10, 300)
    request.app.state.limiter.check(
        "login-peer", request.client.host if request.client else "", 300, 300
    )
    try:
        username = validate_username(data.get("username"))
    except ValueError:
        username = ""
    request.app.state.limiter.check(
        "login-account", f"{request.state.client_ip}:{username}", 10, 300
    )
    row = request.app.state.db.query_one("SELECT * FROM users WHERE username=?", (username,))
    if row is None:
        verify_password(str(data.get("password", ""))[:1024], request.app.state.fake_password_hash)
        _login_failure(request, None, reason="credentials")
        raise HTTPException(401, "invalid credentials")
    locked_until = from_iso(row["locked_until"])
    locked = bool(locked_until and locked_until > now())
    if not row["is_active"]:
        _login_failure(request, row, reason="locked")
        raise HTTPException(401, "invalid credentials")
    password = data.get("password")
    if not isinstance(password, str) or not verify_password(password[:1024], row["password_hash"]):
        _login_failure(request, row, reason="locked" if locked else "credentials")
        raise HTTPException(401, "invalid credentials")
    if row["totp_secret"]:
        try:
            secret = request.app.state.crypto.decrypt(bytes(row["totp_secret"]))
        except Exception as exc:
            raise HTTPException(401, "invalid credentials") from exc
        code = data.get("totp")
        recovered = False
        if isinstance(code, str) and not verify_totp(secret, code):
            recovery = request.app.state.db.query_one(
                "SELECT code_hash FROM recovery_codes WHERE user_id=? AND code_hash=?",
                (row["id"], hash_token(code)),
            )
            if recovery:
                request.app.state.db.execute(
                    "DELETE FROM recovery_codes WHERE user_id=? AND code_hash=?",
                    (row["id"], recovery["code_hash"]),
                )
                recovered = True
            else:
                _login_failure(request, row, reason="locked" if locked else "two_factor")
                raise HTTPException(401, "invalid credentials")
        elif not isinstance(code, str):
            _login_failure(request, row, reason="locked" if locked else "two_factor")
            raise HTTPException(401, "invalid credentials")
        if recovered:
            audit(
                request.app.state.db, "auth.recovery_code_used", request=request, user_id=row["id"]
            )
    replacement = hash_password(password) if password_needs_rehash(row["password_hash"]) else None
    request.app.state.db.execute(
        "UPDATE users SET failed_logins=0,locked_until=NULL,"
        "password_hash=COALESCE(?,password_hash),updated_at=? WHERE id=?",
        (replacement, to_iso(now()), row["id"]),
    )
    token, session_id, _ = new_session(
        request.app.state.db,
        row["id"],
        request,
        days=request.app.state.settings.web.session_days,
    )
    remember = data.get("remember_me") in (True, 1, "1", "true", "on")
    response = (
        JSONResponse({"ok": True, "must_change_password": bool(row["must_change_password"])})
        if wants_json(request)
        or request.headers.get("content-type", "").startswith("application/json")
        else RedirectResponse("/settings" if row["must_change_password"] else "/", 303)
    )
    set_auth_cookies(
        response,
        token,
        csrf_token(
            request.app.state.crypto,
            session_id=session_id,
            ttl_sec=request.app.state.settings.web.session_days * 86400,
        ),
        secure=request.app.state.settings.web.cookie_secure,
        remember=remember,
        days=request.app.state.settings.web.session_days,
    )
    audit(request.app.state.db, "auth.login", request=request, user_id=row["id"])
    return response


@router.post("/logout")
async def logout(request: Request, user: CurrentUser):
    data = await request_data(request)
    write_guard(request, data, user)
    request.app.state.db.execute(
        "UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=?",
        (to_iso(now()), user["session_id"], user["id"]),
    )
    audit(request.app.state.db, "auth.logout", request=request, user_id=user["id"])
    response = (
        JSONResponse({"ok": True}) if wants_json(request) else RedirectResponse("/login", 303)
    )
    clear_auth_cookies(response, secure=request.app.state.settings.web.cookie_secure)
    return response


@router.get("/setup")
async def setup_page(request: Request, token: str = ""):
    if request.app.state.db.query_one("SELECT id FROM users LIMIT 1"):
        raise HTTPException(404)
    try:
        payload = request.app.state.crypto.verify_payload(token, purpose="setup")
    except Exception as exc:
        raise HTTPException(404) from exc
    if payload.get("kind") != "setup":
        raise HTTPException(404)
    return _csrf_page(request, "setup.html", {"setup_token": token})


@router.post("/setup")
async def setup(request: Request):
    data = await request_data(request)
    require_csrf(request, data)
    request.app.state.limiter.check("setup-ip", request.state.client_ip, 5, 300)
    try:
        payload = request.app.state.crypto.verify_payload(
            str(data.get("token", "")), purpose="setup"
        )
        if payload.get("kind") != "setup":
            raise ValueError
        username = validate_username(data.get("username"))
        password = validate_password(data.get("password"))
    except Exception as exc:
        raise HTTPException(400, "invalid setup request") from exc
    timestamp = to_iso(now())
    try:
        with request.app.state.db.transaction() as conn:
            if conn.execute("SELECT id FROM users LIMIT 1").fetchone():
                raise HTTPException(404)
            cursor = conn.execute(
                "INSERT INTO users (username,password_hash,role,created_at,updated_at) VALUES (?,?,'admin',?,?)",
                (username, hash_password(password), timestamp, timestamp),
            )
    except HTTPException:
        raise
    audit(
        request.app.state.db,
        "auth.setup",
        request=request,
        user_id=cursor.lastrowid,
        target_type="user",
        target_id=cursor.lastrowid,
    )
    return (
        JSONResponse({"ok": True}, status_code=201)
        if wants_json(request)
        else RedirectResponse("/login", 303)
    )


@router.get("/settings")
async def settings(request: Request, user: CurrentUser):
    rows = [
        dict(row)
        for row in request.app.state.db.query(
            "SELECT id,created_at,expires_at,created_ip,user_agent FROM sessions "
            "WHERE user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY created_at DESC",
            (user["id"], to_iso(now())),
        )
    ]
    if wants_json(request):
        return {
            "username": user["username"],
            "totp_enabled": bool(user["totp_secret"]),
            "sessions": rows,
        }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "user": user,
            "sessions": rows,
            "totp_enabled": bool(user["totp_secret"]),
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
        },
    )


@router.post("/settings/password")
async def change_password(request: Request, user: CurrentUser):
    data = await request_data(request)
    write_guard(request, data, user)
    if not verify_password(str(data.get("old_password", "")), user["password_hash"]):
        raise HTTPException(400, "current password is incorrect")
    try:
        password = validate_password(data.get("new_password"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with request.app.state.db.transaction() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?,must_change_password=0,updated_at=? WHERE id=?",
            (hash_password(password), to_iso(now()), user["id"]),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
            (to_iso(now()), user["id"], user["session_id"]),
        )
    audit(request.app.state.db, "user.password_changed", request=request, user_id=user["id"])
    return JSONResponse({"ok": True}) if wants_json(request) else RedirectResponse("/settings", 303)


@router.post("/settings/sessions/revoke")
async def revoke_sessions(request: Request, user: CurrentUser):
    data = await request_data(request)
    write_guard(request, data, user)
    request.app.state.db.execute(
        "UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
        (to_iso(now()), user["id"], user["session_id"]),
    )
    audit(request.app.state.db, "user.sessions_revoked", request=request, user_id=user["id"])
    return JSONResponse({"ok": True}) if wants_json(request) else RedirectResponse("/settings", 303)


@router.post("/settings/totp/start")
async def start_totp(request: Request, user: CurrentUser):
    data = await request_data(request)
    write_guard(request, data, user)
    if user["totp_secret"]:
        raise HTTPException(409, "2FA is already enabled")
    secret = new_totp_secret()
    token = request.app.state.crypto.sign_payload(
        {"kind": "totp-setup", "uid": user["id"], "secret": secret},
        ttl_sec=600,
        purpose="session",
    )
    result = {
        "setup_token": token,
        "secret": secret,
        "uri": provisioning_uri(secret, user["username"]),
    }
    if wants_json(request):
        return result
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="totp_setup.html",
        context={
            "user": user,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
            **result,
        },
    )


@router.post("/settings/totp/enable")
async def enable_totp(request: Request, user: CurrentUser):
    data = await request_data(request)
    write_guard(request, data, user)
    if user["totp_secret"]:
        raise HTTPException(409, "2FA is already enabled")
    try:
        payload = request.app.state.crypto.verify_payload(
            str(data.get("setup_token", "")), purpose="session"
        )
        if (
            payload.get("kind") != "totp-setup"
            or payload.get("uid") != user["id"]
            or not verify_totp(payload["secret"], data.get("code"))
        ):
            raise ValueError
    except Exception as exc:
        raise HTTPException(400, "invalid TOTP setup") from exc
    codes = [new_token(8) for _ in range(8)]
    with request.app.state.db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE users SET totp_secret=?,updated_at=? WHERE id=? AND totp_secret IS NULL",
            (request.app.state.crypto.encrypt(payload["secret"]), to_iso(now()), user["id"]),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "2FA is already enabled")
        conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (user["id"],))
        conn.executemany(
            "INSERT INTO recovery_codes (user_id,code_hash,created_at) VALUES (?,?,?)",
            [(user["id"], hash_token(code), to_iso(now())) for code in codes],
        )
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
            (to_iso(now()), user["id"], user["session_id"]),
        )
    audit(request.app.state.db, "user.totp_enabled", request=request, user_id=user["id"])
    result = {"ok": True, "recovery_codes": codes}
    if wants_json(request):
        return result
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="totp_recovery.html",
        context={"user": user, **result},
    )


@router.post("/settings/totp/disable")
async def disable_totp(request: Request, user: CurrentUser):
    data = await request_data(request)
    write_guard(request, data, user)
    password = data.get("password")
    code = data.get("code")
    if not isinstance(password, str) or not verify_password(password[:1024], user["password_hash"]):
        raise HTTPException(400, "invalid password or 2FA code")
    try:
        secret = request.app.state.crypto.decrypt(bytes(user["totp_secret"]))
    except Exception as exc:
        raise HTTPException(400, "invalid password or 2FA code") from exc
    recovery_hash = hash_token(code) if isinstance(code, str) else ""
    recovery = request.app.state.db.query_one(
        "SELECT code_hash FROM recovery_codes WHERE user_id=? AND code_hash=?",
        (user["id"], recovery_hash),
    )
    if not (isinstance(code, str) and verify_totp(secret, code)) and recovery is None:
        raise HTTPException(400, "invalid password or 2FA code")
    with request.app.state.db.transaction() as conn:
        conn.execute(
            "UPDATE users SET totp_secret=NULL,updated_at=? WHERE id=?", (to_iso(now()), user["id"])
        )
        conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (user["id"],))
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
            (to_iso(now()), user["id"], user["session_id"]),
        )
    audit(request.app.state.db, "user.totp_disabled", request=request, user_id=user["id"])
    return JSONResponse({"ok": True}) if wants_json(request) else RedirectResponse("/settings", 303)
