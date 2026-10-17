"""Opt-in provider profiles, separate from the legacy Web provider.

``build-`` and ``ais-`` default to Google's documented public API. An explicit
transport selection can instead use the operator-owned AI Studio HTTP backend.
Capability claims are deliberately limited to reviewed model IDs. Discovery
confirms models.list visibility, not generation quota or permission.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx


TRANSPORT = "google-gemini-public-api"
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_MODEL_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-.][a-z0-9]+)*$")
_EFFORT_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}
_BUDGET_MODELS = frozenset(("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"))
_LEVEL_MODELS = {
    "gemini-3-pro-preview": ("low", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
    "gemini-3-flash-preview": ("low", "medium", "high"),
}
_IMAGE_MIMES = frozenset(("image/png", "image/jpeg", "image/webp", "image/gif", "image/heic", "image/heif"))
_DISCOVERY_CACHE = {}  # type: Dict[Tuple[str, str, Tuple[str, ...]], Tuple[float, List[dict]]]
_CACHE_LOCK = threading.Lock()


class ProviderError(Exception):
    """Safe, client-visible error. Never include raw upstream error text."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.status_code = status
        self.code = code
        self.message = message
        super().__init__(message)


def _number_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise ProviderError(503, "provider_configuration_error", "Invalid provider limit configuration.") from None


@dataclass(frozen=True)
class _Limits:
    connect: float
    read: float
    total: float
    request_bytes: int
    response_bytes: int
    line_bytes: int


def _limits() -> _Limits:
    return _Limits(
        _number_env("PROVIDER_CONNECT_TIMEOUT_SEC", 10, 0.05, 600),
        _number_env("PROVIDER_READ_TIMEOUT_SEC", 60, 0.05, 600),
        _number_env("PROVIDER_TOTAL_TIMEOUT_SEC", 180, 0.05, 3600),
        int(_number_env("PROVIDER_MAX_REQUEST_BYTES", 20 * 1024 * 1024, 1024, 100 * 1024 * 1024)),
        int(_number_env("PROVIDER_MAX_RESPONSE_BYTES", 20 * 1024 * 1024, 1024, 100 * 1024 * 1024)),
        int(_number_env("PROVIDER_MAX_SSE_LINE_BYTES", 1024 * 1024, 256, 10 * 1024 * 1024)),
    )


def _new_client(timeout: httpx.Timeout) -> httpx.Client:
    # No configurable destination, ambient proxies, cookies, redirects, or retry.
    return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, verify=True)


class _Deadline:
    """Bound operations and close the connection when the wall deadline fires."""

    def __init__(self, client: httpx.Client, seconds: float):
        self.client = client
        self.end = time.monotonic() + seconds
        self.expired = threading.Event()
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True

    def _expire(self):
        self.expired.set()
        try:
            self.client.close()
        except Exception:
            pass

    def __enter__(self):
        self.timer.start()
        return self

    def check(self):
        if self.expired.is_set() or time.monotonic() >= self.end:
            raise ProviderError(504, "upstream_timeout", "The public Gemini API request timed out.")

    def __exit__(self, *_args):
        self.timer.cancel()


def _timeout(limits: _Limits, total: Optional[float] = None) -> httpx.Timeout:
    remaining = limits.total if total is None else max(0.05, min(total, limits.total))
    return httpx.Timeout(min(limits.read, remaining), connect=min(limits.connect, remaining),
                         write=min(limits.read, remaining), pool=min(limits.connect, remaining))


def _configured_models(source: str) -> Tuple[str, ...]:
    configured = os.environ.get(source.upper() + "_MODELS", "gemini-2.5-flash")
    models = tuple(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))
    if len(models) > 100 or any(not _MODEL_RE.fullmatch(model) or len(model) > 160 for model in models):
        raise ProviderError(503, "provider_configuration_error", "Invalid provider model allowlist.")
    return models


def _efforts(model: str) -> Tuple[str, ...]:
    if model in _BUDGET_MODELS:
        return tuple(_EFFORT_BUDGETS)
    return _LEVEL_MODELS.get(model, ())


