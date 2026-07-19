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
    tone_mode: str = "preset",
    tone_preset: str = "neutral",
    custom_system_prompt: str | None | object = _UNSET,
    chat_id: int | None = None,
    judge_default_n: int | object = _UNSET,
) -> dict[str, Any]:
    if scope not in {"global", "chat"}:
        raise ValueError("scope must be global or chat")
    if scope == "chat" and chat_id is None:
        raise ValueError("chat scope requires chat_id")
    current = get_config(chat_id)["effective"] if scope == "chat" else (
        _read(GLOBAL_CONFIG_KEY) or dict(DEFAULT_CONFIG)
    )
    prompt_supplied = custom_system_prompt is not _UNSET
    judge_supplied = judge_default_n is not _UNSET
    if not prompt_supplied:
        custom_system_prompt = current["custom_system_prompt"]
    if not judge_supplied:
        judge_default_n = current["judge_default_n"]
    value = validate_config(
        {
            "tone_mode": tone_mode,
            "tone_preset": tone_preset,
            "custom_system_prompt": custom_system_prompt,
            "judge_default_n": judge_default_n,
        }
    )
    if scope == "chat":
        partial = {"tone_mode": tone_mode, "tone_preset": tone_preset}
        if prompt_supplied or tone_mode == "custom":
            partial["custom_system_prompt"] = custom_system_prompt
        if judge_supplied:
            partial["judge_default_n"] = judge_default_n
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
    if tone_preset == "clear":
        encoded = ""
    else:
        encoded = json.dumps(
            {"tone_mode": "preset", "tone_preset": tone_preset},
            separators=(",", ":"),
        )
    store = get_store()
    if hasattr(store, "_values"):
        lock = store._lock  # type: ignore[attr-defined]
        with lock:
            store._purge_if_expired(command_key)  # type: ignore[attr-defined]
            store._purge_if_expired(target_key)  # type: ignore[attr-defined]
            if store._values.get(command_key) is not None:  # type: ignore[attr-defined]
                return False
            if tone_preset == "clear":
                store._values.pop(target_key, None)  # type: ignore[attr-defined]
                store._expiry.pop(target_key, None)  # type: ignore[attr-defined]
            elif scope == "global":
                raw = store._values.get(target_key)  # type: ignore[attr-defined]
                try:
                    value = json.loads(raw) if raw else dict(DEFAULT_CONFIG)
                except (TypeError, ValueError):
                    raise ValueError("stored configuration is corrupt") from None
                if not isinstance(value, dict):
                    raise ValueError("stored configuration is corrupt")
                value.update({"tone_mode": "preset", "tone_preset": tone_preset})
                store._set_unlocked(  # type: ignore[attr-defined]
                    target_key,
                    json.dumps(validate_config(value), separators=(",", ":")),
                    None,
                )
            else:
                store._set_unlocked(target_key, encoded, None)  # type: ignore[attr-defined]
            store._set_unlocked(command_key, "done", COMMAND_RECEIPT_SECONDS)  # type: ignore[attr-defined]
            store._set_unlocked(dedup_key, "done", 86_400)  # type: ignore[attr-defined]
            return True
    script = """
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
if ARGV[1] == '' then
  redis.call('DEL', KEYS[1])
else
local raw = redis.call('GET', KEYS[1])
local value
if raw then
  value = cjson.decode(raw)
elseif ARGV[4] == 'global' then
  value = {tone_mode='preset', tone_preset='neutral', custom_system_prompt=cjson.null, judge_default_n=20}
else
  value = {}
end
  local patch = cjson.decode(ARGV[1])
  value['tone_mode'] = patch['tone_mode']
  value['tone_preset'] = patch['tone_preset']
  redis.call('SET', KEYS[1], cjson.encode(value))
end
redis.call('SET', KEYS[2], 'done', 'EX', ARGV[2])
redis.call('SET', KEYS[3], 'done', 'EX', ARGV[3])
return 1
"""
    return bool(
        store._call(  # type: ignore[attr-defined]
            "eval",
            script,
            keys=[target_key, command_key, dedup_key],
            args=[encoded, str(COMMAND_RECEIPT_SECONDS), "86400", scope],
        )
    )


def record_command(update_id: int) -> bool:
    """Atomically record a read-only command receipt and final dedup marker."""
    store = get_store()
    command_key = f"cmd:{update_id}"
    dedup_key = f"dedup:update:{update_id}"
    if hasattr(store, "_values"):
        with store._lock:  # type: ignore[attr-defined]
            store._purge_if_expired(command_key)  # type: ignore[attr-defined]
            if store._values.get(command_key) is not None:  # type: ignore[attr-defined]
                return False
            store._set_unlocked(command_key, "done", COMMAND_RECEIPT_SECONDS)  # type: ignore[attr-defined]
            store._set_unlocked(dedup_key, "done", 86_400)  # type: ignore[attr-defined]
            return True
    script = "if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end; redis.call('SET', KEYS[1], 'done', 'EX', ARGV[1]); redis.call('SET', KEYS[2], 'done', 'EX', ARGV[2]); return 1"
    return bool(store._call("eval", script, keys=[command_key, dedup_key], args=[str(COMMAND_RECEIPT_SECONDS), "86400"]))  # type: ignore[attr-defined]
