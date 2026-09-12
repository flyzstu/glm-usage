"""Sanic application factory."""

from __future__ import annotations

import secrets
from pathlib import Path

import httpx
from sanic import Request, Sanic
from sanic.exceptions import SanicException
from sanic.response import BaseHTTPResponse, html, redirect

from . import api
from .api import json_response, resolve_token
from .cache import IntervalGate, TTLCache
from .client import BigModelClient, BigModelError
from .config import Settings, TokenStore
from .logging_filters import install_secret_filter
from .metrics import Metrics
from .timerange import BadRequest

STATIC_DIR = Path(__file__).parent / "static"
APP_NAME = "glm_usage"


def normalise_path(raw: str) -> str:
    """``dashboard`` / ``/dashboard/`` -> ``/dashboard`` (``/`` stays ``/``)."""
    path = "/" + raw.strip().strip("/")
    return "/" if path == "/" else path


def create_app(
    settings: Settings | None = None,
    *,
    name: str = APP_NAME,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Sanic:
    """Build the app.

    ``transport`` is injectable so tests can stand in a fake upstream without
    touching the network.
    """
    settings = settings or Settings.from_env()
    app = Sanic(name)
    app.config.FALLBACK_ERROR_FORMAT = "json"
    app.config.DEBUG = settings.debug
    app.config.ACCESS_LOG = settings.access_log
    app.config.KEEP_ALIVE = True
    app.config.KEEP_ALIVE_TIMEOUT = 65
    app.config.REQUEST_TIMEOUT = max(int(settings.request_timeout) + 5, 30)
    app.config.RESPONSE_TIMEOUT = max(int(settings.request_timeout) + 5, 30)
    app.config.GRACEFUL_SHUTDOWN_TIMEOUT = 5

    metrics = Metrics()
    app.ctx.settings = settings
    app.ctx.metrics = metrics
    app.ctx.tokens = TokenStore.from_settings(settings)
    app.ctx.client = BigModelClient(settings, metrics=metrics, transport=transport)
    app.ctx.caches = {
        "quota": TTLCache(settings.quota_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries),
        "usage": TTLCache(settings.usage_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries),
        "performance": TTLCache(
            settings.performance_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries
        ),
        "account": TTLCache(
            settings.account_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries
        ),
        "activity": TTLCache(
            settings.activity_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries
        ),
    }
    # ?refresh=1 bypasses the cache, so it gets its own rate limit.
    app.ctx.refresh_gate = IntervalGate(settings.refresh_min_interval, max_entries=settings.cache_max_entries)

    @app.before_server_start
    async def _open_upstream(app: Sanic) -> None:
        await app.ctx.client.start()
        # Installed here rather than at import time so Sanic's logging config has
        # already been applied.
        install_secret_filter()

    @app.after_server_stop
    async def _close_upstream(app: Sanic) -> None:
        await app.ctx.client.close()

    @app.on_request
    async def _require_api_key(request: Request) -> BaseHTTPResponse | None:
        expected = settings.api_key
        if not expected or not request.path.startswith("/api/"):
            return None
        # The header is the normal way; ?key= exists so a browser can bootstrap
        # the dashboard, which then keeps it in localStorage and stops using the URL.
        provided = request.headers.get("x-api-key") or request.get_args().get("key") or ""
        if not secrets.compare_digest(provided, expected):
            return json_response(
                {"error": {"type": "unauthorized", "message": "缺少或错误的 X-API-Key（也可用 ?key=）"}},
                status=401,
            )
        return None

    @app.on_response
    async def _record(request: Request, response: BaseHTTPResponse) -> None:
        route = getattr(request.route, "path", None) or "-"
        request.app.ctx.metrics.record_request(route, response.status)

    @app.exception(BigModelError)
    async def _upstream_error(_request: Request, exc: BigModelError) -> BaseHTTPResponse:
        return json_response({"error": exc.as_dict()}, status=exc.status)

    @app.exception(BadRequest)
    async def _invalid_request(_request: Request, exc: BadRequest) -> BaseHTTPResponse:
        return json_response(
            {"error": {"type": "invalid_request", "message": str(exc)}},
            status=400,
        )

    @app.exception(SanicException)
    async def _sanic_error(_request: Request, exc: SanicException) -> BaseHTTPResponse:
        return json_response(
            {"error": {"type": "http_error", "message": str(exc)}},
            status=exc.status_code or 500,
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> BaseHTTPResponse:
        """Liveness probe plus the small amount of config the UI needs.

        Stays HTTP 200 even when ``degraded`` so a missing token never makes the
        orchestrator restart-loop the container — but the body tells the truth,
        and ``token.source`` distinguishes "not configured" from "misconfigured".
        """
        tokens: TokenStore = request.app.ctx.tokens
        try:
            resolve_token(request)
            configured = True
        except BigModelError:
            configured = False
        return json_response(
            {
                "status": "ok" if configured else "degraded",
                "version": settings.version,
                "uptimeSeconds": round(request.app.ctx.metrics.snapshot()["uptimeSeconds"], 3),
                "token": {"configured": configured, "source": tokens.source},
                "upstream": settings.base_url,
                "refreshSeconds": settings.dashboard_refresh_seconds,
            }
        )

    app.blueprint(api.bp)

    # 面板故意不挂在根路径：/ 只做跳转，这样反代或中间件可以按
    # /dashboard 这个前缀单独加授权，而不会连带影响 API。
    dashboard_path = normalise_path(settings.dashboard_path)
    dashboard_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace(
        "{{STATIC}}", f"{dashboard_path}/static"
    )

    async def _dashboard(_request: Request) -> BaseHTTPResponse:
        # 渲染好的 HTML 常驻内存：一次读盘，之后零 IO。
        return html(dashboard_html)

    # Sanic 自己会把 /dashboard/ 归到这条路由上（strict_slashes 默认关闭），
    # 重复注册带尾斜杠的版本会直接 RouteExists。
    app.add_route(_dashboard, dashboard_path, methods=["GET"])
    app.static(f"{dashboard_path}/static", str(STATIC_DIR), name="dashboard-static")

    if dashboard_path != "/":

        @app.get("/")
        async def _to_dashboard(_request: Request) -> BaseHTTPResponse:
            return redirect(dashboard_path)

    return app
