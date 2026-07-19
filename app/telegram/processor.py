"""Signed QStash worker routes and retry-safe Telegram delivery."""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.llm.client import (
    LLMPermanentError,
    LLMRetryableError,
    generate_flash,
    generate_pro,
)
from app.llm.prompts import build_judge_messages, build_reply_messages
from app.llm.prompts import build_claim_messages
from app.search.judge import parse_claim_response, validate_citations, validate_claims
from app.search.tavily import (
    TavilyUnavailable,
    sanitize_query,
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
from app.store import history
from app.store.jobs import (
    FailureNoticeLease,
    JobLease,
    JobRecord,
    JobRepository,
    JobStoreError,
    OwnershipLost,
    get_job_repository,
)
from app.telegram import client as telegram_client
from app.telegram.identity import BotIdentity, BotIdentityUnavailable, get_bot_identity

router = APIRouter()

MAX_PROCESS_BODY_BYTES = 256
MAX_FAILURE_BODY_BYTES = 64 * 1024
MAX_SOURCE_BODY_BYTES = MAX_PROCESS_BODY_BYTES
PLACEHOLDER_TEXT = "Thinking…"
LEASE_RENEW_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class _WorkError(Exception):
    error_class: str


class _RetryableWork(_WorkError):
    pass


class _PermanentWork(_WorkError):
    pass


class _AmbiguousWork(_WorkError):
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


def _private_search_terms(request: Mapping[str, object]) -> tuple[str, ...]:
    """Collect identifiers and raw text that must not appear in Tavily queries."""
    terms: list[str] = []

    def add_record(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        for field in ("name", "username", "text", "user_id", "id"):
            candidate = value.get(field)
            if isinstance(candidate, str) and candidate.strip():
                terms.append(candidate)
            elif isinstance(candidate, int) and not isinstance(candidate, bool):
                terms.append(str(candidate))
        add_record(value.get("reply_to"))

    add_record(request.get("author"))
    add_record(request.get("trigger"))
    add_record(request.get("reply_context"))
    context = request.get("context")
    if isinstance(context, list):
        for record in context:
            add_record(record)
    return tuple(dict.fromkeys(terms))


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
            "first_name": identity.username,
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
    if (
        isinstance(sender_id, bool)
        or not isinstance(sender_id, int)
        or sender_id <= 0
        or not isinstance(username, str)
        or not username
        or len(username) > 64
    ):
        return None
    return BotIdentity(id=sender_id, username=username)


async def _ensure_placeholder(
    repository: JobRepository,
    lease: JobLease,
    job: JobRecord,
    identity: BotIdentity,
) -> dict[str, object]:
    chat_id = _request_int(job, "chat_id")
    trigger_message_id = _request_int(job, "trigger_message_id")
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": PLACEHOLDER_TEXT,
        "reply_parameters": {"message_id": trigger_message_id},
    }
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
                PLACEHOLDER_TEXT,
                trigger_message_id,
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
                text=PLACEHOLDER_TEXT,
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
                text=PLACEHOLDER_TEXT,
                edited=False,
            )
        except telegram_client.TelegramAPIError:
            raise _PermanentWork("placeholder_checkpoint_invalid") from None
    await _history_upsert(
        job,
        checkpoint,
        identity=identity,
        text=PLACEHOLDER_TEXT,
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
        stored = await _sync(repository.save_answer, lease, fresh.answer_text)
        resumed = await _sync(repository.get, job.job_id)
        if resumed is None:
            raise OwnershipLost()
        return resumed, stored
    await _require_owned(repository, lease)
    try:
        if fresh.request.get("kind") == "judge":
            stored = await _judge_answer(repository, lease, fresh)
            return fresh, await _sync(repository.save_answer, lease, stored)
        elif fresh.request.get("kind") == "deep_reply":
            messages = build_reply_messages(fresh)
            generator = generate_pro
        else:
            messages = build_reply_messages(fresh)
            generator = generate_flash
        await _require_owned(repository, lease)
        generated = await generator(messages)
    except LLMRetryableError as exc:
        raise _RetryableWork(exc.error_class) from None
    except LLMPermanentError as exc:
        raise _PermanentWork(exc.error_class) from None
    stored = await _sync(repository.save_answer, lease, generated)
    saved = await _sync(repository.get, job.job_id)
    if saved is None:
        raise OwnershipLost()
    return saved, stored


async def _judge_answer(
    repository: JobRepository, lease: JobLease, job: JobRecord
) -> str:
    claims_intent = await _sync(
        repository.prepare_intent,
        lease,
        name="judge_claims",
        kind="judgeStage",
        chunk_index=0,
        payload_hash=hashlib.sha256(b"judge-claims-v1").hexdigest(),
        ambiguous_on_takeover=True,
    )
    claims: list[dict[str, str]]
    if claims_intent.status == "ambiguous":
        raise _AmbiguousWork("judge_claims_ambiguous")
    if claims_intent.status == "conflict":
        raise _PermanentWork("judge_claims_conflict")
    if claims_intent.status == "ownership_lost":
        raise OwnershipLost()
    if claims_intent.checkpoint is not None:
        try:
            claims = validate_claims(claims_intent.checkpoint)
        except ValueError:
            raise _PermanentWork("judge_claims_corrupt") from None
    else:
        await _require_owned(repository, lease)
        try:
            raw_claims = await generate_pro(build_claim_messages(job))
            claims = parse_claim_response(raw_claims)
        except LLMRetryableError:
            await _sync(repository.clear_intent, lease, name="judge_claims")
            raise
        except (ValueError, TypeError):
            raise _PermanentWork("judge_claims_invalid") from None
        await _sync(repository.checkpoint, lease, name="judge_claims", result={"claims": claims})

    evidence: list[dict[str, str]] = []
    forbidden_terms = _private_search_terms(job.request)
    unverified_claims: list[dict[str, str]] = []
    for claim_index, claim in enumerate(claims[: settings.FACT_CHECK_MAX_QUERIES]):
        search_intent = await _sync(
            repository.prepare_intent,
            lease,
            name=f"judge_search:{claim_index}",
            kind="judgeStage",
            chunk_index=claim_index + 1,
            payload_hash=hashlib.sha256(
                json.dumps(claim, sort_keys=True).encode()
            ).hexdigest(),
            ambiguous_on_takeover=True,
        )
        if search_intent.status == "ambiguous":
            raise _AmbiguousWork("judge_search_ambiguous")
        if search_intent.status == "conflict":
            raise _PermanentWork("judge_search_conflict")
        if search_intent.status == "ownership_lost":
            raise OwnershipLost()
        checkpoint = search_intent.checkpoint
        if checkpoint is None:
            await _require_owned(repository, lease)
            safe_query = sanitize_query(claim["search_query"], forbidden_terms)
            status = "verified"
            if safe_query is None:
                sources = []
                status = "unverified_private"
            else:
                try:
                    sources = await tavily_search(
                        safe_query, forbidden_terms=forbidden_terms
                    )
                except TavilyUnavailable:
                    sources = []
                    status = "unverified_unavailable"
                if not sources:
                    status = "unverified_unavailable"
            checkpoint = {
                "status": status,
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
                name=f"judge_search:{claim_index}",
                result=checkpoint,
            )
        raw_sources = checkpoint.get("sources")
        status = checkpoint.get("status", "verified")
        if (
            not isinstance(raw_sources, list)
            or status
            not in {"verified", "unverified_private", "unverified_unavailable"}
        ):
            raise _PermanentWork("judge_evidence_corrupt")
        if status != "verified":
            unverified_claims.append(
                {
                    "claim_id": claim["claim_id"],
                    "neutral_claim": claim["neutral_claim"],
                    "status": str(status),
                }
            )
        for source in raw_sources:
            if (
                not isinstance(source, Mapping)
                or not all(
                    isinstance(source.get(field), str)
                    for field in ("title", "url", "snippet")
                )
                or not str(source["url"]).startswith("https://")
            ):
                raise _PermanentWork("judge_evidence_corrupt")
            evidence.append(
                {
                    "source_id": f"S{len(evidence) + 1}",
                    "claim_id": claim["claim_id"],
                    "title": sanitize_source_title(source["title"]),
                    "url": str(source["url"]),
                    "snippet": str(source["snippet"]),
                }
            )
    verdict_intent = await _sync(
        repository.prepare_intent,
        lease,
        name="judge_verdict",
        kind="judgeStage",
        chunk_index=settings.FACT_CHECK_MAX_QUERIES + 1,
        payload_hash=hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
        ambiguous_on_takeover=True,
    )
    if verdict_intent.status == "ambiguous":
        raise _AmbiguousWork("judge_verdict_ambiguous")
    if verdict_intent.status == "conflict":
        raise _PermanentWork("judge_verdict_conflict")
    if verdict_intent.status == "ownership_lost":
        raise OwnershipLost()
    if verdict_intent.checkpoint is not None:
        raw_verdict = verdict_intent.checkpoint.get("text")
        if not isinstance(raw_verdict, str):
            raise _PermanentWork("judge_verdict_corrupt")
        verdict = raw_verdict
    else:
        await _require_owned(repository, lease)
        try:
            verdict = await generate_pro(
                build_judge_messages(
                    job, evidence=[*evidence, *unverified_claims]
                )
            )
        except LLMRetryableError:
            await _sync(repository.clear_intent, lease, name="judge_verdict")
            raise
        await _sync(
            repository.checkpoint,
            lease,
            name="judge_verdict",
            result={"text": verdict},
        )
    source_ids = {str(item.get("source_id")) for item in evidence}
    cleaned = validate_citations(verdict, source_ids)
    if not evidence:
        cleaned = "External fact verification was unavailable or insufficient; this is logic and context analysis only.\n\n" + cleaned
    if evidence:
        cleaned += "\n\nSources:\n" + "\n".join(
            f"{item['source_id']} — {item['title']} — {item['url']}"
            for item in evidence
        )
    if unverified_claims:
        cleaned += "\n\nUnverified claims:\n" + "\n".join(
            f"{item['claim_id']} — {item['status']}"
            for item in unverified_claims
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
    await _require_owned(repository, lease)
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
    except _PermanentWork as exc:
        try:
            await _sync(
                repository.finish,
                lease,
                "failed",
                error_class=exc.error_class,
                failure_notice=True,
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
        if acquisition.job is not None and acquisition.job.state == "failed":
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
    takeover = await _sync(
        repository.failure_takeover,
        job_id,
        source_message_id,
        max_retries=max_retries,
    )
    if takeover.status in {"metadata_pending", "busy"}:
        return _retry_response(takeover.retry_after)
    if takeover.status == "mismatch":
        return Response(status_code=401)
    if takeover.status in {"failed", "terminal"}:
        completed, retry_after = await _failure_notice(repository, job_id)
        if not completed:
            return _retry_response(retry_after)
    return JSONResponse({"ok": True}, status_code=200)
