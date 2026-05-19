#!/usr/bin/env python3
"""Send the US backtest comparison report to Feishu."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True)
class UsBacktestCell:
    period_key: str
    period_label: str
    start: str
    end: str
    strategy_id: str
    strategy_name: str
    strategy_desc: str
    trades: int
    win_rate: float | None
    avg_ret: float | None
    max_drawdown: float | None
    sharpe: float | None
    total_return: float | None


def _as_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(val) or math.isinf(val) else val


def _as_int(value: Any) -> int:
    val = _as_float(value)
    return int(val) if val is not None else 0


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}{suffix}"


def _fmt_signed(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:+.{digits}f}{suffix}"


def _sharpe_badge(value: float | None) -> str:
    if value is None:
        return "⚪"
    if value >= 0.6:
        return "🟢"
    if value >= 0.2:
        return "🟡"
    if value >= 0:
        return "🟠"
    return "🔴"


def _cell_from_summary(path: Path) -> UsBacktestCell | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    strategy = data.get("execution_strategy") if isinstance(data.get("execution_strategy"), dict) else {}
    return UsBacktestCell(
        period_key=str(data.get("period_key") or "unknown"),
        period_label=str(data.get("period_label") or data.get("period_key") or "unknown"),
        start=str(data.get("start") or "").strip(),
        end=str(data.get("end") or "").strip(),
        strategy_id=str(data.get("strategy_id") or strategy.get("id") or path.parent.name),
        strategy_name=str(data.get("strategy_name") or strategy.get("name") or path.parent.name),
        strategy_desc=str(data.get("strategy_desc") or strategy.get("description") or "").strip(),
        trades=_as_int(data.get("trades")),
        win_rate=_as_float(data.get("win_rate_pct")),
        avg_ret=_as_float(data.get("avg_ret_pct")),
        max_drawdown=_as_float(data.get("max_drawdown_pct")),
        sharpe=_as_float(data.get("sharpe_ratio")),
        total_return=_as_float(data.get("portfolio_total_ret_pct")),
    )


def load_cells(artifacts_dir: Path) -> list[UsBacktestCell]:
    cells = []
    for path in sorted(artifacts_dir.glob("**/summary.json")):
        cell = _cell_from_summary(path)
        if cell is not None:
            cells.append(cell)
    return cells


def _rank_key(cell: UsBacktestCell) -> tuple[float, float, int]:
    sharpe = cell.sharpe if cell.sharpe is not None else float("-inf")
    avg = cell.avg_ret if cell.avg_ret is not None else float("-inf")
    return (sharpe, avg, cell.trades)


def _group_by_period(cells: list[UsBacktestCell]) -> dict[str, list[UsBacktestCell]]:
    grouped: dict[str, list[UsBacktestCell]] = defaultdict(list)
    for cell in cells:
        grouped[cell.period_key].append(cell)
    return dict(grouped)


def _period_title(cells: list[UsBacktestCell]) -> str:
    first = cells[0]
    return f"**{first.period_label}**  ·  {first.start} ~ {first.end}  ·  {len(cells)} 策略"


def _row_columns(items: list[tuple[str, int]], *, grey: bool = False) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "grey" if grey else "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": weight,
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
            }
            for text, weight in items
        ],
    }


def _period_elements(cells: list[UsBacktestCell]) -> list[dict[str, Any]]:
    ranked = sorted(cells, key=_rank_key, reverse=True)
    best = ranked[0] if ranked else None
    elements: list[dict[str, Any]] = [{"tag": "div", "text": {"tag": "lark_md", "content": _period_title(cells)}}]
    if best and best.sharpe is not None:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"🏆 **最优策略**: {best.strategy_name}  ·  夏普 {_fmt(best.sharpe, 3)}  ·  "
                        f"胜率 {_fmt(best.win_rate, 1, '%')}  ·  均收 {_fmt_signed(best.avg_ret, 2, '%')}"
                    ),
                },
            }
        )
    elements.append(
        _row_columns(
            [("**策略**", 4), ("**夏普**", 2), ("**胜率**", 2), ("**均收**", 2), ("**回撤**", 2), ("**样本**", 2)],
            grey=True,
        )
    )
    for cell in ranked:
        elements.append(_strategy_row(cell, best))
    elements.append(_strategy_notes(ranked))
    return elements


def _strategy_row(cell: UsBacktestCell, best: UsBacktestCell | None) -> dict[str, Any]:
    marker = " 🏆" if best and cell.strategy_id == best.strategy_id else ""
    sharpe = f"{_sharpe_badge(cell.sharpe)} {_fmt(cell.sharpe, 3)}{marker}"
    return _row_columns(
        [
            (f"**{cell.strategy_name}**", 4),
            (sharpe, 2),
            (_fmt(cell.win_rate, 1, "%"), 2),
            (_fmt_signed(cell.avg_ret, 2, "%"), 2),
            (_fmt(cell.max_drawdown, 1, "%"), 2),
            (f"{cell.trades}笔", 2),
        ]
    )


def _strategy_notes(cells: list[UsBacktestCell]) -> dict[str, Any]:
    lines = [f"- **{c.strategy_name}**: {c.strategy_desc}" for c in cells if c.strategy_desc]
    content = "\n".join(lines) if lines else "- 策略说明缺失，请检查 summary.json 的 execution_strategy 字段。"
    return {"tag": "div", "text": {"tag": "lark_md", "content": "**策略口径**\n" + content}}


def build_card(cells: list[UsBacktestCell], *, run_url: str, top_n: str) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**市场** US  ·  **TopN** {top_n}  ·  **共 {len(cells)} 单元**\n[查看 GitHub Actions 详情]({run_url})",
            },
        }
    ]
    grouped = _group_by_period(cells)
    if not cells:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "⚠️ 未找到可解析的 summary.json"}})
    for period_key in sorted(grouped):
        elements.append({"tag": "hr"})
        elements.extend(_period_elements(grouped[period_key]))
    template = "blue" if any((c.sharpe or -999) > 0 for c in cells) else "orange" if cells else "red"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "📊 US Backtest Grid 美股回测完成"},
                "template": template,
            },
            "elements": elements,
        },
    }


def write_report(path: Path, cells: list[UsBacktestCell], *, run_url: str, top_n: str) -> None:
    lines = ["# US Backtest Strategy Comparison", "", f"- TopN: {top_n}", f"- GitHub Actions: {run_url or '-'}", ""]
    for period_key, period_cells in sorted(_group_by_period(cells).items()):
        ranked = sorted(period_cells, key=_rank_key, reverse=True)
        lines.extend(
            [
                f"## {ranked[0].period_label} ({period_key})",
                "",
                "| 策略 | 说明 | 夏普 | 胜率 | 均收 | 回撤 | 样本 |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for cell in ranked:
            lines.append(
                "| "
                + " | ".join(
                    [
                        cell.strategy_name,
                        cell.strategy_desc.replace("|", "/"),
                        _fmt(cell.sharpe, 3),
                        _fmt(cell.win_rate, 1, "%"),
                        _fmt_signed(cell.avg_ret, 2, "%"),
                        _fmt(cell.max_drawdown, 1, "%"),
                        str(cell.trades),
                    ]
                )
                + " |"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def send_feishu(webhook: str, payload: dict[str, Any]) -> None:
    response = requests.post(webhook, json=payload, timeout=15)
    print(f"飞书通知: status={response.status_code}, body={response.text[:200]}")
    response.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate US backtest summary.json artifacts and notify Feishu.")
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--output", default="docs/BACKTEST_US_REPORT.md")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--top-n", default="2")
    args = parser.parse_args()

    cells = load_cells(Path(args.artifacts_dir))
    print(f"[us-backtest-notify] loaded {len(cells)} summary.json cells")
    write_report(Path(args.output), cells, run_url=args.run_url, top_n=str(args.top_n))
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if webhook:
        send_feishu(webhook, build_card(cells, run_url=args.run_url, top_n=str(args.top_n)))
    else:
        print("FEISHU_WEBHOOK_URL 未配置，跳过飞书通知")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
