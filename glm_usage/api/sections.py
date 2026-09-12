"""The four single-section usage endpoints: a thin, cached passthrough each."""

from __future__ import annotations

from sanic import Request
from sanic.response import BaseHTTPResponse

from ..client import USAGE_TYPES
from ..config import Settings
from ..timerange import BadRequest
from .blueprint import bp
from .common import client, envelope, headers, json_response, loaded
from .params import fingerprint, parse_fields, parse_window, resolve_token, window_key


@bp.get("/quota")
async def quota(request: Request) -> BaseHTTPResponse:
    """5-hour window and weekly credit limits."""
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    token = resolve_token(request)
    key = ("quota", fingerprint(token))
    data, state, extra = await loaded(request, "quota", key, lambda: client(request).quota(token), fields)
    return json_response(
        envelope(data, state, settings.quota_ttl, **extra),
        headers=headers(state, settings.quota_ttl),
    )


@bp.get("/usage")
async def usage(request: Request) -> BaseHTTPResponse:
    """Per-model credit and token consumption."""
    settings: Settings = request.app.ctx.settings
    args = request.get_args()
    fields = parse_fields(request)
    usage_type = (args.get("usageType") or args.get("usage_type") or "MODEL").upper()
    if usage_type not in USAGE_TYPES:
        raise BadRequest(f"usageType 只能是 {' 或 '.join(USAGE_TYPES)}")
    window = parse_window(request, default_days=settings.default_usage_days, settings=settings)
    token = resolve_token(request)
    key = ("usage", fingerprint(token), usage_type, *window_key(window))
    data, state, extra = await loaded(
        request,
        "usage",
        key,
        lambda: client(request).usage_detail(token, window, usage_type=usage_type),
        fields,
    )
    return json_response(
        envelope(data, state, settings.usage_ttl, range=window.as_dict(), usageType=usage_type, **extra),
        headers=headers(state, settings.usage_ttl),
    )


@bp.get("/activity")
async def activity(request: Request) -> BaseHTTPResponse:
    """Cumulative tokens, streaks and the daily series."""
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    window = parse_window(request, default_days=settings.default_activity_days, settings=settings)
    token = resolve_token(request)
    key = ("activity", fingerprint(token), *window_key(window))
    data, state, extra = await loaded(
        request, "activity", key, lambda: client(request).activity(token, window), fields
    )
    return json_response(
        envelope(data, state, settings.activity_ttl, range=window.as_dict(), **extra),
        headers=headers(state, settings.activity_ttl),
    )


@bp.get("/performance")
async def performance(request: Request) -> BaseHTTPResponse:
    """Daily decode speed and success rate (lite vs pro/max)."""
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    window = parse_window(request, default_days=settings.default_usage_days, settings=settings)
    token = resolve_token(request)
    key = ("performance", fingerprint(token), *window_key(window))
    data, state, extra = await loaded(
        request, "performance", key, lambda: client(request).performance(token, window), fields
    )
    return json_response(
        envelope(data, state, settings.performance_ttl, range=window.as_dict(), **extra),
        headers=headers(state, settings.performance_ttl),
    )
