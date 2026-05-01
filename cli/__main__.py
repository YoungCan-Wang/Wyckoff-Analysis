# -*- coding: utf-8 -*-
"""
威科夫终端读盘室 — 入口。

用法:
    wyckoff                         # 启动 TUI
    wyckoff update                  # 升级到最新版
    wyckoff dashboard               # 启动本地可视化面板
    wyckoff auth <email> <password> # 登录
    wyckoff auth logout             # 退出登录
    wyckoff auth status             # 查看登录状态
    wyckoff model list              # 列出模型
    wyckoff model add               # 添加模型（交互式）
    wyckoff model set <id> <provider> <key> [--model X] [--base-url X]
    wyckoff model rm <id>           # 删除模型
    wyckoff model default <id>      # 设默认模型
    wyckoff config                  # 查看数据源配置
    wyckoff config tushare <token>  # 配置 Tushare Token
    wyckoff config tickflow <key>   # 配置 TickFlow API Key
"""
from __future__ import annotations

import argparse
import json
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
_warnings.filterwarnings("ignore", category=ResourceWarning)
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


def _get_version() -> str:
    try:
        from importlib.metadata import version
        return version("youngcan-wyckoff-analysis")
    except Exception:
        return "dev"


def _check_update_async() -> None:
    """后台检查 PyPI 最新版本，有新版则打印提示。"""
    import threading

    def _check():
        try:
            import urllib.request
            local_ver = _get_version()
            if local_ver == "dev":
                return
            url = "https://pypi.org/pypi/youngcan-wyckoff-analysis/json"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            latest = data.get("info", {}).get("version", "")
            if not latest or latest == local_ver:
                return
            local_parts = tuple(int(x) for x in local_ver.split("."))
            latest_parts = tuple(int(x) for x in latest.split("."))
            if latest_parts > local_parts:
                print(
                    f"\033[33m⬆ 新版本可用: {latest}（当前 {local_ver}），"
                    f"运行 wyckoff update 升级\033[0m"
                )
        except Exception:
            pass

    threading.Thread(target=_check, daemon=True).start()


def _mask(val: str) -> str:
    if len(val) > 8:
        return val[:4] + "****" + val[-4:]
    return "****" if val else ""


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def _cmd_update(_args):
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
        url = "https://wyckoff-analysis-youngcanphoenix.streamlit.app/"
        try:
            subprocess.run(["pbcopy"], input=url.encode(), check=True)
        except FileNotFoundError:
            try:
                subprocess.run(["xclip", "-selection", "clipboard"], input=url.encode(), check=True)
            except FileNotFoundError:
                pass
        print(f"\n✓ 升级完成！请重新运行 wyckoff。\n  Web 版已复制到剪切板: {url}")
    except subprocess.CalledProcessError as e:
        print(f"\n✗ 升级失败: {e}")
        sys.exit(1)


def _cmd_auth(args):
    from cli.auth import login, logout, restore_session, _load_session

    sub = args.auth_cmd

    if sub == "logout":
        logout()
        print("✓ 已退出登录")
        return

    if sub == "status":
        session = _load_session()
        if not session:
            print("未登录")
            return
        restored = restore_session()
        if restored:
            print(f"✓ 已登录: {restored['email']}")
            print(f"  user_id: {restored['user_id']}")
        else:
            print("⚠ 登录已过期，请重新登录")
        return

    # wyckoff auth <email> <password>
    email = sub
    password = args.password
    if not password:
        import getpass
        password = getpass.getpass("密码: ")
    try:
        session = login(email, password)
        print(f"✓ 登录成功: {session['email']}")
        print(f"  user_id: {session['user_id']}")
    except Exception as e:
        err = str(e)
        if "Invalid login" in err or "invalid" in err.lower():
            print("✗ 邮箱或密码错误")
        else:
            print(f"✗ 登录失败: {err}")
        sys.exit(1)


