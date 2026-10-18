"""Opt-in adapter for an operator-authorized Antigravity-only Sub2API key.

This module speaks only the self-hosted gateway's documented public surface.
It neither manages Google credentials nor calls Google's private RPCs.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

import httpx

from .providers import ProviderError
from .openai_transport import BoundedOpenAITransport, _base_url, _limit, _truth
from .antigravity_catalog import model_details, read_model_evidence, resolve_model_effort
from .antigravity_quota import load_quota_catalog


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_EFFORT_SUFFIX = re.compile(r"-(low|medium|high)\Z")
_KINDS = {
    "chat": "/v1/chat/completions",
    "chat.completions": "/v1/chat/completions",
    "chat/completions": "/v1/chat/completions",
    "/v1/chat/completions": "/v1/chat/completions",
    "responses": "/v1/responses",
    "/v1/responses": "/v1/responses",
}
_MAX_MODELS = 512


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return None


class AntigravityProvider(BoundedOpenAITransport):
    """Synchronous, bounded HTTP adapter. Host selection is operator-only."""

    provider_name = "Antigravity"

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        models: Sequence[str] = (),
        group_confirmed: bool = False,
        allow_http: bool = False,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        total_timeout: float = 180.0,
        max_request_bytes: int = 20 * 1024 * 1024,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_event_bytes: int = 1024 * 1024,
    ) -> None:
        self.base_url = _base_url(base_url, allow_http) if base_url else ""
        if not isinstance(api_key, str) or re.search(r"[\r\n\x00]", api_key):
            raise ProviderError(500, "invalid_provider_configuration", "Invalid Antigravity API key configuration.")
        self._api_key = api_key
        self._models = tuple(dict.fromkeys(model.strip() for model in models if model.strip()))
        if len(self._models) > _MAX_MODELS or any(not _MODEL_ID.fullmatch(model) for model in self._models):
            raise ProviderError(500, "invalid_provider_configuration", "Invalid ANTIGRAVITY_MODELS allowlist.")
        self.enabled = bool(self.base_url and self._api_key and self._models and group_confirmed)
        self._transport = transport
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.max_event_bytes = max_event_bytes
        if (
            any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
                for value in (connect_timeout, read_timeout, total_timeout))
            or max_request_bytes <= 0 or max_response_bytes <= 0 or max_event_bytes <= 0
        ):
            raise ProviderError(500, "invalid_provider_configuration", "Invalid Antigravity transport limits.")
        self._available = None
        self._available_until = 0.0

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None, **kwargs: Any) -> "AntigravityProvider":
        env = os.environ if environ is None else environ
        for parameter, name, default, low, high in (
            ("connect_timeout", "PROVIDER_CONNECT_TIMEOUT_SEC", 10, 0.05, 600),
            ("read_timeout", "PROVIDER_READ_TIMEOUT_SEC", 60, 0.05, 600),
            ("total_timeout", "PROVIDER_TOTAL_TIMEOUT_SEC", 180, 0.05, 3600),
            ("max_request_bytes", "PROVIDER_MAX_REQUEST_BYTES", 20 * 1024 * 1024, 1024, 100 * 1024 * 1024),
            ("max_response_bytes", "PROVIDER_MAX_RESPONSE_BYTES", 16 * 1024 * 1024, 1024, 100 * 1024 * 1024),
            ("max_event_bytes", "PROVIDER_MAX_SSE_LINE_BYTES", 1024 * 1024, 256, 10 * 1024 * 1024),
        ):
            if parameter not in kwargs:
                value = _limit(env, name, default, low, high)
                kwargs[parameter] = int(value) if parameter.startswith("max_") else value
        return cls(
            base_url=env.get("ANTIGRAVITY_BASE_URL", ""),
            api_key=env.get("ANTIGRAVITY_API_KEY", ""),
            models=env.get("ANTIGRAVITY_MODELS", "").split(","),
            group_confirmed=_truth(env.get("ANTIGRAVITY_GROUP_CONFIRMED", "false")),
            allow_http=_truth(env.get("ANTIGRAVITY_ALLOW_HTTP", "false")),
            **kwargs,
        )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ProviderError(
                503, "provider_not_configured",
                "Antigravity requires a configured self-hosted gateway, API key, model allowlist "
                "and ANTIGRAVITY_GROUP_CONFIRMED=true for an Antigravity-only group.",
            )


    @staticmethod
    def _metadata(alias: str, model: str, evidence: Optional[dict] = None, quota_catalog: Optional[dict] = None) -> dict:
        details = model_details(model, evidence, quota_catalog)
        verified = details["verification"]
        if verified["status"] == "unavailable":
            availability = "unavailable"
        elif verified["http_status"] == 429:
            availability = "temporarily_unavailable"
        elif verified["status"] == "verified":
            availability = "available"
        else:
            availability = "unverified"
        return {
            "source": "antigravity", "transport": "sub2api-openai",
            "model": alias, "upstream_model": model,
            "capabilities": "unknown",
            "source_verification": "operator-confirmed-exclusive-group",
            **details,
            "availability": {"status": availability, "reason": verified["reason"]},
        }


    def list_models(self) -> list:
        if not self.enabled:
            return []
        available = self._discover(time.monotonic() + self.total_timeout)
        evidence, quota_catalog = read_model_evidence(), load_quota_catalog()
        return [
            self._model_row(model, evidence, quota_catalog)
            for model in self._models if model in available
            and evidence["models"].get(model, {}).get("status") != "unavailable"
        ]

    def _model_row(self, model: str, evidence: dict, quota_catalog: dict) -> dict:
        metadata = self._metadata("ag-" + model, model, evidence, quota_catalog)
        return {"id": "ag-" + model, "object": "model", "created": 0,
                "owned_by": "antigravity", **metadata, "x_mixer": metadata}

    def _prepare(self, req: dict, api_kind: str, alias: str, deadline: float, stream: bool) -> Tuple[str, dict, str]:
        self._require_enabled()
        if api_kind not in _KINDS:
            raise ProviderError(501, "unsupported_api", "Antigravity adapter supports Chat Completions and Responses only; native Gemini is unavailable.")
        if not isinstance(req, dict) or not isinstance(alias, str) or not alias.startswith("ag-"):
            raise ProviderError(400, "invalid_model", "Antigravity model names require the ag- prefix.")
        model, body = resolve_model_effort(alias[3:], req, self._models)
        if model not in self._models:
            if _EFFORT_SUFFIX.search(model):
                raise ProviderError(400, "unsupported_thinking_level", "Antigravity thinking suffixes require an exact allowlisted upstream model; no effort mapping is inferred.")
            raise ProviderError(404, "model_not_found", "Antigravity model is not allowlisted.")
        if model not in self._discover(deadline):
            raise ProviderError(404, "model_not_found", "Antigravity upstream does not advertise this model.")
        body["model"] = model
        body["stream"] = stream
        body.pop("x_mixer", None)
        try:
            size = len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError, UnicodeError):
            raise ProviderError(400, "invalid_request", "Antigravity request must contain valid JSON values.") from None
        if size > self.max_request_bytes:
            raise ProviderError(413, "request_too_large", "Antigravity request exceeds the configured limit.")
        return _KINDS[api_kind], body, model


    def quota(self) -> dict:
        self._require_enabled()
        evidence, quota_catalog = read_model_evidence(), load_quota_catalog()
        catalog_models = tuple(dict.fromkeys((*self._models, *evidence["models"])))
        result = {
            "source": "antigravity", "transport": "sub2api-openai", "scope": "sub2api-key-wallet",
            "status": "unknown", "remaining": None, "unit": None, "unlimited": False,
            "limit": None, "used": None, "windows": [],
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "models": [self._model_row(model, evidence, quota_catalog) for model in catalog_models],
            "model_quota_source": "sub2api-account-quota",
            "model_quota_status": quota_catalog.get("status", "unknown"),
        }
        try:
            raw = self._read_json("GET", "/v1/usage", None, time.monotonic() + self.total_timeout)
        except ProviderError as error:
            result["status"] = "unavailable"
            result["error"] = {"status": error.status, "code": error.code}
            return result
        result["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        quota_data = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
        remaining = _number(quota_data.get("remaining"))
        if remaining is None:
            remaining = _number(raw.get("remaining"))
        if remaining is None:
            remaining = _number(raw.get("balance"))
        result["limit"] = _number(quota_data.get("limit"))
        result["used"] = _number(quota_data.get("used"))
        unit = quota_data.get("unit", raw.get("unit"))
        if unit in ("USD", "tokens", "requests", "credits", "percent"):
            result["unit"] = unit
        if remaining == -1:
            result["status"], result["unlimited"] = "unlimited", True
        elif remaining is not None:
            result["remaining"], result["status"] = remaining, "available"
        windows = raw.get("rate_limits")
        if isinstance(windows, list):
            for window in windows[:16]:
                if isinstance(window, dict) and window.get("window") in ("5h", "1d", "7d"):
                    result["windows"].append({
                        "window": window["window"], "limit": _number(window.get("limit")),
                        "used": _number(window.get("used")), "remaining": _number(window.get("remaining")),
                        "reset_at": _timestamp(window.get("reset_at")), "unit": "USD",
                    })
        subscription = raw.get("subscription")
        if isinstance(subscription, dict):
            result["subscription"] = {
                field: _number(subscription.get(field)) for field in (
                    "daily_usage_usd", "weekly_usage_usd", "monthly_usage_usd",
                    "daily_limit_usd", "weekly_limit_usd", "monthly_limit_usd",
                )
            }
            result["subscription"]["expires_at"] = _timestamp(subscription.get("expires_at"))
            result["subscription"]["weekly_window_start"] = _timestamp(subscription.get("weekly_window_start"))
        return result


def list_models() -> list:
    # Discovery is best-effort so a disabled or unavailable optional provider
    # never hides the existing Gemini Web/AI Studio model lists.
    try:
        return AntigravityProvider.from_env().list_models()
    except ProviderError:
        return []


def quota() -> dict:
    return AntigravityProvider.from_env().quota()


def request_metadata(req: dict, alias: str) -> dict:
    """Resolve a verified effort before logging or emitting response headers."""
    models = [value.strip() for value in os.environ.get("ANTIGRAVITY_MODELS", "").split(",") if value.strip()]
    model, _body = resolve_model_effort(alias[3:], req, models)
    return AntigravityProvider._metadata(alias, model)


def proxy(req: dict, api_kind: str, alias: str) -> dict:
    return AntigravityProvider.from_env().proxy(req, api_kind, alias)


def stream(req: dict, api_kind: str, alias: str) -> Iterator[Tuple[Optional[str], Any]]:
    yield from AntigravityProvider.from_env().stream(req, api_kind, alias)
