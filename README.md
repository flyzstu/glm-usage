# GLM Usage

把 bigmodel.cn（智谱 GLM 编程套餐）网页内部接口包装成一个高性能的只读服务：5 小时额度、周额度、各模型积分/Token 消耗、活跃度统计，外加一个自带的可视化看板。

协议细节来自 [DOCS.md](./DOCS.md)（抓包逆向结果）：仅需一个 `Authorization` 请求头，不依赖 cookie。

## 快速开始

```bash
pip install -e ".[dev]"          # 或: pip install sanic httpx orjson uvloop

export BIGMODEL_TOKEN='<bigmodel_token_production 的值>'
./run.sh                          # 等价于 python3 -m glm_usage
# 打开 http://127.0.0.1:8000/dashboard
```

取 token：登录 bigmodel.cn → F12 → Application → Cookies → `https://bigmodel.cn` → 复制 `bigmodel_token_production`。

不想把 token 放环境变量里，就写进文件（服务会在 mtime 变化时自动重载，换 token 无需重启）：

```bash
printf '%s\n' "$TOKEN" > /etc/glm-usage/token
export BIGMODEL_TOKEN_FILE=/etc/glm-usage/token
```

## Docker

```bash
cp .env.example .env      # 填入 BIGMODEL_TOKEN
docker-compose up -d      # 构建并启动，默认映射 8000
# 打开 http://127.0.0.1:8000/dashboard
```

镜像以非 root（uid 10001）运行，根文件系统只读 + `no-new-privileges` + `/tmp` tmpfs，自带 `/healthz` 健康检查，日志按 3×10MB 轮转。容器能接收 SIGTERM 优雅退出（实测 `docker stop` 约 0.2s）。

生产更推荐挂载 token 文件，换 token 不用重启容器：

```yaml
    environment:
      BIGMODEL_TOKEN: ""
      BIGMODEL_TOKEN_FILE: /run/secrets/bigmodel_token
    volumes:
      - ./token:/run/secrets/bigmodel_token:ro
```

