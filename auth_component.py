import os
import streamlit as st
from streamlit_cookies_manager import EncryptedCookieManager
from supabase_client import get_supabase_client, load_user_settings
from supabase import AuthApiError
import time

_ACCESS_TOKEN_KEY = "sb_access_token"
_REFRESH_TOKEN_KEY = "sb_refresh_token"


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


def _cookie_manager() -> EncryptedCookieManager | None:
    manager = st.session_state.get("cookie_manager")
    if manager is None:
        manager = EncryptedCookieManager(
            prefix="wyckoff",
            password=os.getenv("COOKIE_SECRET", "wyckoff-cookie-secret"),
        )
        st.session_state.cookie_manager = manager
    if not manager.ready():
        st.session_state.user = None
        st.session_state.access_token = None
        st.session_state.refresh_token = None
        st.session_state.cookie_manager = None
        st.warning(
            "登录状态无法恢复，已清空本地登录信息。请重新登录。"
        )
        st.caption("提示：如果浏览器阻止第三方 Cookie，也可能导致该问题。")
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
                        with st.spinner("正在登录..."):
                            response = supabase.auth.sign_in_with_password(
                                {"email": email, "password": password}
                            )
                            st.session_state.user = response.user
                            st.session_state.access_token = (
                                response.session.access_token
                            )
                            st.session_state.refresh_token = (
                                response.session.refresh_token
                            )
                            cookies = _cookie_manager()
                            if cookies is not None:
                                cookies[_ACCESS_TOKEN_KEY] = (
                                    response.session.access_token
                                )
                                cookies[_REFRESH_TOKEN_KEY] = (
                                    response.session.refresh_token
                                )
                                cookies.save()
                            # 登录成功，加载用户配置
                            load_user_settings(response.user.id)
                            st.success("登录成功！")
                            time.sleep(0.5)
                            st.rerun()
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
                            with st.spinner("正在注册..."):
                                response = supabase.auth.sign_up(
                                    {"email": new_email, "password": new_password}
                                )
                                st.success(
                                    "注册成功！请检查邮箱并点击验证链接完成激活。"
                                )
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
        return False

    # 1. 如果 Session 中已有用户，直接通过
    if "user" in st.session_state and st.session_state.user:
        return True

    cookies = _cookie_manager()
    if cookies is None:
        return False
    access_token = cookies.get(_ACCESS_TOKEN_KEY)
    refresh_token = cookies.get(_REFRESH_TOKEN_KEY)
    if access_token and refresh_token:
        try:
            session = supabase.auth.set_session(access_token, refresh_token)
            if session:
                st.session_state.user = session.user
                st.session_state.access_token = session.access_token
                st.session_state.refresh_token = session.refresh_token
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
            st.session_state.user = session.user
            st.session_state.access_token = session.access_token
            st.session_state.refresh_token = session.refresh_token
            # 恢复会话成功，加载用户配置
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
    cookies = _cookie_manager()
    if cookies is None:
        return
    cookies.pop(_ACCESS_TOKEN_KEY, None)
    cookies.pop(_REFRESH_TOKEN_KEY, None)
    cookies.save()
    st.rerun()
