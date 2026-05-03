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
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import uuid4

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from functools import partial

from core.batch_report import generate_stock_payload
from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_for_llm
from core.prompts import PRIVATE_PM_DECISION_JSON_PROMPT
from core.wyckoff_engine import FunnelConfig, normalize_hist_from_fetch
from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window
from integrations.llm_client import call_llm
from integrations.supabase_market_signal import compose_market_banner, load_market_signal_daily
from integrations.supabase_portfolio import (
    cancel_trade_orders,
    check_daily_run_exists,
    compute_portfolio_state_signature,
    save_ai_trade_orders,
    update_position_stops,
    upsert_daily_nav,
)
from integrations.supabase_portfolio import (
    load_portfolio_state as load_portfolio_state_from_supabase,
)
from tools.data_fetcher import (
    append_spot_bar_if_needed,
)
from tools.data_fetcher import (
    latest_trade_date_from_hist as _latest_trade_date_from_hist,
)
from tools.report_builder import _extract_json_block
from utils.notify import send_to_telegram
from utils.trading_clock import CN_TZ, resolve_end_calendar_day

_append_spot_bar_if_needed = partial(
    append_spot_bar_if_needed,
    env_prefix="STEP4",
    sleep_default=0.3,
    zero_fallback=True,
)

TRADING_DAYS = 320
TELEGRAM_MAX_LEN = 3900
ENFORCE_TARGET_TRADE_DATE = False
from tools.debug_io import dump_model_input as _dump_model_input_shared

STEP4_MAX_OUTPUT_TOKENS = 8192
STEP4_ATR_PERIOD = int(os.getenv("STEP4_ATR_PERIOD", "14"))
STEP4_ATR_MULTIPLIER = float(os.getenv("STEP4_ATR_MULTIPLIER", "2.0"))
STEP4_MAX_WORKERS = int(os.getenv("STEP4_MAX_WORKERS", "8"))
STEP4_BUY_HARD_STOP_ENABLED = os.getenv("STEP4_BUY_HARD_STOP_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STEP4_BUY_HARD_STOP_PCT = max(
    float(os.getenv("STEP4_BUY_HARD_STOP_PCT", "9.0")),
    0.0,
)
STEP4_BUY_STOP_MODE = os.getenv("STEP4_BUY_STOP_MODE", "floor").strip().lower()
if STEP4_BUY_STOP_MODE not in {"fixed", "floor"}:
    STEP4_BUY_STOP_MODE = "floor"
STEP4_ATR_SLIPPAGE_FACTOR = float(os.getenv("STEP4_ATR_SLIPPAGE_FACTOR", "0.25"))
STEP4_PROBE_BUDGET_LIMIT = min(max(float(os.getenv("STEP4_PROBE_BUDGET_LIMIT", "0.10")), 0.0), 1.0)
STEP4_ATTACK_BUDGET_LIMIT = min(max(float(os.getenv("STEP4_ATTACK_BUDGET_LIMIT", "0.20")), 0.0), 1.0)
STEP4_BUY_BLOCK_REGIMES = {
    x.strip().upper()
    for x in os.getenv("STEP4_BUY_BLOCK_REGIMES", "CRASH,BLACK_SWAN").split(",")
    if x.strip() and x.strip().upper() != "COOLDOWN"
}
STEP4_CHASE_GAP_PCT_MIN = max(float(os.getenv("STEP4_CHASE_GAP_PCT_MIN", "1.2")), 0.2)
STEP4_CHASE_GAP_PCT_MAX = max(float(os.getenv("STEP4_CHASE_GAP_PCT_MAX", "5.5")), STEP4_CHASE_GAP_PCT_MIN)
STEP4_CHASE_ATR_MULT_MIN = max(float(os.getenv("STEP4_CHASE_ATR_MULT_MIN", "0.8")), 0.1)
STEP4_CHASE_ATR_MULT_MAX = max(float(os.getenv("STEP4_CHASE_ATR_MULT_MAX", "2.4")), STEP4_CHASE_ATR_MULT_MIN)

# --- OMS 防追高与滑点保护配置 ---
STEP4_MAX_GAP_UP_PCT = float(os.getenv("STEP4_MAX_GAP_UP_PCT", "3.0"))  # 最大允许跳空/追高幅度(%)
STEP4_MAX_GAP_UP_ATR_MULT = float(os.getenv("STEP4_MAX_GAP_UP_ATR_MULT", "1.5"))  # 最大允许追高 ATR 倍数
STEP4_MAX_NEW_BUYS_RISK_ON = max(int(os.getenv("STEP4_MAX_NEW_BUYS_RISK_ON", "2")), 0)
STEP4_MAX_NEW_BUYS_CAUTION = max(int(os.getenv("STEP4_MAX_NEW_BUYS_CAUTION", "1")), 0)
STEP4_MAX_NEW_BUYS_NEUTRAL = max(int(os.getenv("STEP4_MAX_NEW_BUYS_NEUTRAL", "1")), 0)
STEP4_MAX_NEW_BUYS_RISK_OFF = max(int(os.getenv("STEP4_MAX_NEW_BUYS_RISK_OFF", "0")), 0)

BENCHMARK_REGIME_SEVERITY = {
    "RISK_ON": 0,
    "NEUTRAL": 1,
    "RISK_OFF": 3,
    "CRASH": 4,
    "BLACK_SWAN": 5,
}
PREMARKET_REGIME_SEVERITY = {
    "NORMAL": 0,
    "CAUTION": 2,
    "RISK_OFF": 3,
    "BLACK_SWAN": 5,
}
EFFECTIVE_REGIME_BY_SEVERITY = {
    0: "RISK_ON",
    1: "NEUTRAL",
    2: "CAUTION",
    3: "RISK_OFF",
    4: "CRASH",
    5: "BLACK_SWAN",
}


@dataclass
class PositionItem:
    code: str
    name: str
    cost: float
    buy_dt: str
    shares: int
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
    funnel_score: float | None = None
    wyckoff_track: str = ""
    wyckoff_stage: str = ""
    wyckoff_tag: str = ""
    source_type: str = ""


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
    max_entry_price: float | None = None
    chase_profile: str = ""
    wyckoff_context: str = ""


