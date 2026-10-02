"""Run the versioned C-01 evaluation on the registered original annual reports."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend" / "src"))

from finagent.api.annual_analysis import analyze_annual_pdf  # noqa: E402
from finagent.schemas.financial_fact_v2 import Claim, FinancialFactV2  # noqa: E402
from finagent.verification.claim import verify_claim  # noqa: E402
from finagent.verification.independent_fact import verify_financial_fact  # noqa: E402


PROTOCOL_PATH = ROOT / "evaluation" / "multicompany_evaluation_v2.json"
DISPOSITIONS = {"accepted", "rejected", "abstained"}
VERIFIER_TO_DISPOSITION = {
    "verified": "accepted",
    "conflict": "rejected",
    "insufficient_evidence": "abstained",
}
FAMILIES = (
    "wrong_amount",
    "wrong_unit",
    "wrong_period",
    "wrong_statement_scope",
    "unsupported_claim",
)


class EvaluationInputError(ValueError):
    """Raised when a source or evaluation denominator cannot be trusted."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationInputError(f"无法读取 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise EvaluationInputError(f"JSON 根值必须是对象：{path}")
    return value


def validate_source_registry(sources: list[Mapping[str, Any]]) -> None:
    """Reject missing/duplicate company, path, or source identities before any run."""

    if len(sources) != 3:
        raise EvaluationInputError("C-01 必须且只能登记三家公司来源。")
    company_ids = [str(item.get("company_id", "")) for item in sources]
    source_paths = [str(item.get("source_path", "")) for item in sources]
    document_ids = [str(item.get("document_id", "")) for item in sources]
    if any(not item for item in (*company_ids, *source_paths, *document_ids)):
        raise EvaluationInputError("来源登记缺少 company_id、source_path 或 document_id。")
    if len(set(company_ids)) != len(company_ids):
        raise EvaluationInputError("来源登记存在重复或冲突的 company_id。")
    if len(set(source_paths)) != len(source_paths) or len(set(document_ids)) != len(document_ids):
        raise EvaluationInputError("来源登记存在重复或冲突的 source_path/document_id。")
    if any(not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("sha256", ""))) for item in sources):
        raise EvaluationInputError("来源登记缺少有效的 SHA256。")
    hashes = [str(item["sha256"]).lower() for item in sources]
    if len(set(hashes)) != len(hashes):
        raise EvaluationInputError("不同公司来源登记了相同 SHA256，拒绝混用。")


def validate_source_registration(
    source: Mapping[str, Any],
    source_record: Mapping[str, Any],
    actual_sha256: str,
    *,
    actual_size: int | None = None,
    actual_pages: int | None = None,
) -> None:
    """Require the protocol, source.json, and bytes on disk to identify one report."""

    required = ("company_id", "document_id", "source_url", "source_path", "sha256", "report_year")
    missing = [key for key in required if source.get(key) in (None, "")]
    if missing:
        raise EvaluationInputError(f"来源登记缺少字段：{', '.join(missing)}")
    record_keys = ("company_id", "document_id", "source_url", "sha256")
    record_missing = [key for key in record_keys if source_record.get(key) in (None, "")]
    if record_missing:
        raise EvaluationInputError(f"source.json 缺少字段：{', '.join(record_missing)}")
    for key in record_keys:
        if key == "sha256":
            matches = str(source_record[key]).lower() == str(source[key]).lower()
        else:
            matches = source_record[key] == source[key]
        if not matches:
            raise EvaluationInputError(f"协议来源与 source.json 的 {key} 不一致。")
    expected_sha = str(source["sha256"]).lower()
    if actual_sha256.lower() != expected_sha:
        raise EvaluationInputError("原始 PDF SHA256 与协议登记不一致。")
    if str(source_record.get("local_path", "")) != str(source["source_path"]):
        raise EvaluationInputError("协议 source_path 与 source.json local_path 不一致。")
    if actual_size is not None and int(source.get("byte_size", -1)) != actual_size:
        raise EvaluationInputError("原始 PDF 字节数与来源登记不一致。")
    if actual_pages is not None and int(source.get("pdf_pages", -1)) != actual_pages:
        raise EvaluationInputError("原始 PDF 页数与来源登记不一致。")


