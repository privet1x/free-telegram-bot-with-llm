"""Telegram webhook: secure ingestion, immutable snapshots, and QStash enqueue."""

from __future__ import annotations

import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.llm.prompts import DEFAULT_BASE_POLICY
from app.queue.qstash import QStashPublishError, publish
from app.request_body import (
    InvalidRequestBody,
    RequestBodyTooLarge,
    read_bounded_body,
)
from app.settings import production_bot_config_errors, settings
from app.store import admins, history, users
from app.store import config_store, lists, rules
from app.store.dedup import already_seen, mark_seen
from app.store.jobs import (
    SAFE_ENQUEUE_STATES,
    JobRecord,
    JobStoreError,
    get_job_repository,
)
from app.telegram.client import webhook_reply
from app.telegram.identity import BotIdentityUnavailable
from app.telegram.models import (
    IncomingMessage,
    command_targets_other_bot,
    is_service_command,
    parse_command,
    parse_update,
    to_history_record,
    to_observed_user,
)
from app.telegram.routing import detect_explicit_route, has_exact_mention

router = APIRouter()

MAX_TELEGRAM_UPDATE_BYTES = 1_000_000

HELP_TEXT = (
    "Hello! I am this chat's bot. Available commands:\n"
    "• /ping — check that I am online\n"
    "• /help — show this help\n"
    "\nMention me or reply to one of my messages for an AI response."
)
_TONE_SLUGS = ("neutral", "serious", "scientist", "street", "sarcastic_robot")


@dataclass(frozen=True, slots=True)
class _Prepared:
    response: dict[str, Any] | None = None
    job_id: str | None = None


def _secret_ok(request: Request) -> bool:
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    provided = request.headers.get("x-telegram-bot-api-secret-token", "")
    return bool(expected and hmac.compare_digest(provided, expected))


def _production_ready() -> bool:
    if os.environ.get("VERCEL"):
        return not production_bot_config_errors()
    allowed_chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
    return bool(
        isinstance(allowed_chat_id, int)
        and not isinstance(allowed_chat_id, bool)
        and allowed_chat_id != 0
    ) or settings.ALLOW_UNFILTERED_LOCAL_CHATS


def _safe_enqueue(job: JobRecord) -> bool:
    return bool(job.qstash_message_id and job.state in SAFE_ENQUEUE_STATES)


def _command_response(msg: IncomingMessage) -> dict[str, Any]:
    command = parse_command(msg.text, settings.TELEGRAM_BOT_USERNAME)
    if command == "ping":
        return webhook_reply(msg.chat_id, "pong")
    if command == "help":
        return webhook_reply(msg.chat_id, HELP_TEXT)
    return {"ok": True}


def _tone_command_response(
    msg: IncomingMessage, command: str, *, update_id: int | None = None
) -> dict[str, Any]:
    if not admins.is_admin(msg.user_id):
        if update_id is not None and not config_store.record_command(update_id):
            return {"ok": True, "dedup": True}
        return webhook_reply(msg.chat_id, "Only an administrator can change the bot tone.")
    parts = msg.text.strip().split()
    argument = parts[1].casefold() if len(parts) > 1 else ""
    if argument == "sarcastic":
        argument = "sarcastic_robot"
    if command == "mode":
        active = config_store.get_config(msg.chat_id)["effective"]
        if update_id is not None and not config_store.record_command(update_id):
            return {"ok": True, "dedup": True}
        return webhook_reply(
            msg.chat_id,
            f"Mode: {active['tone_mode']}/{active['tone_preset']}. Allowed: {', '.join(_TONE_SLUGS)}",
        )
    scope = "global" if len(parts) > 2 and parts[1].casefold() == "global" else "chat"
    if scope == "global":
        argument = parts[2].casefold() if len(parts) > 2 else ""
        if argument == "sarcastic":
            argument = "sarcastic_robot"
    if argument == "clear" and scope == "chat":
        if update_id is None:
            config_store.clear_chat_override(msg.chat_id)
        elif not config_store.apply_tone_command(
            update_id,
            scope="chat",
            tone_preset="clear",
            chat_id=msg.chat_id,
        ):
            return {"ok": True, "dedup": True}
        return webhook_reply(msg.chat_id, "Tone override cleared.")
    if argument not in _TONE_SLUGS:
        if update_id is not None and not config_store.record_command(update_id):
            return {"ok": True, "dedup": True}
        return webhook_reply(msg.chat_id, f"Usage: /{command} <{'|'.join(_TONE_SLUGS)}>.")
    if update_id is None:
        config_store.set_tone(
            scope,
            tone_mode="preset",
            tone_preset=argument,
            chat_id=msg.chat_id if scope == "chat" else None,
        )
    elif not config_store.apply_tone_command(
        update_id,
        scope=scope,
        tone_preset=argument,
        chat_id=msg.chat_id,
    ):
        return {"ok": True, "dedup": True}
    return webhook_reply(msg.chat_id, f"Tone set to {argument}.")


