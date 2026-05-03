from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

import pandas as pd

from core.stock_cache import (
    CacheMeta,
    denormalize_hist_df,
    get_cache_meta,
    load_cached_history,
    normalize_hist_df,
    upsert_cache_data,
)
from integrations.data_source import fetch_stock_hist as fetch_stock_hist_from_source

AdjustType = Literal["", "qfq", "hfq"]


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


def _load_from_md_tables(
    symbol: str, start_date: date, end_date: date, adjust: AdjustType, context: str
) -> pd.DataFrame | None:
    """
    预留给未来的 md_* 行情大表（Supabase）读取扩展。
    当前版本仅复用 stock_cache_*，故返回 None。
    """
    _ = (symbol, start_date, end_date, adjust, context)
    return None


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


def _merge_norm_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [f for f in frames if f is not None and not f.empty]
    if not valid:
        return pd.DataFrame()
    out = pd.concat(valid, ignore_index=True)
    out["date"] = out["date"].astype(str)
    out = out.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return out.reset_index(drop=True)


def _can_use_cache_when_tail_gap_fails(
    gap_start: date,
    meta: CacheMeta | None,
    cached_norm: pd.DataFrame | None,
) -> bool:
    """Allow stale-but-current cache when only a future/non-trading tail gap fails."""
    return meta is not None and cached_norm is not None and not cached_norm.empty and gap_start > meta.end_date


def _compute_gap_ranges(requested_start: date, requested_end: date, meta: CacheMeta | None) -> list[tuple[date, date]]:
    if meta is None:
        return [(requested_start, requested_end)]
    gaps: list[tuple[date, date]] = []
    if requested_start < meta.start_date:
        gaps.append((requested_start, meta.start_date - timedelta(days=1)))
    if requested_end > meta.end_date:
        gaps.append((meta.end_date + timedelta(days=1), requested_end))
    return [(s, e) for s, e in gaps if s <= e]


def _fetch_gap(symbol: str, start_date: date, end_date: date, adjust: AdjustType) -> tuple[pd.DataFrame, str]:
    df = fetch_stock_hist_from_source(
        symbol=symbol,
        start=start_date,
        end=end_date,
        adjust=adjust,
    )
    norm = normalize_hist_df(df)
    upstream_source = str(df.attrs.get("source", "") or "realtime")
    norm.attrs["upstream_source"] = upstream_source
    hints = _collect_tickflow_limit_hints(df)
    if hints:
        norm.attrs["tickflow_limit_hints"] = hints
        norm.attrs["tickflow_limit_hint"] = hints[0]
    return norm, upstream_source


