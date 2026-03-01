# -*- coding: utf-8 -*-
# Copyright (c) 2024 youngcan. All Rights Reserved.
# 本代码仅供个人学习研究使用，未经授权不得用于商业目的。
# 商业授权请联系作者支付授权费用。

"""
统一数据源：个股 akshare→baostock→efinance→tushare；大盘 tushare 直连

输出格式与 akshare 兼容：日期, 开盘, 最高, 最低, 收盘, 成交量, 成交额, 涨跌幅, 换手率, 振幅
"""

from __future__ import annotations

import atexit
import threading
from datetime import date
from typing import Literal

import pandas as pd


_BAOSTOCK_LOGGED = False
_BAOSTOCK_EXIT_HOOKED = False
_BAOSTOCK_MODULE = None
_BAOSTOCK_LOCK = threading.RLock()


def _to_ts_code(symbol: str) -> str:
    """6 位代码转 tushare 格式：000001 -> 000001.SZ，600519 -> 600519.SH"""
    s = str(symbol).strip()
    if "." in s:
        return s
    if s.startswith(("600", "601", "603", "605", "688")):
        return f"{s}.SH"
    return f"{s}.SZ"


def _index_to_ts_code(code: str) -> str:
    """指数代码转 tushare 格式：000001->000001.SH, 399001->399001.SZ, 399006->399006.SZ"""
    s = str(code).strip()
    if "." in s:
        return s
    if s.startswith(("000", "880", "899")):
        return f"{s}.SH"
    return f"{s}.SZ"


def _tag_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """在 DataFrame 上附加真实数据源标识，供上层缓存/展示使用。"""
    df.attrs["source"] = source
    return df


# --- 个股 ---


def _fetch_stock_akshare(
    symbol: str, start: str, end: str, adjust: str
) -> pd.DataFrame:
    import akshare as ak

    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start,
        end_date=end,
        adjust=adjust if adjust else "",
    )
    if df is None or df.empty:
        raise RuntimeError("akshare empty")
    if "日期" in df.columns:
        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def _fetch_stock_baostock(symbol: str, start: str, end: str) -> pd.DataFrame:
    if symbol.startswith(("600", "601", "603", "605", "688")):
        bs_code = f"sh.{symbol}"
    else:
        bs_code = f"sz.{symbol}"
    start_dash = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_dash = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    with _BAOSTOCK_LOCK:
        bs = _ensure_baostock_login()
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount,pctChg",
            start_date=start_dash,
            end_date=end_dash,
            frequency="d",
            adjustflag="2",  # 前复权
        )
        if rs.error_code != "0":
            raise RuntimeError(f"baostock: {rs.error_msg}")
        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
    if not rows:
        raise RuntimeError("baostock empty")
    df = pd.DataFrame(rows, columns=rs.fields)
    df = df.rename(
        columns={
            "date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
            "amount": "成交额",
            "pctChg": "涨跌幅",
        }
    )
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["开盘", "最高", "最低", "收盘", "成交量", "成交额", "涨跌幅"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["换手率"] = pd.NA
    df["振幅"] = pd.NA
    return df


def _baostock_logout_on_exit() -> None:
    global _BAOSTOCK_LOGGED
    with _BAOSTOCK_LOCK:
        bs = _BAOSTOCK_MODULE
        if not _BAOSTOCK_LOGGED or bs is None:
            return
        try:
            bs.logout()
        except Exception:
            pass
        _BAOSTOCK_LOGGED = False


def _ensure_baostock_login():
    """
    进程内复用 baostock 会话，避免每只股票 login/logout 导致大量开销与阻塞日志。
    运行特性说明：该会话在当前 Python 进程生命周期内复用，并由 atexit 在进程退出时回收。
    若未来改为长生命周期守护进程/热重载模式，需要关注其“跨任务复用”行为是否符合预期。
    """
    global _BAOSTOCK_LOGGED, _BAOSTOCK_EXIT_HOOKED, _BAOSTOCK_MODULE
    with _BAOSTOCK_LOCK:
        import baostock as bs

        _BAOSTOCK_MODULE = bs
        if _BAOSTOCK_LOGGED:
            return bs

        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login: {lg.error_msg}")
        _BAOSTOCK_LOGGED = True

        if not _BAOSTOCK_EXIT_HOOKED:
            atexit.register(_baostock_logout_on_exit)
            _BAOSTOCK_EXIT_HOOKED = True
        return bs


def _fetch_stock_efinance(symbol: str, start: str, end: str) -> pd.DataFrame:
    import efinance as ef

    # fqt: 0 不复权, 1 前复权, 2 后复权
    fqt = 1  # 默认前复权
    result = ef.stock.get_quote_history(symbol, beg=start, end=end, klt=101, fqt=fqt)
    if isinstance(result, dict):
        df = result.get(str(symbol))
    else:
        df = result
    if df is None or (hasattr(df, "empty") and df.empty):
        raise RuntimeError("efinance empty")

    # efinance 不同版本列名可能带单位后缀，如：涨跌幅(%)、成交额(元)
    df = df.copy()

    def _rename_prefix(std: str) -> None:
        if std in df.columns:
            return
        for c in df.columns:
            if str(c).startswith(std):
                df.rename(columns={c: std}, inplace=True)
                return

    # 日期列兼容
    if "日期" not in df.columns:
        for c in df.columns:
            if str(c).endswith("日期") or "日期" in str(c):
                df.rename(columns={c: "日期"}, inplace=True)
                break

    for std in [
        "开盘",
        "最高",
        "最低",
        "收盘",
        "成交量",
        "成交额",
        "涨跌幅",
        "换手率",
        "振幅",
    ]:
        _rename_prefix(std)
    # efinance: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 换手率
    out_cols = [
        "日期",
        "开盘",
        "最高",
        "最低",
        "收盘",
        "成交量",
        "成交额",
        "涨跌幅",
        "换手率",
        "振幅",
    ]
    for c in ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "涨跌幅"]:
        if c not in df.columns:
            raise RuntimeError(f"efinance missing column {c}")
    for c in ["换手率", "振幅"]:
        if c not in df.columns:
            df = df.assign(**{c: pd.NA})
    df["日期"] = pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d")
    return df[out_cols].copy()


