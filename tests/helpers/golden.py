"""Lightweight golden-file assertion utility."""

from __future__ import annotations

from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"


def assert_golden(name: str, actual: str) -> None:
    """Compare actual output against a golden file.

    On first run (file missing), creates the golden file and skips.
    On subsequent runs, asserts exact match.
    To update: delete the golden file and re-run tests.
    """
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / name
    if not path.exists():
        path.write_text(actual, encoding="utf-8")
        pytest.skip(f"Golden file created: {path}")
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, f"Output changed vs golden file {path}.\nDelete the file and re-run to update."
