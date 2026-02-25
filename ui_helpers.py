import html
import textwrap

import streamlit as st


def inject_custom_css():
    """注入全局自定义 CSS"""
    st.markdown(
        """
        <style>
        /* 全局字体优化 */
        .stApp {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol";
        }
        
        /* 按钮样式优化 */
        .stButton button {
            border-radius: 8px;
            font-weight: 600;
            transition: all 0.2s ease-in-out;
        }
        .stButton button:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }

        /* 卡片式容器 */
        .css-1r6slb0, .css-12w0qpk {
            border-radius: 12px;
            padding: 1rem;
            background-color: white;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }

        /* 加载动画 CSS */
        .loader {
            width: 48px;
            height: 48px;
            border: 5px solid #FFF;
            border-bottom-color: #FF4B4B;
            border-radius: 50%;
            display: inline-block;
            box-sizing: border-box;
            animation: rotation 1s linear infinite;
        }

        @keyframes rotation {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_page_loading(
    *,
    title: str = "加载中...",
    subtitle: str = "正在准备页面内容",
    quote: str | None = None,
) -> st.delta_generator.DeltaGenerator:
    """展示加载占位，可选展示一句名人名言（如股市大牛语录）。"""
    safe_title = html.escape(str(title or ""))
    safe_subtitle = html.escape(str(subtitle or ""))

    quote_html = ""
    if quote:
        safe_quote = html.escape(str(quote))
        quote_html = (
            '<div style="font-size: 12px; color: #888; margin-top: 16px; '
            'font-style: italic; max-width: 360px; margin-left: auto; margin-right: auto;">'
            f'"{safe_quote}"'
            "</div>"
        )

    loading_html = f"""
<style>
.loading-spinner {{
    width: 48px;
    height: 48px;
    border: 4px solid #e5e7eb;
    border-bottom-color: #ef4444;
    border-radius: 50%;
    display: inline-block;
    box-sizing: border-box;
    animation: loading-spin 0.8s linear infinite;
}}
@keyframes loading-spin {{
    0% {{ transform: rotate(0deg); }}
    100% {{ transform: rotate(360deg); }}
}}
</style>
<div style="width: 100%; min-height: 40vh; display: flex; align-items: center; justify-content: center; flex-direction: column;">
    <div class="loading-spinner"></div>
    <div style="text-align:center; padding: 24px 12px;">
        <div style="font-size: 16px; font-weight: 600; color: #333;">{safe_title}</div>
        <div style="font-size: 13px; color: #666; margin-top: 6px;">
            {safe_subtitle}
        </div>
        {quote_html}
    </div>
</div>
"""

    placeholder = st.empty()
    placeholder.markdown(loading_html, unsafe_allow_html=True)
    return placeholder
