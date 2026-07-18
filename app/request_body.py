"""Bounded request-body reads for public webhook endpoints."""

from __future__ import annotations

from fastapi import Request


class InvalidRequestBody(ValueError):
    """The request framing is invalid."""


class RequestBodyTooLarge(ValueError):
    """The request body exceeds the endpoint's configured limit."""


async def read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` without first buffering an unbounded body."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length, 10)
        except ValueError:
            raise InvalidRequestBody("invalid Content-Length") from None
        if declared_length < 0:
            raise InvalidRequestBody("invalid Content-Length")
        if declared_length > max_bytes:
            raise RequestBodyTooLarge("request body is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise RequestBodyTooLarge("request body is too large")
        body.extend(chunk)
    return bytes(body)
