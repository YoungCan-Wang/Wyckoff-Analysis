"""core/funnel_pipeline.py re-export 桥接测试。"""

from __future__ import annotations

import pytest

akshare = pytest.importorskip("akshare", reason="akshare not installed")


def test_bridge_exports_are_importable():
    """确认桥接模块能正常 import 所有公共 API。"""
    from core.funnel_pipeline import (
        TRIGGER_LABELS,
        analyze_benchmark_and_tune_cfg,
        calc_market_breadth,
        rank_l3_candidates,
        run_funnel,
        run_funnel_job,
    )

    assert isinstance(TRIGGER_LABELS, (dict, list, tuple))
    assert callable(run_funnel)
    assert callable(run_funnel_job)
    assert callable(analyze_benchmark_and_tune_cfg)
    assert callable(calc_market_breadth)
    assert callable(rank_l3_candidates)
