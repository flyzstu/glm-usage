const $ = (id) => document.getElementById(id);
const RESET_LABELS = { 3: "5 小时窗口", 6: "周额度" };
const API_KEY_STORAGE = "glm-usage-api-key";
let refreshSeconds = 30;
let nextResets = [];
let inflight = false;
let pendingResetType = null;
let oauthPollingTimer = null;

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
const barColor = (used) => used >= 90 ? "var(--bad)" : used >= 70 ? "var(--warn)" : "var(--good)";

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

// ==================== 吐司通知系统 ====================
function showToast(message, type = "info") {
  const container = $("toast-container");
  if (!container) return;
  const toast = el("div", "toast");
  if (type === "success") {
    toast.style.borderColor = "rgba(16, 185, 129, 0.4)";
    toast.style.background = "rgba(16, 185, 129, 0.12)";
    toast.style.color = "#34d399";
  } else if (type === "error") {
    toast.style.borderColor = "rgba(239, 68, 68, 0.4)";
    toast.style.background = "rgba(239, 68, 68, 0.12)";
    toast.style.color = "#f87171";
  } else if (type === "warn") {
    toast.style.borderColor = "rgba(245, 158, 11, 0.4)";
    toast.style.background = "rgba(245, 158, 11, 0.12)";
    toast.style.color = "#fbbf24";
  }
  toast.textContent = message;
  container.append(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transition = "opacity 0.25s ease";
    setTimeout(() => toast.remove(), 250);
  }, 3200);
}

// ==================== 代码片段复制 ====================
window.copySnippet = function(id) {
  const node = $(id);
  if (!node) return;
  const text = node.textContent;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      showToast("已成功复制到剪贴板！", "success");
    }).catch(() => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
};

function fallbackCopy(text) {
  const input = document.createElement("textarea");
  input.value = text;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  try {
    document.execCommand("copy");
    showToast("已成功复制到剪贴板！", "success");
  } catch (_) {
    showToast("复制失败，请手动选取复制", "error");
  }
  document.body.removeChild(input);
}

// ==================== 网络请求层 ====================
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
    if (type === "unauthorized" || type === "forbidden") {
      throw new Error("未授权：请用 " + window.location.pathname + "?key=你的密钥 打开一次本页");
    }
    throw new Error(msg);
  }
  return body;
}

async function postJSON(url, data) {
  const headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
  };
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;
  const res = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(data || {}),
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON error page */ }
  if (!res.ok) {
    const msg = body && (body.error?.message || body.error?.type || body.message) || (res.status + " " + res.statusText);
    throw new Error(msg);
  }
  return body;
}

// ==================== TAB 选项卡切换 ====================
function setupTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  const contents = document.querySelectorAll(".tab-content");

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const tabId = btn.getAttribute("data-tab");
      buttons.forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      contents.forEach((c) => c.classList.remove("active"));

      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
      const target = $(tabId);
      if (target) target.classList.add("active");

      // 切换即时轻量拉取对应 Tab 数据
      if (tabId === "tab-proxy") loadProxyStatus();
      else if (tabId === "tab-credentials") loadCredentials();
      else if (tabId === "tab-quota") loadResetStatus();
    });
  });
}

