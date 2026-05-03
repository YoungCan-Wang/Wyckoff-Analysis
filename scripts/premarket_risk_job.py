"""
盘前风控任务（周一到周五 07:00, Asia/Shanghai）：
- 富时 A50（akshare）
- VIX（优先 Stooq，失败回退 Yahoo）

仅输出风控结论并通知飞书，不执行选股与下单。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import requests

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations.supabase_market_signal import upsert_market_signal_daily
from utils.feishu import send_feishu_notification
from utils.trading_clock import is_a_share_trading_day

TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")
RISK_A50_CRASH_PCT = float(os.getenv("PREMARKET_A50_CRASH_PCT", "-2.0"))
RISK_A50_OFF_PCT = float(os.getenv("PREMARKET_A50_RISK_OFF_PCT", "-1.0"))
RISK_VIX_CRASH_PCT = float(os.getenv("PREMARKET_VIX_CRASH_PCT", "15.0"))
RISK_VIX_CRASH_CLOSE = float(os.getenv("PREMARKET_VIX_CRASH_CLOSE", "25.0"))
RISK_VIX_OFF_PCT = float(os.getenv("PREMARKET_VIX_RISK_OFF_PCT", "8.0"))
PREMARKET_VIX_READY_HOUR_ET = int(os.getenv("PREMARKET_VIX_READY_HOUR_ET", "17"))
PREMARKET_VIX_POLL_INTERVAL_SECONDS = max(
    1,
    int(os.getenv("PREMARKET_VIX_POLL_INTERVAL_SECONDS", "300")),
)


def _build_action_matrix(regime: str) -> list[str]:
    """盘前动作开关（仅门控建议，不直接下单）"""
    if regime == "BLACK_SWAN":
        return [
            "🔒 **盘前动作开关**（BLACK_SWAN）",
            "- ✅ `EXIT`：允许（破位/止损优先执行）",
            "- ✅ `TRIM`：允许（主动降风险）",
            "- ⚠️ `HOLD`：允许（仅守防线，不主观乐观）",
            "- ⛔ `LIGHT_ADD`：禁止",
            "- ⛔ `PROBE`：禁止",
            "- ⛔ `ATTACK`：禁止",
            "- ⛔ `FULL_ATTACK`：禁止",
        ]
    if regime == "CAUTION":
        return [
            "🟠 **盘前动作开关**（CAUTION）",
            "- ✅ `EXIT`：允许",
            "- ✅ `TRIM`：允许",
            "- ✅ `HOLD`：允许（保持防守纪律）",
            "- ⚠️ `LIGHT_ADD`：仅允许对**已有强势浮盈仓位**小幅加仓",
            "- ✅ `PROBE`：允许（仅小仓位试探，盘中需二次确认）",
            "- ⛔ `ATTACK`：默认禁止",
            "- ⛔ `FULL_ATTACK`：禁止",
        ]
    if regime == "RISK_OFF":
        return [
            "🔒 **盘前动作开关**（RISK_OFF）",
            "- ✅ `EXIT`：允许",
            "- ✅ `TRIM`：允许",
            "- ✅ `HOLD`：允许（防守为主）",
            "- ⚠️ `LIGHT_ADD`：仅允许对**已有浮盈仓位**小幅加仓（总权益 <= 5%）",
            "- ⛔ `PROBE`：默认禁止",
            "- ⛔ `ATTACK`：禁止",
            "- ⛔ `FULL_ATTACK`：禁止",
        ]
    return [
        "🔓 **盘前动作开关**（NORMAL）",
        "- ✅ `EXIT`：允许",
        "- ✅ `TRIM`：允许",
        "- ✅ `HOLD`：允许",
        "- ⚠️ `LIGHT_ADD`：条件允许（仅确认强势且量价匹配）",
        "- ⚠️ `PROBE`：条件允许（控制仓位）",
        "- ⚠️ `ATTACK`：条件允许（需盘中二次确认）",
        "- ⛔ `FULL_ATTACK`：默认禁止；仅在强一致 Risk-On 且盘中确认后考虑",
    ]


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, logs_path: str | None = None) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    if logs_path:
        os.makedirs(os.path.dirname(logs_path) or ".", exist_ok=True)
        with open(logs_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _safe_float(v) -> float | None:
    try:
        if v is None:
            return None
        s = str(v).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _premarket_session_trade_date_str() -> str:
    """
    盘前风控服务于“当天即将开盘的会话”，写库键必须是北京时间当天，
    否则晚间 OMS 无法按自然日读取到同一天的盘前红灯。
    """
    return datetime.now(TZ).date().isoformat()


def _parse_trade_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _latest_expected_us_trade_date(now: datetime | None = None) -> date:
    dt_us = now.astimezone(US_TZ) if now else datetime.now(US_TZ)
    candidate = dt_us.date()
    if dt_us.weekday() < 5 and dt_us.hour >= PREMARKET_VIX_READY_HOUR_ET:
        return candidate
    candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _ensure_vix_fresh(raw_date: object, source: str, now: datetime | None = None) -> date:
    trade_date = _parse_trade_date(raw_date)
    if trade_date is None:
        raise RuntimeError(f"{source} date invalid: {raw_date}")
    expected_date = _latest_expected_us_trade_date(now=now)
    if trade_date < expected_date:
        raise RuntimeError(f"{source} stale: latest={trade_date.isoformat()} < expected={expected_date.isoformat()}")
    return trade_date


def _fetch_a50() -> dict:
    out = {
        "ok": False,
        "source": "akshare:futures_global_hist_em(CN00Y)",
        "date": None,
        "close": None,
        "pct_chg": None,
        "error": None,
    }
    try:
        import time

        import akshare as ak

        last_err = None
        for _ in range(3):
            try:
                df = ak.futures_global_hist_em(symbol="CN00Y")
                if df is None or df.empty:
                    raise RuntimeError("A50 empty")
                last = df.iloc[-1]
                pct = _safe_float(last.get("涨幅"))
                close = _safe_float(last.get("最新价"))
                out.update(
                    {
                        "ok": True,
                        "date": str(last.get("日期")),
                        "close": close,
                        "pct_chg": pct,
                    }
                )
                return out
            except Exception as e:  # noqa: PERF203
                last_err = e
                time.sleep(0.4)
        # 兜底：用实时快照表定位 CN00Y
        try:
            spot = ak.futures_global_spot_em()
            if spot is None or spot.empty:
                raise RuntimeError("A50 spot empty")
            hit = spot[spot["代码"].astype(str).str.upper() == "CN00Y"]
            if hit.empty:
                raise RuntimeError("A50 CN00Y not found in spot")
            row = hit.iloc[0]
            out.update(
                {
                    "ok": True,
                    "source": "akshare:futures_global_spot_em(CN00Y)",
                    "date": datetime.now(TZ).strftime("%Y-%m-%d"),
                    "close": _safe_float(row.get("最新价") or row.get("昨结")),
                    "pct_chg": _safe_float(row.get("涨跌幅")),
                }
            )
            return out
        except Exception as e2:
            raise RuntimeError(f"{last_err or 'hist_fail'}; spot_fallback={e2}")
    except Exception as e:
        out["error"] = str(e)
        return out


def _fetch_vix_stooq() -> dict:
    out = {
        "ok": False,
        "source": "stooq:^vix",
        "date": None,
        "close": None,
        "pct_chg": None,
        "error": None,
    }
    try:
        # Stooq 日线 CSV（无需 key）
        url = "https://stooq.com/q/d/l/?s=%5Evix&i=d"
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        rows = list(csv.DictReader(StringIO(resp.text)))
        if len(rows) < 2:
            raise RuntimeError("stooq rows<2")
        last = rows[-1]
        prev = rows[-2]
        c1 = _safe_float(last.get("Close"))
        c0 = _safe_float(prev.get("Close"))
        if c1 is None or c0 is None or c0 == 0:
            raise RuntimeError("stooq close invalid")
        trade_date = _ensure_vix_fresh(last.get("Date"), out["source"])
        pct = (c1 - c0) / c0 * 100.0
        out.update(
            {
                "ok": True,
                "date": trade_date.isoformat(),
                "close": c1,
                "pct_chg": pct,
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def _fetch_vix_cboe() -> dict:
    out = {
        "ok": False,
        "source": "cboe:VIX_History.csv",
        "date": None,
        "close": None,
        "pct_chg": None,
        "error": None,
    }
    try:
        url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        rows = list(csv.DictReader(StringIO(resp.text)))
        if len(rows) < 2:
            raise RuntimeError("cboe rows<2")
        # 从尾部找最近两个有效 close
        valid: list[tuple[str, float]] = []
        for row in reversed(rows):
            c = _safe_float(row.get("CLOSE"))
            d = str(row.get("DATE", "")).strip()
            if c is None or not d:
                continue
            valid.append((d, c))
            if len(valid) >= 2:
                break
        if len(valid) < 2:
            raise RuntimeError("cboe valid close<2")
        d1, c1 = valid[0]
        _, c0 = valid[1]
        if c0 == 0:
            raise RuntimeError("cboe prev close zero")
        trade_date = _ensure_vix_fresh(d1, out["source"])
        pct = (c1 - c0) / c0 * 100.0
        out.update(
            {
                "ok": True,
                "date": trade_date.isoformat(),
                "close": c1,
                "pct_chg": pct,
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def _fetch_vix_yahoo() -> dict:
    out = {
        "ok": False,
        "source": "yahoo:^VIX",
        "date": None,
        "close": None,
        "pct_chg": None,
        "error": None,
    }
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=5d&interval=1d"
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("chart", {}).get("result", [{}])[0]
        timestamps = result.get("timestamp") or []
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        valid = []
        for ts, c in zip(timestamps, closes):
            cv = _safe_float(c)
            if cv is not None:
                valid.append((int(ts), cv))
        if len(valid) < 2:
            raise RuntimeError("yahoo close<2")
        ts1, c1 = valid[-1]
        _, c0 = valid[-2]
        if c0 == 0:
            raise RuntimeError("yahoo prev close zero")
        pct = (c1 - c0) / c0 * 100.0
        dt = datetime.fromtimestamp(ts1, US_TZ).date()
        trade_date = _ensure_vix_fresh(dt.isoformat(), out["source"])
        out.update(
            {
                "ok": True,
                "date": trade_date.isoformat(),
                "close": c1,
                "pct_chg": pct,
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def _fetch_vix() -> dict:
    s0 = _fetch_vix_cboe()
    if s0["ok"]:
        return s0
    s1 = _fetch_vix_stooq()
    if s1["ok"]:
        return s1
    s2 = _fetch_vix_yahoo()
    if s2["ok"]:
        return s2
    return {
        "ok": False,
        "source": "cboe+stooq+yahoo",
        "date": None,
        "close": None,
        "pct_chg": None,
        "error": (f"cboe={s0.get('error')}; stooq={s1.get('error')}; yahoo={s2.get('error')}"),
    }


PREMARKET_VIX_MAX_ATTEMPTS = max(int(os.getenv("PREMARKET_VIX_MAX_ATTEMPTS", "12")), 1)


def _fetch_vix_until_ready(logs_path: str | None = None) -> dict:
    attempt = 1
    while attempt <= PREMARKET_VIX_MAX_ATTEMPTS:
        vix = _fetch_vix()
        if vix.get("ok"):
            _log(
                f"VIX可用，结束轮询: attempt={attempt}, source={vix.get('source')}, date={vix.get('date')}",
                logs_path,
            )
            return vix

        _log(
            "VIX暂不可用，继续轮询: "
            f"attempt={attempt}/{PREMARKET_VIX_MAX_ATTEMPTS}, "
            f"retry_in={PREMARKET_VIX_POLL_INTERVAL_SECONDS}s, error={vix.get('error')}",
            logs_path,
        )
        attempt += 1
        if attempt <= PREMARKET_VIX_MAX_ATTEMPTS:
            time.sleep(PREMARKET_VIX_POLL_INTERVAL_SECONDS)

    # 超过最大重试次数，返回降级结果而非无限挂起
    _log(
        f"VIX轮询超过最大重试次数({PREMARKET_VIX_MAX_ATTEMPTS})，使用降级结果",
        logs_path,
    )
    return {
        "ok": False,
        "source": "timeout_fallback",
        "date": None,
        "close": None,
        "pct_chg": None,
        "error": f"exceeded max attempts ({PREMARKET_VIX_MAX_ATTEMPTS})",
    }


def _judge_regime(a50: dict, vix: dict) -> tuple[str, list[str]]:
    severity_rank = {
        "NORMAL": 0,
        "CAUTION": 1,
        "RISK_OFF": 2,
        "BLACK_SWAN": 3,
    }

    def _escalate(current: str, target: str) -> str:
        return target if severity_rank.get(target, 0) > severity_rank.get(current, 0) else current

    reasons: list[str] = []
    regime = "NORMAL"

    a50_pct = _safe_float(a50.get("pct_chg"))
    vix_close = _safe_float(vix.get("close"))
    vix_pct = _safe_float(vix.get("pct_chg"))

    if a50_pct is not None and a50_pct <= RISK_A50_CRASH_PCT:
        regime = _escalate(regime, "BLACK_SWAN")
        reasons.append(f"A50跌幅 {a50_pct:.2f}% <= {RISK_A50_CRASH_PCT:.2f}%")
    if vix_pct is not None and vix_pct >= RISK_VIX_CRASH_PCT:
        if vix_close is not None and vix_close >= RISK_VIX_CRASH_CLOSE:
            regime = _escalate(regime, "BLACK_SWAN")
            reasons.append(
                f"VIX绝对值 {vix_close:.2f} >= {RISK_VIX_CRASH_CLOSE:.2f} 且涨幅 {vix_pct:.2f}% >= {RISK_VIX_CRASH_PCT:.2f}%"
            )
        else:
            regime = _escalate(regime, "CAUTION")
            if vix_close is None:
                reasons.append(f"VIX涨幅 {vix_pct:.2f}% >= {RISK_VIX_CRASH_PCT:.2f}%（绝对值缺失，按 CAUTION 处理）")
            else:
                reasons.append(
                    f"VIX涨幅 {vix_pct:.2f}% >= {RISK_VIX_CRASH_PCT:.2f}% 但绝对值 {vix_close:.2f} < {RISK_VIX_CRASH_CLOSE:.2f}，按 CAUTION 处理"
                )

    if regime != "BLACK_SWAN":
        if a50_pct is not None and a50_pct <= RISK_A50_OFF_PCT:
            regime = _escalate(regime, "RISK_OFF")
            reasons.append(f"A50跌幅 {a50_pct:.2f}% <= {RISK_A50_OFF_PCT:.2f}%")
        if vix_pct is not None and vix_pct >= RISK_VIX_OFF_PCT:
            is_vix_caution_case = vix_pct >= RISK_VIX_CRASH_PCT and (
                vix_close is None or vix_close < RISK_VIX_CRASH_CLOSE
            )
            if not is_vix_caution_case:
                regime = _escalate(regime, "RISK_OFF")
                reasons.append(f"VIX涨幅 {vix_pct:.2f}% >= {RISK_VIX_OFF_PCT:.2f}%")

    if not reasons:
        reasons.append("A50/VIX 未触发风险阈值")
    return regime, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="盘前风控：A50 + VIX")
    parser.add_argument("--logs", default=None, help="日志文件路径")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不发飞书")
    args = parser.parse_args()

    logs_path = args.logs or os.path.join(
        os.getenv("LOGS_DIR", "logs"),
        f"premarket_risk_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.log",
    )
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()

    _log("盘前风控任务开始", logs_path)
    a50 = _fetch_a50()
    vix = _fetch_vix_until_ready(logs_path)
    regime, reasons = _judge_regime(a50, vix)

    _log(f"A50: {json.dumps(a50, ensure_ascii=False)}", logs_path)
    _log(f"VIX: {json.dumps(vix, ensure_ascii=False)}", logs_path)
    _log(f"风控结论: regime={regime}, reasons={reasons}", logs_path)
    action_lines = _build_action_matrix(regime)
    _log("盘前动作开关: " + " | ".join(action_lines[1:]), logs_path)

    content_parts = [
        f"**当前北京时间**: {_now()}",
        f"**结论**: `{regime}`",
        f"**触发原因**: {'；'.join(reasons)}",
        "",
        f"**A50** ({a50.get('source')}): date={a50.get('date')}, close={a50.get('close')}, pct={a50.get('pct_chg')}",
        f"**VIX** ({vix.get('source')}): date={vix.get('date')}, close={vix.get('close')}, pct={vix.get('pct_chg')}",
        "",
    ]
    if not a50.get("ok") and a50.get("error"):
        content_parts.append(f"**A50注意**: {a50.get('error')}")
    if not vix.get("ok") and vix.get("error"):
        content_parts.append(f"**VIX注意**: {vix.get('error')}")
    if (not a50.get("ok") and a50.get("error")) or (not vix.get("ok") and vix.get("error")):
        content_parts.append("")
    content_parts.extend(action_lines)
    content_parts.extend(
        [
            "",
            "说明：该任务仅做盘前风控与动作门控建议，不执行选股和下单。",
        ]
    )
    content = "\n".join(content_parts)

    if args.dry_run:
        _log("--dry-run: 不发送飞书", logs_path)
        return 0

    trade_date = _premarket_session_trade_date_str()

    if not is_a_share_trading_day(datetime.now(TZ).date()):
        _log(f"非A股交易日({trade_date})，跳过写库", logs_path)
    else:
        db_ok = upsert_market_signal_daily(
            trade_date,
            {
                "a50_value_date": a50.get("date"),
                "a50_source": a50.get("source"),
                "a50_close": a50.get("close"),
                "a50_pct_chg": a50.get("pct_chg"),
                "vix_value_date": vix.get("date"),
                "vix_source": vix.get("source"),
                "vix_close": vix.get("close"),
                "vix_pct_chg": vix.get("pct_chg"),
                "premarket_regime": regime,
                "premarket_reasons": reasons,
                "source_jobs": {
                    "premarket_risk_job": {
                        "updated_at": datetime.now(TZ).isoformat(),
                        "writer": "a50_vix_risk",
                    }
                },
            },
        )
        _log(f"市场信号写库(premarket): ok={db_ok}, trade_date={trade_date}, regime={regime}", logs_path)

    if not webhook:
        _log("FEISHU_WEBHOOK_URL 未配置，跳过飞书发送", logs_path)
        return 0

    ok = send_feishu_notification(webhook, f"⏰ 盘前风控 {datetime.now(TZ).strftime('%Y-%m-%d')}", content)
    if not ok:
        _log("飞书发送失败", logs_path)
        return 1
    _log("飞书发送成功", logs_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
