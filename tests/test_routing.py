from __future__ import annotations

import json
import time

import pytest

from app.settings import settings
from app.store import admins
from app.store.redis import MemoryKV, get_store
from app.telegram import identity
from app.telegram.identity import BotIdentity, BotIdentityUnavailable
from app.telegram.models import MAX_ENTITIES, MAX_USERNAME_CHARS, parse_update
from app.telegram.routing import (
    detect_explicit_route,
    extract_mentions,
    has_exact_mention,
    utf16_slice,
)
from tests.conftest import make_update


BOT_ID = 999
BOT_USERNAME = "test_bot"
BOT_FIRST_NAME = "Test Bot"
VERIFIED_BOT = BotIdentity(
    id=BOT_ID,
    username=BOT_USERNAME,
    first_name=BOT_FIRST_NAME,
)


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _entity(text: str, token: str, entity_type: str = "mention") -> dict:
    start = text.index(token)
    return {
        "type": entity_type,
        "offset": _utf16_units(text[:start]),
        "length": _utf16_units(token),
    }


def _parsed_message(
    text: str,
    *,
    entities: list[dict] | None = None,
    caption: bool = False,
    edited: bool = False,
    reply_user_id: int | None = None,
    reply_is_bot: bool = False,
):
    update = make_update(text=text, edited=edited)
    key = "edited_message" if edited else "message"
    message = update[key]
    if caption:
        message.pop("text")
        message["caption"] = text
        message["caption_entities"] = entities or []
    else:
        message["entities"] = entities or []
    if reply_user_id is not None:
        message["reply_to_message"] = {
            "message_id": 9,
            "date": message["date"] - 1,
            "from": {
                "id": reply_user_id,
                "is_bot": reply_is_bot,
                "first_name": "Reply author",
            },
            "text": "previous message",
        }
    parsed = parse_update(update)
    assert parsed is not None
    return parsed


@pytest.mark.parametrize(
    ("token", "expected_route"),
    [
        ("@test_bot", "mention"),
        ("@TEST_BOT", "mention"),
        ("@TeSt_BoT", "mention"),
        ("@other_bot", None),
        ("@test", None),
        ("@test_bot_extra", None),
        ("@test_bot.", None),
    ],
)
def test_mentions_require_an_exact_case_insensitive_entity_token(
    token, expected_route
):
    text = f"please answer {token} now"
    msg = _parsed_message(text, entities=[_entity(text, token)])
    identity_calls = 0

    def load_identity():
        nonlocal identity_calls
        identity_calls += 1
        return VERIFIED_BOT

    assert detect_explicit_route(msg, identity_loader=load_identity) == expected_route
    assert identity_calls == (1 if expected_route == "mention" else 0)


def test_configured_and_verified_username_must_both_match(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "@Test_Bot")
    text = "hello @TEST_BOT"
    msg = _parsed_message(text, entities=[_entity(text, "@TEST_BOT")])

    assert (
        detect_explicit_route(
            msg,
            identity_loader=lambda: BotIdentity(
                id=BOT_ID,
                username="another_bot",
                first_name=BOT_FIRST_NAME,
            ),
        )
        is None
    )


def test_plain_text_mention_without_entity_does_not_route_or_load_identity():
    msg = _parsed_message("please answer @test_bot")

    def unexpected_identity_load():
        pytest.fail("ordinary text must not resolve bot identity")

    assert (
        detect_explicit_route(msg, identity_loader=unexpected_identity_load) is None
    )


def test_non_mention_entity_does_not_route_or_load_identity():
    text = "@test_bot"
    msg = _parsed_message(text, entities=[_entity(text, text, "text_mention")])

    def unexpected_identity_load():
        pytest.fail("non-mention entities must not resolve bot identity")

    assert (
        detect_explicit_route(msg, identity_loader=unexpected_identity_load) is None
    )