def get_stock_hist(
    symbol: str,
    start_date: str | date,
    end_date: str | date,
    adjust: AdjustType = "qfq",
    *,
    context: str = "auto",
    cache_only: bool = False,
) -> pd.DataFrame:
    """
    统一股票历史数据入口：
    1) Supabase 缓存优先
    2) 缺口补拉（cache_only=True 时跳过，直接返回缓存中已有的数据）
    3) 回写缓存后返回
    """
    start_d = _to_date(start_date)
    end_d = _to_date(end_date)
    if start_d > end_d:
        raise ValueError("start_date 不能晚于 end_date")

    md_df = _load_from_md_tables(symbol, start_d, end_d, adjust, context)
    if md_df is not None and not md_df.empty:
        out = _slice_df_by_date(normalize_hist_df(md_df), start_d, end_d)
        out_cn = denormalize_hist_df(out)
        out_cn.attrs["source"] = "supabase_md"
        return out_cn

    cache_adjust = adjust or "none"

    # 不复权数据不走缓存，直接拉取（节省 Supabase 存储）
    if cache_adjust == "none":
        df = fetch_stock_hist_from_source(symbol=symbol, start=start_d, end=end_d, adjust=adjust)
        norm = normalize_hist_df(df)
        result_norm = _slice_df_by_date(norm, start_d, end_d)
        result = denormalize_hist_df(result_norm)
        result.attrs["source"] = "realtime"
        result.attrs["upstream_source"] = str(df.attrs.get("source", "") or "realtime")
        result.attrs["cache_status"] = "bypass"
        hints = _collect_tickflow_limit_hints(df)
        if hints:
            result.attrs["tickflow_limit_hints"] = hints
            result.attrs["tickflow_limit_hint"] = hints[0]
            print(f"[stock_repo] ⚠️ {hints[0]}")
        return result

    meta = get_cache_meta(symbol, cache_adjust, context=context)
    cached_norm: pd.DataFrame | None = None
    if meta is not None:
        cached_norm = load_cached_history(
            symbol=symbol,
            adjust=cache_adjust,
            source=meta.source,
            start_date=meta.start_date,
            end_date=meta.end_date,
            context=context,
        )

    # cache_only 模式：只返回缓存中已有的数据，不去 tushare 补拉
    if cache_only:
        if cached_norm is not None and not cached_norm.empty:
            result_norm = _slice_df_by_date(cached_norm, start_d, end_d)
            result = denormalize_hist_df(result_norm)
            result.attrs["source"] = "cache"
            print(
                f"[stock_repo] cache_only symbol={symbol} adjust={cache_adjust} "
                f"range={start_d}..{end_d} rows={len(result_norm)}"
            )
            return result
        print(
            f"[stock_repo] cache_only symbol={symbol} adjust={cache_adjust} range={start_d}..{end_d} rows=0 (no cache)"
        )
        return pd.DataFrame()

    gaps = _compute_gap_ranges(start_d, end_d, meta)
    fetched_frames: list[pd.DataFrame] = []
    tickflow_limit_hints: list[str] = []
    upstream_sources: list[str] = []
    skipped_tail_gap_error = ""
    did_fetch = False

    for gap_start, gap_end in gaps:
        did_fetch = True
        print(
            f"[stock_repo] cache_miss symbol={symbol} adjust={cache_adjust} "
            f"range={gap_start}..{gap_end} context={context}"
        )
        try:
            frame, upstream_source = _fetch_gap(symbol, gap_start, gap_end, adjust)
        except Exception as e:
            if _can_use_cache_when_tail_gap_fails(gap_start, meta, cached_norm):
                skipped_tail_gap_error = f"{type(e).__name__}: {str(e)[:240]}"
                print(
                    f"[stock_repo] tail_gap_fetch_failed_use_cache symbol={symbol} "
                    f"adjust={cache_adjust} gap={gap_start}..{gap_end} "
                    f"cached_until={meta.end_date} err={skipped_tail_gap_error}"
                )
                continue
            raise
        fetched_frames.append(frame)
        if upstream_source and upstream_source not in upstream_sources:
            upstream_sources.append(upstream_source)
        for hint in _collect_tickflow_limit_hints(frame):
            if hint not in tickflow_limit_hints:
                tickflow_limit_hints.append(hint)

    merged = _merge_norm_frames([cached_norm] + fetched_frames if cached_norm is not None else fetched_frames)

    if merged.empty:
        # 缓存无可用数据且补拉失败时，按原行为抛错
        # （fetch_stock_hist_from_source 内部会提供详细数据源失败信息）
        did_fetch = True
        frame, upstream_source = _fetch_gap(symbol, start_d, end_d, adjust)
        merged = frame
        if upstream_source and upstream_source not in upstream_sources:
            upstream_sources.append(upstream_source)
        for hint in _collect_tickflow_limit_hints(frame):
            if hint not in tickflow_limit_hints:
                tickflow_limit_hints.append(hint)

    result_norm = _slice_df_by_date(merged, start_d, end_d)
    if result_norm.empty:
        # 防御性兜底：强制拉取完整窗口
        did_fetch = True
        frame, upstream_source = _fetch_gap(symbol, start_d, end_d, adjust)
        result_norm = _slice_df_by_date(frame, start_d, end_d)
        merged = _merge_norm_frames([merged, frame])
        if upstream_source and upstream_source not in upstream_sources:
            upstream_sources.append(upstream_source)
        for hint in _collect_tickflow_limit_hints(frame):
            if hint not in tickflow_limit_hints:
                tickflow_limit_hints.append(hint)
    chosen_source = "cache"

    # 有缺口补拉或首次拉取时回写缓存；纯命中时不重复写
    if did_fetch or meta is None:
        new_start = min(start_d, meta.start_date) if meta else start_d
        new_end = max(end_d, meta.end_date) if meta else end_d
        upsert_cache_data(
            symbol=symbol,
            adjust=cache_adjust,
            source=chosen_source,
            df=merged,
            context=context,
        )
        print(
            f"[stock_repo] cache_upsert symbol={symbol} adjust={cache_adjust} "
            f"rows={len(merged)} coverage={new_start}..{new_end} source={chosen_source}"
        )
    else:
        print(
            f"[stock_repo] cache_hit symbol={symbol} adjust={cache_adjust} "
            f"range={start_d}..{end_d} rows={len(result_norm)}"
        )

    result = denormalize_hist_df(result_norm)
    result.attrs["source"] = chosen_source
    if upstream_sources:
        result.attrs["upstream_source"] = upstream_sources[-1]
        result.attrs["upstream_sources"] = upstream_sources
    elif meta is not None:
        result.attrs["upstream_source"] = str(meta.source or "cache")
    if skipped_tail_gap_error:
        result.attrs["cache_status"] = "hit_tail_gap_skipped"
        result.attrs["tail_gap_error"] = skipped_tail_gap_error
        result.attrs["cached_until"] = meta.end_date.isoformat() if meta else ""
    else:
        result.attrs["cache_status"] = "miss_refilled" if did_fetch or meta is None else "hit"
    if tickflow_limit_hints:
        result.attrs["tickflow_limit_hints"] = tickflow_limit_hints
        result.attrs["tickflow_limit_hint"] = tickflow_limit_hints[0]
        print(f"[stock_repo] ⚠️ {tickflow_limit_hints[0]}")
    return result
