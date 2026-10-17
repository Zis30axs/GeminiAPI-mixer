"""The privileged exporter never shares credentials with its snapshot consumer."""
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from gemini_web2api import quota_exporter as exporter


EMAIL = "synthetic-admin@example.invalid"
PASSWORD = "synthetic-private-password"
TOKEN = "synthetic-private-access-token"
REFRESH = "synthetic-private-refresh-token"


def ok(data):
    return httpx.Response(200, json={"code": 0, "message": "success", "data": data})


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.credentials = Path(self.directory.name) / "private-login.json"
        self.credentials.write_text(json.dumps({"email": EMAIL, "password": PASSWORD}), encoding="utf-8")
        self.output = Path(self.directory.name) / "shared" / "snapshot.json"
        self.config = exporter.ExporterConfig(
            base_url="https://sub2api.example.invalid", credentials_file=str(self.credentials),
            output_file=str(self.output), group_id=4, account_ids=(2,),
        )
        self.requests = []
        self.group = {"id": 4, "platform": "antigravity", "fallback_group_id": None,
                      "fallback_group_id_on_invalid_request": None}
        self.accounts = [{"id": 2, "platform": "antigravity", "group_ids": [3, 4]}]
        self.usage = {
            "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            "antigravity_quota": {
                "gemini-3-flash": {"utilization": 20, "reset_time": "2030-01-01T00:00:00Z"},
                "claude-sonnet": {"utilization": 100, "reset_time": "2030-01-01T01:00:00Z"},
            },
            "credentials": {"access_token": TOKEN}, "email": EMAIL,
            "password": PASSWORD, "refresh_token": REFRESH,
        }

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/auth/login":
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.content), {"email": EMAIL, "password": PASSWORD})
            self.assertNotIn("authorization", request.headers)
            return ok({"access_token": TOKEN, "refresh_token": REFRESH, "user": {"email": EMAIL}})
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer " + TOKEN)
        if path == "/api/v1/admin/groups/4":
            return ok(self.group)
        if path == "/api/v1/admin/accounts":
            self.assertEqual(request.url.params["platform"], "antigravity")
            self.assertEqual(request.url.params["group"], "4")
            self.assertEqual(request.url.params["lite"], "true")
            return ok({"items": self.accounts, "page": 1, "page_size": 100, "pages": 1})
        if path == "/api/v1/admin/accounts/2/usage":
            self.assertEqual(request.url.params["source"], "active")
            self.assertEqual(request.url.params["force"], "false")
            return ok(self.usage)
        raise AssertionError("Unexpected route")

    def collect(self, handler=None, config=None):
        return exporter.collect_once(config or self.config, transport=httpx.MockTransport(handler or self.handler))

    def assert_unavailable(self, result, code=None):
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["accounts"], [])
        if code:
            self.assertEqual(result["error_code"], code)
        encoded = json.dumps(result)
        for secret in (EMAIL, PASSWORD, TOKEN, REFRESH):
            self.assertNotIn(secret, encoded)

    def test_collects_only_sanitized_quota_and_reset_without_secrets(self):
        result = self.collect()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["source"], "sub2api-account-quota")
        self.assertEqual(result["group_id"], 4)
        account = result["accounts"][0]
        self.assertEqual(account["account_id"], 2)
        self.assertEqual(account["models"]["gemini-3-flash"]["remaining_percent"], 80)
        self.assertEqual(account["models"]["gemini-3-flash"]["reset_at"], "2030-01-01T00:00:00Z")
        self.assertIsNone(account["models"]["claude-sonnet"]["remaining_percent"])
        self.assertEqual(account["models"]["claude-sonnet"]["status"], "reported_exhausted_or_unknown")
        for secret in (EMAIL, PASSWORD, TOKEN, REFRESH):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual([request.method for request in self.requests], ["POST", "GET", "GET", "GET"])
        self.assertEqual(sum(request.method == "POST" for request in self.requests), 1)
        self.assertNotIn("refresh", " ".join(request.url.path for request in self.requests))

    def test_credentials_are_read_only_from_dedicated_exact_json(self):
        for value in ({"email": EMAIL}, {"email": EMAIL, "password": PASSWORD, "oauth_token": TOKEN},
                      {"email": EMAIL, "password": ""}, {"email": EMAIL, "password": "bad\r\nheader"}):
            self.credentials.write_text(json.dumps(value), encoding="utf-8")
            self.assert_unavailable(self.collect(), "credentials_unavailable")
        self.assertEqual(self.requests, [])

    def test_missing_oversized_or_nonregular_credentials_fail_without_network(self):
        self.credentials.unlink()
        self.assert_unavailable(self.collect(), "credentials_unavailable")
        self.credentials.write_bytes(b"x" * (16 * 1024 + 1))
        self.assert_unavailable(self.collect(), "credentials_unavailable")
        self.credentials.unlink()
        self.credentials.mkdir()
        self.assert_unavailable(self.collect(), "credentials_unavailable")
        self.assertEqual(self.requests, [])

    def test_authentication_failure_never_retries_or_relays_raw_error(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401, json={"code": 401, "message": PASSWORD + TOKEN})
        self.assert_unavailable(self.collect(handler), "authentication_failed")
        self.assertEqual(len(calls), 1)

    def test_2fa_and_captcha_require_operator_and_are_not_bypassed(self):
        for data in ({"requires_2fa": True, "temp_token": TOKEN}, {"requires_captcha": True},
                     {"code": 400, "reason": "TURNSTILE_REQUIRED", "message": PASSWORD}):
            calls = []
            def handler(request, data=data):
                calls.append(request)
                return ok(data) if "code" not in data else httpx.Response(400, json=data)
            self.assert_unavailable(self.collect(handler), "interactive_auth_required")
            self.assertEqual(len(calls), 1)

    def test_invalid_access_token_and_unsuccessful_envelope_are_refused(self):
        for response in (ok({"access_token": "token\r\ninjected"}), ok({"refresh_token": REFRESH}),
                         httpx.Response(200, json={"code": 401, "data": {"access_token": TOKEN}})):
            result = self.collect(lambda request, response=response: response)
            self.assert_unavailable(result)

    def test_wrong_group_platform_or_fallback_cannot_export(self):
        for field, value in (("platform", "gemini"), ("id", 3), ("fallback_group_id", 3),
                             ("fallback_group_id_on_invalid_request", 3)):
            original = self.group.copy()
            self.group[field] = value
            self.assert_unavailable(self.collect(), "group_scope_mismatch")
            self.group = original
        self.assertFalse(any("/usage" in request.url.path for request in self.requests))

    def test_account_filter_is_not_trusted_and_allowlist_is_applied(self):
        self.accounts.insert(0, {"id": 77, "platform": "openai", "group_ids": [99], "credentials": TOKEN})
        result = self.collect()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([account["account_id"] for account in result["accounts"]], [2])
        self.assertFalse(any("/77/" in request.url.path for request in self.requests))

    def test_allowlisted_account_wrong_platform_or_membership_fails_closed(self):
        for account in ({"id": 2, "platform": "gemini", "group_ids": [4]},
                        {"id": 2, "platform": "antigravity", "group_ids": [3]},
                        {"id": 2, "platform": "antigravity"}):
            self.accounts = [account]
            self.assert_unavailable(self.collect(), "account_scope_mismatch")
        self.assertFalse(any("/usage" in request.url.path for request in self.requests))

    def test_missing_allowlisted_account_is_unavailable(self):
        self.accounts = [{"id": 3, "platform": "antigravity", "group_ids": [4]}]
        self.assert_unavailable(self.collect(), "account_scope_unavailable")

    def test_pagination_can_find_allowlisted_account_but_is_bounded(self):
        pages = []
        def handler(request):
            if request.url.path != "/api/v1/admin/accounts":
                return self.handler(request)
            page = int(request.url.params["page"])
            pages.append(page)
            return ok({"items": self.accounts if page == 2 else [{"id": 99}], "pages": 2})
        self.assertEqual(self.collect(handler)["status"], "ok")
        self.assertEqual(pages, [1, 2])
        pages.clear()
        def endless(request):
            if request.url.path != "/api/v1/admin/accounts":
                return self.handler(request)
            pages.append(int(request.url.params["page"]))
            return ok({"items": [{"id": 99}], "pages": 1000})
        self.assert_unavailable(self.collect(endless), "account_scope_unavailable")
        self.assertEqual(len(pages), 8)

    def test_upstream_http_200_degraded_usage_is_unavailable(self):
        self.usage["error_code"] = "unauthenticated"
        self.usage["error"] = PASSWORD
        self.assert_unavailable(self.collect(), "upstream_rejected")

    def test_redirect_never_receives_login_secret(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": "https://attacker.invalid/collect"})
        self.assert_unavailable(self.collect(handler), "redirect_rejected")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, "sub2api.example.invalid")

    def test_ambient_proxy_redirect_and_tls_client_options_are_safe(self):
        original = httpx.Client
        def factory(*args, **kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            self.assertTrue(kwargs["verify"])
            return original(*args, **kwargs)
        with mock.patch.object(exporter.httpx, "Client", side_effect=factory):
            self.assertEqual(self.collect()["status"], "ok")

    def test_login_cookie_is_discarded_not_replayed_as_refresh_credentials(self):
        def handler(request):
            response = self.handler(request)
            if request.url.path.endswith("/login"):
                response.headers["Set-Cookie"] = "refresh_token=" + REFRESH + "; Path=/"
            else:
                self.assertNotIn("cookie", request.headers)
            return response
        self.assertEqual(self.collect(handler)["status"], "ok")

    def test_response_size_json_and_connection_failures_are_safe(self):
        self.assert_unavailable(self.collect(lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))), "upstream_response_too_large")
        self.assert_unavailable(self.collect(lambda request: httpx.Response(200, content=PASSWORD.encode())), "upstream_invalid_response")
        def timeout(request):
            raise httpx.ReadTimeout(PASSWORD + TOKEN)
        self.assert_unavailable(self.collect(timeout), "upstream_timeout")
        def failure(request):
            raise httpx.ConnectError(PASSWORD + TOKEN)
        self.assert_unavailable(self.collect(failure), "upstream_connection_error")

    def test_total_deadline_is_checked_before_returning_data(self):
        with mock.patch.object(exporter.time, "monotonic", side_effect=[0, 1, 46]):
            self.assert_unavailable(self.collect(), "upstream_timeout")

    def test_config_requires_explicit_http_and_bounded_operator_scope(self):
        base = {"SUB2API_QUOTA_BASE_URL": "https://sub2api.example.invalid",
                "SUB2API_QUOTA_CREDENTIALS_FILE": str(self.credentials)}
        valid = exporter.ExporterConfig.from_env(base)
        self.assertEqual(valid.group_id, 4)
        self.assertEqual(valid.account_ids, (2,))
        self.assertEqual(valid.interval_sec, 60)
        for key, value in (("SUB2API_QUOTA_BASE_URL", "http://127.0.0.1:18081"),
                           ("SUB2API_QUOTA_BASE_URL", "https://user:secret@host.invalid"),
                           ("SUB2API_QUOTA_BASE_URL", "https://host.invalid/?key=secret"),
                           ("SUB2API_QUOTA_BASE_URL", "https://host.invalid/../admin"),
                           ("SUB2API_QUOTA_ACCOUNT_IDS", "1,2,3,4,5,6,7,8,9"),
                           ("SUB2API_QUOTA_ACCOUNT_IDS", "2,2"),
                           ("SUB2API_QUOTA_ACCOUNT_IDS", "2/../3"),
                           ("SUB2API_QUOTA_GROUP_ID", "0"),
                           ("SUB2API_QUOTA_INTERVAL_SEC", "29"),
                           ("SUB2API_QUOTA_INTERVAL_SEC", "3601"),
                           ("SUB2API_QUOTA_INTERVAL_SEC", "NaN")):
            with self.subTest(key=key, value=value), self.assertRaises(exporter.ExporterError) as raised:
                exporter.ExporterConfig.from_env(dict(base, **{key: value}))
            self.assertEqual(raised.exception.code, "configuration_invalid")
        valid = exporter.ExporterConfig.from_env(dict(base, SUB2API_QUOTA_BASE_URL="http://127.0.0.1:18081", SUB2API_QUOTA_ALLOW_HTTP="true"))
        self.assertEqual(valid.base_url, "http://127.0.0.1:18081")

    def test_output_cannot_replace_private_credentials(self):
        with self.assertRaises(exporter.ExporterError):
            exporter.ExporterConfig(base_url="https://sub2api.example.invalid", credentials_file=str(self.credentials), output_file=str(self.credentials))

    def test_atomic_writer_replaces_previous_fresh_success_with_unavailable(self):
        exporter.write_snapshot(self.collect(), self.output)
        result = self.collect(lambda request: httpx.Response(403, content=b"forbidden"))
        exporter.write_snapshot(result, self.output)
        self.assert_unavailable(json.loads(self.output.read_text(encoding="utf-8")), "authentication_failed")
        self.assertEqual(list(self.output.parent.glob(".quota-*.tmp")), [])

    def test_atomic_replace_failure_preserves_old_file_and_never_leaks_path(self):
        exporter.write_snapshot(self.collect(), self.output)
        old = self.output.read_bytes()
        with mock.patch.object(exporter.os, "replace", side_effect=OSError(PASSWORD + str(self.credentials))):
            with self.assertRaises(exporter.ExporterError) as raised:
                exporter.write_snapshot({"status": "unavailable"}, self.output)
        self.assertEqual(str(raised.exception), "snapshot_write_failed")
        self.assertEqual(self.output.read_bytes(), old)
        self.assertEqual(list(self.output.parent.glob(".quota-*.tmp")), [])

    def test_loop_logs_only_fixed_status_and_error_code(self):
        stop = threading.Event()
        result = self.collect()
        def write(snapshot, path):
            stop.set()
        with mock.patch.object(exporter, "collect_once", return_value=result), mock.patch.object(exporter, "write_snapshot", side_effect=write), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            exporter.run(self.config, stop_event=stop)
            text = output.getvalue()
        self.assertIn("status=ok", text)
        for secret in (EMAIL, PASSWORD, TOKEN, REFRESH):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
