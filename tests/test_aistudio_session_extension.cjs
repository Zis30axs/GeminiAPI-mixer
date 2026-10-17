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

function popup({ mutate, confirmed = true } = {}) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, { value: "", checked: false, disabled: false, textContent: "", addEventListener(_, callback) { this.callback = callback; } });
    return elements.get(id);
  };
  get("email").value = "owner@example.com";
  get("account").value = "5";
  get("confirmed").checked = confirmed;
  let tab = { id: 7, active: true, url: "https://aistudio.google.com/prompts/new_chat?authuser=5" };
  const queries = [], downloads = [];
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
        async getAll(query) { queries.push(query); if (mutate) tab = mutate(tab); return fixture(); }
      },
      downloads: { async download(options) {
        downloads.push({ ...options, state: JSON.parse(await blobs.get(options.url).text()) });
        return 1;
      } }
    }
  });
  vm.runInContext(fs.readFileSync(path.join(root, "popup.js"), "utf8"), context);
  return { get, queries, downloads, async click() { await get("export").callback(); } };
}

test("popup does not read credentials until confirmed user click and downloads only locally", async () => {
  const view = popup();
  assert.equal(view.queries.length, 0);
  await view.click();
  assert.equal(view.queries.length, 1);
  assert.equal(view.queries[0].storeId, "separate-store");
  assert.equal(view.queries[0].url, "https://aistudio.google.com/");
  assert.equal(view.downloads[0].url, "blob:local-only");
  assert.equal(view.downloads[0].saveAs, true);
  assert.equal(view.downloads[0].state.aistudio2api.source.auth_user, "5");
  assert.equal(view.get("export").disabled, false);
  const unconfirmed = popup({ confirmed: false });
  await unconfirmed.click();
  assert.equal(unconfirmed.queries.length, 0);
});

test("popup stops if the active tab changes while credentials are being read", async () => {
  const view = popup({ mutate: tab => ({ ...tab, id: 9 }) });
  await view.click();
  assert.equal(view.downloads.length, 0);
  assert.match(view.get("status").textContent, /发生变化/);
  assert.equal(view.get("export").disabled, false);
});

test("extension has no background worker, content script, debugger, storage, or network access", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json")));
  assert.equal(manifest.background, undefined);
  assert.equal(manifest.content_scripts, undefined);
  assert.deepEqual(manifest.permissions, ["activeTab", "cookies", "downloads"]);
  assert.match(manifest.content_security_policy.extension_pages, /connect-src 'none'/);
  for (const file of ["popup.js", "session.js"]) assert.doesNotMatch(fs.readFileSync(path.join(root, file), "utf8"), /\bfetch\s*\(|XMLHttpRequest|WebSocket|chrome\.storage|console\./);
});