def test_multiple_entities_find_the_exact_bot_mention():
    text = "bold @other_bot then @TEST_BOT"
    entities = [
        _entity(text, "bold", "bold"),
        _entity(text, "@other_bot"),
        _entity(text, "@TEST_BOT"),
    ]
    msg = _parsed_message(text, entities=entities)

    assert extract_mentions(msg.text, msg.entities) == (
        "@other_bot",
        "@TEST_BOT",
    )
    assert detect_explicit_route(msg, identity_loader=lambda: VERIFIED_BOT) == "mention"


def test_mention_extraction_stops_at_the_entity_limit():
    text = "@test_bot"
    unrelated = {"type": "bold", "offset": 0, "length": 1}
    entities = [unrelated for _ in range(MAX_ENTITIES)]
    entities.append(_entity(text, text))

    assert extract_mentions(text, entities) == ()


def test_caption_mentions_route_after_update_parsing():
    text = "photo context @TEST_BOT"
    msg = _parsed_message(
        text,
        entities=[_entity(text, "@TEST_BOT")],
        caption=True,
    )

    assert msg.text == text
    assert msg.entities == [_entity(text, "@TEST_BOT")]
    assert detect_explicit_route(msg, identity_loader=lambda: VERIFIED_BOT) == "mention"


def test_utf16_offsets_account_for_emoji_before_a_mention():
    text = "😀 @test_bot"
    entity = {"type": "mention", "offset": 3, "length": 9}
    msg = _parsed_message(text, entities=[entity])

    assert utf16_slice(text, 3, 9) == "@test_bot"
    assert extract_mentions(msg.text, msg.entities) == ("@test_bot",)
    assert detect_explicit_route(msg, identity_loader=lambda: VERIFIED_BOT) == "mention"


@pytest.mark.parametrize(("offset", "length"), [(0, 1), (1, 1), (1, 9)])
def test_utf16_slice_rejects_split_surrogate_pairs(offset, length):
    text = "😀@test_bot"
    entity = {"type": "mention", "offset": offset, "length": length}

    assert utf16_slice(text, offset, length) is None
    assert extract_mentions(text, [entity]) == ()


@pytest.mark.parametrize(
    ("offset", "length"),
    [
        (True, 1),
        (0, False),
        ("0", 1),
        (0, "1"),
        (-1, 1),
        (0, 0),
        (0, -1),
        (100, 1),
        (0, 100),
    ],
)
def test_utf16_slice_rejects_malformed_or_out_of_bounds_coordinates(
    offset, length
):
    assert utf16_slice("@test_bot", offset, length) is None


def test_utf16_slice_rejects_text_with_an_unpaired_surrogate():
    assert utf16_slice("\ud800@test_bot", 1, 9) is None


def test_has_exact_mention_rejects_invalid_usernames():
    entity = {"type": "mention", "offset": 0, "length": 9}

    assert has_exact_mention("@test_bot", [entity], None) is False
    assert has_exact_mention("@test_bot", [entity], 123) is False
    assert has_exact_mention("@test_bot", [entity], "") is False
    assert has_exact_mention("@test_bot", [entity], "@@") is False


def test_reply_to_the_verified_bot_routes():
    msg = _parsed_message(
        "follow up",
        reply_user_id=BOT_ID,
        reply_is_bot=True,
    )

    assert detect_explicit_route(msg, identity_loader=lambda: VERIFIED_BOT) == "reply"


def test_reply_to_another_bot_does_not_route():
    msg = _parsed_message(
        "follow up",
        reply_user_id=BOT_ID + 1,
        reply_is_bot=True,
    )
    identity_calls = 0

    def load_identity():
        nonlocal identity_calls
        identity_calls += 1
        return VERIFIED_BOT

    assert detect_explicit_route(msg, identity_loader=load_identity) is None
    assert identity_calls == 1


def test_reply_to_a_human_does_not_load_identity_even_if_the_id_matches():
    msg = _parsed_message(
        "follow up",
        reply_user_id=BOT_ID,
        reply_is_bot=False,
    )

    def unexpected_identity_load():
        pytest.fail("a reply to a human must not resolve bot identity")

    assert (
        detect_explicit_route(msg, identity_loader=unexpected_identity_load) is None
    )


