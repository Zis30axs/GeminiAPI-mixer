"""Public static shell; quota data requires a manually entered Mixer key."""

from base64 import b64encode
from hashlib import sha256

__all__ = ["HTML_PAGE", "CONTENT_SECURITY_POLICY"]

HTML_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Mixer 模型与额度</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--ink:#17243b;--muted:#59677c;--line:#dce2ec;--accent:#2558c5;--soft:#edf2fc;--bad:#a23434}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1500px;margin:auto;padding:32px 24px 56px}h1{font-size:29px;line-height:1.3;margin:0 0 8px}h2{font-size:19px;margin:0 0 12px}p{margin:8px 0}.muted,small{color:var(--muted)}small{display:block;font-size:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-top:20px}
.eyebrow{font-size:12px;font-weight:700;letter-spacing:.1em;color:var(--accent);margin-bottom:9px}form,.controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap}form label{font-weight:600}input[type=password]{flex:1;min-width:220px;max-width:490px}input,button{font:inherit}input[type=password],button{border:1px solid var(--line);border-radius:8px;padding:9px 13px;background:var(--card);color:var(--ink)}button{cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.5;cursor:default}button:focus-visible,input:focus-visible{outline:3px solid #91b3ff;outline-offset:2px}.controls{margin-top:15px}.controls label{display:flex;align-items:center;gap:7px}.status{padding:10px 13px;border-radius:8px;background:var(--soft);margin-top:14px}.status[data-kind=error]{color:var(--bad)}.summary{display:flex;flex-wrap:wrap;gap:16px 40px;margin:0}.summary div{min-width:130px}.summary dt{color:var(--muted);font-size:12px}.summary dd{font-size:18px;margin:3px 0 0;font-weight:600}.note{font-size:13px}.table-wrap{overflow:auto;margin-top:16px}table{border-collapse:collapse;width:100%;min-width:1050px;text-align:left}th,td{padding:13px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);white-space:nowrap}td{font-size:13px}td:first-child{min-width:260px}code{font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}td button{display:block;font-size:12px;padding:2px 9px;margin-top:7px}.pill{display:inline-block;border-radius:5px;background:var(--soft);padding:1px 7px;font-size:12px}.empty{padding:28px 12px;color:var(--muted);text-align:center}.source{overflow-wrap:anywhere;font-size:12px}.wallet-window{font-size:13px;color:var(--muted);margin-top:8px}footer{font-size:12px;color:var(--muted);margin-top:20px}
@media(prefers-color-scheme:dark){:root{--bg:#111722;--card:#1a2433;--ink:#e7ecf5;--muted:#acb8cd;--line:#35435a;--accent:#86b0ff;--soft:#273953;--bad:#ffb1a8}button.primary{color:#111722}}
@media(max-width:640px){main{padding:23px 14px 40px}.card{padding:16px}h1{font-size:25px}form{align-items:stretch}form label{width:100%}input[type=password]{max-width:none;width:100%}.summary{gap:15px 25px}}
</style>
</head>
<body>
<main>
<div class="eyebrow">GEMINI API MIXER</div>
<h1>模型与额度</h1>
<p class="muted">查看 Antigravity 通道的实测状态、模型配额与重置时间。</p>
<section class="card" aria-label="连接额度接口">
<form id="connect-form" autocomplete="off">
<label for="mixer-key">Mixer Key</label>
<input id="mixer-key" type="password" autocomplete="off" spellcheck="false" autocapitalize="none" maxlength="4096" placeholder="手工输入 Mixer Key" aria-describedby="key-note">
<button id="connect" class="primary" type="submit">连接并刷新</button>
<button id="disconnect" type="button" disabled>清除密钥</button>
</form>
<p id="key-note" class="muted note">密钥仅保存在本页内存，提交后输入框会清空；刷新或关闭页面即忘记。请使用 HTTPS 或本机地址。</p>
<div class="controls"><button id="refresh" type="button" disabled>立即刷新</button><label><input id="auto-refresh" type="checkbox" checked>每 60 秒刷新</label><span id="received-at" class="muted note">尚未读取数据</span></div>
<div id="status" class="status" role="status" aria-live="polite">输入密钥后连接。此页面不发起推理请求。</div>
</section>
<section class="card" aria-labelledby="wallet-title">
<h2 id="wallet-title">平台钱包</h2>
<p class="muted note">这是 Sub2API Key／钱包额度，与 Google 模型配额分开计算。</p>
<dl class="summary"><div><dt>状态</dt><dd id="wallet-status">未确认</dd></div><div><dt>剩余</dt><dd id="wallet-remaining">未确认</dd></div><div><dt>单位</dt><dd id="wallet-unit">未确认</dd></div><div><dt>观测时间</dt><dd id="wallet-time">未确认</dd></div></dl>
<p id="wallet-source" class="muted source">来源：未确认</p><div id="wallet-windows"></div>
</section>
<section class="card" aria-labelledby="models-title">
<h2 id="models-title">模型配额 <span id="model-count" class="pill">0 项</span></h2>
<p class="note">仅「已实测」表示请求验证成功；名称中的 low / medium / high 不能单独证明档位有效。实测记录也不保证当前仍有可用额度。</p>
<p class="muted note">别名与其指向模型可能共用配额桶，同一配额不重复累计。未确认的 Claude 额度保留为「未确认」，不会按 0% 展示。所有时间以本机时区显示。</p>
<div class="table-wrap"><table><thead><tr><th scope="col">模型 / 来源</th><th scope="col">验证记录</th><th scope="col">当前可用性</th><th scope="col">思考控制</th><th scope="col">模型剩余</th><th scope="col">重置时间</th><th scope="col">配额观测</th></tr></thead><tbody id="models"><tr><td colspan="7" class="empty">连接后显示模型。</td></tr></tbody></table></div>
</section>
<footer>页面读取同源 Mixer 额度接口；自动刷新读取的是服务端快照。请结合来源时间和过期状态判断新鲜度。配额百分比不是剩余请求数或 token 数。</footer>
</main>
<script>
(() => {
  "use strict";
  const ENDPOINT = "/v1/quota/antigravity";
  const MAX_BYTES = 2 * 1024 * 1024;
  const byId = (id) => document.getElementById(id);
  let mixerKey = "";
  let busy = false;
  let activeController = null;
  let refreshTimer = null;
  let authVersion = 0;
  let hasData = false;

  const asObject = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const safeText = (value, fallback = "未确认") => typeof value === "string" && value.trim() ? value.slice(0, 500) : fallback;
  const finiteNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const label = (labels, value, fallback = "未确认") => typeof value === "string" && Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : fallback;
  const percent = (value) => finiteNumber(value) && value >= 0 && value <= 100 ? value.toLocaleString("zh-CN") + "%" : null;
  function timeText(value) {
    if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return "未确认";
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", {hour12: false}) : "未确认";
  }
  function status(message, kind = "info") {
    byId("status").textContent = message;
    byId("status").dataset.kind = kind;
  }
  function textNode(tag, value, className) {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  }
  function cell(row, value, detail) {
    const node = document.createElement("td");
    node.append(textNode("span", value));
    if (detail) node.append(textNode("small", detail));
    row.append(node);
    return node;
  }
  function availabilityText(value) {
    return label({available: "可用", temporarily_unavailable: "暂不可用", unavailable: "不可用", unverified: "未验证"}, value);
  }
  function verificationText(value) {
    return label({verified: "已实测", upstream_declared: "上游声明 · 未实测", unavailable: "实测不可用"}, value, "未验证");
  }
  function quotaStatus(value) {
    return label({available: "已报告额度", unknown: "未确认", unavailable: "暂不可读取", reported_exhausted_or_unknown: "耗尽或未知，未确认", stale: "历史快照", mixed: "多账号状态不同"}, value);
  }
  function remainingText(quota) {
    const value = percent(quota.remaining_percent);
    if (value !== null) return value + (quota.stale === true ? "（历史观测）" : "");
    const range = asObject(quota.remaining_percent_range);
    const minimum = percent(range.min);
    const maximum = percent(range.max);
    if (minimum !== null && maximum !== null && range.min <= range.max) return minimum + "–" + maximum + "（多账号范围）";
    return quota.reported_used_percent === 100 ? "未确认（上游报告已用 100%）" : "未确认";
  }
  function renderWallet(data) {
    byId("wallet-status").textContent = label({available: "已报告余额", unlimited: "该 Key 无额度上限", unavailable: "暂不可读取", unknown: "未确认"}, data.status);
    byId("wallet-remaining").textContent = data.unlimited === true || data.remaining === -1 ? "该 Key 无额度上限" : finiteNumber(data.remaining) && data.remaining >= 0 ? data.remaining.toLocaleString("zh-CN", {maximumFractionDigits: 6}) : "未确认";
    byId("wallet-unit").textContent = safeText(data.unit);
    byId("wallet-time").textContent = timeText(data.observed_at);
    byId("wallet-source").textContent = "来源：" + safeText(data.source) + " / " + safeText(data.transport) + " · 范围：" + safeText(data.scope);
    byId("wallet-windows").replaceChildren();
    const windows = Array.isArray(data.windows) ? data.windows.slice(0, 16) : [];
    for (const item of windows) {
      const window = asObject(item);
      const remaining = finiteNumber(window.remaining) && window.remaining >= 0 ? window.remaining.toLocaleString("zh-CN") : "未确认";
      byId("wallet-windows").append(textNode("p", safeText(window.window) + " 窗口：剩余 " + remaining + " " + safeText(window.unit, "") + "；重置 " + timeText(window.reset_at), "wallet-window"));
    }
  }
  function renderModels(models) {
    const table = byId("models");
    table.replaceChildren();
    byId("model-count").textContent = models.length + " 项";
    for (const item of models) {
      const model = asObject(item);
      const quota = asObject(model.quota);
      const verification = asObject(model.verification);
      const availability = asObject(model.availability);
      const row = document.createElement("tr");
      const identity = document.createElement("td");
      const id = safeText(model.id);
      identity.append(textNode("code", id));
      identity.append(textNode("small", safeText(model.source) + " / " + safeText(model.transport)));
      if (typeof model.alias_of === "string") identity.append(textNode("small", "别名 → " + safeText(model.alias_of)));
      if (typeof model.quota_model_id === "string" || typeof quota.model === "string") identity.append(textNode("small", "配额桶：" + safeText(model.quota_model_id, safeText(quota.model))));
      const copy = textNode("button", "复制模型名");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          if (!navigator.clipboard || typeof model.id !== "string") throw new Error();
          await navigator.clipboard.writeText(model.id);
          status("模型名已复制。");
        } catch (_) { status("无法自动复制，请选中模型名手工复制。", "error"); }
      });
      identity.append(copy);
      row.append(identity);
      const httpStatus = Number.isInteger(verification.http_status) && verification.http_status >= 100 && verification.http_status <= 599 ? "HTTP " + verification.http_status + " · " : "";
      cell(row, verificationText(verification.status), httpStatus + "实测时间：" + timeText(verification.tested_at));
      cell(row, availabilityText(availability.status), safeText(availability.reason, ""));
      const effort = ["low", "medium", "high"].includes(model.requested_effort) ? model.requested_effort : null;
      cell(row, effort === null ? "上游控制 / 未确认档位" : effort + (verification.status === "verified" ? " · 已实测" : " · 未实测"), safeText(model.thinking_control, ""));
      cell(row, remainingText(quota), quotaStatus(quota.status));
      cell(row, timeText(quota.reset_at));
      const age = finiteNumber(quota.age_seconds) && quota.age_seconds >= 0 ? Math.ceil(quota.age_seconds) + " 秒前" : "年龄未确认";
      const freshness = quota.stale === true ? "已过期" : quota.stale === false ? "未过期" : "过期状态未确认";
      const observation = cell(row, timeText(quota.observed_at), age + " · " + freshness);
      observation.append(textNode("small", "来源：" + safeText(quota.source)));
      table.append(row);
    }
    if (!models.length) {
      const row = document.createElement("tr");
      const empty = cell(row, "暂无可展示模型，请检查上游模型配置和访问权限。");
      empty.colSpan = 7;
      empty.className = "empty";
      table.append(row);
    }
  }
  function buttons() {
    byId("connect").disabled = busy;
    byId("refresh").disabled = busy || !mixerKey;
    byId("disconnect").disabled = !mixerKey;
  }
  function schedule() {
    if (refreshTimer !== null) clearInterval(refreshTimer);
    refreshTimer = null;
    if (byId("auto-refresh").checked && mixerKey) refreshTimer = setInterval(refresh, 60000);
  }
  async function readData(response) {
    if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get("Content-Type") || "") || !response.body) throw new Error();
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", {fatal: true});
    let size = 0;
    let body = "";
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        size += chunk.value.byteLength;
        if (size > MAX_BYTES) throw new Error();
        body += decoder.decode(chunk.value, {stream: true});
      }
      body += decoder.decode();
      const data = JSON.parse(body);
      if (!data || typeof data !== "object" || Array.isArray(data) || !Array.isArray(data.models) || data.models.length > 2048 || data.models.some((model) => !model || typeof model !== "object" || Array.isArray(model) || typeof model.id !== "string")) throw new Error();
      return data;
    } finally {
      try { await reader.cancel(); } catch (_) { /* No upstream body or error is displayed. */ }
      reader.releaseLock();
    }
  }
  async function refresh() {
    if (busy || !mixerKey) return;
    busy = true;
    buttons();
    const version = authVersion;
    const controller = new AbortController();
    activeController = controller;
    const timeout = setTimeout(() => controller.abort(), 20000);
    let failure = "额度数据格式无效或连接失败，请稍后重试。";
    status("正在读取额度…");
    try {
      const response = await fetch(ENDPOINT, {
        method: "GET", mode: "same-origin", credentials: "omit", redirect: "error", cache: "no-store",
        headers: {"Accept": "application/json", "Authorization": "Bearer " + mixerKey}, signal: controller.signal
      });
      if (!response.ok) {
        failure = response.status === 401 || response.status === 403 ? "认证失败，请检查 Mixer Key 和访问权限。" : response.status === 404 ? "额度接口返回 HTTP 404，请检查 Mixer 版本或接口路径。" : "额度接口暂不可用（HTTP " + response.status + "），请稍后重试。";
        throw new Error();
      }
      const data = await readData(response);
      if (version !== authVersion) return;
      renderWallet(data);
      renderModels(data.models);
      hasData = true;
      byId("received-at").textContent = "页面接收：" + new Date().toLocaleString("zh-CN", {hour12: false});
      status("已更新。模型配额请以各行的来源时间与过期状态为准。");
    } catch (_) {
      if (version === authVersion) status((controller.signal.aborted ? "读取超时，请稍后重试。" : failure) + (hasData ? " 下方保留上次成功读取的数据。" : ""), "error");
    } finally {
      clearTimeout(timeout);
      if (activeController === controller) activeController = null;
      busy = false;
      buttons();
    }
  }
  byId("connect-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const candidate = byId("mixer-key").value.trim();
    byId("mixer-key").value = "";
    if (!candidate || candidate.length > 4096 || /[\x00-\x20\x7f]/.test(candidate)) { status("请输入有效的 Mixer Key。", "error"); return; }
    if (location.protocol !== "https:" && !(location.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname))) { status("请通过 HTTPS 或本机地址打开页面后再输入密钥。", "error"); return; }
    mixerKey = candidate;
    authVersion += 1;
    schedule();
    refresh();
  });
  byId("disconnect").addEventListener("click", () => {
    mixerKey = "";
    authVersion += 1;
    byId("mixer-key").value = "";
    if (activeController) activeController.abort();
    schedule();
    hasData = false;
    renderWallet({});
    renderModels([]);
    byId("received-at").textContent = "尚未读取数据";
    buttons();
    status("密钥与页面数据已清除。");
  });
  byId("refresh").addEventListener("click", refresh);
  byId("auto-refresh").addEventListener("change", schedule);
  window.addEventListener("pagehide", () => {
    mixerKey = "";
    authVersion += 1;
    byId("mixer-key").value = "";
    if (activeController) activeController.abort();
    if (refreshTimer !== null) clearInterval(refreshTimer);
  });
})();
</script>
</body>
</html>
'''

# Hash the exact UTF-8 script text, including its surrounding newlines. Serve
# this as an HTTP header: frame-ancestors is not enforced by CSP meta elements.
_SCRIPT_TEXT = HTML_PAGE.partition("<script>")[2].partition("</script>")[0]
_SCRIPT_HASH = b64encode(sha256(_SCRIPT_TEXT.encode("utf-8")).digest()).decode("ascii")
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'sha256-" + _SCRIPT_HASH
    + "'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
