"""Validated personal policy lists backed by the shared KV store."""

from __future__ import annotations

import json
import re
from typing import Any

from app.settings import settings
from app.store.redis import get_store

LISTS_INDEX_KEY = "lists:index"
IGNORE_SLUG = "ignore"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCOPES = {"explicit", "auto", "judge"}
_MAX_PROMPT = 8_000
_cap_overflow_count = 0


def policy_cap_overflow_count() -> int:
    return _cap_overflow_count


def _slug(slug: str) -> str:
    if not isinstance(slug, str) or _SLUG_RE.fullmatch(slug) is None:
        raise ValueError("invalid list slug")
    return slug


def _user(user_id: int) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be positive")
    return str(user_id)


def _validate(value: dict[str, Any]) -> dict[str, Any]:
    slug = _slug(str(value.get("slug", "")))
    title = value.get("title")
    prompt = value.get("injected_prompt")
    applies = value.get("applies_to", [])
    priority = value.get("priority", 0)
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise ValueError("title is invalid")
    if (
        not isinstance(prompt, str)
        or len(prompt) > _MAX_PROMPT
        or (slug != IGNORE_SLUG and not prompt.strip())
    ):
        raise ValueError("injected_prompt is invalid")
    if isinstance(priority, bool) or not isinstance(priority, int) or not -1000 <= priority <= 1000:
        raise ValueError("priority is invalid")
    if not isinstance(applies, list) or not applies or any(item not in _SCOPES for item in applies):
        raise ValueError("applies_to is invalid")
    return {
        "slug": slug,
        "title": title.strip(),
        "enabled": bool(value.get("enabled", True)),
        "priority": priority,
        "applies_to": sorted(set(applies)),
        "injected_prompt": prompt,
    }


def _key(slug: str) -> str:
    return f"list:{_slug(slug)}:meta"


def get(slug: str) -> dict[str, Any] | None:
    raw = get_store().get(_key(slug))
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def all_lists() -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for slug in sorted(get_store().smembers(LISTS_INDEX_KEY)):
        item = get(slug)
        if item is not None:
            values.append(item)
    return sorted(values, key=lambda item: (-int(item["priority"]), str(item["slug"])))


def create(value: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    item = _validate(value)
    if item["slug"] == IGNORE_SLUG and not force:
        raise ValueError("ignore is reserved")
    if get(item["slug"]) is not None and not force:
        raise ValueError("list already exists")
    get_store().set(_key(item["slug"]), json.dumps(item, separators=(",", ":")))
    get_store().sadd(LISTS_INDEX_KEY, item["slug"])
    return item


def update(slug: str, value: dict[str, Any]) -> dict[str, Any]:
    if slug == IGNORE_SLUG:
        raise ValueError("ignore is reserved")
    current = get(slug)
    if current is None:
        raise KeyError(slug)
    candidate = dict(current)
    candidate.update(value)
    candidate["slug"] = slug
    return create(candidate, force=True)


def delete(slug: str) -> bool:
    if slug == IGNORE_SLUG:
        raise ValueError("ignore is reserved")
    removed = get_store().delete(_key(slug), f"list:{_slug(slug)}:members")
    get_store().srem(LISTS_INDEX_KEY, slug)
    return bool(removed)


def add_member(slug: str, user_id: int) -> bool:
    if get(slug) is None:
        raise KeyError(slug)
    return bool(get_store().sadd(f"list:{_slug(slug)}:members", _user(user_id)))


def remove_member(slug: str, user_id: int) -> bool:
    return bool(get_store().srem(f"list:{_slug(slug)}:members", _user(user_id)))


def is_member(slug: str, user_id: int) -> bool:
    return get_store().sismember(f"list:{_slug(slug)}:members", _user(user_id))


def member_lists(user_id: int, kind: str) -> list[dict[str, Any]]:
    if kind not in _SCOPES:
        raise ValueError("invalid list scope")
    result = [
        item for item in all_lists()
        if item.get("slug") != IGNORE_SLUG
        and item.get("enabled")
        and kind in item.get("applies_to", [])
        and is_member(item["slug"], user_id)
    ]
    global _cap_overflow_count
    if len(result) > settings.MAX_LIST_POLICIES:
        _cap_overflow_count += 1
    return result[: settings.MAX_LIST_POLICIES]
