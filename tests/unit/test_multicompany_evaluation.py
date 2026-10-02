from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_human_review_packet import (
    ROOT,
    _build_fact_item,
    _html_packet,
    provisional_review_locators,
    validate_result_registration,
)
from scripts.evaluate_multicompany import (
    EvaluationInputError,
    _run_registered_analysis,
    compute_fact_coverage,
    compute_injection_metrics,
    reserve_evaluation_directory,
    validate_source_registration,
    validate_source_registry,
)


def test_analysis_input_contains_source_identity_only_not_labels_or_injections(tmp_path: Path) -> None:
    source = {
        "company_id": "603288",
        "report_year": 2024,
        "document_id": "doc-603288",
        "case_labels": {"value": "known-label"},
        "injections": [{"family": "wrong_amount"}],
        "human_review": {"reviewer_name": "must-not-pass"},
    }
    observed: dict[str, object] = {}

    def analyzer(pdf_path: Path, **kwargs: object) -> str:
        observed["pdf_path"] = pdf_path
        observed.update(kwargs)
        return "analysis result"

    pdf = tmp_path / "source.pdf"
    assert _run_registered_analysis(source, pdf, analyzer, run_id="run-1") == "analysis result"
    assert observed == {
        "pdf_path": pdf,
        "run_id": "run-1",
        "company_id": "603288",
        "report_year": 2024,
        "document_id": "doc-603288",
    }


def test_provisional_unknowns_are_descriptive_and_excluded_from_golden_denominators() -> None:
    records = [
        {"verifier_status": "verified", "factual_review_status": "agent_provisional_pending_human"},
        {"verifier_status": "insufficient_evidence", "factual_review_status": "agent_provisional_pending_human"},
        {"verifier_status": "conflict", "factual_review_status": "agent_provisional_pending_human"},
    ]
    metrics = compute_fact_coverage(records)
    assert metrics["sample_denominator"] == 3
    assert metrics["verified_by_independent_checker"] == 1
    assert metrics["conflict"] == 1
    assert metrics["insufficient_evidence"] == 1
    assert metrics["unknown_or_unconfirmed"] == 1
    assert metrics["human_golden_accuracy_denominator"] == 0
    assert metrics["fraud_false_positive_denominator"] == 0
    assert metrics["fraud_false_negative_denominator"] == 0


def test_injection_abstention_uses_full_denominator_and_is_not_a_rejection() -> None:
    records = [
        {
            "company_id": "603288",
            "family": "wrong_amount",
            "verification_disposition": "rejected",
            "baseline_disposition": "accepted",
            "provisional_label_status": "agent_provisional_pending_human",
        },
        {
            "company_id": "600519",
            "family": "wrong_unit",
            "verification_disposition": "abstained",
            "baseline_disposition": "accepted",
            "provisional_label_status": "agent_provisional_pending_human",
        },
        {
            "company_id": "000858",
            "family": "unsupported_claim",
            "verification_disposition": "accepted",
            "baseline_disposition": "accepted",
            "provisional_label_status": "agent_provisional_pending_human",
        },
    ]
    metrics = compute_injection_metrics(records)
    assert metrics["injected_error_denominator"] == 3
    assert metrics["schema_only_baseline_accepted"] == {"numerator": 3, "denominator": 3}
    assert metrics["independent_verifier_error_accepted"] == {"numerator": 1, "denominator": 3}
    assert metrics["independent_verifier_rejected_as_conflict"] == {"numerator": 1, "denominator": 3}
    assert metrics["independent_verifier_abstained_as_insufficient_evidence"] == {
        "numerator": 1,
        "denominator": 3,
    }
    assert metrics["decision_coverage"] == {"numerator": 2, "denominator": 3}
    assert metrics["abstention_is_counted_as_rejection"] is False


def test_missing_or_conflicting_sources_fail_closed() -> None:
    source = {
        "company_id": "603288",
        "document_id": "doc-1",
        "source_url": "https://example.invalid/report.pdf",
        "source_path": "data/raw/report.pdf",
        "sha256": "a" * 64,
        "report_year": 2024,
    }
    with pytest.raises(EvaluationInputError, match="source.json 缺少字段"):
        validate_source_registration(source, {}, "a" * 64)

    conflicting_record = {
        "company_id": "603288",
        "document_id": "doc-1",
        "source_url": "https://example.invalid/report.pdf",
        "local_path": "data/raw/report.pdf",
        "sha256": "b" * 64,
    }
    with pytest.raises(EvaluationInputError, match="sha256 不一致"):
        validate_source_registration(source, conflicting_record, "a" * 64)

    with pytest.raises(EvaluationInputError, match="只能登记三家公司"):
        validate_source_registry([source])

    conflict_sources = [
        {**source, "company_id": "603288", "sha256": "a" * 64, "source_path": "data/raw/one.pdf"},
        {**source, "company_id": "600519", "sha256": "a" * 64, "document_id": "doc-2", "source_path": "data/raw/two.pdf"},
        {**source, "company_id": "000858", "sha256": "c" * 64, "document_id": "doc-3", "source_path": "data/raw/other.pdf"},
    ]
    with pytest.raises(EvaluationInputError, match="相同 SHA256"):
        validate_source_registry(conflict_sources)


