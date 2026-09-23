"""用合成文字块验收年度事实提取，不读取真实财报。"""

from __future__ import annotations

from decimal import Decimal

from finagent.ingestion.extract_annual_facts import extract_annual_financial_facts
from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox, PdfPageText, PdfTextBlock

_BBOX = PdfBBox(0, 0, 10, 10)


def _page(number: int, texts: list[str]) -> PdfPageText:
    return PdfPageText(
        page_number=number,
        width=400,
        height=600,
        rotation=0,
        status="extracted",
        image_block_count=0,
        limitation=None,
        blocks=tuple(PdfTextBlock(index, text, _BBOX) for index, text in enumerate(texts)),
    )


def _parsed(*pages: PdfPageText) -> ParsedTextPdf:
    return ParsedTextPdf(
        document_id="synthetic-1",
        source_sha256="synthetic-hash",
        source_filename="synthetic.pdf",
        page_count=len(pages),
        coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point",
        coordinate_note="synthetic",
        ocr_applied=False,
        extractor="test",
        extractor_version="0",
        pages=pages,
    )


def _facts(result, name: str):
    return [fact for fact in result.facts if fact.indicator_name == name]


def _issues(result, name: str):
    return [issue for issue in result.issues if issue.indicator_name == name]


def test_main_revenue_row_beats_detail_row_and_other_tables() -> None:
    parsed = _parsed(
        _page(
            3,
            [
                "主要会计数据",
                "单位：元  币种：人民币",
                "2024年 2023年",
                "营业收入\n9,999.00 8,888.00",
            ],
        ),
        _page(
            4,
            [
                "合并利润表",
                "2024 年1—12 月",
                "单位：元  币种：人民币",
                "项目 附注 2024 年度 2023 年度",
                "一、营业收入\n七、61\n2,000.00 1,500.00",
                "其中：营业收入\n9,999.99 8,888.88",
            ],
        ),
        _page(
            5,
            [
                "母公司利润表",
                "2024 年1—12 月",
                "单位：元  币种：人民币",
                "项目 2024 年度 2023 年度",
                "一、营业收入\n5.00 4.00",
            ],
        ),
    )
    result = extract_annual_financial_facts(parsed, "603288", 2024)
    facts = _facts(result, "营业收入")
    assert _issues(result, "营业收入") == []
    assert [fact.report_year for fact in facts] == [2024, 2023]
    assert [Decimal(fact.normalized_value) for fact in facts] == [Decimal("2000.00"), Decimal("1500.00")]
    assert {hit.page_number for fact in facts for hit in fact.hits} == {4}
    assert facts[0].hits[0].block_index == 4
    assert "一、营业收入" in facts[0].hits[0].text
    assert "七、61" in facts[0].hits[0].text
    assert all("其中：营业收入" not in note for fact in facts for note in fact.limitations)
    assert facts[0].source_sha256 == "synthetic-hash"
    assert facts[0].statement_scope == "合并"
    assert [(fact.report_year, fact.column_role) for fact in facts] == [(2024, "current"), (2023, "comparative")]
    assert {fact.restatement_status for fact in facts} == {"unknown"}


def test_detail_revenue_is_marked_when_main_row_is_absent() -> None:
    parsed = _parsed(
        _page(
            2,
            [
                "合并利润表",
                "2024 年1—12 月",
                "单位：元  币种：人民币",
                "项目 附注 2024 年度 2023 年度",
                "其中：营业收入\n七、61\n26,900.50 24,559.25",
            ],
        )
    )
    result = extract_annual_financial_facts(parsed, "603288", 2024)
    facts = _facts(result, "营业收入")
    assert _issues(result, "营业收入") == []
    assert Decimal(facts[0].normalized_value) == Decimal("26900.50")
    assert [(fact.report_year, fact.column_role) for fact in facts] == [(2024, "current"), (2023, "comparative")]
    assert all(fact.restatement_status == "unknown" for fact in facts)
    assert any("一、营业收入" in note and "其中：营业收入" in note for note in facts[0].limitations)
    assert facts[0].hits[0].page_number == 2


def test_duplicate_main_revenue_row_abstains() -> None:
    parsed = _parsed(
        _page(
            1,
            [
                "合并利润表",
                "单位：元  币种：人民币",
                "项目 2024 年度 2023 年度",
                "一、营业收入\n10.00 9.00",
                "一、营业收入\n8.00 7.00",
            ],
        )
    )
    result = extract_annual_financial_facts(parsed, "603288", 2024)
    assert _facts(result, "营业收入") == []
    assert _issues(result, "营业收入")[0].code == "ambiguous_row"


