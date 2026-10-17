"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const session = require("../aistudio-session-extension/session.js");
const root = path.join(__dirname, "../aistudio-session-extension");

function fixture() {
  return ["SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"].map(name => ({
    name, value: "synthetic-session-value", domain: ".google.com", path: "/",
    secure: true, httpOnly: true, session: true, sameSite: "lax"
  }));
}

test("exports the explicit session without changing cookie attributes or inventing OAuth material", () => {
  const cookies = fixture();
  cookies[0].httpOnly = false;
  const result = session.makeState(cookies, "OWNER@example.com", "5");
  assert.equal(result.cookies[0].httpOnly, false);
  assert.equal(result.cookies[0].expires, -1);
  assert.deepEqual(result.origins, []);
  assert.deepEqual(result.aistudio2api, { source: { browser: "mixer-import", email: "owner@example.com", auth_user: "5" } });
});

test("filters cookies belonging to other Google applications, external domains, or partitions", () => {
  const cookies = fixture();
  for (const domain of [".example.com", "mail.google.com", "google.com.evil.test", ".evilgoogle.com"]) {
    cookies.push({ ...cookies[0], domain });
  }
  cookies.push({ ...cookies[0], name: "partitioned", partitionKey: { topLevelSite: "https://google.com" } });
  assert.equal(session.makeState(cookies, "owner@example.com", "5").cookies.length, 3);
});

test("rejects missing or expired authentication and malformed cookie metadata", () => {
  assert.throws(() => session.makeState(fixture().slice(1), "owner@example.com", "0"), /missing_cookies/);
  const expired = fixture();
  expired[0] = { ...expired[0], session: false, expirationDate: 1 };
  assert.throws(() => session.makeState(expired, "owner@example.com", "0"), /missing_cookies/);
  for (const update of [{ value: "bad\r\nvalue" }, { value: "bad\n" }, { name: "bad;name" }, { name: "bad\n" }, { path: "relative" }, { path: "/\n" }, { sameSite: "unknown" }, { secure: false, name: "__Secure-3PAPISID" }]) {
    const cookies = fixture();
    cookies[0] = { ...cookies[0], ...update };
    assert.throws(() => session.makeState(cookies, "owner@example.com", "0"), /invalid_cookies/);
  }
});

test("requires explicit canonical identity and active AI Studio tab", () => {
  for (const index of ["", "05", "-1", "5&authuser=0", "5\r\n", "5\n", 5]) assert.throws(() => session.identity("owner@example.com", index));
  assert.throws(() => session.identity("../owner@example.com", "0"));
  const base = { id: 1, active: true, url: "https://aistudio.google.com/prompts/new_chat?authuser=5" };
  assert.equal(session.checkTab(base, "5").id, 1);
  assert.throws(() => session.checkTab(base, "0"), /account_mismatch/);
  assert.throws(() => session.checkTab({ ...base, url: "https://aistudio.google.com/u/0/prompts/new_chat" }, "5"), /account_mismatch/);
  assert.throws(() => session.checkTab({ ...base, url: "https://aistudio.google.com/?authuser=5&authuser=5" }, "5"), /account_mismatch/);
  assert.throws(() => session.checkTab({ ...base, active: false }, "5"), /wrong_tab/);
  assert.throws(() => session.checkTab({ ...base, url: "https://example.com" }, "5"), /wrong_tab/);
});

function readPage({ url = "https://aistudio.google.com/prompts/new_chat?authuser=5", wiz = {}, meta = {}, nodes = [], bootstraps = [] } = {}) {
  const document = {
    querySelector(selector) {
      const name = selector.match(/meta\[name="([^"]+)"\]/)?.[1];
      return name && meta[name] !== undefined ? { content: meta[name] } : null;
    },
    querySelectorAll(selector) { return selector === 'script[type="application/json"]' ? bootstraps : nodes; },
    get body() { throw new Error("Must not scan page body, prompts, or history"); },
    get cookie() { throw new Error("Must not read credentials during identity inspection"); }
  };
  return vm.runInNewContext("(" + session.readPageIdentity.toString() + ")()", {
    URL, location: new URL(url), document, WIZ_global_data: wiz
  });
}

