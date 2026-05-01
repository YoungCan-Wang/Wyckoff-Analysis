# -*- coding: utf-8 -*-
"""
Wyckoff 智能对话 — 首页。

基于 Google ADK 的对话式投研助手，用户可通过自然语言与 Wyckoff Agent 交互，
触发系统所有能力：筛选、诊断、研报、策略、跟踪等。
"""
import json
import os
import time

import streamlit as st
from dotenv import load_dotenv

from app.auth_component import logout
from app.layout import setup_page
from app.navigation import show_right_nav

load_dotenv()

# ── 页面配置 ──
setup_page(page_title="Wyckoff 读盘室", page_icon="💬")

# ── 用户信息 & 登出 & 供应商切换 ──
with st.sidebar:
    if st.session_state.get("user"):
        st.caption(
            f"当前用户: {st.session_state.user.get('email') if isinstance(st.session_state.user, dict) else ''}"
        )
        if st.button("退出登录"):
            logout()
    st.divider()

    # ── 读盘室供应商快捷切换 ──
    from integrations.llm_client import SUPPORTED_PROVIDERS, PROVIDER_LABELS

    st.session_state.setdefault("chat_provider", "gemini")
    _current_provider = st.session_state.get("chat_provider", "gemini")
    _provider_idx = (
        list(SUPPORTED_PROVIDERS).index(_current_provider)
        if _current_provider in SUPPORTED_PROVIDERS
        else 0
    )
    _new_provider = st.selectbox(
        "🗣️ 读盘室供应商",
        options=list(SUPPORTED_PROVIDERS),
        index=_provider_idx,
        format_func=lambda x: PROVIDER_LABELS.get(x, x),
        help="选择驱动读盘室对话的大模型供应商",
    )
    if _new_provider != _current_provider:
        st.session_state["chat_provider"] = _new_provider
        # 清掉旧 agent，下面会用新 provider 重建
        st.session_state.pop("chat_manager", None)
        st.session_state.pop("chat_messages", None)
        st.rerun()
    st.divider()

content_col = show_right_nav()

# ── 工具函数名 → 中文描述 ──
TOOL_DISPLAY_NAMES = {
    "search_stock_by_name": "搜索股票",
    "analyze_stock": "个股分析",
    "portfolio": "持仓",
    "get_market_overview": "大盘概览",
    "screen_stocks": "漏斗筛选",
    "generate_ai_report": "AI 研报生成",
    "generate_strategy_decision": "策略决策",
    "query_history": "历史查询",
    "update_portfolio": "调仓操作",
    "run_backtest": "回测",
}

# ── 欢迎语 ──
_WELCOME_TEXT = (
    "我是理查德·威科夫。我只看供需关系和主力行为，不听故事。\n\n"
    "你可以直接跟我说：\n"
    "- 🔍 **\"搜一下宁德时代\"** — 我帮你查\n"
    "- 🕵️ **\"帮我看看 000001\"** — 我给它做一次完整的量价体检\n"
    "- 🪓 **\"审判我的持仓\"** — 我会逐一下达去留判决\n"
    "- 🌊 **\"今天大盘水温怎么样\"** — 我告诉你现在适合进攻还是蛰伏\n"
    "- 🧭 **\"有什么机会\"** — 我从四千多只股票里帮你扫出来\n"
    "- 📈 **\"600519 最近走势\"** — 我调它的 K 线给你看\n"
    "- 📝 **\"深度审一下这几只\"** — 我把它们分成三个阵营\n"
    "- ⚔️ **\"该买什么该卖什么\"** — 我给你下作战指令\n"
    "- 🎯 **\"之前推荐的表现怎么样\"** — 我翻一翻战绩\n\n"
    "*说吧，你想看什么？*"
)

# 当系统提示词/工具策略有重要变更时，提升版本号以触发会话内 manager 重建
CHAT_AGENT_VERSION = "2026-05-01-portfolio-tool-source-v1"

