/* Exercise the shipped dashboard script without credentials or network access. */
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");
const SYNTHETIC_KEY = "synthetic-dashboard-test-key";
const rendered = new Map();

function render(origins = "") {
  if (!rendered.has(origins)) {
    const program = [
      "import sys",
      "from gemini_web2api.quota_dashboard import render_dashboard",
      "sys.stdout.write(render_dashboard({'MIXER_DASHBOARD_HTTP_ORIGINS': sys.argv[1]})[0])",
    ].join("\n");
    const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
    const result = spawnSync(python, ["-c", program, origins], {
      cwd: ROOT, encoding: "utf8", maxBuffer: 256 * 1024,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    });
    assert.equal(result.status, 0, result.stderr || "Dashboard rendering failed");
    const script = result.stdout.match(/<script>([\s\S]*?)<\/script>/);
    assert.ok(script, "Rendered page contains the executable dashboard script");
    rendered.set(origins, { html: result.stdout, script: script[1] });
  }
  return rendered.get(origins);
}

// The dashboard needs only elements, text, child lists and event listeners.
// No HTML interpretation, selectors, layout or simulated browser storage.
class Element {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(""); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ""; this.children = nodes; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  dispatch(type) {
    const listener = this.listeners.get(type);
    assert.equal(typeof listener, "function", "Dashboard registered " + type);
    return listener({ preventDefault() {} });
  }
}

function payload() {
  return {
    source: "antigravity", transport: "sub2api-openai", scope: "sub2api-key-wallet",
    status: "available", remaining: null, unit: "USD", observed_at: "2026-10-17T10:00:00Z",
    models: [
      {
        id: "ag-gemini-pro-low", source: "antigravity", transport: "sub2api-openai",
        quota_model_id: "gemini-pro-low", requested_effort: "low", thinking_control: "upstream-model-variant",
        verification: { status: "verified", http_status: 200, tested_at: "2026-10-17T09:00:00Z" },
        availability: { status: "available", reason: "generation_verified" },
        quota: { source: "sub2api-account-quota", status: "available", remaining_percent: 90,
          reset_at: "2026-10-17T12:00:00Z", observed_at: "2026-10-17T09:59:30Z", age_seconds: 30, stale: false },
      },
      {
        id: "ag-claude-sonnet", source: "antigravity", transport: "sub2api-openai",
        verification: { status: "upstream_declared", http_status: 429 },
        availability: { status: "temporarily_unavailable", reason: "rate_limited" },
        quota: { source: "sub2api-account-quota", status: "reported_exhausted_or_unknown",
          remaining_percent: null, reported_used_percent: 100, reset_at: null, age_seconds: null },
      },
    ],
  };
}

