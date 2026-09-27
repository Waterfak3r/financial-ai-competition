"""从原始年度报告 PDF 运行确定性 v2 分析并组装报告。

本模块不调用模型，也不写磁盘。CLI 负责固定输入、归档中间证据和输出报告。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finagent.finance.v2_calculation import (
    RULE_VERSION,
    V2CalculationIssue,
    calculate_v2_annual_changes,
)
from finagent.finance.v2_screening import screen_v2_annual_candidates
from finagent.ingestion import extract_annual_financial_facts, parse_text_pdf
from finagent.reports.annual_report import build_annual_report, render_markdown
from finagent.schemas.financial_fact import ExtractionIssue, FinancialFact
from finagent.schemas.financial_fact_v2 import (
    CALC_SUCCEEDED,
    CLAIM_CALCULATION,
    CLAIM_FACT,
    VERIFIED,
    CalculationResult,
    Claim,
    FinancialFactV2,
    TableCellEvidence,
    VerificationResult,
    adapt_legacy_financial_fact,
)
from finagent.schemas.text_pdf import ParsedTextPdf
from finagent.verification.claim import ClaimCheck, expected_calculation_claim_text, expected_fact_claim_text, verify_claim
from finagent.verification.comparability import AnnualComparabilityCheck, verify_annual_comparability
from finagent.verification.independent_calculation import (
    IndependentCalculationCheck,
    verify_annual_calculation,
)
from finagent.verification.independent_fact import verify_financial_fact


@dataclass(frozen=True, slots=True)
class AnnualAnalysisResult:
    """完整的内存分析结果，供运行归档与报告输出使用。"""

    run_id: str
    company_id: str
    report_year: int
    parsed_pdf: ParsedTextPdf
    extracted_facts: tuple[FinancialFact, ...]
    extraction_issues: tuple[ExtractionIssue, ...]
    facts: tuple[FinancialFactV2, ...]
    evidences: tuple[TableCellEvidence, ...]
    fact_verifications: tuple[VerificationResult, ...]
    comparability: AnnualComparabilityCheck
    calculations: tuple[CalculationResult, ...]
    calculation_issues: tuple[V2CalculationIssue, ...]
    calculation_checks: tuple[IndependentCalculationCheck, ...]
    calculation_verifications: tuple[VerificationResult, ...]
    screening: tuple[dict[str, object], ...]
    claims: tuple[Claim, ...]
    claim_checks: tuple[ClaimCheck, ...]
    claim_verifications: tuple[VerificationResult, ...]
    claim_generation_issues: tuple[dict[str, str], ...]
    report: dict[str, Any]
    markdown: str

    @property
    def verification_results(self) -> tuple[VerificationResult, ...]:
        return (*self.fact_verifications, *self.calculation_verifications, *self.claim_verifications)

    @property
    def status(self) -> str:
        if self.extraction_issues:
            return "completed_with_issues"
        if any(item.status != VERIFIED for item in self.fact_verifications):
            return "completed_with_issues"
        if self.comparability.status != "verified":
            return "completed_with_issues"
        if self.calculation_issues or any(item.status != "verified" for item in self.calculation_checks):
            return "completed_with_issues"
        if self.claim_generation_issues or any(item.status != VERIFIED for item in self.claim_verifications):
            return "completed_with_issues"
        if any(item.get("status") == "abstained" for item in self.screening):
            return "completed_with_issues"
        return "completed"

    def archive_dict(self) -> dict[str, Any]:
        """返回完整 JSON 归档对象；PDF 和逐页解析结果由 CLI 单独保存。"""

        fact_verification_issues = [
            _verification_issue(item)
            for item in self.fact_verifications
            if item.status != VERIFIED
        ]
        calculation_verification_issues = [
            {
                "calculation_id": item.calculation_id,
                "status": item.status,
                "reason": item.reason,
            }
            for item in self.calculation_checks
            if item.status != "verified"
        ]
        claim_check_issues = [
            item.to_dict()
            for item in self.claim_checks
            if item.status not in {"verified", "interpretation"}
        ]
        return {
            "run_id": self.run_id,
            "company_id": self.company_id,
            "report_year": self.report_year,
            "status": self.status,
            "model_called": False,
            "rule_version": RULE_VERSION,
            "extraction": {
                "issues": [_extraction_issue_dict(item) for item in self.extraction_issues],
                "facts": [asdict(item) for item in self.extracted_facts],
            },
            "facts": [item.to_dict() for item in self.facts],
            "evidences": [item.to_dict() for item in self.evidences],
            "verifications": [item.to_dict() for item in self.verification_results],
            "comparability": self.comparability.to_dict(),
            "calculations": [item.to_dict() for item in self.calculations],
            "calculation_checks": [item.to_dict() for item in self.calculation_checks],
            "calculation_issues": [_calculation_issue_dict(item) for item in self.calculation_issues],
            "screening": [_json_safe(item) for item in self.screening],
            "claims": [item.to_dict() for item in self.claims],
            "claim_checks": [item.to_dict() for item in self.claim_checks],
            "claim_generation_issues": list(self.claim_generation_issues),
            "issues": {
                "fact_verification": fact_verification_issues,
                "comparability": []
                if self.comparability.status == "verified"
                else [
                    {
                        "status": self.comparability.status,
                        "conflicts": list(self.comparability.conflicts),
                        "limitations": list(self.comparability.limitations),
                    }
                ],
                "calculation": [_calculation_issue_dict(item) for item in self.calculation_issues],
                "calculation_verification": calculation_verification_issues,
                "screening": [
                    _json_safe(item)
                    for item in self.screening
                    if item.get("status") in {"abstained", "not_triggered"}
                ],
                "claim_generation": list(self.claim_generation_issues),
                "claim_verification": claim_check_issues,
            },
        }


def analyze_annual_pdf(
    source_pdf: str | Path,
    *,
    run_id: str,
    company_id: str,
    report_year: int,
    document_id: str,
) -> AnnualAnalysisResult:
    """从 PDF 原文走完 v2 事实、核验、计算、筛查、Claim 和报告流程。"""

    if not isinstance(company_id, str) or not company_id.strip():
        raise ValueError("company_id 不能为空。")
    if type(report_year) is not int or not 1900 <= report_year <= 2100:
        raise ValueError("report_year 必须是 1900 到 2100 之间的整数。")
    parsed = parse_text_pdf(source_pdf, document_id=document_id)
    extraction = extract_annual_financial_facts(
        parsed,
        company_id=company_id,
        report_year=report_year,
    )

    facts: list[FinancialFactV2] = []
    evidences: list[TableCellEvidence] = []
    fact_verifications: list[VerificationResult] = []
    independent_evidence_ids_by_fact: dict[str, set[str]] = {}
    for legacy_fact in extraction.facts:
        fact, legacy_evidence = adapt_legacy_financial_fact(legacy_fact, report_year=report_year)
        checked = verify_financial_fact(source_pdf, fact)
        facts.append(fact)
        evidences.extend(legacy_evidence)
        evidences.extend(checked.evidence)
        fact_verifications.append(checked.result)
        if checked.result.status == VERIFIED:
            independent_evidence_ids_by_fact[fact.fact_id] = set(checked.result.evidence_ids)

    comparability = verify_annual_comparability(
        source_pdf,
        document_id=parsed.document_id,
        source_sha256=parsed.source_sha256,
        report_year=report_year,
    )
    calculations, calculation_issues = calculate_v2_annual_changes(
        facts,
        fact_verifications,
        report_year,
        comparability=comparability,
    )
    facts_by_id = {item.fact_id: item for item in facts}
    verified_fact_ids = set(independent_evidence_ids_by_fact)
    calculation_checks = tuple(
        verify_annual_calculation(
            calculation,
            facts_by_id,
            verified_fact_ids=verified_fact_ids,
            report_year=report_year,
            comparability=comparability,
        )
        for calculation in calculations
    )
    calculation_check_by_id = {item.calculation_id: item for item in calculation_checks}
    calculation_verifications = tuple(
        _calculation_verification(
            calculation,
            calculation_check_by_id[calculation.calculation_id],
            independent_evidence_ids_by_fact,
        )
        for calculation in calculations
    )
    screening = screen_v2_annual_candidates(
        facts,
        calculations,
        comparability=comparability,
    )

    claims, claim_checks, claim_verifications, claim_issues = _canonical_claims(
        facts=facts,
        calculations=calculations,
        fact_verifications=fact_verifications,
        calculation_checks=calculation_checks,
        independent_evidence_ids_by_fact=independent_evidence_ids_by_fact,
    )
    report = build_annual_report(
        run_id=run_id,
        company_id=company_id,
        report_year=report_year,
        source_document_id=parsed.document_id,
        source_sha256=parsed.source_sha256,
        facts=tuple(facts),
        evidences=tuple(evidences),
        verifications=(
            *fact_verifications,
            *calculation_verifications,
            *claim_verifications,
        ),
        calculations=calculations,
        claims=claims,
        candidate_signals=screening,
        model_called=False,
        comparability=comparability,
    )
    markdown = render_markdown(report)
    return AnnualAnalysisResult(
        run_id=run_id,
        company_id=company_id,
        report_year=report_year,
        parsed_pdf=parsed,
        extracted_facts=extraction.facts,
        extraction_issues=extraction.issues,
        facts=tuple(facts),
        evidences=tuple(evidences),
        fact_verifications=tuple(fact_verifications),
        comparability=comparability,
        calculations=calculations,
        calculation_issues=calculation_issues,
        calculation_checks=calculation_checks,
        calculation_verifications=calculation_verifications,
        screening=screening,
        claims=claims,
        claim_checks=claim_checks,
        claim_verifications=claim_verifications,
        claim_generation_issues=claim_issues,
        report=report,
        markdown=markdown,
    )


def _canonical_claims(
    *,
    facts: list[FinancialFactV2],
    calculations: tuple[CalculationResult, ...],
    fact_verifications: list[VerificationResult],
    calculation_checks: tuple[IndependentCalculationCheck, ...],
    independent_evidence_ids_by_fact: dict[str, set[str]],
) -> tuple[tuple[Claim, ...], tuple[ClaimCheck, ...], tuple[VerificationResult, ...], tuple[dict[str, str], ...]]:
    facts_by_id = {item.fact_id: item for item in facts}
    verified_fact_ids = set(independent_evidence_ids_by_fact)
    verified_calculation_ids = {
        item.calculation_id for item in calculation_checks if item.status == "verified"
    }
    claims: list[Claim] = []
    checks: list[ClaimCheck] = []
    verifications: list[VerificationResult] = []
    issues: list[dict[str, str]] = []

    fact_check_by_id = {item.target_id: item for item in fact_verifications}
    for fact in facts:
        evidence_ids = tuple(sorted(independent_evidence_ids_by_fact.get(fact.fact_id, set())))
        if fact.fact_id not in verified_fact_ids or not evidence_ids:
            check = fact_check_by_id.get(fact.fact_id)
            issues.append(
                {
                    "target_type": "financial_fact",
                    "target_id": fact.fact_id,
                    "reason": "未生成事实 Claim：独立原文核验没有提供已确认的来源证据。"
                    if check is None
                    else "未生成事实 Claim：" + ("；".join(check.limitations) or check.status),
                }
            )
            continue
        claim = Claim(
            claim_id=f"claim:fact:{fact.fact_id}",
            claim_type=CLAIM_FACT,
            text=expected_fact_claim_text(fact),
            supporting_fact_ids=(fact.fact_id,),
            supporting_evidence_ids=evidence_ids,
            calculation_ids=(),
            verification_status=VERIFIED,
        )
        checked = verify_claim(
            claim,
            facts_by_id,
            {},
            verified_fact_ids=verified_fact_ids,
            verified_calculation_ids=verified_calculation_ids,
            verified_fact_evidence_ids=independent_evidence_ids_by_fact,
        )
        claims.append(claim)
        checks.append(checked)
        verifications.append(_claim_verification(claim, checked))

    for calculation in calculations:
        independent_check = next(
            (item for item in calculation_checks if item.calculation_id == calculation.calculation_id),
            None,
        )
        if calculation.status != CALC_SUCCEEDED or independent_check is None or independent_check.status != "verified":
            issues.append(
                {
                    "target_type": "calculation",
                    "target_id": calculation.calculation_id,
                    "reason": "未生成计算 Claim："
                    + (
                        calculation.failure_reason
                        if calculation.failure_reason is not None
                        else independent_check.reason
                        if independent_check is not None and independent_check.reason
                        else "计算未通过独立核验。"
                    ),
                }
            )
            continue
        if any(fact_id not in verified_fact_ids for fact_id in calculation.input_fact_ids):
            issues.append(
                {
                    "target_type": "calculation",
                    "target_id": calculation.calculation_id,
                    "reason": "未生成计算 Claim：输入事实没有全部独立核验通过。",
                }
            )
            continue
        evidence_ids = tuple(
            sorted(
                {
                    evidence_id
                    for fact_id in calculation.input_fact_ids
                    for evidence_id in independent_evidence_ids_by_fact.get(fact_id, set())
                }
            )
        )
        if not evidence_ids:
            issues.append(
                {
                    "target_type": "calculation",
                    "target_id": calculation.calculation_id,
                    "reason": "未生成计算 Claim：计算输入没有已确认的独立原文证据。",
                }
            )
            continue
        claim = Claim(
            claim_id=f"claim:calculation:{calculation.calculation_id}",
            claim_type=CLAIM_CALCULATION,
            text=expected_calculation_claim_text(calculation, facts_by_id),
            supporting_fact_ids=calculation.input_fact_ids,
            supporting_evidence_ids=evidence_ids,
            calculation_ids=(calculation.calculation_id,),
            verification_status=VERIFIED,
        )
        checked = verify_claim(
            claim,
            facts_by_id,
            {item.calculation_id: item for item in calculations},
            verified_fact_ids=verified_fact_ids,
            verified_calculation_ids=verified_calculation_ids,
            verified_fact_evidence_ids=independent_evidence_ids_by_fact,
        )
        claims.append(claim)
        checks.append(checked)
        verifications.append(_claim_verification(claim, checked))

    return tuple(claims), tuple(checks), tuple(verifications), tuple(issues)


def _calculation_verification(
    calculation: CalculationResult,
    check: IndependentCalculationCheck,
    evidence_ids_by_fact: dict[str, set[str]],
) -> VerificationResult:
    evidence_ids = tuple(
        sorted(
            {
                evidence_id
                for fact_id in calculation.input_fact_ids
                for evidence_id in evidence_ids_by_fact.get(fact_id, set())
            }
        )
    )
    return VerificationResult(
        verification_id=f"verify:{calculation.calculation_id}",
        target_type="calculation",
        target_id=calculation.calculation_id,
        status=check.status,
        checks=check.checks,
        conflicts=(check.reason,) if check.status == "conflict" and check.reason else (),
        evidence_ids=evidence_ids if check.status == "verified" else (),
        limitations=() if check.status == "verified" else ((check.reason or "独立计算核验未通过。"),),
        verified_at=datetime.now(timezone.utc),
    )


def _claim_verification(claim: Claim, check: ClaimCheck) -> VerificationResult:
    status = check.status if check.status in {"verified", "conflict", "insufficient_evidence"} else "insufficient_evidence"
    return VerificationResult(
        verification_id=f"verify:{claim.claim_id}",
        target_type="claim",
        target_id=claim.claim_id,
        status=status,
        checks=("按确定性 Claim 规则核对结构化对象、文案和来源引用。",),
        conflicts=(check.reason,) if status == "conflict" and check.reason else (),
        evidence_ids=claim.supporting_evidence_ids if status == "verified" else (),
        limitations=() if status == "verified" else ((check.reason or "Claim 核验未通过。"),),
        verified_at=datetime.now(timezone.utc),
    )


def _verification_issue(item: VerificationResult) -> dict[str, Any]:
    return {
        "target_type": item.target_type,
        "target_id": item.target_id,
        "status": item.status,
        "conflicts": list(item.conflicts),
        "limitations": list(item.limitations),
    }


def _extraction_issue_dict(item: ExtractionIssue) -> dict[str, Any]:
    return {
        "code": item.code,
        "message": item.message,
        "indicator_name": item.indicator_name,
        "table_name": item.table_name,
    }


def _calculation_issue_dict(item: V2CalculationIssue) -> dict[str, Any]:
    return {
        "metric_id": item.metric_id,
        "formula_id": item.formula_id,
        "code": item.code,
        "message": item.message,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value
