"""Synthetic maintenance transactions; never touches local accounts or Docker."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("import_aistudio_session", TOOLS / "import_aistudio_session.py")
secure = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(secure)
sys.modules["import_aistudio_session"] = secure
SPEC = importlib.util.spec_from_file_location("session_manager", TOOLS / "session_manager.py")
manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)


def studio(email="owner@example.com", index="2", token="synthetic-secret"):
    return {"cookies": [{"name": name, "value": token, "domain": ".google.com", "path": "/", "expires": -1,
                         "httpOnly": True, "secure": True, "sameSite": "Lax"} for name in secure.REQUIRED],
            "origins": [], "aistudio2api": {"source": {"browser": "mixer-import", "email": email, "auth_user": index}}}


def web(index="0", token="synthetic-secret"):
    return {"cookie": "SAPISID=" + token + "; __Secure-1PSID=" + token + "; __Secure-3PSID=" + token,
            "sapisid": token, "auth_user": index, "xsrf_token": "synthetic-xsrf", "gemini_bl": "synthetic-build"}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "mixer"; self.root.mkdir()
        self.runtime = self.root / "work/runtime"; self.runtime.mkdir(parents=True)
        self.auth = self.runtime / "studio/auth"; self.auth.parent.mkdir()
        secure.prepare_private_root(self.auth)
        self.web = self.runtime / "web"; self.web.mkdir()
        self.backups = self.base / "Backups"; self.backups.mkdir()
        self.write(self.web / "manifest.json", {"accounts": [{"id": "web-u0", "auth_user": 0, "cookie_file": "u0.json"}]})
        self.write(self.web / "u0.json", web(token="old-synthetic"))
        (self.root / ".env").write_text("AISTUDIO_DATA_DIR='" + str(self.auth.parent) + "'\nGEMINI_WEB_ACCOUNTS_DIR_HOST='" + str(self.web) + "'\nMIXER_API_KEYS='synthetic-mixer-test-key'\nAISTUDIO_ADMIN_PASSWORD='synthetic-admin-password'\n")
        self.m = manager.SessionManager(self.root, docker="synthetic-docker")
        self.commands = []
        def docker(*args):
            self.commands.append(args)
            if args[0] == "inspect":
                return "running|geminiapi-mixer|" + ("mixer" if args[-1] == manager.CONTAINERS["web"] else "aistudio-http")
            return ""
        self.m.docker = docker

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def export(self, state, name="export.json"):
        return manager.read_export(self.write(self.base / name, state))

    def test_mixed_import_adds_web_and_studio_without_config_or_runtime_reset(self):
        original = studio(token="old-studio")
        src = self.write(self.base / "previous.json", original)
        secure.import_session(src, self.auth)
        account = self.auth / "owner@example.com"
        config = secure.read_local_json(account / "account.json"); config["proxy"] = "synthetic-proxy-setting"
        self.write(account / "account.json", config)
        runtime = self.write(account / "runtime.json", {"cooldown_until": 9999999999})
        before = (account / "account.json").read_bytes(), runtime.read_bytes()
        result = self.m.import_exports([self.export(web("4"), "web.json"), self.export(studio(), "studio.json")])
        self.assertEqual(result["imported"], 2)
        self.assertEqual(before, ((account / "account.json").read_bytes(), runtime.read_bytes()))
        self.assertEqual(secure.read_local_json(account / "storage-state.json"), studio())
        self.assertEqual(len(secure.read_local_json(self.web / "manifest.json")["accounts"]), 2)
        self.assertEqual({c[-1] for c in self.commands if c[0] == "start"}, set(manager.CONTAINERS.values()))
        self.assertTrue(Path(result["backup"]).is_dir())

    def test_unchanged_export_never_restarts(self):
        result = self.m.import_exports([self.export(web(token="old-synthetic"))])
        self.assertEqual(result["imported"], 0)
        self.assertEqual(self.commands, [])

    def test_new_studio_account_has_private_valid_state(self):
        result = self.m.import_exports([self.export(studio())])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(secure.read_source(self.auth / "owner@example.com/storage-state.json"), studio())
        self.assertFalse(any(c[-1] == manager.CONTAINERS["web"] for c in self.commands))

    def test_write_failure_rolls_back_all_and_starts_both_services(self):
        before = (self.web / "u0.json").read_bytes()
        write = secure.atomic_private_write
        def broken(path, value):
            if path.name == "storage-state.json" and b"synthetic-secret" in value:
                raise OSError("synthetic failure")
            return write(path, value)
        with patch.object(secure, "atomic_private_write", broken):
            with self.assertRaisesRegex(manager.MaintenanceError, "import_rolled_back"):
                self.m.import_exports([self.export(web(), "web.json"), self.export(studio(), "studio.json")])
        self.assertEqual((self.web / "u0.json").read_bytes(), before)
        self.assertFalse((self.auth / "owner@example.com").exists())
        self.assertEqual({c[-1] for c in self.commands if c[0] == "start"}, set(manager.CONTAINERS.values()))

    def test_start_failure_restores_existing_state(self):
        before = (self.web / "u0.json").read_bytes()
        old = self.m.docker
        count = 0
        def broken(*args):
            nonlocal count
            if args[0] == "start":
                count += 1
                if count == 1:
                    raise manager.MaintenanceError("docker_operation_failed")
            return old(*args)
        self.m.docker = broken
        with self.assertRaisesRegex(manager.MaintenanceError, "import_rolled_back"):
            self.m.import_exports([self.export(web())])
        self.assertEqual((self.web / "u0.json").read_bytes(), before)
        self.assertEqual(count, 2)

    def test_invalid_batch_has_no_side_effects(self):
        first = self.export(web())
        second = self.export(studio())
        second.state["cookies"][0]["domain"] = "attacker.example"
        with self.assertRaises(ValueError):
            self.m.import_exports([first, second])
        self.assertEqual(self.commands, [])

    def test_paths_and_duplicate_json_rejected_without_secret_errors(self):
        path = self.base / "bad.json"
        path.write_text('{"cookie":"SECRET", "cookie":"SECOND"}')
        with self.assertRaisesRegex(ValueError, "duplicate_json_field"):
            manager.read_export(path)
        with self.assertRaises(ValueError):
            manager.read_export(self.base / ".." / "bad.json")

    def test_web_cookie_injection_and_conflicting_index_rejected(self):
        for state in (dict(web(), cookie="SAPISID=secret\r\nX-Evil:1"), dict(web(), auth_user="../5"), dict(web(), sapisid="mismatch")):
            with self.subTest(state_keys=list(state)):
                with self.assertRaises(ValueError):
                    self.export(state)
        item = self.export(web()); item.index = "4"
        with self.assertRaisesRegex(ValueError, "account_identity_mismatch"):
            self.m.import_exports([item])

    def test_duplicate_accounts_rejected_and_latest_downloads_selects_one(self):
        a = self.write(self.base / "gemini-auth-u0.json", web(token="old-token"))
        b = self.write(self.base / "gemini-auth-u0 (1).json", web())
        os.utime(a, (time.time()-100, time.time()-100))
        with self.assertRaisesRegex(ValueError, "duplicate_account"):
            manager.select_exports([a, b])
        chosen, rejected = manager.latest_downloads(self.base)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].state, web())
        self.assertEqual(rejected, 0)

    def test_stale_selection_requires_reselect(self):
        item = self.export(web()); item.selected_at -= 901
        with self.assertRaisesRegex(ValueError, "selection_expired"):
            self.m.import_exports([item])
        self.assertEqual(self.commands, [])

    def test_downloads_renewal_excludes_unconfigured_old_identities(self):
        self.write(self.base / "gemini-auth-u0.json", web())
        self.write(self.base / "gemini-auth-u7.json", web("7"))
        self.write(self.base / "aistudio-session-2.json", studio())
        items, skipped = self.m.downloads(self.base)
        self.assertEqual([(i.kind, i.index) for i in items], [("web", "0")])
        self.assertEqual(skipped, 2)

    def test_cross_process_lock_rejects_second_manager(self):
        other = manager.SessionManager(self.root, docker="synthetic")
        with self.m.exclusive():
            with self.assertRaisesRegex(ValueError, "operation_in_progress"):
                with other.exclusive():
                    self.fail("second lock admitted")
        with other.exclusive():
            pass

    def test_known_failed_auth_does_not_spend_generation_or_reset_cooldown(self):
        self.m.studio_status = lambda: {"counts": {"total": 5, "auth_required": 5}}
        with patch.object(self.m, "_probe", side_effect=AssertionError("no generation")):
            result = self.m.test(["ais", "build"], stream=True)
        self.assertEqual([row["result"] for row in result["results"]], ["auth_required"] * 2)
        self.assertEqual(self.commands, [])

    def test_rate_limited_plain_check_does_not_try_stream_or_retry(self):
        with patch.object(self.m, "_probe", return_value={"source": "web", "ok": False, "result": "rate_limited"}) as probe:
            self.m.test(["web"], stream=True)
        probe.assert_called_once_with("web")

    def probe_transport(self, response):
        client = manager.httpx.Client(transport=manager.httpx.MockTransport(lambda req: response))
        with patch.object(manager.httpx, "Client", return_value=client):
            return self.m._probe("web", True)

    def test_http_200_stream_error_is_failure(self):
        response = manager.httpx.Response(200, headers={"X-Mixer-Source": "web", "Content-Type": "text/event-stream"},
            content=b'data: {"error":{"code":"upstream_authentication_failed"}}\n\ndata: [DONE]\n\n')
        result = self.probe_transport(response)
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "auth_required")

    def test_stream_requires_source_text_and_terminal(self):
        content = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        for source in ("web", "ais"):
            row = self.probe_transport(manager.httpx.Response(200, headers={"X-Mixer-Source": source, "Content-Type": "text/event-stream"}, content=content))
            self.assertEqual(row["ok"], source == "web")
        incomplete = self.probe_transport(manager.httpx.Response(200, headers={"X-Mixer-Source": "web", "Content-Type": "text/event-stream"}, content=content.split(b'data: {"choices":[{"delta":{},')[0]))
        self.assertFalse(incomplete["ok"])

    def test_safe_report_omits_response_and_credentials(self):
        client = manager.httpx.Client(transport=manager.httpx.MockTransport(lambda req: manager.httpx.Response(403,
            json={"error": {"code": "upstream_authentication_failed", "message": "SECRET SESSION CONTENT"}})))
        with patch.object(manager.httpx, "Client", return_value=client):
            result = self.m._probe("web")
        encoded = json.dumps(result)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("synthetic-mixer-test-key", encoded)
        self.assertEqual(result["result"], "auth_required")


if __name__ == "__main__":
    unittest.main()
