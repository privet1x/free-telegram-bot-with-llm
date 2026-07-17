"""Check the Upstash Redis (REST) connection through our store layer.

Performs a real round-trip, including the production Lua EVAL history upsert /
edit / prune path, and cleans up after itself. Run from the project root:
    python scripts/check_redis.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.settings import settings  # noqa: E402
from app.store.redis import get_store, reset_store  # noqa: E402


def main() -> None:
    if not (settings.UPSTASH_REDIS_REST_URL and settings.UPSTASH_REDIS_REST_TOKEN):
        print("Upstash is not configured in .env (no URL/TOKEN) — using in-memory.")
        sys.exit(1)

    reset_store()
    store = get_store()
    print("backend        :", store.backend())
    print("endpoint       :", settings.UPSTASH_REDIS_REST_URL)

    run_id = uuid.uuid4().hex
    prefix = f"healthcheck:{run_id}"
    kv_key = prefix
    list_key = prefix + ":list"
    history_key = prefix + ":history"
    # Stay below 2**53: Redis Lua/cjson represents JSON numbers as doubles.
    probe_user_id = 8_000_000_000_000_000 + (uuid.uuid4().int % 10**15)
    probe_username = f"hc_{run_id[:20]}"
    profile_key = f"user:{probe_user_id}"
    alias_key = f"username:{probe_username}"
    try:
        set_ok = store.set_nx(kv_key, "1", ex=30)
        store.lpush(list_key, "b")
        store.lpush(list_key, "a")
        values = store.lrange(list_key, 0, -1)
        length = store.llen(list_key)
        now = int(time.time())
        store.list_upsert_json(
            history_key,
            "message_id",
            "1",
            '{"message_id":1,"source_update_id":1,"ts":%d,"text":"first"}'
            % now,
            limit=30,
            ex=30,
            prune_field="ts",
            min_value=now - 1,
        )
        store.list_upsert_json(
            history_key,
            "message_id",
            "1",
            '{"message_id":1,"source_update_id":2,"ts":%d,"edit_ts":%d,'
            '"is_edited":true,"text":"edited"}' % (now, now + 1),
            limit=30,
            ex=30,
            prune_field="ts",
            min_value=now - 1,
        )
        history_values = store.lrange(history_key, 0, -1)
        store.list_prune_json(history_key, "ts", now + 1)
        history_pruned = store.llen(history_key)
        observed_user = json.loads(
            store.observe_user_json(
                probe_user_id,
                probe_username,
                json.dumps(
                    {
                        "id": probe_user_id,
                        "username": probe_username,
                        "name": "Redis healthcheck",
                        "is_bot": False,
                        "last_seen_at": now,
                        "last_update_id": 1,
                    },
                    separators=(",", ":"),
                ),
            )
        )
        print("SET NX         :", set_ok)
        print("LPUSH+LRANGE   :", values, "(expected ['a', 'b'])")
        print("LLEN           :", length)
        print("history EVAL   :", history_values, "(expected one edited record)")
        print("history prune  :", history_pruned, "(expected 0)")
        print("user EVAL      :", observed_user["username"] == probe_username)
        assert set_ok is True
        assert values == ["a", "b"]
        assert length == 2
        assert len(history_values) == 1 and '"text":"edited"' in history_values[0]
        assert history_pruned == 0
        assert observed_user["id"] == probe_user_id
        assert observed_user["username"] == probe_username
        assert store.get(alias_key) == str(probe_user_id)
    finally:
        removed = store.delete(kv_key, list_key, history_key, profile_key, alias_key)
        print("cleanup deleted:", removed, "key(s)")

    print("\nOK — Upstash Redis responds over REST, our store layer works.")


if __name__ == "__main__":
    main()
