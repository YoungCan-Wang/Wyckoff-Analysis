"""core/export_artifacts.py 冒烟测试。"""

from __future__ import annotations

import pandas as pd

from core.export_artifacts import file_loader, write_dataframe_csv


class TestWriteDataframeCsv:
    def test_roundtrip(self, tmp_path, monkeypatch):
        """写出 CSV 后应能重新读回相同数据。"""
        import core.export_artifacts as _mod

        monkeypatch.setattr(_mod, "_EXPORT_ROOT", tmp_path)
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        path = write_dataframe_csv(df, prefix="test")
        assert path.exists()
        assert path.parent == tmp_path  # 确认写入了隔离目录
        loaded = pd.read_csv(path)
        assert list(loaded.columns) == ["a", "b"]
        assert len(loaded) == 3


class TestFileLoader:
    def test_returns_bytes(self, tmp_path):
        p = tmp_path / "sample.csv"
        p.write_text("a,b\n1,2\n", encoding="utf-8")
        loader = file_loader(str(p))
        data = loader()
        assert isinstance(data, bytes)
        assert b"a,b" in data
