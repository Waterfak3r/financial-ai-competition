"""资产负债表 v2 提取与独立核验的构造 PDF 错误注入。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from pathlib import Path

import pymupdf

from finagent.ingestion import extract_v2_balance_sheet_facts
from finagent.schemas.text_pdf import (
    COORDINATE_NOTE,
    COORDINATE_SYSTEM,
    COORDINATE_UNIT,
    ParsedTextPdf,
    PdfBBox,
    PdfPageText,
    PdfTextBlock,
)
from finagent.verification.independent_fact import verify_financial_fact

_VALUES = {
    "accounts_receivable_net": ("242,260,969.75", "223,149,082.18"),
    "inventory_net": ("2,525,273,760.73", "2,618,773,147.00"),
}


def _synthetic_pdf(
    path: Path,
    *,
    unit_line: str = "单位：元  币种：人民币",
    years=(2024, 2023),
    omit_inventory_current: bool = False,
) -> str:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "合并资产负债表", fontname="china-s")
    page.insert_text((72, 94), unit_line, fontname="china-s")
    page.insert_text((72, 116), "项目", fontname="china-s")
    page.insert_text((245, 116), f"{years[0]}年12月31日", fontname="china-s")
    page.insert_text((410, 116), f"{years[1]}年12月31日", fontname="china-s")
    rows = (
        ("应收账款", "七、5", *_VALUES["accounts_receivable_net"]),
        ("存货", "七、10", *_VALUES["inventory_net"]),
    )
    for index, (label, note, current, comparative) in enumerate(rows):
        y = 150 + index * 24
        page.insert_text((72, y), label, fontname="china-s")
        page.insert_text((152, y), note, fontname="china-s")
        if not (label == "存货" and omit_inventory_current):
            page.insert_text((245, y), current, fontname="china-s")
        page.insert_text((410, y), comparative, fontname="china-s")
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parsed_for_pdf(
    sha: str,
    filename: str = "synthetic.pdf",
    unit_line: str = "单位：元  币种：人民币",
    years=(2024, 2023),
    omit_inventory_current: bool = False,
) -> ParsedTextPdf:
    inventory = "存货\n七、10\n2,618,773,147.00" if omit_inventory_current else (
        "存货\n七、10\n2,525,273,760.73\n2,618,773,147.00"
    )
    blocks = (
        PdfTextBlock(0, "合并资产负债表", PdfBBox(72, 60, 180, 78)),
        PdfTextBlock(1, unit_line, PdfBBox(72, 82, 250, 100)),
        PdfTextBlock(
            2,
            f"项目\n{years[0]}年12月31日\n{years[1]}年12月31日",
            PdfBBox(72, 104, 520, 126),
        ),
        PdfTextBlock(
            3,
            "应收账款\n七、5\n242,260,969.75\n223,149,082.18",
            PdfBBox(72, 138, 520, 158),
        ),
        PdfTextBlock(4, inventory, PdfBBox(72, 162, 520, 182)),
    )
    return ParsedTextPdf(
        document_id="synthetic-603288-2024",
        source_sha256=sha,
        source_filename=filename,
        page_count=1,
        coordinate_system=COORDINATE_SYSTEM,
        coordinate_unit=COORDINATE_UNIT,
        coordinate_note=COORDINATE_NOTE,
        ocr_applied=False,
        extractor="constructed_test_fixture",
        extractor_version="1",
        pages=(
            PdfPageText(
                page_number=1,
                width=595,
                height=842,
                rotation=0,
                status="extracted",
                image_block_count=0,
                limitation=None,
                blocks=blocks,
            ),
        ),
    )


def test_constructed_balance_sheet_extracts_and_independently_verifies_four_instant_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic-balance-sheet.pdf"
    sha = _synthetic_pdf(path)
    extracted = extract_v2_balance_sheet_facts(
        _parsed_for_pdf(sha), company_id="603288", report_year=2024
    )

    assert extracted.issues == ()
    assert len(extracted.facts) == 4
    assert len(extracted.evidence) == 4
    for fact in extracted.facts:
        year = fact.period_end.year
        current, comparative = _VALUES[fact.metric_id]
        expected = current if year == 2024 else comparative
        assert fact.raw_value == expected
        assert fact.normalized_value == expected.replace(",", "")
        assert fact.period_type == "instant"
        assert fact.period_start is None
        assert fact.period_end == date(year, 12, 31)
        assert fact.comparison_role == ("current" if year == 2024 else "comparative")
        assert fact.scope == "consolidated"
        assert fact.currency == "人民币"
        assert fact.unit == "元"
        assert fact.restatement_status == "unknown"
        checked = verify_financial_fact(path, fact)
        assert checked.result.status == "verified", checked.result.limitations
        assert checked.evidence[0].value_region is not None
        assert checked.evidence[0].value_region.page == 1


def test_independent_balance_sheet_check_rejects_wrong_value_year_column_unit_and_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic-errors.pdf"
    sha = _synthetic_pdf(path)
    result = extract_v2_balance_sheet_facts(
        _parsed_for_pdf(sha), company_id="603288", report_year=2024
    )
    current = next(
        fact
        for fact in result.facts
        if fact.metric_id == "accounts_receivable_net" and fact.period_end.year == 2024
    )
    comparative = next(
        fact
        for fact in result.facts
        if fact.metric_id == "accounts_receivable_net" and fact.period_end.year == 2023
    )

    wrong_amount = replace(current, raw_value="9.00", normalized_value="9.00")
    wrong_year_column = replace(current, raw_value=comparative.raw_value, normalized_value=comparative.normalized_value)
    wrong_unit = replace(current, unit="万元", unit_multiplier="10000")
    wrong_scope = replace(current, scope="parent")
    wrong_role = replace(current, comparison_role="comparative")
    wrong_report_year = replace(current, report_year=2025, comparison_role="comparative")

    for fact in (wrong_amount, wrong_year_column, wrong_unit, wrong_scope, wrong_role, wrong_report_year):
        assert verify_financial_fact(path, fact).result.status == "conflict"


def test_missing_currency_or_annual_column_fails_closed(tmp_path: Path) -> None:
    no_currency = tmp_path / "no-currency.pdf"
    sha = _synthetic_pdf(no_currency, unit_line="单位：元")
    extracted = extract_v2_balance_sheet_facts(
        _parsed_for_pdf(sha, unit_line="单位：元"), company_id="603288", report_year=2024
    )
    current = next(
        fact
        for fact in extracted.facts
        if fact.metric_id == "inventory_net" and fact.period_end.year == 2024
    )
    assert current.currency == "未披露"
    assert verify_financial_fact(no_currency, current).result.status == "insufficient_evidence"

    wrong_years = tmp_path / "wrong-years.pdf"
    wrong_sha = _synthetic_pdf(wrong_years, years=(2022, 2021))
    missing = extract_v2_balance_sheet_facts(
        _parsed_for_pdf(wrong_sha, years=(2022, 2021)), company_id="603288", report_year=2024
    )
    assert missing.facts == ()
    assert all(item.code == "missing_year_header" for item in missing.issues)

    complete_path = tmp_path / "complete-year-source.pdf"
    complete_sha = _synthetic_pdf(complete_path)
    complete = extract_v2_balance_sheet_facts(
        _parsed_for_pdf(complete_sha), company_id="603288", report_year=2024
    )
    candidate = next(fact for fact in complete.facts if fact.metric_id == "inventory_net")
    candidate = replace(candidate, source_sha256=wrong_sha)
    assert verify_financial_fact(wrong_years, candidate).result.status == "insufficient_evidence"


def test_missing_amount_is_not_zero_and_independent_check_abstains(tmp_path: Path) -> None:
    path = tmp_path / "missing-current-amount.pdf"
    sha = _synthetic_pdf(path, omit_inventory_current=True)
    parsed = _parsed_for_pdf(sha, omit_inventory_current=True)
    extracted = extract_v2_balance_sheet_facts(parsed, company_id="603288", report_year=2024)
    assert not any(fact.metric_id == "inventory_net" for fact in extracted.facts)
    assert any(
        issue.metric_id == "inventory_net" and issue.code == "amount_alignment"
        for issue in extracted.issues
    )

    complete_path = tmp_path / "complete-source.pdf"
    complete_sha = _synthetic_pdf(complete_path)
    complete = extract_v2_balance_sheet_facts(
        _parsed_for_pdf(complete_sha), company_id="603288", report_year=2024
    )
    expected = next(
        fact
        for fact in complete.facts
        if fact.metric_id == "inventory_net" and fact.period_end.year == 2024
    )
    candidate = replace(expected, source_sha256=sha)
    assert verify_financial_fact(path, candidate).result.status == "insufficient_evidence"
