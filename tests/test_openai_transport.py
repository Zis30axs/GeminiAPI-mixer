"""Shared SSE termination contracts exercised through both provider profiles."""
import json
import os
import time
import unittest
from unittest import mock

import httpx

from gemini_web2api.antigravity import AntigravityProvider
from gemini_web2api.openai_provider import OpenAIProvider


class HeldConnection(httpx.SyncByteStream):
    def __init__(self, chunks, delay_after_first=0):
        self.chunks = chunks
        self.delay_after_first = delay_after_first
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            self.read_count += 1
            if index and self.delay_after_first:
                time.sleep(self.delay_after_first)
            yield chunk

    def close(self):
        self.closed = True


class OpenAITransportTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def provider(self, provider_type, upstream):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)
        provider = provider_type(base_url="https://gateway.example", api_key="synthetic-key",
                                 models=["gpt-test"], group_confirmed=True,
                                 total_timeout=0.05, transport=httpx.MockTransport(handler))
        alias = "ag-gpt-test" if provider_type is AntigravityProvider else "gpt-test"
        return provider, alias

    def test_responses_terminal_event_closes_keepalive_without_waiting_for_eof(self):
        for provider_type in (OpenAIProvider, AntigravityProvider):
            for terminal in ("completed", "incomplete", "cancelled"):
                for use_event_name in (False, True):
                    with self.subTest(provider=provider_type.__name__, terminal=terminal, event=use_event_name):
                        event = "response." + terminal
                        payload = {"response": {"status": terminal, "output": []}}
                        if not use_event_name:
                            payload["type"] = event
                        encoded = (("event: " + event + "\n") if use_event_name else "")
                        encoded += "data: " + json.dumps(payload) + "\n\n"
                        # Reading the keepalive after a valid terminal event used
                        # to cross the wall deadline and turn success into 504.
                        upstream = HeldConnection([encoded.encode(), b": keepalive\n\n"], delay_after_first=0.075)
                        provider, alias = self.provider(provider_type, upstream)
                        events = list(provider.stream({}, "responses", alias))
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0][1]["response"]["status"], terminal)
                        self.assertEqual(upstream.read_count, 1)
                        self.assertTrue(upstream.closed)

    def test_chat_finish_reason_retains_later_usage_until_done_or_eof(self):
        for provider_type in (OpenAIProvider, AntigravityProvider):
            for done in (False, True):
                with self.subTest(provider=provider_type.__name__, done=done):
                    chunks = [
                        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
                        b'data: {"choices":[],"usage":{"completion_tokens":7}}\n\n',
                    ]
                    if done:
                        chunks.append(b"data: [DONE]\n\n")
                    upstream = HeldConnection(chunks)
                    provider, alias = self.provider(provider_type, upstream)
                    events = list(provider.stream({}, "chat", alias))
                    self.assertEqual(events[1][1]["usage"]["completion_tokens"], 7)
                    self.assertEqual(len(events), 3 if done else 2)
                    self.assertEqual(upstream.read_count, len(chunks))
                    self.assertTrue(upstream.closed)


if __name__ == "__main__":
    unittest.main()
