"""Synthetic account-pool tests; no browser state or upstream network access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError

import httpx

from gemini_web2api import gemini, multimodal, web_accounts
from gemini_web2api.config import CONFIG
from gemini_web2api.providers import ProviderError


class WebAccountPoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "accounts.json"
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("GEMINI_WEB_ACCOUNTS_FILE", None)
        os.environ.pop("GEMINI_WEB_RATE_LIMIT_COOLDOWN_SEC", None)
        self.config = copy.deepcopy(CONFIG)
        self.config.update(auth_user=99, xsrf_token="synthetic-legacy-xsrf", gemini_bl="legacy-bl",
                           proxy=None, request_timeout_sec=5, log_requests=False)
        patcher = mock.patch.dict(CONFIG, self.config, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("_pool", "_pool_path"):
            patcher = mock.patch.object(web_accounts, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_accounts()

    def write_json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def valid_export(index=0):
        return {"cookie": f"SAPISID=synthetic-cookie-{index}; SID=synthetic-session-{index}",
                "sapisid": f"synthetic-cookie-{index}", "auth_user": index,
                "xsrf_token": f"synthetic-xsrf-{index}", "gemini_bl": f"synthetic-bl-{index}"}

    def write_accounts(self, indexes=(0, 4, 5)):
        self.entries = []
        for index in indexes:
            self.write_json(self.root / f"u{index}.json", self.valid_export(index))
            self.entries.append({"id": f"web-u{index}", "auth_user": index,
                                 "cookie_file": f"u{index}.json"})
        self.write_json(self.manifest, {"accounts": self.entries})

    def pool(self):
        pool = web_accounts.WebAccountPool(str(self.manifest))
        self.addCleanup(self.close_clients, pool)
        return pool

    @staticmethod
    def close_clients(pool):
        for account in pool.accounts:
            if account.client is not None:
                account.client.close()

    def invalid(self, path=None):
        with self.assertRaises(ProviderError) as caught:
            web_accounts.WebAccountPool(str(path or self.manifest))
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "web_accounts_invalid"))
        self.assertEqual(str(caught.exception), "Gemini Web account configuration is invalid")
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn("synthetic-cookie", str(caught.exception))

    def test_round_robin_release_and_no_context_leak(self):
        pool = self.pool()
        chosen = []
        for _ in range(6):
            with pool.lease() as account:
                chosen.append(account.id)
                self.assertIs(web_accounts.current_account(), account)
                self.assertTrue(account.busy)
            self.assertFalse(account.busy)
            self.assertIsNone(web_accounts.current_account())
        self.assertEqual(chosen, ["web-u0", "web-u4", "web-u5"] * 2)

    def test_exceptions_and_cancellation_release_slot(self):
        self.write_accounts((0,))
        pool = self.pool()
        for error in (RuntimeError("synthetic failure"), asyncio.CancelledError(), GeneratorExit()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with pool.lease():
                        raise error
                self.assertFalse(pool.accounts[0].busy)
                self.assertIsNone(web_accounts.current_account())
                with pool.lease() as account:
                    self.assertEqual(account.id, "web-u0")

    def test_closed_generator_releases_lease(self):
        self.write_accounts((0,))
        pool = self.pool()
        def stream():
            with pool.lease() as account:
                yield account.id
                yield "terminal"
        response = stream()
        self.assertEqual(next(response), "web-u0")
        self.assertTrue(pool.accounts[0].busy)
        response.close()
        self.assertFalse(pool.accounts[0].busy)
        self.assertIsNone(web_accounts.current_account())

    def test_concurrent_requests_isolate_config_headers_clients_cookies_and_page_cache(self):
        self.write_accounts((0, 4))
        pool = self.pool()
        ready = threading.Barrier(3, timeout=10)
        release = threading.Event()
        baseline = copy.deepcopy(CONFIG)
        def page_tokens():
            return {"at": "page-" + web_accounts.current_account().id}
        def request():
            with pool.lease() as account:
                client = gemini._get_httpx_client()
                client.cookies.set("account-marker", account.id)
                first = multimodal._cached_page_tokens()
                ready.wait()
                if not release.wait(10):
                    raise AssertionError("test synchronization timed out")
                second = multimodal._cached_page_tokens()
                headers = gemini._build_headers()
                self.assertEqual(first, second)
                self.assertEqual(client.cookies.get("account-marker"), account.id)
                self.assertEqual(first["at"], "page-" + account.id)
                self.assertEqual(headers["X-Goog-AuthUser"], str(account.config["auth_user"]))
                self.assertEqual(headers["Cookie"], account.cookie[0])
                self.assertIn(f"/u/{account.config['auth_user']}/", gemini._get_url())
                self.assertIs(gemini._config(), account.config)
                self.assertIs(multimodal._config(), account.config)
                return account.id, id(client), headers["Cookie"], account.config["xsrf_token"]
        with mock.patch.object(multimodal, "_get_page_tokens", side_effect=page_tokens) as fetch:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(request) for _ in range(2)]
                try:
                    ready.wait()
                    self.assertIsNone(web_accounts.current_account())
                    with self.assertRaises(ProviderError) as caught:
                        with pool.lease():
                            self.fail("busy pool unexpectedly admitted a third request")
                    self.assertEqual(caught.exception.status, 503)
                finally:
                    release.set()
                rows = [future.result(timeout=15) for future in futures]
            self.assertEqual(fetch.call_count, 2)
            with pool.lease() as account:
                self.assertEqual(multimodal._cached_page_tokens(), {"at": "page-" + account.id})
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len({row[0] for row in rows}), 2)
        self.assertEqual(len({row[1] for row in rows}), 2)
        self.assertEqual(len({row[2] for row in rows}), 2)
        self.assertEqual(len({row[3] for row in rows}), 2)
        self.assertEqual(CONFIG, baseline)
        self.assertTrue(all(not account.busy for account in pool.accounts))

    def test_config_is_snapshotted_while_legacy_config_stays_live(self):
        pool = self.pool()
        old_timeout = CONFIG["request_timeout_sec"]
        CONFIG["request_timeout_sec"] = old_timeout + 20
        self.assertIs(gemini._config(), CONFIG)
        self.assertEqual(gemini._config()["request_timeout_sec"], old_timeout + 20)
        with pool.lease() as account:
            self.assertEqual(gemini._config()["request_timeout_sec"], old_timeout)
            account.config["xsrf_token"] = "request-local-refresh"
        with pool.lease() as other:
            self.assertNotEqual(other.config["xsrf_token"], "request-local-refresh")
        self.assertEqual(CONFIG["xsrf_token"], "synthetic-legacy-xsrf")

    def test_no_pool_keeps_legacy_cookie_and_configuration(self):
        legacy_file = self.root / "legacy.json"
        self.write_json(legacy_file, {"cookie": "SID=synthetic-legacy", "sapisid": "synthetic-legacy-sap"})
        CONFIG["cookie_file"] = str(legacy_file)
        with mock.patch.dict(gemini._cookie_cache, {"str": "", "sapisid": None, "mtime": 0}, clear=True):
            with web_accounts.lease_account() as account:
                self.assertIsNone(account)
                self.assertIs(web_accounts.request_config(CONFIG), CONFIG)
                self.assertEqual(gemini.load_cookie(), ("SID=synthetic-legacy", "synthetic-legacy-sap"))
                web_accounts.reject_http_status(401)
        self.assertIsNone(web_accounts.get_pool())

    def test_pool_does_not_reload_changed_export_during_service_lifetime(self):
        os.environ["GEMINI_WEB_ACCOUNTS_FILE"] = str(self.manifest)
        pool = web_accounts.get_pool()
        self.addCleanup(self.close_clients, pool)
        original = pool.accounts[0].cookie
        (self.root / "u0.json").write_text("invalid changed export", encoding="utf-8")
        self.assertIs(web_accounts.get_pool(), pool)
        self.assertEqual(web_accounts.get_pool().accounts[0].cookie, original)

    def test_invalid_manifest_shapes_and_unsafe_identifiers(self):
        documents = [[], {}, {"accounts": []}, {"accounts": "private-secret"},
                     {"accounts": self.entries, "extra": "private-secret"},
                     {"accounts": [{"id": "secret@example.invalid", "auth_user": 0, "cookie_file": "u0.json"}]},
                     {"accounts": [{"id": "web-u0", "cookie_file": "u0.json"}]},
                     {"accounts": [{"id": "web-u0", "auth_user": True, "cookie_file": "u0.json"}]},
                     {"accounts": [{"id": "web-u4", "auth_user": 0, "cookie_file": "u0.json"}]},
                     {"accounts": self.entries * 11}]
        for document in documents:
            with self.subTest(document_type=type(document).__name__):
                self.write_json(self.manifest, document)
                self.invalid()

    def test_missing_malformed_and_oversize_manifest(self):
        self.invalid(self.root / "missing-sensitive-name.json")
        for content in (b'{"accounts": secret-cookie', b'\xff', b' ' * (1024 * 1024 + 1)):
            with self.subTest(size=len(content)):
                self.manifest.write_bytes(content)
                self.invalid()
        self.invalid(Path("relative-private-manifest.json"))

    def test_cookie_paths_cannot_escape_or_be_remote(self):
        paths = ["../outside-private.json", "/absolute-private.json", "C:/private.json",
                 "https://example.invalid/private", "sub\\private.json", "", "missing-private.json"]
        for filename in paths:
            with self.subTest(filename=filename):
                self.write_json(self.manifest, {"accounts": [{**self.entries[0], "cookie_file": filename}]})
                self.invalid()

    def test_symlink_cannot_escape_manifest_directory(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside-private.json"
            exported = self.valid_export()
            self.write_json(target, exported)
            self.write_json(self.root / "u0.json", exported)
            self.assertEqual(self.pool().accounts[0].cookie[0], exported["cookie"])
            link = self.root / "linked.json"
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest(f"Symlink creation unavailable: {type(error).__name__}")
            self.write_json(self.manifest, {"accounts": [{**self.entries[0], "cookie_file": "linked.json"}]})
            self.invalid()

    def test_duplicate_accounts_and_duplicate_export_rejected(self):
        for entries in ([self.entries[0], self.entries[0]],
                        [self.entries[0], {**self.entries[1], "cookie_file": "u0.json"}]):
            self.write_json(self.manifest, {"accounts": entries})
            self.invalid()

    def test_export_index_mismatch_and_invalid_cookie_fields(self):
        baseline = self.valid_export()
        self.write_json(self.root / "u0.json", baseline)
        self.assertEqual(self.pool().accounts[0].cookie[0], baseline["cookie"])
        exports = [{"auth_user": 4}, {"auth_user": True},
                   {"cookie": ""}, {"cookie": ["secret"]}, {"cookie": "SID=secret\nInjected: true"},
                   {"sapisid": {"secret": True}}, {"xsrf_token": "private\x00value"},
                   {"gemini_bl": "x" * 513}, []]
        for exported in exports:
            with self.subTest(export_type=type(exported).__name__):
                self.write_json(self.root / "u0.json", {**baseline, **exported} if isinstance(exported, dict) else exported)
                self.invalid()

    def test_export_requires_its_own_index_sapisid_xsrf_and_build(self):
        baseline = self.valid_export()
        self.write_json(self.root / "u0.json", baseline)
        self.assertEqual(self.pool().accounts[0].cookie[0], baseline["cookie"])
        for key in ("cookie", "sapisid", "auth_user", "xsrf_token", "gemini_bl"):
            for remove in (True, False):
                with self.subTest(key=key, removed=remove):
                    exported = dict(baseline)
                    if remove:
                        exported.pop(key)
                    else:
                        exported[key] = ""
                    self.write_json(self.root / "u0.json", exported)
                    self.invalid()

    def test_cookie_fragments_duplicates_and_mismatched_sapisid_rejected(self):
        baseline = self.valid_export()
        self.write_json(self.root / "u0.json", baseline)
        self.assertEqual(self.pool().accounts[0].cookie[0], baseline["cookie"])
        for cookie in ("SAPISID=synthetic-cookie-0; missing-equals",
                       "SAPISID=synthetic-cookie-0; SID=first; SID=second",
                       "SAPISID=synthetic-cookie-0; =missing-name", "SAPISID=actual"):
            with self.subTest(cookie=cookie):
                self.write_json(self.root / "u0.json", {**baseline, "cookie": cookie})
                self.invalid()

    def test_duplicate_json_keys_are_rejected(self):
        self.assertEqual(self.pool().accounts[0].cookie[0], self.valid_export()["cookie"])
        self.manifest.write_text('{"accounts":[],"accounts":' + json.dumps(self.entries) + '}', encoding="utf-8")
        self.invalid()
        self.write_json(self.manifest, {"accounts": self.entries})
        baseline_json = json.dumps(self.valid_export())
        duplicate_cookie = json.dumps("SAPISID=synthetic-cookie-0; SID=another-valid-value")
        (self.root / "u0.json").write_text(
            '{"cookie":' + duplicate_cookie + ',' + baseline_json[1:], encoding="utf-8")
        self.invalid()

    def test_missing_explicit_sapisid_is_rejected_even_with_cookie_value(self):
        self.write_accounts((0,))
        self.write_json(self.root / "u0.json", {"cookie": "SID=synthetic; SAPISID=synthetic-derived",
                                               "auth_user": 0, "xsrf_token": "synthetic-xsrf",
                                               "gemini_bl": "synthetic-build"})
        self.invalid()

    def test_cooldown_configuration_bounds(self):
        for value in ("0", "86401", "-1", "1.5", "NaN", " 10", "１２", ""):
            with self.subTest(value=value):
                os.environ["GEMINI_WEB_RATE_LIMIT_COOLDOWN_SEC"] = value
                self.invalid()
        for value in ("1", "86400"):
            os.environ["GEMINI_WEB_RATE_LIMIT_COOLDOWN_SEC"] = value
            self.assertEqual(self.pool().cooldown, int(value))

    def test_auth_http_status_disables_only_selected_account(self):
        for status in (401, 403):
            with self.subTest(status=status):
                pool = self.pool()
                with self.assertRaises(ProviderError) as caught:
                    with pool.lease() as selected:
                        web_accounts.reject_http_status(status)
                self.assertEqual((caught.exception.status, caught.exception.code), (502, "web_account_auth_failed"))
                self.assertTrue(selected.disabled)
                self.assertFalse(selected.busy)
                self.assertTrue(all(not account.disabled for account in pool.accounts[1:]))
                for _ in range(4):
                    with pool.lease() as other:
                        self.assertNotEqual(other.id, selected.id)

    def test_rate_limit_cooldown_retry_after_and_expiration(self):
        for header, expected in ((None, 300), ("10", 300), ("900", 900), ("999999", 86400),
                                 ("invalid secret header", 300), ("-4", 300)):
            with self.subTest(retry_after=header):
                self.write_accounts((0,))
                pool = self.pool()
                with mock.patch.object(web_accounts.time, "monotonic", return_value=1000):
                    with self.assertRaises(ProviderError) as caught:
                        with pool.lease() as selected:
                            web_accounts.reject_http_status(429, httpx.Headers({"retry-after": header}) if header else {})
                    self.assertEqual((caught.exception.status, caught.exception.code), (429, "web_account_rate_limited"))
                    self.assertFalse(selected.disabled)
                    self.assertEqual(selected.cooldown_until, 1000 + expected)
                    with self.assertRaises(ProviderError):
                        with pool.lease():
                            self.fail("cooling account must not be admitted")
                with mock.patch.object(web_accounts.time, "monotonic", return_value=1000 + expected):
                    with pool.lease() as released:
                        self.assertIs(released, selected)

    def test_http_date_retry_after(self):
        pool = self.pool()
        date = format_datetime(datetime.now(timezone.utc) + timedelta(hours=1), usegmt=True)
        with mock.patch.object(web_accounts.time, "monotonic", return_value=1000):
            with self.assertRaises(ProviderError):
                with pool.lease() as selected:
                    web_accounts.reject_http_status(429, {"Retry-After": date})
        self.assertGreaterEqual(selected.cooldown_until, 4598)
        self.assertLessEqual(selected.cooldown_until, 4600)

    def test_transport_http_errors_are_classified_but_bard_numbers_are_not(self):
        pool = self.pool()
        with pool.lease() as selected:
            for status in (200, 400, 404, 500, "401", None):
                web_accounts.reject_http_status(status)
            for error in (RuntimeError("HTTP 401 synthetic error"),
                          gemini.GeminiUpstreamError("BardErrorInfo [403]"),
                          gemini.GeminiUpstreamError("BardErrorInfo [429]")):
                web_accounts.observe_http_error(error)
            self.assertFalse(selected.disabled)
            self.assertEqual(selected.cooldown_until, 0)
            with self.assertRaises(ProviderError):
                web_accounts.observe_http_error(HTTPError("https://example.invalid", 401, "secret", {}, None))
            self.assertTrue(selected.disabled)
        with pool.lease() as selected:
            request = httpx.Request("POST", "https://example.invalid")
            response = httpx.Response(429, request=request, headers={"Retry-After": "900"})
            with self.assertRaises(ProviderError):
                with web_accounts.capture_http_errors():
                    response.raise_for_status()
            self.assertGreater(selected.cooldown_until, 0)
            self.assertFalse(selected.disabled)


if __name__ == "__main__":
    unittest.main()
