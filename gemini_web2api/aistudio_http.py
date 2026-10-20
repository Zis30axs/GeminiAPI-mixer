"""Bounded adapter for an operator-owned AI Studio HTTP backend.

The backend owns account sessions. Mixer sends only its dedicated service key,
pins one upstream channel, and never falls back to another provider. Discovery
is a capability declaration, not proof of generation access or free quota.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from .providers import ProviderError, _Deadline, _limits, _timeout, _sse_events, _terminal_chunk


TRANSPORT = "aistudio-http"
CHANNEL_HEADER = "X-Mixer-Upstream-Channel"
CHANNELS = {"ais": "playground", "build": "build"}
_MODEL = re.compile(r"[a-z][a-z0-9]*(?:[-.][a-z0-9]+)*\Z")
_CACHE = {}
_LOCK = threading.Lock()


class _SidecarDeadline(_Deadline):
    def check(self):
        if self.expired.is_set() or time.monotonic() >= self.end:
            raise _error(504, "upstream_timeout", "The AI Studio backend request timed out.")


def _error(status, code, message):
    return ProviderError(status, code, message)


def transport_mode(source):
    mode = os.environ.get(source.upper() + "_TRANSPORT", "public-api").strip()
    if mode not in ("public-api", TRANSPORT):
        raise _error(503, "provider_configuration_error", "Invalid AI Studio transport mode.")
    return mode


def initial_metadata(source):
    return {"source": source, "transport": TRANSPORT, "upstream_channel": CHANNELS[source],
            "profile_kind": "self-hosted-aistudio-session", "browser_required": False,
            "webpage_executor": False, "upstream_api": "private-rpc"}


def _config():
    base = os.environ.get("AISTUDIO_HTTP_BASE_URL", "").strip()
    key = os.environ.get("AISTUDIO_HTTP_API_KEY", "").strip()
    if not base or not key:
        raise _error(503, "provider_not_configured", "The AI Studio HTTP backend is not configured.")
    try:
        parsed = urlsplit(base)
        parsed.port
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in base or "#" in base
                or "\\" in base or "%" in base or re.search(r"[\s\x00-\x1f\x7f]", base)
                or any(p in (".", "..") for p in parsed.path.split("/"))
                or parsed.path.rstrip("/").endswith(("/v1", "/v1beta"))
                or parsed.scheme == "http" and os.environ.get("AISTUDIO_HTTP_ALLOW_HTTP", "false").lower() != "true"
                or len(base) > 2048 or len(key) > 4096 or not key.isascii() or re.search(r"[\x00-\x20\x7f]", key)):
            raise ValueError
    except (TypeError, ValueError):
        raise _error(503, "provider_configuration_error", "Invalid AI Studio HTTP backend configuration.") from None
    return base.rstrip("/"), key


def _allowlist(source):
    # Unlike public API profiles, this mode has no implicit model selection.
    raw = os.environ.get(source.upper() + "_MODELS", "")
    models = tuple(dict.fromkeys(v.strip() for v in raw.split(",") if v.strip()))
    if len(models) > 100 or any(len(m) > 160 or not _MODEL.fullmatch(m) for m in models):
        raise _error(503, "provider_configuration_error", "Invalid AI Studio model allowlist.")
    return models


def _new_client(timeout):
    return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, verify=True)


def _status(status):
    if status in (401, 403):
        return _error(502, "upstream_authentication_failed", "The AI Studio backend rejected authentication or account access.")
    if status == 404:
        return _error(404, "upstream_model_not_found", "The requested AI Studio model is unavailable.")
    if status == 429:
        return _error(429, "upstream_rate_limited", "The AI Studio account reached an upstream rate or quota limit.")
    if status in (408, 504):
        return _error(504, "upstream_timeout", "The AI Studio backend request timed out.")
    if 300 <= status < 400:
        return _error(502, "upstream_redirect_rejected", "The AI Studio backend returned an unexpected redirect.")
    if 400 <= status < 500:
        return _error(400, "upstream_invalid_request", "The AI Studio backend rejected the request.")
    return _error(502, "upstream_error", "The AI Studio backend request failed.")


def _check_response(response, channel):
    # Do not consume an error body: private transports can echo session material.
    if response.status_code == 401:
        raise _status(response.status_code)
    if response.headers.get(CHANNEL_HEADER) != channel or response.headers.get("X-Mixer-Channel-Locked") != "1":
        raise _error(502, "upstream_channel_mismatch", "The AI Studio backend did not confirm the requested channel.")
    if response.status_code != 200:
        raise _status(response.status_code)


def _decode(payload):
    try:
        def invalid(_value):
            raise ValueError
        value = json.loads(payload, parse_constant=invalid)
        if not isinstance(value, dict):
            raise ValueError
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise _error(502, "upstream_invalid_response", "The AI Studio backend returned invalid JSON.") from None
    if "error" in value:
        raw = value["error"]
        status = raw.get("code") if isinstance(raw, dict) else None
        raise _status(status if type(status) is int else 502)
    return value


def _read(response, deadline, bound):
    chunks = bytearray()
    for chunk in response.iter_bytes(chunk_size=8192):
        deadline.check()
        if len(chunks) + len(chunk) > bound:
            raise _error(502, "upstream_response_too_large", "The AI Studio backend response exceeded its size limit.")
        chunks.extend(chunk)
    deadline.check()
    return _decode(bytes(chunks))


def _safe_exception(error, deadline):
    if deadline.expired.is_set() or isinstance(error, httpx.TimeoutException):
        return _error(504, "upstream_timeout", "The AI Studio backend request timed out.")
    return _error(502, "upstream_connection_error", "The AI Studio backend connection failed.")


def _headers(key, channel, stream=False):
    return {"Authorization": "Bearer " + key, CHANNEL_HEADER: channel,
            "Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}


def _catalog(source, base, key, allowlist):
    cache_key = (source, base, hashlib.sha256(key.encode()).hexdigest(), allowlist)
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
    bounds, channel = _limits(), CHANNELS[source]
    with _new_client(_timeout(bounds)) as client:
        with _SidecarDeadline(client, min(bounds.total, 15)) as deadline:
            try:
                with client.stream("GET", base + "/v1/models", headers=_headers(key, channel)) as response:
                    _check_response(response, channel)
                    value = _read(response, deadline, min(bounds.response_bytes, 2 * 1024 * 1024))
            except (httpx.HTTPError, OSError, RuntimeError) as error:
                raise _safe_exception(error, deadline) from None
    entries = value.get("data")
    if not isinstance(entries, list) or len(entries) > 1000:
        raise _error(502, "upstream_invalid_response", "The AI Studio backend returned an invalid model catalogue.")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") not in allowlist:
            continue
        caps, channels = entry.get("capabilities", {}), entry.get("channels", [])
        methods = entry.get("supported_generation_methods", [])
        if (not isinstance(caps, dict) or caps.get("chat_model") is not True
                or not isinstance(channels, list) or channel not in channels
                or not isinstance(methods, list) or "generateContent" not in methods):
            continue
        model = entry["id"]
        if model in result:
            raise _error(502, "upstream_invalid_response", "The AI Studio model catalogue contains duplicate models.")
        result[model] = entry
    with _LOCK:
        if len(_CACHE) >= 32:
            _CACHE.clear()
        _CACHE[cache_key] = (time.monotonic() + 30, copy.deepcopy(result))
    return result


def _thinking(entry):
    """Exact native level names from the upstream catalog, no local evidence gate."""
    caps = entry.get("capabilities", {})
    options = entry.get("capability_options", {})
    levels = options.get("thinking_levels", []) if isinstance(options, dict) else []
    levels = ({v.lower() for v in levels} if isinstance(levels, list)
              and all(isinstance(v, str) and v.lower() in ("minimal", "low", "medium", "high") for v in levels)
              else set())
    return {effort: {"thinkingLevel": effort.upper()} for effort in ("low", "medium", "high")
            if caps.get("thinking_level") is True and effort in levels}


class SidecarRoute:
    def __init__(self, source, model, effort, alias, base, key, entry):
        self.source, self.model, self.effort, self.alias = source, model, effort, alias
        self.base, self._key, self.channel = base, key, CHANNELS[source]
        self._bounds, self._entry = _limits(), entry
        self._thinking = _thinking(entry)
        self._native_thinking = {}
        if effort is not None and effort not in self._thinking:
            raise _error(400, "unsupported_reasoning_effort", "The upstream catalog does not declare this native thinking level.")

    @property
    def capabilities(self):
        caps = self._entry["capabilities"]
        return {"text": True, "streaming": True,
                "image_input": caps.get("image_input") is True,
                "image_output": False,
                "tools": caps.get("function_declarations") is True,
                "thinking_efforts": list(self._thinking)}

    @property
    def meta(self):
        result = dict(initial_metadata(self.source), upstream_model=self.model, requested_effort=self.effort,
                      model_identity="exact-upstream-catalogue-id",
                      verification="upstream-declared", capability_policy="upstream-controlled",
                      manual_verification_required=False)
        thinking = self._thinking.get(self.effort, self._native_thinking)
        if "thinkingLevel" in thinking:
            result["effective_thinking_level"] = thinking["thinkingLevel"].lower()
        if "thinkingBudget" in thinking:
            result["effective_thinking_budget"] = thinking["thinkingBudget"]
        return result

    def _encoded(self, body):
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise _error(400, "invalid_request", "The request must contain valid JSON.") from None
        if len(data) > self._bounds.request_bytes:
            raise _error(413, "request_too_large", "The request exceeded the provider size limit.")
        return data

    def prepare(self, native_body):
        from .api_formats import to_native
        self._native_thinking = {}
        self._encoded(native_body)
        body = to_native(native_body, "gemini")
        if "cachedContent" in body or "safetySettings" in body:
            raise _error(400, "unsupported_parameter", "Cached content and safety-setting overrides are not enabled for this channel.")
        if "systemInstruction" in body and any(set(part) != {"text"} for part in body["systemInstruction"]["parts"]):
            raise _error(400, "unsupported_parameter", "System instructions must contain plain text only.")
        if "toolConfig" in body:
            tool_config = body["toolConfig"]
            function_config = tool_config.get("functionCallingConfig") if isinstance(tool_config, dict) else None
            if (not isinstance(tool_config, dict) or set(tool_config) != {"functionCallingConfig"}
                    or not isinstance(function_config, dict)
                    or set(function_config) - {"mode", "allowedFunctionNames"}
                    or function_config.get("mode", "AUTO") not in ("AUTO", "NONE", "ANY")):
                raise _error(400, "unsupported_tools", "The requested function calling configuration is unsupported.")
            names = function_config.get("allowedFunctionNames", [])
            declared = {fn.get("name") for group in body.get("tools", []) for fn in group.get("functionDeclarations", [])
                        if isinstance(fn.get("name"), str)}
            if not isinstance(names, list) or any(not isinstance(name, str) or name not in declared for name in names):
                raise _error(400, "unsupported_tools", "Allowed function names must refer to declared tools.")
        gen = body.setdefault("generationConfig", {})
        for field in ("temperature", "topP", "presencePenalty", "frequencyPenalty"):
            if field in gen and (isinstance(gen[field], bool) or not isinstance(gen[field], (int, float))):
                raise _error(400, "invalid_request", "Generation sampling parameters must be numeric.")
        for field in ("maxOutputTokens", "topK", "seed"):
            if field in gen and (type(gen[field]) is not int or field != "seed" and gen[field] < 1):
                raise _error(400, "invalid_request", "Generation token and sampling limits must be valid integers.")
        if "stopSequences" in gen and (not isinstance(gen["stopSequences"], list) or len(gen["stopSequences"]) > 5
                                       or any(not isinstance(v, str) or not v for v in gen["stopSequences"])):
            raise _error(400, "invalid_request", "Stop sequences must contain at most five nonempty strings.")
        requested = gen.get("thinkingConfig", {})
        if not isinstance(requested, dict):
            raise _error(400, "invalid_thinking_config", "thinkingConfig must be an object.")
        if self.effort is not None and requested:
            raise _error(400, "thinking_conflict", "Do not combine a thinking effort with a native thinking configuration.")
        if requested:
            normalized = dict(requested)
            if set(normalized) - {"thinkingLevel", "thinkingBudget", "includeThoughts"}:
                raise _error(400, "invalid_thinking_config", "Unknown native thinking configuration field.")
            if isinstance(normalized.get("thinkingLevel"), str):
                normalized["thinkingLevel"] = normalized["thinkingLevel"].upper()
            if "thinkingLevel" in normalized and normalized["thinkingLevel"] not in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
                raise _error(400, "invalid_thinking_config", "Invalid native thinking level.")
            if "thinkingBudget" in normalized and (type(normalized["thinkingBudget"]) is not int or not -1 <= normalized["thinkingBudget"] <= 1048576):
                raise _error(400, "invalid_thinking_config", "Invalid native thinking budget.")
            if "includeThoughts" in normalized and type(normalized["includeThoughts"]) is not bool:
                raise _error(400, "invalid_thinking_config", "includeThoughts must be a boolean.")
            gen["thinkingConfig"] = normalized
            self._native_thinking = normalized
        elif self.effort is not None:
            gen["thinkingConfig"] = dict(self._thinking[self.effort])
        self._encoded(body)
        return body

    def _validate_result(self, value, terminal=False):
        from .api_formats import _candidate
        try:
            parts, _finish = _candidate(value, allow_empty=not terminal)
        except (ProviderError, TypeError, ValueError, AttributeError):
            raise _error(502, "upstream_invalid_response", "The AI Studio backend returned an unsupported response.") from None
        if terminal and not _terminal_chunk(value):
            raise _error(502, "upstream_stream_incomplete", "The AI Studio backend returned no completion status.")
        return value

    def request_metadata(self, native_body):
        # Validate once for accurate logs, without returning a rewritten body
        # that could make a subsequent prepare mistake our own mapping for a
        # client-supplied conflicting thinking configuration.
        self.prepare(native_body)
        return self.meta

    def generate(self, native_body):
        body = self.prepare(native_body)
        data = self._encoded(body)
        with _new_client(_timeout(self._bounds)) as client:
            with _SidecarDeadline(client, self._bounds.total) as deadline:
                try:
                    with client.stream("POST", self.base + "/v1beta/models/" + self.model + ":generateContent",
                                       headers=_headers(self._key, self.channel), content=data) as response:
                        self._check_generation_response(response, body)
                        return self._validate_result(_read(response, deadline, self._bounds.response_bytes), True)
                except (httpx.HTTPError, OSError, RuntimeError) as error:
                    raise _safe_exception(error, deadline) from None

    def stream(self, native_body):
        body = self.prepare(native_body)
        data = self._encoded(body)
        with _new_client(_timeout(self._bounds)) as client:
            with _SidecarDeadline(client, self._bounds.total) as deadline:
                try:
                    with client.stream("POST", self.base + "/v1beta/models/" + self.model + ":streamGenerateContent",
                                       params={"alt": "sse"}, headers=_headers(self._key, self.channel, True), content=data) as response:
                        self._check_generation_response(response, body)
                        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                            raise _error(502, "upstream_invalid_stream", "The AI Studio backend did not return SSE.")
                        terminal = False
                        for payload in _sse_events(response, deadline, self._bounds):
                            if payload == b"[DONE]":
                                break
                            value = self._validate_result(_decode(payload))
                            terminal = terminal or _terminal_chunk(value)
                            yield value
                        deadline.check()
                        if not terminal:
                            raise _error(502, "upstream_stream_incomplete", "The AI Studio stream ended without a final result.")
                except (httpx.HTTPError, OSError, RuntimeError) as error:
                    raise _safe_exception(error, deadline) from None
                except ProviderError as error:
                    # Shared parser diagnostics name the public provider; keep
                    # client-visible failures specific to this transport.
                    raise _error(error.status, error.code, "The AI Studio backend stream failed validation or exceeded a transport limit.") from None

    def _check_generation_response(self, response, body):
        _check_response(response, self.channel)
        level = body.get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel")
        if level is not None and (response.headers.get("X-Mixer-Thinking-Level") != level
                                  or response.headers.get("X-Mixer-Thinking-Validation") != "account-catalog"):
            # Confirm the selected account did not silently clamp the level.
            raise _error(502, "upstream_thinking_not_verified",
                         "The AI Studio backend did not verify the requested thinking level against the selected account catalogue.")


def resolve_route(source, alias, effort=None):
    if effort is not None and effort not in ("low", "medium", "high"):
        raise _error(400, "unsupported_reasoning_effort", "reasoning_effort must be low, medium, or high.")
    allowed = _allowlist(source)
    requested = alias[len(source) + 1:]
    model, suffix = requested, None
    if requested not in allowed and "-" in requested:
        stem, candidate = requested.rsplit("-", 1)
        if candidate in ("low", "medium", "high"):
            model, suffix = stem, candidate
    if model not in allowed:
        raise _error(404, "unknown_provider_model", "The requested model is not enabled for this AI Studio channel.")
    if suffix and effort and suffix != effort:
        raise _error(400, "thinking_conflict", "The model suffix and reasoning effort conflict.")
    base, key = _config()
    entry = _catalog(source, base, key, allowed).get(model)
    if not entry:
        raise _error(404, "upstream_model_not_found", "The requested model is not available on this AI Studio channel.")
    return SidecarRoute(source, model, suffix or effort, alias, base, key, entry)


def list_models(source):
    try:
        base, key = _config()
        allowed = _allowlist(source)
        entries = _catalog(source, base, key, allowed) if allowed else {}
        result = []
        for model, entry in entries.items():
            base_alias = source + "-" + model
            route = SidecarRoute(source, model, None, base_alias, base, key, entry)
            for effort in [None] + list(route._thinking):
                alias = base_alias + ("-" + effort if effort else "")
                variant = SidecarRoute(source, model, effort, alias, base, key, entry)
                result.append({"id": alias, "object": "model", "created": 0, "owned_by": "google",
                               "source": source, "transport": TRANSPORT, "upstream_model": model,
                               "capabilities": variant.capabilities, "metadata": variant.meta, "discovered": True})
        return result
    except ProviderError:
        return []
