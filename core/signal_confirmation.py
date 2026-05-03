"""信号确认逻辑：pending → confirmed / expired。纯业务，不依赖 DB。"""

from __future__ import annotations

from typing import Any

import pandas as pd

SIGNAL_TTL_DAYS: dict[str, int] = {
    "sos": 2,
    "spring": 3,
    "lps": 3,
    "evr": 2,
}


def check_confirmation(
    signal_type: str,
    snap: dict[str, Any],
    today_ohlcv: dict[str, float],
    days_elapsed: int,
) -> tuple[str, str]:
    """返回 (new_status, reason)，status ∈ {'pending', 'confirmed', 'expired'}。"""
    ttl = SIGNAL_TTL_DAYS.get(signal_type, 3)
    if days_elapsed >= ttl:
        return "expired", f"TTL {ttl}天已到，未满足确认条件"
    fn = _CONFIRM_DISPATCH.get(signal_type)
    if fn is None:
        return "expired", f"未知信号类型: {signal_type}"
    return fn(snap, today_ohlcv, days_elapsed)


def _confirm_sos(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    snap_low, snap_close, snap_vol = snap.get("snap_low", 0), snap.get("snap_close", 0), snap.get("snap_volume", 0)
    if today["low"] < snap_low:
        return "expired", f"跌破信号日低点 {snap_low:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 0.8 and today["close"] < snap_close * 0.97:
        return "expired", "放量回落，非缩量确认"
    if (
        snap_vol > 0
        and today["volume"] < snap_vol * 0.8
        and today["low"] >= snap_low
        and today["close"] >= snap_close * 0.97
    ):
        return "confirmed", f"缩量回踩确认，收盘 {today['close']:.2f} 守住 {snap_low:.2f}"
    return "pending", "等待缩量确认"


def _confirm_spring(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    support, snap_ma20 = snap.get("snap_support", 0), snap.get("snap_ma20", 0)
    if today["low"] < support * 0.98:
        return "expired", f"跌破支撑 {support:.2f}"
    if today["close"] > support and today["close"] >= snap_ma20 * 0.97:
        return "confirmed", f"守住支撑 {support:.2f}，收盘接近 MA20"
    return "pending", "等待收回 MA20"


def _confirm_lps(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    snap_ma20, snap_vol = snap.get("snap_ma20", 0), snap.get("snap_volume", 0)
    if today["low"] < snap_ma20 * 0.98:
        return "expired", f"跌破 MA20 {snap_ma20:.2f}"
    if snap_vol > 0 and today["volume"] > snap_vol * 1.5:
        return "expired", "异常放量，LPS 逻辑失效"
    if today["close"] >= snap_ma20 and (snap_vol <= 0 or today["volume"] <= snap_vol * 1.2):
        return "confirmed", f"站稳 MA20 {snap_ma20:.2f}，缩量确认"
    return "pending", "等待站稳 MA20"


def _confirm_evr(snap: dict, today: dict, days_elapsed: int) -> tuple[str, str]:
    event_low, snap_close = snap.get("snap_support", 0), snap.get("snap_close", 0)
    if today["close"] < event_low:
        return "expired", f"跌破事件日低点 {event_low:.2f}"
    if today["close"] >= event_low and today["close"] >= snap_close * 0.98:
        return "confirmed", f"守住 {event_low:.2f}，收盘 {today['close']:.2f}"
    return "pending", "等待企稳确认"


_CONFIRM_DISPATCH = {
    "sos": _confirm_sos,
    "spring": _confirm_spring,
    "lps": _confirm_lps,
    "evr": _confirm_evr,
}


def build_snap(
    signal_type: str,
    df: pd.DataFrame,
    score: float,
    cfg: Any = None,
) -> dict[str, Any]:
    """从 OHLCV DataFrame 最后一根 K 线构建价格快照。"""
    df_s = df.sort_values("date") if "date" in df.columns else df
    last = df_s.iloc[-1]
    ma20 = float(df_s["close"].rolling(20).mean().iloc[-1]) if len(df_s) >= 20 else float(last["close"])
    ma50 = float(df_s["close"].rolling(50).mean().iloc[-1]) if len(df_s) >= 50 else float(last["close"])

    snap = {
        "snap_open": float(last["open"]),
        "snap_high": float(last["high"]),
        "snap_low": float(last["low"]),
        "snap_close": float(last["close"]),
        "snap_volume": float(last["volume"]),
        "snap_ma20": ma20,
        "snap_ma50": ma50,
    }

    if signal_type == "spring":
        window = 60 if cfg is None else getattr(cfg, "spring_support_window", 60)
        zone = df_s.iloc[-(window + 2) : -2] if len(df_s) > window + 2 else df_s.iloc[:-2]
        snap["snap_support"] = float(zone["close"].min()) if len(zone) > 0 else float(last["low"])
    elif signal_type == "sos":
        snap["snap_support"] = float(df_s["high"].tail(21).iloc[:-1].max()) if len(df_s) >= 21 else float(last["high"])
    elif signal_type == "lps":
        snap["snap_support"] = ma20
    else:
        snap["snap_support"] = float(last["low"])

    return snap


def build_today_ohlcv(df: pd.DataFrame) -> dict[str, float]:
    """从 DataFrame 最后一根 K 线构建 today_ohlcv dict。"""
    df_s = df.sort_values("date") if "date" in df.columns else df
    last = df_s.iloc[-1]
    ma20 = float(df_s["close"].rolling(20).mean().iloc[-1]) if len(df_s) >= 20 else float(last["close"])
    ma50 = float(df_s["close"].rolling(50).mean().iloc[-1]) if len(df_s) >= 50 else float(last["close"])
    return {
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": float(last["close"]),
        "volume": float(last["volume"]),
        "ma20": ma20,
        "ma50": ma50,
    }


def run_confirmation_cycle(
    pending_signals: list[dict],
    df_map: dict[str, pd.DataFrame],
    trade_date: str,
) -> tuple[list[dict], list[dict]]:
    """对一批 pending 信号执行确认/过期判定，返回 (updates, confirmed_symbols)。"""
    updates: list[dict] = []
    confirmed_symbols: list[dict] = []

    for sig in pending_signals:
        # 信号日当天不做确认检查：当天 K 线 == 信号快照，无法验证"次日回踩"
        if str(sig.get("signal_date", ""))[:10] == str(trade_date)[:10]:
            continue

        code_str = f"{int(sig['code']):06d}"
        df = df_map.get(code_str)
        if df is None or df.empty:
            continue

        days = sig.get("days_elapsed", 0) + 1
        today = build_today_ohlcv(df)
        snap = {k: sig[k] for k in sig if k.startswith("snap_")}
        new_status, reason = check_confirmation(sig["signal_type"], snap, today, days)

        update: dict[str, Any] = {
            "id": sig["id"],
            "status": new_status,
            "days_elapsed": days,
            "confirm_reason": reason,
        }
        if new_status == "confirmed":
            update["confirm_date"] = trade_date
            confirmed_symbols.append(
                {
                    "code": code_str,
                    "name": sig.get("name", code_str),
                    "tag": f"{sig['signal_type'].upper()}(确认)",
                    "track": "Accum" if sig["signal_type"] in ("spring", "lps") else "Trend",
                    "initial_price": today["close"],
                    "score": sig.get("signal_score", 0),
                    "signal_type": sig["signal_type"],
                    "signal_date": str(sig["signal_date"]),
                }
            )
        elif new_status == "expired":
            update["expire_date"] = trade_date
        updates.append(update)

    return updates, confirmed_symbols


class PendingPool:
    """signal_pending 的内存模拟，用于回测。"""

    def __init__(self) -> None:
        self._pool: dict[tuple[str, str], dict] = {}
        self._next_id: int = 1

    def write(
        self,
        signal_date: str,
        triggers: dict[str, list[tuple[str, float]]],
        df_map: dict[str, pd.DataFrame],
        regime: str = "NEUTRAL",
        name_map: dict[str, str] | None = None,
        sector_map: dict[str, str] | None = None,
        cfg: Any = None,
    ) -> int:
        name_map, sector_map = name_map or {}, sector_map or {}
        added = 0
        for signal_type, hits in triggers.items():
            ttl = SIGNAL_TTL_DAYS.get(signal_type, 3)
            for code, score in hits:
                key = (code, signal_type)
                if key in self._pool:
                    continue
                df = df_map.get(code)
                if df is None or df.empty:
                    continue
                snap = build_snap(signal_type, df, score, cfg)
                self._pool[key] = {
                    "id": self._next_id,
                    "code": int(code) if code.isdigit() else code,
                    "signal_type": signal_type,
                    "signal_date": signal_date,
                    "signal_score": score,
                    "status": "pending",
                    "ttl_days": ttl,
                    "days_elapsed": 0,
                    "regime": regime,
                    "name": name_map.get(code, code),
                    "industry": sector_map.get(code, ""),
                    **snap,
                }
                self._next_id += 1
                added += 1
        return added

    def tick(self, df_map: dict[str, pd.DataFrame], trade_date: str) -> list[dict]:
        """推进一天，返回确认通过的 symbol_info 列表。"""
        if not self._pool:
            return []
        updates, confirmed = run_confirmation_cycle(list(self._pool.values()), df_map, trade_date)
        for upd in updates:
            if upd["status"] in ("confirmed", "expired"):
                for key, sig in list(self._pool.items()):
                    if sig["id"] == upd["id"]:
                        del self._pool[key]
                        break
            else:
                for sig in self._pool.values():
                    if sig["id"] == upd["id"]:
                        sig["days_elapsed"] = upd["days_elapsed"]
                        break
        return confirmed
