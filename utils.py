import os
import re

import akshare as ak


def safe_filename_part(value: str | None, *, fallback: str = "Unknown") -> str:
    s = str(value or "").strip()
    if not s:
        return fallback
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = s.replace(os.sep, "_")
    if os.altsep:
        s = s.replace(os.altsep, "_")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def stock_sector_em(symbol: str, *, timeout: float | None = None) -> str:
    try:
        if timeout is None:
            df = ak.stock_individual_info_em(symbol=symbol)
        else:
            df = ak.stock_individual_info_em(symbol=symbol, timeout=timeout)
        if df is None or df.empty:
            return ""
        row = df.loc[df["item"] == "行业", "value"]
        if row.empty:
            return ""
        return str(row.iloc[0]).strip()
    except Exception:
        return ""


def extract_symbols_from_text(
    text: str, *, valid_codes: set[str] | None = None
) -> list[str]:
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
