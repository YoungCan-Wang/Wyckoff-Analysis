from __future__ import annotations

from datetime import datetime

import pandas as pd

from core.tail_buy_strategy import (
    DECISION_BUY,
    DECISION_SKIP,
    DECISION_WATCH,
    TailBuyCandidate,
    _normalize_signal_score,
    build_tail_buy_markdown,
    compute_tail_features,
    evaluate_rule_decision,
    merge_rule_and_llm,
    pick_tail_candidates,
    select_llm_overlay_candidates,
)


def _make_intraday_df(
    *,
    start: float,
    end: float,
    bars: int = 180,
    tail_boost: float = 0.0,
    tail_volume_mult: float = 1.0,
) -> pd.DataFrame:
    idx = pd.date_range(
        start=datetime(2026, 4, 21, 9, 30),
        periods=bars,
        freq="1min",
        tz="Asia/Shanghai",
    )
    base = pd.Series([start + (end - start) * i / max(bars - 1, 1) for i in range(bars)])
    if tail_boost != 0.0:
        tail_n = min(30, bars)
        tail_delta = pd.Series(
            [tail_boost * (i + 1) / tail_n for i in range(tail_n)],
            index=base.index[-tail_n:],
        )
        base.iloc[-tail_n:] = base.iloc[-tail_n:].to_numpy() + tail_delta.to_numpy()
    close = base
    open_ = close.shift(1).fillna(close.iloc[0]) * 0.999
    high = close * 1.003
    low = close * 0.997
    volume = pd.Series([1200.0] * bars)
    tail_n = min(30, bars)
    volume.iloc[-tail_n:] = volume.iloc[-tail_n:] * tail_volume_mult
    amount = close * volume
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": open_.values,
            "high": high.values,
            "low": low.values,
            "close": close.values,
            "volume": volume.values,
            "amount": amount.values,
        }
    )


def test_pick_tail_candidates_filters_prev_trade_day_and_status():
    rows = [
        {
            "code": 301090,
            "name": "华润材料",
            "signal_type": "spring",
            "signal_score": 3.2,
            "status": "pending",
            "signal_date": "2026-04-20",
        },
        {
            "code": 301090,
            "name": "华润材料",
            "signal_type": "spring",
            "signal_score": 2.9,
            "status": "confirmed",
            "signal_date": "2026-04-20",
        },
        {
            "code": "600000",
            "name": "浦发银行",
            "signal_type": "sos",
            "signal_score": 1.5,
            "status": "expired",
            "signal_date": "2026-04-20",
        },
        {
            "code": "000001",
            "name": "平安银行",
            "signal_type": "lps",
            "signal_score": 2.1,
            "status": "pending",
            "signal_date": "2026-04-18",
        },
        {
            "code": "002217",
            "name": "合力泰",
            "signal_type": "sos",
            "signal_score": 5.1,
            "status": "pending",
            "signal_date": "2026-04-20",
        },
    ]
    got = pick_tail_candidates(rows, target_signal_date="2026-04-20")
    assert [x.code for x in got] == ["301090", "002217"]
    assert got[0].status == "confirmed"


def test_evaluate_rule_decision_buy_and_skip_split():
    strong = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
    )
    weak = TailBuyCandidate(
        code="600000",
        name="浦发银行",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=1.0,
    )
    strong_df = _make_intraday_df(start=10.0, end=10.9, tail_boost=0.8, tail_volume_mult=2.0)
    weak_df = _make_intraday_df(start=10.0, end=9.6, tail_boost=-0.2, tail_volume_mult=0.6)

    strong_out = evaluate_rule_decision(strong, strong_df, style="hybrid")
    weak_out = evaluate_rule_decision(weak, weak_df, style="hybrid")

    assert strong_out.rule_decision in {DECISION_BUY, DECISION_WATCH}
    assert strong_out.rule_score > weak_out.rule_score
    assert weak_out.rule_decision == DECISION_SKIP


