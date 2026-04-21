# -*- coding: utf-8 -*-
"""
stock_hist_cache 维护任务：
- 按交易日期 date 清理滑动窗口外的历史记录
"""
from __future__ import annotations

import argparse
import os
import sys


# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.stock_cache import cleanup_cache
from core.constants import TABLE_STOCK_HIST_CACHE


def cleanup_expired_cache(ttl_days: int, context: str = "admin") -> tuple[bool, str]:
    try:
        cleanup_cache(ttl_days=ttl_days, context=context)
        return True, f"cleanup_done ttl_days={ttl_days}"
    except Exception as e:
        return False, f"cleanup failed: {e}"


def cleanup_unadjusted_cache(context: str = "admin") -> tuple[bool, str]:
    """删除 adjust='none'（不复权）的存量缓存数据。"""
    try:
        from integrations.supabase_base import create_admin_client
        client = create_admin_client()
        # 先尝试一次性删除；若数据库 statement timeout，再降级为按 symbol 分批删除。
        try:
            client.table(TABLE_STOCK_HIST_CACHE).delete().eq("adjust", "none").execute()
            return True, "cleaned adjust=none rows (single statement)"
        except Exception as first_err:
            batch_size = max(int(os.getenv("STOCK_CACHE_CLEANUP_SYMBOL_BATCH", "300")), 1)
            max_rounds = max(int(os.getenv("STOCK_CACHE_CLEANUP_MAX_ROUNDS", "200")), 1)
            rounds = 0
            deleted_symbols = 0

            while rounds < max_rounds:
                rounds += 1
                probe = (
                    client.table(TABLE_STOCK_HIST_CACHE)
                    .select("symbol")
                    .eq("adjust", "none")
                    .limit(batch_size)
                    .execute()
                )
                rows = probe.data or []
                symbols = sorted(
                    {
                        str(row.get("symbol") or "").strip()
                        for row in rows
                        if str(row.get("symbol") or "").strip()
                    }
                )
                if not symbols:
                    return (
                        True,
                        "cleaned adjust=none rows "
                        f"(fallback by symbol, rounds={rounds}, symbols={deleted_symbols}, first_err={first_err})",
                    )

                for sym in symbols:
                    (
                        client.table(TABLE_STOCK_HIST_CACHE)
                        .delete()
                        .eq("adjust", "none")
                        .eq("symbol", sym)
                        .execute()
                    )
                    deleted_symbols += 1

            remain = (
                client.table(TABLE_STOCK_HIST_CACHE)
                .select("symbol")
                .eq("adjust", "none")
                .limit(1)
                .execute()
            )
            if remain.data:
                return (
                    False,
                    "cleanup adjust=none partial: "
                    f"reached max_rounds={max_rounds}, deleted_symbols={deleted_symbols}, first_err={first_err}",
                )
            return (
                True,
                "cleaned adjust=none rows "
                f"(fallback by symbol, rounds={rounds}, symbols={deleted_symbols}, first_err={first_err})",
            )
    except Exception as e:
        return False, f"cleanup adjust=none failed: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="stock_hist_cache maintenance")
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=400,
        help="按 date 清理早于该天数的缓存记录（默认 400）",
    )
    args = parser.parse_args()

    ttl_days = max(int(args.ttl_days or 365), 1)
    ok, msg = cleanup_expired_cache(ttl_days=ttl_days, context="admin")
    print(f"[stock_hist_cache_maintenance] cleanup ok={ok}, {msg}")

    ok2, msg2 = cleanup_unadjusted_cache(context="admin")
    print(f"[stock_hist_cache_maintenance] unadjusted ok={ok2}, {msg2}")

    return 0 if (ok and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
