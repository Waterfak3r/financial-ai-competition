"""Run a paired, document-grounded verification ablation without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import statistics
import sys
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import pymupdf  # noqa: E402

from finagent.schemas.financial_fact_v2 import Claim, FinancialFactV2  # noqa: E402
from finagent.verification.claim import verify_claim  # noqa: E402
from finagent.verification.independent_fact import verify_financial_fact  # noqa: E402


PROTOCOL_PATH = ROOT / "evaluation" / "verification_ablation_cases_v1.json"
PRIMARY_ANALYSIS_PATH = ROOT / "artifacts" / "runs" / "annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42" / "analysis.json"
PRIMARY_PDF_REFERENCE_TEXT = (
    "合并利润表",
    "单位：元",
    "币种：人民币",
    "2024年度",
    "2023年度",
    "其中：营业收入",
    "26,900,977,516.70",
    "24,559,312,356.59",
)


class EvaluationInputError(RuntimeError):
    """Raised when a source, archive, protocol, or denominator cannot be trusted."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationInputError(f"无法读取 JSON 输入：{path}") from exc
    if not isinstance(value, dict):
        raise EvaluationInputError(f"JSON 根值必须是对象：{path}")
    return value


def fact_from_dict(payload: Mapping[str, Any]) -> FinancialFactV2:
    values = dict(payload)
    for field in ("period_start", "period_end"):
        if values.get(field) is not None:
            values[field] = date.fromisoformat(values[field])
    for field in ("evidence_ids", "limitations"):
        if field in values:
            values[field] = tuple(values[field])
    return FinancialFactV2(**values)


def claim_from_dict(payload: Mapping[str, Any]) -> Claim:
    values = dict(payload)
    for field in (
        "supporting_fact_ids",
        "supporting_evidence_ids",
        "calculation_ids",
        "limitations",
        "alternative_explanations",
        "follow_up_items",
    ):
        if field in values:
            values[field] = tuple(values[field])
    return Claim(**values)


def validate_primary_pdf_text(page_text: str, reference: Mapping[str, Any]) -> None:
    """Fail closed unless one original-PDF page contains the declared row and columns."""

    printed_page = re.search(r"\b(\d+)\s*/\s*(\d+)\b", page_text)
    if printed_page is None or int(printed_page.group(1)) != int(reference["printed_page"]):
        raise EvaluationInputError("原始 PDF 物理页与协议登记的印刷页码不匹配。")
    compact = re.sub(r"\s+", "", page_text)
    missing = [item for item in PRIMARY_PDF_REFERENCE_TEXT if re.sub(r"\s+", "", item) not in compact]
    if missing:
        raise EvaluationInputError(f"原始 PDF 直接参考值缺少预期行/列/金额片段：{missing}")
    row_signature = re.sub(r"\s+", "", "其中：营业收入七、61 26,900,977,516.70 24,559,312,356.59")
    if row_signature not in compact:
        raise EvaluationInputError("原始 PDF 中收入行的两个年度金额未按协议顺序同处一行。")


def validate_primary_pdf_reference(pdf_path: Path, reference: Mapping[str, Any]) -> dict[str, Any]:
    """Check the declared clean label against text read directly from the source PDF."""

    if not pdf_path.is_file():
        raise EvaluationInputError(f"主样例原始 PDF 不存在：{pdf_path}")
    actual_sha = sha256_file(pdf_path)
    if actual_sha.lower() != str(reference["source_sha256"]).lower():
        raise EvaluationInputError("主样例 PDF SHA256 与协议登记值不一致。")
    page_number = int(reference["pdf_page"])
    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:  # pragma: no cover - delegated parser errors vary by PDF
        raise EvaluationInputError("主样例 PDF 无法读取。") from exc
    try:
        if page_number > document.page_count:
            raise EvaluationInputError("协议引用页超出主样例 PDF 页数。")
        validate_primary_pdf_text(document[page_number - 1].get_text("text"), reference)
    finally:
        document.close()
    return {
        "source_pdf_sha256": actual_sha,
        "pdf_page": page_number,
        "printed_page": int(reference["printed_page"]),
        "table_title": reference["table_title"],
        "row_label": reference["row_label"],
        "column_label": reference["column_label"],
        "value_raw": reference["value_raw"],
        "value_normalized": reference["value_normalized"],
        "unit": reference["unit"],
        "currency": reference["currency"],
        "validation": "read directly from the original PDF; independent of this run's verification result",
    }


