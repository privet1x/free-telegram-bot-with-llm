"""Pure validation helpers for citations in web-grounded answers."""

from __future__ import annotations

import re

_MARKDOWN_URI = re.compile(
    r"\[([^\]\r\n]{0,512})\]\(\s*[a-z][a-z0-9+.-]{0,31}:[^)\r\n]{0,2048}\)",
    re.IGNORECASE,
)
_URI = re.compile(
    r"(?<![\w@])[a-z][a-z0-9+.-]{0,31}:(?://)?[^\s<>\[\]]+",
    re.IGNORECASE,
)
_IP_LITERAL = re.compile(
    r"(?<![\w@])(?:"
    r"(?:\d{1,3}\.){3}\d{1,3}"
    r"|\[(?=[0-9a-f:.]{1,64}\])"
    r"(?=[0-9a-f.]{0,63}:)[0-9a-f:.]{1,64}\]"
    r")(?::\d{1,5})?(?:/[^\s]*)?",
    re.IGNORECASE,
)
_BARE_DOMAIN = re.compile(
    r"(?<![@\w])(?:www[.\u3002\uff0e\uff61])?"
    r"(?:[^\W_][\w-]*[.\u3002\uff0e\uff61])+[^\W\d_][\w-]*"
    r"(?::\d{1,5})?(?:/[^\s]*)?",
    re.IGNORECASE,
)
_CITATION_ID = re.compile(r"\[(s\d+)\]", re.IGNORECASE)


def validate_citations(text: str, source_ids: set[str]) -> str:
    """Remove invented citation IDs and links while preserving answer text."""

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(1).upper()
        return f"[{candidate}]" if candidate in source_ids else ""

    cleaned = _CITATION_ID.sub(replace, text)
    cleaned = _MARKDOWN_URI.sub(lambda match: match.group(1), cleaned)
    cleaned = _URI.sub("", cleaned)
    cleaned = _IP_LITERAL.sub("", cleaned)
    return _BARE_DOMAIN.sub("", cleaned)
