"""Read configuration from the environment (and .env locally).

All fields are optional with defaults so the app boots even without the full set
of secrets (needed for the health check and tests). In production the values are
set via Vercel Env Vars.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `.env.example` intentionally leaves secrets and later-ticket values empty.
        # Ignoring empty values preserves typed/default settings instead of making a
        # freshly copied template fail validation (for example SUPER_ADMIN_ID="").
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    # MVP is intentionally limited to one closed group. None is convenient for
    # local tests, but production readiness checks reject a missing value.
    TELEGRAM_ALLOWED_CHAT_ID: int | None = None
    # Explicit escape hatch for isolated local tests only. Vercel readiness and
    # normal local operation continue to require the single allowed chat ID.
    ALLOW_UNFILTERED_LOCAL_CHATS: bool = False

    # NVIDIA NIM
    NVIDIA_API_KEY: str = ""
    LLM_MODEL: str = "google/gemma-4-31b-it"

    # Explicit live web search for /google
    TAVILY_API_KEY: str = ""

    # Upstash Redis
    UPSTASH_REDIS_REST_URL: str = ""
    UPSTASH_REDIS_REST_TOKEN: str = ""
    HISTORY_RETENTION_SECONDS: int = Field(default=2_592_000, ge=60)  # sliding

    # Upstash QStash (ticket 02)
    QSTASH_URL: str = "https://qstash.upstash.io"
    QSTASH_TOKEN: str = ""
    QSTASH_CURRENT_SIGNING_KEY: str = ""
    QSTASH_NEXT_SIGNING_KEY: str = ""
    JOB_RETENTION_SECONDS: int = Field(default=604_800, ge=1)
    WORKER_BUDGET_SECONDS: int = Field(default=240, ge=1)
    JOB_LEASE_SECONDS: int = Field(default=270, ge=1)

    # Automatic intervention (ticket 03)
    AUTO_TRIGGER_COOLDOWN_SECONDS: int = Field(default=30, ge=1)
    MAX_LIST_POLICIES: int = Field(default=10, ge=1, le=10)
    MAX_RULE_POLICIES: int = Field(default=10, ge=1, le=10)

    # Admin panel / sessions (ticket 05)
    SUPER_ADMIN_ID: int | None = None
    SESSION_SECRET: str = ""
    TELEGRAM_OIDC_CLIENT_ID: str = ""
    TELEGRAM_OIDC_CLIENT_SECRET: str = ""

    # General
    PUBLIC_BASE_URL: str = ""
    # Vercel Cron authentication for scheduled banter.
    CRON_SECRET: str = ""

    @field_validator(
        "JOB_RETENTION_SECONDS",
        "WORKER_BUDGET_SECONDS",
        "JOB_LEASE_SECONDS",
        "AUTO_TRIGGER_COOLDOWN_SECONDS",
        "MAX_LIST_POLICIES",
        "MAX_RULE_POLICIES",
        mode="before",
    )
    @classmethod
    def reject_boolean_job_limits(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid job limits")
        return value


settings = Settings()


_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_VERCEL_MAX_DURATION_SECONDS = 300
# Three configured QStash retries use two 275-second minimum delays followed by
# an exponential delay of roughly 1,808 seconds. One hour leaves additional
# room for publication, the exhausted-retry callback, and provider jitter.
_MIN_JOB_RETENTION_SECONDS = 3_600


def is_https_base_url(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def session_secret_is_safe(value: str) -> bool:
    """Reject short and obviously degenerate session-signing secrets."""
    return bool(
        isinstance(value, str)
        and len(value.encode("utf-8")) >= 32
        and len(set(value)) >= 8
        and value.casefold()
        not in {
            "replace_me",
            "change_me",
            "your_session_secret",
        }
    )


def production_webhook_config_errors(config: Settings = settings) -> list[str]:
    """Return only names of missing/unsafe Ticket-01 production settings."""
    required = {
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_BOT_USERNAME": config.TELEGRAM_BOT_USERNAME,
        "TELEGRAM_WEBHOOK_SECRET": config.TELEGRAM_WEBHOOK_SECRET,
        "TELEGRAM_ALLOWED_CHAT_ID": config.TELEGRAM_ALLOWED_CHAT_ID,
        "PUBLIC_BASE_URL": config.PUBLIC_BASE_URL,
        "UPSTASH_REDIS_REST_URL": config.UPSTASH_REDIS_REST_URL,
        "UPSTASH_REDIS_REST_TOKEN": config.UPSTASH_REDIS_REST_TOKEN,
    }
    errors = [name for name, value in required.items() if value in (None, "")]

    if (
        config.TELEGRAM_WEBHOOK_SECRET
        and _WEBHOOK_SECRET_RE.fullmatch(config.TELEGRAM_WEBHOOK_SECRET) is None
    ):
        errors.append("TELEGRAM_WEBHOOK_SECRET")
    if config.PUBLIC_BASE_URL:
        if not is_https_base_url(config.PUBLIC_BASE_URL):
            errors.append("PUBLIC_BASE_URL")
    if config.UPSTASH_REDIS_REST_URL:
        parsed = urlsplit(config.UPSTASH_REDIS_REST_URL)
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append("UPSTASH_REDIS_REST_URL")
    if isinstance(config.TELEGRAM_ALLOWED_CHAT_ID, bool) or config.TELEGRAM_ALLOWED_CHAT_ID == 0:
        errors.append("TELEGRAM_ALLOWED_CHAT_ID")
    return sorted(set(errors))


def production_bot_config_errors(config: Settings = settings) -> list[str]:
    """Return production errors that prevent Telegram/worker operation."""
    errors = production_webhook_config_errors(config)
    required = {
        "NVIDIA_API_KEY": config.NVIDIA_API_KEY,
        "LLM_MODEL": config.LLM_MODEL,
        "QSTASH_TOKEN": config.QSTASH_TOKEN,
        "QSTASH_CURRENT_SIGNING_KEY": config.QSTASH_CURRENT_SIGNING_KEY,
        "QSTASH_NEXT_SIGNING_KEY": config.QSTASH_NEXT_SIGNING_KEY,
        "CRON_SECRET": config.CRON_SECRET,
    }
    errors.extend(
        name
        for name, value in required.items()
        if not isinstance(value, str) or not value.strip()
    )
    if config.QSTASH_URL and not is_https_base_url(config.QSTASH_URL):
        errors.append("QSTASH_URL")

    integer_values = {
        "JOB_RETENTION_SECONDS": config.JOB_RETENTION_SECONDS,
        "WORKER_BUDGET_SECONDS": config.WORKER_BUDGET_SECONDS,
        "JOB_LEASE_SECONDS": config.JOB_LEASE_SECONDS,
        "AUTO_TRIGGER_COOLDOWN_SECONDS": config.AUTO_TRIGGER_COOLDOWN_SECONDS,
        "MAX_LIST_POLICIES": config.MAX_LIST_POLICIES,
        "MAX_RULE_POLICIES": config.MAX_RULE_POLICIES,
    }
    for name, value in integer_values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(name)

    budget = config.WORKER_BUDGET_SECONDS
    lease = config.JOB_LEASE_SECONDS
    if (
        isinstance(budget, bool)
        or not isinstance(budget, int)
        or isinstance(lease, bool)
        or not isinstance(lease, int)
        or not 0 < budget < lease < _VERCEL_MAX_DURATION_SECONDS
    ):
        errors.extend(["WORKER_BUDGET_SECONDS", "JOB_LEASE_SECONDS"])

    retention = config.JOB_RETENTION_SECONDS
    cooldown = config.AUTO_TRIGGER_COOLDOWN_SECONDS
    if (
        isinstance(retention, bool)
        or not isinstance(retention, int)
        or retention < _MIN_JOB_RETENTION_SECONDS
    ):
        errors.append("JOB_RETENTION_SECONDS")
    if (
        isinstance(cooldown, bool)
        or not isinstance(cooldown, int)
        or not isinstance(retention, int)
        or isinstance(retention, bool)
        or cooldown > retention
    ):
        errors.append("AUTO_TRIGGER_COOLDOWN_SECONDS")

    return sorted(set(errors))


def production_admin_config_errors(config: Settings = settings) -> list[str]:
    """Return production errors that prevent authenticated admin operation."""
    errors: list[str] = []
    if (
        not isinstance(config.SUPER_ADMIN_ID, int)
        or isinstance(config.SUPER_ADMIN_ID, bool)
        or config.SUPER_ADMIN_ID <= 0
    ):
        errors.append("SUPER_ADMIN_ID")
    if not session_secret_is_safe(config.SESSION_SECRET):
        errors.append("SESSION_SECRET")
    if not config.TELEGRAM_OIDC_CLIENT_ID:
        errors.append("TELEGRAM_OIDC_CLIENT_ID")
    if not config.TELEGRAM_OIDC_CLIENT_SECRET:
        errors.append("TELEGRAM_OIDC_CLIENT_SECRET")
    return sorted(set(errors))


def production_config_errors(config: Settings = settings) -> list[str]:
    """Return all missing or unsafe production settings for the full app."""
    return sorted(
        set(
            production_bot_config_errors(config)
            + production_admin_config_errors(config)
        )
    )
