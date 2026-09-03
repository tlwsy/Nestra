"""统一的 structlog / 标准库 logging 配置与机密脱敏。

生产输出为逐行 JSON；本地开发可选 console。uvicorn、httpx 等标准库 logger
必须走同一 ProcessorFormatter，否则容器日志会混入纯文本并绕过脱敏。
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "api_key",
        "apikey",
        "token",
        "token_hash",
        "secret",
        "secret_key",
        "authorization",
        "cookie",
        "set_cookie",
        "session_token",
        "apprise_url",
        "totp_secret",
        "setup_token",
    }
)

_REDACTED = "***"

# 值里的兜底形态。查询参数尤其重要：uvicorn access log 会把完整 URL 写入 event。
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), _REDACTED),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE), _REDACTED),
    (re.compile(r"(?<=://)([^:@/\s]+:[^@/\s]+)(?=@)"), _REDACTED),
    (
        re.compile(
            r"([?&](?:token|api_key|key|secret|password|signature|sig)=)[^&\s\"']+",
            re.IGNORECASE,
        ),
        rf"\1{_REDACTED}",
    ),
    (
        re.compile(r"\b(authorization|cookie|set-cookie):\s*[^\r\n]+", re.IGNORECASE),
        rf"\1: {_REDACTED}",
    ),
)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in _VALUE_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {k: _scrub(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v) for v in value)
    return value


def _scrub(key: str, value: Any) -> Any:
    if key.lower() in _SENSITIVE_KEYS:
        return _REDACTED
    return _scrub_value(value)


def safe_error(error: BaseException) -> str:
    """Bounded, credential-scrubbed exception text safe for persistence."""
    return str(_scrub_value(f"{type(error).__name__}: {error}"))[:500]


def secret_filter(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """渲染前递归脱敏；首次安装令牌是唯一有意输出的机密。"""
    setup_event = event_dict.get("event") == "initial_admin_setup_required"
    return {
        k: v if setup_event and k == "setup_token" else _scrub(str(k), v)
        for k, v in event_dict.items()
    }


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """配置 structlog 与标准库 logging。幂等，可重复调用。"""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if fmt == "console":
        exception_processor: structlog.types.Processor = structlog.processors.format_exc_info
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty()
        )
    else:
        exception_processor = structlog.processors.dict_tracebacks
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            exception_processor,
            secret_filter,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Uvicorn 有些版本自带 handler 且 propagate=false；强制统一到根处理器。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(numeric_level)

    for noisy in ("httpx", "httpcore", "apscheduler", "urllib3"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.types.FilteringBoundLogger:
    """获取与标准库同管线的 structlog logger。"""
    return structlog.get_logger(name)
