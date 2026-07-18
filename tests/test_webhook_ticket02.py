from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from app.queue.qstash import QStashPublishError
from app.store import history
from app.store.dedup import already_seen, mark_seen
from app.store.jobs import chat_index_key, get_job_repository, user_index_key
from app.telegram import routing
from app.telegram import webhook as webhook_module
from app.telegram.identity import BotIdentity
from tests.conftest import make_update, post_webhook


BOT_ID = 999
BOT_IDENTITY = BotIdentity(id=BOT_ID, username="test_bot")


def _now() -> int:
    return int(time.time())


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _message(update: dict) -> dict:
    value = update.get("message", update.get("edited_message"))
    assert isinstance(value, dict)
    return value


def _update(
    update_id: int,
    message_id: int,
    text: str,
    *,
    user_id: int = 5,
    username: str = "alice",
    edited: bool = False,
) -> dict:
    update = make_update(
        update_id=update_id,
        message_id=message_id,
        text=text,
        user_id=user_id,
        username=username,
        edited=edited,
    )
    message = _message(update)
    message["date"] = _now() - 10
    if edited:
        message["edit_date"] = _now() - 1
    return update


def _mention_update(
    update_id: int,
    message_id: int,
    *,
    text: str = "@test_bot please answer",
    user_id: int = 5,
) -> dict:
    update = _update(
        update_id,
        message_id,
        text,
        user_id=user_id,
        username=f"user{user_id}",
    )
    token = "@test_bot"
    start = text.index(token)
    _message(update)["entities"] = [
        {
            "type": "mention",
            "offset": _utf16_units(text[:start]),
            "length": _utf16_units(token),
        }
    ]
    return update


def _reply_update(
    update_id: int,
    message_id: int,
    *,
    reply_user_id: int = BOT_ID,
    reply_is_bot: bool = True,
    user_id: int = 5,
) -> dict:
    update = _update(
        update_id,
        message_id,
        "answering the prior message",
        user_id=user_id,
        username=f"user{user_id}",
    )
    _message(update)["reply_to_message"] = {
        "message_id": message_id - 1,
        "date": _now() - 20,
        "from": {
            "id": reply_user_id,
            "is_bot": reply_is_bot,
            "first_name": "Prior sender",
        },
        "text": "prior message text",
    }
    return update


@pytest.fixture(autouse=True)
def verified_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routing, "get_bot_identity", lambda: BOT_IDENTITY)


def _capture_publications(
    monkeypatch: pytest.MonkeyPatch,
    *,
    callback: Callable[[str], None] | None = None,
) -> list[str]:
    published: list[str] = []

    async def fake_publish(*args: object, **kwargs: object) -> str:
        assert len(args) == 1
        assert kwargs == {}
        job_id = args[0]
        assert isinstance(job_id, str)
        published.append(job_id)
        if callback is not None:
            callback(job_id)
        return f"qstash-{job_id}"

    monkeypatch.setattr(webhook_module, "publish", fake_publish)
    return published


@pytest.mark.parametrize(
    ("update_factory", "expected_route"),
    [
        (_mention_update, "mention"),
        (_reply_update, "reply"),
    ],
)
def test_exact_mention_and_reply_to_this_bot_create_private_jobs(
    client,
    monkeypatch: pytest.MonkeyPatch,
    update_factory: Callable[[int, int], dict],
    expected_route: str,
) -> None:
    published = _capture_publications(monkeypatch)

    response = post_webhook(client, update_factory(200, 20))

    assert response.status_code == 200
    assert response.json() == {"ok": True, "queued": True}
    assert published == ["200"]
    job = get_job_repository().get("200")
    assert job is not None
    assert job.state == "enqueued"
    assert job.qstash_message_id == "qstash-200"
    assert job.request["route"] == expected_route
    assert job.request["kind"] == "reply"


