"""M3 四条年度确定性筛查规则。

模块只计算已独立核验的 FinancialFactV2；它不调用模型，也不接入旧预检或
默认 M2 CLI。调用约定要求传入当前进程直接由 ``verify_financial_fact`` 返回的
VerificationResult；该状态对象本身不是签名 proof，因此审计 JSON 或手工重建对象
都不能作为验收依据。任一规则无法计算时，总分为 None，结果明确标为弃权。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal, Sequence

from finagent.schemas.financial_fact import decimal_to_str
from finagent.schemas.financial_fact_v2 import FinancialFactV2, VerificationResult
from finagent.verification.comparability import (
    AnnualComparabilityCheck,
    comparability_mismatch_reason,
)
from finagent.verification.m3_semantic_mapping import (
    ParentProfitSemanticMapping,
    is_verified_parent_profit_semantic_mapping,
)

RULE_VERSION = "m3-four-annual-rules-v1.0.0-trial"
POINTS_PER_RULE = 25
THRESHOLD_DEDUCTED_PARENT_SHARE = Decimal("0.30")
THRESHOLD_GROWTH_GAP = Decimal("0.20")

RULE_1 = "consolidated_profit_up_cash_down"
RULE_2 = "deducted_parent_profit_share"
RULE_3 = "receivables_growth_gap"
RULE_4 = "inventory_growth_gap"

RuleStatus = Literal["calculable", "abstained"]
ScreeningStatus = Literal["completed", "abstained"]


@dataclass(frozen=True, slots=True)
class M3RuleIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class M3RuleResult:
    rule_id: str
    rule_version: str
    formula: str
    threshold: str | None
    points_if_triggered: int
    points: int | None
    status: RuleStatus
    triggered: bool | None
    calculated_value: str | None
    input_fact_ids: tuple[str, ...]
    issues: tuple[M3RuleIssue, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "formula": self.formula,
            "threshold": self.threshold,
            "points_if_triggered": self.points_if_triggered,
            "points": self.points,
            "status": self.status,
            "triggered": self.triggered,
            "calculated_value": self.calculated_value,
            "input_fact_ids": list(self.input_fact_ids),
            "issues": [item.to_dict() for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class M3ScreeningResult:
    report_year: int
    rule_version: str
    status: ScreeningStatus
    total_score: int | None
    maximum_score: int
    rules: tuple[M3RuleResult, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "report_year": self.report_year,
            "rule_version": self.rule_version,
            "status": self.status,
            "total_score": self.total_score,
            "maximum_score": self.maximum_score,
            "rules": [item.to_dict() for item in self.rules],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class _FactIndex:
    by_metric: dict[str, tuple[FinancialFactV2, ...]]
    by_verification: dict[str, tuple[VerificationResult, ...]]
    duplicate_fact_ids: frozenset[str]
    input_issues: tuple[M3RuleIssue, ...]


def screen_m3_annual_rules(
    facts: Sequence[FinancialFactV2],
    verifications: Sequence[VerificationResult],
    report_year: int,
    *,
    comparability: AnnualComparabilityCheck | None,
    parent_profit_mapping: ParentProfitSemanticMapping | None,
) -> M3ScreeningResult:
    """计算 M3 四条规则；缺事实、独立核验或可比性 proof 时失败关闭。

    ``parent_profit_mapping`` 必须由
    ``verify_parent_profit_semantic_mapping`` 从原 PDF 第 7 页签发，且绑定
    两年 key-data 与合并利润表归母净利润。一般 ``scope=unknown`` 不会通过。
    """

    if type(report_year) is not int or not 1900 <= report_year <= 2100:
        raise ValueError("report_year 必须是 1900 到 2100 之间的整数。")
    index = _index_inputs(facts, verifications)
    result_1 = _evaluate_profit_cash(index, report_year, comparability)
    result_2 = _evaluate_deducted_parent(index, report_year, comparability, parent_profit_mapping)
    result_3 = _evaluate_growth_gap(
        index,
        report_year,
        comparability,
        rule_id=RULE_3,
        metric_a="accounts_receivable_net",
        metric_b="revenue",
        scope="consolidated",
        statement_a="balance_sheet",
        statement_b="income_statement",
        period_type_a="instant",
        formula="应收账款净额同比增速 - 营业收入同比增速 >= 0.20",
    )
    result_4 = _evaluate_growth_gap(
        index,
        report_year,
        comparability,
        rule_id=RULE_4,
        metric_a="inventory_net",
        metric_b="cost_of_goods_sold",
        scope="consolidated",
        statement_a="balance_sheet",
        statement_b="income_statement",
        period_type_a="instant",
        formula="存货净额同比增速 - 营业成本同比增速 >= 0.20",
    )
    rules = (result_1, result_2, result_3, result_4)
    complete = all(item.status == "calculable" for item in rules)
    return M3ScreeningResult(
        report_year=report_year,
        rule_version=RULE_VERSION,
        status="completed" if complete else "abstained",
        total_score=sum(item.points or 0 for item in rules) if complete else None,
        maximum_score=POINTS_PER_RULE * len(rules),
        rules=rules,
        limitations=(
            "试行阈值尚待隔离评测验证并冻结；当前分数只用于进一步核查。",
            "规则二语义映射仅支持海天味业 603288 的 2024 年报第 7 页已核验版式；其他公司或期间必须弃权。",
            "筛查入口要求同一进程中 verify_financial_fact 的直接结果；序列化或反序列化核验 JSON 仅供审计。",
            "异常线索不构成确认舞弊的结论。",
        ),
    )


def _index_inputs(
    facts: Sequence[FinancialFactV2], verifications: Sequence[VerificationResult]
) -> _FactIndex:
    issues: list[M3RuleIssue] = []
    if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
        issues.append(M3RuleIssue("invalid_facts", "facts 必须是 FinancialFactV2 序列。"))
        fact_items: tuple[FinancialFactV2, ...] = ()
    else:
        fact_items = tuple(item for item in facts if isinstance(item, FinancialFactV2))
        if len(fact_items) != len(facts):
            issues.append(M3RuleIssue("invalid_facts", "facts 序列包含非 FinancialFactV2 对象。"))

    if isinstance(verifications, (str, bytes)) or not isinstance(verifications, Sequence):
        issues.append(M3RuleIssue("invalid_verifications", "verifications 必须是 VerificationResult 序列。"))
        verification_items: tuple[VerificationResult, ...] = ()
    else:
        verification_items = tuple(item for item in verifications if isinstance(item, VerificationResult))
        if len(verification_items) != len(verifications):
            issues.append(M3RuleIssue("invalid_verifications", "核验序列包含非 VerificationResult 对象。"))

    fact_groups: dict[str, list[FinancialFactV2]] = defaultdict(list)
    fact_id_counts: dict[str, int] = defaultdict(int)
    for fact in fact_items:
        fact_groups[fact.metric_id].append(fact)
        fact_id_counts[fact.fact_id] += 1
    verification_groups: dict[str, list[VerificationResult]] = defaultdict(list)
    for check in verification_items:
        verification_groups[check.target_id].append(check)
    return _FactIndex(
        by_metric={key: tuple(value) for key, value in fact_groups.items()},
        by_verification={key: tuple(value) for key, value in verification_groups.items()},
        duplicate_fact_ids=frozenset(key for key, count in fact_id_counts.items() if count > 1),
        input_issues=tuple(issues),
    )


def _evaluate_profit_cash(
    index: _FactIndex, report_year: int, comparability: AnnualComparabilityCheck | None
) -> M3RuleResult:
    selected: list[FinancialFactV2] = []
    issues = list(index.input_issues)
    profit = _select_fact(index, "net_profit_consolidated", "current", report_year, issues)
    cash = _select_fact(index, "operating_cash_flow", "current", report_year, issues)
    for fact, period_type, statement in (
        (profit, "duration", "income_statement"),
        (cash, "duration", "cash_flow_statement"),
    ):
        if fact is not None:
            selected.append(fact)
            issues.extend(
                _validate_fact(
                    index,
                    fact,
                    report_year=report_year,
                    role="current",
                    period_type=period_type,
                    scope="consolidated",
                    statement_type=statement,
                )
            )
            issues.extend(_verified(index, fact))
    issues.extend(_same_basis(selected))
    issues.extend(_comparability_issue(selected, comparability, report_year))
    if issues:
        return _abstained(RULE_1, "合并净利润 > 0 且经营活动现金流量净额 < 0", None, selected, issues)
    assert profit is not None and cash is not None
    profit_value = Decimal(profit.normalized_value)
    cash_value = Decimal(cash.normalized_value)
    triggered = profit_value > 0 and cash_value < 0
    return _calculated(
        RULE_1,
        "合并净利润 > 0 且经营活动现金流量净额 < 0",
        None,
        triggered,
        f"net_profit_consolidated={decimal_to_str(profit_value)};operating_cash_flow={decimal_to_str(cash_value)}",
        selected,
    )


def _evaluate_deducted_parent(
    index: _FactIndex,
    report_year: int,
    comparability: AnnualComparabilityCheck | None,
    mapping: ParentProfitSemanticMapping | None,
) -> M3RuleResult:
    issues = list(index.input_issues)
    selected: list[FinancialFactV2] = []
    parent_pair = _select_pair(index, "net_profit_parent", report_year, issues)
    deducted_pair = _select_pair(index, "net_profit_parent_ex_nonrecurring", report_year, issues)
    expected = (
        (parent_pair, "consolidated", "income_statement"),
        (deducted_pair, "unknown", "key_financial_data"),
    )
    for pair, scope, statement_type in expected:
        for role, fact in zip(("current", "comparative"), pair, strict=False):
            if fact is None:
                continue
            selected.append(fact)
            issues.extend(
                _validate_fact(
                    index,
                    fact,
                    report_year=report_year,
                    role=role,
                    period_type="duration",
                    scope=scope,
                    statement_type=statement_type,
                )
            )
            issues.extend(_verified(index, fact))
    issues.extend(_same_basis(selected))
    issues.extend(_comparability_issue(selected, comparability, report_year))
    if not is_verified_parent_profit_semantic_mapping(mapping):
        issues.append(
            M3RuleIssue(
                "semantic_mapping",
                "缺少本进程从原始 PDF 第 7 页签发的规则二专属跨表语义映射 proof。",
            )
        )
    elif parent_pair[0] is not None and parent_pair[1] is not None and deducted_pair[0] is not None and deducted_pair[1] is not None:
        assert mapping is not None
        if not _mapping_matches(mapping, parent_pair, deducted_pair, report_year):
            issues.append(M3RuleIssue("semantic_mapping_mismatch", "规则二语义映射 proof 与本次事实 ID 或金额不匹配。"))
    if issues:
        return _abstained(
            RULE_2,
            "归母净利润 > 0 且 abs(归母净利润 - 扣非归母净利润) / 归母净利润 >= 0.30",
            decimal_to_str(THRESHOLD_DEDUCTED_PARENT_SHARE),
            selected,
            issues,
        )
    parent = parent_pair[0]
    deducted = deducted_pair[0]
    assert parent is not None and deducted is not None
    denominator = Decimal(parent.normalized_value)
    adjusted = Decimal(deducted.normalized_value)
    if denominator <= 0:
        return _abstained(
            RULE_2,
            "归母净利润 > 0 且 abs(归母净利润 - 扣非归母净利润) / 归母净利润 >= 0.30",
            decimal_to_str(THRESHOLD_DEDUCTED_PARENT_SHARE),
            selected,
            (M3RuleIssue("nonpositive_denominator", "报告年归母净利润必须大于 0，规则二的分母非正。"),),
        )
    magnitude = _decimal_precision(denominator, adjusted)
    with localcontext() as context:
        context.prec = magnitude
        difference = abs(denominator - adjusted)
        triggered = difference >= THRESHOLD_DEDUCTED_PARENT_SHARE * denominator
        ratio = difference / denominator
    return _calculated(
        RULE_2,
        "归母净利润 > 0 且 abs(归母净利润 - 扣非归母净利润) / 归母净利润 >= 0.30",
        decimal_to_str(THRESHOLD_DEDUCTED_PARENT_SHARE),
        triggered,
        decimal_to_str(ratio),
        selected,
    )


def _evaluate_growth_gap(
    index: _FactIndex,
    report_year: int,
    comparability: AnnualComparabilityCheck | None,
    *,
    rule_id: str,
    metric_a: str,
    metric_b: str,
    scope: str,
    statement_a: str,
    statement_b: str,
    period_type_a: str,
    formula: str,
) -> M3RuleResult:
    issues = list(index.input_issues)
    pair_a = _select_pair(index, metric_a, report_year, issues)
    pair_b = _select_pair(index, metric_b, report_year, issues)
    selected: list[FinancialFactV2] = []
    for pair, statement, period_type in (
        (pair_a, statement_a, period_type_a),
        (pair_b, statement_b, "duration"),
    ):
        for role, fact in zip(("current", "comparative"), pair, strict=False):
            if fact is None:
                continue
            selected.append(fact)
            issues.extend(
                _validate_fact(
                    index,
                    fact,
                    report_year=report_year,
                    role=role,
                    period_type=period_type,
                    scope=scope,
                    statement_type=statement,
                )
            )
            issues.extend(_verified(index, fact))
    issues.extend(_same_basis(selected))
    issues.extend(_comparability_issue(selected, comparability, report_year))
    if issues:
        return _abstained(
            rule_id,
            formula,
            decimal_to_str(THRESHOLD_GROWTH_GAP),
            selected,
            issues,
        )
    a_current, a_prior = pair_a
    b_current, b_prior = pair_b
    assert a_current is not None and a_prior is not None and b_current is not None and b_prior is not None
    a_current_value, a_prior_value = Decimal(a_current.normalized_value), Decimal(a_prior.normalized_value)
    b_current_value, b_prior_value = Decimal(b_current.normalized_value), Decimal(b_prior.normalized_value)
    if a_prior_value <= 0 or b_prior_value <= 0:
        names = []
        if a_prior_value <= 0:
            names.append(f"{metric_a} 比较期基数 {decimal_to_str(a_prior_value)} <= 0")
        if b_prior_value <= 0:
            names.append(f"{metric_b} 比较期基数 {decimal_to_str(b_prior_value)} <= 0")
        return _abstained(
            rule_id,
            formula,
            decimal_to_str(THRESHOLD_GROWTH_GAP),
            selected,
            (M3RuleIssue("nonpositive_base", "；".join(names) + "，同比不可计算。"),),
        )

    with localcontext() as context:
        context.prec = _decimal_precision(a_current_value, a_prior_value, b_current_value, b_prior_value)
        delta_a = a_current_value - a_prior_value
        delta_b = b_current_value - b_prior_value
        # 交叉相乘比较有理数，避免循环小数的舍入改变 20 个百分点边界。
        spread_numerator = delta_a * b_prior_value - delta_b * a_prior_value
        threshold_numerator = THRESHOLD_GROWTH_GAP * a_prior_value * b_prior_value
        triggered = spread_numerator >= threshold_numerator
        spread = delta_a / a_prior_value - delta_b / b_prior_value
    return _calculated(
        rule_id,
        formula,
        decimal_to_str(THRESHOLD_GROWTH_GAP),
        triggered,
        decimal_to_str(spread),
        selected,
    )


def _select_fact(
    index: _FactIndex,
    metric_id: str,
    role: str,
    report_year: int,
    issues: list[M3RuleIssue],
) -> FinancialFactV2 | None:
    grouped = _role_index(index, metric_id, issues)
    matches = grouped.get(role, ())
    if not matches:
        issues.append(M3RuleIssue("missing_fact", f"缺少 {metric_id} 的 {report_year} 年 {role} 事实。"))
        return None
    if len(matches) != 1:
        issues.append(M3RuleIssue("duplicate_fact", f"{metric_id} 的 {role} 事实重复，无法唯一选择。"))
        return None
    return matches[0]


def _select_pair(
    index: _FactIndex, metric_id: str, report_year: int, issues: list[M3RuleIssue]
) -> tuple[FinancialFactV2 | None, FinancialFactV2 | None]:
    grouped = _role_index(index, metric_id, issues)
    output: list[FinancialFactV2 | None] = []
    for role, year in (("current", report_year), ("comparative", report_year - 1)):
        matches = grouped.get(role, ())
        if not matches:
            issues.append(M3RuleIssue("missing_fact", f"缺少 {metric_id} 的 {year} 年 {role} 事实。"))
            output.append(None)
        elif len(matches) != 1:
            issues.append(M3RuleIssue("duplicate_fact", f"{metric_id} 的 {role} 事实重复，无法唯一选择。"))
            output.append(None)
        else:
            output.append(matches[0])
    return output[0], output[1]


def _role_index(
    index: _FactIndex, metric_id: str, issues: list[M3RuleIssue]
) -> dict[str, tuple[FinancialFactV2, ...]]:
    facts = index.by_metric.get(metric_id, ())
    grouped: dict[str, list[FinancialFactV2]] = defaultdict(list)
    for fact in facts:
        if fact.fact_id in index.duplicate_fact_ids:
            issues.append(M3RuleIssue("duplicate_fact_id", f"事实 ID {fact.fact_id} 重复。"))
        if fact.comparison_role not in {"current", "comparative"}:
            issues.append(M3RuleIssue("unknown_comparison_role", f"{metric_id} 存在未知比较角色事实。"))
            continue
        grouped[fact.comparison_role].append(fact)
    return {key: tuple(value) for key, value in grouped.items()}


def _validate_fact(
    index: _FactIndex,
    fact: FinancialFactV2,
    *,
    report_year: int,
    role: str,
    period_type: str,
    scope: str,
    statement_type: str,
) -> tuple[M3RuleIssue, ...]:
    year = report_year if role == "current" else report_year - 1
    issues: list[M3RuleIssue] = []
    if fact.report_year != report_year:
        issues.append(M3RuleIssue("report_year_mismatch", f"{fact.fact_id} 的 report_year 与筛查报告年不一致。"))
    if fact.period_end != date(year, 12, 31) or fact.comparison_role != role:
        issues.append(M3RuleIssue("period_role_mismatch", f"{fact.fact_id} 的期间结束日或 current/comparative 角色不匹配。"))
    if fact.period_type != period_type or fact.frequency != "annual":
        issues.append(M3RuleIssue("period_type_mismatch", f"{fact.fact_id} 期间类型必须是 annual {period_type}。"))
    if period_type == "duration" and fact.period_start != date(year, 1, 1):
        issues.append(M3RuleIssue("period_start_mismatch", f"{fact.fact_id} 未覆盖完整自然年度。"))
    if period_type == "instant" and fact.period_start is not None:
        issues.append(M3RuleIssue("period_start_mismatch", f"{fact.fact_id} 年末时点事实不能带 duration period_start。"))
    if fact.scope != scope or fact.statement_type != statement_type:
        issues.append(
            M3RuleIssue(
                "scope_mismatch",
                f"{fact.fact_id} 的报表口径或 statement_type 不匹配；需要 {scope}/{statement_type}。",
            )
        )
    if fact.restatement_status == "restated":
        issues.append(M3RuleIssue("restatement_conflict", f"{fact.fact_id} 标记已追溯调整，与本次可比性 proof 冲突。"))
    if fact.unit is None or not fact.unit.strip():
        issues.append(M3RuleIssue("unit_missing", f"{fact.fact_id} 缺少明确单位。"))
    if fact.currency.strip().casefold() in {"未披露", "未知", "unknown", "undisclosed"}:
        issues.append(M3RuleIssue("currency_missing", f"{fact.fact_id} 未明确披露币种。"))
    if fact.fact_id in index.duplicate_fact_ids:
        issues.append(M3RuleIssue("duplicate_fact_id", f"事实 ID {fact.fact_id} 重复。"))
    return tuple(issues)


def _verified(index: _FactIndex, fact: FinancialFactV2) -> tuple[M3RuleIssue, ...]:
    matches = index.by_verification.get(fact.fact_id, ())
    if not matches:
        return (M3RuleIssue("verification_missing", f"{fact.fact_id} 缺少独立事实核验结果。"),)
    if len(matches) != 1:
        return (M3RuleIssue("verification_duplicate", f"{fact.fact_id} 存在重复核验结果。"),)
    result = matches[0]
    if result.target_type != "financial_fact" or result.status != "verified" or not result.evidence_ids:
        return (M3RuleIssue("verification_unconfirmed", f"{fact.fact_id} 的独立核验不是带证据的 verified。"),)
    if set(result.evidence_ids).intersection(fact.evidence_ids):
        return (
            M3RuleIssue(
                "verification_evidence_reused",
                f"{fact.fact_id} 的核验结果复用了提取阶段 evidence_ids，不能视为独立原文核验。",
            ),
        )
    return ()


def _same_basis(facts: Sequence[FinancialFactV2]) -> tuple[M3RuleIssue, ...]:
    if len(facts) < 2:
        return ()
    issues: list[M3RuleIssue] = []
    for field_name, description in (
        ("company_id", "公司"),
        ("source_document_id", "来源文档"),
        ("source_sha256", "原始来源哈希"),
        ("currency", "币种"),
        ("unit", "单位"),
        ("unit_multiplier", "单位倍率"),
    ):
        values = {getattr(item, field_name) for item in facts}
        if len(values) != 1:
            issues.append(M3RuleIssue("basis_mismatch", f"同一规则输入事实的{description}不一致。"))
    years = {item.period_end.year if item.period_end is not None else None for item in facts}
    expected_years = {item.report_year if item.comparison_role == "current" else item.report_year - 1 for item in facts}
    if len({item.report_year for item in facts}) != 1 or years != expected_years:
        issues.append(M3RuleIssue("period_pair_mismatch", "同一规则输入事实的报告年与期间角色不一致。"))
    return tuple(issues)


def _comparability_issue(
    facts: Sequence[FinancialFactV2], comparability: AnnualComparabilityCheck | None, report_year: int
) -> tuple[M3RuleIssue, ...]:
    if not facts:
        return (M3RuleIssue("comparability_unbound", "缺少事实，无法把可比性 proof 绑定到本次来源。"),)
    fact = facts[0]
    reason = comparability_mismatch_reason(
        comparability,
        document_id=fact.source_document_id,
        source_sha256=fact.source_sha256,
        report_year=report_year,
    )
    if reason is not None:
        return (M3RuleIssue("comparability_unverified", f"年度可比性未通过：{reason}"),)
    return ()


def _mapping_matches(
    mapping: ParentProfitSemanticMapping,
    parent_pair: tuple[FinancialFactV2 | None, FinancialFactV2 | None],
    key_pair: tuple[FinancialFactV2 | None, FinancialFactV2 | None],
    report_year: int,
) -> bool:
    parent_current, parent_prior = parent_pair
    key_current, key_prior = key_pair
    if None in (parent_current, parent_prior, key_current, key_prior):
        return False
    assert parent_current is not None and parent_prior is not None
    assert key_current is not None and key_prior is not None
    expected_parent_ids = (parent_current.fact_id, parent_prior.fact_id)
    expected_parent_values = (parent_current.normalized_value, parent_prior.normalized_value)
    expected_key_ids = (key_current.fact_id, key_prior.fact_id)
    expected_key_values = (key_current.normalized_value, key_prior.normalized_value)
    return (
        mapping.report_year == report_year
        and mapping.company_id == parent_current.company_id == key_current.company_id
        and mapping.document_id == parent_current.source_document_id == key_current.source_document_id
        and mapping.source_sha256.lower() == parent_current.source_sha256.lower() == key_current.source_sha256.lower()
        and mapping.parent_fact_ids == expected_parent_ids
        and mapping.parent_fact_values == expected_parent_values
        and mapping.key_fact_ids == expected_key_ids
        and mapping.key_fact_values == expected_key_values
        and len(mapping.direct_disclosure_values) == len(expected_parent_values)
        and all(
            Decimal(direct) == Decimal(parent)
            for direct, parent in zip(mapping.direct_disclosure_values, expected_parent_values, strict=True)
        )
    )


def _calculated(
    rule_id: str,
    formula: str,
    threshold: str | None,
    triggered: bool,
    value: str,
    facts: Sequence[FinancialFactV2],
) -> M3RuleResult:
    return M3RuleResult(
        rule_id=rule_id,
        rule_version=RULE_VERSION,
        formula=formula,
        threshold=threshold,
        points_if_triggered=POINTS_PER_RULE,
        points=POINTS_PER_RULE if triggered else 0,
        status="calculable",
        triggered=triggered,
        calculated_value=value,
        input_fact_ids=tuple(item.fact_id for item in facts),
        issues=(),
    )


def _abstained(
    rule_id: str,
    formula: str,
    threshold: str | None,
    facts: Sequence[FinancialFactV2],
    issues: Sequence[M3RuleIssue],
) -> M3RuleResult:
    unique_issues = tuple(dict.fromkeys(issues))
    return M3RuleResult(
        rule_id=rule_id,
        rule_version=RULE_VERSION,
        formula=formula,
        threshold=threshold,
        points_if_triggered=POINTS_PER_RULE,
        points=None,
        status="abstained",
        triggered=None,
        calculated_value=None,
        input_fact_ids=tuple(item.fact_id for item in facts),
        issues=unique_issues or (M3RuleIssue("calculation_failed", "规则不能计算。"),),
    )


def _decimal_precision(*values: Decimal) -> int:
    nonzero = tuple(item for item in values if item != 0)
    if not nonzero:
        return 64
    max_adjusted = max(item.adjusted() for item in nonzero)
    min_exponent = min(int(item.as_tuple().exponent) for item in values)
    span = max(1, max_adjusted - min_exponent + 1)
    coefficient_digits = sum(max(1, len(item.as_tuple().digits)) for item in values)
    return max(64, span * 8 + coefficient_digits * 2 + 32)
