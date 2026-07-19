"""Validated tone configuration with global and allowed-chat overrides."""

from __future__ import annotations

import json
from typing import Any

from app.store.redis import get_store

GLOBAL_CONFIG_KEY = "cfg:global"
TONE_PRESETS = frozenset({"neutral", "serious", "scientist", "street", "sarcastic_robot"})
TONE_MODES = frozenset({"preset", "custom"})
DEFAULT_CONFIG: dict[str, Any] = {
    "tone_mode": "preset",
    "tone_preset": "neutral",
    "custom_system_prompt": None,
    "judge_default_n": 20,
}
_MAX_PROMPT_CHARS = 8_000
COMMAND_RECEIPT_SECONDS = 2_592_000
_UNSET = object()


def config_key(chat_id: int) -> str:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise ValueError("chat_id must be an integer")
    return f"cfg:{chat_id}"


def validate_config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    result = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in value:
            result[key] = value[key]
    mode = result["tone_mode"]
    preset = result["tone_preset"]
    prompt = result["custom_system_prompt"]
    judge_n = result["judge_default_n"]
    if mode not in TONE_MODES:
        raise ValueError("tone_mode must be preset or custom")
    if preset not in TONE_PRESETS:
        raise ValueError("tone_preset is invalid")
    if mode == "custom" and (
        not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS
    ):
        raise ValueError("custom_system_prompt is required in custom mode")
    if prompt is not None and (
        not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS
    ):
        raise ValueError("custom_system_prompt is invalid")
    if isinstance(judge_n, bool) or not isinstance(judge_n, int) or not 5 <= judge_n <= 30:
        raise ValueError("judge_default_n must be between 5 and 30")
    return result


def _read(key: str, *, missing_default: bool = True) -> dict[str, Any] | None:
    raw = get_store().get(key)
    if raw is None:
        return dict(DEFAULT_CONFIG) if missing_default else None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("stored configuration is corrupt") from None
    return validate_config(value)


def _write(key: str, value: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_config(value)
    get_store().set(key, json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
    return normalized


def get_config(chat_id: int | None = None) -> dict[str, Any]:
    """Return global, chat override, and field-by-field effective settings."""
    global_config = _read(GLOBAL_CONFIG_KEY) or dict(DEFAULT_CONFIG)
    override = _read_override(config_key(chat_id)) if chat_id is not None else None
    effective = dict(global_config)
    if override is not None:
        effective.update(override)
    return {"global": global_config, "chat_override": override, "effective": effective}


def _read_override(key: str) -> dict[str, Any] | None:
    raw = get_store().get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("stored configuration is corrupt") from None
    if not isinstance(value, dict):
        raise ValueError("stored configuration is corrupt")
    merged = dict(DEFAULT_CONFIG)
    merged.update(value)
    normalized = validate_config(merged)
    return {key: normalized[key] for key in value if key in DEFAULT_CONFIG}


def set_tone(
    scope: str,
    *,
    tone_mode: str | object = _UNSET,
    tone_preset: str | object = _UNSET,
    custom_system_prompt: str | None | object = _UNSET,
    chat_id: int | None = None,
    judge_default_n: int | object = _UNSET,
) -> dict[str, Any]:
    if scope not in {"global", "chat"}:
        raise ValueError("scope must be global or chat")
    if scope == "chat" and chat_id is None:
        raise ValueError("chat scope requires chat_id")
    configured = get_config(chat_id) if scope == "chat" else None
    current = (
        configured["effective"]
        if configured is not None
        else (_read(GLOBAL_CONFIG_KEY) or dict(DEFAULT_CONFIG))
    )
    supplied = {
        key: value
        for key, value in {
            "tone_mode": tone_mode,
            "tone_preset": tone_preset,
            "custom_system_prompt": custom_system_prompt,
            "judge_default_n": judge_default_n,
        }.items()
        if value is not _UNSET
    }
    if not supplied:
        raise ValueError("at least one tone field is required")
    candidate = dict(current)
    candidate.update(supplied)
    value = validate_config(candidate)
    if scope == "chat":
        partial = dict(configured["chat_override"] or {})
        partial.update(supplied)
        get_store().set(config_key(chat_id), json.dumps(partial, separators=(",", ":")))
        return value
    return _write(GLOBAL_CONFIG_KEY, value)


def clear_chat_override(chat_id: int) -> bool:
    return bool(get_store().delete(config_key(chat_id)))


def apply_tone_command(
    update_id: int,
    *,
    scope: str,
    tone_preset: str,
    chat_id: int,
) -> bool:
    """Atomically apply a command configuration and its receipts."""
    if scope not in {"global", "chat"} or (
        tone_preset not in TONE_PRESETS and not (scope == "chat" and tone_preset == "clear")
    ):
        raise ValueError("invalid tone command")
    command_key = f"cmd:{update_id}"
    dedup_key = f"dedup:update:{update_id}"
    target_key = GLOBAL_CONFIG_KEY if scope == "global" else config_key(chat_id)
    patch_json = (
        None
        if tone_preset == "clear"
        else json.dumps(
            {"tone_mode": "preset", "tone_preset": tone_preset},
            separators=(",", ":"),
        )
    )
    default_json = json.dumps(
        DEFAULT_CONFIG if scope == "global" else {}, separators=(",", ":")
    )
    return get_store().apply_json_patch_with_receipts(
        target_key,
        command_key,
        dedup_key,
        patch_json,
        default_json,
        COMMAND_RECEIPT_SECONDS,
        86_400,
    )


def record_command(update_id: int) -> bool:
    """Atomically record a read-only command receipt and final dedup marker."""
    command_key = f"cmd:{update_id}"
    dedup_key = f"dedup:update:{update_id}"
    return get_store().record_receipts_once(
        command_key, dedup_key, COMMAND_RECEIPT_SECONDS, 86_400
    )
