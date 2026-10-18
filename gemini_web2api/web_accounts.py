"""Opt-in, request-scoped Gemini Web accounts; never swap process configuration."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import threading
import time

from .config import CONFIG
from .providers import ProviderError


_current = ContextVar("gemini_web_account", default=None)
_pool_lock = threading.Lock()
_pool = None
_pool_path = None
_MAX_FILE_BYTES = 1024 * 1024


def _invalid():
    # Files, paths and parser errors may contain secrets. Never reflect them.
    return ProviderError(503, "web_accounts_invalid", "Gemini Web account configuration is invalid")


def _read_json(path):
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            raise _invalid()
        with path.open("rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            raise _invalid()
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise _invalid()
                result[key] = value
            return result
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise _invalid()
        return value
    except (OSError, UnicodeError, ValueError):
        raise _invalid() from None


def _index(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,3}", str(value)):
        raise _invalid()
    return int(value)


def _safe_value(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


@dataclass(repr=False)
class WebAccount:
    id: str
    config: dict = field(repr=False)
    cookie: tuple = field(repr=False)
    client: object = field(default=None, repr=False)
    page_tokens: dict = field(default_factory=lambda: {"tokens": {}, "ts": 0}, repr=False)
    page_lock: object = field(default_factory=threading.Lock, repr=False)
    rate_limit_cooldown: int = 300
    busy: bool = False
    disabled: bool = False
    cooldown_until: float = 0
    failure: object = field(default=None, repr=False)


class WebAccountPool:
    def __init__(self, manifest_path):
        try:
            path = Path(manifest_path)
            if not path.is_absolute() or str(path).startswith(("//", "\\\\")):
                raise _invalid()
            path = path.resolve(strict=True)
            document = _read_json(path)
            if set(document) != {"accounts"}:
                raise _invalid()
            entries = document["accounts"]
            if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
                raise _invalid()
            accounts, indexes, files = [], set(), set()
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"id", "auth_user", "cookie_file"}:
                    raise _invalid()
                index = _index(entry["auth_user"])
                if entry["id"] != "web-u" + str(index) or index in indexes:
                    raise _invalid()
                relative = entry["cookie_file"]
                if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
                        or Path(relative).is_absolute() or ".." in Path(relative).parts):
                    raise _invalid()
                cookie_path = (path.parent / relative).resolve(strict=True)
                if not cookie_path.is_relative_to(path.parent) or cookie_path in files:
                    raise _invalid()
                exported = _read_json(cookie_path)
                if not {"cookie", "sapisid", "auth_user", "xsrf_token", "gemini_bl"}.issubset(exported):
                    raise _invalid()
                if _index(exported["auth_user"]) != index:
                    raise _invalid()
                cookie = exported.get("cookie")
                if not cookie or not _safe_value(cookie, 256 * 1024):
                    raise _invalid()
                pairs = {}
                for part in cookie.split(";"):
                    if "=" not in part:
                        raise _invalid()
                    name, value = part.strip().split("=", 1)
                    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or name in pairs:
                        raise _invalid()
                    pairs[name] = value
                sapisid = exported["sapisid"]
                if not sapisid or not _safe_value(sapisid, 8192) or pairs.get("SAPISID") != sapisid:
                    raise _invalid()
                config = dict(CONFIG, cookie_file=str(cookie_path), auth_user=index,
                              xsrf_token=None)
                for key, maximum in (("xsrf_token", 32768), ("gemini_bl", 512)):
                    if not exported[key] or not _safe_value(exported[key], maximum):
                        raise _invalid()
                    config[key] = exported[key]
                accounts.append(WebAccount(entry["id"], config, (cookie, sapisid)))
                indexes.add(index)
                files.add(cookie_path)
            cooldown = os.environ.get("GEMINI_WEB_RATE_LIMIT_COOLDOWN_SEC", "300")
            if not cooldown.isascii() or not cooldown.isdecimal() or not 1 <= int(cooldown) <= 86400:
                raise _invalid()
            self.cooldown = int(cooldown)
            self.accounts = accounts
            for account in accounts:
                account.rate_limit_cooldown = self.cooldown
            self.lock = threading.Lock()
            self.next_index = 0
        except (OSError, ValueError, TypeError):
            raise _invalid() from None

    @contextmanager
    def lease(self):
        with self.lock:
            now = time.monotonic()
            account = None
            for offset in range(len(self.accounts)):
                index = (self.next_index + offset) % len(self.accounts)
                candidate = self.accounts[index]
                if not candidate.busy and not candidate.disabled and candidate.cooldown_until <= now:
                    account = candidate
                    account.busy, account.failure = True, None
                    self.next_index = (index + 1) % len(self.accounts)
                    break
            if account is None:
                raise ProviderError(503, "web_accounts_unavailable",
                                    "Gemini Web accounts are busy, cooling down or require renewed authentication")
        token = _current.set(account)
        try:
            yield account
        finally:
            _current.reset(token)
            with self.lock:
                account.busy = False

    def close(self):
        for account in self.accounts:
            if account.client is not None:
                account.client.close()


def get_pool():
    """Cache at startup/first use. Credential updates require a controlled restart."""
    global _pool, _pool_path
    path = os.environ.get("GEMINI_WEB_ACCOUNTS_FILE", "")
    if not path:
        return None
    with _pool_lock:
        if _pool is None or path != _pool_path:
            pool = WebAccountPool(path)
            _pool, _pool_path = pool, path
        return _pool


@contextmanager
def lease_account():
    pool = get_pool()
    if pool is None:
        yield None
    else:
        with pool.lease() as account:
            yield account


def current_account():
    return _current.get()


def request_config(default):
    account = current_account()
    return account.config if account is not None else default


def reject_http_status(status, headers=None):
    """Only actual upstream HTTP status changes account state; never parse messages."""
    account = current_account()
    if account is None or status not in (401, 403, 429):
        return
    if status in (401, 403):
        account.disabled = True
        error = ProviderError(502, "web_account_auth_failed",
                              "Gemini Web rejected this account's authentication or access; renew its session")
    else:
        delay = account.rate_limit_cooldown
        retry_after = (headers or {}).get("Retry-After", "")
        try:
            if str(retry_after).isascii() and str(retry_after).isdecimal():
                delay = max(delay, int(retry_after))
            elif retry_after:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is not None:
                    delay = max(delay, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
        account.cooldown_until = time.monotonic() + min(86400, delay)
        error = ProviderError(429, "web_account_rate_limited",
                              "Gemini Web rate limited this account; the request was not retried with another account")
    account.failure = error
    raise error


def observe_http_error(error):
    if isinstance(error, ProviderError):
        raise error
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "code", None)
    reject_http_status(status, getattr(response, "headers", None) or getattr(error, "headers", None))


@contextmanager
def capture_http_errors():
    try:
        yield
    except Exception as error:
        observe_http_error(error)
        raise
