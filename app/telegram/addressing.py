"""Deterministic Telegram-first-name response addressing."""

from __future__ import annotations

import unicodedata

from app.telegram.models import MAX_NAME_CHARS


def normalize_first_name(value: object) -> str:
    """Bound one Telegram first_name for safe plain-text delivery."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        " "
        if character.isspace()
        else ""
        if unicodedata.category(character).startswith("C")
        else character
        for character in value
    )
    return " ".join(cleaned.split())[:MAX_NAME_CHARS].strip()


def address_text(first_name: object, text: object) -> str:
    """Prefix a response outside the LLM with a verified Telegram first_name."""
    name = normalize_first_name(first_name)
    body = str(text or "").strip()
    if not body:
        return f"{name}," if name else ""
    return f"{name}, {body}" if name else body
