# -*- coding: utf-8 -*-
"""
尾盘买入扫描任务（周一到周五 14:20）：
- 输入：signal_pending（前一交易日 + pending/confirmed）
- 判定：规则全量 + LLM TopN 二判
- 输出：飞书 + Telegram 推送（不写交易表）
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import TABLE_SIGNAL_PENDING
from core.tail_buy_strategy import (
    TailBuyCandidate,
    build_llm_prompt,
    build_tail_buy_markdown,
    evaluate_rule_decision,
    merge_rule_and_llm,
    parse_llm_decision,
    pick_tail_candidates,
)
from integrations.fetch_a_share_csv import _resolve_trading_window
from integrations.llm_client import DEFAULT_GEMINI_MODEL, OPENAI_COMPATIBLE_BASE_URLS, call_llm
from integrations.supabase_base import create_admin_client, is_admin_configured
from integrations.supabase_market_signal import (
    load_latest_market_signal_daily,
    load_market_signal_daily,
)
from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol
from utils.feishu import send_feishu_notification
from utils.notify import send_to_telegram
from utils.trading_clock import resolve_end_calendar_day

TZ = ZoneInfo("Asia/Shanghai")


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


def _resolve_trade_dates(logs_path: str | None = None) -> tuple[str, str]:
    """
    返回 (前一交易日, 当前交易日)。
    """
    end_day = resolve_end_calendar_day()
    try:
        window = _resolve_trading_window(end_calendar_day=end_day, trading_days=2)
        prev_trade = window.start_trade_date.isoformat()
        today_trade = window.end_trade_date.isoformat()
        return prev_trade, today_trade
    except Exception as e:
        prev_trade = (end_day - timedelta(days=1)).isoformat()
        today_trade = end_day.isoformat()
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
        rows = (
            base_query.eq("signal_date", target_signal_date).limit(5000).execute().data
            or []
        )
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
    _log(
        f"候选池加载完成: raw={len(rows)}, picked={len(picked)}, signal_date={target_signal_date}",
        logs_path,
    )
    return picked


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
        candidate.fetch_error = f"TickFlow分钟数据拉取失败: {e}（购买: https://tickflow.org/auth/register?ref=5N4NKTCPL4）"
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
    return scanned


def _run_llm_overlay(
    candidates: list[TailBuyCandidate],
    *,
    llm_routes: list[dict[str, str]],
    style: str,
    max_llm_symbols: int,
    llm_concurrency: int,
    deadline_at: datetime,
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
    top_items = sorted(eligible, key=lambda x: (-x.rule_score, x.code))[:max_llm_symbols]
    total = len(top_items)
    ok = 0
    out: dict[str, dict] = {}
    route_hits: dict[str, int] = {}
    max_workers = max(1, int(llm_concurrency))

    def _judge_one(item: TailBuyCandidate) -> tuple[str, dict | None, str | None]:
        system_prompt, user_prompt = build_llm_prompt(item, style=style)
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
                        _log(f"LLM二判失败: {code}, err={err}", logs_path)
                except Exception as e:
                    _log(f"LLM二判异常: {code}, err={e}", logs_path)
        except FutureTimeout:
            _log("LLM 二判触发 deadline 保护：停止等待剩余结果。", logs_path)
            for fut, code in futures.items():
                if fut.done():
                    continue
                fut.cancel()
                _log(f"LLM二判取消: {code}", logs_path)

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
        _log("TG_BOT_TOKEN/TG_CHAT_ID 未配置", logs_path)
    return feishu_ok, tg_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Tail Buy Intraday Job")
    parser.add_argument("--max-llm-symbols", type=int, default=int(os.getenv("TAIL_BUY_LLM_TOP_N", "20")))
    parser.add_argument("--deadline-minute", type=int, default=int(os.getenv("TAIL_BUY_TASK_TIMEOUT_MIN", "25")))
    parser.add_argument("--logs", default=None, help="日志路径")
    args = parser.parse_args()

    started_at = _now()
    logs_path = args.logs or os.path.join(
        os.getenv("LOGS_DIR", "logs"),
        f"tail_buy_1420_{started_at.strftime('%Y%m%d_%H%M%S')}.log",
    )
    deadline_min = max(int(args.deadline_minute or 25), 5)
    deadline_at = started_at + timedelta(minutes=deadline_min)

    feishu_webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    provider = os.getenv("DEFAULT_LLM_PROVIDER", "gemini").strip().lower() or "gemini"
    api_key = (os.getenv(f"{provider.upper()}_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    model = (os.getenv(f"{provider.upper()}_MODEL") or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    llm_base_url = (
        os.getenv(f"{provider.upper()}_BASE_URL")
        or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "")
        or ""
    ).strip()
    llm_routes = _build_llm_routes(
        primary_provider=provider,
        primary_model=model,
        primary_api_key=api_key,
        primary_base_url=llm_base_url,
    )
    tickflow_api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    style = os.getenv("TAIL_BUY_STYLE", "hybrid").strip().lower() or "hybrid"
    fetch_concurrency = max(int(os.getenv("TAIL_BUY_FETCH_CONCURRENCY", "8")), 1)
    llm_concurrency = max(int(os.getenv("TAIL_BUY_LLM_CONCURRENCY", "4")), 1)
    max_llm_symbols = max(int(args.max_llm_symbols or 20), 0)

    _log("开始尾盘买入扫描任务", logs_path)
    _log(
        f"config: provider={provider}, model={model}, style={style}, "
        f"fetch_concurrency={fetch_concurrency}, llm_concurrency={llm_concurrency}, "
        f"max_llm_symbols={max_llm_symbols}, deadline={deadline_min}m",
        logs_path,
    )
    _log(
        "LLM routes: " + " -> ".join([x["name"] for x in llm_routes]) if llm_routes else "LLM routes: disabled",
        logs_path,
    )

    if not tickflow_api_key:
        _log("缺少 TICKFLOW_API_KEY，尾盘买入扫描需要分钟级数据，请购买 TickFlow: https://tickflow.org/auth/register?ref=5N4NKTCPL4", logs_path)
        return 1
    if not feishu_webhook or not tg_bot_token or not tg_chat_id:
        _log("双通道推送未完整配置（需 FEISHU_WEBHOOK_URL + TG_BOT_TOKEN + TG_CHAT_ID）", logs_path)
        return 1

    prev_trade_date, today_trade_date = _resolve_trade_dates(logs_path)
    try:
        pending_candidates = _load_signal_pending_candidates(prev_trade_date, logs_path)
    except Exception as e:
        _log(f"读取候选池失败: {e}", logs_path)
        return 1

    market_reminder = _resolve_market_reminder(today_trade_date)
    if not pending_candidates:
        report = build_tail_buy_markdown(
            now_text=_now_text(),
            target_signal_date=prev_trade_date,
            market_reminder=market_reminder,
            candidates=[],
            llm_total=0,
            llm_success=0,
            llm_route_plan=[x["name"] for x in llm_routes],
            llm_route_stats={},
            elapsed_seconds=(_now() - started_at).total_seconds(),
        )
        title = f"⏰ 尾盘买入扫描 {started_at.strftime('%Y-%m-%d')}"
        feishu_ok, tg_ok = _send_notifications(
            feishu_webhook=feishu_webhook,
            tg_bot_token=tg_bot_token,
            tg_chat_id=tg_chat_id,
            title=title,
            report=report,
            logs_path=logs_path,
        )
        _log(f"无候选结束: feishu_ok={feishu_ok}, tg_ok={tg_ok}", logs_path)
        return 0 if (feishu_ok and tg_ok) else 1

    tickflow_client = TickFlowClient(api_key=tickflow_api_key)
    scored = _run_rule_scan(
        pending_candidates,
        tickflow_client=tickflow_client,
        style=style,
        fetch_concurrency=fetch_concurrency,
        deadline_at=deadline_at,
        logs_path=logs_path,
    )

    llm_map, llm_total, llm_success, llm_route_stats = _run_llm_overlay(
        scored,
        llm_routes=llm_routes,
        style=style,
        max_llm_symbols=max_llm_symbols,
        llm_concurrency=llm_concurrency,
        deadline_at=deadline_at,
        logs_path=logs_path,
    )
    merged = merge_rule_and_llm(scored, llm_map)
    elapsed = (_now() - started_at).total_seconds()

    title = f"⏰ 尾盘买入扫描 {started_at.strftime('%Y-%m-%d')}"
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
    if not merged:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
