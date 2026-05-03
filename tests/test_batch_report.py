"""core/batch_report.py re-export 桥接测试。"""

from __future__ import annotations

import pytest

akshare = pytest.importorskip("akshare", reason="akshare not installed")


def test_bridge_exports_are_importable():
    """确认桥接模块能正常 import 所有公共 API。"""
    from core.batch_report import (
        extract_operation_pool_codes,
        generate_stock_payload,
        run_step3,
    )

    assert callable(generate_stock_payload)
    assert callable(extract_operation_pool_codes)
    assert callable(run_step3)
