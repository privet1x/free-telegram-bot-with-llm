"""Telegram update webhook route.

Order (see TICKET-01):
1) verify the secret header (otherwise 403);
2) parse the update; no message -> fast 200;
3) fast-check completed updates;
4) idempotently upsert user and history, then write the completion marker;
5) only the completion-race winner replies to service commands (/ping, /help);
6) fast 200.

There is no LLM/QStash here yet — that is ticket 02.
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.settings import production_webhook_config_errors, settings
from app.store import history, users
from app.store.dedup import already_seen, mark_seen
from app.telegram.client import webhook_reply
from app.telegram.models import (
    IncomingMessage,
    parse_command,
    parse_update,
    to_history_record,
    to_observed_user,
)

router = APIRouter()

HELP_TEXT = (
    "Привет! Я бот этого чата. Пока умею немного:\n"
    "• /ping — проверить, что я жив\n"
    "• /help — эта справка\n"
    "\nСкоро научусь отвечать на упоминания, реагировать на триггеры и разбирать споры."
)


def _secret_ok(request: Request) -> bool:
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    provided = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not expected:
        # No secret configured — trust no one.
        return False
    return hmac.compare_digest(provided, expected)


def _production_ready() -> bool:
    """Reject unsafe serverless fallback before accepting an update."""
    if not os.environ.get("VERCEL"):
        return True
    return not production_webhook_config_errors()


def _process_message(msg: IncomingMessage):
    """Run synchronous Redis work in FastAPI's worker threadpool."""
    if already_seen(msg.update_id):
        return {"ok": True, "dedup": True}

    observed_user = to_observed_user(msg)
    if observed_user is not None:
        users.observe(observed_user)
    history.upsert(msg.chat_id, to_history_record(msg))

    # Every operation above is idempotent. SET NX both writes the final marker
    # and elects the sole command-response winner under concurrent delivery.
    if not mark_seen(msg.update_id):
        return {"ok": True, "dedup": True}

    # Edits repair the canonical history record but never re-trigger commands.
    if msg.is_edited:
        return {"ok": True}

    command = parse_command(msg.text, settings.TELEGRAM_BOT_USERNAME)
    if command == "ping":
        return webhook_reply(msg.chat_id, "pong")
    if command == "help":
        return webhook_reply(msg.chat_id, HELP_TEXT)
    return {"ok": True}


@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    if not _production_ready():
        return JSONResponse(
            {"ok": False, "error": "service not ready"}, status_code=503
        )

    # 1) secret
    if not _secret_ok(request):
        return Response(status_code=403)

    # 2) body
    try:
        update = await request.json()
    except Exception:
        return Response(status_code=400)

    msg = parse_update(update)
    if msg is None:
        return {"ok": True}

    allowed_chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
    if allowed_chat_id is not None and msg.chat_id != allowed_chat_id:
        return {"ok": True, "ignored": True}

    return await run_in_threadpool(_process_message, msg)
