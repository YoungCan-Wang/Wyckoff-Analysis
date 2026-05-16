"""
Supabase 形态复盘数据存取模块
"""

from __future__ import annotations

import logging
import os
from bisect import bisect_right
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from core.constants import (
    TABLE_RECOMMENDATION_TRACKING,
    TABLE_RECOMMENDATION_TRACKING_HK,
    TABLE_RECOMMENDATION_TRACKING_US,
)
from integrations.supabase_base import create_admin_client as _get_supabase_admin_client
from integrations.supabase_base import is_admin_configured as is_supabase_configured

logger = logging.getLogger(__name__)


def _fetch_all_tracking_records(client, select_expr: str = "*", page_size: int = 1000) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = max(min(int(page_size), 1000), 1)
    start = 0
    while True:
        resp = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select(select_expr)
            .order("recommend_date", desc=False)
            .order("id", desc=False)
            .range(start, start + page - 1)
            .execute()
        )
        batch = resp.data or []
        records.extend(batch)
        if len(batch) < page:
            return records
        start += page


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except Exception:
        return default
    return value if pd.notna(value) else default


def _code6(raw: Any) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    step = max(int(size), 1)
    return [items[i : i + step] for i in range(0, len(items), step)]


def _upsert_tracking_updates(client, updates: list[dict[str, Any]], batch_size: int = 500) -> int:
    written = 0
    clean_updates = [row for row in updates if row.get("code") is not None and row.get("recommend_date") is not None]
    for chunk in _chunked(clean_updates, max(min(int(batch_size), 1000), 1)):
        client.table(TABLE_RECOMMENDATION_TRACKING).upsert(chunk, on_conflict="code,recommend_date").execute()
        written += len(chunk)
    return written


def _resolve_tickflow_quote_price(quote: dict[str, Any] | None) -> float:
    row = quote or {}
    for key in ("last_price", "close", "last", "price", "current"):
        value = _safe_float(row.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def _quote_trade_date_yyyymmdd(quote: dict[str, Any] | None) -> str:
    timestamp_ms = _safe_float((quote or {}).get("timestamp"), 0.0)
    if timestamp_ms <= 0:
        return ""
    timestamp_s = timestamp_ms / 1000.0 if timestamp_ms > 10_000_000_000 else timestamp_ms
    try:
        return datetime.fromtimestamp(timestamp_s, UTC).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    except Exception:
        return ""


def _close_map_from_tickflow_hist(hist: pd.DataFrame | None) -> dict[str, float]:
    if hist is None or hist.empty or not {"date", "close"}.issubset(hist.columns):
        return {}
    work = hist[["date", "close"]].copy()
    work["trade_date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y%m%d")
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["trade_date", "close"])
    work = work[work["close"] > 0]
    return {str(d): float(px) for d, px in zip(work["trade_date"], work["close"])}


def _ohlc_map_from_tickflow_hist(hist: pd.DataFrame | None) -> dict[str, dict[str, float]]:
    if hist is None or hist.empty or not {"date", "high", "low", "close"}.issubset(hist.columns):
        return {}
    work = hist[["date", "high", "low", "close"]].copy()
    work["trade_date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y%m%d")
    for col in ("high", "low", "close"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["trade_date", "high", "low", "close"])
    work = work[(work["high"] > 0) & (work["low"] > 0) & (work["close"] > 0)]
    return {
        str(row.trade_date): {"high": float(row.high), "low": float(row.low), "close": float(row.close)}
        for row in work.itertuples(index=False)
    }


def _fetch_tickflow_tracking_market_data(
    api_key: str,
    symbols: list[str],
    batch_size: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, pd.DataFrame]]:
    from integrations.tickflow_client import TickFlowClient

    tf_client = TickFlowClient(api_key=api_key)
    quotes: dict[str, dict[str, Any]] = {}
    hist_map: dict[str, pd.DataFrame] = {}
    for chunk in _chunked(symbols, batch_size):
        quotes.update(tf_client.get_quotes(chunk))
        hist_map.update(tf_client.get_klines_batch(chunk, period="1d", count=120, adjust="none"))
    return quotes, hist_map


