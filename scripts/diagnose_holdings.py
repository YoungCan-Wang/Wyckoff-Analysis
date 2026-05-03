"""
独立持仓健康诊断 CLI 工具

用法示例:
    # 命令行传入代码与成本
    python3 scripts/diagnose_holdings.py --codes 300813,600703,300014 --costs 30.695,13.68,67.12

    # 从 Supabase 读取实盘持仓（格式为 USER_LIVE:<user_id>）
    python3 scripts/diagnose_holdings.py --from-portfolio USER_LIVE:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

    # 输出 JSON 格式
    python3 scripts/diagnose_holdings.py --codes 300813 --costs 30.695 --format json

    # 指定股票名称（可选，不传则自动查询为 "--"）
    python3 scripts/diagnose_holdings.py --codes 300813,600703 --costs 30.695,13.68 --names 菲沃泰,三安光电
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.holding_diagnostic import (
    HoldingDiagnostic,
    diagnose_holdings,
    format_diagnostic_text,
)
from core.wyckoff_engine import FunnelConfig, normalize_hist_from_fetch
from integrations.data_source import fetch_index_hist
from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window
from utils.trading_clock import resolve_end_calendar_day

TRADING_DAYS = 320


def _fetch_stock_data(code: str, window) -> tuple[str, pd.DataFrame | None]:
    """拉取单只股票 OHLCV 数据，返回 (code, df_or_None)。"""

    symbol = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
    try:
        raw = _fetch_hist(symbol, window, adjust="qfq")
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            return code, None
        df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
        return code, df
    except Exception as e:
        print(f"  ⚠ {code} 数据拉取失败: {e}", file=sys.stderr)
        return code, None


def _fetch_benchmark(window) -> pd.DataFrame | None:
    """拉取上证指数作为基准。"""

    try:
        bench_raw = fetch_index_hist("000001", window.start_trade_date, window.end_trade_date)
        if bench_raw is None or bench_raw.empty:
            return None
        bench_df = normalize_hist_from_fetch(bench_raw).sort_values("date").reset_index(drop=True)
        return bench_df
    except Exception as e:
        print(f"  ⚠ 基准指数拉取失败: {e}", file=sys.stderr)
        return None


def _load_from_supabase(portfolio_id: str) -> list[tuple[str, str, float]]:
    """从 Supabase 读取实盘持仓，返回 [(code, name, cost), ...]。"""
    try:
        from integrations.supabase_portfolio import is_supabase_configured, load_portfolio_state

        if not is_supabase_configured():
            print("  ✘ Supabase 未配置（缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY）", file=sys.stderr)
            sys.exit(1)

        state = load_portfolio_state(portfolio_id)
        if state is None:
            print(f"  ✘ 未找到组合 {portfolio_id}", file=sys.stderr)
            sys.exit(1)

        positions = state.get("positions", [])
        if not positions:
            print(f"  ✘ 组合 {portfolio_id} 无持仓", file=sys.stderr)
            sys.exit(1)

        holdings = []
        for pos in positions:
            code = pos.get("code", "").strip()
            name = pos.get("name", "--").strip()
            cost = float(pos.get("cost", 0.0))
            if code and cost > 0:
                holdings.append((code, name, cost))
        return holdings
    except ImportError as e:
        print(f"  ✘ 依赖缺失: {e}", file=sys.stderr)
        sys.exit(1)


def _format_json(diagnostics: list[HoldingDiagnostic]) -> str:
    """输出 JSON 格式的诊断结果。"""
    results = []
    for d in diagnostics:
        data = asdict(d)
        # 确保浮点数可读
        for k, v in data.items():
            if isinstance(v, float):
                data[k] = round(v, 4)
        results.append(data)
    return json.dumps(results, ensure_ascii=False, indent=2)


def _format_markdown(diagnostics: list[HoldingDiagnostic]) -> str:
    """输出 Markdown 格式的诊断结果。"""
    lines = ["# 持仓健康诊断报告", ""]
    lines.append(f"诊断时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 总览表格
    lines.append("## 总览")
    lines.append("")
    lines.append("| 代码 | 名称 | 健康 | 盈亏% | 通道 | 均线 | 止损状态 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    for d in diagnostics:
        lines.append(
            f"| {d.code} | {d.name} | {d.health} | {d.pnl_pct:+.2f}% "
            f"| {d.l2_channel} | {d.ma_pattern} | {d.stop_loss_status} |"
        )
    lines.append("")

    # 详细诊断
    lines.append("## 详细诊断")
    lines.append("")
    for d in diagnostics:
        lines.append(f"### {d.code} {d.name}")
        lines.append("```")
        lines.append(format_diagnostic_text(d))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="持仓健康诊断 CLI — 基于 Wyckoff 引擎的结构化诊断",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 scripts/diagnose_holdings.py --codes 300813,600703 --costs 30.695,13.68
  python3 scripts/diagnose_holdings.py --from-portfolio USER_LIVE
  python3 scripts/diagnose_holdings.py --codes 300813 --costs 30.695 --format markdown
        """,
    )
    parser.add_argument(
        "--codes",
        type=str,
        default="",
        help="逗号分隔的股票代码，如 300813,600703,300014",
    )
    parser.add_argument(
        "--costs",
        type=str,
        default="",
        help="逗号分隔的持仓成本，与 --codes 一一对应",
    )
    parser.add_argument(
        "--names",
        type=str,
        default="",
        help="逗号分隔的股票名称（可选），与 --codes 一一对应",
    )
    parser.add_argument(
        "--from-portfolio",
        type=str,
        default="",
        help="从 Supabase 读取持仓，格式 USER_LIVE:<user_id>",
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=["text", "markdown", "json"],
        default="text",
        help="输出格式：text（默认）、markdown、json",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="",
        help="输出到文件（不指定则输出到终端）",
    )

    args = parser.parse_args()

    # ── 解析持仓来源 ──
    holdings: list[tuple[str, str, float]] = []

    if args.from_portfolio:
        print(f"📂 从 Supabase 读取持仓: {args.from_portfolio}")
        holdings = _load_from_supabase(args.from_portfolio)
    elif args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        costs_raw = [c.strip() for c in args.costs.split(",") if c.strip()]
        names_raw = [n.strip() for n in args.names.split(",") if n.strip()] if args.names else []

        if len(codes) != len(costs_raw):
            print(f"  ✘ --codes ({len(codes)}个) 与 --costs ({len(costs_raw)}个) 数量不匹配", file=sys.stderr)
            sys.exit(1)

        for i, (code, cost_str) in enumerate(zip(codes, costs_raw)):
            try:
                cost = float(cost_str)
            except ValueError:
                print(f"  ✘ 成本价格式错误: {cost_str}", file=sys.stderr)
                sys.exit(1)
            name = names_raw[i] if i < len(names_raw) else "--"
            holdings.append((code, name, cost))
    else:
        parser.print_help()
        sys.exit(1)

    if not holdings:
        print("  ✘ 无有效持仓可诊断", file=sys.stderr)
        sys.exit(1)

    print(f"\n🔍 开始诊断 {len(holdings)} 只持仓...")

    # ── 准备数据窗口 ──
    end_day = resolve_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)
    print(f"  数据窗口: {window.start_trade_date} → {window.end_trade_date}")

    # ── 拉取基准 ──
    print("  拉取基准指数 (上证指数)...")
    bench_df = _fetch_benchmark(window)

    # ── 拉取个股数据 ──
    df_map: dict[str, pd.DataFrame] = {}
    for code, name, _cost in holdings:
        print(f"  拉取 {code} {name}...")
        _, df = _fetch_stock_data(code, window)
        if df is not None:
            df_map[code] = df

    # ── 执行诊断 ──
    print("\n⚙ 执行 Wyckoff 健康诊断...\n")
    cfg = FunnelConfig()
    diagnostics = diagnose_holdings(holdings, df_map, bench_df, cfg)

    # ── 输出结果 ──
    if args.format == "json":
        output = _format_json(diagnostics)
    elif args.format == "markdown":
        output = _format_markdown(diagnostics)
    else:
        # text 格式
        separator = "─" * 60
        parts = []
        parts.append(f"\n{'═' * 60}")
        parts.append(f"  持仓健康诊断报告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        parts.append(f"{'═' * 60}\n")
        for d in diagnostics:
            parts.append(format_diagnostic_text(d))
            parts.append(separator)

        # 总览统计
        healthy = sum(1 for d in diagnostics if "健康" in d.health)
        warning = sum(1 for d in diagnostics if "警戒" in d.health)
        danger = sum(1 for d in diagnostics if "危险" in d.health)
        parts.append(f"\n📊 总览: 🟢健康 {healthy} | 🟡警戒 {warning} | 🔴危险 {danger}")

        avg_pnl = sum(d.pnl_pct for d in diagnostics) / len(diagnostics) if diagnostics else 0
        parts.append(f"📈 平均盈亏: {avg_pnl:+.2f}%")
        parts.append("")

        output = "\n".join(parts)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"✅ 结果已写入 {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
