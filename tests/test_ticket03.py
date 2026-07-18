from __future__ import annotations

from app.store import config_store, lists, rules
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


def test_tone_config_merges_and_preserves_custom_prompt():
    config_store.set_tone(
        "global", tone_mode="custom", custom_system_prompt="Be concise.", judge_default_n=25
    )
    without_override = config_store.get_config(100)
    assert without_override["chat_override"] is None
    assert without_override["effective"]["custom_system_prompt"] == "Be concise."
    assert without_override["effective"]["judge_default_n"] == 25
    config_store.set_tone("chat", chat_id=100, tone_mode="preset", tone_preset="serious")
    config = config_store.get_config(100)
    assert config["effective"]["tone_preset"] == "serious"
    assert config["global"]["custom_system_prompt"] == "Be concise."


def test_ignore_list_only_blocks_automatic_membership():
    lists.create({"slug": "aggressive", "title": "Aggressive", "priority": 5, "applies_to": ["auto"], "injected_prompt": "Be dry."})
    lists.create({"slug": "ignore", "title": "Ignore", "priority": 0, "applies_to": ["auto"], "injected_prompt": ""}, force=True)
    lists.add_member("ignore", 5)
    assert lists.member_lists(5, "auto") == []


def test_tone_commands_are_admin_only_idempotent_and_canonicalized(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 5)
    first = post_webhook(client, make_update(update_id=910, text="/tone sarcastic"))
    assert first.status_code == 200
    assert "sarcastic_robot" in first.json()["text"]
    assert config_store.get_config(100)["effective"]["tone_preset"] == "sarcastic_robot"

    duplicate = post_webhook(client, make_update(update_id=910, text="/tone street"))
    assert duplicate.json() == {"ok": True, "dedup": True}
    assert config_store.get_config(100)["effective"]["tone_preset"] == "sarcastic_robot"

    config_store.set_tone("global", tone_mode="custom", custom_system_prompt="Keep me")
    global_change = post_webhook(
        client, make_update(update_id=912, text="/tone global street")
    )
    assert global_change.json()["text"].endswith("street.")
    assert config_store.get_config(100)["global"]["custom_system_prompt"] == "Keep me"

    refused = post_webhook(client, make_update(update_id=911, user_id=7, text="/tone serious"))
    assert "administrator" in refused.json()["text"]
