"""ZCode Coding Plan 官方网关协议、标头、凭据与重置卡服务 (1:1 对齐 ZCode 官方行为)."""

from __future__ import annotations

import json
import logging
import platform
import secrets
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_FILE = Path("~/.zcode_coding_plan_token.json").expanduser()
DEFAULT_ZCODE_OAUTH_BASE_URL = "https://zcode.z.ai/api/v1"
DEFAULT_ZCODE_GATEWAY_ORIGIN = "https://zcode.z.ai"

# 官方网关路由表 (1:1 镜像 official-coding-plan-gateway.ts)
OFFICIAL_CODING_PLAN_GATEWAY_ROUTES = [
    {
        "providerEndpoint": "https://open.bigmodel.cn/api/anthropic/v1/messages",
        "gatewayPath": "/api/v1/ultra/anthropic/v1/messages",
    },
    {
        "providerEndpoint": "https://api.z.ai/api/anthropic/v1/messages",
        "gatewayPath": "/api/v1/ultra-zai/anthropic/v1/messages",
    },
]

ENDPOINTS = {
    "bigmodel": {
        "name": "BigModel (智谱国内版)",
        "provider_id": "bigmodel",
        "provider_endpoint": "https://open.bigmodel.cn/api/anthropic/v1/messages",
        "gateway_path": "/api/v1/ultra/anthropic/v1/messages",
        "default_model": "GLM-5.3",
        "available_models": ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-4.7"],
    },
    "zai": {
        "name": "Z.ai (海外版)",
        "provider_id": "zai",
        "provider_endpoint": "https://api.z.ai/api/anthropic/v1/messages",
        "gateway_path": "/api/v1/ultra-zai/anthropic/v1/messages",
        "default_model": "GLM-5.3",
        "available_models": ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-4.7"],
    },
}

# 常见 Claude 客户端模型映射到 GLM 模型
MODEL_ALIAS_MAP = {
    "claude-3-5-sonnet": "GLM-5.3",
    "claude-3-5-sonnet-20240620": "GLM-5.3",
    "claude-3-5-sonnet-20241022": "GLM-5.3",
    "claude-3-7-sonnet": "GLM-5.3",
    "claude-3-7-sonnet-20250219": "GLM-5.3",
    "claude-sonnet-4-5": "GLM-5.3",
    "claude-opus-4.66": "GLM-5.3",
    "claude-3-opus-20240229": "GLM-5.3",
    "claude-3-5-haiku-20241022": "GLM-5.3-Flash",
    "claude-3-haiku-20240307": "GLM-5.3-Flash",
}


def map_model_name(requested_model: str | None, default_model: str = "GLM-5.3") -> str:
    """如果客户端传入 Claude 系列模型，智能平滑映射到 GLM 对应主力模型；若传入 GLM 模型则保留."""
    if not requested_model:
        return default_model

    req_clean = requested_model.strip()
    req_lower = req_clean.lower()

    if req_lower.startswith("glm-"):
        # 保持大写规范
        parts = req_clean.split("-", 1)
        return f"GLM-{parts[1]}" if len(parts) > 1 else req_clean

    for alias, target in MODEL_ALIAS_MAP.items():
        if req_lower == alias or req_lower.startswith(alias):
            return target

    # 其他未识别的非 GLM 模型，默认回退到主力 GLM-5.3
    if not req_lower.startswith("glm"):
        return default_model

    return req_clean


def resolve_official_coding_plan_gateway_url(
    request_url: str,
    gateway_origin: str = DEFAULT_ZCODE_GATEWAY_ORIGIN,
) -> tuple[bool, str]:
    """1:1 对齐 official-coding-plan-gateway.ts 的动态重写."""
    parsed = urllib.parse.urlparse(request_url)
    clean_target = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    for route in OFFICIAL_CODING_PLAN_GATEWAY_ROUTES:
        if clean_target.lower() == route["providerEndpoint"].lower():
            gateway_url = urllib.parse.urljoin(gateway_origin, route["gatewayPath"])
            if parsed.query:
                gateway_url = f"{gateway_url}?{parsed.query}"
            return True, gateway_url
    return False, request_url


