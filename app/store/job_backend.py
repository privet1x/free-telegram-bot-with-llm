"""Atomic storage primitives for durable Telegram jobs.

The in-memory backend mirrors the production Redis/Lua contract and is used by
tests and local development. Values intentionally use Redis-like string hash
fields so both backends exercise the same serialization boundary.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Protocol

TERMINAL_STATES = frozenset({"delivered", "failed", "failed_ambiguous", "cancelled"})
PROCESSABLE_STATES = frozenset(
    {"received", "enqueued", "failed_retryable", "processing", "ready_to_deliver"}
)
MAX_PROCESS_ATTEMPTS = 4


class JobBackend(Protocol):
    """Atomic operations required by the job repository."""

    def create(
        self,
        *,
        job_id: str,
        fields: Mapping[str, str],
        index_keys: Sequence[str],
        now: int,
    ) -> str: ...

    def create_auto(
        self,
        *,
        job_id: str,
        fields: Mapping[str, str],
        index_keys: Sequence[str],
        cooldown_key: str,
        cooldown_owner: str,
        cooldown_seconds: int,
        now: int,
    ) -> str: ...

    def get(self, job_id: str, *, now: int) -> dict[str, str] | None: ...

    def record_publication(self, job_id: str, message_id: str, *, now: int) -> str: ...

    def acquire(
        self,
        job_id: str,
        *,
        token: str,
        lease_seconds: int,
        now: int,
    ) -> dict[str, object]: ...

    def lease_ttl(self, job_id: str, *, now: int) -> int: ...

    def renew(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        lease_seconds: int,
        now: int,
    ) -> bool: ...

    def guard(self, job_id: str, *, token: str, fence: int, now: int) -> bool: ...

    def prepare_intent(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        intent_json: str,
        ambiguous_on_takeover: bool,
        now: int,
    ) -> dict[str, object]: ...

    def checkpoint(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        checkpoint_json: str,
        now: int,
    ) -> str: ...

    def clear_intent(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        now: int,
    ) -> str: ...

    def save_answer(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        answer: str,
        answer_hash: str,
        now: int,
    ) -> dict[str, object]: ...

    def finish_owned(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        target_state: str,
        error_class: str | None,
        failure_notice_hash: str | None,
        failure_notice_text: str | None,
        now: int,
    ) -> str: ...

    def release(self, job_id: str, *, token: str, fence: int, now: int) -> bool: ...

    def failure_takeover(
        self,
        job_id: str,
        *,
        source_message_id: str,
        failure_notice_hash: str,
        failure_notice_text: str,
        max_retries: int,
        now: int,
    ) -> dict[str, object]: ...

    def claim_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        lease_seconds: int,
        now: int,
    ) -> dict[str, object]: ...

    def guard_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        now: int,
    ) -> bool: ...

    def complete_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        checkpoint_json: str,
        now: int,
    ) -> str: ...

    def fail_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        error_class: str,
        now: int,
    ) -> str: ...

    def release_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        now: int,
    ) -> bool: ...

    def index_members(self, index_key: str, *, now: int) -> list[str]: ...

    def ttl(self, key: str, *, now: int) -> int: ...


def _integer(fields: Mapping[str, str], name: str, default: int = 0) -> int:
    try:
        return int(fields.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _lease_value(token: str, fence: int) -> str:
    return f"{token}:{fence}"


def _json_object(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class MemoryJobBackend:
    """Thread-safe atomic backend with Redis-equivalent absolute expiries."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, str]] = {}
        self._job_expiry: dict[str, int] = {}
        self._leases: dict[str, tuple[str, int]] = {}
        self._notice_leases: dict[str, tuple[str, int]] = {}
        self._indexes: dict[str, dict[str, int]] = {}
        self._index_expiry: dict[str, int] = {}
        self._lock = threading.RLock()
        self._cooldowns: dict[str, tuple[str, int]] = {}

    def _purge_job(self, job_id: str, now: int) -> None:
        expires_at = self._job_expiry.get(job_id)
        if expires_at is not None and expires_at <= now:
            self._jobs.pop(job_id, None)
            self._job_expiry.pop(job_id, None)
            self._leases.pop(job_id, None)
            self._notice_leases.pop(job_id, None)
        for leases in (self._leases, self._notice_leases):
            lease = leases.get(job_id)
            if lease is not None and lease[1] <= now:
                leases.pop(job_id, None)

    def _purge_index(self, key: str, now: int) -> None:
        expires_at = self._index_expiry.get(key)
        if expires_at is not None and expires_at <= now:
            self._indexes.pop(key, None)
            self._index_expiry.pop(key, None)
            return
        values = self._indexes.get(key)
        if values is None:
            return
        for job_id, score in list(values.items()):
            if score <= now:
                values.pop(job_id, None)
        if not values:
            self._indexes.pop(key, None)
            self._index_expiry.pop(key, None)

    def _job(self, job_id: str, now: int) -> dict[str, str] | None:
        self._purge_job(job_id, now)
        return self._jobs.get(job_id)

    def _owns(
        self, job_id: str, token: str, fence: int, now: int
    ) -> tuple[bool, dict[str, str] | None]:
        job = self._job(job_id, now)
        lease = self._leases.get(job_id)
        owns = bool(
            job is not None
            and job.get("state") not in TERMINAL_STATES
            and _integer(job, "fence") == fence
            and lease is not None
            and lease[0] == _lease_value(token, fence)
            and lease[1] > now
        )
        return owns, job

    def create(
        self,
        *,
        job_id: str,
        fields: Mapping[str, str],
        index_keys: Sequence[str],
        now: int,
    ) -> str:
        with self._lock:
            existing = self._job(job_id, now)
            if existing is not None:
                return "existing"
            expires_at = _integer(fields, "expires_at")
            if expires_at <= now:
                return "expired"
            self._jobs[job_id] = dict(fields)
            self._job_expiry[job_id] = expires_at
            for key in sorted(set(index_keys)):
                self._purge_index(key, now)
                self._indexes.setdefault(key, {})[job_id] = expires_at
                self._index_expiry[key] = max(
                    expires_at, self._index_expiry.get(key, expires_at)
                )
            return "created"

    def create_auto(
        self,
        *,
        job_id: str,
        fields: Mapping[str, str],
        index_keys: Sequence[str],
        cooldown_key: str,
        cooldown_owner: str,
        cooldown_seconds: int,
        now: int,
    ) -> str:
        with self._lock:
            existing = self._job(job_id, now)
            if existing is not None:
                return "existing"
            current = self._cooldowns.get(cooldown_key)
            if current is not None and current[1] > now:
                return "suppressed"
            status = self.create(
                job_id=job_id, fields=fields, index_keys=index_keys, now=now
            )
            if status == "created":
                self._cooldowns[cooldown_key] = (
                    cooldown_owner,
                    now + cooldown_seconds,
                )
            return status

    def get(self, job_id: str, *, now: int) -> dict[str, str] | None:
        with self._lock:
            job = self._job(job_id, now)
            return copy.deepcopy(job) if job is not None else None

    def record_publication(self, job_id: str, message_id: str, *, now: int) -> str:
        with self._lock:
            job = self._job(job_id, now)
            if job is None:
                return "missing"
            current = job.get("qstash_message_id")
            if current is not None and current != message_id:
                return "conflict"
            job["qstash_message_id"] = message_id
            job["enqueued_at"] = job.get("enqueued_at", str(now))
            job["updated_at"] = str(now)
            if job.get("state") == "received":
                job["state"] = "enqueued"
            return "recorded"

    def acquire(
        self,
        job_id: str,
        *,
        token: str,
        lease_seconds: int,
        now: int,
    ) -> dict[str, object]:
        with self._lock:
            job = self._job(job_id, now)
            if job is None:
                return {"status": "missing"}
            state = job.get("state", "")
            if state in TERMINAL_STATES:
                return {"status": "terminal", "state": state}
            lease = self._leases.get(job_id)
            if lease is not None and lease[1] > now:
                return {"status": "busy", "ttl": lease[1] - now}
            if state not in PROCESSABLE_STATES:
                return {"status": "invalid_state", "state": state}
            attempts = _integer(job, "attempts")
            if attempts >= MAX_PROCESS_ATTEMPTS:
                return {"status": "exhausted", "attempts": attempts}
            expires_at = _integer(job, "expires_at")
            ttl = min(lease_seconds, expires_at - now)
            if ttl <= 0:
                self._purge_job(job_id, now)
                return {"status": "missing"}
            fence = _integer(job, "fence") + 1
            attempts += 1
            job["fence"] = str(fence)
            job["attempts"] = str(attempts)
            job["updated_at"] = str(now)
            if state in {"received", "enqueued", "failed_retryable"}:
                state = "processing"
                job["state"] = state
            self._leases[job_id] = (_lease_value(token, fence), now + ttl)
            return {
                "status": "acquired",
                "fence": fence,
                "attempts": attempts,
                "state": state,
                "ttl": ttl,
            }

    def lease_ttl(self, job_id: str, *, now: int) -> int:
        with self._lock:
            self._purge_job(job_id, now)
            lease = self._leases.get(job_id)
            return max(lease[1] - now, 0) if lease is not None else 0

    def renew(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        lease_seconds: int,
        now: int,
    ) -> bool:
        with self._lock:
            owns, job = self._owns(job_id, token, fence, now)
            if not owns or job is None:
                return False
            ttl = min(lease_seconds, _integer(job, "expires_at") - now)
            if ttl <= 0:
                return False
            self._leases[job_id] = (_lease_value(token, fence), now + ttl)
            return True

    def guard(self, job_id: str, *, token: str, fence: int, now: int) -> bool:
        with self._lock:
            return self._owns(job_id, token, fence, now)[0]

    def prepare_intent(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        intent_json: str,
        ambiguous_on_takeover: bool,
        now: int,
    ) -> dict[str, object]:
        with self._lock:
            owns, job = self._owns(job_id, token, fence, now)
            if not owns or job is None:
                return {"status": "ownership_lost"}
            checkpoint = job.get(f"checkpoint:{name}")
            field = f"intent:{name}"
            existing_raw = job.get(field)
            if existing_raw is not None:
                existing = _json_object(existing_raw)
                incoming = _json_object(intent_json)
                comparable = ("kind", "chunk_index", "payload_hash")
                if (
                    existing is None
                    or incoming is None
                    or any(existing.get(key) != incoming.get(key) for key in comparable)
                ):
                    return {"status": "conflict"}
                if checkpoint is not None:
                    return {"status": "checkpointed", "checkpoint": checkpoint}
                old_fence = existing.get("fence")
                if ambiguous_on_takeover and old_fence != fence:
                    return {"status": "ambiguous"}
                return {"status": "prepared"}
            if checkpoint is not None:
                return {"status": "conflict"}
            job[field] = intent_json
            job["updated_at"] = str(now)
            return {"status": "prepared"}

    def checkpoint(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        checkpoint_json: str,
        now: int,
    ) -> str:
        with self._lock:
            owns, job = self._owns(job_id, token, fence, now)
            if not owns or job is None:
                return "ownership_lost"
            if f"intent:{name}" not in job:
                return "intent_missing"
            field = f"checkpoint:{name}"
            current = job.get(field)
            if current is not None and current != checkpoint_json:
                return "conflict"
            job[field] = checkpoint_json
            if name == "placeholder":
                decoded = _json_object(checkpoint_json)
                message_id = decoded.get("message_id") if decoded else None
                if isinstance(message_id, int) and not isinstance(message_id, bool):
                    job["placeholder_message_id"] = str(message_id)
            job["updated_at"] = str(now)
            return "checkpointed"

    def clear_intent(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        now: int,
    ) -> str:
        with self._lock:
            owns, job = self._owns(job_id, token, fence, now)
            if not owns or job is None:
                return "ownership_lost"
            if f"checkpoint:{name}" in job:
                return "checkpointed"
            job.pop(f"intent:{name}", None)
            job["updated_at"] = str(now)
            return "cleared"

    def save_answer(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        answer: str,
        answer_hash: str,
        now: int,
    ) -> dict[str, object]:
        with self._lock:
            owns, job = self._owns(job_id, token, fence, now)
            if not owns or job is None:
                return {"status": "ownership_lost"}
            if job.get("state") not in {"processing", "ready_to_deliver"}:
                return {"status": "state_rejected"}
            current = job.get("answer_text")
            if current is not None:
                if job.get("state") == "processing":
                    job["state"] = "ready_to_deliver"
                    job["updated_at"] = str(now)
                return {"status": "existing", "answer": current}
            job["answer_text"] = answer
            job["answer_sha256"] = answer_hash
            job["answer_saved_at"] = str(now)
            job["state"] = "ready_to_deliver"
            job["updated_at"] = str(now)
            return {"status": "saved", "answer": answer}

    def finish_owned(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        target_state: str,
        error_class: str | None,
        failure_notice_hash: str | None,
        failure_notice_text: str | None,
        now: int,
    ) -> str:
        with self._lock:
            owns, job = self._owns(job_id, token, fence, now)
            if not owns or job is None:
                return "ownership_lost"
            state = job.get("state", "")
            allowed_transitions = {
                "processing": {
                    "failed_retryable",
                    "failed",
                    "failed_ambiguous",
                },
                "ready_to_deliver": {
                    "delivered",
                    "failed_retryable",
                    "failed",
                    "failed_ambiguous",
                },
            }
            if target_state not in allowed_transitions.get(state, set()):
                return "state_rejected"
            job["state"] = target_state
            job["updated_at"] = str(now)
            if error_class:
                job["error_class"] = error_class
            else:
                job.pop("error_class", None)
            if target_state in TERMINAL_STATES:
                job["fence"] = str(fence + 1)
                self._leases.pop(job_id, None)
                if (
                    target_state == "failed"
                    and failure_notice_hash
                    and failure_notice_text
                ):
                    job["failure_notice_hash"] = failure_notice_hash
                    job["failure_notice_text"] = failure_notice_text
                    job["failure_notice_state"] = (
                        "pending" if job.get("placeholder_message_id") else "none"
                    )
            else:
                self._leases.pop(job_id, None)
            return "finished"

    def release(self, job_id: str, *, token: str, fence: int, now: int) -> bool:
        with self._lock:
            self._purge_job(job_id, now)
            lease = self._leases.get(job_id)
            if lease is None or lease[0] != _lease_value(token, fence):
                return False
            self._leases.pop(job_id, None)
            return True

    def failure_takeover(
        self,
        job_id: str,
        *,
        source_message_id: str,
        failure_notice_hash: str,
        failure_notice_text: str,
        max_retries: int,
        now: int,
    ) -> dict[str, object]:
        with self._lock:
            job = self._job(job_id, now)
            if job is None:
                return {"status": "missing"}
            saved_message_id = job.get("qstash_message_id")
            if saved_message_id is None:
                return {"status": "metadata_pending"}
            if saved_message_id != source_message_id:
                return {"status": "mismatch"}
            if _integer(job, "qstash_max_retries", -1) != max_retries:
                return {"status": "mismatch"}
            lease = self._leases.get(job_id)
            if lease is not None and lease[1] > now:
                return {"status": "busy", "ttl": lease[1] - now}
            state = job.get("state", "")
            if state in TERMINAL_STATES:
                return {"status": "terminal", "state": state}
            fence = _integer(job, "fence") + 1
            job["fence"] = str(fence)
            job["state"] = "failed"
            job["error_class"] = "qstash_retries_exhausted"
            job["updated_at"] = str(now)
            job["failure_notice_hash"] = failure_notice_hash
            job["failure_notice_text"] = failure_notice_text
            job["failure_notice_state"] = (
                "pending" if job.get("placeholder_message_id") else "none"
            )
            self._leases.pop(job_id, None)
            return {"status": "failed", "fence": fence}

    def claim_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        lease_seconds: int,
        now: int,
    ) -> dict[str, object]:
        with self._lock:
            job = self._job(job_id, now)
            if job is None:
                return {"status": "missing"}
            if job.get("state") != "failed":
                return {"status": "terminal"}
            notice_state = job.get("failure_notice_state", "none")
            if notice_state != "pending":
                return {"status": notice_state}
            placeholder = _integer(job, "placeholder_message_id")
            if placeholder <= 0:
                job["failure_notice_state"] = "none"
                return {"status": "none"}
            current = self._notice_leases.get(job_id)
            if current is not None and current[1] > now:
                return {"status": "busy", "ttl": current[1] - now}
            expires_at = _integer(job, "expires_at")
            ttl = min(lease_seconds, expires_at - now)
            if ttl <= 0:
                return {"status": "missing"}
            fence = _integer(job, "fence")
            self._notice_leases[job_id] = (_lease_value(token, fence), now + ttl)
            return {
                "status": "claimed",
                "fence": fence,
                "placeholder_message_id": placeholder,
                "failure_notice_hash": job.get("failure_notice_hash"),
                "failure_notice_text": job.get("failure_notice_text"),
            }

    def guard_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        now: int,
    ) -> bool:
        with self._lock:
            job = self._job(job_id, now)
            lease = self._notice_leases.get(job_id)
            return bool(
                job is not None
                and job.get("state") == "failed"
                and job.get("failure_notice_state") == "pending"
                and _integer(job, "fence") == fence
                and lease is not None
                and lease[0] == _lease_value(token, fence)
                and lease[1] > now
            )

    def complete_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        checkpoint_json: str,
        now: int,
    ) -> str:
        with self._lock:
            if not self.guard_failure_notice(job_id, token=token, fence=fence, now=now):
                return "ownership_lost"
            job = self._jobs[job_id]
            job["checkpoint:failure_notice"] = checkpoint_json
            job["failure_notice_state"] = "delivered"
            job["updated_at"] = str(now)
            self._notice_leases.pop(job_id, None)
            return "completed"

    def fail_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        error_class: str,
        now: int,
    ) -> str:
        with self._lock:
            if not self.guard_failure_notice(job_id, token=token, fence=fence, now=now):
                return "ownership_lost"
            job = self._jobs[job_id]
            job["failure_notice_state"] = "failed_permanent"
            job["failure_notice_error"] = error_class
            job["updated_at"] = str(now)
            self._notice_leases.pop(job_id, None)
            return "completed"

    def release_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        now: int,
    ) -> bool:
        with self._lock:
            self._purge_job(job_id, now)
            lease = self._notice_leases.get(job_id)
            if lease is None or lease[0] != _lease_value(token, fence):
                return False
            self._notice_leases.pop(job_id, None)
            return True

    def index_members(self, index_key: str, *, now: int) -> list[str]:
        with self._lock:
            self._purge_index(index_key, now)
            values = self._indexes.get(index_key, {})
            return [
                job_id
                for job_id, _score in sorted(
                    values.items(), key=lambda item: (item[1], item[0])
                )
            ]

    def ttl(self, key: str, *, now: int) -> int:
        with self._lock:
            if key.startswith("job:") and key.endswith(":lease"):
                job_id = key[4:-6]
                return self.lease_ttl(job_id, now=now)
            if key.startswith("job:") and key.endswith(":failure-lease"):
                job_id = key[4:-14]
                self._purge_job(job_id, now)
                lease = self._notice_leases.get(job_id)
                return max(lease[1] - now, 0) if lease is not None else 0
            if key.startswith("job:"):
                job_id = key[4:]
                self._purge_job(job_id, now)
                expires_at = self._job_expiry.get(job_id)
                return max(expires_at - now, 0) if expires_at is not None else 0
            self._purge_index(key, now)
            expires_at = self._index_expiry.get(key)
            return max(expires_at - now, 0) if expires_at is not None else 0


def utc_now() -> int:
    return int(time.time())