def _capabilities(model: str) -> Dict[str, Any]:
    reviewed = model in _BUDGET_MODELS or model in _LEVEL_MODELS
    return {"text": True, "streaming": True, "image_input": reviewed,
            "image_output": False, "tools": reviewed, "thinking_efforts": list(_efforts(model))}


def _validate_effort(model: str, effort: Optional[str]):
    if effort is not None and (not isinstance(effort, str) or effort not in _EFFORT_BUDGETS):
        raise ProviderError(400, "unsupported_reasoning_effort", "reasoning_effort must be low, medium, or high.")
    if effort is not None and effort not in _efforts(model):
        raise ProviderError(400, "unsupported_reasoning_effort", "This model does not support the requested thinking effort.")


def _status_error(status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError(502, "upstream_authentication_failed", "The public Gemini API rejected the configured credentials or permissions.")
    if status == 404:
        return ProviderError(404, "upstream_model_not_found", "The requested model is unavailable from the public Gemini API.")
    if status == 429:
        return ProviderError(429, "upstream_rate_limited", "The public Gemini API quota or rate limit was reached.")
    if status in (408, 504):
        return ProviderError(504, "upstream_timeout", "The public Gemini API request timed out.")
    if 300 <= status < 400:
        return ProviderError(502, "upstream_redirect_rejected", "The public Gemini API returned an unexpected redirect.")
    if 400 <= status < 500:
        return ProviderError(400, "upstream_invalid_request", "The public Gemini API rejected the request parameters or capability.")
    return ProviderError(502, "upstream_error", "The public Gemini API request failed.")


def _checked_object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ProviderError(502, "upstream_invalid_response", "The public Gemini API returned an invalid response.")
    if "error" in value:
        error = value["error"]
        status = error.get("code", 502) if isinstance(error, dict) else 502
        raise _status_error(status if type(status) is int else 502)
    return value


def _decode_json(payload: bytes) -> dict:
    try:
        return _checked_object(json.loads(payload))
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError(502, "upstream_invalid_response", "The public Gemini API returned invalid JSON.") from None


def _read_bytes(response: httpx.Response, deadline: _Deadline, limit: int) -> bytes:
    result = bytearray()
    for chunk in response.iter_bytes(chunk_size=8192):
        deadline.check()
        if len(result) + len(chunk) > limit:
            raise ProviderError(502, "upstream_response_too_large", "The public Gemini API response exceeded the configured limit.")
        result.extend(chunk)
    deadline.check()
    return bytes(result)


def _json_response(response: httpx.Response, deadline: _Deadline, limit: int) -> dict:
    if response.status_code != 200:
        # Do not read, log, or relay upstream error bodies; they may echo secrets.
        raise _status_error(response.status_code)
    return _decode_json(_read_bytes(response, deadline, limit))


def _safe_transport_error(error: Exception, deadline: _Deadline) -> ProviderError:
    if deadline.expired.is_set() or isinstance(error, httpx.TimeoutException):
        return ProviderError(504, "upstream_timeout", "The public Gemini API request timed out.")
    return ProviderError(502, "upstream_connection_error", "The public Gemini API connection failed.")


def _parts(body: dict) -> Iterator[dict]:
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise ProviderError(400, "invalid_request", "contents must be a non-empty array.")
    all_contents = list(contents)
    if "systemInstruction" in body:
        all_contents.append(body["systemInstruction"])
    for content in all_contents:
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            raise ProviderError(400, "invalid_request", "Each content must contain a parts array.")
        for part in content["parts"]:
            if not isinstance(part, dict):
                raise ProviderError(400, "invalid_request", "Each content part must be an object.")
            yield part


@dataclass
class Route:
    source: str
    model: str
    effort: Optional[str] = None
    alias: str = ""
    _api_key: str = field(default="", repr=False, compare=False)
    _bounds: _Limits = field(default_factory=_limits, repr=False, compare=False)
    _native_thinking: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.source not in ("build", "ais") or not isinstance(self.model, str) or not _MODEL_RE.fullmatch(self.model):
            raise ProviderError(400, "invalid_model", "Invalid public Gemini provider model.")
        _validate_effort(self.model, self.effort)
        if not self._api_key:
            self._api_key = os.environ.get(self.source.upper() + "_API_KEY", "").strip()
        if not self._api_key:
            raise ProviderError(503, "provider_not_configured", "The requested provider profile has no API key configured.")
        if not self.alias:
            self.alias = self.source + "-" + self.model + ("-" + self.effort if self.effort else "")

    @property
    def capabilities(self) -> Dict[str, Any]:
        return _capabilities(self.model)

    @property
    def meta(self) -> Dict[str, Any]:
        thinking = dict(self._native_thinking)
        if self.effort:
            if self.model in _BUDGET_MODELS:
                thinking = {"thinkingBudget": _EFFORT_BUDGETS[self.effort]}
            else:
                thinking = {"thinkingLevel": self.effort.upper()}
        result = {"source": self.source, "transport": TRANSPORT, "upstream_model": self.model,
                  "requested_effort": self.effort, "profile_kind": "logical-public-api-profile",
                  "webpage_executor": False}
        if "thinkingBudget" in thinking:
            result["effective_thinking_budget"] = thinking["thinkingBudget"]
        if "thinkingLevel" in thinking:
            result["effective_thinking_level"] = thinking["thinkingLevel"].lower()
        return result

    def prepare(self, native_body: dict) -> dict:
        if not isinstance(native_body, dict):
            raise ProviderError(400, "invalid_request", "The request body must be an object.")
        self._encoded(native_body)
        try:
            body = copy.deepcopy(native_body)
        except (TypeError, ValueError, RecursionError):
            raise ProviderError(400, "invalid_request", "The request body must contain valid JSON.") from None
        if "model" in body:
            raise ProviderError(400, "model_conflict", "Specify the model in the route, not in the native request body.")
        generation = body.setdefault("generationConfig", {})
        if not isinstance(generation, dict):
            raise ProviderError(400, "invalid_request", "generationConfig must be an object.")
        if "generation_config" in body or "thinking_config" in generation:
            raise ProviderError(400, "invalid_request", "Native Gemini requests must use camelCase configuration fields.")
        thinking = generation.get("thinkingConfig", {})
        if not isinstance(thinking, dict):
            raise ProviderError(400, "invalid_request", "thinkingConfig must be an object.")
        if any(key not in ("thinkingBudget", "thinkingLevel", "includeThoughts") for key in thinking):
            raise ProviderError(400, "invalid_thinking_config", "Unsupported thinking configuration field.")
        if "includeThoughts" in thinking and type(thinking["includeThoughts"]) is not bool:
            raise ProviderError(400, "invalid_thinking_config", "includeThoughts must be a boolean.")
        if self.effort and any(key in thinking for key in ("thinkingBudget", "thinkingLevel")):
            raise ProviderError(400, "thinking_conflict", "A model effort or reasoning_effort cannot be combined with a native thinking budget or level.")
        if "thinkingBudget" in thinking and "thinkingLevel" in thinking:
            raise ProviderError(400, "thinking_conflict", "Specify one thinking budget or level.")
        if thinking and not _efforts(self.model):
            raise ProviderError(400, "unsupported_reasoning_effort", "Thinking configuration has not been verified for this model.")
        if "thinkingBudget" in thinking:
            budget = thinking["thinkingBudget"]
            minimum, maximum = (128, 32768) if self.model == "gemini-2.5-pro" else (0, 24576)
            if self.model == "gemini-2.5-flash-lite":
                minimum = 512
            if self.model not in _BUDGET_MODELS or type(budget) is not int or not (budget == -1 or minimum <= budget <= maximum or (budget == 0 and self.model == "gemini-2.5-flash-lite")):
                raise ProviderError(400, "unsupported_thinking_budget", "This model does not support the requested native thinking budget.")
        if "thinkingLevel" in thinking:
            level = thinking["thinkingLevel"]
            if not isinstance(level, str) or self.model not in _LEVEL_MODELS or level.lower() not in _efforts(self.model):
                raise ProviderError(400, "unsupported_reasoning_effort", "This model does not support the requested native thinking level.")
            thinking["thinkingLevel"] = level.upper()
        self._native_thinking = dict(thinking)
        if self.effort:
            thinking = dict(thinking)
            thinking["thinkingBudget" if self.model in _BUDGET_MODELS else "thinkingLevel"] = (
                _EFFORT_BUDGETS[self.effort] if self.model in _BUDGET_MODELS else self.effort.upper())
            generation["thinkingConfig"] = thinking
        modalities = generation.get("responseModalities", ["TEXT"])
        if not isinstance(modalities, list) or any(value != "TEXT" for value in modalities):
            raise ProviderError(400, "unsupported_output_modality", "This provider profile supports text output only.")
        if any(key in generation for key in ("imageConfig", "speechConfig")):
            raise ProviderError(400, "unsupported_output_modality", "This provider profile supports text output only.")
        for part in _parts(body):
            if "fileData" in part or "file_data" in part:
                raise ProviderError(400, "unsupported_file_input", "Use inline image data; remote file inputs are not enabled.")
            if "inlineData" in part or "inline_data" in part:
                if not self.capabilities["image_input"]:
                    raise ProviderError(400, "unsupported_image_input", "Image input has not been verified for this model.")
                image = part.get("inlineData")
                if not isinstance(image, dict) or image.get("mimeType") not in _IMAGE_MIMES or not isinstance(image.get("data"), str):
                    raise ProviderError(400, "unsupported_image_input", "Only supported inline image data is accepted.")
                try:
                    if not base64.b64decode(image["data"], validate=True):
                        raise ValueError
                except (ValueError, TypeError):
                    raise ProviderError(400, "invalid_image_input", "Inline image data must be non-empty base64.") from None
            if any(key in part for key in ("functionCall", "functionResponse")) and not self.capabilities["tools"]:
                raise ProviderError(400, "unsupported_tools", "Function calling has not been verified for this model.")
        tools = body.get("tools", [])
        if not isinstance(tools, list):
            raise ProviderError(400, "invalid_request", "tools must be an array.")
        if tools and not self.capabilities["tools"]:
            raise ProviderError(400, "unsupported_tools", "Function calling has not been verified for this model.")
        if any(not isinstance(tool, dict) or set(tool) != {"functionDeclarations"} or not isinstance(tool["functionDeclarations"], list) for tool in tools):
            raise ProviderError(400, "unsupported_tools", "Only native functionDeclarations tools are enabled.")
        self._encoded(body)  # Reject oversized/non-JSON bodies before opening a connection.
        return body

    def _encoded(self, body: dict) -> bytes:
        try:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError(400, "invalid_request", "The request body must contain valid JSON.") from None
        if len(payload) > self._bounds.request_bytes:
            raise ProviderError(413, "request_too_large", "The request exceeded the configured provider size limit.")
        return payload

    def _headers(self) -> dict:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json", "Accept": "application/json"}

    def generate(self, native_body: dict) -> dict:
        payload = self._encoded(self.prepare(native_body))
        with _new_client(_timeout(self._bounds)) as client:
            with _Deadline(client, self._bounds.total) as deadline:
                try:
                    with client.stream("POST", _BASE_URL + "/models/" + self.model + ":generateContent",
                                       headers=self._headers(), content=payload) as response:
                        return _json_response(response, deadline, self._bounds.response_bytes)
                except (httpx.HTTPError, OSError, RuntimeError) as error:
                    raise _safe_transport_error(error, deadline) from None

    def stream(self, native_body: dict) -> Iterator[dict]:
        payload = self._encoded(self.prepare(native_body))
        with _new_client(_timeout(self._bounds)) as client:
            with _Deadline(client, self._bounds.total) as deadline:
                try:
                    with client.stream("POST", _BASE_URL + "/models/" + self.model + ":streamGenerateContent",
                                       params={"alt": "sse"}, headers=dict(self._headers(), Accept="text/event-stream"),
                                       content=payload) as response:
                        if response.status_code != 200:
                            raise _status_error(response.status_code)
                        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                            # A JSON error is occasionally returned with HTTP 200.
                            value = _json_response(response, deadline, self._bounds.response_bytes)
                            _checked_object(value)
                            raise ProviderError(502, "upstream_invalid_stream", "The public Gemini API did not return an SSE stream.")
                        terminal = False
                        for event in _sse_events(response, deadline, self._bounds):
                            if event == b"[DONE]":
                                break
                            value = _decode_json(event)
                            terminal = terminal or _terminal_chunk(value)
                            yield value
                        deadline.check()
                        if not terminal:
                            raise ProviderError(502, "upstream_stream_incomplete", "The public Gemini API stream ended without a final result.")
                except (httpx.HTTPError, OSError, RuntimeError) as error:
                    raise _safe_transport_error(error, deadline) from None


def _terminal_chunk(value: dict) -> bool:
    feedback = value.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        return True
    candidates = value.get("candidates", [])
    return isinstance(candidates, list) and any(isinstance(candidate, dict) and candidate.get("finishReason") for candidate in candidates)


def _sse_events(response: httpx.Response, deadline: _Deadline, limits: _Limits) -> Iterator[bytes]:
    buffer = bytearray()
    data = []  # type: List[bytes]
    event_size = 0
    received = 0
    for chunk in response.iter_bytes():
        deadline.check()
        received += len(chunk)
        if received > limits.response_bytes:
            raise ProviderError(502, "upstream_response_too_large", "The public Gemini API stream exceeded the configured size limit.")
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, remaining = buffer.partition(b"\n")
            buffer = bytearray(remaining)
            line = line.rstrip(b"\r")
            if len(line) > limits.line_bytes:
                raise ProviderError(502, "upstream_sse_line_too_large", "A public Gemini API stream line exceeded the configured limit.")
            if not line:
                if data:
                    yield b"\n".join(data)
                    data, event_size = [], 0
            elif line.startswith(b"data:"):
                value = bytes(line[5:])
                if value.startswith(b" "):
                    value = value[1:]
                event_size += len(value)
                if event_size > limits.line_bytes:
                    raise ProviderError(502, "upstream_sse_event_too_large", "A public Gemini API event exceeded the configured limit.")
                data.append(value)
        if len(buffer) > limits.line_bytes:
            raise ProviderError(502, "upstream_sse_line_too_large", "A public Gemini API stream line exceeded the configured limit.")
    deadline.check()
    if buffer:
        if buffer.startswith(b"data:"):
            data.append(bytes(buffer[5:]).lstrip(b" ").rstrip(b"\r"))
        elif not buffer.startswith(b":"):
            raise ProviderError(502, "upstream_invalid_stream", "The public Gemini API stream ended with an invalid event.")
    if data:
        if sum(map(len, data)) > limits.line_bytes:
            raise ProviderError(502, "upstream_sse_event_too_large", "A public Gemini API event exceeded the configured limit.")
        yield b"\n".join(data)


def resolve_route(alias: Any, effort: Optional[str] = None) -> Optional[Route]:
    """Resolve reserved public profile aliases, returning None for legacy names."""
    if not isinstance(alias, str):
        return None
    source = next((name for name in ("build", "ais") if alias.startswith(name + "-")), None)
    if source is None:
        return None
    from . import aistudio_http
    if aistudio_http.transport_mode(source) == aistudio_http.TRANSPORT:
        return aistudio_http.resolve_route(source, alias, effort)
    requested = alias[len(source) + 1:]
    models = _configured_models(source)
    suffix_effort = None
    model = requested
    if requested not in models and "-" in requested:
        base, suffix = requested.rsplit("-", 1)
        if suffix in _EFFORT_BUDGETS:
            model, suffix_effort = base, suffix
    if not _MODEL_RE.fullmatch(model) or model not in models:
        raise ProviderError(404, "unknown_provider_model", "The requested model is not enabled for this provider profile.")
    if suffix_effort and effort is not None and suffix_effort != effort:
        raise ProviderError(400, "thinking_conflict", "The model suffix and reasoning_effort specify different thinking efforts.")
    actual_effort = suffix_effort if suffix_effort is not None else effort
    _validate_effort(model, actual_effort)
    return Route(source, model, actual_effort, alias)


def _discover(source: str, api_key: str, allowlist: Tuple[str, ...]) -> List[dict]:
    cache_key = (source, hashlib.sha256(api_key.encode("utf-8")).hexdigest(), allowlist)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _DISCOVERY_CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return copy.deepcopy(cached[1])
    bounds = _limits()
    ttl = _number_env("PROVIDER_MODELS_CACHE_SEC", 30, 0, 600)
    found = []
    try:
        with _new_client(_timeout(bounds)) as client:
            with _Deadline(client, min(bounds.total, 15)) as deadline:
                tokens = set()
                token = None
                for _page in range(10):
                    deadline.check()
                    params = {"pageSize": "1000"}
                    if token:
                        params["pageToken"] = token
                    with client.stream("GET", _BASE_URL + "/models", params=params,
                                       headers={"x-goog-api-key": api_key, "Accept": "application/json"},
                                       timeout=_timeout(bounds, deadline.end - time.monotonic())) as response:
                        data = _json_response(response, deadline, min(bounds.response_bytes, 4 * 1024 * 1024))
                    entries = data.get("models", [])
                    if not isinstance(entries, list):
                        raise ProviderError(502, "upstream_invalid_response", "Invalid public Gemini API model catalogue.")
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        name = entry.get("name", "")
                        model = name[len("models/"):] if isinstance(name, str) and name.startswith("models/") else ""
                        methods = entry.get("supportedGenerationMethods", [])
                        if model in allowlist and isinstance(methods, list) and "generateContent" in methods:
                            found.append({"model": model, "thinking": entry.get("thinking")})
                    token = data.get("nextPageToken")
                    if not token:
                        break
                    if not isinstance(token, str) or len(token) > 4096 or token in tokens:
                        raise ProviderError(502, "upstream_invalid_response", "Invalid public Gemini API model pagination.")
                    tokens.add(token)
                else:
                    raise ProviderError(502, "upstream_invalid_response", "The public Gemini API model catalogue exceeded its limit.")
    except (ProviderError, httpx.HTTPError, OSError, RuntimeError):
        found = []
    with _CACHE_LOCK:
        # Bound cache growth when credentials/configuration rotate repeatedly.
        if len(_DISCOVERY_CACHE) >= 32:
            _DISCOVERY_CACHE.clear()
        _DISCOVERY_CACHE[cache_key] = (time.monotonic() + ttl, copy.deepcopy(found))
    return found


def list_models() -> List[dict]:
    """Advertise only configured allowlist models confirmed by public discovery."""
    result = []
    for source in ("build", "ais"):
        from . import aistudio_http
        try:
            if aistudio_http.transport_mode(source) == aistudio_http.TRANSPORT:
                result.extend(aistudio_http.list_models(source))
                continue
        except ProviderError:
            continue
        key = os.environ.get(source.upper() + "_API_KEY", "").strip()
        if not key:
            continue
        try:
            allowed = _configured_models(source)
            discovered = _discover(source, key, allowed) if allowed else []
        except ProviderError:
            continue
        seen = set()
        for entry in discovered:
            model = entry["model"]
            if model in seen:
                continue
            seen.add(model)
            efforts = _efforts(model) if entry["thinking"] is not False else ()
            for effort in (None,) + efforts:
                route = Route(source, model, effort, _api_key=key)
                caps = route.capabilities
                caps["thinking_efforts"] = list(efforts)
                result.append({"id": route.alias, "object": "model", "owned_by": "google", "created": 0,
                               "source": source, "transport": TRANSPORT, "upstream_model": model,
                               "capabilities": caps, "metadata": route.meta, "discovered": True})
    return result
