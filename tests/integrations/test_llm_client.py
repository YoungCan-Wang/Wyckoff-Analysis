"""integrations/llm_client.py 的 LiteLLM 开关测试。"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestLiteLLMSwitch:
    """验证 LITELLM_ENABLED 环境变量路由逻辑。"""

    def test_litellm_disabled_by_default(self):
        """不设 LITELLM_ENABLED 时，不走 LiteLLM。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LITELLM_ENABLED", None)
            with patch("integrations.llm_client._call_gemini", return_value="native reply") as mock_native:
                from integrations.llm_client import call_llm

                result = call_llm(
                    provider="gemini",
                    model="gemini-3.1-flash-lite-preview",
                    api_key="fake-key",
                    system_prompt="test",
                    user_message="hello",
                )
                assert result == "native reply"
                mock_native.assert_called_once()

    def test_litellm_enabled_routes_to_litellm(self):
        """LITELLM_ENABLED=1 且无 images 时，走 LiteLLM。"""
        with (
            patch.dict(os.environ, {"LITELLM_ENABLED": "1"}),
            patch(
                "integrations.llm_adapter.call_llm_via_litellm",
                return_value="litellm reply",
            ) as mock_litellm,
        ):
            from integrations.llm_client import call_llm

            result = call_llm(
                provider="gemini",
                model="gemini-3.1-flash-lite-preview",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
            )
            assert result == "litellm reply"
            mock_litellm.assert_called_once()

    def test_litellm_enabled_true_string(self):
        """LITELLM_ENABLED=true 也应生效。"""
        with (
            patch.dict(os.environ, {"LITELLM_ENABLED": "true"}),
            patch(
                "integrations.llm_adapter.call_llm_via_litellm",
                return_value="litellm reply",
            ),
        ):
            from integrations.llm_client import call_llm

            result = call_llm(
                provider="deepseek",
                model="deepseek-chat",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
            )
            assert result == "litellm reply"

    def test_litellm_enabled_with_images_falls_back(self):
        """LITELLM_ENABLED=1 但带 images 时，降级为原生实现。"""
        with patch.dict(os.environ, {"LITELLM_ENABLED": "1"}):
            with patch("integrations.llm_client._call_gemini", return_value="native with images") as mock_native:
                from integrations.llm_client import call_llm

                result = call_llm(
                    provider="gemini",
                    model="gemini-3.1-flash-lite-preview",
                    api_key="fake-key",
                    system_prompt="test",
                    user_message="hello",
                    images=[b"fake_image_bytes"],
                )
                assert result == "native with images"
                mock_native.assert_called_once()

    def test_litellm_import_error_falls_back(self):
        """LiteLLM 未安装时，降级为原生实现。"""
        with (
            patch.dict(os.environ, {"LITELLM_ENABLED": "1"}),
            patch(
                "integrations.llm_adapter.call_llm_via_litellm",
                side_effect=ImportError("No module named 'litellm'"),
            ),
            patch("integrations.llm_client._call_gemini", return_value="fallback reply") as mock_native,
        ):
            from integrations.llm_client import call_llm

            result = call_llm(
                provider="gemini",
                model="gemini-3.1-flash-lite-preview",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
            )
            assert result == "fallback reply"
            mock_native.assert_called_once()

    def test_validation_still_works_with_litellm(self):
        """即使 LITELLM_ENABLED=1，空 api_key 仍然报错。"""
        with patch.dict(os.environ, {"LITELLM_ENABLED": "1"}):
            from integrations.llm_client import call_llm

            with pytest.raises(ValueError, match="API Key 未配置"):
                call_llm(
                    provider="gemini",
                    model="test",
                    api_key="",
                    system_prompt="test",
                    user_message="hello",
                )

    def test_unsupported_provider_still_raises(self):
        """不支持的 provider 仍然报错，即使 LiteLLM 开关开启。"""
        with patch.dict(os.environ, {"LITELLM_ENABLED": "1"}):
            from integrations.llm_client import call_llm

            with pytest.raises(ValueError, match="不支持的供应商"):
                call_llm(
                    provider="unknown_provider",
                    model="test",
                    api_key="fake-key",
                    system_prompt="test",
                    user_message="hello",
                )


class TestGeminiTruncationHandling:
    @staticmethod
    def _install_fake_google_genai(response):
        google_mod = ModuleType("google")
        genai_mod = ModuleType("google.genai")
        types_mod = ModuleType("google.genai.types")

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.models = SimpleNamespace(generate_content=MagicMock(return_value=response))

        class FakeConfig:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        genai_mod.Client = FakeClient
        types_mod.GenerateContentConfig = FakeConfig
        genai_mod.types = types_mod
        google_mod.genai = genai_mod
        return {
            "google": google_mod,
            "google.genai": genai_mod,
            "google.genai.types": types_mod,
        }

    def test_gemini_truncation_can_return_text_when_allowed(self):
        from integrations.llm_client import _call_gemini

        response = SimpleNamespace(
            text='{"decision":"BUY","reason":"ok","risk":"low","confidence":0.8}',
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                total_token_count=120,
            ),
        )
        fake_modules = self._install_fake_google_genai(response)
        with patch.dict(sys.modules, fake_modules, clear=False):
            out = _call_gemini(
                model="gemini-pro-latest",
                api_key="fake-key",
                system_prompt="test",
                user_message="hello",
                images=None,
                timeout=30,
                max_output_tokens=256,
                allow_truncated_text=True,
                base_url="",
            )
        assert '"decision":"BUY"' in out

    def test_gemini_truncation_still_raises_by_default(self):
        from integrations.llm_client import _call_gemini

        response = SimpleNamespace(
            text='{"decision":"BUY"}',
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                total_token_count=120,
            ),
        )
        fake_modules = self._install_fake_google_genai(response)
        with (
            patch.dict(sys.modules, fake_modules, clear=False),
            patch("integrations.llm_client.time.sleep", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="Gemini 调用失败"):
                _call_gemini(
                    model="gemini-pro-latest",
                    api_key="fake-key",
                    system_prompt="test",
                    user_message="hello",
                    images=None,
                    timeout=30,
                    max_output_tokens=256,
                    allow_truncated_text=False,
                    base_url="",
                )
