import streamlit as st


def show_right_nav():
    """
    Create a right-side sticky navigation column and return the content column.
    """
    content_col, nav_col = st.columns([0.82, 0.18], gap="large")

    with nav_col:
        st.markdown(
            """
            <style>
            #nav-rail-anchor {
                display: none;
            }
            div[data-testid="stVerticalBlock"]:has(#nav-rail-anchor) {
                position: sticky;
                top: 96px;
                align-self: flex-start;
            }
            div[data-testid="stVerticalBlock"]:has(#nav-rail-anchor) [data-testid="stPageLink-NavLink"] {
                padding: 6px 10px;
                border-radius: 10px;
            }
            div[data-testid="stVerticalBlock"]:has(#nav-rail-anchor) [data-testid="stPageLink-NavLink"]:hover {
                background: #FF4B4B;
                color: white;
            }
            div[data-testid="stVerticalBlock"]:has(#nav-rail-anchor) .stButton > button {
                border-radius: 12px;
                width: 100%;
            }
            @media (max-width: 900px) {
                div[data-testid="stVerticalBlock"]:has(#nav-rail-anchor) {
                    position: static;
                }
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.markdown('<div id="nav-rail-anchor"></div>', unsafe_allow_html=True)
        st.page_link("streamlit_app.py", label="🏠 首页")
        st.page_link("pages/CustomExport.py", label="🧰 自定义导出")
        st.page_link("pages/DownloadHistory.py", label="🕘 下载历史")
        st.page_link("pages/WyckoffScreeners.py", label="🧭 沙里淘金")
        st.page_link("pages/Settings.py", label="⚙️ 设置")
        st.page_link("pages/Changelog.py", label="📢 更新日志")
        st.link_button(
            "⭐ GitHub",
            "https://github.com/YoungCan-Wang/Wyckoff-Analysis",
            use_container_width=True,
        )

    return content_col
