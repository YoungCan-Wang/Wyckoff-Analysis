"""core/funnel_pipeline.py re-export 桥接测试。"""

from __future__ import annotations

import pytest

akshare = pytest.importorskip("akshare", reason="akshare not installed")


def test_bridge_exports_are_importable():
    """确认桥接模块能正常 import tools 层公共 API。"""
    from core.funnel_pipeline import (
        TRIGGER_LABELS,
        analyze_benchmark_and_tune_cfg,
        calc_market_breadth,
        rank_l3_candidates,
    )

    assert isinstance(TRIGGER_LABELS, (dict, list, tuple))
    assert callable(analyze_benchmark_and_tune_cfg)
    assert callable(calc_market_breadth)
    assert callable(rank_l3_candidates)


def test_scripts_layer_exports():
    """确认 scripts 层入口函数可直接导入。"""
    from scripts.wyckoff_funnel import run, run_funnel_job

    assert callable(run)
    assert callable(run_funnel_job)
