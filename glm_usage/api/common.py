"""Shared plumbing for the API layer: JSON responses, envelopes and cache access."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable, Iterable
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse
from sanic.response import json as sanic_json

from ..cache import CACHED_STATES, HIT, MISS, STALE, TTLCache
from ..client import BigModelClient
from ..metrics import Metrics
from ..timerange import BadRequest, iso_now
from .params import select_fields

try:  # orjson is ~3x faster than ujson on these payloads
    import orjson
except ImportError:  # pragma: no cover - optional acceleration
    orjson = None  # type: ignore[assignment]


def json_response(body: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> BaseHTTPResponse:
    if orjson is not None:
        return sanic_json(body, status=status, headers=headers, dumps=orjson.dumps)
    return sanic_json(body, status=status, headers=headers)


def compact(**values: Any) -> dict[str, Any]:
    """Build a dict, dropping keys whose value is ``None`` (i.e. not available)."""
    return {key: value for key, value in values.items() if value is not None}


def envelope(data: Any, state: str, ttl: float, **extra: Any) -> dict[str, Any]:
    """Single-section envelope: one cache state plus TTL."""
    meta = compact(
        cacheState=state,
        cached=state != MISS,
        ttl=int(ttl),
        generatedAt=iso_now(),
        **extra,
    )
    return {"data": data, "meta": meta}


def aggregate(data: Any, errors: dict[str, Any], states: dict[str, str], **extra: Any) -> dict[str, Any]:
    """Envelope for endpoints that assemble several upstream calls.

    ``errors`` is always present (``null`` when everything succeeded) so callers
    can distinguish "no failures" from "field missing".
    """
    meta = compact(
        cacheState=states,
        cached=bool(states) and all(state in CACHED_STATES for state in states.values()),
        generatedAt=iso_now(),
        **extra,
    )
    meta["errors"] = errors or None
    return {"data": data, "meta": meta}


def headers(state: str, ttl: float) -> dict[str, str]:
    return {"X-Cache": state, "Cache-Control": f"private, max-age={int(max(ttl, 0))}"}


def cache_header(states: dict[str, str]) -> str:
    """One ``X-Cache`` value for a multi-section response.

    ``HIT`` only when nothing had to be fetched, ``STALE`` when any part came
    from the stale fallback (that is the one worth alerting on), ``MISS`` otherwise.
    """
    if not states:
        return MISS
    values = set(states.values())
    if STALE in values:
        return STALE
    return HIT if values <= {HIT} else MISS


def client(request: Request) -> BigModelClient:
    return request.app.ctx.client


def fold_results(
    pairs: tuple[tuple[str, Any], ...],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Split ``gather(..., return_exceptions=True)`` output into per-section results."""
    sections: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    states: dict[str, str] = {}
    for name, result in pairs:
        if isinstance(result, BaseException):
            errors[name] = {
                "type": getattr(result, "kind", "error"),
                "message": str(result),
                "status": getattr(result, "status", 500),
            }
        else:
            sections[name], states[name] = result
    return sections, errors, states


def sections_meta(request: Request, entries: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Extras shared by the multi-section endpoints.

    ``entries`` is ``(cache_name, cache_key)`` for every part that came back, so
    the response can report how old its oldest number is.
    """
    caches = request.app.ctx.caches
    ages = [age for age in (caches[name].age(key) for name, key in entries) if age is not None]
    return compact(
        ageSeconds=round(max(ages), 1) if ages else None,
        refreshThrottled=getattr(request.ctx, "refresh_throttled", None) or None,
    )


async def cached(
    request: Request,
    cache_name: str,
    key: Hashable,
    loader: Callable[[], Awaitable[Any]],
    *,
    max_age: float | None = None,
) -> tuple[Any, str]:
    """Serve from cache, coalescing concurrent misses.

    ``?refresh=1`` forces a reload, but at most once per ``refresh_min_interval``
    per key — otherwise a shell loop could hammer the upstream while the whole
    point of the cache is to stay well inside the site's rate limits.
    """
    app = request.app
    cache: TTLCache = app.ctx.caches[cache_name]
    metrics: Metrics = app.ctx.metrics
    if request.get_args().get("refresh") in {"1", "true", "yes"}:
        if app.ctx.refresh_gate.allow(key):
            data = await loader()
            cache.set(key, data)
            metrics.record_cache("REFRESH")
            return data, MISS
        request.ctx.refresh_throttled = True
        metrics.record_cache("REFRESH_THROTTLED")
    data, state = await cache.get_or_load(key, loader, max_age=max_age)
    metrics.record_cache(state)
    return data, state


async def loaded(
    request: Request,
    cache_name: str,
    key: Hashable,
    loader: Callable[[], Awaitable[Any]],
    fields: tuple[str, ...],
) -> tuple[Any, str, dict[str, Any]]:
    """Cache-or-load + ``?fields=``.

    Returns ``(data, state, meta_extra)`` where the last item already carries the
    ``fields`` / ``ignoredFields`` / ``ageSeconds`` entries for the envelope, so
    handlers stay short and every endpoint reports how old its data is.
    """
    data, state = await cached(request, cache_name, key, loader)
    narrowed, ignored = select_fields(data, fields)
    if fields and not narrowed:
        raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")
    age = request.app.ctx.caches[cache_name].age(key)
    return narrowed, state, compact(
        fields=fields or None,
        ignoredFields=ignored or None,
        ageSeconds=None if age is None else round(age, 1),
        refreshThrottled=getattr(request.ctx, "refresh_throttled", None) or None,
    )
