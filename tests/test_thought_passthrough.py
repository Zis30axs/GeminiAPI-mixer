"""Synthetic upstream thoughts must reach clients without becoming answer text.

No account files, production configuration, or upstream network are used here.
"""
import contextlib
import io
import json
import unittest
from unittest import mock

from gemini_web2api import gemini, server
from gemini_web2api.thoughts import GenerationText, StreamAccumulator, normalize_delta
from tests.test_upstream_protocol import REJECTED_RESPONSE, single


CONFIG = {
    "cookie_file": None, "proxy": None, "retry_attempts": 1,
    "retry_delay_sec": 0, "request_timeout_sec": 5, "xsrf_token": None,
    "auth_user": None, "gemini_bl": "synthetic-build", "temporary_chats": False,
    "log_requests": False, "default_model": "gemini-3.6-flash", "api_keys": [],
}
ANSWER = "最终答案：42。"
THOUGHT = "先检查已知条件，再计算结果。"


def frame(answer="", thought=None, second_candidate=False):
    candidate = [None] * 38
    candidate[0] = "synthetic-candidate"
    candidate[1] = [answer]
    if thought is not None:
        candidate[37] = [[thought]]
    candidates = [candidate]
    if second_candidate:
        other = [None] * 38
        other[1] = ["DO NOT SELECT SECOND ANSWER"]
        other[37] = [["DO NOT SELECT SECOND THOUGHT"]]
        candidates.append(other)
    return json.dumps([["wrb.fr", None, json.dumps([None] * 4 + [candidates])]]) + "\n"


