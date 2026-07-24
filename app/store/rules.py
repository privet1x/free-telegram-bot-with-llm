"""Deterministic, bounded text-rule matching."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from app.settings import settings
from app.store.redis import get_store

RULES_INDEX_KEY = "rules:index"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MATCH_TYPES = {"substring", "word", "phrase"}
# Keep the legacy judge value readable until old Redis records expire. Current
# API schemas accept only auto, explicit, and all.
_SCOPES = {"auto", "explicit", "judge", "all"}
_MAX_INSTRUCTION = 8_000
_MAX_MATCH_VALUE = 512
MAX_RULES = 100
_cap_overflow_count = 0


def policy_cap_overflow_count() -> int:
    return _cap_overflow_count


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(" " if unicodedata.category(char).startswith("P") else char for char in normalized)
    return " ".join(normalized.split())


def _validate(value: dict[str, Any]) -> dict[str, Any]:
    rule_id = value.get("id")
    if not isinstance(rule_id, str) or _ID_RE.fullmatch(rule_id) is None:
        raise ValueError("id is invalid")
    match = value.get("match")
    instruction = value.get("instruction")
    scope = value.get("scope")
    priority = value.get("priority", 0)
    if not isinstance(match, dict) or match.get("type") not in _MATCH_TYPES:
        raise ValueError("match.type is invalid")
    match_value = match.get("value")
    if (
        not isinstance(match_value, str)
        or len(match_value) > _MAX_MATCH_VALUE
        or not normalize_text(match_value)
    ):
        raise ValueError("match.value is invalid")
    if match.get("type") == "word" and len(normalize_text(match_value).split()) != 1:
        raise ValueError("word match requires exactly one token")
    if scope not in _SCOPES:
        raise ValueError("scope is invalid")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > _MAX_INSTRUCTION:
        raise ValueError("instruction is invalid")
    if isinstance(priority, bool) or not isinstance(priority, int) or not -1000 <= priority <= 1000:
        raise ValueError("priority is invalid")
    return {
        "id": rule_id,
        "enabled": bool(value.get("enabled", True)),
        "priority": priority,
        "scope": scope,
        "match": {"type": match["type"], "value": match_value},
        "instruction": instruction.strip(),
        "stop_processing": bool(value.get("stop_processing", False)),
    }


def _key(rule_id: str) -> str:
    if not isinstance(rule_id, str) or _ID_RE.fullmatch(rule_id) is None:
        raise ValueError("id is invalid")
    return f"rule:{rule_id}"


def get(rule_id: str) -> dict[str, Any] | None:
    raw = get_store().get(_key(rule_id))
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("stored rule is corrupt") from None
    if not isinstance(value, dict):
        raise ValueError("stored rule is corrupt")
    try:
        item = _validate(value)
    except (TypeError, ValueError):
        raise ValueError("stored rule is corrupt") from None
    if item["id"] != rule_id:
        raise ValueError("stored rule is corrupt")
    return item


def all_rules() -> list[dict[str, Any]]:
    store = get_store()
    rule_ids = sorted(store.smembers(RULES_INDEX_KEY))
    result: list[dict[str, Any]] = []
    for rule_id, raw in zip(
        rule_ids, store.get_many([_key(rule_id) for rule_id in rule_ids])
    ):
        if raw is None:
            raise ValueError("stored rule index is corrupt")
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("stored rule is corrupt") from None
        if not isinstance(item, dict):
            raise ValueError("stored rule is corrupt")
        try:
            normalized = _validate(item)
        except (TypeError, ValueError):
            raise ValueError("stored rule is corrupt") from None
        if normalized["id"] != rule_id:
            raise ValueError("stored rule index is corrupt")
        result.append(normalized)
    return sorted(
        result,
        key=lambda item: (-int(item.get("priority", 0)), str(item.get("id", ""))),
    )


def create(value: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    item = _validate(value)
    status = get_store().indexed_json_put(
        RULES_INDEX_KEY,
        _key(item["id"]),
        item["id"],
        json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        create_only=not force,
        max_items=MAX_RULES,
    )
    if status == "exists":
        raise ValueError("rule already exists")
    if status == "limit":
        raise ValueError(f"at most {MAX_RULES} rules are allowed")
    return item


def update(rule_id: str, value: dict[str, Any]) -> dict[str, Any]:
    current = get(rule_id)
    if current is None:
        raise KeyError(rule_id)
    candidate = dict(current)
    candidate.update(value)
    candidate["id"] = rule_id
    return create(candidate, force=True)


def delete(rule_id: str) -> bool:
    removed = get_store().indexed_delete(RULES_INDEX_KEY, _key(rule_id), rule_id)
    return bool(removed)


def matches(rule: dict[str, Any], text: str) -> bool:
    if not rule.get("enabled", True):
        return False
    match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    kind, value = match.get("type"), match.get("value")
    if kind not in _MATCH_TYPES or not isinstance(value, str):
        return False
    haystack = normalize_text(text)
    needle = normalize_text(value)
    if kind == "substring":
        return bool(needle and needle in haystack)
    if kind == "word":
        return bool(needle and needle in haystack.split())
    return bool(needle and re.search(rf"(?<!\S){re.escape(needle)}(?!\S)", haystack))


def resolve(text: str, scope: str) -> list[dict[str, Any]]:
    if scope not in _SCOPES:
        raise ValueError("scope is invalid")
    matched = [
        rule for rule in all_rules()
        if rule.get("scope") in {scope, "all"} and matches(rule, text)
    ]
    matched.sort(key=lambda item: (-int(item.get("priority", 0)), str(item.get("id", ""))))
    selected: list[dict[str, Any]] = []
    current_priority: int | None = None
    stop = False
    for rule in matched:
        priority = int(rule["priority"])
        if current_priority is not None and priority != current_priority and stop:
            break
        if current_priority != priority:
            current_priority = priority
            stop = False
        selected.append(rule)
        stop = stop or bool(rule.get("stop_processing"))
    global _cap_overflow_count
    if len(selected) > settings.MAX_RULE_POLICIES:
        _cap_overflow_count += 1
    return selected[: settings.MAX_RULE_POLICIES]
