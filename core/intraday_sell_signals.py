"""
盘中持仓卖出信号检测 — 纯函数，无副作用。

4 种信号:
  1. 止损穿破 (CRITICAL) — 现价 ≤ 止损位
  2. 跳空低开 (CRITICAL) — 开盘价跳空 ≥ N% (仅上午有效)
  3. 放量滞涨 (WARNING)  — 上午放量超昨日全天但涨幅微弱
  4. VWAP破位 (WARNING)  — 现价 < VWAP 且量比偏高
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class PositionSnapshot:
    code: str
    name: str
    cost: float
    shares: int
    stop_loss: float | None
    current_price: float
    open_price: float
    prev_close: float


@dataclass
class SellSignal:
    code: str
    name: str
    signal_type: str      # "止损穿破"|"跳空低开"|"放量滞涨"|"VWAP破位"
    severity: str         # "CRITICAL"|"WARNING"
    current_price: float
    trigger_value: float
    detail: str


# ── 信号检测 ──────────────────────────────────────────────

def check_stop_breach(snap: PositionSnapshot, hard_pct: float = 7.0) -> SellSignal | None:
    """现价 ≤ 止损价 或 现价 ≤ cost×(1-hard_pct/100)"""
    hard_stop = snap.cost * (1 - hard_pct / 100) if snap.cost > 0 else 0.0
    effective = max(snap.stop_loss or 0.0, hard_stop)
    if effective <= 0 or snap.current_price > effective:
        return None
    pnl_pct = (snap.current_price / snap.cost - 1) * 100 if snap.cost > 0 else 0.0
    return SellSignal(
        code=snap.code,
        name=snap.name,
        signal_type="止损穿破",
        severity="CRITICAL",
        current_price=snap.current_price,
        trigger_value=effective,
        detail=f"现价 {snap.current_price:.2f} ≤ 止损 {effective:.2f} | 成本 {snap.cost:.2f} | {'亏' if pnl_pct < 0 else '盈'} {pnl_pct:+.1f}%",
    )


def check_gap_down(snap: PositionSnapshot, gap_pct: float = 3.0) -> SellSignal | None:
    """开盘跳空低开 ≥ gap_pct%"""
    if snap.prev_close <= 0 or snap.open_price <= 0:
        return None
    gap = (snap.prev_close - snap.open_price) / snap.prev_close * 100
    if gap < gap_pct:
        return None
    return SellSignal(
        code=snap.code,
        name=snap.name,
        signal_type="跳空低开",
        severity="CRITICAL",
        current_price=snap.current_price,
        trigger_value=gap,
        detail=f"开盘 {snap.open_price:.2f} 较昨收 {snap.prev_close:.2f} 跳空 {gap:.1f}%",
    )


def check_volume_stall(
    snap: PositionSnapshot,
    df_5m: pd.DataFrame,
    yday_volume: float,
    gain_pct: float = 1.0,
) -> SellSignal | None:
    """上午累计量 > 昨日全天量 且涨幅 < gain_pct% → 放量滞涨"""
    if df_5m is None or df_5m.empty or yday_volume <= 0:
        return None
    if "datetime" not in df_5m.columns:
        return None
    morning = df_5m[df_5m["datetime"].dt.hour < 12]
    if morning.empty:
        return None
    morning_vol = float(morning["volume"].sum())
    if morning_vol <= yday_volume:
        return None
    price_gain = (snap.current_price / snap.open_price - 1) * 100 if snap.open_price > 0 else 0.0
    if price_gain >= gain_pct:
        return None
    vol_ratio = morning_vol / yday_volume
    return SellSignal(
        code=snap.code,
        name=snap.name,
        signal_type="放量滞涨",
        severity="WARNING",
        current_price=snap.current_price,
        trigger_value=vol_ratio,
        detail=f"上午量 {vol_ratio:.1f}x 昨全天 | 涨幅仅 {price_gain:+.1f}%",
    )


def _compute_vwap(df_1m: pd.DataFrame) -> float | None:
    """
    从 1m K线计算 VWAP，自适应 volume 量纲。
    复用 tail_buy_strategy._infer_session_vwap 的逻辑。
    """
    if df_1m is None or df_1m.empty:
        return None
    if "amount" not in df_1m.columns or "volume" not in df_1m.columns:
        return None
    total_amount = float(df_1m["amount"].sum())
    total_volume = float(df_1m["volume"].sum())
    if total_volume <= 0 or total_amount <= 0:
        return None
    ref_price = float(df_1m["close"].tail(min(len(df_1m), 30)).median())
    best: tuple[float, float] | None = None
    for scale in (1.0, 10.0, 100.0, 1000.0):
        v = total_amount / max(total_volume * scale, 1e-9)
        if v <= 0:
            continue
        err = abs(v - ref_price) / max(ref_price, 1e-8)
        if best is None or err < best[0]:
            best = (err, v)
    if best is None or best[0] > 5.0:
        return None
    return best[1]


def check_vwap_break(
    snap: PositionSnapshot,
    df_1m: pd.DataFrame,
    yday_volume: float,
    vol_ratio_threshold: float = 1.5,
) -> SellSignal | None:
    """现价 < VWAP 且日内量比 > 阈值"""
    vwap = _compute_vwap(df_1m)
    if vwap is None or snap.current_price >= vwap:
        return None
    if yday_volume <= 0:
        return None
    today_vol = float(df_1m["volume"].sum()) if df_1m is not None and not df_1m.empty else 0.0
    # 估算当前应有量 = 昨日量 × 已过交易时间占比
    if "datetime" not in df_1m.columns or df_1m.empty:
        return None
    elapsed_bars = len(df_1m)
    total_bars_per_day = 240  # 4小时 × 60分钟
    expected_vol = yday_volume * (elapsed_bars / total_bars_per_day) if total_bars_per_day > 0 else yday_volume
    vol_ratio = today_vol / max(expected_vol, 1e-9)
    if vol_ratio < vol_ratio_threshold:
        return None
    return SellSignal(
        code=snap.code,
        name=snap.name,
        signal_type="VWAP破位",
        severity="WARNING",
        current_price=snap.current_price,
        trigger_value=vwap,
        detail=f"现价 {snap.current_price:.2f} < VWAP {vwap:.2f} | 量比 {vol_ratio:.1f}x",
    )


# ── 聚合入口 ──────────────────────────────────────────────

def scan_position(
    snap: PositionSnapshot,
    df_1m: pd.DataFrame | None,
    df_5m: pd.DataFrame | None,
    yday_volume: float,
    *,
    hard_pct: float = 7.0,
    gap_pct: float = 3.0,
    gain_pct: float = 1.0,
    vol_ratio: float = 1.5,
    check_gap: bool = True,
) -> list[SellSignal]:
    """对单只持仓跑全部信号检测，返回触发的信号列表。"""
    signals: list[SellSignal] = []
    s = check_stop_breach(snap, hard_pct)
    if s:
        signals.append(s)
    if check_gap:
        s = check_gap_down(snap, gap_pct)
        if s:
            signals.append(s)
    s = check_volume_stall(snap, df_5m, yday_volume, gain_pct)
    if s:
        signals.append(s)
    s = check_vwap_break(snap, df_1m, yday_volume, vol_ratio)
    if s:
        signals.append(s)
    return signals
