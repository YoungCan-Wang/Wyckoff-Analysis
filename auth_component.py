import os
import streamlit as st
from streamlit_cookies_manager import EncryptedCookieManager
from supabase_client import get_supabase_client, load_user_settings
from ui_helpers import show_page_loading
from supabase import AuthApiError
import time

_ACCESS_TOKEN_KEY = "sb_access_token"
_REFRESH_TOKEN_KEY = "sb_refresh_token"


def _user_payload(user) -> dict | None:
    if user is None:
        return None
    if isinstance(user, dict):
        return {
            "id": user.get("id"),
            "email": user.get("email"),
        }
    if hasattr(user, "model_dump"):
        data = user.model_dump()
        return {"id": data.get("id"), "email": data.get("email")}
    if hasattr(user, "dict"):
        data = user.dict()
        return {"id": data.get("id"), "email": data.get("email")}
    return {"id": getattr(user, "id", None), "email": getattr(user, "email", None)}


def _safe_get_supabase_client():
    try:
        return get_supabase_client()
    except Exception as e:
        st.error(
            "Supabase 配置缺失或初始化失败，请检查 SUPABASE_URL/SUPABASE_KEY 或 "
            "Streamlit secrets 设置。"
        )
        st.caption(f"详细错误: {e}")
        return None


def _cookie_manager(clear_on_fail: bool = True) -> EncryptedCookieManager | None:
    manager = st.session_state.get("cookie_manager")
    if manager is None:
        secret = os.getenv("COOKIE_SECRET")
        if not secret:
            try:
                secret = st.secrets["COOKIE_SECRET"]
            except Exception:
                secret = None
        if not secret:
            st.error(
                "COOKIE_SECRET 未配置，无法持久化登录状态。请在环境变量或 secrets 中设置。"
            )
            return None
        manager = EncryptedCookieManager(
            prefix="wyckoff",
            password=secret,
        )
        st.session_state.cookie_manager = manager
    for _ in range(3):
        if manager.ready():
            st.session_state.cookies_pending = False
            st.session_state.cookies_pending_count = 0
            return manager
        time.sleep(0.2)

    pending_count = int(st.session_state.get("cookies_pending_count", 0)) + 1
    st.session_state.cookies_pending_count = pending_count
    if pending_count <= 3:
        st.session_state.cookies_pending = True
        return None

    st.session_state.cookies_pending = False
    st.session_state.cookies_pending_count = 0
    if clear_on_fail:
        st.session_state.user = None
        st.session_state.access_token = None
        st.session_state.refresh_token = None
        st.session_state.cookie_manager = None
    return None
    return manager


