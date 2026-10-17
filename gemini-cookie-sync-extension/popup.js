const CORE_REQUIRED = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];
const LEGACY_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];

const EXPORT_ORDER = [
  "SID",
  "HSID",
  "SSID",
  "APISID",
  "SAPISID",
  "LSID",
  "OSID",
  "SIDCC",
  "AEC",
  "NID",
  "COMPASS",
  "__Secure-1PAPISID",
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-1PSIDCC",
  "__Secure-1PSIDRTS",
  "__Secure-3PAPISID",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "__Secure-3PSIDCC",
  "__Secure-3PSIDRTS",
  "__Secure-OSID",
  "__Host-1PLSID",
  "__Host-3PLSID"
];

const statusEl = document.getElementById("status");
const exportButton = document.getElementById("export");
const inspectButton = document.getElementById("inspect");
const openButton = document.getElementById("open");

function setStatus(message, kind = "") {
  statusEl.textContent = message;
  statusEl.className = kind;
}

function normalizeDomain(domain = "") {
  return domain.replace(/^\./, "").toLowerCase();
}

function isGoogleCookie(cookie) {
  const domain = normalizeDomain(cookie.domain);
  return domain === "google.com" || domain.endsWith(".google.com");
}

function scoreCookie(cookie) {
  const domain = (cookie.domain || "").toLowerCase();
  let score = 0;

  if (domain === ".google.com") score += 120;
  else if (domain === "google.com") score += 110;
  else if (domain === ".gemini.google.com") score += 100;
  else if (domain === "gemini.google.com") score += 95;
  else if (domain === ".accounts.google.com") score += 80;
  else if (domain === "accounts.google.com") score += 75;
  else if (domain.endsWith(".google.com")) score += 40;

  if (cookie.path === "/") score += 10;
  if (cookie.secure) score += 3;
  if (cookie.httpOnly) score += 2;
  if (!cookie.partitionKey) score += 2;
  if (!cookie.session) score += 1;

  return score;
}

async function readGoogleCookies(tab, pageUrl) {
  const stores = await chrome.cookies.getAllCookieStores();
  const store = stores.find((candidate) => candidate.tabIds.includes(tab.id));
  if (!store) {
    throw new Error("Cannot find this Gemini tab's cookie store. Refresh the page and try again.");
  }
  const cookies = await chrome.cookies.getAll({ storeId: store.id, url: pageUrl });
  return cookies.filter((cookie) => cookie.storeId === store.id && isGoogleCookie(cookie) && cookie.value);
}

function selectBestCookies(cookies) {
  const selected = new Map();

  for (const name of EXPORT_ORDER) {
    const candidates = cookies
      .filter((cookie) => cookie.name === name && cookie.value)
      .sort((a, b) => scoreCookie(b) - scoreCookie(a));

    if (candidates.length > 0) selected.set(name, candidates[0]);
  }

  return selected;
}

function validateSelection(selected) {
  const missingCore = CORE_REQUIRED.filter((name) => !selected.has(name));
  const sessionCookie = SESSION_ALTERNATIVES.find((name) => selected.has(name));
  return {
    missingCore,
    sessionCookie,
    valid: missingCore.length === 0 && Boolean(sessionCookie)
  };
}

function listPresent(selected, names) {
  return names.filter((name) => selected.has(name));
}

function summarizeAvailable(cookies) {
  return [...new Set(cookies
    .map((cookie) => cookie.name)
    .filter((name) => /SID|APISID|LSID|OSID|COMPASS/.test(name)))]
    .sort();
}

function accountIndex(value) {
  if (value === null || value === undefined || value === "") return null;
  const text = String(value);
  if (!/^\d+$/.test(text) || !Number.isSafeInteger(Number(text))) return null;
  return String(Number(text));
}

function getPageAccount(pageMetadata) {
  const url = new URL(pageMetadata.url);
  if (url.protocol !== "https:" || url.hostname !== "gemini.google.com") {
    throw new Error("Select the Gemini page for the account you want to export.");
  }
  const pathMatch = url.pathname.match(/^\/u\/(\d+)(?:\/|$)/);
  if (url.pathname.startsWith("/u/") && !pathMatch) {
    throw new Error("The Gemini account path is invalid. Open the account's /u/N/app page and retry.");
  }
  const query = url.searchParams.get("authuser");
  if (query !== null && accountIndex(query) === null) {
    throw new Error("The Gemini account URL needs a numeric account index. Open the account's /u/N/app page.");
  }
  const indices = [pathMatch?.[1], query, pageMetadata.authUser]
    .map(accountIndex).filter((value) => value !== null);
  if (new Set(indices).size > 1) {
    throw new Error("Gemini account indicators disagree. Refresh the selected account page and try again.");
  }
  return indices[0] ?? "0";
}

