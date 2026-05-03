"""
持仓再平衡策略 -- 公共 API 转发层。

re-export scripts/step4_rebalancer.run 为 run_step4，
使消费者从 core/ 导入而非直接从 scripts/ 导入，保持分层干净。
"""

from scripts.step4_rebalancer import (  # noqa: F401
    run as run_step4,
)

__all__ = [
    "run_step4",
]
