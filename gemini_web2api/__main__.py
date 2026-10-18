"""Entry point: python -m gemini_web2api"""
import argparse
import os

from .config import CONFIG, load_config, find_config, load_environment
from .models import MODELS
from .gemini import HAS_HTTPX
from .server import ThreadedServer
from .mixer import MixerHandler
from .web_accounts import get_pool
from .providers import ProviderError
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG") or find_config()
    if config_path:
        load_config(config_path)
    else:
        CONFIG["host"] = "127.0.0.1"
    load_environment()

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    allow_anonymous = os.environ.get("MIXER_ALLOW_UNAUTHENTICATED", "").lower() == "true"
    if not CONFIG.get("api_keys") and not allow_anonymous:
        parser.error("Set MIXER_API_KEYS before starting the gateway")
    if allow_anonymous and CONFIG["host"] not in ("127.0.0.1", "::1", "localhost"):
        parser.error("Unauthenticated mode requires a loopback bind address")

    try:
        web_pool = get_pool()
    except ProviderError as error:
        parser.error(error.message)
    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), MixerHandler)
    print(f"GeminiAPI Mixer (Gemini Web compatibility v{__version__})")
    print(f"  Listening: http://{CONFIG['host']}:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    if web_pool is not None:
        print(f"  Web pool:  {len(web_pool.accounts)} accounts; one active request per account")
    print(f"  Proxy:     {'configured' if CONFIG.get('proxy') else 'not configured'}")
    print(f"  Streaming: {'httpx (true streaming)' if HAS_HTTPX else 'urllib (buffered)'}")
    print(f"  Temporary: {'yes' if CONFIG.get('temporary_chats', False) else 'no'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        if web_pool is not None:
            web_pool.close()


if __name__ == "__main__":
    main()
