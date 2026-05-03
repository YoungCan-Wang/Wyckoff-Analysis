import os
import time
from datetime import date, timedelta

import akshare as ak
import pandas as pd
import streamlit as st

from app.layout import is_data_source_failure_message, setup_page, show_user_error
from app.navigation import show_right_nav
from app.ui_helpers import show_page_loading
from core.export_artifacts import cleanup_export_artifacts, file_loader, write_dataframe_csv
from integrations.fetch_a_share_csv import get_all_stocks
from integrations.stock_hist_repository import get_stock_hist

setup_page(page_title="自定义导出", page_icon="🧰")


content_col = show_right_nav()
with content_col:
    PREVIEW_ROWS = 300
    EXPORT_CLEANUP_INTERVAL_SECONDS = 3 * 60 * 60

    now_ts = time.time()
    last_cleanup_ts = float(st.session_state.get("custom_export_cleanup_last_ts", 0))
    if now_ts - last_cleanup_ts >= EXPORT_CLEANUP_INTERVAL_SECONDS:
        cleanup_export_artifacts()
        st.session_state.custom_export_cleanup_last_ts = now_ts

    # 首次进入页面时复用现有 Thinking 组件（与页面其他操作保持一致）
    if not st.session_state.get("_custom_export_entered", False):
        loading = show_page_loading(title="思考中...", subtitle="正在准备页面内容")
        time.sleep(0.2)
        loading.empty()
        st.session_state["_custom_export_entered"] = True

    st.title("🧰 自定义导出")
    st.markdown("选择一个数据源，配置参数后获取数据，再按需选择字段导出。")

    SOURCES = [
        {
            "id": "stock_zh_a_hist",
            "label": "A股个股历史（日线）",
            "fn": get_stock_hist,
            "has_adjust": True,
            "help": "返回日频 K 线数据；symbol 为 6 位股票代码（支持 akshare/baostock/efinance 自动降级）。",
            "default_symbol": "300364",
        },
        {
            "id": "index_zh_a_hist",
            "label": "指数历史（日线）",
            "fn": ak.index_zh_a_hist,
            "has_adjust": False,
            "help": "返回指数日线；支持上证、深证、创业板、北证等常用指数。",
            "default_symbol": "",
        },
        {
            "id": "fund_etf_hist_em",
            "label": "ETF 历史（日线）",
            "fn": ak.fund_etf_hist_em,
            "has_adjust": True,
            "help": "返回 ETF 日线；symbol 为 ETF 代码（例如 510300 / 159707）。",
            "default_symbol": "159707",
        },
        {
            "id": "macro_china_cpi_monthly",
            "label": "宏观：CPI（月度）",
            "fn": ak.macro_china_cpi_monthly,
            "has_adjust": False,
            "help": "返回月度 CPI 指标，无需输入代码与日期。",
            "default_symbol": "",
        },
    ]

    source_labels = {s["label"]: s for s in SOURCES}

    source_select_key = "custom_export::selected_label"
    prev_selected_label = st.session_state.get(source_select_key, "")
    selected_label = st.selectbox("数据源", options=[s["label"] for s in SOURCES], key=source_select_key)
    source = source_labels[selected_label]
    st.caption(source["help"])

    if prev_selected_label and prev_selected_label != selected_label:
        st.session_state.custom_export_payload = None
        st.session_state.custom_export_source_id = ""
        st.session_state.custom_export_selected_signature = ""
        st.session_state.custom_export_selected_path = ""

    today = date.today()

    symbol = ""
    adjust = ""
    end_date = today
    start_date = end_date - timedelta(days=365)

    @st.cache_data(ttl=3600, show_spinner=False, max_entries=1)
    def _stock_name_map() -> dict[str, str]:
        items = get_all_stocks()
        return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}

    @st.cache_data(ttl=300, show_spinner=False, max_entries=1)
    def _etf_name_map() -> dict[str, str]:
        try:
            df = ak.fund_etf_spot_em()
            return {str(c): str(n) for c, n in zip(df["代码"], df["名称"])}
        except Exception:
            return {}

    INDEX_CHOICES = [
        {"label": "上证指数", "code": "000001"},
        {"label": "深证成指", "code": "399001"},
        {"label": "创业板指", "code": "399006"},
        {"label": "北证50", "code": "899050"},
    ]

    if source["id"] != "macro_china_cpi_monthly":
        col_a, col_b = st.columns(2)
        if source["id"] == "index_zh_a_hist":
            idx_labels = [x["label"] for x in INDEX_CHOICES]
            sel = st.selectbox("指数", options=idx_labels)
            sel_code = next((x["code"] for x in INDEX_CHOICES if x["label"] == sel), "")
            symbol = sel_code
            st.info(f"指数：{sel}（{symbol}）")
        else:
            symbol = st.text_input("代码", value=source.get("default_symbol", "")).strip()
            if source["id"] == "stock_zh_a_hist":
                name = _stock_name_map().get(symbol, "")
                if name:
                    st.info(f"股票：{name}（{symbol}）")
            elif source["id"] == "fund_etf_hist_em":
                etf_name = _etf_name_map().get(symbol, "")
                if etf_name:
                    st.info(f"ETF：{etf_name}（{symbol}）")

        end_key = f"custom_export::{source['id']}::end_date"
        start_key = f"custom_export::{source['id']}::start_date"
        prev_end_key = f"custom_export::{source['id']}::prev_end_date"

        if end_key not in st.session_state:
            st.session_state[end_key] = today

        with col_b:
            end_date = st.date_input("结束日期", key=end_key)

        desired_start = end_date - timedelta(days=365)
        if start_key not in st.session_state:
            st.session_state[start_key] = desired_start
        else:
            prev_end = st.session_state.get(prev_end_key, end_date)
            prev_desired_start = prev_end - timedelta(days=365)
            if end_date != prev_end and st.session_state[start_key] == prev_desired_start:
                st.session_state[start_key] = desired_start
        st.session_state[prev_end_key] = end_date

        with col_a:
            start_date = st.date_input("开始日期", key=start_key)

        if source["has_adjust"]:
            adjust = st.selectbox(
                "复权类型",
                options=["", "qfq", "hfq"],
                format_func=lambda x: "不复权" if x == "" else ("前复权" if x == "qfq" else "后复权"),
                index=0,
            )

    run = st.button("🚀 获取数据", type="primary")

    if run:
        try:
            loading = show_page_loading(title="思考中...", subtitle="正在获取数据")
            try:
                if source["id"] == "macro_china_cpi_monthly":
                    df = source["fn"]()
                else:
                    if start_date > end_date:
                        st.error("开始日期不能晚于结束日期。")
                        st.stop()
                    sd = start_date.strftime("%Y%m%d")
                    ed = end_date.strftime("%Y%m%d")
                    if source["id"] == "stock_zh_a_hist":
                        df = source["fn"](
                            symbol=symbol,
                            start_date=start_date,
                            end_date=end_date,
                            adjust=adjust,
                            context="web",
                        )
                    elif source["id"] == "index_zh_a_hist":
                        df = source["fn"](symbol=symbol, period="daily", start_date=sd, end_date=ed)
                    else:
                        df = source["fn"](
                            symbol=symbol,
                            period="daily",
                            start_date=sd,
                            end_date=ed,
                            adjust=adjust,
                        )
            finally:
                loading.empty()
            csv_path = write_dataframe_csv(
                df,
                prefix=f"{source['id']}_{symbol or 'dataset'}_all",
            )
            preview_df = df.head(PREVIEW_ROWS).copy()
            st.session_state.custom_export_payload = {
                "csv_path": str(csv_path),
                "shape": [int(len(df)), int(len(df.columns))],
                "columns": [str(c) for c in df.columns],
                "preview_rows": preview_df.to_dict(orient="records"),
                "preview_count": int(len(preview_df)),
                "symbol": symbol,
            }
            st.session_state.custom_export_source_id = source["id"]
            st.session_state.custom_export_selected_signature = ""
            st.session_state.custom_export_selected_path = ""

        except Exception as e:
            msg = str(e)
            if is_data_source_failure_message(msg):
                show_user_error(msg, None)
            else:
                show_user_error("获取失败，请稍后重试。", e)
            st.stop()

    payload = st.session_state.get("custom_export_payload")
    if payload is None:
        st.info("请选择数据源并点击“获取数据”。")
        st.stop()

    total_rows = int((payload.get("shape") or [0, 0])[0])
    total_cols = int((payload.get("shape") or [0, 0])[1])
    preview_rows = payload.get("preview_rows") or []
    preview_df = None if not preview_rows else pd.DataFrame(preview_rows, columns=payload.get("columns") or None)
    all_columns = [str(c) for c in (payload.get("columns") or [])]
    csv_path = str(payload.get("csv_path") or "")
    if not csv_path or not os.path.exists(csv_path):
        st.session_state.custom_export_payload = None
        st.warning("导出缓存已失效，请重新获取数据。")
        st.stop()

    st.subheader("📊 数据预览")
    st.caption(
        f"行数：{total_rows} | 列数：{total_cols} | 当前仅预览前 {min(total_rows, PREVIEW_ROWS)} 行，完整数据用于导出。"
    )
    if preview_df is not None:
        st.dataframe(preview_df, width="stretch", height=420)

    st.subheader("✅ 可选内容")
    filter_text = st.text_input("字段筛选", value="", placeholder="输入字段名关键词过滤")

    columns = [c for c in all_columns if filter_text.strip() in str(c)]
    source_key = st.session_state.custom_export_source_id or source["id"]
    state_key_prefix = f"custom_export_cols::{source_key}::"

    selected_cols: list[str] = []
    for c in columns:
        key = state_key_prefix + str(c)
        if key not in st.session_state:
            st.session_state[key] = True
        if st.session_state[key]:
            selected_cols.append(c)

    all_selected = len(columns) > 0 and len(selected_cols) == len(columns)
    toggle_all = st.checkbox("全选", value=all_selected, key=state_key_prefix + "__all__")
    if toggle_all != all_selected:
        for c in columns:
            st.session_state[state_key_prefix + str(c)] = toggle_all
        st.rerun()

    cols = st.columns(4)
    for i, c in enumerate(columns):
        with cols[i % 4]:
            st.checkbox(str(c), key=state_key_prefix + str(c))

    selected_cols = [c for c in columns if st.session_state.get(state_key_prefix + str(c), False)]
    if not selected_cols:
        st.warning("请至少选择 1 个字段。")
        st.stop()

    selected_signature = "|".join(selected_cols)
    selected_path = st.session_state.get("custom_export_selected_path") or ""
    if (
        selected_signature != st.session_state.get("custom_export_selected_signature")
        or not selected_path
        or not os.path.exists(selected_path)
    ):
        selected_df = pd.read_csv(csv_path, usecols=selected_cols)
        selected_path = str(
            write_dataframe_csv(
                selected_df,
                prefix=f"{source_key}_{payload.get('symbol') or 'dataset'}_selected",
            )
        )
        st.session_state.custom_export_selected_signature = selected_signature
        st.session_state.custom_export_selected_path = selected_path

    file_prefix = source_key
    if source["id"] != "macro_china_cpi_monthly":
        file_prefix = f"{source_key}_{payload.get('symbol') or symbol}"

    st.markdown("### 📥 导出")
    selected_bytes = file_loader(selected_path)
    all_bytes = file_loader(csv_path)
    st.download_button(
        label="下载所选字段 CSV",
        data=selected_bytes,
        file_name=f"{file_prefix}_selected.csv",
        mime="text/csv",
        type="primary",
        width="stretch",
    )
    st.download_button(
        label="下载全部字段 CSV",
        data=all_bytes,
        file_name=f"{file_prefix}_all.csv",
        mime="text/csv",
        width="stretch",
    )
