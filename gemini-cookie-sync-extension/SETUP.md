# Gemini Cookie Sync Setup

Short guide for extracting fresh Gemini auth data and applying it to `gemini-web2api`.

## What this extension exports

The extension reads the current signed-in Gemini session and exports:

- Google session cookies
- `SAPISID`
- `SNlM0e` (`xsrf_token`)
- `cfb2h` (`gemini_bl`)
- `auth_user`

It saves them locally as `gemini-auth-uN.json`, where `N` is the account index shown by the extension.
Only the current window's active Gemini tab is used. Its page token, account index, and cookie store must match.

## Install and export

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select the `gemini-cookie-sync-extension` folder
5. Open [https://gemini.google.com/app](https://gemini.google.com/app)
6. Switch to the account you want to export, refresh, and wait for the page to finish loading
7. Open the extension and click **Inspect session**
8. Confirm **Selected account** matches the intended account index
9. Click **Export selected account**

For an already installed copy, open `chrome://extensions` and click its reload button first. Version 1.1.0 fixes selection of the first Gemini tab instead of the current account. Re-export each account from its own active Gemini page; older files with an empty or incorrect `auth_user` should not be treated as distinct accounts based on their cookies alone.

Google sign-in cookies may be shared across multiple accounts. Distinguish exports by their matching `auth_user` and page token, not by cookie equality. A default `/app` page uses account index `0`; `/u/1/app` uses `1`, and so on. Numeric `?authuser=N` URLs are also supported.

Expected ready state:

```text
XSRF / SNlM0e: present
gemini_bl / cfb2h: present
Session and XSRF are ready for export.
```

## Apply it in `gemini-web2api`

Move the exported file into the project. This example uses account `1`; replace `u1` with the index in your exported filename:

```bash
cd /path/to/gemini-web2api

WIN_HOME=$(wslpath "$(powershell.exe -NoProfile -Command '[Environment]::GetFolderPath(\"UserProfile\")' | tr -d '\r')")
cp "$WIN_HOME/Downloads/gemini-auth-u1.json" ./gemini-auth.json
chmod 600 gemini-auth.json
```

Update `config.json`:

```bash
cd /path/to/gemini-web2api

AUTH_FILE="$(pwd)/gemini-auth.json"
tmp=$(mktemp)

jq \
  --arg auth_file "$AUTH_FILE" \
  --slurpfile auth "$AUTH_FILE" \
  '
    .cookie_file = $auth_file
    | .auth_user = $auth[0].auth_user
    | .xsrf_token = $auth[0].xsrf_token
    | if (($auth[0].gemini_bl // "") | length) > 0
      then .gemini_bl = $auth[0].gemini_bl
      else .
      end
  ' config.json > "$tmp" &&
mv "$tmp" config.json

chmod 600 config.json
```

Quick check:

```bash
jq '{
  cookie_file,
  auth_user,
  xsrf_token_set: ((.xsrf_token // "") | length > 0),
  gemini_bl_set: ((.gemini_bl // "") | length > 0)
}' config.json
```

## Restart and test

```bash
systemctl --user restart gemini-proxy
```

```bash
curl -sS http://127.0.0.1:10012/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "gemini-3.1-pro",
    "messages": [
      {
        "role": "user",
        "content": "Reply exactly with: authenticated-ok"
      }
    ]
  }' | jq
```

## Keep it secret

`gemini-auth.json` contains a real Google session. Do not share it, print it, or commit it to Git.
