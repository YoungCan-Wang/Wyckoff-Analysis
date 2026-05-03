from __future__ import annotations

import pytest

pytest.importorskip("textual")

from cli.tui import _pop_lines, _write_counted


class _FakeLog:
    def __init__(self) -> None:
        self.lines = ["kept"]
        self._widest_line_width = 0
        self.virtual_size = None
        self.refreshed = False

    def write(self, renderable: list[str]) -> None:
        self.lines.extend(renderable)

    def refresh(self) -> None:
        self.refreshed = True


def test_write_counted_returns_actual_added_strips_for_wrapped_renderable():
    log = _FakeLog()

    added = _write_counted(log, ["wrap line 1", "wrap line 2"])

    assert added == 2
    assert log.lines == ["kept", "wrap line 1", "wrap line 2"]


def test_pop_lines_removes_actual_added_strips():
    log = _FakeLog()
    added = _write_counted(log, ["wrap line 1", "wrap line 2"])

    _pop_lines(log, added)

    assert log.lines == ["kept"]
    assert log.refreshed is True
