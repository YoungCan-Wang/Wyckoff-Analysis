import html
import streamlit as st


def show_page_loading(
    *,
    title: str = "加载中...",
    subtitle: str = "正在准备页面内容",
    quote: str | None = None,
) -> st.delta_generator.DeltaGenerator:
    """展示加载占位，可选展示一句名人名言（如股市大牛语录）。"""
    quote_html = ""
    if quote:
        safe = html.escape(quote)
        quote_html = f'<div style="font-size: 12px; color: #888; margin-top: 16px; font-style: italic; max-width: 360px; margin-left: auto; margin-right: auto;">"{safe}"</div>'
    placeholder = st.empty()
    placeholder.markdown(
        f"""
        <div style="width: 100%; min-height: 40vh; display: flex; align-items: center; justify-content: center;">
            <div style="text-align:center; padding: 24px 12px;">
                <div style="font-size: 28px; margin-bottom: 8px;">⏳</div>
                <div style="font-size: 16px; font-weight: 600;">{title}</div>
                <div style="font-size: 13px; color: #666; margin-top: 6px;">
                    {subtitle}
                </div>
                {quote_html}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return placeholder
