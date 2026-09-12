const $ = (id) => document.getElementById(id);
const RESET_LABELS = { 3: "5 小时窗口", 6: "周额度" };
const API_KEY_STORAGE = "glm-usage-api-key";
let refreshSeconds = 30;
let nextResets = [];
let inflight = false;

// 支持用 /dashboard?key=xxx 打开一次：把密钥存进 localStorage 后立刻从地址栏抹掉，
// 后续请求只走 X-API-Key 请求头，密钥不再出现在 URL / 历史记录里。
(function bootstrapApiKey() {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("key");
  if (!fromUrl) return;
  localStorage.setItem(API_KEY_STORAGE, fromUrl);
  url.searchParams.delete("key");
  window.history.replaceState({}, "", url.pathname + url.search + url.hash);
})();

const apiKey = () => localStorage.getItem(API_KEY_STORAGE) || "";

const isBlank = (v) => v === null || v === undefined || v === "";
const num = (v, d = 0) => {
  if (isBlank(v)) return "--";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d }) : "--";
};
const pct = (v) => {
  if (isBlank(v)) return "--";
  const n = Number(v);
  if (!Number.isFinite(n)) return "--";
  return (n <= 1 ? n * 100 : n).toFixed(2) + "%";
};
const tokens = (v) => {
  if (isBlank(v)) return "--";
  const n = Number(v) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
};
const duration = (ms) => {
  const s = Math.round((Number(ms) || 0) / 1000);
  if (s < 60) return s + " 秒";
  const m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
  if (d) return d + " 天 " + (h % 24) + " 小时";
  if (h) return h + " 小时 " + (m % 60) + " 分";
  return m + " 分 " + (s % 60) + " 秒";
};
const barColor = (used) => used >= 90 ? "var(--bad)" : used >= 70 ? "var(--warn)" : "var(--accent)";
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

async function getJSON(url) {
  const headers = { accept: "application/json" };
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;
  const res = await fetch(url, { headers });
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON error page */ }
  if (!res.ok) {
    const msg = body && body.error ? (body.error.message || body.error.type) : res.status + " " + res.statusText;
    const type = body && body.error ? body.error.type : "";
    // 认证失败两种：没带 key（403 forbidden）、带了但不对（401 unauthorized）。
    // 服务端只回一句泛化的"未授权"（不点名 header / ?key=，免得被扫描器当路标），
    // 所以给人看的提示放这儿。注意上游 token 缺失/过期也是 401，但类型不同，要原样透出。
    if (type === "unauthorized" || type === "forbidden") {
      throw new Error("未授权：请用 " + window.location.pathname + "?key=你的密钥 打开一次本页（换过密钥也要重新打开一次）");
    }
    throw new Error(msg);
  }
  return body;
}

function renderQuota(quota) {
  const host = $("quota");
  host.textContent = "";
  $("level").textContent = quota && quota.level ? String(quota.level) : "--";
  const limits = (quota && quota.limits) || [];
  nextResets = [];
  if (!limits.length) {
    host.append(el("div", "card empty", "暂无额度数据"));
    return;
  }
  for (const lim of limits) {
    const used = Number(lim.currentValue) || 0;
    const total = Number(lim.usage) || 0;
    const percent = total > 0 ? Math.min(used / total * 100, 100) : Number(lim.percentage) || 0;
    const card = el("div", "card");
    const head = el("div", "card-head");
    head.append(el("span", "t", RESET_LABELS[lim.unit] || ("额度 " + lim.unit + "×" + lim.number)));
    head.append(el("span", "r", percent.toFixed(1) + "%"));
    card.append(head);
    const line = el("div", "line");
    line.append(el("b", null, num(used, 2)), document.createTextNode(" / " + num(total, 2) + " 积分"));
    card.append(line);
    const bar = el("div", "bar");
    const fill = el("i");
    fill.style.width = Math.max(percent, 0.5) + "%";
    fill.style.background = barColor(percent);
    bar.append(fill);
    card.append(bar);
    const foot = el("div", "card-foot");
    foot.append(el("span", null, "剩余 " + num(lim.remaining, 2)));
    const reset = el("span", null, "--");
    if (lim.nextResetTime) {
      nextResets.push([reset, Number(lim.nextResetTime)]);
    }
    foot.append(reset);
    card.append(foot);
    host.append(card);
  }
  tickResets();
}

