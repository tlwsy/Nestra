"""core.logging 的脱敏与配置测试。

日志是密钥泄漏最常见的出口，脱敏是安全承诺而非美化，
所以按键名、按值形态、按嵌套结构分别验证。
"""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from nestra.core.logging import (
    _REDACTED,
    configure_logging,
    get_logger,
    secret_filter,
)

pytestmark = pytest.mark.unit


def scrub(**kwargs: object) -> dict[str, object]:
    return secret_filter(None, "info", dict(kwargs))


# ── 按键名脱敏 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "password_hash",
        "api_key",
        "token",
        "token_hash",
        "secret_key",
        "authorization",
        "cookie",
        "session_token",
        "apprise_url",
        "totp_secret",
        "setup_token",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert scrub(**{key: "real-value"})[key] == _REDACTED


def test_initial_setup_event_intentionally_exposes_one_time_token() -> None:
    value = secret_filter(
        None,
        "warning",
        {"event": "initial_admin_setup_required", "setup_token": "one-time"},
    )
    assert value["setup_token"] == "one-time"


def test_key_matching_is_case_insensitive() -> None:
    assert scrub(API_KEY="v")["API_KEY"] == _REDACTED
    assert scrub(Authorization="v")["Authorization"] == _REDACTED


def test_non_sensitive_keys_pass_through() -> None:
    out = scrub(event="crawl.done", site="ujs-jwc", count=15)
    assert out == {"event": "crawl.done", "site": "ujs-jwc", "count": 15}


# ── 按值形态兜底 ──────────────────────────────────────────────


def test_openai_style_key_in_free_text_redacted() -> None:
    out = scrub(event="provider failed: sk-abcdef1234567890 rejected")
    assert "sk-abcdef1234567890" not in out["event"]
    assert _REDACTED in out["event"]


def test_bearer_header_redacted() -> None:
    out = scrub(detail="sent Bearer eyJhbGciOiJIUzI1NiJ9")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out["detail"]


def test_url_credentials_redacted_but_host_kept() -> None:
    out = scrub(target="https://user:p4ssw0rd@smtp.example.test/send")
    assert "p4ssw0rd" not in out["target"]
    assert "smtp.example.test" in out["target"]


def test_plain_values_unchanged() -> None:
    url = "https://jwc.ujs.edu.cn/info/1331/30031.htm"
    assert scrub(url=url)["url"] == url


# ── 嵌套结构 ──────────────────────────────────────────────────


def test_nested_dict_redacted() -> None:
    out = scrub(provider={"name": "primary", "api_key": "sk-live-xxxxxxxx"})
    assert out["provider"]["api_key"] == _REDACTED
    assert out["provider"]["name"] == "primary"


def test_nested_list_of_dicts_redacted() -> None:
    out = scrub(providers=[{"name": "a", "token": "t1"}, {"name": "b", "token": "t2"}])
    assert [p["token"] for p in out["providers"]] == [_REDACTED, _REDACTED]


def test_list_container_type_preserved() -> None:
    assert isinstance(scrub(items=["a", "b"])["items"], list)
    assert isinstance(scrub(items=("a", "b"))["items"], tuple)


def test_deeply_nested_value_pattern_redacted() -> None:
    out = scrub(ctx={"request": {"headers": ["Bearer abcdefgh12345678"]}})
    assert "abcdefgh12345678" not in json.dumps(out)


def test_non_string_scalars_untouched() -> None:
    out = scrub(count=3, ratio=0.5, ok=True, missing=None)
    assert out == {"count": 3, "ratio": 0.5, "ok": True, "missing": None}


# ── configure_logging ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_structlog():
    yield
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def test_json_format_emits_parseable_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    get_logger("test.mod").info("crawl.done", site="ujs-jwc", api_key="sk-secret-value")

    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["event"] == "crawl.done"
    assert line["site"] == "ujs-jwc"
    assert line["api_key"] == _REDACTED
    assert line["logger"] == "test.mod"
    assert line["level"] == "info"
    assert line["timestamp"].endswith("Z")


def test_console_format_is_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="console")
    get_logger().warning("provider.down", provider="primary")
    err = capsys.readouterr().err
    assert "provider.down" in err
    assert "primary" in err


def test_level_filtering_drops_debug(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", fmt="json")
    log = get_logger()
    log.debug("noise")
    log.info("also noise")
    log.warning("kept")
    err = capsys.readouterr().err
    assert "noise" not in err
    assert "kept" in err


def test_configure_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    configure_logging(level="INFO", fmt="json")
    get_logger().info("once")
    assert capsys.readouterr().err.strip().count('"event": "once"') == 1


def test_noisy_libraries_capped_at_warning() -> None:
    configure_logging(level="DEBUG", fmt="json")
    for name in ("httpx", "httpcore", "apscheduler", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING


def test_stdlib_logger_uses_same_json_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    logging.getLogger("uvicorn.access").info(
        '127.0.0.1 - "GET /setup?token=one-time-secret HTTP/1.1" 200'
    )
    line = json.loads(capsys.readouterr().err.strip())
    assert line["logger"] == "uvicorn.access"
    assert line["level"] == "info"
    assert "one-time-secret" not in line["event"]
    assert "token=***" in line["event"]


def test_stdlib_extra_fields_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    logging.getLogger("foreign").warning("provider failed", extra={"api_key": "AIza-real"})
    line = json.loads(capsys.readouterr().err.strip())
    assert line["api_key"] == _REDACTED
    assert line["event"] == "provider failed"


def test_stdlib_cookie_header_in_message_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", fmt="json")
    logging.getLogger("foreign").info("Cookie: session=top-secret-value")
    line = json.loads(capsys.readouterr().err.strip())
    assert "top-secret-value" not in line["event"]


def test_unknown_level_falls_back_to_info(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="LOUD", fmt="json")
    get_logger().info("still logged")
    assert "still logged" in capsys.readouterr().err
