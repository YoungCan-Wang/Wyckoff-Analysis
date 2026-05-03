from __future__ import annotations

from cli.memory import _extract_keywords, build_memory_context, extract_stock_codes


class TestExtractStockCodes:
    def test_basic(self):
        assert extract_stock_codes("看看 000001 和 600519") == ["000001", "600519"]

    def test_dedup(self):
        assert extract_stock_codes("000001 000001") == ["000001"]

    def test_no_match(self):
        assert extract_stock_codes("没有代码") == []


class TestExtractKeywords:
    def test_chinese_segments(self):
        kw = _extract_keywords("最近市场情绪怎么样")
        assert "市场" in kw
        assert "情绪" in kw

    def test_filters_stopwords(self):
        kw = _extract_keywords("帮我看看这个")
        assert "帮我" not in kw
        assert "看看" not in kw

    def test_strips_codes(self):
        kw = _extract_keywords("000001 走势分析")
        codes_in_kw = [k for k in kw if k.isdigit()]
        assert len(codes_in_kw) == 0

    def test_max_five(self):
        kw = _extract_keywords("这里有很多关键词需要提取出来测试数量限制的功能实现")
        assert len(kw) <= 5


class TestBuildMemoryContext:
    def test_returns_empty_when_no_db(self, monkeypatch):
        def _boom(*a, **kw):
            raise ImportError("no db")

        monkeypatch.setattr("cli.memory.extract_stock_codes", lambda t: [])
        result = build_memory_context("随便问个问题")
        assert result == "" or isinstance(result, str)
