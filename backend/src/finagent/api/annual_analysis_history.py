"""Read-only, bounded index of local annual-analysis jobs and run archives."""

from __future__ import annotations

import json
import ntpath
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from finagent.api.annual_report_read import AnnualReportReadError, SourceReadContext, load_annual_report

_COMPANY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,64}$")
_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JOB_ID = re.compile(r"^annual-job-[0-9a-f]{32}$")
_RUN_ID = re.compile(r"^annual-analysis-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_REPARSE_POINT_ATTRIBUTE = 0x400
_MAX_LIMIT = 50
_DEFAULT_LIMIT = 20
_MAX_JOB_BYTES = 512 * 1024
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_REPORT_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_PDF_BYTES = 256 * 1024 * 1024
_JOB_STATUSES = {
    "queued",
    "running",
    "completed",
    "completed_with_issues",
    "failed",
    "interrupted",
    "cancelled",
}
_RUN_STATUSES = {"running", "completed", "completed_with_issues", "failed", "interrupted"}
_MODES = {"deterministic", "m3_screening", "model_investigation", "integrated_analysis"}


class AnnualAnalysisHistoryError(Exception):
    """A safe, client-facing error for the local history index."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def list_annual_analysis_history(
    project_root: Path,
    *,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Index persisted jobs plus successful standalone CLI archives.

    The index reads bounded job, manifest, and report records. For a failed job
    whose report appears available, it additionally invokes the strict report
    reader, which reads the archived source PDF. It never opens source.json,
    environment files, or model-audit JSON.
    """

    _validate_pagination(limit, offset)
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AnnualAnalysisHistoryError(409, "history_root_invalid", "本机历史目录无法安全访问。") from None

    jobs_root = _optional_directory(root, ("artifacts", "annual-analysis-jobs"))
    runs_root = _optional_directory(root, ("artifacts", "runs"))

    jobs: list[dict[str, Any]] = []
    if jobs_root is not None:
        for entry in _safe_directory_entries(jobs_root):
            job_id = entry.name
            if _JOB_ID.fullmatch(job_id) is None:
                continue
            try:
                _safe_path(root, ("artifacts", "annual-analysis-jobs", job_id), directory=True)
                payload = _read_json_object(
                    root,
                    ("artifacts", "annual-analysis-jobs", job_id, "job.json"),
                    _MAX_JOB_BYTES,
                )
                item = _job_item(job_id, payload)
            except (OSError, ValueError, RecursionError, AnnualAnalysisHistoryError):
                # One corrupt or incomplete job must not make the whole index
                # unavailable or hide other valid rows.
                continue
            if item is None:
                continue
            jobs.append(item)

    run_records: dict[str, dict[str, Any]] = {}
    if runs_root is not None:
        for entry in _safe_directory_entries(runs_root):
            run_id = entry.name
            if _RUN_ID.fullmatch(run_id) is None:
                continue
            try:
                _safe_path(root, ("artifacts", "runs", run_id), directory=True)
                manifest = _read_json_object(
                    root,
                    ("artifacts", "runs", run_id, "manifest.json"),
                    _MAX_MANIFEST_BYTES,
                )
                record = _run_record(root, run_id, manifest)
            except (OSError, ValueError, RecursionError, AnnualAnalysisHistoryError):
                continue
            if record is not None:
                run_records[run_id] = record

    rows: list[dict[str, Any]] = []
    claimed_run_ids: set[str] = set()
    for item in jobs:
        run_id = item["run_id"]
        record = run_records.get(run_id) if run_id is not None else None
        if record is not None:
            if _same_source(item, record):
                claimed_run_ids.add(run_id)
                _attach_run(item, record)
            else:
                _add_issue(item, "run_archive_identity_mismatch", "关联的运行归档与任务来源不一致。")
        elif run_id is not None:
            _add_issue(item, "run_archive_unavailable", "运行归档缺失或无法安全读取。")
        _add_job_status_issue(item)
        rows.append(item)

    for run_id, record in run_records.items():
        if run_id in claimed_run_ids:
            continue
        if record["run_status"] not in {"completed", "completed_with_issues"} and not _has_checkpoint_pointer(root, run_id):
            continue
        rows.append(_standalone_run_item(record))

    rows.sort(key=_sort_key)
    total = len(rows)
    page = rows[offset : offset + limit]
    result_cache: dict[str, tuple[bool, str | None, dict[str, Any] | None]] = {}
    source_context = SourceReadContext()
    for item in page:
        record = item.pop("_run_record", None)
        if isinstance(record, Mapping):
            run_id = str(record["run_id"])
            if run_id not in result_cache:
                if _has_checkpoint_pointer(root, run_id):
                    result_cache[run_id] = _strict_archive_projection(
                        root,
                        run_id,
                        company_code=str(record["company_code"]),
                        year=int(record["year"]),
                        document_id=str(record["source_document_id"]),
                        source_sha256=str(record["source_sha256"]),
                        source_context=source_context,
                    )
                else:
                    # Keep the pre-R2 read-only projection for legacy finals.
                    # Checkpoint-backed partials and sealed v3 finals always
                    # take the strict reader path above.
                    available = _result_archive_available(
                        root,
                        run_id,
                        record["_result_manifest"],
                        str(record["source_sha256"]),
                    )
                    category = None
                    if available and item["job_status"] in {"failed", "interrupted"}:
                        available, category = _strict_failed_result_available(root, run_id, item, source_context)
                    result_cache[run_id] = (available, category, None)
            available, category, snapshot = result_cache[run_id]
            item["result_available"] = available
            item["partial_checkpoint_available"] = bool(snapshot and snapshot.get("is_partial") is True)
            item["snapshot_state"] = snapshot.get("state") if snapshot else None
            item["snapshot_sequence"] = snapshot.get("sequence") if snapshot else None
            item["is_partial"] = snapshot.get("is_partial") if snapshot else None
            if (
                item["job_status"] in {"failed", "interrupted"}
            ):
                if category is not None:
                    item["result_available"] = False
                    _add_issue(
                        item,
                        f"failed_result_{category}",
                        "原任务失败；关联报告未通过当前来源或归档校验，暂不可只读查看。",
                    )
                elif item["result_available"]:
                    _add_issue(
                        item,
                        "failed_job_archive_readable",
                        "原任务仍为失败状态；关联报告已通过当前严格校验，可只读查看。",
                    )
            if (
                item["run_status"] in {"completed", "completed_with_issues"}
                and not item["result_available"]
            ):
                _add_issue(item, "result_archive_unavailable", "报告归档缺失、损坏或无法安全读取。")
        item.pop("_sort_created", None)
        item.pop("_source_sha256", None)
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _strict_failed_result_available(
    root: Path,
    run_id: str,
    item: Mapping[str, Any],
    source_context: SourceReadContext,
) -> tuple[bool, str | None]:
    try:
        archive = load_annual_report(root, run_id, _source_context=source_context)
    except AnnualReportReadError as exc:
        return False, _report_read_error_category(exc.code)
    summary = archive.manifest_summary
    matches = (
        summary.get("company_id") == item.get("company_code")
        and summary.get("report_year") == item.get("year")
        and summary.get("document_id") == item.get("source_document_id")
        and str(summary.get("source_pdf_sha256", "")).lower()
        == str(item.get("_source_sha256", "")).lower()
    )
    return matches, None if matches else "identity_mismatch"


def _strict_archive_projection(
    root: Path,
    run_id: str,
    *,
    company_code: str,
    year: int,
    document_id: str,
    source_sha256: str,
    source_context: SourceReadContext,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Project only strict-reader identity and checkpoint metadata into history."""

    try:
        archive = load_annual_report(root, run_id, _source_context=source_context)
    except AnnualReportReadError as exc:
        return False, _report_read_error_category(exc.code), None
    summary = archive.manifest_summary
    if (
        summary.get("company_id") != company_code
        or summary.get("report_year") != year
        or summary.get("document_id") != document_id
        or str(summary.get("source_pdf_sha256", "")).lower() != source_sha256.lower()
    ):
        return False, "identity_mismatch", None
    return True, None, dict(archive.snapshot) if archive.snapshot is not None else None


def _has_checkpoint_pointer(root: Path, run_id: str) -> bool:
    """Use only the fixed regular pointer name to include standalone partial runs."""

    path = root / "artifacts" / "runs" / run_id / "checkpoint-current.json"
    try:
        info = path.lstat()
    except OSError:
        return False
    return not _is_reparse_point(info) and stat.S_ISREG(info.st_mode) and info.st_size <= 64 * 1024


def _report_read_error_category(code: str) -> str:
    source_codes = {
        "source_sha256_mismatch",
        "source_pdf_invalid",
        "archive_identity_mismatch",
        "report_identity_mismatch",
        "evidence_source_mismatch",
        "annual_discovery_evidence_mismatch",
        "annual_discovery_quote_mismatch",
        "narrative_evidence_source_mismatch",
    }
    return "source_validation_failed" if code in source_codes else "archive_incompatible"


def _validate_pagination(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
        raise AnnualAnalysisHistoryError(400, "invalid_limit", "limit 必须是 1 到 50 之间的整数。")
    if type(offset) is not int or offset < 0:
        raise AnnualAnalysisHistoryError(400, "invalid_offset", "offset 必须是非负整数。")


def _optional_directory(root: Path, parts: tuple[str, ...]) -> Path | None:
    current = root
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史目录无法安全访问。") from None
        if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史目录包含链接或越界路径。")
        try:
            resolved = current.resolve(strict=True)
        except (OSError, RuntimeError):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史目录无法安全访问。") from None
        if not resolved.is_relative_to(root):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史目录包含链接或越界路径。")
    return _safe_path(root, parts, directory=True)


def _safe_directory_entries(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return sorted(entries, key=lambda item: item.name)
    except OSError:
        return []


def _safe_path(root: Path, parts: tuple[str, ...], *, directory: bool) -> Path:
    anchor = root.resolve(strict=True)
    current = anchor
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史归档路径无法安全访问。") from None
        if _is_reparse_point(info):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史归档包含链接或越界路径。")
        try:
            resolved = current.resolve(strict=True)
        except (OSError, RuntimeError):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史归档路径无法安全访问。") from None
        if resolved == anchor or not resolved.is_relative_to(anchor):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史归档包含链接或越界路径。")
        final = index == len(parts) - 1
        if not final and not stat.S_ISDIR(info.st_mode):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史归档目录结构无效。")
        if final and directory and not stat.S_ISDIR(info.st_mode):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史归档目录结构无效。")
        if final and not directory and not stat.S_ISREG(info.st_mode):
            raise AnnualAnalysisHistoryError(409, "history_path_invalid", "本机历史归档文件类型无效。")
    return current


def _read_json_object(root: Path, parts: tuple[str, ...], max_bytes: int) -> dict[str, Any]:
    path = _safe_path(root, parts, directory=False)
    try:
        before = path.lstat()
        if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise AnnualAnalysisHistoryError(409, "history_file_invalid", "本机历史 JSON 文件类型或大小无效。")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                _is_reparse_point(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _file_identity(before) != _file_identity(opened)
            ):
                raise AnnualAnalysisHistoryError(409, "history_file_changed", "本机历史文件在读取期间发生变化。")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                content = handle.read(max_bytes + 1)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if (
            _is_reparse_point(after)
            or _file_identity(before) != _file_identity(after)
            or len(content) > max_bytes
        ):
            raise AnnualAnalysisHistoryError(409, "history_file_changed", "本机历史文件在读取期间发生变化。")
        _safe_path(root, parts, directory=False)
        payload = json.loads(content.decode("utf-8"))
    except AnnualAnalysisHistoryError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise AnnualAnalysisHistoryError(409, "history_json_invalid", "本机历史 JSON 格式无效。") from None
    if not isinstance(payload, dict):
        raise AnnualAnalysisHistoryError(409, "history_json_invalid", "本机历史 JSON 顶层必须是对象。")
    return payload


def _job_item(job_id: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if payload.get("job_id") != job_id:
        return None
    status = payload.get("status")
    request = payload.get("request")
    if not isinstance(status, str) or status not in _JOB_STATUSES or not isinstance(request, Mapping):
        return None
    company_code = _safe_company_code(request.get("company_id"))
    year = _safe_year(request.get("report_year"))
    document_id = _safe_document_id(request.get("document_id"))
    source_sha256 = request.get("sha256")
    if (
        company_code is None
        or year is None
        or document_id is None
        or not isinstance(source_sha256, str)
        or _SHA256.fullmatch(source_sha256) is None
    ):
        return None
    run_id_raw = payload.get("run_id")
    run_id = run_id_raw if isinstance(run_id_raw, str) and _RUN_ID.fullmatch(run_id_raw) else None
    issues: list[dict[str, str]] = []
    if run_id_raw is not None and run_id is None:
        issues.append(_issue("run_id_invalid", "任务中的运行标识无效。"))
    created_at, created_key = _created_at(payload.get("created_at"))
    source_path = request.get("source_pdf_path")
    source_name = _safe_source_name(source_path) if isinstance(source_path, str) else None
    request_mode = request.get("mode")
    return {
        "item_id": job_id,
        "company_code": company_code,
        "year": year,
        "report_type": "annual",
        "source_document_id": document_id,
        "source_id": document_id,
        "source_name": source_name,
        "status": status,
        "job_status": status,
        "run_status": None,
        "mode": request_mode if isinstance(request_mode, str) and request_mode in _MODES else "unknown",
        "run_mode": "unknown",
        "replay": True,
        "created_at": created_at,
        "job_id": job_id,
        "run_id": run_id,
        "result_available": False,
        "partial_checkpoint_available": False,
        "snapshot_state": None,
        "snapshot_sequence": None,
        "is_partial": None,
        "issues": issues,
        "_sort_created": created_key,
        "_source_sha256": source_sha256.lower(),
    }


def _run_record(root: Path, run_id: str, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    status = manifest.get("status")
    company_code = _safe_company_code(manifest.get("company_id"))
    year = _safe_year(manifest.get("report_year"))
    document_id = _safe_document_id(manifest.get("document_id"))
    inputs = manifest.get("inputs")
    outputs = manifest.get("outputs")
    if (
        manifest.get("run_id") != run_id
        or not isinstance(status, str)
        or status not in _RUN_STATUSES
        or company_code is None
        or year is None
        or document_id is None
        or not isinstance(inputs, Mapping)
        or not isinstance(outputs, Mapping)
    ):
        return None
    expected_run_dir = f"runs/{run_id}"
    expected_report_dir = f"reports/{run_id}"
    expected_source = f"{expected_run_dir}/source.pdf"
    expected_report = f"{expected_report_dir}/report.json"
    if (
        inputs.get("source_pdf_archive_path") != expected_source
        or outputs.get("run_dir") != expected_run_dir
        or outputs.get("report_dir") != expected_report_dir
        or outputs.get("report_json") != expected_report
    ):
        return None
    source_sha256 = inputs.get("source_pdf_sha256")
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        return None
    created_at, created_key = _created_at(manifest.get("created_at"))
    result_manifest = {
        "status": status,
        "company_id": company_code,
        "report_year": year,
        "document_id": document_id,
    }
    result_manifest["inputs"] = {
        "source_pdf_archive_path": expected_source,
        "source_pdf_sha256": source_sha256.lower(),
    }
    result_manifest["outputs"] = {
        "run_dir": expected_run_dir,
        "report_dir": expected_report_dir,
        "report_json": expected_report,
    }
    return {
        "run_id": run_id,
        "run_status": status,
        "company_code": company_code,
        "year": year,
        "source_document_id": document_id,
        "source_name": _safe_source_name(inputs.get("source_pdf_original_filename")),
        "mode": _infer_mode(manifest),
        "source_sha256": source_sha256.lower(),
        "created_at": created_at,
        "_sort_created": created_key,
        "result_available": False,
        "_result_manifest": result_manifest,
    }


def _result_archive_available(
    root: Path,
    run_id: str,
    manifest: Mapping[str, Any],
    source_sha256: str,
) -> bool:
    if manifest.get("status") not in {"completed", "completed_with_issues"}:
        return False
    try:
        source_path = _safe_path(root, ("artifacts", "runs", run_id, "source.pdf"), directory=False)
        source_info = source_path.lstat()
        if (
            _is_reparse_point(source_info)
            or not stat.S_ISREG(source_info.st_mode)
            or source_info.st_size > _MAX_SOURCE_PDF_BYTES
        ):
            return False
        report = _read_json_object(
            root,
            ("artifacts", "reports", run_id, "report.json"),
            _MAX_REPORT_BYTES,
        )
    except (OSError, ValueError, RecursionError, AnnualAnalysisHistoryError):
        return False
    return (
        report.get("kind") == "fintrace_annual_analysis_report"
        and report.get("run_id") == run_id
        and report.get("company_id") == manifest.get("company_id")
        and report.get("report_year") == manifest.get("report_year")
        and report.get("source_document_id") == manifest.get("document_id")
        and isinstance(report.get("source_sha256"), str)
        and report.get("source_sha256", "").lower() == source_sha256.lower()
    )


def _same_source(job: Mapping[str, Any], run: Mapping[str, Any]) -> bool:
    return (
        job["company_code"] == run["company_code"]
        and job["year"] == run["year"]
        and job["source_document_id"] == run["source_document_id"]
        and job["_source_sha256"] == run["source_sha256"]
    )


def _attach_run(item: dict[str, Any], record: Mapping[str, Any]) -> None:
    item["run_status"] = record["run_status"]
    item["run_mode"] = record["mode"]
    item["_run_record"] = record
    if item["mode"] != "unknown" and record["mode"] != "unknown" and item["mode"] != record["mode"]:
        _add_issue(item, "mode_mismatch", "任务请求模式与运行归档标记不一致。")
    if record.get("source_name") is not None:
        item["source_name"] = record["source_name"]
    if item["created_at"] is None and record["created_at"] is not None:
        item["created_at"] = record["created_at"]
        item["_sort_created"] = record["_sort_created"]
    if record["run_status"] == "failed":
        _add_issue(item, "run_failed", "年度分析运行失败。")


def _standalone_run_item(record: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(record["run_id"])
    issues: list[dict[str, str]] = []
    item = {
        "item_id": run_id,
        "company_code": record["company_code"],
        "year": record["year"],
        "report_type": "annual",
        "source_document_id": record["source_document_id"],
        "source_id": record["source_document_id"],
        "source_name": record["source_name"],
        "status": record["run_status"],
        "job_status": None,
        "run_status": record["run_status"],
        "mode": record["mode"],
        "run_mode": record["mode"],
        "replay": True,
        "created_at": record["created_at"],
        "job_id": None,
        "run_id": run_id,
        "result_available": False,
        "partial_checkpoint_available": False,
        "snapshot_state": None,
        "snapshot_sequence": None,
        "is_partial": None,
        "_run_record": record,
        "issues": issues,
        "_sort_created": record["_sort_created"],
    }
    return item


def _add_job_status_issue(item: dict[str, Any]) -> None:
    if item["job_status"] == "failed":
        _add_issue(item, "job_failed", "年度分析任务失败。")
    elif item["job_status"] == "interrupted":
        _add_issue(item, "job_interrupted", "服务中断前任务未完成，可重新提交。")
    elif item["job_status"] == "cancelled":
        _add_issue(item, "job_cancelled", "年度分析任务已取消；可查看取消前已保存的部分报告。")


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _add_issue(item: dict[str, Any], code: str, message: str) -> None:
    if not any(issue["code"] == code for issue in item["issues"]):
        item["issues"].append(_issue(code, message))


def _infer_mode(manifest: Mapping[str, Any]) -> str:
    direct_mode = manifest.get("mode")
    m3 = manifest.get("m3_screening")
    rules = manifest.get("rules")
    has_m3 = (isinstance(m3, Mapping) and m3.get("requested") is True) or (
        isinstance(rules, Mapping) and isinstance(rules.get("m3_screening_version"), str)
    )
    investigation = manifest.get("model_investigation")
    has_model = (
        manifest.get("model_called") is True
        or isinstance(investigation, Mapping)
        and (
            investigation.get("requested") is True
            or isinstance(manifest.get("model_investigation_status"), str)
        )
        or isinstance(manifest.get("model_investigation_status"), str)
    )
    inferred = (
        "integrated_analysis"
        if has_m3 and has_model
        else "m3_screening"
        if has_m3
        else "model_investigation"
        if has_model
        else "deterministic"
    )
    if not isinstance(direct_mode, str) or direct_mode not in _MODES:
        return inferred if inferred != "deterministic" else "unknown"
    if direct_mode == "integrated_analysis":
        return "integrated_analysis" if has_m3 and has_model else "unknown"
    expected_markers = {
        "deterministic": not has_m3 and not has_model,
        "m3_screening": has_m3 and not has_model,
        "model_investigation": has_model and not has_m3,
    }
    return direct_mode if expected_markers.get(direct_mode, False) else "unknown"


def _safe_company_code(value: Any) -> str | None:
    if not isinstance(value, str) or _COMPANY_ID.fullmatch(value) is None or ".." in value:
        return None
    return value


def _safe_document_id(value: Any) -> str | None:
    if not isinstance(value, str) or _DOCUMENT_ID.fullmatch(value) is None or ".." in value:
        return None
    return value


def _safe_year(value: Any) -> int | None:
    return value if type(value) is int and 1900 <= value <= 2100 else None


def _safe_source_name(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 1024:
        return None
    name = ntpath.basename(value.replace("/", "\\")).strip()
    name = "".join(character for character in name if character >= " " and character != "\x7f")[:255]
    if not name or name in {".", ".."}:
        return None
    return name


def _created_at(value: Any) -> tuple[str | None, float | None]:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        key = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None, None
    return value, key


def _sort_key(item: Mapping[str, Any]) -> tuple[int, float, str]:
    created_key = item.get("_sort_created")
    if isinstance(created_key, (int, float)):
        return (0, -float(created_key), str(item["item_id"]))
    return (1, 0.0, str(item["item_id"]))


def _is_reparse_point(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
