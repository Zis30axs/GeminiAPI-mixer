"""Isolated, read-only Sub2API quota collector.

Only this process receives the operator's dedicated login file. Its short-lived
access token stays in memory, and the Mixer consumes only a sanitized snapshot.
No OAuth/account credentials, refresh tokens, or administrator credentials are
written to the shared directory. Authentication challenges require an operator.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .antigravity_quota import sanitize_account_usage


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CREDENTIAL_BYTES = 16 * 1024
_MAX_ACCOUNTS = 8
_MAX_PAGES = 8
_ERROR_CODES = frozenset((
    "configuration_invalid", "credentials_unavailable", "authentication_failed",
    "interactive_auth_required", "redirect_rejected", "upstream_rejected",
    "upstream_timeout", "upstream_connection_error", "upstream_response_too_large",
    "upstream_invalid_response", "group_scope_mismatch", "account_scope_mismatch",
    "account_scope_unavailable", "collection_failed", "snapshot_write_failed",
))


class ExporterError(Exception):
    """A fixed error code; raw exception text never crosses this boundary."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "collection_failed"
        super().__init__(self.code)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_id(value: Any) -> bool:
    return type(value) is int and 0 < value < 2**63


def _base_url(value: str, allow_http: bool) -> str:
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or len(value) > 2048 or not parsed.hostname
                or parsed.scheme not in ("http", "https")
                or (parsed.scheme == "http" and not allow_http)
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or "\\" in value or "%" in parsed.netloc or "%" in parsed.path
                or re.search(r"[\s\x00-\x1f\x7f]", value)
                or any(segment in (".", "..") for segment in parsed.path.split("/"))):
            raise ValueError
        parsed.port  # Validate malformed or out-of-range ports.
        return value.rstrip("/")
    except (TypeError, ValueError, AttributeError):
        raise ExporterError("configuration_invalid") from None


@dataclass(frozen=True)
class ExporterConfig:
    base_url: str
    credentials_file: str
    group_id: int = 4
    account_ids: tuple[int, ...] = (2,)
    output_file: str = "/var/lib/mixer-quota/snapshot.json"
    interval_sec: float = 60
    allow_http: bool = False
    connect_timeout: float = 5
    read_timeout: float = 15
    total_timeout: float = 45

    def __post_init__(self):
        object.__setattr__(self, "base_url", _base_url(self.base_url, self.allow_http is True))
        if (not _positive_id(self.group_id) or not isinstance(self.account_ids, tuple)
                or not 1 <= len(self.account_ids) <= _MAX_ACCOUNTS
                or any(not _positive_id(value) for value in self.account_ids)
                or len(set(self.account_ids)) != len(self.account_ids)
                or any(not isinstance(value, (str, os.PathLike)) or not os.fspath(value)
                       or "\x00" in os.fspath(value) for value in (self.credentials_file, self.output_file))
                or isinstance(self.interval_sec, bool) or not isinstance(self.interval_sec, (int, float))
                or not math.isfinite(self.interval_sec) or not 30 <= self.interval_sec <= 3600
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or not 0.01 <= value <= 300
                       for value in (self.connect_timeout, self.read_timeout, self.total_timeout))):
            raise ExporterError("configuration_invalid")
        try:
            if Path(self.credentials_file).resolve() == Path(self.output_file).resolve():
                raise ExporterError("configuration_invalid")
        except (OSError, ValueError, RuntimeError):
            raise ExporterError("configuration_invalid") from None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ExporterConfig":
        env = os.environ if environ is None else environ
        try:
            ids = env.get("SUB2API_QUOTA_ACCOUNT_IDS", "2").split(",")
            if any(not re.fullmatch(r"[0-9]{1,19}", value.strip()) for value in ids):
                raise ValueError
            return cls(
                base_url=env.get("SUB2API_QUOTA_BASE_URL", ""),
                credentials_file=env.get("SUB2API_QUOTA_CREDENTIALS_FILE", ""),
                group_id=int(env.get("SUB2API_QUOTA_GROUP_ID", "4")),
                account_ids=tuple(int(value.strip()) for value in ids),
                output_file=env.get("SUB2API_QUOTA_OUTPUT_FILE", "/var/lib/mixer-quota/snapshot.json"),
                interval_sec=float(env.get("SUB2API_QUOTA_INTERVAL_SEC", "60")),
                allow_http=env.get("SUB2API_QUOTA_ALLOW_HTTP", "false").lower() == "true",
            )
        except (TypeError, ValueError, OverflowError):
            raise ExporterError("configuration_invalid") from None


