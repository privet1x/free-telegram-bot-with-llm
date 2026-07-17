"""Print group IDs visible to the bot without printing message/user content.

This helper uses getUpdates, so an outgoing webhook must not be active. Add the
bot to the target group, send an ordinary message, then run from the project root:
    python scripts/discover_chat_id.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.settings import settings  # noqa: E402


def main() -> None:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set in .env.")
        raise SystemExit(1)

    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"allowed_updates": '["message","edited_message"]', "timeout": 0},
            timeout=30.0,
        )
        data = response.json()
    except httpx.HTTPError as exc:
        print(f"ERROR: getUpdates transport failure ({type(exc).__name__}).")
        raise SystemExit(1) from None
    except ValueError:
        print("ERROR: getUpdates returned invalid JSON.")
        raise SystemExit(1)

    if (
        not 200 <= response.status_code < 300
        or not isinstance(data, dict)
        or data.get("ok") is not True
    ):
        print(f"ERROR: Telegram rejected getUpdates (HTTP {response.status_code}).")
        raise SystemExit(1)

    chats: dict[int, tuple[str, str]] = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") in {"group", "supergroup"} and chat.get("id") is not None:
            chats[int(chat["id"])] = (chat.get("type", ""), chat.get("title", ""))

    if not chats:
        print(
            "No group update found. Ensure no webhook is active, Privacy Mode is "
            "disabled (or the bot is admin), then send a new ordinary group message."
        )
        raise SystemExit(1)

    print("Visible groups (no message content shown):")
    for chat_id, (chat_type, title) in sorted(chats.items()):
        print(f"  TELEGRAM_ALLOWED_CHAT_ID={chat_id}  type={chat_type}  title={title!r}")


if __name__ == "__main__":
    main()
