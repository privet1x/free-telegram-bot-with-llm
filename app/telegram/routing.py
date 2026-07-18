"""Deterministic explicit routing for Telegram mentions and bot replies."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Literal

from app.settings import settings
from app.telegram.identity import BotIdentity, get_bot_identity
from app.telegram.models import IncomingMessage, MAX_ENTITIES

ExplicitRoute = Literal["mention", "reply"]


def utf16_slice(text: str, offset: object, length: object) -> str | None:
    """Slice Telegram entity coordinates expressed as UTF-16 code units."""
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or isinstance(length, bool)
        or not isinstance(length, int)
        or offset < 0
        or length <= 0
    ):
        return None

    try:
        encoded = text.encode("utf-16-le")
    except UnicodeEncodeError:
        return None
    start = offset * 2
    end = (offset + length) * 2
    if start > len(encoded) or end > len(encoded):
        return None
    try:
        return encoded[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def extract_mentions(
    text: str, entities: Iterable[Mapping[str, object]]
) -> tuple[str, ...]:
    """Return complete tokens identified by valid Telegram mention entities."""
    mentions: list[str] = []
    for index, entity in enumerate(entities):
        if index >= MAX_ENTITIES:
            break
        if not isinstance(entity, Mapping) or entity.get("type") != "mention":
            continue
        token = utf16_slice(text, entity.get("offset"), entity.get("length"))
        if token is not None:
            mentions.append(token)
    return tuple(mentions)


def has_exact_mention(
    text: str,
    entities: Iterable[Mapping[str, object]],
    username: object,
) -> bool:
    """Match only a full @username token, case-insensitively."""
    if not isinstance(username, str):
        return False
    expected_username = username.strip().lstrip("@")
    if not expected_username:
        return False
    expected = f"@{expected_username}".casefold()
    return any(token.casefold() == expected for token in extract_mentions(text, entities))


def detect_explicit_route(
    msg: IncomingMessage,
    *,
    identity_loader: Callable[[], BotIdentity] | None = None,
) -> ExplicitRoute | None:
    """Select a Ticket-02 route without verifying identity for ordinary text.

    A local candidate is an exact mention of the configured username or a reply
    whose Telegram metadata identifies its author as a bot. Identity verification
    failures deliberately propagate as retryable ``BotIdentityUnavailable``.
    """
    if msg.is_edited:
        return None

    configured_username = settings.TELEGRAM_BOT_USERNAME
    mention_candidate = has_exact_mention(msg.text, msg.entities, configured_username)
    reply_candidate = msg.reply_to_bot and msg.reply_to_user_id is not None
    if not mention_candidate and not reply_candidate:
        return None

    identity = (identity_loader or get_bot_identity)()
    if mention_candidate and has_exact_mention(
        msg.text,
        msg.entities,
        identity.username,
    ):
        return "mention"
    if reply_candidate and msg.reply_to_user_id == identity.id:
        return "reply"
    return None
