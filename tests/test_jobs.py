from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.settings import Settings
from app.store import jobs as jobs_module
from app.store.job_backend import MAX_PROCESS_ATTEMPTS, MemoryJobBackend
from app.store.jobs import (
    FAILURE_NOTICE_HASH,
    FAILURE_NOTICE_TEXT,
    JobIntegrityError,
    JobLease,
    JobRepository,
    JobStoreError,
    OwnershipLost,
    chat_index_key,
    failure_lease_key,
    job_key,
    lease_key,
    user_index_key,
)

NOW = 1_000


def _repository(*, retention: int = 100, lease: int = 10) -> JobRepository:
    config = Settings(
        _env_file=None,
        JOB_RETENTION_SECONDS=retention,
        JOB_LEASE_SECONDS=lease,
        WORKER_BUDGET_SECONDS=max(lease - 1, 1),
    )
    return JobRepository(MemoryJobBackend(), config)


def _request(
    job_id: int,
    *,
    chat_id: int = -100,
    author_id: int = 7,
    text: str = "original question",
) -> dict[str, object]:
    return {
        "kind": "reply",
        "update_id": job_id,
        "chat_id": chat_id,
        "trigger_message_id": job_id + 100,
        "author": {
            "id": author_id,
            "first_name": "Alice",
            "username": "alice",
        },
        "trigger": {"text": text, "entities": []},
        "reply_to": {
            "message_id": 80,
            "author": {"id": 8, "first_name": "Bob"},
            "text": "earlier reply",
        },
        "context": [
            {
                "message_id": 70,
                "author": {"id": 9, "first_name": "Carol"},
                "text": "context one",
            },
            {
                "message_id": 71,
                "author": {"id": 10, "first_name": "Dan"},
                "reply_to": {"author": {"id": 11, "first_name": "Eve"}},
                "text": "context two",
            },
        ],
    }


def _create(
    repository: JobRepository,
    job_id: int = 101,
    *,
    now: int = NOW,
    chat_id: int = -100,
    author_id: int = 7,
    user_ids: tuple[int, ...] = (8, 9, 10, 11),
):
    return repository.create_reply_job(
        _request(job_id, chat_id=chat_id, author_id=author_id),
        {"actor_id": author_id, "is_admin": False, "tone": "friendly"},
        user_ids,
        now=now,
    )


def _acquire(
    repository: JobRepository,
    job_id: int = 101,
    *,
    token: str = "worker-one",
    now: int = NOW,
) -> JobLease:
    acquisition = repository.acquire(job_id, token=token, now=now)
    assert acquisition.status == "acquired"
    assert acquisition.lease is not None
    return acquisition.lease


def _raw_fields(
    job_id: str,
    *,
    state: str = "received",
    now: int = NOW,
    expires_at: int = NOW + 100,
    attempts: int = 0,
) -> dict[str, str]:
    request_json = json.dumps(
        _request(int(job_id)), separators=(",", ":"), sort_keys=True
    )
    return {
        "job_id": job_id,
        "state": state,
        "request_json": request_json,
        "request_sha256": hashlib.sha256(request_json.encode()).hexdigest(),
        "effective_policy_json": "{}",
        "created_at": str(now),
        "updated_at": str(now),
        "expires_at": str(expires_at),
        "chat_id": "-100",
        "author_id": "7",
        "attempts": str(attempts),
        "fence": "0",
    }


def _failed_job_with_placeholder(
    repository: JobRepository,
    job_id: int = 101,
    *,
    now: int = NOW,
) -> int:
    _create(repository, job_id, now=now)
    lease = _acquire(repository, job_id, now=now)
    intent = repository.prepare_intent(
        lease,
        name="placeholder",
        kind="sendMessage",
        chunk_index=0,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
        now=now + 1,
    )
    assert intent.status == "prepared"
    repository.checkpoint(
        lease,
        name="placeholder",
        result={"message_id": 501},
        now=now + 1,
    )
    repository.finish(
        lease,
        "failed",
        error_class="provider_permanent",
        failure_notice=True,
        now=now + 2,
    )
    return 501