def _reject_constant(_value: str) -> None:
    raise ValueError


def _credentials(path: str) -> dict:
    try:
        candidate = Path(path)
        if str(candidate).startswith(("//", "\\\\")):
            raise ValueError
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError
            content = handle.read(_MAX_CREDENTIAL_BYTES + 1)
        if len(content) > _MAX_CREDENTIAL_BYTES:
            raise ValueError
        value = json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(value, dict) or set(value) != {"email", "password"}:
            raise ValueError
        if any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 4096
               or re.search(r"[\x00-\x1f\x7f]", value[key]) for key in ("email", "password")):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ExporterError("credentials_unavailable") from None


class _Deadline:
    def __init__(self, client: httpx.Client, seconds: float):
        self.client = client
        self.end = time.monotonic() + seconds
        self.expired = threading.Event()
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True

    def _expire(self):
        self.expired.set()
        try:
            self.client.close()
        except Exception:
            pass

    def check(self):
        remaining = self.end - time.monotonic()
        if self.expired.is_set() or remaining <= 0:
            raise ExporterError("upstream_timeout")
        return remaining

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *_args):
        self.timer.cancel()


def _interactive(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("requires_2fa", "requires2fa", "requires_captcha", "captcha_required"):
        if data.get(key) is True:
            return True
    reason = data.get("reason", "")
    return isinstance(reason, str) and any(marker in reason.upper() for marker in ("CAPTCHA", "TURNSTILE", "2FA", "TOTP"))


def _request(client: httpx.Client, config: ExporterConfig, deadline: _Deadline,
             method: str, path: str, *, token: str | None = None,
             body: dict | None = None, params: dict | None = None) -> Any:
    # Every call site supplies a constant route or a validated numeric ID.
    remaining = deadline.check()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    timeout = httpx.Timeout(min(config.read_timeout, remaining), connect=min(config.connect_timeout, remaining),
                            write=min(config.read_timeout, remaining), pool=min(config.connect_timeout, remaining))
    with client.stream(method, config.base_url + path, headers=headers, json=body, params=params, timeout=timeout) as response:
        deadline.check()
        if 300 <= response.status_code < 400:
            raise ExporterError("redirect_rejected")
        payload = bytearray()
        for chunk in response.iter_bytes(chunk_size=8192):
            deadline.check()
            if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise ExporterError("upstream_response_too_large")
            payload.extend(chunk)
        deadline.check()
        try:
            data = json.loads(payload, parse_constant=_reject_constant)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            if response.status_code in (401, 403):
                raise ExporterError("authentication_failed") from None
            raise ExporterError("upstream_invalid_response") from None
        if _interactive(data) or (isinstance(data, dict) and _interactive(data.get("data"))):
            raise ExporterError("interactive_auth_required")
        if response.status_code in (401, 403):
            raise ExporterError("authentication_failed")
        if response.status_code != 200:
            raise ExporterError("upstream_rejected")
        if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0 or "data" not in data:
            raise ExporterError("upstream_invalid_response")
        return data["data"]


def _check_group(data: Any, group_id: int):
    if not isinstance(data, dict) or not _positive_id(data.get("id")) or data["id"] != group_id or data.get("platform") != "antigravity":
        raise ExporterError("group_scope_mismatch")
    for key in ("fallback_group_id", "fallback_group_id_on_invalid_request"):
        fallback = data.get(key)
        if fallback is not None and not (type(fallback) is int and fallback == 0):
            raise ExporterError("group_scope_mismatch")


def _account_ids(client: httpx.Client, config: ExporterConfig, deadline: _Deadline, token: str) -> list[int]:
    allowed, found = set(config.account_ids), set()
    for page in range(1, _MAX_PAGES + 1):
        data = _request(client, config, deadline, "GET", "/api/v1/admin/accounts", token=token,
                        params={"platform": "antigravity", "group": str(config.group_id), "lite": "true",
                                "page": str(page), "page_size": "100"})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list) or len(data["items"]) > 100:
            raise ExporterError("upstream_invalid_response")
        for account in data["items"]:
            if not isinstance(account, dict) or type(account.get("id")) is not int or account["id"] not in allowed:
                continue
            groups = account.get("group_ids")
            if (account.get("platform") != "antigravity" or not isinstance(groups, list)
                    or any(not _positive_id(value) for value in groups) or config.group_id not in groups):
                raise ExporterError("account_scope_mismatch")
            found.add(account["id"])
        if found == allowed:
            return [account_id for account_id in config.account_ids if account_id in found]
        pages = data.get("pages")
        if (type(pages) is int and page >= pages) or not data["items"]:
            break
    raise ExporterError("account_scope_unavailable")


