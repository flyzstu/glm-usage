"""Unit tests for the upstream client: encoding, error mapping and retries."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import ACTIVITY_PAYLOAD, QUOTA_PAYLOAD, USAGE_PAYLOAD, make_settings

from glm_usage.client import (
    ACCOUNT_REPORT_PATH,
    ACTIVITY_PATH,
    AUTO_RENEW_CLOSED_PATH,
    CUSTOMER_INFO_PATH,
    PERFORMANCE_PATH,
    QUOTA_PATH,
    SUBSCRIPTION_LIST_PATH,
    TOKEN_MAGNITUDE_PATH,
    USAGE_DETAIL_PATH,
    BigModelClient,
    InvalidTokenError,
    UpstreamTimeout,
    UpstreamUnavailable,
    encode_query,
    retry_after_seconds,
)
from glm_usage.metrics import Metrics
from glm_usage.timerange import TimeRange, parse_time

WINDOW = TimeRange(start=parse_time("2026-09-06 00:00:00"), end=parse_time("2026-09-12 23:59:59"))


def client_for(handler, **overrides) -> tuple[BigModelClient, Metrics]:
    metrics = Metrics()
    settings = make_settings(**overrides)
    return BigModelClient(settings, metrics=metrics, transport=httpx.MockTransport(handler)), metrics


def responder(payloads: dict[str, dict], calls: list[httpx.Request]):
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = payloads.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={"message": "unknown"})
        return httpx.Response(200, json=payload)

    return handler


def test_encode_query_uses_percent_encoded_spaces() -> None:
    query = encode_query(
        {"startTime": "2026-09-06 00:00:00", "endTime": "2026-09-12 23:59:59", "type": 1, "usageType": "MODEL"}
    )
    assert query == (
        "startTime=2026-09-06%2000:00:00&endTime=2026-09-12%2023:59:59&type=1&usageType=MODEL"
    )
    assert "+" not in query


async def test_quota_returns_data_and_sends_authorization() -> None:
    calls: list[httpx.Request] = []
    client, metrics = client_for(responder({QUOTA_PATH: QUOTA_PAYLOAD}, calls))

    async with client:
        data = await client.quota("jwt-value")

    assert data["level"] == "lite"
    assert calls[0].headers["authorization"] == "jwt-value"
    assert calls[0].url.path == QUOTA_PATH
    assert str(calls[0].url) == f"https://bigmodel.cn{QUOTA_PATH}"
    upstream_stats = metrics.snapshot()["upstream"]
    assert (upstream_stats["calls"], upstream_stats["errors"]) == (1, 0)
    assert upstream_stats["avgLatencyMs"] >= 0


async def test_usage_detail_sends_documented_parameters() -> None:
    calls: list[httpx.Request] = []
    client, _ = client_for(responder({USAGE_DETAIL_PATH: USAGE_PAYLOAD}, calls))

    async with client:
        await client.usage_detail("t", WINDOW, usage_type="MCP")

    assert calls[0].url.query.decode() == (
        "startTime=2026-09-06%2000:00:00&endTime=2026-09-12%2023:59:59&type=1&usageType=MCP"
    )


async def test_activity_returns_the_data_object() -> None:
    calls: list[httpx.Request] = []
    client, _ = client_for(responder({ACTIVITY_PATH: ACTIVITY_PAYLOAD}, calls))

    async with client:
        data = await client.activity("t", WINDOW)

    assert data["summary"]["currentStreakDays"] == 3


async def test_http_401_maps_to_invalid_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client, _ = client_for(handler)
    async with client:
        with pytest.raises(InvalidTokenError):
            await client.quota("expired")


@pytest.mark.parametrize(
    "payload",
    [
        # Both payloads are verbatim responses captured from bigmodel.cn:
        # an invalid token, and a request with no Authorization header.
        {"code": 401, "msg": "令牌已过期或验证不正确", "success": False},
        {
            "code": 1001,
            "msg": "Header中未收到Authorization参数，无法进行身份验证。",
            "success": False,
        },
    ],
)
async def test_live_auth_error_bodies_map_to_invalid_token(payload: dict) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, metrics = client_for(handler, retries=2)
    async with client:
        with pytest.raises(InvalidTokenError) as excinfo:
            await client.quota("test-token")

    assert excinfo.value.status == 401
    assert excinfo.value.code == payload["code"]
    assert payload["msg"] in str(excinfo.value)
    # Deterministic failure: no retries, and it is still counted as an error.
    upstream_stats = metrics.snapshot()["upstream"]
    assert (upstream_stats["calls"], upstream_stats["errors"]) == (1, 1)


async def test_business_error_carries_the_upstream_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 4005, "success": False, "message": "参数不合法"})

    client, _ = client_for(handler)
    async with client:
        with pytest.raises(UpstreamUnavailable) as excinfo:
            await client.quota("t")

    assert excinfo.value.code == 4005
    assert "参数不合法" in str(excinfo.value)


async def test_non_json_body_is_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    client, _ = client_for(handler)
    async with client:
        with pytest.raises(UpstreamUnavailable, match="非 JSON"):
            await client.quota("t")


async def test_server_errors_are_retried_then_raised() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"message": "boom"})

    client, metrics = client_for(handler, retries=2)
    async with client:
        with pytest.raises(UpstreamUnavailable):
            await client.quota("t")

    assert len(calls) == 3
    upstream_stats = metrics.snapshot()["upstream"]
    assert (upstream_stats["calls"], upstream_stats["errors"]) == (3, 3)


async def test_transient_failure_recovers_on_retry() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json=QUOTA_PAYLOAD)

    client, _ = client_for(handler, retries=2)
    async with client:
        data = await client.quota("t")

    assert data["level"] == "lite"
    assert len(calls) == 2


async def test_timeouts_map_to_504() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client, _ = client_for(handler, retries=0)
    async with client:
        with pytest.raises(UpstreamTimeout) as excinfo:
            await client.quota("t")

    assert excinfo.value.status == 504


async def test_biz_endpoints_pass_through_non_object_data() -> None:
    """`data` is a list for subscriptions, a bare boolean for the renew flag."""
    calls: list[httpx.Request] = []
    payloads = {
        SUBSCRIPTION_LIST_PATH: {"code": 200, "success": True, "data": [{"productName": "Lite"}]},
        AUTO_RENEW_CLOSED_PATH: {"code": 200, "success": True, "data": False},
        TOKEN_MAGNITUDE_PATH: {"code": 200, "success": True, "data": {"tokens": 5000000}},
    }
    client, _ = client_for(responder(payloads, calls))

    async with client:
        subscriptions = await client.subscriptions("t")
        auto_renew_closed = await client.auto_renew_closed("t")
        magnitude = await client.token_magnitude("t", "product-047")

    assert subscriptions == [{"productName": "Lite"}]
    assert auto_renew_closed is False
    assert magnitude == {"tokens": 5000000}
    assert parse_qs(calls[0].url.query.decode()) == {"pageSize": ["9999"], "pageNum": ["1"]}
    assert parse_qs(calls[2].url.query.decode()) == {"productId": ["product-047"]}


async def test_account_report_and_customer_info_hit_their_paths() -> None:
    calls: list[httpx.Request] = []
    payloads = {
        ACCOUNT_REPORT_PATH: {"code": 200, "success": True, "data": {"balance": "12.34"}},
        CUSTOMER_INFO_PATH: {"code": 200, "success": True, "data": {"id": "cust-1"}},
    }
    client, _ = client_for(responder(payloads, calls))

    async with client:
        report = await client.account_report("t")
        customer = await client.customer_info("t")

    assert report == {"balance": "12.34"}
    assert customer == {"id": "cust-1"}
    assert calls[0].url.path == ACCOUNT_REPORT_PATH
    assert calls[0].url.query == b""


async def test_performance_omits_the_plan_type_parameter() -> None:
    calls: list[httpx.Request] = []
    client, _ = client_for(responder({PERFORMANCE_PATH: {"code": 200, "success": True, "data": {"x_time": []}}}, calls))

    async with client:
        await client.performance("t", WINDOW)

    assert calls[0].url.query.decode() == "startTime=2026-09-06%2000:00:00&endTime=2026-09-12%2023:59:59"


@pytest.mark.parametrize(
    "header,expected",
    [("7", 7.0), ("0.5", 0.5), ("0", 0.0), ("garbage", None)],
)
def test_retry_after_parses_delay_seconds(header: str, expected: float | None) -> None:
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": header})) == expected


def test_retry_after_parses_an_http_date() -> None:
    moment = datetime.now(timezone.utc) + timedelta(seconds=4)
    response = httpx.Response(429, headers={"retry-after": format_datetime(moment, usegmt=True)})

    assert retry_after_seconds(response) == pytest.approx(4, abs=1)


def test_retry_after_absent() -> None:
    assert retry_after_seconds(httpx.Response(429)) is None


async def test_rate_limit_hint_replaces_the_backoff_delay() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.01"}, json={"message": "slow down"})
        return httpx.Response(200, json=QUOTA_PAYLOAD)

    # A 30s backoff would make this test hang: passing means the hint was used.
    client, _ = client_for(handler, retries=1, retry_backoff=30.0)
    started = time.perf_counter()
    async with client:
        data = await client.quota("t")
    elapsed = time.perf_counter() - started

    assert data["level"] == "lite"
    assert len(calls) == 2
    assert elapsed < 1.0


async def test_a_long_rate_limit_hint_fails_fast() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "120"}, json={"message": "slow down"})

    client, _ = client_for(handler, retries=3)
    async with client:
        with pytest.raises(UpstreamUnavailable) as excinfo:
            await client.quota("t")

    # Holding the request open for two minutes is worse than saying "come back later".
    assert len(calls) == 1
    assert "120" in str(excinfo.value)


async def test_client_must_be_started() -> None:
    client, _ = client_for(responder({}, []))
    with pytest.raises(RuntimeError, match="start"):
        await client.quota("t")


async def test_stale_pool_is_rebuilt_for_a_new_loop() -> None:
    calls: list[httpx.Request] = []
    client, _ = client_for(responder({QUOTA_PATH: QUOTA_PAYLOAD}, calls))

    async with client:
        await client.quota("t")

    # Sanic's test client runs every request in a fresh loop, so re-entering
    # start() must not reuse the pool bound to the previous one.
    async with client:
        data = await client.quota("t")

    assert data["level"] == "lite"
    assert len(calls) == 2
