"""字段加密、子密钥派生、链接签名。

单一主密钥 `NESTRA_SECRET_KEY`，用 HKDF 按用途派生子密钥。这样
会话签名密钥泄漏不会连带暴露推送目标的解密能力。

用 HKDF-SHA256 + HMAC + AES-GCM（via cryptography）。
`cryptography` 与 `argon2-cffi` 都是核心依赖，不依赖可选 Web/通知组件。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Literal

from argon2 import PasswordHasher
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import DecryptionFailed

Purpose = Literal["field", "session", "link", "setup"]

_INFO: dict[Purpose, bytes] = {
    "field": b"nestra/v1/field-encryption",
    "session": b"nestra/v1/session-signing",
    "link": b"nestra/v1/signed-link",
    "setup": b"nestra/v1/setup-token",
}

_VERSION = b"\x01"
_NONCE_LEN = 12


def _hkdf(master: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256（RFC 5869）。salt 留空，主密钥本身已是高熵随机值。"""
    prk = hmac.new(b"\x00" * 32, master, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


class Crypto:
    """按用途派生子密钥的加密门面。"""

    def __init__(self, secret_key: str) -> None:
        if not secret_key:
            raise ValueError("secret_key 不能为空")
        if len(secret_key) < 32:
            raise ValueError("secret_key 长度至少 32 字符（建议 `openssl rand -base64 32`）")
        # 主密钥可能是 base64，也可能是任意字符串；统一摘要成 32 字节
        self._master = hashlib.sha256(secret_key.encode()).digest()
        self._cache: dict[Purpose, bytes] = {}

    def subkey(self, purpose: Purpose) -> bytes:
        if purpose not in self._cache:
            self._cache[purpose] = _hkdf(self._master, _INFO[purpose])
        return self._cache[purpose]

    # ── 字段加密 ──────────────────────────────────────────────────

    def encrypt(self, plaintext: str, *, purpose: Purpose = "field") -> bytes:
        """AES-256-GCM。输出 `版本 || nonce || 密文+tag`。"""
        aead = AESGCM(self.subkey(purpose))
        nonce = os.urandom(_NONCE_LEN)
        return _VERSION + nonce + aead.encrypt(nonce, plaintext.encode(), _INFO[purpose])

    def decrypt(self, blob: bytes, *, purpose: Purpose = "field") -> str:
        if len(blob) < 1 + _NONCE_LEN + 16:
            raise DecryptionFailed("密文长度不足")
        if blob[:1] != _VERSION:
            raise DecryptionFailed(f"不支持的密文版本 {blob[0]!r}")
        aead = AESGCM(self.subkey(purpose))
        nonce = blob[1 : 1 + _NONCE_LEN]
        try:
            return aead.decrypt(nonce, blob[1 + _NONCE_LEN :], _INFO[purpose]).decode()
        except Exception as exc:  # InvalidTag 等
            raise DecryptionFailed("解密失败。通常是 NESTRA_SECRET_KEY 与写入时不一致") from exc

    # ── 签名令牌（有过期时间的 URL 安全串）────────────────────────

    def sign_payload(
        self, payload: dict[str, Any], *, ttl_sec: int, purpose: Purpose = "link"
    ) -> str:
        body = {**payload, "exp": int(time.time()) + ttl_sec}
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        b64 = base64.urlsafe_b64encode(raw).rstrip(b"=")
        sig = hmac.new(self.subkey(purpose), b64, hashlib.sha256).digest()
        return f"{b64.decode()}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"

    def verify_payload(self, token: str, *, purpose: Purpose = "link") -> dict[str, Any]:
        try:
            b64, sig_b64 = token.split(".", 1)
            given = base64.b64decode(
                sig_b64 + "=" * (-len(sig_b64) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error) as exc:
            raise DecryptionFailed("令牌格式非法") from exc

        expected = hmac.new(self.subkey(purpose), b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, given):
            raise DecryptionFailed("签名不匹配")

        try:
            raw = base64.b64decode(
                b64 + "=" * (-len(b64) % 4),
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            expires = payload.get("exp")
            if not isinstance(expires, (int, float)) or isinstance(expires, bool):
                raise TypeError("exp must be a number")
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise DecryptionFailed("令牌载荷非法") from exc

        if expires < time.time():
            raise DecryptionFailed("令牌已过期")
        return payload


# ── 无需密钥的工具 ────────────────────────────────────────────────


def new_token(nbytes: int = 32) -> str:
    """URL 安全随机令牌。用于会话 token、setup token。"""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """会话 token 的存储形态。

    token 本身是高熵随机值，SHA-256 足够；不需要 Argon2 那样的慢哈希。
    """
    return hashlib.sha256(token.encode()).hexdigest()


def url_hash(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode()).hexdigest()


def fingerprint(text: str, *, keep: int = 6) -> str:
    """推送 URL 的脱敏展示形态，如 `tgram://…a1b2c3`。"""
    # 保留协议头便于识别渠道，其余用摘要
    scheme = text.split("://", 1)[0] if "://" in text else "?"
    digest = hashlib.sha256(text.encode()).hexdigest()[:keep]
    return f"{scheme}://…{digest}"


# ── 密码哈希 ────────────────────────────────────────

# 2C2G 上的折中参数：64MB 内存 × 3 轮，单次验证约 100ms。
# OWASP 推荐的 46MB/1 起点偏低，而默认的 t=3/m=64MB 在
# 2 核机器上也不会把登录接口拖垮。
_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32, salt_len=16)


def hash_password(password: str) -> str:
    """Argon2id 哈希，输出自带参数与盐的 PHC 串。"""
    if not password:
        raise ValueError("password 不能为空")
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """验证密码。

    任何异常都收敛成 False：库里存了坏数据或旧格式时，登录接口
    应该拒绝而不是抛 500 把内部细节露出去。
    """
    try:
        return _ph.verify(hashed, password)
    except Exception:
        return False


def password_needs_rehash(hashed: str) -> bool:
    """参数调高后，登录成功时可据此透明升级旧哈希。"""
    try:
        return _ph.check_needs_rehash(hashed)
    except Exception:
        return False


def constant_time_compare(a: str, b: str) -> bool:
    """比较令牌等敏感字符串，避开时序侧道。"""
    return hmac.compare_digest(a.encode(), b.encode())
