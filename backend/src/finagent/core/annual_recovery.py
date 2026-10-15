"""Shared, fail-closed annual-run recovery identity and gate projections.

This module is deliberately independent of the job service. Both the manager
and CLI use the same binding construction and the SQLite ledger remains the
only owner of provider-admission persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from finagent.api.annual_report_read import AnnualReportReadError, load_annual_report
from finagent.core.model_call_ledger import (
    ModelCallAdmissionError,
    ModelCallLedgerReadError,
    provider_model_fingerprint,
    read_gate_admission_count as _ledger_gate_admission_count,
    select_call_gate_database as _ledger_select_gate_database,
)
from finagent.core.model_settings import ModelConfigError, ModelSettings, load_model_settings
from finagent.core.annual_work_budget import (
    BUDGET_CONTRACT_VERSION,
    GLOBAL_REQUEST_LIMIT_CHARS,
    MODEL_CALL_TIMEOUT_SECONDS,
    REVIEW_REQUEST_LIMIT_CHARS,
    archived_budget_contract_v2,
    archived_budget_contract_v3,
    archived_budget_contract_v4,
)
from finagent.agents.annual_review_wire import (
    ISSUE_INPUT_WIRE_SCHEMA,
    V3_INPUT_PROJECTION_VERSION,
    V3_WIRE_SCHEMA,
)
from finagent.retrieval.annual_discovery_context import (
    COMPACT_PLAN_VERSION,
    PERIOD_HEADER_PLAN_VERSION,
    AnnualDiscoveryContext,
    annual_discovery_plan_projection,
    build_annual_discovery_context,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODES = {"model_investigation", "integrated_analysis"}
_PROMPTS = (
    "annual_discovery_v2.md",
    "annual_discovery_v3.md",
    "annual_investigation_v1.md",
    "annual_review_v1.md",
    "annual_review_v2.md",
)
_PARSER_VERSION = "annual-discovery-parser-v3-execution-v4"
_PERIOD_HEADER_PARSER_VERSION = "annual-discovery-parser-v3-execution-v5"
_ISSUE_PIPELINE_PROMPTS = (
    "annual_issue_grouping_v1.md",
    "annual_issue_global_v1.md",
    "annual_issue_supplement_v1.md",
    "annual_review_v3.md",
)


def select_call_gate_database(project_root: Path, job_dir: Path) -> Path:
    """Select the common goal ledger when configured, otherwise a job-local DB."""

    return _ledger_select_gate_database(project_root, job_dir)


def read_gate_admission_count(database_path: Path, gate_key: str) -> int:
    """Read all-stage admissions for one gate; corruption is never reported as 0."""

    return _ledger_gate_admission_count(database_path, gate_key)


def validate_annual_recovery_parent(
    project_root: Path,
    parent_run_id: str,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings | None = None,
    archive: Any | None = None,
    parsed_pdf: object | None = None,
    require_issue_pipeline: bool = True,
) -> dict[str, Any]:
    """Strictly validate a saved parent and return its current binding.

    ``request`` is the already validated, normalized four-mode job request.
    ``require_issue_pipeline`` requires the activity outer v11 binding.
    The returned reason codes contain no paths, prompts, or provider config.
    """

    try:
        if archive is None:
            archive = load_annual_report(Path(project_root), parent_run_id)
        if getattr(archive, "run_id", None) != parent_run_id:
            return {"can_resume": False, "reason": "recovery_parent_invalid", "binding": None}
        report_identity = archive.report
        expected_identity = {
            "company_id": request.get("company_id"),
            "report_year": request.get("report_year"),
            "source_document_id": request.get("document_id"),
            "source_sha256": str(request.get("sha256", "")).lower(),
        }
        if any(report_identity.get(key) != value for key, value in expected_identity.items()):
            return {"can_resume": False, "reason": "recovery_parent_invalid", "binding": None}
        if settings is None:
            settings = load_model_settings()
        binding = build_annual_recovery_binding(
            Path(project_root),
            request,
            settings=settings,
            report=archive.report,
            parsed_pdf=parsed_pdf,
        )
        pipeline_binding = build_annual_issue_pipeline_binding_v11(
            Path(project_root), request, settings=settings, report=archive.report, parsed_pdf=parsed_pdf,
        )
        if require_issue_pipeline:
            pipeline = archive.report.get("model_issue_pipeline")
            if not isinstance(pipeline, Mapping) or pipeline.get("kind") != "annual_issue_pipeline_v1" or pipeline.get("execution_binding") != pipeline_binding:
                return {"can_resume": False, "reason": "recovery_pipeline_binding_mismatch", "binding": binding, "pipeline_binding": pipeline_binding}
        layer = archive.report.get("model_discovery")
        if not isinstance(layer, Mapping):
            return {"can_resume": False, "reason": "recovery_discovery_missing", "binding": None}
        contract = layer.get("execution_contract")
        if not isinstance(contract, Mapping):
            return {"can_resume": False, "reason": "recovery_binding_missing", "binding": None}
        if contract.get("binding") != binding:
            return {"can_resume": False, "reason": "recovery_binding_mismatch", "binding": binding}
        if require_issue_pipeline and contract.get("kind") != "fintrace_annual_discovery_execution_v11":
            return {"can_resume": False, "reason": "recovery_pipeline_binding_mismatch", "binding": binding, "pipeline_binding": pipeline_binding}
        manifest_status = archive.manifest_summary.get("status")
        snapshot = getattr(archive, "snapshot", None)
        snapshot_is_partial = isinstance(snapshot, Mapping) and snapshot.get("is_partial") is True
        if manifest_status == "running":
            return {"can_resume": False, "reason": "recovery_parent_active", "binding": binding}
        if manifest_status not in {"completed", "completed_with_issues", "interrupted", "failed"}:
            return {"can_resume": False, "reason": "recovery_parent_not_terminal", "binding": binding}
        # A completed manifest may briefly precede the final checkpoint seal.
        # The strict reader then returns the last immutable v3 pointer as a
        # partial snapshot. A sealed completed_with_issues report is also
        # resumable when a strictly validated AI stage remains unfinished;
        # an ordinary sealed completed report is never a resume source.
        snapshot_state = snapshot.get("state") if isinstance(snapshot, Mapping) else None
        if (
            manifest_status == "completed" and not snapshot_is_partial
            or manifest_status == "completed_with_issues"
            and not snapshot_is_partial
            and snapshot_state != "completed_with_issues"
        ):
            return {"can_resume": False, "reason": "recovery_parent_not_terminal", "binding": binding}
        batches = contract.get("batches")
        discovery_unfinished = isinstance(batches, list) and any(
            isinstance(row, Mapping) and row.get("state") != "succeeded" for row in batches
        )
        review = archive.report.get("model_review")
        review_unfinished = not isinstance(review, Mapping) or review.get("status") != "completed"
        if (
            review_unfinished
            and isinstance(review, Mapping)
            and review.get("status") == "abstained"
            and review.get("reason") == "annual_review_v3_request_budget_exceeded"
        ):
            # The exact v3 whole-wire overflow is deterministic for this source
            # and prompt. A child with no other work cannot make it fit.
            review_unfinished = False
        unfinished = discovery_unfinished or review_unfinished
        if not unfinished and require_issue_pipeline:
            pipeline = archive.report.get("model_issue_pipeline")
            unfinished = _annual_issue_pipeline_has_executable_work(Path(project_root), pipeline)
        if not unfinished:
            investigation = archive.report.get("model_investigation")
            unfinished = _investigation_has_unfinished_items(investigation)
        if not unfinished:
            return {"can_resume": False, "reason": "recovery_nothing_to_resume", "binding": binding}
        return {"can_resume": True, "reason": None, "binding": binding, "pipeline_binding": pipeline_binding}
    except AnnualReportReadError as exc:
        return {
            "can_resume": False,
            "reason": "recovery_parent_invalid" if exc.code else "recovery_parent_invalid",
            "binding": None,
        }
    except (ModelConfigError, ModelCallAdmissionError, OSError, ValueError, KeyError, TypeError):
        return {"can_resume": False, "reason": "recovery_binding_unavailable", "binding": None}


def _annual_issue_pipeline_has_executable_work(project_root: Path, pipeline: Any) -> bool:
    """Return whether an issue pipeline can make new progress on a child run.

    A fixed supplement selection cap and a deterministic oversized wire body
    are terminal for the current unchanged source/plan. A pending in-cap shard
    remains retryable. This capability check never sends a request.
    """

    if not isinstance(pipeline, Mapping) or pipeline.get("kind") != "annual_issue_pipeline_v1":
        return False
    closure = pipeline.get("global_closure")
    if not isinstance(closure, Mapping):
        return False
    shards = pipeline.get("shards")
    if not isinstance(shards, list):
        return False

    # Use the same prompt, model, serialization, and limits as execution so a
    # body which cannot fit is not advertised as a resumable request.
    from finagent.agents import annual_issue_pipeline as issue_pipeline

    binding = pipeline.get("execution_binding")
    model = binding.get("model") if isinstance(binding, Mapping) else None
    if not isinstance(model, str) or not model:
        return False
    source_value = pipeline.get("source_identity")
    if not isinstance(source_value, Mapping):
        return False
    try:
        source = issue_pipeline._source_identity(source_value)
        issue_pipeline._validate_execution_binding(binding, source)
    except (KeyError, TypeError, ValueError):
        return False
    prompt_files = {
        "grouping": "annual_issue_grouping_v1.md",
        "global": "annual_issue_global_v1.md",
        "supplement": "annual_issue_supplement_v1.md",
    }
    limits = {
        "grouping": issue_pipeline.MAX_SHARD_REQUEST_CHARS,
        "global": issue_pipeline.MAX_GLOBAL_REQUEST_CHARS,
        "supplement": issue_pipeline.MAX_GLOBAL_REQUEST_CHARS,
    }

    def request_fits(kind: Any, body_input: Any) -> bool:
        if kind not in prompt_files or not isinstance(body_input, Mapping):
            return False
        prompt_path = Path(project_root) / "config" / "prompts" / prompt_files[kind]
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
            wire = issue_pipeline._wire_body(model, prompt, body_input)
        except (OSError, TypeError, ValueError):
            # If the exact current request cannot be reconstructed, do not
            # promise that this report can safely create a progressing child.
            return False
        return len(wire.decode("utf-8")) <= limits[kind]

    def is_permanently_oversized(shard: Mapping[str, Any]) -> bool:
        return not request_fits(shard.get("kind"), shard.get("input"))

    pending_shards = [item for item in shards if isinstance(item, Mapping) and item.get("status") == "pending"]
    for shard in pending_shards:
        if shard.get("reason") == "request_size_exceeded" or is_permanently_oversized(shard):
            continue
        # Failed, stopped-before-call, and response-invalid in-cap work may be
        # retried within the same ledger, deadline, and stage budget policies.
        return True

    existing_shard_ids = {
        item.get("shard_id") for item in shards if isinstance(item, Mapping)
    }

    if closure.get("closed") is not True or closure.get("pending_issue_ids") or closure.get("relation_gaps"):
        candidates = pipeline.get("candidates")
        if isinstance(candidates, list) and candidates:
            group_prompt = (Path(project_root) / "config" / "prompts" / "annual_issue_grouping_v1.md").read_text(encoding="utf-8")
            planned_groups = issue_pipeline._build_group_shards(
                issue_pipeline._local_exact_duplicate_groups(candidates), {}, source, model, group_prompt,
                execution_binding=binding,
            )
            for index, planned in enumerate(planned_groups):
                shard_id = f"group:{index:03d}:{planned['semantic_sha256'][:16]}"
                if shard_id not in existing_shard_ids and request_fits("grouping", planned.get("input")):
                    return True

        # A process can stop after publishing all local member decisions but
        # before publishing the cross-shard request. Rebuild that exact request
        # from the immutable local ledger to avoid a false non-resumable state.
        group_shard_count = sum(
            1 for item in shards
            if isinstance(item, Mapping) and item.get("kind") == "grouping" and item.get("status") == "accepted"
        )
        decisions = pipeline.get("member_decisions")
        issues = pipeline.get("issues")
        candidates = pipeline.get("candidates")
        if (
            group_shard_count > 1 and isinstance(decisions, list) and isinstance(issues, list)
            and isinstance(candidates, list)
            and all(isinstance(item, Mapping) and item.get("disposition") != "pending" for item in decisions)
        ):
            provisional = [item for item in issues if isinstance(item, Mapping) and item.get("status") == "provisional"]
            if provisional:
                global_input = issue_pipeline._build_global_input(provisional, decisions, candidates, source)
                global_sha = issue_pipeline._semantic_sha(global_input)
                global_id = f"global:{global_sha[:24]}"
                if global_id not in existing_shard_ids and request_fits("global", global_input):
                    return True
        return False

    retrieval = pipeline.get("retrieval_coverage")
    if not isinstance(retrieval, Mapping):
        return False
    scope = retrieval.get("scope")
    role_pending = retrieval.get("role_assignments_pending")
    role_total = retrieval.get("role_assignments_total")
    role_requested = retrieval.get("role_assignments_requested")
    if type(role_pending) is not int or type(role_total) is not int or type(role_requested) is not int:
        return False
    if scope == "not_started":
        # A closed set with no retrieval checkpoint can still run the bounded
        # local source scan and construct the first supplement request.
        return bool(pipeline.get("issues"))

    supplement_shards = [item for item in shards if isinstance(item, Mapping) and item.get("kind") == "supplement"]
    if any(item.get("status") == "pending" for item in supplement_shards):
        # In-cap pending requests were handled above. The remaining pending
        # shard(s) are permanently oversized and cannot be retried.
        return False
    accepted_supplements = [item for item in supplement_shards if item.get("status") == "accepted"]
    if accepted_supplements:
        # The runner always submits the first MAX_SUPPLEMENT_PAIRS in stable
        # retrieval order. An accepted response plus a nonempty tail means the
        # same first slice would be reused on every child; no later pair can
        # enter the request under this fixed selection rule.
        if (
            role_total > role_requested
            and role_requested == issue_pipeline.MAX_SUPPLEMENT_PAIRS
        ):
            return False
        # A typed accepted result already resolved the complete submitted
        # slice. Pending role cells after that point are outside the stable
        # request or uncovered by the current implementation.
        return False

    # A hard stop can land after the role plan is published but before its
    # shard. In that checkpoint, role_assignments_pending means the stable
    # selection tail only; actual assigned rows still have role_status=pending.
    supplement_rows = pipeline.get("supplements")
    issues = pipeline.get("issues")
    if isinstance(supplement_rows, list) and any(
        isinstance(item, Mapping) and item.get("role_status") == "pending"
        for item in supplement_rows
    ):
        if not isinstance(issues, list):
            return False
        supplement_input = issue_pipeline._supplement_input(issues, supplement_rows, source)
        return role_requested > 0 and request_fits("supplement", supplement_input)
    return role_pending > 0 and role_requested > 0


def build_annual_recovery_binding(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
) -> dict[str, Any]:
    return _build_annual_recovery_binding(
        project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
        prompt_version="annual_discovery_v5",
    )


def _build_annual_recovery_binding(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None,
    prompt_version: str,
) -> dict[str, Any]:
    """Build the exact semantic identity used by CLI and job capability checks."""

    mode = request.get("mode")
    if mode not in _MODES:
        raise ValueError("recovery_mode_invalid")
    source_sha = str(request.get("sha256", "")).lower()
    if _SHA256.fullmatch(source_sha) is None:
        raise ValueError("recovery_source_invalid")
    source_record = _source_record(Path(project_root), request)
    if prompt_version not in {"annual_discovery_v3", "annual_discovery_v4", "annual_discovery_v5"}:
        raise ValueError("recovery_prompt_version_invalid")
    prompt_path = Path(__file__).resolve().parents[4] / "config" / "prompts" / f"{prompt_version}.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    context = build_annual_discovery_context(
        report,
        _source_pdf_path(Path(project_root), request),
        source_record,
        system_prompt=prompt,
        model_name=settings.model,
        plan_version=PERIOD_HEADER_PLAN_VERSION if prompt_version == "annual_discovery_v5" else COMPACT_PLAN_VERSION,
        parsed_pdf=parsed_pdf,
    )
    return build_annual_discovery_binding_from_context(
        context, settings=settings, report=report, analysis_mode=mode,
        prompt_version=prompt_version,
    )


def build_annual_discovery_binding_from_context(
    context: AnnualDiscoveryContext, *, settings: ModelSettings,
    report: Mapping[str, Any], analysis_mode: str,
    prompt_version: str = "annual_discovery_v5",
) -> dict[str, Any]:
    """Bind an already validated source plan, independent of source directory layout."""
    if analysis_mode not in _MODES or prompt_version not in {"annual_discovery_v3", "annual_discovery_v4", "annual_discovery_v5"}:
        raise ValueError("recovery_mode_or_prompt_invalid")
    current = prompt_version == "annual_discovery_v5"
    plan_version = PERIOD_HEADER_PLAN_VERSION if current else COMPACT_PLAN_VERSION
    if context.plan_version != plan_version:
        raise ValueError("recovery_prompt_plan_mismatch")
    identity = context.identity
    prompt_path = Path(__file__).resolve().parents[4] / "config" / "prompts" / f"{prompt_version}.md"
    binding = {
        "source_sha256": identity["source_sha256"],
        "company_id": identity["company_id"],
        "report_year": identity["report_year"],
        "source_document_id": identity["source_document_id"],
        "analysis_mode": analysis_mode,
        "provider_model_fingerprint": provider_model_fingerprint(settings.base_url, settings.model),
        "model": settings.model,
        "prompts_sha256": _prompts_sha256(prompt_version=prompt_version),
        "parser_version": _PERIOD_HEADER_PARSER_VERSION if current else _PARSER_VERSION,
        "plan_version": plan_version,
        "execution_version": "execution_v5" if current else "execution_v4",
        "plan_sha256": _sha256_json(annual_discovery_plan_projection(context)),
        "m3_binding_sha256": _m3_semantic_binding_sha256(report.get("m3_screening"), {
            **dict(identity), "sha256": identity["source_sha256"],
            "document_id": identity["source_document_id"], "mode": analysis_mode,
        }) if analysis_mode == "integrated_analysis" else None,
    }
    if prompt_version in {"annual_discovery_v4", "annual_discovery_v5"}:
        binding.update(prompt_version=prompt_version, prompt_sha256=hashlib.sha256(prompt_path.read_bytes()).hexdigest())
    return binding


def _issue_pipeline_business_binding(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
    prompt_version: str,
    discovery_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared issue-pipeline business fields without kind, version, or budget policy."""

    if discovery_binding is not None:
        discovery_binding = dict(discovery_binding)
    else:
        discovery_binding = _build_annual_recovery_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version=prompt_version,
        )
    root = Path(__file__).resolve().parents[4]
    new_prompt_hashes: dict[str, str] = {}
    for name in _ISSUE_PIPELINE_PROMPTS:
        new_prompt_hashes[name] = hashlib.sha256((root / "config" / "prompts" / name).read_bytes()).hexdigest()
    required_v4_fields = {
        "source_sha256", "company_id", "report_year", "source_document_id", "analysis_mode",
        "provider_model_fingerprint", "model", "prompts_sha256", "parser_version",
        "plan_version", "execution_version", "plan_sha256", "m3_binding_sha256",
    }
    current = prompt_version == "annual_discovery_v5"
    if prompt_version in {"annual_discovery_v4", "annual_discovery_v5"}:
        required_v4_fields |= {"prompt_version", "prompt_sha256"}
    if (
        set(discovery_binding) != required_v4_fields
        or discovery_binding.get("execution_version") != ("execution_v5" if current else "execution_v4")
        or discovery_binding.get("plan_version") != (PERIOD_HEADER_PLAN_VERSION if current else COMPACT_PLAN_VERSION)
        or discovery_binding.get("parser_version") != (_PERIOD_HEADER_PARSER_VERSION if current else _PARSER_VERSION)
        or discovery_binding.get("analysis_mode") not in _MODES
        or discovery_binding.get("company_id") != request.get("company_id")
        or discovery_binding.get("report_year") != request.get("report_year")
        or discovery_binding.get("source_document_id") != request.get("document_id")
        or discovery_binding.get("analysis_mode") != request.get("mode")
        or discovery_binding.get("source_sha256") != str(request.get("sha256", "")).lower()
        or any(
            not isinstance(discovery_binding.get(field), str)
            or _SHA256.fullmatch(discovery_binding[field]) is None
            for field in ("source_sha256", "provider_model_fingerprint", "prompts_sha256", "plan_sha256")
        )
        or not isinstance(discovery_binding.get("model"), str)
        or not discovery_binding["model"].strip()
        or discovery_binding.get("m3_binding_sha256") is not None
        and (
            not isinstance(discovery_binding.get("m3_binding_sha256"), str)
            or _SHA256.fullmatch(discovery_binding["m3_binding_sha256"]) is None
        )
        or discovery_binding.get("analysis_mode") == "model_investigation"
        and discovery_binding.get("m3_binding_sha256") is not None
        or discovery_binding.get("analysis_mode") == "integrated_analysis"
        and discovery_binding.get("m3_binding_sha256") is None
    ):
        raise ValueError("recovery_v4_binding_invalid")
    return {
        "source_sha256": discovery_binding["source_sha256"],
        "company_id": discovery_binding["company_id"],
        "report_year": discovery_binding["report_year"],
        "source_document_id": discovery_binding["source_document_id"],
        "analysis_mode": discovery_binding["analysis_mode"],
        "provider_model_fingerprint": discovery_binding["provider_model_fingerprint"],
        "model": discovery_binding["model"],
        "discovery_binding": discovery_binding,
        "prompts_sha256": _sha256_json({
            ("unchanged_execution_v5_prompt_set_sha256" if current else "unchanged_execution_v4_prompt_set_sha256"): discovery_binding["prompts_sha256"],
            "issue_pipeline_prompt_sha256": new_prompt_hashes,
        }),
        "parser_version": "annual-issue-pipeline-parser-v1-execution-v5",
        "issue_pipeline_version": "annual_issue_pipeline_v1",
        "issue_wire_version": ISSUE_INPUT_WIRE_SCHEMA,
        "grouping_prompt_version": "annual_issue_grouping_v1",
        "review_prompt_version": "annual_review_v3",
        "review_wire_version": V3_WIRE_SCHEMA,
        "review_input_projection_version": V3_INPUT_PROJECTION_VERSION,
        "plan_version": discovery_binding["plan_version"],
        "plan_sha256": discovery_binding["plan_sha256"],
        "m3_binding_sha256": discovery_binding["m3_binding_sha256"],
    }


