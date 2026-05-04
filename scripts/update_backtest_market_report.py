#!/usr/bin/env python3
"""Build the persistent market-cycle backtest report from grid artifacts."""

from __future__ import annotations

import argparse
import csv
import glob
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median

REGIME_LABELS = {
    "CRASH": "下跌/踩踏期",
    "PANIC_REPAIR": "恐慌修复期",
    "RISK_OFF": "防守/风险偏好收缩期",
    "NEUTRAL": "震荡中性期",
    "RISK_ON": "风险偏好扩张期",
}


@dataclass(frozen=True)
class GridCell:
    artifact_dir: Path
    summary_path: Path
    trades_path: Path | None
    hold: int
    stop_loss: int
    take_profit: int
    trailing_stop: int
    start: str
    end: str
    top_n: str
    board: str
    sample_size: str
    trades: int | None
    win_rate: float | None
    avg_ret: float | None
    median_ret: float | None
    max_drawdown: float | None
    sharpe: float | None
    calmar: float | None
    total_return: float | None
    wbt_sharpe: float | None
    wbt_max_drawdown: float | None
    wbt_daily_win_rate: float | None
    metrics_engine: str


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace("%", "")
    text = text.replace("（wbt 可用）", "").replace("（wbt 不可用，已保留 legacy 指标）", "")
    if not text or text in {"-", "None", "nan"}:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _to_int(raw: str | None) -> int | None:
    val = _to_float(raw)
    return int(val) if val is not None else None


def _extract_line_value(content: str, label_pattern: str) -> str | None:
    pattern = re.compile(rf"^\s*-\s*{label_pattern}\s*:\s*(.+?)\s*$")
    for line in content.splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return None


def _extract_float(content: str, label_pattern: str) -> float | None:
    return _to_float(_extract_line_value(content, label_pattern))


def _extract_int(content: str, label_pattern: str) -> int | None:
    return _to_int(_extract_line_value(content, label_pattern))


def _parse_range(content: str) -> tuple[str, str]:
    raw = _extract_line_value(content, "区间")
    if not raw:
        return "", ""
    parts = [p.strip() for p in raw.split("~", 1)]
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def _parse_simple_field(content: str, label: str) -> str:
    raw = _extract_line_value(content, re.escape(label))
    return raw or ""


def _parse_board_sample(content: str) -> tuple[str, str]:
    raw = _parse_simple_field(content, "股票池")
    if not raw:
        return "", ""
    m = re.match(r"(.+?)\s*\(sample=(.+?)\)", raw)
    if not m:
        return raw, ""
    return m.group(1).strip(), m.group(2).strip()


def _parse_params(dirname: str) -> tuple[int, int, int, int] | None:
    # Supports both GitHub artifact names and local output dirs:
    # backtest-grid-h15-sl-6-tp0-tr0-25, h15_sl6_tp0_tr0.
    m = re.search(
        r"h(?P<hold>\d+).*?sl-?(?P<sl>\d+).*?tp(?P<tp>\d+).*?tr-?(?P<tr>\d+)",
        dirname,
    )
    if not m:
        return None
    return (
        int(m.group("hold")),
        int(m.group("sl")),
        int(m.group("tp")),
        int(m.group("tr")),
    )


def _find_trades_path(summary_path: Path) -> Path | None:
    matches = sorted(summary_path.parent.glob("trades_*.csv"))
    return matches[0] if matches else None


