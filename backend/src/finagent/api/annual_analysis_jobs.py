"""持久化并运行 FINTRACE v2 年度分析任务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from finagent.api.annual_report_read import AnnualReportReadError, load_annual_report
from finagent.core.model_settings import ModelConfigError
from finagent.core.model_settings_store import ModelSettingsStoreError, resolve_model_settings

_COMPANY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,64}$")
_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_JOB_ID = re.compile(r"^annual-job-[0-9a-f]{32}$")
_RUN_ID = re.compile(r"\brun_id=(annual-analysis-[0-9a-f-]{36})\b")
_REPARSE_POINT_ATTRIBUTE = 0x400
_MAX_SOURCE_PDF_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_RECORD_BYTES = 1024 * 1024
_MAX_WORKERS = 1
_MAX_QUEUED_JOBS = 3
_DEFAULT_CLI_TIMEOUT_SECONDS = 900
_MIN_CLI_TIMEOUT_SECONDS = 60
_MAX_CLI_TIMEOUT_SECONDS = 900
_SERVICE_REGISTRY_LOCK = threading.Lock()
_SERVICE_REGISTRY: dict[str, "AnnualAnalysisJobService"] = {}


class AnnualAnalysisJobError(Exception):
    """可安全映射到 HTTP 响应的任务创建或读取错误。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class AnnualAnalysisJobService:
    """单进程内的有界队列；状态存入 artifacts/annual-analysis-jobs。"""

    def __init__(
        self,
        project_root: Path,
        *,
        max_workers: int = _MAX_WORKERS,
        max_queued_jobs: int = _MAX_QUEUED_JOBS,
    ) -> None:
        if max_workers < 1 or max_queued_jobs < 0:
            raise ValueError("任务并发和队列上限无效。")
        self.project_root = Path(project_root).resolve()
        self._max_slots = max_workers + max_queued_jobs
        self._slots = threading.BoundedSemaphore(self._max_slots)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="fintrace-annual-analysis",
        )
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._recover_interrupted_jobs()

    @property
    def jobs_root(self) -> Path:
        return self.project_root / "artifacts" / "annual-analysis-jobs"

    def create(self, request: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_job_request(self.project_root, request)
        if normalized["mode"] == "model_investigation":
            capability = get_annual_analysis_capabilities(self.project_root)
            if not capability["model_investigation"]["available"]:
                dependency_missing = capability["model_investigation"]["status"] == "dependency_unavailable"
                raise AnnualAnalysisJobError(
                    503,
                    "model_dependency_unavailable" if dependency_missing else "model_configuration_unavailable",
                    "模型调查所需组件暂不可用。" if dependency_missing else "模型调查配置尚未就绪，请检查服务端模型配置后重试。",
                )
        if not self._slots.acquire(blocking=False):
            raise AnnualAnalysisJobError(
                429,
                "job_capacity_reached",
                "年度分析任务队列已满，请稍后重试。",
            )

        submitted = False
        job_dir: Path | None = None
        job_id: str | None = None
        try:
            with self._lock:
                _ensure_artifact_directories(self.project_root, include_job_root=True)
                job_id = f"annual-job-{uuid4().hex}"
                job_dir = self.jobs_root / job_id
                try:
                    job_dir.mkdir(exist_ok=False)
                except FileExistsError:
                    raise AnnualAnalysisJobError(
                        503,
                        "job_id_collision",
                        "未能分配唯一任务标识，请重试。",
                    ) from None
                now = _utc_now()
                state: dict[str, Any] = {
                    "job_id": job_id,
                    "status": "queued",
                    "stage": "queued",
                    "created_at": now,
                    "updated_at": now,
                    "request": normalized,
                    "run_id": None,
                    "result_url": None,
                    "error": None,
                }
                self._states[job_id] = state
                try:
                    _write_state(job_dir / "job.json", state, create=True)
                except Exception:
                    self._states.pop(job_id, None)
                    try:
                        (job_dir / "job.json").unlink()
                    except OSError:
                        pass
                    try:
                        job_dir.rmdir()
                    except OSError:
                        pass
                    raise
                try:
                    self._executor.submit(self._execute, job_id)
                    submitted = True
                except RuntimeError:
                    state.update(
                        status="failed",
                        stage="failed",
                        updated_at=_utc_now(),
                        error={
                            "code": "job_executor_unavailable",
                            "message": "年度分析服务当前无法启动后台任务，请稍后重试。",
                        },
                    )
                    _write_state(job_dir / "job.json", state, create=False)
                    self._slots.release()
                    submitted = True
                return _public_state(state)
        except Exception:
            if not submitted:
                self._slots.release()
            raise

    def get(self, job_id: str) -> dict[str, Any]:
        checked = _validate_job_id(job_id)
        with self._lock:
            job_dir = _safe_existing_job_dir(self.jobs_root, checked)
            state_path = _safe_regular_file(job_dir / "job.json", self.project_root)
            state = _read_json_object(state_path, _MAX_SOURCE_RECORD_BYTES)
            if state.get("job_id") != checked:
                raise AnnualAnalysisJobError(404, "job_not_found", "找不到该年度分析任务。")
            self._states[checked] = state
            return _public_state(state)

    def _execute(self, job_id: str) -> None:
        try:
            state = self._load_state(job_id)
            self._update(job_id, status="running", stage="validating_source", error=None)
            normalized = validate_job_request(self.project_root, state["request"])
            self._update(job_id, stage="annual_analysis_cli")
            artifacts_root = self.project_root / "artifacts"
            result = _invoke_cli(self.project_root, artifacts_root, normalized)
            run_id = _run_id_from_output(result.stdout + "\n" + result.stderr)
            if run_id is None:
                self._finish_failure(
                    job_id,
                    "analysis_run_id_missing",
                    "年度分析进程未返回有效 run_id，无法确认报告归档。",
                )
                return
            self._update(job_id, run_id=run_id, stage="validating_report_archive")
            try:
                archive = load_annual_report(self.project_root, run_id)
            except AnnualReportReadError as exc:
                manifest = _load_run_manifest(self.project_root, run_id)
                if manifest is not None and manifest.get("status") == "failed":
                    error_type = manifest.get("error_type") or _failure_error_type(self.project_root, run_id)
                    suffix = f"（{error_type}）" if _safe_error_type(error_type) else ""
                    self._finish_failure(
                        job_id,
                        "analysis_failed",
                        f"年度分析未完成{suffix}。失败详情保存在该 run_id 的归档记录中。",
                        run_id=run_id,
                    )
                    return
                self._finish_failure(
                    job_id,
                    "analysis_archive_invalid",
                    f"年度分析报告归档未通过校验（{exc.code}）。",
                    run_id=run_id,
                )
                return

            summary = archive.manifest_summary
            if (
                summary.get("company_id") != normalized["company_id"]
                or summary.get("report_year") != normalized["report_year"]
                or summary.get("document_id") != normalized["document_id"]
                or summary.get("source_pdf_sha256") != normalized["sha256"]
            ):
                self._finish_failure(
                    job_id,
                    "analysis_identity_mismatch",
                    "归档报告的来源身份与任务请求不一致。",
                    run_id=run_id,
                )
                return
            terminal_status = summary["status"]
            if terminal_status not in {"completed", "completed_with_issues"}:
                self._finish_failure(
                    job_id,
                    "analysis_status_invalid",
                    "年度分析报告的终态不受支持。",
                    run_id=run_id,
                )
                return
            self._update(
                job_id,
                status=terminal_status,
                stage=terminal_status,
                run_id=run_id,
                result_url=f"/v1/annual-analyses/{run_id}",
                error=None,
            )
        except AnnualAnalysisJobError as exc:
            self._finish_failure(job_id, exc.code, exc.message)
        except subprocess.TimeoutExpired:
            self._finish_failure(
                job_id,
                "analysis_timeout",
                "年度分析超过配置的运行时限，分析进程已终止；请检查 PDF 后重新提交。",
            )
        except Exception:
            # Never persist exception text or a subprocess traceback. It may
            # contain local paths or environment-specific values.
            self._finish_failure(
                job_id,
                "analysis_worker_failed",
                "年度分析任务执行失败；请检查运行归档并重新提交。",
            )
        finally:
            self._slots.release()

    def _load_state(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(job_id)
            if state is None:
                raise AnnualAnalysisJobError(404, "job_not_found", "找不到该年度分析任务。")
            return dict(state)

    def _update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(job_id)
            if state is None:
                raise AnnualAnalysisJobError(404, "job_not_found", "找不到该年度分析任务。")
            state.update(fields)
            state["updated_at"] = _utc_now()
            job_dir = _safe_existing_job_dir(self.jobs_root, job_id)
            _write_state(job_dir / "job.json", state, create=False)
            return _public_state(state)

    def _finish_failure(
        self,
        job_id: str,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
    ) -> None:
        self._update(
            job_id,
            status="failed",
            stage="failed",
            run_id=run_id,
            result_url=None,
            error={"code": code, "message": message},
        )

    def _recover_interrupted_jobs(self) -> None:
        jobs_root = self.jobs_root
        if not jobs_root.exists():
            return
        try:
            _ensure_artifact_directories(self.project_root, include_job_root=False)
            entries = list(jobs_root.iterdir())
        except (OSError, AnnualAnalysisJobError):
            return
        for entry in entries:
            try:
                info = entry.lstat()
                if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                    continue
                name = _validate_job_id(entry.name)
                state_path = _safe_regular_file(entry / "job.json", self.project_root)
                state = _read_json_object(state_path, _MAX_SOURCE_RECORD_BYTES)
                if state.get("job_id") != name or state.get("status") not in {"queued", "running"}:
                    self._states[name] = state
                    continue
                state.update(
                    status="interrupted",
                    stage="interrupted",
                    updated_at=_utc_now(),
                    result_url=None,
                    error={
                        "code": "service_interrupted",
                        "message": "服务在任务完成前退出；该任务已标记为中断，请重新提交。",
                    },
                )
                _write_state(state_path, state, create=False)
                self._states[name] = state
            except (OSError, ValueError, AnnualAnalysisJobError, json.JSONDecodeError):
                continue


def validate_job_request(project_root: Path, request: Mapping[str, Any]) -> dict[str, Any]:
    """检查请求元数据、来源路径、source.json 身份和字节 SHA256。"""

    company_id = request.get("company_id")
    report_year = request.get("report_year")
    document_id = request.get("document_id")
    source_pdf_path = request.get("source_pdf_path")
    sha256 = request.get("sha256")
    mode = request.get("mode", "deterministic")
    if not isinstance(company_id, str) or _COMPANY_ID.fullmatch(company_id) is None or ".." in company_id:
        raise AnnualAnalysisJobError(400, "invalid_company_id", "company_id 格式无效。")
    if type(report_year) is not int or not 1900 <= report_year <= 2100:
        raise AnnualAnalysisJobError(400, "invalid_report_year", "report_year 必须是 1900 到 2100 的整数。")
    if not isinstance(document_id, str) or _DOCUMENT_ID.fullmatch(document_id) is None or ".." in document_id:
        raise AnnualAnalysisJobError(400, "invalid_document_id", "document_id 格式无效。")
    if not isinstance(mode, str) or mode not in {"deterministic", "m3_screening", "model_investigation"}:
        raise AnnualAnalysisJobError(
            400,
            "invalid_mode",
            "mode 只能是 deterministic、m3_screening 或 model_investigation。",
        )
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise AnnualAnalysisJobError(400, "invalid_sha256", "sha256 必须是 64 位十六进制字符串。")
    relative_parts = _source_relative_parts(source_pdf_path)
    if (
        len(relative_parts) != 4
        or relative_parts[0] != company_id
        or relative_parts[1] != str(report_year)
        or relative_parts[2] != document_id
        or not relative_parts[3].lower().endswith(".pdf")
    ):
        raise AnnualAnalysisJobError(
            409,
            "source_identity_mismatch",
            "source_pdf_path 的公司、年度或文档目录与请求不一致。",
        )

    root = Path(project_root).resolve()
    source_path = _safe_data_path(root, ("data", "raw", *relative_parts), directory=False, missing_code="source_not_found")
    if source_path.stat().st_size > _MAX_SOURCE_PDF_BYTES:
        raise AnnualAnalysisJobError(413, "source_too_large", "原始 PDF 超过允许的 256 MiB 上限。")
    actual_sha256 = _sha256_checked_file(source_path, root)
    expected_sha256 = sha256.lower()
    if actual_sha256 != expected_sha256:
        raise AnnualAnalysisJobError(409, "source_sha256_mismatch", "请求 SHA256 与原始 PDF 内容不一致。")

    record_path = _safe_data_path(
        root,
        ("data", "raw", *relative_parts[:3], "source.json"),
        directory=False,
        missing_code="source_record_not_found",
    )
    source_record = _read_json_object(record_path, _MAX_SOURCE_RECORD_BYTES)
    _validate_source_record(
        source_record,
        company_id=company_id,
        report_year=report_year,
        document_id=document_id,
        source_pdf_path="/".join(relative_parts),
        sha256=expected_sha256,
    )
    return {
        "company_id": company_id,
        "report_year": report_year,
        "document_id": document_id,
        "source_pdf_path": "/".join(relative_parts),
        "sha256": expected_sha256,
        "mode": mode,
    }


def get_annual_analysis_job_service(project_root: Path) -> AnnualAnalysisJobService:
    """同一进程和项目根目录共用一个有界队列，避免多 app 实例重复启动。"""

    root = Path(project_root).resolve()
    key = os.path.normcase(str(root))
    with _SERVICE_REGISTRY_LOCK:
        service = _SERVICE_REGISTRY.get(key)
        if service is None:
            service = AnnualAnalysisJobService(root)
            _SERVICE_REGISTRY[key] = service
        return service


def _source_relative_parts(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or value.startswith("/")
        or (len(value) >= 2 and value[1] == ":")
    ):
        raise AnnualAnalysisJobError(400, "invalid_source_path", "source_pdf_path 必须是 data/raw 内的 POSIX 相对路径。")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part) for part in parts):
        raise AnnualAnalysisJobError(400, "invalid_source_path", "source_pdf_path 包含无效路径片段。")
    if any(".." in part for part in parts):
        raise AnnualAnalysisJobError(400, "invalid_source_path", "source_pdf_path 包含无效路径片段。")
    return parts


