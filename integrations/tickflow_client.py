"""
TickFlow 行情客户端（带重试与超时控制）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

from integrations.tickflow_notice import (
    TICKFLOW_LIMIT_HINT,
    is_tickflow_rate_limited_error,
    record_tickflow_limit_event,
)

TICKFLOW_BASE_URL = os.getenv("TICKFLOW_BASE_URL", "https://api.tickflow.org").strip().rstrip("/")
TICKFLOW_TIMEOUT_SECONDS = max(int(os.getenv("TICKFLOW_TIMEOUT_SECONDS", "12")), 3)
TICKFLOW_MAX_RETRIES = max(int(os.getenv("TICKFLOW_MAX_RETRIES", "3")), 1)
TICKFLOW_RETRY_BACKOFF_SECONDS = max(float(os.getenv("TICKFLOW_RETRY_BACKOFF_SECONDS", "1.5")), 0.1)

_PERIOD_SET = {"1m", "5m", "10m", "15m", "30m", "60m", "1d", "1w", "1M", "1Q", "1Y"}
_CN_TZ = "Asia/Shanghai"
_ADJUST_SET = {"none", "forward", "backward"}
_TICKFLOW_LOG_VERBOSE = os.getenv("TICKFLOW_LOG_VERBOSE", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _tf_log(msg: str, *, always: bool = False) -> None:
    if always or _TICKFLOW_LOG_VERBOSE:
        print(f"[tickflow] {msg}", flush=True)


def _summarize_params(params: dict[str, Any] | None) -> str:
    if not params:
        return "-"
    out: list[str] = []
    for key, value in params.items():
        if key == "symbols":
            items = [x.strip() for x in str(value or "").split(",") if x.strip()]
            head = ",".join(items[:3])
            suffix = "..." if len(items) > 3 else ""
            out.append(f"symbols={len(items)}[{head}{suffix}]")
            continue
        text = str(value)
        if len(text) > 80:
            text = text[:77] + "..."
        out.append(f"{key}={text}")
    return "; ".join(out)


def normalize_cn_symbol(raw: str) -> str:
    """将 A 股 6 位代码标准化为 TickFlow 接口格式：XXXXXX.SH / XXXXXX.SZ。"""
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    if "." in s and len(s.split(".", 1)[0]) == 6:
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 6:
        return s
    if digits.startswith(("0", "3", "2")):
        return f"{digits}.SZ"
    return f"{digits}.SH"


def parse_ohlcv_payload(payload: dict[str, Any]) -> pd.DataFrame:
    """将 TickFlow K线 payload 转为标准 DataFrame。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return pd.DataFrame()
    ts = data.get("timestamp")
    if not isinstance(ts, list) or not ts:
        return pd.DataFrame()

    def _arr(name: str) -> list[float]:
        v = data.get(name)
        if isinstance(v, list):
            return v
        return [None] * len(ts)

    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": _arr("open"),
            "high": _arr("high"),
            "low": _arr("low"),
            "close": _arr("close"),
            "prev_close": _arr("prev_close"),
            "volume": _arr("volume"),
            "amount": _arr("amount"),
        }
    )
    for col in ("open", "high", "low", "close", "prev_close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    dt = pd.to_datetime(df["timestamp"], unit="ms", utc=True, errors="coerce")
    df["datetime"] = dt.dt.tz_convert(_CN_TZ)
    df["date"] = df["datetime"].dt.date.astype(str)
    df = df.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)
    return df


