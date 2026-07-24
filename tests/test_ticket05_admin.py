from __future__ import annotations

import time
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from app.admin import routes as admin_routes
from app.auth import session
from app.auth.telegram_oidc import consume_state, create_state
from app.auth.membership import require_group_member
from app.settings import settings
from app.search.tavily import normalize_explicit_query
from app.store import config_store, history, lists, users
from app.store import admins
from app.store.redis import get_store
from app.store.jobs import get_job_repository, user_index_key


def _admin_headers(user_id: int) -> dict[str, str]:
    token, csrf = session.issue_session(user_id)
    return {
        "Cookie": f"{session.SESSION_COOKIE}={token}",
        "Origin": settings.PUBLIC_BASE_URL,
        "X-CSRF-Token": csrf,
    }


def _configure_admin(monkeypatch: pytest.MonkeyPatch, user_id: int = 101) -> None:
    monkeypatch.setattr(settings, "SUPER_ADMIN_ID", user_id)
    monkeypatch.setattr(settings, "SESSION_SECRET", "0123456789abcdef" * 3)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://admin.example")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", -100123)
    monkeypatch.setattr(settings, "TELEGRAM_OIDC_CLIENT_ID", "123456")
    monkeypatch.setattr(settings, "TELEGRAM_OIDC_CLIENT_SECRET", "oidc-secret")


def test_oidc_state_handles_https_redirect_and_is_one_time(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_OIDC_CLIENT_ID", "123")
    redirect_uri = "https://admin.example/api/auth/telegram/callback"
    state, handle, _challenge, nonce = create_state(redirect_uri)

    with pytest.raises(ValueError):
        consume_state(state, "wrong-browser", redirect_uri)

    verifier, stored_redirect, stored_nonce = consume_state(
        state, handle, redirect_uri
    )
    assert verifier
    assert stored_redirect == redirect_uri
    assert stored_nonce == nonce
    with pytest.raises(ValueError):
        consume_state(state, handle, redirect_uri)


def test_oidc_state_expires_and_concurrent_consumption_has_one_winner(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_OIDC_CLIENT_ID", "123")
    redirect_uri = "https://admin.example/api/auth/telegram/callback"
    expired_state, _handle, _challenge, _nonce = create_state(redirect_uri)
    expired_key = f"auth:state:{hashlib.sha256(expired_state.encode()).hexdigest()}"
    store = get_store()
    store._expiry[expired_key] = time.time() - 1
    with pytest.raises(ValueError):
        consume_state(expired_state, "anything", redirect_uri)

    state, handle, _challenge, _nonce = create_state(redirect_uri)

    def consume() -> bool:
        try:
            consume_state(state, handle, redirect_uri)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: consume(), range(8)))
    assert results.count(True) == 1


def test_oidc_swapped_browser_bindings_do_not_consume_valid_transactions(
    monkeypatch,
):
    monkeypatch.setattr(settings, "TELEGRAM_OIDC_CLIENT_ID", "123")
    redirect_uri = "https://admin.example/api/auth/telegram/callback"
    state_a, handle_a, _challenge_a, _nonce_a = create_state(redirect_uri)
    state_b, handle_b, _challenge_b, _nonce_b = create_state(redirect_uri)

    with pytest.raises(ValueError):
        consume_state(state_a, handle_b, redirect_uri)
    with pytest.raises(ValueError):
        consume_state(state_b, handle_a, redirect_uri)

    assert consume_state(state_a, handle_a, redirect_uri)[0]
    assert consume_state(state_b, handle_b, redirect_uri)[0]