// ==================== TAB 1: 配额与重置卡 ====================
function renderQuota(quota) {
  const host = $("quota-cards");
  if (!host) return;
  host.textContent = "";

  const levelNode = $("plan-level");
  if (levelNode) {
    levelNode.textContent = quota && quota.level ? String(quota.level) : "--";
  }

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

    const card = el("div", "card quota-card");

    const head = el("div", "quota-header");
    head.append(el("span", "quota-title", RESET_LABELS[lim.unit] || ("额度 " + lim.unit + "×" + lim.number)));
    const pctSpan = el("span", "quota-percent", percent.toFixed(1) + "%");
    pctSpan.style.color = percent >= 90 ? "var(--bad)" : percent >= 70 ? "var(--warn)" : "var(--good)";
    head.append(pctSpan);
    card.append(head);

    const line = el("div", "quota-numbers");
    line.append(el("b", null, num(used, 2)), document.createTextNode(" / " + num(total, 2) + " 积分"));
    card.append(line);

    const bar = el("div", "progress-bar");
    const fill = el("div", "progress-fill");
    fill.style.width = Math.max(percent, 0.5) + "%";
    fill.style.background = barColor(percent);
    bar.append(fill);
    card.append(bar);

    const foot = el("div", "quota-footer");
    foot.append(el("span", null, "剩余 " + num(lim.remaining, 2) + " 积分"));
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

async function loadResetStatus() {
  const host = $("reset-cards-grid");
  try {
    const res = await getJSON("/api/v1/reset/status");
    renderResetCards(res.data);
  } catch (err) {
    if (host) {
      host.textContent = "";
      host.append(el("div", "card empty", "获取重置卡状态失败: " + err.message));
    }
  }
}

function renderResetCards(data) {
  const host = $("reset-cards-grid");
  if (!host) return;
  host.textContent = "";

  if (!data || data.available === false) {
    const card = el("div", "card");
    const title = el("div", "reset-body-highlight", "重置卡未就绪");
    const desc = el("div", "reset-body", (data && data.reason) || "缺少 ZCode 平台凭据或接口未开通。请先在「凭据与设置」中配置凭据，或通过官方网页授权登录。");
    const actions = el("div", "modal-footer");
    actions.style.padding = "10px 0 0 0";
    actions.style.background = "none";
    actions.style.border = "none";
    actions.style.justifyContent = "flex-start";
    const btn = el("button", "btn btn-sm btn-primary", "前往凭据设置");
    btn.onclick = () => {
      const credTab = document.querySelector('.tab-btn[data-tab="tab-credentials"]');
      if (credTab) credTab.click();
    };
    actions.append(btn);
    card.append(title, desc, actions);
    host.append(card);
    return;
  }

  const fiveHourList = data.five_hour_resets || [];
  const weekList = data.week_resets || [];

  // 1. 5小时重置卡
  const card5 = createResetCardDOM({
    type: "FIVE_HOUR",
    title: "5小时额度重置卡",
    badge: "FIVE_HOUR",
    count: fiveHourList.length,
    desc: "使用后立即清零当前 5 小时滑动窗口内的已用积分，恢复 100% 额度上限。",
    items: fiveHourList,
  });
  host.append(card5);

  // 2. 周度重置卡
  const cardWeek = createResetCardDOM({
    type: "WEEK",
    title: "周度额度重置卡",
    badge: "WEEK",
    count: weekList.length,
    desc: "使用后立即清零本周累计已用积分，恢复每周总额度上限。",
    items: weekList,
  });
  host.append(cardWeek);

  // 历史核销记录
  renderResetHistory(data);
}

function createResetCardDOM({ type, title, badge, count, desc, items }) {
  const card = el("div", "card reset-card");

  const head = el("div", "reset-card-head");
  head.append(el("span", "quota-title", title));
  head.append(el("span", "reset-badge", badge));
  card.append(head);

  const body = el("div", "reset-body");
  const countDiv = el("div", "reset-body-highlight", count + " 张可用");
  if (count === 0) countDiv.style.color = "var(--dim)";
  body.append(countDiv);
  body.append(el("div", null, desc));

  if (items && items.length > 0) {
    const expireInfo = items[0].expires_at || items[0].expire_time || items[0].valid_until;
    if (expireInfo) {
      body.append(el("div", "form-hint", "最近一张有效期至: " + expireInfo));
    }
  }
  card.append(body);

  const foot = el("div", "reset-footer");
  const statusSpan = el("span", null, count > 0 ? "即时生效 · 无冷却" : "暂无可核销卡券");
  statusSpan.className = count > 0 ? "text-good" : "text-dim";
  foot.append(statusSpan);

  const btn = el("button", "btn btn-sm " + (count > 0 ? "btn-good" : "btn-secondary"), "立即核销");
  if (count === 0) {
    btn.disabled = true;
  } else {
    btn.onclick = () => openResetConfirmModal(type, title);
  }
  foot.append(btn);
  card.append(foot);

  return card;
}

function renderResetHistory(data) {
  const host = $("reset-history");
  if (!host) return;
  host.textContent = "";

  const hist5 = data.latest_five_hour_history;
  const histWeek = data.latest_week_history;

  if (!hist5 && !histWeek) {
    host.append(el("div", "empty", "暂无历史核销记录"));
    return;
  }

  if (hist5) {
    const item = el("div", "history-item");
    const left = el("div");
    left.append(el("b", null, "5小时额度重置卡"), el("div", "form-hint", hist5.created_at || hist5.use_time || "已核销"));
    const right = el("span", "badge", "已清零当前窗口");
    item.append(left, right);
    host.append(item);
  }

  if (histWeek) {
    const item = el("div", "history-item");
    const left = el("div");
    left.append(el("b", null, "周度额度重置卡"), el("div", "form-hint", histWeek.created_at || histWeek.use_time || "已核销"));
    const right = el("span", "badge", "已清零本周累计");
    item.append(left, right);
    host.append(item);
  }
}

// ==================== 核销确认弹窗 ====================
function openResetConfirmModal(resetType, resetTitle) {
  pendingResetType = resetType;
  const modal = $("modal-confirm-reset");
  const title = $("modal-reset-title");
  const body = $("modal-reset-body");

  if (title) title.textContent = `确认核销 ${resetTitle}？`;
  if (body) {
    body.textContent = resetType === "FIVE_HOUR"
      ? "核销后当前 5 小时滑动窗口内已消耗的额度将立即归零并恢复上限。此操作不可逆，请确认是否立即核销。"
      : "核销后本周内已消耗的额度将立即归零并恢复上限。此操作不可逆，请确认是否立即核销。";
  }
  if (modal) modal.hidden = false;
}

function setupResetConfirmModal() {
  const modal = $("modal-confirm-reset");
  const closeBtn = $("modal-reset-close");
  const cancelBtn = $("modal-reset-cancel");
  const confirmBtn = $("modal-reset-confirm");

  const hide = () => {
    if (modal) modal.hidden = true;
    pendingResetType = null;
  };

  if (closeBtn) closeBtn.onclick = hide;
  if (cancelBtn) cancelBtn.onclick = hide;

  if (confirmBtn) {
    confirmBtn.onclick = async () => {
      if (!pendingResetType) return;
      confirmBtn.disabled = true;
      confirmBtn.textContent = "核销中…";
      try {
        const res = await postJSON("/api/v1/reset/use", { reset_type: pendingResetType });
        showToast(res.message || "核销成功！配额已恢复", "success");
        hide();
        load(true);
        loadResetStatus();
      } catch (err) {
        showToast("核销失败: " + err.message, "error");
      } finally {
        confirmBtn.disabled = false;
        confirmBtn.textContent = "确认核销";
      }
    };
  }
}

// ==================== TAB 2: 透明代理服务端 ====================
async function loadProxyStatus() {
  try {
    const res = await getJSON("/api/v1/proxy/status");
    const data = res.data || {};

    const urlCode = $("proxy-endpoint-url");
    if (urlCode && data.localEndpoints?.v1Messages) {
      urlCode.textContent = data.localEndpoints.v1Messages;
    }

    const pill = $("proxy-status-pill");
    const pillText = $("proxy-status-text");
    if (pill && pillText) {
      if (data.status === "ready") {
        pill.className = "status-pill status-ready";
        pillText.textContent = `代理就绪 (${data.providerName || "BigModel"})`;
      } else {
        pill.className = "status-pill status-warn";
        pillText.textContent = "未配置 API Key";
      }
    }

    // 调用统计指标
    const stats = data.stats || {};
    const total = stats.total_requests || 0;
    const stream = stats.streaming_requests || 0;
    const success = stats.success_requests || 0;
    const errors = stats.error_requests || 0;

    const rate = total > 0 ? ((success / total) * 100).toFixed(1) + "%" : "100%";

    if ($("stat-total-reqs")) $("stat-total-reqs").textContent = num(total);
    if ($("stat-stream-reqs")) $("stat-stream-reqs").textContent = num(stream);
    if ($("stat-success-rate")) $("stat-success-rate").textContent = rate;
    if ($("stat-success-count")) $("stat-success-count").textContent = `2xx 成功: ${num(success)}`;
    if ($("stat-error-count")) $("stat-error-count").textContent = num(errors);

    // 快捷代码块动态更新
    if (data.snippets?.claude_code?.env && $("claude-code-snippet")) {
      $("claude-code-snippet").textContent = data.snippets.claude_code.env;
    }
    if (data.snippets?.curl?.command && $("curl-snippet")) {
      $("curl-snippet").textContent = data.snippets.curl.command;
    }
    if (data.localEndpoints?.v1Messages && $("opencode-snippet")) {
      const origin = data.localEndpoints.v1Messages.replace("/v1/messages", "");
      $("opencode-snippet").textContent = `Base URL: ${origin}\nAPI Key : dummy (或留空)\nModel   : GLM-5.3 (或 claude-3-7-sonnet，服务端自动映射)`;
    }
  } catch (err) {
    console.error("加载代理状态失败:", err);
  }
}

// ==================== TAB 3: 用量与模型分析 ====================
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
  if (!host) return;
  host.textContent = "";
  const items = statsFrom(data);
  if (!items.length) {
    host.append(el("div", "stat-card", "暂无统计数据"));
    return;
  }
  for (const item of items) {
    const card = el("div", "stat-card");
    card.append(el("div", "stat-k", item.sub));
    card.append(el("div", "stat-v", item.value));
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
  if (!host || !axis) return;
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
  if (!body) return;
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
  if (!statHost || !chartHost || !axis) return;
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
    const card = el("div", "stat-card");
    card.append(el("div", "stat-k", label));
    card.append(el("div", "stat-v", format(value)));
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
  if (!host) return;
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
  if (!host) return;
  try {
    const body = await getJSON("/api/v1/account" + (force ? "?refresh=1" : ""));
    renderAccount(body);
  } catch (err) {
    host.textContent = "";
    host.append(el("div", "card empty", "套餐信息加载失败：" + err.message));
  }
}

// ==================== TAB 4: 凭据与设置 ====================
async function loadCredentials() {
  const host = $("credentials-card");
  if (!host) return;
  try {
    const res = await getJSON("/api/v1/credentials");
    const data = res.data || {};

    host.textContent = "";

    const rows = [
      ["服务提供商 (Provider)", data.provider === "zai" ? "Z.ai (海外版)" : "BigModel (国内版)"],
      ["API Key 状态", data.configured ? (data.apiKeyMasked || "已配置") : "未配置"],
      ["重置卡核销能力", data.hasResetCapability ? "已具备 (已注入 OAuth/JWT)" : "未就绪 (缺少 OAuth 或 JWT 令牌)"],
      ["凭据存储路径", data.tokenFile || "~/.zcode_coding_plan_token.json"],
      ["凭据有效期", data.expiresAt || "随官方认证会话持续有效"],
      ["最近更新时间", data.updatedAt || "--"],
    ];

    const cardWrap = el("div");
    for (const [k, v] of rows) {
      const row = el("div", "kv-row");
      row.append(el("span", "k", k), el("span", "v", String(v)));
      cardWrap.append(row);
    }
    host.append(cardWrap);

    // 回填设置选择框
    const provSelect = $("input-provider");
    if (provSelect && data.provider) provSelect.value = data.provider;
  } catch (err) {
    host.textContent = "获取凭据失败：" + err.message;
  }
}

function setupManualCredsModal() {
  const modal = $("modal-manual-creds");
  const openBtn = $("btn-manual-creds");
  const closeBtn = $("modal-creds-close");
  const cancelBtn = $("modal-creds-cancel");
  const saveBtn = $("modal-creds-save");

  const hide = () => { if (modal) modal.hidden = true; };
  const show = () => { if (modal) modal.hidden = false; };

  if (openBtn) openBtn.onclick = show;
  if (closeBtn) closeBtn.onclick = hide;
  if (cancelBtn) cancelBtn.onclick = hide;

  if (saveBtn) {
    saveBtn.onclick = async () => {
      const provider = $("input-provider")?.value;
      const keyInput = $("input-api-key")?.value;
      const zcodeJwt = $("input-zcode-jwt")?.value;
      const oauthToken = $("input-oauth-token")?.value;

      saveBtn.disabled = true;
      saveBtn.textContent = "保存中…";
      try {
        const payload = {};
        if (provider) payload.provider = provider;
        if (keyInput) payload.api_key = keyInput;
        if (zcodeJwt) payload.zcode_jwt_token = zcodeJwt;
        if (oauthToken) payload.oauth_access_token = oauthToken;

        await postJSON("/api/v1/credentials", payload);
        showToast("凭据已成功保存！", "success");
        hide();

        if ($("input-api-key")) $("input-api-key").value = "";
        if ($("input-zcode-jwt")) $("input-zcode-jwt").value = "";
        if ($("input-oauth-token")) $("input-oauth-token").value = "";

        load(true);
        loadCredentials();
        loadResetStatus();
        loadProxyStatus();
      } catch (err) {
        showToast("保存凭据失败: " + err.message, "error");
      } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = "保存并更新";
      }
    };
  }
}

