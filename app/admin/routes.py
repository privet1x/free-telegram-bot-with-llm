"""Minimal secure admin/public endpoints shared by the static UI."""

from __future__ import annotations

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import session
from app.auth.telegram_oidc import authorization_url, consume_state, create_state
from app.settings import settings
from app.store import admins
from app.store import config_store, history, lists, rules, users

router = APIRouter()


def _token(request: Request) -> str | None:
    return request.cookies.get(session.SESSION_COOKIE)


def _require(request: Request) -> int | JSONResponse:
    try:
        return session.require_session(_token(request))
    except PermissionError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)


def _require_mutation(request: Request) -> int | JSONResponse:
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    origin = request.headers.get("origin")
    if settings.PUBLIC_BASE_URL and origin and origin.rstrip("/") != settings.PUBLIC_BASE_URL.rstrip("/"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        expected = session.csrf_token(_token(request))
    except PermissionError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("x-csrf-token") != expected:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return actor


@router.get("/api/public/config")
def public_config() -> dict[str, str]:
    return {"telegram_bot_username": settings.TELEGRAM_BOT_USERNAME, "oidc_client_id": settings.TELEGRAM_OIDC_CLIENT_ID}


@router.get("/api/auth/telegram/start")
def auth_start(request: Request):
    redirect_uri = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/auth/telegram/callback"
    state, handle, challenge, nonce = create_state(redirect_uri)
    response = RedirectResponse(authorization_url(state, challenge, redirect_uri, nonce), status_code=302)
    response.set_cookie("__Host-kulajaj_oidc", handle, max_age=600, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    origin = request.headers.get("origin")
    if settings.PUBLIC_BASE_URL and origin and origin.rstrip("/") != settings.PUBLIC_BASE_URL.rstrip("/"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    token = _token(request)
    if token:
        try:
            expected = session.csrf_token(token)
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.headers.get("x-csrf-token") != expected:
            return JSONResponse({"error": "forbidden"}, status_code=403)
    session.revoke(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(session.SESSION_COOKIE, path="/")
    return response


@router.get("/api/auth/telegram/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None):
    response: JSONResponse | RedirectResponse
    try:
        if not code or not state:
            raise ValueError("missing callback parameters")
        redirect_uri = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/auth/telegram/callback"
        cookie = request.cookies.get("__Host-kulajaj_oidc")
        if not cookie:
            raise ValueError("missing browser binding")
        verifier, _, nonce = consume_state(state, cookie, redirect_uri)
        async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
            token_response = await client.post(
                "https://oauth.telegram.org/token",
                data={"client_id": settings.TELEGRAM_OIDC_CLIENT_ID, "client_secret": settings.TELEGRAM_OIDC_CLIENT_SECRET, "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code_verifier": verifier},
            )
        if token_response.status_code != 200:
            raise ValueError("token exchange failed")
        token_data = token_response.json()
        id_token = token_data.get("id_token") if isinstance(token_data, dict) else None
        if not isinstance(id_token, str):
            raise ValueError("missing identity token")
        signing_key = jwt.PyJWKClient("https://oauth.telegram.org/.well-known/jwks.json").get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, signing_key, algorithms=["RS256"], audience=settings.TELEGRAM_OIDC_CLIENT_ID, issuer="https://oauth.telegram.org", options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
        if claims.get("nonce") != nonce:
            raise ValueError("invalid nonce")
        user_id = claims.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("invalid identity")
        token, _ = session.issue_session(user_id)
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(session.SESSION_COOKIE, token, max_age=session.SESSION_TTL, secure=True, httponly=True, samesite="lax", path="/")
    except (ValueError, KeyError, PermissionError, httpx.HTTPError, jwt.PyJWTError):
        response = JSONResponse({"error": "authentication failed"}, status_code=400)
    response.delete_cookie("__Host-kulajaj_oidc", path="/")
    return response


@router.get("/api/admin/me")
def admin_me(request: Request):
    try:
        user_id = session.require_session(_token(request))
    except PermissionError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        csrf = session.csrf_token(_token(request))
    except PermissionError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"user_id": user_id, "is_super_admin": user_id == settings.SUPER_ADMIN_ID, "csrf_token": csrf}


@router.get("/api/admin/admins")
def admin_list(request: Request):
    if isinstance((user_id := _require(request)), JSONResponse):
        return user_id
    return {"admins": admins.list_admins()}


@router.post("/api/admin/admins")
async def admin_add(request: Request):
    user_id = _require_mutation(request)
    if isinstance(user_id, JSONResponse) or user_id != settings.SUPER_ADMIN_ID:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        payload = await request.json()
        admins.add_admin(int(payload["user_id"]))
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "invalid user_id"}, status_code=422)
    return {"ok": True}


@router.delete("/api/admin/admins/{user_id}")
def admin_remove(request: Request, user_id: int):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse) or actor != settings.SUPER_ADMIN_ID:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        admins.remove_admin(user_id)
    except ValueError:
        return JSONResponse({"error": "cannot remove super-admin"}, status_code=422)
    return {"ok": True}


@router.get("/api/admin/lists")
def lists_get(request: Request):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    return {"lists": lists.all_lists()}


@router.post("/api/admin/lists")
async def lists_add(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        item = lists.create(await request.json())
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return item


@router.put("/api/admin/lists/{slug}")
async def lists_update(request: Request, slug: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        return lists.update(slug, await request.json())
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@router.delete("/api/admin/lists/{slug}")
def lists_delete(request: Request, slug: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        return {"deleted": lists.delete(slug)}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@router.post("/api/admin/lists/{slug}/members/{user_id}")
def list_member_add(request: Request, slug: str, user_id: int):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        return {"added": lists.add_member(slug, user_id)}
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@router.delete("/api/admin/lists/{slug}/members/{user_id}")
def list_member_delete(request: Request, slug: str, user_id: int):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    return {"removed": lists.remove_member(slug, user_id)}


@router.get("/api/admin/rules")
def rules_get(request: Request):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    return {"rules": rules.all_rules()}


@router.post("/api/admin/rules")
async def rules_add(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        item = rules.create(await request.json())
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return item


@router.put("/api/admin/rules/{rule_id}")
async def rules_update(request: Request, rule_id: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        return rules.update(rule_id, await request.json())
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@router.delete("/api/admin/rules/{rule_id}")
def rules_delete(request: Request, rule_id: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    return {"deleted": rules.delete(rule_id)}


@router.get("/api/admin/tone")
def tone_get(request: Request):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    return config_store.get_config(settings.TELEGRAM_ALLOWED_CHAT_ID)


@router.put("/api/admin/tone")
async def tone_put(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        payload = await request.json()
        scope = payload.pop("scope")
        chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID if scope == "chat" else None
        return config_store.set_tone(scope, chat_id=chat_id, **payload)
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@router.delete("/api/admin/tone/override")
def tone_override_delete(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    if settings.TELEGRAM_ALLOWED_CHAT_ID is not None:
        config_store.clear_chat_override(settings.TELEGRAM_ALLOWED_CHAT_ID)
    return {"ok": True}


@router.get("/api/admin/users")
def users_get(request: Request, q: str = ""):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    query = q.strip().lstrip("@")
    if query.isdecimal():
        profile = users.get(int(query))
    else:
        profile = users.resolve_username(query)
    return {"user": profile}


@router.get("/api/admin/logs")
def logs_get(request: Request):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    if settings.TELEGRAM_ALLOWED_CHAT_ID is None:
        return {"records": []}
    return {"records": history.recent(settings.TELEGRAM_ALLOWED_CHAT_ID)}
