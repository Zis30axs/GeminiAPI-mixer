"""No live sessions: HTTP adapter contracts use synthetic authenticated peers."""
import copy
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx

from gemini_web2api import aistudio_http as adapter, providers
from gemini_web2api.config import CONFIG
from gemini_web2api.mixer import MixerHandler
from gemini_web2api.server import ThreadedServer


MODEL = "gemini-2.5-flash"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
FINAL = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": "hello"}]}, "finishReason": "STOP"}]}
ENTRY = {"id": MODEL, "supported_generation_methods": ["generateContent"],
         "channels": ["playground", "build"],
         "capabilities": {"chat_model": True, "thinking_budget": True, "function_declarations": True}}


def event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def __iter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class AdapterFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "AIS_TRANSPORT": "aistudio-http", "BUILD_TRANSPORT": "aistudio-http",
            "AIS_MODELS": MODEL, "BUILD_MODELS": MODEL,
            "AISTUDIO_HTTP_BASE_URL": "https://sidecar.example",
            "AISTUDIO_HTTP_API_KEY": "synthetic-sidecar-key",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        adapter._CACHE.clear()
        self.requests, self.entries = [], [copy.deepcopy(ENTRY)]
        self.result = lambda request: self.response(request, json=FINAL)
        self.factory = mock.patch.object(adapter, "_new_client", side_effect=lambda timeout: httpx.Client(
            timeout=timeout, transport=httpx.MockTransport(self.handle), follow_redirects=False, trust_env=False))
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def response(self, request, status=200, **kwargs):
        headers = {adapter.CHANNEL_HEADER: request.headers[adapter.CHANNEL_HEADER], "X-Mixer-Channel-Locked": "1"}
        headers.update(kwargs.pop("headers", {}))
        return httpx.Response(status, headers=headers, **kwargs)

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/models":
            return self.response(request, json={"data": self.entries})
        return self.result(request)

    def proof(self, values=None, **root_changes):
        entry = {"text": True, "streaming": True, "thinking": {"low": {"thinkingBudget": 777}},
                 "parameters": ["maxOutputTokens"]}
        entry.update(values or {})
        data = {"version": 1, "backend_base_url": "https://sidecar.example",
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "models": {"ais/" + MODEL: entry, "build/" + MODEL: copy.deepcopy(entry)}}
        data.update(root_changes)
        path = Path(self.directory.name) / "evidence.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        os.environ["AISTUDIO_HTTP_EVIDENCE_FILE"] = str(path)

    def route(self, alias="ais-" + MODEL, effort=None):
        return providers.resolve_route(alias, effort)

    def error(self, code, operation, status=None):
        with self.assertRaises(providers.ProviderError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        if status:
            self.assertEqual(caught.exception.status, status)
        self.assertNotIn("synthetic-sidecar-key", str(caught.exception))
        return caught.exception


class AdapterTests(AdapterFixture):
    def test_missing_evidence_is_base_text_without_public_thinking_claims(self):
        rows = providers.list_models()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["transport"] == "aistudio-http" for row in rows))
        self.assertTrue(all(row["capabilities"]["thinking_efforts"] == [] for row in rows))
        self.assertTrue(all(row["capabilities"]["streaming"] is False for row in rows))
        route = self.route()
        self.assertEqual(route.meta["verification"], "upstream-declared")
        self.assertEqual(route.generate(BODY), FINAL)
        self.error("unsupported_streaming", lambda: list(route.stream(BODY)), 400)
        self.error("unsupported_reasoning_effort", lambda: self.route(effort="low"), 400)

    def test_verified_native_mapping_and_explicit_source_credentials(self):
        self.proof()
        route = self.route("build-" + MODEL + "-low")
        self.assertEqual(route.generate(BODY), FINAL)
        request = self.requests[-1]
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-sidecar-key")
        self.assertEqual(request.headers[adapter.CHANNEL_HEADER], "build")
        self.assertNotIn("x-goog-api-key", request.headers)
        self.assertNotIn("cookie", request.headers)
        self.assertEqual(request.url.path, "/v1beta/models/" + MODEL + ":generateContent")
        self.assertEqual(json.loads(request.content)["generationConfig"]["thinkingConfig"], {"thinkingBudget": 777})
        self.assertEqual(route.meta["effective_thinking_budget"], 777)
        self.assertNotIn("synthetic", repr(route))

    def test_native_thinking_must_match_evidence_and_conflicts_fail(self):
        self.proof()
        route = self.route()
        native = dict(BODY, generationConfig={"thinkingConfig": {"thinkingBudget": 777}})
        route.prepare(native)
        self.assertEqual(route.meta["effective_thinking_budget"], 777)
        self.error("thinking_conflict", lambda: self.route("ais-" + MODEL + "-low", "high"))
        self.error("thinking_conflict", lambda: self.route(effort="low").prepare(native))
        self.error("unsupported_reasoning_effort", lambda: route.prepare(dict(BODY, generationConfig={"thinkingConfig": {"thinkingBudget": 1024}})))

    def test_catalogue_and_evidence_are_both_required(self):
        self.proof()
        self.entries[0]["capabilities"]["thinking_budget"] = False
        self.error("unsupported_reasoning_effort", lambda: self.route(effort="low"))
        self.entries[0]["channels"] = ["build"]
        adapter._CACHE.clear()
        self.error("upstream_model_not_found", self.route)
        self.assertTrue(all(row["source"] == "build" for row in providers.list_models()))

    def test_stale_wrong_backend_or_malformed_evidence_grants_no_features(self):
        for changes in ({"verified_at": "2000-01-01T00:00:00Z"}, {"backend_base_url": "https://other.example"},
                        {"models": []}, {"version": True}):
            self.proof(**changes)
            self.assertFalse(self.route().capabilities["streaming"])
            self.error("unsupported_reasoning_effort", lambda: self.route(effort="low"))

    def test_unverified_inputs_reject_before_generation(self):
        route = self.route()
        for body, code in ((dict(BODY, tools=[{"functionDeclarations": []}]), "unsupported_tools"),
                           (dict(BODY, generationConfig={"temperature": 1}), "unsupported_parameter"),
                           (dict(BODY, cachedContent="other"), "unsupported_parameter"),
                           (dict(BODY, safetySettings=[]), "unsupported_parameter"),
                           ({"contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": "eA=="}}]}]}, "unsupported_image_input")):
            self.error(code, lambda body=body: route.generate(body))
        self.assertTrue(all(r.method == "GET" for r in self.requests))

    def test_missing_or_wrong_channel_lock_never_accepts_success(self):
        for headers in ({adapter.CHANNEL_HEADER: "build", "X-Mixer-Channel-Locked": "1"},
                        {adapter.CHANNEL_HEADER: "playground"}, {}):
            self.result = lambda request, headers=headers: httpx.Response(200, headers=headers, json=FINAL)
            self.error("upstream_channel_mismatch", lambda: self.route().generate(BODY), 502)

    def test_failure_is_safe_and_does_not_follow_redirect_or_fallback(self):
        for status, code in ((401, "upstream_authentication_failed"), (403, "upstream_authentication_failed"),
                             (429, "upstream_rate_limited"), (302, "upstream_redirect_rejected"), (503, "upstream_error")):
            self.result = lambda request, status=status: self.response(request, status=status, headers={"Location": "https://other.example"},
                                                                       content=b"synthetic-sidecar-key")
            with mock.patch.object(providers, "_new_client", side_effect=AssertionError("public fallback")):
                self.error(code, lambda: self.route().generate(BODY))

    def test_destination_is_operator_only_and_http_requires_opt_in(self):
        for base in ("http://127.0.0.1:8080", "https://user:pass@sidecar.example", "https://sidecar.example/?key=x",
                     "https://sidecar.example/../other", "https://sidecar.example/v1", "https://sidecar.example/%2e"):
            with mock.patch.dict(os.environ, {"AISTUDIO_HTTP_BASE_URL": base}):
                self.error("provider_configuration_error", self.route)
        with mock.patch.dict(os.environ, {"AISTUDIO_HTTP_BASE_URL": "http://127.0.0.1:8080", "AISTUDIO_HTTP_ALLOW_HTTP": "true"}):
            self.assertEqual(self.route().base, "http://127.0.0.1:8080")

    def test_provider_mode_missing_key_and_unknown_model_never_use_legacy(self):
        with mock.patch.dict(os.environ, {"AIS_TRANSPORT": "typo"}):
            self.error("provider_configuration_error", self.route)
        with mock.patch.dict(os.environ, {"AISTUDIO_HTTP_API_KEY": ""}):
            self.error("provider_not_configured", self.route)
        self.error("unknown_provider_model", lambda: self.route("ais-https://other.example"))

    def test_stream_incremental_completion_error_and_consumer_cancellation(self):
        self.proof()
        partial = {"candidates": [{"content": {"parts": [{"text": "part"}]}}]}
        for chunks, code in (([event(partial)], "upstream_stream_incomplete"),
                             ([event(partial), httpx.ReadTimeout("synthetic-sidecar-key")], "upstream_timeout"),
                             ([event({"error": {"code": 429, "message": "synthetic-sidecar-key"}})], "upstream_rate_limited")):
            source = Chunks(chunks)
            self.result = lambda request: self.response(request, headers={"content-type": "text/event-stream"}, stream=source)
            self.error(code, lambda: list(self.route().stream(BODY)))
            self.assertTrue(source.closed)
        source = Chunks([event(partial), event(FINAL)])
        self.result = lambda request: self.response(request, headers={"content-type": "text/event-stream"}, stream=source)
        iterator = self.route().stream(BODY)
        self.assertEqual(next(iterator), partial)
        iterator.close()
        self.assertTrue(source.closed)

    def test_complete_result_without_finish_and_unknown_output_fail_safely(self):
        for result in ({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]},
                       {"candidates": [{"finishReason": "synthetic-sidecar-key"}]}):
            self.result = lambda request, result=result: self.response(request, json=result)
            with self.assertRaises(providers.ProviderError) as caught:
                self.route().generate(BODY)
            self.assertNotIn("synthetic-sidecar-key", str(caught.exception))

    def test_transport_size_limits_are_enforced_before_or_during_io(self):
        with mock.patch.dict(os.environ, {"PROVIDER_MAX_REQUEST_BYTES": "1024"}):
            self.error("request_too_large", lambda: self.route().generate({"contents": [{"parts": [{"text": "x" * 2000}]}]}), 413)
            self.assertTrue(all(r.method == "GET" for r in self.requests))
        self.result = lambda request: self.response(request, content=b"x" * 2000)
        with mock.patch.dict(os.environ, {"PROVIDER_MAX_RESPONSE_BYTES": "1024"}):
            self.error("upstream_response_too_large", lambda: self.route().generate(BODY), 502)
        self.proof()
        self.result = lambda request: self.response(request, headers={"Content-Type": "text/event-stream"}, content=b"data: " + b"x" * 300)
        with mock.patch.dict(os.environ, {"PROVIDER_MAX_SSE_LINE_BYTES": "256"}):
            self.error("upstream_sse_line_too_large", lambda: list(self.route().stream(BODY)), 502)

    def test_declared_tools_need_evidence_and_return_real_native_parts(self):
        self.proof({"tools": True})
        route = self.route()
        native = dict(BODY, tools=[{"functionDeclarations": [{"name": "read_value", "parametersJsonSchema": {"type": "object"}}]}],
                      toolConfig={"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["read_value"]}})
        tool_result = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "read_value", "args": {}},
                                                              "thoughtSignature": "opaque-real-signature"}]}, "finishReason": "STOP"}]}
        self.result = lambda request: self.response(request, json=tool_result)
        self.assertEqual(route.generate(native), tool_result)
        bad = copy.deepcopy(native)
        bad["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] = ["not_declared"]
        self.error("unsupported_tools", lambda: route.generate(bad))
        self.proof({"tools": False})
        self.error("unsupported_tools", lambda: self.route().generate(BODY), 502)

    def test_native_level_proof_is_channel_specific_and_confirms_catalogue_options(self):
        self.entries[0]["capabilities"]["thinking_level"] = True
        self.entries[0]["capability_options"] = {"thinking_levels": ["low", "high"]}
        self.proof({"thinking": {"low": {"thinkingLevel": "LOW"}, "medium": {"thinkingLevel": "MEDIUM"}}})
        route = self.route(effort="low")
        self.assertEqual(route.prepare(BODY)["generationConfig"]["thinkingConfig"], {"thinkingLevel": "LOW"})
        self.error("unsupported_reasoning_effort", lambda: self.route(effort="medium"))
        data = json.loads(Path(os.environ["AISTUDIO_HTTP_EVIDENCE_FILE"]).read_text())
        del data["models"]["build/" + MODEL]
        Path(os.environ["AISTUDIO_HTTP_EVIDENCE_FILE"]).write_text(json.dumps(data))
        self.error("unsupported_reasoning_effort", lambda: self.route("build-" + MODEL, "low"))


class MixerAdapterHTTPTests(AdapterFixture):
    # Use the same synthetic backend and exercise the actual public HTTP handler.
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), MixerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def setUp(self):
        super().setUp()
        config = mock.patch.dict(CONFIG, {"api_keys": ["synthetic-mixer-key"], "log_requests": False})
        config.start()
        self.addCleanup(config.stop)
        os.environ["MIXER_WEB_ENABLED"] = "false"

    def call(self, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None,
                           {"Authorization": "Bearer synthetic-mixer-key", "Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def test_three_api_formats_preserve_channel_and_usage_unknown(self):
        for path, body in (("/v1/chat/completions", {"model": "ais-" + MODEL, "messages": [{"role": "user", "content": "hi"}]}),
                           ("/v1/responses", {"model": "ais-" + MODEL, "input": "hi"}),
                           ("/v1beta/models/ais-" + MODEL + ":generateContent", BODY)):
            status, headers, raw = self.call(path, body)
            self.assertEqual(status, 200, raw)
            self.assertEqual(headers["X-Mixer-Transport"], "aistudio-http")
            value = json.loads(raw)
            self.assertEqual(value["x_mixer"]["upstream_channel"], "playground")
            self.assertIsNone(value.get("usage"))
        self.assertTrue(all(r.headers["Authorization"] == "Bearer synthetic-sidecar-key" for r in self.requests))

    def test_three_stream_formats_and_catalogue_capability_methods(self):
        status, _, raw = self.call("/v1beta/models")
        self.assertEqual(status, 200)
        self.assertTrue(all(m["supportedGenerationMethods"] == ["generateContent"] for m in json.loads(raw)["models"]))
        self.proof()
        self.result = lambda request: self.response(request, headers={"Content-Type": "text/event-stream"}, content=event(FINAL))
        for path, body, terminal in (("/v1/chat/completions", {"model": "ais-" + MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}, "[DONE]"),
                                     ("/v1/responses", {"model": "ais-" + MODEL, "input": "hi", "stream": True}, "response.completed"),
                                     ("/v1beta/models/ais-" + MODEL + ":streamGenerateContent?alt=sse", BODY, "finishReason")):
            status, headers, raw = self.call(path, body)
            self.assertEqual(status, 200, raw)
            self.assertEqual(headers["Content-Type"], "text/event-stream")
            self.assertIn(terminal, raw)
        self.assertTrue(all(m["supportedGenerationMethods"] == ["generateContent", "streamGenerateContent"]
                            for m in json.loads(self.call("/v1beta/models")[2])["models"]))

    def test_http_failure_source_is_correct_and_stream_failure_has_no_success(self):
        with mock.patch.dict(os.environ, {"AISTUDIO_HTTP_API_KEY": ""}):
            status, headers, raw = self.call("/v1/responses", {"model": "ais-" + MODEL, "input": "hi"})
            self.assertEqual(status, 503)
            self.assertEqual(headers["X-Mixer-Transport"], "aistudio-http")
        self.proof()
        partial = {"candidates": [{"content": {"parts": [{"text": "part"}]}}]}
        self.result = lambda request: self.response(request, headers={"Content-Type": "text/event-stream"}, content=event(partial))
        status, _, raw = self.call("/v1/responses", {"model": "ais-" + MODEL, "input": "hi", "stream": True})
        self.assertEqual(status, 200)
        self.assertIn("response.failed", raw)
        self.assertNotIn("response.completed", raw)
        self.assertNotIn("[DONE]", raw)
