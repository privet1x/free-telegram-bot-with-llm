"""Authenticated, same-origin admin API and Telegram OIDC endpoints."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import unicodedata
from typing import Literal, TypeVar
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from app.auth import session
from app.auth.membership import require_group_member
from app.auth.telegram_oidc import authorization_url, consume_state, create_state
from app.request_body import InvalidRequestBody, RequestBodyTooLarge, read_bounded_body
from app.settings import production_config_errors, session_secret_is_safe, settings
from app.store import admins, config_store, history, lists, rules, users
from app.store.jobs import (
    chat_index_key,
    get_job_repository,
    privacy_receipt_key,
    user_index_key,
)
from app.store.redis import get_store
from app.telegram.client import TelegramAPIError

router = APIRouter()

MAX_ADMIN_BODY_BYTES = 64 * 1024
OIDC_COOKIE = "__Host-kulajaj_oidc"
PURGE_CONFIRMATION = "PURGE ALL CHAT DATA"
_jwks_client: jwt.PyJWKClient | None = None
_jwks_lock = threading.Lock()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class AdminSelector(StrictModel):
    user_id: int | None = Field(default=None, gt=0)
    username: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def exactly_one_identity(self) -> "AdminSelector":
        if (self.user_id is None) == (self.username is None):
            raise ValueError("provide exactly one user_id or username")
        return self


class ListInput(StrictModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=256)
    enabled: bool
    priority: int = Field(ge=-1000, le=1000)
    applies_to: list[Literal["explicit", "auto", "judge"]] = Field(
        min_length=1, max_length=3
    )
    injected_prompt: str = Field(max_length=8_000)


class RuleMatchInput(StrictModel):
    type: Literal["substring", "word", "phrase"]
    value: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_word(self) -> "RuleMatchInput":
        if self.type == "word" and len(rules.normalize_text(self.value).split()) != 1:
            raise ValueError("word rules require exactly one normalized token")
        return self


class RuleInput(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    enabled: bool
    priority: int = Field(ge=-1000, le=1000)
    scope: Literal["auto", "explicit", "judge", "all"]
    match: RuleMatchInput
    instruction: str = Field(min_length=1, max_length=8_000)
    stop_processing: bool


class ToneInput(StrictModel):
    scope: Literal["global", "chat"]
    tone_mode: Literal["preset", "custom"] | None = None
    tone_preset: Literal[
        "neutral", "serious", "scientist", "street", "sarcastic_robot"
    ] | None = None
    custom_system_prompt: str | None = Field(default=None, max_length=8_000)
    judge_default_n: int | None = Field(default=None, ge=5, le=30)

    @model_validator(mode="after")
    def contains_tone_update(self) -> "ToneInput":
        update_fields = self.model_fields_set - {"scope"}
        if not update_fields:
            raise ValueError("provide at least one tone field")
        for field in ("tone_mode", "tone_preset", "judge_default_n"):
            if field in update_fields and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PurgeInput(StrictModel):
    confirmation: str = Field(max_length=64)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _token(request: Request) -> str | None:
    return request.cookies.get(session.SESSION_COOKIE)


def _production_ready() -> bool:
    return not os.environ.get("VERCEL") or not production_config_errors()


def _auth_config_ready() -> bool:
    parsed = urlsplit(settings.PUBLIC_BASE_URL)
    return bool(
        settings.TELEGRAM_OIDC_CLIENT_ID
        and settings.TELEGRAM_OIDC_CLIENT_SECRET
        and session_secret_is_safe(settings.SESSION_SECRET)
        and parsed.scheme == "https"
        and parsed.netloc
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _require(request: Request) -> int | JSONResponse:
    if not _production_ready():
        return _error("service not ready", 503)
    try:
        return session.require_session(_token(request))
    except PermissionError:
        return _error("unauthorized", 401)
    except (RuntimeError, TelegramAPIError):
        return _error("Telegram membership check is unavailable", 503)


def _request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if not referer:
        return None
    parsed = urlsplit(referer)
    if parsed.scheme and parsed.netloc and parsed.username is None:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return None


def _require_mutation(request: Request) -> int | JSONResponse:
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    expected_origin = settings.PUBLIC_BASE_URL.rstrip("/")
    provided_origin = _request_origin(request)
    if (
        not expected_origin
        or provided_origin is None
        or not hmac.compare_digest(provided_origin, expected_origin)
    ):
        return _error("forbidden", 403)
    try:
        expected_csrf = session.csrf_token(_token(request))
    except PermissionError:
        return _error("unauthorized", 401)
    except (RuntimeError, TelegramAPIError):
        return _error("Telegram membership check is unavailable", 503)
    provided_csrf = request.headers.get("x-csrf-token", "")
    if not provided_csrf or not hmac.compare_digest(provided_csrf, expected_csrf):
        return _error("forbidden", 403)
    return actor


def _require_super(request: Request, *, mutation: bool) -> int | JSONResponse:
    actor = _require_mutation(request) if mutation else _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    if actor != settings.SUPER_ADMIN_ID:
        return _error("super-admin access required", 403)
    return actor


async def _parse_body(request: Request, model: type[ModelT]) -> ModelT | JSONResponse:
    try:
        raw = await read_bounded_body(request, MAX_ADMIN_BODY_BYTES)
        return model.model_validate_json(raw)
    except RequestBodyTooLarge:
        return _error("request body is too large", 413)
    except (InvalidRequestBody, ValidationError, ValueError):
        return _error("invalid request body", 422)


def _rate_limit(request: Request, purpose: str, limit: int = 20) -> bool:
    address = request.client.host if request.client is not None else "unknown"
    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()[:24]
    return settings.PUBLIC_BASE_URL != "" and get_store().rate_limit(
        f"auth:rate:{purpose}:{digest}", limit, 60
    )


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        with _jwks_lock:
            if _jwks_client is None:
                _jwks_client = jwt.PyJWKClient(
                    "https://oauth.telegram.org/.well-known/jwks.json",
                    cache_keys=True,
                    max_cached_keys=16,
                    cache_jwk_set=True,
                    lifespan=300,
                    timeout=5,
                )
    return _jwks_client


def _decode_identity(id_token: str, nonce: str) -> int:
    signing_key = _jwks().get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        audience=settings.TELEGRAM_OIDC_CLIENT_ID,
        issuer="https://oauth.telegram.org",
        options={
            "require": ["exp", "iat", "iss", "aud", "sub", "nonce", "id"]
        },
        leeway=30,
    )
    claim_nonce = claims.get("nonce")
    if not isinstance(claim_nonce, str) or not hmac.compare_digest(
        claim_nonce, nonce
    ):
        raise ValueError("invalid nonce")
    subject = claims.get("sub")
    user_id = claims.get("id")
    issued_at = claims.get("iat")
    if (
        not isinstance(subject, str)
        or not subject
        or subject != subject.strip()
        or len(subject.encode("utf-8")) > 255
        or any(
            unicodedata.category(character).startswith("C")
            for character in subject
        )
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or issued_at > int(time.time()) + 30
    ):
        raise ValueError("invalid identity")
    return user_id


def _resolve_selector(selector: AdminSelector) -> tuple[int, dict | None]:
    if selector.user_id is not None:
        return selector.user_id, users.get(selector.user_id)
    username = (selector.username or "").lstrip("@")
    profile = users.resolve_username(username)
    if profile is None:
        raise LookupError(
            "Unknown username. The person must first message in the allowed group "
            "or be entered by numeric Telegram ID."
        )
    return int(profile["id"]), profile


@router.get("/api/public/config")
def public_config() -> dict[str, str]:
    return {
        "telegram_bot_username": settings.TELEGRAM_BOT_USERNAME,
        "oidc_client_id": settings.TELEGRAM_OIDC_CLIENT_ID,
    }


@router.get("/api/auth/telegram/start")
def auth_start(request: Request):
    if not _production_ready() or not _auth_config_ready():
        return _error("service not ready", 503)
    if not _rate_limit(request, "start"):
        return _error("too many authentication attempts", 429)
    redirect_uri = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/auth/telegram/callback"
    state, handle, challenge, nonce = create_state(redirect_uri)
    response = RedirectResponse(
        authorization_url(state, challenge, redirect_uri, nonce), status_code=302
    )
    response.set_cookie(
        OIDC_COOKIE,
        handle,
        max_age=600,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/api/auth/telegram/callback")
async def auth_callback(
    request: Request, code: str | None = None, state: str | None = None
):
    response: JSONResponse | RedirectResponse
    try:
        if not _production_ready() or not _auth_config_ready():
            raise PermissionError("authentication unavailable")
        rate_allowed = await run_in_threadpool(_rate_limit, request, "callback", 30)
        if not rate_allowed:
            raise PermissionError("authentication unavailable")
        if not code or len(code) > 4_096 or not state:
            raise ValueError("missing callback parameters")
        redirect_uri = (
            f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/auth/telegram/callback"
        )
        cookie = request.cookies.get(OIDC_COOKIE)
        if not cookie:
            raise ValueError("missing browser binding")
        verifier, _, nonce = await run_in_threadpool(
            consume_state, state, cookie, redirect_uri
        )
        async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
            token_response = await client.post(
                "https://oauth.telegram.org/token",
                auth=httpx.BasicAuth(
                    settings.TELEGRAM_OIDC_CLIENT_ID,
                    settings.TELEGRAM_OIDC_CLIENT_SECRET,
                ),
                data={
                    "client_id": settings.TELEGRAM_OIDC_CLIENT_ID,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            )
        if token_response.status_code != 200:
            raise ValueError("token exchange failed")
        token_data = token_response.json()
        id_token = token_data.get("id_token") if isinstance(token_data, dict) else None
        if not isinstance(id_token, str) or len(id_token) > 32_768:
            raise ValueError("missing identity token")
        user_id = await run_in_threadpool(_decode_identity, id_token, nonce)
        token, _ = await run_in_threadpool(session.issue_session, user_id)
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            session.SESSION_COOKIE,
            token,
            max_age=session.SESSION_TTL,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
    except (
        ValueError,
        KeyError,
        PermissionError,
        RuntimeError,
        httpx.HTTPError,
        jwt.PyJWTError,
        TelegramAPIError,
    ):
        response = _error("authentication failed", 400)
    response.delete_cookie(
        OIDC_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    session.revoke(_token(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        session.SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/api/admin/me")
def admin_me(request: Request):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    profile = users.get(actor)
    try:
        csrf = session.csrf_token(_token(request))
    except PermissionError:
        return _error("unauthorized", 401)
    except (RuntimeError, TelegramAPIError):
        return _error("Telegram membership check is unavailable", 503)
    return {
        "user_id": actor,
        "username": profile.get("username") if profile else None,
        "name": profile.get("name") if profile else None,
        "role": "super_admin" if actor == settings.SUPER_ADMIN_ID else "admin",
        "is_super_admin": actor == settings.SUPER_ADMIN_ID,
        "csrf_token": csrf,
        "retention": {
            "history_limit": 30,
            "history_seconds": settings.HISTORY_RETENTION_SECONDS,
            "job_seconds": settings.JOB_RETENTION_SECONDS,
        },
    }


@router.get("/api/admin/admins")
def admin_list(request: Request):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    ids = sorted(set(admins.list_admins()) | ({settings.SUPER_ADMIN_ID} if settings.SUPER_ADMIN_ID else set()))
    return {
        "admins": [
            {
                "user_id": user_id,
                "is_super_admin": user_id == settings.SUPER_ADMIN_ID,
                "profile": users.get(user_id),
            }
            for user_id in ids
        ]
    }


@router.post("/api/admin/admins", status_code=201)
async def admin_add(request: Request):
    actor = _require_super(request, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    payload = await _parse_body(request, AdminSelector)
    if isinstance(payload, JSONResponse):
        return payload
    try:
        user_id, _ = _resolve_selector(payload)
        if user_id != settings.SUPER_ADMIN_ID:
            require_group_member(user_id, seed_profile=True)
        admins.add_admin(user_id)
    except LookupError as exc:
        return _error(str(exc), 422)
    except PermissionError:
        return _error("user is not an active member of the allowed group", 422)
    except (RuntimeError, TelegramAPIError):
        return _error("Telegram membership check is unavailable", 503)
    return JSONResponse({"user_id": user_id, "profile": users.get(user_id)}, status_code=201)


@router.delete("/api/admin/admins/{user_id}")
def admin_remove(request: Request, user_id: int):
    actor = _require_super(request, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        removed = admins.remove_admin(user_id)
    except ValueError as exc:
        return _error(str(exc), 422)
    return {"removed": removed}


@router.get("/api/admin/users")
def users_get(request: Request, q: str = ""):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    query = q.strip()
    if not query or len(query) > 64:
        return _error("enter an exact numeric ID or observed @username", 422)
    if query.isascii() and query.isdecimal():
        user_id = int(query)
        if user_id <= 0:
            return _error("user ID must be positive", 422)
        return {"user_id": user_id, "user": users.get(user_id)}
    profile = users.resolve_username(query.lstrip("@"))
    if profile is None:
        return _error(
            "Unknown username. The person must first message in the allowed group "
            "or be entered by numeric Telegram ID.",
            422,
        )
    return {"user_id": profile["id"], "user": profile}


@router.delete("/api/admin/users/{user_id}")
def user_delete(request: Request, user_id: int, purge_messages: bool = False):
    actor = _require_super(request, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    if user_id == settings.SUPER_ADMIN_ID:
        return _error("the super-admin profile cannot be deleted", 422)
    try:
        admins.remove_admin(user_id)
        purged_jobs = 0
        if purge_messages:
            index_key = user_index_key(user_id)
            purge_result = get_job_repository().purge_index(index_key)
            purged_jobs = purge_result.job_count
            if settings.TELEGRAM_ALLOWED_CHAT_ID is not None:
                history.purge_user(
                    settings.TELEGRAM_ALLOWED_CHAT_ID,
                    user_id,
                    purge_result.outbound_message_ids,
                )
            get_store().delete(privacy_receipt_key(index_key))
        removed_profile, removed_memberships = users.delete_with_memberships(user_id)
    except ValueError:
        return _error("deletion failed", 422)
    except Exception:
        return _error("temporary storage failure", 503)
    return {
        "deleted": removed_profile,
        "memberships_removed": removed_memberships,
        "messages_purged": purge_messages,
        "jobs_purged": purged_jobs,
        "note": (
            "Data already sent to an external provider cannot be recalled. "
            "Use full-chat purge if older job provenance has expired."
        ),
    }


def _lists_payload() -> dict[str, list[dict]]:
    return {
        "lists": [
            {**item, "members": lists.member_ids(str(item["slug"]))}
            for item in lists.all_lists()
        ]
    }


@router.get("/api/admin/lists")
def lists_get(request: Request):
    actor = _require(request)
    return actor if isinstance(actor, JSONResponse) else _lists_payload()


@router.post("/api/admin/lists", status_code=201)
async def lists_add(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    payload = await _parse_body(request, ListInput)
    if isinstance(payload, JSONResponse):
        return payload
    try:
        item = lists.create(payload.model_dump())
    except (ValueError, TypeError) as exc:
        return _error(str(exc), 422)
    return JSONResponse(item, status_code=201)


@router.put("/api/admin/lists/{slug}")
async def lists_update(request: Request, slug: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    payload = await _parse_body(request, ListInput)
    if isinstance(payload, JSONResponse):
        return payload
    try:
        return lists.update(slug, payload.model_dump())
    except KeyError:
        return _error("list not found", 404)
    except (ValueError, TypeError) as exc:
        return _error(str(exc), 422)


@router.delete("/api/admin/lists/{slug}")
def lists_delete(request: Request, slug: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        removed = lists.delete(slug)
    except ValueError as exc:
        return _error(str(exc), 422)
    return {"deleted": removed}


@router.post("/api/admin/lists/{slug}/members/{user_id}")
def list_member_add(request: Request, slug: str, user_id: int):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        return {"added": lists.add_member(slug, user_id)}
    except KeyError:
        return _error("list not found", 404)
    except ValueError as exc:
        return _error(str(exc), 422)


@router.delete("/api/admin/lists/{slug}/members/{user_id}")
def list_member_delete(request: Request, slug: str, user_id: int):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        return {"removed": lists.remove_member(slug, user_id)}
    except ValueError as exc:
        return _error(str(exc), 422)


@router.get("/api/admin/rules")
def rules_get(request: Request):
    actor = _require(request)
    return actor if isinstance(actor, JSONResponse) else {"rules": rules.all_rules()}


@router.post("/api/admin/rules", status_code=201)
async def rules_add(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    payload = await _parse_body(request, RuleInput)
    if isinstance(payload, JSONResponse):
        return payload
    try:
        item = rules.create(payload.model_dump())
    except (ValueError, TypeError) as exc:
        return _error(str(exc), 422)
    return JSONResponse(item, status_code=201)


@router.put("/api/admin/rules/{rule_id}")
async def rules_update(request: Request, rule_id: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    payload = await _parse_body(request, RuleInput)
    if isinstance(payload, JSONResponse):
        return payload
    if payload.id != rule_id:
        return _error("rule IDs are immutable", 422)
    try:
        return rules.update(rule_id, payload.model_dump())
    except KeyError:
        return _error("rule not found", 404)
    except (ValueError, TypeError) as exc:
        return _error(str(exc), 422)


@router.delete("/api/admin/rules/{rule_id}")
def rules_delete(request: Request, rule_id: str):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    try:
        return {"deleted": rules.delete(rule_id)}
    except ValueError as exc:
        return _error(str(exc), 422)


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
    payload = await _parse_body(request, ToneInput)
    if isinstance(payload, JSONResponse):
        return payload
    values = payload.model_dump(exclude_unset=True)
    scope = values.pop("scope")
    try:
        config_store.set_tone(
            scope,
            chat_id=settings.TELEGRAM_ALLOWED_CHAT_ID if scope == "chat" else None,
            **values,
        )
        return config_store.get_config(settings.TELEGRAM_ALLOWED_CHAT_ID)
    except (ValueError, TypeError) as exc:
        return _error(str(exc), 422)


@router.delete("/api/admin/tone/override")
def tone_override_delete(request: Request):
    actor = _require_mutation(request)
    if isinstance(actor, JSONResponse):
        return actor
    removed = False
    if settings.TELEGRAM_ALLOWED_CHAT_ID is not None:
        removed = config_store.clear_chat_override(settings.TELEGRAM_ALLOWED_CHAT_ID)
    return {"removed": removed}


@router.get("/api/admin/logs")
def logs_get(request: Request):
    actor = _require(request)
    if isinstance(actor, JSONResponse):
        return actor
    records = (
        history.recent(settings.TELEGRAM_ALLOWED_CHAT_ID)
        if settings.TELEGRAM_ALLOWED_CHAT_ID is not None
        else []
    )
    return {"records": records, "limit": 30}


@router.delete("/api/admin/logs")
async def logs_delete(request: Request):
    actor = _require_super(request, mutation=True)
    if isinstance(actor, JSONResponse):
        return actor
    payload = await _parse_body(request, PurgeInput)
    if isinstance(payload, JSONResponse):
        return payload
    if not hmac.compare_digest(payload.confirmation, PURGE_CONFIRMATION):
        return _error("confirmation phrase does not match", 422)
    chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
    if chat_id is None:
        return _error("allowed chat is not configured", 503)
    repository = get_job_repository()
    index_key = chat_index_key(chat_id)
    purge_result = repository.purge_index(index_key)
    history.purge_all(chat_id)
    get_store().delete(privacy_receipt_key(index_key))
    return {
        "purged": True,
        "jobs_purged": purge_result.job_count,
        "note": "Data already sent to external providers cannot be recalled.",
    }
