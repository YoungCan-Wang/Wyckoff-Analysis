import json

import streamlit as st
from postgrest.exceptions import APIError
from supabase import Client

from core.constants import TABLE_USER_SETTINGS
from integrations.llm_client import DEFAULT_GEMINI_MODEL, OPENAI_COMPATIBLE_BASE_URLS
from integrations.supabase_base import create_anon_client

CUSTOM_PROVIDER_KEYS = ("1route", "zhipu", "minimax", "qwen", "volcengine")


def _parse_custom_providers(raw_value) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _pick_custom_provider_value(custom_map: dict, provider: str, *keys: str) -> str:
    info = custom_map.get(provider) or {}
    if not isinstance(info, dict):
        return ""
    for key in keys:
        value = str(info.get(key, "") or "").strip()
        if value:
            return value
    return ""


def reset_user_settings_state() -> None:
    """
    重置当前会话中的用户敏感配置，防止跨账号残留。
    """
    st.session_state.feishu_webhook = ""
    st.session_state.wecom_webhook = ""
    st.session_state.dingtalk_webhook = ""
    st.session_state.gemini_api_key = ""
    st.session_state.tushare_token = ""
    st.session_state.tickflow_api_key = ""
    st.session_state.gemini_model = DEFAULT_GEMINI_MODEL
    st.session_state.gemini_base_url = ""
    st.session_state.tg_bot_token = ""
    st.session_state.tg_chat_id = ""

    # 多厂商大模型配置（按需使用）
    st.session_state["1route_api_key"] = ""
    st.session_state["1route_model"] = ""
    st.session_state["1route_base_url"] = OPENAI_COMPATIBLE_BASE_URLS.get("1route", "")
    st.session_state.openai_api_key = ""
    st.session_state.openai_model = ""
    st.session_state.openai_base_url = OPENAI_COMPATIBLE_BASE_URLS.get("openai", "")
    st.session_state.zhipu_api_key = ""
    st.session_state.zhipu_model = ""
    st.session_state.zhipu_base_url = OPENAI_COMPATIBLE_BASE_URLS.get("zhipu", "")
    st.session_state.minimax_api_key = ""
    st.session_state.minimax_model = ""
    st.session_state.minimax_base_url = OPENAI_COMPATIBLE_BASE_URLS.get("minimax", "")
    st.session_state.deepseek_api_key = ""
    st.session_state.deepseek_model = ""
    st.session_state.deepseek_base_url = OPENAI_COMPATIBLE_BASE_URLS.get("deepseek", "")
    st.session_state.qwen_api_key = ""
    st.session_state.qwen_model = ""
    st.session_state.qwen_base_url = OPENAI_COMPATIBLE_BASE_URLS.get("qwen", "")
    st.session_state.volcengine_api_key = ""
    st.session_state.volcengine_model = ""
    st.session_state.volcengine_base_url = OPENAI_COMPATIBLE_BASE_URLS.get("volcengine", "")


def _get_supabase_client_base() -> Client:
    # ⚠️  此处必须填 anon key（公开权限），不得填 service_role key。
    # 若误填 service_role key，未登录用户将绕过 RLS，可读写所有用户数据。
    return create_anon_client()


def _apply_user_session(supabase: Client) -> None:
    """
    将当前用户会话绑定到 Supabase 客户端（用于 RLS）
    """
    access_token = st.session_state.get("access_token")
    refresh_token = st.session_state.get("refresh_token")

    if access_token and refresh_token:
        try:
            supabase.auth.set_session(access_token, refresh_token)
        except Exception:
            pass

    if access_token:
        supabase.postgrest.auth(access_token)
    else:
        # 回退到 anon key（未登录场景）。
        # ⚠️  此处 supabase_key 应为 anon key；若误配 service_role key 会绕过 RLS。
        supabase.postgrest.auth(supabase.supabase_key)


def get_supabase_client() -> Client:
    if "supabase_client_base" not in st.session_state:
        st.session_state.supabase_client_base = _get_supabase_client_base()
    supabase = st.session_state.supabase_client_base
    _apply_user_session(supabase)
    return supabase


