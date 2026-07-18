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
import time
from typing import Any, Optional

import httpx

from app.settings import settings

_client: Optional[httpx.Client] = None
_lock = threading.Lock()
MAX_TELEGRAM_CHUNK_CHARS = 4_000


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

    def __init__(
        self,
        message: str,
        *,
        method: str,
        description: str | None = None,
        status_code: int | None = None,
        transport_error: bool = False,
        outcome_unknown: bool | None = None,
    ) -> None:
        super().__init__(message)
        # Consumers may need the exact Bot API description for an idempotency
        # decision, but it must never become the exception string/log message.
        self.description = description
        self.method = method
        self.status_code = status_code
        self.transport_error = transport_error
        self.outcome_unknown = (
            transport_error if outcome_unknown is None else outcome_unknown
        )

    @property
    def retryable(self) -> bool:
        return bool(
            self.transport_error
            or self.status_code == 408
            or self.status_code == 429
            or (self.status_code is not None and self.status_code >= 500)
        )

    @property
    def message_not_modified(self) -> bool:
        description = (self.description or "").strip().casefold()
        canonical = "bad request: message is not modified"
        return bool(
            self.method == "editMessageText"
            and self.status_code == 400
            and (description == canonical or description.startswith(canonical + ": "))
        )


def call(method: str, payload: dict[str, Any]) -> Any:
    try:
        resp = _http().post(f"{_base_url()}/{method}", json=payload)
    except httpx.HTTPError as exc:
        # httpx exception strings include request URLs; Telegram puts the bot
        # token in that URL, so never let the original exception escape/log.
        raise TelegramAPIError(
            f"Telegram {method} transport failure ({type(exc).__name__})",
            method=method,
            transport_error=True,
            outcome_unknown=not isinstance(
                exc,
                (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
            ),
        ) from None
    if not 200 <= resp.status_code < 300:
        description = None
        error_code = resp.status_code
        try:
            error_data = resp.json()
        except ValueError:
            error_data = None
        if isinstance(error_data, dict):
            if error_data.get("description") is not None:
                description = str(error_data["description"])
            raw_error_code = error_data.get("error_code")
            if isinstance(raw_error_code, int) and not isinstance(raw_error_code, bool):
                error_code = raw_error_code
        raise TelegramAPIError(
            f"Telegram {method} returned HTTP {resp.status_code}",
            method=method,
            description=description,
            status_code=error_code,
        )
    try:
        data = resp.json()
    except ValueError:
        raise TelegramAPIError(
            f"Telegram {method} returned invalid JSON",
            method=method,
            outcome_unknown=True,
        ) from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = data.get("description") if isinstance(data, dict) else None
        raw_error_code = data.get("error_code") if isinstance(data, dict) else None
        error_code = (
            raw_error_code
            if isinstance(raw_error_code, int) and not isinstance(raw_error_code, bool)
            else None
        )
        raise TelegramAPIError(
            f"Telegram {method} rejected request",
            method=method,
            description=str(description) if description is not None else None,
            status_code=error_code,
            outcome_unknown=not (isinstance(data, dict) and data.get("ok") is False),
        )
    if "result" not in data:
        raise TelegramAPIError(
            "Telegram API response has no result",
            method=method,
            outcome_unknown=True,
        )
    return data["result"]


def send_message(
    chat_id: int, text: str, reply_to_message_id: Optional[int] = None
) -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    result = call("sendMessage", payload)
    if not isinstance(result, dict):
        raise TelegramAPIError(
            "sendMessage result is not a message object",
            method="sendMessage",
            outcome_unknown=True,
        )
    return result


def edit_message_text(chat_id: int, message_id: int, text: str) -> dict:
    result = call(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": text},
    )
    if not isinstance(result, dict):
        raise TelegramAPIError(
            "editMessageText result is not a message object",
            method="editMessageText",
            outcome_unknown=True,
        )
    return result


def get_me() -> dict:
    result = call("getMe", {})
    if not isinstance(result, dict):
        raise TelegramAPIError("getMe result is not a user object", method="getMe")
    return result


def split_plain_text(text: str, limit: int = MAX_TELEGRAM_CHUNK_CHARS) -> list[str]:
    """Split plain text deterministically below Telegram's message limit."""
    if limit <= 0 or limit > MAX_TELEGRAM_CHUNK_CHARS:
        raise ValueError("limit must be between 1 and 4000")
    if not text:
        return []
    return [text[start : start + limit] for start in range(0, len(text), limit)]


def outbound_history_record(
    result: dict[str, Any],
    *,
    source_update_id: int,
    fallback_chat_id: int,
    fallback_user_id: int,
    text: str | None = None,
    edited: bool = False,
    edit_timestamp: int | None = None,
) -> dict[str, Any]:
    """Normalize a successful Bot API message for canonical history storage."""
    message_id = result.get("message_id")
    message_date = result.get("date")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise TelegramAPIError(
            "Telegram result has no integer message_id", method="history"
        )
    if isinstance(message_date, bool) or not isinstance(message_date, int):
        raise TelegramAPIError("Telegram result has no integer date", method="history")

    chat = result.get("chat")
    result_chat_id = chat.get("id") if isinstance(chat, dict) else None
    chat_id = (
        result_chat_id
        if isinstance(result_chat_id, int) and not isinstance(result_chat_id, bool)
        else fallback_chat_id
    )
    if chat_id != fallback_chat_id:
        raise TelegramAPIError(
            "Telegram result belongs to an unexpected chat", method="history"
        )

    sender = result.get("from")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    user_id = (
        sender_id
        if isinstance(sender_id, int) and not isinstance(sender_id, bool)
        else fallback_user_id
    )
    username = sender.get("username") if isinstance(sender, dict) else None
    first_name = sender.get("first_name") if isinstance(sender, dict) else None
    last_name = sender.get("last_name") if isinstance(sender, dict) else None
    name = " ".join(
        part for part in (first_name, last_name) if isinstance(part, str) and part
    ) or str(username or "bot")

    reply = result.get("reply_to_message")
    reply_to = None
    if isinstance(reply, dict):
        reply_message_id = reply.get("message_id")
        reply_from = reply.get("from")
        if isinstance(reply_message_id, int) and not isinstance(reply_message_id, bool):
            reply_to = {
                "message_id": reply_message_id,
                "user_id": (
                    reply_from.get("id") if isinstance(reply_from, dict) else None
                ),
                "is_bot": bool(
                    reply_from.get("is_bot", False)
                    if isinstance(reply_from, dict)
                    else False
                ),
                "text": str(reply.get("text") or reply.get("caption") or "")[:4096],
            }

    raw_edit_timestamp = result.get("edit_date")
    normalized_edit_timestamp = (
        raw_edit_timestamp
        if isinstance(raw_edit_timestamp, int)
        and not isinstance(raw_edit_timestamp, bool)
        else edit_timestamp
    )
    if edited and normalized_edit_timestamp is None:
        # Telegram returns edit_date on normal success. This fallback is needed
        # only after a prior successful edit is recovered via "not modified".
        normalized_edit_timestamp = int(time.time())

    return {
        "message_id": message_id,
        "source_update_id": source_update_id,
        "user_id": user_id,
        "username": username,
        "name": name[:256],
        "text": str(text if text is not None else result.get("text") or "")[:4096],
        "ts": message_date,
        "edit_ts": normalized_edit_timestamp if edited else None,
        "is_edited": edited,
        "is_bot": True,
        "reply_to": reply_to,
    }


def webhook_reply(chat_id: int, text: str) -> dict:
    """Reply returned as the webhook body: Telegram runs sendMessage itself."""
    return {"method": "sendMessage", "chat_id": chat_id, "text": text}
