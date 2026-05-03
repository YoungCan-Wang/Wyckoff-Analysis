"""Shared guardrails and constants for the agent loop."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

MAX_TOOL_ROUNDS = 15
MAX_INCOMPLETE_TOOL_RETRIES = 2
DOOM_LOOP_WINDOW = 6
DOOM_LOOP_THRESHOLD = 3


@dataclass(frozen=True)
class TurnExpectation:
    """A tool call the loop considers mandatory for the current turn."""

    required_tool: str
    reason: str
    required_args: dict[str, str] = field(default_factory=dict)


_PORTFOLIO_VIEW_PHRASES = (
    "我有什么持仓",
    "我的持仓有什么",
    "持仓列表",
    "我买了啥",
    "我买了什么",
    "持仓情况",
    "仓位情况",
)

_PORTFOLIO_DIAGNOSE_PHRASES = (
    "我持仓怎么样",
    "帮我看看持仓",
    "帮我看下持仓",
    "持仓健康吗",
    "持仓体检",
    "体检一下持仓",
    "帮我审一下持仓",
    "审判我的持仓",
    "审一下持仓",
)

_GENERIC_DIAGNOSE_HINTS = (
    "分析",
    "走势",
    "未来走势",
    "日线",
)

_PURE_FOLLOWUP_DIAGNOSE_PHRASES = (
    "做一下体检",
    "做个体检",
    "体检一下",
    "体检",
    "审判",
    "健康吗",
    "健康",
    "诊断",
    "审一下",
)

_AFFIRMATIVE_PHRASES = (
    "要",
    "要的",
    "好的",
    "可以",
    "行",
    "来吧",
    "嗯",
    "好",
)

_PORTFOLIO_FOLLOWUP_REFERENCES = (
    "他们",
    "它们",
    "这些",
    "这几个",
    "几个股票",
    "几只",
    "上面",
    "上述",
    "这些票",
    "这几只",
    "我的持仓",
    "持仓股票",
)

_PORTFOLIO_CONTEXT_MARKERS = (
    "持仓",
    "仓位",
    "持股",
    "成本价",
    "买入日",
    "代码 | 名称 | 持股",
    "总可用",
    "现金",
    "portfolio",
)

_TOOL_CN_NAMES = {
    "portfolio": "持仓数据",
    "analyze_stock": "个股分析",
}


def _normalize_text(text: str) -> str:
    return str(text or "").strip().lower()


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return str(content) if isinstance(content, str) else ""


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            text = _message_text(message)
            if text:
                return _normalize_text(text)
    return ""


def _recent_context_text(messages: list[dict[str, Any]], *, limit: int = 4) -> str:
    pieces: list[str] = []
    for message in messages[-limit:]:
        text = _message_text(message)
        if text:
            pieces.append(_normalize_text(text))
    return "\n".join(pieces)


def resolve_turn_expectation(messages: list[dict[str, Any]]) -> TurnExpectation | None:
    """Infer whether this turn must call a specific tool before answering."""

    if not messages:
        return None

    last_user = _last_user_text(messages)
    if not last_user:
        return None

    if any(phrase in last_user for phrase in _PORTFOLIO_VIEW_PHRASES):
        return TurnExpectation(
            required_tool="portfolio",
            reason="持仓列表查询必须先拉真实持仓数据。",
            required_args={"mode": "view"},
        )

    if any(phrase in last_user for phrase in _PORTFOLIO_DIAGNOSE_PHRASES):
        return TurnExpectation(
            required_tool="portfolio",
            reason="持仓体检必须先调用持仓诊断工具。",
            required_args={"mode": "diagnose"},
        )

    previous_context = _recent_context_text(messages[:-1], limit=4)
    if (
        any(phrase in last_user for phrase in _PURE_FOLLOWUP_DIAGNOSE_PHRASES)
        or (
            any(hint in last_user for hint in _GENERIC_DIAGNOSE_HINTS)
            and any(ref in last_user for ref in _PORTFOLIO_FOLLOWUP_REFERENCES)
        )
    ) and any(marker in previous_context for marker in _PORTFOLIO_CONTEXT_MARKERS):
        return TurnExpectation(
            required_tool="portfolio",
            reason="上一轮上下文已经明确在讨论持仓，这一轮体检需要继续做持仓诊断。",
            required_args={"mode": "diagnose"},
        )

    if (
        last_user in _AFFIRMATIVE_PHRASES
        and (
            any(hint in previous_context for hint in _GENERIC_DIAGNOSE_HINTS)
            or any(hint in previous_context for hint in _PURE_FOLLOWUP_DIAGNOSE_PHRASES)
        )
        and any(marker in previous_context for marker in _PORTFOLIO_CONTEXT_MARKERS)
    ):
        return TurnExpectation(
            required_tool="portfolio",
            reason="用户承接上一轮持仓体检/分析邀请，必须继续调用持仓诊断工具。",
            required_args={"mode": "diagnose"},
        )

    return None


def missing_required_tool(
    expectation: TurnExpectation | None,
    used_tools: Iterable[str | tuple[str, dict]],
) -> bool:
    if expectation is None:
        return False
    req_args = expectation.required_args
    for entry in used_tools:
        if isinstance(entry, tuple):
            name, args = entry
        else:
            name, args = entry, {}
        if name != expectation.required_tool:
            continue
        if not req_args:
            return False
        if all(args.get(k) == v for k, v in req_args.items()):
            return False
    return True


def build_retry_user_message(expectation: TurnExpectation, assistant_text: str = "") -> str:
    """Synthetic follow-up injected when the model skipped a mandatory tool."""

    tool_name = _TOOL_CN_NAMES.get(expectation.required_tool, expectation.required_tool)
    body = str(assistant_text or "").strip()
    if body:
        if _looks_like_plan_only(body):
            lead = "你刚才只给了计划，还没有真正执行。"
        else:
            lead = "你刚才直接给了文本回答，但没有先拿真实数据。"
    else:
        lead = "这一轮没有返回有效工具调用。"
    if expectation.required_args:
        pairs = ", ".join(f'{k}="{v}"' for k, v in expectation.required_args.items())
        call_hint = f"`{expectation.required_tool}({pairs})`"
    else:
        call_hint = f"`{expectation.required_tool}`"
    return (
        f"{lead}{expectation.reason}"
        f" 现在必须先调用 {call_hint}（{tool_name}）拿到真实数据，"
        "再继续回答。不要重复计划，直接执行第一步。"
    )


def build_retry_exhausted_warning(expectation: TurnExpectation, retries: int) -> str:
    tool_name = _TOOL_CN_NAMES.get(expectation.required_tool, expectation.required_tool)
    return f"⚠ 模型连续 {retries} 次没有调用必需工具 `{expectation.required_tool}`（{tool_name}），以下回答可能不可靠。"


def _looks_like_plan_only(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if "计划" in normalized:
        return True
    markers = (
        "第一步",
        "第二步",
        "第三步",
        "先",
        "再",
        "然后",
        "接着",
        "现在开第一刀",
    )
    numbered = any(token in normalized for token in ("1.", "1、", "2.", "2、", "3.", "3、"))
    return numbered and any(marker in normalized for marker in markers)


# ---------------------------------------------------------------------------
# Doom-loop detection
# ---------------------------------------------------------------------------


def _jaccard_similarity(s1: str, s2: str) -> float:
    """计算两个字符串的 Jaccard 相似度（字符 3-gram）。"""
    if not s1 or not s2:
        return 0.0
    grams1 = {s1[i : i + 3] for i in range(max(len(s1) - 2, 1))}
    grams2 = {s2[i : i + 3] for i in range(max(len(s2) - 2, 1))}
    if not grams1 or not grams2:
        return 0.0
    return len(grams1 & grams2) / len(grams1 | grams2)


def check_doom_loop(
    recent_calls: list[tuple[str, int]],
    name: str,
    args: dict[str, Any],
    *,
    recent_args_texts: list[str] | None = None,
    similarity_threshold: float = 0.8,
) -> bool:
    """Track a tool call and return True if a doom-loop is detected.

    Mutates *recent_calls* in place: appends the new entry and trims to
    ``DOOM_LOOP_WINDOW``.  Returns ``True`` when the same (name, args_hash)
    appears >= ``DOOM_LOOP_THRESHOLD`` times in the window,
    OR when similar args (Jaccard >= threshold) appear >= threshold times.
    """
    import json as _json

    args_hash = hash(_json.dumps(args, sort_keys=True, ensure_ascii=False))
    recent_calls.append((name, args_hash))
    if len(recent_calls) > DOOM_LOOP_WINDOW:
        recent_calls.pop(0)

    # 精确匹配
    if recent_calls.count((name, args_hash)) >= DOOM_LOOP_THRESHOLD:
        return True

    # 语义相似匹配：检查同工具的参数是否"换汤不换药"
    if recent_args_texts is not None:
        args_text = _json.dumps(args, sort_keys=True, ensure_ascii=False)
        same_tool_texts = [t for (n, _), t in zip(recent_calls, recent_args_texts) if n == name]
        similar_count = sum(1 for t in same_tool_texts if _jaccard_similarity(args_text, t) >= similarity_threshold)
        if similar_count >= DOOM_LOOP_THRESHOLD:
            return True
        recent_args_texts.append(args_text)
        if len(recent_args_texts) > DOOM_LOOP_WINDOW:
            recent_args_texts.pop(0)

    return False
