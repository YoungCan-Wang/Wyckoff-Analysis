# -*- coding: utf-8 -*-
"""core/backtester.py re-export 桥接测试。"""
from __future__ import annotations


def test_bridge_exports_are_importable():
    """确认桥接模块能正常 import 所有公共 API。"""
    from core.backtester import (
        _calc_calmar_ratio,
        _calc_cvar95_pct,
        _calc_information_ratio,
        _calc_max_drawdown_pct,
        _calc_sharpe_ratio,
        _fmt_metric,
        _parse_date,
        run_backtest,
    )
    assert callable(run_backtest)
    assert callable(_calc_max_drawdown_pct)
    assert callable(_parse_date)