**构建时 pip 报 DNS 解析失败？** 本机 dockerd 给*构建容器*下发的是已失效的 Tailscale DNS（`100.100.100.100`），而 `docker run` 的容器拿到的是宿主机当前的 `119.29.29.29`——所以表现为“跑容器没事、构建装依赖解析不了 pypi.org”。`docker-compose.yml` 已用 `build.network: host` 规避（等价于 `docker build --network=host .`）。想根治可以在维护窗口重启 dockerd（会一并重启所有容器），或给 `/etc/docker/daemon.json` 固定 `"dns": ["119.29.29.29"]`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/dashboard` | 可视化看板（纯静态，无外链依赖）。**故意不在根路径**，方便按路径加授权；`/` 会 302 跳到这里 |
| GET | `/healthz` | 存活探针 + token 是否就绪、刷新间隔 |
| GET | `/api/v1/summary` | 一行搞定：5 小时额度、周额度、总积分、缓存命中率（时间均为 CST） |
| GET | `/api/v1/quota` | 5 小时额度 + 周额度 |
| GET | `/api/v1/usage` | 各模型积分/Token 消耗 |
| GET | `/api/v1/activity` | 累计 Token、连续天数、逐日曲线 |
| GET | `/api/v1/performance` | 系统健康度：Lite / Pro&Max 的 Decode 速度与成功率 |
| GET | `/api/v1/account` | 套餐与账户：订阅、自动续费、额度重置、余额、体验卡、客户信息 |
| GET | `/api/v1/overview` | 前四个用量接口并发聚合，看板一次请求拿走 |
| GET | `/api/v1/metrics` | 进程内计数器 + 缓存占用 |

通用查询参数：

- `days=N`：最近 N 天（默认 usage/performance 7 天、activity 30 天）。起点对齐到当天 00:00，终点对齐到当前整点的 `59:59`——对齐是为了让缓存键在一个小时内保持稳定，否则每个请求都是新 key。
- `startTime` / `endTime`：`YYYY-MM-DD` 或 `YYYY-MM-DD HH:mm:ss`，时区 `Asia/Shanghai`。显式传入时不做任何对齐，按原值转发。
- `usageType=MODEL|MCP`：usage 的维度，默认 `MODEL`。`MCP` 维度的数据在 `data.mcpUsage` 下（平台只在通过 MCP 调用时才有数据，否则恒为 0）。
- `productId=product-005|product-047`：`/account` 里体验卡对应的产品，默认 `product-005`。
- `fields=a,b`：只返回 `data` 下的一级字段（见下）。
- `maxAge=N`：只接受 N 秒以内的缓存（`/summary` 默认 300，其它接口默认只看 TTL）。
- `refresh=1`：跳过缓存强制回源。**同一把 key 每 `GLM_USAGE_REFRESH_MIN_INTERVAL`（默认 30）秒只放行一次**，被节流时退回读缓存并标记 `meta.refreshThrottled: true`。

### 只取需要的字段

`fields` 精确匹配 `data` 下的一级字段名，用来砍掉不需要的部分：

```bash
curl -s 'http://127.0.0.1:8000/api/v1/usage?days=7&fields=modelSummaryList,totalUsage'
# {"data":{"modelSummaryList":[…],"totalUsage":{…}},
#  "meta":{"fields":["modelSummaryList","totalUsage"], …}}
```

`/overview` 与 `/account` 的 `fields` 是**分段名**，而且只会去取被请求的分段——所以"只关心额度"可以直接：

```bash
curl -s 'http://127.0.0.1:8000/api/v1/overview?fields=quota'
# 只回源 1 次，而不是 4 次
```

没匹配上的名字会列在 `meta.ignoredFields`（方便发现拼错）；一个都没匹配上直接返回 400，而不是给你一个空的 `data`。上游本身**不支持**字段投影，裁剪发生在本服务这一层，所以 `summary.totalCredits` 这种嵌套路径不支持，先取 `summary` 再自己挑。

### 摘要接口：一行拿全

```bash
curl -s 'http://127.0.0.1:8000/api/v1/summary'
```

```json
{
  "data": {
    "fiveHour": { "limit": 2000, "used": 37, "remaining": 1962, "usedPercent": 1.85,
                  "resetAt": "2026-09-12T14:40:03+08:00", "resetInSeconds": 5253 },
    "weekly":   { "limit": 10000, "used": 159, "remaining": 9841, "usedPercent": 1.59,
                  "resetAt": "2026-09-19T13:20:03+08:00", "resetInSeconds": 605253 },
    "level": "lite",
    "totalCredits": 159.2652,
    "cacheHitRate": 0.9221,
    "window": { "startTime": "2026-09-05T00:00:00+08:00", "endTime": "2026-09-12T13:59:59+08:00", "days": 7.58 }
  },
  "meta": {
    "cacheState": { "quota": "HIT", "usage": "HIT" }, "cached": true,
    "generatedAt": "2026-09-12T13:12:30+08:00", "timezone": "Asia/Shanghai",
    "ageSeconds": 12.4, "maxAgeSeconds": 300, "errors": null
  }
}
```

- **时间统一为北京时间**（ISO 8601 带 `+08:00`）；`resetInSeconds` 是服务端算好的剩余秒数，客户端不用自己校时。
- `usedPercent` 由 `used / limit` 现算——上游的 `percentage` 是向下取整的整数（7.2% 会变成 7），会丢精度。
- **不额外压上游**：它复用 `/quota` 与 `/usage` 的同一份缓存，命中时不产生任何上游请求；`window` 由 `days` / `startTime` / `endTime` 决定（默认近 7 天）。
- **数据有效性**：`?maxAge=N` 只接受 N 秒以内的缓存，默认取 `GLM_USAGE_SUMMARY_MAX_AGE=300`。超龄就重新取；如果上游正好挂了、缓存又已经太旧，这一格会写进 `meta.errors` 并**从 `data` 中消失**，而不是把旧数字当当前值返回。`maxAge=0` = 每次都回源（别放进高频轮询）。
- `meta.ageSeconds`：本次响应里最旧那一格的年龄，一眼看出数据有多新。

### 授权

面板和 API 是两条独立路径，可以分别授权：

- **面板 `/dashboard`**：本服务不拦它（HTML 本身不含数据）。要保护就在反向代理（Nginx / Nginx Proxy Manager 的 Access List）按 `/dashboard` 前缀加 basic auth。想换路径设 `GLM_USAGE_DASHBOARD_PATH`，`/` 会自动 302 过去。
- **API `/api/*`**：设 `GLM_USAGE_API_KEY` 后必须带 `X-API-Key` 请求头；也接受 `?key=`，方便浏览器。
- 两者都开了时，用 `http://host:8000/dashboard?key=你的密钥` 打开一次：看板把密钥存进 localStorage 并**立刻从地址栏抹掉**，之后所有请求只走请求头。

响应统一信封，`meta.cacheState` 就是 `X-Cache` 响应头的值：

```json
{
  "data": { "level": "lite", "limits": [ /* ... */ ] },
  "meta": { "cacheState": "HIT", "cached": true, "ttl": 15, "generatedAt": "2026-09-12T12:00:00+08:00" }
}
```

`overview` 额外带 `meta.states`（各段缓存状态）、`meta.errors`（哪一段失败及原因）。只要有一段成功就返回 200，全挂才返回 502——上游抖动时看板不会整页白掉。

错误响应：

```json
{ "error": { "type": "invalid_token", "message": "token 无效或已过期（code 1001）…" } }
```

`type` 取值：`missing_token` / `invalid_token`（401）、`invalid_request`（400）、`upstream_unavailable`（502）、`upstream_timeout`（504）。

## 多账号 / 调用方自带 token

请求里带上 `Authorization` 头即用即弃，优先级高于服务端配置：

```bash
curl -H "Authorization: $TOKEN" http://127.0.0.1:8000/api/v1/quota
```

缓存键包含 token 的指纹（blake2b 前 16 位），不同 token 不会串数据。

服务自身要对外暴露时，用 `GLM_USAGE_API_KEY` 加一把锁；除 `/healthz` 外的 `/api/` 路由都要求 `X-API-Key` 请求头。

## 性能设计

针对“上游慢、调用方多、数据短时间内不变”的形态做了五件事：

1. **请求合并（single-flight）**：并发未命中同一 key 时只回源一次，其余请求共享结果（`X-Cache: COALESCED`）。看板刷新、多个终端同时拉数据都只花上游一次调用。
2. **稳定的缓存键**：未显式传时间窗时，`endTime` 对齐到当前整点的 `59:59`（上游数据本就是按天聚合的）。如果直接用“当前秒级时刻”，每个请求都是新 key，缓存会完全失效——这不是猜测，是压测里发现的真实缺陷，修复后 `/api/v1/overview` 吞吐提升 2.8 倍、回源次数下降近两个数量级。
3. **分级 TTL 缓存 + stale 兜底**：quota 60s、usage/activity/performance 300s；条目过期后 `GLM_USAGE_STALE_TTL`（默认 10 分钟）内若上游报错，继续用旧值并标记 `X-Cache: STALE`——每个响应还会带 `meta.ageSeconds`，旧数据不会"静默"。
4. **连接池复用**：单进程共享一个 `httpx.AsyncClient`（keep-alive 30s，默认 32 连接）；`overview` 用 `asyncio.gather` 并发三个上游请求。
5. **快路径**：uvloop（Sanic 检测到即自动启用）、orjson 序列化、看板 HTML 常驻内存。
6. **不给自己挖坑的闸门**：`?refresh=1` 每 key 30 秒只放行一次；收到 429 时按上游的 `Retry-After` 退避（超过 5 秒就直接失败，不把连接挂着）。

**关于 worker 数：默认 1 个，别按 CPU 核数开。** 每个 worker 有独立的缓存，多一个 worker 就多一份上游调用——实测 2 worker 时，TTL 内 40 次请求产生了 2 次上游调用而不是 1 次。这个服务是 I/O 密集 + 重缓存，单进程的吞吐早已远超需要（见下表），多开只会更快撞上站点的频率限制。

实测（2026-09-12，i5-10400 / 12 核，单 worker 进程，`ab` 与压测目标同机，并发 50，缓存命中，访问日志关闭）：

| 端点 | QPS | 平均延迟 |
| --- | --- | --- |
| `/api/v1/quota` | 12,277 | 4.07 ms |
| `/api/v1/summary` | 6,820 | 7.33 ms |
| `/api/v1/overview` | 6,053 | 8.26 ms |

访问日志（`GLM_USAGE_ACCESS_LOG=1`）每个请求要写一次标准输出，同机压测下吞吐大约**减半**（quota 12,277 → 6,427 QPS）。日常几 req/min 的用量完全无感，但要压测或对外提供高频服务时，记得关掉它或改在反代层记录。

同一轮压测中，20,000 次 `overview` 请求（折合 60,000 次分段取数）只回源 3 次；冷启动并发 50 个请求、上游耗时 600ms 时，状态分布是 1 个 `MISS` + 49 个 `COALESCED`，上游只被调用 1 次。

> 已知小瑕疵：客户端在服务端等待上游时**主动断开**，会被 Sanic 计成一次 500（日志里没有 traceback，实测"中断一次 +1"可复现）。只影响 `/api/v1/metrics` 的计数，不影响数据——真实的连接失败返回的是 502 `upstream_unavailable`。

注意：缓存未命中时的吞吐由上游 RTT 决定，不是本服务的能力上限。

## 配置

全部通过环境变量，见 [.env.example](./.env.example)。常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BIGMODEL_TOKEN` | — | 上游 JWT |
| `BIGMODEL_TOKEN_FILE` | — | 从文件读 token，mtime 变更自动重载 |
| `GLM_USAGE_API_KEY` | — | 保护本服务的 `X-API-Key` |
| `GLM_USAGE_HOST` / `GLM_USAGE_PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `GLM_USAGE_WORKERS` | `1` | 进程数；≥2 时缓存与计数按进程独立 |
| `GLM_USAGE_QUOTA_TTL` | `60` | 额度缓存秒数（文档建议上游采集间隔 ≥ 60s，站点有 WAF） |
| `GLM_USAGE_USAGE_TTL` / `ACTIVITY_TTL` / `PERFORMANCE_TTL` | `300` | 用量/活跃度/健康度缓存秒数 |
| `GLM_USAGE_ACCOUNT_TTL` | `3600` | 套餐与账户缓存秒数（很少变） |
| `GLM_USAGE_STALE_TTL` | `600` | 上游故障时旧值可用时长（宁可报错也别把旧数据当当前值） |
| `GLM_USAGE_REFRESH_MIN_INTERVAL` | `30` | 同一把 key 两次 `?refresh=1` 之间的最小间隔，0 = 不限制 |
| `GLM_USAGE_RETRIES` | `2` | 5xx/超时的重试次数（指数退避 + 抖动） |
| `GLM_USAGE_REQUEST_TIMEOUT` | `15` | 上游超时（秒） |
| `GLM_USAGE_DASHBOARD_PATH` | `/dashboard` | 面板路径（不放根路径，便于按路径授权） |
| `GLM_USAGE_SUMMARY_MAX_AGE` | `300` | `/summary` 能接受的最大数据年龄（秒） |

生产建议：**保持 `GLM_USAGE_WORKERS=1`**（见上文说明），前面挂 Nginx/Caddy 做 TLS，并在反代层开 gzip——`/overview` 响应有 10~20KB，压缩比能到 5 倍以上，这比在应用里做更划算。

systemd 示例：

```ini
[Unit]
Description=GLM Usage API
After=network-online.target