def _get_chat_config() -> tuple[str, str, str, str]:
    """
    获取读盘室对话配置：(provider, api_key, model, base_url)。

    根据 session_state['chat_provider'] 决定从哪组配置中读取凭证。
    """
    provider = (
        str(st.session_state.get("chat_provider") or "").strip() or "gemini"
    )

    if provider == "gemini":
        api_key = (
            str(st.session_state.get("gemini_api_key") or "").strip()
            or os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        )
        model = (
            str(st.session_state.get("gemini_model") or "").strip()
            or os.getenv("GEMINI_MODEL", "").strip()
            or "gemini-2.0-flash"
        )
        base_url = (
            str(st.session_state.get("gemini_base_url") or "").strip()
            or os.getenv("GEMINI_BASE_URL", "").strip()
        )
    else:
        # 非 Gemini：读对应 provider 的 key/model/base_url
        from integrations.llm_client import OPENAI_COMPATIBLE_BASE_URLS

        key_prefix = provider.lower()
        env_prefix = key_prefix.upper()
        api_key = (
            str(st.session_state.get(f"{key_prefix}_api_key") or "").strip()
            or os.getenv(f"{env_prefix}_API_KEY", "").strip()
        )
        model = (
            str(st.session_state.get(f"{key_prefix}_model") or "").strip()
            or os.getenv(f"{env_prefix}_MODEL", "").strip()
        )
        base_url = (
            str(st.session_state.get(f"{key_prefix}_base_url") or "").strip()
            or os.getenv(f"{env_prefix}_BASE_URL", "").strip()
            or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "")
        )

    return provider, api_key, model, base_url


def _init_chat_manager():
    """初始化或获取已有的 ChatSessionManager。"""
    user = st.session_state.get("user") or {}
    user_id = str(user.get("id", "") if isinstance(user, dict) else "").strip()

    # 检测 user_id 变化或 agent 版本升级 → 强制重建 manager
    prev_uid = st.session_state.get("_chat_manager_user_id", "")
    prev_ver = st.session_state.get("_chat_manager_agent_version", "")
    if (prev_uid and prev_uid != user_id) or (prev_ver and prev_ver != CHAT_AGENT_VERSION):
        st.session_state.pop("chat_manager", None)
        st.session_state.pop("chat_messages", None)

    if "chat_manager" not in st.session_state:
        provider, api_key, model, base_url = _get_chat_config()
        if not api_key:
            return None

        from agents.wyckoff_chat_agent import create_agent
        from agents.session_manager import ChatSessionManager

        agent = create_agent(
            provider=provider, model=model, api_key=api_key, base_url=base_url,
        )
        mgr = ChatSessionManager(
            user_id=user_id or "anonymous",
            agent=agent,
            api_key=api_key,
        )
        st.session_state["chat_manager"] = mgr
        st.session_state["_chat_manager_user_id"] = user_id
        st.session_state["_chat_manager_agent_version"] = CHAT_AGENT_VERSION

    return st.session_state["chat_manager"]


def _inject_chat_css() -> None:
    """注入聊天界面专属样式。"""
    st.markdown(
        """
<style>
.chat-shell {
    border: 1px solid #e4e7ec;
    border-radius: 16px;
    padding: 0.85rem 1rem 0.3rem;
    background:
      radial-gradient(circle at top right, #f7fbff 0%, #ffffff 42%),
      linear-gradient(180deg, #ffffff 0%, #fbfcff 100%);
}

.chat-header-card {
    border: 1px solid #e4e7ec;
    border-radius: 14px;
    background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
    padding: 0.7rem 0.85rem;
    margin-bottom: 0.65rem;
}

.chat-header-title {
    margin: 0;
    font-size: 1.2rem;
    font-weight: 750;
    color: #101828;
    line-height: 1.25;
}

.chat-header-desc {
    margin-top: 0.24rem;
    color: #475467;
    font-size: 0.86rem;
}

.chat-chip-row {
    display: flex;
    gap: 0.45rem;
    flex-wrap: wrap;
    margin-top: 0.46rem;
}

.chat-chip {
    display: inline-flex;
    align-items: center;
    padding: 0.16rem 0.5rem;
    border-radius: 999px;
    border: 1px solid #d0d5dd;
    color: #344054;
    background: #fff;
    font-size: 0.74rem;
    line-height: 1.25;
}

[data-testid="stChatMessage"] {
    border-radius: 12px;
    margin-bottom: 0.3rem;
    padding: 0.48rem 0.64rem;
}

[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
    background: #f3f7ff;
    border: 1px solid #dbe8ff;
}

[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
    background: #f8fafc;
    border: 1px solid #e4e7ec;
}

[data-testid="stChatInput"] {
    position: sticky;
    bottom: 0;
    background: linear-gradient(180deg, rgba(255,255,255,0.72) 0%, #ffffff 30%);
    padding-top: 0.45rem;
    z-index: 10;
}

.chat-stream-status {
    font-size: 0.82rem;
    color: #475467;
    background: #f2f4f7;
    border: 1px solid #e4e7ec;
    border-radius: 10px;
    padding: 0.38rem 0.55rem;
    margin-bottom: 0.45rem;
}

.compose-row button {
    border-radius: 10px !important;
    min-height: 2.5rem !important;
}
</style>
        """,
        unsafe_allow_html=True,
    )