def validate_provisional_case(case_file: Mapping[str, Any], expected_company_id: str) -> None:
    if case_file.get("review_status") != "agent_provisional_pending_human":
        raise EvaluationInputError("跨公司案例不再是待人工审阅 provisional，停止纳入只读对照。")
    if case_file.get("human_review_status") != "pending":
        raise EvaluationInputError("跨公司案例人工审阅状态变化，停止纳入只读对照。")
    company = case_file.get("company")
    if not isinstance(company, dict) or company.get("company_id") != expected_company_id:
        raise EvaluationInputError("跨公司案例的公司标识与协议不匹配。")


def acceptance_from_unverified(value: FinancialFactV2 | Claim) -> tuple[str, str]:
    """Explicit no-verifier baseline: schema validation plus trust in self-reported Claim status."""

    if isinstance(value, FinancialFactV2):
        return "accepted", "只检查 FinancialFactV2 对象已通过结构校验；不回查 PDF 或核对内容。"
    if isinstance(value, Claim):
        if value.verification_status == "verified" and value.supporting_fact_ids and value.supporting_evidence_ids:
            return "accepted", "信任 Claim 自报的 verified 状态和非空引用；不核对事实内容、证据绑定或支持关系。"
        if value.verification_status == "conflict":
            return "rejected", "只采用 Claim 自报的 conflict 状态。"
        return "abstained", "只采用 Claim 自报的非 verified 状态。"
    raise TypeError("无核验支路只接受已构造的 FinancialFactV2 或 Claim。")


def independent_fact_outcome(pdf_path: Path, fact: FinancialFactV2) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
    checked = verify_financial_fact(pdf_path, fact)
    result = checked.result
    disposition = {
        "verified": "accepted",
        "conflict": "rejected",
        "insufficient_evidence": "abstained",
    }[result.status]
    return disposition, result.to_dict(), checked.evidence


def independent_claim_outcome(
    pdf_path: Path,
    fact: FinancialFactV2,
    claim: Claim,
) -> tuple[str, dict[str, Any], dict[str, Any], tuple[str, ...]]:
    _fact_outcome, fact_result, evidence = independent_fact_outcome(pdf_path, fact)
    evidence_ids = tuple(item.evidence_id for item in evidence)
    fact_status = fact_result["status"]
    verified_fact_ids = {fact.fact_id} if fact_status == "verified" else set()
    checked_claim = verify_claim(
        claim,
        {fact.fact_id: fact},
        {},
        verified_fact_ids=verified_fact_ids,
        verified_calculation_ids=set(),
        verified_fact_evidence_ids={fact.fact_id: set(evidence_ids)},
    )
    disposition = {
        "verified": "accepted",
        "conflict": "rejected",
        "insufficient_evidence": "abstained",
        "interpretation": "abstained",
    }[checked_claim.status]
    return disposition, checked_claim.to_dict(), fact_result, evidence_ids


def disposition_from_branch_result(value: Mapping[str, Any]) -> str:
    result = value["disposition"]
    if result not in {"accepted", "rejected", "abstained"}:
        raise EvaluationInputError(f"未知分支结果状态：{result}")
    return str(result)


