"""
Token 持久化 — 服务端缓存 + URL query_params 双通道。

写入时：token 存入服务端 cache_resource（进程级内存），session_key 写入 URL query_params。
读取时：从 URL query_params 取 session_key → 查服务端缓存 → 恢复 token。
清除时：删缓存 + 清 query_params。

不依赖任何浏览器端 JavaScript，彻底规避 iframe 沙箱限制。
服务端重启后缓存丢失，用户需重新登录（可接受）。
"""

from __future__ import annotations

import secrets
import time

import streamlit as st

_QP_KEY = "sk"  # URL 里的 query param 名称
_MAX_ENTRIES = 500  # 最大缓存条目（防内存泄漏）
_ENTRY_TTL = 7 * 86400  # 7 天过期


# ---------------------------------------------------------------------------
# 服务端 token 缓存（进程级，st.cache_resource 保证跨 session 共享）
# ---------------------------------------------------------------------------


@st.cache_resource
def _get_token_store() -> dict:
    """全局 token 缓存：{session_key: {access_token, refresh_token, ts}}"""
    return {}


def _cleanup_store(store: dict) -> None:
    """淘汰过期和超量条目。"""
    now = time.time()
    expired = [k for k, v in store.items() if now - v.get("ts", 0) > _ENTRY_TTL]
    for k in expired:
        store.pop(k, None)
    # 超量时按时间淘汰最旧的
    if len(store) > _MAX_ENTRIES:
        sorted_keys = sorted(store, key=lambda k: store[k].get("ts", 0))
        for k in sorted_keys[: len(store) - _MAX_ENTRIES]:
            store.pop(k, None)


# ---------------------------------------------------------------------------
# Persist (write)
# ---------------------------------------------------------------------------


def persist_tokens_to_storage(access_token: str, refresh_token: str) -> bool:
    """将 token 存入服务端缓存，session_key 写入 URL query_params。"""
    if not access_token or not refresh_token:
        return False
    try:
        store = _get_token_store()
        _cleanup_store(store)

        # 复用已有 session_key（避免每次登录生成新 key）
        sk = st.session_state.get("_session_key") or secrets.token_urlsafe(24)
        store[sk] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "ts": time.time(),
        }
        st.session_state["_session_key"] = sk
        st.query_params[_QP_KEY] = sk
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Restore (read)
# ---------------------------------------------------------------------------


def restore_tokens_from_storage() -> tuple[str | None, str | None]:
    """从 URL query_params 读 session_key → 查服务端缓存 → 返回 token。"""
    try:
        sk = st.query_params.get(_QP_KEY)
        if not sk:
            return (None, None)
        store = _get_token_store()
        entry = store.get(sk)
        if not entry:
            return (None, None)
        # 检查 TTL
        if time.time() - entry.get("ts", 0) > _ENTRY_TTL:
            store.pop(sk, None)
            return (None, None)
        access = (entry.get("access_token") or "").strip()
        refresh = (entry.get("refresh_token") or "").strip()
        if access and refresh:
            # 续期
            entry["ts"] = time.time()
            st.session_state["_session_key"] = sk
            return (access, refresh)
    except Exception:
        pass
    return (None, None)


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


def clear_tokens_from_storage() -> bool:
    """清除服务端缓存 + URL query_params。"""
    try:
        sk = st.session_state.get("_session_key") or st.query_params.get(_QP_KEY)
        if sk:
            store = _get_token_store()
            store.pop(sk, None)
        if _QP_KEY in st.query_params:
            del st.query_params[_QP_KEY]
        st.session_state.pop("_session_key", None)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Query params 同步（确保跨页面导航后 URL 仍带 session_key）
# ---------------------------------------------------------------------------


def ensure_query_params_synced() -> None:
    """如果 session_state 有 session_key 但 URL 没有，补写 URL。"""
    sk = st.session_state.get("_session_key")
    if sk and st.query_params.get(_QP_KEY) != sk:
        st.query_params[_QP_KEY] = sk
