"""只读读取 v2 年度分析报告与独立核验 PDF 证据预览。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from finagent.core.annual_checkpoints import (
    AnnualCheckpointError,
    AnnualCheckpointRecord,
    _load_current_annual_checkpoint_with_verified_source,
)
from finagent.core.sample_provenance import resolve_sample_provenance
from finagent.core.annual_work_budget import (
    archived_budget_contract_v1,
)
from finagent.core.safe_paths import PathBoundaryError, validate_run_id
from finagent.api.narrative_reading_context import build_reading_context
from finagent.ingestion.parse_text_pdf import parse_text_pdf

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_REPORT_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_PDF_BYTES = 256 * 1024 * 1024
_MAX_PREVIEW_PIXELS = 12_000_000
_MAX_NARRATIVE_EVIDENCES = 8
_MAX_NARRATIVE_EVIDENCE_CHARS = 4000
_MAX_NARRATIVE_SNIPPET_CHARS = 1200
_MAX_DISCOVERY_FINDINGS = 768
_MAX_DISCOVERY_EVIDENCES = 3072
_MAX_DISCOVERY_EVIDENCE_CHARS = 4_000_000
_MAX_DISCOVERY_OPINION_CHARS = 1_200_000
_MAX_DISCOVERY_LIMITATIONS = 1_100
_MAX_DISCOVERY_QUOTE_CHARS = 300
_DISCOVERY_PROMPT_VERSIONS = frozenset({"annual_discovery_v1", "annual_discovery_v2", "annual_discovery_v3"})
_V2_SAFE_BATCH_FAILURES = frozenset(
    {
        "all_findings_rejected",
        "batch_missing",
        "definitive_conclusion_forbidden",
        "findings_invalid",
        "invalid_model_json",
        "invalid_model_response",
        "invalid_model_schema",
        "limitations_invalid",
        "limitation_contains_control_character",
        "limitation_contains_unquoted_digit",
        "limitation_invalid",
        "model_call_failed",
        "model_call_timeout",
        "model_response_too_large",
        "serialized_input_limit_breached",
    }
)
_V3_ATTEMPT_FAILURES = frozenset({
    "http_401", "http_403", "http_429", "http_5xx", "connection_error", "timeout",
    "invalid_response", "transport_error", "cancelled", "ledger_budget_exhausted",
    "ledger_provider_mismatch", "ledger_source_context_missing_or_unapproved",
    "ledger_unavailable", "ledger_policy_mismatch", "call_gate_closed",
    "call_deadline_exhausted", "audit_write_failed", "call_failed",
})
_V3_UNATTEMPTED_REASONS = frozenset({
    "awaiting_model", "workflow_time_budget_exhausted", "user_cancelled",
    "provider_budget_exhausted", "provider_admission_rejected",
    "authentication_failure_stopped", "consecutive_transport_failures_stopped",
    "parent_call_gate_closed",
})
_V4_UNATTEMPTED_REASONS = _V3_UNATTEMPTED_REASONS | frozenset({
    "discovery_stage_budget_exhausted",
})
_V4_DISPATCH_CLOSE_REASONS = frozenset({
    "discovery_stage_budget_exhausted", "workflow_time_budget_exhausted",
    "provider_budget_exhausted", "user_cancelled", "authentication_failure_stopped",
    "provider_admission_rejected", "consecutive_transport_failures_stopped",
})
_V3_BATCH_FAILURES = _V2_SAFE_BATCH_FAILURES | frozenset({"audit_write_failed"})
_V3_EXECUTION_KIND = "fintrace_annual_discovery_execution_v3"
_V4_EXECUTION_KIND = "fintrace_annual_discovery_execution_v4"
_V3_BINDING_FIELDS = {
    "source_sha256", "company_id", "report_year", "source_document_id", "analysis_mode",
    "provider_model_fingerprint", "model", "prompts_sha256", "parser_version", "plan_sha256",
    "m3_binding_sha256",
}
_V3_CONTRACT_FIELDS = {
    "kind", "plan_sha256", "identity_sha256", "binding", "parent_run_id",
    "provider_admission_count", "parent_provider_admission_count",
    "cumulative_provider_admission_count", "response_count", "reused_batches", "batches",
}
_V4_CONTRACT_FIELDS = _V3_CONTRACT_FIELDS | {"scheduler", "phase_budget", "timing"}
_V4_BINDING_FIELDS = _V3_BINDING_FIELDS | {"plan_version", "execution_version"}
_V3_BATCH_FIELDS = {"batch_index", "state", "resolution", "reason", "attempts"}
_V3_ATTEMPT_FIELDS = {
    "attempt_index", "audit_id", "admission_id", "provider_admitted", "status",
    "response_accepted", "accepted_findings", "reason",
}
_V3_COVERAGE_BATCH_FIELDS = {
    "batch_index", "status", "reason", "pages", "raw_chars", "accepted_findings",
    "rejected_findings", "restored_quotes", "resolution", "inherited_findings",
}
_MAX_V3_ATTEMPTS_PER_BATCH = 64
_MAX_V3_ATTEMPTS_TOTAL = 4096
_MAX_REVIEW_AUDIT_BYTES = 512 * 1024
_MAX_REVIEW_PROVIDER_ATTEMPTS = 3
_REVIEW_RETRYABLE_FAILURES = frozenset({"connection_error", "http_429", "http_5xx", "timeout"})
_REPARSE_POINT_ATTRIBUTE = 0x400
NARRATIVE_EVIDENCE_INTERPRETATION = "原文片段定位/模型观点引用，非独立因果核验"
DISCOVERY_EVIDENCE_INTERPRETATION = "年报原文定位，模型发现未经独立语义核验"


class AnnualReportReadError(Exception):
    """可映射为安全 HTTP 错误的归档读取问题。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AnnualReportArchive:
    run_id: str
    report: dict[str, Any]
    manifest_summary: dict[str, Any]
    verification_evidences: tuple[dict[str, Any], ...]
    narrative_evidences: tuple[dict[str, Any], ...]
    source_pdf_bytes: bytes
    source_provenance: dict[str, object]
    # Appended with a default so existing positional constructors remain valid.
    discovery_evidences: tuple[dict[str, Any], ...] = ()
    # Partial snapshots and R2 sealed final reports expose a small, safe handle.
    snapshot: dict[str, Any] | None = None


def load_annual_report(
    project_root: Path,
    run_id: str,
    *,
    _lineage: tuple[str, ...] = (),
) -> AnnualReportArchive:
    """Read a sealed annual report or a strictly verified R2 checkpoint."""

    checked_run_id = _checked_run_id(run_id)
    if checked_run_id in _lineage or len(_lineage) >= 32:
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 父运行链存在循环或超过层级上限。")
    lineage = (*_lineage, checked_run_id)
    root = Path(project_root).resolve()
    _safe_archive_path(root, ("artifacts", "runs"), directory=True, missing_code="run_not_found")
    _safe_archive_path(root, ("artifacts", "runs", checked_run_id), directory=True, missing_code="run_not_found")
    _safe_archive_path(root, ("artifacts", "reports"), directory=True, missing_code="report_not_found")
    _safe_archive_path(root, ("artifacts", "reports", checked_run_id), directory=True, missing_code="report_not_found")

    manifest_path = _safe_archive_path(
        root,
        ("artifacts", "runs", checked_run_id, "manifest.json"),
        directory=False,
        missing_code="archive_incomplete",
    )
    source_path = _safe_archive_path(
        root,
        ("artifacts", "runs", checked_run_id, "source.pdf"),
        directory=False,
        missing_code="archive_incomplete",
    )
    manifest_bytes = _read_checked_file(manifest_path, _MAX_MANIFEST_BYTES, "archive_incomplete")
    source_pdf_bytes = _read_checked_file(source_path, _MAX_SOURCE_PDF_BYTES, "archive_incomplete")
    manifest = _parse_json_object(manifest_bytes, "manifest_invalid")

    if not source_pdf_bytes.startswith(b"%PDF-"):
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 source.pdf 不是有效的 PDF 文件。")
    actual_source_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
    inputs = manifest.get("inputs")
    outputs = manifest.get("outputs")
    if not isinstance(inputs, Mapping) or not isinstance(outputs, Mapping):
        raise AnnualReportReadError(422, "manifest_invalid", "年度分析 manifest 缺少必要字段。")
    manifest_source_sha256 = inputs.get("source_pdf_sha256")
    if not isinstance(manifest_source_sha256, str) or _SHA256.fullmatch(manifest_source_sha256) is None:
        raise AnnualReportReadError(422, "manifest_invalid", "年度分析 manifest 的来源哈希格式无效。")

    expected_run_path = f"runs/{checked_run_id}"
    expected_report_dir = f"reports/{checked_run_id}"
    expected_source_path = f"{expected_run_path}/source.pdf"
    expected_report_path = f"{expected_report_dir}/report.json"
    manifest_status = manifest.get("status")
    if (
        manifest.get("run_id") != checked_run_id
        or manifest_status not in {"running", "completed", "completed_with_issues", "failed", "interrupted"}
        or inputs.get("source_pdf_archive_path") != expected_source_path
        or outputs.get("run_dir") != expected_run_path
        or outputs.get("report_dir") != expected_report_dir
        or outputs.get("report_json") != expected_report_path
    ):
        raise AnnualReportReadError(409, "archive_identity_mismatch", "manifest 与请求的年度分析归档标识或固定路径不一致。")
    if actual_source_sha256 != manifest_source_sha256.lower():
        raise AnnualReportReadError(409, "source_sha256_mismatch", "归档 PDF 与 manifest 记录的来源哈希不一致。")
    if (
        not isinstance(manifest.get("company_id"), str)
        or not isinstance(manifest.get("document_id"), str)
        or type(manifest.get("report_year")) is not int
    ):
        raise AnnualReportReadError(422, "manifest_invalid", "年度分析 manifest 的公司或报告身份字段无效。")

    report_path: Path | None
    try:
        report_path = _safe_archive_path(
            root,
            ("artifacts", "reports", checked_run_id, "report.json"),
            directory=False,
            missing_code="report_not_found",
        )
    except AnnualReportReadError as exc:
        if exc.code != "report_not_found":
            raise
        report_path = None

    checkpoint = _load_checkpoint_for_reader(root, checked_run_id, actual_source_sha256)
    snapshot_metadata: dict[str, Any] | None = None
    if report_path is not None:
        report_file_bytes = _read_checked_file(report_path, _MAX_REPORT_BYTES, "report_not_found")
        report_file = _parse_json_object(report_file_bytes, "report_invalid")
        report_file_sha256 = hashlib.sha256(report_file_bytes).hexdigest()
    else:
        report_file = None
        report_file_bytes = b""
        report_file_sha256 = ""

    if checkpoint is None:
        if report_file is None:
            raise AnnualReportReadError(404, "report_not_found", "找不到该运行的年度分析报告或正式检查点。")
        if manifest_status not in {"completed", "completed_with_issues"}:
            raise AnnualReportReadError(404, "report_not_found", "找不到该运行的年度正式检查点。")
        if _has_v3_execution_contract(report_file):
            raise AnnualReportReadError(409, "checkpoint_final_seal_missing", "年度 v3 最终报告缺少匹配的检查点封存记录。")
        report = report_file
        report_bytes = report_file_bytes
        report_sha256 = report_file_sha256
    elif not checkpoint.is_partial:
        checkpoint_state_is_final = checkpoint.state in {"completed", "completed_with_issues"}
        manifest_matches_seal = (
            manifest_status in {"completed", "completed_with_issues"}
            and checkpoint.state == manifest_status
        )
        running_manifest_has_seal = manifest_status == "running" and checkpoint_state_is_final
        if (
            not checkpoint_state_is_final
            or not (manifest_matches_seal or running_manifest_has_seal)
            or report_file is None
            or checkpoint.report != report_file
        ):
            raise AnnualReportReadError(409, "checkpoint_final_seal_mismatch", "最终报告与检查点封存记录不一致。")
        # A final pointer is itself the durable seal. If the manifest update is
        # the second half of publication, the report and immutable checkpoint
        # agree, so exposing the sealed state does not infer completion.
        report = checkpoint.report
        report_bytes = _canonical_report_bytes(report)
        report_sha256 = checkpoint.report_sha256
        snapshot_metadata = checkpoint.metadata
    elif (
        manifest_status in {"completed", "completed_with_issues"}
        and checkpoint.report.get("kind") == "fintrace_annual_analysis_report"
        and _has_v3_execution_contract(checkpoint.report)
        and checkpoint.state in {"awaiting_model", "running", "partial", "cancelled", "failed", "interrupted"}
    ):
        # CLI finalization publishes report.json and the terminal manifest
        # before the final checkpoint. During that narrow window, project the
        # last strictly verified v3 pointer as partial and ignore the newer,
        # not-yet-sealed report file.
        report = checkpoint.report
        report_bytes = _canonical_report_bytes(report)
        report_sha256 = checkpoint.report_sha256
        snapshot_metadata = checkpoint.metadata
    elif checkpoint.is_partial:
        if not _partial_checkpoint_matches_manifest(checkpoint.state, manifest_status, checkpoint.report):
            raise AnnualReportReadError(409, "checkpoint_state_mismatch", "部分检查点状态与 manifest 终态不匹配。")
        report = checkpoint.report
        report_bytes = _canonical_report_bytes(report)
        report_sha256 = checkpoint.report_sha256
        snapshot_metadata = checkpoint.metadata
    else:
        raise AnnualReportReadError(409, "checkpoint_state_mismatch", "检查点封存状态与 manifest 不匹配。")

    if report.get("kind") != "fintrace_annual_analysis_report":
        raise AnnualReportReadError(422, "report_invalid", "年度检查点或 report.json 不是受支持的 FINTRACE 年度分析报告。")
    if (
        report.get("run_id") != checked_run_id
        or report.get("source_sha256") != manifest_source_sha256.lower()
        or report.get("company_id") != manifest.get("company_id")
        or report.get("report_year") != manifest.get("report_year")
        or report.get("source_document_id") != manifest.get("document_id")
    ):
        raise AnnualReportReadError(409, "report_identity_mismatch", "报告与 manifest 的 run_id、来源哈希或文档元数据不一致。")
    if (
        isinstance(report.get("model_discovery"), Mapping)
        and "execution_contract" in report["model_discovery"]
        and manifest_status in {"completed", "completed_with_issues"}
        and checkpoint is None
    ):
        raise AnnualReportReadError(409, "checkpoint_final_seal_missing", "年度 v3 最终报告缺少匹配的检查点封存记录。")

    verification_evidences = _collect_verified_evidences(report, manifest_source_sha256.lower())
    narrative_evidences = _collect_narrative_evidences(
        report,
        source_pdf_bytes,
        expected_document_id=manifest["document_id"],
        expected_sha256=manifest_source_sha256.lower(),
        verified_evidence_ids={item["evidence_id"] for item in verification_evidences},
    )
    discovery_evidences = _collect_discovery_evidences(
        report,
        source_path,
        expected_run_id=checked_run_id,
        expected_company_id=str(manifest["company_id"]),
        expected_report_year=manifest["report_year"],
        expected_document_id=manifest["document_id"],
        expected_sha256=manifest_source_sha256.lower(),
        expected_analysis_mode=manifest.get("mode"),
        nested_discovery=(
            report.get("model_investigation", {}).get("discovery")
            if isinstance(report.get("model_investigation"), Mapping)
            else None
        ),
    )
    discovery = report.get("model_discovery")
    validated_parent_archive: AnnualReportArchive | None = None
    if isinstance(discovery, Mapping) and "execution_contract" in discovery:
        contract = discovery.get("execution_contract")
        if not isinstance(contract, Mapping):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 执行契约无效。")
        contract_parent = contract.get("parent_run_id")
        if snapshot_metadata is None or snapshot_metadata.get("parent_run_id") != contract_parent:
            raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 父运行与检查点元数据不一致。")
        if contract_parent is not None:
            validated_parent_archive = _validate_v3_parent_archive(
                root,
                contract_parent,
                current_run_id=checked_run_id,
                current_report=report,
                current_contract=contract,
                lineage=lineage,
            )
        model_review = report.get("model_review")
        if isinstance(model_review, Mapping) and model_review.get("reason") == "review_reused_for_matching_inputs":
            if validated_parent_archive is None:
                raise AnnualReportReadError(409, "annual_review_reuse_invalid", "复用的年度评审缺少严格验证的父运行。")
            _validate_v3_reused_model_review(
                root,
                current_report=report,
                current_contract=contract,
                parent_archive=validated_parent_archive,
            )
        elif (
            isinstance(model_review, Mapping)
            and model_review.get("status") == "completed"
            and model_review.get("model_called") is False
        ):
            raise AnnualReportReadError(422, "annual_review_invalid", "完成的年度评审必须绑定本次调用或受控复用证明。")
        elif isinstance(model_review, Mapping) and model_review.get("status") == "completed":
            _validate_v3_called_model_review(root, report, contract)
    raw_mode = manifest.get("mode")
    supported_modes = {"deterministic", "m3_screening", "model_investigation", "integrated_analysis"}
    manifest_mode = raw_mode if isinstance(raw_mode, str) and raw_mode in supported_modes else None
    if raw_mode is not None and manifest_mode is None:
        manifest_mode = "unknown"
    manifest_summary = {
        "run_id": checked_run_id,
        "status": manifest["status"],
        "mode": manifest_mode,
        "company_id": manifest["company_id"],
        "report_year": manifest["report_year"],
        "document_id": manifest["document_id"],
        "source_pdf_archive_path": expected_source_path,
        "report_json_path": expected_report_path,
        "source_pdf_sha256": actual_source_sha256,
        "report_sha256": report_sha256,
    }
    if checkpoint is not None:
        manifest_summary["snapshot_sha256"] = checkpoint.snapshot_sha256
        manifest_summary["manifest_binding_sha256"] = checkpoint.manifest_binding_sha256
    source_provenance = resolve_sample_provenance(
        actual_source_sha256,
        company_id=manifest["company_id"],
        report_year=manifest["report_year"],
        document_id=manifest["document_id"],
    )
    return AnnualReportArchive(
        run_id=checked_run_id,
        report=report,
        manifest_summary=manifest_summary,
        verification_evidences=verification_evidences,
        narrative_evidences=narrative_evidences,
        discovery_evidences=discovery_evidences,
        source_pdf_bytes=source_pdf_bytes,
        source_provenance=source_provenance,
        snapshot=snapshot_metadata,
    )