def _persist_incoming(msg: IncomingMessage) -> None:
    observed_user = to_observed_user(msg)
    if observed_user is not None:
        users.observe(observed_user)
    if not msg.text.strip():
        if msg.is_edited:
            history.remove_message_ids(msg.chat_id, {msg.message_id})
        return
    history.upsert(
        msg.chat_id,
        to_history_record(
            msg,
            is_service=is_service_command(
                msg.text, settings.TELEGRAM_BOT_USERNAME
            ),
        ),
    )


def _persist_job_trigger(job: JobRecord) -> None:
    """Repair ingestion from the immutable snapshot, never a changed retry body."""
    request = job.request
    trigger = request.get("trigger")
    author = request.get("author")
    if not isinstance(trigger, dict) or not isinstance(author, dict):
        raise JobStoreError("job_snapshot_corrupt")
    chat_id = request.get("chat_id")
    update_id = request.get("update_id")
    user_id = author.get("id")
    timestamp = trigger.get("edit_ts") or trigger.get("ts")
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or isinstance(update_id, bool)
        or not isinstance(update_id, int)
    ):
        raise JobStoreError("job_snapshot_corrupt")
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        users.observe(
            {
                "id": user_id,
                "username": author.get("username"),
                "name": author.get("name"),
                "is_bot": bool(trigger.get("is_bot", False)),
                "last_seen_at": timestamp,
                "last_update_id": update_id,
            }
        )
    history.upsert(chat_id, trigger)


