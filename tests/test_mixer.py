"""HTTP contract tests with synthetic credentials and no external requests."""
import http.client
import contextlib
import io
import json
import os
import threading
import unittest
from unittest import mock
from urllib.parse import quote

from gemini_web2api.config import CONFIG
from gemini_web2api.mixer import MixerHandler
from gemini_web2api.providers import ProviderError
from gemini_web2api.server import ThreadedServer


NATIVE = {"candidates": [{"content": {"role": "model", "parts": [{"text": "hello"}]},
                           "finishReason": "STOP", "index": 0}],
          "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3}}


class FakeRoute:
    meta = {"source": "build", "transport": "google-gemini-public-api", "upstream_model": "gemini-2.5-flash"}

    def generate(self, body):
        return NATIVE

    def stream(self, body):
        yield NATIVE


class MixerHTTPTests(unittest.TestCase):
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
        self.config = mock.patch.dict(CONFIG, {"api_keys": ["synthetic+mixer/key"], "log_requests": False})
        self.config.start()
        self.env = mock.patch.dict(os.environ, {"MIXER_WEB_ENABLED": "true", "MIXER_ALLOW_UNAUTHENTICATED": "false"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.config.stop()

    def call(self, path, payload=None, auth=True, raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        if auth:
            request_headers["Authorization"] = "Bearer synthetic+mixer/key"
        request_headers.update(headers or {})
        connection.request("GET" if payload is None and raw is None else "POST", path,
                           body=raw if raw is not None else (json.dumps(payload) if payload is not None else None),
                           headers=request_headers)
        response = connection.getresponse()
        data = response.read().decode()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, response_headers, data

    def test_health_is_public_and_does_not_disclose_models(self):
        status, _, body = self.call("/healthz", auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn("models", json.loads(body))

    def test_quota_shell_is_public_but_does_not_embed_key_or_account_data(self):
        status, headers, body = self.call("/quota", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'sha256-", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic+mixer/key", body)
        self.assertEqual(self.call("/v1/quota/antigravity", auth=False)[0], 401)

    def test_dashboard_trusted_http_origin_comes_from_operator_config_only(self):
        with mock.patch.dict(os.environ, {"MIXER_DASHBOARD_HTTP_ORIGINS": ""}):
            status, default_headers, default_body = self.call("/quota", auth=False, headers={
                "Host": "untrusted.example:60000", "X-Forwarded-Host": "untrusted.example:60000",
            })
        self.assertEqual(status, 200)
        self.assertNotIn("untrusted.example", default_body)
        with mock.patch.dict(os.environ, {"MIXER_DASHBOARD_HTTP_ORIGINS": "http://gateway.example:60000"}):
            status, configured_headers, configured_body = self.call("/quota", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("http://gateway.example:60000", configured_body)
        self.assertNotEqual(default_headers["Content-Security-Policy"], configured_headers["Content-Security-Policy"])
        self.assertNotIn("synthetic+mixer/key", configured_body)

    def test_all_inference_routes_require_authentication(self):
        for path in ("/v1/chat/completions", "/v1/responses", "/v1beta/models/build-test:generateContent"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, {}, auth=False)[0], 401)
        self.assertEqual(self.call("/v1/quota", auth=False)[0], 401)

    @mock.patch("gemini_web2api.mixer.antigravity.list_models", return_value=[])
    @mock.patch("gemini_web2api.mixer.list_models", return_value=[])
    def test_models_keep_web_provenance_and_query_key_auth(self, *_):
        status, _, body = self.call("/v1/models?key=" + quote("synthetic+mixer/key", safe=""), auth=False)
        self.assertEqual(status, 200)
        model = json.loads(body)["data"][0]
        self.assertEqual(model["source"], "web")
        self.assertEqual(model["model_identity"], "unverified-web-preset")

    @mock.patch("gemini_web2api.mixer.resolve_route", return_value=FakeRoute())
    def test_chat_has_source_and_actual_usage(self, resolve):
        status, headers, body = self.call("/v1/chat/completions", {
            "model": "build-gemini-2.5-flash-low", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mixer-Source"], "build")
        result = json.loads(body)
        self.assertEqual(result["x_mixer"]["transport"], "google-gemini-public-api")
        self.assertEqual(result["usage"]["total_tokens"], 3)

    @mock.patch("gemini_web2api.mixer.resolve_route", return_value=FakeRoute())
    def test_native_stream_routes_with_query_parameters(self, _):
        status, headers, body = self.call("/v1beta/models/build-gemini-2.5-flash:streamGenerateContent?alt=sse", {
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn('"candidates"', body)
        self.assertIn('"source": "build"', body)

    @mock.patch("gemini_web2api.mixer.resolve_route", return_value=FakeRoute())
    def test_responses_successful_event_lifecycle(self, _):
        status, _, body = self.call("/v1/responses", {"model": "build-gemini-2.5-flash", "input": "hello", "stream": True})
        self.assertEqual(status, 200, body)
        self.assertIn("event: response.created", body)
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn("event: response.completed", body)

    def test_stream_failure_does_not_send_success_terminator(self):
        route = FakeRoute()
        def fail(body):
            yield {"candidates": [{"content": {"parts": [{"text": "partial"}]}, "index": 0}]}
            raise ProviderError(504, "upstream_timeout", "Upstream timed out")
        route.stream = fail
        with mock.patch("gemini_web2api.mixer.resolve_route", return_value=route):
            status, _, body = self.call("/v1/responses", {"model": "build-gemini-2.5-flash", "input": "hello", "stream": True})
        self.assertEqual(status, 200)
        self.assertIn("event: response.failed", body)
        self.assertNotIn("response.completed", body)
        self.assertNotIn("[DONE]", body)

    def test_early_upstream_failure_is_json_status(self):
        route = FakeRoute()
        def fail(body):
            raise ProviderError(429, "rate_limit", "Upstream rate limit")
            yield
        route.stream = fail
        with mock.patch("gemini_web2api.mixer.resolve_route", return_value=route):
            status, headers, body = self.call("/v1/chat/completions", {"model": "build-gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        self.assertEqual(status, 429)
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_antigravity_data_only_responses_failure_keeps_response_id(self):
        def upstream(*args):
            yield None, {"type": "response.created", "response": {"id": "resp_real", "status": "in_progress"}}
            raise ProviderError(502, "upstream_incomplete", "Truncated upstream")
        with mock.patch("gemini_web2api.mixer.antigravity.stream", side_effect=upstream):
            status, _, body = self.call("/v1/responses", {"model": "ag-test", "input": "hi", "stream": True})
        self.assertEqual(status, 200)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertEqual(events[-1]["response"]["id"], "resp_real")

    @mock.patch("gemini_web2api.server.generate", return_value="legacy works")
    def test_existing_web_generation_remains_available(self, generate):
        status, headers, body = self.call("/v1/chat/completions", {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mixer-Source"], "web")
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "legacy works")
        generate.assert_called_once()

    def test_web_cannot_claim_verified_thinking_levels(self):
        status, _, _ = self.call("/v1/chat/completions", {"model": "gemini-3.6-flash", "reasoning_effort": "low"})
        self.assertEqual(status, 400)

    @mock.patch("gemini_web2api.server.generate", side_effect=RuntimeError("Cookie: synthetic-private-cookie"))
    def test_legacy_error_does_not_expose_exception_credentials(self, _):
        status, _, body = self.call("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 502)
        self.assertNotIn("synthetic-private-cookie", body)

    def test_legacy_failed_stream_emits_safe_error(self):
        def fail(*args):
            yield "partial"
            raise RuntimeError("Cookie: synthetic-private-cookie")
        logs = io.StringIO()
        with mock.patch("gemini_web2api.server.generate_stream", side_effect=fail), mock.patch.dict(CONFIG, {"log_requests": True}), contextlib.redirect_stderr(logs):
            status, _, body = self.call("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "stream": True})
        self.assertEqual(status, 200)
        self.assertIn("event: error", body)
        self.assertNotIn("[DONE]", body)
        self.assertNotIn("synthetic-private-cookie", body + logs.getvalue())

    @mock.patch("gemini_web2api.server.fetch_image_bytes")
    def test_legacy_remote_image_fetch_requires_explicit_opt_in(self, fetch):
        with mock.patch.dict(os.environ, {"MIXER_WEB_ALLOW_REMOTE_IMAGES": "false"}):
            status, _, body = self.call("/v1/chat/completions", {"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "http://127.0.0.1/private"}}]}]})
        self.assertEqual(status, 400)
        fetch.assert_not_called()

    @mock.patch("gemini_web2api.server.generate", return_value="pro output")
    def test_v1_native_web_alias_keeps_selected_model(self, generate):
        from gemini_web2api.models import MODELS
        status, _, body = self.call("/v1/models/gemini-3.1-pro:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(generate.call_args.args[1], MODELS["gemini-3.1-pro"]["mode"])
        self.assertEqual(json.loads(body)["modelVersion"], "gemini-3.1-pro")

    @mock.patch("gemini_web2api.mixer.antigravity.proxy", return_value={"choices": [], "x_mixer": {"thinking_control": "upstream-controlled"}})
    def test_antigravity_slash_model_is_callable_and_metadata_is_merged(self, proxy):
        status, _, body = self.call("/v1/chat/completions", {"model": "ag-google/model:version", "messages": []})
        self.assertEqual(status, 200, body)
        self.assertEqual(proxy.call_args.args[2], "ag-google/model:version")
        self.assertEqual(json.loads(body)["x_mixer"]["thinking_control"], "upstream-controlled")

    def test_scalar_json_and_invalid_model_fail_cleanly(self):
        for payload in ([], {"model": []}, {"model": "x\r\nsecret"}, {"stream": "yes"}):
            self.assertEqual(self.call("/v1/chat/completions", payload)[0], 400)

    def test_body_limit_is_enforced_before_parsing(self):
        with mock.patch.dict(os.environ, {"MIXER_MAX_REQUEST_BYTES": "8"}):
            self.assertEqual(self.call("/v1/chat/completions", raw=b"012345678")[0], 413)

    def test_quota_error_is_not_zero_balance(self):
        with mock.patch("gemini_web2api.mixer.antigravity.quota", side_effect=ProviderError(503, "provider_not_configured", "Not configured")):
            status, _, body = self.call("/v1/quota/antigravity")
        self.assertEqual(status, 503)
        self.assertNotIn('"remaining": 0', body)


if __name__ == "__main__":
    unittest.main()