def _validate_v3_parent_archive(
    root: Path,
    parent_run_id: str,
    *,
    current_run_id: str,
    current_report: Mapping[str, Any],
    current_contract: Mapping[str, Any],
    lineage: tuple[str, ...],
) -> AnnualReportArchive:
    """Recheck the immutable parent report and its source-bound accepted rows."""

    try:
        parent = load_annual_report(root, parent_run_id, _lineage=lineage)
    except AnnualReportReadError:
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 父运行未通过当前严格归档校验。") from None
    parent_discovery = parent.report.get("model_discovery")
    if not isinstance(parent_discovery, Mapping):
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 父运行缺少发现报告。")
    parent_contract = parent_discovery.get("execution_contract")
    if not isinstance(current_contract, Mapping):
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度子运行缺少严格执行绑定。")
    current_kind = current_contract.get("kind")
    if current_kind == _V3_EXECUTION_KIND:
        expected_contract_fields = _V3_CONTRACT_FIELDS
    elif current_kind == _V4_EXECUTION_KIND:
        expected_contract_fields = _V4_CONTRACT_FIELDS
    else:
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度父子运行使用了未知执行版本。")
    if (
        not isinstance(current_contract, Mapping)
        or set(current_contract) != expected_contract_fields
        or not isinstance(parent_contract, Mapping)
        or set(parent_contract) != expected_contract_fields
        or parent_contract.get("kind") != current_kind
    ):
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度父子运行缺少同版本的严格执行绑定。")
    if (
        parent.run_id == current_run_id
        or parent.report.get("source_sha256") != current_report.get("source_sha256")
        or parent.report.get("company_id") != current_report.get("company_id")
        or parent.report.get("report_year") != current_report.get("report_year")
        or parent.report.get("source_document_id") != current_report.get("source_document_id")
        or current_contract.get("identity_sha256") != parent_contract.get("identity_sha256")
        or current_contract.get("plan_sha256") != parent_contract.get("plan_sha256")
        or current_contract.get("binding") != parent_contract.get("binding")
        or current_contract.get("parent_provider_admission_count")
        != parent_contract.get("cumulative_provider_admission_count")
    ):
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 父子运行身份或累计计数不一致。")
    if parent.snapshot is None or (
        not parent.snapshot.get("is_partial")
        and parent.snapshot.get("state") != "completed_with_issues"
    ):
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 父运行没有可续跑检查点。")

    parent_coverage = parent_discovery.get("coverage")
    current_discovery = current_report.get("model_discovery")
    current_coverage = current_discovery.get("coverage") if isinstance(current_discovery, Mapping) else None
    parent_rows = parent_coverage.get("batch_results", []) if isinstance(parent_coverage, Mapping) else None
    current_rows = current_coverage.get("batch_results", []) if isinstance(current_coverage, Mapping) else None
    current_findings = current_discovery.get("findings", []) if isinstance(current_discovery, Mapping) else None
    parent_findings = parent_discovery.get("findings", [])
    if not all(isinstance(items, list) for items in (parent_rows, current_rows, current_findings, parent_findings)):
        raise AnnualReportReadError(422, "annual_discovery_parent_invalid", "年度 v3 父子批次投影无效。")
    parent_counts = {row["batch_index"]: row["accepted_findings"] for row in parent_rows if isinstance(row, Mapping)}
    current_counts = {
        row["batch_index"]: row
        for row in current_rows
        if isinstance(row, Mapping)
    }
    reused = current_contract.get("reused_batches")
    parent_state_by_index = {row["batch_index"]: row["status"] for row in parent_rows if isinstance(row, Mapping)}
    if (
        len(parent_counts) != len(parent_rows)
        or len(current_counts) != len(current_rows)
        or not isinstance(reused, list)
        or any(index not in parent_counts or parent_counts[index] != current_counts[index].get("inherited_findings") for index in current_counts)
        or any(parent_state_by_index.get(index) != "succeeded" for index in reused)
    ):
        raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 父子批次继承或成功重用关系无效。")
    current_by_finding_id = {
        finding.get("finding_id"): finding
        for finding in current_findings
        if isinstance(finding, Mapping) and isinstance(finding.get("finding_id"), str)
    }
    for finding in parent_findings:
        finding_id = finding.get("finding_id") if isinstance(finding, Mapping) else None
        if not isinstance(finding_id, str) or current_by_finding_id.get(finding_id) != finding:
            raise AnnualReportReadError(409, "annual_discovery_parent_invalid", "年度 v3 子报告未完整保留父报告合格发现。")
    return parent


def _validate_v3_reused_model_review(
    root: Path,
    *,
    current_report: Mapping[str, Any],
    current_contract: Mapping[str, Any],
    parent_archive: AnnualReportArchive,
) -> None:
    """Verify a no-call review reuse against its strict parent and original audit."""

    current_review = current_report.get("model_review")
    parent_report = parent_archive.report
    parent_review = parent_report.get("model_review")
    required_fields = {
        "status", "reason", "assessment", "summary", "reasons", "follow_up_items",
        "limitations", "model_called", "call_count", "call_attempt_count",
        "audit_artifacts", "source_identity",
    }
    if (
        not isinstance(current_review, Mapping)
        or set(current_review) != required_fields
        or current_review.get("status") != "completed"
        or current_review.get("reason") != "review_reused_for_matching_inputs"
        or current_review.get("model_called") is not False
        or type(current_review.get("call_count")) is not int
        or current_review.get("call_count") != 0
        or type(current_review.get("call_attempt_count")) is not int
        or current_review.get("call_attempt_count") != 0
        or not isinstance(parent_review, Mapping)
        or set(parent_review) != required_fields
        or parent_review.get("status") != "completed"
    ):
        _raise_review_reuse_invalid()

    binding = current_contract.get("binding")
    parent_discovery = parent_report.get("model_discovery")
    parent_contract = parent_discovery.get("execution_contract") if isinstance(parent_discovery, Mapping) else None
    parent_binding = parent_contract.get("binding") if isinstance(parent_contract, Mapping) else None
    model_name = binding.get("model") if isinstance(binding, Mapping) else None
    if (
        not isinstance(binding, Mapping)
        or not isinstance(parent_binding, Mapping)
        or dict(binding) != dict(parent_binding)
        or not isinstance(model_name, str)
        or parent_review.get("source_identity") != _review_report_identity(parent_report)
        or current_review.get("source_identity") != _review_report_identity(current_report)
        or any(
            current_review.get(field) != parent_review.get(field)
            for field in ("assessment", "summary", "reasons", "follow_up_items", "limitations")
        )
        or not isinstance(current_review.get("audit_artifacts"), list)
        or current_review.get("audit_artifacts") != parent_review.get("audit_artifacts")
    ):
        _raise_review_reuse_invalid()

    parent_investigation = parent_report.get("model_investigation")
    current_investigation = current_report.get("model_investigation")
    if (
        not isinstance(parent_investigation, Mapping)
        or not isinstance(current_investigation, Mapping)
        or parent_investigation.get("model_name") != model_name
        or current_investigation.get("model_name") != model_name
    ):
        _raise_review_reuse_invalid()
    try:
        from finagent.reports.investigation_append import _review_reuse_signature

        parent_signature = _review_reuse_signature(
            parent_report,
            parent_investigation,
            _review_report_identity(parent_report),
            model_name=model_name,
        )
        current_signature = _review_reuse_signature(
            current_report,
            current_investigation,
            _review_report_identity(current_report),
            model_name=model_name,
        )
    except Exception:
        _raise_review_reuse_invalid()
    if parent_signature != current_signature:
        _raise_review_reuse_invalid()

    parent_reason = parent_review.get("reason")
    if parent_reason == "review_reused_for_matching_inputs":
        # load_annual_report() strictly checked the parent's own reuse proof,
        # including its earlier ancestor. Keeping the exact audit index makes
        # that verified origin traceable through any bounded child chain.
        if current_review.get("audit_artifacts") != parent_review.get("audit_artifacts"):
            _raise_review_reuse_invalid()
        return
    if parent_reason is not None:
        _raise_review_reuse_invalid()
    _validate_original_review_audit(
        root,
        report=parent_report,
        review=parent_review,
        binding=parent_binding,
        investigation=parent_investigation,
    )