def reserve_evaluation_directory(artifacts_root: Path, evaluation_id: str) -> Path:
    """Reserve a new output directory; never reuse or overwrite a historical run."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", evaluation_id):
        raise EvaluationInputError("evaluation_id 只能包含字母、数字、点、下划线和连字符。")
    root = Path(artifacts_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = (root / evaluation_id).resolve()
    if output.parent != root:
        raise EvaluationInputError("评测产物路径超出 artifacts/evaluations。")
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise EvaluationInputError(f"评测产物已存在，拒绝覆盖：{evaluation_id}") from exc
    return output


def _new_evaluation_id() -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"multicompany-v2-{now}-{uuid.uuid4().hex[:8]}"


def _fact_to_dict(fact: FinancialFactV2) -> dict[str, Any]:
    return fact.to_dict()


def _disposition_for_status(status: str) -> str:
    try:
        return VERIFIER_TO_DISPOSITION[status]
    except KeyError as exc:
        raise EvaluationInputError(f"未知核验状态：{status}") from exc


def _schema_only_baseline(value: FinancialFactV2 | Claim) -> dict[str, str]:
    """An explicit no-source-check reference arm, not a historical product claim."""

    if isinstance(value, Claim):
        accepted = (
            value.verification_status == "verified"
            and bool(value.supporting_fact_ids)
            and bool(value.supporting_evidence_ids)
        )
        return {
            "disposition": "accepted" if accepted else "abstained",
            "basis": "只信任 schema 构造成功及 Claim 自报 verified/非空引用；不打开 PDF 或验证内容。",
        }
    return {
        "disposition": "accepted",
        "basis": "只检查 FinancialFactV2 结构构造成功；不打开 PDF 或验证内容。",
    }


def _formatted_raw_amount(value: Decimal, raw_sample: str) -> str:
    decimals = len(raw_sample.rsplit(".", 1)[1]) if "." in raw_sample else 0
    return f"{value:,.{decimals}f}"


def _make_injected_fact(fact: FinancialFactV2, family: str) -> FinancialFactV2:
    if family == "wrong_amount":
        altered = Decimal(fact.normalized_value) + Decimal("0.01")
        return replace(
            fact,
            raw_value=_formatted_raw_amount(altered, fact.raw_value),
            normalized_value=f"{altered:.2f}",
        )
    if family == "wrong_unit":
        return replace(fact, unit="万元")
    if family == "wrong_period":
        return replace(
            fact,
            period_start=date(fact.period_end.year - 1, 1, 1),
            period_end=date(fact.period_end.year - 1, 12, 31),
        )
    if family == "wrong_statement_scope":
        return replace(fact, scope="parent")
    raise EvaluationInputError(f"未知事实注入类别：{family}")


def _fact_outcome(pdf_path: Path, fact: FinancialFactV2) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    checked = verify_financial_fact(pdf_path, fact)
    evidence = [item.to_dict() for item in checked.evidence]
    return _disposition_for_status(checked.result.status), checked.result.to_dict(), evidence


def _claim_outcome(
    pdf_path: Path,
    fact: FinancialFactV2,
    claim: Claim,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    fact_disposition, fact_result, evidence = _fact_outcome(pdf_path, fact)
    evidence_ids = {str(item["evidence_id"]) for item in evidence}
    if not evidence_ids:
        evidence_ids = set(fact.evidence_ids)
    checked_claim = verify_claim(
        claim,
        {fact.fact_id: fact},
        {},
        verified_fact_ids={fact.fact_id} if fact_disposition == "accepted" else set(),
        verified_calculation_ids=set(),
        verified_fact_evidence_ids={fact.fact_id: evidence_ids},
    )
    status = checked_claim.status
    disposition = {
        "verified": "accepted",
        "conflict": "rejected",
        "insufficient_evidence": "abstained",
        "interpretation": "abstained",
    }[status]
    return disposition, checked_claim.to_dict(), fact_result, evidence


def _mutation_payload(fact: FinancialFactV2, family: str, evidence_ids: tuple[str, ...]) -> tuple[FinancialFactV2, Claim | None, str]:
    if family != "unsupported_claim":
        mutated = _make_injected_fact(fact, family)
        notes = {
            "wrong_amount": "受控地把报告值和规范值末位增加 0.01。",
            "wrong_unit": "将原事实单位字段改为万元，保持来源绑定与倍率。",
            "wrong_period": "将期间改为 2023 年末，保留 2024 本期金额及来源。",
            "wrong_statement_scope": "将营业收入口径改为母公司。",
        }[family]
        return mutated, None, notes

    text = "2024 年营业收入证明公司存在财务造假。"
    claim = Claim(
        claim_id=f"claim:synthetic-unsupported:{fact.company_id}",
        claim_type="fact",
        text=text,
        supporting_fact_ids=(fact.fact_id,),
        supporting_evidence_ids=evidence_ids or fact.evidence_ids,
        calculation_ids=(),
        verification_status="verified",
        limitations=(),
        alternative_explanations=(),
        follow_up_items=(),
    )
    return fact, claim, "人工构造的无支持 Claim，用于核验阻断；不是任何公司舞弊事实或标签。"


def compute_fact_coverage(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe verifier coverage only; provisional values have no golden accuracy denominator."""

    counts = Counter(str(item["verifier_status"]) for item in records)
    allowed = {"verified", "conflict", "insufficient_evidence"}
    if set(counts) - allowed:
        raise EvaluationInputError("事实覆盖统计包含未知核验状态。")
    if any(item.get("factual_review_status") != "agent_provisional_pending_human" for item in records):
        raise EvaluationInputError("待复核真实事实状态变化，拒绝将其并入 provisional 覆盖统计。")
    total = len(records)
    return {
        "sample_denominator": total,
        "verified_by_independent_checker": counts["verified"],
        "conflict": counts["conflict"],
        "insufficient_evidence": counts["insufficient_evidence"],
        "unknown_or_unconfirmed": counts["insufficient_evidence"],
        "human_golden_accuracy_denominator": 0,
        "fraud_false_positive_denominator": 0,
        "fraud_false_negative_denominator": 0,
        "excluded_from_human_golden_metrics": total,
        "interpretation": "这是从原文重读得到的核验覆盖描述，不是人工正确率或舞弊分类分数。",
    }


