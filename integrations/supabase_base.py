"""
Supabase 客户端工厂 — 不依赖 Streamlit，CLI / 脚本 / Web 通用。

所有需要 Supabase 客户端的代码应从此模块获取，而不是各自 create_client。
- 脚本/定时任务：使用 create_admin_client()（service_role key，绕过 RLS）
- Web 端：使用 integrations.supabase_client.get_supabase_client()（内部调本模块 + 绑定用户 session）
- CLI：无 .env，自动回退到 cli/auth 内置的 anon key
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supabase import Client


def _resolve_credentials() -> tuple[str, str]:
    """解析 Supabase URL 和 Key，统一回退链：环境变量 → 内置 anon key → Streamlit secrets。"""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if url and key:
        return url, key
    # 内置 anon key（CLI / 无 .env 场景）
    from core.constants import SUPABASE_ANON_KEY, SUPABASE_ANON_URL

    url = url or SUPABASE_ANON_URL
    key = key or SUPABASE_ANON_KEY
    if url and key:
        return url, key
    # Streamlit Cloud
    try:
        import streamlit as st

        url = url or str(st.secrets.get("SUPABASE_URL", "") or "").strip()
        key = key or str(st.secrets.get("SUPABASE_KEY", "") or "").strip()
    except Exception:
        pass
    return url, key


def create_admin_client() -> Client:
    """Service-role 客户端（写库用，不经过 RLS）。

    优先读 SUPABASE_SERVICE_ROLE_KEY，回退到通用凭据链。
    """
    from supabase import create_client

    url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and service_key:
        return create_client(url, service_key)
    # 无 service_role key 时走通用链（CLI 场景用 anon key）
    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError("SUPABASE_URL / SUPABASE_KEY 未配置")
    return create_client(url, key)


def create_anon_client() -> Client:
    """Anon-key 客户端（RLS 保护）。

    Web 端由 supabase_client.get_supabase_client() 在此基础上绑定用户 session。
    """
    from supabase import create_client

    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError(
            "Missing Supabase credentials. Please set SUPABASE_URL and SUPABASE_KEY in .env or st.secrets."
        )
    return create_client(url, key)


def create_user_client(access_token: str, refresh_token: str = "") -> Client:
    """用用户 JWT 创建客户端（通过 RLS）。

    CLI 登录后拿到的 access_token 用于身份验证，等同于 Web 端
    supabase_client._apply_user_session 的逻辑。

    set_session 会消耗 refresh_token 并返回新 token pair，
    调用者应通过 get_session_tokens() 获取刷新后的 token 并回写。
    """
    from supabase import create_client

    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError("SUPABASE_URL / SUPABASE_KEY 未配置")
    client = create_client(url, key)
    if refresh_token:
        resp = client.auth.set_session(access_token, refresh_token)
        # set_session 返回新 token pair，用新的 access_token 做 postgrest auth
        new_at = getattr(resp, "access_token", None) or (
            resp.session.access_token if hasattr(resp, "session") and resp.session else None
        )
        if new_at:
            access_token = new_at
    client.postgrest.auth(access_token)
    return client


def get_session_tokens(client: Client) -> tuple[str, str]:
    """从 client 中提取当前有效的 access_token 和 refresh_token。"""
    try:
        session = client.auth.get_session()
        if session:
            return session.access_token or "", session.refresh_token or ""
    except Exception:
        pass
    return "", ""


def is_admin_configured() -> bool:
    """检查是否存在显式 Supabase 凭据（env / st.secrets）。

    说明：
    - 这里用于判断“是否完成业务级配置”，不应把内置 anon 凭据视为“已配置”。
    - 因此不走 _resolve_credentials()（该函数会回退到内置 anon）。
    """
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip() or os.getenv("SUPABASE_KEY", "").strip()
    if url and key:
        return True
    try:
        import streamlit as st

        sec_url = str(st.secrets.get("SUPABASE_URL", "") or "").strip()
        sec_key = (
            str(st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()
            or str(st.secrets.get("SUPABASE_KEY", "") or "").strip()
        )
        return bool(sec_url and sec_key)
    except Exception:
        return False
