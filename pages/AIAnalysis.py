# -*- coding: utf-8 -*-
"""AI 分析页：Alpha 虚拟投委会研报。"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from layout import setup_page
from navigation import show_right_nav
from ui_helpers import show_page_loading
from ai_prompts import ALPHA_CIO_SYSTEM_PROMPT
from llm_client import call_llm, SUPPORTED_PROVIDERS, GEMINI_MODELS
from fetch_a_share_csv import (
    _resolve_trading_window,
    _fetch_hist,
    _build_export,
    _normalize_symbols,
    _stock_name_from_code,
)
from utils import extract_symbols_from_text, stock_sector_em

# 沙里淘金 tactic 与 results key 的对应
WYCKOFF_TACTIC_TO_KEY = {
    "抗跌主力": "resisters",
    "突破临界": "jumpers",
    "异常吸筹/出货": "anomalies",
    "启动龙头": "first_board",
}

TRADING_DAYS_OHLCV = 60
ADJUST = "qfq"
MAX_SYMBOLS = 6

setup_page(page_title="AI 分析", page_icon="🤖")

content_col = show_right_nav()
with content_col:
    st.title("🤖 AI 分析")
    st.markdown("基于 Alpha 虚拟投委会系统提示词，对选定股票的 OHLCV 数据进行深度研报分析。")

    # 1) 供应商与模型（首期仅 Gemini）
    st.subheader("API 与模型")
    provider = st.selectbox(
        "API 供应商",
        options=list(SUPPORTED_PROVIDERS),
        format_func=lambda x: "Gemini" if x == "gemini" else x,
        key="ai_provider",
    )
    model = st.selectbox(
        "模型",
        options=list(GEMINI_MODELS),
        key="ai_model",
    )

    # 2) API Key 校验
    api_key = (st.session_state.get("gemini_api_key") or "").strip()
    if not api_key:
        st.toast("请先在设置页录入 API Key", icon="⚠️")
        st.warning("未检测到 API Key，请先在设置页录入后再使用 AI 分析。")
        st.page_link("pages/Settings.py", label="前往设置", icon="⚙️")
        st.stop()

    # 3) 分析类型与标的
    st.subheader("分析内容")
    analysis_type = st.radio(
        "分析类型",
        options=["stock_list", "find_gold"],
        format_func=lambda x: "指定股票代码 (stock_list)" if x == "stock_list" else "沙里淘金结果 (find_gold)",
        horizontal=True,
        key="ai_analysis_type",
    )

    symbols: list[str] = []
    if analysis_type == "stock_list":
        stock_input = st.text_area(
            "股票代码（最多 6 个）",
            placeholder="例如：000001；600519；300364（分号或空格分隔）",
            height=100,
            key="ai_stock_list_input",
        )
        candidates = extract_symbols_from_text(stock_input or "")
        symbols = _normalize_symbols(candidates)[:MAX_SYMBOLS]
        if not symbols:
            st.info("请至少输入 1 个、最多 6 个股票代码。")
        elif len(_normalize_symbols(candidates)) > MAX_SYMBOLS:
            st.caption(f"已自动截取前 {MAX_SYMBOLS} 个代码：{', '.join(symbols)}")
    else:
        payload = st.session_state.get("wyckoff_payload")
        if not payload or not payload.get("results"):
            st.warning("请先在「沙里淘金」页面执行筛选后再进行 AI 分析。")
            st.page_link("pages/WyckoffScreeners.py", label="前往沙里淘金", icon="🧭")
            st.stop()
        tactic = payload.get("tactic")
        key = WYCKOFF_TACTIC_TO_KEY.get(tactic) if tactic else None
        if not key:
            key = next(iter(payload["results"]), None)
        items = (payload["results"].get(key) or [])[:MAX_SYMBOLS]
        symbols = [item[0] for item in items if isinstance(item, (list, tuple)) and len(item) >= 1]
        if not symbols:
            st.warning("当前淘金结果中没有可用的股票代码。请重新执行沙里淘金。")
            st.stop()
        st.caption(f"将使用沙里淘金结果中的 {len(symbols)} 只股票：{', '.join(symbols)}")

    if not symbols:
        st.stop()

    run_btn = st.button("开始分析", type="primary", key="ai_run_btn")

    if run_btn:
        # 时间窗口：近 60 个交易日，前复权
        end_calendar = date.today() - timedelta(days=1)
        try:
            window = _resolve_trading_window(end_calendar, TRADING_DAYS_OHLCV)
        except Exception as e:
            st.error(f"无法解析交易日窗口：{e}")
            st.stop()

        loading = show_page_loading(title="正在拉取 OHLCV 与生成研报…", subtitle="请稍候")
        failed: list[str] = []
        parts: list[str] = []

        try:
            for symbol in symbols:
                try:
                    df_hist = _fetch_hist(symbol, window, ADJUST)
                    sector = stock_sector_em(symbol, timeout=30)
                    df_export = _build_export(df_hist, sector)
                    try:
                        name = _stock_name_from_code(symbol)
                    except Exception:
                        name = symbol
                    csv_text = df_export.to_csv(index=False, encoding="utf-8-sig")
                    parts.append(f"## {symbol} {name}\n\n```csv\n{csv_text}\n```")
                except Exception as e:
                    failed.append(f"{symbol}（{e}）")
                    continue

            if not parts:
                st.error("所有标的拉取失败，无法进行分析。失败详情：" + "; ".join(failed))
                loading.empty()
                st.stop()

            if failed:
                st.caption("以下标的拉取失败，已跳过：" + "; ".join(failed))

            user_message = (
                "请按 Alpha 投委会流程分析以下 OHLCV 数据（CSV 格式）。\n\n"
                + "\n\n".join(parts)
            )

            report_text = call_llm(
                provider=provider,
                model=model,
                api_key=api_key,
                system_prompt=ALPHA_CIO_SYSTEM_PROMPT,
                user_message=user_message,
                timeout=120,
            )
        except ValueError as e:
            loading.empty()
            st.error(str(e))
            st.stop()
        except RuntimeError as e:
            loading.empty()
            st.error(f"模型调用失败：{e}。请检查 Key、网络或稍后重试。")
            st.stop()
        except Exception as e:
            loading.empty()
            st.error(f"发生错误：{e}")
            st.stop()
        finally:
            loading.empty()

        st.subheader("📄 深度研报")
        st.markdown(report_text)
