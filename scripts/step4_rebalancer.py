# -*- coding: utf-8 -*-
"""
阶段 4：私人账户再平衡决策（OMS 重构版）
1) LLM 只输出结构化动作 JSON
2) Python 订单管理引擎负责仓位/手数/风险计算
3) 输出标准交易工单并推送 Telegram
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.ai_prompts import PRIVATE_PM_DECISION_JSON_PROMPT
from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window
from integrations.llm_client import call_llm
from integrations.data_source import fetch_stock_spot_snapshot
from integrations.supabase_portfolio import (
    check_daily_run_exists,
    load_portfolio_state as load_portfolio_state_from_supabase,
    save_ai_trade_orders,
    update_position_stops,
    upsert_daily_nav,
)
from scripts.step3_batch_report import generate_stock_payload

TRADING_DAYS = 500
TELEGRAM_MAX_LEN = 3900
CN_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", "15"))
MARKET_DATA_READY_HOUR = int(
    os.getenv(
        "MARKET_DATA_READY_HOUR",
        str(max(MARKET_CLOSE_HOUR, 20)),
    )
)
ENFORCE_TARGET_TRADE_DATE = os.getenv(
    "ENFORCE_TARGET_TRADE_DATE", "1"
).strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO_FULL = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {"1", "true", "yes", "on"}
STEP4_MAX_OUTPUT_TOKENS = 8192
STEP4_ATR_PERIOD = int(os.getenv("STEP4_ATR_PERIOD", "14"))
STEP4_ATR_MULTIPLIER = float(os.getenv("STEP4_ATR_MULTIPLIER", "2.0"))
STEP4_MAX_WORKERS = int(os.getenv("STEP4_MAX_WORKERS", "8"))
STEP4_ENABLE_SPOT_PATCH = os.getenv("STEP4_ENABLE_SPOT_PATCH", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STEP4_SPOT_PATCH_RETRIES = int(os.getenv("STEP4_SPOT_PATCH_RETRIES", "2"))
STEP4_SPOT_PATCH_SLEEP = float(os.getenv("STEP4_SPOT_PATCH_SLEEP", "0.3"))
STEP4_ATR_SLIPPAGE_FACTOR = float(os.getenv("STEP4_ATR_SLIPPAGE_FACTOR", "0.25"))
LIVE_PORTFOLIO_ID = "USER_LIVE"


@dataclass
class PositionItem:
    code: str
    name: str
    cost: float
    buy_dt: str
    shares: int
    strategy: str
    stop_loss: float | None = None


@dataclass
class PortfolioState:
    free_cash: float
    total_equity: float | None
    positions: list[PositionItem]


@dataclass
class DecisionItem:
    code: str
    name: str
    action: str
    entry_zone_min: float | None
    entry_zone_max: float | None
    stop_loss: float | None
    trim_ratio: float | None
    tape_condition: str
    invalidate_condition: str
    is_add_on: bool
    reason: str
    confidence: float | None


@dataclass
class ExecutionTicket:
    code: str
    name: str
    action: str
    status: str
    shares: int
    price_hint: float | None
    amount: float
    stop_loss: float | None
    max_loss: float
    drawdown_ratio: float
    reason: str
    tape_condition: str
    invalidate_condition: str
    is_holding: bool
    atr14: float | None
    original_stop_loss: float | None
    effective_stop_loss: float | None
    slippage_bps: float
    audit: str


class WyckoffOrderEngine:
    """
    确定性订单执行引擎（OMS）
    """

    SLIPPAGE_BPS = 0.005
    RISK_LIMITS = {
        "PROBE": 0.008,   # 0.8%
        "ATTACK": 0.012,  # 1.2%
    }
    BUDGET_LIMITS = {
        "PROBE": 0.08,    # 8%
        "ATTACK": 0.25,   # 25%
    }
    PRIORITY_MAP = {
        "EXIT": 1,
        "TRIM": 2,
        "HOLD": 3,
        "PROBE": 4,
        "ATTACK": 5,
    }

    def __init__(
        self,
        total_equity: float,
        free_cash: float,
        position_map: dict[str, PositionItem],
        latest_price_map: dict[str, float],
        atr_map: dict[str, float] | None = None,
    ) -> None:
        self.total_equity = float(max(total_equity, 0.0))
        self.free_cash = float(max(free_cash, 0.0))
        self.position_map = position_map
        self.latest_price_map = latest_price_map
        self.atr_map = atr_map or {}

    def process(self, decisions: list[DecisionItem]) -> tuple[list[ExecutionTicket], float]:
        ordered = sorted(decisions, key=lambda d: self.PRIORITY_MAP.get(d.action, 99))
        tickets: list[ExecutionTicket] = []

        for dec in ordered:
            ticket = self._process_one(dec)
            tickets.append(ticket)
        return (tickets, self.free_cash)

    def _approved_hold(
        self,
        dec: DecisionItem,
        name: str,
        current_price: float,
        effective_stop_loss: float | None,
        atr14: float | None,
        original_stop_loss: float | None,
        audit_parts: list[str],
        reason: str | None = None,
    ) -> ExecutionTicket:
        return ExecutionTicket(
            code=dec.code,
            name=name,
            action="HOLD",
            status="APPROVED",
            shares=0,
            price_hint=current_price,
            amount=0.0,
            stop_loss=effective_stop_loss,
            max_loss=0.0,
            drawdown_ratio=0.0,
            reason=(reason or dec.reason or "").strip(),
            tape_condition=dec.tape_condition,
            invalidate_condition=dec.invalidate_condition,
            is_holding=(dec.code in self.position_map and self.position_map[dec.code].shares >= 100),
            atr14=atr14,
            original_stop_loss=original_stop_loss,
            effective_stop_loss=effective_stop_loss,
            slippage_bps=self.SLIPPAGE_BPS,
            audit="; ".join(audit_parts + ["hold"]),
        )

    def _process_one(self, dec: DecisionItem) -> ExecutionTicket:
        code = dec.code
        name = dec.name or code
        action = dec.action
        current_price = self.latest_price_map.get(code)
        pos = self.position_map.get(code)
        held_shares = int(pos.shares) if pos else 0
        atr14 = self.atr_map.get(code)
        original_stop_loss = dec.stop_loss
        effective_stop_loss = dec.stop_loss
        audit_parts: list[str] = []

        if current_price is None or current_price <= 0:
            return self._no_trade(dec, name, "缺少最新价格")

        if atr14 is not None and atr14 > 0:
            trailing_stop = current_price - STEP4_ATR_MULTIPLIER * atr14
            if dec.action in {"HOLD", "TRIM", "EXIT"}:
                if effective_stop_loss is None or trailing_stop > effective_stop_loss:
                    effective_stop_loss = trailing_stop
                    audit_parts.append(
                        f"atr_trailing_raise({(original_stop_loss if original_stop_loss is not None else float('nan')):.2f}->{effective_stop_loss:.2f})"
                    )
            elif dec.action in {"PROBE", "ATTACK"}:
                if effective_stop_loss is None:
                    effective_stop_loss = trailing_stop
                    audit_parts.append(f"atr_entry_guard({effective_stop_loss:.2f})")
                else:
                    merged = max(effective_stop_loss, trailing_stop)
                    if merged > effective_stop_loss:
                        audit_parts.append(
                            f"atr_entry_tighten({effective_stop_loss:.2f}->{merged:.2f})"
                        )
                    effective_stop_loss = merged

        if action == "EXIT":
            sell_shares = int(math.floor(max(held_shares, 0) / 100.0) * 100)
            if sell_shares < 100:
                return self._no_trade(dec, name, "无可卖持仓")
            fill_price = current_price * (1.0 - self.SLIPPAGE_BPS)
            proceeds = sell_shares * fill_price
            self.free_cash += proceeds
            return ExecutionTicket(
                code=code,
                name=name,
                action=action,
                status="APPROVED",
                shares=sell_shares,
                price_hint=fill_price,
                amount=proceeds,
                stop_loss=effective_stop_loss,
                max_loss=0.0,
                drawdown_ratio=0.0,
                reason=dec.reason,
                tape_condition=dec.tape_condition,
                invalidate_condition=dec.invalidate_condition,
                is_holding=held_shares >= 100,
                atr14=atr14,
                original_stop_loss=original_stop_loss,
                effective_stop_loss=effective_stop_loss,
                slippage_bps=self.SLIPPAGE_BPS,
                audit="; ".join(audit_parts + ["sell_with_slippage"]),
            )

        if action == "TRIM":
            ratio = dec.trim_ratio if dec.trim_ratio is not None else 0.5
            ratio = min(max(ratio, 0.1), 1.0)
            sell_shares = int(math.floor(held_shares * ratio / 100.0) * 100)
            if sell_shares < 100:
                return self._no_trade(dec, name, "减仓股数不足100股")
            fill_price = current_price * (1.0 - self.SLIPPAGE_BPS)
            proceeds = sell_shares * fill_price
            self.free_cash += proceeds
            return ExecutionTicket(
                code=code,
                name=name,
                action=action,
                status="APPROVED",
                shares=sell_shares,
                price_hint=fill_price,
                amount=proceeds,
                stop_loss=effective_stop_loss,
                max_loss=0.0,
                drawdown_ratio=0.0,
                reason=dec.reason,
                tape_condition=dec.tape_condition,
                invalidate_condition=dec.invalidate_condition,
                is_holding=held_shares >= 100,
                atr14=atr14,
                original_stop_loss=original_stop_loss,
                effective_stop_loss=effective_stop_loss,
                slippage_bps=self.SLIPPAGE_BPS,
                audit="; ".join(audit_parts + [f"trim_ratio={ratio:.2f}", "sell_with_slippage"]),
            )

        if action == "HOLD":
            return self._approved_hold(
                dec,
                name,
                current_price,
                effective_stop_loss,
                atr14,
                original_stop_loss,
                audit_parts,
            )

        # BUY: PROBE / ATTACK
        if effective_stop_loss is None:
            if held_shares >= 100:
                return self._approved_hold(
                    dec,
                    name,
                    current_price,
                    effective_stop_loss,
                    atr14,
                    original_stop_loss,
                    audit_parts + ["invalid_stop_loss->hold"],
                    reason=f"非法指令: 缺少 stop_loss，降级为 HOLD；原建议: {dec.reason}",
                )
            return self._no_trade(dec, name, "缺少 stop_loss")
        if effective_stop_loss <= 0:
            if held_shares >= 100:
                return self._approved_hold(
                    dec,
                    name,
                    current_price,
                    None,
                    atr14,
                    original_stop_loss,
                    audit_parts + ["stop_loss<=0->hold"],
                    reason=f"非法指令: stop_loss<=0，降级为 HOLD；原建议: {dec.reason}",
                )
            return self._no_trade(dec, name, "非法 stop_loss<=0")
        if effective_stop_loss >= current_price:
            return self._no_trade(dec, name, "止损倒挂(stop_loss >= current_price)")

        # 加仓开关约束：标记为加仓时，必须已有持仓且浮盈
        if dec.is_add_on:
            if not pos or held_shares < 100:
                return self._no_trade(dec, name, "is_add_on=true 但无可加仓持仓")
            if pos.cost > 0 and current_price <= pos.cost:
                # 对已有持仓若不满足“浮盈加仓”，降级为防守持有，避免给出自相矛盾的加仓指令
                return self._approved_hold(
                    dec,
                    name,
                    current_price,
                    effective_stop_loss,
                    atr14,
                    original_stop_loss,
                    audit_parts + ["add_on_without_profit->hold"],
                    reason=f"加仓条件不满足（当前未浮盈），降级为 HOLD；原建议: {dec.reason}",
                )

        price_for_calc = current_price
        if dec.entry_zone_min is not None and dec.entry_zone_max is not None:
            if dec.entry_zone_min <= 0 or dec.entry_zone_max <= 0:
                if held_shares >= 100:
                    return self._approved_hold(
                        dec,
                        name,
                        current_price,
                        effective_stop_loss,
                        atr14,
                        original_stop_loss,
                        audit_parts + ["entry_zone<=0->hold"],
                        reason=f"非法指令: entry_zone<=0，降级为 HOLD；原建议: {dec.reason}",
                    )
                return self._no_trade(dec, name, "非法 entry_zone<=0")
            if dec.entry_zone_min > dec.entry_zone_max:
                if held_shares >= 100:
                    return self._approved_hold(
                        dec,
                        name,
                        current_price,
                        effective_stop_loss,
                        atr14,
                        original_stop_loss,
                        audit_parts + ["entry_zone_invert->hold"],
                        reason=f"非法指令: entry_zone_min>entry_zone_max，降级为 HOLD；原建议: {dec.reason}",
                    )
                return self._no_trade(dec, name, "非法 entry_zone_min>entry_zone_max")
            price_for_calc = (dec.entry_zone_min + dec.entry_zone_max) / 2.0
            if price_for_calc <= 0:
                price_for_calc = current_price

        # 计算每股真实风险（静态滑点 + ATR 跳空保护）
        base_slippage = current_price * self.SLIPPAGE_BPS
        atr_slippage = (
            max(float(atr14), 0.0) * max(STEP4_ATR_SLIPPAGE_FACTOR, 0.0)
            if atr14 is not None
            else 0.0
        )
        slippage_abs = max(base_slippage, atr_slippage)
        fill_price = current_price + slippage_abs
        risk_per_share = fill_price - effective_stop_loss
        if risk_per_share <= 0:
            return self._no_trade(dec, name, "风险参数异常(risk_per_share<=0)")

        # 1) 风控允许的最大股数
        max_loss_allowed = self.total_equity * self.RISK_LIMITS[action]
        max_shares_by_risk = max_loss_allowed / risk_per_share

        # 2) 预算与现金允许的最大股数
        budget = min(self.total_equity * self.BUDGET_LIMITS[action], self.free_cash)
        max_shares_by_cash = budget / fill_price

        # 3) 取最小值并 A 股整手
        raw_shares = min(max_shares_by_risk, max_shares_by_cash)
        actual_shares = math.floor(raw_shares / 100.0) * 100
        if actual_shares < 100:
            return self._no_trade(dec, name, "计算股数不足100股(触及风控或资金限制)")

        actual_shares = int(actual_shares)
        amount = actual_shares * fill_price
        max_loss = actual_shares * risk_per_share
        drawdown_ratio = (max_loss / self.total_equity) if self.total_equity > 0 else 0.0
        effective_slippage_bps = (
            slippage_abs / current_price if current_price > 0 else self.SLIPPAGE_BPS
        )

        self.free_cash -= amount
        return ExecutionTicket(
            code=code,
            name=name,
            action=action,
            status="APPROVED",
            shares=actual_shares,
            price_hint=price_for_calc if price_for_calc > 0 else fill_price,
            amount=amount,
            stop_loss=effective_stop_loss,
            max_loss=max_loss,
            drawdown_ratio=drawdown_ratio,
            reason=dec.reason,
            tape_condition=dec.tape_condition,
            invalidate_condition=dec.invalidate_condition,
            is_holding=held_shares >= 100,
            atr14=atr14,
            original_stop_loss=original_stop_loss,
            effective_stop_loss=effective_stop_loss,
            slippage_bps=effective_slippage_bps,
            audit="; ".join(
                audit_parts
                + [
                    f"risk_per_share={risk_per_share:.4f}",
                    f"base_slippage={base_slippage:.4f}",
                    f"atr_slippage={atr_slippage:.4f}",
                    f"budget={budget:.2f}",
                    f"shares_by_risk={max_shares_by_risk:.2f}",
                    f"shares_by_cash={max_shares_by_cash:.2f}",
                    "buy_with_slippage",
                ]
            ),
        )

    def _no_trade(self, dec: DecisionItem, name: str, reason: str) -> ExecutionTicket:
        return ExecutionTicket(
            code=dec.code,
            name=name,
            action=dec.action,
            status="NO_TRADE",
            shares=0,
            price_hint=None,
            amount=0.0,
            stop_loss=dec.stop_loss,
            max_loss=0.0,
            drawdown_ratio=0.0,
            reason=f"{reason} | {dec.reason}".strip(" |"),
            tape_condition=dec.tape_condition,
            invalidate_condition=dec.invalidate_condition,
            is_holding=(dec.code in self.position_map and self.position_map[dec.code].shares >= 100),
            atr14=self.atr_map.get(dec.code),
            original_stop_loss=dec.stop_loss,
            effective_stop_loss=dec.stop_loss,
            slippage_bps=self.SLIPPAGE_BPS,
            audit=f"reject:{reason}",
        )


def load_portfolio_from_env(env_key: str = "MY_PORTFOLIO_STATE") -> PortfolioState:
    raw = os.getenv(env_key, "").strip()
    if not raw:
        raise ValueError(f"{env_key} 未配置")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"{env_key} 非法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{env_key} 必须是 JSON 对象")

    free_cash = float(data.get("free_cash", 0.0) or 0.0)
    total_equity_raw = data.get("total_equity")
    total_equity = float(total_equity_raw) if total_equity_raw is not None else None

    positions_raw = data.get("positions", []) or []
    if not isinstance(positions_raw, list):
        raise ValueError("positions 必须是数组")

    positions: list[PositionItem] = []
    for idx, item in enumerate(positions_raw, start=1):
        if not isinstance(item, dict):
            print(f"[step4] 跳过非法持仓#{idx}: 非对象")
            continue
        code = str(item.get("code", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            print(f"[step4] 跳过非法持仓#{idx}: code 非6位")
            continue
        positions.append(
            PositionItem(
                code=code,
                name=str(item.get("name", code)).strip() or code,
                cost=float(item.get("cost", 0.0) or 0.0),
                buy_dt=str(item.get("buy_dt", "")).strip(),
                shares=int(item.get("shares", 0) or 0),
                strategy=str(item.get("strategy", "")).strip(),
                stop_loss=float(item.get("stop_loss")) if item.get("stop_loss") is not None else None,
            )
        )

    return PortfolioState(free_cash=free_cash, total_equity=total_equity, positions=positions)


def _build_portfolio_from_dict(data: dict) -> PortfolioState:
    if not isinstance(data, dict):
        raise ValueError("portfolio data 必须是对象")
    free_cash = float(data.get("free_cash", 0.0) or 0.0)
    total_equity_raw = data.get("total_equity")
    total_equity = float(total_equity_raw) if total_equity_raw is not None else None
    positions_raw = data.get("positions", []) or []
    if not isinstance(positions_raw, list):
        raise ValueError("positions 必须是数组")

    positions: list[PositionItem] = []
    for idx, item in enumerate(positions_raw, start=1):
        if not isinstance(item, dict):
            print(f"[step4] 跳过非法持仓#{idx}: 非对象")
            continue
        code = str(item.get("code", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            print(f"[step4] 跳过非法持仓#{idx}: code 非6位")
            continue
        positions.append(
            PositionItem(
                code=code,
                name=str(item.get("name", code)).strip() or code,
                cost=float(item.get("cost", 0.0) or 0.0),
                buy_dt=str(item.get("buy_dt", "")).strip(),
                shares=int(item.get("shares", 0) or 0),
                strategy=str(item.get("strategy", "")).strip(),
                stop_loss=float(item.get("stop_loss")) if item.get("stop_loss") is not None else None,
            )
        )
    return PortfolioState(free_cash=free_cash, total_equity=total_equity, positions=positions)


def load_portfolio_with_fallback() -> tuple[PortfolioState, str]:
    """
    优先读取 Supabase LIVE_PORTFOLIO_ID；失败或未配置时回退 MY_PORTFOLIO_STATE。
    返回：(PortfolioState, source)
    """
    sb_data = load_portfolio_state_from_supabase(LIVE_PORTFOLIO_ID)
    if sb_data:
        try:
            return (_build_portfolio_from_dict(sb_data), f"supabase:{LIVE_PORTFOLIO_ID.lower()}")
        except Exception as e:
            print(f"[step4] Supabase {LIVE_PORTFOLIO_ID} 解析失败，回退 env: {e}")
    p = load_portfolio_from_env()
    return (p, "env:MY_PORTFOLIO_STATE")


def _job_end_calendar_day() -> date:
    now = datetime.now(CN_TZ)
    if now.hour >= MARKET_DATA_READY_HOUR:
        return now.date()
    return (now - timedelta(days=1)).date()


def _latest_trade_date_from_hist(df: pd.DataFrame) -> date | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    if s.empty:
        return None
    return s.iloc[-1].date()


def _append_spot_bar_if_needed(
    code: str,
    df: pd.DataFrame,
    target_trade_date: date,
) -> tuple[pd.DataFrame, bool]:
    """
    当日线落后于 target_trade_date 时，尝试用实时快照补一根当日 bar。
    仅在 target_trade_date == 今日时启用，避免把 T 日快照错误拼到 T-1。
    """
    if not STEP4_ENABLE_SPOT_PATCH or df is None or df.empty:
        return (df, False)
    latest_trade_date = _latest_trade_date_from_hist(df)
    if latest_trade_date is None or latest_trade_date >= target_trade_date:
        return (df, False)
    if target_trade_date != datetime.now(CN_TZ).date():
        return (df, False)

    df_s = df.sort_values("date").reset_index(drop=True)
    last_close_series = pd.to_numeric(df_s.get("close"), errors="coerce").dropna()
    prev_close = float(last_close_series.iloc[-1]) if not last_close_series.empty else None

    for attempt in range(max(STEP4_SPOT_PATCH_RETRIES, 1)):
        snap = fetch_stock_spot_snapshot(
            code,
            force_refresh=attempt > 0,
        )
        close_v = None if not snap else snap.get("close")
        if close_v is None or float(close_v) <= 0:
            if attempt < max(STEP4_SPOT_PATCH_RETRIES, 1) - 1:
                time.sleep(max(STEP4_SPOT_PATCH_SLEEP, 0.0))
            continue

        close_f = float(close_v)
        open_f = float(snap.get("open")) if snap and snap.get("open") is not None else close_f
        high_raw = float(snap.get("high")) if snap and snap.get("high") is not None else close_f
        low_raw = float(snap.get("low")) if snap and snap.get("low") is not None else close_f
        high_f = max(high_raw, open_f, close_f)
        low_f = min(low_raw, open_f, close_f)
        volume_f = float(snap.get("volume")) if snap and snap.get("volume") is not None else 0.0
        amount_f = float(snap.get("amount")) if snap and snap.get("amount") is not None else 0.0
        pct_f = float(snap.get("pct_chg")) if snap and snap.get("pct_chg") is not None else None
        if pct_f is None and prev_close and prev_close > 0:
            pct_f = (close_f - prev_close) / prev_close * 100.0

        new_row = {
            "date": target_trade_date.isoformat(),
            "open": open_f,
            "high": high_f,
            "low": low_f,
            "close": close_f,
            "volume": volume_f,
            "amount": amount_f,
            "pct_chg": pct_f if pct_f is not None else 0.0,
        }
        patched = pd.concat([df_s, pd.DataFrame([new_row])], ignore_index=True)
        patched = patched.sort_values("date").reset_index(drop=True)
        return (patched, True)

    return (df, False)


def _fetch_latest_real_close(code: str, window) -> float | None:
    try:
        raw = _fetch_hist(code, window, "")
        df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
        if ENFORCE_TARGET_TRADE_DATE:
            df, patched = _append_spot_bar_if_needed(code, df, window.end_trade_date)
            if patched:
                print(f"[step4] {code} 实时快照补偿成功（不复权）")
        if ENFORCE_TARGET_TRADE_DATE:
            latest_trade_date = _latest_trade_date_from_hist(df)
            if latest_trade_date != window.end_trade_date:
                return None
        return float(df.iloc[-1]["close"])
    except Exception:
        try:
            raw = _fetch_hist(code, window, "qfq")
            df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
            if ENFORCE_TARGET_TRADE_DATE:
                df, patched = _append_spot_bar_if_needed(code, df, window.end_trade_date)
                if patched:
                    print(f"[step4] {code} 实时快照补偿成功（前复权）")
            if ENFORCE_TARGET_TRADE_DATE:
                latest_trade_date = _latest_trade_date_from_hist(df)
                if latest_trade_date != window.end_trade_date:
                    return None
            return float(df.iloc[-1]["close"])
        except Exception:
            return None


def _calc_atr(df: pd.DataFrame, period: int = STEP4_ATR_PERIOD) -> float | None:
    if df is None or df.empty:
        return None
    need_cols = {"high", "low", "close"}
    if not need_cols.issubset(set(df.columns)):
        return None
    d = df.copy().sort_values("date").reset_index(drop=True)
    high = pd.to_numeric(d["high"], errors="coerce")
    low = pd.to_numeric(d["low"], errors="coerce")
    close = pd.to_numeric(d["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(max(int(period), 2)).mean()
    if atr.dropna().empty:
        return None
    return float(atr.iloc[-1])


def _extract_stock_codes(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for code in re.findall(r"\b\d{6}\b", text):
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _process_one_position(
    pos: PositionItem,
    window,
) -> tuple[str, str, float, float, float | None]:
    """
    处理单个持仓，返回：(meta_block, failure_msg, live_val, latest_close, atr14)
    用于并行化。
    """
    try:
        raw_qfq = _fetch_hist(pos.code, window, "qfq")
        df_qfq = normalize_hist_from_fetch(raw_qfq).sort_values("date").reset_index(drop=True)
        if ENFORCE_TARGET_TRADE_DATE:
            df_qfq, patched = _append_spot_bar_if_needed(
                pos.code,
                df_qfq,
                window.end_trade_date,
            )
            if patched:
                print(f"[step4] {pos.code} 持仓数据已用实时快照补偿")
            latest_trade_date = _latest_trade_date_from_hist(df_qfq)
            if latest_trade_date != window.end_trade_date:
                raise RuntimeError(
                    f"qfq_latest_trade_date={latest_trade_date}, target_trade_date={window.end_trade_date}"
                )
        atr14 = _calc_atr(df_qfq, STEP4_ATR_PERIOD)

        latest_close = _fetch_latest_real_close(pos.code, window)
        failure_msg = ""
        if latest_close is None:
            latest_close = float(df_qfq.iloc[-1]["close"])
            failure_msg = f"{pos.code}:real_close_fallback_to_qfq"

        live_val = latest_close * max(pos.shares, 0)
        pnl_pct = 0.0
        if pos.cost > 0:
            pnl_pct = (latest_close - pos.cost) / pos.cost * 100.0

        stop_info = f"- 当前止损: {pos.stop_loss:.2f}\n" if pos.stop_loss is not None else "- 当前止损: 未设置\n"

        meta = (
            f"### 持仓 {pos.code} {pos.name}\n"
            f"- 成本价: {pos.cost:.2f}\n"
            f"- 最新收盘(不复权优先): {latest_close:.2f}\n"
            f"- 浮盈亏: {pnl_pct:+.2f}%\n"
            f"{stop_info}"
            f"- ATR{STEP4_ATR_PERIOD}: {(f'{atr14:.3f}' if atr14 is not None else '-')}\n"
            f"- 持仓股数: {pos.shares}\n"
            f"- 买入日期: {pos.buy_dt or '-'}\n"
            f"- 原始策略: {pos.strategy or '-'}\n"
        )
        payload = generate_stock_payload(
            stock_code=pos.code,
            stock_name=pos.name,
            wyckoff_tag=pos.strategy or "持仓",
            df=df_qfq,
        )
        return (meta + "\n" + payload, failure_msg, live_val, latest_close, atr14)
    except Exception as e:
        latest_close = _fetch_latest_real_close(pos.code, window)
        if latest_close is not None:
            live_val = latest_close * max(pos.shares, 0)
            fallback_meta = (
                f"### 持仓 {pos.code} {pos.name}\n"
                f"- 成本价: {pos.cost:.2f}\n"
                f"- 最新收盘(快照补偿): {latest_close:.2f}\n"
                f"- 持仓股数: {pos.shares}\n"
                "- 数据状态: 日线未齐，已降级为快照风控。\n"
            )
            return (fallback_meta, f"{pos.code}:{e}", live_val, latest_close, None)
        return ("", f"{pos.code}:{e}", 0.0, 0.0, None)


def _format_position_payload(
    positions: list[PositionItem],
    window,
) -> tuple[str, list[str], float, dict[str, float], dict[str, float]]:
    blocks: list[str] = []
    failures: list[str] = []
    live_value_sum = 0.0
    latest_close_map: dict[str, float] = {}
    atr_map: dict[str, float] = {}

    if not positions:
        return ("", [], 0.0, {}, {})

    with ThreadPoolExecutor(max_workers=STEP4_MAX_WORKERS) as executor:
        futures = {executor.submit(_process_one_position, pos, window): pos for pos in positions}
        for future in as_completed(futures):
            pos = futures[future]
            meta_block, fail_msg, val, close, atr = future.result()
            if fail_msg:
                failures.append(fail_msg)
            if meta_block:
                blocks.append(meta_block)
                live_value_sum += val
                latest_close_map[pos.code] = close
                if atr is not None:
                    atr_map[pos.code] = atr

    return ("\n\n".join(blocks), failures, live_value_sum, latest_close_map, atr_map)


def _extract_json_block(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start:end + 1]
    return raw


def _parse_bool_like(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v or "").strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off", ""}:
        return False
    return False


def _parse_confidence_like(v: object) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        if s.endswith("%"):
            pct = float(s[:-1].strip())
            if 0.0 <= pct <= 100.0:
                return pct / 100.0
            return None
        x = float(s)
        if 0.0 <= x <= 1.0:
            return x
        if 1.0 < x <= 100.0:
            return x / 100.0
    except Exception:
        return None
    return None


def _parse_decisions(
    raw_text: str,
    allowed_codes: set[str],
    name_map: dict[str, str],
) -> tuple[str, list[DecisionItem], str | None]:
    try:
        data = json.loads(_extract_json_block(raw_text))
    except Exception as e:
        return ("", [], f"json_parse_failed: {e}")

    market_view = str(data.get("market_view", "")).strip()
    raw_decisions = data.get("decisions", []) or []
    if not isinstance(raw_decisions, list):
        return (market_view, [], "decisions_not_list")

    valid_actions = {"EXIT", "TRIM", "HOLD", "PROBE", "ATTACK"}
    out: list[DecisionItem] = []
    for item in raw_decisions:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        action = str(item.get("action", "")).strip().upper()
        if not re.fullmatch(r"\d{6}", code):
            continue
        if code not in allowed_codes:
            continue
        if action not in valid_actions:
            continue

        entry_zone_min = None
        entry_zone_max = None
        zone = item.get("entry_zone")
        if isinstance(zone, list) and len(zone) >= 2:
            try:
                z1 = float(zone[0])
                z2 = float(zone[1])
                entry_zone_min = min(z1, z2)
                entry_zone_max = max(z1, z2)
            except Exception:
                entry_zone_min = None
                entry_zone_max = None

        stop_loss = None
        if item.get("stop_loss") is not None:
            try:
                stop_loss = float(item.get("stop_loss"))
            except Exception:
                stop_loss = None

        trim_ratio = None
        if item.get("trim_ratio") is not None:
            try:
                trim_ratio = float(item.get("trim_ratio"))
            except Exception:
                trim_ratio = None

        confidence = _parse_confidence_like(item.get("confidence"))

        if stop_loss is not None and stop_loss <= 0:
            stop_loss = None

        out.append(
            DecisionItem(
                code=code,
                name=str(item.get("name", "")).strip() or name_map.get(code, code),
                action=action,
                entry_zone_min=entry_zone_min,
                entry_zone_max=entry_zone_max,
                stop_loss=stop_loss,
                trim_ratio=trim_ratio,
                tape_condition=str(item.get("tape_condition", "")).strip(),
                invalidate_condition=str(item.get("invalidate_condition", "")).strip(),
                is_add_on=_parse_bool_like(item.get("is_add_on", False)),
                reason=str(item.get("reason", "")).strip(),
                confidence=confidence,
            )
        )
    return (market_view, out, None)


def _dump_model_input(model: str, system_prompt: str, user_message: str, symbols: list[str]) -> None:
    if not DEBUG_MODEL_IO:
        return
    logs_dir = os.getenv("LOGS_DIR", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, f"step4_model_input_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    body = (
        f"[step4] model={model}\n"
        f"[step4] symbol_count={len(symbols)}\n"
        f"[step4] symbols={','.join(symbols)}\n"
        f"[step4] system_prompt_len={len(system_prompt)}\n"
        f"[step4] user_message_len={len(user_message)}\n"
    )
    if DEBUG_MODEL_IO_FULL:
        body += (
            "\n===== SYSTEM PROMPT =====\n"
            + system_prompt
            + "\n\n===== USER MESSAGE =====\n"
            + user_message
            + "\n"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[step4] 模型输入已落盘: {path}")


def _split_telegram_message(content: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    cur = ""
    for part in content.split("\n\n"):
        candidate = part if not cur else f"{cur}\n\n{part}"
        if len(candidate) <= max_len:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
        cur = ""
        if len(part) <= max_len:
            cur = part
            continue
        start = 0
        while start < len(part):
            chunks.append(part[start:start + max_len])
            start += max_len
    if cur:
        chunks.append(cur)
    return chunks


_TG_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_markdownv2(text: str) -> str:
    """转义 Telegram MarkdownV2 所有保留字符，使原文字面量安全显示。"""
    return _TG_MDV2_SPECIAL.sub(r"\\\1", text or "")


def send_to_telegram(message_text: str) -> bool:
    import requests

    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[step4] TG_BOT_TOKEN/TG_CHAT_ID 未配置，跳过 Telegram 推送")
        return False

    proxy_url = os.getenv("PROXY_URL", "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    escaped = _escape_markdownv2(message_text)
    chunks = _split_telegram_message(escaped)
    for idx, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": chat_id,
            "text": chunk if len(chunks) == 1 else f"\\[{idx}/{len(chunks)}\\]\n{chunk}",
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15, proxies=proxies)
            if resp.status_code != 200:
                print(f"[step4] Telegram 推送失败: status={resp.status_code}, body={resp.text[:200]}")
                return False
        except Exception as e:
            print(f"[step4] Telegram 推送异常: {e}")
            return False
    return True


def _render_trade_ticket(
    model: str,
    market_view: str,
    total_equity: float,
    free_cash_before: float,
    free_cash_after: float,
    tickets: list[ExecutionTicket],
) -> str:
    now_str = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    sells = [t for t in tickets if t.status == "APPROVED" and t.action in {"EXIT", "TRIM"}]
    holds = [t for t in tickets if t.status == "APPROVED" and t.action == "HOLD" and t.is_holding]
    approved_buy = [t for t in tickets if t.status == "APPROVED" and t.action in {"PROBE", "ATTACK"}]
    blocked = [t for t in tickets if t.status != "APPROVED"]

    def _first_sentence(s: str) -> str:
        s = (s or "").strip()
        if not s:
            return "-"
        parts = re.split(r"[。；;\n]+", s, maxsplit=1)
        return parts[0].strip() if parts and parts[0].strip() else s

    def _fmt_stop(v: float | None) -> str:
        return "-" if v is None else f"{v:.2f}"

    lines = [
        "🚨 **Alpha-OMS 交易执行工单**",
        f"📅 日期：{now_str} | 净权益：{total_equity:.2f} | 当前可用现金：{free_cash_before:.2f}",
        f"🤖 模型：{model}",
    ]
    if market_view:
        lines.append(f"📌 市场视图：{market_view}")
    lines.append("")

    lines.append(f"🟥 **[卖出动作 SELL]** ({len(sells)})")
    if not sells:
        lines.append("* 无")
    else:
        for t in sells:
            lines.append(f"* **🟥 {t.action}** | `{t.code} {t.name}`")
            lines.append(f"* 执行：{t.shares} 股 | 回笼：{t.amount:.2f} 元 | 止损：{_fmt_stop(t.stop_loss)}")
            if t.atr14 is not None:
                lines.append(f"* 风控：ATR{STEP4_ATR_PERIOD}={t.atr14:.3f} | 滑点={t.slippage_bps * 100:.2f}%")
            lines.append(f"* 触发：{_first_sentence(t.tape_condition)}")
            lines.append(f"* 失效：{_first_sentence(t.invalidate_condition)}")
            lines.append(f"* 理由：{_first_sentence(t.reason)}")
            lines.append("")

    lines.append(f"🟨 **[持有动作 HOLD]** ({len(holds)})")
    if not holds:
        lines.append("* 无")
    else:
        for t in holds:
            lines.append(f"* **🟨 HOLD** | `{t.code} {t.name}` | 止损：{_fmt_stop(t.stop_loss)}")
            if t.atr14 is not None:
                lines.append(f"* 风控：ATR{STEP4_ATR_PERIOD}={t.atr14:.3f} | 动态止损={_fmt_stop(t.effective_stop_loss)}")
            lines.append(f"* 观察：{_first_sentence(t.reason)}")
            lines.append(f"* 触发：{_first_sentence(t.tape_condition)}")
            lines.append(f"* 失效：{_first_sentence(t.invalidate_condition)}")
            lines.append("")
    lines.append("")

    lines.append(f"🟩 **[买入动作 BUY - APPROVED]** ({len(approved_buy)})")
    if not approved_buy:
        lines.append("* 无")
    else:
        for t in approved_buy:
            lines.append(f"* **🟩 {t.action}** | `{t.code} {t.name}`")
            lines.append(
                f"* 下单：{t.shares} 股 | 占用：{t.amount:.2f} 元 | 参考价："
                f"{('-' if t.price_hint is None else f'{t.price_hint:.2f}')}"
            )
            lines.append(
                f"* 风险：止损 {_fmt_stop(t.stop_loss)} | 最大回撤 {t.max_loss:.2f} 元 ({t.drawdown_ratio * 100:.2f}%)"
                f" | 滑点={t.slippage_bps * 100:.2f}%"
            )
            if t.atr14 is not None:
                lines.append(f"* ATR：ATR{STEP4_ATR_PERIOD}={t.atr14:.3f}")
            if t.tape_condition:
                lines.append(f"* 确认：{_first_sentence(t.tape_condition)}")
            if t.invalidate_condition:
                lines.append(f"* 熔断：{_first_sentence(t.invalidate_condition)}")
            if t.reason:
                lines.append(f"* 理由：{_first_sentence(t.reason)}")
            lines.append("")
    lines.append("")

    lines.append(f"⬛ **[风控拒单 NO_TRADE]** ({len(blocked)})")
    if not blocked:
        lines.append("* 无")
    else:
        for t in blocked:
            lines.append(f"* **⬛ NO_TRADE** | `{t.code} {t.name}` | 原动作：{t.action}")
            lines.append(f"* 原因：{_first_sentence(t.reason)}")
            if t.audit:
                lines.append(f"* 审计：{_first_sentence(t.audit)}")
            lines.append("")
    lines.append("")
    lines.append(f"💰 执行后可用现金：{free_cash_after:.2f}")
    return "\n".join(lines)


def run(
    external_report: str,
    benchmark_context: dict | None,
    api_key: str,
    model: str,
) -> tuple[bool, str]:
    if not api_key or not api_key.strip():
        return (False, "missing_api_key")

    try:
        portfolio, portfolio_source = load_portfolio_with_fallback()
    except Exception as e:
        print(f"[step4] 持仓读取失败: {e}")
        return (True, "skipped_invalid_portfolio")
    print(f"[step4] 持仓来源: {portfolio_source}")

    if not os.getenv("TG_BOT_TOKEN", "").strip() or not os.getenv("TG_CHAT_ID", "").strip():
        print("[step4] TG_BOT_TOKEN/TG_CHAT_ID 未配置，跳过 Step4 推送")
        return (True, "skipped_telegram_unconfigured")

    trade_date = _job_end_calendar_day().strftime("%Y-%m-%d")
    if check_daily_run_exists(LIVE_PORTFOLIO_ID, trade_date):
        print(f"[step4] 幂等性检查: {LIVE_PORTFOLIO_ID} {trade_date} 已运行过，跳过。")
        return (True, "skipped_idempotency")

    end_day = _job_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)
    positions_payload, position_failures, live_value, latest_price_map, atr_map = _format_position_payload(
        portfolio.positions,
        window,
    )
    total_equity = portfolio.total_equity if portfolio.total_equity is not None else (portfolio.free_cash + live_value)

    position_codes = [p.code for p in portfolio.positions]
    external_codes = _extract_stock_codes(external_report)
    candidate_codes = [c for c in external_codes if c not in set(position_codes)]
    allowed_codes = set(position_codes + candidate_codes)
    name_map = {p.code: p.name for p in portfolio.positions}

    benchmark_text = ""
    if benchmark_context:
        benchmark_text = (
            "[宏观水温]\n"
            f"regime={benchmark_context.get('regime')}, close={benchmark_context.get('close')}, "
            f"ma50={benchmark_context.get('ma50')}, ma200={benchmark_context.get('ma200')}, "
            f"recent3={benchmark_context.get('recent3_pct')}, cum3={benchmark_context.get('recent3_cum_pct')}\n\n"
        )

    user_message = (
        benchmark_text
        + "[账户状态]\n"
        + f"free_cash={portfolio.free_cash:.2f}\n"
        + f"total_equity={float(total_equity):.2f}\n"
        + f"position_count={len(portfolio.positions)}\n"
        + f"allowed_codes={','.join(sorted(allowed_codes))}\n\n"
        + "[内部持仓量价切片]\n"
        + (positions_payload if positions_payload else "当前无持仓，仅现金。")
        + "\n\n[外部候选摘要]\n"
        + (external_report.strip() if external_report and external_report.strip() else "无")
    )
    if position_failures:
        user_message += "\n\n[数据注意]\n" + "\n".join(f"- {x}" for x in position_failures)

    _dump_model_input(
        model=model,
        system_prompt=PRIVATE_PM_DECISION_JSON_PROMPT,
        user_message=user_message,
        symbols=sorted(allowed_codes),
    )

    try:
        raw = call_llm(
            provider="gemini",
            model=model,
            api_key=api_key,
            system_prompt=PRIVATE_PM_DECISION_JSON_PROMPT,
            user_message=user_message,
            timeout=300,
            max_output_tokens=STEP4_MAX_OUTPUT_TOKENS,
        )
    except Exception as e:
        print(f"[step4] 模型调用失败: {e}")
        return (False, "llm_failed")

    market_view, decisions, parse_err = _parse_decisions(raw, allowed_codes, name_map)
    if parse_err:
        print(f"[step4] 决策 JSON 解析失败: {parse_err}")
        return (False, "llm_failed")
    if not decisions:
        print("[step4] 模型未产出有效决策，跳过")
        return (True, "skipped_no_decisions")

    # 确保所有持仓至少有一个动作，避免遗漏
    mentioned_codes = {d.code for d in decisions}
    for p in portfolio.positions:
        if p.code in mentioned_codes:
            continue
        decisions.append(
            DecisionItem(
                code=p.code,
                name=p.name,
                action="HOLD",
                entry_zone_min=None,
                entry_zone_max=None,
                stop_loss=None,
                trim_ratio=None,
                tape_condition="默认观察",
                invalidate_condition="",
                is_add_on=True,
                reason="模型未给出动作，系统默认 HOLD",
                confidence=None,
            )
        )

    # 补齐候选最新价
    def _fetch_candidate_data(d_code):
        atr_v = None
        px = None
        try:
            raw_qfq = _fetch_hist(d_code, window, "qfq")
            df_qfq = normalize_hist_from_fetch(raw_qfq).sort_values("date").reset_index(drop=True)
            if ENFORCE_TARGET_TRADE_DATE:
                df_qfq, patched = _append_spot_bar_if_needed(
                    d_code,
                    df_qfq,
                    window.end_trade_date,
                )
                if patched:
                    print(f"[step4] {d_code} 候选数据已用实时快照补偿")
                latest_trade_date = _latest_trade_date_from_hist(df_qfq)
                if latest_trade_date == window.end_trade_date:
                    atr_v = _calc_atr(df_qfq, STEP4_ATR_PERIOD)
            else:
                atr_v = _calc_atr(df_qfq, STEP4_ATR_PERIOD)
        except Exception as e:
            print(f"[step4] {d_code} ATR 计算异常: {e}")
        px = _fetch_latest_real_close(d_code, window)
        return (d_code, atr_v, px)

    missing_codes = [d.code for d in decisions if d.code not in latest_price_map]
    if missing_codes:
        with ThreadPoolExecutor(max_workers=STEP4_MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_candidate_data, c): c for c in missing_codes}
            for future in as_completed(futures):
                c, atr_v, px = future.result()
                if atr_v is not None:
                    atr_map[c] = atr_v
                if px is not None:
                    latest_price_map[c] = px

    engine = WyckoffOrderEngine(
        total_equity=float(total_equity),
        free_cash=portfolio.free_cash,
        position_map={p.code: p for p in portfolio.positions},
        latest_price_map=latest_price_map,
        atr_map=atr_map,
    )
    tickets, free_cash_after = engine.process(decisions)

    # 状态回写：更新持仓止损价到 Supabase
    updates = []
    for t in tickets:
        # 只更新已有持仓且有效止损价有变化的
        if t.is_holding and t.effective_stop_loss is not None:
            updates.append({"code": t.code, "stop_loss": t.effective_stop_loss})
    if updates:
        if update_position_stops(LIVE_PORTFOLIO_ID, updates):
            print(f"[step4] 已更新 {len(updates)} 个持仓的止损价")
        else:
            print("[step4] 持仓止损价更新失败")

    run_id = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S") + "_" + str(uuid4())[:8]
    ticket_rows = [
        {
            "code": t.code,
            "name": t.name,
            "action": t.action,
            "status": t.status,
            "shares": t.shares,
            "price_hint": t.price_hint,
            "amount": t.amount,
            "stop_loss": t.stop_loss,
            "max_loss": t.max_loss,
            "drawdown_ratio": t.drawdown_ratio,
            "reason": (t.reason + (f" | audit={t.audit}" if t.audit else "")).strip(),
            "tape_condition": t.tape_condition,
            "invalidate_condition": t.invalidate_condition,
        }
        for t in tickets
    ]
    for t in tickets:
        if t.status != "APPROVED":
            print(f"[step4][reject_audit] code={t.code}, action={t.action}, reason={t.reason}, audit={t.audit}")
    reject_cnt = sum(1 for t in tickets if t.status != "APPROVED")
    if reject_cnt:
        print(f"[step4][reject_audit] summary: rejected={reject_cnt}, total={len(tickets)}")

    report = _render_trade_ticket(
        model=model,
        market_view=market_view,
        total_equity=float(total_equity),
        free_cash_before=portfolio.free_cash,
        free_cash_after=free_cash_after,
        tickets=tickets,
    )
    sent = send_to_telegram(report)
    if not sent:
        return (False, "telegram_failed")

    # Telegram 发送成功后才写入幂等标记，保证失败可重试
    if save_ai_trade_orders(
        run_id=run_id,
        portfolio_id=LIVE_PORTFOLIO_ID,
        model=model,
        trade_date=trade_date,
        market_view=market_view,
        orders=ticket_rows,
    ):
        print(f"[step4] 已写入 AI 订单记录: run_id={run_id}, count={len(ticket_rows)}")
    else:
        print("[step4] AI 订单记录写入失败（已忽略，不阻断流程）")

    positions_value = max(float(total_equity) - float(portfolio.free_cash), 0.0)
    if upsert_daily_nav(
        portfolio_id=LIVE_PORTFOLIO_ID,
        trade_date=trade_date,
        free_cash=portfolio.free_cash,
        total_equity=float(total_equity),
        positions_value=positions_value,
    ):
        print(f"[step4] 已写入 USER_LIVE 日净值快照: {trade_date}")
    else:
        print("[step4] USER_LIVE 日净值快照写入失败（已忽略）")

    print(f"[step4] 交易工单发送成功: decisions={len(decisions)}, tickets={len(tickets)}, model={model}")
    return (True, "ok")