@dataclass
class ZCodeCredentials:
    """持久化与内存中的 ZCode 凭据."""

    provider: str = "bigmodel"
    api_key: str | None = None
    zcode_jwt_token: str | None = None
    oauth_access_token: str | None = None
    expires_at: float = 0.0
    updated_at: str = ""

    @classmethod
    def load_from_file(cls, path: Path | str = DEFAULT_TOKEN_FILE) -> ZCodeCredentials:
        token_path = Path(path).expanduser()
        if not token_path.exists():
            return cls()

        try:
            with token_path.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return cls()

            provider = data.get("provider", "bigmodel")
            item = data.get(provider) if provider in data and isinstance(data[provider], dict) else data

            api_key = item.get("api_key") or item.get("access_token") or data.get("api_key")
            # 过滤误存的 ZCode 平台 JWT
            if api_key and api_key.startswith("eyJhbGci"):
                api_key = None

            zcode_jwt = (
                item.get("zcode_jwt_token")
                or data.get("zcode_jwt_token")
                or item.get("token")
                or data.get("token")
            )
            oauth_token = (
                item.get("oauth_access_token")
                or data.get("oauth_access_token")
                or item.get("access_token")
                or data.get("access_token")
            )
            expires_at = float(item.get("expires_at") or data.get("expires_at") or 0)
            updated_at = item.get("updated_at") or data.get("updated_at") or ""

            return cls(
                provider=provider,
                api_key=api_key,
                zcode_jwt_token=zcode_jwt,
                oauth_access_token=oauth_token,
                expires_at=expires_at,
                updated_at=updated_at,
            )
        except Exception as e:
            logger.warning("读取凭据文件异常: %s", e)
            return cls()

    def save_to_file(self, path: Path | str = DEFAULT_TOKEN_FILE) -> None:
        token_path = Path(path).expanduser()
        all_data: dict[str, Any] = {}
        if token_path.exists():
            try:
                with token_path.open(encoding="utf-8") as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        all_data = content
            except Exception:
                all_data = {}

        expires_at = self.expires_at or (time.time() + 86400 * 30)
        updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

        record = {
            "provider": self.provider,
            "api_key": self.api_key,
            "zcode_jwt_token": self.zcode_jwt_token,
            "oauth_access_token": self.oauth_access_token,
            "expires_at": expires_at,
            "updated_at": updated_at,
        }

        all_data[self.provider] = record
        all_data["provider"] = self.provider
        all_data["api_key"] = self.api_key
        if self.zcode_jwt_token:
            all_data["zcode_jwt_token"] = self.zcode_jwt_token
        if self.oauth_access_token:
            all_data["oauth_access_token"] = self.oauth_access_token
        all_data["expires_at"] = expires_at

        token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = token_path.with_suffix(".tmp")
        with tmp_file.open("w", encoding="utf-8") as f:
            json.dump(all_data, f, indent=2, ensure_ascii=False)
        tmp_file.replace(token_path)

        self.expires_at = expires_at
        self.updated_at = updated_at

    @property
    def is_valid(self) -> bool:
        if not self.api_key:
            return False
        return not (self.expires_at and self.expires_at < time.time())

    @property
    def has_reset_capability(self) -> bool:
        return bool(self.zcode_jwt_token and self.oauth_access_token)

    def masked_summary(self) -> dict[str, Any]:
        api_key_masked = ""
        if self.api_key:
            api_key_masked = f"{self.api_key[:6]}...{self.api_key[-6:]}" if len(self.api_key) > 12 else "***"

        expire_str = ""
        if self.expires_at:
            expire_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.expires_at))

        return {
            "provider": self.provider,
            "configured": bool(self.api_key),
            "apiKeyMasked": api_key_masked,
            "hasResetCapability": self.has_reset_capability,
            "expiresAt": expire_str,
            "updatedAt": self.updated_at,
        }