def _build_tickflow_tracking_updates(
    grouped: dict[str, list[dict[str, Any]]],
    quotes: dict[str, dict[str, Any]],
    hist_map: dict[str, pd.DataFrame],
    now_iso: str,
) -> tuple[list[dict[str, Any]], int, str]:
    from integrations.tickflow_client import normalize_cn_symbol

    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_trade_date_global = ""
    for code6, rows in grouped.items():
        sym = normalize_cn_symbol(code6)
        quote = quotes.get(sym) or {}
        current_price = _resolve_tickflow_quote_price(quote)
        quote_trade_date = _quote_trade_date_yyyymmdd(quote)
        if quote_trade_date and quote_trade_date > latest_trade_date_global:
            latest_trade_date_global = quote_trade_date

        close_map = _close_map_from_tickflow_hist(hist_map.get(sym))
        trade_dates = sorted(close_map)
        if current_price <= 0 and trade_dates:
            current_price = float(close_map[trade_dates[-1]])
        if trade_dates and trade_dates[-1] > latest_trade_date_global:
            latest_trade_date_global = trade_dates[-1]
        if current_price <= 0 or not trade_dates:
            codes_no_data += 1
            continue

        for row in rows:
            rec_date = _recommend_date_to_yyyymmdd(row.get("recommend_date"))
            pick_date = _pick_close_on_or_before(trade_dates, rec_date)
            initial_close = float(close_map.get(pick_date, 0.0)) if pick_date else 0.0
            if initial_close <= 0:
                continue
            updates.append(
                {
                    "id": row.get("id"),
                    "code": int(code6),
                    "recommend_date": int(rec_date) if rec_date.isdigit() else None,
                    "initial_price": round(initial_close, 4),
                    "current_price": round(current_price, 4),
                    "change_pct": round((current_price - initial_close) / initial_close * 100.0, 2),
                    "updated_at": now_iso,
                }
            )
    return updates, codes_no_data, latest_trade_date_global


def _parse_recommend_date(raw_value: Any) -> date | None:
    if raw_value is None:
        return None
    s = str(raw_value).strip()
    if not s:
        return None
    try:
        if len(s) == 8 and s.isdigit():
            return datetime.strptime(s, "%Y%m%d").date()
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _parse_write_date(record: dict[str, Any]) -> date | None:
    """优先用 recommend_date，没有则回退 created_at。"""
    rec_date = _parse_recommend_date(record.get("recommend_date"))
    if rec_date is not None:
        return rec_date

    created = record.get("created_at")
    if created is not None and str(created).strip():
        try:
            s = str(created).strip()
            if "T" in s or " " in s:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
            if len(s) == 8 and s.isdigit():
                return datetime.strptime(s, "%Y%m%d").date()
            return datetime.fromisoformat(s).date()
        except Exception:
            logger.debug("failed to parse created_at date: %s", created, exc_info=True)
    return None


def _resolve_initial_price_from_history(code_str: str, rec_date: date) -> float:
    """
    用推荐日附近历史日线回填加入价：
    1) 优先 rec_date 当天
    2) 若当天无数据，回看最近 7 天并取 <= rec_date 的最近交易日
    """
    try:
        from integrations.data_source import fetch_stock_hist

        rec_s = rec_date.strftime("%Y-%m-%d")
        hist = fetch_stock_hist(code_str, rec_s, rec_s, adjust="qfq")
        if hist is not None and not hist.empty:
            close_s = pd.to_numeric(hist.get("收盘"), errors="coerce").dropna()
            if not close_s.empty:
                px = float(close_s.iloc[-1])
                if px > 0:
                    return px

        start_s = (rec_date - timedelta(days=7)).strftime("%Y-%m-%d")
        hist2 = fetch_stock_hist(code_str, start_s, rec_s, adjust="qfq")
        if hist2 is None or hist2.empty:
            return 0.0
        df = hist2.copy()
        if "日期" not in df.columns or "收盘" not in df.columns:
            return 0.0
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        df["收盘"] = pd.to_numeric(df["收盘"], errors="coerce")
        df = df.dropna(subset=["日期", "收盘"]).sort_values("日期")
        if df.empty:
            return 0.0
        df = df[df["日期"].dt.date <= rec_date]
        if df.empty:
            return 0.0
        px = float(df["收盘"].iloc[-1])
        return px if px > 0 else 0.0
    except Exception:
        return 0.0


