"""真实海天年报的旧提取 → v2 适配 → 独立原文核验链路。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finagent.ingestion import extract_annual_financial_facts, parse_text_pdf
from finagent.schemas.financial_fact_v2 import adapt_legacy_financial_fact
from finagent.verification.independent_fact import verify_financial_fact

_ROOT = Path(__file__).resolve().parents[2]
_PDF = _ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "1222994233.PDF"
_EXPECTED_PAGES = {
    ("revenue", 2024): 81,
    ("revenue", 2023): 81,
    ("net_profit_parent", 2024): 82,
    ("net_profit_parent", 2023): 82,
    ("operating_cash_flow", 2024): 86,
    ("operating_cash_flow", 2023): 86,
    ("non_recurring_total", 2024): 9,
    ("non_recurring_total", 2023): 9,
}


def test_haitian_legacy_extraction_adaptation_and_independent_verification() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    parsed = parse_text_pdf(_PDF, document_id="cninfo-1222994233")
    extracted = extract_annual_financial_facts(parsed, company_id="603288", report_year=2024)
    assert extracted.issues == ()
    assert len(extracted.facts) == len(_EXPECTED_PAGES)

    verified = {}
    for legacy_fact in extracted.facts:
        fact, _legacy_evidence = adapt_legacy_financial_fact(legacy_fact, report_year=2024)
        assert fact.unit is None

        checked = verify_financial_fact(_PDF, fact)
        key = (fact.metric_id, fact.period_end.year if fact.period_end else None)
        assert checked.result.status == "verified", (key, checked.result.limitations)
        assert key in _EXPECTED_PAGES
        assert key not in verified
        verified[key] = checked

        evidence = checked.evidence[0]
        assert evidence.value_region is not None
        assert evidence.value_region.page == _EXPECTED_PAGES[key]
        assert evidence.value_raw == legacy_fact.raw_value
        assert evidence.value_normalized == legacy_fact.normalized_value
        assert evidence.unit == "元"
        assert evidence.currency == "人民币"
        assert evidence.period_end == date(key[1], 12, 31)
        assert evidence.evidence_id not in fact.evidence_ids
        if fact.metric_id == "net_profit_parent":
            assert evidence.row_region is not None
            assert "归属于母公司股东的净利润" in evidence.row_region.text
        if fact.metric_id == "operating_cash_flow":
            assert evidence.row_region is not None
            assert "经营活动产生的现金流量净额" in evidence.row_region.text

    assert set(verified) == set(_EXPECTED_PAGES)
