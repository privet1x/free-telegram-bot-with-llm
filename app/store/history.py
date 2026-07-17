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
    )


def append(chat_id: int, record: dict) -> None:
    """Backward-compatible alias; all writes now have upsert semantics."""
    upsert(chat_id, record)


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
