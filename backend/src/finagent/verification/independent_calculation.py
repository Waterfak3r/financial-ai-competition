"""独立重算受支持的年度差额与同比率。"""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from finagent.schemas.financial_fact_v2 import CalculationResult, FinancialFactV2
from finagent.verification.comparability import (
    AnnualComparabilityCheck,
    comparability_mismatch_reason,
)

# Keep these values pinned here. This verifier must not inherit a changed formula
# or version from the calculation module it checks.
_RULE_VERSION = "v2-annual-3"
_FORMULAS = {
    "annual_difference": "current.normalized_value - comparative.normalized_value",
    "annual_yoy_rate": (
        "(current.normalized_value - comparative.normalized_value) / comparative.normalized_value"
        "；除法使用 Decimal，精度为 max(28, 两边有效数字位数之和再加 8)，结果不是无限位精确分数"
    ),
}


@dataclass(frozen=True, slots=True)
class IndependentCalculationCheck:
    """本进程重新核算得到的结论，不接受调用方传入的 checks 文本。"""

    calculation_id: str
    status: Literal["verified", "conflict", "insufficient_evidence"]
    recomputed_value: str | None
    reason: str | None
    checks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "calculation_id": self.calculation_id,
            "status": self.status,
            "recomputed_value": self.recomputed_value,
            "reason": self.reason,
            "checks": list(self.checks),
        }