def _validate_v3_called_model_review(
    root: Path,
    report: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> None:
    review = report.get("model_review")
    investigation = report.get("model_investigation")
    binding = contract.get("binding")
    required_fields = {
        "status", "reason", "assessment", "summary", "reasons", "follow_up_items",
        "limitations", "model_called", "call_count", "call_attempt_count",
        "audit_artifacts", "source_identity",
    }
    if (
        not isinstance(review, Mapping)
        or set(review) != required_fields
        or review.get("reason") is not None
        or review.get("model_called") is not True
        or not isinstance(binding, Mapping)
        or not isinstance(investigation, Mapping)
        or investigation.get("model_name") != binding.get("model")
        or review.get("source_identity") != _review_report_identity(report)
    ):
        raise AnnualReportReadError(422, "annual_review_invalid", "完成的年度评审调用身份或字段无效。")
    _validate_original_review_audit(
        root,
        report=report,
        review=review,
        binding=binding,
        investigation=investigation,
    )


def _validate_original_review_audit(
    root: Path,
    *,
    report: Mapping[str, Any],
    review: Mapping[str, Any],
    binding: Mapping[str, Any],
    investigation: Mapping[str, Any],
) -> None:
    """Check the durable annual-review request/response and its bounded attempts."""

    if (
        review.get("model_called") is not True
        or type(review.get("call_count")) is not int
        or review.get("call_count") != 1
        or type(review.get("call_attempt_count")) is not int
        or not 1 <= review.get("call_attempt_count", 0) <= _MAX_REVIEW_PROVIDER_ATTEMPTS
        or not isinstance(review.get("audit_artifacts"), list)
        or len(review["audit_artifacts"]) != 1
    ):
        _raise_review_reuse_invalid()
    audit = review["audit_artifacts"][0]
    audit_fields = {"audit_dir", "request_path", "response_path", "failure_path", "status", "prompt_version"}
    if not isinstance(audit, Mapping) or set(audit) != audit_fields:
        _raise_review_reuse_invalid()
    relative_dir = audit.get("audit_dir")
    if not isinstance(relative_dir, str) or "\\" in relative_dir:
        _raise_review_reuse_invalid()
    parts = relative_dir.split("/")
    if len(parts) != 3 or parts[0] != "runs":
        _raise_review_reuse_invalid()
    origin_run_id = _checked_run_id(parts[1])
    audit_id = parts[2]
    if (
        not _is_uuid4(audit_id)
        or audit.get("request_path") != f"{relative_dir}/request.json"
        or audit.get("response_path") != f"{relative_dir}/response.json"
        or audit.get("failure_path") is not None
        or audit.get("status") != "succeeded"
        or audit.get("prompt_version") != "annual_review_v2"
    ):
        _raise_review_reuse_invalid()

    if origin_run_id != report.get("run_id"):
        _raise_review_reuse_invalid()
    origin_investigation = report.get("model_investigation")
    if not isinstance(origin_investigation, Mapping):
        _raise_review_reuse_invalid()
    model_name = binding.get("model")
    if not isinstance(model_name, str):
        _raise_review_reuse_invalid()
    try:
        from finagent.agents.annual_review import parse_model_review
        from finagent.reports.investigation_append import _review_reuse_request_material

        identity = _review_report_identity(report)
        material = _review_reuse_request_material(
            report,
            origin_investigation,
            identity,
            model_name=model_name,
        )
    except Exception:
        _raise_review_reuse_invalid()

    run_dir_parts = ("artifacts", "runs", origin_run_id, audit_id)
    request_path = _safe_archive_path(root, (*run_dir_parts, "request.json"), directory=False, missing_code="archive_incomplete")
    response_path = _safe_archive_path(root, (*run_dir_parts, "response.json"), directory=False, missing_code="archive_incomplete")
    request_record = _parse_json_object(
        _read_checked_file(request_path, _MAX_REVIEW_AUDIT_BYTES, "archive_incomplete"),
        "annual_review_reuse_invalid",
    )
    response_record = _parse_json_object(
        _read_checked_file(response_path, _MAX_REVIEW_AUDIT_BYTES, "archive_incomplete"),
        "annual_review_reuse_invalid",
    )
    request_fields = {
        "audit_id", "status", "started_at", "finished_at", "duration_seconds",
        "prompt_version", "evidence_refs", "model", "timeout_seconds", "messages", "stream",
    }
    response_fields = {
        "status", "started_at", "finished_at", "duration_seconds", "text", "model",
        "response_id", "usage", "provider_admission_count", "response_count", "attempt_records",
    }
    messages = [
        {"role": "system", "content": material["system_prompt"]},
        {"role": "user", "content": material["user_content"]},
    ]
    if (
        set(request_record) != request_fields
        or request_record.get("audit_id") != audit_id
        or request_record.get("status") != "started"
        or request_record.get("prompt_version") != "annual_review_v2"
        or request_record.get("evidence_refs") != material["evidence_refs"]
        or request_record.get("model") != model_name
        or not _finite_positive_number(request_record.get("timeout_seconds"))
        or request_record.get("messages") != messages
        or request_record.get("stream") is not False
        or not _nonempty_bounded_text(request_record.get("started_at"), 64)
        or request_record.get("finished_at") is not None
        or request_record.get("duration_seconds") is not None
        or set(response_record) != response_fields
        or response_record.get("status") != "succeeded"
        or not _safe_provider_model_text(response_record.get("model"))
        or not _nonempty_bounded_text(response_record.get("response_id"), 256)
        or not _nonempty_bounded_text(response_record.get("text"), 12_000)
        or not _nonempty_bounded_text(response_record.get("started_at"), 64)
        or not _nonempty_bounded_text(response_record.get("finished_at"), 64)
        or not _finite_nonnegative_number(response_record.get("duration_seconds"))
        or type(response_record.get("provider_admission_count")) is not int
        or response_record.get("provider_admission_count") != review.get("call_attempt_count")
        or type(response_record.get("response_count")) is not int
        or response_record.get("response_count") != review.get("call_count")
    ):
        _raise_review_reuse_invalid()

    attempts = response_record.get("attempt_records")
    if not isinstance(attempts, list) or len(attempts) != review.get("call_attempt_count"):
        _raise_review_reuse_invalid()
    admitted = 0
    accepted_responses = 0
    last_text: str | None = None
    seen_attempt_ids: set[str] = set()
    for index, attempt in enumerate(attempts, start=1):
        attempt_fields = {
            "attempt_index", "audit_id", "admission_id", "provider_admitted", "response_accepted",
            "accepted_findings", "status", "reason",
        }
        if (
            not isinstance(attempt, Mapping)
            or set(attempt) != attempt_fields
            or type(attempt.get("attempt_index")) is not int
            or attempt.get("attempt_index") != index
            or not _is_uuid4(attempt.get("audit_id"))
            or attempt.get("audit_id") in seen_attempt_ids
            or not _is_uuid4(attempt.get("admission_id"))
            or attempt.get("provider_admitted") is not True
            or type(attempt.get("response_accepted")) is not bool
            or attempt.get("accepted_findings") != 0
        ):
            _raise_review_reuse_invalid()
        seen_attempt_ids.add(str(attempt["audit_id"]))
        admitted += 1
        attempt_dir_name = f"attempt-{index:02d}"
        attempt_dir = _safe_archive_path(root, (*run_dir_parts, attempt_dir_name), directory=True, missing_code="archive_incomplete")
        attempt_request_path = _safe_archive_path(root, (*run_dir_parts, attempt_dir_name, "request.json"), directory=False, missing_code="archive_incomplete")
        attempt_request = _parse_json_object(
            _read_checked_file(attempt_request_path, _MAX_REVIEW_AUDIT_BYTES, "archive_incomplete"),
            "annual_review_reuse_invalid",
        )
        attempt_request_fields = {
            "status", "attempt_index", "audit_id", "started_at", "prompt_version", "evidence_refs",
            "model", "timeout_seconds", "messages", "stream",
        }
        if (
            set(attempt_request) != attempt_request_fields
            or attempt_request.get("status") != "started"
            or attempt_request.get("attempt_index") != index
            or attempt_request.get("audit_id") != attempt.get("audit_id")
            or attempt_request.get("prompt_version") != "annual_review_v2"
            or attempt_request.get("evidence_refs") != material["evidence_refs"]
            or attempt_request.get("model") != model_name
            or not _finite_positive_number(attempt_request.get("timeout_seconds"))
            or attempt_request.get("messages") != messages
            or attempt_request.get("stream") is not False
            or not _nonempty_bounded_text(attempt_request.get("started_at"), 64)
        ):
            _raise_review_reuse_invalid()
        if attempt.get("response_accepted") is True:
            if index != len(attempts) or attempt.get("status") != "succeeded" or attempt.get("reason") is not None:
                _raise_review_reuse_invalid()
            attempt_response_path = _safe_archive_path(root, (*run_dir_parts, attempt_dir_name, "response.json"), directory=False, missing_code="archive_incomplete")
            attempt_response = _parse_json_object(
                _read_checked_file(attempt_response_path, _MAX_REVIEW_AUDIT_BYTES, "archive_incomplete"),
                "annual_review_reuse_invalid",
            )
            if set(attempt_response) != {
                "status", "attempt_index", "audit_id", "finished_at", "text", "model", "response_id",
                "usage", "provider_admitted", "admission_id",
            } or (
                attempt_response.get("status") != "succeeded"
                or attempt_response.get("attempt_index") != index
                or attempt_response.get("audit_id") != attempt.get("audit_id")
                or attempt_response.get("model") != response_record.get("model")
                or attempt_response.get("provider_admitted") is not True
                or attempt_response.get("admission_id") != attempt.get("admission_id")
                or attempt_response.get("text") != response_record.get("text")
                or attempt_response.get("response_id") != response_record.get("response_id")
                or attempt_response.get("usage") != response_record.get("usage")
                or not _nonempty_bounded_text(attempt_response.get("finished_at"), 64)
            ):
                _raise_review_reuse_invalid()
            accepted_responses += 1
            last_text = str(attempt_response["text"])
        else:
            reason = attempt.get("reason")
            if (
                index == len(attempts)
                or attempt.get("status") != "failed"
                or reason not in _REVIEW_RETRYABLE_FAILURES
            ):
                _raise_review_reuse_invalid()
            failure_path = _safe_archive_path(root, (*run_dir_parts, attempt_dir_name, "failure.json"), directory=False, missing_code="archive_incomplete")
            failure_record = _parse_json_object(
                _read_checked_file(failure_path, _MAX_REVIEW_AUDIT_BYTES, "archive_incomplete"),
                "annual_review_reuse_invalid",
            )
            if (
                set(failure_record) != {"status", "attempt_index", "audit_id", "finished_at", "category", "provider_admitted"}
                or failure_record.get("status") != "failed"
                or failure_record.get("attempt_index") != index
                or failure_record.get("audit_id") != attempt.get("audit_id")
                or failure_record.get("category") != reason
                or failure_record.get("provider_admitted") is not True
                or not _nonempty_bounded_text(failure_record.get("finished_at"), 64)
            ):
                _raise_review_reuse_invalid()
    if admitted != response_record.get("provider_admission_count") or accepted_responses != 1 or last_text != response_record.get("text"):
        _raise_review_reuse_invalid()
    try:
        parsed = parse_model_review(
            str(response_record["text"]),
            allowed_ids=set(material["allowed_ids"]),
            no_priority_allowed=material["no_priority_allowed"],
        )
    except Exception:
        _raise_review_reuse_invalid()
    if any(parsed.get(field) != review.get(field) for field in ("assessment", "summary", "reasons", "follow_up_items", "limitations")):
        _raise_review_reuse_invalid()


def _review_report_identity(report: Mapping[str, Any]) -> dict[str, Any]:
    identity = {
        "run_id": report.get("run_id"),
        "company_id": report.get("company_id"),
        "report_year": report.get("report_year"),
        "source_document_id": report.get("source_document_id"),
        "source_sha256": report.get("source_sha256"),
    }
    if (
        not isinstance(identity["run_id"], str)
        or not isinstance(identity["company_id"], str)
        or type(identity["report_year"]) is not int
        or not isinstance(identity["source_document_id"], str)
        or not isinstance(identity["source_sha256"], str)
        or _LOWER_SHA256.fullmatch(identity["source_sha256"]) is None
    ):
        _raise_review_reuse_invalid()
    return identity


def _raise_review_reuse_invalid() -> None:
    raise AnnualReportReadError(409, "annual_review_reuse_invalid", "年度评审复用未通过来源、请求或原始审计链校验。")


def _nonempty_bounded_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _safe_provider_model_text(value: object) -> bool:
    return (
        _nonempty_bounded_text(value, 128)
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _finite_positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value > 0


def _finite_nonnegative_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0


def _partial_checkpoint_matches_manifest(
    checkpoint_state: str,
    manifest_status: str,
    report: Mapping[str, Any],
) -> bool:
    """Accept stable and exact two-file publication-window pairings."""

    # Partial checkpoint semantics belong to the v3 recovery contract. Legacy
    # v1/v2 reports remain on their existing final-report path only.
    if not _has_v3_execution_contract(report):
        return False
    allowed = {
        # A worker can persist a terminal partial checkpoint before updating
        # the manifest. The pointer remains authoritative during that window.
        "running": {"awaiting_model", "running", "partial", "cancelled", "failed", "interrupted"},
        # CLI failure handling publishes its manifest after the last durable
        # model checkpoint; retain that strictly validated partial projection.
        "failed": {"awaiting_model", "running", "partial", "failed"},
        # The job API exposes cancelled, while the durable run manifest uses
        # interrupted. The checkpoint keeps the user-visible cancellation.
        "interrupted": {"interrupted", "cancelled"},
    }
    return checkpoint_state in allowed.get(manifest_status, set())


def _has_v3_execution_contract(report: Mapping[str, Any]) -> bool:
    discovery = report.get("model_discovery")
    return isinstance(discovery, Mapping) and "execution_contract" in discovery


def _load_checkpoint_for_reader(root: Path, run_id: str, source_sha256: str) -> AnnualCheckpointRecord | None:
    try:
        # source_sha256 was computed from the race-checked source.pdf bytes at
        # the beginning of this strict reader, never copied from job metadata.
        return _load_current_annual_checkpoint_with_verified_source(root, run_id, source_sha256)
    except AnnualCheckpointError as exc:
        raise AnnualReportReadError(exc.status_code, exc.code, exc.message) from None


def _canonical_report_bytes(report: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise AnnualReportReadError(422, "report_invalid", "年度分析报告包含无法安全读取的值。") from None


def render_evidence_preview(
    archive: AnnualReportArchive,
    evidence_id: str,
    *,
    focus: bool = False,
) -> tuple[bytes, int]:
    """只渲染 value_region 所在页，并仅标出该值的 bbox。页码从 1 开始。"""

    if not isinstance(evidence_id, str) or len(evidence_id) > 256:
        raise AnnualReportReadError(404, "evidence_not_found", "该独立核验证据不存在。")
    evidence = next(
        (item for item in archive.verification_evidences if item.get("evidence_id") == evidence_id),
        None,
    )
    if evidence is None:
        raise AnnualReportReadError(404, "evidence_not_found", "该独立核验证据不存在。")

    region = evidence.get("value_region")
    if not isinstance(region, Mapping):
        raise AnnualReportReadError(422, "evidence_region_invalid", "证据缺少有效的 value_region 坐标。")
    page_number = region.get("page")
    bbox = region.get("bbox")
    pdf_page = evidence.get("pdf_page")
    if (
        type(page_number) is not int
        or page_number < 1
        or type(pdf_page) is not int
        or pdf_page != page_number
        or not isinstance(bbox, Mapping)
    ):
        raise AnnualReportReadError(422, "evidence_region_invalid", "证据页码或 value_region 坐标无效。")
    coordinates: list[float] = []
    for key in ("x0", "y0", "x1", "y1"):
        value = bbox.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise AnnualReportReadError(422, "evidence_region_invalid", "证据 value_region 坐标无效。")
        coordinates.append(float(value))
    x0, y0, x1, y1 = coordinates
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise AnnualReportReadError(422, "evidence_region_invalid", "证据 value_region 坐标超出页面范围。")
    if focus:
        return _render_page_region_preview(
            archive.source_pdf_bytes,
            page_number,
            (x0, y0, x1, y1),
            focus=True,
            narrative=False,
        )
    return _render_page_region_preview(
        archive.source_pdf_bytes,
        page_number,
        (x0, y0, x1, y1),
        focus=False,
        narrative=False,
    )


def render_narrative_evidence_preview(
    archive: AnnualReportArchive,
    evidence_id: str,
    *,
    focus: bool = False,
) -> tuple[bytes, int]:
    """Render only a source-bound narrative block; this is not verification evidence."""

    if not isinstance(evidence_id, str) or len(evidence_id) > 256:
        raise AnnualReportReadError(404, "narrative_evidence_not_found", "该同源叙述证据不存在。")
    evidence = next(
        (item for item in archive.narrative_evidences if item.get("evidence_id") == evidence_id),
        None,
    )
    if evidence is None:
        raise AnnualReportReadError(404, "narrative_evidence_not_found", "该同源叙述证据不存在。")
    bbox = evidence.get("bbox")
    page_number = evidence.get("pdf_page")
    if type(page_number) is not int or not isinstance(bbox, Mapping):
        raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据的页码或坐标无效。")
    coordinates = tuple(bbox.get(key) for key in ("x0", "y0", "x1", "y1"))
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in coordinates):
        raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据坐标无效。")
    return _render_page_region_preview(
        archive.source_pdf_bytes,
        page_number,
        tuple(float(value) for value in coordinates),
        focus=focus,
        narrative=True,
    )


def render_discovery_evidence_preview(
    archive: AnnualReportArchive,
    evidence_id: str,
    *,
    focus: bool = False,
) -> tuple[bytes, int]:
    """Render only an exact source-bound block cited by an unverified finding."""

    if not isinstance(evidence_id, str) or len(evidence_id) > 256:
        raise AnnualReportReadError(404, "discovery_evidence_not_found", "该开放式发现证据不存在。")
    evidence = next(
        (item for item in archive.discovery_evidences if item.get("evidence_id") == evidence_id),
        None,
    )
    if evidence is None:
        raise AnnualReportReadError(404, "discovery_evidence_not_found", "该开放式发现证据不存在。")
    bbox = evidence.get("bbox")
    page_number = evidence.get("pdf_page")
    if type(page_number) is not int or not isinstance(bbox, Mapping):
        raise AnnualReportReadError(422, "discovery_evidence_region_invalid", "发现证据的页码或坐标无效。")
    coordinates = tuple(bbox.get(key) for key in ("x0", "y0", "x1", "y1"))
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in coordinates
    ):
        raise AnnualReportReadError(422, "discovery_evidence_region_invalid", "发现证据坐标无效。")
    return _render_page_region_preview(
        archive.source_pdf_bytes,
        page_number,
        tuple(float(value) for value in coordinates),
        focus=focus,
        narrative=False,
    )


def _render_page_region_preview(
    source_pdf_bytes: bytes,
    page_number: int,
    coordinates: tuple[float, float, float, float],
    *,
    focus: bool,
    narrative: bool,
) -> tuple[bytes, int]:
    error_code = "narrative_evidence_region_invalid" if narrative else "evidence_region_invalid"
    try:
        import pymupdf

        document = pymupdf.open(stream=source_pdf_bytes, filetype="pdf")
    except Exception:
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 无法打开。") from None

    try:
        if document.needs_pass:
            raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 受密码保护，无法生成预览。")
        if page_number < 1 or page_number > document.page_count:
            raise AnnualReportReadError(422, error_code, "证据页码超出 PDF 页数。")
        page = document[page_number - 1]
        rect = pymupdf.Rect(*coordinates)
        visible_region = rect * page.rotation_matrix
        page_bounds = page.rect
        if (
            rect.x0 < 0
            or rect.y0 < 0
            or rect.x1 <= rect.x0
            or rect.y1 <= rect.y0
            or visible_region.x0 < -0.1
            or visible_region.y0 < -0.1
            or visible_region.x1 > page_bounds.x1 + 0.1
            or visible_region.y1 > page_bounds.y1 + 0.1
        ):
            raise AnnualReportReadError(422, error_code, "证据坐标与 PDF 页面范围不一致。")
        page.draw_rect(
            rect,
            color=(0.16, 0.31, 0.72) if narrative else (0.82, 0.12, 0.12),
            fill=(0.84, 0.89, 1.0) if narrative else (1.0, 0.92, 0.25),
            fill_opacity=0.22 if narrative else 0.35,
            width=1.4,
            overlay=True,
        )
        render_region = page_bounds
        if focus:
            margin_x = min(48.0, max(18.0, visible_region.width * 0.04))
            margin_y = min(36.0, max(14.0, visible_region.height * 0.8))
            padded_region = visible_region + (-margin_x, -margin_y, margin_x, margin_y)
            render_region = padded_region & page_bounds
        width, height = float(render_region.width), float(render_region.height)
        if width <= 0 or height <= 0:
            raise AnnualReportReadError(422, "source_pdf_invalid", "PDF 页面区域尺寸无效。")
        scale_ceiling = 3.0 if focus else 1.5
        scale = min(scale_ceiling, math.sqrt(_MAX_PREVIEW_PIXELS / (width * height)))
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=render_region if focus else None,
            alpha=False,
            annots=True,
        )
        return pixmap.tobytes("png"), page_number
    except AnnualReportReadError:
        raise
    except Exception:
        raise AnnualReportReadError(422, "preview_failed", "无法生成该证据的 PDF 页预览。") from None
    finally:
        document.close()


