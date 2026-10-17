"""Contract tests for public-provider API adaptation; no accounts or network."""
import copy
import unittest

from gemini_web2api.api_formats import format_result, stream_events, to_native
from gemini_web2api.providers import ProviderError


META = {"source": "build", "transport": "gemini_api", "request_id": "unit"}
MODEL = "build-test-low"
IMAGE = "data:image/png;base64,aGVsbG8="


def native(parts=None, finish="STOP", usage=None):
    value = {"candidates": [{"index": 0, "content": {"role": "model", "parts": parts if parts is not None else [{"text": "hello"}]}}]}
    if finish:
        value["candidates"][0]["finishReason"] = finish
    if usage is not None:
        value["usageMetadata"] = usage
    return value


class RequestTests(unittest.TestCase):
    def chat(self, **kwargs):
        return to_native(dict(messages=[{"role": "user", "content": "hi"}], **kwargs))

    def test_text_instructions_and_generation(self):
        result = to_native({"messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}], "temperature": 0.2, "max_tokens": 12, "stop": "END"})
        self.assertEqual(result["systemInstruction"], {"parts": [{"text": "rules"}]})
        self.assertEqual(result["contents"], [{"role": "user", "parts": [{"text": "hello"}]}])
        self.assertEqual(result["generationConfig"], {"temperature": 0.2, "maxOutputTokens": 12, "stopSequences": ["END"]})

    def test_tool_choice_and_schema(self):
        tool = {"type": "function", "function": {"name": "weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}
        for choice, mode in (("auto", "AUTO"), ("none", "NONE"), ("required", "ANY")):
            result = self.chat(tools=[tool], tool_choice=choice)
            self.assertEqual(result["toolConfig"]["functionCallingConfig"]["mode"], mode)
            self.assertEqual(result["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"], tool["function"]["parameters"])
        result = self.chat(tools=[tool], tool_choice={"type": "function", "function": {"name": "weather"}})
        self.assertEqual(result["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"], ["weather"])

    def test_real_tools_roundtrip_with_signature(self):
        response = format_result(native([{"functionCall": {"id": "original", "name": "weather", "args": {"city": "上海"}}, "thoughtSignature": "opaque"}]), MODEL, META)
        message = response["choices"][0]["message"]
        call = message["tool_calls"][0]
        self.assertEqual(call["id"], "original")
        self.assertEqual(call["extra_content"]["google"]["thought_signature"], "opaque")
        result = to_native({"messages": [{"role": "user", "content": "weather"}, message, {"role": "tool", "tool_call_id": call["id"], "content": '{"temp":21}'}]})
        self.assertEqual(result["contents"][1]["parts"][0]["thoughtSignature"], "opaque")
        self.assertEqual(result["contents"][2]["parts"][0], {"functionResponse": {"id": "original", "name": "weather", "response": {"temp": 21}}})

    def test_signature_only_part_and_signed_text_roundtrip(self):
        raw = native([{"text": "answer", "thoughtSignature": "text-sig"}, {"thoughtSignature": "tail-sig"}])
        response = format_result(raw, MODEL, META)
        message = response["choices"][0]["message"]
        self.assertEqual(message["content"], "answer")
        self.assertIsInstance(message["content"], str)
        request = to_native({"messages": [{"role": "user", "content": "hi"}, message]})
        self.assertEqual(request["contents"][-1]["parts"], raw["candidates"][0]["content"]["parts"])

    def test_hidden_reasoning_does_not_leak_but_signature_survives(self):
        response = format_result(native([{"text": "private thought", "thought": True, "thoughtSignature": "secret-signature"}, {"text": "answer"}]), MODEL, META)
        message = response["choices"][0]["message"]
        self.assertEqual(message["content"], "answer")
        self.assertNotIn("private thought", str(response))
        self.assertEqual(message["extra_content"]["google"]["native_parts"][0], {"thought": True, "thoughtSignature": "secret-signature"})

    def test_native_parts_cannot_override_visible_history(self):
        message = {"role": "assistant", "content": "visible", "extra_content": {"google": {"native_parts": [{"text": "different", "thoughtSignature": "sig"}]}}}
        with self.assertRaises(ProviderError):
            to_native({"messages": [{"role": "user", "content": "hi"}, message]})

    def test_responses_function_output_and_input_images(self):
        request = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "read"}, {"type": "input_image", "image_url": IMAGE}]}, {"type": "function_call", "call_id": "c1", "name": "read", "arguments": '{}', "extra_content": {"google": {"thought_signature": "sig"}}}, {"type": "function_call_output", "call_id": "c1", "output": "done"}], "instructions": "Be concise", "store": False}
        result = to_native(request, "responses")
        self.assertEqual(result["contents"][0]["parts"][1]["inlineData"]["data"], "aGVsbG8=")
        self.assertEqual(result["contents"][1]["parts"][0]["thoughtSignature"], "sig")
        self.assertEqual(result["contents"][2]["parts"][0]["functionResponse"]["response"], {"output": "done"})

    def test_responses_string_input_and_function_choice(self):
        result = to_native({"input": "hi", "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}}], "tool_choice": {"type": "function", "name": "f"}}, "responses")
        self.assertEqual(result["contents"][0]["parts"], [{"text": "hi"}])
        self.assertEqual(result["toolConfig"]["functionCallingConfig"], {"mode": "ANY", "allowedFunctionNames": ["f"]})

    def test_image_input_is_inline_only(self):
        for image in ("https://example.com/a.png", "http://127.0.0.1/private", "file:///a.png", "data:image/png;base64,???", "data:text/html;base64,aGk="):
            with self.subTest(image=image), self.assertRaises(ProviderError) as ctx:
                to_native({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image}}]}]})
            self.assertEqual(ctx.exception.status, 400)

    def test_unsupported_parameters_are_not_silently_dropped(self):
        for fields in ({"n": 2}, {"logprobs": True}, {"store": True}, {"seed": 1}, {"parallel_tool_calls": False}, {"stream_options": {"include_usage": False}}, {"audio": {}}, {"response_format": {"type": "json_schema"}}, {"tools": [{"type": "web_search"}]}):
            with self.subTest(fields=fields), self.assertRaises(ProviderError):
                self.chat(**fields)
        for fields in ({"background": True}, {"previous_response_id": "resp_old"}, {"reasoning": {"effort": "low", "summary": "auto"}}, {"input": [{"type": "input_file", "file_id": "f1"}]}):
            req = {"input": "hi"}
            req.update(fields)
            with self.subTest(fields=fields), self.assertRaises(ProviderError):
                to_native(req, "responses")

    def test_malformed_arguments_and_orphan_tool_results_rejected(self):
        for arguments in ("bad", "[]", "null"):
            with self.subTest(arguments=arguments), self.assertRaises(ProviderError):
                to_native({"messages": [{"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "f", "arguments": arguments}}]}]})
        with self.assertRaises(ProviderError):
            to_native({"messages": [{"role": "tool", "tool_call_id": "missing", "content": "hi"}]})

    def test_native_is_deep_copied_and_thinking_preserved(self):
        req = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "generationConfig": {"thinkingConfig": {"thinkingLevel": "LOW"}}}
        expected = copy.deepcopy(req)
        result = to_native(req, "gemini")
        self.assertEqual(result, expected)
        result["contents"][0]["parts"][0]["text"] = "changed"
        self.assertEqual(req, expected)

    def test_native_unsupported_tools_files_images_multiple_candidates(self):
        for extra in ({"tools": [{"googleSearch": {}}]}, {"generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}, {"generationConfig": {"candidateCount": 2}}, {"contents": [{"parts": [{"fileData": {"fileUri": "gs://bucket/image"}}]}]}):
            req = {"contents": [{"parts": [{"text": "hi"}]}]}
            req.update(extra)
            with self.subTest(extra=extra), self.assertRaises(ProviderError):
                to_native(req, "gemini")


class ResultTests(unittest.TestCase):
    def test_usage_is_actual_and_missing_counts_not_invented(self):
        result = format_result(native(usage={"promptTokenCount": 4, "candidatesTokenCount": 2, "totalTokenCount": 6}), MODEL, META)
        self.assertEqual(result["usage"], {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})
        self.assertIsNone(format_result(native(), MODEL, META)["usage"])
        result = format_result(native(usage={"promptTokenCount": 4}), MODEL, META, "responses")
        self.assertEqual(result["usage"], {"input_tokens": 4})
        result = format_result(native(usage={"promptTokenCount": 4, "candidatesTokenCount": 2, "thoughtsTokenCount": 3, "totalTokenCount": 9}), MODEL, META)
        self.assertEqual(result["usage"], {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 3}})

    def test_source_alias_and_native_metadata(self):
        result = format_result(native(), MODEL, META)
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(result["provider_metadata"], META)
        raw = native()
        result = format_result(raw, MODEL, META, "gemini")
        self.assertEqual(result["candidates"], raw["candidates"])
        self.assertEqual(result["providerMetadata"], META)

    def test_finish_reasons_and_no_empty_success(self):
        self.assertEqual(format_result(native(finish="MAX_TOKENS"), MODEL, META)["choices"][0]["finish_reason"], "length")
        self.assertEqual(format_result(native([], "SAFETY"), MODEL, META)["choices"][0]["finish_reason"], "content_filter")
        self.assertEqual(format_result(native([], "MAX_TOKENS"), MODEL, META, "responses")["status"], "incomplete")
        for value in (native([], "STOP"), {"candidates": []}, native(finish="MALFORMED_FUNCTION_CALL")):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                format_result(value, MODEL, META)

    def test_prompt_block_is_not_empty_success(self):
        result = format_result({"promptFeedback": {"blockReason": "SAFETY"}}, MODEL, META, "responses")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_details"], {"reason": "content_filter"})

    def test_multiple_candidates_and_image_output_rejected(self):
        multi = native()
        multi["candidates"].append(copy.deepcopy(multi["candidates"][0]))
        for value in (multi, native([{"inlineData": {"mimeType": "image/png", "data": "aGk="}}])):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                format_result(value, MODEL, META)

    def test_responses_tool_result_roundtrip(self):
        result = format_result(native([{"functionCall": {"name": "f", "args": {"n": 1}}, "thoughtSignature": "sig"}]), MODEL, META, "responses")
        call = result["output"][0]
        req = to_native({"input": [{"role": "user", "content": "hi"}, call, {"type": "function_call_output", "call_id": call["call_id"], "output": "1"}]}, "responses")
        self.assertEqual(req["contents"][1]["parts"][0]["thoughtSignature"], "sig")
        self.assertEqual(req["contents"][2]["parts"][0]["functionResponse"]["response"], {"output": 1})

    def test_responses_preserves_interleaved_text_call_and_tail_signature(self):
        parts = [{"text": "before"}, {"functionCall": {"id": "call", "name": "f", "args": {}}, "thoughtSignature": "sig"}, {"thoughtSignature": "tail"}, {"text": "after"}]
        result = format_result(native(parts), MODEL, META, "responses")
        self.assertEqual([p["type"] for p in result["output"]], ["message", "function_call", "message"])
        req = to_native({"input": [{"role": "user", "content": "hi"}] + result["output"]}, "responses")
        self.assertEqual(req["contents"][-1]["parts"], parts)


class StreamingTests(unittest.TestCase):
    def test_consumes_lazily_and_yields_real_deltas(self):
        consumed = []

        def upstream():
            consumed.append(1)
            yield native([{"text": "one"}], None)
            consumed.append(2)
            yield native([{"text": "two"}], "STOP")

        stream = stream_events(upstream(), MODEL, META)
        next(stream)  # Role only, no upstream consumption.
        self.assertEqual(consumed, [])
        first = next(stream)[1]
        self.assertEqual(first["choices"][0]["delta"]["content"], "one")
        self.assertEqual(consumed, [1])
        rest = list(stream)
        self.assertEqual(rest[-1], (None, "[DONE]"))

    def test_responses_lifecycle_and_sequences(self):
        events = list(stream_events([native([{"text": "a"}], None), native([{"text": "b"}], "STOP", {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3})], MODEL, META, "responses"))
        types = [name for name, _ in events]
        self.assertEqual(types[:2], ["response.created", "response.in_progress"])
        self.assertEqual(types[-1], "response.completed")
        self.assertIn("response.output_item.added", types)
        self.assertIn("response.content_part.added", types)
        self.assertIn("response.output_text.done", types)
        self.assertIn("response.content_part.done", types)
        self.assertIn("response.output_item.done", types)
        self.assertEqual([p["sequence_number"] for _, p in events], list(range(1, len(events) + 1)))
        response = events[-1][1]["response"]
        self.assertEqual(response["id"], events[0][1]["response"]["id"])
        self.assertEqual(response["output"][0]["content"][0]["text"], "ab")
        self.assertEqual(response["usage"], {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})

    def test_stream_error_never_emits_completed_or_done_marker(self):
        def upstream():
            yield native([{"text": "partial"}], None)
            raise ProviderError(504, "timeout", "upstream timed out")

        for kind in ("chat", "responses", "gemini"):
            seen = []
            with self.subTest(kind=kind), self.assertRaises(ProviderError):
                for event in stream_events(upstream(), MODEL, META, kind):
                    seen.append(event)
            self.assertNotIn((None, "[DONE]"), seen)
            self.assertNotIn("response.completed", [e[0] for e in seen])

    def test_eof_without_finish_reason_is_error(self):
        for kind in ("chat", "responses", "gemini"):
            with self.subTest(kind=kind), self.assertRaises(ProviderError) as ctx:
                list(stream_events([native([{"text": "partial"}], None)], MODEL, META, kind))
            self.assertEqual(ctx.exception.code, "truncated_upstream_stream")

    def test_tool_stream_stable_ids_signature_and_deduplication(self):
        call = {"functionCall": {"name": "f", "args": {"n": 1}}, "thoughtSignature": "sig"}
        chunks = [native([call], None), native([call], "STOP")]
        events = list(stream_events(chunks, MODEL, META))
        deltas = [p["choices"][0]["delta"]["tool_calls"][0] for _, p in events if isinstance(p, dict) and p["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual(len(deltas), 1)
        self.assertTrue(deltas[0]["id"])
        self.assertEqual(deltas[0]["extra_content"]["google"]["thought_signature"], "sig")
        self.assertEqual(events[-2][1]["choices"][0]["finish_reason"], "tool_calls")
        responses = list(stream_events(chunks, MODEL, META, "responses"))
        added = [p for n, p in responses if n == "response.output_item.added"][0]
        done = [p for n, p in responses if n == "response.output_item.done"][0]
        self.assertEqual(added["item"]["id"], done["item"]["id"])
        self.assertEqual(done["item"]["arguments"], '{"n":1}')
        self.assertEqual(done["item"]["extra_content"]["google"]["thought_signature"], "sig")

    def test_changed_complete_tool_call_is_rejected(self):
        chunks = [native([{"functionCall": {"name": "f", "args": {"n": 1}}}], None), native([{"functionCall": {"name": "f", "args": {"n": 2}}}], "STOP")]
        with self.assertRaises(ProviderError):
            list(stream_events(chunks, MODEL, META, "responses"))

    def test_hidden_thought_and_empty_signature_stream(self):
        chunks = [native([{"thought": True, "text": "private thought"}, {"text": "answer"}], None), native([{"thoughtSignature": "tail"}], "STOP")]
        events = list(stream_events(chunks, MODEL, META))
        self.assertNotIn("private thought", str(events))
        self.assertIn("tail", str(events))

    def test_responses_stream_preserves_text_call_text_and_signature_order(self):
        parts = [{"text": "before"}, {"functionCall": {"id": "call", "name": "f", "args": {}}, "thoughtSignature": "sig"}, {"thoughtSignature": "tail"}, {"text": "after"}]
        chunks = [native([part], "STOP" if i == len(parts) - 1 else None) for i, part in enumerate(parts)]
        events = list(stream_events(chunks, MODEL, META, "responses"))
        response = events[-1][1]["response"]
        self.assertEqual([p["type"] for p in response["output"]], ["message", "function_call", "message"])
        req = to_native({"input": [{"role": "user", "content": "hi"}] + response["output"]}, "responses")
        self.assertEqual(req["contents"][-1]["parts"], parts)

    def test_max_tokens_and_safety_are_incomplete(self):
        for finish, expected in (("MAX_TOKENS", "max_output_tokens"), ("SAFETY", "content_filter")):
            events = list(stream_events([native([], finish)], MODEL, META, "responses"))
            self.assertEqual(events[-1][0], "response.incomplete")
            self.assertEqual(events[-1][1]["response"]["incomplete_details"]["reason"], expected)

    def test_native_stream_chunks_remain_native(self):
        value = native([{"text": "hi"}])
        events = list(stream_events([value], MODEL, META, "gemini"))
        self.assertEqual(events[0][1]["candidates"], value["candidates"])
        self.assertEqual(events[0][1]["providerMetadata"], META)


if __name__ == "__main__":
    unittest.main()
