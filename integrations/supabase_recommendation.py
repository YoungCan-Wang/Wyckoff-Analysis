"""
Supabase 推荐跟踪数据存取模块
"""

from __future__ import annotations

import os
from bisect import bisect_right
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from core.constants import TABLE_RECOMMENDATION_TRACKING
from integrations.supabase_base import create_admin_client as _get_supabase_admin_client
from integrations.supabase_base import is_admin_configured as is_supabase_configured


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
            pass
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
    将每日选出的股票存入推荐跟踪表
    recommend_date: YYYYMMDD (int)
    """
    if not is_supabase_configured() or not symbols_info:
        return False
    try:
        client = _get_supabase_admin_client()

        # 预读已有记录（按 code 聚合），用于维护 recommend_count。
        # 规则：仅当 recommend_date 变化时才 +1，同日重跑不重复累计。
        existing_counts: dict[int, int] = {}
        existing_code_dates: dict[int, set[int]] = {}
        try:
            resp = client.table(TABLE_RECOMMENDATION_TRACKING).select("code,recommend_count,recommend_date").execute()
            for row in resp.data or []:
                try:
                    code_int = int(row.get("code"))
                except Exception:
                    continue
                try:
                    cnt = int(row.get("recommend_count") or 1)
                except Exception:
                    cnt = 1
                existing_counts[code_int] = max(existing_counts.get(code_int, 0), cnt)
                try:
                    d = int(row.get("recommend_date"))
                    existing_code_dates.setdefault(code_int, set()).add(d)
                except Exception:
                    pass
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
        resp = client.table(TABLE_RECOMMENDATION_TRACKING).select("code").execute()
        if not resp.data:
            print("[supabase_recommendation] sync_all_tracking_prices: 推荐表无记录，跳过")
            return 0

        unique_codes = sorted(list(set(int(r["code"]) for r in resp.data)))

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

        updated_count = 0
        for code_int in unique_codes:
            code_str = f"{code_int:06d}"
            new_current_price: float | None = None

            if price_map:
                raw_px = price_map.get(code_str)
                try:
                    parsed_px = float(raw_px) if raw_px is not None else 0.0
                except Exception:
                    parsed_px = 0.0
                if parsed_px > 0:
                    new_current_price = parsed_px

            if new_current_price is None:
                new_current_price = _price_from_history(code_str)
            if new_current_price is None:
                new_current_price = _price_from_spot(code_str)
            if new_current_price is None:
                continue

            # 该股票可能有多条推荐记录（不同日期），逐条更新价格与涨跌幅
            rec_resp = client.table(TABLE_RECOMMENDATION_TRACKING).select("*").eq("code", code_int).execute()
            for record in rec_resp.data:
                initial_price = float(record.get("initial_price") or 0.0)
                rec_date = _parse_recommend_date(record.get("recommend_date"))
                update_payload = {
                    "current_price": new_current_price,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                if initial_price > 0:
                    change_pct = (new_current_price - initial_price) / initial_price * 100.0
                    update_payload["change_pct"] = round(change_pct, 2)
                else:
                    backfill_price = _resolve_initial_price_from_history(code_str, rec_date) if rec_date else 0.0
                    if backfill_price <= 0:
                        backfill_price = new_current_price
                    update_payload["initial_price"] = backfill_price
                    update_payload["change_pct"] = (
                        round(
                            (new_current_price - backfill_price) / backfill_price * 100.0,
                            2,
                        )
                        if backfill_price > 0
                        else 0.0
                    )
                client.table(TABLE_RECOMMENDATION_TRACKING).update(update_payload).eq("id", record["id"]).execute()
                updated_count += 1

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
        resp = client.table(TABLE_RECOMMENDATION_TRACKING).select("*").execute()
        if not resp.data:
            return 0
        cache: dict[tuple[str, date], float] = {}
        updated = 0
        for record in resp.data:
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
    """加载推荐跟踪数据"""
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
    使用 Tushare（日线不复权）回填并刷新推荐跟踪价格：
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
    resp = client.table(TABLE_RECOMMENDATION_TRACKING).select("id,code,recommend_date").execute()
    records = resp.data or []
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

    if updates:
        for item in updates:
            row_id = item.pop("id", None)
            code_val = item.pop("code", None)
            rec_date_val = item.pop("recommend_date", None)
            q = client.table(TABLE_RECOMMENDATION_TRACKING).update(item)
            if row_id is not None:
                q = q.eq("id", row_id)
            elif code_val is not None and rec_date_val is not None:
                q = q.eq("code", code_val).eq("recommend_date", rec_date_val)
            else:
                continue
            q.execute()

    updated_keys = {
        f"{x.get('code', '')}:{x.get('recommend_date', '')}"
        for x in updates
        if x.get("code") is not None and x.get("recommend_date") is not None
    }
    return {
        "rows_total": len(records),
        "rows_updated": len(updated_keys),
        "rows_skipped": max(len(records) - len(updated_keys), 0),
        "codes_total": len(grouped),
        "codes_no_data": codes_no_data,
        "latest_trade_date": latest_trade_date_global,
    }
