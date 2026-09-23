"""Query-parameter parsing and credential resolution."""

from __future__ import annotations

import hashlib
from typing import Any

from sanic import Request

from ..client import MissingTokenError
from ..config import Settings, TokenStore
from ..timerange import BadRequest, TimeRange, resolve_range


def resolve_token(request: Request) -> str:
    """Prefer a caller supplied header so the API works multi-token."""
    header = request.headers.get("authorization")
    if header:
        token = header[7:].strip() if header[:7].lower() == "bearer " else header.strip()
        if token:
            return token
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    if zcode_svc and zcode_svc.credentials.api_key:
        return zcode_svc.credentials.api_key
    token = tokens(request).get()
    if not token:
        raise MissingTokenError(
            "未配置 bigmodel token：请设置 BIGMODEL_TOKEN / BIGMODEL_TOKEN_FILE，"
            "或在请求中带上 Authorization 请求头，或在看板完成凭据配置"
        )
    return token


def tokens(request: Request) -> TokenStore:
    return request.app.ctx.tokens


def fingerprint(token: str) -> str:
    """Short, non-reversible token id used to keep cache entries separate."""
    return hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest()


def parse_window(request: Request, *, default_days: int, settings: Settings) -> TimeRange:
    args = request.get_args()
    raw_days = args.get("days")
    if raw_days:
        try:
            default_days = int(raw_days)
        except ValueError:
            raise BadRequest(f"days 必须是整数，收到 {raw_days!r}") from None
        if default_days < 1:
            raise BadRequest("days 必须大于 0")
    return resolve_range(
        args.get("startTime") or args.get("start"),
        args.get("endTime") or args.get("end"),
        default_days=default_days,
        max_days=settings.max_range_days,
    )


def window_key(window: TimeRange) -> tuple[str, str]:
    formatted = window.formatted()
    return formatted["startTime"], formatted["endTime"]


def parse_fields(request: Request) -> tuple[str, ...]:
    """``?fields=a,b`` keeps only those top-level keys of ``data``.

    Absent or empty means "return everything". Names are matched exactly and
    only one level deep, so ``?fields=summary`` works but ``summary.totalCredits``
    does not — the upstream has no projection support, the trimming happens here.
    """
    raw = request.get_args().get("fields")
    if not raw:
        return ()
    names = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not names:
        raise BadRequest("fields 不能为空，例如 ?fields=summary,modelSummaryList")
    for name in names:
        if len(name) > 64 or "." in name:
            raise BadRequest(f"fields 只支持 data 下的一级字段名，收到 {name!r}")
    return names


def select_fields(data: Any, fields: tuple[str, ...]) -> tuple[Any, list[str]]:
    """Narrow ``data`` to ``fields``; also report names that matched nothing."""
    if not fields:
        return data, []
    if not isinstance(data, dict):
        return data, list(fields)
    return {name: data[name] for name in fields if name in data}, [name for name in fields if name not in data]


def select_sections(fields: tuple[str, ...], vocabulary: Any) -> tuple[str, ...]:
    """Same idea as :func:`select_fields`, for the fan-out endpoints' section names."""
    names = tuple(vocabulary)
    if not fields:
        return names
    wanted = tuple(name for name in names if name in fields)
    if not wanted:
        raise BadRequest(f"fields 只能是 {' / '.join(names)}")
    return wanted


def parse_max_age(request: Request, *, default: float | None = None) -> float | None:
    """``?maxAge=60`` refuses cache entries older than 60 seconds (``0`` = always reload)."""
    raw = request.get_args().get("maxAge")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise BadRequest(f"maxAge 必须是秒数，收到 {raw!r}") from None
    if value < 0:
        raise BadRequest("maxAge 不能为负数")
    return value
