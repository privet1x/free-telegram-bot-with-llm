from __future__ import annotations

from datetime import datetime
import json

from app.memory import gathered_for_user, invalidate_source_message, lobotomy, observe_message
from app.memory import store as memory_store
from app.settings import settings
from app.store import history
from app.store import lobotomy_access
from app.store.redis import get_store
from app.store.jobs import get_job_repository
from app.telegram import scheduler
from app.telegram.identity import BotIdentity
from tests.conftest import make_update, post_webhook


def test_gathered_memory_is_bounded_and_lobotomy_owner_bypasses_cooldown(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 5)
    assert observe_message(
        chat_id=100,
        user_id=5,
        message_id=1,
        text="I like tea",
        timestamp=1_000,
    )
    assert gathered_for_user(100, 5)[0]["source_message_id"] == 1
    assert lobotomy(100, 5) == ("reset", 0)
    assert gathered_for_user(100, 5) == []
    assert lobotomy(100, 5) == ("reset", 0)


def test_static_manifest_rejects_duplicate_ids_and_missing_shards(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    shards = tmp_path / "shards"
    shards.mkdir()
    manifest.write_text(
        json.dumps({"participants": [{"user_id": 1, "slug": "user1"}, {"user_id": 1, "slug": "user2"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_store, "_MANIFEST_PATH", manifest)
    monkeypatch.setattr(memory_store, "_SHARDS_PATH", shards)
    monkeypatch.setattr(memory_store, "_static_cache", None)
    try:
        memory_store.static_shards()
    except RuntimeError as exc:
        assert "manifest" in str(exc)
    else:
        raise AssertionError("duplicate manifest entry was accepted")


def test_edited_message_invalidates_its_gathered_observation():
    assert observe_message(
        chat_id=100,
        user_id=5,
        message_id=77,
        text="old fact",
        timestamp=1_000,
    )
    assert invalidate_source_message(100, 5, 77)
    assert gathered_for_user(100, 5) == []


def test_gathered_shard_uses_sender_key_without_chat_id():
    assert observe_message(
        chat_id=100,
        user_id=5,
        message_id=78,
        text="sender-owned shard",
        timestamp=1_001,
    )
    assert get_store().get("memory:gathered5") is not None
    assert get_store().get("memory:gathered:100:5") is None
def test_lobotomy_requires_active_owner_or_invited_member(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 5)
    monkeypatch.setattr(
        "app.telegram.webhook.require_group_member",
        lambda user_id, **_: {
            "id": user_id,
            "name": "Alice" if user_id == 5 else "Bob",
            "username": "alice" if user_id == 5 else "bob",
        },
    )
    assert post_webhook(client, make_update(update_id=700, text="keep", message_id=700)).status_code == 200

    refused = post_webhook(
        client,
        make_update(update_id=701, text="/lobotomy", message_id=701, user_id=6, first_name="Bob"),
    )
    assert refused.json()["text"].startswith("Bob, Lobotomy is restricted")

    owner = post_webhook(
        client,
        make_update(update_id=702, text="/lobotomy", message_id=702, user_id=5),
    )
    assert owner.json()["text"].startswith("Alice, Lobotomy complete.")
    assert any(record.get("text") == "keep" for record in history.recent(100))
    assert all(record.get("text") != "keep" for record in history.context(100, 1))


def test_invite_allows_an_active_observed_member_to_run_lobotomy(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 5)
    monkeypatch.setattr(
        "app.telegram.webhook.require_group_member",
        lambda user_id, **_: {
            "id": user_id,
            "name": "Alice" if user_id == 5 else "Bob",
            "username": "alice" if user_id == 5 else "bob",
        },
    )
    post_webhook(client, make_update(update_id=710, text="hello", message_id=710, user_id=6, username="bob", first_name="Bob"))
    invited = post_webhook(
        client,
        make_update(update_id=711, text="/invite @bob", message_id=711, user_id=5),
    )
    assert invited.json()["text"].startswith("Alice, Bob is added to the /lobotomy roster.")
    assert lobotomy_access.is_invited(100, 6)

    allowed = post_webhook(
        client,
        make_update(update_id=712, text="/lobotomy", message_id=712, user_id=6, username="bob", first_name="Bob"),
    )
    assert allowed.json()["text"].startswith("Bob, Lobotomy complete.")
    uninvited = post_webhook(
        client,
        make_update(update_id=713, text="/uninvite @bob", message_id=713, user_id=5),
    )
    assert uninvited.json()["text"].startswith("Alice, Bob is removed from the /lobotomy roster.")
    assert not lobotomy_access.is_invited(100, 6)
    refused_again = post_webhook(
        client,
        make_update(update_id=714, text="/lobotomy", message_id=714, user_id=6, username="bob", first_name="Bob"),
    )
    assert refused_again.json()["text"].startswith("Bob, Lobotomy is restricted")


def test_invite_is_owner_only_and_requires_an_observed_member(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 5)
    monkeypatch.setattr(
        "app.telegram.webhook.require_group_member",
        lambda user_id, **_: {"id": user_id, "name": "Alice", "username": "alice"},
    )
    not_owner = post_webhook(
        client,
        make_update(update_id=720, text="/invite @nobody", message_id=720, user_id=6, username="bob", first_name="Bob"),
    )
    assert not_owner.json()["text"].startswith("Bob, Only the active super-admin")
    not_owner_uninvite = post_webhook(
        client,
        make_update(update_id=722, text="/uninvite @nobody", message_id=722, user_id=6, username="bob", first_name="Bob"),
    )
    assert not_owner_uninvite.json()["text"].startswith("Bob, Only the active super-admin")
    unknown = post_webhook(
        client,
        make_update(update_id=721, text="/invite @nobody", message_id=721, user_id=5),
    )
    assert "not observed" in unknown.json()["text"]


def test_keyword_reactions_are_one_job_without_auto_cooldown(client, monkeypatch):
    published: list[str] = []

    async def fake_publish(job_id: str) -> str:
        published.append(job_id)
        return f"qstash-{job_id}"

    monkeypatch.setattr("app.telegram.webhook.publish", fake_publish)
    first = post_webhook(client, make_update(update_id=710, text="Это БРЕД и босс", message_id=710))
    second = post_webhook(client, make_update(update_id=711, text="кикни его", message_id=711))
    assert first.status_code == second.status_code == 200
    assert get_job_repository().get(710).request["kind"] == "keyword"
    assert get_job_repository().get(711).request["kind"] == "keyword"
    assert published == ["710", "711"]


def test_scheduler_skips_quiet_hours_and_requires_auth(monkeypatch, client):
    monkeypatch.setattr(settings, "CRON_SECRET", "cron-secret")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 100)
    monkeypatch.setattr(
        scheduler,
        "_slot",
        lambda: (123, datetime(2026, 7, 23, 3, 0, tzinfo=scheduler.WARSAW)),
    )
    unauthorized = client.post("/api/cron/banter")
    quiet = client.post("/api/cron/banter", headers={"Authorization": "Bearer cron-secret"})
    assert unauthorized.status_code == 401
    assert quiet.json() == {"ok": True, "skipped": "quiet_hours"}


def test_scheduler_creates_a_job_even_without_human_context(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 100)
    monkeypatch.setattr(
        scheduler,
        "get_bot_identity",
        lambda: BotIdentity(id=999, username="test_bot", first_name="Bot"),
    )
    job = scheduler._build_job(
        100,
        125,
        datetime(2026, 7, 23, 10, 0, tzinfo=scheduler.WARSAW),
    )
    assert job is not None
    assert job.request["context"] == []
    assert job.request["kind"] == "scheduled"


def test_scheduler_uses_human_context_and_durable_slot(monkeypatch, client):
    monkeypatch.setattr(settings, "CRON_SECRET", "cron-secret")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 100)
    monkeypatch.setattr(
        scheduler,
        "_slot",
        lambda: (124, datetime(2026, 7, 23, 10, 0, tzinfo=scheduler.WARSAW)),
    )
    monkeypatch.setattr(
        scheduler,
        "get_bot_identity",
        lambda: BotIdentity(id=999, username="test_bot", first_name="Bot"),
    )
    published: list[str] = []

    async def fake_publish(job_id: str) -> str:
        published.append(job_id)
        return f"qstash-{job_id}"

    monkeypatch.setattr(scheduler, "publish", fake_publish)
    post_webhook(client, make_update(update_id=720, text="human context", message_id=720))
    response = client.post(
        "/api/cron/banter", headers={"Authorization": "Bearer cron-secret"}
    )
    duplicate = client.post(
        "/api/cron/banter", headers={"Authorization": "Bearer cron-secret"}
    )
    assert response.json()["queued"] is True
    assert duplicate.json()["dedup"] is True
    assert published == ["124"]
