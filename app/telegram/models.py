"""Lightweight parsing of a Telegram Update -> a convenient IncomingMessage.

We do not pull in a heavy framework: on serverless it is enough to parse a single
update from the request body. We handle both message and edited_message (both are
listed in allowed_updates).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MAX_HISTORY_TEXT_CHARS = 4096


@dataclass
class IncomingMessage:
    update_id: int
    chat_id: int
    message_id: int
    text: str
    user_id: Optional[int]
    username: Optional[str]
    name: str
    is_bot: bool
    is_edited: bool
    date: int
    edit_date: Optional[int]
    reply_to_bot: bool
    reply_to_message_id: Optional[int]
    reply_to_user_id: Optional[int]
    reply_to_text: Optional[str]
    entities: list[dict]
    raw: dict


def _full_name(user: dict) -> str:
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p)
    return name or (user.get("username") or "unknown")


def _as_int(value: object) -> Optional[int]:
    """Accept only JSON integer values, never Python's bool-as-int coercion."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bounded_text(value: object) -> str:
    """Bound text persisted in Redis even if an upstream payload is malformed."""
    return str(value or "")[:MAX_HISTORY_TEXT_CHARS]


def parse_update(update: dict) -> Optional[IncomingMessage]:
    """Return an IncomingMessage, or None if the update has no usable message."""
    if not isinstance(update, dict):
        return None
    update_id = _as_int(update.get("update_id"))
    if update_id is None:
        return None

    message = update.get("message")
    is_edited = False
    if not isinstance(message, dict):
        message = update.get("edited_message")
        is_edited = True
    if not isinstance(message, dict):
        return None

    chat = message.get("chat") or {}
    if not isinstance(chat, dict):
        return None
    chat_id = _as_int(chat.get("id"))
    message_id = _as_int(message.get("message_id"))
    message_date = _as_int(message.get("date"))
    edit_date = _as_int(message.get("edit_date"))
    if (
        chat_id is None
        or message_id is None
        or message_date is None
        or (is_edited and edit_date is None)
    ):
        return None

    user = message.get("from") or {}
    if not isinstance(user, dict):
        user = {}
    reply = message.get("reply_to_message") or {}
    if not isinstance(reply, dict):
        reply = {}
    reply_from = reply.get("from") or {}
    if not isinstance(reply_from, dict):
        reply_from = {}
    has_text = message.get("text") is not None
    text = message.get("text") if has_text else message.get("caption")
    reply_text = reply.get("text") or reply.get("caption")

    entities = message.get("entities") if has_text else message.get("caption_entities")
    if not isinstance(entities, list):
        entities = []

    return IncomingMessage(
        update_id=update_id,
        chat_id=chat_id,
        message_id=message_id,
        text=_bounded_text(text),
        user_id=_as_int(user.get("id")),
        username=user.get("username"),
        name=_full_name(user),
        is_bot=bool(user.get("is_bot", False)),
        is_edited=is_edited,
        date=message_date,
        edit_date=edit_date,
        reply_to_bot=bool(reply_from.get("is_bot", False)),
        reply_to_message_id=_as_int(reply.get("message_id")),
        reply_to_user_id=_as_int(reply_from.get("id")),
        reply_to_text=_bounded_text(reply_text) if reply_text is not None else None,
        entities=entities,
        raw=update,
    )


def to_history_record(msg: IncomingMessage) -> dict:
    reply_to = None
    if msg.reply_to_message_id is not None:
        reply_to = {
            "message_id": msg.reply_to_message_id,
            "user_id": msg.reply_to_user_id,
            "is_bot": msg.reply_to_bot,
            "text": msg.reply_to_text,
        }
    return {
        "message_id": msg.message_id,
        "source_update_id": msg.update_id,
        "user_id": msg.user_id,
        "username": msg.username,
        "name": msg.name,
        "text": msg.text,
        "ts": msg.date,
        "edit_ts": msg.edit_date,
        "is_edited": msg.is_edited,
        "is_bot": msg.is_bot,
        "reply_to": reply_to,
    }


def to_observed_user(msg: IncomingMessage) -> Optional[dict]:
    """Build a user-directory record from an incoming message."""
    if msg.user_id is None:
        return None
    return {
        "id": msg.user_id,
        "username": msg.username,
        "name": msg.name,
        "is_bot": msg.is_bot,
        "last_seen_at": msg.edit_date if msg.edit_date is not None else msg.date,
        "last_update_id": msg.update_id,
    }


def parse_command(text: str, bot_username: Optional[str] = None) -> Optional[str]:
    """Return a command only when an optional target names this bot.

    Bare commands always qualify. A command with ``@suffix`` qualifies only when
    ``bot_username`` is configured and matches case-insensitively.
    """
    if not text or not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    command, separator, target = parts[0].partition("@")
    if not command:
        return None
    if separator:
        expected = (bot_username or "").strip().lstrip("@").casefold()
        if not expected or target.casefold() != expected:
            return None
    return command.lower()
