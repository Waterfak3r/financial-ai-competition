"""Optional model-based annual investigation and report appendix."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from datetime import date, datetime
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
    ISSUE_GROUPING_RESERVED_SECONDS,
    MAIN_REPORT_BUDGET_SECONDS,
    runtime_stage_time_limits,
)
from finagent.schemas.text_pdf import ParsedTextPdf
from finagent.retrieval.annual_context import NarrativeSnippet


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
    active_mode = request_mode or ("integrated_analysis" if isinstance(m3_screening, Mapping) else "model_investigation")
    if resume_parent_run_id is not None:
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
            request_mode=active_mode,
            request_identity=request_identity,
            resume_parent_run_id=resume_parent_run_id,
            progress_callback=progress_callback,
            checkpoint_callback=checkpoint_callback,
            parsed_pdf=parsed_pdf,
            main_soft_deadline=main_soft_deadline,
            stage_time_limits_seconds=stage_time_limits_seconds,
        )
    return _run_streamlined_appendix(
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
        request_mode=active_mode,
        progress_callback=progress_callback,
        checkpoint_callback=checkpoint_callback,
        parsed_pdf=parsed_pdf,
        stage_time_limits_seconds=stage_time_limits_seconds,
    )



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
    pipeline_binding: Mapping[str, Any],
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
    from finagent.agents.annual_issue_pipeline import checkpoint_issue_pipeline
    pending_pipeline = checkpoint_issue_pipeline(
        {**dict(identity), "report_period": source_record.get("report_period")},
        discovery, execution_binding=pipeline_binding,
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
        "model_issue_pipeline": pending_pipeline,
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
        "final_review": importlib.import_module("finagent.agents.annual_review").not_called_model_review_v3(report, reason),
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


def _dispatch_progress(callback: Any, stage: str, detail: str) -> None:
    if not callback:
        return
    try:
        callback({
            "event": "batch_started" if stage == "investigation" else "batch_finished",
            "status": "running",
            "batch_index": 1,
            "planned_batches": 1,
            "processed_batches": 1 if stage == "review" else 0,
            "successful_batches": 1 if stage == "review" else 0,
            "partial_batches": 0,
            "failed_batches": 0,
            "total_pages": 1,
            "total_chars": 100,
        })
    except Exception:
        try:
            callback(stage, 0, detail)
        except Exception:
            pass

def _run_streamlined_appendix(
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
    progress_callback: Any | None = None,
    checkpoint_callback: Any | None = None,
    parsed_pdf: object | None = None,
    stage_time_limits_seconds: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    archive_path = state.get("archive_path")
    active_report = dict(binding_source_report)
    before_runs = _run_children(run_dir)
    def _agent_checkpoint(checkpoint_dict: Any, *args: Any) -> None:
        if checkpoint_callback is None:
            return
        try:
            snapshot, _ = attach_investigation_to_report(
                active_report, {}, checkpoint_dict,
                investigation_path=str(archive_path or "investigation.json"),
            )
            checkpoint_callback(snapshot, checkpoint_dict.get("status", "running"), None)
        except Exception:
            pass

    # 1. Run investigation (LangGraph)
    agent_failed = False
    try:
        agent_res = _invoke_agent(
            active_report,
            archived_pdf_path=archived_pdf_path,
            source_record=source_record,
            settings=settings,
            run_dir=run_dir,
            checkpoint_callback=_agent_checkpoint,
            parsed_pdf=parsed_pdf,
            stage_time_limits_seconds=stage_time_limits_seconds,
        )
        if hasattr(agent_res, "to_dict"):
            investigation_dict = agent_res.to_dict()
            raw_items = getattr(agent_res, "items", investigation_dict.get("items", []))
            raw_evidences = getattr(agent_res, "narrative_evidences", investigation_dict.get("narrative_evidences", []))
        else:
            investigation_dict = dict(agent_res)
            raw_items = investigation_dict.get("items", [])
            raw_evidences = investigation_dict.get("narrative_evidences", [])
    except Exception:
        agent_failed = True
        audits, atts, resps = _discover_new_audits(run_dir, before_runs, artifacts_root)
        investigation_dict = {
            "kind": "fintrace_annual_investigation",
            **dict(identity),
            "status": "failed",
            "reason": "annual_investigation_failed",
            "model_called": atts > 0,
            "model_call_count": resps,
            "model_call_attempt_count": atts,
            "investigation_call_count": resps,
            "investigation_call_attempt_count": atts,
            "items": [],
            "narrative_evidences": [],
            "audit_artifacts": audits,
            "archive_path": archive_path,
        }
        raw_items = []
        raw_evidences = []

    # 2. Build lightweight discovery projection
    discovery_findings = []
    for item in raw_items:
        item_dict = item.to_dict() if hasattr(item, "to_dict") else item
        if isinstance(item_dict, Mapping) and item_dict.get("status") == "interpretation" and item_dict.get("explanation"):
            sig_id = item_dict.get("signal_id", "unknown")
            nids = item_dict.get("narrative_evidence_ids", [])
            cit_pages = sorted({
                ev.pdf_page if hasattr(ev, "pdf_page") else ev.get("pdf_page")
                for ev in raw_evidences
                if (ev.evidence_id if hasattr(ev, "evidence_id") else ev.get("evidence_id")) in nids
                and (ev.pdf_page if hasattr(ev, "pdf_page") else ev.get("pdf_page")) is not None
            })
            signal_title = {
                "profit_up_cash_down": "净利润与经营活动现金流背离",
                "revenue_up_cash_down": "营业收入与经营活动现金流背离",
                "consolidated_profit_up_cash_down": "合并净利润与经营活动现金流背离",
                "deducted_parent_profit_share": "扣非净利润占归母净利润比例异常",
                "receivables_growth_gap": "应收账款增幅显著超过营业收入",
                "inventory_growth_gap": "存货增幅显著超过营业成本",
            }.get(sig_id, f"财务指标背离：{sig_id}")
            discovery_findings.append({
                "finding_id": f"lead:{sig_id}",
                "classification": "unverified_model_finding",
                "verification_status": "unverified",
                "title": signal_title,
                "observation": item_dict.get("explanation"),
                "why_it_matters": "该财务指标在报告期内存在跨表背离，需核实变动成因与披露一致性。",
                "follow_up": "建议核实管理层讨论（MD&A）相关披露及期后结算凭证。",
                "priority": "medium",
                "evidence_ids": list(nids),
                "citation_pages": cit_pages,
            })
    discovery_evidences = []
    for ev in raw_evidences:
        ev_dict = ev.to_dict() if hasattr(ev, "to_dict") else ev if isinstance(ev, Mapping) else None
        if ev_dict is not None:
            discovery_evidences.append({
                "evidence_id": ev_dict.get("evidence_id"),
                "source_document_id": ev_dict.get("source_document_id"),
                "source_sha256": ev_dict.get("source_sha256"),
                "section": ev_dict.get("section_label", ev_dict.get("section")),
                "pdf_page": ev_dict.get("pdf_page"),
                "bbox": list(ev_dict.get("bbox", [])),
                "raw_text": ev_dict.get("raw_text", ""),
                "finding_ids": [f"lead:{s}" for s in ev_dict.get("signal_ids", [])],
            })
        elif hasattr(ev, "evidence_id"):
            discovery_evidences.append({
                "evidence_id": ev.evidence_id,
                "source_document_id": ev.source_document_id,
                "source_sha256": ev.source_sha256,
                "section": ev.section_label,
                "pdf_page": ev.pdf_page,
                "bbox": list(ev.bbox),
                "raw_text": ev.raw_text,
                "finding_ids": [f"lead:{s}" for s in ev.signal_ids],
            })

    discovery = _not_called_discovery(
        identity,
        "streamlined_investigation_used",
        status="not_called",
    )

    investigation_dict["discovery"] = discovery

    # 3. Run streamlined review
    _dispatch_progress(progress_callback, "review", "正在生成结构化总评与跟进建议...")
    try:
        review_result = _invoke_review(
            active_report,
            investigation_dict,
            settings=settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
        )
    except Exception:
        review_result = not_called_model_review(active_report, "annual_review_failed")
        review_result["status"] = "failed"
        review_result["reason"] = "annual_review_failed"

    # 4. Attach review outcome & assemble
    result = _attach_review_outcome(investigation_dict, review_result)
    result["archive_path"] = archive_path
    result["source_binding"] = state.get("source_binding")
    if agent_failed:
        result["status"] = "failed"
        result["reason"] = "annual_investigation_failed"
    elif review_result.get("status") == "failed":
        result["status"] = "completed_with_issues"
    else:
        result["status"] = investigation_dict.get("status", "completed")

    snapshot_report, _ = attach_investigation_to_report(
        active_report,
        {},
        result,
        investigation_path=str(archive_path or "investigation.json"),
    )
    if checkpoint_callback is not None:
        try:
            checkpoint_callback(snapshot_report, result["status"], None)
        except Exception:
            pass

    return result


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
    request_identity: Mapping[str, Any] | None,
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
        build_annual_issue_pipeline_binding_v11,
        build_annual_recovery_binding,
        build_annual_discovery_binding_from_context,
        validate_annual_recovery_parent,
    )

    if request_identity is not None:
        if request_identity.get("mode") != request_mode:
            raise ValueError("annual_recovery_request_mode_mismatch")
        binding = build_annual_recovery_binding(
            _repository_root(), request_identity, settings=settings,
            report=binding_source_report, parsed_pdf=parsed_pdf,
        )
    else:
        if resume_parent_run_id is not None:
            raise ValueError("annual_recovery_request_identity_missing")
        from finagent.retrieval.annual_discovery_context import PERIOD_HEADER_PLAN_VERSION, build_annual_discovery_context

        discovery_module = importlib.import_module("finagent.agents.annual_discovery")
        context = build_annual_discovery_context(
            binding_source_report, archived_pdf_path, source_record,
            system_prompt=discovery_module._load_prompt("annual_discovery_v5"),
            model_name=settings.model, plan_version=PERIOD_HEADER_PLAN_VERSION, parsed_pdf=parsed_pdf,
        )
        binding = build_annual_discovery_binding_from_context(
            context, settings=settings, report=binding_source_report, analysis_mode=request_mode,
        )
        request_identity = {
            "company_id": identity["company_id"], "report_year": identity["report_year"],
            "document_id": identity["source_document_id"], "sha256": identity["source_sha256"],
            "mode": request_mode,
        }
    pipeline_binding = build_annual_issue_pipeline_binding_v11(
        _repository_root(), request_identity, settings=settings,
        report=binding_source_report, parsed_pdf=parsed_pdf, discovery_binding=binding,
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
                require_issue_pipeline=True,
            )
            if not capability.get("can_resume"):
                rejection_reason = _safe_recovery_reason(
                    capability.get("reason") or "annual_recovery_parent_rejected"
                )
            elif capability.get("binding") != binding or capability.get("pipeline_binding") != pipeline_binding:
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
                pipeline_binding=pipeline_binding,
                parsed_pdf=parsed_pdf,
                requested_parent_run_id=resume_parent_run_id,
                reason=rejection_reason,
                checkpoint_callback=checkpoint_callback,
            )

    latest_discovery: dict[str, Any] | None = None
    latest_investigation: dict[str, Any] | None = None
    from finagent.agents.annual_issue_pipeline import initial_issue_pipeline

    pipeline_identity = {**dict(identity), "report_period": source_record.get("report_period")}
    latest_issue_pipeline: dict[str, Any] = initial_issue_pipeline(pipeline_identity)
    parent_issue_pipeline = (
        parent_investigation.get("model_issue_pipeline")
        if isinstance(parent_investigation, Mapping)
        and isinstance(parent_investigation.get("model_issue_pipeline"), Mapping)
        else None
    )
    review_agent = importlib.import_module("finagent.agents.annual_review")

    def not_called_current_review(reason: str, *, status: str = "not_called") -> dict[str, Any]:
        return review_agent.not_called_model_review_v3(report, reason, status=status)

    state["final_review"] = not_called_current_review("model_review_not_started")

    def compose_payload(
        fixed_value: Mapping[str, Any] | None = None,
        review_value: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        fixed = dict(fixed_value if isinstance(fixed_value, Mapping) else latest_investigation or {})
        discovery_value = dict(latest_discovery or {})
        pipeline_value = dict(latest_issue_pipeline)
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
            else not_called_current_review("model_review_not_started")
        )
        review_calls = _nonnegative_int(final_review.get("call_count"))
        review_attempts = _nonnegative_int(final_review.get("call_attempt_count"))
        pipeline_summary = pipeline_value.get("audit_summary") if isinstance(pipeline_value.get("audit_summary"), Mapping) else {}
        pipeline_calls = _nonnegative_int(pipeline_summary.get("response_count"))
        pipeline_attempts = _nonnegative_int(pipeline_summary.get("attempt_count"))
        raw_audits = [
            *(discovery_value.get("audit_artifacts", []) if isinstance(discovery_value.get("audit_artifacts"), list) else []),
            *(fixed.get("audit_artifacts", []) if isinstance(fixed.get("audit_artifacts"), list) else []),
            *(final_review.get("audit_artifacts", []) if isinstance(final_review.get("audit_artifacts"), list) else []),
            *(pipeline_summary.get("audit_artifacts", []) if isinstance(pipeline_summary.get("audit_artifacts"), list) else []),
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
            source_binding=dict(state.get("source_binding")) if isinstance(state.get("source_binding"), Mapping) else None,
            status=fixed.get("status", "not_called"),
            model_called=(discovery_attempts + fixed_attempts + pipeline_attempts + review_attempts) > 0,
            discovery=discovery_value,
            model_issue_pipeline=pipeline_value,
            discovery_call_count=discovery_calls,
            discovery_call_attempt_count=discovery_attempts,
            investigation_call_count=fixed_calls,
            investigation_call_attempt_count=fixed_attempts,
            issue_pipeline_call_count=pipeline_calls,
            issue_pipeline_call_attempt_count=pipeline_attempts,
            model_review_call_count=review_calls,
            model_review_call_attempt_count=review_attempts,
            model_call_count=discovery_calls + fixed_calls + pipeline_calls + review_calls,
            model_call_attempt_count=discovery_attempts + fixed_attempts + pipeline_attempts + review_attempts,
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
    reused_evidence_ids = {
        evidence_id
        for item in reusable_items
        for evidence_id in item.get("narrative_evidence_ids", [])
    }
    reusable_narrative_evidences = tuple(
        NarrativeSnippet(
            evidence_id=row["evidence_id"],
            source_document_id=row["source_document_id"],
            source_sha256=row["source_sha256"],
            signal_ids=tuple(row["signal_ids"]),
            section_label=row["section_label"],
            pdf_page=row["pdf_page"],
            bbox=tuple(row["bbox"][key] for key in ("x0", "y0", "x1", "y1")),
            raw_text=row["raw_text"],
            selection_reason=row["selection_reason"],
        )
        for row in (parent_archive.narrative_evidences if parent_archive is not None else ())
        if row["evidence_id"] in reused_evidence_ids
    )

    def discovery_checkpoint(value: Mapping[str, object]) -> None:
        nonlocal latest_issue_pipeline
        from finagent.agents.annual_issue_pipeline import checkpoint_issue_pipeline

        latest_issue_pipeline = checkpoint_issue_pipeline(
            pipeline_identity,
            value,
            execution_binding=pipeline_binding,
        )
        publish(value)

    # Persist an explicit downstream pending layer before the first provider
    # call. This state is replaced with the complete current finding ledger at
    # every discovery checkpoint and never claims no_findings prematurely.
    before = _run_children(run_dir)
    stage_limits = dict(stage_time_limits_seconds or runtime_stage_time_limits(MAIN_REPORT_BUDGET_SECONDS))
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
    discovery_checkpoint(discovery)

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
        review = not_called_current_review("user_cancelled", status="abstained")
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
        review = not_called_current_review(reason, status="abstained")
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
        review = not_called_current_review(stop_reason, status="abstained")
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
            reusable_narrative_evidences=reusable_narrative_evidences,
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
        review = not_called_current_review("user_cancelled", status="abstained")
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    if main_soft_deadline is not None and time.monotonic() >= main_soft_deadline:
        review = not_called_current_review("workflow_time_budget_exhausted", status="abstained")
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    fixed_stop_reason = _investigation_stop_reason(payload)
    if fixed_stop_reason is not None:
        latest_issue_pipeline["status"] = "abstained" if fixed_stop_reason == "cancelled" else "pending"
        latest_issue_pipeline["reason"] = fixed_stop_reason
        review = not_called_current_review(fixed_stop_reason, status="abstained")
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    from finagent.agents.annual_issue_pipeline import run_annual_issue_pipeline

    issue_started = time.monotonic()
    issue_deadline = min(
        value for value in (
            issue_started + int(stage_limits.get("annual_issue_grouping", ISSUE_GROUPING_RESERVED_SECONDS)),
            main_soft_deadline,
        ) if value is not None
    )

    def issue_pipeline_checkpoint(value: Mapping[str, Any]) -> None:
        nonlocal latest_issue_pipeline
        latest_issue_pipeline = dict(value)
        current_payload = compose_payload(latest_investigation or payload)
        publish(discovery, current_payload)

    try:
        latest_issue_pipeline = run_annual_issue_pipeline(
            discovery,
            identity=pipeline_identity,
            parsed_pdf=parsed_pdf if isinstance(parsed_pdf, ParsedTextPdf) else None,
            settings=settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
            execution_binding=pipeline_binding,
            scope_gaps=(report.get("scope", {}).get("gaps", []) if isinstance(report.get("scope"), Mapping) and isinstance(report.get("scope", {}).get("gaps"), list) else []),
            parent_pipeline=parent_issue_pipeline,
            parent_run_id=resume_parent_run_id,
            deadline=issue_deadline,
            main_soft_deadline=main_soft_deadline,
            cancel_check=_call_control_requested,
            checkpoint_callback=issue_pipeline_checkpoint,
        )
    except _RecoveryCheckpointError:
        raise
    except Exception:
        latest_issue_pipeline["status"] = "failed"
        latest_issue_pipeline["reason"] = "issue_pipeline_failed"
    payload = compose_payload(payload)
    publish(discovery, payload)
    pipeline_stop = _issue_pipeline_stop_reason(latest_issue_pipeline)
    if pipeline_stop is not None:
        review = not_called_current_review(pipeline_stop, status="abstained")
        result = compose_payload(payload, review)
        publish(discovery, result, review)
        return result

    reused_review = _reuse_parent_review_v3(
        parent_report,
        report,
        parent_investigation,
        payload,
        parent_issue_pipeline,
        latest_issue_pipeline,
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
            review = not_called_current_review("model_review_call_failed", status="failed")
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
    raw_evidences = value.get("narrative_evidences", [])
    evidence_by_id = {
        evidence["evidence_id"]: evidence
        for evidence in raw_evidences
        if isinstance(evidence, Mapping) and isinstance(evidence.get("evidence_id"), str)
    } if isinstance(raw_evidences, list) else {}
    items = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if item.get("status") == "interpretation":
            evidence_ids = item.get("narrative_evidence_ids")
            if (
                isinstance(evidence_ids, list)
                and all(
                    isinstance(evidence_id, str)
                    and evidence_id in evidence_by_id
                    and item.get("signal_id") in evidence_by_id[evidence_id].get("signal_ids", [])
                    for evidence_id in evidence_ids
                )
            ):
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


def _issue_pipeline_stop_reason(pipeline: Mapping[str, Any]) -> str | None:
    reason = pipeline.get("reason")
    if reason in {
        "http_401", "http_403", "cancelled", "call_gate_closed", "ledger_config_invalid",
        "ledger_source_context_missing_or_unapproved", "ledger_unavailable", "ledger_provider_mismatch",
        "ledger_budget_exhausted", "task_budget_exhausted", "ledger_policy_mismatch", "audit_write_failed",
    }:
        return str(reason)
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


def _reuse_parent_review_v3(
    parent_report: Mapping[str, Any] | None,
    current_report: Mapping[str, Any],
    parent_investigation: Mapping[str, Any] | None,
    current_investigation: Mapping[str, Any],
    parent_pipeline: Mapping[str, Any] | None,
    current_pipeline: Mapping[str, Any],
    *,
    current_model_name: str,
) -> dict[str, Any] | None:
    if not isinstance(parent_report, Mapping) or not isinstance(parent_investigation, Mapping) or not isinstance(parent_pipeline, Mapping):
        return None
    review = parent_report.get("model_review")
    if not isinstance(review, Mapping) or review.get("status") != "completed" or review.get("prompt_version") != "annual_review_v3":
        return None
    if not isinstance(current_pipeline, Mapping) or current_pipeline.get("kind") != "annual_issue_pipeline_v1":
        return None
    audits = review.get("audit_artifacts")
    if not isinstance(audits, list) or not any(
        isinstance(item, Mapping) and item.get("status") == "succeeded"
        and item.get("prompt_version") == "annual_review_v3"
        and isinstance(item.get("request_path"), str) and isinstance(item.get("response_path"), str)
        for item in audits
    ):
        return None
    try:
        parent_identity = _report_identity(parent_report)
        current_identity = _report_identity(current_report)
        if review.get("source_identity") != parent_identity:
            return None
        parent_signature = _review_reuse_signature_v3(parent_report, parent_investigation, parent_identity, current_model_name)
        current_signature = _review_reuse_signature_v3(current_report, current_investigation, current_identity, current_model_name)
    except (OSError, TypeError, ValueError, KeyError):
        return None
    if parent_signature != current_signature:
        return None
    rebound = dict(review)
    rebound.update(
        reason="review_reused_for_matching_inputs", model_called=False,
        call_count=0, call_attempt_count=0,
        source_identity=not_called_model_review(current_report, "review_reused_for_matching_inputs")["source_identity"],
    )
    rebound["prompt_version"] = "annual_review_v3"
    return rebound


def _review_reuse_signature_v3(
    report: Mapping[str, Any], investigation: Mapping[str, Any], identity: Mapping[str, Any], model_name: str,
) -> str:
    from finagent.agents.annual_review import (
        V3_REVIEW_INPUT_PROJECTION_VERSION,
        V3_REVIEW_WIRE_VERSION,
        _build_review_input_v3,
    )

    prompt_path = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_review_v3.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt_sha256 = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    review_input, allowed_ids, evidence_refs, no_priority = _build_review_input_v3(
        report, investigation, identity, request_model=model_name, system_prompt=prompt,
        enforce_budget=False,
    )
    source_identity = review_input.get("source_identity")
    if not isinstance(source_identity, dict):
        raise ValueError("annual_review_identity_missing")
    source_identity.pop("run_id", None)
    review_input = _canonicalize_claim_verification_timestamps_v3(review_input)
    material = {
        "prompt_version": "annual_review_v3",
        "prompt_sha256": prompt_sha256,
        "review_wire_version": V3_REVIEW_WIRE_VERSION,
        "review_input_projection_version": V3_REVIEW_INPUT_PROJECTION_VERSION,
        "model": model_name,
        "input": review_input,
        "allowed_ids": sorted(allowed_ids),
        "evidence_refs": [{"document_id": item.document_id, "page": item.page} for item in evidence_refs],
        "no_priority_allowed": no_priority,
    }
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonicalize_claim_verification_timestamps_v3(review_input: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only volatile timestamps in the explicit claim-verification pool.

    This copy is used only to decide whether a later run has the same business
    inputs. The complete review input retains original timestamps; its purpose
    projection omits this explicit metadata path. Valid ISO ``verified_at``
    values are removed here while a structured sibling ``timestamp_state``
    preserves presence and non-ISO values. Re-keying this signature copy and
    both claim reference lists is necessary because the reference ID hashes the
    complete verification record.
    """

    normalized = copy.deepcopy(dict(review_input))
    pool = normalized.get("claim_verification_pool")
    if not isinstance(pool, list):
        raise ValueError("annual_review_v3_claim_verification_pool_invalid")

    old_to_new: dict[str, str] = {}
    normalized_rows: dict[str, dict[str, Any]] = {}
    for row in pool:
        if not isinstance(row, dict) or set(row) != {"reference_id", "record"}:
            raise ValueError("annual_review_v3_claim_verification_pool_invalid")
        old_reference = row.get("reference_id")
        record = row.get("record")
        if (
            not isinstance(old_reference, str)
            or not old_reference.startswith("claim-verification:")
            or not isinstance(record, Mapping)
        ):
            raise ValueError("annual_review_v3_claim_verification_pool_invalid")

        canonical_record = dict(record)
        if "verified_at" not in canonical_record:
            timestamp_state = "missing"
        elif isinstance(canonical_record["verified_at"], str) and _is_iso_datetime(canonical_record["verified_at"]):
            # Remove only a valid timestamp value from this signature copy.
            # Keep its presence as structured metadata instead of inserting a
            # string sentinel that could collide with an accepted non-ISO value.
            canonical_record.pop("verified_at")
            timestamp_state = "valid_iso_present"
        else:
            # Preserve nulls and every non-ISO value exactly. A separate state
            # also distinguishes an absent field from a present null value.
            timestamp_state = "non_iso_present"
        canonical_material = {
            "record": canonical_record,
            "timestamp_state": timestamp_state,
        }
        encoded = json.dumps(canonical_material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        new_reference = f"claim-verification:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"
        prior = normalized_rows.get(new_reference)
        normalized_row = {
            "record": canonical_record,
            "timestamp_state": timestamp_state,
        }
        if prior is not None and prior != normalized_row:
            raise ValueError("annual_review_v3_claim_pool_collision")
        normalized_rows[new_reference] = normalized_row
        if old_reference in old_to_new:
            raise ValueError("annual_review_v3_claim_verification_pool_invalid")
        old_to_new[old_reference] = new_reference

    normalized["claim_verification_pool"] = [
        {"reference_id": reference, **normalized_rows[reference]}
        for reference in sorted(normalized_rows)
    ]
    for section_name in ("verified_claims", "interpretations"):
        section = normalized.get(section_name)
        if not isinstance(section, list):
            raise ValueError("annual_review_v3_claim_verification_refs_invalid")
        for claim in section:
            if not isinstance(claim, dict) or not isinstance(claim.get("verification_refs"), list):
                raise ValueError("annual_review_v3_claim_verification_refs_invalid")
            rebound_refs: list[str] = []
            for old_reference in claim["verification_refs"]:
                if not isinstance(old_reference, str) or old_reference not in old_to_new:
                    raise ValueError("annual_review_v3_claim_verification_refs_invalid")
                rebound_refs.append(old_to_new[old_reference])
            claim["verification_refs"] = rebound_refs
    return normalized


def _is_iso_datetime(value: str) -> bool:
    if len(value) < 16 or not any(separator in value for separator in ("T", " ")):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


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
        versions = {audit["prompt_version"] for audit in audits}
        if len(versions) == 1:
            result["prompt_version"] = next(iter(versions))
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
        "prompt_version": "annual_discovery_v5",
        "model_called": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "coverage": empty_coverage(),
        "findings": [],
        "evidences": [],
        "audit_artifacts": [],
        "limitations": [],
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
        pipeline = investigation.get("model_issue_pipeline")
        if isinstance(pipeline, Mapping) and pipeline.get("kind") == "annual_issue_pipeline_v1":
            return agent.run_annual_model_review_v3(
                report, investigation, settings, run_dir=run_dir, artifacts_root=artifacts_root,
            )
        return agent.run_streamlined_annual_review(
            report, investigation, settings, run_dir=run_dir, artifacts_root=artifacts_root,
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
    issue_pipeline = investigation.get("model_issue_pipeline")
    if isinstance(issue_pipeline, Mapping):
        updated_report["model_issue_pipeline"] = dict(issue_pipeline)
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

    lines = ["## 年报模型调查", "", "本节基于大模型对年报文本披露进行成因分析与综合评审，供专业复核参考。", ""]
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

    issue_pipeline = investigation.get("model_issue_pipeline")
    if isinstance(issue_pipeline, Mapping):
        issue_input = issue_pipeline.get("input") if isinstance(issue_pipeline.get("input"), Mapping) else {}
        closure = issue_pipeline.get("global_closure") if isinstance(issue_pipeline.get("global_closure"), Mapping) else {}
        candidates = issue_pipeline.get("candidates") if isinstance(issue_pipeline.get("candidates"), list) else []
        decisions = {
            item.get("finding_id"): item for item in issue_pipeline.get("member_decisions", [])
            if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
        } if isinstance(issue_pipeline.get("member_decisions"), list) else {}
        issues = issue_pipeline.get("issues") if isinstance(issue_pipeline.get("issues"), list) else []
        issues_by_member = {
            member: issue for issue in issues if isinstance(issue, Mapping)
            for member in issue.get("member_finding_ids", []) if isinstance(member, str)
        }
        lines.extend(["", "## 年度问题归并与处理账", "", "所有合格发现均完整保留为本地候选；当前已知关系闭合与全文发现覆盖分别记录。模型归并和同源检索未独立核验。", ""])
        lines.append(
            f"- 状态：`{issue_pipeline.get('status', 'unknown')}`；发现 {issue_input.get('finding_count', 0)} 条；"
            f"候选 {issue_input.get('candidate_count', 0)} 项；已知关系闭合={closure.get('closed', False)}；"
            f"全文覆盖完整={issue_input.get('discovery_coverage_complete', False)}；"
            f"允许无优先事项意见={closure.get('no_priority_allowed', False)}。"
        )
        for label, key in (("跨片关系缺口", "relation_gaps"), ("全文覆盖缺口", "coverage_gaps")):
            values = closure.get(key)
            if isinstance(values, list) and values:
                lines.append(f"- {label}：" + "；".join(str(item) for item in values))
        if not candidates:
            lines.append("- 当前候选为空；若扫描未完成，不能据此判断全年无异常线索。")
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            finding_id = candidate.get("finding_id")
            decision = decisions.get(finding_id, {})
            issue = issues_by_member.get(finding_id, {})
            lines.extend([
                "", f"### {candidate.get('title', '未命名候选')}（{candidate.get('priority', '未提供')}，未核实）", "",
                f"- 成员：`{finding_id}`；逐项处置：`{decision.get('disposition', 'pending')}`；理由：`{decision.get('reason_code', 'awaiting_grouping')}`。",
                f"- 问题 ID：`{decision.get('issue_id') or '未形成'}`；状态：`{issue.get('status', '未闭合')}`；完整成员：{', '.join(issue.get('member_finding_ids', [])) if issue else '未形成'}。",
                "**观察：**" + str(candidate.get("observation", "")),
                "- 关注原因：" + str(candidate.get("why_it_matters", "")),
                "- 后续核查：" + str(candidate.get("follow_up", "")),
            ])
            for citation in candidate.get("citations", []) if isinstance(candidate.get("citations"), list) else []:
                if isinstance(citation, Mapping):
                    lines.append(
                        f"- 原文引用定位：PDF 第 {citation.get('pdf_page')} 页 / 块 {citation.get('block_index')} / "
                        f"字符 {citation.get('start_char')} / `{citation.get('evidence_id')}`"
                    )
        supplements = issue_pipeline.get("supplements")
        if isinstance(supplements, list) and supplements:
            lines.extend(["", "### 同源定向检索片段", ""])
            for supplement in supplements:
                if not isinstance(supplement, Mapping):
                    continue
                role_rows = supplement.get("issue_roles", [])
                roles = "; ".join(
                    f"{row.get('issue_id')}：{row.get('role')}（{row.get('reason_code')}）"
                    for row in role_rows if isinstance(row, Mapping)
                ) if isinstance(role_rows, list) else ""
                lines.append(
                    f"- {supplement.get('section')} · PDF 第 {supplement.get('pdf_page')} 页 / 块 {supplement.get('block_index')} / "
                    f"字符 {supplement.get('start_char')}–{supplement.get('end_char')} · {supplement.get('role_status')} · "
                    f"问题关系：{roles or supplement.get('role', 'unknown')}。此检索不构成独立验证。"
                )

    review = investigation.get("final_review")
    if isinstance(review, Mapping):
        lines.extend(["", "### AI 综合评审意见", ""])
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
                lines.append("**说明：**" + str(item.get("reason") or "年报未检索到成因披露。"))
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
    reusable_narrative_evidences: tuple[NarrativeSnippet, ...] = (),
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
            reusable_narrative_evidences=reusable_narrative_evidences,
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
                    "prompt_version": json.loads(request.read_text(encoding="utf-8"))["prompt_version"],
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
        if not isinstance(request, Mapping) or request.get("prompt_version") not in {
            "annual_discovery_v1", "annual_discovery_v2", "annual_discovery_v3", "annual_discovery_v4", "annual_discovery_v5",
        }:
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
    sentence = "AI 深度调查与评审已启用：结合年报管理层讨论（MD&A）与财务报表附注披露进行成因核验与结构化综合评审，供专业复核参考。"
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
