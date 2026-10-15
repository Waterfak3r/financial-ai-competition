"""利润表 v2 提取与独立核验的真实 PDF 错误注入测试。"""

from __future__ import annotations

import hashlib
import re

from dataclasses import replace
from pathlib import Path

import pymupdf

from finagent.ingestion import extract_v2_income_statement_facts, parse_text_pdf
from finagent.verification.independent_fact import verify_financial_fact

_VALUES = {
    "net_profit_consolidated": ("6,355,860,951.43", "5,642,186,761.43"),
    "cost_of_goods_sold": ("16,948,316,999.75", "16,028,535,574.92"),
}


def _synthetic_pdf(
    path: Path,
    *,
    unit_line: str = "单位：元  币种：人民币",
    years: tuple[int, int] = (2024, 2023),
    omit_cost_current: bool = False,
) -> str:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "合并利润表", fontname="china-s")
    page.insert_text((72, 94), unit_line, fontname="china-s")
    page.insert_text((72, 116), "项目", fontname="china-s")
    page.insert_text((300, 116), f"{years[0]}年度", fontname="china-s", fontsize=7)
    page.insert_text((445, 116), f"{years[1]}年度", fontname="china-s", fontsize=7)
    page.insert_text((72, 146), "五、净利润（净亏损以‘－’号填列）", fontname="china-s", fontsize=7)
    page.insert_text((300, 146), _VALUES["net_profit_consolidated"][0], fontname="china-s", fontsize=7)
    page.insert_text((445, 146), _VALUES["net_profit_consolidated"][1], fontname="china-s", fontsize=7)
    page.insert_text((72, 174), "其中：营业成本", fontname="china-s", fontsize=7)
    if not omit_cost_current:
        page.insert_text((300, 174), _VALUES["cost_of_goods_sold"][0], fontname="china-s", fontsize=7)
    page.insert_text((445, 174), _VALUES["cost_of_goods_sold"][1], fontname="china-s", fontsize=7)
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract(path: Path, *, report_year: int = 2024, company_id: str = "603288", document_id: str = "synthetic-603288-2024"):
    parsed = parse_text_pdf(path, document_id=document_id)
    return extract_v2_income_statement_facts(parsed, company_id=company_id, report_year=report_year)


def test_constructed_income_statement_extracts_and_independently_verifies_four_annual_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic-income-statement.pdf"
    source_sha256 = _synthetic_pdf(path)
    extracted = _extract(path)

    assert extracted.issues == ()
    assert len(extracted.facts) == 4
    assert len(extracted.evidence) == 4
    seen = set()
    for fact, extracted_evidence in zip(extracted.facts, extracted.evidence, strict=True):
        year = fact.period_end.year
        key = (fact.metric_id, year)
        assert key not in seen
        seen.add(key)
        expected = _VALUES[fact.metric_id][0 if year == 2024 else 1]
        assert fact.raw_value == expected
        assert fact.normalized_value == expected.replace(",", "")
        assert fact.report_year == 2024
        assert fact.period_start.year == year and fact.period_start.month == 1 and fact.period_start.day == 1
        assert fact.period_end.year == year and fact.period_end.month == 12 and fact.period_end.day == 31
        assert fact.period_type == "duration"
        assert fact.frequency == "annual"
        assert fact.statement_type == "income_statement"
        assert fact.scope == "consolidated"
        assert fact.comparison_role == ("current" if year == 2024 else "comparative")
        assert fact.currency == "人民币"
        assert fact.unit == "元"
        assert fact.source_sha256 == source_sha256
        assert fact.restatement_status == "unknown"
        assert extracted_evidence.table_title == "合并利润表"
        assert extracted_evidence.value_raw == expected
        assert extracted_evidence.value_region is not None

        checked = verify_financial_fact(path, fact)
        assert checked.result.status == "verified", (key, checked.result.limitations)
        assert checked.evidence[0].evidence_id not in fact.evidence_ids
        evidence = checked.evidence[0]
        assert evidence.table_title == "合并利润表"
        assert evidence.value_raw == expected
        assert evidence.value_normalized == expected.replace(",", "")
        assert evidence.unit == "元"
        assert evidence.currency == "人民币"
        assert evidence.period_type == "duration"
        assert evidence.period_start is not None and evidence.period_start.year == year
        assert evidence.period_end is not None and evidence.period_end.year == year
        assert evidence.value_region is not None and evidence.value_region.page == 1

    assert seen == {
        ("net_profit_consolidated", 2024),
        ("net_profit_consolidated", 2023),
        ("cost_of_goods_sold", 2024),
        ("cost_of_goods_sold", 2023),
    }


