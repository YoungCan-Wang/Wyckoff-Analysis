from __future__ import annotations

from datetime import datetime
import uuid
import streamlit as st


def _ensure_state():
    if "download_history" not in st.session_state:
        st.session_state.download_history = []


def add_download_history(
    *,
    page: str,
    source: str,
    title: str,
    file_name: str,
    mime: str,
    data: bytes,
):
    _ensure_state()

    entry = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "page": page,
        "source": source,
        "title": title,
        "file_name": file_name,
        "mime": mime,
        "data": data,
        "size_kb": round(len(data) / 1024, 1) if data is not None else 0,
    }

    def same(a: dict, b: dict) -> bool:
        return (
            a.get("page") == b.get("page")
            and a.get("source") == b.get("source")
            and a.get("file_name") == b.get("file_name")
        )

    st.session_state.download_history = [
        x for x in st.session_state.download_history if not same(x, entry)
    ]
    st.session_state.download_history.insert(0, entry)
    st.session_state.download_history = st.session_state.download_history[:10]


def get_download_history() -> list[dict]:
    _ensure_state()
    return list(st.session_state.download_history)

