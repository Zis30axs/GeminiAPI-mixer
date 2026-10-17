"""Import an explicitly exported AI Studio session into a private local account store.

No browser discovery, network requests, credential logging, or Drive authorization.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

MAX_BYTES = 1024 * 1024
MAX_COOKIES = 512
DOMAINS = {"google.com", ".google.com", "aistudio.google.com", ".aistudio.google.com"}
REQUIRED = {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}
EMAIL = re.compile(r"[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}\Z")
COOKIE_VALUE = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+\Z")
MARKER = ".mixer-session-import"
MARKER_CONTENT = b"Mixer AI Studio private account store v1\n"


class ImportRejected(ValueError):
    """Fixed error codes keep credential values and file contents out of output."""


def reject(code: str) -> None:
    raise ImportRejected(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject("duplicate_json_field")
        result[key] = value
    return result


def validate_state(value, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not isinstance(value, dict) or set(value) != {"cookies", "origins", "aistudio2api"}:
        reject("invalid_state_fields")
    if value["origins"] != []:
        reject("unexpected_local_storage")
    extension = value["aistudio2api"]
    if not isinstance(extension, dict) or set(extension) != {"source"}:
        reject("invalid_auth_source")
    source = extension["source"]
    if not isinstance(source, dict) or set(source) != {"browser", "email", "auth_user"} or source["browser"] != "mixer-import":
        reject("invalid_auth_source")
    email = source["email"]
    if (not isinstance(email, str) or len(email) > 254 or not EMAIL.fullmatch(email)
            or ".." in email.partition("@")[0] or email.partition("@")[0].endswith(".")):
        reject("invalid_email")
    index = source["auth_user"]
    if not isinstance(index, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", index):
        reject("invalid_account_index")
    cookies = value["cookies"]
    if not isinstance(cookies, list) or not 1 <= len(cookies) <= MAX_COOKIES:
        reject("invalid_cookie_count")
    seen = set()
    required = set()
    expected_fields = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    for cookie in cookies:
        if not isinstance(cookie, dict) or set(cookie) != expected_fields:
            reject("invalid_cookie_fields")
        name, content, domain, path = (cookie[field] for field in ("name", "value", "domain", "path"))
        if not isinstance(name, str) or not COOKIE_NAME.fullmatch(name):
            reject("invalid_cookie_name")
        if not isinstance(content, str) or len(content) > 16384 or not COOKIE_VALUE.fullmatch(content):
            reject("invalid_cookie_value")
        if not isinstance(domain, str) or domain not in DOMAINS:
            reject("invalid_cookie_domain")
        if not isinstance(path, str) or len(path) > 2048 or not re.fullmatch(r"/[^\x00-\x20\x7f;]*", path):
            reject("invalid_cookie_path")
        if type(cookie["httpOnly"]) is not bool or type(cookie["secure"]) is not bool:
            reject("invalid_cookie_flags")
        if name.startswith(("__Secure-", "__Host-")) and not cookie["secure"]:
            reject("invalid_cookie_flags")
        if name.startswith("__Host-") and (path != "/" or domain.startswith(".")):
            reject("invalid_cookie_flags")
        if cookie["sameSite"] not in ("", "None", "Lax", "Strict"):
            reject("invalid_same_site")
        expires = cookie["expires"]
        if type(expires) not in (int, float) or not math.isfinite(expires) or (expires != -1 and not now < expires <= 253402300799):
            reject("expired_or_invalid_cookie")
        identity = (name, domain, path)
        if identity in seen:
            reject("duplicate_cookie")
        seen.add(identity)
        if name in REQUIRED and domain == ".google.com" and path == "/":
            required.add(name)
    if required != REQUIRED:
        reject("missing_required_cookies")
    return value


def checked_path(raw: str | Path) -> Path:
    value = os.fspath(raw)
    if not value or any(ord(character) < 32 for character in value) or value.startswith(("\\\\", "//")):
        reject("unsafe_path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        reject("absolute_local_path_required")
    if os.name == "nt":
        for part in path.parts[1:]:
            stem = part.split(".", 1)[0].upper()
            if part.endswith((".", " ")) or ":" in part or stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
                reject("unsafe_path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            reject("symlink_or_reparse_point")
    return path


def read_local_json(path: Path, max_bytes: int = MAX_BYTES):
    path = checked_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes or before.st_nlink != 1:
        reject("unsafe_source_file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or not stat.S_ISREG(opened.st_mode):
            reject("source_changed")
        encoded = source.read(max_bytes + 1)
        if len(encoded) > max_bytes:
            reject("source_too_large")
    try:
        value = json.loads(encoded.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        reject("invalid_json")
    return value


def read_source(path: Path) -> dict:
    return validate_state(read_local_json(path))


def validate_previous_identity(path: Path, email: str) -> None:
    # Upstream may expire/remove cookies or write additional storage data. Renewal
    # replaces that state in full; only its explicit owner must remain unchanged.
    previous = read_local_json(path)
    if not isinstance(previous, dict):
        reject("invalid_previous_state")
    extension = previous.get("aistudio2api")
    source = extension.get("source") if isinstance(extension, dict) else None
    if (not isinstance(source, dict) or source.get("browser") != "mixer-import"
            or source.get("email") != email):
        reject("account_identity_mismatch")


def private_permissions(path: Path, directory: bool) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600, follow_symlinks=False)
        return
    # A protected DACL permits only the current Windows identity and SYSTEM.
    # No shell or subprocess receives session material.
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    security = ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            reject("private_permissions_failed")
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            reject("private_permissions_failed")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)):
            reject("private_permissions_failed")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            reject("private_permissions_failed")
        inheritance = "OICI" if directory else ""
        descriptor = f"D:P(A;{inheritance};FA;;;{sid_text.value})(A;{inheritance};FA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(descriptor, 1, ctypes.byref(security), None):
            reject("private_permissions_failed")
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(security, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present:
            reject("private_permissions_failed")
        if advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None) != 0:
            reject("private_permissions_failed")
    finally:
        if security:
            kernel.LocalFree(security)
        if sid_text:
            kernel.LocalFree(sid_text)
        if token:
            kernel.CloseHandle(token)


def atomic_private_write(path: Path, encoded: bytes) -> None:
    checked_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".session-import-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as target:
            private_permissions(temporary_path, False)
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        checked_path(path)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def prepare_private_root(root: Path) -> None:
    root = checked_path(root)
    protected = {Path.home(), Path.cwd(), Path(__file__).resolve().parents[1]}
    protected.update(Path.home() / name for name in ("Documents", "Desktop", "Downloads", ".ssh", ".codex"))
    if root in protected or not root.parent.is_dir():
        reject("dedicated_private_directory_required")
    if root.exists():
        if not root.is_dir():
            reject("invalid_auth_directory")
        marker = checked_path(root / MARKER)
        if marker.exists():
            if not marker.is_file() or marker.stat().st_size != len(MARKER_CONTENT) or marker.read_bytes() != MARKER_CONTENT:
                reject("invalid_auth_directory")
        elif any(root.iterdir()):
            reject("dedicated_private_directory_required")
    else:
        root.mkdir(mode=0o700)
    private_permissions(root, True)
    if not (root / MARKER).exists():
        atomic_private_write(root / MARKER, MARKER_CONTENT)


def import_session(source: str | Path, auth_dir: str | Path, replace: bool = False) -> int:
    source_path, root = checked_path(source), checked_path(auth_dir)
    state = read_source(source_path)
    email = state["aistudio2api"]["source"]["email"]
    # Upstream uses the directory basename as Account.ID and requires that ID
    # to be the same Google email when acquiring the runtime lease.
    target = checked_path(root / email)
    if source_path == target / "storage-state.json":
        reject("source_is_destination")
    if target.exists() and not replace:
        reject("account_exists_use_replace")
    prepare_private_root(root)
    account = {"label": email, "enabled": True, "proxy": "", "locale": "zh-CN", "timezone": "Asia/Shanghai"}
    if target.exists():
        if not target.is_dir():
            reject("invalid_account_directory")
        validate_previous_identity(checked_path(target / "storage-state.json"), email)
        config_path = checked_path(target / "account.json")
        try:
            config = read_local_json(config_path, max_bytes=8192)
        except (ValueError, RecursionError):
            reject("invalid_account_configuration")
        if not isinstance(config, dict) or config.get("label") != email:
            reject("account_identity_mismatch")
        # Renewal preserves account settings and cooldown/runtime files.
        private_permissions(target, True)
    else:
        target.mkdir(mode=0o700)
        private_permissions(target, True)
        atomic_private_write(target / "account.json", (json.dumps(account, ensure_ascii=True, indent=2) + "\n").encode())
    atomic_private_write(target / "storage-state.json", (json.dumps(state, ensure_ascii=True, indent=2) + "\n").encode())
    return len(state["cookies"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import a manually exported AI Studio session into a dedicated private directory.")
    parser.add_argument("--source", required=True, help="Absolute local path of the exported JSON file")
    parser.add_argument("--auth-dir", required=True, help="Absolute dedicated private account directory; parent must exist")
    parser.add_argument("--replace", action="store_true", help="Replace the same account's session after stopping the sidecar")
    args = parser.parse_args(argv)
    try:
        count = import_session(args.source, args.auth_dir, args.replace)
    except ImportRejected as error:
        print(json.dumps({"status": "rejected", "reason": str(error)}))
        return 2
    except (OSError, ValueError, TypeError, OverflowError):
        print(json.dumps({"status": "rejected", "reason": "local_import_failed"}))
        return 2
    print(json.dumps({"status": "imported", "cookie_count": count}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
