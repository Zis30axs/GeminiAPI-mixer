"""Configuration management."""
import json
import os

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_environment():
    """Environment overrides for the mixer; never serialize runtime secrets."""
    mapping = {"MIXER_HOST": "host", "MIXER_DEFAULT_MODEL": "default_model",
               "GEMINI_WEB_COOKIE_FILE": "cookie_file", "GEMINI_WEB_PROXY": "proxy"}
    for variable, field in mapping.items():
        if variable in os.environ:
            CONFIG[field] = os.environ[variable]
    if "MIXER_PORT" in os.environ:
        CONFIG["port"] = int(os.environ["MIXER_PORT"])
    if "MIXER_API_KEYS" in os.environ:
        CONFIG["api_keys"] = [key.strip() for key in os.environ["MIXER_API_KEYS"].split(",") if key.strip()]
    return CONFIG


def load_config(path: str = None):
    """Load config from JSON file."""
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
