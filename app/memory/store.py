"""Bounded memory storage with immutable checked-in participant shards.

Static shards are loaded from the deployment package and are never written by
runtime code. Each participant's gathered shard is a bounded JSON document in
Redis because Vercel's filesystem is not durable; it records Telegram-authored
messages and fallible model observations under that sender's numeric ID. All
gathered data is lower priority than the immutable super-context and static
shards.
"""

from __future__ import annotations

import json
import re
import threading
import time
import asyncio
import secrets
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.store.redis import get_store
from app.metrics import timed

MAX_SHARD_CHARS = 8_000
MAX_TOTAL_STATIC_CHARS = 48_000
MAX_GATHERED_ITEMS = 24
GATHERED_MAX_CHARS = 12_000
GATHERED_RETENTION_SECONDS = 2_592_000
LOBOTOMY_COOLDOWN_SECONDS = 1_200
_GATHERED_LOCK_SECONDS = 60
_MANIFEST_PATH = Path(__file__).with_name("manifest.json")
_SHARDS_PATH = Path(__file__).with_name("shards")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_cache_lock = threading.RLock()
_static_cache: dict[int, str] | None = None
_always_static_cache: frozenset[int] | None = None
_gather_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory")
_gather_slots = threading.BoundedSemaphore(32)


def _decode_manifest() -> list[dict[str, Any]]:
    try:
        value = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("memory manifest is invalid") from exc
    participants = value.get("participants") if isinstance(value, dict) else None
    if not isinstance(participants, list):
        raise RuntimeError("memory manifest is invalid")
    result: list[dict[str, Any]] = []
    ids: set[int] = set()
    slugs: set[str] = set()
    for item in participants:
        if not isinstance(item, dict):
            raise RuntimeError("memory manifest entry is invalid")
        user_id = item.get("user_id")
        slug = item.get("slug")
        always_include = item.get("always_include", False)
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(slug, str)
            or _SLUG_RE.fullmatch(slug) is None
            or not isinstance(always_include, bool)
            or user_id in ids
            or slug in slugs
        ):
            raise RuntimeError("memory manifest entry is invalid")
        ids.add(user_id)
        slugs.add(slug)
        result.append(
            {"user_id": user_id, "slug": slug, "always_include": always_include}
        )
    return result


def _load_static() -> tuple[dict[int, str], frozenset[int]]:
    manifest = _decode_manifest()
    loaded: dict[int, str] = {}
    always: set[int] = set()
    total = 0
    for item in manifest:
        path = _SHARDS_PATH / f"memory-shard-{item['slug']}.md"
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError("configured memory shard is missing") from exc
        if not content or len(content) > MAX_SHARD_CHARS:
            raise RuntimeError("configured memory shard is invalid")
        total += len(content)
        if total > MAX_TOTAL_STATIC_CHARS:
            raise RuntimeError("configured static memory is too large")
        loaded[item["user_id"]] = content
        if item["always_include"]:
            always.add(item["user_id"])
    return loaded, frozenset(always)


def _ensure_static_cache() -> None:
    global _static_cache, _always_static_cache
    with _cache_lock:
        if _static_cache is None or _always_static_cache is None:
            _static_cache, _always_static_cache = _load_static()


def static_shards() -> dict[int, str]:
    """Return a cached copy of immutable static memory."""
    _ensure_static_cache()
    with _cache_lock:
        return dict(_static_cache or {})


def always_static_user_ids() -> frozenset[int]:
    """Return IDs whose immutable facts load into every reply in their chat.

    Participants flagged ``always_include`` in the manifest stay in the prompt
    even when they are not part of the current conversation window, so the bot
    keeps their stable context (nicknames, running jokes) available on demand.
    """
    _ensure_static_cache()
    with _cache_lock:
        return _always_static_cache or frozenset()


def reload_static_shards() -> dict[int, str]:
    """Reload checked-in files after a deployment or explicit cache reset."""
    global _static_cache, _always_static_cache
    loaded, always = _load_static()
    with _cache_lock:
        _static_cache = loaded
        _always_static_cache = always
        return dict(loaded)


def _epoch_key(chat_id: int) -> str:
    return f"memory:epoch:{chat_id}"


