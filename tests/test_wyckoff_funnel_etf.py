from __future__ import annotations

import pandas as pd

import scripts.wyckoff_funnel as funnel
from scripts.wyckoff_funnel import (
    _append_etf_section,
    _merge_trigger_maps,
    _promote_l2_bypass_for_ai,
    _rank_etf_candidates,
    _rank_l2_bypass_pool,
)


def _frame(step: float, last_volume: float) -> pd.DataFrame:
    dates = pd.date_range("2026-04-01", periods=30, freq="B")
    close = pd.Series([100.0 + i * step for i in range(30)])
    volume = pd.Series([100.0] * 29 + [last_volume])
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "close": close,
            "volume": volume,
        }
    )


def test_rank_etf_candidates_orders_by_strength():
    rows = _rank_etf_candidates(
        ["512880", "512480"],
        {
            "512880": _frame(0.1, 100.0),
            "512480": _frame(1.0, 280.0),
        },
        {"512880": "证券", "512480": "半导体"},
        {"512880": "吸筹通道", "512480": "主升通道+点火破局"},
    )

    assert [row["code"] for row in rows] == ["512480", "512880"]
    assert rows[0]["name"] == "半导体ETF"
    assert rows[0]["ret20"] > rows[1]["ret20"]


def test_append_etf_section_renders_compact_rows():
    rows = [
        {
            "code": "512480",
            "name": "半导体ETF",
            "score": 12.3,
            "ret3": 2.1,
            "ret20": 10.5,
            "vol_ratio": 1.8,
            "channel": "主升通道",
        }
    ]
    lines: list[str] = []

    _append_etf_section(lines, {"pool": 2, "fetched": 2, "l2_passed": 1}, rows)

    text = "\n".join(lines)
    assert "ETF强势池" in text
    assert "512480 半导体ETF" in text
    assert "3日+2.1%" in text


def test_merge_trigger_maps_keeps_bypass_l4_hits():
    merged = _merge_trigger_maps(
        {"lps": [("000001", 1.0)], "evr": [("000002", 2.0)]},
        {"lps": [("000001", 9.0), ("000003", 3.0)]},
    )

    assert merged["lps"] == [("000001", 1.0), ("000003", 3.0)]
    assert merged["evr"] == [("000002", 2.0)]


def test_promote_l2_bypass_for_ai_assigns_tracks_and_scores():
    selected = ["000001"]
    trend = ["000001"]
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = _promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000002", "000003"],
        {"000002": 4.0, "000003": 8.0},
        {"000002": ["lps"], "000003": ["evr"]},
        score_map,
    )

    assert added == 2
    assert selected == ["000001", "000003", "000002"]
    assert trend == ["000001", "000003"]
    assert accum == ["000002"]
    assert score_map["000002"] == 4.0


def test_rank_l2_bypass_pool_orders_by_score_then_code():
    ranked = _rank_l2_bypass_pool(
        ["000003", "000001", "000002", "000002"],
        {"000001": 5.0, "000002": 8.0, "000003": 8.0},
    )

    assert ranked == ["000002", "000003", "000001"]


def test_promote_l2_bypass_for_ai_respects_budget(monkeypatch):
    monkeypatch.setattr(funnel, "FUNNEL_L2_BYPASS_AI_CAP", 2)
    selected: list[str] = []
    trend: list[str] = []
    accum: list[str] = []
    score_map: dict[str, float] = {}

    added = funnel._promote_l2_bypass_for_ai(
        selected,
        trend,
        accum,
        ["000001", "000002", "000003"],
        {"000001": 1.0, "000002": 3.0, "000003": 2.0},
        {"000001": ["evr"], "000002": ["evr"], "000003": ["evr"]},
        score_map,
    )

    assert added == 2
    assert selected == ["000002", "000003"]
