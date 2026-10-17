"""Opt-in adapter for an operator-authorized Antigravity-only Sub2API key.

This module speaks only the self-hosted gateway's documented public surface.
It neither manages Google credentials nor calls Google's private RPCs.
"""

from __future__ import annotations

import codecs
import copy
import ipaddress
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import httpx

from .providers import ProviderError


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


class _WallDeadline:
    """Close the current client when a peer keeps a response alive forever."""

    def __init__(self, client: httpx.Client, deadline: float) -> None:
        self.client = client
        self.timer = threading.Timer(max(0.001, deadline - time.monotonic()), self._expire)
        self.timer.daemon = True

    def _expire(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def __enter__(self) -> "_WallDeadline":
        self.timer.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.timer.cancel()


def _truth(value: Any) -> bool:
    return str(value).lower() == "true"


def _limit(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(env.get(name, str(default)))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ProviderError(500, "invalid_provider_configuration", "Invalid Antigravity transport limit configuration.") from None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON number")


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


def _base_url(value: str, allow_http: bool) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port  # Reject invalid port syntax before passing to httpx.
        del port
        if (
            not host
            or parts.scheme not in ("http", "https")
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or re.search(r"[\s\x00-\x1f\x7f]", value)
            or "%" in parts.netloc
            or "%" in parts.path
            or any(segment in (".", "..") for segment in parts.path.split("/"))
            or parts.path.rstrip("/").endswith("/v1")
        ):
            raise ValueError
        loopback = host.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if parts.scheme == "http" and not (loopback or allow_http):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise ProviderError(
            500, "invalid_provider_configuration",
            "ANTIGRAVITY_BASE_URL must be a clean HTTPS origin/base without /v1; "
            "non-loopback HTTP requires ANTIGRAVITY_ALLOW_HTTP=true.",
        ) from None


class AntigravityProvider:
    """Synchronous, bounded HTTP adapter. Host selection is operator-only."""

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

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(504, "upstream_timeout", "Antigravity upstream deadline exceeded.")
        return remaining

    def _client(self, deadline: float) -> httpx.Client:
        remaining = self._remaining(deadline)
        return httpx.Client(
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=min(self.connect_timeout, remaining),
                read=min(self.read_timeout, remaining),
                write=min(self.read_timeout, remaining),
                pool=min(self.connect_timeout, remaining),
            ),
        )

    def _headers(self, stream: bool = False) -> Dict[str, str]:
        return {
            "Authorization": "Bearer " + self._api_key,
            "Accept": "text/event-stream" if stream else "application/json",
        }

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        status = response.status_code
        if not 200 <= status < 300:
            if 300 <= status < 400:
                raise ProviderError(502, "upstream_redirect_rejected", "Antigravity upstream redirects are disabled.")
            raise ProviderError(
                status if 400 <= status < 500 else 502,
                "upstream_rejected_request" if status < 500 else "upstream_error",
                "Antigravity upstream returned HTTP %d." % status,
            )

    def _read_json(self, method: str, path: str, body: Optional[dict], deadline: float) -> dict:
        try:
            with self._client(deadline) as client, _WallDeadline(client, deadline):
                with client.stream(method, self.base_url + path, headers=self._headers(), json=body) as response:
                    self._remaining(deadline)
                    self._check_status(response)
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        self._remaining(deadline)
                        content.extend(chunk)
                        if len(content) > self.max_response_bytes:
                            raise ProviderError(502, "upstream_response_too_large", "Antigravity upstream response exceeds the configured limit.")
                    self._remaining(deadline)
                    result = json.loads(content, parse_constant=_invalid_json_constant)
                    if not isinstance(result, dict):
                        raise ValueError
                    if result.get("error") is not None:
                        raise ProviderError(502, "upstream_error", "Antigravity upstream returned an error.")
                    return result
        except httpx.TimeoutException:
            raise ProviderError(504, "upstream_timeout", "Antigravity upstream timed out.") from None
        except (httpx.HTTPError, RuntimeError):
            self._remaining(deadline)
            raise ProviderError(502, "upstream_connection_error", "Antigravity upstream connection failed.") from None
        except (ValueError, UnicodeError):
            raise ProviderError(502, "invalid_upstream_response", "Antigravity upstream returned invalid JSON.") from None

    @staticmethod
    def _metadata(alias: str, model: str) -> dict:
        return {
            "source": "antigravity", "transport": "sub2api-openai",
            "model": alias, "upstream_model": model,
            "thinking_control": "upstream-controlled", "capabilities": "unknown",
            "source_verification": "operator-confirmed-exclusive-group",
        }

    def _decorate(self, result: dict, alias: str, model: str) -> dict:
        result = copy.deepcopy(result)
        if "model" in result:
            result["model"] = alias
        if isinstance(result.get("response"), dict):
            if "model" in result["response"]:
                result["response"]["model"] = alias
            result["response"]["x_mixer"] = self._metadata(alias, model)
        result["x_mixer"] = self._metadata(alias, model)
        return result

    def _discover(self, deadline: float) -> set:
        if self._available is not None and time.monotonic() < self._available_until:
            return self._available
        result = self._read_json("GET", "/v1/models", None, deadline)
        rows = result.get("data")
        if not isinstance(rows, list):
            raise ProviderError(502, "invalid_upstream_response", "Antigravity upstream models response is invalid.")
        available = {
            row["id"] for row in rows[:4096]
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] in self._models
        }
        self._available = available
        self._available_until = time.monotonic() + 30.0
        return available

    def list_models(self) -> list:
        if not self.enabled:
            return []
        available = self._discover(time.monotonic() + self.total_timeout)
        return [
            {
                "id": "ag-" + model, "object": "model", "created": 0,
                "owned_by": "antigravity", "source": "antigravity", "transport": "sub2api-openai",
                "capabilities": "unknown", "x_mixer": self._metadata("ag-" + model, model),
            }
            for model in self._models if model in available
        ]

    def _prepare(self, req: dict, api_kind: str, alias: str, deadline: float, stream: bool) -> Tuple[str, dict, str]:
        self._require_enabled()
        if api_kind not in _KINDS:
            raise ProviderError(501, "unsupported_api", "Antigravity adapter supports Chat Completions and Responses only; native Gemini is unavailable.")
        if not isinstance(req, dict) or not isinstance(alias, str) or not alias.startswith("ag-"):
            raise ProviderError(400, "invalid_model", "Antigravity model names require the ag- prefix.")
        model = alias[3:]
        if model not in self._models:
            if _EFFORT_SUFFIX.search(model):
                raise ProviderError(400, "unsupported_thinking_level", "Antigravity thinking suffixes require an exact allowlisted upstream model; no effort mapping is inferred.")
            raise ProviderError(404, "model_not_found", "Antigravity model is not allowlisted.")
        if model not in self._discover(deadline):
            raise ProviderError(404, "model_not_found", "Antigravity upstream does not advertise this model.")
        body = copy.deepcopy(req)
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

    def proxy(self, req: dict, api_kind: str, alias: str) -> dict:
        deadline = time.monotonic() + self.total_timeout
        path, body, model = self._prepare(req, api_kind, alias, deadline, False)
        return self._decorate(self._read_json("POST", path, body, deadline), alias, model)

    def _sse_event(self, event: Optional[str], data: list, alias: str, model: str) -> Optional[Tuple[Optional[str], Any]]:
        if not data:
            return None
        if event is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", event):
            raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream SSE event name is invalid.")
        raw = "\n".join(data)
        if raw == "[DONE]":
            return event, "[DONE]"
        try:
            payload = json.loads(raw, parse_constant=_invalid_json_constant)
        except (ValueError, UnicodeError):
            raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream returned an invalid SSE event.") from None
        if not isinstance(payload, dict):
            raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream SSE payload must be an object.")
        if (
            event in ("error", "response.failed") or payload.get("error") is not None
            or payload.get("type") in ("error", "response.failed")
            or (isinstance(payload.get("response"), dict) and payload["response"].get("error"))
        ):
            raise ProviderError(502, "upstream_stream_error", "Antigravity upstream stream failed.")
        return event, self._decorate(payload, alias, model)

    @staticmethod
    def _terminal(event: Optional[str], payload: Any) -> bool:
        if payload == "[DONE]":
            return True
        if event in ("response.completed", "response.incomplete", "response.cancelled"):
            return True
        if isinstance(payload, dict):
            if payload.get("type") in ("response.completed", "response.incomplete", "response.cancelled"):
                return True
            choices = payload.get("choices")
            if isinstance(choices, list):
                return any(isinstance(choice, dict) and choice.get("finish_reason") is not None for choice in choices)
        return False

    def stream(self, req: dict, api_kind: str, alias: str) -> Iterator[Tuple[Optional[str], Any]]:
        deadline = time.monotonic() + self.total_timeout
        path, body, model = self._prepare(req, api_kind, alias, deadline, True)
        try:
            with self._client(deadline) as client, _WallDeadline(client, deadline):
                with client.stream("POST", self.base_url + path, headers=self._headers(True), json=body) as response:
                    self._check_status(response)
                    if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                        raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream did not return SSE.")
                    decoder = codecs.getincrementaldecoder("utf-8")("strict")
                    at_start = True
                    buffer = ""
                    event = None
                    data = []
                    event_size = 0
                    total_size = 0
                    saw_data = False
                    terminal = False
                    for chunk in response.iter_bytes():
                        self._remaining(deadline)
                        total_size += len(chunk)
                        if total_size > self.max_response_bytes:
                            raise ProviderError(502, "upstream_response_too_large", "Antigravity upstream stream exceeds the configured limit.")
                        decoded = decoder.decode(chunk)
                        if at_start and decoded:
                            decoded = decoded.lstrip("\ufeff")
                            at_start = False
                        buffer += decoded
                        while True:
                            self._remaining(deadline)
                            match = re.search(r"[\r\n]", buffer)
                            if match is None or (buffer[match.start()] == "\r" and match.start() == len(buffer) - 1):
                                break
                            end = match.start() + (2 if buffer[match.start():].startswith("\r\n") else 1)
                            line, buffer = buffer[:match.start()], buffer[end:]
                            event_size += len(line.encode("utf-8")) + 1
                            if event_size > self.max_event_bytes:
                                raise ProviderError(502, "upstream_event_too_large", "Antigravity upstream SSE event exceeds the configured limit.")
                            if not line:
                                item = self._sse_event(event, data, alias, model)
                                if item is not None:
                                    if item[1] == "[DONE]" and not saw_data:
                                        raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream returned an empty SSE stream.")
                                    saw_data = True
                                    terminal = terminal or self._terminal(*item)
                                    yield item
                                    if item[1] == "[DONE]":
                                        return
                                event, data, event_size = None, [], 0
                            elif not line.startswith(":"):
                                field, sep, value = line.partition(":")
                                if value.startswith(" "):
                                    value = value[1:]
                                if field == "data":
                                    data.append(value if sep else "")
                                elif field == "event":
                                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
                                        raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream SSE event name is invalid.")
                                    event = value
                        if event_size + len(buffer.encode("utf-8")) > self.max_event_bytes:
                            raise ProviderError(502, "upstream_event_too_large", "Antigravity upstream SSE event exceeds the configured limit.")
                    self._remaining(deadline)
                    buffer += decoder.decode(b"", final=True)
                    for line in buffer.splitlines():
                        field, sep, value = line.partition(":")
                        if field == "data":
                            data.append(value[1:] if value.startswith(" ") else value)
                        elif field == "event":
                            event = value.strip()
                    item = self._sse_event(event, data, alias, model)
                    if item is not None:
                        saw_data = True
                        terminal = terminal or self._terminal(*item)
                        yield item
                    if not saw_data:
                        raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream returned an empty SSE stream.")
                    if not terminal:
                        raise ProviderError(502, "upstream_stream_truncated", "Antigravity upstream stream ended without a terminal event.")
        except httpx.TimeoutException:
            raise ProviderError(504, "upstream_timeout", "Antigravity upstream timed out.") from None
        except (httpx.HTTPError, RuntimeError):
            self._remaining(deadline)
            raise ProviderError(502, "upstream_connection_error", "Antigravity upstream connection failed.") from None
        except UnicodeError:
            raise ProviderError(502, "invalid_upstream_stream", "Antigravity upstream stream is not valid UTF-8.") from None

    def quota(self) -> dict:
        self._require_enabled()
        result = {
            "source": "antigravity", "transport": "sub2api-openai", "scope": "sub2api-key-wallet",
            "status": "unknown", "remaining": None, "unit": None, "unlimited": False,
            "limit": None, "used": None, "windows": [],
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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


def proxy(req: dict, api_kind: str, alias: str) -> dict:
    return AntigravityProvider.from_env().proxy(req, api_kind, alias)


def stream(req: dict, api_kind: str, alias: str) -> Iterator[Tuple[Optional[str], Any]]:
    yield from AntigravityProvider.from_env().stream(req, api_kind, alias)