async function startOAuthFlow() {
  const btn = $("btn-start-oauth");
  const statusBox = $("oauth-polling-status");
  if (btn) btn.disabled = true;
  if (statusBox) {
    statusBox.hidden = false;
    statusBox.textContent = "正在向官方申请设备授权流程…";
  }

  try {
    const res = await postJSON("/api/v1/oauth/init", {});
    const data = res.data || {};
    const flowId = data.flow_id;
    const verifyUrl = data.verification_uri_complete || data.verification_uri;
    const userCode = data.user_code;

    statusBox.textContent = "";

    const info = el("div");
    info.style.marginBottom = "10px";
    info.append(
      document.createTextNode("请在弹出的官方页面中完成登录授权。若未自动弹出，请手动点击：")
    );
    const linkNode = el("a", null, verifyUrl || "官方授权链接");
    linkNode.href = verifyUrl;
    linkNode.target = "_blank";
    linkNode.style.color = "#60a5fa";
    linkNode.style.textDecoration = "underline";
    linkNode.style.marginLeft = "6px";
    info.append(linkNode);
    statusBox.append(info);

    if (userCode) {
      const codeRow = el("div");
      codeRow.style.marginBottom = "10px";
      codeRow.append(document.createTextNode("用户确认码: "), el("b", null, userCode));
      statusBox.append(codeRow);
    }

    const pollNotice = el("div", "form-hint", "正在等待浏览器端授权完成（后台自动轮询校验中）…");
    statusBox.append(pollNotice);

    // 尝试直接在浏览器新标签页打开授权页
    if (verifyUrl) {
      window.open(verifyUrl, "_blank");
    }

    // 轮询流程
    if (oauthPollingTimer) clearInterval(oauthPollingTimer);

    oauthPollingTimer = setInterval(async () => {
      try {
        const pollRes = await getJSON(`/api/v1/oauth/poll/${flowId}`);
        const pollData = pollRes.data || {};
        if (pollData.status === "ready") {
          clearInterval(oauthPollingTimer);
          statusBox.textContent = "";
          const okDiv = el("div", "text-good", "✓ 官方授权成功！已自动获取并持久化 Coding Plan API Key 及重置卡凭据。");
          statusBox.append(okDiv);
          showToast("官方 OAuth 授权成功！", "success");
          if (btn) btn.disabled = false;
          load(true);
          loadCredentials();
          loadResetStatus();
          loadProxyStatus();
        } else if (pollData.status === "expired" || pollData.status === "error") {
          clearInterval(oauthPollingTimer);
          statusBox.textContent = "授权已失效或异常：" + (pollData.message || pollData.status);
          if (btn) btn.disabled = false;
          showToast("OAuth 授权失败：" + (pollData.message || pollData.status), "error");
        }
      } catch (_) {
        // 网络抖动继续轮询
      }
    }, 2000);

  } catch (err) {
    if (statusBox) statusBox.textContent = "发起授权失败：" + err.message;
    if (btn) btn.disabled = false;
    showToast("发起授权失败: " + err.message, "error");
  }
}

