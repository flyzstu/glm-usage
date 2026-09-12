"""Unit tests for time-window parsing and formatting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from glm_usage.timerange import SHANGHAI, InvalidRange, TimeRange, format_time, parse_time, resolve_range


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-06 00:00:00", datetime(2026, 9, 6, 0, 0, 0, tzinfo=SHANGHAI)),
        ("2026-09-06", datetime(2026, 9, 6, 0, 0, 0, tzinfo=SHANGHAI)),
        ("2026-09-06T13:45", datetime(2026, 9, 6, 13, 45, 0, tzinfo=SHANGHAI)),
        ("2026-09-06T13:45:30", datetime(2026, 9, 6, 13, 45, 30, tzinfo=SHANGHAI)),
    ],
)
def test_parse_time_accepts_common_formats(raw: str, expected: datetime) -> None:
    assert parse_time(raw) == expected


def test_parse_time_rejects_garbage() -> None:
    with pytest.raises(InvalidRange):
        parse_time("yesterday")


def test_format_time_normalises_timezone() -> None:
    utc = datetime(2026, 9, 6, 0, 0, 0, tzinfo=timezone.utc)
    assert format_time(utc) == "2026-09-06 08:00:00"


def test_resolve_defaults_align_to_midnight() -> None:
    window = resolve_range(None, None, default_days=7, max_days=366)
    assert window.start.hour == 0 and window.start.minute == 0
    assert (window.end - window.start).days == 7


def test_implicit_window_is_snapped_to_the_hour() -> None:
    window = resolve_range(None, None, default_days=7, max_days=366)
    assert (window.end.minute, window.end.second) == (59, 59)
    # Stable for the whole hour: this is what keeps the HTTP cache effective.
    assert resolve_range(None, None, default_days=7, max_days=366).formatted() == window.formatted()


def test_resolve_keeps_explicit_bounds() -> None:
    window = resolve_range("2026-01-01 08:00:00", "2026-01-05 20:00:00", default_days=7, max_days=366)
    assert window.start == datetime(2026, 1, 1, 8, 0, 0, tzinfo=SHANGHAI)
    assert window.end == datetime(2026, 1, 5, 20, 0, 0, tzinfo=SHANGHAI)
    assert window.formatted()["startTime"] == "2026-01-01 08:00:00"


def test_resolve_open_ended_start() -> None:
    window = resolve_range(None, "2026-01-10 12:00:00", default_days=3, max_days=366)
    assert window.start == datetime(2026, 1, 7, 0, 0, 0, tzinfo=SHANGHAI)


def test_resolve_allows_the_maximum_window() -> None:
    window = resolve_range(None, None, default_days=366, max_days=366)
    assert (window.end - window.start).days == 366


def test_resolve_rejects_reversed_bounds() -> None:
    with pytest.raises(InvalidRange, match="startTime 必须早于"):
        resolve_range("2026-01-05 00:00:00", "2026-01-01 00:00:00", default_days=7, max_days=366)


def test_resolve_rejects_future_end() -> None:
    future = (datetime.now(SHANGHAI) + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with pytest.raises(InvalidRange, match="不能超过当前时间"):
        resolve_range(None, future, default_days=7, max_days=366)


def test_resolve_rejects_oversized_window() -> None:
    end = datetime.now(SHANGHAI) - timedelta(days=1)
    start = end - timedelta(days=400)
    with pytest.raises(InvalidRange, match="时间跨度不能超过"):
        resolve_range(format_time(start), format_time(end), default_days=7, max_days=366)


def test_time_range_dict() -> None:
    window = TimeRange(
        start=datetime(2026, 1, 1, tzinfo=SHANGHAI),
        end=datetime(2026, 1, 3, 12, 0, tzinfo=SHANGHAI),
    )
    assert window.as_dict() == {
        "startTime": "2026-01-01T00:00:00+08:00",
        "endTime": "2026-01-03T12:00:00+08:00",
        "days": 2.5,
    }
