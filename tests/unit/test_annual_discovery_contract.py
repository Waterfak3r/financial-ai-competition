from __future__ import annotations

import copy

import pytest

from finagent.agents import annual_discovery
from finagent.api.annual_report_export import _V2_SAFE_BATCH_FAILURES as EXPORT_SAFE_BATCH_FAILURES
from finagent.api.annual_report_read import (
    AnnualReportReadError,
    _V2_SAFE_BATCH_FAILURES as READ_SAFE_BATCH_FAILURES,
    _validate_discovery_coverage,
)
from finagent.retrieval.annual_discovery_context import empty_coverage


def test_text_page_with_embedded_image_can_be_complete_without_ocr_claim() -> None:
    coverage = empty_coverage()
    coverage.pop("partial_batches")
    coverage.update(
        total_pages=1,
        text_pages=1,
        total_blocks=1,
        total_chars=24,
        planned_batches=1,
        processed_batches=1,
        successful_batches=1,
        processed_chars=24,
        attempted_chars=24,
        planned_chars=24,
        image_pages=[1],
        batch_results=[{
            "batch_index": 1,
            "status": "succeeded",
            "reason": None,
            "pages": [1],
            "raw_chars": 24,
        }],
        source_status="parsed",
        content_status="text_available",
        text_only_scope="extractable text only",
        complete=True,
    )

    _validate_discovery_coverage(coverage, "completed")


def _v2_partial_coverage() -> dict[str, object]:
    coverage = empty_coverage()
    coverage.update(
        total_pages=1,
        text_pages=1,
        total_blocks=1,
        total_chars=24,
        planned_batches=1,
        processed_batches=1,
        successful_batches=0,
        partial_batches=1,
        failed_batches=0,
        processed_chars=0,
        attempted_chars=24,
        planned_chars=24,
        unread_pages=[1],
        batch_results=[{
            "batch_index": 1,
            "status": "partial",
            "reason": "some_findings_rejected",
            "pages": [1],
            "raw_chars": 24,
            "accepted_findings": 1,
            "rejected_findings": 1,
            "restored_quotes": 1,
        }],
        source_status="parsed",
        content_status="text_available",
        text_only_scope="extractable text only",
        complete=False,
    )
    return coverage


def test_v2_partial_batches_are_explicit_and_never_count_as_complete() -> None:
    coverage = _v2_partial_coverage()
    _validate_discovery_coverage(coverage, "partial", "annual_discovery_v2")

    falsely_complete = copy.deepcopy(coverage)
    falsely_complete["complete"] = True
    with pytest.raises(AnnualReportReadError) as exc_info:
        _validate_discovery_coverage(falsely_complete, "partial", "annual_discovery_v2")
    assert exc_info.value.code == "annual_discovery_invalid"


def test_v2_partial_findings_can_exist_with_zero_fully_successful_batches() -> None:
    _validate_discovery_coverage(_v2_partial_coverage(), "partial", "annual_discovery_v2")


def test_v2_batch_failure_code_whitelists_match_engine_and_export() -> None:
    assert annual_discovery._SAFE_BATCH_FAILURES == READ_SAFE_BATCH_FAILURES
    assert annual_discovery._SAFE_BATCH_FAILURES == EXPORT_SAFE_BATCH_FAILURES


def test_v2_rejects_batch_finding_counts_over_the_six_item_cap() -> None:
    coverage = _v2_partial_coverage()
    coverage["batch_results"][0]["accepted_findings"] = 4
    coverage["batch_results"][0]["rejected_findings"] = 3
    with pytest.raises(AnnualReportReadError) as exc_info:
        _validate_discovery_coverage(coverage, "partial", "annual_discovery_v2")
    assert exc_info.value.code == "annual_discovery_invalid"


def test_image_only_pdf_is_not_called_and_is_not_marked_truncated_or_complete() -> None:
    coverage = empty_coverage()
    coverage.pop("partial_batches")
    coverage.update(
        total_pages=1,
        no_text_pages=[1],
        image_pages=[1],
        unread_image_pages=[1],
        unread_pages=[1],
        source_status="parsed",
        content_status="no_extractable_text",
        text_only_scope="extractable text only",
    )

    _validate_discovery_coverage(coverage, "not_called")
