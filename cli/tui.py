# -*- coding: utf-8 -*-
"""
威科夫终端读盘室 — Textual TUI。

全屏布局：上方可滚动聊天区 + 下方固定输入框。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import deque
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from cli.loop_guard import (
    build_retry_exhausted_warning,
    build_retry_user_message,
    missing_required_tool,
    resolve_turn_expectation,
)
from core.prompts import with_current_time


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

from cli.compaction import TAIL_KEEP as _TAIL_KEEP_DEFAULT, compact_messages, estimate_tokens
from cli.loop_guard import MAX_INCOMPLETE_TOOL_RETRIES as _MAX_INCOMPLETE_TOOL_RETRIES


def _pop_lines(log_widget, n: int) -> None:
    """从 RichLog 底部移除 n 行 strips。"""
    from textual.geometry import Size
    if n > 0 and len(log_widget.lines) >= n:
        del log_widget.lines[-n:]
        log_widget.virtual_size = Size(
            log_widget._widest_line_width, len(log_widget.lines)
        )
        log_widget.refresh()


def _write_counted(log_widget, renderable) -> int:
    """写入 RichLog，并返回实际新增的 visual strips 数。"""
    before = len(log_widget.lines)
    log_widget.write(renderable)
    return max(0, len(log_widget.lines) - before)


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


class SelectorScreen(ModalScreen):
    """模态选择器 — 上下键选择，Enter 确认，Esc 取消。"""

    DEFAULT_CSS = """
    SelectorScreen {
        align: center middle;
    }
    #selector-box {
        width: 60;
        max-height: 16;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    #selector-options {
        height: auto;
        max-height: 12;
    }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, options: list[tuple[str, str]], callback_id: str):
        super().__init__()
        self._options = options
        self._values = [v for v, _ in options]
        self._callback_id = callback_id

    def compose(self) -> ComposeResult:
        with Vertical(id="selector-box"):
            yield OptionList(
                *[Option(label, id=val) for val, label in self._options],
                id="selector-options",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        value = self._values[event.option_index]
        self.dismiss(None)
        self.app._on_selector_choice(self._callback_id, value)

    def action_cancel(self) -> None:
        self.dismiss(None)
        self.app._on_selector_choice(self._callback_id, None)


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ---------------------------------------------------------------------------
# 交互式输入状态机（/login, /model）
# ---------------------------------------------------------------------------

class _InputState:
    """管理多步交互式输入流程。"""
    NONE = "none"
    LOGIN_EMAIL = "login_email"
    LOGIN_PASSWORD = "login_password"
    CONFIG_KEY = "config_key"
    MODEL_ID = "model_id"
    MODEL_PROVIDER = "model_provider"
    MODEL_KEY = "model_key"
    MODEL_NAME = "model_name"
    MODEL_URL = "model_url"


# ---------------------------------------------------------------------------
# 工具确认弹窗
# ---------------------------------------------------------------------------

class ToolConfirmScreen(ModalScreen[dict]):
    """高风险工具执行前的确认弹窗。"""

    DEFAULT_CSS = """
    ToolConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 64;
        max-height: 20;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #confirm-summary {
        color: $text-muted;
        margin-bottom: 1;
    }
    #confirm-options {
        height: auto;
        max-height: 6;
    }
    #confirm-edit {
        display: none;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, tool_name: str, args: dict, display_name: str):
        super().__init__()
        self.tool_name = tool_name
        self.tool_args = args
        self.display_name = display_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(
                f"⚠ [bold]{self.display_name}[/bold] 需要确认",
                id="confirm-title",
            )
            yield Static(self._format_summary(), id="confirm-summary")
            yield OptionList(
                Option("允许一次", id="once"),
                Option("本次会话总是允许", id="always"),
                Option("修改后执行", id="edit"),
                Option("不允许", id="deny"),
                id="confirm-options",
            )
            yield Input(
                value=self._editable_value(),
                placeholder="修改后按 Enter 执行",
                id="confirm-edit",
            )

    def _format_summary(self) -> str:
        if self.tool_name == "exec_command":
            return f"  命令: {self.tool_args.get('command', '')}"
        if self.tool_name == "write_file":
            path = self.tool_args.get("path", "")
            size = len(self.tool_args.get("content", ""))
            return f"  路径: {path}\n  内容: {size} 字符"
        if self.tool_name == "update_portfolio":
            action = self.tool_args.get("action", "")
            code = self.tool_args.get("code", "")
            parts = [f"操作: {action}"]
            if code:
                parts.append(f"代码: {code}")
            shares = self.tool_args.get("shares")
            if shares:
                parts.append(f"股数: {shares}")
            cost = self.tool_args.get("cost_price")
            if cost:
                parts.append(f"成本: {cost}")
            cash = self.tool_args.get("free_cash")
            if cash is not None:
                parts.append(f"现金: {cash}")
            return "  " + "  ".join(parts)
        return f"  {json.dumps(self.tool_args, ensure_ascii=False)}"

    def _editable_value(self) -> str:
        if self.tool_name == "exec_command":
            return self.tool_args.get("command", "")
        if self.tool_name == "write_file":
            return self.tool_args.get("path", "")
        return json.dumps(self.tool_args, ensure_ascii=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id == "edit":
            self.query_one("#confirm-options").display = False
            edit_input = self.query_one("#confirm-edit", Input)
            edit_input.display = True
            edit_input.focus()
        else:
            self.dismiss({"action": event.option_id})

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "confirm-edit":
            return
        modified = dict(self.tool_args)
        if self.tool_name == "exec_command":
            modified["command"] = event.value
        elif self.tool_name == "write_file":
            modified["path"] = event.value
        else:
            try:
                modified = json.loads(event.value)
            except json.JSONDecodeError:
                pass
        self.dismiss({"action": "edit", "modified_args": modified})

    def action_cancel(self) -> None:
        self.dismiss({"action": "deny"})


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
    """

    ENABLE_COMMAND_PALETTE = True
    COMMAND_PALETTE_BINDING = "ctrl+p"
    COMMANDS = set()  # will be populated below after class definition

    BINDINGS = [
        Binding("ctrl+c", "smart_copy", show=False, priority=True),
        Binding("ctrl+q", "quit", "退出", show=False),
        Binding("ctrl+n", "new_chat", "新对话"),
        Binding("ctrl+l", "clear_chat", "清屏"),
    ]

    def __init__(
        self,
        provider: Any = None,
        tools: Any = None,
        state: dict | None = None,
        system_prompt: str = "",
        session_expired: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._provider = provider
        self._tools = tools
        self._state = state or {}
        self._system_prompt = system_prompt
        self._session_expired = session_expired
        self._messages: list[dict] = []
        self._session_tokens = {"input": 0, "output": 0, "rounds": 0}
        self._busy = False
        self._queue: deque[str] = deque()
        self._session_id = uuid.uuid4().hex[:12]
        # 文件日志（记录流式断开等异常）
        self._agent_log = logging.getLogger("wyckoff.agent")
        self._agent_log.setLevel(logging.DEBUG)
        if not self._agent_log.handlers:
            try:
                from core.constants import LOCAL_DB_PATH
                log_path = LOCAL_DB_PATH.parent / "agent.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(str(log_path), encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
                self._agent_log.addHandler(fh)
                self._agent_log.propagate = False
            except Exception:
                pass
        # 后台任务管理
        from cli.background import BackgroundTaskManager
        self._bg_manager = BackgroundTaskManager()
        if self._tools:
            self._tools.set_background_manager(self._bg_manager, self._on_bg_complete)
            self._tools.set_confirm_callback(self._request_tool_confirm)
        # 交互式输入状态
        self._input_mode = _InputState.NONE
        self._input_buf: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield StatusBar(self._build_status_text(), id="status-bar")
        yield ChatLog(id="chat-log", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="问我关于股票的任何问题... (/help 查看命令)", id="chat-input")

    def on_mount(self) -> None:
        # 加载保存的主题
        try:
            from cli.auth import load_config
            saved_theme = load_config().get("theme", "")
            if saved_theme and saved_theme in self.available_themes:
                self.theme = saved_theme
        except Exception:
            pass

        log = self.query_one("#chat-log", ChatLog)
        log.write(Text.from_markup(
            "[bold]Wyckoff 读盘室[/bold]\n"
            "[dim]直接输入问题开始对话  ·  /help 查看命令  ·  Ctrl+P 命令面板  ·  Ctrl+C 复制/退出[/dim]\n"
        ))
        if not self._provider:
            log.write(Text.from_markup(
                "[yellow]⚠ 未配置模型，请输入 /model add 添加[/yellow]\n"
            ))
        if self._session_expired:
            log.write(Text.from_markup(
                "[yellow]⚠ 登录已过期，请输入 /login 重新登录[/yellow]\n"
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

    # ----- 工具确认 -----

    def _request_tool_confirm(self, name: str, args: dict) -> dict:
        """从 worker 线程调用，阻塞直到用户在弹窗中做出选择。"""
        import threading as _th

        event = _th.Event()
        result: list[dict | None] = [None]
        display = self._tools.display_name(name) if self._tools else name

        def _on_dismiss(choice: dict) -> None:
            result[0] = choice
            event.set()

        def _show() -> None:
            self.push_screen(ToolConfirmScreen(name, args, display), _on_dismiss)

        self.call_from_thread(_show)
        event.wait(timeout=120)
        return result[0] or {"action": "deny"}

    # ----- 快捷键动作 -----

    def _save_and_exit(self) -> None:
        if self._messages and self._provider:
            try:
                from cli.memory import save_session_summary
                import threading
                t = threading.Thread(
                    target=save_session_summary,
                    args=(list(self._messages), self._provider),
                    daemon=True,
                )
                t.start()
                t.join(timeout=5)
            except Exception:
                pass
        self.exit()

    def action_quit(self) -> None:
        self._save_and_exit()

    def action_smart_copy(self) -> None:
        """Ctrl+C: 有选中文本 → 复制；无选中 → 退出。"""
        text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)
            self.screen.clear_selection()
            self.notify("已复制", timeout=1)
        else:
            self._save_and_exit()

    def action_switch_model(self) -> None:
        self._switch_model_selector()

    def action_list_models(self) -> None:
        self._list_models()

    def action_add_model(self) -> None:
        self._start_model_add()

    def action_start_login(self) -> None:
        self._start_login()

    def action_do_logout(self) -> None:
        self._do_logout()

    def action_show_token(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        t = self._session_tokens
        if t["rounds"] == 0:
            log.write(Text.from_markup("[dim]本次会话尚无 Token 记录[/dim]"))
        else:
            log.write(Text.from_markup(
                f"\n[bold]Token 用量[/bold]  "
                f"输入: {t['input']:,}  输出: {t['output']:,}  "
                f"合计: {t['input']+t['output']:,}  轮次: {t['rounds']}"
            ))

    def action_switch_theme(self) -> None:
        """打开主题切换器并保存选择。"""
        self.action_change_theme()

    def watch_theme(self, new_theme: str) -> None:
        """主题变化时自动保存。"""
        try:
            from cli.auth import save_config_key
            save_config_key("theme", new_theme)
        except Exception:
            pass

    # ----- Spinner（ChatLog 底部边框） -----

    def _start_spinner(self, label: str = "thinking") -> None:
        self._spinner_label = label
        self._spinner_idx = 0
        log = self.query_one("#chat-log", ChatLog)
        log.border_subtitle = f"{_SPINNER[0]} {label}"
        if not hasattr(self, "_spinner_timer") or self._spinner_timer is None:
            self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    def _stop_spinner(self) -> None:
        if hasattr(self, "_spinner_timer") and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.query_one("#chat-log", ChatLog).border_subtitle = ""

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
        self.query_one("#chat-log", ChatLog).border_subtitle = (
            f"{_SPINNER[self._spinner_idx]} {self._spinner_label}"
        )

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

        if not self._provider:
            log.write(Text.from_markup("[yellow]⚠ 未配置模型，请先输入 /model add[/yellow]"))
            return

        if self._busy:
            self._queue.append(text)
            return

        # 用户消息
        self._send_message(text)

    def _send_message(self, text: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        log.write(Text(""))
        log.write(Text.from_markup(f"[bold cyan]❯[/bold cyan] {text}"))
        # 注入记忆上下文
        try:
            from cli.memory import build_memory_context
            mem_ctx = build_memory_context(text)
            if mem_ctx and mem_ctx not in self._system_prompt:
                self._system_prompt = self._system_prompt.rstrip() + "\n" + mem_ctx
        except Exception:
            pass
        self._messages.append({"role": "user", "content": text})
        self._start_spinner("thinking")
        self._run_agent()

    # ----- 斜杠命令 -----

    def _handle_command(self, raw: str) -> None:
        cmd = raw.lower().split()[0]
        log = self.query_one("#chat-log", ChatLog)

        if cmd in ("/quit", "/exit", "/q"):
            self._save_and_exit()
        elif cmd == "/clear":
            self.action_clear_chat()
        elif cmd == "/new":
            self.action_new_chat()
        elif cmd == "/help":
            from cli.skills import load_skills
            skills = load_skills()
            skill_lines = "".join(
                f"  /{s.name:<11s}— {s.description}\n" for s in skills.values()
            )
            log.write(Text.from_markup(
                "\n[bold]可用命令[/bold]\n"
                "  /model   — 切换模型（list/add/rm/default）\n"
                "  /config  — 数据源配置（tushare_token, tickflow_api_key）\n"
                "  /login   — 登录\n"
                "  /logout  — 退出登录\n"
                "  /token   — Token 用量\n"
                "  /resume  — 恢复历史对话\n"
                "  /new     — 新对话 (Ctrl+N)\n"
                "  /clear   — 清屏 (Ctrl+L)\n"
                "  /quit    — 退出 (Ctrl+Q)\n"
                f"\n[bold]Skills[/bold]\n{skill_lines}"
                "\n[bold]快捷键[/bold]\n"
                "  Ctrl+P   — 命令面板\n"
                "  Ctrl+C   — 复制选中文本 / 退出\n"
                "  Ctrl+N   — 新对话\n"
                "  Ctrl+L   — 清屏\n"
                "  鼠标拖选  — 选择文本\n"
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
        elif cmd == "/config":
            parts = raw.strip().split(maxsplit=2)
            if len(parts) == 1:
                self._show_config()
            elif parts[1] == "set" and len(parts) >= 3:
                self._start_config_set(parts[2])
            else:
                log.write(Text.from_markup(
                    "[dim]/config 用法: /config (查看) | /config set tushare_token | /config set tickflow_api_key[/dim]"
                ))
        elif cmd == "/model":
            parts = raw.strip().split()
            if len(parts) == 1:
                self._switch_model_selector()
            elif parts[1] == "list":
                self._list_models()
            elif parts[1] == "add":
                self._start_model_add()
            elif parts[1] == "rm" and len(parts) >= 3:
                self._remove_model(parts[2])
            elif parts[1] == "default" and len(parts) >= 3:
                self._set_default_model(parts[2])
            else:
                log.write(Text.from_markup(
                    "[dim]/model 用法: /model (切换) | /model list | /model add | /model rm <id> | /model default <id>[/dim]"
                ))
        elif cmd == "/resume":
            parts = raw.strip().split(maxsplit=1)
            if len(parts) > 1:
                self._resume_session(parts[1].strip())
            else:
                self._resume_session_selector()
        else:
            self._try_skill(raw, log)

    # ----- Skills -----

    def _try_skill(self, raw: str, log) -> None:
        from cli.skills import load_skills
        skills = load_skills()
        parts = raw.strip().split(maxsplit=1)
        cmd_name = parts[0].lstrip("/").lower()
        user_input = parts[1] if len(parts) > 1 else ""
        if cmd_name in skills:
            self._execute_skill(cmd_name, user_input)
        else:
            log.write(Text.from_markup(f"[red]未知命令: {raw}[/red]，/help 查看"))

    def _execute_skill(self, name: str, user_input: str = "") -> None:
        from cli.skills import load_skills
        log = self.query_one("#chat-log", ChatLog)
        skills = load_skills()
        skill = skills.get(name)
        if not skill:
            log.write(Text.from_markup(f"[red]未知 skill: {name}[/red]"))
            return
        if not self._provider:
            log.write(Text.from_markup("[yellow]⚠ 未配置模型，请先输入 /model add[/yellow]"))
            return
        prompt = skill.prompt.replace("{user_input}", user_input).strip()
        self._send_message(prompt)

    def action_run_skill(self, name: str) -> None:
        """命令面板调用 skill 入口。"""
        self._execute_skill(name)

    # ----- /config 交互 -----

    _CONFIG_KEYS = {
        "tushare_token": ("Tushare Token", "TUSHARE_TOKEN", "https://tushare.pro 注册获取"),
        "tickflow_api_key": ("TickFlow API Key", "TICKFLOW_API_KEY", "https://tickflow.org 注册获取"),
    }

    def _show_config(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import load_config
        cfg = load_config()
        log.write(Text.from_markup("\n[bold]数据源配置[/bold]"))
        for key, (label, _, hint) in self._CONFIG_KEYS.items():
            val = str(cfg.get(key, "") or "").strip()
            if val:
                masked = val[:4] + "****" + val[-4:] if len(val) > 8 else "****"
                log.write(Text.from_markup(f"  {label}: [green]{masked}[/green]"))
            else:
                log.write(Text.from_markup(f"  {label}: [dim]未配置[/dim] — {hint}"))
        log.write(Text.from_markup("\n[dim]使用 /config set tushare_token 或 /config set tickflow_api_key 配置[/dim]"))

    def _start_config_set(self, key: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        key = key.strip().lower()
        if key not in self._CONFIG_KEYS:
            log.write(Text.from_markup(f"[red]不支持的配置项: {key}[/red]，可选: {', '.join(self._CONFIG_KEYS)}"))
            return
        label, _, hint = self._CONFIG_KEYS[key]
        log.write(Text.from_markup(f"\n[bold]配置 {label}[/bold]"))
        log.write(Text.from_markup(f"  {hint}"))
        log.write(Text.from_markup("  输入值（留空取消）："))
        inp.placeholder = f"{label}..."
        inp.password = True
        self._input_mode = _InputState.CONFIG_KEY
        self._input_buf = {"config_key": key}

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

    def _list_models(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import load_model_configs, load_default_model_id
        configs = load_model_configs()
        default_id = load_default_model_id()
        if not configs:
            log.write(Text.from_markup("[dim]尚无模型配置，使用 /model add 添加[/dim]"))
            return
        log.write(Text.from_markup("\n[bold]已配置模型[/bold] [dim](↑↓选择 Enter确认 Esc取消)[/dim]"))
        for c in configs:
            mark = " [green]⭐ 默认[/green]" if c["id"] == default_id else ""
            log.write(Text.from_markup(
                f"  [bold]{c['id']}[/bold] — {c['provider_name']}/{c.get('model', '?')}{mark}"
            ))
        self._switch_model_selector()

    def _remove_model(self, model_id: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import remove_model_entry
        if remove_model_entry(model_id):
            log.write(Text.from_markup(f"  [green]✓ 已删除 {model_id}[/green]"))
            self._rebuild_provider()
        else:
            log.write(Text.from_markup(f"  [red]无法删除（至少保留一个模型）[/red]"))

    def _set_default_model(self, model_id: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        from cli.auth import load_model_configs, set_default_model
        configs = load_model_configs()
        if not any(c["id"] == model_id for c in configs):
            log.write(Text.from_markup(f"  [red]未找到: {model_id}[/red]"))
            return
        set_default_model(model_id)
        log.write(Text.from_markup(f"  [green]✓ 默认模型已设为 {model_id}[/green]"))
        self._rebuild_provider()

    def _rebuild_provider(self) -> None:
        from cli.auth import load_model_configs, load_default_model_id
        configs = load_model_configs()
        default_id = load_default_model_id()
        if not configs:
            self._provider = None
            return
        default_cfg = next((c for c in configs if c["id"] == default_id), configs[0])
        if len(configs) == 1:
            from cli.__main__ import _create_provider
            provider, err = _create_provider(
                default_cfg["provider_name"], default_cfg["api_key"],
                default_cfg.get("model", ""), default_cfg.get("base_url", ""),
            )
            if not err:
                self._provider = provider
        else:
            from cli.providers.fallback import FallbackProvider
            self._provider = FallbackProvider(configs, default_id)
        self._state.update(default_cfg)
        if self._tools and self._provider:
            self._tools.set_provider(self._provider)
        self._update_status()

    def _show_selector(self, options: list[tuple[str, str]], callback_id: str) -> None:
        """显示模态选择器。options: [(value, label), ...]"""
        self.push_screen(SelectorScreen(options, callback_id))

    def _dismiss_selector(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def _on_selector_choice(self, callback_id: str, value: str | None) -> None:
        """选择器回调。"""
        self._dismiss_selector()
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)

        if value is None:
            log.write(Text.from_markup("[dim]已取消[/dim]"))
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            return

        if callback_id == "model_switch":
            self._set_default_model(value)

        elif callback_id == "session_resume":
            self._resume_session(value)

        elif callback_id == "model_provider":
            self._input_buf["provider"] = value
            log.write(Text.from_markup(f"  供应商: {value}"))
            log.write(Text.from_markup("  输入 API Key："))
            inp.placeholder = "API Key..."
            inp.password = True
            self._input_mode = _InputState.MODEL_KEY

    def _switch_model_selector(self) -> None:
        """弹出浮层选择器切换当前模型。"""
        from cli.auth import load_model_configs, load_default_model_id
        configs = load_model_configs()
        if not configs:
            log = self.query_one("#chat-log", ChatLog)
            log.write(Text.from_markup("[dim]尚无模型配置，使用 /model add 添加[/dim]"))
            return
        default_id = load_default_model_id()
        options = []
        for c in configs:
            mark = " ⭐" if c["id"] == default_id else ""
            label = f"{c['id']} ({c.get('model', '?')}){mark}"
            options.append((c["id"], label))
        self._show_selector(options, "model_switch")

    def _start_model_add(self) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        log.write(Text.from_markup(
            "\n[bold]添加模型[/bold]\n"
            "  输入别名（如 gemini, longcat, deepseek）："
        ))
        inp.placeholder = "模型别名..."
        self._input_mode = _InputState.MODEL_ID
        self._input_buf = {}

    # ----- 交互式输入状态机 -----

    def _handle_interactive_input(self, text: str) -> None:
        log = self.query_one("#chat-log", ChatLog)
        inp = self.query_one("#chat-input", Input)
        mode = self._input_mode

        # MODEL_NAME 和 MODEL_URL 留空表示用默认值，不取消
        if not text and mode not in (_InputState.MODEL_NAME, _InputState.MODEL_URL, _InputState.MODEL_ID):
            log.write(Text.from_markup("[dim]已取消[/dim]"))
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            return

        if mode == _InputState.CONFIG_KEY:
            inp.password = False
            key = self._input_buf["config_key"]
            label, env_key, _ = self._CONFIG_KEYS[key]
            from cli.auth import save_config_key
            save_config_key(key, text)
            import os
            os.environ[env_key] = text
            log.write(Text.from_markup(f"  [green]✓ {label} 已保存[/green]"))
            self._input_mode = _InputState.NONE
            inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
            return

        elif mode == _InputState.LOGIN_EMAIL:
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
                from core.stock_cache import set_cli_tokens
                set_cli_tokens(session.get("access_token", ""), session.get("refresh_token", ""))
                log.write(Text.from_markup(f"  [green]✓ 登录成功 ({session['email']})[/green]"))
                self._update_status()
            except Exception as e:
                err = str(e)
                if "Invalid login" in err or "invalid" in err.lower():
                    log.write(Text.from_markup("  [red]邮箱或密码错误，请重新输入[/red]"))
                else:
                    log.write(Text.from_markup(f"  [red]登录失败: {err}，请重新输入[/red]"))
                self._start_login()

        elif mode == _InputState.MODEL_ID:
            model_id = text.strip().lower() if text.strip() else ""
            if not model_id:
                log.write(Text.from_markup("[dim]已取消[/dim]"))
                self._input_mode = _InputState.NONE
                inp.placeholder = "问我关于股票的任何问题... (/help 查看命令)"
                return
            self._input_buf["id"] = model_id
            log.write(Text.from_markup(f"  别名: {model_id}"))
            log.write(Text.from_markup("  选择供应商（↑↓ 选择，Enter 确认，Esc 取消）："))
            self._input_mode = _InputState.MODEL_PROVIDER
            self._show_selector([
                ("gemini", "Gemini (Google)"),
                ("openai", "OpenAI / 兼容接口 (LongCat, DeepSeek, Qwen...)"),
                ("claude", "Claude (Anthropic)"),
            ], "model_provider")
            return  # 等 selector 回调

        elif mode == _InputState.MODEL_PROVIDER:
            # 文本输入兜底（selector 取消后手动输入）
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
            entry = {
                "id": buf.get("id", buf["provider"]),
                "provider_name": buf["provider"],
                "api_key": buf["api_key"],
                "model": buf.get("model", ""),
                "base_url": buf.get("base_url", ""),
            }
            from cli.auth import save_model_entry, load_model_configs, set_default_model
            save_model_entry(entry)
            # 首条模型或新添加的设为默认
            if len(load_model_configs()) == 1:
                set_default_model(entry["id"])
            import os
            env_key = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(buf["provider"])
            if env_key:
                os.environ[env_key] = buf["api_key"]
            self._rebuild_provider()
            log.write(Text.from_markup(f"  [green]✓ 已添加 {entry['id']} ({self._provider.name if self._provider else '?'})[/green]"))
        except Exception as e:
            log.write(Text.from_markup(f"  [red]配置失败: {e}[/red]"))

    def _chatlog_save(self, role: str, content: str, **kwargs):
        """保存一条对话记录到 SQLite（静默失败）。"""
        try:
            from integrations.local_db import save_chat_log
            save_chat_log(self._session_id, role, content, **kwargs)
        except Exception:
            pass

    # ----- Agent 执行（后台 Worker）-----

    @work(thread=True, exclusive=True)
    def _run_agent(self) -> None:
        self._busy = True
        log = self.query_one("#chat-log", ChatLog)

        def _write(renderable):
            self.call_from_thread(log.write, renderable)

        def _write_stream(renderable) -> int:
            return self.call_from_thread(_write_counted, log, renderable)

        def _scroll():
            self.call_from_thread(log.scroll_end, animate=False)

        def _spinner_start(label="思考中"):
            self.call_from_thread(self._start_spinner, label)

        def _spinner_stop():
            self.call_from_thread(self._stop_spinner)

        total_input = 0
        total_output = 0
        t_start = time.monotonic()
        _recent_calls: list[tuple[str, str]] = []  # doom-loop: (name, args_hash)
        _doom_break = False

        # 记录用户输入
        _user_text = self._messages[-1]["content"] if self._messages else ""
        _model_name = getattr(self._provider, "name", "") if self._provider else ""
        _provider_name = self._state.get("provider_name", "") if self._state else ""
        expectation = resolve_turn_expectation(self._messages)
        incomplete_tool_retries = 0
        used_tools_this_turn: list[tuple[str, dict]] = []
        executed_tool_summaries: list[dict[str, object]] = []
        self._agent_log.info("session=%s user: %s", self._session_id, _user_text[:200])
        _chatlog_save = self._chatlog_save  # bound method ref

        try:
            from cli.loop_guard import MAX_TOOL_ROUNDS, check_doom_loop

            _model_name_for_compact = getattr(self._provider, "name", "") if self._provider else ""

            for round_idx in range(MAX_TOOL_ROUNDS):
                # ── Context compaction ──
                _spinner_start("压缩上下文")
                prev_len = len(self._messages)
                self._messages, compacted = compact_messages(
                    self._messages, self._provider, _model_name_for_compact,
                )
                _spinner_stop()
                if compacted:
                    _write(Text.from_markup(
                        f"  [dim]📦 上下文已压缩（{prev_len - _TAIL_KEEP_DEFAULT}条→摘要，保留最近{_TAIL_KEEP_DEFAULT}条）[/dim]"
                    ))

                text_buf = ""
                thinking_buf = ""
                tool_calls = None
                round_usage = {}
                _stream_separator_strips = 0
                _stream_text_strips = 0
                _streaming_started = False
                _stream_line_buf = ""

                def _clear_streamed_block(*, include_separator: bool) -> None:
                    nonlocal _stream_separator_strips
                    nonlocal _stream_text_strips, _streaming_started
                    strip_count = _stream_text_strips
                    if include_separator:
                        strip_count += _stream_separator_strips
                    if _streaming_started and strip_count > 0:
                        self.call_from_thread(_pop_lines, log, strip_count)
                    _stream_text_strips = 0
                    if include_separator:
                        _stream_separator_strips = 0
                        _streaming_started = False

                if round_idx > 0:
                    _spinner_start()

                # ── 带重试的 streaming ──
                _MAX_STREAM_RETRIES = 3
                _stream = None
                for _retry in range(_MAX_STREAM_RETRIES):
                    try:
                        _stream = self._provider.chat_stream(
                            self._messages, self._tools.schemas(), with_current_time(self._system_prompt)
                        )
                        break
                    except Exception as _stream_err:
                        self._agent_log.warning(
                            "session=%s stream_connect_fail retry=%d err=%s",
                            self._session_id, _retry, _stream_err,
                        )
                        from cli.providers.fallback import _is_retriable
                        if not _is_retriable(_stream_err) or _retry == _MAX_STREAM_RETRIES - 1:
                            raise
                        _delay = min(2 ** (_retry + 1), 30)
                        _write(Text.from_markup(
                            f"  [yellow]⚡ 连接失败，{_delay}s 后重试（{_retry+1}/{_MAX_STREAM_RETRIES}）[/yellow]"
                        ))
                        _scroll()
                        time.sleep(_delay)

                for chunk in _stream:
                    if chunk["type"] == "thinking_delta":
                        thinking_buf += chunk["text"]

                    elif chunk["type"] == "text_delta":
                        text_buf += chunk["text"]
                        _stream_line_buf += chunk["text"]
                        if not _streaming_started:
                            _spinner_stop()
                            _stream_separator_strips += _write_stream(
                                Text.from_markup("  [dim]───[/dim]")
                            )
                            _streaming_started = True
                        while "\n" in _stream_line_buf:
                            line, _stream_line_buf = _stream_line_buf.split("\n", 1)
                            _stream_text_strips += _write_stream(Text(line))
                            _scroll()

                    elif chunk["type"] == "tool_calls":
                        tool_calls = chunk["tool_calls"]
                        partial = chunk.get("text", "")
                        if partial and not text_buf:
                            text_buf = partial

                    elif chunk["type"] == "usage":
                        round_usage = chunk

                _spinner_stop()

                # 刷出流式行缓冲剩余
                if _stream_line_buf:
                    _stream_text_strips += _write_stream(Text(_stream_line_buf))
                    _stream_line_buf = ""
                    _scroll()

                total_input += round_usage.get("input_tokens", 0)
                total_output += round_usage.get("output_tokens", 0)

                will_retry_missing_tool = (
                    missing_required_tool(expectation, used_tools_this_turn)
                    and incomplete_tool_retries < _MAX_INCOMPLETE_TOOL_RETRIES
                )
                if tool_calls or will_retry_missing_tool:
                    _clear_streamed_block(include_separator=True)
                elif _streaming_started:
                    _clear_streamed_block(include_separator=False)

                # ── Fallback 通知 ──
                fb_msg = getattr(self._provider, "last_fallback_msg", None)
                if fb_msg:
                    _write(Text.from_markup(f"  [yellow]⚡ {fb_msg}[/yellow]"))
                    self._provider.last_fallback_msg = None

                # ── Thinking 摘要（折叠为一行） ──
                if thinking_buf:
                    preview = thinking_buf.strip().replace("\n", " ")
                    if len(preview) > 80:
                        preview = preview[:80] + "…"
                    _write(Text.from_markup(
                        f"  [italic magenta]💭 {preview}[/italic magenta]  [dim]({len(thinking_buf)} 字)[/dim]"
                    ))

                # ── 工具调用 ──
                if tool_calls:
                    assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
                    if text_buf:
                        assistant_msg["content"] = text_buf
                    if thinking_buf:
                        assistant_msg["reasoning_content"] = thinking_buf
                    self._messages.append(assistant_msg)

                    for call in tool_calls:
                        name = call["name"]
                        args = call["args"]
                        call_id = call["id"]
                        display = self._tools.display_name(name)
                        used_tools_this_turn.append((name, args))

                        # ── Doom-loop 检测 ──
                        if check_doom_loop(_recent_calls, name, args):
                            _write(Text.from_markup(
                                f"  [yellow]⚠ 检测到重复调用 {display}，已中止循环[/yellow]"
                            ))
                            self._messages.append({
                                "role": "tool", "tool_call_id": call_id, "name": name,
                                "content": json.dumps({"error": "doom-loop: 同参数重复调用3次，已中止"}, ensure_ascii=False),
                            })
                            tool_calls = None
                            _doom_break = True
                            break

                        _spinner_start(display)

                        t_tool = time.monotonic()
                        result = self._tools.execute(name, args)
                        elapsed_tool = time.monotonic() - t_tool

                        _spinner_stop()

                        if isinstance(result, dict) and result.get("error"):
                            executed_tool_summaries.append({
                                "name": name,
                                "args_brief": str(args)[:100],
                                "status": "error",
                                "error": str(result.get("error", ""))[:160],
                            })
                            _write(Text.from_markup(
                                f"  [red]✗ {display}[/red] [dim]{elapsed_tool:.1f}s {str(result['error'])[:80]}[/dim]"
                            ))
                        elif isinstance(result, dict) and result.get("status") == "background":
                            executed_tool_summaries.append({
                                "name": name,
                                "args_brief": str(args)[:100],
                                "status": "background",
                            })
                            _write(Text.from_markup(
                                f"  [cyan]↗ {display}[/cyan] [dim]已提交后台[/dim]"
                            ))
                        else:
                            executed_tool_summaries.append({
                                "name": name,
                                "args_brief": str(args)[:100],
                                "status": "ok",
                            })
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
                    if tool_calls is not None:
                        continue
                    # doom-loop 中止：不再继续下一轮，走到最终输出

                if (
                    missing_required_tool(expectation, used_tools_this_turn)
                    and incomplete_tool_retries < _MAX_INCOMPLETE_TOOL_RETRIES
                ):
                    retry_prompt = build_retry_user_message(expectation, text_buf)
                    incomplete_tool_retries += 1
                    self._agent_log.info(
                        "session=%s loop_guard retry=%d required_tool=%s reason=%s",
                        self._session_id,
                        incomplete_tool_retries,
                        expectation.required_tool if expectation else "",
                        expectation.reason if expectation else "",
                    )
                    _write(Text.from_markup(
                        "  [yellow]⚠ 模型未执行必需工具，已自动要求继续执行[/yellow]"
                    ))
                    _scroll()
                    if text_buf:
                        _retry_msg: dict[str, Any] = {"role": "assistant", "content": text_buf}
                        if thinking_buf:
                            _retry_msg["reasoning_content"] = thinking_buf
                        self._messages.append(_retry_msg)
                    self._messages.append({"role": "user", "content": retry_prompt})
                    continue

                # ── 最终输出 ──
                if missing_required_tool(expectation, used_tools_this_turn):
                    warning = build_retry_exhausted_warning(expectation, incomplete_tool_retries)
                    text_buf = f"{warning}\n\n{text_buf}".strip()
                if not _doom_break:
                    _final_msg: dict[str, Any] = {"role": "assistant", "content": text_buf}
                    if thinking_buf:
                        _final_msg["reasoning_content"] = thinking_buf
                    self._messages.append(_final_msg)
                if text_buf:
                    if not _streaming_started:
                        _write(Text.from_markup("  [dim]───[/dim]"))
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

                # 保存对话记录
                _chatlog_save("user", _user_text, model=_model_name, provider=_provider_name)
                _tc_json = (
                    json.dumps(executed_tool_summaries, ensure_ascii=False)
                    if executed_tool_summaries
                    else ""
                )
                _chatlog_save(
                    "assistant", text_buf,
                    model=_model_name, provider=_provider_name,
                    tokens_in=total_input, tokens_out=total_output,
                    elapsed_s=round(elapsed, 2), tool_calls_json=_tc_json,
                )
                self._agent_log.info(
                    "session=%s done in=%.1fs tokens=%d/%d",
                    self._session_id, elapsed, total_input, total_output,
                )
                break
            else:
                _write(Text.from_markup("[yellow](工具调用轮次超限)[/yellow]"))

        except Exception as e:
            _spinner_stop()
            err = str(e)
            # 清理 HTML 错误响应，只保留关键信息
            if "<html" in err.lower():
                import re
                title = re.search(r"<title>(.*?)</title>", err, re.IGNORECASE)
                err = title.group(1) if title else "服务端返回 HTML 错误"
            if len(err) > 200:
                err = err[:200] + "..."
            _write(Text.from_markup(f"[red]错误: {err}[/red]"))
            # 记录错误到日志和 SQLite
            _elapsed = time.monotonic() - t_start
            self._agent_log.error(
                "session=%s error after=%.1fs type=%s msg=%s",
                self._session_id, _elapsed, type(e).__name__, str(e)[:500],
            )
            _chatlog_save("user", _user_text, model=_model_name, provider=_provider_name)
            _chatlog_save(
                "error", "", model=_model_name, provider=_provider_name,
                elapsed_s=round(_elapsed, 2),
                error=f"{type(e).__name__}: {str(e)[:500]}",
            )
            while self._messages and self._messages[-1].get("role") != "user":
                self._messages.pop()
            if self._messages:
                self._messages.pop()

        finally:
            self._busy = False
            if self._queue:
                next_msg = self._queue.popleft()
                self.call_from_thread(self._send_message, next_msg)

    # ----- 后台任务回调 -----

    def _on_bg_complete(self, task_id: str, tool_name: str, result) -> None:
        """后台任务完成，注入结果到消息队列。"""
        from cli.tools import TOOL_DISPLAY_NAMES
        display = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
        is_error = isinstance(result, dict) and result.get("error")

        log = self.query_one("#chat-log", ChatLog)
        if is_error:
            self.call_from_thread(
                log.write,
                Text.from_markup(f"  [red]✗ 后台任务失败：{display}[/red] [dim]{str(result['error'])[:80]}[/dim]"),
            )
        else:
            self.call_from_thread(
                log.write,
                Text.from_markup(f"  [green]✅ 后台任务完成：{display}[/green]"),
            )

        summary = json.dumps(result, ensure_ascii=False, default=str)
        if len(summary) > 3000:
            summary = summary[:3000] + "..."
        self._queue.append(f"[后台任务完成] {tool_name}: {summary}")
        # 空闲时自动触发
        if not self._busy:
            self.call_from_thread(self._send_message, self._queue.popleft())

    # ----- Actions -----

    def action_clear_chat(self) -> None:
        self.query_one("#chat-log", ChatLog).clear()

    def action_resume_session(self) -> None:
        self._resume_session_selector()

    def _resume_session_selector(self) -> None:
        """弹出选择器，选择要恢复的历史会话。"""
        from integrations.local_db import list_chat_sessions, get_session_preview
        log = self.query_one("#chat-log", ChatLog)
        sessions = list_chat_sessions(limit=20)
        sessions = [s for s in sessions if s["session_id"] != self._session_id]
        if not sessions:
            log.write(Text.from_markup("[dim]没有可恢复的历史会话[/dim]"))
            return
        options = []
        for s in sessions:
            preview = get_session_preview(s["session_id"])
            started = (s["started_at"] or "?")[:16]
            n = s["msg_count"]
            label = f"{started}  ({n}条)  {preview}"
            options.append((s["session_id"], label))
        self._show_selector(options, "session_resume")

    def _resume_session(self, session_id: str) -> None:
        """恢复指定会话，加载历史消息到 self._messages。"""
        from integrations.local_db import load_chat_logs, list_chat_sessions
        log = self.query_one("#chat-log", ChatLog)

        if session_id.isdigit():
            idx = int(session_id)
            sessions = list_chat_sessions(limit=20)
            sessions = [s for s in sessions if s["session_id"] != self._session_id]
            if idx < 1 or idx > len(sessions):
                log.write(Text.from_markup(f"[red]无效序号: {idx} (共 {len(sessions)} 个历史会话)[/red]"))
                return
            session_id = sessions[idx - 1]["session_id"]

        rows = load_chat_logs(session_id=session_id)
        if not rows:
            log.write(Text.from_markup(f"[red]未找到会话: {session_id}[/red]"))
            return

        # 保存当前会话记忆
        if self._messages and self._provider:
            try:
                from cli.memory import save_session_summary
                import threading
                threading.Thread(
                    target=save_session_summary,
                    args=(list(self._messages), self._provider),
                    daemon=True,
                ).start()
            except Exception:
                pass

        # 重置状态
        self._messages.clear()
        self._queue.clear()
        self._session_tokens = {"input": 0, "output": 0, "rounds": 0}
        self._session_id = session_id
        log.clear()

        log.write(Text.from_markup(
            f"[green]已恢复会话[/green] [dim]{session_id} · {len(rows)} 条记录[/dim]\n"
        ))

        for row in rows:
            role = row["role"]
            content = row["content"] or ""

            if role == "error":
                if row.get("error"):
                    log.write(Text.from_markup(f"  [dim red]✗ {str(row['error'])[:80]}[/dim red]"))
                continue

            if role == "user":
                self._messages.append({"role": "user", "content": content})
                preview = content if len(content) <= 120 else content[:120] + "…"
                log.write(Text.from_markup(f"[bold cyan]❯[/bold cyan] {preview}"))

            elif role == "assistant":
                self._messages.append({"role": "assistant", "content": content})
                tc = row.get("tool_calls", "")
                if tc:
                    try:
                        calls = json.loads(tc)
                        names = ", ".join(c.get("name", "?") for c in calls)
                        log.write(Text.from_markup(f"  [dim green]✓ {names}[/dim green]"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                if content:
                    preview = content if len(content) <= 200 else content[:200] + "…"
                    log.write(Text.from_markup(f"  [dim]{preview}[/dim]"))

        log.write(Text.from_markup("\n[dim]───── 历史消息结束，继续对话 ─────[/dim]\n"))
        log.scroll_end(animate=False)
        self._update_status()

    def action_new_chat(self) -> None:
        # 保存会话记忆
        if self._messages and self._provider:
            try:
                from cli.memory import save_session_summary
                import threading
                msgs_copy = list(self._messages)
                threading.Thread(
                    target=save_session_summary,
                    args=(msgs_copy, self._provider),
                    daemon=True,
                ).start()
            except Exception:
                pass
        self._messages.clear()
        self._queue.clear()
        self._session_tokens = {"input": 0, "output": 0, "rounds": 0}
        self._session_id = uuid.uuid4().hex[:12]
        log = self.query_one("#chat-log", ChatLog)
        log.clear()
        log.write(Text.from_markup("[green]新对话已开始[/green]\n"))
        self._update_status()


# 注册命令面板（class 定义完成后）
try:
    from cli.commands import WyckoffCommands
    WyckoffTUI.COMMANDS = {WyckoffCommands}
except ImportError:
    pass


def _brief_args(args: dict) -> str:
    if not args:
        return ""
    s = ", ".join(f"{k}={v}" for k, v in args.items())
    return s[:60] + ("..." if len(s) > 60 else "")
