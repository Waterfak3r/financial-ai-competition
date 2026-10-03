"""季度流量的确定性差分和期间比较。独立重算位于 verification/quarterly.py。"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Sequence
from uuid import uuid4

from finagent.schemas.quarterly import (
    CumulativeObservation,
    DerivedQuarter,
    METRIC_STATEMENTS,
    QuarterlyComparison,
    QUARTER_SOURCE_PERIOD,
)

RULE_VERSION = "quarterly-flow-v1"
RATE_PRECISION = 50
_SUPPORTED_CURRENCIES = {"CNY", "USD", "HKD", "EUR", "JPY", "GBP"}
_UNIT_MULTIPLIERS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}


class QuarterlyCalculationError(ValueError):
    pass


def derive_quarter(
    observations: Sequence[CumulativeObservation],
    *,
    company_id: str,
    metric_id: str,
    year: int,
    quarter: str,
) -> DerivedQuarter:
    """从该年年初累计流量推导单季值，时点值和缺失前序严格失败。"""

    if quarter not in QUARTER_SOURCE_PERIOD:
        raise QuarterlyCalculationError("quarter 必须是 Q1/Q2/Q3/Q4。")
    candidates = [
        item for item in observations
        if item.company_id == company_id
        and item.metric_id == metric_id
        and item.observation_year == year
        and item.column_role == "current"
    ]
    if not candidates:
        raise QuarterlyCalculationError("缺少目标公司、指标或报告年的 current 累计 observation。")
    if any(item.basis != "year_to_date" for item in candidates):
        raise QuarterlyCalculationError("时点 observation 不能差分生成季度流量。")
    if metric_id not in {"revenue", "net_profit_parent", "operating_cash_flow"}:
        raise QuarterlyCalculationError("只有已登记流量指标可以差分。")
    end_period, start_period = QUARTER_SOURCE_PERIOD[quarter]
    ends = [item for item in candidates if item.report_period == end_period]
    if len(ends) != 1:
        raise QuarterlyCalculationError("目標累计期间缺失或来源重复。")
    end_item = ends[0]
    _assert_observation_context(end_item)
    if start_period is None:
        if end_item.period_end is None or end_item.period_end.month != 3:
            raise QuarterlyCalculationError("Q1 需要 1 月 1 日至 3 月 31 日累计值。")
        inputs = (end_item,)
        value = end_item.value
        formula = "Q1 = Q1_ytd"
    else:
        starts = [item for item in candidates if item.report_period == start_period]
        if len(starts) != 1:
            raise QuarterlyCalculationError("缺少上一个连续累计期间；不得把未知值当作零。")
        start_item = starts[0]
        _assert_compatible(end_item, start_item)
        expected_month = {"H1": 6, "9M": 9, "FY": 12}[end_period]
        expected_prior_month = {"Q1": 3, "H1": 6, "9M": 9}[start_period]
        if end_item.period_end is None or start_item.period_end is None:
            raise QuarterlyCalculationError("累计 observation 缺少期间结束日。")
        if end_item.period_end.month != expected_month or start_item.period_end.month != expected_prior_month:
            raise QuarterlyCalculationError("累计 observation 期间不连续。")
        if start_item.period_start != end_item.period_start:
            raise QuarterlyCalculationError("累计 observation 起始日期不一致。")
        if start_item.source_document_id == end_item.source_document_id:
            raise QuarterlyCalculationError("用于差分的累计 observation 必须来自不同报告文档。")
        inputs = (end_item, start_item)
        value = _exact_difference(end_item.value, start_item.value)
        formula = f"{quarter} = {end_period}_ytd - {start_period}_ytd"
    return DerivedQuarter(
        derived_id=f"qderived-{company_id}-{metric_id}-{year}-{quarter}-{uuid4()}",
        metric_id=metric_id,
        company_id=company_id,
        year=year,
        quarter=quarter,  # type: ignore[arg-type]
        normalized_value=format(value, "f"),
        currency=end_item.currency,
        unit=end_item.unit,
        unit_multiplier=end_item.unit_multiplier,
        statement_type=end_item.statement_type,
        scope=end_item.scope,
        formula_id=f"derive_{quarter.lower()}_from_ytd",
        formula_expression=formula,
        input_observation_ids=tuple(item.observation_id for item in inputs),
        input_source_document_ids=tuple(item.source_document_id for item in inputs),
    )


def compare_quarters(
    current: DerivedQuarter,
    prior: DerivedQuarter,
    *,
    relation: str,
) -> QuarterlyComparison:
    if relation not in {"year_over_year", "quarter_over_quarter"}:
        raise QuarterlyCalculationError("relation 必须是 year_over_year 或 quarter_over_quarter。")
    _assert_derived_compatible(current, prior)
    if relation == "year_over_year":
        if current.year != prior.year + 1 or current.quarter != prior.quarter:
            raise QuarterlyCalculationError("同比必须比较相邻年度同一季度。")
    else:
        expected = _prior_quarter(current.year, current.quarter)
        if (prior.year, prior.quarter) != expected:
            raise QuarterlyCalculationError("环比必须比较相邻季度；Q1 对上一年 Q4。")
    difference = _exact_difference(current.value, prior.value)
    limitations: tuple[str, ...] = ()
    growth: str | None
    if prior.value <= 0:
        growth = None
        limitations = ("比较期基数非正，仅保留有依据的差额；增长率弃权。",)
    else:
        with localcontext() as context:
            context.prec = RATE_PRECISION
            growth = format(difference / prior.value, "f")
    return QuarterlyComparison(
        comparison_id=f"qcompare-{current.derived_id}-{prior.derived_id}-{relation}",
        metric_id=current.metric_id,
        relation=relation,  # type: ignore[arg-type]
        current_id=current.derived_id,
        prior_id=prior.derived_id,
        current_value=current.normalized_value,
        prior_value=prior.normalized_value,
        difference=format(difference, "f"),
        growth_rate=growth,
        status="calculated",
        failure_reason=None,
        limitations=limitations,
    )


def _assert_compatible(left: CumulativeObservation, right: CumulativeObservation) -> None:
    _assert_observation_context(left)
    _assert_observation_context(right)
    names = ("company_id", "metric_id", "currency", "unit", "unit_multiplier", "scope", "statement_type", "observation_year")
    mismatches = [name for name in names if getattr(left, name) != getattr(right, name)]
    if mismatches:
        raise QuarterlyCalculationError("累计输入的" + "、".join(mismatches) + "不一致。")
    if left.basis != "year_to_date" or right.basis != "year_to_date":
        raise QuarterlyCalculationError("季度差分只接受 year_to_date 流量累计值。")


def _assert_derived_compatible(left: DerivedQuarter, right: DerivedQuarter) -> None:
    _assert_derived_context(left)
    _assert_derived_context(right)
    names = ("company_id", "metric_id", "currency", "unit", "unit_multiplier", "scope", "statement_type")
    mismatches = [name for name in names if getattr(left, name) != getattr(right, name)]
    if mismatches:
        raise QuarterlyCalculationError("比较输入的" + "、".join(mismatches) + "不一致。")


def _assert_observation_context(item: CumulativeObservation) -> None:
    expected = METRIC_STATEMENTS.get(item.metric_id)
    if expected is None or item.statement_type != expected[0]:
        raise QuarterlyCalculationError("累计 observation 的报表类型未登记或与指标不匹配。")
    if item.scope not in {"consolidated", "parent"}:
        raise QuarterlyCalculationError("累计 observation 必须明确标注 consolidated 或 parent 口径。")
    if not isinstance(item.currency, str) or item.currency.upper() not in _SUPPORTED_CURRENCIES:
        raise QuarterlyCalculationError("累计 observation 缺少受支持的明确币种。")
    if item.unit not in _UNIT_MULTIPLIERS:
        raise QuarterlyCalculationError("累计 observation 缺少受支持的明确单位。")
    try:
        multiplier = Decimal(item.unit_multiplier)
    except Exception as exc:
        raise QuarterlyCalculationError("累计 observation 单位倍率无效。") from exc
    if not multiplier.is_finite() or multiplier <= 0 or multiplier != _UNIT_MULTIPLIERS[item.unit]:
        raise QuarterlyCalculationError("累计 observation 单位倍率缺失、非正数或与单位不匹配。")


def _assert_derived_context(item: DerivedQuarter) -> None:
    expected = METRIC_STATEMENTS.get(item.metric_id)
    if expected is None or item.statement_type != expected[0]:
        raise QuarterlyCalculationError("派生季度报表类型未登记或与指标不匹配。")
    if item.scope not in {"consolidated", "parent"}:
        raise QuarterlyCalculationError("派生季度必须明确标注 consolidated 或 parent 口径。")
    if not isinstance(item.currency, str) or item.currency.upper() not in _SUPPORTED_CURRENCIES:
        raise QuarterlyCalculationError("派生季度缺少受支持的明确币种。")
    if item.unit not in _UNIT_MULTIPLIERS:
        raise QuarterlyCalculationError("派生季度缺少受支持的明确单位。")
    try:
        multiplier = Decimal(item.unit_multiplier)
    except Exception as exc:
        raise QuarterlyCalculationError("派生季度单位倍率无效。") from exc
    if not multiplier.is_finite() or multiplier <= 0 or multiplier != _UNIT_MULTIPLIERS[item.unit]:
        raise QuarterlyCalculationError("派生季度单位倍率缺失、非正数或与单位不匹配。")


def _exact_difference(left: Decimal, right: Decimal) -> Decimal:
    lowest_exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    highest_adjusted = max(left.adjusted(), right.adjusted())
    with localcontext() as context:
        context.prec = max(28, highest_adjusted - lowest_exponent + 2)
        return left - right


def _prior_quarter(year: int, quarter: str) -> tuple[int, str]:
    if quarter == "Q1":
        return year - 1, "Q4"
    return year, f"Q{int(quarter[1]) - 1}"