def login_form():
    """显示登录/注册表单"""
    supabase = _safe_get_supabase_client()
    if supabase is None:
        return

    st.markdown(
        """
    <style>
    .auth-container {
        max-width: 400px;
        margin: 0 auto;
        padding: 2rem;
        background-color: var(--secondary-background-color);
        border-radius: 10px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    .stButton button {
        width: 100%;
    }
    </style>
    """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown(
            """
            <div style="text-align: center; margin-bottom: 2rem;">
                <h1>🔐</h1>
                <h2>欢迎回来</h2>
                <p style="color: #666;">请登录以继续使用 Akshare 智能投研平台</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        tab1, tab2 = st.tabs(["登录", "注册"])

        with tab1:
            with st.form("login_form", clear_on_submit=False):
                email = st.text_input(
                    "邮箱", key="login_email", placeholder="name@example.com"
                )
                password = st.text_input(
                    "密码",
                    type="password",
                    key="login_password",
                    placeholder="请输入密码",
                )
                submit = st.form_submit_button("登录", type="primary", width="stretch")

                if submit:
                    try:
                        loading = show_page_loading(
                            title="加载中...", subtitle="正在登录"
                        )
                        try:
                            response = supabase.auth.sign_in_with_password(
                                {"email": email, "password": password}
                            )
                            st.session_state.user = _user_payload(response.user)
                            st.session_state.access_token = (
                                response.session.access_token
                            )
                            st.session_state.refresh_token = (
                                response.session.refresh_token
                            )
                            cookies = _cookie_manager(clear_on_fail=False)
                            if cookies is not None:
                                cookies[_ACCESS_TOKEN_KEY] = (
                                    response.session.access_token
                                )
                                cookies[_REFRESH_TOKEN_KEY] = (
                                    response.session.refresh_token
                                )
                                cookies.save()
                            # 登录成功，加载用户配置
                            if response.user is not None:
                                load_user_settings(response.user.id)
                            st.success("登录成功！")
                            time.sleep(0.5)
                            st.rerun()
                        finally:
                            loading.empty()
                    except AuthApiError as e:
                        st.error(f"登录失败: {e.message}")
                    except Exception as e:
                        st.error(f"登录失败: {str(e)}")

        with tab2:
            with st.form("register_form", clear_on_submit=False):
                new_email = st.text_input(
                    "邮箱", key="reg_email", placeholder="name@example.com"
                )
                new_password = st.text_input(
                    "密码",
                    type="password",
                    key="reg_password",
                    placeholder="至少 6 位字符",
                )
                confirm_password = st.text_input(
                    "确认密码",
                    type="password",
                    key="reg_confirm",
                    placeholder="请再次输入密码",
                )
                submit_reg = st.form_submit_button(
                    "注册新账号", type="primary", width="stretch"
                )

                if submit_reg:
                    if new_password != confirm_password:
                        st.error("两次输入的密码不一致")
                    elif len(new_password) < 6:
                        st.error("密码长度至少为 6 位")
                    else:
                        try:
                            loading = show_page_loading(
                                title="加载中...", subtitle="正在注册"
                            )
                            try:
                                response = supabase.auth.sign_up(
                                    {"email": new_email, "password": new_password}
                                )
                                st.success(
                                    "注册成功！请检查邮箱并点击验证链接完成激活。"
                                )
                            finally:
                                loading.empty()
                        except AuthApiError as e:
                            st.error(f"注册失败: {e.message}")
                        except Exception as e:
                            st.error(f"注册失败: {str(e)}")


def check_auth():
    """
    检查用户认证状态
    """
    supabase = _safe_get_supabase_client()
    if supabase is None:
        return True

    # 1. 如果 Session 中已有用户，直接通过
    user = st.session_state.get("user")
    if user is not None:
        return True

    cookies = _cookie_manager(clear_on_fail=True)
    if cookies is None:
        return False
    access_token = cookies.get(_ACCESS_TOKEN_KEY)
    refresh_token = cookies.get(_REFRESH_TOKEN_KEY)
    if access_token and refresh_token:
        try:
            session = supabase.auth.set_session(access_token, refresh_token)
            if session:
                st.session_state.user = _user_payload(session.user)
                st.session_state.access_token = session.access_token
                st.session_state.refresh_token = session.refresh_token
                if session.user is not None:
                    load_user_settings(session.user.id)
                return True
        except Exception:
            cookies.pop(_ACCESS_TOKEN_KEY, None)
            cookies.pop(_REFRESH_TOKEN_KEY, None)
            cookies.save()

    # 尝试恢复会话
    try:
        session = supabase.auth.get_session()
        if session:
            st.session_state.user = _user_payload(session.user)
            st.session_state.access_token = session.access_token
            st.session_state.refresh_token = session.refresh_token
            # 恢复会话成功，加载用户配置
            if session.user is not None:
                load_user_settings(session.user.id)
            return True
    except:
        pass

    return False


def logout():
    """登出"""
    supabase = _safe_get_supabase_client()
    if supabase is None:
        return
    try:
        supabase.auth.sign_out()
    except:
        pass
    st.session_state.user = None
    st.session_state.access_token = None
    st.session_state.refresh_token = None
    cookies = _cookie_manager(clear_on_fail=False)
    if cookies is None:
        return
    cookies.pop(_ACCESS_TOKEN_KEY, None)
    cookies.pop(_REFRESH_TOKEN_KEY, None)
    cookies.save()
    st.rerun()