function statsFrom(data) {
  const usage = (data.usage && data.usage.summary) || {};
  const act = (data.activity && data.activity.summary) || {};
  const series = (data.activity && data.activity.series) || [];
  const pick = (obj, key, format, suffix) => {
    if (!obj || !obj[key]) return null;
    const raw = obj[key].value !== undefined ? obj[key].value : obj[key];
    if (raw === null || raw === undefined || raw === "") return null;
    return { value: format ? format(raw) : num(raw), sub: suffix || "" };
  };
  const items = [
    pick(usage, "totalCredits", (v) => num(v, 2), "积分 · 统计区间"),
    pick(usage, "averageDailyCredits", (v) => num(v, 2), "积分 / 天"),
    pick(usage, "cacheHitRate", pct, "缓存命中"),
    pick(usage, "offPeakUsageRate", pct, "低峰时段"),
    pick(act, "totalTokens", tokens, "tokens 累计"),
    pick(act, "currentStreakDays", (v) => num(v) + " 天", "当前连续"),
    pick(act, "peakDailyTokens", tokens, "单日峰值"),
    pick(act, "totalUsageDurationMs", duration, "累计时长"),
  ].filter(Boolean);
  if (series.length) {
    const mcp = series.reduce((sum, row) => sum + (Number(row.mcpCalls) || 0), 0);
    items.push({ value: num(mcp), sub: "MCP 调用" });
  }
  return items;
}

function renderStats(data) {
  const host = $("stats");
  host.textContent = "";
  const items = statsFrom(data);
  if (!items.length) {
    host.append(el("div", "stat", "暂无统计数据"));
    return;
  }
  for (const item of items) {
    const card = el("div", "stat");
    card.append(el("div", "k", item.sub));
    card.append(el("div", "v", item.value));
    host.append(card);
  }
}

function chartSeries(data) {
  const act = data.activity;
  if (act && Array.isArray(act.series) && act.series.length) {
    return act.series.map((row) => ({
      date: row.date,
      credits: Number(row.totalCredits) || 0,
      tokens: Number(row.totalTokens) || 0,
    }));
  }
  const usage = data.usage;
  if (usage && Array.isArray(usage.xTime) && Array.isArray(usage.modelDataList)) {
    return usage.xTime.map((date, i) => {
      let credits = 0, tok = 0;
      for (const m of usage.modelDataList) {
        const c = m.totalCreditsUsage, t = m.totalTokensUsage;
        if (Array.isArray(c)) credits += Number(c[i]) || 0;
        if (Array.isArray(t)) tok += Number(t[i]) || 0;
      }
      return { date, credits, tokens: tok };
    });
  }
  return [];
}

function renderChart(data) {
  const host = $("chart"), axis = $("axis");
  host.textContent = ""; axis.textContent = "";
  const series = chartSeries(data);
  if (!series.length) {
    host.append(el("div", "empty", "暂无每日数据"));
    return;
  }
  const max = Math.max(...series.map((d) => d.credits), 0.0001);
  for (const day of series) {
    const col = el("div", "col");
    const bar = el("i");
    bar.style.height = Math.max(day.credits / max * 100, 0.5) + "%";
    col.title = day.date + "：" + num(day.credits, 2) + " 积分 / " + tokens(day.tokens) + " tokens";
    col.append(bar);
    host.append(col);
  }
  axis.append(el("span", null, series[0].date), el("span", null, "峰值 " + num(max, 2) + " 积分"), el("span", null, series[series.length - 1].date));
}

function renderModels(data) {
  const body = $("models");
  body.textContent = "";
  const list = (data.usage && data.usage.modelSummaryList) || [];
  if (!list.length) {
    const row = el("tr");
    const cell = el("td", "empty", "暂无模型数据");
    cell.colSpan = 4;
    row.append(cell); body.append(row);
    return;
  }
  const totalCredits = list.reduce((sum, m) => sum + (Number(m.totalCredits) || 0), 0);
  const sorted = [...list].sort((a, b) => (Number(b.totalCredits) || 0) - (Number(a.totalCredits) || 0));
  for (const m of sorted) {
    const credits = Number(m.totalCredits) || 0;
    const row = el("tr");
    row.append(el("td", null, String(m.modelName || m.modelCode || "未知")));
    row.append(el("td", null, tokens(m.totalTokens)));
    row.append(el("td", null, num(credits, 2)));
    row.append(el("td", null, totalCredits ? (credits / totalCredits * 100).toFixed(1) + "%" : "--"));
    body.append(row);
  }
}

