"""
统一 LLM 调用层：支持 Gemini，可选 OpenAI 兼容接口。
入参：provider、model、api_key、system_prompt、user_message；可选 base_url（OpenAI 兼容）。

可选 LiteLLM 路由（LITELLM_ENABLED=1）：
  - 自动路由到 integrations/llm_adapter.py 的 LiteLLM 实现
  - 所有现有调用方（step3, step4, single_stock_logic, rag_veto）零改动自动切换
  - images 参数暂不支持 LiteLLM 路径，带 images 时自动降级为原生实现
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# 多厂商：Gemini + OpenAI 兼容（OpenAI/智谱/Minimax/DeepSeek/Qwen/火山引擎）
SUPPORTED_PROVIDERS = (
    "1route",
    "gemini",
    "openai",
    "zhipu",
    "minimax",
    "deepseek",
    "qwen",
    "volcengine",
)
# OpenAI 兼容接口的默认 base_url（可被调用方 base_url 覆盖）
OPENAI_COMPATIBLE_BASE_URLS = {
    "1route": "https://www.1route.dev/v1",
    "openai": "https://api.openai.com/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "minimax": "https://api.minimax.chat/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
}
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"
GEMINI_MODELS = (
    DEFAULT_GEMINI_MODEL,
    "gemini-2.5-flash-lite",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
)
GEMINI_MAX_OUTPUT_TOKENS_DEFAULT = 32768
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAY = 2.0

# 供应商展示名（供 UI selectbox 的 format_func 使用）
PROVIDER_LABELS: dict[str, str] = {
    "1route": "1Route（推荐）",
    "gemini": "Gemini",
    "openai": "OpenAI",
    "zhipu": "智谱",
    "minimax": "Minimax",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "volcengine": "火山引擎",
}


def get_provider_credentials(provider: str) -> tuple[str, str, str]:
    """
    根据 provider 从 Streamlit session_state 和环境变量取 (api_key, model, base_url)。

    优先 session_state，其次环境变量，Gemini 有模型兜底。
    """
    import streamlit as st

    key_suffix = provider.lower()
    env_prefix = key_suffix.upper()
    api_key = (st.session_state.get(f"{key_suffix}_api_key") or "").strip() or os.getenv(
        f"{env_prefix}_API_KEY", ""
    ).strip()
    model = (st.session_state.get(f"{key_suffix}_model") or "").strip() or os.getenv(f"{env_prefix}_MODEL", "").strip()
    base_url = (st.session_state.get(f"{key_suffix}_base_url") or "").strip() or os.getenv(
        f"{env_prefix}_BASE_URL", ""
    ).strip()
    if not base_url and provider in OPENAI_COMPATIBLE_BASE_URLS:
        base_url = (OPENAI_COMPATIBLE_BASE_URLS.get(provider, "") or "").strip()
    if not model and provider == "gemini":
        model = st.session_state.get("gemini_model") or DEFAULT_GEMINI_MODEL
    if not model and provider == "1route":
        model = "gpt-5.5"
    return (api_key, model or "", base_url)


# Gemini finish_reason 在不同 SDK/模型下可能是字符串或数字枚举，这里统一兜底识别“输出被截断”。
_GEMINI_TRUNCATION_REASONS = {
    "MAX_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "TOKEN_LIMIT",
    "LENGTH",
    "2",  # 兼容部分枚举输出
}


def call_llm(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    *,
    images: list | None = None,
    base_url: str | None = None,
    timeout: int = 120,
    max_output_tokens: int | None = None,
    allow_truncated_text: bool = False,
) -> str:
    """
    调用大模型，返回回复文本。

    Args:
        provider: 供应商，当前仅支持 "gemini"。
        model: 模型名，如 gemini-3.1-flash-lite-preview。
        api_key: 对应供应商的 API Key。
        system_prompt: 系统提示词（Alpha 投委会等）。
        user_message: 用户消息（拼装好的 OHLCV 等）。
        images: 可选图片列表（PIL Image 或 bytes），仅部分模型支持。
        base_url: 可选代理地址，Gemini 和 OpenAI 兼容均支持。
        timeout: 请求超时秒数。
        allow_truncated_text: 当供应商返回“输出被截断”但已有非空文本时，是否直接返回文本。

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

    # ── Phase 2: LiteLLM 路由开关 ──────────────────────────────
    # 当 LITELLM_ENABLED=1 时，走 LiteLLM 统一适配层。
    # 带 images 参数时降级为原生实现（LiteLLM 暂不支持 Gemini 多模态）。
    if os.environ.get("LITELLM_ENABLED", "").strip() in ("1", "true", "yes"):
        if not images:
            try:
                from integrations.llm_adapter import call_llm_via_litellm

                logger.info(
                    "[llm] LITELLM_ENABLED=1, routing to LiteLLM: provider=%s model=%s",
                    provider,
                    model,
                )
                return call_llm_via_litellm(
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    base_url=base_url,
                    timeout=timeout,
                    max_output_tokens=max_output_tokens,
                    allow_truncated_text=allow_truncated_text,
                )
            except ImportError:
                logger.warning("[llm] LiteLLM not installed, falling back to native implementation")
        else:
            logger.info("[llm] LITELLM_ENABLED=1 but images present, using native Gemini implementation")
    # ── /Phase 2 ────────────────────────────────────────────────

    if provider == "gemini":
        return _call_gemini(
            model=model,
            api_key=api_key.strip(),
            system_prompt=system_prompt,
            user_message=user_message,
            images=images,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            allow_truncated_text=allow_truncated_text,
            base_url=(base_url or "").strip(),
        )
    if provider in OPENAI_COMPATIBLE_BASE_URLS:
        base = (base_url or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "") or "").rstrip("/")
        if not base:
            raise ValueError(f"未配置 {provider} 的 base_url")
        return _call_openai_compatible(
            base_url=base,
            api_key=api_key.strip(),
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )
    raise ValueError(f"未实现的供应商: {provider}")


