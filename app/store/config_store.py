"""Validated tone configuration with global and allowed-chat overrides."""

from __future__ import annotations

import json
from typing import Any

from app.store.redis import get_store

GLOBAL_CONFIG_KEY = "cfg:global"
TONE_PRESETS = frozenset(
    {"neutral", "serious", "scientist", "street", "sarcastic_bot"}
)
DEFAULT_CONFIG: dict[str, Any] = {"tone_preset": "neutral"}
COMMAND_RECEIPT_SECONDS = 2_592_000


def config_key(chat_id: int) -> str:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise ValueError("chat_id must be an integer")
    return f"cfg:{chat_id}"


def _canonical_preset(value: object) -> str:
    if value == "sarcastic_robot":
        return "sarcastic_bot"
    if not isinstance(value, str) or value not in TONE_PRESETS:
        raise ValueError("tone_preset is invalid")
    return value


def validate_config(value: object) -> dict[str, Any]:
    """Read current or legacy configuration into the immutable-core schema."""
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return {
        "tone_preset": _canonical_preset(
            value.get("tone_preset", DEFAULT_CONFIG["tone_preset"])
        )
    }


def _read(key: str, *, missing_default: bool = True) -> dict[str, Any] | None:
    raw = get_store().get(key)
    if raw is None:
        return dict(DEFAULT_CONFIG) if missing_default else None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("stored configuration is corrupt") from None
    return validate_config(value)


def _read_override(key: str) -> dict[str, Any] | None:
    """Read a chat override without inventing a tone for removed-only legacy data."""
    raw = get_store().get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("stored configuration is corrupt") from None
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    if "tone_preset" not in value:
        return None
    return {"tone_preset": _canonical_preset(value["tone_preset"])}


def _write(key: str, value: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_config(value)
    get_store().set(
        key,
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
    )
    return normalized


def get_config(chat_id: int | None = None) -> dict[str, Any]:
    """Return global, chat override, and effective tone settings."""
    global_config = _read(GLOBAL_CONFIG_KEY) or dict(DEFAULT_CONFIG)
    override = _read_override(config_key(chat_id)) if chat_id is not None else None
    effective = dict(global_config)
    if override is not None:
        effective.update(override)
    return {
        "global": global_config,
        "chat_override": override,
        "effective": effective,
    }


def set_tone(
    scope: str,
    *,
    tone_preset: str,
    chat_id: int | None = None,
) -> dict[str, Any]:
    if scope not in {"global", "chat"}:
        raise ValueError("scope must be global or chat")
    if scope == "chat" and chat_id is None:
        raise ValueError("chat scope requires chat_id")
    value = {"tone_preset": _canonical_preset(tone_preset)}
    target = GLOBAL_CONFIG_KEY if scope == "global" else config_key(chat_id)
    return _write(target, value)


def clear_chat_override(chat_id: int) -> bool:
    return bool(get_store().delete(config_key(chat_id)))


def apply_tone_command(
    update_id: int,
    *,
    tone_preset: str,
    chat_id: int,
) -> bool:
    """Atomically apply a public chat tone command and its receipts."""
    canonical = _canonical_preset(tone_preset)
    command_key = f"cmd:{update_id}"
    dedup_key = f"dedup:update:{update_id}"
    value = json.dumps(
        {"tone_preset": canonical},
        separators=(",", ":"),
    )
    return get_store().set_value_with_receipts(
        config_key(chat_id),
        command_key,
        dedup_key,
        value,
        COMMAND_RECEIPT_SECONDS,
        86_400,
    )


def record_command(update_id: int) -> bool:
    """Atomically record a read-only command receipt and final dedup marker."""
    command_key = f"cmd:{update_id}"
    dedup_key = f"dedup:update:{update_id}"
    return get_store().record_receipts_once(
        command_key,
        dedup_key,
        COMMAND_RECEIPT_SECONDS,
        86_400,
    )