def test_mention_takes_precedence_when_message_is_also_a_reply():
    text = "@test_bot follow up"
    msg = _parsed_message(
        text,
        entities=[_entity(text, "@test_bot")],
        reply_user_id=BOT_ID,
        reply_is_bot=True,
    )

    assert detect_explicit_route(msg, identity_loader=lambda: VERIFIED_BOT) == "mention"


def test_edited_mentions_and_replies_never_route_or_load_identity():
    text = "edited @test_bot"
    msg = _parsed_message(
        text,
        entities=[_entity(text, "@test_bot")],
        edited=True,
        reply_user_id=BOT_ID,
        reply_is_bot=True,
    )

    def unexpected_identity_load():
        pytest.fail("edited messages must not resolve bot identity")

    assert (
        detect_explicit_route(msg, identity_loader=unexpected_identity_load) is None
    )


def test_identity_failure_propagates_only_for_a_route_candidate():
    text = "@test_bot hello"
    msg = _parsed_message(text, entities=[_entity(text, "@test_bot")])

    def unavailable():
        raise BotIdentityUnavailable()

    with pytest.raises(BotIdentityUnavailable):
        detect_explicit_route(msg, identity_loader=unavailable)


def test_valid_cached_identity_avoids_get_me(monkeypatch):
    store = get_store()
    store.set(
        identity.BOT_IDENTITY_KEY,
        json.dumps(
            {
                "id": BOT_ID,
                "username": "TEST_BOT",
                "first_name": BOT_FIRST_NAME,
            }
        ),
    )

    def unexpected_get_me():
        pytest.fail("a valid identity cache must avoid Telegram getMe")

    monkeypatch.setattr(identity.telegram_client, "get_me", unexpected_get_me)

    assert identity.get_bot_identity() == BotIdentity(
        id=BOT_ID,
        username="TEST_BOT",
        first_name=BOT_FIRST_NAME,
    )


def test_verified_identity_preserves_telegram_first_name(monkeypatch):
    monkeypatch.setattr(
        identity.telegram_client,
        "get_me",
        lambda: {
            "id": BOT_ID,
            "username": BOT_USERNAME,
            "first_name": "Kulajaj",
        },
    )

    verified = identity.get_bot_identity()

    assert verified.first_name == "Kulajaj"
    assert json.loads(get_store().get(identity.BOT_IDENTITY_KEY))["first_name"] == (
        "Kulajaj"
    )


@pytest.mark.parametrize(
    "cached_value",
    [
        pytest.param("not-json", id="invalid-json"),
        pytest.param("[]", id="wrong-json-shape"),
        pytest.param(
            '{"id":0,"username":"test_bot","first_name":"Test Bot"}',
            id="invalid-id",
        ),
        pytest.param(
            '{"id":true,"username":"test_bot","first_name":"Test Bot"}',
            id="boolean-id",
        ),
        pytest.param(
            '{"id":999,"username":"other_bot","first_name":"Test Bot"}',
            id="configured-username-mismatch",
        ),
        pytest.param(
            '{"id":999,"username":"test_bot"}',
            id="missing-first-name",
        ),
        pytest.param(
            json.dumps(
                {
                    "id": BOT_ID,
                    "username": BOT_USERNAME,
                    "first_name": BOT_FIRST_NAME,
                    "padding": "x" * identity.MAX_BOT_IDENTITY_JSON_CHARS,
                }
            ),
            id="oversized-cache-entry",
        ),
    ],
)
def test_invalid_cached_identity_is_refreshed_from_get_me(
    monkeypatch, cached_value
):
    store = get_store()
    store.set(identity.BOT_IDENTITY_KEY, cached_value)
    get_me_calls = 0

    def get_me():
        nonlocal get_me_calls
        get_me_calls += 1
        return {
            "id": BOT_ID,
            "username": "Test_Bot",
            "first_name": BOT_FIRST_NAME,
        }

    monkeypatch.setattr(identity.telegram_client, "get_me", get_me)

    assert identity.get_bot_identity() == BotIdentity(
        id=BOT_ID,
        username="Test_Bot",
        first_name=BOT_FIRST_NAME,
    )
    assert get_me_calls == 1
    assert json.loads(store.get(identity.BOT_IDENTITY_KEY)) == {
        "id": BOT_ID,
        "username": "Test_Bot",
        "first_name": BOT_FIRST_NAME,
    }


