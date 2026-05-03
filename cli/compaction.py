"""上下文压缩 — TUI 和 headless agent loop 共用。"""

from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# 模型 context window 映射（按前缀匹配，单位 token）
# ---------------------------------------------------------------------------

_MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("deepseek", 64_000),
    ("gpt-4o", 128_000),
    ("gpt-4", 128_000),
    ("gpt-3.5", 16_000),
    ("gemini-3", 128_000),
    ("gemini-2", 1_000_000),
    ("gemini", 128_000),
    ("claude-opus", 200_000),
    ("claude-sonnet", 200_000),
    ("claude", 200_000),
    ("minimax", 128_000),
    ("kimi", 128_000),
    ("qwen", 128_000),
    ("longcat", 64_000),
    ("mistral", 128_000),
    ("step", 64_000),
]

_DEFAULT_CONTEXT_WINDOW = 64_000
COMPACT_RATIO = 0.25
TAIL_KEEP = 4

_CODE_RE = re.compile(r"\d{6}")


def get_context_window(model_name: str) -> int:
    lower = model_name.lower()
    for prefix, window in _MODEL_CONTEXT_WINDOWS:
        if prefix in lower:
            return window
    return _DEFAULT_CONTEXT_WINDOW


def get_compact_threshold(model_name: str) -> int:
    return int(get_context_window(model_name) * COMPACT_RATIO)


