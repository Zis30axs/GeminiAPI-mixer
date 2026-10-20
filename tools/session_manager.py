"""Local-only session maintenance. No HTTP listener, browser discovery or raw logs.

The native UI owns explicit file selection. All network destinations and Docker
targets are fixed; a session's own fields cannot choose a URL or command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

import httpx
import import_aistudio_session as secure

SOURCES = {"web": "gemini-3.8-flash", "ais": "ais-gemini-3.8-flash", "build": "build-gemini-3.8-flash"}
CONTAINERS = {"web": "geminiapi-mixer-mixer-1", "studio": "geminiapi-mixer-aistudio-http-1"}
STATES = {"ready", "busy", "cooldown", "auth_required", "disabled", "stopped", "starting", "error"}
MAX_BATCH = 32


class MaintenanceError(ValueError):
    pass


def fail(code):
    raise MaintenanceError(code)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value):
    return (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode()


def masked(email):
    local, _, domain = email.partition("@")
    return local[:2] + "***@" + domain


def read_settings(root):
    path = secure.checked_path(root / ".env")
    if path.stat().st_size > 1024 * 1024:
        fail("invalid_configuration")
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("\\'", "'")
        elif value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                fail("invalid_configuration")
        if key in result:
            fail("duplicate_configuration")
        result[key] = value
    return result


def web_state(value):
    fields = {"cookie", "sapisid", "auth_user", "xsrf_token", "gemini_bl"}
    if not isinstance(value, dict) or set(value) != fields:
        fail("invalid_web_fields")
    if any(not isinstance(v, str) or not v or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in value.values()):
        fail("invalid_web_value")
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", value["auth_user"]):
        fail("invalid_web_index")
    if any(len(value[k]) > limit for k, limit in (("cookie", 256 * 1024), ("sapisid", 8192), ("xsrf_token", 32768), ("gemini_bl", 512))):
        fail("web_value_too_large")
    pairs = {}
    for item in value["cookie"].split(";"):
        name, sep, content = item.strip().partition("=")
        if not sep or not secure.COOKIE_NAME.fullmatch(name) or not secure.COOKIE_VALUE.fullmatch(content) or name in pairs:
            fail("invalid_web_cookie")
        pairs[name] = content
    if pairs.get("SAPISID") != value["sapisid"] or not all(pairs.get(k) for k in ("__Secure-1PSID", "__Secure-3PSID")):
        fail("missing_web_session_cookie")
    return value


@dataclass(repr=False)
class Export:
    kind: str
    index: str
    identity: str = field(repr=False)
    state: dict = field(repr=False)
    filename: str
    selected_at: float = field(default_factory=time.monotonic)

    def summary(self):
        return {"kind": self.kind, "index": self.index,
                "account": "Web u" + self.index if self.kind == "web" else masked(self.identity),
                "filename": self.filename}


def read_export(path):
    path = secure.checked_path(path)
    if path.suffix.lower() != ".json":
        fail("json_file_required")
    value = secure.read_local_json(path)
    if isinstance(value, dict) and "aistudio2api" in value:
        state = secure.validate_state(value)
        source = state["aistudio2api"]["source"]
        return Export("studio", source["auth_user"], source["email"], state, path.name)
    state = web_state(value)
    return Export("web", state["auth_user"], state["auth_user"], state, path.name)


def select_exports(paths):
    if not 1 <= len(paths) <= MAX_BATCH:
        fail("batch_size_limit")
    exports, seen = [], set()
    for path in paths:
        item = read_export(path)
        identity = (item.kind, item.identity)
        if identity in seen:
            fail("duplicate_account_in_selection")
        seen.add(identity)
        exports.append(item)
    return exports


def latest_downloads(directory):
    directory = secure.checked_path(directory)
    # Only named extension exports; no browser profile/database access.
    paths = []
    for pattern in ("gemini-auth*.json", "aistudio-session-*.json"):
        for path in directory.glob(pattern):
            paths.append(path)
            if len(paths) > 256:
                fail("too_many_downloads_select_files")
    ordered, rejected = [], 0
    for path in paths:
        try:
            checked = secure.checked_path(path)
            ordered.append((checked.lstat().st_mtime, checked))
        except (ValueError, OSError):
            rejected += 1
    ordered.sort(key=lambda pair: pair[0], reverse=True)
    chosen, seen = [], set()
    for _, path in ordered:
        try:
            item = read_export(path)
        except (ValueError, OSError):
            rejected += 1
            continue
        identity = (item.kind, item.identity)
        if identity not in seen:
            seen.add(identity)
            chosen.append(item)
            if len(chosen) > MAX_BATCH:
                fail("batch_size_limit")
    return chosen, rejected


def classify(status, code=""):
    if code in ("web_authentication_failed", "upstream_authentication_failed") or status in (401, 403):
        return "auth_required"
    if status == 429 or "rate_limit" in code:
        return "rate_limited"
    if status in (408, 504) or "timeout" in code:
        return "timeout"
    if code == "web_accounts_unavailable":
        return "web_accounts_unavailable"
    if "connection" in code:
        return "connection_failed"
    if status == 404:
        return "model_unavailable"
    return "upstream_error"


class SessionManager:
    def __init__(self, deployment_root, *, docker=None):
        self.root = secure.checked_path(deployment_root)
        settings = read_settings(self.root)
        self.auth = secure.checked_path(Path(settings["AISTUDIO_DATA_DIR"]) / "auth")
        self.web = secure.checked_path(Path(settings["GEMINI_WEB_ACCOUNTS_DIR_HOST"]))
        self.runtime = secure.checked_path(self.root / "work/runtime")
        self.backups = secure.checked_path(self.root.parent / "Backups")
        self.docker_exe = docker or shutil.which("docker")
        if not self.docker_exe:
            fail("docker_missing")
        self.lock = threading.Lock()

    def docker(self, *arguments):
        result = subprocess.run([self.docker_exe, *arguments], capture_output=True, timeout=65,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            fail("docker_operation_failed")
        return result.stdout.decode("utf-8", errors="replace").strip()

    def container(self, kind):
        name = CONTAINERS[kind]
        template = '{{.State.Status}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}'
        values = self.docker("inspect", "--type", "container", "--format", template, name).split("|")
        if len(values) != 3 or values[1:] != ["geminiapi-mixer", "mixer" if kind == "web" else "aistudio-http"]:
            fail("unexpected_container")
        return values[0]

    def request(self, base, path, *, headers=None, body=None, client=None, timeout=12):
        # Both caller-controlled choices are internal constants, never file data.
        if base not in ("http://127.0.0.1:18083", "http://127.0.0.1:18084"):
            fail("invalid_local_destination")
        own = client is None
        client = client or httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout)
        timer = threading.Timer(timeout, client.close)
        timer.daemon = True
        try:
            timer.start()
            with client.stream("GET" if body is None else "POST", base + path, headers=headers, json=body) as response:
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > 2 * 1024 * 1024:
                        fail("response_too_large")
                return response.status_code, json.loads(raw)
        finally:
            timer.cancel()
            if own:
                client.close()

    def studio_status(self):
        settings = read_settings(self.root)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=8) as client:
            status, _ = self.request("http://127.0.0.1:18084", "/api/auth/login", body={
                "username": settings.get("AISTUDIO_ADMIN_USERNAME", "mixer-admin"),
                "password": settings["AISTUDIO_ADMIN_PASSWORD"]}, client=client)
            if status != 200:
                fail("admin_authentication_failed")
            status, value = self.request("http://127.0.0.1:18084", "/api/status", client=client)
            if status != 200 or not isinstance(value, dict):
                fail("admin_unavailable")
            _, details = self.request("http://127.0.0.1:18084", "/api/accounts", client=client)
        identities = {}
        for path in self.auth.glob("*/storage-state.json"):
            state = secure.read_local_json(path)
            source = state.get("aistudio2api", {}).get("source", {})
            identities[source.get("email")] = source.get("auth_user", "?")
        accounts = [{"account": masked(str(a.get("id", ""))), "index": identities.get(a.get("id"), "?"),
                     "state": a.get("state") if a.get("state") in STATES else "unknown"}
                    for a in details.get("accounts", [])[:32]]
        counts = value.get("accounts", {})
        return {"ready": value.get("ready") is True, "running": value.get("running") is True,
                "counts": {k: v for k, v in counts.items() if k in ("total", "ready", "busy", "cooldown", "auth_required") and type(v) is int},
                "accounts": accounts}

    def snapshot(self):
        result = {"checked_at": now()}
        for kind in CONTAINERS:
            try:
                result[kind + "_container"] = self.container(kind)
            except (ValueError, OSError, subprocess.SubprocessError):
                result[kind + "_container"] = "unavailable"
        try:
            status, _ = self.request("http://127.0.0.1:18083", "/healthz")
            result["mixer_reachable"] = status == 200
        except (ValueError, OSError, RuntimeError, httpx.HTTPError):
            result["mixer_reachable"] = False
        try:
            manifest = secure.read_local_json(self.web / "manifest.json")
            result["web_accounts"] = [{"account": a["id"], "index": str(a["auth_user"])} for a in manifest["accounts"]]
        except (ValueError, OSError, KeyError, TypeError):
            result["web_accounts"] = []
        try:
            result["studio"] = self.studio_status()
        except (ValueError, OSError, RuntimeError, httpx.HTTPError):
            result["studio"] = {"error": "studio_status_unavailable"}
        try:
            key = read_settings(self.root)["MIXER_API_KEYS"].split(",")[0].strip()
            status, catalog = self.request("http://127.0.0.1:18083", "/v1/models", headers={"Authorization": "Bearer " + key})
            if status == 200:
                result["tools"] = {source: next((row.get("capabilities", {}).get("tools") is True
                    for row in catalog.get("data", []) if row.get("id") == SOURCES[source]), None) for source in ("ais", "build")}
        except (ValueError, OSError, RuntimeError, KeyError, AttributeError, httpx.HTTPError):
            pass
        return result

    def downloads(self, directory):
        """Quick renewal selects configured identities; new accounts use the picker."""
        items, rejected = latest_downloads(directory)
        manifest = secure.read_local_json(self.web / "manifest.json")
        allowed = {("web", str(row["auth_user"])) for row in manifest.get("accounts", [])}
        for path in self.auth.glob("*/storage-state.json"):
            state = secure.read_local_json(path)
            email = state.get("aistudio2api", {}).get("source", {}).get("email")
            if isinstance(email, str):
                allowed.add(("studio", email))
        selected = [item for item in items if (item.kind, item.identity) in allowed]
        return selected, rejected + len(items) - len(selected)

    def _plans(self, exports):
        if not exports or len(exports) > MAX_BATCH:
            fail("batch_size_limit")
        secure.checked_path(self.web)
        manifest_path = self.web / "manifest.json"
        manifest = secure.read_local_json(manifest_path)
        entries = manifest.get("accounts")
        if not isinstance(entries, list) or len(entries) > MAX_BATCH:
            fail("invalid_existing_manifest")
        seen, plans = set(), []
        for item in exports:
            identity = (item.kind, item.identity)
            if identity in seen:
                fail("duplicate_account_in_selection")
            seen.add(identity)
            if time.monotonic() - item.selected_at > 900:
                fail("selection_expired")
            if item.kind == "web":
                web_state(item.state)
                if item.state["auth_user"] != item.index or item.identity != item.index:
                    fail("account_identity_mismatch")
                target = secure.checked_path(self.web / ("u" + item.index + ".json"))
                existing = [a for a in entries if str(a.get("auth_user")) == item.index]
                if existing and existing != [{"id": "web-u" + item.index, "auth_user": int(item.index), "cookie_file": target.name}]:
                    fail("unexpected_existing_web_slot")
                if not existing:
                    entries.append({"id": "web-u" + item.index, "auth_user": int(item.index), "cookie_file": target.name})
                if len(entries) > MAX_BATCH:
                    fail("account_limit")
            elif item.kind == "studio":
                secure.validate_state(item.state)
                if item.state["aistudio2api"]["source"] != {"browser": "mixer-import", "email": item.identity, "auth_user": item.index}:
                    fail("account_identity_mismatch")
                directory = secure.checked_path(self.auth / item.identity)
                target = directory / "storage-state.json"
                if directory.exists():
                    secure.validate_previous_identity(target, item.identity)
                    config = secure.read_local_json(directory / "account.json")
                    if config.get("label") != item.identity:
                        fail("account_identity_mismatch")
            else:
                fail("invalid_export_kind")
            unchanged = target.exists() and secure.read_local_json(target) == item.state
            plans.append((item, target, unchanged))
        return plans, manifest

    @contextmanager
    def exclusive(self):
        """Serialize maintenance even when the shortcut opens two windows."""
        if not self.lock.acquire(blocking=False):
            fail("operation_in_progress")
        handle = None
        try:
            path = secure.checked_path(self.runtime / ".session-manager.lock")
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            handle = os.fdopen(descriptor, "r+b")
            secure.private_permissions(path, False)
            if path.stat().st_size == 0:
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fail("operation_in_progress")
            yield
        finally:
            if handle:
                handle.close()  # OS releases the lock, including on a crash.
            self.lock.release()

    def import_exports(self, exports, progress=lambda _: None):
        with self.exclusive():
            plans, manifest = self._plans(exports)
            changes = [(item, target) for item, target, same in plans if not same]
            if not changes:
                return {"imported": 0, "unchanged": len(plans), "message": "unchanged_exports"}
            kinds = {item.kind for item, _ in changes}
            for kind in kinds:
                self.container(kind)  # Verify both before stopping either.
            backup = secure.checked_path(self.backups / ("session-entry-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]))
            backup.mkdir(); secure.private_permissions(backup, True)
            saved, created, stopped = [], [], []
            try:
                for kind in sorted(kinds):
                    progress("正在暂停 " + ("Mixer" if kind == "web" else "AIS / Build") + " 并备份…")
                    self.docker("stop", "--time", "20", CONTAINERS[kind]); stopped.append(kind)
                destinations = [target for _, target in changes]
                if "web" in kinds:
                    destinations.append(self.web / "manifest.json")
                for number, target in enumerate(destinations):
                    if target.exists():
                        content = target.read_bytes()
                        secure.atomic_private_write(backup / (str(number) + ".json"), content)
                        saved.append((target, content))
                    else:
                        created.append(target)
                secure.atomic_private_write(backup / "restore.json", encode({
                    "saved": [{"destination": str(target), "backup": str(number) + ".json"}
                              for number, target in enumerate(destinations) if target.exists()],
                    "created": [str(target) for target in created]}))
                for number, (item, target) in enumerate(changes):
                    progress("正在导入 " + ("Web u" + item.index if item.kind == "web" else "AIS / Build u" + item.index))
                    if item.kind == "web":
                        secure.atomic_private_write(target, encode(item.state))
                    else:
                        source = backup / ("new-" + str(number) + ".json")
                        secure.atomic_private_write(source, encode(item.state))
                        secure.import_session(source, self.auth, replace=target.parent.exists())
                if "web" in kinds:
                    secure.atomic_private_write(self.web / "manifest.json", encode(manifest))
                for kind in stopped:
                    progress("正在启动 " + ("Mixer" if kind == "web" else "AIS / Build") + "…")
                    self.docker("start", CONTAINERS[kind])
            except Exception:
                # Stop writers before rollback; never change cooldown/runtime files.
                rollback_errors = False
                for kind in stopped:
                    try:
                        self.docker("stop", "--time", "20", CONTAINERS[kind])
                    except (ValueError, OSError, subprocess.SubprocessError):
                        rollback_errors = True
                if rollback_errors:
                    # If the daemon cannot confirm a stop, never restore under
                    # a possibly active account writer. The private journal is
                    # available for recovery after the service is stopped.
                    fail("rollback_needs_attention")
                for target, content in saved:
                    secure.atomic_private_write(target, content)
                for target in created:
                    if target.exists():
                        secure.checked_path(target).unlink()
                    if target.parent != self.web and target.parent.exists():
                        # Only files created by this transaction, no recursive delete.
                        account = target.parent / "account.json"
                        if account.exists():
                            secure.checked_path(account).unlink()
                        if not any(target.parent.iterdir()):
                            target.parent.rmdir()
                for kind in stopped:
                    try:
                        self.docker("start", CONTAINERS[kind])
                    except (ValueError, OSError, subprocess.SubprocessError):
                        rollback_errors = True
                fail("rollback_needs_attention" if rollback_errors else "import_rolled_back")
            return {"imported": len(changes), "unchanged": len(plans) - len(changes), "backup": str(backup),
                    "accounts": [item.summary() for item, _ in changes]}

    def _probe(self, source, stream=False):
        start = time.monotonic()
        row = {"source": source, "stream": stream, "ok": False, "checked_at": now()}
        settings = read_settings(self.root)
        key = settings["MIXER_API_KEYS"].split(",")[0].strip()
        body = {"model": SOURCES[source], "messages": [{"role": "user", "content": "Reply with exactly OK."}], "stream": stream}
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(90, connect=5)) as client:
                timer = threading.Timer(120, client.close); timer.daemon = True; timer.start()
                try:
                    with client.stream("POST", "http://127.0.0.1:18083/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json=body) as response:
                        row["status"] = response.status_code
                        actual_source = response.headers.get("X-Mixer-Source")
                        row["source_confirmed"] = actual_source == source
                        account = response.headers.get("X-Mixer-Web-Account", "")
                        if re.fullmatch(r"web-u[0-9]{1,3}", account):
                            row["account"] = account
                        if response.status_code != 200 or not stream:
                            raw = bytearray()
                            for chunk in response.iter_bytes():
                                raw.extend(chunk)
                                if len(raw) > 2 * 1024 * 1024:
                                    fail("response_too_large")
                            data = json.loads(raw)
                            if response.status_code != 200 or data.get("error"):
                                code = data.get("error", {}).get("code", "")
                                row["result"] = "mixer_key_rejected" if response.status_code == 401 and not actual_source else classify(response.status_code, str(code))
                            else:
                                choices = data.get("choices") or [{}]
                                content = choices[0].get("message", {}).get("content", "")
                                row["ok"] = bool(isinstance(content, str) and content.strip() and row["source_confirmed"])
                                row["result"] = "ok" if row["ok"] else "invalid_generation"
                        else:
                            if "text/event-stream" not in response.headers.get("content-type", ""):
                                fail("invalid_stream")
                            total, chars, terminal, done, error = 0, 0, False, False, None
                            for line in response.iter_lines():
                                if time.monotonic() - start > 120:
                                    raise TimeoutError
                                total += len(line)
                                if total > 2 * 1024 * 1024 or len(line) > 1024 * 1024:
                                    fail("response_too_large")
                                if not line.startswith("data:"):
                                    continue
                                payload = line[5:].strip()
                                if payload == "[DONE]":
                                    done = True; break
                                event = json.loads(payload)
                                if event.get("error"):
                                    error = classify(502, str(event["error"].get("code", "")))
                                for choice in event.get("choices", []):
                                    chars += len(choice.get("delta", {}).get("content") or "")
                                    terminal = terminal or bool(choice.get("finish_reason"))
                            row["ok"] = bool(chars and terminal and done and not error and row["source_confirmed"])
                            row["result"] = "ok" if row["ok"] else error or "incomplete_stream"
                finally:
                    timer.cancel()
        except (httpx.TimeoutException, TimeoutError):
            row["result"] = "timeout"
        except httpx.HTTPError:
            row["result"] = "connection_failed"
        except (ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            row["result"] = "invalid_response"
        row["seconds"] = round(time.monotonic() - start, 2)
        return row

    def test(self, sources, stream=False, progress=lambda _: None):
        if not sources or any(s not in SOURCES for s in sources):
            fail("invalid_test_source")
        with self.exclusive():
            results = []
            studio = None
            if any(s in sources for s in ("ais", "build")):
                try:
                    studio = self.studio_status()
                except (ValueError, OSError, RuntimeError, httpx.HTTPError):
                    pass
            for source in dict.fromkeys(sources):
                progress("正在测活 " + source.upper() + " · Gemini 3.8…")
                if source != "web" and studio and studio.get("counts", {}).get("total", 0) > 0 and studio["counts"].get("auth_required") == studio["counts"].get("total"):
                    row = {"source": source, "stream": False, "ok": False, "result": "auth_required", "generation_skipped": True, "checked_at": now()}
                else:
                    row = self._probe(source)
                results.append(row)
                if stream and row["ok"]:
                    progress("正在检查 " + source.upper() + " 流式…")
                    results.append(self._probe(source, True))
            report = {"checked_at": now(), "results": results}
            secure.atomic_private_write(self.runtime / "session-manager-check.json", encode(report))
            return report

    def wait_after_import(self, studio, progress=lambda _: None):
        end = time.monotonic() + (100 if studio else 20)
        while time.monotonic() < end:
            progress("正在等待账号初始化；登录过期时仍需重新导出…")
            try:
                status, _ = self.request("http://127.0.0.1:18083", "/healthz", timeout=4)
                if status == 200:
                    if not studio:
                        return
                    state = self.studio_status()
                    counts = state.get("counts", {})
                    if state.get("ready") or counts.get("total", 0) and counts.get("auth_required") == counts.get("total"):
                        return
            except (ValueError, OSError, RuntimeError, httpx.HTTPError):
                pass
            time.sleep(2)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Native local Gemini session entry")
    parser.add_argument("--deployment-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="Safe status only; no generation")
    args = parser.parse_args()
    try:
        manager = SessionManager(args.deployment_root)
        if args.check:
            print(json.dumps(manager.snapshot(), ensure_ascii=True))
        else:
            # Keep the UI and entrypoint's safe exception types identical.
            import sys
            sys.modules.setdefault("session_manager", sys.modules[__name__])
            from session_manager_ui import open_window
            open_window(manager)
    except Exception:
        # No exception string can include credentials or session directory names.
        if args.check:
            print(json.dumps({"error": "local_configuration_or_runtime_error"}))
        else:
            import tkinter.messagebox
            tkinter.messagebox.showerror("Gemini 快捷入口", "入口未能启动。请先启动 D 盘服务，并检查本机配置。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
