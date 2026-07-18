"""Signed, revocable administrator sessions."""

from __future__ import annotations

import secrets
import time
import json
from typing import Any

import jwt

from app.settings import settings
from app.store import admins
from app.store.redis import get_store

SESSION_COOKIE = "__Host-kulajaj_session"
SESSION_TTL = 8 * 60 * 60


def _session_key(jti: str) -> str:
    return f"session:{jti}"


def issue_session(user_id: int) -> tuple[str, str]:
    if not admins.is_admin(user_id):
        raise PermissionError("not an administrator")
    jti = secrets.token_urlsafe(24)
    now = int(time.time())
    token = jwt.encode(
        {"iss": "kulajaj", "aud": "kulajaj-admin", "sub": str(user_id), "tg_user_id": user_id, "jti": jti, "admin_version": admins.admin_version(user_id), "iat": now, "nbf": now, "exp": now + SESSION_TTL},
        settings.SESSION_SECRET,
        algorithm="HS256",
    )
    csrf = secrets.token_urlsafe(24)
    get_store().set(_session_key(jti), json.dumps({"user_id": user_id, "csrf": csrf}), ex=SESSION_TTL)
    return token, csrf


def require_session(token: str | None) -> int:
    if not token or not settings.SESSION_SECRET:
        raise PermissionError("unauthorized")
    try:
        claims: dict[str, Any] = jwt.decode(token, settings.SESSION_SECRET, algorithms=["HS256"], audience="kulajaj-admin", issuer="kulajaj", options={"require": ["iss", "aud", "sub", "tg_user_id", "jti", "iat", "nbf", "exp", "admin_version"]})
    except jwt.PyJWTError:
        raise PermissionError("unauthorized") from None
    user_id = claims.get("tg_user_id")
    jti = claims.get("jti")
    raw = get_store().get(_session_key(jti)) if isinstance(jti, str) else None
    try:
        record = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        record = {}
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not isinstance(jti, str) or claims.get("sub") != str(user_id) or claims.get("admin_version") != admins.admin_version(user_id) or record.get("user_id") != user_id or not admins.is_admin(user_id):
        raise PermissionError("unauthorized")
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
    if not token or not settings.SESSION_SECRET:
        raise PermissionError("unauthorized")
    try:
        claims = jwt.decode(token, settings.SESSION_SECRET, algorithms=["HS256"], audience="kulajaj-admin", issuer="kulajaj")
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
