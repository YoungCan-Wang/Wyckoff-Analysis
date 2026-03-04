# -*- coding: utf-8 -*-
"""
定时任务主入口：Wyckoff Funnel → 批量研报

配置来源：仅读取环境变量（GitHub Secrets），与 Streamlit 用户配置（Supabase）完全独立。
环境变量：FEISHU_WEBHOOK_URL, GEMINI_API_KEY, GEMINI_MODEL,
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY(可选), SUPABASE_USER_ID,
TG_BOT_TOKEN, TG_CHAT_ID, MY_PORTFOLIO_STATE(可选兜底)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TZ = ZoneInfo("Asia/Shanghai")
STEP3_REASON_MAP = {
    "data_all_failed": "OHLCV 全部拉取失败",
    "llm_failed": "大模型调用失败",
    "feishu_failed": "飞书推送失败",
    "skipped_no_symbols": "无输入股票，已跳过",
    "no_data_but_no_error": "无可用数据",
}
STEP4_REASON_MAP = {
    "missing_api_key": "GEMINI_API_KEY 缺失",
    "skipped_invalid_portfolio": "用户持仓缺失或格式错误，已跳过",
    "skipped_telegram_unconfigured": "Telegram 未配置，已跳过",
    "skipped_idempotency": "今日已运行，已跳过",
    "skipped_no_decisions": "模型未给出有效决策，已跳过",
    "llm_failed": "Step4 模型调用失败",
    "telegram_failed": "Telegram 推送失败",
    "ok": "ok",
}


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, logs_path: str | None = None) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    if logs_path:
        os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
        with open(logs_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _load_step4_target() -> tuple[dict | None, str]:
    target_user_id = os.getenv("SUPABASE_USER_ID", "").strip()
    if not target_user_id:
        return None, "SUPABASE_USER_ID 未配置"

    portfolio_id = f"USER_LIVE:{target_user_id}"
    try:
        from integrations.supabase_portfolio import load_portfolio_state
    except Exception as e:
        return None, f"supabase portfolio 读取器不可用: {e}"

    # 强制按唯一 user_id 读取目标账户
    p = load_portfolio_state(portfolio_id)
    has_env_fallback = bool(os.getenv("MY_PORTFOLIO_STATE", "").strip())
    if not isinstance(p, dict) and not has_env_fallback:
        return None, f"未匹配到 user_id={target_user_id} 的持仓（{portfolio_id}）"

    return {
        "user_id": target_user_id,
        "portfolio_id": portfolio_id,
    }, ("ok_supabase" if isinstance(p, dict) else "ok_env_fallback")


def main() -> int:
    parser = argparse.ArgumentParser(description="每日定时任务：Wyckoff Funnel → 批量研报")
    parser.add_argument("--dry-run", action="store_true", help="仅校验配置，不执行任务")
    parser.add_argument("--logs", default=None, help="日志文件路径，默认 logs/daily_job_YYYYMMDD_HHMMSS.log")
    args = parser.parse_args()

    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").strip() or "gemini-2.5-flash-lite"

    logs_path = args.logs or os.path.join(
        os.getenv("LOGS_DIR", "logs"),
        f"daily_job_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.log",
    )

    # Secret 完整性预检
    missing = []
    if not webhook:
        missing.append("FEISHU_WEBHOOK_URL")
    if not api_key:
        missing.append("GEMINI_API_KEY")
    if missing:
        _log(f"配置缺失: {', '.join(missing)}", logs_path)
        return 1

    if args.dry_run:
        _log("--dry-run: 配置校验通过，退出", logs_path)
        return 0

    # 定时任务日线口径：禁用 akshare/efinance，优先 baostock，失败再 tushare。
    os.environ["DATA_SOURCE_DISABLE_AKSHARE"] = "1"
    os.environ["DATA_SOURCE_DISABLE_EFINANCE"] = "1"

    from scripts.wyckoff_funnel import run as run_step2
    from scripts.step3_batch_report import run as run_step3
    from scripts.step4_rebalancer import run as run_step4

    summary: list[dict] = []
    has_blocking_failure = False
    symbols_info: list[dict] = []
    benchmark_context: dict = {}
    step3_report_text = ""

    _log("开始定时任务", logs_path)

    # 阶段 1：Wyckoff Funnel
    t0 = datetime.now(TZ)
    step2_ok = False
    step2_err = None
    try:
        step2_ok, symbols_info, benchmark_context = run_step2(webhook)
        step2_err = None if step2_ok else "飞书发送失败"
    except Exception as e:
        step2_err = str(e)
    elapsed2 = (datetime.now(TZ) - t0).total_seconds()
    summary.append({
        "step": "Wyckoff Funnel",
        "ok": step2_ok and step2_err is None,
        "err": step2_err,
        "elapsed_s": round(elapsed2, 1),
        "output": f"{len(symbols_info)} symbols",
    })
    _log(f"阶段 1 Wyckoff Funnel: ok={step2_ok}, symbols={len(symbols_info)}, elapsed={elapsed2:.1f}s, err={step2_err}", logs_path)
    if step2_err:
        has_blocking_failure = True

    # 阶段 2：批量研报（可降级：失败不影响 Funnel 成功）
    step3_ok = True
    step3_err = None
    if symbols_info:
        t0 = datetime.now(TZ)
        try:
            step3_ok, step3_reason, step3_report_text = run_step3(
                symbols_info, webhook, api_key, model, benchmark_context=benchmark_context
            )
            step3_err = None if step3_ok else STEP3_REASON_MAP.get(step3_reason, step3_reason)
        except Exception as e:
            step3_ok = False
            step3_err = str(e)
        elapsed3 = (datetime.now(TZ) - t0).total_seconds()
        summary.append({
            "step": "批量研报",
            "ok": step3_ok and step3_err is None,
            "err": step3_err,
            "elapsed_s": round(elapsed3, 1),
            "output": f"{len(symbols_info)} symbols",
        })
        _log(f"阶段 2 批量研报: ok={step3_ok}, elapsed={elapsed3:.1f}s, err={step3_err}", logs_path)
    else:
        summary.append({"step": "批量研报", "ok": True, "err": None, "elapsed_s": 0, "output": "skipped (no symbols)"})
        _log("阶段 2 批量研报: 跳过（无筛选结果）", logs_path)

    # 阶段 3：私人账户再平衡（按 SUPABASE_USER_ID 唯一执行）
    step4_target, step4_target_reason = _load_step4_target()
    if not step4_target:
        summary.append({
            "step": "私人再平衡",
            "ok": True,
            "err": None,
            "elapsed_s": 0,
            "output": f"skipped ({step4_target_reason})",
        })
        _log(f"阶段 3 私人再平衡: 跳过（{step4_target_reason}）", logs_path)
    else:
        tg_bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
        tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
        if not tg_bot_token or not tg_chat_id:
            summary.append({
                "step": "私人再平衡",
                "ok": True,
                "err": None,
                "elapsed_s": 0,
                "output": "skipped (TG_BOT_TOKEN/TG_CHAT_ID 未配置)",
            })
            _log("阶段 3 私人再平衡: 跳过（TG_BOT_TOKEN/TG_CHAT_ID 未配置）", logs_path)
            step4_target = None
        if step4_target is None:
            pass
        else:
            t0 = datetime.now(TZ)
            user_id = str(step4_target.get("user_id", "") or "").strip()
            portfolio_id = str(step4_target.get("portfolio_id", "") or "").strip()
            step4_ok = True
            step4_reason = "ok"
            step4_err = None
            try:
                step4_ok, step4_reason = run_step4(
                    external_report=step3_report_text,
                    benchmark_context=benchmark_context,
                    api_key=api_key,
                    model=model,
                    portfolio_id=portfolio_id,
                    tg_bot_token=tg_bot_token,
                    tg_chat_id=tg_chat_id,
                )
                step4_err = None if step4_ok else STEP4_REASON_MAP.get(step4_reason, step4_reason)
            except Exception as e:
                step4_ok = False
                step4_reason = "unexpected_exception"
                step4_err = str(e)
            elapsed4 = (datetime.now(TZ) - t0).total_seconds()
            summary.append({
                "step": "私人再平衡",
                "ok": step4_ok and step4_err is None,
                "err": step4_err,
                "elapsed_s": round(elapsed4, 1),
                "output": (
                    f"user={user_id}, portfolio={portfolio_id}, reason={step4_reason}"
                ),
            })
            _log(
                f"阶段 3 私人再平衡: user={user_id}, portfolio={portfolio_id}, "
                f"ok={step4_ok}, reason={step4_reason}, elapsed={elapsed4:.1f}s, err={step4_err}",
                logs_path,
            )

    # 汇总
    total_elapsed = sum(s.get("elapsed_s", 0) for s in summary)
    _log("", logs_path)
    _log("=== 阶段汇总 ===", logs_path)
    for s in summary:
        status = "✅" if s["ok"] else "❌"
        _log(f"  {status} {s['step']}: {s.get('elapsed_s', 0)}s, {s.get('output', '')}" + (f" | {s['err']}" if s.get("err") else ""), logs_path)
    _log(f"总耗时: {total_elapsed:.1f}s", logs_path)
    _log("定时任务结束", logs_path)

    # 阻断型失败：Funnel 失败
    if has_blocking_failure:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