def load_grid_cells(artifacts_dir: Path) -> list[GridCell]:
    summaries = sorted(Path(p) for p in glob.glob(str(artifacts_dir / "**" / "summary_*.md"), recursive=True))
    cells: list[GridCell] = []
    for summary_path in summaries:
        params = _parse_params(summary_path.parent.name)
        if not params:
            continue
        hold, stop_loss, take_profit, trailing_stop = params
        content = summary_path.read_text(encoding="utf-8")
        start, end = _parse_range(content)
        board, sample_size = _parse_board_sample(content)
        metrics_engine = _parse_simple_field(content, "绩效引擎")
        cells.append(
            GridCell(
                artifact_dir=summary_path.parent,
                summary_path=summary_path,
                trades_path=_find_trades_path(summary_path),
                hold=hold,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop=trailing_stop,
                start=start,
                end=end,
                top_n=_parse_simple_field(content, "每日候选上限").replace("Top", "").strip(),
                board=board,
                sample_size=sample_size,
                trades=_extract_int(content, "成交样本"),
                win_rate=_extract_float(content, "胜率"),
                avg_ret=_extract_float(content, "平均收益"),
                median_ret=_extract_float(content, "中位收益"),
                max_drawdown=_extract_float(content, "最大回撤"),
                sharpe=_extract_float(content, r"夏普比(?:\s*\(Sharpe Ratio\))?"),
                calmar=_extract_float(content, r"卡玛比(?:\s*\(Calmar Ratio\))?"),
                total_return=_extract_float(content, "组合总收益"),
                wbt_sharpe=_extract_float(content, "wbt 夏普比"),
                wbt_max_drawdown=_extract_float(content, "wbt 最大回撤"),
                wbt_daily_win_rate=_extract_float(content, "wbt 日胜率"),
                metrics_engine=metrics_engine,
            )
        )
    return cells