def _cmd_model(args):
    from cli.auth import (
        load_model_configs, load_default_model_id,
        save_model_entry, remove_model_entry, set_default_model,
    )

    sub = args.model_cmd or "list"

    if sub == "list":
        configs = load_model_configs()
        default_id = load_default_model_id()
        if not configs:
            print("尚无模型配置，使用 wyckoff model add 添加")
            return
        for c in configs:
            mark = " *" if c["id"] == default_id else ""
            print(f"  {c['id']}{mark}  provider={c.get('provider_name','')}  model={c.get('model','')}  base_url={c.get('base_url','') or '(default)'}")
        return

    if sub == "add":
        # 交互式添加
        model_id = input("别名 (如 gemini, longcat): ").strip().lower()
        if not model_id:
            print("已取消")
            return
        provider = input("供应商 (gemini/openai/claude): ").strip().lower()
        if provider not in ("gemini", "openai", "claude"):
            print(f"✗ 不支持: {provider}")
            sys.exit(1)
        import getpass
        api_key = getpass.getpass("API Key: ").strip()
        if not api_key:
            print("已取消")
            return
        default_models = {"gemini": "gemini-2.0-flash", "openai": "gpt-4o", "claude": "claude-sonnet-4-20250514"}
        model = input(f"模型名 (留空使用 {default_models.get(provider, '')}): ").strip()
        model = model or default_models.get(provider, "")
        base_url = input("Base URL (留空使用默认): ").strip()
        entry = {
            "id": model_id,
            "provider_name": provider,
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
        }
        save_model_entry(entry)
        if len(load_model_configs()) == 1:
            set_default_model(model_id)
        print(f"✓ 模型 {model_id} 已保存")
        return

    if sub == "set":
        model_id = args.model_id
        provider = args.provider
        api_key = args.api_key
        if not all([model_id, provider, api_key]):
            print("用法: wyckoff model set <id> <provider> <api_key> [--model X] [--base-url X]")
            sys.exit(1)
        entry = {
            "id": model_id,
            "provider_name": provider,
            "api_key": api_key,
            "model": args.model_name or "",
            "base_url": args.base_url or "",
        }
        save_model_entry(entry)
        print(f"✓ 模型 {model_id} 已保存")
        return

    if sub == "rm":
        model_id = args.model_id
        if not model_id:
            print("用法: wyckoff model rm <id>")
            sys.exit(1)
        if remove_model_entry(model_id):
            print(f"✓ 模型 {model_id} 已删除")
        else:
            print("✗ 至少保留一个模型")
        return

    if sub == "default":
        model_id = args.model_id
        if not model_id:
            print("用法: wyckoff model default <id>")
            sys.exit(1)
        configs = load_model_configs()
        if not any(c["id"] == model_id for c in configs):
            print(f"✗ 模型 {model_id} 不存在")
            sys.exit(1)
        set_default_model(model_id)
        print(f"✓ 默认模型已切换为 {model_id}")
        return

    print(f"未知子命令: {sub}")
    print("用法: wyckoff model [list|add|set|rm|default]")
    sys.exit(1)


def _cmd_config(args):
    from cli.auth import load_config, save_config_key

    CONFIG_KEYS = {
        "tushare": ("tushare_token", "Tushare Token", "TUSHARE_TOKEN"),
        "tickflow": ("tickflow_api_key", "TickFlow API Key", "TICKFLOW_API_KEY"),
    }

    sub = args.config_cmd

    if not sub:
        # 显示所有配置
        cfg = load_config()
        print("数据源配置 (~/.wyckoff/wyckoff.json)")
        print()
        for alias, (key, label, _) in CONFIG_KEYS.items():
            val = str(cfg.get(key, "") or "").strip()
            status = f"\033[32m{_mask(val)}\033[0m" if val else "\033[90m未配置\033[0m"
            print(f"  {label}: {status}")
        print()
        print("使用 wyckoff config tushare <token> 或 wyckoff config tickflow <key> 配置")
        return

    if sub not in CONFIG_KEYS:
        print(f"✗ 未知配置项: {sub}")
        print(f"可选: {', '.join(CONFIG_KEYS)}")
        sys.exit(1)

    key, label, env_key = CONFIG_KEYS[sub]
    value = args.value
    if not value:
        import getpass
        value = getpass.getpass(f"{label}: ").strip()
    if not value:
        print("已取消")
        return
    save_config_key(key, value)
    os.environ[env_key] = value
    print(f"✓ {label} 已保存")


# ---------------------------------------------------------------------------
# 持仓 helpers
# ---------------------------------------------------------------------------

def _get_session_client():
    """从 session.json 获取 user client，返回 (client, user_id, portfolio_id) 或退出。"""
    from cli.auth import restore_session
    session = restore_session()
    if not session or not session.get("access_token"):
        print("✗ 未登录，请先执行 wyckoff auth <email> <password>")
        sys.exit(1)
    from integrations.supabase_base import create_user_client
    from integrations.supabase_portfolio import build_user_live_portfolio_id
    client = create_user_client(session["access_token"], session["refresh_token"])
    uid = session["user_id"]
    pid = build_user_live_portfolio_id(uid)
    return client, uid, pid


