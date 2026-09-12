"""End-to-end tests against the real Sanic request cycle."""

from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import parse_qs

import pytest
from conftest import FakeUpstream, build_app
from sanic import Sanic
from sanic_testing.testing import SanicTestClient

from glm_usage.client import (
    ACCOUNT_REPORT_PATH,
    ACTIVITY_PATH,
    CUSTOMER_INFO_PATH,
    PERFORMANCE_PATH,
    QUOTA_PATH,
    SUBSCRIPTION_LIST_PATH,
    TOKEN_MAGNITUDE_PATH,
    USAGE_DETAIL_PATH,
    encode_query,
)
from glm_usage.timerange import SHANGHAI, format_time, now


@pytest.fixture
def client_for(upstream: FakeUpstream):
    """Factory so a test can build extra apps (e.g. tokenless ones)."""
    def _make(**overrides) -> SanicTestClient:
        app: Sanic = build_app(upstream, **overrides)
        return SanicTestClient(app, port=None)

    return _make


def test_healthz_reports_missing_token(client_for) -> None:
    client = client_for(token=None)
    _, response = client.get("/healthz")
    assert response.status == 200
    # 200 + degraded：不要因为没配 token 就让容器反复重启，但状态要如实反映
    assert response.json["status"] == "degraded"
    assert response.json["token"] == {"configured": False, "source": "none"}


def test_healthz_is_ok_with_a_token(client: SanicTestClient) -> None:
    _, response = client.get("/healthz")
    assert response.json["status"] == "ok"
    assert response.json["upstreamAuth"] == {"rejected": False, "rejections": 0}


