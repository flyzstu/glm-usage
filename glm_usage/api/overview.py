"""``/overview``: every usage section in one round trip."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from ..client import USAGE_TYPES
from ..config import Settings
from ..timerange import BadRequest
from .blueprint import bp
from .common import aggregate, cache_header, cached, client, fold_results, json_response, sections_meta
from .params import fingerprint, parse_fields, parse_window, resolve_token, select_sections, window_key


@bp.get("/overview")
async def overview(request: Request) -> BaseHTTPResponse:
    """Everything the dashboard needs, fetched concurrently in one round trip.

    Upstream failures are reported per section instead of failing the request,
    so a partial outage still renders most of the page. ``?fields=`` picks the
    sections *and* skips fetching the ones that were not asked for.
    """
    settings: Settings = request.app.ctx.settings
    args = request.get_args()
    usage_type = (args.get("usageType") or "MODEL").upper()
    if usage_type not in USAGE_TYPES:
        raise BadRequest(f"usageType 只能是 {' 或 '.join(USAGE_TYPES)}")
    token = resolve_token(request)
    ident = fingerprint(token)
    usage_window = parse_window(request, default_days=settings.default_usage_days, settings=settings)
    activity_window = parse_window(request, default_days=settings.default_activity_days, settings=settings)

    upstream = client(request)
    loaders: dict[str, Callable[[], Awaitable[Any]]] = {
        "quota": lambda: upstream.quota(token),
        "usage": lambda: upstream.usage_detail(token, usage_window, usage_type=usage_type),
        "activity": lambda: upstream.activity(token, activity_window),
        "performance": lambda: upstream.performance(token, usage_window),
    }
    keys: dict[str, Any] = {
        "quota": ("quota", ident),
        "usage": ("usage", ident, usage_type, *window_key(usage_window)),
        "activity": ("activity", ident, *window_key(activity_window)),
        "performance": ("performance", ident, *window_key(usage_window)),
    }

    names = select_sections(parse_fields(request), loaders)
    results = await asyncio.gather(
        *(cached(request, name, keys[name], loaders[name]) for name in names),
        return_exceptions=True,
    )
    sections, errors, states = fold_results(tuple(zip(names, results, strict=True)))

    return json_response(
        aggregate(
            sections,
            errors,
            states,
            **sections_meta(request, [(name, keys[name]) for name in states]),
            range={
                "usage": usage_window.as_dict(),
                "activity": activity_window.as_dict(),
                "performance": usage_window.as_dict(),
            },
            usageType=usage_type,
        ),
        headers={"X-Cache": cache_header(states)},
        status=200 if sections else 502,
    )
