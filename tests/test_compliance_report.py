from __future__ import annotations

import json

import pandas as pd


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "600000",
                "name": "浦发银行",
                "industry": "银行",
                "track": "Trend",
                "tag": "SOS",
                "funnel_score": 0.82,
            },
            {
                "code": "300001",
                "name": "特锐德",
                "industry": "电力设备",
                "track": "Accum",
                "tag": "LPS",
                "funnel_score": 0.55,
            },
        ]
    )


def test_public_payload_is_deidentified():
    from core.compliance_report import build_public_payload

    payload = build_public_payload(
        benchmark_context={"regime": "NEUTRAL", "breadth": {"ratio_pct": 51.2}},
        selected_df=_sample_df(),
        ops_codes=["600000"],
    )

    text = json.dumps(payload, ensure_ascii=False)
    assert "600000" not in text
    assert "300001" not in text
    assert "浦发银行" not in text
    assert "特锐德" not in text
    assert "银行" in text
    assert payload["sample_stats"]["candidate_count"] == 2
    assert payload["sample_stats"]["springboard_count"] == 1


def test_public_payload_handles_missing_industry_column():
    from core.compliance_report import build_public_payload

    payload = build_public_payload(
        benchmark_context={"regime": "NEUTRAL"},
        selected_df=pd.DataFrame([{"code": "600000", "name": "浦发银行", "tag": "SOS"}]),
    )

    assert payload["sample_stats"]["candidate_count"] == 1
    assert payload["sector_stats"] == []


def test_validate_compliance_report_blocks_codes_names_and_action_terms():
    from core.compliance_report import validate_compliance_report

    bad = "建议关注 600000 浦发银行，明日买入并设置止损。"
    result = validate_compliance_report(bad, forbidden_names=["浦发银行"])

    assert not result.ok
    assert "contains_stock_code" in result.reasons
    assert "contains_stock_name" in result.reasons
    assert any(reason.startswith("contains_term:") for reason in result.reasons)


def test_resolve_compliance_llm_prefers_openrouter(monkeypatch):
    from core.compliance_report import OPENROUTER_BASE_URL, resolve_compliance_llm_config

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "tencent/hy3-preview:free")
    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "longcat")
    monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://example.com/v1")

    cfg = resolve_compliance_llm_config()

    assert cfg is not None
    assert cfg.provider == "openrouter"
    assert cfg.api_key == "or-key"
    assert cfg.model == "tencent/hy3-preview:free"
    assert cfg.base_url == OPENROUTER_BASE_URL


def test_resolve_compliance_llm_uses_efficiency_when_openrouter_absent(monkeypatch):
    from core.compliance_report import resolve_compliance_llm_config

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setenv("EFFICIENCY_API_KEY", "eff-key")
    monkeypatch.setenv("EFFICIENCY_MODEL", "longcat")
    monkeypatch.setenv("EFFICIENCY_BASE_URL", "https://example.com/v1")

    cfg = resolve_compliance_llm_config()

    assert cfg is not None
    assert cfg.provider == "efficiency"
    assert cfg.base_url == "https://example.com/v1"


def test_generate_compliance_brief_fallback_has_no_stock_identifiers(monkeypatch):
    from core.compliance_report import generate_compliance_brief

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("EFFICIENCY_API_KEY", raising=False)
    monkeypatch.delenv("EFFICIENCY_MODEL", raising=False)
    monkeypatch.delenv("EFFICIENCY_BASE_URL", raising=False)

    text = generate_compliance_brief(
        benchmark_context={"regime": "RISK_OFF", "breadth": {"ratio_pct": 20}},
        selected_df=_sample_df(),
        ops_codes=["600000"],
        code_name={"600000": "浦发银行", "300001": "特锐德"},
    )

    assert "600000" not in text
    assert "300001" not in text
    assert "浦发银行" not in text
    assert "特锐德" not in text
    assert "市场观察简报" in text


def test_generate_compliance_brief_rejects_bad_llm_output(monkeypatch):
    import core.compliance_report as cr

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "tencent/hy3-preview:free")
    monkeypatch.setenv("STEP3_COMPLIANCE_MAX_RETRIES", "0")
    monkeypatch.setattr(cr, "call_llm", lambda **kwargs: "600000 浦发银行 可以买入")

    text = cr.generate_compliance_brief(
        benchmark_context={"regime": "NEUTRAL"},
        selected_df=_sample_df(),
        ops_codes=["600000"],
        code_name={"600000": "浦发银行"},
    )

    assert "600000" not in text
    assert "浦发银行" not in text
    assert "买入" not in text