def test_creation_freezes_request_policy_and_indexes_every_supplied_user() -> None:
    repository = _repository()
    request = _request(101)
    policy = {"actor_id": 7, "is_admin": False, "nested": {"tone": "friendly"}}
    original_request = json.loads(json.dumps(request))
    original_policy = json.loads(json.dumps(policy))

    job = repository.create_reply_job(
        request,
        policy,
        [11, 10, 9, 8, 8, 0, -1],
        now=NOW,
    )

    assert job.state == "received"
    assert job.request == original_request
    assert job.effective_policy == original_policy
    assert len(job.request["context"]) == 2
    assert job.created_at == NOW
    assert job.updated_at == NOW
    assert job.expires_at == NOW + 100
    assert job.attempts == 0
    assert job.fence == 0

    raw = repository.backend.get("101", now=NOW)
    assert raw is not None
    assert (
        raw["request_sha256"]
        == hashlib.sha256(raw["request_json"].encode("utf-8")).hexdigest()
    )
    assert repository.index_job_ids(chat_index_key(-100), now=NOW) == ["101"]
    for user_id in (7, 8, 9, 10, 11):
        assert repository.index_job_ids(user_index_key(user_id), now=NOW) == ["101"]
    assert repository.index_job_ids(user_index_key(0), now=NOW) == []
    assert repository.index_job_ids(user_index_key(-1), now=NOW) == []

    request["context"][0]["text"] = "mutated after create"
    policy["nested"]["tone"] = "mutated after create"
    job.request["context"][0]["text"] = "mutated returned record"
    fetched = repository.get(101, now=NOW)
    assert fetched is not None
    assert fetched.request == original_request
    assert fetched.effective_policy == original_policy


def test_idempotent_create_reuses_original_snapshot_expiry_and_indexes() -> None:
    repository = _repository()
    first = _create(repository, now=NOW)
    replacement = _request(101, author_id=99, text="replacement")

    second = repository.create_reply_job(
        replacement,
        {"actor_id": 99, "is_admin": True},
        [99],
        now=NOW + 20,
    )

    assert second.request == first.request
    assert second.effective_policy == first.effective_policy
    assert second.created_at == NOW
    assert second.expires_at == NOW + 100
    assert repository.ttl(job_key(101), now=NOW + 20) == 80
    assert repository.ttl(chat_index_key(-100), now=NOW + 20) == 80
    assert repository.index_job_ids(user_index_key(99), now=NOW + 20) == []
    assert repository.index_job_ids(user_index_key(7), now=NOW + 20) == ["101"]


@pytest.mark.parametrize(
    ("request_change", "user_ids", "message"),
    [
        ({"update_id": True}, (), "update_id"),
        ({"chat_id": "-100"}, (), "chat_id"),
        ({"author": {"id": True}}, (), "author_id"),
        ({}, (True,), "user_id"),
    ],
)
def test_create_rejects_non_integer_identifiers(
    request_change: dict[str, object],
    user_ids: tuple[object, ...],
    message: str,
) -> None:
    repository = _repository()
    request = _request(101)
    request.update(request_change)

    with pytest.raises(ValueError, match=message):
        repository.create_reply_job(request, {}, user_ids, now=NOW)  # type: ignore[arg-type]


def test_absolute_expiry_removes_job_and_indexes_and_allows_recreation() -> None:
    repository = _repository(retention=5)
    original = _create(repository, now=NOW)

    assert repository.get(101, now=NOW + 4) == original
    assert repository.get(101, now=NOW + 5) is None
    assert repository.ttl(job_key(101), now=NOW + 5) == 0
    assert repository.index_job_ids(chat_index_key(-100), now=NOW + 5) == []
    assert repository.index_job_ids(user_index_key(7), now=NOW + 5) == []

    recreated = repository.create_reply_job(
        _request(101, text="new after retention"),
        {"version": 2},
        [7],
        now=NOW + 5,
    )
    assert recreated.request["trigger"]["text"] == "new after retention"
    assert recreated.created_at == NOW + 5


def test_concurrent_idempotent_creation_stores_one_snapshot_and_index_member() -> None:
    repository = _repository()

    def create_once(_: int):
        return _create(repository, now=NOW)

    with ThreadPoolExecutor(max_workers=16) as executor:
        records = list(executor.map(create_once, range(64)))

    assert {record.raw["request_sha256"] for record in records} == {
        records[0].raw["request_sha256"]
    }
    assert repository.index_job_ids(chat_index_key(-100), now=NOW) == ["101"]
    assert repository.index_job_ids(user_index_key(7), now=NOW) == ["101"]


def test_publication_is_idempotent_moves_received_and_never_downgrades() -> None:
    repository = _repository()
    _create(repository, 101)

    enqueued = repository.record_publication(101, "qstash-101", now=NOW + 1)
    assert enqueued.state == "enqueued"
    assert enqueued.qstash_message_id == "qstash-101"
    assert enqueued.raw["enqueued_at"] == str(NOW + 1)

    repeated = repository.record_publication(101, "qstash-101", now=NOW + 2)
    assert repeated.state == "enqueued"
    assert repeated.raw["enqueued_at"] == str(NOW + 1)

    _create(repository, 102)
    processing_lease = _acquire(repository, 102, now=NOW + 1)
    raced = repository.record_publication(102, "qstash-102", now=NOW + 2)
    assert raced.state == "processing"
    repository.save_answer(processing_lease, "answer", now=NOW + 3)
    repository.finish(processing_lease, "delivered", now=NOW + 4)
    terminal = repository.record_publication(102, "qstash-102", now=NOW + 5)
    assert terminal.state == "delivered"


