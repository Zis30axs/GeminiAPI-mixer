"""Offline transport and public HTTP contract tests; all credentials synthetic."""
import copy
import http.client
import json
import os
import threading
import time
import unittest
from unittest import mock

import httpx

from gemini_web2api import openai_provider as op
from gemini_web2api.config import CONFIG
from gemini_web2api.mixer import MixerHandler
from gemini_web2api.providers import ProviderError
from gemini_web2api.server import ThreadedServer


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    def __iter__(self):
        for chunk in self.chunks:
            if self.delay:
                time.sleep(self.delay)
            yield chunk

    def close(self):
        self.closed = True


class OpenAIProviderTests(unittest.TestCase):
    def provider(self, handler=None, **kwargs):
        if handler is None:
            handler = lambda request: httpx.Response(200, json={"data": [{"id": "gpt-test"}]})
        config = dict(base_url="https://gateway.example/base", api_key="synthetic-upstream-key",
                      models=["gpt-test"], group_confirmed=True, transport=httpx.MockTransport(handler))
        config.update(kwargs)
        return op.OpenAIProvider(**config)

    def assert_error(self, operation, status, code=None):
        with self.assertRaises(ProviderError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn("synthetic-upstream-key", str(caught.exception))
        self.assertNotIn("Antigravity", str(caught.exception))
        return caught.exception

    def test_disabled_provider_preserves_catalog_and_never_opens_network(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(op.list_models(), [])
            self.assert_error(lambda: op.proxy({}, "chat", "gpt-test"), 503)
        provider = self.provider(group_confirmed=False, handler=lambda _: self.fail("network"))
        self.assertEqual(provider.list_models(), [])
        self.assert_error(lambda: provider.proxy({}, "chat", "gpt-test"), 503)

    def test_configuration_is_independent_of_antigravity_and_environment_is_unchanged(self):
        env = {"ANTIGRAVITY_BASE_URL": "https://wrong.example", "ANTIGRAVITY_API_KEY": "wrong-key",
               "ANTIGRAVITY_MODELS": "gemini-test", "ANTIGRAVITY_GROUP_CONFIRMED": "true",
               "MIXER_OPENAI_BASE_URL": "http://sub2api:8080", "MIXER_OPENAI_API_KEY": "synthetic-upstream-key",
               "MIXER_OPENAI_MODELS": "gpt-test, o3", "MIXER_OPENAI_GROUP_CONFIRMED": "true",
               "MIXER_OPENAI_ALLOW_HTTP": "true", "MIXER_OPENAI_READ_TIMEOUT_SEC": "120"}
        before = copy.deepcopy(env)
        provider = op.OpenAIProvider.from_env(env)
        self.assertEqual(provider.base_url, "http://sub2api:8080")
        self.assertEqual(provider._models, ("gpt-test", "o3"))
        self.assertEqual(provider.read_timeout, 120)
        self.assertEqual(env, before)

    def test_strict_base_url_and_allowlist_validation(self):
        for base in ("http://remote.example", "https://user:secret@gateway.example", "https://gateway.example/v1",
                     "https://gateway.example?key=secret", "https://gateway.example/../elsewhere",
                     "https://gateway.example/%2e%2e/", "https://gateway.example\\evil", "https://gateway.example\n"):
            self.assert_error(lambda: self.provider(base_url=base), 500)
        for models in (["ag-gemini"], ["gpt-a/b"], ["claude-test"], [42]):
            self.assert_error(lambda: self.provider(models=models), 503)
        self.assertTrue(self.provider(base_url="http://127.0.0.1:8080").enabled)
        self.assert_error(lambda: self.provider(api_key="unsafe\nsecret"), 503)

    def test_catalog_intersects_allowlist_and_strips_untrusted_metadata(self):
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-upstream-key")
            self.assertEqual(request.headers["x-mixer-upstream-hop"], "1")
            return httpx.Response(200, json={"data": [
                {"id": "gpt-test", "email": "private@example.test", "source": "wrong"},
                {"id": "gpt-not-allowed"}, {"id": "gpt-test-high"}, {"id": "o3"}]})
        rows = self.provider(handler, models=["gpt-test", "o3"]).list_models()
        self.assertEqual([row["id"] for row in rows], ["gpt-test", "o3"])
        self.assertTrue(all(row["source"] == "openai" and row["capabilities"] == "upstream-controlled" for row in rows))
        self.assertTrue(all(row["source_verification"] == "operator-confirmed-openai-only-group" for row in rows))
        self.assertNotIn("private@", json.dumps(rows))
        self.assertNotIn("thinking_efforts", json.dumps(rows))

    def test_unknown_or_invisible_model_does_not_generate(self):
        provider = self.provider(lambda _: httpx.Response(200, json={"data": []}))
        self.assert_error(lambda: provider.proxy({}, "chat", "gpt-test"), 404)
        self.assert_error(lambda: provider.proxy({}, "chat", "gpt-test-high"), 404)
        self.assert_error(lambda: provider.proxy({}, "gemini", "gpt-test"), 501)

    def test_chat_and_responses_preserve_tool_image_and_reasoning_parameters(self):
        for kind in ("chat", "responses"):
            request = {"model": "gpt-test", "x_mixer": {"source": "forged"},
                       "tools": [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}],
                       "tool_choice": "auto", "messages": [{"role": "user", "content": [
                           {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}],
                       "input": [{"type": "function_call_output", "call_id": "call_1", "output": "ok"}],
                       "reasoning_effort": "high", "max_output_tokens": 256}
            before = copy.deepcopy(request)
            def handler(incoming):
                if incoming.method == "GET":
                    return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})
                body = json.loads(incoming.content)
                expected = dict(before, stream=False)
                expected.pop("x_mixer")
                self.assertEqual(body, expected)
                self.assertEqual(incoming.url.path, "/base/v1/" + ("responses" if kind == "responses" else "chat/completions"))
                return httpx.Response(200, json={"model": "gpt-test", "choices": [{"message": {"tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}]}}],
                    "output": [{"type": "function_call", "call_id": "call_1", "arguments": "{}"}]})
            result = self.provider(handler).proxy(request, kind, "gpt-test")
            self.assertEqual(request, before)
            self.assertEqual(result["x_mixer"]["source"], "openai")
            self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["id"], "call_1")
            self.assertEqual(result["output"][0]["type"], "function_call")

    def test_reasoning_conflict_and_invalid_control_are_local_safe_errors(self):
        provider = self.provider(handler=lambda _: self.fail("network"))
        self.assert_error(lambda: provider.proxy({"reasoning_effort": "low", "reasoning": {"effort": "high"}}, "chat", "gpt-test"), 400)
        self.assert_error(lambda: provider.proxy({"reasoning_effort": "high\nX-Key: secret"}, "chat", "gpt-test"), 400)

    def test_request_validation_precedes_network(self):
        provider = self.provider(handler=lambda _: self.fail("network"), max_request_bytes=32)
        self.assert_error(lambda: provider.proxy({"messages": ["x" * 100]}, "chat", "gpt-test"), 413)
        self.assert_error(lambda: provider.proxy({"temperature": float("nan")}, "chat", "gpt-test"), 400)

    def test_upstream_status_redaction_and_no_redirects(self):
        for upstream, expected, code in ((401, 502, "upstream_authentication_failed"), (403, 502, "upstream_authentication_failed"),
                                        (429, 429, "upstream_rate_limited"), (404, 404, "upstream_not_found"),
                                        (408, 504, "upstream_timeout"), (504, 504, "upstream_timeout"),
                                        (302, 502, "upstream_redirect_rejected"), (500, 502, "upstream_error")):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(upstream, headers={"location": "https://evil.example"},
                                      json={"error": {"message": "synthetic-upstream-key"}})
            self.assert_error(self.provider(handler).list_models, expected, code)
            self.assertEqual(len(calls), 1)

    def test_transport_timeouts_response_size_and_invalid_json_are_safe(self):
        def timeout(request):
            raise httpx.ReadTimeout("synthetic-upstream-key", request=request)
        self.assert_error(self.provider(timeout).list_models, 504, "upstream_timeout")
        self.assert_error(self.provider(lambda _: httpx.Response(200, content=b"x" * 64), max_response_bytes=32).list_models,
                          502, "upstream_response_too_large")
        self.assert_error(self.provider(lambda _: httpx.Response(200, content=b"not JSON")).list_models, 502, "invalid_upstream_response")

    def test_client_disables_ambient_proxies_redirects_and_cookie_reuse(self):
        provider = self.provider()
        with mock.patch("gemini_web2api.openai_transport.httpx.Client") as factory:
            provider._client(time.monotonic() + 10)
        self.assertIs(factory.call_args.kwargs["trust_env"], False)
        self.assertIs(factory.call_args.kwargs["follow_redirects"], False)

    def stream_provider(self, chunks, **kwargs):
        body = Chunks(chunks)
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})
            self.assertTrue(json.loads(request.content)["stream"])
            return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})
        return self.provider(handler, **kwargs), body

    def test_chat_sse_tool_delta_and_usage_survive(self):
        data = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
                "usage": {"completion_tokens": 3}}
        provider, body = self.stream_provider([("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode()])
        events = list(provider.stream({}, "chat", "gpt-test"))
        self.assertEqual(events[0][1]["choices"], data["choices"])
        self.assertEqual(events[0][1]["usage"], data["usage"])
        self.assertEqual(events[-1][1], "[DONE]")
        self.assertTrue(body.closed)

    def test_responses_sse_preserves_event_names_and_function_call_output(self):
        data = {"type": "response.completed", "response": {"model": "gpt-test", "status": "completed",
                "output": [{"type": "function_call", "call_id": "call_1", "arguments": "{}"}]}}
        provider, body = self.stream_provider([("event: response.completed\ndata: " + json.dumps(data) + "\n\n").encode()])
        events = list(provider.stream({}, "responses", "gpt-test"))
        self.assertEqual(events[0][0], "response.completed")
        self.assertEqual(events[0][1]["response"]["output"], data["response"]["output"])
        self.assertEqual(events[0][1]["response"]["x_mixer"]["source"], "openai")
        self.assertTrue(body.closed)

    def test_truncation_is_error_and_cancellation_closes_upstream(self):
        event = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        provider, body = self.stream_provider([event])
        stream = provider.stream({}, "chat", "gpt-test")
        self.assertEqual(next(stream)[1]["choices"][0]["delta"]["content"], "partial")
        self.assert_error(lambda: next(stream), 502, "upstream_stream_truncated")
        self.assertTrue(body.closed)
        provider, body = self.stream_provider([event, event])
        stream = provider.stream({}, "chat", "gpt-test")
        next(stream)
        stream.close()
        self.assertTrue(body.closed)

    def test_sse_errors_and_event_size_are_redacted(self):
        provider, _ = self.stream_provider([b'event: error\ndata: {"error":{"message":"synthetic-upstream-key"}}\n\n'])
        self.assert_error(lambda: list(provider.stream({}, "chat", "gpt-test")), 502, "upstream_stream_error")
        provider, _ = self.stream_provider([b'data: ' + b'x' * 64 + b'\n\n'], max_event_bytes=32)
        self.assert_error(lambda: list(provider.stream({}, "chat", "gpt-test")), 502, "upstream_event_too_large")

    def test_total_deadline_stops_drip_response(self):
        body = Chunks([b'{"data":', b'[]}'], delay=0.06)
        provider = self.provider(lambda _: httpx.Response(200, stream=body), total_timeout=0.05)
        self.assert_error(provider.list_models, 504, "upstream_timeout")
        self.assertTrue(body.closed)


class OpenAIMixerTests(unittest.TestCase):
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
        for patch in (mock.patch.dict(CONFIG, {"api_keys": ["synthetic-mixer-key"], "log_requests": False}),
                      mock.patch.dict(os.environ, {}, clear=True),
                      mock.patch("gemini_web2api.mixer.list_models", return_value=[]),
                      mock.patch("gemini_web2api.mixer.antigravity.list_models", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.calls = []
        self.upstream_status = 200
        self.provider = op.OpenAIProvider(base_url="https://gateway.example", api_key="synthetic-upstream-key",
                                         models=["gpt-test"], group_confirmed=True, transport=httpx.MockTransport(self.upstream))
        patch = mock.patch.object(op.OpenAIProvider, "from_env", return_value=self.provider)
        patch.start()
        self.addCleanup(patch.stop)

    def upstream(self, request):
        self.assertEqual(request.headers["authorization"], "Bearer synthetic-upstream-key")
        self.assertNotIn("synthetic-mixer-key", str(request.headers))
        if self.upstream_status != 200:
            return httpx.Response(self.upstream_status, json={"error": {"message": "synthetic-upstream-key"}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})
        self.calls.append(json.loads(request.content))
        if self.calls[-1].get("stream"):
            content = ('event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
                       if request.url.path.endswith("responses") else 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
        return httpx.Response(200, json={"model": "gpt-test", "choices": [], "output": []})

    def call(self, path, body=None, key="synthetic-mixer-key", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            hdrs = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
            hdrs.update(headers or {})
            connection.request("GET" if body is None else "POST", path,
                               body=None if body is None else json.dumps(body), headers=hdrs)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_same_mixer_key_lists_web_and_gpt_without_upstream_key_exposure(self):
        status, _, content = self.call("/v1/models")
        self.assertEqual(status, 200)
        rows = json.loads(content)["data"]
        self.assertTrue(any(row["source"] == "web" for row in rows))
        self.assertTrue(any(row["id"] == "gpt-test" and row["source"] == "openai" for row in rows))
        self.assertNotIn("synthetic-upstream-key", content)
        self.assertEqual(self.call("/v1/models", key="synthetic-upstream-key")[0], 401)

    def test_chat_responses_and_sse_carry_source_and_requested_effort(self):
        for path in ("/v1/chat/completions", "/v1/responses"):
            for streaming in (False, True):
                status, headers, content = self.call(path, {"model": "gpt-test", "stream": streaming,
                                                          "reasoning": {"effort": "high"}})
                self.assertEqual(status, 200, content)
                self.assertEqual(headers["X-Mixer-Source"], "openai")
                self.assertEqual(headers["X-Mixer-Requested-Effort"], "high")
                self.assertEqual(headers["X-Mixer-Transport"], "sub2api-openai")
                self.assertEqual(self.calls[-1]["reasoning"], {"effort": "high"})

    def test_unknown_gpt_and_native_never_enter_web(self):
        with mock.patch("gemini_web2api.server.GeminiHandler._handle_chat", side_effect=AssertionError("Web fallback")):
            self.assertEqual(self.call("/v1/chat/completions", {"model": "gpt-unknown"})[0], 404)
            self.assertEqual(self.call("/v1/chat/completions", {"model": "o9-unknown"})[0], 404)
            status, headers, _ = self.call("/v1beta/models/gpt-test:generateContent", {"contents": []})
            self.assertEqual(status, 501)
            self.assertEqual(headers["X-Mixer-Source"], "openai")

    def test_discovery_failure_does_not_hide_other_providers(self):
        self.upstream_status = 503
        status, _, content = self.call("/v1/models")
        self.assertEqual(status, 200)
        self.assertTrue(all(row["source"] == "web" for row in json.loads(content)["data"]))
        self.assertEqual(self.call("/v1/chat/completions", {"model": "gpt-test"})[0], 502)

    def test_upstream_auth_does_not_blame_mixer_key_or_expose_secret(self):
        self.upstream_status = 401
        status, headers, content = self.call("/v1/chat/completions", {"model": "gpt-test"})
        self.assertEqual(status, 502)
        self.assertEqual(headers["X-Mixer-Source"], "openai")
        self.assertIn("upstream_authentication_failed", content)
        self.assertNotIn("synthetic-upstream-key", content)

    def test_direct_loop_is_rejected_before_upstream(self):
        self.assertEqual(self.call("/v1/models", headers={"X-Mixer-Upstream-Hop": "1"})[0], 508)
        self.assertEqual(self.call("/v1/chat/completions", {"model": "gpt-test"}, headers={"X-Mixer-Upstream-Hop": "1"})[0], 508)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