def _cmd_portfolio(args):
    sub = args.portfolio_cmd or "list"

    if sub == "list":
        client, uid, pid = _get_session_client()
        from integrations.supabase_portfolio import load_portfolio_state
        state = load_portfolio_state(pid, client=client)
        if not state or not state.get("positions"):
            print("暂无持仓记录")
            if state:
                print(f"可用资金: {state.get('free_cash', 0):,.2f}")
            return
        print(f"持仓 ({len(state['positions'])} 只)  可用资金: {state.get('free_cash', 0):,.2f}")
        print(f"{'代码':<8} {'名称':<10} {'股数':>6} {'成本':>8} {'买入日期':<10} {'止损':>8}")
        print("-" * 60)
        for p in state["positions"]:
            sl = f"{p.get('stop_loss', 0):.2f}" if p.get("stop_loss") else "-"
            print(f"{p['code']:<8} {p.get('name',''):<10} {p.get('shares',0):>6} {p.get('cost',0):>8.3f} {p.get('buy_dt',''):<10} {sl:>8}")
        return

    if sub == "add":
        client, uid, pid = _get_session_client()
        code = args.code
        if not code:
            print("用法: wyckoff portfolio add <code> --name X --shares N --cost N [--buy-dt YYYYMMDD]")
            sys.exit(1)
        from integrations.supabase_portfolio import upsert_position
        position = {
            "code": code,
            "name": args.name or "",
            "shares": args.shares or 0,
            "cost_price": args.cost or 0,
            "buy_dt": args.buy_dt or "",
        }
        ok, msg = upsert_position(pid, position, client=client)
        print(f"{'✓' if ok else '✗'} {msg}")
        return

    if sub == "rm":
        client, uid, pid = _get_session_client()
        code = args.code
        if not code:
            print("用法: wyckoff portfolio rm <code>")
            sys.exit(1)
        from integrations.supabase_portfolio import delete_position
        ok, msg = delete_position(pid, code, client=client)
        print(f"{'✓' if ok else '✗'} {msg}")
        return

    if sub == "cash":
        client, uid, pid = _get_session_client()
        amount = args.amount
        if amount is None:
            # 查看
            from integrations.supabase_portfolio import load_portfolio_state
            state = load_portfolio_state(pid, client=client)
            print(f"可用资金: {state.get('free_cash', 0):,.2f}" if state else "暂无记录")
            return
        from integrations.supabase_portfolio import update_free_cash
        ok, msg = update_free_cash(pid, float(amount), client=client)
        print(f"{'✓' if ok else '✗'} {msg}")
        return

    print(f"未知子命令: {sub}")
    print("用法: wyckoff portfolio [list|add|rm|cash]")
    sys.exit(1)


def _cmd_signal(args):
    status = args.status or "all"
    limit = args.limit or 30
    from agents.chat_tools import query_history
    result = query_history(source="signal", status=status, limit=limit)
    if result.get("error"):
        print(f"✗ {result['error']}")
        sys.exit(1)
    records = result.get("records", [])
    if not records:
        print(result.get("message", "暂无信号记录"))
        return
    counts = result.get("status_counts", {})
    print(f"信号确认池 ({result.get('total', 0)} 条)  " + "  ".join(f"{k}:{v}" for k, v in counts.items()))
    print(f"{'代码':<8} {'名称':<8} {'信号':<6} {'状态':<10} {'日期':<10} {'天数':>4} {'评分':>6} {'行业':<10}")
    print("-" * 75)
    for r in records:
        score = r.get("signal_score", 0)
        score_s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(
            f"{r['code']:<8} {r['name']:<8} {r['signal_type']:<6} {r['status']:<10} "
            f"{r['signal_date']:<10} {r.get('days_elapsed',0):>4} {score_s:>6} "
            f"{r.get('industry',''):<10}"
        )


