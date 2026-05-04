from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy_compare import (
    STRATEGY_V1_CURRENT,
    STRATEGY_V2_STRUCTURE,
    StrategyRun,
    compare_strategy_runs,
    extract_l4_candidates,
    format_strategy_comparison_markdown,
)
from core.wyckoff_engine import FunnelConfig, FunnelResult
from core.wyckoff_v2_structure import detect_structure_triggers, identify_trading_range


def _range_df(n: int = 120) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=n)
    x = np.linspace(0, 10 * np.pi, n)
    close = 11.0 + 0.9 * np.sin(x)
    open_ = close * 0.998
    high = close + 0.22
    low = close - 0.22
    volume = np.full(n, 1_000_000.0)
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "pct_chg": pd.Series(close).pct_change().fillna(0.0) * 100.0,
        }
    )


def _empty_result(triggers: dict[str, list[tuple[str, float]]]) -> FunnelResult:
    return FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers=triggers,
        stage_map={"000001": "Markup", "000002": "Accum_C", "000003": "Accum_B"},
        markup_symbols=[],
        exit_signals={},
        channel_map={"000001": "main", "000002": "accum", "000003": "structure"},
    )


def test_identify_trading_range_from_repeated_swings():
    df = _range_df()
    cfg = FunnelConfig()

    tr = identify_trading_range(df, cfg, exclude_last=0)

    assert tr is not None
    assert 9.5 <= tr.support <= 10.5
    assert 11.5 <= tr.resistance <= 12.5
    assert tr.support_tests >= 2
    assert tr.resistance_tests >= 2


def test_structure_spring_uses_prior_trading_range():
    df = _range_df()
    # Last bar pierces the already visible support and recovers above it.
    df.loc[df.index[-1], ["open", "high", "low", "close", "volume", "pct_chg"]] = [
        10.0,
        10.7,
        9.55,
        10.45,
        1_700_000.0,
        4.0,
    ]
    cfg = FunnelConfig()
    cfg.spring_vol_ratio = 1.0

    result = detect_structure_triggers(["000001"], {"000001": df}, cfg)

    assert result.trading_ranges["000001"].support < 10.5
    assert result.triggers["spring"]
    assert result.stage_map["000001"] == "Accum_C"


def test_structure_sos_uses_dynamic_resistance():
    df = _range_df()
    df.loc[df.index[-1], ["open", "high", "low", "close", "volume", "pct_chg"]] = [
        11.6,
        12.9,
        11.5,
        12.65,
        3_000_000.0,
        7.0,
    ]
    cfg = FunnelConfig()
    cfg.sos_pct_min = 5.0
    cfg.sos_vol_ratio = 2.0

    result = detect_structure_triggers(["000001"], {"000001": df}, cfg)

    assert result.triggers["sos"]
    assert result.stage_map["000001"] == "Markup"


def test_strategy_compare_counts_overlap_and_unique_candidates():
    v1 = StrategyRun(
        strategy_id=STRATEGY_V1_CURRENT,
        result=_empty_result({"sos": [("000001", 2.0)], "spring": [("000002", 1.0)], "lps": [], "evr": []}),
        candidates=(),
    )
    v2 = StrategyRun(
        strategy_id=STRATEGY_V2_STRUCTURE,
        result=_empty_result({"sos": [("000001", 3.0)], "spring": [("000003", 1.5)], "lps": [], "evr": []}),
        candidates=(),
    )
    v1 = StrategyRun(v1.strategy_id, v1.result, extract_l4_candidates(v1.result, v1.strategy_id))
    v2 = StrategyRun(v2.strategy_id, v2.result, extract_l4_candidates(v2.result, v2.strategy_id))

    comparison = compare_strategy_runs((v1, v2))

    assert comparison.intersection == ("000001",)
    assert comparison.only_by_strategy[STRATEGY_V1_CURRENT] == ("000002",)
    assert comparison.only_by_strategy[STRATEGY_V2_STRUCTURE] == ("000003",)
    assert comparison.counts["union"] == 3


def test_shadow_report_uses_standalone_funnel_style():
    v1 = StrategyRun(
        strategy_id=STRATEGY_V1_CURRENT,
        result=_empty_result({"sos": [("000001", 2.0)], "spring": [("000002", 1.0)], "lps": [], "evr": []}),
        candidates=(),
    )
    v2 = StrategyRun(
        strategy_id=STRATEGY_V2_STRUCTURE,
        result=_empty_result({"sos": [("000001", 3.0)], "spring": [("000003", 1.5)], "lps": [], "evr": []}),
        candidates=(),
    )
    v1 = StrategyRun(v1.strategy_id, v1.result, extract_l4_candidates(v1.result, v1.strategy_id))
    v2 = StrategyRun(v2.strategy_id, v2.result, extract_l4_candidates(v2.result, v2.strategy_id))
    comparison = compare_strategy_runs((v1, v2))

    report = format_strategy_comparison_markdown(
        (v1, v2),
        comparison,
        name_map={"000001": "平安银行", "000002": "万科A", "000003": "国农科技"},
        sector_map={"000001": "银行", "000002": "房地产", "000003": "农业"},
        benchmark_context={
            "regime": "RISK_ON",
            "close": 3200.0,
            "ma50": 3100.0,
            "ma200": 3000.0,
            "recent3_cum_pct": 1.2,
            "breadth": {"ratio_pct": 55.0},
        },
        input_symbol_count=3,
    )

    assert "**股票池**: 影子池 3 只" in report
    assert "**漏斗概览**: 3只 → 结构命中:2" in report
    assert "**【⚡ SOS（强势信号） 量价点火】1 只**" in report
    assert "000001 平安银行" in report
    assert "[银行]" in report
    assert "SOS（强势信号）" in report
    assert "正式" not in report
    assert "V1" not in report
    assert "对照" not in report
    assert "比较" not in report
