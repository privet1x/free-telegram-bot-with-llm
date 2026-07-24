from __future__ import annotations

import pytest

from app.store import config_store, lists, rules
from app.store.redis import MemoryKV, get_store
from app.store.jobs import get_job_repository
from app.settings import settings
from app.telegram import webhook as webhook_module
from tests.conftest import make_update, post_webhook


def test_rule_matching_normalizes_unicode_and_punctuation():
    rule = {
        "id": "root",
        "match": {"type": "word", "value": "NONSENSE"},
        "instruction": "answer calmly",
        "scope": "all",
        "priority": 1,
    }
    assert rules.create(rule)["id"] == "root"
    assert rules.matches(rule, "This — nonsense!") is True
    assert rules.matches(rule, "nonsensical") is False


def test_rule_store_rejects_unbounded_and_multi_token_word_matches():
    base = {
        "id": "invalid",
        "instruction": "Reply.",
        "scope": "all",
        "priority": 1,
    }
    with pytest.raises(ValueError, match="match.value"):
        rules.create(
            {**base, "match": {"type": "substring", "value": "x" * 513}}
        )
    with pytest.raises(ValueError, match="exactly one token"):
        rules.create(
            {**base, "match": {"type": "word", "value": "two words"}}
        )


def test_rule_resolution_groups_stop_processing_and_is_deterministic():
    rules.create({"id": "z", "match": {"type": "substring", "value": "x"}, "instruction": "z", "scope": "all", "priority": 5})
    rules.create({"id": "a", "match": {"type": "substring", "value": "x"}, "instruction": "a", "scope": "all", "priority": 5, "stop_processing": True})
    rules.create({"id": "low", "match": {"type": "substring", "value": "x"}, "instruction": "low", "scope": "all", "priority": 1})
    assert [item["id"] for item in rules.resolve("x", "auto")] == ["a", "z"]


def test_automatic_rule_creates_job_and_cooldown_suppresses_next_update(
    client, monkeypatch
):
    published: list[str] = []

    async def publish(job_id: str) -> str:
        published.append(str(job_id))
        return f"qstash-{job_id}"

    monkeypatch.setattr(webhook_module, "publish", publish)
    rules.create(
        {
            "id": "nonsense",
            "match": {"type": "word", "value": "nonsense"},
            "instruction": "Explain calmly.",
            "scope": "all",
            "priority": 10,
        }
    )
    first = post_webhook(client, make_update(update_id=900, message_id=900, text="this is nonsense"))
    assert first.status_code == 200
    job = get_job_repository().get(900)
    assert job is not None and job.request["kind"] == "auto_rule"
    assert job.effective_policy["rule_policies"][0]["id"] == "nonsense"
    assert published == ["900"]

    second = post_webhook(client, make_update(update_id=901, message_id=901, text="more nonsense"))
    assert second.status_code == 200
    assert second.json()["suppressed"] is True
    assert get_job_repository().get(901) is None


def test_automatic_route_reuses_the_authoritative_rule_snapshot(
    client, monkeypatch
):
    async def publish(job_id: str) -> str:
        return f"qstash-{job_id}"

    monkeypatch.setattr(webhook_module, "publish", publish)
    rules.create(
        {
            "id": "stable",
            "match": {"type": "word", "value": "trigger"},
            "instruction": "Stable instruction.",
            "scope": "auto",
            "priority": 1,
        }
    )
    real_resolve = rules.resolve
    calls = 0

    def resolve_once(text, scope):
        nonlocal calls
        calls += 1
        if calls > 1:
            pytest.fail("automatic policy rules were loaded twice")
        return real_resolve(text, scope)

    monkeypatch.setattr(rules, "resolve", resolve_once)
    response = post_webhook(
        client,
        make_update(update_id=905, message_id=905, text="trigger"),
    )

    assert response.status_code == 200
    job = get_job_repository().get(905)
    assert job is not None
    assert job.effective_policy["rule_policies"][0]["id"] == "stable"
    assert calls == 1


def test_corrupt_rule_index_is_retryable_instead_of_silently_ignored(client):
    store = get_store()
    store.sadd(rules.RULES_INDEX_KEY, "broken")
    store.set("rule:broken", "{not-json")

    response = post_webhook(
        client,
        make_update(update_id=906, message_id=906, text="potential trigger"),
    )

    assert response.status_code == 503
    assert get_job_repository().get(906) is None
    assert store.get("dedup:update:906") is None


def test_tone_config_merges_global_and_chat_presets():
    config_store.set_tone("global", tone_preset="scientist")
    without_override = config_store.get_config(100)
    assert without_override["chat_override"] is None
    assert without_override["effective"] == {"tone_preset": "scientist"}
    config_store.set_tone("chat", chat_id=100, tone_preset="serious")
    config = config_store.get_config(100)
    assert config["effective"] == {"tone_preset": "serious"}
    assert config["global"] == {"tone_preset": "scientist"}


