"""Bounded narrative retrieval against the archived Haitian 2024 golden path."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from finagent.retrieval.annual_context import retrieve_annual_context

_ROOT = Path(__file__).resolve().parents[2]
_PDF = _ROOT / "data/raw/603288/2024/cninfo-1222994233/1222994233.PDF"
_SOURCE_RECORD = _ROOT / "data/raw/603288/2024/cninfo-1222994233/source.json"
_REPORT = _ROOT / "artifacts/reports/annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42/report.json"
_ANALYSIS = _ROOT / "artifacts/runs/annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42/analysis.json"


def test_haitian_archived_report_retrieves_bound_mda_and_note_context() -> None:
    if not all(path.is_file() for path in (_PDF, _SOURCE_RECORD, _REPORT)):
        pytest.skip("本机缺少海天 2024 原文、来源记录或已归档 v2 报告。")
    report = json.loads(_REPORT.read_text(encoding="utf-8"))
    source_record = json.loads(_SOURCE_RECORD.read_text(encoding="utf-8"))

    result = retrieve_annual_context(report, _PDF, source_record)

    assert result.status == "retrieved", result.reason
    assert result.source_document_id == "cninfo-1222994233"
    assert result.source_sha256 == source_record["sha256"]
    assert result.sections_found == ("mda", "financial_notes")
    assert len(result.snippets) <= 4
    assert sum(len(item.raw_text) for item in result.snippets) <= 2600
    assert any(item.pdf_page in {15, 21} and item.section_label == "mda" for item in result.snippets)
    assert any(item.section_label == "financial_notes" for item in result.snippets)
    assert all(item.source_document_id == source_record["document_id"] for item in result.snippets)
    assert all(item.source_sha256 == source_record["sha256"] for item in result.snippets)
    assert all(item.pdf_page > 0 and all(math.isfinite(value) for value in item.bbox) for item in result.snippets)
    assert all(item.evidence_id and item.raw_text and item.selection_reason for item in result.snippets)
    assert {signal.signal_id for signal in result.signal_results if signal.status == "retrieved"} == {
        "profit_up_cash_down",
        "revenue_up_cash_down",
    }


def test_haitian_archived_analysis_json_screening_is_also_candidate_only() -> None:
    if not all(path.is_file() for path in (_PDF, _SOURCE_RECORD, _ANALYSIS)):
        pytest.skip("本机缺少海天 2024 原文、来源记录或已归档 analysis.json。")
    analysis = json.loads(_ANALYSIS.read_text(encoding="utf-8"))
    source_record = json.loads(_SOURCE_RECORD.read_text(encoding="utf-8"))

    result = retrieve_annual_context(analysis, _PDF, source_record)

    assert result.status == "retrieved", result.reason
    assert result.source_document_id == source_record["document_id"]
    assert result.source_sha256 == source_record["sha256"]
    assert any(item.pdf_page in {15, 21} and item.section_label == "mda" for item in result.snippets)
    assert all(
        signal.signal_id in {"profit_up_cash_down", "revenue_up_cash_down"}
        for signal in result.signal_results
    )
