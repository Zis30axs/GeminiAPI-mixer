"""Account selection spans the full legacy protocol handler, using synthetic sessions."""
import base64
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

from gemini_web2api import gemini, server, web_accounts
from gemini_web2api.config import CONFIG
from gemini_web2api.mixer import MixerHandler
from gemini_web2api.server import ThreadedServer


class WebAccountHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), MixerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        entries = []
        for index in (0, 4, 5):
            filename = f"u{index}.json"
            (root / filename).write_text(json.dumps({
                "cookie": f"SID=synthetic-{index}; SAPISID=synthetic-sap-{index}",
                "sapisid": f"synthetic-sap-{index}",
                "auth_user": str(index), "xsrf_token": f"synthetic-xsrf-{index}",
                "gemini_bl": f"synthetic-build-{index}"}), encoding="utf-8")
            entries.append({"id": f"web-u{index}", "auth_user": index, "cookie_file": filename})
        manifest = root / "accounts.json"
        manifest.write_text(json.dumps({"accounts": entries}), encoding="utf-8")
        self.stack.enter_context(mock.patch.dict(CONFIG, {"api_keys": ["synthetic-key"], "log_requests": False,
                                                         "proxy": None, "retry_delay_sec": 0}))
        self.stack.enter_context(mock.patch.dict(os.environ, {"GEMINI_WEB_ACCOUNTS_FILE": str(manifest)}, clear=True))
        self.pool = web_accounts.get_pool()

    def call(self, path, payload):
        with contextlib.closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)) as connection:
            connection.request("POST", path, json.dumps(payload), {"Authorization": "Bearer synthetic-key",
                                                                  "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()

    def observe(self):
        account = web_accounts.current_account()
        self.assertIsNotNone(account)
        self.assertTrue(account.busy)
        index = account.config["auth_user"]
        self.assertEqual(gemini.load_cookie()[0], f"SID=synthetic-{index}; SAPISID=synthetic-sap-{index}")
        self.assertEqual(gemini._build_headers()["X-Goog-AuthUser"], str(index))
        self.assertEqual(gemini._config()["xsrf_token"], f"synthetic-xsrf-{index}")
        return account.id

    def test_all_api_shapes_and_streams_rotate_without_changing_model_names(self):
        seen = []
        def generate(*args, **kwargs):
            seen.append(self.observe())
            return "synthetic answer"
        def stream(*args, **kwargs):
            seen.append(self.observe())
            yield {"content": "synthetic "}
            self.assertEqual(self.observe(), seen[-1])
            yield {"content": "answer"}
        calls = [
            ("/v1/chat/completions", {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hello"}]}),
            ("/v1/responses", {"model": "gemini-3.8-flash", "input": "hello"}),
            ("/v1beta/models/gemini-3.8-flash:generateContent", {"contents": [{"parts": [{"text": "hello"}]}]}),
        ]
        with mock.patch.object(server, "generate", side_effect=generate), mock.patch.object(server, "generate_stream", side_effect=stream):
            for streaming in (False, True):
                for index, (path, body) in enumerate(calls):
                    body = dict(body, stream=streaming)
                    if streaming and ":generateContent" in path:
                        path = path.replace(":generateContent", ":streamGenerateContent")
                    status, headers, result = self.call(path, body)
                    self.assertEqual(status, 200, result)
                    self.assertEqual(headers["X-Mixer-Web-Account"], ("web-u0", "web-u4", "web-u5")[index])
                    self.assertEqual(headers["X-Mixer-Source"], "web")
                    self.assertIn("gemini-3.8-flash", result)
                    if not streaming:
                        self.assertEqual(json.loads(result)["x_mixer"]["web_account"], headers["X-Mixer-Web-Account"])
        self.assertEqual(seen, ["web-u0", "web-u4", "web-u5"] * 2)
        self.assertFalse(any(account.busy for account in self.pool.accounts))

    def test_image_upload_and_emulated_tools_keep_same_account(self):
        stages = []
        def upload(*args, **kwargs):
            stages.append(("upload", self.observe()))
            return "/synthetic-upload"
        def generate(*args, **kwargs):
            stages.append(("generate", self.observe()))
            self.assertEqual(args[3], [("/synthetic-upload", "image.png")])
            return 'synthetic answer'
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode()
        payload = {"model": "gemini-3.8-flash", "stream": True,
                   "messages": [{"role": "user", "content": [{"type": "text", "text": "inspect"},
                                 {"type": "image_url", "image_url": {"url": image}}]}],
                   "tools": [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}]}
        with mock.patch.object(server, "upload_image", side_effect=upload), mock.patch.object(server, "generate", side_effect=generate):
            for expected in ("web-u0", "web-u4", "web-u5"):
                status, headers, result = self.call("/v1/chat/completions", payload)
                self.assertEqual(status, 200, result)
                self.assertIn("[DONE]", result)
                self.assertEqual(headers["X-Mixer-Web-Account"], expected)
                self.assertEqual(stages[-2:], [("upload", expected), ("generate", expected)])

    def test_auth_failure_is_quarantined_and_not_retried_with_another_account(self):
        payload = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hello"}]}
        error = urllib.error.HTTPError("https://gemini.google.com/synthetic", 401, "synthetic", {}, None)
        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=error) as upstream:
            status, headers, result = self.call("/v1/chat/completions", payload)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(result)["error"]["code"], "web_account_auth_failed")
        self.assertEqual(headers["X-Mixer-Web-Account"], "web-u0")
        self.assertTrue(self.pool.accounts[0].disabled)
        self.assertFalse(any(account.busy for account in self.pool.accounts))
        with mock.patch.object(server, "generate", return_value="ok"):
            self.assertEqual(self.call("/v1/chat/completions", payload)[1]["X-Mixer-Web-Account"], "web-u4")
        self.assertNotIn("synthetic-", result)

    def test_rate_limit_is_preserved_without_retry_or_false_auth_error(self):
        error = urllib.error.HTTPError("https://gemini.google.com/synthetic", 429, "synthetic", {"Retry-After": "600"}, None)
        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=error) as upstream:
            status, headers, result = self.call("/v1/responses", {"model": "gemini-3.8-flash", "input": "hello"})
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(result)["error"]["code"], "web_account_rate_limited")
        self.assertFalse(self.pool.accounts[0].disabled)
        self.assertGreater(self.pool.accounts[0].cooldown_until, 0)

    def test_stream_disconnect_closes_generator_before_releasing_lease(self):
        closed = []
        class BrokenOutput(io.BytesIO):
            writes = 0
            def write(self, value):
                self.writes += 1
                if self.writes >= 2:
                    raise BrokenPipeError()
                return super().write(value)
        def stream(*args, **kwargs):
            try:
                yield {"content": "one"}
                yield {"content": "two"}
            finally:
                closed.append(self.observe())
        handler = MixerHandler.__new__(MixerHandler)
        payload = json.dumps({"model": "gemini-3.8-flash", "stream": True,
                              "messages": [{"role": "user", "content": "hello"}]}).encode()
        handler.path, handler.command = "/v1/chat/completions", "POST"
        handler.headers = {"Authorization": "Bearer synthetic-key", "Content-Length": str(len(payload))}
        handler.rfile, handler.wfile = io.BytesIO(payload), BrokenOutput()
        handler._metadata, handler._sse_started, handler._request_id = None, False, "synthetic-request"
        handler._sequence, handler._response_id, handler._stream_api = 0, None, None
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(server, "generate_stream", side_effect=stream):
            handler.do_POST()
        self.assertEqual(closed, ["web-u0"])
        self.assertFalse(any(account.busy for account in self.pool.accounts))
        self.assertIsNone(web_accounts.current_account())

    def test_unconfigured_pool_preserves_single_account_path(self):
        with mock.patch.dict(os.environ, {"GEMINI_WEB_ACCOUNTS_FILE": ""}), mock.patch.object(server, "generate", return_value="ok"):
            status, headers, result = self.call("/v1/responses", {"model": "gemini-3.8-flash", "input": "hello"})
        self.assertEqual(status, 200)
        self.assertNotIn("X-Mixer-Web-Account", headers)
        self.assertNotIn("web_account", json.loads(result)["x_mixer"])

    def test_all_busy_returns_configuration_safe_error_without_upstream_request(self):
        for account in self.pool.accounts:
            account.busy = True
        with mock.patch.object(server, "generate") as generate:
            status, _, result = self.call("/v1/responses", {"model": "gemini-3.8-flash", "input": "hello"})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(result)["error"]["code"], "web_accounts_unavailable")
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