@dataclass(frozen=True)
class CandidateMeta:
    code: str
    name: str
    tag: str = ""
    track: str = ""
    stage: str = ""
    industry: str = ""
    sector_state: str = ""
    sector_state_code: str = ""
    sector_note: str = ""
    funnel_score: float | None = None
    exit_signal: str = ""
    exit_price: float | None = None
    exit_reason: str = ""
    source_type: str = ""


def _clean_text(raw: object) -> str:
    return str(raw or "").strip()


def _parse_float_like(raw: object) -> float | None:
    try:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        return float(text)
    except Exception:
        return None


def _normalize_benchmark_regime(raw: object) -> str:
    regime = _clean_text(raw).upper()
    if regime in BENCHMARK_REGIME_SEVERITY:
        return regime
    return "NEUTRAL"


def _normalize_premarket_regime(raw: object) -> str:
    regime = _clean_text(raw).upper()
    if regime in PREMARKET_REGIME_SEVERITY:
        return regime
    return "NORMAL"


def _resolve_effective_market_regime(benchmark_regime: object, premarket_regime: object) -> str:
    benchmark_norm = _normalize_benchmark_regime(benchmark_regime)
    premarket_norm = _normalize_premarket_regime(premarket_regime)
    severity = max(
        BENCHMARK_REGIME_SEVERITY.get(benchmark_norm, 1),
        PREMARKET_REGIME_SEVERITY.get(premarket_norm, 0),
    )
    return EFFECTIVE_REGIME_BY_SEVERITY.get(severity, benchmark_norm)


def _load_market_signal_for_trade_date(trade_date: str) -> dict[str, object] | None:
    try:
        return load_market_signal_daily(trade_date)
    except Exception as e:
        print(f"[step4] 读取 market_signal_daily 失败: trade_date={trade_date}, err={e}")
        return None


def _build_market_guardrail(
    *,
    trade_date: str,
    benchmark_context: dict | None,
    market_signal_row: dict[str, object] | None,
) -> tuple[str, str, str]:
    row = dict(market_signal_row or {})
    benchmark_regime = _normalize_benchmark_regime(
        row.get("benchmark_regime") or (benchmark_context or {}).get("regime")
    )
    premarket_regime = _normalize_premarket_regime(row.get("premarket_regime"))
    effective_regime = _resolve_effective_market_regime(benchmark_regime, premarket_regime)

    if benchmark_context:
        row.update(
            {
                "benchmark_regime": benchmark_regime,
                "main_index_close": benchmark_context.get("close"),
                "main_index_ma50": benchmark_context.get("ma50"),
                "main_index_ma200": benchmark_context.get("ma200"),
                "main_index_recent3_cum_pct": benchmark_context.get("recent3_cum_pct"),
                "main_index_today_pct": benchmark_context.get("main_today_pct"),
                "smallcap_close": benchmark_context.get("smallcap_close"),
                "smallcap_recent3_cum_pct": benchmark_context.get("smallcap_recent3_cum_pct"),
            }
        )
    row["premarket_regime"] = premarket_regime

    banner = compose_market_banner(row)
    panic_reasons = [
        str(x).strip() for x in ((benchmark_context or {}).get("panic_reasons", []) or []) if str(x).strip()
    ]
    premarket_reasons = [str(x).strip() for x in (row.get("premarket_reasons", []) or []) if str(x).strip()]

    lines = [
        "[全局风控]",
        f"trade_date={trade_date}, effective_regime={effective_regime}, "
        f"benchmark_regime={benchmark_regime}, premarket_regime={premarket_regime}",
    ]
    if benchmark_context:
        lines.append(
            f"benchmark_close={benchmark_context.get('close')}, ma50={benchmark_context.get('ma50')}, "
            f"ma200={benchmark_context.get('ma200')}, recent3={benchmark_context.get('recent3_pct')}, "
            f"cum3={benchmark_context.get('recent3_cum_pct')}, smallcap_today={benchmark_context.get('smallcap_today_pct')}"
        )
    if effective_regime in STEP4_BUY_BLOCK_REGIMES:
        lines.append("⚠️ 全局风控一票否决：OMS 将强制拦截全部买入动作（仅允许 HOLD/TRIM/EXIT）。")
    elif premarket_regime == "CAUTION":
        lines.append("⚠️ 盘前情绪扰动已触发：OMS 会自动收紧追价阈值并优先防守。")
    if panic_reasons:
        lines.append("panic_reasons=" + " | ".join(panic_reasons))
    if premarket_reasons:
        lines.append("premarket_reasons=" + " | ".join(premarket_reasons))
    lines.append("")

    posture_name = _clean_text(banner.get("market_posture_name"))
    action_phrase = _clean_text(banner.get("action_phrase"))
    system_market_view = f"系统风控：{effective_regime}"
    if posture_name:
        system_market_view += f" / {posture_name}"
    view_parts = [f"收盘={benchmark_regime}"]
    if premarket_regime != "NORMAL":
        view_parts.append(f"盘前={premarket_regime}")
    if action_phrase:
        view_parts.append(action_phrase)
    if view_parts:
        system_market_view += " | " + "；".join(view_parts)

    return (effective_regime, "\n".join(lines), system_market_view)


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    text_norm = text.lower()
    for keyword in keywords:
        if keyword.lower() in text_norm:
            return True
    return False


def _normalize_track(raw: object) -> str:
    text = _clean_text(raw)
    text_norm = text.lower()
    if text_norm == "trend":
        return "Trend"
    if text_norm == "accum":
        return "Accum"
    if _contains_keyword(text, ("markup", "trend", "主升", "点火", "sos", "突破")):
        return "Trend"
    if _contains_keyword(text, ("accum", "spring", "lps", "潜伏", "吸筹", "地量", "护盘")):
        return "Accum"
    return ""


def _normalize_stage(raw: object) -> str:
    text = _clean_text(raw)
    text_norm = text.lower()
    if "markup" in text_norm:
        return "Markup"
    for stage in ("Accum_A", "Accum_B", "Accum_C"):
        if stage.lower() in text_norm:
            return stage
    return ""


def _infer_track_from_text(raw: object) -> str:
    return _normalize_track(raw)


