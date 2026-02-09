# -*- coding: utf-8 -*-
"""
统一 LLM 调用层：支持 Gemini，可选 OpenAI 兼容接口。
入参：provider、model、api_key、system_prompt、user_message；可选 base_url（OpenAI 兼容）。
"""
from __future__ import annotations

from typing import Optional

# 首期仅实现 Gemini；后续可增加 openai
SUPPORTED_PROVIDERS = ("gemini",)
GEMINI_MODELS = (
    "gemini-3-pro-preview",
    "gemini-3-flash-preview"
)


def call_llm(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    *,
    base_url: Optional[str] = None,
    timeout: int = 120,
) -> str:
    """
    调用大模型，返回回复文本。

    Args:
        provider: 供应商，当前仅支持 "gemini"。
        model: 模型名，如 gemini-2.0-flash。
        api_key: 对应供应商的 API Key。
        system_prompt: 系统提示词（Alpha 投委会等）。
        user_message: 用户消息（拼装好的 OHLCV 等）。
        base_url: 仅 OpenAI 兼容时使用，Gemini 忽略。
        timeout: 请求超时秒数。

    Returns:
        模型回复的纯文本。

    Raises:
        ValueError: provider 不支持或参数无效。
        RuntimeError: 调用失败或返回为空。
    """
    if not api_key or not api_key.strip():
        raise ValueError("API Key 未配置，请先在设置页录入。")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"不支持的供应商: {provider}，当前仅支持: {SUPPORTED_PROVIDERS}")

    if provider == "gemini":
        return _call_gemini(
            model=model,
            api_key=api_key.strip(),
            system_prompt=system_prompt,
            user_message=user_message,
            timeout=timeout,
        )
    # 后续可加: elif provider == "openai": return _call_openai(...)
    raise ValueError(f"未实现的供应商: {provider}")


def _call_gemini(
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
) -> str:
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    generative_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=system_prompt,
    )
    generation_config = {
        "temperature": 0.4,
        "top_p": 0.95,
        "top_k": 40,
        "max_output_tokens": 8192,
    }
    response = generative_model.generate_content(
        user_message,
        generation_config=generation_config,
        request_options={"timeout": timeout},
    )
    if response is None:
        raise RuntimeError("Gemini 返回空响应")
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini 返回内容为空")
    return text
