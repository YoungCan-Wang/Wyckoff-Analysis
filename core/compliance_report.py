"""Compliance-safe market brief generation for Step3.

The raw Wyckoff report remains the internal artifact.  The public/compliance
brief is generated from a deliberately de-identified payload: market regime,
style distribution, sector aggregates, and risk notes only.  The cheap model
is a wording assistant, not a decision maker; a deterministic validator and
template fallback own the safety boundary.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from integrations.llm_client import OPENAI_COMPATIBLE_BASE_URLS, call_llm

OPENROUTER_PROVIDER = "openrouter"
EFFICIENCY_PROVIDER = "efficiency"
OPENROUTER_BASE_URL = OPENAI_COMPATIBLE_BASE_URLS["openrouter"]
DEFAULT_MAX_OUTPUT_TOKENS = 2048

_STOCK_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_PROHIBITED_TERMS = (
    "买入",
    "卖出",
    "建仓",
    "加仓",
    "清仓",
    "减仓",
    "止损",
    "目标价",
    "参考价",
    "强烈推荐",
    "重点推荐",
    "明日买",
    "可操作",
    "PROBE",
    "ATTACK",
    "EXIT",
    "TRIM",
)


@dataclass(frozen=True)
class ComplianceLLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str
    source: str


@dataclass(frozen=True)
class ComplianceValidation:
    ok: bool
    reasons: tuple[str, ...] = ()


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except Exception:
        return default


def fmt_pct(value: Any) -> str:
    num = pd.to_numeric(value, errors="coerce")
    if pd.isna(num):
        return "待更新"
    x = float(num)
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def resolve_compliance_llm_config() -> ComplianceLLMConfig | None:
    """Resolve the cheap/efficiency model channel from environment variables.

    Priority:
    1. OPENROUTER_API_KEY + OPENROUTER_MODEL, with default OpenRouter base URL.
    2. EFFICIENCY_API_KEY + EFFICIENCY_MODEL + EFFICIENCY_BASE_URL for any
       OpenAI-compatible cheap endpoint.
    """

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    openrouter_model = os.getenv("OPENROUTER_MODEL", "").strip()
    if openrouter_key and openrouter_model:
        return ComplianceLLMConfig(
            provider=OPENROUTER_PROVIDER,
            api_key=openrouter_key,
            model=openrouter_model,
            base_url=OPENROUTER_BASE_URL,
            source="openrouter",
        )

    efficiency_key = os.getenv("EFFICIENCY_API_KEY", "").strip()
    efficiency_model = os.getenv("EFFICIENCY_MODEL", "").strip()
    efficiency_base_url = os.getenv("EFFICIENCY_BASE_URL", "").strip()
    if efficiency_key and efficiency_model and efficiency_base_url:
        return ComplianceLLMConfig(
            provider=EFFICIENCY_PROVIDER,
            api_key=efficiency_key,
            model=efficiency_model,
            base_url=efficiency_base_url,
            source="efficiency",
        )

    return None


def _score_bucket(score: float) -> str:
    if score >= 0.75:
        return "高"
    if score >= 0.45:
        return "中"
    return "低"


def build_public_payload(
    *,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str] | None = None,
    rag_veto_count: int = 0,
) -> dict[str, Any]:
    """Build a de-identified payload for the compliance brief."""

    ctx = benchmark_context or {}
    breadth = ctx.get("breadth", {}) or {}
    df = selected_df.copy() if isinstance(selected_df, pd.DataFrame) else pd.DataFrame()
    ops_set = {str(code).strip() for code in (ops_codes or []) if str(code).strip()}

    payload: dict[str, Any] = {
        "trade_date": str(ctx.get("trade_date") or ctx.get("end_trade_date") or ""),
        "market": {
            "regime": str(ctx.get("regime", "NEUTRAL") or "NEUTRAL").strip().upper(),
            "main_today_pct": fmt_pct(ctx.get("main_today_pct")),
            "recent3_cum_pct": fmt_pct(ctx.get("recent3_cum_pct")),
            "breadth_ratio": fmt_pct(breadth.get("ratio_pct")),
            "volume_state": str(ctx.get("main_volume_state", "") or "").strip() or "待更新",
        },
        "sample_stats": {
            "candidate_count": int(len(df)),
            "springboard_count": int(len(ops_set)),
            "rag_veto_count": int(max(rag_veto_count, 0)),
        },
        "style_stats": {},
        "sector_stats": [],
        "risk_flags": [],
    }

    if not df.empty:
        track_series = df.get("track", pd.Series(dtype=str)).astype(str).str.strip()
        payload["style_stats"] = {
            "trend_count": int((track_series == "Trend").sum()),
            "accum_count": int((track_series == "Accum").sum()),
            "unknown_count": int((~track_series.isin(["Trend", "Accum"])).sum()),
        }

        tag_series = df.get("tag", pd.Series(dtype=str)).astype(str).str.lower()
        payload["trigger_stats"] = {
            "sos_count": int(tag_series.str.contains("sos|点火|突破", regex=True).sum()),
            "spring_count": int(tag_series.str.contains("spring", regex=True).sum()),
            "lps_count": int(tag_series.str.contains("lps", regex=True).sum()),
            "evr_count": int(tag_series.str.contains("evr", regex=True).sum()),
        }

        sec_df = df.copy()
        if "industry" in sec_df.columns:
            sec_df["industry"] = sec_df["industry"].astype(str).str.strip()
        else:
            sec_df["industry"] = ""
        sec_df = sec_df[sec_df["industry"] != ""]
        if not sec_df.empty:
            priority_raw = (
                sec_df["priority_score"]
                if "priority_score" in sec_df.columns
                else pd.Series([pd.NA] * len(sec_df), index=sec_df.index)
            )
            funnel_raw = (
                sec_df["funnel_score"]
                if "funnel_score" in sec_df.columns
                else pd.Series([pd.NA] * len(sec_df), index=sec_df.index)
            )
            score_series = pd.to_numeric(priority_raw, errors="coerce")
            score_series = score_series.where(score_series.notna(), pd.to_numeric(funnel_raw, errors="coerce"))
            sec_df["score"] = score_series.fillna(0.0)
            grouped = (
                sec_df.groupby("industry", as_index=False)
                .agg(sample_count=("industry", "count"), avg_score=("score", "mean"))
                .sort_values(["sample_count", "avg_score"], ascending=[False, False])
                .head(5)
            )
            payload["sector_stats"] = [
                {
                    "industry": str(row["industry"]),
                    "sample_count": int(row["sample_count"]),
                    "score_bucket": _score_bucket(float(row["avg_score"])),
                }
                for _, row in grouped.iterrows()
            ]

    regime = payload["market"]["regime"]
    if regime in {"RISK_OFF", "CRASH", "BLACK_SWAN"}:
        payload["risk_flags"].append("市场风险偏高，弱市假突破与流动性折价需要重点防范")
    if payload["sample_stats"]["rag_veto_count"] > 0:
        payload["risk_flags"].append("本轮存在负面信息防雷剔除样本")
    if payload["sample_stats"]["candidate_count"] <= 0:
        payload["risk_flags"].append("本轮可观察样本不足")
    if not payload["risk_flags"]:
        payload["risk_flags"].append("维持常规风险揭示，避免根据单日信号过度外推")
    return payload


def _render_payload_text(payload: dict[str, Any]) -> str:
    sector_lines = payload.get("sector_stats") or []
    style = payload.get("style_stats") or {}
    trigger = payload.get("trigger_stats") or {}
    risk_flags = payload.get("risk_flags") or []
    market = payload.get("market") or {}
    sample = payload.get("sample_stats") or {}

    lines = [
        f"market_regime={market.get('regime', 'NEUTRAL')}",
        f"main_today_pct={market.get('main_today_pct')}",
        f"recent3_cum_pct={market.get('recent3_cum_pct')}",
        f"breadth_ratio={market.get('breadth_ratio')}",
        f"volume_state={market.get('volume_state')}",
        f"candidate_count={sample.get('candidate_count', 0)}",
        f"springboard_count={sample.get('springboard_count', 0)}",
        f"rag_veto_count={sample.get('rag_veto_count', 0)}",
        (
            "style_stats="
            f"Trend:{style.get('trend_count', 0)}, "
            f"Accum:{style.get('accum_count', 0)}, "
            f"Unknown:{style.get('unknown_count', 0)}"
        ),
        (
            "trigger_stats="
            f"SOS:{trigger.get('sos_count', 0)}, "
            f"Spring:{trigger.get('spring_count', 0)}, "
            f"LPS:{trigger.get('lps_count', 0)}, "
            f"EVR:{trigger.get('evr_count', 0)}"
        ),
        "sector_stats:",
    ]
    if sector_lines:
        for item in sector_lines:
            lines.append(
                f"- {item.get('industry')} | sample_count={item.get('sample_count')} | score_bucket={item.get('score_bucket')}"
            )
    else:
        lines.append("- 无明显行业聚集")

    lines.append("risk_flags:")
    for item in risk_flags:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _system_prompt() -> str:
    return """你是证券市场观察简报编辑，只能基于输入的脱敏统计数据写市场研究摘要。

