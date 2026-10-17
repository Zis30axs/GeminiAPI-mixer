"""Live-probe behavior is tested entirely against a synthetic HTTP transport."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import httpx


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "verify_aistudio_http.py"
SPEC = importlib.util.spec_from_file_location("verify_aistudio_http", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

MODEL = "gemini-example"
KEY = "synthetic-private-service-key"
BASE = "https://sidecar.example"
ENV = {"AISTUDIO_HTTP_BASE_URL": BASE, "AISTUDIO_HTTP_API_KEY": KEY}


def native(text="", finish=None):
    candidate = {"content": {"parts": [{"text": text}]}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


def event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def __iter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "proof.json"
        self.requests, self.streams = [], []
        self.behavior = self.success
        self.thinking_level = None
        self.catalog = {"object": "list", "data": [{"id": MODEL, "channels": ["playground", "build"],
                         "capabilities": {"thinking_level": True},
                         "capability_options": {"thinking_levels": ["minimal", "low", "medium", "high"]}}]}
        self.catalog_behavior = None

    def factory(self, **kwargs):
        self.assertFalse(kwargs["follow_redirects"])
        self.assertFalse(kwargs["trust_env"])
        self.assertTrue(kwargs["verify"])
        return httpx.Client(transport=httpx.MockTransport(self.handle), **kwargs)

    def handle(self, request):
        self.requests.append(request)
        self.assertEqual(request.headers["Authorization"], "Bearer " + KEY)
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual((request.url.scheme, request.url.host, request.url.port), ("https", "sidecar.example", None))
        if request.method == "GET":
            self.assertEqual(request.url.path, "/v1/models")
            self.assertEqual(request.content, b"")
            return (self.catalog_behavior(request) if self.catalog_behavior else
                    self.response(request, json=self.catalog))
        expected_body = verify.BODY if self.thinking_level is None else dict(
            verify.BODY, generationConfig={"thinkingConfig": {"thinkingLevel": self.thinking_level.upper()}})
        self.assertEqual(json.loads(request.content), expected_body)
        return self.behavior(request)

    def test_long_generation_wait_uses_the_requested_deadline(self):
        received = []
        def factory(**kwargs):
            received.append(kwargs["timeout"])
            return self.factory(**kwargs)
        result = verify.probe(BASE, KEY, MODEL, "ais", False, 90, factory)
        self.assertTrue(result["ok"])
        self.assertEqual(received[0].read, 90)
        self.assertEqual(received[0].connect, 10)

    def response(self, request, **kwargs):
        headers = {"X-Mixer-Upstream-Channel": request.headers["X-Mixer-Upstream-Channel"],
                   "X-Mixer-Channel-Locked": "1"}
        if self.thinking_level is not None:
            headers.update({"X-Mixer-Thinking-Level": self.thinking_level.upper(),
                            "X-Mixer-Thinking-Validation": "account-catalog"})
        headers.update(kwargs.pop("headers", {}))
        return httpx.Response(kwargs.pop("status", 200), headers=headers, **kwargs)

    def success(self, request):
        if request.url.path.endswith(":generateContent"):
            return self.response(request, json=native("1. ready\n2. ready", "STOP"))
        source = Stream([event(native("1. ready\n")), event(native("2. ready\n")), event(native(finish="STOP"))])
        self.streams.append(source)
        return self.response(request, headers={"content-type": "text/event-stream"}, stream=source)

    def run_probe(self, sources=("ais", "build"), output=True, **kwargs):
        return verify.run(MODEL, list(sources), 1, ENV, str(self.output) if output else None,
                          client_factory=self.factory, **kwargs)

    def test_two_sources_receive_exact_channel_pins_and_only_basic_evidence(self):
        result = self.run_probe()
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([r.headers["X-Mixer-Upstream-Channel"] for r in self.requests], ["playground", "playground", "build", "build"])
        self.assertTrue(all(s.closed for s in self.streams))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn("ready", json.dumps(result))
        proof = json.loads(self.output.read_text())
        self.assertEqual(proof["backend_base_url"], BASE)
        self.assertEqual(set(proof["models"]), {"ais/" + MODEL, "build/" + MODEL})
        for entry in proof["models"].values():
            self.assertEqual(entry, {"text": True, "streaming": True, "image_input": False, "tools": False, "parameters": [], "thinking": {}})

    def test_explicit_thinking_levels_require_catalog_and_native_confirmation_for_both_calls(self):
        for level in ("low", "medium", "high"):
            with self.subTest(level=level):
                self.requests.clear()
                self.thinking_level = level
                result = self.run_probe(thinking_level=level)
                self.assertTrue(result["ok"])
                self.assertEqual([r.method for r in self.requests], ["GET", "POST"] * 4)
                self.assertEqual([r.headers["X-Mixer-Upstream-Channel"] for r in self.requests],
                                 ["playground"] * 4 + ["build"] * 4)
                self.assertEqual(verify.BODY, {"contents": [{"role": "user", "parts": [{"text": verify.PROMPT}]}]})
                for item in result["results"]:
                    for mode in ("non_stream", "stream"):
                        self.assertEqual(item[mode]["thinking_level"], level)
                        self.assertEqual(item[mode]["thinking_validation"], "account-catalog")
                for entry in json.loads(self.output.read_text())["models"].values():
                    self.assertEqual(entry["thinking"], {level: {"thinkingLevel": level.upper()}})
                    self.assertNotIn("thinkingBudget", json.dumps(entry))
                self.assertNotIn(KEY, json.dumps(result))
                self.assertNotIn("ready", json.dumps(result))

    def test_thinking_catalog_missing_unsupported_or_malformed_prevents_generation(self):
        valid = copy.deepcopy(self.catalog["data"][0])
        cases = [({}, "invalid_model_catalog"),
                 ({"object": "list", "data": {}}, "invalid_model_catalog"),
                 ({"object": "model", "data": [valid]}, "invalid_model_catalog"),
                 ({"object": "list", "data": [None]}, "invalid_model_catalog"),
                 ({"object": "list", "data": [{"id": 5}]}, "invalid_model_catalog"),
                 ({"object": "list", "data": [valid, valid]}, "invalid_model_catalog"),
                 ({"object": "list", "data": []}, "upstream_model_not_found"),
                 ({"object": "list", "data": [dict(valid, id="models/" + MODEL)]}, "upstream_model_not_found")]
        for caps in (None, {}, {"thinking_level": False}, {"thinking_level": "true"}, {"thinking_level": 1}):
            cases.append(({"object": "list", "data": [dict(valid, capabilities=caps)]}, "unsupported_thinking_level"))
        for options in (None, []):
            cases.append(({"object": "list", "data": [dict(valid, capability_options=options)]}, "unsupported_thinking_level"))
        for channels in (None, [], "playground", [1], ["playground", "playground"], ["unknown"]):
            cases.append(({"object": "list", "data": [dict(valid, channels=channels)]}, "invalid_model_catalog"))
        cases.append(({"object": "list", "data": [dict(valid, channels=["build"])]}, "upstream_model_channel_unavailable"))
        for levels in (None, [], "low", ["LOW"], ["low", "low"], [1], ["low", "unknown"]):
            entry = dict(valid, capability_options={"thinking_levels": levels})
            cases.append(({"object": "list", "data": [entry]}, "invalid_thinking_catalog"))
        entry = dict(valid, capability_options={"thinking_levels": ["minimal", "low", "high"]})
        cases.append(({"object": "list", "data": [entry]}, "unsupported_thinking_level"))
        for catalog, code in cases:
            with self.subTest(catalog=catalog):
                self.catalog = catalog
                self.requests.clear()
                result = self.run_probe(sources=("ais",), thinking_level="medium")
                self.assertEqual(result["results"][0]["non_stream"]["code"], code)
                self.assertEqual([r.method for r in self.requests], ["GET"])
                self.assertFalse(result["evidence_written"])
                self.assertFalse(self.output.exists())

    def test_catalog_is_rechecked_before_stream_and_cannot_refresh_unsupported_evidence(self):
        self.thinking_level = "medium"
        calls = 0
        def catalog_response(request):
            nonlocal calls
            calls += 1
            catalog = copy.deepcopy(self.catalog)
            if calls == 2:
                catalog["data"][0]["capability_options"]["thinking_levels"] = ["low", "high"]
            return self.response(request, json=catalog)
        self.catalog_behavior = catalog_response
        result = self.run_probe(sources=("ais",), thinking_level="medium")
        self.assertEqual([r.method for r in self.requests], ["GET", "POST", "GET"])
        self.assertEqual(result["results"][0]["stream"]["code"], "unsupported_thinking_level")
        self.assertFalse(result["evidence_written"])

    def test_thinking_headers_and_channel_lock_are_required_on_both_generation_modes(self):
        self.thinking_level = "high"
        complete = {"X-Mixer-Upstream-Channel": "playground", "X-Mixer-Channel-Locked": "1",
                    "X-Mixer-Thinking-Level": "HIGH", "X-Mixer-Thinking-Validation": "account-catalog"}
        cases = [(dict(complete, **{"X-Mixer-Thinking-Level": value}), "upstream_thinking_not_confirmed")
                 for value in ("LOW", "high", "HIGH, HIGH")]
        cases += [(dict(complete, **{"X-Mixer-Thinking-Validation": value}), "upstream_thinking_not_confirmed")
                  for value in ("echo", "", "account-catalog, account-catalog")]
        for missing in ("X-Mixer-Thinking-Level", "X-Mixer-Thinking-Validation"):
            cases.append(({key: value for key, value in complete.items() if key != missing}, "upstream_thinking_not_confirmed"))
        cases.append((dict(complete, **{"X-Mixer-Channel-Locked": "0"}), "upstream_channel_not_locked"))
        for stream in (False, True):
            for headers, code in cases:
                with self.subTest(stream=stream, headers=headers):
                    def response(request):
                        if request.url.path.endswith(":streamGenerateContent") != stream:
                            return self.success(request)
                        return httpx.Response(200, headers=headers, json=native("private answer", "STOP"))
                    self.behavior = response
                    self.requests.clear()
                    result = self.run_probe(sources=("ais",), thinking_level="high")
                    self.assertEqual(result["results"][0]["stream" if stream else "non_stream"]["code"], code)
                    self.assertEqual(len(self.requests), 4 if stream else 2)
                    self.assertFalse(result["evidence_written"])
                    self.assertNotIn("private answer", json.dumps(result))

    def test_thinking_headers_do_not_turn_buffered_stream_into_evidence(self):
        self.thinking_level = "low"
        self.behavior = lambda request: (self.success(request) if request.url.path.endswith(":generateContent") else
            self.response(request, headers={"Content-Type": "text/event-stream"},
                          stream=Stream([event(native("first")) + event(native("second")) + event(native(finish="STOP"))])))
        result = self.run_probe(sources=("ais",), thinking_level="low")
        self.assertEqual(result["results"][0]["stream"]["code"], "incremental_delivery_not_observed")
        self.assertFalse(self.output.exists())

    def test_catalog_non_200_errors_never_generate_and_auth_or_quota_stop_other_sources(self):
        for status in (301, 400, 401, 403, 404, 429, 500, 504):
            with self.subTest(status=status):
                self.requests.clear()
                self.catalog_behavior = lambda request, status=status: httpx.Response(
                    status, headers={"Location": "https://other.example"}, content=KEY.encode())
                result = self.run_probe(thinking_level="low")
                self.assertEqual(result["results"][0]["non_stream"]["code"], verify.status_error(status).code)
                self.assertTrue(all(r.method == "GET" for r in self.requests))
                self.assertEqual(len(self.requests), 1 if status in (401, 403, 429) else 2)
                if status in (401, 403, 429):
                    self.assertIn(result["stopped"], verify.STOP_CODES)
                self.assertFalse(self.output.exists())
                self.assertNotIn(KEY, json.dumps(result))

    def test_generation_non_200_does_not_accept_thinking_ack_and_stops_auth_or_quota(self):
        self.thinking_level = "low"
        for status in (401, 403, 429, 500):
            self.requests.clear()
            self.behavior = lambda request, status=status: self.response(request, status=status, content=KEY.encode())
            result = self.run_probe(thinking_level="low")
            self.assertEqual(len(self.requests), 2 if status in (401, 403, 429) else 4)
            self.assertEqual(result["results"][0]["non_stream"]["code"], verify.status_error(status).code)
            self.assertFalse(result["evidence_written"])
            self.assertNotIn(KEY, json.dumps(result))

    def test_catalog_size_json_mime_and_transport_fail_closed(self):
        fixtures = [({"content": b"x" * (verify.MAX_RESPONSE + 1), "headers": {"Content-Type": "application/json"}}, "response_size_limit"),
                    ({"content": b'{"object":"list","data":[],"data":[]}', "headers": {"Content-Type": "application/json"}}, "invalid_json_response"),
                    ({"content": b'{"object":"list","data":[NaN]}', "headers": {"Content-Type": "application/json"}}, "invalid_json_response"),
                    ({"content": b'[]', "headers": {"Content-Type": "application/json"}}, "invalid_json_response"),
                    ({"json": self.catalog, "headers": {"Content-Type": "text/html"}}, "invalid_model_catalog")]
        for kwargs, code in fixtures:
            self.requests.clear()
            self.catalog_behavior = lambda request, kwargs=kwargs: self.response(request, **kwargs)
            result = self.run_probe(sources=("ais",), thinking_level="low")
            self.assertEqual(result["results"][0]["non_stream"]["code"], code)
            self.assertEqual(len(self.requests), 1)
        def failed(request):
            raise httpx.ReadTimeout(KEY)
        self.catalog_behavior = failed
        result = self.run_probe(sources=("ais",), thinking_level="low")
        self.assertEqual(result["results"][0]["non_stream"]["code"], "upstream_timeout")
        self.assertNotIn(KEY, json.dumps(result))

    def test_catalog_timeout_is_bounded_and_explicit_probe_api_keeps_existing_arguments(self):
        self.thinking_level = "high"
        def catalog_response(request):
            self.assertLessEqual(request.extensions["timeout"]["read"], verify.CATALOG_TIMEOUT)
            return self.response(request, json=self.catalog)
        self.catalog_behavior = catalog_response
        result = verify.probe(BASE, KEY, MODEL, "ais", False, 90, self.factory, "high")
        self.assertTrue(result["ok"])
        for level in ("HIGH", "minimal", "", 1):
            self.requests.clear()
            with self.assertRaises(verify.ProbeError):
                self.run_probe(thinking_level=level)
            self.assertEqual(self.requests, [])

    def test_catalog_wall_deadline_closes_transport_without_starting_generation(self):
        closed = threading.Event()
        methods = []
        class Waiting(Stream):
            def __iter__(self):
                yield b'{"object":'
                closed.wait(1)
                raise OSError(KEY)
        class ClosingTransport(httpx.BaseTransport):
            def handle_request(self, request):
                methods.append(request.method)
                return httpx.Response(200, headers={"X-Mixer-Upstream-Channel": "playground", "X-Mixer-Channel-Locked": "1",
                                                   "Content-Type": "application/json"}, stream=Waiting([]))
            def close(self):
                closed.set()
        factory = lambda **kwargs: httpx.Client(transport=ClosingTransport(), **kwargs)
        result = verify.probe(BASE, KEY, MODEL, "ais", False, .03, factory, "low")
        self.assertEqual(result["code"], "upstream_timeout")
        self.assertEqual(methods, ["GET"])
        self.assertLess(result["elapsed_seconds"], .5)
        self.assertTrue(closed.is_set())

    def test_main_thinking_argument_is_explicit_and_forwarded(self):
        output = io.StringIO()
        with mock.patch.object(verify, "run", return_value={"ok": True}) as run, contextlib.redirect_stdout(output):
            self.assertEqual(verify.main(["--model", MODEL, "--source", "build", "--thinking-level", "medium"]), 0)
        self.assertEqual(run.call_args.kwargs, {"thinking_level": "medium"})
        self.assertEqual(run.call_args.args[1], ["build"])

    def test_thinking_catalog_requires_matching_source_lock_before_generation(self):
        for headers in ({}, {"X-Mixer-Upstream-Channel": "playground"},
                        {"X-Mixer-Upstream-Channel": "build", "X-Mixer-Channel-Locked": "1"},
                        {"X-Mixer-Upstream-Channel": "playground", "X-Mixer-Channel-Locked": "0"}):
            self.requests.clear()
            self.catalog_behavior = lambda request, headers=headers: httpx.Response(200, headers=headers, json=self.catalog)
            result = self.run_probe(sources=("ais",), thinking_level="low")
            self.assertEqual(result["results"][0]["non_stream"]["code"], "upstream_channel_not_locked")
            self.assertEqual([r.method for r in self.requests], ["GET"])
            self.assertFalse(self.output.exists())

    def test_no_output_flag_never_writes_and_container_base_override_is_validated(self):
        result = self.run_probe(output=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["evidence_written"])
        self.assertFalse(self.output.exists())
        with self.assertRaises(verify.ProbeError):
            self.run_probe(evidence_base_url="http://sidecar:8080")
        result = verify.run(MODEL, ["ais"], 1, dict(ENV, AISTUDIO_HTTP_ALLOW_HTTP="true"), str(self.output),
                            "http://sidecar:8080", self.factory)
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(self.output.read_text())["backend_base_url"], "http://sidecar:8080")

    def test_quota_and_auth_failures_stop_all_subsequent_calls_without_retry(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.requests.clear()
                self.behavior = lambda request, status=status: self.response(request, status=status, content=KEY.encode())
                result = self.run_probe()
                self.assertFalse(result["ok"])
                self.assertEqual(len(self.requests), 1)
                self.assertIn(result["stopped"], verify.STOP_CODES)
                self.assertFalse(self.output.exists())
                self.assertNotIn(KEY, json.dumps(result))

    def test_partial_failure_writes_only_proven_source_without_refreshing_old_entries(self):
        self.output.write_text(json.dumps({"models": {"ais/old-model": {"thinking": {"high": {"thinkingBudget": 123}}}}}))
        self.behavior = lambda request: (self.response(request, status=500, content=b"private detail")
                                         if request.headers["X-Mixer-Upstream-Channel"] == "playground" else self.success(request))
        result = self.run_probe()
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["evidence_sources"], ["build"])
        self.assertEqual(set(json.loads(self.output.read_text())["models"]), {"build/" + MODEL})

    def test_rate_limit_inside_sse_stops_second_channel_and_writes_nothing(self):
        self.behavior = lambda request: (self.success(request) if request.url.path.endswith(":generateContent") else
            self.response(request, headers={"Content-Type": "text/event-stream"},
                          stream=Stream([event(native("partial")), event({"error": {"code": 429, "message": KEY}})])))
        result = self.run_probe()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["stopped"], "upstream_rate_limited")
        self.assertFalse(self.output.exists())
        self.assertNotIn(KEY, json.dumps(result))

    def test_complete_failure_preserves_existing_file_and_never_marks_evidence_written(self):
        self.output.write_text("existing local evidence")
        self.behavior = lambda request: self.response(request, status=503)
        result = self.run_probe()
        self.assertFalse(result["evidence_written"])
        self.assertEqual(self.output.read_text(), "existing local evidence")
        self.assertEqual(len(self.requests), 2)

    def test_channel_mismatch_or_missing_lock_prevents_evidence(self):
        for headers in ({"X-Mixer-Upstream-Channel": "build", "X-Mixer-Channel-Locked": "1"},
                        {"X-Mixer-Upstream-Channel": "playground"}, {}):
            self.behavior = lambda request, headers=headers: httpx.Response(200, headers=headers, json=native("answer", "STOP"))
            result = self.run_probe(sources=("ais",))
            self.assertEqual(result["results"][0]["non_stream"]["code"], "upstream_channel_not_locked")
            self.assertFalse(result["evidence_written"])

    def test_single_event_or_wholly_buffered_sse_is_not_incremental_evidence(self):
        fixtures = [[event(native("whole answer", "STOP"))],
                    [event(native("first")) + event(native("second")) + event(native(finish="STOP"))]]
        for chunks in fixtures:
            self.behavior = lambda request, chunks=chunks: (self.success(request) if request.url.path.endswith(":generateContent") else
                self.response(request, headers={"Content-Type": "text/event-stream"}, stream=Stream(chunks)))
            result = self.run_probe(sources=("ais",))
            self.assertEqual(result["results"][0]["stream"]["code"], "incremental_delivery_not_observed")
            self.assertFalse(self.output.exists())

    def test_stream_missing_terminal_error_after_text_and_cumulative_text_fail(self):
        cases = [([event(native("first")), event(native("second"))], "missing_terminal_status"),
                 ([event(native("first")), event({"error": {"code": 429, "message": KEY}})], "upstream_rate_limited"),
                 ([event(native("first long text")), event(native("first long text plus later")), event(native(finish="STOP"))], "cumulative_stream_not_deltas"),
                 ([event(native("first")), event(native("second")), event(native(finish="STOP")), b"data: truncated"], "truncated_stream_event")]
        for chunks, code in cases:
            self.behavior = lambda request, chunks=chunks: (self.success(request) if request.url.path.endswith(":generateContent") else
                self.response(request, headers={"Content-Type": "text/event-stream"}, stream=Stream(chunks)))
            result = self.run_probe(sources=("ais",))
            self.assertEqual(result["results"][0]["stream"]["code"], code)
            self.assertFalse(result["evidence_written"])
            self.assertNotIn(KEY, json.dumps(result))

    def test_redirects_response_limits_and_exception_text_are_safe(self):
        behaviors = [lambda request: self.response(request, status=302, headers={"Location": "https://other.example"}),
                     lambda request: self.response(request, content=b"x" * (verify.MAX_RESPONSE + 1))]
        for behavior, code in zip(behaviors, ("upstream_redirect_rejected", "response_size_limit")):
            self.requests.clear()
            self.behavior = behavior
            result = self.run_probe(sources=("ais",))
            self.assertEqual(result["results"][0]["non_stream"]["code"], code)
            self.assertEqual(len(self.requests), 1)
        def failing(request):
            raise httpx.ConnectError(KEY)
        self.behavior = failing
        result = self.run_probe(sources=("ais",))
        self.assertEqual(result["results"][0]["non_stream"]["code"], "transport_error")
        self.assertNotIn(KEY, json.dumps(result))

    def test_wall_deadline_closes_transport_that_keeps_stream_open(self):
        closed = threading.Event()
        class Waiting(Stream):
            def __iter__(self):
                yield event(native("partial"))
                closed.wait(1)
                raise OSError(KEY)
        class ClosingTransport(httpx.BaseTransport):
            def handle_request(self, request):
                return httpx.Response(200, headers={"X-Mixer-Upstream-Channel": "playground", "X-Mixer-Channel-Locked": "1",
                                                   "Content-Type": "text/event-stream"}, stream=Waiting([]))
            def close(self):
                closed.set()
        factory = lambda **kwargs: httpx.Client(transport=ClosingTransport(), **kwargs)
        result = verify.probe(BASE, KEY, MODEL, "ais", True, .03, factory)
        self.assertEqual(result["code"], "upstream_timeout")
        self.assertLess(result["elapsed_seconds"], .5)
        self.assertTrue(closed.is_set())

    def test_main_prints_only_fixed_error_for_missing_config(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            code = verify.main(["--model", MODEL])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "missing_or_invalid_service_key")

    def test_invalid_destination_model_timeout_and_sources_make_no_requests(self):
        for base in ("http://localhost:8080", "https://user:password@sidecar.example", "https://sidecar.example/?x=secret", "https://sidecar.example/v1"):
            with self.assertRaises(verify.ProbeError):
                verify.run(MODEL, ["ais"], 1, dict(ENV, AISTUDIO_HTTP_BASE_URL=base), client_factory=self.factory)
        for model, sources, timeout in (("../bad", ["ais"], 1), (MODEL, ["unknown"], 1), (MODEL, ["ais", "ais"], 1), (MODEL, ["ais"], 181)):
            with self.assertRaises(verify.ProbeError):
                verify.run(model, sources, timeout, ENV, client_factory=self.factory)
        self.assertEqual(self.requests, [])