def test_publication_rejects_conflicts_invalid_ids_and_missing_jobs() -> None:
    repository = _repository()
    _create(repository)
    repository.record_publication(101, "qstash-101", now=NOW)

    with pytest.raises(JobIntegrityError, match="qstash_message_id_conflict"):
        repository.record_publication(101, "qstash-other", now=NOW + 1)
    with pytest.raises(JobStoreError, match="job_missing"):
        repository.record_publication(999, "qstash-999", now=NOW)
    for invalid in ("", "bad\nvalue", "x" * 513):
        with pytest.raises(ValueError, match="message_id"):
            repository.record_publication(101, invalid, now=NOW)


def test_acquire_reports_missing_busy_terminal_invalid_and_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository()
    backend = repository.backend
    monkeypatch.setattr(jobs_module.secrets, "randbelow", lambda _: 0)

    missing = repository.acquire(999, token="missing", now=NOW)
    assert missing.status == "missing"
    assert missing.job is None

    _create(repository, 101)
    lease = _acquire(repository, 101, now=NOW)
    assert lease.attempt == 1
    busy = repository.acquire(101, token="other", now=NOW + 3)
    assert busy.status == "busy"
    assert busy.retry_after == 8

    repository.save_answer(lease, "answer", now=NOW + 4)
    repository.finish(lease, "delivered", now=NOW + 5)
    terminal = repository.acquire(101, token="late", now=NOW + 6)
    assert terminal.status == "terminal"
    assert terminal.job is not None
    assert terminal.job.state == "delivered"

    assert isinstance(backend, MemoryJobBackend)
    assert (
        backend.create(
            job_id="102",
            fields=_raw_fields("102", state="unexpected"),
            index_keys=[],
            now=NOW,
        )
        == "created"
    )
    assert backend.acquire("102", token="worker", lease_seconds=10, now=NOW) == {
        "status": "invalid_state",
        "state": "unexpected",
    }

    assert (
        backend.create(
            job_id="103",
            fields=_raw_fields("103", attempts=MAX_PROCESS_ATTEMPTS),
            index_keys=[],
            now=NOW,
        )
        == "created"
    )
    exhausted = repository.acquire(103, token="worker", now=NOW)
    assert exhausted.status == "exhausted"
    assert exhausted.job is not None
    assert exhausted.job.attempts == MAX_PROCESS_ATTEMPTS


def test_acquire_rejects_invalid_lease_tokens() -> None:
    repository = _repository()
    _create(repository)

    for token in ("", "x" * 129):
        with pytest.raises(ValueError, match="lease token"):
            repository.acquire(101, token=token, now=NOW)


def test_expired_lease_takeover_increments_fence_and_fences_old_owner() -> None:
    repository = _repository(lease=10)
    _create(repository)
    first = _acquire(repository, token="first", now=NOW)

    assert repository.guard(first, now=NOW + 9) is True
    assert repository.guard(first, now=NOW + 10) is False
    second = _acquire(repository, token="second", now=NOW + 10)
    assert second.fence == first.fence + 1
    assert second.attempt == 2
    assert repository.guard(second, now=NOW + 10) is True
    assert repository.renew(first, now=NOW + 10) is False
    assert repository.release(first, now=NOW + 10) is False
    with pytest.raises(OwnershipLost):
        repository.save_answer(first, "stale", now=NOW + 10)


def test_renew_and_guard_require_exact_owner_and_cap_lease_at_job_expiry() -> None:
    repository = _repository(retention=12, lease=10)
    _create(repository)
    lease = _acquire(repository, now=NOW)

    assert repository.ttl(lease_key(101), now=NOW) == 10
    assert repository.renew(replace(lease, token="wrong"), now=NOW + 5) is False
    assert repository.renew(replace(lease, fence=lease.fence + 1), now=NOW + 5) is False
    assert repository.renew(lease, now=NOW + 5) is True
    assert repository.ttl(lease_key(101), now=NOW + 5) == 7
    assert repository.ttl(job_key(101), now=NOW + 5) == 7
    assert repository.guard(lease, now=NOW + 11) is True
    assert repository.guard(lease, now=NOW + 12) is False
    assert repository.renew(lease, now=NOW + 12) is False

    with pytest.raises(OwnershipLost):
        repository.require_guard(lease, now=NOW + 12)


