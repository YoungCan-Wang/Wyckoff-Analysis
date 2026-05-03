"""
推荐跟踪价格回填任务（独立定时）：
- 从 recommendation_tracking 读取 code / recommend_date
- 使用 Tushare 不复权日线（daily）计算：
  - initial_price: 推荐时间对应最近交易日收盘价
  - current_price: 当前系统时间对应最近交易日收盘价
  - change_pct
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations.supabase_recommendation import refresh_tracking_prices_with_tushare_unadjusted

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, logs_path: str | None = None) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    if logs_path:
        os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
        with open(logs_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="推荐跟踪价格回填任务（Tushare 不复权）")
    parser.add_argument("--logs", default="", help="日志文件路径（可选）")
    args = parser.parse_args()
    logs_path = str(args.logs or "").strip() or None

    _log("开始执行 recommendation tracking 回填任务（Tushare 不复权）", logs_path)
    try:
        summary = refresh_tracking_prices_with_tushare_unadjusted()
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
        f"latest_trade_date={summary.get('latest_trade_date', '') or '-'}",
        logs_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
