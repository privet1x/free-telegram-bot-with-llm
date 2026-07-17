"""Check the Telegram bot token via getMe.

Confirms the token is valid, verifies the username, and shows the privacy mode
status (can_read_all_group_messages) — critical for history/triggers.

Run from the project root:
    python scripts/check_telegram.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.settings import settings  # noqa: E402


def _get(method: str, url: str, **kwargs: object) -> dict:
    try:
        response = httpx.get(url, **kwargs)
        data = response.json()
    except httpx.HTTPError as exc:
        print(f"ERROR: {method} transport failure ({type(exc).__name__}).")
        raise SystemExit(1) from None
    except ValueError:
        print(f"ERROR: {method} returned invalid JSON.")
        raise SystemExit(1)
    if not 200 <= response.status_code < 300 or not isinstance(data, dict):
        print(f"ERROR: {method} failed (HTTP {response.status_code}).")
        raise SystemExit(1)
    return data


def main() -> None:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env.")
        sys.exit(1)

    data = _get("getMe", f"https://api.telegram.org/bot{token}/getMe", timeout=30.0)
    print("getMe -> response received")
    if not data.get("ok"):
        print("Token is INVALID.")
        sys.exit(1)

    res = data["result"]
    can_read_all = res.get("can_read_all_group_messages")
    print("id                :", res.get("id"))
    print("username          :", res.get("username"))
    print("can_join_groups   :", res.get("can_join_groups"))
    print("privacy OFF?      :", can_read_all, "(should be True)")

    configuration_ok = True
    expected = settings.TELEGRAM_BOT_USERNAME.lstrip("@")
    if not expected:
        configuration_ok = False
        print("ERROR: TELEGRAM_BOT_USERNAME is not set.")
    else:
        match = str(res.get("username") or "").casefold() == expected.casefold()
        print("username match    :", match, f"(.env: {expected})")
        if not match:
            configuration_ok = False
            print("\nERROR: fix TELEGRAM_BOT_USERNAME before deployment.")

    allowed_chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
    if allowed_chat_id is None:
        print(
            "\nWARNING: TELEGRAM_ALLOWED_CHAT_ID is not set. Production will reject "
            "updates until the closed group is configured."
        )
        sys.exit(1)

    chat_data = _get(
        "getChat",
        f"https://api.telegram.org/bot{token}/getChat",
        params={"chat_id": allowed_chat_id},
        timeout=30.0,
    )
    print(f"getChat({allowed_chat_id}) -> response received")
    if not chat_data.get("ok"):
        print("Configured chat is unavailable to the bot.")
        sys.exit(1)

    chat = chat_data["result"]
    print("allowed chat type :", chat.get("type"))
    print("allowed chat title:", chat.get("title"))
    if chat.get("type") not in {"group", "supergroup"}:
        print("ERROR: TELEGRAM_ALLOWED_CHAT_ID must point to the closed group.")
        sys.exit(1)

    if not can_read_all:
        member_data = _get(
            "getChatMember",
            f"https://api.telegram.org/bot{token}/getChatMember",
            params={"chat_id": allowed_chat_id, "user_id": res.get("id")},
            timeout=30.0,
        )
        status = (
            member_data.get("result", {}).get("status")
            if member_data.get("ok")
            else None
        )
        bot_is_admin = status in {"administrator", "creator"}
        print("bot group admin   :", bot_is_admin)
        if not bot_is_admin:
            configuration_ok = False
            print(
                "\nERROR: Privacy Mode is on and the bot is not a group admin, so "
                "ordinary messages are invisible. Disable privacy in @BotFather "
                "and re-add the bot, or promote it to group admin."
            )
    else:
        print("privacy coverage  : OK (Privacy Mode is off)")

    if not configuration_ok:
        sys.exit(1)
    print("\nOK — token, username, closed-group access, and message visibility are valid.")


if __name__ == "__main__":
    main()
