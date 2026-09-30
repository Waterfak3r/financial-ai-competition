"""扣非归母净利润主要会计数据表的提取和独立核验边界。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from pathlib import Path

import pymupdf

from finagent.ingestion import (
    extract_v2_key_financial_data_facts,
    parse_text_pdf,
)
from finagent.verification.independent_fact import verify_financial_fact

_VALUES = {
    2024: "6,069,416,506.67",
    2023: "5,394,663,934.17",
    2022: "5,965,755,875.29",
}
_PERCENT = "12.51"


def _synthetic_pdf(
    path: Path,
    *,
    years: tuple[int, int, int] = (2024, 2023, 2022),
    unit_line: str = "单位：元  币种：人民币",
    values: tuple[str, str, str, str] | None = None,
    omit_year: int | None = None,
    omit_percentage_header: bool = False,
    omit_row: bool = False,
) -> str:
    year_values = values or (_VALUES[2024], _VALUES[2023], _PERCENT, _VALUES[2022])
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "七、近三年主要会计数据和财务指标", fontname="china-s", fontsize=8)
    page.insert_text((72, 102), "（一）主要会计数据", fontname="china-s", fontsize=8)
    page.insert_text((390, 126), unit_line, fontname="china-s", fontsize=8)
    page.insert_text((66, 153), "主要会计数据", fontname="china-s", fontsize=8)
    page.insert_text((199, 153), f"{years[0]}年", fontname="china-s", fontsize=8)
    page.insert_text((301, 153), f"{years[1]}年", fontname="china-s", fontsize=8)
    if not omit_percentage_header:
        page.insert_text((374, 144), "本期比上年同期", fontname="china-s", fontsize=7)
        page.insert_text((386, 162), "增减（%）", fontname="china-s", fontsize=7)
    page.insert_text((487, 153), f"{years[2]}年", fontname="china-s", fontsize=8)
    if not omit_row:
        page.insert_text((40, 190), "归属于上市公司股东的扣", fontname="china-s", fontsize=8)
        page.insert_text((40, 208), "除非经常性损益的净利润", fontname="china-s", fontsize=8)
        positions = (178, 278, 422, 464)
        for index, (x, value) in enumerate(zip(positions, year_values, strict=True)):
            if omit_year is not None and index in (0, 1, 3) and years[(0, 1, 3).index(index)] == omit_year:
                continue
            page.insert_text((x, 199), value, fontname="china-s", fontsize=7)
    page.insert_text((72, 245), "（二）主要财务指标", fontname="china-s", fontsize=8)
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract(path: Path, *, report_year: int = 2024):
    parsed = parse_text_pdf(path, document_id="synthetic-603288-2024")
    result = extract_v2_key_financial_data_facts(
        parsed,
        source_pdf_path=path,
        company_id="603288",
        report_year=report_year,
    )
    return parsed, result


def test_constructed_key_financial_data_aligns_amounts_to_year_headers_and_verifies(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-key-data.pdf"
    source_sha256 = _synthetic_pdf(path)
    _parsed, extracted = _extract(path)

    assert extracted.issues == ()
    assert len(extracted.facts) == 2
    assert len(extracted.evidence) == 2
    expected = {
        2024: _VALUES[2024],
        2023: _VALUES[2023],
    }
    for fact, extract_evidence in zip(extracted.facts, extracted.evidence, strict=True):
        year = fact.period_end.year
        assert fact.metric_id == "net_profit_parent_ex_nonrecurring"
        assert fact.label_raw == "归属于上市公司股东的扣除非经常性损益的净利润"
        assert fact.raw_value == expected[year]
        assert fact.normalized_value == expected[year].replace(",", "")
        assert fact.source_sha256 == source_sha256
        assert fact.period_start == date(year, 1, 1)
        assert fact.period_end == date(year, 12, 31)
        assert fact.period_type == "duration"
        assert fact.frequency == "annual"
        assert fact.comparison_role == ("current" if year == 2024 else "comparative")
        assert fact.statement_type == "key_financial_data"
        assert fact.scope == "unknown"
        assert fact.unit == "元"
        assert fact.unit_multiplier == "1"
        assert fact.currency == "人民币"
        assert fact.restatement_status == "unknown"
        assert extract_evidence.table_title == "主要会计数据"
        assert extract_evidence.column_label == f"{year}年"
        assert extract_evidence.value_raw == expected[year]
        assert extract_evidence.pdf_page == 1
        assert extract_evidence.value_region is not None
        assert extract_evidence.row_region is not None
        assert "归属于上市公司股东的扣" in extract_evidence.row_region.text
        assert extract_evidence.column_region is not None
        assert f"{year}年" in extract_evidence.column_region.text
        assert extract_evidence.unit_region is not None
        assert "人民币" in extract_evidence.unit_region.text
        assert extract_evidence.title_region is not None
        assert "主要会计数据" in extract_evidence.title_region.text

        checked = verify_financial_fact(path, fact)
        assert checked.result.status == "verified", checked.result.limitations
        assert checked.evidence[0].evidence_id not in fact.evidence_ids
        evidence = checked.evidence[0]
        assert evidence.table_title == "主要会计数据"
        assert evidence.row_label == fact.label_raw
        assert evidence.column_label is not None and f"{year}年" in evidence.column_label
        assert evidence.value_raw == expected[year]
        assert evidence.value_normalized == expected[year].replace(",", "")
        assert evidence.unit == "元"
        assert evidence.currency == "人民币"
        assert evidence.period_type == "duration"
        assert evidence.period_start == date(year, 1, 1)
        assert evidence.period_end == date(year, 12, 31)
        assert evidence.pdf_page == 1
        assert "scope 保持 unknown" in checked.result.limitations[1]
        for region in (
            evidence.value_region,
            evidence.row_region,
            evidence.column_region,
            evidence.title_region,
            evidence.unit_region,
        ):
            assert region is not None
            assert region.page == 1
            assert region.bbox.x1 > region.bbox.x0
            assert region.bbox.y1 > region.bbox.y0


def test_real_key_data_pdf_rejects_wrong_value_year_column_unit_currency_and_scope(tmp_path: Path) -> None:
    good_path = tmp_path / "good.pdf"
    _synthetic_pdf(good_path)
    _parsed, extracted = _extract(good_path)
    current = next(fact for fact in extracted.facts if fact.period_end.year == 2024)
    comparative = next(fact for fact in extracted.facts if fact.period_end.year == 2023)

    assert verify_financial_fact(
        good_path,
        replace(current, raw_value="9,999,999.00", normalized_value="9999999"),
    ).result.status == "conflict"
    # A historical 2022 amount and the 12.51% growth figure cannot pass as the 2023 value.
    for mistaken in (_VALUES[2022], _PERCENT):
        assert verify_financial_fact(
            good_path,
            replace(comparative, raw_value=mistaken, normalized_value=mistaken.replace(",", "")),
        ).result.status == "conflict"

    wrong_unit_path = tmp_path / "wrong-unit.pdf"
    wrong_unit_hash = _synthetic_pdf(wrong_unit_path, unit_line="单位：万元  币种：人民币")
    assert verify_financial_fact(
        wrong_unit_path,
        replace(current, source_sha256=wrong_unit_hash),
    ).result.status == "conflict"

    wrong_currency_path = tmp_path / "wrong-currency.pdf"
    wrong_currency_hash = _synthetic_pdf(wrong_currency_path, unit_line="单位：元  币种：美元")
    assert verify_financial_fact(
        wrong_currency_path,
        replace(current, source_sha256=wrong_currency_hash),
    ).result.status == "conflict"

    assert verify_financial_fact(good_path, replace(current, scope="consolidated")).result.status == "conflict"
    assert verify_financial_fact(good_path, replace(current, statement_type="income_statement")).result.status == "conflict"


def test_year_header_order_missing_comparative_amount_and_source_hash_fail_closed(tmp_path: Path) -> None:
    wrong_year_path = tmp_path / "wrong-years.pdf"
    _synthetic_pdf(wrong_year_path, years=(2024, 2022, 2023))
    _parsed_wrong_year, wrong_year_extraction = _extract(wrong_year_path)
    assert wrong_year_extraction.facts == ()
    assert wrong_year_extraction.issues[0].code == "year_header_not_found"

    missing_value_path = tmp_path / "missing-comparative.pdf"
    _synthetic_pdf(missing_value_path, omit_year=2023)
    _parsed_missing, missing_extraction = _extract(missing_value_path)
    assert missing_extraction.facts == ()
    assert missing_extraction.issues[0].code == "amount_alignment"

    good_path = tmp_path / "good.pdf"
    _synthetic_pdf(good_path)
    parsed_good = parse_text_pdf(good_path, document_id="synthetic-603288-2024")
    other_path = tmp_path / "other.pdf"
    _synthetic_pdf(other_path, values=("6,111,111,111.11", _VALUES[2023], _PERCENT, _VALUES[2022]))
    mismatch = extract_v2_key_financial_data_facts(
        parsed_good,
        source_pdf_path=other_path,
        company_id="603288",
        report_year=2024,
    )
    assert mismatch.facts == ()
    assert mismatch.issues[0].code == "source_sha256_mismatch"


def test_missing_currency_missing_percentage_header_or_missing_row_does_not_verify(tmp_path: Path) -> None:
    no_currency_path = tmp_path / "no-currency.pdf"
    _synthetic_pdf(no_currency_path, unit_line="单位：元")
    _parsed_no_currency, no_currency = _extract(no_currency_path)
    assert len(no_currency.facts) == 2
    assert any(item.code == "currency_undisclosed" for item in no_currency.issues)
    assert all(fact.currency == "未披露" for fact in no_currency.facts)
    assert verify_financial_fact(no_currency_path, no_currency.facts[0]).result.status == "insufficient_evidence"

    no_percent_path = tmp_path / "no-percent-heading.pdf"
    _synthetic_pdf(no_percent_path, omit_percentage_header=True)
    _parsed_no_percent, no_percent = _extract(no_percent_path)
    assert no_percent.facts == ()
    assert no_percent.issues[0].code == "percentage_column_not_found"

    missing_row_path = tmp_path / "missing-row.pdf"
    _synthetic_pdf(missing_row_path, omit_row=True)
    _parsed_missing_row, missing_row = _extract(missing_row_path)
    assert missing_row.facts == ()
    assert missing_row.issues[0].code == "row_not_found"
