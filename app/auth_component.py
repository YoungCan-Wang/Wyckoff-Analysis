import re

import streamlit as st
from supabase import AuthApiError

from app.ui_helpers import show_page_loading
from integrations.supabase_client import (
    get_supabase_client,
    load_user_settings,
    reset_user_settings_state,
)

try:
    from core.token_storage import clear_tokens_from_storage, persist_tokens_to_storage
except ImportError:

    def persist_tokens_to_storage(a: str, r: str) -> bool:
        return False

    def clear_tokens_from_storage() -> bool:
        return False


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MIN_PASSWORD_LEN = 6


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _is_valid_email(email: str) -> bool:
    return bool(_EMAIL_PATTERN.match(email))


def _user_payload(user) -> dict | None:
    if user is None:
        return None
    if isinstance(user, dict):
        return {"id": user.get("id"), "email": user.get("email")}
    # Pydantic v1/v2 及普通对象统一走 getattr
    return {"id": getattr(user, "id", None), "email": getattr(user, "email", None)}


def _safe_get_supabase_client():
    try:
        return get_supabase_client()
    except Exception as e:
        st.error("Supabase 配置缺失或初始化失败，请检查 SUPABASE_URL/SUPABASE_KEY 或 Streamlit secrets 设置。")
        st.caption(f"详细错误: {e}")
        return None


def _extract_user_from_response(response):
    if response is None:
        return None
    if hasattr(response, "user"):
        return response.user
    if isinstance(response, dict):
        return response.get("user")
    return None


def _restore_user_from_tokens(supabase) -> dict | None:
    access_token = st.session_state.get("access_token")
    refresh_token = st.session_state.get("refresh_token")
    if not access_token or not refresh_token:
        return None

    try:
        supabase.auth.set_session(access_token, refresh_token)
    except Exception:
        st.session_state.access_token = None
        st.session_state.refresh_token = None
        return None

    try:
        user_resp = supabase.auth.get_user(access_token)
    except TypeError:
        try:
            user_resp = supabase.auth.get_user()
        except Exception:
            return None
    except Exception:
        return None

    user_payload = _user_payload(_extract_user_from_response(user_resp))
    if not user_payload or not user_payload.get("id"):
        return None

    st.session_state.user = user_payload
    load_user_settings(user_payload["id"])
    return user_payload


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
    <p style="color: #666;">请登录以继续使用 Wyckoff 智能投研平台</p>
</div>
            """,
            unsafe_allow_html=True,
        )

        tab1, tab2 = st.tabs(["登录", "注册"])

        with tab1, st.form("login_form", clear_on_submit=False):
            email = st.text_input("邮箱", key="login_email", placeholder="name@example.com")
            password = st.text_input(
                "密码",
                type="password",
                key="login_password",
                placeholder="请输入密码",
            )
            submit = st.form_submit_button("登录", type="primary", width="stretch")

            if submit:
                email = _normalize_email(email)
                if not email or not password:
                    st.error("请填写邮箱和密码")
                elif not _is_valid_email(email):
                    st.error("请输入有效的邮箱地址")
                else:
                    try:
                        loading = show_page_loading(title="思考中...", subtitle="正在登录")
                        try:
                            response = supabase.auth.sign_in_with_password({"email": email, "password": password})
                            user_payload = _user_payload(response.user)
                            if not user_payload or not user_payload.get("id"):
                                raise RuntimeError("登录成功但未拿到用户信息")

                            st.session_state.user = user_payload
                            session = getattr(response, "session", None)
                            st.session_state.access_token = (
                                getattr(session, "access_token", None) if session is not None else None
                            )
                            st.session_state.refresh_token = (
                                getattr(session, "refresh_token", None) if session is not None else None
                            )
                            load_user_settings(user_payload["id"])
                            persist_tokens_to_storage(
                                st.session_state.access_token or "",
                                st.session_state.refresh_token or "",
                            )
                            st.success("登录成功！")
                            st.rerun()
                        finally:
                            loading.empty()
                    except AuthApiError:
                        st.error("登录失败：邮箱或密码错误，或账号尚未完成验证")
                    except Exception as e:
                        st.error(f"登录失败: {str(e)}")

        with tab2:
            st.caption("注册好账号密码后，无需邮件确认即可登录。")
            with st.form("register_form", clear_on_submit=False):
                new_email = st.text_input("邮箱", key="reg_email", placeholder="name@example.com")
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
                submit_reg = st.form_submit_button("注册新账号", type="primary", width="stretch")

                if submit_reg:
                    new_email = _normalize_email(new_email)
                    if not new_email:
                        st.error("请输入邮箱")
                    elif not _is_valid_email(new_email):
                        st.error("请输入有效的邮箱地址")
                    elif new_password != confirm_password:
                        st.error("两次输入的密码不一致")
                    elif len(new_password) < _MIN_PASSWORD_LEN:
                        st.error(f"密码长度至少为 {_MIN_PASSWORD_LEN} 位")
                    else:
                        try:
                            loading = show_page_loading(title="思考中...", subtitle="正在注册")
                            try:
                                supabase.auth.sign_up({"email": new_email, "password": new_password})
                                st.success("注册成功！无需邮件确认，可直接登录。")
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
        return False

    user = st.session_state.get("user")
    if isinstance(user, dict) and user.get("id"):
        return True

    restored = _restore_user_from_tokens(supabase)
    return restored is not None


def logout():
    """登出"""
    supabase = _safe_get_supabase_client()
    if supabase is None:
        return
    try:
        supabase.auth.sign_out()
    except Exception:
        pass
    clear_tokens_from_storage()
    reset_user_settings_state()
    # 清理聊天状态，防止跨账号会话串用
    for key in ("chat_manager", "chat_messages", "_chat_manager_user_id"):
        st.session_state.pop(key, None)
    st.session_state.user = None
    st.session_state.access_token = None
    st.session_state.refresh_token = None
    st.rerun()