def _infer_stage_from_text(raw: object) -> str:
    return _normalize_stage(raw)


def _build_candidate_meta_map(
    candidate_meta: list[dict] | None,
    positions: list[PositionItem],
) -> dict[str, CandidateMeta]:
    meta_map: dict[str, CandidateMeta] = {}

    for item in candidate_meta or []:
        if not isinstance(item, dict):
            continue
        code = _clean_text(item.get("code"))
        if not re.fullmatch(r"\d{6}", code):
            continue
        meta_map[code] = CandidateMeta(
            code=code,
            name=_clean_text(item.get("name")) or code,
            tag=_clean_text(item.get("tag")),
            track=_normalize_track(item.get("track")),
            stage=_normalize_stage(item.get("stage")),
            industry=_clean_text(item.get("industry")),
            sector_state=_clean_text(item.get("sector_state")),
            sector_state_code=_clean_text(item.get("sector_state_code")),
            sector_note=_clean_text(item.get("sector_note")),
            funnel_score=_parse_float_like(item.get("score")),
            exit_signal=_clean_text(item.get("exit_signal")),
            exit_price=_parse_float_like(item.get("exit_price")),
            exit_reason=_clean_text(item.get("exit_reason")),
            source_type="external",
        )

    for pos in positions:
        existing = meta_map.get(pos.code)
        meta_map[pos.code] = CandidateMeta(
            code=pos.code,
            name=pos.name or pos.code,
            tag=(existing.tag if existing and existing.tag else "持仓"),
            track=(existing.track if existing and existing.track else None),
            stage=(existing.stage if existing and existing.stage else None),
            source_type="holding",
        )

    return meta_map


def _attach_candidate_meta(
    decisions: list[DecisionItem],
    meta_map: dict[str, CandidateMeta],
) -> list[DecisionItem]:
    out: list[DecisionItem] = []
    for dec in decisions:
        meta = meta_map.get(dec.code)
        if not meta:
            out.append(dec)
            continue
        out.append(
            replace(
                dec,
                wyckoff_track=meta.track or dec.wyckoff_track,
                wyckoff_stage=meta.stage or dec.wyckoff_stage,
                wyckoff_tag=meta.tag or dec.wyckoff_tag,
                funnel_score=meta.funnel_score if dec.funnel_score is None else dec.funnel_score,
                source_type=meta.source_type or dec.source_type,
            )
        )
    return out


def _format_wyckoff_context(track: str, stage: str, tag: str) -> str:
    parts = [x for x in [_clean_text(track), _clean_text(stage), _clean_text(tag)] if x]
    return " | ".join(parts)


def _resolve_chase_limits(dec: DecisionItem, market_regime: str) -> tuple[float, float, str, str]:
    regime = _clean_text(market_regime).upper() or "NEUTRAL"
    track = _normalize_track(dec.wyckoff_track) or _infer_track_from_text(dec.wyckoff_tag)
    stage = _normalize_stage(dec.wyckoff_stage) or _infer_stage_from_text(dec.wyckoff_tag)
    tag = _clean_text(dec.wyckoff_tag)

    pct_limit = float(max(STEP4_MAX_GAP_UP_PCT, 0.0))
    atr_limit = float(max(STEP4_MAX_GAP_UP_ATR_MULT, 0.0))
    profile_parts = [regime]

    regime_mult = {
        "RISK_ON": 1.10,
        "NEUTRAL": 1.00,
        "CAUTION": 0.92,
        "PANIC_REPAIR": 0.95,
        "RISK_OFF": 0.85,
        "CRASH": 0.70,
        "BLACK_SWAN": 0.60,
    }.get(regime, 1.00)
    pct_limit *= regime_mult
    atr_limit *= regime_mult

    if track == "Trend":
        pct_limit *= 1.12
        atr_limit *= 1.12
        profile_parts.append("Trend")
    elif track == "Accum":
        pct_limit *= 0.82
        atr_limit *= 0.82
        profile_parts.append("Accum")
    else:
        profile_parts.append("Unclassified")

    if stage == "Markup" or _contains_keyword(tag, ("sos", "点火", "突破", "主升")):
        pct_limit *= 1.10
        atr_limit *= 1.15
        profile_parts.append("Momentum")
    elif stage == "Accum_C" or _contains_keyword(tag, ("spring", "lps", "终极震仓", "缩量回踩")):
        pct_limit *= 0.90
        atr_limit *= 0.90
        profile_parts.append("Trigger")
    elif stage in {"Accum_A", "Accum_B"}:
        pct_limit *= 0.82
        atr_limit *= 0.85
        profile_parts.append(stage)
    elif stage:
        profile_parts.append(stage)

    if dec.is_add_on:
        pct_limit *= 0.95
        atr_limit *= 0.95
        profile_parts.append("AddOn")

    pct_limit = min(max(pct_limit, STEP4_CHASE_GAP_PCT_MIN), STEP4_CHASE_GAP_PCT_MAX)
    atr_limit = min(max(atr_limit, STEP4_CHASE_ATR_MULT_MIN), STEP4_CHASE_ATR_MULT_MAX)
    context = _format_wyckoff_context(track, stage, tag)
    return (pct_limit, atr_limit, "/".join(profile_parts), context)


