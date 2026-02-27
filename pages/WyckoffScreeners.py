# -*- coding: utf-8 -*-
"""Wyckoff Funnel — 4 层漏斗筛选页面。"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from utils import extract_symbols_from_text
from app.layout import is_data_source_failure_message, setup_page
from core.wyckoff_engine import (
    FunnelConfig,
    normalize_hist_from_fetch,
    run_funnel,
)
from integrations.fetch_a_share_csv import (
    _resolve_trading_window,
    _fetch_hist,
    get_all_stocks,
    get_stocks_by_board,
    _normalize_symbols,
)
from integrations.data_source import fetch_index_hist, fetch_sector_map, fetch_market_cap_map
from app.navigation import show_right_nav

_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "wyckoff_cache")
)

setup_page(page_title="Wyckoff Funnel", page_icon="🔬")

content_col = show_right_nav()
with content_col:
    st.title("🔬 Wyckoff Funnel")
    st.markdown("4 层漏斗：剥离垃圾 → 强弱甄别 → 板块共振 → 威科夫狙击")

    TRIGGER_LABELS = {
        "spring": "Spring（终极震仓）",
        "lps": "LPS（缩量回踩）",
        "evr": "Effort vs Result（放量不跌）",
    }

    # ---- helpers ----

    @st.cache_data(ttl=3600, show_spinner=False)
    def _stock_name_map() -> dict[str, str]:
        items = get_all_stocks()
        return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}

    def _cache_key(prefix: str, symbol: str, start: str, end: str, adjust: str) -> str:
        return f"{prefix}_{symbol}_{start}_{end}_{adjust}".replace("/", "_")

    def _cache_path(key: str) -> str:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        return os.path.join(_CACHE_DIR, f"{key}.csv")

    def _load_cache(key: str) -> pd.DataFrame | None:
        path = _cache_path(key)
        if not os.path.exists(path):
            return None
        try:
            return pd.read_csv(path)
        except Exception:
            return None

    def _save_cache(key: str, df: pd.DataFrame) -> None:
        try:
            df.to_csv(_cache_path(key), index=False)
        except Exception:
            return

    def _load_hist_with_source(
        symbol: str, window, adjust: str, use_cache: bool,
    ) -> tuple[pd.DataFrame, str]:
        start = window.start_trade_date.strftime("%Y%m%d")
        end = window.end_trade_date.strftime("%Y%m%d")
        cache_key = _cache_key("stock", symbol, start, end, adjust or "none")
        if use_cache:
            cached = _load_cache(cache_key)
            if cached is not None and not cached.empty:
                return cached, "cache"
        df = _fetch_hist(symbol=symbol, window=window, adjust=adjust)
        df = normalize_hist_from_fetch(df)
        if use_cache:
            _save_cache(cache_key, df)
        return df, "data_source"

    def _parse_symbols(pool_mode: str, text: str, board: str, limit_count: int) -> list[str]:
        if pool_mode == "手动输入":
            candidates = extract_symbols_from_text(str(text or ""), valid_codes=None)
            return _normalize_symbols(candidates)
        stocks = get_stocks_by_board(board)
        codes = [s.get("code") for s in stocks if s.get("code")]
        if limit_count > 0:
            return codes[:limit_count]
        return codes

    # ---- sidebar ----

    with st.sidebar:
        st.subheader("漏斗参数")

        st.markdown("**Layer 1: 剥离垃圾**")
        min_cap = st.number_input("最小市值(亿)", min_value=5.0, max_value=100.0, value=20.0, step=5.0, format="%.0f")
        min_amt = st.number_input("近20日均成交额阈值(万)", min_value=1000.0, max_value=20000.0, value=5000.0, step=1000.0, format="%.0f")

        st.markdown("**Layer 2: 强弱甄别**")
        ma_short = st.number_input("短期均线", min_value=10, max_value=100, value=50, step=10)
        ma_long = st.number_input("长期均线", min_value=100, max_value=500, value=200, step=50)
        ma_hold = st.number_input("守线均线", min_value=5, max_value=60, value=20, step=5)

        st.markdown("**Layer 3: 板块共振**")
        top_n = st.number_input("Top-N 行业", min_value=1, max_value=10, value=3, step=1)

        st.markdown("**Layer 4: 威科夫狙击**")
        spring_support_w = st.number_input("Spring 支撑窗口", min_value=20, max_value=120, value=60, step=10)
        lps_vol_dry = st.number_input("LPS 缩量比", min_value=0.1, max_value=0.8, value=0.35, step=0.05, format="%.2f")
        evr_vol_ratio = st.number_input("EvR 量比阈值", min_value=1.0, max_value=5.0, value=2.0, step=0.5, format="%.1f")

        st.divider()
        trading_days = st.number_input("交易日数量", min_value=200, max_value=1200, value=500, step=50)
        use_cache = st.checkbox("使用缓存", value=True)
        max_workers = int(st.number_input("并发拉取数", min_value=1, max_value=16, value=10, step=1))

    # ---- pool ----

    st.subheader("股票池")
    pool_mode = st.radio("来源", options=["板块", "手动输入"], horizontal=True)
    board = "all"
    limit_count = 500
    symbols_input = ""

    if pool_mode == "手动输入":
        symbols_input = st.text_area("股票代码", placeholder="例如: 600519, 000001", height=120)
    else:
        board = st.selectbox(
            "选择板块",
            options=["all", "main", "chinext"],
            format_func=lambda v: {"all": "全部主板+创业板", "main": "主板", "chinext": "创业板"}.get(v, v),
        )
        limit_count = st.number_input("股票数量上限", min_value=50, max_value=5000, value=500, step=100)

    run_btn = st.button("开始漏斗筛选", type="primary")

    if run_btn:
        funnel_cfg = FunnelConfig(
            trading_days=int(trading_days),
            min_market_cap_yi=float(min_cap),
            min_avg_amount_wan=float(min_amt),
            ma_short=int(ma_short),
            ma_long=int(ma_long),
            ma_hold=int(ma_hold),
            top_n_sectors=int(top_n),
            spring_support_window=int(spring_support_w),
            lps_vol_dry_ratio=float(lps_vol_dry),
            evr_vol_ratio=float(evr_vol_ratio),
        )

        symbols = _parse_symbols(pool_mode, symbols_input, board, int(limit_count))
        symbols = [s for s in symbols if s]
        if not symbols:
            st.warning("请先输入股票代码或选择板块")
            st.stop()

        window = _resolve_trading_window(
            end_calendar_day=date.today() - timedelta(days=1),
            trading_days=int(funnel_cfg.trading_days),
        )
        start_s = window.start_trade_date.strftime("%Y%m%d")
        end_s = window.end_trade_date.strftime("%Y%m%d")

        # 元数据
        with st.spinner("加载行业 & 市值数据..."):
            sector_map = fetch_sector_map()
            market_cap_map = fetch_market_cap_map()
            name_map = _stock_name_map()

        # 大盘基准
        bench_df = None
        with st.spinner("加载大盘基准..."):
            try:
                bench_df = fetch_index_hist("000001", start_s, end_s)
            except Exception as exc:
                st.warning(f"大盘基准加载失败: {exc}")

        # 并发拉取日线
        progress = st.progress(0)
        status_text = st.empty()
        data_map: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        total = len(symbols)

        def _fetch_one(sym: str) -> tuple[str, pd.DataFrame | None, str | None, str | None]:
            try:
                df, src = _load_hist_with_source(sym, window, adjust="qfq", use_cache=use_cache)
                return (sym, df, src, None)
            except Exception as exc:
                return (sym, None, None, str(exc))

        completed = 0
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_fetch_one, sym): sym for sym in symbols}
                for future in as_completed(futures):
                    sym, df, src, err = future.result()
                    completed += 1
                    progress.progress(completed / total)
                    status_text.caption(f"已拉取 {completed}/{total}")
                    if err:
                        errors[sym] = err
                    elif df is not None:
                        data_map[sym] = df
        except Exception as exc:
            msg = str(exc)
            if is_data_source_failure_message(msg):
                st.error(msg)
            else:
                st.error(f"拉取出错: {exc}")

        progress.progress(1.0)
        progress.empty()
        status_text.empty()

        # 运行 4 层漏斗
        with st.spinner("运行 4 层漏斗筛选..."):
            result = run_funnel(
                all_symbols=list(data_map.keys()),
                df_map=data_map,
                bench_df=bench_df,
                name_map=name_map,
                market_cap_map=market_cap_map,
                sector_map=sector_map,
                cfg=funnel_cfg,
            )

        st.session_state.funnel_payload = {
            "result": result,
            "total": len(symbols),
            "fetched": len(data_map),
            "errors": errors,
            "name_map": name_map,
            "sector_map": sector_map,
        }

    # ---- 结果展示 ----

    payload = st.session_state.get("funnel_payload")
    if payload:
        result = payload["result"]
        name_map_r = payload["name_map"]
        sector_map_r = payload["sector_map"]

        st.subheader("漏斗结果")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("L1 剥离垃圾", f"{len(result.layer1_symbols)} 只")
        col2.metric("L2 强弱甄别", f"{len(result.layer2_symbols)} 只")
        col3.metric("L3 板块共振", f"{len(result.layer3_symbols)} 只")
        total_hits = sum(len(v) for v in result.triggers.values())
        col4.metric("L4 命中", f"{total_hits} 只")

        if result.top_sectors:
            st.info(f"Top 行业: {', '.join(result.top_sectors)}")

        for key, label in TRIGGER_LABELS.items():
            pairs = sorted(result.triggers.get(key, []), key=lambda x: -x[1])
            st.markdown(f"**{label}**")
            if pairs:
                rows = []
                for code, score in pairs:
                    rows.append({
                        "代码": code,
                        "名称": name_map_r.get(code, ""),
                        "行业": sector_map_r.get(code, ""),
                        "评分": round(score, 3),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.caption("无")

        errs = payload.get("errors", {})
        if errs:
            with st.expander(f"拉取失败明细 ({len(errs)})"):
                for code, msg in list(errs.items())[:50]:
                    st.write(f"{code}: {msg}")
