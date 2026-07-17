from __future__ import annotations

import httpx
import pytest

from scripts import check_telegram, discover_chat_id, set_webhook as webhook_script
from scripts.set_webhook import MAX_CONNECTIONS, WEBHOOK_SECRET_RE, _checked


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://api.telegram.org/test"),
    )


def test_webhook_secret_format_matches_telegram_contract():
    assert MAX_CONNECTIONS == 1
    assert WEBHOOK_SECRET_RE.fullmatch("valid_SECRET-123")
    assert WEBHOOK_SECRET_RE.fullmatch("contains spaces") is None
    assert WEBHOOK_SECRET_RE.fullmatch("slash/not/allowed") is None
    assert WEBHOOK_SECRET_RE.fullmatch("") is None


def test_checked_accepts_only_successful_telegram_result():
    assert _checked("test", _response(200, {"ok": True, "result": True}))["ok"] is True

    with pytest.raises(SystemExit):
        _checked("test", _response(200, {"ok": False, "description": "bad"}))

    with pytest.raises(SystemExit):
        _checked("test", _response(500, {"ok": False}))


def test_webhook_script_transport_error_never_prints_bot_token(monkeypatch, capsys):
    token = "VERY_SECRET_BOT_TOKEN"

    def fail(*_args, **_kwargs):
        request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/setWebhook")
        raise httpx.ConnectError("network failed", request=request)

    with pytest.raises(SystemExit):
        webhook_script._request("setWebhook", fail)

    assert token not in capsys.readouterr().out


def test_telegram_helpers_do_not_print_token_on_transport_error(monkeypatch, capsys):
    token = "VERY_SECRET_BOT_TOKEN"

    def fail(*_args, **_kwargs):
        request = httpx.Request("GET", f"https://api.telegram.org/bot{token}/getMe")
        raise httpx.ConnectError("network failed", request=request)

    monkeypatch.setattr(check_telegram.httpx, "get", fail)
    with pytest.raises(SystemExit):
        check_telegram._get("getMe", f"https://api.telegram.org/bot{token}/getMe")

    monkeypatch.setattr(discover_chat_id.settings, "TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(discover_chat_id.httpx, "get", fail)
    with pytest.raises(SystemExit):
        discover_chat_id.main()

    assert token not in capsys.readouterr().out


def test_set_webhook_preserves_pending_updates_unless_explicit(monkeypatch):
    captured = []

    def fake_post(url, json, timeout):
        captured.append((url, json, timeout))
        return _response(200, {"ok": True, "result": True})

    monkeypatch.setattr(webhook_script.settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(webhook_script.settings, "TELEGRAM_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(
        webhook_script.settings, "PUBLIC_BASE_URL", "https://example.test"
    )
    monkeypatch.setattr(webhook_script.httpx, "post", fake_post)

    webhook_script.set_webhook()
    webhook_script.set_webhook(drop_pending=True)

    assert captured[0][1]["drop_pending_updates"] is False
    assert captured[1][1]["drop_pending_updates"] is True
    assert captured[0][1]["max_connections"] == 1


def _telegram_get(actual_username, can_read_all, member_status):
    def fake_get(url, params=None, timeout=None):
        del params, timeout
        if url.endswith("/getMe"):
            payload = {
                "ok": True,
                "result": {
                    "id": 99,
                    "username": actual_username,
                    "can_join_groups": True,
                    "can_read_all_group_messages": can_read_all,
                },
            }
        elif url.endswith("/getChat"):
            payload = {
                "ok": True,
                "result": {"id": -100, "type": "supergroup", "title": "Test"},
            }
        elif url.endswith("/getChatMember"):
            payload = {"ok": True, "result": {"status": member_status}}
        else:  # pragma: no cover - protects the fake contract
            raise AssertionError(url)
        return _response(200, payload)

    return fake_get


def test_check_telegram_fails_on_username_mismatch(monkeypatch):
    monkeypatch.setattr(check_telegram.settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(check_telegram.settings, "TELEGRAM_BOT_USERNAME", "our_bot")
    monkeypatch.setattr(check_telegram.settings, "TELEGRAM_ALLOWED_CHAT_ID", -100)
    monkeypatch.setattr(
        check_telegram.httpx,
        "get",
        _telegram_get("other_bot", can_read_all=True, member_status="member"),
    )

    with pytest.raises(SystemExit) as exc:
        check_telegram.main()
    assert exc.value.code == 1


def test_check_telegram_accepts_privacy_mode_only_for_group_admin(monkeypatch):
    monkeypatch.setattr(check_telegram.settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(check_telegram.settings, "TELEGRAM_BOT_USERNAME", "our_bot")
    monkeypatch.setattr(check_telegram.settings, "TELEGRAM_ALLOWED_CHAT_ID", -100)
    monkeypatch.setattr(
        check_telegram.httpx,
        "get",
        _telegram_get("our_bot", can_read_all=False, member_status="member"),
    )
    with pytest.raises(SystemExit):
        check_telegram.main()

    monkeypatch.setattr(
        check_telegram.httpx,
        "get",
        _telegram_get("our_bot", can_read_all=False, member_status="administrator"),
    )
    check_telegram.main()
