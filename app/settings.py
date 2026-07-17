"""Read configuration from the environment (and .env locally).

All fields are optional with defaults so the app boots even without the full set
of secrets (needed for the health check and tests). In production the values are
set via Vercel Env Vars.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import Field
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

    # NVIDIA NIM (used from ticket 02 onwards)
    NVIDIA_API_KEY: str = ""
    LLM_MODEL_FAST: str = "deepseek-ai/deepseek-v4-flash"
    LLM_MODEL_SMART: str = "deepseek-ai/deepseek-v4-pro"

    # Grounded fact checks for /judge (ticket 04)
    TAVILY_API_KEY: str = ""
    FACT_CHECK_MAX_QUERIES: int = Field(default=3, ge=1, le=3)

    # Upstash Redis
    UPSTASH_REDIS_REST_URL: str = ""
    UPSTASH_REDIS_REST_TOKEN: str = ""
    HISTORY_RETENTION_SECONDS: int = Field(default=2_592_000, ge=60)  # sliding

    # Upstash QStash (ticket 02)
    QSTASH_URL: str = "https://qstash.upstash.io"
    QSTASH_TOKEN: str = ""
    QSTASH_CURRENT_SIGNING_KEY: str = ""
    QSTASH_NEXT_SIGNING_KEY: str = ""

    # Admin panel / sessions (ticket 05)
    SUPER_ADMIN_ID: int | None = None
    SESSION_SECRET: str = ""

    # General
    PUBLIC_BASE_URL: str = ""


settings = Settings()


_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


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
        parsed = urlsplit(config.PUBLIC_BASE_URL)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            errors.append("PUBLIC_BASE_URL")
    if config.UPSTASH_REDIS_REST_URL:
        parsed = urlsplit(config.UPSTASH_REDIS_REST_URL)
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append("UPSTASH_REDIS_REST_URL")
    if isinstance(config.TELEGRAM_ALLOWED_CHAT_ID, bool) or config.TELEGRAM_ALLOWED_CHAT_ID == 0:
        errors.append("TELEGRAM_ALLOWED_CHAT_ID")
    return sorted(set(errors))
