"""Minimal asynchronous QStash publishing and signature verification.

Only an opaque job ID leaves the application when work is queued. Exceptions
from this module deliberately contain stable error classes rather than request
URLs, credentials, payloads, or provider response bodies.
"""

from __future__ import annotations

import asyncio
import json
from typing import Final
from urllib.parse import urlsplit

import httpx
from qstash import Receiver
from qstash.errors import SignatureError

from app.settings import Settings, settings

PROCESS_PATH: Final = "/api/telegram/process"
FAILURE_PATH: Final = "/api/telegram/failure"
PUBLISH_RETRIES: Final = 3
RETRY_DELAY_EXPRESSION: Final = "max(275000, exp(2.5 * retried) * 1000)"

_MAX_JOB_ID_CHARS: Final = 32
_MAX_MESSAGE_ID_CHARS: Final = 512
_MAX_SIGNATURE_CHARS: Final = 8_192
_DEFAULT_MAX_SIGNED_BODY_BYTES: Final = 64 * 1024
_PUBLISH_TOTAL_TIMEOUT_SECONDS: Final = 20.0
_PUBLISH_TIMEOUT: Final = httpx.Timeout(
    connect=5.0,
    read=12.0,
    write=5.0,
    pool=5.0,
)


class QStashPublishError(RuntimeError):
    """A sanitized failure while publishing a QStash message."""

    def __init__(self, error_class: str, *, retryable: bool) -> None:
        self.error_class = error_class
        self.retryable = retryable
        super().__init__(error_class)


class QStashVerificationError(RuntimeError):
    """A sanitized QStash request-verification failure."""

    def __init__(self, error_class: str, *, status_code: int) -> None:
        self.error_class = error_class
        self.status_code = status_code
        super().__init__(error_class)


def _canonical_base_url(config: Settings) -> str:
    base_url = config.PUBLIC_BASE_URL
    parsed = urlsplit(base_url)
    if (
        not base_url
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise QStashVerificationError("qstash_configuration", status_code=503)
    return base_url.rstrip("/")


def process_url(config: Settings = settings) -> str:
    """Return the exact configured processor destination URL."""
    return f"{_canonical_base_url(config)}{PROCESS_PATH}"


def failure_url(config: Settings = settings) -> str:
    """Return the exact configured failure-callback URL."""
    return f"{_canonical_base_url(config)}{FAILURE_PATH}"


def _normalize_job_id(job_id: int | str) -> str:
    if isinstance(job_id, bool):
        raise ValueError("job_id must be a decimal Telegram update ID")
    normalized = str(job_id)
    if (
        not normalized
        or len(normalized) > _MAX_JOB_ID_CHARS
        or not normalized.isascii()
        or not normalized.isdecimal()
    ):
        raise ValueError("job_id must be a decimal Telegram update ID")
    return normalized


def _publish_endpoint(config: Settings, destination: str) -> str:
    qstash_url = config.QSTASH_URL.rstrip("/")
    parsed = urlsplit(qstash_url)
    if (
        not qstash_url
        or not config.QSTASH_TOKEN
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise QStashPublishError("qstash_configuration", retryable=False)
    return f"{qstash_url}/v2/publish/{destination}"


def _publish_headers(
    *, job_id: str, config: Settings, callback_url: str
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.QSTASH_TOKEN}",
        "Content-Type": "application/json",
        "Upstash-Method": "POST",
        "Upstash-Retries": str(PUBLISH_RETRIES),
        "Upstash-Retry-Delay": RETRY_DELAY_EXPRESSION,
        "Upstash-Failure-Callback": callback_url,
        "Upstash-Deduplication-Id": f"telegram-{job_id}",
    }


def _message_id(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        raise QStashPublishError("qstash_invalid_response", retryable=True) from None

    message_id = payload.get("messageId") if isinstance(payload, dict) else None
    if (
        not isinstance(message_id, str)
        or not message_id
        or len(message_id) > _MAX_MESSAGE_ID_CHARS
        or any(ord(character) < 0x20 for character in message_id)
    ):
        raise QStashPublishError("qstash_invalid_response", retryable=True)
    return message_id


async def publish(
    job_id: int | str,
    *,
    config: Settings = settings,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Publish one opaque job ID and return QStash's message ID.

    No application data other than ``job_id`` is serialized into the body.
    ``client`` is an optional owned-elsewhere client for tests and connection
    reuse; the adapter always applies its own bounded request timeout.
    """
    normalized_job_id = _normalize_job_id(job_id)
    try:
        destination = process_url(config)
        callback_url = failure_url(config)
    except QStashVerificationError:
        raise QStashPublishError("qstash_configuration", retryable=False) from None
    endpoint = _publish_endpoint(config, destination)
    headers = _publish_headers(
        job_id=normalized_job_id,
        config=config,
        callback_url=callback_url,
    )
    body = json.dumps(
        {"job_id": normalized_job_id},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    owns_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=False)
    try:
        async with asyncio.timeout(_PUBLISH_TOTAL_TIMEOUT_SECONDS):
            response = await http.post(
                endpoint,
                headers=headers,
                content=body,
                timeout=_PUBLISH_TIMEOUT,
                follow_redirects=False,
            )
    except TimeoutError:
        raise QStashPublishError("qstash_timeout", retryable=True) from None
    except httpx.TimeoutException:
        raise QStashPublishError("qstash_timeout", retryable=True) from None
    except httpx.TransportError:
        raise QStashPublishError("qstash_transport", retryable=True) from None
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code == 429:
        raise QStashPublishError("qstash_rate_limited", retryable=True)
    if response.status_code >= 500:
        raise QStashPublishError("qstash_unavailable", retryable=True)
    if not 200 <= response.status_code < 300:
        raise QStashPublishError("qstash_rejected", retryable=False)
    return _message_id(response)


def verify_signature(
    raw_body: bytes,
    signature: str,
    destination_url: str,
    *,
    config: Settings = settings,
    max_body_bytes: int = _DEFAULT_MAX_SIGNED_BODY_BYTES,
) -> None:
    """Verify a raw UTF-8 QStash body against an exact configured route URL.

    The official receiver automatically tries both the current and next signing
    keys. Callers must run this before parsing the body as JSON.
    """
    if (
        not isinstance(raw_body, bytes)
        or max_body_bytes <= 0
        or len(raw_body) > max_body_bytes
    ):
        raise QStashVerificationError("qstash_invalid_body", status_code=400)
    try:
        body = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise QStashVerificationError("qstash_invalid_body", status_code=400) from None

    if (
        not isinstance(signature, str)
        or not signature
        or len(signature) > _MAX_SIGNATURE_CHARS
    ):
        raise QStashVerificationError("qstash_invalid_signature", status_code=401)

    allowed_urls = {process_url(config), failure_url(config)}
    if destination_url not in allowed_urls:
        raise QStashVerificationError("qstash_invalid_destination", status_code=401)
    if not config.QSTASH_CURRENT_SIGNING_KEY or not config.QSTASH_NEXT_SIGNING_KEY:
        raise QStashVerificationError("qstash_configuration", status_code=503)

    receiver = Receiver(
        current_signing_key=config.QSTASH_CURRENT_SIGNING_KEY,
        next_signing_key=config.QSTASH_NEXT_SIGNING_KEY,
    )
    try:
        receiver.verify(
            signature=signature,
            body=body,
            url=destination_url,
        )
    except SignatureError:
        raise QStashVerificationError(
            "qstash_invalid_signature", status_code=401
        ) from None
