"""凭据管理与 OAuth 授权端点."""

from __future__ import annotations

import logging
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from ..zcode import ENDPOINTS
from .blueprint import bp
from .common import json_response

logger = logging.getLogger(__name__)


@bp.get("/credentials")
async def get_credentials(request: Request) -> BaseHTTPResponse:
    """获取当前凭据掩码摘要与授权状态."""
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    if not zcode_svc:
        return json_response({"error": {"message": "ZCode 服务未初始化"}}, status=500)

    summary = zcode_svc.credentials.masked_summary()
    summary["tokenFile"] = str(zcode_svc.token_file)
    return json_response({"success": True, "data": summary})


@bp.post("/credentials")
async def update_credentials(request: Request) -> BaseHTTPResponse:
    """手动更新 API Key 或 ZCode 认证令牌."""
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    if not zcode_svc:
        return json_response({"error": {"message": "ZCode 服务未初始化"}}, status=500)

    try:
        body: dict[str, Any] = request.json or {}
    except Exception:
        return json_response({"error": {"message": "请求体格式错误，需提供 JSON"}}, status=400)

    provider = body.get("provider")
    if provider and provider in ENDPOINTS:
        zcode_svc.credentials.provider = provider

    if "api_key" in body:
        api_key = str(body["api_key"]).strip()
        zcode_svc.credentials.api_key = api_key or None

    if "zcode_jwt_token" in body:
        jwt = str(body["zcode_jwt_token"]).strip()
        zcode_svc.credentials.zcode_jwt_token = jwt or None

    if "oauth_access_token" in body:
        oauth = str(body["oauth_access_token"]).strip()
        zcode_svc.credentials.oauth_access_token = oauth or None

    # 保存并更新
    zcode_svc.credentials.save_to_file(zcode_svc.token_file)

    # 清空所有可能使用了旧凭据的缓存
    for c in request.app.ctx.caches.values():
        c.clear()

    return json_response(
        {
            "success": True,
            "message": "凭据已更新并持久化保存！",
            "data": zcode_svc.credentials.masked_summary(),
        }
    )


@bp.post("/oauth/init")
async def oauth_init(request: Request) -> BaseHTTPResponse:
    """发起 ZCode CLI 官方 OAuth 授权流程."""
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    if not zcode_svc:
        return json_response({"error": {"message": "ZCode 服务未初始化"}}, status=500)

    try:
        body: dict[str, Any] = request.json or {}
    except Exception:
        body = {}

    provider = body.get("provider", "bigmodel")
    if provider not in ENDPOINTS:
        provider = "bigmodel"

    try:
        flow = await zcode_svc.init_oauth_flow(provider=provider)
        return json_response({"success": True, "data": flow})
    except Exception as exc:
        logger.exception("发起 OAuth 授权失败: %s", exc)
        return json_response({"error": {"message": f"发起授权失败: {exc}"}}, status=500)


@bp.get("/oauth/poll/<flow_id:str>")
async def oauth_poll(request: Request, flow_id: str) -> BaseHTTPResponse:
    """轮询 OAuth 授权完成状态."""
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    if not zcode_svc:
        return json_response({"error": {"message": "ZCode 服务未初始化"}}, status=500)

    try:
        result = await zcode_svc.poll_oauth_flow(flow_id)
        if result.get("status") == "ready":
            # 授权成功，清空旧缓存
            for c in request.app.ctx.caches.values():
                c.clear()
        return json_response({"success": True, "data": result})
    except Exception as exc:
        logger.exception("轮询 OAuth 授权失败: %s", exc)
        return json_response({"error": {"message": f"轮询授权异常: {exc}"}}, status=500)
