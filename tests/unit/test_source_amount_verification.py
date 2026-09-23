"""原文坐标金额核验。证明不采用解析块文字。"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from finagent.schemas.financial_fact import COLUMN_ROLE_CURRENT, RESTATEMENT_STATUS_UNKNOWN, FactHit, FinancialFact
from finagent.verification.source_amount import _find_amount, verify_source_amounts


def _pdf(path: Path) -> tuple[float, float, float, float]:
    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((20, 40), "其他位置 999.00", fontsize=12)
    page.insert_text((20, 120), "营业收入 150.00", fontsize=12)
    blocks = page.get_text("blocks")
    target = next(block for block in blocks if "150.00" in block[4])
    document.save(path)
    document.close()
    return tuple(round(float(value), 4) for value in target[:4])


def _fact(raw: str, normalized: str, bbox: tuple[float, float, float, float], text: str = "不应被当作证据") -> FinancialFact:
    x0, y0, x1, y1 = bbox
    return FinancialFact(
        document_id="doc",
        source_sha256="a" * 64,
        company_id="603288",
        indicator_name="营业收入",
        table_name="合并利润表",
        raw_value=raw,
        normalized_value=normalized,
        report_year=2024,
        period_label="2024年度",
        period_type="annual",
        column_role=COLUMN_ROLE_CURRENT,
        restatement_status=RESTATEMENT_STATUS_UNKNOWN,
        unit_multiplier="1",
        currency="人民币",
        statement_scope="合并",
        extraction_method="test",
        hits=(FactHit(1, 1, text, x0, y0, x1, y1),),
    )


def test_accepts_amount_inside_cited_clip_and_ignores_hit_text(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    bbox = _pdf(pdf)
    report = verify_source_amounts(pdf, [_fact("150.00", "150.00", bbox, text="完全不同的文字")])
    result = report["results"][0]
    assert report["status"] == "passed"
    assert result["amount_located"] is True
    assert result["calculation_ok"] is True
    assert result["evidence"][0]["amount_in_this_clip"] is True
    assert "150.00" in result["evidence"][0]["extracted_text"]
    assert "完全不同的文字" not in result["evidence"][0]["extracted_text"]


def test_same_page_amount_outside_clip_fails(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    _pdf(pdf)
    wrong = (20.0, 20.0, 160.0, 50.0)
    report = verify_source_amounts(pdf, [_fact("150.00", "150.00", wrong)])
    result = report["results"][0]
    assert result["status"] == "failed"
    assert result["amount_located"] is False
    assert "999.00" in result["evidence"][0]["extracted_text"]
    assert "150.00" not in result["evidence"][0]["extracted_text"]


def test_tampered_amount_is_not_proven_by_page_text(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    bbox = _pdf(pdf)
    report = verify_source_amounts(pdf, [_fact("999.00", "999.00", bbox)])
    result = report["results"][0]
    assert result["status"] == "failed"
    assert result["amount_located"] is False
    assert result["calculation_ok"] is True


def test_amount_match_respects_digit_boundaries_and_split_blocks(tmp_path: Path) -> None:
    pdf = tmp_path / "boundary.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=240)
    page.insert_text((20, 40), "1150.00", fontsize=12)
    page.insert_text((20, 100), "150.", fontsize=12)
    page.insert_text((20, 160), "00", fontsize=12)
    blocks = page.get_text("blocks")
    whole = next(tuple(round(float(value), 4) for value in block[:4]) for block in blocks if "1150.00" in block[4])
    left = next(tuple(round(float(value), 4) for value in block[:4]) for block in blocks if block[4].strip() == "150.")
    right = next(tuple(round(float(value), 4) for value in block[:4]) for block in blocks if block[4].strip() == "00")
    document.save(pdf)
    document.close()
    embedded = verify_source_amounts(pdf, [_fact("150.00", "150.00", whole)])
    assert embedded["results"][0]["status"] == "failed"
    assert embedded["results"][0]["amount_located"] is False
    pieces = _fact("150.00", "150.00", left)
    pieces = FinancialFact(
        document_id=pieces.document_id,
        source_sha256=pieces.source_sha256,
        company_id=pieces.company_id,
        indicator_name=pieces.indicator_name,
        table_name=pieces.table_name,
        raw_value=pieces.raw_value,
        normalized_value=pieces.normalized_value,
        report_year=pieces.report_year,
        period_label=pieces.period_label,
        period_type=pieces.period_type,
        column_role=pieces.column_role,
        restatement_status=pieces.restatement_status,
        unit_multiplier=pieces.unit_multiplier,
        currency=pieces.currency,
        statement_scope=pieces.statement_scope,
        extraction_method=pieces.extraction_method,
        hits=(
            FactHit(1, 2, "解析文字不可用", *left),
            FactHit(1, 3, "解析文字不可用", *right),
        ),
    )
    joined = verify_source_amounts(pdf, [pieces])
    assert joined["results"][0]["status"] == "passed"
    assert joined["results"][0]["evidence"][0]["amount_in_this_clip"] is False
    assert joined["results"][0]["evidence"][1]["amount_in_this_clip"] is False


def test_amount_token_rejects_thousands_sign_and_parentheses() -> None:
    assert _find_amount("1,150.00", "150.00") is None
    assert _find_amount("1，150.00", "150.00") is None
    assert _find_amount("-150.00", "150.00") is None
    assert _find_amount("－150.00", "150.00") is None
    assert _find_amount("(150.00)", "150.00") is None
    assert _find_amount("（150.00）", "150.00") is None
    assert _find_amount("1150.00", "150.00") is None
    assert _find_amount("营业收入 1,150.00 元", "1,150.00")["matched_text"] == "1,150.00"
    assert _find_amount("期初 -150.00", "-150.00")["matched_text"] == "-150.00"
    assert _find_amount("亏损（150.00）完", "（150.00）")["matched_text"] == "（150.00）"
    assert _find_amount("150.\n00", "150.00")["matched_text"].replace("\n", "") == "150.00"
    assert _find_amount("七、61\n26,900,977,516.70", "26,900,977,516.70")["matched_text"] == "26,900,977,516.70"
    assert _find_amount("- 150.00", "150.00") is None
    assert _find_amount("-\t150.00", "150.00") is None
    assert _find_amount("( 150.00 )", "150.00") is None
    assert _find_amount("（ 150.00 ）", "150.00") is None
    assert _find_amount("1, 150.00", "150.00") is None
    assert _find_amount("- 150.00", "-150.00")["matched_text"].replace(" ", "") == "-150.00"
    assert _find_amount("( 150.00 )", "(150.00)")["matched_text"].replace(" ", "") == "(150.00)"
    assert _find_amount("（\t150.00\t）", "（150.00）")["matched_text"].replace("\t", "") == "（150.00）"
    assert _find_amount("26,900.00 24,559.00", "24,559.00")["matched_text"] == "24,559.00"
    assert _find_amount("26,900.00 24,559.00", "26,900.00")["matched_text"] == "26,900.00"
    assert _find_amount("61\n150.00", "150.00")["matched_text"] == "150.00"


def test_evidence_keeps_full_clip_text_and_match(tmp_path: Path) -> None:
    pdf = tmp_path / "long.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=900)
    y = 20.0
    for _ in range(40):
        page.insert_text((20, y), "注" * 20, fontsize=11)
        y += 16
    page.insert_text((20, y), "150.00", fontsize=11)
    document.save(pdf)
    document.close()
    bbox = (10.0, 5.0, 360.0, y + 8)
    result = verify_source_amounts(pdf, [_fact("150.00", "150.00", bbox)])["results"][0]
    assert result["status"] == "passed"
    assert len(result["evidence"][0]["extracted_text"]) > 500
    assert "150.00" in result["evidence"][0]["extracted_text"]
    assert result["amount_match"]["matched_text"] == "150.00"


def test_empty_facts_are_abstained(tmp_path: Path) -> None:
    pdf = tmp_path / "empty.pdf"
    _pdf(pdf)
    report = verify_source_amounts(pdf, [])
    assert report["status"] == "abstained"
    assert report["passed_count"] == 0
    assert report["results"] == []


def test_unreadable_hit_blocks_pass_even_if_another_clip_has_amount(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    bbox = _pdf(pdf)
    valid = _fact("150.00", "150.00", bbox)
    broken = FinancialFact(
        document_id=valid.document_id,
        source_sha256=valid.source_sha256,
        company_id=valid.company_id,
        indicator_name=valid.indicator_name,
        table_name=valid.table_name,
        raw_value=valid.raw_value,
        normalized_value=valid.normalized_value,
        report_year=valid.report_year,
        period_label=valid.period_label,
        period_type=valid.period_type,
        column_role=valid.column_role,
        restatement_status=valid.restatement_status,
        unit_multiplier=valid.unit_multiplier,
        currency=valid.currency,
        statement_scope=valid.statement_scope,
        extraction_method=valid.extraction_method,
        hits=(FactHit(1, 0, "坏坐标", bbox[0], bbox[1], bbox[0], bbox[1]), valid.hits[0]),
    )
    report = verify_source_amounts(pdf, [broken])
    result = report["results"][0]
    assert result["status"] == "abstained"
    assert result["amount_located"] is False
    assert "不能因其他引用块" in result["reason"]
    assert report["status"] == "abstained"


def test_wrong_normalized_value_fails_decimal_check(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    bbox = _pdf(pdf)
    report = verify_source_amounts(pdf, [_fact("150.00", "150.01", bbox)])
    result = report["results"][0]
    assert result["status"] == "failed"
    assert result["amount_located"] is True
    assert result["calculation_ok"] is False
    assert "不一致" in result["reason"]