class WyckoffOrderEngine:
    """
    确定性订单执行引擎（OMS）
    """

    SLIPPAGE_BPS = 0.005
    RISK_LIMITS = {
        "PROBE": 0.008,  # 0.8%
        "ATTACK": 0.012,  # 1.2%
    }
    BUDGET_LIMITS = {
        "PROBE": STEP4_PROBE_BUDGET_LIMIT,
        "ATTACK": STEP4_ATTACK_BUDGET_LIMIT,
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
        market_regime: str | None = None,
    ) -> None:
        self.total_equity = float(max(total_equity, 0.0))
        self.free_cash = float(max(free_cash, 0.0))
        self.position_map = position_map
        self.latest_price_map = latest_price_map
        self.atr_map = atr_map or {}
        self.market_regime = str(market_regime or "NEUTRAL").strip().upper()

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

        if pos and pos.stop_loss is not None and pos.stop_loss > 0:
            if effective_stop_loss is None:
                effective_stop_loss = pos.stop_loss
                audit_parts.append(f"inherit_pos_stop({pos.stop_loss:.2f})")
            else:
                merged = max(effective_stop_loss, pos.stop_loss)
                if merged > effective_stop_loss:
                    audit_parts.append(f"tighter_by_pos_stop({effective_stop_loss:.2f}->{merged:.2f})")
                effective_stop_loss = merged

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
                        audit_parts.append(f"atr_entry_tighten({effective_stop_loss:.2f}->{merged:.2f})")
                    effective_stop_loss = merged

        # 实盘硬止损：买入动作不得放宽到 -STEP4_BUY_HARD_STOP_PCT 之外
        # 仅约束 PROBE/ATTACK，不影响 HOLD/TRIM/EXIT 的存量跟踪止损逻辑。
        if action in {"PROBE", "ATTACK"} and STEP4_BUY_HARD_STOP_ENABLED and STEP4_BUY_HARD_STOP_PCT > 0:
            hard_stop = current_price * (1.0 - STEP4_BUY_HARD_STOP_PCT / 100.0)
            if hard_stop > 0:
                if STEP4_BUY_STOP_MODE == "fixed":
                    prev_stop = effective_stop_loss
                    if prev_stop is None:
                        effective_stop_loss = hard_stop
                        audit_parts.append(f"hard_stop_fixed_init({effective_stop_loss:.2f})")
                    elif prev_stop < hard_stop:
                        # fixed 模式也不允许放宽已有更紧止损，最多只做风控兜底上调。
                        effective_stop_loss = hard_stop
                        audit_parts.append(f"hard_stop_fixed_raise({prev_stop:.2f}->{hard_stop:.2f})")
                    else:
                        audit_parts.append(f"hard_stop_fixed_keep_tighter({prev_stop:.2f})")
                else:
                    if effective_stop_loss is None:
                        effective_stop_loss = hard_stop
                        audit_parts.append(f"hard_stop_floor_init({effective_stop_loss:.2f})")
                    elif effective_stop_loss < hard_stop:
                        audit_parts.append(f"hard_stop_floor_raise({effective_stop_loss:.2f}->{hard_stop:.2f})")
                        effective_stop_loss = hard_stop

        if action == "EXIT":
            # A 股清仓允许卖出零股；EXIT 必须一次性卖完全部持仓。
            sell_shares = int(max(held_shares, 0))
            if sell_shares <= 0:
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
        if action in {"PROBE", "ATTACK"} and self.market_regime in STEP4_BUY_BLOCK_REGIMES:
            return self._no_trade(
                dec,
                name,
                f"系统性风控拦截: regime={self.market_regime} 禁止买入",
            )
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

        # =========================================================
        # 防跳空、防追高物理拦截 (Anti-Chase Protection)
        # 优化后：记录参数到 ExecutionTicket 以供明天交易作为 limit_price 参考。不再拒绝订单。
        # =========================================================
        max_entry_price = None
        chase_profile = ""
        wyckoff_context = _format_wyckoff_context(dec.wyckoff_track, dec.wyckoff_stage, dec.wyckoff_tag)
        if action in {"PROBE", "ATTACK"}:
            gap_pct_limit, atr_mult_limit, chase_profile, wyckoff_context = _resolve_chase_limits(
                dec,
                self.market_regime,
            )
            limit_by_pct = current_price * (1.0 + gap_pct_limit / 100.0)
            limit_by_atr = float("inf")
            if atr14 is not None and atr14 > 0:
                limit_by_atr = current_price + (atr_mult_limit * atr14)
            limit_by_ai = dec.entry_zone_max if dec.entry_zone_max is not None else float("inf")

            max_entry_price = min(limit_by_pct, limit_by_atr, limit_by_ai)
            audit_parts.append(f"chase_profile={chase_profile}")
            audit_parts.append(f"gap_limit_pct={gap_pct_limit:.2f}")
            audit_parts.append(f"atr_limit_mult={atr_mult_limit:.2f}")
            audit_parts.append(f"T+1_max_entry_price={max_entry_price:.2f}")
        # =========================================================

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
        atr_slippage = max(float(atr14), 0.0) * max(STEP4_ATR_SLIPPAGE_FACTOR, 0.0) if atr14 is not None else 0.0
        slippage_abs = max(base_slippage, atr_slippage)
        fill_price = current_price + slippage_abs
        # 对称滑点口径：入场更贵，止损成交更差，避免低估极端风险。
        expected_exit_price = max(effective_stop_loss - slippage_abs, 0.0)
        risk_per_share = fill_price - expected_exit_price
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
        effective_slippage_bps = slippage_abs / current_price if current_price > 0 else self.SLIPPAGE_BPS

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
                    f"expected_exit_price={expected_exit_price:.4f}",
                    f"base_slippage={base_slippage:.4f}",
                    f"atr_slippage={atr_slippage:.4f}",
                    f"budget={budget:.2f}",
                    f"shares_by_risk={max_shares_by_risk:.2f}",
                    f"shares_by_cash={max_shares_by_cash:.2f}",
                    "buy_with_slippage",
                ]
            ),
            max_entry_price=max_entry_price,
            chase_profile=chase_profile,
            wyckoff_context=wyckoff_context,
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
                stop_loss=float(item.get("stop_loss")) if item.get("stop_loss") is not None else None,
            )
        )
    return PortfolioState(free_cash=free_cash, total_equity=total_equity, positions=positions)


def _load_portfolio_from_env(env_key: str = "MY_PORTFOLIO_STATE") -> PortfolioState:
    raw = os.getenv(env_key, "").strip()
    if not raw:
        raise ValueError(f"{env_key} 未配置")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"{env_key} 非法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{env_key} 必须是 JSON 对象")
    return _build_portfolio_from_dict(data)


def _portfolio_state_signature_from_state(portfolio: PortfolioState) -> str:
    return compute_portfolio_state_signature(
        portfolio.free_cash,
        [
            {
                "code": p.code,
                "shares": p.shares,
                "cost_price": p.cost,
                "buy_dt": p.buy_dt,
            }
            for p in portfolio.positions
        ],
    )