def build_annual_issue_pipeline_binding_v5(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
) -> dict[str, Any]:
    """Legacy execution_v5 reconstruction binding. Activity must not call this."""

    return {
        "kind": "fintrace_annual_execution_binding_v5",
        "execution_version": "execution_v5",
        **_issue_pipeline_business_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version="annual_discovery_v3",
        ),
    }


def build_annual_issue_pipeline_binding_v6(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
) -> dict[str, Any]:
    """Historical execution_v6 reconstruction. Activity must not call this."""

    archived = archived_budget_contract_v2(scope="new_run")
    return {
        "kind": "fintrace_annual_execution_binding_v6",
        "execution_version": "execution_v6",
        **_issue_pipeline_business_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version="annual_discovery_v3",
        ),
        "budget_contract_version": archived["schema_version"],
        "review_request_limit_chars": archived["review_request_limit_chars"],
        "model_call_timeout_seconds": archived["model_call_timeout_seconds"],
    }


def build_annual_issue_pipeline_binding_v7(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
) -> dict[str, Any]:
    """Historical execution_v7 reconstruction. Activity must not call this."""

    return {
        "kind": "fintrace_annual_execution_binding_v7",
        "execution_version": "execution_v7",
        **_issue_pipeline_business_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version="annual_discovery_v3",
        ),
        "budget_contract_version": archived_budget_contract_v3(scope="new_run")["schema_version"],
        "review_request_limit_chars": archived_budget_contract_v3(scope="new_run")["review_request_limit_chars"],
        "model_call_timeout_seconds": archived_budget_contract_v3(scope="new_run")["model_call_timeout_seconds"],
    }


