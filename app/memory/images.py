"""Bounded Telegram-image analysis for durable per-user gathered memory."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping

from app.llm.client import generate
from app.memory.store import attach_image_analysis, gathered_for_user
from app.settings import settings
from app.telegram.client import (
    MAX_TELEGRAM_IMAGE_BYTES,
    download_file,
    get_file,
)

MAX_IMAGE_ANALYSIS_CHARS = 1_600


def _request_image(request: Mapping[str, object]) -> tuple[dict[str, object], int, int] | None:
    image = request.get("image")
    author = request.get("author")
    trigger = request.get("trigger")
    if not isinstance(image, Mapping) or not isinstance(author, Mapping):
        return None
    user_id = author.get("id")
    message_id = trigger.get("message_id") if isinstance(trigger, Mapping) else None
    file_id = image.get("file_id")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or isinstance(message_id, bool)
        or not isinstance(message_id, int)
        or message_id <= 0
        or not isinstance(file_id, str)
        or not file_id
        or len(file_id) > 256
    ):
        return None
    return dict(image), user_id, message_id


def _extract_analysis(raw: str) -> str:
    text = " ".join(raw.split())[:4_000]
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
        if text.casefold().startswith("json"):
            text = text[4:].strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return text[:MAX_IMAGE_ANALYSIS_CHARS]
    if not isinstance(value, Mapping):
        return text[:MAX_IMAGE_ANALYSIS_CHARS]
    ocr = value.get("ocr")
    summary = value.get("summary")
    parts: list[str] = []
    if isinstance(ocr, str) and ocr.strip():
        parts.append("OCR: " + " ".join(ocr.split()))
    if isinstance(summary, str) and summary.strip():
        parts.append("Описание: " + " ".join(summary.split()))
    return "\n".join(parts)[:MAX_IMAGE_ANALYSIS_CHARS]


def analyze_image(request: Mapping[str, object]) -> str | None:
    """Download one Telegram image, ask Gemma for OCR/description, and persist it."""
    parsed = _request_image(request)
    if parsed is None:
        return None
    image, user_id, message_id = parsed
    chat_id = request.get("chat_id")
    memory_epoch = request.get("memory_epoch")
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or chat_id == 0
        or (memory_epoch is not None and not isinstance(memory_epoch, int))
    ):
        return None
    for item in gathered_for_user(chat_id, user_id):
        if (
            item.get("source_message_id") == message_id
            and isinstance(item.get("image_analysis"), str)
            and item.get("image_analysis")
        ):
            return str(item["image_analysis"])

    file_info = get_file(str(image["file_id"]))
    file_path = file_info.get("file_path")
    if not isinstance(file_path, str):
        return None
    content = download_file(file_path, max_bytes=MAX_TELEGRAM_IMAGE_BYTES)
    if not content:
        return None
    mime_type = str(image.get("mime_type") or "image/jpeg")
    data_uri = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
    caption = ""
    trigger = request.get("trigger")
    if isinstance(trigger, Mapping) and isinstance(trigger.get("text"), str):
        caption = trigger["text"][:600]
    messages = [
        {
            "role": "system",
            "content": (
                "Ты анализируешь изображение для памяти Telegram-бота. Верни только "
                "JSON-объект с двумя строковыми полями: ocr (краткий распознанный "
                "текст, либо пустая строка) и summary (краткое нейтральное описание "
                "изображения). Не выдумывай нечитаемый текст, не определяй личности "
                "и не делай выводов о защищённых признаках."
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Проанализируй это изображение. Подпись пользователя: "
                    + (caption or "нет"),
                },
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        },
    ]
    raw = asyncio.run(
        generate(messages, thinking=False, model=settings.LLM_MODEL_VISION)
    )
    analysis = _extract_analysis(raw)
    if not analysis:
        return None
    stored = attach_image_analysis(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        analysis=analysis,
        memory_epoch=memory_epoch,
    )
    if not stored:
        raise RuntimeError("image memory write failed")
    return analysis