def _oidc_token(private_key, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://oauth.telegram.org",
        "aud": settings.TELEGRAM_OIDC_CLIENT_ID,
        "sub": "telegram-subject-101",
        "id": 101,
        "nonce": "expected-nonce",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def test_oidc_jwks_validation_uses_verified_numeric_id_and_supports_rotation(
    monkeypatch,
):
    _configure_admin(monkeypatch)
    keys = {
        "first": rsa.generate_private_key(public_exponent=65_537, key_size=2_048),
        "second": rsa.generate_private_key(public_exponent=65_537, key_size=2_048),
    }

    class RotatingJwks:
        def get_signing_key_from_jwt(self, token):
            kid = jwt.get_unverified_header(token)["kid"]
            return SimpleNamespace(key=keys[kid].public_key())

    monkeypatch.setattr(admin_routes, "_jwks", lambda: RotatingJwks())
    now = int(time.time())
    base = {
        "iss": "https://oauth.telegram.org",
        "aud": settings.TELEGRAM_OIDC_CLIENT_ID,
        "sub": "opaque-telegram-subject",
        "id": 101,
        "nonce": "expected-nonce",
        "iat": now,
        "exp": now + 300,
    }
    for kid, private_key in keys.items():
        token = jwt.encode(
            base,
            private_key,
            algorithm="RS256",
            headers={"kid": kid},
        )
        assert admin_routes._decode_identity(token, "expected-nonce") == 101


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.example"},
        {"aud": "wrong-client"},
        {"nonce": "wrong-nonce"},
        {"iat": int(time.time()) + 120},
        {"exp": int(time.time()) - 60},
        {"id": True},
        {"id": "101"},
        {"sub": ""},
        {"sub": " leading-space"},
        {"sub": "trailing-space "},
        {"sub": "subject\nwith-control"},
        {"sub": "subject\u202ewith-bidi-control"},
        {"sub": "x" * 256},
    ],
)
def test_oidc_rejects_invalid_verified_claims(monkeypatch, overrides):
    _configure_admin(monkeypatch)
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    monkeypatch.setattr(
        admin_routes,
        "_jwks",
        lambda: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                key=private_key.public_key()
            )
        ),
    )
    with pytest.raises((ValueError, jwt.PyJWTError)):
        admin_routes._decode_identity(
            _oidc_token(private_key, **overrides), "expected-nonce"
        )


def test_oidc_rejects_wrong_signature_and_algorithm(monkeypatch):
    _configure_admin(monkeypatch)
    trusted = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    attacker = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    monkeypatch.setattr(
        admin_routes,
        "_jwks",
        lambda: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                key=trusted.public_key()
            )
        ),
    )
    with pytest.raises(jwt.PyJWTError):
        admin_routes._decode_identity(
            _oidc_token(attacker), "expected-nonce"
        )
    claims = {
        "iss": "https://oauth.telegram.org",
        "aud": settings.TELEGRAM_OIDC_CLIENT_ID,
        "sub": "subject",
        "id": 101,
        "nonce": "expected-nonce",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    wrong_algorithm = jwt.encode(claims, "a" * 64, algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        admin_routes._decode_identity(wrong_algorithm, "expected-nonce")


def test_public_config_is_an_exact_secret_free_shape(client, monkeypatch):
    _configure_admin(monkeypatch)
    response = client.get("/api/public/config")
    assert response.status_code == 200
    assert response.json() == {
        "telegram_bot_username": "test_bot",
        "oidc_client_id": "123456",
    }


def test_production_admin_routes_fail_closed_on_unsafe_session_secret(
    client, monkeypatch
):
    _configure_admin(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "SESSION_SECRET", "too-short")

    with pytest.raises(PermissionError):
        session.issue_session(101)
    assert client.get("/api/auth/telegram/start").status_code == 503
    assert client.get("/api/admin/me").status_code == 503


def test_mutations_require_same_origin_and_csrf(client, monkeypatch):
    _configure_admin(monkeypatch)
    headers = _admin_headers(101)
    no_origin = dict(headers)
    no_origin.pop("Origin")
    assert client.delete("/api/admin/rules/example", headers=no_origin).status_code == 403

    bad_csrf = dict(headers, **{"X-CSRF-Token": "wrong"})
    assert client.delete("/api/admin/rules/example", headers=bad_csrf).status_code == 403

    strict_referer = dict(headers)
    strict_referer.pop("Origin")
    strict_referer["Referer"] = f"{settings.PUBLIC_BASE_URL}/rules"
    assert (
        client.delete("/api/admin/rules/example", headers=strict_referer).status_code
        == 200
    )

    attacker_referer = dict(strict_referer)
    attacker_referer["Referer"] = "https://admin.example.evil/rules"
    assert (
        client.delete("/api/admin/rules/example", headers=attacker_referer).status_code
        == 403
    )


def test_admin_assignment_requires_current_group_member(client, monkeypatch):
    _configure_admin(monkeypatch)
    seen: list[int] = []

    def fake_member(user_id: int, *, seed_profile: bool = False) -> dict:
        seen.append(user_id)
        return {"id": user_id, "username": "new_admin", "name": "New Admin"}

    monkeypatch.setattr("app.admin.routes.require_group_member", fake_member)
    response = client.post(
        "/api/admin/admins",
        json={"user_id": 202},
        headers=_admin_headers(101),
    )
    assert response.status_code == 201
    assert response.json()["user_id"] == 202
    assert seen == [202]
    with pytest.raises(PermissionError, match="owner"):
        session.issue_session(202)


def test_admin_json_models_reject_unknown_fields(client, monkeypatch):
    _configure_admin(monkeypatch)
    response = client.post(
        "/api/admin/rules",
        json={
            "id": "test",
            "enabled": True,
            "priority": 1,
            "scope": "all",
            "match": {"type": "word", "value": "hello"},
            "instruction": "Be brief.",
            "stop_processing": False,
            "unexpected": "must fail",
        },
        headers=_admin_headers(101),
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/admin/admins", {"user_id": True}),
        (
            "/api/admin/lists",
            {
                "slug": 1,
                "title": "List",
                "enabled": True,
                "priority": 1,
                "applies_to": ["auto"],
                "injected_prompt": "Policy.",
            },
        ),
        (
            "/api/admin/lists",
            {
                "slug": "list",
                "title": "List",
                "enabled": "false",
                "priority": 1,
                "applies_to": ["auto"],
                "injected_prompt": "Policy.",
            },
        ),
        (
            "/api/admin/rules",
            {
                "id": "rule",
                "enabled": True,
                "priority": 1,
                "scope": "all",
                "match": {"type": "word", "value": "hello"},
                "instruction": "Reply.",
                "stop_processing": "false",
            },
        ),
    ],
)
def test_admin_models_reject_coercive_json_types(client, monkeypatch, path, payload):
    _configure_admin(monkeypatch)
    monkeypatch.setattr(
        "app.admin.routes.require_group_member",
        lambda *_args, **_kwargs: pytest.fail("invalid input reached membership"),
    )

    response = client.post(path, json=payload, headers=_admin_headers(101))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/rules/INVALID!",
        "/api/admin/lists/INVALID!/members/5",
    ],
)
def test_invalid_admin_path_identifiers_return_4xx(client, monkeypatch, path):
    _configure_admin(monkeypatch)

    response = client.delete(path, headers=_admin_headers(101))

    assert response.status_code == 422


