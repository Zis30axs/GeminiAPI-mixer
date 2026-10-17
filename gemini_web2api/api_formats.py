"""Strict, stateless API adapters for authorized Gemini-native providers.

No remote images are fetched and no token counts or thought signatures are invented.
Streaming consumes one upstream event at a time; errors propagate to the HTTP layer.
"""
import base64
import binascii
import copy
import json
import re
import time
import uuid

from .providers import ProviderError


def _fail(message, code="unsupported_parameter", status=400):
    raise ProviderError(status, code, message)


def _object(value, label):
    if not isinstance(value, dict):
        _fail("%s must be an object" % label, "invalid_request")
    return value


def _keys(value, allowed, label):
    _object(value, label)
    unknown = set(value) - set(allowed)
    if unknown:
        _fail("Unsupported %s field(s): %s" % (label, ", ".join(sorted(unknown))))


def _json(value, label):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        _fail("%s must contain valid JSON" % label, "invalid_request")


def _arguments(value):
    if not isinstance(value, str):
        _fail("Function arguments must be a JSON object string", "invalid_request")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        _fail("Function arguments must be valid JSON", "invalid_request")
    if not isinstance(parsed, dict):
        _fail("Function arguments must decode to an object", "invalid_request")
    _json(parsed, "Function arguments")
    return parsed


def _image(url):
    if not isinstance(url, str) or not url.startswith("data:"):
        _fail("Only inline image data URLs are supported; remote URLs and file IDs are disabled")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif|heic|heif));base64,([A-Za-z0-9+/]*={0,2})", url)
    if not match:
        _fail("Image must be a supported base64 image data URL", "invalid_request")
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        _fail("Invalid base64 image", "invalid_request")
    if not data:
        _fail("Image data must not be empty", "invalid_request")
    if len(data) > 20 * 1024 * 1024:
        _fail("Inline image exceeds 20 MiB", "invalid_request")
    return {"inlineData": {"mimeType": match.group(1), "data": match.group(2)}}


def _google_extra(value):
    if value is None:
        return {}
    _keys(value, {"google"}, "extra_content")
    google = value.get("google", {})
    _keys(google, {"thought_signature", "native_parts"}, "extra_content.google")
    if "thought_signature" in google and not isinstance(google["thought_signature"], str):
        _fail("thought_signature must be a string", "invalid_request")
    return google


def _parts(content, responses=False):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        _fail("Message content must be text or an array", "invalid_request")
    result = []
    for part in content:
        _object(part, "content part")
        kind = part.get("type")
        if kind in ({"input_text", "output_text"} if responses else {"text"}):
            _keys(part, {"type", "text", "extra_content", "annotations", "logprobs"}, "text part")
            if part.get("annotations") or part.get("logprobs"):
                _fail("Annotated output history is not supported")
            if not isinstance(part.get("text"), str):
                _fail("Text part requires text", "invalid_request")
            converted = {"text": part["text"]}
            extra = _google_extra(part.get("extra_content"))
            if "native_parts" in extra:
                _fail("native_parts is supported at message/item level only")
            if "thought_signature" in extra:
                converted["thoughtSignature"] = extra["thought_signature"]
            result.append(converted)
        elif kind == ("input_image" if responses else "image_url"):
            _keys(part, {"type", "image_url", "detail"}, "image part")
            image = part.get("image_url")
            if isinstance(image, dict):
                _keys(image, {"url", "detail"}, "image_url")
                detail = image.get("detail", "auto")
                image = image.get("url")
            else:
                detail = part.get("detail", "auto")
            if detail != "auto":
                _fail("Image detail other than auto is not supported")
            result.append(_image(image))
        else:
            _fail("Unsupported content part type: %s" % kind)
    return result