def load_portfolio_from_supabase(portfolio_id: str) -> tuple[PortfolioState, str, str]:
    """
    优先从 Supabase 读取指定 portfolio_id；
    若缺失则回退到 MY_PORTFOLIO_STATE（Action Secret）。
    返回：(PortfolioState, source, state_signature)
    """
    sb_data = load_portfolio_state_from_supabase(portfolio_id)
    if sb_data:
        try:
            portfolio = _build_portfolio_from_dict(sb_data)
            state_signature = str(sb_data.get("state_signature", "") or "").strip().lower()
            if not state_signature:
                state_signature = _portfolio_state_signature_from_state(portfolio)
            return (portfolio, f"supabase:{portfolio_id.lower()}", state_signature)
        except Exception as e:
            raise ValueError(f"Supabase {portfolio_id} 解析失败: {e}") from e
    try:
        p = _load_portfolio_from_env("MY_PORTFOLIO_STATE")
        return (p, "env:MY_PORTFOLIO_STATE", _portfolio_state_signature_from_state(p))
    except Exception as e:
        raise ValueError(f"Supabase {portfolio_id} 未就绪，且 env 持仓不可用: {e}") from e


def _calc_holding_trade_days(
    df: pd.DataFrame,
    buy_dt: str,
    end_trade_date: date,
) -> int | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    if not str(buy_dt or "").strip():
        return None
    buy_ts = pd.to_datetime(buy_dt, errors="coerce")
    if pd.isna(buy_ts):
        return None
    buy_date = buy_ts.date()

    dates = pd.to_datetime(df["date"], errors="coerce").dropna().dt.date.tolist()
    if not dates:
        return None
    dates = sorted(set(d for d in dates if d <= end_trade_date))
    if not dates:
        return None
    entry_trade_date = next((d for d in dates if d >= buy_date), None)
    if entry_trade_date is None:
        return None
    return int(sum(1 for d in dates if d >= entry_trade_date))


def _fetch_latest_real_close(code: str, window) -> float | None:
    # 优先不复权；若交易日未对齐或拉取异常，再回退到前复权，避免误判“无最新价”。
    for adjust, label in [("", "不复权"), ("qfq", "前复权")]:
        try:
            raw = _fetch_hist(code, window, adjust)
            df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
            if ENFORCE_TARGET_TRADE_DATE:
                df, patched = _append_spot_bar_if_needed(code, df, window.end_trade_date)
                if patched:
                    print(f"[step4] {code} 实时快照补偿成功（{label}）")
                latest_trade_date = _latest_trade_date_from_hist(df)
                if latest_trade_date != window.end_trade_date:
                    print(
                        f"[step4] {code} {label}交易日未对齐: "
                        f"latest_trade_date={latest_trade_date}, target_trade_date={window.end_trade_date}"
                    )
                    continue
            return float(df.iloc[-1]["close"])
        except Exception:
            continue
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
) -> tuple[str, str, float, float, float | None, int | None]:
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
        hold_trade_days = _calc_holding_trade_days(df_qfq, pos.buy_dt, window.end_trade_date)

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
            f"- 持仓交易日: {(hold_trade_days if hold_trade_days is not None else '-')}\n"
            f"- 买入日期: {pos.buy_dt or '-'}\n"
        )
        # 持仓健康诊断：复用 Wyckoff 引擎检测 L2 通道、退出信号、阶段等
        try:
            diag = diagnose_one_stock(
                code=pos.code,
                name=pos.name,
                cost=pos.cost,
                df=df_qfq,
                bench_df=None,
                cfg=FunnelConfig(),
            )
            diag_text = f"- {format_diagnostic_for_llm(diag)}\n"
            # 将诊断结果传入 payload 生成，让 AI 看到退出预警
            payload = generate_stock_payload(
                stock_code=pos.code,
                stock_name=pos.name,
                wyckoff_tag="持仓",
                df=df_qfq,
                track=diag.track if diag.track != "Unknown" else None,
                stage=diag.accum_stage,
                exit_signal=diag.exit_signal,
                exit_price=diag.exit_price,
                exit_reason=diag.exit_reason,
            )
        except Exception:
            diag_text = ""
            payload = generate_stock_payload(
                stock_code=pos.code,
                stock_name=pos.name,
                wyckoff_tag="持仓",
                df=df_qfq,
            )
        return (meta + diag_text + "\n" + payload, failure_msg, live_val, latest_close, atr14, hold_trade_days)
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
            return (fallback_meta, f"{pos.code}:{e}", live_val, latest_close, None, None)
        return ("", f"{pos.code}:{e}", 0.0, 0.0, None, None)


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
            try:
                meta_block, fail_msg, val, close, atr, _ = future.result()
            except Exception as e:
                failures.append(f"{pos.code} {pos.name}: 数据处理异常 {e}")
                print(f"[step4] ⚠ 持仓 {pos.code} 处理异常: {e}")
                continue
            if fail_msg:
                failures.append(fail_msg)
            if meta_block:
                blocks.append(meta_block)
                live_value_sum += val
                latest_close_map[pos.code] = close
                if atr is not None:
                    atr_map[pos.code] = atr

    return ("\n\n".join(blocks), failures, live_value_sum, latest_close_map, atr_map)


