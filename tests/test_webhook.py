from __future__ import annotations

import threading

from app.settings import settings
from app.store import history, users
from app.store.dedup import already_seen
from tests.conftest import make_update, post_webhook


# --- security: secret header ---


def test_rejects_missing_secret(client):
    r = post_webhook(client, make_update(), secret=None)
    assert r.status_code == 403


def test_rejects_wrong_secret(client):
    r = post_webhook(client, make_update(), secret="nope")
    assert r.status_code == 403


# --- history logging ---


def test_message_logged_to_history(client):
    update = make_update(update_id=1, text="hello chat", chat_id=100)
    update["message"]["date"] = 1_784_200_000
    r = post_webhook(client, update)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    hist = history.recent(100)
    assert len(hist) == 1
    assert hist[0]["text"] == "hello chat"
    assert hist[0]["username"] == "alice"
    assert hist[0]["name"] == "Alice"
    assert hist[0]["ts"] == 1_784_200_000
    assert users.resolve_username("@ALICE")["id"] == 5


def test_edited_message_replaces_original_in_place(client, monkeypatch):
    monkeypatch.setattr(history.time, "time", lambda: 200)
    original = make_update(update_id=1, message_id=10, text="original")
    original["message"]["date"] = 100
    edited = make_update(update_id=2, message_id=10, text="edited", edited=True)
    edited["edited_message"]["date"] = 100
    edited["edited_message"]["edit_date"] = 120

    post_webhook(client, original)
    r = post_webhook(client, edited)

    assert r.status_code == 200
    records = history.recent(100)
    assert len(records) == 1
    assert records[0]["text"] == "edited"
    assert records[0]["ts"] == 100
    assert records[0]["edit_ts"] == 120
    assert records[0]["is_edited"] is True


def test_update_without_message_ignored(client):
    r = client.post(
        "/api/telegram/webhook",
        json={"update_id": 7},
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert history.recent(100) == []


def test_other_chat_is_ignored_before_any_persistence(client, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 999)

    response = post_webhook(client, make_update(update_id=70, chat_id=100))

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}
    assert history.recent(100) == []
    assert users.resolve_username("alice") is None
    assert already_seen(70) is False


# --- commands ---


def test_ping(client):
    r = post_webhook(client, make_update(text="/ping"))
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "sendMessage"
    assert body["chat_id"] == 100
    assert body["text"] == "pong"


def test_ping_with_botname(client):
    r = post_webhook(client, make_update(update_id=11, text="/ping@test_bot"))
    assert r.json()["text"] == "pong"


def test_ping_addressed_to_another_bot_is_not_answered(client):
    r = post_webhook(client, make_update(update_id=13, text="/ping@OtherBot"))
    assert r.json() == {"ok": True, "ignored": True}


def test_captionless_media_updates_user_and_dedup_without_using_history(client):
    update = make_update(update_id=14, message_id=14, text="placeholder")
    update["message"].pop("text")
    update["message"]["photo"] = [{"file_id": "photo-1"}]

    first = post_webhook(client, update)
    duplicate = post_webhook(client, update)

    assert first.json() == {"ok": True}
    assert duplicate.json() == {"ok": True, "dedup": True}
    assert history.recent(100) == []
    assert users.get(5)["last_update_id"] == 14


def test_edit_that_removes_media_caption_removes_old_history_record(client):
    assert post_webhook(
        client,
        make_update(update_id=15, message_id=15, text="temporary caption"),
    ).status_code == 200
    edited = make_update(
        update_id=16,
        message_id=15,
        text="placeholder",
        edited=True,
    )
    edited["edited_message"].pop("text")
    edited["edited_message"]["photo"] = [{"file_id": "photo-1"}]

    response = post_webhook(client, edited)

    assert response.status_code == 200
    assert history.recent(100) == []


def test_help(client):
    r = post_webhook(client, make_update(update_id=12, text="/help"))
    body = r.json()
    assert body["method"] == "sendMessage"
    assert "/ping" in body["text"]


def test_edited_service_command_only_repairs_history(client):
    original = make_update(update_id=14, message_id=14, text="ordinary")
    edited = make_update(update_id=15, message_id=14, text="/ping", edited=True)

    assert post_webhook(client, original).json() == {"ok": True}
    assert post_webhook(client, edited).json() == {"ok": True}
    assert history.recent(100)[0]["text"] == "/ping"
    assert history.recent(100)[0]["is_service"] is True


# --- deduplication ---


def test_dedup_same_update_id(client):
    upd = make_update(update_id=42, text="once", chat_id=200)
    first = post_webhook(client, upd)
    second = post_webhook(client, upd)
    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True, "dedup": True}
    # the message is recorded exactly once
    assert len(history.recent(200)) == 1


