"""FinancialFactV2 的确定性年度差额与同比。

数值只来自事实的规范值。未核实、追溯状态未知或口径不一致时不产生 succeeded。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Sequence

from finagent.schemas.financial_fact import decimal_to_str
from finagent.schemas.financial_fact_v2 import (
    CALC_FAILED,
    CALC_SUCCEEDED,
    SCOPE_UNKNOWN,
    VERIFIED,
    CalculationResult,
    FinancialFactV2,
    VerificationResult,
)
from finagent.verification.comparability import (
    AnnualComparabilityCheck,
    comparability_mismatch_reason,
)

RULE_VERSION = "v2-annual-3"
FORMULA_DIFFERENCE = "annual_difference"
FORMULA_YOY = "annual_yoy_rate"
DIFFERENCE_EXPRESSION = "current.normalized_value - comparative.normalized_value"
YOY_EXPRESSION = (
    "(current.normalized_value - comparative.normalized_value) / comparative.normalized_value"
    "；除法使用 Decimal，精度为 max(28, 两边有效数字位数之和再加 8)，结果不是无限位精确分数"
)


@dataclass(frozen=True, slots=True)
class V2CalculationIssue:
    """一项不能成功计算的原因。"""

    metric_id: str | None
    formula_id: str | None
    code: str
    message: str


def calculate_v2_annual_changes(
    facts: Sequence[FinancialFactV2],
    verifications: Sequence[VerificationResult],
    report_year: int,
    *,
    comparability: AnnualComparabilityCheck | None = None,
) -> tuple[tuple[CalculationResult, ...], tuple[V2CalculationIssue, ...]]:
    """按 metric_id 配对报告年与上一年度。缺失不当作零。

    旧三参调用仍可使用，但没有从来源 PDF 独立核验的可比性证明时，年度计算失败。
    """

    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise ValueError("report_year 必须是 1900 到 2100 之间的整数。")
    items = _facts(facts)
    checks = _verifications(verifications)
    results: list[CalculationResult] = []
    issues: list[V2CalculationIssue] = []
    seen: dict[str, FinancialFactV2] = {}
    blocked: set[str] = set()
    for fact in items:
        if fact.fact_id in seen:
            blocked.add(fact.fact_id)
            _fail_pair(
                results,
                issues,
                fact.metric_id,
                (fact.fact_id,),
                "duplicate_fact_id",
                f"{fact.fact_id} 重复，不能计算。",
            )
            continue
        seen[fact.fact_id] = fact
    grouped: dict[str, list[FinancialFactV2]] = defaultdict(list)
    for fact in seen.values():
        if fact.fact_id in blocked:
            continue
        grouped[fact.metric_id].append(fact)
    for metric_id in sorted(grouped):
        _pair(metric_id, grouped[metric_id], checks, report_year, comparability, results, issues)
    return tuple(results), tuple(issues)


def _pair(
    metric_id: str,
    facts: list[FinancialFactV2],
    checks: dict[str, list[VerificationResult]],
    report_year: int,
    comparability: AnnualComparabilityCheck | None,
    results: list[CalculationResult],
    issues: list[V2CalculationIssue],
) -> None:
    current = [
        fact
        for fact in facts
        if fact.comparison_role == "current" and _year(fact) == report_year
    ]
    prior = [
        fact
        for fact in facts
        if fact.comparison_role == "comparative" and _year(fact) == report_year - 1
    ]
    if len(current) != 1 or len(prior) != 1:
        ids = tuple(fact.fact_id for fact in (*current, *prior))
        if len(current) == 0 or len(prior) == 0:
            code = "missing_fact"
            message = f"{metric_id} 缺少 {report_year} 或 {report_year - 1} 的年度事实，缺失不当作零。"
        else:
            code = "duplicate_fact"
            message = f"{metric_id} 在同一年度角色上有重复事实。"
        _fail_pair(results, issues, metric_id, ids, code, message)
        return
    left, right = current[0], prior[0]
    ids = (left.fact_id, right.fact_id)
    reason = _compatible(left, right, report_year)
    if reason is not None:
        _fail_pair(results, issues, metric_id, ids, "incompatible_facts", reason)
        return
    verify_reason = _verified(left, checks) or _verified(right, checks)
    if verify_reason is not None:
        _fail_pair(results, issues, metric_id, ids, "verification", verify_reason)
        return
    comparability_reason = comparability_mismatch_reason(
        comparability,
        document_id=left.source_document_id,
        source_sha256=left.source_sha256,
        report_year=report_year,
    )
    if comparability_reason is not None:
        _fail_pair(
            results,
            issues,
            metric_id,
            ids,
            "comparability",
            "追溯调整状态未知，不能当作可比；缺少有效年度可比性证明，不能产生已成功的同比或已核实差额："
            + comparability_reason,
        )
        return
    if left.restatement_status == "restated" or right.restatement_status == "restated":
        _fail_pair(
            results,
            issues,
            metric_id,
            ids,
            "restatement_mismatch",
            "事实字段与核验通过的 not_restated 可比性证明冲突。",
        )
        return
    difference = Decimal(left.normalized_value) - Decimal(right.normalized_value)
    _succeeded(results, metric_id, FORMULA_DIFFERENCE, ids, DIFFERENCE_EXPRESSION, difference, report_year)
    prior_value = Decimal(right.normalized_value)
    if prior_value <= 0:
        message = (
            "上期规范值为零，不输出通常意义的同比增速。"
            if prior_value == 0
            else "上期规范值为负，不输出通常意义的同比增速。"
        )
        _failed(results, issues, metric_id, FORMULA_YOY, ids, "nonpositive_base", message)
        return
    _succeeded(results, metric_id, FORMULA_YOY, ids, YOY_EXPRESSION, _divide(difference, prior_value), report_year)


def _compatible(current: FinancialFactV2, prior: FinancialFactV2, report_year: int) -> str | None:
    if current.period_type != "duration" or prior.period_type != "duration":
        return "period_type 必须是 duration，两个 instant 不能计算同比。"
    if current.report_year != report_year or prior.report_year != report_year:
        return "两条事实的 report_year 必须都是本次报告年。"
    if current.comparison_role != "current" or _year(current) != report_year:
        return "current 事实的期间结束年必须等于报告年。"
    if prior.comparison_role != "comparative" or _year(prior) != report_year - 1:
        return "comparative 事实的期间结束年必须等于上一自然年。"
    if not _natural_year(current) or not _natural_year(prior):
        return "期间必须覆盖完整自然年，不能只凭 frequency。"
    if current.frequency != "annual" or prior.frequency != "annual":
        return "frequency 不是 annual。"
    checks = (
        ("company_id", current.company_id != prior.company_id),
        ("source_document_id", current.source_document_id != prior.source_document_id),
        ("source_sha256", current.source_sha256 != prior.source_sha256),
        ("currency", current.currency != prior.currency),
        ("scope", current.scope != prior.scope),
        ("unit", current.unit != prior.unit),
        ("unit_multiplier", Decimal(current.unit_multiplier) != Decimal(prior.unit_multiplier)),
    )
    failed = [name for name, bad in checks if bad]
    if failed:
        return "两年事实的" + "、".join(failed) + "不一致。"
    if current.scope == SCOPE_UNKNOWN and current.metric_id != "non_recurring_total":
        return "普通利润、收入或现金流等指标的报表口径未知，不能计算年度差额或同比。"
    return None


def _natural_year(fact: FinancialFactV2) -> bool:
    if fact.period_start is None or fact.period_end is None:
        return False
    return (
        fact.period_start == fact.period_end.replace(month=1, day=1)
        and fact.period_end.month == 12
        and fact.period_end.day == 31
    )


def _verified(fact: FinancialFactV2, checks: dict[str, list[VerificationResult]]) -> str | None:
    found = [item for item in checks.get(fact.fact_id, []) if item.target_type == "financial_fact"]
    if len(found) != 1:
        return f"{fact.fact_id} 缺少唯一的 financial_fact 语义核验。"
    if found[0].status != VERIFIED:
        return f"{fact.fact_id} 的语义核验不是 verified。"
    return None


def _year(fact: FinancialFactV2) -> int | None:
    if fact.period_end is None:
        return None
    return fact.period_end.year


def _facts(facts: Sequence[FinancialFactV2]) -> tuple[FinancialFactV2, ...]:
    if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
        raise ValueError("facts 必须是 FinancialFactV2 序列。")
    items = tuple(facts)
    if any(not isinstance(item, FinancialFactV2) for item in items):
        raise ValueError("facts 必须是 FinancialFactV2 序列。")
    return items


def _verifications(verifications: Sequence[VerificationResult]) -> dict[str, list[VerificationResult]]:
    if isinstance(verifications, (str, bytes)) or not isinstance(verifications, Sequence):
        raise ValueError("verifications 必须是 VerificationResult 序列。")
    grouped: dict[str, list[VerificationResult]] = defaultdict(list)
    for item in verifications:
        if not isinstance(item, VerificationResult):
            raise ValueError("verifications 必须是 VerificationResult 序列。")
        grouped[item.target_id].append(item)
    return grouped


def _fail_pair(
    results: list[CalculationResult],
    issues: list[V2CalculationIssue],
    metric_id: str,
    fact_ids: tuple[str, ...],
    code: str,
    message: str,
) -> None:
    for formula_id in (FORMULA_DIFFERENCE, FORMULA_YOY):
        _failed(results, issues, metric_id, formula_id, fact_ids, code, message)


def _failed(
    results: list[CalculationResult],
    issues: list[V2CalculationIssue],
    metric_id: str,
    formula_id: str,
    fact_ids: tuple[str, ...],
    code: str,
    message: str,
) -> None:
    results.append(
        CalculationResult(
            calculation_id=f"calc:{formula_id}:{metric_id}",
            formula_id=formula_id,
            input_fact_ids=fact_ids,
            formula_expression=DIFFERENCE_EXPRESSION if formula_id == FORMULA_DIFFERENCE else YOY_EXPRESSION,
            output_value=None,
            unit=None,
            status=CALC_FAILED,
            failure_reason=message,
            rule_version=RULE_VERSION,
        )
    )
    issues.append(V2CalculationIssue(metric_id, formula_id, code, message))


def _succeeded(
    results: list[CalculationResult],
    metric_id: str,
    formula_id: str,
    fact_ids: tuple[str, ...],
    expression: str,
    value: Decimal,
    report_year: int,
) -> None:
    del report_year
    results.append(
        CalculationResult(
            calculation_id=f"calc:{formula_id}:{metric_id}",
            formula_id=formula_id,
            input_fact_ids=fact_ids,
            formula_expression=expression,
            output_value=decimal_to_str(value),
            unit=None,
            status=CALC_SUCCEEDED,
            failure_reason=None,
            rule_version=RULE_VERSION,
        )
    )


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    digits = len(numerator.as_tuple().digits) + len(denominator.as_tuple().digits) + 8
    with localcontext() as ctx:
        ctx.prec = max(28, digits)
        return numerator / denominator