def upsert_recommendations(recommend_date: int, symbols_info: list[dict[str, Any]]) -> bool:
    """
    将每日选出的股票存入形态复盘表
    recommend_date: YYYYMMDD (int)
    """
    if not is_supabase_configured() or not symbols_info:
        return False
    try:
        client = _get_supabase_admin_client()

        existing_counts: dict[int, int] = {}
        existing_code_dates: dict[int, set[int]] = {}
        try:
            all_rows = _fetch_all_tracking_records(client, "code,recommend_count,recommend_date")
            for row in all_rows:
                try:
                    code_int = int(row.get("code"))
                except (TypeError, ValueError):
                    continue
                cnt = int(row.get("recommend_count") or 1) if row.get("recommend_count") else 1
                existing_counts[code_int] = max(existing_counts.get(code_int, 0), cnt)
                try:
                    d = int(row.get("recommend_date"))
                    existing_code_dates.setdefault(code_int, set()).add(d)
                except (TypeError, ValueError):
                    logger.debug("invalid recommend_date for code %s", row.get("code"), exc_info=True)
        except Exception:
            existing_counts = {}
            existing_code_dates = {}

        payload = []
        for s in symbols_info:
            raw_code = str(s.get("code", "")).strip()
            # 提取纯数字部分 (比如 "000001.SZ" -> "000001")
            code_str = "".join(filter(str.isdigit, raw_code))
            if not code_str:
                continue

            # price 优先使用 step2 传入的 initial_price，并做多字段兜底
            price = 0.0
            for key in ("initial_price", "current_price", "price", "latest_price", "close"):
                raw_price = s.get(key)
                if raw_price is None or raw_price == "":
                    continue
                try:
                    parsed = float(raw_price)
                except Exception:
                    continue
                if parsed > 0:
                    price = parsed
                    break

            score_val: float | None = None
            for score_key in ("funnel_score", "priority_score", "score"):
                raw_score = s.get(score_key)
                if raw_score is None or raw_score == "":
                    continue
                try:
                    score_val = float(raw_score)
                    break
                except Exception:
                    continue

            code_int = int(code_str)
            old_cnt = existing_counts.get(code_int, 0)
            seen_dates = existing_code_dates.get(code_int, set())
            if old_cnt <= 0:
                new_cnt = 1
            elif recommend_date in seen_dates:
                new_cnt = old_cnt
            else:
                new_cnt = old_cnt + 1

            payload.append(
                {
                    "code": code_int,  # 存为 INT，首位0会消失
                    "name": str(s.get("name", "")).strip(),
                    "recommend_reason": str(s.get("tag", "")).strip(),
                    "recommend_date": recommend_date,
                    "initial_price": price,
                    "current_price": price,  # 初始时当前价等于加入价
                    "change_pct": 0.0,  # 初始涨跌幅为 0
                    "recommend_count": new_cnt,
                    "funnel_score": score_val,
                    "is_ai_recommended": False,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )

        if payload:
            # 使用 upsert，基于 (code, recommend_date) 唯一约束：
            # - 同一只股票在同一天重跑会覆盖更新；
            # - 跨天会新增一条记录；
            # - recommend_count 按 code 维度累计。
            try:
                client.table(TABLE_RECOMMENDATION_TRACKING).upsert(payload, on_conflict="code,recommend_date").execute()
            except Exception as e:
                msg = str(e).lower()
                optional_cols = ("is_ai_recommended", "funnel_score", "recommend_count")
                if any(col in msg for col in optional_cols):
                    fallback_payload: list[dict[str, Any]] = []
                    for row in payload:
                        r = dict(row)
                        for col in optional_cols:
                            r.pop(col, None)
                        fallback_payload.append(r)
                    client.table(TABLE_RECOMMENDATION_TRACKING).upsert(
                        fallback_payload, on_conflict="code,recommend_date"
                    ).execute()
                else:
                    raise
        return True
    except Exception as e:
        print(f"[supabase_recommendation] upsert_recommendations failed: {e}")
        return False


def mark_ai_recommendations(recommend_date: int, ai_codes: list[str]) -> bool:
    """
    将某个推荐日的记录标记为是否 AI 推荐（可操作池）。
    ai_codes 传入 6 位代码字符串列表。
    """
    if not is_supabase_configured():
        return False
    try:
        client = _get_supabase_admin_client()
        now_iso = datetime.now(UTC).isoformat()
        # 先全量置 false，再对白名单置 true，避免前一次残留。
        client.table(TABLE_RECOMMENDATION_TRACKING).update({"is_ai_recommended": False, "updated_at": now_iso}).eq(
            "recommend_date", recommend_date
        ).execute()

        code_ints: list[int] = []
        for code in ai_codes or []:
            code_digits = "".join(ch for ch in str(code) if ch.isdigit())
            if not code_digits:
                continue
            try:
                code_ints.append(int(code_digits))
            except Exception:
                continue
        code_ints = sorted(set(code_ints))
        if code_ints:
            client.table(TABLE_RECOMMENDATION_TRACKING).update({"is_ai_recommended": True, "updated_at": now_iso}).eq(
                "recommend_date", recommend_date
            ).in_("code", code_ints).execute()
        return True
    except Exception as e:
        msg = str(e)
        if "is_ai_recommended" in msg:
            print(
                "[supabase_recommendation] mark_ai_recommendations skipped: "
                "missing column is_ai_recommended (please run SQL migration)"
            )
            return False
        print(f"[supabase_recommendation] mark_ai_recommendations failed: {e}")
        return False


def _resolve_price(code_str, price_map, history_fn, spot_fn) -> float | None:
    if price_map:
        try:
            px = float(price_map.get(code_str) or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            return px
    px = history_fn(code_str)
    if px is not None:
        return px
    return spot_fn(code_str)


def _build_price_update_row(record: dict, new_price: float, code_str: str, now_iso: str) -> dict:
    row: dict = {"id": record["id"], "current_price": new_price, "updated_at": now_iso}
    initial_price = float(record.get("initial_price") or 0.0)
    if initial_price > 0:
        row["change_pct"] = round((new_price - initial_price) / initial_price * 100.0, 2)
    else:
        rec_date = _parse_recommend_date(record.get("recommend_date"))
        backfill = _resolve_initial_price_from_history(code_str, rec_date) if rec_date else 0.0
        if backfill <= 0:
            backfill = new_price
        row["initial_price"] = backfill
        row["change_pct"] = round((new_price - backfill) / backfill * 100.0, 2) if backfill > 0 else 0.0
    return row


def sync_all_tracking_prices(
    price_map: dict[str, float] | None = None,
) -> int:
    """
    遍历表中所有股票，用最新价刷新 current_price 与 change_pct。
    price_map: 可选，code_str -> 最新收盘价。非空时优先使用；
    对缺失代码优先回退到历史日线收盘（qfq），最后才按开关尝试实时快照。
    返回成功更新的数量。
    """
    if not is_supabase_configured():
        print("[supabase_recommendation] sync_all_tracking_prices: Supabase 未配置，跳过")
        return 0

    try:
        client = _get_supabase_admin_client()
        allow_spot_fallback = os.getenv("RECOMMENDATION_PRICE_ALLOW_SPOT_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        # 获取需要跟踪的股票代码（去重）
        code_rows = _fetch_all_tracking_records(client, "id,code")
        if not code_rows:
            print("[supabase_recommendation] sync_all_tracking_prices: 推荐表无记录，跳过")
            return 0

        unique_codes = sorted({int(r["code"]) for r in code_rows if r.get("code") is not None})

        # 统一日线窗口（与 step2 同口径），避免实时快照不稳定导致脏数据。
        hist_start_s: str | None = None
        hist_end_s: str | None = None
        hist_close_cache: dict[str, float] = {}
        try:
            from integrations.fetch_a_share_csv import _resolve_trading_window
            from utils.trading_clock import resolve_end_calendar_day

            window = _resolve_trading_window(
                end_calendar_day=resolve_end_calendar_day(),
                trading_days=20,
            )
            hist_start_s = window.start_trade_date.strftime("%Y-%m-%d")
            hist_end_s = window.end_trade_date.strftime("%Y-%m-%d")
        except Exception:
            hist_start_s = None
            hist_end_s = None

        def _price_from_history(code_str: str) -> float | None:
            if code_str in hist_close_cache:
                cached = hist_close_cache[code_str]
                return cached if cached > 0 else None
            if not hist_start_s or not hist_end_s:
                hist_close_cache[code_str] = 0.0
                return None
            try:
                from integrations.data_source import fetch_stock_hist

                hist = fetch_stock_hist(
                    code_str,
                    hist_start_s,
                    hist_end_s,
                    adjust="qfq",
                )
                if hist is None or hist.empty or "收盘" not in hist.columns:
                    hist_close_cache[code_str] = 0.0
                    return None
                close_s = pd.to_numeric(hist.get("收盘"), errors="coerce").dropna()
                if close_s.empty:
                    hist_close_cache[code_str] = 0.0
                    return None
                px = float(close_s.iloc[-1])
                hist_close_cache[code_str] = px if px > 0 else 0.0
                return px if px > 0 else None
            except Exception:
                hist_close_cache[code_str] = 0.0
                return None

        def _price_from_spot(code_str: str) -> float | None:
            if not allow_spot_fallback:
                return None
            try:
                from integrations.data_source import fetch_stock_spot_snapshot

                snap = fetch_stock_spot_snapshot(code_str, force_refresh=False)
                if not snap or snap.get("close") is None:
                    return None
                px = float(snap["close"])
                return px if px > 0 else None
            except Exception:
                return None

        all_records = _fetch_all_tracking_records(client, "*")
        records_by_code: dict[int, list[dict]] = {}
        for rec in all_records:
            try:
                c = int(rec.get("code"))
            except (TypeError, ValueError):
                continue
            records_by_code.setdefault(c, []).append(rec)

        updated_count = 0
        upsert_batch: list[dict] = []
        now_iso = datetime.now(UTC).isoformat()

        for code_int in unique_codes:
            code_str = f"{code_int:06d}"
            new_current_price = _resolve_price(code_str, price_map, _price_from_history, _price_from_spot)
            if new_current_price is None:
                continue
            for record in records_by_code.get(code_int, []):
                row = _build_price_update_row(record, new_current_price, code_str, now_iso)
                upsert_batch.append(row)
                if len(upsert_batch) >= 50:
                    client.table(TABLE_RECOMMENDATION_TRACKING).upsert(upsert_batch, on_conflict="id").execute()
                    updated_count += len(upsert_batch)
                    upsert_batch = []

        if upsert_batch:
            client.table(TABLE_RECOMMENDATION_TRACKING).upsert(upsert_batch, on_conflict="id").execute()
            updated_count += len(upsert_batch)

        if unique_codes and updated_count == 0:
            print(
                f"[supabase_recommendation] sync_all_tracking_prices: 推荐表有 {len(unique_codes)} 只股票但 0 条更新，"
                "可能是 price_map 为空且历史/实时行情均不可用"
            )
        return updated_count
    except Exception as e:
        print(f"[supabase_recommendation] sync_all_tracking_prices failed: {e}")
        return 0


def correct_tracking_initial_prices() -> int:
    """
    纠错流程：遍历推荐表每条记录，用「推荐日」当天收盘价（前复权）回填 initial_price，
    并用当前 current_price 重算 change_pct。
    每日执行可让历史数据逐步修正。
    返回被更新的记录数。
    """
    if not is_supabase_configured():
        print("[supabase_recommendation] correct_tracking_initial_prices: Supabase 未配置，跳过")
        return 0
    try:
        client = _get_supabase_admin_client()
        records = _fetch_all_tracking_records(client, "*")
        if not records:
            return 0
        cache: dict[tuple[str, date], float] = {}
        updated = 0
        for record in records:
            write_date = _parse_write_date(record)
            if not write_date:
                continue
            code_int = record.get("code")
            if code_int is None:
                continue
            code_str = f"{int(code_int):06d}"
            current_price = float(record.get("current_price") or 0.0)
            if current_price <= 0:
                continue
            key = (code_str, write_date)
            if key not in cache:
                cache[key] = _resolve_initial_price_from_history(code_str, write_date)
            initial_from_hist = cache[key]
            if initial_from_hist <= 0:
                continue
            change_pct = round((current_price - initial_from_hist) / initial_from_hist * 100.0, 2)
            client.table(TABLE_RECOMMENDATION_TRACKING).update(
                {
                    "initial_price": initial_from_hist,
                    "change_pct": change_pct,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ).eq("id", record["id"]).execute()
            updated += 1
        return updated
    except Exception as e:
        print(f"[supabase_recommendation] correct_tracking_initial_prices failed: {e}")
        return 0


def load_recommendation_tracking(limit: int = 1000, client=None) -> list[dict[str, Any]]:
    """加载形态复盘数据"""
    try:
        if client is None:
            client = _get_supabase_admin_client()
        resp = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select("*")
            .order("recommend_date", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        print(f"[supabase_recommendation] load_recommendation_tracking failed: {e}")
        return []


def _to_ts_code_recommendation(symbol: str) -> str:
    s = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    s = s[-6:].zfill(6)
    if s.startswith(("600", "601", "603", "605", "688")):
        return f"{s}.SH"
    return f"{s}.SZ"


def _recommend_date_to_yyyymmdd(raw: Any) -> str:
    d = _parse_recommend_date(raw)
    if d is None:
        return ""
    return d.strftime("%Y%m%d")


def _pick_close_on_or_before(sorted_trade_dates: list[str], target_yyyymmdd: str) -> str:
    if not sorted_trade_dates or not target_yyyymmdd:
        return ""
    i = bisect_right(sorted_trade_dates, target_yyyymmdd) - 1
    if i < 0:
        return ""
    return sorted_trade_dates[i]


def refresh_tracking_prices_with_tushare_unadjusted() -> dict[str, Any]:
    """
    使用 Tushare（日线不复权）回填并刷新形态复盘价格：
    - initial_price: 推荐日（或之前最近交易日）收盘价
    - current_price: 当前系统时间对应最近交易日收盘价
    - change_pct: (current - initial) / initial * 100
    """
    from integrations.tushare_client import get_pro

    if not is_supabase_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")

    pro = get_pro()
    if pro is None:
        raise ValueError("TUSHARE_TOKEN 未配置或 tushare 不可用")

    client = _get_supabase_admin_client()
    records = _fetch_all_tracking_records(client, "id,code,recommend_date")
    if not records:
        return {
            "rows_total": 0,
            "rows_updated": 0,
            "rows_skipped": 0,
            "codes_total": 0,
            "codes_no_data": 0,
            "latest_trade_date": "",
        }

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end_date = today.strftime("%Y%m%d")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        code_digits = "".join(ch for ch in str(row.get("code", "")) if ch.isdigit())
        if not code_digits:
            continue
        code6 = code_digits[-6:].zfill(6)
        grouped.setdefault(code6, []).append(row)

    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_trade_date_global = ""

    for code6, rows in grouped.items():
        rec_dates = [_recommend_date_to_yyyymmdd(r.get("recommend_date")) for r in rows]
        rec_dates = [d for d in rec_dates if d]
        if not rec_dates:
            continue
        start_date = min(rec_dates)
        ts_code = _to_ts_code_recommendation(code6)

        try:
            df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception as e:
            print(f"[supabase_recommendation] tushare daily failed {ts_code}: {e}")
            codes_no_data += 1
            continue

        if df is None or df.empty:
            codes_no_data += 1
            continue

        work = df.copy()
        if "trade_date" not in work.columns or "close" not in work.columns:
            codes_no_data += 1
            continue
        work["trade_date"] = work["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
        work["close"] = pd.to_numeric(work["close"], errors="coerce")
        work = work.dropna(subset=["trade_date", "close"])
        work = work[work["close"] > 0]
        if work.empty:
            codes_no_data += 1
            continue

        close_map = {str(td): float(px) for td, px in zip(work["trade_date"].tolist(), work["close"].tolist())}
        trade_dates = sorted(close_map.keys())
        current_trade_date = trade_dates[-1]
        current_close = float(close_map[current_trade_date])
        if not latest_trade_date_global or current_trade_date > latest_trade_date_global:
            latest_trade_date_global = current_trade_date

        for row in rows:
            rec_date = _recommend_date_to_yyyymmdd(row.get("recommend_date"))
            pick_date = _pick_close_on_or_before(trade_dates, rec_date)
            if not pick_date:
                continue
            initial_close = float(close_map[pick_date])
            if initial_close <= 0 or current_close <= 0:
                continue
            change_pct = round((current_close - initial_close) / initial_close * 100.0, 2)
            row_id = row.get("id")
            updates.append(
                {
                    "id": row_id,
                    "code": int(code6),
                    "recommend_date": int(rec_date) if rec_date.isdigit() else None,
                    "initial_price": round(initial_close, 4),
                    "current_price": round(current_close, 4),
                    "change_pct": change_pct,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )

    written = _upsert_tracking_updates(client, updates)

    return {
        "rows_total": len(records),
        "rows_updated": written,
        "rows_skipped": max(len(records) - written, 0),
        "codes_total": len(grouped),
        "codes_no_data": codes_no_data,
        "latest_trade_date": latest_trade_date_global,
    }


# ---------------------------------------------------------------------------
# US / HK 推荐表（独立表，code 为字符串如 AAPL.US / 00700.HK）
# ---------------------------------------------------------------------------

_MARKET_TABLE_MAP: dict[str, str] = {
    "us": TABLE_RECOMMENDATION_TRACKING_US,
    "hk": TABLE_RECOMMENDATION_TRACKING_HK,
}


def _resolve_global_table(market: str) -> str:
    table = _MARKET_TABLE_MAP.get(market.lower())
    if not table:
        raise ValueError(f"unsupported market: {market}, must be 'us' or 'hk'")
    return table


def _fetch_records_from_table(
    client,
    table: str,
    select_expr: str,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    start = 0
    page = max(min(int(page_size), 1000), 1)
    while True:
        resp = (
            client.table(table)
            .select(select_expr)
            .order("recommend_date", desc=False)
            .order("id", desc=False)
            .range(start, start + page - 1)
            .execute()
        )
        batch = resp.data or []
        records.extend(batch)
        if len(batch) < page:
            return records
        start += page


def _upsert_to_table(
    client,
    table: str,
    updates: list[dict[str, Any]],
    batch_size: int = 500,
) -> int:
    written = 0
    clean = [r for r in updates if r.get("code") and r.get("recommend_date")]
    for chunk in _chunked(clean, max(min(int(batch_size), 1000), 1)):
        client.table(table).upsert(chunk, on_conflict="code,recommend_date").execute()
        written += len(chunk)
    return written


def upsert_global_recommendations(
    recommend_date: int,
    candidates: list[dict[str, Any]],
    market: str,
) -> bool:
    table = _resolve_global_table(market)
    if not is_supabase_configured() or not candidates:
        return False
    try:
        client = _get_supabase_admin_client()
        payload = []
        for c in candidates:
            code = str(c.get("code") or c.get("symbol") or "").strip()
            if not code:
                continue
            price = _extract_price(c)
            score_val = _extract_score(c)
            payload.append(
                {
                    "code": code,
                    "name": str(c.get("name", "")).strip(),
                    "recommend_reason": str(c.get("tag") or c.get("recommend_reason") or "").strip(),
                    "recommend_date": recommend_date,
                    "initial_price": price,
                    "current_price": price,
                    "change_pct": 0.0,
                    "funnel_score": score_val,
                    "is_ai_recommended": False,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        if payload:
            client.table(table).upsert(
                payload,
                on_conflict="code,recommend_date",
            ).execute()
        return True
    except Exception as e:
        print(f"[supabase_recommendation] upsert_global({market}) failed: {e}")
        return False


def _extract_price(c: dict[str, Any]) -> float:
    for key in ("initial_price", "latest_close", "current_price", "close"):
        raw = c.get(key)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


def _extract_score(c: dict[str, Any]) -> float | None:
    for sk in ("funnel_score", "score", "priority_score"):
        raw = c.get(sk)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def refresh_global_tracking_prices(market: str) -> dict[str, Any]:
    table = _resolve_global_table(market)
    if not is_supabase_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")

    client = _get_supabase_admin_client()
    records = _fetch_records_from_table(client, table, "id,code,recommend_date")
    empty = {
        "rows_total": 0,
        "rows_updated": 0,
        "rows_skipped": 0,
        "codes_total": 0,
        "codes_no_data": 0,
        "latest_trade_date": "",
    }
    if not records:
        return empty

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        code = str(row.get("code") or "").strip()
        if code:
            grouped.setdefault(code, []).append(row)

    symbols = sorted(grouped.keys())
    batch_size = max(min(int(os.getenv("RECOMMENDATION_TICKFLOW_BATCH_SIZE", "80")), 200), 1)
    quotes, hist_map = _fetch_tickflow_tracking_market_data(api_key, symbols, batch_size)

    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_td = ""
    now_iso = datetime.now(UTC).isoformat()

    for code, rows in grouped.items():
        quote = quotes.get(code) or {}
        cur = _resolve_tickflow_quote_price(quote)
        qtd = _quote_trade_date_yyyymmdd(quote)
        if qtd and qtd > latest_td:
            latest_td = qtd
        cmap = _close_map_from_tickflow_hist(hist_map.get(code))
        tdates = sorted(cmap)
        if cur <= 0 and tdates:
            cur = float(cmap[tdates[-1]])
        if tdates and tdates[-1] > latest_td:
            latest_td = tdates[-1]
        if cur <= 0 or not tdates:
            codes_no_data += 1
            continue
        for row in rows:
            rd = _recommend_date_to_yyyymmdd(row.get("recommend_date"))
            pd_ = _pick_close_on_or_before(tdates, rd)
            init = float(cmap.get(pd_, 0.0)) if pd_ else 0.0
            if init <= 0:
                continue
            updates.append(
                {
                    "id": row.get("id"),
                    "code": code,
                    "recommend_date": int(rd) if rd.isdigit() else None,
                    "initial_price": round(init, 4),
                    "current_price": round(cur, 4),
                    "change_pct": round((cur - init) / init * 100.0, 2),
                    "updated_at": now_iso,
                }
            )

    written = _upsert_to_table(client, table, updates)
    return {
        "rows_total": len(records),
        "rows_updated": written,
        "rows_skipped": max(len(records) - written, 0),
        "codes_total": len(grouped),
        "codes_no_data": codes_no_data,
        "latest_trade_date": latest_td,
    }


def _latest_market_records(records: list[dict[str, Any]], max_dates: int) -> list[dict[str, Any]]:
    limit = max(int(max_dates), 1)
    dates = sorted(
        {d for d in (_recommend_date_to_yyyymmdd(row.get("recommend_date")) for row in records) if d},
        reverse=True,
    )[:limit]
    allowed = set(dates)
    return [row for row in records if _recommend_date_to_yyyymmdd(row.get("recommend_date")) in allowed]


def _build_us_performance_update(
    row: dict[str, Any],
    code: str,
    ohlc: dict[str, dict[str, float]],
    now_iso: str,
) -> dict[str, Any] | None:
    trade_dates = sorted(ohlc)
    rd = _recommend_date_to_yyyymmdd(row.get("recommend_date"))
    entry_date = _pick_close_on_or_before(trade_dates, rd)
    entry = _safe_float(row.get("initial_price"), 0.0)
    if entry <= 0 and entry_date:
        entry = _safe_float(ohlc.get(entry_date, {}).get("close"), 0.0)
    if entry <= 0 or not entry_date:
        return None
    window = [(d, ohlc[d]) for d in trade_dates if d >= entry_date]
    if not window:
        return None
    high_date, high_row = max(window, key=lambda item: item[1]["high"])
    low_date, low_row = min(window, key=lambda item: item[1]["low"])
    latest_date, latest_row = window[-1]
    mfe_price = float(high_row["high"])
    mae_price = float(low_row["low"])
    current_price = float(latest_row["close"])
    return {
        "id": row.get("id"),
        "code": code,
        "recommend_date": int(rd) if rd.isdigit() else None,
        "initial_price": round(entry, 4),
        "current_price": round(current_price, 4),
        "change_pct": round((current_price / entry - 1.0) * 100.0, 2),
        "mfe_pct": round((mfe_price / entry - 1.0) * 100.0, 2),
        "mae_pct": round((mae_price / entry - 1.0) * 100.0, 2),
        "range_amp_pct": round((mfe_price / mae_price - 1.0) * 100.0, 2) if mae_price > 0 else 0.0,
        "mfe_price": round(mfe_price, 4),
        "mae_price": round(mae_price, 4),
        "mfe_date": int(high_date),
        "mae_date": int(low_date),
        "performance_days": len(window),
        "performance_updated_at": now_iso,
        "updated_at": now_iso,
    }


def refresh_us_tracking_performance(max_dates: int = 60, kline_count: int = 160) -> dict[str, Any]:
    if not is_supabase_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")

    client = _get_supabase_admin_client()
    table = TABLE_RECOMMENDATION_TRACKING_US
    records = _fetch_records_from_table(client, table, "id,code,recommend_date,initial_price")
    records = _latest_market_records(records, max_dates)
    if not records:
        return _empty_us_performance_summary()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        code = str(row.get("code") or "").strip()
        if code:
            grouped.setdefault(code, []).append(row)

    from integrations.tickflow_client import TickFlowClient

    tf_client = TickFlowClient(api_key=api_key)
    symbols = sorted(grouped)
    hist_map = tf_client.get_klines_batch(symbols, period="1d", count=max(int(kline_count), 1), adjust="none")
    now_iso = datetime.now(UTC).isoformat()
    updates, codes_no_data, latest_td = _build_us_performance_updates(grouped, hist_map, now_iso)
    written = _upsert_to_table(client, table, updates)
    return _us_performance_summary(records, grouped, written, codes_no_data, latest_td, updates)


def _build_us_performance_updates(
    grouped: dict[str, list[dict[str, Any]]],
    hist_map: dict[str, pd.DataFrame],
    now_iso: str,
) -> tuple[list[dict[str, Any]], int, str]:
    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_td = ""
    for code, rows in grouped.items():
        ohlc = _ohlc_map_from_tickflow_hist(hist_map.get(code))
        trade_dates = sorted(ohlc)
        if not trade_dates:
            codes_no_data += 1
            continue
        latest_td = max(latest_td, trade_dates[-1])
        for row in rows:
            update = _build_us_performance_update(row, code, ohlc, now_iso)
            if update is not None:
                updates.append(update)
    return updates, codes_no_data, latest_td


def _empty_us_performance_summary() -> dict[str, Any]:
    return {
        "rows_total": 0,
        "rows_updated": 0,
        "rows_skipped": 0,
        "codes_total": 0,
        "codes_no_data": 0,
        "latest_trade_date": "",
        "mfe_ge_5": 0,
        "mfe_ge_10": 0,
        "mae_le_neg5": 0,
    }


def _us_performance_summary(
    records: list[dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
    written: int,
    codes_no_data: int,
    latest_trade_date: str,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _empty_us_performance_summary()
    summary.update(
        {
            "rows_total": len(records),
            "rows_updated": written,
            "rows_skipped": max(len(records) - written, 0),
            "codes_total": len(grouped),
            "codes_no_data": codes_no_data,
            "latest_trade_date": latest_trade_date,
            "mfe_ge_5": sum(_safe_float(row.get("mfe_pct")) >= 5.0 for row in updates),
            "mfe_ge_10": sum(_safe_float(row.get("mfe_pct")) >= 10.0 for row in updates),
            "mae_le_neg5": sum(_safe_float(row.get("mae_pct")) <= -5.0 for row in updates),
        }
    )
    return summary


def refresh_tracking_prices_with_tickflow_realtime() -> dict[str, Any]:
    """
    使用 Tickflow 实时报价刷新形态复盘价格：
    - current_price: Tickflow /v1/quotes 的 last_price
    - initial_price: 推荐日（或之前最近交易日）Tickflow 不复权日线收盘价
    - change_pct: (current - initial) / initial * 100
    """
    if not is_supabase_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")

    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")

    from integrations.tickflow_client import normalize_cn_symbol

    client = _get_supabase_admin_client()
    records = _fetch_all_tracking_records(client, "id,code,recommend_date")
    if not records:
        return {
            "rows_total": 0,
            "rows_updated": 0,
            "rows_skipped": 0,
            "codes_total": 0,
            "codes_no_data": 0,
            "latest_trade_date": "",
        }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        code6 = _code6(row.get("code"))
        if code6:
            grouped.setdefault(code6, []).append(row)

    symbols = [normalize_cn_symbol(code6) for code6 in sorted(grouped)]
    symbols = [sym for sym in symbols if sym]
    batch_size = max(min(int(os.getenv("RECOMMENDATION_TICKFLOW_BATCH_SIZE", "80")), 200), 1)
    quotes, hist_map = _fetch_tickflow_tracking_market_data(api_key, symbols, batch_size)
    updates, codes_no_data, latest_trade_date_global = _build_tickflow_tracking_updates(
        grouped,
        quotes,
        hist_map,
        datetime.now(UTC).isoformat(),
    )

    written = _upsert_tracking_updates(client, updates)

    return {
        "rows_total": len(records),
        "rows_updated": written,
        "rows_skipped": max(len(records) - written, 0),
        "codes_total": len(grouped),
        "codes_no_data": codes_no_data,
        "latest_trade_date": latest_trade_date_global,
    }
