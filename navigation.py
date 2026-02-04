import streamlit as st


def _nav_button(label: str, help_text: str, target: str) -> None:
    if st.button(
        label,
        help=help_text,
        use_container_width=True,
        key=f"nav:{target}",
    ):
        st.switch_page(target)


def show_right_nav():
    """
    Create a right-side sticky navigation column and return the content column.
    The nav uses icon-only buttons with hover tooltips.
    """
    content_col, nav_col = st.columns([0.84, 0.16], gap="large")

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
            div[data-testid="stVerticalBlock"]:has(#nav-rail-anchor) .stButton > button {
                border-radius: 12px;
                width: 100%;
                height: 42px;
                font-size: 18px;
                padding: 0;
            }
            .nav-grid a {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 100%;
                height: 42px;
                border-radius: 12px;
                border: 1px solid rgba(128,128,128,0.25);
                text-decoration: none;
                font-size: 18px;
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

        grid_items = [
            ("🏠", "首页", "streamlit_app.py"),
            ("🧰", "自定义导出", "pages/CustomExport.py"),
            ("🕘", "下载历史", "pages/DownloadHistory.py"),
            ("🧭", "沙里淘金", "pages/WyckoffScreeners.py"),
            ("⚙️", "设置", "pages/Settings.py"),
            ("📢", "更新日志", "pages/Changelog.py"),
        ]

        cols = st.columns(2, gap="small")
        for idx, (icon, help_text, target) in enumerate(grid_items):
            with cols[idx % 2]:
                _nav_button(icon, help_text, target)

        st.markdown('<div class="nav-grid">', unsafe_allow_html=True)
        st.markdown(
            '<a href="https://github.com/YoungCan-Wang/Wyckoff-Analysis" '
            'target="_blank" title="GitHub">⭐</a>',
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)

    return content_col