def test_processing_is_limited_to_four_acquired_attempts() -> None:
    repository = _repository()
    _create(repository)

    leases = []
    for attempt in range(1, MAX_PROCESS_ATTEMPTS + 1):
        lease = _acquire(
            repository,
            token=f"worker-{attempt}",
            now=NOW + attempt,
        )
        leases.append(lease)
        assert lease.attempt == attempt
        assert repository.release(lease, now=NOW + attempt) is True

    exhausted = repository.acquire(101, token="worker-five", now=NOW + 5)
    assert exhausted.status == "exhausted"
    assert exhausted.job is not None
    assert exhausted.job.attempts == MAX_PROCESS_ATTEMPTS
    assert [lease.fence for lease in leases] == [1, 2, 3, 4]


def test_initial_lease_is_capped_by_near_job_expiry() -> None:
    repository = _repository(retention=5, lease=10)
    _create(repository)

    lease = _acquire(repository, now=NOW)
    assert repository.ttl(lease_key(101), now=NOW) == 5
    assert repository.guard(lease, now=NOW + 4) is True
    assert repository.acquire(101, token="late", now=NOW + 5).status == "missing"


def test_concurrent_acquire_has_exactly_one_owner() -> None:
    repository = _repository()
    _create(repository)

    def acquire_once(index: int):
        return repository.acquire(101, token=f"worker-{index}", now=NOW)

    with ThreadPoolExecutor(max_workers=16) as executor:
        acquisitions = list(executor.map(acquire_once, range(32)))

    assert [result.status for result in acquisitions].count("acquired") == 1
    assert [result.status for result in acquisitions].count("busy") == 31
    winner = next(result.lease for result in acquisitions if result.lease is not None)
    assert repository.guard(winner, now=NOW) is True


def test_non_idempotent_send_intent_becomes_ambiguous_after_takeover() -> None:
    repository = _repository(lease=10)
    _create(repository)
    first = _acquire(repository, token="first", now=NOW)

    prepared = repository.prepare_intent(
        first,
        name="placeholder",
        kind="sendMessage",
        chunk_index=0,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
        now=NOW,
    )
    assert prepared.status == "prepared"

    second = _acquire(repository, token="second", now=NOW + 10)
    ambiguous = repository.prepare_intent(
        second,
        name="placeholder",
        kind="sendMessage",
        chunk_index=0,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
        now=NOW + 10,
    )
    assert ambiguous.status == "ambiguous"
    job = repository.get(101, now=NOW + 10)
    assert job is not None
    assert job.intent("placeholder") == {
        "chunk_index": 0,
        "fence": first.fence,
        "kind": "sendMessage",
        "payload_hash": "a" * 64,
    }


def test_idempotent_edit_intent_can_resume_across_fence_and_checkpoint() -> None:
    repository = _repository(lease=10)
    _create(repository)
    first = _acquire(repository, token="first", now=NOW)
    assert (
        repository.prepare_intent(
            first,
            name="answer:0",
            kind="editMessageText",
            chunk_index=0,
            payload_hash="b" * 64,
            ambiguous_on_takeover=False,
            now=NOW,
        ).status
        == "prepared"
    )

    second = _acquire(repository, token="second", now=NOW + 10)
    assert (
        repository.prepare_intent(
            second,
            name="answer:0",
            kind="editMessageText",
            chunk_index=0,
            payload_hash="b" * 64,
            ambiguous_on_takeover=False,
            now=NOW + 10,
        ).status
        == "prepared"
    )
    repository.checkpoint(
        second,
        name="answer:0",
        result={"message_id": 501, "not_modified": True},
        now=NOW + 10,
    )
    replay = repository.prepare_intent(
        second,
        name="answer:0",
        kind="editMessageText",
        chunk_index=0,
        payload_hash="b" * 64,
        ambiguous_on_takeover=False,
        now=NOW + 10,
    )
    assert replay.status == "checkpointed"
    assert replay.checkpoint == {"message_id": 501, "not_modified": True}


@pytest.mark.parametrize(
    ("kind", "chunk_index", "payload_hash"),
    [
        ("editMessageText", 0, "a" * 64),
        ("sendMessage", 1, "a" * 64),
        ("sendMessage", 0, "b" * 64),
    ],
)
def test_intent_identity_conflicts_are_rejected(
    kind: str, chunk_index: int, payload_hash: str
) -> None:
    repository = _repository()
    _create(repository)
    lease = _acquire(repository)
    assert (
        repository.prepare_intent(
            lease,
            name="chunk:1",
            kind="sendMessage",
            chunk_index=0,
            payload_hash="a" * 64,
            ambiguous_on_takeover=True,
            now=NOW,
        ).status
        == "prepared"
    )

    conflict = repository.prepare_intent(
        lease,
        name="chunk:1",
        kind=kind,
        chunk_index=chunk_index,
        payload_hash=payload_hash,
        ambiguous_on_takeover=True,
        now=NOW,
    )
    assert conflict.status == "conflict"