def _validate_source_record(
    source_record: Mapping[str, Any],
    *,
    company_id: str,
    report_year: int,
    document_id: str,
    source_pdf_path: str,
    sha256: str,
) -> None:
    report_period = source_record.get("report_period")
    period_year = None
    if isinstance(report_period, str):
        match = re.match(r"^(\d{4})(?:-|$)", report_period)
        period_year = int(match.group(1)) if match else None
    local_path = source_record.get("local_path")
    if (
        source_record.get("company_id") != company_id
        or source_record.get("document_id") != document_id
        or period_year != report_year
        or local_path != f"data/raw/{source_pdf_path}"
    ):
        raise AnnualAnalysisJobError(
            409,
            "source_identity_mismatch",
            "source.json 中的公司、报告年度、文档标识或本地路径与请求不一致。",
        )
    record_sha = source_record.get("sha256")
    if not isinstance(record_sha, str) or record_sha.lower() != sha256:
        raise AnnualAnalysisJobError(409, "source_record_hash_mismatch", "source.json 记录的 SHA256 与原始 PDF 不一致。")


def _invoke_cli(project_root: Path, artifacts_root: Path, request: Mapping[str, Any]) -> CliResult:
    script_path = project_root / "scripts" / "analyze_annual.py"
    if not script_path.is_file():
        raise RuntimeError("年度分析 CLI 脚本不存在。")
    source_pdf = project_root / "data" / "raw" / Path(*str(request["source_pdf_path"]).split("/"))
    command = [
        sys.executable,
        str(script_path),
        "--source-pdf",
        str(source_pdf),
        "--company-id",
        str(request["company_id"]),
        "--report-year",
        str(request["report_year"]),
        "--document-id",
        str(request["document_id"]),
        "--expected-sha256",
        str(request["sha256"]),
        "--artifacts-root",
        str(artifacts_root),
    ]
    if request["mode"] == "m3_screening":
        command.append("--with-m3-screening")
    environment = _cli_environment_without_credentials()
    if request["mode"] == "model_investigation":
        source_record = source_pdf.parent / "source.json"
        command.extend(["--with-model", "--source-record", str(source_record)])
        settings = resolve_model_settings(project_root)
        environment.update(
            {
                "MODEL_BASE_URL": settings.base_url,
                "MODEL_API_KEY": settings.api_key,
                "MODEL_NAME": settings.model,
            }
        )
    completed = subprocess.run(
        command,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=_configured_cli_timeout_seconds(),
    )
    return CliResult(completed.returncode, completed.stdout, completed.stderr)


