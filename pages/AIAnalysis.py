# -*- coding: utf-8 -*-
"""AI 分析页：Alpha 虚拟投委会研报。"""
import os
import random
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st
import akshare as ak

from layout import setup_page
from navigation import show_right_nav
from ui_helpers import show_page_loading
from ai_prompts import ALPHA_CIO_SYSTEM_PROMPT
from llm_client import call_llm, SUPPORTED_PROVIDERS, GEMINI_MODELS
from fetch_a_share_csv import (
    _resolve_trading_window,
    _fetch_hist,
    _build_export,
    get_all_stocks,
    get_stocks_by_board,
    _normalize_symbols,
    _stock_name_from_code,
)
from utils import extract_symbols_from_text, stock_sector_em
from wyckoff_engine import (
    normalize_hist_from_fetch,
    run_screener,
    ScreenerConfig,
)

# 等待时随机展示的股市名人名言（本地列表）
STOCK_QUOTES = [
    "市场永远是对的。——杰西·利弗莫尔",
    "不要与市场争辩，最小阻力线才是方向。——杰西·利弗莫尔",
    "买你懂的，不懂不买。——彼得·林奇",
    "当你持有好公司时，时间站在你这一边。——彼得·林奇",
    "别人恐惧时我贪婪，别人贪婪时我恐惧。——沃伦·巴菲特",
    "价格是你付出的，价值是你得到的。——沃伦·巴菲特",
    "趋势是你的朋友。——华尔街谚语",
    "截断亏损，让利润奔跑。——威廉·埃克哈特",
    "计划你的交易，交易你的计划。——佚名",
    "本金安全第一，先求不败再求胜。——杰西·利弗莫尔",
    "没有纪律，再好的策略也是空谈。——理查德·丹尼斯",
    "市场会奖励耐心与纪律。——查理·芒格",
    "只在关键点出手。——杰西·利弗莫尔",
    "量在价先，资金不会骗人。——威科夫",
    "买在分歧，卖在一致。——情绪流龙头战法",
]

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
    st.markdown("选定股票或筛出候选后，一键生成多维度深度研报，供你决策参考。")

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
        # 沙里淘金：本页直接执行，无需跳转
        find_gold_result: list[tuple[str, float]] = st.session_state.get("ai_find_gold_result") or []
        if find_gold_result:
            symbols = [s for s, _ in find_gold_result[:MAX_SYMBOLS]]
            st.caption(f"将使用本次淘金结果中的 {len(symbols)} 只股票：{', '.join(symbols)}")
            if st.button("重新淘金", key="ai_reset_find_gold"):
                del st.session_state["ai_find_gold_result"]
                st.rerun()
        else:
            with st.container(border=True):
                st.markdown("**先筛选值得关注的股票**")
                tactic_fg = st.radio(
                    "战术",
                    options=["抗跌主力", "突破临界", "异常吸筹/出货", "启动龙头"],
                    horizontal=True,
                    key="ai_find_gold_tactic",
                )
                pool_mode = st.radio("股票池", options=["手动输入", "板块"], horizontal=True, key="ai_pool_mode")
                symbols_input_fg = ""
                board_fg = "all"
                limit_fg = 500
                if pool_mode == "手动输入":
                    symbols_input_fg = st.text_area(
                        "股票代码",
                        placeholder="例如：600519, 000001",
                        height=80,
                        key="ai_find_gold_symbols_text",
                    )
                else:
                    board_fg = st.selectbox(
                        "板块",
                        options=["all", "main", "chinext", "star", "bse"],
                        format_func=lambda v: {"all": "全部 A 股", "main": "主板", "chinext": "创业板", "star": "科创板", "bse": "北交所"}.get(v, v),
                        key="ai_find_gold_board",
                    )
                    limit_fg = int(st.number_input("股票数量上限", min_value=50, max_value=2000, value=500, step=50, key="ai_find_gold_limit"))

                run_find_gold = st.button("执行沙里淘金", type="primary", key="ai_run_find_gold")
                if run_find_gold:
                    if pool_mode == "手动输入":
                        candidates_fg = extract_symbols_from_text(symbols_input_fg or "")
                        pool_symbols = _normalize_symbols(candidates_fg)
                    else:
                        pool_symbols = [s.get("code") for s in get_stocks_by_board(board_fg) if s.get("code")][:limit_fg]
                    if not pool_symbols:
                        st.warning("请先输入股票代码或选择板块。")
                        st.stop()
                    end_cal = date.today() - timedelta(days=1)
                    try:
                        window_fg = _resolve_trading_window(end_cal, 500)
                    except Exception as e:
                        st.error(f"交易日窗口解析失败：{e}")
                        st.stop()
                    start_s, end_s = window_fg.start_trade_date.strftime("%Y%m%d"), window_fg.end_trade_date.strftime("%Y%m%d")
                    progress_ph = st.empty()
                    progress_bar = progress_ph.progress(0)
                    data_map_fg: dict[str, pd.DataFrame] = {}
                    for idx, sym in enumerate(pool_symbols):
                        try:
                            df_h = _fetch_hist(sym, window_fg, "qfq")
                            data_map_fg[sym] = normalize_hist_from_fetch(df_h)
                        except Exception:
                            pass
                        progress_bar.progress((idx + 1) / len(pool_symbols))
                    progress_ph.empty()
                    benchmark_df_fg = None
                    if tactic_fg == "抗跌主力":
                        try:
                            df_idx = ak.index_zh_a_hist(symbol="000001", period="daily", start_date=start_s, end_date=end_s)
                            if df_idx is not None and not df_idx.empty:
                                benchmark_df_fg = normalize_hist_from_fetch(df_idx)
                        except Exception:
                            pass
                    cfg = ScreenerConfig(trading_days=500)
                    stock_name_map_fg = {s.get("code", ""): s.get("name", "") for s in get_all_stocks() if isinstance(s, dict) and s.get("code")}

                    def _list_date(s: str):
                        try:
                            df_i = ak.stock_individual_info_em(symbol=s)
                            if df_i is None or df_i.empty:
                                return None
                            row = df_i.loc[df_i["item"] == "上市日期", "value"]
                            if row.empty:
                                return None
                            return datetime.strptime(str(row.iloc[0]).strip(), "%Y-%m-%d").date()
                        except Exception:
                            return None

                    def _market_cap(s: str):
                        try:
                            df_i = ak.stock_individual_info_em(symbol=s)
                            if df_i is None or df_i.empty:
                                return 0.0
                            row = df_i.loc[df_i["item"] == "总市值", "value"]
                            if row.empty:
                                return 0.0
                            raw = str(row.iloc[0]).strip()
                            if raw.endswith("亿"):
                                return float(raw.replace("亿", "")) * 10000
                            if raw.endswith("万"):
                                return float(raw.replace("万", ""))
                            return float(raw)
                        except Exception:
                            return 0.0

                    result_list = run_screener(
                        tactic_fg,
                        data_map_fg,
                        benchmark_df_fg,
                        cfg,
                        stock_name_map=stock_name_map_fg,
                        list_date_fn=_list_date,
                        market_cap_fn=_market_cap,
                    )
                    st.session_state["ai_find_gold_result"] = result_list
                    st.toast(f"淘金完成，共 {len(result_list)} 只", icon="✅")
                    st.rerun()
                st.stop()

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

        loading = show_page_loading(
            title="正在拉取 OHLCV 与生成研报…",
            subtitle="请稍候",
            quote=random.choice(STOCK_QUOTES),
        )
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
