# -*- coding: utf-8 -*-
"""
盘中持仓监控（周一到周五 10:00 / 11:00 / 13:30 / 14:30 北京时间）：
- 输入：Supabase 持仓 + TickFlow 实时行情/分钟K线
- 检测：止损穿破、跳空低开、放量滞涨、VWAP破位
- 输出：飞书 + Telegram 推送
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.intraday_sell_signals import PositionSnapshot, SellSignal, scan_position
from integrations.supabase_base import is_admin_configured
from integrations.supabase_portfolio import load_portfolio_state
from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol
from utils.feishu import send_feishu_notification
from utils.notify import send_to_telegram

logger = logging.getLogger(__name__)

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


# ── 环境变量读取 ──────────────────────────────────────────

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return max(int(os.getenv(key, str(default))), 1)
    except (ValueError, TypeError):
        return default


# ── 单只持仓扫描 ──────────────────────────────────────────

def _fetch_and_scan(
    client: TickFlowClient,
    snap: PositionSnapshot,
    thresholds: dict,
    check_gap: bool,
    logs_path: str | None,
) -> list[SellSignal]:
    """获取分钟K线并扫描单只持仓。"""
    symbol = normalize_cn_symbol(snap.code)
    df_1m = None
    df_5m = None
    yday_volume = 0.0

    try:
        df_daily = client.get_klines(symbol, period="1d", count=2, intraday=False)
        if df_daily is not None and len(df_daily) >= 2:
            yday_volume = float(df_daily.iloc[-2]["volume"])
        elif df_daily is not None and len(df_daily) == 1:
            yday_volume = float(df_daily.iloc[0]["volume"])
    except Exception as e:
        _log(f"  {snap.code} 日线获取失败: {e}", logs_path)

    try:
        df_1m = client.get_intraday(symbol, period="1m", count=500)
    except Exception as e:
        _log(f"  {snap.code} 1m获取失败: {e}", logs_path)

    try:
        df_5m = client.get_intraday(symbol, period="5m", count=500)
    except Exception as e:
        _log(f"  {snap.code} 5m获取失败: {e}", logs_path)

    return scan_position(
        snap, df_1m, df_5m, yday_volume,
        hard_pct=thresholds["hard_pct"],
        gap_pct=thresholds["gap_pct"],
        gain_pct=thresholds["gain_pct"],
        vol_ratio=thresholds["vol_ratio"],
        check_gap=check_gap,
    )


# ── 推送消息构建 ──────────────────────────────────────────

def _build_report(
    signals: list[SellSignal],
    total_positions: int,
    time_text: str,
    elapsed_s: float,
) -> str:
    lines: list[str] = []
    lines.append(f"持仓 {total_positions} 只 | 触发 {len(signals)} 个信号")
    lines.append("")

    for sig in signals:
        icon = "\U0001f6a8" if sig.severity == "CRITICAL" else "\u26a0\ufe0f"
        lines.append(f"{icon} **{sig.signal_type}** — {sig.code} {sig.name}")
        lines.append(sig.detail)
        if sig.severity == "CRITICAL":
            lines.append("\u25b6 建议立即止损卖出")
        else:
            lines.append("\u25b6 关注盘中走势，考虑减仓")
        lines.append("")

    safe_codes = set()
    triggered_codes = {s.code for s in signals}
    # safe_codes will be shown at end
    lines.append(f"耗时 {elapsed_s:.1f}s")
    return "\n".join(lines)


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
    else:
        _log("FEISHU_WEBHOOK_URL 未配置", logs_path)

    if tg_bot_token and tg_chat_id:
        try:
            tg_ok = bool(send_to_telegram(f"{title}\n\n{report}", tg_bot_token=tg_bot_token, tg_chat_id=tg_chat_id))
        except Exception as e:
            _log(f"Telegram 推送异常: {e}", logs_path)
    else:
        _log("TG_BOT_TOKEN/TG_CHAT_ID 未配置", logs_path)
    return feishu_ok, tg_ok


# ── 主函数 ────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Intraday Position Monitor")
    parser.add_argument("--deadline-minute", type=int, default=_env_int("MONITOR_TASK_TIMEOUT_MIN", 10))
    parser.add_argument("--portfolio-id", default=os.getenv("MONITOR_PORTFOLIO_ID", "USER_LIVE"))
    parser.add_argument("--logs", default=None, help="日志路径")
    args = parser.parse_args()

    started_at = _now()
    logs_path = args.logs or os.path.join(
        os.getenv("LOGS_DIR", "logs"),
        f"position_monitor_{started_at.strftime('%Y%m%d_%H%M%S')}.log",
    )
    deadline_min = max(args.deadline_minute, 3)
    deadline_at = started_at + timedelta(minutes=deadline_min)

    # 配置
    tickflow_api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    feishu_webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    fetch_concurrency = _env_int("MONITOR_FETCH_CONCURRENCY", 4)
    push_all_clear = os.getenv("MONITOR_PUSH_ALL_CLEAR", "0").strip() == "1"

    thresholds = {
        "hard_pct": _env_float("MONITOR_HARD_STOP_PCT", 7.0),
        "gap_pct": _env_float("MONITOR_GAP_DOWN_PCT", 3.0),
        "gain_pct": _env_float("MONITOR_CLIMAX_GAIN_PCT", 1.0),
        "vol_ratio": _env_float("MONITOR_VWAP_VOL_RATIO", 1.5),
    }

    _log("开始盘中持仓监控", logs_path)
    _log(f"config: portfolio={args.portfolio_id}, concurrency={fetch_concurrency}, deadline={deadline_min}m", logs_path)
    _log(f"thresholds: {thresholds}", logs_path)

    if not tickflow_api_key:
        _log("缺少 TICKFLOW_API_KEY，盘中监控需要实时行情+分钟K线，请购买 TickFlow: https://tickflow.org/auth/register?ref=5N4NKTCPL4", logs_path)
        return 1
    if not is_admin_configured():
        _log("Supabase 凭据未配置，任务失败", logs_path)
        return 1

    # 加载持仓
    state = load_portfolio_state(args.portfolio_id)
    if not state or not state.get("positions"):
        _log("无持仓数据，任务结束", logs_path)
        return 0

    positions = state["positions"]
    _log(f"持仓 {len(positions)} 只: {', '.join(p['code'] for p in positions)}", logs_path)

    # 批量获取实时行情
    client = TickFlowClient(api_key=tickflow_api_key)
    codes = [p["code"] for p in positions]
    try:
        quotes = client.get_quotes(codes)
    except Exception as e:
        _log(f"实时行情获取失败: {e}", logs_path)
        return 1

    if not quotes:
        _log("行情返回为空（可能非交易时段），任务结束", logs_path)
        return 0

    # 构建 PositionSnapshot
    snapshots: list[PositionSnapshot] = []
    for pos in positions:
        sym = normalize_cn_symbol(pos["code"])
        q = quotes.get(sym)
        if not q:
            _log(f"  {pos['code']} 无行情数据，跳过", logs_path)
            continue
        snapshots.append(PositionSnapshot(
            code=pos["code"],
            name=pos.get("name", pos["code"]),
            cost=float(pos.get("cost", 0)),
            shares=int(pos.get("shares", 0)),
            stop_loss=pos.get("stop_loss"),
            current_price=float(q.get("close", 0) or q.get("last", 0) or 0),
            open_price=float(q.get("open", 0) or 0),
            prev_close=float(q.get("prev_close", 0) or q.get("preClose", 0) or 0),
        ))

    if not snapshots:
        _log("所有持仓均无行情数据，任务结束", logs_path)
        return 0

    # 跳空低开只在上午 11:30 前检查
    check_gap = _now().hour < 12

    # 快速路径：先用行情数据检测止损和跳空（无需K线）
    all_signals: list[SellSignal] = []
    for snap in snapshots:
        quick_signals = scan_position(
            snap, None, None, 0,
            hard_pct=thresholds["hard_pct"],
            gap_pct=thresholds["gap_pct"],
            check_gap=check_gap,
        )
        all_signals.extend(quick_signals)

    quick_triggered = {s.code for s in all_signals}
    _log(f"快速路径: {len(all_signals)} 个信号 ({', '.join(quick_triggered) or '无'})", logs_path)

    # 并发获取K线，跑放量滞涨 + VWAP破位
    kline_signals: list[SellSignal] = []
    with ThreadPoolExecutor(max_workers=fetch_concurrency) as pool:
        futures = {}
        for snap in snapshots:
            if _remaining_seconds(deadline_at) <= 5:
                _log("接近 deadline，停止提交K线任务", logs_path)
                break
            fut = pool.submit(_fetch_and_scan, client, snap, thresholds, check_gap, logs_path)
            futures[fut] = snap.code

        for fut in as_completed(futures, timeout=max(_remaining_seconds(deadline_at), 1)):
            code = futures[fut]
            try:
                sigs = fut.result(timeout=3)
                # 只保留K线相关信号（放量滞涨、VWAP破位），避免与快速路径重复
                for s in sigs:
                    if s.signal_type in ("放量滞涨", "VWAP破位"):
                        kline_signals.append(s)
            except Exception as e:
                _log(f"  {code} K线扫描异常: {e}", logs_path)

    all_signals.extend(kline_signals)
    elapsed = (_now() - started_at).total_seconds()
    _log(f"扫描完成: {len(all_signals)} 个信号, 耗时 {elapsed:.1f}s", logs_path)

    # 无信号时根据配置决定是否推送
    if not all_signals:
        if push_all_clear:
            time_hm = _now().strftime("%H:%M")
            title = f"\u2705 持仓监控 {time_hm} 正常"
            report = f"持仓 {len(snapshots)} 只均正常\n耗时 {elapsed:.1f}s"
            _send_notifications(
                feishu_webhook=feishu_webhook, tg_bot_token=tg_bot_token,
                tg_chat_id=tg_chat_id, title=title, report=report, logs_path=logs_path,
            )
        else:
            _log("无信号，不推送", logs_path)
        return 0

    # 构建并推送报告
    time_hm = _now().strftime("%H:%M")
    title = f"\U0001f50d 盘中持仓监控 {time_hm}"
    report = _build_report(all_signals, len(snapshots), time_hm, elapsed)
    feishu_ok, tg_ok = _send_notifications(
        feishu_webhook=feishu_webhook, tg_bot_token=tg_bot_token,
        tg_chat_id=tg_chat_id, title=title, report=report, logs_path=logs_path,
    )
    _log(f"推送完成: feishu={'OK' if feishu_ok else 'FAIL'}, tg={'OK' if tg_ok else 'FAIL'}", logs_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
