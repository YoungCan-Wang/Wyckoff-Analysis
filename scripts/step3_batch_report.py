# -*- coding: utf-8 -*-
"""
阶段 3：批量 AI 研报
拉取选中股票的 OHLCV → 第五步特征工程 → AI 分析 → 飞书发送
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from integrations.ai_prompts import WYCKOFF_FUNNEL_SYSTEM_PROMPT
from integrations.fetch_a_share_csv import _resolve_trading_window, _fetch_hist
from integrations.llm_client import call_llm
from utils.feishu import send_feishu_notification
from core.wyckoff_engine import normalize_hist_from_fetch

TRADING_DAYS = 500
FEISHU_MAX_LEN = 12000
GEMINI_MODEL_FALLBACK = "gemini-2.0-flash-lite"
OPERATION_TARGET = 6

RECENT_DAYS = 15
HIGHLIGHT_DAYS = 60
HIGHLIGHT_PCT_THRESHOLD = 5.0
HIGHLIGHT_VOL_RATIO = 2.0
DEBUG_MODEL_IO = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO_FULL = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {"1", "true", "yes", "on"}


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


def _compress_report(report: str, max_len: int = FEISHU_MAX_LEN) -> str:
    if len(report) <= max_len:
        return report
    truncated = report[:max_len]
    last_period = truncated.rfind("。")
    if last_period > max_len // 2:
        return truncated[: last_period + 1]
    return truncated + "…"


def generate_stock_payload(
    stock_code: str,
    stock_name: str,
    wyckoff_tag: str,
    df: pd.DataFrame,
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

    header = (
        f"• {stock_code} {stock_name} | 机器标签：{wyckoff_tag}\n"
        f"  [价格锚点] 最新实际收盘价={close_val:.2f}（执行建议需围绕该锚点给出结构战区，不得给单点预测价）。\n"
        f"{background}\n"
    )

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

    end_day = date.today() - timedelta(days=1)
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)

    parts: list[str] = []
    failed: list[tuple[str, str]] = []
    for item in items:
        code = item["code"]
        name = item.get("name", code)
        tag = item.get("tag", "")
        try:
            df_raw = _fetch_hist(code, window, "qfq")
            df = normalize_hist_from_fetch(df_raw)
            payload = generate_stock_payload(code, name, tag, df)
            parts.append(payload)
        except Exception as e:
            failed.append((code, str(e)))

    if not parts:
        if failed:
            detail = ", ".join(f"{s}({e})" for s, e in failed)
            print(f"[step3] OHLCV 全部拉取失败: {detail}")
            return (False, "data_all_failed", "")
        return (True, "no_data_but_no_error", "")

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
        + "以下是通过 Wyckoff Funnel 命中的全量候选名单。\n"
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
    _dump_model_input(items=items, model=model, system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT, user_message=user_message)

    report = ""
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
            )
            break
        except Exception as e:
            print(f"[step3] 模型 {m} 失败: {e}")
            if m == models_to_try[-1]:
                return (False, "llm_failed", "")

    content = _compress_report(report)
    if failed:
        content += f"\n\n**获取失败**: {', '.join(f'{s}({e})' for s, e in failed)}"

    title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
    sent = send_feishu_notification(webhook_url, title, content)
    if not sent:
        print("[step3] 飞书推送失败")
        return (False, "feishu_failed", report)
    print(f"[step3] 研报发送成功，股票数={len(items)}，拉取失败数={len(failed)}")
    return (True, "ok", report)
