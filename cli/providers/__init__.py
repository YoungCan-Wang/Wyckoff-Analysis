"""Provider 注册 — 延迟导入，缺少 SDK 时跳过对应 provider。"""

from cli.providers.base import LLMProvider

PROVIDERS: dict[str, type[LLMProvider]] = {}

try:
    from cli.providers.gemini import GeminiProvider

    PROVIDERS["gemini"] = GeminiProvider
except ImportError:
    pass

try:
    from cli.providers.claude import ClaudeProvider

    PROVIDERS["claude"] = ClaudeProvider
except ImportError:
    pass

try:
    from cli.providers.openai import OpenAIProvider

    PROVIDERS["openai"] = OpenAIProvider
except ImportError:
    pass
