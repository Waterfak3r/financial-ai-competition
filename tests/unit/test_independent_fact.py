"""独立事实核验的合成 PDF。不使用提取坐标。"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import pymupdf

from finagent.schemas.financial_fact_v2 import FinancialFactV2
from finagent.verification.independent_fact import verify_financial_fact


def _pdf(path: Path, pages: list[list[list[tuple[int, str]]]]) -> str:
    document = pymupdf.open()
    for lines in pages:
        page = document.new_page()
        y = 72
        for line in lines:
            for x, text in line:
                page.insert_text((x, y), text, fontname="china-s")
            y += 22
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fact(sha: str, **overrides) -> FinancialFactV2:
    payload = dict(
        fact_id="fact-revenue",
        metric_id="revenue",
        company_id="603288",
        label_raw="其中：营业收入",
        raw_value="100.00",
        normalized_value="100.00",
        currency="CNY",
        unit_multiplier="1",
        unit="元",
        report_year=2024,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_type="duration",
        frequency="annual",
        statement_type="income_statement",
        scope="consolidated",
        comparison_role="current",
        restatement_status="unknown",
        source_document_id="doc-1",
        source_sha256=sha,
        evidence_ids=("legacy-evidence",),
        extraction_method="test",
    )
    payload.update(overrides)
    return FinancialFactV2(**payload)


def _base() -> list[list[tuple[int, str]]]:
    return [
        [(72, "合并利润表")],
        [(72, "单位：元  币种：人民币")],
        [(72, "项目"), (280, "2024 年度"), (420, "2023 年度")],
        [(72, "其中：营业收入"), (280, "100.00"), (420, "80.00")],
    ]


def _split_cash_flow_pdf(path: Path, final_label: str) -> str:
    document = pymupdf.open()
    page = document.new_page()
    for x, y, text in (
        (72, 72, "合并现金流量表"),
        (72, 94, "单位：元  币种：人民币"),
        (72, 116, "项目"),
        (280, 116, "2024 年度"),
        (420, 116, "2023 年度"),
        (72, 153, "经营活动产生的现金流量净"),
        (280, 163, "100.00"),
        (420, 163, "80.00"),
        (72, 173, final_label),
    ):
        page.insert_text((x, y), text, fontname="china-s")
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verified_fact_uses_new_regions(tmp_path: Path) -> None:
    sha = _pdf(tmp_path / "ok.pdf", [_base()])
    checked = verify_financial_fact(tmp_path / "ok.pdf", _fact(sha))
    assert checked.result.status == "verified"
    assert checked.result.evidence_ids != ("legacy-evidence",)
    evidence = checked.evidence[0]
    assert evidence.value_region is not None
    assert evidence.row_region is not None
    assert evidence.column_region is not None
    assert evidence.title_region is not None
    assert evidence.unit_region is not None
    assert evidence.unit == "元"
    assert evidence.currency == "人民币"
    assert evidence.value_normalized == "100.00"
    assert evidence.period_end == date(2024, 12, 31)
    assert evidence.period_type == "duration"
    assert "追溯" in "".join(checked.result.limitations)


def test_wrong_value_column_unit_scope_header_and_duplicate(tmp_path: Path) -> None:
    sha = _pdf(tmp_path / "a.pdf", [_base()])
    assert verify_financial_fact(tmp_path / "a.pdf", _fact(sha, raw_value="9.00", normalized_value="9.00")).result.status == "conflict"
    assert verify_financial_fact(tmp_path / "a.pdf", _fact(sha, raw_value="80.00", normalized_value="80.00", period_end=date(2023, 12, 31), period_start=date(2023, 1, 1), comparison_role="comparative")).result.status == "verified"
    wrong_column = verify_financial_fact(
        tmp_path / "a.pdf",
        _fact(sha, raw_value="80.00", normalized_value="80.00"),
    )
    assert wrong_column.result.status == "conflict"
    assert verify_financial_fact(tmp_path / "a.pdf", _fact(sha, unit="万元", unit_multiplier="10000")).result.status == "conflict"
    assert verify_financial_fact(tmp_path / "a.pdf", _fact(sha, unit="万元", unit_multiplier="1")).result.status == "conflict"
    unknown_unit = verify_financial_fact(tmp_path / "a.pdf", _fact(sha, unit=None))
    assert unknown_unit.result.status == "verified"
    assert any("未提供单位" in note for note in unknown_unit.result.limitations)
    assert verify_financial_fact(
        tmp_path / "a.pdf",
        _fact(sha, unit=None, unit_multiplier="10000"),
    ).result.status == "conflict"
    assert verify_financial_fact(tmp_path / "a.pdf", _fact(sha, scope="parent")).result.status == "conflict"
    missing = _pdf(
        tmp_path / "b.pdf",
        [[[(72, "合并利润表")], [(72, "单位：元  币种：人民币")], [(72, "其中：营业收入"), (280, "100.00"), (420, "80.00")]]],
    )
    assert verify_financial_fact(tmp_path / "b.pdf", _fact(missing)).result.status == "insufficient_evidence"
    duplicated = _pdf(
        tmp_path / "c.pdf",
        [_base() + [[(72, "其中：营业收入"), (280, "100.00"), (420, "80.00")]]],
    )
    assert verify_financial_fact(tmp_path / "c.pdf", _fact(duplicated)).result.status == "insufficient_evidence"


def test_split_minus_and_parentheses_preserve_negative_amount(tmp_path: Path) -> None:
    minus_lines = _base()
    minus_lines[-1] = [(72, "其中：营业收入"), (280, "-"), (286, "100.00"), (420, "80.00")]
    minus_sha = _pdf(tmp_path / "minus.pdf", [minus_lines])
    negative = verify_financial_fact(
        tmp_path / "minus.pdf",
        _fact(minus_sha, raw_value="-100.00", normalized_value="-100.00"),
    )
    assert negative.result.status == "verified"
    assert negative.evidence[0].value_raw == "-100.00"
    assert negative.evidence[0].value_region is not None
    assert negative.evidence[0].value_region.text == "-100.00"
    assert negative.evidence[0].value_region.bbox.x0 < 286
    assert verify_financial_fact(tmp_path / "minus.pdf", _fact(minus_sha)).result.status == "conflict"

    parenthesized_lines = _base()
    parenthesized_lines[-1] = [
        (72, "其中：营业收入"),
        (280, "("),
        (285, "100.00"),
        (355, ")"),
        (420, "80.00"),
    ]
    paren_sha = _pdf(tmp_path / "parenthesized.pdf", [parenthesized_lines])
    accounting_negative = verify_financial_fact(
        tmp_path / "parenthesized.pdf",
        _fact(paren_sha, raw_value="(100.00)", normalized_value="-100.00"),
    )
    assert accounting_negative.result.status == "verified"
    assert accounting_negative.evidence[0].value_raw == "(100.00)"
    assert accounting_negative.evidence[0].value_region is not None
    assert accounting_negative.evidence[0].value_region.text == "(100.00)"
    assert accounting_negative.evidence[0].value_region.bbox.x0 < 285
    assert verify_financial_fact(tmp_path / "parenthesized.pdf", _fact(paren_sha)).result.status == "conflict"


def test_unmatched_parenthesis_is_insufficient(tmp_path: Path) -> None:
    lines = _base()
    lines[-1] = [(72, "其中：营业收入"), (280, "("), (285, "100.00"), (420, "80.00")]
    sha = _pdf(tmp_path / "unmatched.pdf", [lines])
    checked = verify_financial_fact(
        tmp_path / "unmatched.pdf",
        _fact(sha, raw_value="100.00", normalized_value="100.00"),
    )
    assert checked.result.status == "insufficient_evidence"


def test_distant_split_minus_is_not_silently_dropped(tmp_path: Path) -> None:
    lines = _base()
    lines[-1] = [(72, "其中：营业收入"), (280, "-"), (300, "100.00"), (420, "80.00")]
    sha = _pdf(tmp_path / "distant-minus.pdf", [lines])
    checked = verify_financial_fact(
        tmp_path / "distant-minus.pdf",
        _fact(sha, raw_value="100.00", normalized_value="100.00"),
    )
    assert checked.result.status == "insufficient_evidence"


def test_cross_page_row_and_hash_conflict(tmp_path: Path) -> None:
    sha = _pdf(
        tmp_path / "cross.pdf",
        [
            [[(72, "合并现金流量表")], [(72, "单位：元  币种：人民币")], [(72, "项目"), (280, "2024 年度"), (420, "2023 年度")]],
            [[(72, "经营活动产生的现金流量净额"), (280, "20.00"), (420, "30.00")]],
        ],
    )
    fact = _fact(
        sha,
        fact_id="fact-cash",
        metric_id="operating_cash_flow",
        label_raw="经营活动产生的现金流量净额",
        raw_value="20.00",
        normalized_value="20.00",
        statement_type="cash_flow_statement",
    )
    checked = verify_financial_fact(tmp_path / "cross.pdf", fact)
    assert checked.result.status == "verified"
    assert checked.evidence[0].title_region.page == 1
    assert checked.evidence[0].value_region.page == 2
    mismatched = verify_financial_fact(tmp_path / "cross.pdf", _fact("b" * 64))
    assert mismatched.result.status == "conflict"
    assert mismatched.evidence == ()


def test_cash_flow_label_region_contains_only_complete_recognized_label(tmp_path: Path) -> None:
    path = tmp_path / "cash-flow.pdf"
    sha = _split_cash_flow_pdf(path, "额")
    fact = _fact(
        sha,
        fact_id="fact-cash-split-label",
        metric_id="operating_cash_flow",
        label_raw="经营活动产生的现金流量净额",
        statement_type="cash_flow_statement",
    )
    checked = verify_financial_fact(path, fact)
    assert checked.result.status == "verified", checked.result.limitations
    evidence = checked.evidence[0]
    assert evidence.row_label == "经营活动产生的现金流量净额"
    assert evidence.row_region is not None
    assert evidence.row_region.text == "经营活动产生的现金流量净额"


def test_unrelated_neighbor_text_cannot_complete_cash_flow_row(tmp_path: Path) -> None:
    path = tmp_path / "cash-flow-unrelated.pdf"
    sha = _split_cash_flow_pdf(path, "额外说明")
    fact = _fact(
        sha,
        fact_id="fact-cash-unrelated-label",
        metric_id="operating_cash_flow",
        label_raw="经营活动产生的现金流量净额",
        statement_type="cash_flow_statement",
    )
    assert verify_financial_fact(path, fact).result.status == "insufficient_evidence"
