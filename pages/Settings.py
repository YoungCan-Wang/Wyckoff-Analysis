import streamlit as st
import os
import sys

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from layout import setup_page
from navigation import show_right_nav
from supabase_client import save_user_settings

setup_page(page_title="设置", page_icon="⚙️")

# Show Navigation
show_right_nav()

st.title("⚙️ 设置 (Settings)")
st.markdown("配置您的 API Key 和通知服务，让 Akshare 更加智能。")

# 获取当前用户 ID
user_id = st.session_state.get("user").id if st.session_state.get("user") else None


def on_save_settings():
    """保存配置到云端"""
    if not user_id:
        st.error("用户未登录，无法保存配置")
        return

    settings = {
        "feishu_webhook": st.session_state.feishu_webhook,
        "gemini_api_key": st.session_state.gemini_api_key,
    }

    with st.spinner("正在保存到云端..."):
        if save_user_settings(user_id, settings):
            st.toast("✅ 配置已保存到云端", icon="☁️")
        else:
            st.toast("❌ 保存失败，请检查网络", icon="⚠️")


col1, col2 = st.columns([2, 1])

with col1:
    # 1. 飞书 Webhook
    st.subheader("🔔 通知配置")
    with st.container(border=True):
        st.markdown(
            "配置 **飞书 Webhook** 后，批量下载任务完成后将自动发送通知到您的飞书群。"
        )

        new_feishu_webhook = st.text_input(
            "飞书 Webhook URL",
            value=st.session_state.feishu_webhook,
            type="password",
            placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/...",
            help="如需获取 Webhook URL，请查看 [飞书官方教程](https://open.feishu.cn/community/articles/7271149634339422210)。",
        )

        if st.button("💾 保存 Webhook 配置", key="save_webhook"):
            if new_feishu_webhook != st.session_state.feishu_webhook:
                st.session_state.feishu_webhook = new_feishu_webhook
            on_save_settings()

    st.divider()

    # 2. Gemini API
    st.subheader("🧠 AI 配置")
    with st.container(border=True):
        st.markdown("配置 **Gemini API Key** 以启用智能诊股、研报摘要等高级功能。")

        new_gemini_key = st.text_input(
            "Gemini API Key",
            value=st.session_state.gemini_api_key,
            type="password",
            placeholder="AIzaSy...",
            help="获取 Key: [Google AI Studio](https://aistudio.google.com/api-keys)",
        )

        if st.button("💾 保存 AI 配置", key="save_ai"):
            if new_gemini_key != st.session_state.gemini_api_key:
                st.session_state.gemini_api_key = new_gemini_key
            on_save_settings()

    st.info("☁️ 您的配置已启用云端同步，将在所有登录设备间自动漫游。")