def _checked_run_id(run_id: str) -> str:
    try:
        checked = validate_run_id(run_id)
    except PathBoundaryError:
        raise AnnualReportReadError(400, "invalid_run_id", "run_id 格式无效。") from None
    # Reject names that normalize specially on Windows filesystems.
    stem = checked.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if checked.endswith(".") or stem in reserved:
        raise AnnualReportReadError(400, "invalid_run_id", "run_id 格式无效。")
    return checked


def _safe_archive_path(root: Path, parts: tuple[str, ...], *, directory: bool, missing_code: str) -> Path:
    anchor = root.resolve()
    current = anchor
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            _raise_missing(missing_code)
        except OSError:
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档路径无法安全读取。") from None
        if _is_reparse_point(info):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档包含符号链接或 reparse point。")
        try:
            resolved = current.resolve(strict=True)
        except OSError:
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档路径无法安全读取。") from None
        if resolved == anchor or not resolved.is_relative_to(anchor):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档路径超出项目目录。")
        is_final = index == len(parts) - 1
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档目录结构无效。")
        if is_final and directory and not stat.S_ISDIR(info.st_mode):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档目录结构无效。")
        if is_final and not directory and not stat.S_ISREG(info.st_mode):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件类型无效。")
    return current


def _raise_missing(code: str) -> None:
    if code == "run_not_found":
        raise AnnualReportReadError(404, code, "找不到该年度分析运行。")
    if code == "report_not_found":
        raise AnnualReportReadError(404, code, "找不到该运行的年度分析报告。")
    raise AnnualReportReadError(409, code, "年度分析归档不完整。")


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _read_checked_file(path: Path, limit: int, missing_code: str) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError:
        _raise_missing(missing_code)
    except OSError:
        raise AnnualReportReadError(409, "archive_file_unreadable", "年度分析归档文件无法读取。") from None
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件类型无效。")
    if before.st_size > limit:
        raise AnnualReportReadError(422, "archive_file_too_large", "年度分析归档文件超过允许大小。")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件在读取期间发生变化。")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = None
            data = stream.read(limit + 1)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件在读取期间发生变化。")
    except AnnualReportReadError:
        raise
    except FileNotFoundError:
        _raise_missing(missing_code)
    except OSError:
        raise AnnualReportReadError(409, "archive_file_unreadable", "年度分析归档文件无法读取。") from None
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > limit:
        raise AnnualReportReadError(422, "archive_file_too_large", "年度分析归档文件超过允许大小。")
    return data


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _parse_json_object(data: bytes, error_code: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AnnualReportReadError(422, error_code, "年度分析归档 JSON 格式无效。") from None
    if not isinstance(payload, dict):
        raise AnnualReportReadError(422, error_code, "年度分析归档 JSON 顶层必须是对象。")
    return payload


def _collect_verified_evidences(report: Mapping[str, Any], expected_sha256: str) -> tuple[dict[str, Any], ...]:
    confirmed = report.get("confirmed")
    if not isinstance(confirmed, Mapping) or not isinstance(confirmed.get("metrics"), list):
        raise AnnualReportReadError(422, "report_invalid", "年度分析报告缺少有效的 confirmed 指标区。")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for metric in confirmed["metrics"]:
        if not isinstance(metric, Mapping):
            raise AnnualReportReadError(422, "report_invalid", "年度分析报告的 confirmed 指标结构无效。")
        if metric.get("source_sha256") != expected_sha256 or metric.get("source_document_id") != report.get("source_document_id"):
            raise AnnualReportReadError(409, "evidence_source_mismatch", "confirmed 指标与报告来源不一致。")
        verifications = metric.get("verifications", [])
        records = metric.get("verification_evidences", [])
        if not isinstance(verifications, list) or not isinstance(records, list):
            raise AnnualReportReadError(422, "report_invalid", "年度分析报告的核验引用结构无效。")
        verified_ids: set[str] = set()
        fact_id = metric.get("fact_id")
        for verification in verifications:
            if not isinstance(verification, Mapping):
                raise AnnualReportReadError(422, "report_invalid", "年度分析报告的核验记录结构无效。")
            ids = verification.get("evidence_ids", [])
            if not isinstance(ids, list):
                raise AnnualReportReadError(422, "report_invalid", "年度分析报告的核验证据引用结构无效。")
            if (
                verification.get("status") == "verified"
                and verification.get("target_type") == "financial_fact"
                and isinstance(fact_id, str)
                and verification.get("target_id") == fact_id
            ):
                verified_ids.update(item for item in ids if isinstance(item, str))
        for evidence in records:
            if not isinstance(evidence, Mapping):
                raise AnnualReportReadError(422, "report_invalid", "年度分析报告的 verification_evidences 结构无效。")
            evidence_id = evidence.get("evidence_id")
            # Extraction/legacy evidence and any unverified references never
            # enter this index, even if they are present in the archived JSON.
            if not isinstance(evidence_id, str) or evidence_id not in verified_ids:
                continue
            if evidence.get("source_sha256") != expected_sha256 or evidence.get("document_id") != report.get("source_document_id"):
                raise AnnualReportReadError(409, "evidence_source_mismatch", "独立核验证据与报告来源不一致。")
            normalized = dict(evidence)
            existing = evidence_by_id.get(evidence_id)
            if existing is not None and existing != normalized:
                raise AnnualReportReadError(409, "evidence_id_conflict", "报告中存在内容不一致的重复 evidence_id。")
            evidence_by_id[evidence_id] = normalized
    return tuple(evidence_by_id[key] for key in sorted(evidence_by_id))


def _collect_discovery_evidences(
    report: Mapping[str, Any],
    source_pdf_path: Path,
    *,
    expected_run_id: str,
    expected_company_id: str,
    expected_report_year: int,
    expected_document_id: str,
    expected_sha256: str,
    expected_analysis_mode: object,
    nested_discovery: object,
) -> tuple[dict[str, Any], ...]:
    """Rebuild cited discovery evidence from the exact archived source PDF."""

    top_present = "model_discovery" in report
    investigation = report.get("model_investigation")
    nested_present = isinstance(investigation, Mapping) and "discovery" in investigation
    if not top_present:
        if nested_present:
            raise AnnualReportReadError(
                409,
                "annual_discovery_layer_mismatch",
                "年度开放式发现缺少顶层来源绑定记录。",
            )
        return ()
    layer = report.get("model_discovery")
    if not isinstance(layer, Mapping):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现结构无效。")
    base_layer_fields = {
        "kind", "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
        "status", "prompt_version", "model_called", "model_call_count",
        "model_call_attempt_count", "coverage", "findings", "evidences", "audit_artifacts",
        "limitations", "reason", "fraud_conclusion",
    }
    has_execution_contract = "execution_contract" in layer
    layer_fields = base_layer_fields | ({"execution_contract"} if has_execution_contract else set())
    if set(layer) != layer_fields:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现字段集合无效。")
    if nested_present and nested_discovery != layer:
        raise AnnualReportReadError(
            409,
            "annual_discovery_layer_mismatch",
            "模型调查附录与顶层开放式发现不一致。",
        )
    expected_identity = {
        "run_id": expected_run_id,
        "company_id": expected_company_id,
        "report_year": expected_report_year,
        "source_document_id": expected_document_id,
        "source_sha256": expected_sha256,
    }
    if layer.get("kind") != "fintrace_annual_discovery" or any(
        layer.get(key) != value for key, value in expected_identity.items()
    ):
        raise AnnualReportReadError(
            409,
            "annual_discovery_identity_mismatch",
            "年度开放式发现与当前归档来源身份不一致。",
        )
    prompt_version = layer.get("prompt_version")
    if not isinstance(prompt_version, str) or prompt_version not in _DISCOVERY_PROMPT_VERSIONS:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现提示词版本无效。")
    status = layer.get("status")
    if not isinstance(status, str) or status not in {"completed", "partial", "failed", "not_called"}:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现状态无效。")
    reason = layer.get("reason")
    if reason is not None and (
        not isinstance(reason, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,99}", reason) is None
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现原因代码无效。")
    coverage = layer.get("coverage")
    if not isinstance(coverage, Mapping):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖记录无效。")
    execution_summary: dict[str, Any] | None = None
    if has_execution_contract:
        if prompt_version == "annual_discovery_v2":
            execution_summary = _validate_v3_execution_contract(
                layer.get("execution_contract"),
                expected_run_id=expected_run_id,
                expected_company_id=expected_company_id,
                expected_report_year=expected_report_year,
                expected_document_id=expected_document_id,
                expected_sha256=expected_sha256,
                expected_analysis_mode=expected_analysis_mode,
                m3_screening=report.get("m3_screening"),
                coverage=coverage,
            )
        elif prompt_version == "annual_discovery_v3":
            raw_contract = layer.get("execution_contract")
            execution_summary = _validate_v4_execution_contract(
                raw_contract,
                expected_run_id=expected_run_id,
                expected_company_id=expected_company_id,
                expected_report_year=expected_report_year,
                expected_document_id=expected_document_id,
                expected_sha256=expected_sha256,
                expected_analysis_mode=expected_analysis_mode,
                m3_screening=report.get("m3_screening"),
                coverage=coverage,
            )
            scheduler = raw_contract.get("scheduler") if isinstance(raw_contract, Mapping) else None
            dispatch_reason = scheduler.get("dispatch_closed_reason") if isinstance(scheduler, Mapping) else None
            if (
                dispatch_reason is not None
                and status in {"not_called", "partial", "failed"}
                and reason != dispatch_reason
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 发现原因与调度闭合原因不一致。")
        else:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "该提示词版本不能包含年度执行契约。")
    _validate_discovery_coverage(
        coverage,
        status,
        str(prompt_version),
        execution_contract=layer.get("execution_contract") if has_execution_contract else None,
    )
    if has_execution_contract and execution_summary is not None:
        for row in coverage["batch_results"]:
            batch = execution_summary["batches"].get(row["batch_index"])
            if batch is not None:
                batch["accepted_findings"] = row["accepted_findings"]
    if layer.get("fraud_conclusion") is not None:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现不能包含舞弊结论。")
    called = layer.get("model_called")
    call_count = layer.get("model_call_count")
    attempt_count = layer.get("model_call_attempt_count")
    if (
        not isinstance(called, bool)
        or type(call_count) is not int
        or type(attempt_count) is not int
        or call_count < 0
        or attempt_count < call_count
        or called != (attempt_count > 0)
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现模型调用计数无效。")
    if status == "not_called" and attempt_count != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "未调用状态不能包含模型请求。")
    if status == "not_called" and call_count != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "未调用状态不能包含模型响应。")
    if has_execution_contract and (
        execution_summary is None
        or attempt_count != execution_summary["provider_admission_count"]
        or call_count != execution_summary["response_count"]
        or called != (execution_summary["provider_admission_count"] > 0)
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度执行契约与当前模型调用计数不一致。")
    if not has_execution_contract and coverage["source_status"] == "parsed" and (
        attempt_count != coverage["processed_batches"]
        or coverage["successful_batches"] + (coverage.get("partial_batches", 0) if prompt_version in {"annual_discovery_v2", "annual_discovery_v3"} else 0) > call_count
        or call_count > attempt_count
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "已解析来源的请求/响应数与结束批次不一致。")
    if status == "completed" and not has_execution_contract and (
        call_count != coverage["planned_batches"]
        or attempt_count != coverage["planned_batches"]
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "完成状态的模型调用数与批次数不一致。")

    raw_findings = layer.get("findings")
    raw_evidences = layer.get("evidences")
    limitations = layer.get("limitations")
    if (
        not isinstance(raw_findings, list)
        or len(raw_findings) > _MAX_DISCOVERY_FINDINGS
        or not isinstance(raw_evidences, list)
        or len(raw_evidences) > _MAX_DISCOVERY_EVIDENCES
        or not isinstance(limitations, list)
        or len(limitations) > _MAX_DISCOVERY_LIMITATIONS
        or any(not isinstance(item, str) or len(item) > 400 for item in limitations)
        or sum(len(item) for item in limitations if isinstance(item, str)) > _MAX_DISCOVERY_LIMITATIONS * 400
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现条目超过结构或字符上限。")
    if prompt_version in {"annual_discovery_v2", "annual_discovery_v3"} and not has_execution_contract:
        accepted_count_ceiling = sum(
            int(item["accepted_findings"])
            for item in coverage["batch_results"]
            if isinstance(item, Mapping) and type(item.get("accepted_findings")) is int
        )
        if len(raw_findings) > accepted_count_ceiling:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "归档发现数量超过批次验收计数。")
    accepted_batch_count = coverage["successful_batches"] + (
        coverage.get("partial_batches", 0) if prompt_version in {"annual_discovery_v2", "annual_discovery_v3"} else 0
    )
    if (
        has_execution_contract
        and sum(int(item.get("accepted_findings", 0)) for item in coverage["batch_results"])
        == 0
        and (raw_findings or raw_evidences)
        or not has_execution_contract and accepted_batch_count == 0 and (raw_findings or raw_evidences)
    ):
        raise AnnualReportReadError(
            422,
            "annual_discovery_invalid",
            "没有成功处理的批次时不能归档模型发现或引文证据。",
        )

    audit_artifacts = layer.get("audit_artifacts")
    if not isinstance(audit_artifacts, list) or len(audit_artifacts) > 128:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计索引无效。")
    for audit in audit_artifacts:
        if not isinstance(audit, Mapping) or set(audit) != {
            "batch_index", "audit_dir", "request_path", "response_path", "failure_path", "status", "prompt_version"
        }:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计记录字段无效。")
        batch_index = audit.get("batch_index")
        audit_status = audit.get("status")
        if (
            type(batch_index) is not int or batch_index < 1 or batch_index > 128
            or not isinstance(audit_status, str)
            or audit_status not in {"started", "succeeded", "failed", "incomplete"}
            or audit.get("prompt_version") != prompt_version
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计记录值无效。")
        audit_dir = audit.get("audit_dir")
        if audit_dir is not None and (
            not isinstance(audit_dir, str) or not _safe_audit_relative_path(audit_dir)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计路径无效。")
        for path_field in ("request_path", "response_path", "failure_path"):
            value = audit.get(path_field)
            if value is not None and (not isinstance(value, str) or not _safe_audit_relative_path(value)):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计文件路径无效。")

    evidence_records: dict[str, Mapping[str, Any]] = {}
    total_chars = 0
    for record in raw_evidences:
        if not isinstance(record, Mapping):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据结构无效。")
        if set(record) != {
            "evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index",
            "start_char", "bbox", "raw_text",
        }:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据字段集合无效。")
        evidence_id = record.get("evidence_id")
        raw_text = record.get("raw_text")
        bbox = record.get("bbox")
        if (
            not isinstance(evidence_id, str)
            or not re.fullmatch(r"adisc-[0-9a-f]{12}-p[1-9][0-9]*-b[0-9]+-c[0-9]+-[0-9a-f]{12}", evidence_id)
            or evidence_id in evidence_records
            or record.get("source_document_id") != expected_document_id
            or record.get("source_sha256") != expected_sha256
            or type(record.get("pdf_page")) is not int
            or record["pdf_page"] < 1
            or type(record.get("block_index")) is not int
            or record["block_index"] < 0
            or type(record.get("start_char")) is not int
            or record["start_char"] < 0
            or not isinstance(raw_text, str)
            or not raw_text.strip()
            or len(raw_text) > 1200
            or not isinstance(bbox, Mapping)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据字段无效。")
        coordinates: dict[str, float] = {}
        for key in ("x0", "y0", "x1", "y1"):
            value = bbox.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据坐标无效。")
            coordinates[key] = float(value)
        if coordinates["x0"] < 0 or coordinates["y0"] < 0 or coordinates["x1"] <= coordinates["x0"] or coordinates["y1"] <= coordinates["y0"]:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据坐标范围无效。")
        if evidence_id != _annual_discovery_evidence_id(
            expected_sha256,
            record["pdf_page"],
            record["block_index"],
            record["start_char"],
        ):
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现证据 ID 与来源坐标不一致。")
        total_chars += len(raw_text)
        if total_chars > _MAX_DISCOVERY_EVIDENCE_CHARS:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据超过专用字符上限。")
        evidence_records[evidence_id] = record

    cited_by: dict[str, set[str]] = {}
    quotes_by_evidence: dict[str, list[dict[str, str]]] = {}
    finding_ids: set[str] = set()
    findings_by_batch: dict[int, int] = {}
    total_opinion_chars = 0
    total_quote_chars = 0
    for finding in raw_findings:
        if not isinstance(finding, Mapping):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现条目结构无效。")
        finding_fields = {
            "finding_id", "title", "observation", "why_it_matters", "follow_up", "priority",
            "classification", "verification_status", "evidence_ids", "citations",
        }
        if has_execution_contract:
            finding_fields.add("batch_index")
        if set(finding) != finding_fields:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现字段集合无效。")
        finding_id = finding.get("finding_id")
        priority = finding.get("priority")
        if (
            not isinstance(finding_id, str)
            or not re.fullmatch(r"disc:[0-9a-f]{12}-[0-9a-f]{20}", finding_id)
            or not finding_id.startswith(f"disc:{expected_sha256[:12]}-")
            or finding_id in finding_ids
            or finding.get("classification") != "unverified_model_finding"
            or finding.get("verification_status") != "unverified"
            or not isinstance(priority, str)
            or priority not in {"high", "medium", "low"}
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现条目标识或状态无效。")
        if has_execution_contract:
            batch_index = finding.get("batch_index")
            batch = execution_summary["batches"].get(batch_index) if type(batch_index) is int else None
            if (
                type(batch_index) is not int
                or batch is None
                or batch["accepted_findings"] <= 0
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度发现条目缺少有效的所属批次。")
            findings_by_batch[batch_index] = findings_by_batch.get(batch_index, 0) + 1
        finding_ids.add(finding_id)
        for field, limit in (("title", 100), ("observation", 600), ("why_it_matters", 400), ("follow_up", 400)):
            value = finding.get(field)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > limit
                or re.search(r"[0-9]", value)
                or any(ord(char) < 32 for char in value)
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现文本超过字段上限。")
            total_opinion_chars += len(value)
        if total_opinion_chars > _MAX_DISCOVERY_OPINION_CHARS:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现文本超过专用总字符上限。")
        citations = finding.get("citations")
        evidence_ids = finding.get("evidence_ids")
        is_v2 = prompt_version in {"annual_discovery_v2", "annual_discovery_v3"}
        if (
            not isinstance(citations, list)
            or not 1 <= len(citations) <= 4
            or not isinstance(evidence_ids, list)
            or any(not isinstance(item, str) for item in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
            or not is_v2 and len(evidence_ids) != len(citations)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现引用列表无效。")
        cited_ids: set[str] = set()
        citation_id_order: list[str] = []
        citation_pairs: set[tuple[str, str]] = set()
        for citation in citations:
            if not isinstance(citation, Mapping) or set(citation) != {"evidence_id", "quote"}:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现原文引用结构无效。")
            evidence_id = citation.get("evidence_id")
            quote = citation.get("quote")
            source = evidence_records.get(evidence_id) if isinstance(evidence_id, str) else None
            if (
                source is None
                or not isinstance(quote, str)
                or not quote.strip()
                or len(quote) > _MAX_DISCOVERY_QUOTE_CHARS
                or len(quote.strip()) < 2
                or quote not in str(source.get("raw_text", ""))
            ):
                raise AnnualReportReadError(409, "annual_discovery_quote_mismatch", "开放式发现引用与当前 PDF 原文不一致。")
            total_quote_chars += len(quote)
            if total_quote_chars > _MAX_DISCOVERY_FINDINGS * 4 * _MAX_DISCOVERY_QUOTE_CHARS:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现引用超过专用字符上限。")
            cited_ids.add(evidence_id)
            citation_id_order.append(evidence_id)
            pair = (evidence_id, quote)
            if is_v2 and pair in citation_pairs:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现包含重复的证据引用对。")
            citation_pairs.add(pair)
            cited_by.setdefault(evidence_id, set()).add(finding_id)
            quotes_by_evidence.setdefault(evidence_id, []).append(
                {"finding_id": finding_id, "quote": quote}
            )
        if cited_ids != set(evidence_ids) or is_v2 and evidence_ids != list(dict.fromkeys(citation_id_order)):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现引用 ID 与证据 ID 列表不一致。")
    if set(evidence_records) != set(cited_by):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现包含未被实际引用的证据。")
    if has_execution_contract and any(
        findings_by_batch.get(index, 0) != batch["accepted_findings"]
        for index, batch in execution_summary["batches"].items()
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "报告发现数量与 v3 批次去重并集计数不一致。")
    if not evidence_records:
        return ()

    try:
        parsed = parse_text_pdf(source_pdf_path, expected_document_id)
    except Exception:
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 无法复核开放式发现证据。") from None
    if parsed.source_sha256 != expected_sha256:
        raise AnnualReportReadError(409, "source_sha256_mismatch", "重新解析 PDF 的来源哈希与归档记录不一致。")
    pages = {page.page_number: page for page in parsed.pages}
    normalized: list[dict[str, Any]] = []
    for evidence_id, record in evidence_records.items():
        page = pages.get(record["pdf_page"])
        block_index = record["block_index"]
        if page is None or block_index >= len(page.blocks):
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现证据页码或文字块不存在。")
        block = page.blocks[block_index]
        raw_text = str(record["raw_text"])
        start_char = int(record["start_char"])
        if block.block_index != block_index or block.text[start_char : start_char + len(raw_text)] != raw_text:
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现片段与解析后的原文位置不一致。")
        bbox = record["bbox"]
        expected_box = (block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1)
        actual_box = tuple(float(bbox[key]) for key in ("x0", "y0", "x1", "y1"))
        if any(abs(actual - expected) > 0.01 for actual, expected in zip(actual_box, expected_box)):
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现坐标与 PDF 解析结果不一致。")
        normalized.append({
            "evidence_id": evidence_id,
            "source_document_id": expected_document_id,
            "source_sha256": expected_sha256,
            "pdf_page": record["pdf_page"],
            "block_index": block_index,
            "start_char": start_char,
            "bbox": {key: actual_box[index] for index, key in enumerate(("x0", "y0", "x1", "y1"))},
            "raw_text": raw_text,
            "finding_ids": sorted(cited_by[evidence_id]),
            "citations": sorted(quotes_by_evidence[evidence_id], key=lambda item: (item["finding_id"], item["quote"])),
        })
    return tuple(sorted(normalized, key=lambda item: item["evidence_id"]))


def _annual_discovery_evidence_id(source_sha256: str, page: int, block_index: int, start_char: int) -> str:
    material = f"{source_sha256}\n{page}\n{block_index}\n{start_char}".encode("ascii")
    digest = hashlib.sha256(material).hexdigest()[:12]
    return f"adisc-{source_sha256[:12]}-p{page}-b{block_index}-c{start_char}-{digest}"


def _m3_semantic_binding_sha256(value: object, binding: Mapping[str, Any]) -> str:
    """Rebuild the execution layer's pure semantic M3 input projection.

    This intentionally excludes generated IDs and verification timestamps so
    the binding follows financial inputs rather than incidental archive IDs.
    Keep the selected fields and canonical JSON shape aligned with
    ``annual_recovery._m3_semantic_binding_sha256``.
    """

    if not isinstance(value, Mapping):
        return ""
    facts: list[dict[str, Any]] = []
    fields = (
        "metric_id", "source_document_id", "source_sha256", "company_id",
        "report_year", "period_start", "period_end", "period_type",
        "frequency", "comparison_role", "raw_value", "normalized_value",
        "unit_multiplier", "unit", "currency", "scope", "statement_type",
        "restatement_status",
    )
    for section_name in ("extraction", "explicit_unit_facts"):
        section = value.get(section_name)
        rows = section.get("facts") if isinstance(section, Mapping) else None
        if not isinstance(rows, list):
            continue
        for fact in rows:
            if isinstance(fact, Mapping):
                facts.append({key: fact.get(key) for key in fields})
    try:
        facts.sort(key=lambda row: _canonical_report_bytes(row))
        projection = {
            "kind": "fintrace_m3_semantic_binding_v1",
            "source_sha256": str(binding.get("source_sha256", "")).lower(),
            "company_id": binding.get("company_id"),
            "report_year": binding.get("report_year"),
            "source_document_id": binding.get("source_document_id"),
            "analysis_mode": binding.get("analysis_mode"),
            "rule_version": value.get("rule_version"),
            "facts": facts,
        }
        return hashlib.sha256(_canonical_report_bytes(projection)).hexdigest()
    except AnnualReportReadError:
        return ""


def _validate_v4_execution_contract(
    value: object,
    *,
    expected_run_id: str,
    expected_company_id: str,
    expected_report_year: int,
    expected_document_id: str,
    expected_sha256: str,
    expected_analysis_mode: object,
    m3_screening: object,
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the versioned v4 envelope, then reuse v3 batch invariants."""

    if (
        not isinstance(value, Mapping)
        or set(value) != _V4_CONTRACT_FIELDS
        or value.get("kind") != _V4_EXECUTION_KIND
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 执行契约字段集合无效。")
    binding = value.get("binding")
    if (
        not isinstance(binding, Mapping)
        or set(binding) != _V4_BINDING_FIELDS
        or binding.get("plan_version") != "annual_discovery_plan_v3"
        or binding.get("execution_version") != "execution_v4"
        or binding.get("parser_version") != "annual-discovery-parser-v3-execution-v4"
        or not isinstance(value.get("identity_sha256"), str)
        or _LOWER_SHA256.fullmatch(value.get("identity_sha256", "")) is None
        or hashlib.sha256(_canonical_report_bytes(binding)).hexdigest() != value.get("identity_sha256")
    ):
        raise AnnualReportReadError(409, "annual_discovery_binding_mismatch", "年度 v4 执行身份或版本不一致。")

    phase_budget = value.get("phase_budget")
    if not isinstance(phase_budget, Mapping):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 阶段预算无效。")
    scope = phase_budget.get("scope")
    if scope not in {"new_run", "resume"}:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 预算范围无效。")
    expected_scope = "resume" if value.get("parent_run_id") is not None else "new_run"
    if scope != expected_scope:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 预算范围与父运行关系不一致。")
    expected_budget = archived_budget_contract_v1(scope=scope)
    if dict(phase_budget) != expected_budget:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 阶段预算与受控配置不一致。")

    scheduler = value.get("scheduler")
    scheduler_fields = {
        "requested_max_concurrency", "effective_max_concurrency", "inflight_peak",
        "checkpoint_coordinator_count", "checkpoint_publications", "dispatch_closed_reason",
        "time_deadline_reason", "inflight_drained", "inflight_count",
    }
    if not isinstance(scheduler, Mapping) or set(scheduler) != scheduler_fields:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 调度记录字段集合无效。")
    requested = scheduler.get("requested_max_concurrency")
    effective = scheduler.get("effective_max_concurrency")
    peak = scheduler.get("inflight_peak")
    publications = scheduler.get("checkpoint_publications")
    active = scheduler.get("inflight_count")
    closed_reason = scheduler.get("dispatch_closed_reason")
    time_deadline_reason = scheduler.get("time_deadline_reason")
    if (
        type(requested) is not int or not 1 <= requested <= 3
        or type(effective) is not int or not 1 <= effective <= 3
        or type(peak) is not int or not 0 <= peak <= effective
        or type(scheduler.get("checkpoint_coordinator_count")) is not int
        or scheduler.get("checkpoint_coordinator_count") != 1
        or type(publications) is not int or publications < 0
        or type(active) is not int or not 0 <= active <= peak
        or type(scheduler.get("inflight_drained")) is not bool
        or scheduler.get("inflight_drained") != (active == 0)
        or closed_reason is not None and closed_reason not in _V4_DISPATCH_CLOSE_REASONS
        or time_deadline_reason not in {
            None, "discovery_stage_budget_exhausted", "workflow_time_budget_exhausted",
        }
        or time_deadline_reason is not None and time_deadline_reason != closed_reason
        or closed_reason == "workflow_time_budget_exhausted"
        and time_deadline_reason != "workflow_time_budget_exhausted"
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 调度边界无效。")

    timing = value.get("timing")
    timing_fields = {
        "source_parse_seconds", "planning_seconds", "source_parse_reused",
        "discovery_elapsed_seconds", "main_work_remaining_seconds",
    }
    if not isinstance(timing, Mapping) or set(timing) != timing_fields:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 计时字段集合无效。")
    for name in ("source_parse_seconds", "planning_seconds", "discovery_elapsed_seconds"):
        elapsed = timing.get(name)
        if type(elapsed) not in {int, float} or not math.isfinite(float(elapsed)) or elapsed < 0:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 阶段耗时无效。")
    remaining = timing.get("main_work_remaining_seconds")
    if remaining is not None and (
        type(remaining) not in {int, float} or not math.isfinite(float(remaining)) or remaining < 0
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 剩余时限无效。")
    if type(timing.get("source_parse_reused")) is not bool:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 解析复用标记无效。")

    # Preserve the original v4 digest check above. Only after it succeeds is
    # the v4 envelope projected to the immutable v3 batch validator's shape.
    raw_batches = value.get("batches")
    coverage_rows = coverage.get("batch_results")
    if not isinstance(raw_batches, list):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 批次计划无效。")
    coverage_by_index = {
        row.get("batch_index"): row
        for row in coverage_rows
        if isinstance(row, Mapping) and type(row.get("batch_index")) is int
    } if isinstance(coverage_rows, list) else {}
    for raw_batch in raw_batches:
        if not isinstance(raw_batch, Mapping):
            continue  # The immutable v3 validator below reports the structural error.
        attempts = raw_batch.get("attempts")
        if not isinstance(attempts, list):
            continue
        accepted_responses = 0
        for raw_attempt in attempts:
            if not isinstance(raw_attempt, Mapping):
                continue  # The immutable v3 validator below reports the structural error.
            accepted = raw_attempt.get("accepted_findings")
            response_accepted = raw_attempt.get("response_accepted")
            if response_accepted is True:
                accepted_responses += 1
                if type(accepted) is not int or not 0 <= accepted <= 3:
                    raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 单次响应发现数超出三条上限。")
        row = coverage_by_index.get(raw_batch.get("batch_index"))
        if not isinstance(row, Mapping):
            continue  # The coverage validator below reports missing or malformed rows.
        accepted_total = row.get("accepted_findings")
        rejected = row.get("rejected_findings")
        restored = row.get("restored_quotes")
        inherited = row.get("inherited_findings")
        if not all(type(item) is int and item >= 0 for item in (accepted_total, rejected, restored, inherited)):
            continue  # The legacy validator below reports the structural error.
        if raw_batch.get("resolution") == "reused":
            # A v4 resume may safely reuse a strict v3 parent that allowed six
            # findings per batch; no new v4 response or quote repair occurred.
            if accepted_total > 6 or rejected != 0 or restored != 0:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 复用批次发现计数无效。")
            continue
        new_accepted = accepted_total - inherited
        if (
            new_accepted < 0
            or new_accepted > accepted_responses * 3
            or rejected > accepted_responses * 3
            or new_accepted + rejected > accepted_responses * 3
            or restored > accepted_responses * 12
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 响应发现数或引用修复数超出上限。")

    # Preserve the original v4 digest check above. Only after it succeeds is
    # the v4 envelope projected to the immutable v3 batch validator's shape.
    normalized_binding = {key: binding[key] for key in _V3_BINDING_FIELDS}
    normalized = {key: value[key] for key in _V3_CONTRACT_FIELDS}
    normalized["kind"] = _V3_EXECUTION_KIND
    normalized["binding"] = normalized_binding
    normalized["identity_sha256"] = hashlib.sha256(
        _canonical_report_bytes(normalized_binding)
    ).hexdigest()
    normalized_batches: list[dict[str, Any]] = []
    for raw_batch in normalized["batches"]:
        if not isinstance(raw_batch, Mapping):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 批次结构无效。")
        batch_copy = dict(raw_batch)
        if batch_copy.get("state") == "unattempted" and batch_copy.get("reason") not in _V4_UNATTEMPTED_REASONS:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 未尝试批次原因无效。")
        if batch_copy.get("reason") == "discovery_stage_budget_exhausted":
            batch_copy["reason"] = "provider_budget_exhausted"
        attempts = batch_copy.get("attempts")
        if isinstance(attempts, list):
            translated: list[dict[str, Any]] = []
            for raw_attempt in attempts:
                if not isinstance(raw_attempt, Mapping):
                    raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 尝试结构无效。")
                attempt = dict(raw_attempt)
                reason = attempt.get("reason")
                if reason in {"stage_budget_exhausted", "task_budget_exhausted"}:
                    if attempt.get("provider_admitted") is not False or attempt.get("status") != "rejected":
                        raise AnnualReportReadError(422, "annual_discovery_invalid", "预算拒绝不能有 provider 准入。")
                    attempt["reason"] = "ledger_budget_exhausted"
                translated.append(attempt)
            batch_copy["attempts"] = translated
        normalized_batches.append(batch_copy)
    normalized["batches"] = normalized_batches
    return _validate_v3_execution_contract(
        normalized,
        expected_run_id=expected_run_id,
        expected_company_id=expected_company_id,
        expected_report_year=expected_report_year,
        expected_document_id=expected_document_id,
        expected_sha256=expected_sha256,
        expected_analysis_mode=expected_analysis_mode,
        m3_screening=m3_screening,
        coverage=coverage,
    )


def _validate_v3_execution_contract(
    value: object,
    *,
    expected_run_id: str,
    expected_company_id: str,
    expected_report_year: int,
    expected_document_id: str,
    expected_sha256: str,
    expected_analysis_mode: object,
    m3_screening: object,
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _V3_CONTRACT_FIELDS:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 执行契约字段集合无效。")
    binding = value.get("binding")
    if not isinstance(binding, Mapping) or set(binding) != _V3_BINDING_FIELDS:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 执行绑定字段集合无效。")
    plan_sha256 = value.get("plan_sha256")
    identity_sha256 = value.get("identity_sha256")
    hash_fields = ("plan_sha256", "identity_sha256")
    if (
        value.get("kind") != _V3_EXECUTION_KIND
        or any(not isinstance(value.get(field), str) or _LOWER_SHA256.fullmatch(value[field]) is None for field in hash_fields)
        or binding.get("plan_sha256") != plan_sha256
        or binding.get("source_sha256") != expected_sha256
        or binding.get("company_id") != expected_company_id
        or binding.get("report_year") != expected_report_year
        or binding.get("source_document_id") != expected_document_id
        or binding.get("analysis_mode") not in {"model_investigation", "integrated_analysis"}
        or binding.get("analysis_mode") != expected_analysis_mode
        or (
            binding.get("analysis_mode") == "model_investigation"
            and binding.get("m3_binding_sha256") is not None
        )
        or (
            binding.get("analysis_mode") == "integrated_analysis"
            and (
                not isinstance(binding.get("m3_binding_sha256"), str)
                or _LOWER_SHA256.fullmatch(binding.get("m3_binding_sha256", "")) is None
            )
        )
        or not isinstance(binding.get("provider_model_fingerprint"), str)
        or _LOWER_SHA256.fullmatch(binding.get("provider_model_fingerprint", "")) is None
        or not isinstance(binding.get("prompts_sha256"), str)
        or _LOWER_SHA256.fullmatch(binding.get("prompts_sha256", "")) is None
        or not isinstance(binding.get("model"), str)
        or not binding["model"]
        or len(binding["model"]) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in binding["model"])
        or not isinstance(binding.get("parser_version"), str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", binding["parser_version"]) is None
        or hashlib.sha256(_canonical_report_bytes(binding)).hexdigest() != identity_sha256
        or (
            binding.get("analysis_mode") == "integrated_analysis"
            and binding.get("m3_binding_sha256") != _m3_semantic_binding_sha256(m3_screening, binding)
        )
    ):
        raise AnnualReportReadError(409, "annual_discovery_binding_mismatch", "年度 v3 执行身份与报告来源不一致。")

    parent_run_id = value.get("parent_run_id")
    if parent_run_id is not None:
        parent_run_id = _checked_run_id(parent_run_id)
        if parent_run_id == expected_run_id:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 执行契约不能引用自身为父运行。")
    counter_fields = (
        "provider_admission_count", "parent_provider_admission_count",
        "cumulative_provider_admission_count", "response_count",
    )
    counters: dict[str, int] = {}
    for field in counter_fields:
        counter = value.get(field)
        if type(counter) is not int or counter < 0 or counter > _MAX_V3_ATTEMPTS_TOTAL * 1000:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 执行调用计数无效。")
        counters[field] = counter
    if (
        counters["cumulative_provider_admission_count"]
        != counters["parent_provider_admission_count"] + counters["provider_admission_count"]
        or parent_run_id is None and counters["parent_provider_admission_count"] != 0
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 累计准入计数关系无效。")

    planned_batches = coverage.get("planned_batches")
    batches = value.get("batches")
    if (
        type(planned_batches) is not int
        or not 0 <= planned_batches <= 128
        or not isinstance(batches, list)
        or len(batches) != planned_batches
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次计划长度无效。")
    batch_map: dict[int, dict[str, Any]] = {}
    audit_ids: set[str] = set()
    admission_ids: set[str] = set()
    admitted_total = 0
    response_total = 0
    attempt_total = 0
    derived_reused: list[int] = []
    for position, batch in enumerate(batches, start=1):
        if not isinstance(batch, Mapping) or set(batch) != _V3_BATCH_FIELDS:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次字段集合无效。")
        index = batch.get("batch_index")
        state = batch.get("state")
        resolution = batch.get("resolution")
        reason = batch.get("reason")
        attempts = batch.get("attempts")
        if (
            type(index) is not int or index != position
            or state not in {"succeeded", "partial", "failed", "unattempted"}
            or resolution not in {"executed", "reused", "unattempted"}
            or not isinstance(attempts, list)
            or len(attempts) > _MAX_V3_ATTEMPTS_PER_BATCH
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次顺序、状态或重试数无效。")
        if state == "succeeded":
            valid_reason = reason is None
        elif state == "partial":
            valid_reason = reason == "some_findings_rejected"
        elif state == "failed":
            valid_reason = isinstance(reason, str) and reason in _V3_BATCH_FAILURES
        else:
            valid_reason = isinstance(reason, str) and reason in _V3_UNATTEMPTED_REASONS
        if not valid_reason:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次原因与状态无效。")
        if (
            resolution == "reused"
            and (state != "succeeded" or reason is not None or attempts)
            or resolution == "unattempted"
            and (state != "unattempted" or attempts)
            or resolution == "executed" and state in {"succeeded", "partial", "failed"} and not attempts
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次状态与解析方式不一致。")
        if resolution == "reused":
            derived_reused.append(index)

        admitted_in_batch = 0
        responses_in_batch = 0
        accepted_in_batch = 0
        previous_attempt = 0
        for attempt in attempts:
            if not isinstance(attempt, Mapping) or set(attempt) != _V3_ATTEMPT_FIELDS:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 尝试字段集合无效。")
            attempt_index = attempt.get("attempt_index")
            audit_id = attempt.get("audit_id")
            admission_id = attempt.get("admission_id")
            provider_admitted = attempt.get("provider_admitted")
            response_accepted = attempt.get("response_accepted")
            attempt_status = attempt.get("status")
            accepted_findings = attempt.get("accepted_findings")
            attempt_reason = attempt.get("reason")
            if (
                type(attempt_index) is not int or attempt_index != previous_attempt + 1
                or not _is_uuid4(audit_id)
                or audit_id in audit_ids
                or type(provider_admitted) is not bool
                or (admission_id is not None and not _is_uuid4(admission_id))
                or (provider_admitted != (admission_id is not None))
                or admission_id is not None and admission_id in admission_ids
                or type(response_accepted) is not bool
                or type(accepted_findings) is not int or not 0 <= accepted_findings <= 6
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 尝试标识、计数或准入绑定无效。")
            if response_accepted:
                if not provider_admitted or attempt_status != "succeeded" or attempt_reason is not None:
                    raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 可用响应状态无效。")
            elif (
                accepted_findings != 0
                or attempt_status == "failed" and (not provider_admitted or attempt_reason not in _V3_ATTEMPT_FAILURES)
                or attempt_status == "rejected" and (provider_admitted or attempt_reason not in _V3_ATTEMPT_FAILURES)
                or attempt_status not in {"failed", "rejected"}
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 失败尝试状态或原因无效。")
            if resolution == "unattempted" and provider_admitted:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "未尝试批次不能包含 provider 准入。")
            previous_attempt = attempt_index
            attempt_total += 1
            if attempt_total > _MAX_V3_ATTEMPTS_TOTAL:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 尝试总数超出上限。")
            audit_ids.add(str(audit_id))
            if admission_id is not None:
                admission_ids.add(str(admission_id))
            if provider_admitted:
                admitted_in_batch += 1
                admitted_total += 1
            if response_accepted:
                responses_in_batch += 1
                response_total += 1
            accepted_in_batch += accepted_findings
        if state == "unattempted" and resolution == "executed" and any(
            attempt["provider_admitted"] for attempt in attempts
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "未完成批次不能已有 provider 准入。")
        batch_map[index] = {
            "batch_index": index,
            "state": state,
            "resolution": resolution,
            "reason": reason,
            "attempts": list(attempts),
            "provider_admission_count": admitted_in_batch,
            "response_count": responses_in_batch,
            "attempt_accepted_findings": accepted_in_batch,
        }
    if (
        admitted_total != counters["provider_admission_count"]
        or response_total != counters["response_count"]
        or counters["response_count"] > counters["provider_admission_count"]
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 执行计数与批次尝试记录不一致。")
    reused = value.get("reused_batches")
    if (
        not isinstance(reused, list)
        or any(type(index) is not int or index < 1 or index > planned_batches for index in reused)
        or len(reused) != len(set(reused))
        or reused != sorted(reused)
        or reused != derived_reused
        or (parent_run_id is None and (reused or any(
            row.get("resolution") == "reused" for row in batch_map.values()
        )))
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 重用批次索引无效。")
    return {
        **counters,
        "parent_run_id": parent_run_id,
        "batches": batch_map,
    }


def _is_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _validate_discovery_coverage(
    coverage: Mapping[str, Any],
    status: str,
    prompt_version: str = "annual_discovery_v1",
    *,
    execution_contract: object = None,
) -> None:
    if execution_contract is not None:
        if isinstance(execution_contract, Mapping) and execution_contract.get("kind") == _V4_EXECUTION_KIND:
            scheduler = execution_contract.get("scheduler")
            dispatch_reason = scheduler.get("dispatch_closed_reason") if isinstance(scheduler, Mapping) else None
            time_reason = scheduler.get("time_deadline_reason") if isinstance(scheduler, Mapping) else None
            time_exhausted = coverage.get("time_budget_exhausted")
            if (
                type(time_exhausted) is not bool
                or time_exhausted != (time_reason is not None)
                or time_reason is not None and time_reason != dispatch_reason
                or dispatch_reason == "workflow_time_budget_exhausted"
                and time_reason != "workflow_time_budget_exhausted"
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 时间覆盖与停止原因不一致。")
            projected_contract = dict(execution_contract)
            projected_batches: list[dict[str, Any]] = []
            for batch in execution_contract.get("batches", []):
                if not isinstance(batch, Mapping):
                    raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 批次结构无效。")
                projected = dict(batch)
                if projected.get("reason") == "discovery_stage_budget_exhausted":
                    projected["reason"] = "provider_budget_exhausted"
                projected_batches.append(projected)
            projected_contract["batches"] = projected_batches
            projected_coverage = dict(coverage)
            projected_rows: list[dict[str, Any]] = []
            for row in coverage.get("batch_results", []):
                if not isinstance(row, Mapping):
                    raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 覆盖批次结构无效。")
                projected = dict(row)
                if projected.get("reason") == "discovery_stage_budget_exhausted":
                    projected["reason"] = "provider_budget_exhausted"
                projected_rows.append(projected)
            projected_coverage["batch_results"] = projected_rows
            _validate_discovery_coverage_v3(projected_coverage, status, projected_contract)
            unattempted = [
                row for row in execution_contract.get("batches", [])
                if isinstance(row, Mapping) and row.get("state") == "unattempted"
                and row.get("reason") != "awaiting_model"
            ]
            if unattempted and dispatch_reason is None:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 未尝试批次缺少调度关闭原因。")
            if dispatch_reason is not None and any(row.get("reason") != dispatch_reason for row in unattempted):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v4 未尝试批次与调度关闭原因不一致。")
        else:
            _validate_discovery_coverage_v3(coverage, status, execution_contract)
        return
    is_v2 = prompt_version in {"annual_discovery_v2", "annual_discovery_v3"}
    fields = {
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches",
        "processed_batches", "successful_batches", "failed_batches", "batch_results",
        "processed_chars", "attempted_chars", "unattempted_batches", "unattempted_chars",
        "planned_chars", "omitted_chars", "unread_pages", "failed_pages", "no_text_pages",
        "image_pages", "unread_image_pages", "truncated", "complete", "source_status",
        "content_status", "time_budget_exhausted", "text_only_scope",
    }
    if is_v2:
        fields.add("partial_batches")
    if set(coverage) != fields:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖字段集合无效。")
    integer_fields = (
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches",
        "processed_batches", "successful_batches", "failed_batches", "processed_chars",
        "attempted_chars", "unattempted_batches", "unattempted_chars", "planned_chars",
        "omitted_chars",
    )
    if is_v2:
        integer_fields = (*integer_fields[:-1], "partial_batches", integer_fields[-1])
    numbers: dict[str, int] = {}
    for field in integer_fields:
        value = coverage.get(field)
        if type(value) is not int or value < 0:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖计数无效。")
        numbers[field] = value
    for field in ("truncated", "complete", "time_budget_exhausted"):
        if type(coverage.get(field)) is not bool:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖状态无效。")
    source_status = coverage.get("source_status")
    content_status = coverage.get("content_status")
    if not isinstance(source_status, str) or source_status not in {"parsed", "unavailable"}:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现来源状态无效。")
    if not isinstance(content_status, str) or content_status not in {"text_available", "no_extractable_text", "unavailable"}:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现内容状态无效。")
    if not isinstance(coverage.get("text_only_scope"), str) or len(coverage["text_only_scope"]) > 200:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现范围说明无效。")

    page_list_fields = ("unread_pages", "failed_pages", "no_text_pages", "image_pages", "unread_image_pages")
    pages: dict[str, list[int]] = {}
    for field in page_list_fields:
        value = coverage.get(field)
        if (
            not isinstance(value, list)
            or len(value) > numbers["total_pages"]
            or any(type(page) is not int or page < 1 or page > numbers["total_pages"] for page in value)
            or len(set(value)) != len(value)
            or value != sorted(value)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖页码列表无效。")
        pages[field] = value
    if (
        numbers["text_pages"] > numbers["total_pages"]
        or numbers["text_pages"] + len(pages["no_text_pages"]) != numbers["total_pages"]
        or not set(pages["unread_image_pages"]).issubset(pages["image_pages"])
        or not set(pages["unread_image_pages"]).issubset(pages["no_text_pages"])
        or not set(pages["failed_pages"]).issubset(pages["unread_pages"])
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖页面关系无效。")

    if numbers["planned_batches"] > 128 or numbers["processed_batches"] > numbers["planned_batches"]:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现批次计数越界。")
    partial_batches = numbers.get("partial_batches", 0)
    if (
        numbers["successful_batches"] + partial_batches + numbers["failed_batches"] != numbers["processed_batches"]
        or numbers["unattempted_batches"] != numbers["planned_batches"] - numbers["processed_batches"]
        or numbers["unattempted_chars"] > numbers["planned_chars"]
        or numbers["attempted_chars"] > numbers["planned_chars"]
        or numbers["processed_chars"] > numbers["attempted_chars"]
        or numbers["processed_chars"] > numbers["planned_chars"]
        or numbers["planned_chars"] > numbers["total_chars"]
        or numbers["omitted_chars"] != max(numbers["total_chars"] - numbers["planned_chars"], 0) + numbers["unattempted_chars"]
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现批次与字符计数关系无效。")
    if content_status == "text_available" and numbers["total_chars"] <= 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "可提取文字状态与字符计数不一致。")
    if content_status == "no_extractable_text" and numbers["total_chars"] != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "无可提取文字状态与字符计数不一致。")
    if source_status == "unavailable":
        if (
            content_status != "unavailable"
            or any(numbers[field] != 0 for field in integer_fields)
            or any(pages[field] for field in page_list_fields)
            or coverage["complete"]
            or coverage["truncated"]
            or coverage["time_budget_exhausted"]
            or coverage.get("batch_results") != []
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "不可用来源不能包含虚构的批次覆盖。")
        if status not in {"failed", "not_called"}:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "不可用来源与发现状态不一致。")
    else:
        if content_status == "unavailable":
            raise AnnualReportReadError(422, "annual_discovery_invalid", "已解析来源不能使用不可用内容状态。")
        batch_results = coverage.get("batch_results")
        if not isinstance(batch_results, list) or len(batch_results) != numbers["planned_batches"]:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖批次结果数与计划不一致。")
        seen_indices: set[int] = set()
        result_statuses = ("succeeded", "partial", "failed", "unattempted") if is_v2 else ("succeeded", "failed", "unattempted")
        result_chars = {key: 0 for key in result_statuses}
        result_pages: dict[str, set[int]] = {key: set() for key in result_chars}
        for result in batch_results:
            result_fields = {"batch_index", "status", "reason", "pages", "raw_chars"}
            if is_v2:
                result_fields.update({"accepted_findings", "rejected_findings", "restored_quotes"})
            if not isinstance(result, Mapping) or set(result) != result_fields:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖批次结果字段无效。")
            index = result.get("batch_index")
            batch_status = result.get("status")
            raw_chars = result.get("raw_chars")
            batch_pages = result.get("pages")
            reason = result.get("reason")
            if (
                type(index) is not int or index < 1 or index > numbers["planned_batches"]
                or index in seen_indices
                or not isinstance(batch_status, str) or batch_status not in result_chars
                or type(raw_chars) is not int or raw_chars <= 0 or raw_chars > 12_000
                or not isinstance(batch_pages, list) or not batch_pages
                or any(type(page) is not int or page < 1 or page > numbers["total_pages"] for page in batch_pages)
                or len(set(batch_pages)) != len(batch_pages)
                or batch_pages != sorted(batch_pages)
                or (batch_status == "succeeded" and reason is not None)
                or (
                    batch_status != "succeeded"
                    and (
                        not isinstance(reason, str)
                        or re.fullmatch(r"[a-z][a-z0-9_]{0,99}", reason) is None
                    )
                )
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖批次结果值无效。")
            if is_v2:
                accepted = result.get("accepted_findings")
                rejected = result.get("rejected_findings")
                restored = result.get("restored_quotes")
                if (
                    type(accepted) is not int or not 0 <= accepted <= 6
                    or type(rejected) is not int or not 0 <= rejected <= 6
                    or accepted + rejected > 6
                    or type(restored) is not int or not 0 <= restored <= 24
                    or restored > accepted * 4
                    or batch_status == "succeeded" and (rejected != 0 or reason is not None)
                    or batch_status == "partial" and (accepted < 1 or rejected < 1 or reason != "some_findings_rejected")
                    or batch_status == "failed" and (
                        accepted != 0
                        or not isinstance(reason, str)
                        or reason not in _V2_SAFE_BATCH_FAILURES
                        or (reason == "all_findings_rejected") != (rejected > 0)
                    )
                    or batch_status == "unattempted" and (
                        accepted != 0 or rejected != 0 or restored != 0 or reason != "workflow_time_budget_exhausted"
                    )
                    or batch_status != "partial" and batch_status != "failed" and rejected != 0
                ):
                    raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖批次发现计数或安全原因无效。")
            seen_indices.add(index)
            result_chars[batch_status] += raw_chars
            result_pages[batch_status].update(batch_pages)
        if (
            seen_indices != set(range(1, numbers["planned_batches"] + 1))
            or sum(result_chars.values()) != numbers["planned_chars"]
            or result_chars["succeeded"] != numbers["processed_chars"]
            or result_chars["unattempted"] != numbers["unattempted_chars"]
            or len([r for r in batch_results if r["status"] == "succeeded"]) != numbers["successful_batches"]
            or len([r for r in batch_results if r["status"] == "failed"]) != numbers["failed_batches"]
            or len([r for r in batch_results if r["status"] == "unattempted"]) != numbers["unattempted_batches"]
            or not result_pages["failed"].issubset(pages["failed_pages"])
            or not result_pages["failed"].issubset(pages["unread_pages"])
            or not result_pages["unattempted"].issubset(pages["unread_pages"])
            or result_pages["failed"] != set(pages["failed_pages"])
            or numbers["attempted_chars"] != result_chars["succeeded"] + result_chars["failed"] + result_chars.get("partial", 0)
            or is_v2 and len([r for r in batch_results if r["status"] == "partial"]) != numbers["partial_batches"]
            or is_v2 and any(page not in pages["unread_pages"] for page in result_pages["partial"])
            or any(
                (index <= numbers["processed_batches"]) == (result["status"] == "unattempted")
                for index, result in ((item["batch_index"], item) for item in batch_results)
            )
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "批次结果与覆盖汇总不一致。")
        if coverage["truncated"] != bool(
            numbers["omitted_chars"] or numbers["unattempted_batches"] or numbers["unattempted_chars"]
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "截断标志与字符/图片页缺口不一致。")

    expected_complete = (
        source_status == "parsed"
        and content_status == "text_available"
        and numbers["total_chars"] > 0
        and not coverage["truncated"]
        and not pages["unread_image_pages"]
        and numbers["planned_chars"] == numbers["total_chars"]
        and numbers["processed_batches"] == numbers["planned_batches"]
        and partial_batches == 0
        and numbers["failed_batches"] == 0
        and numbers["successful_batches"] == numbers["planned_batches"]
        and numbers["processed_chars"] == numbers["planned_chars"]
        and not pages["unread_pages"]
    )
    if coverage["complete"] != expected_complete:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖完整标志与批次/字符/页面计数不一致。")
    if status == "completed" and not expected_complete:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "完成状态与全文覆盖记录不一致。")
    if status == "partial" and (
        expected_complete
        or numbers["successful_batches"] + (partial_batches if is_v2 else 0) == 0
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "部分完成状态缺少成功批次或覆盖缺口。")
    if status == "failed" and source_status == "parsed" and (
        numbers["failed_batches"] == 0
        or numbers["successful_batches"] != 0
        or is_v2 and partial_batches != 0
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "失败状态与已尝试批次结果不一致。")
    if status == "not_called" and source_status == "parsed" and numbers["processed_batches"] != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "未调用状态不能包含已结束的批次。")


def _validate_discovery_coverage_v3(
    coverage: Mapping[str, Any],
    status: str,
    execution_contract: object,
) -> None:
    """Validate R2 coverage without inheriting v1/v2 prefix/call assumptions."""

    if not isinstance(execution_contract, Mapping):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 执行契约无效。")
    fields = {
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches",
        "processed_batches", "successful_batches", "failed_batches", "batch_results",
        "processed_chars", "attempted_chars", "unattempted_batches", "unattempted_chars",
        "planned_chars", "omitted_chars", "unread_pages", "failed_pages", "no_text_pages",
        "image_pages", "unread_image_pages", "truncated", "complete", "source_status",
        "content_status", "time_budget_exhausted", "text_only_scope", "partial_batches",
    }
    if set(coverage) != fields:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 覆盖字段集合无效。")
    integer_fields = (
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches",
        "processed_batches", "successful_batches", "partial_batches", "failed_batches",
        "processed_chars", "attempted_chars", "unattempted_batches", "unattempted_chars",
        "planned_chars", "omitted_chars",
    )
    numbers: dict[str, int] = {}
    for field in integer_fields:
        count = coverage.get(field)
        if type(count) is not int or count < 0:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 覆盖计数无效。")
        numbers[field] = count
    if numbers["planned_batches"] > 128 or numbers["processed_batches"] > numbers["planned_batches"]:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次数超出上限。")
    for field in ("truncated", "complete", "time_budget_exhausted"):
        if type(coverage.get(field)) is not bool:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 覆盖状态无效。")
    source_status = coverage.get("source_status")
    content_status = coverage.get("content_status")
    if source_status not in {"parsed", "unavailable"} or content_status not in {
        "text_available", "no_extractable_text", "unavailable",
    }:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 来源或内容状态无效。")
    if not isinstance(coverage.get("text_only_scope"), str) or len(coverage["text_only_scope"]) > 200:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 文本范围说明无效。")
    page_list_fields = ("unread_pages", "failed_pages", "no_text_pages", "image_pages", "unread_image_pages")
    pages: dict[str, list[int]] = {}
    for field in page_list_fields:
        value = coverage.get(field)
        if (
            not isinstance(value, list)
            or len(value) > numbers["total_pages"]
            or any(type(page) is not int or page < 1 or page > numbers["total_pages"] for page in value)
            or len(set(value)) != len(value)
            or value != sorted(value)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 页码覆盖列表无效。")
        pages[field] = value
    if (
        numbers["text_pages"] > numbers["total_pages"]
        or numbers["text_pages"] + len(pages["no_text_pages"]) != numbers["total_pages"]
        or not set(pages["unread_image_pages"]).issubset(pages["image_pages"])
        or not set(pages["unread_image_pages"]).issubset(pages["no_text_pages"])
        or not set(pages["failed_pages"]).issubset(pages["unread_pages"])
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 页码覆盖关系无效。")

    contract_batches = execution_contract.get("batches")
    batch_results = coverage.get("batch_results")
    if (
        not isinstance(contract_batches, list)
        or not isinstance(batch_results, list)
        or len(contract_batches) != numbers["planned_batches"]
        or len(batch_results) != numbers["planned_batches"]
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次结果与计划长度不一致。")
    result_chars = {key: 0 for key in ("succeeded", "partial", "failed", "unattempted")}
    result_pages: dict[str, set[int]] = {key: set() for key in result_chars}
    attempted_chars = 0
    findings_by_state = {key: 0 for key in result_chars}
    successful_count = partial_count = failed_count = unattempted_count = 0
    for index, (contract_batch, result) in enumerate(zip(contract_batches, batch_results), start=1):
        if not isinstance(contract_batch, Mapping) or not isinstance(result, Mapping):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次结果结构无效。")
        if set(result) != _V3_COVERAGE_BATCH_FIELDS:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 覆盖批次字段集合无效。")
        batch_index = result.get("batch_index")
        batch_status = result.get("status")
        reason = result.get("reason")
        resolution = result.get("resolution")
        raw_chars = result.get("raw_chars")
        batch_pages = result.get("pages")
        accepted = result.get("accepted_findings")
        inherited = result.get("inherited_findings")
        rejected = result.get("rejected_findings")
        restored = result.get("restored_quotes")
        contract_attempts = contract_batch.get("attempts")
        batch_attempts = [item for item in contract_attempts if isinstance(item, Mapping)] if isinstance(contract_attempts, list) else []
        response_count = sum(item.get("response_accepted") is True for item in batch_attempts)
        admitted_count = sum(item.get("provider_admitted") is True for item in batch_attempts)
        accepted_from_attempts = sum(
            int(item.get("accepted_findings", 0))
            for item in batch_attempts
            if type(item.get("accepted_findings")) is int
        )
        if (
            batch_index != index
            or batch_status not in result_chars
            or result.get("batch_index") != contract_batch.get("batch_index")
            or batch_status != contract_batch.get("state")
            or resolution != contract_batch.get("resolution")
            or reason != contract_batch.get("reason")
            or type(raw_chars) is not int or not 0 < raw_chars <= 12_000
            or not isinstance(batch_pages, list) or not batch_pages
            or any(type(page) is not int or page < 1 or page > numbers["total_pages"] for page in batch_pages)
            or len(set(batch_pages)) != len(batch_pages)
            or batch_pages != sorted(batch_pages)
            or type(accepted) is not int or not 0 <= accepted <= _MAX_DISCOVERY_FINDINGS
            or type(inherited) is not int or not 0 <= inherited <= _MAX_DISCOVERY_FINDINGS
            or accepted < inherited
            or type(rejected) is not int or not 0 <= rejected <= response_count * 6
            or type(restored) is not int or not 0 <= restored <= response_count * 24
            or restored > accepted_from_attempts * 4
            or accepted > inherited + accepted_from_attempts
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次计数、顺序或契约绑定无效。")
        state = batch_status
        if (
            state == "succeeded" and (reason is not None or rejected != 0)
            or state == "partial" and (reason != "some_findings_rejected" or rejected == 0)
            or state == "failed" and (not isinstance(reason, str) or reason not in _V3_BATCH_FAILURES)
            or state == "failed" and reason == "all_findings_rejected" and rejected == 0
            or state == "unattempted" and (
                not isinstance(reason, str) or reason not in _V3_UNATTEMPTED_REASONS
                or accepted != inherited or rejected != 0 or restored != 0
            )
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次状态与安全原因或计数不一致。")
        if resolution == "reused" and (accepted != inherited or rejected != 0 or restored != 0):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "重用批次不能虚构本次模型结果。")
        if resolution == "unattempted" and (accepted != inherited or rejected != 0 or restored != 0):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "未计划批次不能包含本次发现结果。")
        if admitted_count:
            attempted_chars += raw_chars
        result_chars[state] += raw_chars
        result_pages[state].update(batch_pages)
        findings_by_state[state] += accepted
        if state == "succeeded":
            successful_count += 1
        elif state == "partial":
            partial_count += 1
        elif state == "failed":
            failed_count += 1
        else:
            unattempted_count += 1
    processed_batches = successful_count + partial_count + failed_count
    planned_chars = sum(result_chars.values())
    unattempted_chars = result_chars["unattempted"]
    processed_chars = result_chars["succeeded"]
    omitted_chars = max(numbers["total_chars"] - numbers["planned_chars"], 0) + unattempted_chars
    if (
        numbers["successful_batches"] != successful_count
        or numbers["partial_batches"] != partial_count
        or numbers["failed_batches"] != failed_count
        or numbers["processed_batches"] != processed_batches
        or numbers["unattempted_batches"] != unattempted_count
        or numbers["planned_chars"] != planned_chars
        or numbers["processed_chars"] != processed_chars
        or numbers["attempted_chars"] != attempted_chars
        or numbers["unattempted_chars"] != unattempted_chars
        or numbers["omitted_chars"] != omitted_chars
        or not result_pages["failed"].issubset(pages["failed_pages"])
        or not result_pages["failed"].issubset(pages["unread_pages"])
        or not result_pages["unattempted"].issubset(pages["unread_pages"])
        or result_pages["failed"] != set(pages["failed_pages"])
        or any(page not in pages["unread_pages"] for page in result_pages["partial"])
        or coverage["truncated"] != bool(omitted_chars)
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 批次覆盖汇总不一致。")

    if source_status == "unavailable":
        if (
            content_status != "unavailable"
            or any(numbers[field] != 0 for field in integer_fields)
            or any(pages[field] for field in page_list_fields)
            or coverage["complete"] or coverage["truncated"] or coverage["time_budget_exhausted"]
            or batch_results or status not in {"failed", "not_called"}
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "不可用来源不能包含年度 v3 批次覆盖。")
    else:
        if content_status == "unavailable":
            raise AnnualReportReadError(422, "annual_discovery_invalid", "已解析来源不能使用 unavailable 内容状态。")
        if content_status == "text_available" and numbers["total_chars"] <= 0:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "可提取文字状态与年度 v3 字符计数不一致。")
        if content_status == "no_extractable_text" and numbers["total_chars"] != 0:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "无可提取文字状态与年度 v3 字符计数不一致。")
    expected_complete = (
        source_status == "parsed"
        and content_status == "text_available"
        and numbers["total_chars"] > 0
        and not coverage["truncated"]
        and not pages["unread_image_pages"]
        and numbers["planned_chars"] == numbers["total_chars"]
        and numbers["processed_batches"] == numbers["planned_batches"]
        and numbers["partial_batches"] == 0
        and numbers["failed_batches"] == 0
        and numbers["successful_batches"] == numbers["planned_batches"]
        and numbers["processed_chars"] == numbers["planned_chars"]
        and not pages["unread_pages"]
    )
    if coverage["complete"] != expected_complete:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 覆盖完整标志与实际结果不一致。")
    if status == "completed" and not expected_complete:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 完成状态与覆盖缺口不一致。")
    if status == "partial" and (
        expected_complete or numbers["successful_batches"] + numbers["partial_batches"] == 0
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 部分状态缺少可用批次或覆盖缺口。")
    if status == "failed" and source_status == "parsed" and (
        numbers["failed_batches"] == 0
        or numbers["successful_batches"] != 0
        or numbers["partial_batches"] != 0
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 失败状态与批次结果不一致。")
    if status == "not_called" and source_status == "parsed" and numbers["processed_batches"] != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度 v3 未调用状态包含已处理批次。")


def _safe_audit_relative_path(value: str) -> bool:
    if len(value) > 500 or "\\" in value or value.startswith("/"):
        return False
    parts = value.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "artifacts"
        and parts[1] == "runs"
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _collect_narrative_evidences(
    report: Mapping[str, Any],
    source_pdf_bytes: bytes,
    *,
    expected_document_id: str,
    expected_sha256: str,
    verified_evidence_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    """Return only cited narrative blocks that still match their archived PDF."""

    investigation = report.get("model_investigation")
    if not isinstance(investigation, Mapping):
        return ()
    # Old reports never stored the block text needed for safe narrative preview.
    if "narrative_evidences" not in investigation:
        return ()
    records = investigation.get("narrative_evidences")
    items = investigation.get("items")
    if not isinstance(records, list) or len(records) > _MAX_NARRATIVE_EVIDENCES:
        raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据归档结构无效。")
    if not isinstance(items, list):
        raise AnnualReportReadError(422, "report_invalid", "模型调查的引用列表结构无效。")

    cited_by: dict[str, set[str]] = {}
    for item in items:
        if not isinstance(item, Mapping) or item.get("status") != "interpretation":
            continue
        signal_id = item.get("signal_id")
        evidence_ids = item.get("narrative_evidence_ids")
        if not isinstance(signal_id, str) or not isinstance(evidence_ids, list):
            continue
        for evidence_id in evidence_ids:
            if isinstance(evidence_id, str) and evidence_id.startswith("nctx-"):
                cited_by.setdefault(evidence_id, set()).add(signal_id)
    if not cited_by:
        return ()

    try:
        import pymupdf

        document = pymupdf.open(stream=source_pdf_bytes, filetype="pdf")
    except Exception:
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 无法复核叙述证据。") from None

    evidence_by_id: dict[str, dict[str, Any]] = {}
    total_chars = 0
    try:
        if document.needs_pass:
            raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 受密码保护，无法复核叙述证据。")
        for record in records:
            if not isinstance(record, Mapping):
                raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据记录无效。")
            evidence_id = record.get("evidence_id")
            if not isinstance(evidence_id, str) or evidence_id not in cited_by:
                continue
            if evidence_id in verified_evidence_ids:
                raise AnnualReportReadError(409, "evidence_id_conflict", "叙述证据 ID 与独立核验证据 ID 冲突。")
            if record.get("source_document_id") != expected_document_id or record.get("source_sha256") != expected_sha256:
                raise AnnualReportReadError(409, "narrative_evidence_source_mismatch", "叙述证据与归档原文来源不一致。")
            if not evidence_id.startswith("nctx-"):
                raise AnnualReportReadError(422, "report_invalid", "叙述证据 ID 不属于叙述证据命名空间。")
            page_number = record.get("pdf_page")
            raw_text = record.get("raw_text")
            selection_reason = record.get("selection_reason")
            section_label = record.get("section_label")
            bbox = record.get("bbox")
            signal_ids = record.get("signal_ids")
            if (
                type(page_number) is not int
                or page_number < 1
                or page_number > document.page_count
                or not isinstance(raw_text, str)
                or not raw_text.strip()
                or len(raw_text) > _MAX_NARRATIVE_SNIPPET_CHARS
                or not isinstance(selection_reason, str)
                or not selection_reason.strip()
                or len(selection_reason) > 500
                or not isinstance(section_label, str)
                or section_label not in {"mda", "financial_notes"}
                or not isinstance(bbox, Mapping)
                or not isinstance(signal_ids, list)
                or any(not isinstance(value, str) for value in signal_ids)
            ):
                raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据字段无效。")
            coordinates: list[float] = []
            for key in ("x0", "y0", "x1", "y1"):
                value = bbox.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据坐标无效。")
                coordinates.append(float(value))
            x0, y0, x1, y1 = coordinates
            if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
                raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据坐标超出页面范围。")
            page = document[page_number - 1]
            source_block_matches = False
            citation_block_index: int | None = None
            source_blocks = page.get_text("blocks", sort=True)
            for block_index, block in enumerate(source_blocks):
                if len(block) < 5 or str(block[4]).strip() != raw_text.strip():
                    continue
                if all(abs(float(actual) - expected) <= 0.1 for actual, expected in zip(block[:4], coordinates)):
                    source_block_matches = True
                    citation_block_index = block_index
                    break
            if not source_block_matches:
                raise AnnualReportReadError(409, "narrative_evidence_source_mismatch", "叙述文本或坐标与归档 PDF 不一致。")
            assert citation_block_index is not None
            total_chars += len(raw_text)
            if total_chars > _MAX_NARRATIVE_EVIDENCE_CHARS:
                raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据超过文本上限。")
            reading_context = build_reading_context(
                source_blocks,
                citation_block_index,
                pdf_page=page_number,
                page_width=float(page.mediabox.width),
                source_sha256=expected_sha256,
            )
            normalized = {
                "evidence_id": evidence_id,
                "source_document_id": expected_document_id,
                "source_sha256": expected_sha256,
                "signal_ids": list(dict.fromkeys(signal_ids)),
                "section_label": section_label,
                "pdf_page": page_number,
                "bbox": {key: coordinates[index] for index, key in enumerate(("x0", "y0", "x1", "y1"))},
                "raw_text": raw_text,
                "selection_reason": selection_reason,
                "cited_by_signal_ids": sorted(cited_by[evidence_id]),
                # This is rebuilt from the hash-checked PDF on every read. Any
                # similarly named field inside report.json is ignored.
                "reading_context": reading_context,
            }
            existing = evidence_by_id.get(evidence_id)
            if existing is not None and existing != normalized:
                raise AnnualReportReadError(409, "evidence_id_conflict", "报告中存在内容不一致的重复叙述证据 ID。")
            evidence_by_id[evidence_id] = normalized
    finally:
        document.close()

    return tuple(evidence_by_id[key] for key in sorted(evidence_by_id))
