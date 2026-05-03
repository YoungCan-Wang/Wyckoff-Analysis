from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback

logger = logging.getLogger(__name__)
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


def _sanitize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize(x) for x in obj]
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)


def _write_result(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(_sanitize(payload), f, ensure_ascii=False, indent=2)


def _load_payload(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _apply_funnel_env(payload: dict[str, Any]) -> None:
    pool_mode = str(payload.get("pool_mode", "") or "").strip().lower()
    if pool_mode in {"manual", "board"}:
        os.environ["FUNNEL_POOL_MODE"] = pool_mode
    board = str(payload.get("board", "") or "").strip().lower()
    if board:
        os.environ["FUNNEL_POOL_BOARD"] = board
    manual_symbols = str(payload.get("manual_symbols", "") or "").strip()
    if manual_symbols:
        os.environ["FUNNEL_POOL_MANUAL_SYMBOLS"] = manual_symbols
    limit_count = payload.get("limit_count")
    if limit_count not in {None, ""}:
        os.environ["FUNNEL_POOL_LIMIT_COUNT"] = str(limit_count)

    env_map = {
        "trading_days": "FUNNEL_TRADING_DAYS",
        "max_workers": "FUNNEL_MAX_WORKERS",
        "batch_size": "FUNNEL_BATCH_SIZE",
        "min_market_cap_yi": "FUNNEL_CFG_MIN_MARKET_CAP_YI",
        "min_avg_amount_wan": "FUNNEL_CFG_MIN_AVG_AMOUNT_WAN",
        "ma_short": "FUNNEL_CFG_MA_SHORT",
        "ma_long": "FUNNEL_CFG_MA_LONG",
        "ma_hold": "FUNNEL_CFG_MA_HOLD",
        "top_n_sectors": "FUNNEL_CFG_TOP_N_SECTORS",
        "spring_support_window": "FUNNEL_CFG_SPRING_SUPPORT_WINDOW",
        "lps_vol_dry_ratio": "FUNNEL_CFG_LPS_VOL_DRY_RATIO",
        "evr_vol_ratio": "FUNNEL_CFG_EVR_VOL_RATIO",
    }
    for key, env_name in env_map.items():
        value = payload.get(key)
        if value not in {None, ""}:
            os.environ[env_name] = str(value)


def _run_funnel_screen(request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _apply_funnel_env(payload)
    from core.funnel_pipeline import run_funnel

    ok, symbols_for_report, benchmark_context, details = run_funnel(
        "",
        notify=False,
        return_details=True,
    )
    metrics = details.get("metrics", {}) or {}
    triggers = details.get("triggers", {}) or {}
    name_map = details.get("name_map", {}) or {}
    sector_map = details.get("sector_map", {}) or {}

    trigger_groups: dict[str, list[dict[str, Any]]] = {}
    unique_hit_codes: set[str] = set()
    for trigger_name, rows in triggers.items():
        group_rows: list[dict[str, Any]] = []
        for code, score in rows:
            code_s = str(code).strip()
            if code_s:
                unique_hit_codes.add(code_s)
            group_rows.append(
                {
                    "code": code_s,
                    "name": str(name_map.get(code_s, code_s)),
                    "industry": str(sector_map.get(code_s, "") or "未知行业"),
                    "score": float(score),
                }
            )
        trigger_groups[str(trigger_name)] = group_rows

    return {
        "request_id": request_id,
        "job_kind": "funnel_screen",
        "ok": bool(ok),
        "benchmark_context": benchmark_context,
        "metrics": metrics,
        "summary": {
            "total_symbols": int(metrics.get("total_symbols", 0) or 0),
            "layer1": int(metrics.get("layer1", 0) or 0),
            "layer2": int(metrics.get("layer2", 0) or 0),
            "layer3": int(metrics.get("layer3", 0) or 0),
            "l4_unique_hits": int(len(unique_hit_codes)),
            "selected_for_ai": int(len(details.get("selected_for_ai", []) or [])),
        },
        "trigger_groups": trigger_groups,
        "symbols_for_report": symbols_for_report,
        "selected_for_ai": details.get("selected_for_ai", []) or [],
        "trend_selected": details.get("trend_selected", []) or [],
        "accum_selected": details.get("accum_selected", []) or [],
        "top_sectors": metrics.get("top_sectors", []) or [],
        "content_preview": str(details.get("content", "") or "")[:4000],
    }


def _resolve_model_credentials(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    from integrations.llm_client import DEFAULT_GEMINI_MODEL, OPENAI_COMPATIBLE_BASE_URLS, SUPPORTED_PROVIDERS

    user_id = str(payload.get("user_id", "") or "").strip()
    provider = str(payload.get("provider", "") or "gemini").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        provider = "gemini"
    api_key = str(payload.get("api_key", "") or "").strip()
    model = str(payload.get("model", "") or "").strip()
    base_url = str(payload.get("base_url", "") or "").strip()

    key_field = f"{provider}_api_key"
    model_field = f"{provider}_model"
    base_url_field = f"{provider}_base_url"
    env_api_key = f"{provider.upper()}_API_KEY"
    env_model = f"{provider.upper()}_MODEL"
    env_base_url = f"{provider.upper()}_BASE_URL"
    if user_id:
        from integrations.supabase_portfolio import load_user_settings_admin

        settings = load_user_settings_admin(user_id) or {}
        custom_providers = settings.get("custom_providers") or {}
        if isinstance(custom_providers, str):
            try:
                custom_providers = json.loads(custom_providers)
            except Exception:
                custom_providers = {}
        if not isinstance(custom_providers, dict):
            custom_providers = {}

        api_key = str(settings.get(key_field, "") or "").strip()
        if not model:
            model = str(settings.get(model_field, "") or "").strip()
        if not base_url:
            base_url = str(settings.get(base_url_field, "") or "").strip()

        provider_entry = custom_providers.get(provider) or {}
        if isinstance(provider_entry, dict):
            if not api_key:
                api_key = str(provider_entry.get("apikey") or provider_entry.get("api_key") or "").strip()
            if not model:
                model = str(provider_entry.get("model") or "").strip()
            if not base_url:
                base_url = str(provider_entry.get("baseurl") or provider_entry.get("base_url") or "").strip()
    if not api_key:
        api_key = str(os.getenv(env_api_key, "") or "").strip()
    if not api_key and provider == "gemini":
        api_key = str(os.getenv("GEMINI_API_KEY", "") or "").strip()
    if not model:
        model = str(os.getenv(env_model, "") or "").strip()
    if not model and provider == "gemini":
        model = str(os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL) or "").strip()
    if not base_url:
        base_url = str(os.getenv(env_base_url, "") or "").strip()
    if not base_url:
        base_url = str(OPENAI_COMPATIBLE_BASE_URLS.get(provider, "") or "").strip()
    if not api_key:
        logger.warning("[resolve_credentials] 未找到可用的 %s API Key，将以 noLLM 模式运行", provider)
    if not model and api_key:
        raise ValueError(f"未找到可用的 {provider} 模型名（payload / 用户配置 / 环境变量均为空）")
    return provider, api_key, model, base_url


def _run_batch_ai_report(request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    symbols_info = payload.get("symbols_info")
    if not isinstance(symbols_info, list) or not symbols_info:
        raise ValueError("symbols_info 为空")

    preview_only = bool(payload.get("preview_only"))
    if preview_only:
        os.environ["STEP3_SKIP_LLM"] = "1"

    provider, api_key, model, base_url = _resolve_model_credentials(payload)
    webhook_url = str(payload.get("webhook_url", "") or "").strip()
    benchmark_context = payload.get("benchmark_context", {}) or {}

    from core.batch_report import run_step3

    ok, reason, report_text = run_step3(
        symbols_info,
        webhook_url=webhook_url,
        api_key=api_key,
        model=model,
        benchmark_context=benchmark_context,
        notify=bool(webhook_url),
        provider=provider,
        llm_base_url=base_url,
    )
    return {
        "request_id": request_id,
        "job_kind": "batch_ai_report",
        "ok": bool(ok),
        "reason": str(reason or ""),
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "webhook_url": webhook_url,
        "preview_only": preview_only,
        "symbol_count": len(symbols_info),
        "symbols_info": symbols_info,
        "benchmark_context": benchmark_context,
        "report_text": str(report_text or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Actions 后台量化作业")
    parser.add_argument("--job-kind", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--payload-json", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    payload = _load_payload(args.payload_json)
    requested_by_user_id = str(payload.get("user_id", "") or "").strip()

    # 注入用户配置的环境变量（Tushare Token 等）
    if requested_by_user_id:
        try:
            from integrations.supabase_portfolio import load_user_settings_admin

            user_settings = load_user_settings_admin(requested_by_user_id)
            if user_settings:
                ts_token = str(user_settings.get("tushare_token") or "").strip()
                if ts_token:
                    os.environ["TUSHARE_TOKEN"] = ts_token
                    # print(f"[web_background_job] 已注入用户 {requested_by_user_id[:8]} 的 Tushare Token")
        except Exception as e:
            print(f"[web_background_job] 注入用户配置失败: {e}")

    base_result: dict[str, Any] = {
        "request_id": args.request_id,
        "job_kind": args.job_kind,
        "requested_by_user_id": requested_by_user_id,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "success",
    }

    try:
        if args.job_kind == "funnel_screen":
            result = _run_funnel_screen(args.request_id, payload)
        elif args.job_kind == "batch_ai_report":
            result = _run_batch_ai_report(args.request_id, payload)
        else:
            raise ValueError(f"不支持的 job_kind: {args.job_kind}")
        base_result.update(result)
    except Exception as e:
        base_result.update(
            {
                "status": "error",
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
        )
        _write_result(args.output, base_result)
        print(base_result["traceback"], file=sys.stderr)
        return 1

    _write_result(args.output, base_result)
    print(
        f"[web_background_job] finished kind={args.job_kind} request_id={args.request_id} "
        f"user_id={requested_by_user_id or '-'}"
    )
    if not base_result.get("ok", True):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