def _fetch_stock_tushare(
    symbol: str, start: str, end: str, adjust: str
) -> pd.DataFrame:
    import tushare as ts
    from utils.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise RuntimeError("TUSHARE_TOKEN 未配置")
    ts_code = _to_ts_code(symbol)
    adj_map = {"": None, "qfq": "qfq", "hfq": "hfq"}
    adj_val = adj_map.get(adjust, "qfq")
    # pro_bar 支持复权，pro.daily 仅未复权
    df = ts.pro_bar(ts_code=ts_code, adj=adj_val, start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError("tushare empty")
    df = df.rename(
        columns={
            "trade_date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "vol": "成交量",
            "amount": "成交额",
            "pct_chg": "涨跌幅",
        }
    )
    df["成交量"] = pd.to_numeric(df["成交量"], errors="coerce") * 100  # 手 -> 股
    df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce") * 1000  # 千元 -> 元
    df["换手率"] = pd.NA
    df["振幅"] = pd.NA
    df["日期"] = (
        df["日期"].astype(str).str[:4]
        + "-"
        + df["日期"].astype(str).str[4:6]
        + "-"
        + df["日期"].astype(str).str[6:8]
    )
    return df[
        [
            "日期",
            "开盘",
            "最高",
            "最低",
            "收盘",
            "成交量",
            "成交额",
            "涨跌幅",
            "换手率",
            "振幅",
        ]
    ].copy()


def fetch_stock_hist(
    symbol: str,
    start: str | date,
    end: str | date,
    adjust: Literal["", "qfq", "hfq"] = "qfq",
) -> pd.DataFrame:
    """
    个股日线：akshare → baostock → efinance（各试一次），全失败再用 tushare。
    返回列：日期, 开盘, 最高, 最低, 收盘, 成交量, 成交额, 涨跌幅, 换手率, 振幅
    """
    start_s = (
        start.strftime("%Y%m%d")
        if isinstance(start, date)
        else str(start).replace("-", "")
    )
    end_s = (
        end.strftime("%Y%m%d") if isinstance(end, date) else str(end).replace("-", "")
    )

    failed_sources: list[str] = []

    # 1. akshare
    try:
        return _tag_source(
            _fetch_stock_akshare(symbol, start_s, end_s, adjust), "akshare"
        )
    except ModuleNotFoundError as e:
        failed_sources.append(f"akshare(缺少依赖 {e.name})")
    except Exception:
        failed_sources.append("akshare")

    # 2. baostock (仅前复权)
    try:
        return _tag_source(_fetch_stock_baostock(symbol, start_s, end_s), "baostock")
    except Exception:
        failed_sources.append("baostock")

    # 3. efinance (仅前复权)
    try:
        return _tag_source(_fetch_stock_efinance(symbol, start_s, end_s), "efinance")
    except ModuleNotFoundError as e:
        failed_sources.append(f"efinance(未安装: {e.name})")
    except Exception:
        failed_sources.append("efinance")

    # 4. tushare（可选，未配置则直接报错）
    from utils.tushare_client import get_pro

    if get_pro() is not None:
        try:
            return _tag_source(
                _fetch_stock_tushare(symbol, start_s, end_s, adjust), "tushare"
            )
        except Exception as e:
            raise RuntimeError(
                f"拉取失败（非程序错误）：免费数据源 {', '.join(failed_sources)} 均失败；tushare 也失败：{e}"
            ) from e

    raise RuntimeError(
        f"拉取失败（非程序错误）：免费数据源 {', '.join(failed_sources)} 均无可用数据。可配置 Tushare Token 作为备用。"
    )


# --- 大盘指数 ---


def _fetch_index_tushare(code: str, start: str, end: str) -> pd.DataFrame:
    from utils.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise RuntimeError(
            "拉取失败（非程序错误）：大盘指数需 Tushare Token，免费数据源（akshare 等）不支持大盘指数。请配置 TUSHARE_TOKEN。"
        )
    ts_code = _index_to_ts_code(code)
    df = pro.index_daily(ts_code=ts_code, start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError("拉取失败（非程序错误）：tushare 大盘指数返回空数据")
    df = df.copy()
    df["date"] = (
        df["trade_date"].astype(str).str[:4]
        + "-"
        + df["trade_date"].astype(str).str[4:6]
        + "-"
        + df["trade_date"].astype(str).str[6:8]
    )
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    return df[["date", "open", "high", "low", "close", "volume", "pct_chg"]].copy()


def fetch_index_hist(code: str, start: str | date, end: str | date) -> pd.DataFrame:
    """
    大盘指数日线：直接使用 tushare（免费源大盘 100% 失败，故不试）。
    返回列：date, open, high, low, close, volume, pct_chg（小写，供 step2 使用）
    """
    start_s = (
        start.strftime("%Y%m%d")
        if isinstance(start, date)
        else str(start).replace("-", "")
    )
    end_s = (
        end.strftime("%Y%m%d") if isinstance(end, date) else str(end).replace("-", "")
    )
    return _fetch_index_tushare(code, start_s, end_s)


# --- 行业 & 市值批量获取（tushare） ---

import json
import os
import time
from pathlib import Path

_DATA_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
_SECTOR_CACHE = _DATA_CACHE_DIR / "sector_map_cache.json"
_MARKET_CAP_CACHE = _DATA_CACHE_DIR / "market_cap_cache.json"
_CACHE_TTL = 24 * 60 * 60


def _ts_code_to_symbol(ts_code: str) -> str:
    """000001.SZ -> 000001"""
    return ts_code.split(".")[0] if "." in ts_code else ts_code


def fetch_sector_map() -> dict[str, str]:
    """
    全市场 code->行业映射。优先用缓存，过期后通过 tushare stock_basic 刷新。
    """
    try:
        if (
            _SECTOR_CACHE.exists()
            and (time.time() - _SECTOR_CACHE.stat().st_mtime) < _CACHE_TTL
        ):
            with open(_SECTOR_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass

    from utils.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        try:
            if _SECTOR_CACHE.exists():
                with open(_SECTOR_CACHE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    df = pro.stock_basic(fields="ts_code,industry")
    if df is None or df.empty:
        return {}

    mapping = {}
    for _, row in df.iterrows():
        sym = _ts_code_to_symbol(str(row["ts_code"]))
        industry = str(row.get("industry", "")).strip()
        if sym and industry:
            mapping[sym] = industry

    try:
        _DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SECTOR_CACHE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False)
    except Exception:
        pass

    return mapping


def fetch_market_cap_map() -> dict[str, float]:
    """
    全市场 code->总市值(亿元)。通过 tushare daily_basic 获取最新交易日数据。
    """
    try:
        if (
            _MARKET_CAP_CACHE.exists()
            and (time.time() - _MARKET_CAP_CACHE.stat().st_mtime) < _CACHE_TTL
        ):
            with open(_MARKET_CAP_CACHE, "r", encoding="utf-8") as f:
                return {k: float(v) for k, v in json.load(f).items()}
    except Exception:
        pass

    from utils.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        try:
            if _MARKET_CAP_CACHE.exists():
                with open(_MARKET_CAP_CACHE, "r", encoding="utf-8") as f:
                    return {k: float(v) for k, v in json.load(f).items()}
        except Exception:
            pass
        return {}

    from datetime import date as _date, timedelta as _td

    # 尝试最近几个交易日
    mapping: dict[str, float] = {}
    for offset in range(5):
        d = _date.today() - _td(days=1 + offset)
        trade_date = d.strftime("%Y%m%d")
        try:
            df = pro.daily_basic(trade_date=trade_date, fields="ts_code,total_mv")
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    sym = _ts_code_to_symbol(str(row["ts_code"]))
                    total_mv = row.get("total_mv")
                    if sym and pd.notna(total_mv):
                        mapping[sym] = float(total_mv) / 10000.0  # 万元 -> 亿元
                break
        except Exception:
            continue

    if mapping:
        try:
            _DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_MARKET_CAP_CACHE, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False)
        except Exception:
            pass

    return mapping
