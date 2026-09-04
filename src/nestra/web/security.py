# ruff: noqa: E501
"""Small security primitives used by the private Web UI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import struct
import threading
import time
from collections import OrderedDict, deque
from datetime import timedelta
from urllib.parse import quote

from fastapi import HTTPException, Request

from nestra.core.crypto import Crypto, constant_time_compare, hash_token, new_token
from nestra.core.time import now, to_iso

SESSION_COOKIE = "nestra_session"
CSRF_COOKIE = "nestra_csrf"
LOCALE_COOKIE = "nestra_locale"
ADVANCED_COOKIE = "nestra_advanced"
PASSWORD_MIN_LENGTH = 12
_MAX_RATE_LIMIT_KEYS = 10_000
_WEAK = {
    "123456789012",
    "adminadminadmin",
    "letmeinletmein",
    "password1234",
    "qwertyuiop12",
    "welcome12345",
}


def validate_password(password: object) -> str:
    if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= 1024:
        raise ValueError(f"password must be {PASSWORD_MIN_LENGTH}-1024 characters")
    if password.casefold() in _WEAK:
        raise ValueError("password is too common")
    return password


def validate_username(username: object) -> str:
    if not isinstance(username, str):
        raise ValueError("invalid username")
    value = username.strip().lower()
    if not 1 <= len(value) <= 64 or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for c in value
    ):
        raise ValueError("username must contain only a-z, 0-9, _, . or -")
    return value


def csrf_token(
    crypto: Crypto,
    *,
    session_id: str | None = None,
    ttl_sec: int = 24 * 60 * 60,
) -> str:
    return crypto.sign_payload(
        {"kind": "csrf", "sid": session_id or "public", "nonce": new_token(16)},
        ttl_sec=ttl_sec,
        purpose="session",
    )


def verify_csrf(
    crypto: Crypto, cookie: str | None, supplied: object, *, session_id: str | None = None
) -> None:
    if not cookie or not isinstance(supplied, str) or not constant_time_compare(cookie, supplied):
        raise HTTPException(403, "CSRF validation failed")
    try:
        payload = crypto.verify_payload(cookie, purpose="session")
    except Exception as exc:
        raise HTTPException(403, "CSRF validation failed") from exc
    expected = session_id or "public"
    if payload.get("kind") != "csrf" or payload.get("sid") != expected:
        raise HTTPException(403, "CSRF validation failed")


def set_auth_cookies(
    response, token: str, csrf: str, *, secure: bool, remember: bool, days: int
) -> None:
    common = {"secure": secure, "samesite": "lax", "path": "/"}
    max_age = days * 86400 if remember else None
    response.set_cookie(SESSION_COOKIE, token, httponly=True, max_age=max_age, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, max_age=max_age, **common)


def clear_auth_cookies(response, *, secure: bool) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, httponly=False, samesite="lax")


def new_session(db, user_id: int, request: Request, *, days: int) -> tuple[str, str, str]:
    token = new_token()
    session_id = new_token(18)
    created = to_iso(now())
    expires = to_iso(now() + timedelta(days=days))
    db.execute(
        "INSERT INTO sessions (id,user_id,token_hash,expires_at,created_at,created_ip,user_agent) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            session_id,
            user_id,
            hash_token(token),
            expires,
            created,
            request.state.client_ip,
            request.headers.get("user-agent", "")[:500],
        ),
    )
    return token, session_id, expires


class RateLimiter:
    """Process-local fixed window limiter; the documented deployment uses one worker."""

    def __init__(self) -> None:
        self._events: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, scope: str, key: str, limit: int, window: int) -> None:
        current = time.monotonic()
        with self._lock:
            identity = (scope, key)
            events = self._events.setdefault(identity, deque())
            self._events.move_to_end(identity)
            if len(self._events) > _MAX_RATE_LIMIT_KEYS:
                self._events.popitem(last=False)
            while events and events[0] <= current - window:
                events.popleft()
            if len(events) >= limit:
                retry = max(1, int(window - (current - events[0])))
                raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(retry)})
            events.append(current)


def _trusted(peer: str, configured: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(peer)
        return any(address in ipaddress.ip_network(item, strict=False) for item in configured)
    except ValueError:
        return False


def client_ip(request: Request, trusted_proxies: list[str]) -> str:
    peer = request.client.host if request.client else "unknown"
    if not _trusted(peer, trusted_proxies):
        return peer
    chain = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
    chain.append(peer)
    for value in reversed(chain):
        if not _trusted(value, trusted_proxies):
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                return peer
    return peer


def request_is_https(request: Request, trusted_proxies: list[str]) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else "unknown"
    return (
        _trusted(peer, trusted_proxies)
        and request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    )


def audit(
    db,
    action: str,
    *,
    request: Request,
    user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO audit_log (user_id,action,target_type,target_id,detail,ip,created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, action, target_type, target_id, detail, request.state.client_ip, to_iso(now())),
    )


# RFC 6238, SHA-1 is required for compatibility rather than password hashing.
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, timestamp: int | None = None) -> str:
    raw = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = (int(time.time()) if timestamp is None else timestamp) // 30
    digest = hmac.new(raw, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret: str, code: object) -> bool:
    if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
        return False
    current = int(time.time())
    return any(
        hmac.compare_digest(totp_code(secret, current + step * 30), code) for step in (-1, 0, 1)
    )


def provisioning_uri(secret: str, username: str) -> str:
    return f"otpauth://totp/Nestra:{quote(username)}?secret={secret}&issuer=Nestra"
