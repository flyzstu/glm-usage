"""``/account``: plan and account details, cached far longer than usage data."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from ..client import DEFAULT_PRODUCT_ID
from .blueprint import bp
from .common import aggregate, cache_header, cached, client, fold_results, json_response, sections_meta
from .params import fingerprint, parse_fields, resolve_token, select_sections


@bp.get("/account")
async def account(request: Request) -> BaseHTTPResponse:
    """Subscription, renewals, quota resets, balance, trial tokens, customer info.

    These change rarely, so they are cached far longer than the usage data and
    are kept out of /overview to keep the dashboard's refresh cheap. ``?fields=``
    selects which of them to fetch.
    """
    product_id = request.get_args().get("productId") or DEFAULT_PRODUCT_ID
    token = resolve_token(request)
    ident = fingerprint(token)
    upstream = client(request)

    loaders: dict[str, Callable[[], Awaitable[Any]]] = {
        "subscription": lambda: upstream.subscriptions(token),
        "autoRenewClosed": lambda: upstream.auto_renew_closed(token),
        "quotaResets": lambda: upstream.package_resets(token),
        "balance": lambda: upstream.account_report(token),
        "customer": lambda: upstream.customer_info(token),
        "trialTokens": lambda: upstream.token_magnitude(token, product_id),
    }
    keys: dict[str, Any] = {
        name: ("account", name, ident, product_id if name == "trialTokens" else "") for name in loaders
    }

    names = select_sections(parse_fields(request), loaders)
    results = await asyncio.gather(
        *(cached(request, "account", keys[name], loaders[name]) for name in names),
        return_exceptions=True,
    )
    sections, errors, states = fold_results(tuple(zip(names, results, strict=True)))

    return json_response(
        aggregate(
            sections,
            errors,
            states,
            **sections_meta(request, [("account", keys[name]) for name in states]),
            productId=product_id,
        ),
        headers={"X-Cache": cache_header(states)},
        status=200 if sections else 502,
    )
