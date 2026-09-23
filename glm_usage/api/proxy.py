"""透明代理服务端：1:1 模拟 ZCode 官方 Ultra 网关行为，直通下游错误."""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

import httpx
from sanic import Blueprint, Request
from sanic.response import HTTPResponse

from ..zcode import (
    DEFAULT_ZCODE_GATEWAY_ORIGIN,
    ENDPOINTS,
    build_zcode_headers,
    map_model_name,
)

logger = logging.getLogger(__name__)

proxy_bp = Blueprint("transparent_proxy")


class ProxyStats:
    """透明代理实时统计计数器."""

    def __init__(self) -> None:
        self.total_requests: int = 0
        self.stream_requests: int = 0
        self.success_requests: int = 0
        self.error_requests: int = 0
        self.status_codes: dict[int, int] = {}
        self.last_request_time: float = 0.0
        self.last_model_used: str = ""

    def record(self, status: int, *, is_stream: bool, model: str) -> None:
        self.total_requests += 1
        if is_stream:
            self.stream_requests += 1
        if 200 <= status < 300:
            self.success_requests += 1
        else:
            self.error_requests += 1
        self.status_codes[status] = self.status_codes.get(status, 0) + 1
        self.last_request_time = time.time()
        self.last_model_used = model

    def snapshot(self) -> dict[str, Any]:
        return {
            "totalRequests": self.total_requests,
            "streamRequests": self.stream_requests,
            "successRequests": self.success_requests,
            "errorRequests": self.error_requests,
            "statusCodes": self.status_codes,
            "lastRequestTime": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_request_time))
                if self.last_request_time
                else None
            ),
            "lastModelUsed": self.last_model_used,
        }


proxy_stats = ProxyStats()


def _check_auth(request: Request) -> HTTPResponse | None:
    """检查下游请求认证（若服务端配置了 GLM_USAGE_API_KEY 则必须匹配）."""
    expected = getattr(request.app.ctx.settings, "api_key", None)
    if not expected:
        return None

    # 支持 x-api-key 或 Authorization: Bearer <key>
    provided = request.headers.get("x-api-key") or ""
    if not provided:
        auth_hdr = request.headers.get("authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            provided = auth_hdr[7:].strip()
        elif auth_hdr:
            provided = auth_hdr.strip()

    if not provided:
        # 允许从 query 参数获取（如 ?key=）
        provided = request.args.get("key", "")

    if not secrets.compare_digest(provided, expected):
        return HTTPResponse(
            body=json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "未授权：请提供有效的 x-api-key 或 Authorization 标头",
                    },
                }
            ),
            status=401,
            content_type="application/json",
        )
    return None


