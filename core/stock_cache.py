from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
from typing import Optional

import pandas as pd
from postgrest.exceptions import APIError
from supabase import Client, create_client

from core.constants import TABLE_STOCK_HIST_CACHE

_ADMIN_CLIENT: Client | None = None


def _parse_iso_datetime(value: str) -> datetime:
    """Parse ISO datetime strings and accept trailing 'Z' timezone markers."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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


def _parse_iso_date(value: str) -> date:
    return pd.to_datetime(str(value)).date()


def _get_admin_supabase_client() -> Client | None:
    global _ADMIN_CLIENT
    if _ADMIN_CLIENT is not None:
        return _ADMIN_CLIENT
    url = str(os.getenv("SUPABASE_URL", "") or "").strip()
    key = str(
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.getenv("SUPABASE_KEY", "")
        or ""
    ).strip()
    if not url or not key:
        return None
    try:
        _ADMIN_CLIENT = create_client(url, key)
    except Exception:
        _ADMIN_CLIENT = None
    return _ADMIN_CLIENT


def _get_stock_cache_client(context: str = "auto") -> Client | None:
    """
    context:
    - web/session: 优先使用登录态 session client（RLS）
    - background/admin: 使用 service-role/admin client
    - auto: session 优先，失败后回退 admin client
    """
    ctx = str(context or "auto").strip().lower()
    try_session = ctx in {"auto", "web", "session"}
    try_admin = ctx in {"auto", "background", "admin"}

    if try_session:
        try:
            from integrations.supabase_client import get_supabase_client

            client = get_supabase_client()
            if client is not None:
                return client
        except Exception:
            if ctx in {"web", "session"}:
                return None

    if try_admin:
        return _get_admin_supabase_client()
    return None


def get_cache_meta(symbol: str, adjust: str, *, context: str = "auto") -> Optional[CacheMeta]:
    supabase = _get_stock_cache_client(context=context)
    if supabase is None:
        return None
    first_resp = (
        supabase.table(TABLE_STOCK_HIST_CACHE)
        .select("date")
        .eq("symbol", symbol)
        .eq("adjust", adjust)
        .order("date", desc=False)
        .limit(1)
        .execute()
    )
    if not first_resp.data:
        return None

    last_resp = (
        supabase.table(TABLE_STOCK_HIST_CACHE)
        .select("date,updated_at")
        .eq("symbol", symbol)
        .eq("adjust", adjust)
        .order("date", desc=True)
        .limit(1)
        .execute()
    )
    if not last_resp.data:
        return None

    first_row = first_resp.data[0]
    last_row = last_resp.data[0]
    updated_raw = last_row.get("updated_at")
    updated_at = _parse_iso_datetime(updated_raw) if updated_raw else datetime.utcnow()
    return CacheMeta(
        symbol=symbol,
        adjust=adjust,
        source="cache",
        start_date=_parse_iso_date(first_row["date"]),
        end_date=_parse_iso_date(last_row["date"]),
        updated_at=updated_at,
    )


def load_cached_history(
    symbol: str,
    adjust: str,
    source: str,
    start_date: date,
    end_date: date,
    *,
    context: str = "auto",
) -> Optional[pd.DataFrame]:
    supabase = _get_stock_cache_client(context=context)
    if supabase is None:
        return None
    try:
        resp = (
            supabase.table(TABLE_STOCK_HIST_CACHE)
            .select("date,open,high,low,close,volume,amount,pct_chg")
            .eq("symbol", symbol)
            .eq("adjust", adjust)
            .gte("date", start_date.isoformat())
            .lte("date", end_date.isoformat())
            .order("date")
            .execute()
        )
        if resp.data:
            return pd.DataFrame(resp.data)
    except APIError:
        return None
    except Exception:
        return None
    return None


def upsert_cache_data(
    symbol: str,
    adjust: str,
    source: str,
    df: pd.DataFrame,
    *,
    context: str = "auto",
) -> None:
    if df is None or df.empty:
        return
    supabase = _get_stock_cache_client(context=context)
    if supabase is None:
        return

    payload = df.copy()
    payload["date"] = payload["date"].astype(str)
    payload["symbol"] = symbol
    payload["adjust"] = adjust
    payload["updated_at"] = datetime.utcnow().isoformat()
    records = payload.to_dict(orient="records")

    try:
        supabase.table(TABLE_STOCK_HIST_CACHE).upsert(records).execute()
        return
    except APIError:
        return
    except Exception:
        return

def upsert_cache_meta(
    symbol: str,
    adjust: str,
    source: str,
    start_date: date,
    end_date: date,
    *,
    context: str = "auto",
) -> None:
    # breaking change: 单表架构不再维护独立 meta 表
    _ = (symbol, adjust, source, start_date, end_date, context)
    return


def cleanup_cache(ttl_days: int = 30, *, context: str = "auto") -> None:
    supabase = _get_stock_cache_client(context=context)
    if supabase is None:
        return
    cutoff = datetime.utcnow() - timedelta(days=ttl_days)
    cutoff_iso = cutoff.isoformat()
    try:
        supabase.table(TABLE_STOCK_HIST_CACHE).delete().lt(
            "updated_at", cutoff_iso
        ).execute()
    except Exception:
        pass
