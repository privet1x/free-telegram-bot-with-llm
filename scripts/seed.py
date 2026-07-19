"""Seed the reserved ignore list and conservative demo policies."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.settings import settings  # noqa: E402
from app.store import lists, rules  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="replace demo records")
    args = parser.parse_args()
    if settings.TELEGRAM_ALLOWED_CHAT_ID is None:
        print("TELEGRAM_ALLOWED_CHAT_ID is required", file=sys.stderr)
        return 1
    try:
        list_records = [
            {
                "slug": "ignore",
                "title": "Ignore automatic replies",
                "enabled": True,
                "priority": 0,
                "applies_to": ["auto"],
                "injected_prompt": "",
            },
            {
                "slug": "aggressive",
                "title": "Sarcastic response",
                "enabled": True,
                "priority": 50,
                "applies_to": ["explicit", "auto", "judge"],
                "injected_prompt": "Use dry sarcasm without personal attacks.",
            },
        ]
        for item in list_records:
            if lists.get(item["slug"]) is None:
                lists.create(item, force=item["slug"] == lists.IGNORE_SLUG)
            elif args.force:
                lists.create(item, force=True)
        rule_record = {
                "id": "nonsense",
                "enabled": True,
                "priority": 50,
                "scope": "all",
                "match": {"type": "substring", "value": "nonsense"},
                "instruction": "Explain calmly why the argument is not nonsense.",
                "stop_processing": False,
            }
        if rules.get(rule_record["id"]) is None or args.force:
            rules.create(rule_record, force=args.force)
    except Exception as exc:
        print(f"seed failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Seed completed for the configured allowed chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
