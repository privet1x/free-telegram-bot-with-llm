"""Durable per-chat roster allowed to run the restricted lobotomy command."""

from __future__ import annotations

from app.store.redis import get_store


def _key(chat_id: int) -> str:
    return f"lobotomy:members:{chat_id}"


def invite(chat_id: int, user_id: int) -> bool:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError("chat_id must be a non-zero integer")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be positive")
    return bool(get_store().sadd(_key(chat_id), str(user_id)))


def revoke(chat_id: int, user_id: int) -> bool:
    """Remove one user from the per-chat lobotomy roster."""
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError("chat_id must be a non-zero integer")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be positive")
    return bool(get_store().srem(_key(chat_id), str(user_id)))


def is_invited(chat_id: int, user_id: int) -> bool:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        return False
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    return bool(get_store().sismember(_key(chat_id), str(user_id)))


def clear(chat_id: int) -> int:
    return int(get_store().delete(_key(chat_id)))
