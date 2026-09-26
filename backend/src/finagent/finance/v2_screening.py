"""基于 v2 年度差额的候选线索。未知可比性一律弃权，不跨指标混算未知口径。"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from finagent.finance.v2_calculation import FORMULA_DIFFERENCE
from finagent.schemas.financial_fact_v2 import SCOPE_UNKNOWN, CalculationResult, FinancialFactV2

PROFIT = "net_profit_parent"
CASH = "operating_cash_flow"
REVENUE = "revenue"


def screen_v2_annual_candidates(
    facts: Sequence[FinancialFactV2],
    calculations: Sequence[CalculationResult],
) -> tuple[dict[str, object], ...]:
    """只有两边差额都 succeeded 且口径已知并相同，才给出候选。"""

    by_id = {fact.fact_id: fact for fact in facts}
    signals = (
        ("profit_up_cash_down", PROFIT, CASH),
        ("revenue_up_cash_down", REVENUE, CASH),
    )
    output: list[dict[str, object]] = []
    for signal_id, left_metric, right_metric in signals:
        left = _difference(calculations, left_metric)
        right = _difference(calculations, right_metric)
        if left is None or right is None or left.status != "succeeded" or right.status != "succeeded":
            output.append(_row(signal_id, "abstained", "缺少已成功且可比的年度差额。", left, right, None, None))
            continue
        if left.output_value is None or right.output_value is None:
            output.append(_row(signal_id, "abstained", "成功差额缺少精确输出。", left, right, None, None))
            continue
        missing = [fact_id for fact_id in (*left.input_fact_ids, *right.input_fact_ids) if fact_id not in by_id]
        if missing:
            output.append(_row(signal_id, "abstained", "差额引用的事实不在输入中。", left, right, left.output_value, right.output_value))
            continue
        scopes = {by_id[fact_id].scope for fact_id in (*left.input_fact_ids, *right.input_fact_ids)}
        if SCOPE_UNKNOWN in scopes or len(scopes) != 1:
            output.append(_row(signal_id, "abstained", "未知或不同口径不能跨指标混算。", left, right, left.output_value, right.output_value))
            continue
        triggered = Decimal(left.output_value) > 0 and Decimal(right.output_value) < 0
        status = "candidate" if triggered else "not_triggered"
        output.append(_row(signal_id, status, "", left, right, left.output_value, right.output_value))
    return tuple(output)


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


def _difference(calculations: Sequence[CalculationResult], metric_id: str) -> CalculationResult | None:
    found = [
        item
        for item in calculations
        if item.formula_id == FORMULA_DIFFERENCE and item.calculation_id.endswith(f":{metric_id}")
    ]
    if len(found) != 1:
        return None
    return found[0]


