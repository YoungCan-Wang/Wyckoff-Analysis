# -*- coding: utf-8 -*-
"""
阶段 4：私人账户再平衡决策
读取私密持仓（Secret）+ Step3 外部候选研报 + 大盘水温，交给 LLM 输出 Buy/Hold/Sell，
并通过 Telegram 私密发送完整结果。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from integrations.ai_prompts import PRIVATE_PM_SYSTEM_PROMPT
from integrations.fetch_a_share_csv import _fetch_hist, _resolve_trading_window
from integrations.llm_client import call_llm
from scripts.step3_batch_report import generate_stock_payload
from core.wyckoff_engine import normalize_hist_from_fetch

TRADING_DAYS = 500
TELEGRAM_MAX_LEN = 3900
DEBUG_MODEL_IO = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO_FULL = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {"1", "true", "yes", "on"}
CN_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", "15"))


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


def load_portfolio_from_env(env_key: str = "MY_PORTFOLIO_STATE") -> PortfolioState:
    """
    从环境变量读取私密账本 JSON。
    约定该配置由 Secret 提供，格式：
    {
      "free_cash": 100000,
      "positions": [
        {"code":"000001","name":"示例股","cost":10.0,"buy_dt":"20260101","shares":1000,"strategy":"示例策略"}
      ],
      "total_equity": 200000  # 可选，不填则运行时自动推导
    }
    """
    raw = os.getenv(env_key, "").strip()
    if not raw:
        raise ValueError(f"{env_key} 未配置")

    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"{env_key} 不是合法 JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"{env_key} 必须是对象 JSON")

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
        if not code:
            print(f"[step4] 跳过非法持仓#{idx}: 缺少 code")
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
    return PortfolioState(
        free_cash=free_cash,
        total_equity=total_equity,
        positions=positions,
    )


def _dump_model_input(
    portfolio: PortfolioState,
    model: str,
    system_prompt: str,
    user_message: str,
) -> str:
    if not DEBUG_MODEL_IO:
        return ""

    logs_dir = os.getenv("LOGS_DIR", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, f"step4_model_input_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    symbols_line = ", ".join(f"{x.code}" for x in portfolio.positions) or "(no_positions)"
    body = (
        f"[step4] model={model}\n"
        f"[step4] position_count={len(portfolio.positions)}\n"
        f"[step4] positions={symbols_line}\n"
        f"[step4] system_prompt_len={len(system_prompt)}\n"
        f"[step4] user_message_len={len(user_message)}\n"
    )
    if DEBUG_MODEL_IO_FULL:
        body += (
            "\n===== SYSTEM PROMPT =====\n"
            f"{system_prompt}\n"
            "\n===== USER MESSAGE =====\n"
            f"{user_message}\n"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[step4] 模型输入已落盘: {path}")
    return path


def _split_telegram_message(content: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(content) <= max_len:
        return [content]
    parts = content.split("\n\n")
    chunks: list[str] = []
    cur = ""
    for p in parts:
        candidate = p if not cur else f"{cur}\n\n{p}"
        if len(candidate) <= max_len:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(p) <= max_len:
            cur = p
            continue
        start = 0
        while start < len(p):
            chunks.append(p[start:start + max_len])
            start += max_len
    if cur:
        chunks.append(cur)
    return chunks


def _normalize_for_telegram_text(content: str) -> str:
    """
    Telegram Markdown 容易因特殊字符导致整段降级失败；
    这里转为稳定纯文本样式，保证可读性。
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            out.append(title)
            continue
        if stripped.startswith(("- ", "* ")):
            out.append("• " + stripped[2:].strip())
            continue
        out.append(line)
    text = "\n".join(out)
    text = text.replace("**", "").replace("`", "")
    return text.strip()


def _job_end_calendar_day() -> date:
    """
    定时任务统一口径：
    - 北京时间收盘后（默认 >=15:00）走 T+0（当天）
    - 收盘前走 T-1（上一自然日）
    """
    now = datetime.now(CN_TZ)
    if now.hour >= MARKET_CLOSE_HOUR:
        return now.date()
    return (now - timedelta(days=1)).date()


def send_to_telegram(message_text: str) -> bool:
    import requests

    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[step4] TG_BOT_TOKEN/TG_CHAT_ID 未配置，跳过 Telegram 推送")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    normalized = _normalize_for_telegram_text(message_text)
    chunks = _split_telegram_message(normalized)
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        text = chunk if total == 1 else f"[{idx}/{total}]\n{chunk}"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                continue
            print(f"[step4] Telegram 推送失败 part={idx}/{total}, status={resp.status_code}, body={resp.text[:300]}")
            return False
        except Exception as e:
            print(f"[step4] Telegram 推送异常 part={idx}/{total}: {e}")
            return False
    return True


