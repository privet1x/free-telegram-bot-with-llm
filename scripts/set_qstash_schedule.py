"""Manage the QStash schedule that invokes scheduled Telegram banter.

Usage from the project root:
    python scripts/set_qstash_schedule.py set
    python scripts/set_qstash_schedule.py info
    python scripts/set_qstash_schedule.py delete

The schedule replaces Vercel Cron, which is not available every 20 minutes on
the Hobby plan. It forwards the existing CRON_SECRET to the protected route.
"""

from __future__ import annotations

import os
import sys

# Allows running as `python scripts/set_qstash_schedule.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qstash import QStash  # noqa: E402

from app.settings import is_https_base_url, settings  # noqa: E402

SCHEDULE_ID = "kulajaj-banter-20m"
CRON_EXPRESSION = "*/20 * * * *"
CRON_PATH = "/api/cron/banter"


def _require(name: str, value: str) -> str:
    if not value:
        print(f"ERROR: {name} is not set (check .env / Vercel Env).")
        raise SystemExit(1)
    return value


def _destination() -> str:
    base = _require("PUBLIC_BASE_URL", settings.PUBLIC_BASE_URL).rstrip("/")
    if not is_https_base_url(base):
        print("ERROR: PUBLIC_BASE_URL must be an HTTPS origin without a path.")
        raise SystemExit(1)
    return f"{base}{CRON_PATH}"


def _client() -> QStash:
    token = _require("QSTASH_TOKEN", settings.QSTASH_TOKEN)
    base_url = _require("QSTASH_URL", settings.QSTASH_URL).rstrip("/")
    if not is_https_base_url(base_url):
        print("ERROR: QSTASH_URL must be an HTTPS origin without a path.")
        raise SystemExit(1)
    return QStash(token, base_url=base_url)


def set_schedule() -> None:
    secret = _require("CRON_SECRET", settings.CRON_SECRET)
    destination = _destination()
    schedule_id = _client().schedule.create_json(
        destination=destination,
        cron=CRON_EXPRESSION,
        body={},
        method="POST",
        headers={"Authorization": f"Bearer {secret}"},
        schedule_id=SCHEDULE_ID,
        retries=3,
        label="kulajaj scheduled banter",
    )
    print(
        "QStash schedule set: "
        f"id={schedule_id}; cron={CRON_EXPRESSION}; destination={destination}"
    )


def info_schedule() -> None:
    schedule = _client().schedule.get(SCHEDULE_ID)
    print(f"schedule id : {schedule.schedule_id}")
    print(f"destination : {schedule.destination}")
    print(f"cron        : {schedule.cron}")
    print(f"paused      : {schedule.paused}")
    print(f"next run    : {schedule.next_schedule_time}")


def delete_schedule() -> None:
    _client().schedule.delete(SCHEDULE_ID)
    print(f"QStash schedule deleted: {SCHEDULE_ID}")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    command = arguments[:1]
    if command == ["set"]:
        set_schedule()
        return 0
    if command == ["info"]:
        info_schedule()
        return 0
    if command == ["delete"]:
        delete_schedule()
        return 0
    print("Usage: python scripts/set_qstash_schedule.py [set|info|delete]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
