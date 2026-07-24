"""Versioned immutable Telegram job-snapshot contract."""

from __future__ import annotations

from typing import Final

JOB_SNAPSHOT_VERSION: Final = 2
SUPPORTED_JOB_KINDS: Final = frozenset(
    {
        "reply",
        "auto_rule",
        "think",
        "google",
        "keyword",
        "scheduled",
        "image_memory",
    }
)

# Persistence reserves deterministic headroom for the verified first-name
# prefix, degraded-search disclosure, and up to three bounded source lines.
MAX_GENERATED_RESPONSE_CHARS: Final = 64_000
MAX_SAVED_ANSWER_CHARS: Final = 80_000
