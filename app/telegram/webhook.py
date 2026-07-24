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

from app.queue.qstash import QStashPublishError, publish
from app.request_body import (
    InvalidRequestBody,
    RequestBodyTooLarge,
    read_bounded_body,
)
from app.settings import production_bot_config_errors, settings
from app.store import history, users
from app.store import config_store, lists, rules
from app.store import lobotomy_access
from app.auth.membership import require_group_member
from app.memory import (
    current_epoch,
    invalidate_source_message,
    lobotomy,
    record_message,
    schedule_observation,
)
from app.metrics import timed
from app.store.dedup import already_seen, mark_seen
from app.store.jobs import (
    SAFE_ENQUEUE_STATES,
    JobRecord,
    JobStoreError,
    get_job_repository,
)
from app.telegram.client import TelegramAPIError, webhook_reply
from app.telegram.addressing import address_text
from app.telegram.identity import BotIdentityUnavailable
from app.telegram.job_contract import JOB_SNAPSHOT_VERSION
from app.telegram.models import (
    IncomingMessage,
    command_targets_other_bot,
    is_service_command,
    parse_command,
    parse_update,
    to_history_record,
    to_image_attachment,
    to_observed_user,
)
from app.telegram.routing import detect_explicit_route
from app.telegram.triggers import detect_keyword_triggers

router = APIRouter()

MAX_TELEGRAM_UPDATE_BYTES = 1_000_000

HELP_TEXT = (
    "I am this chat's AI bot. Available commands:\n"
    "• /ping — check that I am online\n"
    "• /help — show this help\n"
    "• /tone <preset> — change the chat tone\n"
    "• /mode — show the active tone\n"
    "• /think <question> — request a deeper answer\n"
    "• /google <query> — search the web and answer with sources\n"
    "• /lobotomy — clear changeable memory and recent context (owner/invited roster)\n"
    "• /invite @user — allow a current group member to use /lobotomy (owner only)\n"
    "• /uninvite @user — remove a user from the /lobotomy roster (owner only)\n"
    "\nMention me or reply to one of my messages for an AI response."
)
_TONE_SLUGS = ("neutral", "serious", "scientist", "street", "sarcastic_bot")
_MAX_GOOGLE_QUERY_CHARS = 240


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
        return webhook_reply(msg.chat_id, address_text(msg.name, "pong"))
    if command == "help":
        return webhook_reply(msg.chat_id, address_text(msg.name, HELP_TEXT))
    if command == "lobotomy":
        return webhook_reply(msg.chat_id, address_text(msg.name, "Lobotomy complete."))
    return {"ok": True}


def _active_group_member(user_id: int) -> bool:
    try:
        require_group_member(user_id, allow_cache=True)
    except (PermissionError, RuntimeError, TelegramAPIError):
        return False
    return True


def _lobotomy_allowed(msg: IncomingMessage) -> bool:
    user_id = msg.user_id
    if user_id is None or user_id <= 0:
        return False
    if user_id != settings.SUPER_ADMIN_ID and not lobotomy_access.is_invited(
        msg.chat_id, user_id
    ):
        return False
    return _active_group_member(user_id)


def _invite_command_response(msg: IncomingMessage) -> dict[str, Any]:
    if msg.user_id != settings.SUPER_ADMIN_ID or not _active_group_member(msg.user_id or 0):
        return webhook_reply(
            msg.chat_id,
            address_text(msg.name, "Only the active super-admin can use /invite."),
        )
    parts = msg.text.strip().split()
    if len(parts) != 2 or not parts[1].startswith("@"):
        return webhook_reply(
            msg.chat_id,
            address_text(msg.name, "Usage: /invite @username."),
        )
    profile = users.resolve_username(parts[1][1:])
    if profile is None or not isinstance(profile.get("id"), int):
        return webhook_reply(
            msg.chat_id,
            address_text(
                msg.name,
                "That username is not observed yet. Ask the user to send one message first.",
            ),
        )
    target_id = int(profile["id"])
    try:
        target_profile = require_group_member(target_id, seed_profile=True)
    except PermissionError:
        return webhook_reply(
            msg.chat_id,
            address_text(msg.name, "That user is not an active member of this group."),
        )
    invited = lobotomy_access.invite(msg.chat_id, target_id)
    target_name = str(target_profile.get("name") or profile.get("name") or parts[1])
    outcome = "added to" if invited else "already in"
    return webhook_reply(
        msg.chat_id,
        address_text(msg.name, f"{target_name} is {outcome} the /lobotomy roster."),
    )


