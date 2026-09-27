"""基于 v2 年度差额的候选线索。

筛查会重新检查差额引用的事实身份、年度可比性和规范值计算。输入 CalculationResult
只作为待复核数据，不能单凭 succeeded 状态形成候选。
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Sequence

from finagent.finance.v2_calculation import (
    DIFFERENCE_EXPRESSION,
    FORMULA_DIFFERENCE,
    RULE_VERSION,
)
from finagent.schemas.financial_fact_v2 import CalculationResult, FinancialFactV2
from finagent.verification.comparability import (
    AnnualComparabilityCheck,
    comparability_mismatch_reason,
)

PROFIT = "net_profit_parent"
CASH = "operating_cash_flow"
REVENUE = "revenue"


def screen_v2_annual_candidates(
    facts: Sequence[FinancialFactV2],
    calculations: Sequence[CalculationResult],
    *,
    comparability: AnnualComparabilityCheck | None = None,
) -> tuple[dict[str, object], ...]:
    """核对完整输入身份、年度可比性证明与差额后，再给出候选或弃权。"""

    fact_items = tuple(facts)
    calculation_items = tuple(calculations)
    counts = Counter(fact.fact_id for fact in fact_items)
    duplicate_fact_ids = {fact_id for fact_id, count in counts.items() if count > 1}
    by_id = {fact.fact_id: fact for fact in fact_items if counts[fact.fact_id] == 1}

    signals = (
        ("profit_up_cash_down", PROFIT, CASH),
        ("revenue_up_cash_down", REVENUE, CASH),
    )
    output: list[dict[str, object]] = []
    for signal_id, left_metric, right_metric in signals:
        left, left_reason = _difference(calculation_items, left_metric)
        right, right_reason = _difference(calculation_items, right_metric)
        if left_reason or right_reason:
            reason = left_reason or right_reason or "缺少唯一的年度差额。"
            output.append(_row(signal_id, "abstained", reason, left, right, None, None))
            continue
        if left is None or right is None:
            output.append(_row(signal_id, "abstained", "缺少唯一的年度差额。", left, right, None, None))
            continue

        if left.status != "succeeded" or right.status != "succeeded":
            output.append(_row(signal_id, "abstained", "缺少已成功且可比的年度差额。", left, right, None, None))
            continue

        if duplicate_fact_ids:
            duplicate = ", ".join(sorted(duplicate_fact_ids))
            output.append(
                _row(signal_id, "abstained", f"输入中存在重复 fact_id，不能确定事实身份：{duplicate}。", left, right, None, None)
            )
            continue

        left_facts, left_value, reason = _validated_difference(left, left_metric, by_id, comparability)
        if reason is not None:
            output.append(_row(signal_id, "abstained", reason, left, right, None, None))
            continue
        right_facts, right_value, reason = _validated_difference(right, right_metric, by_id, comparability)
        if reason is not None:
            output.append(_row(signal_id, "abstained", reason, left, right, None, None))
            continue
        assert left_facts is not None and left_value is not None
        assert right_facts is not None and right_value is not None

        combined_facts = (*left_facts, *right_facts)
        reason = _shared_identity_reason(combined_facts)
        if reason is not None:
            output.append(
                _row(signal_id, "abstained", reason, left, right, left.output_value, right.output_value)
            )
            continue

        triggered = left_value > 0 and right_value < 0
        status = "candidate" if triggered else "not_triggered"
        output.append(
            _row(signal_id, status, "", left, right, left.output_value, right.output_value)
        )
    return tuple(output)


def _difference(
    calculations: Sequence[CalculationResult], metric_id: str
) -> tuple[CalculationResult | None, str | None]:
    expected_id = f"calc:{FORMULA_DIFFERENCE}:{metric_id}"
    found = [item for item in calculations if item.calculation_id == expected_id]
    if len(found) != 1:
        return None, f"{metric_id} 缺少唯一的年度差额计算结果。"
    return found[0], None


def _validated_difference(
    calculation: CalculationResult,
    metric_id: str,
    by_id: dict[str, FinancialFactV2],
    comparability: AnnualComparabilityCheck | None,
) -> tuple[tuple[FinancialFactV2, FinancialFactV2] | None, Decimal | None, str | None]:
    expected_id = f"calc:{FORMULA_DIFFERENCE}:{metric_id}"
    if calculation.calculation_id != expected_id or calculation.formula_id != FORMULA_DIFFERENCE:
        return None, None, "年度差额的计算标识与公式不匹配。"
    if calculation.rule_version != RULE_VERSION:
        return None, None, "年度差额规则版本不匹配，不能用于本次筛查。"
    if calculation.formula_expression != DIFFERENCE_EXPRESSION:
        return None, None, "年度差额公式表达式与受控规则不匹配。"
    if calculation.status != "succeeded" or calculation.output_value is None:
        return None, None, "年度差额没有已成功的精确输出。"
    if len(calculation.input_fact_ids) != 2 or len(set(calculation.input_fact_ids)) != 2:
        return None, None, "年度差额必须恰好引用两条不同的年度事实。"

    missing = [fact_id for fact_id in calculation.input_fact_ids if fact_id not in by_id]
    if missing:
        return None, None, "差额引用的事实不在输入中，或 fact_id 不唯一。"
    current, prior = (by_id[fact_id] for fact_id in calculation.input_fact_ids)
    if current.metric_id != metric_id or prior.metric_id != metric_id:
        return None, None, "差额引用事实的 metric_id 与候选指标不匹配。"
    if current.comparison_role != "current" or prior.comparison_role != "comparative":
        return None, None, "差额输入必须依次为 current 与 comparative 事实。"
    if current.period_type != "duration" or prior.period_type != "duration":
        return None, None, "差额事实必须是年度期间值。"
    if current.frequency != "annual" or prior.frequency != "annual":
        return None, None, "差额事实的 frequency 必须为 annual。"
    if current.report_year is None or prior.report_year != current.report_year:
        return None, None, "差额事实必须属于同一报告年。"
    if not _covers_calendar_year(current, current.report_year):
        return None, None, "current 事实必须覆盖报告年完整自然年。"
    if not _covers_calendar_year(prior, current.report_year - 1):
        return None, None, "comparative 事实必须覆盖报告年上一完整自然年。"
    if current.scope == "unknown" or prior.scope == "unknown":
        return None, None, "未知报表口径不能用于候选筛查。"
    if current.scope != prior.scope:
        return None, None, "同一指标两年的报表口径不一致。"
    if current.unit != prior.unit:
        return None, None, "同一指标两年的单位不一致。"
    if Decimal(current.unit_multiplier) != Decimal(prior.unit_multiplier):
        return None, None, "同一指标两年的单位倍率不一致。"
    comparability_reason = comparability_mismatch_reason(
        comparability,
        document_id=current.source_document_id,
        source_sha256=current.source_sha256,
        report_year=current.report_year,
    )
    if comparability_reason is not None:
        return None, None, "年度差额的可比性证据未通过：" + comparability_reason
    if current.restatement_status == "restated" or prior.restatement_status == "restated":
        return None, None, "事实字段与核验通过的 not_restated 可比性证明冲突。"

    expected_value = Decimal(current.normalized_value) - Decimal(prior.normalized_value)
    if Decimal(calculation.output_value) != expected_value:
        return None, None, "年度差额与输入事实的规范值重算结果不一致。"
    return (current, prior), expected_value, None


def _covers_calendar_year(fact: FinancialFactV2, year: int) -> bool:
    return (
        fact.period_start == date(year, 1, 1)
        and fact.period_end == date(year, 12, 31)
    )


def _shared_identity_reason(facts: tuple[FinancialFactV2, ...]) -> str | None:
    if len(facts) != 4:
        return "跨指标筛查必须使用四条唯一年度事实。"
    if len({fact.fact_id for fact in facts}) != 4:
        return "跨指标筛查不能重复使用同一 fact_id。"
    identity_checks = (
        ("company_id", {fact.company_id for fact in facts}),
        ("来源 document_id", {fact.source_document_id for fact in facts}),
        ("来源 hash", {fact.source_sha256 for fact in facts}),
        ("currency", {fact.currency for fact in facts}),
        ("report_year", {fact.report_year for fact in facts}),
    )
    mismatches = [name for name, values in identity_checks if len(values) != 1]
    if mismatches:
        return "跨指标输入的" + "、".join(mismatches) + "不一致，不能组合候选线索。"
    if any(fact.scope == "unknown" for fact in facts):
        return "跨指标输入存在未知报表口径，不能组合候选线索。"
    if len({fact.scope for fact in facts}) != 1:
        return "跨指标输入的报表口径不一致，不能组合候选线索。"
    return None


def _row(
    signal_id: str,
    status: str,
    reason: str,
    left: CalculationResult | None,
    right: CalculationResult | None,
    left_difference: str | None,
    right_difference: str | None,
) -> dict[str, object]:
    fact_ids: list[str] = []
    calculation_ids: list[str] = []
    for item in (left, right):
        if item is None:
            continue
        calculation_ids.append(item.calculation_id)
        fact_ids.extend(item.input_fact_ids)
    return {
        "signal_id": signal_id,
        "status": status,
        "reason": reason,
        "input_fact_ids": tuple(fact_ids),
        "calculation_ids": tuple(calculation_ids),
        "left_difference": left_difference,
        "right_difference": right_difference,
    }
