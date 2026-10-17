"""Explicit, bounded live acceptance probe for the private AI Studio backend.

No session files are read. Credentials come only from AISTUDIO_HTTP_API_KEY.
The output contains status, timing, and counts, never generated text or errors
from the backend. There are no retries and no thinking/tool/image claims.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


CHANNELS = {"ais": "playground", "build": "build"}
MODEL_RE = re.compile(r"[a-z][a-z0-9]*(?:[-.][a-z0-9]+)*\Z")
PROMPT = "Reply with exactly twelve numbered lines, from 1 to 12. Each line must say: <number>. ready"
BODY = {"contents": [{"role": "user", "parts": [{"text": PROMPT}]}]}
MAX_RESPONSE = 1024 * 1024
MAX_EVENT = 128 * 1024
STOP_CODES = frozenset({"upstream_authentication_failed", "upstream_rate_limited"})


class ProbeError(Exception):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


def valid_base(value, allow_http):
    try:
        parsed = urlsplit(value)
        parsed.port
        if (len(value) > 2048 or parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or any(char in value for char in ("?", "#", "\\", "%"))
                or re.search(r"[\s\x00-\x1f\x7f]", value)
                or any(part in (".", "..") for part in parsed.path.split("/"))
                or parsed.path.rstrip("/").endswith(("/v1", "/v1beta"))
                or parsed.scheme == "http" and not allow_http):
            raise ValueError
        return value.rstrip("/")
    except (TypeError, ValueError, AttributeError):
        raise ProbeError("invalid_backend_configuration") from None


def config(environ):
    key = environ.get("AISTUDIO_HTTP_API_KEY", "").strip()
    if not key or len(key) > 4096 or not key.isascii() or re.search(r"[\x00-\x20\x7f]", key):
        raise ProbeError("missing_or_invalid_service_key")
    allow_http = environ.get("AISTUDIO_HTTP_ALLOW_HTTP", "false").lower() == "true"
    return valid_base(environ.get("AISTUDIO_HTTP_BASE_URL", "").strip(), allow_http), key, allow_http


def decode(data):
    def invalid_constant(_value):
        raise ValueError
    try:
        value = json.loads(data, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProbeError("invalid_json_response") from None
    if "error" in value:
        error = value["error"]
        status = error.get("code") if isinstance(error, dict) else None
        raise status_error(status if type(status) is int else 502)
    return value


def status_error(status):
    if status in (401, 403):
        return ProbeError("upstream_authentication_failed", status)
    if status == 429:
        return ProbeError("upstream_rate_limited", status)
    if status in (408, 504):
        return ProbeError("upstream_timeout", status)
    if 300 <= status < 400:
        return ProbeError("upstream_redirect_rejected", status)
    if status == 404:
        return ProbeError("upstream_model_not_found", status)
    return ProbeError("upstream_request_failed", status)


def check_response(response, channel):
    if response.status_code != 200:
        raise status_error(response.status_code)
    if (response.headers.get("X-Mixer-Upstream-Channel") != channel
            or response.headers.get("X-Mixer-Channel-Locked") != "1"):
        raise ProbeError("upstream_channel_not_locked", response.status_code)


def answer(value, require_terminal=False):
    """Accept only plain visible text and a genuine STOP completion status."""
    feedback = value.get("promptFeedback", {})
    if not isinstance(feedback, dict) or feedback.get("blockReason"):
        raise ProbeError("upstream_blocked_or_invalid")
    candidates = value.get("candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 1:
        raise ProbeError("invalid_generation_result")
    if not candidates:
        if require_terminal:
            raise ProbeError("empty_generation_result")
        return "", False
    candidate = candidates[0]
    if not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
        raise ProbeError("invalid_generation_result")
    content = candidate.get("content", {})
    if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
        raise ProbeError("invalid_generation_result")
    texts = []
    for part in content.get("parts", []):
        if (not isinstance(part, dict) or set(part) - {"text", "thought", "thoughtSignature"}
                or "text" in part and not isinstance(part["text"], str)
                or "thought" in part and type(part["thought"]) is not bool):
            raise ProbeError("unexpected_generation_modality")
        if not part.get("thought"):
            texts.append(part.get("text", ""))
    finish = candidate.get("finishReason")
    if finish not in (None, "", "STOP"):
        raise ProbeError("generation_not_completed")
    if require_terminal and finish != "STOP":
        raise ProbeError("missing_terminal_status")
    return "".join(texts), finish == "STOP"


class Deadline:
    def __init__(self, client, seconds):
        self.client, self.end = client, time.monotonic() + seconds
        self.expired = threading.Event()
        self.timer = threading.Timer(seconds, self.expire)
        self.timer.daemon = True

    def expire(self):
        self.expired.set()
        try:
            self.client.close()
        except Exception:
            pass

    def check(self):
        if self.expired.is_set() or time.monotonic() >= self.end:
            raise ProbeError("upstream_timeout")

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *_args):
        self.timer.cancel()


def read_json(response, deadline):
    data = bytearray()
    for chunk in response.iter_bytes():
        deadline.check()
        if len(data) + len(chunk) > MAX_RESPONSE:
            raise ProbeError("response_size_limit")
        data.extend(chunk)
    deadline.check()
    return decode(bytes(data))


def sse_events(response, deadline):
    """Retain the transport-read ordinal to detect wholly buffered output."""
    buffer, event_lines = bytearray(), []
    received, event_size, ordinal = 0, 0, 0
    for chunk in response.iter_bytes():
        deadline.check()
        if not chunk:
            continue
        ordinal += 1
        received += len(chunk)
        if received > MAX_RESPONSE:
            raise ProbeError("response_size_limit")
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, tail = buffer.partition(b"\n")
            buffer = bytearray(tail)
            line = line.rstrip(b"\r")
            if len(line) > MAX_EVENT:
                raise ProbeError("stream_event_size_limit")
            if not line:
                if event_lines:
                    yield b"\n".join(event_lines), ordinal
                    event_lines, event_size = [], 0
            elif line.startswith(b"data:"):
                data = bytes(line[5:])
                if data.startswith(b" "):
                    data = data[1:]
                event_size += len(data)
                if event_size > MAX_EVENT:
                    raise ProbeError("stream_event_size_limit")
                event_lines.append(data)
        if len(buffer) > MAX_EVENT:
            raise ProbeError("stream_event_size_limit")
    deadline.check()
    # An incomplete event at EOF must never turn into evidence of success.
    if buffer or event_lines:
        raise ProbeError("truncated_stream_event")


def check_stream(response, deadline, started):
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
        raise ProbeError("expected_sse_response")
    content_events, text_chars = 0, 0
    first_time, first_read, terminal_time, terminal_read = None, None, None, None
    complete_text = ""
    reads, event_count = 0, 0
    for payload, ordinal in sse_events(response, deadline):
        reads = max(reads, ordinal)
        event_count += 1
        if payload == b"[DONE]":
            if terminal_read is None:
                raise ProbeError("missing_terminal_status")
            continue
        text, terminal = answer(decode(payload))
        if text:
            if terminal_read is not None:
                raise ProbeError("content_after_terminal_status")
            # The native protocol promises deltas. Reject a repeated cumulative
            # transcript rather than counting it as several useful increments.
            if len(complete_text) >= 8 and text.startswith(complete_text):
                raise ProbeError("cumulative_stream_not_deltas")
            complete_text += text
            content_events += 1
            text_chars += len(text)
            if first_time is None:
                first_time, first_read = time.monotonic() - started, ordinal
        if terminal:
            if terminal_read is not None:
                raise ProbeError("duplicate_terminal_status")
            terminal_time, terminal_read = time.monotonic() - started, ordinal
    if terminal_read is None:
        raise ProbeError("missing_terminal_status")
    if content_events < 2 or first_read is None or first_read >= terminal_read:
        raise ProbeError("incremental_delivery_not_observed")
    return {"content_events": content_events, "event_count": event_count, "transport_reads": reads,
            "text_chars": text_chars, "first_text_seconds": round(first_time, 4),
            "terminal_seconds": round(terminal_time, 4), "terminal_status": "STOP",
            "incremental_delivery_observed": True}


def probe(base, key, model, source, stream, total_timeout, client_factory=httpx.Client):
    started = time.monotonic()
    channel = CHANNELS[source]
    method = "streamGenerateContent" if stream else "generateContent"
    headers = {"Authorization": "Bearer " + key, "X-Mixer-Upstream-Channel": channel,
               "Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
    summary = {"ok": False}
    deadline = None
    try:
        with client_factory(timeout=httpx.Timeout(total_timeout, connect=min(10, total_timeout)),
                            follow_redirects=False, trust_env=False, verify=True) as client:
            with Deadline(client, total_timeout) as deadline:
                with client.stream("POST", base + "/v1beta/models/" + model + ":" + method,
                                   params={"alt": "sse"} if stream else None, headers=headers, json=BODY) as response:
                    check_response(response, channel)
                    if stream:
                        details = check_stream(response, deadline, started)
                    else:
                        text, _ = answer(read_json(response, deadline), require_terminal=True)
                        if not text.strip():
                            raise ProbeError("empty_generation_result")
                        details = {"text_chars": len(text), "terminal_status": "STOP"}
                    summary.update(details, ok=True, status=200, channel_locked=True)
    except ProbeError as error:
        summary.update(code=error.code, status=error.status)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
        summary["code"] = ("upstream_timeout" if isinstance(error, httpx.TimeoutException)
                           or deadline is not None and deadline.expired.is_set() else "transport_error")
    except Exception:
        # Never dump raw exceptions or traceback: a transport can include keys.
        summary["code"] = "probe_failed"
    summary["elapsed_seconds"] = round(time.monotonic() - started, 4)
    return summary


def write_evidence(path, base, model, sources):
    if not sources:
        return False
    entries = {source + "/" + model: {"text": True, "streaming": True, "image_input": False,
                                      "tools": False, "parameters": [], "thinking": {}} for source in sources}
    data = {"version": 1, "backend_base_url": base,
            "verified_at": datetime.now(timezone.utc).isoformat(), "models": entries}
    destination = Path(path)
    temporary = None
    try:
        if destination.is_symlink():
            raise OSError
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".aistudio-evidence-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return True
    except OSError:
        raise ProbeError("evidence_write_failed") from None
    finally:
        if temporary and os.path.isfile(temporary):
            os.unlink(temporary)


def run(model, sources, timeout, environ, evidence_output=None, evidence_base_url=None, client_factory=httpx.Client):
    if not MODEL_RE.fullmatch(model) or len(model) > 160 or not 1 <= timeout <= 180:
        raise ProbeError("invalid_probe_arguments")
    if not sources or len(set(sources)) != len(sources) or any(source not in CHANNELS for source in sources):
        raise ProbeError("invalid_probe_arguments")
    base, key, allow_http = config(environ)
    evidence_base = valid_base(evidence_base_url, allow_http) if evidence_base_url else base
    result = {"ok": False, "model": model, "results": [], "evidence_written": False, "evidence_sources": []}
    passed = []
    for source in sources:
        item = {"source": source, "channel": CHANNELS[source]}
        result["results"].append(item)
        item["non_stream"] = probe(base, key, model, source, False, timeout, client_factory)
        failure = item["non_stream"]
        if item["non_stream"]["ok"]:
            item["stream"] = probe(base, key, model, source, True, timeout, client_factory)
            failure = item["stream"]
            if item["stream"]["ok"]:
                passed.append(source)
        if failure.get("code") in STOP_CODES:
            result["stopped"] = failure["code"]
            break
    if evidence_output:
        # Replace with only this run's proven entries. Merging would refresh
        # unrelated stale evidence because version 1 has a global timestamp.
        result["evidence_written"] = write_evidence(evidence_output, evidence_base, model, passed)
        result["evidence_sources"] = passed if result["evidence_written"] else []
    result["ok"] = len(passed) == len(sources)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe two AI Studio channels without retries or private prompt data.")
    parser.add_argument("--model", required=True, help="Exact upstream model ID, without ais-/build- prefixes")
    parser.add_argument("--source", choices=("all", "ais", "build"), default="all")
    parser.add_argument("--timeout", type=float, default=60, help="Wall-clock limit per request, 1–180 seconds")
    parser.add_argument("--evidence-output", help="Replace this JSON with successful entries from this run only")
    parser.add_argument("--evidence-base-url", help="Optional backend URL as addressed from the Mixer container")
    args = parser.parse_args(argv)
    try:
        result = run(args.model, list(CHANNELS) if args.source == "all" else [args.source], args.timeout, os.environ,
                     args.evidence_output, args.evidence_base_url)
    except ProbeError as error:
        result = {"ok": False, "code": error.code, "evidence_written": False}
    except Exception:
        result = {"ok": False, "code": "probe_failed", "evidence_written": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
