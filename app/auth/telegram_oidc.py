"""Telegram OIDC state and PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

from app.settings import settings
from app.store.redis import get_store

STATE_TTL = 600


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def create_state(redirect_uri: str) -> tuple[str, str, str, str]:
    state = _b64(secrets.token_bytes(32))
    handle = _b64(secrets.token_bytes(32))
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    nonce = _b64(secrets.token_bytes(32))
    get_store().set(
        f"auth:state:{hashlib.sha256(state.encode()).hexdigest()}",
        f"{hashlib.sha256(handle.encode()).hexdigest()}:{verifier}:{redirect_uri}:{nonce}",
        ex=STATE_TTL,
    )
    return state, handle, challenge, nonce


def authorization_url(state: str, challenge: str, redirect_uri: str, nonce: str = "") -> str:
    return "https://oauth.telegram.org/auth?" + urlencode(
        {"client_id": settings.TELEGRAM_OIDC_CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code", "scope": "openid profile", "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    )


def consume_state(state: str, handle: str, redirect_uri: str) -> tuple[str, str, str]:
    key = f"auth:state:{hashlib.sha256(state.encode()).hexdigest()}"
    raw = get_store().get(key)
    if raw is None:
        raise ValueError("invalid state")
    parts = raw.split(":", 3)
    if len(parts) != 4 or not secrets.compare_digest(parts[0], hashlib.sha256(handle.encode()).hexdigest()) or parts[2] != redirect_uri:
        raise ValueError("invalid state")
    if not get_store().delete_if_value(key, raw):
        raise ValueError("state already consumed")
    return parts[1], parts[2], parts[3]
