"""
Tail Buy 任务（周一到周五 14:00）：
- 输入：signal_pending（前一交易日 + pending/confirmed）
- 判定：规则全量 + LLM TopN 二判
- 输出：飞书 + Telegram 推送（不写交易表）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import TABLE_PORTFOLIOS, TABLE_RECOMMENDATION_TRACKING, TABLE_SIGNAL_PENDING
from core.tail_buy_strategy import (
    DECISION_BUY,
    DECISION_SKIP,
    DECISION_WATCH,
    TailBuyCandidate,
    build_llm_prompt,
    build_tail_buy_markdown,
    compute_tail_features,
    evaluate_rule_decision,
    merge_rule_and_llm,
    parse_llm_decision,
    pick_tail_candidates,
    score_tail_features,
    select_llm_overlay_candidates,
)
from integrations.fetch_a_share_csv import _resolve_trading_window
from integrations.llm_client import DEFAULT_GEMINI_MODEL, OPENAI_COMPATIBLE_BASE_URLS, call_llm
from integrations.supabase_base import create_admin_client, is_admin_configured
from integrations.supabase_market_signal import (
    load_latest_market_signal_daily,
    load_market_signal_daily,
)
from integrations.supabase_portfolio import load_portfolio_state
from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol
from integrations.tickflow_notice import TICKFLOW_LIMIT_HINT, is_tickflow_rate_limited_error
from utils.feishu import send_feishu_notification, send_tail_buy_card
from utils.notify import send_to_telegram

TZ = ZoneInfo("Asia/Shanghai")
TICKFLOW_UPGRADE_HINT = TICKFLOW_LIMIT_HINT
HOLDING_ACTION_ADD = "ADD"
HOLDING_ACTION_HOLD = "HOLD"
HOLDING_ACTION_TRIM = "TRIM"


@dataclass
class HoldingAdvice:
    code: str
    name: str
    shares: int = 0
    cost: float = 0.0
    current_price: float = 0.0
    pnl_pct: float = 0.0
    rule_score: float = 0.0
    rule_decision: str = DECISION_SKIP
    action: str = HOLDING_ACTION_HOLD
    reasons: list[str] = field(default_factory=list)
    fetch_error: str = ""
    features: dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(TZ)


def _now_text() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, logs_path: str | None = None) -> None:
    line = f"[{_now_text()}] {msg}"
    print(line, flush=True)
    if logs_path:
        os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
        with open(logs_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _remaining_seconds(deadline_at: datetime) -> float:
    return (deadline_at - _now()).total_seconds()


def _log_fetch_error_summary(
    items: list[TailBuyCandidate],
    *,
    stage: str,
    logs_path: str | None = None,
) -> None:
    reasons = [str(x.fetch_error or "").strip() for x in items if str(x.fetch_error or "").strip()]
    if not reasons:
        _log(f"{stage}失败汇总: 无", logs_path)
        return
    counter = Counter(reasons)
    top = counter.most_common(5)
    summary = " | ".join([f"{reason[:80]} x{cnt}" for reason, cnt in top])
    _log(f"{stage}失败汇总: total={len(reasons)}, unique={len(counter)}, top={summary}", logs_path)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _default_tail_buy_portfolio_id() -> str:
    direct = str(os.getenv("TAIL_BUY_PORTFOLIO_ID", "") or "").strip()
    if direct:
        return direct
    user_id = str(os.getenv("SUPABASE_USER_ID", "") or "").strip()
    if user_id:
        return f"USER_LIVE:{user_id}"
    monitor = str(os.getenv("MONITOR_PORTFOLIO_ID", "") or "").strip()
    if monitor:
        return monitor
    return "USER_LIVE"


def _plan_intraday_scan_budget(
    total_candidates: int,
    *,
    limit_per_min: int,
    max_over_limit_symbols: int,
    force_over_limit: bool,
) -> tuple[int, int]:
    """
    规划本轮分时扫描数量。
    - 超限股票数始终被限制在 <= 5
    - 当 force_over_limit=True 且候选足够时，尽量打到 (limit + over_limit)
    返回 (to_scan, planned_over_limit_symbols)
    """
    total = max(int(total_candidates), 0)
    limit = max(int(limit_per_min), 1)
    over = max(min(int(max_over_limit_symbols), 5), 0)
    if total <= limit:
        return total, 0
    if force_over_limit and over > 0:
        to_scan = min(total, limit + over)
        return to_scan, max(to_scan - limit, 0)
    to_scan = min(total, limit)
    return to_scan, 0


def _chunked(seq: list[Any], chunk_size: int) -> list[list[Any]]:
    size = max(int(chunk_size), 1)
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        if raw is None:
            return default
        text = str(raw).strip()
        if not text:
            return default
        return float(text)
    except Exception:
        return default


def _normalize_iso_date(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def _normalize_yyyymmdd(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return ""


def _normalize_code6(raw: Any) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return ""
    return digits[-6:].zfill(6)


def _safe_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on", "t"}


def _resolve_quote_price(quote: dict[str, Any] | None) -> float:
    row = quote or {}
    for key in ("close", "last", "price", "current", "open"):
        value = _safe_float(row.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def _resolve_effective_stop(cost: float, stop_loss: Any, hard_stop_pct: float) -> float:
    stops = []
    explicit_stop = _safe_float(stop_loss, 0.0)
    if explicit_stop > 0:
        stops.append(explicit_stop)
    if cost > 0 and hard_stop_pct > 0:
        stops.append(cost * (1 - hard_stop_pct / 100.0))
    if not stops:
        return 0.0
    return max(stops)


def _dedupe_texts(values: list[str], limit: int = 3) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max(int(limit), 1):
            break
    return out


def _is_tickflow_upgrade_related_error(err_or_text: Any) -> bool:
    text = str(err_or_text or "").lower()
    if not text:
        return False
    markers = (
        "tickflow http 429",
        "http 429",
        "rate_limited",
        "too many requests",
        "限流",
        "forbidden",
        "套餐不支持",
        "不支持日内批量查询",
        "not support intraday batch",
        "permission denied",
    )
    return any(m in text for m in markers)


def _with_tickflow_upgrade_hint(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return text
    if TICKFLOW_UPGRADE_HINT in text:
        return text
    if _is_tickflow_upgrade_related_error(text):
        return f"{text}（{TICKFLOW_UPGRADE_HINT}）"
    return text


def _normalize_effective_positions(raw_positions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    positions: list[dict[str, Any]] = []
    stats = {
        "raw": 0,
        "active": 0,
        "invalid_code": 0,
        "zero_shares": 0,
        "invalid_row": 0,
    }
    for row in raw_positions or []:
        stats["raw"] += 1
        if not isinstance(row, dict):
            stats["invalid_row"] += 1
            continue
        code = "".join(ch for ch in str(row.get("code", "") or "").strip() if ch.isdigit()).zfill(6)
        if len(code) != 6:
            stats["invalid_code"] += 1
            continue
        shares = int(_safe_float(row.get("shares"), 0))
        if shares <= 0:
            stats["zero_shares"] += 1
            continue
        stats["active"] += 1
        positions.append(
            {
                "code": code,
                "name": str(row.get("name", "") or code).strip() or code,
                "shares": shares,
                "cost": _safe_float(row.get("cost"), 0.0),
                "stop_loss": row.get("stop_loss"),
            }
        )
    return positions, stats


def _discover_user_live_portfolios(logs_path: str | None = None, limit: int = 30) -> list[str]:
    try:
        client = create_admin_client()
        rows = (
            client.table(TABLE_PORTFOLIOS)
            .select("portfolio_id,updated_at")
            .like("portfolio_id", "USER_LIVE:%")
            .order("updated_at", desc=True)
            .limit(max(int(limit), 1))
            .execute()
            .data
            or []
        )
        ids = [str(row.get("portfolio_id", "") or "").strip() for row in rows if isinstance(row, dict)]
        ids = [x for x in ids if x]
        _log(f"持仓回退候选: USER_LIVE:* count={len(ids)}", logs_path)
        return ids
    except Exception as e:
        _log(f"持仓回退候选查询失败: {e}", logs_path)
        return []


def _analyze_holdings_actions(
    *,
    tickflow_client: TickFlowClient,
    portfolio_id: str,
    signal_map: dict[str, TailBuyCandidate],
    style: str,
    intraday_batch_size: int,
    hard_stop_pct: float,
    deadline_at: datetime,
    logs_path: str | None = None,
) -> tuple[list[HoldingAdvice], bool, str]:
    requested_portfolio_id = str(portfolio_id or "").strip() or "USER_LIVE"
    resolved_portfolio_id = requested_portfolio_id
    state = load_portfolio_state(requested_portfolio_id)
    positions: list[dict[str, Any]] = []
    position_stats = {
        "raw": 0,
        "active": 0,
        "invalid_code": 0,
        "zero_shares": 0,
        "invalid_row": 0,
    }
    if isinstance(state, dict):
        raw_positions = list(state.get("positions") or [])
        positions, position_stats = _normalize_effective_positions(raw_positions)
        _log(
            f"持仓读取: requested={requested_portfolio_id}, resolved={resolved_portfolio_id}, "
            f"raw={position_stats['raw']}, active={position_stats['active']}, "
            f"invalid_code={position_stats['invalid_code']}, zero_shares={position_stats['zero_shares']}, "
            f"invalid_row={position_stats['invalid_row']}",
            logs_path,
        )
    elif requested_portfolio_id == "USER_LIVE":
        _log("持仓读取: USER_LIVE 不存在或读取失败，尝试回退 USER_LIVE:*", logs_path)

    if requested_portfolio_id == "USER_LIVE" and (not isinstance(state, dict) or not positions):
        for candidate_id in _discover_user_live_portfolios(logs_path=logs_path, limit=30):
            fallback_state = load_portfolio_state(candidate_id)
            if not isinstance(fallback_state, dict):
                continue
            fallback_positions, fallback_stats = _normalize_effective_positions(
                list(fallback_state.get("positions") or [])
            )
            if not fallback_positions:
                continue
            state = fallback_state
            positions = fallback_positions
            position_stats = fallback_stats
            resolved_portfolio_id = candidate_id
            _log(
                f"持仓回退命中: requested=USER_LIVE -> resolved={resolved_portfolio_id}, "
                f"raw={position_stats['raw']}, active={position_stats['active']}",
                logs_path,
            )
            break

    if not isinstance(state, dict):
        return [], False, f"组合 {requested_portfolio_id} 不存在或不可读取"

    if not positions:
        meta = (
            f"portfolio={resolved_portfolio_id}, state_sig={state.get('state_signature', '-')}, "
            f"raw_positions={position_stats['raw']}, active_positions={position_stats['active']}, "
            f"invalid_code={position_stats['invalid_code']}, zero_shares={position_stats['zero_shares']}"
        )
        if requested_portfolio_id == "USER_LIVE":
            meta += "（提示：USER_LIVE 无有效仓位；请检查是否应使用 USER_LIVE:<user_id>）"
        return [], False, meta

    symbols = [normalize_cn_symbol(p["code"]) for p in positions]
    symbol_set = sorted(set([s for s in symbols if s]))
    _log(
        f"持仓动作分析开始: requested={requested_portfolio_id}, resolved={resolved_portfolio_id}, "
        f"positions={len(positions)}, symbols={len(symbol_set)}",
        logs_path,
    )

    tickflow_limit_hit = False
    quotes: dict[str, dict[str, Any]] = {}
    try:
        quotes = tickflow_client.get_quotes(symbol_set)
    except Exception as e:
        if is_tickflow_rate_limited_error(e):
            tickflow_limit_hit = True
        _log(f"持仓动作分析: 批量实时行情失败: {e}", logs_path)

    intraday_map: dict[str, Any] = {}
    intraday_error_by_symbol: dict[str, str] = {}
    for chunk in _chunked(symbol_set, max(min(int(intraday_batch_size), 200), 1)):
        if _remaining_seconds(deadline_at) <= 5:
            for sym in chunk:
                intraday_error_by_symbol[sym] = "超出任务时限，未执行持仓分时分析"
            break
        try:
            data_map = tickflow_client.get_intraday_batch(chunk, period="1m", count=5000)
            intraday_map.update(data_map)
            for sym in chunk:
                if sym not in data_map:
                    intraday_error_by_symbol[sym] = "TickFlow返回空分时"
        except Exception as e:
            if is_tickflow_rate_limited_error(e):
                tickflow_limit_hit = True
            reason = _with_tickflow_upgrade_hint(f"TickFlow持仓分时拉取失败: {e}")
            for sym in chunk:
                intraday_error_by_symbol[sym] = reason

    out: list[HoldingAdvice] = []
    add_count = 0
    trim_count = 0
    for p in positions:
        code = p["code"]
        name = p["name"]
        sym = normalize_cn_symbol(code)
        cost = _safe_float(p.get("cost"), 0.0)
        shares = int(_safe_float(p.get("shares"), 0))
        quote = quotes.get(sym) or {}
        price = _resolve_quote_price(quote)
        pnl_pct = ((price / cost - 1.0) * 100.0) if price > 0 and cost > 0 else 0.0
        effective_stop = _resolve_effective_stop(cost, p.get("stop_loss"), hard_stop_pct)

        advice = HoldingAdvice(
            code=code,
            name=name,
            shares=shares,
            cost=cost,
            current_price=price,
            pnl_pct=pnl_pct,
        )

        df_1m = intraday_map.get(sym)
        fetch_error = str(intraday_error_by_symbol.get(sym, "") or "").strip()
        if df_1m is None or getattr(df_1m, "empty", True):
            advice.fetch_error = fetch_error or "持仓分时缺失"
            advice.rule_decision = DECISION_WATCH
            advice.rule_score = 0.0
            advice.action = HOLDING_ACTION_HOLD
            advice.reasons = _dedupe_texts(
                [
                    "分时数据缺失，先维持观察",
                    advice.fetch_error,
                ],
                limit=2,
            )
            if price > 0 and effective_stop > 0 and price <= effective_stop:
                advice.action = HOLDING_ACTION_TRIM
                advice.reasons = _dedupe_texts(
                    [
                        f"现价{price:.2f}跌破风控位{effective_stop:.2f}",
                        advice.fetch_error,
                    ],
                    limit=2,
                )
        else:
            signal_item = signal_map.get(code)
            signal_score = _safe_float(signal_item.signal_score, 0.0) if signal_item else 0.0
            status = str(signal_item.status if signal_item else "pending")
            features = compute_tail_features(df_1m)
            score, decision, reasons = score_tail_features(
                features,
                signal_score=signal_score,
                status=status,
                style=style,
            )
            if advice.current_price <= 0:
                advice.current_price = _safe_float(features.get("last_close"), 0.0)
                advice.pnl_pct = (
                    (advice.current_price / cost - 1.0) * 100.0 if advice.current_price > 0 and cost > 0 else 0.0
                )
            advice.rule_score = score
            advice.rule_decision = decision
            advice.features = features

            dist_vwap_pct = _safe_float(features.get("dist_vwap_pct"), 0.0)
            close_pos = _safe_float(features.get("close_pos"), 0.0)
            last30_ret_pct = _safe_float(features.get("last30_ret_pct"), 0.0)
            drop_from_high_pct = _safe_float(features.get("drop_from_high_pct"), 0.0)

            base_reasons = _dedupe_texts(reasons, limit=2)
            if advice.current_price > 0 and effective_stop > 0 and advice.current_price <= effective_stop:
                advice.action = HOLDING_ACTION_TRIM
                advice.reasons = _dedupe_texts(
                    [
                        f"现价{advice.current_price:.2f}跌破风控位{effective_stop:.2f}",
                        *base_reasons,
                    ],
                    limit=3,
                )
            elif decision == DECISION_BUY and dist_vwap_pct >= 0.15 and close_pos >= 0.68 and last30_ret_pct >= 0.2:
                advice.action = HOLDING_ACTION_ADD
                advice.reasons = _dedupe_texts(
                    [
                        "尾盘结构延续走强，可考虑小幅加仓",
                        *base_reasons,
                    ],
                    limit=3,
                )
            elif decision == DECISION_SKIP and (
                dist_vwap_pct <= -0.6 or close_pos < 0.42 or last30_ret_pct <= -0.8 or drop_from_high_pct <= -2.2
            ):
                advice.action = HOLDING_ACTION_TRIM
                advice.reasons = _dedupe_texts(
                    [
                        "尾盘结构转弱，优先减仓控制回撤",
                        *base_reasons,
                    ],
                    limit=3,
                )
            else:
                advice.action = HOLDING_ACTION_HOLD
                advice.reasons = _dedupe_texts(
                    [
                        "结构中性，先持有观察",
                        *base_reasons,
                    ],
                    limit=3,
                )

        if advice.action == HOLDING_ACTION_ADD:
            add_count += 1
        elif advice.action == HOLDING_ACTION_TRIM:
            trim_count += 1
        out.append(advice)

    rank = {
        HOLDING_ACTION_ADD: 0,
        HOLDING_ACTION_TRIM: 1,
        HOLDING_ACTION_HOLD: 2,
    }
    out.sort(key=lambda x: (rank.get(x.action, 9), -x.rule_score, x.code))
    _log(
        f"持仓动作分析完成: total={len(out)}, add={add_count}, trim={trim_count}, "
        f"hold={len(out) - add_count - trim_count}, tickflow_limit_hit={tickflow_limit_hit}",
        logs_path,
    )
    meta = (
        f"portfolio={resolved_portfolio_id}, state_sig={state.get('state_signature', '-')}, "
        f"raw_positions={position_stats['raw']}, active_positions={position_stats['active']}"
    )
    if requested_portfolio_id != resolved_portfolio_id:
        meta += f"（fallback from {requested_portfolio_id}）"
    return out, tickflow_limit_hit, meta


def _build_holdings_markdown(
    *,
    holdings: list[HoldingAdvice],
    portfolio_meta: str,
    tickflow_limit_hit: bool,
) -> str:
    lines: list[str] = ["## 持仓动作建议（加仓/减仓）"]
    if portfolio_meta:
        lines.append(f"- 持仓来源: {portfolio_meta}")

    if not holdings:
        lines.append("- 持仓数量: 0")
        lines.append("- 动作分布: ADD=0 / HOLD=0 / TRIM=0")
        lines.append("- 无可分析持仓（仅输出候选池结果）")
        lines.append("")
        lines.append("说明：持仓动作仅为盘中辅助建议，不自动下单。")
        return "\n".join(lines)

    counter = Counter([x.action for x in holdings])
    lines.append(f"- 持仓数量: {len(holdings)}")
    lines.append(
        f"- 动作分布: ADD={counter.get(HOLDING_ACTION_ADD, 0)} / "
        f"HOLD（持有观察）={counter.get(HOLDING_ACTION_HOLD, 0)} / "
        f"TRIM（减仓）={counter.get(HOLDING_ACTION_TRIM, 0)}"
    )
    if tickflow_limit_hit:
        lines.append(f"- ⚠️ {TICKFLOW_UPGRADE_HINT}")
    lines.append("")

    def _append_block(title: str, action: str) -> None:
        block = [x for x in holdings if x.action == action]
        lines.append(f"### {title}")
        if not block:
            lines.append("- 无")
            lines.append("")
            return
        for item in block:
            reasons = "；".join(_dedupe_texts(item.reasons, limit=2)) or "结构中性"
            current = f"{item.current_price:.2f}" if item.current_price > 0 else "--"
            pnl = f"{item.pnl_pct:+.1f}%" if item.current_price > 0 and item.cost > 0 else "--"
            lines.append(
                f"- {item.code} {item.name} | 持仓={item.shares}股 | 现价={current} | "
                f"浮盈={pnl} | 规则={item.rule_decision}({item.rule_score:.1f}) | {reasons}"
            )
        lines.append("")

    _append_block("ADD（可考虑加仓）", HOLDING_ACTION_ADD)
    _append_block("TRIM（可考虑减仓）", HOLDING_ACTION_TRIM)
    _append_block("HOLD（持有观察）", HOLDING_ACTION_HOLD)
    lines.append("说明：持仓动作仅为盘中辅助建议，不自动下单。")
    return "\n".join(lines)


def _resolve_trade_dates(logs_path: str | None = None) -> tuple[str, str]:
    """
    返回 (前一交易日, 当前交易日)。
    """
    calendar_today = _now().date()
    try:
        window = _resolve_trading_window(end_calendar_day=calendar_today, trading_days=2)
        prev_trade = window.start_trade_date.isoformat()
        today_trade = window.end_trade_date.isoformat()
        # 周末/节假日：今天不是交易日，则将“前一交易日”定位为最新交易日，
        # 以便尾盘任务仍分析最新一日推荐池，而不是回退到更早一天。
        if window.end_trade_date < calendar_today:
            prev_trade = today_trade
        return prev_trade, today_trade
    except Exception as e:
        prev_trade = (calendar_today - timedelta(days=1)).isoformat()
        today_trade = calendar_today.isoformat()
        _log(
            f"交易日历解析失败，降级为自然日: prev={prev_trade}, today={today_trade}, err={e}",
            logs_path,
        )
        return prev_trade, today_trade


def _load_signal_pending_candidates(target_signal_date: str, logs_path: str | None = None) -> list[TailBuyCandidate]:
    if not is_admin_configured():
        raise RuntimeError("Supabase 凭据未配置，无法读取 signal_pending")

    client = create_admin_client()
    base_query = (
        client.table(TABLE_SIGNAL_PENDING)
        .select("code,name,signal_type,signal_score,status,signal_date")
        .in_("status", ["pending", "confirmed"])
    )
    rows: list[dict] = []
    try:
        rows = base_query.eq("signal_date", target_signal_date).limit(5000).execute().data or []
    except Exception as e:
        _log(f"按 signal_date 精确查询失败，尝试宽松查询: {e}", logs_path)

    if not rows:
        try:
            rows = (
                client.table(TABLE_SIGNAL_PENDING)
                .select("code,name,signal_type,signal_score,status,signal_date")
                .in_("status", ["pending", "confirmed"])
                .order("signal_date", desc=True)
                .limit(8000)
                .execute()
                .data
                or []
            )
        except Exception as e:
            raise RuntimeError(f"读取 signal_pending 失败: {e}") from e

    picked = pick_tail_candidates(rows, target_signal_date=target_signal_date)
    _log(f"signal_pending 候选加载: raw={len(rows)}, picked={len(picked)}, signal_date={target_signal_date}", logs_path)
    return picked


def _load_recommendation_tracking_candidates(
    target_signal_date: str,
    logs_path: str | None = None,
) -> list[TailBuyCandidate]:
    if not is_admin_configured():
        raise RuntimeError("Supabase 凭据未配置，无法读取 recommendation_tracking")

    client = create_admin_client()
    target_yyyymmdd = target_signal_date.replace("-", "")
    rows: list[dict[str, Any]] = []
    try:
        rows = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select("code,name,recommend_date,funnel_score,is_ai_recommended")
            .eq("recommend_date", int(target_yyyymmdd))
            .limit(5000)
            .execute()
            .data
            or []
        )
    except Exception as e:
        _log(f"recommendation_tracking 精确查询失败，尝试宽松查询: {e}", logs_path)

    if not rows:
        try:
            rows = (
                client.table(TABLE_RECOMMENDATION_TRACKING)
                .select("code,name,recommend_date,funnel_score,is_ai_recommended")
                .order("recommend_date", desc=True)
                .limit(10000)
                .execute()
                .data
                or []
            )
        except Exception as e:
            raise RuntimeError(f"读取 recommendation_tracking 失败: {e}") from e

    picked_map: dict[str, TailBuyCandidate] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        rec_date = _normalize_yyyymmdd(row.get("recommend_date"))
        if rec_date != target_yyyymmdd:
            continue
        code = _normalize_code6(row.get("code"))
        if not code:
            continue
        status = "confirmed" if _safe_bool(row.get("is_ai_recommended")) else "pending"
        candidate = TailBuyCandidate(
            code=code,
            name=str(row.get("name", "") or code).strip() or code,
            signal_date=_normalize_iso_date(target_signal_date) or target_signal_date,
            status=status,
            signal_type="unknown",
            signal_score=_safe_float(row.get("funnel_score"), 0.0),
        )
        old = picked_map.get(code)
        if old is None:
            picked_map[code] = candidate
            continue
        old_rank = 1 if old.status == "confirmed" else 0
        new_rank = 1 if candidate.status == "confirmed" else 0
        if (new_rank, candidate.signal_score) > (old_rank, old.signal_score):
            picked_map[code] = candidate

    picked = list(picked_map.values())
    picked.sort(key=lambda x: (x.status != "confirmed", -x.signal_score, x.code))
    _log(
        f"recommendation_tracking 候选加载: raw={len(rows)}, picked={len(picked)}, recommend_date={target_yyyymmdd}",
        logs_path,
    )
    return picked


def _merge_tail_candidates(
    signal_pending_candidates: list[TailBuyCandidate],
    recommendation_candidates: list[TailBuyCandidate],
) -> tuple[list[TailBuyCandidate], int]:
    merged: dict[str, TailBuyCandidate] = {}
    for item in signal_pending_candidates or []:
        if item.code:
            merged[item.code] = item

    appended = 0
    for rec in recommendation_candidates or []:
        if not rec.code:
            continue
        old = merged.get(rec.code)
        if old is None:
            merged[rec.code] = rec
            appended += 1
            continue
        old.signal_score = max(_safe_float(old.signal_score), _safe_float(rec.signal_score))
        if old.status != "confirmed" and rec.status == "confirmed":
            old.status = "confirmed"
        if (not old.name or old.name == old.code) and rec.name:
            old.name = rec.name

    out = list(merged.values())
    out.sort(key=lambda x: (x.status != "confirmed", -x.signal_score, x.code))
    return out, appended


def _load_tail_candidates(target_signal_date: str, logs_path: str | None = None) -> tuple[list[TailBuyCandidate], str]:
    pending: list[TailBuyCandidate] = []
    recs: list[TailBuyCandidate] = []
    pending_err = ""
    rec_err = ""

    try:
        pending = _load_signal_pending_candidates(target_signal_date, logs_path)
    except Exception as e:
        pending_err = str(e)
        _log(f"signal_pending 加载失败（降级继续）: {e}", logs_path)

    try:
        recs = _load_recommendation_tracking_candidates(target_signal_date, logs_path)
    except Exception as e:
        rec_err = str(e)
        _log(f"recommendation_tracking 加载失败（降级继续）: {e}", logs_path)

    if pending_err and rec_err:
        raise RuntimeError(f"候选池双源都失败: signal_pending={pending_err}; recommendation_tracking={rec_err}")

    merged, rec_added = _merge_tail_candidates(pending, recs)
    source_desc = (
        "signal_pending + recommendation_tracking "
        f"(signal_date/recommend_date={target_signal_date}; rec_only={rec_added})"
    )
    _log(
        "候选池加载完成: "
        f"signal_pending={len(pending)}, recommendation_tracking={len(recs)}, "
        f"merged={len(merged)}, signal_date={target_signal_date}",
        logs_path,
    )
    return merged, source_desc


def _scan_one_symbol(
    client: TickFlowClient,
    candidate: TailBuyCandidate,
    *,
    style: str,
) -> TailBuyCandidate:
    symbol = normalize_cn_symbol(candidate.code)
    try:
        df_1m = client.get_intraday(symbol, period="1m", count=5000)
    except Exception as e:
        candidate.fetch_error = _with_tickflow_upgrade_hint(f"TickFlow分钟数据拉取失败: {e}")
        candidate.rule_reasons = [candidate.fetch_error]
        return candidate
    if df_1m is None or df_1m.empty:
        candidate.fetch_error = "TickFlow返回空分时"
        candidate.rule_reasons = [candidate.fetch_error]
        return candidate
    try:
        return evaluate_rule_decision(candidate, df_1m, style=style)
    except Exception as e:
        candidate.fetch_error = f"规则评分失败: {e}"
        candidate.rule_reasons = [candidate.fetch_error]
        return candidate


def _run_rule_scan(
    candidates: list[TailBuyCandidate],
    *,
    tickflow_client: TickFlowClient,
    style: str,
    fetch_concurrency: int,
    deadline_at: datetime,
    logs_path: str | None = None,
) -> list[TailBuyCandidate]:
    if not candidates:
        return []
    max_workers = max(int(fetch_concurrency), 1)
    futures = {}
    scanned: list[TailBuyCandidate] = []
    skipped_due_deadline = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for item in candidates:
            if _remaining_seconds(deadline_at) <= 5:
                skipped_due_deadline += 1
                item.fetch_error = "超出任务时限，未执行分时扫描"
                item.rule_reasons = [item.fetch_error]
                scanned.append(item)
                continue
            future = ex.submit(_scan_one_symbol, tickflow_client, item, style=style)
            futures[future] = item.code

        timeout_seconds = max(1, int(_remaining_seconds(deadline_at)))
        try:
            for fut in as_completed(futures, timeout=timeout_seconds):
                try:
                    scanned.append(fut.result())
                except Exception as e:
                    code = futures.get(fut, "")
                    fallback = TailBuyCandidate(
                        code=str(code),
                        name=str(code),
                        signal_date="",
                        status="pending",
                        signal_type="unknown",
                        signal_score=0.0,
                        fetch_error=f"并发执行异常: {e}",
                        rule_reasons=[f"并发执行异常: {e}"],
                    )
                    scanned.append(fallback)
        except FutureTimeout:
            _log("规则扫描触发 deadline 保护：停止等待剩余任务。", logs_path)
            for fut, code in futures.items():
                if fut.done():
                    continue
                fut.cancel()
                fallback = TailBuyCandidate(
                    code=str(code),
                    name=str(code),
                    signal_date="",
                    status="pending",
                    signal_type="unknown",
                    signal_score=0.0,
                    fetch_error="超出任务时限，任务已取消",
                    rule_reasons=["超出任务时限，任务已取消"],
                )
                scanned.append(fallback)

    scanned.sort(key=lambda x: (-x.rule_score, x.code))
    ok_cnt = sum(1 for x in scanned if not x.fetch_error)
    fail_cnt = len(scanned) - ok_cnt
    if skipped_due_deadline:
        _log(f"规则扫描: 因 deadline 提前跳过 {skipped_due_deadline} 只", logs_path)
    _log(
        f"规则扫描完成: total={len(scanned)}, ok={ok_cnt}, fail={fail_cnt}, workers={max_workers}",
        logs_path,
    )
    _log_fetch_error_summary(scanned, stage="规则扫描(single)", logs_path=logs_path)
    return scanned


def _run_rule_scan_batch(
    candidates: list[TailBuyCandidate],
    *,
    tickflow_client: TickFlowClient,
    style: str,
    batch_size: int,
    deadline_at: datetime,
    logs_path: str | None = None,
) -> list[TailBuyCandidate]:
    """
    优先批量拉取分时，显著降低请求次数，尽量避免单标的限流。
    """
    if not candidates:
        return []

    chunks = _chunked(candidates, max(min(int(batch_size), 200), 1))
    scanned: list[TailBuyCandidate] = []
    skipped_due_deadline = 0
    batch_fail_symbols = 0
    batch_rate_limited_symbols = 0

    for idx, chunk in enumerate(chunks, start=1):
        _log(
            f"规则扫描(batch): chunk={idx}/{len(chunks)}, size={len(chunk)}, "
            f"time_left={_remaining_seconds(deadline_at):.1f}s",
            logs_path,
        )
        if _remaining_seconds(deadline_at) <= 5:
            skipped_due_deadline += len(chunk)
            for item in chunk:
                item.fetch_error = "超出任务时限，未执行分时扫描"
                item.rule_reasons = [item.fetch_error]
                scanned.append(item)
            continue

        symbols = [normalize_cn_symbol(item.code) for item in chunk]
        try:
            data_map = tickflow_client.get_intraday_batch(symbols, period="1m", count=5000)
        except Exception as e:
            reason = _with_tickflow_upgrade_hint(f"TickFlow批量分时拉取失败: {e}")
            batch_fail_symbols += len(chunk)
            if "429" in str(e) or "RATE_LIMITED" in str(e):
                batch_rate_limited_symbols += len(chunk)
            for item in chunk:
                item.fetch_error = reason
                item.rule_reasons = [reason]
                scanned.append(item)
            _log(
                f"规则扫描(batch): chunk={idx}/{len(chunks)} failed, affected={len(chunk)}, err={e}",
                logs_path,
            )
            continue

        _log(
            f"规则扫描(batch): chunk={idx}/{len(chunks)} data_hit={len(data_map)}/{len(chunk)}",
            logs_path,
        )
        for item in chunk:
            sym = normalize_cn_symbol(item.code)
            df_1m = data_map.get(sym)
            if df_1m is None or df_1m.empty:
                item.fetch_error = "TickFlow返回空分时"
                item.rule_reasons = [item.fetch_error]
                scanned.append(item)
                continue
            try:
                scanned.append(evaluate_rule_decision(item, df_1m, style=style))
            except Exception as e:
                item.fetch_error = f"规则评分失败: {e}"
                item.rule_reasons = [item.fetch_error]
                scanned.append(item)

    scanned.sort(key=lambda x: (-x.rule_score, x.code))
    ok_cnt = sum(1 for x in scanned if not x.fetch_error)
    fail_cnt = len(scanned) - ok_cnt
    if skipped_due_deadline:
        _log(f"规则扫描(batch): 因 deadline 提前跳过 {skipped_due_deadline} 只", logs_path)
    _log(
        f"规则扫描(batch)完成: total={len(scanned)}, ok={ok_cnt}, fail={fail_cnt}, "
        f"batch_size={max(min(int(batch_size), 200), 1)}, batch_fail_symbols={batch_fail_symbols}, "
        f"batch_rate_limited_symbols={batch_rate_limited_symbols}",
        logs_path,
    )
    _log_fetch_error_summary(scanned, stage="规则扫描(batch)", logs_path=logs_path)
    return scanned


def _fetch_depth_features(
    client: TickFlowClient,
    candidates: list[TailBuyCandidate],
    *,
    max_symbols: int = 20,
    concurrency: int = 4,
    logs_path: str | None = None,
) -> dict[str, dict]:
    """并发获取五档行情，计算委比。返回 {code: {bid_total, ask_total, weibi}}"""
    top_codes = [c.code for c in candidates if not c.fetch_error][:max_symbols]
    if not top_codes:
        _log("[depth] 跳过五档: 无可用候选", logs_path)
        return {}
    results: dict[str, dict] = {}
    failed: list[str] = []

    def _one(code: str) -> tuple[str, dict | None]:
        try:
            d = client.get_depth(code)
            bid_total = sum(d.get("bid_volumes") or [])
            ask_total = sum(d.get("ask_volumes") or [])
            total = bid_total + ask_total
            weibi = (bid_total - ask_total) / total * 100 if total > 0 else 0.0
            return code, {"bid_total": bid_total, "ask_total": ask_total, "weibi": round(weibi, 1)}
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for code, feat in ex.map(_one, top_codes):
            if feat:
                results[code] = feat
            else:
                failed.append(code)
    sample = ",".join(failed[:8]) if failed else "-"
    _log(
        f"[depth] 五档获取完成: ok={len(results)}/{len(top_codes)}, fail={len(failed)}, sample_fail={sample}",
        logs_path,
    )
    return results


DEPTH_WEIBI_SKIP_THRESHOLD = -40.0


def _run_llm_overlay(
    candidates: list[TailBuyCandidate],
    *,
    llm_routes: list[dict[str, str]],
    style: str,
    max_llm_symbols: int,
    min_rule_score: float,
    allowed_rule_decisions: tuple[str, ...],
    llm_concurrency: int,
    deadline_at: datetime,
    depth_map: dict[str, dict] | None = None,
    logs_path: str | None = None,
) -> tuple[dict[str, dict], int, int, dict[str, int]]:
    if not candidates or max_llm_symbols <= 0:
        return {}, 0, 0, {}
    if not llm_routes:
        _log("LLM 路由未配置，跳过二判，降级为纯规则结果", logs_path)
        return {}, 0, 0, {}

    eligible = [x for x in candidates if not x.fetch_error]
    if not eligible:
        return {}, 0, 0, {}
    top_items = select_llm_overlay_candidates(
        eligible,
        max_llm_symbols=max_llm_symbols,
        min_rule_score=min_rule_score,
        allowed_rule_decisions=allowed_rule_decisions,
    )
    _log(
        "LLM候选过滤: "
        f"eligible={len(eligible)}, selected={len(top_items)}, "
        f"allowed={','.join(allowed_rule_decisions) or 'NONE'}, min_rule_score={min_rule_score:.1f}",
        logs_path,
    )
    if not top_items:
        _log("LLM候选过滤后为空：跳过二判，保留纯规则结果。", logs_path)
        return {}, 0, 0, {}
    total = len(top_items)
    ok = 0
    out: dict[str, dict] = {}
    route_hits: dict[str, int] = {}
    llm_error_counter: Counter[str] = Counter()
    max_workers = max(1, int(llm_concurrency))

    def _judge_one(item: TailBuyCandidate) -> tuple[str, dict | None, str | None]:
        di = (depth_map or {}).get(item.code)
        system_prompt, user_prompt = build_llm_prompt(item, style=style, depth_info=di)
        last_err = ""
        for route in llm_routes:
            left = _remaining_seconds(deadline_at)
            if left <= 8:
                return item.code, None, "deadline_exceeded"
            timeout = int(max(10, min(45, left - 4)))
            route_name = route.get("name", "unknown")
            try:
                text = call_llm(
                    provider=route["provider"],
                    model=route["model"],
                    api_key=route["api_key"],
                    system_prompt=system_prompt,
                    user_message=user_prompt,
                    base_url=(route.get("base_url") or None),
                    timeout=timeout,
                    max_output_tokens=512,
                    allow_truncated_text=True,
                )
                parsed = parse_llm_decision(text)
                if parsed:
                    parsed["model_used"] = route_name
                    return item.code, parsed, None
                last_err = f"{route_name}:llm_parse_failed"
            except Exception as e:
                last_err = f"{route_name}:{e}"
                _log(f"LLM路由失败: code={item.code}, route={route_name}, err={e}", logs_path)
                continue
        return item.code, None, last_err or "all_routes_failed"

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_judge_one, item): item.code for item in top_items}
        timeout_seconds = max(1, int(_remaining_seconds(deadline_at)))
        try:
            for fut in as_completed(futures, timeout=timeout_seconds):
                code = futures[fut]
                try:
                    c, payload, err = fut.result()
                    if payload:
                        out[c] = payload
                        ok += 1
                        route = str(payload.get("model_used", "") or "").strip() or "unknown"
                        route_hits[route] = route_hits.get(route, 0) + 1
                    elif err:
                        llm_error_counter[str(err)] += 1
                        _log(f"LLM二判失败: {code}, err={err}", logs_path)
                except Exception as e:
                    llm_error_counter[f"FutureException:{type(e).__name__}"] += 1
                    _log(f"LLM二判异常: {code}, err={e}", logs_path)
        except FutureTimeout:
            _log("LLM 二判触发 deadline 保护：停止等待剩余结果。", logs_path)
            for fut, code in futures.items():
                if fut.done():
                    continue
                fut.cancel()
                llm_error_counter["deadline_cancelled"] += 1
                _log(f"LLM二判取消: {code}", logs_path)

    if llm_error_counter:
        top_err = " | ".join([f"{k} x{v}" for k, v in llm_error_counter.most_common(5)])
        _log(f"LLM二判失败汇总: {top_err}", logs_path)
    _log(
        f"LLM二判汇总: total={total}, ok={ok}, fail={total - ok}, route_hits={route_hits}",
        logs_path,
    )

    return out, total, ok, route_hits


def _build_llm_routes(
    *,
    primary_provider: str,
    primary_model: str,
    primary_api_key: str,
    primary_base_url: str,
) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _append_route(name: str, provider: str, model: str, api_key: str, base_url: str = "") -> None:
        p = str(provider or "").strip().lower()
        m = str(model or "").strip()
        k = str(api_key or "").strip()
        b = str(base_url or "").strip()
        if not p or not m or not k:
            return
        key = (p, m, b)
        if key in seen:
            return
        seen.add(key)
        routes.append(
            {
                "name": name,
                "provider": p,
                "model": m,
                "api_key": k,
                "base_url": b,
            }
        )

    primary_name = f"{primary_provider}:{primary_model}"
    _append_route(
        name=primary_name,
        provider=primary_provider,
        model=primary_model,
        api_key=primary_api_key,
        base_url=primary_base_url,
    )

    # fallback: NVIDIA Kimi K2（仅当前主路由失败时使用）
    nvidia_key = os.getenv("NVIDIA_API_KEY", "").strip()
    nvidia_base = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()
    nvidia_kimi = os.getenv("NVIDIA_MODEL_KIMI", "").strip()
    if nvidia_key and nvidia_base and nvidia_kimi:
        _append_route(
            name=f"nvidia-kimi:{nvidia_kimi}",
            provider="openai",
            model=nvidia_kimi,
            api_key=nvidia_key,
            base_url=nvidia_base,
        )
    return routes


def _resolve_market_reminder(today_trade_date: str) -> str:
    row = load_market_signal_daily(today_trade_date) or load_latest_market_signal_daily()
    if not row:
        return "market_signal_daily 暂无可用记录（仅提示，不拦截信号）"
    benchmark = str(row.get("benchmark_regime", "UNKNOWN") or "UNKNOWN").strip().upper()
    premarket = str(row.get("premarket_regime", "UNKNOWN") or "UNKNOWN").strip().upper()
    message = str(row.get("banner_message", "") or "").strip()
    if message:
        message = message.replace("\n", " ")
        return f"{benchmark}/{premarket} | {message}"
    return f"{benchmark}/{premarket}（仅风险提示，不拦截买入）"


def _send_notifications(
    *,
    feishu_webhook: str,
    tg_bot_token: str,
    tg_chat_id: str,
    title: str,
    report: str,
    logs_path: str | None = None,
) -> tuple[bool, bool]:
    feishu_ok = False
    tg_ok = False
    if feishu_webhook:
        try:
            use_rich_card = _env_flag("FEISHU_TAIL_BUY_RICH_CARD", True)
            if use_rich_card:
                feishu_ok = bool(send_tail_buy_card(feishu_webhook, title, report))
                if not feishu_ok:
                    _log("Tail Buy 富卡片发送失败，降级为文本卡片重试。", logs_path)
                    feishu_ok = bool(send_feishu_notification(feishu_webhook, title, report))
            else:
                feishu_ok = bool(send_feishu_notification(feishu_webhook, title, report))
        except Exception as e:
            _log(f"飞书推送异常: {e}", logs_path)
            feishu_ok = False
    else:
        _log("FEISHU_WEBHOOK_URL 未配置", logs_path)

    if tg_bot_token and tg_chat_id:
        try:
            tg_ok = bool(send_to_telegram(f"{title}\n\n{report}", tg_bot_token=tg_bot_token, tg_chat_id=tg_chat_id))
        except Exception as e:
            _log(f"Telegram 推送异常: {e}", logs_path)
            tg_ok = False
    else:
        _log("Telegram 未完整配置，跳过 Telegram 推送", logs_path)
    return feishu_ok, tg_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Tail Buy Intraday Job")
    parser.add_argument("--max-llm-symbols", type=int, default=int(os.getenv("TAIL_BUY_LLM_TOP_N", "20")))
    parser.add_argument("--deadline-minute", type=int, default=int(os.getenv("TAIL_BUY_TASK_TIMEOUT_MIN", "25")))
    parser.add_argument(
        "--portfolio-id",
        default=_default_tail_buy_portfolio_id(),
    )
    parser.add_argument("--logs", default=None, help="日志路径")
    args = parser.parse_args()

    started_at = _now()
    logs_path = args.logs or os.path.join(
        os.getenv("LOGS_DIR", "logs"),
        f"tail_buy_1400_{started_at.strftime('%Y%m%d_%H%M%S')}.log",
    )
    deadline_min = max(int(args.deadline_minute or 25), 5)
    deadline_at = started_at + timedelta(minutes=deadline_min)

    feishu_webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    provider = os.getenv("DEFAULT_LLM_PROVIDER", "gemini").strip().lower() or "gemini"
    api_key = (os.getenv(f"{provider.upper()}_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    model = (
        os.getenv(f"{provider.upper()}_MODEL") or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    ).strip() or DEFAULT_GEMINI_MODEL
    llm_base_url = (
        os.getenv(f"{provider.upper()}_BASE_URL") or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "") or ""
    ).strip()
    llm_routes = _build_llm_routes(
        primary_provider=provider,
        primary_model=model,
        primary_api_key=api_key,
        primary_base_url=llm_base_url,
    )
    tickflow_api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    style = os.getenv("TAIL_BUY_STYLE", "auto").strip().lower() or "auto"
    fetch_concurrency = max(int(os.getenv("TAIL_BUY_FETCH_CONCURRENCY", "8")), 1)
    llm_concurrency = max(int(os.getenv("TAIL_BUY_LLM_CONCURRENCY", "4")), 1)
    max_llm_symbols = max(int(args.max_llm_symbols or 20), 0)
    llm_min_rule_score = max(_safe_float(os.getenv("TAIL_BUY_LLM_MIN_RULE_SCORE", "60"), 60.0), 0.0)
    llm_allowed_rule_decisions = tuple(
        x.strip().upper()
        for x in str(os.getenv("TAIL_BUY_LLM_ALLOWED_RULE_DECISIONS", "BUY,WATCH") or "").split(",")
        if x.strip()
    ) or (DECISION_BUY, DECISION_WATCH)
    intraday_limit_per_min = max(int(os.getenv("TAIL_BUY_INTRADAY_LIMIT_PER_MIN", "30")), 1)
    max_over_limit_symbols = max(
        min(int(os.getenv("TAIL_BUY_MAX_OVER_LIMIT_SYMBOLS", "5")), 5),
        0,
    )
    force_over_limit = _env_flag("TAIL_BUY_FORCE_OVER_LIMIT", True)
    tickflow_task_retries = max(int(os.getenv("TAIL_BUY_TICKFLOW_MAX_RETRIES", "1")), 1)
    use_batch_intraday = _env_flag("TAIL_BUY_USE_BATCH_INTRADAY", True)
    intraday_batch_size = max(min(int(os.getenv("TAIL_BUY_INTRADAY_BATCH_SIZE", "200")), 200), 1)
    holding_hard_stop_pct = max(_safe_float(os.getenv("TAIL_BUY_HOLDING_HARD_STOP_PCT", "6"), 6.0), 0.0)
    portfolio_id = str(args.portfolio_id or "USER_LIVE").strip() or "USER_LIVE"

    _log("开始 Tail Buy 任务", logs_path)
    _log(
        f"config: provider={provider}, model={model}, style={style}, "
        f"fetch_concurrency={fetch_concurrency}, llm_concurrency={llm_concurrency}, "
        f"max_llm_symbols={max_llm_symbols}, llm_min_rule_score={llm_min_rule_score}, "
        f"llm_allowed_rule_decisions={','.join(llm_allowed_rule_decisions)}, deadline={deadline_min}m, "
        f"portfolio_id={portfolio_id}, holding_hard_stop_pct={holding_hard_stop_pct}, "
        f"intraday_limit={intraday_limit_per_min}/min, max_over_limit={max_over_limit_symbols}, "
        f"force_over_limit={force_over_limit}, tickflow_retries={tickflow_task_retries}, "
        f"use_batch_intraday={use_batch_intraday}, intraday_batch_size={intraday_batch_size}",
        logs_path,
    )
    _log(
        "LLM routes: " + " -> ".join([x["name"] for x in llm_routes]) if llm_routes else "LLM routes: disabled",
        logs_path,
    )

    if not tickflow_api_key:
        _log(f"缺少 TICKFLOW_API_KEY，Tail Buy 需要分钟级数据。{TICKFLOW_UPGRADE_HINT}", logs_path)
        return 1
    if not feishu_webhook:
        _log("缺少 FEISHU_WEBHOOK_URL，Tail Buy 需要至少配置飞书推送；Telegram 为可选通道。", logs_path)
        return 1
    if not tg_bot_token or not tg_chat_id:
        _log("Telegram 未完整配置，将仅使用飞书推送。", logs_path)

    prev_trade_date, today_trade_date = _resolve_trade_dates(logs_path)
    try:
        pending_candidates, candidate_source_desc = _load_tail_candidates(prev_trade_date, logs_path)
    except Exception as e:
        _log(f"读取候选池失败: {e}", logs_path)
        return 1

    market_reminder = _resolve_market_reminder(today_trade_date)

    tickflow_client = TickFlowClient(
        api_key=tickflow_api_key,
        max_retries=tickflow_task_retries,
    )

    signal_map = {x.code: x for x in pending_candidates}
    holdings, holdings_limit_hit, portfolio_meta = _analyze_holdings_actions(
        tickflow_client=tickflow_client,
        portfolio_id=portfolio_id,
        signal_map=signal_map,
        style=style,
        intraday_batch_size=intraday_batch_size,
        hard_stop_pct=holding_hard_stop_pct,
        deadline_at=deadline_at,
        logs_path=logs_path,
    )
    holdings_section = _build_holdings_markdown(
        holdings=holdings,
        portfolio_meta=portfolio_meta,
        tickflow_limit_hit=holdings_limit_hit,
    )

    scored: list[TailBuyCandidate] = []
    merged: list[TailBuyCandidate] = []
    llm_total = 0
    llm_success = 0
    llm_route_stats: dict[str, int] = {}

    if pending_candidates:
        if use_batch_intraday:
            _log(
                f"规则扫描模式: batch（batch_size={intraday_batch_size}, candidates={len(pending_candidates)}）",
                logs_path,
            )
            scored = _run_rule_scan_batch(
                pending_candidates,
                tickflow_client=tickflow_client,
                style=style,
                batch_size=intraday_batch_size,
                deadline_at=deadline_at,
                logs_path=logs_path,
            )
            hard_batch_fail = sum(1 for x in scored if "TickFlow批量分时拉取失败" in str(x.fetch_error or ""))
            if scored and hard_batch_fail >= len(scored):
                _log("批量接口全部失败，降级到单标的限流模式。", logs_path)
                scored = []

        if not scored:
            to_scan_count, planned_over_limit = _plan_intraday_scan_budget(
                len(pending_candidates),
                limit_per_min=intraday_limit_per_min,
                max_over_limit_symbols=max_over_limit_symbols,
                force_over_limit=force_over_limit,
            )
            to_scan = pending_candidates[:to_scan_count]
            deferred = pending_candidates[to_scan_count:]
            _log(
                f"分时扫描预算(single): total={len(pending_candidates)}, to_scan={len(to_scan)}, "
                f"deferred={len(deferred)}, limit={intraday_limit_per_min}/min, "
                f"planned_over_limit={planned_over_limit}",
                logs_path,
            )
            if deferred:
                defer_reason = (
                    f"限流保护：本轮仅扫描前 {len(to_scan)} 只（TickFlow预算 {intraday_limit_per_min}/min，"
                    f"超限缓冲 <= {max_over_limit_symbols} 只）"
                )
                for item in deferred:
                    item.fetch_error = defer_reason
                    item.rule_reasons = [defer_reason]

            scored_scanned = _run_rule_scan(
                to_scan,
                tickflow_client=tickflow_client,
                style=style,
                fetch_concurrency=fetch_concurrency,
                deadline_at=deadline_at,
                logs_path=logs_path,
            )
            scored = scored_scanned + deferred
            scored.sort(key=lambda x: (-x.rule_score, x.code))

        # ---- 五档行情过滤 ----
        depth_map: dict[str, dict] = {}
        if tickflow_client and _remaining_seconds(deadline_at) > 30:
            depth_map = _fetch_depth_features(
                tickflow_client,
                sorted(
                    [c for c in scored if not c.fetch_error],
                    key=lambda x: (-x.rule_score, x.code),
                ),
                max_symbols=max_llm_symbols,
                concurrency=4,
                logs_path=logs_path,
            )
            skip_cnt = 0
            for c in scored:
                di = depth_map.get(c.code)
                if di and di["weibi"] < DEPTH_WEIBI_SKIP_THRESHOLD and c.rule_decision != "SKIP":
                    c.rule_decision = "SKIP"
                    c.rule_reasons = (c.rule_reasons or []) + [f"五档委比={di['weibi']}%，卖压过重"]
                    skip_cnt += 1
            if skip_cnt:
                _log(f"[depth] 委比过滤: {skip_cnt} 只标的被跳过（阈值<{DEPTH_WEIBI_SKIP_THRESHOLD}%）", logs_path)

        llm_map, llm_total, llm_success, llm_route_stats = _run_llm_overlay(
            scored,
            llm_routes=llm_routes,
            style=style,
            max_llm_symbols=max_llm_symbols,
            min_rule_score=llm_min_rule_score,
            allowed_rule_decisions=llm_allowed_rule_decisions,
            llm_concurrency=llm_concurrency,
            deadline_at=deadline_at,
            depth_map=depth_map,
            logs_path=logs_path,
        )
        merged = merge_rule_and_llm(scored, llm_map)
    else:
        _log("候选池为空：本轮仅输出持仓动作建议。", logs_path)
    elapsed = (_now() - started_at).total_seconds()
    decision_counter = Counter([str(x.final_decision or "").strip() or "UNKNOWN" for x in merged])
    _log(
        "最终决策分布: " + ", ".join([f"{k}={v}" for k, v in sorted(decision_counter.items())]),
        logs_path,
    )
    _log_fetch_error_summary(merged, stage="最终输出", logs_path=logs_path)

    # 持久化 BUY/WATCH 到本地 SQLite
    try:
        from integrations.local_db import init_db, save_tail_buy_results

        init_db()
        persistable = [
            {
                "code": c.code,
                "name": c.name,
                "run_date": started_at.strftime("%Y-%m-%d"),
                "signal_date": c.signal_date,
                "signal_type": c.signal_type,
                "status": c.status,
                "final_decision": c.final_decision,
                "rule_score": c.rule_score,
                "priority_score": c.priority_score,
                "rule_reasons": json.dumps(c.rule_reasons, ensure_ascii=False),
                "llm_decision": c.llm_decision or "",
                "llm_reason": c.llm_reason,
            }
            for c in merged
            if c.final_decision != "SKIP"
        ]
        saved = save_tail_buy_results(persistable)
        _log(f"已写入 {saved} 条尾盘结果到本地 SQLite", logs_path)
        # 持久化 BUY 到 Supabase
        buy_rows = [r for r in persistable if r["final_decision"] == "BUY"]
        if buy_rows:
            from integrations.supabase_tail_buy import save_tail_buy_to_supabase

            n_sb = save_tail_buy_to_supabase(buy_rows)
            _log(f"已写入 {n_sb} 条 BUY 到 Supabase", logs_path)
    except Exception as e:
        _log(f"写入 SQLite 失败（不影响推送）: {e}", logs_path)

    title = f"⏰ Tail Buy {started_at.strftime('%Y-%m-%d')}"
    report = build_tail_buy_markdown(
        now_text=_now_text(),
        target_signal_date=prev_trade_date,
        market_reminder=market_reminder,
        candidates=merged,
        llm_total=llm_total,
        llm_success=llm_success,
        llm_route_plan=[x["name"] for x in llm_routes],
        llm_route_stats=llm_route_stats,
        elapsed_seconds=elapsed,
        extra_sections=[holdings_section],
        extra_sections_first=True,
        candidate_source=candidate_source_desc,
    )
    feishu_ok, tg_ok = _send_notifications(
        feishu_webhook=feishu_webhook,
        tg_bot_token=tg_bot_token,
        tg_chat_id=tg_chat_id,
        title=title,
        report=report,
        logs_path=logs_path,
    )
    _log(
        f"任务结束: candidates={len(merged)}, llm={llm_success}/{llm_total}, "
        f"llm_routes_hit={llm_route_stats}, "
        f"feishu_ok={feishu_ok}, tg_ok={tg_ok}, elapsed={elapsed:.1f}s",
        logs_path,
    )

    if not feishu_ok or not tg_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
