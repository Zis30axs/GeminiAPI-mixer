"""Local, streaming account pool for isolated gemini-web2api workers.

The workers own all Google credentials.  This gateway reads only its API keys
and a list of loopback worker addresses, and never rewrites model names.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import httpx


MAX_BODY_BYTES = 32 * 1024 * 1024
RETRY_STATUSES = frozenset({429, 502, 503, 504})
HOP_HEADERS = frozenset({
    "connection", "proxy-connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
})
ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
GENERATION_PATH = re.compile(
    r"/v1(?:beta)?/models/[^/]+:(?:generateContent|streamGenerateContent)\Z"
)
CLIENT_THINKING_FIELDS = frozenset({
    "reasoning", "reasoning_effort", "thinking", "thinking_config", "thinkingConfig",
    "thinking_level", "thinkingLevel", "thinking_budget", "thinkingBudget",
    "include_thoughts", "includeThoughts", "enable_thinking", "enableThinking",
})


def _without_client_thinking(path: str, body: bytes) -> bytes:
    """Remove client controls at API option locations, never inside user content."""
    if path not in ("/v1/chat/completions", "/v1/responses") and not GENERATION_PATH.fullmatch(path):
        return body
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        # Parsing and validation errors belong to the worker's normal API handler.
        return body
    if not isinstance(payload, dict):
        return body
    locations = [payload]
    for parent in (payload, payload.get("extra_body")):
        if not isinstance(parent, dict):
            continue
        if parent is not payload:
            locations.append(parent)
        for name in ("generationConfig", "generation_config"):
            config = parent.get(name)
            if isinstance(config, dict):
                locations.append(config)
    changed = False
    for options in locations:
        for name in CLIENT_THINKING_FIELDS:
            if name in options:
                del options[name]
                changed = True
    if not changed:
        return body
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} is outside its allowed range")
    return result


def _port(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not (0 if allow_zero else 1) <= value <= 65535:
        raise ValueError(f"{name} is outside its allowed range")
    return value


def _backend_url(value: Any) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError("backend url must be a string")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        loopback = host == "localhost" or (
            host is not None and ipaddress.ip_address(host).is_loopback
        )
    except ValueError:
        raise ValueError("backend url must use a loopback IP or localhost") from None
    if (parsed.scheme != "http" or not loopback or port is None
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("backend url must be an HTTP loopback origin with an explicit port")
    _port(port, "backend port")
    # Normalize localhost to a numeric address to avoid consulting DNS later.
    normalized_host = "127.0.0.1" if host == "localhost" else host
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    return f"http://{normalized_host}:{port}", port


@dataclass
class Backend:
    id: str
    url: str
    inflight: int = 0
    cooldown_until: float = 0.0
    served: int = 0
    failures: int = 0


class AccountPool:
    def __init__(self, pool: dict[str, Any], gateway_port: int):
        if not isinstance(pool, dict):
            raise ValueError("pool configuration must be an object")
        self.require_api_key = pool.get("require_api_key", True)
        if not isinstance(self.require_api_key, bool):
            raise ValueError("require_api_key must be a boolean")
        entries = pool.get("backends")
        if not isinstance(entries, list) or not entries:
            raise ValueError("pool backends must be a nonempty list")
        self.backends: list[Backend] = []
        ids: set[str] = set()
        ports: set[int] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("each backend must be an object")
            account_id = entry.get("id")
            if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
                raise ValueError("backend id is invalid")
            url, port = _backend_url(entry.get("url"))
            if account_id in ids or port in ports:
                raise ValueError("backend ids and ports must be unique")
            if gateway_port and port == gateway_port:
                raise ValueError("a backend cannot use the gateway port")
            ids.add(account_id)
            ports.add(port)
            self.backends.append(Backend(account_id, url))
        limit = pool.get("max_concurrent_per_account", 1)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("max_concurrent_per_account must be a positive integer")
        self.max_concurrent_per_account = limit
        self.queue_timeout_sec = _number(pool.get("queue_timeout_sec", 30), "queue_timeout_sec")
        self.failure_cooldown_sec = _number(pool.get("failure_cooldown_sec", 15), "failure_cooldown_sec")
        self.request_timeout_sec = _number(
            pool.get("request_timeout_sec", 180), "request_timeout_sec", minimum=0.001,
        )
        self._condition = threading.Condition()
        self._cursor = 0

    def acquire(self, excluded: set[str]) -> Backend | None:
        deadline = time.monotonic() + self.queue_timeout_sec
        with self._condition:
            while True:
                now = time.monotonic()
                eligible = [b for b in self.backends if b.id not in excluded]
                if not eligible:
                    return None
                available = [b for b in eligible if b.inflight < self.max_concurrent_per_account
                             and b.cooldown_until <= now]
                if available:
                    minimum = min(b.inflight for b in available)
                    for offset in range(len(self.backends)):
                        index = (self._cursor + offset) % len(self.backends)
                        backend = self.backends[index]
                        if backend in available and backend.inflight == minimum:
                            backend.inflight += 1
                            self._cursor = (index + 1) % len(self.backends)
                            return backend
                remaining = deadline - now
                if remaining <= 0:
                    return None
                cooldowns = [b.cooldown_until - now for b in eligible
                             if b.inflight < self.max_concurrent_per_account
                             and b.cooldown_until > now]
                self._condition.wait(min(remaining, min(cooldowns)) if cooldowns else remaining)

    def release(self, backend: Backend, *, failed: bool, served: bool) -> None:
        with self._condition:
            backend.inflight -= 1
            if failed:
                backend.failures += 1
                backend.cooldown_until = time.monotonic() + self.failure_cooldown_sec
            if served:
                backend.served += 1
            self._condition.notify_all()

    def status(self) -> list[dict[str, Any]]:
        with self._condition:
            now = time.monotonic()
            return [{
                "id": b.id,
                "inflight": b.inflight,
                "cooldown_remaining_sec": round(max(0.0, b.cooldown_until - now), 3),
                "served": b.served,
                "failures": b.failures,
            } for b in self.backends]


class RequestBodyError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class PoolHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], pool: AccountPool, api_keys: tuple[bytes, ...]):
        self.pool = pool
        self.api_keys = api_keys
        self.client = httpx.Client(
            timeout=httpx.Timeout(pool.request_timeout_sec),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=16),
        )
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        try:
            super().__init__(address, PoolHandler)
        except BaseException:
            self.client.close()
            raise

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.client.close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Never print an exception that may contain an authenticated request URL.
        print("Account pool: a client request ended unexpectedly.", flush=True)


class PoolHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GeminiAccountPool/1.0"
    sys_version = ""
    server: PoolHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format: str, *args: Any) -> None:
        # The standard access log includes the query string, which may hold a key.
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, x-api-key, x-goog-api-key")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": "account_pool_error", "code": code}})

    def _authorized(self, query: str) -> bool:
        if not self.server.pool.require_api_key:
            return True
        candidates: list[str] = []
        authorization = self.headers.get("Authorization", "")
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer":
            candidates.append(credential.strip())
        for name in ("x-api-key", "x-goog-api-key"):
            candidates.extend(self.headers.get_all(name, []))
        candidates.extend(parse_qs(query, max_num_fields=256).get("key", []))
        return any(hmac.compare_digest(candidate.encode("utf-8"), key)
                   for candidate in candidates if len(candidate) <= 4096
                   for key in self.server.api_keys)

    def _read_exact(self, length: int) -> bytes:
        data = self.rfile.read(length)
        if len(data) != length:
            raise RequestBodyError(400, "The request body ended prematurely.")
        return data

    def _body(self) -> bytes:
        lengths = self.headers.get_all("Content-Length", [])
        transfers = self.headers.get_all("Transfer-Encoding", [])
        if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
            raise RequestBodyError(400, "Ambiguous request body framing.")
        if transfers:
            if transfers[0].strip().lower() != "chunked":
                raise RequestBodyError(400, "Only chunked transfer encoding is supported.")
            chunks = bytearray()
            while True:
                line = self.rfile.readline(8193)
                if len(line) > 8192 or not line.endswith(b"\r\n"):
                    raise RequestBodyError(400, "Invalid chunk framing.")
                size_text = line[:-2].split(b";", 1)[0].strip()
                if not re.fullmatch(rb"[0-9a-fA-F]{1,16}", size_text):
                    raise RequestBodyError(400, "Invalid chunk size.")
                size = int(size_text, 16)
                if size == 0:
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(8193)
                        trailer_bytes += len(trailer)
                        if len(trailer) > 8192 or trailer_bytes > 32768 or not trailer.endswith(b"\r\n"):
                            raise RequestBodyError(400, "Invalid chunk trailers.")
                        if trailer == b"\r\n":
                            return bytes(chunks)
                if len(chunks) + size > MAX_BODY_BYTES:
                    raise RequestBodyError(413, "The request body exceeds 32 MiB.")
                chunks.extend(self._read_exact(size))
                if self._read_exact(2) != b"\r\n":
                    raise RequestBodyError(400, "Invalid chunk terminator.")
        if not lengths:
            return b""
        length_text = lengths[0].strip()
        if not re.fullmatch(r"[0-9]{1,20}", length_text):
            raise RequestBodyError(400, "Invalid Content-Length.")
        length = int(length_text)
        if length > MAX_BODY_BYTES:
            raise RequestBodyError(413, "The request body exceeds 32 MiB.")
        return self._read_exact(length)

    @staticmethod
    def _connection_headers(headers: Any) -> set[str]:
        blocked = set(HOP_HEADERS)
        for value in headers.get_all("Connection", []) if hasattr(headers, "get_all") else headers.get_list("Connection"):
            blocked.update(token.strip().lower() for token in value.split(","))
        return blocked

    def _upstream_headers(self) -> list[tuple[str, str]]:
        blocked = self._connection_headers(self.headers) | {
            "host", "content-length", "expect", "authorization", "x-api-key", "x-goog-api-key",
        }
        headers = [(key, value) for key, value in self.headers.items() if key.lower() not in blocked]
        headers.append(("Authorization", "Bearer " + self.server.api_keys[0].decode("utf-8")))
        return headers

    def _relay(self, response: httpx.Response) -> None:
        self.close_connection = True
        self.send_response(response.status_code)
        blocked = self._connection_headers(response.headers) | {"server", "date", "content-length"}
        for key, value in response.headers.multi_items():
            if key.lower() not in blocked and not key.lower().startswith("access-control-"):
                self.send_header(key, value)
        lengths = response.headers.get_list("Content-Length")
        if len(lengths) == 1 and re.fullmatch(r"[0-9]+", lengths[0]):
            self.send_header("Content-Length", lengths[0])
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            for chunk in response.iter_raw():
                self.wfile.write(chunk)
                self.wfile.flush()

    def _proxy(self, body: bytes) -> None:
        attempted: set[str] = set()
        headers = self._upstream_headers()
        target = urlsplit(self.path)
        body = _without_client_thinking(target.path, body)
        # Workers log their request target and some routes compare exact paths.
        # Authenticate them by header, keeping API keys out of their URLs.
        query = urlencode([(key, value) for key, value in
                           parse_qsl(target.query, keep_blank_values=True, max_num_fields=256)
                           if key != "key"])
        backend_target = target.path + ("?" + query if query else "")
        last_status = 502
        while len(attempted) < len(self.server.pool.backends):
            backend = self.server.pool.acquire(attempted)
            if backend is None:
                self._error(503, "pool_busy", "All available accounts are busy or cooling down.")
                return
            attempted.add(backend.id)
            failed = False
            served = False
            committed = False
            try:
                with self.server.client.stream(
                    self.command, backend.url + backend_target, headers=headers, content=body,
                ) as response:
                    if response.status_code in RETRY_STATUSES:
                        failed = True
                        last_status = response.status_code
                        if len(attempted) < len(self.server.pool.backends):
                            continue
                    # Mark committed before any response bytes can reach the client.
                    committed = True
                    self._relay(response)
                    served = not failed
                    return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                # A disconnected client does not indicate a broken account.
                return
            except httpx.HTTPError as exc:
                failed = True
                last_status = 504 if isinstance(exc, httpx.TimeoutException) else 502
                if committed:
                    # A partial SSE answer must never be replayed on another account.
                    self.close_connection = True
                    return
            finally:
                self.server.pool.release(backend, failed=failed, served=served)
        self._error(last_status, "upstream_unavailable", "No account could complete the upstream request.")

    def _handle(self) -> None:
        self.close_connection = True
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment or not target.path.startswith("/"):
                self._error(400, "invalid_target", "An origin-form request target is required.")
                return
            if self.command == "OPTIONS":
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if target.path == "/" and self.command in ("GET", "HEAD"):
                self._json(200, {"status": "ok", "mode": "account_pool", "accounts": len(self.server.pool.backends)})
                return
            if not self._authorized(target.query):
                self._error(401, "invalid_api_key", "A valid API key is required.")
                return
            if target.path == "/pool/status" and self.command in ("GET", "HEAD"):
                self._json(200, {
                    "status": "ok", "mode": "account_pool",
                    "max_concurrent_per_account": self.server.pool.max_concurrent_per_account,
                    "backends": self.server.pool.status(),
                })
                return
            if not (target.path in ("/v1", "/v1beta") or target.path.startswith(("/v1/", "/v1beta/"))):
                self._error(404, "not_found", "This endpoint does not exist.")
                return
            self._proxy(self._body())
        except RequestBodyError as exc:
            self._error(exc.status, "invalid_request_body", str(exc))
        except (ValueError, UnicodeError):
            self._error(400, "invalid_request", "The request target or API key is invalid.")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            self.close_connection = True

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle


def make_server(config: dict[str, Any], pool: dict[str, Any]) -> PoolHTTPServer:
    """Validate configuration and bind a gateway; caller controls its lifecycle."""
    if not isinstance(config, dict):
        raise ValueError("gateway configuration must be an object")
    host = config.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise ValueError("gateway host is invalid")
    port = _port(config.get("port", 8081), "gateway port", allow_zero=True)
    keys = config.get("api_keys")
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys):
        raise ValueError("gateway api_keys must be a nonempty list of strings")
    account_pool = AccountPool(pool, port)
    return PoolHTTPServer((host, port), account_pool, tuple(key.encode("utf-8") for key in keys))


def main() -> None:
    parser = argparse.ArgumentParser(description="Load balance isolated local Gemini account workers.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--pool", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        pool = json.loads(args.pool.read_text(encoding="utf-8-sig"))
        server = make_server(config, pool)
    except (OSError, ValueError, TypeError):
        parser.exit(2, "Account pool configuration is invalid or unavailable.\n")
    print(f"Account pool started on port {server.server_port} with {len(server.pool.backends)} accounts.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
