"""Credential-free regressions for the Docker package's image-input protocol.

Network mocks sit at urllib/curl boundaries: upload, token extraction, payload
construction, response parsing, and HTTP handlers execute their real code.
"""
import base64
import contextlib
import http.client
import io
import json
import threading
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from gemini_web2api import gemini, multimodal
from gemini_web2api.config import CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer


IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN2sAAAAASUVORK5CYII="
)
FILE_REF = "/contrib_service/ttl_1d/fake-image-reference"
UPLOAD_URL = "https://content-push.googleapis.com/upload?token=fake-upload-secret"
PAGE_HTML = b'<script>window.WIZ_global_data={"FdrFJe": "fake-session-id", "SNlM0e": "fake-xsrf-token", "qKIAYe":"fake-push", "Ylro7b":"fake-context"};</script>'


def decode_payload(body):
    if isinstance(body, bytes):
        body = body.decode()
    params = parse_qs(body)
    return params, json.loads(json.loads(params["f.req"][0])[1])


def response_frame(text):
    inner = [None, None, None, None, [["candidate", [text]]], "padding" * 40]
    return json.dumps([["wrb.fr", None, json.dumps(inner)]])


def rejection_frame(code=1003):
    details = {"error": ["type.googleapis.com/google.internal.bard.v1.BardErrorInfo", [code], "fake-response-secret"]}
    return ")]}'\n216\n" + json.dumps([["wrb.fr", None, json.dumps(details)]]) + "\n"


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        if isinstance(body, str):
            body = body.encode()
        super().__init__(body)
        self.text = body.decode("utf-8", errors="replace")
        self.headers = headers or {}
        self.status_code = 200

    def raise_for_status(self):
        return None


class ConfiguredTestCase(unittest.TestCase):
    def setUp(self):
        self.config_patch = mock.patch.dict(CONFIG, {
            "cookie_file": None,
            "proxy": None,
            "auth_user": None,
            "xsrf_token": None,
            "api_keys": [],
            "log_requests": False,
            "retry_attempts": 1,
            "retry_delay_sec": 0,
            "request_timeout_sec": 5,
            "temporary_chats": False,
        })
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        # Prevent even accidental reads of a developer's cookie file.
        for target in ("gemini_web2api.gemini.load_cookie", "gemini_web2api.multimodal.load_cookie"):
            patch = mock.patch(target, return_value=("", None))
            patch.start()
            self.addCleanup(patch.stop)
        token_cache = {"tokens": {}, "ts": 0}
        cache_patch = mock.patch.object(multimodal, "_page_tokens_cache", token_cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)


class ImagePayloadTests(ConfiguredTestCase):
    def test_plain_text_keeps_existing_102_slot_payload(self):
        _, payload = decode_payload(gemini._build_payload("hello", 1, 4))
        self.assertEqual(len(payload), 102)
        self.assertEqual(payload[0], ["hello", 0, None, None, None, None, 0])
        self.assertEqual(payload[41], [2])
        self.assertIsNone(payload[80])
        self.assertEqual(payload[79], 1)

    def test_image_reference_uses_nested_attachment_and_slot_80(self):
        _, payload = decode_payload(gemini._build_payload("describe", 1, 4, [FILE_REF]))
        self.assertEqual(len(payload), 81)
        self.assertEqual(payload[0][3], [[[FILE_REF], "image.png"]])
        self.assertEqual(payload[80], 1)

    def test_attachment_filename_survives_payload_serialization(self):
        refs = [(FILE_REF, "screenshot.png"), (FILE_REF + "-2", "photo.jpg")]
        _, payload = decode_payload(gemini._build_payload("compare", 1, 4, refs))
        self.assertEqual(payload[0][3], [
            [[FILE_REF], "screenshot.png"], [[FILE_REF + "-2"], "photo.jpg"],
        ])

    def test_enhanced_model_extras_cannot_override_image_slot_80(self):
        _, payload = decode_payload(gemini._build_payload("describe", 3, 4, [FILE_REF], {31: 2, 80: 3}))
        self.assertEqual(payload[31], 2)
        self.assertEqual(payload[80], 1)

    def test_bard_error_never_echoes_raw_response_tokens(self):
        raw = 'BardErrorInfo [1003] {"at":"fake-response-secret","cookie":"fake-cookie-secret"}'
        with self.assertRaises(RuntimeError) as caught:
            gemini.extract_response_text(raw)
        self.assertEqual(str(caught.exception), "Gemini upstream rejected request: BardErrorInfo [1003]")

    def test_structured_bard_error_inside_serialized_frame_is_not_empty_success(self):
        with self.assertRaises(RuntimeError) as caught:
            gemini.extract_response_text(rejection_frame())
        self.assertEqual(str(caught.exception), "Gemini upstream rejected request: BardErrorInfo [1003]")

    def test_page_token_cache_does_not_cross_google_accounts(self):
        with mock.patch.object(multimodal, "_get_page_tokens", side_effect=[{"f_sid": "account-0"}, {"f_sid": "account-2"}]) as fetch:
            first = multimodal._cached_page_tokens()
            CONFIG["auth_user"] = 2
            second = multimodal._cached_page_tokens()
        self.assertEqual(first["f_sid"], "account-0")
        self.assertEqual(second["f_sid"], "account-2")
        self.assertEqual(fetch.call_count, 2)

    def test_file_request_forces_fresh_page_state_even_without_clock_advance(self):
        with mock.patch.object(multimodal.time, "time", return_value=123456), mock.patch.object(multimodal, "_get_page_tokens", side_effect=[{"f_sid": "stale"}, {"f_sid": "fresh"}]):
            first = multimodal._cached_page_tokens()
            second = multimodal._cached_page_tokens(max_age=0)
        self.assertEqual(first["f_sid"], "stale")
        self.assertEqual(second["f_sid"], "fresh")


