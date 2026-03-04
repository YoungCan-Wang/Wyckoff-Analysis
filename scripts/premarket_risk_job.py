# -*- coding: utf-8 -*-
"""
盘前风控任务（周一到周五 08:30, Asia/Shanghai）：
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
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.feishu import send_feishu_notification

TZ = ZoneInfo("Asia/Shanghai")
RISK_A50_CRASH_PCT = float(os.getenv("PREMARKET_A50_CRASH_PCT", "-2.0"))
RISK_A50_OFF_PCT = float(os.getenv("PREMARKET_A50_RISK_OFF_PCT", "-1.0"))
RISK_VIX_CRASH_PCT = float(os.getenv("PREMARKET_VIX_CRASH_PCT", "15.0"))
RISK_VIX_OFF_PCT = float(os.getenv("PREMARKET_VIX_RISK_OFF_PCT", "8.0"))


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
        import akshare as ak
        import time

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
        pct = (c1 - c0) / c0 * 100.0
        out.update(
            {
                "ok": True,
                "date": str(last.get("Date", "")),
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
        pct = (c1 - c0) / c0 * 100.0
        out.update(
            {
                "ok": True,
                "date": d1,
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
        result = (
            payload.get("chart", {})
            .get("result", [{}])[0]
        )
        timestamps = result.get("timestamp") or []
        closes = (
            result.get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
        )
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
        dt = datetime.fromtimestamp(ts1, TZ).strftime("%Y-%m-%d")
        out.update(
            {
                "ok": True,
                "date": dt,
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
        "error": (
            f"cboe={s0.get('error')}; "
            f"stooq={s1.get('error')}; "
            f"yahoo={s2.get('error')}"
        ),
    }


def _judge_regime(a50: dict, vix: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    regime = "NORMAL"

    a50_pct = _safe_float(a50.get("pct_chg"))
    vix_pct = _safe_float(vix.get("pct_chg"))

    if a50_pct is not None and a50_pct <= RISK_A50_CRASH_PCT:
        regime = "BLACK_SWAN"
        reasons.append(f"A50跌幅 {a50_pct:.2f}% <= {RISK_A50_CRASH_PCT:.2f}%")
    if vix_pct is not None and vix_pct >= RISK_VIX_CRASH_PCT:
        regime = "BLACK_SWAN"
        reasons.append(f"VIX涨幅 {vix_pct:.2f}% >= {RISK_VIX_CRASH_PCT:.2f}%")

    if regime != "BLACK_SWAN":
        if a50_pct is not None and a50_pct <= RISK_A50_OFF_PCT:
            regime = "RISK_OFF"
            reasons.append(f"A50跌幅 {a50_pct:.2f}% <= {RISK_A50_OFF_PCT:.2f}%")
        if vix_pct is not None and vix_pct >= RISK_VIX_OFF_PCT:
            regime = "RISK_OFF"
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
    vix = _fetch_vix()
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
        f"**A50** ({a50.get('source')}): "
        f"date={a50.get('date')}, close={a50.get('close')}, pct={a50.get('pct_chg')}",
        f"**VIX** ({vix.get('source')}): "
        f"date={vix.get('date')}, close={vix.get('close')}, pct={vix.get('pct_chg')}",
        "",
    ]
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

    if not webhook:
        _log("FEISHU_WEBHOOK_URL 未配置，跳过飞书发送", logs_path)
        return 0

    ok = send_feishu_notification(
        webhook, f"⏰ 盘前风控 {datetime.now(TZ).strftime('%Y-%m-%d')}", content
    )
    if not ok:
        _log("飞书发送失败", logs_path)
        return 1
    _log("飞书发送成功", logs_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
