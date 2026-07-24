"""Privacy-safe bounded latency measurements (no message or user identifiers)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_LOCK = threading.Lock()
_SAMPLES: dict[str, list[float]] = {}
_MAX_SAMPLES = 1_000


@contextmanager
def timed(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, time.perf_counter() - started)


def record(name: str, seconds: float) -> None:
    if not isinstance(name, str) or not name or not isinstance(seconds, (int, float)):
        return
    with _LOCK:
        samples = _SAMPLES.setdefault(name, [])
        samples.append(max(float(seconds), 0.0))
        del samples[:-_MAX_SAMPLES]


def snapshot() -> dict[str, dict[str, float | int]]:
    with _LOCK:
        result: dict[str, dict[str, float | int]] = {}
        for name, values in _SAMPLES.items():
            if not values:
                continue
            ordered = sorted(values)
            result[name] = {
                "count": len(ordered),
                "p50": ordered[(len(ordered) - 1) // 2],
                "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
            }
        return result


def reset() -> None:
    with _LOCK:
        _SAMPLES.clear()