@dataclass
class TickFlowClient:
    api_key: str
    base_url: str = TICKFLOW_BASE_URL
    timeout_seconds: int = TICKFLOW_TIMEOUT_SECONDS
    max_retries: int = TICKFLOW_MAX_RETRIES
    retry_backoff_seconds: float = TICKFLOW_RETRY_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        self.api_key = str(self.api_key or "").strip()
        self.base_url = str(self.base_url or TICKFLOW_BASE_URL).strip().rstrip("/")
        self.timeout_seconds = max(int(self.timeout_seconds), 3)
        self.max_retries = max(int(self.max_retries), 1)
        self.retry_backoff_seconds = max(float(self.retry_backoff_seconds), 0.1)
        if not self.api_key:
            raise ValueError("TICKFLOW_API_KEY 未配置，购买: https://tickflow.org/auth/register?ref=5N4NKTCPL4")

    def _request(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_err: Exception | None = None
        url = f"{self.base_url}{path}"
        headers = {"x-api-key": self.api_key}
        params_summary = _summarize_params(params)
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout_seconds,
                )
                if resp.status_code == 200:
                    elapsed = (time.monotonic() - started) * 1000
                    if attempt > 1:
                        _tf_log(
                            f"recover ok path={path} attempt={attempt}/{self.max_retries} "
                            f"elapsed_ms={elapsed:.0f} params={params_summary}",
                            always=True,
                        )
                    else:
                        _tf_log(
                            f"ok path={path} elapsed_ms={elapsed:.0f} params={params_summary}",
                            always=False,
                        )
                    return resp.json()
                # Cloudflare 1010 / 临时网关错误等，走重试
                body = (resp.text or "").strip()
                if resp.status_code == 429 or "rate_limited" in body.lower():
                    record_tickflow_limit_event(body)
                    _tf_log(
                        f"rate_limited path={path} attempt={attempt}/{self.max_retries} "
                        f"params={params_summary} body={body[:160]}",
                        always=True,
                    )
                    raise RuntimeError(f"TickFlow HTTP 429: {body[:200]}（{TICKFLOW_LIMIT_HINT}）")
                if attempt < self.max_retries and (resp.status_code >= 500 or "error code: 1010" in body.lower()):
                    _tf_log(
                        f"retryable_http path={path} status={resp.status_code} "
                        f"attempt={attempt}/{self.max_retries} params={params_summary}",
                        always=True,
                    )
                    time.sleep(self.retry_backoff_seconds * attempt)
                    continue
                _tf_log(
                    f"http_fail path={path} status={resp.status_code} "
                    f"attempt={attempt}/{self.max_retries} params={params_summary} "
                    f"body={body[:160]}",
                    always=True,
                )
                raise RuntimeError(f"TickFlow HTTP {resp.status_code}: {body[:200]}")
            except Exception as e:  # requests.Timeout / requests.ConnectionError / RuntimeError
                if is_tickflow_rate_limited_error(e):
                    record_tickflow_limit_event(e)
                _tf_log(
                    f"request_error path={path} attempt={attempt}/{self.max_retries} "
                    f"params={params_summary} err={type(e).__name__}: {e}",
                    always=True,
                )
                last_err = e
                if attempt >= self.max_retries:
                    break
                time.sleep(self.retry_backoff_seconds * attempt)
        _tf_log(
            f"request_fail_final path={path} retries={self.max_retries} "
            f"params={params_summary} err={type(last_err).__name__ if last_err else 'Unknown'}: {last_err}",
            always=True,
        )
        raise RuntimeError(f"TickFlow 请求失败: {last_err}")

    def get_klines(
        self,
        symbol: str,
        *,
        period: str = "1d",
        count: int = 300,
        intraday: bool = False,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        adjust: str | None = None,
    ) -> pd.DataFrame:
        p = str(period or "1d").strip()
        if p not in _PERIOD_SET:
            raise ValueError(f"不支持的 period: {p}")
        endpoint = "/v1/klines/intraday" if intraday else "/v1/klines"
        params: dict[str, Any] = {
            "symbol": normalize_cn_symbol(symbol),
            "period": p,
            "count": max(int(count), 1),
        }
        if start_time_ms is not None:
            params["start_time"] = int(start_time_ms)
        if end_time_ms is not None:
            params["end_time"] = int(end_time_ms)
        if not intraday and adjust is not None:
            adj = str(adjust or "").strip().lower()
            if adj not in _ADJUST_SET:
                raise ValueError(f"不支持的 adjust: {adjust}")
            params["adjust"] = adj
        payload = self._request(
            endpoint,
            params=params,
        )
        return parse_ohlcv_payload(payload)

    def get_intraday(self, symbol: str, *, period: str = "1m", count: int = 500) -> pd.DataFrame:
        return self.get_klines(symbol, period=period, count=count, intraday=True)

    def get_intraday_batch(
        self,
        symbols: list[str],
        *,
        period: str = "1m",
        count: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """
        批量查询当日分时 K 线。
        接口: GET /v1/klines/intraday/batch
        返回: { "000001.SZ": DataFrame, ... }
        """
        p = str(period or "1m").strip()
        if p not in _PERIOD_SET:
            raise ValueError(f"不支持的 period: {p}")
        clean = [normalize_cn_symbol(x) for x in symbols if str(x or "").strip()]
        clean = sorted(set(x for x in clean if x))
        if not clean:
            _tf_log("get_intraday_batch skip: no valid symbols", always=True)
            return {}
        _tf_log(
            f"get_intraday_batch request symbols={len(clean)} period={p} count={max(int(count), 1)}",
            always=True,
        )

        payload = self._request(
            "/v1/klines/intraday/batch",
            params={
                "symbols": ",".join(clean),
                "period": p,
                "count": max(int(count), 1),
            },
        )
        raw = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            _tf_log("get_intraday_batch empty payload", always=True)
            return {}

        out: dict[str, pd.DataFrame] = {}
        for sym, kline_payload in raw.items():
            symbol = normalize_cn_symbol(str(sym or "").strip())
            if not symbol or not isinstance(kline_payload, dict):
                continue
            df = parse_ohlcv_payload({"data": kline_payload})
            out[symbol] = df
        _tf_log(f"get_intraday_batch done: received={len(out)}/{len(clean)}", always=True)
        return out

    def get_depth(self, symbol: str) -> dict[str, Any]:
        """获取单个标的五档行情。返回 {bid_prices, bid_volumes, ask_prices, ask_volumes, timestamp}"""
        sym = normalize_cn_symbol(str(symbol or "").strip())
        if not sym:
            return {}
        resp = self._request("/v1/depth", params={"symbol": sym})
        return resp.get("data", {}) if isinstance(resp, dict) else {}

    def get_financial_metrics(self, symbols: list[str], *, latest: bool = True) -> dict[str, list[dict]]:
        """批量获取核心财务指标。返回 {symbol: [MetricsRecord]}"""
        clean = [normalize_cn_symbol(x) for x in symbols if str(x or "").strip()]
        clean = sorted(set(x for x in clean if x))
        if not clean:
            _tf_log("get_financial_metrics skip: no valid symbols", always=True)
            return {}
        _tf_log(
            f"get_financial_metrics request symbols={len(clean)} latest={latest}",
            always=True,
        )
        resp = self._request(
            "/v1/financials/metrics",
            params={"symbols": ",".join(clean), "latest": "true" if latest else "false"},
        )
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            _tf_log("get_financial_metrics empty payload", always=True)
            return {}
        out: dict[str, list[dict]] = {}
        for sym, records in data.items():
            key = normalize_cn_symbol(str(sym).strip())
            if key and isinstance(records, list):
                out[key] = records
        _tf_log(f"get_financial_metrics done: received={len(out)}/{len(clean)}", always=True)
        return out

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        clean = [normalize_cn_symbol(x) for x in symbols if str(x or "").strip()]
        clean = [x for x in clean if x]
        if not clean:
            return {}
        payload = self._request(
            "/v1/quotes",
            params={"symbols": ",".join(sorted(set(clean)))},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            sym = normalize_cn_symbol(str(row.get("symbol", "")).strip())
            if not sym:
                continue
            out[sym] = row
        return out
