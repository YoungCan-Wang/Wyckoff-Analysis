from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests


class StrategyApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class StrategyApiConfig:
    base_url: str
    api_key: str
    mode: str
    timeout_seconds: float
    poll_interval_seconds: float
    poll_timeout_seconds: float
    strategy_version: str


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _clean_mode(raw: str) -> str:
    mode = str(raw or "remote").strip().lower()
    if mode in {"api", "required"}:
        return "remote"
    if mode not in {"local", "auto", "remote"}:
        return "remote"
    return mode


def get_strategy_api_config() -> StrategyApiConfig:
    return StrategyApiConfig(
        base_url=str(os.getenv("WYCKOFF_STRATEGY_API_URL", "") or "").strip().rstrip("/"),
        api_key=str(os.getenv("WYCKOFF_STRATEGY_API_KEY", "") or "").strip(),
        mode=_clean_mode(str(os.getenv("WYCKOFF_STRATEGY_API_MODE", "remote") or "remote")),
        timeout_seconds=_env_float("WYCKOFF_STRATEGY_API_TIMEOUT", 30.0),
        poll_interval_seconds=_env_float("WYCKOFF_STRATEGY_API_POLL_INTERVAL", 2.0),
        poll_timeout_seconds=_env_float("WYCKOFF_STRATEGY_API_POLL_TIMEOUT", 600.0),
        strategy_version=str(os.getenv("WYCKOFF_STRATEGY_VERSION", "private-v1") or "private-v1").strip(),
    )


def is_strategy_api_configured(config: StrategyApiConfig | None = None) -> bool:
    cfg = config or get_strategy_api_config()
    return bool(cfg.base_url and cfg.api_key)


def is_strategy_api_required(config: StrategyApiConfig | None = None) -> bool:
    cfg = config or get_strategy_api_config()
    return cfg.mode == "remote"


def is_strategy_api_enabled(config: StrategyApiConfig | None = None) -> bool:
    cfg = config or get_strategy_api_config()
    return cfg.mode == "remote" or (cfg.mode == "auto" and is_strategy_api_configured(cfg))


def _require_config(config: StrategyApiConfig | None = None) -> StrategyApiConfig:
    cfg = config or get_strategy_api_config()
    if not cfg.base_url:
        raise StrategyApiError("WYCKOFF_STRATEGY_API_URL is not configured")
    if not cfg.api_key:
        raise StrategyApiError("WYCKOFF_STRATEGY_API_KEY is not configured")
    return cfg


