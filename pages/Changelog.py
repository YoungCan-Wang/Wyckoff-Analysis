import sys
import os

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from navigation import show_right_nav


st.set_page_config(
    page_title="版本更新日志",
    page_icon="📢",
    layout="wide"
)

st.title("📢 版本更新日志")

show_right_nav()

def show_changelog():
    """Reads and displays the changelog from CHANGELOG.md"""
    try:
        # Go up one level to find CHANGELOG.md since we are in pages/
        changelog_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "CHANGELOG.md")
        
        if os.path.exists(changelog_path):
            with open(changelog_path, "r", encoding="utf-8") as f:
                changelog_content = f.read()
            st.markdown(changelog_content)
        else:
            st.warning("CHANGELOG.md not found.")
    except Exception as e:
        st.error(f"无法加载更新日志: {e}")

show_changelog()
