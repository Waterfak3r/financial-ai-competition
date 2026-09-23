"""年度同比计算，以及万元规范值换算为元。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finagent.finance.annual_change import calculate_annual_changes
from finagent.ingestion.extract_annual_facts import extract_annual_financial_facts
from finagent.schemas.financial_fact import (
    PERIOD_TYPE_ANNUAL,
    RESTATEMENT_STATUS_UNKNOWN,
    ExtractionResult,
    FactHit,
    FinancialFact,
)
from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox, PdfPageText, PdfTextBlock


def _hit() -> FactHit:
    return FactHit(page_number=1, block_index=0, text="行", x0=0, y0=0, x1=1, y1=1)


def _fact(indicator: str, year: int, value: str, **overrides) -> FinancialFact:
    payload = {
        "document_id": "doc-1",
        "source_sha256": "abc",
        "company_id": "603288",
        "indicator_name": indicator,
        "table_name": "合并利润表",
        "raw_value": value,
        "normalized_value": value,
        "report_year": year,
        "period_label": f"{year}年度",
        "period_type": PERIOD_TYPE_ANNUAL,
        "column_role": "current" if year == 2024 else "comparative",
        "restatement_status": RESTATEMENT_STATUS_UNKNOWN,
        "unit_multiplier": "1",
        "currency": "人民币",
        "statement_scope": "合并",
        "extraction_method": "test",
        "hits": (_hit(),),
    }
    payload.update(overrides)
    return FinancialFact(**payload)


def test_difference_and_rate_use_exact_decimal_strings() -> None:
    result = calculate_annual_changes(
        [_fact("营业收入", 2024, "150.00"), _fact("营业收入", 2023, "100.00")],
        2024,
    )
    assert result.issues == ()
    change = result.changes[0]
    assert change.difference == "50.00"
    assert change.source_sha256 == "abc"
    assert Decimal(change.rate) == Decimal("0.5")
    assert change.current_value == "150.00"
    assert change.prior_value == "100.00"
    assert "上期规范值" in change.formula


def test_negative_prior_still_calculates() -> None:
    result = calculate_annual_changes(
        [_fact("营业收入", 2024, "-50.00"), _fact("营业收入", 2023, "-100.00")],
        2024,
    )
    change = result.changes[0]
    assert change.difference == "50.00"
    assert Decimal(change.rate) == Decimal("-0.5")


def test_accepts_extraction_result() -> None:
    source = ExtractionResult(
        facts=(
            _fact("营业收入", 2024, "110"),
            _fact("营业收入", 2023, "100"),
        )
    )
    result = calculate_annual_changes(source, 2024)
    assert result.issues == ()
    assert Decimal(result.changes[0].difference) == Decimal("10")


@pytest.mark.parametrize(
    ("field", "current", "prior"),
    [
        ("document_id", "doc-a", "doc-b"),
        ("source_sha256", "hash-a", "hash-b"),
        ("company_id", "1", "2"),
        ("currency", "人民币", "美元"),
        ("unit_multiplier", "1", "10000"),
        ("statement_scope", "合并", "披露表格口径"),
        ("period_type", PERIOD_TYPE_ANNUAL, "quarter"),
    ],
)
def test_incompatible_pair_returns_issue_without_number(field: str, current: str, prior: str) -> None:
    facts = [
        _fact("营业收入", 2024, "150", **{field: current}),
        _fact("营业收入", 2023, "100", **{field: prior}),
    ]
    result = calculate_annual_changes(facts, 2024)
    assert result.changes == ()
    assert result.issues[0].code == "incompatible_facts"
    assert field in result.issues[0].message


def test_zero_prior_returns_issue() -> None:
    result = calculate_annual_changes(
        [_fact("营业收入", 2024, "10"), _fact("营业收入", 2023, "0")],
        2024,
    )
    assert result.changes == ()
    assert result.issues[0].code == "zero_prior"


def test_missing_and_duplicate_return_issues() -> None:
    missing = calculate_annual_changes([_fact("营业收入", 2024, "10")], 2024)
    assert missing.changes == ()
    assert missing.issues[0].code == "missing_prior"
    duplicate = calculate_annual_changes(
        [
            _fact("营业收入", 2024, "10"),
            _fact("营业收入", 2024, "11"),
            _fact("营业收入", 2023, "9"),
        ],
        2024,
    )
    assert duplicate.changes == ()
    assert duplicate.issues[0].code == "duplicate_fact"


def test_non_annual_period_type_is_not_calculated() -> None:
    facts = [
        _fact("营业收入", 2024, "150", period_type="quarter"),
        _fact("营业收入", 2023, "100", period_type="quarter"),
    ]
    result = calculate_annual_changes(facts, 2024)
    assert result.changes == ()
    assert result.issues[0].code == "period_type_not_annual"


def test_wan_yuan_normalized_value_is_converted_to_yuan() -> None:
    page = PdfPageText(
        page_number=1,
        width=300,
        height=200,
        rotation=0,
        status="extracted",
        image_block_count=0,
        limitation=None,
        blocks=(
            PdfTextBlock(0, "合并利润表", PdfBBox(0, 0, 40, 10)),
            PdfTextBlock(1, "2024 年1—12 月", PdfBBox(0, 12, 80, 22)),
            PdfTextBlock(2, "单位：万元  币种：人民币", PdfBBox(0, 24, 120, 34)),
            PdfTextBlock(3, "项目 附注 2024 年度 2023 年度", PdfBBox(0, 36, 160, 46)),
            PdfTextBlock(4, "其中：营业收入\n1,234.50 1,000.00", PdfBBox(0, 48, 180, 70)),
        ),
    )
    parsed = ParsedTextPdf(
        document_id="doc-wan",
        source_sha256="hash",
        source_filename="sample.pdf",
        page_count=1,
        coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point",
        coordinate_note="test",
        ocr_applied=False,
        extractor="test",
        extractor_version="0",
        pages=(page,),
    )
    extracted = extract_annual_financial_facts(parsed, "603288", 2024)
    revenue = [fact for fact in extracted.facts if fact.indicator_name == "营业收入"]
    by_year = {fact.report_year: fact for fact in revenue}
    assert by_year[2024].raw_value.replace(" ", "") == "1,234.50"
    assert by_year[2024].unit_multiplier == "10000"
    assert by_year[2024].period_type == PERIOD_TYPE_ANNUAL
    assert Decimal(by_year[2024].normalized_value) == Decimal("12345000.00")
    assert Decimal(by_year[2023].normalized_value) == Decimal("10000000.00")
    change = calculate_annual_changes(revenue, 2024).changes[0]
    assert Decimal(change.difference) == Decimal("2345000.00")
    assert Decimal(change.rate) == Decimal("0.2345")
