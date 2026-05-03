"""
大盘水温 + 市场广度 + regime 分类工具。

分析大盘指数走势，计算市场广度，输出 regime 分类并动态调整阈值。
"""

from __future__ import annotations

import os

import pandas as pd

from core.wyckoff_engine import FunnelConfig

# ── 环境变量配置 ──

BREADTH_MA_WINDOW = int(os.getenv("FUNNEL_BREADTH_MA_WINDOW", "20"))
BREADTH_RISK_OFF_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_OFF_PCT", "20.0"))
BREADTH_RISK_ON_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_ON_PCT", "60.0"))
BREADTH_RISK_ON_MIN_DELTA = float(os.getenv("FUNNEL_BREADTH_RISK_ON_DELTA", "0.0"))
BREADTH_CLIFF_DROP_PCT = float(os.getenv("FUNNEL_BREADTH_CLIFF_DROP_PCT", "-10.0"))
SMALLCAP_BENCH_CODE = os.getenv("FUNNEL_SMALLCAP_BENCH_CODE", "399006").strip() or "399006"
CRASH_MAIN_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_MAIN_DAY_DROP_PCT", "-1.3"))
CRASH_SMALL_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_SMALL_DAY_DROP_PCT", "-2.5"))
CRASH_BREADTH_RATIO_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_RATIO_PCT", "15.0"))
CRASH_BREADTH_DELTA_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_DELTA_PCT", "-20.0"))
PANIC_REPAIR_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_PANIC_REPAIR_MIN_AVG_AMOUNT_WAN", "7000.0"))
RISK_OFF_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_RISK_OFF_MIN_AVG_AMOUNT_WAN", "8000.0"))
RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", "10000.0"))
CRASH_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "12000.0"))
PANIC_REPAIR_ENABLE = os.getenv("FUNNEL_PANIC_REPAIR_ENABLE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PANIC_REPAIR_MAIN_REBOUND_PCT = float(os.getenv("FUNNEL_PANIC_REPAIR_MAIN_REBOUND_PCT", "0.8"))
PANIC_REPAIR_SMALL_REBOUND_PCT = float(os.getenv("FUNNEL_PANIC_REPAIR_SMALL_REBOUND_PCT", "1.5"))
FUNNEL_EVR_POLICY = os.getenv("FUNNEL_EVR_POLICY", "all_regimes").strip().lower()


def calc_market_breadth(
    df_map: dict[str, pd.DataFrame],
    ma_window: int = BREADTH_MA_WINDOW,
) -> dict:
    """
    全市场广度：
    breadth = 收盘价站上 MA20 的股票占比（%）。
    额外给出前一日广度与日变化，用于识别扩散/收敛。
    """
    valid_now = 0
    valid_prev = 0
    above_now = 0
    above_prev = 0
    w = max(int(ma_window), 2)
    for df in df_map.values():
        if df is None or df.empty:
            continue
        s = df
        if "date" in s.columns:
            try:
                if not s["date"].is_monotonic_increasing:
                    s = s.sort_values("date")
            except Exception:
                s = s.sort_values("date")

        close = pd.to_numeric(s.get("close"), errors="coerce").dropna().tail(w + 1)
        if len(close) < (w + 1):
            continue

        c_now = float(close.iloc[-1])
        ma_now = float(close.iloc[1:].mean())
        c_prev = float(close.iloc[-2])
        ma_prev = float(close.iloc[:-1].mean())

        valid_now += 1
        if c_now >= ma_now:
            above_now += 1

        valid_prev += 1
        if c_prev >= ma_prev:
            above_prev += 1

    ratio_now = (above_now / valid_now * 100.0) if valid_now > 0 else None
    ratio_prev = (above_prev / valid_prev * 100.0) if valid_prev > 0 else None
    delta = None
    if ratio_now is not None and ratio_prev is not None:
        delta = ratio_now - ratio_prev
    return {
        "ratio_pct": ratio_now,
        "prev_ratio_pct": ratio_prev,
        "delta_pct": delta,
        "sample_size": valid_now,
    }


def analyze_benchmark_and_tune_cfg(
    bench_df: pd.DataFrame | None,
    smallcap_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    breadth: dict | None = None,
) -> dict:
    """
    Step 0：大盘总闸
    - 输出宏观水温（RISK_ON / NEUTRAL / RISK_OFF / CRASH / PANIC_REPAIR）
    - 在 RISK_OFF 时动态收紧个股过滤阈值
    """
    context = {
        "regime": "UNKNOWN",
        "main_code": "000001",
        "close": None,
        "ma50": None,
        "ma200": None,
        "ma50_slope_5d": None,
        "recent3_pct": [],
        "recent3_cum_pct": None,
        "smallcap_code": SMALLCAP_BENCH_CODE,
        "smallcap_close": None,
        "smallcap_recent3_pct": [],
        "smallcap_recent3_cum_pct": None,
        "smallcap_today_pct": None,
        "panic_triggered": False,
        "panic_reasons": [],
        "repair_triggered": False,
        "repair_reasons": [],
        "tuned": {
            "min_avg_amount_wan": cfg.min_avg_amount_wan,
            "rs_min_long": cfg.rs_min_long,
            "rs_min_short": cfg.rs_min_short,
            "rps_fast_min": cfg.rps_fast_min,
            "rps_slow_min": cfg.rps_slow_min,
        },
        "breadth": {
            "ratio_pct": None,
            "prev_ratio_pct": None,
            "delta_pct": None,
            "sample_size": 0,
            "ma_window": BREADTH_MA_WINDOW,
        },
    }
    close = None
    ma50 = None
    ma200 = None
    ma50_slope_5d = None
    recent3_list: list[float] = []
    recent3_cum = None
    main_today_pct = None
    main_prev_pct = None
    main_vol_ma5 = None
    main_vol_ma20 = None
    main_vol_ratio_5_20 = None
    main_volume_state = "未知"
    small_close = None
    small_recent3_list: list[float] = []
    small_recent3_cum = None
    small_today_pct = None
    small_prev_pct = None

    if bench_df is not None and not bench_df.empty:
        b = bench_df.sort_values("date").copy()
        b["close"] = pd.to_numeric(b["close"], errors="coerce")
        b["pct_chg"] = pd.to_numeric(b["pct_chg"], errors="coerce")
        b["volume"] = pd.to_numeric(b.get("volume"), errors="coerce")
        if len(b) >= 60:
            close = float(b["close"].iloc[-1])
            ma50 = float(b["close"].rolling(50).mean().iloc[-1])
            ma200 = float(b["close"].rolling(200).mean().iloc[-1])
            ma50_prev = b["close"].rolling(50).mean().shift(5).iloc[-1]
            ma50_slope_5d = None if pd.isna(ma50_prev) else float(ma50 - ma50_prev)
            recent3 = b["pct_chg"].dropna().tail(3)
            recent3_list = [float(x) for x in recent3.tolist()]
            if not recent3.empty:
                recent3_cum = float(((recent3 / 100.0 + 1.0).prod() - 1.0) * 100.0)
            if recent3_list:
                main_today_pct = float(recent3_list[-1])
                if len(recent3_list) >= 2:
                    main_prev_pct = float(recent3_list[-2])
            vol = b["volume"].dropna()
            if len(vol) >= 20:
                main_vol_ma20 = float(vol.tail(20).mean())
                main_vol_ma5 = float(vol.tail(5).mean())
                if main_vol_ma20 > 0:
                    main_vol_ratio_5_20 = float(main_vol_ma5 / main_vol_ma20)
                    if main_vol_ratio_5_20 >= 1.15:
                        main_volume_state = "放量"
                    elif main_vol_ratio_5_20 <= 0.85:
                        main_volume_state = "缩量"
                    else:
                        main_volume_state = "平量"

    if smallcap_df is not None and not smallcap_df.empty:
        s = smallcap_df.sort_values("date").copy()
        s["close"] = pd.to_numeric(s["close"], errors="coerce")
        s["pct_chg"] = pd.to_numeric(s["pct_chg"], errors="coerce")
        if len(s) >= 10:
            small_close = float(s["close"].iloc[-1])
            s_recent3 = s["pct_chg"].dropna().tail(3)
            small_recent3_list = [float(x) for x in s_recent3.tolist()]
            if not s_recent3.empty:
                small_recent3_cum = float(((s_recent3 / 100.0 + 1.0).prod() - 1.0) * 100.0)
            if small_recent3_list:
                small_today_pct = float(small_recent3_list[-1])
                if len(small_recent3_list) >= 2:
                    small_prev_pct = float(small_recent3_list[-2])

    regime = "NEUTRAL"
    if (
        ma200 is not None
        and ma50 is not None
        and ma50_slope_5d is not None
        and recent3_cum is not None
        and close is not None
    ):
        risk_off = (close < ma200) and (ma50 < ma200) and (ma50_slope_5d < 0) and (recent3_cum <= -2.0)
        risk_on = (close > ma50 > ma200) and (ma50_slope_5d > 0) and (recent3_cum >= 0.0)
        if risk_off:
            regime = "RISK_OFF"
        elif risk_on:
            regime = "RISK_ON"

    breadth_ratio = None
    breadth_prev = None
    breadth_delta = None
    breadth_sample = 0
    if breadth:
        breadth_ratio = breadth.get("ratio_pct")
        breadth_prev = breadth.get("prev_ratio_pct")
        breadth_delta = breadth.get("delta_pct")
        breadth_sample = int(breadth.get("sample_size") or 0)
    if breadth_ratio is not None:
        if float(breadth_ratio) <= BREADTH_RISK_OFF_THRESHOLD:
            regime = "RISK_OFF"
        elif float(breadth_ratio) >= BREADTH_RISK_ON_THRESHOLD:
            if breadth_delta is None or float(breadth_delta) >= BREADTH_RISK_ON_MIN_DELTA:
                regime = "RISK_ON"

        # 强力悬崖检测 (Breadth Cliff Drop): 赚了指数不赚钱，暗流涌动的隐性雪崩
        if breadth_delta is not None and float(breadth_delta) <= BREADTH_CLIFF_DROP_PCT:
            regime = "RISK_OFF"

    panic_reasons: list[str] = []
    if main_today_pct is not None and float(main_today_pct) <= float(CRASH_MAIN_DAY_DROP_PCT):
        panic_reasons.append(f"main_day_drop={main_today_pct:.2f}%<=阈值{CRASH_MAIN_DAY_DROP_PCT:.2f}%")
    if small_today_pct is not None and float(small_today_pct) <= float(CRASH_SMALL_DAY_DROP_PCT):
        panic_reasons.append(f"smallcap_day_drop={small_today_pct:.2f}%<=阈值{CRASH_SMALL_DAY_DROP_PCT:.2f}%")
    if breadth_ratio is not None and float(breadth_ratio) <= float(CRASH_BREADTH_RATIO_PCT):
        panic_reasons.append(f"breadth_ratio={float(breadth_ratio):.2f}%<=阈值{CRASH_BREADTH_RATIO_PCT:.2f}%")
    if breadth_delta is not None and float(breadth_delta) <= float(CRASH_BREADTH_DELTA_PCT):
        panic_reasons.append(f"breadth_delta={float(breadth_delta):.2f}%<=阈值{CRASH_BREADTH_DELTA_PCT:.2f}%")
    repair_reasons: list[str] = []
    if panic_reasons:
        regime = "CRASH"
    elif PANIC_REPAIR_ENABLE:
        # 改进逻辑：支持连续反弹（前 1-2 天是 CRASH，最近 1-2 天反弹）
        prev_panic = (main_prev_pct is not None and float(main_prev_pct) <= float(CRASH_MAIN_DAY_DROP_PCT)) or (
            small_prev_pct is not None and float(small_prev_pct) <= float(CRASH_SMALL_DAY_DROP_PCT)
        )
        rebound_ok = (main_today_pct is not None and float(main_today_pct) >= float(PANIC_REPAIR_MAIN_REBOUND_PCT)) or (
            small_today_pct is not None and float(small_today_pct) >= float(PANIC_REPAIR_SMALL_REBOUND_PCT)
        )
        # 连续反弹：最近 2 日都反弹
        continuous_rebound = False
        if main_today_pct is not None and main_prev_pct is not None:
            continuous_rebound = (
                float(main_today_pct) >= float(PANIC_REPAIR_MAIN_REBOUND_PCT) * 0.5
                and float(main_prev_pct) >= float(PANIC_REPAIR_MAIN_REBOUND_PCT) * 0.5
            )

        if (prev_panic and rebound_ok) or continuous_rebound:
            regime = "PANIC_REPAIR"
            repair_reasons = [
                f"prev_panic(main_prev={main_prev_pct}, small_prev={small_prev_pct})",
                f"rebound_ok(main_today={main_today_pct}, small_today={small_today_pct})",
            ]

    # EVR 开关策略：
    # - all_regimes(默认): 各市场水温都开启，保持信号连续性
    # - cold_only: 仅在 RISK_OFF/CRASH 开启
    # - respect_cfg: 使用 FunnelConfig 当前值
    # - off: 全关闭
    evr_policy = FUNNEL_EVR_POLICY
    if evr_policy in {"cold_only", "risk_off", "risk_off_crash"}:
        cfg.enable_evr_trigger = regime in {"RISK_OFF", "CRASH"}
    elif evr_policy in {"off", "disabled", "disable", "0", "false", "no"}:
        cfg.enable_evr_trigger = False
    elif evr_policy in {"respect_cfg", "cfg", "config"}:
        cfg.enable_evr_trigger = bool(cfg.enable_evr_trigger)
    else:
        cfg.enable_evr_trigger = True

    # 动态调参：风险越冷，过滤越严
    if regime == "CRASH":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, CRASH_MIN_AVG_AMOUNT_WAN)
        cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)
    elif regime == "PANIC_REPAIR":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, PANIC_REPAIR_MIN_AVG_AMOUNT_WAN)
        cfg.rs_min_long = max(cfg.rs_min_long, 1.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.2)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 75.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 65.0)
    elif regime == "RISK_OFF":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, RISK_OFF_MIN_AVG_AMOUNT_WAN)
        cfg.rs_min_long = max(cfg.rs_min_long, 2.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.5)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)
        if recent3_cum is not None and recent3_cum <= -4.0:
            cfg.min_avg_amount_wan = max(
                cfg.min_avg_amount_wan,
                RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN,
            )
            cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
            cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
    elif regime == "RISK_ON":
        cfg.rs_min_long = max(cfg.rs_min_long, 0.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.0)
        cfg.rps_fast_min = min(cfg.rps_fast_min, 70.0)
        cfg.rps_slow_min = min(cfg.rps_slow_min, 60.0)

    price_zone = "结构待确认"
    if close is not None and ma50 is not None and ma200 is not None:
        if close > ma50 > ma200:
            price_zone = "多头上方"
        elif close < ma50 < ma200:
            price_zone = "空头下方"
        elif close >= ma50 and close <= ma200:
            price_zone = "反抽修复区"
        elif close < ma50 and close >= ma200:
            price_zone = "高位回撤区"
        else:
            price_zone = "震荡博弈区"
    ratio_text = f"{main_vol_ratio_5_20:.2f}x" if main_vol_ratio_5_20 is not None else "未知"
    market_pv_summary = f"沪深300近5日均量/20日均量={ratio_text}（{main_volume_state}），当前位于{price_zone}。"
    if regime == "RISK_ON":
        market_pv_outlook = (
            "次日推演：若量能维持在20日均量0.95x上方且不破MA50，偏强震荡延续概率更高；若放量跌破MA50，需转入防守。"
        )
    elif regime == "PANIC_REPAIR":
        market_pv_outlook = "次日推演：修复阶段以确认强度为先，若放量站稳MA50可继续修复；若缩量冲高回落，按反抽处理。"
    elif regime == "NEUTRAL":
        market_pv_outlook = (
            "次日推演：中性震荡为主，等待\u201c放量突破近高\u201d或\u201c放量跌破MA50\u201d后再确认方向。"
        )
    elif regime in {"RISK_OFF", "CRASH"}:
        market_pv_outlook = "次日推演：防守优先，若出现放量下压并失守MA50，继续收缩风险敞口；仅在缩量止跌后再评估试探。"
    else:
        market_pv_outlook = "次日推演：结构信息不足，先观察量能与MA50得失再定方向。"

    context.update(
        {
            "regime": regime,
            "close": close,
            "ma50": ma50,
            "ma200": ma200,
            "ma50_slope_5d": ma50_slope_5d,
            "recent3_pct": recent3_list,
            "recent3_cum_pct": recent3_cum,
            "main_today_pct": main_today_pct,
            "main_vol_ma5": main_vol_ma5,
            "main_vol_ma20": main_vol_ma20,
            "main_vol_ratio_5_20": main_vol_ratio_5_20,
            "main_volume_state": main_volume_state,
            "market_pv_summary": market_pv_summary,
            "market_pv_outlook": market_pv_outlook,
            "smallcap_close": small_close,
            "smallcap_recent3_pct": small_recent3_list,
            "smallcap_recent3_cum_pct": small_recent3_cum,
            "smallcap_today_pct": small_today_pct,
            "panic_triggered": bool(panic_reasons),
            "panic_reasons": panic_reasons,
            "repair_triggered": bool(repair_reasons),
            "repair_reasons": repair_reasons,
            "tuned": {
                "min_avg_amount_wan": cfg.min_avg_amount_wan,
                "rs_min_long": cfg.rs_min_long,
                "rs_min_short": cfg.rs_min_short,
                "rps_fast_min": cfg.rps_fast_min,
                "rps_slow_min": cfg.rps_slow_min,
                "enable_evr_trigger": bool(cfg.enable_evr_trigger),
            },
            "breadth": {
                "ratio_pct": breadth_ratio,
                "prev_ratio_pct": breadth_prev,
                "delta_pct": breadth_delta,
                "sample_size": breadth_sample,
                "ma_window": BREADTH_MA_WINDOW,
            },
        }
    )
    return context