def test_concurrent_duplicate_commands_have_one_response_winner(client, monkeypatch):
    real_upsert = history.upsert
    barrier = threading.Barrier(2)
    responses = []
    responses_lock = threading.Lock()

    def synchronized_upsert(chat_id, record):
        real_upsert(chat_id, record)
        barrier.wait(timeout=3)

    def deliver(update):
        response = post_webhook(client, update)
        with responses_lock:
            responses.append(response.json())

    monkeypatch.setattr(history, "upsert", synchronized_upsert)
    update = make_update(update_id=43, message_id=43, text="/ping", chat_id=201)
    threads = [threading.Thread(target=deliver, args=(update,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(body.get("text") == "pong" for body in responses) == 1
    assert responses.count({"ok": True, "dedup": True}) == 1
    assert len(history.recent(201)) == 1


def test_dedup_does_not_block_new_updates(client):
    post_webhook(client, make_update(update_id=1, message_id=1, text="a", chat_id=300))
    post_webhook(client, make_update(update_id=2, message_id=2, text="b", chat_id=300))
    assert len(history.recent(300)) == 2


def test_history_failure_does_not_mark_update_complete(client, monkeypatch):
    real_upsert = history.upsert
    calls = 0

    def fail_once(chat_id, record):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary Redis failure")
        return real_upsert(chat_id, record)

    monkeypatch.setattr(history, "upsert", fail_once)
    update = make_update(update_id=88, message_id=88, text="retry me")

    failed = post_webhook(client, update)
    assert failed.status_code == 503

    assert already_seen(88) is False
    assert history.recent(100) == []

    retry = post_webhook(client, update)
    assert retry.json() == {"ok": True}
    assert already_seen(88) is True
    assert [item["text"] for item in history.recent(100)] == ["retry me"]
    assert users.resolve_username("alice")["id"] == 5


# --- buffer capacity ---


def test_history_capped_at_30(client):
    for i in range(1, 36):  # 35 messages
        post_webhook(
            client,
            make_update(update_id=i, message_id=i, text=f"msg{i}", chat_id=400),
        )
    hist = history.recent(400)
    assert len(hist) == 30
    # newest first
    assert hist[0]["text"] == "msg35"
    # oldest kept is msg6 (msg1..msg5 evicted)
    assert hist[-1]["text"] == "msg6"


def test_recent_n_limit(client):
    for i in range(1, 11):
        post_webhook(
            client,
            make_update(update_id=i, message_id=i, text=f"m{i}", chat_id=500),
        )
    assert len(history.recent(500, n=3)) == 3


def test_webhook_fails_closed_without_allowlist_outside_vercel(client, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", None)
    monkeypatch.setattr(settings, "ALLOW_UNFILTERED_LOCAL_CHATS", False)

    response = post_webhook(client, make_update(update_id=90))

    assert response.status_code == 503
    assert history.recent(100) == []


def test_webhook_fails_closed_on_vercel_without_allowlist(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", None)

    response = post_webhook(client, make_update(update_id=91))

    assert response.status_code == 503
    assert history.recent(100) == []


def test_webhook_fails_closed_on_vercel_without_public_url(client, monkeypatch):
    # Keep this assertion local: production-looking credentials below must not
    # make the postcondition accidentally contact the network.
    assert history.recent(100) == []
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "test_bot")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 100)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_URL", "https://redis.test")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_TOKEN", "token")

    response = post_webhook(client, make_update(update_id=92))

    assert response.status_code == 503
    assert history.recent(100) == []


def test_webhook_fails_closed_on_vercel_with_unsafe_public_url(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "test_bot")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 100)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "http://example.test/path")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_URL", "https://redis.test")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_TOKEN", "token")

    response = post_webhook(client, make_update(update_id=93))

    assert response.status_code == 503


def test_production_bot_can_ingest_without_optional_admin_oidc_secrets(
    client, monkeypatch
):
    # Select in-memory test adapters before installing production-looking values.
    from app.store.jobs import get_job_repository

    assert history.recent(100) == []
    get_job_repository()
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 100)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bot.example")
    monkeypatch.setattr(
        settings, "UPSTASH_REDIS_REST_URL", "https://redis.example"
    )
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_TOKEN", "redis-token")
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "nvidia-key")
    monkeypatch.setattr(settings, "QSTASH_TOKEN", "qstash-token")
    monkeypatch.setattr(
        settings, "QSTASH_CURRENT_SIGNING_KEY", "current-signing-key"
    )
    monkeypatch.setattr(
        settings, "QSTASH_NEXT_SIGNING_KEY", "next-signing-key"
    )
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", None)
    monkeypatch.setattr(settings, "SESSION_SECRET", "")
    monkeypatch.setattr(settings, "TELEGRAM_OIDC_CLIENT_ID", "")
    monkeypatch.setattr(settings, "TELEGRAM_OIDC_CLIENT_SECRET", "")

    response = post_webhook(
        client, make_update(update_id=94, message_id=94, text="ordinary message")
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert history.recent(100)[0]["text"] == "ordinary message"
    assert client.get("/api/admin/me").status_code == 503
