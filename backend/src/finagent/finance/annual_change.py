"""同一指标的年度同比。只做差额和比率，不判断风险或舞弊。"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from typing import Any, Iterable

from finagent.ingestion.errors import PdfInputError
from finagent.schemas.financial_fact import (
    PERIOD_TYPE_ANNUAL,
    ExtractionResult,
    FinancialFact,
    decimal_to_str,
)

ANNUAL_CHANGE_FORMULA = "(本期规范值-上期规范值)/上期规范值；规范值已按单位倍率换算为元"
_FORMULA = ANNUAL_CHANGE_FORMULA


@dataclass(frozen=True, slots=True)
class AnnualChange:
    """一对可比事实的差额和同比率。

    difference 由本期规范值减上期规范值，可用这两项精确复算。
    rate 是 Decimal 除法在当前精度下的近似小数，不是无限位精确分数。
    """

    indicator_name: str
    report_year: int
    prior_year: int
    document_id: str
    source_sha256: str
    company_id: str
    currency: str
    unit_multiplier: str
    statement_scope: str
    period_type: str
    current_value: str
    prior_value: str
    difference: str
    rate: str
    formula: str


@dataclass(frozen=True, slots=True)
class AnnualChangeIssue:
    """无法计算时的原因。不含数值结果。"""

    code: str
    message: str
    indicator_name: str


@dataclass(frozen=True, slots=True)
class AnnualChangeResult:
    """同比计算的成功项与弃权项。"""

    changes: tuple[AnnualChange, ...]
    issues: tuple[AnnualChangeIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def calculate_annual_changes(
    source: ExtractionResult | Iterable[FinancialFact],
    report_year: int,
) -> AnnualChangeResult:
    """按指标名称配对报告年度和上一年度。"""

    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise PdfInputError("report_year 必须是 1900 到 2100 之间的整数。")
    grouped: dict[str, list[FinancialFact]] = defaultdict(list)
    for fact in _facts(source):
        grouped[fact.indicator_name].append(fact)
    changes: list[AnnualChange] = []
    issues: list[AnnualChangeIssue] = []
    for indicator_name in sorted(grouped):
        change, issue = _pair(grouped[indicator_name], indicator_name, report_year)
        if issue is not None:
            issues.append(issue)
        elif change is not None:
            changes.append(change)
    return AnnualChangeResult(changes=tuple(changes), issues=tuple(issues))


def _facts(source: ExtractionResult | Iterable[FinancialFact]) -> tuple[FinancialFact, ...]:
    if isinstance(source, ExtractionResult):
        items = source.facts
    elif isinstance(source, FinancialFact):
        items = (source,)
    else:
        items = tuple(source)
    if any(not isinstance(item, FinancialFact) for item in items):
        raise PdfInputError("同比计算只接受 FinancialFact 或 ExtractionResult。")
    return items


def _pair(
    facts: list[FinancialFact],
    indicator_name: str,
    report_year: int,
) -> tuple[AnnualChange | None, AnnualChangeIssue | None]:
    prior_year = report_year - 1
    current = [fact for fact in facts if fact.report_year == report_year]
    prior = [fact for fact in facts if fact.report_year == prior_year]
    if len(current) > 1 or len(prior) > 1:
        return None, _issue(indicator_name, "duplicate_fact", "同一指标在同一年度有重复事实。")
    if not current:
        return None, _issue(indicator_name, "missing_current", f"缺少 {report_year} 年事实。")
    if not prior:
        return None, _issue(indicator_name, "missing_prior", f"缺少 {prior_year} 年事实。")
    left, right = current[0], prior[0]
    mismatched = _mismatches(left, right)
    if mismatched:
        names = "、".join(mismatched)
        return None, _issue(indicator_name, "incompatible_facts", f"两年事实的{names}不一致。")
    if left.period_type != PERIOD_TYPE_ANNUAL:
        return None, _issue(indicator_name, "period_type_not_annual", "期间类型不是年度，不计算同比。")
    current_value = Decimal(left.normalized_value)
    prior_value = Decimal(right.normalized_value)
    if prior_value == 0:
        return None, _issue(indicator_name, "zero_prior", "上期规范值为零，不能计算同比率。")
    difference = current_value - prior_value
    rate = _divide(difference, prior_value)
    return (
        AnnualChange(
            indicator_name=indicator_name,
            report_year=report_year,
            prior_year=prior_year,
            document_id=left.document_id,
            source_sha256=left.source_sha256,
            company_id=left.company_id,
            currency=left.currency,
            unit_multiplier=left.unit_multiplier,
            statement_scope=left.statement_scope,
            period_type=left.period_type,
            current_value=decimal_to_str(current_value),
            prior_value=decimal_to_str(prior_value),
            difference=decimal_to_str(difference),
            rate=decimal_to_str(rate),
            formula=_FORMULA,
        ),
        None,
    )


def _mismatches(current: FinancialFact, prior: FinancialFact) -> list[str]:
    checks = (
        ("document_id", current.document_id != prior.document_id),
        ("source_sha256", current.source_sha256 != prior.source_sha256),
        ("company_id", current.company_id != prior.company_id),
        ("currency", current.currency != prior.currency),
        ("unit_multiplier", Decimal(current.unit_multiplier) != Decimal(prior.unit_multiplier)),
        ("statement_scope", current.statement_scope != prior.statement_scope),
        ("period_type", current.period_type != prior.period_type),
    )
    return [name for name, failed in checks if failed]


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """按有效数字精度做除法。结果是近似小数，差额本身不经过这次除法。"""

    digits = len(numerator.as_tuple().digits) + len(denominator.as_tuple().digits) + 8
    with localcontext() as ctx:
        ctx.prec = max(28, digits)
        return numerator / denominator


def _issue(indicator_name: str, code: str, message: str) -> AnnualChangeIssue:
    return AnnualChangeIssue(code=code, message=message, indicator_name=indicator_name)
