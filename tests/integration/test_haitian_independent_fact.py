"""真实海天年报的旧提取 → v2 适配 → 独立原文核验链路。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finagent.ingestion import (
    extract_annual_financial_facts,
    extract_v2_balance_sheet_facts,
    extract_v2_income_statement_facts,
    extract_v2_key_financial_data_facts,
    parse_text_pdf,
)
from finagent.schemas.financial_fact_v2 import adapt_legacy_financial_fact
from finagent.verification.independent_fact import verify_financial_fact

_ROOT = Path(__file__).resolve().parents[2]
_PDF = _ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "1222994233.PDF"
_EXPECTED_PAGES = {
    ("revenue", 2024): 81,
    ("revenue", 2023): 81,
    ("net_profit_parent", 2024): 82,
    ("net_profit_parent", 2023): 82,
    ("operating_cash_flow", 2024): 86,
    ("operating_cash_flow", 2023): 86,
    ("non_recurring_total", 2024): 9,
    ("non_recurring_total", 2023): 9,
}


def test_haitian_legacy_extraction_adaptation_and_independent_verification() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    parsed = parse_text_pdf(_PDF, document_id="cninfo-1222994233")
    extracted = extract_annual_financial_facts(parsed, company_id="603288", report_year=2024)
    assert extracted.issues == ()
    assert len(extracted.facts) == len(_EXPECTED_PAGES)

    verified = {}
    for legacy_fact in extracted.facts:
        fact, _legacy_evidence = adapt_legacy_financial_fact(legacy_fact, report_year=2024)
        assert fact.unit is None

        checked = verify_financial_fact(_PDF, fact)
        key = (fact.metric_id, fact.period_end.year if fact.period_end else None)
        assert checked.result.status == "verified", (key, checked.result.limitations)
        assert key in _EXPECTED_PAGES
        assert key not in verified
        verified[key] = checked

        evidence = checked.evidence[0]
        assert evidence.value_region is not None
        assert evidence.value_region.page == _EXPECTED_PAGES[key]
        assert evidence.value_raw == legacy_fact.raw_value
        assert evidence.value_normalized == legacy_fact.normalized_value
        assert evidence.unit == "元"
        assert evidence.currency == "人民币"
        assert evidence.period_end == date(key[1], 12, 31)
        assert evidence.evidence_id not in fact.evidence_ids
        if fact.metric_id == "net_profit_parent":
            assert evidence.row_region is not None
            assert "归属于母公司股东的净利润" in evidence.row_region.text
        if fact.metric_id == "operating_cash_flow":
            assert evidence.row_region is not None
            assert "经营活动产生的现金流量净额" in evidence.row_region.text

    assert set(verified) == set(_EXPECTED_PAGES)


def test_haitian_v2_balance_sheet_extracts_and_independently_verifies_four_instant_facts() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    parsed = parse_text_pdf(_PDF, document_id="cninfo-1222994233")
    extracted = extract_v2_balance_sheet_facts(parsed, company_id="603288", report_year=2024)
    assert extracted.issues == ()
    assert len(extracted.facts) == 4
    assert len(extracted.evidence) == 4

    expected = {
        ("accounts_receivable_net", 2024): "242,260,969.75",
        ("accounts_receivable_net", 2023): "223,149,082.18",
        ("inventory_net", 2024): "2,525,273,760.73",
        ("inventory_net", 2023): "2,618,773,147.00",
    }
    seen = set()
    for fact, extracted_evidence in zip(extracted.facts, extracted.evidence, strict=True):
        year = fact.period_end.year
        key = (fact.metric_id, year)
        assert key in expected
        assert key not in seen
        seen.add(key)
        assert fact.raw_value == expected[key]
        assert fact.normalized_value == expected[key].replace(",", "")
        assert fact.company_id == "603288"
        assert fact.source_document_id == "cninfo-1222994233"
        assert fact.source_sha256 == parsed.source_sha256
        assert fact.period_type == "instant"
        assert fact.period_start is None
        assert fact.period_end == date(year, 12, 31)
        assert fact.comparison_role == ("current" if year == 2024 else "comparative")
        assert fact.restatement_status == "unknown"
        assert fact.scope == "consolidated"
        assert fact.statement_type == "balance_sheet"
        assert fact.currency == "人民币"
        assert fact.unit == "元"
        assert extracted_evidence.pdf_page == 76
        assert extracted_evidence.value_region is not None
        assert extracted_evidence.value_region.page == 76
        assert extracted_evidence.value_region.bbox.x1 > extracted_evidence.value_region.bbox.x0
        assert extracted_evidence.value_region.bbox.y1 > extracted_evidence.value_region.bbox.y0

        checked = verify_financial_fact(_PDF, fact)
        assert checked.result.status == "verified", (key, checked.result.limitations)
        assert checked.evidence[0].evidence_id not in fact.evidence_ids
        independent = checked.evidence[0]
        assert independent.pdf_page == 76
        assert independent.table_title == "合并资产负债表"
        assert independent.row_label == fact.label_raw
        assert independent.value_raw == fact.raw_value
        assert independent.value_normalized == fact.normalized_value
        assert independent.unit == "元"
        assert independent.currency == "人民币"
        assert independent.period_type == "instant"
        assert independent.period_start is None
        assert independent.period_end == date(year, 12, 31)
        for region in (
            independent.value_region,
            independent.row_region,
            independent.column_region,
            independent.title_region,
            independent.unit_region,
        ):
            assert region is not None
            assert region.page == 76
            assert region.bbox.x1 > region.bbox.x0
            assert region.bbox.y1 > region.bbox.y0

    assert seen == set(expected)


def test_haitian_v2_income_statement_extracts_and_independently_verifies_four_duration_facts() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    parsed = parse_text_pdf(_PDF, document_id="cninfo-1222994233")
    extracted = extract_v2_income_statement_facts(parsed, company_id="603288", report_year=2024)
    assert extracted.issues == ()
    assert len(extracted.facts) == 4
    assert len(extracted.evidence) == 4

    expected = {
        ("net_profit_consolidated", 2024): ("6,355,860,951.43", 82),
        ("net_profit_consolidated", 2023): ("5,642,186,761.43", 82),
        ("cost_of_goods_sold", 2024): ("16,948,316,999.75", 81),
        ("cost_of_goods_sold", 2023): ("16,028,535,574.92", 81),
    }
    seen = set()
    for fact, extracted_evidence in zip(extracted.facts, extracted.evidence, strict=True):
        year = fact.period_end.year
        key = (fact.metric_id, year)
        assert key in expected
        assert key not in seen
        seen.add(key)
        value, page_number = expected[key]
        assert fact.raw_value == value
        assert fact.normalized_value == value.replace(",", "")
        assert fact.company_id == "603288"
        assert fact.source_document_id == "cninfo-1222994233"
        assert fact.source_sha256 == parsed.source_sha256
        assert fact.report_year == 2024
        assert fact.period_start == date(year, 1, 1)
        assert fact.period_end == date(year, 12, 31)
        assert fact.period_type == "duration"
        assert fact.frequency == "annual"
        assert fact.comparison_role == ("current" if year == 2024 else "comparative")
        assert fact.restatement_status == "unknown"
        assert fact.scope == "consolidated"
        assert fact.statement_type == "income_statement"
        assert fact.currency == "人民币"
        assert fact.unit == "元"
        assert extracted_evidence.pdf_page == page_number
        assert extracted_evidence.table_title == "合并利润表"
        assert extracted_evidence.value_raw == value
        assert extracted_evidence.value_region is not None
        assert extracted_evidence.value_region.page == page_number
        assert extracted_evidence.value_region.bbox.x1 > extracted_evidence.value_region.bbox.x0
        assert extracted_evidence.value_region.bbox.y1 > extracted_evidence.value_region.bbox.y0

        checked = verify_financial_fact(_PDF, fact)
        assert checked.result.status == "verified", (key, checked.result.limitations)
        assert checked.evidence[0].evidence_id not in fact.evidence_ids
        evidence = checked.evidence[0]
        assert evidence.pdf_page == page_number
        assert evidence.table_title == "合并利润表"
        assert evidence.row_label is not None
        assert ("净利润" if fact.metric_id == "net_profit_consolidated" else "营业成本") in evidence.row_label
        assert evidence.value_raw == value
        assert evidence.value_normalized == value.replace(",", "")
        assert evidence.unit == "元"
        assert evidence.currency == "人民币"
        assert evidence.period_type == "duration"
        assert evidence.period_start == date(year, 1, 1)
        assert evidence.period_end == date(year, 12, 31)
        for region in (
            evidence.value_region,
            evidence.row_region,
            evidence.column_region,
            evidence.title_region,
            evidence.unit_region,
        ):
            assert region is not None
            assert region.page in {81, 82}
            assert region.bbox.x1 > region.bbox.x0
            assert region.bbox.y1 > region.bbox.y0
        assert evidence.value_region is not None and evidence.value_region.page == page_number
        assert evidence.row_region is not None and evidence.row_region.page == page_number
        assert evidence.column_region is not None and evidence.column_region.page == 81
        assert evidence.title_region is not None and evidence.title_region.page == 81
        assert evidence.unit_region is not None and evidence.unit_region.page == 81

    assert seen == set(expected)


def test_haitian_v2_key_financial_data_extracts_and_verifies_deducted_parent_profit() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    parsed = parse_text_pdf(_PDF, document_id="cninfo-1222994233")
    extracted = extract_v2_key_financial_data_facts(
        parsed,
        source_pdf_path=_PDF,
        company_id="603288",
        report_year=2024,
    )
    assert extracted.issues == ()
    assert len(extracted.facts) == 2
    assert len(extracted.evidence) == 2

    expected = {
        ("net_profit_parent_ex_nonrecurring", 2024): "6,069,416,506.67",
        ("net_profit_parent_ex_nonrecurring", 2023): "5,394,663,934.17",
    }
    seen = set()
    for fact, extracted_evidence in zip(extracted.facts, extracted.evidence, strict=True):
        year = fact.period_end.year
        key = (fact.metric_id, year)
        assert key in expected
        assert key not in seen
        seen.add(key)
        raw_value = expected[key]
        assert fact.raw_value == raw_value
        assert fact.normalized_value == raw_value.replace(",", "")
        assert fact.company_id == "603288"
        assert fact.source_document_id == "cninfo-1222994233"
        assert fact.source_sha256 == parsed.source_sha256
        assert fact.report_year == 2024
        assert fact.period_start == date(year, 1, 1)
        assert fact.period_end == date(year, 12, 31)
        assert fact.period_type == "duration"
        assert fact.frequency == "annual"
        assert fact.comparison_role == ("current" if year == 2024 else "comparative")
        assert fact.statement_type == "key_financial_data"
        assert fact.scope == "unknown"
        assert fact.currency == "人民币"
        assert fact.unit == "元"
        assert fact.restatement_status == "unknown"
        assert extracted_evidence.pdf_page == 7
        assert extracted_evidence.printed_page is None
        assert extracted_evidence.table_title == "主要会计数据"
        assert extracted_evidence.column_label == f"{year}年"
        assert extracted_evidence.value_raw == raw_value
        assert extracted_evidence.value_region is not None
        assert extracted_evidence.value_region.page == 7
        assert extracted_evidence.row_region is not None
        assert "归属于上市公司股东的扣" in extracted_evidence.row_region.text
        assert extracted_evidence.column_region is not None
        assert f"{year}年" in extracted_evidence.column_region.text
        assert extracted_evidence.unit_region is not None
        assert "人民币" in extracted_evidence.unit_region.text

        checked = verify_financial_fact(_PDF, fact)
        assert checked.result.status == "verified", checked.result.limitations
        assert checked.evidence[0].evidence_id not in fact.evidence_ids
        evidence = checked.evidence[0]
        assert evidence.pdf_page == 7
        assert evidence.table_title == "主要会计数据"
        assert evidence.row_label == fact.label_raw
        assert evidence.value_raw == raw_value
        assert evidence.value_normalized == raw_value.replace(",", "")
        assert evidence.column_label is not None and f"{year}年" in evidence.column_label
        assert evidence.unit == "元"
        assert evidence.currency == "人民币"
        assert evidence.period_start == date(year, 1, 1)
        assert evidence.period_end == date(year, 12, 31)
        assert evidence.period_type == "duration"
        assert "scope 保持 unknown" in checked.result.limitations[1]

    assert seen == set(expected)
