"""Thin Telegram Bot API client on top of httpx.

Two ways to reply:
* ``webhook_reply`` — return the method directly as the webhook response body (no
  extra request); Telegram will execute it. Handy for an instant reply (e.g. /ping).
* ``call`` / ``send_message`` — a regular outbound Bot API call (needed from ticket
  02 onwards for the placeholder + editMessageText pattern).

The httpx client is created lazily (reduces cold start and avoids opening a socket
where the reply is returned via the webhook body).
"""

from __future__ import annotations

import threading
from typing import Any, Optional

import httpx

from app.settings import settings

_client: Optional[httpx.Client] = None
_lock = threading.Lock()


def _http() -> httpx.Client:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = httpx.Client(timeout=httpx.Timeout(15.0))
    return _client


def _base_url() -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"


class TelegramAPIError(RuntimeError):
    """A Telegram Bot API response with ``ok: false`` or an invalid result."""

    def __init__(self, message: str, *, description: str | None = None) -> None:
        super().__init__(message)
        # Consumers may need the exact Bot API description for an idempotency
        # decision, but it must never become the exception string/log message.
        self.description = description


def call(method: str, payload: dict[str, Any]) -> Any:
    try:
        resp = _http().post(f"{_base_url()}/{method}", json=payload)
    except httpx.HTTPError as exc:
        # httpx exception strings include request URLs; Telegram puts the bot
        # token in that URL, so never let the original exception escape/log.
        raise TelegramAPIError(
            f"Telegram {method} transport failure ({type(exc).__name__})"
        ) from None
    if not 200 <= resp.status_code < 300:
        raise TelegramAPIError(f"Telegram {method} returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise TelegramAPIError(f"Telegram {method} returned invalid JSON") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = data.get("description") if isinstance(data, dict) else None
        raise TelegramAPIError(
            f"Telegram {method} rejected request",
            description=str(description) if description is not None else None,
        )
    if "result" not in data:
        raise TelegramAPIError("Telegram API response has no result")
    return data["result"]


def send_message(
    chat_id: int, text: str, reply_to_message_id: Optional[int] = None
) -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    result = call("sendMessage", payload)
    if not isinstance(result, dict):
        raise TelegramAPIError("sendMessage result is not a message object")
    return result


def webhook_reply(chat_id: int, text: str) -> dict:
    """Reply returned as the webhook body: Telegram runs sendMessage itself."""
    return {"method": "sendMessage", "chat_id": chat_id, "text": text}
