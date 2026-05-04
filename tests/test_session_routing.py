from __future__ import annotations

from agents.session_manager import _agent_input_text, _should_force_diagnose_route, _strip_route_hint


def test_agent_input_adds_private_hint_for_single_stock_diagnose():
    text = _agent_input_text("帮我看看 000001")

    assert _should_force_diagnose_route("帮我看看 000001")
    assert "mode" in text
    assert "diagnose" in text
    assert "price" in text
    assert _strip_route_hint(text) == "帮我看看 000001"


def test_agent_input_does_not_force_price_route():
    text = _agent_input_text("600519 最近走势怎么样")

    assert not _should_force_diagnose_route("600519 最近走势怎么样")
    assert text == "600519 最近走势怎么样"