def _process_one_candidate(
    item: dict,
    window,
) -> tuple[str, str, float | None, float | None]:
    code = _clean_text(item.get("code"))
    name = _clean_text(item.get("name")) or code
    try:
        raw_qfq = _fetch_hist(code, window, "qfq")
        df_qfq = normalize_hist_from_fetch(raw_qfq).sort_values("date").reset_index(drop=True)
        if ENFORCE_TARGET_TRADE_DATE:
            df_qfq, patched = _append_spot_bar_if_needed(
                code,
                df_qfq,
                window.end_trade_date,
            )
            if patched:
                print(f"[step4] {code} 候选切片已用实时快照补偿")
            latest_trade_date = _latest_trade_date_from_hist(df_qfq)
            if latest_trade_date != window.end_trade_date:
                raise RuntimeError(
                    f"qfq_latest_trade_date={latest_trade_date}, target_trade_date={window.end_trade_date}"
                )

        atr14 = _calc_atr(df_qfq, STEP4_ATR_PERIOD)
        latest_close = _fetch_latest_real_close(code, window)
        if latest_close is None:
            latest_close = float(df_qfq.iloc[-1]["close"])

        payload = generate_stock_payload(
            stock_code=code,
            stock_name=name,
            wyckoff_tag=_clean_text(item.get("tag")) or "漏斗候选",
            df=df_qfq,
            industry=_clean_text(item.get("industry")) or None,
            track=_clean_text(item.get("track")) or None,
            stage=_clean_text(item.get("stage")) or None,
            funnel_score=_parse_float_like(item.get("score")),
            sector_state=_clean_text(item.get("sector_state")) or None,
            sector_state_code=_clean_text(item.get("sector_state_code")) or None,
            sector_note=_clean_text(item.get("sector_note")) or None,
            exit_signal=_clean_text(item.get("exit_signal")) or None,
            exit_price=_parse_float_like(item.get("exit_price")),
            exit_reason=_clean_text(item.get("exit_reason")) or None,
        )
        return (payload, "", latest_close, atr14)
    except Exception as e:
        return ("", f"{code}:{e}", None, None)


def _format_candidate_payload(
    candidate_items: list[dict],
    window,
) -> tuple[str, list[str], dict[str, float], dict[str, float]]:
    if not candidate_items:
        return ("", [], {}, {})

    blocks_by_index: dict[int, str] = {}
    failures: list[str] = []
    latest_close_map: dict[str, float] = {}
    atr_map: dict[str, float] = {}

    with ThreadPoolExecutor(max_workers=STEP4_MAX_WORKERS) as executor:
        futures = {
            executor.submit(_process_one_candidate, item, window): (idx, item)
            for idx, item in enumerate(candidate_items)
        }
        for future in as_completed(futures):
            idx, item = futures[future]
            block, fail_msg, latest_close, atr14 = future.result()
            if fail_msg:
                failures.append(fail_msg)
            if block:
                blocks_by_index[idx] = block
            code = _clean_text(item.get("code"))
            if latest_close is not None:
                latest_close_map[code] = latest_close
            if atr14 is not None:
                atr_map[code] = atr14

    ordered_blocks = [blocks_by_index[idx] for idx in sorted(blocks_by_index)]
    return ("\n\n".join(ordered_blocks), failures, latest_close_map, atr_map)


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


def _max_new_buy_names(market_regime: str) -> int:
    regime = _clean_text(market_regime).upper() or "NEUTRAL"
    if regime == "RISK_ON":
        return STEP4_MAX_NEW_BUYS_RISK_ON
    if regime == "CAUTION":
        return STEP4_MAX_NEW_BUYS_CAUTION
    if regime == "RISK_OFF":
        return STEP4_MAX_NEW_BUYS_RISK_OFF
    if regime in {"CRASH", "BLACK_SWAN"}:
        return 0
    return STEP4_MAX_NEW_BUYS_NEUTRAL


def _trim_new_buy_decisions(
    decisions: list[DecisionItem],
    held_codes: set[str],
    market_regime: str,
) -> list[DecisionItem]:
    max_new_names = _max_new_buy_names(market_regime)
    if max_new_names < 0:
        return decisions

    new_buys = [dec for dec in decisions if dec.action in {"PROBE", "ATTACK"} and dec.code not in held_codes]
    if len(new_buys) <= max_new_names:
        return decisions

    def _rank_key(dec: DecisionItem) -> tuple[float, float, int]:
        confidence = dec.confidence if dec.confidence is not None else -1.0
        funnel_score = dec.funnel_score if dec.funnel_score is not None else float("-inf")
        action_rank = 1 if dec.action == "ATTACK" else 0
        return (confidence, funnel_score, action_rank)

    keep_codes = {dec.code for dec in sorted(new_buys, key=_rank_key, reverse=True)[:max_new_names]}
    dropped = [dec.code for dec in new_buys if dec.code not in keep_codes]
    if dropped:
        print(
            f"[step4] 组合级限购生效: regime={market_regime}, "
            f"max_new_buy_names={max_new_names}, dropped={','.join(dropped)}"
        )
    return [
        dec
        for dec in decisions
        if not (dec.action in {"PROBE", "ATTACK"} and dec.code not in held_codes and dec.code not in keep_codes)
    ]


