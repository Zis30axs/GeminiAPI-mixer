"""Synthetic-only regression tests for the explicit local session importer."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "import_aistudio_session.py"
SPEC = importlib.util.spec_from_file_location("session_import", SCRIPT)
session_import = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(session_import)


def state_fixture(email="owner@example.com", index="5"):
    return {
        "cookies": [
            {"name": name, "value": "synthetic-session-value", "domain": ".google.com", "path": "/",
             "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}
            for name in sorted(session_import.REQUIRED)
        ],
        "origins": [],
        "aistudio2api": {"source": {"browser": "mixer-import", "email": email, "auth_user": index}},
    }


class SessionImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mixer-session-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "export.json"
        self.auth = self.base / "private-auth"
        self.write_state()

    def write_state(self, state=None):
        self.source.write_text(json.dumps(state_fixture() if state is None else state), encoding="utf-8")

    def account_path(self):
        return next(path for path in self.auth.iterdir() if path.is_dir())

    def test_import_preserves_selected_identity_and_protects_files(self):
        self.assertEqual(session_import.import_session(self.source, self.auth), 3)
        account = self.account_path()
        self.assertEqual(account.name, "owner@example.com")
        self.assertEqual(json.loads((account / "account.json").read_text())["label"], "owner@example.com")
        state = json.loads((account / "storage-state.json").read_text())
        self.assertEqual(state["aistudio2api"]["source"]["auth_user"], "5")
        self.assertEqual(state["aistudio2api"]["source"]["email"], account.name)
        self.assertEqual(state, state_fixture())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(account.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((account / "storage-state.json").stat().st_mode), 0o600)
        else:
            self.assert_windows_dacl(account / "storage-state.json")

    def assert_windows_dacl(self, target):
        import ctypes
        from ctypes import wintypes
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        security = ctypes.c_void_p()
        advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        encoded = wintypes.LPWSTR()
        try:
            self.assertEqual(advapi.GetNamedSecurityInfoW(str(target), 1, 4, None, None, None, None, ctypes.byref(security)), 0)
            self.assertTrue(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(security, 1, 4, ctypes.byref(encoded), None))
            dacl = encoded.value
            self.assertTrue(dacl.startswith("D:P"), "DACL must disable inherited permissions")
            self.assertEqual(dacl.count("(A;"), 2)
            self.assertIn(";;;SY)", dacl)
            self.assertNotIn(";;;WD)", dacl)
            self.assertNotIn(";;;BU)", dacl)
        finally:
            if encoded:
                kernel.LocalFree(encoded)
            if security:
                kernel.LocalFree(security)

    def test_multiple_accounts_keep_independent_sessions(self):
        session_import.import_session(self.source, self.auth)
        self.write_state(state_fixture("second@example.com", "2"))
        session_import.import_session(self.source, self.auth)
        accounts = [path for path in self.auth.iterdir() if path.is_dir()]
        self.assertEqual(len(accounts), 2)
        self.assertEqual({json.loads((path / "storage-state.json").read_text())["aistudio2api"]["source"]["auth_user"] for path in accounts}, {"5", "2"})

    def test_replace_is_explicit_and_keeps_config_and_cooldowns(self):
        session_import.import_session(self.source, self.auth)
        account = self.account_path()
        with self.assertRaisesRegex(session_import.ImportRejected, "account_exists"):
            session_import.import_session(self.source, self.auth)
        config = json.loads((account / "account.json").read_text())
        config["enabled"] = False
        config["proxy"] = "http://127.0.0.1:8888"
        (account / "account.json").write_text(json.dumps(config))
        (account / "runtime-state.json").write_text('{"cooldowns":{"test":"preserved"}}')
        old = state_fixture()
        old["cookies"][0]["expires"] = time.time() - 30
        old["cookies"][0]["value"] = ""
        old["cookies"].pop()
        old["cookies"].append({"name": "upstream-expired", "value": "", "domain": "accounts.google.com"})
        old["origins"] = [{"origin": "https://aistudio.google.com", "localStorage": []}]
        (account / "storage-state.json").write_text(json.dumps(old))
        replacement = state_fixture(index="1")
        replacement["cookies"][0]["value"] = "synthetic-renewed-value"
        self.write_state(replacement)
        session_import.import_session(self.source, self.auth, replace=True)
        self.assertEqual(json.loads((account / "account.json").read_text()), config)
        self.assertEqual((account / "runtime-state.json").read_text(), '{"cooldowns":{"test":"preserved"}}')
        self.assertEqual(json.loads((account / "storage-state.json").read_text()), replacement)

    def test_replace_rejects_changed_owner_or_corrupted_prior_json(self):
        session_import.import_session(self.source, self.auth)
        account = self.account_path()
        old = state_fixture(email="another@example.com")
        target = account / "storage-state.json"
        target.write_text(json.dumps(old))
        with self.assertRaisesRegex(session_import.ImportRejected, "account_identity_mismatch"):
            session_import.import_session(self.source, self.auth, replace=True)
        self.assertEqual(json.loads(target.read_text()), old)
        target.write_text('{"aistudio2api":{},"aistudio2api":{}}')
        with self.assertRaisesRegex(session_import.ImportRejected, "duplicate_json_field"):
            session_import.import_session(self.source, self.auth, replace=True)

    def test_rejects_cookie_and_identity_corruption_before_creating_target(self):
        mutations = [
            lambda value: value["cookies"][0].update(domain=".evilgoogle.com"),
            lambda value: value["cookies"][0].update(name="bad;name"),
            lambda value: value["cookies"][0].update(value="bad\r\nheader"),
            lambda value: value["cookies"][0].update(expires=0),
            lambda value: value["cookies"][0].update(expires=float("nan")),
            lambda value: value["cookies"][0].update(expires=True),
            lambda value: value["cookies"][0].update(path="relative"),
            lambda value: value["cookies"][0].update(secure="true"),
            lambda value: value["cookies"][0].update(partitionKey="unsupported"),
            lambda value: value["cookies"].append(copy.deepcopy(value["cookies"][0])),
            lambda value: value["cookies"].pop(),
            lambda value: value["aistudio2api"]["source"].update(email="../owner@example.com"),
            lambda value: value["aistudio2api"]["source"].update(email="owner..name@example.com"),
            lambda value: value["aistudio2api"]["source"].update(email="owner.@example.com"),
            lambda value: value["aistudio2api"]["source"].update(email="owner:stream@example.com"),
            lambda value: value["aistudio2api"]["source"].update(email="owner\\name@example.com"),
            lambda value: value["aistudio2api"]["source"].update(auth_user="05"),
            lambda value: value["aistudio2api"]["source"].update(auth_user=5),
            lambda value: value["aistudio2api"]["source"].update(browser="chrome"),
            lambda value: value["aistudio2api"].update(oauth={"refresh_token": "synthetic-must-not-import"}),
            lambda value: value.update(origins=[{"origin": "https://example.com"}]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = state_fixture()
                mutate(value)
                self.write_state(value)
                with self.assertRaises(session_import.ImportRejected):
                    session_import.import_session(self.source, self.auth)
                self.assertFalse(self.auth.exists())

    def test_rejects_oversized_duplicate_and_malformed_json(self):
        for content in [" " * (session_import.MAX_BYTES + 1), '{"cookies":[],"cookies":[]}', '{"secret":"synthetic-value",']:
            with self.subTest(size=len(content)):
                self.source.write_text(content)
                with self.assertRaises(session_import.ImportRejected):
                    session_import.import_session(self.source, self.auth)
                self.assertFalse(self.auth.exists())

    def test_rejects_relative_traversal_root_and_populated_destination(self):
        for path in ["relative.json", str(self.base / ".." / "export.json"), self.base.anchor, "//server/share/session.json"]:
            with self.subTest(path=path):
                with self.assertRaises(session_import.ImportRejected):
                    session_import.checked_path(path)
        self.auth.mkdir()
        (self.auth / "unrelated.txt").write_text("keep")
        with self.assertRaisesRegex(session_import.ImportRejected, "dedicated_private_directory"):
            session_import.import_session(self.source, self.auth)
        self.assertEqual((self.auth / "unrelated.txt").read_text(), "keep")

    def test_rejects_symlinks_in_source_or_destination_ancestors(self):
        link = self.base / "link"
        try:
            link.symlink_to(self.source)
        except OSError:
            self.skipTest("Host does not permit symlink creation")
        with self.assertRaisesRegex(session_import.ImportRejected, "symlink_or_reparse"):
            session_import.import_session(link, self.auth)
        directory_link = self.base / "linked-directory"
        real = self.base / "real"
        real.mkdir()
        directory_link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(session_import.ImportRejected, "symlink_or_reparse"):
            session_import.import_session(self.source, directory_link / "auth")

    def test_cli_output_contains_only_status_and_count(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = session_import.main(["--source", str(self.source), "--auth-dir", str(self.auth)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "imported", "cookie_count": 3})
        self.assertNotIn("owner", output.getvalue())
        self.assertNotIn("synthetic", output.getvalue())
        self.source.write_text('{"incomplete":"synthetic-secret"')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = session_import.main(["--source", str(self.source), "--auth-dir", str(self.auth)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"status": "rejected", "reason": "invalid_json"})


if __name__ == "__main__":
    unittest.main()
