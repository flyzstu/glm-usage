"""Tests for quota reset cards and credentials endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeUpstream, build_app
from sanic import Sanic
from sanic_testing.testing import SanicTestClient

from glm_usage.zcode import ZCodeCredentials


@pytest.fixture
def zcode_app(upstream: FakeUpstream, tmp_path: Path) -> Sanic:
    app = build_app(upstream)
    app.ctx.zcode.token_file = tmp_path / "test_token.json"
    app.ctx.zcode.credentials = ZCodeCredentials(
        provider="bigmodel",
        api_key="mock-key.mock-secret",
        zcode_jwt_token="mock-jwt",
        oauth_access_token="mock-oauth",
        expires_at=9999999999.0,
    )
    return app


@pytest.fixture
def zcode_client(zcode_app: Sanic) -> SanicTestClient:
    return SanicTestClient(zcode_app, port=None)


def test_reset_status_without_oauth_token(upstream: FakeUpstream) -> None:
    app = build_app(upstream)
    # 没有 zcode_jwt_token 和 oauth_access_token
    app.ctx.zcode.credentials = ZCodeCredentials(api_key="key.secret")
    client = SanicTestClient(app, port=None)

    _, resp = client.get("/api/v1/reset/status")
    assert resp.status == 200
    data = resp.json["data"]
    assert data["available"] is False
    assert "缺少 ZCode 平台认证令牌" in data["reason"]


def test_reset_status_success(zcode_client: SanicTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_reset_status(self: Any) -> dict[str, Any]:
        return {
            "available": True,
            "five_hour_resets": [{"id": "card_5h_1", "status": "AVAILABLE"}],
            "week_resets": [{"id": "card_wk_1", "status": "AVAILABLE"}],
            "latest_five_hour_history": None,
            "latest_week_history": None,
            "has_unread_history": False,
        }

    from glm_usage.zcode import ZCodeService

    monkeypatch.setattr(ZCodeService, "get_reset_status", fake_get_reset_status)

    _, resp = zcode_client.get("/api/v1/reset/status")
    assert resp.status == 200
    data = resp.json["data"]
    assert data["available"] is True
    assert len(data["five_hour_resets"]) == 1
    assert len(data["week_resets"]) == 1


def test_reset_use_success(zcode_client: SanicTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    used_type: list[str] = []

    async def fake_use_reset(self: Any, reset_type: str) -> dict[str, Any]:
        used_type.append(reset_type)
        return {"code": 0, "msg": "ok", "data": {"success": True}}

    from glm_usage.zcode import ZCodeService

    monkeypatch.setattr(ZCodeService, "use_reset", fake_use_reset)

    _, resp = zcode_client.post("/api/v1/reset/use", json={"reset_type": "FIVE_HOUR"})
    assert resp.status == 200
    assert "成功核销" in resp.json["message"]
    assert used_type == ["FIVE_HOUR"]


def test_reset_use_invalid_type(zcode_client: SanicTestClient) -> None:
    _, resp = zcode_client.post("/api/v1/reset/use", json={"reset_type": "INVALID_TYPE"})
    assert resp.status == 400
    assert "FIVE_HOUR" in resp.json["error"]["message"]


def test_credentials_get_and_update(zcode_client: SanicTestClient) -> None:
    _, resp = zcode_client.get("/api/v1/credentials")
    assert resp.status == 200
    data = resp.json["data"]
    assert data["configured"] is True
    assert data["hasResetCapability"] is True

    # 更新凭据
    payload = {
        "api_key": "new-key.new-secret",
        "provider": "bigmodel",
    }
    _, resp2 = zcode_client.post("/api/v1/credentials", json=payload)
    assert resp2.status == 200
    assert resp2.json["success"] is True
    assert "new-ke" in resp2.json["data"]["apiKeyMasked"]
