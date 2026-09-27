"""按结构化事实和计算对象核验报告 Claim。"""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import Literal

from finagent.schemas.financial_fact_v2 import (
    CLAIM_CALCULATION,
    CLAIM_FACT,
    CLAIM_HYPOTHESIS,
    CLAIM_INFERENCE,
    CalculationResult,
    Claim,
    FinancialFactV2,
)

ClaimCheckStatus = Literal["verified", "conflict", "insufficient_evidence", "interpretation"]


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    """确定性 Claim 检查结果。推论和假设最多得到 interpretation。"""

    claim_id: str
    status: ClaimCheckStatus
    reason: str | None
    expected_text: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "status": self.status,
            "reason": self.reason,
            "expected_text": self.expected_text,
        }


def expected_fact_claim_text(fact: FinancialFactV2) -> str:
    """返回事实主张唯一允许的确定性文本表示。"""

    period = fact.period_end.isoformat() if fact.period_end is not None else "期间未确认"
    return (
        f"{fact.label_raw}（{period}）规范值为 {fact.normalized_value}；"
        f"币种 {fact.currency}；单位 {fact.unit or '未记录'}；"
        f"单位倍率 {fact.unit_multiplier}。"
    )


def expected_calculation_claim_text(
    calculation: CalculationResult,
    facts: Mapping[str, FinancialFactV2],
) -> str:
    """返回计算主张唯一允许的文本，并显示被引用的两个期间。"""

    fact_pair = [facts[fact_id] for fact_id in calculation.input_fact_ids if fact_id in facts]
    if len(fact_pair) == 2 and all(item.period_end is not None for item in fact_pair):
        current_year = fact_pair[0].period_end.year  # type: ignore[union-attr]
        comparative_year = fact_pair[1].period_end.year  # type: ignore[union-attr]
        period = f"{current_year} 年本期相对 {comparative_year} 年比较期"
    else:
        period = "年度比较"
    if calculation.formula_id == "annual_difference":
        metric = calculation.calculation_id.removeprefix("calc:annual_difference:")
        measure = "年度差额"
    elif calculation.formula_id == "annual_yoy_rate":
        metric = calculation.calculation_id.removeprefix("calc:annual_yoy_rate:")
        measure = "同比率（比值）"
    else:
        metric = calculation.formula_id
        measure = "计算结果"
    value = calculation.output_value if calculation.output_value is not None else "无"
    return f"{metric} {period}的{measure}为 {value}。"