def compute_injection_metrics(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Count acceptance, conflict rejection, and abstention over every controlled injection."""

    if not records:
        raise EvaluationInputError("注入案例分母为空。")
    if any(item.get("family") not in FAMILIES for item in records):
        raise EvaluationInputError("发现未登记的注入类别。")
    if any(item.get("provisional_label_status") != "agent_provisional_pending_human" for item in records):
        raise EvaluationInputError("受控注入必须绑定待人工复核的原始样本状态。")
    dispositions = [str(item.get("verification_disposition", "")) for item in records]
    if set(dispositions) - DISPOSITIONS:
        raise EvaluationInputError("注入分支包含未知 disposition。")
    grouped_company: dict[str, Counter[str]] = defaultdict(Counter)
    grouped_family: dict[str, Counter[str]] = defaultdict(Counter)
    for item, disposition in zip(records, dispositions):
        grouped_company[str(item["company_id"])][disposition] += 1
        grouped_family[str(item["family"])][disposition] += 1

    denominator = len(records)
    counts = Counter(dispositions)
    baseline_accepted = sum(item.get("baseline_disposition") == "accepted" for item in records)
    return {
        "injected_error_denominator": denominator,
        "schema_only_baseline_accepted": {"numerator": baseline_accepted, "denominator": denominator},
        "independent_verifier_error_accepted": {"numerator": counts["accepted"], "denominator": denominator},
        "independent_verifier_rejected_as_conflict": {"numerator": counts["rejected"], "denominator": denominator},
        "independent_verifier_abstained_as_insufficient_evidence": {
            "numerator": counts["abstained"],
            "denominator": denominator,
        },
        "decision_coverage": {
            "numerator": counts["accepted"] + counts["rejected"],
            "denominator": denominator,
        },
        "abstention_is_counted_as_rejection": False,
        "by_company": {
            company: {"denominator": sum(group.values()), **dict(group)}
            for company, group in sorted(grouped_company.items())
        },
        "by_error_family": {
            family: {"denominator": sum(group.values()), **dict(group)}
            for family, group in sorted(grouped_family.items())
        },
        "expected_pattern_note": "注入是人工构造控制输入；拒绝、错误接受与弃权分别计数。弃权不是成功拒绝。",
    }


def _direction_summaries(company_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    facts = company_result["facts"]
    by_metric: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for record in facts:
        fact = record["fact"]
        if fact.get("period_end") is None:
            continue
        role = str(fact.get("comparison_role"))
        if role in {"current", "comparative"}:
            by_metric[str(fact["metric_id"])][role] = fact
    output = []
    for metric_id, periods in sorted(by_metric.items()):
        current = periods.get("current")
        comparative = periods.get("comparative")
        if current is None or comparative is None:
            continue
        current_value = Decimal(str(current["normalized_value"]))
        comparative_value = Decimal(str(comparative["normalized_value"]))
        relation = "higher" if current_value > comparative_value else "lower" if current_value < comparative_value else "equal"
        output.append(
            {
                "metric_id": metric_id,
                "current_reported_value": str(current["normalized_value"]),
                "comparative_reported_value": str(comparative["normalized_value"]),
                "reported_numeric_relation": relation,
                "comparability_status": company_result["comparability"].get("status"),
                "trend_claim": "not_assessed",
                "note": "仅比较同份年报原表列示数值的方向；不代表独立证明可比、经营原因、风险或舞弊。",
            }
        )
    return output


def _validate_disclosure_pages(source: Mapping[str, Any], pdf_path: Path) -> list[dict[str, Any]]:
    try:
        import pymupdf

        document = pymupdf.open(pdf_path)
    except Exception as exc:
        raise EvaluationInputError(f"无法读取来源 PDF 披露页：{source['company_id']}") from exc
    records = []
    try:
        for item in source.get("provisional_disclosures", []):
            page_number = int(item["pdf_page"])
            if page_number < 1 or page_number > document.page_count:
                raise EvaluationInputError(f"披露页超出 PDF 页数：{source['company_id']} p.{page_number}")
            page_text = document[page_number - 1].get_text("text")
            compact_text = re.sub(r"\s+", "", page_text)
            compact_expected = re.sub(r"\s+", "", str(item["required_text"]))
            present = compact_expected in compact_text
            if not present:
                raise EvaluationInputError(
                    f"协议披露定位与 PDF 不符：{source['company_id']} p.{page_number} {item['topic']}"
                )
            records.append(
                {
                    **dict(item),
                    "source_text_present_in_registered_pdf": True,
                    "review_status": "agent_provisional_pending_human",
                    "human_interpretation": None,
                }
            )
    finally:
        document.close()
    return records


def _source_description(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: source.get(key)
        for key in (
            "company_id",
            "company_name",
            "company_role",
            "adaptation_status",
            "previous_exposure",
            "report_year",
            "report_type",
            "document_id",
            "source_path",
            "source_url",
            "disclosure_date",
            "sha256",
            "byte_size",
            "pdf_pages",
            "use_condition",
        )
    }


def _prepare_source(source: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    source_path = (ROOT / str(source["source_path"])).resolve()
    raw_root = (ROOT / "data" / "raw").resolve()
    if not source_path.is_relative_to(raw_root) or not source_path.is_file():
        raise EvaluationInputError(f"原始 PDF 不存在或不在 data/raw 内：{source.get('company_id')}")
    record_path = (ROOT / str(source["source_record_path"])).resolve()
    if not record_path.is_relative_to(raw_root) or not record_path.is_file():
        raise EvaluationInputError(f"来源 source.json 不存在或不在 data/raw 内：{source.get('company_id')}")
    source_record = read_json(record_path)
    actual_sha = sha256_file(source_path)
    actual_size = source_path.stat().st_size
    try:
        import pymupdf

        with pymupdf.open(source_path) as document:
            actual_pages = document.page_count
    except Exception as exc:
        raise EvaluationInputError(f"原始 PDF 无法解析：{source.get('company_id')}") from exc
    validate_source_registration(
        source,
        source_record,
        actual_sha,
        actual_size=actual_size,
        actual_pages=actual_pages,
    )
    return source_path, source_record


def _run_registered_analysis(
    source: Mapping[str, Any],
    pdf_path: Path,
    analyzer: Callable[..., Any] = analyze_annual_pdf,
    *,
    run_id: str,
) -> Any:
    """Keep the analyzer call narrow so labels and mutations cannot enter its arguments."""

    return analyzer(
        pdf_path,
        run_id=run_id,
        company_id=str(source["company_id"]),
        report_year=int(source["report_year"]),
        document_id=str(source["document_id"]),
    )


def _independent_fact_records(pdf_path: Path, result: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for fact in result.facts:
        started = time.perf_counter_ns()
        checked = verify_financial_fact(pdf_path, fact)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        records.append(
            {
                "fact": _fact_to_dict(fact),
                "factual_review_status": "agent_provisional_pending_human",
                "excluded_from_human_golden_metrics": True,
                "baseline_disposition": "accepted",
                "baseline_basis": "schema-only extractor output; no source recheck",
                "analysis_pipeline_verifier_status": next(
                    (item.status for item in result.fact_verifications if item.target_id == fact.fact_id),
                    "missing",
                ),
                "verifier_status": checked.result.status,
                "verifier_result": checked.result.to_dict(),
                "independent_evidence": [item.to_dict() for item in checked.evidence],
                "elapsed_ms": elapsed_ms,
            }
        )
    return records


def _inject_controlled_errors(
    source: Mapping[str, Any],
    pdf_path: Path,
    result: Any,
) -> list[dict[str, Any]]:
    candidates = [
        fact
        for fact in result.facts
        if fact.metric_id == "revenue"
        and fact.comparison_role == "current"
        and fact.period_end == date(int(source["report_year"]), 12, 31)
    ]
    if len(candidates) != 1:
        raise EvaluationInputError(f"找不到唯一的当前年度收入事实：{source['company_id']}")
    base_fact = candidates[0]
    seed_check = verify_financial_fact(pdf_path, base_fact)
    seed_evidence = tuple(item.evidence_id for item in seed_check.evidence) or tuple(base_fact.evidence_ids)
    output = []
    for family in FAMILIES:
        baseline_unverified: dict[str, Any]
        mutated_fact, claim, mutation_note = _mutation_payload(base_fact, family, seed_evidence)
        input_payload = {
            "fact": mutated_fact.to_dict(),
            "claim": None if claim is None else claim.to_dict(),
        }
        input_hash = canonical_sha256(input_payload)
        if claim is None:
            baseline_unverified = _schema_only_baseline(mutated_fact)
            started = time.perf_counter_ns()
            disposition, verifier_result, evidence = _fact_outcome(pdf_path, mutated_fact)
            verification_record = {
                "disposition": disposition,
                "verifier_status": verifier_result["status"],
                "verifier_result": verifier_result,
                "independent_evidence": evidence,
                "elapsed_ms": (time.perf_counter_ns() - started) / 1_000_000,
            }
        else:
            baseline_unverified = _schema_only_baseline(claim)
            started = time.perf_counter_ns()
            disposition, claim_result, fact_result, evidence = _claim_outcome(pdf_path, mutated_fact, claim)
            verification_record = {
                "disposition": disposition,
                "verifier_status": claim_result["status"],
                "claim_verifier_result": claim_result,
                "fact_verifier_result": fact_result,
                "independent_evidence": evidence,
                "elapsed_ms": (time.perf_counter_ns() - started) / 1_000_000,
            }
        actual_payload = {
            "fact": mutated_fact.to_dict(),
            "claim": None if claim is None else claim.to_dict(),
        }
        if canonical_sha256(actual_payload) != input_hash:
            raise EvaluationInputError("评测分支改变了配对输入，消融对比无效。")
        output.append(
            {
                "case_id": f"{source['company_id']}-2024-{family}",
                "company_id": str(source["company_id"]),
                "family": family,
                "kind": "claim" if claim is not None else "fact",
                "provisional_label_status": "agent_provisional_pending_human",
                "excluded_from_human_golden_metrics": True,
                "expected_case_nature": "synthetic_controlled_error_not_fraud_label",
                "baseline_disposition": baseline_unverified["disposition"],
                "baseline_basis": baseline_unverified["basis"],
                "verification_disposition": disposition,
                "mutation_note": mutation_note,
                "source_fact_id": base_fact.fact_id,
                "source_sha256": base_fact.source_sha256,
                "input_sha256": input_hash,
                "input_fact": mutated_fact.to_dict(),
                "input_claim": None if claim is None else claim.to_dict(),
                "verification": verification_record,
            }
        )
    return output


def _group_company_results(records: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for company_id in sorted({str(item["company_id"]) for item in records}):
        company = [item for item in records if str(item["company_id"]) == company_id]
        counts = Counter(str(item["verifier_status"]) for item in company)
        output[company_id] = {
            "sample_denominator": len(company),
            "verified": counts["verified"],
            "conflict": counts["conflict"],
            "insufficient_evidence": counts["insufficient_evidence"],
            "unknown_or_unconfirmed": counts["insufficient_evidence"],
            "schema_only_baseline_accepted": len(company),
            "direct_recheck_changed_from_schema_only_baseline": len(company) - counts["verified"],
            "elapsed_ms": round(sum(float(item["elapsed_ms"]) for item in company), 3),
            "facts": company,
        }
    return output


def _git_provenance() -> dict[str, Any]:
    def _git(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(ROOT), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip()

    status = _git("status", "--porcelain")
    source_files = (
        PROTOCOL_PATH,
        ROOT / "scripts" / "evaluate_multicompany.py",
        ROOT / "scripts" / "build_human_review_packet.py",
    )
    package_versions: dict[str, str | None] = {}
    for package in ("PyMuPDF", "fastapi", "pydantic"):
        try:
            package_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_versions[package] = None
    return {
        "head": _git("rev-parse", "HEAD"),
        "dirty": bool(status),
        "model_called": False,
        "source_files": {
            path.relative_to(ROOT).as_posix(): sha256_file(path)
            for path in source_files
        },
        "runtime": {
            "python": platform.python_version(),
            "packages": package_versions,
        },
    }


def run_evaluation(evaluation_id: str | None = None) -> tuple[Path, dict[str, Any]]:
    protocol = read_json(PROTOCOL_PATH)
    if protocol.get("schema_version") != 2 or protocol.get("protocol_id") != "fintrace-multicompany-evaluation-v2":
        raise EvaluationInputError("评测协议版本或 ID 不匹配。")
    sources = protocol.get("sources")
    if not isinstance(sources, list):
        raise EvaluationInputError("评测协议缺少 sources 列表。")
    validate_source_registry(sources)

    prepared: list[tuple[Mapping[str, Any], Path, dict[str, Any]]] = []
    for source in sources:
        pdf_path, source_record = _prepare_source(source)
        prepared.append((source, pdf_path, source_record))

    selected_id = evaluation_id or _new_evaluation_id()
    output_dir = reserve_evaluation_directory(ROOT / "artifacts" / "evaluations", selected_id)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_started = time.perf_counter_ns()

    # Analyze the three PDFs before loading or consulting any case labels/mutations.
    analyses: dict[str, tuple[Mapping[str, Any], Path, Any, float]] = {}
    for source, pdf_path, _source_record in prepared:
        company_id = str(source["company_id"])
        analysis_started = time.perf_counter_ns()
        result = _run_registered_analysis(
            source,
            pdf_path,
            run_id=f"{selected_id}-{company_id}",
        )
        elapsed_ms = (time.perf_counter_ns() - analysis_started) / 1_000_000
        if result.parsed_pdf.source_sha256.lower() != str(source["sha256"]).lower():
            raise EvaluationInputError(f"年度分析输入哈希与来源登记不一致：{company_id}")
        if result.company_id != company_id or result.report_year != int(source["report_year"]):
            raise EvaluationInputError(f"年度分析输出公司/报告年不匹配：{company_id}")
        analyses[company_id] = (source, pdf_path, result, elapsed_ms)

    fact_records: list[dict[str, Any]] = []
    injections: list[dict[str, Any]] = []
    company_results: dict[str, Any] = {}
    for company_id, (source, pdf_path, result, analysis_elapsed_ms) in analyses.items():
        disclosure_records = _validate_disclosure_pages(source, pdf_path)
        direct_facts = _independent_fact_records(pdf_path, result)
        fact_records.extend({**item, "company_id": company_id} for item in direct_facts)
        company_injections = _inject_controlled_errors(source, pdf_path, result)
        injections.extend(company_injections)
        status_counts = Counter(str(item["verifier_status"]) for item in direct_facts)
        comp = result.comparability.to_dict()
        company_results[company_id] = {
            "source": _source_description(source),
            "factual_review_status": "agent_provisional_pending_human",
            "human_review_status": "pending",
            "excluded_from_human_golden_metrics": True,
            "fraud_or_misconduct_label": {"status": "not_assessed", "value": None},
            "analysis_run_id": result.run_id,
            "analysis_status": result.status,
            "analysis_elapsed_ms": analysis_elapsed_ms,
            "analysis_pipeline_fact_status_counts": dict(Counter(item.status for item in result.fact_verifications)),
            "direct_recheck_status_counts": {
                "verified": status_counts["verified"],
                "conflict": status_counts["conflict"],
                "insufficient_evidence": status_counts["insufficient_evidence"],
            },
            "direct_recheck_elapsed_ms": round(sum(float(item["elapsed_ms"]) for item in direct_facts), 3),
            "comparability": comp,
            "restatement_status_from_comparability_check": comp.get("restatement_status", "unknown"),
            "unknown_items": {
                "insufficient_evidence_fact_ids": [
                    item["fact"]["fact_id"]
                    for item in direct_facts
                    if item["verifier_status"] == "insufficient_evidence"
                ],
                "undisclosed_currency_fact_ids": [
                    item["fact"]["fact_id"]
                    for item in direct_facts
                    if item["fact"].get("currency") == "未披露"
                ],
                "fact_restatement_unknown_count": sum(
                    item["fact"].get("restatement_status") == "unknown" for item in direct_facts
                ),
                "comparability_needs_review": comp.get("status") == "insufficient_evidence",
                "comparability_conflict_count": len(comp.get("conflicts", [])),
            },
            "provisional_disclosures": disclosure_records,
            "reported_value_directions": _direction_summaries(
                {"facts": direct_facts, "comparability": comp}
            ),
            "facts": direct_facts,
            "controlled_error_cases": company_injections,
            "raw_fact_human_golden_denominator": 0,
        }
        company_dir = output_dir / "companies" / company_id
        company_dir.mkdir(parents=True, exist_ok=False)
        _write_json_new(company_dir / "analysis.json", result.archive_dict())
        _write_json_new(company_dir / "independent_rechecks.json", direct_facts)

    fact_coverage = compute_fact_coverage(fact_records)
    injection_metrics = compute_injection_metrics(injections)
    comparison_counts = Counter(
        "changed" if item["verifier_status"] != "verified" else "same"
        for item in fact_records
    )
    total_elapsed_ms = (time.perf_counter_ns() - total_started) / 1_000_000
    summary = {
        "protocol_id": protocol["protocol_id"],
        "protocol_version": protocol["protocol_version"],
        "evaluation_id": selected_id,
        "created_at": started_at,
        "status": "completed",
        "code_provenance": _git_provenance(),
        "model_called": False,
        "label_firewall": protocol["label_firewall"],
        "holdout_limitations": protocol["holdout_limitations"],
        "human_review_gate": protocol["human_review_gate"],
        "source_use_boundary": protocol["source_use_boundary"],
        "company_count": len(company_results),
        "company_ids": sorted(company_results),
        "fact_coverage": fact_coverage,
        "raw_sample_baseline_comparison": {
            "schema_only_baseline_accepted": len(fact_records),
            "independent_verifier_accepted": fact_coverage["verified_by_independent_checker"],
            "direct_recheck_changed_from_schema_only_baseline": comparison_counts["changed"],
            "conflict": fact_coverage["conflict"],
            "insufficient_evidence": fact_coverage["insufficient_evidence"],
            "note": "schema-only 是对照基线定义，不是历史产品或模型运行；保留 verifier verified/conflict/insufficient_evidence 原状态。",
        },
        "controlled_error_metrics": injection_metrics,
        "elapsed_ms": round(total_elapsed_ms, 3),
        "companies": company_results,
        "controlled_error_cases": injections,
    }
    _write_json_new(output_dir / "result.json", summary)
    _write_text_new(output_dir / "README.md", render_result_markdown(summary))
    return output_dir, summary


def _write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, ensure_ascii=False, indent=2, default=str)
            target.write("\n")
    except FileExistsError as exc:
        raise EvaluationInputError(f"拒绝覆盖已有评测文件：{path}") from exc


def _write_text_new(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as target:
            target.write(value)
    except FileExistsError as exc:
        raise EvaluationInputError(f"拒绝覆盖已有评测文件：{path}") from exc


def render_result_markdown(summary: Mapping[str, Any]) -> str:
    coverage = summary["fact_coverage"]
    injections = summary["controlled_error_metrics"]
    lines = [
        "# C-01 多公司评测审计结果",
        "",
        f"- evaluation_id：`{summary['evaluation_id']}`",
        f"- 创建时间：{summary['created_at']}",
        f"- 公司：{', '.join(summary['company_ids'])}",
        f"- 模型调用：`{str(summary['model_called']).lower()}`",
        "- 真实事实状态：`agent_provisional_pending_human`；人工 golden 与舞弊分类分母均为 0。",
        "- 茅台、五粮液为 post-hoc 公司隔离参考，不是盲测。",
        "",
        "## 原始事实覆盖（描述性，不是准确率）",
        "",
        f"共 {coverage['sample_denominator']} 条：独立核验 verified {coverage['verified_by_independent_checker']}、conflict {coverage['conflict']}、insufficient_evidence/unknown {coverage['insufficient_evidence']}。",
        "",
        "| 公司 | 事实分母 | verified | conflict | insufficient_evidence / unknown | 可比性 | 追溯状态 |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for company_id in sorted(summary["companies"]):
        company = summary["companies"][company_id]
        counts = company["direct_recheck_status_counts"]
        lines.append(
            f"| {company_id} | {sum(counts.values())} | {counts['verified']} | {counts['conflict']} | {counts['insufficient_evidence']} | {company['comparability']['status']} | {company['restatement_status_from_comparability_check']} |"
        )
    lines.extend(
        [
            "",
            "## 受控错误注入",
            "",
            f"分母 {injections['injected_error_denominator']}；schema-only 基线接受 {injections['schema_only_baseline_accepted']['numerator']}；独立核验错误接受 {injections['independent_verifier_error_accepted']['numerator']}、冲突拒绝 {injections['independent_verifier_rejected_as_conflict']['numerator']}、证据不足弃权 {injections['independent_verifier_abstained_as_insufficient_evidence']['numerator']}。弃权单独计数，不计作拒绝。",
            "",
            "| 错误类型 | 分母 | accepted | rejected/conflict | abstained/insufficient_evidence |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for family, counts in injections["by_error_family"].items():
        lines.append(
            f"| {family} | {counts['denominator']} | {counts.get('accepted', 0)} | {counts.get('rejected', 0)} | {counts.get('abstained', 0)} |"
        )
    lines.extend(
        [
            "",
            "不对 provisional 事实计算人工正确率、舞弊误报率或漏报率。披露值方向仅为同份年报原列示数值之间的关系，不构成增长、异常或舞弊判断。原文和页图仅允许本地开发/评测；未确认再分发许可。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在三份已登记 2024 年报上运行 C-01 多公司评测。")
    parser.add_argument("--evaluation-id", default=None, help="可选唯一产物 ID；已存在时失败且不会覆盖。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output_dir, summary = run_evaluation(args.evaluation_id)
    except (EvaluationInputError, OSError, ValueError) as exc:
        print(f"status=failed reason={exc}", file=sys.stderr)
        return 2
    print(
        f"status=completed evaluation_id={summary['evaluation_id']} companies={summary['company_count']} "
        f"raw_facts={summary['fact_coverage']['sample_denominator']} "
        f"injections={summary['controlled_error_metrics']['injected_error_denominator']}"
    )
    print(f"output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
