"""UTC 约定、日期解析、跨零点静默时段与退避测试。"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from nestra.core import time as t

pytestmark = pytest.mark.unit


def dt(hour: int, minute: int = 0) -> datetime:
    """2026-01-01 的 UTC 时刻。"""
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


# ── UTC 序列化 ────────────────────────────────────────────────


def test_now_is_aware_utc() -> None:
    value = t.now()
    assert value.tzinfo is UTC
    assert abs((datetime.now(UTC) - value).total_seconds()) < 1


def test_now_iso_has_second_precision() -> None:
    value = t.now_iso()
    assert value.endswith("+00:00")
    assert "." not in value


def test_to_iso_normalizes_offset_to_utc() -> None:
    source = datetime.fromisoformat("2026-07-21T17:30:45+08:00")
    assert t.to_iso(source) == "2026-07-21T09:30:45+00:00"


def test_to_iso_treats_naive_as_utc() -> None:
    assert t.to_iso(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05+00:00"


def test_from_iso_normalizes_offset_to_utc() -> None:
    value = t.from_iso("2026-07-21T17:30:45+08:00")
    assert value == datetime(2026, 7, 21, 9, 30, 45, tzinfo=UTC)
    assert value.tzinfo is UTC


def test_from_iso_handles_naive_invalid_and_empty() -> None:
    assert t.from_iso("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert t.from_iso("not-a-date") is None
    assert t.from_iso(None) is None
    assert t.from_iso("") is None


# ── 站点日期解析 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("发布时间：2026-07-21", datetime(2026, 7, 20, 16, tzinfo=UTC)),
        ("发布日期: 2026年07月21日", datetime(2026, 7, 20, 16, tzinfo=UTC)),
        ("文章更新于 2026-07-21 17:30", datetime(2026, 7, 21, 9, 30, tzinfo=UTC)),
        ("2026/07/21", datetime(2026, 7, 20, 16, tzinfo=UTC)),
    ],
)
def test_parse_flexible_as_site_timezone(raw: str, expected: datetime) -> None:
    assert t.parse_flexible(raw, tz="Asia/Shanghai") == expected


def test_parse_flexible_preserves_explicit_offset() -> None:
    value = t.parse_flexible("2026-07-21T17:30:00+08:00", tz="UTC")
    assert value == datetime(2026, 7, 21, 9, 30, tzinfo=UTC)


def test_parse_flexible_handles_z_suffix() -> None:
    assert t.parse_flexible("2026-07-21T09:30:00Z") == datetime(2026, 7, 21, 9, 30, tzinfo=UTC)


def test_parse_flexible_invalid_and_blank() -> None:
    assert t.parse_flexible("") is None
    assert t.parse_flexible("昨天上午") is None


def test_parse_http_date() -> None:
    value = t.parse_http_date("Tue, 21 Jul 2026 09:36:08 GMT")
    assert value == datetime(2026, 7, 21, 9, 36, 8, tzinfo=UTC)
    assert t.parse_http_date("garbage") is None


def test_local_and_format_local() -> None:
    assert t.local(dt(0), "Asia/Shanghai").hour == 8
    # 无时区输入严格按 UTC，而非宿主机本地时区解释
    assert t.local(datetime(2026, 1, 1), "Asia/Shanghai").hour == 8
    assert t.format_local(dt(0), "Asia/Shanghai") == "2026-01-01 08:00"


# ── 静默时段 ──────────────────────────────────────────────────


def test_parse_quiet_hours() -> None:
    assert t.parse_quiet_hours("23:00-07:00") == (time(23), time(7))
    assert t.parse_quiet_hours(" 8:05-18:30 ") == (time(8, 5), time(18, 30))


@pytest.mark.parametrize("bad", ["", "23-07", "24:00-07:00", "23:60-07:00", "7:00 - 8:00"])
def test_parse_quiet_hours_rejects_bad_format(bad: str) -> None:
    with pytest.raises(ValueError, match="静默时段"):
        t.parse_quiet_hours(bad)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(22, False), (23, True), (0, True), (6, True), (7, False), (12, False)],
)
def test_cross_midnight_quiet_hours(hour: int, expected: bool) -> None:
    assert t.in_quiet_hours(dt(hour), "23:00-07:00", "UTC") is expected


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(8, False), (9, True), (12, True), (17, False)],
)
def test_same_day_quiet_hours(hour: int, expected: bool) -> None:
    assert t.in_quiet_hours(dt(hour), "09:00-17:00", "UTC") is expected


def test_equal_quiet_boundaries_mean_disabled() -> None:
    assert not t.in_quiet_hours(dt(0), "00:00-00:00", "UTC")


def test_quiet_hours_use_requested_timezone() -> None:
    # UTC 15:30 = 上海 23:30，落入 23:00-07:00
    assert t.in_quiet_hours(dt(15, 30), "23:00-07:00", "Asia/Shanghai")


def test_next_active_time_cross_midnight_before_midnight() -> None:
    assert t.next_active_time(dt(23, 30), "23:00-07:00", "UTC") == datetime(
        2026, 1, 2, 7, tzinfo=UTC
    )


def test_next_active_time_cross_midnight_after_midnight() -> None:
    assert t.next_active_time(dt(2), "23:00-07:00", "UTC") == dt(7)


def test_next_active_time_outside_quiet_returns_same_object() -> None:
    source = dt(12)
    assert t.next_active_time(source, "23:00-07:00", "UTC") is source


# ── 退避 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("attempt", "expected"), [(0, 5), (1, 5), (2, 10), (3, 20), (8, 640)])
def test_backoff_without_jitter(attempt: int, expected: float) -> None:
    assert t.backoff_delay(attempt, 5, jitter=False) == expected


def test_backoff_obeys_cap() -> None:
    assert t.backoff_delay(20, 5, cap_sec=60, jitter=False) == 60


def test_backoff_jitter_stays_in_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("random.uniform", lambda _a, _b: 0.5)
    assert t.backoff_delay(3, 10, jitter=True) == 20
    monkeypatch.setattr("random.uniform", lambda _a, _b: 1.5)
    assert t.backoff_delay(3, 10, jitter=True) == 60


def test_next_attempt_at_is_in_future(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(t, "now", lambda: fixed)
    monkeypatch.setattr(t, "backoff_delay", lambda *_args, **_kwargs: 30)
    assert t.next_attempt_at(1, 5) == t.to_iso(fixed + timedelta(seconds=30))
