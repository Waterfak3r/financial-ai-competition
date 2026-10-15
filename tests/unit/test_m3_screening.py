"""M3 四条规则的边界、失败关闭和专属语义映射。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from finagent.finance.m3_screening import (
    RULE_1,
    RULE_2,
    RULE_3,
    RULE_4,
    RULE_VERSION,
    screen_m3_annual_rules,
)
from finagent.schemas.financial_fact_v2 import FinancialFactV2, VerificationResult
from finagent.verification.comparability import verify_annual_comparability
from finagent.verification.m3_semantic_mapping import (
    ParentProfitSemanticMapping,
    is_verified_parent_profit_semantic_mapping,
    verify_parent_profit_semantic_mapping,
)

_REPORT_YEAR = 2024
_KEY_LABEL = "归属于上市公司股东的扣除非经常性损益的净利润"
_PARENT_LABEL = "归属于母公司股东的净利润"


def _fact(
    metric: str,
    year: int,
    value: str,
    *,
    scope: str = "consolidated",
    statement: str = "income_statement",
    period_type: str = "duration",
    role: str | None = None,
    fact_id: str | None = None,
    label: str | None = None,
    source_document_id: str = "m3-synthetic",
    source_sha256: str,
    unit: str = "元",
    currency: str = "人民币",
    multiplier: str = "1",
    company_id: str = "603288",
    report_year: int = _REPORT_YEAR,
) -> FinancialFactV2:
    resolved_role = role or ("current" if year == report_year else "comparative")
    resolved_label = label or {
        "net_profit_parent": _PARENT_LABEL,
        "net_profit_parent_ex_nonrecurring": _KEY_LABEL,
    }.get(metric, metric)
    return FinancialFactV2(
        fact_id=fact_id or f"{metric}-{year}",
        metric_id=metric,
        company_id=company_id,
        label_raw=resolved_label,
        raw_value=value,
        normalized_value=value,
        currency=currency,
        unit_multiplier=multiplier,
        unit=unit,
        report_year=report_year,
        period_start=date(year, 1, 1) if period_type == "duration" else None,
        period_end=date(year, 12, 31),
        period_type=period_type,  # type: ignore[arg-type]
        frequency="annual",
        statement_type=statement,
        scope=scope,  # type: ignore[arg-type]
        comparison_role=resolved_role,  # type: ignore[arg-type]
        restatement_status="unknown",
        source_document_id=source_document_id,
        source_sha256=source_sha256,
        evidence_ids=(f"extract-{fact_id or metric + '-' + str(year)}",),
        extraction_method="constructed_test_fact",
    )



def _verified(fact: FinancialFactV2, *, status: str = "verified") -> VerificationResult:
    return VerificationResult(
        verification_id=f"verified-{fact.fact_id}",
        target_type="financial_fact",
        target_id=fact.fact_id,
        status=status,  # type: ignore[arg-type]
        checks=("raw_pdf_row_amount_period_unit_scope",),
        conflicts=(),
        evidence_ids=(f"independent-{fact.fact_id}",) if status == "verified" else (),
        limitations=(),
        verified_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )


def _write_context_pdf(path: Path, *, direct_profit_current: str = "200.00", direct_profit_prior: str = "100.00") -> str:
    document = pymupdf.open()

    page = document.new_page()
    _text(page, 70, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")
    _text(page, 70, 110, "(1).重要会计政策变更")
    _text(page, 70, 130, "□适用 √不适用")
    _text(page, 70, 160, "(2).重要会计估计变更")
    _text(page, 70, 180, "□适用 √不适用")
    _text(page, 70, 210, "(3).2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 70, 230, "□适用 √不适用")

    page = document.new_page()
    _text(page, 70, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")
    disclosures = (
        "1、由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润0元。",
        "2、由于会计政策变更，影响期初未分配利润0元。",
        "3、由于重大会计差错更正，影响期初未分配利润0元。",
        "4、由于同一控制导致的合并范围变更，影响期初未分配利润0元。",
        "5、其他调整合计影响期初未分配利润0元。",
    )
    for index, disclosure in enumerate(disclosures):
        _text(page, 60, 110 + index * 20, disclosure, fontsize=9)

    page = document.new_page()
    _text(page, 70, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")
    _text(page, 70, 620, "十八、其他重要事项")
    _text(page, 70, 640, "1、前期会计差错更正")
    _text(page, 70, 660, "(1).追溯重述法")
    _text(page, 70, 680, "□适用 √不适用")

    for _ in range(3):
        page = document.new_page()
        _text(page, 70, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")

    page = document.new_page()
    _text(page, 70, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")
    _text(page, 70, 177, "七、近三年主要会计数据和财务指标")
    _text(page, 70, 200, "(一) 主要会计数据")
    _text(page, 390, 230, "单位：元  币种：人民币")
    _text(page, 66, 249, "主要会计数据")
    _text(page, 199, 249, "2024年")
    _text(page, 301, 249, "2023年")
    _text(page, 487, 249, "2022年")
    _text(page, 40, 296, "归属于上市公司股东的净")
    _text(page, 40, 314, "利润")
    _text(page, 178, 305, direct_profit_current)
    _text(page, 278, 305, direct_profit_prior)
    _text(page, 422, 305, "100.00")
    _text(page, 464, 305, "80.00")
    _text(page, 40, 332, "归属于上市公司股东的扣")
    _text(page, 40, 350, "除非经常性损益的净利润")
    _text(page, 178, 341, "140.00")
    _text(page, 278, 341, "70.00")
    _text(page, 422, 341, "100.00")
    _text(page, 464, 341, "60.00")

    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(page, x: int, y: int, content: str, *, fontsize: int = 10) -> None:
    page.insert_text((x, y), content, fontname="china-s", fontsize=fontsize)


def _write_key_data_table(
    page,
    *,
    report_year: int,
    direct_current: str,
    direct_prior: str,
    unit_line: str = "单位：元  币种：人民币",
    omit_direct_prior: bool = False,
) -> None:
    _text(page, 70, 177, "近三年主要会计数据和财务指标")
    _text(page, 70, 200, "(一) 主要会计数据")
    _text(page, 390, 230, unit_line)
    _text(page, 66, 249, "主要会计数据")
    _text(page, 199, 249, f"{report_year}年")
    _text(page, 301, 249, f"{report_year - 1}年")
    _text(page, 487, 249, f"{report_year - 2}年")
    _text(page, 40, 296, "归属于上市公司股东的净")
    _text(page, 40, 314, "利润")
    _text(page, 178, 305, direct_current)
    if not omit_direct_prior:
        _text(page, 278, 305, direct_prior)
    _text(page, 422, 305, "100.00")
    _text(page, 464, 305, "80.00")
    _text(page, 40, 332, "归属于上市公司股东的扣")
    _text(page, 40, 350, "除非经常性损益的净利润")
    _text(page, 178, 341, "140.00")
    _text(page, 278, 341, "70.00")
    _text(page, 422, 341, "100.00")
    _text(page, 464, 341, "60.00")
    _text(page, 70, 430, "(二) 主要财务指标")


def _write_mapping_pdf(
    path: Path,
    *,
    report_year: int = 2025,
    leading_blank_pages: int = 2,
    direct_current: str = "200.00",
    direct_prior: str = "100.00",
    unit_line: str = "单位：元  币种：人民币",
    duplicate_table: bool = False,
    incomplete_second_section: bool = False,
) -> str:
    document = pymupdf.open()
    for _ in range(leading_blank_pages):
        document.new_page()
    page = document.new_page()
    _write_key_data_table(
        page,
        report_year=report_year,
        direct_current=direct_current,
        direct_prior=direct_prior,
        unit_line=unit_line,
    )
    if duplicate_table or incomplete_second_section:
        extra = document.new_page()
        _write_key_data_table(
            extra,
            report_year=report_year,
            direct_current=direct_current,
            direct_prior=direct_prior,
            unit_line=unit_line,
            omit_direct_prior=incomplete_second_section,
        )
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping_facts(sha256: str, *, company_id: str, report_year: int) -> list[FinancialFactV2]:
    return [
        _fact(
            "net_profit_parent",
            report_year,
            "200",
            source_sha256=sha256,
            company_id=company_id,
            report_year=report_year,
        ),
        _fact(
            "net_profit_parent",
            report_year - 1,
            "100",
            source_sha256=sha256,
            company_id=company_id,
            report_year=report_year,
        ),
        _fact(
            "net_profit_parent_ex_nonrecurring",
            report_year,
            "140",
            scope="unknown",
            statement="key_financial_data",
            source_sha256=sha256,
            company_id=company_id,
            report_year=report_year,
        ),
        _fact(
            "net_profit_parent_ex_nonrecurring",
            report_year - 1,
            "70",
            scope="unknown",
            statement="key_financial_data",
            source_sha256=sha256,
            company_id=company_id,
            report_year=report_year,
        ),
    ]


@pytest.fixture
def screening_context(tmp_path: Path):
    pdf_path = tmp_path / "m3-synthetic.pdf"
    sha256 = _write_context_pdf(pdf_path)
    facts = [
        _fact("net_profit_consolidated", 2024, "100", source_sha256=sha256),
        _fact("net_profit_consolidated", 2023, "80", source_sha256=sha256),
        _fact("operating_cash_flow", 2024, "-5", statement="cash_flow_statement", source_sha256=sha256),
        _fact("operating_cash_flow", 2023, "3", statement="cash_flow_statement", source_sha256=sha256),
        _fact("net_profit_parent", 2024, "200", source_sha256=sha256),
        _fact("net_profit_parent", 2023, "100", source_sha256=sha256),
        _fact(
            "net_profit_parent_ex_nonrecurring",
            2024,
            "140",
            scope="unknown",
            statement="key_financial_data",
            source_sha256=sha256,
        ),
        _fact(
            "net_profit_parent_ex_nonrecurring",
            2023,
            "70",
            scope="unknown",
            statement="key_financial_data",
            source_sha256=sha256,
        ),
        _fact("accounts_receivable_net", 2024, "140", scope="consolidated", statement="balance_sheet", period_type="instant", source_sha256=sha256),
        _fact("accounts_receivable_net", 2023, "100", scope="consolidated", statement="balance_sheet", period_type="instant", source_sha256=sha256),
        _fact("revenue", 2024, "150", source_sha256=sha256),
        _fact("revenue", 2023, "100", source_sha256=sha256),
        _fact("inventory_net", 2024, "140", scope="consolidated", statement="balance_sheet", period_type="instant", source_sha256=sha256),
        _fact("inventory_net", 2023, "100", scope="consolidated", statement="balance_sheet", period_type="instant", source_sha256=sha256),
        _fact("cost_of_goods_sold", 2024, "120", source_sha256=sha256),
        _fact("cost_of_goods_sold", 2023, "100", source_sha256=sha256),
    ]
    verifications = [_verified(fact) for fact in facts]
    comparability = verify_annual_comparability(
        pdf_path,
        document_id="m3-synthetic",
        source_sha256=sha256,
        report_year=_REPORT_YEAR,
    )
    assert comparability.status == "verified", comparability.limitations
    mapping_check = verify_parent_profit_semantic_mapping(
        pdf_path,
        key_facts=[fact for fact in facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in facts if fact.metric_id == "net_profit_parent"],
        verifications=verifications,
        report_year=_REPORT_YEAR,
    )
    assert mapping_check.proof is not None, mapping_check.issues
    return pdf_path, facts, verifications, comparability, mapping_check.proof


def test_four_rules_use_exact_boundary_scores_and_explicit_trial_version(screening_context) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context

    result = screen_m3_annual_rules(
        facts,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )

    assert result.status == "completed"
    assert result.rule_version == RULE_VERSION == "m3-four-annual-rules-v1.0.0-trial"
    assert result.total_score == 75
    assert [(rule.rule_id, rule.triggered, rule.points) for rule in result.rules] == [
        (RULE_1, True, 25),
        (RULE_2, True, 25),  # abs(200 - 140) / 200 == 0.30
        (RULE_3, False, 0),  # 40% - 50% < 20 percentage points
        (RULE_4, True, 25),  # 40% - 20% == 20 percentage points
    ]
    assert Decimal(result.rules[1].calculated_value) == Decimal("0.30")
    assert result.rules[3].threshold == "0.20"
    assert Decimal(result.rules[3].calculated_value) == Decimal("0.20")
    assert "不构成确认舞弊" in result.limitations[-1]
    assert result.to_dict()["total_score"] == 75


def test_growth_gap_includes_exact_twenty_percentage_point_boundary(screening_context) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context
    adjusted = [
        replace(fact, normalized_value="120")
        if fact.metric_id == "revenue" and fact.period_end.year == 2024
        else fact
        for fact in facts
    ]

    result = screen_m3_annual_rules(
        adjusted,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )

    assert result.status == "completed"
    rule_3 = next(item for item in result.rules if item.rule_id == RULE_3)
    assert rule_3.status == "calculable"
    assert rule_3.triggered is True
    assert Decimal(rule_3.calculated_value) == Decimal("0.2")


def test_missing_comparability_or_unverified_fact_makes_total_unknown(screening_context) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context

    no_comparability = screen_m3_annual_rules(
        facts,
        verifications,
        _REPORT_YEAR,
        comparability=None,
        parent_profit_mapping=mapping,
    )
    assert no_comparability.total_score is None
    assert no_comparability.status == "abstained"
    assert all(item.status == "abstained" for item in no_comparability.rules)

    bad_verifications = [
        replace(check, status="insufficient_evidence", evidence_ids=())
        if check.target_id == "accounts_receivable_net-2024"
        else check
        for check in verifications
    ]
    unverified = screen_m3_annual_rules(
        facts,
        bad_verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )
    assert unverified.total_score is None
    rule_3 = next(item for item in unverified.rules if item.rule_id == RULE_3)
    assert rule_3.status == "abstained"
    assert "verification_unconfirmed" in {issue.code for issue in rule_3.issues}


def test_extraction_evidence_cannot_be_reused_as_independent_verification(screening_context) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context
    selected = next(
        fact
        for fact in facts
        if fact.metric_id == "accounts_receivable_net" and fact.comparison_role == "current"
    )
    overlapping_verifications = [
        replace(check, evidence_ids=(selected.evidence_ids[0], "independent-also"))
        if check.target_id == selected.fact_id
        else check
        for check in verifications
    ]

    result = screen_m3_annual_rules(
        facts,
        overlapping_verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )

    assert result.total_score is None
    rule_3 = next(item for item in result.rules if item.rule_id == RULE_3)
    assert rule_3.status == "abstained"
    assert "verification_evidence_reused" in {issue.code for issue in rule_3.issues}


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("missing", "verification_missing"),
        ("conflict", "verification_unconfirmed"),
        ("duplicate", "verification_duplicate"),
        ("serialized", "verification_missing"),
    ),
)
def test_nonverified_or_ambiguous_verification_results_abstain(
    screening_context, mutation: str, expected_code: str
) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context
    target_id = "accounts_receivable_net-2024"
    changed = list(verifications)
    match = next(item for item in verifications if item.target_id == target_id)
    if mutation == "missing":
        changed.remove(match)
    elif mutation == "conflict":
        changed = [replace(item, status="conflict", evidence_ids=()) if item is match else item for item in changed]
    elif mutation == "duplicate":
        changed.append(match)
    elif mutation == "serialized":
        changed = [item.to_dict() if item is match else item for item in changed]  # type: ignore[list-item]

    result = screen_m3_annual_rules(
        facts,
        changed,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )

    assert result.total_score is None
    rule_3 = next(item for item in result.rules if item.rule_id == RULE_3)
    assert rule_3.status == "abstained"
    assert expected_code in {issue.code for issue in rule_3.issues}


@pytest.mark.parametrize(
    ("mutation", "expected_rule", "expected_code"),
    (
        ("duplicate", RULE_3, "duplicate_fact"),
        ("unit", RULE_3, "basis_mismatch"),
        ("source", RULE_4, "basis_mismatch"),
        ("scope", RULE_3, "scope_mismatch"),
        ("period", RULE_4, "period_role_mismatch"),
        ("currency", RULE_2, "basis_mismatch"),
        ("company", RULE_4, "basis_mismatch"),
        ("missing", RULE_3, "missing_fact"),
        ("restated", RULE_1, "restatement_conflict"),
    ),
)
def test_fact_ambiguity_or_incompatible_metadata_fails_closed(
    screening_context, mutation: str, expected_rule: str, expected_code: str
) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context
    changed = list(facts)
    if mutation == "duplicate":
        duplicate = next(fact for fact in facts if fact.metric_id == "accounts_receivable_net" and fact.comparison_role == "current")
        changed.append(duplicate)
    elif mutation == "unit":
        changed = [
            replace(fact, unit="万元") if fact.metric_id == "revenue" and fact.comparison_role == "current" else fact
            for fact in facts
        ]
    elif mutation == "source":
        changed = [
            replace(fact, source_document_id="other-document")
            if fact.metric_id == "cost_of_goods_sold" and fact.comparison_role == "comparative"
            else fact
            for fact in facts
        ]
    elif mutation == "scope":
        changed = [
            replace(fact, scope="unknown")
            if fact.metric_id == "accounts_receivable_net" and fact.comparison_role == "current"
            else fact
            for fact in facts
        ]
    elif mutation == "period":
        changed = [
            replace(fact, period_end=date(2024, 12, 31))
            if fact.metric_id == "cost_of_goods_sold" and fact.comparison_role == "comparative"
            else fact
            for fact in facts
        ]
    elif mutation == "currency":
        changed = [
            replace(fact, currency="美元")
            if fact.metric_id == "net_profit_parent_ex_nonrecurring" and fact.comparison_role == "current"
            else fact
            for fact in facts
        ]
    elif mutation == "company":
        changed = [
            replace(fact, company_id="000001")
            if fact.metric_id == "cost_of_goods_sold" and fact.comparison_role == "comparative"
            else fact
            for fact in facts
        ]
    elif mutation == "missing":
        changed = [
            fact
            for fact in facts
            if not (fact.metric_id == "accounts_receivable_net" and fact.comparison_role == "comparative")
        ]
    elif mutation == "restated":
        changed = [
            replace(fact, restatement_status="restated")
            if fact.metric_id == "net_profit_consolidated" and fact.comparison_role == "current"
            else fact
            for fact in facts
        ]
    result = screen_m3_annual_rules(
        changed,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )
    assert result.total_score is None
    rule = next(item for item in result.rules if item.rule_id == expected_rule)
    assert rule.status == "abstained"
    assert expected_code in {issue.code for issue in rule.issues}


@pytest.mark.parametrize(
    ("metric", "rule_id"),
    (("accounts_receivable_net", RULE_3), ("inventory_net", RULE_4)),
)
def test_nonpositive_comparative_base_abstains_from_growth_rule(screening_context, metric: str, rule_id: str) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context
    changed = [
        replace(fact, normalized_value="0")
        if fact.metric_id == metric and fact.comparison_role == "comparative"
        else fact
        for fact in facts
    ]
    result = screen_m3_annual_rules(
        changed,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )
    assert result.total_score is None
    rule = next(item for item in result.rules if item.rule_id == rule_id)
    assert rule.status == "abstained"
    assert "nonpositive_base" in {issue.code for issue in rule.issues}


@pytest.mark.parametrize(
    ("metric", "rule_id"),
    (
        ("revenue", RULE_3),
        ("cost_of_goods_sold", RULE_4),
    ),
)
def test_nonpositive_income_statement_base_also_abstains(screening_context, metric: str, rule_id: str) -> None:
    _pdf_path, facts, verifications, comparability, mapping = screening_context
    changed = [
        replace(fact, normalized_value="0")
        if fact.metric_id == metric and fact.comparison_role == "comparative"
        else fact
        for fact in facts
    ]

    result = screen_m3_annual_rules(
        changed,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )

    assert result.total_score is None
    rule = next(item for item in result.rules if item.rule_id == rule_id)
    assert rule.status == "abstained"
    assert "nonpositive_base" in {issue.code for issue in rule.issues}


def test_unknown_scope_key_fact_needs_signed_exact_semantic_mapping(screening_context) -> None:
    pdf_path, facts, verifications, comparability, mapping = screening_context
    forged = replace(mapping, key_fact_values=("999", "70"))
    assert not is_verified_parent_profit_semantic_mapping(forged)

    no_mapping = screen_m3_annual_rules(
        facts,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=None,
    )
    assert no_mapping.total_score is None
    rule_2 = next(item for item in no_mapping.rules if item.rule_id == RULE_2)
    assert "semantic_mapping" in {issue.code for issue in rule_2.issues}

    mismatched = screen_m3_annual_rules(
        facts,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=forged,
    )
    assert mismatched.total_score is None
    assert "semantic_mapping" in {issue.code for issue in next(item for item in mismatched.rules if item.rule_id == RULE_2).issues}

    key_pair = [fact for fact in facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"]
    wrong_label = [replace(key_pair[0], label_raw="扣非净利润"), key_pair[1]]
    proof_check = verify_parent_profit_semantic_mapping(
        pdf_path,
        key_facts=wrong_label,
        parent_facts=[fact for fact in facts if fact.metric_id == "net_profit_parent"],
        verifications=verifications,
        report_year=_REPORT_YEAR,
    )
    assert proof_check.proof is None
    assert any("标签必须完整且精确" in issue for issue in proof_check.issues)


def test_rule_two_mapping_discovers_unique_table_for_other_company_year_and_page(
    tmp_path: Path, screening_context
) -> None:
    pdf_path, facts, verifications, comparability, mapping = screening_context
    other_company = [replace(fact, company_id="000001") for fact in facts]
    check = verify_parent_profit_semantic_mapping(
        pdf_path,
        key_facts=[fact for fact in other_company if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in other_company if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact) for fact in other_company],
        report_year=_REPORT_YEAR,
    )
    assert check.proof is not None, check.issues
    assert check.proof.company_id == "000001"
    assert all(item.page == 7 for item in check.proof.evidence)

    result = screen_m3_annual_rules(
        facts,
        verifications,
        _REPORT_YEAR,
        comparability=comparability,
        parent_profit_mapping=mapping,
    )
    assert "第 7 页" not in "".join(result.limitations)
    assert "603288" not in result.limitations[1]

    path = tmp_path / "mapping-page3-2025.pdf"
    sha = _write_mapping_pdf(path, report_year=2025, leading_blank_pages=2)
    facts_2025 = _mapping_facts(sha, company_id="600519", report_year=2025)
    check_2025 = verify_parent_profit_semantic_mapping(
        path,
        key_facts=[fact for fact in facts_2025 if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in facts_2025 if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact) for fact in facts_2025],
        report_year=2025,
    )
    assert check_2025.proof is not None, check_2025.issues
    assert check_2025.proof.company_id == "600519"
    assert check_2025.proof.report_year == 2025
    assert all(item.page == 3 for item in check_2025.proof.evidence)
    assert is_verified_parent_profit_semantic_mapping(check_2025.proof)


def test_rule_two_mapping_rejects_ambiguous_wrong_amount_missing_currency_and_bad_source(
    tmp_path: Path, screening_context
) -> None:
    dup_path = tmp_path / "mapping-duplicate.pdf"
    dup_sha = _write_mapping_pdf(dup_path, report_year=2025, duplicate_table=True)
    dup_facts = _mapping_facts(dup_sha, company_id="600519", report_year=2025)
    dup_check = verify_parent_profit_semantic_mapping(
        dup_path,
        key_facts=[fact for fact in dup_facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in dup_facts if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact) for fact in dup_facts],
        report_year=2025,
    )
    assert dup_check.proof is None
    assert any("不唯一" in issue for issue in dup_check.issues)

    incomplete_path = tmp_path / "mapping-second-missing-amount.pdf"
    incomplete_sha = _write_mapping_pdf(incomplete_path, report_year=2025, incomplete_second_section=True)
    incomplete_facts = _mapping_facts(incomplete_sha, company_id="600519", report_year=2025)
    incomplete_check = verify_parent_profit_semantic_mapping(
        incomplete_path,
        key_facts=[fact for fact in incomplete_facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in incomplete_facts if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact) for fact in incomplete_facts],
        report_year=2025,
    )
    assert incomplete_check.proof is None
    assert any("不唯一" in issue for issue in incomplete_check.issues)

    _old_path, facts, _verifications, _comparability, _mapping = screening_context
    bad_path = tmp_path / "m3-wrong-direct-profit.pdf"
    new_hash = _write_context_pdf(bad_path, direct_profit_current="201.00")
    changed_facts = [replace(fact, source_sha256=new_hash) for fact in facts]
    amount_check = verify_parent_profit_semantic_mapping(
        bad_path,
        key_facts=[fact for fact in changed_facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in changed_facts if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact) for fact in changed_facts],
        report_year=_REPORT_YEAR,
    )
    assert amount_check.proof is None
    assert any("至少一年金额不一致" in issue for issue in amount_check.issues)

    no_currency_path = tmp_path / "mapping-no-currency.pdf"
    no_currency_sha = _write_mapping_pdf(no_currency_path, report_year=2025, unit_line="单位：元")
    no_currency_facts = _mapping_facts(no_currency_sha, company_id="600519", report_year=2025)
    currency_check = verify_parent_profit_semantic_mapping(
        no_currency_path,
        key_facts=[fact for fact in no_currency_facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in no_currency_facts if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact) for fact in no_currency_facts],
        report_year=2025,
    )
    assert currency_check.proof is None
    assert any("币种" in issue for issue in currency_check.issues)

    good_path = tmp_path / "mapping-good.pdf"
    good_sha = _write_mapping_pdf(good_path, report_year=2025)
    other_path = tmp_path / "mapping-other.pdf"
    _write_mapping_pdf(other_path, report_year=2025, direct_current="201.00")
    sha_facts = _mapping_facts(good_sha, company_id="600519", report_year=2025)
    sha_check = verify_parent_profit_semantic_mapping(
        other_path,
        key_facts=[fact for fact in sha_facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in sha_facts if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact) for fact in sha_facts],
        report_year=2025,
    )
    assert sha_check.proof is None
    assert any("SHA256" in issue for issue in sha_check.issues)

    unverified_facts = _mapping_facts(good_sha, company_id="600519", report_year=2025)
    unverified_check = verify_parent_profit_semantic_mapping(
        good_path,
        key_facts=[fact for fact in unverified_facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"],
        parent_facts=[fact for fact in unverified_facts if fact.metric_id == "net_profit_parent"],
        verifications=[_verified(fact, status="insufficient_evidence") for fact in unverified_facts],
        report_year=2025,
    )
    assert unverified_check.proof is None
    assert any("未由独立原文核验" in issue for issue in unverified_check.issues)



def test_serialized_semantic_mapping_does_not_authorize_new_proof(screening_context) -> None:
    _pdf_path, _facts, _verifications, _comparability, mapping = screening_context
    audit_copy = mapping.to_dict()
    assert audit_copy["evidence"][0]["page"] == 7
    assert is_verified_parent_profit_semantic_mapping(mapping)
    assert not isinstance(audit_copy, ParentProfitSemanticMapping)
