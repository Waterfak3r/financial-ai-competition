"""从海天 2024 原始 PDF 验收到 FINTRACE v2 报告的真实跨模块回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from finagent.finance.v2_calculation import calculate_v2_annual_changes
from finagent.finance.v2_screening import screen_v2_annual_candidates
from finagent.ingestion import extract_annual_financial_facts, parse_text_pdf
from finagent.reports.annual_report import build_annual_report, render_markdown
from finagent.schemas.financial_fact_v2 import adapt_legacy_financial_fact
from finagent.verification.independent_fact import verify_financial_fact

_ROOT = Path(__file__).resolve().parents[2]
_PDF = _ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "1222994233.PDF"
_EXPECTED = {
    ("revenue", 2024): 81,
    ("revenue", 2023): 81,
    ("net_profit_parent", 2024): 82,
    ("net_profit_parent", 2023): 82,
    ("operating_cash_flow", 2024): 86,
    ("operating_cash_flow", 2023): 86,
    ("non_recurring_total", 2024): 9,
    ("non_recurring_total", 2023): 9,
}


def test_haitian_pdf_to_v2_report_preserves_unknown_comparability() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    parsed = parse_text_pdf(_PDF, document_id="cninfo-1222994233")
    extracted = extract_annual_financial_facts(parsed, company_id="603288", report_year=2024)
    assert extracted.issues == ()
    assert len(extracted.facts) == len(_EXPECTED)

    facts = []
    evidences = []
    verifications = []
    verified_keys = set()
    for legacy_fact in extracted.facts:
        fact, extraction_evidence = adapt_legacy_financial_fact(legacy_fact, report_year=2024)
        assert fact.unit is None
        assert fact.restatement_status == "unknown"

        checked = verify_financial_fact(_PDF, fact)
        key = (fact.metric_id, fact.period_end.year if fact.period_end else None)
        assert checked.result.status == "verified", (key, checked.result.limitations)
        assert key in _EXPECTED
        assert key not in verified_keys
        verified_keys.add(key)
        independent_evidence = checked.evidence[0]
        assert independent_evidence.value_region is not None
        assert independent_evidence.value_region.page == _EXPECTED[key]

        facts.append(fact)
        evidences.extend(extraction_evidence)
        evidences.extend(checked.evidence)
        verifications.append(checked.result)

    assert verified_keys == set(_EXPECTED)
    calculations, issues = calculate_v2_annual_changes(facts, verifications, report_year=2024)
    assert len(calculations) == 8
    assert len(issues) == 8
    assert all(item.status == "failed" for item in calculations)
    assert all(
        item.failure_reason is not None and "追溯调整状态未知" in item.failure_reason
        for item in calculations
    )

    screening = screen_v2_annual_candidates(facts, calculations)
    assert len(screening) == 2
    assert all(item["status"] == "abstained" for item in screening)
    assert all("缺少已成功且可比的年度差额" in str(item["reason"]) for item in screening)
    assert all(item["left_difference"] is None and item["right_difference"] is None for item in screening)

    report = build_annual_report(
        run_id="haitian-v2-report-regression",
        company_id="603288",
        report_year=2024,
        source_document_id=parsed.document_id,
        source_sha256=parsed.source_sha256,
        facts=tuple(facts),
        evidences=tuple(evidences),
        verifications=tuple(verifications),
        calculations=calculations,
        claims=(),
        candidate_signals=screening,
    )
    assert len(report["confirmed"]["metrics"]) == 8
    assert {item["status"] for fact in report["confirmed"]["metrics"] for item in fact["verifications"]} == {
        "verified"
    }
    assert report["confirmed"]["analyses"] == []
    assert len(report["pending_review"]["calculations"]) == 8
    assert all(item["status"] == "abstained" for item in report["pending_review"]["candidate_signals"])

    markdown = render_markdown(report)
    assert "追溯调整状态未知，不能当作可比" in markdown
    assert "筛查原因：缺少已成功且可比的年度差额" in markdown
    assert "左侧差额：`未提供`" in markdown
    assert "右侧差额：`未提供`" in markdown
    assert "PDF 第81页" in markdown
    assert "坐标 bbox" in markdown
    assert "#pdf-page-" not in markdown