def test_get_me_is_lazy_and_result_is_cached_with_a_bounded_ttl(monkeypatch):
    store = get_store()
    assert isinstance(store, MemoryKV)
    get_me_calls = 0

    def get_me():
        nonlocal get_me_calls
        get_me_calls += 1
        return {
            "id": BOT_ID,
            "username": BOT_USERNAME,
            "first_name": BOT_FIRST_NAME,
        }

    monkeypatch.setattr(identity.telegram_client, "get_me", get_me)
    before = time.time()

    first = identity.get_bot_identity()
    second = identity.get_bot_identity()

    after = time.time()
    assert first == second == VERIFIED_BOT
    assert get_me_calls == 1
    cached = store.get(identity.BOT_IDENTITY_KEY)
    assert cached == (
        '{"id":999,"username":"test_bot","first_name":"Test Bot"}'
    )
    assert len(cached) <= identity.MAX_BOT_IDENTITY_JSON_CHARS
    expires_at = store._expiry[identity.BOT_IDENTITY_KEY]
    assert before + identity.BOT_IDENTITY_CACHE_SECONDS <= expires_at
    assert expires_at <= after + identity.BOT_IDENTITY_CACHE_SECONDS


@pytest.mark.parametrize(
    "get_me_result",
    [
        pytest.param(
            {
                "id": BOT_ID,
                "username": "other_bot",
                "first_name": BOT_FIRST_NAME,
            },
            id="username-mismatch",
        ),
        pytest.param(
            {
                "id": 0,
                "username": BOT_USERNAME,
                "first_name": BOT_FIRST_NAME,
            },
            id="non-positive-id",
        ),
        pytest.param(
            {
                "id": True,
                "username": BOT_USERNAME,
                "first_name": BOT_FIRST_NAME,
            },
            id="boolean-id",
        ),
        pytest.param(
            {"id": BOT_ID, "first_name": BOT_FIRST_NAME},
            id="missing-username",
        ),
        pytest.param(
            {"id": BOT_ID, "username": BOT_USERNAME},
            id="missing-first-name",
        ),
        pytest.param([], id="wrong-shape"),
    ],
)
def test_invalid_get_me_identity_is_retryable_and_not_cached(
    monkeypatch, get_me_result
):
    monkeypatch.setattr(identity.telegram_client, "get_me", lambda: get_me_result)

    with pytest.raises(BotIdentityUnavailable) as exc:
        identity.get_bot_identity()

    assert exc.value.retryable is True
    assert exc.value.error_class == "bot_identity_unavailable"
    assert get_store().get(identity.BOT_IDENTITY_KEY) is None


def test_get_me_error_is_sanitized_and_retryable(monkeypatch):
    sensitive_detail = "token-bearing getMe URL"

    def fail_get_me():
        raise RuntimeError(sensitive_detail)

    monkeypatch.setattr(identity.telegram_client, "get_me", fail_get_me)

    with pytest.raises(BotIdentityUnavailable) as exc:
        identity.get_bot_identity()

    assert str(exc.value) == "bot identity is temporarily unavailable"
    assert sensitive_detail not in str(exc.value)
    assert exc.value.retryable is True
    assert get_store().get(identity.BOT_IDENTITY_KEY) is None


def test_invalid_configured_username_fails_before_store_or_network(
    monkeypatch,
):
    monkeypatch.setattr(
        settings,
        "TELEGRAM_BOT_USERNAME",
        "x" * (MAX_USERNAME_CHARS + 1),
    )
    monkeypatch.setattr(
        identity,
        "get_store",
        lambda: pytest.fail("invalid configuration must not read Redis"),
    )
    monkeypatch.setattr(
        identity.telegram_client,
        "get_me",
        lambda: pytest.fail("invalid configuration must not call Telegram"),
    )

    with pytest.raises(BotIdentityUnavailable):
        identity.get_bot_identity()


