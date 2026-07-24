"""Signed QStash worker routes and retry-safe Telegram delivery."""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import json
from datetime import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.llm.client import (
    LLMPermanentError,
    LLMRetryableError,
    generate,
)
from app.llm.prompts import build_google_messages, build_reply_messages
from app.metrics import timed
from app.search.citations import validate_citations
from app.search.tavily import (
    MAX_RESULTS as MAX_GOOGLE_SOURCES,
    TavilyUnavailable,
    normalize_source_url,
    sanitize_source_title,
)
from app.search.tavily import search as tavily_search
from app.queue.qstash import (
    QStashVerificationError,
    failure_url,
    process_url,
    verify_signature,
)
from app.request_body import (
    InvalidRequestBody,
    RequestBodyTooLarge,
    read_bounded_body,
)
from app.settings import settings
from app.memory import current_epoch
from app.store import history
from app.store.jobs import (
    FAILURE_NOTICE_TEXT,
    FailureNoticeLease,
    JobLease,
    JobRecord,
    JobRepository,
    JobStoreError,
    OwnershipLost,
    get_job_repository,
)
from app.telegram import client as telegram_client
from app.telegram.addressing import address_text, normalize_first_name
from app.telegram.identity import BotIdentity, BotIdentityUnavailable, get_bot_identity
from app.telegram.job_contract import JOB_SNAPSHOT_VERSION, SUPPORTED_JOB_KINDS

router = APIRouter()

MAX_PROCESS_BODY_BYTES = 256
MAX_FAILURE_BODY_BYTES = 64 * 1024
MAX_SOURCE_BODY_BYTES = MAX_PROCESS_BODY_BYTES
PLACEHOLDER_TEXT = "Thinking…"
LEASE_RENEW_INTERVAL_SECONDS = 60
_WARSAW = ZoneInfo("Europe/Warsaw")


@dataclass(frozen=True, slots=True)
class _WorkError(Exception):
    error_class: str


class _RetryableWork(_WorkError):
    pass


class _PermanentWork(_WorkError):
    pass


class _RejectedSnapshot(_PermanentWork):
    pass


class _AmbiguousWork(_WorkError):
    pass


class _CancelledWork(_WorkError):
    pass


