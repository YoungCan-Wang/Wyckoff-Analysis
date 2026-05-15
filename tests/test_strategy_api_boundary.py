from pathlib import Path


def test_tail_buy_job_does_not_import_private_tail_strategy():
    source = Path("scripts/tail_buy_intraday_job.py").read_text(encoding="utf-8")
    assert "core.tail_buy_strategy" not in source
    for name in (
        "compute_tail_features",
        "score_tail_features",
        "evaluate_rule_decision",
        "build_llm_prompt",
        "merge_rule_and_llm",
        "parse_llm_decision",
        "select_llm_overlay_candidates",
    ):
        assert name not in source
