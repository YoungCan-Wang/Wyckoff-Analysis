"""
Agent 跨会话记忆 — 会话摘要提取 + 记忆注入。
"""

from __future__ import annotations

import re
from typing import Any

_SESSION_SUMMARY_PROMPT = """请将以下对话提取为结构化记忆（中文，≤300字）：
1. 讨论了哪些股票（代码+结论）
2. 用户的操作意图和决策
3. 重要的市场判断
4. 用户表达的偏好或禁忌（如"不要推荐ST股"、"不追涨"等）
每条记忆一行，前缀标注类型：[股票] / [决策] / [市场] / [偏好]
只保留有价值的结论，忽略寒暄和工具调用细节。"""

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CJK_RE = re.compile(r"[一-鿿]{2,4}")
_STOPWORDS = frozenset(
    list("的了吗呢啊哦呀吧嘛是不在有我你他它这那都也就要会")
    + [
        "可以",
        "一个",
        "什么",
        "怎么",
        "如何",
        "看看",
        "一下",
        "帮我",
        "请问",
        "能否",
        "可否",
        "这个",
        "那个",
        "我的",
        "你的",
        "现在",
    ]
)


def extract_stock_codes(text: str) -> list[str]:
    return list(dict.fromkeys(_CODE_RE.findall(text)))


def _extract_keywords(text: str) -> list[str]:
    text = _CODE_RE.sub("", text)
    segments = _CJK_RE.findall(text)
    # 长片段拆成 2-gram 提升召回率
    bigrams: list[str] = []
    for seg in segments:
        if len(seg) <= 2:
            bigrams.append(seg)
        else:
            for i in range(len(seg) - 1):
                bigrams.append(seg[i : i + 2])
    return [s for s in dict.fromkeys(bigrams) if s not in _STOPWORDS][:5]


def _has_tool_calls(messages: list[dict]) -> bool:
    return any(m.get("tool_calls") for m in messages)


def save_session_summary(messages: list[dict], provider: Any) -> None:
    if not messages or len(messages) < 4 or not _has_tool_calls(messages):
        return
    try:
        from integrations.local_db import save_memory

        lines = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "tool":
                content = content[:200] + "..." if len(content) > 200 else content
            if content:
                lines.append(f"[{role}] {content}")
        dialog_text = "\n".join(lines[-40:])

        chunks = list(
            provider.chat_stream(
                [{"role": "user", "content": dialog_text}],
                [],
                _SESSION_SUMMARY_PROMPT,
            )
        )
        summary = "".join(c.get("text", "") for c in chunks if c.get("type") == "text_delta")
        if not summary or len(summary) < 10:
            return

        all_text = " ".join(m.get("content", "") or "" for m in messages)
        codes = extract_stock_codes(all_text)
        codes_str = ",".join(codes[:20])

        # 分离偏好记忆单独存储
        session_lines = []
        for line in summary.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("[偏好]"):
                save_memory("preference", stripped[4:].strip(), codes=codes_str)
            else:
                session_lines.append(stripped)

        session_text = "\n".join(session_lines).strip()
        if session_text and len(session_text) >= 10:
            save_memory("session", session_text, codes=codes_str)
    except Exception:
        pass


def build_memory_context(user_message: str) -> str:
    try:
        from integrations.local_db import (
            get_recent_memories,
            search_memory_hybrid,
        )

        codes = extract_stock_codes(user_message)
        keywords = _extract_keywords(user_message)

        # Hybrid search: FTS5 + 代码 + 关键词 + 时间衰减
        memories = search_memory_hybrid(
            query_text=user_message,
            codes=codes or None,
            keywords=keywords or None,
            limit=8,
            decay_half_life_days=30.0,
        )

        # 偏好记忆始终置顶（hybrid search 已包含，但确保完整性）
        prefs = get_recent_memories(memory_type="preference", limit=5)

        if not memories and not prefs:
            return ""

        lines = [""]
        if prefs:
            lines.append("# 用户偏好")
            for p in prefs:
                content = str(p.get("content", "")).strip()
                if content:
                    lines.append(f"- {content}")

        if memories:
            lines.append("# 历史记忆")
            for m in memories[:8]:
                date_str = str(m.get("created_at", ""))[:10]
                content = str(m.get("content", "")).strip()
                if len(content) > 200:
                    content = content[:200] + "…"
                lines.append(f"- [{date_str}] {content}")
        return "\n".join(lines)
    except Exception:
        return ""
