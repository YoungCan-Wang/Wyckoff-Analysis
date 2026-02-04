import streamlit as st


def show_right_nav():
    """
    Keep a floating right-side nav for layout, but use native navigation
    buttons so the session is preserved.
    """
    st.markdown(
        """
        <style>
        .element-container:has(#float-nav-anchor) {
            display: none;
        }
        .element-container:has(#float-nav-anchor) + .element-container {
            position: fixed;
            right: 18px;
            top: 50%;
            transform: translateY(-50%);
            z-index: 99999;
            display: flex;
            flex-direction: column;
            gap: 8px;
            background: var(--secondary-background-color);
            border-radius: 16px;
            padding: 12px 8px;
            border: 1px solid rgba(128, 128, 128, 0.2);
            box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        }
        @media (max-width: 768px) {
            .element-container:has(#float-nav-anchor) + .element-container {
                right: 8px;
            }
        }
        .element-container:has(#float-nav-anchor) + .element-container [data-testid="stPageLink-NavLink"] {
            padding: 6px 10px;
            border-radius: 10px;
        }
        .element-container:has(#float-nav-anchor) + .element-container [data-testid="stPageLink-NavLink"]:hover {
            background: #FF4B4B;
            color: white;
        }
        .element-container:has(#float-nav-anchor) + .element-container .stButton > button {
            border-radius: 12px;
            width: 100%;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div id="float-nav-anchor"></div>', unsafe_allow_html=True)
    with st.container():
        st.page_link("streamlit_app.py", label="🏠")
        st.page_link("pages/CustomExport.py", label="🧰")
        st.page_link("pages/DownloadHistory.py", label="🕘")
        st.page_link("pages/WyckoffScreeners.py", label="🧭")
        st.page_link("pages/Settings.py", label="⚙️")
        st.page_link("pages/Changelog.py", label="📢")
        st.link_button("⭐", "https://github.com/YoungCan-Wang/Wyckoff-Analysis")
