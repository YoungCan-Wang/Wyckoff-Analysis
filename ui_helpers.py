import streamlit as st


def show_page_loading(
    *,
    title: str = "加载中...",
    subtitle: str = "正在准备页面内容",
) -> st.delta_generator.DeltaGenerator:
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
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return placeholder