async def _sync(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return await run_in_threadpool(functools.partial(function, *args, **kwargs))


def _json_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_job_body(raw_body: bytes) -> str:
    if len(raw_body) > MAX_PROCESS_BODY_BYTES:
        raise ValueError("invalid process body")
    try:
        value = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid process body") from None
    if not isinstance(value, dict) or set(value) != {"job_id"}:
        raise ValueError("invalid process body")
    job_id = value.get("job_id")
    if (
        not isinstance(job_id, str)
        or not job_id
        or len(job_id) > 32
        or not job_id.isascii()
        or not job_id.isdecimal()
    ):
        raise ValueError("invalid process body")
    return job_id


def _parse_failure_body(raw_body: bytes) -> tuple[str, str, int]:
    if len(raw_body) > MAX_FAILURE_BODY_BYTES:
        raise ValueError("invalid failure body")
    try:
        value = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid failure body") from None
    if not isinstance(value, dict):
        raise ValueError("invalid failure body")

    source_body = value.get("sourceBody")
    if not isinstance(source_body, str) or len(source_body) > 1_024:
        raise ValueError("invalid failure source body")
    try:
        decoded = base64.b64decode(source_body, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid failure source body") from None
    if len(decoded) > MAX_SOURCE_BODY_BYTES:
        raise ValueError("invalid failure source body")
    job_id = _parse_job_body(decoded)

    source_message_id = value.get("sourceMessageId")
    retried = value.get("retried")
    max_retries = value.get("maxRetries")
    if (
        not isinstance(source_message_id, str)
        or not source_message_id
        or len(source_message_id) > 512
        or value.get("url") != process_url()
        or value.get("method") != "POST"
        or isinstance(retried, bool)
        or not isinstance(retried, int)
        or isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or retried < 0
        or max_retries < 0
        or retried != max_retries
        or max_retries > 16
    ):
        raise ValueError("invalid failure metadata")
    return job_id, source_message_id, max_retries


def _retry_response(retry_after: int | None = None) -> JSONResponse:
    headers = (
        {"Retry-After": str(max(retry_after, 1))}
        if retry_after is not None
        else {}
    )
    return JSONResponse(
        {"ok": False, "error": "temporary worker failure"},
        status_code=503,
        headers=headers,
    )


async def _require_owned(repository: JobRepository, lease: JobLease) -> None:
    if not await _sync(repository.guard, lease):
        raise OwnershipLost()


async def _renew_lease(
    repository: JobRepository, lease: JobLease, finished: asyncio.Event
) -> None:
    while True:
        try:
            await asyncio.wait_for(
                finished.wait(), timeout=LEASE_RENEW_INTERVAL_SECONDS
            )
            return
        except TimeoutError:
            try:
                renewed = await _sync(repository.renew, lease)
            except Exception:
                return
            if not renewed:
                return


def _request_int(job: JobRecord, name: str) -> int:
    value = job.request.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _PermanentWork("job_snapshot_invalid")
    return value


def _author_first_name(job: JobRecord) -> str:
    author = job.request.get("author")
    first_name = (
        normalize_first_name(author.get("name"))
        if isinstance(author, Mapping)
        else ""
    )
    if not first_name:
        raise _PermanentWork("job_snapshot_invalid")
    return first_name


def _addressed(job: JobRecord, text: str) -> str:
    if job.request.get("kind") == "scheduled":
        return text.strip()
    addressed = address_text(_author_first_name(job), text)
    if not addressed:
        raise _PermanentWork("job_snapshot_invalid")
    return addressed


def _validate_job_contract(job: JobRecord) -> None:
    version = job.request.get("version")
    kind = job.request.get("kind")
    if (
        isinstance(version, bool)
        or version != JOB_SNAPSHOT_VERSION
        or kind not in SUPPORTED_JOB_KINDS
    ):
        raise _RejectedSnapshot("job_snapshot_unsupported")
    try:
        _author_first_name(job)
    except _PermanentWork as exc:
        raise _RejectedSnapshot(exc.error_class) from None


def _ensure_memory_epoch(job: JobRecord) -> None:
    expected = job.request.get("memory_epoch", 0)
    chat_id = job.request.get("chat_id")
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or (
            settings.TELEGRAM_ALLOWED_CHAT_ID is not None
            and chat_id != settings.TELEGRAM_ALLOWED_CHAT_ID
        )
        or (
            job.request.get("kind") == "scheduled"
            and 1 <= datetime.now(_WARSAW).hour < 9
        )
        or current_epoch(chat_id) != expected
    ):
        raise _CancelledWork("memory_epoch_cancelled")


async def _ensure_image_memory(
    repository: JobRepository, lease: JobLease, job: JobRecord
) -> None:
    """Analyze a Telegram image once before answering or completing its memory job."""
    if not isinstance(job.request.get("image"), Mapping):
        raise _PermanentWork("image_snapshot_invalid")
    _ensure_memory_epoch(job)
    await _require_owned(repository, lease)
    from app.memory.images import analyze_image

    try:
        result = await _sync(analyze_image, job.request)
    except telegram_client.TelegramAPIError as exc:
        if exc.retryable or exc.transport_error:
            raise _RetryableWork("telegram_image_download") from None
        raise _PermanentWork("telegram_image_invalid") from None
    except LLMRetryableError as exc:
        raise _RetryableWork(exc.error_class) from None
    except LLMPermanentError as exc:
        raise _PermanentWork(exc.error_class) from None
    except Exception:
        raise _RetryableWork("image_analysis_failed") from None
    if not isinstance(result, str) or not result.strip():
        raise _PermanentWork("image_analysis_empty")
    _ensure_memory_epoch(job)
    await _require_owned(repository, lease)


def _failure_notice_text(job: JobRecord | None) -> str | None:
    """Return an addressed notice only for a fully supported immutable snapshot."""
    if job is None:
        return FAILURE_NOTICE_TEXT
    try:
        _validate_job_contract(job)
        return _addressed(job, FAILURE_NOTICE_TEXT)
    except _PermanentWork:
        return None


async def _history_upsert(
    job: JobRecord,
    result: Mapping[str, object],
    *,
    identity: BotIdentity,
    text: str,
    edited: bool,
    repository: JobRepository | None = None,
    lease: JobLease | None = None,
) -> None:
    _ensure_memory_epoch(job)
    if (repository is None) != (lease is None):
        raise ValueError("repository and lease must be provided together")
    if repository is not None and lease is not None:
        await _require_owned(repository, lease)
    chat_id = _request_int(job, "chat_id")
    record = telegram_client.outbound_history_record(
        dict(result),
        source_update_id=int(job.job_id),
        fallback_chat_id=chat_id,
        fallback_user_id=identity.id,
        text=text,
        edited=edited,
    )
    record["memory_epoch"] = job.request.get("memory_epoch", 0)
    _ensure_memory_epoch(job)
    await _sync(history.upsert, chat_id, record)
    if repository is not None and lease is not None:
        if not await _sync(repository.guard, lease):
            await _sync(
                history.remove_message_ids,
                chat_id,
                {int(record["message_id"])},
            )
            raise OwnershipLost()


def _canonical_checkpoint(
    job: JobRecord,
    result: Mapping[str, object],
    *,
    identity: BotIdentity,
    text: str,
    edited: bool,
    expected_message_id: int | None = None,
    allow_missing_edit_date: bool = False,
) -> dict[str, object]:
    """Validate and bound a Bot API message before it becomes a checkpoint."""
    chat_id = _request_int(job, "chat_id")
    message_id = result.get("message_id")
    message_date = result.get("date")
    result_chat = result.get("chat")
    result_chat_id = result_chat.get("id") if isinstance(result_chat, dict) else None
    sender = result.get("from")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    sender_is_bot = sender.get("is_bot") if isinstance(sender, dict) else None
    edit_date = result.get("edit_date")
    if (
        isinstance(message_id, bool)
        or not isinstance(message_id, int)
        or isinstance(message_date, bool)
        or not isinstance(message_date, int)
        or isinstance(result_chat_id, bool)
        or result_chat_id != chat_id
        or isinstance(sender_id, bool)
        or sender_id != identity.id
        or sender_is_bot is not True
        or (
            edited
            and not (
                isinstance(edit_date, int)
                and not isinstance(edit_date, bool)
            )
            and not (allow_missing_edit_date and edit_date is None)
        )
    ):
        raise telegram_client.TelegramAPIError(
            "Telegram returned an invalid message result",
            method="editMessageText" if edited else "sendMessage",
            outcome_unknown=True,
        )
    try:
        record = telegram_client.outbound_history_record(
            dict(result),
            source_update_id=int(job.job_id),
            fallback_chat_id=chat_id,
            fallback_user_id=identity.id,
            text=text,
            edited=edited,
        )
    except telegram_client.TelegramAPIError:
        raise telegram_client.TelegramAPIError(
            "Telegram returned an invalid message result",
            method="editMessageText" if edited else "sendMessage",
            outcome_unknown=True,
        ) from None

    message_id = record["message_id"]
    if record["user_id"] != identity.id or (
        expected_message_id is not None and message_id != expected_message_id
    ):
        raise telegram_client.TelegramAPIError(
            "Telegram returned an unexpected message result",
            method="editMessageText" if edited else "sendMessage",
            outcome_unknown=True,
        )

    checkpoint: dict[str, object] = {
        "message_id": message_id,
        "date": record["ts"],
        "chat": {"id": chat_id},
        "from": {
            "id": identity.id,
            "is_bot": True,
            "first_name": identity.first_name,
            "username": identity.username,
        },
        "text": text,
    }
    if edited:
        checkpoint["edit_date"] = record["edit_ts"]
        if allow_missing_edit_date and record["edit_ts"] is None:
            checkpoint["_edit_recovered"] = True
    reply_to = record.get("reply_to")
    if isinstance(reply_to, dict):
        checkpoint["reply_to_message"] = {
            "message_id": reply_to.get("message_id"),
            "from": {
                "id": reply_to.get("user_id"),
                "is_bot": bool(reply_to.get("is_bot", False)),
            },
            "text": str(reply_to.get("text") or "")[:4096],
        }
    return checkpoint


def _checkpoint_identity(checkpoint: Mapping[str, object]) -> BotIdentity | None:
    sender = checkpoint.get("from")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    username = sender.get("username") if isinstance(sender, dict) else None
    first_name = (
        normalize_first_name(sender.get("first_name"))
        if isinstance(sender, dict)
        else ""
    )
    if (
        isinstance(sender_id, bool)
        or not isinstance(sender_id, int)
        or sender_id <= 0
        or not isinstance(username, str)
        or not username
        or len(username) > 64
        or not first_name
    ):
        return None
    return BotIdentity(
        id=sender_id,
        username=username,
        first_name=first_name,
    )


async def _ensure_placeholder(
    repository: JobRepository,
    lease: JobLease,
    job: JobRecord,
    identity: BotIdentity,
) -> dict[str, object]:
    _ensure_memory_epoch(job)
    chat_id = _request_int(job, "chat_id")
    trigger_message_id = _request_int(job, "trigger_message_id")
    placeholder_text = _addressed(job, PLACEHOLDER_TEXT)
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": placeholder_text,
    }
    if job.request.get("kind") != "scheduled":
        payload["reply_parameters"] = {"message_id": trigger_message_id}
    intent = await _sync(
        repository.prepare_intent,
        lease,
        name="placeholder",
        kind="sendMessage",
        chunk_index=-1,
        payload_hash=_json_hash(payload),
        ambiguous_on_takeover=True,
    )
    if intent.status == "ambiguous":
        raise _AmbiguousWork("telegram_send_ambiguous")
    if intent.status == "conflict":
        raise _PermanentWork("delivery_intent_conflict")
    if intent.status == "ownership_lost":
        raise OwnershipLost()

    checkpoint = intent.checkpoint
    if checkpoint is None:
        await _require_owned(repository, lease)
        try:
            result = await _sync(
                telegram_client.send_message,
                chat_id,
                placeholder_text,
                None if job.request.get("kind") == "scheduled" else trigger_message_id,
            )
        except telegram_client.TelegramAPIError as exc:
            if exc.outcome_unknown:
                raise _AmbiguousWork("telegram_send_ambiguous") from None
            if exc.retryable:
                await _sync(repository.clear_intent, lease, name="placeholder")
                raise _RetryableWork("telegram_retryable") from None
            await _sync(repository.clear_intent, lease, name="placeholder")
            raise _PermanentWork("telegram_permanent") from None
        try:
            checkpoint = _canonical_checkpoint(
                job,
                result,
                identity=identity,
                text=placeholder_text,
                edited=False,
            )
        except telegram_client.TelegramAPIError:
            raise _AmbiguousWork("telegram_send_ambiguous") from None
        await _sync(
            repository.checkpoint,
            lease,
            name="placeholder",
            result=checkpoint,
        )
    else:
        try:
            checkpoint = _canonical_checkpoint(
                job,
                checkpoint,
                identity=identity,
                text=placeholder_text,
                edited=False,
            )
        except telegram_client.TelegramAPIError:
            raise _PermanentWork("placeholder_checkpoint_invalid") from None
    await _history_upsert(
        job,
        checkpoint,
        identity=identity,
        text=placeholder_text,
        edited=False,
        repository=repository,
        lease=lease,
    )
    return checkpoint


