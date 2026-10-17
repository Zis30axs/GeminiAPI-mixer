import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from gemini_web2api import antigravity_quota as quota


NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED = "2026-10-17T11:59:30Z"
RESET = "2026-10-17T22:00:00+08:00"


class AntigravityQuotaTests(unittest.TestCase):
    def account(self, used=12, *, account_id=2, observed=OBSERVED, reset=RESET):
        raw = {
            "updated_at": observed, "email": "private@example.test",
            "credentials": {"refresh_token": "private-test-key"},
            "details": {"access_token": "private-test-key"},
            "antigravity_quota": {"gemini-3-flash": {
                "utilization": used, "reset_time": reset,
                "email": "private@example.test", "extra": "private-test-key",
            }},
        }
        return quota.sanitize_account_usage(account_id, raw, now=NOW)

    def catalog(self, *accounts, observed="2026-10-17T12:00:00Z", status="ok"):
        return {
            "schema_version": 1, "source": quota.SOURCE, "group_id": 4,
            "observed_at": observed, "status": status, "accounts": list(accounts),
        }

    def lookup(self, catalog, model="gemini-3-flash", **kwargs):
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_QUOTA_MAX_AGE_SEC": "180"}):
            return quota.quota_for_model(model, catalog, now=kwargs.get("now", NOW))

    def test_normalizer_keeps_only_quota_fields_and_utc_times(self):
        result = self.account()
        self.assertEqual(result["observed_at"], OBSERVED)
        model = result["models"]["gemini-3-flash"]
        self.assertEqual(model["remaining_percent"], 88)
        self.assertEqual(model["reported_used_percent"], 12)
        self.assertEqual(model["reset_at"], "2026-10-17T14:00:00Z")
        self.assertEqual(model["precision"], "integer-percent-as-reported")
        for secret in ("private", "credentials", "refresh_token", "email", "details", "extra"):
            self.assertNotIn(secret, json.dumps(result))

    def test_integer_percent_is_not_a_request_count_or_exact_fraction(self):
        for used, remaining in ((0, 100), (1, 99), (99, 1)):
            result = self.lookup(self.catalog(self.account(used)))
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["remaining_percent"], remaining)
            self.assertNotIn("remaining_requests", result)
            self.assertNotIn("remaining_tokens", result)

    def test_reported_100_percent_is_always_ambiguous(self):
        account = self.account(100)
        account["models"]["gemini-3-flash"]["remaining_percent"] = 0
        account["models"]["gemini-3-flash"]["evidence_verified_fraction"] = 0
        result = self.lookup(self.catalog(account))
        self.assertEqual(result["status"], "reported_exhausted_or_unknown")
        self.assertEqual(result["reported_used_percent"], 100)
        self.assertIsNone(result["remaining_percent"])
        self.assertNotIn("evidence_verified_fraction", json.dumps(result))

    def test_missing_null_bad_and_nonfinite_utilization_are_unknown(self):
        for used in (None, True, False, "12", -1, 101, 0.5, 12.0,
                     float("nan"), float("inf"), float("-inf"), [], {}):
            with self.subTest(used=used):
                result = self.lookup(self.catalog(self.account(used)))
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["remaining_percent"])
                self.assertIsNone(result["reported_used_percent"])
        result = quota.sanitize_account_usage(2, {"updated_at": OBSERVED,
            "antigravity_quota": {"gemini-3-flash": {}}}, now=NOW)
        self.assertEqual(result["models"]["gemini-3-flash"]["status"], "unknown")

    def test_missing_future_and_naive_source_times_are_unknown(self):
        for observed in (None, "not a timestamp", "2026-10-17T12:00:00",
                         "2026-10-17T12:00:01Z", "2026-10-17T20:00:01+08:00"):
            with self.subTest(observed=observed):
                account = self.account(observed=observed)
                self.assertIsNone(account["observed_at"])
                self.assertIsNone(account["models"]["gemini-3-flash"]["remaining_percent"])
                self.assertEqual(self.lookup(self.catalog(account))["remaining_percent"], None)

    def test_reset_is_not_inferred_from_window_or_naive_time(self):
        for reset in (None, "bad", "2026-10-17T14:00:00", True, 42):
            result = self.lookup(self.catalog(self.account(reset=reset)))
            self.assertIsNone(result["reset_at"])
            self.assertEqual(result["remaining_percent"], 88)

    def test_account_source_cache_age_overrides_fresh_file_collection(self):
        source = "2026-10-17T11:55:00Z"
        result = self.lookup(self.catalog(self.account(observed=source)))
        self.assertTrue(result["stale"])
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["age_seconds"], 300)
        self.assertEqual(result["observed_at"], source)
        self.assertEqual(result["collected_at"], "2026-10-17T12:00:00Z")
        self.assertTrue(result["accounts"][0]["stale"])

    def test_fresh_source_stale_collection_remains_stale(self):
        old = "2026-10-17T11:55:00Z"
        result = self.lookup(self.catalog(self.account(observed=old), observed=old))
        self.assertTrue(result["stale"])
        self.assertEqual(result["status"], "stale")

    def test_custom_max_age_can_cover_upstream_cache(self):
        catalog = self.catalog(self.account(observed="2026-10-17T11:55:00Z"))
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_QUOTA_MAX_AGE_SEC": "600"}):
            result = quota.quota_for_model("gemini-3-flash", catalog, now=NOW)
        self.assertFalse(result["stale"])

    def test_multicount_range_is_not_added_and_earliest_future_reset_is_explicit(self):
        result = self.lookup(self.catalog(
            self.account(12, account_id=2, observed="2026-10-17T11:59:00Z", reset="2026-10-17T14:00:00Z"),
            self.account(25, account_id=3, reset="2026-10-17T21:00:00+08:00"),
        ))
        self.assertEqual(result["status"], "available")
        self.assertIsNone(result["remaining_percent"])
        self.assertEqual(result["remaining_percent_range"], {"min": 75, "max": 88})
        self.assertEqual(result["reset_at"], "2026-10-17T13:00:00Z")
        self.assertFalse(result["aggregate"]["percentages_are_additive"])
        self.assertEqual(result["aggregate"]["reset_method"], "earliest-future-account-reset")
        self.assertEqual(result["observed_at"], "2026-10-17T11:59:00Z")
        self.assertEqual(result["age_seconds"], 60)

    def test_multicount_unknown_is_explicit_and_past_reset_not_next_reset(self):
        result = self.lookup(self.catalog(
            self.account(100, account_id=2, reset="2026-10-17T11:00:00Z"),
            self.account(25, account_id=3, reset=None),
        ))
        self.assertEqual(result["status"], "mixed")
        self.assertIsNone(result["remaining_percent"])
        self.assertEqual(result["remaining_percent_range"], {"min": 75, "max": 75})
        self.assertEqual(result["aggregate"]["known_remaining_account_count"], 1)
        self.assertIsNone(result["reset_at"])

    def test_quota_model_lookup_is_exact_without_suffix_guesses(self):
        catalog = self.catalog(self.account())
        for model in ("ag-gemini-3-flash", "gemini-3-flash-high", "unknown"):
            result = self.lookup(catalog, model)
            self.assertEqual(result["status"], "unknown")
            self.assertIsNone(result["remaining_percent"])
            self.assertEqual(result["accounts"], [])

    def test_duplicate_accounts_are_not_double_counted(self):
        account = self.account()
        result = self.lookup(self.catalog(account, account))
        self.assertEqual(len(result["accounts"]), 1)
        self.assertNotIn("aggregate", result)

    def test_catalog_sanitizes_again_including_nested_fields(self):
        account = self.account()
        account["credentials"] = "private-test-key"
        account["models"]["gemini-3-flash"]["extra"] = "private-test-key"
        catalog = self.catalog(account)
        catalog["email"] = "private@example.test"
        result = self.lookup(catalog)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("credentials", json.dumps(result))

    def test_loader_reads_env_and_bounded_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            path.write_text(json.dumps(self.catalog(self.account())), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANTIGRAVITY_QUOTA_FILE": str(path)}):
                result = quota.load_quota_catalog(now=NOW)
            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["stale"])
            self.assertEqual(self.lookup(result)["remaining_percent"], 88)
            path.write_bytes(b" " * (quota.MAX_BYTES + 1))
            self.assertEqual(quota.load_quota_catalog(path, now=NOW)["status"], "unavailable")

    def test_loader_failures_are_safe_and_do_not_raise_or_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            for content in (b"{private-test-key}", b"[]", b"null", b"NaN", b"Infinity", b"-Infinity",
                            b'{"secret":"private-test-key","number":NaN}', b'{"number":1e999}',
                            b"\xff", b"[" * 2000):
                with self.subTest(content=content[:20]):
                    path.write_bytes(content)
                    result = quota.load_quota_catalog(path, now=NOW)
                    self.assertEqual(result["status"], "unavailable")
                    self.assertNotIn("private", json.dumps(result))
            for candidate in (path.with_name("missing"), directory, "https://example.test/quota.json",
                              "file:///private-test-key", "//server/private-test-key", r"\\server\private-test-key"):
                result = quota.load_quota_catalog(candidate, now=NOW)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("private", json.dumps(result))

    def test_unconfigured_unavailable_and_invalid_snapshots_stay_distinct(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            empty = quota.load_quota_catalog(now=NOW)
        self.assertEqual(empty["status"], "unknown")
        self.assertEqual(self.lookup(empty)["status"], "unknown")
        for raw in ({}, [], None, self.catalog(self.account(), status="unavailable")):
            self.assertEqual(self.lookup(raw)["status"], "unavailable" if raw is not None else "unknown")

    def test_bad_snapshot_observation_and_schema_are_not_accepted(self):
        for observed in (None, "2026-10-17T12:00:01Z", "2026-10-17T12:00:00"):
            self.assertEqual(self.lookup(self.catalog(self.account(), observed=observed))["status"], "unknown")
        for field, value in (("schema_version", True), ("schema_version", 2), ("source", "private-test-key"),
                             ("group_id", True), ("group_id", "4"), ("accounts", {})):
            raw = self.catalog(self.account())
            raw[field] = value
            result = self.lookup(raw)
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("private", json.dumps(result))

    def test_source_observed_after_collection_is_invalid(self):
        raw = self.catalog(self.account(), observed="2026-10-17T11:59:00Z")
        result = self.lookup(raw)
        self.assertIsNone(result["remaining_percent"])
        self.assertTrue(result["stale"])

    def test_permission_error_and_invalid_max_age_do_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            path.write_text(json.dumps(self.catalog(self.account())), encoding="utf-8")
            with mock.patch.object(Path, "open", side_effect=PermissionError("private-test-key")):
                result = quota.load_quota_catalog(path, now=NOW)
            self.assertEqual(result["status"], "unavailable")
            for invalid in ("NaN", "Infinity", "-1", "bad", "0"):
                with mock.patch.dict(os.environ, {"ANTIGRAVITY_QUOTA_MAX_AGE_SEC": invalid}):
                    self.assertEqual(quota.load_quota_catalog(path, now=NOW)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