def verify_claim(
    claim: Claim,
    facts: Mapping[str, FinancialFactV2],
    calculations: Mapping[str, CalculationResult],
    *,
    verified_fact_ids: Set[str],
    verified_calculation_ids: Set[str],
    verified_fact_evidence_ids: Mapping[str, Set[str]],
) -> ClaimCheck:
    """检查 Claim 的对象引用及事实/计算类文本。

    事实与计算主张只接受由结构化对象生成的精确文本。Claim 自带的状态和附加
    VerificationResult 不能覆盖文本或对象不匹配。推论、假设始终标记为解释，
    并要求已确认引用、替代解释及明确限制。
    """

    if not isinstance(claim, Claim):
        raise TypeError("claim 必须是 Claim。")
    if claim.claim_type in {CLAIM_INFERENCE, CLAIM_HYPOTHESIS}:
        return _check_interpretation(
            claim,
            facts,
            calculations,
            verified_fact_ids,
            verified_calculation_ids,
            verified_fact_evidence_ids,
        )
    if claim.verification_status != "verified":
        return _check(claim, "insufficient_evidence", "Claim 自身状态不是 verified。")
    if not claim.supporting_evidence_ids:
        return _check(claim, "insufficient_evidence", "事实或计算主张必须引用已确认的来源证据。")

    if claim.claim_type == CLAIM_FACT:
        if len(claim.supporting_fact_ids) != 1 or claim.calculation_ids:
            return _check(claim, "conflict", "事实主张必须只引用一个事实，且不能引用计算。")
        fact_id = claim.supporting_fact_ids[0]
        fact = facts.get(fact_id)
        if fact is None or fact_id not in verified_fact_ids:
            return _check(claim, "insufficient_evidence", "事实主张引用的事实未全部确认。")
        allowed = verified_fact_evidence_ids.get(fact_id, set())
        if set(claim.supporting_evidence_ids) != set(allowed) or not allowed:
            return _check(claim, "conflict", "事实主张的证据引用与该事实的已确认来源证据不完全一致。")
        expected = expected_fact_claim_text(fact)
        if claim.text != expected:
            return _check(claim, "conflict", "事实主张文本与结构化事实的受控表示不一致。", expected)
        return _check(claim, "verified", None, expected)

    if claim.claim_type == CLAIM_CALCULATION:
        if len(claim.calculation_ids) != 1:
            return _check(claim, "conflict", "计算主张必须只引用一个计算对象。")
        calculation_id = claim.calculation_ids[0]
        calculation = calculations.get(calculation_id)
        if calculation is None or calculation_id not in verified_calculation_ids:
            return _check(claim, "insufficient_evidence", "计算主张引用的计算未全部确认。")
        if claim.supporting_fact_ids != calculation.input_fact_ids:
            return _check(claim, "conflict", "计算主张的事实引用没有按计算对象绑定的输入顺序列出。")
        expected_evidence = _evidence_ids_for_facts(
            calculation.input_fact_ids,
            verified_fact_evidence_ids,
        )
        if set(claim.supporting_evidence_ids) != expected_evidence or not expected_evidence:
            return _check(claim, "conflict", "计算主张的证据引用与输入事实已确认的来源证据不一致。")
        expected = expected_calculation_claim_text(calculation, facts)
        if claim.text != expected:
            return _check(claim, "conflict", "计算主张文本与结构化计算结果的受控表示不一致。", expected)
        return _check(claim, "verified", None, expected)

    return _check(claim, "insufficient_evidence", "该主张类型不在独立 Claim 核验范围内。")


def _check_interpretation(
    claim: Claim,
    facts: Mapping[str, FinancialFactV2],
    calculations: Mapping[str, CalculationResult],
    verified_fact_ids: Set[str],
    verified_calculation_ids: Set[str],
    verified_fact_evidence_ids: Mapping[str, Set[str]],
) -> ClaimCheck:
    references = bool(claim.supporting_fact_ids or claim.calculation_ids)
    if not references:
        return _check(claim, "insufficient_evidence", "推论或假设没有引用事实或计算对象。")
    if any(fact_id not in facts or fact_id not in verified_fact_ids for fact_id in claim.supporting_fact_ids):
        return _check(claim, "insufficient_evidence", "推论或假设引用了未确认的事实。")
    if any(
        calculation_id not in calculations or calculation_id not in verified_calculation_ids
        for calculation_id in claim.calculation_ids
    ):
        return _check(claim, "insufficient_evidence", "推论或假设引用了未确认的计算。")
    expected_evidence = _evidence_ids_for_facts(
        claim.supporting_fact_ids,
        verified_fact_evidence_ids,
    )
    for calculation_id in claim.calculation_ids:
        calculation = calculations[calculation_id]
        expected_evidence.update(
            _evidence_ids_for_facts(calculation.input_fact_ids, verified_fact_evidence_ids)
        )
    if set(claim.supporting_evidence_ids) != expected_evidence or not expected_evidence:
        return _check(claim, "insufficient_evidence", "推论或假设的证据引用与其事实、计算对象不一致。")
    if not claim.alternative_explanations:
        return _check(claim, "insufficient_evidence", "推论或假设缺少替代解释，须保留为待核查内容。")
    if not claim.limitations:
        return _check(claim, "insufficient_evidence", "推论或假设缺少限制说明，须保留为待核查内容。")
    return _check(claim, "interpretation", "推论或假设只作为解释展示，不构成已核实事实。")


def _evidence_ids_for_facts(
    fact_ids: tuple[str, ...],
    evidence_by_fact: Mapping[str, Set[str]],
) -> set[str]:
    result: set[str] = set()
    for fact_id in fact_ids:
        result.update(evidence_by_fact.get(fact_id, set()))
    return result


def _check(
    claim: Claim,
    status: ClaimCheckStatus,
    reason: str | None,
    expected_text: str | None = None,
) -> ClaimCheck:
    return ClaimCheck(claim.claim_id, status, reason, expected_text)
