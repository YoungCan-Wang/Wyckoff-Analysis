"""
威科夫终端读盘室 — 入口。

用法:
    wyckoff                         # 启动 TUI
    wyckoff update                  # 升级到最新版
    wyckoff screen                  # 全市场漏斗筛选
    wyckoff backtest                # 策略历史回测
    wyckoff report 000001,600519    # AI 深度研报
    wyckoff mcp                     # 启动 MCP Server
    wyckoff memory                  # 查看 Agent 记忆
    wyckoff log                     # 查看对话日志
    wyckoff trace                   # 查看 JSONL 运行轨迹
    wyckoff dashboard               # 启动本地可视化面板
    wyckoff auth <email>            # 登录
    wyckoff model list/add/rm       # 模型管理
    wyckoff config                  # 数据源配置
    wyckoff portfolio list/add/rm   # 持仓管理
    wyckoff signal                  # 信号确认池
    wyckoff recommend               # AI 推荐跟踪
    wyckoff sync                    # 同步 Supabase → SQLite
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
    import inspect

    from cli.providers import PROVIDERS

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
                print(f"\033[33m⬆ 新版本可用: {latest}（当前 {local_ver}），运行 wyckoff update 升级\033[0m")
        except Exception:
            pass

    threading.Thread(target=_check, daemon=True).start()


def _mask(val: str) -> str:
    if len(val) > 8:
        return val[:4] + "****" + val[-4:]
    return "****" if val else ""


def _set_terminal_title(title: str) -> None:
    """Set the terminal tab title only for interactive terminals."""

    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except Exception:
        pass


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
        url = "https://wyckoff-analysis.pages.dev/"
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
    from cli.auth import _load_session, login, logout, restore_session

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
        load_default_model_id,
        load_model_configs,
        remove_model_entry,
        save_model_entry,
        set_default_model,
    )

    sub = args.model_cmd or "list"

    if sub == "list":
        configs = load_model_configs()
        default_id = load_default_model_id()
        from cli.auth import load_fallback_model_id

        fallback_id = load_fallback_model_id()
        if not configs:
            print("尚无模型配置，使用 wyckoff model add 添加")
            return
        for c in configs:
            marks = ""
            if c["id"] == default_id:
                marks += " *"
            if c["id"] == fallback_id:
                marks += " ⚡"
            print(
                f"  {c['id']}{marks}  provider={c.get('provider_name', '')}  model={c.get('model', '')}  base_url={c.get('base_url', '') or '(default)'}"
            )
        if fallback_id:
            print("\n  * = 默认  ⚡ = fallback")
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

        api_key = getpass.getpass("API Key (购买: https://www.1route.dev/register?aff=359904261): ").strip()
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

    if sub == "fallback":
        from cli.auth import load_fallback_model_id, set_fallback_model

        model_id = args.model_id
        if not model_id:
            current = load_fallback_model_id()
            print(f"当前 fallback: {current or '未设置（降级到所有模型）'}")
            return
        if model_id == "none":
            set_fallback_model("")
            print("✓ 已清除 fallback 设置（将降级到所有模型）")
            return
        configs = load_model_configs()
        if not any(c["id"] == model_id for c in configs):
            print(f"✗ 模型 {model_id} 不存在")
            sys.exit(1)
        set_fallback_model(model_id)
        print(f"✓ Fallback 模型已设为 {model_id}")
        return

    print(f"未知子命令: {sub}")
    print("用法: wyckoff model [list|add|set|rm|default|fallback]")
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
        for _alias, (key, label, _) in CONFIG_KEYS.items():
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
            print(
                f"{p['code']:<8} {p.get('name', ''):<10} {p.get('shares', 0):>6} {p.get('cost', 0):>8.3f} {p.get('buy_dt', ''):<10} {sl:>8}"
            )
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
            f"{r['signal_date']:<10} {r.get('days_elapsed', 0):>4} {score_s:>6} "
            f"{r.get('industry', ''):<10}"
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
    print(
        f"{'代码':<8} {'名称':<8} {'阵营':<8} {'推荐日':<10} {'推荐价':>8} {'现价':>8} {'盈亏%':>7} {'最高%':>7} {'状态':<8}"
    )
    print("-" * 85)
    for r in records:
        code = str(r.get("code", "")).strip().zfill(6)
        rec_p = f"{r['recommend_price']:.2f}" if r.get("recommend_price") else "-"
        cur_p = f"{r['current_price']:.2f}" if r.get("current_price") else "-"
        pnl = f"{r['pnl_pct']:.1f}" if r.get("pnl_pct") is not None else "-"
        max_pnl = f"{r['max_pnl_pct']:.1f}" if r.get("max_pnl_pct") is not None else "-"
        print(
            f"{code:<8} {r['name']:<8} {r.get('camp', ''):<8} {r['recommend_date']:<10} "
            f"{rec_p:>8} {cur_p:>8} {pnl:>7} {max_pnl:>7} {r.get('status', ''):<8}"
        )


# ---------------------------------------------------------------------------
# wyckoff screen — 漏斗筛选
# ---------------------------------------------------------------------------


def _cmd_screen(args):
    from integrations.local_db import init_db

    init_db()
    board = args.board or "all"
    print(f"正在执行全市场漏斗筛选 (board={board}) ...")
    from scripts.wyckoff_funnel import run_funnel_job

    try:
        triggers, metrics = run_funnel_job()
    except Exception as e:
        print(f"✗ 筛选失败: {e}")
        sys.exit(1)
    total = sum(len(v) for v in triggers.values())
    print(f"\n✓ 筛选完成  命中 {total} 只")
    for signal_type, items in triggers.items():
        if items:
            print(f"\n  [{signal_type}] ({len(items)} 只)")
            for code, score in items[:10]:
                print(f"    {code}  score={score:.2f}")
    if metrics:
        print(f"\n  指标: {json.dumps(metrics, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# wyckoff backtest — 策略回测
# ---------------------------------------------------------------------------


def _cmd_backtest(args):
    from datetime import date, timedelta

    from integrations.local_db import init_db

    init_db()

    end_dt = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=args.months * 30)
    print(f"正在回测 {start_dt} → {end_dt}  hold_days={args.hold_days} ...")
    from scripts.backtest_runner import run_backtest

    try:
        df, summary = run_backtest(
            start_dt=start_dt,
            end_dt=end_dt,
            hold_days=args.hold_days,
            top_n=args.top_n,
            board="all",
            sample_size=0,
            trading_days=60,
            max_workers=4,
        )
    except Exception as e:
        print(f"✗ 回测失败: {e}")
        sys.exit(1)
    print(f"\n✓ 回测完成  交易 {len(df)} 笔")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# wyckoff report — AI 研报
# ---------------------------------------------------------------------------


def _cmd_report(args):
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        print("用法: wyckoff report 000001,600519")
        sys.exit(1)
    from integrations.local_db import init_db

    init_db()
    # 需要 LLM
    from cli.auth import load_default_model_id, load_model_configs

    configs = load_model_configs()
    default_id = load_default_model_id()
    if not configs:
        print("✗ 未配置模型，请先 wyckoff model add")
        sys.exit(1)
    cfg = next((c for c in configs if c["id"] == default_id), configs[0])
    env_map = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    for mc in configs:
        ek = env_map.get(mc.get("provider_name", ""))
        if ek and mc.get("api_key"):
            os.environ.setdefault(ek, mc["api_key"])

    symbols_info = [{"code": c, "name": "", "tag": ""} for c in codes]
    print(f"正在生成 AI 研报 ({len(codes)} 只) ...")
    from scripts.step3_batch_report import run as run_report

    try:
        result = run_report(
            symbols_info=symbols_info,
            webhook_url="",
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""),
            notify=False,
            provider=cfg.get("provider_name", "gemini"),
            llm_base_url=cfg.get("base_url", ""),
        )
        print("✓ 研报生成完成")
        if isinstance(result, dict):
            for camp, stocks in result.items():
                if stocks:
                    print(f"\n  [{camp}]")
                    for s in stocks[:5]:
                        print(f"    {s}")
    except Exception as e:
        print(f"✗ 研报生成失败: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# wyckoff mcp — 启动 MCP Server
# ---------------------------------------------------------------------------


def _cmd_mcp(_args):
    print("启动 Wyckoff MCP Server ...")
    print("按 Ctrl+C 停止\n")
    from mcp_server import main as mcp_main

    try:
        mcp_main()
    except KeyboardInterrupt:
        print("\nMCP Server 已停止")


# ---------------------------------------------------------------------------
# wyckoff memory — 查看/清除 Agent 记忆
# ---------------------------------------------------------------------------


def _cmd_memory(args):
    from integrations.local_db import get_recent_memories, init_db, prune_memories

    init_db()
    sub = args.memory_cmd or "list"

    if sub == "list":
        mtype = args.type or None
        memories = get_recent_memories(memory_type=mtype, limit=args.limit)
        if not memories:
            print("暂无记忆记录")
            return
        print(f"Agent 记忆 ({len(memories)} 条)")
        print(f"{'ID':>5} {'类型':<12} {'日期':<20} {'内容'}")
        print("-" * 80)
        for m in memories:
            content = m.get("content", "")
            if len(content) > 60:
                content = content[:60] + "..."
            print(f"{m['id']:>5} {m.get('memory_type', ''):<12} {m.get('created_at', '')[:19]:<20} {content}")
        return

    if sub == "search":
        keyword = args.keyword
        if not keyword:
            print("用法: wyckoff memory search <关键词>")
            sys.exit(1)
        from integrations.local_db import search_memory

        results = search_memory(keyword=keyword, limit=args.limit)
        if not results:
            print(f"未找到包含 '{keyword}' 的记忆")
            return
        for m in results:
            content = m.get("content", "")
            if len(content) > 80:
                content = content[:80] + "..."
            print(f"  [{m['id']}] {m.get('memory_type', '')} | {content}")
        return

    if sub == "clear":
        count = prune_memories(keep_days=0)
        print(f"✓ 已清除 {count} 条过期记忆（preference 类型保留）")
        return

    if sub == "delete":
        mid = args.memory_id
        if not mid:
            print("用法: wyckoff memory delete <id>")
            sys.exit(1)
        from integrations.local_db import get_db

        conn = get_db()
        with conn:
            cur = conn.execute("DELETE FROM agent_memory WHERE id=?", (mid,))
        if cur.rowcount:
            print(f"✓ 已删除记忆 #{mid}")
        else:
            print(f"✗ 记忆 #{mid} 不存在")
        return

    print(f"未知子命令: {sub}")
    print("用法: wyckoff memory [list|search|clear|delete]")
    sys.exit(1)


# ---------------------------------------------------------------------------
# wyckoff log — 查看对话日志
# ---------------------------------------------------------------------------


def _cmd_log(args):
    from integrations.local_db import init_db, load_chat_logs

    init_db()
    session_id = args.session or None
    limit = args.limit
    logs = load_chat_logs(session_id=session_id, limit=limit)
    if not logs:
        print("暂无对话日志")
        return
    if session_id:
        print(f"会话 {session_id} 的对话日志 ({len(logs)} 条)")
    else:
        print(f"最近对话日志 ({len(logs)} 条)")
    print()
    for entry in logs[-limit:]:
        role = entry.get("role", "")
        ts = entry.get("created_at", "")[:19]
        content = entry.get("content", "")
        if isinstance(content, str) and len(content) > 120:
            content = content[:120] + "..."
        tag = {"user": "❯", "assistant": "◆", "tool": "⚙"}.get(role, "·")
        sid = entry.get("session_id", "")[:8]
        print(f"  {tag} [{ts}] ({sid}) {content}")


# ---------------------------------------------------------------------------
# wyckoff trace — 查看 append-only 运行轨迹
# ---------------------------------------------------------------------------


def _cmd_trace(args):
    from pathlib import Path

    from cli.scratchpad import wyckoff_home

    trace_dir = wyckoff_home() / "scratchpad"
    if args.path:
        print(trace_dir)
        return

    if args.show:
        target = Path(args.show).expanduser()
        if not target.is_absolute():
            target = trace_dir / target
        if not target.exists():
            print(f"轨迹文件不存在: {target}")
            sys.exit(1)
        text = target.read_text(encoding="utf-8", errors="replace")
        print(text[: args.chars])
        if len(text) > args.chars:
            print(f"\n... 已截断，完整文件: {target}")
        return

    files = (
        sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if trace_dir.exists() else []
    )
    if not files:
        print("暂无运行轨迹")
        return

    print(f"最近运行轨迹 ({min(args.limit, len(files))} 条)")
    print()
    for path in files[: args.limit]:
        query = ""
        try:
            first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            data = json.loads(first)
            query = str(data.get("content", ""))
        except Exception as exc:
            query = f"(无法读取摘要: {type(exc).__name__})"
        if len(query) > 80:
            query = query[:80] + "..."
        stamp = path.name.split("_", 1)[0]
        print(f"  [{stamp}] {path.name}  {query}")


# ---------------------------------------------------------------------------
# wyckoff sync — 手动同步 Supabase → SQLite
# ---------------------------------------------------------------------------


def _cmd_sync(_args=None):
    from integrations.local_db import get_sync_meta, init_db

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


def _cmd_cleanup(args):
    from integrations.local_db import cleanup_old_records, init_db

    init_db()
    days = args.days
    print(f"清理 {days} 天前的本地数据...")
    deleted = cleanup_old_records(days)
    for table, count in deleted.items():
        print(f"  {table}: 删除 {count} 条")
    total = sum(deleted.values())
    if total:
        print(f"共清理 {total} 条记录")
    else:
        print("无过期数据")


# ---------------------------------------------------------------------------
# TUI 启动
# ---------------------------------------------------------------------------


def _cmd_tui(_args=None):
    _check_update_async()

    from cli.tools import ToolRegistry

    tools = ToolRegistry()

    session_expired = False
    try:
        from cli.auth import _load_session, restore_session

        had_session = _load_session() is not None
        session = restore_session()
        if session:
            tools.state.update(
                {
                    "user_id": session["user_id"],
                    "email": session["email"],
                    "access_token": session.get("access_token", ""),
                    "refresh_token": session.get("refresh_token", ""),
                }
            )
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
        from cli.auth import load_default_model_id, load_model_configs

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
                    default_cfg["provider_name"],
                    default_cfg["api_key"],
                    default_cfg.get("model", ""),
                    default_cfg.get("base_url", ""),
                )
                if not err:
                    state.update(default_cfg)
                    state["provider"] = provider
            else:
                from cli.auth import load_fallback_model_id
                from cli.providers.fallback import FallbackProvider

                state.update(default_cfg)
                state["provider"] = FallbackProvider(configs, default_id, fallback_id=load_fallback_model_id())
    except Exception:
        pass

    if state["provider"]:
        tools.set_provider(state["provider"])

    # 后台启动 dashboard 并打开浏览器
    import threading
    import webbrowser

    def _dash_bg():
        try:
            from http.server import HTTPServer

            from cli.dashboard import _Handler

            HTTPServer(("127.0.0.1", 8765), _Handler).serve_forever()
        except Exception:
            pass

    threading.Thread(target=_dash_bg, daemon=True).start()
    webbrowser.open("http://127.0.0.1:8765")

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


def _dispatch_command(args) -> None:
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
    elif args.cmd == "screen":
        _cmd_screen(args)
    elif args.cmd in ("backtest", "bt"):
        _cmd_backtest(args)
    elif args.cmd == "report":
        _cmd_report(args)
    elif args.cmd == "mcp":
        _cmd_mcp(args)
    elif args.cmd in ("memory", "mem"):
        _cmd_memory(args)
    elif args.cmd == "log":
        _cmd_log(args)
    elif args.cmd == "trace":
        _cmd_trace(args)
    elif args.cmd == "sync":
        _cmd_sync(args)
    elif args.cmd == "cleanup":
        _cmd_cleanup(args)
    else:
        _cmd_tui(args)


def main():
    _set_terminal_title("Wyckoff-Analysis")

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

    # wyckoff screen
    p_screen = sub.add_parser("screen", help="全市场漏斗筛选")
    p_screen.add_argument("--board", default="all", help="板块 (all/main/gem/star)")

    # wyckoff backtest
    p_bt = sub.add_parser("backtest", help="策略历史回测", aliases=["bt"])
    p_bt.add_argument("--hold-days", type=int, default=15, help="持有天数 (默认 15)")
    p_bt.add_argument("--months", type=int, default=18, help="回测月数 (默认 18)")
    p_bt.add_argument("--top-n", type=int, default=5, help="每批取前N只 (默认 5)")

    # wyckoff report
    p_report = sub.add_parser("report", help="AI 深度研报")
    p_report.add_argument("codes", help="股票代码，逗号分隔 (如 000001,600519)")

    # wyckoff mcp
    sub.add_parser("mcp", help="启动 MCP Server")

    # wyckoff memory
    p_mem = sub.add_parser("memory", help="Agent 记忆管理", aliases=["mem"])
    p_mem.add_argument("memory_cmd", nargs="?", default="list", help="list/search/clear/delete")
    p_mem.add_argument("keyword", nargs="?", default="", help="搜索关键词 (search 时)")
    p_mem.add_argument("memory_id", nargs="?", default="", help="记忆 ID (delete 时)")
    p_mem.add_argument("--type", default="", help="过滤类型 (session/preference)")
    p_mem.add_argument("-n", "--limit", type=int, default=20, help="返回条数")

    # wyckoff log
    p_log = sub.add_parser("log", help="查看对话日志")
    p_log.add_argument("--session", default="", help="指定会话 ID")
    p_log.add_argument("-n", "--limit", type=int, default=30, help="返回条数")

    # wyckoff trace
    p_trace = sub.add_parser("trace", help="查看 JSONL 运行轨迹")
    p_trace.add_argument("-n", "--limit", type=int, default=10, help="返回条数")
    p_trace.add_argument("--show", default="", help="显示指定轨迹文件（文件名或绝对路径）")
    p_trace.add_argument("--chars", type=int, default=12000, help="--show 输出字符数上限")
    p_trace.add_argument("--path", action="store_true", help="只打印轨迹目录")

    # wyckoff sync
    p_sync = sub.add_parser("sync", help="同步 Supabase → 本地 SQLite")
    p_sync.add_argument("sync_cmd", nargs="?", default="", help="status: 查看同步状态")

    # wyckoff cleanup
    p_cleanup = sub.add_parser("cleanup", help="清理过期本地数据")
    p_cleanup.add_argument("--days", type=int, default=30, help="保留天数 (默认 30)")

    args = parser.parse_args()
    _dispatch_command(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