def test_cross_page_split_keeps_both_block_hits() -> None:
    parsed = _parsed(
        _page(
            1,
            [
                "合并利润表",
                "2024 年1—12 月",
                "单位：元  币种：人民币",
                "项目 附注 2024 年度 2023 年度",
                "1.归属于母公司股东的净利润（净",
            ],
        ),
        _page(
            2,
            [
                "亏损以“-”号填列）\n6,344.00\n5,000.00",
                "合并现金流量表",
                "2024 年1—12 月",
                "单位：元  币种：人民币",
                "项目 附注 2024年度 2023年度",
                "经营活动产生的现金流量净",
            ],
        ),
        _page(3, ["额\n-1,200.50\n（800.25）"]),
    )
    result = extract_annual_financial_facts(parsed, "603288", 2024)
    profit = _facts(result, "归属于母公司股东的净利润")
    cash = _facts(result, "经营活动产生的现金流量净额")
    assert _issues(result, "归属于母公司股东的净利润") == []
    assert _issues(result, "经营活动产生的现金流量净额") == []
    assert [(hit.page_number, hit.block_index) for hit in profit[0].hits] == [(1, 4), (2, 0)]
    assert Decimal(profit[0].normalized_value) == Decimal("6344.00")
    assert Decimal(profit[1].normalized_value) == Decimal("5000.00")
    assert [(hit.page_number, hit.block_index) for hit in cash[0].hits] == [(2, 5), (3, 0)]
    assert Decimal(cash[0].normalized_value) == Decimal("-1200.50")
    assert Decimal(cash[1].normalized_value) == Decimal("-800.25")
    assert cash[1].raw_value == "（800.25）"


def test_non_recurring_total_stops_before_next_section() -> None:
    parsed = _parsed(
        _page(
            8,
            [
                "十、 非经常性损益项目和金额",
                "单位：元  币种：人民币",
                "非经常性损益项目 2024 年金额",
                "2023 年金额 2022 年金额",
                "政府补助\n10.00 9.00 8.00",
            ],
        ),
        _page(
            9,
            [
                "合计\n274.50 231.25 200.00",
                "十一、 采用公允价值计量的项目",
                "单位：元  币种：人民币",
                "项目 2024 年金额 2023 年金额 2022 年金额",
                "合计\n999.00 888.00 777.00",
            ],
        ),
    )
    result = extract_annual_financial_facts(parsed, "603288", 2024)
    facts = _facts(result, "披露的非经常性损益合计")
    assert _issues(result, "披露的非经常性损益合计") == []
    assert [Decimal(fact.normalized_value) for fact in facts] == [Decimal("274.50"), Decimal("231.25")]
    assert facts[0].statement_scope == "披露表格口径"
    assert [(hit.page_number, hit.block_index) for hit in facts[0].hits] == [(9, 0)]
    assert "999.00" not in facts[0].hits[0].text


def test_missing_unit_or_year_header_abstains() -> None:
    missing_unit = _parsed(
        _page(
            1,
            [
                "合并利润表",
                "项目 2024 年度 2023 年度",
                "一、营业收入\n10.00 9.00",
            ],
        )
    )
    missing_year = _parsed(
        _page(
            1,
            [
                "合并利润表",
                "单位：元  币种：人民币",
                "一、营业收入\n10.00 9.00",
            ],
        )
    )
    no_unit = extract_annual_financial_facts(missing_unit, "603288", 2024)
    no_year = extract_annual_financial_facts(missing_year, "603288", 2024)
    assert _facts(no_unit, "营业收入") == []
    assert _issues(no_unit, "营业收入")[0].code == "missing_unit"
    assert _facts(no_year, "营业收入") == []
    assert _issues(no_year, "营业收入")[0].code == "missing_year_header"


def test_plain_decimal_parentheses_and_note_number() -> None:
    parsed = _parsed(
        _page(
            6,
            [
                "合并利润表",
                "单位：元  币种：人民币",
                "项目 附注 2024 年度 2023 年度",
                "一、营业收入\n七、61\n1234.50 （1000.25）",
            ],
        )
    )
    result = extract_annual_financial_facts(parsed, "603288", 2024)
    facts = _facts(result, "营业收入")
    assert _issues(result, "营业收入") == []
    assert facts[0].raw_value == "1234.50"
    assert facts[1].raw_value == "（1000.25）"
    assert Decimal(facts[0].normalized_value) == Decimal("1234.50")
    assert Decimal(facts[1].normalized_value) == Decimal("-1000.25")
    assert facts[0].hits[0].page_number == 6
    assert facts[0].hits[0].block_index == 3
