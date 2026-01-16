import argparse
import json
import os
import re
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from functools import lru_cache

import akshare as ak
import pandas as pd
import requests


@dataclass(frozen=True)
class TradingWindow:
    start_trade_date: date
    end_trade_date: date


def _safe_filename_part(value: str) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    for ch in ['<', '>', ':', '"', '/', '\\', '|', '?', '*']:
        s = s.replace(ch, "_")
    s = s.replace(os.sep, "_")
    if os.altsep:
        s = s.replace(os.altsep, "_")
    return s


def _trade_dates() -> list[date]:
    cache_dir = Path(__file__).resolve().parent / "data"
    cache_path = cache_dir / "trade_dates_cache.json"
    cache_ttl_seconds = 7 * 24 * 60 * 60

    def _read_cache() -> list[date]:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                return []
            out: list[date] = []
            for x in raw:
                try:
                    out.append(pd.to_datetime(x).date())
                except Exception:
                    continue
            out.sort()
            return out
        except Exception:
            return []

    def _write_cache(dates: list[date]) -> None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump([d.strftime("%Y-%m-%d") for d in dates], f, ensure_ascii=False)
        except Exception:
            return

    def _fetch_with_timeout(timeout: float) -> list[date]:
        try:
            import py_mini_racer
            from akshare.stock.cons import hk_js_decode
        except Exception as e:
            raise RuntimeError(f"missing dependency for trade calendar decode: {e}")
        url = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        payload = r.text.split("=")[1].split(";")[0].replace('"', "")
        js_code = py_mini_racer.MiniRacer()
        js_code.eval(hk_js_decode)
        dict_list = js_code.call("d", payload)
        df = pd.DataFrame(dict_list)
        df.columns = ["trade_date"]
        s = pd.to_datetime(df["trade_date"]).dt.date
        dates = s.tolist()
        dates.append(date(year=1992, month=5, day=4))
        dates.sort()
        return dates

    try:
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age <= cache_ttl_seconds:
                cached = _read_cache()
                if cached:
                    return cached
    except Exception:
        pass

    last_err: Exception | None = None
    for _ in range(3):
        try:
            dates = _fetch_with_timeout(timeout=10)
            if dates:
                _write_cache(dates)
                return dates
        except Exception as e:
            last_err = e
            time.sleep(0.6)

    cached = _read_cache()
    if cached:
        return cached

    end = date.today() + timedelta(days=366)
    start = date(1990, 1, 1)
    approx = pd.bdate_range(start=start, end=end).date.tolist()
    approx.sort()
    if not approx:
        raise RuntimeError(f"failed to build trade calendar: {last_err}")
    return approx


@lru_cache(maxsize=1)
def _trade_dates_cached() -> tuple[date, ...]:
    return tuple(_trade_dates())


def _resolve_trading_window(end_calendar_day: date, trading_days: int) -> TradingWindow:
    if trading_days <= 0:
        raise ValueError("trading_days must be > 0")
    dates = list(_trade_dates_cached())
    idx = bisect_right(dates, end_calendar_day) - 1
    if idx < 0:
        raise RuntimeError("trade calendar has no date <= end_calendar_day")
    if idx - (trading_days - 1) < 0:
        raise RuntimeError("trade calendar does not have enough historical dates")
    start_trade = dates[idx - (trading_days - 1)]
    end_trade = dates[idx]
    return TradingWindow(start_trade_date=start_trade, end_trade_date=end_trade)


def _stock_name_from_code(symbol: str) -> str:
    info = ak.stock_info_a_code_name()
    row = info.loc[info["code"] == symbol, "name"]
    if row.empty:
        raise RuntimeError(f"symbol not found in stock list: {symbol}")
    return str(row.iloc[0])


def get_all_stocks() -> list[dict[str, str]]:
    """
    Get all A-share stock codes and names.
    Returns:
        list of dict: [{"code": "000001", "name": "平安银行"}, ...]
    """
    cache_dir = Path(__file__).resolve().parent / "data"
    cache_path = cache_dir / "stock_list_cache.json"
    cache_ttl_seconds = 24 * 60 * 60

    def _read_cache() -> list[dict[str, str]]:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [{"code": str(x.get("code", "")), "name": str(x.get("name", ""))} for x in data if isinstance(x, dict)]
        except Exception:
            return []
        return []

    try:
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age <= cache_ttl_seconds:
                cached = _read_cache()
                if cached:
                    return cached

        info = ak.stock_info_a_code_name()
        info["code"] = info["code"].astype(str)
        info["name"] = info["name"].astype(str)
        records = info.to_dict("records")

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False)
        except Exception:
            pass

        return records
    except Exception as e:
        cached = _read_cache()
        if cached:
            return cached
        print(f"Error fetching stock list: {e}")
        return []


def _fetch_hist(symbol: str, window: TradingWindow, adjust: str) -> pd.DataFrame:
    start = window.start_trade_date.strftime("%Y%m%d")
    end = window.end_trade_date.strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start,
        end_date=end,
        adjust=adjust,
    )
    if df is None or df.empty:
        raise RuntimeError(f"empty data returned for symbol={symbol}, start={start}, end={end}, adjust={adjust!r}")
    return df


def _stock_sector_em(symbol: str) -> str:
    try:
        df = ak.stock_individual_info_em(symbol=symbol)
        if df is None or df.empty:
            return ""
        row = df.loc[df["item"] == "行业", "value"]
        if row.empty:
            return ""
        return str(row.iloc[0]).strip()
    except Exception:
        return ""


