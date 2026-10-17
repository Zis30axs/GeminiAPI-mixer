"""Read a credential-free Sub2API account quota snapshot.

Mixer deliberately receives a local, read-only export rather than Sub2API
administrator credentials. Percentages are integer observations, not token or
request balances. Sub2API's default-zero remainingFraction makes a reported
100% utilization ambiguous; it must never become a fabricated zero balance.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE = "sub2api-account-quota"
MAX_BYTES = 2 * 1024 * 1024
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _id(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


def _used(value: Any) -> int | None:
    # Go reports an integer percent. Do not coerce strings, booleans, NaN, or
    # fractional percentages and imply greater precision than was observed.
    return value if type(value) is int and 0 <= value <= 100 else None


def _quota(used: Any, reset: Any, observation_valid: bool) -> dict:
    percent = _used(used) if observation_valid else None
    reset_time = _time(reset) if observation_valid else None
    result = {
        "status": "unknown",
        "remaining_percent": None,
        "reported_used_percent": percent,
        "reset_at": _utc(reset_time) if reset_time is not None else None,
        "precision": None,
    }
    if percent is not None:
        result["precision"] = "integer-percent-as-reported"
        if percent == 100:
            result["status"] = "reported_exhausted_or_unknown"
        else:
            result["status"] = "available"
            result["remaining_percent"] = 100 - percent
    return result


def sanitize_account_usage(account_id: int, usage: dict, *, now: datetime | None = None) -> dict:
    """Normalize only antigravity_quota and its explicit updated_at timestamp.

    Callers extracting a nested upstream timestamp must name it ``updated_at``.
    Unknown/missing/future observation times invalidate the observation. An
    unverified ``evidence_verified_fraction`` field is intentionally ignored.
    """
    current = _now(now)
    usage = usage if isinstance(usage, dict) else {}
    observed = _time(usage.get("updated_at"))
    valid = _id(account_id) is not None and observed is not None and observed <= current
    quotas = usage.get("antigravity_quota")
    models = {}
    if isinstance(quotas, dict):
        for model, raw in quotas.items():
            if isinstance(model, str) and _MODEL.fullmatch(model):
                raw = raw if isinstance(raw, dict) else {}
                models[model] = _quota(raw.get("utilization"), raw.get("reset_time"), valid)
    return {
        "account_id": _id(account_id),
        "observed_at": _utc(observed) if valid else None,
        "status": "ok" if valid and models else "unknown",
        "models": models,
    }


def _max_age() -> float:
    try:
        value = float(os.environ.get("ANTIGRAVITY_QUOTA_MAX_AGE_SEC", "180"))
        if not math.isfinite(value) or not 1 <= value <= 86400:
            raise ValueError
        return value
    except (TypeError, ValueError):
        return 180.0


def _empty(status: str = "unknown") -> dict:
    return {
        "schema_version": 1, "source": SOURCE, "group_id": None,
        "observed_at": None, "status": status, "accounts": [],
        "stale": True, "age_seconds": None,
    }


def _catalog(raw: Any, current: datetime) -> dict:
    """Revalidate the export and rebuild every returned object from a whitelist."""
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        return _empty("unavailable")
    if (raw.get("source") == SOURCE and raw.get("group_id") is None
            and raw.get("observed_at") is None and raw.get("accounts") == []
            and raw.get("status") in ("unknown", "unavailable")):
        return _empty(raw["status"])
    if raw.get("source") != SOURCE or _id(raw.get("group_id")) is None:
        return _empty("unavailable")
    observed = _time(raw.get("observed_at"))
    if observed is None or observed > current:
        return _empty("unknown")
    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or len(accounts) > 4096:
        return _empty("unavailable")
    age = (current - observed).total_seconds()
    result = {
        "schema_version": 1, "source": SOURCE, "group_id": raw["group_id"],
        "observed_at": _utc(observed),
        "status": "ok" if raw.get("status") == "ok" else "unavailable",
        "accounts": [], "stale": age > _max_age(), "age_seconds": age,
    }
    seen = set()
    for account in accounts:
        if not isinstance(account, dict) or _id(account.get("account_id")) is None:
            continue
        account_id = account["account_id"]
        if account_id in seen:
            continue
        seen.add(account_id)
        account_time = _time(account.get("observed_at"))
        valid = (account_time is not None and account_time <= current
                 and account_time <= observed and account.get("status") == "ok")
        account_age = (current - account_time).total_seconds() if valid else None
        clean = {
            "account_id": account_id,
            "observed_at": _utc(account_time) if valid else None,
            "status": "ok" if valid else "unknown",
            "stale": not valid or account_age > _max_age(),
            "age_seconds": account_age, "models": {},
        }
        models = account.get("models")
        if isinstance(models, dict):
            for model, value in models.items():
                if isinstance(model, str) and _MODEL.fullmatch(model) and isinstance(value, dict):
                    clean["models"][model] = _quota(
                        value.get("reported_used_percent"), value.get("reset_at"), valid,
                    )
        result["accounts"].append(clean)
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def load_quota_catalog(path: str | os.PathLike | None = None, *, now: datetime | None = None) -> dict:
    """Read at most 2 MiB from an administrator-configured regular local file.

    No URL fetching, credentials, logging, or raw exception messages. A broken
    export must not prevent models discovery or inference from working.
    """
    current = _now(now)
    configured = os.environ.get("ANTIGRAVITY_QUOTA_FILE", "") if path is None else path
    if not configured:
        return _empty()
    try:
        candidate = os.fspath(configured)
        if not isinstance(candidate, str) or len(candidate) > 4096 or "\x00" in candidate:
            return _empty("unavailable")
        if candidate.startswith(("//", "\\\\")):
            return _empty("unavailable")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate) and not re.match(r"^[A-Za-z]:[\\/]", candidate):
            return _empty("unavailable")
        resolved = Path(candidate).resolve(strict=True)
        if str(resolved).startswith(("//", "\\\\")) or not stat.S_ISREG(resolved.stat().st_mode):
            return _empty("unavailable")
        with resolved.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return _empty("unavailable")
            content = handle.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            return _empty("unavailable")
        raw = json.loads(content.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)
        return _catalog(raw, current)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return _empty("unavailable")


def quota_for_model(model: str, catalog: dict | None = None, *, now: datetime | None = None) -> dict:
    """Return an exact upstream quota bucket, without guessing alias suffixes.

    Independent account percentages cannot be added. Multiple matching accounts
    produce a range and explicitly identify the earliest future reset.
    """
    current = _now(now)
    clean = load_quota_catalog(now=current) if catalog is None else _catalog(catalog, current)
    result = {
        "source": SOURCE, "scope": "model", "model": model,
        "observed_at": clean["observed_at"], "stale": clean["stale"],
        "collected_at": clean["observed_at"],
        "age_seconds": clean["age_seconds"], "status": "unknown",
        "remaining_percent": None, "reset_at": None, "accounts": [],
    }
    if clean["status"] != "ok":
        result["status"] = clean["status"]
        return result
    for account in clean["accounts"]:
        value = account["models"].get(model)
        if value is None:
            continue
        row = {
            "account_id": account["account_id"], "observed_at": account["observed_at"],
            "age_seconds": account["age_seconds"],
            "stale": clean["stale"] or account["stale"], **value,
        }
        if row["stale"]:
            row["status"] = "stale"
        result["accounts"].append(row)
    rows = result["accounts"]
    if not rows:
        return result
    result["stale"] = clean["stale"] or any(row["stale"] for row in rows)
    observations = [row["observed_at"] for row in rows if row["observed_at"] is not None]
    result["observed_at"] = min(observations, key=_time) if observations else None
    ages = [row["age_seconds"] for row in rows if row["age_seconds"] is not None]
    result["age_seconds"] = max([clean["age_seconds"], *ages])
    if len(rows) == 1:
        result.update({key: rows[0][key] for key in (
            "status", "remaining_percent", "reported_used_percent", "reset_at", "precision",
        )})
        result["observed_at"] = rows[0]["observed_at"]
        return result
    values = [row["remaining_percent"] for row in rows if row["remaining_percent"] is not None]
    result["remaining_percent_range"] = {"min": min(values), "max": max(values)} if values else None
    future = [row["reset_at"] for row in rows if _time(row["reset_at"]) is not None
              and _time(row["reset_at"]) > current]
    result["reset_at"] = min(future, key=_time) if future else None
    result["aggregate"] = {
        "method": "independent-account-range", "account_count": len(rows),
        "known_remaining_account_count": len(values), "reset_method": "earliest-future-account-reset",
        "percentages_are_additive": False,
    }
    statuses = {row["status"] for row in rows}
    result["status"] = "stale" if result["stale"] else next(iter(statuses)) if len(statuses) == 1 else "mixed"
    return result
