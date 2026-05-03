"""
TickFlow 限流提示：统一文案、错误识别、进程内短期事件记录。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

TICKFLOW_UPGRADE_URL = os.getenv(
    "TICKFLOW_UPGRADE_URL",
    "https://tickflow.org/auth/register?ref=5N4NKTCPL4",
).strip()
TICKFLOW_LIMIT_HINT = f"触发数据源限制，升级数据源：{TICKFLOW_UPGRADE_URL}"

_EVENT_TTL_SECONDS = max(int(os.getenv("TICKFLOW_LIMIT_NOTICE_TTL_SECONDS", "1800")), 60)
_EVENTS: list[float] = []
_EVENTS_LOCK = threading.Lock()


def is_tickflow_rate_limited_error(err: Exception | str | None) -> bool:
    text = str(err or "").lower()
    if not text:
        return False
    markers = (
        "tickflow http 429",
        "http 429",
        "rate_limited",
        "too many requests",
        "限流",
    )
    return any(m in text for m in markers)


def record_tickflow_limit_event(err: Exception | str | None = None) -> None:
    if err is not None and not is_tickflow_rate_limited_error(err):
        return
    now = time.monotonic()
    with _EVENTS_LOCK:
        _EVENTS.append(now)
        cutoff = now - _EVENT_TTL_SECONDS
        while _EVENTS and _EVENTS[0] < cutoff:
            _EVENTS.pop(0)


def has_recent_tickflow_limit_event() -> bool:
    now = time.monotonic()
    with _EVENTS_LOCK:
        cutoff = now - _EVENT_TTL_SECONDS
        while _EVENTS and _EVENTS[0] < cutoff:
            _EVENTS.pop(0)
        return bool(_EVENTS)


def append_tickflow_limit_hint(text: Any) -> str:
    content = str(text or "")
    if not has_recent_tickflow_limit_event():
        return content
    if TICKFLOW_LIMIT_HINT in content:
        return content
    if not content.strip():
        return TICKFLOW_LIMIT_HINT
    return f"{content.rstrip()}\n\n⚠️ {TICKFLOW_LIMIT_HINT}"
