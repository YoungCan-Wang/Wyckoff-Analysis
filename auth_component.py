import streamlit as st
from supabase_client import get_supabase_client, load_user_settings
import time

def login_form():
    """显示登录/注册表单"""
    supabase = get_supabase_client()
    
    st.markdown("""
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
    """, unsafe_allow_html=True)

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
            unsafe_allow_html=True
        )
        
        tab1, tab2 = st.tabs(["登录", "注册"])
        
        with tab1:
            with st.form("login_form", clear_on_submit=False):
                email = st.text_input("邮箱", key="login_email", placeholder="name@example.com")
                password = st.text_input("密码", type="password", key="login_password", placeholder="请输入密码")
                submit = st.form_submit_button("登录", type="primary", use_container_width=True)
                
                if submit:
                    try:
                        with st.spinner("正在登录..."):
                            response = supabase.auth.sign_in_with_password({
                                "email": email,
                                "password": password
                            })
                            st.session_state.user = response.user
                            st.session_state.access_token = response.session.access_token
                            # 登录成功，加载用户配置
                            load_user_settings(response.user.id)
                            st.success("登录成功！")
                            time.sleep(0.5)
                            st.rerun()
                    except Exception as e:
                        st.error(f"登录失败: {str(e)}")

        with tab2:
            with st.form("register_form", clear_on_submit=False):
                new_email = st.text_input("邮箱", key="reg_email", placeholder="name@example.com")
                new_password = st.text_input("密码", type="password", key="reg_password", placeholder="至少 6 位字符")
                confirm_password = st.text_input("确认密码", type="password", key="reg_confirm", placeholder="请再次输入密码")
                submit_reg = st.form_submit_button("注册新账号", type="primary", use_container_width=True)
                
                if submit_reg:
                    if new_password != confirm_password:
                        st.error("两次输入的密码不一致")
                    elif len(new_password) < 6:
                        st.error("密码长度至少为 6 位")
                    else:
                        try:
                            with st.spinner("正在注册..."):
                                response = supabase.auth.sign_up({
                                    "email": new_email,
                                    "password": new_password
                                })
                                st.success("注册成功！请检查邮箱并点击验证链接完成激活。")
                        except Exception as e:
                            st.error(f"注册失败: {str(e)}")

def check_auth():
    """
    检查用户认证状态
    """
    supabase = get_supabase_client()
    
    # 1. 如果 Session 中已有用户，直接通过
    if "user" in st.session_state and st.session_state.user:
        return True

    # 尝试恢复会话
    try:
        session = supabase.auth.get_session()
        if session:
            st.session_state.user = session.user
            st.session_state.access_token = session.access_token
            # 恢复会话成功，加载用户配置
            load_user_settings(session.user.id)
            return True
    except:
        pass

    return False

def logout():
    """登出"""
    supabase = get_supabase_client()
    try:
        supabase.auth.sign_out()
    except:
        pass
    st.session_state.user = None
    st.session_state.access_token = None
    st.rerun()
