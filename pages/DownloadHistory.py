import sys
import os

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from download_history import get_download_history
from navigation import show_right_nav


st.set_page_config(
    page_title="下载历史",
    page_icon="🕘",
    layout="wide",
)


st.title("🕘 下载历史（最近 10 条）")


show_right_nav()


history = get_download_history()
if not history:
    st.info("暂无下载记录。")
    st.stop()

rows = []
for item in history:
    rows.append(
        {
            "时间": item.get("ts", ""),
            "页面": item.get("page", ""),
            "数据源": item.get("source", ""),
            "文件名": item.get("file_name", ""),
            "大小(KB)": item.get("size_kb", 0),
        }
    )

st.dataframe(rows, use_container_width=True, height=320)

st.markdown("### 📥 重新下载")
for item in history:
    label = f"{item.get('ts','')} | {item.get('page','')} | {item.get('file_name','')}"
    st.download_button(
        label=label,
        data=item.get("data", b""),
        file_name=item.get("file_name", "download.bin"),
        mime=item.get("mime", "application/octet-stream"),
        use_container_width=True,
        key=f"rehit::{item.get('id','')}",
    )

