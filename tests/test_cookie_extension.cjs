"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const popupSource = fs.readFileSync(
  path.join(__dirname, "../gemini-cookie-sync-extension/popup.js"),
  "utf8"
);

// Every session value below is synthetic; the harness never opens a browser.
function makeCookies(storeId = "normal", marker = "synthetic-normal") {
  return ["SAPISID", "SID"].map((name) => ({
    name,
    value: `${marker}-${name}`,
    domain: ".google.com",
    path: "/",
    secure: true,
    httpOnly: true,
    session: false,
    storeId
  }));
}

function createPopup({
  tabs = [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/u/2/app" }],
  stores = [{ id: "normal", tabIds: tabs.map((tab) => tab.id) }],
  cookiesByStore = { normal: makeCookies() },
  pageByTab = {},
  onCookies,
  onScript
} = {}) {
  const liveTabs = tabs.map((tab) => ({ ...tab }));
  const elements = new Map();
  const downloads = [];
  const cookieQueries = [];
  const scriptTabs = [];
  const blobUrls = new Map();
  let blobCounter = 0;

  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        textContent: "",
        className: "",
        disabled: false,
        value: "",
        innerHTML: "",
        hidden: false,
        options: [],
        listeners: {},
        addEventListener(event, handler) { this.listeners[event] = handler; },
        appendChild(child) { this.options.push(child); },
        replaceChildren(...children) { this.options = children; },
        add(child) { this.options.push(child); }
      });
    }
    return elements.get(id);
  }

  class BrowserURL extends URL {}
  BrowserURL.createObjectURL = (blob) => {
    const url = `blob:synthetic-${++blobCounter}`;
    blobUrls.set(url, blob);
    return url;
  };
  BrowserURL.revokeObjectURL = (url) => blobUrls.delete(url);

  const chrome = {
    tabs: {
      async query(query = {}) {
        return liveTabs.filter((tab) => {
          if (query.url && !tab.url?.startsWith("https://gemini.google.com/")) return false;
          if (query.active && !tab.active) return false;
          if ((query.currentWindow || query.lastFocusedWindow) && tab.windowId !== 1) return false;
          if (query.windowId !== undefined && tab.windowId !== query.windowId) return false;
          return true;
        }).map((tab) => ({ ...tab }));
      },
      async get(tabId) {
        const tab = liveTabs.find((entry) => entry.id === tabId);
        if (!tab) throw new Error("Synthetic tab was closed");
        return { ...tab };
      },
      async create() {}
    },
    windows: {
      async getLastFocused() { return { id: 1, focused: true }; },
      async getCurrent() { return { id: 1, focused: true }; }
    },
    cookies: {
      async getAllCookieStores() {
        return stores.map((store) => ({ ...store, tabIds: [...store.tabIds] }));
      },
      async getAll(query) {
        cookieQueries.push({ ...query });
        if (onCookies) await onCookies({ query, liveTabs, livePages: pageByTab });
        return (cookiesByStore[query.storeId] || []).map((cookie) => ({ ...cookie }));
      }
    },
    scripting: {
      async executeScript(details) {
        scriptTabs.push(details.target.tabId);
        if (onScript) await onScript({ details, liveTabs });
        const tab = liveTabs.find((entry) => entry.id === details.target.tabId);
        const page = pageByTab[tab.id] || {};
        const documentId = page.documentId || `synthetic-document-tab-${tab.id}`;
        if (details.target.documentIds && !details.target.documentIds.includes(documentId)) {
          throw new Error("Synthetic document was replaced");
        }
        const pageUrl = page.url || tab.url;
        const pageContext = vm.createContext({
          URL,
          location: new URL(pageUrl),
          document: {
            documentElement: { innerHTML: page.html || "" },
            querySelector(selector) {
              return selector === 'meta[name="google.authuser"]' && page.metaAccount !== undefined
                ? { content: page.metaAccount }
                : null;
            }
          },
          WIZ_global_data: {
            SNlM0e: `synthetic-xsrf-tab-${tab.id}`,
            cfb2h: `synthetic-build-tab-${tab.id}`,
            ...(page.wiz || {})
          },
          performance: { getEntriesByType() { return []; } }
        });
        const result = vm.runInContext(`(${details.func.toString()})()`, pageContext);
        return [{ documentId, result: { ...result, ...(page.result || {}) } }];
      }
    },
    downloads: {
      async download(details) {
        const blob = blobUrls.get(details.url);
        assert.ok(blob, "Download must use the generated JSON blob");
        downloads.push({ ...details, payload: JSON.parse(await blob.text()) });
        return downloads.length;
      }
    },
    runtime: { lastError: null }
  };

  const context = vm.createContext({
    chrome,
    document: {
      getElementById: element,
      createElement(tag) { return { tagName: tag.toUpperCase(), value: "", textContent: "" }; }
    },
    URL: BrowserURL,
    Blob,
    console,
    setTimeout() { return 1; },
    clearTimeout() {}
  });
  vm.runInContext(popupSource, context, { filename: "popup.js" });

  return {
    downloads,
    cookieQueries,
    scriptTabs,
    elements,
    async export() {
      const handler = element("export").listeners.click;
      assert.equal(typeof handler, "function", "Export button must have a click handler");
      await handler();
      assert.equal(element("export").disabled, false, "Export button must recover after completion");
      return downloads.at(-1)?.payload;
    },
    status() { return element("status").textContent; }
  };
}

