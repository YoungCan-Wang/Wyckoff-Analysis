import streamlit as st
import pandas as pd
from datetime import date, timedelta
import akshare as ak
from fetch_a_share_csv import (
    _resolve_trading_window,
    _stock_name_from_code,
    _fetch_hist,
    _stock_sector_em,
    _build_export,
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

# Sidebar for inputs
with st.sidebar:
    st.header("参数配置")
    
    symbol_input = st.text_input(
        "股票代码 (必填)",
        value=st.session_state.current_symbol,
        help="请输入 6 位股票代码，例如 300364",
        key="symbol_input_widget"
    )
    
    symbol_name_input = st.text_input(
        "股票名称 (选填)",
        value="",
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
        index=0
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

# Sync widget value back to session state if changed manually
if symbol_input != st.session_state.current_symbol:
    st.session_state.current_symbol = symbol_input

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
                
                # Display data
                st.subheader("📊 数据预览")
                st.dataframe(df_export, use_container_width=True)
                
                # Download buttons
                col1, col2 = st.columns(2)
                
                csv_export = df_export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                file_name_export = f"{st.session_state.current_symbol}_{name}_ohlcv.csv"
                
                csv_hist = df_hist.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                file_name_hist = f"{st.session_state.current_symbol}_{name}_hist_data.csv"

                with col1:
                    st.download_button(
                        label="📥 下载 OHLCV (增强版)",
                        data=csv_export,
                        file_name=file_name_export,
                        mime="text/csv",
                        type="primary"
                    )
                
                with col2:
                    st.download_button(
                        label="📥 下载原始数据 (Hist Data)",
                        data=csv_hist,
                        file_name=file_name_hist,
                        mime="text/csv"
                    )
                    
        except Exception as e:
            st.error(f"发生错误: {str(e)}")
            st.exception(e)

