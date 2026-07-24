from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import time
from collections.abc import Callable

import jwt
import pytest

from app.llm.client import LLMPermanentError, LLMRetryableError
from app.queue.qstash import PUBLISH_RETRIES, failure_url, process_url
from app.search.tavily import SearchSource
from app.search.tavily import (
    MAX_RESULTS as MAX_GOOGLE_SOURCES,
    MAX_SOURCE_TITLE_CHARS,
    MAX_SOURCE_URL_CHARS,
)
from app.settings import settings
from app.store import history
from app.store import jobs as jobs_module
from app.store.jobs import (
    FAILURE_NOTICE_TEXT,
    JobLease,
    JobRepository,
    get_job_repository,
)
from app.telegram import processor
from app.telegram.client import TelegramAPIError
from app.telegram.identity import BotIdentity
from app.telegram.job_contract import (
    JOB_SNAPSHOT_VERSION,
    MAX_GENERATED_RESPONSE_CHARS,
    MAX_SAVED_ANSWER_CHARS,
)


CURRENT_KEY = "current-signing-key-with-at-least-32-bytes"
NEXT_KEY = "next-signing-key-with-at-least-32-bytes"
BOT_IDENTITY = BotIdentity(
    id=999,
    username="test_bot",
    first_name="Test Bot",
)
ADDRESSED_PLACEHOLDER = "Alice, Thinking…"
ADDRESSED_FAILURE_NOTICE = f"Alice, {FAILURE_NOTICE_TEXT}"


def _now() -> int:
    return int(time.time())


