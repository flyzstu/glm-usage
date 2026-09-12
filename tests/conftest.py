"""Shared fixtures: a fake bigmodel upstream and a Sanic app wired to it."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sanic import Sanic
from sanic_testing.testing import SanicTestClient

from glm_usage.app import create_app
from glm_usage.client import (
    ACCOUNT_REPORT_PATH,
    ACTIVITY_PATH,
    AUTO_RENEW_CLOSED_PATH,
    CUSTOMER_INFO_PATH,
    PACKAGE_RESET_PATH,
    PERFORMANCE_PATH,
    QUOTA_PATH,
    SUBSCRIPTION_LIST_PATH,
    TOKEN_MAGNITUDE_PATH,
    USAGE_DETAIL_PATH,
)
from glm_usage.config import Settings

QUOTA_PAYLOAD: dict[str, Any] = {
    "code": 200,
    "success": True,
    "data": {
        "level": "lite",
        "limits": [
            {
                "type": "CREDIT_LIMIT",
                "unit": 3,
                "number": 5,
                "usage": 2000,
                "currentValue": 37,
                "remaining": 1962,
                "percentage": 1,
                "nextResetTime": 1789195203620,
            },
            {
                "type": "CREDIT_LIMIT",
                "unit": 6,
                "number": 1,
                "usage": 10000,
                "currentValue": 159,
                "remaining": 9841,
                "percentage": 2,
                "nextResetTime": 1789795203620,
            },
        ],
    },
}

USAGE_PAYLOAD: dict[str, Any] = {
    "code": 200,
    "success": True,
    "data": {
        "granularity": "DAY",
        "timezone": "Asia/Shanghai",
        "summary": {
            "cacheHitRate": {"value": "0.9221"},
            "totalCredits": {"value": "159.2652"},
            "averageDailyCredits": {"value": "22.7522"},
            "offPeakUsageRate": {"value": "0.9716"},
        },
        "totalUsage": {"totalTokens": 4845999, "totalCredits": "159.2652"},
        "modelSummaryList": [
            {"modelCode": "glm-5.3", "modelName": "GLM-5.3", "totalTokens": 107949, "totalCredits": "33.9747"},
            {
                "modelCode": "glm-5.3-flash",
                "modelName": "GLM-5.3-Flash",
                "totalTokens": 4738050,
                "totalCredits": "125.2905",
            },
        ],
        "modelDataList": [
            {
                "modelCode": "glm-5.3",
                "totalTokensUsage": [100, 200],
                "totalCreditsUsage": [1.5, 2.5],
            }
        ],
        "xTime": ["2026-09-06", "2026-09-07"],
    },
}

ACTIVITY_PAYLOAD: dict[str, Any] = {
    "code": 200,
    "success": True,
    "data": {
        "summary": {
            "totalTokens": 4845999,
            "peakDailyTokens": 900000,
            "peakDailyTokensDate": "2026-09-07",
            "totalUsageDurationMs": 3600000,
            "currentStreakDays": 3,
            "longestStreakDays": 7,
        },
        # totalCredits is a string on the wire, even though it holds a number.
        "series": [{"date": "2026-09-06", "totalCredits": "10.0000", "totalTokens": 100, "mcpCalls": 2}],
    },
}

AUTH_ERROR_PAYLOAD = {"code": 1001, "success": False, "message": "Authentication parameter not received in Header"}

PERFORMANCE_PAYLOAD: dict[str, Any] = {
    "code": 200,
    "success": True,
    "data": {
        "x_time": ["2026-09-05", "2026-09-06"],
        "liteDecodeSpeed": [94.3, 89.33],
        "proMaxDecodeSpeed": [111.66, 114.33],
        "liteSuccessRate": [0.9994, 0.9992],
        "proMaxSuccessRate": [0.9994, 0.9994],
    },
}

# The /api/biz/* endpoints do not all return an object in ``data``: the
# subscription list is an array and the auto-renew flag is a bare boolean.
SUBSCRIPTION_LIST_PAYLOAD: dict[str, Any] = {
    "code": 200,
    "success": True,
    "data": [
        {
            "productName": "GLM Coding Lite",
            "status": "VALID",
            "valid": "2026-10-01 00:00:00",
            "autoRenew": True,
            "actualPrice": "20.00",
            "renewPrice": "20.00",
            "billingCycle": "MONTH",
            "inCurrentPeriod": True,
            "nextRenewTime": "2026-10-01 00:00:00",
        }
    ],
}

BIZ_PAYLOADS: dict[str, dict[str, Any]] = {
    SUBSCRIPTION_LIST_PATH: SUBSCRIPTION_LIST_PAYLOAD,
    AUTO_RENEW_CLOSED_PATH: {"code": 200, "success": True, "data": False},
    PACKAGE_RESET_PATH: {"code": 200, "success": True, "data": {"fiveHourResets": [], "weekResets": []}},
    ACCOUNT_REPORT_PATH: {"code": 200, "success": True, "data": {"balance": "12.34", "totalSpend": "159.27"}},
    CUSTOMER_INFO_PATH: {"code": 200, "success": True, "data": {"id": "cust-1", "email": "dev@example.com"}},
    TOKEN_MAGNITUDE_PATH: {"code": 200, "success": True, "data": {"tokens": 5000000}},
}


class FakeUpstream:
    """In-memory stand-in for bigmodel.cn with call bookkeeping."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_paths: set[str] = set()
        self.auth_error_paths: set[str] = set()
        self.delay = 0.0
        self.inflight = 0
        self.max_inflight = 0
        self.payloads: dict[str, dict[str, Any]] = {
            QUOTA_PATH: QUOTA_PAYLOAD,
            USAGE_DETAIL_PATH: USAGE_PAYLOAD,
            ACTIVITY_PATH: ACTIVITY_PAYLOAD,
            PERFORMANCE_PATH: PERFORMANCE_PAYLOAD,
            **BIZ_PAYLOADS,
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            path = request.url.path
            if path in self.auth_error_paths:
                return httpx.Response(200, json=AUTH_ERROR_PAYLOAD)
            if path in self.fail_paths:
                return httpx.Response(500, json={"message": "upstream exploded"})
            payload = self.payloads.get(path)
            if payload is None:
                return httpx.Response(404, json={"message": "unknown path"})
            return httpx.Response(200, json=payload)
        finally:
            self.inflight -= 1

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def count(self, path: str) -> int:
        return sum(1 for request in self.requests if request.url.path == path)

    def tokens_seen(self) -> list[str | None]:
        return [request.headers.get("authorization") for request in self.requests]

    def query_of(self, index: int = 0) -> str:
        return self.requests[index].url.query.decode()


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "token": "test-token",
        "access_log": False,
        "retry_backoff": 0.0,
        "quota_ttl": 60.0,
        "usage_ttl": 60.0,
        "activity_ttl": 60.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def build_app(upstream: FakeUpstream | None = None, **overrides: Any) -> Sanic:
    """Every app needs a unique name: Sanic refuses duplicate registrations."""
    name = f"glm_usage_test_{uuid.uuid4().hex[:8]}"
    transport = upstream.transport if upstream is not None else None
    return create_app(make_settings(**overrides), name=name, transport=transport)


@pytest.fixture
def app(upstream: FakeUpstream) -> Sanic:
    return build_app(upstream)


@pytest.fixture
def client(app: Sanic) -> SanicTestClient:
    return SanicTestClient(app, port=None)
