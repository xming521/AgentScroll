"""Shared pacing for public detail-page requests."""

from __future__ import annotations

import os
import random
import threading
import time


_MIN_DELAY_ENV = "AGENTSCROLL_DETAIL_DELAY_MIN"
_MAX_DELAY_ENV = "AGENTSCROLL_DETAIL_DELAY_MAX"
_DEFAULT_MIN_DELAY = 1.5
_DEFAULT_MAX_DELAY = 3.8
_MAX_CONFIGURED_DELAY = 60.0

_lock = threading.Lock()
_next_detail_request_at: dict[str, float] = {}


def _seconds_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, min(value, _MAX_CONFIGURED_DELAY))


def wait_for_detail_request(platform: str) -> None:
    """Space detail requests within one platform without blocking other platforms."""
    minimum = _seconds_from_env(_MIN_DELAY_ENV, _DEFAULT_MIN_DELAY)
    maximum = _seconds_from_env(_MAX_DELAY_ENV, _DEFAULT_MAX_DELAY)
    if maximum < minimum:
        minimum, maximum = maximum, minimum

    now = time.monotonic()
    key = platform.strip().lower() or "detail"
    with _lock:
        scheduled_at = max(now, _next_detail_request_at.get(key, 0.0))
        _next_detail_request_at[key] = scheduled_at + random.uniform(minimum, maximum)
    remaining = scheduled_at - now
    if remaining > 0:
        time.sleep(remaining)