def test_independent_income_statement_check_rejects_wrong_value_column_unit_scope_and_role(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic-income-errors.pdf"
    _synthetic_pdf(path)
    extracted = _extract(path)
    current = next(
        fact
        for fact in extracted.facts
        if fact.metric_id == "net_profit_consolidated" and fact.period_end.year == 2024
    )
    comparative = next(
        fact
        for fact in extracted.facts
        if fact.metric_id == "net_profit_consolidated" and fact.period_end.year == 2023
    )
    variants = (
        replace(current, raw_value="9.00", normalized_value="9.00"),
        replace(current, raw_value=comparative.raw_value, normalized_value=comparative.normalized_value),
        replace(current, unit="万元", unit_multiplier="10000"),
        replace(current, currency="美元"),
        replace(current, scope="parent"),
        replace(current, comparison_role="comparative"),
        replace(current, report_year=2025),
        replace(current, statement_type="balance_sheet"),
        replace(current, frequency=None),
        replace(current, restatement_status="not_restated"),
    )
    for fact in variants:
        assert verify_financial_fact(path, fact).result.status == "conflict"


def test_missing_currency_year_column_or_amount_fails_closed(tmp_path: Path) -> None:
    no_currency = tmp_path / "no-currency.pdf"
    _synthetic_pdf(no_currency, unit_line="单位：元")
    extracted = _extract(no_currency)
    candidate = next(fact for fact in extracted.facts if fact.metric_id == "cost_of_goods_sold")
    assert candidate.currency == "未披露"
    assert verify_financial_fact(no_currency, candidate).result.status == "insufficient_evidence"

    wrong_years = tmp_path / "wrong-years.pdf"
    wrong_sha = _synthetic_pdf(wrong_years, years=(2022, 2021))
    bad_header = _extract(wrong_years)
    assert bad_header.facts == ()
    assert all(item.code == "missing_year_header" for item in bad_header.issues)

    good_path = tmp_path / "good-year-source.pdf"
    _synthetic_pdf(good_path)
    good = _extract(good_path)
    fact = next(item for item in good.facts if item.metric_id == "net_profit_consolidated")
    assert verify_financial_fact(wrong_years, replace(fact, source_sha256=wrong_sha)).result.status == "insufficient_evidence"

    missing_amount = tmp_path / "missing-amount.pdf"
    missing_sha = _synthetic_pdf(missing_amount, omit_cost_current=True)
    incomplete = _extract(missing_amount)
    assert not any(fact.metric_id == "cost_of_goods_sold" for fact in incomplete.facts)
    assert any(issue.metric_id == "cost_of_goods_sold" and issue.code == "amount_alignment" for issue in incomplete.issues)
    current = next(
        fact
        for fact in good.facts
        if fact.metric_id == "cost_of_goods_sold" and fact.period_end.year == 2024
    )
    assert verify_financial_fact(missing_amount, replace(current, source_sha256=missing_sha)).result.status == "insufficient_evidence"


def test_missing_unit_prevents_income_statement_fact_creation(tmp_path: Path) -> None:
    path = tmp_path / "no-unit.pdf"
    _synthetic_pdf(path, unit_line="财务报表")
    extracted = _extract(path)
    assert extracted.facts == ()
    assert all(issue.code == "missing_unit" for issue in extracted.issues)


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
_CURRENT_COLUMN_X = 300
_COMPARATIVE_COLUMN_X = 445


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


def _multiline_block_pdf(path: Path, *, years: tuple[int, int] = (2025, 2024)) -> str:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "合并利润表", fontname="china-s")
    page.insert_text((72, 94), "单位：元", fontname="china-s")
    page.insert_text((180, 94), "币种：人民币", fontname="china-s")
    page.insert_text((72, 116), "项目", fontname="china-s")
    page.insert_text((_CURRENT_COLUMN_X, 116), f"{years[0]}年度", fontname="china-s", fontsize=7)
    page.insert_text((_COMPARATIVE_COLUMN_X, 116), f"{years[1]}年度", fontname="china-s", fontsize=7)
    _insert_same_block_rows(
        page,
        "减：所得税费用  58  29,444,936,771.41  30,303,850,168.56\n"
        "五、净利润（净亏损以‘－’号填列）  6,355,860,951.43  5,642,186,761.43",
        origin_y=140,
        current_x=_CURRENT_COLUMN_X,
        comparative_x=_COMPARATIVE_COLUMN_X,
    )
    page.insert_text((72, 230), "其中：营业成本", fontname="china-s", fontsize=7)
    page.insert_text((_CURRENT_COLUMN_X, 230), _VALUES["cost_of_goods_sold"][0], fontname="china-s", fontsize=7)
    page.insert_text((_COMPARATIVE_COLUMN_X, 230), _VALUES["cost_of_goods_sold"][1], fontname="china-s", fontsize=7)
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_multiline_same_block_finds_net_profit_and_keeps_cost_row(tmp_path: Path) -> None:
    path = tmp_path / "multiline-income.pdf"
    source_sha256 = _multiline_block_pdf(path)
    parsed = parse_text_pdf(path, document_id="synthetic-600519-2025")
    assert any(
        len(block.text.splitlines()) > 1
        and all(token in block.text for token in (
            "所得税费用", "净利润", *_VALUES["net_profit_consolidated"],
        ))
        for block in parsed.pages[0].blocks
    ), "fixture must contain the target row, adjacent row and amounts in one multiline block"
    extracted = _extract(path, report_year=2025, company_id="600519", document_id="synthetic-600519-2025")

    assert not any(issue.metric_id == "net_profit_consolidated" for issue in extracted.issues)
    profit = {
        fact.period_end.year: fact
        for fact in extracted.facts
        if fact.metric_id == "net_profit_consolidated"
    }
    assert set(profit) == {2025, 2024}
    assert profit[2025].raw_value == _VALUES["net_profit_consolidated"][0]
    assert profit[2024].raw_value == _VALUES["net_profit_consolidated"][1]
    assert profit[2025].company_id == "600519"
    assert profit[2025].currency == "人民币"
    assert profit[2025].source_sha256 == source_sha256
    for fact, evidence in zip(extracted.facts, extracted.evidence, strict=True):
        if fact.metric_id != "net_profit_consolidated":
            continue
        assert "所得税费用" not in evidence.surrounding_text
        assert "净利润" in evidence.surrounding_text
        assert fact.raw_value in evidence.surrounding_text
        assert evidence.row_region is not None
        assert "所得税费用" not in evidence.row_region.text
    cost = [fact for fact in extracted.facts if fact.metric_id == "cost_of_goods_sold"]
    assert len(cost) == 2
    for fact in (*profit.values(), *cost):
        checked = verify_financial_fact(path, fact)
        assert checked.result.status == "verified", (fact.metric_id, fact.period_end.year, checked.result.limitations)



