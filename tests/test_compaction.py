# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from cli.compaction import (
    COMPACT_RATIO,
    TAIL_KEEP,
    _summarize_tool_result,
    compact_messages,
    estimate_tokens,
    get_compact_threshold,
    get_context_window,
    serialize_messages_for_compaction,
)


class TestGetContextWindow:
    def test_deepseek(self):
        assert get_context_window("deepseek-v4-flash") == 64_000

    def test_claude(self):
        assert get_context_window("claude-sonnet-4-20260514") == 200_000

    def test_gemini_2(self):
        assert get_context_window("gemini-2.5-flash") == 1_000_000

    def test_unknown_fallback(self):
        assert get_context_window("some-unknown-model") == 64_000

    def test_threshold_ratio(self):
        assert get_compact_threshold("claude-sonnet-4") == int(200_000 * COMPACT_RATIO)


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens([]) == 0

    def test_text_message(self):
        tokens = estimate_tokens([{"role": "user", "content": "hello world"}])
        assert tokens > 0

    def test_tool_calls_counted(self):
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "analyze_stock", "args": {"code": "000001"}}],
        }
        assert estimate_tokens([msg]) > 0

    def test_chinese_text(self):
        tokens = estimate_tokens([{"role": "user", "content": "你好世界，今天天气不错"}])
        assert tokens > 0


class TestSummarizeToolResult:
    def test_short_content_unchanged(self):
        assert _summarize_tool_result("any_tool", "short") == "short"

    def test_analyze_stock_keeps_key_fields(self):
        data = {
            "code": "000001",
            "name": "平安银行",
            "phase": "accumulation",
            "trigger_signals": ["Spring"],
            "health": "STRONG",
            "extra_large_data": "x" * 2000,
        }
        result = _summarize_tool_result("analyze_stock", json.dumps(data, ensure_ascii=False))
        parsed = json.loads(result)
        assert parsed["code"] == "000001"
        assert parsed["health"] == "STRONG"
        assert "extra_large_data" not in parsed

    def test_analyze_stock_keeps_tail(self):
        prices = [{"date": f"2024-01-{i:02d}", "close": 10 + i} for i in range(1, 21)]
        result = _summarize_tool_result("analyze_stock", json.dumps(prices))
        parsed = json.loads(result)
        assert len(parsed) == 5
        assert parsed[0]["date"] == "2024-01-16"

    def test_generic_keeps_error_message(self):
        data = {"error": "timeout", "status": 500, "huge_payload": "y" * 2000}
        result = _summarize_tool_result("some_tool", json.dumps(data))
        parsed = json.loads(result)
        assert parsed["error"] == "timeout"
        assert parsed["status"] == 500
        assert "huge_payload" not in parsed

    def test_non_json_truncated(self):
        long_text = "a" * 1000
        result = _summarize_tool_result("any_tool", long_text)
        assert len(result) <= 401
        assert result.endswith("…")


class TestSerializeMessages:
    def test_tool_message(self):
        msgs = [{"role": "tool", "name": "analyze_stock", "content": '{"price":10}'}]
        text = serialize_messages_for_compaction(msgs)
        assert "[tool:analyze_stock]" in text

    def test_assistant_tool_call(self):
        msgs = [
            {
                "role": "assistant",
                "content": "查一下",
                "tool_calls": [{"name": "analyze_stock", "args": {"code": "000001"}}],
            }
        ]
        text = serialize_messages_for_compaction(msgs)
        assert "[assistant:tool_call]" in text
        assert "[assistant] 查一下" in text

    def test_user_message(self):
        msgs = [{"role": "user", "content": "帮我看看600519"}]
        text = serialize_messages_for_compaction(msgs)
        assert "[user] 帮我看看600519" in text


class TestCompactMessages:
    class FakeProvider:
        def chat_stream(self, messages, tools, system_prompt):
            return [{"type": "text_delta", "text": "这是一段压缩后的摘要，包含了用户之前对股票的分析讨论。"}]

    def _make_messages(self, n: int) -> list[dict]:
        msgs = []
        for i in range(n):
            msgs.append({"role": "user", "content": f"消息内容 {i} " + "这是一段很长的中文测试文本用来占据token空间" * 50})
            msgs.append({"role": "assistant", "content": f"回复 {i} " + "这是助手的回复内容同样需要足够长" * 50})
        return msgs

    def test_no_compaction_when_short(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        result, compacted = compact_messages(msgs, self.FakeProvider(), "deepseek")
        assert not compacted
        assert result is msgs

    def test_compaction_triggers_on_large_context(self):
        msgs = self._make_messages(30)
        result, compacted = compact_messages(msgs, self.FakeProvider(), "deepseek")
        assert compacted
        assert len(result) < len(msgs)
        assert result[0]["content"].startswith("[对话摘要]")
        assert result[-1] == msgs[-1]

    def test_tail_messages_preserved(self):
        msgs = self._make_messages(30)
        result, compacted = compact_messages(msgs, self.FakeProvider(), "deepseek")
        assert compacted
        tail = msgs[-TAIL_KEEP:]
        assert result[-TAIL_KEEP:] == tail

    def test_failed_compaction_returns_original(self):
        class FailProvider:
            def chat_stream(self, messages, tools, system_prompt):
                raise RuntimeError("LLM unavailable")

        msgs = self._make_messages(30)
        result, compacted = compact_messages(msgs, FailProvider(), "deepseek")
        assert not compacted
        assert result is msgs