def test_user_delete_removes_profile_memberships_jobs_and_private_history(
    client, monkeypatch
):
    _configure_admin(monkeypatch)
    target = 202
    users.observe(
        {
            "id": target,
            "username": "target",
            "name": "Target",
            "is_bot": False,
            "last_seen_at": int(time.time()),
            "last_update_id": 1,
        }
    )
    lists.create(
        {
            "slug": "test-list",
            "title": "Test",
            "enabled": True,
            "priority": 1,
            "applies_to": ["explicit"],
            "injected_prompt": "Test policy.",
        }
    )
    lists.add_member("test-list", target)
    history.upsert(
        -100123,
        {
            "message_id": 10,
            "source_update_id": 77,
            "user_id": target,
            "username": "target",
            "name": "Target",
            "text": "private text",
            "ts": int(time.time()),
            "edit_ts": None,
            "is_edited": False,
            "is_bot": False,
            "reply_to": None,
        },
    )
    repository = get_job_repository()
    repository.create_reply_job(
        {
            "update_id": 77,
            "chat_id": -100123,
            "author": {"id": target},
            "trigger_message_id": 10,
            "context": [],
        },
        {"actor": {"user_id": target, "is_admin": False}},
        [target],
        now=int(time.time()),
    )

    response = client.delete(
        f"/api/admin/users/{target}?purge_messages=true",
        headers=_admin_headers(101),
    )
    assert response.status_code == 200
    assert users.get(target) is None
    assert not lists.is_member("test-list", target)
    assert history.recent(-100123) == []
    assert repository.index_job_ids(user_index_key(target)) == []
    assert repository.get("77") is None


