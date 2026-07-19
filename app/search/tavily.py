"""Bounded, de-identified Tavily basic search adapter."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.settings import settings

TAVILY_ENDPOINT = "https://api.tavily.com/search"
MAX_RESULTS = 3
MAX_RESPONSE_BYTES = 256_000
MAX_QUERY_CHARS = 240
_IDENTIFIER = re.compile(r"(?:@|https?://|\b\d{4,}\b)")
_SEARCH_SEMAPHORE = asyncio.Semaphore(3)


class TavilyUnavailable(RuntimeError):
    """Search is unavailable; callers can degrade to logic-only reasoning."""


@dataclass(frozen=True, slots=True)
class SearchSource:
    source_id: str
    title: str
    url: str
    snippet: str


def sanitize_query(query: str, forbidden_terms: tuple[str, ...] = ()) -> str | None:
    if not isinstance(query, str):
        return None
    value = " ".join(query.split())[:MAX_QUERY_CHARS]
    lowered = value.casefold()
    if not value or _IDENTIFIER.search(value):
        return None
    for term in forbidden_terms:
        private = " ".join(term.split()).casefold()
        if not private:
            continue
        if private in lowered:
            return None
        if len(lowered) >= 4 and lowered in private:
            return None
    return value


async def search(
    query: str,
    *,
    client: httpx.AsyncClient | None = None,
    forbidden_terms: tuple[str, ...] = (),
) -> list[SearchSource]:
    safe = sanitize_query(query, forbidden_terms)
    if safe is None or not settings.TAVILY_API_KEY:
        raise TavilyUnavailable("search_unavailable")
    owned = client is None
    http = client or httpx.AsyncClient(follow_redirects=False)
    try:
        async with _SEARCH_SEMAPHORE:
            response = None
            for attempt in range(2):
                try:
                    async with asyncio.timeout(12):
                        response = await http.post(
                            TAVILY_ENDPOINT,
                            headers={"Authorization": f"Bearer {settings.TAVILY_API_KEY}"},
                            json={"query": safe, "search_depth": "basic", "max_results": MAX_RESULTS},
                            timeout=httpx.Timeout(connect=3, read=8, write=3, pool=3),
                        )
                except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
                    if attempt == 1:
                        raise TavilyUnavailable("search_unavailable") from None
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 1:
                        raise TavilyUnavailable("search_unavailable")
                    continue
                break
            if response is None:
                raise TavilyUnavailable("search_unavailable")
    finally:
        if owned:
            await http.aclose()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise TavilyUnavailable("search_response_too_large")
    if response.status_code == 401 or response.status_code >= 500:
        raise TavilyUnavailable("search_unavailable")
    if not 200 <= response.status_code < 300:
        raise TavilyUnavailable("search_rejected")
    try:
        payload = response.json()
    except ValueError:
        raise TavilyUnavailable("search_invalid_response") from None
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        return []
    result: list[SearchSource] = []
    for item in raw_results[:MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            not parsed
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            continue
        result.append(
            SearchSource(
                source_id=f"S{len(result) + 1}",
                title=" ".join(str(item.get("title") or "").split())[:256],
                url=url[:2_048],
                snippet=" ".join(
                    str(item.get("content") or item.get("snippet") or "").split()
                )[:1_000],
            )
        )
    return result
