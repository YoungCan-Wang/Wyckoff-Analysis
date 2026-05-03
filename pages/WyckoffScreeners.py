"""Wyckoff Funnel 后台筛选页。"""

import pandas as pd
import streamlit as st

from app.background_jobs import (
    background_jobs_ready_for_current_user,
    load_latest_job_result,
    refresh_background_job_data,
    render_background_job_status,
    submit_background_job,
    sync_background_job_state,
)
from app.layout import setup_page
from app.navigation import show_right_nav
from utils import extract_symbols_from_text

setup_page(page_title="Wyckoff Funnel", page_icon="🔬")

TRIGGER_LABELS = {
    "sos": "SOS（量价点火）",
    "spring": "Spring（终极震仓）",
    "lps": "LPS（缩量回踩）",
    "evr": "Effort vs Result（放量不跌）",
}
STATE_KEY = "funnel_background_job"


def _parse_symbols(text: str) -> str:
    codes = extract_symbols_from_text(str(text or ""), valid_codes=None)
    deduped: list[str] = []
    seen: set[str] = set()
    for code in codes:
        code_s = str(code or "").strip()
        if not code_s or code_s in seen:
            continue
        seen.add(code_s)
        deduped.append(code_s)
    return ",".join(deduped)


def _render_job_status(state: dict | None) -> dict | None:
    return render_background_job_status(state, noun="筛选")