function browser(pageURL, { origins = "", framed = false, data = payload(), responseStatus = 200 } = {}) {
  const page = render(origins);
  const nodes = new Map([...page.html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  const requests = [];
  const location = new URL(pageURL);
  const window = { addEventListener() {} };
  window.self = window;
  window.top = framed ? {} : window;
  const document = {
    getElementById(id) { assert.ok(nodes.has(id), "Existing dashboard element: " + id); return nodes.get(id); },
    createElement(tagName) { return new Element(tagName); },
  };
  const context = vm.createContext({
    window, document, location, URL, TextDecoder, AbortController,
    navigator: {}, setTimeout, clearTimeout, setInterval, clearInterval,
    fetch: async (input, options) => {
      const url = new URL(input, pageURL);
      requests.push({ input, url, options });
      assert.equal(url.origin, location.origin, "Quota reads remain on the page origin");
      return {
        ok: responseStatus >= 200 && responseStatus < 300, status: responseStatus,
        headers: { get(name) { return name.toLowerCase() === "content-type" ? "application/json; charset=utf-8" : null; } },
        body: new ReadableStream({ start(controller) {
          controller.enqueue(new TextEncoder().encode(JSON.stringify(data)));
          controller.close();
        } }),
      };
    },
  });
  vm.runInContext(page.script, context, { timeout: 1000, filename: "quota-dashboard-inline.js" });
  // An explicit unchecked checkbox prevents periodic timers in this unit harness.
  nodes.get("auto-refresh").checked = false;
  return {
    nodes, requests,
    async submit() {
      nodes.get("mixer-key").value = SYNTHETIC_KEY;
      nodes.get("connect-form").dispatch("submit");
      for (let attempt = 0; attempt < 12; attempt += 1) {
        await new Promise(setImmediate);
        if (!nodes.get("connect").disabled && requests.length) break;
      }
    },
  };
}

test("prefixed dashboard submits its key only to /gemini/v1 quota with safe fetch options", async () => {
  const page = browser("https://gateway.example/gemini/quota");
  assert.equal(page.requests.length, 0, "Opening the shell does not fetch quota");
  await page.submit();
  assert.equal(page.requests.length, 1);
  const { url, options } = page.requests[0];
  assert.equal(url.href, "https://gateway.example/gemini/v1/quota/antigravity");
  assert.equal(url.search, "");
  assert.equal(options.headers.Authorization, "Bearer " + SYNTHETIC_KEY);
  assert.equal(options.method, "GET");
  assert.equal(options.mode, "same-origin");
  assert.equal(options.credentials, "omit");
  assert.equal(options.redirect, "error");
  assert.equal(options.cache, "no-store");
  assert.equal(page.nodes.get("mixer-key").value, "");
  assert.match(page.nodes.get("status").textContent, /已更新/);
});

test("unprefixed loopback dashboard retains the /v1 quota path", async () => {
  const page = browser("http://127.0.0.1:18083/quota");
  await page.submit();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url.href, "http://127.0.0.1:18083/v1/quota/antigravity");
});

test("public plaintext HTTP is blocked by default even for a manually dispatched submit", async () => {
  const page = browser("http://gateway.example:60000/gemini/quota");
  assert.equal(page.nodes.get("mixer-key").disabled, true);
  assert.equal(page.nodes.get("connect").disabled, true);
  await page.submit();
  assert.equal(page.requests.length, 0);
  assert.equal(page.nodes.get("mixer-key").value, "");
  assert.match(page.nodes.get("status").textContent, /当前来源未获允许/);
});

test("explicit HTTP origin enables only that exact origin and never an embedded page", async () => {
  const origins = "http://gateway.example:60000";
  const trusted = browser(origins + "/gemini/quota", { origins });
  await trusted.submit();
  assert.equal(trusted.requests.length, 1);
  for (const [url, framed] of [
    ["http://different.example:60000/gemini/quota", false],
    ["http://gateway.example:60001/gemini/quota", false],
    [origins + "/gemini/quota", true],
  ]) {
    const rejected = browser(url, { origins, framed });
    await rejected.submit();
    assert.equal(rejected.requests.length, 0, "Origin/frame gate rejects " + url);
    assert.equal(rejected.nodes.get("connect").disabled, true);
  }
});

test("successful quota rendering preserves reset, age and unknown balances without inventing zero", async () => {
  const page = browser("https://gateway.example/gemini/quota");
  await page.submit();
  const rows = page.nodes.get("models").children;
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /ag-gemini-pro-low/);
  assert.match(rows[0].textContent, /90%/);
  assert.match(rows[0].textContent, /30 秒前/);
  const reset = new Date("2026-10-17T12:00:00Z").toLocaleString("zh-CN", { hour12: false });
  assert.ok(rows[0].textContent.includes(reset));
  assert.equal(rows[1].children[4].children[0].textContent, "未确认（上游报告已用 100%）");
  assert.equal(rows[1].children[5].textContent, "未确认");
  assert.equal(page.nodes.get("wallet-remaining").textContent, "未确认");
  assert.equal(page.nodes.get("model-count").textContent, "2 项");
});

test("clearing the key clears quota and prevents a manually dispatched refresh", async () => {
  const page = browser("https://gateway.example/gemini/quota");
  await page.submit();
  page.nodes.get("disconnect").dispatch("click");
  assert.equal(page.nodes.get("refresh").disabled, true);
  assert.equal(page.nodes.get("disconnect").disabled, true);
  assert.equal(page.nodes.get("wallet-remaining").textContent, "未确认");
  assert.equal(page.nodes.get("model-count").textContent, "0 项");
  await page.nodes.get("refresh").dispatch("click");
  assert.equal(page.requests.length, 1);
  assert.match(page.nodes.get("status").textContent, /密钥与页面数据已清除/);
});
