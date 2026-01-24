import streamlit as st
from datetime import date, timedelta, datetime
import zipfile
import io
import re
import requests
import os
import random
from dotenv import load_dotenv
import akshare as ak
from fetch_a_share_csv import (
    _resolve_trading_window,
    _fetch_hist,
    _build_export,
    get_all_stocks,
    get_stocks_by_board,
    _normalize_symbols,
)
from download_history import add_download_history
from auth_component import check_auth, login_form, logout
from navigation import show_right_nav

# Load environment variables from .env file
load_dotenv()

# Page configuration
st.set_page_config(
    page_title="A股历史行情导出工具",
    page_icon="📈",
    layout="wide"
)

# === Auth Check ===
if not check_auth():
    # 使用空布局，避免显示侧边栏和其他干扰元素
    empty_container = st.empty()
    with empty_container.container():
        login_form()
    st.stop()

# === Logged In User Info ===
with st.sidebar:
    if st.session_state.get("user"):
        st.caption(f"当前用户: {st.session_state.user.email}")
        if st.button("退出登录"):
            logout()
    st.divider()

# Initialize session state for search history
if "search_history" not in st.session_state:
    st.session_state.search_history = []
if "current_symbol" not in st.session_state:
    st.session_state.current_symbol = "300364"
if "should_run" not in st.session_state:
    st.session_state.should_run = False
if "feishu_webhook" not in st.session_state:
    st.session_state.feishu_webhook = os.getenv("FEISHU_WEBHOOK_URL", "")

# 如果是从 .env 自动加载的，确保是空字符串而不是None
if st.session_state.feishu_webhook is None:
    st.session_state.feishu_webhook = ""

if "mobile_mode" not in st.session_state:
    st.session_state.mobile_mode = False

@st.cache_data(ttl=3600, show_spinner=False)
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

def _safe_filename_part(value: str) -> str:
    s = str(value).strip()
    if not s:
        return "Unknown"
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _parse_batch_symbols(text: str) -> list[str]:
    parts = re.split(r"[;；\s,，\n]+", str(text or ""))
    candidates: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        candidates.extend(re.findall(r"\d{6}", part))
    return _normalize_symbols(candidates)

@st.cache_data(ttl=3600, show_spinner=False)
def _stock_name_map():
    stocks = load_stock_list()
    return {s.get("code"): s.get("name") for s in stocks if s.get("code")}

def _stock_sector_em_timeout(symbol: str, timeout: float):
    try:
        df = ak.stock_individual_info_em(symbol=symbol, timeout=timeout)
        if df is None or df.empty:
            return ""
        row = df.loc[df["item"] == "行业", "value"]
        if row.empty:
            return ""
        return str(row.iloc[0]).strip()
    except Exception:
        return ""

def send_feishu_notification(webhook_url: str, title: str, content: str):
    """发送飞书卡片消息"""
    if not webhook_url:
        return False
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": content
                    }
                }
            ]
        }
    }
    
    try:
        resp = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"Feishu notification failed: {e}")
        return False



st.title("📈 A股历史行情导出工具")
st.markdown("基于 **akshare**，支持导出 **威科夫分析** 所需的增强版 CSV（包含量价、换手率、振幅、均价、板块等）。")
st.markdown("💡 灵感来自 **秋生trader @Hoyooyoo**，祝各位在祖国的大A里找到价值！")

show_right_nav()