function avatar(label, href = null, visible = true) {
  return {
    getBoundingClientRect() { return { width: visible ? 40 : 0, height: visible ? 40 : 0 }; },
    getAttribute(name) { return ({ "aria-label": label, href })[name] || null; }
  };
}

function bootstrap(value, id = "random-bootstrap-id") {
  return { id, textContent: typeof value === "string" ? value : JSON.stringify(value) };
}

test("reads bounded inert bootstrap email without WIZ globals or stable script ID", () => {
  for (const [id, index] of [["19fd2a18-987a-4185-9111-33a7c5ef3fba", "2"], ["changed-on-next-load", "5"]]) {
    const result = readPage({ url: "https://aistudio.google.com/u/" + index + "/prompts/new_chat", bootstraps: [bootstrap({
      WIu0Nc: "synthetic-marker-never-return-this", oPEP7c: "OWNER@example.com",
      token: "synthetic-private-token-never-return-this", unrelated: "x".repeat(21000)
    }, id)] });
    assert.equal(result.email, "owner@example.com");
    assert.equal(result.authUser, index);
    assert.ok(result.sources.includes("bootstrap-json"));
    assert.deepEqual(Object.keys(result).sort(), ["authUser", "email", "sources", "url"]);
    assert.doesNotMatch(JSON.stringify(result), /synthetic|WIu0Nc|token|unrelated/);
  }
  const withoutIndex = readPage({ url: "https://aistudio.google.com/prompts/new_chat", bootstraps: [bootstrap({
    WIu0Nc: "synthetic-marker", oPEP7c: "owner@example.com"
  })] });
  assert.equal(withoutIndex.email, "owner@example.com");
  assert.equal(withoutIndex.authUser, null);
});

test("bootstrap accepts explicit account fields and rejects conflicting identity sources", () => {
  for (const field of ["authuser", "currentpageaccount"]) {
    const result = readPage({ url: "https://aistudio.google.com/prompts/new_chat", bootstraps: [bootstrap({
      WIu0Nc: "synthetic-marker", oPEP7c: "owner@example.com", [field]: 3
    })] });
    assert.equal(result.authUser, "3");
  }
  const first = bootstrap({ WIu0Nc: "synthetic-marker", oPEP7c: "owner@example.com", authuser: "5" });
  const other = bootstrap({ WIu0Nc: "synthetic-marker", oPEP7c: "other@example.com", authuser: "5" });
  assert.equal(readPage({ bootstraps: [first, other] }).error, "identity_conflict");
  assert.equal(readPage({ wiz: { oPEP7c: "other@example.com" }, bootstraps: [first] }).error, "identity_conflict");
  assert.equal(readPage({ nodes: [avatar("Google Account: other@example.com")], bootstraps: [first] }).error, "identity_conflict");
  assert.equal(readPage({ url: "https://aistudio.google.com/?authuser=2", bootstraps: [first] }).error, "identity_conflict");
  assert.equal(readPage({ bootstraps: [bootstrap({ WIu0Nc: "synthetic-marker", oPEP7c: "owner@example.com", authuser: "5", currentpageaccount: "2" })] }).error, "identity_conflict");
});

test("bootstrap ignores nonmatching script shapes and never evaluates executable text or nested identities", () => {
  const values = [null, [], 5, "throw new Error('must not execute')", { oPEP7c: "other@example.com" },
    { WIu0Nc: "synthetic-marker" }, { WIu0Nc: 1, oPEP7c: "other@example.com" },
    { WIu0Nc: {}, oPEP7c: "other@example.com" }, { WIu0Nc: "synthetic-marker", oPEP7c: ["other@example.com"] },
    { WIu0Nc: "synthetic-marker", oPEP7c: "not-an-email" },
    { accounts: [{ WIu0Nc: "synthetic-marker", oPEP7c: "other@example.com" }] }];
  for (const value of values) {
    const result = readPage({ bootstraps: [bootstrap(value)] });
    assert.equal(result.email, null);
    assert.equal(result.authUser, "5");
    assert.equal(result.error, undefined);
  }
});