def test_user_delete_retry_keeps_outbound_purge_receipt_after_partial_failure(
    client, monkeypatch
):
    _configure_admin(monkeypatch)
    target = 202
    repository = get_job_repository()
    repository.create_reply_job(
        {
            "update_id": 78,
            "chat_id": -100123,
            "author": {"id": target},
            "trigger_message_id": 10,
            "context": [],
        },
        {"actor": {"user_id": target, "is_admin": False}},
        [target],
        now=int(time.time()),
    )
    acquired = repository.acquire(78, token="delete-race")
    assert acquired.lease is not None
    repository.prepare_intent(
        acquired.lease,
        name="placeholder",
        kind="sendMessage",
        chunk_index=-1,
        payload_hash="a" * 64,
        ambiguous_on_takeover=True,
    )
    repository.checkpoint(
        acquired.lease,
        name="placeholder",
        result={"message_id": 9_078},
    )
    history.upsert(
        -100123,
        {
            "message_id": 9_078,
            "source_update_id": 78,
            "user_id": 999,
            "name": "Bot",
            "text": "derived private answer",
            "ts": int(time.time()),
            "is_bot": True,
        },
    )
    real_purge = history.purge_user
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary history failure")
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(history, "purge_user", fail_once)
    first = client.delete(
        f"/api/admin/users/{target}?purge_messages=true",
        headers=_admin_headers(101),
    )
    retry = client.delete(
        f"/api/admin/users/{target}?purge_messages=true",
        headers=_admin_headers(101),
    )

    assert first.status_code == 503
    assert retry.status_code == 200
    assert repository.get(78) is None
    assert history.recent(-100123) == []


def test_super_admin_cannot_be_removed_or_deleted(client, monkeypatch):
    _configure_admin(monkeypatch)
    headers = _admin_headers(101)
    assert client.delete("/api/admin/admins/101", headers=headers).status_code == 422
    assert client.delete("/api/admin/users/101", headers=headers).status_code == 422
    assert admins.is_admin(101) is True


def test_super_admin_assignment_never_creates_mutable_role_state(monkeypatch):
    _configure_admin(monkeypatch)
    before = admins.admin_version(101)

    assert admins.add_admin(101) is False
    assert get_store().smembers("admins") == set()
    assert admins.admin_version(101) == before


def test_full_chat_purge_requires_exact_confirmation(client, monkeypatch):
    _configure_admin(monkeypatch)
    history.upsert(
        -100123,
        {
            "message_id": 1,
            "source_update_id": 1,
            "user_id": 202,
            "username": None,
            "name": "User",
            "text": "private",
            "ts": int(time.time()),
            "edit_ts": None,
            "is_edited": False,
            "is_bot": False,
            "reply_to": None,
        },
    )
    headers = _admin_headers(101)
    rejected = client.request(
        "DELETE",
        "/api/admin/logs",
        json={"confirmation": "wrong"},
        headers=headers,
    )
    assert rejected.status_code == 422
    accepted = client.request(
        "DELETE",
        "/api/admin/logs",
        json={"confirmation": "PURGE ALL CHAT DATA"},
        headers=headers,
    )
    assert accepted.status_code == 200
    assert history.recent(-100123) == []


def test_admin_frontend_has_management_sections_without_inner_html():
    html = Path("public/index.html").read_text(encoding="utf-8")
    javascript = Path("public/app.js").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    for section in ("users", "lists", "rules", "tone", "logs", "privacy"):
        assert f'id="{section}"' in html
    assert 'id="admins"' not in html
    assert "custom system prompt" not in html.casefold()
    assert "judge context" not in html.casefold()
    assert "innerHTML" not in javascript
    assert "textContent" in javascript
    assert "Durable model-job snapshots" in javascript
    assert "Recent group history is not sent to Tavily." in javascript
    assert "Expired records are excluded and removed on the next access." in javascript
    assert "Math.round" not in javascript
    assert "${retention.history_seconds} seconds" in javascript
    assert "${retention.job_seconds} seconds" in javascript
    assert 'id="owner-contact"' in html
    assert 'id="copy-notice" type="button" disabled' in html
    assert "copyButton.disabled = !contact" in javascript
    assert "restoreButtonStates();" in javascript
    assert 'button.id === "copy-notice" && !hasOwnerContact' in javascript
    assert (
        'document.querySelectorAll("button").forEach((button) => '
        "{ button.disabled = false; });"
    ) not in javascript
    assert "Enter a monitored owner contact before copying." in javascript
    assert "REPLACE WITH OWNER CONTACT" not in javascript
    assert "30 days by default" not in readme
    assert "exact configured retention values" in readme
    guide = Path("HOW_TO_RUN_AND_TEST_ALL_FEATURES.md").read_text(encoding="utf-8")
    assert "Enter a monitored owner contact" in guide