def _cmd_recommend(args):
    limit = args.limit or 20
    from agents.chat_tools import query_history
    result = query_history(source="recommendation", limit=limit)
    if result.get("error"):
        print(f"✗ {result['error']}")
        sys.exit(1)
    records = result.get("records", [])
    if not records:
        print(result.get("message", "暂无推荐记录"))
        return
    print(f"AI 推荐跟踪 ({result.get('total', 0)} 条)")
    print(f"{'代码':<8} {'名称':<8} {'阵营':<8} {'推荐日':<10} {'推荐价':>8} {'现价':>8} {'盈亏%':>7} {'最高%':>7} {'状态':<8}")
    print("-" * 85)
    for r in records:
        code = str(r.get("code", "")).strip().zfill(6)
        rec_p = f"{r['recommend_price']:.2f}" if r.get("recommend_price") else "-"
        cur_p = f"{r['current_price']:.2f}" if r.get("current_price") else "-"
        pnl = f"{r['pnl_pct']:.1f}" if r.get("pnl_pct") is not None else "-"
        max_pnl = f"{r['max_pnl_pct']:.1f}" if r.get("max_pnl_pct") is not None else "-"
        print(
            f"{code:<8} {r['name']:<8} {r.get('camp',''):<8} {r['recommend_date']:<10} "
            f"{rec_p:>8} {cur_p:>8} {pnl:>7} {max_pnl:>7} {r.get('status',''):<8}"
        )


# ---------------------------------------------------------------------------
# wyckoff sync — 手动同步 Supabase → SQLite
# ---------------------------------------------------------------------------

def _cmd_sync(_args=None):
    from integrations.local_db import init_db, get_sync_meta
    init_db()

    if _args and getattr(_args, "sync_cmd", "") == "status":
        for tbl in ("recommendation_tracking", "signal_pending", "market_signal_daily", "portfolio"):
            meta = get_sync_meta(tbl)
            if meta:
                print(f"  {tbl}: {meta['row_count']} rows, last_synced={meta['last_synced_at']}")
            else:
                print(f"  {tbl}: 未同步")
        return

    from integrations.sync import sync_all
    print("正在同步 Supabase → 本地...")
    result = sync_all()
    if not result:
        print("无需同步（数据未过期或 Supabase 未配置）")
        return
    for tbl, count in result.items():
        status = f"{count} rows" if count >= 0 else "failed"
        print(f"  {tbl}: {status}")
    print("同步完成")


# ---------------------------------------------------------------------------
# TUI 启动
# ---------------------------------------------------------------------------

