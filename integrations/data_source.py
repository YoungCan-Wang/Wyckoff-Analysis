# Copyright (c) 2024-2026 youngcan. All Rights Reserved.
# 本代码仅供个人学习研究使用，未经授权不得用于商业目的。
# 商业授权请联系作者支付授权费用。

"""
统一数据源：个股日线 tickflow 优先（qfq）→ tushare → akshare→baostock→efinance；大盘 tushare 直连

输出格式与 akshare 兼容：日期, 开盘, 最高, 最低, 收盘, 成交量, 成交额, 涨跌幅, 换手率, 振幅
"""

from __future__ import annotations

import atexit
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta, timezone
from http.client import RemoteDisconnected
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

import pandas as pd

from integrations.tickflow_notice import (
    TICKFLOW_LIMIT_HINT,
    is_tickflow_rate_limited_error,
    record_tickflow_limit_event,
)

_BAOSTOCK_LOGGED = False
_BAOSTOCK_EXIT_HOOKED = False
_BAOSTOCK_MODULE = None
_BAOSTOCK_LOCK = threading.RLock()
_SPOT_SNAPSHOT_TTL_SECONDS = int(os.getenv("SPOT_SNAPSHOT_TTL_SECONDS", "20"))
_SPOT_SNAPSHOT_TIMEOUT_SECONDS = float(os.getenv("SPOT_SNAPSHOT_TIMEOUT_SECONDS", "8.0"))
_SPOT_SNAPSHOT_TS = 0.0
_SPOT_SNAPSHOT_MAP: dict[str, dict[str, float | None]] = {}
_SPOT_SNAPSHOT_LOCK = threading.RLock()
_SPOT_TURNOVER_MAX_REL_ERR = float(os.getenv("SPOT_TURNOVER_MAX_REL_ERR", "0.35"))
_DATA_SOURCE_DEBUG = os.getenv("DATA_SOURCE_DEBUG", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_BAOSTOCK_MAX_SECONDS = float(os.getenv("BAOSTOCK_MAX_SECONDS", "6.0"))
_BAOSTOCK_SOCKET_TIMEOUT = float(os.getenv("BAOSTOCK_SOCKET_TIMEOUT", "3.0"))
_BAOSTOCK_CIRCUIT_THRESHOLD = int(os.getenv("BAOSTOCK_CIRCUIT_THRESHOLD", "10"))
_AKSHARE_RETRY_TIMES = max(int(os.getenv("AKSHARE_RETRY_TIMES", "2")), 1)
_AKSHARE_RETRY_SLEEP_SECONDS = float(os.getenv("AKSHARE_RETRY_SLEEP_SECONDS", "0.8"))
_BAOSTOCK_CONSEC_FAILS = 0
_BAOSTOCK_CIRCUIT_OPEN = False
_BAOSTOCK_CIRCUIT_NOTE = ""
_TICKFLOW_CLIENT = None
_TICKFLOW_CLIENT_READY = False
_TICKFLOW_DAILY_MAX_COUNT = max(int(os.getenv("TICKFLOW_DAILY_MAX_COUNT", "10000")), 64)
_TICKFLOW_LIMIT_NOTICE_EMITTED = False
_TICKFLOW_LIMIT_NOTICE_LOCK = threading.Lock()


def _debug_source_fail(source: str, err: Exception) -> None:
    if _DATA_SOURCE_DEBUG:
        print(f"[data_source] {source} failed: {type(err).__name__}: {err}")


def _baostock_circuit_state() -> tuple[bool, str]:
    with _BAOSTOCK_LOCK:
        return (_BAOSTOCK_CIRCUIT_OPEN, _BAOSTOCK_CIRCUIT_NOTE)


def _baostock_mark_success() -> None:
    global _BAOSTOCK_CONSEC_FAILS
    with _BAOSTOCK_LOCK:
        _BAOSTOCK_CONSEC_FAILS = 0


def _baostock_mark_failure(reason: str) -> None:
    global _BAOSTOCK_CONSEC_FAILS, _BAOSTOCK_CIRCUIT_OPEN, _BAOSTOCK_CIRCUIT_NOTE
    with _BAOSTOCK_LOCK:
        _BAOSTOCK_CONSEC_FAILS += 1
        if (
            not _BAOSTOCK_CIRCUIT_OPEN
            and _BAOSTOCK_CIRCUIT_THRESHOLD > 0
            and _BAOSTOCK_CONSEC_FAILS >= _BAOSTOCK_CIRCUIT_THRESHOLD
        ):
            _BAOSTOCK_CIRCUIT_OPEN = True
            _BAOSTOCK_CIRCUIT_NOTE = f"consecutive_failures={_BAOSTOCK_CONSEC_FAILS}, reason={reason}"
            if _DATA_SOURCE_DEBUG:
                print(f"[data_source] baostock circuit opened: {_BAOSTOCK_CIRCUIT_NOTE}")


def _compact_error(err: Exception, max_len: int = 120) -> str:
    msg = str(err or "").strip().replace("\n", " ")
    msg = re.sub(r"\s+", " ", msg)
    if len(msg) > max_len:
        msg = msg[: max_len - 3] + "..."
    if msg:
        return f"{type(err).__name__}: {msg}"
    return type(err).__name__


def _network_hint_from_details(details: list[str]) -> str:
    blob = " ".join(details).lower()
    dns_markers = [
        "nameresolutionerror",
        "nodename nor servname provided",
        "temporary failure in name resolution",
        "getaddrinfo failed",
        "failed to resolve",
    ]
    ssl_markers = [
        "ssl",
        "certificate",
        "cert verify failed",
    ]
    if any(k in blob for k in dns_markers):
        return "疑似 DNS/网络异常，请检查代理、DNS、系统防火墙或公司网络策略。"
    if any(k in blob for k in ssl_markers):
        return "疑似 SSL/证书链异常，请检查系统证书与 Python requests/certifi 环境。"
    if "remotedisconnected" in blob or "remote end closed connection" in blob:
        return "疑似上游行情源瞬时断连，可稍后重试；服务端已支持自动重试。"
    if "permission denied" in blob and "efinance" in blob:
        return "部署环境对 site-packages 为只读，efinance 本地缓存写入失败；建议依赖 tushare/akshare/baostock 或启用兼容修复。"
    return ""


def _is_retryable_akshare_error(err: Exception) -> bool:
    text = _compact_error(err).lower()
    markers = [
        "remotedisconnected",
        "remote end closed connection",
        "connection aborted",
        "connection reset",
        "read timed out",
        "connecttimeout",
        "proxyerror",
    ]
    return any(m in text for m in markers) or isinstance(err, RemoteDisconnected)


def _to_ts_code(symbol: str) -> str:
    """6 位代码转 tushare 格式：000001 -> 000001.SZ，600519 -> 600519.SH"""
    s = str(symbol).strip()
    if "." in s:
        return s
    if s.startswith(("600", "601", "603", "605", "688")):
        return f"{s}.SH"
    return f"{s}.SZ"


def _index_to_ts_code(code: str) -> str:
    """指数代码转 tushare 格式：000001->000001.SH, 399001->399001.SZ, 399006->399006.SZ"""
    s = str(code).strip()
    if "." in s:
        return s
    if s.startswith(("000", "880", "899")):
        return f"{s}.SH"
    return f"{s}.SZ"


def _get_tickflow_client():
    """懒加载 TickFlow client（缺 key 时返回 None）。"""
    global _TICKFLOW_CLIENT, _TICKFLOW_CLIENT_READY
    if _TICKFLOW_CLIENT_READY:
        return _TICKFLOW_CLIENT
    _TICKFLOW_CLIENT_READY = True
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        _TICKFLOW_CLIENT = None
        return None
    try:
        from integrations.tickflow_client import TickFlowClient

        _TICKFLOW_CLIENT = TickFlowClient(api_key=api_key)
    except Exception as e:
        _debug_source_fail("tickflow(client_init)", e)
        _TICKFLOW_CLIENT = None
    return _TICKFLOW_CLIENT


def _tag_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """在 DataFrame 上附加真实数据源标识，供上层缓存/展示使用。"""
    df.attrs["source"] = source
    return df


def _emit_tickflow_limit_notice_once() -> None:
    global _TICKFLOW_LIMIT_NOTICE_EMITTED
    with _TICKFLOW_LIMIT_NOTICE_LOCK:
        if _TICKFLOW_LIMIT_NOTICE_EMITTED:
            return
        _TICKFLOW_LIMIT_NOTICE_EMITTED = True
    print(f"[data_source] ⚠️ {TICKFLOW_LIMIT_HINT}", flush=True)


def _attach_tickflow_limit_notices(
    df: pd.DataFrame,
    notices: list[str] | None,
) -> pd.DataFrame:
    if df is None:
        return df
    uniq: list[str] = []
    for item in notices or []:
        text = str(item or "").strip()
        if text and text not in uniq:
            uniq.append(text)
    if not uniq:
        return df
    df.attrs["tickflow_limit_hint"] = uniq[0]
    df.attrs["tickflow_limit_hints"] = uniq
    _emit_tickflow_limit_notice_once()
    return df


def _to_float_or_none(v: Any) -> float | None:
    if v is None or pd.isna(v):
        return None
    try:
        return float(v)
    except Exception:
        try:
            s = str(v).strip().replace(",", "")
            if s.endswith("%"):
                s = s[:-1]
            return float(s)
        except Exception:
            return None


def _pick_first(row: pd.Series, candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        if key in row.index:
            v = row.get(key)
            if v is not None and not pd.isna(v):
                return v
    return None


def _normalize_spot_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if "." in s:
        s = s.split(".", 1)[0]
    m = re.search(r"(\d{6})", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s.zfill(6)
    return ""


def _normalize_spot_turnover(
    close_v: float | None,
    volume_v: float | None,
    amount_v: float | None,
) -> tuple[float | None, float | None, bool]:
    """
    统一实时快照的量能单位到“股/元”。
    不同数据源可能返回“股/手”与“元/千元/万元”混合口径。
    用“隐含成交均价≈最新价”做最优匹配；若误差过大，返回不可用。
    """
    if close_v is None or volume_v is None or amount_v is None:
        return (None, None, False)
    close = float(close_v)
    vol_raw = float(volume_v)
    amt_raw = float(amount_v)
    if close <= 0 or vol_raw <= 0 or amt_raw <= 0:
        return (None, None, False)

    # volume: 原始可能是 股 或 手；amount: 原始可能是 元 / 千元 / 万元
    vol_factors = (1.0, 100.0)
    amt_factors = (1.0, 1000.0, 10000.0)
    best: tuple[float, float, float] | None = None  # (rel_err, vol_shares, amt_yuan)
    for vf in vol_factors:
        vol_shares = vol_raw * vf
        if vol_shares <= 0:
            continue
        for af in amt_factors:
            amt_yuan = amt_raw * af
            if amt_yuan <= 0:
                continue
            implied_price = amt_yuan / vol_shares
            rel_err = abs(implied_price - close) / max(close, 1e-9)
            if best is None or rel_err < best[0]:
                best = (rel_err, vol_shares, amt_yuan)
    if best is None:
        return (None, None, False)

    rel_err, vol_shares, amt_yuan = best
    if rel_err <= max(_SPOT_TURNOVER_MAX_REL_ERR, 0.0):
        return (float(vol_shares), float(amt_yuan), True)
    return (None, None, False)


def _load_spot_snapshot_map(force_refresh: bool = False) -> dict[str, dict[str, float | None]]:
    global _SPOT_SNAPSHOT_TS, _SPOT_SNAPSHOT_MAP
    now_ts = time.time()
    with _SPOT_SNAPSHOT_LOCK:
        if (
            not force_refresh
            and _SPOT_SNAPSHOT_MAP
            and (now_ts - _SPOT_SNAPSHOT_TS) < max(_SPOT_SNAPSHOT_TTL_SECONDS, 1)
        ):
            return _SPOT_SNAPSHOT_MAP

        try:
            import akshare as ak

            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(ak.stock_zh_a_spot_em)
                df = fut.result(timeout=max(_SPOT_SNAPSHOT_TIMEOUT_SECONDS, 1.0))
            if df is None or df.empty:
                raise RuntimeError("spot snapshot empty")

            code_col = "代码"
            if code_col not in df.columns:
                fallback_cols = [c for c in df.columns if "代码" in str(c)]
                if fallback_cols:
                    code_col = str(fallback_cols[0])
                else:
                    raise RuntimeError("spot snapshot code column missing")

            spot_map: dict[str, dict[str, float | None]] = {}
            for _, row in df.iterrows():
                symbol = _normalize_spot_symbol(row.get(code_col))
                if not symbol:
                    continue
                close_v = _to_float_or_none(_pick_first(row, ("最新价", "最新", "现价", "收盘")))
                if close_v is None or close_v <= 0:
                    continue
                open_v = _to_float_or_none(_pick_first(row, ("今开", "开盘")))
                high_v = _to_float_or_none(_pick_first(row, ("最高",)))
                low_v = _to_float_or_none(_pick_first(row, ("最低",)))
                volume_raw = _to_float_or_none(_pick_first(row, ("成交量", "总手", "总量")))
                amount_raw = _to_float_or_none(_pick_first(row, ("成交额", "金额")))
                volume_v, amount_v, turnover_unit_ok = _normalize_spot_turnover(
                    close_v=close_v,
                    volume_v=volume_raw,
                    amount_v=amount_raw,
                )
                pct_v = _to_float_or_none(_pick_first(row, ("涨跌幅", "涨跌幅%")))

                spot_map[symbol] = {
                    "open": open_v,
                    "high": high_v,
                    "low": low_v,
                    "close": close_v,
                    "volume": volume_v,
                    "amount": amount_v,
                    "pct_chg": pct_v,
                    "turnover_unit_ok": 1.0 if turnover_unit_ok else 0.0,
                }
            if not spot_map:
                raise RuntimeError("spot snapshot parsed empty")

            _SPOT_SNAPSHOT_MAP = spot_map
            _SPOT_SNAPSHOT_TS = now_ts
            return _SPOT_SNAPSHOT_MAP
        except FuturesTimeoutError:
            _debug_source_fail(
                "spot_snapshot",
                TimeoutError(f"timeout>{_SPOT_SNAPSHOT_TIMEOUT_SECONDS:.1f}s"),
            )
            return _SPOT_SNAPSHOT_MAP
        except Exception as e:
            _debug_source_fail("spot_snapshot", e)
            return _SPOT_SNAPSHOT_MAP


def fetch_stock_spot_snapshot(
    symbol: str,
    *,
    force_refresh: bool = False,
) -> dict[str, float | None] | None:
    """
    获取单只股票最新快照（open/high/low/close/volume/amount/pct_chg）。
    用于日线延迟时的“单点补偿”。
    """
    s = _normalize_spot_symbol(symbol)
    if not s:
        return None
    spot_map = _load_spot_snapshot_map(force_refresh=force_refresh)
    return spot_map.get(s)


# --- 个股 ---


def _fetch_stock_akshare(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak

    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start,
        end_date=end,
        adjust=adjust if adjust else "",
    )
    if df is None or df.empty:
        raise RuntimeError("akshare empty")
    if "日期" in df.columns:
        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def _fetch_stock_baostock(symbol: str, start: str, end: str) -> pd.DataFrame:
    if symbol.startswith(("600", "601", "603", "605", "688")):
        bs_code = f"sh.{symbol}"
    else:
        bs_code = f"sz.{symbol}"
    start_dash = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_dash = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    with _BAOSTOCK_LOCK:
        old_sock_timeout = socket.getdefaulttimeout()
        if _BAOSTOCK_SOCKET_TIMEOUT > 0:
            socket.setdefaulttimeout(_BAOSTOCK_SOCKET_TIMEOUT)
        bs = _ensure_baostock_login()
        try:
            started = time.monotonic()
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,pctChg",
                start_date=start_dash,
                end_date=end_dash,
                frequency="d",
                adjustflag="2",  # 前复权
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock: {rs.error_msg}")
            rows: list[list[str]] = []
            while rs.next():
                if _BAOSTOCK_MAX_SECONDS > 0 and (time.monotonic() - started) > _BAOSTOCK_MAX_SECONDS:
                    raise TimeoutError(f"baostock hard timeout > {_BAOSTOCK_MAX_SECONDS:.2f}s")
                rows.append(rs.get_row_data())
        finally:
            socket.setdefaulttimeout(old_sock_timeout)
    if not rows:
        raise RuntimeError("baostock empty")
    df = pd.DataFrame(rows, columns=rs.fields)
    df = df.rename(
        columns={
            "date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
            "amount": "成交额",
            "pctChg": "涨跌幅",
        }
    )
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["开盘", "最高", "最低", "收盘", "成交量", "成交额", "涨跌幅"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["换手率"] = pd.NA
    df["振幅"] = pd.NA
    return df


def _baostock_logout_on_exit() -> None:
    global _BAOSTOCK_LOGGED
    with _BAOSTOCK_LOCK:
        bs = _BAOSTOCK_MODULE
        if not _BAOSTOCK_LOGGED or bs is None:
            return
        try:
            bs.logout()
        except BaseException:
            pass
        _BAOSTOCK_LOGGED = False


def _ensure_baostock_login():
    """
    进程内复用 baostock 会话，避免每只股票 login/logout 导致大量开销与阻塞日志。
    运行特性说明：该会话在当前 Python 进程生命周期内复用，并由 atexit 在进程退出时回收。
    若未来改为长生命周期守护进程/热重载模式，需要关注其“跨任务复用”行为是否符合预期。
    """
    global _BAOSTOCK_LOGGED, _BAOSTOCK_EXIT_HOOKED, _BAOSTOCK_MODULE
    with _BAOSTOCK_LOCK:
        import baostock as bs

        _BAOSTOCK_MODULE = bs
        if _BAOSTOCK_LOGGED:
            return bs

        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login: {lg.error_msg}")
        _BAOSTOCK_LOGGED = True

        if not _BAOSTOCK_EXIT_HOOKED:
            atexit.register(_baostock_logout_on_exit)
            _BAOSTOCK_EXIT_HOOKED = True
        return bs


def _fetch_stock_efinance(symbol: str, start: str, end: str) -> pd.DataFrame:
    # Streamlit Cloud / 只读部署环境下，efinance 在 import 阶段会尝试写 site-packages/efinance/data。
    # 这里做一次兼容导入：临时忽略该 mkdir 的 PermissionError，随后把缓存目录重定向到 /tmp。
    import pathlib
    import tempfile

    orig_mkdir = pathlib.Path.mkdir

    def _patched_mkdir(self, *args, **kwargs):
        try:
            return orig_mkdir(self, *args, **kwargs)
        except PermissionError:
            path_text = str(self)
            if "site-packages" in path_text and "efinance" in path_text and "data" in path_text:
                return None
            raise

    pathlib.Path.mkdir = _patched_mkdir
    try:
        import efinance as ef
        import efinance.config as ef_cfg

        # 预触发内部检查，某些版本在此处会尝试读取 data 目录
    except (PermissionError, FileNotFoundError) as e:
        _debug_source_fail("efinance_patch", e)
    finally:
        pathlib.Path.mkdir = orig_mkdir

    cache_dir = Path(tempfile.gettempdir()) / "efinance-cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    ef_cfg.DATA_DIR = cache_dir
    ef_cfg.SEARCH_RESULT_CACHE_PATH = str(cache_dir / "search-cache.json")

    # 额外抑制 efinance 内部对 site-packages 下 data 目录的硬编码访问尝试导致的 FileNotFoundError
    # 这种错误通常发生在 Python 3.13 + Streamlit Cloud 环境下

    # fqt: 0 不复权, 1 前复权, 2 后复权
    fqt = 1  # 默认前复权
    result = ef.stock.get_quote_history(symbol, beg=start, end=end, klt=101, fqt=fqt)
    if isinstance(result, dict):
        df = result.get(str(symbol))
    else:
        df = result
    if df is None or (hasattr(df, "empty") and df.empty):
        raise RuntimeError("efinance empty")

    # efinance 不同版本列名可能带单位后缀，如：涨跌幅(%)、成交额(元)
    df = df.copy()

    def _rename_prefix(std: str) -> None:
        if std in df.columns:
            return
        for c in df.columns:
            if str(c).startswith(std):
                df.rename(columns={c: std}, inplace=True)
                return

    # 日期列兼容
    if "日期" not in df.columns:
        for c in df.columns:
            if str(c).endswith("日期") or "日期" in str(c):
                df.rename(columns={c: "日期"}, inplace=True)
                break

    for std in [
        "开盘",
        "最高",
        "最低",
        "收盘",
        "成交量",
        "成交额",
        "涨跌幅",
        "换手率",
        "振幅",
    ]:
        _rename_prefix(std)
    # efinance: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 换手率
    out_cols = [
        "日期",
        "开盘",
        "最高",
        "最低",
        "收盘",
        "成交量",
        "成交额",
        "涨跌幅",
        "换手率",
        "振幅",
    ]
    for c in ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "涨跌幅"]:
        if c not in df.columns:
            raise RuntimeError(f"efinance missing column {c}")
    for c in ["换手率", "振幅"]:
        if c not in df.columns:
            df = df.assign(**{c: pd.NA})
    df["日期"] = pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d")
    return df[out_cols].copy()


def _fetch_stock_tushare(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import tushare as ts

    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise RuntimeError("TUSHARE_TOKEN 未配置")
    ts_code = _to_ts_code(symbol)
    # 口径固定：优先使用前复权（qfq）。
    adj_val = "qfq"
    # ts.pro_bar 绕过了 pro 对象，直接使用全局 token，需要显式限流
    from integrations.tushare_client import _wait_for_rate_limit

    _wait_for_rate_limit()
    df = ts.pro_bar(ts_code=ts_code, adj=adj_val, start_date=start, end_date=end)

    if df is None or df.empty:
        # 诊断：尝试拉取不复权数据，看是否是权限问题（qfq 需要更高积分）
        try:
            df_no_adj = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
            if df_no_adj is not None and not df_no_adj.empty:
                raise RuntimeError("tushare empty (qfq auth limit?)")
        except Exception:
            pass
        raise RuntimeError("tushare empty")

    df = df.rename(
        columns={
            "trade_date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "vol": "成交量",
            "amount": "成交额",
            "pct_chg": "涨跌幅",
        }
    )
    df["成交量"] = pd.to_numeric(df["成交量"], errors="coerce") * 100  # 手 -> 股
    df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce") * 1000  # 千元 -> 元
    df["换手率"] = pd.NA
    df["振幅"] = pd.NA
    df["日期"] = (
        df["日期"].astype(str).str[:4] + "-" + df["日期"].astype(str).str[4:6] + "-" + df["日期"].astype(str).str[6:8]
    )
    return df[
        [
            "日期",
            "开盘",
            "最高",
            "最低",
            "收盘",
            "成交量",
            "成交额",
            "涨跌幅",
            "换手率",
            "振幅",
        ]
    ].copy()


def _fetch_stock_tickflow(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """
    TickFlow 日线主链路（优先级最高）。
    输出列与主链路保持一致：日期, 开盘, 最高, 最低, 收盘, 成交量, 成交额, 涨跌幅, 换手率, 振幅
    """
    client = _get_tickflow_client()
    if client is None:
        raise RuntimeError("TICKFLOW_API_KEY 未配置，购买: https://tickflow.org/auth/register?ref=5N4NKTCPL4")

    try:
        start_d = datetime.strptime(start, "%Y%m%d").date()
        end_d = datetime.strptime(end, "%Y%m%d").date()
    except Exception as e:
        raise RuntimeError(f"tickflow date parse failed: {start}..{end}") from e
    if end_d < start_d:
        raise RuntimeError(f"tickflow invalid range: {start}..{end}")

    cn_tz = timezone(timedelta(hours=8))
    start_dt = datetime.combine(start_d, datetime.min.time(), tzinfo=cn_tz)
    end_dt = datetime.combine(end_d + timedelta(days=1), datetime.min.time(), tzinfo=cn_tz) - timedelta(milliseconds=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    day_span = (end_d - start_d).days + 1
    count = min(max(day_span * 2 + 16, 64), _TICKFLOW_DAILY_MAX_COUNT)
    adjust_norm = str(adjust or "").strip().lower()
    adjust_map = {
        "": "none",
        "none": "none",
        "qfq": "forward",
        "forward": "forward",
        "hfq": "backward",
        "backward": "backward",
    }
    tick_adjust = adjust_map.get(adjust_norm, "forward")

    df = client.get_klines(
        symbol=symbol,
        period="1d",
        count=count,
        intraday=False,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        adjust=tick_adjust,
    )
    if df is None or df.empty:
        raise RuntimeError("tickflow empty")

    start_iso = start_d.isoformat()
    end_iso = end_d.isoformat()
    out = df[(df["date"] >= start_iso) & (df["date"] <= end_iso)].copy()
    if out.empty:
        raise RuntimeError("tickflow empty in range")

    close = pd.to_numeric(out.get("close"), errors="coerce")
    prev_close = pd.to_numeric(out.get("prev_close"), errors="coerce")
    prev_ref = prev_close.where(prev_close > 0)
    if prev_ref.notna().sum() == 0:
        prev_ref = close.shift(1)
    pct = (close / prev_ref - 1.0) * 100.0
    amp = (
        (pd.to_numeric(out.get("high"), errors="coerce") - pd.to_numeric(out.get("low"), errors="coerce"))
        / prev_ref
        * 100.0
    )

    result = pd.DataFrame(
        {
            "日期": out["date"],
            "开盘": pd.to_numeric(out.get("open"), errors="coerce"),
            "最高": pd.to_numeric(out.get("high"), errors="coerce"),
            "最低": pd.to_numeric(out.get("low"), errors="coerce"),
            "收盘": close,
            "成交量": pd.to_numeric(out.get("volume"), errors="coerce"),
            "成交额": pd.to_numeric(out.get("amount"), errors="coerce"),
            "涨跌幅": pct,
            "换手率": pd.NA,
            "振幅": amp,
        }
    )
    return result[
        [
            "日期",
            "开盘",
            "最高",
            "最低",
            "收盘",
            "成交量",
            "成交额",
            "涨跌幅",
            "换手率",
            "振幅",
        ]
    ].copy()


def fetch_stock_hist(
    symbol: str,
    start: str | date,
    end: str | date,
    adjust: Literal["", "qfq", "hfq"] = "qfq",
) -> pd.DataFrame:
    """
    个股日线：tickflow 优先（固定 qfq），失败时回退 tushare/akshare/baostock/efinance。
    可用环境变量按需禁用数据源：
    - DATA_SOURCE_DISABLE_TICKFLOW=1
    - DATA_SOURCE_DISABLE_AKSHARE=1
    - DATA_SOURCE_DISABLE_BAOSTOCK=1
    - DATA_SOURCE_DISABLE_EFINANCE=1
    返回列：日期, 开盘, 最高, 最低, 收盘, 成交量, 成交额, 涨跌幅, 换手率, 振幅
    """
    start_s = start.strftime("%Y%m%d") if isinstance(start, date) else str(start).replace("-", "")
    end_s = end.strftime("%Y%m%d") if isinstance(end, date) else str(end).replace("-", "")

    failed_sources: list[str] = []
    failed_details: list[str] = []
    tickflow_limit_notices: list[str] = []
    tickflow_failed = False
    disable_akshare = os.getenv("DATA_SOURCE_DISABLE_AKSHARE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    disable_tickflow = os.getenv("DATA_SOURCE_DISABLE_TICKFLOW", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    disable_baostock = os.getenv("DATA_SOURCE_DISABLE_BAOSTOCK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    disable_efinance = os.getenv("DATA_SOURCE_DISABLE_EFINANCE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # 1. tickflow 优先（固定 qfq）
    if disable_tickflow:
        failed_sources.append("tickflow(disabled)")
        failed_details.append("tickflow=disabled_by_env")
    elif not os.getenv("TICKFLOW_API_KEY", "").strip():
        failed_sources.append("tickflow(unconfigured)")
        failed_details.append("tickflow=api_key_missing(购买: https://tickflow.org/auth/register?ref=5N4NKTCPL4)")
    else:
        try:
            return _tag_source(
                _fetch_stock_tickflow(symbol, start_s, end_s, adjust),
                "tickflow",
            )
        except Exception as e:
            tickflow_failed = True
            _debug_source_fail("tickflow", e)
            failed_sources.append("tickflow")
            failed_details.append(f"tickflow={_compact_error(e)}")
            if is_tickflow_rate_limited_error(e):
                record_tickflow_limit_event(e)
                tickflow_limit_notices.append(TICKFLOW_LIMIT_HINT)
                failed_details.append(f"tickflow_limit_hint={TICKFLOW_LIMIT_HINT}")
                print(
                    f"[data_source] tickflow限流触发: symbol={symbol}, range={start_s}..{end_s}, "
                    f"fallback_chain=tushare->akshare->baostock->efinance",
                    flush=True,
                )
            else:
                print(
                    f"[data_source] tickflow失败，将回退: symbol={symbol}, range={start_s}..{end_s}, "
                    f"err={_compact_error(e)}",
                    flush=True,
                )

    # 2) tushare 次优先（固定 qfq）
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is not None:
        try:
            out = _tag_source(
                _attach_tickflow_limit_notices(
                    _fetch_stock_tushare(symbol, start_s, end_s, "qfq"),
                    tickflow_limit_notices,
                ),
                "tushare",
            )
            if tickflow_failed:
                print(
                    f"[data_source] fallback命中: symbol={symbol}, source=tushare, "
                    f"tickflow_limit_hint={bool(tickflow_limit_notices)}",
                    flush=True,
                )
            return out
        except Exception as e:
            _debug_source_fail("tushare", e)
            failed_sources.append("tushare")
            failed_details.append(f"tushare={_compact_error(e)}")
    else:
        failed_sources.append("tushare(unconfigured)")
        failed_details.append("tushare=token_missing")

    # 3. akshare
    if disable_akshare:
        failed_sources.append("akshare(disabled)")
        failed_details.append("akshare=disabled_by_env")
    else:
        for attempt in range(1, _AKSHARE_RETRY_TIMES + 1):
            try:
                out = _tag_source(
                    _attach_tickflow_limit_notices(
                        _fetch_stock_akshare(symbol, start_s, end_s, adjust),
                        tickflow_limit_notices,
                    ),
                    "akshare",
                )
                if tickflow_failed:
                    print(
                        f"[data_source] fallback命中: symbol={symbol}, source=akshare, "
                        f"tickflow_limit_hint={bool(tickflow_limit_notices)}",
                        flush=True,
                    )
                return out
            except ModuleNotFoundError as e:
                _debug_source_fail("akshare", e)
                failed_sources.append(f"akshare(缺少依赖 {e.name})")
                failed_details.append(f"akshare={_compact_error(e)}")
                break
            except Exception as e:
                _debug_source_fail("akshare", e)
                if attempt < _AKSHARE_RETRY_TIMES and _is_retryable_akshare_error(e):
                    time.sleep(max(_AKSHARE_RETRY_SLEEP_SECONDS, 0.0))
                    continue
                failed_sources.append("akshare")
                failed_details.append(f"akshare={_compact_error(e)}")
                break

    # 4. baostock (仅前复权)
    baostock_circuit_open, baostock_circuit_note = _baostock_circuit_state()
    if disable_baostock:
        failed_sources.append("baostock(disabled)")
        failed_details.append("baostock=disabled_by_env")
    elif baostock_circuit_open:
        note = baostock_circuit_note or "circuit_open"
        failed_sources.append("baostock(circuit_open)")
        failed_details.append(f"baostock={note}")
    else:
        started = time.monotonic()
        try:
            df = _fetch_stock_baostock(symbol, start_s, end_s)
            elapsed = time.monotonic() - started
            if _BAOSTOCK_MAX_SECONDS > 0 and elapsed > _BAOSTOCK_MAX_SECONDS:
                raise TimeoutError(f"baostock slow={elapsed:.2f}s > {_BAOSTOCK_MAX_SECONDS:.2f}s")
            _baostock_mark_success()
            out = _tag_source(
                _attach_tickflow_limit_notices(df, tickflow_limit_notices),
                "baostock",
            )
            if tickflow_failed:
                print(
                    f"[data_source] fallback命中: symbol={symbol}, source=baostock, "
                    f"tickflow_limit_hint={bool(tickflow_limit_notices)}",
                    flush=True,
                )
            return out
        except ModuleNotFoundError as e:
            _debug_source_fail("baostock", e)
            _baostock_mark_failure(_compact_error(e))
            failed_sources.append(f"baostock(未安装: {e.name})")
            failed_details.append(f"baostock={_compact_error(e)}")
        except Exception as e:
            _debug_source_fail("baostock", e)
            _baostock_mark_failure(_compact_error(e))
            failed_sources.append("baostock")
            failed_details.append(f"baostock={_compact_error(e)}")

    # 5. efinance (仅前复权)
    if disable_efinance:
        failed_sources.append("efinance(disabled)")
        failed_details.append("efinance=disabled_by_env")
    else:
        try:
            out = _tag_source(
                _attach_tickflow_limit_notices(
                    _fetch_stock_efinance(symbol, start_s, end_s),
                    tickflow_limit_notices,
                ),
                "efinance",
            )
            if tickflow_failed:
                print(
                    f"[data_source] fallback命中: symbol={symbol}, source=efinance, "
                    f"tickflow_limit_hint={bool(tickflow_limit_notices)}",
                    flush=True,
                )
            return out
        except ModuleNotFoundError as e:
            _debug_source_fail("efinance", e)
            failed_sources.append(f"efinance(未安装: {e.name})")
            failed_details.append(f"efinance={_compact_error(e)}")
        except Exception as e:
            _debug_source_fail("efinance", e)
            failed_sources.append("efinance")
            failed_details.append(f"efinance={_compact_error(e)}")

    detail_suffix = f" 失败详情：{'；'.join(failed_details[:4])}。" if failed_details else ""
    hint = _network_hint_from_details(failed_details)
    hint_suffix = f" 诊断提示：{hint}" if hint else ""
    raise RuntimeError(
        f"数据拉取全线失败 [标:{symbol}, 范围:{start_s}..{end_s}, 复权:{adjust}]：已按顺序尝试 tickflow→tushare→akshare→baostock→efinance，"
        f"均无可用 K 线数据。请检查该标的是否已退市或处于长期停牌期。{detail_suffix}{hint_suffix}"
    )


# --- 大盘指数 ---


def _fetch_index_tushare(code: str, start: str, end: str) -> pd.DataFrame:
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise RuntimeError(
            "拉取失败（非程序错误）：大盘指数需 Tushare Token，免费数据源（akshare 等）不支持大盘指数。请配置 TUSHARE_TOKEN。"
        )
    ts_code = _index_to_ts_code(code)
    df = pro.index_daily(ts_code=ts_code, start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError("拉取失败（非程序错误）：tushare 大盘指数返回空数据")
    df = df.copy()
    df["date"] = (
        df["trade_date"].astype(str).str[:4]
        + "-"
        + df["trade_date"].astype(str).str[4:6]
        + "-"
        + df["trade_date"].astype(str).str[6:8]
    )
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    return df[["date", "open", "high", "low", "close", "volume", "pct_chg"]].copy()


def _fetch_index_akshare(code: str, start: str, end: str) -> pd.DataFrame:
    """akshare 大盘指数日线 fallback（tushare 不可用时自动降级）。"""
    import akshare as ak

    df = ak.index_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start,
        end_date=end,
    )
    if df is None or df.empty:
        raise RuntimeError("akshare 大盘指数返回空数据")
    df = df.rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "涨跌幅": "pct_chg",
        }
    )
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["open", "high", "low", "close", "volume", "pct_chg"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["date", "open", "high", "low", "close", "volume", "pct_chg"]].copy()


def fetch_index_hist(code: str, start: str | date, end: str | date) -> pd.DataFrame:
    """
    大盘指数日线：tushare 优先，失败时 fallback 到 akshare。
    返回列：date, open, high, low, close, volume, pct_chg（小写，供 step2 使用）
    """
    start_s = start.strftime("%Y%m%d") if isinstance(start, date) else str(start).replace("-", "")
    end_s = end.strftime("%Y%m%d") if isinstance(end, date) else str(end).replace("-", "")

    # 1) tushare 优先
    try:
        return _fetch_index_tushare(code, start_s, end_s)
    except Exception as e:
        _debug_source_fail("tushare(index)", e)

    # 2) akshare fallback
    try:
        return _fetch_index_akshare(code, start_s, end_s)
    except Exception as e2:
        _debug_source_fail("akshare(index)", e2)

    raise RuntimeError(f"大盘指数 {code} 拉取全部失败（tushare + akshare），请检查 TUSHARE_TOKEN 或网络连通性。")


# --- 行业 & 市值批量获取（tushare） ---

_DATA_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
_SECTOR_CACHE = _DATA_CACHE_DIR / "sector_map_cache.json"
_MARKET_CAP_CACHE = _DATA_CACHE_DIR / "market_cap_cache.json"
_CACHE_TTL = 24 * 60 * 60


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass


def _ts_code_to_symbol(ts_code: str) -> str:
    """000001.SZ -> 000001"""
    return ts_code.split(".")[0] if "." in ts_code else ts_code


def fetch_sector_map() -> dict[str, str]:
    """
    全市场 code->行业映射。优先用缓存，过期后通过 tushare stock_basic 刷新。
    """
    try:
        if _SECTOR_CACHE.exists() and (time.time() - _SECTOR_CACHE.stat().st_mtime) < _CACHE_TTL:
            with open(_SECTOR_CACHE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        _debug_source_fail("sector_cache_read", e)

    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        try:
            if _SECTOR_CACHE.exists():
                with open(_SECTOR_CACHE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            _debug_source_fail("sector_cache_fallback_read", e)
        return {}

    try:
        df = pro.stock_basic(fields="ts_code,industry")
    except Exception as e:
        _debug_source_fail("tushare_stock_basic", e)
        # tushare 短时抖动时，退回本地缓存，避免上游任务整体失败
        try:
            if _SECTOR_CACHE.exists():
                with open(_SECTOR_CACHE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception as cache_e:
            _debug_source_fail("sector_cache_error_fallback_read", cache_e)
        return {}

    if df is None or df.empty:
        try:
            if _SECTOR_CACHE.exists():
                with open(_SECTOR_CACHE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            _debug_source_fail("sector_cache_empty_fallback_read", e)
        return {}

    mapping = {}
    for _, row in df.iterrows():
        sym = _ts_code_to_symbol(str(row["ts_code"]))
        industry = str(row.get("industry", "")).strip()
        if sym and industry:
            mapping[sym] = industry

    try:
        _atomic_write_json(_SECTOR_CACHE, mapping)
    except Exception as e:
        _debug_source_fail("sector_cache_write", e)

    return mapping


def fetch_market_cap_map() -> dict[str, float]:
    """
    全市场 code->总市值(亿元)。通过 tushare daily_basic 获取最新交易日数据。
    """
    try:
        if _MARKET_CAP_CACHE.exists() and (time.time() - _MARKET_CAP_CACHE.stat().st_mtime) < _CACHE_TTL:
            with open(_MARKET_CAP_CACHE, encoding="utf-8") as f:
                return {k: float(v) for k, v in json.load(f).items()}
    except Exception as e:
        _debug_source_fail("market_cap_cache_read", e)

    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        try:
            if _MARKET_CAP_CACHE.exists():
                with open(_MARKET_CAP_CACHE, encoding="utf-8") as f:
                    return {k: float(v) for k, v in json.load(f).items()}
        except Exception as e:
            _debug_source_fail("market_cap_cache_fallback_read", e)
        return {}

    from datetime import date as _date
    from datetime import timedelta as _td

    # 尝试最近几个交易日
    mapping: dict[str, float] = {}
    for offset in range(5):
        d = _date.today() - _td(days=1 + offset)
        trade_date = d.strftime("%Y%m%d")
        try:
            df = pro.daily_basic(trade_date=trade_date, fields="ts_code,total_mv")
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    sym = _ts_code_to_symbol(str(row["ts_code"]))
                    total_mv = row.get("total_mv")
                    if sym and pd.notna(total_mv):
                        mapping[sym] = float(total_mv) / 10000.0  # 万元 -> 亿元
                break
        except Exception as e:
            _debug_source_fail(f"tushare_daily_basic[{trade_date}]", e)
            continue

    if mapping:
        try:
            _atomic_write_json(_MARKET_CAP_CACHE, mapping)
        except Exception as e:
            _debug_source_fail("market_cap_cache_write", e)

    return mapping
