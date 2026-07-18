"""Verified identity of the Telegram bot represented by the configured token."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from app.settings import settings
from app.store.redis import get_store
from app.telegram import client as telegram_client
from app.telegram.models import MAX_USERNAME_CHARS

BOT_IDENTITY_KEY = "bot:self"
BOT_IDENTITY_CACHE_SECONDS = 86_400
MAX_BOT_IDENTITY_JSON_CHARS = 256

_identity_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """A getMe result verified against local configuration."""

    id: int
    username: str


class BotIdentityUnavailable(RuntimeError):
    """A sanitized retryable failure to establish the current bot identity."""

    retryable = True
    error_class = "bot_identity_unavailable"

    def __init__(self) -> None:
        super().__init__("bot identity is temporarily unavailable")


def _configured_username() -> str | None:
    raw = settings.TELEGRAM_BOT_USERNAME
    if not isinstance(raw, str):
        return None
    username = raw.strip().lstrip("@")
    if not username or len(username) > MAX_USERNAME_CHARS:
        return None
    return username


def _strict_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _verified_identity(value: object, expected_username: str) -> BotIdentity | None:
    if not isinstance(value, dict):
        return None
    bot_id = _strict_positive_int(value.get("id"))
    username = value.get("username")
    if (
        bot_id is None
        or not isinstance(username, str)
        or not username
        or len(username) > MAX_USERNAME_CHARS
        or username.casefold() != expected_username.casefold()
    ):
        return None
    return BotIdentity(id=bot_id, username=username)


def _decode_cached(raw: object, expected_username: str) -> BotIdentity | None:
    if not isinstance(raw, str) or len(raw) > MAX_BOT_IDENTITY_JSON_CHARS:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return _verified_identity(value, expected_username)


def get_bot_identity() -> BotIdentity:
    """Return a cached identity or verify it lazily through Telegram getMe.

    Cache corruption, a configured-username mismatch, Telegram failure, and
    Redis failure all use one sanitized retryable error at this boundary.
    """
    expected_username = _configured_username()
    if expected_username is None:
        raise BotIdentityUnavailable()

    try:
        store = get_store()
        cached = _decode_cached(store.get(BOT_IDENTITY_KEY), expected_username)
        if cached is not None:
            return cached

        with _identity_lock:
            cached = _decode_cached(store.get(BOT_IDENTITY_KEY), expected_username)
            if cached is not None:
                return cached

            result = telegram_client.get_me()
            identity = _verified_identity(result, expected_username)
            if identity is None:
                raise BotIdentityUnavailable()
            encoded = json.dumps(
                {"id": identity.id, "username": identity.username},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            store.set(
                BOT_IDENTITY_KEY,
                encoded,
                ex=BOT_IDENTITY_CACHE_SECONDS,
            )
            return identity
    except BotIdentityUnavailable:
        raise
    except Exception:
        raise BotIdentityUnavailable() from None
