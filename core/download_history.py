from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st
from integrations.supabase_client import get_supabase_client
from postgrest.exceptions import APIError
from core.constants import TABLE_DOWNLOAD_HISTORY

DOWNLOAD_HISTORY_BUCKET = str(
    os.getenv("DOWNLOAD_HISTORY_BUCKET", "download-history") or "download-history"
).strip()


def _current_user_id() -> str | None:
    user = st.session_state.get("user")
    if isinstance(user, dict):
        return user.get("id")
    return None


def _safe_file_name(name: str) -> str:
    base = Path(str(name or "")).name.strip()
    if not base:
        return "download.bin"
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".", " ") else "_" for ch in base)


def _upload_history_artifact(
    supabase,
    user_id: str,
    file_name: str,
    mime: str,
    data: bytes | None,
) -> tuple[str, str] | None:
    if not data:
        return None
    safe_name = _safe_file_name(file_name)
    now = datetime.now(timezone.utc)
    path = (
        f"{user_id}/{now.strftime('%Y/%m/%d')}/"
        f"{now.strftime('%H%M%S')}_{uuid4().hex[:10]}_{safe_name}"
    )
    file_opts = {"upsert": "false"}
    if mime:
        file_opts["content-type"] = str(mime)
    try:
        supabase.storage.from_(DOWNLOAD_HISTORY_BUCKET).upload(path, data, file_opts)
        return DOWNLOAD_HISTORY_BUCKET, path
    except Exception as e:
        print(f"[download_history] upload artifact failed: {e}")
        return None


def _insert_history_with_fallback(supabase, entry: dict[str, Any]) -> None:
    """
    兼容旧表结构：
    - 先尝试写入包含新字段的记录
    - 若目标库缺少新列，则回退到旧字段写入
    """
    try:
        supabase.table(TABLE_DOWNLOAD_HISTORY).insert(entry).execute()
        return
    except APIError as e:
        msg = f"{e.code}:{e.message}".lower()
        # 新列不存在时回退旧结构
        if "column" not in msg and "does not exist" not in msg:
            raise
    legacy = {
        "user_id": entry.get("user_id"),
        "page": entry.get("page"),
        "source": entry.get("source"),
        "title": entry.get("title"),
        "file_name": entry.get("file_name"),
        "mime": entry.get("mime"),
        "size_kb": entry.get("size_kb", 0),
    }
    supabase.table(TABLE_DOWNLOAD_HISTORY).insert(legacy).execute()


def add_download_history(
    *,
    page: str,
    source: str,
    title: str,
    file_name: str,
    mime: str,
    data: bytes | None,
    request_payload: dict[str, Any] | None = None,
):
    """
    Add a download record to Supabase and persist downloadable artifact when data exists.
    """
    user_id = _current_user_id()
    if not user_id:
        print("Warning: add_download_history called but no user logged in.")
        return  # Anonymous users don't save history

    try:
        supabase = get_supabase_client()
        artifact = _upload_history_artifact(
            supabase=supabase,
            user_id=user_id,
            file_name=file_name,
            mime=mime,
            data=data,
        )
        entry = {
            "user_id": user_id,
            "page": page,
            "source": source,
            "title": title,
            "file_name": file_name,
            "mime": mime,
            "size_kb": round(len(data) / 1024, 1) if data is not None else 0,
            "artifact_bucket": artifact[0] if artifact else None,
            "artifact_path": artifact[1] if artifact else None,
            "request_payload": request_payload or {},
        }
        _insert_history_with_fallback(supabase, entry)
    except APIError as e:
        print(f"Supabase API Error in add_download_history: {e.code} - {e.message}")
    except Exception as e:
        print(f"Unexpected error in add_download_history: {e}")


def get_download_history() -> list[dict]:
    """
    Fetch download history from Supabase for current user.
    """
    user_id = _current_user_id()
    if not user_id:
        return []

    try:
        supabase = get_supabase_client()
        # Fetch latest 20 records
        response = (
            supabase.table(TABLE_DOWNLOAD_HISTORY)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        return response.data
    except APIError as e:
        print(f"Supabase API Error in get_download_history: {e.code} - {e.message}")
        return []
    except Exception as e:
        print(f"Unexpected error in get_download_history: {e}")
        return []


def load_download_history_artifact(item: dict[str, Any]) -> bytes | None:
    """
    读取下载历史关联的文件内容（Storage）。
    """
    if not isinstance(item, dict):
        return None
    path = str(item.get("artifact_path") or "").strip()
    if not path:
        return None
    bucket = str(item.get("artifact_bucket") or DOWNLOAD_HISTORY_BUCKET).strip() or DOWNLOAD_HISTORY_BUCKET
    try:
        supabase = get_supabase_client()
        data = supabase.storage.from_(bucket).download(path)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    except Exception as e:
        print(f"[download_history] load artifact failed: {e}")
    return None
