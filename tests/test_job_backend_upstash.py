from __future__ import annotations

import threading

from app.store import job_backend_upstash as backend_module
from app.store.job_backend_upstash import UpstashJobBackend


class _RecordingRedis:
    def __init__(self, result: object = "ok") -> None:
        self.result = result
        self.calls: list[tuple[str, list[str], list[str]]] = []

    def eval(self, script: str, *, keys: list[str], args: list[str]) -> object:
        self.calls.append((script, keys, args))
        return self.result


def _backend(redis: _RecordingRedis) -> UpstashJobBackend:
    backend = UpstashJobBackend.__new__(UpstashJobBackend)
    backend._r = redis
    return backend


def test_upstash_lua_contract_covers_fencing_checkpoints_and_retry_policy():
    assert "EXPIREAT" in backend_module._ACQUIRE_LUA
    assert "attempts" in backend_module._ACQUIRE_LUA
    assert "checkpoint" in backend_module._PREPARE_INTENT_LUA
    assert "payload_hash" in backend_module._PREPARE_INTENT_LUA
    assert "failed_retryable" in backend_module._FINISH_OWNED_LUA
    assert "ready_to_deliver" in backend_module._FINISH_OWNED_LUA
    assert "qstash_max_retries" in backend_module._FAILURE_TAKEOVER_LUA
    assert "failure_notice_hash" in backend_module._CLAIM_FAILURE_NOTICE_LUA


def test_upstash_failure_takeover_passes_callback_retry_policy():
    redis = _RecordingRedis('{"status":"mismatch"}')
    backend = _backend(redis)

    result = backend.failure_takeover(
        "job-1",
        source_message_id="qstash-1",
        failure_notice_hash="a" * 64,
        failure_notice_text="failed",
        max_retries=4,
        now=100,
    )

    assert result == {"status": "mismatch"}
    assert redis.calls[-1][2][-1] == "4"


def test_upstash_privacy_purge_returns_snapshot_and_removes_all_indexes_atomically():
    redis = _RecordingRedis(
        ["state", "processing", "checkpoint:placeholder", '{"message_id":9001}']
    )
    backend = _backend(redis)

    result = backend.purge(
        "job-1",
        index_keys=["jobs:chat:-100", "jobs:user:42"],
        receipt_key="privacy:receipt:test",
        receipt_ttl=604_800,
        now=100,
    )

    assert result == {
        "state": "processing",
        "checkpoint:placeholder": '{"message_id":9001}',
    }
    script, keys, args = redis.calls[-1]
    assert script.index("HGETALL") < script.index("DEL")
    assert "ZREM" in script
    assert keys[-3:] == [
        "privacy:receipt:test",
        "jobs:chat:-100",
        "jobs:user:42",
    ]
    assert "SADD" in script and "EXPIRE" in script
    assert args == ["job-1", "604800"]


def test_upstash_job_calls_use_independent_thread_local_clients(monkeypatch):
    barrier = threading.Barrier(2)
    client_ids: list[int] = []
    result_lock = threading.Lock()

    class ConcurrentRedis(_RecordingRedis):
        def eval(self, script, *, keys, args):
            with result_lock:
                client_ids.append(id(self))
            barrier.wait(timeout=1)
            return super().eval(script, keys=keys, args=args)

    monkeypatch.setattr(
        backend_module, "build_upstash_redis", lambda _url, _token: ConcurrentRedis()
    )
    backend = UpstashJobBackend("https://redis.example", "token")
    errors: list[BaseException] = []

    def evaluate() -> None:
        try:
            backend._eval("return 1", keys=["job:1"], args=[])
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=evaluate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(client_ids)) == 2