[Service]
WorkingDirectory=/opt/glm-usage
EnvironmentFile=/etc/glm-usage/env
ExecStart=/opt/glm-usage/run.sh
Restart=on-failure
User=glm

[Install]
WantedBy=multi-user.target
```

## 测试

```bash
python3 -m pytest -q     # 130 项
ruff check .             # 与 CI 同一套规则
```

覆盖：缓存命中/过期/stale/请求合并/取消传染/maxAge 上限、`?refresh` 节流闸门、时间窗口解析与边界、查询串 `%20` 编码、上游错误码与 `Retry-After`、鉴权（`X-API-Key` 与 `?key=`）、日志脱敏、token 文件热重载、入口 AppLoader 目标可解析，以及 `/api/v1/*` 全链路（上游用 `httpx.MockTransport` 伪造，不发真实网络请求）。CI 见 `.github/workflows/ci.yml`。

## 注意

- 这些是 bigmodel 网页内部接口，不属于官方公开 API，路径与字段可能随官网改版变化。
- JWT 有效期有限。上游的鉴权失败是 **HTTP 200 + body 里的错误码**，实测两种：`code 1001`（没带 Authorization 头）和 `code 401`（token 过期或无效）。两者都会被本服务识别为 401 `invalid_token` 并带上上游原文，重新登录取新 token 即可——注意别把它当成 502，那通常意味着上游真的出问题了。
- `/api/v1/account` 会返回账户标识信息（客户 ID、邮箱等）。对外暴露时记得设置 `GLM_USAGE_API_KEY`。
- 上游站点有 WAF，文档建议采集间隔 ≥ 60 秒；默认各档缓存 TTL 已按此设计，调小前请自行评估限流风险。
- 服务只读，不会修改任何上游数据。
