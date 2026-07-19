"""Canonical capped chat history.

Messages are upserted by Telegram ``message_id`` so edits and webhook retries do
not consume additional slots.  The store primitive performs replace/prepend and
trim atomically.
"""

from __future__ import annotations

import json
import time

from app.settings import settings
from app.store.redis import get_store

HISTORY_LIMIT = 30


def history_key(chat_id: int) -> str:
    return f"hist:{chat_id}"


def upsert(chat_id: int, record: dict) -> None:
    """Insert a message or replace the existing item with the same message ID."""
    message_id = record.get("message_id")
    timestamp = record.get("ts")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise ValueError("history record requires an integer message_id")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("history record requires an integer ts")
    get_store().list_upsert_json(
        history_key(chat_id),
        "message_id",
        str(message_id),
        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        HISTORY_LIMIT,
        ex=settings.HISTORY_RETENTION_SECONDS,
        prune_field="ts",
        min_value=int(time.time()) - settings.HISTORY_RETENTION_SECONDS,
        block_key=(
            f"privacy:job:{record['source_update_id']}"
            if isinstance(record.get("source_update_id"), int)
            and not isinstance(record.get("source_update_id"), bool)
            else None
        ),
    )


def recent(chat_id: int, n: int = HISTORY_LIMIT) -> list[dict]:
    """Return the last n messages, newest first."""
    if n <= 0:
        return []
    store = get_store()
    cutoff = int(time.time()) - settings.HISTORY_RETENTION_SECONDS
    store.list_prune_json(history_key(chat_id), "ts", cutoff)
    # Read the whole bounded list before filtering so a legacy/corrupt/expired
    # entry near the front does not hide a valid record just beyond `n`.
    raw = store.lrange(history_key(chat_id), 0, HISTORY_LIMIT - 1)
    out: list[dict] = []
    for item in raw:
        try:
            decoded = json.loads(item)
        except (ValueError, TypeError):
            continue
        timestamp = decoded.get("ts") if isinstance(decoded, dict) else None
        if (
            isinstance(decoded, dict)
            and not isinstance(timestamp, bool)
            and isinstance(timestamp, int)
            and timestamp >= cutoff
        ):
            out.append(decoded)
    return out[:n]


def purge_user(
    chat_id: int, user_id: int, outbound_message_ids: set[int] | None = None
) -> int:
    """Remove authored/private-derived records and redact replies atomically."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be positive")
    return get_store().list_privacy_filter(
        history_key(chat_id), user_id, outbound_message_ids or set()
    )


def purge_all(chat_id: int) -> bool:
    return bool(get_store().delete(history_key(chat_id)))


def remove_message_ids(chat_id: int, message_ids: set[int]) -> int:
    if not message_ids:
        return len(recent(chat_id))
    return get_store().list_remove_message_ids(history_key(chat_id), message_ids)
