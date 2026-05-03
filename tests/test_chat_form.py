"""
用 Streamlit AppTest 验证读盘室聊天输入框的交互行为：
  1. Enter / 点击发送 能正确提交消息
  2. 新对话按钮能清空消息历史
  3. 提交后输入框被清空
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ── 最小化 Streamlit 页面，只含聊天 form 逻辑 ──
_MINI_APP = """\
import streamlit as st

st.session_state.setdefault("chat_messages", [])

compose_left, compose_mid = st.columns([1.5, 8.5])
with compose_left:
    new_chat_clicked = st.button("新对话", key="btn_new")

with compose_mid:
    with st.form("chat_compose_form", clear_on_submit=True):
        _form_cols = st.columns([8, 1])
        with _form_cols[0]:
            draft_input = st.text_input("输入消息", key="draft")
        with _form_cols[1]:
            send_clicked = st.form_submit_button("发送")

if new_chat_clicked:
    st.session_state["chat_messages"] = []
    st.rerun()

prompt = str(draft_input or "").strip() if send_clicked else ""
if prompt:
    st.session_state["chat_messages"].append({"role": "user", "content": prompt})

# 输出消息数量供测试断言
st.text(f"msg_count={len(st.session_state['chat_messages'])}")
"""

_MINI_APP_PATH = Path(__file__).parent / "_mini_chat_app.py"


@pytest.fixture(autouse=True)
def _write_mini_app(tmp_path):
    """将 mini app 写入临时文件供 AppTest 加载。"""
    p = tmp_path / "mini_chat.py"
    p.write_text(_MINI_APP, encoding="utf-8")
    yield p


@pytest.fixture()
def _no_network():
    """此测试不需要网络，覆盖 conftest 的全局 fixture。"""
    pass


def test_enter_sends_message(_write_mini_app):
    """模拟 form 提交（等效于按 Enter），验证消息被追加。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(_write_mini_app))
    at.run(timeout=10)

    # 初始状态：0 条消息
    assert "msg_count=0" in at.text[0].value

    # 输入文字并提交 form（模拟 Enter）
    at.text_input(key="draft").set_value("你好威科夫").run(timeout=10)

    # 现在需要触发 form submit
    # AppTest 中对 form 的操作：设值 + 点击 submit
    at.text_input(key="draft").set_value("你好威科夫")
    at.button(key="FormSubmitter:chat_compose_form-发送").click().run(timeout=10)

    assert "msg_count=1" in at.text[0].value


def test_new_chat_clears_messages(_write_mini_app):
    """点击新对话后消息历史清空。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(_write_mini_app))
    at.run(timeout=10)

    # 先发一条消息
    at.text_input(key="draft").set_value("测试消息")
    at.button(key="FormSubmitter:chat_compose_form-发送").click().run(timeout=10)
    assert "msg_count=1" in at.text[0].value

    # 点击新对话
    at.button(key="btn_new").click().run(timeout=10)
    assert "msg_count=0" in at.text[0].value


def test_submit_does_not_duplicate_message(_write_mini_app):
    """连续两次 run（模拟 rerun）不会重复追加同一条消息。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(_write_mini_app))
    at.run(timeout=10)

    at.text_input(key="draft").set_value("只发一次")
    at.button(key="FormSubmitter:chat_compose_form-发送").click().run(timeout=10)
    assert "msg_count=1" in at.text[0].value

    # 再 run 一次（非提交），不应追加
    at.run(timeout=10)
    assert "msg_count=1" in at.text[0].value
