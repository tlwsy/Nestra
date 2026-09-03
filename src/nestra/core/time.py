"""时间工具。

约定：**存储与比较一律 UTC**，只在渲染给用户时转本地时区。
混用是时间 bug 的头号来源。

静默时段要处理跨零点（`23:00-07:00`），朴素的 `start <= t <= end`
在这种区间上是错的。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

_QUIET_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")

_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y年%m月%d日",
    "%Y/%m/%d",
)


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    """DB 存储用的 UTC ISO8601 字符串，秒精度。"""
    return now().replace(microsecond=0).isoformat()


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_flexible(value: str, *, tz: str = "UTC") -> datetime | None:
    """解析站点上五花八门的日期写法。

    没有时区信息时按站点本地时区解释 —— 中文站点写 `2026-07-21`
    指的是当地日期，按 UTC 解释会差 8 小时，可能跨天。
    """
    text = value.strip()
    if not text:
        return None

    text = re.sub(r"^(发布(时间|日期)|时间|日期)[：:]\s*", "", text)
    text = text.replace("年", "-").replace("月", "-").replace("日", "")

    if m := re.search(
        r"\d{4}-\d{1,2}-\d{1,2}"
        r"(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?",
        text,
    ):
        text = m.group(0)

    zone = ZoneInfo(tz)
    for fmt in _ISO_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (dt.replace(tzinfo=zone) if dt.tzinfo is None else dt).astimezone(UTC)

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (dt.replace(tzinfo=zone) if dt.tzinfo is None else dt).astimezone(UTC)


def parse_http_date(value: str) -> datetime | None:
    """RFC 7231 日期（Last-Modified / Date 头）。"""
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def local(dt: datetime, tz: str) -> datetime:
    # 流水线约定无时区值按 UTC 解释；不能让 astimezone 猜宿主机时区。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz))


def format_local(dt: datetime, tz: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return local(dt, tz).strftime(fmt)


# ── 静默时段 ──────────────────────────────────────────────────────


def parse_quiet_hours(spec: str) -> tuple[time, time]:
    m = _QUIET_RE.match(spec.strip())
    if not m:
        raise ValueError(f"静默时段格式非法: {spec!r}，应形如 '23:00-07:00'")
    sh, sm, eh, em = (int(g) for g in m.groups())
    return time(sh, sm), time(eh, em)


def in_quiet_hours(dt: datetime, spec: str, tz: str) -> bool:
    """判断某 UTC 时刻是否落在用户本地时区的静默时段内。"""
    start, end = parse_quiet_hours(spec)
    current = local(dt, tz).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # 跨零点


def next_active_time(dt: datetime, spec: str, tz: str) -> datetime:
    """静默时段结束的时刻（UTC）。不在静默期内则原样返回。"""
    if not in_quiet_hours(dt, spec, tz):
        return dt

    _, end = parse_quiet_hours(spec)
    zone = ZoneInfo(tz)
    local_dt = dt.astimezone(zone)
    candidate = local_dt.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local_dt:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


# ── 退避 ──────────────────────────────────────────────────────────


def backoff_delay(
    attempt: int, base_sec: float, *, cap_sec: float = 3600, jitter: bool = True
) -> float:
    """指数退避 + 抖动。

    抖动不是可选项：同一站点的多篇文章会在同一轮一起失败，
    无抖动会让它们在同一秒重试，制造周期性尖峰。
    """
    import random

    delay = min(base_sec * (2 ** max(0, attempt - 1)), cap_sec)
    if jitter:
        delay *= random.uniform(0.5, 1.5)  # noqa: S311 — 非加密用途
    return round(min(delay, cap_sec), 2)


def next_attempt_at(attempt: int, base_sec: float, *, cap_sec: float = 3600) -> str:
    return to_iso(now() + timedelta(seconds=backoff_delay(attempt, base_sec, cap_sec=cap_sec)))
