# -*- coding: utf-8 -*-
"""
威科夫终端读盘室 — Textual TUI。

全屏布局：上方可滚动聊天区 + 下方固定输入框。
"""
from __future__ import annotations

import json
import time
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Input, RichLog, Static


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class ChatLog(RichLog):
    DEFAULT_CSS = """
    ChatLog {
        background: $surface;
        scrollbar-size: 1 1;
    }
    """


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        dock: top;
        height: 1;
        background: $primary-background;
        color: $text-muted;
        padding: 0 1;
    }
    """


# ---------------------------------------------------------------------------
# 交互式输入状态机（/login, /model）
# ---------------------------------------------------------------------------

class _InputState:
    """管理多步交互式输入流程。"""
    NONE = "none"
    LOGIN_EMAIL = "login_email"
    LOGIN_PASSWORD = "login_password"
    MODEL_PROVIDER = "model_provider"
    MODEL_KEY = "model_key"
    MODEL_NAME = "model_name"
    MODEL_URL = "model_url"


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------

class WyckoffTUI(App):
    """威科夫终端读盘室。"""

    TITLE = "Wyckoff 读盘室"
    CSS = """
    Screen {
        layout: vertical;
    }
    #chat-log {
        height: 1fr;
        border: round $primary;
        margin: 0 1;
    }
    #chat-input {
        dock: bottom;
        margin: 0 1 0 1;
    }
    Footer {
        background: $primary-background;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", show=True),
        Binding("ctrl+l", "clear_chat", "清屏", show=True),
        Binding("ctrl+n", "new_chat", "新对话", show=True),
    ]

    def __init__(
        self,
        provider: Any = None,
        tools: Any = None,
        state: dict | None = None,
        system_prompt: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._provider = provider
        self._tools = tools
        self._state = state or {}
        self._system_prompt = system_prompt
        self._messages: list[dict] = []
        self._session_tokens = {"input": 0, "output": 0, "rounds": 0}
        self._busy = False
        # 交互式输入状态
        self._input_mode = _InputState.NONE
        self._input_buf: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield StatusBar(self._build_status_text(), id="status-bar")
        yield ChatLog(id="chat-log", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="问我关于股票的任何问题... (/help 查看命令)", id="chat-input")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        log.write(Text.from_markup(
            "[bold]Wyckoff 读盘室[/bold]\n"
            "[dim]直接输入问题开始对话  ·  /help 查看命令  ·  Ctrl+C 退出[/dim]\n"
        ))
        if not self._provider:
            log.write(Text.from_markup(
                "[yellow]⚠ 未配置模型，请输入 /model 设置[/yellow]\n"
            ))
        self.query_one("#chat-input", Input).focus()

    def _build_status_text(self) -> str:
        parts = ["Wyckoff CLI"]
        prov = self._state.get("provider_name", "")
        model = self._state.get("model", "")
        if prov and model:
            parts.append(f"{prov}:{model}")
        email = self._tools.state.get("email", "") if self._tools else ""
        parts.append(email or "未登录")
        t = self._session_tokens
        if t["rounds"] > 0:
            parts.append(f"Token: {t['input']+t['output']:,}")
        return " · ".join(parts)

    def _update_status(self) -> None:
        self.query_one("#status-bar", StatusBar).update(self._build_status_text())

    # ----- 输入处理 -----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        inp = event.input
        inp.clear()

        # 交互式多步输入
        if self._input_mode != _InputState.NONE:
            self._handle_interactive_input(text)
            return

        if not text:
            return

        log = self.query_one("#chat-log", ChatLog)

        # 斜杠命令
        if text.startswith("/"):
            self._handle_command(text)
            return

        if self._busy:
            log.write(Text.from_markup("[yellow]正在处理上一条消息，请稍候...[/yellow]"))
            return

        if not self._provider:
            log.write(Text.from_markup("[yellow]⚠ 未配置模型，请先输入 /model[/yellow]"))
            return

        # 用户消息
        log.write(Text(""))
        log.write(Text.from_markup(f"[bold cyan]❯[/bold cyan] {text}"))
        self._messages.append({"role": "user", "content": text})
        self._run_agent()

    # ----- 斜杠命令 -----

    def _handle_command(self, raw: str) -> None:
        cmd = raw.lower().split()[0]
        log = self.query_one("#chat-log", ChatLog)

        if cmd in ("/quit", "/exit", "/q"):
            self.exit()
        elif cmd == "/clear":
            self.action_clear_chat()
        elif cmd == "/new":
            self.action_new_chat()
        elif cmd == "/help":
            log.write(Text.from_markup(
                "\n[bold]可用命令[/bold]\n"
                "  /model   — 配置模型供应商\n"
                "  /login   — 登录\n"
                "  /logout  — 退出登录\n"
                "  /token   — Token 用量\n"
                "  /new     — 新对话 (Ctrl+N)\n"
                "  /clear   — 清屏 (Ctrl+L)\n"
                "  /quit    — 退出 (Ctrl+C)\n"
            ))
        elif cmd == "/token":
            t = self._session_tokens
            if t["rounds"] == 0:
                log.write(Text.from_markup("[dim]本次会话尚无 Token 记录[/dim]"))
            else:
                log.write(Text.from_markup(
                    f"\n[bold]Token 用量[/bold]  "
                    f"输入: {t['input']:,}  输出: {t['output']:,}  "
                    f"合计: {t['input']+t['output']:,}  轮次: {t['rounds']}"
                ))
        elif cmd == "/login":
            self._start_login()
        elif cmd == "/logout":
            self._do_logout()
        elif cmd == "/model":
            self._start_model_config()
        else:
            log.write(Text.from_markup(f"[red]未知命令: {raw}[/red]，/help 查看"))

    # ----- /login 交互 -----

    def _start_login(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        log.write(Text.from_markup("\n[bold]登录[/bold]"))
        log.write(Text.from_markup("  输入邮箱（留空取消）："))
        inp.placeholder = "邮箱..."
        self._input_mode = _InputState.LOGIN_EMAIL
        self._input_buf = {}

    def _do_logout(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        if self._tools:
            try:
                from cli.auth import logout
                logout()
            except Exception:
                pass
            self._tools.state.update({"user_id": "", "email": "", "access_token": "", "refresh_token": ""})
        log.write(Text.from_markup("[green]已退出登录[/green]"))
        self._update_status()

    # ----- /model 交互 -----

    def _start_model_config(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        log.write(Text.from_markup(
            "\n[bold]模型配置[/bold]\n"
            "  供应商：gemini / openai / claude\n"
            "  输入供应商名（留空取消）："
        ))
        inp.placeholder = "供应商: gemini / openai / claude"
        self._input_mode = _InputState.MODEL_PROVIDER
        self._input_buf = {}

    # ----- 交互式输入状态机 -----

    def _handle_interactive_input(self, text: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        mode = self._input_mode

        if not text:
            # 留空取消
            log.write(Text.from_markup("[dim]已取消[/dim]"))
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            return

        if mode == _InputState.LOGIN_EMAIL:
            self._input_buf["email"] = text
            log.write(Text.from_markup(f"  邮箱: {text}"))
            log.write(Text.from_markup("  输入密码："))
            inp.placeholder = "密码..."
            inp.password = True
            self._input_mode = _InputState.LOGIN_PASSWORD

        elif mode == _InputState.LOGIN_PASSWORD:
            inp.password = False
            log.write(Text.from_markup("  密码: ****"))
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            # 执行登录
            try:
                from cli.auth import login
                session = login(self._input_buf["email"], text)
                self._tools.state.update({
                    "user_id": session["user_id"],
                    "email": session["email"],
                    "access_token": session.get("access_token", ""),
                    "refresh_token": session.get("refresh_token", ""),
                })
                log.write(Text.from_markup(f"  [green]✓ 登录成功 ({session['email']})[/green]"))
                self._update_status()
            except Exception as e:
                err = str(e)
                if "Invalid login" in err or "invalid" in err.lower():
                    log.write(Text.from_markup("  [red]邮箱或密码错误[/red]"))
                else:
                    log.write(Text.from_markup(f"  [red]登录失败: {err}[/red]"))

        elif mode == _InputState.MODEL_PROVIDER:
            prov = text.strip().lower()
            if prov not in ("gemini", "openai", "claude"):
                log.write(Text.from_markup(f"  [red]不支持: {prov}[/red]"))
                self._input_mode = _InputState.NONE
                inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
                return
            self._input_buf["provider"] = prov
            log.write(Text.from_markup(f"  供应商: {prov}"))
            log.write(Text.from_markup("  输入 API Key："))
            inp.placeholder = "API Key..."
            inp.password = True
            self._input_mode = _InputState.MODEL_KEY

        elif mode == _InputState.MODEL_KEY:
            inp.password = False
            self._input_buf["api_key"] = text
            log.write(Text.from_markup("  API Key: ****"))
            default_models = {"gemini": "gemini-2.0-flash", "openai": "gpt-4o", "claude": "claude-sonnet-4-20250514"}
            default = default_models.get(self._input_buf["provider"], "")
            log.write(Text.from_markup(f"  输入模型名（留空使用 {default}）："))
            inp.placeholder = f"模型名，默认 {default}"
            self._input_mode = _InputState.MODEL_NAME

        elif mode == _InputState.MODEL_NAME:
            default_models = {"gemini": "gemini-2.0-flash", "openai": "gpt-4o", "claude": "claude-sonnet-4-20250514"}
            model = text or default_models.get(self._input_buf["provider"], "")
            self._input_buf["model"] = model
            log.write(Text.from_markup(f"  模型: {model}"))
            log.write(Text.from_markup("  输入 Base URL（留空使用默认）："))
            inp.placeholder = "Base URL（可选）"
            self._input_mode = _InputState.MODEL_URL

        elif mode == _InputState.MODEL_URL:
            self._input_buf["base_url"] = text
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            # 创建 provider
            self._apply_model_config()

    def _apply_model_config(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        buf = self._input_buf
        try:
            from cli.__main__ import _create_provider
            provider, err = _create_provider(
                buf["provider"], buf["api_key"],
                buf.get("model", ""), buf.get("base_url", ""),
            )
            if err:
                log.write(Text.from_markup(f"  [red]{err}[/red]"))
                return
            self._provider = provider
            self._state.update({
                "provider": provider,
                "provider_name": buf["provider"],
                "api_key": buf["api_key"],
                "model": buf.get("model", ""),
                "base_url": buf.get("base_url", ""),
            })
            # 持久化
            from cli.auth import save_model_config
            save_model_config({
                "provider_name": buf["provider"],
                "api_key": buf["api_key"],
                "model": buf.get("model", ""),
                "base_url": buf.get("base_url", ""),
            })
            import os
            env_key = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(buf["provider"])
            if env_key:
                os.environ[env_key] = buf["api_key"]
            log.write(Text.from_markup(f"  [green]✓ 已切换到 {provider.name}[/green]"))
            self._update_status()
        except Exception as e:
            log.write(Text.from_markup(f"  [red]配置失败: {e}[/red]"))

    # ----- Agent 执行（后台 Worker）-----

    @work(thread=True, exclusive=True)
    def _run_agent(self) -> None:
        self._busy = True
        log = self.query_one("#chat-log", ChatLog)

        def _write(renderable):
            self.call_from_thread(log.write, renderable)

        def _scroll():
            self.call_from_thread(log.scroll_end, animate=False)

        _write(Text.from_markup("  [dim]思考中...[/dim]"))

        total_input = 0
        total_output = 0
        t_start = time.monotonic()

        try:
            from cli.agent import MAX_TOOL_ROUNDS

            for round_idx in range(MAX_TOOL_ROUNDS):
                text_buf = ""
                thinking_buf = ""
                tool_calls = None
                round_usage = {}
                last_thinking_update = 0

                for chunk in self._provider.chat_stream(
                    self._messages, self._tools.schemas(), self._system_prompt
                ):
                    if chunk["type"] == "thinking_delta":
                        thinking_buf += chunk["text"]
                        now = time.monotonic()
                        if now - last_thinking_update > 0.3:
                            _write(Text(f"  💭 {thinking_buf[-200:]}", style="dim italic"))
                            _scroll()
                            last_thinking_update = now

                    elif chunk["type"] == "text_delta":
                        text_buf += chunk["text"]

                    elif chunk["type"] == "tool_calls":
                        tool_calls = chunk["tool_calls"]
                        partial = chunk.get("text", "")
                        if partial and not text_buf:
                            text_buf = partial

                    elif chunk["type"] == "usage":
                        round_usage = chunk

                total_input += round_usage.get("input_tokens", 0)
                total_output += round_usage.get("output_tokens", 0)

                if thinking_buf:
                    _write(Text(f"  💭 推理完成 ({len(thinking_buf)} 字)", style="dim"))

                if tool_calls:
                    assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
                    if text_buf:
                        assistant_msg["content"] = text_buf
                    self._messages.append(assistant_msg)

                    for call in tool_calls:
                        name = call["name"]
                        args = call["args"]
                        call_id = call["id"]
                        display = self._tools.display_name(name)

                        _write(Text.from_markup(
                            f"  [yellow]⚡ {display}[/yellow] [dim]{_brief_args(args)}[/dim]"
                        ))
                        _scroll()

                        t_tool = time.monotonic()
                        result = self._tools.execute(name, args)
                        elapsed_tool = time.monotonic() - t_tool

                        if isinstance(result, dict) and result.get("error"):
                            _write(Text.from_markup(
                                f"  [red]✗ {display}[/red] [dim]{elapsed_tool:.1f}s {str(result['error'])[:80]}[/dim]"
                            ))
                        else:
                            _write(Text.from_markup(
                                f"  [green]✓ {display}[/green] [dim]{elapsed_tool:.1f}s[/dim]"
                            ))
                        _scroll()

                        self._messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": name,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        })
                    continue

                # 纯文本回答
                self._messages.append({"role": "assistant", "content": text_buf})
                if text_buf:
                    _write(Markdown(text_buf))
                    _scroll()

                elapsed = time.monotonic() - t_start
                self._session_tokens["input"] += total_input
                self._session_tokens["output"] += total_output
                self._session_tokens["rounds"] += 1

                usage_parts = []
                if total_input or total_output:
                    usage_parts.append(f"↑{total_input:,} ↓{total_output:,}")
                usage_parts.append(f"{elapsed:.1f}s")
                _write(Text.from_markup(f"  [dim]{' · '.join(usage_parts)}[/dim]"))
                _scroll()
                self.call_from_thread(self._update_status)
                break
            else:
                _write(Text.from_markup("[yellow](工具调用轮次超限)[/yellow]"))

        except Exception as e:
            _write(Text.from_markup(f"[red]Agent 错误: {e}[/red]"))
            while self._messages and self._messages[-1].get("role") != "user":
                self._messages.pop()
            if self._messages:
                self._messages.pop()

        finally:
            self._busy = False

    # ----- Actions -----

    def action_clear_chat(self) -> None:
        self.query_one("#chat-log", ChatLog).clear()

    def action_new_chat(self) -> None:
        self._messages.clear()
        log = self.query_one("#chat-log", ChatLog)
        log.clear()
        log.write(Text.from_markup("[green]新对话已开始[/green]\n"))


def _brief_args(args: dict) -> str:
    if not args:
        return ""
    s = ", ".join(f"{k}={v}" for k, v in args.items())
    return s[:60] + ("..." if len(s) > 60 else "")