@pytest.fixture(autouse=True)
def processor_configuration(fresh_store: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_generate(
        _: list[object], *, thinking: bool = False
    ) -> str:
        pytest.fail("a test must explicitly install its LLM fake")

    def unexpected_telegram(*_: object, **__: object) -> dict[str, object]:
        pytest.fail("a test must explicitly install its Telegram fake")

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bot.example")
    monkeypatch.setattr(settings, "QSTASH_CURRENT_SIGNING_KEY", CURRENT_KEY)
    monkeypatch.setattr(settings, "QSTASH_NEXT_SIGNING_KEY", NEXT_KEY)
    monkeypatch.setattr(settings, "WORKER_BUDGET_SECONDS", 240)
    monkeypatch.setattr(settings, "JOB_LEASE_SECONDS", 270)
    monkeypatch.setattr(processor, "get_bot_identity", lambda: BOT_IDENTITY)
    monkeypatch.setattr(processor, "generate", unexpected_generate)
    monkeypatch.setattr(processor.telegram_client, "send_message", unexpected_telegram)
    monkeypatch.setattr(
        processor.telegram_client, "edit_message_text", unexpected_telegram
    )


def _signed_token(body: bytes, destination: str, key: str) -> str:
    body_hash = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode()
    now = _now()
    return jwt.encode(
        {
            "iss": "Upstash",
            "sub": destination,
            "exp": now + 60,
            "nbf": now - 1,
            "body": body_hash.rstrip("="),
        },
        key,
        algorithm="HS256",
    )


def _process_body(job_id: int | str) -> bytes:
    return json.dumps({"job_id": str(job_id)}, separators=(",", ":")).encode()


def _post_process(
    client,
    job_id: int | str,
    *,
    key: str = CURRENT_KEY,
    raw_body: bytes | None = None,
    signature: str | None = None,
):
    body = raw_body if raw_body is not None else _process_body(job_id)
    signed = signature or _signed_token(body, process_url(), key)
    return client.post(
        "/api/telegram/process",
        content=body,
        headers={"Upstash-Signature": signed, "Content-Type": "application/json"},
    )


def _failure_payload(
    job_id: int | str,
    source_message_id: str,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "sourceBody": base64.b64encode(_process_body(job_id)).decode(),
        "sourceMessageId": source_message_id,
        "url": process_url(),
        "method": "POST",
        "retried": PUBLISH_RETRIES,
        "maxRetries": PUBLISH_RETRIES,
        # QStash may include a response body. The worker must ignore it.
        "responseBody": "untrusted-private-response-canary",
    }
    payload.update(overrides)
    return payload


def _post_failure(
    client,
    payload: dict[str, object],
    *,
    key: str = CURRENT_KEY,
    signature: str | None = None,
):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signed = signature or _signed_token(body, failure_url(), key)
    return client.post(
        "/api/telegram/failure",
        content=body,
        headers={"Upstash-Signature": signed, "Content-Type": "application/json"},
    )


def _request(job_id: int, *, chat_id: int = 100) -> dict[str, object]:
    timestamp = _now() - 10
    trigger = {
        "message_id": job_id + 1_000,
        "source_update_id": job_id,
        "user_id": 5,
        "username": "alice",
        "name": "Alice",
        "text": "@test_bot answer this",
        "ts": timestamp,
        "edit_ts": None,
        "is_edited": False,
        "is_bot": False,
        "reply_to": None,
    }
    return {
        "version": JOB_SNAPSHOT_VERSION,
        "kind": "reply",
        "route": "mention",
        "chat_id": chat_id,
        "update_id": job_id,
        "trigger_message_id": trigger["message_id"],
        "author": {"id": 5, "name": "Alice", "username": "alice"},
        "trigger": trigger,
        "trigger_text": trigger["text"],
        "trigger_entities": [{"type": "mention", "offset": 0, "length": 9}],
        "reply_context": None,
        "context": [],
        "received_at": timestamp,
    }


def _create_job(
    job_id: int,
    *,
    repository: JobRepository | None = None,
    published: bool = True,
    now: int | None = None,
):
    repo = repository or get_job_repository()
    job = repo.create_reply_job(
        _request(job_id),
        {
            "tone_preset": "neutral",
            "list_policies": [],
            "rule_policies": [],
        },
        [5],
        now=now,
    )
    if published:
        job = repo.record_publication(
            job_id,
            f"qstash-{job_id}",
            now=now,
        )
    return job


def _create_google_job(
    job_id: int,
    *,
    repository: JobRepository | None = None,
):
    repo = repository or get_job_repository()
    request = _request(job_id)
    request["kind"] = "google"
    request["query"] = "current public fact"
    request["context"] = []
    repo.create_reply_job(
        request,
        {
            "tone_preset": "neutral",
            "list_policies": [],
            "rule_policies": [],
        },
        [5],
    )
    return repo.record_publication(job_id, f"qstash-{job_id}")


def _telegram_result(
    message_id: int,
    text: str,
    *,
    chat_id: int = 100,
    reply_to_message_id: int | None = None,
    edited: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "message_id": message_id,
        "date": _now(),
        "chat": {"id": chat_id, "type": "group"},
        "from": {
            "id": BOT_IDENTITY.id,
            "is_bot": True,
            "first_name": "Test Bot",
            "username": BOT_IDENTITY.username,
        },
        "text": text,
    }
    if reply_to_message_id is not None:
        result["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "date": _now() - 10,
            "from": {"id": 5, "is_bot": False, "first_name": "Alice"},
            "text": "trigger",
        }
    if edited:
        result["edit_date"] = _now()
    return result


class FakeTelegram:
    def __init__(self, *, events: list[str] | None = None) -> None:
        self.send_calls: list[tuple[int, str, int | None]] = []
        self.edit_calls: list[tuple[int, int, str]] = []
        self.edit_errors: list[TelegramAPIError] = []
        self.send_errors: list[TelegramAPIError] = []
        self.next_message_id = 5_000
        self.events = events if events is not None else []
        self.before_answer_delivery: Callable[[str], None] | None = None

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, object]:
        self.send_calls.append((chat_id, text, reply_to_message_id))
        self.events.append(f"send:{text[:16]}")
        if not text.endswith(processor.PLACEHOLDER_TEXT) and self.before_answer_delivery:
            self.before_answer_delivery(text)
        if self.send_errors:
            raise self.send_errors.pop(0)
        message_id = self.next_message_id
        self.next_message_id += 1
        return _telegram_result(
            message_id,
            text,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
        )

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str
    ) -> dict[str, object]:
        self.edit_calls.append((chat_id, message_id, text))
        self.events.append(f"edit:{text[:16]}")
        if text != FAILURE_NOTICE_TEXT and self.before_answer_delivery:
            self.before_answer_delivery(text)
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        return _telegram_result(
            message_id,
            text,
            chat_id=chat_id,
            edited=True,
        )


def _install_telegram(monkeypatch: pytest.MonkeyPatch, fake: FakeTelegram) -> None:
    monkeypatch.setattr(processor.telegram_client, "send_message", fake.send_message)
    monkeypatch.setattr(
        processor.telegram_client, "edit_message_text", fake.edit_message_text
    )


def _install_answer(
    monkeypatch: pytest.MonkeyPatch,
    answer: str | BaseException,
    *,
    events: list[str] | None = None,
) -> list[list[object]]:
    calls: list[list[object]] = []

    async def generate(
        messages: list[object], *, thinking: bool = False
    ) -> str:
        calls.append(messages)
        if events is not None:
            events.append("flash")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(processor, "generate", generate)
    return calls