def _build_export(df: pd.DataFrame, sector: str) -> pd.DataFrame:
    required = ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "换手率", "振幅"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"unexpected columns, missing: {missing}; got: {df.columns.tolist()}")

    out = df[required].copy()
    out["AvgPrice"] = out["成交额"] / out["成交量"].replace(0, pd.NA)
    out["Sector"] = sector

    out = out.rename(
        columns={
            "日期": "Date",
            "开盘": "Open",
            "最高": "High",
            "最低": "Low",
            "收盘": "Close",
            "成交量": "Volume",
            "成交额": "Amount",
            "换手率": "TurnoverRate",
            "振幅": "Amplitude",
        }
    )
    out = out[
        [
            "Date",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "Amount",
            "TurnoverRate",
            "Amplitude",
            "AvgPrice",
            "Sector",
        ]
    ]
    return out


def _extract_symbols_from_text(text: str, valid_codes: set[str] | None) -> list[str]:
    if not text:
        return []
    digit_runs = re.findall(r"\d{6,}", text)
    if not digit_runs:
        return []

    out: list[str] = []

    def accept(code: str) -> bool:
        if not re.fullmatch(r"\d{6}", code):
            return False
        if valid_codes is None:
            out.append(code)
            return True
        if code in valid_codes:
            out.append(code)
            return True
        return False

    for run in digit_runs:
        if len(run) == 6:
            accept(run)
            continue

        if valid_codes is not None and len(run) == 7:
            fixed = False
            for i in range(7):
                cand = run[:i] + run[i + 1 :]
                if accept(cand):
                    fixed = True
                    break
            if fixed:
                continue

        if valid_codes is not None and len(run) % 6 == 0:
            for i in range(0, len(run), 6):
                accept(run[i : i + 6])
            continue

        if valid_codes is None:
            accept(run[:6])
            continue

        i = 0
        while i <= len(run) - 6:
            cand = run[i : i + 6]
            if accept(cand):
                i += 6
            else:
                i += 1

    return out


def _normalize_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        s = str(raw).strip()
        if not s:
            continue
        if not re.fullmatch(r"\d{6}", s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _write_two_csv(symbol: str, name: str, df_hist: pd.DataFrame, out_dir: str, sector: str) -> tuple[str, str]:
    file_prefix = f"{_safe_filename_part(symbol)}_{_safe_filename_part(name)}"
    hist_path = os.path.join(out_dir, f"{file_prefix}_hist_data.csv")
    ohlcv_path = os.path.join(out_dir, f"{file_prefix}_ohlcv.csv")
    df_hist.to_csv(hist_path, index=False, encoding="utf-8-sig")
    _build_export(df_hist, sector=sector).to_csv(ohlcv_path, index=False, encoding="utf-8-sig")
    return hist_path, ohlcv_path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_a_share_csv.py",
        description="使用 akshare 拉取 A 股指定股票近 N 个交易日数据，并输出 hist_data 与 ohlcv 两个 CSV 文件。",
    )
    parser.add_argument("--symbol", help="单个股票代码，如 300364")
    parser.add_argument("--symbols", nargs="*", help="多个股票代码，如 000973 600798 300459")
    parser.add_argument(
        "--symbols-text",
        help="从一段文本中提取股票代码（支持夹中文/无空格），如 \"000973 佛塑科技 600798鲁抗医药\"",
    )
    parser.add_argument("--trading-days", type=int, default=500, help="交易日数量，默认 500")
    parser.add_argument(
        "--end-offset-days",
        type=int,
        default=1,
        help="结束日期偏移（自然日），默认 1 表示系统日期-1天，再向前对齐到最近交易日",
    )
    parser.add_argument(
        "--adjust",
        default="",
        choices=["", "qfq", "hfq"],
        help="复权类型：空字符串=不复权，qfq=前复权，hfq=后复权",
    )
    parser.add_argument("--out-dir", default="data", help="输出目录，默认 data 目录")
    args = parser.parse_args()

    info = ak.stock_info_a_code_name()
    code_to_name: dict[str, str] = dict(zip(info["code"].astype(str), info["name"].astype(str)))
    valid_codes = set(code_to_name.keys())

    candidates: list[str] = []
    if args.symbol:
        candidates.append(args.symbol)
    if args.symbols:
        candidates.extend(args.symbols)
    if args.symbols_text:
        candidates.extend(_extract_symbols_from_text(args.symbols_text, valid_codes=valid_codes))
    symbols = _normalize_symbols(candidates)
    if not symbols:
        raise SystemExit("请提供股票代码：--symbol 或 --symbols 或 --symbols-text")

    end_calendar = date.today() - timedelta(days=int(args.end_offset_days))
    window = _resolve_trading_window(end_calendar_day=end_calendar, trading_days=int(args.trading_days))

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    print(f"trade_window={window.start_trade_date}..{window.end_trade_date} (trading_days={args.trading_days})")
    failures: list[tuple[str, str]] = []
    for symbol in symbols:
        try:
            name = code_to_name.get(symbol)
            if not name:
                raise RuntimeError(f"symbol not found in stock list: {symbol}")
            df_hist = _fetch_hist(symbol=symbol, window=window, adjust=str(args.adjust))
            sector = _stock_sector_em(symbol)
            hist_path, ohlcv_path = _write_two_csv(
                symbol=symbol,
                name=name,
                df_hist=df_hist,
                out_dir=out_dir,
                sector=sector,
            )
            print(f"OK symbol={symbol} name={name} -> {os.path.basename(hist_path)}, {os.path.basename(ohlcv_path)}")
        except Exception as e:
            failures.append((symbol, str(e)))
            print(f"FAIL symbol={symbol} err={e}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
