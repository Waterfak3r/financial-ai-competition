"""把 v2 事实、证据、核验、计算、主张和候选线索整理成 FINTRACE 年度财报分析报告。

报告分区时独立重算受支持的年度计算，并核验事实、计算类 Claim 的确定性文本。
不调用模型，也不写文件。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from finagent.schemas.financial_fact import decimal_to_str
from finagent.schemas.financial_fact_v2 import (
    CALC_SUCCEEDED,
    CLAIM_HYPOTHESIS,
    CLAIM_INFERENCE,
    SCOPE_CONSOLIDATED,
    SCOPE_PARENT,
    SCOPE_UNKNOWN,
    VERIFIED,
    CalculationResult,
    Claim,
    FinancialFactV2,
    TableCellEvidence,
    VerificationResult,
    contains_complete_raw_token,
)
from finagent.verification.claim import verify_claim
from finagent.verification.comparability import AnnualComparabilityCheck
from finagent.verification.independent_calculation import verify_annual_calculation

_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_REPORT_TITLE = "FINTRACE 年度财报分析报告"
_INTERPRETATION_TYPES = {CLAIM_INFERENCE, CLAIM_HYPOTHESIS}
_FACT_TYPES = {"financial_fact", "fact"}
_CALC_TYPES = {"calculation"}
_CLAIM_TYPES = {"claim"}
_NO_VERIFIED_VALUE = "此证据没有 value_region，只保留文字块候选，不能用于 verified。"


class ReportBuildError(ValueError):
    """报告引用或文档身份无法闭合。"""


def build_annual_report(
    run_id: str,
    company_id: str,
    report_year: int,
    source_document_id: str,
    source_sha256: str,
    facts: Sequence[FinancialFactV2],
    evidences: Sequence[TableCellEvidence],
    verifications: Sequence[VerificationResult],
    calculations: Sequence[CalculationResult],
    claims: Sequence[Claim],
    candidate_signals: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    limitations: Sequence[str] = (),
    model_called: bool | None = None,
    comparability: AnnualComparabilityCheck | None = None,
) -> dict[str, Any]:
    """组装报告字典。调用方负责归档，本函数不写磁盘。"""

    _require_id(run_id, "run_id")
    _require_text(company_id, "company_id")
    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise ReportBuildError("report_year 必须是 1900 到 2100 的整数。")
    if model_called is not None and not isinstance(model_called, bool):
        raise ReportBuildError("model_called 只能由调用方显式传入布尔值；未标记时不要填写。")
    _require_text(source_document_id, "source_document_id")
    if not isinstance(source_sha256, str) or _SHA.fullmatch(source_sha256) is None:
        raise ReportBuildError("source_sha256 必须是 64 位十六进制字符串。")

    fact_list = _typed_tuple(facts, FinancialFactV2, "facts")
    evidence_list = _typed_tuple(evidences, TableCellEvidence, "evidences")
    verification_list = _typed_tuple(verifications, VerificationResult, "verifications")
    calculation_list = _typed_tuple(calculations, CalculationResult, "calculations")
    claim_list = _typed_tuple(claims, Claim, "claims")
    signals = _normalize_signals(candidate_signals)
    report_limitations = _text_list(limitations, "limitations")

    facts_by_id = _index(fact_list, "fact_id", "事实")
    evidences_by_id = _index(evidence_list, "evidence_id", "证据")
    calculations_by_id = _index(calculation_list, "calculation_id", "计算")
    claims_by_id = _index(claim_list, "claim_id", "主张")
    _index(verification_list, "verification_id", "核验")

    for fact in fact_list:
        _same_source(fact.source_document_id, fact.source_sha256, source_document_id, source_sha256, f"事实 {fact.fact_id}")
        if fact.company_id != company_id:
            raise ReportBuildError(f"事实 {fact.fact_id} 的 company_id 与报告不一致。")
        if fact.report_year is not None and fact.report_year != report_year:
            raise ReportBuildError(f"事实 {fact.fact_id} 的 report_year 与报告不一致。")
        _require_known_ids(fact.evidence_ids, evidences_by_id, f"事实 {fact.fact_id}")
    for evidence in evidence_list:
        _same_source(
            evidence.document_id,
            evidence.source_sha256,
            source_document_id,
            source_sha256,
            f"证据 {evidence.evidence_id}",
        )
    for calculation in calculation_list:
        _require_known_ids(calculation.input_fact_ids, facts_by_id, f"计算 {calculation.calculation_id}")
    for claim in claim_list:
        _require_known_ids(claim.supporting_fact_ids, facts_by_id, f"主张 {claim.claim_id}")
        _require_known_ids(claim.supporting_evidence_ids, evidences_by_id, f"主张 {claim.claim_id}")
        _require_known_ids(claim.calculation_ids, calculations_by_id, f"主张 {claim.claim_id}")
    for verification in verification_list:
        _require_known_ids(verification.evidence_ids, evidences_by_id, f"核验 {verification.verification_id}")
        _require_target(verification, facts_by_id, calculations_by_id, claims_by_id, evidences_by_id)

    verifications_for: dict[tuple[str, str], list[VerificationResult]] = {}
    for verification in verification_list:
        kind = _target_kind(verification.target_type)
        verifications_for.setdefault((kind, verification.target_id), []).append(verification)
    for group in verifications_for.values():
        group.sort(key=lambda item: item.verification_id)

    confirmed_ids: set[str] = set()
    confirmed_fact_evidence_ids: dict[str, set[str]] = {}
    confirmed_metrics: list[dict[str, Any]] = []
    pending_facts: list[dict[str, Any]] = []
    for fact in sorted(fact_list, key=lambda item: item.fact_id):
        record, confirmed = _place_fact(fact, evidences_by_id, verifications_for.get(("fact", fact.fact_id), ()))
        if confirmed:
            confirmed_ids.add(fact.fact_id)
            confirmed_fact_evidence_ids[fact.fact_id] = _confirmed_fact_evidence_ids(
                fact,
                verifications_for.get(("fact", fact.fact_id), ()),
                evidences_by_id,
            )
            confirmed_metrics.append(record)
        else:
            pending_facts.append(record)

    confirmed_analyses: list[dict[str, Any]] = []
    pending_calculations: list[dict[str, Any]] = []
    for calculation in sorted(calculation_list, key=lambda item: item.calculation_id):
        record, confirmed = _place_calculation(
            calculation,
            confirmed_ids,
            facts_by_id,
            evidences_by_id,
            verifications_for.get(("calculation", calculation.calculation_id), ()),
            report_year,
            comparability,
        )
        if confirmed:
            confirmed_analyses.append(record)
        else:
            pending_calculations.append(record)

    verified_claims: list[dict[str, Any]] = []
    interpretations: list[dict[str, Any]] = []
    pending_claims: list[dict[str, Any]] = []
    for claim in sorted(claim_list, key=lambda item: item.claim_id):
        attached = verifications_for.get(("claim", claim.claim_id), ())
        record = _claim_record(
            claim,
            attached,
            evidences_by_id,
            confirmed_ids,
            {item["calculation_id"] for item in confirmed_analyses},
            confirmed_fact_evidence_ids,
            facts_by_id,
            calculations_by_id,
        )
        if record["placement"] == "interpretations":
            interpretations.append(record)
        elif record["placement"] == "verified_claims":
            verified_claims.append(record)
        else:
            pending_claims.append(record)
    used_evidence = {evidence_id for fact in fact_list for evidence_id in fact.evidence_ids}
    used_evidence.update(evidence_id for verification in verification_list for evidence_id in verification.evidence_ids)
    used_evidence.update(evidence_id for claim in claim_list for evidence_id in claim.supporting_evidence_ids)
    pending_evidences = [
        _evidence_record(evidence)
        for evidence in sorted(evidence_list, key=lambda item: item.evidence_id)
        if evidence.evidence_id not in used_evidence
    ]
    pending_signals = [_signal_record(item) for item in signals]

    gaps = _gaps(fact_list, verification_list, confirmed_metrics, confirmed_analyses, pending_signals)
    report_limitations = list(dict.fromkeys([*report_limitations, *gaps, _FRAUD_LIMIT]))
    report: dict[str, Any] = {
        "kind": "fintrace_annual_analysis_report",
        "title": _REPORT_TITLE,
        "run_id": run_id,
        "company_id": company_id,
        "report_year": report_year,
        "source_document_id": source_document_id,
        "source_sha256": source_sha256.lower(),
        "fraud_conclusion": None,
        "scope": {
            "note": (
                "本报告检查事实与引证是否对齐，按受控公式独立重算受支持的年度差额和同比率，"
                "并用确定规则核对事实与计算类主张；推论和假设单独列示。报告不调用模型，也不确认舞弊。"
            ),
            "gaps": gaps,
        },
        "comparability": None if comparability is None else comparability.to_dict(),
        "confirmed": {"metrics": confirmed_metrics, "analyses": confirmed_analyses},
        "verified_claims": verified_claims,
        "interpretations": interpretations,
        "pending_review": {
            "facts": pending_facts,
            "calculations": pending_calculations,
            "claims": pending_claims,
            "candidate_signals": pending_signals,
            "uncited_evidences": pending_evidences,
        },
        "limitations": report_limitations,
    }
    if model_called is not None:
        report["model_called"] = model_called
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    """把报告字典写成 Markdown。页码用锚点和文字同时标出。"""

    if not isinstance(report, Mapping) or report.get("kind") != "fintrace_annual_analysis_report":
        raise ReportBuildError("render_markdown 只接受 build_annual_report 的结果。")
    lines = [
        f"# {_REPORT_TITLE} {report['company_id']} {report['report_year']}",
        "",
        f"- run_id：`{report['run_id']}`",
        f"- 来源文档：`{report['source_document_id']}`",
        f"- SHA256：`{report['source_sha256']}`",
    ]
    if "model_called" in report:
        lines.append(f"- 模型调用：{'是' if report['model_called'] else '否'}")
    lines.extend([
        "- 舞弊结论：无。候选线索不是确认舞弊。",
        "",
        "## 范围与缺口",
        "",
        str(report["scope"]["note"]),
        "",
    ])
    gaps = report["scope"]["gaps"]
    if not gaps:
        lines.append("- 已提供的对象都进入了已确认区或待核查区。")
    else:
        lines.extend(f"- {gap}" for gap in gaps)
    comparability = report.get("comparability")
    if comparability is not None:
        lines.extend(["", "## 年度可比性核验", ""])
        lines.extend(
            [
                f"- 核验状态：`{comparability['status']}`",
                f"- 追溯调整状态：`{comparability['restatement_status']}`",
                f"- 覆盖期间：{comparability['current_year']} 年本期与 {comparability['comparative_year']} 年比较期",
            ]
        )
        for check in comparability["checks"]:
            lines.append(f"- 已检查：{check}")
        for conflict in comparability["conflicts"]:
            lines.append(f"- 冲突：{conflict}")
        for limitation in comparability["limitations"]:
            lines.append(f"- 限制：{limitation}")
        if not comparability["evidence"]:
            lines.append("- 没有可定位的原文可比性证据。")
        for evidence in comparability["evidence"]:
            bbox = evidence["bbox"]
            lines.extend(
                [
                    "",
                    f"### `{evidence['topic']}`（PDF 第{evidence['pdf_page']}页）",
                    "",
                    f"- 证据 ID：`{evidence['evidence_id']}`",
                    (
                        f"- 坐标 bbox：`{bbox['x0']}, {bbox['y0']}, "
                        f"{bbox['x1']}, {bbox['y1']}`"
                    ),
                    f"- 原文确认报告年份：PDF 第{evidence['report_year_page']}页，{evidence['report_year_text']}",
                    "> " + "\n> ".join(str(evidence["text"]).splitlines()),
                ]
            )
    lines.extend(["", "## 已确认指标与分析", ""])
    confirmed = report["confirmed"]
    if not confirmed["metrics"] and not confirmed["analyses"]:
        lines.append("没有同时具备核验、引用和交叉对应的指标或计算。这里不补写结论。")
    for metric in confirmed["metrics"]:
        lines.extend(_metric_lines(metric))
    for analysis in confirmed["analyses"]:
        lines.extend(_analysis_lines(analysis))
    lines.extend(["", "## 已核实主张", ""])
    if not report["verified_claims"]:
        lines.append("没有已核实的事实或计算主张。")
    for claim in report["verified_claims"]:
        lines.extend(_claim_lines(claim))
    lines.extend(["", "## 推论与假设", ""])
    if not report["interpretations"]:
        lines.append("没有推论或假设。")
    for claim in report["interpretations"]:
        lines.extend(_claim_lines(claim))
    pending = report["pending_review"]
    lines.extend(["", "## 待核查", ""])
    if not any(pending[key] for key in ("facts", "calculations", "claims", "candidate_signals", "uncited_evidences")):
        lines.append("没有待核查对象。")
    for fact in pending["facts"]:
        lines.extend(_metric_lines(fact, pending=True))
    for analysis in pending["calculations"]:
        lines.extend(_analysis_lines(analysis, pending=True))
    for claim in pending["claims"]:
        lines.extend(_claim_lines(claim))
    fact_records = {
        metric["fact_id"]: metric
        for metric in (*confirmed["metrics"], *pending["facts"])
    }
    for signal in pending["candidate_signals"]:
        lines.extend(_signal_lines(signal, fact_records))
    for evidence in pending["uncited_evidences"]:
        lines.extend(["", f"### 未引用证据 `{evidence['evidence_id']}`", "", _page_text(evidence)])
    lines.extend(["", "## 限制", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


_FRAUD_LIMIT = "候选异常和未核验对象都在待核查区，本报告不确认舞弊。"


def _place_fact(
    fact: FinancialFactV2,
    evidences: Mapping[str, TableCellEvidence],
    verifications: Sequence[VerificationResult],
) -> tuple[dict[str, Any], bool]:
    reasons: list[str] = []
    verified = [item for item in verifications if item.status == VERIFIED]
    blocking = [item for item in verifications if item.status != VERIFIED]
    if not verifications:
        reasons.append("没有核验对象。")
    if blocking:
        reasons.append("存在 conflict 或 insufficient_evidence 核验，不能进入已确认区。")
    supporting = [item for item in verified if _verification_supports_fact(fact, item, evidences)]
    if verified and not supporting:
        reasons.append("verified 状态单独不够；核验证据须对齐值、行、列、表题、单位、期间和口径，只引用旧文字块不能进入已确认区。")
    confirmed = not reasons and bool(supporting)
    record = _fact_record(fact, evidences, verifications)
    record["placement_reasons"] = [] if confirmed else reasons
    return record, confirmed


def _place_calculation(
    calculation: CalculationResult,
    confirmed_fact_ids: set[str],
    facts: Mapping[str, FinancialFactV2],
    evidences: Mapping[str, TableCellEvidence],
    verifications: Sequence[VerificationResult],
    report_year: int,
    comparability: AnnualComparabilityCheck | None,
) -> tuple[dict[str, Any], bool]:
    reasons: list[str] = []
    if calculation.status != CALC_SUCCEEDED or calculation.output_value is None:
        reasons.append("计算未成功，不能进入已确认区。")
    missing_inputs = [fact_id for fact_id in calculation.input_fact_ids if fact_id not in confirmed_fact_ids]
    if missing_inputs:
        reasons.append("输入事实未全部进入已确认指标。")
    blocking = [item for item in verifications if item.status != VERIFIED]
    if blocking:
        reasons.append("存在 conflict 或 insufficient_evidence 核验，不能进入已确认区。")
    independent_check = verify_annual_calculation(
        calculation,
        facts,
        verified_fact_ids=confirmed_fact_ids,
        report_year=report_year,
        comparability=comparability,
    )
    if independent_check.status != "verified":
        reasons.append("独立计算核验未通过：" + (independent_check.reason or "未能确认计算结果。"))
    confirmed = not reasons and independent_check.status == "verified"
    record = {
        "calculation_id": calculation.calculation_id,
        "formula_id": calculation.formula_id,
        "formula_expression": calculation.formula_expression,
        "input_fact_ids": list(calculation.input_fact_ids),
        "output_value": calculation.output_value,
        "unit": calculation.unit,
        "status": calculation.status,
        "failure_reason": calculation.failure_reason,
        "rule_version": calculation.rule_version,
        "verifications": [_verification_record(item) for item in verifications],
        "independent_verification": independent_check.to_dict(),
        "placement_reasons": [] if confirmed else reasons,
    }
    return record, confirmed


def _fact_record(
    fact: FinancialFactV2,
    evidences: Mapping[str, TableCellEvidence],
    verifications: Sequence[VerificationResult],
) -> dict[str, Any]:
    payload = fact.to_dict()
    payload["normalized_value"] = fact.normalized_value
    payload["raw_value"] = fact.raw_value
    payload["unit_multiplier"] = fact.unit_multiplier
    payload["evidences"] = [_evidence_record(evidences[evidence_id]) for evidence_id in fact.evidence_ids]
    cited_ids: list[str] = []
    for verification in verifications:
        for evidence_id in verification.evidence_ids:
            if evidence_id not in cited_ids:
                cited_ids.append(evidence_id)
    payload["verification_evidences"] = [_evidence_record(evidences[evidence_id]) for evidence_id in cited_ids]
    payload["verifications"] = [_verification_record(item) for item in verifications]
    return payload


def _evidence_record(evidence: TableCellEvidence) -> dict[str, Any]:
    payload = evidence.to_dict()
    if evidence.value_normalized is not None:
        payload["value_normalized"] = evidence.value_normalized
    if evidence.value_raw is not None:
        payload["value_raw"] = evidence.value_raw
    return payload


def _verification_record(verification: VerificationResult) -> dict[str, Any]:
    return verification.to_dict()


def _claim_record(
    claim: Claim,
    verifications: Sequence[VerificationResult],
    evidences: Mapping[str, TableCellEvidence],
    confirmed_fact_ids: set[str],
    confirmed_calculation_ids: set[str],
    confirmed_fact_evidence_ids: Mapping[str, set[str]],
    facts: Mapping[str, FinancialFactV2],
    calculations: Mapping[str, CalculationResult],
) -> dict[str, Any]:
    payload = claim.to_dict()
    payload["verifications"] = [_verification_record(item) for item in verifications]
    payload["evidences"] = [_evidence_record(evidences[evidence_id]) for evidence_id in claim.supporting_evidence_ids]
    independent_check = verify_claim(
        claim,
        facts,
        calculations,
        verified_fact_ids=confirmed_fact_ids,
        verified_calculation_ids=confirmed_calculation_ids,
        verified_fact_evidence_ids=confirmed_fact_evidence_ids,
    )
    payload["independent_verification"] = independent_check.to_dict()
    if claim.claim_type in _INTERPRETATION_TYPES and independent_check.status == "interpretation":
        payload["placement"] = "interpretations"
        payload["requires_alternative_explanations"] = True
        payload["requires_follow_up_items"] = True
        payload["placement_reasons"] = [independent_check.reason or "推论或假设单独列出。"]
        return payload
    if independent_check.status == "verified":
        payload["placement"] = "verified_claims"
        payload["placement_reasons"] = []
        return payload
    payload["placement"] = "pending_review"
    payload["placement_reasons"] = [
        independent_check.reason or "事实或计算主张未通过确定性 Claim 核验。"
    ]
    return payload


def _signal_record(signal: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json_safe(dict(signal))
    payload["placement"] = "pending_review"
    payload["fraud_conclusion"] = None
    payload["placement_reasons"] = ["候选或未验证线索只进入待核查，不确认舞弊。"]
    return payload


def _verification_supports_fact(
    fact: FinancialFactV2,
    verification: VerificationResult,
    evidences: Mapping[str, TableCellEvidence],
) -> bool:
    if verification.status != VERIFIED or not verification.evidence_ids:
        return False
    cited = [evidences[evidence_id] for evidence_id in verification.evidence_ids]
    if all(_is_legacy_text_only(evidence) for evidence in cited):
        return False
    return any(_evidence_matches_fact(fact, evidence) for evidence in cited)


def _confirmed_fact_evidence_ids(
    fact: FinancialFactV2,
    verifications: Sequence[VerificationResult],
    evidences: Mapping[str, TableCellEvidence],
) -> set[str]:
    """收集实际匹配事实的已核验来源证据，供 Claim 检查引用绑定。"""

    return {
        evidence_id
        for verification in verifications
        if verification.status == VERIFIED
        for evidence_id in verification.evidence_ids
        if _evidence_matches_fact(fact, evidences[evidence_id])
    }


def _is_legacy_text_only(evidence: TableCellEvidence) -> bool:
    if _NO_VERIFIED_VALUE in evidence.limitations or evidence.value_region is None:
        return True
    return any(
        getattr(evidence, name) is None
        for name in ("row_region", "column_region", "title_region", "unit_region")
    )


def _evidence_matches_fact(fact: FinancialFactV2, evidence: TableCellEvidence) -> bool:
    if _is_legacy_text_only(evidence):
        return False
    if evidence.value_raw != fact.raw_value or evidence.value_normalized != fact.normalized_value:
        return False
    if not _currency_matches(fact.currency, evidence.currency) or not _unit_matches_multiplier(fact, evidence):
        return False
    if evidence.period_start != fact.period_start or evidence.period_end != fact.period_end:
        return False
    if evidence.period_type != fact.period_type or fact.period_end is None:
        return False
    if evidence.value_region is None or not contains_complete_raw_token(evidence.value_region.text, fact.raw_value):
        return False
    if evidence.row_region is None or not _row_matches_metric(fact.metric_id, evidence.row_region.text):
        return False
    if evidence.column_region is None or str(fact.period_end.year) not in evidence.column_region.text:
        return False
    if evidence.title_region is None or evidence.unit_region is None:
        return False
    return _scope_matches(fact, evidence)


def _unit_matches_multiplier(fact: FinancialFactV2, evidence: TableCellEvidence) -> bool:
    if evidence.unit_region is None or evidence.unit is None:
        return False
    from_region = _multiplier_from_unit_text(evidence.unit_region.text)
    from_unit = _multiplier_from_unit_text(evidence.unit)
    if from_region is None or from_region != from_unit:
        return False
    try:
        expected = Decimal(fact.unit_multiplier)
    except Exception:
        return False
    if not expected.is_finite() or expected != from_region:
        return False
    return fact.unit is None or fact.unit == evidence.unit


def _currency_matches(fact_currency: str, evidence_currency: str | None) -> bool:
    if evidence_currency == fact_currency:
        return True
    return {fact_currency, evidence_currency} == {"人民币", "CNY"}


def _multiplier_from_unit_text(text: str) -> Decimal | None:
    compact = _compact(text)
    if "亿元" in compact:
        return Decimal("100000000")
    if "万元" in compact:
        return Decimal("10000")
    if "元" in compact:
        return Decimal("1")
    return None


def _row_matches_metric(metric_id: str, text: str) -> bool:
    compact = _compact(text)
    if metric_id == "revenue":
        return "营业收入" in compact and "扣除" not in compact
    if metric_id == "net_profit_parent":
        return "归属于母公司股东的净利润" in compact and "扣除" not in compact
    if metric_id == "operating_cash_flow":
        return "经营活动产生的现金流量净额" in compact and "小计" not in compact
    if metric_id == "non_recurring_total":
        return compact == "合计"
    return False


def _scope_matches(fact: FinancialFactV2, evidence: TableCellEvidence) -> bool:
    if evidence.title_region is None:
        return False
    title = _compact(evidence.title_region.text)
    table = _compact(evidence.table_title or "")
    if fact.scope == SCOPE_CONSOLIDATED:
        return "合并" in title and "母公司" not in title and "合并" in table and "母公司" not in table
    if fact.scope == SCOPE_PARENT:
        return "母公司" in title and "合并" not in title and "母公司" in table and "合并" not in table
    if fact.scope == SCOPE_UNKNOWN and fact.metric_id == "non_recurring_total":
        canonical = _compact("非经常性损益项目和金额")
        return _non_recurring_title(title) == canonical and _non_recurring_title(table) == canonical
    return False


def _non_recurring_title(text: str) -> str:
    compact = _compact(text)
    return re.sub(r"^(?:[一二三四五六七八九十百千]+、|\d+[、.．])", "", compact)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _require_target(
    verification: VerificationResult,
    facts: Mapping[str, FinancialFactV2],
    calculations: Mapping[str, CalculationResult],
    claims: Mapping[str, Claim],
    evidences: Mapping[str, TableCellEvidence],
) -> None:
    kind = _target_kind(verification.target_type)
    pools = {
        "fact": facts,
        "calculation": calculations,
        "claim": claims,
        "evidence": evidences,
    }
    pool = pools.get(kind)
    if pool is None or verification.target_id not in pool:
        raise ReportBuildError(
            f"引用缺失：核验 {verification.verification_id} 的 {verification.target_type} {verification.target_id}。"
        )


def _target_kind(target_type: str) -> str:
    if target_type in _FACT_TYPES:
        return "fact"
    if target_type in _CALC_TYPES:
        return "calculation"
    if target_type in _CLAIM_TYPES:
        return "claim"
    if target_type == "evidence":
        return "evidence"
    return target_type


def _normalize_signals(
    candidate_signals: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if candidate_signals is None:
        return []
    if isinstance(candidate_signals, Mapping):
        items = candidate_signals.get("items", [candidate_signals])
    elif isinstance(candidate_signals, Sequence) and not isinstance(candidate_signals, str):
        items = candidate_signals
    else:
        raise ReportBuildError("candidate_signals 必须是映射或映射序列。")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ReportBuildError("候选线索必须是映射。")
        signal_id = item.get("signal_id", item.get("id"))
        if not isinstance(signal_id, str) or signal_id.strip() == "":
            raise ReportBuildError("候选线索缺少 signal_id。")
        if signal_id in seen:
            raise ReportBuildError(f"候选线索重复：{signal_id}。")
        seen.add(signal_id)
        payload = _json_safe(dict(item))
        payload["signal_id"] = signal_id
        normalized.append(payload)
    normalized.sort(key=lambda entry: entry["signal_id"])
    return normalized


def _gaps(
    facts: Sequence[FinancialFactV2],
    verifications: Sequence[VerificationResult],
    metrics: Sequence[Mapping[str, Any]],
    analyses: Sequence[Mapping[str, Any]],
    signals: Sequence[Mapping[str, Any]],
) -> list[str]:
    gaps: list[str] = []
    if not facts:
        gaps.append("未提供财务事实，不能形成已确认指标。")
    if not verifications:
        gaps.append("未提供核验结果，已确认区保持为空。")
    if facts and not metrics:
        gaps.append("没有任何事实同时满足 verified、证据引用和交叉对应。")
    if not analyses:
        gaps.append("没有已确认计算。")
    if any(item.get("status") == "candidate" for item in signals):
        gaps.append("候选异常只在待核查区列出，不写成舞弊结论。")
    return gaps


def _metric_lines(metric: Mapping[str, Any], *, pending: bool = False) -> list[str]:
    heading = "待核查事实" if pending else "已确认事实"
    lines = [
        "",
        f"### {heading} `{metric['fact_id']}`",
        "",
        f"- 指标：{metric['label_raw']}（`{metric['metric_id']}`）",
        f"- 原始金额：`{metric['raw_value']}`",
        f"- 规范金额：`{metric['normalized_value']}`",
        f"- 单位倍率：`{metric['unit_multiplier']}`",
        f"- 单位：{metric['unit'] if metric['unit'] is not None else '未提供'}",
        f"- 币种：{metric['currency']}",
        f"- 期间：{metric['period_start'] or '未提供'} 至 {metric['period_end'] or '未提供'}（{metric['period_type'] or '未提供'}）",
        f"- 口径：{metric['scope']}；比较角色：{metric['comparison_role']}",
    ]
    for evidence in metric["evidences"]:
        lines.append(f"- 提取引用：{_page_text(evidence)}")
    for evidence in metric.get("verification_evidences", []):
        lines.append(f"- 核验引用：{_page_text(evidence)}")
    for verification in metric["verifications"]:
        lines.append(f"- 核验 `{verification['verification_id']}`：{verification['status']}")
        lines.extend(f"  - 分项：{check}" for check in verification["checks"])
        lines.extend(f"  - 冲突：{conflict}" for conflict in verification["conflicts"])
        lines.extend(f"  - 核验限制：{item}" for item in verification["limitations"])
    lines.extend(f"- 限制：{item}" for item in metric["limitations"])
    lines.extend(f"- 分区原因：{item}" for item in metric.get("placement_reasons", []))
    return lines


def _analysis_lines(analysis: Mapping[str, Any], *, pending: bool = False) -> list[str]:
    heading = "待核查计算" if pending else "已确认计算"
    lines = [
        "",
        f"### {heading} `{analysis['calculation_id']}`",
        "",
        f"- 公式编号：`{analysis['formula_id']}`",
        f"- 公式：`{analysis['formula_expression']}`",
        f"- 结果：`{analysis['output_value'] if analysis['output_value'] is not None else '无'}`",
        f"- 单位：{analysis['unit'] if analysis['unit'] is not None else '未提供'}",
        f"- 输入事实：{', '.join(f'`{fact_id}`' for fact_id in analysis['input_fact_ids']) or '无'}",
    ]
    for verification in analysis["verifications"]:
        lines.append(f"- 核验 `{verification['verification_id']}`：{verification['status']}")
        lines.extend(f"  - 分项：{check}" for check in verification["checks"])
        lines.extend(f"  - 核验限制：{item}" for item in verification["limitations"])
    independent = analysis.get("independent_verification")
    if independent is not None:
        lines.append(f"- 独立重算核验：{independent['status']}")
        if independent["recomputed_value"] is not None:
            lines.append(f"- 独立重算值：`{independent['recomputed_value']}`")
        if independent["reason"] is not None:
            lines.append(f"- 独立核验原因：{independent['reason']}")
    if analysis["failure_reason"] is not None:
        lines.append(f"- 失败原因：{analysis['failure_reason']}")
    lines.extend(f"- 分区原因：{item}" for item in analysis["placement_reasons"])
    return lines


def _claim_lines(claim: Mapping[str, Any]) -> list[str]:
    heading = {
        "verified_claims": "已核实主张",
        "interpretations": "推论或假设",
    }.get(claim["placement"], "待核查主张")
    lines = [
        "",
        f"### {heading} `{claim['claim_id']}`（{claim['claim_type']}）",
        "",
        claim["text"],
        "",
        f"- 核验状态：{claim.get('independent_verification', {}).get('status', claim['verification_status'])}",
    ]
    if claim["claim_type"] in {CLAIM_INFERENCE, CLAIM_HYPOTHESIS} or claim["alternative_explanations"] or claim["follow_up_items"]:
        alternatives = claim["alternative_explanations"] or ["未提供替代解释。"]
        follow_ups = claim["follow_up_items"] or ["未提供后续核查项。"]
        lines.append("- 替代解释：")
        lines.extend(f"  - {item}" for item in alternatives)
        lines.append("- 后续核查：")
        lines.extend(f"  - {item}" for item in follow_ups)
    lines.extend(f"- 限制：{item}" for item in claim["limitations"])
    for verification in claim["verifications"]:
        lines.append(f"- 核验 `{verification['verification_id']}`：{verification['status']}")
        lines.extend(f"  - 核验限制：{item}" for item in verification["limitations"])
    independent = claim.get("independent_verification")
    if independent is not None:
        lines.append(f"- 独立主张核验：{independent['status']}")
        if independent["reason"] is not None:
            lines.append(f"- 独立核验原因：{independent['reason']}")
        if independent["expected_text"] is not None and independent["status"] != "verified":
            lines.append(f"- 受控文本应为：{independent['expected_text']}")
    lines.extend(f"- 分区原因：{item}" for item in claim["placement_reasons"])
    return lines


def _signal_lines(signal: Mapping[str, Any], facts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    title = signal.get("title") or signal["signal_id"]
    lines = [
        "",
        f"### 待核查候选 `{signal['signal_id']}`",
        "",
        f"- 标题：{title}",
        f"- 状态：{signal.get('status', '未提供')}",
        "- 这是候选线索，不是确认舞弊。",
    ]
    if "reason" in signal:
        lines.append(f"- 筛查原因：{signal['reason'] or '上游未提供'}")
    for key, label in (("left_difference", "左侧差额"), ("right_difference", "右侧差额")):
        if key in signal:
            value = signal[key]
            lines.append(f"- {label}：`{value if value is not None else '未提供'}`")
    if signal.get("formula") is not None:
        lines.append(f"- 公式：`{signal['formula']}`")
    if signal.get("value") is not None:
        lines.append(f"- 数值：`{signal['value']}`")
    calculation_ids = signal.get("calculation_ids", [])
    if calculation_ids:
        lines.append(f"- 计算引用：{', '.join(f'`{item}`' for item in calculation_ids)}")
    fact_ids = signal.get("input_fact_ids", [])
    if fact_ids:
        lines.append(f"- 输入事实：{', '.join(f'`{item}`' for item in fact_ids)}")
        cited: set[str] = set()
        for fact_id in fact_ids:
            fact = facts.get(fact_id)
            if fact is None:
                continue
            evidence_records = fact.get("verification_evidences") or fact.get("evidences", [])
            for evidence in evidence_records:
                evidence_id = evidence.get("evidence_id")
                if not isinstance(evidence_id, str) or evidence_id in cited:
                    continue
                cited.add(evidence_id)
                lines.append(f"  - 引用 `{evidence_id}`：{_page_text(evidence)}")
    lines.extend(f"- 分区原因：{item}" for item in signal["placement_reasons"])
    return lines


def _page_text(evidence: Mapping[str, Any]) -> str:
    regions = []
    for name in ("value_region", "row_region", "column_region", "title_region", "unit_region", "legacy_text_region"):
        region = evidence.get(name)
        if isinstance(region, Mapping):
            regions.append(f"{name} {_region_text(region)}")
    row = evidence.get("row_label") or "行未提供"
    column = evidence.get("column_label") or "列未提供"
    period = f"{evidence.get('period_start') or '期间起未提供'} 至 {evidence.get('period_end') or '期间止未提供'}"
    unit = evidence.get("unit") or "单位未提供"
    page = evidence["pdf_page"]
    printed = evidence.get("printed_page")
    printed_text = f"，印刷页 {printed}" if printed is not None else ""
    pdf_page = f"PDF 第{page}页"
    detail = "；".join(regions) if regions else "没有区域坐标"
    return f"{pdf_page}{printed_text}；行：{row}；列：{column}；期间：{period}；单位：{unit}；{detail}"


def _region_text(region: Mapping[str, Any]) -> str:
    bbox = region["bbox"]
    page = region["page"]
    return (
        f"PDF 第{page}页，坐标 bbox "
        f"({bbox['x0']}, {bbox['y0']}, {bbox['x1']}, {bbox['y1']})：{region['text']}"
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        raise ReportBuildError("报告不接受浮点数，金额请用十进制字符串。")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise ReportBuildError("候选线索含无法精确写入报告的值。")


def _same_source(document_id: str, digest: str, expected_document: str, expected_digest: str, label: str) -> None:
    if document_id != expected_document or digest.lower() != expected_digest.lower():
        raise ReportBuildError(f"文档或哈希不一致：{label}。")


def _require_known_ids(ids: Sequence[str], pool: Mapping[str, Any], label: str) -> None:
    for item_id in ids:
        if item_id not in pool:
            raise ReportBuildError(f"引用缺失：{label} 引用了不存在的 {item_id}。")


def _index(items: Sequence[Any], field: str, label: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for item in items:
        item_id = getattr(item, field)
        if item_id in indexed:
            raise ReportBuildError(f"{label}标识重复：{item_id}。")
        indexed[item_id] = item
    return indexed


def _typed_tuple(values: Sequence[Any], kind: type, label: str) -> tuple[Any, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ReportBuildError(f"{label} 必须是序列。")
    for item in values:
        if not isinstance(item, kind):
            raise ReportBuildError(f"{label} 含有 {kind.__name__} 以外的对象。")
    return tuple(values)


def _text_list(values: Sequence[str], label: str) -> list[str]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ReportBuildError(f"{label} 必须是字符串序列。")
    items: list[str] = []
    for item in values:
        if not isinstance(item, str) or item.strip() == "":
            raise ReportBuildError(f"{label} 中的每一项都必须是非空字符串。")
        items.append(item)
    return items


def _require_id(value: object, label: str) -> None:
    if not isinstance(value, str) or value.strip() == "" or any(char.isspace() for char in value):
        raise ReportBuildError(f"{label} 必须是不含空白的标识。")


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or value.strip() == "":
        raise ReportBuildError(f"{label} 不能为空。")
