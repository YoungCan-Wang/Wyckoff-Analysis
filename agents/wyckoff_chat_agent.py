"""
Wyckoff 对话 Agent — 基于 Google ADK 的智能投研助手。

使用 ADK LlmAgent 定义，工具列表来自 agents/chat_tools.py，
系统 Prompt 来自 core/prompts.py。

支持两种模型后端：
  - Gemini（默认）：通过子类化 Gemini 注入 genai.Client，不写入 os.environ
  - OpenAI 兼容（Longcat / DeepSeek / 智谱 等）：通过 ADK LiteLlm 适配器桥接

用法:
    # Gemini
    agent = create_agent(model="gemini-2.0-flash", api_key="AIza...")
    # OpenAI 兼容
    agent = create_agent(
        provider="openai", model="LongCat-Flash-Chat",
        api_key="ak_xxx", base_url="https://api.longcat.chat/openai",
    )
"""

from __future__ import annotations

import logging

from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.genai import Client, types

from agents.chat_tools import WYCKOFF_TOOLS
from core.prompts import CHAT_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# 默认模型
DEFAULT_CHAT_MODEL = "gemini-2.0-flash"


def _build_gemini_model(model: str, api_key: str, base_url: str = "") -> Gemini:
    """
    构建绑定了指定 API Key 的 Gemini 实例。

    通过覆盖 cached_property 将预构建的 genai.Client 注入实例，
    ADK 后续所有 LLM 调用都走这个 Client，不再读 os.environ。

    Parameters
    ----------
    base_url : 可选代理地址，留空使用 Google 官方端点。
    """
    gemini = Gemini(model=model)
    client_kwargs: dict = {"api_key": api_key}
    if base_url:
        client_kwargs["http_options"] = {"base_url": base_url.rstrip("/")}
    client = Client(**client_kwargs)
    # 注入到 Pydantic 模型的 __dict__，覆盖 cached_property 的懒加载
    object.__setattr__(gemini, "api_client", client)
    return gemini


def _build_litellm_model(model: str, api_key: str, base_url: str = ""):
    """
    构建 ADK LiteLlm 实例，桥接 OpenAI 兼容 API。

    Parameters
    ----------
    model : 模型名，如 "LongCat-Flash-Chat"
    api_key : 供应商 API Key
    base_url : OpenAI 兼容端点，如 "https://api.longcat.chat/openai"
    """
    from google.adk.models.lite_llm import LiteLlm

    # litellm 约定：openai/model_name（如果模型名本身没有 provider 前缀）
    litellm_model = f"openai/{model}" if "/" not in model else model

    kwargs: dict = {"model": litellm_model, "api_key": api_key}
    if base_url:
        kwargs["api_base"] = base_url.rstrip("/")

    return LiteLlm(**kwargs)


def create_agent(
    *,
    model: str = DEFAULT_CHAT_MODEL,
    api_key: str = "",
    base_url: str = "",
    provider: str = "gemini",
) -> LlmAgent:
    """
    创建 Wyckoff 对话 Agent 实例。

    Parameters
    ----------
    model : 模型名称
    api_key : 对应供应商的 API Key
    base_url : 可选代理/端点地址
    provider : 供应商标识，"gemini" 走原生 Gemini，其余走 LiteLlm 桥接

    Returns
    -------
    配置好的 LlmAgent 实例
    """
    is_gemini = provider == "gemini"

    if is_gemini:
        llm_model = _build_gemini_model(model, api_key, base_url) if api_key else model
    else:
        llm_model = _build_litellm_model(model, api_key, base_url)

    # thinking 仅 Gemini 支持，非 Gemini 不传（否则报错）
    extra_kwargs: dict = {}
    if is_gemini:
        extra_kwargs["generate_content_config"] = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                include_thoughts=True,
            ),
        )

    agent = LlmAgent(
        name="wyckoff_advisor",
        model=llm_model,
        instruction=CHAT_AGENT_SYSTEM_PROMPT,
        tools=WYCKOFF_TOOLS,
        output_key="last_response",
        **extra_kwargs,
    )
    logger.info(
        "Created Wyckoff chat agent: provider=%s, model=%s, tools=%d, key_injected=%s, base_url=%s",
        provider,
        model,
        len(WYCKOFF_TOOLS),
        bool(api_key),
        base_url or "(default)",
    )
    return agent
