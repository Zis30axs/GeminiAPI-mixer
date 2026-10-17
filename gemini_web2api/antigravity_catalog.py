"""Operator-recorded model evidence and verified thinking-variant routing.

The file contains safe test outcomes, never account credentials or raw upstream
errors. A model name's suffix alone is not evidence of a thinking capability.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .antigravity_quota import quota_for_model
from .providers import ProviderError


MAX_BYTES = 256 * 1024
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_REVISION = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_EFFORTS = frozenset(("low", "medium", "high"))
_STATUSES = frozenset(("verified", "upstream_declared", "unavailable"))
_CONTROLS = frozenset(("upstream-model-variant", "upstream-controlled"))
_REASONS = frozenset((
    "generation_verified", "source_declared", "upstream_declared", "model_unsupported",
    "unsupported_model", "quota_exhausted", "quota_or_capacity_exhausted", "rate_limited",
    "capacity_exhausted", "upstream_unavailable", "validation_failed", "unknown",
))
_QUOTA_REASONS = frozenset(("quota_exhausted", "quota_or_capacity_exhausted", "rate_limited", "capacity_exhausted"))
_UNSUPPORTED_REASONS = frozenset(("model_unsupported", "unsupported_model"))


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return None


def _model(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _empty() -> dict:
    return {"schema_version": 1, "source_revision": None, "models": {}}


def _sanitize(raw: Any) -> dict:
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        return _empty()
    models = raw.get("models")
    if not isinstance(models, dict) or len(models) > 512:
        return _empty()
    revision = raw.get("source_revision")
    clean = {
        "schema_version": 1,
        "source_revision": revision.lower() if isinstance(revision, str) and _REVISION.fullmatch(revision) else None,
        "models": {},
    }
    for model, value in models.items():
        if _model(model) is None or not isinstance(value, dict):
            continue
        status = value.get("status")
        control = value.get("thinking_control")
        effort = value.get("effort")
        effective = _model(value.get("effective_upstream_model"))
        quota_model = _model(value.get("quota_model_id"))
        http_status = value.get("http_status")
        tested_at = _timestamp(value.get("tested_at"))
        reason = value.get("reason")
        if (not isinstance(status, str) or status not in _STATUSES
                or not isinstance(control, str) or control not in _CONTROLS
                or effort is not None and (not isinstance(effort, str) or effort not in _EFFORTS)
                or effective is None or quota_model is None
                or type(http_status) is not int or not 100 <= http_status <= 599):
            continue
        # A successful test requires a real timestamp and 2xx response. Source
        # declarations may carry a test failure and remain explicitly unverified.
        if status == "verified" and (tested_at is None or not 200 <= http_status <= 299
                or datetime.fromisoformat(tested_at.replace("Z", "+00:00")) > datetime.now(timezone.utc)):
            continue
        if control == "upstream-model-variant" and effort is None:
            continue
        if control == "upstream-controlled" and effort is not None:
            continue
        clean["models"][model] = {
            "status": status, "tested_at": tested_at, "http_status": http_status,
            "effective_upstream_model": effective, "quota_model_id": quota_model,
            "effort": effort, "thinking_control": control,
            "reason": reason if isinstance(reason, str) and reason in _REASONS else "unknown",
        }
        response_model = _model(value.get("upstream_response_model"))
        if response_model is not None:
            clean["models"][model]["upstream_response_model"] = response_model
    return clean


def _reject_constant(_: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def read_model_evidence(path: str | os.PathLike | None = None) -> dict:
    """Read only bounded local regular files; failures mean unknown evidence."""
    configured = os.environ.get("ANTIGRAVITY_MODEL_EVIDENCE_FILE", "") if path is None else path
    if not configured:
        return _empty()
    try:
        candidate = os.fspath(configured)
        if not isinstance(candidate, str) or len(candidate) > 4096 or "\x00" in candidate:
            return _empty()
        if candidate.startswith(("//", "\\\\")):
            return _empty()
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate) and not re.match(r"^[A-Za-z]:[\\/]", candidate):
            return _empty()
        resolved = Path(candidate).resolve(strict=True)
        if str(resolved).startswith(("//", "\\\\")) or not stat.S_ISREG(resolved.stat().st_mode):
            return _empty()
        with resolved.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return _empty()
            content = handle.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            return _empty()
        raw = json.loads(content.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)
        return _sanitize(raw)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return _empty()


def model_details(model: str, evidence: dict | None = None, quota_catalog: dict | None = None) -> dict:
    """Describe only an exact evidence entry and its explicitly named quota key."""
    evidence = read_model_evidence() if evidence is None else _sanitize(evidence)
    row = evidence["models"].get(model, {})
    effective = row.get("effective_upstream_model", model)
    quota_model = row.get("quota_model_id", model)
    result = {
        "verification": {
            "status": row.get("status", "unknown"),
            "http_status": row.get("http_status"), "tested_at": row.get("tested_at"),
            "reason": row.get("reason", "unknown"),
        },
        "effective_upstream_model": effective, "quota_model_id": quota_model,
        "thinking_control": row.get("thinking_control", "upstream-controlled"),
        "quota": quota_for_model(quota_model, quota_catalog),
    }
    if effective != model:
        result["alias_of"] = effective
    if row.get("effort") is not None:
        result["requested_effort"] = row["effort"]
    if row.get("upstream_response_model") is not None:
        result["upstream_response_model"] = row["upstream_response_model"]
    return result


def _unavailable(row: dict) -> None:
    if row.get("status") != "unavailable":
        return
    if row.get("http_status") == 429 or row.get("reason") in _QUOTA_REASONS:
        raise ProviderError(429, "upstream_model_quota_unavailable", "Antigravity model quota or capacity is unavailable.")
    if row.get("http_status") == 404 or row.get("reason") in _UNSUPPORTED_REASONS:
        raise ProviderError(404, "model_not_found", "Antigravity upstream does not support this model.")
    if row.get("http_status") in (401, 403):
        raise ProviderError(403, "upstream_model_access_denied", "Antigravity upstream has not authorized this model.")
    raise ProviderError(400, "upstream_model_unavailable", "Antigravity model is currently unavailable.")


def _effort(value: Any) -> str:
    if not isinstance(value, str) or value not in _EFFORTS:
        raise ProviderError(400, "unsupported_thinking_level", "Antigravity reasoning effort must be low, medium, or high.")
    return value


def resolve_model_effort(
    model: str, req: dict, allowed_models: Sequence[str], evidence: dict | None = None,
) -> tuple[str, dict]:
    """Route body effort only through a verified, exact allowlisted variant.

    Delete body controls once the model ID is authoritative: Sub2API's generic
    effort conversion can otherwise change an explicitly selected low variant.
    """
    if not isinstance(req, dict):
        raise ProviderError(400, "invalid_request", "Antigravity request must be a JSON object.")
    evidence = read_model_evidence() if evidence is None else _sanitize(evidence)
    rows = evidence["models"]
    row = rows.get(model, {})
    _unavailable(row)
    clean = copy.deepcopy(req)
    requested = _effort(clean["reasoning_effort"]) if "reasoning_effort" in clean else None
    reasoning = clean.get("reasoning")
    if "reasoning" in clean and not isinstance(reasoning, dict):
        raise ProviderError(400, "invalid_request", "Antigravity reasoning must be a JSON object.")
    nested = _effort(reasoning["effort"]) if isinstance(reasoning, dict) and "effort" in reasoning else None
    if requested is not None and nested is not None and requested != nested:
        raise ProviderError(400, "conflicting_thinking_level", "Conflicting Antigravity reasoning effort controls.")
    requested = requested if requested is not None else nested
    if requested is None:
        return model, clean
    selected = None
    if row.get("thinking_control") == "upstream-model-variant" and row.get("effort") is not None:
        if row["effort"] != requested:
            raise ProviderError(400, "conflicting_thinking_level", "Antigravity model variant conflicts with the requested reasoning effort.")
        if row.get("status") == "verified" and model in allowed_models:
            selected = model
    else:
        candidate = model + "-" + requested
        target = rows.get(candidate, {})
        if (candidate in allowed_models and target.get("status") == "verified"
                and target.get("thinking_control") == "upstream-model-variant"
                and target.get("effort") == requested):
            selected = candidate
    if selected is None:
        raise ProviderError(400, "unsupported_thinking_level", "No verified Antigravity model variant supports the requested reasoning effort.")
    clean.pop("reasoning_effort", None)
    if isinstance(reasoning, dict) and "effort" in reasoning:
        reasoning.pop("effort")
        if not reasoning:
            clean.pop("reasoning", None)
    return selected, clean
