# -*- coding: utf-8 -*-
"""
阶段 1：大盘日报
拉取今日大盘指数数据 → AI 分析 → 飞书发送
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

# 确保可导入项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
import pandas as pd

from ai_prompts import ALPHA_CIO_SYSTEM_PROMPT
from fetch_a_share_csv import _resolve_trading_window
from llm_client import call_llm
from utils.feishu import send_feishu_notification

INDEX_CODES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}


def fetch_market_data(trading_days: int = 120) -> str:
    """拉取多指数日 K，返回 CSV 文本。"""
    end_day = date.today() - timedelta(days=1)
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=trading_days)
    start_s = window.start_trade_date.strftime("%Y%m%d")
    end_s = window.end_trade_date.strftime("%Y%m%d")

    parts: list[str] = []
    for code, name in INDEX_CODES.items():
        try:
            df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_s, end_date=end_s)
            if df is not None and not df.empty:
                df = df.rename(columns={"日期": "Date", "收盘": "Close", "涨跌幅": "PctChg", "成交量": "Volume"})
                csv = df[["Date", "Close", "PctChg", "Volume"]].to_csv(index=False, encoding="utf-8-sig")
                parts.append(f"## {name} ({code})\n\n```csv\n{csv}\n```")
        except Exception as e:
            parts.append(f"## {name} ({code})\n\n获取失败: {e}")

    if not parts:
        raise RuntimeError("所有指数数据获取失败")
    return "\n\n".join(parts)


def run(webhook_url: str, api_key: str, model: str) -> bool:
    """执行大盘日报并发送飞书。"""
    csv_text = fetch_market_data()
    user_message = (
        "请基于以下大盘指数日 K 数据，从宏观定势、趋势结构、风险与仓位建议等角度，"
        "输出一份《大盘日报》摘要（控制在 800 字以内，适合飞书卡片展示）。\n\n"
        + csv_text
    )
    report = call_llm(
        provider="gemini",
        model=model,
        api_key=api_key,
        system_prompt=ALPHA_CIO_SYSTEM_PROMPT,
        user_message=user_message,
        timeout=90,
    )
    title = f"📊 大盘日报 {date.today().strftime('%Y-%m-%d')}"
    content = report[:2000]  # 飞书卡片有长度限制
    return send_feishu_notification(webhook_url, title, content)
