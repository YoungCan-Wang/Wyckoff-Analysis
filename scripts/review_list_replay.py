"""
手动复盘 review_list：检查每只股票在漏斗中止步的层级与原因，并发送飞书。

输入：
- REVIEW_LIST / review_list: 股票代码列表，逗号/空白分隔
- FEISHU_WEBHOOK_URL: 飞书机器人 webhook
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import contextlib

from core.funnel_pipeline import TRIGGER_LABELS
from core.wyckoff_engine import FunnelConfig, _sorted_if_needed
from scripts.wyckoff_funnel import run_funnel_job
from utils.feishu import send_feishu_notification


def _is_main_or_chinext(code: str) -> bool:
    return str(code).startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301"))


def _build_layer2_context(
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
) -> dict:
    return {
        "bench_df_raw": bench_df,
        "rps_universe": list(df_map.keys()),
    }


def _explain_l1_fail(
    code: str,
    cfg: FunnelConfig,
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    df_map: dict[str, pd.DataFrame],
) -> str:
    name = str(name_map.get(code, ""))
    if not _is_main_or_chinext(code):
        return "非主板/创业板代码"
    if "ST" in name.upper():
        return "ST股票"
    if market_cap_map:
        cap = float(market_cap_map.get(code, 0.0) or 0.0)
        if cap < cfg.min_market_cap_yi:
            return f"市值不足: {cap:.2f}亿 < {cfg.min_market_cap_yi:.2f}亿"
    df = df_map.get(code)
    if df is None or df.empty:
        return "缺少日线数据"
    s = _sorted_if_needed(df)
    if "amount" in s.columns:
        avg_amt = pd.to_numeric(s["amount"], errors="coerce").tail(cfg.amount_avg_window).mean()
        if pd.notna(avg_amt) and float(avg_amt) < cfg.min_avg_amount_wan * 10000:
            return f"成交额不足: {float(avg_amt) / 10000.0:.1f}万 < {cfg.min_avg_amount_wan:.1f}万"
    return "未通过L1（综合条件不满足）"


def _explain_l2_fail(
    code: str,
    cfg: FunnelConfig,
    df_map: dict[str, pd.DataFrame],
    ctx: dict,
) -> str:
    """复用引擎的 layer2_strength_detailed 做单票验证，返回通道归因。"""
    from core.wyckoff_engine import layer2_strength_detailed

    df = df_map.get(code)
    if df is None or len(df) < cfg.ma_long:
        return f"历史长度不足: < MA{cfg.ma_long}"

    bench_df_raw = ctx.get("bench_df_raw")
    rps_universe = ctx.get("rps_universe", [code])

    # 用引擎做单票 L2 判断
    passed, channel_map, _ = layer2_strength_detailed(
        [code],
        df_map,
        bench_df_raw,
        cfg,
        rps_universe=rps_universe,
    )
    if passed:
        channel = channel_map.get(code, "未知通道")
        return f"引擎判定通过L2[{channel}]，应在L3或后续层被淘汰"

    return "六通道均未通过（主升/潜伏/吸筹/地量蓄势/暗中护盘/点火破局）"


def _build_hit_map(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, list[str]]:
    hit_map: dict[str, list[str]] = {}
    for trig, label in TRIGGER_LABELS.items():
        for code, _ in triggers.get(trig, []):
            hit_map.setdefault(str(code), [])
            if label not in hit_map[str(code)]:
                hit_map[str(code)].append(label)
    return hit_map


def _latest_pct_change(df: pd.DataFrame) -> float | None:
    s = _sorted_if_needed(df)
    close = pd.to_numeric(s.get("close"), errors="coerce").dropna()
    if len(close) >= 2:
        prev_close = float(close.iloc[-2])
        if prev_close > 0:
            return (float(close.iloc[-1]) / prev_close - 1.0) * 100.0

    pct = pd.to_numeric(s.get("pct_chg"), errors="coerce")
    if pct.empty or pd.isna(pct.iloc[-1]):
        return None
    return float(pct.iloc[-1])


def _find_big_gainers(
    df_map: dict[str, pd.DataFrame],
    name_map: dict[str, str],
    threshold: float = 8.0,
) -> list[str]:
    """找出当日涨幅 >= threshold% 的主板+创业板非ST股票。"""
    codes: list[str] = []
    for code, df in df_map.items():
        if not _is_main_or_chinext(code):
            continue
        if "ST" in str(name_map.get(code, "")).upper():
            continue
        if df is None or df.empty:
            continue
        pct = _latest_pct_change(df)
        if pct is not None and pct >= threshold:
            codes.append(code)
    codes.sort()
    return codes


def _find_big_gainers_from_spot(
    spot_map: dict[str, dict],
    name_map: dict[str, str],
    threshold: float = 8.0,
) -> tuple[list[str], int]:
    """从全市场实时快照中找出涨幅 >= threshold% 的主板+创业板非ST股票。"""
    codes: list[str] = []
    usable = 0
    for code, snap in (spot_map or {}).items():
        code = str(code).strip()
        if code not in name_map:
            continue
        if not _is_main_or_chinext(code):
            continue
        if "ST" in str(name_map.get(code, "")).upper():
            continue
        try:
            pct = snap.get("pct_chg") if isinstance(snap, dict) else None
            if pct is None:
                continue
            usable += 1
            if float(pct) >= threshold:
                codes.append(code)
        except Exception:
            continue
    codes.sort()
    return codes, usable


def _review_spot_min_coverage() -> float:
    try:
        value = float(os.getenv("REVIEW_SPOT_MIN_COVERAGE", "0.8"))
    except ValueError:
        value = 0.8
    return min(max(value, 0.0), 1.0)


def _load_today_review_codes(all_codes: list[str], name_map_today: dict[str, str], today_window) -> list[str]:
    review_codes: list[str] = []
    spot_usable = 0
    try:
        from integrations.data_source import _load_spot_snapshot_map

        spot_map = _load_spot_snapshot_map(force_refresh=True)
        review_codes, spot_usable = _find_big_gainers_from_spot(
            spot_map=spot_map,
            name_map=name_map_today,
            threshold=8.0,
        )
        print(
            "[review] 实时快照加载完成: "
            f"symbols={len(spot_map or {})}, usable_pct={spot_usable}, "
            f"big_gainers={len(review_codes)}"
        )
    except Exception as e:
        print(f"[review] 实时快照加载失败，准备回退日线拉取: {e}")

    spot_min_coverage = _review_spot_min_coverage()
    spot_coverage = spot_usable / max(len(all_codes), 1)
    if spot_usable > 0 and spot_coverage >= spot_min_coverage:
        return review_codes

    from tools.data_fetcher import fetch_all_ohlcv

    if spot_usable <= 0:
        print("[review] 实时快照不可用，回退到两日 OHLCV 拉取")
    else:
        print(
            "[review] 实时快照覆盖不足，回退到两日 OHLCV 拉取: "
            f"coverage={spot_coverage:.1%}, min={spot_min_coverage:.1%}"
        )
    today_df_map, today_fetch_stats = fetch_all_ohlcv(
        symbols=all_codes,
        window=today_window,
        enforce_target_trade_date=True,
        direct_source=True,
    )
    print(
        "[review] 今日数据拉取完成: "
        f"ok={today_fetch_stats.get('fetch_ok', len(today_df_map))}, "
        f"fail={today_fetch_stats.get('fetch_fail', 0)}, "
        f"target_trade_date={today_window.end_trade_date}"
    )
    return _find_big_gainers(today_df_map, name_map_today, threshold=8.0)


def _blocked_exit_signal_map(exit_signals: dict[str, dict] | None) -> dict[str, dict]:
    blocked: dict[str, dict] = {}
    for code, raw in (exit_signals or {}).items():
        signal = str((raw or {}).get("signal", "")).strip()
        if signal in {"stop_loss", "distribution_warning"}:
            blocked[str(code)] = dict(raw or {})
    return blocked


def _explain_risk_reject(
    code: str,
    blocked_exit_map: dict[str, dict],
    hit_map: dict[str, list[str]],
) -> str:
    exit_sig = blocked_exit_map.get(code, {}) or {}
    signal = str(exit_sig.get("signal", "")).strip()
    signal_label = {
        "stop_loss": "触发结构止损",
        "distribution_warning": "触发Distribution派发警告",
    }.get(signal, "触发风控硬剔除")
    reason = str(exit_sig.get("reason", "")).strip()
    price = exit_sig.get("price")
    trigger_labels = "、".join(hit_map.get(code, []))

    parts = [signal_label]
    if price is not None:
        with contextlib.suppress(Exception):
            parts.append(f"参考价={float(price):.2f}")
    if trigger_labels:
        parts.append(f"L4命中={trigger_labels}")
    if reason:
        parts.append(reason)
    return " | ".join(parts)


def _normalize_code6(raw: object) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _normalize_recommend_date(raw: object) -> str:
    s = str(raw or "").strip()
    if not s:
        return "日期未知"
    try:
        d = pd.to_datetime(s, format="%Y%m%d") if len(s) == 8 and s.isdigit() else pd.to_datetime(s)
        if pd.isna(d):
            return s
        return d.strftime("%Y-%m-%d")
    except Exception:
        return s


def _load_recommendation_lookup(codes: list[str]) -> tuple[dict[str, list[dict]], str]:
    clean_codes = sorted({int(c) for c in (_normalize_code6(code) for code in codes) if c})
    if not clean_codes:
        return {}, ""
    try:
        from core.constants import TABLE_RECOMMENDATION_TRACKING
        from integrations.supabase_base import create_admin_client, is_admin_configured

        if not is_admin_configured():
            return {}, "推荐表未配置，无法确认是否被推荐过"

        client = create_admin_client()
        rows: list[dict] = []
        chunk_size = 200
        for start in range(0, len(clean_codes), chunk_size):
            chunk = clean_codes[start : start + chunk_size]
            resp = (
                client.table(TABLE_RECOMMENDATION_TRACKING)
                .select("code,name,recommend_date,recommend_count,is_ai_recommended")
                .in_("code", chunk)
                .order("recommend_date", desc=True)
                .limit(10000)
                .execute()
            )
            rows.extend([row for row in (resp.data or []) if isinstance(row, dict)])

        lookup: dict[str, list[dict]] = {}
        for row in rows:
            code = _normalize_code6(row.get("code"))
            if code:
                lookup.setdefault(code, []).append(row)
        return lookup, ""
    except Exception as e:
        print(f"[review] 推荐表读取失败: {e}")
        return {}, "推荐表读取失败，无法确认是否被推荐过"


def _format_recommendation_history(code: str, lookup: dict[str, list[dict]], load_error: str = "") -> str:
    if load_error:
        return f"推荐记录: {load_error}"
    records = lookup.get(_normalize_code6(code), [])
    if not records:
        return "推荐记录: 此股没被推荐过"

    dates = sorted({_normalize_recommend_date(row.get("recommend_date")) for row in records}, reverse=True)
    parsed_counts = []
    for row in records:
        with contextlib.suppress(Exception):
            parsed_counts.append(int(row.get("recommend_count") or 0))
    count = max([len(dates), *parsed_counts]) if parsed_counts else len(dates)
    return f"推荐记录: {'、'.join(dates)} 被推荐过；累计推荐{count}次"


def _short_code_list(rows: list[dict[str, str]], limit: int = 8) -> str:
    shown = [f"{row['code']}{row['name']}" for row in rows[:limit]]
    if len(rows) > limit:
        shown.append(f"等{len(rows)}只")
    return "、".join(shown) if shown else "无"


def _build_focus_lines(rows: list[dict[str, str]], today: date, previous_trade_date: date) -> list[str]:
    total = max(len(rows), 1)
    stage_rows: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        stage_rows.setdefault(row["stage"], []).append(row)

    l2_rows = stage_rows.get("L2淘汰", [])
    risk_rows = stage_rows.get("风控淘汰[触发结构止损或派发]", [])
    l4_miss_rows = stage_rows.get("L4未命中", [])
    l3_rows = stage_rows.get("L3淘汰", [])
    l1_rows = stage_rows.get("L1淘汰", [])
    l4_hit_rows = stage_rows.get("L4命中", [])

    lines = ["**重点归因**"]
    if (today - previous_trade_date).days > 1:
        lines.append(
            f"- **日期间隔**：{previous_trade_date} 收盘后到 {today} 之间跨 {((today - previous_trade_date).days)} 个自然日，节假日/周末消息驱动的跳空异动，本来就很难由前一交易日日线结构提前捕获。"
        )
    if l2_rows:
        pct = len(l2_rows) / total * 100.0
        lines.append(
            f"- **L2 是主因**：{len(l2_rows)} / {total}（{pct:.1f}%）前一日没有进入六通道。这里不宜直接放宽实盘漏斗，否则会把大量无结构、纯事件驱动的一日异动混入候选池。"
        )
    if risk_rows:
        lines.append(
            f"- **风控冲突优先复盘**：{_short_code_list(risk_rows)}。这些票已进入后续层，但被结构止损/派发硬剔除，最适合单独检查止损是否对节后修复过敏。"
        )
    if l4_miss_rows:
        lines.append(
            f"- **L4 扳机漏网**：{_short_code_list(l4_miss_rows)}。这些票已过 L2/L3，只差 Spring/LPS/EVR/SOS 微观触发，适合测试“爆发前夜压缩/试盘”类观察信号。"
        )
    if l3_rows:
        lines.append(
            f"- **板块层漏网**：{_short_code_list(l3_rows)}。若同一题材后续反复出现，可考虑给极强个股更多行业绕行权。"
        )
    if l1_rows:
        lines.append(
            f"- **基础过滤漏网**：{_short_code_list(l1_rows)}。主要是成交额/基础流动性，不建议为涨停复盘反向放宽。"
        )
    if l4_hit_rows:
        lines.append(
            f"- **已被漏斗捕获**：{_short_code_list(l4_hit_rows)}。这类不是形态漏检，后续应核对是否被 AI 配额或风控环节挡住。"
        )
    return lines


def _build_report_lines(
    rows: list[dict[str, str]],
    stage_counter: Counter[str],
    today: date,
    previous_trade_date: date,
    end_trade_date: str,
) -> list[str]:
    summary = " | ".join([f"{k}{v}" for k, v in stage_counter.items()]) or "无"
    recommendation_notes = [str(row.get("recommendation", "")).strip() for row in rows]
    recommendation_hits = sum(1 for note in recommendation_notes if "累计推荐" in note)
    recommendation_unknown = sum(1 for note in recommendation_notes if "无法确认" in note)
    lines = [
        f"**今日**: {today}",
        f"**前一日漏斗**: {end_trade_date}",
        f"**今日涨幅 ≥ 8% 股票数**: {len(rows)}",
        f"**结果汇总**: {summary}",
        f"**推荐表交叉检查**: 命中{recommendation_hits}只 | 未推荐{len(rows) - recommendation_hits - recommendation_unknown}只"
        + (f" | 无法确认{recommendation_unknown}只" if recommendation_unknown else ""),
        "",
        *_build_focus_lines(rows, today=today, previous_trade_date=previous_trade_date),
        "",
        "**逐票复盘（在前一日漏斗中止步层级与原因）**",
        "",
    ]

    for row in rows:
        recommendation = str(row.get("recommendation", "")).strip()
        suffix = f" | {recommendation}" if recommendation else ""
        lines.append(f"• {row['code']} {row['name']} | {row['stage']} | {row['reason']}{suffix}")
    return lines


def main() -> int:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[review] FEISHU_WEBHOOK_URL 未配置")
        return 2

    # 1. 先获取今日涨幅 ≥ 8% 的股票（使用今日数据）
    print("[review] 获取今日涨幅 ≥ 8% 股票...")
    from datetime import timedelta

    from integrations.fetch_a_share_csv import _resolve_trading_window, get_stocks_by_board
    from utils.trading_clock import resolve_end_calendar_day

    end_calendar_day = resolve_end_calendar_day()
    today_window = _resolve_trading_window(end_calendar_day=end_calendar_day, trading_days=2)
    today = today_window.end_trade_date
    previous_window = _resolve_trading_window(end_calendar_day=today - timedelta(days=1), trading_days=1)
    previous_trade_date = previous_window.end_trade_date

    # 获取今日数据找涨停股
    print(f"[review] 今日: {today}, 前一交易日: {previous_trade_date}")
    stock_items = get_stocks_by_board("main_chinext")
    name_map_today = {
        str(item.get("code", "")).strip(): str(item.get("name", "")).strip()
        for item in stock_items
        if isinstance(item, dict) and str(item.get("code", "")).strip()
    }
    all_codes = sorted(name_map_today.keys())
    review_codes = _load_today_review_codes(all_codes, name_map_today, today_window)

    if not review_codes:
        print("[review] 今日无涨幅 ≥ 8% 的股票，跳过")
        send_feishu_notification(webhook, "🔍 涨停复盘", f"交易日 {today}：今日无涨幅 ≥ 8% 的主板/创业板股票")
        return 0
    print(f"[review] 今日发现涨幅 ≥ 8% 股票 {len(review_codes)} 只: {', '.join(review_codes)}")

    # 2. 回放前一日漏斗（使用前一日数据）
    print(f"[review] 回放前一交易日 ({previous_trade_date}) 漏斗...")
    original_end_day = os.getenv("END_CALENDAR_DAY", "")
    os.environ["END_CALENDAR_DAY"] = previous_trade_date.strftime("%Y-%m-%d")

    try:
        triggers, metrics = run_funnel_job(include_debug_context=True, direct_source=True)
    finally:
        if original_end_day:
            os.environ["END_CALENDAR_DAY"] = original_end_day
        else:
            os.environ.pop("END_CALENDAR_DAY", None)

    debug = metrics.get("_debug", {}) or {}
    if not debug:
        print("[review] 缺少调试上下文，无法复盘")
        return 3

    cfg: FunnelConfig = debug.get("cfg")
    all_symbols = [str(x) for x in (debug.get("all_symbols", []) or [])]
    name_map = debug.get("name_map", {}) or {}
    market_cap_map = debug.get("market_cap_map", {}) or {}
    sector_map = debug.get("sector_map", {}) or {}
    bench_df = debug.get("bench_df")
    df_map = debug.get("all_df_map", {}) or {}
    l1_symbols = [str(x) for x in (debug.get("layer1_symbols", []) or [])]
    l2_symbols = [str(x) for x in (debug.get("layer2_symbols", []) or [])]
    l3_symbols = [str(x) for x in (debug.get("layer3_symbols_raw", []) or [])]
    end_trade_date = str(debug.get("end_trade_date", "未知"))

    l1_set = set(l1_symbols)
    l2_set = set(l2_symbols)
    l3_set = set(l3_symbols)
    all_symbol_set = set(all_symbols)

    l2_ctx = _build_layer2_context(df_map=df_map, bench_df=bench_df)
    hit_map = _build_hit_map(triggers)
    blocked_exit_map = _blocked_exit_signal_map(metrics.get("exit_signals", {}) or {})
    recommendation_lookup, recommendation_error = _load_recommendation_lookup(review_codes)

    rows: list[dict[str, str]] = []
    stage_counter: Counter[str] = Counter()

    for code in review_codes:
        name = str(name_map.get(code, code)).strip() or code
        stage = ""
        reason = ""

        if code not in all_symbol_set:
            stage = "池外"
            reason = "不在当日主板+创业板去ST股票池"
        elif code not in df_map:
            stage = "数据失败"
            reason = "日线拉取失败/超时"
        elif code not in l1_set:
            stage = "L1淘汰"
            reason = _explain_l1_fail(
                code=code,
                cfg=cfg,
                name_map=name_map,
                market_cap_map=market_cap_map,
                df_map=df_map,
            )
        elif code not in l2_set:
            stage = "L2淘汰"
            reason = _explain_l2_fail(
                code=code,
                cfg=cfg,
                df_map=df_map,
                ctx=l2_ctx,
            )
        elif code not in l3_set:
            stage = "L3淘汰"
            sector = sector_map.get(code, "未知行业")
            reason = f"行业共振层未通过（{sector}）"
        elif code in blocked_exit_map:
            stage = "风控淘汰[触发结构止损或派发]"
            reason = _explain_risk_reject(
                code=code,
                blocked_exit_map=blocked_exit_map,
                hit_map=hit_map,
            )
        elif code in hit_map:
            stage = "L4命中"
            reason = "、".join(hit_map.get(code, []))
        else:
            stage = "L4未命中"
            reason = "未触发 Spring（弹簧/假跌破）/LPS（最后支撑点）/EVR（放量不跌）/SOS（强势信号）"

        stage_counter[stage] += 1
        rows.append(
            {
                "code": code,
                "name": name,
                "stage": stage,
                "reason": reason,
                "recommendation": _format_recommendation_history(code, recommendation_lookup, recommendation_error),
            }
        )

    lines = _build_report_lines(
        rows=rows,
        stage_counter=stage_counter,
        today=today,
        previous_trade_date=previous_trade_date,
        end_trade_date=end_trade_date,
    )
    title = "🔍 涨停复盘：今日涨停为何未在前一日漏斗捕获"
    content = "\n".join(lines)
    ok = send_feishu_notification(webhook, title, content)
    print(f"[review] feishu_sent={ok}")

    if not ok:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