def test_checkpoint_is_idempotent_conflict_safe_and_blocks_intent_clear() -> None:
    repository = _repository()
    _create(repository)
    lease = _acquire(repository)

    with pytest.raises(JobIntegrityError, match="job_checkpoint_intent_missing"):
        repository.checkpoint(
            lease,
            name="placeholder",
            result={"message_id": 501},
            now=NOW,
        )

    repository.prepare_intent(
        lease,
        name="placeholder",
        kind="sendMessage",
        chunk_index=0,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
        now=NOW,
    )
    repository.clear_intent(lease, name="placeholder", now=NOW + 1)
    job = repository.get(101, now=NOW + 1)
    assert job is not None
    assert job.intent("placeholder") is None

    repository.prepare_intent(
        lease,
        name="placeholder",
        kind="sendMessage",
        chunk_index=0,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
        now=NOW + 1,
    )
    repository.checkpoint(
        lease,
        name="placeholder",
        result={"message_id": 501},
        now=NOW + 2,
    )
    repository.checkpoint(
        lease,
        name="placeholder",
        result={"message_id": 501},
        now=NOW + 3,
    )
    saved = repository.get(101, now=NOW + 3)
    assert saved is not None
    assert saved.placeholder_message_id == 501
    assert saved.checkpoint("placeholder") == {"message_id": 501}

    with pytest.raises(JobIntegrityError, match="job_checkpoint_conflict"):
        repository.checkpoint(
            lease,
            name="placeholder",
            result={"message_id": 999},
            now=NOW + 3,
        )
    with pytest.raises(JobIntegrityError, match="job_intent_checkpointed"):
        repository.clear_intent(lease, name="placeholder", now=NOW + 3)


def test_stale_owner_cannot_mutate_intents_checkpoints_answer_or_state() -> None:
    repository = _repository(lease=10)
    _create(repository)
    stale = _acquire(repository, token="stale", now=NOW)
    repository.prepare_intent(
        stale,
        name="placeholder",
        kind="sendMessage",
        chunk_index=0,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
        now=NOW,
    )
    _acquire(repository, token="current", now=NOW + 10)

    assert (
        repository.prepare_intent(
            stale,
            name="other",
            kind="sendMessage",
            chunk_index=1,
            payload_hash="b" * 64,
            ambiguous_on_takeover=True,
            now=NOW + 10,
        ).status
        == "ownership_lost"
    )
    with pytest.raises(OwnershipLost):
        repository.checkpoint(
            stale,
            name="placeholder",
            result={"message_id": 501},
            now=NOW + 10,
        )
    with pytest.raises(OwnershipLost):
        repository.clear_intent(stale, name="placeholder", now=NOW + 10)
    with pytest.raises(OwnershipLost):
        repository.save_answer(stale, "stale answer", now=NOW + 10)
    with pytest.raises(OwnershipLost):
        repository.finish(stale, "failed", now=NOW + 10)


def test_answer_is_saved_once_reused_and_moves_to_ready_to_deliver() -> None:
    repository = _repository()
    _create(repository)
    lease = _acquire(repository)

    assert repository.save_answer(lease, "first answer", now=NOW + 1) == (
        "first answer"
    )
    assert repository.save_answer(lease, "different answer", now=NOW + 2) == (
        "first answer"
    )
    job = repository.get(101, now=NOW + 2)
    assert job is not None
    assert job.state == "ready_to_deliver"
    assert job.answer_text == "first answer"
    assert job.raw["answer_sha256"] == hashlib.sha256(b"first answer").hexdigest()
    assert job.raw["answer_saved_at"] == str(NOW + 1)


def test_answer_validation() -> None:
    repository = _repository()
    _create(repository)
    lease = _acquire(repository)

    for invalid in ("", "x" * 64_001):
        with pytest.raises(ValueError, match="answer"):
            repository.save_answer(lease, invalid, now=NOW)


def test_concurrent_answer_saves_reuse_one_winner() -> None:
    repository = _repository()
    _create(repository)
    lease = _acquire(repository)

    def save(index: int) -> str:
        return repository.save_answer(lease, f"answer-{index}", now=NOW + 1)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(save, range(32)))

    assert len(set(results)) == 1
    job = repository.get(101, now=NOW + 1)
    assert job is not None
    assert job.answer_text == results[0]
    assert job.state == "ready_to_deliver"


