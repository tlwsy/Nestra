"""子密钥派生、AES-GCM 字段加密、签名令牌、密码哈希。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from nestra.core import crypto as c
from nestra.core.crypto import Crypto
from nestra.core.errors import DecryptionFailed

pytestmark = pytest.mark.unit

OTHER_KEY = "a-completely-different-secret-key-0123456789"


def test_encrypt_decrypt_roundtrip(crypto: Crypto) -> None:
    plain = "tgram://bot123:AAH-secret/@channel"
    blob = crypto.encrypt(plain)
    assert plain.encode() not in blob
    assert crypto.decrypt(blob) == plain


def test_encrypt_is_non_deterministic(crypto: Crypto) -> None:
    """每次新 nonce，相同明文密文不同，避免密文比对泄漏信息。"""
    assert crypto.encrypt("same") != crypto.encrypt("same")


def test_decrypt_with_wrong_key_fails(crypto: Crypto) -> None:
    blob = crypto.encrypt("secret")
    with pytest.raises(DecryptionFailed):
        Crypto(OTHER_KEY).decrypt(blob)


def test_decrypt_rejects_tampered_ciphertext(crypto: Crypto) -> None:
    blob = bytearray(crypto.encrypt("secret"))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionFailed):
        crypto.decrypt(bytes(blob))


def test_decrypt_rejects_short_and_bad_version(crypto: Crypto) -> None:
    with pytest.raises(DecryptionFailed, match="长度不足"):
        crypto.decrypt(b"\x01short")
    blob = crypto.encrypt("x")
    with pytest.raises(DecryptionFailed, match="版本"):
        crypto.decrypt(b"\x02" + blob[1:])


def test_purposes_are_isolated(crypto: Crypto) -> None:
    """会话密钥不该能解开字段密文 —— 这是分用途派生的全部意义。"""
    blob = crypto.encrypt("x", purpose="field")
    with pytest.raises(DecryptionFailed):
        crypto.decrypt(blob, purpose="session")
    assert crypto.subkey("field") != crypto.subkey("session")


def test_subkey_is_stable_and_key_dependent(crypto: Crypto) -> None:
    assert crypto.subkey("link") == crypto.subkey("link")
    assert len(crypto.subkey("link")) == 32
    assert crypto.subkey("link") != Crypto(OTHER_KEY).subkey("link")


def test_short_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="至少 32"):
        Crypto("short")
    with pytest.raises(ValueError, match="不能为空"):
        Crypto("")


def test_signed_payload_roundtrip(crypto: Crypto) -> None:
    token = crypto.sign_payload({"attachment_id": 7}, ttl_sec=60)
    payload = crypto.verify_payload(token)
    assert payload["attachment_id"] == 7
    assert payload["exp"] > time.time()


def test_signed_payload_rejects_tampering(crypto: Crypto) -> None:
    token = crypto.sign_payload({"attachment_id": 7}, ttl_sec=60)
    body, sig = token.split(".", 1)
    with pytest.raises(DecryptionFailed, match="签名不匹配"):
        crypto.verify_payload(f"{body}x.{sig}")
    with pytest.raises(DecryptionFailed, match="格式非法"):
        crypto.verify_payload("no-dot-here")


def test_signed_payload_expires(crypto: Crypto) -> None:
    token = crypto.sign_payload({"a": 1}, ttl_sec=-1)
    with pytest.raises(DecryptionFailed, match="已过期"):
        crypto.verify_payload(token)


def test_signed_payload_rejects_other_purpose(crypto: Crypto) -> None:
    token = crypto.sign_payload({"a": 1}, ttl_sec=60, purpose="link")
    with pytest.raises(DecryptionFailed, match="签名不匹配"):
        crypto.verify_payload(token, purpose="setup")


def _sign_raw_payload(crypto: Crypto, payload: object) -> str:
    """生成签名正确但载荷形状恶意的令牌。"""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(crypto.subkey("link"), body, hashlib.sha256).digest()
    encoded_sig = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{body.decode()}.{encoded_sig.decode()}"


def test_signed_payload_normalizes_malformed_base64(crypto: Crypto) -> None:
    with pytest.raises(DecryptionFailed, match="格式非法"):
        crypto.verify_payload("abc.%%%")


def test_signed_payload_requires_json_object(crypto: Crypto) -> None:
    with pytest.raises(DecryptionFailed, match="载荷非法"):
        crypto.verify_payload(_sign_raw_payload(crypto, ["not", "an", "object"]))


def test_signed_payload_requires_numeric_expiry(crypto: Crypto) -> None:
    with pytest.raises(DecryptionFailed, match="载荷非法"):
        crypto.verify_payload(_sign_raw_payload(crypto, {"exp": "tomorrow"}))


def test_password_hash_verify() -> None:
    hashed = c.hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert c.verify_password("correct horse battery staple", hashed) is True
    assert c.verify_password("wrong password", hashed) is False


def test_password_hash_is_salted() -> None:
    assert c.hash_password("same") != c.hash_password("same")


def test_empty_password_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        c.hash_password("")


def test_verify_password_survives_malformed_hash() -> None:
    """库里存了坏数据也只能返回 False，不能把登录接口打成 500。"""
    assert c.verify_password("x", "not-a-hash") is False
    assert c.verify_password("x", "") is False
    assert c.password_needs_rehash("not-a-hash") is False


def test_token_generation_and_hash() -> None:
    raw = c.new_token()
    assert len(raw) >= 32
    digest = c.hash_token(raw)
    assert len(digest) == 64
    assert digest == c.hash_token(raw)
    assert digest != c.hash_token(c.new_token())


def test_constant_time_compare() -> None:
    assert c.constant_time_compare("abc", "abc") is True
    assert c.constant_time_compare("abc", "abd") is False
    assert c.constant_time_compare("abc", "abcd") is False


def test_url_hash_is_stable() -> None:
    assert c.url_hash("http://a/1") == c.url_hash("http://a/1")
    assert c.url_hash("http://a/1") != c.url_hash("http://a/2")


def test_fingerprint_hides_credentials() -> None:
    fp = c.fingerprint("tgram://bot123:AAH-supersecret/@chan")
    assert fp.startswith("tgram://")
    assert "AAH-supersecret" not in fp
    assert c.fingerprint("no-scheme").startswith("?://")
