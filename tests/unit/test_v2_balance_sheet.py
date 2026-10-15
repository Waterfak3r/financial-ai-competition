"""资产负债表 v2 提取与独立核验的构造 PDF 错误注入。"""

from __future__ import annotations

import hashlib
import re

from dataclasses import replace
from datetime import date
from pathlib import Path

import pymupdf

from finagent.ingestion import extract_v2_balance_sheet_facts, parse_text_pdf
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


def _extract_pdf(
    path: Path,
    *,
    report_year: int = 2024,
    company_id: str = "603288",
    document_id: str = "synthetic-603288-2024",
):
    parsed = parse_text_pdf(path, document_id=document_id)
    return extract_v2_balance_sheet_facts(parsed, company_id=company_id, report_year=report_year)


_AMOUNT_TOKEN_RE = re.compile(
    r"[（(]\s*[-－−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?\s*[）)]"
    r"|[（(]\s*[-－−]?\d+\.\d+\s*[）)]"
    r"|\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?"
    r"|\d+\.\d+"
)
_BODY_FONTNAME = "china-s"
_BODY_FONTSIZE = 7
_BODY_ROW_GAP = 9
_LABEL_X = 72
_CURRENT_COLUMN_X = 245
_COMPARATIVE_COLUMN_X = 410


def _split_label_amounts(line: str) -> tuple[str, tuple[str, ...]]:
    parts = line.split()
    amounts: list[str] = []
    while parts and _AMOUNT_TOKEN_RE.fullmatch(parts[-1]):
        amounts.append(parts.pop())
    amounts.reverse()
    return " ".join(parts), tuple(amounts)


def _insert_same_block_rows(
    page,
    body: str,
    *,
    origin_y: float,
    current_x: float,
    comparative_x: float,
) -> None:
    """Keep adjacent statement rows in one text block, with amounts under year columns."""
    y = origin_y
    last_y = origin_y
    pending: list[str] = []

    def flush(at_y: float) -> None:
        nonlocal pending
        if len(pending) >= 2:
            page.insert_text((current_x, at_y), pending[0], fontname=_BODY_FONTNAME, fontsize=_BODY_FONTSIZE)
            page.insert_text((comparative_x, at_y), pending[1], fontname=_BODY_FONTNAME, fontsize=_BODY_FONTSIZE)
            pending = pending[2:]
        if len(pending) == 1:
            page.insert_text((current_x, at_y), pending[0], fontname=_BODY_FONTNAME, fontsize=_BODY_FONTSIZE)
            pending = []

    for raw in body.splitlines():
        if raw.strip() == "":
            continue
        label, amounts = _split_label_amounts(raw)
        if label:
            flush(last_y)
            page.insert_text((_LABEL_X, y), label, fontname=_BODY_FONTNAME, fontsize=_BODY_FONTSIZE)
            last_y = y
            if len(amounts) >= 2:
                page.insert_text((current_x, y), amounts[0], fontname=_BODY_FONTNAME, fontsize=_BODY_FONTSIZE)
                page.insert_text((comparative_x, y), amounts[1], fontname=_BODY_FONTNAME, fontsize=_BODY_FONTSIZE)
            elif len(amounts) == 1:
                page.insert_text((current_x, y), amounts[0], fontname=_BODY_FONTNAME, fontsize=_BODY_FONTSIZE)
            y += _BODY_ROW_GAP
        else:
            pending.extend(amounts)
    flush(last_y)