def _income_body_pdf(path: Path, body: str, *, years: tuple[int, int] = (2024, 2023)) -> str:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "合并利润表", fontname="china-s")
    page.insert_text((72, 94), "单位：元  币种：人民币", fontname="china-s")
    page.insert_text((72, 116), "项目", fontname="china-s")
    page.insert_text((_CURRENT_COLUMN_X, 116), f"{years[0]}年度", fontname="china-s", fontsize=7)
    page.insert_text((_COMPARATIVE_COLUMN_X, 116), f"{years[1]}年度", fontname="china-s", fontsize=7)
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


def test_same_block_scanner_rejects_duplicate_and_does_not_steal_next_row(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate-profit.pdf"
    _income_body_pdf(
        duplicate,
        "五、净利润（净亏损以‘－’号填列）  6,355,860,951.43  5,642,186,761.43\n"
        "五、净利润（净亏损以‘－’号填列）  1.00  2.00",
    )
    duplicated = _extract(duplicate)
    assert not any(fact.metric_id == "net_profit_consolidated" for fact in duplicated.facts)
    assert any(
        issue.metric_id == "net_profit_consolidated" and issue.code == "ambiguous_row"
        for issue in duplicated.issues
    )

    stolen = tmp_path / "missing-profit-amounts.pdf"
    _income_body_pdf(
        stolen,
        "五、净利润（净亏损以‘－’号填列）\n"
        "其中：营业成本  16,948,316,999.75  16,028,535,574.92",
    )
    missing = _extract(stolen)
    assert not any(fact.metric_id == "net_profit_consolidated" for fact in missing.facts)
    assert any(
        issue.metric_id == "net_profit_consolidated" and issue.code in {"row_not_found", "amount_alignment"}
        for issue in missing.issues
    )
    cost = {
        fact.period_end.year: fact
        for fact in missing.facts
        if fact.metric_id == "cost_of_goods_sold"
    }
    assert set(cost) == {2024, 2023}
    assert cost[2024].raw_value == _VALUES["cost_of_goods_sold"][0]
    assert cost[2023].raw_value == _VALUES["cost_of_goods_sold"][1]
    for fact in cost.values():
        checked = verify_financial_fact(stolen, fact)
        assert checked.result.status == "verified", checked.result.limitations

    spaced = tmp_path / "note-blank-profit.pdf"
    _income_body_pdf(
        spaced,
        "五、净利润（净亏损以‘－’号填列）\n58\n\n6,355,860,951.43\n5,642,186,761.43",
    )
    kept = _extract(spaced)
    profit = {
        fact.period_end.year: fact
        for fact in kept.facts
        if fact.metric_id == "net_profit_consolidated"
    }
    assert set(profit) == {2024, 2023}
    assert profit[2024].raw_value == _VALUES["net_profit_consolidated"][0]
    assert profit[2023].raw_value == _VALUES["net_profit_consolidated"][1]
