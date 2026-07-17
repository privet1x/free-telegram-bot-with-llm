from __future__ import annotations

import json
import threading
import time

import pytest

from app.store import history, users
from app.store.dedup import mark_seen
from app.store.redis import MemoryKV, UpstashKV, _redis_slice, get_store


def test_set_nx_first_time_only():
    kv = MemoryKV()
    assert kv.set_nx("k", "1", ex=60) is True
    assert kv.set_nx("k", "1", ex=60) is False


def test_set_nx_expiry():
    kv = MemoryKV()
    assert kv.set_nx("k", "1", ex=1) is True
    # simulate expiry by moving the expiration time into the past
    kv._expiry["k"] = time.time() - 1
    assert kv.set_nx("k", "1", ex=1) is True  # key expired -> allowed again


def test_get_set_and_ping():
    kv = MemoryKV()
    assert kv.ping() is True
    assert kv.get("missing") is None
    kv.set("key", "first")
    kv.set("key", "second")
    assert kv.get("key") == "second"


def test_lpush_ltrim_lrange_order():
    kv = MemoryKV()
    for v in ["a", "b", "c"]:
        kv.lpush("l", v)  # list order: c, b, a (newest on the left)
    assert kv.lrange("l", 0, -1) == ["c", "b", "a"]
    kv.ltrim("l", 0, 1)
    assert kv.lrange("l", 0, -1) == ["c", "b"]
    assert kv.llen("l") == 2


def test_redis_slice_semantics():
    lst = ["0", "1", "2", "3", "4"]
    assert _redis_slice(lst, 0, -1) == lst
    assert _redis_slice(lst, 0, 1) == ["0", "1"]
    assert _redis_slice(lst, 0, 29) == lst  # stop out of range -> clamped
    assert _redis_slice(lst, -2, -1) == ["3", "4"]
    assert _redis_slice([], 0, -1) == []
    assert _redis_slice(lst, 3, 1) == []  # start > stop


def test_lrange_empty_key():
    kv = MemoryKV()
    assert kv.lrange("missing", 0, -1) == []
    assert kv.llen("missing") == 0


def test_list_upsert_replaces_in_place_and_always_trims():
    kv = MemoryKV()
    for message_id in range(1, 5):
        value = json.dumps({"message_id": message_id, "text": str(message_id)})
        kv.list_upsert_json("hist", "message_id", str(message_id), value, limit=3)

    assert [json.loads(item)["message_id"] for item in kv.lrange("hist", 0, -1)] == [
        4,
        3,
        2,
    ]

    replacement = json.dumps({"message_id": 3, "text": "edited"})
    assert kv.list_upsert_json("hist", "message_id", "3", replacement, limit=2) == 2
    values = [json.loads(item) for item in kv.lrange("hist", 0, -1)]
    assert [item["message_id"] for item in values] == [4, 3]
    assert values[1]["text"] == "edited"


def test_list_upsert_tolerates_corrupt_and_non_object_json_items():
    kv = MemoryKV()
    kv.lpush("hist", "not-json")
    kv.lpush("hist", "null")

    kv.list_upsert_json("hist", "message_id", "1", '{"message_id":1}', limit=30)

    assert kv.llen("hist") == 3


def test_recent_skips_corrupt_history_items(monkeypatch):
    monkeypatch.setattr(history.time, "time", lambda: 200)
    store = get_store()
    store.lpush(history.history_key(1), "not-json")
    store.lpush(history.history_key(1), "null")
    store.lpush(history.history_key(1), '{"message_id":1,"text":"valid","ts":190}')

    assert history.recent(1) == [{"message_id": 1, "text": "valid", "ts": 190}]


def test_list_upsert_is_thread_safe_for_duplicate_messages():
    kv = MemoryKV()

    def write(index: int) -> None:
        value = json.dumps({"message_id": 7, "text": f"version-{index}"})
        kv.list_upsert_json("hist", "message_id", "7", value, limit=30)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert kv.llen("hist") == 1
    assert json.loads(kv.lrange("hist", 0, 0)[0])["message_id"] == 7


def test_list_upsert_refreshes_expiry_and_expired_lists_are_purged():
    kv = MemoryKV()
    kv.list_upsert_json("hist", "message_id", "1", '{"message_id":1}', limit=30, ex=60)
    first_expiry = kv._expiry["hist"]
    kv.list_upsert_json("hist", "message_id", "1", '{"message_id":1}', limit=30, ex=60)
    assert kv._expiry["hist"] >= first_expiry

    kv._expiry["hist"] = time.time() - 1
    assert kv.lrange("hist", 0, -1) == []


