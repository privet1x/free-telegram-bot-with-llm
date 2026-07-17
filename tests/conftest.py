from __future__ import annotations

import pytest

from app.settings import settings
from app.store import redis as store_mod

WEBHOOK_SECRET = "test-secret"


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    """Fresh in-memory store and predictable settings for each test."""
    # Force the memory backend even if Upstash creds exist in the environment.
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_URL", "")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "test_bot")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", None)
    monkeypatch.delenv("VERCEL", raising=False)
    store_mod.reset_store()
    yield
    store_mod.reset_store()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.server import app

    return TestClient(app)


def make_update(
    update_id: int = 1,
    text: str = "hello",
    chat_id: int = 100,
    user_id: int = 5,
    username: str = "alice",
    first_name: str = "Alice",
    is_bot: bool = False,
    edited: bool = False,
    reply_to_bot: bool = False,
    message_id: int = 10,
) -> dict:
    message = {
        "message_id": message_id,
        "date": 1_784_200_000,
        "chat": {"id": chat_id, "type": "group"},
        "from": {
            "id": user_id,
            "is_bot": is_bot,
            "first_name": first_name,
            "username": username,
        },
        "text": text,
    }
    if edited:
        message["edit_date"] = 1_784_200_001
    if reply_to_bot:
        message["reply_to_message"] = {
            "message_id": message_id - 1,
            "from": {"id": 999, "is_bot": True, "first_name": "TheBot"},
        }
    key = "edited_message" if edited else "message"
    return {"update_id": update_id, key: message}


def post_webhook(client, update: dict, secret: str | None = WEBHOOK_SECRET):
    headers = {}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return client.post("/api/telegram/webhook", json=update, headers=headers)