@pytest.mark.parametrize(
    ("target", "error_class", "expected_fence"),
    [
        ("failed_retryable", "provider_timeout", 1),
        ("failed", "provider_auth", 2),
        ("failed_ambiguous", "telegram_send_ambiguous", 2),
        ("delivered", None, 2),
    ],
)
def test_finish_records_retryable_permanent_ambiguous_and_delivered_states(
    target: str, error_class: str | None, expected_fence: int
) -> None:
    repository = _repository()
    _create(repository)
    lease = _acquire(repository)
    if target == "delivered":
        repository.save_answer(lease, "answer", now=NOW + 1)

    repository.finish(
        lease,
        target,
        error_class=error_class,
        failure_notice=target == "failed",
        now=NOW + 2,
    )

    job = repository.get(101, now=NOW + 2)
    assert job is not None
    assert job.state == target
    assert job.error_class == error_class
    assert job.fence == expected_fence
    assert repository.guard(lease, now=NOW + 2) is False
    assert repository.ttl(lease_key(101), now=NOW + 2) == 0
    if target == "failed":
        assert job.failure_notice_state == "none"
        assert job.raw["failure_notice_hash"] == FAILURE_NOTICE_HASH
    if target == "failed_retryable":
        retry = _acquire(repository, token="retry", now=NOW + 2)
        assert retry.attempt == 2
        assert retry.fence == 2


def test_finish_validates_target_error_and_failure_notice_with_placeholder() -> None:
    repository = _repository()
    _failed_job_with_placeholder(repository)
    failed = repository.get(101, now=NOW + 2)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.failure_notice_state == "pending"
    assert failed.raw["failure_notice_hash"] == FAILURE_NOTICE_HASH

    other = _repository()
    _create(other)
    lease = _acquire(other)
    with pytest.raises(JobStoreError, match="job_state_rejected"):
        other.finish(lease, "cancelled", now=NOW)
    with pytest.raises(JobStoreError, match="job_state_rejected"):
        other.finish(lease, "delivered", now=NOW)
    for invalid in ("", "x" * 97):
        with pytest.raises(ValueError, match="error_class"):
            other.finish(lease, "failed", error_class=invalid, now=NOW)


def test_failure_takeover_handles_metadata_race_mismatch_busy_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(lease=10)
    monkeypatch.setattr(jobs_module.secrets, "randbelow", lambda _: 0)

    assert repository.failure_takeover(999, "unknown", now=NOW).status == "missing"
    _create(repository)
    pending = repository.failure_takeover(101, "qstash-101", now=NOW + 1)
    assert pending.status == "metadata_pending"
    assert pending.job is not None
    assert pending.job.state == "received"

    repository.record_publication(101, "qstash-101", now=NOW + 2)
    mismatch = repository.failure_takeover(101, "other", now=NOW + 2)
    assert mismatch.status == "mismatch"
    assert mismatch.job is not None
    assert mismatch.job.state == "enqueued"

    lease = _acquire(repository, now=NOW + 2)
    busy = repository.failure_takeover(101, "qstash-101", now=NOW + 2)
    assert busy.status == "busy"
    assert busy.retry_after == 11
    assert busy.job is None

    takeover = repository.failure_takeover(101, "qstash-101", now=NOW + 12)
    assert takeover.status == "failed"
    assert takeover.job is not None
    assert takeover.job.state == "failed"
    assert takeover.job.error_class == "qstash_retries_exhausted"
    assert takeover.job.fence == lease.fence + 1
    assert takeover.job.failure_notice_state == "none"
    assert repository.guard(lease, now=NOW + 12) is False

    terminal = repository.failure_takeover(101, "qstash-101", now=NOW + 12)
    assert terminal.status == "terminal"
    assert terminal.job is not None
    assert terminal.job.state == "failed"


def test_failure_takeover_requires_the_job_retry_policy():
    repository = _repository()
    _create(repository)
    repository.record_publication(101, "qstash-101", now=NOW)
    repository.backend._jobs["101"]["qstash_max_retries"] = "4"  # type: ignore[attr-defined]

    assert repository.failure_takeover(101, "qstash-101", now=NOW).status == "mismatch"
    assert (
        repository.failure_takeover(101, "qstash-101", max_retries=4, now=NOW).status
        == "failed"
    )


def test_concurrent_failure_takeover_has_one_fence_increment() -> None:
    repository = _repository()
    _create(repository)
    repository.record_publication(101, "qstash-101", now=NOW)

    def takeover(_: int):
        return repository.failure_takeover(101, "qstash-101", now=NOW + 1)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(takeover, range(32)))

    assert [result.status for result in results].count("failed") == 1
    assert [result.status for result in results].count("terminal") == 31
    job = repository.get(101, now=NOW + 1)
    assert job is not None
    assert job.fence == 1
    assert job.state == "failed"