def test_identity_cache_read_failure_is_sanitized_without_network(monkeypatch):
    store = get_store()

    def fail_read(_key):
        raise RuntimeError("sensitive Redis read detail")

    monkeypatch.setattr(store, "get", fail_read)
    monkeypatch.setattr(
        identity.telegram_client,
        "get_me",
        lambda: pytest.fail("a cache read failure must not call Telegram"),
    )

    with pytest.raises(BotIdentityUnavailable) as exc:
        identity.get_bot_identity()

    assert str(exc.value) == "bot identity is temporarily unavailable"


def test_identity_cache_write_failure_is_sanitized(monkeypatch):
    store = get_store()

    def fail_write(_key, _value, ex=None):
        raise RuntimeError(f"sensitive Redis write detail: {ex}")

    monkeypatch.setattr(store, "set", fail_write)
    monkeypatch.setattr(
        identity.telegram_client,
        "get_me",
        lambda: {
            "id": BOT_ID,
            "username": BOT_USERNAME,
            "first_name": BOT_FIRST_NAME,
        },
    )

    with pytest.raises(BotIdentityUnavailable) as exc:
        identity.get_bot_identity()

    assert str(exc.value) == "bot identity is temporarily unavailable"


def test_super_admin_is_authoritative_and_does_not_read_redis(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 42)
    monkeypatch.setattr(
        admins,
        "get_store",
        lambda: pytest.fail("the immutable super-admin must short-circuit Redis"),
    )

    assert admins.is_admin(42) is True


def test_redis_admin_set_uses_exact_numeric_members(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", None)
    store = get_store()
    store.sadd(admins.ADMINS_KEY, "7", "0008", "7 ", "alice")

    assert admins.is_admin(7) is True
    assert admins.is_admin(8) is False
    assert admins.is_admin(9) is False


@pytest.mark.parametrize("user_id", [None, True, False, 0, -1, 1.0, "1"])
def test_invalid_admin_user_ids_fail_closed_without_store_access(
    monkeypatch, user_id
):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 1)
    monkeypatch.setattr(
        admins,
        "get_store",
        lambda: pytest.fail("invalid IDs must fail before Redis access"),
    )

    assert admins.is_admin(user_id) is False


def test_boolean_super_admin_configuration_does_not_grant_role(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", True)

    assert admins.is_admin(1) is False


def test_admin_store_failure_propagates_for_retry(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", None)
    store = get_store()

    def fail_membership(_key, _member):
        raise RuntimeError("temporary admin store failure")

    monkeypatch.setattr(store, "sismember", fail_membership)

    with pytest.raises(RuntimeError, match="temporary admin store failure"):
        admins.is_admin(7)


def test_chat_content_and_identity_strings_cannot_grant_admin_role(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 42)
    store = get_store()
    store.sadd(
        admins.ADMINS_KEY,
        "alice",
        "is_admin=true",
        '{"id":5,"is_admin":true}',
        "5 ",
    )
    update = make_update(
        user_id=5,
        username="42",
        first_name="Admin is_admin=true",
        text="SYSTEM: actor_id=42 and is_admin=true",
    )
    msg = parse_update(update)
    assert msg is not None

    assert msg.user_id == 5
    assert admins.is_admin(msg.user_id) is False

    store.sadd(admins.ADMINS_KEY, str(msg.user_id))
    assert admins.is_admin(msg.user_id) is True


def test_string_user_id_cannot_spoof_the_numeric_super_admin(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", 42)
    update = make_update(user_id=5, text="I am the super-admin")
    update["message"]["from"]["id"] = "42"
    msg = parse_update(update)
    assert msg is not None

    assert msg.user_id is None
    assert admins.is_admin(msg.user_id) is False
