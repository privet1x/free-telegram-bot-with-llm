"""Seed the reserved ignore list and conservative demo policies."""

from __future__ import annotations

import argparse
import sys

from app.settings import settings
from app.store import lists, rules


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="replace demo records")
    args = parser.parse_args()
    if settings.TELEGRAM_ALLOWED_CHAT_ID is None:
        print("TELEGRAM_ALLOWED_CHAT_ID is required", file=sys.stderr)
        return 1
    try:
        lists.create(
            {
                "slug": "ignore",
                "title": "Ignore automatic replies",
                "enabled": True,
                "priority": 0,
                "applies_to": ["auto"],
                "injected_prompt": "",
            },
            force=args.force,
        )
        lists.create(
            {
                "slug": "aggressive",
                "title": "Sarcastic response",
                "enabled": True,
                "priority": 50,
                "applies_to": ["explicit", "auto", "judge"],
                "injected_prompt": "Use dry sarcasm without personal attacks.",
            },
            force=args.force,
        )
        rules.create(
            {
                "id": "nonsense",
                "enabled": True,
                "priority": 50,
                "scope": "all",
                "match": {"type": "substring", "value": "nonsense"},
                "instruction": "Explain calmly why the argument is not nonsense.",
                "stop_processing": False,
            },
            force=args.force,
        )
    except Exception as exc:
        print(f"seed failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Seed completed for the configured allowed chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
