"""Optional model-based annual investigation and report appendix."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from finagent.core.model_settings import ModelConfigError, ModelSettings, load_model_settings
from finagent.audit.audited_chat import annual_call_deadline
from finagent.agents.annual_review import (
    PROMPT_VERSION as ANNUAL_REVIEW_PROMPT_VERSION,
    _build_review_input,
    _load_prompt,
    not_called_model_review,
)
from finagent.core.annual_work_budget import (
    FINAL_REVIEW_BUDGET_SECONDS,
    FIXED_INVESTIGATION_BUDGET_SECONDS,
    DISCOVERY_BUDGET_SECONDS,
    runtime_stage_time_limits,
)


def run_annual_investigation_appendix(
    report: Mapping[str, Any],
    *,
    archived_pdf_path: Path,
    original_pdf_path: Path,
    source_record_path: Path,
    run_dir: Path,
    artifacts_root: Path,
    progress_callback: Any | None = None,
    request_mode: str | None = None,
    m3_screening: Mapping[str, Any] | None = None,
    resume_parent_run_id: str | None = None,
    request_identity: Mapping[str, Any] | None = None,
    checkpoint_callback: Any | None = None,
    parsed_pdf: object | None = None,
    main_soft_deadline: float | None = None,
    stage_time_limits_seconds: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Run the optional model investigation when explicitly requested.

    Configuration is read only inside this function, which the CLI calls only
    for ``--with-model``. Input or runtime failures are represented in the
    investigation artifact so they do not discard the deterministic report.
    """

    try:
        identity = _report_identity(report)
    except Exception:
        return {
            "kind": "fintrace_annual_investigation",
            "status": "failed",
            "model_called": False,
            "model_call_count": 0,
            "model_call_attempt_count": 0,
            "items": [],
            "audit_artifacts": [],
            "fraud_conclusion": None,
            "reason": "annual_report_identity_invalid",
            "final_review": {
                "status": "not_called",
                "reason": "annual_report_identity_invalid",
                "assessment": None,
                "summary": None,
                "reasons": [],
                "follow_up_items": [],
                "limitations": [],
                "model_called": False,
                "call_count": 0,
                "call_attempt_count": 0,
                "audit_artifacts": [],
                "source_identity": None,
            },
            "archive_path": _relative_or_absolute(run_dir / "investigation.json", artifacts_root),
        }
    archive_path = _relative_or_absolute(run_dir / "investigation.json", artifacts_root)
    state: dict[str, Any] = {
        "kind": "fintrace_annual_investigation",
        **identity,
        "status": "failed",
        "model_called": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "supplementary_retrieval_used": False,
        "items": [],
        "discovery": _not_called_discovery(identity, "annual_discovery_not_started"),
        "audit_artifacts": [],
        "fraud_conclusion": None,
        "archive_path": archive_path,
        "final_review": not_called_model_review(report, "model_review_not_started"),
    }

    formal_runs_root = (_repository_root() / "artifacts" / "runs").resolve()
    if run_dir.resolve().parent != formal_runs_root:
        state.update(status="failed", reason="model_requires_repository_artifacts_root")
        state["final_review"] = not_called_model_review(report, "model_requires_repository_artifacts_root")
        return state

    try:
        record, record_sha256 = _load_source_record(source_record_path)
        binding = _validate_source_binding(
            report,
            record,
            record_sha256=record_sha256,
            source_record_path=source_record_path,
            original_pdf_path=original_pdf_path,
            archived_pdf_path=archived_pdf_path,
        )
        state["source_binding"] = binding
    except _SourceBindingError as exc:
        state.update(status="failed", reason=exc.code)
        state["final_review"] = not_called_model_review(report, exc.code)
        return state
    except Exception:
        state.update(status="failed", reason="source_binding_validation_failed")
        state["final_review"] = not_called_model_review(report, "source_binding_validation_failed")
        return state

    try:
        _ensure_langgraph_available()
    except _OptionalDependencyUnavailable:
        state.update(
            status="failed",
            reason="langgraph_unavailable",
            reason_detail="年度调查图需要可选依赖 langgraph>=1.2,<1.3；本机未安装该依赖。",
        )
        state["final_review"] = not_called_model_review(report, "langgraph_unavailable")
        return state

    try:
        settings = load_model_settings()
    except ModelConfigError as exc:
        state.update(
            status="abstained",
            reason="model_configuration_unavailable",
            reason_detail=str(exc),
        )
        state["final_review"] = not_called_model_review(report, "model_configuration_unavailable")
        return state
    except Exception:
        state.update(status="failed", reason="model_configuration_load_failed")
        state["final_review"] = not_called_model_review(report, "model_configuration_load_failed")
        return state

    state["model_name"] = settings.model
    if request_mode in {"model_investigation", "integrated_analysis"} and isinstance(request_identity, Mapping):
        return _run_recoverable_appendix(
            report,
            state=state,
            identity=identity,
            binding_source_report={
                **dict(report),
                **({"m3_screening": dict(m3_screening)} if isinstance(m3_screening, Mapping) else {}),
            },
            archived_pdf_path=archived_pdf_path,
            source_record=record,
            settings=settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
            request_mode=request_mode,
            request_identity=request_identity,
            resume_parent_run_id=resume_parent_run_id,
            progress_callback=progress_callback,
            checkpoint_callback=checkpoint_callback,
            parsed_pdf=parsed_pdf,
            main_soft_deadline=main_soft_deadline,
            stage_time_limits_seconds=stage_time_limits_seconds,
        )

    before = _run_children(run_dir)
    try:
        stage_limits = dict(stage_time_limits_seconds or runtime_stage_time_limits(300))
        fixed_deadline = min(
            value for value in (
                time.monotonic() + int(stage_limits.get("annual_investigation", FIXED_INVESTIGATION_BUDGET_SECONDS)),
                main_soft_deadline,
            ) if value is not None
        )
        with annual_call_deadline(fixed_deadline):
            result = _invoke_agent(
                report,
                archived_pdf_path=archived_pdf_path,
                source_record=record,
                settings=settings,
                run_dir=run_dir,
                parsed_pdf=parsed_pdf,
                deadline=fixed_deadline,
                main_soft_deadline=main_soft_deadline,
            )
        payload = result.to_dict()
        _validate_result_identity(payload, identity)
        attempts = _nonnegative_int(payload.get("model_call_attempt_count"))
        calls = _nonnegative_int(payload.get("model_call_count"))
        payload.update(
            model_called=calls > 0,
            model_call_count=calls,
            model_call_attempt_count=attempts,
            archive_path=archive_path,
            source_binding=binding,
            model_name=settings.model,
        )
    except Exception as exc:
        audits, attempts, calls = _discover_new_audits(run_dir, before, artifacts_root)
        error_name = type(exc).__name__
        if error_name == "LangGraphUnavailableError":
            state.update(
                status="failed",
                reason="langgraph_unavailable",
                reason_detail="年度调查图需要可选依赖 langgraph>=1.2,<1.3；本机未安装该依赖。",
            )
        else:
            state.update(status="failed", reason="annual_investigation_failed")
        state.update(
            model_called=attempts > 0,
            model_call_count=calls,
            model_call_attempt_count=attempts,
            audit_artifacts=audits,
        )
        payload = state

    # Open-ended discovery is an independent pass over all extracted source
    # text. It deliberately does not depend on the fixed candidate list or M3.
    discovery_started = time.monotonic()
    discovery_deadline = min(
        value for value in (
            discovery_started + int(stage_limits.get("annual_discovery", DISCOVERY_BUDGET_SECONDS)),
            main_soft_deadline,
        ) if value is not None
    )
    with annual_call_deadline(discovery_deadline):
        discovery = _invoke_discovery(
            report,
            source_pdf_path=archived_pdf_path,
            source_record=record,
            settings=settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
            progress_callback=progress_callback,
            parsed_pdf=parsed_pdf,
            main_soft_deadline=main_soft_deadline,
            discovery_deadline=discovery_deadline,
        )
    payload["discovery"] = discovery
    payload["discovery_call_count"] = _nonnegative_int(discovery.get("model_call_count"))
    payload["discovery_call_attempt_count"] = _nonnegative_int(
        discovery.get("model_call_attempt_count")
    )
    payload["investigation_call_count"] = _nonnegative_int(payload.get("model_call_count"))
    payload["investigation_call_attempt_count"] = _nonnegative_int(
        payload.get("model_call_attempt_count")
    )
    payload["model_call_count"] = (
        _nonnegative_int(payload.get("model_call_count"))
        + payload["discovery_call_count"]
    )
    payload["model_call_attempt_count"] = (
        _nonnegative_int(payload.get("model_call_attempt_count"))
        + payload["discovery_call_attempt_count"]
    )
    payload["model_called"] = payload["model_call_attempt_count"] > 0
    existing_audits = payload.get("audit_artifacts")
    discovery_audits = discovery.get("audit_artifacts")
    payload["audit_artifacts"] = [
        *(existing_audits if isinstance(existing_audits, list) else []),
        *(discovery_audits if isinstance(discovery_audits, list) else []),
    ]

    try:
        review = _invoke_review(
            report,
            payload,
            settings=settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
        )
    except Exception:
        review = not_called_model_review(report, "model_review_call_failed")
        review["status"] = "failed"
    return _attach_review_outcome(payload, review)


