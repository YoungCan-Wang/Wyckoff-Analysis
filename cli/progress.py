"""后台任务进度上报 — ContextVar + 全局便捷函数。"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

# callback signature: (stage, detail, progress) -> None
_reporter: ContextVar[Callable | None] = ContextVar("_reporter", default=None)


def set_reporter(cb: Callable | None) -> None:
    _reporter.set(cb)


def report_progress(stage: str, detail: str = "", progress: float = -1.0) -> None:
    """有 reporter 时上报，没有时 no-op。"""
    cb = _reporter.get()
    if cb is not None:
        cb(stage, detail, progress)
