"""FallbackProvider — 多模型自动降级。"""

from __future__ import annotations

import logging
from typing import Any

from cli.providers.base import LLMProvider

logger = logging.getLogger(__name__)


def _is_retriable(exc: Exception) -> bool:
    """可恢复错误（应 fallback）vs 配置错误（不 fallback）。"""
    # SDK 特定异常
    for mod, cls_names in (
        ("openai", ("RateLimitError", "InternalServerError", "APITimeoutError", "APIConnectionError")),
        ("anthropic", ("RateLimitError", "InternalServerError", "APITimeoutError", "APIConnectionError")),
    ):
        try:
            sdk = __import__(mod)
            for name in cls_names:
                cls = getattr(sdk, name, None)
                if cls and isinstance(exc, cls):
                    return True
        except ImportError:
            pass
    # google-genai ServerError
    try:
        from google.genai import errors as genai_errors

        if isinstance(exc, genai_errors.ServerError):
            return True
    except ImportError:
        pass
    # 通用网络/超时
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    # httpx（openai/anthropic 底层）
    try:
        import httpx

        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return True
    except ImportError:
        pass
    return False


class FallbackProvider(LLMProvider):
    """按优先级依次尝试多个 provider，可恢复错误自动 fallback。"""

    def __init__(self, configs: list[dict[str, Any]], default_id: str, fallback_id: str = ""):
        if fallback_id:
            ids = [default_id, fallback_id]
            self._configs = [c for c in configs if c["id"] in ids]
            self._configs.sort(key=lambda c: ids.index(c["id"]))
        else:
            self._configs = sorted(configs, key=lambda c: c["id"] != default_id)
        self._providers: dict[str, LLMProvider] = {}
        self._active_id = self._configs[0]["id"]
        self.last_fallback_msg: str | None = None

    @property
    def name(self) -> str:
        if self._active_id in self._providers:
            return self._providers[self._active_id].name
        cfg = next(c for c in self._configs if c["id"] == self._active_id)
        return f"{cfg.get('provider_name', '?')} ({cfg.get('model', '?')})"

    def chat(self, messages, tools, system_prompt=""):
        return self._with_fallback(
            lambda p: p.chat(messages, tools, system_prompt),
        )

    def chat_stream(self, messages, tools, system_prompt=""):
        yield from self._stream_with_fallback(messages, tools, system_prompt)

    def _get_provider(self, model_id: str) -> LLMProvider:
        if model_id not in self._providers:
            cfg = next(c for c in self._configs if c["id"] == model_id)
            from cli.__main__ import _create_provider

            provider, err = _create_provider(
                cfg["provider_name"],
                cfg["api_key"],
                cfg.get("model", ""),
                cfg.get("base_url", ""),
            )
            if err:
                raise RuntimeError(err)
            self._providers[model_id] = provider
        return self._providers[model_id]

    def _with_fallback(self, fn):
        self.last_fallback_msg = None
        last_exc = None
        for cfg in self._configs:
            try:
                provider = self._get_provider(cfg["id"])
                self._active_id = cfg["id"]
                return fn(provider)
            except Exception as e:
                if not _is_retriable(e):
                    raise
                last_exc = e
                logger.warning("Provider %s failed: %s, trying next", cfg["id"], e)
                self.last_fallback_msg = f"{cfg['id']} 失败 ({type(e).__name__})，已切换"
        raise last_exc

    def _stream_with_fallback(self, messages, tools, system_prompt):
        self.last_fallback_msg = None
        last_exc = None
        for cfg in self._configs:
            try:
                provider = self._get_provider(cfg["id"])
                self._active_id = cfg["id"]
                # 尝试获取第一个 chunk 来确认连接成功
                stream = provider.chat_stream(messages, tools, system_prompt)
                first = next(stream)
                yield first
                yield from stream
                return
            except StopIteration:
                return
            except Exception as e:
                if not _is_retriable(e):
                    raise
                last_exc = e
                logger.warning("Provider %s stream failed: %s, trying next", cfg["id"], e)
                self.last_fallback_msg = f"{cfg['id']} 失败 ({type(e).__name__})，已切换"
        raise last_exc
