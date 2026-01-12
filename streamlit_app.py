import streamlit as st
import pandas as pd
from datetime import date, timedelta
import akshare as ak
import zipfile
import io
from fetch_a_share_csv import (
    _resolve_trading_window,
    _stock_name_from_code,
    _fetch_hist,
    _stock_sector_em,
    _build_export,
    get_all_stocks,
    TradingWindow
)

# Page configuration
st.set_page_config(
    page_title="A股历史行情导出工具",
    page_icon="📈",
    layout="wide"
)

# Initialize session state for search history
if "search_history" not in st.session_state:
    st.session_state.search_history = []
if "current_symbol" not in st.session_state:
    st.session_state.current_symbol = "300364"
if "should_run" not in st.session_state:
    st.session_state.should_run = False

@st.cache_data(ttl=3600)  # Cache for 1 hour
def load_stock_list():
    return get_all_stocks()

def add_to_history(symbol, name):
    item = {"symbol": symbol, "name": name}
    # Remove if exists to move to top
    st.session_state.search_history = [x for x in st.session_state.search_history if x["symbol"] != symbol]
    st.session_state.search_history.insert(0, item)
    # Keep only last 10
    st.session_state.search_history = st.session_state.search_history[:10]

def set_symbol_from_history(symbol):
    st.session_state.current_symbol = symbol
    st.session_state.should_run = True

st.title("📈 A股历史行情导出工具")
st.markdown("基于 **akshare**，支持导出 **威科夫分析** 所需的增强版 CSV（包含量价、换手率、振幅、均价、板块等）。")
st.markdown("💡 灵感来自 **秋生trader @Hoyooyoo**，祝各位在祖国的大A里找到价值！")

def show_right_nav():
    """Injects a floating navigation bar on the right side with collapse/expand support"""
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
    
    /* Collapsed state: hidden and moved right */
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
    
    /* Icon rotation/switching */
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
    
    /* Tooltip text */
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

# Load stocks
all_stocks = load_stock_list()
# Format as "code name" for display
stock_options = [f"{s['code']} {s['name']}" for s in all_stocks] if all_stocks else []

# Sidebar for inputs
with st.sidebar:
    st.header("参数配置")
    
    # Smart search box
    # Try to find index of current symbol
    default_index = 0
    if st.session_state.current_symbol and stock_options:
        for i, opt in enumerate(stock_options):
            if opt.startswith(st.session_state.current_symbol):
                default_index = i
                break
    
    selected_stock = st.selectbox(
        "选择股票 (支持代码或名称搜索)",
        options=stock_options,
        index=default_index,
        help="输入代码（如 300364）或名称（如 中文在线）进行搜索",
        key="stock_selector"
    )
    
    # Extract code from selection
    if selected_stock:
        current_code = selected_stock.split(" ")[0]
        current_name_from_select = selected_stock.split(" ")[1] if len(selected_stock.split(" ")) > 1 else ""
        # Update session state if changed via selectbox
        if current_code != st.session_state.current_symbol:
            st.session_state.current_symbol = current_code
    else:
        # Fallback if list is empty (e.g. network error)
        symbol_input = st.text_input(
            "股票代码 (必填)",
            value=st.session_state.current_symbol,
            help="请输入 6 位股票代码，例如 300364",
            key="symbol_input_widget"
        )
        if symbol_input != st.session_state.current_symbol:
            st.session_state.current_symbol = symbol_input
        current_name_from_select = ""

    
    symbol_name_input = st.text_input(
        "股票名称 (选填)",
        value=current_name_from_select,
        help="仅用于展示或文件名，留空则自动从 akshare 获取"
    )
    
    trading_days = st.number_input(
        "回溯交易日数量",
        min_value=1,
        max_value=5000,
        value=500,
        step=50,
        help="从结束日期向前回溯的交易日天数"
    )
    
    end_offset = st.number_input(
        "结束日期偏移 (天)",
        min_value=0,
        value=1,
        help="0 表示今天，1 表示昨天。系统会自动对齐到最近的交易日。"
    )
    
    adjust = st.selectbox(
        "复权类型",
        options=["", "qfq", "hfq"],
        format_func=lambda x: "不复权" if x == "" else ("前复权" if x == "qfq" else "后复权"),
        index=0,
        help=(
            "不复权：原始行情；\n"
            "前复权(qfq)：把历史价格按当前口径调整，除权后走势连续，适合看长期趋势；\n"
            "后复权(hfq)：把当前价格按历史口径调整，便于对比历史绝对价位。"
        )
    )

    st.caption(
        "复权用于处理分红送转等导致的价格跳变：前复权更常用于看趋势；后复权更常用于还原历史价位对比。"
    )

    run_btn = st.button("🚀 开始获取数据", type="primary")

    if st.session_state.search_history:
        st.markdown("---")
        st.header("🕒 搜索历史")
        for item in st.session_state.search_history:
            label = f"{item['symbol']} {item['name']}"
            if st.button(label, key=f"hist_{item['symbol']}", use_container_width=True):
                set_symbol_from_history(item['symbol'])
                st.rerun()

