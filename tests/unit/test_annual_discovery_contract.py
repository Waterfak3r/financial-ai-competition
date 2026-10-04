from __future__ import annotations

from finagent.api.annual_report_read import _validate_discovery_coverage
from finagent.retrieval.annual_discovery_context import empty_coverage


def test_text_page_with_embedded_image_can_be_complete_without_ocr_claim() -> None:
    coverage = empty_coverage()
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


def test_image_only_pdf_is_not_called_and_is_not_marked_truncated_or_complete() -> None:
    coverage = empty_coverage()
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