def test_merge_rule_and_llm_keeps_non_top_symbols_on_rule_decision():
    c1 = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=5.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
    )
    c2 = TailBuyCandidate(
        code="002217",
        name="合力泰",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=4.0,
        rule_score=60.0,
        rule_decision=DECISION_WATCH,
        final_decision=DECISION_WATCH,
    )
    c3 = TailBuyCandidate(
        code="600000",
        name="浦发银行",
        signal_date="2026-04-20",
        status="pending",
        signal_type="sos",
        signal_score=2.0,
        rule_score=35.0,
        rule_decision=DECISION_SKIP,
        final_decision=DECISION_SKIP,
    )

    llm_map = {
        "002217": {
            "decision": DECISION_BUY,
            "reason": "尾盘再加速",
            "confidence": 0.76,
            "model_used": "nvidia-kimi:moonshot",
        },
        "301090": {
            "decision": DECISION_WATCH,
            "reason": "高位波动扩大",
            "confidence": 0.64,
            "model_used": "gemini:flash",
        },
    }
    merged = merge_rule_and_llm([c1, c2, c3], llm_map)
    by_code = {x.code: x for x in merged}

    assert by_code["002217"].final_decision == DECISION_BUY
    assert by_code["301090"].final_decision == DECISION_WATCH
    assert by_code["600000"].final_decision == DECISION_SKIP
    assert by_code["600000"].llm_decision is None
    assert by_code["002217"].llm_model_used.startswith("nvidia-kimi")


def test_compute_tail_features_handles_volume_lot_unit_for_vwap():
    df = _make_intraday_df(start=10.0, end=10.6, tail_boost=0.3, tail_volume_mult=1.3)
    # 模拟 TickFlow: volume 为“手”，amount 为“元”（需 /100 才接近真实价格）
    df["amount"] = df["close"] * df["volume"] * 100.0
    feats = compute_tail_features(df)
    assert feats["bars"] >= 60
    assert feats["vwap_volume_scale"] == 100.0
    assert 8.0 < feats["vwap"] < 20.0
    assert feats["dist_vwap_pct"] > -20.0


def test_select_llm_overlay_candidates_prefilters_skip_and_low_score():
    items = [
        TailBuyCandidate(
            code="301090",
            name="华润材料",
            signal_date="2026-04-20",
            status="confirmed",
            signal_type="spring",
            signal_score=6.0,
            rule_score=82.0,
            rule_decision=DECISION_BUY,
        ),
        TailBuyCandidate(
            code="002217",
            name="合力泰",
            signal_date="2026-04-20",
            status="pending",
            signal_type="sos",
            signal_score=4.0,
            rule_score=61.0,
            rule_decision=DECISION_WATCH,
        ),
        TailBuyCandidate(
            code="600000",
            name="浦发银行",
            signal_date="2026-04-20",
            status="pending",
            signal_type="sos",
            signal_score=2.0,
            rule_score=58.0,
            rule_decision=DECISION_WATCH,
        ),
        TailBuyCandidate(
            code="000001",
            name="平安银行",
            signal_date="2026-04-20",
            status="pending",
            signal_type="sos",
            signal_score=1.0,
            rule_score=75.0,
            rule_decision=DECISION_SKIP,
        ),
    ]

    selected = select_llm_overlay_candidates(
        items,
        max_llm_symbols=10,
        min_rule_score=60.0,
        allowed_rule_decisions=(DECISION_BUY, DECISION_WATCH),
    )

    assert [x.code for x in selected] == ["301090", "002217"]


def test_build_tail_buy_markdown_can_append_extra_sections():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        extra_sections=["## 持仓动作建议（加仓/减仓）\n- 持仓数量: 1"],
    )
    assert "持仓动作建议（加仓/减仓）" in md
    assert "持仓数量: 1" in md


def test_build_tail_buy_markdown_supports_custom_candidate_source():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        candidate_source="signal_pending + recommendation_tracking (2026-04-22)",
    )
    assert "signal_pending + recommendation_tracking (2026-04-22)" in md


def test_build_tail_buy_markdown_can_prepend_extra_sections():
    c = TailBuyCandidate(
        code="301090",
        name="华润材料",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=6.0,
        rule_score=80.0,
        rule_decision=DECISION_BUY,
        final_decision=DECISION_BUY,
        priority_score=90.0,
        rule_reasons=["尾盘走强"],
    )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=[c],
        llm_total=1,
        llm_success=1,
        elapsed_seconds=10.0,
        extra_sections=["## 持仓动作建议（加仓/减仓）\n- 持仓数量: 1"],
        extra_sections_first=True,
    )
    assert md.find("持仓动作建议（加仓/减仓）") < md.find("## BUY（优先关注）")