def _request(method: str, path: str, *, json_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _require_config()
    url = f"{cfg.base_url}{path}"
    try:
        response = requests.request(
            method,
            url,
            headers={"X-API-Key": cfg.api_key, "Accept": "application/json"},
            json=json_payload,
            timeout=cfg.timeout_seconds,
        )
    except requests.RequestException as exc:
        raise StrategyApiError(f"Strategy API request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise StrategyApiError(f"Strategy API {response.status_code}: {detail or response.text[:200]}")
    if not isinstance(payload, dict):
        raise StrategyApiError("Strategy API returned a non-object response")
    return payload


def _clean_code(code: str) -> str:
    text = str(code or "").strip().upper()
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


def _json_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _health_from_rating(rating: str, risk_level: str) -> str:
    if rating in {"strong", "candidate"} and risk_level != "high":
        return "healthy"
    if rating == "watch" or risk_level == "medium":
        return "watch"
    return "avoid"


def _pnl_pct(latest_close: Any, cost: float) -> float | None:
    close = _json_float(latest_close)
    if close is None or cost <= 0:
        return None
    return round((close / float(cost) - 1.0) * 100.0, 2)


def analyze_stock_legacy(
    code: str,
    *,
    name: str | None = None,
    cost: float = 0.0,
    trade_date: str | None = None,
    include_explanation: bool = True,
    user_id: str | None = None,
) -> dict[str, Any]:
    cfg = _require_config()
    data = _request(
        "POST",
        "/v1/analyze",
        json_payload={
            "code": _clean_code(code),
            "name": name,
            "cost": cost,
            "trade_date": trade_date,
            "include_explanation": include_explanation,
            "strategy_version": cfg.strategy_version,
            "user_id": user_id,
        },
    )
    setups = [str(item) for item in data.get("setups") or [] if str(item or "").strip()]
    risk_notes = [str(item) for item in data.get("risk_notes") or [] if str(item or "").strip()]
    latest_close = data.get("latest_close")
    return {
        "source": "strategy_api",
        "code": str(data.get("code") or _clean_code(code)),
        "name": data.get("name") or name or _clean_code(code),
        "health": _health_from_rating(str(data.get("rating") or ""), str(data.get("risk_level") or "")),
        "pnl_pct": _pnl_pct(latest_close, float(cost or 0.0)),
        "latest_close": latest_close,
        "score": data.get("score"),
        "rating": data.get("rating"),
        "risk_level": data.get("risk_level"),
        "ma_pattern": data.get("phase"),
        "l2_channel": setups[0] if setups else data.get("phase"),
        "track": data.get("phase"),
        "accum_stage": data.get("phase"),
        "l4_triggers": setups[1:] if len(setups) > 1 else [],
        "health_reasons": risk_notes,
        "formatted_text": data.get("explanation") or "",
        "data_status": "ok",
        "latest_date": data.get("trade_date") or trade_date,
        "strategy_version": data.get("strategy_version"),
    }


def screen_stocks_legacy(
    *,
    board: str = "all",
    universe: list[str] | None = None,
    top_n: int = 20,
    trade_date: str | None = None,
) -> dict[str, Any]:
    cfg = _require_config()
    board_norm = str(board or "all").strip().lower()
    board_norm = {
        "gem": "chinext",
        "创业板": "chinext",
        "主板": "main",
        "全部": "all",
        "main_chinext": "all",
        "main-chinext": "all",
        "main+chinext": "all",
    }.get(board_norm, board_norm)
    if board_norm not in {"all", "main", "chinext"}:
        raise StrategyApiError(f"Unsupported strategy API board: {board}")
    top_n = max(1, min(int(top_n or 20), 200))
    data = _request(
        "POST",
        "/v1/screen",
        json_payload={
            "trade_date": trade_date,
            "universe": [_clean_code(code) for code in universe] if universe else None,
            "board": board_norm,
            "top_n": top_n,
            "strategy_version": cfg.strategy_version,
        },
    )
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    symbols_for_report: list[dict[str, Any]] = []
    trigger_rows: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        row = {
            "code": str(item.get("code") or ""),
            "name": str(item.get("name") or item.get("code") or ""),
            "score": _json_float(item.get("score")) or 0.0,
            "priority_score": _json_float(item.get("score")) or 0.0,
            "track": str(item.get("phase") or ""),
            "tag": "strategy_api",
            "risk_level": str(item.get("risk_level") or ""),
            "reasons": item.get("reasons") or [],
        }
        symbols_for_report.append(row)
        trigger_rows.append({"code": row["code"], "name": row["name"], "score": row["score"]})
    total_scanned = int(data.get("total_scanned") or len(symbols_for_report))
    return {
        "ok": True,
        "source": "strategy_api",
        "strategy_version": data.get("strategy_version"),
        "trade_date": data.get("trade_date"),
        "summary": {
            "total_scanned": total_scanned,
            "layer1_passed": total_scanned,
            "layer2_passed": len(symbols_for_report),
            "layer3_passed": len(symbols_for_report),
        },
        "trigger_groups": {"strategy_api": trigger_rows},
        "top_sectors": [],
        "symbols_for_report": symbols_for_report,
        "candidates": candidates,
    }


def wait_for_task(task_id: str) -> dict[str, Any]:
    cfg = _require_config()
    deadline = time.monotonic() + cfg.poll_timeout_seconds
    while True:
        task = _request("GET", f"/v1/tasks/{task_id}")
        status = str(task.get("status") or "")
        if status == "completed":
            return task
        if status == "failed":
            raise StrategyApiError(str(task.get("error") or "Strategy API task failed"))
        if time.monotonic() >= deadline:
            raise StrategyApiError(f"Strategy API task timed out: {task_id}")
        time.sleep(max(0.2, cfg.poll_interval_seconds))


def run_backtest_legacy(
    *,
    start: str,
    end: str,
    hold_days: int,
    top_n: int,
    board: str,
    stop_loss_pct: float,
    take_profit_pct: float | None,
) -> dict[str, Any]:
    cfg = _require_config()
    accepted = _request(
        "POST",
        "/v1/backtest",
        json_payload={
            "start_date": start,
            "end_date": end,
            "board": board,
            "top_n": top_n,
            "hold_days": [hold_days],
            "stop_loss": [stop_loss_pct],
            "take_profit": [take_profit_pct],
            "strategy_version": cfg.strategy_version,
        },
    )
    task = wait_for_task(str(accepted.get("task_id") or ""))
    result = task.get("result") or {}
    if not isinstance(result, dict):
        raise StrategyApiError("Strategy API task result is not an object")
    best = result.get("best") or {}
    if not isinstance(best, dict):
        best = {}
    return {
        "source": "strategy_api",
        "period": f"{start} ~ {end}",
        "hold_days": best.get("hold_days", hold_days),
        "top_n": result.get("top_n", top_n),
        "board": board,
        "stop_loss_pct": best.get("stop_loss_pct", stop_loss_pct),
        "take_profit_pct": best.get("take_profit_pct", take_profit_pct),
        "trades": best.get("trades", 0),
        "win_rate_pct": best.get("win_rate_pct"),
        "avg_ret_pct": best.get("avg_ret_pct"),
        "median_ret_pct": best.get("median_ret_pct"),
        "sharpe_ratio": best.get("sharpe_ratio"),
        "max_drawdown_pct": best.get("max_drawdown_pct"),
        "portfolio_total_ret_pct": best.get("portfolio_total_ret_pct"),
        "portfolio_ann_ret_pct": best.get("portfolio_ann_ret_pct"),
        "rows": result.get("rows") or [],
        "task_id": task.get("task_id"),
        "strategy_version": result.get("strategy_version"),
    }


def score_tail_buy_remote(
    *,
    candidates: list[dict[str, Any]],
    intraday_by_code: dict[str, list[dict[str, Any]]],
    style: str = "auto",
) -> dict[str, Any]:
    cfg = _require_config()
    return _request(
        "POST",
        "/v1/tail-buy/score",
        json_payload={
            "strategy_version": cfg.strategy_version,
            "candidates": candidates,
            "intraday_by_code": intraday_by_code,
            "style": style,
        },
    )


def prepare_tail_buy_remote(
    *,
    candidates: list[dict[str, Any]],
    depth_by_code: dict[str, dict[str, Any]] | None = None,
    style: str = "auto",
    max_llm_symbols: int = 20,
    llm_min_rule_score: float = 60.0,
    llm_allowed_rule_decisions: list[str] | tuple[str, ...] = ("BUY", "WATCH"),
) -> dict[str, Any]:
    cfg = _require_config()
    return _request(
        "POST",
        "/v1/tail-buy/prepare",
        json_payload={
            "strategy_version": cfg.strategy_version,
            "candidates": candidates,
            "depth_by_code": depth_by_code or {},
            "style": style,
            "max_llm_symbols": max(int(max_llm_symbols), 0),
            "llm_min_rule_score": max(float(llm_min_rule_score), 0.0),
            "llm_allowed_rule_decisions": [str(x).strip().upper() for x in llm_allowed_rule_decisions if str(x).strip()],
        },
    )


def finalize_tail_buy_remote(
    *,
    candidates: list[dict[str, Any]],
    llm_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cfg = _require_config()
    return _request(
        "POST",
        "/v1/tail-buy/finalize",
        json_payload={
            "strategy_version": cfg.strategy_version,
            "candidates": candidates,
            "llm_outputs": llm_outputs,
        },
    )


def analyze_tail_buy_holdings_remote(
    *,
    holdings: list[dict[str, Any]],
    style: str = "auto",
    hard_stop_pct: float = 6.0,
) -> dict[str, Any]:
    cfg = _require_config()
    return _request(
        "POST",
        "/v1/tail-buy/holdings",
        json_payload={
            "strategy_version": cfg.strategy_version,
            "holdings": holdings,
            "style": style,
            "hard_stop_pct": hard_stop_pct,
        },
    )
