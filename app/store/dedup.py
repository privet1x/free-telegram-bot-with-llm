"""Completion markers for Telegram updates.

The marker means that all idempotent persistence for the update succeeded.  It
must therefore be written last: a failed history write remains safe to retry.
"""

from __future__ import annotations

from app.store.redis import get_store

DEDUP_TTL_SECONDS = 24 * 60 * 60


def dedup_key(update_id: int) -> str:
    return f"dedup:update:{update_id}"


def already_seen(update_id: int) -> bool:
    """Return whether an update was completely persisted before."""
    return get_store().get(dedup_key(update_id)) is not None


def mark_seen(update_id: int) -> bool:
    """Atomically mark successful completion.

    The return value elects the winner when duplicate webhook requests race.
    Only the winner may form a command response; all persistence before this
    call must be idempotent.
    """
    return get_store().set_nx(dedup_key(update_id), "1", ex=DEDUP_TTL_SECONDS)
