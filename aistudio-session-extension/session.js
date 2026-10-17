"use strict";

(function (scope) {
  const REQUIRED = ["SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"];
  const DOMAINS = new Set(["google.com", ".google.com", "aistudio.google.com", ".aistudio.google.com"]);
  const COOKIE_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$(?![\s\S])/;
  const COOKIE_VALUE = /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+$(?![\s\S])/;
  const EMAIL = /^[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

  function identity(email, authUser) {
    const normalizedEmail = String(email).trim().toLowerCase();
    if (normalizedEmail.length > 254 || !EMAIL.test(normalizedEmail) || normalizedEmail.split("@")[0].includes("..") || normalizedEmail.split("@")[0].endsWith(".")) throw new Error("invalid_identity");
    if (typeof authUser !== "string" || !/^(?:0|[1-9][0-9]{0,9})$(?![\s\S])/.test(authUser)) throw new Error("invalid_identity");
    return { email: normalizedEmail, authUser };
  }

  function checkTab(tab, authUser) {
    let url;
    try { url = new URL(tab.url); } catch { throw new Error("wrong_tab"); }
    if (!Number.isInteger(tab.id) || !tab.active || url.origin !== "https://aistudio.google.com" || url.username || url.password) throw new Error("wrong_tab");
    if (tab.status === "loading" || tab.pendingUrl) throw new Error("changed_tab");
    const query = url.searchParams.getAll("authuser");
    const route = url.pathname.match(/^\/u\/([^/]+)(?:\/|$)/);
    if (url.pathname.startsWith("/u/") && !route) throw new Error("account_mismatch");
    if (authUser !== undefined && ((query.length && (query.length !== 1 || query[0] !== authUser)) || (route && route[1] !== authUser))) throw new Error("account_mismatch");
    return tab;
  }

  // This self-contained function runs only in the current AI Studio top frame.
  // Return identity metadata only: never cookies, tokens, page text, or full globals.
  function readPageIdentity() {
    const url = new URL(location.href);
    if (url.origin !== "https://aistudio.google.com" || url.username || url.password) return { error: "wrong_tab" };
    const indices = new Set(), emails = new Set(), sources = new Set();
    let conflict = false;
    const validIndex = /^(?:0|[1-9][0-9]{0,9})$(?![\s\S])/;
    const validEmail = /^[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$(?![\s\S])/;
    function addIndex(raw, source) {
      if (raw === undefined || raw === null || raw === "") return;
      const value = typeof raw === "number" && Number.isSafeInteger(raw) ? String(raw) : raw;
      if (typeof value !== "string" || !validIndex.test(value)) { conflict = true; return; }
      indices.add(value); sources.add(source);
    }
    function addEmail(raw, source) {
      if (typeof raw !== "string" || !raw) return;
      const value = raw.trim().toLowerCase();
      if (value.length > 254 || !validEmail.test(value) || value.split("@")[0].includes("..") || value.split("@")[0].endsWith(".")) return;
      emails.add(value); sources.add(source);
    }
    const route = url.pathname.match(/^\/u\/([^/]+)(?:\/|$)/);
    if (url.pathname.startsWith("/u/") && !route) conflict = true;
    if (route) addIndex(route[1], "url");
    const query = url.searchParams.getAll("authuser");
    if (query.length > 1) conflict = true;
    query.forEach(value => addIndex(value, "url"));
    const wiz = globalThis.WIZ_global_data;
    if (wiz && typeof wiz === "object") {
      addEmail(wiz.oPEP7c, "page-metadata");
      addIndex(wiz.currentpageaccount, "page-metadata");
      addIndex(wiz.authuser, "page-metadata");
    }
    addIndex(globalThis.currentpageaccount, "page-metadata");
    addIndex(document.querySelector('meta[name="google.authuser"]')?.content, "page-metadata");
    addEmail(document.querySelector('meta[name="google.email"]')?.content, "page-metadata");
    // Current AI Studio also places its bootstrap object in an inert JSON
    // script whose ID changes on each load. Inspect the bounded shape, not an
    // ID or executable script. Other bootstrap fields stay in this page world.
    const bootstrapScripts = document.querySelectorAll('script[type="application/json"]');
    const maxBootstrapScripts = 8, maxBootstrapChars = 65536, maxTotalBootstrapChars = 131072;
    if (bootstrapScripts.length > maxBootstrapScripts) return { url: url.href, error: "inspection_unavailable" };
    let totalBootstrapChars = 0;
    for (const script of bootstrapScripts) {
      const text = script.textContent;
      if (typeof text !== "string") continue;
      totalBootstrapChars += text.length;
      if (text.length > maxBootstrapChars || totalBootstrapChars > maxTotalBootstrapChars) {
        return { url: url.href, error: "inspection_unavailable" };
      }
      let bootstrap;
      try { bootstrap = JSON.parse(text); } catch { continue; }
      if (!bootstrap || typeof bootstrap !== "object" || Array.isArray(bootstrap) ||
          !Object.prototype.hasOwnProperty.call(bootstrap, "WIu0Nc") ||
          !Object.prototype.hasOwnProperty.call(bootstrap, "oPEP7c") ||
          typeof bootstrap.WIu0Nc !== "string" || typeof bootstrap.oPEP7c !== "string") continue;
      // WIu0Nc is a shape marker only. Never return, log, or store its value.
      const bootstrapEmail = bootstrap.oPEP7c.trim().toLowerCase();
      if (bootstrapEmail.length > 254 || !validEmail.test(bootstrapEmail) ||
          bootstrapEmail.split("@")[0].includes("..") || bootstrapEmail.split("@")[0].endsWith(".")) continue;
      addEmail(bootstrapEmail, "bootstrap-json");
      if (Object.prototype.hasOwnProperty.call(bootstrap, "currentpageaccount")) addIndex(bootstrap.currentpageaccount, "bootstrap-json");
      if (Object.prototype.hasOwnProperty.call(bootstrap, "authuser")) addIndex(bootstrap.authuser, "bootstrap-json");
    }
    // A current-account avatar label is narrower than body text or an account list.
    // Do not inspect account-switcher menus, [data-email] lists, prompts, or history.
    const nodes = document.querySelectorAll('a[href*="accounts.google.com/SignOutOptions"], button[aria-label], a[aria-label]');
    const emailPattern = /[^\s()[\]<>"',;：:]+@[^\s()[\]<>"',;：:]+/g;
    for (const node of Array.from(nodes).slice(0, 256)) {
      const box = node.getBoundingClientRect();
      if (!box.width || !box.height || node.getAttribute("aria-hidden") === "true") continue;
      if (typeof getComputedStyle === "function") {
        const style = getComputedStyle(node);
        if (style.visibility !== "visible" || style.display === "none" || style.opacity === "0") continue;
      }
      const label = node.getAttribute("aria-label") || node.getAttribute("title") || "";
      if (!label || label.length > 1024) continue;
      let avatar = /^(?:Google (?:Account|账号|帐号|帳戶|帐户|帳號)|Google账户|Google-Konto|Compte Google)\s*[:：]/i.test(label);
      const href = node.getAttribute("href");
      if (href) {
        try {
          const accountURL = new URL(href, location.href);
          avatar ||= accountURL.protocol === "https:" && accountURL.hostname === "accounts.google.com" && accountURL.pathname === "/SignOutOptions";
        } catch { /* A malformed link is not identity evidence. */ }
      }
      if (!avatar) continue;
      const found = (label.match(emailPattern) || []).filter(value => validEmail.test(value.toLowerCase()));
      if (new Set(found.map(value => value.toLowerCase())).size > 1) { conflict = true; continue; }
      if (found.length) addEmail(found[0], "account-avatar");
    }
    if (conflict || indices.size > 1 || emails.size > 1) return { url: url.href, error: "identity_conflict" };
    return { url: url.href, email: [...emails][0] || null, authUser: [...indices][0] || null, sources: [...sources] };
  }

  function makeState(rawCookies, email, authUser, now = Date.now() / 1000) {
    const selected = identity(email, authUser);
    if (!Array.isArray(rawCookies) || rawCookies.length > 512) throw new Error("invalid_cookies");
    const cookies = [];
    const seen = new Set();
    for (const raw of rawCookies) {
      if (!DOMAINS.has(raw.domain) || raw.partitionKey) continue;
      const expires = raw.session ? -1 : raw.expirationDate;
      if (expires !== -1 && (!Number.isFinite(expires) || expires <= now || expires > 253402300799)) continue;
      if (!COOKIE_NAME.test(raw.name) || typeof raw.value !== "string" || raw.value.length > 16384 || !COOKIE_VALUE.test(raw.value)) throw new Error("invalid_cookies");
      if (typeof raw.path !== "string" || raw.path.length > 2048 || !/^\/[^\x00-\x20\x7f;]*$(?![\s\S])/.test(raw.path)) throw new Error("invalid_cookies");
      if (typeof raw.httpOnly !== "boolean" || typeof raw.secure !== "boolean") throw new Error("invalid_cookies");
      if ((raw.name.startsWith("__Secure-") || raw.name.startsWith("__Host-")) && !raw.secure) throw new Error("invalid_cookies");
      if (raw.name.startsWith("__Host-") && (raw.path !== "/" || raw.domain.startsWith("."))) throw new Error("invalid_cookies");
      const sameSite = { no_restriction: "None", lax: "Lax", strict: "Strict", unspecified: "" }[raw.sameSite];
      if (sameSite === undefined) throw new Error("invalid_cookies");
      const key = JSON.stringify([raw.name, raw.domain, raw.path]);
      if (seen.has(key)) throw new Error("invalid_cookies");
      seen.add(key);
      cookies.push({ name: raw.name, value: raw.value, domain: raw.domain, path: raw.path, expires, httpOnly: raw.httpOnly, secure: raw.secure, sameSite });
    }
    if (!REQUIRED.every(name => cookies.some(cookie => cookie.name === name && cookie.domain === ".google.com" && cookie.path === "/"))) throw new Error("missing_cookies");
    return { cookies, origins: [], aistudio2api: { source: { browser: "mixer-import", email: selected.email, auth_user: selected.authUser } } };
  }

  const api = { identity, checkTab, makeState, readPageIdentity };
  if (typeof module === "object" && module.exports) module.exports = api;
  else scope.AIStudioSession = Object.freeze(api);
})(globalThis);