def _gathered_key(chat_id: int, user_id: int) -> str:
    return f"memory:gathered{user_id}"


def _legacy_gathered_key(chat_id: int, user_id: int) -> str:
    return f"memory:gathered:{chat_id}:{user_id}"


def _gathered_index_key(chat_id: int) -> str:
    return f"memory:gathered:index:{chat_id}"


def _user_tombstone_key(chat_id: int, user_id: int) -> str:
    return f"memory:gathered:tombstone:{chat_id}:{user_id}"


def _tombstone_before(store: Any, chat_id: int, user_id: int) -> int | None:
    raw = store.get(_user_tombstone_key(chat_id, user_id))
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value > 0 else None


def _gathered_chat_lock_key(chat_id: int) -> str:
    return f"memory:gathered:chat-lock:{chat_id}"


@contextmanager
def _gathered_write_lock(chat_id: int, user_id: int):
    """Serialize gathered writes with chat-wide lobotomy fencing."""
    store = get_store()
    chat_key = _gathered_chat_lock_key(chat_id)
    chat_token = secrets.token_urlsafe(12)
    if not store.set_nx(chat_key, chat_token, ex=_GATHERED_LOCK_SECONDS):
        yield None
        return
    user_key = f"memory:gathered:lock:{chat_id}:{user_id}"
    user_token = secrets.token_urlsafe(12)
    if not store.set_nx(user_key, user_token, ex=_GATHERED_LOCK_SECONDS):
        store.delete_if_value(chat_key, chat_token)
        yield None
        return
    try:
        yield store
    finally:
        store.delete_if_value(user_key, user_token)
        store.delete_if_value(chat_key, chat_token)


@contextmanager
def _gathered_chat_lock(chat_id: int):
    """Serialize lobotomy's epoch increment/deletion with gathered writers."""
    store = get_store()
    key = _gathered_chat_lock_key(chat_id)
    token = secrets.token_urlsafe(12)
    if not store.set_nx(key, token, ex=_GATHERED_LOCK_SECONDS):
        yield None
        return
    try:
        yield store
    finally:
        store.delete_if_value(key, token)


