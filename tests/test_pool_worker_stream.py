"""Exercise the real monolithic HTTP handler before and after SSE commits."""

import contextlib
import http.client
from http.server import HTTPServer
import json
import threading
import unittest
from unittest import mock

from tests.test_upstream_protocol import single


class WorkerStreamTests(unittest.TestCase):
    @contextlib.contextmanager
    def worker(self, iterator):
        # This is the production handler, with only its upstream iterator mocked.
        server = HTTPServer(("127.0.0.1", 0), single.GeminiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with mock.patch.dict(single.CONFIG, {"api_keys": ["synthetic-worker-key"]}):
            with mock.patch.object(single, "gemini_stream_generate_iter", return_value=iterator) as upstream:
                with mock.patch.object(single, "log") as logged:
                    thread.start()
                    try:
                        yield server, upstream, logged
                    finally:
                        server.shutdown()
                        thread.join(timeout=3)
                        server.server_close()

    def request(self, server):
        model = next(iter(single.MODELS))
        body = json.dumps({
            "model": model, "stream": True,
            "messages": [{"role": "user", "content": "Synthetic stream test"}],
        })
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request("POST", "/v1/chat/completions", body=body, headers={
                "Authorization": "Bearer synthetic-worker-key", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read().decode("utf-8")
        finally:
            connection.close()

    @staticmethod
    def events(body):
        return [json.loads(line[6:]) for line in body.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"]

    def test_initial_iterator_failure_returns_502_json_before_sse(self):
        def rejected():
            raise RuntimeError("synthetic initial rejection")
            yield  # Keep failure lazy, matching the real upstream generator.

        with self.worker(rejected()) as (server, upstream, _logged):
            status, content_type, body = self.request(server)
            self.assertEqual(status, 502)
            self.assertEqual(content_type, "application/json")
            self.assertIn("synthetic initial rejection", json.loads(body)["error"]["message"])
            self.assertNotIn("data:", body)
            upstream.assert_called_once()

    def test_empty_iterator_returns_502_json(self):
        for chunks in ([], ["", ""]):
            with self.subTest(chunks=chunks):
                with self.worker(iter(chunks)) as (server, upstream, _logged):
                    status, content_type, body = self.request(server)
                    self.assertEqual(status, 502)
                    self.assertEqual(content_type, "application/json")
                    self.assertIn("empty upstream stream", json.loads(body)["error"]["message"])
                    self.assertNotIn("data:", body)
                    upstream.assert_called_once()

    def test_valid_stream_preserves_first_text_exactly_once(self):
        with self.worker(iter(["", "first ", "second"])) as (server, upstream, _logged):
            status, content_type, body = self.request(server)
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "text/event-stream")
            events = self.events(body)
            contents = [event["choices"][0]["delta"].get("content") for event in events]
            self.assertEqual([text for text in contents if text is not None], ["first ", "second"])
            self.assertEqual(events[0]["choices"][0]["delta"], {"role": "assistant"})
            self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")
            self.assertEqual(body.count("data: [DONE]"), 1)
            upstream.assert_called_once()

    def test_later_failure_leaves_one_partial_stream_without_replay(self):
        def interrupted():
            yield "partial answer"
            raise RuntimeError("synthetic later failure")

        with self.worker(interrupted()) as (server, upstream, logged):
            status, content_type, body = self.request(server)
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "text/event-stream")
            events = self.events(body)
            contents = [event["choices"][0]["delta"].get("content") for event in events]
            self.assertEqual([text for text in contents if text is not None], ["partial answer"])
            self.assertNotIn("[DONE]", body)
            self.assertNotIn("upstream error:", body)
            self.assertNotIn("HTTP/1.", body)
            self.assertEqual(len(events), 2)
            upstream.assert_called_once()
            self.assertTrue(any("synthetic later failure" in str(call) for call in logged.call_args_list))


if __name__ == "__main__":
    unittest.main()