def _render_funnel_result(result: dict) -> None:
    summary = result.get("summary", {}) or {}
    metrics = result.get("metrics", {}) or {}
    trigger_groups = result.get("trigger_groups", {}) or {}
    symbols_for_report = result.get("symbols_for_report", []) or []

    st.subheader("漏斗结果")
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("股票池", int(summary.get("total_symbols", 0) or 0))
    col2.metric("L1", int(summary.get("layer1", 0) or 0))
    col3.metric("L2", int(summary.get("layer2", 0) or 0))
    col4.metric("L3", int(summary.get("layer3", 0) or 0))
    col5.metric("L4 命中", int(summary.get("l4_unique_hits", 0) or 0))

    top_sectors = result.get("top_sectors", []) or []
    if top_sectors:
        st.info(f"Top 行业: {', '.join(str(x) for x in top_sectors)}")

    st.caption("后台版结果只回传轻量摘要与候选，不再把全量 OHLCV 明细塞进页面会话。")

    st.markdown("### AI 候选池")
    if symbols_for_report:
        st.session_state["ai_find_gold_background_symbols"] = symbols_for_report
        rows = []
        for item in symbols_for_report:
            rows.append(
                {
                    "代码": str(item.get("code", "")),
                    "名称": str(item.get("name", "")),
                    "行业": str(item.get("industry", "")),
                    "轨道": str(item.get("track", "")),
                    "阶段": str(item.get("stage", "")),
                    "标签": str(item.get("tag", "")),
                    "评分": round(float(item.get("score", 0.0) or 0.0), 3),
                    "风控": str(item.get("exit_signal", "") or "-"),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.page_link("pages/AIAnalysis.py", label="前往 AI 分析页使用这批候选", icon="🤖")
    else:
        st.caption("无 AI 候选。")

    st.markdown("### L4 触发分组")
    for key, label in TRIGGER_LABELS.items():
        rows = trigger_groups.get(key, []) or []
        st.markdown(f"**{label}**")
        if not rows:
            st.caption("无")
            continue
        table_rows = [
            {
                "代码": str(row.get("code", "")),
                "名称": str(row.get("name", "")),
                "行业": str(row.get("industry", "")),
                "评分": round(float(row.get("score", 0.0) or 0.0), 3),
            }
            for row in rows
        ]
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    benchmark_context = result.get("benchmark_context", {}) or {}
    if benchmark_context:
        with st.expander("市场上下文"):
            st.json(benchmark_context)
    with st.expander("后台摘要 JSON"):
        st.json(
            {
                "request_id": result.get("request_id"),
                "job_kind": result.get("job_kind"),
                "metrics": metrics,
            }
        )


content_col = show_right_nav()
with content_col:
    st.title("🔬 Wyckoff Funnel")
    st.markdown("后台版 4 层漏斗：前台只负责提交任务、看状态、读结果。")
    st.warning("网页端不再本地执行全量漏斗。重计算已经迁到 GitHub Actions，这样能明显降低 Streamlit 内存和超时风险。")

    with st.sidebar:
        st.subheader("漏斗参数")
        min_cap = st.number_input("最小市值(亿)", min_value=5.0, max_value=100.0, value=35.0, step=5.0, format="%.0f")
        min_amt = st.number_input(
            "近20日均成交额阈值(万)", min_value=1000.0, max_value=20000.0, value=5000.0, step=1000.0, format="%.0f"
        )
        ma_short = st.number_input("短期均线", min_value=10, max_value=100, value=50, step=10)
        ma_long = st.number_input("长期均线", min_value=100, max_value=500, value=200, step=50)
        ma_hold = st.number_input("守线均线", min_value=5, max_value=60, value=20, step=5)
        top_n = st.number_input("Top-N 行业", min_value=1, max_value=10, value=3, step=1)
        spring_support_w = st.number_input("Spring 支撑窗口", min_value=20, max_value=120, value=60, step=10)
        lps_vol_dry = st.number_input("LPS 缩量比", min_value=0.1, max_value=0.8, value=0.35, step=0.05, format="%.2f")
        evr_vol_ratio = st.number_input(
            "EvR 量比阈值", min_value=1.0, max_value=5.0, value=2.0, step=0.5, format="%.1f"
        )
        trading_days = st.number_input("交易日数量", min_value=200, max_value=500, value=320, step=50)
        max_workers = int(st.number_input("后台并发拉取数", min_value=1, max_value=16, value=8, step=1))

    st.subheader("股票池")
    pool_mode = st.radio("来源", options=["板块", "手动输入"], horizontal=True)
    board = "all"
    manual_symbols = ""
    if pool_mode == "手动输入":
        manual_symbols = st.text_area("股票代码", placeholder="例如: 600519, 000001", height=120)
    else:
        board = st.selectbox(
            "选择板块",
            options=["all", "main", "chinext"],
            format_func=lambda v: {"all": "全部主板+创业板", "main": "主板", "chinext": "创业板"}.get(v, v),
        )

    run_btn = st.button("提交后台漏斗筛选", type="primary")
    refresh_btn = st.button("刷新后台状态")

    if run_btn:
        ready, msg = background_jobs_ready_for_current_user()
        if not ready:
            st.error(msg)
            st.stop()
        payload = {
            "pool_mode": "manual" if pool_mode == "手动输入" else "board",
            "board": board,
            "manual_symbols": _parse_symbols(manual_symbols),
            "trading_days": int(trading_days),
            "max_workers": int(max_workers),
            "min_market_cap_yi": float(min_cap),
            "min_avg_amount_wan": float(min_amt),
            "ma_short": int(ma_short),
            "ma_long": int(ma_long),
            "ma_hold": int(ma_hold),
            "top_n_sectors": int(top_n),
            "spring_support_window": int(spring_support_w),
            "lps_vol_dry_ratio": float(lps_vol_dry),
            "evr_vol_ratio": float(evr_vol_ratio),
        }
        request_id = submit_background_job("funnel_screen", payload, state_key=STATE_KEY)
        st.success(f"后台任务已提交：`{request_id}`")

    state = sync_background_job_state(state_key=STATE_KEY)
    active_result = _render_job_status(state)

    if refresh_btn:
        refresh_background_job_data()
        st.rerun()

    if not active_result:
        latest_run, latest_result = load_latest_job_result("funnel_screen")
        if latest_result:
            st.divider()
            st.caption(
                "以下展示当前账号最近一次成功的后台漏斗结果。"
                + (f" Run #{latest_run.run_number}" if latest_run else "")
            )
            active_result = latest_result

    if active_result:
        _render_funnel_result(active_result)

    st.divider()
    st.page_link(
        "pages/AIAnalysis.py",
        label="前往 AI 分析",
        icon="🤖",
    )
