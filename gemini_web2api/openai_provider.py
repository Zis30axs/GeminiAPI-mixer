"""Opt-in GPT routing through an operator-controlled OpenAI-only Sub2API group.

The exact upstream model ID and request parameters are preserved. Discovery
confirms visibility, not per-model tools, vision, reasoning support or quota.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any, Mapping, Optional, Sequence

import httpx

from .openai_transport import BoundedOpenAITransport, _base_url, _limit, _truth
from .providers import ProviderError


SOURCE = "openai"
TRANSPORT = "sub2api-openai"
_MODEL = re.compile(r"(?:gpt-[A-Za-z0-9][A-Za-z0-9._-]*|chatgpt-[A-Za-z0-9][A-Za-z0-9._-]*|o[0-9]+(?:[.-][A-Za-z0-9][A-Za-z0-9._-]*)?)\Z")
_KINDS = {"chat": "/v1/chat/completions", "responses": "/v1/responses"}


def owns_model(alias: str) -> bool:
    """Reserve GPT/o-series names even when disabled, never falling back to Web."""
    return alias.lower().startswith(("gpt-", "chatgpt-")) or bool(re.match(r"o[0-9]", alias, re.I))


def request_metadata(req: dict, alias: str) -> dict:
    reasoning = req.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise ProviderError(400, "invalid_request", "reasoning must be an object.")
    first, second = req.get("reasoning_effort"), (reasoning or {}).get("effort")
    if first is not None and second is not None and first != second:
        raise ProviderError(400, "thinking_conflict", "reasoning_effort and reasoning.effort conflict.")
    effort = first if first is not None else second
    if effort is not None and (not isinstance(effort, str) or not re.fullmatch(r"[a-z]{1,24}", effort)):
        raise ProviderError(400, "invalid_request", "Invalid reasoning effort value.")
    metadata = OpenAIProvider._metadata(alias, alias)
    if effort is not None:
        metadata["requested_effort"] = effort
    return metadata


class OpenAIProvider(BoundedOpenAITransport):
    provider_name = "OpenAI"

    def __init__(self, base_url: str = "", api_key: str = "", models: Sequence[str] = (),
                 group_confirmed: bool = False, allow_http: bool = False, *,
                 transport: Optional[httpx.BaseTransport] = None,
                 connect_timeout: float = 10, read_timeout: float = 90,
                 total_timeout: float = 180, max_request_bytes: int = 20 * 1024 * 1024,
                 max_response_bytes: int = 16 * 1024 * 1024,
                 max_event_bytes: int = 1024 * 1024):
        self.base_url = _base_url(base_url, allow_http, "MIXER_OPENAI") if base_url else ""
        if not isinstance(api_key, str) or re.search(r"[\r\n\x00]", api_key):
            raise ProviderError(503, "invalid_provider_configuration", "Invalid OpenAI upstream API key configuration.")
        self._api_key = api_key
        if any(not isinstance(model, str) for model in models):
            raise ProviderError(503, "invalid_provider_configuration", "Invalid MIXER_OPENAI_MODELS allowlist.")
        self._models = tuple(dict.fromkeys(model.strip() for model in models if model.strip()))
        if len(self._models) > 512 or any(len(model) > 200 or not _MODEL.fullmatch(model) for model in self._models):
            raise ProviderError(503, "invalid_provider_configuration", "MIXER_OPENAI_MODELS must contain exact GPT or o-series IDs.")
        self.enabled = bool(self.base_url and self._api_key and self._models and group_confirmed)
        self._transport = transport
        for name, value, low, high in (
            ("connect_timeout", connect_timeout, 0.05, 600),
            ("read_timeout", read_timeout, 0.05, 600),
            ("total_timeout", total_timeout, 0.05, 3600),
            ("max_request_bytes", max_request_bytes, 1, 100 * 1024 * 1024),
            ("max_response_bytes", max_response_bytes, 1, 100 * 1024 * 1024),
            ("max_event_bytes", max_event_bytes, 1, 10 * 1024 * 1024),
        ):
            number = _limit({name: str(value)}, name, value, low, high, self.provider_name)
            setattr(self, name, int(number) if name.startswith("max_") else number)
        self._available, self._available_until = None, 0.0

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None, **kwargs: Any):
        env = os.environ if environ is None else environ
        for parameter, name, default, low, high in (
            ("connect_timeout", "CONNECT_TIMEOUT_SEC", 10, 0.05, 600),
            ("read_timeout", "READ_TIMEOUT_SEC", 90, 0.05, 600),
            ("total_timeout", "TOTAL_TIMEOUT_SEC", 180, 0.05, 3600),
            ("max_request_bytes", "MAX_REQUEST_BYTES", 20 * 1024 * 1024, 1024, 100 * 1024 * 1024),
            ("max_response_bytes", "MAX_RESPONSE_BYTES", 16 * 1024 * 1024, 1024, 100 * 1024 * 1024),
            ("max_event_bytes", "MAX_SSE_LINE_BYTES", 1024 * 1024, 256, 10 * 1024 * 1024),
        ):
            if parameter not in kwargs:
                value = _limit(env, "MIXER_OPENAI_" + name, default, low, high, cls.provider_name)
                kwargs[parameter] = int(value) if parameter.startswith("max_") else value
        return cls(base_url=env.get("MIXER_OPENAI_BASE_URL", ""),
                   api_key=env.get("MIXER_OPENAI_API_KEY", ""),
                   models=env.get("MIXER_OPENAI_MODELS", "").split(","),
                   group_confirmed=_truth(env.get("MIXER_OPENAI_GROUP_CONFIRMED", "false")),
                   allow_http=_truth(env.get("MIXER_OPENAI_ALLOW_HTTP", "false")), **kwargs)

    def _require_enabled(self):
        if not self.enabled:
            raise ProviderError(503, "provider_not_configured",
                                "OpenAI requires a gateway, dedicated API key, model allowlist and MIXER_OPENAI_GROUP_CONFIRMED=true.")

    def _headers(self, stream=False):
        return dict(super()._headers(stream), **{"X-Mixer-Upstream-Hop": "1"})

    @staticmethod
    def _check_status(response):
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise ProviderError(502, "upstream_authentication_failed", "OpenAI upstream rejected its configured credentials or permissions; the Mixer key was accepted.")
        if status == 429:
            raise ProviderError(429, "upstream_rate_limited", "OpenAI upstream quota or rate limit was reached.")
        if status == 404:
            raise ProviderError(404, "upstream_not_found", "OpenAI upstream model or API endpoint is unavailable.")
        if status in (408, 504):
            raise ProviderError(504, "upstream_timeout", "OpenAI upstream timed out.")
        if 300 <= status < 400:
            raise ProviderError(502, "upstream_redirect_rejected", "OpenAI upstream redirects are disabled.")
        raise ProviderError(status if 400 <= status < 500 else 502,
                            "upstream_rejected_request" if status < 500 else "upstream_error",
                            "OpenAI upstream rejected the request or requested capability." if status < 500 else "OpenAI upstream request failed.")

    @staticmethod
    def _metadata(alias, model):
        return {"source": SOURCE, "transport": TRANSPORT, "model": alias,
                "upstream_model": model, "capabilities": "upstream-controlled",
                "thinking_control": "upstream-controlled",
                "source_verification": "operator-confirmed-openai-only-group"}

    def list_models(self):
        if not self.enabled:
            return []
        available = self._discover(time.monotonic() + min(self.total_timeout, 15))
        return [{"id": model, "object": "model", "created": 0, "owned_by": SOURCE,
                 "discovered": True, **self._metadata(model, model),
                 "x_mixer": self._metadata(model, model)} for model in self._models if model in available]

    def _prepare(self, req, api_kind, alias, deadline, stream):
        self._require_enabled()
        if api_kind not in _KINDS:
            raise ProviderError(501, "unsupported_api", "OpenAI adapter supports Chat Completions and Responses only; native Gemini is unavailable.")
        if not isinstance(req, dict) or not isinstance(alias, str) or alias not in self._models:
            raise ProviderError(404, "model_not_found", "OpenAI model is not allowlisted; use an exact model ID from /v1/models.")
        request_metadata(req, alias)
        try:
            body = copy.deepcopy(req)
            body["model"], body["stream"] = alias, stream
            body.pop("x_mixer", None)
            size = len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderError(400, "invalid_request", "OpenAI request must contain valid JSON values.") from None
        if size > self.max_request_bytes:
            raise ProviderError(413, "request_too_large", "OpenAI request exceeds the configured limit.")
        if alias not in self._discover(deadline):
            raise ProviderError(404, "model_not_found", "OpenAI upstream does not advertise this model.")
        return _KINDS[api_kind], body, alias


def list_models():
    try:
        return OpenAIProvider.from_env().list_models()
    except ProviderError as error:
        # A down optional provider must not hide existing Gemini/Claude models.
        from .gemini import log
        log("provider_discovery source=openai code=%s status=%s" % (error.code, error.status))
        return []


def proxy(req, api_kind, alias):
    return OpenAIProvider.from_env().proxy(req, api_kind, alias)


def stream(req, api_kind, alias):
    yield from OpenAIProvider.from_env().stream(req, api_kind, alias)