function renderPerformance(data) {
  const perf = data.performance || {};
  const statHost = $("perf-stats");
  const chartHost = $("perf-chart");
  const axis = $("perf-axis");
  statHost.textContent = "";
  chartHost.textContent = "";
  axis.textContent = "";

  const xTime = perf.x_time || perf.xTime || [];
  const lite = perf.liteDecodeSpeed || [];
  const proMax = perf.proMaxDecodeSpeed || [];
  const liteRate = perf.liteSuccessRate || [];
  const proMaxRate = perf.proMaxSuccessRate || [];
  const latest = (values) => {
    for (let i = values.length - 1; i >= 0; i -= 1) {
      if (!isBlank(values[i])) return values[i];
    }
    return null;
  };

  const summary = [
    ["Lite Decode", latest(lite), (v) => num(v, 2) + " t/s"],
    ["Lite 成功率", latest(liteRate), pct],
    ["Pro&Max Decode", latest(proMax), (v) => num(v, 2) + " t/s"],
    ["Pro&Max 成功率", latest(proMaxRate), pct],
  ];
  for (const [label, value, format] of summary) {
    if (isBlank(value)) continue;
    const card = el("div", "stat");
    card.append(el("div", "k", label));
    card.append(el("div", "v", format(value)));
    statHost.append(card);
  }

  if (!xTime.length) {
    chartHost.append(el("div", "empty", "暂无健康度数据"));
    return;
  }
  const scale = Math.max(...[...lite, ...proMax].map(Number).filter(Number.isFinite), 0.0001);
  xTime.forEach((day, i) => {
    const col = el("div", "col");
    const barLite = el("i");
    barLite.style.height = Math.max((Number(lite[i]) || 0) / scale * 100, 0.5) + "%";
    const barPro = el("i", "alt");
    barPro.style.height = Math.max((Number(proMax[i]) || 0) / scale * 100, 0.5) + "%";
    col.title = day + "：" + num(lite[i], 2) + " t/s（成功率 " + pct(liteRate[i]) + "） / Pro&Max "
      + num(proMax[i], 2) + " t/s（成功率 " + pct(proMaxRate[i]) + "）";
    col.append(barLite, barPro);
    chartHost.append(col);
  });
  axis.append(el("span", null, xTime[0]), el("span", null, "Lite / Pro&Max Decode 速度"), el("span", null, xTime[xTime.length - 1]));
}

function cardWithRows(title, rows, badge) {
  const card = el("div", "card");
  const head = el("div", "card-head");
  head.append(el("span", "t", title));
  if (badge) head.append(el("span", "badge", badge));
  card.append(head);
  for (const [key, value] of rows) {
    const row = el("div", "kv-row");
    row.append(el("span", "k", key), el("span", "v", String(value)));
    card.append(row);
  }
  return card;
}

function scalarRows(value, limit = 8) {
  if (!value || typeof value !== "object") return [];
  const rows = [];
  for (const [key, item] of Object.entries(value)) {
    if (rows.length >= limit) break;
    if (isBlank(item)) continue;
    if (Array.isArray(item)) { rows.push([key, item.length + " 项"]); continue; }
    if (typeof item === "object") continue;
    rows.push([key, String(item)]);
  }
  return rows;
}

function firstRecord(value) {
  if (Array.isArray(value)) return value[0] || null;
  if (value && typeof value === "object") {
    for (const key of ["list", "records", "rows", "items", "data"]) {
      if (Array.isArray(value[key])) return value[key][0] || null;
    }
    return value;
  }
  return null;
}

