from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from postgrest.exceptions import APIError

try:
    from constants import TABLE_STOCK_CACHE_DATA, TABLE_STOCK_CACHE_META
except ImportError:
    from constants import TABLE_STOCK_CACHE_META

    TABLE_STOCK_CACHE_DATA = "stock_cache_data"
from supabase_client import get_supabase_client


@dataclass
class CacheMeta:
    symbol: str
    adjust: str
    source: str
    start_date: date
    end_date: date
    updated_at: datetime


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
    out = df.rename(columns=reverse).copy()
    return out


def get_cache_meta(symbol: str, adjust: str) -> Optional[CacheMeta]:
    supabase = get_supabase_client()
    try:
        resp = (
            supabase.table(TABLE_STOCK_CACHE_META)
            .select("symbol,adjust,source,start_date,end_date,updated_at")
            .eq("symbol", symbol)
            .eq("adjust", adjust)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None
        row = resp.data[0]
        return CacheMeta(
            symbol=row["symbol"],
            adjust=row["adjust"],
            source=row["source"],
            start_date=datetime.fromisoformat(row["start_date"]).date(),
            end_date=datetime.fromisoformat(row["end_date"]).date(),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
    except APIError:
        return None
    except Exception:
        return None


def load_cached_history(
    symbol: str,
    adjust: str,
    source: str,
    start_date: date,
    end_date: date,
) -> Optional[pd.DataFrame]:
    supabase = get_supabase_client()
    try:
        resp = (
            supabase.table(TABLE_STOCK_CACHE_DATA)
            .select("date,open,high,low,close,volume,amount,pct_chg")
            .eq("symbol", symbol)
            .eq("adjust", adjust)
            .eq("source", source)
            .gte("date", start_date.isoformat())
            .lte("date", end_date.isoformat())
            .order("date")
            .execute()
        )
        if not resp.data:
            return None
        return pd.DataFrame(resp.data)
    except APIError:
        return None
    except Exception:
        return None


def upsert_cache_data(
    symbol: str,
    adjust: str,
    source: str,
    df: pd.DataFrame,
) -> None:
    if df is None or df.empty:
        return
    supabase = get_supabase_client()
    payload = df.copy()
    payload["symbol"] = symbol
    payload["adjust"] = adjust
    payload["source"] = source
    payload["updated_at"] = datetime.utcnow().isoformat()
    records = payload.to_dict(orient="records")
    try:
        supabase.table(TABLE_STOCK_CACHE_DATA).upsert(records).execute()
    except Exception:
        return


def upsert_cache_meta(
    symbol: str,
    adjust: str,
    source: str,
    start_date: date,
    end_date: date,
) -> None:
    supabase = get_supabase_client()
    payload = {
        "symbol": symbol,
        "adjust": adjust,
        "source": source,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    try:
        supabase.table(TABLE_STOCK_CACHE_META).upsert(payload).execute()
    except Exception:
        return


def cleanup_cache(ttl_days: int = 30) -> None:
    supabase = get_supabase_client()
    cutoff = datetime.utcnow() - timedelta(days=ttl_days)
    cutoff_iso = cutoff.isoformat()
    try:
        supabase.table(TABLE_STOCK_CACHE_DATA).delete().lt(
            "updated_at", cutoff_iso
        ).execute()
    except Exception:
        pass
    try:
        supabase.table(TABLE_STOCK_CACHE_META).delete().lt(
            "updated_at", cutoff_iso
        ).execute()
    except Exception:
        pass