def test_normalize_signal_score_lps_inverted():
    assert _normalize_signal_score(0.2, "lps") > 6.0
    assert _normalize_signal_score(0.5, "lps") > 2.0
    assert _normalize_signal_score(0.65, "lps") == 0.0


def test_normalize_signal_score_sos_scales():
    assert _normalize_signal_score(2.0, "sos") == 0.0
    assert 4.5 < _normalize_signal_score(4.0, "sos") < 5.5
    assert _normalize_signal_score(6.0, "sos") == 10.0


def test_auto_style_selects_by_signal_type():
    strong_df = _make_intraday_df(start=10.0, end=10.9, tail_boost=0.8, tail_volume_mult=2.0)
    spring_c = TailBuyCandidate(
        code="301090",
        name="T",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="spring",
        signal_score=5.0,
    )
    sos_c = TailBuyCandidate(
        code="002217",
        name="T",
        signal_date="2026-04-20",
        status="confirmed",
        signal_type="sos",
        signal_score=4.0,
    )
    spring_out = evaluate_rule_decision(spring_c, strong_df)
    sos_out = evaluate_rule_decision(sos_c, strong_df)
    assert spring_out.rule_score > 0
    assert sos_out.rule_score > 0


def test_build_tail_buy_markdown_truncates_error_items_over_limit():
    items = []
    for i in range(7):
        items.append(
            TailBuyCandidate(
                code=f"60000{i}",
                name=f"样本{i}",
                signal_date="2026-04-20",
                status="pending",
                signal_type="sos",
                signal_score=1.0,
                rule_score=0.0,
                rule_decision=DECISION_SKIP,
                final_decision=DECISION_SKIP,
                priority_score=-20.0,
                fetch_error=f"ERR-{i}",
                rule_reasons=[f"ERR-{i}"],
            )
        )
    md = build_tail_buy_markdown(
        now_text="2026-04-23 14:10:00",
        target_signal_date="2026-04-22",
        market_reminder="NORMAL/NORMAL",
        candidates=items,
        llm_total=0,
        llm_success=0,
        elapsed_seconds=10.0,
        max_error_items_per_block=5,
    )
    assert md.count("ERR-") == 5
    assert "其余 2 只报错标的已省略" in md


def test_tail_buy_history_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "test.db")
    from integrations import local_db

    local_db._conn = None  # reset singleton
    local_db.init_db()

    from integrations.local_db import load_tail_buy_history, save_tail_buy_results

    rows = [
        {
            "code": "301090",
            "name": "华润材料",
            "run_date": "2026-04-25",
            "signal_date": "2026-04-24",
            "signal_type": "spring",
            "status": "confirmed",
            "final_decision": "BUY",
            "rule_score": 78.5,
            "priority_score": 90.5,
            "rule_reasons": '["尾盘走强"]',
            "llm_decision": "BUY",
            "llm_reason": "强势回踩",
        },
        {
            "code": "002217",
            "name": "合力泰",
            "run_date": "2026-04-25",
            "signal_date": "2026-04-24",
            "signal_type": "sos",
            "status": "pending",
            "final_decision": "WATCH",
            "rule_score": 55.0,
            "priority_score": 58.0,
            "rule_reasons": '["量能一般"]',
            "llm_decision": "WATCH",
            "llm_reason": "",
        },
    ]
    saved = save_tail_buy_results(rows)
    assert saved == 2

    all_records = load_tail_buy_history()
    assert len(all_records) == 2

    buy_only = load_tail_buy_history(decision="BUY")
    assert len(buy_only) == 1
    assert buy_only[0]["code"] == "301090"

    by_date = load_tail_buy_history(run_date="2026-04-25")
    assert len(by_date) == 2

    empty = load_tail_buy_history(run_date="2020-01-01")
    assert len(empty) == 0

    local_db._conn = None  # cleanup
