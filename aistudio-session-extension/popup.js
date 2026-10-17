"use strict";

const exportButton = document.getElementById("export");
const statusElement = document.getElementById("status");
const messages = {
  invalid_identity: "请填写有效邮箱和明确的账号索引（0、1、2…）。",
  wrong_tab: "请先切换到已登录的 AI Studio 页面，再打开此扩展。",
  account_mismatch: "填写的账号索引与当前页面网址不同，已停止导出。",
  changed_tab: "导出期间页面或账号环境发生变化，请重新核对后再试。",
  missing_cookies: "当前登录状态不完整，请在 AI Studio 正常登录后重试。",
  invalid_cookies: "登录数据未通过检查，已停止导出。"
};

async function selectedTab(authUser) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs.length !== 1) throw new Error("wrong_tab");
  return AIStudioSession.checkTab(tabs[0], authUser);
}

async function storeForTab(tabId) {
  const stores = await chrome.cookies.getAllCookieStores();
  const selected = stores.filter(store => store.tabIds.includes(tabId));
  if (selected.length !== 1) throw new Error("changed_tab");
  return selected[0].id;
}

exportButton.addEventListener("click", async () => {
  statusElement.textContent = "";
  if (!document.getElementById("confirmed").checked) {
    statusElement.textContent = "请先核对页面邮箱和账号索引，再勾选确认。";
    return;
  }
  exportButton.disabled = true;
  let blobURL;
  try {
    const selected = AIStudioSession.identity(document.getElementById("email").value, document.getElementById("account").value);
    const tab = await selectedTab(selected.authUser);
    const storeId = await storeForTab(tab.id);
    const rawCookies = await chrome.cookies.getAll({ url: "https://aistudio.google.com/", storeId });
    const currentTab = await selectedTab(selected.authUser);
    if (currentTab.id !== tab.id || currentTab.url !== tab.url || await storeForTab(tab.id) !== storeId) throw new Error("changed_tab");
    const state = AIStudioSession.makeState(rawCookies, selected.email, selected.authUser);
    const encoded = JSON.stringify(state, null, 2) + "\n";
    if (encoded.length > 1024 * 1024) throw new Error("invalid_cookies");
    blobURL = URL.createObjectURL(new Blob([encoded], { type: "application/json" }));
    await chrome.downloads.download({ url: blobURL, filename: `aistudio-session-${selected.authUser}.json`, saveAs: true, conflictAction: "uniquify" });
    statusElement.textContent = `已提交本机保存，共 ${state.cookies.length} 项。请在下载列表确认保存完成。`;
  } catch (error) {
    statusElement.textContent = messages[error?.message] || "导出未完成。请检查扩展权限和本机保存操作后重试。";
  } finally {
    if (blobURL) setTimeout(() => URL.revokeObjectURL(blobURL), 30000);
    exportButton.disabled = false;
  }
});
