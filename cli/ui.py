"""终端 UI — Claude Code 风格的精致 TUI。"""

from __future__ import annotations

import os
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

console = Console()

_commands = WordCompleter(
    ["/help", "/clear", "/new", "/quit", "/exit", "/q", "/model", "/login", "/logout", "/token"],
    sentence=True,
)
_session: PromptSession | None = None

# Enter 提交，Escape+Enter（终端中的 Alt+Enter）插入换行
_kb = KeyBindings()


@_kb.add("escape", "enter")
def _insert_newline(event):
    event.current_buffer.insert_text("\n")


def _get_session() -> PromptSession:
    global _session
    if _session is None:
        history_path = Path.home() / ".wyckoff" / "input_history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        _session = PromptSession(
            history=FileHistory(str(history_path)),
            completer=_commands,
            key_bindings=_kb,
        )
    return _session


# ---------------------------------------------------------------------------
# 交互式输入辅助
# ---------------------------------------------------------------------------


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = console.input(f"  [dim]{label}{suffix}:[/dim] ").strip()
        return val or default
    except (EOFError, KeyboardInterrupt):
        return default


def _prompt_secret(label: str, current: str = "") -> str:
    import getpass

    masked = ""
    if current:
        masked = current[:6] + "..." + current[-4:] if len(current) > 12 else "***"
    suffix = f" [{masked}]" if masked else ""
    try:
        val = getpass.getpass(f"  {label}{suffix}: ").strip()
        return val or current
    except (EOFError, KeyboardInterrupt):
        return current


# ---------------------------------------------------------------------------
# Banner — Claude Code 风格
# ---------------------------------------------------------------------------

# 小型像素风 logo（5 行高）
_LOGO = r"""
 ╦ ╦
 ║║║
 ╚╩╝
""".strip("\n")


def print_banner(email: str = "", model: str = "", version: str = "") -> None:
    if not version:
        try:
            from importlib.metadata import version as _v

            version = _v("youngcan-wyckoff-analysis")
        except Exception:
            version = "dev"

    console.print()

    # Logo + 标题行
    logo_lines = _LOGO.split("\n")
    info_lines = [
        f"[bold]Wyckoff CLI[/bold] v{version}",
        "",
        "",
    ]

    # 状态信息
    parts = []
    if model:
        parts.append(model)
    else:
        parts.append("[dim]未配置模型 /model[/dim]")
    if email:
        parts.append(email)
    else:
        parts.append("[dim]未登录 /login[/dim]")
    info_lines[1] = " · ".join(parts)

    # 提示
    info_lines[2] = "[dim]直接输入问题开始对话，/help 查看更多[/dim]"

    for i, logo_line in enumerate(logo_lines):
        info = info_lines[i] if i < len(info_lines) else ""
        console.print(f"        [bold yellow]{logo_line}[/bold yellow]   {info}")

    console.print()
    # 分隔线
    console.print(f"[dim]{'─' * min(console.width, 80)}[/dim]")


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def print_help() -> None:
    console.print()
    console.print("  [bold]命令[/bold]")
    console.print("  [cyan]/model[/cyan]    配置模型      [cyan]/login[/cyan]   登录")
    console.print("  [cyan]/new[/cyan]      新对话        [cyan]/logout[/cyan]  登出")
    console.print("  [cyan]/token[/cyan]    用量统计      [cyan]/quit[/cyan]    退出")
    console.print("  [cyan]/clear[/cyan]    清屏")
    console.print()
    console.print("  [bold]试试这样问[/bold]")
    console.print("  [dim]帮我看看宁德时代[/dim]          个股诊断")
    console.print("  [dim]大盘现在什么水温[/dim]          市场概览")
    console.print("  [dim]有没有确认的信号[/dim]          信号确认池")
    console.print("  [dim]我的持仓还安全吗[/dim]          持仓体检")
    console.print("  [dim]帮我从全市场找机会[/dim]        五层漏斗扫描")
    console.print("  [dim]过去推荐的表现怎么样[/dim]      战绩追踪")
    console.print()


# ---------------------------------------------------------------------------
# /login 交互式登录
# ---------------------------------------------------------------------------


def login_prompt() -> tuple[str, str] | None:
    console.print()
    email = _prompt("邮箱")
    if not email:
        return None
    password = _prompt_secret("密码")
    if not password:
        return None
    return email, password


# ---------------------------------------------------------------------------
# /model 交互式配置
# ---------------------------------------------------------------------------

PROVIDER_CHOICES = {
    "1": "gemini",
    "2": "claude",
    "3": "openai",
}

KEY_ENV_MAP = {
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "claude": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
}


def _save_to_env(key: str, value: str) -> None:
    """将配置写入环境变量（运行时生效，持久化由 config.json 负责）。"""
    os.environ[key] = value


