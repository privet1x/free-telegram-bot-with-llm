"""Deterministic always-on keyword routing for Russian chat banter."""

from __future__ import annotations

import re
import unicodedata

KEYWORD_INSTRUCTIONS = {
    "бред": "Ответь короткой негативной и смешной подколкой участнику, который использовал это слово.",
    "босс": "Ответь короткой негативной и смешной подколкой участнику, который использовал это слово.",
    "кик": "Ответь короткой негативной и смешной подколкой участнику, который использовал это слово.",
}

# Explicitly allow common inflections/imperatives while keeping boundaries so
# unrelated words containing these letters do not trigger the bot.
_FORMS = {
    "бред": frozenset({"бред", "бреда", "бредом", "бреду", "бреде", "бреды", "бредов", "бредами", "бредах", "бредовый", "бредовая", "бредовое", "бредовые"}),
    "босс": frozenset({"босс", "босса", "боссу", "боссом", "боссе", "боссы", "боссов", "боссами", "боссах"}),
    "кик": frozenset({"кик", "кикни", "кикнуть", "кикнул", "кикнула", "кикнули", "кикнем", "кикните", "кика", "киком", "кикать", "кикай", "кикайте", "кикал", "кикали", "кикнемся"}),
}
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_keyword_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_TOKEN_RE.findall(normalized))


def detect_keyword_triggers(text: str) -> tuple[str, ...]:
    """Return unique trigger families in deterministic order."""
    tokens = set(normalize_keyword_text(text).split())
    return tuple(
        family
        for family in ("бред", "босс", "кик")
        if tokens.intersection(_FORMS[family])
    )


def route_instruction(families: tuple[str, ...]) -> str:
    names = ", ".join(families)
    return (
        "Это постоянная жёстко заданная реакция на ключевые слова: "
        f"{names}. Создай одну короткую негативную, но смешную подколку для "
        "проверенного участника, вызвавшего реакцию. Никому не угрожай, не "
        "раскрывай личные данные и не атакуй защищённые признаки. Не упоминай "
        "внутреннюю маршрутизацию."
    )
