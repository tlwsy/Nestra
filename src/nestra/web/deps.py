"""Request parsing and authentication dependencies."""

from __future__ import annotations

import json
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request

from nestra.core.crypto import hash_token
from nestra.core.time import now, to_iso

from .security import CSRF_COOKIE, SESSION_COOKIE, verify_csrf


async def request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json":
        try:
            value = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "JSON body must be an object")
        return value
    if content_type == "application/x-www-form-urlencoded":
        try:
            return {
                key: values[-1]
                for key, values in parse_qs(
                    (await request.body()).decode(), keep_blank_values=True
                ).items()
            }
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "invalid form encoding") from exc
    if not await request.body():
        return {}
    raise HTTPException(415, "use JSON or URL-encoded forms")


def wants_json(request: Request) -> bool:
    return (
        request.query_params.get("format") == "json"
        or "application/json" in request.headers.get("accept", "")
        or request.headers.get("content-type", "").split(";", 1)[0] == "application/json"
    )


async def current_user(request: Request) -> dict[str, Any]:
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "authentication required")
    row = request.app.state.db.query_one(
        "SELECT u.*,s.id AS session_id,s.expires_at AS session_expires "
        "FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND u.is_active=1",
        (hash_token(token), to_iso(now())),
    )
    if row is None:
        raise HTTPException(401, "authentication required")
    user = dict(row)
    request.state.user = user
    if user.get("must_change_password") and request.url.path not in {
        "/settings",
        "/settings/password",
        "/logout",
    }:
        raise HTTPException(403, "password change required")
    return user


async def admin_user(request: Request) -> dict[str, Any]:
    user = await current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    return user


CurrentUser = Annotated[dict[str, Any], Depends(current_user)]
AdminUser = Annotated[dict[str, Any], Depends(admin_user)]


def require_csrf(
    request: Request, data: dict[str, Any], user: dict[str, Any] | None = None
) -> None:
    supplied = request.headers.get("x-csrf-token") or data.get("_csrf")
    verify_csrf(
        request.app.state.crypto,
        request.cookies.get(CSRF_COOKIE),
        supplied,
        session_id=user["session_id"] if user else None,
    )


def write_guard(request: Request, data: dict[str, Any], user: dict[str, Any]) -> None:
    require_csrf(request, data, user)
    request.app.state.limiter.check("write", str(user["id"]), 60, 60)


def integer(value: object, name: str = "id") -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid {name}") from exc
    if result < 1:
        raise HTTPException(400, f"invalid {name}")
    return result


def number(value: object, name: str, *, minimum: float = 0, maximum: float = 1) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid {name}") from exc
    if not minimum <= result <= maximum:
        raise HTTPException(400, f"invalid {name}")
    return result


def flag(value: object) -> int:
    return int(value in (True, 1, "1", "true", "on", "yes"))
