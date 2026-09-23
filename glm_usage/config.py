"""Configuration and credential resolution for the GLM usage server."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def _raw(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _str(name: str, default: str | None = None) -> str | None:
    return _raw(name) or default


def _coerce(name: str, default: T, cast: Callable[[str], T]) -> T:
    value = _raw(name)
    if value is None:
        return default
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"environment variable {name} has invalid value {value!r}") from exc


def _int(name: str, default: int) -> int:
    return _coerce(name, default, int)


def _float(name: str, default: float) -> float:
    return _coerce(name, default, float)


def _bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings, normally built from the environment."""

    base_url: str = "https://bigmodel.cn"
    token: str | None = None
    token_file: str | None = None
    zcode_token_file: str | None = None
    api_key: str | None = None

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    debug: bool = False
    access_log: bool = True

    request_timeout: float = 15.0
    connect_timeout: float = 5.0
    max_connections: int = 32
    max_keepalive_connections: int = 16
    retries: int = 2
    retry_backoff: float = 0.3

    quota_ttl: float = 60.0
    usage_ttl: float = 300.0
    activity_ttl: float = 300.0
    performance_ttl: float = 300.0
    account_ttl: float = 3600.0
    # 上游故障时旧值还能用多久。设短一点：宁可报错，也别把半小时前的数字当现在。
    stale_ttl: float = 600.0
    cache_max_entries: int = 256
    # 同一把 key 两次强制刷新之间的最小间隔（?refresh=1），0 表示不限制
    refresh_min_interval: float = 30.0

    default_usage_days: int = 7
    default_activity_days: int = 30
    max_range_days: int = 366
    dashboard_refresh_seconds: int = 30
    # 摘要接口默认只接受这个年龄以内的缓存；超过就重新取，取不到就报错，
    # 而不是把很旧的数据当最新数据返回。
    summary_max_age: float = 300.0
    # 面板路径，故意不放在根路径，方便在反代/中间件上按路径授权
    dashboard_path: str = "/dashboard"

    version: str = "0.1.1"

    @classmethod
    def from_env(cls) -> Settings:
        prefix = "GLM_USAGE_"
        return cls(
            base_url=(_str(f"{prefix}BASE_URL") or _str("BIGMODEL_BASE_URL") or "https://bigmodel.cn").rstrip("/"),
            token=_str("BIGMODEL_TOKEN"),
            token_file=_str("BIGMODEL_TOKEN_FILE"),
            zcode_token_file=_str(f"{prefix}ZCODE_TOKEN_FILE")
            or _str("ZCODE_TOKEN_FILE", str(Path("~/.zcode_coding_plan_token.json").expanduser())),
            api_key=_str(f"{prefix}API_KEY"),
            host=_str(f"{prefix}HOST", "0.0.0.0") or "0.0.0.0",
            port=_int(f"{prefix}PORT", 8000),
            workers=_int(f"{prefix}WORKERS", 1),
            debug=_bool(f"{prefix}DEBUG", False),
            access_log=_bool(f"{prefix}ACCESS_LOG", True),
            request_timeout=_float(f"{prefix}REQUEST_TIMEOUT", 15.0),
            connect_timeout=_float(f"{prefix}CONNECT_TIMEOUT", 5.0),
            max_connections=_int(f"{prefix}MAX_CONNECTIONS", 32),
            max_keepalive_connections=_int(f"{prefix}MAX_KEEPALIVE_CONNECTIONS", 16),
            retries=_int(f"{prefix}RETRIES", 2),
            retry_backoff=_float(f"{prefix}RETRY_BACKOFF", 0.3),
            quota_ttl=_float(f"{prefix}QUOTA_TTL", 60.0),
            usage_ttl=_float(f"{prefix}USAGE_TTL", 300.0),
            activity_ttl=_float(f"{prefix}ACTIVITY_TTL", 300.0),
            performance_ttl=_float(f"{prefix}PERFORMANCE_TTL", 300.0),
            account_ttl=_float(f"{prefix}ACCOUNT_TTL", 3600.0),
            stale_ttl=_float(f"{prefix}STALE_TTL", 600.0),
            cache_max_entries=_int(f"{prefix}CACHE_MAX_ENTRIES", 256),
            refresh_min_interval=_float(f"{prefix}REFRESH_MIN_INTERVAL", 30.0),
            default_usage_days=_int(f"{prefix}DEFAULT_USAGE_DAYS", 7),
            default_activity_days=_int(f"{prefix}DEFAULT_ACTIVITY_DAYS", 30),
            max_range_days=_int(f"{prefix}MAX_RANGE_DAYS", 366),
            dashboard_refresh_seconds=_int(f"{prefix}DASHBOARD_REFRESH_SECONDS", 30),
            summary_max_age=_float(f"{prefix}SUMMARY_MAX_AGE", 300.0),
            dashboard_path=_str(f"{prefix}DASHBOARD_PATH", "/dashboard") or "/dashboard",
        )


class TokenStore:
    """Resolves the bigmodel JWT from an inline value or a file.

    The file is re-read whenever its mtime changes, so dropping a fresh
    ``bigmodel_token_production`` value into it is picked up without a restart.
    """

    __slots__ = ("_inline", "_path", "_mtime", "_cached", "_checked_at")

    # 用文件提供 token 时，每次请求 stat() 一次没必要：文件变动的粒度是"人来换"，
    # 每秒最多检查一次足够，也避免在事件循环里做多余的同步 IO。
    CHECK_INTERVAL = 1.0

    def __init__(self, inline: str | None = None, path: str | Path | None = None) -> None:
        self._inline = inline.strip() if inline else None
        self._path = Path(path).expanduser() if path else None
        self._mtime: float | None = None
        self._cached: str | None = None
        self._checked_at = 0.0

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenStore:
        return cls(settings.token, settings.token_file)

    @property
    def source(self) -> str:
        if self._path is not None:
            return "file"
        if self._inline:
            return "env"
        return "none"

    def get(self) -> str | None:
        if self._path is None:
            return self._inline
        now = time.monotonic()
        if self._mtime is not None and now - self._checked_at < self.CHECK_INTERVAL:
            return self._cached or self._inline
        self._checked_at = now
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return self._cached or self._inline
        if mtime != self._mtime:
            try:
                value = self._path.read_text(encoding="utf-8").strip()
            except OSError:
                return self._cached or self._inline
            self._cached = value or None
            self._mtime = mtime
        return self._cached or self._inline