def test_failure_notice_is_none_without_known_placeholder() -> None:
    repository = _repository()
    _create(repository)
    lease = _acquire(repository)
    repository.finish(
        lease,
        "failed",
        error_class="provider_permanent",
        failure_notice=True,
        now=NOW + 1,
    )

    job = repository.get(101, now=NOW + 1)
    assert job is not None
    assert job.failure_notice_state == "none"
    claim = repository.claim_failure_notice(101, token="notice", now=NOW + 1)
    assert claim.status == "none"
    assert claim.lease is None


def test_failure_notice_claim_guard_busy_release_retry_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(lease=10)
    _failed_job_with_placeholder(repository)
    monkeypatch.setattr(jobs_module.secrets, "randbelow", lambda _: 0)

    claimed = repository.claim_failure_notice(101, token="notice-one", now=NOW + 3)
    assert claimed.status == "claimed"
    assert claimed.lease is not None
    lease = claimed.lease
    assert lease.placeholder_message_id == 501
    assert lease.text == FAILURE_NOTICE_TEXT
    pending = repository.get(101, now=NOW + 3)
    assert pending is not None
    assert pending.raw["failure_notice_text"] == FAILURE_NOTICE_TEXT
    assert repository.guard_failure_notice(lease, now=NOW + 3) is True
    assert (
        repository.guard_failure_notice(replace(lease, token="wrong"), now=NOW + 3)
        is False
    )
    assert (
        repository.guard_failure_notice(
            replace(lease, fence=lease.fence + 1), now=NOW + 3
        )
        is False
    )

    busy = repository.claim_failure_notice(101, token="notice-two", now=NOW + 5)
    assert busy.status == "busy"
    assert busy.retry_after == 9
    assert (
        repository.release_failure_notice(replace(lease, token="wrong"), now=NOW + 5)
        is False
    )
    assert repository.release_failure_notice(lease, now=NOW + 5) is True
    assert repository.guard_failure_notice(lease, now=NOW + 5) is False

    retried = repository.claim_failure_notice(101, token="notice-three", now=NOW + 5)
    assert retried.status == "claimed"
    assert retried.lease is not None
    assert retried.lease.fence == lease.fence
    assert repository.guard_failure_notice(retried.lease, now=NOW + 14) is True
    assert repository.guard_failure_notice(retried.lease, now=NOW + 15) is False
    reclaimed = repository.claim_failure_notice(101, token="notice-four", now=NOW + 15)
    assert reclaimed.status == "claimed"


def test_failure_notice_claim_rejects_tampered_stored_text():
    repository = _repository()
    _failed_job_with_placeholder(repository)
    repository.backend._jobs["101"]["failure_notice_text"] = "tampered"  # type: ignore[attr-defined]

    with pytest.raises(JobIntegrityError, match="failure_notice_corrupt"):
        repository.claim_failure_notice(101, token="notice", now=NOW + 3)


def test_failure_notice_complete_is_checkpointed_and_cannot_be_reclaimed() -> None:
    repository = _repository()
    _failed_job_with_placeholder(repository)
    claim = repository.claim_failure_notice(101, token="notice", now=NOW + 3)
    assert claim.lease is not None

    repository.complete_failure_notice(
        claim.lease,
        {"message_id": 501, "edited": True},
        now=NOW + 4,
    )

    job = repository.get(101, now=NOW + 4)
    assert job is not None
    assert job.state == "failed"
    assert job.failure_notice_state == "delivered"
    assert job.checkpoint("failure_notice") == {
        "edited": True,
        "message_id": 501,
    }
    assert repository.ttl(failure_lease_key(101), now=NOW + 4) == 0
    assert repository.claim_failure_notice(101, now=NOW + 4).status == "delivered"
    with pytest.raises(OwnershipLost):
        repository.complete_failure_notice(
            claim.lease,
            {"message_id": 501},
            now=NOW + 4,
        )


def test_failure_notice_permanent_failure_is_terminal_for_notice_retries() -> None:
    repository = _repository()
    _failed_job_with_placeholder(repository)
    claim = repository.claim_failure_notice(101, token="notice", now=NOW + 3)
    assert claim.lease is not None

    repository.fail_failure_notice(
        claim.lease,
        "telegram_edit_permanent",
        now=NOW + 4,
    )

    job = repository.get(101, now=NOW + 4)
    assert job is not None
    assert job.failure_notice_state == "failed_permanent"
    assert job.raw["failure_notice_error"] == "telegram_edit_permanent"
    assert (
        repository.claim_failure_notice(101, token="again", now=NOW + 4).status
        == "failed_permanent"
    )
    assert repository.guard_failure_notice(claim.lease, now=NOW + 4) is False


