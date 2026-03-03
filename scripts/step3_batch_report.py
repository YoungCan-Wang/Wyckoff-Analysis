# -*- coding: utf-8 -*-
"""
阶段 3：批量 AI 研报
拉取选中股票的 OHLCV → 第五步特征工程 → AI 分析 → 飞书发送
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from integrations.ai_prompts import WYCKOFF_FUNNEL_SYSTEM_PROMPT
from integrations.fetch_a_share_csv import _resolve_trading_window, _fetch_hist
from integrations.llm_client import call_llm
from integrations.rag_veto import is_rag_veto_enabled, run_negative_news_veto
from integrations.data_source import fetch_index_hist, fetch_sector_map, fetch_stock_spot_snapshot
from utils.feishu import send_feishu_notification
from utils.trading_clock import CN_TZ, resolve_end_calendar_day
from core.wyckoff_engine import normalize_hist_from_fetch

TRADING_DAYS = 500
GEMINI_MODEL_FALLBACK = "gemini-2.0-flash-lite"
STEP3_MAX_AI_INPUT = 0
STEP3_MAX_PER_INDUSTRY = int(os.getenv("STEP3_MAX_PER_INDUSTRY", "5"))
STEP3_MAX_OUTPUT_TOKENS = 32768
DYNAMIC_MAINLINE_BONUS_RATE = 0.15
DYNAMIC_MAINLINE_TOP_N = 3
DYNAMIC_MAINLINE_MIN_CLUSTER = 2
STEP3_ENABLE_COMPRESSION = False
STEP3_ENABLE_RAG_VETO = os.getenv("STEP3_ENABLE_RAG_VETO", "1").strip().lower() in {
    "1", "true", "yes", "on"
}


RECENT_DAYS = 15
HIGHLIGHT_DAYS = 60
HIGHLIGHT_PCT_THRESHOLD = 5.0
HIGHLIGHT_VOL_RATIO = 2.0
DEBUG_MODEL_IO = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO_FULL = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {"1", "true", "yes", "on"}
# 已按策略要求关闭“目标交易日强校验”，避免数据源时差导致候选被整批跳过。
ENFORCE_TARGET_TRADE_DATE = False
STEP3_ENABLE_SPOT_PATCH = os.getenv("STEP3_ENABLE_SPOT_PATCH", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STEP3_SPOT_PATCH_RETRIES = int(os.getenv("STEP3_SPOT_PATCH_RETRIES", "2"))
STEP3_SPOT_PATCH_SLEEP = float(os.getenv("STEP3_SPOT_PATCH_SLEEP", "0.2"))


def _dump_model_input(
    items: list[dict],
    model: str,
    system_prompt: str,
    user_message: str,
) -> str:
    if not DEBUG_MODEL_IO:
        return ""

    logs_dir = os.getenv("LOGS_DIR", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, f"step3_model_input_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    symbols_line = ", ".join(f"{x.get('code', '')}" for x in items)
    body = (
        f"[step3] model={model}\n"
        f"[step3] symbol_count={len(items)}\n"
        f"[step3] symbols={symbols_line}\n"
        f"[step3] system_prompt_len={len(system_prompt)}\n"
        f"[step3] user_message_len={len(user_message)}\n"
    )
    if DEBUG_MODEL_IO_FULL:
        body += (
            "\n===== SYSTEM PROMPT =====\n"
            f"{system_prompt}\n"
            "\n===== USER MESSAGE =====\n"
            f"{user_message}\n"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[step3] 模型输入已落盘: {path}")
    return path


def _has_required_sections(report: str) -> bool:
    text = (report or "").replace(" ", "")
    has_watch = ("继续观察" in text) or ("观察池" in text)
    has_trade = ("立刻建仓" in text) or ("可操作池" in text) or ("操作池" in text)
    return has_watch and has_trade


def _repair_report_structure(
    report: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
) -> str:
    """
    当模型未给出“继续观察/立刻建仓”双层结构时，做一次结构修复重写。
    """
    if not report.strip():
        return report

    repair_system = (
        "你是格式修复器。请将输入研报重排为标准 Markdown，"
        "必须包含两个章节：1) 继续观察（数量不限，含观察条件）"
        " 2) 立刻建仓（数量不限，按优先级排序）。"
        "不可新增未在输入中出现的股票代码。"
    )
    repair_user = (
        "允许使用的股票代码："
        + ", ".join(selected_codes)
        + "\n\n以下是待修复文本：\n\n"
        + report
    )
    try:
        fixed = call_llm(
            provider="gemini",
            model=model,
            api_key=api_key,
            system_prompt=repair_system,
            user_message=repair_user,
            timeout=180,
            max_output_tokens=STEP3_MAX_OUTPUT_TOKENS,
        )
        return fixed or report
    except Exception as e:
        print(f"[step3] 结构修复失败: {e}")
        return report


def _build_fallback_sections(selected_df: pd.DataFrame) -> str:
    """
    最后兜底：确保飞书一定出现“继续观察/立刻建仓”结果块。
    """
    if selected_df is None or selected_df.empty:
        return (
            "## 👀 继续观察（系统兜底）\n"
            "- 本轮无可用候选。\n\n"
            "## 🚀 立刻建仓（系统兜底）\n"
            "- 本轮无可建仓标的。"
        )

    lines = ["## 👀 继续观察（系统兜底）"]
    for _, row in selected_df.iterrows():
        code = str(row.get("code", ""))
        name = str(row.get("name", code))
        tag = str(row.get("tag", ""))
        score = row.get("wyckoff_score")
        score_text = f"{float(score):.3f}" if pd.notna(score) else "-"
        lines.append(
            f"- `{code} {name}` | 标签: {tag or '-'} | 量化分: {score_text} | 观察条件: 回踩结构战区时需缩量确认。"
        )

    lines.append("")
    lines.append("## 🚀 立刻建仓（系统兜底）")
    lines.append("- 无（模型未输出可建仓标的，保持观察）")
    return "\n".join(lines)


def _extract_json_block(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start : end + 1]
    return raw


def _normalize_structured_pool(
    payload: dict,
    allowed_codes: set[str],
    code_name: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    watch_raw = (
        payload.get("continue_watch")
        or payload.get("observe_pool")
        or payload.get("watch_pool")
        or payload.get("observation_pool")
        or payload.get("watchlist")
        or payload.get("继续观察")
        or payload.get("观察池")
        or []
    )
    ops_raw = (
        payload.get("build_now")
        or payload.get("immediate_build")
        or payload.get("operation_pool")
        or payload.get("tradable_pool")
        or payload.get("立刻建仓")
        or payload.get("操作池")
        or payload.get("可操作池")
        or []
    )

    watch_items: list[dict[str, str]] = []
    op_items: list[dict[str, str]] = []
    seen_watch: set[str] = set()
    seen_ops: set[str] = set()

    if isinstance(watch_raw, list):
        for item in watch_raw:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not re.fullmatch(r"\d{6}", code) or code not in allowed_codes:
                continue
            if code in seen_watch:
                continue
            seen_watch.add(code)
            watch_items.append(
                {
                    "code": code,
                    "name": str(item.get("name", "")).strip() or code_name.get(code, code),
                    "reason": str(item.get("reason", "")).strip(),
                    "condition": str(item.get("condition", "")).strip(),
                }
            )

    if isinstance(ops_raw, list):
        for item in ops_raw:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not re.fullmatch(r"\d{6}", code) or code not in allowed_codes:
                continue
            if code in seen_ops:
                continue
            seen_ops.add(code)
            op_items.append(
                {
                    "code": code,
                    "name": str(item.get("name", "")).strip() or code_name.get(code, code),
                    "action": str(item.get("action", "")).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                    "entry_condition": str(item.get("entry_condition", "")).strip(),
                }
            )

    return {
        "watch_pool": watch_items,
        "operation_pool": op_items,
    }


def _try_parse_structured_report(
    report: str,
    allowed_codes: set[str],
    code_name: dict[str, str],
) -> dict[str, list[dict[str, str]]] | None:
    raw = (report or "").strip()
    if not raw:
        return None
    for candidate in [raw, _extract_json_block(raw)]:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        normalized = _normalize_structured_pool(payload, allowed_codes, code_name)
        if normalized["watch_pool"] or normalized["operation_pool"]:
            return normalized
    return None





def _extract_codes_from_text(
    text: str,
    allowed_codes: set[str],
) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for code in re.findall(r"\b\d{6}\b", text or ""):
        if code not in allowed_codes or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _job_end_calendar_day() -> date:
    """
    定时任务统一口径：
    - 北京时间 17:00-23:59 走 T（当天）
    - 北京时间 00:00-16:59 走 T-1（上一自然日）
    """
    return resolve_end_calendar_day()


def _latest_trade_date_from_hist(df: pd.DataFrame) -> date | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    if s.empty:
        return None
    return s.iloc[-1].date()


def _append_spot_bar_if_needed(
    code: str,
    df: pd.DataFrame,
    target_trade_date: date,
) -> tuple[pd.DataFrame, bool]:
    if not STEP3_ENABLE_SPOT_PATCH or df is None or df.empty:
        return (df, False)
    latest_trade_date = _latest_trade_date_from_hist(df)
    if latest_trade_date is None or latest_trade_date >= target_trade_date:
        return (df, False)
    if target_trade_date != datetime.now(CN_TZ).date():
        return (df, False)

    df_s = df.sort_values("date").reset_index(drop=True)
    last_close_series = pd.to_numeric(df_s.get("close"), errors="coerce").dropna()
    prev_close = float(last_close_series.iloc[-1]) if not last_close_series.empty else None

    for attempt in range(max(STEP3_SPOT_PATCH_RETRIES, 1)):
        snap = fetch_stock_spot_snapshot(code, force_refresh=attempt > 0)
        close_v = None if not snap else snap.get("close")
        if close_v is None or float(close_v) <= 0:
            if attempt < max(STEP3_SPOT_PATCH_RETRIES, 1) - 1:
                time.sleep(max(STEP3_SPOT_PATCH_SLEEP, 0.0))
            continue

        close_f = float(close_v)
        open_f = float(snap.get("open")) if snap and snap.get("open") is not None else close_f
        high_raw = float(snap.get("high")) if snap and snap.get("high") is not None else close_f
        low_raw = float(snap.get("low")) if snap and snap.get("low") is not None else close_f
        high_f = max(high_raw, open_f, close_f)
        low_f = min(low_raw, open_f, close_f)
        volume_f = float(snap.get("volume")) if snap and snap.get("volume") is not None else 0.0
        amount_f = float(snap.get("amount")) if snap and snap.get("amount") is not None else 0.0
        pct_f = float(snap.get("pct_chg")) if snap and snap.get("pct_chg") is not None else None
        if pct_f is None and prev_close and prev_close > 0:
            pct_f = (close_f - prev_close) / prev_close * 100.0

        new_row = {
            "date": target_trade_date.isoformat(),
            "open": open_f,
            "high": high_f,
            "low": low_f,
            "close": close_f,
            "volume": volume_f,
            "amount": amount_f,
            "pct_chg": pct_f if pct_f is not None else 0.0,
        }
        patched = pd.concat([df_s, pd.DataFrame([new_row])], ignore_index=True)
        patched = patched.sort_values("date").reset_index(drop=True)
        return (patched, True)
    return (df, False)


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
    df["dry_score"] = df["min_vol_ratio_5d"].rank(
        pct=True, ascending=False, method="average"
    )
    df["base_wyckoff_score"] = 0.6 * df["rs_score"] + 0.4 * df["dry_score"]

    # 动态主线识别：候选池内“有集群且相对强度高”的行业
    industry_stats = (
        df.groupby("industry", as_index=False)
        .agg(stock_count=("code", "count"), avg_rs=("rs_score", "mean"))
    )
    valid_industry_stats = industry_stats[
        industry_stats["stock_count"] >= DYNAMIC_MAINLINE_MIN_CLUSTER
    ]
    hot_industries: set[str] = set()
    if not valid_industry_stats.empty:
        hot_industries = set(
            valid_industry_stats.nlargest(DYNAMIC_MAINLINE_TOP_N, "avg_rs")["industry"]
            .astype(str)
            .tolist()
        )
    df["is_hot_mainline"] = df["industry"].astype(str).isin(hot_industries)
    df["policy_tag"] = df.apply(
        lambda r: _format_mainline_tag(str(r.get("industry", "")), bool(r.get("is_hot_mainline"))),
        axis=1,
    )
    df["dynamic_bonus"] = df["is_hot_mainline"].map(
        lambda v: float(bonus_rate) if bool(v) else 0.0
    )
    df["wyckoff_score"] = df["base_wyckoff_score"] * (1.0 + df["dynamic_bonus"])

    # 先全局排序，再做行业拥挤度限制
    df = df.sort_values("wyckoff_score", ascending=False).copy()
    df["industry_rank"] = (
        df.groupby("industry")["wyckoff_score"]
        .rank(ascending=False, method="first")
        .astype(int)
    )
    df = df.groupby("industry", group_keys=False).head(max_per_industry)
    df = df.head(max_total).reset_index(drop=True)
    if hot_industries:
        print(f"[step3] 动态主线行业: {', '.join(sorted(hot_industries))}")
    else:
        print("[step3] 动态主线行业: 无（未形成有效行业集群）")
    return df


def generate_stock_payload(
    stock_code: str,
    stock_name: str,
    wyckoff_tag: str,
    df: pd.DataFrame,
    *,
    industry: str | None = None,
    quant_score: float | None = None,
    industry_rank: int | None = None,
    policy_tag: str | None = None,
) -> str:
    """
    第五步：将 500 天 OHLCV 浓缩为发给 AI 的高密度文本。
    1. 大背景（MA50 / MA200 / 乖离率）
    2. 近 15 日量价切片（放量比 + 涨跌幅）
    3. 近 60 日异动高光时刻
    """
    df = df.copy().sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    df["ma50"] = close.rolling(50).mean()
    df["ma200"] = close.rolling(200).mean()
    df["vol_ma20"] = volume.rolling(20).mean()
    df["pct_chg_calc"] = close.pct_change() * 100

    latest = df.iloc[-1]
    ma50_val = latest["ma50"]
    ma200_val = latest["ma200"]
    close_val = latest["close"]

    if pd.notna(ma50_val) and pd.notna(ma200_val) and ma200_val > 0:
        if ma50_val > ma200_val:
            trend = "长期多头排列 (MA50 > MA200)"
        else:
            trend = "长期空头或震荡 (MA50 <= MA200)"
        bias_200 = (close_val - ma200_val) / ma200_val * 100
        background = (
            f"  [结构背景] 现价:{close_val:.2f}, MA50:{ma50_val:.2f}, MA200:{ma200_val:.2f}。"
            f"{trend}，年线乖离率:{bias_200:.1f}%"
        )
    else:
        background = f"  [结构背景] 现价:{close_val:.2f}（数据不足以计算 MA200）"

    policy_prefix = f" {policy_tag}" if policy_tag else ""
    header = (
        f"• {stock_code} {stock_name}{policy_prefix} | 机器标签：{wyckoff_tag}\n"
        f"  [价格锚点] 最新实际收盘价={close_val:.2f}（执行建议需围绕该锚点给出结构战区，不得给单点预测价）。\n"
        f"{background}\n"
    )
    if industry:
        header += f"  [行业] {industry}\n"
    if quant_score is not None:
        rank_text = f"，行业内排名 Top {industry_rank}" if industry_rank is not None else ""
        header += f"  [量化评分] 综合人因子得分: {quant_score:.3f}{rank_text}\n"

    # 近 15 日量价切片
    recent = df.tail(RECENT_DAYS)
    recent_lines = ["  [近15日量价切片]:"]
    for _, row in recent.iterrows():
        vol_ratio = row["volume"] / row["vol_ma20"] if pd.notna(row["vol_ma20"]) and row["vol_ma20"] > 0 else 0
        pct = row["pct_chg_calc"] if pd.notna(row["pct_chg_calc"]) else 0
        date_str = str(row["date"])[5:10]
        recent_lines.append(f"    {date_str}: 收{row['close']:.2f} ({pct:+.1f}%), 量比:{vol_ratio:.1f}x")

    # 近 60 日异动高光
    tail60 = df.tail(HIGHLIGHT_DAYS)
    highlights = []
    for _, row in tail60.iterrows():
        pct = row["pct_chg_calc"] if pd.notna(row["pct_chg_calc"]) else 0
        vol_ratio = row["volume"] / row["vol_ma20"] if pd.notna(row["vol_ma20"]) and row["vol_ma20"] > 0 else 0
        if abs(pct) >= HIGHLIGHT_PCT_THRESHOLD or vol_ratio >= HIGHLIGHT_VOL_RATIO:
            date_str = str(row["date"])[5:10]
            tag_parts = []
            if abs(pct) >= HIGHLIGHT_PCT_THRESHOLD:
                tag_parts.append(f"涨跌{pct:+.1f}%")
            if vol_ratio >= HIGHLIGHT_VOL_RATIO:
                tag_parts.append(f"量比{vol_ratio:.1f}x")
            highlights.append(f"    {date_str}: 收{row['close']:.2f} ({', '.join(tag_parts)})")

    highlight_section = ""
    if highlights:
        highlight_section = "\n  [近60日异动高光]:\n" + "\n".join(highlights) + "\n"

    return header + "\n".join(recent_lines) + "\n" + highlight_section + "\n"


def run(
    symbols_info: list[dict] | list[str],
    webhook_url: str,
    api_key: str,
    model: str,
    benchmark_context: dict | None = None,
) -> tuple[bool, str, str]:
    """
    拉取 OHLCV → 第五步特征工程 → AI 研报 → 飞书发送。
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

    end_day = _job_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)

    regime = (benchmark_context or {}).get("regime", "NEUTRAL")
    sector_map = fetch_sector_map()
    benchmark_ret_10: float | None = None
    try:
        bench_df = fetch_index_hist("000001", window.start_trade_date, window.end_trade_date)
        benchmark_ret_10 = _safe_return(bench_df["close"], lookback=10)
    except Exception:
        benchmark_ret_10 = None

    parts: list[str] = []
    failed: list[tuple[str, str]] = []
    candidate_rows: list[dict] = []
    code_to_df: dict[str, pd.DataFrame] = {}
    for item in items:
        code = item["code"]
        name = item.get("name", code)
        tag = item.get("tag", "")
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
            vol_ratio = volume / vol_ma20.replace(0, pd.NA)
            min_vol_ratio_5d = pd.to_numeric(vol_ratio.tail(5), errors="coerce").min()

            candidate_rows.append(
                {
                    "code": code,
                    "name": name,
                    "tag": tag,
                    "industry": sector_map.get(code, "未知行业"),
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
    candidates_df["policy_tag"] = ""
    selected_df = candidates_df.copy()
    selected_df["wyckoff_score"] = pd.NA
    selected_df["industry_rank"] = pd.NA

    if STEP3_ENABLE_COMPRESSION:
        compressed_df = ultimate_compressor(
            candidates_df,
            regime=regime,
            bonus_rate=DYNAMIC_MAINLINE_BONUS_RATE,
            max_total=STEP3_MAX_AI_INPUT,
            max_per_industry=STEP3_MAX_PER_INDUSTRY,
        )
        if compressed_df.empty:
            print("[step3] 压缩器结果为空，回退为全量候选列表")
        else:
            selected_df = compressed_df
        print(
            f"[step3] 候选压缩已启用: raw={len(candidates_df)} -> selected={len(selected_df)} "
            f"(regime={regime}, max_total={STEP3_MAX_AI_INPUT}, max_per_industry={STEP3_MAX_PER_INDUSTRY})"
        )
    else:
        print(f"[step3] 候选压缩未启用: selected=全量{len(selected_df)}")

    if STEP3_MAX_AI_INPUT > 0 and len(selected_df) > STEP3_MAX_AI_INPUT:
        before_n = len(selected_df)
        selected_df = selected_df.head(STEP3_MAX_AI_INPUT).reset_index(drop=True)
        print(
            f"[step3] 上下文硬上限生效: selected {before_n} -> {len(selected_df)} "
            f"(STEP3_MAX_AI_INPUT={STEP3_MAX_AI_INPUT})"
        )

    # P2: RAG 防雷（负面新闻关键词 veto）
    # 注意：RAG 永远在压缩/硬上限之后执行，确保筛查集合最多为 STEP3_MAX_AI_INPUT。
    rag_veto_lines: list[str] = []
    if STEP3_ENABLE_RAG_VETO and is_rag_veto_enabled() and not selected_df.empty:
        rag_inputs = [
            {"code": str(r.get("code", "")).strip(), "name": str(r.get("name", ""))}
            for _, r in selected_df.iterrows()
        ]
        veto_map = run_negative_news_veto(rag_inputs)
        vetoed_codes: list[str] = []
        for code, result in veto_map.items():
            if result.error:
                print(f"[step3][rag] {code} 检索异常: {result.error}")
            if result.veto:
                vetoed_codes.append(code)
                hit_text = "、".join(result.hits[:5]) if result.hits else "负面关键词"
                ev_text = f" | 证据: {result.evidence[0]}" if result.evidence else ""
                rag_veto_lines.append(f"- {code} {result.name}: 命中 {hit_text}{ev_text}")
        if vetoed_codes:
            before_n = len(selected_df)
            selected_df = selected_df[~selected_df["code"].astype(str).isin(set(vetoed_codes))].reset_index(drop=True)
            print(f"[step3][rag] 负面新闻 veto: {before_n} -> {len(selected_df)}（剔除{len(vetoed_codes)}）")
        else:
            print("[step3][rag] 未命中负面关键词，保持候选不变")
    else:
        if STEP3_ENABLE_RAG_VETO:
            print("[step3][rag] 未启用（缺少 TAVILY_API_KEY/SERPAPI_API_KEY 或候选为空）")

    selected_codes = [str(x) for x in selected_df["code"].tolist()]
    if not selected_codes:
        report = (
            "# 🏛️ Alpha 投委会机密电报：今日最终决断\n\n"
            "## 👀 继续观察\n"
            "- 无（候选均被 RAG 防雷 veto 或数据不足）\n\n"
            "## 🚀 立刻建仓\n"
            "- 无（风险过高，今日观望）"
        )
        if rag_veto_lines:
            report += "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
        model_banner = f"🤖 模型: {model}"
        content = f"{model_banner}\n\n{report}"
        title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
        sent = send_feishu_notification(webhook_url, title, content)
        if not sent:
            return (False, "feishu_failed", report)
        return (True, "ok", report)

    for _, row in selected_df.iterrows():
        code = str(row["code"])
        df = code_to_df.get(code)
        if df is None:
            continue
        policy_val = row.get("policy_tag")
        policy_text = (
            str(policy_val).strip()
            if isinstance(policy_val, str) and str(policy_val).strip()
            else None
        )
        payload = generate_stock_payload(
            stock_code=code,
            stock_name=str(row.get("name", code)),
            wyckoff_tag=str(row.get("tag", "")),
            df=df,
            industry=str(row.get("industry", "")),
            quant_score=float(row["wyckoff_score"]) if pd.notna(row.get("wyckoff_score")) else None,
            industry_rank=int(row["industry_rank"]) if pd.notna(row.get("industry_rank")) else None,
            policy_tag=policy_text,
        )
        parts.append(payload)

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
        benchmark_lines.append(
            f"recent3_pct={benchmark_context.get('recent3_pct')}, "
            f"recent3_cum_pct={benchmark_context.get('recent3_cum_pct')}, "
            f"tuned={benchmark_context.get('tuned')}"
        )
        if breadth_ctx:
            benchmark_lines.append(
                f"breadth_pct={breadth_ctx.get('ratio_pct')}, "
                f"breadth_prev_pct={breadth_ctx.get('prev_ratio_pct')}, "
                f"breadth_delta_pct={breadth_ctx.get('delta_pct')}, "
                f"breadth_sample={breadth_ctx.get('sample_size')}, "
                f"breadth_ma_window={breadth_ctx.get('ma_window')}"
            )

    user_message = (
        ("{}\n\n".format("\n".join(benchmark_lines)) if benchmark_lines else "")
        + (
            (
                f"[量化压缩] 候选已从 {len(candidates_df)} 只压缩到 {len(parts)} 只，"
                f"regime={regime}, max_total={STEP3_MAX_AI_INPUT}, "
                f"max_per_industry={STEP3_MAX_PER_INDUSTRY}。\n\n"
            )
            if STEP3_ENABLE_COMPRESSION and len(candidates_df) > len(parts)
            else ""
        )
        + (
            "以下是通过 Wyckoff Funnel 命中并经量化压缩后的候选名单。\n"
            if STEP3_ENABLE_COMPRESSION
            else "以下是通过 Wyckoff Funnel 命中的全量候选名单（未压缩）。\n"
        )
        + "请仅做二分类："
        + "1) 继续观察 2) 立刻建仓。\n"
        + "输出必须包含这两个部分，且只能使用输入列表中的股票代码，不得遗漏或新增。\n\n"
        + "交易执行硬约束：\n"
        + "1) 禁止单点价格指令，必须给“结构战区(Action Zone) + 盘面确认条件(Tape Condition)”。\n"
        + "2) 战区需围绕每只股票的“价格锚点（最新收盘价）”描述，但不得刻舟求剑。\n"
        + "3) 买入触发必须包含量价确认条件（如缩量回踩/拒绝下破）；若放量下破，必须取消买入。\n"
        + "4) 强势突破标的必须给“防踏空策略”：开盘强势确认后可先用计划仓位1/3试单，其余等待二次确认。\n\n"
        + (
            "[RAG防雷剔除清单]\n"
            + "\n".join(rag_veto_lines)
            + "\n\n"
            if rag_veto_lines
            else ""
        )
        + "\n".join(parts)
    )
    selected_set = set(selected_codes)
    selected_items = [x for x in items if str(x.get("code")) in selected_set]
    _dump_model_input(items=selected_items, model=model, system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT, user_message=user_message)

    report = ""
    used_model = ""
    models_to_try = [model]
    if GEMINI_MODEL_FALLBACK and GEMINI_MODEL_FALLBACK != model:
        models_to_try.append(GEMINI_MODEL_FALLBACK)

    for m in models_to_try:
        try:
            report = call_llm(
                provider="gemini",
                model=m,
                api_key=api_key,
                system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
                user_message=user_message,
                timeout=300,
                max_output_tokens=STEP3_MAX_OUTPUT_TOKENS,
            )
            used_model = m
            break
        except Exception as e:
            print(f"[step3] 模型 {m} 失败: {e}")
            if m == models_to_try[-1]:
                return (False, "llm_failed", "")

    if not _has_required_sections(report):
        print("[step3] 首版研报缺少继续观察/立刻建仓，执行一次结构修复")
        report = _repair_report_structure(
            report=report,
            model=used_model or model,
            api_key=api_key,
            selected_codes=selected_codes,
        )
    if not _has_required_sections(report):
        print("[step3] 结构修复后仍缺少关键章节，追加系统兜底分层")
        report = report.rstrip() + "\n\n" + _build_fallback_sections(selected_df)

    model_banner = f"🤖 模型: {used_model or model}"
    code_name = {
        str(row.get("code")): str(row.get("name", row.get("code")))
        for _, row in selected_df.iterrows()
    }
    selected_set = set(selected_codes)
    # 优先直接 JSON 解析；若无法解析则不做“补齐到固定数量”。
    structured = _try_parse_structured_report(
        report=report,
        allowed_codes=selected_set,
        code_name=code_name,
    )
    ops_codes: list[str] = []
    if structured and structured.get("operation_pool"):
        for item in structured["operation_pool"]:
            code = str(item.get("code", "")).strip()
            if code and code not in ops_codes:
                ops_codes.append(code)
    ops_lines = [f"- {c} {code_name.get(c, c)}" for c in ops_codes]
    ops_preview = (
        "## 🚀 立刻建仓速览（前置）\n"
        + ("\n".join(ops_lines) if ops_lines else "- 无")
        + "\n\n---\n"
    )

    content = f"{model_banner}\n\n{ops_preview}\n{report}"
    if rag_veto_lines:
        content += "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
    print(f"[step3] 飞书发送原文长度={len(content)}（不压缩，交由飞书分片）")
    print(f"[step3] 研报实际使用模型={used_model or model}")
    if failed:
        content += f"\n\n**获取失败**: {', '.join(f'{s}({e})' for s, e in failed)}"

    title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
    sent = send_feishu_notification(webhook_url, title, content)
    if not sent:
        print("[step3] 飞书推送失败")
        return (False, "feishu_failed", report)
    print(f"[step3] 研报发送成功，股票数={len(parts)}，拉取失败数={len(failed)}")
    return (True, "ok", report)
