"""Unit tests for settings parsing and the token store."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from glm_usage.config import Settings, TokenStore
from glm_usage.logging_filters import RedactQueryFilter, install_secret_filter


def test_access_logs_never_contain_the_api_key() -> None:
    record = logging.LogRecord(
        name="sanic.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="",
        args=(),
        exc_info=None,
    )
    record.request = "GET http://host:8000/dashboard?key=s3cret&x=1"

    assert RedactQueryFilter().filter(record) is True
    assert record.request == "GET http://host:8000/dashboard?key=***&x=1"


def test_redaction_leaves_other_records_alone() -> None:
    record = logging.LogRecord(
        name="sanic.access", level=logging.INFO, pathname=__file__, lineno=1, msg="", args=(), exc_info=None
    )
    record.request = "GET http://host:8000/api/v1/quota?days=7"

    RedactQueryFilter().filter(record)

    assert record.request == "GET http://host:8000/api/v1/quota?days=7"


def test_secret_filter_is_installed_idempotently() -> None:
    install_secret_filter("glm_usage.test.access")
    install_secret_filter("glm_usage.test.access")

    logger = logging.getLogger("glm_usage.test.access")
    assert sum(isinstance(f, RedactQueryFilter) for f in logger.filters) == 1


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("GLM_USAGE_", "BIGMODEL_")):
            monkeypatch.delenv(name, raising=False)


def test_defaults(clean_env: None) -> None:
    settings = Settings.from_env()

    assert settings.base_url == "https://bigmodel.cn"
    assert settings.token is None
    assert settings.dashboard_path == "/dashboard"
    assert settings.refresh_min_interval == 30.0
    assert settings.stale_ttl == 600.0


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv("BIGMODEL_TOKEN", "jwt")
    monkeypatch.setenv("GLM_USAGE_PORT", "9001")
    monkeypatch.setenv("GLM_USAGE_BASE_URL", "https://example.test/")
    monkeypatch.setenv("GLM_USAGE_ACCESS_LOG", "no")
    monkeypatch.setenv("GLM_USAGE_QUOTA_TTL", "5.5")

    settings = Settings.from_env()

    assert settings.token == "jwt"
    assert settings.port == 9001
    assert settings.base_url == "https://example.test"  # trailing slash trimmed
    assert settings.access_log is False
    assert settings.quota_ttl == 5.5


def test_bad_values_fail_loudly(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv("GLM_USAGE_PORT", "not-a-port")

    with pytest.raises(RuntimeError, match="GLM_USAGE_PORT"):
        Settings.from_env()


def test_blank_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv("GLM_USAGE_PORT", "   ")
    monkeypatch.setenv("BIGMODEL_TOKEN", "")

    settings = Settings.from_env()

    assert settings.port == 8000
    assert settings.token is None


def test_token_store_prefers_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(TokenStore, "CHECK_INTERVAL", 0.0)
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n", encoding="utf-8")
    store = TokenStore("from-env", token_file)

    assert store.source == "file"
    assert store.get() == "from-file"

    # Operationally the useful bit: a fresh token in the file is picked up
    # without a restart.
    token_file.write_text("rotated\n", encoding="utf-8")
    os.utime(token_file, (0, 0))
    assert store.get() == "rotated"


def test_token_store_falls_back_to_env_when_the_file_is_gone(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(TokenStore, "CHECK_INTERVAL", 0.0)
    store = TokenStore("from-env", tmp_path / "missing")

    assert store.get() == "from-env"

    # A file that appears later wins from then on.
    (tmp_path / "missing").write_text("later\n", encoding="utf-8")
    assert store.get() == "later"


def test_token_store_without_a_file(clean_env: None) -> None:
    assert TokenStore("from-env").get() == "from-env"
    assert TokenStore("from-env").source == "env"
    assert TokenStore().source == "none"


def test_token_store_throttles_file_checks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(TokenStore, "CHECK_INTERVAL", 60.0)
    token_file = tmp_path / "token"
    token_file.write_text("first\n", encoding="utf-8")
    store = TokenStore(path=token_file)

    assert store.get() == "first"

    checks = []
    monkeypatch.setattr(type(token_file), "stat", lambda self, **kw: checks.append(1) or Path.stat(self))

    assert store.get() == "first"
    assert checks == []  # 一分钟内不再 stat，避免每请求一次同步 IO