def _dump_model_input(model: str, system_prompt: str, user_message: str, symbols: list[str]) -> None:
    """step4 专用包装：转发到 tools.debug_io.dump_model_input。"""
    _dump_model_input_shared(
        step_prefix="step4",
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        symbols=symbols,
    )


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
        "🚨 Alpha-OMS 交易执行工单",
        f"📅 日期：{now_str} | 净权益：{total_equity:.2f} | 当前可用现金：{free_cash_before:.2f}",
        f"🤖 模型：{model}",
    ]
    if market_view:
        lines.append(f"📌 市场视图：{market_view}")
    lines.append("")

    lines.append(f"🟥 [卖出动作 SELL] ({len(sells)})")
    if not sells:
        lines.append("- 无")
    else:
        for t in sells:
            lines.append(f"- 🟥 {t.action} | {t.code} {t.name}")
            lines.append(f"  执行：{t.shares} 股 | 回笼：{t.amount:.2f} 元 | 止损：{_fmt_stop(t.stop_loss)}")
            if t.atr14 is not None:
                lines.append(f"  风控：ATR{STEP4_ATR_PERIOD}={t.atr14:.3f} | 滑点={t.slippage_bps * 100:.2f}%")
            lines.append(f"  触发：{_first_sentence(t.tape_condition)}")
            lines.append(f"  失效：{_first_sentence(t.invalidate_condition)}")
            lines.append(f"  理由：{_first_sentence(t.reason)}")
            lines.append("")

    lines.append(f"🟨 [持有动作 HOLD] ({len(holds)})")
    if not holds:
        lines.append("- 无")
    else:
        for t in holds:
            lines.append(f"- 🟨 HOLD | {t.code} {t.name} | 止损：{_fmt_stop(t.stop_loss)}")
            if t.atr14 is not None:
                lines.append(
                    f"  风控：ATR{STEP4_ATR_PERIOD}={t.atr14:.3f} | 动态止损={_fmt_stop(t.effective_stop_loss)}"
                )
            lines.append(f"  观察：{_first_sentence(t.reason)}")
            lines.append(f"  触发：{_first_sentence(t.tape_condition)}")
            lines.append(f"  失效：{_first_sentence(t.invalidate_condition)}")
            lines.append("")
    lines.append("")

    lines.append(f"🟩 [买入动作 BUY - APPROVED] ({len(approved_buy)})")
    if not approved_buy:
        lines.append("- 无")
    else:
        for t in approved_buy:
            lines.append(f"- 🟩 {t.action} | {t.code} {t.name}")
            lines.append(
                f"  下单：{t.shares} 股 | 占用：{t.amount:.2f} 元 | 参考价："
                f"{('-' if t.price_hint is None else f'{t.price_hint:.2f}')}"
            )
            if t.chase_profile:
                lines.append(f"  分层：{t.chase_profile}")
            if t.wyckoff_context:
                lines.append(f"  结构：{t.wyckoff_context}")
            # ---> 新增这一行红色高亮提示 <---
            if t.max_entry_price is not None:
                lines.append(f"  🛑 【防追高限价】明日开盘价若 > {t.max_entry_price:.2f} 元，请放弃买入！")

            lines.append(
                f"  风险：止损 {_fmt_stop(t.stop_loss)} | 最大回撤 {t.max_loss:.2f} 元 ({t.drawdown_ratio * 100:.2f}%)"
                f" | 滑点={t.slippage_bps * 100:.2f}%"
            )
            if t.atr14 is not None:
                lines.append(f"  ATR：ATR{STEP4_ATR_PERIOD}={t.atr14:.3f}")
            if t.tape_condition:
                lines.append(f"  确认：{_first_sentence(t.tape_condition)}")
            if t.invalidate_condition:
                lines.append(f"  熔断：{_first_sentence(t.invalidate_condition)}")
            if t.reason:
                lines.append(f"  理由：{_first_sentence(t.reason)}")
            lines.append("")
    lines.append("")

    lines.append(f"⬛ [风控拒单 NO_TRADE] ({len(blocked)})")
    if not blocked:
        lines.append("- 无")
    else:
        for t in blocked:
            lines.append(f"- ⬛ NO_TRADE | {t.code} {t.name} | 原动作：{t.action}")
            lines.append(f"  原因：{_first_sentence(t.reason)}")
            if t.audit:
                lines.append(f"  审计：{_first_sentence(t.audit)}")
            lines.append("")
    lines.append("")
    lines.append(f"💰 执行后可用现金：{free_cash_after:.2f}")
    return "\n".join(lines)