# ---------------------------------------------------------------------------
# Token 估算
# ---------------------------------------------------------------------------


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += max(len(content) // 2, len(content.encode("utf-8")) // 3)
        for tc in m.get("tool_calls", []):
            args_str = json.dumps(tc.get("args", {}), ensure_ascii=False)
            total += len(args_str) // 3
    return total


# ---------------------------------------------------------------------------
# 分层消息序列化（保留工具结果中的关键数据）
# ---------------------------------------------------------------------------


def _summarize_tool_result(name: str, content: str, max_len: int = 400) -> str:
    """从工具返回结果中提取关键信息而不是粗暴截断。"""
    if len(content) <= max_len:
        return content

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content[:max_len] + "…"

    # analyze_stock: 按返回结构区分 price/diagnose 模式
    if name == "analyze_stock" and isinstance(data, dict):
        if "data" in data and isinstance(data.get("data"), list):
            kept: dict[str, Any] = {}
            for key in ("code", "latest_close", "latest_date", "days"):
                if key in data:
                    kept[key] = data[key]
            kept["data"] = data["data"][-5:]
            return json.dumps(kept, ensure_ascii=False)[:max_len]
        kept = {}
        for key in (
            "code",
            "name",
            "channel",
            "phase",
            "trigger_signals",
            "exit_signals",
            "health",
            "positions",
            "message",
        ):
            if key in data:
                kept[key] = data[key]
        if kept:
            return json.dumps(kept, ensure_ascii=False)[:max_len]

    if name == "analyze_stock" and isinstance(data, list):
        return json.dumps(data[-5:], ensure_ascii=False)[:max_len]

    # portfolio — 按结构区分 view/diagnose
    if name == "portfolio" and isinstance(data, dict):
        if "diagnostics" in data:
            kept = {}
            for key in (
                "portfolio_id",
                "position_count",
                "successful_count",
                "failed_count",
                "free_cash",
                "diagnostics",
            ):
                if key in data:
                    kept[key] = data[key]
            if kept:
                return json.dumps(kept, ensure_ascii=False)[:max_len]
        else:
            kept = {}
            for key in ("portfolio_id", "free_cash", "position_count", "positions", "message"):
                if key in data:
                    kept[key] = data[key]
            if kept:
                return json.dumps(kept, ensure_ascii=False)[:max_len]

    # 通用：保留 error/message/status 等顶层键
    if isinstance(data, dict):
        kept = {}
        for key in ("error", "message", "status", "code", "name", "result"):
            if key in data:
                kept[key] = data[key]
        if kept:
            return json.dumps(kept, ensure_ascii=False)[:max_len]

    return content[:max_len] + "…"


def serialize_messages_for_compaction(messages: list[dict[str, Any]]) -> str:
    """将消息序列化为压缩输入，工具结果做智能摘要而非粗暴截断。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "")
        if role == "tool":
            name = m.get("name", "tool")
            content = m.get("content", "")
            summary = _summarize_tool_result(name, content)
            lines.append(f"[tool:{name}] {summary}")
        elif role == "assistant" and m.get("tool_calls"):
            calls = ", ".join(
                f"{tc.get('name', '?')}({json.dumps(tc.get('args', {}), ensure_ascii=False)[:80]})"
                for tc in m["tool_calls"]
            )
            lines.append(f"[assistant:tool_call] {calls}")
            if m.get("content"):
                lines.append(f"[assistant] {m['content']}")
        else:
            content = m.get("content", "") or ""
            lines.append(f"[{role}] {content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Memory Flush — 压缩前提取持久事实
# ---------------------------------------------------------------------------

_FLUSH_PROMPT = """请从以下对话片段中提取用户的持久偏好或重要事实，每条一行。
只提取以下类型的信号：
- 投资偏好（如"不买ST股"、"偏好大盘蓝筹"、"不追涨"）
- 风险偏好（如"止损线8%"、"仓位不超过20%"）
- 重要结论（如"000001适合长期持有"、"银行板块看好"）

如果没有值得记忆的偏好或事实，只输出"无"。
不要提取临时操作指令或工具调用细节。"""


def flush_memory_before_compaction(
    messages: list[dict[str, Any]],
    provider: Any,
) -> None:
    """在压缩前，用 LLM 从待压缩消息中提取 preference 存入记忆。"""
    try:
        from cli.memory import extract_stock_codes
        from integrations.local_db import save_memory
    except ImportError:
        return

    # 只从 user/assistant 消息中提取，跳过工具结果
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "")
        if role in ("user", "assistant"):
            content = m.get("content", "")
            if content and len(content) > 10:
                lines.append(f"[{role}] {content[:300]}")
    if len(lines) < 2:
        return

    text = "\n".join(lines[-20:])
    try:
        chunks = list(
            provider.chat_stream(
                [{"role": "user", "content": text}],
                [],
                _FLUSH_PROMPT,
            )
        )
        result = "".join(c.get("text", "") for c in chunks if c.get("type") == "text_delta")
        if not result or "无" in result.strip()[:5]:
            return

        all_text = " ".join(m.get("content", "") or "" for m in messages)
        codes = extract_stock_codes(all_text)

        for line in result.strip().split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line and len(line) >= 5 and "无" not in line[:3]:
                save_memory("preference", line, codes=",".join(codes[:10]))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 压缩 prompt
# ---------------------------------------------------------------------------

COMPACTION_PROMPT = """请将以下对话历史总结为简洁的上下文摘要，保留关键信息：
1. 用户的目标和意图
2. 已完成的操作和结果（保留具体股票代码、价格、信号等数据）
3. 工具调用的关键发现和结论
4. 未完成的任务

用中文输出，控制在 500 字以内。只输出摘要，不要其他内容。"""


# ---------------------------------------------------------------------------
# 执行压缩
# ---------------------------------------------------------------------------


def compact_messages(
    messages: list[dict[str, Any]],
    provider: Any,
    model_name: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """检查并执行上下文压缩。

    Returns (messages, compacted) — 如果未压缩则原样返回。
    """
    threshold = get_compact_threshold(model_name) if model_name else 12_000
    if len(messages) <= TAIL_KEEP + 2 or estimate_tokens(messages) <= threshold:
        return messages, False

    head = messages[:-TAIL_KEEP]
    tail = messages[-TAIL_KEEP:]

    # 压缩前先提取持久偏好到记忆
    flush_memory_before_compaction(head, provider)

    head_text = serialize_messages_for_compaction(head)

    try:
        chunks = list(
            provider.chat_stream(
                [{"role": "user", "content": head_text}],
                [],
                COMPACTION_PROMPT,
            )
        )
        summary = "".join(c.get("text", "") for c in chunks if c.get("type") == "text_delta")
        if summary and len(summary) >= 20:
            compacted = [
                {"role": "user", "content": f"[对话摘要]\n{summary}"},
                {"role": "assistant", "content": "好的，我已了解之前的对话上下文，请继续。"},
            ] + tail
            return compacted, True
    except Exception:
        pass

    return messages, False