硬规则：
- 不得输出任何股票代码、股票名称、个股名单或个股排序。
- 不得给出买入、卖出、建仓、加仓、清仓、减仓、止损、目标价、参考价等交易指令。
- 不得承诺收益，不得暗示确定性上涨。
- 只允许讨论市场水温、资金风格、行业聚集、量价结构分布和风险提示。
- 输出中文 Markdown，结构固定为：市场水温、资金风格、板块热度、策略观察、风险提示。
"""


def validate_compliance_report(text: str, *, forbidden_names: list[str] | None = None) -> ComplianceValidation:
    reasons: list[str] = []
    body = text or ""
    if _STOCK_CODE_RE.search(body):
        reasons.append("contains_stock_code")
    for term in _PROHIBITED_TERMS:
        if term in body:
            reasons.append(f"contains_term:{term}")
            break
    for name in forbidden_names or []:
        clean = str(name or "").strip()
        if len(clean) >= 2 and clean in body:
            reasons.append("contains_stock_name")
            break
    return ComplianceValidation(ok=not reasons, reasons=tuple(reasons))


def render_compliance_fallback(payload: dict[str, Any]) -> str:
    market = payload.get("market") or {}
    sample = payload.get("sample_stats") or {}
    style = payload.get("style_stats") or {}
    trigger = payload.get("trigger_stats") or {}
    sectors = payload.get("sector_stats") or []
    risk_flags = payload.get("risk_flags") or []

    lines = [
        "## 今日市场观察简报（合规版）",
        "",
        "### 一、市场水温",
        (
            f"- 市场状态：{market.get('regime', 'NEUTRAL')}；当日涨跌：{market.get('main_today_pct')}；"
            f"近3日累计：{market.get('recent3_cum_pct')}；市场广度：{market.get('breadth_ratio')}；"
            f"量能状态：{market.get('volume_state')}"
        ),
        "",
        "### 二、资金风格",
        (
            f"- 本轮观察样本 {sample.get('candidate_count', 0)} 个；"
            f"趋势结构 {style.get('trend_count', 0)} 个，吸筹结构 {style.get('accum_count', 0)} 个。"
        ),
        (
            f"- 触发形态分布：SOS {trigger.get('sos_count', 0)}，Spring {trigger.get('spring_count', 0)}，"
            f"LPS {trigger.get('lps_count', 0)}，EVR {trigger.get('evr_count', 0)}。"
        ),
        "",
        "### 三、板块热度",
    ]
    if sectors:
        for item in sectors[:5]:
            lines.append(
                f"- {item.get('industry')}：样本聚集度 {item.get('sample_count')}，综合强度 {item.get('score_bucket')}"
            )
    else:
        lines.append("- 暂无明显行业聚集，样本结构偏分散。")

    lines.extend(
        [
            "",
            "### 四、策略观察",
            "- 当前更适合从市场环境、板块扩散和量价确认质量三个角度观察，不宜根据单日信号外推确定性结论。",
            "- 若市场广度走弱或量能无法配合，应优先关注风险暴露和信号失效率。",
            "",
            "### 五、风险提示",
        ]
    )
    for item in risk_flags:
        lines.append(f"- {item}")
    lines.extend(
        [
            "- 本简报仅用于市场研究与信息交流，不构成投资建议。",
            "- 模型生成内容可能存在遗漏或偏差，请结合公开信息独立判断。",
            "- 股市有风险，投资需谨慎。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def generate_compliance_brief(
    *,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str] | None = None,
    code_name: dict[str, str] | None = None,
    rag_veto_count: int = 0,
) -> str:
    payload = build_public_payload(
        benchmark_context=benchmark_context,
        selected_df=selected_df,
        ops_codes=ops_codes,
        rag_veto_count=rag_veto_count,
    )
    fallback = render_compliance_fallback(payload)
    if not _bool_env("STEP3_COMPLIANCE_LLM_ENABLED", True):
        return fallback

    llm_cfg = resolve_compliance_llm_config()
    if llm_cfg is None:
        return fallback

    forbidden_names = list((code_name or {}).values())
    retries = max(_int_env("STEP3_COMPLIANCE_MAX_RETRIES", 1), 0)
    max_output_tokens = max(_int_env("STEP3_COMPLIANCE_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS), 512)
    user_message = (
        "请根据以下脱敏统计数据生成合规版市场观察简报。"
        "不要使用任何个股代码、名称或交易动作词。\n\n" + _render_payload_text(payload)
    )
    last_reasons: tuple[str, ...] = ()
    for attempt in range(retries + 1):
        prompt = _system_prompt()
        if attempt > 0 and last_reasons:
            prompt += "\n上一版未通过合规校验，原因：" + "，".join(last_reasons) + "。请重写并严格避开。"
        try:
            text = call_llm(
                provider=llm_cfg.provider,
                model=llm_cfg.model,
                api_key=llm_cfg.api_key,
                system_prompt=prompt,
                user_message=user_message,
                base_url=llm_cfg.base_url,
                timeout=90,
                max_output_tokens=max_output_tokens,
            ).strip()
        except Exception as exc:
            print(f"[step3][compliance] {llm_cfg.source} 生成失败: {exc}")
            return fallback
        validation = validate_compliance_report(text, forbidden_names=forbidden_names)
        if validation.ok:
            print(f"[step3][compliance] 使用 {llm_cfg.source} 模型生成合规简报: {llm_cfg.model}")
            return text.rstrip() + "\n"
        last_reasons = validation.reasons
        print(
            f"[step3][compliance] 合规校验失败: attempt={attempt + 1}/{retries + 1}, reasons={','.join(last_reasons)}"
        )

    print("[step3][compliance] 已降级为确定性模板")
    return fallback