def compute_branch_metrics(cases: list[Mapping[str, Any]], branch_name: str) -> dict[str, Any]:
    if any(case.get("label_scope") not in {"document_grounded_known", "provisional_unknown"} for case in cases):
        raise EvaluationInputError("发现未定义标签范围的案例，拒绝计算统计量。")
    known = [case for case in cases if case.get("label_scope") == "document_grounded_known"]
    if not known:
        raise EvaluationInputError("统计分母为空，拒绝生成准确率。")
    if any(case.get("label_scope") == "provisional_unknown" for case in known):
        raise EvaluationInputError("provisional/unknown 案例不能进入已知标签统计分母。")

    outcomes = [disposition_from_branch_result(case["branches"][branch_name]) for case in known]
    expected = [case["expected_disposition"] for case in known]
    injections = [
        (outcome, label)
        for case, outcome, label in zip(known, outcomes, expected)
        if case["is_injected_error"]
    ]
    controls = [
        (outcome, label)
        for case, outcome, label in zip(known, outcomes, expected)
        if not case["is_injected_error"]
    ]
    error_denominator = len(injections)
    clean_denominator = len(controls)
    if error_denominator == 0 or clean_denominator == 0:
        raise EvaluationInputError("錯誤注入或 clean 對照為空，拒絕生成消融統計。")
    error_accepted = sum(outcome == "accepted" for outcome, _ in injections)
    error_rejected = sum(outcome == "rejected" for outcome, _ in injections)
    error_abstained = sum(outcome == "abstained" for outcome, _ in injections)
    clean_accepted = sum(outcome == "accepted" for outcome, _ in controls)
    correct_exact = sum(outcome == label for outcome, label in zip(outcomes, expected))
    decided = sum(outcome != "abstained" for outcome in outcomes)
    return {
        "branch": branch_name,
        "known_case_denominator": len(known),
        "clean_control_denominator": clean_denominator,
        "injected_error_denominator": error_denominator,
        "clean_correct_acceptance": {"numerator": clean_accepted, "denominator": clean_denominator},
        "injected_error_acceptance": {"numerator": error_accepted, "denominator": error_denominator},
        "injected_error_rejection": {"numerator": error_rejected, "denominator": error_denominator},
        "injected_error_abstention": {"numerator": error_abstained, "denominator": error_denominator},
        "injected_error_blocked": {
            "numerator": error_rejected + error_abstained,
            "denominator": error_denominator,
        },
        "known_case_decision_coverage": {
            "numerator": decided,
            "denominator": len(known),
        },
        "known_case_exact_decision_rate": {
            "numerator": correct_exact,
            "denominator": len(known),
        },
        "provisional_unknown_excluded_count": sum(
            case.get("label_scope") == "provisional_unknown" for case in cases
        ),
    }


def _parse_analysis_fact(analysis: Mapping[str, Any], fact_id: str) -> FinancialFactV2:
    facts = analysis.get("facts")
    if not isinstance(facts, list):
        raise EvaluationInputError("归档分析中缺少 facts 列表。")
    matches = [item for item in facts if isinstance(item, dict) and item.get("fact_id") == fact_id]
    if len(matches) != 1:
        raise EvaluationInputError(f"归档分析中事实 ID 缺失或重复：{fact_id}")
    return fact_from_dict(matches[0])