def run(
    external_report: str,
    benchmark_context: dict | None,
    api_key: str,
    model: str,
    *,
    candidate_meta: list[dict] | None = None,
    portfolio_id: str,
    tg_bot_token: str,
    tg_chat_id: str,
) -> tuple[bool, str]:
    if not api_key or not api_key.strip():
        return (False, "missing_api_key")
    if not portfolio_id:
        return (True, "skipped_invalid_portfolio")

    try:
        portfolio, portfolio_source, state_signature = load_portfolio_from_supabase(portfolio_id)
    except Exception as e:
        print(f"[step4] 持仓读取失败: {e}")
        return (True, "skipped_invalid_portfolio")
    print(f"[step4] 持仓来源: {portfolio_source} | portfolio_id={portfolio_id} | state_sig={state_signature or '-'}")
    from cli.progress import report_progress

    report_progress("持仓决策", f"来源: {portfolio_source}", 0.1)

    if not str(tg_bot_token or "").strip() or not str(tg_chat_id or "").strip():
        print("[step4] tg_bot_token/tg_chat_id 未配置，跳过 Step4 推送")
        return (True, "skipped_telegram_unconfigured")

    trade_date = resolve_end_calendar_day().strftime("%Y-%m-%d")
    if check_daily_run_exists(portfolio_id, trade_date, state_signature=state_signature):
        print(f"[step4] 幂等性检查: {portfolio_id} {trade_date} 当前持仓快照已运行过，跳过。")
        return (True, "skipped_idempotency")

    end_day = resolve_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)
    (
        positions_payload,
        position_failures,
        live_value,
        latest_price_map,
        atr_map,
    ) = _format_position_payload(
        portfolio.positions,
        window,
    )
    # 风控基数统一按“最新价格口径”重算，避免沿用旧 total_equity 导致仓位偏差。
    computed_total_equity = float(portfolio.free_cash + live_value)
    if portfolio.total_equity is not None:
        drift = abs(float(portfolio.total_equity) - computed_total_equity)
        if drift >= 1e-6:
            print(
                f"[step4] total_equity 已按实时口径重算: "
                f"input={float(portfolio.total_equity):.2f}, computed={computed_total_equity:.2f}, drift={drift:.2f}"
            )
    total_equity = computed_total_equity

    position_codes = [p.code for p in portfolio.positions]
    position_code_set = set(position_codes)
    candidate_codes: list[str] = []
    seen_candidate_codes: set[str] = set()
    candidate_items: list[dict] = []
    for item in candidate_meta or []:
        if not isinstance(item, dict):
            continue
        code = _clean_text(item.get("code"))
        if not re.fullmatch(r"\d{6}", code):
            continue
        if code in position_code_set or code in seen_candidate_codes:
            continue
        seen_candidate_codes.add(code)
        candidate_codes.append(code)
        candidate_items.append(dict(item))
    for code in _extract_stock_codes(external_report):
        if code in position_code_set or code in seen_candidate_codes:
            continue
        seen_candidate_codes.add(code)
        candidate_codes.append(code)
    allowed_codes = set(position_codes + candidate_codes)
    candidate_meta_map = _build_candidate_meta_map(candidate_meta, portfolio.positions)
    name_map = {p.code: p.name for p in portfolio.positions}
    for code, meta in candidate_meta_map.items():
        if code in allowed_codes and code not in name_map:
            name_map[code] = meta.name or code

    candidate_payload, candidate_failures, candidate_latest_price_map, candidate_atr_map = _format_candidate_payload(
        candidate_items,
        window,
    )
    if candidate_latest_price_map:
        latest_price_map.update(candidate_latest_price_map)
    if candidate_atr_map:
        atr_map.update(candidate_atr_map)

    market_signal_row = _load_market_signal_for_trade_date(trade_date)
    if market_signal_row:
        print(
            f"[step4] 读取全局风控: trade_date={trade_date}, "
            f"benchmark={market_signal_row.get('benchmark_regime') or '-'}, "
            f"premarket={market_signal_row.get('premarket_regime') or '-'}"
        )
    else:
        print(f"[step4] 未读取到当日全局风控: trade_date={trade_date}")
    market_regime, benchmark_text, system_market_view = _build_market_guardrail(
        trade_date=trade_date,
        benchmark_context=benchmark_context,
        market_signal_row=market_signal_row,
    )

    max_new_buy_names = _max_new_buy_names(market_regime)
    user_message = (
        benchmark_text
        + "[账户状态]\n"
        + f"free_cash={portfolio.free_cash:.2f}\n"
        + f"total_equity={float(total_equity):.2f}\n"
        + f"position_count={len(portfolio.positions)}\n"
        + f"candidate_count={len(candidate_codes)}\n"
        + f"allowed_codes={','.join(sorted(allowed_codes))}\n\n"
        + "[组合决策约束]\n"
        + f"max_new_buy_names={max_new_buy_names}\n"
        + "external_candidates_are_optional=true\n"
        + "omit_rejected_candidates_from_decisions=true\n"
        + "prefer_cash_over_marginal_candidates=true\n"
        + "all_existing_positions_must_have_action=true\n\n"
        + "[系统硬规则]\n"
        + f"buy_stop_mode={STEP4_BUY_STOP_MODE}, buy_stop_pct={STEP4_BUY_HARD_STOP_PCT:.1f}\n"
        + "仅允许依据结构止损、Distribution 信号与量价破坏做减仓/清仓，不得因为持有天数到期而机械离场。\n\n"
        + "[内部持仓量价切片]\n"
        + (positions_payload if positions_payload else "当前无持仓，仅现金。")
        + "\n\n[漏斗候选量价切片]\n"
        + (candidate_payload if candidate_payload else "无")
    )
    data_notes: list[str] = []
    data_notes.extend(position_failures)
    data_notes.extend(candidate_failures)
    if data_notes:
        user_message += "\n\n[数据注意]\n" + "\n".join(f"- {x}" for x in data_notes)
    if (not candidate_payload) and external_report and external_report.strip():
        user_message += "\n\n[Step3参考摘要-仅在候选切片缺失时启用]\n" + external_report.strip()

    _dump_model_input(
        model=model,
        system_prompt=PRIVATE_PM_DECISION_JSON_PROMPT,
        user_message=user_message,
        symbols=sorted(allowed_codes),
    )

    report_progress("LLM决策", "计算中", 0.5)
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

    rendered_market_view = system_market_view
    if market_view and system_market_view:
        rendered_market_view = f"{system_market_view} | 模型摘要：{market_view}"
    elif market_view:
        rendered_market_view = market_view

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

    decisions = _attach_candidate_meta(decisions, candidate_meta_map)
    decisions = _trim_new_buy_decisions(
        decisions,
        held_codes=position_code_set,
        market_regime=market_regime,
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
        market_regime=market_regime,
    )
    tickets, free_cash_after = engine.process(decisions)

    # 状态回写：更新持仓止损价到 Supabase
    updates = []
    for t in tickets:
        # 只更新已有持仓且有效止损价有变化的
        if t.is_holding and t.effective_stop_loss is not None:
            updates.append({"code": t.code, "stop_loss": t.effective_stop_loss})
    if updates:
        if update_position_stops(portfolio_id, updates):
            print(f"[step4] 已更新 {len(updates)} 个持仓的止损价 | portfolio_id={portfolio_id}")
        else:
            print(f"[step4] 持仓止损价更新失败 | portfolio_id={portfolio_id}")

    run_id = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S") + "_" + str(uuid4())[:8]
    if state_signature:
        run_id += f"_sig{state_signature.lower()}"
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
        market_view=rendered_market_view,
        total_equity=float(total_equity),
        free_cash_before=portfolio.free_cash,
        free_cash_after=free_cash_after,
        tickets=tickets,
    )
    sent = send_to_telegram(
        report,
        tg_bot_token=tg_bot_token,
        tg_chat_id=tg_chat_id,
    )
    if not sent:
        return (False, "telegram_failed")

    # Telegram 发送成功后才写入幂等标记，保证失败可重试
    if save_ai_trade_orders(
        run_id=run_id,
        portfolio_id=portfolio_id,
        model=model,
        trade_date=trade_date,
        market_view=rendered_market_view,
        orders=ticket_rows,
    ):
        print(f"[step4] 已写入 AI 订单记录: run_id={run_id}, count={len(ticket_rows)}, portfolio_id={portfolio_id}")
        cancelled = cancel_trade_orders(
            portfolio_id=portfolio_id,
            trade_date=trade_date,
            exclude_run_id=run_id,
        )
        if cancelled:
            print(f"[step4] 已作废同日旧 AI 订单: cancelled={cancelled}, portfolio_id={portfolio_id}")
    else:
        print(f"[step4] AI 订单记录写入失败（已忽略，不阻断流程） | portfolio_id={portfolio_id}")

    positions_value = max(float(total_equity) - float(free_cash_after), 0.0)
    if upsert_daily_nav(
        portfolio_id=portfolio_id,
        trade_date=trade_date,
        free_cash=float(free_cash_after),
        total_equity=float(total_equity),
        positions_value=positions_value,
    ):
        print(f"[step4] 已写入 {portfolio_id} 日净值快照: {trade_date}")
    else:
        print(f"[step4] {portfolio_id} 日净值快照写入失败（已忽略）")

    print(
        f"[step4] 交易工单发送成功: decisions={len(decisions)}, tickets={len(tickets)}, "
        f"model={model}, portfolio_id={portfolio_id}"
    )
    report_progress("决策完成", f"订单={len(tickets)}条", 1.0)
    return (True, "ok")
