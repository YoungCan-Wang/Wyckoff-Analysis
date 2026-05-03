"""
Wyckoff 漏斗管线 — 公共 API 转发层。

将 tools/ 和 scripts/wyckoff_funnel.py 中被其他模块引用的函数集中 re-export，
使消费者从 core/ 导入而非直接从 scripts/ 或 tools/ 导入，保持分层干净。
"""

# run / run_funnel_job 仍在 scripts/（CLI 入口逻辑）
from scripts.wyckoff_funnel import (  # noqa: F401
    run as run_funnel,
)
from scripts.wyckoff_funnel import (
    run_funnel_job,
)
from tools.candidate_ranker import (  # noqa: F401
    TRIGGER_LABELS,
    rank_l3_candidates,
)
from tools.market_regime import (  # noqa: F401
    analyze_benchmark_and_tune_cfg,
    calc_market_breadth,
)

__all__ = [
    "TRIGGER_LABELS",
    "analyze_benchmark_and_tune_cfg",
    "calc_market_breadth",
    "rank_l3_candidates",
    "run_funnel",
    "run_funnel_job",
]
