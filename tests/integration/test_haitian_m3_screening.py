"""M3 四规则对海天 2024 年报真实字段的独立核验组合。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from finagent.finance.m3_screening import RULE_1, RULE_2, RULE_3, RULE_4, screen_m3_annual_rules
from finagent.ingestion import (
    extract_annual_financial_facts,
    extract_v2_balance_sheet_facts,
    extract_v2_income_statement_facts,
    extract_v2_key_financial_data_facts,
    parse_text_pdf,
)
from finagent.schemas.financial_fact_v2 import FinancialFactV2, adapt_legacy_financial_fact
from finagent.verification.comparability import verify_annual_comparability
from finagent.verification.independent_fact import verify_financial_fact
from finagent.verification.m3_semantic_mapping import verify_parent_profit_semantic_mapping

_ROOT = Path(__file__).resolve().parents[2]
_PDF = _ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "1222994233.PDF"


def test_haitian_real_pdf_all_m3_rules_are_calculable_and_do_not_trigger() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    parsed = parse_text_pdf(_PDF, document_id="cninfo-1222994233")
    legacy = extract_annual_financial_facts(parsed, company_id="603288", report_year=2024)
    assert legacy.issues == ()

    facts: list[FinancialFactV2] = []
    # 旧 v2 适配器不推断单位。M3 用第一轮独立核验重新读出的单位补齐字段，再
    # 重新核验带单位的事实，确保正式计算不会把 unknown unit 当成可比口径。
    for legacy_fact in legacy.facts:
        fact, _ = adapt_legacy_financial_fact(legacy_fact, report_year=2024)
        first_check = verify_financial_fact(_PDF, fact)
        assert first_check.result.status == "verified", first_check.result.limitations
        assert first_check.evidence and first_check.evidence[0].unit == "元"
        fact = replace(fact, unit=first_check.evidence[0].unit)
        verified_check = verify_financial_fact(_PDF, fact)
        assert verified_check.result.status == "verified", verified_check.result.limitations
        facts.append(fact)

    balance = extract_v2_balance_sheet_facts(parsed, company_id="603288", report_year=2024)
    income = extract_v2_income_statement_facts(parsed, company_id="603288", report_year=2024)
    key_data = extract_v2_key_financial_data_facts(
        parsed,
        source_pdf_path=_PDF,
        company_id="603288",
        report_year=2024,
    )
    assert balance.issues == ()
    assert income.issues == ()
    assert key_data.issues == ()
    facts.extend(balance.facts)
    facts.extend(income.facts)
    facts.extend(key_data.facts)

    verifications = [verify_financial_fact(_PDF, fact).result for fact in facts]
    assert all(item.status == "verified" for item in verifications)
    comparability = verify_annual_comparability(
        _PDF,
        document_id="cninfo-1222994233",
        source_sha256=parsed.source_sha256,
        report_year=2024,
    )
    assert comparability.status == "verified", comparability.limitations

    semantic_check = verify_parent_profit_semantic_mapping(
        _PDF,
        key_facts=[fact for fact in facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in facts if fact.metric_id == "net_profit_parent"],
        verifications=verifications,
        report_year=2024,
    )
    assert semantic_check.proof is not None, semantic_check.issues
    assert all(item.page == 7 for item in semantic_check.proof.evidence)
    assert semantic_check.proof.direct_disclosure_values == (
        "6344125969.00",
        "5626626091.97",
    )

    result = screen_m3_annual_rules(
        facts,
        verifications,
        2024,
        comparability=comparability,
        parent_profit_mapping=semantic_check.proof,
    )

    assert result.status == "completed"
    assert result.total_score == 0
    assert [(item.rule_id, item.status, item.triggered, item.points) for item in result.rules] == [
        (RULE_1, "calculable", False, 0),
        (RULE_2, "calculable", False, 0),
        (RULE_3, "calculable", False, 0),
        (RULE_4, "calculable", False, 0),
    ]
