"""
批量研报管线 — 公共 API 转发层。

将 tools/ 和 scripts/step3_batch_report.py 中被其他模块引用的函数集中 re-export，
使消费者从 core/ 导入而非直接从 scripts/ 或 tools/ 导入，保持分层干净。
"""

# run 仍在 scripts/（CLI 入口逻辑）
from scripts.step3_batch_report import (  # noqa: F401
    run as run_step3,
)
from tools.report_builder import (  # noqa: F401
    extract_operation_pool_codes,
    generate_stock_payload,
)

__all__ = [
    "extract_operation_pool_codes",
    "generate_stock_payload",
    "run_step3",
]