def test_snapshot_is_chronological_and_excludes_trigger_and_later_messages(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _capture_publications(monkeypatch)
    assert post_webhook(client, _update(201, 21, "first")).json() == {"ok": True}
    assert post_webhook(client, _update(202, 22, "second")).json() == {"ok": True}

    response = post_webhook(client, _mention_update(203, 23))
    assert response.json() == {"ok": True, "queued": True}
    assert post_webhook(client, _update(204, 24, "later")).json() == {"ok": True}

    job = get_job_repository().get(203)
    assert job is not None
    context = job.request["context"]
    assert isinstance(context, list)
    assert [record["text"] for record in context] == ["first", "second"]
    assert job.request["trigger_text"] == "@test_bot please answer"
    assert all(record["message_id"] != 23 for record in context)
    assert all(record["text"] != "later" for record in context)
    assert published == ["203"]


def test_retry_reuses_immutable_snapshot_and_server_computed_role(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fail_then_publish(job_id: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise QStashPublishError("qstash_unavailable", retryable=True)
        return f"qstash-{job_id}"

    monkeypatch.setattr(webhook_module, "publish", fail_then_publish)
    monkeypatch.setattr(webhook_module.settings, "SUPER_ADMIN_ID", 5)
    original = _mention_update(205, 25, text="@test_bot original request")

    first = post_webhook(client, original)
    assert first.status_code == 503
    before = get_job_repository().get(205)
    assert before is not None
    assert before.state == "received"
    original_request = before.request
    original_policy = before.effective_policy
    assert original_policy["actor"] == {"user_id": 5, "is_admin": True}
    assert already_seen(205) is False

    monkeypatch.setattr(webhook_module.settings, "SUPER_ADMIN_ID", None)
    changed_retry = _mention_update(205, 25, text="@test_bot changed retry body")
    second = post_webhook(client, changed_retry)

    assert second.status_code == 200
    assert second.json() == {"ok": True, "queued": True}
    after = get_job_repository().get(205)
    assert after is not None
    assert after.request == original_request
    assert after.effective_policy == original_policy
    assert after.request["trigger_text"] == "@test_bot original request"
    assert history.recent(100)[0]["text"] == "@test_bot original request"
    assert calls == 2


def test_edit_and_reply_to_another_bot_never_publish(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_publish(_: str) -> str:
        pytest.fail("an edit or reply to another bot must not be queued")

    monkeypatch.setattr(webhook_module, "publish", unexpected_publish)
    edited = _mention_update(206, 26)
    edited["edited_message"] = edited.pop("message")
    _message(edited)["edit_date"] = _now()

    edit_response = post_webhook(client, edited)
    other_bot_response = post_webhook(
        client,
        _reply_update(207, 27, reply_user_id=888, reply_is_bot=True),
    )

    assert edit_response.json() == {"ok": True}
    assert other_bot_response.json() == {"ok": True}
    assert get_job_repository().get(206) is None
    assert get_job_repository().get(207) is None
    assert {record["message_id"] for record in history.recent(100)} == {26, 27}


def test_publication_failure_returns_503_and_early_marker_cannot_skip_retry(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fail_then_publish(job_id: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise QStashPublishError("qstash_timeout", retryable=True)
        return f"qstash-{job_id}"

    monkeypatch.setattr(webhook_module, "publish", fail_then_publish)
    update = _mention_update(208, 28)

    first = post_webhook(client, update)
    job = get_job_repository().get(208)
    assert first.status_code == 503
    assert job is not None and job.state == "received"
    assert job.qstash_message_id is None
    assert already_seen(208) is False

    # Even a stale/early receipt marker is not a safe enqueue marker.
    assert mark_seen(208) is True
    retry = post_webhook(client, update)

    assert retry.status_code == 200
    assert retry.json() == {"ok": True, "queued": True}
    completed = get_job_repository().get(208)
    assert completed is not None
    assert completed.state == "enqueued"
    assert completed.qstash_message_id == "qstash-208"
    assert calls == 2


def test_callback_before_publish_response_does_not_downgrade_processing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = get_job_repository()

    def worker_arrives(job_id: str) -> None:
        acquisition = repository.acquire(job_id, token="worker-before-response")
        assert acquisition.status == "acquired"

    _capture_publications(monkeypatch, callback=worker_arrives)

    response = post_webhook(client, _mention_update(209, 29))

    assert response.status_code == 200
    assert response.json() == {"ok": True, "queued": True}
    job = repository.get(209)
    assert job is not None
    assert job.state == "processing"
    assert job.qstash_message_id == "qstash-209"
    assert already_seen(209) is True


def test_history_failure_keeps_received_job_for_retry(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _capture_publications(monkeypatch)
    real_upsert = history.upsert
    calls = 0

    def fail_once(chat_id: int, record: dict) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary history failure")
        real_upsert(chat_id, record)

    monkeypatch.setattr(history, "upsert", fail_once)
    update = _mention_update(210, 30)

    failed = post_webhook(client, update)
    assert failed.status_code == 503

    pending = get_job_repository().get(210)
    assert pending is not None
    assert pending.state == "received"
    assert pending.qstash_message_id is None
    assert published == []
    assert already_seen(210) is False

    retry = post_webhook(client, update)
    assert retry.json() == {"ok": True, "queued": True}
    assert published == ["210"]
    assert history.recent(100)[0]["message_id"] == 30


def test_job_indexes_every_user_present_in_snapshot(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_publications(monkeypatch)
    timestamp = _now() - 30
    history.upsert(
        100,
        {
            "message_id": 31,
            "source_update_id": 211,
            "user_id": 10,
            "username": "ten",
            "name": "Ten",
            "text": "context one",
            "ts": timestamp,
            "edit_ts": None,
            "is_edited": False,
            "is_bot": False,
            "reply_to": {
                "message_id": 30,
                "user_id": 11,
                "is_bot": False,
                "text": "nested one",
            },
        },
    )
    history.upsert(
        100,
        {
            "message_id": 32,
            "source_update_id": 212,
            "user_id": 12,
            "username": "twelve",
            "name": "Twelve",
            "text": "context two",
            "ts": timestamp + 1,
            "edit_ts": None,
            "is_edited": False,
            "is_bot": False,
            "reply_to": {
                "message_id": 31,
                "user_id": 13,
                "is_bot": False,
                "text": "nested two",
            },
        },
    )

    response = post_webhook(client, _reply_update(213, 33, user_id=14))

    assert response.json() == {"ok": True, "queued": True}
    repository = get_job_repository()
    assert repository.index_job_ids(chat_index_key(100)) == ["213"]
    for user_id in {10, 11, 12, 13, 14, BOT_ID}:
        assert repository.index_job_ids(user_index_key(user_id)) == ["213"]
