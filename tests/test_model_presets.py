"""Fixed model presets must survive every upstream transport unchanged."""
import io
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs
import uuid

from gemini_web2api import gemini, models, server
from tests.test_upstream_protocol import NORMAL_RESPONSE, NORMAL_TEXT, single


ROUTING_HEADER = "X-Goog-Ext-525001261-Jspb"
LEVELS = {"low": (4, 1), "medium": (2, 4), "high": (0, 2)}
FAMILIES = {
    "gemini-4-argon": (8, "95a4c0abcba24364"),
    "gemini-3.1-pro": (3, "e6fa609c3fa255c0"),
}
SYNTHETIC_CONFIG = {
    "cookie_file": None,
    "proxy": None,
    "retry_attempts": 1,
    "retry_delay_sec": 0,
    "request_timeout_sec": 5,
    "xsrf_token": None,
    "auth_user": None,
    "gemini_bl": "synthetic-build",
    "temporary_chats": False,
    "log_requests": False,
    "default_model": "gemini-3.6-flash",
    "api_keys": [],
}


def resolve_both(name):
    yield single.GeminiHandler._resolve_model(None, name)
    yield models.resolve_model(name)


class ModelPresetTests(unittest.TestCase):
    def test_model_catalogue_is_shared_between_entry_points(self):
        self.assertIs(single.MODELS, models.MODELS)

    def test_named_levels_and_default_models_resolve_identically(self):
        expected = [("gemini-4-argon", 8, 2), ("gemini-3.1-pro", 3, 4)]
        expected.extend(
            (f"{family}-{level}", mode, think)
            for family, (mode, _) in FAMILIES.items()
            for level, (think, _) in LEVELS.items()
        )
        for name, mode, think in expected:
            for resolved in resolve_both(name):
                with self.subTest(name=name, result_length=len(resolved)):
                    self.assertEqual(resolved[:4], (name, mode, think, None))
                    self.assertEqual(models.MODELS[name]["mode"], mode)
                    self.assertEqual(models.MODELS[name]["think"], think)

    def test_client_think_suffix_cannot_override_model_level(self):
        for family, (mode, _) in FAMILIES.items():
            for level, (think, _) in LEVELS.items():
                name = f"{family}-{level}"
                for suffix in ("@think=0", "@think=4", "@think=invalid", "@think=", "@think=0@think=4"):
                    for resolved in resolve_both(name + suffix):
                        with self.subTest(name=name, suffix=suffix, result_length=len(resolved)):
                            self.assertEqual(resolved[:4], (name, mode, think, None))

    def test_unknown_or_nonstring_model_is_a_validation_error(self):
        for value in (None, 42, True, [], {}, "", "unknown-model", "unknown-model@think=invalid"):
            for resolved in resolve_both(value):
                with self.subTest(value=value, result_length=len(resolved)):
                    self.assertTrue(resolved[3])
                    self.assertIsNone(resolved[0])
                    self.assertIsNone(resolved[1])
                    self.assertIsNone(resolved[2])

    def assert_routing_header(self, headers, mode, ticket, variant):
        lower_headers = {name.lower(): value for name, value in headers.items()}
        routing = json.loads(lower_headers[ROUTING_HEADER.lower()])
        self.assertEqual(len(routing), 17)
        request_id = routing[16]
        self.assertEqual(str(uuid.UUID(request_id)), request_id)
        self.assertEqual(routing, [
            1, None, None, None, ticket, None, None, 0,
            [4, 5, 6, 8, 4, 5, 6, 8], None, None, 2,
            None, None, mode, variant, request_id,
        ])

    def assert_request_preset(self, body, headers, mode, think, ticket, variant):
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        outer = json.loads(parse_qs(body)["f.req"][0])
        payload = json.loads(outer[1])
        self.assertEqual(payload[17], [[think]])
        self.assertEqual(payload[79], mode)
        if len(payload) > 80:
            self.assertIsNone(payload[80])
        self.assert_routing_header(headers, mode, ticket, variant)

    def test_routing_helper_encodes_all_fixed_levels(self):
        for family, (mode, ticket) in FAMILIES.items():
            for level, (think, variant) in LEVELS.items():
                with self.subTest(family=family, level=level):
                    headers = models.build_model_routing_headers(mode, think)
                    self.assert_routing_header(headers, mode, ticket, variant)

    def test_flash_routes_preserve_dynamic_thinking_variant_exception(self):
        presets = (
            ("gemini-3.8-flash", 1, 4, 1, "56fdd199312815e2"),
            ("gemini-3.8-flash-thinking", 2, 0, 2, "56fdd199312815e2"),
            ("gemini-3.8-flash-thinking-lite", 5, 0, 1, "56fdd199312815e2"),
            ("gemini-flash-lite", 6, 4, 1, "8c46e95b1a07cecc"),
        )
        for name, mode, think, variant, ticket in presets:
            self.assert_routing_header(models.build_model_routing_headers(mode, think, name), mode, ticket, variant)
            for resolved in resolve_both(name + "@think=2"):
                with self.subTest(name=name, result_length=len(resolved)):
                    self.assertEqual(resolved[:4], (name, mode, think, None))
            for module in (single, gemini):
                with self.subTest(name=name, module=module.__name__):
                    response = mock.Mock()
                    response.read.return_value = NORMAL_RESPONSE.encode("utf-8")
                    with mock.patch.dict(module.CONFIG, SYNTHETIC_CONFIG, clear=True):
                        with mock.patch.object(module.urllib.request, "urlopen", return_value=response) as open_url:
                            if module is single:
                                module.gemini_stream_generate("test", mode, think, model_name=name)
                            else:
                                module.generate("test", mode, think, model_name=name)
                    request = open_url.call_args.args[0]
                    self.assert_request_preset(request.data, dict(request.header_items()), mode, think, ticket, variant)

    def test_legacy_flash_names_and_default_transport_omit_routing_ticket(self):
        presets = (
            (None, 1, 4), (None, 2, 0), (None, 5, 0),
            ("gemini-3.7-flash", 1, 4), ("gemini-3.6-flash", 1, 4),
            ("gemini-3.5-flash", 1, 4), ("gemini-3.5-flash-thinking", 2, 0),
            ("gemini-3.5-flash-thinking-lite", 5, 0),
        )
        for name, mode, think in presets:
            with self.subTest(name=name, mode=mode):
                self.assertEqual(models.build_model_routing_headers(mode, think, name), {})
                for module in (single, gemini):
                    response = mock.Mock()
                    response.read.return_value = NORMAL_RESPONSE.encode("utf-8")
                    with mock.patch.dict(module.CONFIG, SYNTHETIC_CONFIG, clear=True):
                        with mock.patch.object(module.urllib.request, "urlopen", return_value=response) as open_url:
                            if module is single:
                                module.gemini_stream_generate("test", mode, think, model_name=name)
                            else:
                                module.generate("test", mode, think, model_name=name)
                    request = open_url.call_args.args[0]
                    self.assertNotIn(ROUTING_HEADER.lower(), {key.lower() for key, _ in request.header_items()})

    def test_http_handlers_preserve_new_and_legacy_flash_transport_choice(self):
        # Invoke production handlers directly, with in-memory I/O and mocked
        # upstream transports: no HTTP socket or account configuration is used.
        for module, handler_type in ((single, single.GeminiHandler), (gemini, server.GeminiHandler)):
            for name in ("gemini-3.8-flash", "gemini-3.6-flash"):
                for stream in (False, True):
                    with self.subTest(module=module.__name__, name=name, stream=stream):
                        handler = handler_type.__new__(handler_type)
                        handler.send_json = mock.Mock()
                        handler.send_response = mock.Mock()
                        handler.send_header = mock.Mock()
                        handler.end_headers = mock.Mock()
                        handler.wfile = io.BytesIO()
                        body = json.dumps({
                            "model": name + "@think=0", "stream": stream,
                            "messages": [{"role": "user", "content": "test"}],
                        }).encode("utf-8")
                        with mock.patch.dict(module.CONFIG, SYNTHETIC_CONFIG, clear=True):
                            if stream:
                                response = mock.MagicMock()
                                response.iter_text.return_value = iter([NORMAL_RESPONSE])
                                client = mock.MagicMock()
                                client.stream.return_value.__enter__.return_value = response
                                with mock.patch.object(module, "HAS_HTTPX", True):
                                    if module is single:
                                        with mock.patch.object(module.httpx, "Client") as constructor:
                                            constructor.return_value.__enter__.return_value = client
                                            handler.handle_chat(body)
                                    else:
                                        with mock.patch.object(module, "_get_httpx_client", return_value=client):
                                            handler._handle_chat(body)
                                self.assertIn(b"data: [DONE]", handler.wfile.getvalue())
                                request_body = client.stream.call_args.kwargs["content"]
                                headers = client.stream.call_args.kwargs["headers"]
                            else:
                                response = mock.Mock()
                                response.read.return_value = NORMAL_RESPONSE.encode("utf-8")
                                with mock.patch.object(module.urllib.request, "urlopen", return_value=response) as open_url:
                                    if module is single:
                                        handler.handle_chat(body)
                                    else:
                                        handler._handle_chat(body)
                                self.assertEqual(handler.send_json.call_args.args[0]["model"], name)
                                request = open_url.call_args.args[0]
                                request_body, headers = request.data, dict(request.header_items())
                        if name == "gemini-3.8-flash":
                            self.assert_request_preset(request_body, headers, 1, 4, "56fdd199312815e2", 1)
                        else:
                            self.assertNotIn(ROUTING_HEADER.lower(), {key.lower() for key in headers})
                            if isinstance(request_body, bytes):
                                request_body = request_body.decode("utf-8")
                            payload = json.loads(json.loads(parse_qs(request_body)["f.req"][0])[1])
                            self.assertEqual(payload[17], [[4]])
                            self.assertEqual(payload[79], 1)

    def test_modular_header_builder_keeps_optional_arguments_compatible(self):
        with mock.patch.dict(gemini.CONFIG, SYNTHETIC_CONFIG, clear=True):
            headers = gemini._build_headers()
            self.assertEqual(headers["Origin"], "https://gemini.google.com")
            self.assertNotIn("Cookie", headers)
            self.assertNotIn("Authorization", headers)

    def test_sync_requests_use_named_preset_in_payload_and_routing_header(self):
        for module in (single, gemini):
            for family, (mode, ticket) in FAMILIES.items():
                for level, (think, variant) in LEVELS.items():
                    with self.subTest(module=module.__name__, family=family, level=level):
                        response = mock.Mock()
                        response.read.return_value = NORMAL_RESPONSE.encode("utf-8")
                        with mock.patch.dict(module.CONFIG, SYNTHETIC_CONFIG, clear=True):
                            with mock.patch.object(module.urllib.request, "urlopen", return_value=response) as open_url:
                                if module is single:
                                    result = module.extract_response_text(module.gemini_stream_generate("test", mode, think))
                                else:
                                    result = module.generate("test", mode, think)
                        self.assertEqual(result, NORMAL_TEXT.strip())
                        request = open_url.call_args.args[0]
                        self.assert_request_preset(request.data, dict(request.header_items()), mode, think, ticket, variant)
                        self.assertNotIn("Cookie", dict(request.header_items()))

    def test_sse_requests_use_named_preset_in_payload_and_routing_header(self):
        for module in (single, gemini):
            for family, (mode, ticket) in FAMILIES.items():
                for level, (think, variant) in LEVELS.items():
                    with self.subTest(module=module.__name__, family=family, level=level):
                        response = mock.MagicMock()
                        response.iter_text.return_value = iter([NORMAL_RESPONSE])
                        client = mock.MagicMock()
                        client.stream.return_value.__enter__.return_value = response
                        with mock.patch.dict(module.CONFIG, SYNTHETIC_CONFIG, clear=True):
                            with mock.patch.object(module, "HAS_HTTPX", True):
                                if module is single:
                                    with mock.patch.object(module.httpx, "Client") as constructor:
                                        constructor.return_value.__enter__.return_value = client
                                        text = "".join(module.gemini_stream_generate_iter("test", mode, think))
                                else:
                                    with mock.patch.object(module, "_get_httpx_client", return_value=client):
                                        text = "".join(module.generate_stream("test", mode, think))
                        self.assertEqual(text, NORMAL_TEXT)
                        self.assertEqual(client.stream.call_count, 1)
                        request_kwargs = client.stream.call_args.kwargs
                        self.assert_request_preset(request_kwargs["content"], request_kwargs["headers"], mode, think, ticket, variant)
                        self.assertNotIn("Cookie", request_kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
