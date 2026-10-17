"""Provider and HTTP integration tests using safe on-disk runtime exports."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from gemini_web2api import antigravity
from gemini_web2api.antigravity_quota import SOURCE, sanitize_account_usage
from gemini_web2api.config import CONFIG
from gemini_web2api.mixer import MixerHandler
from gemini_web2api.providers import ProviderError
from gemini_web2api.server import ThreadedServer


BASE = "gemini-3.8-pro"
CLAUDE = "claude-sonnet-4.6"
UNAVAILABLE = "gemini-3.7-pro-low"


class _RuntimeExports:
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        now = datetime.now(timezone.utc)
        observed = (now - timedelta(seconds=30)).isoformat()
        tested_at = (now - timedelta(seconds=60)).isoformat()
        self.reset = (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        entries = {}
        for model, effort, status, http_status, reason in (
            (BASE, None, "verified", 200, "generation_verified"),
            (BASE + "-low", "low", "verified", 200, "generation_verified"),
            (BASE + "-medium", "medium", "verified", 200, "generation_verified"),
            (BASE + "-high", "high", "verified", 200, "generation_verified"),
            (CLAUDE, None, "upstream_declared", 429, "rate_limited"),
            (UNAVAILABLE, "low", "unavailable", 404, "model_unsupported"),
        ):
            entries[model] = {
                "status": status, "tested_at": tested_at, "http_status": http_status,
                "effective_upstream_model": model, "quota_model_id": model,
                "effort": effort, "thinking_control": "upstream-model-variant" if effort else "upstream-controlled",
                "reason": reason, "upstream_response_model": "gemini-3.8-flash-n" if model.startswith("gemini") else CLAUDE,
            }
        self.allowed = list(entries)
        quota_raw = {
            "updated_at": observed,
            "antigravity_quota": {
                model: {"utilization": utilization, "reset_time": self.reset}
                for model, utilization in zip(self.allowed, (20, 10, 20, 100, 100, 100))
            },
            "credentials": {"refresh_token": "private-upstream-secret"},
        }
        snapshot = {
            "schema_version": 1, "source": SOURCE, "group_id": 4,
            "observed_at": now.isoformat(), "status": "ok",
            "accounts": [sanitize_account_usage(2, quota_raw, now=now)],
        }
        evidence_path, quota_path = directory / "evidence.json", directory / "quota.json"
        evidence_path.write_text(json.dumps({"schema_version": 1, "source_revision": "1234567abcdef", "models": entries}), encoding="utf-8")
        quota_path.write_text(json.dumps(snapshot), encoding="utf-8")
        environment = mock.patch.dict(os.environ, {
            "ANTIGRAVITY_MODEL_EVIDENCE_FILE": str(evidence_path),
            "ANTIGRAVITY_QUOTA_FILE": str(quota_path), "ANTIGRAVITY_QUOTA_MAX_AGE_SEC": "600",
            "ANTIGRAVITY_MODELS": ",".join(self.allowed),
            "MIXER_WEB_ENABLED": "false", "MIXER_ALLOW_UNAUTHENTICATED": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.requests = []
        self.wallet_status = 200
        self.provider = antigravity.AntigravityProvider(
            base_url="https://gateway.example", api_key="synthetic-provider-key", models=self.allowed,
            group_confirmed=True, transport=httpx.MockTransport(self.upstream),
        )

    def upstream(self, request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model} for model in self.allowed]})
        if request.url.path == "/v1/usage":
            if self.wallet_status != 200:
                return httpx.Response(self.wallet_status, json={"error": {"message": "private-upstream-secret"}})
            return httpx.Response(200, json={"quota": {"remaining": 7, "unit": "USD"}})
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        actual_model = "gemini-3.8-flash-n" if body["model"].startswith("gemini") else CLAUDE
        if body.get("stream"):
            if request.url.path == "/v1/responses":
                chunks = [
                    {"type": "response.created", "response": {"id": "resp_synthetic", "model": actual_model}},
                    {"type": "response.completed", "response": {"id": "resp_synthetic", "model": actual_model, "status": "completed"}},
                ]
                content = "".join("event: " + row["type"] + "\ndata: " + json.dumps(row) + "\n\n" for row in chunks)
            else:
                content = "data: " + json.dumps({"model": actual_model, "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json={"id": "resp_synthetic", "object": "response", "model": actual_model, "status": "completed", "output": []})
        return httpx.Response(200, json={"model": actual_model, "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})


class AntigravityProviderIntegrationTests(_RuntimeExports, unittest.TestCase):
    def test_model_catalog_carries_each_verified_effort_and_quota_observation(self):
        models = {row["id"]: row for row in self.provider.list_models()}
        for effort in ("low", "medium", "high"):
            row = models["ag-" + BASE + "-" + effort]
            self.assertEqual(row["requested_effort"], effort)
            self.assertEqual(row["verification"]["status"], "verified")
            self.assertEqual(row["upstream_model"], BASE + "-" + effort)
            self.assertEqual(row["upstream_response_model"], "gemini-3.8-flash-n")
            self.assertEqual(row["quota_model_id"], BASE + "-" + effort)
            self.assertEqual(row["quota"]["reset_at"], self.reset)
            self.assertGreaterEqual(row["quota"]["age_seconds"], 30)
            self.assertFalse(row["quota"]["stale"])
            self.assertEqual(row["x_mixer"]["quota"], row["quota"])
        self.assertEqual(models["ag-" + BASE + "-low"]["quota"]["remaining_percent"], 90)
        high_quota = models["ag-" + BASE + "-high"]["quota"]
        self.assertIsNone(high_quota["remaining_percent"])
        self.assertEqual(high_quota["status"], "reported_exhausted_or_unknown")
        self.assertNotIn("private-upstream-secret", json.dumps(models))

    def test_unsupported_models_are_hidden_from_normal_catalog_but_remain_diagnostic(self):
        self.assertNotIn("ag-" + UNAVAILABLE, [row["id"] for row in self.provider.list_models()])
        diagnostics = {row["id"]: row for row in self.provider.quota()["models"]}
        self.assertEqual(diagnostics["ag-" + UNAVAILABLE]["availability"]["status"], "unavailable")
        self.assertEqual(diagnostics["ag-" + UNAVAILABLE]["verification"]["http_status"], 404)
        with self.assertRaises(ProviderError) as caught:
            self.provider.proxy({}, "chat", "ag-" + UNAVAILABLE)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.requests, [])

    def test_claude_rate_limit_evidence_is_temporary_and_does_not_disable_retry(self):
        rows = {row["id"]: row for row in self.provider.list_models()}
        self.assertEqual(rows["ag-" + CLAUDE]["availability"]["status"], "temporarily_unavailable")
        self.assertEqual(rows["ag-" + CLAUDE]["verification"]["status"], "upstream_declared")
        response = self.provider.proxy({"messages": []}, "chat", "ag-" + CLAUDE)
        self.assertEqual(response["x_mixer"]["upstream_model"], CLAUDE)
        self.assertEqual(self.requests[0][1]["model"], CLAUDE)
        for effort in ("low", "medium", "high"):
            for body in ({"reasoning_effort": effort}, {"reasoning": {"effort": effort}}):
                with self.assertRaises(ProviderError) as caught:
                    self.provider.proxy(body, "chat", "ag-" + CLAUDE)
                self.assertEqual(caught.exception.code, "unsupported_thinking_level")
        self.assertEqual(len(self.requests), 1)

    def test_bare_model_body_effort_routes_to_exact_variant_for_both_protocols(self):
        for kind, control in (("chat", {"reasoning_effort": "low"}), ("responses", {"reasoning": {"effort": "low"}})):
            request = {"model": "ag-" + BASE, **control}
            result = self.provider.proxy(request, kind, "ag-" + BASE)
            body = self.requests[-1][1]
            self.assertEqual(body["model"], BASE + "-low")
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("reasoning", body)
            self.assertEqual(result["model"], "ag-" + BASE)
            self.assertEqual(result["x_mixer"]["upstream_model"], BASE + "-low")
            self.assertEqual(result["x_mixer"]["requested_effort"], "low")
            self.assertEqual(result["x_mixer"]["quota"]["remaining_percent"], 90)
            self.assertIn(next(iter(control)), request)

    def test_chat_and_responses_sse_keep_resolved_metadata_and_quota(self):
        for kind in ("chat", "responses"):
            rows = list(self.provider.stream({"reasoning_effort": "low"}, kind, "ag-" + BASE))
            payloads = [payload for _, payload in rows if isinstance(payload, dict)]
            self.assertTrue(payloads)
            for payload in payloads:
                self.assertEqual(payload["x_mixer"]["upstream_model"], BASE + "-low")
                self.assertEqual(payload["x_mixer"]["requested_effort"], "low")
                self.assertEqual(payload["x_mixer"]["quota"]["reset_at"], self.reset)
                if "response" in payload:
                    self.assertEqual(payload["response"]["x_mixer"]["upstream_model"], BASE + "-low")

    def test_wallet_failure_preserves_each_model_quota(self):
        self.wallet_status = 503
        result = self.provider.quota()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["model_quota_status"], "ok")
        rows = {row["id"]: row for row in result["models"]}
        self.assertEqual(rows["ag-" + BASE + "-low"]["quota"]["remaining_percent"], 90)
        self.assertEqual(rows["ag-" + BASE + "-low"]["quota"]["reset_at"], self.reset)
        self.assertNotIn("private-upstream-secret", json.dumps(result))


class AntigravityMixerIntegrationTests(_RuntimeExports, unittest.TestCase):
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
        super().setUp()
        config = mock.patch.dict(CONFIG, {"api_keys": ["synthetic-mixer-key"], "log_requests": False})
        config.start()
        self.addCleanup(config.stop)
        provider = mock.patch.object(antigravity.AntigravityProvider, "from_env", return_value=self.provider)
        provider.start()
        self.addCleanup(provider.stop)

    def call(self, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", path, body=json.dumps(body), headers={
                "Authorization": "Bearer synthetic-mixer-key", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_http_json_metadata_does_not_overwrite_resolved_upstream_variant(self):
        # Whitespace is accepted by the Provider's comma-separated allowlist.
        os.environ["ANTIGRAVITY_MODELS"] = " , ".join(os.environ["ANTIGRAVITY_MODELS"].split(","))
        for path, control in (("/v1/chat/completions", {"reasoning_effort": "low"}),
                              ("/v1/responses", {"reasoning": {"effort": "low"}})):
            status, headers, content = self.call(path, {"model": "ag-" + BASE, **control})
            self.assertEqual(status, 200, content)
            self.assertEqual(headers["X-Mixer-Requested-Effort"], "low")
            metadata = json.loads(content)["x_mixer"]
            self.assertEqual(metadata["upstream_model"], BASE + "-low")
            self.assertEqual(metadata["requested_effort"], "low")
            self.assertEqual(metadata["quota_model_id"], BASE + "-low")
            self.assertEqual(self.requests[-1][1]["model"], BASE + "-low")
            self.assertNotIn("reasoning_effort", self.requests[-1][1])

    def test_http_sse_metadata_keeps_variant_selected_by_body_effort(self):
        status, headers, content = self.call("/v1/chat/completions", {
            "model": "ag-" + BASE, "reasoning_effort": "low", "stream": True,
        })
        self.assertEqual(status, 200, content)
        self.assertEqual(headers["X-Mixer-Requested-Effort"], "low")
        payloads = [json.loads(line[6:]) for line in content.splitlines() if line.startswith("data: {")]
        self.assertTrue(payloads)
        self.assertEqual(payloads[0]["x_mixer"]["upstream_model"], BASE + "-low")
        self.assertEqual(payloads[0]["x_mixer"]["quota"]["remaining_percent"], 90)


if __name__ == "__main__":
    unittest.main()