def test_existing_evaluation_directory_is_never_overwritten(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts" / "evaluations"
    historical = artifacts / "historical-run"
    historical.mkdir(parents=True)
    marker = historical / "result.json"
    marker.write_text("historical evidence", encoding="utf-8")

    with pytest.raises(EvaluationInputError, match="拒绝覆盖"):
        reserve_evaluation_directory(artifacts, "historical-run")
    assert marker.read_text(encoding="utf-8") == "historical evidence"


def test_review_item_stays_pending_and_has_no_automatic_human_identity() -> None:
    source = {"company_name": "示例公司"}
    record = {
        "fact": {
            "report_year": 2024,
            "metric_id": "revenue",
            "label_raw": "营业收入",
            "raw_value": "100.00",
            "normalized_value": "100.00",
            "unit": None,
            "unit_multiplier": "1",
            "currency": "人民币",
            "scope": "consolidated",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "period_type": "duration",
            "comparison_role": "current",
            "restatement_status": "unknown",
            "source_document_id": "doc-1",
            "source_sha256": "a" * 64,
        },
        "independent_evidence": [],
        "verifier_status": "insufficient_evidence",
        "verifier_result": {"status": "insufficient_evidence"},
        "elapsed_ms": 1.0,
    }
    item = _build_fact_item("603288", source, record, {})
    assert item["factual_review_status"] == "agent_provisional_pending_human"
    assert item["human_review"]["status"] == "pending"
    assert item["human_review"]["conclusion"] is None
    assert item["human_review"]["reviewer_name"] is None
    assert item["human_review"]["review_date"] is None
    assert item["human_review"]["supporting_basis"] is None


def test_review_packet_rejects_archived_source_or_fact_identity_conflicts() -> None:
    source = {
        "company_id": "603288",
        "document_id": "doc-1",
        "sha256": "a" * 64,
        "report_year": 2024,
    }
    company_summary = {
        "source": {**source},
        "facts": [
            {
                "fact": {
                    "company_id": "603288",
                    "source_document_id": "doc-1",
                    "source_sha256": "a" * 64,
                    "report_year": 2024,
                }
            }
        ],
    }
    sources = {"603288": source}
    validate_result_registration({"603288": company_summary}, sources)

    company_summary["source"]["document_id"] = "other-document"
    with pytest.raises(EvaluationInputError, match="评测结果来源登记冲突"):
        validate_result_registration({"603288": company_summary}, sources)

    company_summary["source"]["document_id"] = "doc-1"
    company_summary["facts"][0]["fact"]["source_sha256"] = "b" * 64
    with pytest.raises(EvaluationInputError, match="评测事实来源登记冲突"):
        validate_result_registration({"603288": company_summary}, sources)


def test_provisional_candidate_locator_does_not_upgrade_insufficient_recheck() -> None:
    case_path = ROOT / "evaluation" / "cases" / "000858-2024-provisional.json"
    case = json.loads(case_path.read_text(encoding="utf-8"))
    fact = {
        "report_year": 2024,
        "metric_id": "revenue",
        "label_raw": "营业收入",
        "raw_value": "89,175,178,322.70",
        "normalized_value": "89175178322.70",
        "unit": None,
        "unit_multiplier": "1",
        "currency": "未披露",
        "scope": "consolidated",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "period_type": "duration",
        "comparison_role": "current",
        "restatement_status": "unknown",
        "source_document_id": "cninfo-1223311527",
        "source_sha256": "b737191a758994a35e9442d46847de771fe48b758feb9a7f35601def922435e7",
    }
    candidates = provisional_review_locators(case, fact)
    assert candidates
    assert all(item["candidate_status"] == "unverified_until_human_review" for item in candidates)
    record = {
        "fact": fact,
        "independent_evidence": [],
        "verifier_status": "insufficient_evidence",
        "verifier_result": {"status": "insufficient_evidence"},
        "elapsed_ms": 1.0,
    }
    item = _build_fact_item("000858", {"company_name": "五粮液"}, record, {}, candidates)
    assert item["independent_reverification"]["status"] == "insufficient_evidence"
    assert item["source_evidence"] == []
    assert item["provisional_reference_locators"]
    assert item["human_review"]["status"] == "pending"


def test_review_html_renders_url_disclosure_hash_and_use_boundary() -> None:
    packet = {
        "evaluation_id": "run-1",
        "sources": [
            {
                "company_id": "603288",
                "company_name": "示例公司",
                "source_url": "https://example.org/annual-report.pdf",
                "disclosure_date": "2025-04-03",
                "sha256": "a" * 64,
                "use_condition": "仅本地开发与评测；未确认再分发许可。",
                "provisional_disclosures": [],
            }
        ],
        "facts": [],
        "evaluation_companies": {"603288": {"comparability": {"status": "insufficient_evidence"}}},
        "page_images": {"603288": {}},
    }
    page = _html_packet(packet)
    assert 'href="https://example.org/annual-report.pdf"' in page
    assert "2025-04-03" in page
    assert "a" * 64 in page
    assert "仅本地开发与评测；未确认再分发许可。" in page
