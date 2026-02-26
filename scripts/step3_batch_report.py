# -*- coding: utf-8 -*-
"""
阶段 3：批量 AI 研报
拉取选中股票的 OHLCV → AI 分析 → 飞书发送

环境变量：STEP3_MAX_SYMBOLS(6), GEMINI_MODEL_FALLBACK（主模型失败时备用）
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from ai_prompts import ALPHA_CIO_SYSTEM_PROMPT
from fetch_a_share_csv import _resolve_trading_window, _fetch_hist, _build_export, _stock_name_from_code
from llm_client import call_llm
from utils import stock_sector_em
from utils.feishu import send_feishu_notification

TRADING_DAYS = 60
MAX_SYMBOLS = int(os.getenv("STEP3_MAX_SYMBOLS", "6"))
FEISHU_MAX_LEN = 2000


def _compress_report(report: str, max_len: int = FEISHU_MAX_LEN) -> str:
    """优先保留结论、风险、操作建议，再按长度截断。"""
    if len(report) <= max_len:
        return report
    # 简单策略：取前 max_len 字符，尽量在句号处截断
    truncated = report[:max_len]
    last_period = truncated.rfind("。")
    if last_period > max_len // 2:
        return truncated[: last_period + 1]
    return truncated + "…"


def run(
    symbols: list[str],
    webhook_url: str,
    api_key: str,
    model: str,
) -> bool:
    """拉取 symbols 的 OHLCV，生成批量研报并发送飞书。"""
    if not symbols:
        return True

    if len(symbols) > MAX_SYMBOLS:
        print(f"[step3] 超过上限 {MAX_SYMBOLS}，已截断")
    symbols = symbols[:MAX_SYMBOLS]

    end_day = date.today() - timedelta(days=1)
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)

    parts: list[str] = []
    failed: list[tuple[str, str]] = []
    for symbol in symbols:
        try:
            df = _fetch_hist(symbol, window, "qfq")
            sector = stock_sector_em(symbol, timeout=15)
            df_export = _build_export(df, sector)
            try:
                name = _stock_name_from_code(symbol)
            except Exception:
                name = symbol
            csv_text = df_export.to_csv(index=False, encoding="utf-8-sig")
            parts.append(f"## {symbol} {name}\n\n```csv\n{csv_text}\n```")
        except Exception as e:
            failed.append((symbol, str(e)))

    if not parts:
        if failed:
            print(f"[step3] 全部获取失败: {failed}")
        return True

    user_message = (
        "请按 Alpha 投委会流程分析以下 OHLCV 数据（CSV 格式）。"
        "输出精简研报，必须包含：**结论**、**风险**、**操作建议** 三部分，控制在 600 字以内。\n\n"
        + "\n\n".join(parts)
    )

    report = ""
    models_to_try = [model]
    fallback = os.getenv("GEMINI_MODEL_FALLBACK", "").strip()
    if fallback and fallback != model:
        models_to_try.append(fallback)

    for m in models_to_try:
        try:
            report = call_llm(
                provider="gemini",
                model=m,
                api_key=api_key,
                system_prompt=ALPHA_CIO_SYSTEM_PROMPT,
                user_message=user_message,
                timeout=120,
            )
            break
        except Exception as e:
            print(f"[step3] 模型 {m} 失败: {e}")
            if m == models_to_try[-1]:
                raise

    content = _compress_report(report)
    if failed:
        content += f"\n\n**获取失败**: {', '.join(f'{s}({e})' for s, e in failed)}"

    title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
    return send_feishu_notification(webhook_url, title, content)
