"""Bounded Tavily basic-search adapter for explicit and sanitized queries."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.settings import settings

TAVILY_ENDPOINT = "https://api.tavily.com/search"
MAX_RESULTS = 3
MAX_SOURCE_TITLE_CHARS = 256
MAX_SOURCE_URL_CHARS = 2_048
MAX_RESPONSE_BYTES = 256_000
MAX_QUERY_CHARS = 240
SEARCH_ATTEMPT_TIMEOUT_SECONDS = 6
_IDENTIFIER = re.compile(r"(?:@|https?://|\b\d{4,}\b)")
_TITLE_URL = re.compile(r"(?i)(?<![\w@])(?:https?://|www\.)[^\s<>()]+")
_SEARCH_SEMAPHORE = asyncio.Semaphore(3)


class TavilyUnavailable(RuntimeError):
    """Search is unavailable; callers can disclose degraded operation."""


@dataclass(frozen=True, slots=True)
class SearchSource:
    source_id: str
    title: str
    url: str
    snippet: str


def sanitize_source_title(value: object) -> str:
    """Flatten an untrusted result title for a code-rendered source line."""
    raw = str(value or "")
    without_controls = "".join(
        " "
        if character.isspace()
        else ""
        if unicodedata.category(character).startswith("C")
        else character
        for character in raw
    )
    without_urls = _TITLE_URL.sub("", without_controls)
    normalized = " ".join(without_urls.split())[
        :MAX_SOURCE_TITLE_CHARS
    ].strip(" -\u2013\u2014")
    return normalized or "Untitled source"


def normalize_source_url(value: object) -> str | None:
    """Accept one bounded HTTPS URL that is safe to render on one source line."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SOURCE_URL_CHARS
        or any(
            character.isspace()
            or unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value


def sanitize_query(query: str, forbidden_terms: tuple[str, ...] = ()) -> str | None:
    if not isinstance(query, str):
        return None
    value = " ".join(query.split())[:MAX_QUERY_CHARS]
    lowered = value.casefold()
    if not value or _IDENTIFIER.search(value):
        return None
    compact_query = "".join(character for character in lowered if character.isalnum())
    for term in forbidden_terms:
        private = " ".join(term.split()).casefold()
        if not private:
            continue
        if private in lowered:
            return None
        if len(lowered) >= 4 and lowered in private:
            return None
        compact_private = "".join(
            character for character in private if character.isalnum()
        )
        if len(compact_private) >= 3 and compact_private in compact_query:
            return None
    return value


def normalize_explicit_query(query: str) -> str | None:
    """Bound a query that a user explicitly chose to send to Tavily."""
    if not isinstance(query, str):
        return None
    without_controls = "".join(
        " "
        if character.isspace()
        else ""
        if unicodedata.category(character).startswith("C")
        else character
        for character in query
    )
    value = " ".join(without_controls.split())[:MAX_QUERY_CHARS]
    return value or None


async def search(
    query: str,
    *,
    client: httpx.AsyncClient | None = None,
    forbidden_terms: tuple[str, ...] = (),
    explicit: bool = False,
) -> list[SearchSource]:
    safe = (
        normalize_explicit_query(query)
        if explicit
        else sanitize_query(query, forbidden_terms)
    )
    if safe is None or not settings.TAVILY_API_KEY:
        raise TavilyUnavailable("search_unavailable")
    owned = client is None
    http = client or httpx.AsyncClient(follow_redirects=False)
    try:
        async with _SEARCH_SEMAPHORE:
            status_code: int | None = None
            response_body = b""
            try:
                async with asyncio.timeout(SEARCH_ATTEMPT_TIMEOUT_SECONDS):
                    async with http.stream(
                        "POST",
                        TAVILY_ENDPOINT,
                        headers={
                            "Authorization": f"Bearer {settings.TAVILY_API_KEY}"
                        },
                        json={
                            "query": safe,
                            "search_depth": "basic",
                            "max_results": MAX_RESULTS,
                        },
                        timeout=httpx.Timeout(connect=3, read=5, write=3, pool=3),
                    ) as response:
                        status_code = response.status_code
                        if 200 <= status_code < 300:
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                chunks.extend(chunk)
                                if len(chunks) > MAX_RESPONSE_BYTES:
                                    raise TavilyUnavailable(
                                        "search_response_too_large"
                                    )
                            response_body = bytes(chunks)
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
                # A failed client response does not prove that the paid request
                # failed server-side. Never repeat it inside this invocation.
                raise TavilyUnavailable("search_unavailable") from None
            if status_code is None:
                raise TavilyUnavailable("search_unavailable")
    finally:
        if owned:
            await http.aclose()
    if status_code in {401, 429} or status_code >= 500:
        raise TavilyUnavailable("search_unavailable")
    if not 200 <= status_code < 300:
        raise TavilyUnavailable("search_rejected")
    try:
        payload = httpx.Response(200, content=response_body).json()
    except (UnicodeDecodeError, ValueError):
        raise TavilyUnavailable("search_invalid_response") from None
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        return []
    result: list[SearchSource] = []
    for item in raw_results[:MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        url = normalize_source_url(item.get("url"))
        if url is None:
            continue
        result.append(
            SearchSource(
                source_id=f"S{len(result) + 1}",
                title=sanitize_source_title(item.get("title")),
                url=url,
                snippet=" ".join(
                    str(item.get("content") or item.get("snippet") or "").split()
                )[:1_000],
            )
        )
    return result