def _render_header_card(provider: str, model: str) -> None:
    from integrations.llm_client import PROVIDER_LABELS

    provider_label = PROVIDER_LABELS.get(provider, provider)
    model_text = (model or "gemini-2.0-flash").strip()
    uid = st.session_state.get("_chat_manager_user_id", "") or "anonymous"
    uid_preview = str(uid)[:8] + "***" if uid and uid != "anonymous" else "anonymous"
    st.markdown(
        (
            '<div class="chat-header-card">'
            '<div class="chat-header-title">💬 Wyckoff 读盘室</div>'
            '<div class="chat-header-desc">与威科夫对话，快速完成盘前研判、持仓审判和机会筛选</div>'
            '<div class="chat-chip-row">'
            f'<span class="chat-chip">供应商: {provider_label}</span>'
            f'<span class="chat-chip">模型: {model_text}</span>'
            f'<span class="chat-chip">会话用户: {uid_preview}</span>'
            '</div>'
            '</div>'
        ),
        unsafe_allow_html=True,
    )
# =====================================================================
# 页面主体
# =====================================================================
with content_col:

    # ── API Key 检查 ──
    _provider, _api_key, _model, _base_url = _get_chat_config()
    if not _api_key:
        st.warning(
            "未检测到 API Key。请前往 **设置页面** 配置后使用。",
            icon="🔑",
        )
        st.page_link("pages/Settings.py", label="前往设置", icon="⚙️")
        st.stop()

    # ── 初始化 ChatSessionManager ──
    manager = _init_chat_manager()
    if manager is None:
        st.error("ChatSessionManager 初始化失败")
        st.stop()

    # ── 消息历史 ──
    if "chat_messages" not in st.session_state:
        st.session_state["chat_messages"] = []

    _inject_chat_css()
    shell = st.container(border=True)
    with shell:
        _render_header_card(provider=_provider, model=_model)

        # ── 聊天区域（固定高度） ──
        chat_area = st.container(height=560)

        with chat_area:
            if not st.session_state["chat_messages"]:
                with st.chat_message("assistant", avatar="assistant"):
                    st.markdown(_WELCOME_TEXT)

            for msg in st.session_state["chat_messages"]:
                avatar = "assistant" if msg["role"] == "assistant" else "user"
                with st.chat_message(msg["role"], avatar=avatar):
                    st.markdown(msg["content"])

        # ── 用户输入 ──
        st.markdown('<div class="compose-row">', unsafe_allow_html=True)
        compose_left, compose_mid, compose_right = st.columns([1.25, 7.2, 1.2])
        with compose_left:
            new_chat_clicked = st.button(
                "🆕 新对话",
                use_container_width=True,
            )
        # form 内只保留一个 submit_button，确保 Enter 键能可靠触发发送
        with compose_mid:
            with st.form("chat_compose_form", clear_on_submit=True):
                _form_cols = st.columns([8, 1])
                with _form_cols[0]:
                    draft_input = st.text_input(
                        "输入消息",
                        label_visibility="collapsed",
                        placeholder="问我关于股票的任何问题...",
                    )
                with _form_cols[1]:
                    send_clicked = st.form_submit_button(
                        "发送",
                        type="primary",
                        use_container_width=True,
                    )
        st.markdown("</div>", unsafe_allow_html=True)

        if new_chat_clicked:
            mgr = st.session_state.get("chat_manager")
            if mgr:
                mgr.new_session()
            st.session_state["chat_messages"] = []
            st.rerun()

        # form 提交（Enter 或点击发送）时读取输入
        prompt = str(draft_input or "").strip() if send_clicked else ""
        if prompt:

            # 显示用户消息
            st.session_state["chat_messages"].append({"role": "user", "content": prompt})

            with chat_area:
                with st.chat_message("user", avatar="user"):
                    st.markdown(prompt)

                # Agent 流式回复
                with st.chat_message("assistant", avatar="assistant"):
                    # 布局：status/thinking 在上方，正文在下方
                    status_container = st.container()
                    response_placeholder = st.empty()
                    stream_status_placeholder = status_container.empty()

                    accumulated_text = ""
                    thinking_text = ""
                    thinking_expander = None
                    thinking_placeholder = None
                    final_response = ""
                    called_tools: list[str] = []
                    seen_tool_calls: set[str] = set()
                    total_input_tokens = 0
                    total_output_tokens = 0
                    t_start = time.monotonic()

                    stream_status_placeholder.markdown(
                        '<div class="chat-stream-status">🧠 正在思考与检索数据...</div>',
                        unsafe_allow_html=True,
                    )

                    for event_type, data in manager.send_message_streaming(prompt):

                        if event_type == "thinking":
                            # 首次出现 thinking 时创建可折叠区域
                            if thinking_expander is None:
                                thinking_expander = status_container.expander(
                                    "💭 推理过程", expanded=True
                                )
                                thinking_placeholder = thinking_expander.empty()
                            thinking_text += data
                            thinking_placeholder.markdown(thinking_text + "▌")

                        elif event_type == "tool_call":
                            tool_name = data.get("name", "unknown")
                            tool_args = data.get("args", {})
                            try:
                                args_sig = json.dumps(
                                    tool_args,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    default=str,
                                )
                            except Exception:
                                args_sig = str(tool_args)
                            call_sig = f"{tool_name}:{args_sig}"
                            if call_sig in seen_tool_calls:
                                continue
                            seen_tool_calls.add(call_sig)
                            display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                            called_tools.append(f"🔧 {display_name}")
                            stream_status_placeholder.markdown(
                                '<div class="chat-stream-status">正在调用工具：'
                                + " · ".join(called_tools[-3:])
                                + "</div>",
                                unsafe_allow_html=True,
                            )

                        elif event_type == "tool_result":
                            tool_name = data.get("name", "unknown")
                            display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                            stream_status_placeholder.markdown(
                                f'<div class="chat-stream-status">✅ 已完成：{display_name}</div>',
                                unsafe_allow_html=True,
                            )

                        elif event_type == "usage":
                            total_input_tokens += data.get("input_tokens", 0)
                            total_output_tokens += data.get("output_tokens", 0)

                        elif event_type == "text_chunk":
                            accumulated_text += data
                            response_placeholder.markdown(accumulated_text + "▌")

                        elif event_type == "done":
                            elapsed = time.monotonic() - t_start
                            # 优先使用 done 携带的完整文本，回退到累积文本
                            final_response = data if data else accumulated_text
                            response_placeholder.markdown(final_response)
                            # 显示耗时和 token
                            usage_parts = []
                            if total_input_tokens or total_output_tokens:
                                usage_parts.append(f"↑{total_input_tokens:,} ↓{total_output_tokens:,}")
                            usage_parts.append(f"{elapsed:.1f}s")
                            stream_status_placeholder.markdown(
                                f'<div class="chat-stream-status" style="text-align:right">{" · ".join(usage_parts)}</div>',
                                unsafe_allow_html=True,
                            )
                            # 收起 thinking 光标
                            if thinking_placeholder and thinking_text:
                                thinking_placeholder.markdown(thinking_text)

                        elif event_type == "error":
                            final_response = f"⚠️ Agent 出错: {data}"
                            response_placeholder.markdown(final_response)
                            stream_status_placeholder.empty()

                    # 保存到消息历史
                    if final_response:
                        st.session_state["chat_messages"].append({
                            "role": "assistant",
                            "content": final_response,
                        })

            st.rerun()
