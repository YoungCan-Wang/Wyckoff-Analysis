"""
LiteLLM 适配层 — 为 Agent 层提供统一的 LLM 调用接口。

当前：
  - call_llm_via_litellm() 作为 integrations/llm_client.call_llm() 的可选替代
  - 支持现有全部 provider: Gemini / OpenAI / DeepSeek / Qwen / Zhipu / Volcengine / Minimax
  - 内部通过 LiteLLM 自动路由，无需手动切分 Gemini vs OpenAI-compat 逻辑

可通过 LITELLM_ENABLED=1 启用，作为 llm_client.call_llm() 的路由替代
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider → LiteLLM model prefix 映射
# ---------------------------------------------------------------------------

# LiteLLM 对各 provider 使用不同前缀来路由请求：
#   gemini → "gemini/<model>"
#   openai → "openai/<model>"  或直接 "<model>"
#   deepseek → "deepseek/<model>"
#   其他 OpenAI-compat → "openai/<model>" + base_url
# 参考: https://docs.litellm.ai/docs/providers
PROVIDER_PREFIX_MAP: dict[str, str] = {
    "gemini": "gemini",
    "openai": "openai",
    "deepseek": "deepseek",
    "qwen": "openai",  # DashScope OpenAI-compatible
    "zhipu": "openai",  # 智谱 OpenAI-compatible
    "volcengine": "openai",  # 火山引擎 OpenAI-compatible
    "minimax": "openai",  # Minimax OpenAI-compatible
}

# 复用 llm_client.py 的 URL 表，避免重复维护
from integrations.llm_client import OPENAI_COMPATIBLE_BASE_URLS as DEFAULT_BASE_URLS

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------
DEFAULT_MAX_OUTPUT_TOKENS = 32768
DEFAULT_TEMPERATURE = 0.4
DEFAULT_TOP_P = 0.95


def _resolve_litellm_model(provider: str, model: str) -> str:
    """将 (provider, model) 转换为 LiteLLM 能识别的 model 字符串。"""
    provider = (provider or "gemini").strip().lower()
    prefix = PROVIDER_PREFIX_MAP.get(provider, "openai")
    # 如果 model 已经带有前缀（如 "gemini/gemini-3.1-..."），直接用
    if "/" in model:
        return model
    return f"{prefix}/{model}"


def _resolve_base_url(provider: str, base_url: str | None) -> str | None:
    """解析 base_url：优先用户传入，其次默认表，Gemini 不需要 base_url。"""
    if base_url:
        return base_url
    provider = (provider or "gemini").strip().lower()
    if provider == "gemini":
        return None
    return DEFAULT_BASE_URLS.get(provider)


def call_llm_via_litellm(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    *,
    base_url: str | None = None,
    timeout: int = 120,
    max_output_tokens: int | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float = DEFAULT_TOP_P,
    allow_truncated_text: bool = False,
) -> str:
    """
    通过 LiteLLM 调用任意 provider 的 LLM。

    签名与 integrations/llm_client.call_llm() 对齐（去掉 images 参数），
    可作为 drop-in replacement。

    Raises:
        ImportError: LiteLLM 未安装
        RuntimeError: LLM 调用失败
    """
    try:
        import litellm
    except ImportError as e:
        raise ImportError(
            "LiteLLM is required for the agent layer. Install it with: pip install litellm>=1.40.0"
        ) from e

    litellm_model = _resolve_litellm_model(provider, model)
    resolved_base_url = _resolve_base_url(provider, base_url)
    max_tokens = max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
    _ = allow_truncated_text

    logger.info(
        "LiteLLM call: provider=%s model=%s litellm_model=%s base_url=%s max_tokens=%d",
        provider,
        model,
        litellm_model,
        resolved_base_url or "(default)",
        max_tokens,
    )

    # Gemini 需要通过环境变量传递 API key
    env_backup = {}
    provider_lower = (provider or "gemini").strip().lower()
    if provider_lower == "gemini" and api_key:
        env_backup["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = api_key

    try:
        response = litellm.completion(
            model=litellm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            api_key=api_key if provider_lower != "gemini" else None,
            base_url=resolved_base_url,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
        )
    except Exception as e:
        raise RuntimeError(f"LiteLLM call failed ({litellm_model}): {e}") from e
    finally:
        # 恢复环境变量
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise RuntimeError(f"LiteLLM returned empty response ({litellm_model})")

    logger.info(
        "LiteLLM response: model=%s tokens_in=%s tokens_out=%s",
        litellm_model,
        getattr(response.usage, "prompt_tokens", "?"),
        getattr(response.usage, "completion_tokens", "?"),
    )
    return content.strip()