async function chooseGeminiTab() {
  const tabs = await chrome.tabs.query({
    active: true,
    currentWindow: true,
    url: "https://gemini.google.com/*"
  });
  if (tabs.length !== 1 || !tabs[0].id) {
    throw new Error("Switch to the Gemini account page in this window, then open this extension again.");
  }
  if (tabs[0].status === "loading" || tabs[0].pendingUrl) {
    throw new Error("The Gemini page is changing. Wait for it to finish loading, then try again.");
  }
  return tabs[0];
}

async function readGeminiPageMetadata(tab) {
  if (!tab?.id) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "Open gemini.google.com in a tab, sign in, then refresh the page."
    };
  }

  try {
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: () => {
        const wiz = globalThis.WIZ_global_data || {};
        const html = document.documentElement?.innerHTML || "";

        const decode = (value) => {
          if (!value) return null;
          try {
            return JSON.parse(`"${value.replace(/"/g, '\\"')}"`);
          } catch {
            return value
              .replace(/\\u003d/gi, "=")
              .replace(/\\u0026/gi, "&")
              .replace(/\\u003c/gi, "<")
              .replace(/\\u003e/gi, ">");
          }
        };

        const regexValue = (name) => {
          const patterns = [
            new RegExp(`"${name}"\\s*:\\s*"([^"\\n]+)"`),
            new RegExp(`\\\\"${name}\\\\"\\s*:\\s*\\\\"([^"\\n]+)\\\\"`)
          ];
          for (const pattern of patterns) {
            const match = html.match(pattern);
            if (match?.[1]) return decode(match[1]);
          }
          return null;
        };

        let resourceBl = null;
        try {
          const resources = performance.getEntriesByType("resource");
          for (const entry of resources) {
            if (!entry?.name?.includes("gemini.google.com")) continue;
            const url = new URL(entry.name);
            const bl = url.searchParams.get("bl");
            if (bl) {
              resourceBl = bl;
              break;
            }
          }
        } catch {
          // Ignore performance API failures.
        }

        const xsrfToken = wiz.SNlM0e || regexValue("SNlM0e");
        const geminiBl = wiz.cfb2h || resourceBl || regexValue("cfb2h");
        const pageUrl = new URL(location.href);
        const pathAccount = pageUrl.pathname.match(/^\/u\/(\d+)(?:\/|$)/)?.[1];
        const metaAccount = document.querySelector?.('meta[name="google.authuser"]')?.content;
        const accountValues = [pathAccount, pageUrl.searchParams.get("authuser"),
          wiz.currentpageaccount, globalThis.currentpageaccount, wiz.authuser, metaAccount];
        const accounts = accountValues
          .filter((value) => value !== null && value !== undefined && value !== "")
          .map(String)
          .filter((value) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)))
          .map((value) => String(Number(value)));
        if (new Set(accounts).size > 1) {
          return { url: location.href, error: "Gemini account indicators changed. Refresh the page and retry." };
        }

        let source = null;
        if (wiz.SNlM0e || wiz.cfb2h) source = "WIZ_global_data";
        else if (resourceBl) source = "performance";
        else if (xsrfToken || geminiBl) source = "page-html";

        return {
          xsrfToken: xsrfToken || null,
          geminiBl: geminiBl || null,
          authUser: accounts[0] ?? "0",
          source,
          url: location.href
        };
      }
    });

    const metadata = result?.[0]?.result;
    return metadata ? { ...metadata, documentId: result[0].documentId } : {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "Failed to read Gemini page data."
    };
  } catch (error) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: error?.message || String(error)
    };
  }
}

async function assertTabUnchanged(tab, pageMetadata) {
  const current = await chrome.tabs.get(tab.id);
  const active = await chooseGeminiTab();
  if (active.id !== tab.id || current.windowId !== tab.windowId ||
      current.url !== tab.url || current.url !== pageMetadata.url ||
      current.status === "loading" || current.pendingUrl) {
    throw new Error("The Gemini account or page changed during inspection. Refresh and try again.");
  }
  if (pageMetadata.documentId) {
    try {
      const checks = await chrome.scripting.executeScript({
        target: { tabId: tab.id, documentIds: [pageMetadata.documentId] },
        world: "MAIN",
        func: () => ({ url: location.href, xsrfToken: globalThis.WIZ_global_data?.SNlM0e || null })
      });
      const check = checks[0]?.result;
      if (checks[0]?.documentId !== pageMetadata.documentId || check?.url !== pageMetadata.url ||
          (check?.xsrfToken && check.xsrfToken !== pageMetadata.xsrfToken)) {
        throw new Error("Changed document");
      }
    } catch {
      throw new Error("The Gemini session changed during inspection. Refresh and try again.");
    }
  }
}

