"""core/backtester.py re-export 桥接测试。"""

from __future__ import annotations

import pytest

akshare = pytest.importorskip("akshare", reason="akshare not installed")


def test_bridge_exports_are_importable():
    """确认桥接模块能正常 import 所有公共 API。"""
    from core.backtester import (
        calc_max_drawdown_pct,
        fmt_metric,
        parse_date,
        run_backtest,
    )

    assert callable(run_backtest)
    assert callable(calc_max_drawdown_pct)
    assert callable(parse_date)
    assert callable(fmt_metric)