def data_events(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


class ThoughtParserTests(unittest.TestCase):
    def test_generation_text_is_a_string_with_separate_thoughts(self):
        value = GenerationText(ANSWER, THOUGHT)
        self.assertIsInstance(value, str)
        self.assertEqual(value, ANSWER)
        self.assertEqual(json.loads(json.dumps({"text": value})), {"text": ANSWER})
        self.assertEqual(value.reasoning_content, THOUGHT)

    def test_full_parser_uses_only_first_candidate_and_preserves_thoughts(self):
        raw = frame(ANSWER, THOUGHT, second_candidate=True)
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                parsed = module.extract_response_text(raw)
                self.assertEqual(parsed, ANSWER)
                self.assertEqual(parsed.reasoning_content, THOUGHT)
                self.assertNotIn(THOUGHT, parsed)

    def test_full_parser_keeps_thoughts_when_later_answer_frame_omits_them(self):
        raw = frame("", THOUGHT) + frame(ANSWER)
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                parsed = module.extract_response_text(raw)
                self.assertEqual(parsed, ANSWER)
                self.assertEqual(parsed.reasoning_content, THOUGHT)

    def test_full_parser_ignores_shorter_cumulative_repeats_but_accepts_final_revision(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                parsed = module.extract_response_text(frame(ANSWER, THOUGHT) + frame(ANSWER[:2], THOUGHT[:2]))
                self.assertEqual(parsed, ANSWER)
                self.assertEqual(parsed.reasoning_content, THOUGHT)
                revised = module.extract_response_text(frame("initial", "first") + frame("revised", "second"))
                self.assertEqual(revised, "revised")
                self.assertEqual(revised.reasoning_content, "second")

    def test_missing_or_malformed_thoughts_are_ordinary_answer_compatible(self):
        for thought in (None, [], {}, 12):
            for module in (single, gemini):
                with self.subTest(module=module.__name__, thought=thought):
                    parsed = module.extract_response_text(frame(ANSWER, thought))
                    self.assertEqual(parsed, ANSWER)
                    self.assertEqual(parsed.reasoning_content, "")

    def test_short_frames_are_not_dropped_by_old_minimum_length_heuristic(self):
        # A valid compact answer frame can be shorter than the previous 200-byte guard.
        raw = json.dumps([["wrb.fr", None, json.dumps([None] * 4 + [[["id", ["ok"]]]])]]) + "\n"
        self.assertLess(len(raw), 200)
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.extract_response_text(raw), "ok")

    def test_accumulator_deduplicates_independent_cumulative_channels(self):
        accumulator = StreamAccumulator(lambda text, strip=True: text.strip() if strip else text)
        emitted = []
        for raw in (frame("", "先"), frame("", "先"), frame("", "先检查"),
                    frame("答", "先"), frame("答案", "先检查"), frame("答", "先检查")):
            emitted.extend(accumulator.feed(raw))
        self.assertEqual(emitted, [
            {"reasoning_content": "先"}, {"reasoning_content": "检查"},
            {"content": "答"}, {"content": "案"},
        ])

    def test_accumulator_rejects_rewritten_thought_or_answer_prefix(self):
        for field in ("content", "reasoning_content"):
            with self.subTest(field=field):
                accumulator = StreamAccumulator(lambda text, strip=True: text)
                accumulator.feed(frame("first" if field == "content" else "",
                                       "first" if field == "reasoning_content" else None))
                with self.assertRaises(RuntimeError):
                    accumulator.feed(frame("changed" if field == "content" else "",
                                           "changed" if field == "reasoning_content" else None))

    def test_normalize_delta_preserves_legacy_answer_and_separate_channels(self):
        self.assertEqual(normalize_delta("legacy"), {"content": "legacy"})
        self.assertEqual(normalize_delta({"reasoning_content": THOUGHT}), {"reasoning_content": THOUGHT})
        self.assertEqual(normalize_delta({"content": ANSWER}), {"content": ANSWER})


class ThoughtGeneratorTests(unittest.TestCase):
    def collect(self, module, chunks, include_thoughts=False, attempts=1):
        response = mock.MagicMock()
        response.iter_text.return_value = iter(chunks)
        client = mock.MagicMock()
        client.stream.return_value.__enter__.return_value = response
        self.last_stream_client = client
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(module.CONFIG, {**CONFIG, "retry_attempts": attempts}, clear=True))
            stack.enter_context(mock.patch.object(module, "HAS_HTTPX", True))
            if module is single:
                constructor = stack.enter_context(mock.patch.object(module.httpx, "Client"))
                constructor.return_value.__enter__.return_value = client
                iterator = module.gemini_stream_generate_iter("synthetic", 1, 4, include_thoughts=include_thoughts)
            else:
                stack.enter_context(mock.patch.object(module, "_get_httpx_client", return_value=client))
                iterator = module.generate_stream("synthetic", 1, 4, include_thoughts=include_thoughts)
            result = list(iterator)
        return result, client

    def test_split_repeated_frames_produce_one_copy_of_thoughts_and_answer(self):
        raw = frame("", "先检查") + frame("", "先检查") + frame("", THOUGHT) + frame(ANSWER, THOUGHT) + frame(ANSWER, THOUGHT)
        chunks = [raw[:43], raw[43:80], raw[80:145], raw[145:]]
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                deltas, client = self.collect(module, chunks, include_thoughts=True)
                self.assertEqual("".join(delta.get("reasoning_content", "") for delta in deltas), THOUGHT)
                self.assertEqual("".join(delta.get("content", "") for delta in deltas), ANSWER)
                self.assertTrue(all(set(delta) in ({"reasoning_content"}, {"content"}) for delta in deltas))
                self.assertEqual(client.stream.call_count, 1)

    def test_default_stream_contract_still_yields_only_answer_strings(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                deltas, _ = self.collect(module, [frame("", THOUGHT), frame(ANSWER, THOUGHT)])
                self.assertTrue(all(isinstance(delta, str) for delta in deltas))
                self.assertEqual("".join(deltas), ANSWER)

    def test_no_httpx_fallback_keeps_thoughts_optional_and_separate(self):
        for module in (single, gemini):
            for include_thoughts in (False, True):
                with self.subTest(module=module.__name__, include_thoughts=include_thoughts):
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch.dict(module.CONFIG, CONFIG, clear=True))
                        stack.enter_context(mock.patch.object(module, "HAS_HTTPX", False))
                        if module is single:
                            stack.enter_context(mock.patch.object(module, "gemini_stream_generate", return_value=frame(ANSWER, THOUGHT)))
                            iterator = module.gemini_stream_generate_iter("synthetic", 1, 4, include_thoughts=include_thoughts)
                        else:
                            stack.enter_context(mock.patch.object(module, "generate", return_value=GenerationText(ANSWER, THOUGHT)))
                            iterator = module.generate_stream("synthetic", 1, 4, include_thoughts=include_thoughts)
                        deltas = list(iterator)
                    self.assertEqual(deltas, [{"reasoning_content": THOUGHT}, {"content": ANSWER}]
                                     if include_thoughts else [ANSWER])

    def test_final_frame_without_newline_is_preserved(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                deltas, _ = self.collect(module, [frame(ANSWER, THOUGHT).rstrip("\n")], include_thoughts=True)
                self.assertEqual("".join(delta.get("content", "") for delta in deltas), ANSWER)
                self.assertEqual("".join(delta.get("reasoning_content", "") for delta in deltas), THOUGHT)

    def test_rejection_after_thought_is_not_retried_or_silently_completed(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                with self.assertRaisesRegex(RuntimeError, "BardErrorInfo"):
                    self.collect(module, [frame("", THOUGHT), REJECTED_RESPONSE],
                                 include_thoughts=True, attempts=2)
                self.assertEqual(self.last_stream_client.stream.call_count, 1)


class ThoughtEndpointTests(unittest.TestCase):
    def handler(self, module, path="/v1/chat/completions"):
        handler_type = single.GeminiHandler if module is single else server.GeminiHandler
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        return handler

    def call(self, module, kind="chat", stream=False, tools=False, raw=None):
        if kind == "chat":
            path = "/v1/chat/completions"
            payload = {"messages": [{"role": "user", "content": "synthetic"}]}
            method = "handle_chat" if module is single else "_handle_chat"
        elif kind == "responses":
            path = "/v1/responses"
            payload = {"input": "synthetic"}
            method = "handle_responses" if module is single else "_handle_responses"
        else:
            path = "/v1beta/models/gemini-3.6-flash:generateContent"
            payload = {"contents": [{"role": "user", "parts": [{"text": "synthetic"}]}]}
            method = "_handle_google_generate"
        payload.update(model="gemini-3.6-flash", stream=stream)
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": "lookup", "parameters": {"type": "object"},
            }}]
        handler = self.handler(module, path)
        response = mock.Mock()
        response.read.return_value = (raw or frame(ANSWER, THOUGHT)).encode()
        with mock.patch.dict(module.CONFIG, CONFIG, clear=True):
            with mock.patch.object(module.urllib.request, "urlopen", return_value=response):
                if kind == "google":
                    getattr(handler, method)(json.dumps(payload).encode(), stream=stream)
                else:
                    getattr(handler, method)(json.dumps(payload).encode())
        return handler, handler.wfile.getvalue().decode()

    def test_chat_nonstream_exposes_thoughts_without_client_reasoning_parameters(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                handler, body = self.call(module)
                self.assertEqual(handler.send_response.call_args.args[0], 200)
                message = json.loads(body)["choices"][0]["message"]
                self.assertEqual(message["content"], ANSWER)
                self.assertEqual(message["reasoning_content"], THOUGHT)

    def test_ordinary_answer_omits_absent_thoughts_and_thought_only_keeps_metadata(self):
        for module in (single, gemini):
            for answer, thought in ((ANSWER, None), ("", THOUGHT)):
                with self.subTest(module=module.__name__, answer=answer, thought=thought):
                    _, body = self.call(module, raw=frame(answer, thought))
                    message = json.loads(body)["choices"][0]["message"]
                    self.assertEqual(message["content"], answer or None)
                    if thought:
                        self.assertEqual(message["reasoning_content"], thought)
                    else:
                        self.assertNotIn("reasoning_content", message)

    def test_tool_call_parsing_keeps_reasoning_for_both_chat_output_modes(self):
        raw = frame('```tool_call\n{"name":"lookup","arguments":{"x":1}}\n```', THOUGHT)
        for module in (single, gemini):
            for stream in (False, True):
                with self.subTest(module=module.__name__, stream=stream):
                    _, body = self.call(module, tools=True, stream=stream, raw=raw)
                    choice = (data_events(body)[0] if stream else json.loads(body))["choices"][0]
                    message = choice["delta" if stream else "message"]
                    self.assertEqual(message["reasoning_content"], THOUGHT)
                    self.assertEqual(message["tool_calls"][0]["function"]["name"], "lookup")
                    self.assertEqual(choice["finish_reason"], "tool_calls")
                    self.assertFalse(message["content"])

    def test_google_returns_separate_thought_true_part(self):
        for module in (single, gemini):
            for stream in (False, True):
                with self.subTest(module=module.__name__, stream=stream):
                    if stream and module is gemini:
                        # Modular native streaming uses the real stream generator; cover separately below.
                        continue
                    _, body = self.call(module, kind="google", stream=stream)
                    data = data_events(body)[0] if stream else json.loads(body)
                    parts = data["candidates"][0]["content"]["parts"]
                    self.assertEqual([part["text"] for part in parts if part.get("thought")], [THOUGHT])
                    self.assertEqual([part["text"] for part in parts if not part.get("thought")], [ANSWER])

    def test_responses_nonstream_and_buffered_stream_expose_reasoning_summary(self):
        for module in (single, gemini):
            for stream in (False, True):
                with self.subTest(module=module.__name__, stream=stream):
                    _, body = self.call(module, kind="responses", stream=stream)
                    if stream:
                        events = data_events(body)
                        data = events[-1]["response"]
                        reasoning_deltas = [event["delta"] for event in events
                                            if event["type"] == "response.reasoning_summary_text.delta"]
                        self.assertEqual("".join(reasoning_deltas), THOUGHT)
                        self.assertEqual([event["sequence_number"] for event in events], list(range(1, len(events) + 1)))
                    else:
                        data = json.loads(body)
                    reasoning = [item for item in data["output"] if item["type"] == "reasoning"]
                    self.assertEqual(len(reasoning), 1)
                    self.assertEqual(reasoning[0]["summary"], [{"type": "summary_text", "text": THOUGHT}])
                    messages = [item for item in data["output"] if item["type"] == "message"]
                    self.assertEqual(messages[0]["content"][0]["text"], ANSWER)

    def stream_call(self, module, iterator, google=False):
        path = "/v1beta/models/gemini-3.6-flash:streamGenerateContent" if google else "/v1/chat/completions"
        handler = self.handler(module, path)
        payload = {"model": "gemini-3.6-flash", "stream": True,
                   "messages": [{"role": "user", "content": "synthetic"}]}
        if google:
            payload["contents"] = [{"role": "user", "parts": [{"text": "synthetic"}]}]
        target = single if module is single else server
        name = "gemini_stream_generate_iter" if module is single else "generate_stream"
        with mock.patch.dict(module.CONFIG, CONFIG, clear=True):
            with mock.patch.object(target, name, return_value=iterator) as generated:
                if google:
                    handler._handle_google_generate(json.dumps(payload).encode(), stream=True)
                else:
                    method = handler.handle_chat if module is single else handler._handle_chat
                    method(json.dumps(payload).encode())
        return handler, handler.wfile.getvalue().decode(), generated

    def test_chat_stream_separates_reasoning_deltas_and_accepts_legacy_strings(self):
        for module in (single, gemini):
            with self.subTest(module=module.__name__):
                _, body, generated = self.stream_call(module, iter([
                    {"reasoning_content": THOUGHT}, {"content": ANSWER[:3]}, ANSWER[3:],
                ]))
                deltas = [event["choices"][0]["delta"] for event in data_events(body)]
                self.assertEqual(deltas[0], {"role": "assistant"})
                self.assertEqual("".join(delta.get("reasoning_content", "") for delta in deltas), THOUGHT)
                self.assertEqual("".join(delta.get("content", "") for delta in deltas), ANSWER)
                self.assertEqual(body.count("data: [DONE]"), 1)
                self.assertTrue(generated.call_args.kwargs.get("include_thoughts"))

    def test_modular_google_stream_marks_thoughts_without_mixing_answer(self):
        _, body, _ = self.stream_call(gemini, iter([
            {"reasoning_content": THOUGHT}, {"content": ANSWER},
        ]), google=True)
        parts = [part for event in data_events(body) for candidate in event["candidates"]
                 for part in candidate.get("content", {}).get("parts", [])]
        self.assertEqual([part["text"] for part in parts if part.get("thought")], [THOUGHT])
        self.assertEqual([part["text"] for part in parts if not part.get("thought")], [ANSWER])

    def test_monolithic_initial_failure_returns_json_502_before_sse(self):
        def rejected():
            raise RuntimeError("synthetic initial rejection")
            yield
        handler, body, generated = self.stream_call(single, rejected())
        self.assertEqual(handler.send_response.call_args_list, [mock.call(502)])
        self.assertIn("synthetic initial rejection", json.loads(body)["error"]["message"])
        self.assertNotIn("data:", body)
        generated.assert_called_once()

    def test_monolithic_error_after_first_thought_closes_partial_stream_without_done(self):
        def interrupted():
            yield {"reasoning_content": THOUGHT}
            raise RuntimeError("synthetic later rejection")
        handler, body, generated = self.stream_call(single, interrupted())
        self.assertEqual(handler.send_response.call_args_list, [mock.call(200)])
        deltas = [event["choices"][0]["delta"] for event in data_events(body)]
        self.assertEqual(deltas, [{"role": "assistant"}, {"reasoning_content": THOUGHT}])
        self.assertNotIn("[DONE]", body)
        self.assertNotIn("synthetic later rejection", body)
        generated.assert_called_once()


if __name__ == "__main__":
    unittest.main()