def test_failure_notice_lease_is_capped_by_job_expiry_and_stale_completion_fails() -> (
    None
):
    repository = _repository(retention=10, lease=10)
    _failed_job_with_placeholder(repository)
    claim = repository.claim_failure_notice(101, token="notice", now=NOW + 8)
    assert claim.lease is not None

    assert repository.ttl(failure_lease_key(101), now=NOW + 8) == 2
    assert repository.guard_failure_notice(claim.lease, now=NOW + 9) is True
    assert repository.guard_failure_notice(claim.lease, now=NOW + 10) is False
    with pytest.raises(OwnershipLost):
        repository.complete_failure_notice(
            claim.lease,
            {"message_id": 501},
            now=NOW + 10,
        )


def test_concurrent_failure_notice_claim_has_exactly_one_owner() -> None:
    repository = _repository()
    _failed_job_with_placeholder(repository)

    def claim(index: int):
        return repository.claim_failure_notice(
            101, token=f"notice-{index}", now=NOW + 3
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        claims = list(executor.map(claim, range(32)))

    assert [result.status for result in claims].count("claimed") == 1
    assert [result.status for result in claims].count("busy") == 31
    winner = next(result.lease for result in claims if result.lease is not None)
    assert repository.guard_failure_notice(winner, now=NOW + 3) is True


def test_job_derived_fields_and_indexes_share_one_absolute_expiry() -> None:
    repository = _repository(retention=20, lease=10)
    _create(repository)
    repository.record_publication(101, "qstash-101", now=NOW + 2)
    lease = _acquire(repository, now=NOW + 5)
    repository.prepare_intent(
        lease,
        name="placeholder",
        kind="sendMessage",
        chunk_index=0,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
        now=NOW + 6,
    )
    repository.checkpoint(
        lease,
        name="placeholder",
        result={"message_id": 501},
        now=NOW + 6,
    )
    repository.save_answer(lease, "durable answer", now=NOW + 7)

    assert repository.ttl(job_key(101), now=NOW + 10) == 10
    assert repository.ttl(chat_index_key(-100), now=NOW + 10) == 10
    assert repository.ttl(user_index_key(7), now=NOW + 10) == 10
    assert repository.ttl(lease_key(101), now=NOW + 10) == 5
    raw = repository.backend.get("101", now=NOW + 10)
    assert raw is not None
    assert {
        "answer_text",
        "answer_sha256",
        "intent:placeholder",
        "checkpoint:placeholder",
    }.issubset(raw)

    assert repository.get(101, now=NOW + 20) is None
    assert repository.ttl(job_key(101), now=NOW + 20) == 0
    assert repository.ttl(lease_key(101), now=NOW + 20) == 0
    assert repository.ttl(chat_index_key(-100), now=NOW + 20) == 0
    assert repository.ttl(user_index_key(7), now=NOW + 20) == 0
    assert repository.index_job_ids(chat_index_key(-100), now=NOW + 20) == []


def test_index_reads_prune_expired_members_without_removing_live_members() -> None:
    repository = _repository(retention=10)
    _create(repository, 101, now=NOW, user_ids=(8,))
    _create(repository, 102, now=NOW + 5, user_ids=(8,))

    assert repository.index_job_ids(chat_index_key(-100), now=NOW + 9) == [
        "101",
        "102",
    ]
    assert repository.index_job_ids(chat_index_key(-100), now=NOW + 10) == ["102"]
    assert repository.index_job_ids(user_index_key(8), now=NOW + 10) == ["102"]
    assert repository.ttl(chat_index_key(-100), now=NOW + 10) == 5
    assert repository.index_job_ids(chat_index_key(-100), now=NOW + 15) == []
    assert repository.ttl(chat_index_key(-100), now=NOW + 15) == 0


def test_index_order_is_expiry_then_job_id() -> None:
    repository = _repository()
    _create(repository, 103, now=NOW)
    _create(repository, 101, now=NOW)
    _create(repository, 102, now=NOW + 1)

    assert repository.index_job_ids(chat_index_key(-100), now=NOW) == [
        "101",
        "103",
        "102",
    ]


def test_concurrent_index_updates_do_not_lose_members() -> None:
    repository = _repository()
    job_ids = list(range(200, 264))

    def create(job_id: int) -> None:
        _create(repository, job_id, now=NOW, user_ids=(8,))

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(create, job_ids))

    expected = sorted(str(job_id) for job_id in job_ids)
    assert repository.index_job_ids(chat_index_key(-100), now=NOW) == expected
    assert repository.index_job_ids(user_index_key(8), now=NOW) == expected
    assert repository.index_job_ids(user_index_key(7), now=NOW) == expected
