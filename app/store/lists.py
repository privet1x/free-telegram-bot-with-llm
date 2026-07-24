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
# Keep the legacy judge value readable until old Redis records expire. Current
# API schemas accept only explicit and auto.
_SCOPES = {"explicit", "auto", "judge"}
_MAX_PROMPT = 8_000
MAX_LISTS = 100
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
        raise ValueError("stored list is corrupt") from None
    if not isinstance(value, dict):
        raise ValueError("stored list is corrupt")
    try:
        item = _validate(value)
    except (TypeError, ValueError):
        raise ValueError("stored list is corrupt") from None
    if item["slug"] != slug:
        raise ValueError("stored list is corrupt")
    return item


def all_lists() -> list[dict[str, Any]]:
    store = get_store()
    slugs = sorted(store.smembers(LISTS_INDEX_KEY))
    values: list[dict[str, Any]] = []
    for slug, raw in zip(slugs, store.get_many([_key(slug) for slug in slugs])):
        if raw is None:
            raise ValueError("stored list index is corrupt")
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("stored list is corrupt") from None
        if not isinstance(item, dict):
            raise ValueError("stored list is corrupt")
        try:
            normalized = _validate(item)
        except (TypeError, ValueError):
            raise ValueError("stored list is corrupt") from None
        if normalized["slug"] != slug:
            raise ValueError("stored list index is corrupt")
        values.append(normalized)
    return sorted(values, key=lambda item: (-int(item["priority"]), str(item["slug"])))


def create(value: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    item = _validate(value)
    if item["slug"] == IGNORE_SLUG and not force:
        raise ValueError("ignore is reserved")
    status = get_store().indexed_json_put(
        LISTS_INDEX_KEY,
        _key(item["slug"]),
        item["slug"],
        json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        create_only=not force,
        max_items=MAX_LISTS,
    )
    if status == "exists":
        raise ValueError("list already exists")
    if status == "limit":
        raise ValueError(f"at most {MAX_LISTS} lists are allowed")
    return item


def update(slug: str, value: dict[str, Any]) -> dict[str, Any]:
    if slug == IGNORE_SLUG:
        raise ValueError("ignore is reserved")
    current = get(slug)
    if current is None:
        raise KeyError(slug)
    candidate = dict(current)
    candidate.update(value)
    new_slug = value.get("slug", slug)
    if new_slug == IGNORE_SLUG:
        raise ValueError("ignore is reserved")
    candidate["slug"] = new_slug
    item = _validate(candidate)
    status = get_store().rename_indexed_set(
        LISTS_INDEX_KEY,
        _key(slug),
        _key(item["slug"]),
        f"list:{_slug(slug)}:members",
        f"list:{item['slug']}:members",
        slug,
        item["slug"],
        json.dumps(item, ensure_ascii=False, separators=(",", ":")),
    )
    if status == "missing":
        raise KeyError(slug)
    if status == "exists":
        raise ValueError("list already exists")
    return item


def delete(slug: str) -> bool:
    if slug == IGNORE_SLUG:
        raise ValueError("ignore is reserved")
    removed = get_store().list_delete(
        LISTS_INDEX_KEY,
        _key(slug),
        f"list:{_slug(slug)}:members",
        slug,
    )
    return bool(removed)


def add_member(slug: str, user_id: int) -> bool:
    status = get_store().list_member_add(
        _key(slug), f"list:{_slug(slug)}:members", _user(user_id)
    )
    if status == "missing":
        raise KeyError(slug)
    return status == "added"


def remove_member(slug: str, user_id: int) -> bool:
    return bool(get_store().srem(f"list:{_slug(slug)}:members", _user(user_id)))


def is_member(slug: str, user_id: int) -> bool:
    return get_store().sismember(f"list:{_slug(slug)}:members", _user(user_id))


def member_ids(slug: str) -> list[int]:
    if get(slug) is None:
        raise KeyError(slug)
    result: list[int] = []
    for raw in get_store().smembers(f"list:{_slug(slug)}:members"):
        try:
            user_id = int(raw)
        except (TypeError, ValueError):
            continue
        if user_id > 0:
            result.append(user_id)
    return sorted(set(result))


def remove_user_from_all(user_id: int) -> int:
    member = _user(user_id)
    return sum(
        get_store().srem(f"list:{item['slug']}:members", member)
        for item in all_lists()
    )


def member_lists(user_id: int, kind: str) -> list[dict[str, Any]]:
    if kind not in _SCOPES:
        raise ValueError("invalid list scope")
    candidates = [
        item
        for item in all_lists()
        if item.get("slug") != IGNORE_SLUG
        and item.get("enabled")
        and kind in item.get("applies_to", [])
    ]
    memberships = get_store().set_memberships(
        [f"list:{item['slug']}:members" for item in candidates], _user(user_id)
    )
    result = [
        item for item, is_current_member in zip(candidates, memberships)
        if is_current_member
    ]
    global _cap_overflow_count
    if len(result) > settings.MAX_LIST_POLICIES:
        _cap_overflow_count += 1
    return result[: settings.MAX_LIST_POLICIES]
