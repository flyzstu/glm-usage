#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ZCode Coding Plan 配额与重置卡查询客户端 (Python 实现)
- 1:1 对齐 ZCode 源码中的配额与重置卡逻辑:
  1. 配额查询: BigModelUsageQuotaProvider & codingPlanQuotaPresentation
  2. 重置卡状态: /api/v1/coding-plan/reset/status
  3. 重置卡核销: /api/v1/coding-plan/reset/use
- 自动加载 ~/.zcode_coding_plan_token.json 凭据
- 支持查询:
  1. 实时剩余额度 (5小时滑动窗口限额、每周限额、每月工具限额)
  2. 套餐订阅详情 (套餐名称、计费周期、有效期、自动续费)
  3. 额度重置卡 (5小时重置卡、每周重置卡、可用数量、有效期与使用历史)
  4. 近期 Token 与模型消耗统计概览
"""

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

CACHE_TOKEN_FILE = os.path.expanduser("~/.zcode_coding_plan_token.json")
ZCODE_BASE_URL = "https://zcode.z.ai"

# 官方配额与用量服务端点
BASE_URLS = {
    "bigmodel": "https://bigmodel.cn",
    "zai": "https://api.z.ai",
}

# 配额类型映射 (对齐 packages/ui/src/lib/codingPlanQuotaPresentation.ts)
# unit=3, number=5 -> 5小时窗口
# unit=6, number=1 -> 每周限额
# unit=5, number=1 (TIME_LIMIT) -> 每月工具调用配额
LIMIT_UNIT_MAP = {
    (3, 5): "5小时滑动窗口额度 (5-Hour Window)",
    (6, 1): "每周总限额 (Weekly Quota)",
    (5, 1): "每月工具调用限额 (Monthly Tools)",
}


class ZCodeQuotaClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        provider: str = "bigmodel",
        token_file: str = CACHE_TOKEN_FILE,
    ):
        self.provider = provider
        self.token_file = token_file
        self.zcode_jwt_token: Optional[str] = None
        self.oauth_access_token: Optional[str] = None
        self.api_key = api_key or self._load_credentials()

        if not self.api_key:
            raise ValueError(
                f"未找到有效 API Key！请先运行 python3 scripts/zcode_coding_plan_client.py 完成登录，"
                f"或在启动时通过 --api-key 传入。"
            )
        self.base_url = BASE_URLS.get(self.provider, BASE_URLS["bigmodel"])

    def _load_credentials(self) -> Optional[str]:
        if not os.path.exists(self.token_file):
            return None
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.provider = data.get("provider", self.provider)
                provider_data = data.get(self.provider, {}) if isinstance(data.get(self.provider), dict) else {}

                self.zcode_jwt_token = (
                    data.get("zcode_jwt_token") or provider_data.get("zcode_jwt_token")
                )
                self.oauth_access_token = (
                    data.get("oauth_access_token") or provider_data.get("oauth_access_token")
                )
                return data.get("api_key") or provider_data.get("api_key")
        except Exception:
            return None

    def _request(
        self,
        base_url: str,
        path: str,
        headers: Dict[str, str],
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        data = None
        req_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) ZCode/3.14.0",
            "Accept": "application/json",
            **headers,
        }

        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {e.reason} - {error_body}")
        except Exception as e:
            raise RuntimeError(f"请求失败 [{url}]: {str(e)}")

    def get_quota_limits(self) -> Dict[str, Any]:
        """
        获取配额上限与剩余量
        对齐 BigModelUsageQuotaProvider.fetchQuota:
        GET https://bigmodel.cn/api/monitor/usage/quota/limit
        Header: authorization: <api_key> (不带 Bearer)
        """
        res = self._request(
            self.base_url,
            "/api/monitor/usage/quota/limit",
            headers={"authorization": self.api_key},
        )
        if not res.get("success", False) and res.get("code") != 200:
            raise RuntimeError(f"获取配额失败: {res.get('msg', '未知错误')}")
        return res.get("data", {})

    def get_subscriptions(self) -> List[Dict[str, Any]]:
        """
        获取当前 Coding Plan 订阅列表
        对齐 fetchPersonalCodingPlanEntitlement:
        GET https://bigmodel.cn/api/biz/subscription/list
        """
        res = self._request(
            self.base_url,
            "/api/biz/subscription/list",
            headers={"authorization": self.api_key},
        )
        if not res.get("success", False) and res.get("code") != 200:
            return []
        return res.get("data", [])

    def get_reset_status(self) -> Dict[str, Any]:
        """
        获取额度重置卡状态 (5小时重置卡、每周重置卡、历史使用记录)
        对齐 BigModelUsageQuotaProvider.getCodingPlanResetStatus:
        GET https://zcode.z.ai/api/v1/coding-plan/reset/status
        Headers:
          Authorization: Bearer <zcode_jwt_token>
          X-Bigmodel-Authorization: <oauth_access_token>
          Bigmodel-Target-Type: PERSONAL
        """
        if not self.zcode_jwt_token or not self.oauth_access_token:
            return {
                "available": False,
                "reason": "缺少 ZCode 平台认证令牌 (运行 python3 zcode_coding_plan_client.py --relogin 即可激活重置卡读取)",
            }

        jwt = self.zcode_jwt_token
        auth_header = jwt if jwt.startswith("Bearer ") else f"Bearer {jwt}"

        headers = {
            "Authorization": auth_header,
            "X-Bigmodel-Authorization": self.oauth_access_token,
            "Bigmodel-Target-Type": "PERSONAL",
        }

        try:
            res = self._request(
                ZCODE_BASE_URL,
                "/api/v1/coding-plan/reset/status",
                headers=headers,
            )
            if res.get("code") != 0:
                return {"available": False, "reason": res.get("msg", "获取重置卡状态失败")}
            data = res.get("data") or {}
            return {
                "available": True,
                "five_hour_resets": data.get("available_five_hour_resets", []),
                "week_resets": data.get("available_week_resets", []),
                "latest_five_hour_history": data.get("latest_five_hour_reset_history"),
                "latest_week_history": data.get("latest_week_reset_history"),
                "has_unread_history": data.get("has_unread_history", False),
            }
        except Exception as e:
            return {"available": False, "reason": str(e)}

    def use_reset(self, reset_type: str) -> Dict[str, Any]:
        """
        使用一张额度重置卡 (FIVE_HOUR 或 WEEK)
        对齐 BigModelUsageQuotaProvider.useCodingPlanReset:
        POST https://zcode.z.ai/api/v1/coding-plan/reset/use
        Body: {"idempotency_key": "...", "reset_type": "FIVE_HOUR"|"WEEK"}
        """
        if not self.zcode_jwt_token or not self.oauth_access_token:
            raise RuntimeError("缺少 ZCode 平台凭证，无法核销重置卡。")

        reset_type = reset_type.upper()
        if reset_type not in ("FIVE_HOUR", "WEEK"):
            raise ValueError("重置类型必须为 'FIVE_HOUR' 或 'WEEK'")

        jwt = self.zcode_jwt_token
        auth_header = jwt if jwt.startswith("Bearer ") else f"Bearer {jwt}"
        headers = {
            "Authorization": auth_header,
            "X-Bigmodel-Authorization": self.oauth_access_token,
            "Bigmodel-Target-Type": "PERSONAL",
        }

        body = {
            "idempotency_key": uuid.uuid4().hex,
            "reset_type": reset_type,
        }

        res = self._request(
            ZCODE_BASE_URL,
            "/api/v1/coding-plan/reset/use",
            headers=headers,
            method="POST",
            json_body=body,
        )
        return res

    def get_usage_activity(self, days: int = 7) -> Dict[str, Any]:
        """
        获取 Token 消耗活动统计概览
        对齐 BigModelUsageQuotaProvider.fetchCreditUsageActivity:
        GET https://bigmodel.cn/api/monitor/credit-usage/activity?startTime=...&endTime=...
        """
        now = datetime.datetime.now()
        start = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        end = now.strftime("%Y-%m-%d 23:59:59")
        try:
            res = self._request(
                self.base_url,
                "/api/monitor/credit-usage/activity",
                headers={"authorization": self.api_key},
                params={"startTime": start, "endTime": end},
            )
            return res.get("data", {}) if res.get("success") else {}
        except Exception:
            return {}

    @staticmethod
    def format_timestamp(ts: Optional[int]) -> str:
        if not ts:
            return "--"
        try:
            if ts > 1e11:
                ts = ts / 1000.0
            dt = datetime.datetime.fromtimestamp(ts)
            now = datetime.datetime.now()
            if dt.date() == now.date():
                return dt.strftime("今日 %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    @staticmethod
    def format_remaining_time(expire_at: Optional[int]) -> str:
        if not expire_at:
            return "--"
        if expire_at > 1e11:
            expire_at = expire_at / 1000.0
        remaining_sec = int(expire_at - datetime.datetime.now().timestamp())
        if remaining_sec <= 0:
            return "已到期"
        days = remaining_sec // 86400
        hours = (remaining_sec % 86400) // 3600
        minutes = (remaining_sec % 3600) // 60
        if days > 0:
            return f"剩余 {days}天 {hours}小时"
        if hours > 0:
            return f"剩余 {hours}小时 {minutes}分钟"
        return f"剩余 {minutes}分钟"

    def print_quota_dashboard(self):
        """格式化输出控制台配额与重置卡看板"""
        print("\n" + "=" * 65)
        print("          ⚡ ZCode Coding Plan 配额与订阅状态看板 ⚡          ")
        print("=" * 65)

        # 1. 订阅套餐信息
        subs = self.get_subscriptions()
        active_sub = next((s for s in subs if s.get("status") == "VALID"), None)
        if not active_sub and subs:
            active_sub = subs[0]

        if active_sub:
            product_name = active_sub.get("productName", "Coding Plan")
            cycle = active_sub.get("billingCycle", "--")
            valid = active_sub.get("valid", "--")
            status = active_sub.get("status", "--")
            next_renew = active_sub.get("nextRenewTime", "--")
            print(f"📦 当前订阅套餐: \033[1;32m{product_name}\033[0m")
            print(f"   • 订阅状态  : {status}")
            print(f"   • 计费周期  : {cycle}")
            print(f"   • 有效期区间: {valid}")
            print(f"   • 下次续费日: {next_renew}")
        else:
            print(f"📦 订阅状态: 未查询到有效订阅或使用独立 API Key")

        print("-" * 65)

        # 2. 核心额度限制与剩余
        quota_data = self.get_quota_limits()
        level = quota_data.get("level", "normal")
        limits = quota_data.get("limits", [])

        print(f"🎯 配额等级: \033[1;36m{level.upper()}\033[0m")
        print(f"📊 实时额度窗口状态:")

        if not limits:
            print("   (暂无配额限制数据)")
        else:
            for limit in limits:
                limit_type = limit.get("type", "")
                unit = limit.get("unit", 0)
                num = limit.get("number", 0)

                desc = LIMIT_UNIT_MAP.get((unit, num))
                if not desc:
                    if unit == 6:
                        desc = "每周总限额 (Weekly Quota)"
                    elif limit_type == "TIME_LIMIT":
                        desc = f"时间限制配额 (Unit:{unit}, Num:{num})"
                    else:
                        desc = f"{limit_type} (Unit:{unit}, Num:{num})"

                usage_limit = limit.get("usage", 0)
                used = limit.get("currentValue", 0)
                remaining = limit.get("remaining", 0)
                used_percentage = limit.get("percentage", 0)
                rem_percentage = max(0, 100 - used_percentage)
                reset_time_str = self.format_timestamp(limit.get("nextResetTime"))

                # 进度条渲染
                bar_len = 20
                rem_ratio = min(1.0, max(0.0, rem_percentage / 100.0))
                filled = int(round(rem_ratio * bar_len))
                empty = bar_len - filled

                if rem_percentage > 50:
                    color = "\033[32m"
                elif rem_percentage > 20:
                    color = "\033[33m"
                else:
                    color = "\033[31m"
                reset_color = "\033[0m"

                bar_str = f"{color}{'█' * filled}{'░' * empty}{reset_color}"

                print(f"\n   ▶ \033[1m{desc}\033[0m")
                print(f"     剩余量: {color}\033[1m{remaining:,}\033[0m / 总额: {usage_limit:,} ({used_percentage}% 已用)")
                print(f"     可用率: [{bar_str}] {color}{rem_percentage}%\033[0m 剩余")
                print(f"     刷新时间: \033[34m{reset_time_str}\033[0m (当前周期已用: {used:,})")

        print("-" * 65)

        # 3. 额度重置卡 (Quota Reset Opportunities)
        reset_info = self.get_reset_status()
        print("🎁 额度重置卡 (Quota Reset Cards):")

        if not reset_info.get("available"):
            reason = reset_info.get("reason", "未激活")
            print(f"   ℹ️  \033[33m{reason}\033[0m")
        else:
            five_cards = reset_info.get("five_hour_resets", [])
            week_cards = reset_info.get("week_resets", [])
            five_history = reset_info.get("latest_five_hour_history")
            week_history = reset_info.get("latest_week_history")

            # 5小时重置卡
            five_color = "\033[1;32m" if five_cards else "\033[90m"
            print(f"   • \033[1m5小时窗口重置卡\033[0m: {five_color}{len(five_cards)} 张可用\033[0m")
            for idx, c in enumerate(five_cards, 1):
                exp_ts = c.get("expire_at")
                exp_str = self.format_timestamp(exp_ts)
                rem_time = self.format_remaining_time(exp_ts)
                print(f"     └ 卡片 #{idx}: 过期日 {exp_str} ({rem_time})")
            if five_history and five_history.get("used_at"):
                used_str = self.format_timestamp(five_history.get("used_at"))
                print(f"     └ 最近使用: {used_str}")

            # 每周重置卡
            week_color = "\033[1;32m" if week_cards else "\033[90m"
            print(f"   • \033[1m每周额度重置卡\033[0m: {week_color}{len(week_cards)} 张可用\033[0m")
            for idx, c in enumerate(week_cards, 1):
                exp_ts = c.get("expire_at")
                exp_str = self.format_timestamp(exp_ts)
                rem_time = self.format_remaining_time(exp_ts)
                print(f"     └ 卡片 #{idx}: 过期日 {exp_str} ({rem_time})")
            if week_history and week_history.get("used_at"):
                used_str = self.format_timestamp(week_history.get("used_at"))
                print(f"     └ 最近使用: {used_str}")

        print("-" * 65)

        # 4. 近7日用量消耗汇总
        activity = self.get_usage_activity(days=7)
        summary = activity.get("summary")
        if summary:
            total_tokens = summary.get("totalTokens", 0)
            peak_tokens = summary.get("peakDailyTokens", 0)
            peak_date = summary.get("peakDailyTokensDate", "--")
            print("📈 近 7 日 Token 消耗统计:")
            print(f"   • 7日总消耗  : \033[1;33m{total_tokens:,}\033[0m Tokens")
            print(f"   • 单日消耗峰值: {peak_tokens:,} Tokens ({peak_date})")

        print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="ZCode Coding Plan 配额与重置卡查询客户端")
    parser.add_argument("--json", action="store_true", help="以原始 JSON 格式输出配额与重置卡结果")
    parser.add_argument("--api-key", type=str, default=None, help="自定义 API Key (默认读取本地凭证)")
    parser.add_argument("--provider", type=str, default="bigmodel", choices=["bigmodel", "zai"], help="所属平台")
    parser.add_argument("--use-reset", type=str, choices=["FIVE_HOUR", "WEEK"], default=None, help="核销一张重置卡 (FIVE_HOUR 或 WEEK)")
    args = parser.parse_args()

    try:
        client = ZCodeQuotaClient(api_key=args.api_key, provider=args.provider)

        if args.use_reset:
            print(f"[*] 正在请求使用一张 {args.use_reset} 重置卡...")
            res = client.use_reset(args.use_reset)
            print(f"[✓] 重置卡使用成功: {res}")
            return

        if args.json:
            result = {
                "provider": client.provider,
                "subscriptions": client.get_subscriptions(),
                "quota": client.get_quota_limits(),
                "reset_cards": client.get_reset_status(),
                "activity": client.get_usage_activity(),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            client.print_quota_dashboard()
    except Exception as e:
        print(f"\033[31m[错误] {str(e)}\033[0m", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