def test_hostile_admin_and_history_strings_remain_plain_text_data(
    client, monkeypatch
):
    _configure_admin(monkeypatch)
    hostile = '<img src=x onerror=alert(1)><script>alert(2)</script>javascript:bad'
    created = client.post(
        "/api/admin/rules",
        json={
            "id": "hostile",
            "enabled": True,
            "priority": 1,
            "scope": "all",
            "match": {"type": "phrase", "value": hostile},
            "instruction": hostile,
            "stop_processing": False,
        },
        headers=_admin_headers(101),
    )
    assert created.status_code == 201
    assert created.json()["instruction"] == hostile
    history.upsert(
        -100123,
        {
            "message_id": 88,
            "source_update_id": 88,
            "user_id": 202,
            "name": hostile,
            "text": hostile,
            "ts": int(time.time()),
            "is_bot": False,
        },
    )
    logs = client.get("/api/admin/logs", headers=_admin_headers(101))
    assert logs.status_code == 200
    assert logs.json()["records"][0]["text"] == hostile

    javascript = Path("public/app.js").read_text(encoding="utf-8")
    for unsafe_sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
    ):
        assert unsafe_sink not in javascript
    assert "body.textContent = details" in javascript


def test_session_is_server_backed_and_assigned_roles_never_grant_access(monkeypatch):
    _configure_admin(monkeypatch)
    admins.add_admin(202)
    with pytest.raises(PermissionError, match="owner"):
        session.issue_session(202)

    token, _csrf = session.issue_session(101)
    assert session.require_session(token) == 101
    claims = jwt.decode(
        token,
        settings.SESSION_SECRET,
        algorithms=["HS256"],
        audience="kulajaj-admin",
        issuer="kulajaj",
    )
    get_store().delete(f"session:{claims['jti']}")
    with pytest.raises(PermissionError):
        session.require_session(token)


def test_session_rejects_noncanonical_subject_and_wrong_algorithm(monkeypatch):
    _configure_admin(monkeypatch)
    now = int(time.time())
    jti = "j" * 24
    version = admins.admin_version(101)
    get_store().set(
        f"session:{jti}",
        '{"user_id":101,"csrf":"token","admin_version":0}',
        ex=300,
    )
    claims = {
        "iss": "kulajaj",
        "aud": "kulajaj-admin",
        "sub": "0101",
        "tg_user_id": 101,
        "jti": jti,
        "admin_version": version,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }
    bad_subject = jwt.encode(claims, settings.SESSION_SECRET, algorithm="HS256")
    with pytest.raises(PermissionError):
        session.require_session(bad_subject)
    wrong_algorithm = jwt.encode(claims, "different-secret" * 4, algorithm="HS384")
    with pytest.raises(PermissionError):
        session.require_session(wrong_algorithm)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": "+101"},
        {"sub": 101},
        {"tg_user_id": True},
        {"tg_user_id": 101.0},
        {"admin_version": True},
        {"iss": "attacker"},
        {"aud": "wrong-audience"},
        {"exp_offset": -1},
        {"exp_offset": session.SESSION_TTL + 1},
    ],
)
def test_session_rejects_noncanonical_or_invalid_claims(monkeypatch, overrides):
    _configure_admin(monkeypatch)
    now = int(time.time())
    jti = "canonical-session-jti-1234"
    version = admins.admin_version(101)
    get_store().set(
        f"session:{jti}",
        json.dumps(
            {"user_id": 101, "csrf": "csrf", "admin_version": version}
        ),
        ex=session.SESSION_TTL,
    )
    claims: dict[str, object] = {
        "iss": "kulajaj",
        "aud": "kulajaj-admin",
        "sub": "101",
        "tg_user_id": 101,
        "jti": jti,
        "admin_version": version,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }
    claim_overrides = dict(overrides)
    exp_offset = claim_overrides.pop("exp_offset", None)
    claims.update(claim_overrides)
    if exp_offset is not None:
        claims["exp"] = now + exp_offset
    token = jwt.encode(claims, settings.SESSION_SECRET, algorithm="HS256")
    with pytest.raises(PermissionError):
        session.require_session(token)


