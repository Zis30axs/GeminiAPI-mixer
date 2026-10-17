import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

from gemini_web2api import gemini


_spec = importlib.util.spec_from_file_location(
    "gemini_single_file", Path(__file__).resolve().parents[1] / "gemini_web2api.py"
)
single = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(single)

# Actual anonymous upstream rejection; the error frame is only 121 bytes.
REJECTED_RESPONSE = (
    ")]}'\n\n121\n"
    '[["wrb.fr",null,null,null,null,[9,null,[["type.googleapis.com/'
    'assistant.boq.bard.application.BardErrorInfo",[1060]]]]]]\n'
    '55\n[["di",73],["af.httprm",73,"-3710870456854416867",2]]\n'
    '25\n[["e",4,null,null,214]]\n'
)
NORMAL_TEXT = "A successful response with enough text to exercise the existing response parser. " * 2
NORMAL_RESPONSE = json.dumps([
    ["wrb.fr", None, json.dumps([None, None, None, None, [["candidate", [NORMAL_TEXT]]]])]
]) + "\n"


class UpstreamResponseTests(unittest.TestCase):
    def test_nested_error_frame_raises_in_both_parsers(self):
        self.assertEqual(len(REJECTED_RESPONSE), 214)
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                with self.assertRaisesRegex(RuntimeError, r"BardErrorInfo \[1060\]"):
                    module.extract_response_text(REJECTED_RESPONSE)

    def test_legacy_error_and_normal_response_in_both_parsers(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                with self.assertRaisesRegex(RuntimeError, r"BardErrorInfo \[1060\]"):
                    module.extract_response_text("BardErrorInfo [1060]")
                self.assertEqual(module.extract_response_text(NORMAL_RESPONSE), NORMAL_TEXT.strip())

    def _stream(self, module, chunks):
        response = mock.MagicMock()
        response.iter_text.return_value = iter(chunks)
        client = mock.MagicMock()
        client.stream.return_value.__enter__.return_value = response
        with mock.patch.dict(module.CONFIG, {"retry_attempts": 1, "cookie_file": None}):
            with mock.patch.object(module, "HAS_HTTPX", True):
                if module is single:
                    with mock.patch.object(module.httpx, "Client") as constructor:
                        constructor.return_value.__enter__.return_value = client
                        return list(module.gemini_stream_generate_iter("hi", 1, 4))
                with mock.patch.object(module, "_get_httpx_client", return_value=client):
                    return list(module.generate_stream("hi", 1, 4))

    def test_nested_error_split_across_stream_chunks_raises(self):
        split = REJECTED_RESPONSE.index("[1060]")
        chunks = [REJECTED_RESPONSE[:split], REJECTED_RESPONSE[split:]]
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                with self.assertRaisesRegex(RuntimeError, r"BardErrorInfo \[1060\]"):
                    self._stream(module, chunks)

    def test_normal_stream_still_yields_text(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                self.assertEqual("".join(self._stream(module, [NORMAL_RESPONSE])), NORMAL_TEXT)

    def test_build_discovery_supports_current_and_legacy_page_labels(self):
        for label in (
            "boq_gemini-web-uiserver_20261013.29_p0",
            "boq_assistant-bard-web-server_20260716.08_p0",
        ):
            with self.subTest(label=label):
                response = mock.Mock()
                response.read.return_value = json.dumps({"cfb2h": label}).encode()
                with mock.patch.dict(single.CONFIG, {"proxy": None}):
                    with mock.patch.object(single.urllib.request, "urlopen", return_value=response):
                        self.assertEqual(single.fetch_latest_bl(), label)


if __name__ == "__main__":
    unittest.main()
