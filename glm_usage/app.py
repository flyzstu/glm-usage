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
from .zcode import ZCodeCredentials, ZCodeService

STATIC_DIR = Path(__file__).parent / "static"
APP_NAME = "glm_usage"
# ?key= 只用来"开一次门"：进门时种下这个 cookie，此后面板自己的 HTML / JS / CSS
# 都认它——刷新页面是顶层导航，带不了 X-API-Key 请求头，光靠 ?key= 会一刷新就 403。
DASHBOARD_COOKIE = "glm_usage_key"
DASHBOARD_COOKIE_MAX_AGE = 30 * 24 * 3600


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
    dashboard_path = normalise_path(settings.dashboard_path)
    app = Sanic(name)
    app.config.FALLBACK_ERROR_FORMAT = "json"
    app.config.DEBUG = settings.debug
    app.config.ACCESS_LOG = settings.access_log
    app.config.KEEP_ALIVE = True
    app.config.KEEP_ALIVE_TIMEOUT = 65
    app.config.REQUEST_TIMEOUT = max(int(settings.request_timeout) + 5, 30)
    app.config.RESPONSE_TIMEOUT = 600
    app.config.GRACEFUL_SHUTDOWN_TIMEOUT = 5

    metrics = Metrics()
    app.ctx.settings = settings
    app.ctx.metrics = metrics
    app.ctx.tokens = TokenStore.from_settings(settings)
    app.ctx.proxy_transport = transport
    if settings.zcode_token_file:
        app.ctx.zcode = ZCodeService(token_file=settings.zcode_token_file, transport=transport)
    else:
        app.ctx.zcode = ZCodeService(credentials=ZCodeCredentials(), transport=transport)
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

    def presented_key(request: Request, *, allow_cookie: bool = False) -> str:
        """Header first, then ``?key=``; the cookie only counts for the dashboard."""
        key = request.headers.get("x-api-key") or request.get_args().get("key") or ""
        if not key and allow_cookie:
            key = request.cookies.get(DASHBOARD_COOKIE) or ""
        return key

    def rejection(provided: str) -> BaseHTTPResponse:
        """Opaque denial: naming the header or ``?key=`` would hand scanners a map.

        Nothing presented at all (the usual scanner pose) is a 403; something that
        simply does not match is a 401. Both bodies are the same one-liner.
        """
        missing = not provided
        return json_response(
            {"error": {"type": "forbidden" if missing else "unauthorized", "message": "未授权"}},
            status=403 if missing else 401,
        )

    @app.on_request
    async def _require_api_key(request: Request) -> BaseHTTPResponse | None:
        expected = settings.api_key
        if not expected or not request.path.startswith("/api/"):
            return None
        provided = presented_key(request)
        return None if secrets.compare_digest(provided, expected) else rejection(provided)

    @app.on_request
    async def _require_dashboard_key(request: Request) -> BaseHTTPResponse | None:
        """The dashboard bundle is a resource too: no key, no HTML / JS / CSS.

        The API says nothing about the auth scheme, so serving ``app.js`` (which
        spells out ``?key=``) to anonymous callers would leak it right back out.
        """
        expected = settings.api_key
        if not expected:
            return None
        if request.path != dashboard_path and not request.path.startswith(f"{dashboard_path}/"):
            return None
        provided = presented_key(request, allow_cookie=True)
        return None if secrets.compare_digest(provided, expected) else rejection(provided)

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

        Stays HTTP 200 even when ``degraded`` so a missing or expired token never
        makes the orchestrator restart-loop the container — but the body tells the
        truth: ``token.source`` distinguishes "not configured" from "misconfigured",
        and ``upstreamAuth.rejected`` means bigmodel turned the token down (it
        clears itself as soon as any upstream call succeeds again).
        """
        tokens: TokenStore = request.app.ctx.tokens
        metrics: Metrics = request.app.ctx.metrics
        zcode_svc = getattr(request.app.ctx, "zcode", None)
        zcode_info = zcode_svc.credentials.masked_summary() if zcode_svc else {}
        token_source = "zcode_token_file" if (zcode_svc and zcode_svc.credentials.api_key) else tokens.source

        try:
            resolve_token(request)
            configured = True
        except BigModelError:
            configured = False

        auth = metrics.auth_state()
        return json_response(
            {
                "status": "ok" if configured and not auth["rejected"] else "degraded",
                "version": settings.version,
                "uptimeSeconds": round(metrics.snapshot()["uptimeSeconds"], 3),
                "token": {"configured": configured, "source": token_source},
                "zcode": zcode_info,
                "upstreamAuth": auth,
                "upstream": settings.base_url,
                "refreshSeconds": settings.dashboard_refresh_seconds,
            }
        )

    app.blueprint(api.bp)
    app.blueprint(api.proxy_bp)

    # 面板故意不挂在根路径：/ 只做跳转，这样反代或中间件可以按
    # /dashboard 这个前缀单独加授权，而不会连带影响 API。
    dashboard_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace(
        "{{STATIC}}", f"{dashboard_path}/static"
    )

    async def _dashboard(request: Request) -> BaseHTTPResponse:
        # 渲染好的 HTML 常驻内存：一次读盘，之后零 IO。
        response = html(dashboard_html)
        bootstrap = request.args.get("key") or ""
        if settings.api_key and bootstrap and secrets.compare_digest(bootstrap, settings.api_key):
            # 带 ?key= 进来的那一次顺便种 cookie：地址栏随即被前端抹干净，之后刷新
            # （顶层导航，带不了请求头）靠它进门。httponly —— 前端不需要读它，
            # API 调用照旧走 X-API-Key（密钥在 localStorage）。secure 保持关闭：
            # 文档里的 http://127.0.0.1:8000 是本机明文用法，开了浏览器会直接丢掉。
            response.add_cookie(
                DASHBOARD_COOKIE,
                settings.api_key,
                path=dashboard_path,
                httponly=True,
                secure=False,
                max_age=DASHBOARD_COOKIE_MAX_AGE,
            )
        return response

    # Sanic 自己会把 /dashboard/ 归到这条路由上（strict_slashes 默认关闭），
    # 重复注册带尾斜杠的版本会直接 RouteExists。
    app.add_route(_dashboard, dashboard_path, methods=["GET"])
    app.static(f"{dashboard_path}/static", str(STATIC_DIR), name="dashboard-static")

    if dashboard_path != "/":

        @app.get("/")
        async def _to_dashboard(_request: Request) -> BaseHTTPResponse:
            return redirect(dashboard_path)

    return app
