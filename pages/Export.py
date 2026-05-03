import random
import time
from datetime import date, datetime, timedelta

import streamlit as st
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.auth_component import logout
from app.layout import is_data_source_failure_message, setup_page, show_user_error
from app.navigation import show_right_nav
from app.ui_helpers import inject_custom_css, show_page_loading
from core.export_artifacts import (
    cleanup_export_artifacts,
    file_loader,
    write_dataframe_csv,
    write_zip_from_files,
)
from integrations.fetch_a_share_csv import (
    _build_export,
    _normalize_symbols,
    _resolve_trading_window,
    _stock_name_from_code,
    get_all_stocks,
    get_stocks_by_board,
)
from integrations.stock_hist_repository import get_stock_hist
from utils import extract_symbols_from_text, safe_filename_part, stock_sector_em

# Load environment variables from .env file
load_dotenv()

setup_page(page_title="数据导出", page_icon="📁")
inject_custom_css()

# === Logged In User Info ===
with st.sidebar:
    if st.session_state.get("user"):
        st.caption(f"当前用户: {st.session_state.user.get('email') if isinstance(st.session_state.user, dict) else ''}")
        if st.button("退出登录"):
            logout()
    st.divider()


@st.cache_data(ttl=3600, show_spinner=False, max_entries=1)
def load_stock_list():
    return get_all_stocks()


@st.cache_data(ttl=3600, show_spinner=False, max_entries=4)
def _cached_stocks_by_board(board: str):
    return get_stocks_by_board(board)


EXPORT_CLEANUP_INTERVAL_SECONDS = 3 * 60 * 60


def _maybe_cleanup_export_artifacts() -> None:
    now_ts = time.time()
    last_ts = float(st.session_state.get("export_cleanup_last_ts", 0))
    if now_ts - last_ts < EXPORT_CLEANUP_INTERVAL_SECONDS:
        return
    cleanup_export_artifacts()
    st.session_state.export_cleanup_last_ts = now_ts


# 增加网络请求重试机制，应对 RemoteDisconnected 等反爬限制
def _should_retry_fetch(e: Exception) -> bool:
    # 明确的"数据源全失败"不应重试，否则页面会长时间卡在加载中
    if is_data_source_failure_message(str(e)):
        return False
    return True


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_exception(_should_retry_fetch),
    reraise=True,
)
def _fetch_hist_with_retry(symbol, window, adjust):
    return get_stock_hist(
        symbol=symbol,
        start_date=window.start_trade_date,
        end_date=window.end_trade_date,
        adjust=adjust or "",
        context="web",
    )


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


def _parse_batch_symbols(text: str) -> list[str]:
    candidates = extract_symbols_from_text(str(text or ""), valid_codes=None)
    return _normalize_symbols(candidates)


@st.cache_data(ttl=3600, show_spinner=False, max_entries=1)
def _stock_name_map():
    stocks = load_stock_list()
    return {s.get("code"): s.get("name") for s in stocks if s.get("code")}


def _friendly_error_message(e: Exception, symbol: str, trading_days: int) -> str:
    msg = str(e)
    if "not found in stock list" in msg:
        return f"股票代码 {symbol} 未找到或已退市"
    if "empty data returned" in msg:
        return f"数据源返回空 (可能停牌或上市不足 {trading_days} 天)"
    # 数据源拉取失败：直接展示原始提示（已标明哪些免费数据源失败）
    if is_data_source_failure_message(msg):
        return msg
    return f"未知错误: {msg}"


