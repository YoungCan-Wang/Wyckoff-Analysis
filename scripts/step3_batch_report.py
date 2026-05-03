"""
批量 AI 研报（Step3）
拉取选中股票的 OHLCV → 特征工程 → AI 三阵营分析 → 飞书/企微/钉钉推送
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from typing import Any

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from functools import partial

from core.prompts import WYCKOFF_FUNNEL_SYSTEM_PROMPT
from core.sector_rotation import SECTOR_STATE_LABELS
from core.wyckoff_engine import fit_ai_candidate_quotas, normalize_hist_from_fetch
from integrations.data_source import (
    fetch_index_hist,
    fetch_market_cap_map,
    fetch_sector_map,
)
from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window
from integrations.llm_client import call_llm
from integrations.rag_veto import (
    get_rag_veto_runtime_status,
    is_rag_veto_enabled,
    run_negative_news_veto,
)
from tools.data_fetcher import (
    append_spot_bar_if_needed,
)
from tools.data_fetcher import (
    latest_trade_date_from_hist as _latest_trade_date_from_hist,
)

# ── tools/ 层导入 ──
from tools.report_builder import (
    _extract_ops_codes_from_markdown,
    _try_parse_structured_report,
    generate_stock_payload,
)
from tools.report_builder import (
    build_track_user_message as _build_track_user_message,
)
from utils.feishu import send_feishu_notification
from utils.notify import send_dingtalk_notification, send_wecom_notification
from utils.trading_clock import resolve_end_calendar_day

_append_spot_bar_if_needed = partial(
    append_spot_bar_if_needed,
    env_prefix="STEP3",
    zero_fallback=True,
)

TRADING_DAYS = 320
GEMINI_MODEL_FALLBACK = ""
STEP3_REPORT_STYLE = os.getenv("STEP3_REPORT_STYLE", "v3_three_camp").strip().lower()
_LEGACY_REPORT_STYLES = {
    "legacy",
    "legacy_dual_pool",
    "dual_pool",
    "classic",
    "v1",
}
STEP3_USE_LEGACY_REPORT = STEP3_REPORT_STYLE in _LEGACY_REPORT_STYLES
if STEP3_USE_LEGACY_REPORT:
    raise RuntimeError(
        "STEP3_REPORT_STYLE legacy 口径已禁用。请改为 v3_three_camp（或任意非 legacy 样式）以启用三阵营输出。"
    )
STEP3_MAX_AI_INPUT = int(os.getenv("STEP3_MAX_AI_INPUT", "0"))
STEP3_DEFAULT_CONTEXT_CAP = max(
    int(os.getenv("STEP3_DEFAULT_CONTEXT_CAP", "0")),
    0,
)
STEP3_MAX_PER_INDUSTRY = int(os.getenv("STEP3_MAX_PER_INDUSTRY", "0"))
STEP3_EMPTY_COMPRESSION_FALLBACK_CAP = max(
    int(os.getenv("STEP3_EMPTY_COMPRESSION_FALLBACK_CAP", "8")),
    0,
)
STEP3_MAX_UPSTREAM_FILL = max(int(os.getenv("STEP3_MAX_UPSTREAM_FILL", "0")), 0)
STEP3_MAX_OUTPUT_TOKENS = 32768
DYNAMIC_MAINLINE_BONUS_RATE = 0.15
DYNAMIC_MAINLINE_TOP_N = 3
DYNAMIC_MAINLINE_MIN_CLUSTER = 2
STEP3_ENABLE_COMPRESSION = os.getenv("STEP3_ENABLE_COMPRESSION", "1").strip().lower() in {"1", "true", "yes", "on"}
STEP3_ENABLE_RAG_VETO = os.getenv("STEP3_ENABLE_RAG_VETO", "1").strip().lower() in {"1", "true", "yes", "on"}
STEP3_SKIP_LLM = os.getenv("STEP3_SKIP_LLM", "0").strip().lower() in {"1", "true", "yes", "on"}
STEP3_RESPECT_UPSTREAM_PRIORITY = os.getenv("STEP3_RESPECT_UPSTREAM_PRIORITY", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STEP3_SEND_COMPLIANCE_BRIEF = os.getenv("STEP3_SEND_COMPLIANCE_BRIEF", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STEP3_COMPLIANCE_FOCUS_LIMIT = max(
    int(os.getenv("STEP3_COMPLIANCE_FOCUS_LIMIT", "5")),
    1,
)


RECENT_DAYS = 15
HIGHLIGHT_DAYS = 60
HIGHLIGHT_PCT_THRESHOLD = 5.0
HIGHLIGHT_VOL_RATIO = 2.0
from tools.debug_io import dump_model_input as _dump_model_input_shared

# 已按策略要求关闭"目标交易日强校验"，避免数据源时差导致候选被整批跳过。
ENFORCE_TARGET_TRADE_DATE = False
SUPPLY_HEAVY_VOL_RATIO = 1.5
SUPPLY_DRY_VOL_RATIO = 0.8
SUPPLY_TEST_MAX_ABS_PCT = 1.0
KEY_LEVEL_WINDOW = 20
TRACK_LABELS = {
    "Trend": "Trend轨（右侧主升 / 放量点火）",
    "Accum": "Accum轨（左侧潜伏 / Spring / LPS）",
}
SPRINGBOARD_ABC_LEGEND = (
    "## 🧾 起跳板硬门槛释义（A/B/C）\n"
    "- A：近5日出现缩量测试/拒绝下跌（量比 < 0.8x 且收位 > 60%）\n"
    "- B：突破日量比 >= 1.5x 且收盘站稳突破位（收位 > 70%）\n"
    "- C：支撑位至少经过 2 次测试且未被有效击穿\n"
    "- 进入“处于起跳板”通常需至少满足 2 条；若弱市（RISK_OFF/CRASH）执行更严格。\n\n"
    "---\n"
)


def _dump_model_input(
    items: list[dict],
    model: str,
    system_prompt: str,
    user_message: str,
    *,
    name_hint: str = "",
) -> str:
    """step3 专用包装：转发到 tools.debug_io.dump_model_input。"""
    return _dump_model_input_shared(
        step_prefix="step3",
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        items=items,
        name_hint=name_hint,
    )


def _send_input_preview(
    webhook_url: str,
    model: str,
    system_prompt: str,
    previews: list[dict],
    *,
    wecom_webhook: str = "",
    dingtalk_webhook: str = "",
) -> tuple[bool, str]:
    """
    预演模式：不调用模型，仅展示将发送给模型的输入内容。
    """
    total_selected = sum(int(x.get("selected_count", 0) or 0) for x in previews)
    blocks: list[str] = [
        "# 🧪 Step3 模型输入预演（未调用大模型）",
        "",
        f"- 目标模型: `{model}`",
        f"- 输入股票数: `{total_selected}`",
        "- 模式: `STEP3_SKIP_LLM=1`",
        "",
        "## SYSTEM PROMPT",
        "",
        "```text",
        system_prompt,
        "```",
        "",
    ]
    for idx, item in enumerate(previews, start=1):
        blocks.extend(
            [
                f"## USER MESSAGE {idx} / {len(previews)}",
                "",
                f"- 轨道: `{item.get('track', '')}`",
                f"- 股票数: `{item.get('selected_count', 0)}`",
                "",
                "```text",
                str(item.get("user_message", "") or ""),
                "```",
                "",
            ]
        )
    report = "\n".join(blocks).rstrip() + "\n"
    title = f"🧪 模型输入预演 {date.today().strftime('%Y-%m-%d')}"
    sent = send_feishu_notification(webhook_url, title, report) if webhook_url else True
    if wecom_webhook:
        send_wecom_notification(wecom_webhook, title, report)
    if dingtalk_webhook:
        send_dingtalk_notification(dingtalk_webhook, title, report)
    if not sent:
        print("[step3] 预演报告飞书推送失败")
        return (False, report)
    print(f"[step3] 预演报告发送成功，股票数={total_selected}")
    return (True, report)


def _has_required_sections(report: str) -> bool:
    text = (report or "").replace(" ", "")
    has_invalidated = "逻辑破产" in text
    has_building = "储备营地" in text
    has_springboard = "处于起跳板" in text
    return has_invalidated and has_building and has_springboard


def _repair_report_structure(
    report: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
    *,
    provider: str = "gemini",
    llm_base_url: str = "",
) -> str:
    """
    当模型未给出可识别的分层结构时，做一次结构修复重写。
    """
    if not report.strip():
        return report

    repair_system = (
        "你是格式修复器。请将输入研报重排为标准 Markdown，"
        "必须包含三个章节：1) 逻辑破产 2) 储备营地 3) 处于起跳板。"
        "如果输入原本是旧口径的继续观察/立刻建仓，也要将其重排到上述三阵营中。"
        "明显假突破、派发、放量失守归入逻辑破产；其余未到起跳点的非操作标的归入储备营地。"
        "不可新增未在输入中出现的股票代码。"
    )
    repair_user = "允许使用的股票代码：" + ", ".join(selected_codes) + "\n\n以下是待修复文本：\n\n" + report
    try:
        fixed = call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=repair_system,
            user_message=repair_user,
            base_url=llm_base_url or None,
            timeout=180,
            max_output_tokens=STEP3_MAX_OUTPUT_TOKENS,
        )
        return fixed or report
    except Exception as e:
        print(f"[step3] 结构修复失败: {e}")
        return report


def _build_fallback_sections(selected_df: pd.DataFrame) -> str:
    """
    最后兜底：确保飞书一定出现标准三阵营结果块。
    """
    if selected_df is None or selected_df.empty:
        return (
            "## 💀 逻辑破产（系统兜底）\n"
            "- 无（本轮无明确失效标的可判定）。\n\n"
            "## ⏳ 储备营地（系统兜底）\n"
            "- 无（本轮无可用候选）。\n\n"
            "## 🏹 处于起跳板（系统兜底）\n"
            "- 无（本轮无可操作标的）。"
        )

    lines = ["## 💀 逻辑破产（系统兜底）", "- 无（系统未判定明确逻辑破产标的）。", ""]
    lines.append("## ⏳ 储备营地（系统兜底）")
    for _, row in selected_df.iterrows():
        code = str(row.get("code", ""))
        name = str(row.get("name", code))
        tag = str(row.get("tag", ""))
        score = row.get("wyckoff_score")
        score_text = f"{float(score):.3f}" if pd.notna(score) else "-"
        lines.append(
            f"- `{code} {name}` | 标签: {tag or '-'} | 量化分: {score_text} | 仍需条件: 回踩结构战区时需缩量确认。"
        )

    lines.append("")
    lines.append("## 🏹 处于起跳板（系统兜底）")
    lines.append("- 无（模型未输出可操作标的，保持耐心观察）")
    return "\n".join(lines)


def _safe_return(series: pd.Series, lookback: int = 10) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) <= lookback:
        return None
    start = float(s.iloc[-lookback - 1])
    end = float(s.iloc[-1])
    if start == 0:
        return None
    return (end - start) / start * 100.0


def _resolve_bias_range(regime: str | None) -> tuple[float, float]:
    r = str(regime or "").upper()
    if r == "BLACK_SWAN":
        return (0.0, 15.0)
    if r == "CRASH":
        return (0.0, 20.0)
    if r == "RISK_ON":
        return (-5.0, 45.0)
    if r == "RISK_OFF":
        return (0.0, 25.0)
    return (0.0, 35.0)


def _format_mainline_tag(industry: str | None, is_hot: bool) -> str:
    if not is_hot or not industry:
        return ""
    return f"🔥 [当前资金最强主线: {industry}]"


def ultimate_compressor(
    candidates_df: pd.DataFrame,
    regime: str | None,
    bonus_rate: float = DYNAMIC_MAINLINE_BONUS_RATE,
    max_total: int = STEP3_MAX_AI_INPUT,
    max_per_industry: int = STEP3_MAX_PER_INDUSTRY,
) -> pd.DataFrame:
    """
    Step 4.5 终极压缩：动态乖离过滤 + 因子标准化 + 动态主线识别 + 行业上限。
    """
    if candidates_df is None or candidates_df.empty:
        return pd.DataFrame()

    df = candidates_df.copy()
    if max_total <= 0:
        max_total = len(df)
    if max_per_industry <= 0:
        max_per_industry = len(df)
    df["code"] = df.get("code", "").astype(str).str.strip()
    df["bias_200"] = pd.to_numeric(df.get("bias_200"), errors="coerce")
    df["rs_10"] = pd.to_numeric(df.get("rs_10"), errors="coerce")
    df["min_vol_ratio_5d"] = pd.to_numeric(df.get("min_vol_ratio_5d"), errors="coerce")
    df["industry"] = df.get("industry", "").astype(str).str.strip()
    df.loc[df["industry"] == "", "industry"] = pd.NA

    # 先删脏数据：核心字段缺失直接淘汰
    df = df.dropna(subset=["bias_200", "rs_10", "min_vol_ratio_5d", "industry"])
    if df.empty:
        return pd.DataFrame()

    # 动态水温阈值
    bias_min, bias_max = _resolve_bias_range(regime)
    df = df[(df["bias_200"] >= bias_min) & (df["bias_200"] <= bias_max)]
    if df.empty:
        return pd.DataFrame()

    # 百分位因子分数
    df["rs_score"] = df["rs_10"].rank(pct=True, ascending=True, method="average")
    # 量比越小越好：ascending=False 使小值获得更高分位
    df["dry_score"] = df["min_vol_ratio_5d"].rank(pct=True, ascending=False, method="average")
    df["base_wyckoff_score"] = 0.6 * df["rs_score"] + 0.4 * df["dry_score"]

    # 动态主线识别：候选池内"有集群且相对强度高"的行业
    industry_stats = df.groupby("industry", as_index=False).agg(
        stock_count=("code", "count"), avg_rs=("rs_score", "mean")
    )
    valid_industry_stats = industry_stats[industry_stats["stock_count"] >= DYNAMIC_MAINLINE_MIN_CLUSTER]
    hot_industries: set[str] = set()
    if not valid_industry_stats.empty:
        hot_industries = set(
            valid_industry_stats.nlargest(DYNAMIC_MAINLINE_TOP_N, "avg_rs")["industry"].astype(str).tolist()
        )
    df["is_hot_mainline"] = df["industry"].astype(str).isin(hot_industries)
    df["policy_tag"] = df.apply(
        lambda r: _format_mainline_tag(str(r.get("industry", "")), bool(r.get("is_hot_mainline"))),
        axis=1,
    )
    df["dynamic_bonus"] = df["is_hot_mainline"].map(lambda v: float(bonus_rate) if bool(v) else 0.0)
    df["wyckoff_score"] = df["base_wyckoff_score"] * (1.0 + df["dynamic_bonus"])

    # 先全局排序，再做行业拥挤度限制
    df = df.sort_values("wyckoff_score", ascending=False).copy()
    df["industry_rank"] = df.groupby("industry")["wyckoff_score"].rank(ascending=False, method="first").astype(int)
    df = df.groupby("industry", group_keys=False).head(max_per_industry)
    df = df.head(max_total).reset_index(drop=True)
    if hot_industries:
        print(f"[step3] 动态主线行业: {', '.join(sorted(hot_industries))}")
    else:
        print("[step3] 动态主线行业: 无（未形成有效行业集群）")
    return df


def _fallback_candidates_when_compression_empty(
    candidates_df: pd.DataFrame,
) -> pd.DataFrame:
    if candidates_df is None or candidates_df.empty:
        return pd.DataFrame()

    df = candidates_df.copy()
    df["wyckoff_score"] = pd.to_numeric(df.get("funnel_score"), errors="coerce")
    df = df.sort_values(
        by=["wyckoff_score", "rs_10", "min_vol_ratio_5d"],
        ascending=[False, False, True],
        na_position="last",
    ).reset_index(drop=True)
    if STEP3_EMPTY_COMPRESSION_FALLBACK_CAP > 0:
        df = df.head(STEP3_EMPTY_COMPRESSION_FALLBACK_CAP).reset_index(drop=True)
    return df


def _coerce_bool_like(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_step3_context_cap(raw_count: int) -> int:
    raw_n = max(int(raw_count), 0)
    if raw_n <= 0:
        return 0
    if STEP3_MAX_AI_INPUT > 0:
        return min(STEP3_MAX_AI_INPUT, raw_n)
    if STEP3_DEFAULT_CONTEXT_CAP > 0:
        return min(STEP3_DEFAULT_CONTEXT_CAP, raw_n)
    return raw_n


def _has_upstream_priority_context(candidates_df: pd.DataFrame) -> bool:
    if not STEP3_RESPECT_UPSTREAM_PRIORITY or candidates_df is None or candidates_df.empty:
        return False
    if "priority_score" in candidates_df.columns:
        if pd.to_numeric(candidates_df["priority_score"], errors="coerce").notna().any():
            return True
    if "selection_source" in candidates_df.columns:
        if candidates_df["selection_source"].astype(str).str.strip().ne("").any():
            return True
    return False


def _select_upstream_priority_candidates(
    candidates_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Respect the Funnel's upstream ordering and only do a conservative tail cut here.
    Step3 should not re-rank already-selected AI candidates into a different order.
    """
    if candidates_df is None or candidates_df.empty:
        return pd.DataFrame()

    df = candidates_df.copy()
    df["input_order"] = pd.to_numeric(df.get("input_order"), errors="coerce")
    df["input_order"] = df["input_order"].fillna(pd.Series(range(len(df)), index=df.index)).astype(int)
    if "selection_is_fill" in df.columns:
        df["selection_is_fill"] = df["selection_is_fill"].apply(_coerce_bool_like)
    else:
        df["selection_is_fill"] = False
    df["priority_score"] = pd.to_numeric(df.get("priority_score"), errors="coerce")
    df = df.sort_values(
        by=["selection_is_fill", "input_order"],
        ascending=[True, True],
        kind="stable",
    ).reset_index(drop=True)

    context_cap = _resolve_step3_context_cap(len(df))
    if context_cap <= 0 or len(df) <= context_cap:
        return df

    trend_total = int((df["track"] == "Trend").sum())
    accum_total = int((df["track"] == "Accum").sum())
    trend_cap, accum_cap = fit_ai_candidate_quotas(context_cap, trend_total, accum_total)

    core_df = df[~df["selection_is_fill"]].copy()
    fill_df = df[df["selection_is_fill"]].copy()

    selected_parts: list[pd.DataFrame] = []
    if trend_cap > 0:
        selected_parts.append(core_df[core_df["track"] == "Trend"].head(trend_cap))
    if accum_cap > 0:
        selected_parts.append(core_df[core_df["track"] == "Accum"].head(accum_cap))

    selected_df = pd.concat(selected_parts, ignore_index=False) if selected_parts else df.iloc[0:0].copy()
    selected_codes = set(selected_df["code"].astype(str).tolist())

    remaining_slots = max(context_cap - len(selected_df), 0)
    if remaining_slots > 0:
        core_remainder = core_df[~core_df["code"].astype(str).isin(selected_codes)]
        if not core_remainder.empty:
            extra_core = core_remainder.head(remaining_slots)
            selected_df = pd.concat([selected_df, extra_core], ignore_index=False)
            selected_codes.update(extra_core["code"].astype(str).tolist())
            remaining_slots = max(context_cap - len(selected_df), 0)

    if remaining_slots > 0 and STEP3_MAX_UPSTREAM_FILL > 0:
        fill_remainder = fill_df[~fill_df["code"].astype(str).isin(selected_codes)]
        if not fill_remainder.empty:
            fill_take = fill_remainder.head(min(remaining_slots, STEP3_MAX_UPSTREAM_FILL))
            selected_df = pd.concat([selected_df, fill_take], ignore_index=False)

    selected_df = selected_df.sort_values("input_order", kind="stable").reset_index(drop=True)
    return selected_df


