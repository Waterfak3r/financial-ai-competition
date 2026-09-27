"""Deterministic and provenance-bound annual narrative retrieval tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from finagent.retrieval.annual_context import retrieve_annual_context


DOC_ID = "test-doc-2024"
COMPANY_ID = "603288"


def _fixture(
    tmp_path: Path,
    *,
    include_mda: bool = True,
    include_notes: bool = True,
    relevant: bool = True,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    pdf_path = tmp_path / "source.pdf"
    document = pymupdf.open()
    page = document.new_page()
    lines = []
    if include_mda:
        mda_content = (
            [
                "报告期营业收入同比增长，归属于上市公司股东的净利润增加。",
                "经营活动产生的现金流量净额下降；经营活动现金流入与现金流出有变动。",
                "另一段经营活动现金流明细用于检查结果数量和排序。",
            ]
            if relevant
            else ["报告期治理架构与制度运行情况符合披露要求。"]
        )
        lines.extend(["第三节 管理层讨论与分析", *mda_content, "第四节 公司治理"])
    else:
        lines.extend(["第四节 公司治理"])
    for index, line in enumerate(lines):
        page.insert_text((50, 70 + index * 28), line, fontsize=10, fontname="china-s")

    if include_notes:
        note_page = document.new_page()
        note_lines = [
            "第十节 财务报告",
            "七、合并财务报表项目注释",
            (
                "将净利润调节为经营活动现金流量，经营性应收项目变动。"
                if relevant
                else "本附注只说明会计政策和估计方法的格式。"
            ),
        ]
        for index, line in enumerate(note_lines):
            note_page.insert_text((50, 70 + index * 30), line, fontsize=10, fontname="china-s")
    elif include_mda:
        note_page = document.new_page()
        for index, line in enumerate(("第十节 财务报告", "审计报告及报表")):
            note_page.insert_text((50, 70 + index * 30), line, fontsize=10, fontname="china-s")
    document.save(pdf_path)
    document.close()

    sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    report = {
        "kind": "fintrace_annual_analysis_report",
        "company_id": COMPANY_ID,
        "report_year": 2024,
        "source_document_id": DOC_ID,
        "source_sha256": sha256,
        "pending_review": {
            "candidate_signals": [
                {"signal_id": "profit_up_cash_down", "status": "candidate"},
                {"signal_id": "revenue_up_cash_down", "status": "candidate"},
            ]
        },
    }
    source_record = {
        "document_id": DOC_ID,
        "company_id": COMPANY_ID,
        "report_period": "2024-12-31",
        "sha256": sha256,
    }
    return pdf_path, report, source_record


def test_accepts_only_supported_candidate_v2_signals(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    report["pending_review"] = {
        "candidate_signals": [
            {"signal_id": "profit_up_cash_down", "status": "verified"},
            {"signal_id": "other_signal", "status": "candidate"},
            {"signal_id": ["profit_up_cash_down"], "status": "candidate"},
            {"signal_id": {"value": "revenue_up_cash_down"}, "status": "candidate"},
            {"signal_id": "revenue_up_cash_down", "status": "abstained"},
        ]
    }

    result = retrieve_annual_context(report, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "no_candidate_signal"
    assert result.snippets == ()


def test_retrieval_is_bounded_provenance_rich_and_deterministic(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)

    first = retrieve_annual_context(
        report,
        pdf_path,
        source_record,
        max_snippets=3,
        max_chars_per_snippet=200,
        max_total_chars=420,
    )
    second = retrieve_annual_context(
        report,
        pdf_path,
        source_record,
        max_snippets=3,
        max_chars_per_snippet=200,
        max_total_chars=420,
    )

    assert first == second
    assert first.status == "retrieved"
    assert first.sections_found == ("mda", "financial_notes")
    assert len(first.snippets) <= 3
    assert sum(len(item.raw_text) for item in first.snippets) <= 420
    assert all(len(item.raw_text) <= 200 for item in first.snippets)
    assert all(item.source_document_id == DOC_ID for item in first.snippets)
    assert all(item.source_sha256 == source_record["sha256"] for item in first.snippets)
    assert all(item.pdf_page > 0 and item.bbox[0] < item.bbox[2] for item in first.snippets)
    assert all(item.evidence_id.startswith("nctx-") for item in first.snippets)
    assert {signal.signal_id for signal in first.signal_results if signal.status == "retrieved"} == {
        "profit_up_cash_down",
        "revenue_up_cash_down",
    }
    assert all("不表示因果解释" in item.selection_reason for item in first.snippets)


def test_hash_mismatch_abstains_before_retrieval(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    wrong_sha = "0" * 64
    report["source_sha256"] = wrong_sha
    source_record["sha256"] = wrong_sha

    result = retrieve_annual_context(report, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "source_pdf_hash_mismatch"
    assert result.snippets == ()


def test_source_document_identity_mismatch_abstains(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    source_record["document_id"] = "another-document"

    result = retrieve_annual_context(report, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "source_document_id_mismatch"
    assert result.snippets == ()


def test_financial_report_heading_does_not_stand_in_for_notes_heading(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path, include_notes=False)

    result = retrieve_annual_context(report, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "required_section_heading_missing:financial_notes"
    assert result.sections_found == ("mda",)
    assert result.snippets == ()


def test_no_relevant_blocks_returns_explicit_abstention(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path, relevant=False)
    report["pending_review"] = {
        "candidate_signals": [{"signal_id": "profit_up_cash_down", "status": "candidate"}]
    }

    result = retrieve_annual_context(report, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "no_relevant_snippets_within_per_snippet_limit"
    assert result.sections_found == ("mda", "financial_notes")


def test_accepts_archived_analysis_json_candidate_shape(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    analysis = {
        "run_id": "test-run",
        "status": "completed",
        "company_id": COMPANY_ID,
        "report_year": 2024,
        "screening": report["pending_review"]["candidate_signals"],
        "extraction": {
            "facts": [
                {
                    "document_id": DOC_ID,
                    "source_sha256": source_record["sha256"],
                    "company_id": COMPANY_ID,
                }
            ]
        },
    }

    result = retrieve_annual_context(analysis, pdf_path, source_record)

    assert result.status == "retrieved"
    assert result.source_document_id == DOC_ID
    assert result.source_sha256 == source_record["sha256"]


def test_analysis_json_abstains_when_any_fact_has_a_second_source(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    analysis = _analysis_from(report, source_record)
    analysis["extraction"]["facts"].append(
        {
            "document_id": "another-document",
            "source_sha256": "cd" * 32,
            "company_id": COMPANY_ID,
        }
    )

    result = retrieve_annual_context(analysis, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "analysis_facts_source_identity_mismatch"


def test_analysis_json_abstains_when_any_fact_source_identity_is_invalid(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    analysis = _analysis_from(report, source_record)
    analysis["extraction"]["facts"].append(
        {"document_id": DOC_ID, "source_sha256": "not-a-sha256", "company_id": COMPANY_ID}
    )

    result = retrieve_annual_context(analysis, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "analysis_fact_source_identity_missing_or_invalid"


def test_analysis_json_abstains_when_fact_company_differs_from_report(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    analysis = _analysis_from(report, source_record)
    analysis["extraction"]["facts"][0]["company_id"] = "another-company"

    result = retrieve_annual_context(analysis, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == "analysis_fact_company_id_mismatch"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("company_id", None, "source_record_company_id_missing_or_invalid"),
        ("company_id", "another-company", "source_company_id_mismatch"),
        ("report_period", None, "source_record_report_period_missing_or_invalid"),
        ("report_period", "not-a-date", "source_record_report_period_missing_or_invalid"),
        ("report_period", "2023-12-31", "source_report_period_mismatch"),
    ],
)
def test_source_record_company_and_period_are_required_and_match(
    tmp_path: Path,
    field: str,
    value: str | None,
    reason: str,
) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    if value is None:
        source_record.pop(field)
    else:
        source_record[field] = value

    result = retrieve_annual_context(report, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("company_id", "", "report_company_id_missing_or_invalid"),
        ("report_year", None, "report_year_missing_or_invalid"),
        ("report_year", True, "report_year_missing_or_invalid"),
    ],
)
def test_report_company_and_year_are_required_and_valid(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)
    report[field] = value

    result = retrieve_annual_context(report, pdf_path, source_record)

    assert result.status == "abstained"
    assert result.reason == reason


def test_caller_cannot_raise_the_context_hard_limit(tmp_path: Path) -> None:
    pdf_path, report, source_record = _fixture(tmp_path)

    with pytest.raises(ValueError, match="硬上限"):
        retrieve_annual_context(report, pdf_path, source_record, max_total_chars=4001)


def _analysis_from(report: dict[str, Any], source_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": "test-run",
        "status": "completed",
        "company_id": COMPANY_ID,
        "report_year": 2024,
        "screening": report["pending_review"]["candidate_signals"],
        "extraction": {
            "facts": [
                {
                    "document_id": DOC_ID,
                    "source_sha256": source_record["sha256"],
                    "company_id": COMPANY_ID,
                }
            ]
        },
    }
