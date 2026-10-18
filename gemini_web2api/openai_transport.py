"""Bounded OpenAI-compatible transport shared by independent provider profiles.

Subclasses own configuration, model selection and safe response metadata. No
client credentials, redirects, ambient proxy settings, or retry are forwarded.
"""
from __future__ import annotations

import codecs
import copy
import ipaddress
import json
import math
import re
import threading
import time
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import httpx
from .providers import ProviderError


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


def _limit(env: Mapping[str, str], name: str, default: float, low: float, high: float, provider_name: str = "Antigravity") -> float:
    try:
        value = float(env.get(name, str(default)))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ProviderError(500, "invalid_provider_configuration", f"Invalid {provider_name} transport limit configuration.") from None


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _base_url(value: str, allow_http: bool, prefix: str = "ANTIGRAVITY") -> str:
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
            f"{prefix}_BASE_URL must be a clean HTTPS origin/base without /v1; "
            f"non-loopback HTTP requires {prefix}_ALLOW_HTTP=true.",
        ) from None


class BoundedOpenAITransport:
    provider_name = "OpenAI-compatible"

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(504, "upstream_timeout", f"{self.provider_name} upstream deadline exceeded.")
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


    def _check_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if not 200 <= status < 300:
            if 300 <= status < 400:
                raise ProviderError(502, "upstream_redirect_rejected", f"{self.provider_name} upstream redirects are disabled.")
            raise ProviderError(
                status if 400 <= status < 500 else 502,
                "upstream_rejected_request" if status < 500 else "upstream_error",
                f"{self.provider_name} upstream returned HTTP %d." % status,
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
                            raise ProviderError(502, "upstream_response_too_large", f"{self.provider_name} upstream response exceeds the configured limit.")
                    self._remaining(deadline)
                    result = json.loads(content, parse_constant=_invalid_json_constant)
                    if not isinstance(result, dict):
                        raise ValueError
                    if result.get("error") is not None:
                        raise ProviderError(502, "upstream_error", f"{self.provider_name} upstream returned an error.")
                    return result
        except httpx.TimeoutException:
            raise ProviderError(504, "upstream_timeout", f"{self.provider_name} upstream timed out.") from None
        except (httpx.HTTPError, RuntimeError):
            self._remaining(deadline)
            raise ProviderError(502, "upstream_connection_error", f"{self.provider_name} upstream connection failed.") from None
        except (ValueError, UnicodeError):
            raise ProviderError(502, "invalid_upstream_response", f"{self.provider_name} upstream returned invalid JSON.") from None


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
            raise ProviderError(502, "invalid_upstream_response", f"{self.provider_name} upstream models response is invalid.")
        available = {
            row["id"] for row in rows[:4096]
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] in self._models
        }
        self._available = available
        self._available_until = time.monotonic() + 30.0
        return available


    def proxy(self, req: dict, api_kind: str, alias: str) -> dict:
        deadline = time.monotonic() + self.total_timeout
        path, body, model = self._prepare(req, api_kind, alias, deadline, False)
        return self._decorate(self._read_json("POST", path, body, deadline), alias, model)


    def _sse_event(self, event: Optional[str], data: list, alias: str, model: str) -> Optional[Tuple[Optional[str], Any]]:
        if not data:
            return None
        if event is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", event):
            raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream SSE event name is invalid.")
        raw = "\n".join(data)
        if raw == "[DONE]":
            return event, "[DONE]"
        try:
            payload = json.loads(raw, parse_constant=_invalid_json_constant)
        except (ValueError, UnicodeError):
            raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream returned an invalid SSE event.") from None
        if not isinstance(payload, dict):
            raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream SSE payload must be an object.")
        if (
            event in ("error", "response.failed") or payload.get("error") is not None
            or payload.get("type") in ("error", "response.failed")
            or (isinstance(payload.get("response"), dict) and payload["response"].get("error"))
        ):
            raise ProviderError(502, "upstream_stream_error", f"{self.provider_name} upstream stream failed.")
        return event, self._decorate(payload, alias, model)


    @staticmethod
    def _responses_terminal(event: Optional[str], payload: Any) -> bool:
        terminal_events = ("response.completed", "response.incomplete", "response.cancelled")
        return event in terminal_events or (isinstance(payload, dict) and payload.get("type") in terminal_events)


    @staticmethod
    def _terminal(event: Optional[str], payload: Any) -> bool:
        if payload == "[DONE]":
            return True
        if BoundedOpenAITransport._responses_terminal(event, payload):
            return True
        if isinstance(payload, dict):
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
                        raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream did not return SSE.")
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
                            raise ProviderError(502, "upstream_response_too_large", f"{self.provider_name} upstream stream exceeds the configured limit.")
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
                                raise ProviderError(502, "upstream_event_too_large", f"{self.provider_name} upstream SSE event exceeds the configured limit.")
                            if not line:
                                item = self._sse_event(event, data, alias, model)
                                if item is not None:
                                    if item[1] == "[DONE]" and not saw_data:
                                        raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream returned an empty SSE stream.")
                                    saw_data = True
                                    terminal = terminal or self._terminal(*item)
                                    yield item
                                    # Responses terminal events finish the protocol
                                    # even if the peer keeps the socket alive. Chat
                                    # finish_reason may precede a final usage chunk.
                                    if item[1] == "[DONE]" or (path == "/v1/responses" and self._responses_terminal(*item)):
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
                                        raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream SSE event name is invalid.")
                                    event = value
                        if event_size + len(buffer.encode("utf-8")) > self.max_event_bytes:
                            raise ProviderError(502, "upstream_event_too_large", f"{self.provider_name} upstream SSE event exceeds the configured limit.")
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
                        raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream returned an empty SSE stream.")
                    if not terminal:
                        raise ProviderError(502, "upstream_stream_truncated", f"{self.provider_name} upstream stream ended without a terminal event.")
        except httpx.TimeoutException:
            raise ProviderError(504, "upstream_timeout", f"{self.provider_name} upstream timed out.") from None
        except (httpx.HTTPError, RuntimeError):
            self._remaining(deadline)
            raise ProviderError(502, "upstream_connection_error", f"{self.provider_name} upstream connection failed.") from None
        except UnicodeError:
            raise ProviderError(502, "invalid_upstream_stream", f"{self.provider_name} upstream stream is not valid UTF-8.") from None