def configure_model(state: dict) -> dict | None:
    console.print()
    console.print("  [bold]选择 Provider[/bold]")
    console.print("  [cyan]1[/cyan]) Gemini   [cyan]2[/cyan]) Claude   [cyan]3[/cyan]) OpenAI（含兼容端点）")
    console.print()

    cur_provider = state.get("provider_name", "")
    cur_num = ""
    for k, v in PROVIDER_CHOICES.items():
        if v == cur_provider:
            cur_num = k
            break

    choice = _prompt("输入编号", cur_num or "1")
    provider_name = PROVIDER_CHOICES.get(choice)
    if not provider_name:
        print_error(f"无效选项: {choice}")
        return None

    env_key = KEY_ENV_MAP.get(provider_name, "")
    env_val = os.getenv(env_key, "").strip() if env_key else ""

    if env_val:
        masked = env_val[:6] + "..." + env_val[-4:] if len(env_val) > 12 else "***"
        console.print(f"  [green]+[/green] {env_key} [dim]({masked})[/dim]")
        api_key = env_val
    else:
        api_key = _prompt_secret(f"输入 {env_key} (购买: https://www.1route.dev/register?aff=359904261)")
        if not api_key:
            print_error("API Key 不能为空。")
            return None
        _save_to_env(env_key, api_key)

    model_env_key = f"{provider_name.upper()}_MODEL"
    if provider_name == "claude":
        model_env_key = "ANTHROPIC_MODEL"
    env_model = os.getenv(model_env_key, "").strip()
    if env_model:
        console.print(f"  [green]+[/green] {model_env_key} [dim]({env_model})[/dim]")
        model = env_model
    else:
        default_model = DEFAULT_MODELS.get(provider_name, "")
        model = _prompt("模型名称", default_model)
        if model and model != default_model:
            _save_to_env(model_env_key, model)

    base_url = ""
    if provider_name == "openai":
        env_base = os.getenv("OPENAI_BASE_URL", "").strip()
        if env_base:
            console.print(f"  [green]+[/green] OPENAI_BASE_URL [dim]({env_base})[/dim]")
            base_url = env_base
        else:
            console.print("  [dim]OpenAI 官方直接回车跳过，第三方兼容端点请输入 URL[/dim]")
            base_url = _prompt("Base URL")
            if base_url:
                _save_to_env("OPENAI_BASE_URL", base_url)

    console.print()
    return {
        "provider_name": provider_name,
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
    }


# ---------------------------------------------------------------------------
# 工具调用 / 响应渲染
# ---------------------------------------------------------------------------

_live: Live | None = None
_tool_start: float = 0


def _stop_live() -> None:
    global _live
    if _live is not None:
        _live.stop()
        _live = None


def print_tool_call(name: str, display_name: str, args: dict) -> None:
    import time

    global _live, _tool_start
    _stop_live()
    _tool_start = time.monotonic()

    args_brief = ""
    if args:
        args_str = ", ".join(f"{k}={v}" for k, v in args.items())
        args_brief = f" [dim]{args_str[:60]}{'...' if len(args_str) > 60 else ''}[/dim]"

    spinner = Spinner("dots", text=Text.from_markup(f"  [yellow]{display_name}[/yellow]{args_brief}"))
    _live = Live(spinner, console=console, refresh_per_second=10, transient=True)
    _live.start()


def print_tool_result(name: str, display_name: str, result) -> None:
    import time

    _stop_live()
    elapsed = time.monotonic() - _tool_start
    time_str = f" [dim]{elapsed:.1f}s[/dim]" if elapsed >= 1.0 else ""
    if isinstance(result, dict) and result.get("error"):
        console.print(f"  [red]✗ {display_name}[/red]{time_str} [dim]{result['error']}[/dim]")
    else:
        console.print(f"  [green]✓ {display_name}[/green]{time_str}")


def print_response(text: str) -> None:
    _stop_live()
    console.print()
    console.print(Markdown(text), width=min(console.width, 100))
    console.print()


def print_usage(input_tokens: int, output_tokens: int, elapsed: float = 0, model: str = "") -> None:
    parts = []
    if input_tokens or output_tokens:
        parts.append(f"{input_tokens:,} in · {output_tokens:,} out")
    if elapsed > 0:
        parts.append(f"{elapsed:.1f}s")
    if parts:
        console.print(f"  [dim]↳ {' · '.join(parts)}[/dim]")


def print_token_summary(session_tokens: dict, model: str = "") -> None:
    total = session_tokens["input"] + session_tokens["output"]
    console.print()
    console.print("  [bold]本次会话 Token 用量[/bold]")
    if model:
        console.print(f"  模型      {model}")
    console.print(f"  对话轮数  {session_tokens['rounds']}")
    console.print(f"  输入      {session_tokens['input']:,}")
    console.print(f"  输出      {session_tokens['output']:,}")
    console.print(f"  合计      {total:,}")
    console.print()


def print_error(message: str) -> None:
    console.print(f"  [red]{message}[/red]")


def print_info(message: str) -> None:
    console.print(f"  [dim]{message}[/dim]")


_ctrl_c_count = 0


def get_input() -> str:
    global _ctrl_c_count
    try:
        result = _get_session().prompt(HTML('<b><style fg="ansiblue">❯ </style></b>')).strip()
        _ctrl_c_count = 0
        return result
    except KeyboardInterrupt:
        _ctrl_c_count += 1
        console.print()
        if _ctrl_c_count >= 2:
            return "/quit"
        console.print("  [dim]再按一次 Ctrl+C 退出，或继续输入[/dim]")
        return ""
    except EOFError:
        # Ctrl+D 退出
        console.print()
        return "/quit"
