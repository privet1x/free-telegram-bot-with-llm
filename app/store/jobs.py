"""Typed repository for durable, retry-safe Telegram/QStash jobs."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.settings import Settings, settings
from app.store.job_backend import JobBackend, MemoryJobBackend, utc_now
from app.store.redis import get_store

JobState = Literal[
    "received",
    "enqueued",
    "processing",
    "ready_to_deliver",
    "delivered",
    "failed_retryable",
    "failed",
    "failed_ambiguous",
    "cancelled",
]

TERMINAL_STATES = frozenset({"delivered", "failed", "failed_ambiguous", "cancelled"})
SAFE_ENQUEUE_STATES = frozenset(
    {
        "enqueued",
        "processing",
        "ready_to_deliver",
        "delivered",
        "failed_retryable",
        "failed",
        "failed_ambiguous",
        "cancelled",
    }
)

_MAX_JOB_ID_CHARS = 32
_MAX_MESSAGE_ID_CHARS = 512
_MAX_INTENT_NAME_CHARS = 96
_MAX_ERROR_CLASS_CHARS = 96
_MAX_ANSWER_CHARS = 64_000
FAILURE_NOTICE_TEXT = "Sorry, I could not complete that reply. Please try again later."
FAILURE_NOTICE_HASH = hashlib.sha256(FAILURE_NOTICE_TEXT.encode()).hexdigest()


class JobStoreError(RuntimeError):
    """Stable repository error that never includes private job data."""

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(error_class)


class JobIntegrityError(JobStoreError):
    pass


class OwnershipLost(JobStoreError):
    def __init__(self) -> None:
        super().__init__("job_ownership_lost")


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    state: JobState
    request: dict[str, object]
    effective_policy: dict[str, object]
    created_at: int
    updated_at: int
    expires_at: int
    attempts: int
    fence: int
    qstash_message_id: str | None
    placeholder_message_id: int | None
    answer_text: str | None
    error_class: str | None
    failure_notice_state: str | None
    raw: Mapping[str, str] = field(repr=False)

    def checkpoint(self, name: str) -> dict[str, object] | None:
        return _decode_object(self.raw.get(f"checkpoint:{name}"))

    def intent(self, name: str) -> dict[str, object] | None:
        return _decode_object(self.raw.get(f"intent:{name}"))


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    token: str
    fence: int
    attempt: int


@dataclass(frozen=True, slots=True)
class LeaseAcquisition:
    status: str
    lease: JobLease | None = None
    job: JobRecord | None = None
    retry_after: int | None = None


@dataclass(frozen=True, slots=True)
class IntentResult:
    status: str
    checkpoint: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class FailureTakeover:
    status: str
    job: JobRecord | None = None
    retry_after: int | None = None


@dataclass(frozen=True, slots=True)
class FailureNoticeLease:
    job_id: str
    token: str
    fence: int
    placeholder_message_id: int
    text: str


@dataclass(frozen=True, slots=True)
class FailureNoticeClaim:
    status: str
    lease: FailureNoticeLease | None = None
    retry_after: int | None = None


@dataclass(frozen=True, slots=True)
class PurgeResult:
    job_count: int
    outbound_message_ids: set[int]


def job_key(job_id: str | int) -> str:
    return f"job:{_job_id(job_id)}"


def lease_key(job_id: str | int) -> str:
    return f"{job_key(job_id)}:lease"


def privacy_receipt_key(index_key: str) -> str:
    digest = hashlib.sha256(index_key.encode("utf-8")).hexdigest()
    return f"privacy:receipt:{digest}"


def _checkpoint_message_ids(raw: Mapping[str, str]) -> set[int]:
    result: set[int] = set()
    for name, value in raw.items():
        if not name.startswith("checkpoint:"):
            continue
        checkpoint = _decode_object(value)
        message_id = checkpoint.get("message_id") if checkpoint else None
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            result.add(message_id)
    return result


def failure_lease_key(job_id: str | int) -> str:
    return f"{job_key(job_id)}:failure-lease"


def chat_index_key(chat_id: int) -> str:
    return f"jobs:chat:{chat_id}"


def user_index_key(user_id: int) -> str:
    return f"jobs:user:{user_id}"


def _job_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise ValueError("job_id must be a decimal Telegram update ID")
    result = str(value)
    if (
        not result
        or len(result) > _MAX_JOB_ID_CHARS
        or not result.isascii()
        or not result.isdecimal()
    ):
        raise ValueError("job_id must be a decimal Telegram update ID")
    return result


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _raw_int(raw: Mapping[str, str], name: str, default: int = 0) -> int:
    try:
        return int(raw.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_object(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record(job_id: str, raw: Mapping[str, str] | None) -> JobRecord | None:
    if raw is None:
        return None
    request = _decode_object(raw.get("request_json"))
    policy = _decode_object(raw.get("effective_policy_json"))
    state = raw.get("state")
    if (
        request is None
        or policy is None
        or state
        not in {
            "received",
            "enqueued",
            "processing",
            "ready_to_deliver",
            "delivered",
            "failed_retryable",
            "failed",
            "failed_ambiguous",
            "cancelled",
        }
    ):
        raise JobIntegrityError("job_corrupt")
    placeholder = _raw_int(raw, "placeholder_message_id")
    return JobRecord(
        job_id=job_id,
        state=state,
        request=request,
        effective_policy=policy,
        created_at=_raw_int(raw, "created_at"),
        updated_at=_raw_int(raw, "updated_at"),
        expires_at=_raw_int(raw, "expires_at"),
        attempts=_raw_int(raw, "attempts"),
        fence=_raw_int(raw, "fence"),
        qstash_message_id=raw.get("qstash_message_id"),
        placeholder_message_id=placeholder if placeholder > 0 else None,
        answer_text=raw.get("answer_text"),
        error_class=raw.get("error_class"),
        failure_notice_state=raw.get("failure_notice_state"),
        raw=dict(raw),
    )


def _retry_after(ttl: object) -> int:
    remaining = ttl if isinstance(ttl, int) and not isinstance(ttl, bool) else 1
    return max(remaining, 1) + secrets.randbelow(5) + 1


class JobRepository:
    """Domain validation around an atomic job backend."""

    def __init__(self, backend: JobBackend, config: Settings = settings) -> None:
        self._backend = backend
        self._config = config

    @property
    def backend(self) -> JobBackend:
        return self._backend

    def create_reply_job(
        self,
        request: Mapping[str, object],
        effective_policy: Mapping[str, object],
        user_ids: Sequence[int],
        *,
        now: int | None = None,
        auto_cooldown_owner: str | None = None,
        auto_cooldown_seconds: int | None = None,
    ) -> JobRecord:
        current_time = utc_now() if now is None else now
        update_id = _strict_int(request.get("update_id"), "update_id")
        chat_id = _strict_int(request.get("chat_id"), "chat_id")
        author = request.get("author")
        author_id = (
            author.get("id")
            if isinstance(author, Mapping)
            else request.get("author_id")
        )
        normalized_author_id = _strict_int(author_id, "author_id")
        normalized_id = _job_id(update_id)
        request_json = _json(dict(request))
        policy_json = _json(dict(effective_policy))
        expires_at = current_time + self._config.JOB_RETENTION_SECONDS
        fields = {
            "job_id": normalized_id,
            "state": "received",
            "request_json": request_json,
            "request_sha256": _sha256_text(request_json),
            "effective_policy_json": policy_json,
            "created_at": str(current_time),
            "updated_at": str(current_time),
            "expires_at": str(expires_at),
            "chat_id": str(chat_id),
            "author_id": str(normalized_author_id),
            "attempts": "0",
            "fence": "0",
            "qstash_max_retries": "3",
        }
        normalized_users: set[int] = set()
        for user_id in user_ids:
            normalized = _strict_int(user_id, "user_id")
            if normalized > 0:
                normalized_users.add(normalized)
        normalized_users.add(normalized_author_id)
        indexes = [chat_index_key(chat_id)] + [
            user_index_key(user_id) for user_id in sorted(normalized_users)
        ]
        fields["index_keys_json"] = _json(sorted(indexes))
        if auto_cooldown_owner is not None:
            if auto_cooldown_seconds is None or auto_cooldown_seconds <= 0:
                raise ValueError("auto cooldown is invalid")
            result = self._backend.create_auto(
                job_id=normalized_id,
                fields=fields,
                index_keys=indexes,
                cooldown_key=f"cooldown:auto:{chat_id}",
                cooldown_owner=auto_cooldown_owner,
                cooldown_seconds=auto_cooldown_seconds,
                now=current_time,
            )
            if result == "suppressed":
                raise JobStoreError("auto_cooldown")
        else:
            result = self._backend.create(
                job_id=normalized_id,
                fields=fields,
                index_keys=indexes,
                now=current_time,
            )
        if result not in {"created", "existing"}:
            raise JobStoreError("job_create_failed")
        job = self.get(normalized_id, now=current_time)
        if job is None:
            raise JobStoreError("job_create_failed")
        return job

    def get(self, job_id: str | int, *, now: int | None = None) -> JobRecord | None:
        normalized_id = _job_id(job_id)
        current_time = utc_now() if now is None else now
        return _record(
            normalized_id, self._backend.get(normalized_id, now=current_time)
        )

    def record_publication(
        self, job_id: str | int, message_id: str, *, now: int | None = None
    ) -> JobRecord:
        normalized_id = _job_id(job_id)
        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > _MAX_MESSAGE_ID_CHARS
            or any(ord(character) < 0x20 for character in message_id)
        ):
            raise ValueError("message_id is invalid")
        current_time = utc_now() if now is None else now
        result = self._backend.record_publication(
            normalized_id, message_id, now=current_time
        )
        if result == "conflict":
            raise JobIntegrityError("qstash_message_id_conflict")
        if result != "recorded":
            raise JobStoreError("job_missing")
        job = self.get(normalized_id, now=current_time)
        if job is None:
            raise JobStoreError("job_missing")
        return job

    def acquire(
        self,
        job_id: str | int,
        *,
        token: str | None = None,
        now: int | None = None,
    ) -> LeaseAcquisition:
        normalized_id = _job_id(job_id)
        lease_token = secrets.token_urlsafe(24) if token is None else token
        if not lease_token or len(lease_token) > 128:
            raise ValueError("lease token is invalid")
        current_time = utc_now() if now is None else now
        result = self._backend.acquire(
            normalized_id,
            token=lease_token,
            lease_seconds=self._config.JOB_LEASE_SECONDS,
            now=current_time,
        )
        status = str(result.get("status"))
        if status == "acquired":
            fence = result.get("fence")
            attempt = result.get("attempts")
            if not isinstance(fence, int) or not isinstance(attempt, int):
                raise JobIntegrityError("job_lease_corrupt")
            job = self.get(normalized_id, now=current_time)
            if job is None:
                raise JobStoreError("job_missing")
            return LeaseAcquisition(
                status=status,
                lease=JobLease(normalized_id, lease_token, fence, attempt),
                job=job,
            )
        if status == "busy":
            return LeaseAcquisition(
                status=status, retry_after=_retry_after(result.get("ttl"))
            )
        return LeaseAcquisition(
            status=status, job=self.get(normalized_id, now=current_time)
        )

    def renew(self, lease: JobLease, *, now: int | None = None) -> bool:
        return self._backend.renew(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            lease_seconds=self._config.JOB_LEASE_SECONDS,
            now=utc_now() if now is None else now,
        )

    def guard(self, lease: JobLease, *, now: int | None = None) -> bool:
        return self._backend.guard(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            now=utc_now() if now is None else now,
        )

    def require_guard(self, lease: JobLease, *, now: int | None = None) -> None:
        if not self.guard(lease, now=now):
            raise OwnershipLost()

    def prepare_intent(
        self,
        lease: JobLease,
        *,
        name: str,
        kind: str,
        chunk_index: int,
        payload_hash: str,
        ambiguous_on_takeover: bool,
        now: int | None = None,
    ) -> IntentResult:
        _validate_intent_name(name)
        if kind not in {"sendMessage", "editMessageText", "judgeStage"}:
            raise ValueError("intent kind is invalid")
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            raise ValueError("chunk_index must be an integer")
        if len(payload_hash) != 64:
            raise ValueError("payload_hash must be a SHA-256 hex digest")
        intent_json = _json(
            {
                "kind": kind,
                "chunk_index": chunk_index,
                "payload_hash": payload_hash,
                "fence": lease.fence,
            }
        )
        result = self._backend.prepare_intent(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            name=name,
            intent_json=intent_json,
            ambiguous_on_takeover=ambiguous_on_takeover,
            now=utc_now() if now is None else now,
        )
        checkpoint = result.get("checkpoint")
        return IntentResult(
            status=str(result.get("status")),
            checkpoint=_decode_object(checkpoint)
            if isinstance(checkpoint, str)
            else None,
        )

    def checkpoint(
        self,
        lease: JobLease,
        *,
        name: str,
        result: Mapping[str, object],
        now: int | None = None,
    ) -> None:
        _validate_intent_name(name)
        status = self._backend.checkpoint(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            name=name,
            checkpoint_json=_json(dict(result)),
            now=utc_now() if now is None else now,
        )
        if status == "ownership_lost":
            raise OwnershipLost()
        if status != "checkpointed":
            raise JobIntegrityError(f"job_checkpoint_{status}")

    def clear_intent(
        self, lease: JobLease, *, name: str, now: int | None = None
    ) -> None:
        _validate_intent_name(name)
        status = self._backend.clear_intent(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            name=name,
            now=utc_now() if now is None else now,
        )
        if status == "ownership_lost":
            raise OwnershipLost()
        if status != "cleared":
            raise JobIntegrityError(f"job_intent_{status}")

    def save_answer(
        self, lease: JobLease, answer: str, *, now: int | None = None
    ) -> str:
        if not isinstance(answer, str) or not answer or len(answer) > _MAX_ANSWER_CHARS:
            raise ValueError("answer is invalid")
        result = self._backend.save_answer(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            answer=answer,
            answer_hash=_sha256_text(answer),
            now=utc_now() if now is None else now,
        )
        status = result.get("status")
        if status == "ownership_lost":
            raise OwnershipLost()
        if status not in {"saved", "existing"}:
            raise JobStoreError("job_state_rejected")
        stored = result.get("answer")
        if not isinstance(stored, str):
            raise JobIntegrityError("job_answer_corrupt")
        return stored

    def finish(
        self,
        lease: JobLease,
        target_state: str,
        *,
        error_class: str | None = None,
        failure_notice: bool = False,
        now: int | None = None,
    ) -> None:
        if error_class is not None and (
            not error_class or len(error_class) > _MAX_ERROR_CLASS_CHARS
        ):
            raise ValueError("error_class is invalid")
        status = self._backend.finish_owned(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            target_state=target_state,
            error_class=error_class,
            failure_notice_hash=FAILURE_NOTICE_HASH if failure_notice else None,
            failure_notice_text=FAILURE_NOTICE_TEXT if failure_notice else None,
            now=utc_now() if now is None else now,
        )
        if status == "ownership_lost":
            raise OwnershipLost()
        if status != "finished":
            raise JobStoreError("job_state_rejected")

    def release(self, lease: JobLease, *, now: int | None = None) -> bool:
        return self._backend.release(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            now=utc_now() if now is None else now,
        )

    def failure_takeover(
        self,
        job_id: str | int,
        source_message_id: str,
        *,
        max_retries: int = 3,
        now: int | None = None,
    ) -> FailureTakeover:
        normalized_id = _job_id(job_id)
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise ValueError("max_retries is invalid")
        current_time = utc_now() if now is None else now
        result = self._backend.failure_takeover(
            normalized_id,
            source_message_id=source_message_id,
            failure_notice_hash=FAILURE_NOTICE_HASH,
            failure_notice_text=FAILURE_NOTICE_TEXT,
            max_retries=max_retries,
            now=current_time,
        )
        status = str(result.get("status"))
        if status == "busy":
            return FailureTakeover(
                status=status, retry_after=_retry_after(result.get("ttl"))
            )
        return FailureTakeover(
            status=status, job=self.get(normalized_id, now=current_time)
        )

    def claim_failure_notice(
        self,
        job_id: str | int,
        *,
        token: str | None = None,
        now: int | None = None,
    ) -> FailureNoticeClaim:
        normalized_id = _job_id(job_id)
        claim_token = secrets.token_urlsafe(24) if token is None else token
        if not claim_token or len(claim_token) > 128:
            raise ValueError("failure notice token is invalid")
        current_time = utc_now() if now is None else now
        result = self._backend.claim_failure_notice(
            normalized_id,
            token=claim_token,
            lease_seconds=min(30, self._config.JOB_LEASE_SECONDS),
            now=current_time,
        )
        status = str(result.get("status"))
        if status == "busy":
            return FailureNoticeClaim(
                status=status, retry_after=_retry_after(result.get("ttl"))
            )
        if status == "claimed":
            fence = result.get("fence")
            placeholder = result.get("placeholder_message_id")
            notice_text = result.get("failure_notice_text")
            notice_hash = result.get("failure_notice_hash")
            if (
                not isinstance(fence, int)
                or not isinstance(placeholder, int)
                or not isinstance(notice_text, str)
                or not notice_text
                or len(notice_text) > 4_000
                or not isinstance(notice_hash, str)
                or len(notice_hash) != 64
                or any(
                    character not in "0123456789abcdef" for character in notice_hash
                )
                or hashlib.sha256(notice_text.encode("utf-8")).hexdigest()
                != notice_hash
            ):
                raise JobIntegrityError("failure_notice_corrupt")
            return FailureNoticeClaim(
                status=status,
                lease=FailureNoticeLease(
                    normalized_id, claim_token, fence, placeholder, notice_text
                ),
            )
        return FailureNoticeClaim(status=status)

    def guard_failure_notice(
        self, lease: FailureNoticeLease, *, now: int | None = None
    ) -> bool:
        return self._backend.guard_failure_notice(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            now=utc_now() if now is None else now,
        )

    def complete_failure_notice(
        self,
        lease: FailureNoticeLease,
        checkpoint: Mapping[str, object],
        *,
        now: int | None = None,
    ) -> None:
        result = self._backend.complete_failure_notice(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            checkpoint_json=_json(dict(checkpoint)),
            now=utc_now() if now is None else now,
        )
        if result != "completed":
            raise OwnershipLost()

    def fail_failure_notice(
        self,
        lease: FailureNoticeLease,
        error_class: str,
        *,
        now: int | None = None,
    ) -> None:
        result = self._backend.fail_failure_notice(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            error_class=error_class,
            now=utc_now() if now is None else now,
        )
        if result != "completed":
            raise OwnershipLost()

    def release_failure_notice(
        self, lease: FailureNoticeLease, *, now: int | None = None
    ) -> bool:
        return self._backend.release_failure_notice(
            lease.job_id,
            token=lease.token,
            fence=lease.fence,
            now=utc_now() if now is None else now,
        )

    def index_job_ids(self, index_key: str, *, now: int | None = None) -> list[str]:
        return self._backend.index_members(
            index_key, now=utc_now() if now is None else now
        )

    def purge_index(
        self, index_key: str, *, now: int | None = None
    ) -> PurgeResult:
        """Erase every indexed private job and report delivery artifacts."""
        current_time = utc_now() if now is None else now
        store = get_store()
        receipt_key = privacy_receipt_key(index_key)
        outbound_ids = {
            int(value)
            for value in store.smembers(receipt_key)
            if value.isdecimal() and int(value) > 0
        }
        purged_jobs = 0
        for job_id in self.index_job_ids(index_key, now=current_time):
            job = self.get(job_id, now=current_time)
            if job is None:
                continue
            raw_indexes = job.raw.get("index_keys_json")
            try:
                decoded_indexes = json.loads(raw_indexes) if raw_indexes else []
            except (TypeError, ValueError):
                decoded_indexes = []
            indexes = {
                value for value in decoded_indexes if isinstance(value, str) and value
            }
            if not indexes:
                chat_id = job.request.get("chat_id")
                if isinstance(chat_id, int) and not isinstance(chat_id, bool):
                    indexes.add(chat_index_key(chat_id))
                indexes.update(
                    user_index_key(user_id)
                    for user_id in _request_user_ids(job.request)
                )
            known_ids = _checkpoint_message_ids(job.raw)
            if known_ids:
                stored = store.set_add_expiring(
                    receipt_key,
                    {str(value) for value in known_ids},
                    self._config.JOB_RETENTION_SECONDS,
                )
                outbound_ids.update(
                    int(value) for value in stored if value.isdecimal()
                )
            store.set(
                f"privacy:job:{job_id}",
                "purged",
                ex=max(job.expires_at - current_time, 1),
            )
            snapshot = self._backend.purge(
                job_id,
                index_keys=sorted(indexes),
                receipt_key=receipt_key,
                receipt_ttl=self._config.JOB_RETENTION_SECONDS,
                now=current_time,
            )
            if snapshot is not None:
                purged_jobs += 1
                snapshot_ids = _checkpoint_message_ids(snapshot)
                outbound_ids.update(snapshot_ids)
        return PurgeResult(purged_jobs, outbound_ids)

    def ttl(self, key: str, *, now: int | None = None) -> int:
        return self._backend.ttl(key, now=utc_now() if now is None else now)


def _validate_intent_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > _MAX_INTENT_NAME_CHARS
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789:_-"
            for character in name
        )
    ):
        raise ValueError("intent name is invalid")


def _request_user_ids(value: object) -> set[int]:
    result: set[int] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"user_id", "id"} and isinstance(item, int) and not isinstance(item, bool) and item > 0:
                result.add(item)
            else:
                result.update(_request_user_ids(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            result.update(_request_user_ids(item))
    return result


_repository: JobRepository | None = None
_repository_lock = threading.Lock()


def _build_repository() -> JobRepository:
    if settings.UPSTASH_REDIS_REST_URL and settings.UPSTASH_REDIS_REST_TOKEN:
        from app.store.job_backend_upstash import UpstashJobBackend

        backend: JobBackend = UpstashJobBackend(
            settings.UPSTASH_REDIS_REST_URL,
            settings.UPSTASH_REDIS_REST_TOKEN,
        )
    else:
        backend = MemoryJobBackend()
    return JobRepository(backend, settings)


def get_job_repository() -> JobRepository:
    global _repository
    if _repository is None:
        with _repository_lock:
            if _repository is None:
                _repository = _build_repository()
    return _repository


def reset_job_repository() -> None:
    global _repository
    with _repository_lock:
        _repository = None
