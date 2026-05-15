import os
import platform
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

from app.layout import is_data_source_failure_message
from app.ui_helpers import show_page_loading
from core.prompts import WYCKOFF_SINGLE_SYSTEM_PROMPT
from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window, _stock_name_from_code
from integrations.llm_client import call_llm
from utils import extract_symbols_from_text, stock_sector_em

TRADING_DAYS_OHLCV = 320  # 单股分析窗口：240~320 交易日，默认取上沿以保证 MA200 稳定
ADJUST = "qfq"
SINGLE_STOCK_FETCH_TIMEOUT_S = max(int(os.getenv("SINGLE_STOCK_FETCH_TIMEOUT_S", "70")), 20)
SINGLE_STOCK_SECTOR_TIMEOUT_S = max(int(os.getenv("SINGLE_STOCK_SECTOR_TIMEOUT_S", "20")), 5)
SINGLE_STOCK_LLM_TOTAL_TIMEOUT_S = max(int(os.getenv("SINGLE_STOCK_LLM_TOTAL_TIMEOUT_S", "240")), 60)
SINGLE_STOCK_LLM_REQUEST_TIMEOUT_S = max(int(os.getenv("SINGLE_STOCK_LLM_REQUEST_TIMEOUT_S", "90")), 15)
SINGLE_STOCK_PLOT_TIMEOUT_S = max(int(os.getenv("SINGLE_STOCK_PLOT_TIMEOUT_S", "45")), 10)
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def get_chinese_font_path():
    """获取系统中文字体路径"""
    system = platform.system()
    if system == "Darwin":
        paths = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    elif system == "Linux":
        # 常见 Linux/Docker 字体
        paths = [
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    return None


def _strip_code_blocks_for_ui(text: str) -> str:
    """
    页面展示时移除模型返回的代码块，避免在前端暴露 Python 实现细节。
    """
    if not text:
        return ""
    cleaned = re.sub(r"```(?:python)?\s*.*?```", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(
        r"(?im)^\s{0,3}#{0,6}\s*威科夫.*(?:绘图代码|标注图绘制代码).*$\n?",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?im)^\s*接下来[，,\s].*?python\s*代码[：:]\s*$\n?",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?im)^\s*请运行以下\s*python\s*代码[：:]\s*$\n?",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _prepare_plot_dataframe(df_hist: pd.DataFrame) -> pd.DataFrame:
    """
    将多来源历史数据统一为绘图输入字段：
    date/open/high/low/close/volume（缺失列尽量补齐）。
    """
    if df_hist is None or df_hist.empty:
        raise ValueError("历史数据为空，无法生成结构图")

    src = df_hist.copy()
    col_map = {str(c).strip().lower(): c for c in src.columns}

    def _pick(*candidates: str):
        for c in candidates:
            hit = col_map.get(c.lower())
            if hit is not None:
                return hit
        return None

    date_col = _pick("date", "trade_date", "datetime", "dt", "日期")
    close_col = _pick("close", "close_price", "last", "收盘")
    open_col = _pick("open", "open_price", "开盘")
    high_col = _pick("high", "high_price", "最高")
    low_col = _pick("low", "low_price", "最低")
    vol_col = _pick("volume", "vol", "成交量")

    if date_col is None:
        raise ValueError("缺少 date 列")
    if close_col is None:
        raise ValueError("缺少 close 列")

    out = pd.DataFrame()
    date_s = src[date_col].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    out["date"] = pd.to_datetime(date_s, errors="coerce")
    out["close"] = pd.to_numeric(src[close_col], errors="coerce")

    if open_col is not None:
        out["open"] = pd.to_numeric(src[open_col], errors="coerce")
    else:
        out["open"] = out["close"]
    if high_col is not None:
        out["high"] = pd.to_numeric(src[high_col], errors="coerce")
    else:
        out["high"] = out[["open", "close"]].max(axis=1)
    if low_col is not None:
        out["low"] = pd.to_numeric(src[low_col], errors="coerce")
    else:
        out["low"] = out[["open", "close"]].min(axis=1)
    if vol_col is not None:
        out["volume"] = pd.to_numeric(src[vol_col], errors="coerce")
    else:
        out["volume"] = 0.0

    out = out.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if out.empty:
        raise ValueError("历史数据为空，无法生成结构图")
    return out


def _run_with_timeout(desc: str, timeout_s: int, fn):
    """
    为单股分析关键步骤增加硬超时，避免页面无限转圈。
    """
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=max(int(timeout_s), 1))
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(f"{desc} 超时（>{timeout_s}s）")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _build_safe_structure_plot(df_hist: pd.DataFrame, symbol: str, name: str):
    """
    在禁用 LLM 代码执行时，使用固定模板输出一张可读结构图。
    仅依赖历史行情数据，不执行任何模型生成代码。
    """
    df_plot = _prepare_plot_dataframe(df_hist)

    # 仅展示最近 240 根，避免图形过于拥挤。
    if len(df_plot) > 240:
        df_plot = df_plot.tail(240).reset_index(drop=True)

    df_plot["MA50"] = df_plot["close"].rolling(50).mean()
    df_plot["MA200"] = df_plot["close"].rolling(200).mean()

    fig, (ax_price, ax_vol) = plt.subplots(
        2,
        1,
        figsize=(12, 7),
        sharex=True,
        gridspec_kw={"height_ratios": [3.0, 1.0]},
    )

    ax_price.plot(df_plot["date"], df_plot["close"], color="#303643", linewidth=1.6, label="Close")
    ax_price.plot(df_plot["date"], df_plot["MA50"], color="#d9480f", linewidth=1.3, label="MA50")
    ax_price.plot(df_plot["date"], df_plot["MA200"], color="#1d4ed8", linewidth=1.3, label="MA200")

    latest = float(df_plot["close"].iloc[-1])
    ax_price.axhline(latest, color="#6b7280", linestyle="--", linewidth=0.9, alpha=0.8)
    ax_price.text(
        df_plot["date"].iloc[-1],
        latest,
        f" {latest:.2f}",
        va="bottom",
        ha="left",
        color="#374151",
        fontsize=9,
    )

    title_name = f"{symbol} {name}".strip()
    ax_price.set_title(f"Wyckoff Structure Snapshot | {title_name}", fontsize=12, pad=10)
    ax_price.grid(alpha=0.22, linewidth=0.6)
    ax_price.legend(loc="upper left", fontsize=9, frameon=False)
    ax_price.set_ylabel("Price")

    has_real_open = "open" in df_hist.columns or "open_price" in df_hist.columns
    if has_real_open and df_plot["open"].notna().any():
        vol_colors = ["#d14343" if c >= o else "#1f8f55" for c, o in zip(df_plot["close"], df_plot["open"])]
    else:
        vol_colors = "#9ca3af"
    ax_vol.bar(df_plot["date"], df_plot["volume"], color=vol_colors, width=1.0, alpha=0.85)
    ax_vol.set_ylabel("Vol")
    ax_vol.grid(alpha=0.16, linewidth=0.5)

    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def render_single_stock_page(
    provider,
    model,
    api_key,
    *,
    base_url: str = "",
    feishu_webhook: str = "",  # deprecated, kept for caller compat
):
    """渲染单股分析页面"""
    st.markdown("### 🔍 威科夫单股分析 (大师模式)")
    st.caption("上传 K 线/分时图（可选），配合近 320 个交易日数据，生成大师级威科夫分析与标注图表。")

    col1, col2 = st.columns([1, 1])
    with col1:
        stock_input = st.text_input(
            "股票代码", placeholder="例如：601318", help="请输入单个 A 股代码", key="single_stock_code"
        )
    with col2:
        uploaded_file = st.file_uploader(
            "上传今日盘面截图 (可选)",
            type=["png", "jpg", "jpeg"],
            help="上传分时图或 K 线图，辅助判断当日微观结构",
            key="single_stock_image",
        )

    # 提取代码
    symbol = ""
    if stock_input:
        candidates = extract_symbols_from_text(stock_input)
        if candidates:
            symbol = candidates[0]

    run_btn = st.button("开始大师分析", type="primary", disabled=not symbol, key="run_single_stock")

    if run_btn and symbol:
        _run_analysis(
            symbol,
            uploaded_file,
            provider,
            model,
            api_key,
            base_url=base_url,
        )


def _run_analysis(
    symbol,
    image_file,
    provider,
    model,
    api_key,
    *,
    base_url: str = "",
):
    """执行分析流程"""
    end_calendar = date.today() - timedelta(days=1)
    try:
        window = _resolve_trading_window(end_calendar, TRADING_DAYS_OHLCV)
    except Exception as e:
        st.error(f"无法解析交易日窗口：{e}")
        return

    loading = show_page_loading(
        title="威科夫大师正在读图...",
        subtitle=f"正在拉取 {symbol} 近 {TRADING_DAYS_OHLCV} 天数据并进行结构分析",
    )

    try:
        # 获取 CSV 数据
        df_hist = _run_with_timeout(
            "历史行情拉取",
            SINGLE_STOCK_FETCH_TIMEOUT_S,
            lambda: _fetch_hist(symbol, window, ADJUST, user_id=st.session_state.get("user", {}).get("id", "")),
        )
        try:
            sector = _run_with_timeout(
                "行业信息获取",
                SINGLE_STOCK_SECTOR_TIMEOUT_S,
                lambda: stock_sector_em(symbol, timeout=SINGLE_STOCK_SECTOR_TIMEOUT_S),
            )
        except Exception:
            sector = "未知行业"
        try:
            name = _stock_name_from_code(symbol)
        except Exception:
            name = symbol

        # 通过 Strategy API 获取威科夫阶段和风险信息；主应用不再本地运行量化因子。
        from integrations.strategy_api_client import StrategyApiError, analyze_stock_legacy

        stage_info = ""
        exit_info = ""
        try:
            api_diag = analyze_stock_legacy(
                symbol,
                name=name,
                user_id=st.session_state.get("user", {}).get("id", ""),
            )
            phase = str(api_diag.get("ma_pattern") or api_diag.get("track") or api_diag.get("accum_stage") or "").strip()
            if phase:
                stage_info = f"✓ **当前阶段**: {phase}\n"
            risk_notes = [str(x) for x in api_diag.get("health_reasons") or [] if str(x).strip()]
            if risk_notes:
                exit_info = "⚠ **风险提醒**: " + "；".join(risk_notes[:2]) + "\n"
        except StrategyApiError as exc:
            st.error(f"Strategy API 结构分析失败：{exc}")
            return

        # 转换为 CSV 文本：优先使用标准字段，减少模型绘图代码字段不一致。
        try:
            csv_df = _prepare_plot_dataframe(df_hist)
        except Exception:
            csv_df = df_hist.copy()
        csv_text = csv_df.to_csv(index=False, encoding="utf-8-sig")

        # 准备 Prompt
        current_time = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
        font_path = get_chinese_font_path()
        font_hint = (
            f"\n【系统检测】当前环境建议中文字体路径：'{font_path}'"
            if font_path
            else "\n【系统检测】未检测到常见中文字体，请尝试自动查找。"
        )

        final_system_prompt = WYCKOFF_SINGLE_SYSTEM_PROMPT + font_hint

        user_msg = (
            f"当前北京时间（系统注入，UTC+8）：{current_time}\n"
            f"分析标的：{symbol} {name} ({sector})\n"
            f"数据长度：{len(df_hist)} 交易日\n\n"
            f"{stage_info}"
            f"{exit_info}"
            f"\n以下是 CSV 数据：\n```csv\n{csv_text}\n```\n\n"
            "请开始分析。图表由系统固定模板生成，不需要输出绘图代码。"
        )

        # 准备图片
        images = []
        if image_file:
            # 读取图片 bytes
            from PIL import Image

            img = Image.open(image_file)
            images.append(img)
            user_msg += "\n\n【用户已上传今日盘面截图，请结合分析】"
        response_text = _run_with_timeout(
            "大模型分析",
            SINGLE_STOCK_LLM_TOTAL_TIMEOUT_S,
            lambda: call_llm(
                provider=provider,
                model=model,
                api_key=api_key,
                system_prompt=final_system_prompt,
                user_message=user_msg,
                images=images,
                base_url=base_url or None,
                timeout=SINGLE_STOCK_LLM_REQUEST_TIMEOUT_S,
            ),
        )
        loading.empty()

        report_text = _strip_code_blocks_for_ui(response_text)
        st.markdown("### 📝 威科夫大师研报")
        st.markdown(report_text or "（研报正文已生成）")

        st.markdown("### 📊 结构标注图")
        with st.spinner("正在生成结构图..."):
            try:
                fig = _run_with_timeout(
                    "结构图生成",
                    SINGLE_STOCK_PLOT_TIMEOUT_S,
                    lambda: _build_safe_structure_plot(df_hist, symbol, name),
                )
                st.pyplot(fig)
            except Exception as e:
                st.error(f"结构图生成失败：{e}")
                st.expander("错误详情").text(traceback.format_exc())

    except Exception as e:
        loading.empty()
        msg = str(e)
        if is_data_source_failure_message(msg):
            st.error(msg)
        else:
            st.error(f"分析过程中发生错误：{e}")
        st.expander("错误详情").text(traceback.format_exc())
