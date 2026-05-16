from __future__ import annotations

import json

import pandas as pd

from scripts.market_funnel_job import _candidate_rows, run_market_funnel


def _daily_frame(rows: int = 230) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=rows, freq="B")
    close = pd.Series(range(rows), dtype="float64") * 0.2 + 100.0
    open_ = close - 0.5
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([1_000_000 + i * 1000 for i in range(rows)], dtype="float64")
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "pct_chg": close.pct_change().fillna(0.0) * 100.0,
        }
    )


def test_candidate_rows_keep_raw_trigger_strength():
    rows = _candidate_rows(
        {"sos": [("BZFD.US", 535.7), ("WOK.US", 26.3), ("QUBT.US", 11.23)]},
        name_map={"BZFD.US": "BuzzFeed", "WOK.US": "WORK Medical Tech", "QUBT.US": "Quantum Computing"},
        df_map={
            "BZFD.US": pd.DataFrame({"close": [1.39]}),
            "WOK.US": pd.DataFrame({"close": [6.66]}),
            "QUBT.US": pd.DataFrame({"close": [11.78]}),
        },
    )

    assert [row["symbol"] for row in rows] == ["BZFD.US", "WOK.US", "QUBT.US"]
    assert [row["score"] for row in rows] == [535.7, 26.3, 11.23]


class FakeTickFlowClient:
    def __init__(self) -> None:
        self.quote_batches: list[list[str]] = []
        self.kline_batches: list[list[str]] = []

    def get_quotes(self, symbols=None, *, universes=None):
        assert universes is None
        self.quote_batches.append(list(symbols or []))
        quotes = {
            "00700.HK": {
                "symbol": "00700.HK",
                "last_price": 350.0,
                "amount": 9_000_000.0,
                "ext": {"name": "Tencent", "change_pct": 0.01},
            },
            "00005.HK": {
                "symbol": "00005.HK",
                "last_price": 65.0,
                "amount": 8_000_000.0,
                "ext": {"name": "HSBC", "change_pct": -0.005},
            },
            "09999.HK": {"symbol": "09999.HK", "last_price": 0.0, "amount": 10_000_000.0},
        }
        return {symbol: quotes[symbol] for symbol in symbols or [] if symbol in quotes}

    def get_klines_batch(self, symbols, *, period, count, adjust):
        self.kline_batches.append(list(symbols))
        assert period == "1d"
        assert count == 230
        assert adjust == "forward"
        return {symbol: _daily_frame() for symbol in symbols}


class ManyCandidateTickFlowClient:
    def __init__(self) -> None:
        self.symbol_count = 0

    def get_quotes(self, symbols=None, *, universes=None):
        assert universes is None
        rows = {}
        for index, symbol in enumerate(symbols or [], start=1):
            rows[symbol] = {
                "symbol": symbol,
                "last_price": 100.0 + index,
                "amount": 10_000_000.0 + index,
                "ext": {"name": f"Name {symbol}", "change_pct": 0.0},
            }
        self.symbol_count += len(rows)
        return rows

    def get_klines_batch(self, symbols, *, period, count, adjust):
        assert period == "1d"
        assert count == 230
        assert adjust == "forward"
        return {symbol: _daily_frame() for symbol in symbols}


def test_run_market_funnel_uses_quote_prefilter_and_batch_fetch(tmp_path, monkeypatch):
    symbol_file = tmp_path / "hk_symbols.txt"
    symbol_file.write_text("00700.HK\n00005.HK\n09999.HK\n", encoding="utf-8")
    monkeypatch.setenv("MARKET_FUNNEL_SYMBOL_FILE", str(symbol_file))
    monkeypatch.setenv("MARKET_FUNNEL_MAX_SYMBOLS", "2")
    monkeypatch.setenv("MARKET_FUNNEL_QUOTE_BATCH_SIZE", "1")
    monkeypatch.setenv("MARKET_FUNNEL_QUOTE_BATCH_SLEEP", "0")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_COUNT", "230")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_BATCH_SIZE", "1")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_BATCH_SLEEP", "0")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_QUOTE_AMOUNT", "0")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "220")
    output = tmp_path / "hk_result.json"
    client = FakeTickFlowClient()

    result = run_market_funnel("hk", output=str(output), client=client)

    assert result["ok"] is True
    assert result["market"] == "hk"
    assert result["quote_count"] == 3
    assert result["universe_symbol_count"] == 3
    assert result["selected_count"] == 2
    assert result["fetched_count"] == 2
    assert client.quote_batches == [["00700.HK"], ["00005.HK"], ["09999.HK"]]
    assert client.kline_batches == [["00700.HK"], ["00005.HK"]]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["limits"]["quote_batch_size"] == 1
    assert payload["limits"]["quote_batch_sleep"] == 0.0
    assert payload["limits"]["kline_batch_size"] == 1
    report = output.with_name("hk_report.md").read_text(encoding="utf-8")
    assert "Wyckoff Funnel 港股 最终报告" in report
    assert "## 漏斗概览" in report
    assert "| 股票池 | 3 |" in report


def test_run_market_funnel_writes_all_candidates_to_db(tmp_path, monkeypatch):
    symbols = [f"S{i:03d}.US" for i in range(55)]
    symbol_file = tmp_path / "us_symbols.txt"
    symbol_file.write_text("\n".join(symbols), encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_layers(fetched_symbols, name_map, df_map, runtime):
        hits = [(symbol, float(index)) for index, symbol in enumerate(fetched_symbols, start=1)]
        return {"sos": hits}, {"total_hits": len(hits), "by_trigger": {"sos": len(hits)}}

    def fake_upsert(recommend_date, rows, market):
        captured["rows"] = rows
        captured["market"] = market
        return True

    monkeypatch.setenv("MARKET_FUNNEL_SYMBOL_FILE", str(symbol_file))
    monkeypatch.setenv("MARKET_FUNNEL_MAX_SYMBOLS", "55")
    monkeypatch.setenv("MARKET_FUNNEL_QUOTE_BATCH_SIZE", "100")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_COUNT", "230")
    monkeypatch.setenv("MARKET_FUNNEL_KLINE_BATCH_SIZE", "100")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_QUOTE_AMOUNT", "0")
    monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "220")
    monkeypatch.setenv("MARKET_FUNNEL_WRITE_DB", "1")
    monkeypatch.setattr("scripts.market_funnel_job._run_layers", fake_run_layers)
    monkeypatch.setattr("integrations.supabase_recommendation.upsert_global_recommendations", fake_upsert)

    result = run_market_funnel("us", output=str(tmp_path / "us_result.json"), client=ManyCandidateTickFlowClient())

    assert len(result["top_candidates"]) == 50
    assert len(captured["rows"]) == 55
    assert captured["market"] == "us"
