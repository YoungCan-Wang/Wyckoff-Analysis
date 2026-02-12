import html

import streamlit as st


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
            '<div style="font-size:12px;color:#888;margin-top:16px;font-style:italic;'
            'max-width:360px;margin-left:auto;margin-right:auto;">'
            f'"{safe_quote}"'
            "</div>"
        )

    # NOTE:
    # Do not indent HTML lines with 4+ leading spaces; Markdown may treat them as code block,
    # causing raw tags like "</div>" to appear in UI.
    loading_html = (
        '<div style="width:100%;min-height:40vh;display:flex;align-items:center;justify-content:center;">'
        '<div style="text-align:center;padding:24px 12px;">'
        '<div style="font-size:28px;margin-bottom:8px;">⏳</div>'
        f'<div style="font-size:16px;font-weight:600;">{safe_title}</div>'
        '<div style="font-size:13px;color:#666;margin-top:6px;">'
        f"{safe_subtitle}"
        "</div>"
        f"{quote_html}"
        "</div>"
        "</div>"
    )

    placeholder = st.empty()
    placeholder.markdown(loading_html, unsafe_allow_html=True)
    return placeholder
