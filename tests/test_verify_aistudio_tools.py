"""Synthetic tool acceptance/evidence tests; no real sessions or inference."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import test_session_manager as fixtures
manager, secure, TOOLS = fixtures.manager, fixtures.secure, fixtures.TOOLS

sys.path.insert(0, str(TOOLS))
try:
    SPEC = importlib.util.spec_from_file_location("verify_aistudio_tools", TOOLS / "verify_aistudio_tools.py")
    tools = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(tools)
finally:
    sys.path.pop(0)


class ToolTests(__import__('unittest').TestCase):
    setUp = fixtures.ManagerTests.setUp
    write = fixtures.ManagerTests.write

    def evidence(self):
        self.path = self.runtime / "evidence.json"
        self.old = {"version": 1, "backend_base_url": "http://aistudio-http:2048",
            "verified_at": (datetime.now(timezone.utc)-timedelta(hours=4)).isoformat(),
            "models": {"ais/gemini-3.8-flash": {"text": True, "streaming": True, "tools": False},
                       "build/gemini-3.8-flash": {"text": True, "streaming": True, "tools": False},
                       "ais/gemini-4-argon": {"text": True, "tools": False, "thinking": {"high": {"thinkingLevel": "HIGH"}}}}}
        self.write(self.path, self.old)
        with (self.root / ".env").open("a") as f:
            f.write("AISTUDIO_EVIDENCE_FILE='" + str(self.path) + "'\n")
        self.m.studio_status = lambda: {"ready": True}

    def run_verification(self, results, **kwargs):
        with patch.object(tools, "declared", return_value=True), patch.object(tools, "check", side_effect=results):
            return tools.verify(self.m, ["ais", "build"], **kwargs)

    def test_success_enables_only_proven_channels_without_refreshing_other_evidence(self):
        self.evidence()
        rows = [{"source": s, "stream": stream, "ok": True} for s in ("ais", "build") for stream in (False, True)]
        report = self.run_verification(rows, apply=True)
        new = secure.read_local_json(self.path)
        self.assertEqual(report["enabled_sources"], ["ais", "build"])
        self.assertEqual(new["verified_at"], self.old["verified_at"])
        self.assertEqual(new["models"]["ais/gemini-4-argon"], self.old["models"]["ais/gemini-4-argon"])
        self.assertTrue(new["models"]["ais/gemini-3.8-flash"]["tools"])

    def test_rate_limit_stops_without_enabling_or_switching_channel(self):
        self.evidence()
        report = self.run_verification([{"source": "ais", "ok": False, "code": "upstream_rate_limited"}], apply=True)
        self.assertEqual(report["enabled_sources"], [])
        self.assertEqual(secure.read_local_json(self.path), self.old)
        self.assertEqual(self.commands, [])

    def test_plain_only_is_insufficient_to_enable_streaming_client_capability(self):
        self.evidence()
        report = self.run_verification([{"ok": True}, {"ok": True}], stream=False, apply=True)
        self.assertEqual(report["enabled_sources"], [])
        self.assertEqual(secure.read_local_json(self.path), self.old)

    def test_missing_backend_declaration_never_generates_or_enables(self):
        self.evidence()
        with patch.object(tools, "declared", return_value=False), patch.object(tools, "check", side_effect=AssertionError("no inference")):
            report = tools.verify(self.m, ["ais"], apply=True)
        self.assertEqual(report["enabled_sources"], [])

    def test_exact_function_call_and_real_function_result_are_required(self):
        call = {"candidates": [{"content": {"parts": [{"functionCall": {"name": tools.NAME, "args": {"value": tools.MARKER}}}]}, "finishReason": "STOP"}]}
        answer = {"candidates": [{"content": {"parts": [{"text": tools.MARKER}]}, "finishReason": "STOP"}]}
        with patch.object(tools, "request", side_effect=[[call], [answer]]):
            self.assertTrue(tools.check({}, "ais", True)["ok"])
        broken = copy.deepcopy(call); broken["candidates"][0]["content"]["parts"][0]["functionCall"]["name"] = "other"
        with patch.object(tools, "request", return_value=[broken]):
            self.assertFalse(tools.check({}, "ais", True)["ok"])
        broken = copy.deepcopy(answer); broken["candidates"][0]["finishReason"] = "MAX_TOKENS"
        with patch.object(tools, "request", side_effect=[[call], [broken]]):
            self.assertFalse(tools.check({}, "ais", True)["ok"])
