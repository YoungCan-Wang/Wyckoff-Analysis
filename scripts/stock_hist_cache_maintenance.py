# -*- coding: utf-8 -*-
"""
stock_hist_cache 维护任务：
- 按 updated_at 清理过期缓存记录
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.stock_cache import cleanup_cache


def cleanup_expired_cache(ttl_days: int, context: str = "admin") -> tuple[bool, str]:
    try:
        cleanup_cache(ttl_days=ttl_days, context=context)
        return True, f"cleanup_done ttl_days={ttl_days}"
    except Exception as e:
        return False, f"cleanup failed: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="stock_hist_cache maintenance")
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=365,
        help="按 updated_at 清理早于该天数的缓存记录（默认 365）",
    )
    args = parser.parse_args()

    ttl_days = max(int(args.ttl_days or 365), 1)
    ok, msg = cleanup_expired_cache(ttl_days=ttl_days, context="admin")
    print(f"[stock_hist_cache_maintenance] cleanup ok={ok}, {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
