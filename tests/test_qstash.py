from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest

from app.queue import qstash as qstash_module
from app.queue.qstash import (
    QStashPublishError,
    QStashVerificationError,
    failure_url,
    process_url,
    publish,
    verify_signature,
)
from app.settings import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "PUBLIC_BASE_URL": "https://bot.example/",
        "QSTASH_URL": "https://qstash.example/",
        "QSTASH_TOKEN": "qstash-token-canary",
        "QSTASH_CURRENT_SIGNING_KEY": "current-signing-key-with-at-least-32-bytes",
        "QSTASH_NEXT_SIGNING_KEY": "next-signing-key-with-at-least-32-bytes",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _run_publish(
    job_id: int | str,
    *,
    config: Settings,
    client: Any,
) -> str:
    return asyncio.run(publish(job_id, config=config, client=client))


def _signed_token(
    body: bytes,
    destination: str,
    key: str,
    *,
    expires_at: int | None = None,
    not_before: int | None = None,
) -> str:
    now = int(time.time())
    body_hash = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode()
    return jwt.encode(
        {
            "iss": "Upstash",
            "sub": destination,
            "exp": expires_at if expires_at is not None else now + 60,
            "nbf": not_before if not_before is not None else now - 1,
            "body": body_hash.rstrip("="),
        },
        key,
        algorithm="HS256",
    )


@pytest.mark.parametrize("status_code", [200, 202])
def test_publish_sends_exact_private_job_only_contract(status_code: int):
    config = _settings()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, json={"messageId": "qstash-message-1"})

    async def exercise() -> str:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await publish(7_654_321, config=config, client=client)

    assert asyncio.run(exercise()) == "qstash-message-1"
    assert len(captured) == 1

    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://qstash.example/v2/publish/"
        "https://bot.example/api/telegram/process"
    )
    assert request.content == b'{"job_id":"7654321"}'
    assert json.loads(request.content) == {"job_id": "7654321"}
    assert b"qstash-token-canary" not in request.content
    assert b"private-chat-canary" not in request.content
    assert request.headers["Authorization"] == "Bearer qstash-token-canary"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Upstash-Method"] == "POST"
    assert request.headers["Upstash-Retries"] == "3"
    assert request.headers["Upstash-Retry-Delay"] == (
        "max(275000, exp(2.5 * retried) * 1000)"
    )
    assert request.headers["Upstash-Failure-Callback"] == (
        "https://bot.example/api/telegram/failure"
    )
    assert request.headers["Upstash-Deduplication-Id"] == "telegram-7654321"

    timeout = request.extensions["timeout"]
    assert timeout == {"connect": 5.0, "read": 12.0, "write": 5.0, "pool": 5.0}
    assert all(0 < value <= 20 for value in timeout.values())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"messageId": None},
        {"messageId": True},
        {"messageId": ""},
        {"messageId": "line\nbreak"},
        {"messageId": "x" * 513},
    ],
)
def test_publish_rejects_malformed_success_payload_without_leaking_body(
    payload: object,
):
    private_response = "private-response-canary"

    def handler(_: httpx.Request) -> httpx.Response:
        response_payload = payload
        if isinstance(response_payload, dict):
            response_payload = {**response_payload, "private": private_response}
        return httpx.Response(200, json=response_payload)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(QStashPublishError) as raised:
                await publish(10, config=_settings(), client=client)
        assert raised.value.error_class == "qstash_invalid_response"
        assert raised.value.retryable is True
        assert str(raised.value) == "qstash_invalid_response"
        assert private_response not in repr(raised.value)

    asyncio.run(exercise())


