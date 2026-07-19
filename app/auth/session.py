"""Signed, revocable administrator sessions."""

from __future__ import annotations

import secrets
import time
import json
from typing import Any

import jwt

from app.settings import session_secret_is_safe, settings
from app.auth.membership import require_group_member
from app.store import admins
from app.store.redis import get_store

SESSION_COOKIE = "__Host-kulajaj_session"
SESSION_TTL = 8 * 60 * 60


def _session_key(jti: str) -> str:
    return f"session:{jti}"


def issue_session(user_id: int) -> tuple[str, str]:
    if not admins.is_admin(user_id):
        raise PermissionError("not an administrator")
    if not session_secret_is_safe(settings.SESSION_SECRET):
        raise PermissionError("session service unavailable")
    if user_id != settings.SUPER_ADMIN_ID:
        require_group_member(user_id, seed_profile=True, allow_cache=False)
    jti = secrets.token_urlsafe(24)
    now = int(time.time())
    token = jwt.encode(
        {"iss": "kulajaj", "aud": "kulajaj-admin", "sub": str(user_id), "tg_user_id": user_id, "jti": jti, "admin_version": admins.admin_version(user_id), "iat": now, "nbf": now, "exp": now + SESSION_TTL},
        settings.SESSION_SECRET,
        algorithm="HS256",
    )
    csrf = secrets.token_urlsafe(24)
    get_store().set(
        _session_key(jti),
        json.dumps(
            {
                "user_id": user_id,
                "csrf": csrf,
                "admin_version": admins.admin_version(user_id),
            },
            separators=(",", ":"),
        ),
        ex=SESSION_TTL,
    )
    return token, csrf


def require_session(token: str | None) -> int:
    if not token or not session_secret_is_safe(settings.SESSION_SECRET):
        raise PermissionError("unauthorized")
    try:
        claims: dict[str, Any] = jwt.decode(token, settings.SESSION_SECRET, algorithms=["HS256"], audience="kulajaj-admin", issuer="kulajaj", options={"require": ["iss", "aud", "sub", "tg_user_id", "jti", "iat", "nbf", "exp", "admin_version"]})
    except jwt.PyJWTError:
        raise PermissionError("unauthorized") from None
    user_id = claims.get("tg_user_id")
    jti = claims.get("jti")
    claim_version = claims.get("admin_version")
    issued_at = claims.get("iat")
    not_before = claims.get("nbf")
    expires_at = claims.get("exp")
    raw = get_store().get(_session_key(jti)) if isinstance(jti, str) else None
    try:
        record = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        record = {}
    version = admins.admin_version(user_id) if isinstance(user_id, int) else -1
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not isinstance(jti, str)
        or not 16 <= len(jti) <= 128
        or isinstance(claim_version, bool)
        or not isinstance(claim_version, int)
        or isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or isinstance(not_before, bool)
        or not isinstance(not_before, int)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= issued_at
        or expires_at - issued_at > SESSION_TTL
        or not_before > issued_at
        or claims.get("sub") != str(user_id)
        or claim_version != version
        or record.get("user_id") != user_id
        or record.get("admin_version") != version
        or not admins.is_admin(user_id)
    ):
        raise PermissionError("unauthorized")
    if user_id != settings.SUPER_ADMIN_ID:
        try:
            require_group_member(user_id, allow_cache=True)
        except PermissionError:
            raise PermissionError("unauthorized") from None
    return user_id


def revoke(token: str | None) -> None:
    if not token or not settings.SESSION_SECRET:
        return
    try:
        claims = jwt.decode(token, settings.SESSION_SECRET, algorithms=["HS256"], options={"verify_exp": False, "verify_aud": False})
    except jwt.PyJWTError:
        return
    jti = claims.get("jti") if isinstance(claims, dict) else None
    if isinstance(jti, str):
        get_store().delete(_session_key(jti))


def csrf_token(token: str | None) -> str:
    require_session(token)
    try:
        claims = jwt.decode(
            token,
            settings.SESSION_SECRET,
            algorithms=["HS256"],
            audience="kulajaj-admin",
            issuer="kulajaj",
        )
    except jwt.PyJWTError:
        raise PermissionError("unauthorized") from None
    jti = claims.get("jti")
    raw = get_store().get(_session_key(jti)) if isinstance(jti, str) else None
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        value = {}
    csrf = value.get("csrf")
    if not isinstance(csrf, str):
        raise PermissionError("unauthorized")
    return csrf