def test_legacy_tone_fields_are_discarded_on_read():
    get_store().set(
        config_store.GLOBAL_CONFIG_KEY,
        (
            '{"tone_mode":"custom","tone_preset":"sarcastic_robot",'
            '"custom_system_prompt":"replace core","judge_default_n":30}'
        ),
    )

    assert config_store.get_config(100)["global"] == {
        "tone_preset": "sarcastic_bot"
    }


def test_ignore_list_only_blocks_automatic_membership():
    lists.create({"slug": "aggressive", "title": "Aggressive", "priority": 5, "applies_to": ["auto"], "injected_prompt": "Be dry."})
    lists.create({"slug": "ignore", "title": "Ignore", "priority": 0, "applies_to": ["auto"], "injected_prompt": ""}, force=True)
    lists.add_member("ignore", 5)
    assert lists.member_lists(5, "auto") == []


def test_policy_loading_batches_metadata_and_membership_reads(monkeypatch):
    store = MemoryKV()
    monkeypatch.setattr(lists, "get_store", lambda: store)
    monkeypatch.setattr(rules, "get_store", lambda: store)
    lists.create(
        {
            "slug": "one",
            "title": "One",
            "priority": 2,
            "applies_to": ["explicit"],
            "injected_prompt": "First policy.",
        }
    )
    lists.create(
        {
            "slug": "two",
            "title": "Two",
            "priority": 1,
            "applies_to": ["explicit"],
            "injected_prompt": "Second policy.",
        }
    )
    lists.add_member("one", 5)
    rules.create(
        {
            "id": "hello",
            "match": {"type": "word", "value": "hello"},
            "instruction": "Reply.",
            "scope": "all",
            "priority": 1,
        }
    )
    calls = {"get_many": 0, "memberships": 0}
    real_get_many = store.get_many
    real_memberships = store.set_memberships

    def get_many(keys):
        calls["get_many"] += 1
        return real_get_many(keys)

    def memberships(keys, member):
        calls["memberships"] += 1
        return real_memberships(keys, member)

    monkeypatch.setattr(store, "get_many", get_many)
    monkeypatch.setattr(store, "set_memberships", memberships)

    assert [item["slug"] for item in lists.member_lists(5, "explicit")] == ["one"]
    assert [item["id"] for item in rules.resolve("hello", "explicit")] == ["hello"]
    assert calls == {"get_many": 2, "memberships": 1}


def test_policy_entity_caps_are_enforced_atomically(monkeypatch):
    monkeypatch.setattr(lists, "MAX_LISTS", 1)
    monkeypatch.setattr(rules, "MAX_RULES", 1)
    lists.create(
        {
            "slug": "one",
            "title": "One",
            "priority": 1,
            "applies_to": ["auto"],
            "injected_prompt": "One.",
        }
    )
    with pytest.raises(ValueError, match="at most 1"):
        lists.create(
            {
                "slug": "two",
                "title": "Two",
                "priority": 1,
                "applies_to": ["auto"],
                "injected_prompt": "Two.",
            }
        )
    rules.create(
        {
            "id": "one",
            "match": {"type": "word", "value": "one"},
            "instruction": "One.",
            "scope": "auto",
            "priority": 1,
        }
    )
    with pytest.raises(ValueError, match="at most 1"):
        rules.create(
            {
                "id": "two",
                "match": {"type": "word", "value": "two"},
                "instruction": "Two.",
                "scope": "auto",
                "priority": 1,
            }
        )


def test_tone_commands_are_public_idempotent_and_canonicalized(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 5)
    first = post_webhook(
        client,
        make_update(update_id=910, user_id=7, text="/tone sarcastic"),
    )
    assert first.status_code == 200
    assert "sarcastic_bot" in first.json()["text"]
    assert config_store.get_config(100)["effective"]["tone_preset"] == "sarcastic_bot"

    duplicate = post_webhook(
        client,
        make_update(update_id=910, user_id=7, text="/tone street"),
    )
    assert duplicate.json() == {"ok": True, "dedup": True}
    assert config_store.get_config(100)["effective"]["tone_preset"] == "sarcastic_bot"

    second_user = post_webhook(
        client,
        make_update(update_id=911, user_id=8, first_name="Bob", text="/tone serious"),
    )
    assert second_user.json()["text"] == "Bob, Tone set to serious."
    assert config_store.get_config(100)["effective"]["tone_preset"] == "serious"


def test_mode_configuration_failure_does_not_consume_command_receipt(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 5)
    real_get_config = config_store.get_config
    calls = 0

    def fail_once(chat_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary configuration failure")
        return real_get_config(chat_id)

    monkeypatch.setattr(config_store, "get_config", fail_once)
    update = make_update(update_id=913, message_id=913, text="/mode")

    first = post_webhook(client, update)
    retry = post_webhook(client, update)

    assert first.status_code == 503
    assert retry.status_code == 200
    assert retry.json()["text"].startswith("Alice, Mode:")
