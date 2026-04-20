# -*- coding: utf-8 -*-
"""
威科夫终端读盘室 — 入口。

用法:
    wyckoff                # 启动 TUI
    wyckoff update         # 升级到最新版
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv

# 加载 .env（项目根目录）
load_dotenv()

# 抑制 Streamlit 在非 Streamlit 环境下的全部日志
os.environ["STREAMLIT_LOG_LEVEL"] = "error"
import logging as _logging


def _silence_streamlit():
    for name in list(_logging.Logger.manager.loggerDict):
        if name.startswith("streamlit"):
            lg = _logging.getLogger(name)
            lg.handlers.clear()
            lg.setLevel(_logging.CRITICAL)
            lg.propagate = False


try:
    import streamlit  # noqa: F401
except Exception:
    pass
_silence_streamlit()

# CLI 环境：只显示 CRITICAL，不泄漏 traceback 给用户
import warnings as _warnings
_warnings.filterwarnings("ignore", category=DeprecationWarning)
_logging.basicConfig(level=_logging.CRITICAL)


# ---------------------------------------------------------------------------
# Provider 工厂
# ---------------------------------------------------------------------------

def _create_provider(provider_name: str, api_key: str, model: str = "", base_url: str = ""):
    from cli.providers import PROVIDERS
    import inspect

    cls = PROVIDERS.get(provider_name)
    if cls is None:
        install_hints = {
            "gemini": "pip install google-genai",
            "claude": "pip install anthropic",
            "openai": "pip install openai",
        }
        hint = install_hints.get(provider_name, "")
        return None, f"Provider '{provider_name}' 不可用，请先安装依赖：{hint}"

    kwargs = {"api_key": api_key}
    if model:
        kwargs["model"] = model
    if base_url:
        kwargs["base_url"] = base_url

    sig = inspect.signature(cls.__init__)
    kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

    return cls(**kwargs), None


def _do_update():
    import shutil
    print("正在升级 youngcan-wyckoff-analysis ...")
    pkg = "youngcan-wyckoff-analysis"
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable, "--upgrade", pkg]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", pkg]
    try:
        subprocess.check_call(cmd)
        print("\n升级完成！请重新运行 wyckoff。")
    except subprocess.CalledProcessError as e:
        print(f"\n升级失败: {e}")
    sys.exit(0)


def _get_version() -> str:
    try:
        from importlib.metadata import version
        return version("youngcan-wyckoff-analysis")
    except Exception:
        return "dev"


def main():
    parser = argparse.ArgumentParser(
        prog="wyckoff",
        description="威科夫终端读盘室 — Wyckoff 量价分析 Agent",
    )
    parser.add_argument("-v", "--version", action="version", version=f"wyckoff {_get_version()}")
    parser.add_argument(
        "command", nargs="?", default=None,
        help="子命令: update（升级到最新版）",
    )
    args = parser.parse_args()

    if args.command == "update":
        _do_update()
    elif args.command is not None:
        print(f"未知命令: {args.command}")
        print("可用命令: wyckoff update")
        sys.exit(1)

    # --- 初始化：Auth + Tools + Provider ---
    from cli.tools import ToolRegistry
    tools = ToolRegistry()

    try:
        from cli.auth import restore_session
        session = restore_session()
        if session:
            tools.state.update({
                "user_id": session["user_id"],
                "email": session["email"],
                "access_token": session.get("access_token", ""),
                "refresh_token": session.get("refresh_token", ""),
            })
    except Exception:
        pass

    from core.prompts import CHAT_AGENT_SYSTEM_PROMPT
    system_prompt = CHAT_AGENT_SYSTEM_PROMPT

    state = {
        "provider": None,
        "provider_name": "",
        "model": "",
        "api_key": "",
        "base_url": "",
    }

    try:
        from cli.auth import load_model_config
        saved_config = load_model_config()
        if saved_config and saved_config.get("provider_name") and saved_config.get("api_key"):
            env_key = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(saved_config["provider_name"])
            if env_key:
                os.environ[env_key] = saved_config["api_key"]
            provider, err = _create_provider(
                saved_config["provider_name"], saved_config["api_key"],
                saved_config.get("model", ""), saved_config.get("base_url", ""),
            )
            if not err:
                state.update(saved_config)
                state["provider"] = provider
    except Exception:
        pass

    # --- 启动 TUI ---
    from cli.tui import WyckoffTUI
    app = WyckoffTUI(
        provider=state["provider"],
        tools=tools,
        state=state,
        system_prompt=system_prompt,
    )
    app.run()


if __name__ == "__main__":
    main()