def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_output_tokens: int | None,
) -> str:
    """通过 OpenAI 兼容的 /chat/completions 接口调用（OpenAI/智谱/DeepSeek/Qwen 等）。"""
    import requests

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    max_tokens = int(max_output_tokens) if max_output_tokens is not None else 8192
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max(256, max_tokens),
        "temperature": 0.4,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI 兼容接口 HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI 兼容接口返回无 choices")
    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raise RuntimeError("OpenAI 兼容接口返回内容为空")
    return text


def _call_gemini(
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    images: list | None,
    timeout: int,
    max_output_tokens: int | None,
    allow_truncated_text: bool,
    base_url: str = "",
) -> str:
    from google import genai
    from google.genai import types

    # 包含 timeout（+ 可选代理地址）的 HTTP 参数传入 Client
    http_opts: dict = {"timeout": timeout * 1000}
    if base_url:
        http_opts["base_url"] = base_url.rstrip("/")
    client = genai.Client(api_key=api_key, http_options=http_opts)

    resolved_max_tokens = int(max_output_tokens) if max_output_tokens is not None else GEMINI_MAX_OUTPUT_TOKENS_DEFAULT

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.4,
        top_p=0.95,
        top_k=40,
        max_output_tokens=max(1024, resolved_max_tokens),
    )

    contents = [user_message]
    if images:
        contents.extend(images)

    last_err: Exception | None = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            if response is None:
                raise RuntimeError("Gemini 返回空响应")

            text = getattr(response, "text", None) or ""
            if not text and getattr(response, "candidates", None):
                parts = []
                for c in response.candidates:
                    content = getattr(c, "content", None)
                    if not content:
                        continue
                    for p in getattr(content, "parts", []) or []:
                        t = getattr(p, "text", None)
                        if t:
                            parts.append(t)
                text = "".join(parts).strip()

            if not text:
                raise RuntimeError("Gemini 返回内容为空")

            finish_reason = ""
            if getattr(response, "candidates", None) and len(response.candidates) > 0:
                fr = getattr(response.candidates[0], "finish_reason", "")
                if fr is not None:
                    # 枚举处理
                    finish_reason = getattr(fr, "name", str(fr))

            usage = getattr(response, "usage_metadata", None)
            prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
            completion_tokens = getattr(usage, "candidates_token_count", None) if usage else None
            total_tokens = getattr(usage, "total_token_count", None) if usage else None
            finish_reason_norm = finish_reason.strip().upper()
            print(
                "[llm] gemini model={} finish_reason={} prompt_tokens={} completion_tokens={} total_tokens={} max_output_tokens={}".format(
                    model,
                    finish_reason or "unknown",
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    config.max_output_tokens,
                )
            )
            if finish_reason_norm in _GEMINI_TRUNCATION_REASONS:
                if allow_truncated_text and text.strip():
                    print("[llm] gemini truncation tolerated: using returned text because allow_truncated_text=1")
                    return text
                raise RuntimeError(
                    f"Gemini 输出被截断(finish_reason={finish_reason or 'unknown'})，请缩短输入或提升输出上限后重试"
                )
            return text
        except Exception as e:
            last_err = e
            if attempt >= GEMINI_MAX_RETRIES:
                break
            sleep_s = GEMINI_RETRY_DELAY * (2 ** (attempt - 1))
            sleep_s = min(sleep_s, 30.0)
            print(f"[llm] gemini attempt {attempt}/{GEMINI_MAX_RETRIES} failed: {e}; retry in {sleep_s:.1f}s")
            time.sleep(sleep_s)

    raise RuntimeError(f"Gemini 调用失败: {last_err}")
