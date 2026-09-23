#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ZCode 官方 Coding Plan 套餐 OAuth 认证与上游对话模拟器
- 自动完成 OAuth 授权与 Coding Plan API Key 换取
- 修复 SSE 流式中文字符分包 UnicodeDecodeError
- 自动处理 BigModel / Z.ai 的业务凭据转换
"""

import json
import os
import secrets
import sys
import time
import urllib.parse
import uuid
import webbrowser
import requests

# ---------------------------- 配置常量 ----------------------------
CACHE_TOKEN_FILE = os.path.expanduser("~/.zcode_coding_plan_token.json")
DEFAULT_ZCODE_OAUTH_BASE_URL = "https://zcode.z.ai/api/v1"
DEFAULT_ZCODE_GATEWAY_ORIGIN = "https://zcode.z.ai"

# ---------------------------- 官方网关路由表 (1:1 镜像 official-coding-plan-gateway.ts) ----------------------------
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


def resolve_official_coding_plan_gateway_url(
    request_url: str,
    gateway_origin: str = DEFAULT_ZCODE_GATEWAY_ORIGIN,
) -> tuple[bool, str]:
    """
    1:1 对齐 official-coding-plan-gateway.ts:
    官方 Coding Plan 的模型请求经 ZCode 平台网关发送。
    Z.ai / BigModel Coding Plan 是 ZCode 的官方订阅套餐，模型请求统一发往 ZCode 平台网关，
    由平台完成套餐权益校验后转发到对应的模型服务。客户端把官方模型端点替换为对应的网关端点。
    """
    parsed = urllib.parse.urlparse(request_url)
    clean_target = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    for route in OFFICIAL_CODING_PLAN_GATEWAY_ROUTES:
        if clean_target.lower() == route["providerEndpoint"].lower():
            gateway_url = urllib.parse.urljoin(gateway_origin, route["gatewayPath"])
            if parsed.query:
                gateway_url = f"{gateway_url}?{parsed.query}"
            return True, gateway_url
    return False, request_url


ENDPOINTS = {
    "bigmodel": {
        "name": "BigModel (智谱国内版)",
        "provider_id": "bigmodel",
        # 官方 Provider 标准端点 (zcode-builtin.json 中配置的 baseUrl)
        "provider_endpoint": "https://open.bigmodel.cn/api/anthropic/v1/messages",
        "default_model": "GLM-5.3",
        "available_models": ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-4.7"],
    },
    "zai": {
        "name": "Z.ai (海外版)",
        "provider_id": "zai",
        "provider_endpoint": "https://api.z.ai/api/anthropic/v1/messages",
        "default_model": "GLM-5.3",
        "available_models": ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-4.7"],
    },
}

# ---------------------------- Agent 本地可用工具列表 ----------------------------
DEFAULT_TOOLS = [
    {
        "name": "web_search",
        "description": "联网实时搜索。当用户询问最新新闻、近期事件、实时信息或外部资料时必须调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或短语"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "bash",
        "description": "在本地系统执行终端命令行（例如查看系统信息、检查文件、运行脚本等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 命令"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取本地指定路径的文本文件内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对或相对路径"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_current_time",
        "description": "获取当前精确的日期、时间和时区。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


def execute_tool(name: str, args: dict) -> str:
    """在本地运行 Agent 请求调用的工具并返回文本"""
    try:
        if name == "web_search":
            query = args.get("query", "").strip()
            if not query:
                return "错误：搜索关键词不能为空"
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=5))
                    if not results:
                        return f"未检索到关于 '{query}' 的网络结果。"
                    items = []
                    for i, r in enumerate(results, 1):
                        items.append(f"[{i}] {r.get('title')}\n链接: {r.get('href')}\n摘要: {r.get('body')}\n")
                    return "\n".join(items)
            except Exception as e:
                return f"联网搜索执行异常: {e}"

        elif name == "bash":
            cmd = args.get("command", "").strip()
            if not cmd:
                return "错误：命令不能为空"
            import subprocess
            res = subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
            return res.stdout if res.stdout else f"(命令已执行完成，退出码 {res.returncode})"

        elif name == "read_file":
            path = os.path.expanduser(args.get("path", "").strip())
            if not os.path.exists(path):
                return f"错误：文件不存在: {path}"
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(20000)

        elif name == "get_current_time":
            return time.strftime("%Y年%m月%d日 %H:%M:%S (时区: Asia/Shanghai)")

        else:
            return f"错误：不支持的工具 '{name}'"
    except Exception as e:
        return f"工具执行异常: {e}"


class ZCodeOAuthClient:
    def __init__(self, provider: str = "bigmodel"):
        if provider not in ENDPOINTS:
            raise ValueError(f"不支持的 Provider: {provider}，可选: {list(ENDPOINTS.keys())}")
        self.provider = provider
        self.config = ENDPOINTS[provider]
        self.session = requests.Session()
        self.session_id = str(uuid.uuid4())

    def _get_base_headers(self) -> dict:
        """
        1:1 组装 ZCode 官方全部标头：
        1. 基础来源标头 (packages/shared/src/zcode-source-headers.ts)
        2. 操作系统与架构指纹 (apps/zcode-cli/packages/bootstrap/src/runtime-platform-headers.ts)
        3. 会话观测与归因标头 (apps/zcode-cli/packages/adapters/src/model/runner-attribution.ts)
        """
        import platform

        os_name = sys.platform  # 'linux', 'darwin', 'win32'
        os_cat = "macos" if os_name == "darwin" else ("windows" if os_name == "win32" else "linux")
        arch = platform.machine().lower()
        if arch in ("x86_64", "amd64"):
            arch = "x64"
        elif arch in ("aarch64", "arm64"):
            arch = "arm64"

        req_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())

        return {
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
            "x-request-id": req_id,
            "x-session-id": self.session_id,
            "x-zcode-trace-id": trace_id,
            "x-zcode-session-type": "main",
        }

    def load_cached_token(self) -> str | None:
        """从本地缓存加载有效 API Key"""
        if not os.path.exists(CACHE_TOKEN_FILE):
            return None
        try:
            with open(CACHE_TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # 支持多 Provider 分组存储以及兼容旧版单 Provider 格式
            item = data.get(self.provider) if isinstance(data, dict) and self.provider in data else data
            if not isinstance(item, dict):
                return None

            token = item.get("api_key") or item.get("access_token")
            # 过滤旧版本误存的 ZCode 平台 JWT (以 eyJhbGci 开头)
            if token and token.startswith("eyJhbGci"):
                return None

            expires_at = item.get("expires_at", 0)
            if item.get("provider") == self.provider and expires_at > time.time():
                expire_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))
                print(f"[✓] 命中本地持久化凭据: {CACHE_TOKEN_FILE}")
                print(f"[*] 账号已在有效期内 (有效至 {expire_str})，已跳过网页授权！")
                print(f"[*] API Key: {token[:8]}...{token[-8:]}")
                return token
        except Exception:
            return None
        return None

    def save_cached_token(
        self,
        api_key: str,
        zcode_jwt_token: str = None,
        oauth_access_token: str = None,
        expires_in: int = 86400 * 30,
    ):
        """缓存最终模型 API Key 以及 ZCode 平台 JWT 和 OAuth 令牌到本地文件 (~/.zcode_coding_plan_token.json)"""
        all_data = {}
        if os.path.exists(CACHE_TOKEN_FILE):
            try:
                with open(CACHE_TOKEN_FILE, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        all_data = content
            except Exception:
                all_data = {}

        expires_at = time.time() + expires_in
        record = {
            "provider": self.provider,
            "api_key": api_key,
            "zcode_jwt_token": zcode_jwt_token or all_data.get(self.provider, {}).get("zcode_jwt_token"),
            "oauth_access_token": oauth_access_token or all_data.get(self.provider, {}).get("oauth_access_token"),
            "expires_at": expires_at,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        all_data[self.provider] = record
        # 保留顶层字段兼容
        all_data["provider"] = self.provider
        all_data["api_key"] = api_key
        if record["zcode_jwt_token"]:
            all_data["zcode_jwt_token"] = record["zcode_jwt_token"]
        if record["oauth_access_token"]:
            all_data["oauth_access_token"] = record["oauth_access_token"]
        all_data["expires_at"] = expires_at

        try:
            with open(CACHE_TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(all_data, f, indent=2, ensure_ascii=False)
            print(f"[✓] 凭据已持久化保存至: {CACHE_TOKEN_FILE}（有效期 30 天，无需重复登录）")
        except Exception as e:
            print(f"[!] 写入缓存文件失败: {e}")

    def resolve_bigmodel_api_key(self, oauth_access_token: str) -> str:
        """
        根据 BigModel OAuth Access Token，向智谱业务接口换取实际的 Coding Plan API Key (id.secret)
        """
        headers = {
            # 关键：智谱业务接口直接传 token，不加 Bearer 前缀
            "Authorization": oauth_access_token,
            "Content-Type": "application/json",
            "User-Agent": "ZCode/3.14.0",
        }

        # 1. 获取用户信息与机构/项目 ID
        resp = self.session.get("https://bigmodel.cn/api/biz/customer/getCustomerInfo", headers=headers, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"获取客户信息失败: {resp.status_code} {resp.text}")
        res_json = resp.json()
        if res_json.get("code") != 0 and res_json.get("code") != 200:
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
        keys_resp = self.session.get(list_url, headers=headers, timeout=15)
        keys_resp.raise_for_status()
        keys_list = keys_resp.json().get("data") or []

        zcode_key = next((k for k in keys_list if k.get("name") == "zcode-api-key"), None)
        if not zcode_key:
            create_resp = self.session.post(list_url, headers=headers, json={"name": "zcode-api-key"}, timeout=15)
            create_resp.raise_for_status()
            zcode_key = create_resp.json().get("data") or {}

        api_key_id = zcode_key.get("apiKey")
        if not api_key_id:
            raise RuntimeError("无法获取 zcode-api-key 标识")

        # 3. 读取对应的完整 secretKey
        copy_url = f"{list_url}/copy/{urllib.parse.quote(api_key_id)}"
        copy_resp = self.session.get(copy_url, headers=headers, timeout=15)
        copy_resp.raise_for_status()
        secret_data = copy_resp.json().get("data") or {}
        secret_key = secret_data.get("secretKey") or ""

        if secret_key:
            return f"{api_key_id}.{secret_key}"
        return api_key_id

    def resolve_zai_api_key(self, oauth_access_token: str) -> str:
        """海外版 Z.ai 换取业务 API Key"""
        # 1. 换取业务 JWT
        biz_resp = self.session.post(
            "https://api.z.ai/api/auth/z/login",
            headers={"Content-Type": "application/json"},
            json={"token": oauth_access_token},
            timeout=10,
        )
        biz_json = biz_resp.json()
        biz_token = (biz_json.get("data") or {}).get("access_token")
        if not biz_token:
            raise RuntimeError(f"Z.ai 业务 Token 转换失败: {biz_resp.text}")

        # 2. 查找或创建 API Key
        headers = {
            "Authorization": f"Bearer {biz_token}",
            "Content-Type": "application/json",
            "User-Agent": "ZCode/3.14.0",
        }
        cust_resp = self.session.get("https://api.z.ai/api/biz/customer/getCustomerInfo", headers=headers, timeout=15)
        c_data = (cust_resp.json().get("data") or {})
        orgs = c_data.get("organizations") or []
        org_id = orgs[0].get("organizationId") if orgs else ""
        projs = orgs[0].get("projects") if orgs else []
        proj_id = projs[0].get("projectId") if projs else ""

        list_url = f"https://api.z.ai/api/biz/v1/organization/{org_id}/projects/{proj_id}/api_keys"
        keys_resp = self.session.get(list_url, headers=headers, timeout=15)
        keys_list = keys_resp.json().get("data") or []
        zcode_key = next((k for k in keys_list if k.get("name") == "zcode-api-key"), None)
        if not zcode_key:
            create_resp = self.session.post(list_url, headers=headers, json={"name": "zcode-api-key"}, timeout=15)
            zcode_key = create_resp.json().get("data") or {}

        api_key_id = zcode_key.get("apiKey")
        copy_url = f"{list_url}/copy/{urllib.parse.quote(api_key_id)}"
        copy_resp = self.session.get(copy_url, headers=headers, timeout=15)
        secret_key = (copy_resp.json().get("data") or {}).get("secretKey") or ""

        if secret_key:
            return f"{api_key_id}.{secret_key}"
        return api_key_id

    def login(self, force: bool = False) -> str:
        """执行官方标准的设备轮询 OAuth 登录并自动换取 API Key"""
        if not force:
            cached = self.load_cached_token()
            if cached:
                return cached

        poll_token = secrets.token_hex(32)
        headers = self._get_base_headers()
        headers["Authorization"] = f"Bearer {poll_token}"
        headers["Content-Type"] = "application/json"

        print(f"\n[*] 正在向 ZCode 申请 {self.config['name']} 登录流程...")
        init_resp = self.session.post(
            f"{DEFAULT_ZCODE_OAUTH_BASE_URL}/oauth/cli/init",
            headers=headers,
            json={"provider": self.config["provider_id"]},
            timeout=15,
        )
        if init_resp.status_code != 200:
            raise RuntimeError(f"申请登录流程失败: {init_resp.status_code} {init_resp.text}")

        res_json = init_resp.json()
        if res_json.get("code") != 0:
            raise RuntimeError(f"申请登录业务失败: {res_json.get('msg')}")

        data = res_json["data"]
        flow_id = data["flow_id"]
        auth_url = data["authorize_url"]
        poll_interval = max(1, data.get("poll_interval_sec", 2))
        expires_at = data.get("expires_at", time.time() + 300)

        print("\n" + "=" * 65)
        print(f"[*] 请在浏览器中完成授权（网页将正常显示登录成功，无需复制授权码）：")
        print(f"    {auth_url}")
        print("=" * 65 + "\n")

        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        print("[*] 正在等待浏览器端完成授权（每 2 秒自动检查一次）", end="", flush=True)

        poll_headers = {
            "Authorization": f"Bearer {poll_token}",
            "User-Agent": "ZCode/3.14.0",
        }

        while time.time() < expires_at:
            time.sleep(poll_interval)
            print(".", end="", flush=True)

            try:
                poll_resp = self.session.get(
                    f"{DEFAULT_ZCODE_OAUTH_BASE_URL}/oauth/cli/poll/{flow_id}",
                    headers=poll_headers,
                    timeout=10,
                )
            except Exception:
                continue

            if poll_resp.status_code == 200:
                poll_json = poll_resp.json()
                poll_data = poll_json.get("data") or {}
                status = poll_data.get("status")

                if status == "ready":
                    provider_id = self.config["provider_id"]
                    provider_info = poll_data.get(provider_id) or {}
                    oauth_access_token = (
                        provider_info.get("access_token")
                        or provider_info.get("accessToken")
                        or poll_data.get("accessToken")
                    )

                    user = poll_data.get("user") or {}
                    username = user.get("name") or user.get("email") or user.get("user_id") or "用户"

                    print(f"\n[✓] 网页授权成功！欢迎: {username}")
                    print("[*] 正在自动为您换取 Coding Plan 模型 API Key...")

                    if self.provider == "bigmodel":
                        api_key = self.resolve_bigmodel_api_key(oauth_access_token)
                    else:
                        api_key = self.resolve_zai_api_key(oauth_access_token)

                    print(f"[✓] 成功就绪！API Key: {api_key[:8]}...{api_key[-8:]}")
                    self.save_cached_token(
                        api_key=api_key,
                        zcode_jwt_token=poll_data.get("token"),
                        oauth_access_token=oauth_access_token,
                    )
                    return api_key

                elif status == "failed":
                    raise RuntimeError("登录已失败或被用户取消")

        raise TimeoutError("登录超时，请重新运行脚本。")

    def _build_system_context(self) -> str:
        """
        1:1 对齐 ZCode ContextBuilder (apps/zcode-cli/packages/core/src/context/builder.ts):
        为大模型注入工作区目录、Git 状态、操作系统、精确时间和 Agent 行为指导规范。
        """
        import platform
        import subprocess

        cwd = os.getcwd()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S (Asia/Shanghai)")
        git_info = "非 Git 仓库"
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            git_info = f"Git 分支: {branch}"
        except Exception:
            pass

        return f"""You are ZCode Assistant, an expert AI programming and software engineering agent powered by official Coding Plan ({self.config['name']}).