class ImageInputEndpointTests(ConfiguredTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", path, body=json.dumps(payload), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    @contextlib.contextmanager
    def upstream(self, generation_body=None, upload_body=None, upload_headers=None, text_generation_body=None):
        self.requests = []
        self.responses = []
        self.curl_calls = []

        def urlopen(request, **kwargs):
            self.requests.append(request)
            if request.full_url.endswith("/app"):
                response = FakeResponse(PAGE_HTML)
            elif request.full_url == "https://content-push.googleapis.com/upload/":
                response = FakeResponse(headers=(upload_headers if upload_headers is not None else {"X-Goog-Upload-URL": UPLOAD_URL}))
            elif request.full_url == UPLOAD_URL:
                response = FakeResponse(FILE_REF if upload_body is None else upload_body)
            elif "StreamGenerate" in request.full_url:
                response = FakeResponse(response_frame("Text response") if text_generation_body is None else text_generation_body)
            else:
                raise AssertionError("Unexpected external request")
            self.responses.append(response)
            return response

        def post(url, **kwargs):
            self.curl_calls.append((url, kwargs))
            response = FakeResponse(response_frame("Image response") if generation_body is None else generation_body)
            self.responses.append(response)
            return response

        # create=True permits these regression tests to run against the original
        # revision, where image requests incorrectly use the text transport.
        curl = types.SimpleNamespace(post=mock.Mock(side_effect=post))
        with mock.patch("urllib.request.urlopen", side_effect=urlopen), mock.patch.object(gemini, "curl_requests", curl, create=True), mock.patch.object(gemini, "HAS_CURL_CFFI", True, create=True), mock.patch.object(gemini, "_get_httpx_client", side_effect=AssertionError("Image input must not use the text streaming transport")):
            yield

    def image_request(self, stream=False):
        return {
            "model": "gemini-3.6-flash",
            "stream": stream,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this screenshot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(IMAGE_BYTES).decode()}},
            ]}],
        }

    def assert_file_generation_protocol(self):
        self.assertEqual(len(self.curl_calls), 1)
        url, args = self.curl_calls[0]
        params, payload = decode_payload(args["data"])
        self.assertEqual(payload[0][3], [[[FILE_REF], "image.png"]])
        self.assertEqual(len(payload), 81)
        self.assertEqual(payload[80], 1)
        self.assertEqual(params["at"], ["fake-xsrf-token"])
        self.assertEqual(parse_qs(urlparse(url).query)["f.sid"], ["fake-session-id"])
        self.assertEqual(args["impersonate"], "chrome")
        headers = {key.lower(): value for key, value in args["headers"].items()}
        self.assertEqual(json.loads(headers["x-goog-ext-525005358-jspb"]), [payload[59], 1])
        uploads = [request for request in self.requests if "content-push.googleapis.com" in request.full_url]
        self.assertEqual(len(uploads), 2)
        start_headers = {key.lower(): value for key, value in uploads[0].header_items()}
        self.assertEqual(start_headers["x-goog-upload-protocol"], "resumable")
        self.assertEqual(start_headers["x-goog-upload-header-content-length"], str(len(IMAGE_BYTES)))
        self.assertEqual(start_headers["x-goog-upload-header-content-type"], "image/png")
        self.assertEqual(uploads[1].data, IMAGE_BYTES)
        self.assertTrue(all(response.closed for response in self.responses), "Image requests must release upload, page, and generation responses")

    def test_text_request_runs_real_generate_with_existing_transport(self):
        with self.upstream():
            status, _, body = self.post_json("/v1/chat/completions", {
                "messages": [{"role": "user", "content": "Hello"}],
            })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "Text response")
        self.assertEqual(self.curl_calls, [])
        self.assertEqual(len(self.requests), 1)
        _, payload = decode_payload(self.requests[0].data)
        self.assertEqual(len(payload), 102)
        self.assertIsNone(payload[0][3])

    def test_image_upload_and_generation_run_real_production_methods(self):
        with self.upstream():
            status, _, body = self.post_json("/v1/chat/completions", self.image_request())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "Image response")
        self.assert_file_generation_protocol()

    def test_nondefault_account_binds_page_and_file_generation(self):
        CONFIG["auth_user"] = 2
        with self.upstream():
            status, _, _ = self.post_json("/v1/chat/completions", self.image_request())
        self.assertEqual(status, 200)
        self.assert_file_generation_protocol()
        self.assertTrue(all("/u/2/app" in request.full_url for request in self.requests if request.full_url.endswith("/app")))
        self.assertIn("/u/2/_/BardChatUi/", self.curl_calls[0][0])
        self.assertEqual(self.curl_calls[0][1]["headers"]["X-Goog-AuthUser"], "2")

    def test_image_stream_uses_file_transport_and_emits_complete_result(self):
        with self.upstream():
            status, headers, body = self.post_json("/v1/chat/completions", self.image_request(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn("Image response", body)
        self.assertIn("data: [DONE]", body)
        self.assert_file_generation_protocol()

    def test_image_stream_rejection_returns_502_before_sse_headers(self):
        raw = 'BardErrorInfo [1003] {"at":"fake-response-secret"}'
        with self.upstream(generation_body=raw):
            status, headers, body = self.post_json("/v1/chat/completions", self.image_request(stream=True))
        self.assertEqual(status, 502)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("BardErrorInfo [1003]", json.loads(body)["error"]["message"])
        self.assertNotIn("fake-response-secret", body)

    def test_structured_image_rejection_returns_502_without_retry_or_token_leak(self):
        CONFIG["retry_attempts"] = 3
        with self.upstream(generation_body=rejection_frame()), mock.patch.object(gemini.time, "sleep") as sleep:
            status, _, body = self.post_json("/v1/chat/completions", self.image_request())
        self.assertEqual(status, 502)
        self.assertIn("BardErrorInfo [1003]", json.loads(body)["error"]["message"])
        self.assertNotIn("fake-response-secret", body)
        self.assertEqual(len(self.curl_calls), 1)
        sleep.assert_not_called()

    def test_structured_text_rejection_returns_502_without_retry(self):
        CONFIG["retry_attempts"] = 3
        with self.upstream(text_generation_body=rejection_frame()), mock.patch.object(gemini.time, "sleep") as sleep:
            status, _, body = self.post_json("/v1/chat/completions", {"messages": [{"role": "user", "content": "Hello"}]})
        self.assertEqual(status, 502)
        self.assertIn("BardErrorInfo [1003]", json.loads(body)["error"]["message"])
        self.assertEqual(len(self.requests), 1)
        sleep.assert_not_called()

    def test_invalid_upload_reference_is_rejected_without_exposing_body(self):
        with self.upstream(upload_body='{"token":"fake-upload-secret"}'):
            status, _, body = self.post_json("/v1/chat/completions", self.image_request())
        self.assertEqual(status, 502)
        self.assertEqual(self.curl_calls, [])
        self.assertNotIn("fake-upload-secret", body)

    def test_upload_session_error_is_rejected_without_exposing_headers(self):
        with self.upstream(upload_headers={"X-Sensitive": "fake-header-secret"}):
            status, _, body = self.post_json("/v1/chat/completions", self.image_request())
        self.assertEqual(status, 502)
        self.assertEqual(self.curl_calls, [])
        self.assertNotIn("fake-header-secret", body)

    def test_successful_upload_does_not_log_session_url_or_file_reference(self):
        CONFIG["log_requests"] = True
        stderr = io.StringIO()
        with self.upstream(), contextlib.redirect_stderr(stderr):
            status, _, _ = self.post_json("/v1/chat/completions", self.image_request())
        self.assertEqual(status, 200)
        self.assertNotIn("fake-upload-secret", stderr.getvalue())
        self.assertNotIn(FILE_REF, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