def _load_known_cases(
    protocol: Mapping[str, Any], analysis: Mapping[str, Any], reference: Mapping[str, Any]
) -> Iterator[dict[str, Any]]:
    case_definitions = protocol.get("known_cases")
    if not isinstance(case_definitions, list) or len(case_definitions) < 6:
        raise EvaluationInputError("协议需至少包含 clean 对照与五类注入/控制案例。")
    source_facts: dict[str, FinancialFactV2] = {}
    archive_claims = analysis.get("claims")
    for definition in case_definitions:
        fact_id = str(definition["source_fact_id"])
        if fact_id not in source_facts:
            source_facts[fact_id] = _parse_analysis_fact(analysis, fact_id)
        fact = source_facts[fact_id]
        if fact.source_document_id != reference["document_id"] or fact.source_sha256.lower() != reference["source_sha256"].lower():
            raise EvaluationInputError("案例事实来源与主样例 PDF 不匹配。")
        if fact.company_id != reference["company_id"] or fact.report_year != reference["report_year"]:
            raise EvaluationInputError("案例事实公司或报告年与主样例不匹配。")
        if fact_id == reference.get("clean_fact_id") and (
            fact.raw_value != reference["value_raw"]
            or fact.normalized_value != reference["value_normalized"]
            or fact.label_raw != "营业收入"
            or fact.period_end != date(reference["report_year"], 12, 31)
            or fact.comparison_role != "current"
            or fact.scope != "consolidated"
        ):
            raise EvaluationInputError("归档 clean fact 与独立 PDF 参考行、值、期间或口径不一致。")
        mutation = definition.get("mutation")
        if mutation:
            unknown_fields = set(mutation) - set(FinancialFactV2.__dataclass_fields__)
            if unknown_fields or {"fact_id", "source_document_id", "source_sha256", "company_id"} & set(mutation):
                raise EvaluationInputError(f"注入试图更改不可变来源绑定或未知字段：{sorted(unknown_fields or mutation)}")
            case_fact = replace(fact, **mutation)
        else:
            case_fact = fact
        case_record: dict[str, Any] = {
            "case_id": definition["case_id"],
            "case_kind": definition["kind"],
            "label_scope": "document_grounded_known",
            "expected_disposition": definition["expected_disposition"],
            "is_injected_error": bool(definition.get("error_family")),
            "error_family": definition.get("error_family"),
            "notes": definition.get("notes"),
            "source": {
                "document_id": case_fact.source_document_id,
                "source_sha256": case_fact.source_sha256,
                "company_id": case_fact.company_id,
                "report_year": case_fact.report_year,
                "pdf_page": int(reference["pdf_page"]),
                "printed_page": int(reference["printed_page"]),
                "table_title": reference["table_title"],
                "row_label": reference["row_label"],
                "column_label": reference["column_label"],
                "external_reference_value": reference["value_raw"],
            },
            "mutation": mutation,
            "input_fact": case_fact.to_dict(),
        }
        claim = None
        if definition["kind"] == "claim":
            if not isinstance(archive_claims, list):
                raise EvaluationInputError("归档分析中缺少 claims 列表。")
            claim_payload = definition.get("claim")
            if not isinstance(claim_payload, dict):
                raise EvaluationInputError(f"Claim 案例没有输入对象：{definition['case_id']}")
            claim = claim_from_dict(claim_payload)
            if claim.supporting_fact_ids != (fact.fact_id,):
                raise EvaluationInputError("Claim 案例必须绑定其声明的海天来源事实。")
            if not claim.supporting_evidence_ids:
                raise EvaluationInputError("Claim 注入不能利用空 evidence 引用规避支持关系检查。")
            if definition["case_id"] == "haitian_revenue_claim_supported_control":
                archive_matches = [
                    item
                    for item in archive_claims
                    if isinstance(item, dict) and item.get("claim_id") == claim.claim_id
                ]
                if len(archive_matches) != 1 or archive_matches[0] != claim.to_dict():
                    raise EvaluationInputError("clean Claim 对照与归档的受控事实 Claim 不一致。")
            case_record["input_claim"] = claim.to_dict()
        case_payload = {
            "kind": definition["kind"],
            "fact": case_fact.to_dict(),
            "claim": None if claim is None else claim.to_dict(),
        }
        case_record["input_sha256"] = canonical_sha256(case_payload)

        start = time.perf_counter_ns()
        if claim is None:
            disposition, explanation = acceptance_from_unverified(case_fact)
            unverified_record = {
                "disposition": disposition,
                "verifier_status": None,
                "explanation": explanation,
                "elapsed_ms": (time.perf_counter_ns() - start) / 1_000_000,
            }
            start = time.perf_counter_ns()
            checked_disposition, result, checked_evidence = independent_fact_outcome(
                ROOT / reference["source_path"], case_fact
            )
            verified_record = {
                "disposition": checked_disposition,
                "verifier_status": result["status"],
                "verifier_result": result,
                "evidence": [item.to_dict() for item in checked_evidence],
                "evidence_ids_used_by_claim_check": [item.evidence_id for item in checked_evidence],
                "explanation": "; ".join(result.get("conflicts", []) or result.get("limitations", []) or result.get("checks", [])),
                "elapsed_ms": (time.perf_counter_ns() - start) / 1_000_000,
            }
        else:
            disposition, explanation = acceptance_from_unverified(claim)
            unverified_record = {
                "disposition": disposition,
                "verifier_status": claim.verification_status,
                "self_reported_status": claim.verification_status,
                "explanation": explanation,
                "elapsed_ms": (time.perf_counter_ns() - start) / 1_000_000,
            }
            start = time.perf_counter_ns()
            checked_disposition, claim_result, fact_result, evidence_ids = independent_claim_outcome(
                ROOT / reference["source_path"], case_fact, claim
            )
            verified_record = {
                "disposition": checked_disposition,
                "verifier_status": claim_result["status"],
                "claim_verifier_result": claim_result,
                "fact_verifier_result": fact_result,
                "evidence_ids_used_by_claim_check": list(evidence_ids),
                "explanation": claim_result.get("reason") or f"Claim 核验状态为 {claim_result['status']}。",
                "elapsed_ms": (time.perf_counter_ns() - start) / 1_000_000,
            }
        case_record["branches"] = {
            "without_independent_verification": unverified_record,
            "with_independent_verification": verified_record,
        }
        if case_record["input_sha256"] != canonical_sha256(case_payload):
            raise EvaluationInputError("执行分支改变了配对输入，消融对比无效。")
        yield_record = case_record
        yield_record["expected_outcome_met"] = (
            verified_record["disposition"] == case_record["expected_disposition"]
        )
        yield yield_record


