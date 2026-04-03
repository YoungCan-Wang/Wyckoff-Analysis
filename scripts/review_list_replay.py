# -*- coding: utf-8 -*-
"""
手动复盘 review_list：检查每只股票在漏斗中止步的层级与原因，并发送飞书。

输入：
- REVIEW_LIST / review_list: 股票代码列表，逗号/空白分隔
- FEISHU_WEBHOOK_URL: 飞书机器人 webhook
"""

from __future__ import annotations

from collections import Counter
import os
import re
import sys

import pandas as pd


# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.wyckoff_engine import FunnelConfig, _latest_trade_date, _sorted_if_needed
from core.funnel_pipeline import TRIGGER_LABELS, run_funnel_job
from utils.feishu import send_feishu_notification


def _is_main_or_chinext(code: str) -> bool:
    return str(code).startswith(
        ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")
    )


def _parse_review_list(raw: str) -> list[str]:
    tokens = re.split(r"[,，;；\s]+", str(raw or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        code = tok.strip()
        if not code:
            continue
        if re.fullmatch(r"\d{1,6}", code):
            code = code.zfill(6)
        if code and code not in seen:
            out.append(code)
            seen.add(code)
    return out


def _close_return_pct(close_series: pd.Series, lookback: int) -> float | None:
    s = pd.to_numeric(close_series, errors="coerce").dropna()
    lb = max(int(lookback), 1)
    if len(s) <= lb:
        return None
    start = float(s.iloc[-lb - 1])
    end = float(s.iloc[-1])
    if start == 0:
        return None
    return (end - start) / start * 100.0


def _cum_return_pct_from_series(pct_series: pd.Series) -> float | None:
    s = pd.to_numeric(pct_series, errors="coerce").dropna()
    if s.empty:
        return None
    return float(((s / 100.0 + 1.0).prod() - 1.0) * 100.0)


def _calc_rs(
    stock_df: pd.DataFrame,
    bench_sorted_df: pd.DataFrame,
    cfg: FunnelConfig,
) -> tuple[float | None, float | None]:
    stock_p = stock_df[["date", "pct_chg"]].copy()
    bench_p = bench_sorted_df[["date", "pct_chg"]].copy()
    merged = stock_p.merge(bench_p, on="date", how="inner", suffixes=("_s", "_b"))
    if merged.empty:
        return (None, None)
    w_long = max(int(cfg.rs_window_long), 1)
    w_short = max(int(cfg.rs_window_short), 1)
    if len(merged) < max(w_long, w_short):
        return (None, None)
    s_long = _cum_return_pct_from_series(merged["pct_chg_s"].tail(w_long))
    b_long = _cum_return_pct_from_series(merged["pct_chg_b"].tail(w_long))
    s_short = _cum_return_pct_from_series(merged["pct_chg_s"].tail(w_short))
    b_short = _cum_return_pct_from_series(merged["pct_chg_b"].tail(w_short))
    if s_long is None or b_long is None or s_short is None or b_short is None:
        return (None, None)
    return (s_long - b_long, s_short - b_short)


def _build_layer2_context(
    l1_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
) -> dict:
    bench_dropping = False
    bench_sorted = None
    bench_latest_date = None
    if bench_df is not None and not bench_df.empty:
        bench_sorted = _sorted_if_needed(bench_df)
        bench_latest_date = _latest_trade_date(bench_sorted)
        if len(bench_sorted) >= cfg.bench_drop_days:
            recent_bench = bench_sorted.tail(cfg.bench_drop_days)
            bench_cum = (recent_bench["pct_chg"].dropna() / 100.0 + 1).prod() - 1
            bench_dropping = bench_cum * 100 <= cfg.bench_drop_threshold

    rps_fast_map: dict[str, float] = {}
    rps_slow_map: dict[str, float] = {}
    rps_filter_active = False
    if cfg.enable_rps_filter and l1_symbols:
        rows: list[tuple[str, float, float]] = []
        for sym in l1_symbols:
            df = df_map.get(sym)
            if df is None or df.empty:
                continue
            s = _sorted_if_needed(df)
            close = pd.to_numeric(s.get("close"), errors="coerce")
            ret_fast = _close_return_pct(close, cfg.rps_window_fast)
            ret_slow = _close_return_pct(close, cfg.rps_window_slow)
            if ret_fast is None or ret_slow is None:
                continue
            rows.append((sym, ret_fast, ret_slow))
        if rows:
            rps_df = pd.DataFrame(rows, columns=["sym", "ret_fast", "ret_slow"])
            rps_df["rps_fast"] = rps_df["ret_fast"].rank(
                pct=True,
                ascending=True,
                method="average",
            ) * 100.0
            rps_df["rps_slow"] = rps_df["ret_slow"].rank(
                pct=True,
                ascending=True,
                method="average",
            ) * 100.0
            rps_fast_map = rps_df.set_index("sym")["rps_fast"].astype(float).to_dict()
            rps_slow_map = rps_df.set_index("sym")["rps_slow"].astype(float).to_dict()
            rps_filter_active = True

    return {
        "bench_sorted": bench_sorted,
        "bench_latest_date": bench_latest_date,
        "bench_dropping": bench_dropping,
        "rps_fast_map": rps_fast_map,
        "rps_slow_map": rps_slow_map,
        "rps_filter_active": rps_filter_active,
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
            return (
                f"成交额不足: {float(avg_amt)/10000.0:.1f}万"
                f" < {cfg.min_avg_amount_wan:.1f}万"
            )
    return "未通过L1（综合条件不满足）"


def _explain_l2_fail(
    code: str,
    cfg: FunnelConfig,
    df_map: dict[str, pd.DataFrame],
    ctx: dict,
) -> str:
    df = df_map.get(code)
    if df is None or len(df) < cfg.ma_long:
        return f"历史长度不足: < MA{cfg.ma_long}"
    s = _sorted_if_needed(df)

    bench_sorted = ctx.get("bench_sorted")
    bench_latest_date = ctx.get("bench_latest_date")
    bench_dropping = bool(ctx.get("bench_dropping"))
    rps_fast_map = ctx.get("rps_fast_map", {}) or {}
    rps_slow_map = ctx.get("rps_slow_map", {}) or {}
    rps_filter_active = bool(ctx.get("rps_filter_active"))

    if (
        cfg.require_bench_latest_alignment
        and bench_latest_date is not None
        and _latest_trade_date(s) != bench_latest_date
    ):
        return "与大盘最新交易日不对齐"

    close = pd.to_numeric(s.get("close"), errors="coerce")
    ma_short = close.rolling(cfg.ma_short).mean()
    ma_long = close.rolling(cfg.ma_long).mean()
    last_ma_short = ma_short.iloc[-1]
    last_ma_long = ma_long.iloc[-1]
    last_close = close.iloc[-1]

    bullish_alignment = (
        pd.notna(last_ma_short)
        and pd.notna(last_ma_long)
        and float(last_ma_short) > float(last_ma_long)
    )
    holding_ma20 = False
    if bench_dropping:
        ma_hold = close.rolling(cfg.ma_hold).mean()
        last_ma_hold = ma_hold.iloc[-1]
        if pd.notna(last_ma_hold) and pd.notna(last_close):
            holding_ma20 = float(last_close) >= float(last_ma_hold)

    momentum_rs_ok = True
    ambush_rs_ok = True
    rs_long = None
    rs_short = None
    if cfg.enable_rs_filter and bench_sorted is not None and not bench_sorted.empty:
        rs_long, rs_short = _calc_rs(s, bench_sorted, cfg)
        if rs_long is None or rs_short is None:
            momentum_rs_ok = False
            ambush_rs_ok = False
        else:
            momentum_rs_ok = (
                rs_long >= cfg.rs_min_long and rs_short >= cfg.rs_min_short
            )
            ambush_rs_ok = (
                rs_long >= cfg.ambush_rs_long_min and rs_short >= cfg.ambush_rs_short_min
            )

    rps_fast = rps_fast_map.get(code)
    rps_slow = rps_slow_map.get(code)
    momentum_rps_ok = True
    ambush_rps_ok = True
    if cfg.enable_rps_filter and rps_filter_active:
        momentum_rps_ok = (
            rps_fast is not None
            and rps_slow is not None
            and float(rps_fast) >= cfg.rps_fast_min
            and float(rps_slow) >= cfg.rps_slow_min
        )
        ambush_rps_ok = (
            rps_fast is not None
            and rps_slow is not None
            and float(rps_fast) <= cfg.ambush_rps_fast_max
            and float(rps_slow) >= cfg.ambush_rps_slow_min
        )

    momentum_ok = (bullish_alignment or holding_ma20) and momentum_rs_ok and momentum_rps_ok

    ambush_shape_ok = False
    if (
        cfg.enable_ambush_channel
        and pd.notna(last_ma_long)
        and float(last_ma_long) > 0
        and pd.notna(last_close)
    ):
        bias_200 = (float(last_close) - float(last_ma_long)) / float(last_ma_long)
        ret20 = _close_return_pct(close, 20)
        ambush_shape_ok = (
            abs(bias_200) <= cfg.ambush_bias_200_abs_max
            and ret20 is not None
            and ret20 <= cfg.ambush_ret20_max
        )
    ambush_ok = (
        cfg.enable_ambush_channel
        and ambush_shape_ok
        and ambush_rs_ok
        and ambush_rps_ok
    )

    accum_ok = False
    accum_reasons: list[str] = []
    if not cfg.enable_accumulation_channel:
        accum_reasons.append("通道关闭")
    elif len(s) < max(cfg.accum_lookback_days, cfg.accum_vol_dry_ref_window):
        accum_reasons.append("历史长度不足")
    else:
        period_low = float(close.tail(max(int(cfg.accum_lookback_days), 2)).min())
        accum_low_ok = (
            period_low > 0
            and float(last_close) <= period_low * (1.0 + cfg.accum_price_from_low_max)
        )
        if not accum_low_ok:
            accum_reasons.append("未处于年内低位区")

        accum_range_ok = False
        if accum_low_ok:
            rw = max(int(cfg.accum_range_window), 5)
            zone = s.tail(rw)
            high = pd.to_numeric(zone.get("high"), errors="coerce")
            low = pd.to_numeric(zone.get("low"), errors="coerce")
            if not high.dropna().empty and not low.dropna().empty:
                h_max = float(high.max())
                l_min = float(low.min())
                if l_min > 0:
                    range_pct = (h_max - l_min) / l_min * 100.0
                    accum_range_ok = range_pct <= cfg.accum_range_max_pct
        if accum_low_ok and not accum_range_ok:
            accum_reasons.append("横盘振幅过大")

        accum_vol_ok = False
        if accum_range_ok:
            vol = pd.to_numeric(s.get("volume"), errors="coerce")
            dw = max(int(cfg.accum_vol_dry_window), 2)
            rfw = max(int(cfg.accum_vol_dry_ref_window), dw + 1)
            recent_vol_mean = float(vol.tail(dw).mean()) if len(vol) >= dw else None
            ref_vol_mean = float(vol.tail(rfw).iloc[:-dw].mean()) if len(vol) >= rfw else None
            if (
                recent_vol_mean is not None
                and ref_vol_mean is not None
                and ref_vol_mean > 0
            ):
                accum_vol_ok = (recent_vol_mean / ref_vol_mean) < cfg.accum_vol_dry_ratio
        if accum_range_ok and not accum_vol_ok:
            accum_reasons.append("量能未萎缩")

        accum_ma_ok = False
        if accum_vol_ok:
            if (
                pd.notna(last_ma_short)
                and pd.notna(last_ma_long)
                and float(last_ma_long) > 0
            ):
                ma_gap = abs(float(last_ma_short) - float(last_ma_long)) / float(last_ma_long)
                accum_ma_ok = ma_gap <= cfg.accum_ma_gap_max
        if accum_vol_ok and not accum_ma_ok:
            accum_reasons.append("均线未胶着")

        accum_ok = accum_low_ok and accum_range_ok and accum_vol_ok and accum_ma_ok

    if momentum_ok or ambush_ok or accum_ok:
        return "通过L2（不应出现在淘汰列表）"

    momentum_reasons: list[str] = []
    if not (bullish_alignment or holding_ma20):
        momentum_reasons.append("结构不满足(MA50<=MA200且未守MA20)")
    if not momentum_rs_ok:
        if rs_long is None or rs_short is None:
            momentum_reasons.append("RS无法计算")
        else:
            momentum_reasons.append(
                f"RS不足(long={rs_long:.2f}, short={rs_short:.2f})"
            )
    if not momentum_rps_ok:
        if rps_fast is None or rps_slow is None:
            momentum_reasons.append("RPS无法计算")
        else:
            momentum_reasons.append(
                f"RPS不足(fast={float(rps_fast):.1f}, slow={float(rps_slow):.1f})"
            )

    ambush_reasons: list[str] = []
    if not cfg.enable_ambush_channel:
        ambush_reasons.append("通道关闭")
    else:
        if not ambush_shape_ok:
            ambush_reasons.append("形态不满足")
        if not ambush_rs_ok:
            ambush_reasons.append("RS不满足")
        if not ambush_rps_ok:
            ambush_reasons.append("RPS不满足")

    if not accum_reasons:
        accum_reasons.append("四条件未全满足")

    return (
        f"主升失败[{';'.join(momentum_reasons) or '条件不满足'}]；"
        f"潜伏失败[{';'.join(ambush_reasons) or '条件不满足'}]；"
        f"吸筹失败[{';'.join(accum_reasons)}]"
    )


def _build_hit_map(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, list[str]]:
    hit_map: dict[str, list[str]] = {}
    for trig, label in TRIGGER_LABELS.items():
        for code, _ in triggers.get(trig, []):
            hit_map.setdefault(str(code), [])
            if label not in hit_map[str(code)]:
                hit_map[str(code)].append(label)
    return hit_map


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
        try:
            parts.append(f"参考价={float(price):.2f}")
        except Exception:
            pass
    if trigger_labels:
        parts.append(f"L4命中={trigger_labels}")
    if reason:
        parts.append(reason)
    return " | ".join(parts)


def main() -> int:
    raw_list = os.getenv("REVIEW_LIST", "").strip() or os.getenv("review_list", "").strip()
    review_codes = _parse_review_list(raw_list)
    if not review_codes:
        print("[review] REVIEW_LIST/review_list 为空，示例: 300164,600759,002378")
        return 2

    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[review] FEISHU_WEBHOOK_URL 未配置")
        return 2

    print(f"[review] 输入代码数={len(review_codes)}: {', '.join(review_codes)}")
    triggers, metrics = run_funnel_job(include_debug_context=True)

    debug = metrics.get("_debug", {}) or {}
    if not debug:
        print("[review] 缺少调试上下文，无法复盘")
        return 3

    cfg: FunnelConfig = debug.get("cfg")
    all_symbols = [str(x) for x in (debug.get("all_symbols", []) or [])]
    name_map = debug.get("name_map", {}) or {}
    market_cap_map = debug.get("market_cap_map", {}) or {}
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

    l2_ctx = _build_layer2_context(l1_symbols=l1_symbols, df_map=df_map, bench_df=bench_df, cfg=cfg)
    hit_map = _build_hit_map(triggers)
    blocked_exit_map = _blocked_exit_signal_map(metrics.get("exit_signals", {}) or {})

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
            reason = "行业共振层未通过"
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
            reason = "未触发 Spring/LPS/EVR/SOS"

        stage_counter[stage] += 1
        rows.append(
            {
                "code": code,
                "name": name,
                "stage": stage,
                "reason": reason,
            }
        )

    summary = " | ".join([f"{k}{v}" for k, v in stage_counter.items()]) or "无"
    lines = [
        f"**交易日**: {end_trade_date}",
        f"**输入代码数**: {len(review_codes)}",
        f"**结果汇总**: {summary}",
        "",
        "**逐票复盘（止步层级与原因）**",
        "",
    ]

    for row in rows:
        lines.append(
            f"• {row['code']} {row['name']} | {row['stage']} | {row['reason']}"
        )

    title = "🧪 Review List 漏斗复盘"
    content = "\n".join(lines)
    ok = send_feishu_notification(webhook, title, content)
    print(f"[review] feishu_sent={ok}")

    if not ok:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