test("bootstrap limits script count and individual or combined size without parsing a partial selection", () => {
  const value = { WIu0Nc: "synthetic-marker", oPEP7c: "owner@example.com" };
  const eight = Array.from({ length: 8 }, (_, index) => bootstrap(index === 7 ? value : {}, "id-" + index));
  assert.equal(readPage({ bootstraps: eight }).email, "owner@example.com");
  const mustNotRead = { get textContent() { throw new Error("Count limit must be checked before reading scripts"); } };
  assert.equal(readPage({ bootstraps: Array(9).fill(mustNotRead) }).error, "inspection_unavailable");
  const oversized = bootstrap({ ...value, ignored: "x".repeat(65536) });
  assert.equal(readPage({ bootstraps: [oversized] }).error, "inspection_unavailable");
  assert.equal(readPage({ bootstraps: [bootstrap(value), oversized] }).error, "inspection_unavailable");
  const withinSingleLimit = bootstrap({ ignored: "x".repeat(50000) });
  assert.equal(readPage({ bootstraps: [withinSingleLimit, withinSingleLimit, withinSingleLimit] }).error, "inspection_unavailable");
});

test("automatically recognizes u/1, u/2, u/3 and current-page email metadata", () => {
  for (const index of ["1", "2", "3"]) {
    const result = readPage({ url: "https://aistudio.google.com/u/" + index + "/prompts/new_chat", wiz: { oPEP7c: "owner" + index + "@example.com", currentpageaccount: index } });
    assert.equal(result.authUser, index);
    assert.equal(result.email, "owner" + index + "@example.com");
    assert.equal(result.error, undefined);
  }
  const query = readPage({ url: "https://aistudio.google.com/prompts/new_chat?authuser=2", wiz: { oPEP7c: "owner2@example.com" } });
  assert.equal(query.authUser, "2");
});

test("uses current-account avatar labels but ignores arbitrary prompt email and hidden account entries", () => {
  const result = readPage({ nodes: [
    avatar("Google 账号：用户名 (owner@example.com)"),
    avatar("Email another@example.com"),
    avatar("Google Account: Hidden hidden@example.com", null, false)
  ] });
  assert.equal(result.email, "owner@example.com");
  const signOut = readPage({ nodes: [avatar("Owner owner@example.com", "https://accounts.google.com/SignOutOptions?authuser=5")] });
  assert.equal(signOut.email, "owner@example.com");
});

test("does not guess default account when metadata is missing and rejects conflicting identity indicators", () => {
  const blank = readPage({ url: "https://aistudio.google.com/prompts/new_chat" });
  assert.equal(blank.authUser, null);
  assert.equal(blank.email, null);
  assert.equal(readPage({ wiz: { authuser: "0" } }).error, "identity_conflict");
  assert.equal(readPage({ wiz: { oPEP7c: "owner@example.com" }, nodes: [avatar("Google Account: other@example.com")] }).error, "identity_conflict");
  assert.equal(readPage({ url: "https://aistudio.google.com/?authuser=5&authuser=5" }).error, "identity_conflict");
  assert.equal(readPage({ url: "https://example.com/?authuser=5" }).error, "wrong_tab");
});

test("rejects malformed u/ account paths before inspection or export without an unhandled ReferenceError", () => {
  for (const pathname of ["/u/", "/u//", "/u//prompts/new_chat"]) {
    const url = "https://aistudio.google.com" + pathname;
    const tab = { id: 1, active: true, url };
    for (const selected of [undefined, "0", "5"]) {
      assert.throws(() => session.checkTab(tab, selected), error => error.name === "Error" && error.message === "account_mismatch");
    }
    assert.equal(readPage({ url }).error, "identity_conflict");
  }
});