test("exports the active Gemini account even if an inactive default-account tab comes first", async () => {
  const popup = createPopup({
    tabs: [
      { id: 1, active: false, windowId: 1, url: "https://gemini.google.com/app" },
      { id: 2, active: true, windowId: 1, url: "https://gemini.google.com/u/2/app" }
    ]
  });
  const payload = await popup.export();
  assert.ok(payload, popup.status());
  assert.equal(payload.auth_user, "2");
  assert.equal(payload.xsrf_token, "synthetic-xsrf-tab-2");
  assert.ok(popup.scriptTabs.every((id) => id === 2));
});

test("recognizes an explicit authuser query parameter", async () => {
  const popup = createPopup({
    tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/app?authuser=2" }]
  });
  const payload = await popup.export();
  assert.ok(payload, popup.status());
  assert.equal(payload.auth_user, "2");
});

test("uses the current page account metadata when the default route has no account index", async () => {
  const popup = createPopup({
    tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/app" }],
    pageByTab: { 2: { wiz: { currentpageaccount: "2" } } }
  });
  const payload = await popup.export();
  assert.ok(payload, popup.status());
  assert.equal(payload.auth_user, "2");
});

test("queries cookies only from the selected tab's cookie store", async () => {
  const popup = createPopup({
    stores: [
      { id: "other", tabIds: [99] },
      { id: "normal", tabIds: [2] }
    ],
    cookiesByStore: {
      other: makeCookies("other", "synthetic-other-account"),
      normal: makeCookies()
    }
  });
  const payload = await popup.export();
  assert.ok(payload, popup.status());
  assert.equal(payload.sapisid, "synthetic-normal-SAPISID");
  assert.ok(popup.cookieQueries.length > 0);
  assert.deepEqual([...new Set(popup.cookieQueries.map((query) => query.storeId))], ["normal"]);
  assert.ok(popup.cookieQueries.every((query) => query.url === "https://gemini.google.com/u/2/app"));
});

test("shared Google cookies can export two different selected account indices", async () => {
  const first = createPopup({
    tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/u/1/app" }]
  });
  const second = createPopup({
    tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/u/2/app" }]
  });
  const firstPayload = await first.export();
  const secondPayload = await second.export();
  assert.ok(firstPayload, first.status());
  assert.ok(secondPayload, second.status());
  assert.equal(firstPayload.cookie, secondPayload.cookie);
  assert.equal(firstPayload.auth_user, "1");
  assert.equal(secondPayload.auth_user, "2");
});

test("does not silently export the first Gemini tab when no Gemini tab is active and selection is ambiguous", async () => {
  const popup = createPopup({
    tabs: [
      { id: 1, active: false, windowId: 1, url: "https://gemini.google.com/u/1/app" },
      { id: 2, active: false, windowId: 1, url: "https://gemini.google.com/u/2/app" },
      { id: 3, active: true, windowId: 1, url: "https://example.test/" }
    ]
  });
  await popup.export();
  assert.equal(popup.downloads.length, 0, "Ambiguous account selection must not create a session file");
  assert.match(popup.status(), /active|select|multiple|choose|Gemini/i);
});

