"""Pure validation helpers for grounded judge claims and citations."""

from __future__ import annotations

import re
from typing import Mapping

from app.search.tavily import sanitize_query

_CLAIM_ID = re.compile(r"^C[1-3]$")


def validate_claims(value: object) -> list[dict[str, str]]:
    raw = value.get("claims") if isinstance(value, Mapping) else None
    if not isinstance(raw, list):
        raise ValueError("claims must be a list")
    claims: list[dict[str, str]] = []
    for item in raw[:3]:
        if not isinstance(item, Mapping):
            continue
        claim_id, neutral, query = item.get("claim_id"), item.get("neutral_claim"), item.get("search_query")
        safe_query = sanitize_query(query) if isinstance(query, str) else None
        if not isinstance(claim_id, str) or not _CLAIM_ID.fullmatch(claim_id):
            continue
        if not isinstance(neutral, str) or not neutral.strip() or safe_query is None:
            continue
        claims.append({"claim_id": claim_id, "neutral_claim": neutral[:500], "search_query": safe_query})
    return claims


def validate_citations(text: str, source_ids: set[str]) -> str:
    """Remove invented citation IDs while preserving plain-text verdict content."""
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in source_ids else ""

    cleaned = re.sub(r"\[(S\d+)\]", replace, text)
    return re.sub(r"https?://\S+", "", cleaned)
