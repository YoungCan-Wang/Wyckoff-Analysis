import streamlit as st

_RIGHT_NAV_OPEN_KEY = "right_nav_open"


def _toggle_right_nav() -> None:
    st.session_state[_RIGHT_NAV_OPEN_KEY] = not bool(
        st.session_state.get(_RIGHT_NAV_OPEN_KEY, True)
    )


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
    Right-side navigation rail that mirrors the left sidebar behavior:
    open by default, can collapse to the right, and keeps session state.
    """
    if _RIGHT_NAV_OPEN_KEY not in st.session_state:
        st.session_state[_RIGHT_NAV_OPEN_KEY] = True

    is_open = bool(st.session_state[_RIGHT_NAV_OPEN_KEY])
    if is_open:
        content_col, nav_col = st.columns([0.82, 0.18], gap="large")
    else:
        content_col, nav_col = st.columns([0.96, 0.04], gap="small")

    with nav_col:
        st.markdown(
            """
            <style>
            div[data-testid="stVerticalBlock"]:has(.right-nav-marker) {
                position: sticky;
                top: 80px;
                align-self: flex-start;
            }
            .right-nav-panel {
                border: 1px solid rgba(128, 128, 128, 0.2);
                border-radius: 16px;
                padding: 10px;
                background: var(--secondary-background-color);
            }
            .right-nav-collapsed {
                border: 1px solid rgba(128, 128, 128, 0.2);
                border-radius: 16px;
                padding: 8px 6px;
                background: var(--secondary-background-color);
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.markdown('<div class="right-nav-marker"></div>', unsafe_allow_html=True)

        if is_open:
            with st.container(border=False):
                st.markdown('<div class="right-nav-panel">', unsafe_allow_html=True)
                st.button(
                    "»",
                    key="right_nav_toggle",
                    help="向右收起导航栏",
                    use_container_width=True,
                    on_click=_toggle_right_nav,
                )

                _nav_button("🏠 首页", "返回首页", "streamlit_app.py")
                _nav_button("🧰 自定义导出", "打开自定义导出页", "pages/CustomExport.py")
                _nav_button("🕘 下载历史", "查看下载历史", "pages/DownloadHistory.py")
                _nav_button("🧭 沙里淘金", "打开沙里淘金页", "pages/WyckoffScreeners.py")
                _nav_button("⚙️ 设置", "打开设置页", "pages/Settings.py")
                _nav_button("📢 更新日志", "查看更新日志", "pages/Changelog.py")
                st.link_button(
                    "⭐ GitHub",
                    "https://github.com/YoungCan-Wang/Wyckoff-Analysis",
                    use_container_width=True,
                )
                st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.markdown('<div class="right-nav-collapsed">', unsafe_allow_html=True)
            st.button(
                "«",
                key="right_nav_toggle",
                help="展开导航栏",
                use_container_width=True,
                on_click=_toggle_right_nav,
            )
            st.markdown("</div>", unsafe_allow_html=True)

    return content_col
