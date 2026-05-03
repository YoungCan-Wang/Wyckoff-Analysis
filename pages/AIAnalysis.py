"""AI 分析页：单股大师模式（本地深度分析 + 图表生成）。"""

import streamlit as st

from app.layout import setup_page
from app.navigation import show_right_nav
from app.single_stock_logic import render_single_stock_page
from integrations.llm_client import (
    GEMINI_MODELS,
    OPENAI_COMPATIBLE_BASE_URLS,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    get_provider_credentials,
)

AI_ANALYSIS_DEFAULT_FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/4ef56ec3-fb84-4eb4-b4d9-775ae7de69ff"


def _resolve_ai_analysis_feishu_webhook() -> str:
    """
    AI 分析页专用 webhook 口径：
    1) 优先用户在设置页保存的 feishu_webhook
    2) 用户未设置时，回退到 AI 分析页专用兜底地址
    """
    user_webhook = str(st.session_state.get("feishu_webhook") or "").strip()
    return user_webhook or AI_ANALYSIS_DEFAULT_FEISHU_WEBHOOK


_get_provider_credentials = get_provider_credentials


def _render_single_stock_page_compat(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    feishu_webhook: str,
) -> None:
    """
    兼容旧版本 single_stock_logic.render_single_stock_page(provider, model, api_key)
    与新版本 render_single_stock_page(..., base_url=...)。
    """
    try:
        render_single_stock_page(
            provider,
            model,
            api_key,
            base_url=base_url,
            feishu_webhook=feishu_webhook,
        )
    except TypeError as e:
        err = str(e)
        if "unexpected keyword argument 'feishu_webhook'" in err:
            try:
                render_single_stock_page(
                    provider,
                    model,
                    api_key,
                    base_url=base_url,
                )
                return
            except TypeError as e2:
                if "unexpected keyword argument 'base_url'" not in str(e2):
                    raise
                render_single_stock_page(provider, model, api_key)
                return
        if "unexpected keyword argument 'base_url'" in err:
            render_single_stock_page(provider, model, api_key)
            return
        raise


setup_page(page_title="大师模式", page_icon="🤖")

content_col = show_right_nav()
with content_col:
    st.title("🤖 大师模式")
    st.markdown(
        "单股深度分析 — 七位虚拟投委会大师联合会诊（默认近 320 个交易日）。 批量研报请到 [读盘室](/) 用对话触发。"
    )

    effective_feishu_webhook = _resolve_ai_analysis_feishu_webhook()
    provider = st.selectbox(
        "API 供应商",
        options=list(SUPPORTED_PROVIDERS),
        format_func=lambda x: PROVIDER_LABELS.get(x, x),
        key="ai_provider_single",
    )
    api_key, default_model, base_url = _get_provider_credentials(provider)
    model = st.text_input(
        "模型",
        value=default_model or (GEMINI_MODELS[0] if provider == "gemini" else ""),
        key="ai_model_single",
    ).strip()
    effective_single_base_url = base_url
    if provider in OPENAI_COMPATIBLE_BASE_URLS:
        single_base_url_input = st.text_input(
            "Base URL（可选）",
            value=base_url,
            key=f"ai_single_base_url_{provider}",
            help="留空时自动使用该供应商默认 Base URL。",
        ).strip()
        effective_single_base_url = single_base_url_input or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "")
    if not api_key:
        st.warning(f"需要 {PROVIDER_LABELS.get(provider, provider)} API Key，请先在设置页录入或配置环境变量。")
        st.page_link("pages/Settings.py", label="前往设置", icon="⚙️")
        st.stop()
    if provider == "gemini":
        st.caption("常用模型示例：" + "、".join(GEMINI_MODELS[:6]))
    _render_single_stock_page_compat(
        provider,
        model or default_model or (GEMINI_MODELS[0] if provider == "gemini" else ""),
        api_key,
        effective_single_base_url,
        effective_feishu_webhook,
    )