def _cmd_tui(_args=None):
    _check_update_async()

    from cli.tools import ToolRegistry
    tools = ToolRegistry()

    session_expired = False
    try:
        from cli.auth import restore_session, _load_session
        had_session = _load_session() is not None
        session = restore_session()
        if session:
            tools.state.update({
                "user_id": session["user_id"],
                "email": session["email"],
                "access_token": session.get("access_token", ""),
                "refresh_token": session.get("refresh_token", ""),
            })
            from core.stock_cache import set_cli_tokens
            set_cli_tokens(session.get("access_token", ""), session.get("refresh_token", ""))
        elif had_session:
            session_expired = True
    except Exception:
        pass

    # 初始化本地 SQLite + 后台同步
    try:
        from integrations.local_db import init_db, prune_memories
        init_db()
        prune_memories()
        from integrations.sync import sync_all_background
        sync_all_background()
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

    # --- 从 wyckoff.json 注入数据源 token ---
    try:
        from cli.auth import load_config
        _cfg = load_config()
        for _k, _env in [("tushare_token", "TUSHARE_TOKEN"), ("tickflow_api_key", "TICKFLOW_API_KEY")]:
            _v = str(_cfg.get(_k, "") or "").strip()
            if _v:
                os.environ.setdefault(_env, _v)
    except Exception:
        pass

    try:
        from cli.auth import load_model_configs, load_default_model_id
        configs = load_model_configs()
        default_id = load_default_model_id()
        if configs and default_id:
            env_map = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
            for cfg in configs:
                ek = env_map.get(cfg.get("provider_name", ""))
                if ek and cfg.get("api_key"):
                    os.environ.setdefault(ek, cfg["api_key"])
            default_cfg = next((c for c in configs if c["id"] == default_id), configs[0])
            if len(configs) == 1:
                provider, err = _create_provider(
                    default_cfg["provider_name"], default_cfg["api_key"],
                    default_cfg.get("model", ""), default_cfg.get("base_url", ""),
                )
                if not err:
                    state.update(default_cfg)
                    state["provider"] = provider
            else:
                from cli.providers.fallback import FallbackProvider
                state.update(default_cfg)
                state["provider"] = FallbackProvider(configs, default_id)
    except Exception:
        pass

    if state["provider"]:
        tools.set_provider(state["provider"])

    from cli.tui import WyckoffTUI
    app = WyckoffTUI(
        provider=state["provider"],
        tools=tools,
        state=state,
        system_prompt=system_prompt,
        session_expired=session_expired,
    )
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            import baostock as bs
            bs.logout()
        except (Exception, KeyboardInterrupt):
            pass
        try:
            _devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(_devnull, 1)
            os.dup2(_devnull, 2)
            os.close(_devnull)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Argparse 主入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="wyckoff",
        description="威科夫终端读盘室 — Wyckoff 量价分析 Agent",
    )
    parser.add_argument("-v", "--version", action="version", version=f"wyckoff {_get_version()}")
    sub = parser.add_subparsers(dest="cmd")

    # wyckoff update
    sub.add_parser("update", help="升级到最新版")

    # wyckoff auth
    p_auth = sub.add_parser("auth", help="登录/登出/状态")
    p_auth.add_argument("auth_cmd", help="email 或 logout/status")
    p_auth.add_argument("password", nargs="?", default="", help="密码（可省略，交互输入）")

    # wyckoff model
    p_model = sub.add_parser("model", help="模型管理")
    p_model.add_argument("model_cmd", nargs="?", default="list", help="list/add/set/rm/default")
    p_model.add_argument("model_id", nargs="?", default="", help="模型 ID")
    p_model.add_argument("provider", nargs="?", default="", help="供应商 (set 时)")
    p_model.add_argument("api_key", nargs="?", default="", help="API Key (set 时)")
    p_model.add_argument("--model", dest="model_name", default="", help="模型名")
    p_model.add_argument("--base-url", dest="base_url", default="", help="Base URL")

    # wyckoff config
    p_config = sub.add_parser("config", help="数据源配置")
    p_config.add_argument("config_cmd", nargs="?", default="", help="tushare/tickflow")
    p_config.add_argument("value", nargs="?", default="", help="值（可省略，交互输入）")

    # wyckoff portfolio
    p_port = sub.add_parser("portfolio", help="持仓管理", aliases=["pf"])
    p_port.add_argument("portfolio_cmd", nargs="?", default="list", help="list/add/rm/cash")
    p_port.add_argument("code", nargs="?", default="", help="股票代码 (add/rm 时)")
    p_port.add_argument("--name", default="", help="股票名称")
    p_port.add_argument("--shares", type=int, default=0, help="持仓数量")
    p_port.add_argument("--cost", type=float, default=0, help="成本价")
    p_port.add_argument("--buy-dt", dest="buy_dt", default="", help="买入日期 YYYYMMDD")
    p_port.add_argument("--amount", type=float, default=None, help="可用资金金额 (cash 时)")

    # wyckoff signal
    p_signal = sub.add_parser("signal", help="信号确认池")
    p_signal.add_argument("status", nargs="?", default="all", help="all/pending/confirmed/expired")
    p_signal.add_argument("-n", "--limit", type=int, default=30, help="返回条数")

    # wyckoff recommend
    p_rec = sub.add_parser("recommend", help="AI 推荐跟踪", aliases=["rec"])
    p_rec.add_argument("-n", "--limit", type=int, default=20, help="返回条数")

    # wyckoff dashboard / dash
    p_dash = sub.add_parser("dashboard", help="启动本地可视化面板", aliases=["dash"])
    p_dash.add_argument("--port", type=int, default=8765, help="HTTP 端口 (默认 8765)")

    # wyckoff sync
    p_sync = sub.add_parser("sync", help="同步 Supabase → 本地 SQLite")
    p_sync.add_argument("sync_cmd", nargs="?", default="", help="status: 查看同步状态")

    args = parser.parse_args()

    if args.cmd == "update":
        _cmd_update(args)
    elif args.cmd == "auth":
        _cmd_auth(args)
    elif args.cmd == "model":
        _cmd_model(args)
    elif args.cmd == "config":
        _cmd_config(args)
    elif args.cmd in ("portfolio", "pf"):
        _cmd_portfolio(args)
    elif args.cmd == "signal":
        _cmd_signal(args)
    elif args.cmd in ("recommend", "rec"):
        _cmd_recommend(args)
    elif args.cmd in ("dashboard", "dash"):
        from cli.dashboard import start_dashboard
        start_dashboard(port=args.port)
    elif args.cmd == "sync":
        _cmd_sync(args)
    else:
        _cmd_tui(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
