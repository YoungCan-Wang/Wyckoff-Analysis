import os
import streamlit as st
from supabase import create_client, Client

@st.cache_resource
def get_supabase_client() -> Client:
    # 优先尝试从 os.getenv 读取（本地 .env 文件）
    # 其次尝试从 st.secrets 读取（Streamlit Cloud 部署环境）
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    
    if not url or not key:
        # 如果 os.getenv 没取到，再试 st.secrets
        try:
            url = st.secrets["SUPABASE_URL"]
            key = st.secrets["SUPABASE_KEY"]
        except (FileNotFoundError, KeyError):
            pass

    if not url or not key:
        raise ValueError("Missing Supabase credentials. Please set SUPABASE_URL and SUPABASE_KEY in .env or secrets.")
        
    return create_client(url, key)

def load_user_settings(user_id: str):
    """从 Supabase 加载用户配置到 st.session_state"""
    try:
        supabase = get_supabase_client()
        response = supabase.table("user_settings").select("*").eq("user_id", user_id).execute()
        
        if response.data and len(response.data) > 0:
            settings = response.data[0]
            # 仅当 session_state 为空时才覆盖，或者强制覆盖
            st.session_state.feishu_webhook = settings.get("feishu_webhook") or ""
            st.session_state.gemini_api_key = settings.get("gemini_api_key") or ""
            return True
    except Exception as e:
        print(f"Failed to load settings: {e}")
    return False

def save_user_settings(user_id: str, settings: dict):
    """保存用户配置到 Supabase"""
    try:
        supabase = get_supabase_client()
        data = {
            "user_id": user_id,
            **settings
        }
        # upsert: 存在则更新，不存在则插入
        supabase.table("user_settings").upsert(data).execute()
        return True
    except Exception as e:
        print(f"Failed to save settings: {e}")
        return False