def _configured_cli_timeout_seconds() -> int:
    raw_value = os.environ.get("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS")
    if raw_value is None:
        return _DEFAULT_CLI_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        raise RuntimeError("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS 必须是 60 到 900 之间的整数。") from None
    if not _MIN_CLI_TIMEOUT_SECONDS <= value <= _MAX_CLI_TIMEOUT_SECONDS:
        raise RuntimeError("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS 必须是 60 到 900 之间的整数。")
    return value


def _cli_environment_without_credentials() -> dict[str, str]:
    # Deterministic and M3 CLI paths do not need any model configuration.
    blocked = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "QWEN_API_KEY",
        "AZURE_OPENAI_API_KEY",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("MODEL_") and key.upper() not in blocked
    }


def get_annual_analysis_capabilities(project_root: Path | None = None) -> dict[str, Any]:
    """Expose model readiness without returning model configuration values."""

    try:
        import importlib

        importlib.import_module("langgraph.graph")
    except ImportError:
        return {"model_investigation": {"available": False, "status": "dependency_unavailable"}}
    try:
        root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[4]
        resolve_model_settings(root)
    except (ModelConfigError, ModelSettingsStoreError):
        return {"model_investigation": {"available": False, "status": "not_configured"}}
    except Exception:
        return {"model_investigation": {"available": False, "status": "not_configured"}}
    return {"model_investigation": {"available": True, "status": "ready"}}


