Gemini Cookie Sync v1.1.0

Purpose:
- Read cookies only for the current window's active Gemini tab and its cookie store.
- Extract the XSRF token named SNlM0e from the Gemini page.
- Extract gemini_bl from cfb2h or from page requests when available.
- Export `gemini-auth-uN.json` locally only, where N is the selected account index.

Installation:
1. Open `chrome://extensions`
2. Enable Developer mode
3. Click Load unpacked
4. Select this folder
5. Switch to the desired account in Gemini, refresh, and wait for the page to load
6. Click Inspect session
7. Check the selected account index and click Export selected account

Updating an installed copy:
1. Open chrome://extensions and reload Gemini Cookie Sync (version 1.1.0)
2. Close and reopen the extension popup from the desired Gemini account page
3. Export each account separately; account 0 is the default account

Multiple accounts:
Google accounts in the same browser can share sign-in cookies. The auth_user
index and XSRF token must come from the same selected page. Do not identify an
account by comparing cookie values alone. This version supports /u/N paths and
numeric ?authuser=N parameters, and refuses exports if the page changes during
inspection. Old exports without a reliable auth_user need to be exported again.

Security:
The generated file represents the real Google session and must be treated as secret. Do not send it, print it, or commit it to Git.