def build_annual_issue_pipeline_binding_v8(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
    discovery_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Historical v8 reconstruction; activity must not use its frozen v3 budget."""

    archived = archived_budget_contract_v3(scope="new_run")
    return {
        "kind": "fintrace_annual_execution_binding_v8",
        "execution_version": "execution_v8",
        **_issue_pipeline_business_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version="annual_discovery_v4", discovery_binding=discovery_binding,
        ),
        "supplement_retrieval_version": "source_anchor_then_ngram_v2",
        "budget_contract_version": archived["schema_version"],
        "review_request_limit_chars": archived["review_request_limit_chars"],
        "model_call_timeout_seconds": archived["model_call_timeout_seconds"],
    }


def build_annual_issue_pipeline_binding_v9(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
    discovery_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Historical v9 reconstruction with its unchanged frozen v4 capacity policy."""

    archived = archived_budget_contract_v4(scope="new_run")
    return {
        "kind": "fintrace_annual_execution_binding_v9",
        "execution_version": "execution_v9",
        **_issue_pipeline_business_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version="annual_discovery_v4", discovery_binding=discovery_binding,
        ),
        "supplement_retrieval_version": "source_anchor_then_ngram_v2",
        "budget_contract_version": archived["schema_version"],
        "global_request_limit_chars": archived["global_request_limit_chars"],
        "review_request_limit_chars": archived["review_request_limit_chars"],
        "model_call_timeout_seconds": archived["model_call_timeout_seconds"],
    }


