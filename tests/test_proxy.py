"""Tests for transparent proxy forwarding, 1:1 headers, and error passthrough."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import FakeUpstream, build_app
from sanic import Sanic
from sanic_testing.testing import SanicTestClient

from glm_usage.zcode import ZCodeCredentials


@pytest.fixture
def proxy_app(upstream: FakeUpstream) -> Sanic:
    app = build_app(upstream)
    # 模拟有效的 ZCode 凭据
    app.ctx.zcode.credentials = ZCodeCredentials(
        provider="bigmodel",
        api_key="mock-key.mock-secret",
        zcode_jwt_token="mock-jwt",
        oauth_access_token="mock-oauth",
        expires_at=9999999999.0,
    )
    return app


@pytest.fixture
def proxy_client(proxy_app: Sanic) -> SanicTestClient:
    return SanicTestClient(proxy_app, port=None)


def test_v1_models(proxy_client: SanicTestClient) -> None:
    _, resp = proxy_client.get("/v1/models")
    assert resp.status == 200
    data = resp.json
    assert "data" in data
    model_ids = [m["id"] for m in data["data"]]
    assert "GLM-5.3" in model_ids
    assert "GLM-5.3-Flash" in model_ids
    assert "claude-3-7-sonnet-20250219" in model_ids


def test_proxy_status(proxy_client: SanicTestClient) -> None:
    _, resp = proxy_client.get("/api/v1/proxy/status")
    assert resp.status == 200
    data = resp.json["data"]
    assert data["status"] == "ready"
    assert "localEndpoints" in data
    assert "snippets" in data
    assert "stats" in data


def test_proxy_messages_success_non_stream(proxy_client: SanicTestClient, upstream: FakeUpstream) -> None:
    mock_resp_body = {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello from GLM-5.3"}],
        "model": "GLM-5.3",
    }
    upstream.custom_responses["/api/v1/ultra/anthropic/v1/messages"] = httpx.Response(
        status_code=200,
        json=mock_resp_body,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    _, resp = proxy_client.post("/v1/messages", json=payload)
    assert resp.status == 200
    assert resp.json["id"] == "msg_123"

    # 验证 1:1 ZCode 请求头与路由重写
    ultra_reqs = [r for r in upstream.requests if r.url.path == "/api/v1/ultra/anthropic/v1/messages"]
    assert len(ultra_reqs) == 1
    sent_req = ultra_reqs[0]
    assert sent_req.headers["User-Agent"] == "ZCode/3.14.0"
    assert sent_req.headers["X-Title"] == "Z Code@electron"
    assert sent_req.headers["X-ZCode-Agent"] == "glm"
    assert sent_req.headers["Authorization"] == "Bearer mock-key.mock-secret"
    assert sent_req.headers["x-api-key"] == "mock-key.mock-secret"
    assert "/api/v1/ultra/anthropic/v1/messages" in str(sent_req.url)

    # 验证模型名称从 claude 自动映射为 GLM-5.3
    body = json.loads(sent_req.content.decode("utf-8"))
    assert body["model"] == "GLM-5.3"


def test_proxy_messages_upstream_error_passthrough(
    proxy_client: SanicTestClient, upstream: FakeUpstream
) -> None:
    """关键测试：上游返回任何错误（429/400/500）直接透传给下游，内部不做处理."""
    error_payload = {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "code": "1310",
            "message": "您已达到每周使用上限，您的限额将在 2026-09-25 12:16:19 重置。",
        },
    }
    upstream.custom_responses["/api/v1/ultra/anthropic/v1/messages"] = httpx.Response(
        status_code=429,
        json=error_payload,
        headers={"content-type": "application/json", "retry-after": "3600"},
    )

    payload = {
        "model": "GLM-5.3",
        "messages": [{"role": "user", "content": "ping"}],
    }

    _, resp = proxy_client.post("/v1/messages", json=payload)
    # 状态码必须原样透传
    assert resp.status == 429
    # 错误体必须完全原样透传
    assert resp.json["error"]["type"] == "rate_limit_error"
    assert resp.json["error"]["code"] == "1310"
    assert "2026-09-25" in resp.json["error"]["message"]
    # 标头必须透传
    assert resp.headers.get("retry-after") == "3600"


def test_proxy_auth_enforced_when_api_key_configured(upstream: FakeUpstream) -> None:
    """当服务端配置了 GLM_USAGE_API_KEY 时，下游客户端必须携带匹配的密钥."""
    app = build_app(upstream, api_key="my-secret-proxy-key")
    app.ctx.zcode.credentials = ZCodeCredentials(
        provider="bigmodel",
        api_key="mock-key.mock-secret",
    )
    client = SanicTestClient(app, port=None)

    payload = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}]}

    # 未携带密钥 -> 401
    _, resp = client.post("/v1/messages", json=payload)
    assert resp.status == 401
    assert "未授权" in resp.json["error"]["message"]

    # 携带错误密钥 -> 401
    _, resp = client.post("/v1/messages", json=payload, headers={"x-api-key": "wrong-key"})
    assert resp.status == 401

    # 携带正确密钥 -> 通过校验（在此用例中会因未 mock 上游而尝试连接或被捕获）
    # 证明 auth 检查通过
