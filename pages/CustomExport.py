import sys
import os

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from datetime import date, timedelta
import akshare as ak
from download_history import add_download_history
from fetch_a_share_csv import get_all_stocks
from navigation import show_right_nav


st.set_page_config(
    page_title="自定义导出",
    page_icon="🧰",
    layout="wide",
)


st.title("🧰 自定义导出")
st.markdown("选择一个数据源，配置参数后获取数据，再按需选择字段导出。")

show_right_nav()

SOURCES = [
    {
        "id": "stock_zh_a_hist",
        "label": "A股个股历史（日线）",
        "fn": ak.stock_zh_a_hist,
        "has_adjust": True,
        "help": "返回日频 K 线数据；symbol 为 6 位股票代码。",
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
    st.session_state.custom_export_df = None
    st.session_state.custom_export_source_id = ""


today = date.today()

symbol = ""
adjust = ""
end_date = today
start_date = end_date - timedelta(days=365)

@st.cache_data(ttl=3600, show_spinner=False)
def _stock_name_map() -> dict[str, str]:
    items = get_all_stocks()
    return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}

@st.cache_data(ttl=300, show_spinner=False)
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

if "custom_export_df" not in st.session_state:
    st.session_state.custom_export_df = None
if "custom_export_source_id" not in st.session_state:
    st.session_state.custom_export_source_id = ""

if run:
    try:
        with st.spinner("正在获取数据..."):
            if source["id"] == "macro_china_cpi_monthly":
                df = source["fn"]()
            else:
                if start_date > end_date:
                    st.error("开始日期不能晚于结束日期。")
                    st.stop()
                sd = start_date.strftime("%Y%m%d")
                ed = end_date.strftime("%Y%m%d")
                if source["id"] == "index_zh_a_hist":
                    df = source["fn"](symbol=symbol, period="daily", start_date=sd, end_date=ed)
                else:
                    df = source["fn"](symbol=symbol, period="daily", start_date=sd, end_date=ed, adjust=adjust)
        st.session_state.custom_export_df = df
        st.session_state.custom_export_source_id = source["id"]

        # === 自动记录查询历史 ===
        # 生成一个唯一的 query_key 来防止重复记录
        current_query_key = f"{source['id']}_{symbol}_{start_date}_{end_date}"
        last_query_key = st.session_state.get("last_custom_export_query")
        
        if current_query_key != last_query_key:
            add_download_history(
                page="CustomExport",
                source=source["id"],
                title=f"{symbol} ({start_date}~{end_date})",
                file_name=f"{symbol}_{start_date}_{end_date}.csv",
                mime="text/csv",
                data=None
            )
            st.session_state["last_custom_export_query"] = current_query_key

    except Exception as e:
        st.error(f"获取失败：{e}")
        st.stop()


df = st.session_state.custom_export_df
if df is None:
    st.info("请选择数据源并点击“获取数据”。")
    st.stop()


st.subheader("📊 数据预览")
st.caption(f"行数：{len(df)} | 列数：{len(df.columns)}")
st.dataframe(df, width="stretch", height=420)


st.subheader("✅ 可选内容")
filter_text = st.text_input("字段筛选", value="", placeholder="输入字段名关键词过滤")

columns = [c for c in df.columns if filter_text.strip() in str(c)]
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

csv_selected = df[selected_cols].to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
csv_all = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")

file_prefix = source_key
if source["id"] != "macro_china_cpi_monthly":
    file_prefix = f"{source_key}_{symbol}"

st.markdown("### 📥 导出")
st.download_button(
    label="下载所选字段 CSV",
    data=csv_selected,
    file_name=f"{file_prefix}_selected.csv",
    mime="text/csv",
    type="primary",
    width="stretch",
)
st.download_button(
    label="下载全部字段 CSV",
    data=csv_all,
    file_name=f"{file_prefix}_all.csv",
    mime="text/csv",
    width="stretch",
)
