"""Authenticated twenty-minute scheduled banter for the one allowed chat."""

from __future__ import annotations

import hmac
import random
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.memory import current_epoch
from app.queue.qstash import QStashPublishError, publish
from app.settings import settings
from app.store import config_store, history
from app.store.redis import get_store
from app.store.jobs import JobStoreError, get_job_repository
from app.telegram.identity import BotIdentityUnavailable, get_bot_identity

router = APIRouter()
WARSAW = ZoneInfo("Europe/Warsaw")
SCHEDULE_MINUTES = 20
BANTER_ANGLES = (
    "подколоть самую хаотичную повторяющуюся идею",
    "придумать абсурдный заголовок про эту беседу",
    "заметить невероятно самоуверенный смешной шаблон",
    "сделать игривое предсказание о том, что произойдёт дальше",
)


def _slot(now: datetime | None = None) -> tuple[int, datetime]:
    current = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    slot_epoch = int(current.timestamp()) // (SCHEDULE_MINUTES * 60)
    return slot_epoch, datetime.fromtimestamp(slot_epoch * SCHEDULE_MINUTES * 60, timezone.utc).astimezone(WARSAW)


def _quiet(local_slot: datetime) -> bool:
    return 1 <= local_slot.hour < 9


def _cron_ok(request: Request) -> bool:
    secret = settings.CRON_SECRET
    provided = request.headers.get("authorization", "")
    expected = f"Bearer {secret}" if secret else ""
    return bool(secret and hmac.compare_digest(provided, expected))


def _human_context(chat_id: int) -> list[dict[str, object]]:
    return list(reversed(history.recent_human(chat_id, n=30, memory_epoch=current_epoch(chat_id))))


def choose_angle(slot_id: int, rng: random.Random | None = None) -> str:
    chooser = rng or random.Random(slot_id)
    return chooser.choice(BANTER_ANGLES)


def _build_job(chat_id: int, slot_id: int, local_slot: datetime):
    identity = get_bot_identity()
    context = _human_context(chat_id)
    trigger_ts = int(local_slot.timestamp())
    request = {
        "version": 2,
        "kind": "scheduled",
        "route": "scheduled",
        "chat_id": chat_id,
        "update_id": slot_id,
        "trigger_message_id": 0,
        "author": {
            "id": identity.id,
            "name": identity.first_name,
            "username": identity.username,
        },
        "trigger": {
            "message_id": 0,
            "source_update_id": slot_id,
            "user_id": identity.id,
            "username": identity.username,
            "name": identity.first_name,
            "text": "",
            "ts": trigger_ts,
            "edit_ts": None,
            "is_edited": False,
            "is_bot": True,
            "reply_to": None,
        },
        "trigger_text": "",
        "trigger_entities": [],
        "reply_context": None,
        "context": context,
        "received_at": int(time.time()),
        "memory_epoch": current_epoch(chat_id),
        "slot_id": slot_id,
        "scheduled_angle": choose_angle(slot_id),
    }
    policy = {
        "tone_preset": config_store.get_config(chat_id)["effective"]["tone_preset"],
        "list_policies": [],
        "rule_policies": [],
    }
    return get_job_repository().create_reply_job(request, policy, [identity.id])


@router.post("/api/cron/banter")
async def scheduled_banter(request: Request):
    if not _cron_ok(request):
        return JSONResponse({"ok": False}, status_code=401)
    chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        return JSONResponse({"ok": False, "error": "chat not configured"}, status_code=503)
    slot_id, local_slot = _slot()
    if _quiet(local_slot):
        return {"ok": True, "skipped": "quiet_hours"}
    marker_key = f"scheduled:slot:{chat_id}:{slot_id}"
    store = get_store()
    marker = store.get(marker_key)
    if marker is None and not store.set_nx(marker_key, str(slot_id), ex=settings.JOB_RETENTION_SECONDS):
        marker = store.get(marker_key)
    job_id = marker or str(slot_id)
    try:
        if marker is None:
            job = await run_in_threadpool(_build_job, chat_id, slot_id, local_slot)
            job_id = job.job_id
        else:
            job = await run_in_threadpool(get_job_repository().get, job_id)
            if job is None:
                store.delete(marker_key)
                return {"ok": True, "skipped": "expired"}
        if job.qstash_message_id:
            return {"ok": True, "queued": True, "dedup": True}
        message_id = await publish(job_id)
        await run_in_threadpool(get_job_repository().record_publication, job_id, message_id)
        return {"ok": True, "queued": True, "slot": slot_id}
    except (QStashPublishError, JobStoreError, BotIdentityUnavailable):
        return JSONResponse({"ok": False, "error": "temporary scheduling failure"}, status_code=503)
