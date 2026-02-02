import os
import time

import streamlit as st

from auth_component import check_auth, login_form
from ui_helpers import show_page_loading


def _set_default(key: str, value) -> None:
    if key not in st.session_state or st.session_state.get(key) is None:
        st.session_state[key] = value


def init_session_state() -> None:
    _set_default("user", None)
    _set_default("access_token", None)
    _set_default("refresh_token", None)
    _set_default("cookie_manager", None)
    _set_default("search_history", [])
    _set_default("current_symbol", "300364")
    _set_default("should_run", False)
    _set_default("mobile_mode", False)
    _set_default("last_home_batch_key", "")
    _set_default("last_home_single_key", "")
    _set_default("last_custom_export_query", "")
    _set_default("custom_export_df", None)
    _set_default("custom_export_source_id", "")
    _set_default("wyckoff_payload", None)
    _set_default("cookies_pending", False)
    _set_default("cookies_pending_count", 0)

    _set_default("feishu_webhook", os.getenv("FEISHU_WEBHOOK_URL", ""))
    if st.session_state.feishu_webhook is None:
        st.session_state.feishu_webhook = ""

    _set_default("gemini_api_key", os.getenv("GEMINI_API_KEY", ""))
    if st.session_state.gemini_api_key is None:
        st.session_state.gemini_api_key = ""


def require_auth() -> None:
    if check_auth():
        return
    if st.session_state.get("cookies_pending"):
        show_page_loading()
        time.sleep(0.3)
        st.rerun()
    empty_container = st.empty()
    with empty_container.container():
        login_form()
    st.stop()


def setup_page(
    *,
    page_title: str,
    page_icon: str,
    layout: str = "wide",
    require_login: bool = True,
) -> None:
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout=layout)
    init_session_state()
    if require_login:
        require_auth()


def show_user_error(message: str, err: Exception | None = None) -> None:
    st.error(message)
    if err is not None:
        st.caption(f"详情: {err}")