test("aborts export if the tab switches accounts while session data is collected", async () => {
  const popup = createPopup({
    onCookies({ liveTabs }) {
      liveTabs.find((tab) => tab.id === 2).url = "https://gemini.google.com/u/0/app";
    }
  });
  await popup.export();
  assert.equal(popup.downloads.length, 0, "Navigation must not combine old account selection with new page credentials");
  assert.match(popup.status(), /chang|navigat|refresh|retry|again|account/i);
});

test("does not export an active Gemini tab from a different browser window", async () => {
  const popup = createPopup({
    tabs: [
      { id: 1, active: false, windowId: 1, url: "https://gemini.google.com/u/1/app" },
      { id: 2, active: true, windowId: 2, url: "https://gemini.google.com/u/2/app" },
      { id: 3, active: true, windowId: 1, url: "https://example.test/" }
    ]
  });
  await popup.export();
  assert.equal(popup.downloads.length, 0, "A different window's active tab must not supply the exported session");
  assert.match(popup.status(), /active|select|Gemini|window/i);
});

test("exports an explicit zero account index without dropping it as a falsy value", async () => {
  const popup = createPopup({
    tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/u/0/app?authuser=0" }],
    pageByTab: { 2: { wiz: { currentpageaccount: 0 } } }
  });
  const payload = await popup.export();
  assert.ok(payload, popup.status());
  assert.equal(payload.auth_user, "0");
  assert.equal(popup.downloads[0].filename, "gemini-auth-u0.json");
});

test("uses account zero for the default route when the page has no explicit account index", async () => {
  const popup = createPopup({
    tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/app" }]
  });
  const payload = await popup.export();
  assert.ok(payload, popup.status());
  assert.equal(payload.auth_user, "0");
});

test("rejects conflicting account indices in path and query", async () => {
  const popup = createPopup({
    tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/u/2/app?authuser=1" }]
  });
  await popup.export();
  assert.equal(popup.downloads.length, 0);
  assert.equal(popup.cookieQueries.length, 0, "Conflicting page selection must fail before reading cookies");
  assert.match(popup.status(), /account|disagree|chang|refresh/i);
});

test("fails when the selected tab has no cookie store instead of using another store", async () => {
  const popup = createPopup({
    stores: [{ id: "other", tabIds: [99] }],
    cookiesByStore: { other: makeCookies("other", "synthetic-other-account") }
  });
  await popup.export();
  assert.equal(popup.downloads.length, 0);
  assert.equal(popup.cookieQueries.length, 0);
  assert.match(popup.status(), /store|refresh|try again/i);
});

test("does not fall back to another cookie store when the selected store cannot be read", async () => {
  const popup = createPopup({
    stores: [{ id: "normal", tabIds: [2] }, { id: "other", tabIds: [99] }],
    cookiesByStore: { other: makeCookies("other", "synthetic-other-account") },
    onCookies({ query }) {
      if (query.storeId === "normal") throw new Error("Synthetic selected cookie store read failed");
    }
  });
  await popup.export();
  assert.equal(popup.downloads.length, 0);
  assert.deepEqual(popup.cookieQueries.map((query) => query.storeId), ["normal"]);
  assert.match(popup.status(), /store|failed|again/i);
});

for (const navigationState of [{ status: "loading" }, { pendingUrl: "https://gemini.google.com/u/1/app" }]) {
  const stateName = navigationState.status ? "loading" : "pending navigation";
  test(`blocks export while the selected Gemini tab has ${stateName}`, async () => {
    const popup = createPopup({
      tabs: [{ id: 2, active: true, windowId: 1, url: "https://gemini.google.com/u/2/app", ...navigationState }]
    });
    await popup.export();
    assert.equal(popup.downloads.length, 0);
    assert.equal(popup.cookieQueries.length, 0);
    assert.match(popup.status(), /chang|load|wait|again/i);
  });
}

test("aborts a same-URL document reload during cookie collection", async () => {
  const popup = createPopup({
    onCookies({ livePages }) {
      livePages[2] = { documentId: "synthetic-reloaded-document" };
    }
  });
  await popup.export();
  assert.equal(popup.downloads.length, 0, "A reloaded document must not reuse the old document's session metadata");
  assert.match(popup.status(), /chang|refresh|again|session/i);
});