async def _answer(
    repository: JobRepository, lease: JobLease, job: JobRecord
) -> tuple[JobRecord, str]:
    fresh = await _sync(repository.get, job.job_id)
    if fresh is None:
        raise OwnershipLost()
    if fresh.answer_text is not None:
        expected_prefix = f"{_author_first_name(fresh)}, "
        if fresh.request.get("kind") != "scheduled" and not fresh.answer_text.startswith(expected_prefix):
            raise _PermanentWork("job_answer_invalid")
        stored = await _sync(repository.save_answer, lease, fresh.answer_text)
        resumed = await _sync(repository.get, job.job_id)
        if resumed is None:
            raise OwnershipLost()
        return resumed, stored
    await _require_owned(repository, lease)
    try:
        kind = fresh.request.get("kind")
        if isinstance(fresh.request.get("image"), Mapping):
            await _ensure_image_memory(repository, lease, fresh)
        if kind == "google":
            generated = await _google_answer(repository, lease, fresh)
        else:
            messages = build_reply_messages(fresh)
            await _require_owned(repository, lease)
            with timed(f"route.{kind}.llm"):
                generated = await generate(
                    messages,
                    thinking=kind in {"think", "scheduled"},
                )
    except LLMRetryableError as exc:
        raise _RetryableWork(exc.error_class) from None
    except LLMPermanentError as exc:
        raise _PermanentWork(exc.error_class) from None
    await _sync(_ensure_memory_epoch, fresh)
    stored = await _sync(
        repository.save_answer,
        lease,
        _addressed(fresh, generated),
    )
    saved = await _sync(repository.get, job.job_id)
    if saved is None:
        raise OwnershipLost()
    return saved, stored


