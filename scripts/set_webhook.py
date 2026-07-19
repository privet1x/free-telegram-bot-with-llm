"""Manage the Telegram webhook.

Usage (from the project root):
    python scripts/set_webhook.py set      # register without dropping pending updates
    python scripts/set_webhook.py info     # getWebhookInfo (diagnostics)
    python scripts/set_webhook.py delete   # delete the webhook
    python scripts/set_webhook.py set --drop-pending  # explicit destructive reset

Requires in .env: TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, PUBLIC_BASE_URL.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any

# Allows running as `python scripts/set_webhook.py` (add project root to path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.settings import is_https_base_url, settings  # noqa: E402

ALLOWED_UPDATES = ["message", "edited_message"]
MAX_CONNECTIONS = 1  # preserve chat order; this bot serves one small group
WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"


def _require(name: str, value: str) -> str:
    if not value:
        print(f"ERROR: {name} is not set (check .env / Vercel Env).")
        sys.exit(1)
    return value


def _checked(method: str, response: httpx.Response) -> dict[str, Any]:
    """Print a token-safe Telegram API result and fail on API errors."""
    try:
        data = response.json()
    except ValueError:
        print(f"ERROR: {method} returned invalid JSON (HTTP {response.status_code}).")
        raise SystemExit(1)

    print(f"{method} -> HTTP {response.status_code}; ok={data.get('ok') if isinstance(data, dict) else False}")
    if not 200 <= response.status_code < 300 or not isinstance(data, dict) or data.get("ok") is not True:
        print(f"ERROR: Telegram rejected {method}.")
        raise SystemExit(1)
    return data


def _request(method: str, request: Any, *args: Any, **kwargs: Any) -> httpx.Response:
    try:
        return request(*args, **kwargs)
    except httpx.HTTPError as exc:
        # A Bot API URL embeds the token. Do not print the exception/request URL.
        print(f"ERROR: {method} transport failure ({type(exc).__name__}).")
        raise SystemExit(1) from None


def set_webhook(*, drop_pending: bool = False) -> None:
    _require("TELEGRAM_BOT_TOKEN", settings.TELEGRAM_BOT_TOKEN)
    secret = _require("TELEGRAM_WEBHOOK_SECRET", settings.TELEGRAM_WEBHOOK_SECRET)
    if WEBHOOK_SECRET_RE.fullmatch(secret) is None:
        print(
            "ERROR: TELEGRAM_WEBHOOK_SECRET must be 1-256 characters and contain "
            "only A-Z, a-z, 0-9, '_' or '-'."
        )
        raise SystemExit(1)
    configured_base = _require("PUBLIC_BASE_URL", settings.PUBLIC_BASE_URL)
    if not is_https_base_url(configured_base):
        print("ERROR: PUBLIC_BASE_URL must be an HTTPS origin without userinfo, path, query, or fragment.")
        raise SystemExit(1)
    base = configured_base.rstrip("/")
    url = f"{base}/api/telegram/webhook"
    resp = _request(
        "setWebhook",
        httpx.post,
        _api("setWebhook"),
        json={
            "url": url,
            "secret_token": secret,
            "allowed_updates": ALLOWED_UPDATES,
            "max_connections": MAX_CONNECTIONS,
            "drop_pending_updates": drop_pending,
        },
        timeout=30.0,
    )
    _checked("setWebhook", resp)


def webhook_info() -> None:
    _require("TELEGRAM_BOT_TOKEN", settings.TELEGRAM_BOT_TOKEN)
    resp = _request("getWebhookInfo", httpx.get, _api("getWebhookInfo"), timeout=30.0)
    _checked("getWebhookInfo", resp)


def delete_webhook(*, drop_pending: bool = False) -> None:
    _require("TELEGRAM_BOT_TOKEN", settings.TELEGRAM_BOT_TOKEN)
    resp = _request(
        "deleteWebhook",
        httpx.post,
        _api("deleteWebhook"),
        json={"drop_pending_updates": drop_pending},
        timeout=30.0,
    )
    _checked("deleteWebhook", resp)


def main() -> None:
    args = sys.argv[1:]
    action = args[0] if args else "set"
    flags = set(args[1:])
    if flags - {"--drop-pending"}:
        print(f"Unknown option(s): {', '.join(sorted(flags))}")
        sys.exit(2)
    drop_pending = "--drop-pending" in flags
    if action == "set":
        set_webhook(drop_pending=drop_pending)
    elif action == "info":
        if flags:
            print("--drop-pending is valid only for set/delete.")
            sys.exit(2)
        webhook_info()
    elif action == "delete":
        delete_webhook(drop_pending=drop_pending)
    else:
        print(
            f"Unknown command: {action!r}. Expected set | info | delete "
            "[--drop-pending]."
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