async function buildInspection() {
  const tab = await chooseGeminiTab();
  const pageMetadata = await readGeminiPageMetadata(tab);
  if (pageMetadata.error) throw new Error(pageMetadata.error);
  const authUser = getPageAccount(pageMetadata);
  const cookies = await readGoogleCookies(tab, pageMetadata.url);
  await assertTabUnchanged(tab, pageMetadata);

  const selected = selectBestCookies(cookies);
  const validation = validateSelection(selected);
  const legacyPresent = listPresent(selected, LEGACY_COOKIES);
  const legacyMissing = LEGACY_COOKIES.filter((name) => !selected.has(name));
  const available = summarizeAvailable(cookies);

  return {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  };
}

function inspectionMessage(info) {
  const {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  } = info;

  const lines = [
    `Selected account: ${authUser} (current Gemini tab).`,
    `Page: ${new URL(pageMetadata.url).pathname}`,
    `Found ${cookies.length} cookie(s) for this Gemini session.`,
    "",
    `SAPISID: ${selected.has("SAPISID") ? "present" : "missing"}`,
    `Session cookie: ${validation.sessionCookie || "missing"}`,
    `XSRF / SNlM0e: ${pageMetadata.xsrfToken ? "present" : "missing"}`,
    `gemini_bl / cfb2h: ${pageMetadata.geminiBl ? "present" : "missing"}`,
    `auth_user: ${authUser ?? "default account"}`,
    `Page data source: ${pageMetadata.source || "unavailable"}`,
    "",
    `Legacy present: ${legacyPresent.join(", ") || "none"}`,
    `Legacy not visible to the extension: ${legacyMissing.join(", ") || "none"}`,
    "",
    `Available for export: ${available.join(", ") || "none"}`
  ];

  if (pageMetadata.error) {
    lines.push("", `Page note: ${pageMetadata.error}`);
  }

  if (validation.valid && pageMetadata.xsrfToken) {
    lines.push("", "Session and XSRF are ready for export.");
  } else if (validation.valid) {
    lines.push("", "Cookies are ready, but XSRF is missing. Open Gemini, refresh the page, then inspect again.");
  } else {
    lines.push("", "Session is incomplete: SAPISID and one of __Secure-1PSID, __Secure-3PSID, or SID are required.");
  }

  return lines.join("\n");
}

function buildCookieString(info) {
  return EXPORT_ORDER
    .filter((name) => info.selected.has(name))
    .map((name) => `${name}=${info.selected.get(name).value}`)
    .join("; ");
}

async function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2) + "\n"], {
    type: "application/json;charset=utf-8"
  });
  const url = URL.createObjectURL(blob);

  await chrome.downloads.download({
    url,
    filename,
    saveAs: true,
    conflictAction: "uniquify"
  });

  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

openButton.addEventListener("click", async () => {
  await chrome.tabs.create({ url: "https://gemini.google.com/app" });
});

inspectButton.addEventListener("click", async () => {
  inspectButton.disabled = true;
  setStatus("Inspecting session and page data…");

  try {
    const info = await buildInspection();
    const ready = info.validation.valid && Boolean(info.pageMetadata.xsrfToken);
    setStatus(inspectionMessage(info), ready ? "ok" : "warn");
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
  } finally {
    inspectButton.disabled = false;
  }
});

exportButton.addEventListener("click", async () => {
  exportButton.disabled = true;
  setStatus("Reading cookies and XSRF…");

  try {
    const info = await buildInspection();

    if (!info.validation.valid || !info.pageMetadata.xsrfToken) {
      throw new Error(inspectionMessage(info));
    }

    const cookieString = buildCookieString(info);
    const payload = {
      cookie: cookieString,
      sapisid: info.selected.get("SAPISID").value,
      auth_user: info.authUser,
      xsrf_token: info.pageMetadata.xsrfToken,
      gemini_bl: info.pageMetadata.geminiBl
    };

    const filename = `gemini-auth-u${info.authUser}.json`;
    await downloadJson(filename, payload);

    const exportedNames = EXPORT_ORDER.filter((name) => info.selected.has(name));
    setStatus(
      `Created ${filename} with ${exportedNames.length} cookie(s) and XSRF.\n\n` +
      `Session cookie: ${info.validation.sessionCookie}\n` +
      `XSRF: present\n` +
      `gemini_bl: ${info.pageMetadata.geminiBl ? "present" : "not present — current server setting will remain"}\n` +
      `auth_user: ${info.authUser ?? "null"}\n\n` +
      "Move the file into gemini-web2api and do not share it or commit it to Git.",
      "ok"
    );
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
  } finally {
    exportButton.disabled = false;
  }
});