def load_user_settings(user_id: str):
    """从 Supabase 加载用户配置到 st.session_state"""
    reset_user_settings_state()
    if not user_id:
        return False
    try:
        supabase = get_supabase_client()
        response = supabase.table(TABLE_USER_SETTINGS).select("*").eq("user_id", user_id).execute()

        if response.data and len(response.data) > 0:
            settings = response.data[0]
            custom_providers = _parse_custom_providers(settings.get("custom_providers"))
            # 通知类
            st.session_state.feishu_webhook = settings.get("feishu_webhook") or ""
            st.session_state.wecom_webhook = settings.get("wecom_webhook") or ""
            st.session_state.dingtalk_webhook = settings.get("dingtalk_webhook") or ""

            # 大模型配置
            st.session_state.gemini_api_key = settings.get("gemini_api_key") or ""
            st.session_state.gemini_model = settings.get("gemini_model") or DEFAULT_GEMINI_MODEL
            st.session_state.gemini_base_url = settings.get("gemini_base_url") or ""
            st.session_state.openai_api_key = settings.get("openai_api_key") or ""
            st.session_state.openai_model = settings.get("openai_model") or ""
            st.session_state.openai_base_url = settings.get("openai_base_url") or OPENAI_COMPATIBLE_BASE_URLS.get(
                "openai", ""
            )
            st.session_state.deepseek_api_key = settings.get("deepseek_api_key") or ""
            st.session_state.deepseek_model = settings.get("deepseek_model") or ""
            st.session_state.deepseek_base_url = settings.get("deepseek_base_url") or OPENAI_COMPATIBLE_BASE_URLS.get(
                "deepseek", ""
            )
            for provider in CUSTOM_PROVIDER_KEYS:
                st.session_state[f"{provider}_api_key"] = (
                    _pick_custom_provider_value(custom_providers, provider, "apikey", "api_key")
                    or settings.get(f"{provider}_api_key")
                    or ""
                )
                st.session_state[f"{provider}_model"] = (
                    _pick_custom_provider_value(custom_providers, provider, "model")
                    or settings.get(f"{provider}_model")
                    or ""
                )
                st.session_state[f"{provider}_base_url"] = (
                    _pick_custom_provider_value(custom_providers, provider, "baseurl", "base_url")
                    or settings.get(f"{provider}_base_url")
                    or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "")
                )

            # 读盘室供应商
            st.session_state.chat_provider = settings.get("chat_provider") or "gemini"

            # 其它
            st.session_state.tushare_token = settings.get("tushare_token") or ""
            st.session_state.tickflow_api_key = settings.get("tickflow_api_key") or ""
            st.session_state.tg_bot_token = settings.get("tg_bot_token") or ""
            st.session_state.tg_chat_id = settings.get("tg_chat_id") or ""
            return True
    except APIError as e:
        import logging

        logging.warning("Supabase API Error in load_user_settings: %s - %s", e.code, e.message)
        st.toast(f"⚠️ 配置加载异常: {e.code}", icon="⚠️")
    except Exception as e:
        import logging

        logging.warning("Unexpected error in load_user_settings: %s", e)
        st.toast("⚠️ 配置加载失败，将使用默认值", icon="⚠️")
    return False


def save_user_settings(user_id: str, settings: dict):
    """保存用户配置到 Supabase"""
    try:
        supabase = get_supabase_client()
        data = {"user_id": user_id, **settings}
        # upsert: 存在则更新，不存在则插入
        supabase.table(TABLE_USER_SETTINGS).upsert(data).execute()
        return True
    except APIError as e:
        print(f"Supabase API Error in save_user_settings: {e.code} - {e.message}")
        try:
            supabase = get_supabase_client()
            fallback = {"user_id": user_id, **settings}
            changed = False

            # 兼容 custom_providers 既可能是 jsonb（对象）也可能是 text（字符串）的库表定义
            if isinstance(fallback.get("custom_providers"), dict):
                fallback["custom_providers"] = json.dumps(fallback["custom_providers"], ensure_ascii=False)
                changed = True

            # 兼容旧表结构（尚未添加这些列）时的兜底
            for optional_key in ("custom_providers", "gemini_base_url", "chat_provider"):
                if optional_key in fallback and "column" in str(e.message or "").lower():
                    fallback.pop(optional_key, None)
                    changed = True

            if not changed:
                return False

            supabase.table(TABLE_USER_SETTINGS).upsert(fallback).execute()
            return True
        except Exception as retry_err:
            print(f"Retry save_user_settings failed: {retry_err}")
            return False
    except Exception as e:
        print(f"Unexpected error in save_user_settings: {e}")
        return False
