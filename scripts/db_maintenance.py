"""
数据库维护任务 — 多表过期数据清理。
每日定时运行，按各表 TTL 策略删除历史记录以节约数据库空间。
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
    TABLE_STOCK_HIST_CACHE,
    TABLE_TAIL_BUY_HISTORY,
    TABLE_TRADE_ORDERS,
)
from integrations.supabase_base import create_admin_client

# (table, date_column, ttl_days, cutoff_kind)
# cutoff_kind:
# - iso_date:      YYYY-MM-DD（字符串日期列）
# - yyyymmdd_int:  YYYYMMDD（整数日期列）
CLEANUP_RULES: list[tuple[str, str, int, str]] = [
    (TABLE_STOCK_HIST_CACHE, "date", 320, "iso_date"),
    (TABLE_TRADE_ORDERS, "trade_date", 15, "iso_date"),
    (TABLE_RECOMMENDATION_TRACKING, "recommend_date", 40, "yyyymmdd_int"),
    (TABLE_SIGNAL_PENDING, "signal_date", 15, "iso_date"),
    (TABLE_MARKET_SIGNAL_DAILY, "trade_date", 30, "iso_date"),
    (TABLE_DAILY_NAV, "trade_date", 15, "iso_date"),
    (TABLE_TAIL_BUY_HISTORY, "run_date", 10, "iso_date"),
]


def _cutoff_value(ttl_days: int, kind: str) -> str | int:
    d = (datetime.now(UTC) - timedelta(days=ttl_days)).date()
    if kind == "yyyymmdd_int":
        return int(d.strftime("%Y%m%d"))
    return d.isoformat()


def _is_statement_timeout_error(err: object) -> bool:
    text = str(err).lower()
    return "statement timeout" in text or "57014" in text


def _cleanup_stock_hist_cache_before_cutoff(
    client,
    cutoff_iso: str,
) -> tuple[bool, str]:
    """
    stock_hist_cache 大表超时兜底：按 symbol 分批删除 date < cutoff 的历史数据。
    """
    symbol_batch = max(int(os.getenv("STOCK_CACHE_CLEANUP_SYMBOL_BATCH", "120")), 1)
    max_rounds = max(int(os.getenv("STOCK_CACHE_CLEANUP_MAX_ROUNDS", "60")), 1)
    deleted_symbols = 0
    timeout_symbols = 0

    for _ in range(max_rounds):
        try:
            probe = (
                client.table(TABLE_STOCK_HIST_CACHE)
                .select("symbol")
                .lt("date", cutoff_iso)
                .limit(symbol_batch)
                .execute()
            )
        except Exception as probe_err:
            if _is_statement_timeout_error(probe_err):
                return True, f"probe timeout, skipped remaining cleanup: {probe_err}"
            return False, f"probe failed: {probe_err}"

        symbols = sorted(
            {str(r.get("symbol", "")).strip() for r in (probe.data or []) if str(r.get("symbol", "")).strip()}
        )
        if not symbols:
            return (
                True,
                f"batched cleanup done, deleted_symbols={deleted_symbols}, timeout_symbols={timeout_symbols}",
            )

        for sym in symbols:
            try:
                (client.table(TABLE_STOCK_HIST_CACHE).delete().eq("symbol", sym).lt("date", cutoff_iso).execute())
                deleted_symbols += 1
            except Exception as delete_err:
                if _is_statement_timeout_error(delete_err):
                    timeout_symbols += 1
                    continue
                return False, f"delete failed on symbol={sym}: {delete_err}"

    return (
        True,
        f"partial cleanup (max_rounds reached), deleted_symbols={deleted_symbols}, timeout_symbols={timeout_symbols}",
    )


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
        # stock_hist_cache 全表删除在数据量大时容易触发 statement timeout，降级为按 symbol 分批
        if (
            table == TABLE_STOCK_HIST_CACHE
            and date_col == "date"
            and isinstance(cutoff, str)
            and _is_statement_timeout_error(e)
            and not dry_run
        ):
            ok, msg = _cleanup_stock_hist_cache_before_cutoff(client, cutoff)
            if ok:
                return f"ok_batched: {msg}", None
            return f"error: {msg}", None
        return f"error: {e}", None


def cleanup_unadjusted_cache(client) -> tuple[bool, str]:
    """删除 stock_hist_cache 中 adjust='none' 的存量缓存。"""
    try:
        client.table(TABLE_STOCK_HIST_CACHE).delete().eq("adjust", "none").execute()
        return True, "cleaned adjust=none rows"
    except Exception as first_err:
        try:
            batch_size = max(int(os.getenv("STOCK_CACHE_CLEANUP_SYMBOL_BATCH", "300")), 1)
            max_rounds = max(int(os.getenv("STOCK_CACHE_CLEANUP_MAX_ROUNDS", "200")), 1)
            deleted_symbols = 0
            timeout_symbols = 0
            for _ in range(max_rounds):
                try:
                    probe = (
                        client.table(TABLE_STOCK_HIST_CACHE)
                        .select("symbol")
                        .eq("adjust", "none")
                        .limit(batch_size)
                        .execute()
                    )
                except Exception as probe_err:
                    if _is_statement_timeout_error(probe_err):
                        return True, f"adjust=none probe timeout, skipped remaining cleanup: {probe_err}"
                    raise
                symbols = sorted(
                    {str(r.get("symbol", "")).strip() for r in (probe.data or []) if str(r.get("symbol", "")).strip()}
                )
                if not symbols:
                    return (
                        True,
                        f"cleaned adjust=none (batched, symbols={deleted_symbols}, timeout_symbols={timeout_symbols})",
                    )
                for sym in symbols:
                    try:
                        (client.table(TABLE_STOCK_HIST_CACHE).delete().eq("adjust", "none").eq("symbol", sym).execute())
                        deleted_symbols += 1
                    except Exception as delete_err:
                        if _is_statement_timeout_error(delete_err):
                            timeout_symbols += 1
                            continue
                        raise
            return (
                True,
                "partial cleanup (max_rounds reached), "
                f"deleted_symbols={deleted_symbols}, timeout_symbols={timeout_symbols}, first_err={first_err}",
            )
        except Exception as batch_err:
            if _is_statement_timeout_error(first_err) and _is_statement_timeout_error(batch_err):
                return (
                    True,
                    "skipped adjust=none cleanup due to persistent statement timeout, "
                    f"first_err={first_err}, batch_err={batch_err}",
                )
            return False, f"batch cleanup also failed: {batch_err} (original: {first_err})"


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

    ok, msg = cleanup_unadjusted_cache(client)
    print(f"[db_maintenance] stock_hist_cache adjust=none: ok={ok}, {msg}")
    if not ok:
        all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
