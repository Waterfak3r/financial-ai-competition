"""v2 年度差额与同比。构造事实，不读取真实财报，也不使用模型数值。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from finagent.finance.v2_calculation import (
    FORMULA_DIFFERENCE,
    FORMULA_YOY,
    RULE_VERSION,
    calculate_v2_annual_changes,
)
from finagent.finance.v2_screening import screen_v2_annual_candidates
from finagent.schemas.financial_fact_v2 import FinancialFactV2, VerificationResult
from finagent.verification.comparability import AnnualComparabilityCheck, verify_annual_comparability


def _fact(fact_id: str, metric: str, year: int, value: str, role: str, **overrides) -> FinancialFactV2:
    payload = dict(
        fact_id=fact_id,
        metric_id=metric,
        company_id="603288",
        label_raw=metric,
        raw_value=value,
        normalized_value=value,
        currency="CNY",
        unit_multiplier="1",
        unit="元",
        report_year=2024,
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        period_type="duration",
        frequency="annual",
        statement_type="income_statement",
        scope="consolidated",
        comparison_role=role,
        restatement_status="not_restated",
        source_document_id="doc-1",
        source_sha256="a" * 64,
        evidence_ids=(f"ev-{fact_id}",),
        extraction_method="test",
    )
    payload.update(overrides)
    return FinancialFactV2(**payload)


def _ok(fact: FinancialFactV2, status: str = "verified") -> VerificationResult:
    return VerificationResult(
        verification_id=f"ver-{fact.fact_id}",
        target_type="financial_fact",
        target_id=fact.fact_id,
        status=status,  # type: ignore[arg-type]
        checks=("semantic",),
        conflicts=(),
        evidence_ids=fact.evidence_ids,
        limitations=(),
        verified_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )


def _pair(metric: str = "net_profit_parent") -> list[FinancialFactV2]:
    return [
        _fact("cur", metric, 2024, "80", "current"),
        _fact("pri", metric, 2023, "50", "comparative"),
    ]


def _calc(facts, verifications=None, comparability=None):
    if verifications is None:
        verifications = [_ok(fact) for fact in facts]
    return calculate_v2_annual_changes(facts, verifications, 2024, comparability=comparability)


def _bind_facts_to_comparability(facts, comparability):
    return [
        replace(
            fact,
            source_document_id=comparability.document_id,
            source_sha256=comparability.source_sha256,
        )
        for fact in facts
    ]


@pytest.fixture
def comparability(tmp_path: Path) -> AnnualComparabilityCheck:
    pdf = tmp_path / "comparability.pdf"
    document = pymupdf.open()

    page = document.new_page()
    _report_header(page)
    _pdf_text(page, 70, 110, "(1).重要会计政策变更")
    _pdf_text(page, 70, 130, "□适用   √不适用")
    _pdf_text(page, 70, 160, "(2).重要会计估计变更")
    _pdf_text(page, 70, 180, "□适用   √不适用")
    _pdf_text(page, 70, 210, "(3).2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _pdf_text(page, 70, 230, "□适用   √不适用")

    page = document.new_page()
    _report_header(page)
    for index, text in enumerate((
        "1、由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润0元。",
        "2、由于会计政策变更，影响期初未分配利润0元。",
        "3、由于重大会计差错更正，影响期初未分配利润0元。",
        "4、由于同一控制导致的合并范围变更，影响期初未分配利润0元。",
        "5、其他调整合计影响期初未分配利润0元。",
    )):
        _pdf_text(page, 70, 110 + index * 20, text, fontsize=9)

    page = document.new_page()
    _report_header(page)
    _pdf_text(page, 70, 620, "十八、其他重要事项")
    _pdf_text(page, 70, 640, "1、前期会计差错更正")
    _pdf_text(page, 70, 660, "(1).追溯重述法")
    _pdf_text(page, 70, 680, "□适用   √不适用")

    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    return verify_annual_comparability(
        pdf,
        document_id="doc-1",
        source_sha256=sha256,
        report_year=2024,
    )


def _report_header(page) -> None:
    _pdf_text(page, 70, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")


def _pdf_text(page, x: int, y: int, text: str, *, fontsize: int = 10) -> None:
    page.insert_text((x, y), text, fontname="china-s", fontsize=fontsize)


def _by_formula(results, formula_id):
    return next(item for item in results if item.formula_id == formula_id)


def test_verified_comparability_pair_has_exact_difference_and_rate(comparability) -> None:
    facts = [replace(fact, restatement_status="unknown") for fact in _bind_facts_to_comparability(_pair(), comparability)]
    results, issues = _calc(facts, comparability=comparability)
    assert issues == ()
    difference = _by_formula(results, FORMULA_DIFFERENCE)
    rate = _by_formula(results, FORMULA_YOY)
    assert difference.status == "succeeded"
    assert difference.output_value == "30"
    assert difference.input_fact_ids == ("cur", "pri")
    assert difference.rule_version == RULE_VERSION == "v2-annual-3"
    assert "current.normalized_value" in difference.formula_expression
    assert rate.status == "succeeded"
    assert Decimal(rate.output_value) == Decimal("0.6")
    assert "max(28" in rate.formula_expression


def test_unknown_restatement_without_proof_does_not_succeed() -> None:
    facts = [
        _fact("cur", "net_profit_parent", 2024, "80", "current", restatement_status="unknown"),
        _fact("pri", "net_profit_parent", 2023, "50", "comparative"),
    ]
    results, issues = _calc(facts)
    assert all(item.status == "failed" for item in results)
    assert any(item.code == "comparability" for item in issues)
    assert "已核实差额" in issues[0].message


def test_hand_flipping_fact_field_does_not_unlock_calculation() -> None:
    unknown = [replace(fact, restatement_status="unknown") for fact in _pair()]
    facts = [replace(fact, restatement_status="not_restated") for fact in unknown]

    results, issues = _calc(facts)

    assert all(item.status == "failed" for item in results)
    assert all(item.code == "comparability" for item in issues)
    assert all("可比性证明" in item.message for item in issues)


def test_modified_comparability_result_cannot_authorize_calculation(comparability) -> None:
    facts = _bind_facts_to_comparability(_pair(), comparability)
    tampered = replace(comparability, limitations=("manually edited",))

    results, issues = _calc(facts, comparability=tampered)

    assert all(item.status == "failed" for item in results)
    assert all(item.code == "comparability" for item in issues)
    assert all("核验函数签发" in item.message for item in issues)


def test_scope_mismatch_fails() -> None:
    facts = [
        _fact("cur", "net_profit_parent", 2024, "80", "current", scope="parent"),
        _fact("pri", "net_profit_parent", 2023, "50", "comparative", scope="consolidated"),
    ]
    results, issues = _calc(facts)
    assert _by_formula(results, FORMULA_DIFFERENCE).status == "failed"
    assert any("scope" in item.message for item in issues)


def test_unknown_scope_is_rejected_for_ordinary_flow_metrics() -> None:
    for metric in ("net_profit_parent", "revenue", "operating_cash_flow"):
        facts = [
            _fact("cur", metric, 2024, "80", "current", scope="unknown"),
            _fact("pri", metric, 2023, "50", "comparative", scope="unknown"),
        ]
        results, issues = _calc(facts)
        assert all(item.status == "failed" for item in results)
        assert any("口径未知" in item.message for item in issues)


def test_missing_verification_fails() -> None:
    facts = _pair()
    results, issues = _calc(facts, [_ok(facts[0])])
    assert all(item.status == "failed" for item in results)
    assert any(item.code == "verification" for item in issues)


def test_zero_and_negative_base_keep_difference_without_rate(comparability) -> None:
    zero = [
        _fact("cur", "net_profit_parent", 2024, "10", "current"),
        _fact("pri", "net_profit_parent", 2023, "0", "comparative"),
    ]
    zero = _bind_facts_to_comparability(zero, comparability)
    results, issues = _calc(zero, comparability=comparability)
    assert _by_formula(results, FORMULA_DIFFERENCE).output_value == "10"
    assert _by_formula(results, FORMULA_YOY).status == "failed"
    assert any(item.code == "nonpositive_base" for item in issues)
    negative = [
        _fact("cur", "net_profit_parent", 2024, "-5", "current"),
        _fact("pri", "net_profit_parent", 2023, "-10", "comparative"),
    ]
    negative = _bind_facts_to_comparability(negative, comparability)
    results, issues = _calc(negative, comparability=comparability)
    assert _by_formula(results, FORMULA_DIFFERENCE).output_value == "5"
    assert _by_formula(results, FORMULA_YOY).output_value is None
    assert "负" in _by_formula(results, FORMULA_YOY).failure_reason


def test_missing_and_duplicate_are_issues() -> None:
    only_current = [_fact("cur", "revenue", 2024, "10", "current")]
    results, issues = _calc(only_current)
    assert any(item.code == "missing_fact" for item in issues)
    assert all(item.output_value is None for item in results)
    duplicated = _pair() + [_fact("cur", "net_profit_parent", 2024, "1", "current")]
    results, issues = _calc(duplicated)
    assert any(item.code == "duplicate_fact_id" for item in issues)
    assert all(item.status == "failed" for item in results)


def test_instant_or_partial_year_or_report_year_mismatch_fails() -> None:
    instants = [
        _fact("cur", "net_profit_parent", 2024, "80", "current", period_type="instant", period_start=None),
        _fact("pri", "net_profit_parent", 2023, "50", "comparative", period_type="instant", period_start=None),
    ]
    results, issues = _calc(instants)
    assert all(item.status == "failed" for item in results)
    assert any("duration" in item.message for item in issues)
    partial = [
        _fact("cur", "net_profit_parent", 2024, "80", "current", period_end=date(2024, 6, 30)),
        _fact("pri", "net_profit_parent", 2023, "50", "comparative"),
    ]
    results, issues = _calc(partial)
    assert any("自然年" in item.message for item in issues)
    wrong_report = [
        _fact("cur", "net_profit_parent", 2024, "80", "current"),
        _fact("pri", "net_profit_parent", 2023, "50", "comparative", report_year=2023),
    ]
    results, issues = _calc(wrong_report)
    assert any("report_year" in item.message for item in issues)


def test_screening_keeps_exact_differences_and_abstains_without_facts(comparability) -> None:
    profit = _pair("net_profit_parent")
    cash = [
        _fact("cash-cur", "operating_cash_flow", 2024, "20", "current"),
        _fact("cash-pri", "operating_cash_flow", 2023, "40", "comparative"),
    ]
    facts = _bind_facts_to_comparability(profit + cash, comparability)
    results, issues = _calc(facts, comparability=comparability)
    assert issues == ()
    screened = screen_v2_annual_candidates(facts, results, comparability=comparability)
    profit_signal = next(item for item in screened if item["signal_id"] == "profit_up_cash_down")
    assert profit_signal["status"] == "candidate"
    assert profit_signal["left_difference"] == "30"
    assert profit_signal["right_difference"] == "-20"
    assert profit_signal["input_fact_ids"] == ("cur", "pri", "cash-cur", "cash-pri")
    assert profit_signal["calculation_ids"]
    no_proof = screen_v2_annual_candidates(facts, results)
    assert all(item["status"] == "abstained" for item in no_proof)
    assert "可比性" in str(no_proof[0]["reason"])
    missing = screen_v2_annual_candidates([], results)
    assert all(item["status"] == "abstained" for item in missing)
    assert "事实不在输入中" in str(missing[0]["reason"])


def test_unknown_scope_same_metric_can_differ_but_screening_abstains(comparability) -> None:
    facts = _bind_facts_to_comparability([
        _fact(
            "cur",
            "non_recurring_total",
            2024,
            "3",
            "current",
            scope="unknown",
            label_raw="披露的非经常性损益合计",
        ),
        _fact(
            "pri",
            "non_recurring_total",
            2023,
            "1",
            "comparative",
            scope="unknown",
            label_raw="披露的非经常性损益合计",
        ),
    ], comparability)
    results, issues = _calc(facts, comparability=comparability)
    assert issues == ()
    assert _by_formula(results, FORMULA_DIFFERENCE).output_value == "2"
    screened = screen_v2_annual_candidates(facts, results, comparability=comparability)
    assert screened
    assert all(item["status"] == "abstained" for item in screened)


def _screening_inputs(comparability):
    facts = _bind_facts_to_comparability(_pair("net_profit_parent") + [
        _fact("cash-cur", "operating_cash_flow", 2024, "20", "current"),
        _fact("cash-pri", "operating_cash_flow", 2023, "40", "comparative"),
    ], comparability)
    calculations, issues = _calc(facts, comparability=comparability)
    assert issues == ()
    return facts, calculations


def _profit_signal(facts, calculations, comparability):
    screened = screen_v2_annual_candidates(facts, calculations, comparability=comparability)
    return next(item for item in screened if item["signal_id"] == "profit_up_cash_down")


def test_screening_abstains_on_cross_company_document_hash_currency_and_period(comparability) -> None:
    facts, calculations = _screening_inputs(comparability)
    mutations = (
        ("company_id", "000001"),
        ("source_document_id", "doc-2"),
        ("source_sha256", "b" * 64),
        ("currency", "USD"),
        ("period_end", date(2024, 6, 30)),
        ("report_year", 2023),
        ("scope", "parent"),
    )
    for field, value in mutations:
        changed = list(facts)
        changed[2] = replace(changed[2], **{field: value})
        signal = _profit_signal(changed, calculations, comparability)
        assert signal["status"] == "abstained", (field, signal)


def test_screening_abstains_on_wrong_period_role_and_metric_attribution(comparability) -> None:
    facts, calculations = _screening_inputs(comparability)
    for field, value in (
        ("comparison_role", "current"),
        ("metric_id", "revenue"),
    ):
        changed = list(facts)
        changed[3] = replace(changed[3], **{field: value})
        signal = _profit_signal(changed, calculations, comparability)
        assert signal["status"] == "abstained", (field, signal)


def test_screening_abstains_when_metric_pairs_have_different_unit_or_scope(comparability) -> None:
    facts, calculations = _screening_inputs(comparability)
    for field, value in (("unit", "万元"), ("unit_multiplier", "10000")):
        changed = list(facts)
        changed[0] = replace(changed[0], **{field: value})
        signal = _profit_signal(changed, calculations, comparability)
        assert signal["status"] == "abstained", (field, signal)

    changed_scope = [
        replace(fact, scope="parent") if fact.fact_id in {"cash-cur", "cash-pri"} else fact
        for fact in facts
    ]
    signal = _profit_signal(changed_scope, calculations, comparability)
    assert signal["status"] == "abstained"
    assert "报表口径不一致" in str(signal["reason"])


def test_screening_abstains_when_difference_does_not_match_fact_values(comparability) -> None:
    facts, calculations = _screening_inputs(comparability)
    changed_calculations = tuple(
        replace(item, output_value="999")
        if item.calculation_id == "calc:annual_difference:net_profit_parent"
        else item
        for item in calculations
    )
    signal = _profit_signal(facts, changed_calculations, comparability)
    assert signal["status"] == "abstained"
    assert "重算结果" in str(signal["reason"])


def test_screening_abstains_on_duplicate_fact_id_without_overwriting(comparability) -> None:
    facts, calculations = _screening_inputs(comparability)
    duplicated = [*facts, _fact("cur", "net_profit_parent", 2024, "999", "current")]
    signal = _profit_signal(duplicated, calculations, comparability)
    assert signal["status"] == "abstained"
    assert "重复 fact_id" in str(signal["reason"])


def test_screening_rechecks_unknown_scope_and_unknown_restatement(comparability) -> None:
    facts, calculations = _screening_inputs(comparability)
    unknown_scope = [replace(fact, scope="unknown") for fact in facts]
    assert _profit_signal(unknown_scope, calculations, comparability)["status"] == "abstained"

    unknown_restatement = [
        replace(fact, restatement_status="unknown") if fact.fact_id == "cash-pri" else fact
        for fact in facts
    ]
    signal = _profit_signal(unknown_restatement, calculations, comparability)
    assert signal["status"] == "candidate"

    no_proof = screen_v2_annual_candidates(unknown_restatement, calculations)
    assert no_proof[0]["status"] == "abstained"
    assert "可比性" in str(no_proof[0]["reason"])
