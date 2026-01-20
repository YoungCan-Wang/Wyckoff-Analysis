import streamlit as st
import os
import sys

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth_component import check_auth, login_form
from download_history import get_download_history
from navigation import show_right_nav


st.set_page_config(
    page_title="下载历史",
    page_icon="🕘",
    layout="wide",
)

# === Auth Check ===
if not check_auth():
    # 使用空布局，避免显示侧边栏和其他干扰元素
    empty_container = st.empty()
    with empty_container.container():
        login_form()
    st.stop()

st.title("🕘 下载历史（最近 20 条）")


show_right_nav()


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

st.dataframe(rows, use_container_width=True, height=500, hide_index=True)

st.caption("注：出于节省存储成本考虑，目前仅保留下载记录元数据，不支持直接重新下载历史文件。如需文件请重新执行查询。")

