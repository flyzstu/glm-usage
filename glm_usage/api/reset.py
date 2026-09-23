"""额度重置卡管理接口 (对齐 ZCode 官方重置卡查询与核销)."""

from __future__ import annotations

import logging
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from .blueprint import bp
from .common import json_response

logger = logging.getLogger(__name__)


@bp.get("/reset/status")
async def reset_status(request: Request) -> BaseHTTPResponse:
    """查询当前可用额度重置卡 (5小时重置卡、每周重置卡及核销历史)."""
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    if not zcode_svc:
        return json_response(
            {"success": False, "data": {"available": False, "reason": "ZCode 服务未初始化"}},
            status=500,
        )

    try:
        data = await zcode_svc.get_reset_status()
        return json_response({"success": True, "data": data})
    except Exception as exc:
        logger.exception("获取重置卡状态失败: %s", exc)
        return json_response(
            {"success": False, "data": {"available": False, "reason": str(exc)}},
            status=500,
        )


@bp.post("/reset/use")
async def reset_use(request: Request) -> BaseHTTPResponse:
    """核销一张额度重置卡 (FIVE_HOUR 或 WEEK)."""
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    if not zcode_svc:
        return json_response({"error": {"message": "ZCode 服务未初始化"}}, status=500)

    try:
        body: dict[str, Any] = request.json or {}
    except Exception:
        return json_response({"error": {"message": "请求体格式错误，需提供 JSON"}}, status=400)

    reset_type = str(body.get("reset_type") or body.get("resetType") or "").strip().upper()
    if reset_type not in ("FIVE_HOUR", "WEEK"):
        return json_response(
            {"error": {"message": "reset_type 必须为 'FIVE_HOUR' 或 'WEEK'"}},
            status=400,
        )

    try:
        result = await zcode_svc.use_reset(reset_type)

        # 核销成功后，清除本地配额缓存，确保后续查询直接回源读取最新清零后的额度
        quota_cache = request.app.ctx.caches.get("quota")
        if quota_cache:
            quota_cache.clear()

        # 同时也清除 account 缓存中的 quotaResets
        account_cache = request.app.ctx.caches.get("account")
        if account_cache:
            account_cache.clear()

        return json_response(
            {
                "success": True,
                "message": f"成功核销一张 {reset_type} 额度重置卡！配额已恢复。",
                "result": result,
            }
        )
    except Exception as exc:
        logger.error("核销重置卡失败: %s", exc)
        return json_response(
            {"error": {"message": f"核销重置卡失败: {exc}"}},
            status=500,
        )
