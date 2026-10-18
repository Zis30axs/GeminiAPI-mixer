"""Multimodal: Scotty resumable upload for Gemini image input."""
import json
import base64
import urllib.request
import urllib.parse
import time
import ssl
import re
import hashlib
import threading
from urllib.parse import urlparse

from .config import CONFIG
from .gemini import load_cookie, make_sapisidhash, _get_ssl_ctx, log
from .web_accounts import current_account, request_config, observe_http_error, capture_http_errors


def _config():
    return request_config(CONFIG)


def _get_page_tokens() -> dict:
    """Fetch WIZ_global_data tokens from the configured Gemini account page."""
    auth_user = _config().get("auth_user")
    account_prefix = "" if auth_user is None or auth_user == "" else f"/u/{auth_user}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(auth_user)
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    try:
        req = urllib.request.Request(
            f"https://gemini.google.com{account_prefix}/app", headers=headers
        )
        proxy = _config().get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
            resp = opener.open(req, timeout=30)
        else:
            resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=30)
        with resp:
            html = resp.read().decode()
        tokens = {}
        for key, pattern in [
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
            # These values bind file-bearing StreamGenerate requests to the
            # currently loaded Gemini Web session.
            ("f_sid", r'"FdrFJe":\s*"([^"]+)"'),
            ("at", r'"SNlM0e":\s*"([^"]+)"'),
        ]:
            m = re.search(pattern, html)
            if m:
                tokens[key] = json.loads('"' + m.group(1) + '"')
        if not tokens.get("at"):
            m = re.search(r'"thykhd":\s*"([^\"]+)"', html)
            if m:
                tokens["at"] = json.loads('"' + m.group(1) + '"')
        return tokens
    except Exception as error:
        observe_http_error(error)
        log("Page token fetch failed")
        return {}


_page_tokens_cache = {"tokens": {}, "ts": 0}
_page_tokens_lock = threading.Lock()


def _cached_page_tokens(max_age: int = 600) -> dict:
    """Return Gemini page tokens, refreshing when they are older than max_age.

    File generation asks for a fresh page state because ``f.sid`` is a
    short-lived frontend routing value; uploads can safely reuse the normal
    cache.
    """
    cookie, _ = load_cookie()
    account = current_account()
    cache = account.page_tokens if account is not None else _page_tokens_cache
    # Invalidate after an account, Cookie or proxy change without retaining secrets.
    key = (_config().get("auth_user"), hashlib.sha256(cookie.encode()).digest(), _config().get("proxy"))
    with account.page_lock if account is not None else _page_tokens_lock:
        now = time.time()
        if (max_age <= 0 or now - cache["ts"] > max_age
                or cache.get("key") != key):
            tokens = _get_page_tokens()
            cache.update(tokens=tokens, ts=now if tokens else 0, key=key)
        return dict(cache["tokens"])


def detect_image_mime(image_bytes: bytes, fallback: str = "image/png") -> str:
    """Infer a common raster image MIME type from its file signature."""
    if not isinstance(image_bytes, bytes):
        return fallback
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
    return fallback


@capture_http_errors()
def upload_image(image_bytes: bytes, filename: str = "image.png", mime_type: str = "image/png") -> str:
    """Upload image via Scotty resumable upload. Returns file reference path."""
    tokens = _cached_page_tokens()
    push_id = tokens.get("push_id", "feeds/mcudyrk2a4khkz")
    pctx = tokens.get("pctx", "CgcSBWjK7pYx")

    cookie_str, sapisid = load_cookie()
    ctx = _get_ssl_ctx()
    proxy = _config().get("proxy")

    # Step 1: Initiate resumable upload
    start_headers = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "X-Client-Pctx": pctx,
        "X-Goog-Upload-Header-Content-Length": str(len(image_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    auth_user = _config().get("auth_user")
    if auth_user is not None and auth_user != "":
        start_headers["X-Goog-AuthUser"] = str(auth_user)
    if cookie_str:
        start_headers["Cookie"] = cookie_str
    if sapisid:
        start_headers["Authorization"] = make_sapisidhash(sapisid)

    start_url = "https://content-push.googleapis.com/upload/"
    req = urllib.request.Request(start_url, data=b"", headers=start_headers, method="POST")

    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx)
        )
        resp = opener.open(req, timeout=30)
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=30)

    with resp:
        upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError("No upload URL in response headers")
    parsed_upload = urlparse(upload_url)
    if (parsed_upload.scheme != "https" or parsed_upload.hostname != "content-push.googleapis.com"
            or parsed_upload.username or parsed_upload.password or parsed_upload.port not in (None, 443)):
        raise RuntimeError("Invalid upload URL")

    log("Upload session started")

    # Step 2: Upload file data + finalize
    upload_headers = {
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/octet-stream",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    req2 = urllib.request.Request(upload_url, data=image_bytes, headers=upload_headers, method="POST")
    if proxy:
        resp2 = opener.open(req2, timeout=60)
    else:
        resp2 = urllib.request.urlopen(req2, context=ctx, timeout=60)

    with resp2:
        file_ref = resp2.read().decode().strip()
    if not file_ref or not file_ref.startswith("/"):
        raise RuntimeError("Invalid file reference")

    log("Image uploaded")
    return file_ref


def fetch_image_bytes(url: str) -> bytes:
    """Fetch image from URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        log(f"Image fetch skipped for unsupported URL scheme: {parsed.scheme or 'none'}")
        return b""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        proxy = _config().get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
            resp = opener.open(req, timeout=30)
        else:
            resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=30)
        with resp:
            return resp.read()
    except Exception as e:
        log(f"Image fetch failed: {type(e).__name__}")
        return b""