def test_list_upsert_prunes_records_older_than_cutoff():
    kv = MemoryKV()
    kv.list_upsert_json(
        "hist",
        "message_id",
        "1",
        '{"message_id":1,"ts":100}',
        limit=30,
    )
    kv.list_upsert_json(
        "hist",
        "message_id",
        "2",
        '{"message_id":2,"ts":200}',
        limit=30,
        prune_field="ts",
        min_value=150,
    )

    assert [json.loads(item)["message_id"] for item in kv.lrange("hist", 0, -1)] == [
        2
    ]


def test_upstash_history_upsert_uses_one_atomic_eval():
    calls = []

    class FakeRedis:
        def eval(self, script, keys, args):
            calls.append((script, keys, args))
            return 1

    kv = UpstashKV.__new__(UpstashKV)
    kv._r = FakeRedis()
    result = kv.list_upsert_json(
        "hist:1", "message_id", "9", '{"message_id":9}', limit=30, ex=300
    )

    assert result == 1
    assert len(calls) == 1
    assert "table.sort" in calls[0][0]
    assert "RPUSH" in calls[0][0]
    assert "EXPIRE" in calls[0][0]
    assert calls[0][1] == ["hist:1"]
    assert calls[0][2][3:5] == ["30", "300"]


def test_upstash_observe_user_uses_one_atomic_eval():
    calls = []

    class FakeRedis:
        def eval(self, script, keys, args):
            calls.append((script, keys, args))
            return args[1]

    kv = UpstashKV.__new__(UpstashKV)
    kv._r = FakeRedis()
    value = '{"id":42,"username":"alice","last_seen_at":100,"last_update_id":1}'

    assert kv.observe_user_json(42, "alice", value) == value
    assert len(calls) == 1
    assert "compare_order" in calls[0][0]
    assert "username:" in calls[0][0]
    assert calls[0][1] == ["user:42"]
    assert calls[0][2] == ["42", value, "alice"]


def test_history_prunes_each_record_by_retention(monkeypatch):
    monkeypatch.setattr(history.settings, "HISTORY_RETENTION_SECONDS", 60)
    monkeypatch.setattr(history.time, "time", lambda: 200)

    history.upsert(1, {"message_id": 1, "ts": 100, "text": "old"})
    history.upsert(1, {"message_id": 2, "ts": 180, "text": "recent"})

    assert [item["message_id"] for item in history.recent(1)] == [2]


def test_history_read_physically_prunes_record_that_aged_out(monkeypatch):
    clock = {"now": 100}
    monkeypatch.setattr(history.settings, "HISTORY_RETENTION_SECONDS", 60)
    monkeypatch.setattr(history.time, "time", lambda: clock["now"])

    history.upsert(1, {"message_id": 1, "ts": 100, "text": "temporary"})
    assert get_store().llen(history.history_key(1)) == 1

    clock["now"] = 161
    assert history.recent(1) == []
    assert get_store().llen(history.history_key(1)) == 0


def test_history_rejects_new_records_without_canonical_identity_or_timestamp():
    with pytest.raises(ValueError, match="message_id"):
        history.upsert(1, {"message_id": "1", "ts": 100})
    with pytest.raises(ValueError, match="integer ts"):
        history.upsert(1, {"message_id": 1, "text": "missing timestamp"})


def test_history_read_physically_prunes_record_with_invalid_timestamp(monkeypatch):
    monkeypatch.setattr(history.time, "time", lambda: 200)
    store = get_store()
    key = history.history_key(1)
    store.lpush(key, '{"message_id":1,"text":"legacy","ts":"190"}')

    assert history.recent(1) == []
    assert store.llen(key) == 0


def test_delayed_original_update_cannot_roll_an_edit_back(monkeypatch):
    monkeypatch.setattr(history.settings, "HISTORY_RETENTION_SECONDS", 10_000)
    monkeypatch.setattr(history.time, "time", lambda: 1_000)
    original = {
        "message_id": 7,
        "source_update_id": 10,
        "ts": 100,
        "edit_ts": None,
        "is_edited": False,
        "text": "original",
    }
    edited = {
        **original,
        "source_update_id": 11,
        "edit_ts": 120,
        "is_edited": True,
        "text": "edited",
    }

    history.upsert(1, edited)
    history.upsert(1, original)

    assert history.recent(1)[0]["text"] == "edited"


def test_edit_of_evicted_message_does_not_displace_newer_history(monkeypatch):
    monkeypatch.setattr(history.settings, "HISTORY_RETENTION_SECONDS", 10_000)
    monkeypatch.setattr(history.time, "time", lambda: 1_000)
    for message_id in range(1, 32):
        history.upsert(
            1,
            {
                "message_id": message_id,
                "source_update_id": message_id,
                "ts": message_id,
                "edit_ts": None,
                "is_edited": False,
                "text": str(message_id),
            },
        )

    history.upsert(
        1,
        {
            "message_id": 1,
            "source_update_id": 32,
            "ts": 1,
            "edit_ts": 999,
            "is_edited": True,
            "text": "late edit",
        },
    )

    assert [record["message_id"] for record in history.recent(1)] == list(
        range(31, 1, -1)
    )


