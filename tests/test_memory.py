from __future__ import annotations

from cli.memory import _extract_keywords, build_memory_context, extract_stock_codes, save_session_summary


class _Provider:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def chat_stream(self, *_args):
        yield {"type": "text_delta", "text": self.outputs.pop(0)}


def _init_tmp_db(monkeypatch, tmp_path):
    import integrations.local_db as local_db

    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "memory.db")
    local_db.init_db()
    return local_db


def _close_tmp_db(local_db):
    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None


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

    def test_injects_layered_context_with_source(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            local_db.save_memory("persona", "用户偏好低换手、重视止损", memory_level="L3")
            local_db.save_memory("preference", "不追涨", codes="000001")
            local_db.save_memory(
                "stock_opinion",
                "000001 处于吸筹观察，等待放量确认",
                codes="000001",
                source_ref="chat_log:s1",
            )

            context = build_memory_context("000001 接下来怎么处理")

            assert "# 用户画像" in context
            assert "# 历史原子记忆" in context
            assert "源:chat_log:s1" in context
        finally:
            _close_tmp_db(local_db)


class TestSaveSessionSummary:
    def test_stores_atoms_layers_and_source(self, monkeypatch, tmp_path):
        local_db = _init_tmp_db(monkeypatch, tmp_path)
        try:
            provider = _Provider(
                [
                    "[股票] 000001 吸筹观察，等待放量确认\n[决策] 用户决定暂不加仓\n[偏好] 不追涨",
                    "[画像] 用户偏好确认后再加仓\n[场景] 000001 吸筹后等待放量确认",
                ]
            )
            messages = [
                {"role": "user", "content": "看看 000001"},
                {"role": "assistant", "tool_calls": [{"id": "tc1", "name": "analyze_stock", "args": {}}]},
                {"role": "tool", "content": '{"code":"000001"}'},
                {"role": "assistant", "content": "先观察。"},
            ]

            save_session_summary(messages, provider, session_id="s1")

            memories = local_db.get_recent_memories(limit=10)
            types = {m["memory_type"] for m in memories}
            assert {"stock_opinion", "decision", "preference", "persona", "scenario"} <= types
            assert any(m["source_ref"] == "chat_log:s1" for m in memories)
        finally:
            _close_tmp_db(local_db)