def current_epoch(chat_id: int) -> int:
    raw = get_store().get(_epoch_key(chat_id))
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _bounded(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _normalize_observations(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry_type = item.get("entry_type", "observation")
        if entry_type not in {"message", "image", "observation"}:
            continue
        text = _bounded(item.get("text"), 600)
        image_analysis = _bounded(item.get("image_analysis"), 1_600)
        if entry_type == "observation" and not text:
            continue
        if entry_type in {"message", "image"} and not text and not image_analysis:
            text = "[изображение]" if entry_type == "image" else ""
        source_id = item.get("source_message_id")
        timestamp = item.get("timestamp")
        confidence = item.get("confidence")
        provenance = _bounded(item.get("provenance"), 80)
        user_id = item.get("user_id")
        name = _bounded(item.get("name"), 64)
        memory_epoch = item.get("memory_epoch")
        if (
            isinstance(source_id, bool)
            or not isinstance(source_id, int)
            or source_id <= 0
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp <= 0
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
            or not provenance
        ):
            continue
        normalized: dict[str, object] = {
            "entry_type": entry_type,
            "text": text,
            "source_message_id": source_id,
            "timestamp": timestamp,
            "confidence": float(confidence),
            "provenance": provenance,
        }
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0:
            normalized["user_id"] = user_id
        if name:
            normalized["name"] = name
        if isinstance(memory_epoch, int) and not isinstance(memory_epoch, bool) and memory_epoch >= 0:
            normalized["memory_epoch"] = memory_epoch
        if image_analysis:
            normalized["image_analysis"] = image_analysis
        image = item.get("image")
        if isinstance(image, dict):
            normalized_image = {
                key: image[key]
                for key in ("mime_type", "width", "height", "file_size")
                if key in image and image[key] is not None
            }
            if normalized_image:
                normalized["image"] = normalized_image
        result.append(normalized)
    return result[-MAX_GATHERED_ITEMS:]


def gathered_for_user(chat_id: int, user_id: int) -> list[dict[str, object]]:
    store = get_store()
    raw = store.get(_gathered_key(chat_id, user_id))
    if raw is None:
        raw = store.get(_legacy_gathered_key(chat_id, user_id))
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    current = current_epoch(chat_id)
    return [
        item
        for item in _normalize_observations(value)
        if item.get("memory_epoch", 0) == current
    ]


def gathered_for_users(chat_id: int, user_ids: set[int]) -> list[dict[str, object]]:
    """Return only bounded observations for participants in the request."""
    result: list[dict[str, object]] = []
    for user_id in sorted(user_ids):
        for item in gathered_for_user(chat_id, user_id):
            enriched = dict(item)
            enriched["user_id"] = user_id
            result.append(enriched)
    return result[-MAX_GATHERED_ITEMS:]


def static_for_users(user_ids: set[int]) -> list[dict[str, object]]:
    shards = static_shards()
    return [
        {"user_id": user_id, "text": shards[user_id]}
        for user_id in sorted(user_ids)
        if user_id in shards
    ]


def _write_observation(
    chat_id: int,
    user_id: int,
    observation: dict[str, object],
    expected_epoch: int | None = None,
) -> bool:
    if expected_epoch is not None and expected_epoch != current_epoch(chat_id):
        return False
    with _gathered_write_lock(chat_id, user_id) as store:
        if store is None:
            return False
        if expected_epoch is None:
            expected_epoch = current_epoch(chat_id)
        observation["memory_epoch"] = expected_epoch
        tombstone = _tombstone_before(store, chat_id, user_id)
        timestamp = observation.get("timestamp")
        if tombstone is not None and isinstance(timestamp, int) and timestamp <= tombstone:
            return False
        if tombstone is not None:
            store.delete(_user_tombstone_key(chat_id, user_id))
        existing = gathered_for_user(chat_id, user_id)
        text = str(observation["text"])
        if any(item.get("text") == text for item in existing):
            return False
        updated = _normalize_observations([*existing, observation])
        encoded = json.dumps(updated, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > GATHERED_MAX_CHARS:
            updated = updated[-max(1, len(updated) // 2):]
            encoded = json.dumps(updated, ensure_ascii=False, separators=(",", ":"))
        if expected_epoch is not None and expected_epoch != current_epoch(chat_id):
            return False
        store.set(_gathered_key(chat_id, user_id), encoded, ex=GATHERED_RETENTION_SECONDS)
        store.sadd(_gathered_index_key(chat_id), str(user_id))
        return True


def record_message(
    *,
    chat_id: int,
    user_id: int,
    name: str,
    message_id: int,
    text: str,
    timestamp: int,
    image: dict[str, object] | None = None,
    is_edited: bool = False,
    memory_epoch: int | None = None,
) -> bool:
    """Append one Telegram-authored message to that user's durable shard.

    The author ID and first name are supplied by Telegram parsing code, never by
    message content. Replays replace the same message ID, and edits therefore
    cannot create duplicate shard entries.
    """
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or isinstance(message_id, bool)
        or not isinstance(message_id, int)
        or message_id <= 0
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp <= 0
        or not isinstance(name, str)
        or not name.strip()
        or (not text.strip() and image is None)
        or memory_epoch is not None and memory_epoch != current_epoch(chat_id)
    ):
        return False
    entry_type = "image" if image is not None else "message"
    observation: dict[str, object] = {
        "entry_type": entry_type,
        "user_id": user_id,
        "name": " ".join(name.split())[:64],
        "text": text.strip()[:600],
        "source_message_id": message_id,
        "timestamp": timestamp,
        "confidence": 1.0,
        "provenance": "telegram_message",
    }
    if image is not None:
        observation["image"] = {
            key: image[key]
            for key in ("mime_type", "width", "height", "file_size")
            if key in image and image[key] is not None
        }
    with _gathered_write_lock(chat_id, user_id) as store:
        if store is None:
            return False
        if memory_epoch is not None and memory_epoch != current_epoch(chat_id):
            return False
        observation["memory_epoch"] = current_epoch(chat_id)
        tombstone = _tombstone_before(store, chat_id, user_id)
        if tombstone is not None and timestamp <= tombstone:
            return False
        if tombstone is not None:
            store.delete(_user_tombstone_key(chat_id, user_id))
        existing = gathered_for_user(chat_id, user_id)
        updated = [
            item
            for item in existing
            if item.get("source_message_id") != message_id
            or item.get("entry_type") not in {"message", "image"}
        ]
        updated.append(observation)
        encoded = json.dumps(
            _normalize_observations(updated),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        while len(encoded) > GATHERED_MAX_CHARS and len(updated) > 1:
            updated.pop(0)
            encoded = json.dumps(
                _normalize_observations(updated),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if memory_epoch is not None and memory_epoch != current_epoch(chat_id):
            return False
        store.set(_gathered_key(chat_id, user_id), encoded, ex=GATHERED_RETENTION_SECONDS)
        store.sadd(_gathered_index_key(chat_id), str(user_id))
        return True


def attach_image_analysis(
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    analysis: str,
    memory_epoch: int | None = None,
) -> bool:
    """Attach bounded Gemma OCR/description to the sender's image entry."""
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or isinstance(message_id, bool)
        or not isinstance(message_id, int)
        or message_id <= 0
        or not analysis.strip()
        or memory_epoch is not None and memory_epoch != current_epoch(chat_id)
    ):
        return False
    with _gathered_write_lock(chat_id, user_id) as store:
        if store is None:
            return False
        current = gathered_for_user(chat_id, user_id)
        changed = False
        for item in current:
            if (
                item.get("source_message_id") == message_id
                and item.get("entry_type") == "image"
            ):
                item["image_analysis"] = " ".join(analysis.split())[:1_600]
                item["provenance"] = "gemma_image_analysis"
                item["confidence"] = 0.85
                changed = True
                break
        if not changed:
            return False
        if memory_epoch is not None and memory_epoch != current_epoch(chat_id):
            return False
        encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        while len(encoded) > GATHERED_MAX_CHARS and len(current) > 1:
            current.pop(0)
            encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        if memory_epoch is not None and memory_epoch != current_epoch(chat_id):
            return False
        store.set(
            _gathered_key(chat_id, user_id),
            encoded,
            ex=GATHERED_RETENTION_SECONDS,
        )
        store.sadd(_gathered_index_key(chat_id), str(user_id))
        return True


def observe_message(
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    text: str,
    timestamp: int,
    is_bot: bool = False,
    is_replayed_edit: bool = False,
    memory_epoch: int | None = None,
) -> bool:
    """Store one concise, fallible observation without blocking replies.

    A model-assisted ordinary extraction is attempted in the background when
    NVIDIA is configured. The bounded participant text is a safe fallback when
    that worker is unavailable; no provider call is made on the webhook path.
    """
    if is_bot or is_replayed_edit or not text.strip() or user_id <= 0:
        return False
    if memory_epoch is not None and memory_epoch != current_epoch(chat_id):
        return False
    with timed("memory.gathered_extraction"):
        candidate = _model_candidate(text) or " ".join(text.split())[:600]
    if not candidate or candidate.startswith("/"):
        return False
    return _write_observation(
        chat_id,
        user_id,
        {
            "text": candidate,
            "source_message_id": message_id,
            "timestamp": timestamp,
            "confidence": 0.35,
            "provenance": "human_message_fallback",
        },
        memory_epoch,
    )


def _model_candidate(text: str) -> str:
    """Try a cheap ordinary-inference extraction; safely fall back on failure."""
    from app.settings import settings

    if not settings.NVIDIA_API_KEY:
        return ""
    try:
        from app.llm.client import generate

        messages = [
            {
                "role": "system",
                "content": (
                    "Выдели не более одного краткого и предположительного факта, "
                    "который участник явно сообщил о себе. Верни только этот факт "
                    "обычным текстом или слово НЕТ, если полезного стабильного факта "
                    "нет. Никогда не делай выводов о личных данных, идентичности, "
                    "доступе или защищённых признаках."
                ),
            },
            {"role": "user", "content": text[:2_000]},
        ]
        result = asyncio.run(generate(messages, thinking=False))
        normalized = " ".join(result.split())[:600]
        if not normalized or normalized.casefold() in {"нет", "empty"}:
            return ""
        return normalized
    except Exception:
        return ""


def schedule_observation(**kwargs: object) -> None:
    """Queue a bounded observation without delaying Telegram ingestion."""
    if not _gather_slots.acquire(blocking=False):
        return
    try:
        future = _gather_executor.submit(observe_message, **kwargs)
        future.add_done_callback(lambda _future: _gather_slots.release())
    except RuntimeError:
        # Shutdown/reload should never fail the primary message path.
        _gather_slots.release()
        return


def invalidate_source_message(chat_id: int, user_id: int, message_id: int) -> bool:
    """Drop gathered observations derived from an edited human message."""
    expected_epoch = current_epoch(chat_id)
    with _gathered_write_lock(chat_id, user_id) as store:
        if store is None:
            return False
        current = gathered_for_user(chat_id, user_id)
        kept = [item for item in current if item.get("source_message_id") != message_id]
        if len(kept) == len(current):
            return False
        if expected_epoch != current_epoch(chat_id):
            return False
        if kept:
            if expected_epoch != current_epoch(chat_id):
                return False
            store.set(
                _gathered_key(chat_id, user_id),
                json.dumps(kept, ensure_ascii=False, separators=(",", ":")),
                ex=GATHERED_RETENTION_SECONDS,
            )
        else:
            store.delete(_gathered_key(chat_id, user_id))
            store.srem(_gathered_index_key(chat_id), str(user_id))
        return True


def _clear_gathered_unlocked(chat_id: int) -> int:
    store = get_store()
    user_ids = store.smembers(_gathered_index_key(chat_id))
    keys = [
        key
        for value in user_ids
        if value.isdecimal()
        for key in (
            _gathered_key(chat_id, int(value)),
            _legacy_gathered_key(chat_id, int(value)),
        )
    ]
    removed = store.delete(*keys, _gathered_index_key(chat_id)) if keys else store.delete(_gathered_index_key(chat_id))
    return int(removed)


def clear_gathered(chat_id: int) -> int:
    """Clear every gathered shard under the same fence as writers."""
    with _gathered_chat_lock(chat_id) as store:
        if store is None:
            return 0
        store.set(_epoch_key(chat_id), str(current_epoch(chat_id) + 1))
        return _clear_gathered_unlocked(chat_id)


def lobotomy(chat_id: int, actor_id: int | None) -> tuple[str, int]:
    """Reset mutable chat memory; the configured super-admin bypasses cooldown."""
    now = int(time.time())
    is_owner = actor_id is not None and actor_id > 0
    from app.settings import settings

    is_owner = is_owner and actor_id == settings.SUPER_ADMIN_ID
    cooldown_key = f"memory:lobotomy-cooldown:{chat_id}"
    lock_key = f"memory:lobotomy-lock:{chat_id}"
    store = get_store()
    if not is_owner:
        if not store.set_nx(cooldown_key, str(now), ex=LOBOTOMY_COOLDOWN_SECONDS):
            raw = store.get(cooldown_key)
            try:
                remaining = max(int(raw or now) + LOBOTOMY_COOLDOWN_SECONDS - now, 1)
            except (TypeError, ValueError):
                remaining = LOBOTOMY_COOLDOWN_SECONDS
            return "cooldown", remaining
    lock_token = secrets.token_urlsafe(12)
    if not store.set_nx(lock_key, lock_token, ex=10):
        return "cooldown", 1
    try:
        with _gathered_chat_lock(chat_id) as memory_lock:
            if memory_lock is None:
                return "cooldown", 1
            # The epoch and deletion are fenced against every gathered writer.
            next_epoch = current_epoch(chat_id) + 1
            memory_lock.set(_epoch_key(chat_id), str(next_epoch))
            _clear_gathered_unlocked(chat_id)
            with _cache_lock:
                reload_static_shards()
            return "reset", 0
    finally:
        store.delete_if_value(lock_key, lock_token)


def purge_user(chat_id: int, user_id: int) -> int:
    """Remove gathered memory and its index entry for privacy deletion."""
    with _gathered_chat_lock(chat_id) as store:
        if store is None:
            return 0
        removed = store.delete(
            _gathered_key(chat_id, user_id),
            _legacy_gathered_key(chat_id, user_id),
        )
        # Set removal is available through the generic store API.
        removed += store.srem(_gathered_index_key(chat_id), str(user_id))
        store.set(
            _user_tombstone_key(chat_id, user_id),
            str(int(time.time())),
            ex=GATHERED_RETENTION_SECONDS,
        )
        return int(removed)