def test_publish_rejects_non_json_success_response_safely():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"private-invalid-json-canary")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(QStashPublishError) as raised:
                await publish(11, config=_settings(), client=client)
        assert raised.value.error_class == "qstash_invalid_response"
        assert raised.value.retryable is True
        assert "private-invalid-json-canary" not in repr(raised.value)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("status_code", "error_class", "retryable"),
    [
        (400, "qstash_rejected", False),
        (401, "qstash_rejected", False),
        (429, "qstash_rate_limited", True),
        (500, "qstash_unavailable", True),
        (503, "qstash_unavailable", True),
    ],
)
def test_publish_classifies_http_failures_without_leaking_response(
    status_code: int,
    error_class: str,
    retryable: bool,
):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="private-http-body-canary")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(QStashPublishError) as raised:
                await publish(12, config=_settings(), client=client)
        assert raised.value.error_class == error_class
        assert raised.value.retryable is retryable
        assert str(raised.value) == error_class
        assert "private-http-body-canary" not in repr(raised.value)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("exception_factory", "error_class"),
    [
        (
            lambda request: httpx.ConnectError(
                "private-transport-canary", request=request
            ),
            "qstash_transport",
        ),
        (
            lambda request: httpx.ReadTimeout(
                "private-timeout-canary", request=request
            ),
            "qstash_timeout",
        ),
    ],
)
def test_publish_sanitizes_transport_and_httpx_timeout_failures(
    exception_factory: Callable[[httpx.Request], Exception],
    error_class: str,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_factory(request)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(QStashPublishError) as raised:
                await publish(13, config=_settings(), client=client)
        assert raised.value.error_class == error_class
        assert raised.value.retryable is True
        assert str(raised.value) == error_class
        assert "private" not in repr(raised.value)

    asyncio.run(exercise())


def test_publish_enforces_a_bounded_total_timeout(monkeypatch: pytest.MonkeyPatch):
    class SlowClient:
        async def post(self, *_: object, **__: object) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"messageId": "too-late"})

    monkeypatch.setattr(qstash_module, "_PUBLISH_TOTAL_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(QStashPublishError) as raised:
        _run_publish(14, config=_settings(), client=SlowClient())

    assert raised.value.error_class == "qstash_timeout"
    assert raised.value.retryable is True
    assert str(raised.value) == "qstash_timeout"


@pytest.mark.parametrize(
    "job_id",
    [True, False, "", "-1", "+1", "1.0", " 1", "1 ", "１２", "1" * 33],
)
def test_publish_rejects_non_decimal_job_ids_before_http(job_id: object):
    class UnexpectedClient:
        async def post(self, *_: object, **__: object) -> httpx.Response:
            raise AssertionError("HTTP must not run for an invalid job ID")

    with pytest.raises(ValueError, match="decimal Telegram update ID"):
        _run_publish(job_id, config=_settings(), client=UnexpectedClient())


def test_publish_configuration_errors_are_sanitized():
    with pytest.raises(QStashPublishError) as raised:
        _run_publish(
            15,
            config=_settings(QSTASH_TOKEN=""),
            client=object(),
        )

    assert raised.value.error_class == "qstash_configuration"
    assert raised.value.retryable is False
    assert str(raised.value) == "qstash_configuration"


@pytest.mark.parametrize(
    ("key_name", "destination_factory"),
    [
        ("QSTASH_CURRENT_SIGNING_KEY", process_url),
        ("QSTASH_NEXT_SIGNING_KEY", process_url),
        ("QSTASH_CURRENT_SIGNING_KEY", failure_url),
        ("QSTASH_NEXT_SIGNING_KEY", failure_url),
    ],
)
def test_signature_verifier_accepts_current_and_next_keys_for_canonical_routes(
    key_name: str,
    destination_factory: Callable[[Settings], str],
):
    config = _settings()
    destination = destination_factory(config)
    body = b'{"job_id":"123"}'
    signature = _signed_token(body, destination, getattr(config, key_name))

    assert verify_signature(body, signature, destination, config=config) is None


def test_signature_covers_exact_raw_body_including_whitespace():
    config = _settings()
    destination = process_url(config)
    raw_body = b'{\n  "job_id" : "123"\t\n}'
    signature = _signed_token(
        raw_body,
        destination,
        config.QSTASH_CURRENT_SIGNING_KEY,
    )

    verify_signature(raw_body, signature, destination, config=config)

    with pytest.raises(QStashVerificationError) as raised:
        verify_signature(b'{"job_id":"123"}', signature, destination, config=config)
    assert raised.value.error_class == "qstash_invalid_signature"
    assert raised.value.status_code == 401


def test_signature_accepts_utf8_and_rejects_invalid_utf8_before_verification():
    config = _settings()
    destination = process_url(config)
    raw_body = '{"job_id":"123","marker":"Zażółć 🚀"}'.encode()
    signature = _signed_token(
        raw_body,
        destination,
        config.QSTASH_CURRENT_SIGNING_KEY,
    )

    verify_signature(raw_body, signature, destination, config=config)

    with pytest.raises(QStashVerificationError) as raised:
        verify_signature(b'{"job_id":"\xff"}', signature, destination, config=config)
    assert raised.value.error_class == "qstash_invalid_body"
    assert raised.value.status_code == 400


def test_signature_rejects_body_hash_url_expiry_and_wrong_key():
    config = _settings()
    destination = process_url(config)
    body = b'{"job_id":"123"}'
    now = int(time.time())
    cases = [
        (
            b'{"job_id":"124"}',
            _signed_token(
                body,
                destination,
                config.QSTASH_CURRENT_SIGNING_KEY,
            ),
            destination,
        ),
        (
            body,
            _signed_token(
                body,
                failure_url(config),
                config.QSTASH_CURRENT_SIGNING_KEY,
            ),
            destination,
        ),
        (
            body,
            _signed_token(
                body,
                destination,
                config.QSTASH_CURRENT_SIGNING_KEY,
                expires_at=now - 1,
                not_before=now - 60,
            ),
            destination,
        ),
        (
            body,
            _signed_token(
                body,
                destination,
                "untrusted-signing-key-with-at-least-32-bytes",
            ),
            destination,
        ),
    ]

    for candidate_body, signature, candidate_url in cases:
        with pytest.raises(QStashVerificationError) as raised:
            verify_signature(
                candidate_body,
                signature,
                candidate_url,
                config=config,
            )
        assert raised.value.error_class == "qstash_invalid_signature"
        assert raised.value.status_code == 401
        assert str(raised.value) == "qstash_invalid_signature"


def test_signature_rejects_noncanonical_destination_before_sdk_verification():
    config = _settings()
    body = b'{"job_id":"123"}'
    noncanonical_url = "https://bot.example/api/telegram/process/"
    signature = _signed_token(
        body,
        noncanonical_url,
        config.QSTASH_CURRENT_SIGNING_KEY,
    )

    with pytest.raises(QStashVerificationError) as raised:
        verify_signature(body, signature, noncanonical_url, config=config)
    assert raised.value.error_class == "qstash_invalid_destination"
    assert raised.value.status_code == 401


def test_signature_rejects_oversized_body_before_decoding_or_verification():
    config = _settings()

    with pytest.raises(QStashVerificationError) as raised:
        verify_signature(
            b"x" * (64 * 1024 + 1),
            "not-even-a-jwt",
            process_url(config),
            config=config,
        )

    assert raised.value.error_class == "qstash_invalid_body"
    assert raised.value.status_code == 400


@pytest.mark.parametrize("signature", ["", "x" * 8_193, None])
def test_signature_rejects_missing_or_oversized_signature(signature: object):
    config = _settings()

    with pytest.raises(QStashVerificationError) as raised:
        verify_signature(
            b'{"job_id":"123"}',
            signature,
            process_url(config),
            config=config,
        )

    assert raised.value.error_class == "qstash_invalid_signature"
    assert raised.value.status_code == 401


def test_signature_requires_both_rotation_keys_without_exposing_them():
    config = _settings(QSTASH_NEXT_SIGNING_KEY="")
    body = b'{"job_id":"123"}'
    destination = process_url(config)
    signature = _signed_token(
        body,
        destination,
        config.QSTASH_CURRENT_SIGNING_KEY,
    )

    with pytest.raises(QStashVerificationError) as raised:
        verify_signature(body, signature, destination, config=config)

    assert raised.value.error_class == "qstash_configuration"
    assert raised.value.status_code == 503
    assert config.QSTASH_CURRENT_SIGNING_KEY not in repr(raised.value)
