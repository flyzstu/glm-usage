"""Time-window helpers.

The upstream API speaks ``YYYY-MM-DD HH:mm:ss`` in ``Asia/Shanghai`` and rejects
anything else, so every user supplied range is normalised here before it reaches
the client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - depends on the tz database being present
    from zoneinfo import ZoneInfo

    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - fallback keeps the service usable
    SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_PARSERS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


class BadRequest(ValueError):
    """Raised for any query parameter the caller got wrong."""


class InvalidRange(BadRequest):
    """Raised when a caller supplied time window cannot be parsed or is unusable."""


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime

    def formatted(self) -> dict[str, str]:
        return {"startTime": format_time(self.start), "endTime": format_time(self.end)}

    def as_dict(self) -> dict[str, str]:
        return {
            "startTime": self.start.isoformat(),
            "endTime": self.end.isoformat(),
            "days": round((self.end - self.start).total_seconds() / 86400, 2),
        }


def now() -> datetime:
    return datetime.now(SHANGHAI)


def format_time(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(SHANGHAI)
    return value.strftime(TIME_FORMAT)


def parse_time(raw: str, *, field: str = "time") -> datetime:
    text = raw.strip().replace("+00:00", "").strip()
    for fmt in _PARSERS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    raise InvalidRange(
        f"无法解析 {field}={raw!r}，请使用 YYYY-MM-DD 或 'YYYY-MM-DD HH:mm:ss' 格式"
    )


def _normalise(value: datetime) -> datetime:
    return value.astimezone(SHANGHAI) if value.tzinfo else value.replace(tzinfo=SHANGHAI)


def resolve_range(
    start_raw: str | None,
    end_raw: str | None,
    *,
    default_days: int,
    max_days: int,
) -> TimeRange:
    """Turn loose query parameters into a validated window.

    Missing bounds fall back to "the last ``default_days`` days, aligned to
    midnight" which is what the dashboard wants. Windows longer than
    ``max_days`` are rejected so a stray ``?days=100000`` cannot hammer upstream.
    """
    reference = now()
    if end_raw:
        end = _normalise(parse_time(end_raw, field="endTime"))
    else:
        # Implicit windows end at the close of the current hour. The upstream
        # data is a daily aggregate, and a second-resolution bound would make
        # every request a different cache key (one upstream round trip per
        # second instead of one per TTL).
        end = reference.replace(minute=59, second=59, microsecond=0)
    if start_raw:
        start = _normalise(parse_time(start_raw, field="startTime"))
    else:
        start = (end - timedelta(days=max(default_days, 1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    if start >= end:
        raise InvalidRange("startTime 必须早于 endTime")
    if end > reference + timedelta(days=1):
        raise InvalidRange("endTime 不能超过当前时间")
    # One day of slack: a requested "last N days" starts at midnight, so its raw
    # span is always N days plus the elapsed part of today.
    if end - start > timedelta(days=max_days + 1):
        raise InvalidRange(f"时间跨度不能超过 {max_days} 天")
    return TimeRange(start=start, end=end)


def iso_now() -> str:
    return now().isoformat(timespec="seconds")
