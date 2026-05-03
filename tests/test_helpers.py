"""utils/helpers.py 冒烟测试。"""

from __future__ import annotations

from utils.helpers import extract_symbols_from_text


class TestExtractSymbolsFromText:
    def test_semicolon_separated(self):
        result = extract_symbols_from_text("000001；600519；300364")
        assert "000001" in result
        assert "600519" in result
        assert "300364" in result

    def test_dedup(self):
        result = extract_symbols_from_text("000001, 000001, 000001")
        assert result.count("000001") == 1

    def test_empty_string(self):
        result = extract_symbols_from_text("")
        assert result == []

    def test_with_valid_codes_filter(self):
        result = extract_symbols_from_text("000001 999999", valid_codes={"000001"})
        assert "000001" in result
        assert "999999" not in result