def _uninvite_command_response(msg: IncomingMessage) -> dict[str, Any]:
    if msg.user_id != settings.SUPER_ADMIN_ID or not _active_group_member(msg.user_id or 0):
        return webhook_reply(
            msg.chat_id,
            address_text(msg.name, "Only the active super-admin can use /uninvite."),
        )
    parts = msg.text.strip().split()
    if len(parts) != 2 or not parts[1].startswith("@"):
        return webhook_reply(
            msg.chat_id,
            address_text(msg.name, "Usage: /uninvite @username."),
        )
    profile = users.resolve_username(parts[1][1:])
    if profile is None or not isinstance(profile.get("id"), int):
        return webhook_reply(
            msg.chat_id,
            address_text(
                msg.name,
                "That username is not observed yet. Ask the user to send one message first.",
            ),
        )
    target_id = int(profile["id"])
    if target_id == settings.SUPER_ADMIN_ID:
        return webhook_reply(
            msg.chat_id,
            address_text(msg.name, "The super-admin cannot be removed from the roster."),
        )
    removed = lobotomy_access.revoke(msg.chat_id, target_id)
    target_name = str(profile.get("name") or parts[1])
    outcome = "removed from" if removed else "was not in"
    return webhook_reply(
        msg.chat_id,
        address_text(msg.name, f"{target_name} is {outcome} the /lobotomy roster."),
    )


def _tone_command_response(
    msg: IncomingMessage, command: str, *, update_id: int | None = None
) -> dict[str, Any]:
    parts = msg.text.strip().split()
    argument = parts[1].casefold() if len(parts) > 1 else ""
    if command == "mode":
        active = config_store.get_config(msg.chat_id)["effective"]
        if update_id is not None and not config_store.record_command(update_id):
            return {"ok": True, "dedup": True}
        return webhook_reply(
            msg.chat_id,
            address_text(
                msg.name,
                f"Mode: {active['tone_preset']}. Allowed: {', '.join(_TONE_SLUGS)}",
            ),
        )
    if argument in {"sarcastic", "sarcastic_robot"}:
        argument = "sarcastic_bot"
    if argument not in _TONE_SLUGS:
        if update_id is not None and not config_store.record_command(update_id):
            return {"ok": True, "dedup": True}
        return webhook_reply(
            msg.chat_id,
            address_text(
                msg.name,
                f"Usage: /{command} <{'|'.join(_TONE_SLUGS)}>.",
            ),
        )
    if update_id is None:
        config_store.set_tone(
            "chat",
            tone_preset=argument,
            chat_id=msg.chat_id,
        )
    elif not config_store.apply_tone_command(
        update_id,
        tone_preset=argument,
        chat_id=msg.chat_id,
    ):
        return {"ok": True, "dedup": True}
    return webhook_reply(
        msg.chat_id,
        address_text(msg.name, f"Tone set to {argument}."),
    )


def _unknown_command_response(
    msg: IncomingMessage, *, update_id: int
) -> dict[str, Any]:
    if not config_store.record_command(update_id):
        return {"ok": True, "dedup": True}
    return webhook_reply(
        msg.chat_id,
        address_text(msg.name, "Unknown command. Use /help to see available commands."),
    )


