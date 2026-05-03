from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from postgrest.exceptions import APIError
from supabase import Client

from core.constants import TABLE_STOCK_HIST_CACHE
from integrations.supabase_base import create_admin_client as _create_admin_client

_ADMIN_CLIENT: Client | None = None
_CLI_CLIENT: Client | None = None
_CLI_TOKENS: tuple[str, str] = ("", "")
_STOCK_HIST_RETENTION_DAYS = 320


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
    try:
        _ADMIN_CLIENT = _create_admin_client()
    except Exception:
        _ADMIN_CLIENT = None
    return _ADMIN_CLIENT


def set_cli_tokens(access_token: str, refresh_token: str = "") -> None:
    """CLI 登录后调用，注入 user JWT 供缓存层使用。"""
    global _CLI_CLIENT, _CLI_TOKENS
    _CLI_TOKENS = (access_token, refresh_token)
    _CLI_CLIENT = None  # 下次调用时重建


def _get_cli_user_client() -> Client | None:
    global _CLI_CLIENT
    if _CLI_CLIENT is not None:
        return _CLI_CLIENT
    at, rt = _CLI_TOKENS
    if not at:
        return None
    try:
        from integrations.supabase_base import create_user_client

        _CLI_CLIENT = create_user_client(at, rt)
        return _CLI_CLIENT
    except Exception:
        return None


def _get_stock_cache_client(context: str = "auto") -> Client | None:
    """
    context:
    - web/session: 优先使用登录态 session client（RLS）
    - background/admin: 使用 service-role/admin client
    - auto: web session → CLI user client → admin client
    """
    ctx = str(context or "auto").strip().lower()
    try_session = ctx in {"auto", "web", "session"}
    try_admin = ctx in {"auto", "background", "admin"}

    if try_session:
        # 1) Streamlit session client
        try:
            from integrations.supabase_client import get_supabase_client

            client = get_supabase_client()
            if client is not None:
                return client
        except Exception:
            pass
        # 2) CLI user client (access_token)
        cli = _get_cli_user_client()
        if cli is not None:
            return cli
        if ctx in {"web", "session"}:
            return None

    if try_admin:
        return _get_admin_supabase_client()
    return None


def get_cache_meta(symbol: str, adjust: str, *, context: str = "auto") -> CacheMeta | None:
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
    updated_at = _parse_iso_datetime(updated_raw) if updated_raw else datetime.now(UTC)
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
) -> pd.DataFrame | None:
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
    payload["updated_at"] = datetime.now(UTC).isoformat()
    records = payload.to_dict(orient="records")

    try:
        supabase.table(TABLE_STOCK_HIST_CACHE).upsert(records).execute()
        _trim_symbol_history_window(
            supabase=supabase,
            symbol=symbol,
            adjust=adjust,
            retention_days=_STOCK_HIST_RETENTION_DAYS,
        )
        return
    except APIError:
        return
    except Exception:
        return


def _trim_symbol_history_window(
    *,
    supabase: Client,
    symbol: str,
    adjust: str,
    retention_days: int,
) -> None:
    cutoff_date = (datetime.utcnow().date() - timedelta(days=max(retention_days, 1))).isoformat()
    try:
        (
            supabase.table(TABLE_STOCK_HIST_CACHE)
            .delete()
            .eq("symbol", symbol)
            .eq("adjust", adjust)
            .lt("date", cutoff_date)
            .execute()
        )
    except Exception:
        pass


def cleanup_cache(ttl_days: int = _STOCK_HIST_RETENTION_DAYS, *, context: str = "auto") -> None:
    supabase = _get_stock_cache_client(context=context)
    if supabase is None:
        return
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).date().isoformat()
    try:
        supabase.table(TABLE_STOCK_HIST_CACHE).delete().lt("date", cutoff).execute()
    except Exception:
        pass
