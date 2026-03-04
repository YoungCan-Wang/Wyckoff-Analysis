import streamlit as st
import os
import sys

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.layout import setup_page
from app.navigation import show_right_nav
from integrations.supabase_client import save_user_settings
from app.ui_helpers import show_page_loading

setup_page(page_title="设置", page_icon="⚙️")

# Show Navigation
content_col = show_right_nav()
with content_col:

    st.title("⚙️ 设置 (Settings)")
    st.markdown("配置您的 API Key 和通知服务，让 Akshare 更加智能。")

    # 获取当前用户 ID
    user = st.session_state.get("user") or {}
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        st.error("无法识别当前用户，设置页已拒绝展示。请重新登录。")
        st.stop()

    # 顶部展示 user_id，方便复制
    with st.expander("🔑 账户信息", expanded=True):
        st.info(f"当前用户 ID (SUPABASE_USER_ID): `{user_id}`")
        st.caption("请复制此 ID 并配置到 GitHub Secrets 的 SUPABASE_USER_ID 中，以便定时任务能识别您的账户。")


    def on_save_settings():
        """保存配置到云端"""
        if not user_id:
            st.error("用户未登录，无法保存配置")
            return

        settings = {
            "feishu_webhook": st.session_state.feishu_webhook,
            "gemini_api_key": st.session_state.gemini_api_key,
            "tushare_token": st.session_state.tushare_token,
            "gemini_model": st.session_state.gemini_model,
            "tg_bot_token": st.session_state.tg_bot_token,
            "tg_chat_id": st.session_state.tg_chat_id,
        }

        loading = show_page_loading(title="加载中...", subtitle="正在保存到云端")
        try:
            if save_user_settings(user_id, settings):
                st.toast("✅ 配置已保存到云端", icon="☁️")
            else:
                st.toast("❌ 保存失败，请检查网络", icon="⚠️")
        finally:
            loading.empty()


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

            new_gemini_model = st.text_input(
                "Gemini 模型",
                value=st.session_state.gemini_model,
                placeholder="gemini-2.5-flash-lite",
                help="如 gemini-2.5-flash-lite、gemini-2.5-flash 等",
            )

            if st.button("💾 保存 AI 配置", key="save_ai"):
                st.session_state.gemini_api_key = new_gemini_key
                st.session_state.gemini_model = new_gemini_model
                on_save_settings()

        st.divider()

        # 3. 数据源
        st.subheader("📊 数据源配置")
        with st.container(border=True):
            st.markdown("**Tushare Token**（可选）用于行情、市值等。不配置时优先用 akshare/baostock/efinance，三者均失败时才需 Tushare。")
            new_tushare = st.text_input(
                "Tushare Token",
                value=st.session_state.tushare_token,
                type="password",
                placeholder="Tushare Pro token",
                key="tushare_input",
            )
            if st.button("💾 保存数据源配置", key="save_tushare"):
                st.session_state.tushare_token = new_tushare
                on_save_settings()

        st.divider()

        # 4. 私人决断
        st.subheader("🕶️ 私人决断")
        with st.container(border=True):
            st.markdown("可选，用于 Telegram 私密推送买卖建议。")
            new_tg_bot = st.text_input("Telegram Bot Token", value=st.session_state.tg_bot_token, type="password", key="tg_bot")
            new_tg_chat = st.text_input("Telegram Chat ID", value=st.session_state.tg_chat_id, type="password", key="tg_chat")
            if st.button("💾 保存 Step4 配置", key="save_step4"):
                st.session_state.tg_bot_token = new_tg_bot
                st.session_state.tg_chat_id = new_tg_chat
                on_save_settings()

        st.info("☁️ 您的配置已启用云端同步，将在所有登录设备间自动漫游。")