def _load_provisional_comparisons(protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    definitions = protocol.get("provisional_cross_company_comparisons")
    if not isinstance(definitions, list) or len(definitions) < 2:
        raise EvaluationInputError("协议必须含至少两个既有 provisional 跨公司状态对照。")
    comparisons = []
    for definition in definitions:
        case_path = ROOT / definition["case_path"]
        source_path = ROOT / definition["source_path"]
        case_file = read_json(case_path)
        company_id = str(definition["company_id"])
        validate_provisional_case(case_file, company_id)
        if case_file.get("case_id") != definition["source_case_id"]:
            raise EvaluationInputError(f"provisional 案例文件 ID 不匹配：{definition['case_id']}")
        if case_file.get("report", {}).get("report_year") != 2024:
            raise EvaluationInputError(f"provisional 案例报告年度不匹配：{definition['case_id']}")
        case_source = case_file.get("source", {})
        if case_source.get("local_path") != definition["source_path"]:
            raise EvaluationInputError(f"provisional 案例 PDF 路径与协议不匹配：{definition['case_id']}")
        if str(case_source.get("sha256", "")).lower() != str(definition["source_sha256"]).lower():
            raise EvaluationInputError(f"provisional 案例 PDF 哈希与协议不匹配：{definition['case_id']}")
        if case_file.get("cli_run", {}).get("run_id") != definition["analysis_run_id"]:
            raise EvaluationInputError(f"provisional 案例运行 ID 与协议不匹配：{definition['case_id']}")
        if case_file.get("review_status") != "agent_provisional_pending_human":
            raise EvaluationInputError("provisional 案例 review_status 已变化。")
        if company_id == "603288":
            raise EvaluationInputError("主样例不能重复作为跨公司 provisional 状态对照。")
        analysis_path = ROOT / "artifacts" / "runs" / definition["analysis_run_id"] / "analysis.json"
        analysis = read_json(analysis_path)
        if analysis.get("run_id") != definition["analysis_run_id"] or analysis.get("company_id") != company_id:
            raise EvaluationInputError("provisional 归档 run 的公司或 ID 不匹配。")
        if sha256_file(source_path).lower() != str(definition["source_sha256"]).lower():
            raise EvaluationInputError(f"provisional 原始 PDF SHA256 不匹配：{definition['case_id']}")
        expected_fact_id = None
        candidates = [
            item
            for item in analysis.get("facts", [])
            if item.get("metric_id") == definition["metric_id"]
            and item.get("period_end") == definition["period_end"]
            and item.get("comparison_role") == "current"
        ]
        if len(candidates) != 1:
            raise EvaluationInputError(f"provisional 归档代表事实缺失或重复：{definition['case_id']}")
        fact = fact_from_dict(candidates[0])
        expected_fact_id = fact.fact_id
        if fact.source_sha256.lower() != str(definition["source_sha256"]).lower():
            raise EvaluationInputError("provisional 事实哈希与原始 PDF 不一致。")
        actual_sha = sha256_file(source_path)
        start = time.perf_counter_ns()
        disposition, result, evidence = independent_fact_outcome(source_path, fact)
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
        evidence_pages = sorted({item.pdf_page for item in evidence})
        comparisons.append(
            {
                "case_id": definition["case_id"],
                "label_scope": "provisional_unknown",
                "review_status": case_file["review_status"],
                "human_review_status": definition["human_review_status"],
                "metric_label_status": definition["metric_label_status"],
                "company_id": company_id,
                "source_document_id": fact.source_document_id,
                "source_sha256": actual_sha,
                "source_pdf_pages_from_independent_reread": evidence_pages,
                "source_fact_id": expected_fact_id,
                "input_sha256": canonical_sha256(fact.to_dict()),
                "input_fact": fact.to_dict(),
                "current_verifier_disposition": disposition,
                "current_verifier_result": result,
                "elapsed_ms": elapsed_ms,
                "scored_in_known_denominator": False,
                "exclusion_reason": "案例和事实参考仍为 agent provisional、待人工审阅；没有独立确认标签。",
            }
        )
    return comparisons


def _ratio(pair: Mapping[str, int]) -> float | None:
    return None if pair["denominator"] == 0 else pair["numerator"] / pair["denominator"]


def _attach_rates(metrics: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "clean_correct_acceptance",
        "injected_error_acceptance",
        "injected_error_rejection",
        "injected_error_abstention",
        "injected_error_blocked",
        "known_case_decision_coverage",
        "known_case_exact_decision_rate",
    ):
        metrics[key]["rate"] = _ratio(metrics[key])
    return metrics


def run_evaluation(output_id: str | None = None) -> tuple[Path, dict[str, Any]]:
    protocol = read_json(PROTOCOL_PATH)
    reference = protocol.get("primary_reference")
    if not isinstance(reference, dict):
        raise EvaluationInputError("协议缺少 primary_reference。")
    primary_pdf = ROOT / reference["source_path"]
    pdf_reference = validate_primary_pdf_reference(primary_pdf, reference)
    analysis = read_json(PRIMARY_ANALYSIS_PATH)
    if analysis.get("run_id") != reference["analysis_run_id"]:
        raise EvaluationInputError("海天归档分析 run_id 与评估协议不匹配。")
    if analysis.get("company_id") != reference["company_id"] or analysis.get("report_year") != reference["report_year"]:
        raise EvaluationInputError("海天归档分析公司/年度与评估协议不匹配。")
    if analysis.get("model_called") is not False:
        raise EvaluationInputError("当前协议只允许复用不含在线模型调用的海天归档分析。")
    cases = list(_load_known_cases(protocol, analysis, reference))
    error_families = {case.get("error_family") for case in cases if case["is_injected_error"]}
    required_families = {
        "wrong_amount",
        "wrong_unit",
        "wrong_year_column_or_period",
        "wrong_statement_scope",
        "unsupported_claim",
    }
    if not required_families <= error_families:
        raise EvaluationInputError(f"错误注入类别不完整：{sorted(required_families - error_families)}")
    provisional = _load_provisional_comparisons(protocol)
    if any(item.get("scored_in_known_denominator") for item in provisional):
        raise EvaluationInputError("provisional 结果进入已知标签分母，拒绝输出。")

    metric_cases = [*cases, *provisional]
    by_branch = {
        branch: _attach_rates(compute_branch_metrics(metric_cases, branch))
        for branch in ("without_independent_verification", "with_independent_verification")
    }
    errors = [item for item in cases if item["is_injected_error"]]
    controls = [item for item in cases if not item["is_injected_error"]]
    deviations = [
        {
            "case_id": item["case_id"],
            "expected_disposition": item["expected_disposition"],
            "observed_disposition": item["branches"]["with_independent_verification"]["disposition"],
            "explanation": item["branches"]["with_independent_verification"]["explanation"],
        }
        for item in cases
        if item["branches"]["with_independent_verification"]["disposition"] != item["expected_disposition"]
    ]
    now = datetime.now(timezone.utc)
    evaluation_id = output_id or f"verification-ablation-m4-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"verification-ablation-m4-[a-zA-Z0-9T_-]+", evaluation_id):
        raise EvaluationInputError("evaluation_id 含有不允许的路径字符。")
    output_dir = ROOT / "artifacts" / "evaluations" / evaluation_id
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise EvaluationInputError(f"评估目录已存在，拒绝覆盖：{output_dir}") from exc

    manifest = {
        "evaluation_id": evaluation_id,
        "protocol_id": protocol["protocol_id"],
        "protocol_version": protocol["protocol_version"],
        "created_at": now.isoformat(),
        "project_root": ".",
        "primary_analysis_run_id": reference["analysis_run_id"],
        "primary_analysis_sha256": sha256_file(PRIMARY_ANALYSIS_PATH),
        "primary_pdf_sha256": pdf_reference["source_pdf_sha256"],
        "protocol_sha256": sha256_file(PROTOCOL_PATH),
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "python_version": platform.python_version(),
        "pymupdf_version": getattr(pymupdf, "VersionBind", "unknown"),
        "model_called": False,
        "network_calls": 0,
        "primary_pdf_reference": pdf_reference,
        "known_case_ids": [item["case_id"] for item in cases],
        "provisional_case_ids": [item["case_id"] for item in provisional],
        "provisional_accuracy_denominator_excluded": True,
    }
    summary = {
        "evaluation_id": evaluation_id,
        "protocol_id": protocol["protocol_id"],
        "scope": "M4 有无独立核验消融基线；单一公司一个报告年度，外部 PDF 文档直接参考与合成错误注入，不是人工 golden。",
        "known_label_denominator": {
            "case_count": len(cases),
            "clean_controls": len(controls),
            "injected_errors": len(errors),
            "provisional_unknown_included": 0,
        },
        "metrics": by_branch,
        "provisional_cross_company_status_only": {
            "case_count": len(provisional),
            "included_in_accuracy_or_coverage": False,
            "items": [
                {
                    "case_id": item["case_id"],
                    "company_id": item["company_id"],
                    "verifier_status": item["current_verifier_result"]["status"],
                    "disposition": item["current_verifier_disposition"],
                    "elapsed_ms": item["elapsed_ms"],
                }
                for item in provisional
            ],
        },
        "paired_timing": {
            branch: {
                "case_count": len(cases),
                "total_ms": sum(item["branches"][branch]["elapsed_ms"] for item in cases),
                "median_ms": statistics.median(item["branches"][branch]["elapsed_ms"] for item in cases),
                "note": "Local one-pass wall time; includes the current branch operations only, not PDF/API/model acquisition. Not a latency benchmark.",
            }
            for branch in ("without_independent_verification", "with_independent_verification")
        },
        "expected_outcome_check": {
            "all_clean_controls_accepted_with_verifier": all(
                item["branches"]["with_independent_verification"]["disposition"] == "accepted"
                for item in controls
            ),
            "all_injected_errors_not_accepted_with_verifier": all(
                item["branches"]["with_independent_verification"]["disposition"] != "accepted"
                for item in errors
            ),
            "deviations": deviations,
        },
        "limitations": [
            "Primary clean labels were transcribed by the agent directly from one public PDF table and have not received human review.",
            "Known labels cover selected structured facts and one controlled Claim mismatch, not fraud outcomes or general semantic entailment.",
            "The no-verification arm is an explicit schema/self-report acceptance baseline; it does not estimate every possible unverified workflow.",
            "Two existing real-company cross-company samples remain provisional and are descriptive verifier-state comparisons only.",
            "Single-pass elapsed times are descriptive and sensitive to local machine/cache state.",
            "This M4 ablation baseline does not complete the planned 3–5-company human-golden evaluation.",
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "case_results.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "provisional_comparisons.json").write_text(
        json.dumps(provisional, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output_dir, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evaluation-id",
        help="可选的唯一产物目录名；已存在目录会失败关闭且绝不覆盖。",
    )
    args = parser.parse_args()
    try:
        output_dir, summary = run_evaluation(args.evaluation_id)
    except (EvaluationInputError, OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"评估停止：{exc}\n")
    print(json.dumps({"evaluation_dir": str(output_dir.relative_to(ROOT)), "summary": summary}, ensure_ascii=False, indent=2))
    if summary["expected_outcome_check"]["deviations"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
