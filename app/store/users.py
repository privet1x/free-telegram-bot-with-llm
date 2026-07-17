"""Directory of Telegram users observed in allowed-chat updates."""

from __future__ import annotations

import json
from typing import Any, Optional

from app.store.redis import get_store


def normalize_username(username: Optional[str]) -> Optional[str]:
    """Return the case-insensitive username used by index keys."""
    if not username:
        return None
    normalized = username.strip().lstrip("@").casefold()
    return normalized or None


def user_key(user_id: int) -> str:
    return f"user:{user_id}"


def username_key(username: str) -> str:
    return f"username:{username}"


def _decode(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def get(user_id: int) -> Optional[dict[str, Any]]:
    """Return one observed profile by its canonical Telegram ID."""
    return _decode(get_store().get(user_key(user_id)))


def observe(user: dict[str, Any]) -> dict[str, Any]:
    """Atomically upsert a profile and its globally versioned username alias."""
    user_id = int(user["id"])
    username = user.get("username") or None
    normalized = normalize_username(username)
    record = {
        "id": user_id,
        "username": username,
        "name": str(user.get("name") or username or "unknown"),
        "is_bot": bool(user.get("is_bot", False)),
        "last_seen_at": user.get("last_seen_at"),
        "last_update_id": user.get("last_update_id"),
    }

    stored = get_store().observe_user_json(
        user_id,
        normalized,
        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
    )
    decoded = _decode(stored)
    if decoded is None:  # Defensive: adapter contracts require a JSON object.
        raise RuntimeError("user store returned an invalid profile")
    return decoded


def resolve_username(username: str) -> Optional[dict[str, Any]]:
    """Resolve only a currently indexed, already-observed Telegram profile."""
    normalized = normalize_username(username)
    if normalized is None:
        return None
    store = get_store()
    raw_id = store.get(username_key(normalized))
    if raw_id is None:
        return None
    try:
        record = get(int(raw_id))
    except ValueError:
        return None
    if record is None or normalize_username(record.get("username")) != normalized:
        return None
    return record
