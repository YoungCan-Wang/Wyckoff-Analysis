# -*- coding: utf-8 -*-
"""core/funnel_pipeline.py re-export 桥接测试。"""
from __future__ import annotations


def test_bridge_exports_are_importable():
    """确认桥接模块能正常 import 所有公共 API。"""
    from core.funnel_pipeline import (
        TRIGGER_LABELS,
        _analyze_benchmark_and_tune_cfg,
        _calc_market_breadth,
        _rank_l3_candidates,
        run_funnel,
        run_funnel_job,
    )
    assert isinstance(TRIGGER_LABELS, (dict, list, tuple))
    assert callable(run_funnel)
    assert callable(run_funnel_job)
