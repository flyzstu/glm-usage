# Bigmodel GLM Coding Plan 用量统计 API 文档

> 本文整理自对 `https://bigmodel.cn/coding-plan/personal/usage`（用量统计页面）的网络请求逆向分析，
> 全部接口均在登录态下实测验证通过（2026-09-12）。
>
> **注意**：这些是网页内部接口，不在官方公开文档范围内，路径与字段可能随网站改版变化。

## 目录

- [通用约定](#通用约定)
- [鉴权](#鉴权)
- [核心用量接口](#核心用量接口)
  - [1. 额度（5小时额度 / 周额度）](#1-额度5小时额度--周额度)
  - [2. 使用详情（积分消耗 / 各模型消耗）](#2-使用详情积分消耗--各模型消耗)
  - [3. 活跃度（累计 Tokens / 连续天数）](#3-活跃度累计-tokens--连续天数)
  - [4. 系统健康度（Decode 速度 / 成功率）](#4-系统健康度decode-速度--成功率)
- [套餐与账户信息接口](#套餐与账户信息接口)
- [完整示例程序](#完整示例程序)
- [注意事项](#注意事项)

---

## 通用约定

| 项目 | 说明 |
|---|---|
| Base URL | `https://bigmodel.cn` |
| 请求方法 | 全部为 `GET` |
| 请求/响应格式 | JSON（请求无 body，参数全部在 query string） |
| 时区 | `Asia/Shanghai` |
| 统一响应包装 | `{"code": 200, "msg": "...", "data": {...}, "success": true}` |

`code` 含义（实测观察）：

| code | 含义 |
|---|---|
| 200 | 成功 |
| 1001 | 鉴权失败：`"Authentication parameter not received in Header, unable to authenticate"`，即 token 缺失或已过期 |

## 鉴权

- 请求头：`Authorization: <JWT>`，加不加 `Bearer ` 前缀均可（两者实测均通过）。
- token 来源：登录 bigmodel.cn 后浏览器中的 **cookie `bigmodel_token_production`**（JWT，非 httpOnly，可直接读取）。
  - 获取方法：浏览器 F12 → Application（应用）→ Cookies → `https://bigmodel.cn` → 复制 `bigmodel_token_production` 的值。
- **无需携带任何 cookie**，仅凭 `Authorization` 请求头即可调用（已用 `credentials: 'omit'` 实测）。
- JWT 有有效期，过期后返回 `code=1001`，重新登录网页端获取新 cookie 即可。

```bash
# 最小验证命令
curl -H "Authorization: $TOKEN" "https://bigmodel.cn/api/monitor/usage/quota/limit"
```

---

## 核心用量接口

用量统计页面的全部数据由以下 4 个接口提供。
页面的"每日/每周/累计"图表切换**不会发出新请求**（前端对 activity 数据本地聚合）；
"近7天/近30天"切换会以不同时间范围重发对应接口。

### 1. 额度（5小时额度 / 周额度）

```
GET /api/monitor/usage/quota/limit
```

无查询参数。

响应示例：

```json
{
  "code": 200,
  "msg": "Operation successful",
  "data": {
    "level": "lite",
    "limits": [
      {
        "type": "CREDIT_LIMIT",
        "unit": 3,
        "number": 5,
        "usage": 2000,
        "currentValue": 37,
        "remaining": 1962,
        "percentage": 1,
        "nextResetTime": 1789195203620
      },
      {
        "type": "CREDIT_LIMIT",
        "unit": 6,
        "number": 1,
        "usage": 10000,
        "currentValue": 171,
        "remaining": 9828,
        "percentage": 1,
        "nextResetTime": 1789704979994
      }
    ]
  },
  "success": true
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `data.level` | 套餐档位（`lite` / `pro` / `max`） |
| `limits[].type` | 固定 `CREDIT_LIMIT` |
| `limits[].unit` / `number` | 时间窗口。实测 `unit=3, number=5` 为 5 小时窗口；`unit=6, number=1` 为周额度（枚举语义为推断） |
| `limits[].usage` | 额度上限（积分） |
| `limits[].currentValue` | 已用积分 |
| `limits[].remaining` | 剩余积分 |
| `limits[].percentage` | 已用百分比（整数） |
| `limits[].nextResetTime` | 重置时间，**Unix 毫秒时间戳** |

### 2. 使用详情（积分消耗 / 各模型消耗）

```
GET /api/monitor/credit-usage/usage-detail
```

| 参数 | 说明 |
|---|---|
| `startTime` | 起始时间，`YYYY-MM-DD HH:mm:ss`（空格 URL 编码为 `%20` 或 `+`） |
| `endTime` | 结束时间，同上（页面惯例传 `23:59:59`） |
| `type` | 固定 `1`（个人编程套餐） |
| `usageType` | `MODEL`（模型维度）或 `MCP`（工具/MCP 维度，即页面"工具"标签） |

`usageType=MODEL` 响应示例（节选）：

```json
{
  "code": 200,
  "data": {
    "granularity": "DAY",
    "timezone": "Asia/Shanghai",
    "summary": {
      "cacheHitRate": { "value": "0.9221" },
      "offPeakUsageRate": { "value": "0.9716" },
      "totalCredits": { "value": "159.2652" },
      "averageDailyCredits": { "value": "22.7522" }
    },
    "totalUsage": { "totalTokens": 4845999, "totalCredits": "159.2652" },
    "modelSummaryList": [
      { "modelCode": "glm-5.3", "modelName": "GLM-5.3",
        "totalTokens": 107949, "totalCredits": "33.9747" },
      { "modelCode": "glm-5.3-flash", "modelName": "GLM-5.3-Flash",
        "totalTokens": 4738050, "totalCredits": "125.2905" }
    ],
    "modelDataList": [
      {
        "modelCode": "glm-5.3",
        "modelName": "GLM-5.3",
        "uncachedInputTokensUsage": [0, 0, 0, 0, 0, 80158, 0],
        "cachedInputTokensUsage":   [0, 0, 0, 0, 0, 25984, 0],
        "inputTokensUsage":         [0, 0, 0, 0, 0, 106142, 0],
        "outputTokensUsage":        [0, 0, 0, 0, 0, 1807, 0],
        "totalTokensUsage":         [0, 0, 0, 0, 0, 107949, 0],
        "uncachedInputCreditsUsage": ["0.0000", "...", "0.0000"],
        "cachedInputCreditsUsage":   ["0.0000", "...", "0.0000"],
        "inputCreditsUsage":         ["0.0000", "...", "0.0000"],
        "outputCreditsUsage":        ["0.0000", "...", "0.0000"],
        "totalCreditsUsage":         ["0.0000", "...", "33.9747"]
      }
    ],
    "xTime": ["2026-09-06", "2026-09-07", "2026-09-08", "..."]
  },
  "success": true
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `granularity` | 聚合粒度，实测固定 `DAY` |
| `summary.cacheHitRate` | Cache 命中率（0~1 小数，字符串） |
| `summary.offPeakUsageRate` | 非高峰时段用量占比 |
| `summary.totalCredits` | 区间积分总消耗（字符串，4 位小数） |
| `summary.averageDailyCredits` | 日均积分 |
| `modelSummaryList[]` | 每模型汇总：`totalTokens`（整数）、`totalCredits`（字符串） |
| `modelDataList[]` | 每模型逐日明细，**数组下标与 `xTime[]` 日期一一对应** |
| `modelDataList[].*TokensUsage` | 逐日 tokens（整数数组），细分 未缓存输入 / 缓存输入 / 输入合计 / 输出 / 合计 |
| `modelDataList[].*CreditsUsage` | 逐日积分（字符串数组），同样五套细分 |
| `xTime[]` | 日期轴 |

`usageType=MCP` 时返回结构为 `data.mcpUsage`：

```json
{
  "totalUsage": { "totalMcpCalls": 0, "totalCredits": "0.0000" },
  "mcpDataList": [],
  "mcpSummaryList": [],
  "xTime": ["2026-09-06", "..."]
}
```

> 页面上"工具"维度即此接口。平台只提供 `MODEL` 与 `MCP` 两种维度，
> **没有**更细的 HTTP 接口 / 客户端工具归因数据；未通过 MCP 调用的用量在这里恒为 0。

### 3. 活跃度（累计 Tokens / 连续天数）

```
GET /api/monitor/credit-usage/activity
```

| 参数 | 说明 |
|---|---|
| `startTime` / `endTime` | 同上；实测支持一次拉取一整年 |
| `type` | 固定 `1` |

响应示例（节选）：

```json
{
  "code": 200,
  "data": {
    "granularity": "DAY",
    "timezone": "Asia/Shanghai",
    "summary": {
      "totalTokens": 30915704,
      "peakDailyTokens": 6119933,
      "peakDailyTokensDate": "2026-02-17",
      "totalUsageDurationMs": 1546898,
      "currentStreakDays": 2,
      "longestStreakDays": 12
    },
    "series": [
      { "date": "2025-09-12", "totalCredits": "0.0000", "totalTokens": 0, "mcpCalls": 0 }
    ]
  },
  "success": true
}
```

| 字段 | 含义 |
|---|---|
| `summary.totalTokens` | 累计 Tokens |
| `summary.peakDailyTokens(+Date)` | 单日峰值 Tokens 及日期 |
| `summary.totalUsageDurationMs` | 累计使用时长（毫秒） |
| `summary.currentStreakDays` / `longestStreakDays` | 当前 / 最长连续使用天数 |
| `series[]` | 逐日 `{date, totalCredits, totalTokens, mcpCalls}`，页面"每日"柱状图数据源 |

### 4. 系统健康度（Decode 速度 / 成功率）

```
GET /api/monitor/usage/model-performance-day
```

| 参数 | 说明 |
|---|---|
| `startTime` / `endTime` | 同上；页面近7天视图传 `D-7 00:00:00 ~ D-1 23:59:59`（不含当天） |

响应示例：

```json
{
  "code": 200,
  "data": {
    "x_time": ["2026-09-05", "2026-09-06", "..."],
    "liteDecodeSpeed":    [94.30, 89.33, "..."],
    "proMaxDecodeSpeed":  [111.66, 114.33, "..."],
    "liteSuccessRate":    [0.9994, 0.9992, "..."],
    "proMaxSuccessRate":  [0.9994, 0.9994, "..."]
  },
  "success": true
}
```

| 字段 | 含义 |
|---|---|
| `x_time[]` | 日期轴（注意是蛇形命名） |
| `liteDecodeSpeed[]` / `proMaxDecodeSpeed[]` | Lite / Max&Pro 高峰期平均 Decode 速度（tokens/s） |
| `liteSuccessRate[]` / `proMaxSuccessRate[]` | 对应成功率（0~1 小数） |

---

## 套餐与账户信息接口

页面同时调用以下接口（与套餐展示相关，可作为补充数据源）：

| 接口 | 说明 |
|---|---|
| `GET /api/biz/subscription/list?pageSize=9999&pageNum=1` | 套餐订单列表：`productName`、`status`、`valid`（有效期）、`autoRenew`、`actualPrice`/`renewPrice`、`billingCycle`、`inCurrentPeriod`、`nextRenewTime` 等 |
| `GET /api/biz/subscription/v1-coding-plan-auto-renew-closed-by-system` | 是否被系统关闭自动续订（`data: false/true`） |
| `GET /api/biz/customer-package-reset/list?targetType=PERSONAL` | 额度重置记录：`fiveHourResets` / `weekResets`（数组，可为空） |
| `GET /api/biz/customer/getTokenMagnitude?productId=product-005` | 体验卡 token 总量（`data.tokens`，如 5000000）；另有 `product-047` |
| `GET /api/biz/account/query-customer-account-report` | 账户资金报告：余额、充值、赠送、累计消费等 |
| `GET /api/biz/customer/getCustomerInfo` | 用户基础信息（ID、邮箱、机构/项目等） |

其余请求（`whiteList` 校验、`university`、客服 SDK、埋点 beacon 等）为页面壳与功能开关，无数据价值，可忽略。

---

## 完整示例程序

```python
#!/usr/bin/env python3
"""Bigmodel GLM Coding Plan 用量统计采集示例"""
from datetime import date, timedelta
import requests

BASE = "https://bigmodel.cn"
TOKEN = "把 bigmodel_token_production cookie 的值粘贴到这里"
H = {"Authorization": f"Bearer {TOKEN}"}


def get(path: str, **params):
    r = requests.get(BASE + path, headers=H, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 200:
        raise RuntimeError(f"{path} -> code={j.get('code')}: {j.get('msg')}")
    return j["data"]


def fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


today = date.today()
week_ago = today - timedelta(days=6)
yesterday = today - timedelta(days=1)

# 1. 额度（5小时额度 + 周额度）
quota = get("/api/monitor/usage/quota/limit")
print("套餐档位:", quota["level"])
for lim in quota["limits"]:
    print(f"  已用 {lim['currentValue']}/{lim['usage']} "
          f"({lim['percentage']}%), 剩余 {lim['remaining']}")

# 2. 使用详情（各模型积分 / token 消耗）
usage = get("/api/monitor/credit-usage/usage-detail",
            startTime=fmt(week_ago), endTime=fmt(today)[:-8] + "23:59:59",
            type=1, usageType="MODEL")
print(f"近7天积分: {usage['summary']['totalCredits']['value']}, "
      f"Cache命中率: {usage['summary']['cacheHitRate']['value']}")
for m in usage["modelSummaryList"]:
    print(f"  {m['modelName']}: {m['totalCredits']} 积分 / {m['totalTokens']} tokens")

# 3. 活跃度
act = get("/api/monitor/credit-usage/activity",
          startTime=fmt(today - timedelta(days=365)), endTime=fmt(today),
          type=1)
print("累计 Tokens:", act["summary"]["totalTokens"],
      ", 最长连续:", act["summary"]["longestStreakDays"], "天")

# 4. 系统健康度
perf = get("/api/monitor/usage/model-performance-day",
           startTime=fmt(week_ago), endTime=fmt(yesterday)[:-8] + "23:59:59")

# 5. 套餐信息
plan = get("/api/biz/subscription/list", pageSize=9999, pageNum=1)
for sub in plan:
    print(sub["productName"], sub["status"], "有效期至:", sub["valid"])
```

---

## 注意事项

1. **非公开接口**：路径、参数、字段结构均来自网页逆向，随时可能改版失效；遇到异常先对照网页 DevTools 的 Network 面板复核。
2. **token 有效期**：JWT 过期后返回 `code=1001`，需重新登录网页端并更新 `bigmodel_token_production`。建议程序对该 code 做明确提示。
3. **请求频率**：站点有 WAF（`ssxmod_itna` 等防护 cookie），请勿高频轮询；采集间隔建议 ≥ 60 秒。
4. **token 安全**：`Authorization` 头等同于登录态，不要提交到公共仓库或日志中。
5. **字段类型**：积分字段普遍为字符串（保留 4 位小数），tokens 为整数；`nextResetTime` 为毫秒时间戳，注意按秒换算。
6. **数据延迟**：页面数据有约 1~2 分钟刷新延迟（页面标注"最近刷新时间"），接口返回的是服务端最新聚合值，实时请求期间可观察到数值持续增长。

