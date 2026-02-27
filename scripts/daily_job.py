# -*- coding: utf-8 -*-
"""
定时任务主入口：Wyckoff Funnel → 批量研报

配置来源：仅读取环境变量（GitHub Secrets），与 Streamlit 用户配置（Supabase）完全独立。
环境变量：FEISHU_WEBHOOK_URL, GEMINI_API_KEY, GEMINI_MODEL
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


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, logs_path: str | None = None) -> None:
    line = f"[{_now()}] {msg}"
    print(line)
    if logs_path:
        os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
        with open(logs_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="每日定时任务：Wyckoff Funnel → 批量研报")
    parser.add_argument("--dry-run", action="store_true", help="仅校验配置，不执行任务")
    parser.add_argument("--logs", default=None, help="日志文件路径，默认 logs/daily_job_YYYYMMDD_HHMMSS.log")
    args = parser.parse_args()

    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"

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

    from scripts.wyckoff_funnel import run as run_step2
    from scripts.step3_batch_report import run as run_step3

    summary: list[dict] = []
    has_blocking_failure = False
    symbols_info: list[dict] = []
    benchmark_context: dict = {}

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
            step3_ok, step3_reason = run_step3(
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