function renderAccount(body) {
  const host = $("account");
  host.textContent = "";
  const data = (body && body.data) || {};
  const errors = (body && body.meta && body.meta.errors) || {};

  const plan = firstRecord(data.subscription);
  if (plan) {
    const rows = [
      ["状态", plan.status],
      ["有效期至", plan.valid],
      ["下次续费", plan.nextRenewTime],
      ["价格", [plan.actualPrice, plan.renewPrice].filter((v) => !isBlank(v)).join(" / ")],
      ["计费周期", plan.billingCycle],
    ].filter(([, value]) => !isBlank(value));
    const renew = data.autoRenewClosed === true
      ? "自动续订已被关闭"
      : (plan.autoRenew ? "自动续费中" : "未开启自动续费");
    host.append(cardWithRows(String(plan.productName || "套餐"), rows, renew));
  } else if (!isBlank(data.autoRenewClosed)) {
    host.append(cardWithRows("套餐", [["自动续订", data.autoRenewClosed === true ? "已被平台关闭" : "正常"]]));
  }

  const resets = data.quotaResets;
  if (resets && typeof resets === "object" && !Array.isArray(resets)) {
    host.append(cardWithRows("额度重置记录", [
      ["5 小时额度", ((resets.fiveHourResets || []).length) + " 次重置"],
      ["周额度", ((resets.weekResets || []).length) + " 次重置"],
    ]));
  }

  const trial = data.trialTokens;
  const trialTokens = trial && typeof trial === "object" ? trial.tokens : trial;
  if (!isBlank(trialTokens)) {
    host.append(cardWithRows("体验卡", [["Token 总量", tokens(trialTokens)]]));
  }

  const balanceRows = scalarRows(data.balance);
  if (balanceRows.length) host.append(cardWithRows("账户资金", balanceRows));

  const customerRows = scalarRows(data.customer);
  if (customerRows.length) host.append(cardWithRows("账户信息", customerRows));

  const errorRows = Object.entries(errors).map(([name, err]) => [name, (err && err.message) || "请求失败"]);
  if (errorRows.length) host.append(cardWithRows("部分接口失败", errorRows));

  if (!host.childElementCount) host.append(el("div", "card empty", "暂无套餐信息"));
}

async function loadAccount(force) {
  const host = $("account");
  try {
    const body = await getJSON("/api/v1/account" + (force ? "?refresh=1" : ""));
    renderAccount(body);
  } catch (err) {
    host.textContent = "";
    host.append(el("div", "card empty", "套餐信息加载失败：" + err.message));
  }
}

function tickResets() {
  const now = Date.now();
  for (const [node, ts] of nextResets) {
    const left = ts - now;
    if (left <= 0) { node.textContent = "即将重置"; continue; }
    const s = Math.floor(left / 1000);
    const days = Math.floor(s / 86400);
    const hours = Math.floor((s % 86400) / 3600);
    const minutes = Math.floor((s % 3600) / 60);
    let text;
    if (days > 0) text = days + " 天 " + hours + " 小时";
    else if (hours > 0) text = hours + " 小时 " + minutes + " 分";
    else text = minutes + " 分 " + (s % 60) + " 秒";
    node.textContent = text + "后重置";
  }
}

function showErrors(errors) {
  const host = $("errors");
  host.textContent = "";
  if (!errors) return;
  for (const [section, err] of Object.entries(errors)) {
    if (!err) continue;
    host.append(el("div", null, section + "：" + (err.message || err.type)));
  }
}

async function load(force) {
  if (inflight) return;
  inflight = true;
  const button = $("refresh");
  button.disabled = true;
  try {
    const body = await getJSON("/api/v1/overview" + (force ? "?refresh=1" : ""));
    const data = body.data || {};
    renderQuota(data.quota);
    renderStats(data);
    renderChart(data);
    renderModels(data);
    renderPerformance(data);
    showErrors(body.meta && body.meta.errors);
    $("updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
    $("banner").hidden = true;
  } catch (err) {
    $("updated").textContent = "更新失败";
    const banner = $("banner");
    banner.textContent = "加载失败：" + err.message;
    banner.hidden = false;
  } finally {
    inflight = false;
    button.disabled = false;
  }
}

async function boot() {
  try {
    const health = await getJSON("/healthz");
    if (health.refreshSeconds) refreshSeconds = health.refreshSeconds;
    if (!health.token || !health.token.configured) {
      const banner = $("banner");
      banner.textContent = "服务端未配置 bigmodel token：设置 BIGMODEL_TOKEN 环境变量，或把 JWT 写入 BIGMODEL_TOKEN_FILE 指向的文件。";
      banner.hidden = false;
    }
  } catch (_) { /* health is best effort */ }

  await load(false);
  loadAccount(false);
  $("refresh").addEventListener("click", () => { load(true); loadAccount(true); });
  setInterval(() => { if (!document.hidden) load(false); }, refreshSeconds * 1000);
  setInterval(tickResets, 1000);
}

boot();