function popup({ mutate, confirmed = true, page = {}, onCookies } = {}) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, { value: "", checked: false, disabled: false, textContent: "", addEventListener(event, callback) { this[event] = callback; this.callback = callback; } });
    return elements.get(id);
  };
  let tab = { id: 7, active: true, url: page.url || "https://aistudio.google.com/prompts/new_chat?authuser=5" };
  const livePage = { wiz: { oPEP7c: "owner@example.com", currentpageaccount: "5" }, documentId: "synthetic-document", ...page };
  const queries = [], downloads = [], scripts = [];
  const blobs = new Map();
  class LocalURL extends URL {}
  LocalURL.createObjectURL = blob => { blobs.set("blob:local-only", blob); return "blob:local-only"; };
  LocalURL.revokeObjectURL = url => blobs.delete(url);
  const context = vm.createContext({
    AIStudioSession: session, document: { getElementById: get }, URL: LocalURL, Blob, setTimeout() {},
    chrome: {
      tabs: { async query() { return [{ ...tab }]; } },
      cookies: {
        async getAllCookieStores() { return [{ id: "separate-store", tabIds: [7] }, { id: "other-store", tabIds: [2] }]; },
        async getAll(query) { queries.push(query); if (mutate) tab = mutate(tab); if (onCookies) onCookies(livePage); return fixture(); }
      },
      scripting: { async executeScript(options) {
        scripts.push(options);
        if (options.target.tabId !== tab.id || options.target.documentIds && !options.target.documentIds.includes(livePage.documentId)) throw new Error("Synthetic document changed");
        return [{ documentId: livePage.documentId, frameId: 0, result: readPage({ ...livePage, url: tab.url }) }];
      } },
      downloads: { async download(options) {
        downloads.push({ ...options, state: JSON.parse(await blobs.get(options.url).text()) });
        return 1;
      } }
    }
  });
  vm.runInContext(fs.readFileSync(path.join(root, "popup.js"), "utf8"), context);
  const ready = vm.runInContext("inspectionReady", context);
  return {
    get, queries, downloads, scripts, livePage, ready,
    async click() { await ready; get("confirmed").checked = confirmed; await get("export").callback(); },
    async refresh() { await get("inspect").callback(); }
  };
}

test("popup does not read credentials until confirmed user click and downloads only locally", async () => {
  const view = popup();
  assert.equal(view.queries.length, 0);
  await view.ready;
  assert.equal(view.get("email").value, "owner@example.com");
  assert.equal(view.get("account").value, "5");
  assert.equal(view.get("confirmed").checked, false);
  await view.click();
  assert.equal(view.queries.length, 1);
  assert.equal(view.queries[0].storeId, "separate-store");
  assert.equal(view.queries[0].url, "https://aistudio.google.com/");
  assert.equal(view.downloads[0].url, "blob:local-only");
  assert.equal(view.downloads[0].saveAs, true);
  assert.equal(view.downloads[0].state.aistudio2api.source.auth_user, "5");
  assert.equal(view.get("export").disabled, false);
  assert.equal(view.scripts.length, 3);
  assert.ok(view.scripts.every(script => script.target.tabId === 7 && script.world === "MAIN"));
  assert.deepEqual(view.scripts[1].target.documentIds[0], "synthetic-document");
  const unconfirmed = popup({ confirmed: false });
  await unconfirmed.click();
  assert.equal(unconfirmed.queries.length, 0);
});

