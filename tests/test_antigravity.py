import json
import os
import time
import unittest
from unittest import mock

import httpx

from gemini_web2api import antigravity
from gemini_web2api.providers import ProviderError


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            if self.delay:
                time.sleep(self.delay)
            yield chunk

    def close(self):
        self.closed = True


class AntigravityTests(unittest.TestCase):
    def provider(self, handler=None, **kwargs):
        if handler is None:
            handler = lambda request: httpx.Response(200, json={"data": [{"id": "gemini-test"}]})
        config = dict(base_url="https://gateway.example/base", api_key="private-test-key",
                      models=["gemini-test"], group_confirmed=True,
                      transport=httpx.MockTransport(handler))
        config.update(kwargs)
        return antigravity.AntigravityProvider(**config)

    def assert_error(self, status, operation, code=None):
        with self.assertRaises(ProviderError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)
        self.assertNotIn("private-test-key", str(caught.exception))
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_missing_config_and_unconfirmed_group_never_send_requests(self):
        for configuration in ({}, {"group_confirmed": False}, {"api_key": ""}, {"models": []}):
            with self.subTest(configuration=configuration):
                if not configuration:
                    provider = antigravity.AntigravityProvider.from_env({})
                else:
                    provider = self.provider(handler=lambda _: self.fail("Unexpected network request"), **configuration)
                self.assertFalse(provider.enabled)
                self.assertEqual(provider.list_models(), [])
                self.assert_error(503, provider.quota)
                self.assert_error(503, lambda: provider.proxy({}, "chat", "ag-gemini-test"))

    def test_module_discovery_keeps_other_providers_available(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(antigravity.list_models(), [])
            self.assert_error(503, antigravity.quota)

    def test_url_validation_and_explicit_http_opt_in(self):
        for url in (
            "ftp://example.test", "https://a:b@example.test", "https://example.test?key=secret",
            "https://example.test#secret", "https://example.test/v1", "https://example.test/v1/",
            "https://example.test/../private", "https://example.test/%2e%2e/private",
            "https://example.test\\evil", "https://example.test\n", "http://compose-service:8080",
            "https://example.test:invalid", "https://example.test?", "https://example.test#",
        ):
            with self.subTest(url=url):
                self.assert_error(500, lambda: self.provider(base_url=url))
        for url in ("http://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080"):
            self.assertEqual(self.provider(base_url=url).base_url, url)
        provider = self.provider(base_url="http://sub2api:8080", allow_http=True)
        self.assertTrue(provider.enabled)

    def test_env_group_gate_requires_literal_true(self):
        env = dict(ANTIGRAVITY_BASE_URL="https://gateway.example", ANTIGRAVITY_API_KEY="private-test-key",
                   ANTIGRAVITY_MODELS="gemini-test", ANTIGRAVITY_GROUP_CONFIRMED="yes")
        self.assertFalse(antigravity.AntigravityProvider.from_env(env).enabled)
        env["ANTIGRAVITY_GROUP_CONFIRMED"] = "true"
        self.assertTrue(antigravity.AntigravityProvider.from_env(env).enabled)

    def test_discovery_uses_allowlist_intersection_and_safe_metadata(self):
        def handler(request):
            self.assertEqual(request.url.path, "/base/v1/models")
            self.assertEqual(request.headers["authorization"], "Bearer private-test-key")
            return httpx.Response(200, json={"data": [
                {"id": "gemini-test", "email": "hidden@example.test"}, {"id": "not-allowlisted"},
                {"id": "gemini-test-high"}, "invalid",
            ]})
        result = self.provider(handler).list_models()
        self.assertEqual([row["id"] for row in result], ["ag-gemini-test"])
        self.assertEqual(result[0]["source"], "antigravity")
        self.assertEqual(result[0]["transport"], "sub2api-openai")
        self.assertEqual(result[0]["capabilities"], "unknown")
        self.assertNotIn("hidden@", json.dumps(result))

    def test_chat_and_responses_are_forwarded_without_mutating_input(self):
        for kind, suffix in (("chat", "/chat/completions"), ("responses", "/responses")):
            requests = []
            def handler(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"data": [{"id": "gemini-test"}]})
                requests.append(request)
                data = json.loads(request.content)
                self.assertEqual(data["model"], "gemini-test")
                self.assertFalse(data["stream"])
                self.assertNotIn("x_mixer", data)
                self.assertEqual(data["tools"][0]["function"]["name"], "weather")
                self.assertTrue(request.url.path.endswith(suffix))
                return httpx.Response(200, json={"model": "gemini-test", "choices": [], "x_mixer": {"source": "fake"}})
            original = {"model": "ag-gemini-test", "tools": [{"type": "function", "function": {"name": "weather"}}],
                        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}],
                        "x_mixer": {"source": "client-supplied"}}
            result = self.provider(handler).proxy(original, kind, "ag-gemini-test")
            self.assertEqual(result["model"], "ag-gemini-test")
            self.assertEqual(result["x_mixer"]["source"], "antigravity")
            self.assertEqual(original["model"], "ag-gemini-test")
            self.assertEqual(original["x_mixer"]["source"], "client-supplied")
            self.assertEqual(len(requests), 1)

    def test_native_gemini_is_explicitly_unsupported(self):
        self.assert_error(501, lambda: self.provider().proxy({}, "gemini", "ag-gemini-test"))

    def test_request_size_and_invalid_json_fail_before_inference(self):
        provider = self.provider(max_request_bytes=64)
        self.assert_error(413, lambda: provider.proxy({"messages": ["x" * 100]}, "chat", "ag-gemini-test"))
        self.assert_error(400, lambda: self.provider().proxy({"temperature": float("nan")}, "chat", "ag-gemini-test"))

    def test_no_inferred_thinking_suffix_and_exact_upstream_suffix_allowed(self):
        self.assert_error(400, lambda: self.provider().proxy({}, "chat", "ag-gemini-test-high"), "unsupported_thinking_level")
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "gemini-test-high"}]})
            self.assertEqual(json.loads(request.content)["model"], "gemini-test-high")
            return httpx.Response(200, json={"model": "gemini-test-high", "choices": []})
        result = self.provider(handler, models=["gemini-test-high"]).proxy({}, "chat", "ag-gemini-test-high")
        self.assertEqual(result["x_mixer"]["thinking_control"], "upstream-controlled")
        self.assertNotIn("requested_effort", result["x_mixer"])

    def test_configured_but_unadvertised_model_is_rejected(self):
        provider = self.provider(lambda _: httpx.Response(200, json={"data": []}))
        self.assert_error(404, lambda: provider.proxy({}, "chat", "ag-gemini-test"))

    def test_redirects_errors_timeout_and_size_are_sanitized(self):
        for status in (302, 400, 401, 403, 429, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, headers={"location": "https://evil.example"},
                                      json={"error": {"message": "private-test-key"}})
            self.assert_error(status if 400 <= status < 500 else 502, self.provider(handler).list_models)
            self.assertEqual(len(calls), 1)
        provider = self.provider(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("private-test-key", request=request)))
        self.assert_error(504, provider.list_models)
        provider = self.provider(lambda _: httpx.Response(200, content=b"x" * 100), max_response_bytes=32)
        self.assert_error(502, provider.list_models, "upstream_response_too_large")

    def test_wall_deadline_stops_trickling_body_and_closes_stream(self):
        upstream = Chunks([b'{"data":', b'[]}'], delay=0.025)
        provider = self.provider(lambda _: httpx.Response(200, stream=upstream), total_timeout=0.01)
        self.assert_error(504, provider.list_models, "upstream_timeout")
        self.assertTrue(upstream.closed)

    def test_quota_whitelist_preserves_units_windows_and_resets(self):
        payload = {"mode": "quota_limited", "email": "private@example.test", "user_id": 7,
                   "quota": {"limit": 12, "used": 4, "remaining": 8, "unit": "USD"},
                   "rate_limits": [{"window": "5h", "limit": 10, "used": 2, "remaining": 8,
                                    "reset_at": "2026-10-17T20:00:00+08:00", "email": "private"}]}
        def handler(request):
            self.assertEqual(request.url.path, "/base/v1/usage")
            return httpx.Response(200, json=payload)
        result = self.provider(handler).quota()
        self.assertEqual((result["status"], result["remaining"], result["unit"]), ("available", 8, "USD"))
        self.assertEqual(result["scope"], "sub2api-key-wallet")
        self.assertEqual(result["windows"][0]["reset_at"], "2026-10-17T12:00:00Z")
        self.assertNotIn("email", json.dumps(result))
        self.assertNotIn("user_id", result)

    def test_quota_unknown_zero_unlimited_and_failure_are_distinct(self):
        for payload, status, remaining in (({}, "unknown", None), ({"remaining": 0, "unit": "USD"}, "available", 0),
                                            ({"remaining": -1}, "unlimited", None), ({"remaining": "0"}, "unknown", None)):
            with self.subTest(payload=payload):
                result = self.provider(lambda _: httpx.Response(200, json=payload)).quota()
                self.assertEqual((result["status"], result["remaining"]), (status, remaining))
                self.assertEqual(result["unlimited"], status == "unlimited")
        result = self.provider(lambda _: httpx.Response(503, content=b"private-test-key")).quota()
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["remaining"])
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_quota_subscriptions_and_invalid_numbers_are_not_invented(self):
        payload = {"remaining": None, "subscription": {"daily_usage_usd": 2, "weekly_usage_usd": True,
                    "daily_limit_usd": 10, "monthly_limit_usd": None, "expires_at": "2026-10-20T00:00:00Z",
                    "weekly_window_start": "not a timestamp", "email": "hidden"}}
        result = self.provider(lambda _: httpx.Response(200, json=payload)).quota()
        self.assertIsNone(result["remaining"])
        self.assertEqual(result["subscription"]["daily_usage_usd"], 2)
        self.assertIsNone(result["subscription"]["weekly_usage_usd"])
        self.assertIsNone(result["subscription"]["weekly_window_start"])
        self.assertNotIn("hidden", json.dumps(result))

    def sse_provider(self, upstream, **kwargs):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "gemini-test"}]})
            self.assertTrue(json.loads(request.content)["stream"])
            return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=upstream)
        return self.provider(handler, **kwargs)

    def test_stream_is_incremental_and_handles_split_utf8_crlf_multiline(self):
        first = 'data: {"model":"gemini-test",\r\ndata: "choices":[{"delta":{"content":"你"}}]}\r\n\r\n'.encode()
        cut = first.index("你".encode()) + 1
        upstream = Chunks([first[:cut], first[cut:], b'data: {"choices":[{"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'])
        generator = self.sse_provider(upstream).stream({}, "chat", "ag-gemini-test")
        event, payload = next(generator)
        self.assertIsNone(event)
        self.assertEqual(payload["choices"][0]["delta"]["content"], "你")
        self.assertEqual(payload["model"], "ag-gemini-test")
        self.assertEqual(upstream.read_count, 2)
        rest = list(generator)
        self.assertEqual(rest[-1], (None, "[DONE]"))
        self.assertTrue(upstream.closed)

    def test_responses_events_and_metadata_are_preserved(self):
        upstream = Chunks([b'event: response.created\ndata: {"type":"response.created","response":{"model":"gemini-test"}}\n\n',
                           b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n'])
        rows = list(self.sse_provider(upstream).stream({}, "responses", "ag-gemini-test"))
        self.assertEqual([row[0] for row in rows], ["response.created", "response.completed"])
        self.assertEqual(rows[0][1]["response"]["model"], "ag-gemini-test")
        self.assertEqual(rows[1][1]["x_mixer"]["source"], "antigravity")

    def test_stream_errors_malformed_truncated_and_oversized_events(self):
        for content, code in ((b'event: error\ndata: {"message":"private-test-key"}\n\n', "upstream_stream_error"),
                              (b'event: response.failed\ndata: {"message":"private-test-key"}\n\n', "upstream_stream_error"),
                              (b'data: not-json\n\n', "invalid_upstream_stream"),
                              (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', "upstream_stream_truncated"),
                              (b'data: {"choices":null}\n\n', "upstream_stream_truncated"),
                              (b': heartbeat\n\n', "invalid_upstream_stream")):
            with self.subTest(code=code):
                self.assert_error(502, lambda: list(self.sse_provider(Chunks([content])).stream({}, "chat", "ag-gemini-test")), code)
        provider = self.sse_provider(Chunks([b'data: ' + b'x' * 128]), max_event_bytes=32)
        self.assert_error(502, lambda: list(provider.stream({}, "chat", "ag-gemini-test")), "upstream_event_too_large")

    def test_tool_signatures_and_arguments_survive_streaming(self):
        tool = {"id": "call_test", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Paris"}'},
                "extra_content": {"google": {"thought_signature": "opaque-model-signature"}}}
        payload = {"model": "gemini-test", "choices": [{"delta": {"tool_calls": [tool]}, "finish_reason": "tool_calls"}]}
        upstream = Chunks([("data: " + json.dumps(payload) + "\n\n").encode(), b"data: [DONE]\n\n"])
        rows = list(self.sse_provider(upstream).stream({}, "chat", "ag-gemini-test"))
        self.assertEqual(rows[0][1]["choices"][0]["delta"]["tool_calls"][0], tool)

    def test_stream_timeout_does_not_expose_request_secrets(self):
        class TimeoutStream(httpx.SyncByteStream):
            def __iter__(self):
                raise httpx.ReadTimeout("private-test-key")
                yield b""
        provider = self.sse_provider(TimeoutStream())
        self.assert_error(504, lambda: list(provider.stream({}, "chat", "ag-gemini-test")))


if __name__ == "__main__":
    unittest.main()
