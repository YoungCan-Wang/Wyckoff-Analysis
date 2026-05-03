"""
股票池解析工具。

根据环境变量选择股票来源（手动指定 / 板块筛选 / 全市场默认）。
"""

from __future__ import annotations

import os

from integrations.fetch_a_share_csv import (
    _normalize_symbols,
    get_stocks_by_board,
)
from tools.funnel_config import parse_int_env


def _stock_name_map() -> dict[str, str]:
    """获取全部 A 股代码→名称映射（如失败返回空 dict）。"""
    try:
        from integrations.fetch_a_share_csv import get_all_stocks

        items = get_all_stocks()
        return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}
    except Exception:
        return {}


def resolve_symbol_pool_from_env() -> tuple[list[str], dict[str, str], dict[str, int | str]]:
    """
    根据环境变量 FUNNEL_POOL_MODE / FUNNEL_POOL_MANUAL_SYMBOLS 等
    解析当前使用的股票池。

    返回: (symbols, name_map, pool_stats)
    """
    pool_mode = str(os.getenv("FUNNEL_POOL_MODE", "") or "").strip().lower()
    limit_count = max(parse_int_env("FUNNEL_POOL_LIMIT_COUNT", 0), 0)

    if pool_mode == "manual":
        manual_raw = str(os.getenv("FUNNEL_POOL_MANUAL_SYMBOLS", "") or "")
        all_name_map = _stock_name_map()
        symbols = _normalize_symbols([x.strip() for x in manual_raw.replace(";", ",").replace("\n", ",").split(",")])
        name_map = {code: all_name_map.get(code, "") for code in symbols}
        return (
            symbols,
            name_map,
            {
                "pool_mode": "manual",
                "pool_main": 0,
                "pool_chinext": 0,
                "pool_merged": len(symbols),
                "pool_st_excluded": 0,
                "pool_limit": limit_count,
            },
        )

    board_name = str(os.getenv("FUNNEL_POOL_BOARD", "") or "").strip().lower()
    if pool_mode == "board" and board_name in {"main", "chinext", "all"}:
        if board_name == "all":
            items = get_stocks_by_board("main") + get_stocks_by_board("chinext")
        else:
            items = get_stocks_by_board(board_name)
        merged_code_to_name: dict[str, str] = {}
        for item in items:
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            if code not in merged_code_to_name:
                merged_code_to_name[code] = str(item.get("name", "")).strip()
        symbols = _normalize_symbols(list(merged_code_to_name.keys()))
        if limit_count > 0:
            symbols = symbols[:limit_count]
        return (
            symbols,
            {code: merged_code_to_name.get(code, "") for code in symbols},
            {
                "pool_mode": "board",
                "pool_main": len(items)
                if board_name == "main"
                else len(get_stocks_by_board("main"))
                if board_name == "all"
                else 0,
                "pool_chinext": len(items)
                if board_name == "chinext"
                else len(get_stocks_by_board("chinext"))
                if board_name == "all"
                else 0,
                "pool_merged": len(symbols),
                "pool_st_excluded": 0,
                "pool_limit": limit_count,
            },
        )

    main_items = get_stocks_by_board("main")
    chinext_items = get_stocks_by_board("chinext")
    merged_code_to_name: dict[str, str] = {}
    for item in main_items + chinext_items:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        if code not in merged_code_to_name:
            merged_code_to_name[code] = str(item.get("name", "")).strip()
    merged_symbols = _normalize_symbols(list(merged_code_to_name.keys()))
    st_symbols = [sym for sym in merged_symbols if "ST" in merged_code_to_name.get(sym, "").upper()]
    st_set = set(st_symbols)
    all_symbols = [sym for sym in merged_symbols if sym not in st_set]
    if limit_count > 0:
        all_symbols = all_symbols[:limit_count]
    return (
        all_symbols,
        {code: merged_code_to_name.get(code, "") for code in all_symbols},
        {
            "pool_mode": "default",
            "pool_main": len(main_items),
            "pool_chinext": len(chinext_items),
            "pool_merged": len(merged_symbols),
            "pool_st_excluded": len(st_symbols),
            "pool_limit": limit_count,
        },
    )
