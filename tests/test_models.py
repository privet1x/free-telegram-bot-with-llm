from __future__ import annotations

import pytest
import httpx

from app.telegram import client as telegram_client
from app.telegram.models import (
    MAX_HISTORY_TEXT_CHARS,
    parse_command,
    parse_update,
    to_history_record,
    to_observed_user,
)
from tests.conftest import make_update


def test_parse_basic_message():
    msg = parse_update(make_update(update_id=3, text="hi", chat_id=100, user_id=5))
    assert msg is not None
    assert msg.update_id == 3
    assert msg.chat_id == 100
    assert msg.user_id == 5
    assert msg.text == "hi"
    assert msg.is_edited is False
    assert msg.reply_to_bot is False


def test_parse_edited_message_flag():
    update = make_update(text="x", edited=True)
    update["edited_message"]["date"] = 100
    update["edited_message"]["edit_date"] = 120
    msg = parse_update(update)
    assert msg is not None
    assert msg.is_edited is True
    assert msg.date == 100
    assert msg.edit_date == 120


def test_parse_reply_to_bot():
    update = make_update(reply_to_bot=True)
    update["message"]["reply_to_message"]["text"] = "earlier bot answer"
    msg = parse_update(update)
    assert msg is not None
    assert msg.reply_to_bot is True
    assert msg.reply_to_message_id == 9
    assert msg.reply_to_user_id == 999
    assert msg.reply_to_text == "earlier bot answer"


def test_parse_name_fallbacks():
    upd = make_update()
    upd["message"]["from"] = {"id": 9, "is_bot": False, "username": "onlyuser"}
    msg = parse_update(upd)
    assert msg is not None
    assert msg.name == "onlyuser"


def test_parse_returns_none_without_message():
    assert parse_update({"update_id": 1}) is None
    assert parse_update({}) is None
    assert (
        parse_update({"update_id": 1, "message": {"from": {"id": 1}}}) is None
    )  # no chat
    assert parse_update({"update_id": "invalid", "message": {}}) is None

    missing_date = make_update()
    missing_date["message"].pop("date")
    assert parse_update(missing_date) is None

    missing_edit_date = make_update(edited=True)
    missing_edit_date["edited_message"].pop("edit_date")
    assert parse_update(missing_edit_date) is None


def test_parse_rejects_boolean_identifiers_and_timestamps():
    bad_update_id = make_update()
    bad_update_id["update_id"] = True
    assert parse_update(bad_update_id) is None

    bad_timestamp = make_update()
    bad_timestamp["message"]["date"] = False
    assert parse_update(bad_timestamp) is None


def test_parse_caption_and_caption_entities():
    update = make_update()
    message = update["message"]
    message.pop("text")
    message["caption"] = "photo caption"
    message["caption_entities"] = [{"type": "bold", "offset": 0, "length": 5}]

    msg = parse_update(update)

    assert msg is not None
    assert msg.text == "photo caption"
    assert msg.entities == message["caption_entities"]


def test_parse_bounds_main_and_reply_text_before_persistence():
    update = make_update(text="x" * (MAX_HISTORY_TEXT_CHARS + 10), reply_to_bot=True)
    update["message"]["reply_to_message"]["text"] = "y" * (MAX_HISTORY_TEXT_CHARS + 10)

    msg = parse_update(update)

    assert msg is not None
    assert msg.text == "x" * MAX_HISTORY_TEXT_CHARS
    assert msg.reply_to_text == "y" * MAX_HISTORY_TEXT_CHARS


def test_to_history_record_shape():
    update = make_update(text="rec")
    update["message"]["date"] = 1_784_200_000
    msg = parse_update(update)
    assert msg is not None
    rec = to_history_record(msg)
    assert set(rec) == {
        "message_id",
        "source_update_id",
        "user_id",
        "username",
        "name",
        "text",
        "ts",
        "edit_ts",
        "is_edited",
        "is_bot",
        "reply_to",
    }
    assert rec["text"] == "rec"
    assert rec["source_update_id"] == 1
    assert rec["ts"] == 1_784_200_000
    assert rec["edit_ts"] is None
    assert rec["reply_to"] is None


def test_history_record_retains_minimal_reply_context():
    update = make_update(reply_to_bot=True)
    update["message"]["reply_to_message"]["text"] = "quoted"
    msg = parse_update(update)
    assert msg is not None

    assert to_history_record(msg)["reply_to"] == {
        "message_id": 9,
        "user_id": 999,
        "is_bot": True,
        "text": "quoted",
    }


def test_to_observed_user():
    update = make_update()
    update["message"]["date"] = 123
    msg = parse_update(update)
    assert msg is not None
    assert to_observed_user(msg) == {
        "id": 5,
        "username": "alice",
        "name": "Alice",
        "is_bot": False,
        "last_seen_at": 123,
        "last_update_id": 1,
    }


def test_parse_command():
    assert parse_command("/ping") == "ping"
    assert parse_command("/ping@MyBot", "mybot") == "ping"
    assert parse_command("/ping@MYBOT", "@mybot") == "ping"
    assert parse_command("/ping@OtherBot", "mybot") is None
    assert parse_command("/ping@MyBot") is None
    assert parse_command("/ping now") == "ping"
    assert parse_command("/JUDGE") == "judge"
    assert parse_command("hello") is None
    assert parse_command("") is None
    assert parse_command("/") is None


