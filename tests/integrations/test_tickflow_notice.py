from __future__ import annotations

import integrations.tickflow_notice as tn


def test_is_tickflow_rate_limited_error_markers() -> None:
    assert tn.is_tickflow_rate_limited_error("TickFlow HTTP 429: RATE_LIMITED")
    assert tn.is_tickflow_rate_limited_error("too many requests")
    assert not tn.is_tickflow_rate_limited_error("connection reset by peer")


def test_append_tickflow_limit_hint(monkeypatch) -> None:
    monkeypatch.setattr(tn, "_EVENTS", [])
    text = "扫描完成"
    assert tn.append_tickflow_limit_hint(text) == text

    tn.record_tickflow_limit_event("TickFlow HTTP 429: RATE_LIMITED")
    out = tn.append_tickflow_limit_hint(text)
    assert "触发数据源限制，升级数据源：" in out