def test_upstash_read_prune_preserves_ttl_in_one_eval():
    calls = []

    class FakeRedis:
        def eval(self, script, keys, args):
            calls.append((script, keys, args))
            return 0

    kv = UpstashKV.__new__(UpstashKV)
    kv._r = FakeRedis()

    assert kv.list_prune_json("hist:1", "ts", 100) == 0
    assert len(calls) == 1
    assert "TTL" in calls[0][0]
    assert "EXPIRE" in calls[0][0]
    assert calls[0][1] == ["hist:1"]
    assert calls[0][2] == ["ts", "100"]


def test_delete():
    kv = MemoryKV()
    kv.set_nx("a", "1")
    kv.lpush("b", "x")
    assert kv.delete("a", "b", "missing") == 2  # 2 existed, 1 did not
    assert kv.set_nx("a", "1") is True  # key deleted -> allowed again
    assert kv.lrange("b", 0, -1) == []


def test_observed_user_username_index_tracks_renames():
    users.observe(
        {
            "id": 42,
            "username": "Alice",
            "name": "Alice A",
            "is_bot": False,
            "last_seen_at": 100,
        }
    )
    assert users.resolve_username("@aLiCe")["id"] == 42

    users.observe(
        {
            "id": 42,
            "username": "Alice_New",
            "name": "Alice A",
            "is_bot": False,
            "last_seen_at": 101,
        }
    )

    assert users.resolve_username("alice") is None
    assert users.resolve_username("@ALICE_NEW")["id"] == 42


def test_observed_user_rename_retry_does_not_leave_old_alias(monkeypatch):
    class FailNewAliasOnce(MemoryKV):
        def __init__(self):
            super().__init__()
            self.failed = False

        def observe_user_json(self, user_id, normalized_username, value):
            if normalized_username == "alice_new" and not self.failed:
                self.failed = True
                raise RuntimeError("temporary index failure")
            return super().observe_user_json(user_id, normalized_username, value)

    kv = FailNewAliasOnce()
    monkeypatch.setattr(users, "get_store", lambda: kv)
    users.observe(
        {
            "id": 42,
            "username": "Alice",
            "name": "Alice A",
            "is_bot": False,
            "last_seen_at": 100,
        }
    )

    renamed = {
        "id": 42,
        "username": "Alice_New",
        "name": "Alice A",
        "is_bot": False,
        "last_seen_at": 101,
    }
    with pytest.raises(RuntimeError, match="temporary index failure"):
        users.observe(renamed)

    # The failed atomic operation did not leave the old profile/index half-updated.
    assert users.resolve_username("alice")["id"] == 42
    users.observe(renamed)

    assert kv.get("username:alice") is None
    assert users.resolve_username("alice") is None
    assert users.resolve_username("alice_new")["id"] == 42


def test_observed_user_delayed_retry_cannot_roll_profile_back():
    users.observe(
        {
            "id": 42,
            "username": "New_Name",
            "name": "New",
            "is_bot": False,
            "last_seen_at": 200,
            "last_update_id": 20,
        }
    )

    result = users.observe(
        {
            "id": 42,
            "username": "Old_Name",
            "name": "Old",
            "is_bot": False,
            "last_seen_at": 100,
            "last_update_id": 10,
        }
    )

    assert result["username"] == "New_Name"
    assert users.resolve_username("new_name")["id"] == 42
    assert users.resolve_username("old_name") is None


def test_transferred_username_is_not_stolen_by_a_stale_retry():
    users.observe(
        {
            "id": 1,
            "username": "shared_name",
            "name": "A",
            "is_bot": False,
            "last_seen_at": 100,
            "last_update_id": 10,
        }
    )
    users.observe(
        {
            "id": 2,
            "username": "shared_name",
            "name": "B",
            "is_bot": False,
            "last_seen_at": 200,
            "last_update_id": 20,
        }
    )

    replay = users.observe(
        {
            "id": 1,
            "username": "shared_name",
            "name": "A retry",
            "is_bot": False,
            "last_seen_at": 150,
            "last_update_id": 15,
        }
    )

    assert replay["username"] is None
    assert users.resolve_username("shared_name")["id"] == 2


def test_completion_marker_elects_one_concurrent_winner():
    results = []
    lock = threading.Lock()

    def complete() -> None:
        won = mark_seen(1234)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=complete) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 19
