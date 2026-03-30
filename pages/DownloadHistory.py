import streamlit as st
import os
import sys

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.layout import setup_page
from core.download_history import get_download_history, load_download_history_artifact
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
    history_map = {}
    for idx, item in enumerate(history):
        # Supabase stored 'ts' as ISO string, format it if needed or just use slice
        ts_str = item.get("created_at", "")[:19].replace("T", " ")
        label = f"{ts_str} | {item.get('file_name', '')}"
        history_map[label] = idx
        rows.append(
            {
                "时间": ts_str,
                "页面": item.get("page", ""),
                "数据源": item.get("source", ""),
                "文件名": item.get("file_name", ""),
                "大小(KB)": item.get("size_kb", 0),
                "可重下": "是" if item.get("artifact_path") else "否",
            }
        )

    st.dataframe(rows, width="stretch", height=500, hide_index=True)

    st.markdown("### ♻️ 历史文件重下")
    labels = list(history_map.keys())
    selected_label = st.selectbox("选择一条记录", options=labels)
    selected = history[history_map[selected_label]]
    selected_name = str(selected.get("file_name") or "history_download.bin")
    selected_mime = str(selected.get("mime") or "application/octet-stream")

    artifact_bytes = load_download_history_artifact(selected)
    if artifact_bytes is None:
        st.info("该记录暂不支持直接重下（可能是历史旧数据或未存储文件内容）。")
    else:
        st.download_button(
            "下载该历史文件",
            data=artifact_bytes,
            file_name=selected_name,
            mime=selected_mime,
            type="primary",
            width="stretch",
        )
