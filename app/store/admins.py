"""Server-authoritative Telegram administrator role checks."""

from __future__ import annotations

from app.settings import settings
from app.store.redis import get_store

ADMINS_KEY = "admins"


def _version_key(user_id: int) -> str:
    return f"adminver:{user_id}"


def _validate_user_id(user_id: int) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be positive")
    return str(user_id)


def list_admins() -> list[int]:
    values = get_store().smembers(ADMINS_KEY)
    result: list[int] = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.append(parsed)
    return sorted(set(result))


def add_admin(user_id: int) -> bool:
    normalized = _validate_user_id(user_id)
    changed = bool(get_store().sadd(ADMINS_KEY, normalized))
    if changed:
        current = get_store().get(_version_key(user_id))
        get_store().set(_version_key(user_id), str(int(current or "0") + 1))
    return changed


def remove_admin(user_id: int) -> bool:
    normalized = _validate_user_id(user_id)
    if settings.SUPER_ADMIN_ID == user_id:
        raise ValueError("super-admin cannot be removed")
    changed = bool(get_store().srem(ADMINS_KEY, normalized))
    if changed:
        current = get_store().get(_version_key(user_id))
        get_store().set(_version_key(user_id), str(int(current or "0") + 1))
    return changed


def admin_version(user_id: int) -> int:
    raw = get_store().get(_version_key(user_id))
    try:
        return int(raw or "0")
    except ValueError:
        return 0


def is_admin(user_id: int | None) -> bool:
    """Check the immutable super-admin and the Redis administrator set."""
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        return False

    super_admin_id = settings.SUPER_ADMIN_ID
    if (
        not isinstance(super_admin_id, bool)
        and isinstance(super_admin_id, int)
        and user_id == super_admin_id
    ):
        return True

    return get_store().sismember(ADMINS_KEY, str(user_id))
