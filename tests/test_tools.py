"""tools/ 层单元测试 — 测试 Phase 2 提取的纯逻辑 Tool 函数。"""

from __future__ import annotations

import pandas as pd

# ── tools/funnel_config ──


class TestFunnelConfig:
    def test_parse_int_env_reads_env(self, monkeypatch):
        from tools.funnel_config import parse_int_env

        monkeypatch.setenv("_TEST_INT", "42")
        assert parse_int_env("_TEST_INT", 0) == 42

    def test_parse_int_env_fallback_on_missing(self, monkeypatch):
        from tools.funnel_config import parse_int_env

        monkeypatch.delenv("_TEST_INT", raising=False)
        assert parse_int_env("_TEST_INT", 7) == 7

    def test_parse_int_env_handles_float_string(self, monkeypatch):
        from tools.funnel_config import parse_int_env

        monkeypatch.setenv("_TEST_INT", "5.0")
        assert parse_int_env("_TEST_INT", 0) == 5

    def test_parse_bool_truthy(self):
        from tools.funnel_config import parse_bool

        for val in ("1", "true", "True", "yes", "on"):
            assert parse_bool(val) is True, f"Expected True for {val!r}"

    def test_parse_bool_falsy(self):
        from tools.funnel_config import parse_bool

        for val in ("0", "false", "no", "off", ""):
            assert parse_bool(val) is False, f"Expected False for {val!r}"


# ── tools/report_builder ──


class TestReportBuilder:
    def test_extract_ops_codes_from_markdown_happy_path(self):
        from tools.report_builder import _extract_ops_codes_from_markdown

        report = (
            "# \u5904\u4e8e\u8d77\u8df3\u677f\n"
            "- 600056 \u4e2d\u56fd\u533b\u836f\n"
            "- 300632 \u5149\u83c6\u80a1\u4efd\n"
            "# \u903b\u8f91\u7834\u4ea7\n"
            "- 000001 \u5e73\u5b89\u94f6\u884c\n"
        )
        allowed = {"600056", "300632", "000001"}
        result = _extract_ops_codes_from_markdown(report, allowed)
        assert result == ["600056", "300632"]
        assert "000001" not in result

    def test_extract_ops_codes_empty_report(self):
        from tools.report_builder import _extract_ops_codes_from_markdown

        assert _extract_ops_codes_from_markdown("", set()) == []

    def test_try_parse_structured_report_none_on_empty(self):
        from tools.report_builder import _try_parse_structured_report

        assert _try_parse_structured_report("", set(), {}) is None

    def test_extract_json_block_strips_fences(self):
        from tools.report_builder import _extract_json_block

        raw = '```json\n{"key": "value"}\n```'
        result = _extract_json_block(raw)
        assert result == '{"key": "value"}'

    def test_extract_json_block_plain_json(self):
        from tools.report_builder import _extract_json_block

        raw = '{"a": 1}'
        assert _extract_json_block(raw) == '{"a": 1}'

    def test_extract_operation_pool_codes_happy_path(self):
        from tools.report_builder import extract_operation_pool_codes

        report = "# \u5904\u4e8e\u8d77\u8df3\u677f\n- 600056 \u4e2d\u56fd\u533b\u836f\n"
        codes = extract_operation_pool_codes(report, ["600056", "300632"])
        assert "600056" in codes

    def test_extract_operation_pool_codes_deduplicates(self):
        from tools.report_builder import extract_operation_pool_codes

        report = "# \u5904\u4e8e\u8d77\u8df3\u677f\n- 600056 A\n- 600056 B\n"
        codes = extract_operation_pool_codes(report, ["600056"])
        assert codes == ["600056"]


# ── tools/candidate_ranker ──


class TestCandidateRanker:
    def test_calc_close_return_pct_normal(self):
        from tools.candidate_ranker import calc_close_return_pct

        s = pd.Series([100.0, 105.0, 110.0])
        result = calc_close_return_pct(s, lookback=1)
        assert result is not None
        assert abs(result - 4.76) < 0.1  # (110-105)/105 * 100

    def test_calc_close_return_pct_short_series(self):
        from tools.candidate_ranker import calc_close_return_pct

        s = pd.Series([100.0])
        assert calc_close_return_pct(s, lookback=5) is None

    def test_calc_close_return_pct_zero_start(self):
        from tools.candidate_ranker import calc_close_return_pct

        s = pd.Series([0.0, 10.0, 20.0])
        # lookback=1 → start=10, end=20 → 100%
        result = calc_close_return_pct(s, lookback=1)
        assert result is not None
        assert abs(result - 100.0) < 0.1

    def test_trigger_labels_is_dict(self):
        from tools.candidate_ranker import TRIGGER_LABELS

        assert isinstance(TRIGGER_LABELS, dict)
        assert "sos" in TRIGGER_LABELS
        assert "spring" in TRIGGER_LABELS
        assert len(TRIGGER_LABELS) == 4


# ── tools/market_regime ──


class TestMarketRegime:
    def test_imports_callable(self):
        from tools.market_regime import analyze_benchmark_and_tune_cfg, calc_market_breadth

        assert callable(analyze_benchmark_and_tune_cfg)
        assert callable(calc_market_breadth)

    def test_calc_market_breadth_empty(self):
        from tools.market_regime import calc_market_breadth

        result = calc_market_breadth({})
        assert result["ratio_pct"] is None
        assert result["sample_size"] == 0


# ── tools/data_fetcher ──


class TestDataFetcher:
    def test_latest_trade_date_from_hist_empty(self):
        from tools.data_fetcher import latest_trade_date_from_hist

        assert latest_trade_date_from_hist(pd.DataFrame()) is None

    def test_latest_trade_date_from_hist_no_date_col(self):
        from tools.data_fetcher import latest_trade_date_from_hist

        df = pd.DataFrame({"close": [1, 2, 3]})
        assert latest_trade_date_from_hist(df) is None

    def test_latest_trade_date_from_hist_valid(self):
        from datetime import date

        from tools.data_fetcher import latest_trade_date_from_hist

        df = pd.DataFrame({"date": ["2025-01-01", "2025-01-02"]})
        result = latest_trade_date_from_hist(df)
        assert result == date(2025, 1, 2)


# ── tools/symbol_pool ──


class TestSymbolPool:
    def test_stock_name_map_callable(self):
        from tools.symbol_pool import _stock_name_map

        assert callable(_stock_name_map)


# ── core/strategy bridge ──


class TestStrategyBridge:
    def test_bridge_exports_are_importable(self):
        from core.strategy import run_step4

        assert callable(run_step4)