<environment_context>
- Current Workspace Directory: {cwd}
- Operating System: {platform.system()} {platform.machine()} (Kernel: {platform.release()})
- Python Environment: {platform.python_version()}
- Current Local Time: {now_str}
- Git Status: {git_info}
</environment_context>

<agent_guidelines>
1. You have built-in tools (web_search, bash, read_file, get_current_time). Proactively call them when relevant.
2. For real-time news, live dates, or external information, always call `web_search` or `get_current_time` rather than hallucinating.
3. When answering programming or technical questions, provide clear, concise, actionable solutions with clean Markdown.
4. Always respond in Chinese unless instructed otherwise.
</agent_guidelines>"""

    def chat_stream(
        self,
        api_key: str,
        messages: list[dict],
        model: str | None = None,
        max_agent_turns: int = 5,
    ) -> str:
        """
        完全模拟 ZCode 客户端行为：
        1. 专属 Ultra 网关动态重写
        2. 1:1 官方请求标头 (包括会话与环境指纹)
        3. 注入系统上下文 (System Prompt + Workspace Context)
        4. 完整的 Agent ReAct 工具调用循环 (Web 搜索、Bash、读文件、时间等)
        5. 安全的 SSE 深度思考与中文字符流式渲染
        """
        target_model = model or self.config["default_model"]
        raw_endpoint = self.config["provider_endpoint"]

        # 核心：1:1 对齐 official-coding-plan-gateway.ts 的动态重写
        via_gateway, actual_endpoint = resolve_official_coding_plan_gateway_url(raw_endpoint)

        headers = self._get_base_headers()
        headers.update({
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        })

        system_context = self._build_system_context()
        final_reply_content = []

        for agent_turn in range(max_agent_turns):
            payload = {
                "model": target_model,
                "system": system_context,
                "messages": messages[-30:],  # 滑动窗口：保留最近 30 条对话上下文，防止超长上下文溢出
                "tools": DEFAULT_TOOLS,
                "max_tokens": 4096,
                "stream": True,
            }

            resp = self.session.post(
                actual_endpoint,
                headers=headers,
                json=payload,
                stream=True,
                timeout=60,
            )

            if resp.status_code != 200:
                raise RuntimeError(f"模型请求失败，HTTP {resp.status_code}: {resp.text}")

            current_tool = None
            turn_tool_calls = []
            turn_text_chunks = []
            raw_buffer = b""
            is_thinking = False

            for chunk in resp.iter_content(chunk_size=512):
                if not chunk:
                    continue
                raw_buffer += chunk

                while b"\n" in raw_buffer:
                    line_bytes, raw_buffer = raw_buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event_data = json.loads(data_str)
                            event_type = event_data.get("type")

                            # 1. 块开始 (检测工具调用)
                            if event_type == "content_block_start":
                                cb = event_data.get("content_block", {})
                                if cb.get("type") == "tool_use":
                                    current_tool = {
                                        "id": cb.get("id"),
                                        "name": cb.get("name"),
                                        "json_buf": "",
                                    }

                            # 2. 块增量 (思考 / 正文 / 工具参数)
                            elif event_type == "content_block_delta":
                                delta = event_data.get("delta", {})
                                delta_type = delta.get("type")

                                if delta_type == "thinking_delta":
                                    is_thinking = True
                                    sys.stdout.write(f"\033[90m{delta.get('thinking', '')}\033[0m")
                                    sys.stdout.flush()

                                elif delta_type == "text_delta":
                                    if is_thinking:
                                        sys.stdout.write("\n\n")
                                        is_thinking = False
                                    text_chunk = delta.get("text", "")
                                    turn_text_chunks.append(text_chunk)
                                    final_reply_content.append(text_chunk)
                                    sys.stdout.write(text_chunk)
                                    sys.stdout.flush()

                                elif delta_type == "input_json_delta" and current_tool:
                                    current_tool["json_buf"] += delta.get("partial_json", "")

                            # 3. 块结束 (工具参数组装完毕)
                            elif event_type == "content_block_stop":
                                if current_tool:
                                    try:
                                        current_tool["input"] = json.loads(current_tool["json_buf"])
                                    except Exception:
                                        current_tool["input"] = {}
                                    turn_tool_calls.append(current_tool)
                                    current_tool = None

                        except json.JSONDecodeError:
                            continue

            # 如果本轮没有触发任何工具调用，说明已生成最终回答
            if not turn_tool_calls:
                sys.stdout.write("\n")
                return "".join(final_reply_content)

            # 组装 Assistant 回复到上下文
            asst_content = []
            turn_text = "".join(turn_text_chunks)
            if turn_text:
                asst_content.append({"type": "text", "text": turn_text})
            for tc in turn_tool_calls:
                asst_content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                })
            messages.append({"role": "assistant", "content": asst_content})

            # 本地并发或顺序执行工具
            tool_results = []
            for tc in turn_tool_calls:
                tool_name = tc["name"]
                tool_args = tc["input"]
                print(f"\n\033[33m🔍 [Agent 调用工具] {tool_name}({json.dumps(tool_args, ensure_ascii=False)})\033[0m")
                tool_output = execute_tool(tool_name, tool_args)
                summary = tool_output.strip().replace("\n", " ")[:100]
                print(f"\033[32m[✓] [工具执行完毕] {summary}...\033[0m\n")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": tool_output,
                })

            messages.append({"role": "user", "content": tool_results})
            print("[*] 正在向大模型反馈工具结果...", end="", flush=True)

        sys.stdout.write("\n")
        return "".join(final_reply_content)


# ---------------------------- 交互式对话主程序 ----------------------------
def main():
    force_relogin = "--relogin" in sys.argv or "-r" in sys.argv
    if force_relogin:
        print("[*] 检测到 --relogin 参数，将强制重新执行浏览器授权流程。")

    print("请选择 Coding Plan 账号所属服务：")
    print("  1. BigModel (智谱国内版)")
    print("  2. Z.ai (海外版)")
    choice = input("请输入选项 (1/2，默认 1): ").strip()
    provider = "zai" if choice == "2" else "bigmodel"

    client = ZCodeOAuthClient(provider=provider)
    api_key = client.login(force=force_relogin)

    print("\n" + "=" * 60)
    print(f"[*] 官方 Coding Plan 对话已就绪 ({client.config['name']})")
    print(f"[*] 套餐模型: {client.config['default_model']} (Coding Plan 套餐专属，无需余额)")
    print("[*] 输入你的问题按回车，输入 '/quota' 或 '/balance' 查看当前剩余配额，输入 'exit' 退出")
    print("=" * 60 + "\n")

    conversation_history = []

    while True:
        try:
            prompt = input("\n[User]: ").strip()
            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit"):
                print("退出对话。")
                break
            if prompt.lower() in ("/quota", "/balance", "quota", "配额"):
                try:
                    from zcode_coding_plan_quota import ZCodeQuotaClient
                    ZCodeQuotaClient(api_key=api_key, provider=provider).print_quota_dashboard()
                except Exception as qe:
                    print(f"\n[Error] 查询配额失败: {qe}")
                continue

            conversation_history.append({"role": "user", "content": prompt})

            print("\n[Assistant]: ", end="", flush=True)
            assistant_reply = client.chat_stream(api_key, conversation_history)

            conversation_history.append({"role": "assistant", "content": assistant_reply})

        except KeyboardInterrupt:
            print("\n对话已终止。")
            break
        except Exception as e:
            print(f"\n[Error] 请求异常: {e}")


if __name__ == "__main__":
    main()
