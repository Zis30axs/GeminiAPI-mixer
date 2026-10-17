"""Exercise worker image input with synthetic accounts and mocked network I/O.

The pool launches this monolithic entry point. Upload, payload construction,
generation parsing, and HTTP response rendering all execute their real code.
"""
import base64
import contextlib
import io
import json
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from gemini_web2api import gemini, multimodal
from tests.test_model_presets import ROUTING_HEADER, SYNTHETIC_CONFIG
from tests.test_multimodal_upload import (
    FILE_REF, IMAGE_BYTES, PAGE_HTML, UPLOAD_URL, FakeResponse,
    decode_payload, rejection_frame,
)
from tests.test_thought_passthrough import ANSWER, THOUGHT, data_events, frame
from tests.test_upstream_protocol import single


class WorkerImageInputTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(single.CONFIG, {
            **SYNTHETIC_CONFIG, "auth_user": 2,
        }, clear=True))
        # Worker binding is intentional. Restore module identities afterward so
        # other tests retain the original package configuration objects.
        self.stack.enter_context(mock.patch.object(gemini, "CONFIG", gemini.CONFIG))
        self.stack.enter_context(mock.patch.object(multimodal, "CONFIG", multimodal.CONFIG))
        for module in (single, gemini, multimodal):
            self.stack.enter_context(mock.patch.object(module, "load_cookie", return_value=("", None)))
        self.stack.enter_context(mock.patch.object(multimodal, "_page_tokens_cache", {"tokens": {}, "ts": 0}))

    @contextlib.contextmanager
    def upstream(self, generation_body=None):
        self.requests = []
        self.responses = []
        self.curl_calls = []

        def urlopen(request, **kwargs):
            self.requests.append(request)
            if request.full_url.endswith("/app"):
                response = FakeResponse(PAGE_HTML)
            elif request.full_url == "https://content-push.googleapis.com/upload/":
                response = FakeResponse(headers={"X-Goog-Upload-URL": UPLOAD_URL})
            elif request.full_url == UPLOAD_URL:
                response = FakeResponse(FILE_REF)
            elif "StreamGenerate" in request.full_url:
                response = FakeResponse(frame(ANSWER, THOUGHT))
            else:
                raise AssertionError("Unexpected external request")
            self.responses.append(response)
            return response

        def post(url, **kwargs):
            self.curl_calls.append((url, kwargs))
            response = FakeResponse(frame(ANSWER, THOUGHT) if generation_body is None else generation_body)
            self.responses.append(response)
            return response

        curl = types.SimpleNamespace(post=mock.Mock(side_effect=post))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=urlopen))
            stack.enter_context(mock.patch.object(gemini, "curl_requests", curl, create=True))
            stack.enter_context(mock.patch.object(gemini, "HAS_CURL_CFFI", True, create=True))
            stack.enter_context(mock.patch.object(gemini, "_get_httpx_client", side_effect=AssertionError("Image input used text stream client")))
            stack.enter_context(mock.patch.object(single.httpx, "Client", side_effect=AssertionError("Image input used worker text stream client")))
            yield

    def handler(self):
        handler = single.GeminiHandler.__new__(single.GeminiHandler)
        handler.path = "/v1/chat/completions"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        return handler

    def request(self, stream=False, model="gemini-4-argon-high"):
        handler = self.handler()
        handler.handle_chat(json.dumps({
            "model": model, "stream": stream,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this screenshot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(IMAGE_BYTES).decode()}},
            ]}],
        }).encode())
        return handler, handler.wfile.getvalue().decode()

    def assert_image_protocol(self, model_id=8, think=0, ticket="95a4c0abcba24364", variant=2):
        self.assertEqual(len(self.curl_calls), 1)
        url, args = self.curl_calls[0]
        params, payload = decode_payload(args["data"])
        self.assertEqual(payload[0][3], [[[FILE_REF], "image.png"]])
        self.assertEqual(len(payload), 81)
        self.assertEqual(payload[17], [[think]])
        self.assertEqual(payload[79], model_id)
        self.assertEqual(payload[80], 1)
        self.assertEqual(params["at"], ["fake-xsrf-token"])
        self.assertEqual(parse_qs(urlparse(url).query)["f.sid"], ["fake-session-id"])
        self.assertIn("/u/2/_/BardChatUi/", url)
        headers = {key.lower(): value for key, value in args["headers"].items()}
        self.assertEqual(headers["x-goog-authuser"], "2")
        self.assertEqual(json.loads(headers["x-goog-ext-525005358-jspb"]), [payload[59], 1])
        selector = json.loads(headers[ROUTING_HEADER.lower()])
        self.assertEqual(selector[4], ticket)
        self.assertEqual(selector[14:16], [model_id, variant])
        self.assertEqual(args["impersonate"], "chrome")
        pages = [request for request in self.requests if request.full_url.endswith("/app")]
        self.assertTrue(pages)
        self.assertTrue(all("/u/2/app" in request.full_url for request in pages))
        uploads = [request for request in self.requests if "content-push.googleapis.com" in request.full_url]
        self.assertEqual(len(uploads), 2)
        self.assertEqual(uploads[1].data, IMAGE_BYTES)
        self.assertIs(gemini.CONFIG, single.CONFIG)
        self.assertIs(multimodal.CONFIG, single.CONFIG)
        self.assertTrue(all(response.closed for response in self.responses))

    def test_real_upload_generate_and_parse_bind_worker_account_and_keep_thoughts(self):
        with self.upstream():
            refs = single.upload_images([(IMAGE_BYTES, "image/png")])
            raw = single.gemini_stream_generate("describe", 8, 0, refs, model_name="gemini-4-argon-high")
            text = single.extract_response_text(raw)
        self.assertEqual(text, ANSWER)
        self.assertEqual(text.reasoning_content, THOUGHT)
        self.assertNotIn(THOUGHT, text)
        self.assert_image_protocol()

    def test_worker_http_image_response_keeps_answer_and_thoughts_separate(self):
        with self.upstream():
            handler, body = self.request()
        self.assertEqual(handler.send_response.call_args_list, [mock.call(200)])
        result = json.loads(body)
        self.assertEqual(result["model"], "gemini-4-argon-high")
        message = result["choices"][0]["message"]
        self.assertEqual(message["content"], ANSWER)
        self.assertEqual(message["reasoning_content"], THOUGHT)
        self.assert_image_protocol()

    def test_worker_image_stream_keeps_thoughts_and_emits_done_once(self):
        with self.upstream():
            handler, body = self.request(stream=True)
        self.assertEqual(handler.send_response.call_args_list, [mock.call(200)])
        deltas = [event["choices"][0]["delta"] for event in data_events(body)]
        self.assertEqual("".join(delta.get("content", "") for delta in deltas), ANSWER)
        self.assertEqual("".join(delta.get("reasoning_content", "") for delta in deltas), THOUGHT)
        self.assertEqual(body.count("data: [DONE]"), 1)
        self.assert_image_protocol()

    def test_worker_image_rejection_returns_json_502_before_sse_and_no_retry(self):
        single.CONFIG["retry_attempts"] = 3
        with self.upstream(generation_body=rejection_frame()), mock.patch.object(gemini.time, "sleep") as sleep:
            handler, body = self.request(stream=True)
        self.assertEqual(handler.send_response.call_args_list, [mock.call(502)])
        self.assertIn("BardErrorInfo [1003]", json.loads(body)["error"]["message"])
        self.assertNotIn("fake-response-secret", body)
        self.assertNotIn("data:", body)
        self.assertEqual(len(self.curl_calls), 1)
        sleep.assert_not_called()

    def test_image_transport_keeps_explicit_flash_routing_ticket(self):
        with self.upstream():
            handler, body = self.request(model="gemini-3.8-flash-thinking-lite")
        self.assertEqual(handler.send_response.call_args_list, [mock.call(200)])
        self.assertEqual(json.loads(body)["model"], "gemini-3.8-flash-thinking-lite")
        self.assert_image_protocol(5, 0, "56fdd199312815e2", 1)

    def test_worker_plain_text_keeps_existing_transport_and_fixed_model_route(self):
        with self.upstream():
            text = single.extract_response_text(single.gemini_stream_generate(
                "hello", 8, 0, model_name="gemini-4-argon-high"))
        self.assertEqual(text, ANSWER)
        self.assertEqual(text.reasoning_content, THOUGHT)
        self.assertEqual(self.curl_calls, [])
        self.assertEqual(len(self.requests), 1)
        _, payload = decode_payload(self.requests[0].data)
        self.assertEqual(len(payload), 102)
        self.assertIsNone(payload[0][3])
        headers = {key.lower(): value for key, value in self.requests[0].header_items()}
        self.assertEqual(json.loads(headers[ROUTING_HEADER.lower()])[14:16], [8, 2])


if __name__ == "__main__":
    unittest.main()
