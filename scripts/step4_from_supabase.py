"""
Run Step4 directly from today's Supabase recommendation_tracking rows.

This intentionally skips Step2 funnel and Step3 report generation. It reuses
the rows already written by the daily funnel and treats is_ai_recommended=true
as the Step3 operation-pool candidates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

# Ensure project root is on sys.path for direct script invocation.
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import TABLE_RECOMMENDATION_TRACKING
from integrations._llm_types import DEFAULT_GEMINI_MODEL
from integrations.supabase_base import close_client, create_admin_client
from scripts.daily_job import TZ, _latest_trade_date_str, _load_step4_target, _log, _run_step4_pipeline


def _norm_code(raw: object) -> str:
    text = str(raw or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return digits.zfill(6)[-6:]
    return text


def _resolve_recommend_date(raw: str) -> int:
    text = str(raw or "").strip()
    if not text:
        text = os.getenv("STEP4_RECOMMEND_DATE", "").strip()
    if not text:
        return int(_latest_trade_date_str().replace("-", ""))
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 8:
        raise ValueError(f"invalid recommend date: {raw!r}")
    return int(digits)


def _load_recommendations(recommend_date: int) -> tuple[list[dict], list[str]]:
    client = create_admin_client()
    try:
        resp = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select(
                "code,name,recommend_reason,initial_price,current_price,"
                "funnel_score,is_ai_recommended,recommend_count,recommend_date"
            )
            .eq("recommend_date", recommend_date)
            .execute()
        )
        rows = resp.data or []
    finally:
        close_client(client)

    symbols_info: list[dict] = []
    for row in rows:
        code = _norm_code(row.get("code"))
        if not code:
            continue
        score = row.get("funnel_score")
        symbols_info.append(
            {
                "code": code,
                "name": str(row.get("name") or code).strip(),
                "tag": str(row.get("recommend_reason") or "").strip(),
                "score": score,
                "priority_score": score,
                "funnel_score": score,
                "initial_price": row.get("initial_price"),
                "current_price": row.get("current_price"),
                "recommend_count": row.get("recommend_count"),
                "source_type": "supabase_recommendation_tracking",
            }
        )

    ai_codes = sorted({_norm_code(row.get("code")) for row in rows if row.get("is_ai_recommended")})
    ai_codes = [code for code in ai_codes if code]
    return symbols_info, ai_codes


def _build_external_report(recommend_date: int, symbols_info: list[dict], ai_codes: list[str]) -> str:
    ai_set = set(ai_codes)
    lines = [
        f"Supabase复用今日Step3起跳板候选，recommend_date={recommend_date}，候选={', '.join(ai_codes)}。"
    ]
    for item in symbols_info:
        if item["code"] not in ai_set:
            continue
        lines.append(
            f"- {item['code']} {item['name']} | {item.get('tag') or '-'} | score={item.get('funnel_score')}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Step4 from Supabase recommendation_tracking")
    parser.add_argument("--recommend-date", default="", help="YYYYMMDD or YYYY-MM-DD; defaults to latest trade date")
    parser.add_argument("--logs", default="", help="Log file path")
    args = parser.parse_args()

    recommend_date = _resolve_recommend_date(args.recommend_date)
    logs_path = args.logs or os.path.join(
        os.getenv("LOGS_DIR", "logs"),
        f"step4_from_supabase_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.log",
    )

    _log(f"Step4 direct run: loading Supabase recommendations date={recommend_date}", logs_path)
    symbols_info, ai_codes = _load_recommendations(recommend_date)
    _log(
        f"Step4 direct run: recommendation_rows={len(symbols_info)}, "
        f"ai_codes={len(ai_codes)} ({', '.join(ai_codes) if ai_codes else 'none'})",
        logs_path,
    )
    if not symbols_info:
        _log(f"Step4 direct run: no recommendation rows for date={recommend_date}", logs_path)
        return 1
    if not ai_codes:
        _log(f"Step4 direct run: no AI recommended rows for date={recommend_date}", logs_path)
        return 1

    step4_target, step4_target_reason = _load_step4_target()
    if not step4_target:
        _log(f"Step4 direct run: target unavailable ({step4_target_reason})", logs_path)
        return 1
    _log(f"Step4 direct run: target={step4_target['portfolio_id']}", logs_path)

    provider = os.getenv("DEFAULT_LLM_PROVIDER", "gemini").strip().lower() or "gemini"
    api_key = (os.getenv(f"{provider.upper()}_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    model = (
        os.getenv(f"{provider.upper()}_MODEL") or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    ).strip() or DEFAULT_GEMINI_MODEL

    summary = _run_step4_pipeline(
        step4_target=step4_target,
        symbols_info=symbols_info,
        step3_springboard_codes=ai_codes,
        step3_report_text=_build_external_report(recommend_date, symbols_info, ai_codes),
        benchmark_context={},
        api_key=api_key,
        model=model,
        logs_path=logs_path,
    )
    _log("Step4 direct run summary: " + json.dumps(summary, ensure_ascii=False), logs_path)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
