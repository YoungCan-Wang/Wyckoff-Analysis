"""
RAG 防雷：基于东方财富个股新闻做负面关键词 veto（通过 akshare）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_NEGATIVE_KEYWORDS = [
    "立案",
    "调查",
    "证监会",
    "处罚",
    "违规",
    "造假",
    "财务造假",
    "退市",
    "st",
    "*st",
    "减持",
    "质押爆仓",
    "债务违约",
    "业绩预亏",
    "业绩下滑",
    "商誉减值",
    "诉讼",
    "仲裁",
    "冻结",
    "无法表示意见",
    "审计保留意见",
]

RAG_MAX_WORKERS = int(os.getenv("RAG_MAX_WORKERS", "6"))
RAG_NEWS_LOOKBACK_DAYS = int(os.getenv("RAG_NEWS_LOOKBACK_DAYS", "7"))
RAG_SEMANTIC_VETO_ENABLED = os.getenv("RAG_SEMANTIC_VETO_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RAG_SEMANTIC_TIMEOUT = int(os.getenv("RAG_SEMANTIC_TIMEOUT", "25"))
from integrations.llm_client import DEFAULT_GEMINI_MODEL as _DEFAULT_GEMINI_MODEL

RAG_SEMANTIC_MODEL = (
    os.getenv("RAG_SEMANTIC_MODEL", "").strip() or os.getenv("GEMINI_MODEL", "").strip() or _DEFAULT_GEMINI_MODEL
)
_STAR_ST_PATTERN = re.compile(r"(?<![a-z0-9])(?:\*|＊)st\s*[\u4e00-\u9fff]", re.IGNORECASE)
_ST_PATTERN = re.compile(r"(?<![a-z0-9\*＊])st\s*[\u4e00-\u9fff]", re.IGNORECASE)


@dataclass
class VetoResult:
    code: str
    name: str
    veto: bool
    hits: list[str]
    evidence: list[str]
    search_source: str = ""
    raw_result_count: int = 0
    relevant_result_count: int = 0
    elapsed_ms: int = 0
    semantic_checked: bool = False
    semantic_negative: bool | None = None
    semantic_reason: str | None = None
    error: str | None = None


def is_rag_veto_enabled() -> bool:
    flag = os.getenv("RAG_VETO_ENABLED", "1").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def get_rag_veto_runtime_status() -> dict[str, Any]:
    enabled = is_rag_veto_enabled()
    return {
        "enabled": enabled,
        "has_provider": True,  # akshare 无需 API key
        "lookback_days": int(max(RAG_NEWS_LOOKBACK_DAYS, 1)),
        "max_workers": int(max(RAG_MAX_WORKERS, 1)),
        "source": "akshare/eastmoney",
    }


def _normalize_keywords() -> list[str]:
    raw = os.getenv("RAG_NEGATIVE_KEYWORDS", "").strip()
    if not raw:
        return DEFAULT_NEGATIVE_KEYWORDS
    parts = [x.strip().lower() for x in raw.replace("，", ",").split(",") if x.strip()]
    return parts or DEFAULT_NEGATIVE_KEYWORDS


def _normalize_match_text(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _extract_hits(text: str, keywords: list[str]) -> list[str]:
    hits: list[str] = []
    for kw in keywords:
        k = str(kw or "").strip().lower()
        if not k or k in {"st", "*st"}:
            continue
        if k in text and k not in hits:
            hits.append(k)

    if _STAR_ST_PATTERN.search(text):
        hits.append("*st")
    if _ST_PATTERN.search(text):
        hits.append("st")
    return hits


def _fetch_news_akshare(code: str) -> list[dict[str, str]]:
    """通过 akshare 拉取东方财富个股新闻，返回近 N 天内的条目。"""
    import akshare as ak

    cutoff = datetime.now(UTC) - timedelta(days=max(RAG_NEWS_LOOKBACK_DAYS, 1))
    df = ak.stock_news_em(symbol=code)
    if df is None or df.empty:
        return []
    results: list[dict[str, str]] = []
    for _, row in df.iterrows():
        pub_time = row.get("发布时间")
        if pub_time:
            try:
                dt = datetime.fromisoformat(str(pub_time))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                if dt < cutoff:
                    continue
            except Exception:
                pass
        results.append(
            {
                "title": str(row.get("新闻标题", "")).strip(),
                "content": str(row.get("新闻内容", "")).strip(),
            }
        )
    return results


def _parse_semantic_judgement(raw: str) -> tuple[bool | None, str]:
    text = str(raw or "").strip()
    if not text:
        return (None, "")
    try:
        obj = json.loads(text)
        v = obj.get("is_extreme_negative")
        reason = str(obj.get("reason", "")).strip()
        if isinstance(v, bool):
            return (v, reason)
    except Exception:
        pass

    m = re.search(r'"is_extreme_negative"\s*:\s*(true|false)', text, flags=re.IGNORECASE)
    if m:
        v = m.group(1).lower() == "true"
        rm = re.search(r'"reason"\s*:\s*"([^"]*)"', text, flags=re.IGNORECASE)
        reason = rm.group(1).strip() if rm else ""
        return (v, reason)

    upper = text.upper()
    if "TRUE" in upper and "FALSE" not in upper:
        return (True, "")
    if "FALSE" in upper and "TRUE" not in upper:
        return (False, "")
    return (None, "")


def _semantic_negative_via_gemini(
    code: str,
    name: str,
    hits: list[str],
    snippets: list[str],
) -> tuple[bool | None, str | None]:
    """关键词命中后的二次语义判定。"""
    if not RAG_SEMANTIC_VETO_ENABLED:
        return (None, None)
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return (None, "semantic_disabled:missing_gemini_api_key")

    from integrations.llm_client import call_llm

    normalized_hits = [str(h or "").strip().lower() for h in hits if str(h or "").strip()]
    cleaned_snippets = [s for s in snippets if str(s or "").strip()]
    relevant_snippets: list[str] = []
    if normalized_hits:
        for s in cleaned_snippets:
            ss = str(s).lower()
            if any(h in ss for h in normalized_hits):
                relevant_snippets.append(s)
    if not relevant_snippets:
        relevant_snippets = cleaned_snippets[:2]

    content = "\n\n".join(relevant_snippets[:3]).strip()
    if not content:
        return (None, "semantic_disabled:empty_snippets")
    if len(content) > 3000:
        content = content[:3000]

    system_prompt = (
        "你是A股舆情风控判定器。任务是判断新闻是否构成【极端负面实锤风险】。\n"
        "极端负面=监管立案属实、财务造假属实、退市风险、重大诉讼败诉、债务违约等会显著打击股价的事件。\n"
        "若新闻为辟谣、澄清、误传、传闻未证实、或中性事件，则判定为 false。\n"
        "只输出 JSON，不要输出额外文本。"
    )
    user_message = (
        f"股票: {code} {name}\n"
        f"关键词命中: {', '.join(hits[:8])}\n"
        "新闻片段:\n"
        f"{content}\n\n"
        '输出格式: {{"is_extreme_negative": true|false, "reason": "<20字内原因>"}}'
    )
    try:
        raw = call_llm(
            provider="gemini",
            model=RAG_SEMANTIC_MODEL,
            api_key=api_key,
            system_prompt=system_prompt,
            user_message=user_message,
            timeout=max(RAG_SEMANTIC_TIMEOUT, 8),
            max_output_tokens=256,
        )
        verdict, reason = _parse_semantic_judgement(raw)
        if verdict is None:
            return (None, f"semantic_parse_failed:{str(raw)[:120]}")
        return (verdict, reason or None)
    except Exception as e:
        return (None, f"semantic_llm_err:{e}")


def _scan_one(code: str, name: str, keywords: list[str]) -> VetoResult:
    started = time.perf_counter()
    search_source = "akshare"

    try:
        results = _fetch_news_akshare(code)
    except Exception as e:
        logger.debug("[rag_veto] akshare fetch failed for %s: %s", code, e)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return VetoResult(
            code=code,
            name=name,
            veto=False,
            hits=[],
            evidence=[],
            search_source=search_source,
            raw_result_count=0,
            relevant_result_count=0,
            elapsed_ms=elapsed_ms,
            error=f"akshare_err:{e}",
        )

    text_parts: list[str] = []
    evidence: list[str] = []
    semantic_snippets: list[str] = []
    for item in results:
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        merged = f"{title}\n{content}".strip()
        if merged:
            text_parts.append(merged.lower())
            semantic_snippets.append(merged)
        if title:
            evidence.append(title)
    combined = "\n".join(text_parts)
    relevant_count = len(results)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    hits = _extract_hits(combined, keywords)
    if not hits:
        return VetoResult(
            code=code,
            name=name,
            veto=False,
            hits=[],
            evidence=evidence[:3],
            search_source=search_source,
            raw_result_count=len(results),
            relevant_result_count=relevant_count,
            elapsed_ms=elapsed_ms,
        )

    # 关键词命中 → 语义二判
    semantic_checked = False
    semantic_negative: bool | None = None
    semantic_reason: str | None = None
    semantic_err: str | None = None
    verdict, reason_or_err = _semantic_negative_via_gemini(
        code=code,
        name=name,
        hits=hits,
        snippets=semantic_snippets,
    )
    if verdict is not None:
        semantic_checked = True
        semantic_negative = bool(verdict)
        semantic_reason = reason_or_err
        veto = bool(verdict)
    else:
        veto = True
        semantic_err = reason_or_err

    return VetoResult(
        code=code,
        name=name,
        veto=veto,
        hits=hits,
        evidence=evidence[:3],
        search_source=search_source,
        raw_result_count=len(results),
        relevant_result_count=relevant_count,
        elapsed_ms=elapsed_ms,
        semantic_checked=semantic_checked,
        semantic_negative=semantic_negative,
        semantic_reason=semantic_reason,
        error=semantic_err,
    )


def run_negative_news_veto(candidates: list[dict[str, str]]) -> dict[str, VetoResult]:
    """
    candidates: [{"code":"000001","name":"平安银行"}, ...]
    """
    out: dict[str, VetoResult] = {}
    if not is_rag_veto_enabled():
        return out

    keywords = _normalize_keywords()
    items = [
        {"code": str(x.get("code", "")).strip(), "name": str(x.get("name", "")).strip()}
        for x in candidates
        if str(x.get("code", "")).strip()
    ]
    if not items:
        return out

    with ThreadPoolExecutor(max_workers=max(RAG_MAX_WORKERS, 1)) as ex:
        futures = {ex.submit(_scan_one, it["code"], it["name"] or it["code"], keywords): it["code"] for it in items}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = VetoResult(code=code, name=code, veto=False, hits=[], evidence=[], error=str(e))
            out[code] = result
    return out
