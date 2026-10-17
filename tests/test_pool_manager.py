"""Lifecycle regressions using synthetic files and fake process handles only."""

from contextlib import ExitStack
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import pool_manager


class FakeProcess:
    """Model termination without starting or stopping an operating-system process."""

    def __init__(self, pid=900001, resist_terminate=False, resist_kill=False):
        self.pid = pid
        self.running = True
        self.resist_terminate = resist_terminate
        self.resist_kill = resist_kill
        self.calls = []

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.calls.append("terminate")
        if not self.resist_terminate:
            self.running = False

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.running:
            raise subprocess.TimeoutExpired("synthetic-process", timeout)
        return 0

    def kill(self):
        self.calls.append("kill")
        if self.resist_kill:
            raise OSError("Synthetic termination failure")
        self.running = False


class PoolManagerTests(unittest.TestCase):
    def setUp(self):
        # Keep fixtures in a configurable work directory. No real account,
        # imported credential, user Downloads file, or real service is accessed.
        work = Path(os.environ.get("MIXER_TEST_WORK_DIR", str(Path(__file__).resolve().parents[1] / "work")))
        work.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(prefix="pool-manager-test-", dir=work)
        self.root = Path(self.fixture.name).resolve()
        if not self.root.is_relative_to(work.resolve()):
            raise AssertionError("Fixture must stay within the workspace work directory")
        self.addCleanup(self.fixture.cleanup)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.profile = self.auth / "config-u0.json"
        self.profile.write_text('{"auth_user":"0","preserve_me":true}\n', encoding="utf-8")
        self.state = self.auth / "pool-state.json"
        self.config = {"host": "127.0.0.1", "port": 49100, "api_keys": ["synthetic-key"]}
        self.pool = {"backends": [{"id": "u0", "url": "http://127.0.0.1:49101"}]}
        self.plan = [
            {"id": "u0", "port": 49101, "script": str(self.root / "backend.py"),
             "config": str(self.profile)},
            {"id": "gateway", "port": 49100, "script": str(self.root / "gateway.py"),
             "config": str(self.root / "config.json")},
        ]
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in {
            "ROOT": self.root, "AUTH": self.auth,
            "POOL_FILE": self.auth / "pool.json", "STATE_FILE": self.state,
            "CONFIG_FILE": self.root / "config.json",
        }.items():
            self.patches.enter_context(mock.patch.object(pool_manager, name, value))
        self.patches.enter_context(mock.patch.object(pool_manager.os, "environ", {}))
        self.load_plan_impl = pool_manager.load_plan
        self.load_plan = self.patches.enter_context(mock.patch.object(pool_manager, "load_plan", return_value=(self.config, self.pool, self.plan)))
        self.port_open = self.patches.enter_context(mock.patch.object(pool_manager, "port_open", return_value=False))
        self.launch = self.patches.enter_context(mock.patch.object(pool_manager.subprocess, "Popen"))
        self.broad_stop = self.patches.enter_context(mock.patch.object(pool_manager, "stop", side_effect=AssertionError("Unexpected broad process stop")))
        self.patches.enter_context(mock.patch.object(pool_manager.subprocess, "run", side_effect=AssertionError("Unexpected real process execution")))

    def test_atomic_json_persists_complete_new_value(self):
        pool_manager.atomic_json(self.profile, {"replacement": True})
        self.assertEqual(json.loads(self.profile.read_text(encoding="utf-8")), {"replacement": True})

    def test_failed_atomic_replace_keeps_original_config(self):
        original = self.profile.read_bytes()
        with mock.patch.object(Path, "replace", side_effect=OSError("Synthetic replace failure")):
            with self.assertRaisesRegex(OSError, "Synthetic replace failure"):
                pool_manager.atomic_json(self.profile, {"replacement": True})
        self.assertEqual(self.profile.read_bytes(), original)

    def test_failed_json_serialization_keeps_original_config(self):
        original = self.profile.read_bytes()
        with self.assertRaises(TypeError):
            pool_manager.atomic_json(self.profile, {"not_json": object()})
        self.assertEqual(self.profile.read_bytes(), original)

    def test_state_write_failure_stops_unrecorded_spawn(self):
        process = FakeProcess()
        self.launch.return_value = process
        with mock.patch.object(pool_manager, "save_state", side_effect=OSError("Synthetic state failure")):
            with self.assertRaisesRegex(OSError, "Synthetic state failure"):
                pool_manager.start()
        self.assertFalse(process.running)
        self.assertIn("terminate", process.calls)
        self.assertIn("wait", process.calls)
        self.launch.assert_called_once()
        self.broad_stop.assert_not_called()

    def test_later_spawn_failure_stops_previously_spawned_child(self):
        process = FakeProcess()
        self.launch.side_effect = [process, OSError("Synthetic launch failure")]
        with self.assertRaisesRegex(OSError, "Synthetic launch failure"):
            pool_manager.start()
        self.assertFalse(process.running)
        self.assertIn("wait", process.calls)
        self.assertFalse(self.state.exists())
        self.broad_stop.assert_not_called()

    def test_termination_timeout_escalates_using_owned_handle(self):
        process = FakeProcess(resist_terminate=True)
        self.launch.return_value = process
        with mock.patch.object(pool_manager, "save_state", side_effect=OSError("Synthetic state failure")):
            with self.assertRaisesRegex(OSError, "Synthetic state failure"):
                pool_manager.start()
        self.assertFalse(process.running)
        self.assertEqual(process.calls, ["terminate", "wait", "kill", "wait"])
        self.broad_stop.assert_not_called()

    def test_failed_child_cleanup_preserves_recorded_state(self):
        process = FakeProcess(resist_terminate=True, resist_kill=True)
        self.launch.side_effect = [process, OSError("Synthetic launch failure")]
        with self.assertRaises((OSError, RuntimeError)):
            pool_manager.start()
        self.assertTrue(process.running)
        self.assertTrue(self.state.exists(), "A surviving child must remain tracked")
        recorded = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(recorded["components"][0]["pid"], process.pid)
        self.broad_stop.assert_not_called()

    def test_occupied_gateway_never_writes_config_or_launches(self):
        original = self.profile.read_bytes()
        self.port_open.return_value = True
        with mock.patch.object(pool_manager, "get_json", return_value={"status": "ok", "mode": "other"}):
            with self.assertRaisesRegex(RuntimeError, "gateway port is occupied"):
                pool_manager.start()
        self.assertEqual(self.profile.read_bytes(), original)
        self.launch.assert_not_called()
        self.broad_stop.assert_not_called()

    def test_lan_gateway_plan_keeps_workers_on_loopback(self):
        self.config["host"] = "0.0.0.0"
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.auth / "pool.json").write_text(json.dumps(self.pool), encoding="utf-8")
        cookie = self.auth / "synthetic-cookie.json"
        cookie.write_text("{}", encoding="utf-8")
        self.profile.write_text(json.dumps({
            "auth_user": "0", "host": "0.0.0.0", "cookie_file": str(cookie),
        }), encoding="utf-8")
        self.load_plan.side_effect = self.load_plan_impl
        self.launch.side_effect = [FakeProcess(900001), FakeProcess(900002)]
        with mock.patch.object(pool_manager, "get_json", return_value={"status": "ok"}) as health:
            with contextlib.redirect_stdout(io.StringIO()):
                pool_manager.start()
        worker = json.loads(self.profile.read_text(encoding="utf-8"))
        gateway = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(worker["host"], "127.0.0.1")
        self.assertEqual(worker["port"], 49101)
        self.assertEqual(gateway["host"], "0.0.0.0")
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in health.call_args_list],
            ["http://127.0.0.1:49101/", "http://127.0.0.1:49100/"],
        )
        self.broad_stop.assert_not_called()

    def test_gateway_plan_rejects_unapproved_bind_hosts(self):
        (self.auth / "pool.json").write_text(json.dumps(self.pool), encoding="utf-8")
        for host in ("localhost", "127.0.0.2", "::1", "example.invalid", None):
            with self.subTest(host=host):
                config = {**self.config, "host": host}
                (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.load_plan_impl()
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
