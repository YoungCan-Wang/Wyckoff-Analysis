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
    safe_quote = html.escape(str(quote or "")) if quote else ""

    placeholder = st.empty()
    with placeholder.container():
        left, center, right = st.columns([1, 2, 1])
        with center:
            st.markdown("<div style='height:10vh;'></div>", unsafe_allow_html=True)
            st.markdown(
                "<div style='text-align:center;font-size:36px;line-height:1;'>⏳</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<div style='text-align:center;font-size:42px;font-weight:700;margin-top:10px;'>{safe_title}</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<div style='text-align:center;font-size:30px;color:#666;margin-top:6px;'>{safe_subtitle}</div>",
                unsafe_allow_html=True,
            )
            if safe_quote:
                st.markdown(
                    (
                        "<div style='text-align:center;font-size:20px;color:#888;"
                        "margin-top:16px;font-style:italic;'>"
                        f"“{safe_quote}”"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )

    return placeholder
