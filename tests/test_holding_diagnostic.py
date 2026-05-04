"""Harness tests for core.holding_diagnostic module."""

from __future__ import annotations

import pandas as pd

from core.holding_diagnostic import (
    HoldingDiagnostic,
    diagnose_holdings,
    diagnose_one_stock,
    format_diagnostic_text,
)
from tests.helpers.golden import assert_golden
from tests.helpers.synthetic_data import make_ohlcv


class TestDiagnoseOneStock:
    def test_healthy_uptrend(self):
        df = make_ohlcv(n=250, trend="up", base=10.0, volatility=0.008, seed=1)
        result = diagnose_one_stock("600519", "贵州茅台", cost=10.0, df=df)

        assert isinstance(result, HoldingDiagnostic)
        assert result.health == "🟢健康"
        assert result.ma_pattern in ("多头排列", "MA50>MA200(偏强)")
        assert result.pnl_pct > 0
        assert result.ma50 is not None
        assert result.ma200 is not None

    def test_danger_stop_loss_breached(self):
        df = make_ohlcv(n=250, trend="down", base=20.0, seed=2)
        latest = float(df["close"].iloc[-1])
        cost = latest * 1.15  # cost 15% above current → breached 7% stop
        result = diagnose_one_stock("000001", "平安银行", cost=cost, df=df)

        assert result.health == "🔴危险"
        assert result.stop_loss_status == "已穿止损"
        assert any("已穿" in r for r in result.health_reasons)

    def test_warning_signals(self):
        df = make_ohlcv(n=250, trend="down", base=15.0, seed=3)
        latest = float(df["close"].iloc[-1])
        cost = latest * 1.06  # moderate loss
        result = diagnose_one_stock("002230", "科大讯飞", cost=cost, df=df)

        assert result.health in ("🟡警戒", "🔴危险")
        assert len(result.health_reasons) > 0

    def test_short_dataframe_no_crash(self):
        df = make_ohlcv(n=10, trend="flat", base=12.0, seed=4)
        result = diagnose_one_stock("300750", "宁德时代", cost=12.0, df=df)

        assert isinstance(result, HoldingDiagnostic)
        assert result.ma50 is None
        assert result.ma200 is None
        assert result.ma_pattern == "数据不足"


class TestDiagnoseHoldings:
    def test_empty_dataframe_returns_danger(self):
        results = diagnose_holdings(
            holdings=[("600519", "贵州茅台", 1800.0)],
            df_map={"600519": pd.DataFrame()},
        )
        assert len(results) == 1
        assert results[0].health == "🔴危险"
        assert "无法获取行情数据" in results[0].health_reasons

    def test_missing_code_returns_danger(self):
        results = diagnose_holdings(
            holdings=[("999999", "不存在", 10.0)],
            df_map={},
        )
        assert len(results) == 1
        assert results[0].health == "🔴危险"


class TestFormatDiagnosticText:
    def test_golden_healthy(self):
        df = make_ohlcv(n=250, trend="up", base=10.0, volatility=0.008, seed=1)
        d = diagnose_one_stock("600519", "贵州茅台", cost=10.0, df=df)
        text = format_diagnostic_text(d)
        assert_golden("diagnostic_healthy.txt", text)

    def test_golden_danger(self):
        df = make_ohlcv(n=250, trend="down", base=20.0, seed=2)
        latest = float(df["close"].iloc[-1])
        d = diagnose_one_stock("000001", "平安银行", cost=latest * 1.15, df=df)
        text = format_diagnostic_text(d)
        assert_golden("diagnostic_danger.txt", text)