class _RecoveryCheckpointError(RuntimeError):
    """A durable partial report could not be saved; stop before another call."""


class AnnualRecoveryRejectedError(RuntimeError):
    """A resume request was rejected before provider admission and durably archived."""

    def __init__(
        self,
        reason: str,
        *,
        requested_parent_run_id: str,
        investigation: Mapping[str, Any],
        snapshot_report: Mapping[str, Any],
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.requested_parent_run_id = requested_parent_run_id
        self.investigation = dict(investigation)
        self.snapshot_report = dict(snapshot_report)


def _safe_recovery_reason(value: object) -> str:
    if isinstance(value, str) and 1 <= len(value) <= 100 and all(
        char.islower() or char.isdigit() or char == "_" for char in value
    ):
        return value
    return "recovery_binding_unavailable"


def _raise_recovery_rejected_before_calls(
    report: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    identity: Mapping[str, Any],
    binding_source_report: Mapping[str, Any],
    archived_pdf_path: Path,
    source_record: Mapping[str, Any],
    settings: ModelSettings,
    run_dir: Path,
    execution_binding: Mapping[str, Any],
    parsed_pdf: object | None,
    requested_parent_run_id: str,
    reason: str,
    checkpoint_callback: Any | None,
) -> None:
    """Persist a child-bound zero-call projection before rejecting its parent."""

    discovery_module = importlib.import_module("finagent.agents.annual_discovery")
    discovery = discovery_module.build_annual_discovery_initial_snapshot(
        report,
        archived_pdf_path,
        source_record,
        settings,
        run_dir=run_dir,
        execution_binding=execution_binding,
        parsed_pdf=parsed_pdf,
    )
    archive_path = state.get("archive_path")
    investigation = {
        "kind": "fintrace_annual_investigation",
        **dict(identity),
        "status": "failed",
        "reason": reason,
        "model_called": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "discovery": discovery,
        "discovery_call_count": 0,
        "discovery_call_attempt_count": 0,
        "investigation_call_count": 0,
        "investigation_call_attempt_count": 0,
        "model_review_call_count": 0,
        "model_review_call_attempt_count": 0,
        "items": [],
        "narrative_evidences": [],
        "audit_artifacts": [],
        "fraud_conclusion": None,
        "limitations": ["父运行绑定校验未通过；本次未发起模型调用。"],
        "archive_path": archive_path,
        "source_binding": state.get("source_binding"),
        "model_name": settings.model,
        "final_review": not_called_model_review(report, reason),
    }
    snapshot_report, _snapshot_analysis = attach_investigation_to_report(
        binding_source_report,
        {},
        investigation,
        investigation_path=str(archive_path or "investigation.json"),
    )
    if checkpoint_callback is not None:
        try:
            # The unverified requested parent is recorded only in failure.json;
            # this v3 execution has no accepted parent and inherits no data.
            checkpoint_callback(snapshot_report, "failed", None)
        except Exception:
            raise _RecoveryCheckpointError("annual_checkpoint_persist_failed") from None
    raise AnnualRecoveryRejectedError(
        reason,
        requested_parent_run_id=requested_parent_run_id,
        investigation=investigation,
        snapshot_report=snapshot_report,
    )


_RETRYABLE_PARENT_ITEM_REASONS = frozenset({
    "model_call_failed", "invalid_model_json", "invalid_model_response",
    "prompt_build_failed", "model_call_budget_exhausted",
    "model_call_budget_exhausted_before_supplement", "supplementary_retrieval_failed",
    "supplementary_retrieval_exhausted", "no_valid_same_source_citation",
    "http_401", "http_403", "ledger_config_invalid",
    "ledger_source_context_missing_or_unapproved", "ledger_unavailable",
    "ledger_provider_mismatch", "ledger_budget_exhausted", "ledger_policy_mismatch",
    "call_gate_closed", "audit_write_failed", "cancelled",
})

_INVESTIGATION_FATAL_STOP_REASONS = frozenset({
    "http_401", "http_403", "ledger_config_invalid",
    "ledger_source_context_missing_or_unapproved", "ledger_unavailable",
    "ledger_provider_mismatch", "ledger_budget_exhausted", "task_budget_exhausted", "ledger_policy_mismatch",
    "call_gate_closed", "audit_write_failed", "cancelled",
})


def _run_recoverable_appendix(
    report: Mapping[str, Any],
    *,
    state: dict[str, Any],
    identity: Mapping[str, Any],
    binding_source_report: Mapping[str, Any],
    archived_pdf_path: Path,
    source_record: Mapping[str, Any],
    settings: ModelSettings,
    run_dir: Path,
    artifacts_root: Path,
    request_mode: str,
    request_identity: Mapping[str, Any],
    resume_parent_run_id: str | None,
    progress_callback: Any | None,
    checkpoint_callback: Any | None,
    parsed_pdf: object | None = None,
    main_soft_deadline: float | None = None,
    stage_time_limits_seconds: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Execute discovery first so every model stage starts after a v3 checkpoint."""

    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.annual_recovery import (
        build_annual_recovery_binding,
        validate_annual_recovery_parent,
    )

    if request_identity.get("mode") != request_mode:
        raise ValueError("annual_recovery_request_mode_mismatch")
    binding = build_annual_recovery_binding(
        _repository_root(),
        request_identity,
        settings=settings,
        report=binding_source_report,
        parsed_pdf=parsed_pdf,
    )
    parent_archive = None
    parent_report: Mapping[str, Any] | None = None
    parent_discovery: Mapping[str, Any] | None = None
    parent_investigation: Mapping[str, Any] | None = None
    parent_count = 0
    if resume_parent_run_id is not None:
        rejection_reason: str | None = None
        try:
            parent_archive = load_annual_report(_repository_root(), resume_parent_run_id)
            capability = validate_annual_recovery_parent(
                _repository_root(),
                resume_parent_run_id,
                request_identity,
                settings=settings,
                archive=parent_archive,
                parsed_pdf=parsed_pdf,
            )
            if not capability.get("can_resume"):
                rejection_reason = _safe_recovery_reason(
                    capability.get("reason") or "annual_recovery_parent_rejected"
                )
            elif capability.get("binding") != binding:
                rejection_reason = "annual_recovery_binding_mismatch"
            else:
                parent_report = parent_archive.report
                raw_discovery = parent_report.get("model_discovery")
                raw_investigation = parent_report.get("model_investigation")
                contract = raw_discovery.get("execution_contract") if isinstance(raw_discovery, Mapping) else None
                if not isinstance(raw_discovery, Mapping) or not isinstance(contract, Mapping):
                    rejection_reason = "annual_recovery_parent_binding_missing"
                else:
                    candidate_count = contract.get("cumulative_provider_admission_count", -1)
                    if type(candidate_count) is not int or candidate_count < 0:
                        rejection_reason = "annual_recovery_parent_count_invalid"
                    else:
                        parent_discovery = raw_discovery
                        parent_investigation = raw_investigation if isinstance(raw_investigation, Mapping) else None
                        parent_count = candidate_count
        except Exception as exc:
            # Parent reading and semantic binding checks happen before any
            # provider call. Preserve a typed reader code when available, but
            # never put paths or exception text into the report contract.
            rejection_reason = _safe_recovery_reason(
                getattr(exc, "code", None) or "recovery_binding_unavailable"
            )
        if rejection_reason is not None:
            _raise_recovery_rejected_before_calls(
                report,
                state=state,
                identity=identity,
                binding_source_report=binding_source_report,
                archived_pdf_path=archived_pdf_path,
                source_record=source_record,
                settings=settings,
                run_dir=run_dir,
                execution_binding=binding,
                parsed_pdf=parsed_pdf,
                requested_parent_run_id=resume_parent_run_id,
                reason=rejection_reason,
                checkpoint_callback=checkpoint_callback,
            )

    latest_discovery: dict[str, Any] | None = None
    latest_investigation: dict[str, Any] | None = None

    def compose_payload(
        fixed_value: Mapping[str, Any] | None = None,
        review_value: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        fixed = dict(fixed_value if isinstance(fixed_value, Mapping) else latest_investigation or {})
        discovery_value = dict(latest_discovery or {})
        discovery_calls = _nonnegative_int(discovery_value.get("model_call_count"))
        discovery_attempts = _nonnegative_int(discovery_value.get("model_call_attempt_count"))
        fixed_calls = _nonnegative_int(
            fixed.get("investigation_call_count", fixed.get("model_call_count", 0))
        )
        fixed_attempts = _nonnegative_int(
            fixed.get("investigation_call_attempt_count", fixed.get("model_call_attempt_count", 0))
        )
        final_review = (
            dict(review_value)
            if isinstance(review_value, Mapping)
            else dict(fixed.get("final_review"))
            if isinstance(fixed.get("final_review"), Mapping)
            else not_called_model_review(report, "model_review_not_started")
        )
        review_calls = _nonnegative_int(final_review.get("call_count"))
        review_attempts = _nonnegative_int(final_review.get("call_attempt_count"))
        raw_audits = [
            *(discovery_value.get("audit_artifacts", []) if isinstance(discovery_value.get("audit_artifacts"), list) else []),
            *(fixed.get("audit_artifacts", []) if isinstance(fixed.get("audit_artifacts"), list) else []),
            *(final_review.get("audit_artifacts", []) if isinstance(final_review.get("audit_artifacts"), list) else []),
        ]
        audits: list[object] = []
        audit_keys: set[tuple[object, object]] = set()
        for item in raw_audits:
            if not isinstance(item, Mapping):
                continue
            key = (item.get("audit_dir"), item.get("request_path"))
            if key in audit_keys:
                continue
            audit_keys.add(key)
            audits.append(dict(item))
        fixed.update(
            kind="fintrace_annual_investigation",
            **dict(identity),
            status=fixed.get("status", "not_called"),
            model_called=(discovery_attempts + fixed_attempts + review_attempts) > 0,
            discovery=discovery_value,
            discovery_call_count=discovery_calls,
            discovery_call_attempt_count=discovery_attempts,
            investigation_call_count=fixed_calls,
            investigation_call_attempt_count=fixed_attempts,
            model_review_call_count=review_calls,
            model_review_call_attempt_count=review_attempts,
            model_call_count=discovery_calls + fixed_calls + review_calls,
            model_call_attempt_count=discovery_attempts + fixed_attempts + review_attempts,
            audit_artifacts=audits,
            final_review=final_review,
        )
        return fixed

    def publish(
        discovery: Mapping[str, Any],
        investigation: Mapping[str, Any] | None = None,
        review: Mapping[str, Any] | None = None,
    ) -> None:
        nonlocal latest_discovery, latest_investigation
        latest_discovery = dict(discovery)
        if investigation is not None:
            latest_investigation = dict(investigation)
        full_payload = compose_payload(latest_investigation, review)
        snapshot_report, _snapshot_analysis = attach_investigation_to_report(
            binding_source_report,
            {},
            full_payload,
            investigation_path=str(full_payload.get("archive_path") or state["archive_path"]),
        )
        if checkpoint_callback is not None:
            try:
                checkpoint_callback(
                    snapshot_report,
                    "running",
                    resume_parent_run_id,
                )
            except Exception:
                raise _RecoveryCheckpointError("annual_checkpoint_persist_failed") from None

    reusable_items = _reusable_parent_items(parent_investigation)

    def discovery_checkpoint(value: Mapping[str, object]) -> None:
        publish(value)

    before = _run_children(run_dir)
    stage_limits = dict(stage_time_limits_seconds or runtime_stage_time_limits(300))
    discovery_started = time.monotonic()
    discovery_deadline = min(
        value for value in (
            discovery_started + int(stage_limits.get("annual_discovery", DISCOVERY_BUDGET_SECONDS)),
            main_soft_deadline,
        ) if value is not None
    )
    discovery = _invoke_discovery(
        report,
        source_pdf_path=archived_pdf_path,
        source_record=source_record,
        settings=settings,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        progress_callback=progress_callback,
        execution_binding=binding,
        parent_discovery=parent_discovery,
        parent_provider_admission_count=parent_count,
        checkpoint_callback=discovery_checkpoint,
        cancel_check=_call_control_requested,
        strict_v3=True,
        parsed_pdf=parsed_pdf,
        main_soft_deadline=main_soft_deadline,
        discovery_deadline=discovery_deadline,
    )
    # A workflow error or failed checkpoint write is fatal for the remainder
    # of this CLI process. The most recent immutable pointer remains readable.
    _validate_discovery_identity(discovery, identity)
    # Seal the returned stage projection even if a compatible runner omitted
    # a final callback. This also makes the exact value used by later stages
    # the current durable checkpoint.
    publish(discovery)

    if _call_control_requested():
        payload = _cancelled_investigation_payload(
            identity,
            discovery,
            reusable_items,
            parent_investigation,
            archive_path=state.get("archive_path"),
            source_binding=state.get("source_binding"),
            model_name=settings.model,
            reason="user_cancelled",
        )
        review = not_called_model_review(report, "user_cancelled")
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    if main_soft_deadline is not None and time.monotonic() >= main_soft_deadline:
        reason = "workflow_time_budget_exhausted"
        payload = _cancelled_investigation_payload(
            identity, discovery, reusable_items, parent_investigation,
            archive_path=state.get("archive_path"),
            source_binding=state.get("source_binding"),
            model_name=settings.model,
            reason=reason,
        )
        review = not_called_model_review(report, reason)
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    stop_reason = _discovery_stop_reason(discovery)
    if stop_reason is not None:
        payload = _cancelled_investigation_payload(
            identity,
            discovery,
            reusable_items,
            parent_investigation,
            archive_path=state.get("archive_path"),
            source_binding=state.get("source_binding"),
            model_name=settings.model,
            reason=stop_reason,
        )
        review = not_called_model_review(report, stop_reason)
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    fixed_before = _run_children(run_dir)
    fixed_started = time.monotonic()
    fixed_deadline = min(
        value for value in (
            fixed_started + int(stage_limits.get("annual_investigation", FIXED_INVESTIGATION_BUDGET_SECONDS)),
            main_soft_deadline,
        ) if value is not None
    )
    try:
        agent_result = _invoke_agent(
            report,
            archived_pdf_path=archived_pdf_path,
            source_record=source_record,
            settings=settings,
            run_dir=run_dir,
            reusable_items=reusable_items,
            checkpoint_callback=lambda partial: publish(discovery, partial),
            parsed_pdf=parsed_pdf,
            deadline=fixed_deadline,
            main_soft_deadline=main_soft_deadline,
        )
        payload = agent_result.to_dict()
        _validate_result_identity(payload, identity)
        fixed_attempts = _nonnegative_int(payload.get("model_call_attempt_count"))
        fixed_calls = _nonnegative_int(payload.get("model_call_count"))
        payload.update(
            model_called=fixed_attempts > 0,
            model_call_count=fixed_calls,
            model_call_attempt_count=fixed_attempts,
            investigation_call_count=fixed_calls,
            investigation_call_attempt_count=fixed_attempts,
            archive_path=state["archive_path"],
            source_binding=state.get("source_binding"),
            model_name=settings.model,
        )
    except _RecoveryCheckpointError:
        raise
    except Exception as exc:
        audits, attempts, calls = _discover_new_audits(run_dir, fixed_before, artifacts_root)
        payload = dict(latest_investigation or state)
        payload.update(
            status="failed",
            reason="annual_investigation_failed",
            model_called=attempts > 0,
            model_call_count=calls,
            model_call_attempt_count=attempts,
            investigation_call_count=calls,
            investigation_call_attempt_count=attempts,
            audit_artifacts=audits,
            archive_path=state["archive_path"],
            source_binding=state.get("source_binding"),
            model_name=settings.model,
        )
        if type(getattr(exc, "provider_admission_count", None)) is int:
            payload["model_call_attempt_count"] = getattr(exc, "provider_admission_count")
            payload["investigation_call_attempt_count"] = getattr(exc, "provider_admission_count")
        if type(getattr(exc, "response_count", None)) is int:
            payload["model_call_count"] = getattr(exc, "response_count")
            payload["investigation_call_count"] = getattr(exc, "response_count")

    publish(discovery, payload)
    payload = compose_payload(payload)
    if _call_control_requested():
        review = not_called_model_review(report, "user_cancelled")
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    if main_soft_deadline is not None and time.monotonic() >= main_soft_deadline:
        review = not_called_model_review(report, "workflow_time_budget_exhausted")
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    fixed_stop_reason = _investigation_stop_reason(payload)
    if fixed_stop_reason is not None:
        review = not_called_model_review(report, fixed_stop_reason)
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    reused_review = _reuse_parent_review(
        parent_report,
        report,
        parent_investigation,
        payload,
        parent_discovery,
        discovery,
        current_model_name=settings.model,
    )
    if reused_review is not None:
        review = reused_review
    else:
        try:
            review_started = time.monotonic()
            review_deadline = min(
                value for value in (
                    review_started + int(stage_limits.get("annual_review", FINAL_REVIEW_BUDGET_SECONDS)),
                    main_soft_deadline,
                ) if value is not None
            )
            review = _invoke_review(
                report,
                payload,
                settings=settings,
                run_dir=run_dir,
                artifacts_root=artifacts_root,
                deadline=review_deadline,
                main_soft_deadline=main_soft_deadline,
            )
        except Exception:
            review = not_called_model_review(report, "model_review_call_failed")
            review["status"] = "failed"
    result = compose_payload(payload, review)
    publish(discovery, result, review)
    return result


def _reusable_parent_items(value: Mapping[str, Any] | None) -> list[Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return []
    raw = value.get("items")
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if item.get("status") == "interpretation":
            items.append(item)
        elif (
            item.get("status") == "abstained"
            and isinstance(item.get("reason"), str)
            and item.get("reason") not in _RETRYABLE_PARENT_ITEM_REASONS
        ):
            items.append(item)
    return items


def _cancelled_investigation_payload(
    identity: Mapping[str, Any],
    discovery: Mapping[str, Any],
    reusable_items: list[Mapping[str, object]],
    parent_investigation: Mapping[str, Any] | None,
    *,
    archive_path: object = None,
    source_binding: object = None,
    model_name: str | None = None,
    reason: str = "user_cancelled",
) -> dict[str, Any]:
    parent_evidences = (
        parent_investigation.get("narrative_evidences", [])
        if isinstance(parent_investigation, Mapping)
        else []
    )
    items = [dict(item) for item in reusable_items]
    discovery_calls = _nonnegative_int(discovery.get("model_call_count"))
    discovery_attempts = _nonnegative_int(discovery.get("model_call_attempt_count"))
    return {
        "kind": "fintrace_annual_investigation",
        **dict(identity),
        "status": "abstained",
        "reason": reason,
        "model_called": discovery_attempts > 0,
        "model_call_count": discovery_calls,
        "model_call_attempt_count": discovery_attempts,
        "investigation_call_count": 0,
        "investigation_call_attempt_count": 0,
        "discovery_call_count": discovery_calls,
        "discovery_call_attempt_count": discovery_attempts,
        "discovery": dict(discovery),
        "items": items,
        "narrative_evidences": [dict(item) for item in parent_evidences if isinstance(item, Mapping)] if isinstance(parent_evidences, list) else [],
        "audit_artifacts": list(discovery.get("audit_artifacts", [])),
        "archive_path": archive_path,
        "source_binding": source_binding,
        "model_name": model_name,
        "final_review": None,
    }


def _discovery_stop_reason(discovery: Mapping[str, Any]) -> str | None:
    top_level_reason = discovery.get("reason")
    if top_level_reason in {
        "user_cancelled", "authentication_failure_stopped", "provider_budget_exhausted",
        "provider_admission_rejected", "consecutive_transport_failures_stopped",
        "parent_call_gate_closed", "workflow_time_budget_exhausted",
    }:
        return str(top_level_reason)
    contract = discovery.get("execution_contract")
    batches = contract.get("batches") if isinstance(contract, Mapping) else None
    if not isinstance(batches, list):
        return None
    pending_reasons = {
        row.get("reason") for row in batches
        if isinstance(row, Mapping) and row.get("state") == "unattempted"
    }
    reason_order = (
        "user_cancelled",
        "authentication_failure_stopped",
        "provider_budget_exhausted",
        "provider_admission_rejected",
        "consecutive_transport_failures_stopped",
        "parent_call_gate_closed",
    )
    for reason in reason_order:
        if reason in pending_reasons:
            return reason

    for batch in batches:
        if not isinstance(batch, Mapping) or not isinstance(batch.get("attempts"), list):
            continue
        for attempt in batch["attempts"]:
            if not isinstance(attempt, Mapping):
                continue
            reason = attempt.get("reason")
            if reason in {"http_401", "http_403"}:
                return "authentication_failure_stopped"
            if reason in {"ledger_budget_exhausted", "task_budget_exhausted", "call_gate_closed"}:
                return "provider_budget_exhausted"
            if reason in {
                "ledger_provider_mismatch", "ledger_source_context_missing_or_unapproved",
                "ledger_unavailable", "ledger_policy_mismatch", "audit_write_failed",
            }:
                return "provider_admission_rejected"
    return None


def _investigation_stop_reason(investigation: Mapping[str, Any]) -> str | None:
    reason = investigation.get("reason")
    if isinstance(reason, str) and reason in _INVESTIGATION_FATAL_STOP_REASONS:
        return reason
    items = investigation.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_reason = item.get("reason")
        if isinstance(item_reason, str) and item_reason in _INVESTIGATION_FATAL_STOP_REASONS:
            return item_reason
    return None


def _reuse_parent_review(
    parent_report: Mapping[str, Any] | None,
    current_report: Mapping[str, Any],
    parent_investigation: Mapping[str, Any] | None,
    current_investigation: Mapping[str, Any],
    parent_discovery: Mapping[str, Any] | None,
    current_discovery: Mapping[str, Any],
    *,
    current_model_name: str,
) -> dict[str, Any] | None:
    if not isinstance(parent_report, Mapping) or not isinstance(parent_investigation, Mapping):
        return None
    review = parent_report.get("model_review")
    if not isinstance(review, Mapping) or review.get("status") != "completed":
        return None
    if not isinstance(parent_discovery, Mapping):
        return None
    parent_contract = parent_discovery.get("execution_contract")
    current_contract = current_discovery.get("execution_contract")
    parent_binding = parent_contract.get("binding") if isinstance(parent_contract, Mapping) else None
    current_binding = current_contract.get("binding") if isinstance(current_contract, Mapping) else None
    if (
        not isinstance(parent_binding, Mapping)
        or not isinstance(current_binding, Mapping)
        or dict(parent_binding) != dict(current_binding)
        or parent_binding.get("model") != current_model_name
        or parent_investigation.get("model_name") != current_model_name
        or current_investigation.get("model_name") != current_model_name
    ):
        return None
    review_audits = review.get("audit_artifacts")
    if not isinstance(review_audits, list) or not any(
        isinstance(item, Mapping)
        and item.get("status") == "succeeded"
        and item.get("prompt_version") == ANNUAL_REVIEW_PROMPT_VERSION
        and isinstance(item.get("request_path"), str)
        and isinstance(item.get("response_path"), str)
        for item in review_audits
    ):
        return None
    try:
        parent_identity = _report_identity(parent_report)
        if review.get("source_identity") != parent_identity:
            return None
        parent_signature = _review_reuse_signature(
            parent_report,
            parent_investigation,
            parent_identity,
            model_name=current_model_name,
        )
        current_identity = _report_identity(current_report)
        current_signature = _review_reuse_signature(
            current_report,
            current_investigation,
            current_identity,
            model_name=current_model_name,
        )
    except (OSError, TypeError, ValueError, KeyError):
        return None
    if parent_signature != current_signature:
        return None
    rebound = dict(review)
    rebound.update(
        reason="review_reused_for_matching_inputs",
        model_called=False,
        call_count=0,
        call_attempt_count=0,
        source_identity=not_called_model_review(current_report, "review_reused_for_matching_inputs")["source_identity"],
    )
    return rebound


def _review_reuse_signature(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    model_name: str,
) -> str:
    """Fingerprint the exact semantic review request while rebinding its run ID.

    The annual review builder is the authoritative whitelist for what the model
    sees. Its output retains verified values, units, periods, scope, citation
    pages, verification eligibility, discovery coverage/findings, and review
    items, but deliberately omits volatile verification timestamps. Only the
    run ID in the explicit source identity is removed because a child run has a
    different ID while referring to the same strictly bound source.
    """

    material = _review_reuse_request_material(
        report,
        investigation,
        identity,
        model_name=model_name,
    )
    semantic_input = json.loads(material["user_content"])
    source_identity = semantic_input.get("source_identity")
    if not isinstance(source_identity, dict):
        raise ValueError("annual_review_identity_missing")
    source_identity.pop("run_id", None)
    material = {
        "prompt_version": ANNUAL_REVIEW_PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(material["system_prompt"].encode("utf-8")).hexdigest(),
        "model": model_name,
        "input": semantic_input,
        "allowed_ids": sorted(material["allowed_ids"]),
        "evidence_refs": material["evidence_refs"],
        "no_priority_allowed": material["no_priority_allowed"],
    }
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _review_reuse_request_material(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    model_name: str,
) -> dict[str, Any]:
    """Return the canonical annual-review request parts for reuse and audit checks."""

    system_prompt = _load_prompt()
    review_input, allowed_ids, evidence_refs, no_priority_allowed = _build_review_input(
        report,
        investigation,
        identity,
        request_model=model_name,
        system_prompt=system_prompt,
    )
    return {
        "system_prompt": system_prompt,
        "user_content": json.dumps(review_input, ensure_ascii=False, separators=(",", ":")),
        "allowed_ids": allowed_ids,
        "evidence_refs": [
            {"document_id": item.document_id, "page": item.page}
            for item in evidence_refs
        ],
        "no_priority_allowed": no_priority_allowed,
    }


def _call_control_requested() -> bool:
    cancel_path = os.environ.get("FINTRACE_CALL_CANCEL_FILE")
    if cancel_path:
        try:
            if Path(cancel_path).exists():
                return True
        except OSError:
            return True
    liveness_path = os.environ.get("FINTRACE_CALL_LIVENESS_FILE")
    if liveness_path:
        try:
            path = Path(liveness_path)
            if not path.is_file() or time.time() - path.stat().st_mtime > 5.0:
                return True
        except OSError:
            return True
    return False


def _invoke_discovery(
    report: Mapping[str, Any],
    *,
    source_pdf_path: Path,
    source_record: Mapping[str, Any],
    settings: ModelSettings,
    run_dir: Path,
    artifacts_root: Path,
    progress_callback: Any | None = None,
    execution_binding: Mapping[str, Any] | None = None,
    parent_discovery: Mapping[str, Any] | None = None,
    parent_provider_admission_count: int = 0,
    checkpoint_callback: Any | None = None,
    cancel_check: Any | None = None,
    strict_v3: bool = False,
    parsed_pdf: object | None = None,
    main_soft_deadline: float | None = None,
    discovery_deadline: float | None = None,
) -> dict[str, Any]:
    identity = _report_identity(report)
    before = _run_children(run_dir)
    try:
        agent = importlib.import_module("finagent.agents.annual_discovery")
        result = agent.run_annual_discovery(
            report,
            source_pdf_path,
            source_record,
            settings,
            run_dir=run_dir,
            progress_callback=progress_callback,
            execution_binding=execution_binding,
            parent_discovery=parent_discovery,
            parent_provider_admission_count=parent_provider_admission_count,
            checkpoint_callback=checkpoint_callback,
            cancel_check=cancel_check,
            parsed_pdf=parsed_pdf,
            main_soft_deadline=main_soft_deadline,
            discovery_deadline=discovery_deadline,
        )
        payload = result.to_dict()
        _validate_discovery_identity(payload, identity)
        return payload
    except Exception:
        if strict_v3:
            # A v3 workflow must not turn checkpoint failures or malformed
            # execution contracts into legacy-shaped success or not-called
            # records. The previously published immutable snapshot remains
            # the only recoverable state.
            raise
        audits, attempts, responses, unmapped = _discover_new_discovery_audits(
            run_dir, before, artifacts_root, identity
        )
        status = "failed" if attempts else "not_called"
        result = _not_called_discovery(
            identity,
            "annual_discovery_failed",
            status=status,
        )
        result["audit_artifacts"] = audits
        result["model_call_attempt_count"] = attempts
        result["model_call_count"] = responses
        result["model_called"] = attempts > 0
        # Audit responses may have existed before an unexpected local failure,
        # but do not prove strict parsing/acceptance or the planned batch total.
        # Keep the full scan unavailable instead of synthesizing success rows.
        result["coverage"]["source_status"] = "unavailable"
        result["coverage"]["content_status"] = "unavailable"
        result["limitations"] = [
            "开放式发现流程发生内部错误；已从安全审计目录恢复实际请求与响应计数。",
            "无法从失败流程恢复完整批次覆盖；不得解释为已完成全文扫描。",
        ]
        if unmapped:
            result["limitations"].append(
                "部分审计目录缺少可验证的当前来源批次索引；已保留调用计数，但未编造批次审计记录。"
            )
        return result


def _not_called_discovery(
    identity: Mapping[str, Any],
    reason: str,
    *,
    status: str = "not_called",
) -> dict[str, Any]:
    from finagent.retrieval.annual_discovery_context import empty_coverage

    return {
        "kind": "fintrace_annual_discovery",
        **dict(identity),
        "status": status,
        "prompt_version": "annual_discovery_v2",
        "model_called": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "coverage": empty_coverage(),
        "findings": [],
        "evidences": [],
        "audit_artifacts": [],
        "limitations": ["本次开放式发现尚未进入模型调用。"] if status == "not_called" else [],
        "reason": reason,
        "fraud_conclusion": None,
    }


def _validate_discovery_identity(payload: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    if payload.get("kind") != "fintrace_annual_discovery":
        raise ValueError("annual_discovery_kind_invalid")
    for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256"):
        actual = payload.get(key)
        expected = identity[key]
        if key == "source_sha256" and isinstance(actual, str):
            actual = actual.lower()
        if actual != expected:
            raise ValueError("annual_discovery_identity_mismatch")
    if payload.get("status") not in {"completed", "partial", "failed", "not_called"}:
        raise ValueError("annual_discovery_status_invalid")


def _invoke_review(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    *,
    settings: ModelSettings,
    run_dir: Path,
    artifacts_root: Path,
    deadline: float | None = None,
    main_soft_deadline: float | None = None,
) -> dict[str, Any]:
    agent = importlib.import_module("finagent.agents.annual_review")
    with annual_call_deadline(deadline):
        return agent.run_annual_model_review(
            report,
            investigation,
            settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
        )


def _attach_review_outcome(
    investigation: Mapping[str, Any], review: Mapping[str, Any]
) -> dict[str, Any]:
    payload = dict(investigation)
    base_calls = _nonnegative_int(payload.get("model_call_count"))
    base_attempts = _nonnegative_int(payload.get("model_call_attempt_count"))
    review_calls = _nonnegative_int(review.get("call_count"))
    review_attempts = _nonnegative_int(review.get("call_attempt_count"))
    discovery_calls = _nonnegative_int(payload.get("discovery_call_count"))
    discovery_attempts = _nonnegative_int(payload.get("discovery_call_attempt_count"))
    payload["investigation_call_count"] = _nonnegative_int(
        payload.get("investigation_call_count", max(base_calls - discovery_calls, 0))
    )
    payload["investigation_call_attempt_count"] = _nonnegative_int(
        payload.get("investigation_call_attempt_count", max(base_attempts - discovery_attempts, 0))
    )
    payload["model_review_call_count"] = review_calls
    payload["model_review_call_attempt_count"] = review_attempts
    payload["model_call_count"] = base_calls + review_calls
    payload["model_call_attempt_count"] = base_attempts + review_attempts
    payload["model_called"] = payload["model_call_attempt_count"] > 0
    audits = payload.get("audit_artifacts")
    review_audits = review.get("audit_artifacts")
    payload["audit_artifacts"] = [
        *(audits if isinstance(audits, list) else []),
        *(review_audits if isinstance(review_audits, list) else []),
    ]
    payload["final_review"] = dict(review)
    return payload


def attach_investigation_to_report(
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    investigation: Mapping[str, Any],
    *,
    investigation_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add the unverified M3 appendix without changing verified report sections."""

    updated_report = dict(report)
    scope = dict(report.get("scope", {}))
    original_note = str(scope.get("note", ""))
    scope["note"] = _model_scope_note(original_note)
    updated_report["scope"] = scope
    updated_report["model_called"] = bool(investigation.get("model_called"))
    updated_report["model_investigation"] = dict(investigation)
    discovery = investigation.get("discovery")
    if isinstance(discovery, Mapping):
        updated_report["model_discovery"] = dict(discovery)
    final_review = investigation.get("final_review")
    if isinstance(final_review, Mapping):
        updated_report["model_review"] = dict(final_review)

    updated_analysis = dict(analysis)
    updated_analysis["model_called"] = bool(investigation.get("model_called"))
    updated_analysis["scope"] = {"note": scope["note"]}
    if isinstance(final_review, Mapping):
        updated_analysis["model_review"] = dict(final_review)
    updated_analysis["investigation"] = {
        "status": investigation.get("status"),
        "model_called": bool(investigation.get("model_called")),
        "model_call_count": investigation.get("model_call_count", 0),
        "model_call_attempt_count": investigation.get("model_call_attempt_count", 0),
        "output_path": investigation_path,
    }
    if isinstance(discovery, Mapping):
        updated_analysis["model_discovery"] = {
            "status": discovery.get("status"),
            "model_called": discovery.get("model_called"),
            "model_call_count": discovery.get("model_call_count", 0),
            "model_call_attempt_count": discovery.get("model_call_attempt_count", 0),
            "output_path": "discovery.json",
        }
    return updated_report, updated_analysis


def render_investigation_markdown(investigation: Mapping[str, Any]) -> str:
    """Render model interpretations as explicitly unverified material."""

    lines = ["## 年报模型调查", "", "本节模型观点均未经独立核验；开放式发现不参与基础核验，也不确认或排除舞弊。", ""]
    lines.append(f"- 调查状态：`{investigation.get('status', 'failed')}`")
    lines.append(f"- 实际模型调用：{'是' if investigation.get('model_called') else '否'}")
    lines.append(f"- 调查归档：`{investigation.get('archive_path', 'investigation.json')}`")
    if investigation.get("reason"):
        lines.append(f"- 状态说明：`{investigation['reason']}`")
    if investigation.get("reason_detail"):
        lines.append(f"- 详情：{investigation['reason_detail']}")

    discovery = investigation.get("discovery")
    if isinstance(discovery, Mapping):
        coverage = discovery.get("coverage") if isinstance(discovery.get("coverage"), Mapping) else {}
        lines.extend(["", "### 全文开放式发现", ""])
        lines.append(f"- 发现状态：`{discovery.get('status', 'not_called')}`；实际调用：{'是' if discovery.get('model_called') else '否'}")
        lines.append(
            "- 文字覆盖："
            f"{coverage.get('total_pages', 0)} 页（有文字 {coverage.get('text_pages', 0)} 页），"
            f"{coverage.get('total_blocks', 0)} 个原始文字块 / {coverage.get('total_chars', 0)} 字符；"
            f"计划 {coverage.get('planned_batches', 0)} 批、已结束 {coverage.get('processed_batches', 0)} 批 "
            f"（成功 {coverage.get('successful_batches', 0)}、失败 {coverage.get('failed_batches', 0)}），"
            f"已处理 {coverage.get('processed_chars', 0)} 字符；完整={coverage.get('complete', False)}，"
            f"截断={coverage.get('truncated', False)}。"
        )
        unread_pages = coverage.get("unread_pages")
        if isinstance(unread_pages, list) and unread_pages:
            lines.append("- 未完整处理页码：" + ", ".join(str(page) for page in unread_pages))
        lines.append("- 观点标签：模型提出、未独立语义核验；不参与事实/计算/主张核验。")
        findings = discovery.get("findings")
        if not isinstance(findings, list) or not findings:
            lines.append("- 本次没有通过引用校验的开放式发现。")
        else:
            evidence_pages: dict[str, int] = {}
            for evidence in discovery.get("evidences", []) if isinstance(discovery.get("evidences"), list) else []:
                if isinstance(evidence, Mapping) and isinstance(evidence.get("evidence_id"), str) and type(evidence.get("pdf_page")) is int:
                    evidence_pages[evidence["evidence_id"]] = evidence["pdf_page"]
            for finding in findings:
                if not isinstance(finding, Mapping):
                    continue
                lines.extend(["", f"#### {finding.get('title', '未命名发现')}（{finding.get('priority', '未提供')}，未核实）", ""])
                lines.append("**观察：**" + str(finding.get("observation", "")))
                lines.append("- 影响说明：" + str(finding.get("why_it_matters", "")))
                lines.append("- 后续核查：" + str(finding.get("follow_up", "")))
                citations = finding.get("citations")
                if isinstance(citations, list):
                    refs = []
                    for citation in citations:
                        if not isinstance(citation, Mapping):
                            continue
                        evidence_id = citation.get("evidence_id")
                        page = evidence_pages.get(evidence_id)
                        if isinstance(evidence_id, str) and page is not None:
                            refs.append(f"PDF 第{page}页 / {evidence_id}")
                    lines.append("- 原文定位：" + ("；".join(refs) if refs else "无有效引用定位"))
        for item in discovery.get("limitations", []) if isinstance(discovery.get("limitations"), list) else []:
            lines.append(f"- 开放式发现限制：{item}")

    review = investigation.get("final_review")
    if isinstance(review, Mapping):
        lines.extend(["", "## 最终模型评审（模型观点，未独立核验）", ""])
        lines.append(f"- 评审状态：`{review.get('status', 'failed')}`")
        lines.append(f"- 实际模型调用：{'是' if review.get('model_called') else '否'}")
        if review.get("reason"):
            lines.append(f"- 状态说明：`{review['reason']}`")
        if review.get("assessment"):
            lines.append(f"- 评审意见：`{review['assessment']}`")
        if review.get("summary"):
            lines.extend(["", str(review["summary"])])
        for item in review.get("reasons", []) if isinstance(review.get("reasons"), list) else []:
            if isinstance(item, Mapping):
                lines.append(f"- 理由：{item.get('text', '')}（依据：{', '.join(item.get('evidence_ids', []))}）")
        for item in review.get("follow_up_items", []) if isinstance(review.get("follow_up_items"), list) else []:
            if isinstance(item, Mapping):
                lines.append(f"- 后续核查：{item.get('object', '')}；{item.get('action', '')}；依据：{', '.join(item.get('evidence_ids', []))}")
        for item in review.get("limitations", []) if isinstance(review.get("limitations"), list) else []:
            lines.append(f"- 评审限制：{item}")

    items = investigation.get("items")
    if not isinstance(items, list) or not items:
        lines.extend(["", "基础比较候选调查没有可用的模型解释；开放式发现状态已单独列示。"])
    else:
        for item in items:
            if not isinstance(item, Mapping):
                continue
            signal_id = item.get("signal_id", "unknown")
            status = item.get("status", "abstained")
            lines.extend(["", f"### `{signal_id}`（`{status}`）", ""])
            if status == "interpretation":
                lines.append("**未核实模型解释：**" + str(item.get("explanation") or "未提供。"))
            else:
                lines.append("**弃权：**" + str(item.get("reason") or "证据不足。"))
            for label, key in (
                ("替代解释", "alternative_explanations"),
                ("限制", "limitations"),
                ("同源叙述证据 ID", "narrative_evidence_ids"),
            ):
                values = item.get(key)
                if isinstance(values, list) and values:
                    lines.append(f"- {label}：" + "；".join(str(value) for value in values))

    audits = investigation.get("audit_artifacts")
    if isinstance(audits, list) and audits:
        lines.extend(["", "### 调用审计路径", ""])
        for audit in audits:
            if not isinstance(audit, Mapping):
                continue
            audit_dir = audit.get("audit_dir")
            if audit_dir:
                lines.append(f"- `{audit_dir}`（`{audit.get('status', 'unknown')}`）")
            for field in ("request_path", "response_path", "failure_path"):
                value = audit.get(field)
                if value:
                    lines.append(f"  - {field}: `{value}`")
    return "\n".join(lines).rstrip() + "\n"


def render_report_with_investigation(
    report: Mapping[str, Any], investigation: Mapping[str, Any]
) -> str:
    """Re-render the standard report and append the model investigation section."""

    from finagent.reports.annual_report import render_markdown

    return render_markdown(report).rstrip() + "\n\n" + render_investigation_markdown(investigation)


class _SourceBindingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _OptionalDependencyUnavailable(RuntimeError):
    pass


def _ensure_langgraph_available() -> None:
    try:
        importlib.import_module("langgraph.graph")
    except ImportError as exc:
        raise _OptionalDependencyUnavailable from exc


def _load_source_record(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
    except OSError:
        raise _SourceBindingError("source_record_unreadable") from None
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _SourceBindingError("source_record_invalid_json") from None
    if not isinstance(record, dict):
        raise _SourceBindingError("source_record_invalid_shape")
    return record, hashlib.sha256(payload).hexdigest()


def _validate_source_binding(
    report: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    record_sha256: str,
    source_record_path: Path,
    original_pdf_path: Path,
    archived_pdf_path: Path,
) -> dict[str, Any]:
    identity = _report_identity(report)
    report_year = identity["report_year"]
    period = record.get("report_period")
    if (
        record.get("document_id") != identity["source_document_id"]
        or record.get("company_id") != identity["company_id"]
        or not isinstance(period, str)
        or not _is_matching_period(period, report_year)
        or not isinstance(record.get("sha256"), str)
        or record["sha256"].lower() != identity["source_sha256"]
    ):
        raise _SourceBindingError("source_record_identity_mismatch")

    local_path = record.get("local_path")
    if not isinstance(local_path, str) or not local_path.strip():
        raise _SourceBindingError("source_record_pdf_path_missing")
    declared_path = Path(local_path.strip()).expanduser()
    if not declared_path.is_absolute():
        declared_path = _repository_root() / declared_path
    try:
        if declared_path.resolve() != original_pdf_path.resolve():
            raise _SourceBindingError("source_record_pdf_path_mismatch")
        original_hash = _sha256(original_pdf_path)
        archived_hash = _sha256(archived_pdf_path)
    except OSError:
        raise _SourceBindingError("source_pdf_unreadable") from None
    if original_hash != identity["source_sha256"] or archived_hash != identity["source_sha256"]:
        raise _SourceBindingError("source_pdf_hash_mismatch")

    return {
        "document_id": identity["source_document_id"],
        "company_id": identity["company_id"],
        "report_period": period,
        "source_sha256": identity["source_sha256"],
        "source_record_sha256": record_sha256,
        "source_record_path": _display_path(source_record_path),
        "source_pdf_path": _display_path(original_pdf_path),
        "archived_pdf_path": archived_pdf_path.name,
    }


def _report_identity(report: Mapping[str, Any]) -> dict[str, str | int]:
    run_id = report.get("run_id")
    company_id = report.get("company_id")
    report_year = report.get("report_year")
    document_id = report.get("source_document_id")
    source_sha256 = report.get("source_sha256")
    if (
        not isinstance(run_id, str)
        or not isinstance(company_id, str)
        or type(report_year) is not int
        or not isinstance(document_id, str)
        or not isinstance(source_sha256, str)
    ):
        raise ValueError("年度报告缺少调查所需的来源身份字段。")
    return {
        "run_id": run_id,
        "company_id": company_id,
        "report_year": report_year,
        "source_document_id": document_id,
        "source_sha256": source_sha256.lower(),
    }


def _validate_result_identity(payload: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256"):
        actual = payload.get(key)
        expected = identity[key]
        if key == "source_sha256" and isinstance(actual, str):
            actual = actual.lower()
        if actual != expected:
            raise ValueError("调查返回的来源身份与年度报告不一致。")
    if payload.get("status") not in {"completed", "abstained"}:
        raise ValueError("调查返回了未知状态。")


def _invoke_agent(
    report: Mapping[str, Any],
    *,
    archived_pdf_path: Path,
    source_record: Mapping[str, Any],
    settings: ModelSettings,
    run_dir: Path,
    reusable_items: list[Mapping[str, object]] | tuple[Mapping[str, object], ...] = (),
    checkpoint_callback: Any | None = None,
    parsed_pdf: object | None = None,
    deadline: float | None = None,
    main_soft_deadline: float | None = None,
    stage_time_limits_seconds: Mapping[str, int] | None = None,
) -> Any:
    agent = importlib.import_module("finagent.agents.annual_investigation")
    chosen_deadline = deadline
    if chosen_deadline is None and stage_time_limits_seconds is not None:
        limits = dict(stage_time_limits_seconds)
        chosen_deadline = min(
            value for value in (
                time.monotonic() + int(limits.get("annual_investigation", FIXED_INVESTIGATION_BUDGET_SECONDS)),
                main_soft_deadline,
            ) if value is not None
        )
    with annual_call_deadline(chosen_deadline):
        return agent.run_annual_investigation(
            report,
            archived_pdf_path,
            source_record,
            settings,
            run_dir=run_dir,
            reusable_items=reusable_items,
            checkpoint_callback=checkpoint_callback,
            parsed_pdf=parsed_pdf,
        )


def _discover_new_audits(
    run_dir: Path,
    before: set[str],
    artifacts_root: Path,
    *,
    prompt_version: str = "annual_investigation_v1",
) -> tuple[list[dict[str, Any]], int, int]:
    audits: list[dict[str, Any]] = []
    persisted_attempts = 0
    persisted_responses = 0
    for child in _new_uuid_children(run_dir, before):
        request = child / "request.json"
        response = child / "response.json"
        failure = child / "failure.json"
        admitted, accepted_responses = _audit_admission_counts(child)
        persisted_attempts += admitted
        persisted_responses += accepted_responses
        try:
            audit_dir = child.resolve().relative_to(artifacts_root.resolve()).as_posix()
        except (OSError, ValueError):
            audit_dir = child.name
        audits.append(
            {
                "audit_dir": audit_dir,
                "request_path": f"{audit_dir}/request.json" if request.exists() else None,
                "response_path": f"{audit_dir}/response.json" if response.exists() else None,
                "failure_path": f"{audit_dir}/failure.json" if failure.exists() else None,
                "status": "succeeded" if response.exists() else ("failed" if failure.exists() else "started"),
                "prompt_version": prompt_version,
            }
        )
    # Only the durable per-attempt ledger summary proves provider admissions.
    # The outer request file records audit start and is not a billable attempt.
    return audits, persisted_attempts, persisted_responses


def _audit_admission_counts(audit_dir: Path) -> tuple[int, int]:
    """Read durable retry counts; never treat an audit-start row as admission."""
    for name in ("response.json", "failure.json"):
        path = audit_dir / name
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                return 0, 0
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return 0, 0
        if not isinstance(value, Mapping):
            return 0, 0
        records = value.get("attempt_records")
        if isinstance(records, list):
            admissions = sum(
                1 for row in records
                if isinstance(row, Mapping) and row.get("provider_admitted") is True
            )
            responses = sum(
                1 for row in records
                if isinstance(row, Mapping) and row.get("response_accepted") is True
            )
            return admissions, responses
        admissions = value.get("provider_admission_count")
        responses = value.get("response_count")
        if (
            type(admissions) is int and admissions >= 0
            and type(responses) is int and 0 <= responses <= admissions
        ):
            return admissions, responses
        return 0, 0
    return 0, 0


def _discover_new_discovery_audits(
    run_dir: Path,
    before: set[str],
    artifacts_root: Path,
    identity: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int, int, int]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    persisted_attempts = 0
    persisted_responses = 0
    unmapped = 0
    for child in _new_uuid_children(run_dir, before):
        request = child / "request.json"
        response = child / "response.json"
        failure = child / "failure.json"
        admitted, accepted_responses = _audit_admission_counts(child)
        persisted_attempts += admitted
        persisted_responses += accepted_responses
        batch_index = _discovery_batch_index(request, identity) if request.is_file() else None
        audit_dir = _discovery_audit_relative_path(child, artifacts_root)
        if batch_index is None or audit_dir is None:
            unmapped += 1
            continue
        status = "succeeded" if response.is_file() else ("failed" if failure.is_file() else "started")
        candidates.append(
            (
                batch_index,
                {
                    "batch_index": batch_index,
                    "audit_dir": audit_dir,
                    "request_path": f"{audit_dir}/request.json" if request.is_file() else None,
                    "response_path": f"{audit_dir}/response.json" if response.is_file() else None,
                    "failure_path": f"{audit_dir}/failure.json" if failure.is_file() else None,
                    "status": status,
                    "prompt_version": "annual_discovery_v2",
                },
            )
        )

    counts: dict[int, int] = {}
    for batch_index, _artifact in candidates:
        counts[batch_index] = counts.get(batch_index, 0) + 1
    audits = [
        artifact
        for batch_index, artifact in candidates
        if counts[batch_index] == 1
    ]
    unmapped += len(candidates) - len(audits)
    audits.sort(key=lambda artifact: artifact["batch_index"])
    return audits, persisted_attempts, persisted_responses, unmapped


def _discovery_batch_index(request_path: Path, identity: Mapping[str, Any]) -> int | None:
    try:
        if request_path.stat().st_size > 2 * 1024 * 1024:
            return None
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, Mapping) or request.get("prompt_version") != "annual_discovery_v2":
            return None
        messages = request.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            return None
        user_message = messages[1]
        if not isinstance(user_message, Mapping) or user_message.get("role") != "user":
            return None
        user_content = user_message.get("content")
        if not isinstance(user_content, str) or len(user_content) > 120_000:
            return None
        payload = json.loads(user_content)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, Mapping) or set(payload) != {
        "company_id", "report_year", "source_document_id", "source_sha256", "batch_index", "fragments"
    }:
        return None
    if any(payload.get(key) != identity[key] for key in (
        "company_id", "report_year", "source_document_id", "source_sha256"
    )):
        return None
    batch_index = payload.get("batch_index")
    if type(batch_index) is not int or not 1 <= batch_index <= 128:
        return None
    return batch_index


def _discovery_audit_relative_path(child: Path, artifacts_root: Path) -> str | None:
    try:
        relative = child.resolve().relative_to(artifacts_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if not relative.startswith("runs/") or "\\" in relative:
        return None
    return f"artifacts/{relative}"


def _new_uuid_children(run_dir: Path, before: set[str]) -> list[Path]:
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for child in children:
        if not child.is_dir() or child.name in before:
            continue
        try:
            uuid.UUID(child.name)
        except ValueError:
            continue
        found.append(child)
    return found


def _run_children(run_dir: Path) -> set[str]:
    try:
        return {item.name for item in run_dir.iterdir() if item.is_dir()}
    except OSError:
        return set()


def _nonnegative_int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _is_matching_period(value: str, year: int) -> bool:
    if value != f"{year:04d}-12-31":
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _model_scope_note(note: str) -> str:
    sentence = (
        "显式启用的年度模型调查会调用模型：全文开放式发现扫描可提取文字并提出公司特有的未核实线索，"
        "不经过独立语义核验且不参与基础事实、计算和主张核验；固定候选调查与最终评审只提供模型观点，"
        "也不提升核验状态或确认、排除舞弊。"
    )
    for original in ("报告不调用模型，也不确认舞弊。", "报告不调用模型。"):
        if original in note:
            return note.replace(original, sentence)
    if sentence not in note:
        return f"{note.rstrip()} {sentence}".strip()
    return note


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(_repository_root().resolve()).as_posix()
    except (OSError, ValueError):
        return str(path.resolve())


def _relative_or_absolute(path: Path, parent: Path) -> str:
    try:
        return path.relative_to(parent).as_posix()
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
