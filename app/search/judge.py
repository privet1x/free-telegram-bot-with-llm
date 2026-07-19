"""Pure validation helpers for grounded judge claims and citations."""

from __future__ import annotations

import json
import re
from typing import Mapping

_CLAIM_ID = re.compile(r"^C[1-3]$")
_JSON_FENCE = re.compile(
    r"^```(?:json)?\s*(\{.*\})\s*```$", re.DOTALL | re.IGNORECASE
)
_MARKDOWN_LINK = re.compile(
    r"\[([^\]]*)\]\(\s*(?:https?://|www\.)[^)]+\)", re.IGNORECASE
)
_BARE_DOMAIN = re.compile(
    r"(?<![@\w])(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?",
    re.IGNORECASE,
)


def validate_claims(value: object) -> list[dict[str, str]]:
    raw = value.get("claims") if isinstance(value, Mapping) else None
    if not isinstance(raw, list):
        raise ValueError("claims must be a list")
    claims: list[dict[str, str]] = []
    for item in raw[:3]:
        if not isinstance(item, Mapping):
            continue
        claim_id = item.get("claim_id")
        neutral = item.get("neutral_claim")
        query = item.get("search_query")
        if not isinstance(claim_id, str) or not _CLAIM_ID.fullmatch(claim_id):
            continue
        if (
            not isinstance(neutral, str)
            or not neutral.strip()
            or not isinstance(query, str)
            or not query.strip()
        ):
            continue
        claims.append(
            {
                "claim_id": claim_id,
                "neutral_claim": neutral.strip()[:500],
                # Privacy classification requires the complete job snapshot and
                # therefore belongs in the worker immediately before Tavily.
                "search_query": " ".join(query.split())[:500],
            }
        )
    return claims


def parse_claim_response(text: str) -> list[dict[str, str]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("claim response must be JSON")
    candidate = text.strip()
    fenced = _JSON_FENCE.fullmatch(candidate)
    if fenced is not None:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        raise ValueError("claim response must be JSON") from None
    return validate_claims(value)


def validate_citations(text: str, source_ids: set[str]) -> str:
    """Remove invented citation IDs while preserving plain-text verdict content."""
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in source_ids else ""

    cleaned = re.sub(r"\[(S\d+)\]", replace, text)
    cleaned = _MARKDOWN_LINK.sub(lambda match: match.group(1), cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned, flags=re.IGNORECASE)
    return _BARE_DOMAIN.sub("", cleaned)
