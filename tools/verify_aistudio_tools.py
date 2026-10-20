"""Explicit real function-call/response probes for Gemini 3.8 only.

No external function is executed. The only tool returns a fixed local marker.
Uses the operator's normal backend API and channel lock, not browser credentials.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import httpx
import verify_aistudio_http as probe
from session_manager import SessionManager, read_settings, encode
import import_aistudio_session as secure

MODEL = "gemini-3.8-flash"
MARKER = "MIXER_TOOL_OK"
NAME = "mixer_probe_echo"
DECLARATIONS = [{"functionDeclarations": [{"name": NAME, "description": "Return the provided value unchanged.",
    "parameters": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}}, "required": ["value"]}}]}]


def request(settings, source, body, stream):
    headers = {"Authorization": "Bearer " + settings["AISTUDIO_HTTP_API_KEY"],
               "X-Mixer-Upstream-Channel": probe.CHANNELS[source]}
    method = "streamGenerateContent?alt=sse" if stream else "generateContent"
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=75) as client:
        with probe.Deadline(client, 90) as deadline:
            with client.stream("POST", "http://127.0.0.1:18084/v1beta/models/" + MODEL + ":" + method, headers=headers, json=body) as response:
                probe.check_response(response, probe.CHANNELS[source])
                if not stream:
                    return [probe.read_json(response, deadline)]
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise probe.ProbeError("expected_sse")
                return [probe.decode(raw) for raw, _ in probe.sse_events(response, deadline) if raw != b"[DONE]"]


def parts(events):
    return [part for event in events for candidate in event.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])]


def terminal(events):
    return any(c.get("finishReason") == "STOP" for e in events for c in e.get("candidates", []))


def declared(settings, source):
    headers = {"Authorization": "Bearer " + settings["AISTUDIO_HTTP_API_KEY"], "X-Mixer-Upstream-Channel": probe.CHANNELS[source]}
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=12) as client:
        with probe.Deadline(client, 15) as deadline:
            with client.stream("GET", "http://127.0.0.1:18084/v1/models", headers=headers) as response:
                probe.check_response(response, probe.CHANNELS[source])
                data = probe.read_json(response, deadline)
    return any(item.get("id") == MODEL and item.get("capabilities", {}).get("function_declarations") is True
               and probe.CHANNELS[source] in item.get("channels", []) for item in data.get("data", []))


def check(settings, source, stream):
    row = {"source": source, "stream": stream, "ok": False}
    try:
        initial = {"contents": [{"role": "user", "parts": [{"text": "Call mixer_probe_echo exactly once with value MIXER_TOOL_OK. Use the function."}]}],
                   "tools": copy.deepcopy(DECLARATIONS),
                   "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [NAME]}}}
        events = request(settings, source, initial, stream)
        native_parts = parts(events)
        calls = [p["functionCall"] for p in native_parts if "functionCall" in p]
        if len(calls) != 1 or calls[0].get("name") != NAME or calls[0].get("args") != {"value": MARKER} or not terminal(events):
            row["code"] = "native_function_call_not_confirmed"
            return row
        row["function_call_confirmed"] = True
        followup = {"contents": initial["contents"] + [
            {"role": "model", "parts": native_parts},
            {"role": "user", "parts": [{"functionResponse": {"name": NAME, "response": {"value": MARKER}}}]},
            {"role": "user", "parts": [{"text": "Reply with exactly the value returned by the function. Do not call another function."}]}],
            "tools": copy.deepcopy(DECLARATIONS), "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}}}
        returned = request(settings, source, followup, stream)
        returned_parts = parts(returned)
        text = "".join(p.get("text", "") for p in returned_parts if not p.get("thought"))
        row["function_response_confirmed"] = text.strip() == MARKER and not any("functionCall" in p for p in returned_parts) and terminal(returned)
        row["ok"] = row["function_response_confirmed"]
        if not row["ok"]:
            row["code"] = "native_function_response_not_confirmed"
    except probe.ProbeError as error:
        row["code"] = error.code
        if error.status:
            row["status"] = error.status
    except Exception:
        row["code"] = "tool_probe_failed"
    return row


def verify(manager, sources, *, stream=True, apply=False, progress=lambda _: None):
    if not sources or any(s not in probe.CHANNELS for s in sources):
        raise ValueError("invalid_test_source")
    settings = read_settings(manager.root)
    report = {"model": MODEL, "checked_at": datetime.now(timezone.utc).isoformat(), "results": [], "enabled_sources": []}
    passed = []
    with manager.exclusive():
        state = manager.studio_status()
        if not state.get("ready"):
            return dict(report, code="studio_not_ready")
        for source in dict.fromkeys(sources):
            if not declared(settings, source):
                report["results"].append({"source": source, "ok": False, "code": "tools_not_declared"})
                continue
            complete = True
            for use_stream in ((False, True) if stream else (False,)):
                progress("正在验证 " + source.upper() + (" 流式工具回环…" if use_stream else " 工具回环…"))
                row = check(settings, source, use_stream)
                report["results"].append(row)
                if not row["ok"]:
                    complete = False
                    break
            if complete and stream:
                passed.append(source)
            if not complete and row.get("code") in probe.STOP_CODES:
                break
        if apply and passed:
            path = secure.checked_path(Path(settings["AISTUDIO_EVIDENCE_FILE"]))
            evidence = secure.read_local_json(path)
            # Keep the original v1 global timestamp: never refresh unrelated
            # Argon/thinking/image/text evidence as a side effect of tool probes.
            observed = datetime.fromisoformat(evidence["verified_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            if (evidence.get("version") != 1 or evidence.get("backend_base_url") != "http://aistudio-http:2048"
                    or not -300 <= age <= float(settings.get("AISTUDIO_HTTP_EVIDENCE_MAX_AGE_SEC", "604800"))):
                raise ValueError("base_evidence_missing_or_stale")
            import uuid
            backup = manager.backups / ("tool-evidence-" + uuid.uuid4().hex[:12])
            backup.mkdir(); secure.private_permissions(backup, True)
            old = path.read_bytes()
            secure.atomic_private_write(backup / "evidence.json", old)
            for source in passed:
                proof = evidence.get("models", {}).get(source + "/" + MODEL)
                if not isinstance(proof, dict) or proof.get("text") is not True:
                    raise ValueError("base_text_evidence_missing")
                proof["tools"] = True
            # Evidence is a file bind mount, so restart Mixer after atomic replacement.
            manager.container("web")
            manager.docker("stop", "--time", "20", "geminiapi-mixer-mixer-1")
            try:
                secure.atomic_private_write(path, encode(evidence))
                manager.docker("start", "geminiapi-mixer-mixer-1")
            except Exception:
                secure.atomic_private_write(path, old)
                manager.docker("start", "geminiapi-mixer-mixer-1")
                raise ValueError("evidence_update_rolled_back") from None
            report["enabled_sources"] = passed
            report["backup"] = str(backup)
        secure.atomic_private_write(manager.runtime / "session-manager-tools.json", encode(report))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment-root", type=Path, required=True)
    parser.add_argument("--source", choices=("ais", "build", "all"), default="all")
    parser.add_argument("--plain-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        manager = SessionManager(args.deployment_root)
        report = verify(manager, ["ais", "build"] if args.source == "all" else [args.source], stream=not args.plain_only, apply=args.apply)
        print(json.dumps(report, ensure_ascii=True), flush=True)
        return 0 if report["results"] and all(r["ok"] for r in report["results"]) else 1
    except Exception:
        print(json.dumps({"ok": False, "code": "tool_verification_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
