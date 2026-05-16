"""
美股推荐后验表现回刷任务。
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import suppress
from datetime import datetime
from zoneinfo import ZoneInfo

if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with suppress(Exception):
    from dotenv import load_dotenv

    load_dotenv()

from integrations.supabase_recommendation import refresh_us_tracking_performance

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, logs_path: str | None = None) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    if not logs_path:
        return
    os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
    with open(logs_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh US recommendation MFE/MAE/range metrics.")
    parser.add_argument("--logs", default="", help="日志文件路径（可选）")
    parser.add_argument("--max-dates", type=int, default=int(os.getenv("US_TRACKING_PERFORMANCE_MAX_DATES", "60")))
    parser.add_argument("--kline-count", type=int, default=int(os.getenv("US_TRACKING_PERFORMANCE_KLINE_COUNT", "160")))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logs_path = str(args.logs or "").strip() or None
    _log(
        f"开始执行美股推荐表现回刷 max_dates={args.max_dates}, kline_count={args.kline_count}",
        logs_path,
    )
    try:
        summary = refresh_us_tracking_performance(max_dates=args.max_dates, kline_count=args.kline_count)
    except Exception as e:
        _log(f"任务失败: {e}", logs_path)
        return 1
    _log(
        "任务完成: "
        f"rows_total={summary.get('rows_total', 0)}, "
        f"rows_updated={summary.get('rows_updated', 0)}, "
        f"rows_skipped={summary.get('rows_skipped', 0)}, "
        f"codes_total={summary.get('codes_total', 0)}, "
        f"codes_no_data={summary.get('codes_no_data', 0)}, "
        f"latest_trade_date={summary.get('latest_trade_date', '') or '-'}, "
        f"mfe_ge_5={summary.get('mfe_ge_5', 0)}, "
        f"mfe_ge_10={summary.get('mfe_ge_10', 0)}, "
        f"mae_le_neg5={summary.get('mae_le_neg5', 0)}",
        logs_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
