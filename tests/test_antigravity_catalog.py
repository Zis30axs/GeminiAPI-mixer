import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gemini_web2api import antigravity_catalog as catalog
from gemini_web2api.providers import ProviderError


class AntigravityCatalogTests(unittest.TestCase):
    def entry(self, *, effort=None, status="verified", effective="gemini-pro", quota="gemini-pro", **updates):
        result = {
            "status": status, "tested_at": "2026-10-17T12:00:00+08:00", "http_status": 200,
            "effective_upstream_model": effective, "quota_model_id": quota,
            "effort": effort, "thinking_control": "upstream-model-variant" if effort else "upstream-controlled",
            "reason": "generation_verified",
        }
        result.update(updates)
        return result

    def evidence(self, **entries):
        return {"schema_version": 1, "source_revision": "1234567abcdef", "models": entries}

    def resolve(self, model, req, evidence, allowed=None):
        return catalog.resolve_model_effort(model, req, list(evidence["models"]) if allowed is None else allowed, evidence)

    def assert_error(self, status, operation, code=None):
        with self.assertRaises(ProviderError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn("private-test-key", str(caught.exception))

    def test_bare_gemini_maps_only_verified_exact_requested_variant(self):
        evidence = self.evidence(**{
            "gemini-pro": self.entry(), "gemini-pro-low": self.entry(effort="low"),
            "gemini-pro-medium": self.entry(effort="medium", status="upstream_declared"),
            "gemini-pro-high": self.entry(effort="high"),
        })
        request = {"messages": [], "reasoning_effort": "low", "reasoning": {"effort": "low", "summary": "auto"}}
        original = copy.deepcopy(request)
        model, clean = self.resolve("gemini-pro", request, evidence)
        self.assertEqual(model, "gemini-pro-low")
        self.assertEqual(clean, {"messages": [], "reasoning": {"summary": "auto"}})
        self.assertEqual(request, original)
        self.assert_error(400, lambda: self.resolve("gemini-pro", {"reasoning_effort": "medium"}, evidence), "unsupported_thinking_level")

    def test_exact_verified_variant_accepts_matching_body_and_removes_controls(self):
        evidence = self.evidence(**{"gemini-pro-low": self.entry(effort="low")})
        for body in ({"reasoning_effort": "low"}, {"reasoning": {"effort": "low"}},
                     {"reasoning_effort": "low", "reasoning": {"effort": "low"}}):
            model, clean = self.resolve("gemini-pro-low", body, evidence)
            self.assertEqual((model, clean), ("gemini-pro-low", {}))

    def test_claude_has_no_fabricated_effort_mapping(self):
        evidence = self.evidence(**{"claude-sonnet": self.entry(effective="claude-sonnet", quota="claude-sonnet")})
        for effort in ("low", "medium", "high"):
            for body in ({"reasoning_effort": effort}, {"reasoning": {"effort": effort}}):
                self.assert_error(400, lambda: self.resolve("claude-sonnet", body, evidence), "unsupported_thinking_level")

    def test_conflicting_body_controls_and_variant_are_rejected(self):
        evidence = self.evidence(**{"gemini-pro-low": self.entry(effort="low")})
        for body in ({"reasoning_effort": "high"}, {"reasoning": {"effort": "medium"}},
                     {"reasoning_effort": "low", "reasoning": {"effort": "high"}}):
            self.assert_error(400, lambda: self.resolve("gemini-pro-low", body, evidence), "conflicting_thinking_level")

    def test_invalid_effort_values_are_rejected_for_both_protocols(self):
        evidence = self.evidence()
        for value in (None, "minimal", "LOW", 0, True, [], {}):
            for body in ({"reasoning_effort": value}, {"reasoning": {"effort": value}}):
                self.assert_error(400, lambda: self.resolve("gemini-pro", body, evidence), "unsupported_thinking_level")
        for value in (None, "low", [], False):
            self.assert_error(400, lambda: self.resolve("gemini-pro", {"reasoning": value}, evidence), "invalid_request")

    def test_unknown_suffix_is_never_capability_evidence_but_legacy_exact_passes(self):
        evidence = self.evidence()
        result = self.resolve("gemini-test-high", {"messages": []}, evidence, ["gemini-test-high"])
        self.assertEqual(result, ("gemini-test-high", {"messages": []}))
        self.assert_error(400, lambda: self.resolve("gemini-test-high", {"reasoning_effort": "high"}, evidence), "unsupported_thinking_level")
        details = catalog.model_details("gemini-test-high", evidence, {})
        self.assertEqual(details["verification"]["status"], "unknown")
        self.assertEqual(details["effective_upstream_model"], "gemini-test-high")
        self.assertEqual(details["quota_model_id"], "gemini-test-high")
        self.assertNotIn("requested_effort", details)

    def test_variant_must_be_allowed_verified_and_match_effort(self):
        for status in ("upstream_declared", "unavailable"):
            evidence = self.evidence(**{"gemini-pro-low": self.entry(effort="low", status=status)})
            self.assert_error(400, lambda: self.resolve("gemini-pro", {"reasoning_effort": "low"}, evidence), "unsupported_thinking_level")
        evidence = self.evidence(**{"gemini-pro-low": self.entry(effort="low")})
        self.assert_error(400, lambda: self.resolve("gemini-pro", {"reasoning_effort": "low"}, evidence, []))
        evidence["models"]["gemini-pro-low"]["effort"] = "high"
        self.assert_error(400, lambda: self.resolve("gemini-pro", {"reasoning_effort": "low"}, evidence))

    def test_model_details_expose_explicit_alias_effort_and_exact_quota_bucket(self):
        evidence = self.evidence(**{"gemini-pro-low": self.entry(effort="low", effective="gemini-pro-high", quota="real-quota-key")})
        with mock.patch.object(catalog, "quota_for_model", return_value={"remaining_percent": None}) as quota:
            details = catalog.model_details("gemini-pro-low", evidence, {"catalog": "sentinel"})
        self.assertEqual(details["effective_upstream_model"], "gemini-pro-high")
        self.assertEqual(details["alias_of"], "gemini-pro-high")
        self.assertEqual(details["quota_model_id"], "real-quota-key")
        self.assertEqual(details["requested_effort"], "low")
        self.assertEqual(details["thinking_control"], "upstream-model-variant")
        self.assertEqual(details["verification"]["tested_at"], "2026-10-17T04:00:00Z")
        self.assertNotIn("budget", json.dumps(details))
        quota.assert_called_once_with("real-quota-key", {"catalog": "sentinel"})

    def test_unavailable_model_returns_explicit_safe_client_error(self):
        for reason, http_status, expected in (("quota_exhausted", 500, 429), ("unknown", 429, 429),
                                               ("unsupported_model", 400, 404), ("unknown", 404, 404),
                                               ("unknown", 403, 403), ("upstream_unavailable", 503, 400)):
            evidence = self.evidence(**{"gemini-pro": self.entry(status="unavailable", reason=reason, http_status=http_status)})
            self.assert_error(expected, lambda: self.resolve("gemini-pro", {}, evidence))

    def test_sensitive_fields_and_raw_reason_are_stripped(self):
        entry = self.entry()
        entry.update(credentials={"token": "private-test-key"}, email="private@example.test", reason="private-test-key")
        evidence = self.evidence(**{"gemini-pro": entry})
        evidence["admin_api_key"] = "private-test-key"
        result = catalog.model_details("gemini-pro", evidence, {})
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(result["verification"]["reason"], "unknown")

    def test_response_model_is_safe_metadata_and_does_not_merge_quota_buckets(self):
        evidence = self.evidence(**{
            "gemini-pro-low": self.entry(effort="low", quota="gemini-pro-low", upstream_response_model="gemini-flash-n"),
            "gemini-pro-high": self.entry(effort="high", quota="gemini-pro-high", upstream_response_model="gemini-flash-n"),
        })
        low = catalog.model_details("gemini-pro-low", evidence, {})
        high = catalog.model_details("gemini-pro-high", evidence, {})
        self.assertEqual(low["upstream_response_model"], high["upstream_response_model"])
        self.assertEqual(low["quota_model_id"], "gemini-pro-low")
        self.assertEqual(high["quota_model_id"], "gemini-pro-high")
        evidence["models"]["gemini-pro-low"]["upstream_response_model"] = "private token with spaces"
        self.assertNotIn("upstream_response_model", catalog.model_details("gemini-pro-low", evidence, {}))

    def test_future_verified_evidence_cannot_enable_an_effort_variant(self):
        evidence = self.evidence(**{"gemini-pro-low": self.entry(effort="low", tested_at="2999-01-01T00:00:00Z")})
        self.assertEqual(catalog.model_details("gemini-pro-low", evidence, {})["verification"]["status"], "unknown")
        self.assert_error(400, lambda: self.resolve("gemini-pro", {"reasoning_effort": "low"}, evidence), "unsupported_thinking_level")

    def test_bad_entry_types_do_not_become_verified(self):
        for field, value in (("status", "ready"), ("status", []), ("thinking_control", {}),
                             ("http_status", True), ("http_status", "200"),
                             ("tested_at", "2026-10-17T12:00:00"), ("http_status", 429),
                             ("effort", "minimal"), ("thinking_control", "budget"),
                             ("quota_model_id", "private token with spaces")):
            evidence = self.evidence(**{"gemini-pro": self.entry(**{field: value})})
            self.assertEqual(catalog.model_details("gemini-pro", evidence, {})["verification"]["status"], "unknown")

    def test_file_loader_is_bounded_local_and_uses_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            evidence = self.evidence(**{"gemini-pro-low": self.entry(effort="low")})
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANTIGRAVITY_MODEL_EVIDENCE_FILE": str(path)}):
                self.assertEqual(catalog.read_model_evidence()["models"]["gemini-pro-low"]["effort"], "low")
            path.write_bytes(b" " * (catalog.MAX_BYTES + 1))
            self.assertEqual(catalog.read_model_evidence(path)["models"], {})
            for content in (b"[]", b"null", b"{private-test-key}", b'{"float":NaN}', b'{"float":1e999}', b"\xff", b"[" * 2000):
                path.write_bytes(content)
                self.assertEqual(catalog.read_model_evidence(path)["models"], {})
            for candidate in (directory, path.with_name("missing"), "https://example.test/evidence.json", "file:///secret",
                              "//server/secret", r"\\server\secret"):
                self.assertEqual(catalog.read_model_evidence(candidate)["models"], {})
            with mock.patch.object(Path, "open", side_effect=PermissionError("private-test-key")):
                self.assertEqual(catalog.read_model_evidence(path)["models"], {})

    def test_no_evidence_file_is_unknown_and_does_not_raise(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(catalog.read_model_evidence()["models"], {})
            result = catalog.model_details("unknown-model")
            self.assertEqual(result["verification"]["status"], "unknown")
            self.assertEqual(catalog.resolve_model_effort("gemini-test-high", {}, ["gemini-test-high"]), ("gemini-test-high", {}))


if __name__ == "__main__":
    unittest.main()
