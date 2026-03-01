# -*- coding: utf-8 -*-
import re
import traceback
from datetime import date, datetime, timedelta
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import platform
import os

from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window, _stock_name_from_code
from utils import extract_symbols_from_text, stock_sector_em
from integrations.llm_client import call_llm
from core.wyckoff_single_prompt import WYCKOFF_SINGLE_SYSTEM_PROMPT
from app.layout import is_data_source_failure_message
from app.ui_helpers import show_page_loading

TRADING_DAYS_OHLCV = 500  # 威科夫分析需要较长周期
ADJUST = "qfq"

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
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    return None

def extract_python_code(text: str) -> str | None:
    """从 LLM 回复中提取 Python 代码块"""
    # 匹配 ```python ... ``` 或 ``` ... ```
    pattern = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)
    matches = pattern.findall(text)
    if matches:
        # 返回最长的一段，通常是完整代码
        return max(matches, key=len)
    return None

def render_single_stock_page(provider, model, api_key):
    """渲染单股分析页面"""
    st.markdown("### 🔍 威科夫单股分析 (大师模式)")
    st.caption("上传 K 线/分时图（可选），配合 500 天历史数据，生成大师级威科夫分析与标注图表。")

    col1, col2 = st.columns([1, 1])
    with col1:
        stock_input = st.text_input(
            "股票代码",
            placeholder="例如：600519",
            help="请输入单个 A 股代码",
            key="single_stock_code"
        )
    with col2:
        uploaded_file = st.file_uploader(
            "上传今日盘面截图 (可选)",
            type=["png", "jpg", "jpeg"],
            help="上传分时图或 K 线图，辅助判断当日微观结构",
            key="single_stock_image"
        )

    # 提取代码
    symbol = ""
    if stock_input:
        candidates = extract_symbols_from_text(stock_input)
        if candidates:
            symbol = candidates[0]

    run_btn = st.button("开始大师分析", type="primary", disabled=not symbol, key="run_single_stock")

    if run_btn and symbol:
        _run_analysis(symbol, uploaded_file, provider, model, api_key)

def _run_analysis(symbol, image_file, provider, model, api_key):
    """执行分析流程"""
    # 1. 准备数据
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
        df_hist = _fetch_hist(symbol, window, ADJUST)
        sector = stock_sector_em(symbol, timeout=30)
        try:
            name = _stock_name_from_code(symbol)
        except Exception:
            name = symbol
        
        # 转换为 CSV 文本
        csv_text = df_hist.to_csv(index=False, encoding="utf-8-sig")
        
        # 准备 Prompt
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        font_path = get_chinese_font_path()
        font_hint = f"\n【系统检测】当前环境建议中文字体路径：'{font_path}'" if font_path else "\n【系统检测】未检测到常见中文字体，请尝试自动查找。"
        
        final_system_prompt = WYCKOFF_SINGLE_SYSTEM_PROMPT + font_hint
        
        user_msg = (
            f"当前北京时间：{current_time}\n"
            f"分析标的：{symbol} {name} ({sector})\n"
            f"数据长度：{len(df_hist)} 交易日\n\n"
            f"以下是 CSV 数据：\n```csv\n{csv_text}\n```\n\n"
            "请开始分析，并生成绘图代码。"
        )

        # 准备图片
        images = []
        if image_file:
            # 读取图片 bytes
            from PIL import Image
            img = Image.open(image_file)
            images.append(img)
            user_msg += "\n\n【用户已上传今日盘面截图，请结合分析】"

        # 2. 调用 LLM
        response_text = call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=final_system_prompt,
            user_message=user_msg,
            images=images,
            timeout=180, # 增加超时时间，因为任务复杂
        )

        # 3. 展示分析结果
        loading.empty()
        
        # 分离代码和文本
        code_block = extract_python_code(response_text)
        
        # 展示文本部分（去除代码块后，或者直接展示全部）
        # 为了美观，我们可以尝试把代码块折叠，或者只展示非代码部分
        # 这里简单起见，直接展示 Markdown
        st.markdown("### 📝 威科夫大师研报")
        st.markdown(response_text)

        # 4. 执行绘图代码
        if code_block:
            st.markdown("### 📊 结构标注图")
            with st.spinner("正在绘制图表..."):
                try:
                    # 准备执行环境
                    exec_globals = {
                        "pd": pd,
                        "plt": plt,
                        "fm": fm,
                        "datetime": datetime,
                        "date": date
                    }
                    # 执行代码定义
                    exec(code_block, exec_globals)
                    
                    # 调用 create_plot
                    if "create_plot" in exec_globals:
                        # 传入 df，注意 df 已经在 _fetch_hist 中处理过，但需要确保日期格式
                        df_plot = df_hist.copy()
                        # _fetch_hist 返回的 df 列名通常是 date, open, close... 且 date 可能是 string
                        if 'date' in df_plot.columns:
                            df_plot['date'] = pd.to_datetime(df_plot['date'])
                        
                        fig = exec_globals["create_plot"](df_plot)
                        st.pyplot(fig)
                    else:
                        st.warning("未找到 create_plot 函数，无法绘图。")
                except Exception as e:
                    st.error(f"绘图代码执行失败：{e}")
                    st.expander("查看生成代码").code(code_block, language="python")
                    st.expander("错误详情").text(traceback.format_exc())

    except Exception as e:
        loading.empty()
        msg = str(e)
        if is_data_source_failure_message(msg):
            st.error(msg)
        else:
            st.error(f"分析过程中发生错误：{e}")
        st.expander("错误详情").text(traceback.format_exc())
