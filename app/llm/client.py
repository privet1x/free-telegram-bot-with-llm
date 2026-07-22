"""Lazy NVIDIA NIM clients with sanitized, stable error classification."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Final, Sequence

from app.settings import Settings, settings

FLASH_TEMPERATURE: Final = 0.4
FLASH_MAX_COMPLETION_TOKENS: Final = 2_048
FLASH_TIMEOUT_SECONDS: Final = 180.0
PRO_TIMEOUT_SECONDS: Final = 75.0
_MAX_RESPONSE_CHARS: Final = 64_000
_THINK_BLOCK: Final = re.compile(
    r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE
)
_THINK_TAG: Final = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)


class LLMRetryableError(RuntimeError):
    """A transient provider failure safe to expose to job state/logs."""

    retryable = True

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(error_class)


class LLMPermanentError(RuntimeError):
    """A permanent provider failure safe to expose to job state/logs."""

    retryable = False

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(error_class)


def _create_flash_client(model: str, api_key: str) -> Any:
    # Keep this import on the worker path so webhook/health cold starts do not
    # import LangChain and its provider stack.
    from langchain_nvidia_ai_endpoints import ChatNVIDIA

    base = ChatNVIDIA(
        model=model,
        api_key=api_key,
        temperature=FLASH_TEMPERATURE,
        max_completion_tokens=FLASH_MAX_COMPLETION_TOKENS,
        timeout=FLASH_TIMEOUT_SECONDS,
    )
    return base.with_thinking_mode(enabled=False)


def get_flash_client(config: Settings = settings) -> Any:
    """Build an isolated non-thinking NVIDIA client for one invoke."""
    if (
        not config.NVIDIA_API_KEY
        or not isinstance(config.LLM_MODEL_FAST, str)
        or not config.LLM_MODEL_FAST.strip()
    ):
        raise LLMPermanentError("provider_configuration")
    return _create_flash_client(config.LLM_MODEL_FAST, config.NVIDIA_API_KEY)


def get_pro_client(config: Settings = settings) -> Any:
    if (
        not config.NVIDIA_API_KEY
        or not isinstance(config.LLM_MODEL_SMART, str)
        or not config.LLM_MODEL_SMART.strip()
    ):
        raise LLMPermanentError("provider_configuration")
    from langchain_nvidia_ai_endpoints import ChatNVIDIA

    base = ChatNVIDIA(
        model=config.LLM_MODEL_SMART,
        api_key=config.NVIDIA_API_KEY,
        temperature=0.2,
        max_completion_tokens=2_048,
        timeout=PRO_TIMEOUT_SECONDS,
    )
    return base.with_thinking_mode(enabled=False)


def _integer_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def _status_from_object(value: object) -> int | None:
    for name in ("status_code", "status"):
        status = _integer_status(getattr(value, name, None))
        if status is not None:
            return status
    response = getattr(value, "response", None)
    if response is not None and response is not value:
        for name in ("status_code", "status"):
            status = _integer_status(getattr(response, name, None))
            if status is not None:
                return status
    return None


def _provider_status(exc: BaseException, client: object) -> int | None:
    """Inspect status attributes only; never inspect or stringify error bodies."""
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        status = _status_from_object(current)
        if status is not None:
            return status
        current = current.__cause__ or current.__context__

    # ChatNVIDIA 1.4.3 raises a generic Exception for HTTP errors but retains
    # the response on the bound model's private async transport.
    model = getattr(client, "bound", client)
    transport = getattr(model, "_async_client", None)
    response = getattr(transport, "last_response", None)
    return _status_from_object(response) if response is not None else None


def _is_transport_failure(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, OSError)):
        return True
    transport_modules = ("aiohttp", "httpx", "requests", "urllib3")
    markers = (
        "Connection",
        "Connector",
        "Disconnected",
        "Network",
        "Payload",
        "Protocol",
        "Timeout",
        "Transport",
    )
    for error_type in type(exc).__mro__:
        module = error_type.__module__
        name = error_type.__name__
        if module.startswith(transport_modules) and any(
            marker in name for marker in markers
        ):
            return True
    return False


def _classified_error(exc: BaseException, client: object) -> RuntimeError:
    status = _provider_status(exc, client)
    if status == 408:
        return LLMRetryableError("provider_timeout")
    if status == 429:
        return LLMRetryableError("provider_rate_limited")
    if status is not None and status >= 500:
        return LLMRetryableError("provider_unavailable")
    if status in {401, 403}:
        return LLMPermanentError("provider_auth")
    if status is not None and 400 <= status < 500:
        return LLMPermanentError("provider_request_rejected")
    if _is_transport_failure(exc):
        return LLMRetryableError("provider_transport")
    return LLMPermanentError("provider_invalid_response")


def _response_text(result: object) -> str:
    content = getattr(result, "content", None)
    if not isinstance(content, str):
        raise LLMPermanentError("provider_invalid_response")
    text = _THINK_BLOCK.sub("", content).strip()
    if _THINK_TAG.search(text):
        raise LLMPermanentError("provider_invalid_response")
    if not text or len(text) > _MAX_RESPONSE_CHARS:
        raise LLMPermanentError("provider_invalid_response")
    return text


async def generate_flash(
    messages: Sequence[object],
    *,
    config: Settings = settings,
    client: Any | None = None,
) -> str:
    """Generate a bounded Flash response or raise a sanitized classified error."""
    llm = client or get_flash_client(config)
    try:
        async with asyncio.timeout(FLASH_TIMEOUT_SECONDS):
            result = await llm.ainvoke(list(messages))
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise LLMRetryableError("provider_timeout") from None
    except (LLMRetryableError, LLMPermanentError):
        raise
    except Exception as exc:
        raise _classified_error(exc, llm) from None
    return _response_text(result)


async def generate_pro(
    messages: Sequence[object],
    *,
    config: Settings = settings,
    client: Any | None = None,
) -> str:
    """Generate a bounded non-thinking Pro response with Flash-equivalent safety."""
    llm = client or get_pro_client(config)
    try:
        async with asyncio.timeout(PRO_TIMEOUT_SECONDS):
            result = await llm.ainvoke(list(messages))
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise LLMRetryableError("provider_timeout") from None
    except (LLMRetryableError, LLMPermanentError):
        raise
    except Exception as exc:
        raise _classified_error(exc, llm) from None
    return _response_text(result)