def _strip_report_title(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _fmt_pct(v: Any) -> str:
    num = pd.to_numeric(v, errors="coerce")
    if pd.isna(num):
        return "待更新"
    x = float(num)
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def _build_compliance_brief(
    *,
    benchmark_context: dict,
    selected_df: pd.DataFrame,
    ops_codes: list[str],
    code_name: dict[str, str],
) -> str:
    regime = str((benchmark_context or {}).get("regime", "NEUTRAL") or "NEUTRAL").strip().upper()
    main_today_pct = _fmt_pct((benchmark_context or {}).get("main_today_pct"))
    recent3_cum_pct = _fmt_pct((benchmark_context or {}).get("recent3_cum_pct"))
    breadth = (benchmark_context or {}).get("breadth", {}) or {}
    breadth_ratio = _fmt_pct(breadth.get("ratio_pct"))
    vol_state = str((benchmark_context or {}).get("main_volume_state", "") or "").strip() or "待更新"

    sec_df = selected_df.copy() if isinstance(selected_df, pd.DataFrame) else pd.DataFrame()
    if not sec_df.empty:
        sec_df["industry"] = sec_df.get("industry", "").astype(str).str.strip()
        sec_df = sec_df[sec_df["industry"] != ""]
        score_series = pd.to_numeric(sec_df.get("priority_score"), errors="coerce")
        score_series = score_series.where(
            score_series.notna(), pd.to_numeric(sec_df.get("funnel_score"), errors="coerce")
        )
        sec_df["score"] = score_series.fillna(0.0)

    strong_lines: list[str] = []
    if not sec_df.empty:
        grouped = (
            sec_df.groupby("industry", as_index=False)
            .agg(cnt=("code", "count"), score=("score", "mean"))
            .sort_values(["cnt", "score"], ascending=[False, False])
            .head(3)
        )
        for _, row in grouped.iterrows():
            strong_lines.append(f"- {str(row['industry'])}（样本{int(row['cnt'])}）")

    focus_codes: list[str] = []
    for c in ops_codes:
        cc = str(c).strip()
        if cc and cc not in focus_codes:
            focus_codes.append(cc)
        if len(focus_codes) >= STEP3_COMPLIANCE_FOCUS_LIMIT:
            break
    if len(focus_codes) < STEP3_COMPLIANCE_FOCUS_LIMIT and not sec_df.empty:
        ranked = sec_df.sort_values("score", ascending=False)
        for code in ranked["code"].astype(str).tolist():
            cc = code.strip()
            if cc and cc not in focus_codes:
                focus_codes.append(cc)
            if len(focus_codes) >= STEP3_COMPLIANCE_FOCUS_LIMIT:
                break

    focus_lines = [f"- {c} {code_name.get(c, c)}" for c in focus_codes]

    out: list[str] = [
        "## 📌 今日市场观察简报（合规版）",
        "",
        "⚠️ **重要提示：以下内容由大模型自动生成，仅用于市场研究交流，不构成任何投资建议；股市有风险，投资需谨慎。**",
        "",
        "### 一、今日大盘数据",
        (
            f"- 市场状态: {regime} | 当日涨跌: {main_today_pct} | "
            f"近3日累计: {recent3_cum_pct} | 市场广度: {breadth_ratio} | 量能状态: {vol_state}"
        ),
        "- 说明: 上述为客观量价观察，不构成交易建议。",
        "",
        "### 二、AI分析强势板块",
    ]
    if strong_lines:
        out.extend(strong_lines)
    else:
        out.append("- 暂无明显强势板块（样本不足或分化明显）")

    out.extend(
        [
            "",
            "### 三、今日值得关注（研究观察）",
        ]
    )
    if focus_lines:
        out.extend(focus_lines)
    else:
        out.append("- 今日暂无明确观察标的")

    out.extend(
        [
            "",
            "### 合规声明",
            "- 以下内容为大模型自动生成结果，仅供参考。",
            "- 本简报仅用于市场研究与信息交流，不构成个股推荐或买卖建议。",
            "- 不承诺收益，不涉及代客理财或代客下单。",
            "- **股市有风险，投资需谨慎。**",
            "- 投资决策请独立判断并自行承担风险。",
        ]
    )
    return "\n".join(out).strip() + "\n"


def _call_track_report(
    *,
    track: str,
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
    selected_df: pd.DataFrame,
    provider: str = "gemini",
    llm_base_url: str = "",
) -> tuple[bool, str, str]:
    report = ""
    used_model = ""
    models_to_try = [model]
    if GEMINI_MODEL_FALLBACK and model != GEMINI_MODEL_FALLBACK:
        models_to_try.append(GEMINI_MODEL_FALLBACK)

    for m in models_to_try:
        try:
            report = call_llm(
                provider=provider,
                model=m,
                api_key=api_key,
                system_prompt=system_prompt,
                user_message=user_message,
                base_url=llm_base_url or None,
                timeout=300,
                max_output_tokens=STEP3_MAX_OUTPUT_TOKENS,
            )
            used_model = m
            break
        except Exception as e:
            print(f"[step3] {track} 轨模型 {m} 失败: {e}")
            if m == models_to_try[-1]:
                return (False, "", "")

    if not _has_required_sections(report):
        print(f"[step3] {track} 轨首版研报缺少可识别分层章节，执行一次结构修复")
        report = _repair_report_structure(
            report=report,
            model=used_model or model,
            api_key=api_key,
            selected_codes=selected_codes,
            provider=provider,
            llm_base_url=llm_base_url,
        )
    if not _has_required_sections(report):
        print(f"[step3] {track} 轨结构修复后仍缺少关键章节，追加系统兜底分层")
        report = report.rstrip() + "\n\n" + _build_fallback_sections(selected_df)

    # 校验：检测报告中出现但不属于本轨输入集的股票代码（模型幻觉）
    input_set = set(str(c).strip() for c in selected_codes)
    mentioned = set(re.findall(r"\b(\d{6})\b", report))
    leaked = mentioned - input_set
    if leaked:
        print(f"[step3] ⚠ {track} 轨报告中出现非本轨标的: {','.join(sorted(leaked))}")
        report += f"\n\n> ⚠ 以下代码不在{track}轨输入集中，可能为模型幻觉: {', '.join(sorted(leaked))}"

    return (True, report, used_model or model)


def run(
    symbols_info: list[dict] | list[str],
    webhook_url: str,
    api_key: str,
    model: str,
    benchmark_context: dict | None = None,
    *,
    notify: bool = True,
    provider: str = "gemini",
    llm_base_url: str = "",
    wecom_webhook: str = "",
    dingtalk_webhook: str = "",
) -> tuple[bool, str, str]:
    """
    拉取 OHLCV → 第五步特征工程 → AI 研报 → 飞书/企微/钉钉发送。
    symbols_info: list[{"code", "name", "tag"}] 或 list[str]（向后兼容）。
    """
    if not symbols_info:
        print("[step3] 无输入股票，跳过")
        return (True, "skipped_no_symbols", "")

    # 兼容旧调用（纯 str 列表）
    items: list[dict] = []
    for s in symbols_info:
        if isinstance(s, str):
            items.append({"code": s, "name": s, "tag": ""})
        else:
            items.append(s)

    print(f"[step3] AI 输入股票数={len(items)}（全量命中输入）")
    from cli.progress import report_progress

    report_progress("研报准备", f"输入{len(items)}只", 0.1)

    end_day = resolve_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)

    def _notify_all(title: str, content: str) -> bool:
        """统一向飞书/企微/钉钉推送，返回飞书是否成功。"""
        sent = send_feishu_notification(webhook_url, title, content) if webhook_url else True
        if wecom_webhook:
            send_wecom_notification(wecom_webhook, title, content)
        if dingtalk_webhook:
            send_dingtalk_notification(dingtalk_webhook, title, content)
        return sent

    regime = (benchmark_context or {}).get("regime", "NEUTRAL")
    sector_rotation_ctx = (benchmark_context or {}).get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation_ctx.get("state_map", {}) or {}
    sector_map = fetch_sector_map()
    market_cap_map = fetch_market_cap_map()
    financial_map: dict[str, dict] = {}
    tickflow_api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if tickflow_api_key:
        try:
            from integrations.tickflow_client import TickFlowClient

            _tf = TickFlowClient(api_key=tickflow_api_key)
            codes = [str(i["code"]) for i in items if i.get("code")]
            print(f"[step3] TickFlow 财务指标请求: symbols={len(codes)}")
            raw_fin = _tf.get_financial_metrics(codes, latest=True)
            for sym, records in raw_fin.items():
                if records:
                    financial_map[sym] = records[0]
            missing = max(len(codes) - len(financial_map), 0)
            sample_missing = ",".join(sorted([s for s in codes if s not in financial_map])[:8])
            print(
                f"[step3] TickFlow 财务指标: {len(financial_map)}/{len(codes)}, "
                f"missing={missing}, sample_missing={sample_missing or '-'}"
            )
        except Exception as e:
            print(f"[step3] TickFlow 财务指标加载失败: {e}")
    benchmark_ret_10: float | None = None
    try:
        bench_df = fetch_index_hist("000001", window.start_trade_date, window.end_trade_date)
        benchmark_ret_10 = _safe_return(bench_df["close"], lookback=10)
    except Exception:
        benchmark_ret_10 = None

    failed: list[tuple[str, str]] = []
    candidate_rows: list[dict] = []
    code_to_df: dict[str, pd.DataFrame] = {}
    for item_order, item in enumerate(items):
        code = item["code"]
        name = item.get("name", code)
        tag = item.get("tag", "")
        industry = str(item.get("industry") or sector_map.get(code, "未知行业") or "未知行业").strip()
        rotation_info = sector_rotation_map.get(industry, {}) or {}
        sector_state_code = str(
            item.get("sector_state_code") or rotation_info.get("state", "") or "NEUTRAL_MIXED"
        ).strip()
        sector_state = str(
            item.get("sector_state")
            or rotation_info.get("label", "")
            or SECTOR_STATE_LABELS.get("NEUTRAL_MIXED", "中性混沌")
        ).strip()
        sector_note = str(item.get("sector_note") or rotation_info.get("note", "") or "").strip()
        try:
            df_raw = _fetch_hist(code, window, "qfq")
            df = normalize_hist_from_fetch(df_raw)
            if ENFORCE_TARGET_TRADE_DATE:
                latest_trade_date = _latest_trade_date_from_hist(df)
                if latest_trade_date != window.end_trade_date:
                    df, patched = _append_spot_bar_if_needed(
                        code,
                        df,
                        window.end_trade_date,
                    )
                    if patched:
                        latest_trade_date = _latest_trade_date_from_hist(df)
                        print(f"[step3] {code} 实时快照补偿成功")
                if latest_trade_date != window.end_trade_date:
                    failed.append(
                        (
                            code,
                            f"latest_trade_date={latest_trade_date}, target_trade_date={window.end_trade_date}",
                        )
                    )
                    continue
            code_to_df[code] = df

            close = pd.to_numeric(df["close"], errors="coerce")
            volume = pd.to_numeric(df["volume"], errors="coerce")
            amount = (
                pd.to_numeric(df["amount"], errors="coerce")
                if "amount" in df.columns
                else pd.Series(close * volume, index=df.index, dtype=float)
            )
            if amount.isna().all():
                amount = pd.Series(close * volume, index=df.index, dtype=float)
            ma200 = close.rolling(200).mean()
            latest_close = close.iloc[-1] if len(close) else pd.NA
            latest_ma200 = ma200.iloc[-1] if len(ma200) else pd.NA
            bias_200 = pd.NA
            if pd.notna(latest_close) and pd.notna(latest_ma200) and float(latest_ma200) != 0:
                bias_200 = (float(latest_close) - float(latest_ma200)) / float(latest_ma200) * 100.0

            stock_ret_10 = _safe_return(close, lookback=10)
            rs_10 = stock_ret_10
            if stock_ret_10 is not None and benchmark_ret_10 is not None:
                rs_10 = stock_ret_10 - benchmark_ret_10

            vol_ma20 = volume.rolling(20).mean()
            amount_ma20 = amount.rolling(20).mean()
            vol_ratio = volume / vol_ma20.replace(0, pd.NA)
            min_vol_ratio_5d = pd.to_numeric(vol_ratio.tail(5), errors="coerce").min()
            avg_amount_20_yi = (
                float(amount_ma20.iloc[-1]) / 1e8 if len(amount_ma20) and pd.notna(amount_ma20.iloc[-1]) else pd.NA
            )

            candidate_rows.append(
                {
                    "code": code,
                    "name": name,
                    "input_order": item_order,
                    "tag": tag,
                    "track": str(item.get("track", "")).strip(),
                    "stage": str(item.get("stage", "")).strip(),
                    "funnel_score": pd.to_numeric(item.get("score"), errors="coerce"),
                    "priority_score": pd.to_numeric(item.get("priority_score"), errors="coerce"),
                    "priority_rank": pd.to_numeric(item.get("priority_rank"), errors="coerce"),
                    "selection_source": str(item.get("selection_source", "") or "").strip(),
                    "selection_is_fill": _coerce_bool_like(item.get("selection_is_fill")),
                    "exit_signal": str(item.get("exit_signal", "")).strip(),
                    "exit_price": pd.to_numeric(item.get("exit_price"), errors="coerce"),
                    "exit_reason": str(item.get("exit_reason", "")).strip(),
                    "industry": industry,
                    "sector_state": sector_state,
                    "sector_state_code": sector_state_code,
                    "sector_note": sector_note,
                    "market_cap_yi": pd.to_numeric(market_cap_map.get(code), errors="coerce"),
                    "avg_amount_20_yi": avg_amount_20_yi,
                    "bias_200": bias_200,
                    "rs_10": rs_10,
                    "min_vol_ratio_5d": min_vol_ratio_5d,
                }
            )
        except Exception as e:
            failed.append((code, str(e)))

    if not candidate_rows:
        if failed:
            detail = ", ".join(f"{s}({e})" for s, e in failed)
            print(f"[step3] OHLCV 全部拉取失败: {detail}")
            return (False, "data_all_failed", "")
        return (True, "no_data_but_no_error", "")

    candidates_df = pd.DataFrame(candidate_rows)
    candidates_df["code"] = candidates_df["code"].astype(str).str.strip()
    candidates_df["input_order"] = pd.to_numeric(candidates_df.get("input_order"), errors="coerce")
    candidates_df["input_order"] = (
        candidates_df["input_order"].fillna(pd.Series(range(len(candidates_df)), index=candidates_df.index)).astype(int)
    )
    candidates_df["track"] = candidates_df.get("track", "").astype(str).str.strip()
    candidates_df.loc[~candidates_df["track"].isin(["Trend", "Accum"]), "track"] = "Trend"
    candidates_df["policy_tag"] = ""
    selected_df = candidates_df.copy()
    selected_df["wyckoff_score"] = pd.to_numeric(
        selected_df.get("priority_score"),
        errors="coerce",
    )
    selected_df["wyckoff_score"] = selected_df["wyckoff_score"].where(
        selected_df["wyckoff_score"].notna(),
        pd.to_numeric(selected_df.get("funnel_score"), errors="coerce"),
    )
    selected_df["industry_rank"] = pd.NA
    effective_context_cap = _resolve_step3_context_cap(len(candidates_df))

    if _has_upstream_priority_context(candidates_df):
        selected_df = _select_upstream_priority_candidates(candidates_df)
        fill_count = int(selected_df.get("selection_is_fill", pd.Series(dtype=bool)).sum())
        track_counts = selected_df["track"].value_counts().to_dict() if "track" in selected_df.columns else {}
        print(
            f"[step3] 尊重上游优先级收口: raw={len(candidates_df)} -> selected={len(selected_df)} "
            f"(cap={effective_context_cap}, Trend={int(track_counts.get('Trend', 0))}, "
            f"Accum={int(track_counts.get('Accum', 0))}, fills={fill_count})"
        )
    elif STEP3_ENABLE_COMPRESSION:
        compressed_df = ultimate_compressor(
            candidates_df,
            regime=regime,
            bonus_rate=DYNAMIC_MAINLINE_BONUS_RATE,
            max_total=effective_context_cap,
            max_per_industry=STEP3_MAX_PER_INDUSTRY,
        )
        if compressed_df.empty:
            selected_df = _fallback_candidates_when_compression_empty(candidates_df)
            print(
                "[step3] 压缩器结果为空，回退为受控候选列表 "
                f"(fallback_cap={STEP3_EMPTY_COMPRESSION_FALLBACK_CAP}, selected={len(selected_df)})"
            )
        else:
            selected_df = compressed_df
        print(
            f"[step3] 候选压缩已启用: raw={len(candidates_df)} -> selected={len(selected_df)} "
            f"(regime={regime}, max_total={effective_context_cap}, max_per_industry={STEP3_MAX_PER_INDUSTRY})"
        )
    else:
        print(f"[step3] 候选压缩未启用: selected=全量{len(selected_df)}")

    if effective_context_cap > 0 and len(selected_df) > effective_context_cap:
        before_n = len(selected_df)
        selected_df = selected_df.head(effective_context_cap).reset_index(drop=True)
        print(
            f"[step3] 上下文硬上限生效: selected {before_n} -> {len(selected_df)} "
            f"(effective_context_cap={effective_context_cap}, env_STEP3_MAX_AI_INPUT={STEP3_MAX_AI_INPUT}, "
            f"default_cap={STEP3_DEFAULT_CONTEXT_CAP})"
        )

    selected_df["wyckoff_score"] = pd.to_numeric(
        selected_df.get("priority_score"),
        errors="coerce",
    )
    selected_df["wyckoff_score"] = selected_df["wyckoff_score"].where(
        selected_df["wyckoff_score"].notna(),
        pd.to_numeric(selected_df.get("funnel_score"), errors="coerce"),
    )
    if "industry_rank" not in selected_df.columns:
        selected_df["industry_rank"] = pd.NA

    # P2: RAG 防雷（负面新闻关键词 veto）
    # 注意：RAG 永远在压缩/硬上限之后执行，确保筛查集合已被有效上下文 cap 收口。
    rag_veto_lines: list[str] = []
    rag_veto_preview = ""
    rag_skip_reason = ""
    if STEP3_ENABLE_RAG_VETO and not selected_df.empty:
        rag_status = get_rag_veto_runtime_status()
        if not bool(rag_status.get("enabled")):
            print("[step3][rag] 已关闭（RAG_VETO_ENABLED=0）")
            rag_skip_reason = "RAG_VETO_ENABLED=0"
        else:
            rag_inputs = [
                {"code": str(r.get("code", "")).strip(), "name": str(r.get("name", ""))}
                for _, r in selected_df.iterrows()
            ]
            print(
                f"[step3][rag] 启动：candidates={len(rag_inputs)}, "
                f"source={rag_status.get('source', 'akshare')}, "
                f"lookback_days={rag_status.get('lookback_days')}, "
                f"workers={rag_status.get('max_workers')}"
            )

            veto_map = run_negative_news_veto(rag_inputs)
            vetoed_codes: list[str] = []
            scanned_n = len(veto_map)
            external_ok_n = 0
            relevant_n = 0
            semantic_checked_n = 0
            error_n = 0
            for code, result in veto_map.items():
                if int(result.raw_result_count or 0) > 0:
                    external_ok_n += 1
                if int(result.relevant_result_count or 0) > 0:
                    relevant_n += 1
                if bool(result.semantic_checked):
                    semantic_checked_n += 1
                if result.error:
                    error_n += 1

                hit_text = "、".join(result.hits[:5]) if result.hits else "-"
                print(
                    "[step3][rag] "
                    f"{code} source={result.search_source or '-'} "
                    f"raw={int(result.raw_result_count or 0)} "
                    f"relevant={int(result.relevant_result_count or 0)} "
                    f"hits={hit_text} "
                    f"veto={bool(result.veto)} "
                    f"semantic_checked={bool(result.semantic_checked)} "
                    f"elapsed_ms={int(result.elapsed_ms or 0)}" + (f" err={result.error}" if result.error else "")
                )

                if result.veto:
                    vetoed_codes.append(code)
                    ev_text = f" | 证据: {result.evidence[0]}" if result.evidence else ""
                    semantic_text = ""
                    if result.semantic_checked:
                        semantic_text = f" | 语义判定: 极端负面={result.semantic_negative}" + (
                            f"({result.semantic_reason})" if result.semantic_reason else ""
                        )
                    rag_veto_lines.append(
                        f"- {code} {result.name}: 命中 {hit_text if hit_text != '-' else '负面关键词'}{semantic_text}{ev_text}"
                    )

            rag_summary_lines = [
                f"- 扫描股票: {scanned_n}",
                f"- 新闻拉取成功: {external_ok_n}/{scanned_n}" if scanned_n else "- 新闻拉取成功: 0/0",
                f"- 命中负面关键词: {relevant_n}/{scanned_n}" if scanned_n else "- 命中负面关键词: 0/0",
                f"- 语义二判执行: {semantic_checked_n}",
                f"- 拉取异常: {error_n}",
                f"- veto 剔除: {len(vetoed_codes)}",
            ]

            if vetoed_codes:
                before_n = len(selected_df)
                selected_df = selected_df[~selected_df["code"].astype(str).isin(set(vetoed_codes))].reset_index(
                    drop=True
                )
                print(f"[step3][rag] 负面新闻 veto: {before_n} -> {len(selected_df)}（剔除{len(vetoed_codes)}）")
                rag_veto_preview = (
                    "## 🛡️ RAG 防雷执行摘要（前置）\n"
                    + "\n".join(rag_summary_lines)
                    + "\n\n## 🛑 RAG 防雷已剔除（前置）\n"
                    + "\n".join(rag_veto_lines)
                    + "\n\n---\n"
                )
            else:
                print("[step3][rag] 未命中负面关键词，保持候选不变")
                rag_veto_preview = "## 🛡️ RAG 防雷执行摘要（前置）\n" + "\n".join(rag_summary_lines) + "\n\n---\n"
    else:
        if STEP3_ENABLE_RAG_VETO:
            if selected_df.empty:
                print("[step3][rag] 跳过：候选为空")
                rag_skip_reason = "候选为空"
            elif not is_rag_veto_enabled():
                print("[step3][rag] 跳过：RAG_VETO_ENABLED=0")
                rag_skip_reason = "RAG_VETO_ENABLED=0"
            else:
                print("[step3][rag] 跳过：未满足运行条件")
                rag_skip_reason = "未满足运行条件"
    if STEP3_ENABLE_RAG_VETO and not rag_veto_preview and rag_skip_reason:
        rag_veto_preview = f"## 🛡️ RAG 防雷执行摘要（前置）\n- 执行状态: 跳过\n- 原因: {rag_skip_reason}\n\n---\n"

    selected_codes = [str(x) for x in selected_df["code"].tolist()]
    if not selected_codes:
        report = (
            "# 🏛️ Alpha 投委会机密电报：威科夫盘面审判\n\n"
            "## 💀 逻辑破产\n"
            "- 无（本轮无明确失效标的可判定）\n\n"
            "## ⏳ 储备营地\n"
            "- 无（候选均被 RAG 防雷 veto 或数据不足）\n\n"
            "## 🏹 处于起跳板\n"
            "- 无（风险过高，今日保持观望）"
        )
        if rag_veto_lines:
            report = rag_veto_preview + report + "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
        if notify:
            model_banner = f"🤖 模型: {model}"
            content = f"{model_banner}\n\n{report}"
            title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
            if not _notify_all(title, content):
                return (False, "feishu_failed", report)
            if STEP3_SEND_COMPLIANCE_BRIEF:
                compliance_title = f"📄 市场观察简报（合规版） {date.today().strftime('%Y-%m-%d')}"
                compliance_name_map = {
                    str(x.get("code", "")).strip(): str(x.get("name", x.get("code", ""))).strip()
                    for x in items
                    if isinstance(x, dict) and str(x.get("code", "")).strip()
                }
                compliance_content = _build_compliance_brief(
                    benchmark_context=benchmark_context or {},
                    selected_df=selected_df,
                    ops_codes=[],
                    code_name=compliance_name_map,
                )
                if not _notify_all(compliance_title, compliance_content):
                    print("[step3] 合规简报推送失败（主报告已发送）")
        return (True, "ok", report)

    payloads_by_track: dict[str, list[str]] = {"Trend": [], "Accum": []}
    df_by_track: dict[str, pd.DataFrame] = {
        "Trend": selected_df.iloc[0:0].copy(),
        "Accum": selected_df.iloc[0:0].copy(),
    }
    selected_codes_by_track: dict[str, list[str]] = {"Trend": [], "Accum": []}
    items_by_track: dict[str, list[dict]] = {"Trend": [], "Accum": []}

    for _, row in selected_df.iterrows():
        code = str(row["code"])
        df = code_to_df.get(code)
        if df is None:
            continue
        track_key = str(row.get("track", "")).strip()
        if track_key not in {"Trend", "Accum"}:
            track_key = "Trend"
        policy_val = row.get("policy_tag")
        policy_text = str(policy_val).strip() if isinstance(policy_val, str) and str(policy_val).strip() else None
        _exit_sig = str(row.get("exit_signal", "")).strip() or None
        _exit_price_raw = pd.to_numeric(row.get("exit_price"), errors="coerce")
        _exit_price = float(_exit_price_raw) if pd.notna(_exit_price_raw) else None
        _exit_reason = str(row.get("exit_reason", "")).strip() or None
        payload = generate_stock_payload(
            stock_code=code,
            stock_name=str(row.get("name", code)),
            wyckoff_tag=str(row.get("tag", "")),
            df=df,
            industry=str(row.get("industry", "")),
            market_cap_yi=pd.to_numeric(row.get("market_cap_yi"), errors="coerce"),
            avg_amount_20_yi=pd.to_numeric(row.get("avg_amount_20_yi"), errors="coerce"),
            policy_tag=policy_text,
            track=track_key,
            stage=str(row.get("stage", "")).strip() or None,
            sector_state=str(row.get("sector_state", "")).strip() or None,
            sector_state_code=str(row.get("sector_state_code", "")).strip() or None,
            sector_note=str(row.get("sector_note", "")).strip() or None,
            exit_signal=_exit_sig,
            exit_price=_exit_price,
            exit_reason=_exit_reason,
            financial_metrics=financial_map.get(code),
        )
        payloads_by_track.setdefault(track_key, []).append(payload)
        df_by_track[track_key] = pd.concat(
            [df_by_track[track_key], row.to_frame().T],
            ignore_index=True,
        )
        selected_codes_by_track[track_key].append(code)
        item = next((x for x in items if str(x.get("code")) == code), None)
        if item:
            items_by_track[track_key].append(item)

    benchmark_lines = []
    if benchmark_context:
        breadth_ctx = benchmark_context.get("breadth", {}) or {}
        benchmark_lines.append("[宏观水温 / Benchmark Context]")
        benchmark_lines.append(
            f"regime={benchmark_context.get('regime')}, "
            f"close={benchmark_context.get('close')}, "
            f"ma50={benchmark_context.get('ma50')}, "
            f"ma200={benchmark_context.get('ma200')}, "
            f"ma50_slope_5d={benchmark_context.get('ma50_slope_5d')}"
        )
        benchmark_lines.append(f"recent3_cum_pct={benchmark_context.get('recent3_cum_pct')}")
        if benchmark_context.get("main_vol_ratio_5_20") is not None:
            benchmark_lines.append(
                f"main_vol_ratio_5_20={benchmark_context.get('main_vol_ratio_5_20'):.3f}, "
                f"main_volume_state={benchmark_context.get('main_volume_state')}"
            )
        market_pv_summary = str(benchmark_context.get("market_pv_summary", "") or "").strip()
        market_pv_outlook = str(benchmark_context.get("market_pv_outlook", "") or "").strip()
        if market_pv_summary or market_pv_outlook:
            benchmark_lines.append("[大盘量价推演 / Price-Volume Outlook]")
            if market_pv_summary:
                benchmark_lines.append(market_pv_summary)
            if market_pv_outlook:
                benchmark_lines.append(market_pv_outlook)
        if breadth_ctx:
            benchmark_lines.append(
                f"breadth_pct={breadth_ctx.get('ratio_pct')}, breadth_delta_pct={breadth_ctx.get('delta_pct')}"
            )
        rotation_headline = str(sector_rotation_ctx.get("headline", "")).strip()
        rotation_lines = sector_rotation_ctx.get("overview_lines", []) or []
        if rotation_headline or rotation_lines:
            benchmark_lines.append("[板块轮动 / Sector Rotation]")
            if rotation_headline:
                benchmark_lines.append(rotation_headline)
            benchmark_lines.extend(rotation_lines[:4])

    active_tracks = [track for track in ["Trend", "Accum"] if payloads_by_track.get(track)]
    if not active_tracks:
        detail = ", ".join(f"{s}({e})" for s, e in failed) if failed else "无可用 payload"
        print(f"[step3] 候选存在，但未能生成可用模型输入: {detail}")
        return (False, "payload_build_failed", "")
    candidate_track_counts = candidates_df["track"].value_counts().to_dict() if "track" in candidates_df.columns else {}
    current_regime = str(benchmark_context.get("regime", "")) if benchmark_context else ""
    track_requests: list[dict] = []
    for track in active_tracks:
        user_message = _build_track_user_message(
            track=track,
            benchmark_lines=benchmark_lines,
            payloads=payloads_by_track.get(track, []),
            compressed=STEP3_ENABLE_COMPRESSION,
            raw_count=int(candidate_track_counts.get(track, len(payloads_by_track.get(track, [])))),
            selected_count=len(payloads_by_track.get(track, [])),
            regime=current_regime,
        )
        track_requests.append(
            {
                "track": track,
                "user_message": user_message,
                "selected_count": len(payloads_by_track.get(track, [])),
            }
        )
        _dump_model_input(
            items=items_by_track.get(track, []),
            model=model,
            system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
            user_message=user_message,
            name_hint=track.lower(),
        )

    if STEP3_SKIP_LLM:
        if notify:
            ok, preview_report = _send_input_preview(
                webhook_url=webhook_url,
                model=model,
                system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
                previews=track_requests,
            )
            if not ok:
                return (False, "feishu_failed", preview_report)
        else:
            preview_blocks: list[str] = [
                "# 🧪 Step3 模型输入预演（未调用大模型）",
                "",
                f"- 目标模型: `{model}`",
                f"- 输入股票数: `{sum(int(x.get('selected_count', 0) or 0) for x in track_requests)}`",
                "- 模式: `STEP3_SKIP_LLM=1`",
                "",
            ]
            for req in track_requests:
                preview_blocks.extend(
                    [
                        f"## {TRACK_LABELS.get(str(req.get('track', '')), str(req.get('track', '')))}",
                        "",
                        str(req.get("user_message", "") or ""),
                        "",
                    ]
                )
            preview_report = "\n".join(preview_blocks).strip()
        return (True, "ok_preview", preview_report)

    track_reports: list[tuple[str, str]] = []
    used_models: dict[str, str] = {}
    for _req_idx, request in enumerate(track_requests):
        track = str(request.get("track", "Trend"))
        report_progress("LLM生成", f"{track}轨调用中", 0.3 + 0.5 * _req_idx / max(len(track_requests), 1))
        ok, track_report, used_model = _call_track_report(
            track=track,
            system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
            user_message=str(request.get("user_message", "")),
            model=model,
            api_key=api_key,
            selected_codes=selected_codes_by_track.get(track, []),
            selected_df=df_by_track.get(track, selected_df.iloc[0:0].copy()),
            provider=provider,
            llm_base_url=llm_base_url,
        )
        if not ok:
            return (False, "llm_failed", "")
        used_models[track] = used_model
        track_title = TRACK_LABELS.get(track, track)
        track_reports.append(
            (
                track,
                f"## {track_title}\n\n{_strip_report_title(track_report)}".strip(),
            )
        )

    report = "\n\n---\n\n".join(section for _, section in track_reports).strip()

    unique_used_models = list(dict.fromkeys(used_models.values()))
    if len(unique_used_models) == 1:
        model_banner = f"🤖 模型: {unique_used_models[0]}（分轨调用）"
    else:
        model_banner = "🤖 模型: " + " | ".join(
            f"{TRACK_LABELS.get(track, track)}={used_models.get(track, model)}" for track in active_tracks
        )
    code_name = {str(row.get("code")): str(row.get("name", row.get("code"))) for _, row in selected_df.iterrows()}
    selected_set = set(selected_codes)
    # 优先从 Markdown 操作区提取；若未来回退为结构化 JSON，也保持兼容。
    ops_codes = _extract_ops_codes_from_markdown(report, selected_set)
    structured = _try_parse_structured_report(
        report=report,
        allowed_codes=selected_set,
        code_name=code_name,
    )
    if not ops_codes and structured and structured.get("operation_pool"):
        for item in structured["operation_pool"]:
            code = str(item.get("code", "")).strip()
            if code and code not in ops_codes:
                ops_codes.append(code)
    ops_lines = [f"- {c} {code_name.get(c, c)}" for c in ops_codes]
    ops_preview = "## 🏹 处于起跳板速览（前置）\n" + ("\n".join(ops_lines) if ops_lines else "- 无") + "\n\n---\n"

    content = f"{model_banner}\n\n{rag_veto_preview}{ops_preview}{SPRINGBOARD_ABC_LEGEND}\n{report}"
    if rag_veto_lines:
        content += "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
    print(f"[step3] 飞书发送原文长度={len(content)}（不压缩，交由飞书分片）")
    print(
        "[step3] 研报实际使用模型=" + " | ".join(f"{track}:{used_models.get(track, model)}" for track in active_tracks)
    )
    if failed:
        content += f"\n\n**获取失败**: {', '.join(f'{s}({e})' for s, e in failed)}"

    title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
    if notify:
        if not _notify_all(title, content):
            print("[step3] 飞书推送失败")
            return (False, "feishu_failed", report)
        if STEP3_SEND_COMPLIANCE_BRIEF:
            compliance_title = f"📄 市场观察简报（合规版） {date.today().strftime('%Y-%m-%d')}"
            compliance_content = _build_compliance_brief(
                benchmark_context=benchmark_context or {},
                selected_df=selected_df,
                ops_codes=ops_codes,
                code_name=code_name,
            )
            if not _notify_all(compliance_title, compliance_content):
                print("[step3] 合规简报推送失败（主报告已发送）")
    print(
        f"[step3] 研报发送成功，股票数={sum(len(payloads_by_track.get(t, [])) for t in active_tracks)}，"
        f"拉取失败数={len(failed)}"
    )
    report_progress("研报完成", "", 1.0)
    return (True, "ok", report)