def _format_position_payload(positions: list[PositionItem], window) -> tuple[str, list[str], float]:
    blocks: list[str] = []
    failures: list[str] = []
    live_value_sum = 0.0
    for pos in positions:
        try:
            raw_qfq = _fetch_hist(pos.code, window, "qfq")
            df_qfq = normalize_hist_from_fetch(raw_qfq).sort_values("date").reset_index(drop=True)
            latest_close = float(df_qfq.iloc[-1]["close"])

            # 浮盈亏/市值统一使用真实价格（不复权）优先，qfq 仅用于结构分析
            try:
                raw_real = _fetch_hist(pos.code, window, "")
                df_real = normalize_hist_from_fetch(raw_real).sort_values("date").reset_index(drop=True)
                latest_close = float(df_real.iloc[-1]["close"])
            except Exception as e:
                failures.append(f"{pos.code}:real_close_fallback_to_qfq({e})")

            live_value_sum += latest_close * max(pos.shares, 0)
            profit_pct = 0.0
            if pos.cost > 0:
                profit_pct = (latest_close - pos.cost) / pos.cost * 100.0

            meta = (
                f"### 持仓 {pos.code} {pos.name}\n"
                f"- 成本价: {pos.cost:.2f}\n"
                f"- 最新收盘: {latest_close:.2f}\n"
                f"- 浮盈亏: {profit_pct:+.2f}%\n"
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
    return ("\n\n".join(blocks), failures, live_value_sum)


def run(
    external_report: str,
    benchmark_context: dict | None,
    api_key: str,
    model: str,
) -> tuple[bool, str]:
    """
    Step 4 执行入口：
    - 读取私密账本（MY_PORTFOLIO_STATE）
    - 读取并分析持仓切片
    - 融合 Step3 外部候选研报
    - 输出私人 Buy/Hold/Sell 决策并发到 Telegram
    """
    if not api_key or not api_key.strip():
        return (False, "missing_api_key")

    try:
        portfolio = load_portfolio_from_env()
    except Exception as e:
        print(f"[step4] 读取持仓失败: {e}")
        return (True, "skipped_invalid_portfolio")

    if not os.getenv("TG_BOT_TOKEN", "").strip() or not os.getenv("TG_CHAT_ID", "").strip():
        print("[step4] TG_BOT_TOKEN/TG_CHAT_ID 未配置，跳过 Step4 推送")
        return (True, "skipped_telegram_unconfigured")

    end_day = _job_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)
    positions_payload, position_failures, live_positions_value = _format_position_payload(portfolio.positions, window)
    # total_equity 允许不传：默认按"现金 + 当前持仓市值"推导
    inferred_total_equity = portfolio.free_cash + live_positions_value
    total_equity_for_prompt = (
        portfolio.total_equity if portfolio.total_equity is not None else inferred_total_equity
    )

    benchmark_lines: list[str] = []
    if benchmark_context:
        benchmark_lines.append("[宏观水温 / Benchmark Context]")
        benchmark_lines.append(
            f"regime={benchmark_context.get('regime')}, "
            f"close={benchmark_context.get('close')}, "
            f"ma50={benchmark_context.get('ma50')}, "
            f"ma200={benchmark_context.get('ma200')}, "
            f"ma50_slope_5d={benchmark_context.get('ma50_slope_5d')}, "
            f"recent3_pct={benchmark_context.get('recent3_pct')}, "
            f"recent3_cum_pct={benchmark_context.get('recent3_cum_pct')}"
        )

    user_message = (
        ("{}\n\n".format("\n".join(benchmark_lines)) if benchmark_lines else "")
        + "[账户状态]\n"
        + f"free_cash={portfolio.free_cash:.2f}\n"
        + f"total_equity={total_equity_for_prompt:.2f}\n"
        + f"position_count={len(portfolio.positions)}\n\n"
        + "[交易执行硬约束]\n"
        + "- 禁止单点价格指令，必须给“结构战区(Action Zone) + 盘面确认条件(Tape Condition)”。\n"
        + "- 战区需围绕输入里的价格锚点（最新收盘价）描述，不得僵化为固定数字挂单。\n"
        + "- 买入触发必须有量价确认条件；若放量下破战区，必须取消买入并观望。\n"
        + "- 强势突破标的必须给“防踏空 1/3 试单”条件；若条件不满足，禁止追高。\n\n"
        + "[内部持仓量价切片]\n"
        + (positions_payload if positions_payload else "当前无持仓，仅现金。")
        + "\n\n[外部候选（来自 Step3 研报）]\n"
        + (external_report.strip() if external_report and external_report.strip() else "无外部候选报告（本轮 Step3 未产出有效文本）。")
    )
    if position_failures:
        user_message += "\n\n[持仓拉取失败]\n" + "\n".join(f"- {x}" for x in position_failures)
    if portfolio.total_equity is None:
        user_message += "\n\n[账户净值说明]\n- total_equity 未在配置中提供，已按 free_cash + 持仓最新市值自动推导。"

    _dump_model_input(
        portfolio=portfolio,
        model=model,
        system_prompt=PRIVATE_PM_SYSTEM_PROMPT,
        user_message=user_message,
    )

    try:
        report = call_llm(
            provider="gemini",
            model=model,
            api_key=api_key,
            system_prompt=PRIVATE_PM_SYSTEM_PROMPT,
            user_message=user_message,
            timeout=300,
        )
    except Exception as e:
        print(f"[step4] 模型调用失败: {e}")
        return (False, "llm_failed")

    sent = send_to_telegram(report)
    if not sent:
        return (False, "telegram_failed")

    print(f"[step4] 私人再平衡决策发送成功，持仓数={len(portfolio.positions)}")
    return (True, "ok")
