"""统一股票历史数据入口 — 直接从数据源拉取，无 Supabase 缓存层。"""

from __future__ import annotations

from datetime import date
from typing import Literal

import pandas as pd

from integrations.data_source import fetch_stock_hist as fetch_stock_hist_from_source

AdjustType = Literal["", "qfq", "hfq"]

# ─── 纯工具函数（原 core/stock_cache.py，多文件依赖） ───

_COL_MAP = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_chg",
}


def normalize_hist_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(columns=_COL_MAP).copy()
    keep = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
    out = out[[c for c in keep if c in out.columns]].copy()
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "date" in out.columns:
        out["date"] = out["date"].astype(str)
    return out


def denormalize_hist_df(df: pd.DataFrame) -> pd.DataFrame:
    reverse = {v: k for k, v in _COL_MAP.items()}
    return df.rename(columns=reverse).copy()


# ─── 内部辅助 ───


def _collect_tickflow_limit_hints(df: pd.DataFrame | None) -> list[str]:
    if df is None:
        return []
    hints = df.attrs.get("tickflow_limit_hints")
    if isinstance(hints, list):
        out: list[str] = []
        for item in hints:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        if out:
            return out
    one = str(df.attrs.get("tickflow_limit_hint", "") or "").strip()
    return [one] if one else []


def _to_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return pd.to_datetime(str(value)).date()


def _date_str(d: date) -> str:
    return d.isoformat()


def _slice_df_by_date(df: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["date"] = out["date"].astype(str)
    mask = (out["date"] >= _date_str(start_date)) & (out["date"] <= _date_str(end_date))
    out = out.loc[mask].copy()
    if out.empty:
        return out
    return out.sort_values("date").reset_index(drop=True)


# ─── 公开 API ───


def get_stock_hist(
    symbol: str,
    start_date: str | date,
    end_date: str | date,
    adjust: AdjustType = "qfq",
    **_kwargs,
) -> pd.DataFrame:
    """
    统一股票历史数据入口：直接从数据源拉取。
    **_kwargs 吸收历史调用方传入的 context/cache_only/user_id 等已废弃参数，保持兼容。
    """
    start_d = _to_date(start_date)
    end_d = _to_date(end_date)
    if start_d > end_d:
        raise ValueError("start_date 不能晚于 end_date")

    df = fetch_stock_hist_from_source(symbol=symbol, start=start_d, end=end_d, adjust=adjust)
    norm = normalize_hist_df(df)
    result_norm = _slice_df_by_date(norm, start_d, end_d)
    result = denormalize_hist_df(result_norm)
    result.attrs["source"] = "realtime"
    result.attrs["upstream_source"] = str(df.attrs.get("source", "") or "realtime")
    hints = _collect_tickflow_limit_hints(df)
    if hints:
        result.attrs["tickflow_limit_hints"] = hints
        result.attrs["tickflow_limit_hint"] = hints[0]
        print(f"[stock_repo] ⚠️ {hints[0]}")
    return result
