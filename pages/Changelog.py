import sys
import os

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from app.layout import setup_page, show_user_error
from app.navigation import show_right_nav


setup_page(page_title="版本更新日志", page_icon="📢", require_login=False)

content_col = show_right_nav()
with content_col:
    st.title("📢 版本更新日志")


    def show_changelog():
        """Reads and displays the changelog from CHANGELOG.md"""
        try:
            # Go up one level to find CHANGELOG.md since we are in pages/
            changelog_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "CHANGELOG.md"
            )

            if os.path.exists(changelog_path):
                with open(changelog_path, "r", encoding="utf-8") as f:
                    changelog_content = f.read()
                st.markdown(changelog_content)
            else:
                st.warning("CHANGELOG.md not found.")
        except Exception as e:
            show_user_error("无法加载更新日志。", e)


    show_changelog()