def _validated_google_sources(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        raise _PermanentWork("google_search_corrupt")
    raw_sources = value.get("sources")
    if not isinstance(raw_sources, list):
        raise _PermanentWork("google_search_corrupt")
    result: list[dict[str, str]] = []
    for item in raw_sources[:MAX_GOOGLE_SOURCES]:
        url = (
            normalize_source_url(item.get("url"))
            if isinstance(item, Mapping)
            else None
        )
        if (
            not isinstance(item, Mapping)
            or not all(
                isinstance(item.get(field), str)
                for field in ("title", "url", "snippet")
            )
            or url is None
        ):
            raise _PermanentWork("google_search_corrupt")
        result.append(
            {
                "source_id": f"S{len(result) + 1}",
                "title": sanitize_source_title(item["title"]),
                "url": url,
                "snippet": str(item["snippet"]),
            }
        )
    return result


async def _google_answer(
    repository: JobRepository, lease: JobLease, job: JobRecord
) -> str:
    query = job.request.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 4_096:
        raise _PermanentWork("job_snapshot_invalid")
    search_intent = await _sync(
        repository.prepare_intent,
        lease,
        name="google_search",
        kind="externalSearch",
        chunk_index=0,
        payload_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
        ambiguous_on_takeover=True,
    )
    if search_intent.status == "ambiguous":
        raise _AmbiguousWork("google_search_ambiguous")
    if search_intent.status == "conflict":
        raise _PermanentWork("google_search_conflict")
    if search_intent.status == "ownership_lost":
        raise OwnershipLost()
    if search_intent.checkpoint is not None:
        evidence = _validated_google_sources(search_intent.checkpoint)
    else:
        await _require_owned(repository, lease)
        try:
            with timed("tavily.search"):
                sources = await tavily_search(query, explicit=True)
        except TavilyUnavailable:
            sources = []
        checkpoint = {
            "sources": [
                {
                    "title": source.title,
                    "url": source.url,
                    "snippet": source.snippet,
                }
                for source in sources
            ]
        }
        await _sync(
            repository.checkpoint,
            lease,
            name="google_search",
            result=checkpoint,
        )
        evidence = _validated_google_sources(checkpoint)

    await _require_owned(repository, lease)
    with timed("route.google.llm"):
        answer = await generate(
            build_google_messages(job, evidence),
            thinking=True,
        )
    source_ids = {str(item.get("source_id")) for item in evidence}
    cleaned = validate_citations(answer, source_ids)
    if not evidence:
        cleaned = (
            "Live web search was unavailable or returned no usable sources.\n\n"
            + cleaned
        )
    if evidence:
        cleaned += "\n\nSources:\n" + "\n".join(
            f"{item['source_id']} — {item['title']} — {item['url']}"
            for item in evidence
        )
    return cleaned


def _synthetic_edit_result(
    placeholder: Mapping[str, object], text: str
) -> dict[str, object]:
    result = dict(placeholder)
    result["text"] = text
    if not isinstance(result.get("edit_date"), int) or isinstance(
        result.get("edit_date"), bool
    ):
        result.pop("edit_date", None)
    return result


async def _edit_first_chunk(
    repository: JobRepository,
    lease: JobLease,
    job: JobRecord,
    identity: BotIdentity,
    placeholder: Mapping[str, object],
    text: str,
) -> None:
    _ensure_memory_epoch(job)
    chat_id = _request_int(job, "chat_id")
    placeholder_id = placeholder.get("message_id")
    if isinstance(placeholder_id, bool) or not isinstance(placeholder_id, int):
        raise _PermanentWork("placeholder_checkpoint_invalid")
    payload = {"chat_id": chat_id, "message_id": placeholder_id, "text": text}
    intent = await _sync(
        repository.prepare_intent,
        lease,
        name="answer_edit",
        kind="editMessageText",
        chunk_index=0,
        payload_hash=_json_hash(payload),
        ambiguous_on_takeover=False,
    )
    if intent.status in {"conflict", "ambiguous"}:
        raise _PermanentWork("delivery_intent_conflict")
    if intent.status == "ownership_lost":
        raise OwnershipLost()
    checkpoint = intent.checkpoint
    if checkpoint is None:
        await _require_owned(repository, lease)
        _ensure_memory_epoch(job)
        try:
            result = await _sync(
                telegram_client.edit_message_text,
                chat_id,
                placeholder_id,
                text,
            )
            checkpoint = _canonical_checkpoint(
                job,
                result,
                identity=identity,
                text=text,
                edited=True,
                expected_message_id=placeholder_id,
            )
        except telegram_client.TelegramAPIError as exc:
            if exc.message_not_modified:
                checkpoint = _canonical_checkpoint(
                    job,
                    _synthetic_edit_result(placeholder, text),
                    identity=identity,
                    text=text,
                    edited=True,
                    expected_message_id=placeholder_id,
                    allow_missing_edit_date=True,
                )
            elif exc.retryable or exc.outcome_unknown:
                raise _RetryableWork("telegram_retryable") from None
            else:
                raise _PermanentWork("telegram_permanent") from None
        await _sync(
            repository.checkpoint,
            lease,
            name="answer_edit",
            result=checkpoint,
        )
    else:
        try:
            checkpoint = _canonical_checkpoint(
                job,
                checkpoint,
                identity=identity,
                text=text,
                edited=True,
                expected_message_id=placeholder_id,
                allow_missing_edit_date=checkpoint.get("_edit_recovered") is True,
            )
        except telegram_client.TelegramAPIError:
            raise _PermanentWork("answer_checkpoint_invalid") from None
    await _history_upsert(
        job,
        checkpoint,
        identity=identity,
        text=text,
        edited=True,
        repository=repository,
        lease=lease,
    )


async def _send_chunk(
    repository: JobRepository,
    lease: JobLease,
    job: JobRecord,
    identity: BotIdentity,
    index: int,
    text: str,
) -> None:
    chat_id = _request_int(job, "chat_id")
    name = f"chunk:{index}"
    payload = {"chat_id": chat_id, "text": text}
    intent = await _sync(
        repository.prepare_intent,
        lease,
        name=name,
        kind="sendMessage",
        chunk_index=index,
        payload_hash=_json_hash(payload),
        ambiguous_on_takeover=True,
    )
    if intent.status == "ambiguous":
        raise _AmbiguousWork("telegram_send_ambiguous")
    if intent.status == "conflict":
        raise _PermanentWork("delivery_intent_conflict")
    if intent.status == "ownership_lost":
        raise OwnershipLost()
    checkpoint = intent.checkpoint
    if checkpoint is None:
        await _require_owned(repository, lease)
        _ensure_memory_epoch(job)
        try:
            result = await _sync(telegram_client.send_message, chat_id, text)
        except telegram_client.TelegramAPIError as exc:
            if exc.outcome_unknown:
                raise _AmbiguousWork("telegram_send_ambiguous") from None
            if exc.retryable:
                await _sync(repository.clear_intent, lease, name=name)
                raise _RetryableWork("telegram_retryable") from None
            await _sync(repository.clear_intent, lease, name=name)
            raise _PermanentWork("telegram_permanent") from None
        try:
            checkpoint = _canonical_checkpoint(
                job,
                result,
                identity=identity,
                text=text,
                edited=False,
            )
        except telegram_client.TelegramAPIError:
            raise _AmbiguousWork("telegram_send_ambiguous") from None
        await _sync(repository.checkpoint, lease, name=name, result=checkpoint)
    else:
        try:
            checkpoint = _canonical_checkpoint(
                job,
                checkpoint,
                identity=identity,
                text=text,
                edited=False,
            )
        except telegram_client.TelegramAPIError:
            raise _PermanentWork("answer_checkpoint_invalid") from None
    await _history_upsert(
        job,
        checkpoint,
        identity=identity,
        text=text,
        edited=False,
        repository=repository,
        lease=lease,
    )


async def _run_delivery(
    repository: JobRepository, lease: JobLease, initial_job: JobRecord
) -> None:
    _validate_job_contract(initial_job)
    _ensure_memory_epoch(initial_job)
    await _require_owned(repository, lease)
    if initial_job.request.get("kind") == "image_memory":
        await _ensure_image_memory(repository, lease, initial_job)
        return
    try:
        identity = await _sync(get_bot_identity)
    except BotIdentityUnavailable:
        raise _RetryableWork("bot_identity_unavailable") from None
    placeholder = await _ensure_placeholder(repository, lease, initial_job, identity)
    job, answer = await _answer(repository, lease, initial_job)
    chunks = telegram_client.split_plain_text(answer)
    if not chunks:
        raise _PermanentWork("provider_invalid_response")
    await _edit_first_chunk(
        repository,
        lease,
        job,
        identity,
        placeholder,
        chunks[0],
    )
    for index, chunk in enumerate(chunks[1:], start=1):
        await _send_chunk(repository, lease, job, identity, index, chunk)


async def _failure_notice(
    repository: JobRepository, job_id: str
) -> tuple[bool, int | None]:
    claim = await _sync(repository.claim_failure_notice, job_id)
    if claim.status == "busy":
        return False, claim.retry_after
    if claim.status != "claimed" or claim.lease is None:
        return True, None
    lease: FailureNoticeLease = claim.lease
    notice_text = lease.text
    try:
        if not await _sync(repository.guard_failure_notice, lease):
            return False, None
        job = await _sync(repository.get, job_id)
        if job is None:
            return True, None
        chat_id = _request_int(job, "chat_id")
        placeholder = job.checkpoint("placeholder")
        identity = (
            _checkpoint_identity(placeholder) if isinstance(placeholder, dict) else None
        )
        if placeholder is None or identity is None:
            await _sync(
                repository.fail_failure_notice,
                lease,
                "failure_notice_checkpoint_invalid",
            )
            return True, None
        try:
            result = await _sync(
                telegram_client.edit_message_text,
                chat_id,
                lease.placeholder_message_id,
                notice_text,
            )
            checkpoint = _canonical_checkpoint(
                job,
                result,
                identity=identity,
                text=notice_text,
                edited=True,
                expected_message_id=lease.placeholder_message_id,
            )
        except telegram_client.TelegramAPIError as exc:
            if exc.message_not_modified:
                try:
                    checkpoint = _canonical_checkpoint(
                        job,
                        _synthetic_edit_result(placeholder, notice_text),
                        identity=identity,
                        text=notice_text,
                        edited=True,
                        expected_message_id=lease.placeholder_message_id,
                        allow_missing_edit_date=True,
                    )
                except telegram_client.TelegramAPIError:
                    await _sync(
                        repository.fail_failure_notice,
                        lease,
                        "failure_notice_checkpoint_invalid",
                    )
                    return True, None
            elif exc.retryable or exc.outcome_unknown:
                return False, None
            else:
                await _sync(
                    repository.fail_failure_notice,
                    lease,
                    "failure_notice_permanent",
                )
                return True, None

        try:
            await _history_upsert(
                job,
                checkpoint,
                identity=identity,
                text=notice_text,
                edited=True,
            )
        except _CancelledWork:
            return True, None
        except Exception:
            return False, None
        await _sync(repository.complete_failure_notice, lease, checkpoint)
        return True, None
    finally:
        await _sync(repository.release_failure_notice, lease)


async def _owned_job_response(
    repository: JobRepository, lease: JobLease, job: JobRecord
) -> JSONResponse:
    try:
        await _run_delivery(repository, lease, job)
        await _sync(repository.finish, lease, "delivered")
        return JSONResponse({"ok": True}, status_code=200)
    except _AmbiguousWork as exc:
        try:
            await _sync(
                repository.finish,
                lease,
                "failed_ambiguous",
                error_class=exc.error_class,
            )
        except OwnershipLost:
            pass
        return JSONResponse({"ok": True}, status_code=200)
    except _RejectedSnapshot as exc:
        try:
            await _sync(
                repository.finish,
                lease,
                "failed",
                error_class=exc.error_class,
            )
        except OwnershipLost:
            pass
        return JSONResponse({"ok": True}, status_code=200)
    except _CancelledWork as exc:
        try:
            await _sync(
                repository.finish,
                lease,
                "cancelled",
                error_class=exc.error_class,
            )
        except OwnershipLost:
            pass
        return JSONResponse({"ok": True}, status_code=200)
    except _PermanentWork as exc:
        if job.request.get("kind") == "image_memory":
            try:
                await _sync(
                    repository.finish,
                    lease,
                    "failed",
                    error_class=exc.error_class,
                )
            except OwnershipLost:
                pass
            return JSONResponse({"ok": True}, status_code=200)
        try:
            await _sync(
                repository.finish,
                lease,
                "failed",
                error_class=exc.error_class,
                failure_notice=True,
                failure_notice_text=_addressed(job, FAILURE_NOTICE_TEXT),
            )
        except OwnershipLost:
            return JSONResponse({"ok": True}, status_code=200)
        completed, retry_after = await _failure_notice(repository, job.job_id)
        return (
            JSONResponse({"ok": True}, status_code=200)
            if completed
            else _retry_response(retry_after)
        )
    except OwnershipLost:
        current = await _sync(repository.get, job.job_id)
        return (
            JSONResponse({"ok": True}, status_code=200)
            if current is not None
            and current.state
            in {"delivered", "failed", "failed_ambiguous", "cancelled"}
            else _retry_response()
        )
    except (_RetryableWork, JobStoreError) as exc:
        try:
            await _sync(
                repository.finish,
                lease,
                "failed_retryable",
                error_class=exc.error_class,
            )
        except OwnershipLost:
            pass
        return _retry_response()
    except TimeoutError:
        raise
    except Exception:
        # Unknown storage/client exceptions are never serialized. If ownership
        # remains, persist only a stable retryable class.
        try:
            await _sync(
                repository.finish,
                lease,
                "failed_retryable",
                error_class="worker_internal",
            )
        except Exception:
            pass
        return _retry_response()


async def _process_job(job_id: str) -> JSONResponse:
    repository = get_job_repository()
    acquisition = await _sync(repository.acquire, job_id)
    if acquisition.status == "busy":
        return _retry_response(acquisition.retry_after)
    if acquisition.status == "terminal":
        if (
            acquisition.job is not None
            and acquisition.job.state == "failed"
            and _failure_notice_text(acquisition.job) is not None
        ):
            completed, retry_after = await _failure_notice(repository, job_id)
            if not completed:
                return _retry_response(retry_after)
        return JSONResponse({"ok": True}, status_code=200)
    if acquisition.status in {"missing", "invalid_state"}:
        return JSONResponse({"ok": True}, status_code=200)
    if acquisition.status == "exhausted":
        return _retry_response()
    if acquisition.lease is None or acquisition.job is None:
        return _retry_response()

    lease = acquisition.lease
    finished = asyncio.Event()
    renew_task = asyncio.create_task(_renew_lease(repository, lease, finished))
    try:
        async with asyncio.timeout(settings.WORKER_BUDGET_SECONDS):
            return await _owned_job_response(
                repository, lease, acquisition.job
            )
    except TimeoutError:
        current = await _sync(repository.get, job_id)
        if current is not None and current.state in {
            "delivered",
            "failed_ambiguous",
            "cancelled",
        }:
            return JSONResponse({"ok": True}, status_code=200)
        try:
            await _sync(
                repository.finish,
                lease,
                "failed_retryable",
                error_class="worker_budget_exceeded",
            )
        except (OwnershipLost, JobStoreError):
            pass
        return _retry_response()
    finally:
        finished.set()
        renew_task.cancel()
        try:
            await renew_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await _sync(repository.release, lease)
        except Exception:
            pass


@router.post("/api/telegram/process")
async def process_job(request: Request):
    try:
        raw_body = await read_bounded_body(request, MAX_PROCESS_BODY_BYTES)
    except (InvalidRequestBody, RequestBodyTooLarge):
        return Response(status_code=400)
    try:
        verify_signature(
            raw_body,
            request.headers.get("upstash-signature", ""),
            process_url(),
            max_body_bytes=MAX_PROCESS_BODY_BYTES,
        )
    except QStashVerificationError as exc:
        return Response(status_code=exc.status_code)
    try:
        job_id = _parse_job_body(raw_body)
    except ValueError:
        return Response(status_code=400)
    return await _process_job(job_id)


@router.post("/api/telegram/failure")
async def failure_callback(request: Request):
    try:
        raw_body = await read_bounded_body(request, MAX_FAILURE_BODY_BYTES)
    except (InvalidRequestBody, RequestBodyTooLarge):
        return Response(status_code=400)
    try:
        verify_signature(
            raw_body,
            request.headers.get("upstash-signature", ""),
            failure_url(),
            max_body_bytes=MAX_FAILURE_BODY_BYTES,
        )
    except QStashVerificationError as exc:
        return Response(status_code=exc.status_code)
    try:
        job_id, source_message_id, max_retries = _parse_failure_body(raw_body)
    except ValueError:
        return Response(status_code=400)

    repository = get_job_repository()
    stored_job = await _sync(repository.get, job_id)
    failure_notice_text = _failure_notice_text(stored_job)
    takeover = await _sync(
        repository.failure_takeover,
        job_id,
        source_message_id,
        failure_notice_text=failure_notice_text,
        max_retries=max_retries,
    )
    if takeover.status in {"metadata_pending", "busy"}:
        return _retry_response(takeover.retry_after)
    if takeover.status == "mismatch":
        return Response(status_code=401)
    if (
        takeover.status in {"failed", "terminal"}
        and failure_notice_text is not None
    ):
        completed, retry_after = await _failure_notice(repository, job_id)
        if not completed:
            return _retry_response(retry_after)
    return JSONResponse({"ok": True}, status_code=200)
