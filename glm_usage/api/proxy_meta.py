"""透明代理状态与快捷配置元数据接口."""

from __future__ import annotations

from sanic import Request
from sanic.response import BaseHTTPResponse

from ..zcode import DEFAULT_ZCODE_GATEWAY_ORIGIN, ENDPOINTS
from .blueprint import bp
from .common import json_response
from .proxy import proxy_stats


@bp.get("/proxy/status")
async def proxy_status(request: Request) -> BaseHTTPResponse:
    """获取透明代理运行状态、网关路由与快捷客户端配置指南."""
    zcode_svc = getattr(request.app.ctx, "zcode", None)
    provider = getattr(zcode_svc.credentials, "provider", "bigmodel") if zcode_svc else "bigmodel"
    endpoint_cfg = ENDPOINTS.get(provider, ENDPOINTS["bigmodel"])

    scheme = request.scheme or "http"
    host = request.host

    base_proxy_url = f"{scheme}://{host}"
    messages_endpoint = f"{base_proxy_url}/v1/messages"
    ultra_endpoint = f"{DEFAULT_ZCODE_GATEWAY_ORIGIN}{endpoint_cfg['gateway_path']}"

    # 快捷配置示例
    snippets = {
        "claude_code": {
            "title": "Claude Code CLI",
            "env": f'export ANTHROPIC_BASE_URL="{base_proxy_url}"\nexport ANTHROPIC_API_KEY="dummy"\nclaude',
        },
        "opencode": {
            "title": "OpenCode / Aider / Cursor",
            "base_url": base_proxy_url,
            "model": endpoint_cfg["default_model"],
            "notes": "支持 GLM-5.3, GLM-5.3-Flash, GLM-5.2 等模型，或传入 claude-3-7-sonnet 自动映射",
        },
        "curl": {
            "title": "Curl 测试调用",
            "command": (
                f'curl -X POST "{messages_endpoint}" \\\n'
                f'  -H "Content-Type: application/json" \\\n'
                f'  -d \'{{"model": "GLM-5.3", "messages": [{{"role": "user", "content": "hi"}}], "max_tokens": 50}}\''
            ),
        },
    }

    return json_response(
        {
            "success": True,
            "data": {
                "status": "ready" if (zcode_svc and zcode_svc.credentials.is_valid) else "unconfigured",
                "provider": provider,
                "providerName": endpoint_cfg["name"],
                "targetGatewayUrl": ultra_endpoint,
                "localEndpoints": {
                    "v1Messages": messages_endpoint,
                    "anthropicMessages": f"{base_proxy_url}/api/anthropic/v1/messages",
                    "ultraGateway": f"{base_proxy_url}{endpoint_cfg['gateway_path']}",
                    "models": f"{base_proxy_url}/v1/models",
                },
                "stats": proxy_stats.snapshot(),
                "availableModels": endpoint_cfg["available_models"],
                "snippets": snippets,
            },
        }
    )
