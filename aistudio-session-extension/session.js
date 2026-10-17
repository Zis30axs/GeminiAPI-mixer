"use strict";

(function (scope) {
  const REQUIRED = ["SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"];
  const DOMAINS = new Set(["google.com", ".google.com", "aistudio.google.com", ".aistudio.google.com"]);
  const COOKIE_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$(?![\s\S])/;
  const COOKIE_VALUE = /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+$(?![\s\S])/;
  const EMAIL = /^[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

  function identity(email, authUser) {
    const normalizedEmail = String(email).trim().toLowerCase();
    if (normalizedEmail.length > 254 || !EMAIL.test(normalizedEmail)) throw new Error("invalid_identity");
    if (typeof authUser !== "string" || !/^(?:0|[1-9][0-9]{0,9})$(?![\s\S])/.test(authUser)) throw new Error("invalid_identity");
    return { email: normalizedEmail, authUser };
  }

  function checkTab(tab, authUser) {
    let url;
    try { url = new URL(tab.url); } catch { throw new Error("wrong_tab"); }
    if (!Number.isInteger(tab.id) || !tab.active || url.origin !== "https://aistudio.google.com" || url.username || url.password) throw new Error("wrong_tab");
    const query = url.searchParams.getAll("authuser");
    const route = url.pathname.match(/^\/u\/([^/]+)(?:\/|$)/);
    if ((query.length && (query.length !== 1 || query[0] !== authUser)) || (route && route[1] !== authUser)) throw new Error("account_mismatch");
    return tab;
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

  const api = { identity, checkTab, makeState };
  if (typeof module === "object" && module.exports) module.exports = api;
  else scope.AIStudioSession = Object.freeze(api);
})(globalThis);