def _user_ids_in_record(record: object) -> set[int]:
    if not isinstance(record, dict):
        return set()
    result: set[int] = set()
    for candidate in (
        record.get("user_id"),
        (record.get("reply_to") or {}).get("user_id")
        if isinstance(record.get("reply_to"), dict)
        else None,
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            result.add(candidate)
    return result


def _request_snapshot(
    msg: IncomingMessage, route: str, context: list[dict[str, Any]]
) -> dict[str, object]:
    trigger = to_history_record(
        msg,
        is_service=is_service_command(msg.text, settings.TELEGRAM_BOT_USERNAME),
    )
    return {
        "version": 1,
        "kind": route if route in {"judge", "deep_reply", "auto_rule"} else "reply",
        "route": route,
        "chat_id": msg.chat_id,
        "update_id": msg.update_id,
        "trigger_message_id": msg.message_id,
        "author": {
            "id": msg.user_id,
            "name": msg.name,
            "username": msg.username,
        },
        "trigger": trigger,
        "trigger_text": msg.text,
        "trigger_entities": list(msg.entities),
        "reply_context": trigger.get("reply_to"),
        "context": context,
        "received_at": int(time.time()),
    }


def _effective_policy(
    msg: IncomingMessage,
    scope: str,
    *,
    matched_rules: list[dict[str, Any]] | None = None,
    rule_text: str | None = None,
) -> dict[str, object]:
    configuration = config_store.get_config(msg.chat_id)["effective"]
    matched_lists = lists.member_lists(msg.user_id or 0, scope)
    selected_rules = (
        rules.resolve(msg.text if rule_text is None else rule_text, scope)
        if matched_rules is None
        else matched_rules
    )
    return {
        "tone_mode": configuration["tone_mode"],
        "tone_preset": configuration["tone_preset"],
        "custom_system_prompt": configuration["custom_system_prompt"],
        "judge_default_n": configuration["judge_default_n"],
        "base_system_prompt": DEFAULT_BASE_POLICY,
        "actor": {
            "user_id": msg.user_id,
            "is_admin": admins.is_admin(msg.user_id),
        },
        "list_policies": matched_lists[: settings.MAX_LIST_POLICIES],
        "rule_policies": selected_rules[: settings.MAX_RULE_POLICIES],
    }


def _prepare_message(msg: IncomingMessage) -> _Prepared:
    repository = get_job_repository()
    existing = repository.get(msg.update_id)

    # A final marker is safe only when no routed job is incomplete.
    if existing is None and already_seen(msg.update_id):
        return _Prepared(response={"ok": True, "dedup": True})
    if existing is not None:
        if already_seen(msg.update_id) and _safe_enqueue(existing):
            return _Prepared(response={"ok": True, "dedup": True})
        _persist_job_trigger(existing)
        if _safe_enqueue(existing):
            mark_seen(msg.update_id)
            return _Prepared(response={"ok": True, "queued": True})
        return _Prepared(job_id=existing.job_id)

    # Edits and built-in service commands never make an LLM job.
    command = parse_command(msg.text, settings.TELEGRAM_BOT_USERNAME)
    if command is None and command_targets_other_bot(
        msg.text, settings.TELEGRAM_BOT_USERNAME
    ):
        _persist_incoming(msg)
        if not mark_seen(msg.update_id):
            return _Prepared(response={"ok": True, "dedup": True})
        return _Prepared(response={"ok": True, "ignored": True})
    normalized_text = " ".join(msg.text.casefold().split())
    phrase_judge = (
        ("judge us" in normalized_text or "who is right" in normalized_text)
        and has_exact_mention(msg.text, msg.entities, settings.TELEGRAM_BOT_USERNAME)
    )
    judge_requested = command in {"judge", "dispute", "deep"} or phrase_judge
    if judge_requested and not msg.is_edited:
        if not admins.is_admin(msg.user_id):
            _persist_incoming(msg)
            if not mark_seen(msg.update_id):
                return _Prepared(response={"ok": True, "dedup": True})
            return _Prepared(
                response=webhook_reply(
                    msg.chat_id, "Only an administrator can run this command."
                )
            )
        context = list(reversed(history.recent(msg.chat_id, n=30)))
        if command == "deep":
            parts = msg.text.strip().split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                _persist_incoming(msg)
                if not mark_seen(msg.update_id):
                    return _Prepared(response={"ok": True, "dedup": True})
                return _Prepared(
                    response=webhook_reply(msg.chat_id, "Usage: /deep <question>.")
                )
            request = _request_snapshot(msg, "deep_reply", context)
            policy = _effective_policy(msg, "explicit")
            indexed_users = _user_ids_in_record(request["trigger"])
            for record in context:
                indexed_users.update(_user_ids_in_record(record))
            job = repository.create_reply_job(
                request, policy, sorted(indexed_users)
            )
            _persist_job_trigger(job)
            return _Prepared(job_id=job.job_id)

        route = "judge"
        requested_n = 20
        explicit_n = False
        if command in {"judge", "dispute"}:
            parts = msg.text.strip().split()
            if len(parts) > 1:
                if (
                    len(parts) != 2
                    or not parts[1].isascii()
                    or not parts[1].isdecimal()
                ):
                    _persist_incoming(msg)
                    if not mark_seen(msg.update_id):
                        return _Prepared(response={"ok": True, "dedup": True})
                    return _Prepared(
                        response=webhook_reply(
                            msg.chat_id, f"Usage: /{command} [5-30]."
                        )
                    )
                requested_n = int(parts[1])
                explicit_n = True
        configured_n = config_store.get_config(msg.chat_id)["effective"].get(
            "judge_default_n", 20
        )
        if not explicit_n:
            requested_n = configured_n if isinstance(configured_n, int) else 20
        requested_n = max(5, min(30, requested_n))
        meaningful = [
            record
            for record in context
            if str(record.get("text", "")).strip()
            and not bool(record.get("is_service", False))
        ][-requested_n:]
        human_authors = {
            record.get("user_id")
            for record in meaningful
            if record.get("is_bot") is not True
            and isinstance(record.get("user_id"), int)
        }
        if len(meaningful) < 3 or len(human_authors) < 2:
            _persist_incoming(msg)
            if not mark_seen(msg.update_id):
                return _Prepared(response={"ok": True, "dedup": True})
            return _Prepared(
                response=webhook_reply(
                    msg.chat_id, "Not enough context to analyze this dispute."
                )
            )
        request = _request_snapshot(msg, route, meaningful)
        request["judge_n"] = requested_n
        policy = _effective_policy(
            msg,
            "judge",
            rule_text="\n".join(str(record["text"]) for record in meaningful),
        )
        indexed_users = _user_ids_in_record(request["trigger"])
        for record in context:
            indexed_users.update(_user_ids_in_record(record))
        job = repository.create_reply_job(request, policy, sorted(indexed_users))
        _persist_job_trigger(job)
        return _Prepared(job_id=job.job_id)
    if msg.is_edited or command in {"ping", "help", "tone", "set_mode", "mode"}:
        _persist_incoming(msg)
        if not msg.is_edited and command in {"tone", "set_mode", "mode"}:
            response = _tone_command_response(
                msg, command, update_id=msg.update_id
            )
            return _Prepared(response=response)
        if not mark_seen(msg.update_id):
            return _Prepared(response={"ok": True, "dedup": True})
        return _Prepared(
            response={"ok": True} if msg.is_edited else _command_response(msg)
        )

    route = detect_explicit_route(msg)
    auto_rules = (
        rules.resolve(msg.text, "auto")
        if route is None and not msg.text.lstrip().startswith("/")
        else []
    )
    if route is None and auto_rules and msg.user_id and msg.user_id > 0:
        if lists.is_member(lists.IGNORE_SLUG, msg.user_id):
            auto_rules = []
        else:
            owner = f"{msg.update_id}:{os.urandom(12).hex()}"
            route = "auto_rule"
    if route is None or msg.user_id is None or msg.user_id <= 0:
        _persist_incoming(msg)
        if not mark_seen(msg.update_id):
            return _Prepared(response={"ok": True, "dedup": True})
        return _Prepared(response={"ok": True})

    # history.recent is newest-first; snapshots are chronological.
    context = list(reversed(history.recent(msg.chat_id, n=30)))
    request = _request_snapshot(msg, route, context)
    effective_policy = _effective_policy(
        msg,
        "auto" if route == "auto_rule" else "explicit",
        matched_rules=auto_rules if route == "auto_rule" else None,
    )
    indexed_users = _user_ids_in_record(request["trigger"])
    for record in context:
        indexed_users.update(_user_ids_in_record(record))
    indexed_users.update(_user_ids_in_record(request.get("reply_context")))

    try:
        job = repository.create_reply_job(
            request,
            effective_policy,
            sorted(indexed_users),
            auto_cooldown_owner=owner if route == "auto_rule" else None,
            auto_cooldown_seconds=(
                settings.AUTO_TRIGGER_COOLDOWN_SECONDS
                if route == "auto_rule"
                else None
            ),
        )
    except JobStoreError as exc:
        if str(exc) == "auto_cooldown":
            _persist_incoming(msg)
            mark_seen(msg.update_id)
            return _Prepared(response={"ok": True, "suppressed": True})
        raise
    _persist_job_trigger(job)
    return _Prepared(job_id=job.job_id)


def _finish_enqueue(job_id: str, message_id: str) -> dict[str, Any]:
    repository = get_job_repository()
    job = repository.record_publication(job_id, message_id)
    if not _safe_enqueue(job):
        raise JobStoreError("job_enqueue_incomplete")
    mark_seen(int(job_id))
    return {"ok": True, "queued": True}


@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    if not _production_ready():
        return JSONResponse(
            {"ok": False, "error": "service not ready"}, status_code=503
        )
    if not _secret_ok(request):
        return Response(status_code=403)

    try:
        raw_body = await read_bounded_body(request, MAX_TELEGRAM_UPDATE_BYTES)
    except RequestBodyTooLarge:
        return Response(status_code=413)
    except InvalidRequestBody:
        return Response(status_code=400)
    try:
        update = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Response(status_code=400)

    msg = parse_update(update)
    if msg is None:
        return {"ok": True}

    allowed_chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
    if allowed_chat_id is not None and msg.chat_id != allowed_chat_id:
        return {"ok": True, "ignored": True}

    try:
        prepared = await run_in_threadpool(_prepare_message, msg)
    except BotIdentityUnavailable:
        return JSONResponse(
            {"ok": False, "error": "temporary dependency failure"},
            status_code=503,
        )
    except Exception:
        # Redis/history/user persistence is part of ingestion. A transient
        # storage failure must be surfaced as retryable without exposing the
        # exception or marking the update complete.
        return JSONResponse(
            {"ok": False, "error": "temporary storage failure"},
            status_code=503,
        )
    if prepared.response is not None:
        return prepared.response
    if prepared.job_id is None:
        return JSONResponse({"ok": False, "error": "job error"}, status_code=503)

    try:
        message_id = await publish(prepared.job_id)
        return await run_in_threadpool(
            _finish_enqueue, prepared.job_id, message_id
        )
    except (QStashPublishError, JobStoreError):
        # The received job and immutable snapshot remain durable for Telegram's
        # retry. Never include provider details or the snapshot in this response.
        return JSONResponse(
            {"ok": False, "error": "temporary queue failure"}, status_code=503
        )
