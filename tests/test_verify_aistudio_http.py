"""Live-probe behavior is tested entirely against a synthetic HTTP transport."""
import contextlib
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

    def factory(self, **kwargs):
        self.assertFalse(kwargs["follow_redirects"])
        self.assertFalse(kwargs["trust_env"])
        self.assertTrue(kwargs["verify"])
        return httpx.Client(transport=httpx.MockTransport(self.handle), **kwargs)

    def handle(self, request):
        self.requests.append(request)
        self.assertEqual(request.headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(json.loads(request.content), verify.BODY)
        self.assertNotIn("Cookie", request.headers)
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
