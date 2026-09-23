"""Unit tests for ZCode gateway helpers, model mapping, and credentials."""

from __future__ import annotations

import json
from pathlib import Path

from glm_usage.zcode import (
    DEFAULT_ZCODE_GATEWAY_ORIGIN,
    ZCodeCredentials,
    build_zcode_headers,
    map_model_name,
    resolve_official_coding_plan_gateway_url,
)


def test_map_model_name() -> None:
    assert map_model_name("claude-3-5-sonnet") == "GLM-5.3"
    assert map_model_name("claude-3-7-sonnet-20250219") == "GLM-5.3"
    assert map_model_name("claude-3-5-haiku-20241022") == "GLM-5.3-Flash"
    assert map_model_name("GLM-5.2") == "GLM-5.2"
    assert map_model_name("glm-4.7") == "GLM-4.7"
    assert map_model_name(None, default_model="GLM-5.3") == "GLM-5.3"
    assert map_model_name("unknown-model", default_model="GLM-5.3") == "GLM-5.3"


def test_resolve_gateway_url() -> None:
    matched, url = resolve_official_coding_plan_gateway_url(
        "https://open.bigmodel.cn/api/anthropic/v1/messages"
    )
    assert matched is True
    assert url == f"{DEFAULT_ZCODE_GATEWAY_ORIGIN}/api/v1/ultra/anthropic/v1/messages"

    matched, url = resolve_official_coding_plan_gateway_url(
        "https://api.z.ai/api/anthropic/v1/messages"
    )
    assert matched is True
    assert url == f"{DEFAULT_ZCODE_GATEWAY_ORIGIN}/api/v1/ultra-zai/anthropic/v1/messages"

    matched, url = resolve_official_coding_plan_gateway_url("https://other.domain.com/v1/messages")
    assert matched is False
    assert url == "https://other.domain.com/v1/messages"


def test_build_zcode_headers() -> None:
    headers = build_zcode_headers(
        "test-key",
        req_id="custom-req-id",
        session_id="custom-session-id",
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    assert headers["User-Agent"] == "ZCode/3.14.0"
    assert headers["HTTP-Referer"] == "https://zcode.z.ai"
    assert headers["X-Title"] == "Z Code@electron"
    assert headers["X-ZCode-App-Version"] == "3.14.0"
    assert headers["X-Release-Channel"] == "production"
    assert headers["X-Client-Language"] == "zh-CN"
    assert headers["X-Client-Timezone"] == "Asia/Shanghai"
    assert headers["X-ZCode-Agent"] == "glm"
    assert "X-Platform" in headers
    assert "X-Os-Category" in headers
    assert "X-Os-Version" in headers
    assert headers["x-request-id"] == "custom-req-id"
    assert headers["x-session-id"] == "custom-session-id"
    assert headers["x-zcode-session-type"] == "main"
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["x-api-key"] == "test-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "prompt-caching-2024-07-31"


def test_zcode_credentials_persistence(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    creds = ZCodeCredentials(
        provider="bigmodel",
        api_key="key-123.secret-456",
        zcode_jwt_token="jwt-789",
        oauth_access_token="oauth-abc",
        expires_at=9999999999.0,
    )
    creds.save_to_file(token_file)
    assert token_file.exists()

    with token_file.open(encoding="utf-8") as f:
        data = json.load(f)
    assert data["provider"] == "bigmodel"
    assert data["api_key"] == "key-123.secret-456"

    loaded = ZCodeCredentials.load_from_file(token_file)
    assert loaded.provider == "bigmodel"
    assert loaded.api_key == "key-123.secret-456"
    assert loaded.zcode_jwt_token == "jwt-789"
    assert loaded.oauth_access_token == "oauth-abc"
    assert loaded.is_valid is True
    assert loaded.has_reset_capability is True

    summary = loaded.masked_summary()
    assert summary["configured"] is True
    assert summary["apiKeyMasked"] == "key-12...et-456"
    assert summary["hasResetCapability"] is True