# Sidebar for inputs
with st.sidebar:
    st.header("参数配置")

    st.toggle(
        "手机模式",
        value=bool(st.session_state.get("mobile_mode", False)),
        key="mobile_mode",
        help="手机模式会优化按钮布局与表格展示。"
    )

    batch_mode = st.toggle(
        "批量生成",
        value=False,
        help=(
            "开启后支持手动输入多个代码或按板块全量添加。\\n"
            "注意：按板块添加可能涉及数千只股票，耗时较长且受数据源限流影响，请谨慎操作。"
        )
    )

    batch_symbols_text = ""
    selected_boards_codes = []
    
    if batch_mode:
        st.markdown("##### 📌 1. 手动输入代码")
        st.caption("批量模式：为降低失败率与封禁风险，固定回溯 60 个交易日，且最多 6 只股票。")
        batch_symbols_text = st.text_area(
            "股票代码列表（支持粘贴混合文本）",
            value="",
            placeholder="例如：000973;600798;300459（; 或 ；均可）",
            help="用分号（; 或 ；）分隔，系统会提取其中的 6 位数字作为股票代码（自动去重）。"
        )
        
        board_help = (
            "**💡 各板块交易规则速览**：\\n"
            "- **主板**: 门槛无特殊要求；涨跌幅限制 ±10%（ST股±5%）。\\n"
            "- **创业板**: 10万资产 + 2年经验；涨跌幅限制 ±20%。\\n"
            "- **科创板**: 50万资产 + 2年经验；涨跌幅限制 ±20%。\\n"
            "- **北交所**: 50万资产 + 2年经验；涨跌幅限制 ±30%。"
        )
        
        st.markdown("##### 📌 2. 按板块批量添加 (可选)", help=board_help)
        col_b1, col_b2, col_b3, col_b4 = st.columns(4)
        with col_b1:
            check_main = st.checkbox("主板", key="check_board_main", help=board_help)
        with col_b2:
            check_chinext = st.checkbox("创业板", key="check_board_chinext")
        with col_b3:
            check_star = st.checkbox("科创板", key="check_board_star")
        with col_b4:
            check_bse = st.checkbox("北交所", key="check_board_bse")
            
        if check_main:
            selected_boards_codes.extend([s['code'] for s in get_stocks_by_board("main")])
        if check_chinext:
            selected_boards_codes.extend([s['code'] for s in get_stocks_by_board("chinext")])
        if check_star:
            selected_boards_codes.extend([s['code'] for s in get_stocks_by_board("star")])
        if check_bse:
            selected_boards_codes.extend([s['code'] for s in get_stocks_by_board("bse")])
            
        if selected_boards_codes:
            st.info(f"✅ 已从板块选择 {len(selected_boards_codes)} 只股票")

    else:
        enable_stock_search = st.toggle(
            "启用股票名称搜索",
            value=True,
            help="开启后会加载全量股票列表用于搜索（首次加载可能较慢）。关闭则直接输入股票代码。"
        )

        stock_options = []
        if enable_stock_search:
            with st.spinner("正在加载股票列表..."):
                all_stocks = load_stock_list()
            stock_options = [f"{s['code']} {s['name']}" for s in all_stocks] if all_stocks else []

        if stock_options:
            default_index = 0
            if st.session_state.current_symbol:
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

            current_code = selected_stock.split(" ")[0]
            current_name_from_select = selected_stock.split(" ")[1] if len(selected_stock.split(" ")) > 1 else ""
            if current_code != st.session_state.current_symbol:
                st.session_state.current_symbol = current_code
        else:
            if enable_stock_search:
                st.warning("股票列表加载失败（可能是网络或数据源问题）。你仍可直接输入 6 位股票代码继续使用。")
                if st.button("🔄 重试加载股票列表", use_container_width=True):
                    load_stock_list.clear()
                    st.rerun()

            symbol_input = st.text_input(
                "股票代码 (必填)",
                value=st.session_state.current_symbol,
                help="请输入 6 位股票代码，例如 300364",
                key="symbol_input_widget"
            )
            if symbol_input != st.session_state.current_symbol:
                st.session_state.current_symbol = symbol_input
            current_name_from_select = ""

    
    symbol_name_input = ""
    if not batch_mode:
        symbol_name_input = st.text_input(
            "股票名称 (选填)",
            value=current_name_from_select,
            help="仅用于展示或文件名，留空则自动从 akshare 获取"
        )
    
    trading_days = st.number_input(
        "回溯交易日数量",
        min_value=1,
        max_value=700,
        value=min(500, 700),
        step=50,
        help="从结束日期向前回溯的交易日天数（上限 700）"
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
    
    st.markdown("---")

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
        
    try:
        is_mobile = bool(st.session_state.get("mobile_mode"))

        if batch_mode:
            symbols = _parse_batch_symbols(batch_symbols_text)
            
            if selected_boards_codes:
                symbols.extend(selected_boards_codes)
            symbols = _normalize_symbols(symbols)

            if not symbols:
                st.error("请至少输入 1 个股票代码，或勾选至少 1 个板块。")
                st.stop()
            if len(symbols) > 6:
                st.error(f"批量生成一次最多支持 6 个股票代码（当前识别到 {len(symbols)} 个）。")
                st.stop()

            progress_ph = st.empty()
            status_ph = st.empty()
            progress_bar = progress_ph.progress(0)
            results_ph = st.empty()

            with st.spinner(f"正在批量生成（{len(symbols)} 个）..."):
                end_calendar = date.today() - timedelta(days=int(end_offset))
                window = _resolve_trading_window(end_calendar, 60)

                zip_buffer = io.BytesIO()
                results: list[dict[str, str]] = []
                name_map = _stock_name_map()

                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for idx, symbol in enumerate(symbols, start=1):
                        status_ph.caption(f"({idx}/{len(symbols)}) 正在处理：{symbol}")
                        try:
                            name = name_map.get(symbol) or "Unknown"

                            df_hist = _fetch_hist(symbol, window, adjust)

                            sector = _stock_sector_em_timeout(symbol, timeout=60)
                            df_export = _build_export(df_hist, sector)

                            safe_symbol = _safe_filename_part(symbol)
                            safe_name = _safe_filename_part(name)
                            file_name_export = f"{safe_symbol}_{safe_name}_ohlcv.csv"
                            file_name_hist = f"{safe_symbol}_{safe_name}_hist_data.csv"

                            csv_export = df_export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                            csv_hist = df_hist.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")

                            zf.writestr(file_name_export, csv_export)
                            zf.writestr(file_name_hist, csv_hist)

                            add_to_history(symbol, name)
                            results.append({"symbol": symbol, "name": name, "status": "ok", "error": ""})
                        except Exception as e:
                            msg = _friendly_error_message(e, symbol, 60)
                            results.append({"symbol": symbol, "name": "", "status": "failed", "error": msg})
                        time.sleep(random.uniform(0.8, 1.2))
                        progress_bar.progress(idx / len(symbols))
                        results_ph.dataframe(results, use_container_width=True, height=260)

                zip_data = zip_buffer.getvalue()
                file_name_zip = f"batch_{_safe_filename_part(str(window.start_trade_date))}_{_safe_filename_part(str(window.end_trade_date))}.zip"

            # === 自动记录批量下载历史 ===
            # 只要任务完成，就记录一次
            symbols_str = "_".join(symbols[:3]) + (f"_etc_{len(symbols)}" if len(symbols) > 3 else "")
            current_batch_key = f"batch_{symbols_str}_{datetime.now().strftime('%H%M')}"
            last_batch_key = st.session_state.get("last_home_batch_key")
            
            if current_batch_key != last_batch_key:
                add_download_history(
                    page="Home",
                    source="批量生成",
                    title=f"批量 ({len(symbols)} 只)",
                    file_name=file_name_zip,
                    mime="application/zip",
                    data=None
                )
                st.session_state["last_home_batch_key"] = current_batch_key
            
            # Send Feishu notification
            if st.session_state.feishu_webhook:
                success_count = len([r for r in results if r["status"] == "ok"])
                failed_count = len(results) - success_count
                notify_title = f"📦 批量下载完成 ({success_count}/{len(symbols)})"
                notify_text = (
                    f"**任务状态**: 已完成\n"
                    f"**成功**: {success_count} 个\n"
                    f"**失败**: {failed_count} 个\n"
                    f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"**文件**: {file_name_zip}"
                )
                if failed_count > 0:
                    failed_details = "\\n".join([f"- {r['symbol']}: {r['error']}" for r in results if r["status"] != "ok"])
                    notify_text += f"\\n\\n**失败详情**:\\n{failed_details}"
                
                send_feishu_notification(st.session_state.feishu_webhook, notify_title, notify_text)
                st.toast("✅ 飞书通知已发送", icon="🔔")

            status_ph.empty()
            progress_ph.empty()
            results_ph.empty()

            st.subheader("📦 批量生成结果")
            st.dataframe(results, use_container_width=True)
            clicked = st.download_button(
                label="📦 下载全部 (.zip)",
                data=zip_data,
                file_name=file_name_zip,
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
            st.stop()

        if not st.session_state.current_symbol or not st.session_state.current_symbol.isdigit() or len(st.session_state.current_symbol) != 6:
            st.error("请输入有效的 6 位数字股票代码！")
            st.stop()

        with st.spinner(f"正在获取 {st.session_state.current_symbol} 的数据..."):
            end_calendar = date.today() - timedelta(days=int(end_offset))
            window = _resolve_trading_window(end_calendar, int(trading_days))
            
            if not symbol_name_input:
                try:
                    name = _stock_name_from_code(st.session_state.current_symbol)
                except Exception as e:
                    st.warning(f"无法自动获取名称: {e}")
                    name = "Unknown"
            else:
                name = symbol_name_input
            
            add_to_history(st.session_state.current_symbol, name)
            
            st.info(f"股票: **{st.session_state.current_symbol} {name}** | 时间窗口: **{window.start_trade_date}** 至 **{window.end_trade_date}** ({trading_days} 个交易日)")

            df_hist = _fetch_hist(st.session_state.current_symbol, window, adjust)
            sector = _stock_sector_em_timeout(st.session_state.current_symbol, timeout=60)
            df_export = _build_export(df_hist, sector)
            
            st.subheader("📊 数据预览")
            tab1, tab2 = st.tabs(["📈 OHLCV (增强版)", "📄 原始数据 (Hist Data)"])
            
            with tab1:
                if is_mobile:
                    st.dataframe(df_export, use_container_width=True, height=420)
                else:
                    st.dataframe(df_export, use_container_width=True)
            
            with tab2:
                if is_mobile:
                    st.dataframe(df_hist, use_container_width=True, height=420)
                else:
                    st.dataframe(df_hist, use_container_width=True)
            
            csv_export = df_export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            file_name_export = f"{st.session_state.current_symbol}_{name}_ohlcv.csv"
            
            csv_hist = df_hist.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            file_name_hist = f"{st.session_state.current_symbol}_{name}_hist_data.csv"

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(file_name_export, csv_export)
                zf.writestr(file_name_hist, csv_hist)
            zip_data = zip_buffer.getvalue()
            file_name_zip = f"{st.session_state.current_symbol}_{name}_all.zip"

            # === 自动记录单只下载历史 ===
            current_single_key = f"single_{st.session_state.current_symbol}_{datetime.now().strftime('%H%M')}"
            last_single_key = st.session_state.get("last_home_single_key")

            if current_single_key != last_single_key:
                add_download_history(
                    page="Home",
                    source="单只导出",
                    title=f"{st.session_state.current_symbol} {name}",
                    file_name=file_name_zip,
                    mime="application/zip",
                    data=None
                )
                st.session_state["last_home_single_key"] = current_single_key

            st.markdown("### 📥 下载数据")
            if is_mobile:
                st.download_button(
                    label="📦 全部下载 (.zip)",
                    data=zip_data,
                    file_name=file_name_zip,
                    mime="application/zip",
                    type="primary",
                    use_container_width=True,
                )
                st.download_button(
                    label="下载 OHLCV (增强版)",
                    data=csv_export,
                    file_name=file_name_export,
                    mime="text/csv",
                    use_container_width=True,
                )
                st.download_button(
                    label="下载原始数据 (Hist Data)",
                    data=csv_hist,
                    file_name=file_name_hist,
                    mime="text/csv",
                    use_container_width=True,
                )
            else:
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.download_button(
                        label="下载 OHLCV (增强版)",
                        data=csv_export,
                        file_name=file_name_export,
                        mime="text/csv",
                        type="primary",
                        use_container_width=True,
                    )
                
                with col2:
                    st.download_button(
                        label="下载原始数据 (Hist Data)",
                        data=csv_hist,
                        file_name=file_name_hist,
                        mime="text/csv",
                        use_container_width=True,
                    )

                with col3:
                    st.download_button(
                        label="📦 全部下载 (.zip)",
                        data=zip_data,
                        file_name=file_name_zip,
                        mime="application/zip",
                        type="primary",
                        use_container_width=True,
                    )
                
    except Exception as e:
        st.error(f"发生错误: {str(e)}")
        st.exception(e)

else:
    st.info("👈 请在左侧输入参数并点击“开始获取数据”")
