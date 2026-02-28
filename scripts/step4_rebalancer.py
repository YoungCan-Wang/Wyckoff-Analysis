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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.ai_prompts import PRIVATE_PM_DECISION_JSON_PROMPT
from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window
from integrations.llm_client import call_llm
from scripts.step3_batch_report import generate_stock_payload

TRADING_DAYS = 500
TELEGRAM_MAX_LEN = 3900
CN_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", "15"))
DEBUG_MODEL_IO = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO_FULL = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class PositionItem:
    code: str
    name: str
    cost: float
    buy_dt: str
    shares: int
    strategy: str


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
    ) -> None:
        self.total_equity = float(max(total_equity, 0.0))
        self.free_cash = float(max(free_cash, 0.0))
        self.position_map = position_map
        self.latest_price_map = latest_price_map

    def process(self, decisions: list[DecisionItem]) -> tuple[list[ExecutionTicket], float]:
        ordered = sorted(decisions, key=lambda d: self.PRIORITY_MAP.get(d.action, 99))
        tickets: list[ExecutionTicket] = []

        for dec in ordered:
            ticket = self._process_one(dec)
            tickets.append(ticket)
        return (tickets, self.free_cash)

    def _process_one(self, dec: DecisionItem) -> ExecutionTicket:
        code = dec.code
        name = dec.name or code
        action = dec.action
        current_price = self.latest_price_map.get(code)
        pos = self.position_map.get(code)
        held_shares = int(pos.shares) if pos else 0

        if current_price is None or current_price <= 0:
            return self._no_trade(dec, name, "缺少最新价格")

        if action == "EXIT":
            sell_shares = int(math.floor(max(held_shares, 0) / 100.0) * 100)
            if sell_shares < 100:
                return self._no_trade(dec, name, "无可卖持仓")
            proceeds = sell_shares * current_price
            self.free_cash += proceeds
            return ExecutionTicket(
                code=code,
                name=name,
                action=action,
                status="APPROVED",
                shares=sell_shares,
                price_hint=current_price,
                amount=proceeds,
                stop_loss=dec.stop_loss,
                max_loss=0.0,
                drawdown_ratio=0.0,
                reason=dec.reason,
                tape_condition=dec.tape_condition,
                invalidate_condition=dec.invalidate_condition,
            )

        if action == "TRIM":
            ratio = dec.trim_ratio if dec.trim_ratio is not None else 0.5
            ratio = min(max(ratio, 0.1), 1.0)
            sell_shares = int(math.floor(held_shares * ratio / 100.0) * 100)
            if sell_shares < 100:
                return self._no_trade(dec, name, "减仓股数不足100股")
            proceeds = sell_shares * current_price
            self.free_cash += proceeds
            return ExecutionTicket(
                code=code,
                name=name,
                action=action,
                status="APPROVED",
                shares=sell_shares,
                price_hint=current_price,
                amount=proceeds,
                stop_loss=dec.stop_loss,
                max_loss=0.0,
                drawdown_ratio=0.0,
                reason=dec.reason,
                tape_condition=dec.tape_condition,
                invalidate_condition=dec.invalidate_condition,
            )

        if action == "HOLD":
            return ExecutionTicket(
                code=code,
                name=name,
                action=action,
                status="APPROVED",
                shares=0,
                price_hint=current_price,
                amount=0.0,
                stop_loss=dec.stop_loss,
                max_loss=0.0,
                drawdown_ratio=0.0,
                reason=dec.reason,
                tape_condition=dec.tape_condition,
                invalidate_condition=dec.invalidate_condition,
            )

        # BUY: PROBE / ATTACK
        if dec.stop_loss is None:
            return self._no_trade(dec, name, "缺少 stop_loss")
        if dec.stop_loss >= current_price:
            return self._no_trade(dec, name, "止损倒挂(stop_loss >= current_price)")

        # 加仓开关约束：标记为加仓时，必须已有持仓且浮盈
        if dec.is_add_on:
            if not pos or held_shares < 100:
                return self._no_trade(dec, name, "is_add_on=true 但无可加仓持仓")
            if pos.cost > 0 and current_price <= pos.cost:
                return self._no_trade(dec, name, "is_add_on=true 但当前未浮盈")

        price_for_calc = current_price
        if dec.entry_zone_min is not None and dec.entry_zone_max is not None:
            price_for_calc = (dec.entry_zone_min + dec.entry_zone_max) / 2.0
            if price_for_calc <= 0:
                price_for_calc = current_price

        # 计算每股真实风险（含滑点）
        assumed_slippage = current_price * self.SLIPPAGE_BPS
        risk_per_share = (current_price - dec.stop_loss) + assumed_slippage
        if risk_per_share <= 0:
            return self._no_trade(dec, name, "风险参数异常(risk_per_share<=0)")

        # 1) 风控允许的最大股数
        max_loss_allowed = self.total_equity * self.RISK_LIMITS[action]
        max_shares_by_risk = max_loss_allowed / risk_per_share

        # 2) 预算与现金允许的最大股数
        budget = min(self.total_equity * self.BUDGET_LIMITS[action], self.free_cash)
        max_shares_by_cash = budget / current_price

        # 3) 取最小值并 A 股整手
        raw_shares = min(max_shares_by_risk, max_shares_by_cash)
        actual_shares = math.floor(raw_shares / 100.0) * 100
        if actual_shares < 100:
            return self._no_trade(dec, name, "计算股数不足100股(触及风控或资金限制)")

        actual_shares = int(actual_shares)
        amount = actual_shares * current_price
        max_loss = actual_shares * risk_per_share
        drawdown_ratio = (max_loss / self.total_equity) if self.total_equity > 0 else 0.0

        self.free_cash -= amount
        return ExecutionTicket(
            code=code,
            name=name,
            action=action,
            status="APPROVED",
            shares=actual_shares,
            price_hint=price_for_calc,
            amount=amount,
            stop_loss=dec.stop_loss,
            max_loss=max_loss,
            drawdown_ratio=drawdown_ratio,
            reason=dec.reason,
            tape_condition=dec.tape_condition,
            invalidate_condition=dec.invalidate_condition,
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
            )
        )

    return PortfolioState(free_cash=free_cash, total_equity=total_equity, positions=positions)