def _read_trades(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _safe_median(values: list[float]) -> float | None:
    return median(values) if values else None


def _pct(num: int, den: int) -> float | None:
    return num / den * 100.0 if den else None


def _fmt_num(value: float | int | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return "-"
    return f"{value:.{digits}f}{suffix}"


def _fmt_signed(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:+.{digits}f}{suffix}"


def _fmt_param(cell: GridCell) -> str:
    tp = f"TP{cell.take_profit}%" if cell.take_profit else "无TP"
    tr = f"Trail-{cell.trailing_stop}%" if cell.trailing_stop else "无Trail"
    return f"{cell.hold}天 / SL-{cell.stop_loss}% / {tp} / {tr}"


def _cell_sort_key(cell: GridCell) -> float:
    return cell.sharpe if cell.sharpe is not None else float("-inf")


def _build_matrix(cells: list[GridCell], best: GridCell) -> list[str]:
    holds = sorted({c.hold for c in cells})
    stops = sorted({c.stop_loss for c in cells})
    by_pair: dict[tuple[int, int], GridCell] = {}
    for c in cells:
        key = (c.hold, c.stop_loss)
        if key not in by_pair or _cell_sort_key(c) > _cell_sort_key(by_pair[key]):
            by_pair[key] = c

    lines = []
    lines.append("| 持有\\SL | " + " | ".join(f"-{s}%" for s in stops) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(stops)) + "|")
    for h in holds:
        row = [f"{h}天"]
        for s in stops:
            c = by_pair.get((h, s))
            if not c or c.sharpe is None:
                row.append("-")
            else:
                marker = " 🏆" if c == best else ""
                row.append(f"{c.sharpe:.3f}{marker}")
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _numeric_ret(row: dict[str, str]) -> float | None:
    try:
        return float(row.get("ret_pct", ""))
    except ValueError:
        return None


def _group_stats(rows: list[dict[str, str]], key_fn: Callable[[dict[str, str]], str]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[key_fn(row) or "-"].append(row)

    stats: list[dict[str, object]] = []
    for key, items in groups.items():
        returns = [v for r in items if (v := _numeric_ret(r)) is not None]
        wins = sum(1 for v in returns if v > 0)
        dates = sorted({r.get("signal_date", "") for r in items if r.get("signal_date")})
        stats.append(
            {
                "key": key,
                "count": len(returns),
                "win_rate": _pct(wins, len(returns)),
                "avg": _safe_mean(returns),
                "median": _safe_median(returns),
                "first_date": dates[0] if dates else "",
                "last_date": dates[-1] if dates else "",
            }
        )
    return sorted(stats, key=lambda x: (-int(x["count"]), str(x["key"])))


def _latest_cycle(rows: list[dict[str, str]], sample_size: int = 20) -> tuple[str, str]:
    dated = [r for r in rows if r.get("signal_date")]
    dated.sort(key=lambda r: (r.get("signal_date", ""), r.get("code", "")))
    tail = dated[-sample_size:]
    if not tail:
        return "样本不足", "未找到可完整验证的尾段交易样本。"

    counts = Counter(r.get("regime", "-") or "-" for r in tail)
    dominant = counts.most_common(2)
    latest_date = tail[-1].get("signal_date", "")
    first_date = tail[0].get("signal_date", "")
    label_parts = [f"{k}({REGIME_LABELS.get(k, k)}) {v}/{len(tail)}" for k, v in dominant]
    if len(dominant) >= 2:
        cycle = f"{dominant[0][0]} / {dominant[1][0]} 切换观察期"
    else:
        cycle = f"{dominant[0][0]} 主导期"
    detail = (
        f"最优组合可完整验证的尾段信号为 {first_date} ~ {latest_date}，近 {len(tail)} 笔以 "
        + "、".join(label_parts)
        + " 为主。"
    )
    return cycle, detail


def _build_trade_diagnostics(rows: list[dict[str, str]]) -> dict[str, object]:
    returns = [v for r in rows if (v := _numeric_ret(r)) is not None]
    wins = [v for v in returns if v > 0]
    losses = [v for v in returns if v <= 0]
    sorted_desc = sorted(returns, reverse=True)
    drop_top_1 = _safe_mean(sorted_desc[1:]) if len(sorted_desc) > 1 else None
    drop_top_3 = _safe_mean(sorted_desc[3:]) if len(sorted_desc) > 3 else None
    payoff = None
    if wins and losses:
        avg_loss = abs(mean(losses))
        payoff = mean(wins) / avg_loss if avg_loss > 0 else None
    dates = sorted({r.get("signal_date", "") for r in rows if r.get("signal_date")})
    return {
        "count": len(returns),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _pct(len(wins), len(returns)),
        "avg_win": _safe_mean(wins),
        "avg_loss": _safe_mean(losses),
        "payoff": payoff,
        "avg_all": _safe_mean(returns),
        "median_all": _safe_median(returns),
        "drop_top_1_avg": drop_top_1,
        "drop_top_3_avg": drop_top_3,
        "first_signal_date": dates[0] if dates else "",
        "last_signal_date": dates[-1] if dates else "",
    }


def _best_per_hold_comment(cells: list[GridCell]) -> str:
    parts = []
    for hold in sorted({c.hold for c in cells}):
        subset = [c for c in cells if c.hold == hold and c.sharpe is not None]
        if not subset:
            continue
        best = max(subset, key=_cell_sort_key)
        parts.append(f"{hold}天最佳 {_fmt_param(best)}，夏普 {best.sharpe:.3f}")
    return "；".join(parts)


def build_report(cells: list[GridCell], run_url: str = "", generated_at: str = "") -> str:
    if not cells:
        raise ValueError("未找到可解析的 backtest summary artifacts")

    ranked = sorted(cells, key=_cell_sort_key, reverse=True)
    best = ranked[0]
    best_rows = _read_trades(best.trades_path)
    diagnostics = _build_trade_diagnostics(best_rows)
    regime_stats = _group_stats(best_rows, lambda r: r.get("regime", ""))
    trigger_stats = _group_stats(best_rows, lambda r: r.get("trigger", ""))
    current_cycle, cycle_detail = _latest_cycle(best_rows)

    start = best.start or min((c.start for c in cells if c.start), default="")
    end = best.end or max((c.end for c in cells if c.end), default="")
    generated = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pos_sharpe = sum(1 for c in cells if (c.sharpe or 0) > 0)
    neg_sharpe = len(cells) - pos_sharpe

    lines: list[str] = [
        "# 当前市场回测报告",
        "",
        f"> 自动生成于 {generated}。本文件由 `scripts/update_backtest_market_report.py` 从 Backtest Grid artifacts 更新。",
        "",
        "## 执行上下文",
        "",
        "- 回测脚本: `python -m scripts.backtest_runner`（由 `.github/workflows/backtest_grid.yml` 以 18 单元参数网格并发执行）",
        f"- 回测区间: {start} ~ {end}",
        f"- 市场周期: {current_cycle}",
        f"- 周期说明: {cycle_detail}",
        f"- 可完整验证信号期: {diagnostics.get('first_signal_date') or '-'} ~ {diagnostics.get('last_signal_date') or '-'}",
        f"- 股票池: {best.board or '-'} (sample={best.sample_size or '-'})",
        f"- 每日候选上限: {best.top_n or '-'}",
        f"- 参数单元: {len(cells)} 组；正夏普 {pos_sharpe} 组，非正夏普 {neg_sharpe} 组",
        f"- GitHub Actions: {run_url or '-'}",
        "",
        "## 本次结论",
        "",
        f"- 最优参数: **{_fmt_param(best)}**",
        f"- 最优夏普: **{_fmt_num(best.sharpe, 3)}**；胜率 **{_fmt_num(best.win_rate, 1, '%')}**；单笔均收 **{_fmt_signed(best.avg_ret, 2, '%')}**；最大回撤 **{_fmt_num(best.max_drawdown, 1, '%')}**；样本 **{best.trades or 0}** 笔",
        f"- wbt 校验: 夏普 {_fmt_num(best.wbt_sharpe, 3)}，最大回撤 {_fmt_num(best.wbt_max_drawdown, 2, '%')}，日胜率 {_fmt_num(best.wbt_daily_win_rate, 2, '%')}；绩效引擎 `{best.metrics_engine or '-'}`",
        f"- 参数观察: {_best_per_hold_comment(cells)}",
    ]

    if best.take_profit == 0:
        lines.append("- 退出观察: 当前最佳组合关闭固定止盈，说明右尾大赢家对收益贡献很大，固定 TP 容易截断趋势。")
    if best.win_rate is not None and best.win_rate < 35 and best.avg_ret is not None and best.avg_ret > 0:
        lines.append(
            "- 胜率结构: 单笔胜率偏低但均收为正，属于低胜率/高赔率的趋势跟踪形态；需要监控右尾依赖，而不是单纯追求高胜率。"
        )
    if diagnostics.get("drop_top_1_avg") is not None and best.avg_ret is not None:
        lines.append(
            f"- 右尾依赖: 去掉最大盈利单后单笔均收约 {_fmt_signed(diagnostics['drop_top_1_avg'], 2, '%')}；"
            f"去掉前三大盈利单后约 {_fmt_signed(diagnostics['drop_top_3_avg'], 2, '%')}。"
        )

    lines.extend(
        [
            "",
            "## 参数梯队",
            "",
            "| 排名 | 参数组合 | 夏普 | 胜率 | 均收 | 回撤 | 样本 |",
            "|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for idx, cell in enumerate(ranked, 1):
        marker = " 🏆" if cell == best else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    _fmt_param(cell),
                    f"{_fmt_num(cell.sharpe, 3)}{marker}",
                    _fmt_num(cell.win_rate, 1, "%"),
                    _fmt_signed(cell.avg_ret, 2, "%"),
                    _fmt_num(cell.max_drawdown, 1, "%"),
                    str(cell.trades or 0),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 最优夏普矩阵", "", *_build_matrix(cells, best)])

    lines.extend(
        [
            "",
            "## 最优组合交易结构",
            "",
            f"- 交易笔数: {diagnostics['count']}；盈利 {diagnostics['wins']}；亏损 {diagnostics['losses']}",
            f"- 单笔胜率: {_fmt_num(diagnostics['win_rate'], 2, '%')}",
            f"- 盈利单均值: {_fmt_signed(diagnostics['avg_win'], 2, '%')}",
            f"- 亏损单均值: {_fmt_signed(diagnostics['avg_loss'], 2, '%')}",
            f"- 盈亏比: {_fmt_num(diagnostics['payoff'], 2)}",
            f"- 单笔中位数: {_fmt_signed(diagnostics['median_all'], 2, '%')}",
            "",
            "## 市场周期分层",
            "",
            "| 周期 | 含义 | 笔数 | 信号期 | 胜率 | 均收 | 中位数 |",
            "|---|---|---:|---|---:|---:|---:|",
        ]
    )
    for stat in regime_stats:
        key = str(stat["key"])
        date_range = f"{stat['first_date']} ~ {stat['last_date']}" if stat["first_date"] else "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    key,
                    REGIME_LABELS.get(key, "-"),
                    str(stat["count"]),
                    date_range,
                    _fmt_num(stat["win_rate"], 1, "%"),
                    _fmt_signed(stat["avg"], 2, "%"),
                    _fmt_signed(stat["median"], 2, "%"),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 信号类型分层",
            "",
            "| 信号 | 笔数 | 胜率 | 均收 | 中位数 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for stat in trigger_stats:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(stat["key"]),
                    str(stat["count"]),
                    _fmt_num(stat["win_rate"], 1, "%"),
                    _fmt_signed(stat["avg"], 2, "%"),
                    _fmt_signed(stat["median"], 2, "%"),
                ]
            )
            + " |"
        )

    negative_regimes = [s for s in regime_stats if isinstance(s["avg"], float) and s["avg"] < 0]
    positive_regimes = [s for s in regime_stats if isinstance(s["avg"], float) and s["avg"] > 0]
    lines.extend(["", "## 解读与后续策略", ""])
    if positive_regimes:
        pos_text = "、".join(f"{s['key']}({_fmt_signed(s['avg'], 2, '%')})" for s in positive_regimes)
        lines.append(f"- 优势周期: {pos_text}，这些水温下更适合保留趋势跟踪仓位。")
    if negative_regimes:
        neg_text = "、".join(f"{s['key']}({_fmt_signed(s['avg'], 2, '%')})" for s in negative_regimes)
        lines.append(f"- 弱势周期: {neg_text}，这些水温下建议降仓、禁开或增加确认。")
    pure_sos = next((s for s in trigger_stats if s["key"] == "sos"), None)
    if pure_sos and isinstance(pure_sos["avg"], float) and pure_sos["avg"] < 0:
        lines.append(
            f"- 纯 SOS 信号本轮均收 {_fmt_signed(pure_sos['avg'], 2, '%')}，建议后续测试 `SOS+EVR/Spring/LPS` 或次日跟随确认，避免宽口径突破噪音。"
        )
    lines.append(
        "- 后续每次 Backtest Grid 完成后，本文件会被 workflow 自动刷新；若 Actions token 有写权限，会提交到仓库，否则仍会作为 artifact 留存。"
    )

    lines.extend(
        [
            "",
            "## 口径说明",
            "",
            "- 胜率是单笔交易 `ret_pct > 0` 的比例，不是组合每日正收益比例。",
            "- 回测使用信号日收盘后出信号、次日开盘买入、T+1 后检查止损/止盈的口径，并包含买卖双边摩擦成本。",
            "- `可完整验证信号期` 会早于回测结束日，因为持有窗口需要足够后续交易日完成离场验证。",
            "- 本结果仍可能包含当前股票池幸存者偏差，以及当前截面市值/行业映射带来的前视偏差；用于参数方向和市场周期适配判断，不等同于实盘承诺。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update docs/BACKTEST_MARKET_REPORT.md from backtest grid artifacts.")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory containing backtest-grid-* artifacts")
    parser.add_argument("--output", default="docs/BACKTEST_MARKET_REPORT.md", help="Report markdown path")
    parser.add_argument("--run-url", default="", help="GitHub Actions run URL")
    parser.add_argument("--generated-at", default="", help="Override generated timestamp")
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    cells = load_grid_cells(artifacts_dir)
    report = build_report(cells, run_url=args.run_url, generated_at=args.generated_at)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report + "\n", encoding="utf-8")
    print(f"[backtest-report] wrote {out_path} from {len(cells)} grid cells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