def build_zcode_headers(
    api_key: str,
    *,
    req_id: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """1:1 组装 ZCode 官方全部标头：

    1. 基础来源标头 (packages/shared/src/zcode-source-headers.ts)
    2. 操作系统与架构指纹 (apps/zcode-cli/packages/bootstrap/src/runtime-platform-headers.ts)
    3. 会话观测与归因标头 (apps/zcode-cli/packages/adapters/src/model/runner-attribution.ts)
    4. Anthropic 协议与认证标头
    """
    os_name = sys.platform  # 'linux', 'darwin', 'win32'
    os_cat = "macos" if os_name == "darwin" else ("windows" if os_name == "win32" else "linux")
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        arch = "x64"
    elif arch in ("aarch64", "arm64"):
        arch = "arm64"

    req_id_val = req_id or str(uuid.uuid4())
    session_id_val = session_id or str(uuid.uuid4())
    trace_id_val = trace_id or str(uuid.uuid4())

    headers = {
        # 1. 客户端身份标头 (zcode-source-headers.ts)
        "User-Agent": "ZCode/3.14.0",
        "HTTP-Referer": "https://zcode.z.ai",
        "X-Title": "Z Code@electron",
        "X-ZCode-App-Version": "3.14.0",
        "X-Release-Channel": "production",
        "X-Client-Language": "zh-CN",
        "X-Client-Timezone": "Asia/Shanghai",
        "X-ZCode-Agent": "glm",
        # 2. 系统与设备架构指纹 (runtime-platform-headers.ts)
        "X-Platform": f"{os_name}-{arch}",
        "X-Os-Category": os_cat,
        "X-Os-Version": platform.release(),
        # 3. 观测与归因标头 (runner-attribution.ts)
        "x-request-id": req_id_val,
        "x-session-id": session_id_val,
        "x-zcode-trace-id": trace_id_val,
        "x-zcode-session-type": "main",
        # 4. 认证与协议标头
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    if extra_headers:
        for k, v in extra_headers.items():
            k_lower = k.lower()
            if k_lower in ("anthropic-version", "anthropic-beta", "accept"):
                headers[k] = v

    return headers


class ZCodeService:
    """ZCode Coding Plan 官方重置卡与 OAuth 服务."""

    def __init__(
        self,
        credentials: ZCodeCredentials | None = None,
        token_file: Path | str = DEFAULT_TOKEN_FILE,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.token_file = Path(token_file).expanduser()
        self.credentials = credentials or ZCodeCredentials.load_from_file(self.token_file)
        self.transport = transport
        self.active_oauth_flows: dict[str, dict[str, Any]] = {}

    def reload_credentials(self) -> ZCodeCredentials:
        self.credentials = ZCodeCredentials.load_from_file(self.token_file)
        return self.credentials

    async def get_reset_status(self) -> dict[str, Any]:
        """获取额度重置卡状态 (5小时重置卡、每周重置卡、历史使用记录)

        对齐 BigModelUsageQuotaProvider.getCodingPlanResetStatus:
        GET https://zcode.z.ai/api/v1/coding-plan/reset/status
        """
        if not self.credentials.zcode_jwt_token or not self.credentials.oauth_access_token:
            return {
                "available": False,
                "reason": "缺少 ZCode 平台认证令牌，无法查询重置卡。可通过网页端【启动授权】或手动填写令牌激活。",
            }

        jwt = self.credentials.zcode_jwt_token
        auth_header = jwt if jwt.startswith("Bearer ") else f"Bearer {jwt}"

        headers = {
            "Authorization": auth_header,
            "X-Bigmodel-Authorization": self.credentials.oauth_access_token,
            "Bigmodel-Target-Type": "PERSONAL",
            "User-Agent": "ZCode/3.14.0",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0, transport=self.transport) as client:
            resp = await client.get(f"{DEFAULT_ZCODE_GATEWAY_ORIGIN}/api/v1/coding-plan/reset/status", headers=headers)
            if resp.status_code != 200:
                return {
                    "available": False,
                    "reason": f"重置卡接口响应 HTTP {resp.status_code}: {resp.text[:120]}",
                }

            res_json = resp.json()
            if res_json.get("code") != 0:
                return {
                    "available": False,
                    "reason": res_json.get("msg", "获取重置卡状态失败"),
                }

            data = res_json.get("data") or {}
            return {
                "available": True,
                "five_hour_resets": data.get("available_five_hour_resets", []),
                "week_resets": data.get("available_week_resets", []),
                "latest_five_hour_history": data.get("latest_five_hour_reset_history"),
                "latest_week_history": data.get("latest_week_reset_history"),
                "has_unread_history": data.get("has_unread_history", False),
            }

    async def use_reset(self, reset_type: str) -> dict[str, Any]:
        """核销一张额度重置卡 (FIVE_HOUR 或 WEEK)

        对齐 BigModelUsageQuotaProvider.useCodingPlanReset:
        POST https://zcode.z.ai/api/v1/coding-plan/reset/use
        """
        if not self.credentials.zcode_jwt_token or not self.credentials.oauth_access_token:
            raise RuntimeError("缺少 ZCode 平台凭证，无法核销重置卡。")

        reset_type = reset_type.upper()
        if reset_type not in ("FIVE_HOUR", "WEEK"):
            raise ValueError("重置类型必须为 'FIVE_HOUR' 或 'WEEK'")

        jwt = self.credentials.zcode_jwt_token
        auth_header = jwt if jwt.startswith("Bearer ") else f"Bearer {jwt}"

        headers = {
            "Authorization": auth_header,
            "X-Bigmodel-Authorization": self.credentials.oauth_access_token,
            "Bigmodel-Target-Type": "PERSONAL",
            "User-Agent": "ZCode/3.14.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        body = {
            "idempotency_key": uuid.uuid4().hex,
            "reset_type": reset_type,
        }

        async with httpx.AsyncClient(timeout=15.0, transport=self.transport) as client:
            resp = await client.post(
                f"{DEFAULT_ZCODE_GATEWAY_ORIGIN}/api/v1/coding-plan/reset/use",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"核销请求失败 HTTP {resp.status_code}: {resp.text}")

            res_json = resp.json()
            if res_json.get("code") != 0:
                raise RuntimeError(res_json.get("msg") or "核销重置卡失败")

            return res_json

    async def resolve_bigmodel_api_key(self, client: httpx.AsyncClient, oauth_access_token: str) -> str:
        """根据 BigModel OAuth Access Token，向智谱业务接口换取实际的 Coding Plan API Key (id.secret)."""
        headers = {
            "Authorization": oauth_access_token,
            "Content-Type": "application/json",
            "User-Agent": "ZCode/3.14.0",
        }

        # 1. 获取用户信息与机构/项目 ID
        resp = await client.get("https://bigmodel.cn/api/biz/customer/getCustomerInfo", headers=headers, timeout=15.0)
        if resp.status_code != 200:
            raise RuntimeError(f"获取客户信息失败: {resp.status_code} {resp.text}")
        res_json = resp.json()
        if res_json.get("code") not in (0, 200):
            raise RuntimeError(f"获取客户信息失败: {res_json.get('msg')}")

        c_data = res_json.get("data") or {}
        orgs = c_data.get("organizations") or []
        if not orgs:
            raise RuntimeError(f"未找到可用的机构信息: {c_data}")
        org = next((o for o in orgs if "默认机构" in (o.get("organizationName") or "")), orgs[0])
        org_id = org.get("organizationId")

        projs = org.get("projects") or []
        if not projs:
            raise RuntimeError("未找到可用的项目信息")
        proj = next((p for p in projs if "默认项目" in (p.get("projectName") or "")), projs[0])
        proj_id = proj.get("projectId")

        # 2. 查询或创建 zcode-api-key
        list_url = f"https://bigmodel.cn/api/biz/v1/organization/{org_id}/projects/{proj_id}/api_keys"
        keys_resp = await client.get(list_url, headers=headers, timeout=15.0)
        keys_resp.raise_for_status()
        keys_list = keys_resp.json().get("data") or []

        zcode_key = next((k for k in keys_list if k.get("name") == "zcode-api-key"), None)
        if not zcode_key:
            create_resp = await client.post(list_url, headers=headers, json={"name": "zcode-api-key"}, timeout=15.0)
            create_resp.raise_for_status()
            zcode_key = create_resp.json().get("data") or {}

        api_key_id = zcode_key.get("apiKey")
        if not api_key_id:
            raise RuntimeError("无法获取 zcode-api-key 标识")

        # 3. 读取对应的完整 secretKey
        copy_url = f"{list_url}/copy/{urllib.parse.quote(api_key_id)}"
        copy_resp = await client.get(copy_url, headers=headers, timeout=15.0)
        copy_resp.raise_for_status()
        secret_data = copy_resp.json().get("data") or {}
        secret_key = secret_data.get("secretKey") or ""

        if secret_key:
            return f"{api_key_id}.{secret_key}"
        return api_key_id

    async def resolve_zai_api_key(self, client: httpx.AsyncClient, oauth_access_token: str) -> str:
        """海外版 Z.ai 换取业务 API Key."""
        biz_resp = await client.post(
            "https://api.z.ai/api/auth/z/login",
            headers={"Content-Type": "application/json"},
            json={"token": oauth_access_token},
            timeout=10.0,
        )
        biz_json = biz_resp.json()
        biz_token = (biz_json.get("data") or {}).get("access_token")
        if not biz_token:
            raise RuntimeError(f"Z.ai 业务 Token 转换失败: {biz_resp.text}")

        headers = {
            "Authorization": f"Bearer {biz_token}",
            "Content-Type": "application/json",
            "User-Agent": "ZCode/3.14.0",
        }
        cust_resp = await client.get("https://api.z.ai/api/biz/customer/getCustomerInfo", headers=headers, timeout=15.0)
        c_data = cust_resp.json().get("data") or {}
        orgs = c_data.get("organizations") or []
        org_id = orgs[0].get("organizationId") if orgs else ""
        projs = orgs[0].get("projects") if orgs else []
        proj_id = projs[0].get("projectId") if projs else ""

        list_url = f"https://api.z.ai/api/biz/v1/organization/{org_id}/projects/{proj_id}/api_keys"
        keys_resp = await client.get(list_url, headers=headers, timeout=15.0)
        keys_list = keys_resp.json().get("data") or []
        zcode_key = next((k for k in keys_list if k.get("name") == "zcode-api-key"), None)
        if not zcode_key:
            create_resp = await client.post(list_url, headers=headers, json={"name": "zcode-api-key"}, timeout=15.0)
            zcode_key = create_resp.json().get("data") or {}

        api_key_id = zcode_key.get("apiKey")
        copy_url = f"{list_url}/copy/{urllib.parse.quote(api_key_id)}"
        copy_resp = await client.get(copy_url, headers=headers, timeout=15.0)
        secret_key = (copy_resp.json().get("data") or {}).get("secretKey") or ""

        if secret_key:
            return f"{api_key_id}.{secret_key}"
        return api_key_id

    async def init_oauth_flow(self, provider: str = "bigmodel") -> dict[str, Any]:
        """初始化 ZCode 设备轮询登录流程."""
        if provider not in ENDPOINTS:
            raise ValueError(f"不支持的 Provider: {provider}")

        poll_token = secrets.token_hex(32)
        base_headers = build_zcode_headers("dummy")
        base_headers["Authorization"] = f"Bearer {poll_token}"

        async with httpx.AsyncClient(timeout=15.0, transport=self.transport) as client:
            resp = await client.post(
                f"{DEFAULT_ZCODE_OAUTH_BASE_URL}/oauth/cli/init",
                headers=base_headers,
                json={"provider": provider},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"初始化登录流程失败: {resp.status_code} {resp.text}")

            res_json = resp.json()
            if res_json.get("code") != 0:
                raise RuntimeError(f"申请登录失败: {res_json.get('msg')}")

            data = res_json["data"]
            flow_id = data["flow_id"]
            auth_url = data["authorize_url"]
            poll_interval = max(1, data.get("poll_interval_sec", 2))
            expires_at = data.get("expires_at", time.time() + 300)

            flow_record = {
                "flow_id": flow_id,
                "authorize_url": auth_url,
                "poll_token": poll_token,
                "poll_interval": poll_interval,
                "expires_at": expires_at,
                "provider": provider,
                "status": "pending",
                "user": None,
                "error": None,
            }
            self.active_oauth_flows[flow_id] = flow_record

            return {
                "flow_id": flow_id,
                "authorize_url": auth_url,
                "poll_interval": poll_interval,
                "expires_at": expires_at,
            }

    async def poll_oauth_flow(self, flow_id: str) -> dict[str, Any]:
        """轮询 OAuth 授权状态."""
        flow = self.active_oauth_flows.get(flow_id)
        if not flow:
            return {"status": "expired", "message": "授权会话不存在或已超时"}

        if flow["status"] == "ready":
            return {"status": "ready", "user": flow["user"]}

        if flow["status"] == "failed":
            return {"status": "failed", "message": flow.get("error", "授权失败")}

        if time.time() > flow["expires_at"]:
            self.active_oauth_flows.pop(flow_id, None)
            return {"status": "expired", "message": "授权超时，请重试"}

        poll_headers = {
            "Authorization": f"Bearer {flow['poll_token']}",
            "User-Agent": "ZCode/3.14.0",
        }

        async with httpx.AsyncClient(timeout=10.0, transport=self.transport) as client:
            resp = await client.get(
                f"{DEFAULT_ZCODE_OAUTH_BASE_URL}/oauth/cli/poll/{flow_id}",
                headers=poll_headers,
            )
            if resp.status_code != 200:
                return {"status": "pending"}

            poll_json = resp.json()
            poll_data = poll_json.get("data") or {}
            status = poll_data.get("status")

            if status == "ready":
                provider_id = flow["provider"]
                provider_info = poll_data.get(provider_id) or {}
                oauth_access_token = (
                    provider_info.get("access_token")
                    or provider_info.get("accessToken")
                    or poll_data.get("accessToken")
                )

                user = poll_data.get("user") or {}
                username = user.get("name") or user.get("email") or user.get("user_id") or "用户"
                flow["user"] = username

                # 换取模型 API Key
                if provider_id == "bigmodel":
                    api_key = await self.resolve_bigmodel_api_key(client, oauth_access_token)
                else:
                    api_key = await self.resolve_zai_api_key(client, oauth_access_token)

                # 更新并持久化凭据
                self.credentials.provider = provider_id
                self.credentials.api_key = api_key
                self.credentials.zcode_jwt_token = poll_data.get("token")
                self.credentials.oauth_access_token = oauth_access_token
                self.credentials.expires_at = time.time() + 86400 * 30
                self.credentials.save_to_file(self.token_file)

                flow["status"] = "ready"
                return {"status": "ready", "user": username}

            if status == "failed":
                flow["status"] = "failed"
                flow["error"] = "登录已失败或被用户取消"
                return {"status": "failed", "message": flow["error"]}

            return {"status": "pending"}
