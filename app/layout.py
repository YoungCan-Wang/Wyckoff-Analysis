import os

import streamlit as st

from app.auth_component import check_auth, login_form
from core.token_storage import restore_tokens_from_storage


def _set_default(key: str, value) -> None:
    if key not in st.session_state or st.session_state.get(key) is None:
        st.session_state[key] = value


def init_session_state() -> None:
    _set_default("user", None)
    _set_default("access_token", None)
    _set_default("refresh_token", None)
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

    _set_default("feishu_webhook", os.getenv("FEISHU_WEBHOOK_URL", ""))
    if st.session_state.feishu_webhook is None:
        st.session_state.feishu_webhook = ""

    _set_default("gemini_api_key", os.getenv("GEMINI_API_KEY", ""))
    if st.session_state.gemini_api_key is None:
        st.session_state.gemini_api_key = ""

    _set_default("tushare_token", os.getenv("TUSHARE_TOKEN", ""))
    if st.session_state.tushare_token is None:
        st.session_state.tushare_token = ""

    _set_default("gemini_model", os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
    if st.session_state.gemini_model is None:
        st.session_state.gemini_model = "gemini-2.0-flash"

    _set_default("tg_bot_token", os.getenv("TG_BOT_TOKEN", ""))
    if st.session_state.tg_bot_token is None:
        st.session_state.tg_bot_token = ""

    _set_default("tg_chat_id", os.getenv("TG_CHAT_ID", ""))
    if st.session_state.tg_chat_id is None:
        st.session_state.tg_chat_id = ""

    _set_default("my_portfolio_state", os.getenv("MY_PORTFOLIO_STATE", ""))
    if st.session_state.my_portfolio_state is None:
        st.session_state.my_portfolio_state = ""

    # 从 localStorage 恢复 token（刷新页面后登录态保持）
    access = st.session_state.get("access_token") or ""
    refresh = st.session_state.get("refresh_token") or ""
    if (not access or not refresh) and not st.session_state.get("_token_restore_attempted"):
        try:
            st.session_state["_token_restore_attempted"] = True
            restored_access, restored_refresh = restore_tokens_from_storage()
            if restored_access and restored_refresh:
                st.session_state.access_token = restored_access
                st.session_state.refresh_token = restored_refresh
        except Exception:
            pass


def _inject_base_ui_css() -> None:
    """注入全局基础样式，统一中文字体与控件排版。"""
    st.markdown(
        """
<style>
:root {
  --app-font-stack: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
    "Noto Sans CJK SC", "Source Han Sans SC", -apple-system, BlinkMacSystemFont,
    "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

[data-testid="stAppViewContainer"],
[data-testid="stSidebar"],
[data-testid="stHeader"] {
  font-family: var(--app-font-stack);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}

h1, h2, h3 {
  letter-spacing: 0;
}

[data-testid="stNumberInput"] input {
  font-family: var(--app-font-stack);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
  line-height: 1.35;
}

[data-testid="stNumberInput"] button {
  line-height: 1;
  font-weight: 600;
}

[data-testid="stDataEditor"] * {
  font-family: var(--app-font-stack) !important;
}

[data-testid="stDataEditor"] [role="columnheader"],
[data-testid="stDataEditor"] [role="gridcell"] {
  line-height: 1.35;
}
</style>
        """,
        unsafe_allow_html=True,
    )


def require_auth() -> None:
    if check_auth():
        return
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
    _inject_base_ui_css()
    if require_login:
        require_auth()


def is_data_source_failure_message(msg: str) -> bool:
    """判断是否为数据源拉取失败提示（已标明失败数据源，非程序 bug）"""
    return "拉取失败（非程序错误）" in msg or ("免费数据源" in msg and "均" in msg)


def show_user_error(message: str, err: Exception | None = None) -> None:
    st.error(message)
    if err is not None:
        st.caption(f"详情: {err}")