def _function(tool, responses=False):
    if not isinstance(tool, dict) or tool.get("type") != "function":
        _fail("Only function tools are supported")
    _keys(tool, {"type", "name", "description", "parameters", "strict"} if responses else {"type", "function"}, "tool")
    fn = tool if responses else tool.get("function")
    _keys(fn, {"type", "name", "description", "parameters", "strict"} if responses else {"name", "description", "parameters", "strict"}, "function")
    if not isinstance(fn.get("name"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", fn["name"]):
        _fail("Invalid function name", "invalid_request")
    if fn.get("strict") not in (None, False):
        _fail("Strict function-schema enforcement is not supported")
    converted = {"name": fn["name"]}
    if "description" in fn:
        if not isinstance(fn["description"], str):
            _fail("Function description must be text", "invalid_request")
        converted["description"] = fn["description"]
    if "parameters" in fn:
        _object(fn["parameters"], "function parameters")
        converted["parametersJsonSchema"] = copy.deepcopy(fn["parameters"])
    return converted


def _tools(req, out, responses=False):
    tools = req.get("tools", [])
    if not isinstance(tools, list):
        _fail("tools must be an array", "invalid_request")
    declarations = [_function(tool, responses) for tool in tools]
    names = [item["name"] for item in declarations]
    if len(names) != len(set(names)):
        _fail("Function names must be unique", "invalid_request")
    if declarations:
        out["tools"] = [{"functionDeclarations": declarations}]
    choice = req.get("tool_choice", "auto")
    config = None
    if isinstance(choice, str):
        modes = {"auto": "AUTO", "none": "NONE", "required": "ANY"}
        if choice not in modes:
            _fail("Unsupported tool_choice")
        if not declarations and choice != "auto":
            _fail("tool_choice requires function tools", "invalid_request")
        if declarations:
            config = {"mode": modes[choice]}
    elif isinstance(choice, dict):
        _keys(choice, {"type", "name"} if responses else {"type", "function"}, "tool_choice")
        if choice.get("type") != "function":
            _fail("Only a named function tool_choice is supported")
        if responses:
            name = choice.get("name")
        else:
            _keys(choice.get("function"), {"name"}, "tool_choice.function")
            name = choice["function"].get("name")
        if name not in names:
            _fail("tool_choice names an undeclared function", "invalid_request")
        config = {"mode": "ANY", "allowedFunctionNames": [name]}
    else:
        _fail("Invalid tool_choice", "invalid_request")
    if config:
        out["toolConfig"] = {"functionCallingConfig": config}
    if req.get("parallel_tool_calls") is not None:
        _fail("parallel_tool_calls cannot be enforced by this provider")


def _native_part(part, output=False):
    _keys(part, {"text", "inlineData", "functionCall", "functionResponse", "thought", "thoughtSignature"}, "Gemini part")
    content_keys = set(part) & {"text", "inlineData", "functionCall", "functionResponse"}
    if len(content_keys) > 1 or not content_keys and "thoughtSignature" not in part:
        _fail("Invalid Gemini content part", "invalid_request", 502 if output else 400)
    if "text" in part and not isinstance(part["text"], str):
        _fail("Gemini text must be a string", "invalid_request", 502 if output else 400)
    if "thoughtSignature" in part and not isinstance(part["thoughtSignature"], str):
        _fail("Invalid thought signature", "invalid_request", 502 if output else 400)
    if "thought" in part and not isinstance(part["thought"], bool):
        _fail("Invalid thought flag", "invalid_request", 502 if output else 400)
    if "inlineData" in part:
        if output:
            _fail("Image/audio output is not supported by this API adapter", "unsupported_output", 502)
        blob = part["inlineData"]
        _keys(blob, {"mimeType", "data"}, "inlineData")
        _image("data:%s;base64,%s" % (blob.get("mimeType", ""), blob.get("data", "")))
    for field in ("functionCall", "functionResponse"):
        if field in part:
            if output and field == "functionResponse":
                _fail("Unexpected upstream functionResponse output", "invalid_upstream_response", 502)
            fn = part[field]
            _keys(fn, {"name", "args", "id"} if field == "functionCall" else {"name", "response", "id"}, field)
            if not isinstance(fn.get("name"), str) or not fn["name"]:
                _fail("Function name is required", "invalid_request", 502 if output else 400)
            if "id" in fn and (not isinstance(fn["id"], str) or not fn["id"]):
                _fail("Function ID must be nonempty text", "invalid_request", 502 if output else 400)
            _object(fn.get("args", {}) if field == "functionCall" else fn.get("response"), field)
    return copy.deepcopy(part)


def _append(out, role, parts):
    if not parts:
        return
    if out and out[-1]["role"] == role:
        out[-1]["parts"].extend(parts)
    else:
        out.append({"role": role, "parts": parts})


def _history(items, responses=False):
    if not isinstance(items, list) or not items:
        _fail("Input messages must be a nonempty array", "invalid_request")
    contents, system, calls = [], [], {}
    for item in items:
        _object(item, "input item")
        kind = item.get("type", "message") if responses else "message"
        if responses and kind in {"function_call", "function_call_output"}:
            allowed = {"type", "id", "call_id", "name", "arguments", "status", "extra_content"} if kind == "function_call" else {"type", "call_id", "output"}
            _keys(item, allowed, "function item")
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                _fail("Function item requires call_id", "invalid_request")
            if kind == "function_call":
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    _fail("Function item requires name", "invalid_request")
                if call_id in calls:
                    _fail("Duplicate function call ID", "invalid_request")
                calls[call_id] = name
                part = {"functionCall": {"id": call_id, "name": name, "args": _arguments(item.get("arguments"))}}
                extra = _google_extra(item.get("extra_content"))
                if "thought_signature" in extra:
                    part["thoughtSignature"] = extra["thought_signature"]
                if "native_parts" in extra:
                    _fail("native_parts is supported on message items only")
                _append(contents, "model", [part])
            else:
                _append(contents, "user", [_tool_result(call_id, item.get("output"), calls)])
            continue
        if kind != "message":
            _fail("Unsupported Responses input item: %s" % kind)
        allowed = {"type", "id", "status", "role", "content", "extra_content"} if responses else {"role", "content", "tool_calls", "tool_call_id", "extra_content"}
        _keys(item, allowed, "message")
        role = item.get("role")
        if role not in {"user", "assistant", "system", "developer", "tool"} or responses and role == "tool":
            _fail("Unsupported message role: %s" % role, "invalid_request")
        if role == "tool":
            if item.get("extra_content") or item.get("tool_calls"):
                _fail("Tool response extensions are not supported")
            _append(contents, "user", [_tool_result(item.get("tool_call_id"), item.get("content"), calls)])
            continue
        parts = _parts(item.get("content"), responses)
        if role in {"system", "developer"}:
            if item.get("tool_calls") or item.get("extra_content") or any(set(p) != {"text"} for p in parts):
                _fail("System/developer instructions must contain plain text only")
            system.extend(parts)
            continue
        if item.get("tool_calls"):
            if role != "assistant" or not isinstance(item["tool_calls"], list):
                _fail("tool_calls requires an assistant message and an array", "invalid_request")
            for call in item["tool_calls"]:
                _keys(call, {"id", "type", "function", "extra_content"}, "tool call")
                if call.get("type") != "function":
                    _fail("Only function calls are supported")
                _keys(call.get("function"), {"name", "arguments"}, "tool call function")
                call_id, name = call.get("id"), call["function"].get("name")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or call_id in calls:
                    _fail("Function calls require unique IDs and names", "invalid_request")
                calls[call_id] = name
                part = {"functionCall": {"id": call_id, "name": name, "args": _arguments(call["function"].get("arguments"))}}
                extra = _google_extra(call.get("extra_content"))
                if "native_parts" in extra:
                    _fail("native_parts is supported on messages only")
                if "thought_signature" in extra:
                    part["thoughtSignature"] = extra["thought_signature"]
                parts.append(part)
        extra = _google_extra(item.get("extra_content"))
        if "thought_signature" in extra:
            if len(parts) != 1:
                _fail("Message thought_signature requires exactly one part", "invalid_request")
            parts[0]["thoughtSignature"] = extra["thought_signature"]
        # Signature-only parts cannot be represented by ordinary OpenAI content.
        # This explicit extension carries them without inventing text or signatures.
        if "native_parts" in extra:
            if role != "assistant":
                _fail("native_parts is supported only on assistant history")
            if not isinstance(extra["native_parts"], list):
                _fail("native_parts must be an array", "invalid_request")
            raw_parts = [_native_part(raw) for raw in extra["native_parts"]]
            if any(p.get("thought") and p.get("text") or "inlineData" in p or "functionResponse" in p for p in raw_parts):
                _fail("native_parts cannot carry hidden thought text or additional input data")
            raw_has_content = any(p.get("text") or p.get("functionCall") for p in raw_parts)
            if raw_has_content:
                def semantics(values):
                    return ("".join(p.get("text", "") for p in values), [p["functionCall"] for p in values if "functionCall" in p])
                if semantics(raw_parts) != semantics(parts):
                    _fail("native_parts must match the message text and function calls", "invalid_request")
                parts = raw_parts
            else:
                if any("thoughtSignature" not in p for p in raw_parts):
                    _fail("Empty native_parts require thought signatures", "invalid_request")
                parts.extend(raw_parts)
        _append(contents, "model" if role == "assistant" else "user", parts)
    if not contents:
        _fail("At least one user/model message is required", "invalid_request")
    return contents, system


def _tool_result(call_id, content, calls):
    if not isinstance(call_id, str) or not call_id or call_id not in calls:
        _fail("Tool result references an unknown function call ID", "invalid_request")
    if not isinstance(content, str):
        _fail("Function output must be a string", "invalid_request")
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = content
    response = parsed if isinstance(parsed, dict) else {"output": parsed}
    return {"functionResponse": {"id": call_id, "name": calls[call_id], "response": response}}


def _generation(req, out, responses=False):
    config = {}
    mapping = {"temperature": "temperature", "top_p": "topP", "max_output_tokens" if responses else "max_completion_tokens": "maxOutputTokens"}
    if not responses and "max_tokens" in req:
        if "max_completion_tokens" in req:
            _fail("Specify only one token limit", "invalid_request")
        mapping["max_tokens"] = "maxOutputTokens"
    for key, target in mapping.items():
        if key in req:
            value = req[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _fail("%s must be numeric" % key, "invalid_request")
            if target == "maxOutputTokens" and (not isinstance(value, int) or value < 1):
                _fail("Token limit must be a positive integer", "invalid_request")
            if target == "temperature" and not 0 <= value <= 2 or target == "topP" and not 0 < value <= 1:
                _fail("Invalid %s" % key, "invalid_request")
            config[target] = value
    if "stop" in req:
        stop = [req["stop"]] if isinstance(req["stop"], str) else req["stop"]
        if not isinstance(stop, list) or not all(isinstance(s, str) and s for s in stop) or len(stop) > 5:
            _fail("stop must contain at most five nonempty strings", "invalid_request")
        config["stopSequences"] = stop
    if "response_format" in req:
        fmt = req["response_format"]
        _keys(fmt, {"type"}, "response_format")
        if fmt.get("type") == "json_object":
            config["responseMimeType"] = "application/json"
        elif fmt.get("type") != "text":
            _fail("Only text/json_object response_format is supported")
    if responses and "text" in req:
        _keys(req["text"], {"format"}, "text")
        fmt = req["text"].get("format", {"type": "text"})
        _keys(fmt, {"type"}, "text.format")
        if fmt.get("type") == "json_object":
            config["responseMimeType"] = "application/json"
        elif fmt.get("type") != "text":
            _fail("Only text/json_object output format is supported")
    if config:
        out["generationConfig"] = config


def to_native(req, api_kind="chat"):
    """Validate a supported request and return a Gemini generateContent body."""
    _object(req, "request")
    if "stream" in req and not isinstance(req["stream"], bool):
        _fail("stream must be boolean", "invalid_request")
    if api_kind == "gemini":
        _keys(req, {"model", "stream", "contents", "systemInstruction", "tools", "toolConfig", "safetySettings", "generationConfig", "cachedContent"}, "request")
        out = copy.deepcopy({k: v for k, v in req.items() if k not in {"model", "stream"}})
        if not isinstance(out.get("contents"), list) or not out["contents"]:
            _fail("contents must be a nonempty array", "invalid_request")
        for content in out["contents"] + ([out["systemInstruction"]] if "systemInstruction" in out else []):
            _keys(content, {"role", "parts"}, "Gemini content")
            if content.get("role", "user") not in {"user", "model"}:
                _fail("Invalid Gemini content role", "invalid_request")
            if not isinstance(content.get("parts"), list) or not content["parts"]:
                _fail("Gemini parts must be a nonempty array", "invalid_request")
            for part in content["parts"]:
                _native_part(part)
        gen = out.get("generationConfig", {})
        _keys(gen, {"temperature", "topP", "topK", "maxOutputTokens", "stopSequences", "responseMimeType", "responseSchema", "responseJsonSchema", "presencePenalty", "frequencyPenalty", "seed", "thinkingConfig", "candidateCount", "responseModalities"}, "generationConfig")
        if gen.get("candidateCount", 1) != 1:
            _fail("Only one candidate is supported")
        if gen.get("responseModalities", ["TEXT"]) != ["TEXT"]:
            _fail("Only text output is supported")
        tools = out.get("tools", [])
        if not isinstance(tools, list):
            _fail("tools must be an array", "invalid_request")
        for tool in tools:
            _keys(tool, {"functionDeclarations"}, "Gemini tool")
            if not isinstance(tool.get("functionDeclarations"), list):
                _fail("Only function declarations are supported")
            for fn in tool["functionDeclarations"]:
                _keys(fn, {"name", "description", "parameters", "parametersJsonSchema", "response", "responseJsonSchema"}, "function declaration")
        _json(out, "Gemini request")
        return out
    if api_kind not in {"chat", "responses"}:
        _fail("Unknown API format", "invalid_request")
    responses = api_kind == "responses"
    common = {"model", "stream", "tools", "tool_choice", "temperature", "top_p", "parallel_tool_calls", "store", "reasoning_effort"}
    specific = {"input", "instructions", "max_output_tokens", "text", "reasoning", "background", "previous_response_id", "truncation"} if responses else {"messages", "max_tokens", "max_completion_tokens", "stop", "n", "response_format", "stream_options"}
    _keys(req, common | specific, "request")
    if "store" in req and req["store"] is not False:
        _fail("Stored responses are not supported; set store=false")
    if "background" in req and req["background"] is not False:
        _fail("Background responses are not supported")
    if req.get("previous_response_id") is not None:
        _fail("previous_response_id is unsupported; send full input history")
    if req.get("truncation", "disabled") != "disabled":
        _fail("Automatic truncation is not supported")
    if "n" in req and (type(req["n"]) is not int or req["n"] != 1):
        _fail("Only n=1 is supported")
    if "stream_options" in req:
        _keys(req["stream_options"], {"include_usage"}, "stream_options")
        if "include_usage" in req["stream_options"] and not isinstance(req["stream_options"]["include_usage"], bool):
            _fail("include_usage must be boolean", "invalid_request")
        if req["stream_options"].get("include_usage") is not True:
            _fail("Only stream_options.include_usage=true is supported")
    if "reasoning" in req:
        _keys(req["reasoning"], {"effort"}, "reasoning")
    for effort in (req.get("reasoning_effort"), req.get("reasoning", {}).get("effort")):
        if effort is not None and (not isinstance(effort, str) or effort not in {"low", "medium", "high"}):
            _fail("Supported reasoning effort values are low, medium, high")
    items = req.get("input") if responses else req.get("messages")
    if responses and isinstance(items, str):
        items = [{"role": "user", "content": items}]
    contents, system = _history(items, responses)
    if responses and "instructions" in req:
        if not isinstance(req["instructions"], str):
            _fail("instructions must be text", "invalid_request")
        system.insert(0, {"text": req["instructions"]})
    out = {"contents": contents}
    if system:
        out["systemInstruction"] = {"parts": system}
    _tools(req, out, responses)
    _generation(req, out, responses)
    _json(out, "request")
    return out


_BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"}


def _candidate(native, allow_empty=False):
    _object(native, "upstream response")
    if native.get("error"):
        _fail("Upstream returned an error", "upstream_error", 502)
    feedback = native.get("promptFeedback", {})
    if feedback.get("blockReason") not in (None, "", "BLOCK_REASON_UNSPECIFIED"):
        return [], "SAFETY"
    candidates = native.get("candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 1:
        _fail("Upstream returned unsupported multiple candidates", "invalid_upstream_response", 502)
    if not candidates:
        if allow_empty:
            return [], None
        _fail("Upstream returned no candidate", "empty_upstream_response", 502)
    candidate = _object(candidates[0], "candidate")
    if candidate.get("index", 0) != 0:
        _fail("Only candidate index zero is supported", "invalid_upstream_response", 502)
    finish = candidate.get("finishReason")
    if finish not in (None, "", "STOP", "MAX_TOKENS") and finish not in _BLOCKED:
        _fail("Unsupported upstream finish reason: %s" % finish, "upstream_generation_failed", 502)
    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else None
    if not isinstance(parts, list):
        _fail("Upstream parts must be an array", "invalid_upstream_response", 502)
    validated = [_native_part(part, output=True) for part in parts]
    return validated, finish


def _visible(parts):
    return [part for part in parts if not part.get("thought")]


def _usage(native, responses=False):
    raw = native.get("usageMetadata")
    if not isinstance(raw, dict):
        return None
    result = {}
    mapping = {"promptTokenCount": "input_tokens" if responses else "prompt_tokens", "candidatesTokenCount": "output_tokens" if responses else "completion_tokens", "totalTokenCount": "total_tokens"}
    for source, target in mapping.items():
        if source in raw:
            if type(raw[source]) is not int or raw[source] < 0:
                _fail("Invalid upstream token count", "invalid_upstream_response", 502)
            result[target] = raw[source]
    for source, target, field in (("cachedContentTokenCount", "input_tokens_details" if responses else "prompt_tokens_details", "cached_tokens"), ("thoughtsTokenCount", "output_tokens_details" if responses else "completion_tokens_details", "reasoning_tokens")):
        if source in raw:
            if type(raw[source]) is not int or raw[source] < 0:
                _fail("Invalid upstream token count", "invalid_upstream_response", 502)
            result.setdefault(target, {})[field] = raw[source]
    # Gemini candidatesTokenCount excludes thought tokens; OpenAI output counts
    # include reasoning. Sum only the two actual reported counts, never estimate.
    if "candidatesTokenCount" in raw and "thoughtsTokenCount" in raw:
        result["output_tokens" if responses else "completion_tokens"] = raw["candidatesTokenCount"] + raw["thoughtsTokenCount"]
    return result or None


def _signature_parts(parts):
    return [copy.deepcopy(p) for p in parts if "thoughtSignature" in p and not p.get("functionCall") and (p.get("thought") or not p.get("text"))]


def _tool_call(part, index, prefix):
    fn = part["functionCall"]
    item = {"id": fn.get("id") or "%s_%d" % (prefix, index), "type": "function", "function": {"name": fn["name"], "arguments": _json(fn.get("args", {}), "function arguments")}}
    if "thoughtSignature" in part:
        item["extra_content"] = {"google": {"thought_signature": part["thoughtSignature"]}}
    return item


def _message(parts, prefix):
    visible = _visible(parts)
    text_parts = [p for p in visible if "text" in p]
    text = "".join(p["text"] for p in text_parts)
    calls = []
    for part in visible:
        if "functionCall" in part:
            calls.append(_tool_call(part, len(calls), prefix))
    message = {"role": "assistant", "content": text if text else None}
    if calls:
        message["tool_calls"] = calls
    if any("thoughtSignature" in p for p in parts):
        # Keep ChatCompletionMessage.content a string. A checked extension retains
        # native part boundaries, opaque tail signatures, and exact tool ordering.
        raw_parts, call_index = [], 0
        for part in parts:
            if part.get("thought"):
                if "thoughtSignature" in part:
                    raw_parts.append({k: v for k, v in part.items() if k in {"thoughtSignature", "thought"}})
                continue
            raw = copy.deepcopy(part)
            if "functionCall" in raw:
                raw["functionCall"]["id"] = calls[call_index]["id"]
                raw["functionCall"].setdefault("args", {})
                call_index += 1
            raw_parts.append(raw)
        message["extra_content"] = {"google": {"native_parts": raw_parts}}
    return message


def _response_base(alias, metadata, response_id, created):
    return {"id": response_id, "object": "response", "created_at": created, "model": alias, "status": "in_progress", "output": [], "error": None, "incomplete_details": None, "usage": None, "metadata": copy.deepcopy(metadata), "store": False}


def _response_items(parts, prefix):
    items, pending, call_index = [], [], 0

    def flush():
        if not pending:
            return
        content = []
        for part in pending:
            if "text" in part and part["text"] and not part.get("thought"):
                output = {"type": "output_text", "text": part["text"], "annotations": []}
                if "thoughtSignature" in part:
                    output["extra_content"] = {"google": {"thought_signature": part["thoughtSignature"]}}
                content.append(output)
        item = {"id": prefix + "_message_" + str(len(items)), "type": "message", "role": "assistant", "status": "completed", "content": content}
        empty = [{k: v for k, v in p.items() if k in {"thoughtSignature", "thought"}} for p in _signature_parts(pending)]
        if empty:
            raw = [({k: v for k, v in p.items() if k in {"thoughtSignature", "thought"}} if p.get("thought") else copy.deepcopy(p)) for p in pending if not p.get("thought") or "thoughtSignature" in p]
            item["extra_content"] = {"google": {"native_parts": raw}}
        if content or empty:
            items.append(item)
        pending.clear()

    for part in parts:
        if "functionCall" not in part or part.get("thought"):
            pending.append(part)
            continue
        flush()
        call = _tool_call(part, call_index, prefix)
        call_index += 1
        item = {"id": prefix + "_" + call["id"], "type": "function_call", "status": "completed", "call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]}
        if "extra_content" in call:
            item["extra_content"] = call["extra_content"]
        items.append(item)
    flush()
    return items


def format_result(native, alias, metadata, api_kind="chat"):
    """Convert a complete response without inferring usage or hidden reasoning."""
    parts, finish = _candidate(native)
    visible = _visible(parts)
    if not any(p.get("text") or p.get("functionCall") for p in visible) and finish not in _BLOCKED and finish != "MAX_TOKENS":
        _fail("Upstream returned no answer or tool call", "empty_upstream_response", 502)
    if api_kind == "gemini":
        out = copy.deepcopy(native)
        out["providerMetadata"] = copy.deepcopy(metadata)
        return out
    prefix = "call_" + uuid.uuid4().hex
    created = int(time.time())
    if api_kind == "chat":
        message = _message(parts, prefix)
        reason = "content_filter" if finish in _BLOCKED else "length" if finish == "MAX_TOKENS" else "tool_calls" if message.get("tool_calls") else "stop"
        if reason == "content_filter":
            message["refusal"] = "The upstream provider blocked this response."
        return {"id": "chatcmpl_" + uuid.uuid4().hex, "object": "chat.completion", "created": created, "model": alias, "choices": [{"index": 0, "message": message, "finish_reason": reason}], "usage": _usage(native), "provider_metadata": copy.deepcopy(metadata)}
    if api_kind != "responses":
        _fail("Unknown API format", "invalid_request")
    response_id = "resp_" + str(metadata.get("request_id") or uuid.uuid4().hex)
    out = _response_base(alias, metadata, response_id, created)
    out["output"] = _response_items(parts, prefix)
    out["status"] = "incomplete" if finish == "MAX_TOKENS" or finish in _BLOCKED else "completed"
    if finish == "MAX_TOKENS" or finish in _BLOCKED:
        out["incomplete_details"] = {"reason": "max_output_tokens" if finish == "MAX_TOKENS" else "content_filter"}
    out["usage"] = _usage(native, True)
    return out


def stream_events(native_chunks, alias, metadata, api_kind="chat"):
    """Yield SSE (event name, payload) pairs while consuming native chunks lazily.

    Raises ProviderError on failure, including missing terminal finishReason. The
    handler owns the sole error/response.failed event, so no false success follows.
    Gemini function-call chunks must contain complete argument objects. Repeated
    complete calls are deduplicated by upstream ID, or name and position otherwise.
    """
    if api_kind not in {"chat", "responses", "gemini"}:
        _fail("Unknown API format", "invalid_request")
    created, token = int(time.time()), uuid.uuid4().hex
    response_id = "resp_" + str(metadata.get("request_id") or token)
    chat_id, prefix = "chatcmpl_" + token, "call_" + token
    response = _response_base(alias, metadata, response_id, created)
    sequence, parts_all, calls, usage, finish, seen_answer = 0, [], {}, {}, None, False
    items, message_index, text_index, message_parts = [], None, None, {}

    def event(name, **fields):
        nonlocal sequence
        sequence += 1
        return name, dict(type=name, sequence_number=sequence, **fields)

    def chat(delta, reason=None, counts=None):
        out = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": alias, "choices": [{"index": 0, "delta": delta, "finish_reason": reason}], "provider_metadata": copy.deepcopy(metadata)}
        if counts is not None:
            out["usage"] = counts
        return None, out

    def ensure_message():
        nonlocal message_index, text_index
        if message_index is not None:
            return items[message_index], None
        message_index = len(items)
        text_index = None
        item = {"id": "msg_" + token + "_" + str(message_index), "type": "message", "status": "in_progress", "role": "assistant", "content": []}
        items.append(item)
        message_parts[message_index] = []
        return item, event("response.output_item.added", output_index=message_index, item=copy.deepcopy(item))

    if api_kind == "responses":
        yield event("response.created", response=copy.deepcopy(response))
        yield event("response.in_progress", response=copy.deepcopy(response))
    elif api_kind == "chat":
        yield chat({"role": "assistant", "content": ""})
    for native in native_chunks:
        parts, chunk_finish = _candidate(native, allow_empty=True)
        if finish and parts:
            _fail("Content arrived after upstream finishReason", "invalid_upstream_response", 502)
        if chunk_finish:
            finish = chunk_finish
        if isinstance(native.get("usageMetadata"), dict):
            usage.update(native["usageMetadata"])
        if api_kind == "gemini":
            out = copy.deepcopy(native)
            out["providerMetadata"] = copy.deepcopy(metadata)
            yield None, out
            seen_answer = seen_answer or any(p.get("text") or p.get("functionCall") for p in _visible(parts))
            continue
        call_position = 0
        for part in parts:
            if part.get("thought"):
                if "thoughtSignature" in part:
                    signed = {k: v for k, v in part.items() if k in {"thought", "thoughtSignature"}}
                    parts_all.append(signed)
                    if api_kind == "chat":
                        yield chat({"extra_content": {"google": {"native_parts": [copy.deepcopy(signed)]}}})
                    else:
                        item, added = ensure_message()
                        message_parts[message_index].append(copy.deepcopy(signed))
                        if added:
                            yield added
                continue
            if "functionResponse" in part:
                _fail("Unexpected upstream functionResponse output", "invalid_upstream_response", 502)
            if "functionCall" in part:
                fn = part["functionCall"]
                key = ("id", fn["id"]) if fn.get("id") else ("position", call_position, fn["name"])
                call_position += 1
                encoded = _json(fn.get("args", {}), "function arguments")
                if key in calls:
                    previous = calls[key]
                    if previous["arguments"] != encoded or previous["name"] != fn["name"]:
                        _fail("Partial/revised function arguments are unsupported", "invalid_upstream_response", 502)
                    if "thoughtSignature" in part and previous.get("signature") != part["thoughtSignature"]:
                        previous["signature"] = part["thoughtSignature"]
                        previous["part"]["thoughtSignature"] = part["thoughtSignature"]
                        extra = {"google": {"thought_signature": part["thoughtSignature"]}}
                        if api_kind == "chat":
                            yield chat({"tool_calls": [{"index": previous["tool_index"], "extra_content": extra}]})
                        else:
                            items[previous["output_index"]]["extra_content"] = extra
                    continue
                index = len(calls)
                call = _tool_call(part, index, prefix)
                stored_part = copy.deepcopy(part)
                parts_all.append(stored_part)
                state = {"arguments": encoded, "name": fn["name"], "signature": part.get("thoughtSignature"), "part": stored_part, "tool_index": index}
                calls[key] = state
                seen_answer = True
                if api_kind == "chat":
                    delta = dict(call, index=index)
                    yield chat({"tool_calls": [delta]})
                else:
                    output_index = len(items)
                    item = {"id": "fc_" + token + "_" + str(index), "type": "function_call", "status": "in_progress", "call_id": call["id"], "name": fn["name"], "arguments": ""}
                    if "extra_content" in call:
                        item["extra_content"] = call["extra_content"]
                    items.append(item)
                    state["output_index"] = output_index
                    yield event("response.output_item.added", output_index=output_index, item=copy.deepcopy(item))
                    yield event("response.function_call_arguments.delta", item_id=item["id"], output_index=output_index, delta=encoded)
                    item["arguments"] = encoded
                    message_index, text_index = None, None
                continue
            parts_all.append(copy.deepcopy(part))
            if "text" in part and part["text"]:
                seen_answer = True
                if api_kind == "chat":
                    delta = {"content": part["text"]}
                    if "thoughtSignature" in part:
                        delta["extra_content"] = {"google": {"thought_signature": part["thoughtSignature"]}}
                    yield chat(delta)
                else:
                    item, added = ensure_message()
                    message_parts[message_index].append(copy.deepcopy(part))
                    if added:
                        yield added
                    if text_index is None:
                        text_index = len(item["content"])
                        item["content"].append({"type": "output_text", "text": "", "annotations": []})
                        yield event("response.content_part.added", item_id=item["id"], output_index=message_index, content_index=text_index, part=copy.deepcopy(item["content"][text_index]))
                    yield event("response.output_text.delta", item_id=item["id"], output_index=message_index, content_index=text_index, delta=part["text"], logprobs=[])
                    item["content"][text_index]["text"] += part["text"]
                    if "thoughtSignature" in part:
                        item["content"][text_index]["extra_content"] = {"google": {"thought_signature": part["thoughtSignature"]}}
                        text_index = None
            elif "thoughtSignature" in part and api_kind == "chat":
                yield chat({"extra_content": {"google": {"native_parts": [copy.deepcopy(part)]}}})
            elif "thoughtSignature" in part and api_kind == "responses":
                item, added = ensure_message()
                message_parts[message_index].append(copy.deepcopy(part))
                if added:
                    yield added
    if not finish:
        _fail("Upstream stream ended without finishReason", "truncated_upstream_stream", 502)
    if not seen_answer and finish not in _BLOCKED and finish != "MAX_TOKENS":
        _fail("Upstream stream produced no answer or tool call", "empty_upstream_response", 502)
    if api_kind == "gemini":
        return
    if api_kind == "chat":
        reason = "content_filter" if finish in _BLOCKED else "length" if finish == "MAX_TOKENS" else "tool_calls" if calls else "stop"
        final_delta = {}
        if any("thoughtSignature" in p for p in parts_all):
            final_delta["extra_content"] = _message(parts_all, prefix)["extra_content"]
        yield chat(final_delta, reason, _usage({"usageMetadata": usage}))
        yield None, "[DONE]"
        return
    for output_index, item in enumerate(items):
        if item["type"] == "function_call":
            yield event("response.function_call_arguments.done", item_id=item["id"], output_index=output_index, arguments=item["arguments"])
        else:
            for content_index, part in enumerate(item["content"]):
                yield event("response.output_text.done", item_id=item["id"], output_index=output_index, content_index=content_index, text=part["text"], logprobs=[])
                yield event("response.content_part.done", item_id=item["id"], output_index=output_index, content_index=content_index, part=copy.deepcopy(part))
            raw_parts = message_parts[output_index]
            if any("thoughtSignature" in p for p in raw_parts):
                item["extra_content"] = {"google": {"native_parts": copy.deepcopy(raw_parts)}}
        item["status"] = "incomplete" if finish in _BLOCKED or finish == "MAX_TOKENS" else "completed"
        yield event("response.output_item.done", output_index=output_index, item=copy.deepcopy(item))
    response["output"] = items
    response["usage"] = _usage({"usageMetadata": usage}, True)
    response["status"] = "incomplete" if finish in _BLOCKED or finish == "MAX_TOKENS" else "completed"
    if response["status"] == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens" if finish == "MAX_TOKENS" else "content_filter"}
    yield event("response.incomplete" if response["status"] == "incomplete" else "response.completed", response=copy.deepcopy(response))