@proxy_bp.get("/v1/models")
async def list_models(request: Request) -> HTTPResponse:
    """兼容 Anthropic / OpenAI 客户端的模型列表发现接口."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    models = [
        {"id": "GLM-5.3", "object": "model", "created": 1735689600, "owned_by": "bigmodel"},
        {"id": "GLM-5.3-Flash", "object": "model", "created": 1735689600, "owned_by": "bigmodel"},
        {"id": "GLM-5.2", "object": "model", "created": 1735689600, "owned_by": "bigmodel"},
        {"id": "GLM-4.7", "object": "model", "created": 1735689600, "owned_by": "bigmodel"},
        {"id": "claude-3-7-sonnet-20250219", "object": "model", "created": 1735689600, "owned_by": "bigmodel"},
        {"id": "claude-3-5-sonnet-20241022", "object": "model", "created": 1735689600, "owned_by": "bigmodel"},
        {"id": "claude-3-5-haiku-20241022", "object": "model", "created": 1735689600, "owned_by": "bigmodel"},
    ]
    return HTTPResponse(
        body=json.dumps({"object": "list", "data": models}),
        status=200,
        content_type="application/json",
    )


async def _handle_messages_proxy(request: Request) -> HTTPResponse:
    """1:1 ZCode 官方网关透明代理核心处理器：

    1. 动态对齐 ZCode 官方 Ultra 网关
    2. 1:1 注入 ZCode 官方标头 (操作系统、架构、会话与观测指纹)
    3. 支持 Claude 模型映射到 GLM
    4. 零延迟 SSE 流式转发
    5. 遇到任何错误（4xx、5xx、限流）直接原样透传给下游，不做任何内部拦截与篡改
    """
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    # 1. 解析请求 JSON
    try:
        req_json: dict[str, Any] = request.json or {}
    except Exception:
        try:
            req_json = json.loads(request.body.decode("utf-8")) if request.body else {}
        except Exception:
            return HTTPResponse(
                body=json.dumps(
                    {
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "请求体必须为合法 JSON 格式"},
                    }
                ),
                status=400,
                content_type="application/json",
            )

    # 2. 模型映射 (若下游请求 Claude 模型，映射到 GLM-5.3)
    raw_model = req_json.get("model", "GLM-5.3")
    target_model = map_model_name(raw_model)
    req_json["model"] = target_model

    # 3. 确定上游目标端点 (1:1 镜像 official-coding-plan-gateway.ts)
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    provider = getattr(zcode_svc.credentials, "provider", "bigmodel") if zcode_svc else "bigmodel"
    if "/ultra-zai/" in request.path or provider == "zai":
        gateway_path = ENDPOINTS["zai"]["gateway_path"]
    else:
        gateway_path = ENDPOINTS["bigmodel"]["gateway_path"]

    upstream_url = f"{DEFAULT_ZCODE_GATEWAY_ORIGIN}{gateway_path}"

    # 4. 解析可用 API Key
    api_key = ""
    # 下游请求头若携带了 BigModel 格式 key (id.secret)，可优先使用
    downstream_key = request.headers.get("x-api-key") or ""
    if not downstream_key:
        auth_hdr = request.headers.get("authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            downstream_key = auth_hdr[7:].strip()
    if downstream_key and "." in downstream_key and len(downstream_key) > 20:
        api_key = downstream_key

    # 否则使用服务绑定的凭据
    if not api_key and zcode_svc and zcode_svc.credentials.api_key:
        api_key = zcode_svc.credentials.api_key

    if not api_key:
        # 尝试从 tokens store 获取
        token_store = getattr(request.app.ctx, "tokens", None)
        token_val = token_store.get() if token_store else None
        if token_val and not token_val.startswith("eyJhbGci"):
            api_key = token_val

    if not api_key:
        return HTTPResponse(
            body=json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": (
                            "服务端未配置有效的 BigModel / ZCode API Key。"
                            "请在网页看板(/dashboard)完成 OAuth 授权或配置有效凭据。"
                        ),
                    },
                }
            ),
            status=401,
            content_type="application/json",
        )

    # 5. 组装 1:1 ZCode 官方请求标头
    extra_headers: dict[str, str] = {}
    for h in ("anthropic-version", "anthropic-beta", "accept"):
        val = request.headers.get(h)
        if val:
            extra_headers[h] = val

    req_id = request.headers.get("x-request-id")
    session_id = request.headers.get("x-session-id")

    upstream_headers = build_zcode_headers(
        api_key,
        req_id=req_id,
        session_id=session_id,
        extra_headers=extra_headers,
    )

    is_stream = bool(req_json.get("stream", False))
    req_body_bytes = json.dumps(req_json, ensure_ascii=False).encode("utf-8")

    # 6. 发起上游透明代理请求
    # Coding Plan 处理长上下文可能需要较长超时
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=60.0)

    try:
        # 使用 httpx.AsyncClient stream 发起调用
        transport = getattr(request.app.ctx, "proxy_transport", None)
        client = httpx.AsyncClient(timeout=timeout, headers={"user-agent": "ZCode/3.14.0"}, transport=transport)
        req = client.build_request("POST", upstream_url, headers=upstream_headers, content=req_body_bytes)
        req.headers["user-agent"] = "ZCode/3.14.0"
        upstream_resp = await client.send(req, stream=True)

        # -------------------------------------------------------------
        # 核心原则：遇到错误直接抛给下游内部不做处理
        # -------------------------------------------------------------
        if upstream_resp.status_code != 200:
            err_body = await upstream_resp.aread()
            await upstream_resp.aclose()
            await client.aclose()

            resp_headers: dict[str, str] = {}
            if "retry-after" in upstream_resp.headers:
                resp_headers["retry-after"] = upstream_resp.headers["retry-after"]
            if "content-type" in upstream_resp.headers:
                resp_headers["content-type"] = upstream_resp.headers["content-type"]

            proxy_stats.record(upstream_resp.status_code, is_stream=is_stream, model=target_model)
            return HTTPResponse(
                body=err_body,
                status=upstream_resp.status_code,
                content_type=upstream_resp.headers.get("content-type", "application/json"),
                headers=resp_headers,
            )

        # -------------------------------------------------------------
        # 正常流式转发 (SSE)
        # -------------------------------------------------------------
        if is_stream:
            resp_content_type = upstream_resp.headers.get("content-type", "text/event-stream; charset=utf-8")
            response = await request.respond(
                status=200,
                content_type=resp_content_type,
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

            try:
                async for chunk in upstream_resp.aiter_bytes():
                    if chunk:
                        await response.send(chunk)
            finally:
                await upstream_resp.aclose()
                await client.aclose()

            proxy_stats.record(200, is_stream=True, model=target_model)
            await response.eof()
            return response

        # -------------------------------------------------------------
        # 正常非流式转发 (JSON)
        # -------------------------------------------------------------
        resp_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        await client.aclose()

        proxy_stats.record(200, is_stream=False, model=target_model)
        return HTTPResponse(
            body=resp_body,
            status=200,
            content_type=upstream_resp.headers.get("content-type", "application/json"),
        )

    except httpx.ConnectTimeout as exc:
        proxy_stats.record(504, is_stream=is_stream, model=target_model)
        return HTTPResponse(
            body=json.dumps(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"连接上游官方网关超时: {exc}"},
                }
            ),
            status=504,
            content_type="application/json",
        )
    except httpx.ConnectError as exc:
        proxy_stats.record(502, is_stream=is_stream, model=target_model)
        return HTTPResponse(
            body=json.dumps(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"连接上游官方网关失败: {exc}"},
                }
            ),
            status=502,
            content_type="application/json",
        )
    except Exception as exc:
        logger.exception("代理请求发生未预期异常: %s", exc)
        proxy_stats.record(500, is_stream=is_stream, model=target_model)
        return HTTPResponse(
            body=json.dumps(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"代理请求内部异常: {exc}"},
                }
            ),
            status=500,
            content_type="application/json",
        )


# 注册多格式路由支持：满足 Claude Code, Cursor, OpenCode, Aider, 原生端点
@proxy_bp.post("/v1/messages")
async def messages_v1(request: Request) -> HTTPResponse:
    return await _handle_messages_proxy(request)


@proxy_bp.post("/api/anthropic/v1/messages")
async def messages_anthropic(request: Request) -> HTTPResponse:
    return await _handle_messages_proxy(request)


@proxy_bp.post("/api/v1/ultra/anthropic/v1/messages")
async def messages_ultra(request: Request) -> HTTPResponse:
    return await _handle_messages_proxy(request)


@proxy_bp.post("/api/v1/ultra-zai/anthropic/v1/messages")
async def messages_ultra_zai(request: Request) -> HTTPResponse:
    return await _handle_messages_proxy(request)
