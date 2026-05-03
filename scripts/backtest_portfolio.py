"""
组合级回测器 (Portfolio-Level Backtest)

基于 backtest_runner 的逐笔 trades_df，模拟同时持仓 N 只股票的组合净值曲线，
计算组合级的夏普比、卡玛比、最大回撤、信息比等风险调整指标。

用法:
    # 先跑单票回测生成 trades.csv
    python -m scripts.backtest_runner --start 2025-09-01 --end 2026-02-28 ...

    # 再跑组合回测
    python -m scripts.backtest_portfolio \
        --trades analysis/backtest/trades_20250901_20260228_h5_n3.csv \
        --benchmark-start 2025-09-01 --benchmark-end 2026-02-28 \
        --output-dir analysis/portfolio
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.backtester import (
    calc_cvar95_pct,
    calc_information_ratio,
    calc_sharpe_ratio,
    fmt_metric,
)


def build_portfolio_nav(
    trades_df: pd.DataFrame,
    *,
    initial_capital: float = 1_000_000.0,
    max_concurrent: int = 5,
    weight_mode: str = "equal",  # "equal" or "score"
) -> pd.DataFrame:
    """
    从逐笔交易记录构建组合每日净值曲线。

    逻辑：
    1. 按 signal_date 排序，每天最多建仓 max_concurrent 只。
    2. 等权分配资金（或按 score 加权）。
    3. 每只持仓在 exit_date 按 ret_pct 结算。
    4. 输出每日 NAV 时间序列。

    返回 DataFrame: columns = [date, nav, daily_ret_pct, cash, positions_count]
    """
    if trades_df.empty:
        return pd.DataFrame(columns=["date", "nav", "daily_ret_pct", "cash", "positions_count"])

    df = trades_df.copy()
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date
    df["exit_date"] = pd.to_datetime(df["exit_date"]).dt.date

    all_dates = sorted(set(df["signal_date"].tolist() + df["exit_date"].tolist()))
    if not all_dates:
        return pd.DataFrame(columns=["date", "nav", "daily_ret_pct", "cash", "positions_count"])

    # Build daily schedule
    nav_records: list[dict] = []
    cash = initial_capital
    active_positions: list[dict] = []  # {code, entry_capital, ret_pct, exit_date}

    for day in all_dates:
        # 1. Close positions that exit today
        closing = [p for p in active_positions if p["exit_date"] <= day]
        for pos in closing:
            realized = pos["entry_capital"] * (1.0 + pos["ret_pct"] / 100.0)
            cash += realized
        active_positions = [p for p in active_positions if p["exit_date"] > day]

        # 2. Open new positions from today's signals
        new_signals = df[df["signal_date"] == day].copy()
        if not new_signals.empty:
            slots_available = max(max_concurrent - len(active_positions), 0)
            if slots_available > 0:
                # Rank by score descending
                new_signals = new_signals.sort_values("score", ascending=False).head(slots_available)
                n_new = len(new_signals)

                if weight_mode == "score" and "score" in new_signals.columns:
                    scores = pd.to_numeric(new_signals["score"], errors="coerce").fillna(1.0)
                    total_score = scores.sum()
                    if total_score > 0:
                        weights = scores / total_score
                    else:
                        weights = pd.Series([1.0 / n_new] * n_new, index=new_signals.index)
                else:
                    weights = pd.Series([1.0 / n_new] * n_new, index=new_signals.index)

                # Allocate from available cash (use fraction of total capital per slot)
                allocable = cash * 0.95  # 保留 5% 现金缓冲
                for (_, row), w in zip(new_signals.iterrows(), weights):
                    entry_cap = allocable * float(w)
                    if entry_cap <= 0:
                        continue
                    cash -= entry_cap
                    active_positions.append(
                        {
                            "code": row.get("code", ""),
                            "track": row.get("track", ""),
                            "entry_capital": entry_cap,
                            "ret_pct": float(row.get("ret_pct", 0.0)),
                            "exit_date": row["exit_date"],
                        }
                    )

        # 3. Calculate NAV
        positions_value = sum(p["entry_capital"] for p in active_positions)
        nav = cash + positions_value
        prev_nav = nav_records[-1]["nav"] if nav_records else initial_capital
        daily_ret = (nav / prev_nav - 1.0) * 100.0 if prev_nav > 0 else 0.0

        nav_records.append(
            {
                "date": day,
                "nav": nav,
                "daily_ret_pct": daily_ret,
                "cash": cash,
                "positions_count": len(active_positions),
            }
        )

    return pd.DataFrame(nav_records)


def calc_portfolio_metrics(
    nav_df: pd.DataFrame,
    bench_daily_ret: pd.Series | None = None,
    initial_capital: float = 1_000_000.0,
) -> dict:
    """计算组合级风险调整指标。"""
    if nav_df.empty:
        return {}

    nav = pd.to_numeric(nav_df["nav"], errors="coerce").dropna()
    daily_ret = pd.to_numeric(nav_df["daily_ret_pct"], errors="coerce").dropna()

    total_ret_pct = (float(nav.iloc[-1]) / initial_capital - 1.0) * 100.0
    n_days = len(nav)
    ann_factor = 250.0 / max(n_days, 1)
    ann_ret_pct = total_ret_pct * ann_factor

    # Max drawdown from NAV curve
    peak = nav.cummax()
    dd = nav / peak - 1.0
    max_dd_pct = float(dd.min()) * 100.0 if not dd.empty else None

    # Sharpe from daily returns
    sharpe = calc_sharpe_ratio(daily_ret, periods_per_year=250.0)
    calmar = None
    if max_dd_pct is not None and max_dd_pct < 0:
        calmar = ann_ret_pct / abs(max_dd_pct)

    ir = calc_information_ratio(daily_ret, bench_daily_ret, periods_per_year=250.0)
    var95, cvar95 = calc_cvar95_pct(daily_ret)

    # Positions stats
    pos_counts = pd.to_numeric(nav_df.get("positions_count"), errors="coerce").dropna()

    return {
        "total_return_pct": total_ret_pct,
        "annualized_return_pct": ann_ret_pct,
        "max_drawdown_pct": max_dd_pct,
        "sharpe_ratio": sharpe,
        "calmar_ratio": calmar,
        "information_ratio": ir,
        "var95_daily_pct": var95,
        "cvar95_daily_pct": cvar95,
        "trading_days": n_days,
        "avg_positions": float(pos_counts.mean()) if not pos_counts.empty else 0,
        "max_positions": int(pos_counts.max()) if not pos_counts.empty else 0,
        "final_nav": float(nav.iloc[-1]),
    }


def _build_portfolio_md(metrics: dict, nav_df: pd.DataFrame) -> str:
    lines = [
        "# 组合级回测结果 (Portfolio Backtest)",
        "",
        "## 组合收益",
        f"- 总收益: {fmt_metric(metrics.get('total_return_pct'), 3)}%",
        f"- 年化收益: {fmt_metric(metrics.get('annualized_return_pct'), 3)}%",
        f"- 最终净值: {fmt_metric(metrics.get('final_nav'), 2)}",
        f"- 交易日数: {metrics.get('trading_days', 0)}",
        "",
        "## 风险调整指标",
        f"- 夏普比 (Sharpe): {fmt_metric(metrics.get('sharpe_ratio'), 3)}",
        f"- 卡玛比 (Calmar): {fmt_metric(metrics.get('calmar_ratio'), 3)}",
        f"- 信息比 (IR vs Benchmark): {fmt_metric(metrics.get('information_ratio'), 3)}",
        f"- 最大回撤: {fmt_metric(metrics.get('max_drawdown_pct'), 3)}%",
        f"- VaR95(日): {fmt_metric(metrics.get('var95_daily_pct'), 3)}%",
        f"- CVaR95(日): {fmt_metric(metrics.get('cvar95_daily_pct'), 3)}%",
        "",
        "## 持仓统计",
        f"- 平均持仓数: {fmt_metric(metrics.get('avg_positions'), 1)}",
        f"- 最大同时持仓: {metrics.get('max_positions', 0)}",
        "",
    ]

    # NAV 摘要（首尾 + 最低点）
    if not nav_df.empty:
        nav_s = pd.to_numeric(nav_df["nav"], errors="coerce")
        if not nav_s.empty:
            min_idx = nav_s.idxmin()
            lines.extend(
                [
                    "## 净值关键节点",
                    f"- 起点: {nav_df.iloc[0]['date']} | NAV={nav_s.iloc[0]:.2f}",
                    f"- 最低: {nav_df.iloc[min_idx]['date']} | NAV={nav_s.iloc[min_idx]:.2f}",
                    f"- 终点: {nav_df.iloc[-1]['date']} | NAV={nav_s.iloc[-1]:.2f}",
                    "",
                ]
            )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="组合级回测")
    parser.add_argument("--trades", required=True, help="backtest_runner 输出的 trades CSV")
    parser.add_argument("--output-dir", default="analysis/portfolio")
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--max-concurrent", type=int, default=5, help="最大同时持仓数")
    parser.add_argument("--weight-mode", choices=["equal", "score"], default="equal")
    args = parser.parse_args()

    trades_path = Path(args.trades).resolve()
    if not trades_path.exists():
        print(f"[portfolio] trades 文件不存在: {trades_path}")
        return 1

    trades_df = pd.read_csv(trades_path, dtype={"code": str})
    print(f"[portfolio] 加载 {len(trades_df)} 笔交易")

    nav_df = build_portfolio_nav(
        trades_df,
        initial_capital=args.initial_capital,
        max_concurrent=args.max_concurrent,
        weight_mode=args.weight_mode,
    )
    print(f"[portfolio] 净值曲线: {len(nav_df)} 个交易日")

    metrics = calc_portfolio_metrics(nav_df, initial_capital=args.initial_capital)

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    nav_path = out_dir / f"portfolio_nav_{stamp}.csv"
    nav_df.to_csv(nav_path, index=False, encoding="utf-8-sig")

    md_content = _build_portfolio_md(metrics, nav_df)
    md_path = out_dir / f"portfolio_summary_{stamp}.md"
    md_path.write_text(md_content + "\n", encoding="utf-8")

    print(md_content)
    print(f"\n[portfolio] NAV CSV -> {nav_path}")
    print(f"[portfolio] Summary -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
