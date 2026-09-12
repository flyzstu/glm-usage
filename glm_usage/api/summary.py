"""``/summary``: the compact, normalised view used by status bars and scripts."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from ..config import Settings
from ..timerange import SHANGHAI, BadRequest, now
from .blueprint import bp
from .common import aggregate, cached, client, compact, fold_results, json_response, sections_meta
from .params import fingerprint, parse_fields, parse_max_age, parse_window, resolve_token, select_fields, window_key

# Upstream identifies the quota windows by (unit, number); these are the two the
# personal coding plan exposes, per DOCS.md.
SUMMARY_UNITS = {"fiveHour": 3, "weekly": 6}
SUMMARY_FIELDS = ("fiveHour", "weekly", "level", "totalCredits", "cacheHitRate", "window")


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def value_of(source: Any, key: str) -> Any:
    """Upstream wraps most scalars as ``{"value": ...}``, but not always."""
    if not isinstance(source, dict):
        return None
    item = source.get(key)
    return item.get("value") if isinstance(item, dict) else item


def quota_block(limit: dict[str, Any]) -> dict[str, Any]:
    """One quota window, normalised with its reset time in Asia/Shanghai."""
    total = as_float(limit.get("usage"))
    used = as_float(limit.get("currentValue"))
    reset_ms = as_float(limit.get("nextResetTime"))
    reset_at = datetime.fromtimestamp(reset_ms / 1000, SHANGHAI) if reset_ms else None
    return compact(
        limit=total,
        used=used,
        remaining=as_float(limit.get("remaining")),
        usedPercent=round(used / total * 100, 2) if used is not None and total else None,
        resetAt=reset_at.isoformat(timespec="seconds") if reset_at else None,
        resetInSeconds=max(int((reset_at - now()).total_seconds()), 0) if reset_at else None,
    )


@bp.get("/summary")
async def summary(request: Request) -> BaseHTTPResponse:
    """One small call for a status bar: both quotas, credit total, cache hit rate.

    It composes the same two cached sections as ``/quota`` and ``/usage``, so it
    never adds upstream traffic of its own. ``?maxAge=`` (default
    ``GLM_USAGE_SUMMARY_MAX_AGE``) is a freshness floor: data older than that is
    refetched, and if the upstream is down the section reports an error instead of
    handing back an old number as if it were current.
    """
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    if fields:
        unknown = [name for name in fields if name not in SUMMARY_FIELDS]
        if unknown:
            raise BadRequest(f"summary 的字段只能是 {' / '.join(SUMMARY_FIELDS)}，无法识别：{' / '.join(unknown)}")
    max_age = parse_max_age(request, default=settings.summary_max_age)
    window = parse_window(request, default_days=settings.default_usage_days, settings=settings)
    token = resolve_token(request)
    ident = fingerprint(token)
    upstream = client(request)
    keys = {
        "quota": ("quota", ident),
        "usage": ("usage", ident, "MODEL", *window_key(window)),
    }

    quota_result, usage_result = await asyncio.gather(
        cached(request, "quota", keys["quota"], lambda: upstream.quota(token), max_age=max_age),
        cached(
            request,
            "usage",
            keys["usage"],
            lambda: upstream.usage_detail(token, window, usage_type="MODEL"),
            max_age=max_age,
        ),
        return_exceptions=True,
    )
    sections, errors, states = fold_results((("quota", quota_result), ("usage", usage_result)))

    quota = sections.get("quota")
    quota = quota if isinstance(quota, dict) else None
    quota_blocks: dict[str, Any] = {}
    limits = quota.get("limits") if quota else None
    if isinstance(limits, list):
        for name, unit in SUMMARY_UNITS.items():
            match = next((item for item in limits if isinstance(item, dict) and item.get("unit") == unit), None)
            if match is not None:
                quota_blocks[name] = quota_block(match)

    usage = sections.get("usage")
    usage_summary = usage.get("summary") if isinstance(usage, dict) else None
    data = compact(
        **quota_blocks,
        level=quota.get("level") if quota else None,
        totalCredits=as_float(value_of(usage_summary, "totalCredits")),
        cacheHitRate=as_float(value_of(usage_summary, "cacheHitRate")),
    )
    if usage_summary is not None:
        data["window"] = window.as_dict()

    data, ignored = select_fields(data, fields)
    if fields and not data:
        raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")

    return json_response(
        aggregate(
            data,
            errors,
            states,
            **sections_meta(request, [(name, keys[name]) for name in states]),
            timezone=str(SHANGHAI),
            maxAgeSeconds=max_age,
            range=window.as_dict(),
            fields=fields or None,
        ),
        status=200 if data else 502,
    )