test("popup uses bootstrap-only email and rejects a bootstrap identity change during export", async () => {
  const metadata = { WIu0Nc: "synthetic-bootstrap-marker", oPEP7c: "bootstrap@example.com" };
  const view = popup({ page: { wiz: {}, bootstraps: [bootstrap(metadata)] } });
  await view.ready;
  assert.equal(view.get("email").value, "bootstrap@example.com");
  assert.equal(view.get("account").value, "5");
  assert.equal(view.queries.length, 0);
  await view.click();
  assert.equal(view.downloads.length, 1);
  assert.equal(view.downloads[0].state.aistudio2api.source.email, "bootstrap@example.com");
  assert.equal(view.downloads[0].state.aistudio2api.source.auth_user, "5");
  assert.doesNotMatch(JSON.stringify(view.downloads), /synthetic-bootstrap-marker|WIu0Nc/);
  const switched = popup({
    page: { wiz: {}, bootstraps: [bootstrap(metadata)] },
    onCookies: page => { page.bootstraps = [bootstrap({ ...metadata, oPEP7c: "other@example.com" })]; }
  });
  await switched.click();
  assert.equal(switched.queries.length, 1);
  assert.equal(switched.downloads.length, 0);
  assert.match(switched.get("status").textContent, /已变化/);
});

test("popup stops if the active tab changes while credentials are being read", async () => {
  const view = popup({ mutate: tab => ({ ...tab, id: 9 }) });
  await view.click();
  assert.equal(view.downloads.length, 0);
  assert.match(view.get("status").textContent, /已变化/);
  assert.equal(view.get("export").disabled, false);
});

test("manual entry remains available when metadata is absent, without silently selecting account zero", async () => {
  const view = popup({ page: { url: "https://aistudio.google.com/prompts/new_chat", wiz: {} } });
  await view.ready;
  assert.equal(view.get("email").value, "");
  assert.equal(view.get("account").value, "");
  assert.match(view.get("inspection").textContent, /手动/);
  view.get("email").value = "manual@example.com";
  view.get("account").value = "2";
  await view.click();
  assert.equal(view.downloads[0].state.aistudio2api.source.email, "manual@example.com");
  assert.equal(view.downloads[0].state.aistudio2api.source.auth_user, "2");
});

test("rejects modified email and same-URL account changes before or during export", async () => {
  const altered = popup();
  await altered.ready;
  altered.get("email").value = "other@example.com";
  await altered.click();
  assert.equal(altered.queries.length, 0);
  assert.equal(altered.downloads.length, 0);
  assert.match(altered.get("status").textContent, /邮箱/);
  const switched = popup();
  await switched.ready;
  switched.livePage.wiz.oPEP7c = "other@example.com";
  await switched.click();
  assert.equal(switched.queries.length, 0);
  assert.match(switched.get("status").textContent, /已变化/);
  const during = popup({ onCookies: page => { page.wiz.oPEP7c = "other@example.com"; } });
  await during.click();
  assert.equal(during.queries.length, 1);
  assert.equal(during.downloads.length, 0);
  const replacedDocument = popup({ onCookies: page => { page.documentId = "new-document"; } });
  await replacedDocument.click();
  assert.equal(replacedDocument.downloads.length, 0);
  assert.match(replacedDocument.get("status").textContent, /已变化/);
});

test("refresh clears previous identity instead of leaving stale autofilled account values", async () => {
  const view = popup();
  await view.ready;
  view.livePage.wiz = {};
  await view.refresh();
  assert.equal(view.get("email").value, "");
  assert.equal(view.get("account").value, "5");
  assert.equal(view.get("confirmed").checked, false);
  assert.match(view.get("inspection").textContent, /未提供可靠邮箱/);
});

test("extension has no background worker, persistent content script, debugger, storage, or network access", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json")));
  assert.equal(manifest.background, undefined);
  assert.equal(manifest.content_scripts, undefined);
  assert.deepEqual(manifest.permissions, ["activeTab", "scripting", "cookies", "downloads"]);
  assert.match(manifest.content_security_policy.extension_pages, /connect-src 'none'/);
  for (const file of ["popup.js", "session.js"]) assert.doesNotMatch(fs.readFileSync(path.join(root, file), "utf8"), /\bfetch\s*\(|XMLHttpRequest|WebSocket|chrome\.storage|console\./);
});