def _run_id_from_output(output: str) -> str | None:
    for match in _RUN_ID.finditer(output):
        candidate = match.group(1)
        if re.fullmatch(r"annual-analysis-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", candidate):
            return candidate
    return None


def _load_run_manifest(project_root: Path, run_id: str) -> dict[str, Any] | None:
    try:
        checked = _safe_run_id(run_id)
        path = _safe_data_path(
            project_root,
            ("artifacts", "runs", checked, "manifest.json"),
            directory=False,
            missing_code="run_not_found",
        )
        return _read_json_object(path, _MAX_SOURCE_RECORD_BYTES)
    except (AnnualAnalysisJobError, OSError, ValueError, json.JSONDecodeError):
        return None


def _failure_error_type(project_root: Path, run_id: str) -> str | None:
    try:
        path = _safe_data_path(
            project_root,
            ("artifacts", "runs", _safe_run_id(run_id), "failure.json"),
            directory=False,
            missing_code="run_not_found",
        )
        payload = _read_json_object(path, _MAX_SOURCE_RECORD_BYTES)
        error_type = payload.get("error_type")
        return error_type if isinstance(error_type, str) else None
    except (AnnualAnalysisJobError, OSError, ValueError, json.JSONDecodeError):
        return None


def _safe_error_type(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", value) is not None


def _safe_run_id(value: str) -> str:
    if re.fullmatch(r"annual-analysis-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is None:
        raise AnnualAnalysisJobError(400, "invalid_run_id", "run_id 格式无效。")
    return value


def _validate_job_id(value: Any) -> str:
    if not isinstance(value, str) or _JOB_ID.fullmatch(value) is None:
        raise AnnualAnalysisJobError(400, "invalid_job_id", "job_id 格式无效。")
    return value


def _safe_existing_job_dir(jobs_root: Path, job_id: str) -> Path:
    checked = _validate_job_id(job_id)
    try:
        return _safe_data_path(
            jobs_root.parent.parent,
            ("artifacts", "annual-analysis-jobs", checked),
            directory=True,
            missing_code="job_not_found",
        )
    except AnnualAnalysisJobError as exc:
        if exc.code == "job_not_found":
            raise AnnualAnalysisJobError(404, "job_not_found", "找不到该年度分析任务。") from None
        raise


def _ensure_artifact_directories(project_root: Path, *, include_job_root: bool) -> None:
    root = Path(project_root).resolve()
    _ensure_directory_chain(root, root / "artifacts")
    artifacts = root / "artifacts"
    for child in ("runs", "reports"):
        path = artifacts / child
        if path.exists():
            _require_plain_directory(path, root)
        else:
            path.mkdir(exist_ok=False)
    jobs = artifacts / "annual-analysis-jobs"
    if include_job_root:
        if jobs.exists():
            _require_plain_directory(jobs, root)
        else:
            jobs.mkdir(exist_ok=False)
    elif jobs.exists():
        _require_plain_directory(jobs, root)


def _ensure_directory_chain(root: Path, target: Path) -> None:
    if not target.is_relative_to(root):
        raise AnnualAnalysisJobError(400, "invalid_artifact_path", "任务归档路径超出项目目录。")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.exists():
            _require_plain_directory(current, root)
        else:
            current.mkdir(exist_ok=False)


def _require_plain_directory(path: Path, root: Path) -> None:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise AnnualAnalysisJobError(409, "artifact_path_invalid", "任务归档目录无法安全访问。") from None
    if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode) or not resolved.is_relative_to(root):
        raise AnnualAnalysisJobError(409, "artifact_path_invalid", "任务归档目录包含符号链接或越界路径。")


def _safe_data_path(
    root: Path,
    parts: tuple[str, ...],
    *,
    directory: bool,
    missing_code: str,
) -> Path:
    anchor = Path(root).resolve()
    current = anchor
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            status = 404 if missing_code in {"source_not_found", "source_record_not_found", "job_not_found", "run_not_found"} else 409
            message = "找不到请求的原始文件或来源记录。" if status == 404 else "请求的归档路径不存在。"
            raise AnnualAnalysisJobError(status, missing_code, message) from None
        except OSError:
            raise AnnualAnalysisJobError(409, "source_path_invalid", "来源路径无法安全读取。") from None
        is_final = index == len(parts) - 1
        if _is_reparse_point(info):
            raise AnnualAnalysisJobError(400, "source_path_invalid", "来源路径包含符号链接或 reparse point。")
        try:
            resolved = current.resolve(strict=True)
        except OSError:
            raise AnnualAnalysisJobError(409, "source_path_invalid", "来源路径无法安全读取。") from None
        if resolved == anchor or not resolved.is_relative_to(anchor):
            raise AnnualAnalysisJobError(400, "source_path_invalid", "来源路径超出项目目录。")
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise AnnualAnalysisJobError(400, "source_path_invalid", "来源路径目录结构无效。")
        if is_final and directory and not stat.S_ISDIR(info.st_mode):
            raise AnnualAnalysisJobError(400, "source_path_invalid", "来源路径目录结构无效。")
        if is_final and not directory and not stat.S_ISREG(info.st_mode):
            raise AnnualAnalysisJobError(400, "source_path_invalid", "来源文件必须是普通文件。")
    return current


def _safe_regular_file(path: Path, project_root: Path) -> Path:
    try:
        relative = path.relative_to(Path(project_root).resolve())
    except ValueError:
        raise AnnualAnalysisJobError(400, "invalid_path", "任务状态路径超出项目目录。") from None
    return _safe_data_path(
        project_root,
        tuple(relative.parts),
        directory=False,
        missing_code="job_not_found",
    )


def _read_json_object(path: Path, max_bytes: int) -> dict[str, Any]:
    try:
        before = path.lstat()
        if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise AnnualAnalysisJobError(409, "source_record_invalid", "来源记录不是大小受限的普通 JSON 文件。")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
                raise AnnualAnalysisJobError(409, "source_record_invalid", "来源记录在读取期间发生变化。")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                content = handle.read(max_bytes + 1)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after) or len(content) > max_bytes:
            raise AnnualAnalysisJobError(409, "source_record_invalid", "来源记录在读取期间发生变化。")
        payload = json.loads(content.decode("utf-8"))
    except AnnualAnalysisJobError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise AnnualAnalysisJobError(409, "source_record_invalid", "来源记录无法读取或 JSON 格式无效。") from None
    if not isinstance(payload, dict):
        raise AnnualAnalysisJobError(409, "source_record_invalid", "来源记录顶层必须是 JSON 对象。")
    return payload


