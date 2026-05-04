from __future__ import annotations

import json

from cli.scratchpad import AgentScratchpad
from cli.tool_results import format_tool_result_for_context


def test_scratchpad_records_jsonl_and_redacts_secrets(tmp_path):
    scratchpad = AgentScratchpad("看看 000001", session_id="session_x", scratchpad_dir=tmp_path)
    scratchpad.record_tool_result(
        "web_fetch",
        {"url": "https://example.com", "api_key": "secret-value"},
        {"ok": True, "token": "secret-token"},
        duration_ms=12,
    )
    scratchpad.record_final("完成", input_tokens=10, output_tokens=5, elapsed_s=0.5)

    lines = [json.loads(line) for line in scratchpad.path.read_text(encoding="utf-8").splitlines()]

    assert [line["type"] for line in lines] == ["init", "tool_result", "final"]
    tool_entry = lines[1]
    assert tool_entry["args"]["api_key"] == "***REDACTED***"
    assert tool_entry["result"]["token"] == "***REDACTED***"
    assert tool_entry["durationMs"] == 12


def test_large_tool_result_is_persisted_with_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path))
    result = {"rows": ["x" * 1000 for _ in range(60)]}

    content = format_tool_result_for_context("screen_stocks", "call_1", result, max_chars=1000)

    assert "工具结果已保存到" in content
    assert "预览:" in content
    stored = list((tmp_path / "tool-results").glob("*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text(encoding="utf-8"))["rows"][0] == "x" * 1000
