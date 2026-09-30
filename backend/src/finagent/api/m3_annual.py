"""在同一进程中执行 M3 年度字段核验与四规则筛查。"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

from finagent.finance.m3_screening import RULE_VERSION, screen_m3_annual_rules
from finagent.ingestion.v2_balance_sheet import extract_v2_balance_sheet_facts
from finagent.ingestion.v2_income_statement import extract_v2_income_statement_facts
from finagent.ingestion.v2_key_financial_data import extract_v2_key_financial_data_facts
from finagent.schemas.financial_fact_v2 import (
    FinancialFactV2,
    TableCellEvidence,
    VerificationResult,
)
from finagent.schemas.text_pdf import ParsedTextPdf
from finagent.verification.comparability import verify_annual_comparability
from finagent.verification.independent_fact import verify_financial_fact
from finagent.verification.m3_semantic_mapping import verify_parent_profit_semantic_mapping

_UNIT_MULTIPLIERS = {"元": "1", "万元": "10000", "亿元": "100000000"}
_UNIT_BOUND_METRICS = (
    "revenue",
    "net_profit_parent",
    "operating_cash_flow",
    "non_recurring_total",
)


def run_m3_annual_screening(
    source_pdf_path: str | Path,
    parsed_pdf: ParsedTextPdf,
    *,
    company_id: str,
    report_year: int,
    annual_facts: Sequence[FinancialFactV2],
    annual_evidences: Sequence[TableCellEvidence],
    annual_verifications: Sequence[VerificationResult],
) -> dict[str, Any]:
    """提取五项 M3 字段、独立复核并运行规则，所有 proof 均在本次调用签发。

    AnnualComparabilityCheck 和 ParentProfitSemanticMapping 只在此调用中保留为
    可用对象；返回值中的序列化副本只供审计，不能作为其他进程的 proof。
    """

    pdf_path = Path(source_pdf_path)
    if parsed_pdf.source_sha256.lower() != _sha256(pdf_path).lower():
        raise ValueError("M3 ParsedTextPdf 与归档 PDF 的来源 SHA256 不一致。")
    if parsed_pdf.document_id.strip() == "":
        raise ValueError("M3 来源 document_id 不能为空。")

    extraction_results = (
        extract_v2_balance_sheet_facts(
            parsed_pdf, company_id=company_id, report_year=report_year
        ),
        extract_v2_income_statement_facts(
            parsed_pdf, company_id=company_id, report_year=report_year
        ),
        extract_v2_key_financial_data_facts(
            parsed_pdf,
            source_pdf_path=pdf_path,
            company_id=company_id,
            report_year=report_year,
        ),
    )
    extracted_facts = [fact for result in extraction_results for fact in result.facts]
    extraction_evidences = [evidence for result in extraction_results for evidence in result.evidence]
    extraction_issues = [asdict(issue) for result in extraction_results for issue in result.issues]

    unit_facts, unit_evidences, unit_issues = _unit_bound_annual_facts(
        pdf_path,
        tuple(annual_facts),
        tuple(annual_evidences),
        tuple(annual_verifications),
    )

    m3_facts = [*extracted_facts, *unit_facts]
    verifications: list[VerificationResult] = []
    independent_evidences: list[TableCellEvidence] = []
    verification_errors: list[dict[str, str]] = []
    for fact in m3_facts:
        try:
            checked = verify_financial_fact(pdf_path, fact)
        except Exception as exc:
            verification_errors.append(
                {
                    "fact_id": fact.fact_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        verifications.append(checked.result)
        independent_evidences.extend(checked.evidence)

    audit_only_fact_ids = {
        fact.fact_id
        for fact in unit_facts
        if fact.metric_id == "non_recurring_total"
    }
    required_unit_issues = [
        issue for issue in unit_issues if issue.get("metric_id") != "non_recurring_total"
    ]
    required_verification_errors = [
        issue
        for issue in verification_errors
        if issue.get("fact_id") not in audit_only_fact_ids
    ]

    comparability = verify_annual_comparability(
        pdf_path,
        document_id=parsed_pdf.document_id,
        source_sha256=parsed_pdf.source_sha256,
        report_year=report_year,
    )
    parent_pair = tuple(
        fact for fact in unit_facts if fact.metric_id == "net_profit_parent"
    )
    key_pair = tuple(
        fact for fact in extracted_facts if fact.metric_id == "net_profit_parent_ex_nonrecurring"
    )
    semantic_check = verify_parent_profit_semantic_mapping(
        pdf_path,
        key_facts=key_pair,
        parent_facts=parent_pair,
        verifications=verifications,
        report_year=report_year,
    )
    screening = screen_m3_annual_rules(
        m3_facts,
        verifications,
        report_year,
        comparability=comparability,
        parent_profit_mapping=semantic_check.proof,
    )

    status = (
        "completed"
        if screening.status == "completed"
        and not extraction_issues
        and not required_unit_issues
        and not required_verification_errors
        else "abstained"
    )
    return {
        "kind": "fintrace_m3_annual_screening",
        "rule_version": RULE_VERSION,
        "status": status,
        "company_id": company_id,
        "report_year": report_year,
        "source_document_id": parsed_pdf.document_id,
        "source_sha256": parsed_pdf.source_sha256.lower(),
        "model_called": False,
        "extraction": {
            "expected_new_fact_count": 10,
            "new_fact_count": len(extracted_facts),
            "facts": [item.to_dict() for item in extracted_facts],
            "evidences": [item.to_dict() for item in extraction_evidences],
            "issues": extraction_issues,
        },
        "explicit_unit_facts": {
            "metrics": list(_UNIT_BOUND_METRICS),
            "fact_count": len(unit_facts),
            "facts": [item.to_dict() for item in unit_facts],
            "evidences": [item.to_dict() for item in unit_evidences],
            "issues": unit_issues,
            "audit_only_metrics": ["non_recurring_total"],
            "audit_only_fact_ids": sorted(audit_only_fact_ids),
            "unit_basis": "同源 PDF 原文独立核验结果中的单位区域、币种和金额；随后对带单位副本重新独立核验。",
            "screening_note": "披露的非经常性损益合计副本只用于单位证据审计，不输入规则二，也不替代扣非归母净利润。",
        },
        "verification": {
            "results": [item.to_dict() for item in verifications],
            "independent_evidences": [item.to_dict() for item in independent_evidences],
            "errors": verification_errors,
        },
        "comparability_proof_audit": {
            "status": comparability.status,
            "usable_in_current_process": comparability.status == "verified",
            "proof": comparability.to_dict(),
            "serialization_note": "此对象仅为当前进程 proof 的审计副本，JSON 反序列化结果不能授权筛查。",
        },
        "parent_profit_semantic_mapping_audit": {
            "status": "verified" if semantic_check.proof is not None else "abstained",
            "proof": None if semantic_check.proof is None else semantic_check.proof.to_dict(),
            "issues": list(semantic_check.issues),
            "serialization_note": "此对象仅为当前进程 proof 的审计副本，JSON 反序列化结果不能授权筛查。",
        },
        "screening": screening.to_dict(),
        "limitations": [
            "规则阈值仍为试行值，尚未通过隔离评测校准或冻结。",
            "规则二的跨表语义 proof 仅支持海天 603288 2024 年报版式。",
            "风险分数只提示进一步核查，不是舞弊事实或舞弊结论。",
        ],
    }


def failed_m3_annual_screening(
    *,
    company_id: str,
    report_year: int,
    document_id: str,
    source_sha256: str,
    error: BaseException,
) -> dict[str, Any]:
    """构造明确失败的 M3 审计对象，保留已成功的 M2 报告。"""

    return {
        "kind": "fintrace_m3_annual_screening",
        "rule_version": RULE_VERSION,
        "status": "failed",
        "company_id": company_id,
        "report_year": report_year,
        "source_document_id": document_id,
        "source_sha256": source_sha256.lower(),
        "model_called": False,
        "extraction": {"expected_new_fact_count": 10, "new_fact_count": 0, "facts": [], "evidences": [], "issues": []},
        "explicit_unit_facts": {"metrics": list(_UNIT_BOUND_METRICS), "fact_count": 0, "facts": [], "evidences": [], "issues": [], "screening_note": "披露的非经常性损益合计不会替代扣非归母净利润。"},
        "verification": {"results": [], "independent_evidences": [], "errors": []},
        "comparability_proof_audit": {"status": "not_run", "usable_in_current_process": False, "proof": None},
        "parent_profit_semantic_mapping_audit": {"status": "not_run", "proof": None, "issues": []},
        "screening": {
            "report_year": report_year,
            "rule_version": RULE_VERSION,
            "status": "abstained",
            "total_score": None,
            "maximum_score": 100,
            "rules": [],
            "limitations": ["规则编排失败，没有形成可用的四规则结果。"],
        },
        "failure": {"error_type": type(error).__name__, "reason": str(error)},
        "limitations": ["M3 运行失败；不得把缺失结果解释为零分或通过。"],
    }


def _unit_bound_annual_facts(
    pdf_path: Path,
    annual_facts: tuple[FinancialFactV2, ...],
    annual_evidences: tuple[TableCellEvidence, ...],
    annual_verifications: tuple[VerificationResult, ...],
) -> tuple[list[FinancialFactV2], list[TableCellEvidence], list[dict[str, str]]]:
    evidence_by_id = {item.evidence_id: item for item in annual_evidences}
    facts_by_metric: dict[str, list[FinancialFactV2]] = {}
    for fact in annual_facts:
        if fact.metric_id in _UNIT_BOUND_METRICS:
            facts_by_metric.setdefault(fact.metric_id, []).append(fact)
    verifications_by_fact: dict[str, list[VerificationResult]] = {}
    for check in annual_verifications:
        verifications_by_fact.setdefault(check.target_id, []).append(check)

    facts: list[FinancialFactV2] = []
    evidences: list[TableCellEvidence] = []
    issues: list[dict[str, str]] = []
    for metric_id in _UNIT_BOUND_METRICS:
        metric_facts = facts_by_metric.get(metric_id, [])
        if len(metric_facts) != 2:
            issues.append(
                {
                    "metric_id": metric_id,
                    "code": "annual_fact_pair_missing",
                    "message": f"M2 中 {metric_id} 事实数为 {len(metric_facts)}，需要 current/comparative 唯一一对。",
                }
            )
            continue
        if {item.comparison_role for item in metric_facts} != {"current", "comparative"}:
            issues.append(
                {"metric_id": metric_id, "code": "annual_fact_pair_ambiguous", "message": "M2 事实的 current/comparative 角色不唯一。"}
            )
            continue
        for original in metric_facts:
            matching_checks = verifications_by_fact.get(original.fact_id, [])
            if len(matching_checks) != 1 or matching_checks[0].status != "verified":
                issues.append(
                    {
                        "metric_id": metric_id,
                        "code": "annual_fact_not_independently_verified",
                        "message": f"{original.fact_id} 缺少唯一的 M2 原 PDF verified 结果，不能据此确定单位。",
                    }
                )
                continue
            check = matching_checks[0]
            source_units = [
                evidence_by_id[item_id]
                for item_id in check.evidence_ids
                if item_id in evidence_by_id
            ]
            source_units = [
                evidence
                for evidence in source_units
                if evidence.value_region is not None
                and evidence.unit_region is not None
                and evidence.unit in _UNIT_MULTIPLIERS
                and evidence.currency is not None
                and evidence.value_normalized == original.normalized_value
                and evidence.document_id == original.source_document_id
                and evidence.source_sha256.lower() == original.source_sha256.lower()
                and _UNIT_MULTIPLIERS[evidence.unit] == original.unit_multiplier
            ]
            if len(source_units) != 1:
                issues.append(
                    {
                        "metric_id": metric_id,
                        "code": "independent_unit_evidence_missing",
                        "message": f"{original.fact_id} 的原 PDF 独立核验证据没有唯一、金额相符的明确单位区域。",
                    }
                )
                continue
            source_evidence = source_units[0]
            extracted = [
                evidence_by_id[item_id]
                for item_id in original.evidence_ids
                if item_id in evidence_by_id
            ]
            extracted = [
                evidence
                for evidence in extracted
                if evidence.value_region is not None
                and evidence.value_normalized == original.normalized_value
                and evidence.value_raw == original.raw_value
            ]
            if len(extracted) != 1:
                issues.append(
                    {
                        "metric_id": metric_id,
                        "code": "extraction_evidence_missing",
                        "message": f"{original.fact_id} 没有唯一匹配的 M2 提取金额引用，不能形成带单位副本。",
                    }
                )
                continue

            new_id = f"m3-unit-bound:{original.fact_id}"
            extraction_evidence_id = f"m3-extract:{original.fact_id}"
            unit = source_evidence.unit
            currency = source_evidence.currency
            assert unit is not None and currency is not None
            unit_evidence = replace(
                extracted[0],
                evidence_id=extraction_evidence_id,
                unit=unit,
                currency=currency,
                limitations=(
                    *extracted[0].limitations,
                    "M3 显式单位来自同源 PDF 原文独立核验证据的单位区域。",
                ),
            )
            explicit_fact = replace(
                original,
                fact_id=new_id,
                unit=unit,
                currency=currency,
                evidence_ids=(extraction_evidence_id,),
                extraction_method="m3_unit_bound_copy_from_independent_pdf_evidence",
                limitations=(
                    *original.limitations,
                    "M3 使用前已从原 PDF 独立核验证据确定单位，并对显式单位副本再次独立核验。",
                ),
            )
            facts.append(explicit_fact)
            evidences.append(unit_evidence)
    return facts, evidences, issues


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