def test_session_server_record_cannot_be_swapped_between_tokens(monkeypatch):
    _configure_admin(monkeypatch)
    first, _csrf = session.issue_session(101)
    second, _csrf = session.issue_session(101)
    first_claims = jwt.decode(
        first,
        settings.SESSION_SECRET,
        algorithms=["HS256"],
        audience="kulajaj-admin",
        issuer="kulajaj",
    )
    second_claims = jwt.decode(
        second,
        settings.SESSION_SECRET,
        algorithms=["HS256"],
        audience="kulajaj-admin",
        issuer="kulajaj",
    )
    first_key = f"session:{first_claims['jti']}"
    second_key = f"session:{second_claims['jti']}"
    first_record = get_store().get(first_key)
    second_record = get_store().get(second_key)
    get_store().set(first_key, second_record, ex=300)
    get_store().set(second_key, first_record, ex=300)

    # Equal-user records are still bound by their CSRF only at mutation time;
    # cross-user substitution is the security boundary.
    assert session.require_session(first) == 101

    get_store().set(
        first_key,
        json.dumps(
            {
                "user_id": 202,
                "csrf": "forged",
                "admin_version": admins.admin_version(202),
            }
        ),
        ex=300,
    )
    with pytest.raises(PermissionError):
        session.require_session(first)


def test_assigned_admin_cannot_create_a_session(client, monkeypatch):
    _configure_admin(monkeypatch)
    admins.add_admin(202)
    with pytest.raises(PermissionError, match="owner"):
        session.issue_session(202)


