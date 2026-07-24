from __future__ import annotations

import time

from app.memory import attach_image_analysis, gathered_for_user, record_message
from app.memory import images as image_memory
from app.memory import store as memory_store
from app.settings import settings
from app.store.redis import get_store
from app.store.jobs import get_job_repository
from app.telegram.models import parse_update
from tests.conftest import make_update, post_webhook


def _photo_update() -> dict:
    update = make_update(update_id=801, message_id=801, text="посмотри")
    update["message"].pop("text")
    update["message"]["caption"] = "посмотри"
    update["message"]["photo"] = [
        {"file_id": "small", "width": 100, "height": 100},
        {"file_id": "large", "width": 1200, "height": 800, "file_size": 5000},
    ]
    return update


def test_parse_selects_largest_photo_and_keeps_caption():
    message = parse_update(_photo_update())

    assert message is not None
    assert message.text == "посмотри"
    assert message.image_file_id == "large"
    assert message.image_mime_type == "image/jpeg"
    assert message.image_width == 1200
    assert message.image_height == 800


def test_record_message_and_image_analysis_are_sender_scoped():
    assert record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=1,
        text="мой текст",
        timestamp=1000,
    )
    assert record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=2,
        text="",
        timestamp=1001,
        image={"mime_type": "image/jpeg", "width": 10, "height": 10},
    )
    assert attach_image_analysis(
        chat_id=100,
        user_id=5,
        message_id=2,
        analysis="OCR: надпись\nОписание: схема",
    )
    records = gathered_for_user(100, 5)
    assert [item["source_message_id"] for item in records] == [1, 2]
    assert records[0]["name"] == "Alice"
    assert records[1]["image_analysis"].startswith("OCR:")
    assert gathered_for_user(100, 6) == []


def test_record_message_rejects_epoch_change_after_lock(monkeypatch):
    epochs = iter((0, 1))
    monkeypatch.setattr(memory_store, "current_epoch", lambda _chat: next(epochs))

    assert not memory_store.record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=3,
        text="stale",
        timestamp=1002,
        memory_epoch=0,
    )


def test_record_message_rejects_epoch_change_during_serialization(monkeypatch):
    epochs = iter((0, 0, 1, 1))
    monkeypatch.setattr(memory_store, "current_epoch", lambda _chat: next(epochs))

    assert not memory_store.record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=4,
        text="stale during write",
        timestamp=1003,
        memory_epoch=0,
    )


def test_legacy_gathered_entries_are_hidden_after_lobotomy_epoch():
    get_store().set(
        "memory:gathered:100:5",
        '[{"text":"old","source_message_id":1,"timestamp":1000,"confidence":0.5,"provenance":"legacy"}]',
    )
    get_store().set("memory:epoch:100", "1")

    assert memory_store.gathered_for_user(100, 5) == []


def test_user_purge_blocks_queued_old_memory_but_allows_new_messages():
    memory_store.record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=5,
        text="before purge",
        timestamp=int(time.time()) - 2,
    )
    memory_store.purge_user(100, 5)

    assert not memory_store.record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=6,
        text="queued old",
        timestamp=int(time.time()) - 1,
    )
    assert memory_store.record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=7,
        text="new after purge",
        timestamp=int(time.time()) + 1,
    )


def test_analyze_image_uses_multimodal_message_and_persists_result(monkeypatch):
    record_message(
        chat_id=100,
        user_id=5,
        name="Alice",
        message_id=2,
        text="",
        timestamp=1001,
        image={"mime_type": "image/jpeg"},
    )
    monkeypatch.setattr(
        image_memory,
        "get_file",
        lambda _file_id: {"file_path": "photos/example.jpg"},
    )
    monkeypatch.setattr(image_memory, "download_file", lambda _path, **_kwargs: b"jpeg")
    captured: list[object] = []
    generation_options: list[dict[str, object]] = []

    async def fake_generate(messages, **kwargs):
        captured.extend(messages)
        generation_options.append(kwargs)
        return '{"ocr":"тест","summary":"картинка"}'

    monkeypatch.setattr(image_memory, "generate", fake_generate)
    result = image_memory.analyze_image(
        {
            "chat_id": 100,
            "memory_epoch": 0,
            "author": {"id": 5},
            "trigger": {"message_id": 2, "text": ""},
            "image": {"file_id": "photo", "mime_type": "image/jpeg"},
        }
    )

    assert result == "OCR: тест\nОписание: картинка"
    assert "image_url" in repr(captured)
    assert "image_analysis" in gathered_for_user(100, 5)[0]
    assert generation_options == [
        {"thinking": False, "model": "google/gemma-4-31b-it"}
    ]


def test_captionless_image_creates_durable_memory_job_when_qstash_is_configured(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "QSTASH_TOKEN", "qstash-token")
    published: list[str] = []

    async def fake_publish(job_id: str) -> str:
        published.append(job_id)
        return "qstash-" + job_id

    monkeypatch.setattr("app.telegram.webhook.publish", fake_publish)
    update = make_update(update_id=802, message_id=802, text="placeholder")
    update["message"].pop("text")
    update["message"]["photo"] = [{"file_id": "photo", "width": 100, "height": 100}]

    response = post_webhook(client, update)
    job = get_job_repository().get(802)

    assert response.status_code == 200
    assert job is not None
    assert job.request["kind"] == "image_memory"
    assert job.request["image"]["file_id"] == "photo"
    assert published == ["802"]
    assert gathered_for_user(100, 5)[0]["entry_type"] == "image"