content_col = show_right_nav()
with content_col:
    _maybe_cleanup_export_artifacts()
    st.title("📈 A股历史行情导出工具")
    st.markdown("基于 **akshare**，支持导出 **威科夫分析** 所需的增强版 CSV（包含量价、换手率、振幅、均价、板块等）。")
    st.markdown("💡 灵感来自 **秋生trader @Hoyooyoo**，祝各位在祖国的大A里找到价值！")

    # Sidebar for inputs
    with st.sidebar:
        st.header("参数配置")

        st.toggle(
            "手机模式",
            value=bool(st.session_state.get("mobile_mode", False)),
            key="mobile_mode",
            help="手机模式会优化按钮布局与表格展示。",
        )

        batch_mode = st.toggle(
            "批量生成",
            value=False,
            help=(
                "开启后支持手动输入多个代码或按板块全量添加。\\n"
                "注意：按板块添加可能涉及数千只股票，耗时较长且受数据源限流影响，请谨慎操作。"
            ),
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
                help="用分号（; 或 ；）分隔，系统会提取其中的 6 位数字作为股票代码（自动去重）。",
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
                selected_boards_codes.extend([s["code"] for s in _cached_stocks_by_board("main")])
            if check_chinext:
                selected_boards_codes.extend([s["code"] for s in _cached_stocks_by_board("chinext")])
            if check_star:
                selected_boards_codes.extend([s["code"] for s in _cached_stocks_by_board("star")])
            if check_bse:
                selected_boards_codes.extend([s["code"] for s in _cached_stocks_by_board("bse")])

            if selected_boards_codes:
                st.info(f"✅ 已从板块选择 {len(selected_boards_codes)} 只股票")

        else:
            enable_stock_search = st.toggle(
                "启用股票名称搜索",
                value=True,
                help="开启后会加载全量股票列表用于搜索（首次加载可能较慢）。关闭则直接输入股票代码。",
            )

            stock_options = []
            if enable_stock_search:
                loading = show_page_loading(title="思考中...", subtitle="正在加载股票列表")
                try:
                    all_stocks = load_stock_list()
                finally:
                    loading.empty()
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
                    key="stock_selector",
                )

                stock_parts = selected_stock.split(maxsplit=1)
                current_code = stock_parts[0] if stock_parts else ""
                current_name_from_select = stock_parts[1] if len(stock_parts) > 1 else ""
                if current_code != st.session_state.current_symbol:
                    st.session_state.current_symbol = current_code
            else:
                if enable_stock_search:
                    st.warning("股票列表加载失败（可能是网络或数据源问题）。你仍可直接输入 6 位股票代码继续使用。")
                    if st.button("🔄 重试加载股票列表", width="stretch"):
                        load_stock_list.clear()
                        st.rerun()

                symbol_input = st.text_input(
                    "股票代码 (必填)",
                    value=st.session_state.current_symbol,
                    help="请输入 6 位股票代码，例如 300364",
                    key="symbol_input_widget",
                )
                if symbol_input != st.session_state.current_symbol:
                    st.session_state.current_symbol = symbol_input
                current_name_from_select = ""

        symbol_name_input = ""
        if not batch_mode:
            symbol_name_input = st.text_input(
                "股票名称 (选填)",
                value=current_name_from_select,
                help="仅用于展示或文件名，留空则自动从 akshare 获取",
            )

        trading_days = st.number_input(
            "回溯交易日数量",
            min_value=1,
            max_value=700,
            value=min(320, 700),
            step=50,
            help="从结束日期向前回溯的交易日天数（上限 700）",
        )

        end_offset = st.number_input(
            "结束日期偏移 (天)",
            min_value=0,
            value=1,
            help="0 表示今天，1 表示昨天。系统会自动对齐到最近的交易日。",
        )

        adjust = st.selectbox(
            "复权类型",
            options=["", "qfq", "hfq"],
            format_func=lambda x: "不复权" if x == "" else ("前复权" if x == "qfq" else "后复权"),
            index=1,
            help=(
                "不复权：原始行情；\n"
                "前复权(qfq)：把历史价格按当前口径调整，除权后走势连续，适合看长期趋势；\n"
                "后复权(hfq)：把当前价格按历史口径调整，便于对比历史绝对价位。"
            ),
        )

        st.caption("复权用于处理分红送转等导致的价格跳变：前复权更常用于看趋势；后复权更常用于还原历史价位对比。")

        st.markdown("---")

        run_btn = st.button("🚀 开始获取数据", type="primary")

        if st.session_state.search_history:
            st.markdown("---")
            st.header("🕒 搜索历史")
            for item in st.session_state.search_history:
                label = f"{item['symbol']} {item['name']}"
                if st.button(label, key=f"hist_{item['symbol']}", width="stretch"):
                    set_symbol_from_history(item["symbol"])
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

                loading = show_page_loading(
                    title="思考中...",
                    subtitle=f"正在批量生成（{len(symbols)} 个）",
                )
                try:
                    end_calendar = date.today() - timedelta(days=int(end_offset))
                    window = _resolve_trading_window(end_calendar, 60)

                    results: list[dict[str, str]] = []
                    name_map = _stock_name_map()
                    zip_members: list[tuple[str, str]] = []
                    for idx, symbol in enumerate(symbols, start=1):
                        status_ph.caption(f"({idx}/{len(symbols)}) 正在处理：{symbol}")
                        try:
                            name = name_map.get(symbol) or "Unknown"

                            # 使用带重试的函数获取数据
                            df_hist = _fetch_hist_with_retry(symbol, window, adjust)

                            sector = stock_sector_em(symbol, timeout=60)
                            df_export = _build_export(df_hist, sector)

                            safe_symbol = safe_filename_part(symbol)
                            safe_name = safe_filename_part(name)
                            file_name_export = f"{safe_symbol}_{safe_name}_ohlcv.csv"
                            file_name_hist = f"{safe_symbol}_{safe_name}_hist_data.csv"

                            export_path = write_dataframe_csv(
                                df_export,
                                prefix=file_name_export.replace(".csv", ""),
                            )
                            hist_path = write_dataframe_csv(
                                df_hist,
                                prefix=file_name_hist.replace(".csv", ""),
                            )
                            zip_members.extend(
                                [
                                    (file_name_export, str(export_path)),
                                    (file_name_hist, str(hist_path)),
                                ]
                            )

                            add_to_history(symbol, name)
                            results.append(
                                {
                                    "symbol": symbol,
                                    "name": name,
                                    "status": "ok",
                                    "error": "",
                                }
                            )
                        except Exception as e:
                            msg = _friendly_error_message(e, symbol, 60)
                            results.append(
                                {
                                    "symbol": symbol,
                                    "name": "",
                                    "status": "failed",
                                    "error": msg,
                                }
                            )

                        # 延长请求间隔到 2.0 ~ 4.0 秒，降低被封禁概率
                        time.sleep(random.uniform(2.0, 4.0))
                        progress_bar.progress(idx / len(symbols))
                        results_ph.dataframe(results, width="stretch", height=260)

                    file_name_zip = (
                        f"batch_{safe_filename_part(str(window.start_trade_date))}_"
                        f"{safe_filename_part(str(window.end_trade_date))}.zip"
                    )
                    zip_path = write_zip_from_files(
                        zip_members,
                        prefix=file_name_zip.replace(".zip", ""),
                    )

                    # 通知：飞书 + 企微 + 钉钉（任一配置则发送）
                    feishu = st.session_state.get("feishu_webhook") or ""
                    wecom = st.session_state.get("wecom_webhook") or ""
                    dingtalk = st.session_state.get("dingtalk_webhook") or ""
                    if feishu or wecom or dingtalk:
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
                            failed_details = "\\n".join(
                                [f"- {r['symbol']}: {r['error']}" for r in results if r["status"] != "ok"]
                            )
                            notify_text += f"\\n\\n**失败详情**:\\n{failed_details}"
                        from utils.notify import send_all_webhooks

                        send_all_webhooks(feishu, wecom, dingtalk, notify_title, notify_text)
                        st.toast("✅ 通知已发送", icon="🔔")

                finally:
                    loading.empty()
                    status_ph.empty()
                    progress_ph.empty()
                    results_ph.empty()

                st.subheader("📦 批量生成结果")
                st.dataframe(results, width="stretch")
                st.download_button(
                    label="📦 下载全部 (.zip)",
                    data=file_loader(zip_path),
                    file_name=file_name_zip,
                    mime="application/zip",
                    type="primary",
                    width="stretch",
                )
                st.stop()

            if (
                not st.session_state.current_symbol
                or not st.session_state.current_symbol.isdigit()
                or len(st.session_state.current_symbol) != 6
            ):
                st.error("请输入有效的 6 位数字股票代码！")
                st.stop()

            loading = show_page_loading(
                title="思考中...",
                subtitle=f"正在获取 {st.session_state.current_symbol} 的数据",
            )
            try:
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

                st.info(
                    f"股票: **{st.session_state.current_symbol} {name}** | "
                    f"时间窗口: **{window.start_trade_date}** 至 "
                    f"**{window.end_trade_date}** ({trading_days} 个交易日)"
                )

                df_hist = _fetch_hist_with_retry(st.session_state.current_symbol, window, adjust)
                sector = stock_sector_em(st.session_state.current_symbol, timeout=60)
                df_export = _build_export(df_hist, sector)

                st.subheader("📊 数据预览")
                tab1, tab2 = st.tabs(["📈 OHLCV (增强版)", "📄 原始数据 (Hist Data)"])

                with tab1:
                    if is_mobile:
                        st.dataframe(df_export, width="stretch", height=420)
                    else:
                        st.dataframe(df_export, width="stretch")

                with tab2:
                    if is_mobile:
                        st.dataframe(df_hist, width="stretch", height=420)
                    else:
                        st.dataframe(df_hist, width="stretch")

                file_name_export = f"{st.session_state.current_symbol}_{name}_ohlcv.csv"
                file_name_hist = f"{st.session_state.current_symbol}_{name}_hist_data.csv"
                file_name_zip = f"{st.session_state.current_symbol}_{name}_all.zip"
                export_path = write_dataframe_csv(
                    df_export,
                    prefix=file_name_export.replace(".csv", ""),
                )
                hist_path = write_dataframe_csv(
                    df_hist,
                    prefix=file_name_hist.replace(".csv", ""),
                )
                zip_path = write_zip_from_files(
                    [
                        (file_name_export, str(export_path)),
                        (file_name_hist, str(hist_path)),
                    ],
                    prefix=file_name_zip.replace(".zip", ""),
                )

                st.markdown("### 📥 下载数据")
                if is_mobile:
                    st.download_button(
                        label="📦 全部下载 (.zip)",
                        data=file_loader(zip_path),
                        file_name=file_name_zip,
                        mime="application/zip",
                        type="primary",
                        width="stretch",
                    )
                    st.download_button(
                        label="下载 OHLCV (增强版)",
                        data=file_loader(export_path),
                        file_name=file_name_export,
                        mime="text/csv",
                        width="stretch",
                    )
                    st.download_button(
                        label="下载原始数据 (Hist Data)",
                        data=file_loader(hist_path),
                        file_name=file_name_hist,
                        mime="text/csv",
                        width="stretch",
                    )
                else:
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.download_button(
                            label="下载 OHLCV (增强版)",
                            data=file_loader(export_path),
                            file_name=file_name_export,
                            mime="text/csv",
                            type="primary",
                            width="stretch",
                        )

                    with col2:
                        st.download_button(
                            label="下载原始数据 (Hist Data)",
                            data=file_loader(hist_path),
                            file_name=file_name_hist,
                            mime="text/csv",
                            width="stretch",
                        )

                    with col3:
                        st.download_button(
                            label="📦 全部下载 (.zip)",
                            data=file_loader(zip_path),
                            file_name=file_name_zip,
                            mime="application/zip",
                            type="primary",
                            width="stretch",
                        )

            finally:
                loading.empty()

        except Exception as e:
            msg = str(e)
            if is_data_source_failure_message(msg):
                show_user_error(msg, None)
            else:
                show_user_error("发生错误，请稍后重试。", e)

    else:
        st.info("👈 请在左侧输入参数并点击「开始获取数据」")

    # ── 自定义导出跳转 ──
    st.page_link(
        "pages/CustomExport.py", label="🧰 更多数据源（ETF / 指数 / 宏观 CPI）→ 自定义导出", use_container_width=True
    )
