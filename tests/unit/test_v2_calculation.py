"""v2 年度差额与同比。构造事实，不读取真实财报，也不使用模型数值。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from finagent.finance.v2_calculation import (
    FORMULA_DIFFERENCE,
    FORMULA_YOY,
    calculate_v2_annual_changes,
)
from finagent.finance.v2_screening import screen_v2_annual_candidates
from finagent.schemas.financial_fact_v2 import FinancialFactV2, VerificationResult


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


def _calc(facts, verifications=None):
    if verifications is None:
        verifications = [_ok(fact) for fact in facts]
    return calculate_v2_annual_changes(facts, verifications, 2024)


def _by_formula(results, formula_id):
    return next(item for item in results if item.formula_id == formula_id)


def test_not_restated_pair_has_exact_difference_and_rate() -> None:
    results, issues = _calc(_pair())
    assert issues == ()
    difference = _by_formula(results, FORMULA_DIFFERENCE)
    rate = _by_formula(results, FORMULA_YOY)
    assert difference.status == "succeeded"
    assert difference.output_value == "30"
    assert difference.input_fact_ids == ("cur", "pri")
    assert difference.rule_version
    assert "current.normalized_value" in difference.formula_expression
    assert rate.status == "succeeded"
    assert Decimal(rate.output_value) == Decimal("0.6")
    assert "max(28" in rate.formula_expression


def test_unknown_restatement_does_not_succeed() -> None:
    facts = [
        _fact("cur", "net_profit_parent", 2024, "80", "current", restatement_status="unknown"),
        _fact("pri", "net_profit_parent", 2023, "50", "comparative"),
    ]
    results, issues = _calc(facts)
    assert all(item.status == "failed" for item in results)
    assert any(item.code == "restatement_unknown" for item in issues)
    assert "已核实差额" in issues[0].message


def test_scope_mismatch_fails() -> None:
    facts = [
        _fact("cur", "net_profit_parent", 2024, "80", "current", scope="parent"),
        _fact("pri", "net_profit_parent", 2023, "50", "comparative", scope="consolidated"),
    ]
    results, issues = _calc(facts)
    assert _by_formula(results, FORMULA_DIFFERENCE).status == "failed"
    assert any("scope" in item.message for item in issues)


def test_missing_verification_fails() -> None:
    facts = _pair()
    results, issues = _calc(facts, [_ok(facts[0])])
    assert all(item.status == "failed" for item in results)
    assert any(item.code == "verification" for item in issues)


def test_zero_and_negative_base_keep_difference_without_rate() -> None:
    zero = [
        _fact("cur", "net_profit_parent", 2024, "10", "current"),
        _fact("pri", "net_profit_parent", 2023, "0", "comparative"),
    ]
    results, issues = _calc(zero)
    assert _by_formula(results, FORMULA_DIFFERENCE).output_value == "10"
    assert _by_formula(results, FORMULA_YOY).status == "failed"
    assert any(item.code == "nonpositive_base" for item in issues)
    negative = [
        _fact("cur", "net_profit_parent", 2024, "-5", "current"),
        _fact("pri", "net_profit_parent", 2023, "-10", "comparative"),
    ]
    results, issues = _calc(negative)
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


def test_screening_keeps_exact_differences_and_abstains_without_facts() -> None:
    profit = _pair("net_profit_parent")
    cash = [
        _fact("cash-cur", "operating_cash_flow", 2024, "20", "current"),
        _fact("cash-pri", "operating_cash_flow", 2023, "40", "comparative"),
    ]
    facts = profit + cash
    results, issues = _calc(facts)
    assert issues == ()
    screened = screen_v2_annual_candidates(facts, results)
    profit_signal = next(item for item in screened if item["signal_id"] == "profit_up_cash_down")
    assert profit_signal["status"] == "candidate"
    assert profit_signal["left_difference"] == "30"
    assert profit_signal["right_difference"] == "-20"
    assert profit_signal["input_fact_ids"] == ("cur", "pri", "cash-cur", "cash-pri")
    assert profit_signal["calculation_ids"]
    missing = screen_v2_annual_candidates([], results)
    assert all(item["status"] == "abstained" for item in missing)
    assert "事实不在输入中" in str(missing[0]["reason"])


def test_unknown_scope_same_metric_can_differ_but_screening_abstains() -> None:
    facts = [
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
    ]
    results, issues = _calc(facts)
    assert issues == ()
    assert _by_formula(results, FORMULA_DIFFERENCE).output_value == "2"
    screened = screen_v2_annual_candidates(facts, results)
    assert screened
    assert all(item["status"] == "abstained" for item in screened)
