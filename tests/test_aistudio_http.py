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
ARGON_MODEL = "gemini-4-argon"
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
        if request.method == "POST":
            level = json.loads(request.content).get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel")
            if level:
                headers["X-Mixer-Thinking-Level"] = level
                headers["X-Mixer-Thinking-Validation"] = "account-catalog"
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

    def argon_level_proof(self, efforts=("low", "medium", "high")):
        # Entirely synthetic evidence: this exercises routing, not the actual
        # upstream account's model access or thinking support.
        os.environ["AIS_MODELS"] = ARGON_MODEL
        self.entries = [{"id": ARGON_MODEL, "supported_generation_methods": ["generateContent"],
                         "channels": ["playground", "build"],
                         "capabilities": {"chat_model": True, "thinking_level": True},
                         "capability_options": {"thinking_levels": ["low", "medium", "high"]}}]
        self.proof(models={"ais/" + ARGON_MODEL: {
            "text": True, "streaming": True, "parameters": [],
            "thinking": {effort: {"thinkingLevel": effort.upper()} for effort in efforts},
        }})
        adapter._CACHE.clear()

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

    def test_argon_catalogue_advertises_only_exact_channel_level_evidence(self):
        self.argon_level_proof()
        os.environ["BUILD_MODELS"] = ARGON_MODEL
        status, _, raw = self.call("/v1/models")
        self.assertEqual(status, 200, raw)
        models = {entry["id"]: entry for entry in json.loads(raw)["data"]}
        expected = {"ais-" + ARGON_MODEL, "build-" + ARGON_MODEL}
        expected.update("ais-" + ARGON_MODEL + "-" + level for level in ("low", "medium", "high"))
        self.assertEqual(set(models), expected)
        for level in ("low", "medium", "high"):
            row = models["ais-" + ARGON_MODEL + "-" + level]
            self.assertEqual(row["capabilities"]["thinking_efforts"], ["low", "medium", "high"])
            self.assertEqual(row["source"], "ais")
            self.assertEqual(row["transport"], "aistudio-http")
            self.assertEqual(row["upstream_model"], ARGON_MODEL)
            self.assertEqual(row["metadata"]["upstream_channel"], "playground")
            self.assertEqual(row["metadata"]["requested_effort"], level)
            self.assertEqual(row["metadata"]["effective_thinking_level"], level)
            self.assertNotIn("effective_thinking_budget", row["metadata"])
        self.assertEqual(models["build-" + ARGON_MODEL]["capabilities"]["thinking_efforts"], [])
        self.assertNotIn("effective_thinking_level", models["ais-" + ARGON_MODEL]["metadata"])
        self.assertTrue(all(request.method == "GET" for request in self.requests))

    def test_argon_three_levels_reach_backend_from_alias_chat_responses_and_native(self):
        self.argon_level_proof()
        base_alias = "ais-" + ARGON_MODEL
        for level in ("low", "medium", "high"):
            for expression in ("alias", "chat_effort", "responses_effort", "native_level"):
                for stream in (False, True):
                    with self.subTest(level=level, expression=expression, stream=stream):
                        alias = base_alias + "-" + level if expression == "alias" else base_alias
                        if expression in ("alias", "chat_effort"):
                            path = "/v1/chat/completions"
                            body = {"model": alias, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
                            if expression == "chat_effort":
                                body["reasoning_effort"] = level
                        elif expression == "responses_effort":
                            path = "/v1/responses"
                            body = {"model": alias, "input": "hi", "reasoning": {"effort": level}, "stream": stream}
                        else:
                            operation = "streamGenerateContent?alt=sse" if stream else "generateContent"
                            path = "/v1beta/models/" + alias + ":" + operation
                            body = dict(BODY, generationConfig={"thinkingConfig": {"thinkingLevel": level}})
                        if stream:
                            self.result = lambda request: self.response(request, headers={"Content-Type": "text/event-stream"}, content=event(FINAL))
                        else:
                            self.result = lambda request: self.response(request, json=FINAL)
                        with mock.patch("gemini_web2api.mixer.log") as logs:
                            status, headers, raw = self.call(path, body)
                        self.assertEqual(status, 200, raw)
                        self.assertEqual(headers["X-Mixer-Source"], "ais")
                        self.assertEqual(headers["X-Mixer-Transport"], "aistudio-http")
                        expected_requested = None if expression == "native_level" else level
                        self.assertEqual(headers.get("X-Mixer-Requested-Effort"), expected_requested)
                        wire = self.requests[-1]
                        operation = "streamGenerateContent" if stream else "generateContent"
                        self.assertEqual(wire.method, "POST")
                        self.assertEqual(wire.url.path, "/v1beta/models/" + ARGON_MODEL + ":" + operation)
                        self.assertEqual(wire.headers[adapter.CHANNEL_HEADER], "playground")
                        sent = json.loads(wire.content)
                        self.assertEqual(sent["generationConfig"]["thinkingConfig"], {"thinkingLevel": level.upper()})
                        self.assertNotIn("reasoning_effort", sent)
                        self.assertNotIn("reasoning", sent)
                        if stream:
                            payloads = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
                            self.assertTrue(payloads)
                        else:
                            payloads = [json.loads(raw)]
                            if expression != "native_level":
                                self.assertEqual(payloads[0]["model"], alias)
                        for payload in payloads:
                            metadata = payload["x_mixer"]
                            self.assertEqual(metadata["source"], "ais")
                            self.assertEqual(metadata["upstream_channel"], "playground")
                            self.assertEqual(metadata["upstream_model"], ARGON_MODEL)
                            self.assertEqual(metadata["requested_effort"], expected_requested)
                            self.assertEqual(metadata["effective_thinking_level"], level)
                            self.assertNotIn("effective_thinking_budget", metadata)
                        route_logs = [args[0] for args, _ in logs.call_args_list if args and str(args[0]).startswith("route ")]
                        self.assertEqual(len(route_logs), 1)
                        self.assertIn("thinking_level=" + level, route_logs[0])
                        self.assertIn("source=ais", route_logs[0])
                        self.assertIn("upstream_channel=playground", route_logs[0])

    def test_argon_missing_level_or_conflicting_controls_fail_before_generation(self):
        self.argon_level_proof(("low", "high"))
        os.environ["BUILD_MODELS"] = ARGON_MODEL
        alias = "ais-" + ARGON_MODEL
        calls = [
            ("/v1/chat/completions", {"model": alias + "-medium", "messages": [{"role": "user", "content": "hi"}]}, "unsupported_reasoning_effort"),
            ("/v1/chat/completions", {"model": alias, "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "medium"}, "unsupported_reasoning_effort"),
            ("/v1/responses", {"model": alias, "input": "hi", "reasoning": {"effort": "medium"}}, "unsupported_reasoning_effort"),
            ("/v1beta/models/" + alias + ":generateContent", dict(BODY, generationConfig={"thinkingConfig": {"thinkingLevel": "MEDIUM"}}), "unsupported_reasoning_effort"),
            ("/v1/chat/completions", {"model": alias + "-low", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}, "thinking_conflict"),
            ("/v1/responses", {"model": alias + "-low", "input": "hi", "reasoning": {"effort": "high"}}, "thinking_conflict"),
            ("/v1beta/models/" + alias + "-low:generateContent", dict(BODY, generationConfig={"thinkingConfig": {"thinkingLevel": "LOW"}}), "thinking_conflict"),
            ("/v1/responses", {"model": "build-" + ARGON_MODEL, "input": "hi", "reasoning": {"effort": "low"}}, "unsupported_reasoning_effort"),
        ]
        for path, body, code in calls:
            with self.subTest(path=path, body=body):
                status, headers, raw = self.call(path, body)
                self.assertEqual(status, 400, raw)
                self.assertEqual(json.loads(raw)["error"]["code"], code)
                self.assertEqual(headers["X-Mixer-Transport"], "aistudio-http")
                self.assertEqual(headers["X-Mixer-Source"], "build" if body.get("model", "").startswith("build-") else "ais")
        self.assertTrue(all(request.method == "GET" for request in self.requests))
        models = json.loads(self.call("/v1/models")[2])["data"]
        self.assertFalse(any(row["id"].endswith("-medium") for row in models))

    def test_argon_catalogue_denial_or_mislabeled_mapping_cannot_enable_a_level(self):
        self.argon_level_proof()
        self.entries[0]["capability_options"] = {"thinking_levels": ["low", "high"]}
        status, _, raw = self.call("/v1/responses", {
            "model": "ais-" + ARGON_MODEL, "input": "hi", "reasoning": {"effort": "medium"}})
        self.assertEqual(status, 400, raw)
        self.assertEqual(json.loads(raw)["error"]["code"], "unsupported_reasoning_effort")
        self.argon_level_proof()
        data = json.loads(Path(os.environ["AISTUDIO_HTTP_EVIDENCE_FILE"]).read_text())
        data["models"]["ais/" + ARGON_MODEL]["thinking"]["low"] = {"thinkingLevel": "HIGH"}
        Path(os.environ["AISTUDIO_HTTP_EVIDENCE_FILE"]).write_text(json.dumps(data))
        status, _, raw = self.call("/v1/chat/completions", {
            "model": "ais-" + ARGON_MODEL + "-low", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400, raw)
        self.assertEqual(json.loads(raw)["error"]["code"], "unsupported_reasoning_effort")
        rows = json.loads(self.call("/v1/models")[2])["data"]
        self.assertFalse(any(row["id"].endswith("-low") for row in rows))
        self.assertTrue(all(request.method == "GET" for request in self.requests))

    def test_argon_unknown_or_empty_catalogue_levels_never_grant_thinking(self):
        for options in (None, {}, {"thinking_levels": []}):
            with self.subTest(options=options):
                self.argon_level_proof()
                if options is None:
                    del self.entries[0]["capability_options"]
                else:
                    self.entries[0]["capability_options"] = options
                rows = json.loads(self.call("/v1/models")[2])["data"]
                self.assertEqual([row["id"] for row in rows], ["ais-" + ARGON_MODEL])
                self.assertEqual(rows[0]["capabilities"]["thinking_efforts"], [])
                for body in ({"model": "ais-" + ARGON_MODEL + "-low", "input": "hi"},
                             {"model": "ais-" + ARGON_MODEL, "input": "hi", "reasoning": {"effort": "low"}}):
                    status, _, raw = self.call("/v1/responses", body)
                    self.assertEqual(status, 400, raw)
                    self.assertEqual(json.loads(raw)["error"]["code"], "unsupported_reasoning_effort")
                status, _, raw = self.call("/v1beta/models/ais-" + ARGON_MODEL + ":generateContent",
                                           dict(BODY, generationConfig={"thinkingConfig": {"thinkingLevel": "LOW"}}))
                self.assertEqual(status, 400, raw)
                self.assertEqual(json.loads(raw)["error"]["code"], "unsupported_reasoning_effort")
        self.assertTrue(all(request.method == "GET" for request in self.requests))

    def test_argon_missing_or_wrong_account_thinking_acknowledgement_rejects_success(self):
        self.argon_level_proof()
        cases = ({}, {"X-Mixer-Thinking-Level": "LOW"},
                 {"X-Mixer-Thinking-Validation": "account-catalog"},
                 {"X-Mixer-Thinking-Level": "HIGH", "X-Mixer-Thinking-Validation": "account-catalog"},
                 {"X-Mixer-Thinking-Level": "LOW", "X-Mixer-Thinking-Validation": "unverified"})
        for stream in (False, True):
            for acknowledgement in cases:
                with self.subTest(stream=stream, acknowledgement=acknowledgement):
                    # Bypass the normal fixture's successful acknowledgement:
                    # channel locking alone cannot confirm the requested level.
                    headers = {adapter.CHANNEL_HEADER: "playground", "X-Mixer-Channel-Locked": "1"}
                    headers.update(acknowledgement)
                    if stream:
                        headers["Content-Type"] = "text/event-stream"
                        self.result = lambda request, headers=headers: httpx.Response(200, headers=headers, content=event(FINAL))
                    else:
                        self.result = lambda request, headers=headers: httpx.Response(200, headers=headers, json=FINAL)
                    status, result_headers, raw = self.call("/v1/chat/completions", {
                        "model": "ais-" + ARGON_MODEL + "-low", "messages": [{"role": "user", "content": "hi"}], "stream": stream})
                    self.assertEqual(status, 502, raw)
                    self.assertEqual(json.loads(raw)["error"]["code"], "upstream_thinking_not_verified")
                    self.assertEqual(result_headers["X-Mixer-Source"], "ais")
                    self.assertEqual(result_headers["X-Mixer-Transport"], "aistudio-http")
                    self.assertNotIn("[DONE]", raw)
                    self.assertNotIn("synthetic-sidecar-key", raw)
                    self.assertNotIn("synthetic-mixer-key", raw)