@pytest.mark.parametrize("key", [CURRENT_KEY, NEXT_KEY])
def test_process_accepts_both_signing_keys_over_the_exact_raw_body(
    client, key: str
) -> None:
    raw_body = b'{\n  "job_id" : "999999"\n}'

    response = _post_process(client, 999_999, key=key, raw_body=raw_body)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_process_rejects_bad_signature_and_invalid_body_before_state_change(
    client,
) -> None:
    repository = get_job_repository()
    _create_job(300, repository=repository)
    valid_body = _process_body(300)

    bad_signature = _post_process(
        client,
        300,
        raw_body=valid_body,
        signature="not-a-valid-signature",
    )
    invalid_body = b'{"job_id":300}'
    signed_invalid_body = _post_process(
        client,
        300,
        raw_body=invalid_body,
        signature=_signed_token(invalid_body, process_url(), CURRENT_KEY),
    )

    assert bad_signature.status_code == 401
    assert signed_invalid_body.status_code == 400
    job = repository.get(300)
    assert job is not None
    assert job.state == "enqueued"
    assert job.attempts == 0


def test_public_worker_routes_reject_oversized_bodies_before_signature_or_parse(
    client,
) -> None:
    webhook = client.post(
        "/api/telegram/webhook",
        content=b"x" * 1_000_001,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    process = client.post(
        "/api/telegram/process",
        content=b"x" * (processor.MAX_PROCESS_BODY_BYTES + 1),
    )
    failure = client.post(
        "/api/telegram/failure",
        content=b"x" * (processor.MAX_FAILURE_BODY_BYTES + 1),
    )

    assert webhook.status_code == 413
    assert process.status_code == 400
    assert failure.status_code == 400


def test_complete_worker_journey_saves_answer_delivers_and_upserts_history(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(301, repository=repository)
    events: list[str] = []
    telegram = FakeTelegram(events=events)
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "A grounded answer.", events=events)

    response = _post_process(client, 301)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert events == [
        "send:Alice, Thinking…",
        "flash",
        "edit:Alice, A grounde",
    ]
    assert len(llm_calls) == 1
    assert telegram.send_calls == [(100, ADDRESSED_PLACEHOLDER, 1_301)]
    assert telegram.edit_calls == [(100, 5_000, "Alice, A grounded answer.")]
    job = repository.get(301)
    assert job is not None
    assert job.state == "delivered"
    assert job.answer_text == "Alice, A grounded answer."
    assert job.attempts == 1
    assert job.checkpoint("placeholder") is not None
    assert job.checkpoint("answer_edit") is not None
    records = history.recent(100)
    assert [(record["message_id"], record["text"]) for record in records] == [
        (5_000, "Alice, A grounded answer.")
    ]
    assert records[0]["name"] == "Test Bot"


def test_retry_reuses_saved_answer_placeholder_and_safe_edit_intent(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(302, repository=repository)
    telegram = FakeTelegram()
    telegram.edit_errors.append(
        TelegramAPIError(
            "safe retryable error",
            method="editMessageText",
            status_code=503,
        )
    )
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "Saved before delivery")

    first = _post_process(client, 302)
    after_first = repository.get(302)
    assert first.status_code == 503
    assert after_first is not None
    assert after_first.state == "failed_retryable"
    assert after_first.answer_text == "Alice, Saved before delivery"
    assert after_first.checkpoint("placeholder") is not None
    assert after_first.checkpoint("answer_edit") is None

    second = _post_process(client, 302)

    assert second.status_code == 200
    assert len(llm_calls) == 1
    assert len(telegram.send_calls) == 1
    assert len(telegram.edit_calls) == 2
    completed = repository.get(302)
    assert completed is not None
    assert completed.state == "delivered"
    assert completed.attempts == 2


def test_malformed_2xx_send_result_is_ambiguous_and_never_checkpointed(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(302_1, repository=repository)

    def malformed_result(*_: object, **__: object) -> dict[str, object]:
        return {"message_id": 5_000, "date": _now(), "chat": {"id": 100}}

    monkeypatch.setattr(processor.telegram_client, "send_message", malformed_result)
    _install_answer(monkeypatch, "must not run")

    response = _post_process(client, 302_1)

    assert response.status_code == 200
    job = repository.get(302_1)
    assert job is not None
    assert job.state == "failed_ambiguous"
    assert job.checkpoint("placeholder") is None
    assert job.intent("placeholder") is not None


def test_long_answer_is_saved_then_delivered_in_order_without_duplicate_retry(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(303, repository=repository)
    answer = "a" * 4_000 + "b" * 4_000 + "c" * 501
    addressed_answer = f"Alice, {answer}"
    chunks = processor.telegram_client.split_plain_text(addressed_answer)
    telegram = FakeTelegram()
    telegram.before_answer_delivery = lambda _text: (
        (
            repository.get(303) is not None
            and repository.get(303).answer_text
            == addressed_answer  # type: ignore[union-attr]
        )
        or pytest.fail("answer must be durable before answer delivery")
    )
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, answer)

    first = _post_process(client, 303)
    completed = repository.get(303)

    assert first.status_code == 200
    assert completed is not None and completed.state == "delivered"
    assert telegram.edit_calls == [(100, 5_000, chunks[0])]
    assert [call[1] for call in telegram.send_calls] == [
        ADDRESSED_PLACEHOLDER,
        *chunks[1:],
    ]
    assert [call[2] for call in telegram.send_calls] == [
        1_303,
        *([None] * (len(chunks) - 1)),
    ]
    assert [
        record["text"] for record in reversed(history.recent(100))
    ] == chunks

    duplicate = _post_process(client, 303)
    assert duplicate.status_code == 200
    assert len(llm_calls) == 1
    assert len(telegram.send_calls) == 3
    assert len(telegram.edit_calls) == 1


def test_maximum_generated_answer_still_fits_after_addressing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(30301, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, "x" * MAX_GENERATED_RESPONSE_CHARS)

    response = _post_process(client, 30301)

    delivered = repository.get(30301)
    assert response.status_code == 200
    assert delivered is not None and delivered.state == "delivered"
    assert delivered.answer_text is not None
    assert delivered.answer_text.startswith("Alice, ")
    assert len(delivered.answer_text) == MAX_GENERATED_RESPONSE_CHARS + len(
        "Alice, "
    )


def test_crash_after_later_chunk_send_intent_becomes_ambiguous_without_resend(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(303_1, repository=repository)
    chunks = processor.telegram_client.split_plain_text(
        "Alice, " + "a" * 4_000 + "b"
    )
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, "a" * 4_000 + "b")
    real_checkpoint = repository.checkpoint

    def crash_later_chunk(
        lease: JobLease,
        *,
        name: str,
        result: dict[str, object],
        now: int | None = None,
    ) -> None:
        if name == "chunk:1":
            raise RuntimeError("crash after later sendMessage")
        real_checkpoint(lease, name=name, result=result, now=now)

    monkeypatch.setattr(repository, "checkpoint", crash_later_chunk)
    first = _post_process(client, 303_1)
    retry = _post_process(client, 303_1)

    assert first.status_code == 503
    assert retry.status_code == 200
    job = repository.get(303_1)
    assert job is not None and job.state == "failed_ambiguous"
    assert [text for _, text, _ in telegram.send_calls] == [
        ADDRESSED_PLACEHOLDER,
        chunks[1],
    ]


def test_history_write_failure_reuses_the_checkpoint_without_a_duplicate_send(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(303_2, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, "answer after history retry")
    real_history_upsert = processor.history.upsert
    failed_once = False

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("temporary history write failure")
        real_history_upsert(*args, **kwargs)

    monkeypatch.setattr(processor.history, "upsert", fail_once)
    first = _post_process(client, 303_2)
    retry = _post_process(client, 303_2)

    assert first.status_code == 503
    assert retry.status_code == 200
    assert [text for _, text, _ in telegram.send_calls] == [
        ADDRESSED_PLACEHOLDER
    ]
    assert len(telegram.edit_calls) == 1


def test_terminal_finish_failure_retries_checkpoints_without_duplicate_delivery(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(303_3, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, "finish retry")
    real_finish = repository.finish
    failed_once = False

    def fail_delivered_once(
        lease: JobLease,
        target_state: str,
        **kwargs: object,
    ) -> None:
        nonlocal failed_once
        if target_state == "delivered" and not failed_once:
            failed_once = True
            raise RuntimeError("temporary finish failure")
        real_finish(lease, target_state, **kwargs)

    monkeypatch.setattr(repository, "finish", fail_delivered_once)
    first = _post_process(client, 303_3)
    retry = _post_process(client, 303_3)

    assert first.status_code == 503
    assert retry.status_code == 200
    assert [text for _, text, _ in telegram.send_calls] == [
        ADDRESSED_PLACEHOLDER
    ]
    assert len(telegram.edit_calls) == 1


def test_hard_worker_budget_marks_the_job_retryable(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(303_4, repository=repository)

    async def slow_delivery(*_: object) -> None:
        await asyncio.sleep(0.02)

    monkeypatch.setattr(settings, "WORKER_BUDGET_SECONDS", 0.001)
    monkeypatch.setattr(processor, "_run_delivery", slow_delivery)
    response = _post_process(client, 303_4)

    assert response.status_code == 503
    job = repository.get(303_4)
    assert job is not None
    assert job.state == "failed_retryable"
    assert job.error_class == "worker_budget_exceeded"


def test_active_lease_returns_retry_after_without_side_effects(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(304, repository=repository)
    acquisition = repository.acquire(304, token="active-worker")
    assert acquisition.status == "acquired" and acquisition.lease is not None
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "must not run")

    response = _post_process(client, 304)

    assert response.status_code == 503
    assert int(response.headers["Retry-After"]) >= 1
    assert telegram.send_calls == []
    assert telegram.edit_calls == []
    assert llm_calls == []
    assert repository.get(304).attempts == 1  # type: ignore[union-attr]


def test_renewal_error_stops_the_background_task_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRepository:
        def renew(self, _lease: JobLease) -> bool:
            raise RuntimeError("transient Redis failure")

    monkeypatch.setattr(processor, "LEASE_RENEW_INTERVAL_SECONDS", 0.001)
    lease = JobLease("304", "worker", 1, 1)

    asyncio.run(
        asyncio.wait_for(
            processor._renew_lease(BrokenRepository(), lease, asyncio.Event()),
            timeout=0.1,
        )
    )


def test_release_error_does_not_override_a_completed_worker_response(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(304_1, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, "complete despite cleanup failure")

    def broken_release(*_: object, **__: object) -> bool:
        raise RuntimeError("temporary Redis release failure")

    monkeypatch.setattr(repository, "release", broken_release)
    response = _post_process(client, 304_1)

    assert response.status_code == 200
    assert repository.get(304_1).state == "delivered"  # type: ignore[union-attr]


def test_expired_lease_takeover_uses_a_new_fence(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1_000_000}
    monkeypatch.setattr(jobs_module, "utc_now", lambda: clock["now"])
    repository = get_job_repository()
    _create_job(305, repository=repository, now=clock["now"])
    old = repository.acquire(305, token="old-worker", now=clock["now"])
    assert old.status == "acquired" and old.lease is not None
    old_lease = old.lease
    clock["now"] += settings.JOB_LEASE_SECONDS + 1
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, "taken over safely")

    response = _post_process(client, 305)

    assert response.status_code == 200
    assert repository.guard(old_lease) is False
    completed = repository.get(305)
    assert completed is not None
    assert completed.state == "delivered"
    assert completed.attempts == 2
    assert completed.fence > old_lease.fence


def test_retryable_provider_failure_is_sanitized_and_requeued(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(306, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, LLMRetryableError("provider_rate_limited"))

    response = _post_process(client, 306)

    assert response.status_code == 503
    assert "Retry-After" not in response.headers
    job = repository.get(306)
    assert job is not None
    assert job.state == "failed_retryable"
    assert job.error_class == "provider_rate_limited"
    assert len(telegram.send_calls) == 1
    assert telegram.edit_calls == []


def test_google_reuses_search_checkpoint_and_enables_thinking_on_llm_retry(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_google_job(306_1, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    search_calls: list[tuple[str, bool]] = []
    generation_modes: list[bool] = []
    responses: list[str | BaseException] = [
        LLMRetryableError("provider_rate_limited"),
        "Grounded answer [S1].",
    ]

    async def search(
        query: str, *, explicit: bool = False, **_kwargs: object
    ) -> list[SearchSource]:
        search_calls.append((query, explicit))
        return [
            SearchSource(
                source_id="provider-source",
                title="Public source",
                url="https://example.test/source",
                snippet="Public evidence.",
            )
        ]

    async def generate(
        _messages: list[object], *, thinking: bool = False
    ) -> str:
        generation_modes.append(thinking)
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(processor, "tavily_search", search)
    monkeypatch.setattr(processor, "generate", generate)

    first = _post_process(client, 306_1)
    retry = _post_process(client, 306_1)

    assert first.status_code == 503
    assert retry.status_code == 200
    assert search_calls == [("current public fact", True)]
    assert generation_modes == [True, True]
    delivered = repository.get(306_1)
    assert delivered is not None and delivered.state == "delivered"
    assert delivered.checkpoint("google_search") is not None
    assert delivered.answer_text == (
        "Alice, Grounded answer [S1].\n\n"
        "Sources:\nS1 — Public source — https://example.test/source"
    )
    assert telegram.send_calls[0][1] == "Alice, Thinking…"


def test_maximum_google_answer_with_maximum_sources_is_saved_and_delivered(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_google_job(306_11, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    maximum_url = "https://example.test/" + (
        "x" * (MAX_SOURCE_URL_CHARS - len("https://example.test/"))
    )
    maximum_sources = [
        SearchSource(
            source_id=f"S{index}",
            title="T" * MAX_SOURCE_TITLE_CHARS,
            url=maximum_url,
            snippet="evidence",
        )
        for index in range(1, MAX_GOOGLE_SOURCES + 1)
    ]

    async def search(
        _query: str, *, explicit: bool = False, **_kwargs: object
    ) -> list[SearchSource]:
        assert explicit is True
        return maximum_sources

    monkeypatch.setattr(processor, "tavily_search", search)
    _install_answer(monkeypatch, "x" * MAX_GENERATED_RESPONSE_CHARS)

    response = _post_process(client, 306_11)

    delivered = repository.get(306_11)
    assert response.status_code == 200
    assert delivered is not None and delivered.state == "delivered"
    assert delivered.answer_text is not None
    assert len(delivered.answer_text) <= MAX_SAVED_ANSWER_CHARS
    assert delivered.answer_text.startswith("Alice, " + "x" * 100)
    for index in range(1, MAX_GOOGLE_SOURCES + 1):
        assert f"\nS{index} — " in delivered.answer_text
    assert len(telegram.edit_calls) == 1


def test_google_ambiguous_search_intent_never_repeats_paid_search(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_google_job(306_2, repository=repository)
    acquired = repository.acquire(306_2, token="crashed-search-worker")
    assert acquired.status == "acquired" and acquired.lease is not None
    intent = repository.prepare_intent(
        acquired.lease,
        name="google_search",
        kind="externalSearch",
        chunk_index=0,
        payload_hash=hashlib.sha256(b"current public fact").hexdigest(),
        ambiguous_on_takeover=True,
    )
    assert intent.status == "prepared"
    assert repository.release(acquired.lease) is True
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)

    async def unexpected(*_args: object, **_kwargs: object):
        pytest.fail("an ambiguous paid search must never be repeated")

    monkeypatch.setattr(processor, "tavily_search", unexpected)
    monkeypatch.setattr(processor, "generate", unexpected)

    response = _post_process(client, 306_2)

    assert response.status_code == 200
    terminal = repository.get(306_2)
    assert terminal is not None and terminal.state == "failed_ambiguous"
    assert terminal.error_class == "google_search_ambiguous"


def test_standard_reply_disables_thinking_and_prefixes_saved_answer(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(306_3, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    modes: list[bool] = []

    async def generate(
        _messages: list[object], *, thinking: bool = False
    ) -> str:
        modes.append(thinking)
        return "Alice, Alice, Final answer."

    monkeypatch.setattr(processor, "generate", generate)

    response = _post_process(client, 306_3)

    assert response.status_code == 200
    assert modes == [False]
    delivered = repository.get(306_3)
    assert delivered is not None
    assert delivered.answer_text == "Alice, Final answer."
    assert telegram.edit_calls == [(100, 5_000, "Alice, Final answer.")]


def test_legacy_snapshot_with_saved_answer_is_rejected_before_delivery(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    legacy_request = _request(306_4)
    legacy_request["version"] = 1
    repository.create_reply_job(
        legacy_request,
        {
            "tone_preset": "neutral",
            "list_policies": [],
            "rule_policies": [],
        },
        [5],
    )
    repository.record_publication(306_4, "qstash-3064")
    acquired = repository.acquire(306_4, token="legacy-writer")
    assert acquired.status == "acquired" and acquired.lease is not None
    repository.save_answer(acquired.lease, "Legacy unaddressed answer.")
    assert repository.release(acquired.lease) is True
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "must not run")

    response = _post_process(client, 306_4)

    assert response.status_code == 200
    failed = repository.get(306_4)
    assert failed is not None and failed.state == "failed"
    assert failed.error_class == "job_snapshot_unsupported"
    assert telegram.send_calls == []
    assert telegram.edit_calls == []
    assert llm_calls == []


def test_current_snapshot_rejects_removed_job_kind_before_side_effects(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    request = _request(306_5)
    request["kind"] = "judge"
    repository.create_reply_job(
        request,
        {
            "tone_preset": "neutral",
            "list_policies": [],
            "rule_policies": [],
        },
        [5],
    )
    repository.record_publication(306_5, "qstash-3065")
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "must not run")

    response = _post_process(client, 306_5)

    assert response.status_code == 200
    failed = repository.get(306_5)
    assert failed is not None and failed.state == "failed"
    assert failed.error_class == "job_snapshot_unsupported"
    assert telegram.send_calls == []
    assert telegram.edit_calls == []
    assert llm_calls == []


def test_legacy_snapshot_with_placeholder_has_no_new_telegram_side_effect(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    legacy_request = _request(306_6)
    legacy_request["version"] = 1
    repository.create_reply_job(
        legacy_request,
        {
            "tone_preset": "neutral",
            "list_policies": [],
            "rule_policies": [],
        },
        [5],
    )
    repository.record_publication(306_6, "qstash-3066")
    _seed_placeholder(repository, 306_6)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "must not run")

    response = _post_process(client, 306_6)

    assert response.status_code == 200
    failed = repository.get(306_6)
    assert failed is not None and failed.state == "failed"
    assert failed.error_class == "job_snapshot_unsupported"
    assert failed.failure_notice_state == "none"
    assert telegram.send_calls == []
    assert telegram.edit_calls == []
    assert llm_calls == []


def test_invalid_current_snapshot_with_placeholder_has_no_side_effect(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    invalid_request = _request(306_7)
    invalid_request["author"] = {"id": 5, "name": "", "username": "alice"}
    repository.create_reply_job(
        invalid_request,
        {
            "tone_preset": "neutral",
            "list_policies": [],
            "rule_policies": [],
        },
        [5],
    )
    repository.record_publication(306_7, "qstash-3067")
    _seed_placeholder(repository, 306_7)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "must not run")

    response = _post_process(client, 306_7)

    assert response.status_code == 200
    failed = repository.get(306_7)
    assert failed is not None and failed.state == "failed"
    assert failed.error_class == "job_snapshot_invalid"
    assert failed.failure_notice_state == "none"
    assert telegram.send_calls == []
    assert telegram.edit_calls == []
    assert llm_calls == []


def test_permanent_provider_failure_is_terminal_and_edits_known_placeholder(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(307, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, LLMPermanentError("provider_auth"))

    response = _post_process(client, 307)

    assert response.status_code == 200
    job = repository.get(307)
    assert job is not None
    assert job.state == "failed"
    assert job.error_class == "provider_auth"
    assert job.failure_notice_state == "delivered"
    assert len(telegram.send_calls) == 1
    assert telegram.edit_calls == [(100, 5_000, ADDRESSED_FAILURE_NOTICE)]


def test_crash_after_send_intent_becomes_ambiguous_without_resending(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(308, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    llm_calls = _install_answer(monkeypatch, "must never be generated")
    real_checkpoint = repository.checkpoint
    crashed = False

    def crash_once(
        lease: JobLease,
        *,
        name: str,
        result: dict[str, object],
        now: int | None = None,
    ) -> None:
        nonlocal crashed
        if name == "placeholder" and not crashed:
            crashed = True
            raise RuntimeError("simulated crash after Telegram accepted sendMessage")
        real_checkpoint(lease, name=name, result=result, now=now)

    monkeypatch.setattr(repository, "checkpoint", crash_once)

    first = _post_process(client, 308)
    assert first.status_code == 503
    pending = repository.get(308)
    assert pending is not None
    assert pending.state == "failed_retryable"
    assert pending.intent("placeholder") is not None
    assert pending.checkpoint("placeholder") is None
    assert len(telegram.send_calls) == 1

    retry = _post_process(client, 308)

    assert retry.status_code == 200
    terminal = repository.get(308)
    assert terminal is not None
    assert terminal.state == "failed_ambiguous"
    assert terminal.error_class == "telegram_send_ambiguous"
    assert len(telegram.send_calls) == 1
    assert llm_calls == []


def test_exact_message_not_modified_is_success_for_known_edit_intent(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(309, repository=repository)
    telegram = FakeTelegram()
    telegram.edit_errors.append(
        TelegramAPIError(
            "safe exact edit result",
            method="editMessageText",
            status_code=400,
            description=(
                "Bad Request: message is not modified: specified new message content "
                "and reply markup are exactly the same as a current content and "
                "reply markup of the message"
            ),
        )
    )
    _install_telegram(monkeypatch, telegram)
    _install_answer(monkeypatch, "already edited answer")

    response = _post_process(client, 309)

    assert response.status_code == 200
    job = repository.get(309)
    assert job is not None
    assert job.state == "delivered"
    checkpoint = job.checkpoint("answer_edit")
    assert checkpoint is not None
    assert checkpoint["text"] == "Alice, already edited answer"
    assert checkpoint["edit_date"] is None
    assert checkpoint["_edit_recovered"] is True
    recovered = history.recent(100)[0]
    assert recovered["text"] == "Alice, already edited answer"
    assert recovered["edit_ts"] is None


@pytest.mark.parametrize(
    "case",
    [
        {"empty": True},
        {"sourceBody": "not-base64!"},
        {"sourceBody": base64.b64encode(b"x" * 257).decode()},
        {"url": "https://wrong.example/process"},
        {"method": "GET"},
        {"retried": PUBLISH_RETRIES - 1},
        {"maxRetries": PUBLISH_RETRIES + 1},
        {"sourceMessageId": ""},
    ],
)
def test_failure_callback_rejects_malformed_source_and_metadata(
    client, case: dict[str, object]
) -> None:
    payload = (
        {} if case.get("empty") is True else _failure_payload(320, "qstash-320", **case)
    )
    response = _post_failure(client, payload)

    assert response.status_code == 400


def test_failure_callback_rejects_bad_signature_before_lookup(client) -> None:
    response = _post_failure(
        client,
        _failure_payload(321, "qstash-321"),
        signature="not-a-valid-signature",
    )

    assert response.status_code == 401
    assert get_job_repository().get(321) is None


def test_failure_callback_waits_for_publication_metadata_then_checks_message_id(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(322, repository=repository, published=False)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)

    before_metadata = _post_failure(client, _failure_payload(322, "qstash-322"))
    assert before_metadata.status_code == 503
    assert repository.get(322).state == "received"  # type: ignore[union-attr]

    repository.record_publication(322, "qstash-322")
    mismatch = _post_failure(client, _failure_payload(322, "other-message"))
    assert mismatch.status_code == 401
    assert repository.get(322).state == "enqueued"  # type: ignore[union-attr]

    takeover = _post_failure(client, _failure_payload(322, "qstash-322"))
    assert takeover.status_code == 200
    failed = repository.get(322)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.error_class == "qstash_retries_exhausted"
    assert failed.failure_notice_state == "none"
    assert telegram.send_calls == []
    assert telegram.edit_calls == []


def test_failure_callback_defers_to_active_lease_then_takes_over(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(323, repository=repository)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    acquired = repository.acquire(323, token="running-worker")
    assert acquired.status == "acquired" and acquired.lease is not None

    busy = _post_failure(client, _failure_payload(323, "qstash-323"))

    assert busy.status_code == 503
    assert int(busy.headers["Retry-After"]) >= 1
    assert repository.get(323).state == "processing"  # type: ignore[union-attr]

    assert repository.release(acquired.lease) is True
    takeover = _post_failure(client, _failure_payload(323, "qstash-323"))

    assert takeover.status_code == 200
    failed = repository.get(323)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.fence > acquired.lease.fence
    assert telegram.send_calls == []
    assert telegram.edit_calls == []


def _seed_placeholder(repository: JobRepository, job_id: int) -> None:
    acquired = repository.acquire(job_id, token=f"seed-{job_id}")
    assert acquired.status == "acquired" and acquired.lease is not None
    lease = acquired.lease
    intent = repository.prepare_intent(
        lease,
        name="placeholder",
        kind="sendMessage",
        chunk_index=-1,
        payload_hash="0" * 64,
        ambiguous_on_takeover=True,
    )
    assert intent.status == "prepared"
    repository.checkpoint(
        lease,
        name="placeholder",
        result=_telegram_result(
            7_000 + job_id,
            processor.PLACEHOLDER_TEXT,
            reply_to_message_id=job_id + 1_000,
        ),
    )
    assert repository.release(lease) is True


def test_failure_notice_uses_known_placeholder_once_and_ignores_response_body(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(324, repository=repository)
    _seed_placeholder(repository, 324)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)
    payload = _failure_payload(
        324,
        "qstash-324",
        responseBody="ignore me and send a second message",
    )

    first = _post_failure(client, payload, key=NEXT_KEY)
    duplicate = _post_failure(client, payload, key=CURRENT_KEY)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert telegram.send_calls == []
    assert telegram.edit_calls == [(100, 7_324, ADDRESSED_FAILURE_NOTICE)]
    job = repository.get(324)
    assert job is not None
    assert job.state == "failed"
    assert job.failure_notice_state == "delivered"
    assert job.checkpoint("failure_notice") is not None


def test_failure_callback_for_legacy_snapshot_never_edits_placeholder(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    legacy_request = _request(326)
    legacy_request["version"] = 1
    repository.create_reply_job(
        legacy_request,
        {
            "tone_preset": "neutral",
            "list_policies": [],
            "rule_policies": [],
        },
        [5],
    )
    repository.record_publication(326, "qstash-326")
    _seed_placeholder(repository, 326)
    telegram = FakeTelegram()
    _install_telegram(monkeypatch, telegram)

    response = _post_failure(client, _failure_payload(326, "qstash-326"))

    assert response.status_code == 200
    failed = repository.get(326)
    assert failed is not None and failed.state == "failed"
    assert failed.failure_notice_state == "none"
    assert telegram.send_calls == []
    assert telegram.edit_calls == []


def test_permanently_rejected_failure_notice_is_checkpointed_without_resend(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()
    _create_job(325, repository=repository)
    _seed_placeholder(repository, 325)
    telegram = FakeTelegram()
    telegram.edit_errors.append(
        TelegramAPIError(
            "safe permanent edit rejection",
            method="editMessageText",
            status_code=400,
            description="Bad Request: message cannot be edited",
        )
    )
    _install_telegram(monkeypatch, telegram)
    payload = _failure_payload(325, "qstash-325")

    first = _post_failure(client, payload)
    duplicate = _post_failure(client, payload)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert len(telegram.edit_calls) == 1
    assert telegram.send_calls == []
    job = repository.get(325)
    assert job is not None
    assert job.failure_notice_state == "failed_permanent"
    assert job.raw["failure_notice_error"] == "failure_notice_permanent"