def verify_annual_calculation(
    calculation: CalculationResult,
    facts: Mapping[str, FinancialFactV2],
    *,
    verified_fact_ids: Set[str],
    report_year: int,
    comparability: AnnualComparabilityCheck | None,
) -> IndependentCalculationCheck:
    """重算 v2-annual-3 的年度差额或同比率。

    ``verified_fact_ids`` 必须由调用方在检查事实及其来源证据后产生。年度证明
    则由本函数再次按文档、哈希和报告年度绑定检查。该函数不读取计算核验记录、
    ``checks`` 文字或状态说明作为通过依据。
    """

    if not isinstance(calculation, CalculationResult):
        raise TypeError("calculation 必须是 CalculationResult。")
    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise ValueError("report_year 必须是 1900 到 2100 之间的整数。")
    if not isinstance(facts, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, FinancialFactV2)
        for key, value in facts.items()
    ):
        raise ValueError("facts 必须是以 fact_id 为键的 FinancialFactV2 映射。")
    if isinstance(verified_fact_ids, (str, bytes)) or not isinstance(verified_fact_ids, Set):
        raise ValueError("verified_fact_ids 必须是 fact_id 集合。")

    expected_expression = _FORMULAS.get(calculation.formula_id)
    if expected_expression is None:
        return _result(calculation, "insufficient_evidence", "公式不在独立核验器支持范围内。")
    if calculation.status != "succeeded" or calculation.output_value is None:
        return _result(calculation, "insufficient_evidence", "计算没有 succeeded 结果可供独立重算。")
    if calculation.rule_version != _RULE_VERSION:
        return _result(calculation, "conflict", f"rule_version 必须是 {_RULE_VERSION}。")
    if calculation.formula_expression != expected_expression:
        return _result(calculation, "conflict", "公式表达式与受控公式不一致。")
    if calculation.unit is not None:
        return _result(calculation, "conflict", "v2 年度公式结果单位必须为空，实际单位需由输入事实解释。")
    if len(calculation.input_fact_ids) != 2 or len(set(calculation.input_fact_ids)) != 2:
        return _result(calculation, "conflict", "年度计算必须按顺序绑定两个不同的本期与比较期事实。")

    current = facts.get(calculation.input_fact_ids[0])
    comparative = facts.get(calculation.input_fact_ids[1])
    if current is None or comparative is None:
        return _result(calculation, "insufficient_evidence", "计算引用的输入事实不完整。")
    if any(fact_id not in verified_fact_ids for fact_id in calculation.input_fact_ids):
        return _result(calculation, "insufficient_evidence", "输入事实没有全部通过独立事实和证据检查。")
    if current.metric_id != comparative.metric_id:
        return _result(calculation, "conflict", "本期与比较期事实的 metric_id 不一致。")
    expected_id = f"calc:{calculation.formula_id}:{current.metric_id}"
    if calculation.calculation_id != expected_id:
        return _result(calculation, "conflict", "calculation_id 与公式及输入指标不一致。")
    if current.report_year != report_year or comparative.report_year != report_year:
        return _result(calculation, "conflict", "输入事实的 report_year 与报告不一致。")
    if current.comparison_role != "current" or comparative.comparison_role != "comparative":
        return _result(calculation, "conflict", "输入事实顺序必须是 current 后接 comparative。")
    if not _natural_year(current, report_year) or not _natural_year(comparative, report_year - 1):
        return _result(calculation, "conflict", "输入事实必须分别覆盖报告年及上一自然年。")
    if current.period_type != "duration" or comparative.period_type != "duration":
        return _result(calculation, "conflict", "年度差额和同比率只支持 duration 事实。")
    if current.frequency != "annual" or comparative.frequency != "annual":
        return _result(calculation, "conflict", "年度差额和同比率只支持 annual 频率。")

    identity_pairs = (
        ("company_id", current.company_id, comparative.company_id),
        ("来源文档", current.source_document_id, comparative.source_document_id),
        ("来源哈希", current.source_sha256.lower(), comparative.source_sha256.lower()),
        ("币种", current.currency, comparative.currency),
        ("报表口径", current.scope, comparative.scope),
        ("单位", current.unit, comparative.unit),
    )
    mismatched = [name for name, left, right in identity_pairs if left != right]
    if mismatched:
        return _result(calculation, "conflict", "两年输入事实的" + "、".join(mismatched) + "不一致。")
    try:
        if Decimal(current.unit_multiplier) != Decimal(comparative.unit_multiplier):
            return _result(calculation, "conflict", "两年输入事实的 unit_multiplier 不一致。")
        current_value = Decimal(current.normalized_value)
        comparative_value = Decimal(comparative.normalized_value)
    except InvalidOperation:
        return _result(calculation, "conflict", "输入事实含无效的 Decimal 数值。")
    if current.scope == "unknown" and current.metric_id != "non_recurring_total":
        return _result(calculation, "insufficient_evidence", "普通年度指标的报表口径未知。")
    if current.restatement_status == "restated" or comparative.restatement_status == "restated":
        return _result(calculation, "conflict", "事实标记为 restated，与 not_restated 可比性证明冲突。")

    proof_error = comparability_mismatch_reason(
        comparability,
        document_id=current.source_document_id,
        source_sha256=current.source_sha256,
        report_year=report_year,
    )
    if proof_error is not None:
        return _result(calculation, "insufficient_evidence", proof_error)

    difference = current_value - comparative_value
    if calculation.formula_id == "annual_difference":
        expected_value = difference
        checks = ("按两个已核实事实重新计算年度差额。",)
    else:
        if comparative_value <= 0:
            return _result(calculation, "conflict", "比较期数值必须为正数，不能核验同比率。")
        digits = len(difference.as_tuple().digits) + len(comparative_value.as_tuple().digits) + 8
        with localcontext() as context:
            context.prec = max(28, digits)
            expected_value = difference / comparative_value
        checks = ("按两个已核实事实重新计算同比率，采用受控 Decimal 精度。",)
    try:
        supplied_value = Decimal(calculation.output_value)
    except InvalidOperation:
        return _result(calculation, "conflict", "计算输出不是有效的 Decimal 数值。")
    if supplied_value != expected_value:
        return IndependentCalculationCheck(
            calculation.calculation_id,
            "conflict",
            str(expected_value),
            f"计算输出 {calculation.output_value} 与独立重算值 {expected_value} 不一致。",
            checks,
        )
    return IndependentCalculationCheck(
        calculation.calculation_id,
        "verified",
        str(expected_value),
        None,
        checks,
    )


def _natural_year(fact: FinancialFactV2, year: int) -> bool:
    return (
        fact.period_start == date(year, 1, 1)
        and fact.period_end == date(year, 12, 31)
    )


def _result(
    calculation: CalculationResult,
    status: Literal["conflict", "insufficient_evidence"],
    reason: str,
) -> IndependentCalculationCheck:
    return IndependentCalculationCheck(calculation.calculation_id, status, None, reason, ())