def test_send_message_returns_normalized_result(monkeypatch):
    captured = {}

    def fake_call(method, payload):
        captured.update({"method": method, "payload": payload})
        return {"message_id": 77, "chat": {"id": 100}, "text": "hello"}

    monkeypatch.setattr(telegram_client, "call", fake_call)

    result = telegram_client.send_message(100, "hello", reply_to_message_id=5)

    assert result["message_id"] == 77
    assert captured == {
        "method": "sendMessage",
        "payload": {
            "chat_id": 100,
            "text": "hello",
            "reply_parameters": {"message_id": 5},
        },
    }


def test_call_unwraps_telegram_result_and_checks_ok(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"message_id": 77}}

    class FakeHTTP:
        def post(self, _url, json):
            assert json == {"chat_id": 100}
            return FakeResponse()

    monkeypatch.setattr(telegram_client, "_http", lambda: FakeHTTP())
    assert telegram_client.call("sendMessage", {"chat_id": 100}) == {"message_id": 77}


def test_call_rejects_telegram_api_error(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "description": "Bad Request"}

    class FakeHTTP:
        def post(self, _url, json):
            return FakeResponse()

    monkeypatch.setattr(telegram_client, "_http", lambda: FakeHTTP())
    with pytest.raises(
        telegram_client.TelegramAPIError, match="rejected request"
    ) as exc:
        telegram_client.call("sendMessage", {})
    assert exc.value.description == "Bad Request"


def test_call_redacts_token_bearing_transport_url(monkeypatch):
    secret = "VERY_SECRET_BOT_TOKEN"

    class FakeHTTP:
        def post(self, url, json):
            request = httpx.Request("POST", url)
            raise httpx.ConnectError("network failed", request=request)

    monkeypatch.setattr(telegram_client.settings, "TELEGRAM_BOT_TOKEN", secret)
    monkeypatch.setattr(telegram_client, "_http", lambda: FakeHTTP())

    with pytest.raises(telegram_client.TelegramAPIError) as exc:
        telegram_client.call("sendMessage", {})

    assert secret not in str(exc.value)


def test_send_message_rejects_non_message_result(monkeypatch):
    monkeypatch.setattr(telegram_client, "call", lambda *_args, **_kwargs: True)
    with pytest.raises(telegram_client.TelegramAPIError) as raised:
        telegram_client.send_message(100, "hello")
    assert raised.value.outcome_unknown is True


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Bad Request: message is not modified", True),
        (
            "Bad Request: message is not modified: specified new message content "
            "and reply markup are exactly the same as a current content and reply "
            "markup of the message",
            True,
        ),
        ("Bad Request: message cannot be edited", False),
    ],
)
def test_message_not_modified_accepts_only_the_known_telegram_description_prefix(
    description: str, expected: bool
):
    error = telegram_client.TelegramAPIError(
        "safe error",
        method="editMessageText",
        status_code=400,
        description=description,
    )

    assert error.message_not_modified is expected


def test_edit_message_text_uses_plain_text_without_parse_mode(monkeypatch):
    captured = {}

    def fake_call(method, payload):
        captured.update(method=method, payload=payload)
        return {"message_id": 77}

    monkeypatch.setattr(telegram_client, "call", fake_call)

    telegram_client.edit_message_text(100, 77, "<b>plain</b>")

    assert captured == {
        "method": "editMessageText",
        "payload": {"chat_id": 100, "message_id": 77, "text": "<b>plain</b>"},
    }


@pytest.mark.parametrize(
    ("length", "sizes"),
    [
        (0, []),
        (1, [1]),
        (3_999, [3_999]),
        (4_000, [4_000]),
        (4_001, [4_000, 1]),
        (8_000, [4_000, 4_000]),
        (8_001, [4_000, 4_000, 1]),
    ],
)
def test_plain_text_split_boundaries(length, sizes):
    chunks = telegram_client.split_plain_text("🙂" * length)
    assert [len(chunk) for chunk in chunks] == sizes
    assert "".join(chunks) == "🙂" * length


def test_telegram_error_classification_and_exact_not_modified():
    retryable = telegram_client.TelegramAPIError(
        "sanitized", method="sendMessage", status_code=429
    )
    transport = telegram_client.TelegramAPIError(
        "sanitized", method="sendMessage", transport_error=True
    )
    not_modified = telegram_client.TelegramAPIError(
        "sanitized",
        method="editMessageText",
        status_code=400,
        description="Bad Request: message is not modified",
    )
    wrong_method = telegram_client.TelegramAPIError(
        "sanitized",
        method="sendMessage",
        status_code=400,
        description="Bad Request: message is not modified",
    )

    assert retryable.retryable is True
    assert transport.retryable is True
    assert not_modified.message_not_modified is True
    assert wrong_method.message_not_modified is False


def test_telegram_timeout_and_pre_send_transport_are_retryable():
    timeout = telegram_client.TelegramAPIError(
        "sanitized", method="sendMessage", status_code=408
    )
    pre_send = telegram_client.TelegramAPIError(
        "sanitized",
        method="sendMessage",
        transport_error=True,
        outcome_unknown=False,
    )

    assert timeout.retryable is True
    assert timeout.outcome_unknown is False
    assert pre_send.retryable is True
    assert pre_send.outcome_unknown is False


def test_outbound_history_record_normalizes_successful_bot_message():
    result = {
        "message_id": 77,
        "date": 1_784_200_100,
        "chat": {"id": 100},
        "from": {
            "id": 999,
            "is_bot": True,
            "first_name": "Test",
            "username": "test_bot",
        },
        "text": "Thinking…",
    }

    record = telegram_client.outbound_history_record(
        result,
        source_update_id=123,
        fallback_chat_id=100,
        fallback_user_id=999,
    )

    assert record["message_id"] == 77
    assert record["source_update_id"] == 123
    assert record["user_id"] == 999
    assert record["text"] == "Thinking…"
    assert record["is_bot"] is True
