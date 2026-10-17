"""Authenticated provider routing around the preserved Gemini Web handler."""
import hmac
import itertools
import json
import os
import re
import socket
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .config import CONFIG
from .gemini import log
from .models import MODELS
from .server import GeminiHandler
from .providers import ProviderError, list_models, resolve_route
from .api_formats import to_native, format_result, stream_events
from . import antigravity
from . import aistudio_http
from .quota_dashboard import render_dashboard


def web_enabled():
    return os.environ.get("MIXER_WEB_ENABLED", "true").lower() == "true"


def web_models():
    if not web_enabled():
        return []
    return [dict(id=name, object="model", created=1700000000, owned_by="gemini-web",
                 description=cfg["desc"], source="web", transport="gemini-web-private-rpc",
                 model_identity="unverified-web-preset",
                 capabilities={"text": True, "image_input": True,
                               "tools": "prompt-emulation", "thinking_efforts": [],
                               "streaming": "legacy-provider-dependent"})
            for name, cfg in MODELS.items()]


class MixerHandler(GeminiHandler):
    """New providers never fall back to Web or forward client credentials."""

    def setup(self):
        super().setup()
        self.connection.settimeout(float(os.environ.get("MIXER_CLIENT_TIMEOUT_SEC", "60")))
        self._metadata = None
        self._sse_started = False
        self._request_id = uuid.uuid4().hex
        self._stream_api = None
        self._response_id = None
        self._sequence = 0

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler's request line contains query API keys.
        path = urlsplit(self.path).path
        safe_path = path if re.fullmatch(r"/[A-Za-z0-9_./:@=-]*", path) else "/invalid"
        log("http method=%s path=%s request_id=%s" %
            (self.command, safe_path, self._request_id))

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return (os.environ.get("MIXER_ALLOW_UNAUTHENTICATED", "").lower() == "true"
                    and self.client_address[0] in ("127.0.0.1", "::1"))
        candidates = [self.headers.get("x-api-key", ""), self.headers.get("x-goog-api-key", "")]
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(auth[7:])
        candidates.extend(parse_qs(urlsplit(self.path).query).get("key", []))
        return any(hmac.compare_digest(candidate.encode(), str(key).encode())
                   for candidate in candidates if candidate for key in keys)

    def _source_headers(self):
        self.send_header("X-Request-ID", self._request_id)
        if self._metadata:
            self.send_header("X-Mixer-Source", self._metadata["source"])
            self.send_header("X-Mixer-Transport", self._metadata["transport"])
            effort = self._metadata.get("requested_effort")
            if effort:
                self.send_header("X-Mixer-Requested-Effort", effort)

    def send_json(self, data, status=200):
        if self._sse_started:
            raise RuntimeError("JSON cannot follow SSE headers")
        if status >= 500 and (self._metadata or {}).get("source") == "web" and isinstance(data, dict):
            # Legacy transports may include full header values in exception strings.
            raw_error = data.get("error", {})
            raw_message = raw_error.get("message", "") if isinstance(raw_error, dict) else ""
            code = re.search(r"BardErrorInfo \[(\d{1,6})\]", str(raw_message))
            message = "Gemini Web upstream request failed"
            if code:
                message += " (BardErrorInfo [" + code[1] + "])"
            data = {"error": {"message": message, "code": "web_upstream_error"}}
        if isinstance(data, dict) and self._metadata:
            existing = data.get("x_mixer") if isinstance(data.get("x_mixer"), dict) else {}
            data = dict(data, x_mixer=dict(existing, **self._metadata))
        encoded = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self._source_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._source_headers()
        self.end_headers()
        self._sse_started = True

    def _emit(self, event, payload):
        if isinstance(payload, dict):
            if self._metadata:
                existing = payload.get("x_mixer") if isinstance(payload.get("x_mixer"), dict) else {}
                payload = dict(payload, x_mixer=dict(existing, **self._metadata))
            self._sequence = max(self._sequence, payload.get("sequence_number", 0))
            response = payload.get("response")
            if isinstance(response, dict) and isinstance(response.get("id"), str):
                self._response_id = response["id"]
        if event:
            self.wfile.write(("event: " + event + "\n").encode())
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write(("data: " + data + "\n\n").encode())
        self.wfile.flush()

    def _read_request_body(self):
        limit = int(os.environ.get("MIXER_MAX_REQUEST_BYTES", str(20 * 1024 * 1024)))
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        length = self.headers.get("Content-Length")
        if transfer and (transfer != "chunked" or length is not None):
            raise ProviderError(400, "invalid_request", "Ambiguous or unsupported body framing")
        if transfer:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(128)
                if not line.endswith(b"\r\n"):
                    raise ProviderError(400, "invalid_request", "Invalid chunk header")
                try:
                    size = int(line.split(b";", 1)[0], 16)
                except ValueError:
                    raise ProviderError(400, "invalid_request", "Invalid chunk size")
                if size < 0 or total + size > limit:
                    raise ProviderError(413, "request_too_large", "Request body exceeds limit")
                if not size:
                    # Trailers are unnecessary here; refusing them bounds their size.
                    if self.rfile.readline(3) != b"\r\n":
                        raise ProviderError(400, "invalid_request", "Request trailers are unsupported")
                    return b"".join(chunks)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ProviderError(400, "invalid_request", "Truncated chunk")
                chunks.append(chunk)
                total += size
        try:
            size = int(length or 0)
        except ValueError:
            raise ProviderError(400, "invalid_request", "Invalid Content-Length")
        if size < 0 or size > limit:
            raise ProviderError(413, "request_too_large", "Request body exceeds limit")
        body = self.rfile.read(size)
        if len(body) != size:
            raise ProviderError(400, "invalid_request", "Truncated request body")
        return body

    def _fail(self, error):
        log("provider_error request_id=%s source=%s code=%s status=%s" %
            (self._request_id, (self._metadata or {}).get("source", "unresolved"),
             error.code, error.status))
        payload = {"error": {"message": error.message, "type": error.code, "code": error.code}}
        if self._sse_started:
            if self._stream_api == "responses":
                self._emit("response.failed", {"type": "response.failed", "sequence_number": self._sequence + 1,
                    "response": {"id": self._response_id or "resp_" + self._request_id,
                        "object": "response", "status": "failed", "output": [],
                        "error": {"code": error.code, "message": error.message},
                        "x_mixer": self._metadata}})
            else:
                # No success terminator follows a failed stream.
                self._emit("error", dict(payload, request_id=self._request_id))
        else:
            self.send_json(payload, error.status)

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/quota":
                # The public shell contains no account data or embedded key.
                page, policy = render_dashboard()
                encoded = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", policy)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(encoded)
                return
            if path in ("/", "/healthz"):
                self.send_json({"status": "ok", "version": __version__})
                return
            if not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            if path in ("/v1/models", "/v1beta/models"):
                models = web_models() + list_models() + antigravity.list_models()
                if path == "/v1/models":
                    self.send_json({"object": "list", "data": models})
                else:
                    self.send_json({"models": [dict(m, name="models/" + m["id"],
                        displayName=m["id"], supportedGenerationMethods=(
                            [] if m.get("source") == "antigravity" else
                            ["generateContent"] + (["streamGenerateContent"] if
                                m.get("transport") != aistudio_http.TRANSPORT or
                                m.get("capabilities", {}).get("streaming") is True else []))) for m in models]})
            elif path in ("/v1/quota", "/v1/quota/antigravity"):
                self.send_json(antigravity.quota())
            else:
                self.send_json({"error": {"message": "not found"}}, 404)
        except ProviderError as exc:
            self._fail(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._fail(ProviderError(502, "provider_error", "Provider operation failed"))

    def do_POST(self):
        try:
            if not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            path = urlsplit(self.path).path
            native = re.fullmatch(r"/(?:v1beta|v1)/models/([^/]+):(generateContent|streamGenerateContent)", path)
            if path not in ("/v1/chat/completions", "/v1/responses") and not native:
                self.send_json({"error": {"message": "not found"}}, 404)
                return
            body = self._read_request_body()
            try:
                req = json.loads(body)
            except (ValueError, UnicodeError):
                raise ProviderError(400, "invalid_json", "Invalid JSON body")
            if not isinstance(req, dict):
                raise ProviderError(400, "invalid_request", "Body must be a JSON object")
            kind = "gemini" if native else ("chat" if path.endswith("completions") else "responses")
            self._stream_api = kind
            alias = unquote(native[1]) if native else req.get("model", CONFIG["default_model"])
            if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_.:/@=-]{1,200}", alias):
                raise ProviderError(400, "invalid_model", "Invalid model name")
            stream = native[2] == "streamGenerateContent" if native else req.get("stream", False)
            if not isinstance(stream, bool):
                raise ProviderError(400, "invalid_request", "stream must be a boolean")
            if alias.startswith("ag-"):
                if kind == "gemini":
                    raise ProviderError(501, "unsupported_api", "Antigravity adapter supports Chat Completions and Responses only")
                self._metadata = {"source": "antigravity", "transport": "sub2api-openai",
                                  "request_id": self._request_id}
                self._metadata.update(antigravity.request_metadata(req, alias))
                self._route_log(alias, stream)
                if stream:
                    self._serve_events(antigravity.stream(req, kind, alias))
                else:
                    self.send_json(antigravity.proxy(req, kind, alias))
                return
            reasoning = req.get("reasoning", {})
            if reasoning is not None and not isinstance(reasoning, dict):
                raise ProviderError(400, "invalid_request", "reasoning must be an object")
            first_effort, second_effort = req.get("reasoning_effort"), (reasoning or {}).get("effort")
            if first_effort is not None and second_effort is not None and first_effort != second_effort:
                raise ProviderError(400, "thinking_conflict", "reasoning_effort and reasoning.effort conflict")
            effort = first_effort if first_effort is not None else second_effort
            if alias.startswith(("build-", "ais-")):
                source = alias.split("-", 1)[0]
                self._metadata = {"source": source,
                                  "transport": "google-gemini-public-api", "request_id": self._request_id,
                                  "profile_kind": "logical-public-api-profile", "webpage_executor": False}
                if aistudio_http.transport_mode(source) == aistudio_http.TRANSPORT:
                    self._metadata = dict(aistudio_http.initial_metadata(source), request_id=self._request_id)
            route = resolve_route(alias, effort=effort)
            if route is None:
                if not web_enabled():
                    raise ProviderError(503, "provider_disabled", "Gemini Web provider is disabled")
                if effort is not None or alias.rsplit("-", 1)[-1] in ("low", "medium", "high"):
                    raise ProviderError(400, "unsupported_thinking", "Legacy Web cannot verify low/medium/high thinking levels")
                self._validate_legacy_images(req)
                self._metadata = {"source": "web", "transport": "gemini-web-private-rpc",
                                  "model_identity": "unverified-web-preset", "request_id": self._request_id}
                self._route_log(alias, stream)
                # The old parsing/generation behavior remains isolated here.
                old_path = self.path
                self.path = ("/v1beta/models/" + alias + ":" + native[2]) if native else path
                try:
                    if kind == "chat":
                        super()._handle_chat(body)
                    elif kind == "responses":
                        super()._handle_responses(body)
                    else:
                        super()._handle_google_generate(body, stream)
                finally:
                    self.path = old_path
                return
            self._metadata = dict(route.meta, request_id=self._request_id)
            request = to_native(req, kind)
            if hasattr(route, "request_metadata"):
                self._metadata = dict(route.request_metadata(request), request_id=self._request_id)
            self._route_log(alias, stream)
            if stream:
                chunks = route.stream(request)
                try:
                    first = next(chunks)
                    self._metadata = dict(route.meta, request_id=self._request_id)
                    events = stream_events(itertools.chain([first], chunks), alias, self._metadata, kind)
                    self._serve_events(events)
                finally:
                    close = getattr(chunks, "close", None)
                    if close:
                        close()
            else:
                native_result = route.generate(request)
                self._metadata = dict(route.meta, request_id=self._request_id)
                result = format_result(native_result, alias, self._metadata, kind)
                self.send_json(result)
        except StopIteration:
            self._fail(ProviderError(502, "empty_upstream", "Upstream returned an empty stream"))
        except ProviderError as exc:
            self._fail(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (TimeoutError, socket.timeout):
            self._fail(ProviderError(408, "request_timeout", "Client request timed out"))
        except Exception:
            # Upstream exception strings can contain URLs, keys and raw content.
            self._fail(ProviderError(502, "upstream_error", "Provider request failed"))

    def _route_log(self, alias, stream):
        log("route request_id=%s source=%s transport=%s model=%s upstream_model=%s effective_upstream_model=%s effort=%s stream=%s upstream_channel=%s thinking_level=%s thinking_budget=%s" %
            (self._request_id, self._metadata["source"], self._metadata["transport"], alias,
             self._metadata.get("upstream_model") or "unknown",
             self._metadata.get("effective_upstream_model", self._metadata.get("upstream_model")) or "unknown",
             self._metadata.get("requested_effort") or "default", stream,
             self._metadata.get("upstream_channel") or "default",
             self._metadata.get("effective_thinking_level") or "default",
             self._metadata.get("effective_thinking_budget", "default")))

    def _stream_error(self, error):
        self._fail(ProviderError(502, "web_upstream_error", "Gemini Web stream failed"))

    def _validate_legacy_images(self, value):
        if isinstance(value, list):
            for item in value:
                self._validate_legacy_images(item)
        elif isinstance(value, dict):
            if value.get("type") in ("image_url", "input_image"):
                image = value.get("image_url", "")
                url = image.get("url", "") if isinstance(image, dict) else image
                if (isinstance(url, str) and not url.startswith("data:")
                        and os.environ.get("MIXER_WEB_ALLOW_REMOTE_IMAGES", "false").lower() != "true"):
                    raise ProviderError(400, "unsupported_image_url", "Use an inline image data URL; legacy remote image fetching is disabled by default")
            if "fileData" in value or "file_data" in value:
                raise ProviderError(400, "unsupported_file_input", "Legacy Web does not support native fileData")
            for item in value.values():
                if isinstance(item, (list, dict)):
                    self._validate_legacy_images(item)

    def _serve_events(self, events):
        try:
            # Validate the first formatted event before committing HTTP 200.
            iterator = iter(events)
            first = next(iterator)
            self._start_sse()
            for event, data in itertools.chain([first], iterator):
                self._emit(event, data)
        finally:
            close = getattr(events, "close", None)
            if close:
                close()