def _balance_body_pdf(path: Path, body: str, *, years: tuple[int, int] = (2024, 2023)) -> str:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "合并资产负债表", fontname="china-s")
    page.insert_text((72, 94), "单位：元  币种：人民币", fontname="china-s")
    page.insert_text((72, 116), "项目", fontname="china-s")
    page.insert_text((_CURRENT_COLUMN_X, 116), f"{years[0]}年12月31日", fontname="china-s")
    page.insert_text((_COMPARATIVE_COLUMN_X, 116), f"{years[1]}年12月31日", fontname="china-s")
    _insert_same_block_rows(
        page,
        body,
        origin_y=140,
        current_x=_CURRENT_COLUMN_X,
        comparative_x=_COMPARATIVE_COLUMN_X,
    )
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_multiline_same_block_locates_receivable_and_inventory_rows(tmp_path: Path) -> None:
    path = tmp_path / "multiline-balance.pdf"
    source_sha256 = _balance_body_pdf(
        path,
        "衍生金融资产\n应收票据  4  1,984,407,967.50  1,000,000.00\n"
        "应收账款  5  242,260,969.75  223,149,082.18\n应收款项融资\n"
        "买入返售金融资产  9  8,879,708,146.97  7,220,310,691.10\n"
        "存货  七、10  2,525,273,760.73  2,618,773,147.00\n其中：数据资源",
        years=(2025, 2024),
    )
    parsed = parse_text_pdf(path, document_id="synthetic-600519-2025")
    assert any(
        len(block.text.splitlines()) > 1
        and all(token in block.text for token in (
            "应收账款", "存货", "242,260,969.75", "223,149,082.18",
            "2,525,273,760.73", "2,618,773,147.00",
        ))
        for block in parsed.pages[0].blocks
    ), "fixture must contain both target rows and their two-year amounts in one multiline block"
    extracted = _extract_pdf(
        path, report_year=2025, company_id="600519", document_id="synthetic-600519-2025"
    )
    assert extracted.issues == ()
    by_metric: dict[str, dict[int, object]] = {}
    for fact, evidence in zip(extracted.facts, extracted.evidence, strict=True):
        by_metric.setdefault(fact.metric_id, {})[fact.period_end.year] = fact
        assert fact.source_sha256 == source_sha256
        assert fact.raw_value in evidence.surrounding_text
        if fact.metric_id == "accounts_receivable_net":
            assert "应收账款" in evidence.surrounding_text
            assert "应收票据" not in evidence.surrounding_text
            assert "应收款项融资" not in evidence.surrounding_text
        if fact.metric_id == "inventory_net":
            assert "存货" in evidence.surrounding_text
            assert "买入返售" not in evidence.surrounding_text
            assert "数据资源" not in evidence.surrounding_text
        checked = verify_financial_fact(path, fact)
        assert checked.result.status == "verified", (fact.metric_id, fact.period_end.year, checked.result.limitations)
    assert by_metric["accounts_receivable_net"][2025].raw_value == _VALUES["accounts_receivable_net"][0]
    assert by_metric["accounts_receivable_net"][2024].raw_value == _VALUES["accounts_receivable_net"][1]
    assert by_metric["inventory_net"][2025].raw_value == _VALUES["inventory_net"][0]
    assert by_metric["inventory_net"][2024].raw_value == _VALUES["inventory_net"][1]
    assert by_metric["accounts_receivable_net"][2025].currency == "人民币"
    assert by_metric["inventory_net"][2025].company_id == "600519"


def test_same_block_scanner_rejects_duplicate_and_does_not_steal_inventory(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate-receivable.pdf"
    _balance_body_pdf(
        duplicate,
        "应收账款  5  242,260,969.75  223,149,082.18\n"
        "应收账款  5  1.00  2.00\n"
        "存货  七、10  2,525,273,760.73  2,618,773,147.00",
    )
    duplicated = _extract_pdf(duplicate)
    assert not any(fact.metric_id == "accounts_receivable_net" for fact in duplicated.facts)
    assert any(
        issue.metric_id == "accounts_receivable_net" and issue.code == "ambiguous_row"
        for issue in duplicated.issues
    )
    inventory = {
        fact.period_end.year: fact
        for fact in duplicated.facts
        if fact.metric_id == "inventory_net"
    }
    assert inventory[2024].raw_value == _VALUES["inventory_net"][0]
    assert inventory[2023].raw_value == _VALUES["inventory_net"][1]

    stolen = tmp_path / "missing-receivable.pdf"
    _balance_body_pdf(
        stolen,
        "应收账款  5\n存货  七、10  2,525,273,760.73  2,618,773,147.00",
    )
    missing = _extract_pdf(stolen)
    assert not any(fact.metric_id == "accounts_receivable_net" for fact in missing.facts)
    assert any(
        issue.metric_id == "accounts_receivable_net" and issue.code in {"row_not_found", "amount_alignment"}
        for issue in missing.issues
    )
    inventory = {
        fact.period_end.year: fact
        for fact in missing.facts
        if fact.metric_id == "inventory_net"
    }
    assert inventory[2024].raw_value == _VALUES["inventory_net"][0]
    assert inventory[2023].raw_value == _VALUES["inventory_net"][1]

    spaced = tmp_path / "note-blank-inventory.pdf"
    _balance_body_pdf(
        spaced,
        "存货\n七、10\n\n2,525,273,760.73\n2,618,773,147.00",
    )
    kept = _extract_pdf(spaced)
    inventory = {
        fact.period_end.year: fact
        for fact in kept.facts
        if fact.metric_id == "inventory_net"
    }
    assert set(inventory) == {2024, 2023}
    assert inventory[2024].raw_value == _VALUES["inventory_net"][0]
    assert inventory[2023].raw_value == _VALUES["inventory_net"][1]