// ==================== 错误处理与全局加载 ====================
function showErrors(errors) {
  const host = $("errors");
  if (!host) return;
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
  if (button) button.disabled = true;
  try {
    const body = await getJSON("/api/v1/overview" + (force ? "?refresh=1" : ""));
    const data = body.data || {};
    renderQuota(data.quota);
    renderStats(data);
    renderChart(data);
    renderModels(data);
    renderPerformance(data);
    showErrors(body.meta && body.meta.errors);
    if ($("updated")) $("updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
    if ($("banner")) $("banner").hidden = true;
  } catch (err) {
    if ($("updated")) $("updated").textContent = "更新失败";
    const banner = $("banner");
    if (banner) {
      banner.textContent = "加载失败：" + err.message;
      banner.hidden = false;
    }
  } finally {
    inflight = false;
    if (button) button.disabled = false;
  }
}

// ==================== 启动主入口 ====================
async function boot() {
  setupTabs();
  setupResetConfirmModal();
  setupManualCredsModal();

  const startOAuthBtn = $("btn-start-oauth");
  if (startOAuthBtn) startOAuthBtn.onclick = startOAuthFlow;

  const copyBtn = $("copy-endpoint-btn");
  if (copyBtn) {
    copyBtn.onclick = () => {
      const url = $("proxy-endpoint-url")?.textContent;
      if (url) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(() => {
            showToast("本地代理端点已复制！", "success");
          }).catch(() => fallbackCopy(url));
        } else {
          fallbackCopy(url);
        }
      }
    };
  }

  try {
    const health = await getJSON("/healthz");
    if (health.refreshSeconds) refreshSeconds = health.refreshSeconds;
    if (!health.token || !health.token.configured) {
      const banner = $("banner");
      if (banner) {
        banner.textContent = "服务端未检测到有效 API Key：请在「凭据与设置」中配置，或使用官方网页授权登录。";
        banner.hidden = false;
      }
    }
  } catch (_) { /* health is best effort */ }

  await load(false);
  loadResetStatus();
  loadProxyStatus();
  loadCredentials();
  loadAccount(false);

  const refreshBtn = $("refresh");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      load(true);
      loadResetStatus();
      loadProxyStatus();
      loadCredentials();
      loadAccount(true);
      showToast("已刷新全部最新数据", "info");
    });
  }

  setInterval(() => {
    if (!document.hidden) {
      load(false);
      loadResetStatus();
      loadProxyStatus();
    }
  }, refreshSeconds * 1000);

  setInterval(tickResets, 1000);
}

boot();
