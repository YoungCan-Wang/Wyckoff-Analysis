"""
参数敏感性分析 (Grid Search)

遍历 hold_days × stop_loss × take_profit × top_n 的参数空间，
调用 backtest_runner.run_backtest() 各跑一轮，
输出 CSV heatmap + 最优参数组合 markdown。

用法:
    python -m scripts.param_sensitivity \
        --start 2025-09-01 --end 2026-02-28 \
        --snapshot-dir data/backtest_snapshots/20260301 \
        --output-dir analysis/sensitivity
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.backtester import parse_date, run_backtest

# ── 默认参数空间（可通过环境变量 JSON 覆盖） ──

DEFAULT_HOLD_DAYS_GRID = [15, 30, 45, 60]
DEFAULT_STOP_LOSS_GRID = [0.0, -5.0, -8.0, -12.0]  # 0 = 不设止损
DEFAULT_TAKE_PROFIT_GRID = [0.0, 8.0, 15.0, 25.0]  # 0 = 不设止盈
DEFAULT_TRAILING_STOP_GRID = [0.0, -5.0, -8.0, -12.0]  # 0 = 不启用移动止盈
DEFAULT_TRAILING_ACTIVATE_GRID = [0.0, 10.0, 15.0]  # 移动止盈激活门槛(%)
DEFAULT_TOP_N_GRID = [3, 5, 8]


def _load_grid(env_key: str, default: list) -> list:
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return default


def run_sensitivity(
    start_dt,
    end_dt,
    *,
    board: str = "all",
    sample_size: int = 300,
    trading_days: int = 320,
    max_workers: int = 8,
    snapshot_dir: Path | None = None,
    hold_days_grid: list[int] | None = None,
    stop_loss_grid: list[float] | None = None,
    take_profit_grid: list[float] | None = None,
    trailing_stop_grid: list[float] | None = None,
    trailing_activate_grid: list[float] | None = None,
    top_n_grid: list[int] | None = None,
    exit_mode: str = "sltp",
) -> pd.DataFrame:
    """遍历参数空间并返回汇总 DataFrame。"""

    hd_grid = hold_days_grid or _load_grid("SENSITIVITY_HOLD_DAYS", DEFAULT_HOLD_DAYS_GRID)
    sl_grid = stop_loss_grid or _load_grid("SENSITIVITY_STOP_LOSS", DEFAULT_STOP_LOSS_GRID)
    tp_grid = take_profit_grid or _load_grid("SENSITIVITY_TAKE_PROFIT", DEFAULT_TAKE_PROFIT_GRID)
    ts_grid = trailing_stop_grid or _load_grid("SENSITIVITY_TRAILING_STOP", DEFAULT_TRAILING_STOP_GRID)
    ta_grid = trailing_activate_grid or _load_grid("SENSITIVITY_TRAILING_ACTIVATE", DEFAULT_TRAILING_ACTIVATE_GRID)
    tn_grid = top_n_grid or _load_grid("SENSITIVITY_TOP_N", DEFAULT_TOP_N_GRID)

    combos = list(itertools.product(hd_grid, sl_grid, tp_grid, ts_grid, ta_grid, tn_grid))
    print(
        f"[sensitivity] 参数空间: {len(hd_grid)}×{len(sl_grid)}×{len(tp_grid)}×{len(ts_grid)}×{len(ta_grid)}×{len(tn_grid)} = {len(combos)} 组合"
    )

    rows: list[dict] = []
    for idx, (hd, sl, tp, ts, ta, tn) in enumerate(combos, 1):
        hd = int(hd)
        sl = float(sl)
        tp = float(tp)
        ts = float(ts)
        ta = float(ta)
        tn = int(tn)
        label = f"hd={hd}_sl={sl}_tp={tp}_ts={ts}_ta={ta}_tn={tn}"
        print(f"\n[sensitivity] ({idx}/{len(combos)}) {label}")
        try:
            _, summary = run_backtest(
                start_dt=start_dt,
                end_dt=end_dt,
                hold_days=hd,
                top_n=tn,
                board=board,
                sample_size=sample_size,
                trading_days=trading_days,
                max_workers=max_workers,
                snapshot_dir=snapshot_dir,
                exit_mode=exit_mode,
                stop_loss_pct=sl,
                take_profit_pct=tp,
                trailing_stop_pct=ts,
                trailing_activate_pct=ta,
            )
            row = {
                "hold_days": hd,
                "stop_loss_pct": sl,
                "take_profit_pct": tp,
                "trailing_stop_pct": ts,
                "trailing_activate_pct": ta,
                "top_n": tn,
                "trades": summary.get("trades", 0),
                "win_rate_pct": summary.get("win_rate_pct"),
                "avg_ret_pct": summary.get("avg_ret_pct"),
                "median_ret_pct": summary.get("median_ret_pct"),
                "max_drawdown_pct": summary.get("max_drawdown_pct"),
                "sharpe_ratio": summary.get("sharpe_ratio"),
                "calmar_ratio": summary.get("calmar_ratio"),
                "var95_ret_pct": summary.get("var95_ret_pct"),
                "cvar95_ret_pct": summary.get("cvar95_ret_pct"),
                "max_consecutive_losses": summary.get("max_consecutive_losses"),
            }
            # 分层数据
            strat = summary.get("stratified", {})
            for track in ["Trend", "Accum"]:
                track_stats = strat.get("by_track", {}).get(track, {})
                row[f"{track}_trades"] = track_stats.get("trades", 0)
                row[f"{track}_win_rate"] = track_stats.get("win_rate_pct")
                row[f"{track}_avg_ret"] = track_stats.get("avg_ret_pct")
                row[f"{track}_sharpe"] = track_stats.get("sharpe_ratio")
            rows.append(row)
            print(
                f"  -> trades={row['trades']}, win={row.get('win_rate_pct', '-')}%, sharpe={row.get('sharpe_ratio', '-')}"
            )
        except Exception as exc:
            print(f"  -> FAILED: {exc}")
            traceback.print_exc()
            rows.append(
                {
                    "hold_days": hd,
                    "stop_loss_pct": sl,
                    "take_profit_pct": tp,
                    "trailing_stop_pct": ts,
                    "trailing_activate_pct": ta,
                    "top_n": tn,
                    "trades": 0,
                    "error": str(exc),
                }
            )

    return pd.DataFrame(rows)


def _build_sensitivity_md(df: pd.DataFrame) -> str:
    lines = [
        "# 参数敏感性分析结果",
        "",
        f"- 参数组合总数: {len(df)}",
        f"- 有效组合数: {len(df[df['trades'] > 0])}",
        "",
    ]

    valid = df[df["trades"] > 0].copy()
    if valid.empty:
        lines.append("⚠️ 无有效回测结果")
        return "\n".join(lines)

    # 最优参数（按夏普比）
    sharpe_col = pd.to_numeric(valid.get("sharpe_ratio"), errors="coerce")
    if sharpe_col.notna().any():
        best_idx = sharpe_col.idxmax()
        best = valid.loc[best_idx]
        lines.extend(
            [
                "## 最优参数（按夏普比）",
                "",
                f"- hold_days: **{int(best['hold_days'])}**",
                f"- stop_loss: **{best['stop_loss_pct']}%**",
                f"- take_profit: **{best['take_profit_pct']}%**",
                f"- trailing_stop: **{best.get('trailing_stop_pct', 0)}%**",
                f"- trailing_activate: **{best.get('trailing_activate_pct', 0)}%**",
                f"- top_n: **{int(best['top_n'])}**",
                f"- 夏普比: **{best.get('sharpe_ratio', '-')}**",
                f"- 胜率: {best.get('win_rate_pct', '-')}%",
                f"- 平均收益: {best.get('avg_ret_pct', '-')}%",
                f"- 最大回撤: {best.get('max_drawdown_pct', '-')}%",
                "",
            ]
        )

    # Top 10
    if sharpe_col.notna().any():
        top10 = valid.nlargest(10, "sharpe_ratio")
        lines.extend(["## Top 10 参数组合（按夏普比）", ""])
        lines.append("| 排名 | hold | SL | TP | topN | 笔数 | 胜率 | 均收 | 夏普 | 卡玛 | MDD |")
        lines.append("|------|------|-----|-----|------|------|------|------|------|------|------|")
        for rank, (_, r) in enumerate(top10.iterrows(), 1):

            def _f(v, n=2):
                return f"{v:.{n}f}" if pd.notna(v) else "-"

            lines.append(
                f"| {rank} | {int(r['hold_days'])} | {r['stop_loss_pct']} | {r['take_profit_pct']} "
                f"| {int(r['top_n'])} | {int(r['trades'])} | {_f(r.get('win_rate_pct'))} "
                f"| {_f(r.get('avg_ret_pct'), 3)} | {_f(r.get('sharpe_ratio'), 3)} "
                f"| {_f(r.get('calmar_ratio'), 3)} | {_f(r.get('max_drawdown_pct'), 3)} |"
            )
        lines.append("")

    # 各维度敏感性
    for dim_name, dim_col in [
        ("hold_days", "hold_days"),
        ("stop_loss_pct", "stop_loss_pct"),
        ("take_profit_pct", "take_profit_pct"),
        ("top_n", "top_n"),
    ]:
        if dim_col not in valid.columns:
            continue
        grouped = (
            valid.groupby(dim_col)
            .agg(
                trades=("trades", "sum"),
                avg_sharpe=("sharpe_ratio", "mean"),
                avg_win_rate=("win_rate_pct", "mean"),
                avg_ret=("avg_ret_pct", "mean"),
            )
            .reset_index()
        )
        lines.extend([f"## 敏感性：{dim_name}", ""])
        lines.append(f"| {dim_name} | 总笔数 | 平均夏普 | 平均胜率 | 平均收益 |")
        lines.append("|----------|--------|---------|---------|---------|")
        for _, r in grouped.iterrows():

            def _f2(v, n=3):
                return f"{v:.{n}f}" if pd.notna(v) else "-"

            lines.append(
                f"| {r[dim_col]} | {int(r['trades'])} | {_f2(r.get('avg_sharpe'))} "
                f"| {_f2(r.get('avg_win_rate'), 2)} | {_f2(r.get('avg_ret'))} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wyckoff 参数敏感性分析")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--board", default="main_chinext")
    parser.add_argument("--sample-size", type=int, default=300)
    parser.add_argument("--trading-days", type=int, default=320)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--snapshot-dir", default="")
    parser.add_argument("--output-dir", default="analysis/sensitivity")
    parser.add_argument("--exit-mode", default="sltp", choices=["close_only", "sltp"])
    args = parser.parse_args()

    start_dt = parse_date(args.start)
    end_dt = parse_date(args.end)
    snapshot = Path(args.snapshot_dir).resolve() if args.snapshot_dir.strip() else None

    result_df = run_sensitivity(
        start_dt,
        end_dt,
        board=args.board,
        sample_size=args.sample_size,
        trading_days=args.trading_days,
        max_workers=args.workers,
        snapshot_dir=snapshot,
        exit_mode=args.exit_mode,
    )

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    csv_path = out_dir / f"sensitivity_{stamp}.csv"
    result_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    md_path = out_dir / f"sensitivity_{stamp}.md"
    md_content = _build_sensitivity_md(result_df)
    md_path.write_text(md_content + "\n", encoding="utf-8")

    print(f"\n[sensitivity] CSV -> {csv_path}")
    print(f"[sensitivity] MD  -> {md_path}")
    print(md_content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
