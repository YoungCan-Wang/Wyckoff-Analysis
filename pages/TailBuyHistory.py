"""尾盘买入记录页面。"""

import pandas as pd
import streamlit as st

from app.layout import setup_page
from app.navigation import show_right_nav
from app.ui_helpers import show_page_loading
from integrations.supabase_tail_buy import load_tail_buy_from_supabase

setup_page(page_title="尾盘记录", page_icon="🌙")

content_col = show_right_nav()

with content_col:
    st.title("🌙 尾盘买入记录")
    st.markdown("每日尾盘策略（14:00 执行）对信号池候选做分时评估后的 BUY 决策记录。")

    loading = show_page_loading(title="加载中...", subtitle="从 Supabase 读取尾盘记录")
    try:
        raw_data = load_tail_buy_from_supabase(limit=200)
    finally:
        loading.empty()

    if not raw_data:
        st.info("暂无尾盘买入记录，等待下一次尾盘策略执行后刷新。")
        st.stop()

    df = pd.DataFrame(raw_data)

    display_cols = [
        "code",
        "name",
        "run_date",
        "signal_type",
        "rule_score",
        "priority_score",
        "llm_decision",
        "llm_reason",
    ]
    display_cols = [c for c in display_cols if c in df.columns]
    df = df[display_cols]

    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.zfill(6)

    for col in ("rule_score", "priority_score"):
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f"{float(x or 0):.1f}")

    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"共 {len(df)} 条记录")
