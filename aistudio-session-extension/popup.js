"use strict";

const exportButton = document.getElementById("export");
const inspectButton = document.getElementById("inspect");
const statusElement = document.getElementById("status");
const inspectionElement = document.getElementById("inspection");
const emailInput = document.getElementById("email");
const accountInput = document.getElementById("account");
const confirmation = document.getElementById("confirmed");
const messages = {
  invalid_identity: "请填写有效邮箱和明确的账号索引（0、1、2…）。",
  wrong_tab: "请先切换到已登录的 AI Studio 页面，再打开此扩展。",
  account_mismatch: "填写的账号索引与当前页面不同，已停止导出。",
  email_mismatch: "填写的邮箱与当前页面身份不同，已停止导出。",
  identity_conflict: "页面的账号信息相互冲突，请刷新目标账号页面后重新读取。",
  inspection_unavailable: "无法检查当前页面，请在 Edge 扩展页重新加载本扩展并刷新 AI Studio 后重试。",
  changed_tab: "页面或账号已变化。请点“重新读取当前页面”，重新核对后再导出。",
  missing_cookies: "当前登录状态不完整，请在 AI Studio 正常登录后重试。",
  invalid_cookies: "登录数据未通过检查，已停止导出。"
};
let inspected = null;

async function selectedTab(authUser) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs.length !== 1) throw new Error("wrong_tab");
  return AIStudioSession.checkTab(tabs[0], authUser);
}

async function readIdentity(tab, documentId) {
  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, ...(documentId ? { documentIds: [documentId] } : { frameIds: [0] }) },
      world: "MAIN",
      func: AIStudioSession.readPageIdentity
    });
  } catch {
    throw new Error(documentId ? "changed_tab" : "inspection_unavailable");
  }
  const entry = results?.length === 1 && results[0];
  if (!entry || entry.frameId !== 0 || !entry.documentId || !entry.result) throw new Error("inspection_unavailable");
  if (entry.result.error) throw new Error(entry.result.error);
  if (entry.result.url !== tab.url || (documentId && entry.documentId !== documentId)) throw new Error("changed_tab");
  return { tabId: tab.id, documentId: entry.documentId, ...entry.result };
}

function samePageIdentity(before, after) {
  if (!before || before.tabId !== after.tabId || before.documentId !== after.documentId ||
      before.url !== after.url || before.email !== after.email || before.authUser !== after.authUser) {
    throw new Error("changed_tab");
  }
}

function matchesSelection(page, selected) {
  if (page.authUser !== null && page.authUser !== selected.authUser) throw new Error("account_mismatch");
  if (page.email !== null && page.email !== selected.email) throw new Error("email_mismatch");
}

async function inspectCurrentPage() {
  exportButton.disabled = true;
  inspectButton.disabled = true;
  confirmation.checked = false;
  confirmation.disabled = true;
  inspected = null;
  statusElement.textContent = "";
  inspectionElement.textContent = "正在读取当前页面身份信息……";
  try {
    const tab = await selectedTab();
    const page = await readIdentity(tab);
    const active = await selectedTab();
    if (active.id !== tab.id || active.url !== tab.url) throw new Error("changed_tab");
    emailInput.value = page.email || "";
    accountInput.value = page.authUser || "";
    inspected = page;
    if (page.email && page.authUser !== null) {
      inspectionElement.textContent = "已读取当前页面的邮箱和账号索引，请与头像菜单核对后勾选确认。";
    } else if (page.email) {
      inspectionElement.textContent = "已读取邮箱；页面未提供可靠账号索引，请手动核对填写，未默认使用 0。";
    } else if (page.authUser !== null) {
      inspectionElement.textContent = "已读取账号索引；页面未提供可靠邮箱，请打开头像菜单核对并手动填写。";
    } else {
      inspectionElement.textContent = "页面未提供可靠邮箱或账号索引，请从当前账号菜单和网址核对后手动填写。";
    }
  } catch (error) {
    emailInput.value = "";
    accountInput.value = "";
    inspectionElement.textContent = messages[error?.message] || messages.inspection_unavailable;
  } finally {
    confirmation.disabled = false;
    exportButton.disabled = false;
    inspectButton.disabled = false;
  }
}

async function storeForTab(tabId) {
  const stores = await chrome.cookies.getAllCookieStores();
  const selected = stores.filter(store => store.tabIds.includes(tabId));
  if (selected.length !== 1) throw new Error("changed_tab");
  return selected[0].id;
}

for (const input of [emailInput, accountInput]) input.addEventListener("input", () => { confirmation.checked = false; });
let inspectionReady = inspectCurrentPage();
inspectButton.addEventListener("click", () => {
  inspectionReady = inspectCurrentPage();
  return inspectionReady;
});

exportButton.addEventListener("click", async () => {
  await inspectionReady;
  statusElement.textContent = "";
  if (!confirmation.checked) {
    statusElement.textContent = "请先核对页面邮箱和账号索引，再勾选确认。";
    return;
  }
  exportButton.disabled = true;
  inspectButton.disabled = true;
  let blobURL;
  try {
    if (!inspected) throw new Error("inspection_unavailable");
    const selected = AIStudioSession.identity(emailInput.value, accountInput.value);
    const tab = await selectedTab(selected.authUser);
    if (tab.id !== inspected.tabId || tab.url !== inspected.url) throw new Error("changed_tab");
    const before = await readIdentity(tab, inspected.documentId);
    samePageIdentity(inspected, before);
    matchesSelection(before, selected);
    const storeId = await storeForTab(tab.id);
    const rawCookies = await chrome.cookies.getAll({ url: "https://aistudio.google.com/", storeId });
    const currentTab = await selectedTab(selected.authUser);
    if (currentTab.id !== tab.id || currentTab.url !== tab.url || await storeForTab(tab.id) !== storeId) throw new Error("changed_tab");
    const after = await readIdentity(currentTab, before.documentId);
    samePageIdentity(before, after);
    matchesSelection(after, selected);
    const currentInput = AIStudioSession.identity(emailInput.value, accountInput.value);
    if (!confirmation.checked || currentInput.email !== selected.email || currentInput.authUser !== selected.authUser) throw new Error("changed_tab");
    const state = AIStudioSession.makeState(rawCookies, selected.email, selected.authUser);
    const encoded = JSON.stringify(state, null, 2) + "\n";
    if (encoded.length > 1024 * 1024) throw new Error("invalid_cookies");
    blobURL = URL.createObjectURL(new Blob([encoded], { type: "application/json" }));
    await chrome.downloads.download({ url: blobURL, filename: "aistudio-session-" + selected.authUser + ".json", saveAs: true, conflictAction: "uniquify" });
    statusElement.textContent = "已提交本机保存，共 " + state.cookies.length + " 项。请在下载列表确认保存完成。";
  } catch (error) {
    confirmation.checked = false;
    statusElement.textContent = messages[error?.message] || "导出未完成。请检查扩展权限和本机保存操作后重试。";
  } finally {
    if (blobURL) setTimeout(() => URL.revokeObjectURL(blobURL), 30000);
    exportButton.disabled = false;
    inspectButton.disabled = false;
  }
});
