import streamlit as st
import os
import sys

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.layout import setup_page
from core.download_history import get_download_history
from app.navigation import show_right_nav


setup_page(page_title="下载历史", page_icon="🕘")

content_col = show_right_nav()
with content_col:
    st.title("🕘 下载历史（最近 20 条）")


    history = get_download_history()
    if not history:
        st.info("暂无下载记录。")
        st.stop()

    rows = []
    for item in history:
        # Supabase stored 'ts' as ISO string, format it if needed or just use slice
        ts_str = item.get("created_at", "")[:19].replace("T", " ")
        rows.append(
            {
                "时间": ts_str,
                "页面": item.get("page", ""),
                "数据源": item.get("source", ""),
                "文件名": item.get("file_name", ""),
                "大小(KB)": item.get("size_kb", 0),
            }
        )

    st.dataframe(rows, width="stretch", height=500, hide_index=True)

    st.caption(
        "注：出于节省存储成本考虑，目前仅保留下载记录元数据，不支持直接重新下载历史文件。如需文件请重新执行查询。"
    )
