"""Public providers use synthetic credentials and an in-memory HTTP transport."""
import base64
import json
import os
import threading
import unittest
from unittest import mock

import httpx

from gemini_web2api import providers


BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
FINAL = {"candidates": [{"content": {"role": "model", "parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2}}


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


def event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {
            "BUILD_API_KEY": "synthetic-build-key", "AIS_API_KEY": "synthetic-ais-key",
            "BUILD_MODELS": "gemini-2.5-flash", "AIS_MODELS": "gemini-2.5-flash",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        providers._DISCOVERY_CACHE.clear()

    def transport(self, handler):
        transport = httpx.MockTransport(handler)
        factory = mock.patch.object(providers, "_new_client", side_effect=lambda timeout: httpx.Client(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False))
        factory.start()
        self.addCleanup(factory.stop)

    def route(self, alias="build-gemini-2.5-flash", effort=None):
        return providers.resolve_route(alias, effort)

    def assert_error(self, code, operation, status=None):
        with self.assertRaises(providers.ProviderError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        if status:
            self.assertEqual(raised.exception.status, status)
        return raised.exception

    def test_legacy_names_return_no_route(self):
        for value in ("gemini-3.6-flash", "unknown", None, 12):
            self.assertIsNone(providers.resolve_route(value))

    def test_reserved_alias_does_not_fall_back_to_web(self):
        for value in ("build-", "ais-unknown", "build-https://evil.example/model", "build-gemini-2.5-flash@think=0"):
            self.assert_error("unknown_provider_model", lambda: self.route(value), 404)

    def test_missing_key_is_service_configuration_error(self):
        del os.environ["BUILD_API_KEY"]
        self.assert_error("provider_not_configured", self.route, 503)

    def test_capabilities_and_provenance_are_explicit(self):
        route = self.route("ais-gemini-2.5-flash-high")
        self.assertEqual(route.source, "ais")
        self.assertEqual(route.model, "gemini-2.5-flash")
        self.assertEqual(route.effort, "high")
        self.assertEqual(route.meta["effective_thinking_budget"], 24576)
        self.assertEqual(route.meta["transport"], "google-gemini-public-api")
        self.assertFalse(route.meta["webpage_executor"])
        self.assertNotIn("synthetic", repr(route))
        self.assertTrue(route.capabilities["image_input"])
        self.assertTrue(route.capabilities["tools"])

    def test_all_reviewed_thinking_mappings(self):
        for model in ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"):
            os.environ["BUILD_MODELS"] = model
            for effort, budget in (("low", 1024), ("medium", 8192), ("high", 24576)):
                route = self.route("build-" + model + "-" + effort)
                self.assertEqual(route.prepare(BODY)["generationConfig"]["thinkingConfig"], {"thinkingBudget": budget})
        for model in ("gemini-3-pro-preview", "gemini-3.1-pro-preview", "gemini-3-flash-preview"):
            os.environ["BUILD_MODELS"] = model
            for effort in ("low", "medium", "high"):
                if model == "gemini-3-pro-preview" and effort == "medium":
                    self.assert_error("unsupported_reasoning_effort", lambda: self.route("build-" + model, effort), 400)
                else:
                    route = self.route("build-" + model, effort)
                    self.assertEqual(route.prepare(BODY)["generationConfig"]["thinkingConfig"], {"thinkingLevel": effort.upper()})

    def test_effort_parameters_cannot_conflict_or_be_silently_ignored(self):
        self.assert_error("thinking_conflict", lambda: self.route("build-gemini-2.5-flash-low", "high"), 400)
        for effort in ("minimal", "none", "HIGH", 1, []):
            self.assert_error("unsupported_reasoning_effort", lambda: self.route(effort=effort), 400)
        route = self.route("build-gemini-2.5-flash-low")
        body = dict(BODY, generationConfig={"thinkingConfig": {"thinkingBudget": 8192}})
        self.assert_error("thinking_conflict", lambda: route.prepare(body), 400)

    def test_prepare_copies_input_and_preserves_include_thoughts(self):
        body = dict(BODY, generationConfig={"thinkingConfig": {"includeThoughts": True}})
        route = self.route(effort="medium")
        prepared = route.prepare(body)
        self.assertEqual(prepared["generationConfig"]["thinkingConfig"], {"includeThoughts": True, "thinkingBudget": 8192})
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"includeThoughts": True})

    def test_native_thinking_budget_and_level_are_validated_and_reported(self):
        route = self.route()
        route.prepare(dict(BODY, generationConfig={"thinkingConfig": {"thinkingBudget": 777}}))
        self.assertEqual(route.meta["effective_thinking_budget"], 777)
        for budget in (True, "1024", -2, 30000):
            body = dict(BODY, generationConfig={"thinkingConfig": {"thinkingBudget": budget}})
            self.assert_error("unsupported_thinking_budget", lambda: route.prepare(body), 400)
        os.environ["BUILD_MODELS"] = "gemini-3-pro-preview"
        route = self.route("build-gemini-3-pro-preview")
        prepared = route.prepare(dict(BODY, generationConfig={"thinkingConfig": {"thinkingLevel": "low"}}))
        self.assertEqual(prepared["generationConfig"]["thinkingConfig"]["thinkingLevel"], "LOW")
        self.assertEqual(route.meta["effective_thinking_level"], "low")

    def test_unknown_allowlisted_models_do_not_claim_images_tools_or_efforts(self):
        os.environ["BUILD_MODELS"] = "gemini-future-model"
        route = self.route("build-gemini-future-model")
        self.assertFalse(route.capabilities["image_input"])
        self.assertFalse(route.capabilities["tools"])
        self.assertEqual(route.capabilities["thinking_efforts"], [])
        route.prepare(BODY)
        self.assert_error("unsupported_reasoning_effort", lambda: self.route("build-gemini-future-model-low"), 400)
        image_body = {"contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": "eA=="}}]}]}
        self.assert_error("unsupported_image_input", lambda: route.prepare(image_body), 400)
        self.assert_error("unsupported_tools", lambda: route.prepare(dict(BODY, tools=[{"functionDeclarations": []}])), 400)

    def test_unconfigured_or_malformed_allowlist_is_not_exposed(self):
        os.environ["BUILD_MODELS"] = "https://attacker.invalid/model"
        self.assert_error("provider_configuration_error", self.route, 503)
        del os.environ["AIS_API_KEY"]
        self.assertEqual(providers.list_models(), [])

    def test_request_uses_fixed_https_host_secret_header_and_native_thinking(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=FINAL)
        self.transport(handler)
        result = self.route("ais-gemini-2.5-flash-high").generate(BODY)
        self.assertEqual(result, FINAL)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertEqual(request.headers["x-goog-api-key"], "synthetic-ais-key")
        self.assertNotIn("key=", str(request.url))
        self.assertEqual(json.loads(request.content)["generationConfig"]["thinkingConfig"], {"thinkingBudget": 24576})
        self.assertEqual(request.extensions["timeout"]["connect"], 10)
        self.assertEqual(request.extensions["timeout"]["read"], 60)

    def test_default_client_disables_ambient_proxy_redirects_and_keeps_tls(self):
        with mock.patch.object(providers.httpx, "Client") as client:
            providers._new_client(httpx.Timeout(1))
            self.assertEqual(client.call_args.kwargs["follow_redirects"], False)
            self.assertEqual(client.call_args.kwargs["trust_env"], False)
            self.assertEqual(client.call_args.kwargs["verify"], True)

    def test_native_image_function_calls_and_signatures_survive(self):
        image = {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"synthetic image").decode()}}
        body = {"contents": [{"role": "user", "parts": [{"text": "describe"}, image]},
                             {"role": "model", "parts": [{"functionCall": {"name": "weather", "args": {"city": "Paris"}}, "thoughtSignature": "synthetic-signature"}]},
                             {"role": "user", "parts": [{"functionResponse": {"name": "weather", "response": {"temperature": 1}}}]}],
                "tools": [{"functionDeclarations": [{"name": "weather", "parameters": {"type": "OBJECT"}}]}]}
        self.transport(lambda request: httpx.Response(200, json=FINAL) if json.loads(request.content)["contents"] == body["contents"] else httpx.Response(400))
        self.assertEqual(self.route().generate(body), FINAL)

    def test_unsupported_media_and_server_tools_are_explicit_errors(self):
        route = self.route()
        self.assert_error("unsupported_file_input", lambda: route.prepare({"contents": [{"parts": [{"fileData": {"fileUri": "http://127.0.0.1/secret"}}]}]}), 400)
        self.assert_error("unsupported_tools", lambda: route.prepare(dict(BODY, tools=[{"googleSearch": {}}])), 400)
        self.assert_error("unsupported_output_modality", lambda: route.prepare(dict(BODY, generationConfig={"responseModalities": ["IMAGE"]})), 400)
        self.assert_error("invalid_image_input", lambda: route.prepare({"contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": "!not-base64!"}}]}]}), 400)

    def test_error_status_and_embedded_errors_never_relay_raw_text(self):
        for status, code in ((400, "upstream_invalid_request"), (401, "upstream_authentication_failed"),
                             (403, "upstream_authentication_failed"), (404, "upstream_model_not_found"),
                             (429, "upstream_rate_limited"), (503, "upstream_error")):
            with self.subTest(status=status):
                self.transport(lambda request, status=status: httpx.Response(status, json={"error": {"message": "synthetic-build-key sensitive"}}))
                error = self.assert_error(code, lambda: self.route().generate(BODY))
                self.assertNotIn("sensitive", str(error))
                self.assertNotIn("synthetic", str(error))
        self.transport(lambda request: httpx.Response(200, json={"error": {"code": 429, "message": "synthetic-build-key sensitive"}}))
        self.assert_error("upstream_rate_limited", lambda: self.route().generate(BODY), 429)

    def test_redirect_is_not_followed_or_retried(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(307, headers={"Location": "https://attacker.invalid/steal"})
        self.transport(handler)
        self.assert_error("upstream_redirect_rejected", lambda: self.route().generate(BODY), 502)
        self.assertEqual(len(requests), 1)

    def test_timeout_and_connection_failures_are_sanitized_without_retries(self):
        for exception, code, status in ((httpx.ReadTimeout("synthetic-build-key"), "upstream_timeout", 504),
                                        (httpx.ConnectError("synthetic-build-key"), "upstream_connection_error", 502)):
            requests = []
            def handler(request):
                requests.append(request)
                raise exception
            self.transport(handler)
            error = self.assert_error(code, lambda: self.route().generate(BODY), status)
            self.assertEqual(len(requests), 1)
            self.assertNotIn("synthetic", str(error))

    def test_body_and_response_limits(self):
        os.environ["PROVIDER_MAX_REQUEST_BYTES"] = "1024"
        self.assert_error("request_too_large", lambda: self.route().prepare({"contents": [{"parts": [{"text": "x" * 2000}]}]}), 413)
        os.environ["PROVIDER_MAX_RESPONSE_BYTES"] = "1024"
        self.transport(lambda request: httpx.Response(200, content=b"x" * 2000))
        self.assert_error("upstream_response_too_large", lambda: self.route().generate(BODY), 502)

    def test_json_errors_are_sanitized(self):
        for content in (b"not json synthetic-build-key", b"[]"):
            self.transport(lambda request, content=content: httpx.Response(200, content=content))
            self.assert_error("upstream_invalid_response", lambda: self.route().generate(BODY), 502)

    def test_stream_preserves_native_fragments_tools_signatures_usage_and_closes(self):
        first = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "f", "args": {}}, "thoughtSignature": "synthetic-signature"}]}}]}
        payload = b": heartbeat\r\n\r\n" + event(first) + event(FINAL)
        stream = Chunks([payload[:7], payload[7:40], payload[40:]])
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        self.transport(handler)
        self.assertEqual(list(self.route().stream(BODY)), [first, FINAL])
        self.assertTrue(stream.closed)
        self.assertEqual(requests[0].url.params["alt"], "sse")
        self.assertNotIn("key", requests[0].url.params)

    def test_stream_delivers_small_event_before_later_transport_failure(self):
        first = {"candidates": [{"content": {"parts": [{"text": "a"}]}}]}
        stream = Chunks([event(first), httpx.ReadTimeout("synthetic-build-key")])
        self.transport(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
        iterator = self.route().stream(BODY)
        self.assertEqual(next(iterator), first)
        error = self.assert_error("upstream_timeout", lambda: next(iterator), 504)
        self.assertNotIn("synthetic", str(error))
        self.assertTrue(stream.closed)

    def test_stream_rejects_embedded_error_and_incomplete_eof(self):
        for payload, code in ((event({"error": {"code": 429, "message": "synthetic-build-key"}}), "upstream_rate_limited"),
                              (event({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}), "upstream_stream_incomplete"),
                              (b"data: broken json\n\n", "upstream_invalid_response")):
            self.transport(lambda request, payload=payload: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=payload))
            self.assert_error(code, lambda: list(self.route().stream(BODY)))

    def test_stream_safety_refusal_is_a_final_native_result(self):
        blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
        self.transport(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=event(blocked)))
        self.assertEqual(list(self.route().stream(BODY)), [blocked])

    def test_stream_line_limit_and_consumer_cancellation_close_connection(self):
        os.environ["PROVIDER_MAX_SSE_LINE_BYTES"] = "256"
        self.transport(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"data: " + b"x" * 300))
        self.assert_error("upstream_sse_line_too_large", lambda: list(self.route().stream(BODY)), 502)
        stream = Chunks([event({"candidates": [{"content": {"parts": [{"text": "a"}]}}]}), event(FINAL)])
        self.transport(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
        iterator = self.route().stream(BODY)
        next(iterator)
        iterator.close()
        self.assertTrue(stream.closed)

    def test_stream_non_sse_is_not_silently_accepted(self):
        self.transport(lambda request: httpx.Response(200, json=FINAL))
        self.assert_error("upstream_invalid_stream", lambda: list(self.route().stream(BODY)), 502)

    def test_total_deadline_is_checked_between_chunks(self):
        with mock.patch.object(providers.time, "monotonic", side_effect=[0, 181]):
            self.transport(lambda request: httpx.Response(200, json=FINAL))
            self.assert_error("upstream_timeout", lambda: self.route().generate(BODY), 504)

    def test_total_deadline_closes_transport_when_stream_stalls(self):
        closed = threading.Event()

        class StalledStream(httpx.SyncByteStream):
            def __iter__(self):
                if not closed.wait(1):
                    raise AssertionError("The total deadline did not close the transport")
                yield event(FINAL)

        class StalledTransport(httpx.BaseTransport):
            def handle_request(self, request):
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=StalledStream())

            def close(self):
                closed.set()

        os.environ["PROVIDER_TOTAL_TIMEOUT_SEC"] = "0.05"
        with mock.patch.object(providers, "_new_client", side_effect=lambda timeout: httpx.Client(
                transport=StalledTransport(), timeout=timeout, trust_env=False)):
            self.assert_error("upstream_timeout", lambda: list(self.route().stream(BODY)), 504)
        self.assertTrue(closed.is_set())

    def test_models_discovery_intersects_allowlist_pages_caches_and_marks_origin(self):
        os.environ["BUILD_MODELS"] = "gemini-2.5-flash,gemini-3-pro-preview,gemini-future"
        del os.environ["AIS_API_KEY"]
        requests = []
        def handler(request):
            requests.append(request)
            if "pageToken" not in request.url.params:
                return httpx.Response(200, json={"models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"], "thinking": True},
                    {"name": "models/not-allowed", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-future", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "next"})
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3-pro-preview", "supportedGenerationMethods": ["generateContent"]}]})
        self.transport(handler)
        first = providers.list_models()
        self.assertEqual({row["id"] for row in first}, {
            "build-gemini-2.5-flash", "build-gemini-2.5-flash-low", "build-gemini-2.5-flash-medium", "build-gemini-2.5-flash-high",
            "build-gemini-3-pro-preview", "build-gemini-3-pro-preview-low", "build-gemini-3-pro-preview-high"})
        self.assertEqual(providers.list_models(), first)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(row["source"] == "build" and row["discovered"] and row["transport"] == providers.TRANSPORT for row in first))
        self.assertNotIn("synthetic", json.dumps(first))
        self.assertEqual(requests[0].headers["x-goog-api-key"], "synthetic-build-key")

    def test_discovery_failure_missing_keys_and_thinking_false_do_not_advertise(self):
        self.transport(lambda request: httpx.Response(403, json={"error": {"message": "synthetic-build-key"}}))
        self.assertEqual(providers.list_models(), [])
        providers._DISCOVERY_CACHE.clear()
        self.transport(lambda request: httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"], "thinking": False}]}))
        result = providers.list_models()
        self.assertEqual({row["id"] for row in result}, {"build-gemini-2.5-flash", "ais-gemini-2.5-flash"})
        self.assertTrue(all(row["capabilities"]["thinking_efforts"] == [] for row in result))
        del os.environ["BUILD_API_KEY"]
        del os.environ["AIS_API_KEY"]
        self.assertEqual(providers.list_models(), [])

    def test_discovery_timeout_and_malformed_pagination_fail_closed(self):
        def timeout(request):
            raise httpx.ReadTimeout("synthetic-build-key")
        self.transport(timeout)
        self.assertEqual(providers.list_models(), [])
        providers._DISCOVERY_CACHE.clear()
        self.transport(lambda request: httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "repeated"}))
        self.assertEqual(providers.list_models(), [])


if __name__ == "__main__":
    unittest.main()