# Main content
if run_btn or st.session_state.should_run:
    # Reset trigger
    if st.session_state.should_run:
        st.session_state.should_run = False
        
    if not st.session_state.current_symbol or not st.session_state.current_symbol.isdigit() or len(st.session_state.current_symbol) != 6:
        st.error("请输入有效的 6 位数字股票代码！")
    else:
        try:
            with st.spinner(f"正在获取 {st.session_state.current_symbol} 的数据..."):
                # 1. Resolve trading window
                end_calendar = date.today() - timedelta(days=int(end_offset))
                window = _resolve_trading_window(end_calendar, int(trading_days))
                
                # 2. Get name if not provided
                if not symbol_name_input:
                    try:
                        name = _stock_name_from_code(st.session_state.current_symbol)
                    except Exception as e:
                        st.warning(f"无法自动获取名称: {e}")
                        name = "Unknown"
                else:
                    name = symbol_name_input
                
                # Add to history
                add_to_history(st.session_state.current_symbol, name)
                
                st.info(f"股票: **{st.session_state.current_symbol} {name}** | 时间窗口: **{window.start_trade_date}** 至 **{window.end_trade_date}** ({trading_days} 个交易日)")

                # 3. Fetch data
                df_hist = _fetch_hist(st.session_state.current_symbol, window, adjust)
                
                # 4. Get sector info
                sector = _stock_sector_em(st.session_state.current_symbol)
                
                # 5. Build export dataframe
                df_export = _build_export(df_hist, sector)
                
                # Display data with Tabs
                st.subheader("📊 数据预览")
                tab1, tab2 = st.tabs(["📈 OHLCV (增强版)", "📄 原始数据 (Hist Data)"])
                
                with tab1:
                    st.dataframe(df_export, use_container_width=True)
                
                with tab2:
                    st.dataframe(df_hist, use_container_width=True)
                
                # Prepare files
                csv_export = df_export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                file_name_export = f"{st.session_state.current_symbol}_{name}_ohlcv.csv"
                
                csv_hist = df_hist.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                file_name_hist = f"{st.session_state.current_symbol}_{name}_hist_data.csv"

                # Create ZIP for "Download All"
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(file_name_export, csv_export)
                    zf.writestr(file_name_hist, csv_hist)
                zip_data = zip_buffer.getvalue()
                file_name_zip = f"{st.session_state.current_symbol}_{name}_all.zip"

                # Download buttons
                st.markdown("### 📥 下载数据")
                col1, col2, col3 = st.columns(3)

                with col1:
                    st.download_button(
                        label="下载 OHLCV (增强版)",
                        data=csv_export,
                        file_name=file_name_export,
                        mime="text/csv",
                        type="primary",
                        use_container_width=True
                    )
                
                with col2:
                    st.download_button(
                        label="下载原始数据 (Hist Data)",
                        data=csv_hist,
                        file_name=file_name_hist,
                        mime="text/csv",
                        use_container_width=True
                    )

                with col3:
                    st.download_button(
                        label="📦 全部下载 (.zip)",
                        data=zip_data,
                        file_name=file_name_zip,
                        mime="application/zip",
                        type="primary",
                        use_container_width=True
                    )
                    
        except Exception as e:
            st.error(f"发生错误: {str(e)}")
            st.exception(e)

else:
    st.info("👈 请在左侧输入参数并点击“开始获取数据”")
