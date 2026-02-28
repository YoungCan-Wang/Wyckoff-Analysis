# -*- coding: utf-8 -*-
"""
阶段 3：批量 AI 研报
拉取选中股票的 OHLCV → 第五步特征工程 → AI 分析 → 飞书发送
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from integrations.ai_prompts import WYCKOFF_FUNNEL_SYSTEM_PROMPT
from integrations.fetch_a_share_csv import _resolve_trading_window, _fetch_hist
from integrations.llm_client import call_llm
from integrations.data_source import fetch_index_hist, fetch_sector_map
from utils.feishu import send_feishu_notification
from core.wyckoff_engine import normalize_hist_from_fetch

TRADING_DAYS = 500
GEMINI_MODEL_FALLBACK = "gemini-2.0-flash-lite"
OPERATION_TARGET = 6
STEP3_MAX_AI_INPUT = int(os.getenv("STEP3_MAX_AI_INPUT", "25"))
STEP3_MAX_PER_INDUSTRY = int(os.getenv("STEP3_MAX_PER_INDUSTRY", "5"))
STEP3_MAX_OUTPUT_TOKENS = 16384
DYNAMIC_MAINLINE_BONUS_RATE = 0.15
DYNAMIC_MAINLINE_TOP_N = 3
DYNAMIC_MAINLINE_MIN_CLUSTER = 2
STEP3_ENABLE_COMPRESSION = os.getenv("STEP3_ENABLE_COMPRESSION", "").strip().lower() in {
    "1", "true", "yes", "on"
}

RECENT_DAYS = 15
HIGHLIGHT_DAYS = 60
HIGHLIGHT_PCT_THRESHOLD = 5.0
HIGHLIGHT_VOL_RATIO = 2.0
DEBUG_MODEL_IO = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO_FULL = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {"1", "true", "yes", "on"}
CN_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", "15"))


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
    has_watch = ("观察池" in text) or ("自选观察池" in text)
    has_trade = ("可操作池" in text) or ("操作池" in text)
    return has_watch and has_trade


def _repair_report_structure(
    report: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
) -> str:
    """
    当模型未给出“观察池/操作池”双层结构时，做一次结构修复重写。
    """
    if not report.strip():
        return report

    repair_system = (
        "你是格式修复器。请将输入研报重排为标准 Markdown，"
        "必须包含两个章节：1) 观察池（数量不限，含观察条件）"
        f" 2) 可操作池（固定 {OPERATION_TARGET} 只，若不足需说明原因）。"
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
    最后兜底：确保飞书一定出现“观察池/可操作池”结果块。
    """
    if selected_df is None or selected_df.empty:
        return (
            "## 📚 观察池（系统兜底）\n"
            "- 本轮无可用候选。\n\n"
            f"## ⚔️ 可操作池（系统兜底，目标 {OPERATION_TARGET} 只）\n"
            "- 本轮无可操作标的。"
        )

    lines = ["## 📚 观察池（系统兜底）"]
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
    lines.append(f"## ⚔️ 可操作池（系统兜底，目标 {OPERATION_TARGET} 只）")
    top_ops = selected_df.head(OPERATION_TARGET)
    if top_ops.empty:
        lines.append("- 无")
    else:
        for _, row in top_ops.iterrows():
            code = str(row.get("code", ""))
            name = str(row.get("name", code))
            lines.append(
                f"- `{code} {name}` | 条件建仓: 仅在战区内缩量回踩或强势确认后 1/3 试单。"
            )
    return "\n".join(lines)


def _extract_operation_pool_codes(report: str) -> list[str]:
    """
    从模型报告中提取“可操作池/操作池”章节内出现的股票代码（6位）。
    用于发送前置速览，避免长文第一片只显示1只导致阅读不完整。
    """
    if not report:
        return []
    lines = report.splitlines()
    in_ops = False
    codes: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if ("可操作池" in line) or ("操作池" in line):
            in_ops = True
            continue
        if (
            in_ops
            and line.startswith("## ")
            and ("可操作池" not in line and "操作池" not in line)
            and ("🎯" not in line)
        ):
            break
        if not in_ops:
            continue
        for c in re.findall(r"\b\d{6}\b", line):
            if c in seen:
                continue
            seen.add(c)
            codes.append(c)
    return codes


def _job_end_calendar_day() -> date:
    """
    定时任务统一口径：
    - 北京时间收盘后（默认 >=15:00）走 T+0（当天）
    - 收盘前走 T-1（上一自然日）
    """
    now = datetime.now(CN_TZ)
    if now.hour >= MARKET_CLOSE_HOUR:
        return now.date()
    return (now - timedelta(days=1)).date()


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

    selected_codes = [str(x) for x in selected_df["code"].tolist()]
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
        + "请先从全部输入中筛出“值得加入自选观察池”的标的（数量不限），并明确每只的观察条件；"
        + f"再从观察池中严格挑选“次日可买入的操作池”{OPERATION_TARGET}只。\n"
        + f"输出必须包含两个部分：1) 观察池（不限，含观察条件） 2) 操作池（固定{OPERATION_TARGET}只）。\n"
        + "硬约束：操作池必须是观察池子集，且两部分只能使用输入列表中的股票代码。\n\n"
        + "交易执行硬约束：\n"
        + "1) 禁止单点价格指令，必须给“结构战区(Action Zone) + 盘面确认条件(Tape Condition)”。\n"
        + "2) 战区需围绕每只股票的“价格锚点（最新收盘价）”描述，但不得刻舟求剑。\n"
        + "3) 买入触发必须包含量价确认条件（如缩量回踩/拒绝下破）；若放量下破，必须取消买入。\n"
        + "4) 强势突破标的必须给“防踏空策略”：开盘强势确认后可先用计划仓位1/3试单，其余等待二次确认。\n\n"
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
        print("[step3] 首版研报缺少观察池/可操作池，执行一次结构修复")
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
    code_name = {str(row.get("code")): str(row.get("name", row.get("code"))) for _, row in selected_df.iterrows()}
    ops_codes = _extract_operation_pool_codes(report)
    if len(ops_codes) < OPERATION_TARGET:
        # 若模型文本过长或结构异常导致提取不足，补齐到 6 只，保证第一屏可读
        for c in selected_codes:
            if c in ops_codes:
                continue
            ops_codes.append(c)
            if len(ops_codes) >= OPERATION_TARGET:
                break
    ops_lines = [f"- {c} {code_name.get(c, c)}" for c in ops_codes[:OPERATION_TARGET]]
    ops_preview = (
        "## ⚔️ 可操作池速览（前置）\n"
        + ("\n".join(ops_lines) if ops_lines else "- 无")
        + "\n\n---\n"
    )

    content = f"{model_banner}\n\n{ops_preview}\n{report}"
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