def _snapshot(config: ExporterConfig | None, accounts: list, code: str | None = None) -> dict:
    result = {"schema_version": 1, "source": "sub2api-account-quota",
              "group_id": config.group_id if config is not None else None,
              "observed_at": _utcnow(), "status": "unavailable" if code else "ok",
              "accounts": [] if code else accounts}
    if code:
        result["error_code"] = code if code in _ERROR_CODES else "collection_failed"
    return result


def collect_once(config: ExporterConfig | None = None, *, transport: httpx.BaseTransport | None = None) -> dict:
    """Collect one sanitized snapshot, without writing files or exposing errors."""
    try:
        config = config or ExporterConfig.from_env()
        login_body = _credentials(config.credentials_file)
        # Credentials are sent only to the operator-configured origin's login.
        with httpx.Client(transport=transport, follow_redirects=False, trust_env=False, verify=True) as client:
            with _Deadline(client, config.total_timeout) as deadline:
                login = _request(client, config, deadline, "POST", "/api/v1/auth/login", body=login_body)
                login_body.clear()
                if not isinstance(login, dict) or _interactive(login):
                    raise ExporterError("interactive_auth_required")
                token = login.get("access_token")
                if not isinstance(token, str) or not token or len(token) > 16384 or re.search(r"[\s\x00-\x1f\x7f]", token):
                    raise ExporterError("authentication_failed")
                login.clear()  # Discard any returned refresh token/user metadata.
                client.cookies.clear()
                group = _request(client, config, deadline, "GET", "/api/v1/admin/groups/" + str(config.group_id), token=token)
                _check_group(group, config.group_id)
                ids = _account_ids(client, config, deadline, token)
                accounts = []
                for account_id in ids:
                    usage = _request(client, config, deadline, "GET", "/api/v1/admin/accounts/" + str(account_id) + "/usage",
                                     token=token, params={"source": "active", "force": "false"})
                    if not isinstance(usage, dict):
                        raise ExporterError("upstream_invalid_response")
                    if any(usage.get(key) for key in ("error", "error_code", "is_forbidden", "needs_reauth", "needs_verify", "is_banned")):
                        raise ExporterError("upstream_rejected")
                    accounts.append(sanitize_account_usage(account_id, usage))
                deadline.check()
                token = None
                return _snapshot(config, accounts)
    except ExporterError as error:
        return _snapshot(config, [], error.code)
    except httpx.TimeoutException:
        return _snapshot(config, [], "upstream_timeout")
    except (httpx.HTTPError, OSError, RuntimeError):
        code = "upstream_timeout" if "deadline" in locals() and deadline.expired.is_set() else "upstream_connection_error"
        return _snapshot(config, [], code)
    except Exception:
        return _snapshot(config, [], "collection_failed")


def write_snapshot(snapshot: dict, output_file: str | os.PathLike) -> None:
    """Replace the shared regular snapshot atomically, keeping failures private."""
    temporary = None
    try:
        destination = Path(output_file)
        if str(destination).startswith(("//", "\\\\")) or destination.is_symlink():
            raise ValueError
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ValueError
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".quota-", suffix=".tmp", dir=str(destination.parent))
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            # Shared volume readers require read permission but never write.
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, destination)
        temporary = None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ExporterError("snapshot_write_failed") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def run(config: ExporterConfig | None = None, *, stop_event: threading.Event | None = None) -> None:
    config = config or ExporterConfig.from_env()
    stop = stop_event or threading.Event()
    while not stop.is_set():
        snapshot = collect_once(config)
        try:
            write_snapshot(snapshot, config.output_file)
            print("quota_exporter status=%s error_code=%s" % (snapshot["status"], snapshot.get("error_code", "none")), flush=True)
        except ExporterError:
            # Keep the old file timestamp: Mixer will mark it stale, never fresh.
            print("quota_exporter status=unavailable error_code=snapshot_write_failed", flush=True)
        stop.wait(config.interval_sec)


def main() -> int:
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    try:
        run(stop_event=stop)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("quota_exporter status=unavailable error_code=configuration_invalid", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