def _persist_incoming(msg: IncomingMessage) -> None:
    observed_user = to_observed_user(msg)
    if observed_user is not None:
        users.observe(observed_user)
        if msg.is_edited and msg.user_id is not None:
            invalidate_source_message(msg.chat_id, msg.user_id, msg.message_id)
        if not msg.is_bot:
            record_message(
                chat_id=msg.chat_id,
                user_id=msg.user_id or 0,
                name=msg.name,
                message_id=msg.message_id,
                text=msg.text,
                timestamp=msg.edit_date if msg.edit_date is not None else msg.date,
                image=to_image_attachment(msg),
                is_edited=msg.is_edited,
                memory_epoch=current_epoch(msg.chat_id),
            )
            schedule_observation(
                chat_id=msg.chat_id,
                user_id=msg.user_id or 0,
                message_id=msg.message_id,
                text=msg.text,
                timestamp=msg.edit_date if msg.edit_date is not None else msg.date,
                is_bot=msg.is_bot,
                is_replayed_edit=msg.is_edited,
                memory_epoch=current_epoch(msg.chat_id),
            )
    if not msg.text.strip():
        if msg.is_edited:
            history.remove_message_ids(msg.chat_id, {msg.message_id})
        return
    record = to_history_record(
        msg,
        is_service=is_service_command(
            msg.text, settings.TELEGRAM_BOT_USERNAME
        ),
    )
    record["memory_epoch"] = current_epoch(msg.chat_id)
    history.upsert(
        msg.chat_id,
        record,
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
        if not bool(trigger.get("is_bot", False)):
            source_message_id = trigger.get("message_id")
            source_timestamp = timestamp
            recorded = record_message(
                chat_id=chat_id,
                user_id=user_id,
                name=str(author.get("name") or "unknown"),
                message_id=(
                    source_message_id
                    if isinstance(source_message_id, int)
                    and not isinstance(source_message_id, bool)
                    else 0
                ),
                text=str(trigger.get("text") or ""),
                timestamp=(
                    source_timestamp
                    if isinstance(source_timestamp, int)
                    and not isinstance(source_timestamp, bool)
                    else 0
                ),
                image=(
                    request.get("image")
                    if isinstance(request.get("image"), dict)
                    else None
                ),
                is_edited=bool(trigger.get("is_edited", False)),
                memory_epoch=(
                    request.get("memory_epoch")
                    if isinstance(request.get("memory_epoch"), int)
                    else None
                ),
            )
            if not recorded:
                raise JobStoreError("gathered_write_failed")
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
    trigger["memory_epoch"] = current_epoch(msg.chat_id)
    image = to_image_attachment(msg)
    kind = (
        route
        if route in {"think", "google", "auto_rule", "keyword", "scheduled", "image_memory"}
        else "reply"
    )
    return {
        "version": JOB_SNAPSHOT_VERSION,
        "kind": kind,
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
        "image": image,
        "reply_context": trigger.get("reply_to"),
        "context": context,
        "received_at": int(time.time()),
        "memory_epoch": current_epoch(msg.chat_id),
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
        "tone_preset": configuration["tone_preset"],
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
    if not msg.is_edited and command in {"think", "google"}:
        parts = msg.text.strip().split(maxsplit=1)
        query = parts[1].strip() if len(parts) == 2 else ""
        query_limit = _MAX_GOOGLE_QUERY_CHARS if command == "google" else 4_096
        if not query or len(query) > query_limit:
            _persist_incoming(msg)
            if not mark_seen(msg.update_id):
                return _Prepared(response={"ok": True, "dedup": True})
            return _Prepared(
                response=webhook_reply(
                    msg.chat_id,
                    address_text(msg.name, f"Usage: /{command} <question>."),
                )
            )
        context = (
            list(reversed(history.context(msg.chat_id, current_epoch(msg.chat_id), n=30)))
            if command == "think"
            else []
        )
        request = _request_snapshot(msg, command, context)
        request["query"] = query
        policy = _effective_policy(msg, "explicit", rule_text=query)
        indexed_users = _user_ids_in_record(request["trigger"])
        for record in context:
            indexed_users.update(_user_ids_in_record(record))
        job = repository.create_reply_job(request, policy, sorted(indexed_users))
        _persist_job_trigger(job)
        return _Prepared(job_id=job.job_id)
    if msg.is_edited or command in {
        "ping",
        "help",
        "tone",
        "mode",
        "lobotomy",
        "invite",
        "uninvite",
    }:
        _persist_incoming(msg)
        if not msg.is_edited and command == "lobotomy":
            if not _lobotomy_allowed(msg):
                if not mark_seen(msg.update_id):
                    return _Prepared(response={"ok": True, "dedup": True})
                return _Prepared(
                    response=webhook_reply(
                        msg.chat_id,
                        address_text(
                            msg.name,
                            "Lobotomy is restricted to the active super-admin and invited roster.",
                        ),
                    )
                )
            status, remaining = lobotomy(msg.chat_id, msg.user_id)
            if status == "cooldown":
                response = address_text(
                    msg.name,
                    f"Lobotomy is cooling down. Try again in {remaining} seconds.",
                )
                if not mark_seen(msg.update_id):
                    return _Prepared(response={"ok": True, "dedup": True})
                return _Prepared(response=webhook_reply(msg.chat_id, response))
            response = address_text(msg.name, "Lobotomy complete. My bad ideas are gone.")
            if not mark_seen(msg.update_id):
                return _Prepared(response={"ok": True, "dedup": True})
            return _Prepared(response=webhook_reply(msg.chat_id, response))
        if not msg.is_edited and command in {"tone", "mode"}:
            response = _tone_command_response(
                msg, command, update_id=msg.update_id
            )
            return _Prepared(response=response)
        if not msg.is_edited and command == "invite":
            response = _invite_command_response(msg)
            if not mark_seen(msg.update_id):
                return _Prepared(response={"ok": True, "dedup": True})
            return _Prepared(response=response)
        if not msg.is_edited and command == "uninvite":
            response = _uninvite_command_response(msg)
            if not mark_seen(msg.update_id):
                return _Prepared(response={"ok": True, "dedup": True})
            return _Prepared(response=response)
        if not mark_seen(msg.update_id):
            return _Prepared(response={"ok": True, "dedup": True})
        return _Prepared(
            response={"ok": True} if msg.is_edited else _command_response(msg)
        )
    if command is not None:
        _persist_incoming(msg)
        return _Prepared(
            response=_unknown_command_response(msg, update_id=msg.update_id)
        )

    route = detect_explicit_route(msg)
    keyword_families = (
        detect_keyword_triggers(msg.text)
        if not msg.is_bot and not msg.text.lstrip().startswith("/")
        else ()
    )
    if keyword_families:
        route = "keyword"
    auto_rules = (
        rules.resolve(msg.text, "auto")
        if route is None and not msg.text.lstrip().startswith("/")
        else []
    )
    if (
        route is None
        and msg.image_file_id
        and not msg.is_bot
        and not msg.is_edited
        and msg.user_id is not None
        and msg.user_id > 0
    ):
        route = "image_memory"
    if route == "image_memory" and not settings.QSTASH_TOKEN:
        _persist_incoming(msg)
        if not mark_seen(msg.update_id):
            return _Prepared(response={"ok": True, "dedup": True})
        return _Prepared(response={"ok": True})
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
    context = list(reversed(history.context(msg.chat_id, current_epoch(msg.chat_id), n=30)))
    request = _request_snapshot(msg, route, context)
    if keyword_families:
        request["keyword_families"] = list(keyword_families)
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
        with timed("webhook.ingestion"):
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
