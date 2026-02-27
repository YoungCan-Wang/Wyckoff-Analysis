from __future__ import annotations

import streamlit as st
from integrations.supabase_client import get_supabase_client
from postgrest.exceptions import APIError
from core.constants import TABLE_DOWNLOAD_HISTORY


def _current_user_id() -> str | None:
    user = st.session_state.get("user")
    if isinstance(user, dict):
        return user.get("id")
    return None


def add_download_history(
    *,
    page: str,
    source: str,
    title: str,
    file_name: str,
    mime: str,
    data: bytes,
):
    """
    Add a download record to Supabase.
    Note: 'data' (file content) is NOT stored to save bandwidth/storage.
    """
    user_id = _current_user_id()
    if not user_id:
        print("Warning: add_download_history called but no user logged in.")
        return  # Anonymous users don't save history

    try:
        supabase = get_supabase_client()
        entry = {
            "user_id": user_id,
            "page": page,
            "source": source,
            "title": title,
            "file_name": file_name,
            "mime": mime,
            "size_kb": round(len(data) / 1024, 1) if data is not None else 0,
        }
        print(f"Attempting to insert download history for user {user_id}...")
        supabase.table(TABLE_DOWNLOAD_HISTORY).insert(entry).execute()
        print("Successfully inserted download history.")
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
