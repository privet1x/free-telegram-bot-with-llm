"""Telegram OIDC state and PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode

from app.settings import settings
from app.store.redis import get_store

STATE_TTL = 600


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def create_state(redirect_uri: str) -> tuple[str, str, str, str]:
    if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
        raise ValueError("invalid redirect URI")
    state = _b64(secrets.token_bytes(32))
    handle = _b64(secrets.token_bytes(32))
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    nonce = _b64(secrets.token_bytes(32))
    get_store().set(
        f"auth:state:{hashlib.sha256(state.encode()).hexdigest()}",
        json.dumps(
            {
                "handle_hash": hashlib.sha256(handle.encode()).hexdigest(),
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "nonce": nonce,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        ex=STATE_TTL,
    )
    return state, handle, challenge, nonce


def authorization_url(state: str, challenge: str, redirect_uri: str, nonce: str = "") -> str:
    return "https://oauth.telegram.org/auth?" + urlencode(
        {"client_id": settings.TELEGRAM_OIDC_CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code", "scope": "openid profile", "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    )


def consume_state(state: str, handle: str, redirect_uri: str) -> tuple[str, str, str]:
    if any(
        not isinstance(value, str) or not value or len(value) > 2_048
        for value in (state, handle, redirect_uri)
    ):
        raise ValueError("invalid state")
    key = f"auth:state:{hashlib.sha256(state.encode()).hexdigest()}"
    store = get_store()
    raw = store.get(key)
    if raw is None:
        raise ValueError("invalid state")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("invalid state") from None
    verifier = value.get("verifier") if isinstance(value, dict) else None
    nonce = value.get("nonce") if isinstance(value, dict) else None
    saved_handle_hash = value.get("handle_hash") if isinstance(value, dict) else None
    saved_redirect_uri = value.get("redirect_uri") if isinstance(value, dict) else None
    if (
        not isinstance(saved_handle_hash, str)
        or not secrets.compare_digest(
            saved_handle_hash, hashlib.sha256(handle.encode()).hexdigest()
        )
        or not isinstance(saved_redirect_uri, str)
        or not secrets.compare_digest(saved_redirect_uri, redirect_uri)
        or not isinstance(verifier, str)
        or not verifier
        or not isinstance(nonce, str)
        or not nonce
    ):
        raise ValueError("invalid state")
    if not store.delete_if_value(key, raw):
        raise ValueError("state already consumed")
    return verifier, redirect_uri, nonce
