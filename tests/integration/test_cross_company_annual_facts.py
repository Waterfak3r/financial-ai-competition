"""用三份真实年报验证四指标提取及独立事实核验的适用边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from finagent.api.annual_analysis import analyze_annual_pdf

_ROOT = Path(__file__).resolve().parents[2]
_CASES = (
    (
        "603288",
        "cninfo-1222994233",
        _ROOT / "data/raw/603288/2024/cninfo-1222994233/1222994233.PDF",
    ),
    (
        "600519",
        "cninfo-600519",
        _ROOT / "data/raw/600519/2024/moutai-2024-annual/8055b7bed7db41bdbc617f4c9b9ec591.pdf",
    ),
    (
        "000858",
        "cninfo-1223311527",
        _ROOT / "data/raw/000858/2024/cninfo-1223311527/1223311527.PDF",
    ),
)


def test_three_real_reports_confirm_supported_facts_and_abstain_on_missing_currency() -> None:
    if any(not path.is_file() for _, _, path in _CASES):
        pytest.skip("本机未同时提供海天、茅台和五粮液 2024 年报原始 PDF。")

    results = {
        company_id: analyze_annual_pdf(
            source_pdf,
            run_id=f"cross-company-{company_id}-2024",
            company_id=company_id,
            report_year=2024,
            document_id=document_id,
        )
        for company_id, document_id, source_pdf in _CASES
    }

    haitian = results["603288"]
    assert haitian.comparability.status == "verified"
    assert haitian.comparability.conflicts == ()
    assert haitian.extraction_issues == ()
    assert len(haitian.extracted_facts) == 8
    assert all(item.status == "verified" for item in haitian.fact_verifications)
    assert len(haitian.report["confirmed"]["metrics"]) == 8

    moutai = results["600519"]
    assert moutai.comparability.status == "insufficient_evidence"
    assert moutai.comparability.conflicts == ()
    assert moutai.extraction_issues == ()
    assert len(moutai.extracted_facts) == 8
    assert all(item.status == "verified" for item in moutai.fact_verifications)
    assert len(moutai.report["confirmed"]["metrics"]) == 8
    independent = {
        item.evidence_id: item
        for item in moutai.evidences
        if item.extraction_method == "independent_pymupdf_words"
    }
    non_recurring = [
        evidence
        for item in moutai.fact_verifications
        for evidence_id in item.evidence_ids
        if (evidence := independent[evidence_id]).table_title
        and "非经常性损益项目和金额" in evidence.table_title
    ]
    assert len(non_recurring) == 2

    wuliangye = results["000858"]
    assert wuliangye.comparability.status == "insufficient_evidence"
    assert wuliangye.comparability.conflicts == ()
    assert any("缺少报告年份抬头" in item for item in wuliangye.comparability.limitations)
    assert wuliangye.extraction_issues == ()
    assert len(wuliangye.extracted_facts) == 8
    assert {item.currency for item in wuliangye.facts} == {"未披露"}
    assert all(
        any("不得按人民币推断" in limitation for limitation in item.limitations)
        for item in wuliangye.extracted_facts
    )
    assert all(item.status == "insufficient_evidence" for item in wuliangye.fact_verifications)
    assert all(
        any("未明确币种" in limitation for limitation in item.limitations)
        for item in wuliangye.fact_verifications
    )
    assert wuliangye.report["confirmed"]["metrics"] == []
    assert wuliangye.report["confirmed"]["analyses"] == []
    assert all(item.status == "failed" for item in wuliangye.calculations)
    assert not any(item.get("status") == "candidate" for item in wuliangye.screening)
    assert all(item.get("status") == "abstained" for item in wuliangye.screening)
