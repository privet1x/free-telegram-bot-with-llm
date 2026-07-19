"""Allowed-group membership checks for assigned administrators."""

from __future__ import annotations

import json
import time

from app.settings import settings
from app.store import users
from app.store.redis import get_store
from app.telegram import client as telegram_client

MEMBERSHIP_CACHE_SECONDS = 300
ACTIVE_STATUSES = frozenset({"creator", "administrator", "member"})


def _key(user_id: int) -> str:
    return f"member:{settings.TELEGRAM_ALLOWED_CHAT_ID}:{user_id}"


def _positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("user_id must be positive")
    return value


def _profile(member: dict, expected_user_id: int) -> dict[str, object]:
    status = member.get("status")
    user = member.get("user")
    member_id = user.get("id") if isinstance(user, dict) else None
    if status not in ACTIVE_STATUSES or member_id != expected_user_id:
        raise PermissionError("user is not an active member of the allowed group")
    first = user.get("first_name") if isinstance(user, dict) else None
    last = user.get("last_name") if isinstance(user, dict) else None
    name = " ".join(
        part.strip()
        for part in (first, last)
        if isinstance(part, str) and part.strip()
    )
    username = user.get("username") if isinstance(user, dict) else None
    return {
        "id": expected_user_id,
        "username": username if isinstance(username, str) else None,
        "name": name or str(username or expected_user_id),
        "is_bot": bool(user.get("is_bot", False)) if isinstance(user, dict) else False,
    }


def require_group_member(
    user_id: int,
    *,
    seed_profile: bool = False,
    allow_cache: bool = False,
) -> dict[str, object]:
    """Require active membership, optionally using the five-minute cache."""
    normalized = _positive_id(user_id)
    chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise RuntimeError("allowed chat is not configured")
    store = get_store()
    if allow_cache:
        raw = store.get(_key(normalized))
        try:
            cached = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("active") is True:
            profile = cached.get("profile")
            if isinstance(profile, dict) and profile.get("id") == normalized:
                return profile

    member = telegram_client.get_chat_member(chat_id, normalized)
    profile = _profile(member, normalized)
    store.set(
        _key(normalized),
        json.dumps(
            {"active": True, "profile": profile},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        ex=MEMBERSHIP_CACHE_SECONDS,
    )
    if seed_profile:
        now = int(time.time())
        users.observe(
            {
                **profile,
                "last_seen_at": now,
                "last_update_id": 0,
            }
        )
    return profile