def _write_state(path: Path, state: Mapping[str, Any], *, create: bool) -> None:
    payload = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if create:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            raise AnnualAnalysisJobError(409, "job_state_exists", "任务状态文件已存在，未覆盖。") from None
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": state.get("job_id"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "request": state.get("request"),
        "run_id": state.get("run_id"),
        "result_url": state.get("result_url"),
        "error": state.get("error"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_reparse_point(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _sha256_checked_file(path: Path, project_root: Path) -> str:
    try:
        before = path.lstat()
        if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SOURCE_PDF_BYTES:
            raise AnnualAnalysisJobError(400, "source_path_invalid", "原始 PDF 不是大小受限的普通文件。")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        digest = hashlib.sha256()
        try:
            opened = os.fstat(descriptor)
            if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
                raise AnnualAnalysisJobError(409, "source_path_changed", "原始 PDF 在读取期间发生变化。")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                header = handle.read(5)
                if not header.startswith(b"%PDF"):
                    raise AnnualAnalysisJobError(422, "invalid_pdf", "原始文件内容不是 PDF。")
                digest.update(header)
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after):
            raise AnnualAnalysisJobError(409, "source_path_changed", "原始 PDF 在读取期间发生变化。")
    except AnnualAnalysisJobError:
        raise
    except OSError:
        raise AnnualAnalysisJobError(409, "source_not_readable", "原始 PDF 无法安全读取。") from None
    return digest.hexdigest()