def build_annual_issue_pipeline_binding_v10(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
    discovery_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Historical v10 reconstruction; its original shards and budgets are frozen."""
    archived = archived_budget_contract_v4(scope="new_run")

    return {
        "kind": "fintrace_annual_execution_binding_v10",
        "execution_version": "execution_v10",
        **_issue_pipeline_business_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version="annual_discovery_v4", discovery_binding=discovery_binding,
        ),
        "supplement_retrieval_version": "source_anchor_then_ngram_v2",
        "budget_contract_version": archived["schema_version"],
        "global_request_limit_chars": archived["global_request_limit_chars"],
        "review_request_limit_chars": archived["review_request_limit_chars"],
        "model_call_timeout_seconds": archived["model_call_timeout_seconds"],
        "grouping_shard_plan_version": "member_bounded_v2",
        "grouping_shard_max_members": 3,
    }


def build_annual_issue_pipeline_binding_v11(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
    discovery_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Activity identity for complete same-page period context, unchanged budgets."""
    from finagent.agents.annual_issue_pipeline import GROUPING_SHARD_PLAN_VERSION, MAX_FINDINGS_PER_SHARD

    return {
        "kind": "fintrace_annual_execution_binding_v11",
        "execution_version": "execution_v11",
        **_issue_pipeline_business_binding(
            project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf,
            prompt_version="annual_discovery_v5", discovery_binding=discovery_binding,
        ),
        "supplement_retrieval_version": "source_anchor_then_ngram_v2",
        "budget_contract_version": BUDGET_CONTRACT_VERSION,
        "global_request_limit_chars": GLOBAL_REQUEST_LIMIT_CHARS,
        "review_request_limit_chars": REVIEW_REQUEST_LIMIT_CHARS,
        "model_call_timeout_seconds": MODEL_CALL_TIMEOUT_SECONDS,
        "grouping_shard_plan_version": GROUPING_SHARD_PLAN_VERSION,
        "grouping_shard_max_members": MAX_FINDINGS_PER_SHARD,
    }




def recovery_identity_sha256(binding: Mapping[str, Any]) -> str:
    return _sha256_json(binding)


def annual_discovery_plan_sha256(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
    parsed_pdf: object | None = None,
) -> str:
    return str(build_annual_recovery_binding(
        project_root, request, settings=settings, report=report, parsed_pdf=parsed_pdf
    )["plan_sha256"])


def _source_pdf_path(root: Path, request: Mapping[str, Any]) -> Path:
    relative = request.get("source_pdf_path")
    if not isinstance(relative, str) or "\\" in relative or relative.startswith("/"):
        raise ValueError("recovery_source_invalid")
    parts = Path(relative).parts
    if (
        len(parts) != 4
        or any(part in {"", ".", ".."} for part in parts)
        or parts[:3] != (request.get("company_id"), str(request.get("report_year")), request.get("document_id"))
        or not parts[3].lower().endswith(".pdf")
    ):
        raise ValueError("recovery_source_invalid")
    candidate = (root / "data" / "raw" / Path(*parts)).resolve(strict=True)
    if not candidate.is_relative_to((root / "data" / "raw").resolve(strict=True)):
        raise ValueError("recovery_source_invalid")
    return candidate


def _source_record(root: Path, request: Mapping[str, Any]) -> dict[str, Any]:
    source = _source_pdf_path(root, request)
    record_path = source.parent / "source.json"
    value = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("recovery_source_record_invalid")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != str(request.get("sha256", "")).lower():
        raise ValueError("recovery_source_mismatch")
    return value


def _prompts_sha256(*, prompt_version: str = "annual_discovery_v5") -> str:
    root = Path(__file__).resolve().parents[4]
    material: dict[str, str] = {}
    for old_name in _PROMPTS:
        name = f"{prompt_version}.md" if old_name == "annual_discovery_v3.md" else old_name
        path = root / "config" / "prompts" / name
        content = path.read_text(encoding="utf-8").encode("utf-8") if prompt_version == "annual_discovery_v3" else path.read_bytes()
        material[name] = hashlib.sha256(content).hexdigest()
    return _sha256_json(material)


def _m3_semantic_binding_sha256(value: object, request: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("recovery_m3_missing")
    facts: list[dict[str, Any]] = []
    for section_name in ("extraction", "explicit_unit_facts"):
        section = value.get(section_name)
        rows = section.get("facts") if isinstance(section, Mapping) else None
        if not isinstance(rows, list):
            continue
        for fact in rows:
            if not isinstance(fact, Mapping):
                continue
            fields = (
                "metric_id", "source_document_id", "source_sha256", "company_id",
                "report_year", "period_start", "period_end", "period_type",
                "frequency", "comparison_role", "raw_value", "normalized_value",
                "unit_multiplier", "unit", "currency", "scope", "statement_type",
                "restatement_status",
            )
            facts.append({key: fact.get(key) for key in fields})
    facts.sort(key=lambda row: _canonical_json(row))
    projection = {
        "kind": "fintrace_m3_semantic_binding_v1",
        "source_sha256": str(request.get("sha256", "")).lower(),
        "company_id": request.get("company_id"),
        "report_year": request.get("report_year"),
        "source_document_id": request.get("document_id"),
        "analysis_mode": request.get("mode"),
        "rule_version": value.get("rule_version"),
        "facts": facts,
    }
    return _sha256_json(projection)


_RETRYABLE_INVESTIGATION_REASONS = frozenset({
    "model_call_failed",
    "invalid_model_json",
    "invalid_model_response",
    "prompt_build_failed",
    "model_call_budget_exhausted",
    "model_call_budget_exhausted_before_supplement",
    "supplementary_retrieval_failed",
    "supplementary_retrieval_exhausted",
    "no_valid_same_source_citation",
    "http_401",
    "http_403",
    "ledger_config_invalid",
    "ledger_source_context_missing_or_unapproved",
    "ledger_unavailable",
    "ledger_provider_mismatch",
    "ledger_budget_exhausted",
    "ledger_policy_mismatch",
    "call_gate_closed",
    "call_deadline_exhausted",
    "audit_write_failed",
    "cancelled",
})


def _investigation_has_unfinished_items(value: object) -> bool:
    if not isinstance(value, Mapping):
        return True
    if value.get("status") not in {"completed", "abstained"}:
        return True
    items = value.get("items")
    if not isinstance(items, list):
        return True
    for item in items:
        if not isinstance(item, Mapping):
            return True
        if item.get("status") == "abstained" and item.get("reason") in _RETRYABLE_INVESTIGATION_REASONS:
            return True
    return False


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()
