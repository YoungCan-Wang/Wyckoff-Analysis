"""
数据库维护任务 — 多表过期数据清理。
每日定时运行，按各表 TTL / 滑动窗口策略删除历史记录以节约数据库空间。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import (
    TABLE_DAILY_NAV,
    TABLE_MARKET_SIGNAL_DAILY,
    TABLE_RECOMMENDATION_TRACKING,
    TABLE_SIGNAL_PENDING,
    TABLE_TAIL_BUY_HISTORY,
    TABLE_TRADE_ORDERS,
)
from integrations.supabase_base import create_admin_client

# (table, date_column, ttl_days, cutoff_kind)
# cutoff_kind:
# - iso_date:      YYYY-MM-DD（字符串日期列）
# - yyyymmdd_int:  YYYYMMDD（整数日期列）
CLEANUP_RULES: list[tuple[str, str, int, str]] = [
    (TABLE_TRADE_ORDERS, "trade_date", 15, "iso_date"),
    (TABLE_SIGNAL_PENDING, "signal_date", 15, "iso_date"),
    (TABLE_MARKET_SIGNAL_DAILY, "trade_date", 30, "iso_date"),
    (TABLE_DAILY_NAV, "trade_date", 15, "iso_date"),
    (TABLE_TAIL_BUY_HISTORY, "run_date", 10, "iso_date"),
]
RECOMMENDATION_KEEP_DATES = 30
RECOMMENDATION_DATE_PAGE_SIZE = 1000


def _cutoff_value(ttl_days: int, kind: str) -> str | int:
    d = (datetime.now(UTC) - timedelta(days=ttl_days)).date()
    if kind == "yyyymmdd_int":
        return int(d.strftime("%Y%m%d"))
    return d.isoformat()


def cleanup_table(
    client,
    table: str,
    date_col: str,
    ttl_days: int,
    cutoff_kind: str,
    *,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    cutoff = _cutoff_value(ttl_days, cutoff_kind)
    try:
        if dry_run:
            resp = client.table(table).select("*", count="exact").lt(date_col, cutoff).limit(0).execute()
            return "dry_run", resp.count or 0
        client.table(table).delete().lt(date_col, cutoff).execute()
        return "ok", None
    except Exception as e:
        return f"error: {e}", None


def _to_int_date(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _latest_recommend_dates(client, keep_dates: int, page_size: int) -> list[int]:
    dates: list[int] = []
    seen: set[int] = set()
    before_date: int | None = None

    while len(dates) < keep_dates:
        query = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select("recommend_date")
            .order("recommend_date", desc=True)
            .limit(page_size)
        )
        if before_date is not None:
            query = query.lt("recommend_date", before_date)

        rows = query.execute().data or []
        page_dates = [_to_int_date(row.get("recommend_date")) for row in rows]
        valid_dates = [d for d in page_dates if d is not None]
        if not valid_dates:
            break

        for recommend_date in valid_dates:
            if recommend_date not in seen:
                seen.add(recommend_date)
                dates.append(recommend_date)
                if len(dates) >= keep_dates:
                    break

        before_date = min(valid_dates)

    return dates


def cleanup_recommendation_tracking(
    client,
    *,
    keep_dates: int = RECOMMENDATION_KEEP_DATES,
    page_size: int = RECOMMENDATION_DATE_PAGE_SIZE,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    keep_dates = max(int(keep_dates), 1)
    page_size = max(int(page_size), 1)
    dates = _latest_recommend_dates(client, keep_dates, page_size)
    if len(dates) < keep_dates:
        count = 0 if dry_run else None
        return f"keep_all, keep_dates={keep_dates}, distinct_dates={len(dates)}", count

    cutoff = dates[keep_dates - 1]
    try:
        if dry_run:
            resp = (
                client.table(TABLE_RECOMMENDATION_TRACKING)
                .select("*", count="exact")
                .lt("recommend_date", cutoff)
                .limit(0)
                .execute()
            )
            return f"dry_run, keep_dates={keep_dates}, cutoff={cutoff}", resp.count or 0
        client.table(TABLE_RECOMMENDATION_TRACKING).delete().lt("recommend_date", cutoff).execute()
        return f"ok, keep_dates={keep_dates}, cutoff={cutoff}", None
    except Exception as e:
        return f"error: {e}", None


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库维护 — 多表过期数据清理")
    parser.add_argument("--dry-run", action="store_true", help="只查询待清理行数，不实际删除")
    args = parser.parse_args()

    client = create_admin_client()
    all_ok = True

    for table, date_col, ttl_days, cutoff_kind in CLEANUP_RULES:
        status, count = cleanup_table(
            client,
            table,
            date_col,
            ttl_days,
            cutoff_kind,
            dry_run=args.dry_run,
        )
        suffix = f" ({count} rows)" if count is not None else ""
        print(f"[db_maintenance] {table}: {status}, ttl={ttl_days}d{suffix}")
        if status.startswith("error"):
            all_ok = False

    status, count = cleanup_recommendation_tracking(client, dry_run=args.dry_run)
    suffix = f" ({count} rows)" if count is not None else ""
    print(f"[db_maintenance] {TABLE_RECOMMENDATION_TRACKING}: {status}{suffix}")
    if status.startswith("error"):
        all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