def _job_end_calendar_day() -> date:
    now = datetime.now(CN_TZ)
    if now.hour >= MARKET_CLOSE_HOUR:
        return now.date()
    return (now - timedelta(days=1)).date()


def _fetch_latest_real_close(code: str, window) -> float | None:
    try:
        raw = _fetch_hist(code, window, "")
        df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
        return float(df.iloc[-1]["close"])
    except Exception:
        try:
            raw = _fetch_hist(code, window, "qfq")
            df = normalize_hist_from_fetch(raw).sort_values("date").reset_index(drop=True)
            return float(df.iloc[-1]["close"])
        except Exception:
            return None


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


def _format_position_payload(
    positions: list[PositionItem],
    window,
) -> tuple[str, list[str], float, dict[str, float]]:
    blocks: list[str] = []
    failures: list[str] = []
    live_value_sum = 0.0
    latest_close_map: dict[str, float] = {}

    for pos in positions:
        try:
            raw_qfq = _fetch_hist(pos.code, window, "qfq")
            df_qfq = normalize_hist_from_fetch(raw_qfq).sort_values("date").reset_index(drop=True)

            latest_close = _fetch_latest_real_close(pos.code, window)
            if latest_close is None:
                latest_close = float(df_qfq.iloc[-1]["close"])
                failures.append(f"{pos.code}:real_close_fallback_to_qfq")

            latest_close_map[pos.code] = latest_close
            live_value_sum += latest_close * max(pos.shares, 0)

            pnl_pct = 0.0
            if pos.cost > 0:
                pnl_pct = (latest_close - pos.cost) / pos.cost * 100.0

            meta = (
                f"### 持仓 {pos.code} {pos.name}\n"
                f"- 成本价: {pos.cost:.2f}\n"
                f"- 最新收盘(不复权优先): {latest_close:.2f}\n"
                f"- 浮盈亏: {pnl_pct:+.2f}%\n"
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
            blocks.append(meta + "\n" + payload)
        except Exception as e:
            failures.append(f"{pos.code}:{e}")

    return ("\n\n".join(blocks), failures, live_value_sum, latest_close_map)


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

        confidence = None
        if item.get("confidence") is not None:
            try:
                confidence = float(item.get("confidence"))
            except Exception:
                confidence = None

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
                is_add_on=bool(item.get("is_add_on", False)),
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


def _markdown_to_telegram_html(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append("")
            continue
        if s.startswith("#"):
            out.append(f"<b>{s.lstrip('#').strip()}</b>")
            continue
        if s.startswith("* "):
            out.append("• " + s[2:])
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        out.append(line)
    return "\n".join(out)


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
    html = _markdown_to_telegram_html(message_text)
    chunks = _split_telegram_message(html)
    for idx, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": chat_id,
            "text": chunk if len(chunks) == 1 else f"[{idx}/{len(chunks)}]\n{chunk}",
            "parse_mode": "HTML",
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
    defense = [t for t in tickets if t.status == "APPROVED" and t.action in {"EXIT", "TRIM", "HOLD"}]
    approved_buy = [t for t in tickets if t.status == "APPROVED" and t.action in {"PROBE", "ATTACK"}]
    blocked = [t for t in tickets if t.status != "APPROVED"]

    lines = [
        "🚨 **Alpha-OMS 交易执行工单**",
        f"📅 日期：{now_str} | 净权益：{total_equity:.2f} | 当前可用现金：{free_cash_before:.2f}",
        f"🤖 模型：{model}",
    ]
    if market_view:
        lines.append(f"📌 市场视图：{market_view}")
    lines.append("")

    lines.append("🟥 **[清仓与防守 - EXIT/TRIM/HOLD]**")
    if not defense:
        lines.append("* 无")
    else:
        for t in defense:
            lines.append(
                f"* `{t.code} {t.name}` | **{t.action}** | 止损: "
                f"{('-' if t.stop_loss is None else f'{t.stop_loss:.2f}')}"
            )
            detail = t.reason
            if t.invalidate_condition:
                detail = f"{detail}；失效: {t.invalidate_condition}" if detail else f"失效: {t.invalidate_condition}"
            if t.tape_condition:
                detail = f"{detail}；触发: {t.tape_condition}" if detail else f"触发: {t.tape_condition}"
            if detail:
                lines.append(f"  * 盘面条件：{detail}")
            if t.action in {"EXIT", "TRIM"}:
                lines.append(f"  * 执行股数：{t.shares} 股 | 预计回笼：{t.amount:.2f} 元")
    lines.append("")

    lines.append("🟩 **[核准买入 - APPROVED]**")
    if not approved_buy:
        lines.append("* 无")
    else:
        for t in approved_buy:
            lines.append(f"* `{t.code} {t.name}` | **{t.action}** | 状态: **APPROVED**")
            lines.append(f"  * 挂单区间：参考 {t.price_hint:.2f} 附近（以盘中战区为准）")
            lines.append(f"  * **执行股数：{t.shares} 股** | 预计占用：{t.amount:.2f} 元")
            lines.append(
                f"  * 防守线：{('-' if t.stop_loss is None else f'{t.stop_loss:.2f}')} | "
                f"预估最大回撤：{t.max_loss:.2f} 元 (占组合 {t.drawdown_ratio * 100:.2f}% ✔️)"
            )
            if t.tape_condition:
                lines.append(f"  * 确认条件：{t.tape_condition}")
            if t.invalidate_condition:
                lines.append(f"  * 熔断条件：{t.invalidate_condition}")
            if t.reason:
                lines.append(f"  * 逻辑：{t.reason}")
    lines.append("")

    lines.append("⬛ **[风控拒单 - NO_TRADE]**")
    if not blocked:
        lines.append("* 无")
    else:
        for t in blocked:
            lines.append(f"* `{t.code} {t.name}` | **{t.action}** | 状态: **NO_TRADE**")
            lines.append(f"  * 拦截原因：{t.reason}")
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
        portfolio = load_portfolio_from_env()
    except Exception as e:
        print(f"[step4] 持仓读取失败: {e}")
        return (True, "skipped_invalid_portfolio")

    if not os.getenv("TG_BOT_TOKEN", "").strip() or not os.getenv("TG_CHAT_ID", "").strip():
        print("[step4] TG_BOT_TOKEN/TG_CHAT_ID 未配置，跳过 Step4 推送")
        return (True, "skipped_telegram_unconfigured")

    end_day = _job_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)
    positions_payload, position_failures, live_value, latest_price_map = _format_position_payload(
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
        return (True, "ok")

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
    for d in decisions:
        if d.code in latest_price_map:
            continue
        px = _fetch_latest_real_close(d.code, window)
        if px is not None:
            latest_price_map[d.code] = px

    engine = WyckoffOrderEngine(
        total_equity=float(total_equity),
        free_cash=portfolio.free_cash,
        position_map={p.code: p for p in portfolio.positions},
        latest_price_map=latest_price_map,
    )
    tickets, free_cash_after = engine.process(decisions)

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

    print(f"[step4] 交易工单发送成功: decisions={len(decisions)}, tickets={len(tickets)}, model={model}")
    return (True, "ok")
