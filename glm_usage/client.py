"""Async client for the bigmodel.cn usage endpoints.

Query strings are percent-encoded here rather than by httpx: httpx turns spaces
into ``+`` while the upstream service is only known to accept ``%20``
(``startTime=2026-09-06%2000:00:00``). Pre-encoded queries are passed through
untouched, so the wire format matches the one verified by hand.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .metrics import Metrics
from .timerange import TimeRange

PLAN_TYPE = "1"
USAGE_TYPES = ("MODEL", "MCP")

# Waiting longer than this inside a request is worse than failing: tell the
# caller to come back later instead of holding the connection open.
MAX_RETRY_AFTER = 5.0

# Auth failures arrive as HTTP 200 with an error body. Both codes below were
# observed against the live service: 1001 = no Authorization header at all,
# 401 = header present but the token is expired/invalid.
AUTH_ERROR_CODES = frozenset({401, 1001})

QUOTA_PATH = "/api/monitor/usage/quota/limit"
USAGE_DETAIL_PATH = "/api/monitor/credit-usage/usage-detail"
ACTIVITY_PATH = "/api/monitor/credit-usage/activity"
PERFORMANCE_PATH = "/api/monitor/usage/model-performance-day"

SUBSCRIPTION_LIST_PATH = "/api/biz/subscription/list"
AUTO_RENEW_CLOSED_PATH = "/api/biz/subscription/v1-coding-plan-auto-renew-closed-by-system"
PACKAGE_RESET_PATH = "/api/biz/customer-package-reset/list"
TOKEN_MAGNITUDE_PATH = "/api/biz/customer/getTokenMagnitude"
ACCOUNT_REPORT_PATH = "/api/biz/account/query-customer-account-report"
CUSTOMER_INFO_PATH = "/api/biz/customer/getCustomerInfo"

DEFAULT_PRODUCT_ID = "product-005"


class BigModelError(Exception):
    """Any failure while talking to the upstream service."""

    status = 502
    kind = "upstream_error"
    # A failed refresh normally falls back to the cache's expired-but-readable entry.
    # Errors that mean "this request was never legitimate" opt out (see the auth ones
    # below), because serving an old number would hide the problem instead of showing it.
    allow_stale = True

    def __init__(self, message: str, *, code: Any = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        if status is not None:
            self.status = status

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.kind, "message": self.message}
        if self.code is not None:
            payload["code"] = self.code
        return payload


class MissingTokenError(BigModelError):
    status = 401
    kind = "missing_token"
    allow_stale = False


class InvalidTokenError(BigModelError):
    status = 401
    kind = "invalid_token"
    allow_stale = False


class UpstreamUnavailable(BigModelError):
    status = 502
    kind = "upstream_unavailable"


class UpstreamTimeout(BigModelError):
    status = 504
    kind = "upstream_timeout"

    def __init__(self, message: str, *, code: Any = None) -> None:
        super().__init__(message, code=code)


def encode_query(params: dict[str, Any]) -> str:
    """Encode ``params`` the way the bigmodel web client does (space as ``%20``)."""
    return "&".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe=':')}" for key, value in params.items()
    )


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse ``Retry-After`` (delay-seconds or HTTP-date) into seconds."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


class BigModelClient:
    """Thin, pooled wrapper around the three usage endpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        metrics: Metrics | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._metrics = metrics
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Create the pooled client for the running loop.

        A pool is bound to the loop it was opened on, so if ``start`` is called
        again from a different loop the stale pool is dropped and rebuilt.
        """
        loop = asyncio.get_running_loop()
        if self._client is not None:
            if self._loop is loop:
                return
            self._client = None
        self._client = httpx.AsyncClient(
            base_url=self._settings.base_url,
            transport=self._transport,
            timeout=httpx.Timeout(self._settings.request_timeout, connect=self._settings.connect_timeout),
            limits=httpx.Limits(
                max_connections=self._settings.max_connections,
                max_keepalive_connections=self._settings.max_keepalive_connections,
                keepalive_expiry=30.0,
            ),
            headers={
                "Accept": "application/json",
                "User-Agent": f"glm-usage/{self._settings.version}",
            },
            follow_redirects=False,
        )
        self._loop = loop

    async def close(self) -> None:
        client, self._client, self._loop = self._client, None, None
        if client is None:
            return
        with suppress(RuntimeError):  # the owning loop is already gone
            await client.aclose()

    async def __aenter__(self) -> BigModelClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def quota(self, token: str) -> dict[str, Any]:
        """5-hour window and weekly credit limits."""
        return await self._get(QUOTA_PATH, {}, token)

    async def usage_detail(
        self,
        token: str,
        window: TimeRange,
        *,
        usage_type: str = "MODEL",
    ) -> dict[str, Any]:
        """Per-model credit/token consumption inside ``window``."""
        return await self._get(
            USAGE_DETAIL_PATH,
            {**window.formatted(), "type": PLAN_TYPE, "usageType": usage_type},
            token,
        )

    async def activity(self, token: str, window: TimeRange) -> dict[str, Any]:
        """Cumulative tokens, streaks and the per-day series."""
        return await self._get(
            ACTIVITY_PATH,
            {**window.formatted(), "type": PLAN_TYPE},
            token,
        )

    async def performance(self, token: str, window: TimeRange) -> dict[str, Any]:
        """Daily decode speed and success rate for lite vs pro/max."""
        return await self._get(PERFORMANCE_PATH, window.formatted(), token)

    async def subscriptions(self, token: str) -> Any:
        """Plan orders (name, status, validity, renewal, price)."""
        return await self._get(SUBSCRIPTION_LIST_PATH, {"pageSize": 9999, "pageNum": 1}, token)

    async def package_resets(self, token: str) -> Any:
        """Recorded five-hour / weekly quota resets."""
        return await self._get(PACKAGE_RESET_PATH, {"targetType": "PERSONAL"}, token)

    async def auto_renew_closed(self, token: str) -> Any:
        """Whether auto-renewal was switched off by the platform."""
        return await self._get(AUTO_RENEW_CLOSED_PATH, {}, token)

    async def account_report(self, token: str) -> Any:
        """Balance, top-ups, grants and cumulative spend."""
        return await self._get(ACCOUNT_REPORT_PATH, {}, token)

    async def customer_info(self, token: str) -> Any:
        """Account basics (id, email, organisation)."""
        return await self._get(CUSTOMER_INFO_PATH, {}, token)

    async def token_magnitude(self, token: str, product_id: str = DEFAULT_PRODUCT_ID) -> Any:
        """Trial-card token allowance for ``product_id``."""
        return await self._get(TOKEN_MAGNITUDE_PATH, {"productId": product_id}, token)

    async def _get(self, path: str, params: dict[str, Any], token: str) -> Any:
        if self._client is None:
            raise RuntimeError("client.start() must be awaited before use")
        url = f"{path}?{encode_query(params)}" if params else path
        headers = {"Authorization": token}
        attempts = max(self._settings.retries, 0) + 1
        failure: BigModelError | None = None

        for attempt in range(attempts):
            started = time.perf_counter()
            retryable = True
            delay: float | None = None
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.TimeoutException:
                failure = UpstreamTimeout(f"bigmodel 请求超时（{self._settings.request_timeout:g}s）")
            except httpx.HTTPError as exc:
                failure = UpstreamUnavailable(f"无法连接 bigmodel：{exc.__class__.__name__}")
            else:
                error = self._status_error(response)
                if error is None:
                    try:
                        data = self._decode(response)
                    except BigModelError as exc:
                        self._observe(started, error=True, failure=exc)
                        raise
                    self._observe(started, error=False)
                    return data
                failure = error
                retryable = isinstance(error, (UpstreamUnavailable, UpstreamTimeout))
                hint = retry_after_seconds(response)
                if hint is not None:
                    if hint > MAX_RETRY_AFTER:
                        self._observe(started, error=True, failure=error)
                        raise UpstreamUnavailable(
                            f"bigmodel 触发了限流（HTTP {response.status_code}），"
                            f"要求 {hint:.0f} 秒后重试，本次不再重试",
                            code=error.code,
                        )
                    failure = UpstreamUnavailable(f"{error}；上游要求 {hint:.0f} 秒后重试", code=error.code)
                    delay = hint

            self._observe(started, error=True, failure=failure)
            if not retryable or attempt + 1 >= attempts:
                break
            await asyncio.sleep(delay if delay is not None else self._backoff(attempt))

        raise failure if failure is not None else UpstreamUnavailable("bigmodel 请求失败")

    def _backoff(self, attempt: int) -> float:
        base = self._settings.retry_backoff * (2**attempt)
        return min(base * (0.5 + random.random()), 5.0)

    def _observe(self, started: float, *, error: bool, failure: BigModelError | None = None) -> None:
        """Counters, plus the upstream's latest auth verdict (reported by ``/healthz``)."""
        if self._metrics is None:
            return
        self._metrics.record_upstream((time.perf_counter() - started) * 1000, error=error)
        if not error:
            # Any call that gets through means the token is good again.
            self._metrics.record_auth_success()
        elif isinstance(failure, (InvalidTokenError, MissingTokenError)):
            self._metrics.record_auth_failure()

    @staticmethod
    def _status_error(response: httpx.Response) -> BigModelError | None:
        status = response.status_code
        if status in (401, 403):
            return InvalidTokenError("bigmodel 返回鉴权失败，token 可能已过期")
        if status == 429:
            return UpstreamUnavailable("bigmodel 触发了限流（429），请稍后重试")
        if status >= 500:
            return UpstreamUnavailable(f"bigmodel 服务异常（HTTP {status}）")
        if status >= 400:
            return UpstreamUnavailable(f"bigmodel 返回 HTTP {status}")
        return None

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamUnavailable("bigmodel 返回了非 JSON 响应") from None
        if not isinstance(payload, dict):
            raise UpstreamUnavailable("bigmodel 返回了非预期的响应结构")

        code = _as_int(payload.get("code"))
        message = payload.get("message") or payload.get("msg") or f"bigmodel 错误码 {code}"
        if code in AUTH_ERROR_CODES:
            raise InvalidTokenError(
                f"token 鉴权失败（code {code}）：{message}。请重新登录 bigmodel.cn 获取新 token",
                code=code,
            )
        if payload.get("success") is False or code not in (None, 200):
            raise UpstreamUnavailable(str(message), code=code)

        # ``data`` is an object for the usage endpoints, but the /api/biz/*
        # endpoints return lists, booleans or nothing at all.
        return payload.get("data", payload)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ACTIVITY_PATH",
    "ACCOUNT_REPORT_PATH",
    "AUTH_ERROR_CODES",
    "AUTO_RENEW_CLOSED_PATH",
    "BigModelClient",
    "BigModelError",
    "CUSTOMER_INFO_PATH",
    "DEFAULT_PRODUCT_ID",
    "InvalidTokenError",
    "MissingTokenError",
    "PACKAGE_RESET_PATH",
    "PERFORMANCE_PATH",
    "PLAN_TYPE",
    "QUOTA_PATH",
    "SUBSCRIPTION_LIST_PATH",
    "TOKEN_MAGNITUDE_PATH",
    "USAGE_DETAIL_PATH",
    "USAGE_TYPES",
    "UpstreamTimeout",
    "UpstreamUnavailable",
    "encode_query",
    "retry_after_seconds",
]
