"""持久化并运行 FINTRACE v2 年度分析任务。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import atexit
import ctypes
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from finagent.api.annual_report_read import AnnualReportReadError, load_annual_report
from finagent.core.annual_runtime_identity import (
    ANNUAL_RUNTIME_PROTOCOL_VERSION,
    RUNTIME_IDENTITY_KIND,
    capture_api_runtime_identity,
    resolve_source_root,
    runtime_identity_is_valid,
    source_manifest_fingerprint,
)
from finagent.core.model_settings import ModelConfigError
from finagent.core.model_settings_store import ModelSettingsStoreError, resolve_model_settings
from finagent.core.annual_checkpoints import AnnualCheckpointError, save_annual_checkpoint
from finagent.core.annual_recovery import (
    read_gate_admission_count,
    select_call_gate_database,
    validate_annual_recovery_parent,
)
from finagent.core.model_call_ledger import close_admission_gate, open_admission_gate
from finagent.core.model_call_ledger import read_gate_stage_admission_counts
from finagent.core.model_call_ledger import (
    ModelCallAdmissionError,
    ModelCallLedgerReadError,
    read_shared_goal_budget,
)
from finagent.core.annual_work_budget import (
    BUDGET_CONTRACT_VERSION,
    DISCOVERY_BUDGET_SECONDS,
    FINALIZATION_RESERVE_SECONDS,
    FINAL_REVIEW_BUDGET_SECONDS,
    FIXED_INVESTIGATION_BUDGET_SECONDS,
    GLOBAL_REQUEST_LIMIT_CHARS,
    ISSUE_GROUPING_RESERVED_SECONDS,
    MAIN_REPORT_BUDGET_SECONDS,
    MODEL_CALL_TIMEOUT_SECONDS,
    NEW_RUN_ATTEMPT_BUDGET,
    NEW_RUN_STAGE_ATTEMPT_BUDGETS,
    RESUME_ATTEMPT_BUDGET,
    RESUME_STAGE_ATTEMPT_BUDGETS,
    REVIEW_REQUEST_LIMIT_CHARS,
    archived_budget_contract_v2,
    archived_budget_contract_v3,
    archived_runtime_stage_time_limits_v1,
    archived_runtime_stage_time_limits_v2,
    archived_runtime_stage_time_limits_v3,
    runtime_stage_time_limits,
)

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
_DEFAULT_DETERMINISTIC_CLI_TIMEOUT_SECONDS = 900
_DEFAULT_MODEL_CLI_TIMEOUT_SECONDS = 3600
_MIN_CLI_TIMEOUT_SECONDS = 60
_MAX_DETERMINISTIC_CLI_TIMEOUT_SECONDS = 900
_MAX_MODEL_CLI_TIMEOUT_SECONDS = 3600
_RUNTIME_PROBE_TIMEOUT_SECONDS = 15
_CALL_LIVENESS_INTERVAL_SECONDS = 1.0
_CALL_LIVENESS_STALE_SECONDS = 5.0
_RECOVERY_OWNER_WAIT_SECONDS = 12.0
_RECOVERY_OWNER_POLL_INTERVAL_SECONDS = 0.1
_CANCEL_GRACE_SECONDS = 3.0
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


def _sealed_final_archive_status(archive: Any) -> str | None:
    snapshot = archive.snapshot
    if isinstance(snapshot, Mapping):
        if snapshot.get("is_partial") is True:
            return None
        terminal = snapshot.get("state")
    else:
        terminal = archive.manifest_summary.get("status")
    if terminal in {"completed", "completed_with_issues"}:
        return str(terminal)
    return None


class AnnualAnalysisJobService:
    """单进程内的有界队列；状态存入 artifacts/annual-analysis-jobs。"""

    def __init__(
        self,
        project_root: Path,
        *,
        max_workers: int = _MAX_WORKERS,
        max_queued_jobs: int = _MAX_QUEUED_JOBS,
        api_runtime_identity: Mapping[str, Any] | None = None,
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
        self._futures: dict[str, Future[Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._heartbeats: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._slot_owners: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._cancel_owner_stopped: set[str] = set()
        self._liveness_failed: set[str] = set()
        self._cancel_finalizing: set[str] = set()
        self._cancel_done: dict[str, threading.Event] = {}
        self._recovery_settling: set[str] = set()
        self._closing = False
        self.api_runtime_identity = dict(
            api_runtime_identity or capture_api_runtime_identity(self.project_root)
        )
        self.cli_startup_identity = _probe_cli_runtime_identity(self.project_root)
        self.cli_startup_error = None if self.cli_startup_identity is not None else "cli_identity_unavailable"
        self._recover_interrupted_jobs()

    @property
    def jobs_root(self) -> Path:
        return self.project_root / "artifacts" / "annual-analysis-jobs"

    def create(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_runtime_compatible()
        normalized = validate_job_request(self.project_root, request)
        try:
            budget_contract = (
                _job_budget_contract("new_run")
                if normalized["mode"] in {"model_investigation", "integrated_analysis"}
                else None
            )
        except RuntimeError:
            raise AnnualAnalysisJobError(
                500, "analysis_budget_invalid", "年度分析预算或共同账本配置无效，未创建任务。"
            ) from None
        if normalized["mode"] in {"model_investigation", "integrated_analysis"}:
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
                now = _utc_now_precise()
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
                    "_cli_owner_expected": False,
                    "recovery_capability": {"can_resume": False, "reason": "recovery_not_available"},
                    "recovery_progress": _empty_recovery_progress(None),
                }
                if budget_contract is not None:
                    state["_analysis_budget_contract"] = budget_contract
                self._states[job_id] = state
                self._slot_owners.add(job_id)
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
                    self._futures[job_id] = self._executor.submit(self._execute, job_id)
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
                    self._persist_state_locked(job_id, state)
                    self._slots.release()
                    self._slot_owners.discard(job_id)
                    submitted = True
                return _public_job_state(self.project_root, state)
        except Exception:
            if not submitted:
                self._slots.release()
            raise

    def get(self, job_id: str) -> dict[str, Any]:
        checked = _validate_job_id(job_id)
        self._settle_pending_recovery(checked)
        with self._lock:
            job_dir = _safe_existing_job_dir(self.jobs_root, checked)
            state_path = _safe_regular_file(job_dir / "job.json", self.project_root)
            state = _read_json_object(state_path, _MAX_SOURCE_RECORD_BYTES)
            if state.get("job_id") != checked:
                raise AnnualAnalysisJobError(404, "job_not_found", "找不到该年度分析任务。")
            self._states[checked] = state
            state = dict(state)
        # Read once outside the service lock. All projections below describe
        # this verified generation (or this read's failure); the next GET must
        # strictly read again. Ellipsis in projection defaults means no read
        # was supplied, whereas None is an already failed/unavailable read.
        strict_archive: Any | None = None
        strict_archive_error: Exception | None = None
        run_id = state.get("run_id")
        if isinstance(run_id, str):
            try:
                strict_archive = load_annual_report(self.project_root, run_id)
            except Exception as exc:
                strict_archive_error = exc
        public = _public_state(state)
        public["result_availability"] = _result_availability_projection(
            self.project_root, state, archive=strict_archive, archive_error=strict_archive_error,
        )
        public["recovery_progress"] = self._recovery_progress_projection(state, archive=strict_archive)
        public["recovery_capability"] = self._recovery_capability_projection(
            state, public["result_availability"], archive=strict_archive, archive_error=strict_archive_error,
        )
        recovery = _discovery_recovery_projection(self.project_root, state, archive=strict_archive)
        if recovery is not None:
            public["recovery"] = recovery
        if state.get("status") == "running" and state.get("stage") == "annual_analysis_cli":
            progress = _read_discovery_progress(job_dir, checked, state)
            if progress is not None:
                public["discovery_progress"] = progress
        budget = _analysis_budget_projection(
            self.project_root, state,
            progress=public.get("discovery_progress") if isinstance(public.get("discovery_progress"), Mapping) else None,
            archive=strict_archive,
        )
        if budget is not None:
            public["analysis_budget"] = budget
        return public

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Close admission before requesting cancellation; terminal jobs stay immutable."""

        checked = _validate_job_id(job_id)
        process: subprocess.Popen[str] | None = None
        gate_db: Path | None = None
        terminal = False
        with self._lock:
            state = self._load_persisted_state_locked(checked)
            status = state.get("status")
            if status not in {"queued", "running"}:
                terminal = True
            future = self._futures.get(checked)
            if not terminal and status == "queued" and future is not None and future.cancel():
                self._futures.pop(checked, None)
                state.update(
                    status="cancelled",
                    stage="cancelled",
                    updated_at=_utc_now(),
                    error=None,
                )
                self._persist_state_locked(checked, state)
                self._release_slot_once_locked(checked)
                state = dict(state)
            elif not terminal:
                state = None
            if not terminal and state is None:
                self._cancel_requested.add(checked)
                gate_value = self._load_persisted_state_locked(checked).get("_call_gate_db")
                gate_db = Path(gate_value) if isinstance(gate_value, str) else None
                process = self._processes.get(checked)
                # Do not advertise cancellation until the SQLite admission
                # transaction has closed the gate below.
        if terminal or state is not None:
            return self.get(checked)

        if gate_db is not None:
            try:
                self._close_job_gate(checked, gate_db)
            except Exception:
                # Fail closed at the process boundary too. Never continue a
                # running request when the persistent gate cannot be closed.
                self._write_cancel_marker(checked)
                self._stop_owned_process(checked, process)
                self._mark_cancel_pending(
                    checked,
                    code="call_gate_unavailable",
                    message="模型调用闸门无法确认关闭；已请求停止，已保存报告可只读查看，暂不能续跑。",
                )
                raise AnnualAnalysisJobError(
                    503,
                    "call_gate_unavailable",
                    "模型调用闸门无法确认关闭；任务仍在收束，暂不能续跑。",
                ) from None
        self._write_cancel_marker(checked)
        with self._lock:
            try:
                state = self._load_persisted_state_locked(checked)
            except AnnualAnalysisJobError:
                state = None
            if state is not None and state.get("status") in {"queued", "running"}:
                state["stage"] = "cancellation_requested"
                self._persist_state_locked(checked, state)
        if self._stop_owned_process(checked, process):
            self._finish_cancelled(checked)
        else:
            self._mark_cancel_pending(checked)
        return self.get(checked)

    def resume(self, parent_job_id: str, parent_run_id: str) -> dict[str, Any]:
        """Create one immutable child job for a strictly validated recoverable parent."""

        checked_parent_job = _validate_job_id(parent_job_id)
        checked_parent_run = _safe_run_id(parent_run_id)
        self._ensure_runtime_compatible()
        self._settle_pending_recovery(checked_parent_job)
        with self._lock:
            parent_state = self._load_persisted_state_locked(checked_parent_job)
            if parent_state.get("run_id") != checked_parent_run:
                raise AnnualAnalysisJobError(409, "recovery_parent_mismatch", "父任务与父运行编号不匹配。")
            if parent_state.get("_recovery_owner_pending") is True:
                raise AnnualAnalysisJobError(
                    409,
                    "parent_active",
                    "原年度分析进程尚未确认停止；已保存报告可只读查看，暂不能续跑。",
                )
            if parent_state.get("status") in {"queued", "running"}:
                raise AnnualAnalysisJobError(409, "parent_active", "父年度分析任务仍在运行，暂不能续跑。")
            if self._has_active_child_locked(checked_parent_job, checked_parent_run):
                raise AnnualAnalysisJobError(409, "already_has_active_child", "该父运行已有续跑任务正在执行。")
            parent_request = parent_state.get("request")
            if not isinstance(parent_request, Mapping):
                raise AnnualAnalysisJobError(409, "recovery_parent_invalid", "父任务请求记录无效，不能续跑。")
            normalized = validate_job_request(self.project_root, parent_request)

        try:
            archive = load_annual_report(self.project_root, checked_parent_run)
        except AnnualReportReadError as exc:
            code = "source_invalid" if _is_source_validation_error(exc.code) else "recovery_not_available"
            raise AnnualAnalysisJobError(409, code, "父运行未通过当前来源与报告严格校验，不能续跑。") from None
        try:
            settings = resolve_model_settings(self.project_root)
        except (ModelConfigError, ModelSettingsStoreError):
            raise AnnualAnalysisJobError(409, "binding_missing", "父运行的模型绑定无法确认，不能续跑。") from None
        capability = validate_annual_recovery_parent(
            self.project_root,
            checked_parent_run,
            normalized,
            settings=settings,
            archive=archive,
            require_issue_pipeline=True,
        )
        if capability.get("can_resume") is not True:
            reason = _public_recovery_reason(capability.get("reason"))
            raise AnnualAnalysisJobError(
                409,
                reason,
                _recovery_reason_message(reason),
            )

        try:
            budget_contract = _job_budget_contract("resume")
        except RuntimeError:
            raise AnnualAnalysisJobError(
                500, "analysis_budget_invalid", "年度分析预算或共同账本配置无效，未创建续跑任务。"
            ) from None

        if not self._slots.acquire(blocking=False):
            raise AnnualAnalysisJobError(429, "job_capacity_reached", "年度分析任务队列已满，请稍后重试。")
        submitted = False
        child_job_id: str | None = None
        try:
            with self._lock:
                latest_parent = self._load_persisted_state_locked(checked_parent_job)
                if latest_parent.get("status") in {"queued", "running"}:
                    raise AnnualAnalysisJobError(409, "parent_active", "父年度分析任务仍在运行，暂不能续跑。")
                if self._has_active_child_locked(checked_parent_job, checked_parent_run):
                    raise AnnualAnalysisJobError(409, "already_has_active_child", "该父运行已有续跑任务正在执行。")
                _ensure_artifact_directories(self.project_root, include_job_root=True)
                child_job_id = f"annual-job-{uuid4().hex}"
                child_dir = self.jobs_root / child_job_id
                child_dir.mkdir(exist_ok=False)
                parent_progress = self._recovery_progress_projection(latest_parent)
                now = _utc_now_precise()
                state = {
                    "job_id": child_job_id,
                    "status": "queued",
                    "stage": "queued",
                    "created_at": now,
                    "updated_at": now,
                    "request": normalized,
                    "run_id": None,
                    "result_url": None,
                    "error": None,
                    "_cli_owner_expected": False,
                    "parent_job_id": checked_parent_job,
                    "parent_run_id": checked_parent_run,
                    "recovery": {
                        "contract_version": "fintrace_annual_recovery_v1",
                        "parent_run_id": checked_parent_run,
                        "reused_batches": 0,
                        "current_attempts": 0,
                    },
                    "recovery_progress": _empty_recovery_progress(
                        checked_parent_run,
                        parent_provider_admission_count=parent_progress["cumulative_provider_admission_count"],
                    ),
                    "recovery_capability": {"can_resume": False, "reason": "recovery_not_available"},
                }
                state["_analysis_budget_contract"] = budget_contract
                self._states[child_job_id] = state
                self._slot_owners.add(child_job_id)
                _write_state(child_dir / "job.json", state, create=True)
                try:
                    self._futures[child_job_id] = self._executor.submit(self._execute, child_job_id)
                except RuntimeError:
                    state.update(
                        status="failed",
                        stage="failed",
                        updated_at=_utc_now(),
                        error={
                            "code": "job_executor_unavailable",
                            "message": "年度分析服务当前无法启动续跑任务，请稍后重试。",
                        },
                    )
                    self._persist_state_locked(child_job_id, state)
                    self._release_slot_once_locked(child_job_id)
                    submitted = True
                    return _public_job_state(self.project_root, state)
                submitted = True
                return _public_job_state(self.project_root, state)
        finally:
            if not submitted:
                with self._lock:
                    if child_job_id is not None and child_job_id in self._slot_owners:
                        state = self._states.get(child_job_id)
                        if state is not None and state.get("status") == "queued":
                            state.update(
                                status="failed",
                                stage="failed",
                                updated_at=_utc_now(),
                                error={
                                    "code": "job_executor_unavailable",
                                    "message": "年度分析服务未能启动续跑任务。",
                                },
                            )
                            try:
                                self._persist_state_locked(child_job_id, state)
                            except (AnnualAnalysisJobError, OSError, ValueError):
                                self._states.pop(child_job_id, None)
                        self._futures.pop(child_job_id, None)
                        self._release_slot_once_locked(child_job_id)
                    else:
                        self._slots.release()

    def runtime_diagnostics(self) -> dict[str, Any]:
        current_source_fingerprint, current_source_file_count = source_manifest_fingerprint(self.project_root)
        cli = self.cli_startup_identity
        return {
            "kind": RUNTIME_IDENTITY_KIND,
            "protocol_version": ANNUAL_RUNTIME_PROTOCOL_VERSION,
            "api": _public_runtime_identity(self.api_runtime_identity),
            "cli_at_startup": _public_runtime_identity(cli) if cli is not None else None,
            "disk_source_manifest_sha256": current_source_fingerprint,
            "disk_source_file_count": current_source_file_count,
            "source_changed_since_api_start": current_source_fingerprint
            != self.api_runtime_identity.get("source_manifest_sha256"),
            "active_job_count": self.active_job_count(),
        }

    def active_job_count(self) -> int:
        with self._lock:
            return sum(state.get("status") in {"queued", "running"} for state in self._states.values())

    def _ensure_runtime_compatible(self) -> None:
        if not runtime_identity_is_valid(self.api_runtime_identity):
            raise AnnualAnalysisJobError(
                503,
                "runtime_identity_unverified",
                "当前 API 无法确认启动时加载的年度分析代码身份。请安全重启本项目服务后重试。",
            )
        if self.cli_startup_identity is None or not runtime_identity_is_valid(self.cli_startup_identity):
            raise AnnualAnalysisJobError(
                503,
                "runtime_version_mismatch",
                "当前 API 启动时未能确认同版本年度分析 CLI。请先安全重启本项目 API 后重试。",
            )
        disk_fingerprint, _ = source_manifest_fingerprint(self.project_root)
        if (
            disk_fingerprint != self.api_runtime_identity.get("source_manifest_sha256")
            or disk_fingerprint != self.cli_startup_identity.get("source_manifest_sha256")
            or self.cli_startup_identity.get("protocol_version") != ANNUAL_RUNTIME_PROTOCOL_VERSION
        ):
            raise AnnualAnalysisJobError(
                409,
                "runtime_version_mismatch",
                "API 内存版本与当前磁盘源码或年度分析 CLI 不一致，未创建任务。请使用启动脚本安全重启本项目服务。",
            )
        current_cli = _probe_cli_runtime_identity(self.project_root)
        if (
            current_cli is None
            or not runtime_identity_is_valid(current_cli)
            or current_cli.get("fingerprint") != self.cli_startup_identity.get("fingerprint")
            or current_cli.get("source_manifest_sha256") != disk_fingerprint
        ):
            raise AnnualAnalysisJobError(
                409,
                "runtime_version_mismatch",
                "年度分析 CLI 与 API 启动时记录的版本不一致，未创建任务。请使用启动脚本安全重启本项目服务。",
            )

    def _execute(self, job_id: str) -> None:
        heartbeat_stop: threading.Event | None = None
        run_id: str | None = None
        gate_db: Path | None = None
        gate_opened = False
        cli_returned = False
        try:
            with self._lock:
                state = self._load_persisted_state_locked(job_id)
                if state.get("status") == "cancelled":
                    return
                if state.get("status") != "queued":
                    return
            self._ensure_runtime_compatible()
            normalized = validate_job_request(self.project_root, state["request"])
            run_id = self._reserve_run_id()
            job_dir = _safe_existing_job_dir(self.jobs_root, job_id)
            gate_db = select_call_gate_database(self.project_root, job_dir)
            open_admission_gate(gate_db, job_id)
            gate_opened = True
            with self._lock:
                current = self._load_persisted_state_locked(job_id)
                if current.get("status") == "cancelled" or job_id in self._cancel_requested:
                    gate_closed, checked_gate = self._try_close_job_gate(job_id, gate_db)
                    if not gate_closed:
                        self._defer_terminal_after_gate_close_failure(
                            job_id,
                            run_id,
                            checked_gate,
                            terminal_target="cancelled",
                            owner_required=False,
                        )
                        return
                    self._cancel_owner_stopped.add(job_id)
                    self._finish_cancelled(job_id)
                    return
                started_at = _utc_now_precise()
                started_monotonic = time.monotonic()
                budget_contract = (
                    _job_budget_contract(
                        "resume" if isinstance(current.get("parent_run_id"), str) else "new_run"
                    )
                    if normalized["mode"] in {"model_investigation", "integrated_analysis"}
                    else None
                )
                current.update(
                    status="running",
                    stage="validating_source",
                    run_id=run_id,
                    result_url=f"/v1/annual-analyses/{run_id}",
                    error=None,
                    _call_gate_db=str(gate_db),
                    running_started_at=started_at,
                    _running_started_monotonic=started_monotonic,
                )
                if budget_contract is not None:
                    current["_analysis_budget_contract"] = budget_contract
                    current["_main_report_deadline_monotonic"] = (
                        started_monotonic + budget_contract["main_report_limit_seconds"]
                    )
                    current["_main_work_deadline_monotonic"] = (
                        started_monotonic + budget_contract["work_deadline_seconds"]
                    )
                current["recovery_progress"] = self._initial_recovery_progress(current)
                self._persist_state_locked(job_id, current)
            heartbeat_stop = self._start_heartbeat(job_id, job_dir / "call-liveness")
            artifacts_root = self.project_root / "artifacts"
            runner_request = dict(normalized)
            runner_request["_internal_job_id"] = job_id
            runner_request["_reserved_run_id"] = run_id
            runner_request["_request_mode"] = normalized["mode"]
            runner_request["_resume_parent_run_id"] = state.get("parent_run_id")
            runner_request["_call_gate_db"] = str(gate_db)
            runner_request["_call_cancel_file"] = str(job_dir / "call-cancel")
            runner_request["_call_liveness_file"] = str(job_dir / "call-liveness")
            runner_request["_call_stopped_file"] = str(job_dir / "call-stopped")
            runner_request["_active_source_sha256"] = str(normalized["sha256"])
            runner_request["_register_process"] = lambda process: self._register_process(job_id, process)
            runner_request["_expected_runtime_fingerprint"] = str(
                self.cli_startup_identity["fingerprint"]
            )
            if budget_contract is not None:
                runner_request["_running_started_at"] = started_at
                runner_request["_running_started_monotonic"] = started_monotonic
                runner_request["_main_report_deadline_monotonic"] = current["_main_report_deadline_monotonic"]
                runner_request["_main_work_deadline_monotonic"] = current["_main_work_deadline_monotonic"]
                runner_request["_analysis_budget_contract"] = budget_contract
            with self._lock:
                if job_id in self._cancel_requested:
                    # This check and the durable owner flag are serialized
                    # with cancel(). A false flag proves this worker has not
                    # handed control to Popen yet.
                    gate_closed, checked_gate = self._try_close_job_gate(job_id, gate_db)
                    if not gate_closed:
                        self._defer_terminal_after_gate_close_failure(
                            job_id,
                            run_id,
                            checked_gate,
                            terminal_target="cancelled",
                            owner_required=False,
                        )
                        return
                    self._cancel_owner_stopped.add(job_id)
                    self._finish_cancelled(job_id)
                    return
                current = self._load_persisted_state_locked(job_id)
                current["stage"] = "annual_analysis_cli"
                current["_cli_owner_expected"] = True
                self._persist_state_locked(job_id, current)
            result = _invoke_cli(self.project_root, artifacts_root, runner_request)
            cli_returned = True
            self._update(job_id, _cli_owner_expected=False)
            reported_run_id = _run_id_from_output(result.stdout + "\n" + result.stderr)
            if job_id in self._cancel_requested:
                gate_closed, checked_gate = self._try_close_job_gate(job_id, gate_db)
                if not gate_closed:
                    self._defer_terminal_after_gate_close_failure(
                        job_id,
                        run_id,
                        checked_gate,
                        terminal_target="cancelled",
                        owner_required=False,
                    )
                    return
                self._finish_cancelled(job_id)
                return
            if reported_run_id is not None and reported_run_id != run_id:
                self._finish_failure(
                    job_id,
                    "analysis_run_id_mismatch",
                    "年度分析进程返回的运行编号与预分配编号不一致。",
                    run_id=run_id,
                    gate_database_path=gate_db,
                    cli_returned=cli_returned,
                )
                return
            if reported_run_id is None:
                self._finish_failure(
                    job_id,
                    "analysis_run_id_missing",
                    "年度分析进程未返回有效 run_id，无法确认报告归档。",
                    run_id=run_id,
                    gate_database_path=gate_db,
                    cli_returned=cli_returned,
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
                        gate_database_path=gate_db,
                        cli_returned=cli_returned,
                    )
                    return
                self._finish_failure(
                    job_id,
                    "analysis_archive_invalid",
                    f"年度分析报告归档未通过校验（{exc.code}）。",
                    run_id=run_id,
                    gate_database_path=gate_db,
                    cli_returned=cli_returned,
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
                    gate_database_path=gate_db,
                    cli_returned=cli_returned,
                )
                return
            terminal_status = summary["status"]
            if terminal_status not in {"completed", "completed_with_issues"}:
                self._finish_failure(
                    job_id,
                    "analysis_status_invalid",
                    "年度分析报告的终态不受支持。",
                    run_id=run_id,
                    gate_database_path=gate_db,
                    cli_returned=cli_returned,
                )
                return
            gate_closed, checked_gate = self._try_close_job_gate(job_id, gate_db)
            if not gate_closed:
                self._defer_terminal_after_gate_close_failure(
                    job_id,
                    run_id,
                    checked_gate,
                    terminal_target=terminal_status,
                    owner_required=False,
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
            if job_id in self._cancel_requested:
                self._finish_cancelled(job_id)
            else:
                self._finish_failure(
                    job_id,
                    exc.code,
                    exc.message,
                    run_id=run_id,
                    gate_database_path=gate_db,
                    cli_returned=cli_returned,
                )
        except subprocess.TimeoutExpired:
            if job_id in self._cancel_requested:
                self._finish_cancelled(job_id)
            else:
                self._finish_cli_failure_after_stop(
                    job_id,
                    run_id,
                    gate_database_path=gate_db,
                    failure_kind="timeout",
                    code="analysis_timeout",
                    message="年度分析超过配置的运行时限，分析进程已终止；请检查 PDF 后重新提交。",
                )
        except Exception:
            # Never persist exception text or a subprocess traceback. It may
            # contain local paths or environment-specific values.
            if job_id in self._cancel_requested:
                self._finish_cancelled(job_id)
            else:
                self._finish_cli_failure_after_stop(
                    job_id,
                    run_id,
                    gate_database_path=gate_db,
                    failure_kind="worker",
                    code="analysis_worker_failed",
                    message="年度分析任务执行失败；请检查运行归档并重新提交。",
                )
        finally:
            try:
                if gate_opened and gate_db is not None:
                    gate_closed, checked_gate = self._try_close_job_gate(job_id, gate_db)
                    if not gate_closed:
                        with self._lock:
                            try:
                                current = self._load_persisted_state_locked(job_id)
                            except AnnualAnalysisJobError:
                                current = {}
                        existing_target = current.get("_recovery_terminal_target")
                        terminal_target = (
                            existing_target
                            if existing_target in {"failed", "interrupted", "cancelled"}
                            else current.get("status")
                        )
                        if terminal_target not in {
                            "completed", "completed_with_issues", "failed", "interrupted", "cancelled"
                        }:
                            terminal_target = "interrupted"
                        self._defer_terminal_after_gate_close_failure(
                            job_id,
                            run_id if run_id is not None else current.get("run_id"),
                            checked_gate,
                            terminal_target=str(terminal_target),
                            owner_required=not cli_returned and self._runner_owner_may_be_active(job_id),
                            failure_kind=current.get("_recovery_failure_kind"),
                        )
            finally:
                if heartbeat_stop is not None:
                    self._stop_heartbeat(job_id, heartbeat_stop)
                with self._lock:
                    self._processes.pop(job_id, None)
                    self._futures.pop(job_id, None)
                    self._release_slot_once_locked(job_id)

    def _load_state(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(job_id)
            if state is None:
                raise AnnualAnalysisJobError(404, "job_not_found", "找不到该年度分析任务。")
            return dict(state)

    def _update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            state = self._load_persisted_state_locked(job_id)
            state.update(fields)
            state["updated_at"] = _utc_now()
            self._persist_state_locked(job_id, state)
            return _public_state(state)

    def _finish_failure(
        self,
        job_id: str,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        gate_database_path: Path | None = None,
        cli_returned: bool = False,
    ) -> None:
        with self._lock:
            state = self._load_persisted_state_locked(job_id)
            if state.get("status") in {"completed", "completed_with_issues", "cancelled"}:
                return
            actual_run_id = run_id if run_id is not None else state.get("run_id")
            stored_gate = state.get("_call_gate_db")
            current_gate = Path(stored_gate) if isinstance(stored_gate, str) else gate_database_path
        if current_gate is not None:
            gate_closed, checked_gate = self._try_close_job_gate(job_id, current_gate)
            if not gate_closed:
                self._defer_terminal_after_gate_close_failure(
                    job_id,
                    actual_run_id if isinstance(actual_run_id, str) else None,
                    checked_gate,
                    terminal_target="failed",
                    owner_required=not cli_returned and self._runner_owner_may_be_active(job_id),
                    terminal_error={"code": code, "message": message},
                )
                return
        if isinstance(actual_run_id, str) and self._preserve_sealed_result(job_id, actual_run_id):
            return
        with self._lock:
            state = self._load_persisted_state_locked(job_id)
            if state.get("status") in {"completed", "completed_with_issues", "cancelled"}:
                return
            state.update(
                status="failed",
                stage="failed",
                run_id=actual_run_id,
                result_url=f"/v1/annual-analyses/{actual_run_id}" if actual_run_id else None,
                error={"code": code, "message": message},
            )
            self._persist_state_locked(job_id, state)

    def _load_persisted_state_locked(self, job_id: str) -> dict[str, Any]:
        checked = _validate_job_id(job_id)
        job_dir = _safe_existing_job_dir(self.jobs_root, checked)
        state_path = _safe_regular_file(job_dir / "job.json", self.project_root)
        state = _read_json_object(state_path, _MAX_SOURCE_RECORD_BYTES)
        if state.get("job_id") != checked:
            raise AnnualAnalysisJobError(404, "job_not_found", "找不到该年度分析任务。")
        self._states[checked] = state
        return state

    def _persist_state_locked(self, job_id: str, state: dict[str, Any]) -> None:
        if state.get("status") in {"completed", "completed_with_issues", "failed", "interrupted", "cancelled"}:
            if not isinstance(state.get("ended_at"), str):
                state["ended_at"] = _utc_now_precise()
        state["updated_at"] = _utc_now()
        job_dir = _safe_existing_job_dir(self.jobs_root, job_id)
        _write_state(job_dir / "job.json", state, create=False)
        self._states[job_id] = dict(state)

    def _release_slot_once_locked(self, job_id: str) -> None:
        if job_id in self._slot_owners:
            self._slot_owners.remove(job_id)
            self._slots.release()

    def _register_process(self, job_id: str, process: subprocess.Popen[str]) -> None:
        cancel_requested = False
        with self._lock:
            self._processes[job_id] = process
            state = self._load_persisted_state_locked(job_id)
            witness = _runner_owner_witness(process)
            if witness is not None:
                state["_runner_owner"] = witness
                self._persist_state_locked(job_id, state)
            cancel_requested = job_id in self._cancel_requested
        if cancel_requested:
            if self._terminate_owned_process(process):
                with self._lock:
                    self._cancel_owner_stopped.add(job_id)
            else:
                self._mark_cancel_pending(job_id)

    def _start_heartbeat(self, job_id: str, path: Path) -> threading.Event:
        stop = threading.Event()
        _touch_owned_file(path, self.project_root)

        def run() -> None:
            while not stop.wait(_CALL_LIVENESS_INTERVAL_SECONDS):
                try:
                    _refresh_owned_file_mtime(path, self.project_root)
                except (AnnualAnalysisJobError, OSError, NotImplementedError):
                    self._fail_closed_liveness(job_id)

        thread = threading.Thread(target=run, name=f"annual-liveness-{job_id[-8:]}", daemon=True)
        with self._lock:
            self._heartbeats[job_id] = (stop, thread)
        thread.start()
        return stop

    def _stop_heartbeat(self, job_id: str, stop: threading.Event) -> None:
        stop.set()
        with self._lock:
            heartbeat = self._heartbeats.pop(job_id, None)
        if heartbeat is not None and heartbeat[1].is_alive():
            heartbeat[1].join(timeout=2.0)

    def _write_cancel_marker(self, job_id: str) -> None:
        job_dir = _safe_existing_job_dir(self.jobs_root, job_id)
        path = job_dir / "call-cancel"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"cancelled\n")
            handle.flush()

    def _close_job_gate(self, job_id: str, database_path: Path) -> None:
        job_dir = _safe_existing_job_dir(self.jobs_root, job_id)
        local_database = (job_dir / "call-gate.sqlite3").resolve()
        try:
            configured_database = select_call_gate_database(self.project_root, job_dir).resolve()
        except Exception:
            configured_database = local_database
        checked_database = Path(database_path).resolve()
        if checked_database not in {local_database, configured_database}:
            raise AnnualAnalysisJobError(409, "call_gate_path_invalid", "本任务调用闸门路径不属于其隔离任务或当前共享账本。")
        close_admission_gate(checked_database, job_id)

    def _try_close_job_gate(
        self,
        job_id: str,
        fallback_database_path: Path | None = None,
    ) -> tuple[bool, Path | None]:
        try:
            with self._lock:
                state = self._load_persisted_state_locked(job_id)
                gate_value = state.get("_call_gate_db")
        except AnnualAnalysisJobError:
            gate_value = None
        database_path = Path(gate_value) if isinstance(gate_value, str) else fallback_database_path
        if database_path is None:
            return True, None
        try:
            self._close_job_gate(job_id, database_path)
        except Exception:
            return False, database_path
        return True, database_path

    def _defer_terminal_after_gate_close_failure(
        self,
        job_id: str,
        run_id: str | None,
        database_path: Path | None,
        *,
        terminal_target: str,
        owner_required: bool,
        terminal_error: Mapping[str, Any] | None = None,
        failure_kind: str | None = None,
    ) -> None:
        """Keep a readable result pending until its exact admission gate is closed."""

        with self._lock:
            current = self._load_persisted_state_locked(job_id)
            actual_run_id = run_id if isinstance(run_id, str) else current.get("run_id")
            if database_path is not None:
                current["_call_gate_db"] = str(database_path)
            pending_error = terminal_error
            if pending_error is None:
                existing_error = current.get("error")
                if (
                    isinstance(existing_error, Mapping)
                    and isinstance(existing_error.get("code"), str)
                    and existing_error.get("code") != "call_gate_unavailable"
                    and isinstance(existing_error.get("message"), str)
                ):
                    pending_error = {
                        "code": existing_error["code"],
                        "message": existing_error["message"],
                    }
            if (
                isinstance(pending_error, Mapping)
                and isinstance(pending_error.get("code"), str)
                and isinstance(pending_error.get("message"), str)
            ):
                current["_recovery_terminal_error"] = {
                    "code": pending_error["code"],
                    "message": pending_error["message"],
                }
            if failure_kind in {"timeout", "worker"}:
                current["_recovery_failure_kind"] = failure_kind
            current.update(
                status="interrupted",
                stage="interrupted",
                run_id=actual_run_id,
                result_url=(
                    f"/v1/annual-analyses/{actual_run_id}"
                    if isinstance(actual_run_id, str)
                    else None
                ),
                error={
                    "code": "call_gate_unavailable",
                    "message": "模型调用闸门无法确认关闭；已保存报告可只读查看，任务仍在收束，暂不能续跑。",
                },
                _recovery_owner_pending=True,
                _recovery_owner_required=owner_required,
            )
            if terminal_target in {"failed", "interrupted", "cancelled"}:
                current["_recovery_terminal_target"] = terminal_target
            else:
                current.pop("_recovery_terminal_target", None)
            self._persist_state_locked(job_id, current)

    def _runner_owner_may_be_active(self, job_id: str) -> bool:
        with self._lock:
            try:
                state = self._load_persisted_state_locked(job_id)
            except AnnualAnalysisJobError:
                return True
            process = self._processes.get(job_id)
        if not _runner_may_be_active(state, process):
            return False
        run_id = state.get("run_id")
        if isinstance(run_id, str) and _runner_stop_confirmed(
            self.jobs_root / job_id,
            state,
            run_id,
            process,
        ):
            return False
        if process is not None:
            try:
                return process.poll() is None
            except (OSError, subprocess.SubprocessError):
                return True
        return _runner_owner_status(state.get("_runner_owner")) != "stopped"

    def _stop_owned_process(self, job_id: str, process: subprocess.Popen[str] | None) -> bool:
        if process is None:
            with self._lock:
                process = self._processes.get(job_id)
                state = self._states.get(job_id)
            if process is None:
                run_id = state.get("run_id") if isinstance(state, Mapping) else None
                if isinstance(run_id, str) and _call_stopped_marker_matches(self.jobs_root / job_id, run_id):
                    return True
                if isinstance(state, Mapping) and state.get("_cli_owner_expected") is False:
                    return True
                return False
        stopped = self._terminate_owned_process(process)
        if stopped:
            with self._lock:
                self._cancel_owner_stopped.add(job_id)
        return stopped

    @staticmethod
    def _terminate_owned_process(process: subprocess.Popen[str]) -> bool:
        if process.poll() is not None:
            return True
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=_CANCEL_GRACE_SECONDS,
                    check=False,
                )
                try:
                    process.wait(timeout=_CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=_CANCEL_GRACE_SECONDS)
            else:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=_CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=_CANCEL_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=_CANCEL_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return process.poll() is not None

    def _mark_cancel_pending(
        self,
        job_id: str,
        *,
        code: str = "process_stop_unconfirmed",
        message: str = "已请求停止并正在确认原进程停止；已保存报告可只读查看，暂不能续跑。",
    ) -> None:
        with self._lock:
            try:
                current = self._load_persisted_state_locked(job_id)
            except AnnualAnalysisJobError:
                return
            if current.get("status") in {"completed", "completed_with_issues", "failed", "cancelled"}:
                return
            current.update(
                stage="cancellation_requested",
                error={"code": code, "message": message},
            )
            current["_recovery_owner_pending"] = True
            current["_recovery_owner_required"] = _runner_may_be_active(
                current,
                self._processes.get(job_id),
            )
            if current["_recovery_owner_required"]:
                current["_recovery_terminal_target"] = "cancelled"
                current.pop("_recovery_failure_kind", None)
            self._persist_state_locked(job_id, current)

    def _finish_cli_failure_after_stop(
        self,
        job_id: str,
        run_id: str | None,
        *,
        gate_database_path: Path | None = None,
        failure_kind: str,
        code: str,
        message: str,
    ) -> None:
        """Close admission and prove the owned CLI stopped before sealing failure."""

        with self._lock:
            try:
                current = self._load_persisted_state_locked(job_id)
            except AnnualAnalysisJobError:
                return
            gate_value = current.get("_call_gate_db")
            if not isinstance(gate_value, str) and gate_database_path is not None:
                gate_value = str(gate_database_path)
            process = self._processes.get(job_id)

        gate_closed = False
        if isinstance(gate_value, str):
            try:
                self._close_job_gate(job_id, Path(gate_value))
                gate_closed = True
            except Exception:
                gate_closed = False
        try:
            self._write_cancel_marker(job_id)
            cancel_written = True
        except (OSError, AnnualAnalysisJobError):
            cancel_written = False

        stop_confirmed = self._stop_owned_process(job_id, process)
        if not gate_closed:
            self._defer_terminal_after_gate_close_failure(
                job_id,
                run_id,
                Path(gate_value) if isinstance(gate_value, str) else None,
                terminal_target="failed",
                owner_required=not stop_confirmed,
                terminal_error={"code": code, "message": message},
                failure_kind=failure_kind,
            )
            return
        if stop_confirmed and gate_closed and cancel_written:
            self._append_terminal_checkpoint(run_id, "failed")
            self._finish_failure(job_id, code, message, run_id=run_id)
            return

        # A sealed final report wins the completion race, but remains
        # non-resumable until both the owner and its admission boundary settle.
        if isinstance(run_id, str):
            self._preserve_sealed_result(job_id, run_id)
        with self._lock:
            try:
                current = self._load_persisted_state_locked(job_id)
            except AnnualAnalysisJobError:
                return
            if current.get("status") not in {"completed", "completed_with_issues"}:
                current.update(
                    status="interrupted",
                    stage="interrupted",
                    run_id=run_id,
                    result_url=f"/v1/annual-analyses/{run_id}" if isinstance(run_id, str) else None,
                )
            current["error"] = {
                "code": "process_stop_unconfirmed",
                "message": "已请求停止并正在确认原分析进程停止；已保存报告可只读查看，暂不能续跑。",
            }
            current["_recovery_owner_pending"] = True
            current["_recovery_owner_required"] = True
            current["_recovery_terminal_target"] = "failed"
            current["_recovery_failure_kind"] = failure_kind
            self._persist_state_locked(job_id, current)

    def _fail_closed_liveness(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._liveness_failed:
                return
            self._liveness_failed.add(job_id)
            try:
                state = self._load_persisted_state_locked(job_id)
            except AnnualAnalysisJobError:
                return
            if state.get("status") != "running":
                return
            self._cancel_requested.add(job_id)
            gate_value = state.get("_call_gate_db")
            process = self._processes.get(job_id)
            state["stage"] = "cancellation_requested"
            state["error"] = {
                "code": "call_liveness_unavailable",
                "message": "本地调用心跳无法安全更新；已请求停止并正在确认原进程停止，已保存报告可只读查看，暂不能续跑。",
            }
            self._persist_state_locked(job_id, state)
        if isinstance(gate_value, str):
            try:
                self._close_job_gate(job_id, Path(gate_value))
            except Exception:
                pass
        try:
            self._write_cancel_marker(job_id)
        except (OSError, AnnualAnalysisJobError):
            pass
        if self._stop_owned_process(job_id, process):
            self._finish_cancelled(job_id)
        else:
            self._mark_cancel_pending(job_id)

    def _finish_cancelled(self, job_id: str) -> None:
        with self._lock:
            current = self._load_persisted_state_locked(job_id)
            if current.get("status") in {"completed", "completed_with_issues", "failed", "cancelled"}:
                return
            gate_value = current.get("_call_gate_db")
            gate_path = Path(gate_value) if isinstance(gate_value, str) else None
            current_run_id = current.get("run_id")
            process = self._processes.get(job_id)
            runner_stopped = (
                not _runner_may_be_active(current, process)
                or (
                    isinstance(current_run_id, str)
                    and _runner_stop_confirmed(
                        self.jobs_root / job_id,
                        current,
                        current_run_id,
                        process,
                    )
                )
            )
        gate_closed, checked_gate = self._try_close_job_gate(job_id, gate_path)
        if not gate_closed:
            self._defer_terminal_after_gate_close_failure(
                job_id,
                current_run_id if isinstance(current_run_id, str) else None,
                checked_gate,
                terminal_target="cancelled",
                owner_required=not runner_stopped,
            )
            return
        with self._lock:
            current = self._load_persisted_state_locked(job_id)
            if current.get("status") in {"completed", "completed_with_issues", "failed", "cancelled"}:
                return
            if job_id in self._cancel_finalizing:
                done = self._cancel_done.setdefault(job_id, threading.Event())
                first = False
                run_id = current.get("run_id")
            else:
                self._cancel_finalizing.add(job_id)
                done = self._cancel_done.setdefault(job_id, threading.Event())
                first = True
                run_id = current.get("run_id")
        if not first:
            done.wait(timeout=10.0)
            return
        if isinstance(run_id, str) and self._preserve_sealed_result(job_id, run_id):
            with self._lock:
                self._cancel_finalizing.discard(job_id)
                done.set()
            return
        with self._lock:
            current = self._load_persisted_state_locked(job_id)
            process = self._processes.get(job_id)
            run_id = current.get("run_id")
            writer_may_be_active = _runner_may_be_active(current, process)
            stop_confirmed = job_id in self._cancel_owner_stopped or (
                isinstance(run_id, str)
                and _runner_stop_confirmed(self.jobs_root / job_id, current, run_id, process)
            )
        if writer_may_be_active and not stop_confirmed:
            if process is not None:
                stop_confirmed = self._terminate_owned_process(process)
                if stop_confirmed:
                    with self._lock:
                        self._cancel_owner_stopped.add(job_id)
            if not stop_confirmed:
                self._mark_cancel_pending(job_id)
                with self._lock:
                    self._cancel_finalizing.discard(job_id)
                    done.set()
                return
        if isinstance(run_id, str):
            self._append_terminal_checkpoint(run_id, "cancelled")
        with self._lock:
            current = self._load_persisted_state_locked(job_id)
            if current.get("status") in {"completed", "completed_with_issues", "failed"}:
                self._cancel_finalizing.discard(job_id)
                done.set()
                return
            current.update(
                status="cancelled",
                stage="cancelled",
                result_url=f"/v1/annual-analyses/{run_id}" if isinstance(run_id, str) else None,
                error=None,
            )
            current.pop("_recovery_owner_pending", None)
            current.pop("_recovery_owner_required", None)
            current.pop("_recovery_terminal_target", None)
            current.pop("_recovery_failure_kind", None)
            self._persist_state_locked(job_id, current)
            self._cancel_owner_stopped.discard(job_id)
            self._liveness_failed.discard(job_id)
            self._cancel_finalizing.discard(job_id)
            done.set()

    def _preserve_sealed_result(self, job_id: str, run_id: str) -> bool:
        try:
            archive = load_annual_report(self.project_root, run_id)
        except Exception:
            return False
        terminal = _sealed_final_archive_status(archive)
        if terminal is None:
            return False
        with self._lock:
            state = self._load_persisted_state_locked(job_id)
            if state.get("status") not in {"queued", "running", "completed", "completed_with_issues"}:
                return False
            process = self._processes.get(job_id)
            owner_may_be_active = _runner_may_be_active(state, process)
            owner_stopped = _runner_stop_confirmed(
                self.jobs_root / job_id,
                state,
                run_id,
                process,
            )
            pending = owner_may_be_active and not owner_stopped
            state.update(
                status=terminal,
                stage=terminal,
                run_id=run_id,
                result_url=f"/v1/annual-analyses/{run_id}",
                error=(
                    {
                        "code": "process_stop_unconfirmed",
                        "message": "报告已完成保存；正在确认原分析进程停止，暂不能续跑。",
                    }
                    if pending
                    else None
                ),
            )
            if pending:
                state["_recovery_owner_pending"] = True
                state["_recovery_owner_required"] = True
            else:
                state.pop("_recovery_owner_pending", None)
                state.pop("_recovery_owner_required", None)
                state.pop("_recovery_terminal_target", None)
                state.pop("_recovery_failure_kind", None)
                if owner_stopped:
                    self._cancel_owner_stopped.discard(job_id)
            self._persist_state_locked(job_id, state)
        return True

    def _append_terminal_checkpoint(self, run_id: str | None, state: str) -> None:
        if not isinstance(run_id, str):
            return
        try:
            archive = load_annual_report(self.project_root, run_id)
        except Exception:
            return
        snapshot = archive.snapshot
        if not isinstance(snapshot, Mapping) or snapshot.get("is_partial") is not True:
            return
        if snapshot.get("state") in {"completed", "completed_with_issues"}:
            return
        parent_run_id = snapshot.get("parent_run_id")
        source_sha = archive.manifest_summary.get("source_pdf_sha256")
        try:
            if snapshot.get("state") != state:
                save_annual_checkpoint(
                    self.project_root,
                    run_id,
                    {
                        "state": state,
                        "is_partial": True,
                        "parent_run_id": parent_run_id,
                        "source_sha256": source_sha,
                        "report": archive.report,
                    },
                )
            _set_running_manifest_terminal_status(self.project_root, run_id, "interrupted" if state == "cancelled" else state)
        except (AnnualCheckpointError, AnnualReportReadError, AnnualAnalysisJobError, OSError, ValueError):
            return

    def _recovery_progress_for_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        saved = state.get("recovery_progress")
        if isinstance(saved, Mapping) and saved.get("contract_version") == "fintrace_annual_recovery_v1":
            return dict(saved)
        parent_run_id = state.get("parent_run_id") if isinstance(state.get("parent_run_id"), str) else None
        return _empty_recovery_progress(parent_run_id)

    def _initial_recovery_progress(self, state: Mapping[str, Any]) -> dict[str, Any]:
        parent = self._recovery_progress_for_state(state)
        return _empty_recovery_progress(
            parent.get("parent_run_id"),
            parent_provider_admission_count=parent.get("parent_provider_admission_count", 0),
        )

    def _recovery_progress_projection(self, state: Mapping[str, Any], *, archive: Any = ...) -> dict[str, Any]:
        current = state.get("recovery_progress")
        if not isinstance(current, Mapping):
            current = _empty_recovery_progress(None)
        count = 0
        job_id = state.get("job_id")
        gate_db_value = state.get("_call_gate_db")
        if isinstance(job_id, str) and isinstance(gate_db_value, str):
            try:
                count = read_gate_admission_count(Path(gate_db_value), job_id)
            except Exception:
                raise AnnualAnalysisJobError(
                    503,
                    "recovery_ledger_unavailable",
                    "本任务的模型准入记录暂不可安全读取，未返回推测计数。",
                ) from None
        parent_count = current.get("parent_provider_admission_count", 0)
        if type(parent_count) is not int or parent_count < 0:
            raise AnnualAnalysisJobError(503, "recovery_ledger_unavailable", "父子运行准入计数记录无效。")
        now = _utc_now()
        return {
            "contract_version": "fintrace_annual_recovery_v1",
            "parent_run_id": current.get("parent_run_id"),
            "parent_provider_admission_count": parent_count,
            "current_provider_admission_count": count,
            "cumulative_provider_admission_count": parent_count + count,
            "reused_batch_count": _reused_batch_count_for_state(self.project_root, state, archive=archive),
            "updated_at": now,
        }

    def _recovery_capability_projection(
        self,
        state: Mapping[str, Any],
        availability: Mapping[str, Any],
        *, archive: Any = ..., archive_error: Exception | None = None,
    ) -> dict[str, Any]:
        status = state.get("status")
        if state.get("_recovery_owner_pending") is True:
            return {"can_resume": False, "reason": "parent_active"}
        if status in {"queued", "running"}:
            return {"can_resume": False, "reason": "parent_active"}
        if status not in {"completed_with_issues", "failed", "interrupted", "cancelled"}:
            return {"can_resume": False, "reason": "recovery_not_available"}
        if availability.get("status") != "available" or not isinstance(state.get("run_id"), str):
            return {"can_resume": False, "reason": "source_invalid" if availability.get("category") == "source_validation_failed" else "recovery_not_available"}
        try:
            if archive_error is not None:
                raise archive_error
            if archive is ...:
                archive = load_annual_report(self.project_root, str(state["run_id"]))
            if archive is None:
                return {"can_resume": False, "reason": "recovery_not_available"}
            request = state.get("request")
            if not isinstance(request, Mapping):
                return {"can_resume": False, "reason": "binding_missing"}
            normalized = validate_job_request(self.project_root, request)
            settings = resolve_model_settings(self.project_root)
            result = validate_annual_recovery_parent(
                self.project_root,
                str(state["run_id"]),
                normalized,
                settings=settings,
                archive=archive,
                require_issue_pipeline=True,
            )
        except AnnualReportReadError as exc:
            return {"can_resume": False, "reason": "source_invalid" if _is_source_validation_error(exc.code) else "recovery_not_available"}
        except (AnnualAnalysisJobError, ModelConfigError, ModelSettingsStoreError, OSError, ValueError):
            return {"can_resume": False, "reason": "binding_missing"}
        if result.get("can_resume") is True:
            with self._lock:
                parent_job_id = state.get("job_id")
                parent_run_id = state.get("run_id")
                if isinstance(parent_job_id, str) and isinstance(parent_run_id, str) and self._has_active_child_locked(parent_job_id, parent_run_id):
                    return {"can_resume": False, "reason": "already_has_active_child"}
            return {"can_resume": True, "reason": "recoverable_batches"}
        return {"can_resume": False, "reason": _public_recovery_reason(result.get("reason"))}

    def _has_active_child_locked(self, parent_job_id: str, parent_run_id: str) -> bool:
        for candidate in self._states.values():
            if (
                candidate.get("parent_job_id") == parent_job_id
                and candidate.get("parent_run_id") == parent_run_id
                and candidate.get("status") in {"queued", "running"}
            ):
                return True
        return False

    def _reserve_run_id(self) -> str:
        for _ in range(5):
            run_id = f"annual-analysis-{uuid4()}"
            run_dir = self.project_root / "artifacts" / "runs" / run_id
            report_dir = self.project_root / "artifacts" / "reports" / run_id
            if not run_dir.exists() and not report_dir.exists():
                return run_id
        raise AnnualAnalysisJobError(503, "run_id_collision", "未能分配唯一年度分析运行编号。")

    def shutdown(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            active_ids = [
                job_id for job_id, state in self._states.items()
                if state.get("status") in {"queued", "running"}
            ]
        for job_id in active_ids:
            try:
                self.cancel(job_id)
            except Exception:
                pass
        self._executor.shutdown(wait=True, cancel_futures=True)

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
                run_id = state.get("run_id")
                runner_started = _runner_may_be_active(state, None)
                safe_to_finalize = True
                gate_value = state.get("_call_gate_db")
                if isinstance(gate_value, str):
                    try:
                        self._close_job_gate(name, Path(gate_value))
                    except Exception:
                        safe_to_finalize = False
                elif runner_started:
                    safe_to_finalize = False
                try:
                    self._write_cancel_marker(name)
                except (OSError, AnnualAnalysisJobError):
                    safe_to_finalize = False
                if runner_started:
                    safe_to_finalize = safe_to_finalize and self._wait_for_prior_runner_stop(
                        entry,
                        state,
                        run_id if isinstance(run_id, str) else None,
                    )
                state.update(
                    status="interrupted",
                    stage="interrupted",
                    updated_at=_utc_now(),
                    result_url=f"/v1/annual-analyses/{run_id}" if isinstance(run_id, str) else None,
                    error={
                        "code": "service_interrupted",
                        "message": "服务在任务完成前退出；已请求停止并正在确认原进程停止，已保存报告可只读查看，暂不能续跑。",
                    },
                )
                state["_recovery_owner_pending"] = True
                state["_recovery_owner_required"] = runner_started
                _write_state(state_path, state, create=False)
                self._states[name] = state
                if safe_to_finalize:
                    self._settle_pending_recovery(name)
            except (OSError, ValueError, AnnualAnalysisJobError, json.JSONDecodeError):
                continue

    def _wait_for_prior_runner_stop(
        self,
        job_dir: Path,
        state: Mapping[str, Any],
        run_id: str | None,
    ) -> bool:
        deadline = time.monotonic() + _RECOVERY_OWNER_WAIT_SECONDS
        while True:
            if _call_stopped_marker_matches(job_dir, run_id):
                return True
            owner_status = _runner_owner_status(state.get("_runner_owner"))
            if owner_status == "stopped":
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_RECOVERY_OWNER_POLL_INTERVAL_SECONDS)

    def _settle_pending_recovery(self, job_id: str) -> None:
        with self._lock:
            try:
                state = self._load_persisted_state_locked(job_id)
            except AnnualAnalysisJobError:
                return
            if state.get("_recovery_owner_pending") is not True or job_id in self._recovery_settling:
                return
            self._recovery_settling.add(job_id)
        try:
            job_dir = _safe_existing_job_dir(self.jobs_root, job_id)
            run_id = state.get("run_id") if isinstance(state.get("run_id"), str) else None
            owner_required = state.get("_recovery_owner_required") is True
            owner_stopped = (
                job_id in self._cancel_owner_stopped
                or _call_stopped_marker_matches(job_dir, run_id)
                or _runner_owner_status(state.get("_runner_owner")) == "stopped"
            )
            if owner_required and not owner_stopped:
                return
            gate_value = state.get("_call_gate_db")
            if owner_required and not isinstance(gate_value, str):
                return
            if isinstance(gate_value, str):
                try:
                    self._close_job_gate(job_id, Path(gate_value))
                except Exception:
                    return
            if run_id is not None:
                try:
                    archive = load_annual_report(self.project_root, run_id)
                except AnnualReportReadError:
                    archive = None
                sealed_status = (
                    _sealed_final_archive_status(archive) if archive is not None else None
                )
                if sealed_status is not None:
                    final_status = sealed_status
                    error = None
                else:
                    target = state.get("_recovery_terminal_target")
                    checkpoint_state = (
                        target
                        if target in {"failed", "interrupted", "cancelled"}
                        else (
                            "failed"
                            if archive is not None and archive.manifest_summary.get("status") == "failed"
                            else "interrupted"
                        )
                    )
                    self._append_terminal_checkpoint(run_id, checkpoint_state)
                    final_status = checkpoint_state
                    if checkpoint_state == "cancelled":
                        error = None
                    elif checkpoint_state == "failed" and target == "failed":
                        stored_error = state.get("_recovery_terminal_error")
                        if (
                            isinstance(stored_error, Mapping)
                            and isinstance(stored_error.get("code"), str)
                            and isinstance(stored_error.get("message"), str)
                        ):
                            error = {
                                "code": stored_error["code"],
                                "message": stored_error["message"],
                            }
                        else:
                            error = _deferred_failure_error(state.get("_recovery_failure_kind"))
                    else:
                        error = {
                            "code": "service_interrupted",
                            "message": "服务在任务完成前退出；已确认原分析进程停止，保存的报告仍可只读查看。",
                        }
            else:
                target = state.get("_recovery_terminal_target")
                final_status = target if target in {"failed", "interrupted", "cancelled"} else "interrupted"
                if final_status == "cancelled":
                    error = None
                elif final_status == "failed" and target == "failed":
                    error = _deferred_failure_error(state.get("_recovery_failure_kind"))
                else:
                    error = {
                        "code": "service_interrupted",
                        "message": "服务在任务完成前退出；任务未启动分析进程。",
                    }
            with self._lock:
                current = self._load_persisted_state_locked(job_id)
                current.update(
                    status=final_status,
                    stage=final_status,
                    updated_at=_utc_now(),
                    error=error,
                )
                current.pop("_recovery_owner_pending", None)
                current.pop("_recovery_owner_required", None)
                current.pop("_recovery_terminal_target", None)
                current.pop("_recovery_failure_kind", None)
                current.pop("_recovery_terminal_error", None)
                self._persist_state_locked(job_id, current)
                self._cancel_owner_stopped.discard(job_id)
        finally:
            with self._lock:
                self._recovery_settling.discard(job_id)


def _runner_owner_witness(process: subprocess.Popen[str]) -> dict[str, Any] | None:
    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 0:
        return None
    token = _process_start_token_from_handle(process)
    if token is None:
        status, token = _process_start_token_for_pid(pid)
        if status != "alive":
            return None
    return {"pid": pid, "start_token": token}


def _deferred_failure_error(failure_kind: object) -> dict[str, str]:
    if failure_kind == "timeout":
        return {
            "code": "analysis_timeout",
            "message": "年度分析超过配置的运行时限，分析进程已终止；请检查 PDF 后重新提交。",
        }
    return {
        "code": "analysis_worker_failed",
        "message": "年度分析任务执行失败；请检查运行归档并重新提交。",
    }


def _runner_may_be_active(
    state: Mapping[str, Any],
    process: subprocess.Popen[str] | None,
) -> bool:
    if process is not None or isinstance(state.get("_runner_owner"), Mapping):
        return True
    if state.get("_cli_owner_expected") is True:
        return True
    if state.get("_cli_owner_expected") is False:
        return False
    # Older job records did not persist the explicit owner flag. Keep their
    # ambiguous post-cancel state fail-closed.
    return state.get("stage") in {"annual_analysis_cli", "cancellation_requested"}


def _runner_stop_confirmed(
    job_dir: Path,
    state: Mapping[str, Any],
    run_id: str,
    process: subprocess.Popen[str] | None,
) -> bool:
    if not _runner_may_be_active(state, process):
        return True
    if _call_stopped_marker_matches(job_dir, run_id):
        return True
    if process is not None:
        try:
            return process.poll() is not None
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def _runner_owner_status(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unknown"
    pid = value.get("pid")
    expected_token = value.get("start_token")
    if type(pid) is not int or pid <= 0 or not isinstance(expected_token, str) or not expected_token:
        return "unknown"
    status, current_token = _process_start_token_for_pid(pid)
    if status == "stopped":
        return "stopped"
    if status != "alive" or current_token is None:
        return "unknown"
    # A different process now using the same PID is not evidence about the
    # original owner. Require its explicit stop marker or keep recovery closed.
    return "alive" if current_token == expected_token else "unknown"


def _process_start_token_from_handle(process: subprocess.Popen[str]) -> str | None:
    if os.name != "nt":
        return None
    handle = getattr(process, "_handle", None)
    if handle is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        created = _FileTime()
        exited = _FileTime()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        if not kernel.GetProcessTimes(
            wintypes.HANDLE(int(handle)),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(_FileTime()),
            ctypes.byref(_FileTime()),
        ):
            return None
        return f"win:{created.high:08x}{created.low:08x}"
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _process_start_token_for_pid(pid: int) -> tuple[str, str | None]:
    if os.name == "nt":
        return _windows_process_start_token(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start_token(pid)
    return "unknown", None


def _windows_process_start_token(pid: int) -> tuple[str, str | None]:
    try:
        import ctypes
        from ctypes import wintypes

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error in {6, 87, 1168}:  # invalid handle/parameter or no such process
                return "stopped", None
            return "unknown", None
        try:
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return "stopped", None
            created = _FileTime()
            if not kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(_FileTime()),
                ctypes.byref(_FileTime()),
                ctypes.byref(_FileTime()),
            ):
                return "unknown", None
            return "alive", f"win:{created.high:08x}{created.low:08x}"
        finally:
            kernel.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown", None


def _linux_process_start_token(pid: int) -> tuple[str, str | None]:
    try:
        process_stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return "stopped", None
    except (OSError, UnicodeError):
        return "unknown", None
    close_paren = process_stat.rfind(")")
    if close_paren < 0:
        return "unknown", None
    fields = process_stat[close_paren + 1 :].split()
    if len(fields) <= 19:
        return "unknown", None
    if fields[0] == "Z":
        return "stopped", None
    try:
        start_ticks = int(fields[19])
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError, ValueError):
        return "unknown", None
    if start_ticks < 0 or not boot_id:
        return "unknown", None
    return "alive", f"linux:{boot_id}:{start_ticks}"


def _call_stopped_marker_matches(job_dir: Path, run_id: str | None) -> bool:
    if run_id is None or _RUN_ID.fullmatch(f"run_id={run_id}") is None:
        return False
    path = job_dir / "call-stopped"
    descriptor: int | None = None
    try:
        before = path.lstat()
        if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > 128:
            return False
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or _file_identity(before) != _file_identity(opened)
            or opened.st_size > 128
        ):
            return False
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(129)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(opened) != _file_identity(after):
            return False
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return data == (run_id + "\n").encode("utf-8")


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
    if not isinstance(mode, str) or mode not in {
        "deterministic",
        "m3_screening",
        "model_investigation",
        "integrated_analysis",
    }:
        raise AnnualAnalysisJobError(
            400,
            "invalid_mode",
            "mode 只能是 deterministic、m3_screening、model_investigation 或 integrated_analysis。",
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


def get_annual_analysis_job_service(
    project_root: Path,
    *,
    api_runtime_identity: Mapping[str, Any] | None = None,
) -> AnnualAnalysisJobService:
    """同一进程和项目根目录共用一个有界队列，避免多 app 实例重复启动。"""

    root = Path(project_root).resolve()
    key = os.path.normcase(str(root))
    with _SERVICE_REGISTRY_LOCK:
        service = _SERVICE_REGISTRY.get(key)
        if service is None:
            service = AnnualAnalysisJobService(root, api_runtime_identity=api_runtime_identity)
            _SERVICE_REGISTRY[key] = service
            atexit.register(service.shutdown)
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
    expected_runtime_fingerprint = request.get("_expected_runtime_fingerprint")
    if not isinstance(expected_runtime_fingerprint, str) or _SHA256.fullmatch(expected_runtime_fingerprint) is None:
        raise RuntimeError("年度分析 CLI 缺少有效的启动时 runtime fingerprint，未执行任务。")
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
        "--expected-runtime-fingerprint",
        expected_runtime_fingerprint,
    ]
    reserved_run_id = request.get("_reserved_run_id")
    if isinstance(reserved_run_id, str):
        command.extend(["--run-id", _safe_run_id(reserved_run_id)])
        command.extend(["--request-mode", str(request.get("_request_mode", request["mode"]))])
    parent_run_id = request.get("_resume_parent_run_id")
    if parent_run_id is not None:
        if request["mode"] not in {"model_investigation", "integrated_analysis"}:
            raise RuntimeError("确定性年度分析任务不能续跑 AI 父运行。")
        command.extend(["--resume-parent-run-id", _safe_run_id(str(parent_run_id))])
    if request["mode"] in {"m3_screening", "integrated_analysis"}:
        command.append("--with-m3-screening")
    environment = _cli_environment_without_credentials()
    internal_job_id = request.get("_internal_job_id")
    call_gate_db = request.get("_call_gate_db")
    cancel_file = request.get("_call_cancel_file")
    liveness_file = request.get("_call_liveness_file")
    stopped_file = request.get("_call_stopped_file")
    active_source_sha = request.get("_active_source_sha256")
    budget_contract = request.get("_analysis_budget_contract")
    if isinstance(internal_job_id, str) and _JOB_ID.fullmatch(internal_job_id):
        if not all(isinstance(value, str) and value for value in (call_gate_db, cancel_file, liveness_file, active_source_sha)):
            raise RuntimeError("任务模型准入控制字段无效。")
        environment.update(
            {
                "FINTRACE_CALL_GATE_DB": str(Path(call_gate_db).resolve()),
                "FINTRACE_CALL_GATE_KEY": internal_job_id,
                "FINTRACE_CALL_CANCEL_FILE": str(Path(cancel_file).resolve()),
                "FINTRACE_CALL_LIVENESS_FILE": str(Path(liveness_file).resolve()),
                "FINTRACE_ACTIVE_SOURCE_SHA256": active_source_sha,
            }
        )
        if isinstance(stopped_file, str) and stopped_file:
            environment["FINTRACE_CALL_STOPPED_FILE"] = str(Path(stopped_file).resolve())
        if isinstance(budget_contract, Mapping):
            stage_attempt_limits = budget_contract.get("stage_attempt_limits")
            if not isinstance(stage_attempt_limits, Mapping):
                raise RuntimeError("年度分析阶段准入上限无效。")
            environment["FINTRACE_CALL_GATE_TASK_LIMIT"] = str(budget_contract.get("task_effective"))
            environment["FINTRACE_CALL_GATE_STAGE_LIMITS"] = json.dumps(
                dict(stage_attempt_limits), sort_keys=True, separators=(",", ":")
            )
    if request["mode"] in {"model_investigation", "integrated_analysis"}:
        source_record = source_pdf.parent / "source.json"
        command.extend(["--with-model", "--source-record", str(source_record)])
        if isinstance(internal_job_id, str) and _JOB_ID.fullmatch(internal_job_id):
            command.extend(["--discovery-progress-job-id", internal_job_id])
        settings = resolve_model_settings(project_root)
        environment.update(
            {
                "MODEL_BASE_URL": settings.base_url,
                "MODEL_API_KEY": settings.api_key,
                "MODEL_NAME": settings.model,
            }
        )
    timeout = _configured_cli_timeout_seconds(str(request["mode"]))
    hard_deadline = request.get("_main_report_deadline_monotonic")
    if isinstance(budget_contract, Mapping):
        soft_deadline = request.get("_main_work_deadline_monotonic")
        stage_time_limits = budget_contract.get("stage_time_limits_seconds")
        if (
            not isinstance(hard_deadline, (int, float))
            or not isinstance(soft_deadline, (int, float))
            or not isinstance(stage_time_limits, Mapping)
            or budget_contract.get("schema_version") != BUDGET_CONTRACT_VERSION
        ):
            raise RuntimeError("年度分析运行时限传递无效。")
        environment.update({
            "FINTRACE_ANNUAL_MAIN_SOFT_DEADLINE_MONOTONIC": format(float(soft_deadline), ".17g"),
            "FINTRACE_ANNUAL_MAIN_HARD_DEADLINE_MONOTONIC": format(float(hard_deadline), ".17g"),
            "FINTRACE_ANNUAL_MAIN_LIMIT_SECONDS": str(budget_contract["main_report_limit_seconds"]),
            "FINTRACE_ANNUAL_STAGE_TIME_LIMITS_SECONDS": json.dumps(
                dict(stage_time_limits), sort_keys=True, separators=(",", ":")
            ),
            "FINTRACE_ANNUAL_BUDGET_CONTRACT_VERSION": BUDGET_CONTRACT_VERSION,
        })
        timeout = max(0.0, float(hard_deadline) - time.monotonic())
    register_process = request.get("_register_process")
    if callable(register_process):
        process_deadline = (
            float(hard_deadline)
            if isinstance(budget_contract, Mapping) and isinstance(hard_deadline, (int, float))
            else time.monotonic() + timeout
        )
        timeout = max(0.0, process_deadline - time.monotonic())
        if timeout <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
        register_process(process)
        while True:
            remaining = process_deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=_CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            try:
                stdout, stderr = process.communicate(timeout=min(1.0, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        return CliResult(process.returncode, stdout, stderr)

    # Keep the direct, unmanaged helper path compatible with callers that use
    # the legacy internal runner tests or invoke this private helper directly.
    completed = subprocess.run(
        command,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    return CliResult(completed.returncode, completed.stdout, completed.stderr)


def _probe_cli_runtime_identity(project_root: Path) -> dict[str, Any] | None:
    """Run a bounded no-model CLI handshake and accept only its exact identity payload."""

    script_path = Path(project_root) / "scripts" / "analyze_annual.py"
    if not script_path.is_file():
        return None
    try:
        completed = subprocess.run(
            [sys.executable, str(script_path), "--runtime-identity"],
            cwd=project_root,
            env=_cli_environment_without_credentials(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_RUNTIME_PROBE_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0 or completed.stderr.strip() or len(completed.stdout) > 8 * 1024:
            return None
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        return None
    expected_fields = {
        "kind", "role", "protocol_version", "loaded_code_sha256", "source_manifest_sha256",
        "source_file_count", "loaded_source_match", "fingerprint", "process_id",
        "process_instance_id", "process_started_at", "model_calls",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("role") != "cli"
        or payload.get("model_calls") != 0
        or not runtime_identity_is_valid(payload)
        or type(payload.get("process_id")) is not int
        or not isinstance(payload.get("process_instance_id"), str)
        or not isinstance(payload.get("process_started_at"), str)
    ):
        return None
    return payload


def _public_runtime_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "kind", "role", "protocol_version", "loaded_code_sha256", "source_manifest_sha256",
        "source_file_count", "loaded_source_match", "fingerprint", "process_id",
        "process_instance_id", "process_started_at", "model_calls",
    )
    return {field: identity.get(field) for field in fields}


def _result_availability_projection(
    project_root: Path,
    state: Mapping[str, Any],
    *, archive: Any = ..., archive_error: Exception | None = None,
) -> dict[str, Any]:
    unavailable = lambda category, message: {
        "status": "unavailable",
        "category": category,
        "message": message,
        "run_id": None,
        "result_url": None,
        "is_partial": False,
        "snapshot_state": None,
        "snapshot_sequence": 0,
    }
    run_id = state.get("run_id")
    if not isinstance(run_id, str):
        return unavailable("no_recovery_projection", None)
    try:
        if archive_error is not None:
            raise archive_error
        if archive is ...:
            archive = load_annual_report(project_root, run_id)
        if archive is None:
            return unavailable("archive_incompatible", "报告归档无法安全读取，报告不可用。")
    except AnnualReportReadError as exc:
        source_codes = {
            "source_sha256_mismatch", "source_pdf_invalid", "source_identity_mismatch",
            "archive_identity_mismatch", "report_identity_mismatch", "evidence_source_mismatch",
            "annual_discovery_evidence_mismatch", "annual_discovery_quote_mismatch",
            "narrative_evidence_source_mismatch", "source_record_hash_mismatch",
        }
        category = "source_validation_failed" if exc.code in source_codes else "archive_incompatible"
        message = (
            "原文来源或引用校验未通过，报告不可用。"
            if category == "source_validation_failed"
            else "报告归档不兼容或无法安全读取。"
        )
        return unavailable(category, message)
    except Exception:
        return unavailable("archive_incompatible", "报告归档无法安全读取，报告不可用。")
    request = state.get("request")
    if not isinstance(request, Mapping):
        return unavailable("source_validation_failed", "报告来源身份与原任务请求不一致，报告不可用。")
    summary = archive.manifest_summary
    if (
        summary.get("company_id") != request.get("company_id")
        or summary.get("report_year") != request.get("report_year")
        or summary.get("document_id") != request.get("document_id")
        or summary.get("source_pdf_sha256") != request.get("sha256")
    ):
        return unavailable("source_validation_failed", "报告来源身份与原任务请求不一致，报告不可用。")
    snapshot = archive.snapshot
    snapshot_state = snapshot.get("state") if isinstance(snapshot, Mapping) else None
    is_partial = snapshot.get("is_partial") is True if isinstance(snapshot, Mapping) else False
    sequence = snapshot.get("sequence", 0) if isinstance(snapshot, Mapping) else 0
    if type(sequence) is not int or sequence < 0:
        return unavailable("archive_incompatible", "报告快照序号无效，报告不可用。")
    job_status = state.get("status")
    if is_partial:
        return {
            "status": "available",
            "category": "partial_checkpoint_available",
            "message": "已保存的部分年报报告可读取；AI 发现仍可能未完成。",
            "run_id": run_id,
            "result_url": f"/v1/annual-analyses/{run_id}",
            "is_partial": True,
            "snapshot_state": snapshot_state,
            "snapshot_sequence": sequence,
        }
    if job_status in {"failed", "interrupted", "cancelled"}:
        return {
            "status": "available",
            "category": "archive_readable_after_failure",
            "message": "原任务仍为失败、中断或取消状态，但现有报告通过当前归档校验，可以只读查看。",
            "run_id": run_id,
            "result_url": f"/v1/annual-analyses/{run_id}",
            "is_partial": False,
            "snapshot_state": snapshot_state,
            "snapshot_sequence": sequence,
        }
    return {
        "status": "available",
        "category": "completed_report_available",
        "message": None,
        "run_id": run_id,
        "result_url": f"/v1/annual-analyses/{run_id}",
        "is_partial": False,
        "snapshot_state": snapshot_state,
        "snapshot_sequence": sequence,
    }


def _configured_cli_timeout_seconds(mode: str = "deterministic") -> int:
    model_mode = mode in {"model_investigation", "integrated_analysis"}
    default = _DEFAULT_MODEL_CLI_TIMEOUT_SECONDS if model_mode else _DEFAULT_DETERMINISTIC_CLI_TIMEOUT_SECONDS
    maximum = _MAX_MODEL_CLI_TIMEOUT_SECONDS if model_mode else _MAX_DETERMINISTIC_CLI_TIMEOUT_SECONDS
    raw_value = os.environ.get("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS")
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        raise RuntimeError(f"FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS 必须是 60 到 {maximum} 之间的整数。") from None
    if not _MIN_CLI_TIMEOUT_SECONDS <= value <= maximum:
        raise RuntimeError(f"FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS 必须是 60 到 {maximum} 之间的整数。")
    return value


def _read_discovery_progress(
    job_dir: Path,
    job_id: str,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Read only the current job's strictly bound, count-only progress record."""

    path = job_dir / "discovery-progress.json"
    expected_fields = {
        "kind", "job_id", "run_id", "company_id", "report_year", "source_document_id",
        "source_sha256", "event", "status", "batch_index", "planned_batches",
        "processed_batches", "successful_batches", "failed_batches", "total_pages",
        "total_chars", "updated_at",
    }
    expected_fields_with_partial = expected_fields | {"partial_batches"}
    try:
        parent = job_dir.resolve(strict=True)
        if path.parent.resolve(strict=True) != parent:
            return None
        before = path.lstat()
        if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > 16 * 1024:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(16 * 1024 + 1)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after) or len(raw) > 16 * 1024:
            return None
        record = json.loads(raw.decode("utf-8"))
        request = state.get("request")
        record_fields = set(record) if isinstance(record, dict) else set()
        if (
            not isinstance(record, dict)
            or record_fields != expected_fields and record_fields != expected_fields_with_partial
            or not isinstance(request, Mapping)
        ):
            return None
        if (
            record.get("kind") != "fintrace_annual_discovery_progress_v1"
            or record.get("job_id") != job_id
            or record.get("company_id") != request.get("company_id")
            or record.get("report_year") != request.get("report_year")
            or record.get("source_document_id") != request.get("document_id")
            or not isinstance(record.get("source_sha256"), str)
            or record["source_sha256"].lower() != request.get("sha256")
            or request.get("mode") not in {"model_investigation", "integrated_analysis"}
        ):
            return None
        try:
            run_id = _safe_run_id(record.get("run_id"))
        except (AnnualAnalysisJobError, TypeError):
            return None
        if record.get("run_id") != run_id:
            return None
        event = record.get("event")
        status = record.get("status")
        if not isinstance(event, str) or event not in {"planned", "batch_started", "batch_finished", "budget_exhausted"}:
            return None
        if not isinstance(status, str) or status not in {"not_called", "running", "completed", "partial", "failed", "budget_exhausted"}:
            return None
        integer_fields = (
            "batch_index", "planned_batches", "processed_batches", "successful_batches",
            "failed_batches", "total_pages", "total_chars",
        )
        counts: dict[str, int] = {}
        for field in integer_fields:
            value = record.get(field)
            if type(value) is not int or value < 0:
                return None
            counts[field] = value
        partial_batches = record.get("partial_batches", 0)
        if type(partial_batches) is not int or partial_batches < 0:
            return None
        counts["partial_batches"] = partial_batches
        if (
            counts["planned_batches"] > 128
            or counts["processed_batches"] > counts["planned_batches"]
            or counts["successful_batches"] + counts["partial_batches"] + counts["failed_batches"] != counts["processed_batches"]
            or counts["batch_index"] > counts["planned_batches"]
            or counts["total_pages"] > 100_000
            or counts["total_chars"] > 9_007_199_254_740_991
        ):
            return None
        updated_at = record.get("updated_at")
        if not isinstance(updated_at, str) or len(updated_at) > 40:
            return None
        timestamp = datetime.fromisoformat(updated_at)
        if timestamp.tzinfo is None:
            return None
        return {
            "event": event,
            "status": status,
            **counts,
            "updated_at": updated_at,
        }
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _cli_environment_without_credentials() -> dict[str, str]:
    # Only explicitly selected AI modes receive model configuration.
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
        return {"model_investigation": {
            "available": False,
            "status": "dependency_unavailable",
            "annual_discovery_supported": True,
        }}
    try:
        root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[4]
        resolve_model_settings(root)
    except (ModelConfigError, ModelSettingsStoreError):
        return {"model_investigation": {
            "available": False,
            "status": "not_configured",
            "annual_discovery_supported": True,
        }}
    except Exception:
        return {"model_investigation": {
            "available": False,
            "status": "not_configured",
            "annual_discovery_supported": True,
        }}
    return {"model_investigation": {
        "available": True,
        "status": "ready",
        "annual_discovery_supported": True,
    }}


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
    public = {
        "job_id": state.get("job_id"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "request": state.get("request"),
        "run_id": state.get("run_id"),
        "result_url": state.get("result_url"),
        "error": state.get("error"),
        "recovery_capability": state.get("recovery_capability", {"can_resume": False, "reason": "recovery_not_available"}),
    }
    for key in ("recovery", "recovery_progress"):
        if isinstance(state.get(key), Mapping):
            public[key] = dict(state[key])
    return public


def _public_job_state(project_root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    public = _public_state(state)
    budget = _analysis_budget_projection(project_root, state)
    if budget is not None:
        public["analysis_budget"] = budget
    return public


def _job_budget_contract(scope: str) -> dict[str, Any]:
    if scope not in {"new_run", "resume"}:
        raise RuntimeError("annual_budget_scope_invalid")
    owner_limit = _configured_cli_timeout_seconds("model_investigation")
    main_limit = min(MAIN_REPORT_BUDGET_SECONDS, owner_limit)
    resumed = scope == "resume"
    stage_attempt_limits = RESUME_STAGE_ATTEMPT_BUDGETS if resumed else NEW_RUN_STAGE_ATTEMPT_BUDGETS
    try:
        shared_limit, shared_remaining = read_shared_goal_budget()
    except (ModelCallAdmissionError, ModelCallLedgerReadError):
        raise RuntimeError("annual_shared_budget_unavailable") from None
    return {
        "schema_version": BUDGET_CONTRACT_VERSION,
        "scope": scope,
        "main_report_limit_seconds": main_limit,
        "work_deadline_seconds": main_limit - FINALIZATION_RESERVE_SECONDS,
        "finalization_reserve_seconds": FINALIZATION_RESERVE_SECONDS,
        "task_default_new_run": NEW_RUN_ATTEMPT_BUDGET,
        "task_default_resume": RESUME_ATTEMPT_BUDGET,
        "task_effective": RESUME_ATTEMPT_BUDGET if resumed else NEW_RUN_ATTEMPT_BUDGET,
        "shared_goal_limit": shared_limit,
        "shared_goal_remaining_at_start": shared_remaining,
        "stage_time_limits_seconds": runtime_stage_time_limits(main_limit),
        "stage_attempt_limits": dict(stage_attempt_limits),
        "global_request_limit_chars": GLOBAL_REQUEST_LIMIT_CHARS,
        "review_request_limit_chars": REVIEW_REQUEST_LIMIT_CHARS,
        "model_call_timeout_seconds": MODEL_CALL_TIMEOUT_SECONDS,
    }


def _analysis_budget_projection(
    project_root: Path,
    state: Mapping[str, Any],
    *,
    progress: Mapping[str, Any] | None = None,
    archive: Any = ...,
) -> dict[str, Any] | None:
    contract = state.get("_analysis_budget_contract")
    if not isinstance(contract, Mapping):
        return None
    scope = contract.get("scope")
    main_limit = contract.get("main_report_limit_seconds")
    work_limit = contract.get("work_deadline_seconds")
    task_limit = contract.get("task_effective")
    stage_time_limits = contract.get("stage_time_limits_seconds")
    stage_attempt_limits = contract.get("stage_attempt_limits")
    schema = contract.get("schema_version")
    review_limit_out: int | None = None
    timeout_out: float | None = None
    global_limit_out: int | None = None
    if schema == "annual_analysis_budget_v1":
        if "review_request_limit_chars" in contract or "model_call_timeout_seconds" in contract:
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。")
        try:
            expected_stages = archived_runtime_stage_time_limits_v1(main_limit)
        except (TypeError, ValueError):
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。") from None
    elif schema == "annual_analysis_budget_v2":
        try:
            expected_stages = archived_runtime_stage_time_limits_v2(main_limit)
        except (TypeError, ValueError):
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。") from None
        frozen_v2 = archived_budget_contract_v2(scope="new_run")
        review_limit = contract.get("review_request_limit_chars")
        model_timeout = contract.get("model_call_timeout_seconds")
        if (
            review_limit != frozen_v2["review_request_limit_chars"]
            or type(model_timeout) not in {int, float}
            or not math.isfinite(float(model_timeout))
            or float(model_timeout) != float(frozen_v2["model_call_timeout_seconds"])
        ):
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。")
        review_limit_out = int(frozen_v2["review_request_limit_chars"])
        timeout_out = float(frozen_v2["model_call_timeout_seconds"])
    elif schema == "annual_analysis_budget_v3":
        try:
            expected_stages = archived_runtime_stage_time_limits_v3(main_limit)
        except (TypeError, ValueError):
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "历史任务预算记录无效，未返回推测值。") from None
        frozen_v3 = archived_budget_contract_v3(scope="new_run")
        model_timeout = contract.get("model_call_timeout_seconds")
        if (
            contract.get("review_request_limit_chars") != frozen_v3["review_request_limit_chars"]
            or type(model_timeout) not in {int, float}
            or not math.isfinite(float(model_timeout))
            or float(model_timeout) != float(frozen_v3["model_call_timeout_seconds"])
        ):
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "历史任务预算记录无效，未返回推测值。")
        review_limit_out = int(frozen_v3["review_request_limit_chars"])
        timeout_out = float(frozen_v3["model_call_timeout_seconds"])
    elif schema == BUDGET_CONTRACT_VERSION:
        try:
            expected_stages = runtime_stage_time_limits(main_limit)
        except (TypeError, ValueError):
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。") from None
        review_limit = contract.get("review_request_limit_chars")
        model_timeout = contract.get("model_call_timeout_seconds")
        if (
            review_limit != REVIEW_REQUEST_LIMIT_CHARS
            or contract.get("global_request_limit_chars") != GLOBAL_REQUEST_LIMIT_CHARS
            or type(model_timeout) not in {int, float}
            or not math.isfinite(float(model_timeout))
            or float(model_timeout) != float(MODEL_CALL_TIMEOUT_SECONDS)
        ):
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。")
        review_limit_out = REVIEW_REQUEST_LIMIT_CHARS
        global_limit_out = GLOBAL_REQUEST_LIMIT_CHARS
        timeout_out = float(MODEL_CALL_TIMEOUT_SECONDS)
    else:
        raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。")
    if (
        scope not in {"new_run", "resume"}
        or type(main_limit) is not int
        or work_limit != main_limit - FINALIZATION_RESERVE_SECONDS
        or task_limit != (RESUME_ATTEMPT_BUDGET if scope == "resume" else NEW_RUN_ATTEMPT_BUDGET)
        or not isinstance(stage_time_limits, Mapping)
        or not isinstance(stage_attempt_limits, Mapping)
        or dict(stage_time_limits) != expected_stages
    ):
        raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务预算记录无效，未返回推测值。")
    shared_limit = contract.get("shared_goal_limit")
    shared_remaining = contract.get("shared_goal_remaining_at_start")
    if (
        shared_limit is not None and (type(shared_limit) is not int or shared_limit <= 0)
        or shared_remaining is not None and (type(shared_remaining) is not int or shared_remaining < 0)
        or shared_limit is None and shared_remaining is not None
        or shared_limit is not None and shared_remaining is not None and shared_remaining > shared_limit
    ):
        raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "共同预算记录无效，未返回推测值。")

    job_id = state.get("job_id")
    gate_db = state.get("_call_gate_db")
    if isinstance(job_id, str) and _JOB_ID.fullmatch(job_id) and isinstance(gate_db, str):
        try:
            counts = read_gate_stage_admission_counts(Path(gate_db), job_id)
        except Exception:
            raise AnnualAnalysisJobError(
                503, "analysis_budget_ledger_unavailable", "本任务阶段准入记录暂不可安全读取，未返回推测计数。"
            ) from None
    elif state.get("status") == "queued" or state.get("running_started_at") is None:
        counts = {key: 0 for key in (
            "annual_discovery", "annual_investigation", "annual_issue_grouping", "annual_review",
        )}
    else:
        raise AnnualAnalysisJobError(503, "analysis_budget_ledger_unavailable", "本任务缺少可核验的准入记录。")

    now = datetime.now(timezone.utc)
    created = _parse_utc_timestamp(state.get("created_at"))
    started = _parse_utc_timestamp(state.get("running_started_at"))
    ended = _parse_utc_timestamp(state.get("ended_at"))
    status = state.get("status")
    if status == "queued" or started is None:
        elapsed = 0.0
    elif status == "running":
        monotonic_start = state.get("_running_started_monotonic")
        elapsed = max(0.0, time.monotonic() - float(monotonic_start)) if isinstance(monotonic_start, (int, float)) else max(0.0, (now - started).total_seconds())
    else:
        if ended is None or started is None:
            raise AnnualAnalysisJobError(
                503,
                "analysis_budget_timestamp_unavailable",
                "任务结束时间记录缺失，未返回推测耗时。",
            )
        elapsed = max(0.0, (ended - started).total_seconds())
    if started is not None and created is not None:
        queue_seconds = max(0.0, (started - created).total_seconds())
    elif created is not None:
        queue_end = ended if status not in {"queued", "running"} and ended is not None else now
        queue_seconds = max(0.0, (queue_end - created).total_seconds())
    else:
        queue_seconds = 0.0

    stages: dict[str, dict[str, Any]] = {}
    progress_status: str | None = None
    planned_batches: int | None = None
    if isinstance(progress, Mapping):
        progress_status = str(progress.get("status"))
        if type(progress.get("planned_batches")) is int:
            planned_batches = int(progress["planned_batches"])
    if progress is None and state.get("status") == "running" and state.get("stage") == "annual_analysis_cli":
        run_id = state.get("run_id")
        if isinstance(run_id, str):
            job_dir = project_root / "artifacts" / "annual-analysis-jobs" / str(job_id)
            loaded = _read_discovery_progress(job_dir, str(job_id), state)
            if isinstance(loaded, Mapping):
                progress_status = str(loaded.get("status"))
                if type(loaded.get("planned_batches")) is int:
                    planned_batches = int(loaded["planned_batches"])

    terminal_report: Mapping[str, Any] | None = None
    run_id = state.get("run_id")
    if status in {"running", "completed", "completed_with_issues", "failed", "interrupted", "cancelled"} and isinstance(run_id, str):
        try:
            if archive is ...:
                archive = load_annual_report(project_root, run_id)
            terminal_report = archive.report if archive is not None else None
        except Exception:
            terminal_report = None

    def stage_status(name: str) -> str:
        if name == "annual_issue_grouping":
            issue_pipeline = terminal_report.get("model_issue_pipeline") if isinstance(terminal_report, Mapping) else None
            if isinstance(issue_pipeline, Mapping):
                reason = issue_pipeline.get("reason")
                if reason in {
                    "stage_deadline", "main_deadline", "workflow_deadline", "workflow_time_budget_exhausted",
                    "stage_budget_exhausted", "provider_budget_exhausted", "task_budget_exhausted",
                    "call_deadline_exhausted", "model_call_budget_exhausted",
                }:
                    return "budget_exhausted"
                return {
                    "pending": "planned",
                    "running": "running",
                    "completed": "completed",
                    "partial": "partial",
                    "failed": "partial",
                    "abstained": "partial",
                }.get(str(issue_pipeline.get("status")), "partial")
            if isinstance(terminal_report, Mapping):
                investigation = terminal_report.get("model_investigation")
                discovery = investigation.get("discovery") if isinstance(investigation, Mapping) else terminal_report.get("model_discovery")
                if not isinstance(discovery, Mapping) or discovery.get("prompt_version") not in {"annual_discovery_v4", "annual_discovery_v5"}:
                    return "reserved"
                return "partial"
            if status == "running" and counts.get(name, 0) > 0:
                return "running"
            if status == "running":
                return "planned"
        if status == "queued":
            return "not_started"
        if status == "running":
            if name == "annual_discovery" and progress_status is not None:
                return {
                    "not_called": "planned", "running": "running", "completed": "completed",
                    "partial": "partial", "failed": "partial", "budget_exhausted": "budget_exhausted",
                }.get(progress_status, "planned")
            if name == "annual_discovery" and counts.get(name, 0) > 0:
                return "running"
            return "planned"
        if not isinstance(terminal_report, Mapping):
            return "partial" if status in {"failed", "interrupted", "cancelled"} else "planned"
        investigation = terminal_report.get("model_investigation")
        discovery = investigation.get("discovery") if isinstance(investigation, Mapping) else None
        if name == "annual_discovery":
            value = discovery
        elif name == "annual_investigation":
            value = investigation
        else:
            value = investigation.get("final_review") if isinstance(investigation, Mapping) else terminal_report.get("model_review")
        if not isinstance(value, Mapping):
            return "partial"
        reason = value.get("reason")
        if reason in {
            "discovery_stage_budget_exhausted", "workflow_time_budget_exhausted",
            "provider_budget_exhausted", "stage_budget_exhausted", "task_budget_exhausted",
            "call_deadline_exhausted", "model_call_budget_exhausted",
        }:
            return "budget_exhausted"
        if value.get("status") in {"completed", "abstained"}:
            return "completed"
        if value.get("status") in {"not_called", "running"} and reason in {"awaiting_model", "annual_discovery_not_started", "model_review_not_started"}:
            return "not_started"
        return "partial"

    stage_names = (
        "annual_discovery", "annual_investigation", "annual_issue_grouping", "annual_review",
    )
    for name in stage_names:
        cap = stage_attempt_limits.get(name)
        limit = stage_time_limits.get(name)
        admitted = counts.get(name)
        if type(cap) is not int or type(limit) is not int or type(admitted) is not int:
            raise AnnualAnalysisJobError(503, "analysis_budget_invalid", "任务阶段预算记录无效，未返回推测值。")
        stages[name] = {
            "time_limit_seconds": limit,
            "attempt_limit": cap,
            "planned_attempts": planned_batches if name == "annual_discovery" else None,
            "admitted_attempts": admitted,
            "status": stage_status(name),
        }
        if name == "annual_issue_grouping" and isinstance(terminal_report, Mapping):
            issue_pipeline = terminal_report.get("model_issue_pipeline")
            timings = issue_pipeline.get("phase_timings") if isinstance(issue_pipeline, Mapping) else None
            group_elapsed = timings.get("annual_issue_grouping_elapsed_seconds") if isinstance(timings, Mapping) else None
            if type(group_elapsed) in {int, float} and math.isfinite(float(group_elapsed)) and group_elapsed >= 0:
                stages[name]["elapsed_seconds"] = round(float(group_elapsed), 3)

    provider_admissions = sum(counts.values())
    payload = {
        "schema_version": schema,
        "scope": scope,
        "main_report_limit_seconds": main_limit,
        "work_deadline_seconds": work_limit,
        "finalization_reserve_seconds": FINALIZATION_RESERVE_SECONDS,
        "elapsed_running_seconds": round(elapsed, 3),
        "queue_seconds": round(queue_seconds, 3),
        "provider_attempt_limits": {
            "task_default_new_run": NEW_RUN_ATTEMPT_BUDGET,
            "task_default_resume": RESUME_ATTEMPT_BUDGET,
            "task_effective": task_limit,
            "shared_goal_limit": shared_limit,
            "shared_goal_remaining_at_start": shared_remaining,
        },
        "provider_admission_count": provider_admissions,
        "provider_attempts_remaining": max(0, task_limit - provider_admissions),
        "stages": stages,
    }
    if review_limit_out is not None and timeout_out is not None:
        payload["review_request_limit_chars"] = review_limit_out
        payload["model_call_timeout_seconds"] = timeout_out
    if global_limit_out is not None:
        payload["global_request_limit_chars"] = global_limit_out
    return payload


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _empty_recovery_progress(
    parent_run_id: str | None,
    *,
    parent_provider_admission_count: int = 0,
) -> dict[str, Any]:
    return {
        "contract_version": "fintrace_annual_recovery_v1",
        "parent_run_id": parent_run_id,
        "parent_provider_admission_count": parent_provider_admission_count,
        "current_provider_admission_count": 0,
        "cumulative_provider_admission_count": parent_provider_admission_count,
        "reused_batch_count": 0,
        "updated_at": _utc_now(),
    }


def _public_recovery_reason(reason: Any) -> str:
    if reason in {"recovery_parent_active", "parent_active"}:
        return "parent_active"
    if reason == "already_has_active_child":
        return "already_has_active_child"
    if reason in {"recovery_binding_missing", "recovery_binding_mismatch", "recovery_binding_unavailable"}:
        return "binding_missing"
    if reason in {"recovery_source_invalid", "source_invalid", "recovery_parent_invalid"}:
        return "source_invalid"
    return "recovery_not_available"


def _recovery_reason_message(reason: str) -> str:
    return {
        "parent_active": "父年度分析任务仍在运行，暂不能续跑。",
        "already_has_active_child": "该父运行已有续跑任务正在执行。",
        "binding_missing": "父运行的模型、提示词或计划绑定无法确认，不能续跑。",
        "source_invalid": "父运行未通过当前来源与报告严格校验，不能续跑。",
        "recovery_not_available": "该报告没有通过严格校验的未完成 AI 工作，不能续跑。",
    }.get(reason, "该报告不能安全续跑。")


def _is_source_validation_error(code: str) -> bool:
    return code in {
        "source_sha256_mismatch", "source_pdf_invalid", "source_identity_mismatch",
        "source_record_hash_mismatch", "source_document_mismatch", "archive_identity_mismatch",
        "report_identity_mismatch", "evidence_source_mismatch", "annual_discovery_evidence_mismatch",
        "annual_discovery_quote_mismatch", "narrative_evidence_source_mismatch",
        "narrative_evidence_quote_mismatch",
    }


def _reused_batch_count_for_state(project_root: Path, state: Mapping[str, Any], *, archive: Any = ...) -> int:
    run_id = state.get("run_id")
    if isinstance(run_id, str):
        try:
            if archive is ...:
                archive = load_annual_report(project_root, run_id)
            if archive is None:
                raise ValueError("strict_archive_unavailable")
            discovery = archive.report.get("model_discovery")
            contract = discovery.get("execution_contract") if isinstance(discovery, Mapping) else None
            batches = contract.get("reused_batches") if isinstance(contract, Mapping) else None
            if isinstance(batches, list) and all(type(item) is int and item > 0 for item in batches):
                return len(batches)
        except Exception:
            pass
    recovery = state.get("recovery")
    reused = recovery.get("reused_batches") if isinstance(recovery, Mapping) else None
    return reused if type(reused) is int and reused >= 0 else 0


def _discovery_recovery_projection(project_root: Path, state: Mapping[str, Any], *, archive: Any = ...) -> dict[str, Any] | None:
    recovery = state.get("recovery")
    if not isinstance(recovery, Mapping):
        return None
    projection = dict(recovery)
    run_id = state.get("run_id")
    if isinstance(run_id, str):
        try:
            if archive is ...:
                archive = load_annual_report(project_root, run_id)
            if archive is None:
                raise ValueError("strict_archive_unavailable")
            discovery = archive.report.get("model_discovery")
            contract = discovery.get("execution_contract") if isinstance(discovery, Mapping) else None
            if isinstance(contract, Mapping):
                attempts = contract.get("provider_admission_count")
                batches = contract.get("reused_batches")
                if type(attempts) is int and attempts >= 0 and isinstance(batches, list):
                    projection["current_attempts"] = attempts
                    projection["reused_batches"] = len(batches)
        except Exception:
            pass
    return projection


def _touch_owned_file(path: Path, project_root: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        _refresh_owned_file_mtime(path, project_root)
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(b"owner\n")
        handle.flush()


def _refresh_owned_file_mtime(path: Path, project_root: Path) -> None:
    checked = _safe_regular_file(path, project_root)
    if os.name == "nt":
        _refresh_windows_regular_file_mtime(checked)
    else:
        _refresh_posix_regular_file_mtime(checked)


def _refresh_posix_regular_file_mtime(path: Path) -> None:
    before = path.lstat()
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        raise AnnualAnalysisJobError(409, "liveness_file_invalid", "本地调用心跳文件不是普通文件。")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            _is_reparse_point(opened)
            or not stat.S_ISREG(opened.st_mode)
            or not _same_file_object(before, opened)
        ):
            raise AnnualAnalysisJobError(409, "liveness_file_changed", "本地调用心跳文件在更新期间发生变化。")
        if os.utime not in os.supports_fd:
            raise OSError("safe_file_handle_utime_unavailable")
        os.utime(descriptor, None)
        after = path.lstat()
        if _is_reparse_point(after) or not stat.S_ISREG(after.st_mode) or not _same_file_object(opened, after):
            raise AnnualAnalysisJobError(409, "liveness_file_changed", "本地调用心跳文件在更新期间发生变化。")
    finally:
        os.close(descriptor)


def _refresh_windows_regular_file_mtime(path: Path) -> None:
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", _FileTime),
            ("last_access_time", _FileTime),
            ("last_write_time", _FileTime),
            ("volume_serial", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    kernel.SetFileTime.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    generic_read_attributes = 0x0080
    file_write_attributes = 0x0100
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse_point = 0x00200000
    backup_semantics = 0x02000000
    invalid_handle = ctypes.c_void_p(-1).value
    handle = kernel.CreateFileW(
        str(path),
        generic_read_attributes | file_write_attributes,
        share_all,
        None,
        open_existing,
        open_reparse_point | backup_semantics,
        None,
    )
    if handle is None or handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        before = path.lstat()
        information = _ByHandleFileInformation()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        file_index = (information.file_index_high << 32) | information.file_index_low
        handle_identity = (information.volume_serial, file_index)
        path_identity = (before.st_dev & 0xFFFFFFFF, before.st_ino)
        if (
            information.attributes & (_REPARSE_POINT_ATTRIBUTE | 0x00000010)
            or _is_reparse_point(before)
            or not stat.S_ISREG(before.st_mode)
            or handle_identity != path_identity
        ):
            raise AnnualAnalysisJobError(409, "liveness_file_invalid", "本地调用心跳文件不是稳定的普通文件。")

        windows_epoch_offset = 11_644_473_600
        ticks = int((time.time() + windows_epoch_offset) * 10_000_000)
        modified = _FileTime(ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF)
        if not kernel.SetFileTime(handle, None, None, ctypes.byref(modified)):
            raise ctypes.WinError(ctypes.get_last_error())

        after = path.lstat()
        if (
            _is_reparse_point(after)
            or not stat.S_ISREG(after.st_mode)
            or (after.st_dev & 0xFFFFFFFF, after.st_ino) != handle_identity
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise AnnualAnalysisJobError(409, "liveness_file_changed", "本地调用心跳文件在更新期间发生变化。")
    finally:
        kernel.CloseHandle(handle)


def _same_file_object(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=_CANCEL_GRACE_SECONDS,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=_CANCEL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _set_running_manifest_terminal_status(project_root: Path, run_id: str, status: str) -> None:
    checked = _safe_run_id(run_id)
    if status not in {"failed", "interrupted"}:
        raise AnnualAnalysisJobError(422, "manifest_status_invalid", "运行归档终态无效。")
    path = _safe_data_path(
        project_root,
        ("artifacts", "runs", checked, "manifest.json"),
        directory=False,
        missing_code="run_not_found",
    )
    manifest = _read_json_object(path, _MAX_SOURCE_RECORD_BYTES)
    if manifest.get("run_id") != checked:
        raise AnnualAnalysisJobError(409, "manifest_identity_mismatch", "运行归档身份不匹配。")
    if manifest.get("status") != "running":
        return
    manifest["status"] = status
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(f".manifest.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_now_precise() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