def test_healthz_flags_a_token_the_upstream_rejects(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.auth_error_paths.add(QUOTA_PATH)
    assert client.get("/api/v1/quota")[1].status == 401

    _, response = client.get("/healthz")
    # 仍然 200：token 过期不该让编排器反复重启容器，但 body 要说实话
    assert response.status == 200
    assert response.json["status"] == "degraded"
    assert response.json["upstreamAuth"]["rejected"] is True
    assert response.json["upstreamAuth"]["rejections"] == 1
    assert response.json["upstreamAuth"]["rejectedAgoSeconds"] >= 0


def test_healthz_recovers_once_an_upstream_call_succeeds(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.auth_error_paths.add(QUOTA_PATH)
    assert client.get("/api/v1/quota")[1].status == 401
    assert client.get("/healthz")[1].json["status"] == "degraded"

    upstream.auth_error_paths.clear()
    assert client.get("/api/v1/usage")[1].status == 200

    _, response = client.get("/healthz")
    assert response.json["status"] == "ok"
    # 累计次数保留，只清"最近一次判定"
    assert response.json["upstreamAuth"] == {"rejected": False, "rejections": 1}


def test_quota_is_cached_between_requests(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, first = client.get("/api/v1/quota")
    _, second = client.get("/api/v1/quota")

    assert first.status == 200
    assert first.json["data"]["level"] == "lite"
    assert len(first.json["data"]["limits"]) == 2
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert upstream.count(QUOTA_PATH) == 1


def test_missing_token_is_401(client_for) -> None:
    _, response = client_for(token=None).get("/api/v1/quota")
    assert response.status == 401
    assert response.json["error"]["type"] == "missing_token"


def test_expired_token_is_401(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.auth_error_paths.add(QUOTA_PATH)
    _, response = client.get("/api/v1/quota")
    assert response.status == 401
    assert response.json["error"]["type"] == "invalid_token"
    assert "1001" in response.json["error"]["message"]


def test_authorization_header_overrides_configured_token(
    client: SanicTestClient, upstream: FakeUpstream
) -> None:
    client.get("/api/v1/quota", headers={"Authorization": "token-a"})
    client.get("/api/v1/quota", headers={"Authorization": "Bearer token-b"})
    client.get("/api/v1/quota", headers={"Authorization": "token-a"})

    # The third call is a cache hit: a different token never reuses another's entry.
    assert upstream.tokens_seen() == ["token-a", "token-b"]


def test_refresh_parameter_bypasses_cache(client: SanicTestClient, upstream: FakeUpstream) -> None:
    client.get("/api/v1/quota")
    _, response = client.get("/api/v1/quota?refresh=1")
    assert response.headers["x-cache"] == "MISS"
    assert upstream.count(QUOTA_PATH) == 2


def test_stale_cache_is_served_when_upstream_fails(client_for, upstream: FakeUpstream) -> None:
    client = client_for(quota_ttl=0.05, stale_ttl=30.0, retries=0)
    assert client.get("/api/v1/quota")[1].status == 200

    time.sleep(0.1)
    upstream.fail_paths.add(QUOTA_PATH)

    _, response = client.get("/api/v1/quota")
    assert response.status == 200
    assert response.headers["x-cache"] == "STALE"
    assert response.json["data"]["level"] == "lite"


def test_expired_token_is_not_papered_over_with_stale_data(client_for, upstream: FakeUpstream) -> None:
    """上游挂了可以拿旧值顶一会儿，token 过期不行——那是必须让调用方看见的状态。"""
    client = client_for(quota_ttl=0.05, stale_ttl=30.0, retries=0)
    assert client.get("/api/v1/quota")[1].status == 200

    time.sleep(0.1)
    upstream.auth_error_paths.add(QUOTA_PATH)

    _, response = client.get("/api/v1/quota")
    assert response.status == 401
    assert response.json["error"]["type"] == "invalid_token"


def test_usage_matches_documented_query_string(client: SanicTestClient, upstream: FakeUpstream) -> None:
    end = now().replace(hour=23, minute=59, second=59, microsecond=0) - timedelta(days=1)
    start = (end - timedelta(days=6)).replace(hour=0, minute=0, second=0)
    start_text, end_text = format_time(start), format_time(end)

    _, response = client.get(f"/api/v1/usage?startTime={start_text}&endTime={end_text}")

    assert response.status == 200
    assert response.json["meta"]["usageType"] == "MODEL"
    assert response.json["data"]["modelSummaryList"][0]["modelName"] == "GLM-5.3"
    assert upstream.query_of(0) == (
        f"startTime={start_text.replace(' ', '%20')}&endTime={end_text.replace(' ', '%20')}"
        "&type=1&usageType=MODEL"
    )


def test_usage_accepts_iso_and_percent_encoded_ranges(client: SanicTestClient, upstream: FakeUpstream) -> None:
    end = now().replace(hour=23, minute=59, second=59, microsecond=0) - timedelta(days=1)
    start = (end - timedelta(days=6)).replace(hour=0, minute=0, second=0)
    query = encode_query({"startTime": format_time(start), "endTime": format_time(end)})

    _, response = client.get(f"/api/v1/usage?{query}")
    assert response.status == 200
    assert response.json["meta"]["range"]["startTime"] == start.isoformat()


def test_usage_days_shortcut_aligns_to_midnight(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/usage?days=3")
    assert response.status == 200
    assert response.json["meta"]["range"]["startTime"].endswith("T00:00:00+08:00")
    assert 3.0 <= response.json["meta"]["range"]["days"] < 4.0


@pytest.mark.parametrize(
    "query,message",
    [
        ("?usageType=UNKNOWN", "usageType"),
        ("?days=0", "days"),
        ("?days=abc", "days"),
        ("?startTime=not-a-date", "无法解析"),
        ("?startTime=2026-09-10 00:00:00&endTime=2026-01-01 00:00:00", "startTime 必须早于"),
        ("?startTime=2026-09-10 00:00:00&endTime=2030-09-10 00:00:00", "不能超过当前时间"),
        (f"?startTime=2020-01-01 00:00:00&endTime={format_time(now() - timedelta(days=1))}", "时间跨度不能超过"),
    ],
)
def test_bad_queries_are_rejected(client: SanicTestClient, query: str, message: str) -> None:
    _, response = client.get(f"/api/v1/usage{query}")
    assert response.status == 400
    assert response.json["error"]["type"] == "invalid_request"
    assert message in response.json["error"]["message"]


def test_activity_endpoint(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/activity?days=30")
    assert response.status == 200
    assert response.json["data"]["summary"]["currentStreakDays"] == 3
    assert "type=1" in upstream.query_of(0)


def test_overview_returns_all_sections(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/overview")
    assert response.status == 200
    assert set(response.json["data"]) == {"quota", "usage", "activity", "performance"}
    assert response.json["meta"]["errors"] is None
    assert set(response.json["meta"]["cacheState"]) == {"quota", "usage", "activity", "performance"}
    assert upstream.count(QUOTA_PATH) == 1
    assert upstream.count(USAGE_DETAIL_PATH) == 1
    assert upstream.count(ACTIVITY_PATH) == 1
    assert upstream.count(PERFORMANCE_PATH) == 1


def test_overview_tolerates_partial_upstream_failure(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.fail_paths.add(ACTIVITY_PATH)
    _, response = client.get("/api/v1/overview")

    assert response.status == 200
    assert set(response.json["data"]) == {"quota", "usage", "performance"}
    assert response.json["meta"]["errors"]["activity"]["type"] == "upstream_unavailable"


def test_overview_reports_502_when_everything_fails(client_for, upstream: FakeUpstream) -> None:
    client = client_for(retries=0)
    upstream.fail_paths.update({QUOTA_PATH, USAGE_DETAIL_PATH, ACTIVITY_PATH, PERFORMANCE_PATH})

    _, response = client.get("/api/v1/overview")
    assert response.status == 502
    assert response.json["data"] == {}
    assert set(response.json["meta"]["errors"]) == {"quota", "usage", "activity", "performance"}


def test_api_key_guard(client_for) -> None:
    client = client_for(api_key="s3cret")

    # 没带 key → 403；带了但不对 → 401
    assert client.get("/api/v1/quota")[1].status == 403
    assert client.get("/api/v1/quota", headers={"X-API-Key": "wrong"})[1].status == 401
    assert client.get("/api/v1/quota", headers={"X-API-Key": "s3cret"})[1].status == 200
    assert client.get("/healthz")[1].status == 200


def test_api_key_rejection_is_opaque(client_for) -> None:
    """拒绝时不回显认证方式（header 名、?key=），免得给扫描器当路标。"""
    client = client_for(api_key="s3cret")

    _, missing = client.get("/api/v1/quota")
    _, wrong = client.get("/api/v1/quota", headers={"X-API-Key": "wrong"})

    assert missing.status == 403
    assert missing.json["error"]["type"] == "forbidden"
    assert wrong.status == 401
    assert wrong.json["error"]["type"] == "unauthorized"

    for response in (missing, wrong):
        text = str(response.json).lower()
        for leak in ("api-key", "api_key", "x-api", "?key", "header"):
            assert leak not in text


def test_metrics_endpoint(client: SanicTestClient) -> None:
    client.get("/api/v1/quota")
    client.get("/api/v1/quota")
    _, response = client.get("/api/v1/metrics")

    assert response.status == 200
    data = response.json["data"]
    assert data["cache"]["MISS"] == 1
    assert data["cache"]["HIT"] == 1
    assert data["upstream"]["calls"] == 1
    assert data["caches"]["quota"]["entries"] == 1


def test_performance_endpoint_sends_only_the_window(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/performance?days=7")

    assert response.status == 200
    assert response.json["data"]["liteDecodeSpeed"] == [94.3, 89.33]
    assert response.json["data"]["x_time"] == ["2026-09-05", "2026-09-06"]
    # Unlike the credit-usage endpoints this one takes no `type` parameter.
    assert set(parse_qs(upstream.query_of(0))) == {"startTime", "endTime"}


def test_account_aggregates_every_biz_endpoint(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/account")

    assert response.status == 200
    data = response.json["data"]
    assert set(data) == {"subscription", "autoRenewClosed", "quotaResets", "balance", "customer", "trialTokens"}
    # `data` is a list here, a bool there: both must survive the decode step.
    assert data["subscription"][0]["productName"] == "GLM Coding Lite"
    assert data["autoRenewClosed"] is False
    assert data["quotaResets"] == {"fiveHourResets": [], "weekResets": []}
    assert data["trialTokens"]["tokens"] == 5000000
    assert response.json["meta"]["productId"] == "product-005"
    assert response.json["meta"]["errors"] is None
    assert upstream.count(SUBSCRIPTION_LIST_PATH) == 1
    assert upstream.count(ACCOUNT_REPORT_PATH) == 1


def test_account_accepts_a_product_id_override(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/account?productId=product-047")

    assert response.status == 200
    assert response.json["meta"]["productId"] == "product-047"
    magnitude = [request for request in upstream.requests if request.url.path == TOKEN_MAGNITUDE_PATH]
    assert parse_qs(magnitude[0].url.query.decode())["productId"] == ["product-047"]


def test_account_is_cached_across_requests(client: SanicTestClient, upstream: FakeUpstream) -> None:
    client.get("/api/v1/account")
    _, second = client.get("/api/v1/account")

    assert second.status == 200
    assert upstream.count(SUBSCRIPTION_LIST_PATH) == 1
    assert set(second.json["meta"]["cacheState"].values()) == {"HIT"}


def test_account_reports_partial_failures(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.fail_paths.add(CUSTOMER_INFO_PATH)
    _, response = client.get("/api/v1/account")

    assert response.status == 200
    assert "customer" not in response.json["data"]
    assert response.json["meta"]["errors"]["customer"]["status"] == 502


def test_fields_trims_data_to_the_requested_keys(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/quota?fields=level")

    assert response.status == 200
    assert response.json["data"] == {"level": "lite"}
    assert response.json["meta"]["fields"] == ["level"]
    assert "ignoredFields" not in response.json["meta"]


def test_fields_keeps_several_keys_and_reports_typos(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/quota?fields=limits,level,limitz")

    assert response.status == 200
    assert set(response.json["data"]) == {"limits", "level"}
    assert response.json["meta"]["ignoredFields"] == ["limitz"]


def test_fields_is_normalised_and_deduplicated(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/quota?fields= level , level ")

    assert response.json["data"] == {"level": "lite"}
    assert response.json["meta"]["fields"] == ["level"]


def test_empty_fields_means_everything(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/quota?fields=")

    assert set(response.json["data"]) == {"level", "limits"}
    assert "fields" not in response.json["meta"]


def test_fields_without_any_match_is_400(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/quota?fields=nope")

    assert response.status == 400
    assert "没有匹配到" in response.json["error"]["message"]


@pytest.mark.parametrize("value", ["summary.totalCredits", "x" * 65])
def test_fields_rejects_nested_or_oversized_names(client: SanicTestClient, value: str) -> None:
    _, response = client.get(f"/api/v1/quota?fields={value}")

    assert response.status == 400
    assert "一级字段名" in response.json["error"]["message"]


def test_fields_on_usage_keeps_the_model_summary(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/usage?fields=modelSummaryList,totalUsage&days=7")

    assert response.status == 200
    assert set(response.json["data"]) == {"modelSummaryList", "totalUsage"}
    assert response.json["meta"]["range"]["days"] >= 7


def test_fields_on_overview_skips_unrequested_sections(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/overview?fields=quota")

    assert response.status == 200
    assert set(response.json["data"]) == {"quota"}
    assert set(response.json["meta"]["cacheState"]) == {"quota"}
    # Sections that were not asked for are not fetched either.
    assert upstream.count(QUOTA_PATH) == 1
    assert upstream.count(USAGE_DETAIL_PATH) == 0
    assert upstream.count(ACTIVITY_PATH) == 0
    assert upstream.count(PERFORMANCE_PATH) == 0


def test_fields_on_overview_rejects_unknown_sections(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/overview?fields=bogus")

    assert response.status == 400
    assert "quota / usage / activity / performance" in response.json["error"]["message"]


def test_fields_on_account_selects_sections(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/account?fields=subscription,balance")

    assert response.status == 200
    assert set(response.json["data"]) == {"subscription", "balance"}
    assert upstream.count(SUBSCRIPTION_LIST_PATH) == 1
    assert upstream.count(CUSTOMER_INFO_PATH) == 0


def test_fields_on_metrics(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/metrics?fields=cache,version")

    assert response.status == 200
    assert set(response.json["data"]) == {"cache", "version"}
    assert response.json["meta"]["fields"] == ["cache", "version"]


def test_summary_exposes_quotas_credits_and_hit_rate(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/summary")

    assert response.status == 200
    data = response.json["data"]
    five_hour = data["fiveHour"]
    assert five_hour["limit"] == 2000
    assert five_hour["used"] == 37
    assert five_hour["remaining"] == 1962
    assert five_hour["usedPercent"] == 1.85
    assert data["weekly"]["limit"] == 10000
    assert data["totalCredits"] == 159.2652
    assert data["cacheHitRate"] == 0.9221
    assert data["window"]["days"] >= 7


def test_summary_reports_times_in_cst(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/summary")

    five_hour = response.json["data"]["fiveHour"]
    # nextResetTime 1789195203620 ms -> 2026-09-12 14:40:03 +08:00
    assert five_hour["resetAt"] == "2026-09-12T14:40:03+08:00"
    assert isinstance(five_hour["resetInSeconds"], int)
    assert five_hour["resetInSeconds"] >= 0
    assert response.json["data"]["weekly"]["resetAt"] == "2026-09-19T13:20:03+08:00"
    assert response.json["meta"]["timezone"] == "Asia/Shanghai"
    assert response.json["meta"]["generatedAt"].endswith("+08:00")


def test_summary_reports_freshness(client: SanicTestClient) -> None:
    _, first = client.get("/api/v1/summary")
    _, second = client.get("/api/v1/summary")

    assert first.json["meta"]["maxAgeSeconds"] == 300.0
    assert second.json["meta"]["cacheState"] == {"quota": "HIT", "usage": "HIT"}
    assert second.json["meta"]["ageSeconds"] >= 0
    assert second.json["meta"]["errors"] is None


def test_summary_shares_the_section_caches(client: SanicTestClient, upstream: FakeUpstream) -> None:
    client.get("/api/v1/quota")
    client.get("/api/v1/usage")
    client.get("/api/v1/summary")
    client.get("/api/v1/summary")

    # No extra upstream traffic: it is the very same two cache entries.
    assert upstream.count(QUOTA_PATH) == 1
    assert upstream.count(USAGE_DETAIL_PATH) == 1


def test_summary_max_age_forces_a_reload(client_for, upstream: FakeUpstream) -> None:
    client = client_for(quota_ttl=600, usage_ttl=600)
    assert client.get("/api/v1/summary")[1].status == 200
    assert upstream.count(QUOTA_PATH) == 1

    _, response = client.get("/api/v1/summary?maxAge=0")

    assert response.status == 200
    assert upstream.count(QUOTA_PATH) == 2
    assert upstream.count(USAGE_DETAIL_PATH) == 2


def test_summary_refuses_stale_data_older_than_max_age(client_for, upstream: FakeUpstream) -> None:
    client = client_for(quota_ttl=0.01, stale_ttl=30.0, retries=0, summary_max_age=0.05)
    assert client.get("/api/v1/summary")[1].status == 200

    time.sleep(0.06)
    upstream.fail_paths.add(QUOTA_PATH)
    _, response = client.get("/api/v1/summary")

    # quota 太旧：宁可这一格报错，也不把旧额度当成当前值返回。
    assert response.status == 200
    assert "fiveHour" not in response.json["data"]
    assert "totalCredits" in response.json["data"]
    assert response.json["meta"]["errors"]["quota"]["type"] == "upstream_unavailable"


def test_summary_validates_fields_before_calling_upstream(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/summary?fields=nope")

    assert response.status == 400
    assert "fiveHour / weekly" in response.json["error"]["message"]
    assert upstream.count(QUOTA_PATH) == 0


def test_summary_fields_trimming(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/summary?fields=fiveHour,totalCredits")

    assert response.status == 200
    assert set(response.json["data"]) == {"fiveHour", "totalCredits"}


@pytest.mark.parametrize("value", ["-5", "abc"])
def test_summary_rejects_bad_max_age(client: SanicTestClient, value: str) -> None:
    _, response = client.get(f"/api/v1/summary?maxAge={value}")

    assert response.status == 400
    assert "maxAge" in response.json["error"]["message"]


def test_data_age_is_reported_on_single_sections(client: SanicTestClient) -> None:
    client.get("/api/v1/quota")
    _, response = client.get("/api/v1/quota")

    assert response.json["meta"]["cacheState"] == "HIT"
    assert response.json["meta"]["ageSeconds"] >= 0


def test_dashboard_is_not_on_the_root_path(client: SanicTestClient) -> None:
    # Sanic 的测试客户端固定跟随跳转，所以校验跳转链本身
    _, response = client.get("/")
    assert [(h.status_code, h.headers.get("location")) for h in response.history] == [(302, "/dashboard")]
    assert "GLM 用量看板" in response.text

    for path in ("/dashboard", "/dashboard/"):
        _, direct = client.get(path)
        assert direct.status == 200
        assert "GLM 用量看板" in direct.text


def test_dashboard_path_is_configurable(client_for) -> None:
    client = client_for(dashboard_path="admin/usage/")

    assert client.get("/admin/usage")[1].status == 200
    assert client.get("/admin/usage/")[1].status == 200
    _, response = client.get("/")
    assert [(h.status_code, h.headers.get("location")) for h in response.history] == [(302, "/admin/usage")]


def test_dashboard_requires_the_key(client_for) -> None:
    """面板的 HTML / JS / CSS 也是资源：没带 key 直接 403，别把 app.js 漏出去。"""
    client = client_for(api_key="s3cret")

    for path in ("/dashboard", "/dashboard/", "/dashboard/static/app.js", "/dashboard/static/style.css"):
        assert client.get(path)[1].status == 403

    assert client.get("/healthz")[1].status == 200  # 探针不受影响
    assert client.get("/dashboard?key=wrong")[1].status == 401


def test_dashboard_bootstraps_a_cookie_from_the_query_key(client_for) -> None:
    """?key= 开一次门就够了：刷新页面是顶层导航，带不了请求头，之后靠 cookie。"""
    client = client_for(api_key="s3cret")

    _, response = client.get("/dashboard?key=s3cret")
    assert response.status == 200
    cookie = response.headers.get("set-cookie", "")
    assert "glm_usage_key=s3cret" in cookie
    assert "HttpOnly" in cookie

    allowed = {"Cookie": "glm_usage_key=s3cret"}
    assert client.get("/dashboard", headers=allowed)[1].status == 200
    assert client.get("/dashboard/static/app.js", headers=allowed)[1].status == 200

    assert client.get("/dashboard", headers={"Cookie": "glm_usage_key=wrong"})[1].status == 401
    assert client.get("/dashboard/static/app.js", headers={"Cookie": "glm_usage_key=wrong"})[1].status == 401


def test_api_key_accepts_the_query_parameter(client_for) -> None:
    client = client_for(api_key="s3cret")

    assert client.get("/api/v1/quota")[1].status == 403
    assert client.get("/api/v1/quota?key=wrong")[1].status == 401
    assert client.get("/api/v1/quota?key=s3cret")[1].status == 200


def test_refresh_is_throttled(client_for, upstream: FakeUpstream) -> None:
    client = client_for(refresh_min_interval=60, quota_ttl=600)

    first = client.get("/api/v1/quota?refresh=1")[1]
    second = client.get("/api/v1/quota?refresh=1")[1]

    assert first.headers["x-cache"] == "MISS"
    # 第二次强制刷新被节流：退回读缓存，并如实标记
    assert second.headers["x-cache"] == "HIT"
    assert second.json["meta"]["refreshThrottled"] is True
    assert upstream.count(QUOTA_PATH) == 1


def test_refresh_throttle_can_be_disabled(client_for, upstream: FakeUpstream) -> None:
    client = client_for(refresh_min_interval=0, quota_ttl=600)

    client.get("/api/v1/quota?refresh=1")
    _, response = client.get("/api/v1/quota?refresh=1")

    assert response.json["meta"]["cacheState"] == "MISS"
    assert upstream.count(QUOTA_PATH) == 2


def test_fan_out_endpoints_set_a_summary_cache_header(client: SanicTestClient) -> None:
    _, first = client.get("/api/v1/overview")
    _, second = client.get("/api/v1/overview")
    _, account = client.get("/api/v1/account")

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert account.headers["x-cache"] == "MISS"


def test_account_reports_data_age(client: SanicTestClient) -> None:
    client.get("/api/v1/account")
    _, response = client.get("/api/v1/account")

    assert response.json["meta"]["ageSeconds"] >= 0
    assert response.json["meta"]["cacheState"] == dict.fromkeys(
        ["subscription", "autoRenewClosed", "quotaResets", "balance", "customer", "trialTokens"], "HIT"
    )


def test_unknown_route_returns_json_404(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/nope")
    assert response.status == 404
    assert response.headers["content-type"].startswith("application/json")


def test_shanghai_timezone_is_used(client: SanicTestClient) -> None:
    assert SHANGHAI.key == "Asia/Shanghai"