def test_owner_session_does_not_depend_on_group_membership_checks(
    client, monkeypatch
):
    _configure_admin(monkeypatch)
    token, _csrf = session.issue_session(101)
    monkeypatch.setattr(
        "app.auth.membership.require_group_member",
        lambda *_args, **_kwargs: pytest.fail("membership must not be consulted"),
    )
    response = client.get(
        "/api/admin/me",
        headers={"Cookie": f"{session.SESSION_COOKIE}={token}"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "super_admin"


def test_logout_requires_origin_and_deletes_secure_host_cookie(client, monkeypatch):
    _configure_admin(monkeypatch)
    response = client.post("/api/auth/logout", headers=_admin_headers(101))
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert session.SESSION_COOKIE in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie


def test_list_rename_preserves_members_atomically():
    lists.create(
        {
            "slug": "before",
            "title": "Before",
            "enabled": True,
            "priority": 1,
            "applies_to": ["explicit"],
            "injected_prompt": "Policy",
        }
    )
    lists.add_member("before", 202)
    renamed = lists.update(
        "before",
        {
            "slug": "after",
            "title": "After",
            "enabled": True,
            "priority": 2,
            "applies_to": ["explicit"],
            "injected_prompt": "New policy",
        },
    )
    assert renamed["slug"] == "after"
    assert lists.get("before") is None
    assert lists.member_ids("after") == [202]


def test_tone_api_writes_the_requested_chat_preset(client, monkeypatch):
    _configure_admin(monkeypatch)
    response = client.put(
        "/api/admin/tone",
        json={
            "scope": "chat",
            "tone_preset": "serious",
        },
        headers=_admin_headers(101),
    )
    assert response.status_code == 200
    assert response.json()["effective"] == {"tone_preset": "serious"}
    assert response.json()["chat_override"] == {"tone_preset": "serious"}


def test_tone_api_rejects_editable_system_prompt_fields(client, monkeypatch):
    _configure_admin(monkeypatch)

    response = client.put(
        "/api/admin/tone",
        json={
            "scope": "chat",
            "tone_preset": "serious",
            "custom_system_prompt": "Replace immutable policy.",
        },
        headers=_admin_headers(101),
    )

    assert response.status_code == 422
    assert config_store.get_config(-100123)["chat_override"] is None


def test_unknown_username_explains_observation_boundary(client, monkeypatch):
    _configure_admin(monkeypatch)
    response = client.get("/api/admin/users?q=@never_seen", headers=_admin_headers(101))
    assert response.status_code == 422
    assert "first message" in response.json()["error"]


def test_admin_body_size_is_bounded_before_json_parsing(client, monkeypatch):
    _configure_admin(monkeypatch)
    response = client.post(
        "/api/admin/rules",
        content=b"{" + b"x" * (64 * 1024),
        headers={**_admin_headers(101), "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_security_headers_protect_admin_document(client):
    response = client.get("/")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_oidc_start_uses_pkce_minimal_scope_and_browser_cookie(client, monkeypatch):
    _configure_admin(monkeypatch)
    response = client.get("/api/auth/telegram/start", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "scope=openid+profile" in location
    assert "code_challenge_method=S256" in location
    assert "write" not in location and "phone" not in location
    assert OIDC_COOKIE_NAME in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_oidc_start_rate_limit_is_enforced(client, monkeypatch):
    _configure_admin(monkeypatch)
    for _attempt in range(20):
        assert (
            client.get("/api/auth/telegram/start", follow_redirects=False).status_code
            == 302
        )
    response = client.get("/api/auth/telegram/start", follow_redirects=False)
    assert response.status_code == 429


OIDC_COOKIE_NAME = "__Host-kulajaj_oidc"


def test_oidc_callback_uses_basic_auth_and_issues_secure_session(
    client, monkeypatch
):
    _configure_admin(monkeypatch)
    redirect_uri = "https://admin.example/api/auth/telegram/callback"
    state_value, handle, _challenge, _nonce = create_state(redirect_uri)
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": "signed-id-token"}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr("app.admin.routes.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.admin.routes._decode_identity", lambda _token, _nonce: 101)
    response = client.get(
        f"/api/auth/telegram/callback?code=code&state={state_value}",
        headers={"Cookie": f"{OIDC_COOKIE_NAME}={handle}"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert isinstance(captured["auth"], httpx.BasicAuth)
    assert "client_secret" not in captured["data"]
    set_cookie = response.headers.get_list("set-cookie")
    assert any(session.SESSION_COOKIE in value and "Secure" in value for value in set_cookie)
    assert any(OIDC_COOKIE_NAME in value and "Max-Age=0" in value and "Secure" in value for value in set_cookie)


def test_oidc_callback_rejects_token_exchange_error_and_clears_binding(
    client, monkeypatch
):
    _configure_admin(monkeypatch)
    redirect_uri = "https://admin.example/api/auth/telegram/callback"
    state_value, handle, _challenge, _nonce = create_state(redirect_uri)

    class FakeResponse:
        status_code = 503

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.admin.routes.httpx.AsyncClient", FakeAsyncClient)
    response = client.get(
        f"/api/auth/telegram/callback?code=code&state={state_value}",
        headers={"Cookie": f"{OIDC_COOKIE_NAME}={handle}"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert session.SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_group_membership_requires_active_status_and_verified_identity(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", -100123)
    monkeypatch.setattr(
        "app.auth.membership.telegram_client.get_chat_member",
        lambda _chat, user_id: {
            "status": "member",
            "user": {
                "id": user_id,
                "first_name": "Member",
                "last_name": "MustNotBeStored",
                "is_bot": False,
            },
        },
    )
    profile = require_group_member(202, seed_profile=True)
    assert profile["id"] == 202
    assert profile["name"] == "Member"
    assert users.get(202)["name"] == "Member"
    get_store().delete("member:-100123:303")
    monkeypatch.setattr(
        "app.auth.membership.telegram_client.get_chat_member",
        lambda _chat, user_id: {"status": "left", "user": {"id": user_id}},
    )
    with pytest.raises(PermissionError):
        require_group_member(303)


def test_explicit_google_query_is_normalized_and_bounded():
    assert normalize_explicit_query("  alice_private   public claim  ") == (
        "alice_private public claim"
    )
    assert normalize_explicit_query("x" * 241) == "x" * 240


def test_seed_is_idempotent_without_force(monkeypatch):
    from scripts import seed

    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", -100123)
    monkeypatch.setattr("sys.argv", ["seed.py"])
    assert seed.main() == 0
    assert seed.main() == 0
    assert lists.get("ignore") is not None
