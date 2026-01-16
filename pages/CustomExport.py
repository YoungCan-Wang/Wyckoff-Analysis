import streamlit as st
from datetime import date, timedelta
import akshare as ak
from download_history import add_download_history


st.set_page_config(
    page_title="自定义导出",
    page_icon="🧰",
    layout="wide",
)


st.title("🧰 自定义导出")
st.markdown("选择一个数据源，配置参数后获取数据，再按需选择字段导出。")


def show_right_nav():
    style = """
    <style>
    .nav-wrapper {
        position: fixed;
        right: 20px;
        top: 50%;
        transform: translateY(-50%);
        z-index: 99999;
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 8px;
    }

    .nav-toggle-checkbox {
        display: none;
    }

    .nav-content {
        background-color: var(--secondary-background-color);
        padding: 12px 8px;
        border-radius: 16px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        display: flex;
        flex-direction: column;
        gap: 16px;
        border: 1px solid rgba(128, 128, 128, 0.2);
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        transform-origin: right center;
        opacity: 1;
        transform: translateX(0);
    }
    
    .nav-toggle-checkbox:not(:checked) ~ .nav-content {
        opacity: 0;
        transform: translateX(100px);
        pointer-events: none;
        height: 0;
        padding: 0;
        margin: 0;
        overflow: hidden;
    }

    .nav-toggle-btn {
        width: 24px;
        height: 24px;
        background-color: var(--secondary-background-color);
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        border: 1px solid rgba(128, 128, 128, 0.2);
        transition: all 0.3s ease;
        color: var(--text-color);
        font-size: 12px;
        user-select: none;
    }

    .nav-toggle-btn:hover {
        background-color: #FF4B4B;
        color: white;
        border-color: #FF4B4B;
    }
    
    .nav-toggle-checkbox:checked ~ .nav-toggle-btn .icon-collapse {
        display: inline-block;
    }
    .nav-toggle-checkbox:checked ~ .nav-toggle-btn .icon-expand {
        display: none;
    }
    
    .nav-toggle-checkbox:not(:checked) ~ .nav-toggle-btn .icon-collapse {
        display: none;
    }
    .nav-toggle-checkbox:not(:checked) ~ .nav-toggle-btn .icon-expand {
        display: inline-block;
    }
    
    .nav-item {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 12px;
        background-color: var(--background-color);
        color: var(--text-color);
        text-decoration: none;
        transition: all 0.2s ease;
        font-size: 20px;
        border: 1px solid transparent;
    }
    
    .nav-item:hover {
        transform: scale(1.1);
        background-color: #FF4B4B;
        color: white;
        border-color: #FF4B4B;
        text-decoration: none;
    }
    
    .nav-item::after {
        content: attr(data-title);
        position: absolute;
        right: 60px;
        background: #333;
        color: white;
        padding: 4px 8px;
        border-radius: 4px;
        font-size: 12px;
        opacity: 0;
        visibility: hidden;
        transition: opacity 0.2s;
        white-space: nowrap;
        pointer-events: none;
    }
    
    .nav-item:hover::after {
        opacity: 1;
        visibility: visible;
    }
    </style>
    """

    content = """
    <div class="nav-wrapper">
        <input type="checkbox" id="nav-toggle" class="nav-toggle-checkbox" checked>
        
        <label for="nav-toggle" class="nav-toggle-btn" title="Toggle Navigation">
            <span class="icon-collapse">▶</span>
            <span class="icon-expand">◀</span>
        </label>
        
        <div class="nav-content">
            <a href="/" target="_self" class="nav-item" data-title="首页 Home">
                <span>🏠</span>
            </a>
            <a href="/CustomExport" target="_self" class="nav-item" data-title="自定义导出 Custom Export">
                <span>🧰</span>
            </a>
            <a href="/DownloadHistory" target="_self" class="nav-item" data-title="下载历史 Download History">
                <span>🕘</span>
            </a>
            <a href="/Changelog" target="_self" class="nav-item" data-title="更新日志 Changelog">
                <span>📢</span>
            </a>
            <a href="https://github.com/YoungCan-Wang/Wyckoff-Analysis" target="_blank" class="nav-item" data-title="辛苦各位点个star，欢迎提各种issue">
                <span>⭐</span>
            </a>
        </div>
    </div>
    """

    st.html(style + content)


show_right_nav()


SOURCES = [
    {
        "id": "stock_zh_a_hist",
        "label": "A股个股历史（日线，东方财富）",
        "fn": ak.stock_zh_a_hist,
        "has_adjust": True,
        "help": "返回日频 K 线数据；symbol 为 6 位股票代码。",
        "default_symbol": "300364",
    },
    {
        "id": "index_zh_a_hist",
        "label": "指数历史（日线，东方财富）",
        "fn": ak.index_zh_a_hist,
        "has_adjust": False,
        "help": "返回指数日线；symbol 为指数代码（例如 000001）。",
        "default_symbol": "000001",
    },
    {
        "id": "fund_etf_hist_em",
        "label": "ETF 历史（日线，东方财富）",
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

if source["id"] != "macro_china_cpi_monthly":
    symbol = st.text_input("代码", value=source.get("default_symbol", "")).strip()
    col_a, col_b = st.columns(2)
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
    except Exception as e:
        st.error(f"获取失败：{e}")
        st.stop()


df = st.session_state.custom_export_df
if df is None:
    st.info("请选择数据源并点击“获取数据”。")
    st.stop()


st.subheader("📊 数据预览")
st.caption(f"行数：{len(df)} | 列数：{len(df.columns)}")
st.dataframe(df, use_container_width=True, height=420)


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
clicked_selected = st.download_button(
    label="下载所选字段 CSV",
    data=csv_selected,
    file_name=f"{file_prefix}_selected.csv",
    mime="text/csv",
    type="primary",
    use_container_width=True,
)
clicked_all = st.download_button(
    label="下载全部字段 CSV",
    data=csv_all,
    file_name=f"{file_prefix}_all.csv",
    mime="text/csv",
    use_container_width=True,
)
if clicked_selected:
    add_download_history(
        page="CustomExport",
        source=source_key,
        title="所选字段 CSV",
        file_name=f"{file_prefix}_selected.csv",
        mime="text/csv",
        data=csv_selected,
    )
if clicked_all:
    add_download_history(
        page="CustomExport",
        source=source_key,
        title="全部字段 CSV",
        file_name=f"{file_prefix}_all.csv",
        mime="text/csv",
        data=csv_all,
    )
