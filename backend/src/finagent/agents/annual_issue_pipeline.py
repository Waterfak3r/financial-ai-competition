"""Strict, resumable issue grouping for the execution_v5 annual workflow.

This layer keeps every accepted discovery finding and its source quote in a
local candidate ledger. Model responses can only assign typed dispositions to
that closed input set; they cannot invent IDs, evidence, facts, or coverage.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from finagent.audit import EvidenceRef, audited_complete_chat
from finagent.audit.audited_chat import annual_call_deadline
from finagent.agents.annual_review_wire import (
    ISSUE_INPUT_WIRE_SCHEMA,
    V3_INPUT_PROJECTION_VERSION,
    V3_WIRE_SCHEMA,
    decode_annual_issue_input_wire,
    encode_annual_issue_input_wire,
)
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage, serialize_chat_body
from finagent.retrieval.annual_issue_supplements import (
    retrieve_annual_issue_supplements as _retrieve_supplements,
    selected_source_char_union_size as _selected_source_char_union_size,
    section_from_heading as _section_from_heading,
    supplement_evidence_id as _supplement_evidence_id,
)
from finagent.schemas.text_pdf import ParsedTextPdf

PIPELINE_KIND = "annual_issue_pipeline_v1"
ISSUE_WIRE_VERSION = ISSUE_INPUT_WIRE_SCHEMA
GROUP_PROMPT_VERSION = "annual_issue_grouping_v1"
MAX_SHARD_REQUEST_CHARS = 22_000
MAX_GLOBAL_REQUEST_CHARS = 24_000
MAX_REVIEW_REQUEST_CHARS = 24_000
MAX_FINDINGS_PER_SHARD = 12
MAX_SUPPLEMENT_PAIRS = 32
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_issue_grouping_v1.md"


def initial_issue_pipeline(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Create the durable empty state before discovery has produced findings."""

    source = _source_identity(identity)
    return {
        "kind": PIPELINE_KIND,
        "status": "pending",
        "reason": "waiting_discovery",
        "source_identity": source,
        "execution_binding": None,
        "input": {
            "discovery_prompt_version": "annual_discovery_v3",
            "discovery_status": "not_started",
            "discovery_coverage_complete": False,
            "finding_ids": [],
            "finding_count": 0,
            "finding_set_sha256": _sha_json([]),
            "candidate_count": 0,
        },
        "candidates": [],
        "issues": [],
        "member_decisions": [],
        "supplements": [],
        "retrieval_coverage": _empty_retrieval_coverage(source),
        "shards": [],
        "global_closure": _empty_global_closure("waiting_discovery"),
        "audit_summary": {"attempt_count": 0, "response_count": 0, "reused_count": 0, "model_called": False, "audit_artifacts": []},
    }


def checkpoint_issue_pipeline(
    identity: Mapping[str, Any],
    discovery: Mapping[str, Any],
    *,
    execution_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project every currently accepted discovery finding as pending locally."""

    source = _source_identity(identity)
    findings, evidences = _validated_findings(discovery, source)
    coverage = discovery.get("coverage") if isinstance(discovery.get("coverage"), Mapping) else {}
    finding_ids = [str(item["finding_id"]) for item in findings]
    candidates = [_candidate_from_finding(item, evidences, source) for item in findings]
    candidate_groups = _local_exact_duplicate_groups(candidates)
    decisions = [
        {
            "finding_id": finding_id,
            "disposition": "pending",
            "issue_id": None,
            "target_finding_id": None,
            "reason_code": "awaiting_grouping",
            "reason_evidence_id": None,
            "reason_quote": None,
            "source_finding_id": finding_id,
        }
        for finding_id in finding_ids
    ]
    input_record = {
        "discovery_prompt_version": discovery.get("prompt_version", "annual_discovery_v3"),
        "discovery_status": discovery.get("status", "partial"),
        "discovery_coverage_complete": coverage.get("complete") is True,
        "finding_ids": finding_ids,
        "finding_count": len(finding_ids),
        "finding_set_sha256": _sha_json(candidates),
        "candidate_count": len(candidate_groups),
    }
    pipeline = initial_issue_pipeline(source)
    no_discovery_results_yet = (
        discovery.get("status") == "running"
        and not finding_ids
        and discovery.get("model_call_attempt_count", 0) == 0
        and discovery.get("model_call_count", 0) == 0
    )
    pipeline.update(
        status="pending",
        reason="waiting_discovery" if no_discovery_results_yet else "discovery_in_progress" if discovery.get("status") == "running" else "awaiting_grouping",
        execution_binding=dict(execution_binding) if isinstance(execution_binding, Mapping) else None,
        input=input_record,
        candidates=candidates,
        member_decisions=decisions,
    )
    pipeline["retrieval_coverage"] = _empty_retrieval_coverage(source)
    pipeline["global_closure"] = _pending_global_closure(pipeline, [], "awaiting_grouping")
    return pipeline


def run_annual_issue_pipeline(
    discovery: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    parsed_pdf: ParsedTextPdf | None,
    settings: ModelSettings,
    run_dir: Path,
    artifacts_root: Path,
    execution_binding: Mapping[str, Any],
    scope_gaps: Sequence[str] = (),
    parent_pipeline: Mapping[str, Any] | None = None,
    parent_run_id: str | None = None,
    deadline: float | None = None,
    main_soft_deadline: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
    checkpoint_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Group all known findings, optionally supplement them, and close globally."""

    pipeline_started = time.monotonic()
    grouping_started: float | None = None
    grouping_finished: float | None = None
    global_started: float | None = None
    global_finished: float | None = None
    supplements_started: float | None = None
    supplements_finished: float | None = None
    source = _source_identity(identity)
    state = checkpoint_issue_pipeline(identity, discovery, execution_binding=execution_binding)
    state["global_closure"]["coverage_gaps"] = [item for item in scope_gaps if isinstance(item, str)]
    state["status"] = "running"
    state["reason"] = "grouping_in_progress"

    def publish_current() -> None:
        now = time.monotonic()
        timing: dict[str, Any] = {
            "schema_version": "annual_issue_pipeline_phase_timings_v1",
            "annual_issue_grouping_elapsed_seconds": round(max(0.0, now - pipeline_started), 3),
        }
        if grouping_started is not None:
            timing["grouping_elapsed_seconds"] = round(max(0.0, (grouping_finished or now) - grouping_started), 3)
        if global_started is not None:
            timing["global_reconciliation_elapsed_seconds"] = round(max(0.0, (global_finished or now) - global_started), 3)
        if supplements_started is not None:
            timing["supplements_elapsed_seconds"] = round(max(0.0, (supplements_finished or now) - supplements_started), 3)
        state["phase_timings"] = timing
        state["audit_summary"] = _audit_summary_from_shards(state.get("shards", []))
        _publish(state, checkpoint_callback)

    publish_current()
    grouping_started = time.monotonic()
    findings, evidence_map = _validated_findings(discovery, source)
    ids = [str(item["finding_id"]) for item in findings]
    candidates = state["candidates"]
    candidate_groups = _local_exact_duplicate_groups(candidates)
    parent_shards = _parent_shards(parent_pipeline)
    accepted_shards: list[dict[str, Any]] = []
    finding_decisions: dict[str, dict[str, Any]] = {}
    fatal_stop_reason: str | None = None
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    shard_inputs = _build_group_shards(candidate_groups, evidence_map, source, settings.model, prompt)

    for shard_index, shard_input in enumerate(shard_inputs):
        shard_id = f"group:{shard_index:03d}:{shard_input['semantic_sha256'][:16]}"
        if _stop_reason(deadline, main_soft_deadline, cancel_check):
            accepted_shards.append(_pending_shard(shard_id, shard_input, "grouping_stopped_before_shard"))
            continue
        old = parent_shards.get(shard_id)
        if old is not None and _try_reuse_shard(old, shard_input, source, artifacts_root, "grouping"):
            result = dict(old["result"])
            audit = dict(old["audit"])
            accepted_shards.append(_accepted_shard(shard_id, shard_input, result, audit, reused=True, parent_run_id=parent_run_id))
            for decision in result["decisions"]:
                finding_decisions[decision["finding_id"]] = dict(decision)
            state["shards"] = accepted_shards
            _apply_group_decisions(state, finding_decisions, candidates, source)
            publish_current()
            continue
        try:
            result, audit, call_attempts, call_responses = _call_typed_shard(
                shard_input,
                settings=settings,
                run_dir=run_dir,
                artifacts_root=artifacts_root,
                prompt=prompt,
                prompt_version=GROUP_PROMPT_VERSION,
                call_stage="annual_issue_grouping",
                kind="grouping",
                deadline=deadline,
            )
        except _DurableShardError as exc:
            audit = exc.audit
            accepted_shards.append(_pending_shard(shard_id, shard_input, exc.code, audit=audit, attempts=exc.attempts, responses=exc.responses))
            if exc.code in {"http_401", "http_403", "cancelled", "call_gate_closed", "ledger_config_invalid", "ledger_source_context_missing_or_unapproved", "ledger_unavailable", "ledger_provider_mismatch", "ledger_budget_exhausted", "task_budget_exhausted", "ledger_policy_mismatch", "audit_write_failed"}:
                fatal_stop_reason = exc.code
        except Exception:
            accepted_shards.append(_pending_shard(shard_id, shard_input, "grouping_call_failed"))
        else:
            accepted_shards.append(_accepted_shard(shard_id, shard_input, result, audit, reused=False, parent_run_id=None, attempt_count=call_attempts, response_count=call_responses))
            for decision in result["decisions"]:
                finding_decisions[decision["finding_id"]] = dict(decision)
        state["shards"] = accepted_shards
        _apply_group_decisions(state, finding_decisions, candidates, source)
        publish_current()
        if fatal_stop_reason is not None:
            for rest_index, rest_input in enumerate(shard_inputs[shard_index + 1 :], start=shard_index + 1):
                rest_id = f"group:{rest_index:03d}:{rest_input['semantic_sha256'][:16]}"
                accepted_shards.append(_pending_shard(rest_id, rest_input, fatal_stop_reason, kind="grouping"))
            state["shards"] = accepted_shards
            _apply_group_decisions(state, finding_decisions, candidates, source)
            publish_current()
            break

    # Every member must have exactly one accepted local disposition. An invalid
    # shard contributes no decisions, leaving its entire input pending.
    _apply_group_decisions(state, finding_decisions, candidates, source)
    provisional_issues = [dict(item) for item in state["issues"] if item.get("status") == "provisional"]
    group_complete = len(finding_decisions) == len(ids) and all(item in finding_decisions for item in ids)
    state["status"] = "running" if group_complete else "partial"
    state["reason"] = None if group_complete else "grouping_members_pending"
    state["global_closure"] = _pending_global_closure(state, provisional_issues, "awaiting_global_reconciliation")
    grouping_finished = time.monotonic() if grouping_started is not None else None

    global_result: dict[str, Any] | None = None
    global_audit: dict[str, Any] | None = None
    global_attempts = 0
    global_responses = 0
    global_reused = False
    if group_complete and len(shard_inputs) > 1 and fatal_stop_reason is None and not _stop_reason(deadline, main_soft_deadline, cancel_check):
        global_started = time.monotonic()
        global_input = _build_global_input(provisional_issues, state["member_decisions"], candidates, source)
        global_prompt = _load_group_prompt("annual_issue_global_v1.md")
        global_semantic_sha = _semantic_sha(global_input)
        global_shard_id = f"global:{global_semantic_sha[:24]}"
        old = parent_shards.get(global_shard_id)
        if old is not None and _try_reuse_shard(old, {"input": global_input, "semantic_sha256": global_semantic_sha, "wire_body_sha256": _wire_hash(settings.model, global_prompt, global_input)}, source, artifacts_root, "global"):
            global_result = dict(old["result"])
            global_audit = dict(old["audit"])
            global_reused = True
        else:
            try:
                global_result, global_audit, call_attempts, call_responses = _call_typed_shard(
                    {"input": global_input, "semantic_sha256": global_semantic_sha, "wire_body_sha256": _wire_hash(settings.model, global_prompt, global_input)},
                    settings=settings,
                    run_dir=run_dir,
                    artifacts_root=artifacts_root,
                    prompt=global_prompt,
                    prompt_version="annual_issue_global_v1",
                    call_stage="annual_issue_grouping",
                    kind="global",
                    deadline=deadline,
                )
                global_attempts, global_responses = call_attempts, call_responses
            except _DurableShardError as exc:
                global_audit = exc.audit
                accepted_shards.append(_pending_shard(global_shard_id, _seal_input(global_input, settings.model, global_prompt), exc.code, audit=global_audit, attempts=exc.attempts, responses=exc.responses, kind="global"))
                if exc.code in {"http_401", "http_403", "cancelled", "call_gate_closed", "ledger_config_invalid", "ledger_source_context_missing_or_unapproved", "ledger_unavailable", "ledger_provider_mismatch", "ledger_budget_exhausted", "task_budget_exhausted", "ledger_policy_mismatch", "audit_write_failed"}:
                    fatal_stop_reason = exc.code
            except Exception:
                accepted_shards.append(_pending_shard(global_shard_id, _seal_input(global_input, settings.model, global_prompt), "global_result_invalid", kind="global"))
        if global_result is not None:
            global_shard = _accepted_shard(global_shard_id, {"input": global_input, "semantic_sha256": global_semantic_sha, "wire_body_sha256": _wire_hash(settings.model, global_prompt, global_input)}, global_result, global_audit or {}, reused=global_reused, parent_run_id=parent_run_id, attempt_count=global_attempts, response_count=global_responses)
            accepted_shards.append(global_shard)
            state["shards"] = accepted_shards
            _apply_global_decisions(state, global_result, provisional_issues)
            publish_current()
        elif not any(item.get("shard_id") == global_shard_id for item in accepted_shards):
            reason = _stop_reason(deadline, main_soft_deadline, cancel_check) or "global_reconciliation_pending"
            accepted_shards.append(_pending_shard(global_shard_id, _seal_input(global_input, settings.model, global_prompt), reason, kind="global"))
            state["shards"] = accepted_shards
            publish_current()

    global_finished = time.monotonic() if global_started is not None else None

    if len(shard_inputs) > 1 and group_complete and global_result is None:
        state["global_closure"] = _pending_global_closure(
            state, provisional_issues,
            fatal_stop_reason or _stop_reason(deadline, main_soft_deadline, cancel_check) or "global_reconciliation_pending",
        )
    elif len(shard_inputs) == 0 and not ids and group_complete:
        # Completed empty discovery closes the known empty set; partial no-hit
        # discovery remains coverage-incomplete and cannot pass no_priority.
        candidate_by_finding = {item["finding_id"]: item for item in state["candidates"]}
        input_member_evidence = sorted(
            (finding_id, evidence_id)
            for finding_id, candidate in candidate_by_finding.items()
            for evidence_id in candidate.get("evidence_ids", [])
        )
        excluded_ids = [item["finding_id"] for item in state["member_decisions"] if item["disposition"] == "exclude"]
        state["global_closure"] = {
            "closed": True,
            "closure_method": "empty_discovery",
            "scope": "current_known_findings",
            "input_issue_ids": [],
            "decision_count": 0,
            "pending_issue_ids": [],
            "excluded_finding_ids": excluded_ids,
            "relation_gaps": [],
            "coverage_gaps": list(scope_gaps),
            "input_finding_ids": list(state["input"]["finding_ids"]),
            "retained_finding_ids": [],
            "pending_finding_ids": [],
            "input_member_evidence": [list(pair) for pair in input_member_evidence],
            "retained_member_evidence": [],
            "excluded_member_evidence": [list(pair) for pair in input_member_evidence if pair[0] in set(excluded_ids)],
            "excluded_issue_account": [],
            "reason": None,
            "discovery_coverage_complete": False,
            "no_priority_allowed": False,
        }
    elif group_complete and len(shard_inputs) == 1 and fatal_stop_reason is None:
        # The single semantic grouping shard contains the whole candidate set
        # and resolves all possible within-shard relationships, including a
        # complete all-excluded ledger with no surviving issue records.
        _close_single_group_shard_scope(state, provisional_issues, scope_gaps)
    elif global_result is None and provisional_issues:
        state["global_closure"] = _pending_global_closure(
            state, provisional_issues,
            fatal_stop_reason or _stop_reason(deadline, main_soft_deadline, cancel_check) or "global_reconciliation_pending",
        )

    if state["global_closure"].get("closed") is True and isinstance(parsed_pdf, ParsedTextPdf) and parsed_pdf.document_id == source["source_document_id"] and parsed_pdf.source_sha256 == source["source_sha256"] and fatal_stop_reason is None and not _stop_reason(deadline, main_soft_deadline, cancel_check):
        supplements_started = time.monotonic()
        supplements, retrieval_coverage = _retrieve_supplements(
            state["issues"], parsed_pdf, source, max_role_assignments=MAX_SUPPLEMENT_PAIRS,
        )
        requested_pairs = [
            (issue_id, item["evidence_id"])
            for item in supplements for issue_id in item["issue_ids"]
        ]
        requested_pairs = requested_pairs[:MAX_SUPPLEMENT_PAIRS]
        requested_pair_set = set(requested_pairs)
        retrieval_coverage["role_assignments_total"] = sum(len(item["issue_ids"]) for item in supplements)
        retrieval_coverage["role_assignments_requested"] = len(requested_pairs)
        retrieval_coverage["role_assignments_pending"] = max(0, retrieval_coverage["role_assignments_total"] - len(requested_pairs))
        state["supplements"] = supplements
        state["retrieval_coverage"] = retrieval_coverage
        publish_current()
        if supplements:
            supplement_prompt = _load_group_prompt("annual_issue_supplement_v1.md")
            supplement_input = _supplement_input(state["issues"], supplements, source)
            sem = _semantic_sha(supplement_input)
            supp_id = f"supplement:{sem[:24]}"
            old = parent_shards.get(supp_id)
            supp_audit = None
            supp_attempts = 0
            supp_responses = 0
            reused_supp = False
            if old is not None and _try_reuse_shard(old, {"input": supplement_input, "semantic_sha256": sem, "wire_body_sha256": _wire_hash(settings.model, supplement_prompt, supplement_input)}, source, artifacts_root, "supplement"):
                supp_result = dict(old["result"])
                supp_audit = dict(old["audit"])
                reused_supp = True
            else:
                try:
                    supp_result, supp_audit, call_attempts, call_responses = _call_typed_shard(
                        {"input": supplement_input, "semantic_sha256": sem, "wire_body_sha256": _wire_hash(settings.model, supplement_prompt, supplement_input)},
                        settings=settings,
                        run_dir=run_dir,
                        artifacts_root=artifacts_root,
                        prompt=supplement_prompt,
                        prompt_version="annual_issue_supplement_v1",
                        call_stage="annual_issue_grouping",
                        kind="supplement",
                        deadline=deadline,
                    )
                    supp_attempts, supp_responses = call_attempts, call_responses
                except _DurableShardError as exc:
                    supp_attempts, supp_responses = exc.attempts, exc.responses
                    supp_audit = exc.audit
                    supp_result = None
                except Exception:
                    supp_audit = None
                    supp_result = None
            if supp_result is not None:
                _apply_supplement_roles(state["supplements"], supp_result)
                for supplement in state["supplements"]:
                    assigned = {
                        (issue_id, supplement["evidence_id"])
                        for issue_id in supplement["issue_ids"]
                    }
                    supplement["role_status"] = "accepted" if assigned and assigned.issubset(requested_pair_set) else "pending"
                accepted_shards.append(_accepted_shard(supp_id, {"input": supplement_input, "semantic_sha256": sem, "wire_body_sha256": _wire_hash(settings.model, supplement_prompt, supplement_input)}, supp_result, supp_audit or {}, reused=reused_supp, parent_run_id=parent_run_id, attempt_count=supp_attempts, response_count=supp_responses))
                state["shards"] = accepted_shards
            else:
                reason = "supplement_result_invalid" if supp_audit and supp_audit.get("status") == "succeeded" else "supplement_role_pending"
                accepted_shards.append(_pending_shard(supp_id, _seal_input(supplement_input, settings.model, supplement_prompt), reason, audit=supp_audit, attempts=supp_attempts, responses=supp_responses, kind="supplement"))
                state["shards"] = accepted_shards
        supplements_finished = time.monotonic() if supplements_started is not None else None
    discovery_coverage = discovery.get("coverage") if isinstance(discovery.get("coverage"), Mapping) else {}
    state["global_closure"]["discovery_coverage_complete"] = discovery_coverage.get("complete") is True
    state["global_closure"]["no_priority_allowed"] = bool(
        state["global_closure"].get("closed") is True
        and discovery.get("status") == "completed"
        and discovery_coverage.get("complete") is True
        and not state["global_closure"].get("pending_issue_ids")
        and not state["global_closure"].get("relation_gaps")
        and not state["global_closure"].get("coverage_gaps")
        and state["retrieval_coverage"].get("complete") is True
        and all(item.get("role_status") == "accepted" for item in state["supplements"])
        and all(item.get("disposition") != "pending" for item in state["member_decisions"])
    )
    retrieval_complete = bool(
        state["retrieval_coverage"].get("scope") != "not_started"
        and state["retrieval_coverage"].get("complete") is True
        and state["retrieval_coverage"].get("role_assignments_pending") == 0
        and all(item.get("role_status") == "accepted" for item in state["supplements"])
    )
    if state["global_closure"].get("closed") is True:
        state["status"] = "completed" if discovery.get("status") == "completed" and retrieval_complete else "partial"
        if discovery.get("status") != "completed":
            state["reason"] = "discovery_coverage_partial"
        elif not retrieval_complete:
            state["reason"] = (
                "supplement_role_pending" if state["retrieval_coverage"].get("role_assignments_pending", 0) > 0
                or any(item.get("role_status") != "accepted" for item in state["supplements"])
                else "supplement_retrieval_incomplete"
            )
        else:
            state["reason"] = None
    else:
        state["status"] = "partial"
        state["reason"] = state["global_closure"].get("reason") or "issue_pipeline_pending"
    if fatal_stop_reason is not None:
        state["status"] = "abstained" if fatal_stop_reason == "cancelled" else "failed"
        state["reason"] = fatal_stop_reason
    publish_current()
    return state


class _DurableShardError(ValueError):
    def __init__(self, code: str, *, attempts: int, responses: int, audit: Mapping[str, Any] | None):
        super().__init__(code)
        self.code = code
        self.attempts = attempts
        self.responses = responses
        self.audit = dict(audit) if isinstance(audit, Mapping) else None


def _call_typed_shard(
    shard: Mapping[str, Any], *, settings: ModelSettings, run_dir: Path, artifacts_root: Path,
    prompt: str, prompt_version: str, call_stage: str, kind: str, deadline: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    input_value = shard["input"]
    messages = _wire_messages(prompt, input_value)
    wire_body = _serialize_chat_messages(settings.model, _wire_message_dicts(prompt, input_value))
    wire_hash = hashlib.sha256(wire_body).hexdigest()
    if wire_hash != shard.get("wire_body_sha256"):
        raise _DurableShardError("request_wire_hash_mismatch", attempts=0, responses=0, audit=None)
    if len(wire_hash) != 64 or len(wire_body.decode("utf-8")) > (MAX_GLOBAL_REQUEST_CHARS if kind == "global" else MAX_SHARD_REQUEST_CHARS if kind == "grouping" else MAX_GLOBAL_REQUEST_CHARS):
        raise _DurableShardError("request_size_exceeded", attempts=0, responses=0, audit=None)
    refs = _evidence_refs(input_value)
    result = None
    audited = None
    try:
        with annual_call_deadline(deadline):
            audited = audited_complete_chat(messages, settings, run_dir=run_dir, evidence_refs=refs,
                prompt_version=prompt_version, call_stage=call_stage)
        audit = _audit_record(audited.audit_dir, artifacts_root, wire_hash, shard["semantic_sha256"], kind)
        result = parse_typed_result(audited.completion.text, kind=kind, input_value=input_value)
        return result, audit, audited.provider_admission_count, audited.response_count
    except Exception as exc:
        if audited is not None:
            attempts, responses = audited.provider_admission_count, audited.response_count
            audit = _audit_record(audited.audit_dir, artifacts_root, wire_hash, shard["semantic_sha256"], kind)
            code = "typed_response_invalid" if isinstance(exc, (ValueError, json.JSONDecodeError)) else "provider_call_failed"
        else:
            attempts = _safe_count(exc, "provider_admission_count")
            responses = _safe_count(exc, "response_count")
            audit = _latest_audit(run_dir, artifacts_root, prompt_version, wire_hash, shard["semantic_sha256"], kind)
            category = getattr(exc, "category", None)
            code = category if isinstance(category, str) and category in {
                "http_401", "http_403", "cancelled", "call_gate_closed", "ledger_config_invalid",
                "ledger_source_context_missing_or_unapproved", "ledger_unavailable", "ledger_provider_mismatch",
                "ledger_budget_exhausted", "task_budget_exhausted", "ledger_policy_mismatch", "audit_write_failed",
            } else "provider_call_failed"
        raise _DurableShardError(code, attempts=attempts, responses=responses, audit=audit) from None


def parse_typed_result(text: str, *, kind: str, input_value: Mapping[str, Any]) -> dict[str, Any]:
    """Parse an exact typed response and enforce input/member conservation."""

    if not isinstance(text, str) or len(text) > 18_000:
        raise ValueError("typed_response_invalid")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object_pairs)
    except json.JSONDecodeError:
        raise ValueError("typed_response_invalid") from None
    if not isinstance(value, dict):
        raise ValueError("typed_response_invalid")
    if kind == "grouping":
        if set(value) != {"schema_version", "decisions"} or value.get("schema_version") != "annual_issue_group_shard_result_v1":
            raise ValueError("typed_response_invalid")
        expected = sorted(input_value.get("input_member_finding_ids", []))
        quote_map = _member_quotes(input_value)
        decisions = _parse_decisions(value.get("decisions"), expected, "finding_id", "target_finding_id", "annual_issue_grouping", quote_map)
    elif kind == "global":
        if set(value) != {"schema_version", "decisions"} or value.get("schema_version") != "annual_issue_global_result_v1":
            raise ValueError("typed_response_invalid")
        expected = sorted(input_value.get("input_issue_ids", []))
        quote_map = _issue_quotes(input_value)
        decisions = _parse_decisions(value.get("decisions"), expected, "issue_id", "target_issue_id", "annual_issue_global", quote_map)
    elif kind == "supplement":
        if set(value) != {"schema_version", "roles"} or value.get("schema_version") != "annual_issue_supplement_result_v1":
            raise ValueError("typed_response_invalid")
        expected = sorted((item["issue_id"], item["evidence_id"]) for item in input_value.get("evidence_assignments", []))
        rows = value.get("roles")
        if not isinstance(rows, list) or len(rows) != len(expected):
            raise ValueError("typed_response_invalid")
        actual: list[tuple[str, str]] = []
        roles = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"issue_id", "evidence_id", "role", "reason_code"}:
                raise ValueError("typed_response_invalid")
            pair = (row["issue_id"], row["evidence_id"])
            role = row.get("role")
            reason = row.get("reason_code")
            if pair not in expected or role not in {"context", "possible_explanation", "unknown"}:
                raise ValueError("typed_response_invalid")
            if (role, reason) not in {("context", "context_only"), ("possible_explanation", "plausible_alternative"), ("unknown", "relationship_unclear")}:
                raise ValueError("typed_response_invalid")
            actual.append(pair)
            roles.append(dict(row))
        if sorted(actual) != expected:
            raise ValueError("typed_response_invalid")
        return {"schema_version": value["schema_version"], "roles": roles}
    else:
        raise ValueError("typed_response_invalid")
    return {"schema_version": value["schema_version"], "decisions": decisions}


def _parse_decisions(rows: Any, expected_ids: list[str], id_field: str, target_field: str, schema_name: str, quote_map: Mapping[str, set[tuple[str, str]]]) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) != len(expected_ids):
        raise ValueError("typed_response_invalid")
    decisions = []
    seen: set[str] = set()
    allowed_reasons = {
        "annual_issue_grouping": {"distinct_issue", "same_observed_event", "exact_duplicate", "not_financial_condition", "citation_scope_mismatch", "topic_overlap_only"},
        "annual_issue_global": {"distinct_across_shards", "same_observed_event", "same_underlying_issue", "not_financial_condition", "citation_scope_mismatch", "relation_unclear", "topic_overlap_only"},
    }[schema_name]
    ids = set(expected_ids)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {id_field, "disposition", target_field, "reason_code", "reason_evidence_id", "reason_quote"}:
            raise ValueError("typed_response_invalid")
        current = row.get(id_field)
        disposition = row.get("disposition")
        target = row.get(target_field)
        reason = row.get("reason_code")
        reason_evidence_id = row.get("reason_evidence_id")
        reason_quote = row.get("reason_quote")
        if not isinstance(current, str) or current not in ids or current in seen or disposition not in {"retain", "merge", "exclude"} or reason not in allowed_reasons:
            raise ValueError("typed_response_invalid")
        if disposition == "merge":
            if not isinstance(target, str) or target not in ids or target == current:
                raise ValueError("typed_response_invalid")
        elif target is not None:
            raise ValueError("typed_response_invalid")
        if disposition == "retain":
            retain_reasons = {"distinct_issue", "topic_overlap_only"} if schema_name == "annual_issue_grouping" else {"distinct_across_shards", "topic_overlap_only", "relation_unclear"}
            if reason not in retain_reasons:
                raise ValueError("typed_response_invalid")
        if disposition == "merge":
            merge_reasons = {"same_observed_event", "exact_duplicate"} if schema_name == "annual_issue_grouping" else {"same_observed_event", "same_underlying_issue"}
            if reason not in merge_reasons:
                raise ValueError("typed_response_invalid")
        if disposition == "exclude":
            if reason not in {"not_financial_condition", "citation_scope_mismatch"}:
                raise ValueError("typed_response_invalid")
            if not isinstance(reason_evidence_id, str) or not isinstance(reason_quote, str) or not reason_quote.strip() or (reason_evidence_id, reason_quote) not in quote_map.get(current, set()):
                raise ValueError("typed_response_invalid")
        elif reason_evidence_id is not None or reason_quote is not None:
            raise ValueError("typed_response_invalid")
        seen.add(current)
        decisions.append({id_field: current, "disposition": disposition, target_field: target, "reason_code": reason, "reason_evidence_id": reason_evidence_id, "reason_quote": reason_quote})
    if seen != ids:
        raise ValueError("typed_response_invalid")
    by_id = {item[id_field]: item for item in decisions}
    for row in decisions:
        if row["disposition"] == "merge" and by_id[row[target_field]]["disposition"] != "retain":
            raise ValueError("typed_response_invalid")
    return sorted(decisions, key=lambda item: item[id_field])


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("typed_response_invalid")
        result[key] = value
    return result


def _member_quotes(input_value: Mapping[str, Any]) -> dict[str, set[tuple[str, str]]]:
    result: dict[str, set[tuple[str, str]]] = {}
    candidates = {
        item.get("finding_id"): item
        for item in input_value.get("candidate_pool", [])
        if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
    }
    for group in input_value.get("groups", []):
        if not isinstance(group, Mapping) or not isinstance(group.get("member_finding_ids"), list):
            continue
        for finding_id in group["member_finding_ids"]:
            candidate = candidates.get(finding_id)
            if not isinstance(candidate, Mapping):
                continue
            result[finding_id] = {
                (str(item.get("evidence_id")), str(item.get("quote")))
                for item in candidate.get("citations", []) if isinstance(item, Mapping)
            }
    return result


def _issue_quotes(input_value: Mapping[str, Any]) -> dict[str, set[tuple[str, str]]]:
    result: dict[str, set[tuple[str, str]]] = {}
    candidates = {
        item.get("finding_id"): item
        for item in input_value.get("candidate_pool", [])
        if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
    }
    for issue in input_value.get("issue_members", []):
        if not isinstance(issue, Mapping) or not isinstance(issue.get("issue_id"), str):
            continue
        members = issue.get("member_finding_ids")
        if not isinstance(members, list) or issue.get("representative_finding_id") not in members:
            continue
        quotes: set[tuple[str, str]] = set()
        for member_id in members:
            candidate = candidates.get(member_id)
            if not isinstance(candidate, Mapping):
                continue
            quotes.update(
                (str(item.get("evidence_id")), str(item.get("quote")))
                for item in candidate.get("citations", []) if isinstance(item, Mapping)
            )
        result[issue["issue_id"]] = quotes
    return result


def validate_pipeline(value: Any, *, identity: Mapping[str, Any]) -> dict[str, Any]:
    """Strict API reader validator for the full v1 issue object."""
    top_fields = {
        "kind", "status", "reason", "source_identity", "execution_binding", "input", "candidates",
        "issues", "member_decisions", "supplements", "retrieval_coverage", "shards", "global_closure", "audit_summary",
    }
    if not isinstance(value, Mapping) or set(value) not in (top_fields, top_fields | {"phase_timings"}):
        raise ValueError("annual_issue_pipeline_invalid")
    if "phase_timings" in value:
        _validate_phase_timings(value["phase_timings"])
    source = _source_identity(identity)
    if value.get("kind") != PIPELINE_KIND or value.get("status") not in {"pending", "running", "partial", "completed", "failed", "abstained"}:
        raise ValueError("annual_issue_pipeline_invalid")
    if dict(value.get("source_identity", {})) != source:
        raise ValueError("annual_issue_pipeline_identity_mismatch")
    binding = value.get("execution_binding")
    binding_fields = {
        "kind", "execution_version", "source_sha256", "company_id", "report_year", "source_document_id",
        "analysis_mode", "provider_model_fingerprint", "model", "discovery_binding", "prompts_sha256",
        "parser_version", "issue_pipeline_version", "grouping_prompt_version", "review_prompt_version",
        "issue_wire_version", "review_wire_version", "review_input_projection_version",
        "plan_version", "plan_sha256", "m3_binding_sha256",
    }
    if (
        not isinstance(binding, Mapping) or set(binding) != binding_fields
        or binding.get("kind") != "fintrace_annual_execution_binding_v5"
        or binding.get("execution_version") != "execution_v5"
        or any(binding.get(key) != source[key] for key in ("source_sha256", "company_id", "report_year", "source_document_id"))
        or binding.get("issue_pipeline_version") != PIPELINE_KIND
        or binding.get("issue_wire_version") != ISSUE_WIRE_VERSION
        or binding.get("grouping_prompt_version") != GROUP_PROMPT_VERSION
        or binding.get("review_prompt_version") != "annual_review_v3"
        or binding.get("review_wire_version") != V3_WIRE_SCHEMA
        or binding.get("review_input_projection_version") != V3_INPUT_PROJECTION_VERSION
        or not isinstance(binding.get("model"), str)
        or not isinstance(binding.get("provider_model_fingerprint"), str)
        or _SHA.fullmatch(str(binding.get("prompts_sha256", ""))) is None
    ):
        raise ValueError("annual_issue_pipeline_binding_invalid")
    discovery_binding = binding.get("discovery_binding")
    if not isinstance(discovery_binding, Mapping) or any(
        discovery_binding.get(key) != binding.get(key)
        for key in ("source_sha256", "company_id", "report_year", "source_document_id", "analysis_mode", "provider_model_fingerprint", "model", "plan_version", "plan_sha256", "m3_binding_sha256")
    ):
        raise ValueError("annual_issue_pipeline_binding_invalid")
    input_value = value.get("input")
    input_fields = {"discovery_prompt_version", "discovery_status", "discovery_coverage_complete", "finding_ids", "finding_count", "finding_set_sha256", "candidate_count"}
    if not isinstance(input_value, Mapping) or set(input_value) != input_fields:
        raise ValueError("annual_issue_pipeline_invalid")
    finding_ids = input_value.get("finding_ids")
    if (
        not isinstance(finding_ids, list) or any(not isinstance(item, str) or re.fullmatch(rf"disc:{source['source_sha256'][:12]}-[0-9a-f]{{20}}", item) is None for item in finding_ids)
        or len(finding_ids) != len(set(finding_ids))
        or input_value.get("discovery_prompt_version") != "annual_discovery_v3"
        or input_value.get("discovery_status") not in {"not_started", "running", "partial", "completed", "failed", "not_called"}
        or type(input_value.get("discovery_coverage_complete")) is not bool
        or input_value.get("finding_count") != len(finding_ids)
        or type(input_value.get("candidate_count")) is not int or input_value["candidate_count"] < 0
    ):
        raise ValueError("annual_issue_pipeline_invalid")
    candidates, decisions, issues, supplements, shards = (value.get(key) for key in ("candidates", "member_decisions", "issues", "supplements", "shards"))
    if not all(isinstance(items, list) for items in (candidates, decisions, issues, supplements, shards)):
        raise ValueError("annual_issue_pipeline_invalid")
    candidate_fields = {"candidate_id", "origin", "status", "finding_id", "member_finding_ids", "title", "observation", "why_it_matters", "follow_up", "priority", "evidence_ids", "citations", "source_identity"}
    candidate_by_finding: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != candidate_fields:
            raise ValueError("annual_issue_pipeline_candidate_invalid")
        finding_id = candidate.get("finding_id")
        if (
            finding_id not in finding_ids or finding_id in candidate_by_finding
            or candidate.get("candidate_id") != "candidate:" + hashlib.sha256((source["source_sha256"] + "\n" + str(finding_id)).encode()).hexdigest()[:24]
            or candidate.get("origin") != "annual_discovery_v3" or candidate.get("status") != "qualified_unverified_finding"
            or candidate.get("member_finding_ids") != [finding_id]
            or candidate.get("source_identity") != {key: source[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")}
            or candidate.get("priority") not in {"high", "medium", "low"}
        ):
            raise ValueError("annual_issue_pipeline_candidate_invalid")
        _validate_pipeline_text(candidate.get("title"), 100)
        _validate_pipeline_text(candidate.get("observation"), 600)
        _validate_pipeline_text(candidate.get("why_it_matters"), 400)
        _validate_pipeline_text(candidate.get("follow_up"), 400)
        evidence_ids = candidate.get("evidence_ids")
        cites = candidate.get("citations")
        if not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) != len(set(evidence_ids)) or not isinstance(cites, list) or not cites:
            raise ValueError("annual_issue_pipeline_candidate_invalid")
        citation_ids = []
        for citation in cites:
            if not isinstance(citation, Mapping) or set(citation) != {"evidence_id", "role", "quote", "source_document_id", "source_sha256", "pdf_page", "block_index", "start_char", "bbox", "independent_verification"}:
                raise ValueError("annual_issue_pipeline_candidate_invalid")
            if (
                not isinstance(citation.get("evidence_id"), str) or citation.get("role") != "primary_finding_quote"
                or not isinstance(citation.get("quote"), str) or not citation["quote"].strip()
                or citation.get("source_document_id") != source["source_document_id"] or citation.get("source_sha256") != source["source_sha256"]
                or type(citation.get("pdf_page")) is not int or citation["pdf_page"] < 1
                or type(citation.get("block_index")) is not int or citation["block_index"] < 0
                or type(citation.get("start_char")) is not int or citation["start_char"] < 0
                or not isinstance(citation.get("bbox"), Mapping) or citation.get("independent_verification") is not False
            ):
                raise ValueError("annual_issue_pipeline_candidate_invalid")
            citation_ids.append(citation["evidence_id"])
        if set(citation_ids) != set(evidence_ids):
            raise ValueError("annual_issue_pipeline_candidate_invalid")
        candidate_by_finding[str(finding_id)] = candidate
    if (
        set(candidate_by_finding) != set(finding_ids)
        or input_value["candidate_count"] != len(_local_exact_duplicate_groups(candidates))
        or input_value["finding_set_sha256"] != _sha_json(candidates)
    ):
        raise ValueError("annual_issue_pipeline_member_conservation_failed")
    decision_fields = {"finding_id", "disposition", "issue_id", "target_finding_id", "reason_code", "reason_evidence_id", "reason_quote", "source_finding_id"}
    decision_by_finding: dict[str, Mapping[str, Any]] = {}
    for decision in decisions:
        if not isinstance(decision, Mapping) or set(decision) != decision_fields:
            raise ValueError("annual_issue_pipeline_member_decision_invalid")
        finding_id, disposition, reason = decision.get("finding_id"), decision.get("disposition"), decision.get("reason_code")
        if finding_id not in finding_ids or finding_id in decision_by_finding or decision.get("source_finding_id") != finding_id:
            raise ValueError("annual_issue_pipeline_member_decision_invalid")
        if disposition == "pending":
            valid = decision.get("issue_id") is None and decision.get("target_finding_id") is None and reason in {"awaiting_grouping", "shard_pending", "merge_target_unavailable"} and decision.get("reason_evidence_id") is None and decision.get("reason_quote") is None
        elif disposition == "retain":
            valid = decision.get("issue_id") in {_issue_id(source, finding_id)} and decision.get("target_finding_id") is None and reason in {"distinct_issue", "distinct_across_shards", "topic_overlap_only", "relation_unclear"} and decision.get("reason_evidence_id") is None and decision.get("reason_quote") is None
        elif disposition == "merge":
            target = decision.get("target_finding_id")
            valid = isinstance(target, str) and decision.get("issue_id") == _issue_id(source, target) and reason in {"same_observed_event", "same_underlying_issue", "exact_duplicate"} and decision.get("reason_evidence_id") is None and decision.get("reason_quote") is None
        elif disposition == "exclude":
            valid = decision.get("issue_id") is None and decision.get("target_finding_id") is None and reason in {"not_financial_condition", "citation_scope_mismatch"} and isinstance(decision.get("reason_evidence_id"), str) and isinstance(decision.get("reason_quote"), str) and bool(decision.get("reason_quote", "").strip())
        else:
            valid = False
        if not valid:
            raise ValueError("annual_issue_pipeline_member_decision_invalid")
        decision_by_finding[str(finding_id)] = decision
    if set(decision_by_finding) != set(finding_ids):
        raise ValueError("annual_issue_pipeline_member_decision_conservation_failed")
    issue_by_id: dict[str, Mapping[str, Any]] = {}
    issue_fields = {"issue_id", "origin", "status", "title", "observation", "why_it_matters", "follow_up", "priority", "member_finding_ids", "evidence_ids", "citations", "member_sources", "verification_status", "independent_verification", "review_order_reason"}
    for issue in issues:
        if not isinstance(issue, Mapping) or not issue_fields.issubset(issue) or set(issue) - (issue_fields | {"global_relation", "global_merged_issue_ids"}):
            raise ValueError("annual_issue_pipeline_issue_invalid")
        issue_id, members = issue.get("issue_id"), issue.get("member_finding_ids")
        if not isinstance(issue_id, str) or issue_id in issue_by_id or not isinstance(members, list) or not members or len(members) != len(set(members)) or any(member not in candidate_by_finding for member in members):
            raise ValueError("annual_issue_pipeline_issue_invalid")
        roots = [row.get("finding_id") for row in issue.get("member_sources", []) if isinstance(row, Mapping) and row.get("disposition") == "retain"] if isinstance(issue.get("member_sources"), list) else []
        if not roots or issue_id != _issue_id(source, roots[0]):
            raise ValueError("annual_issue_pipeline_issue_invalid")
        expected_evidence = {evidence_id for member in members for evidence_id in candidate_by_finding[member]["evidence_ids"]}
        if set(issue.get("evidence_ids", [])) != expected_evidence or issue.get("origin") != "annual_discovery_v3" or issue.get("verification_status") != "unverified" or issue.get("independent_verification") is not False:
            raise ValueError("annual_issue_pipeline_issue_invalid")
        if issue.get("status") not in {"provisional", "accepted", "pending_global_relation", "excluded"}:
            raise ValueError("annual_issue_pipeline_issue_invalid")
        if issue.get("status") in {"accepted", "pending_global_relation", "excluded"} and "global_relation" not in issue or issue.get("status") not in {"accepted", "pending_global_relation", "excluded"} and "global_relation" in issue:
            raise ValueError("annual_issue_pipeline_issue_invalid")
        _validate_pipeline_text(issue.get("review_order_reason"), 360)
        relation = issue.get("global_relation")
        if relation is not None:
            if not isinstance(relation, Mapping) or relation.get("disposition") not in {"retain", "merge", "exclude"}:
                raise ValueError("annual_issue_pipeline_issue_invalid")
            expected_relation_fields = {"disposition", "target_issue_id", "reason_code"}
            if relation.get("disposition") == "exclude":
                expected_relation_fields |= {"reason_evidence_id", "reason_quote"}
            if set(relation) != expected_relation_fields:
                raise ValueError("annual_issue_pipeline_issue_invalid")
        member_sources = issue.get("member_sources")
        if not isinstance(member_sources, list) or {row.get("finding_id") for row in member_sources if isinstance(row, Mapping)} != set(members) or any(not isinstance(row, Mapping) or set(row) != {"finding_id", "disposition", "reason_code"} or row.get("disposition") not in {"retain", "merge"} for row in member_sources):
            raise ValueError("annual_issue_pipeline_issue_invalid")
        issue_by_id[issue_id] = issue
    issue_member_ids = [member for issue in issues for member in issue["member_finding_ids"]]
    if len(issue_member_ids) != len(set(issue_member_ids)):
        raise ValueError("annual_issue_pipeline_issue_member_duplicate")
    for finding_id, decision in decision_by_finding.items():
        issue_id = decision.get("issue_id")
        if decision.get("disposition") in {"retain", "merge"} and issue_id in issue_by_id:
            if finding_id not in issue_by_id[issue_id]["member_finding_ids"]:
                raise ValueError("annual_issue_pipeline_issue_member_mismatch")
    closure = value.get("global_closure")
    closure_fields = {
        "closed", "closure_method", "scope", "input_issue_ids", "decision_count", "pending_issue_ids", "excluded_finding_ids", "relation_gaps", "coverage_gaps",
        "input_finding_ids", "retained_finding_ids", "pending_finding_ids", "input_member_evidence", "retained_member_evidence", "excluded_member_evidence", "excluded_issue_account",
        "reason", "discovery_coverage_complete", "no_priority_allowed",
    }
    if not isinstance(closure, Mapping) or set(closure) != closure_fields or type(closure.get("closed")) is not bool or closure.get("scope") != "current_known_findings" or closure.get("closure_method") not in {"empty_discovery", "single_group_shard", "global_reconciliation", "pending"} or type(closure.get("no_priority_allowed")) is not bool:
        raise ValueError("annual_issue_pipeline_global_closure_invalid")
    for key in ("input_issue_ids", "pending_issue_ids", "excluded_finding_ids", "relation_gaps", "coverage_gaps", "input_finding_ids", "retained_finding_ids", "pending_finding_ids"):
        items = closure.get(key)
        if not isinstance(items, list) or len(items) != len(set(items)) or any(not isinstance(item, str) for item in items):
            raise ValueError("annual_issue_pipeline_global_closure_invalid")
    if type(closure.get("decision_count")) is not int or closure["decision_count"] < 0 or type(closure.get("discovery_coverage_complete")) is not bool:
        raise ValueError("annual_issue_pipeline_global_closure_invalid")
    if closure.get("input_finding_ids"):
        if sorted(closure["input_finding_ids"]) != sorted(finding_ids):
            raise ValueError("annual_issue_pipeline_global_member_conservation_failed")
    elif finding_ids and closure.get("closed"):
        raise ValueError("annual_issue_pipeline_global_member_conservation_failed")
    all_pairs = _validate_member_evidence_pairs(closure.get("input_member_evidence"))
    retained_pairs = _validate_member_evidence_pairs(closure.get("retained_member_evidence"))
    excluded_pairs = _validate_member_evidence_pairs(closure.get("excluded_member_evidence"))
    excluded_issue_account = closure.get("excluded_issue_account")
    if not isinstance(excluded_issue_account, list):
        raise ValueError("annual_issue_pipeline_global_closure_invalid")
    excluded_issue_members: dict[str, Mapping[str, Any]] = {}
    for entry in excluded_issue_account:
        if not isinstance(entry, Mapping) or set(entry) != {"issue_id", "member_finding_ids", "reason_code", "reason_evidence_id", "reason_quote"}:
            raise ValueError("annual_issue_pipeline_global_closure_invalid")
        members = entry.get("member_finding_ids")
        if not isinstance(members, list) or not members or len(members) != len(set(members)) or entry.get("reason_code") not in {"not_financial_condition", "citation_scope_mismatch"} or not isinstance(entry.get("reason_evidence_id"), str) or not isinstance(entry.get("reason_quote"), str) or not entry["reason_quote"].strip():
            raise ValueError("annual_issue_pipeline_global_closure_invalid")
        for member in members:
            if member in excluded_issue_members:
                raise ValueError("annual_issue_pipeline_global_closure_invalid")
            excluded_issue_members[str(member)] = entry
    for finding_id, decision in decision_by_finding.items():
        if decision.get("disposition") != "exclude":
            continue
        direct_quotes = {(citation.get("evidence_id"), citation.get("quote")) for citation in candidate_by_finding[finding_id].get("citations", [])}
        source_account = excluded_issue_members.get(finding_id)
        linked_to_global_issue = source_account is not None and all(source_account.get(field) == decision.get(field) for field in ("reason_code", "reason_evidence_id", "reason_quote"))
        if (decision.get("reason_evidence_id"), decision.get("reason_quote")) not in direct_quotes and not linked_to_global_issue:
            raise ValueError("annual_issue_pipeline_member_decision_invalid")
    expected_pairs = sorted((finding_id, evidence_id) for finding_id, candidate in candidate_by_finding.items() for evidence_id in candidate.get("evidence_ids", []))
    if all_pairs != expected_pairs:
        raise ValueError("annual_issue_pipeline_global_member_conservation_failed")
    if any(pair not in all_pairs for pair in [*retained_pairs, *excluded_pairs]):
        raise ValueError("annual_issue_pipeline_global_member_conservation_failed")
    if closure.get("closed"):
        if (
            closure.get("pending_issue_ids") or closure.get("pending_finding_ids") or closure.get("relation_gaps")
            or closure.get("input_issue_ids") != sorted(closure.get("input_issue_ids"))
            or closure.get("decision_count") != len(closure.get("input_issue_ids", []))
            or sorted([*retained_pairs, *excluded_pairs]) != expected_pairs
            or set(closure.get("retained_finding_ids", [])) | set(closure.get("excluded_finding_ids", [])) != set(finding_ids)
            or set(closure.get("retained_finding_ids", [])) & set(closure.get("excluded_finding_ids", []))
        ):
            raise ValueError("annual_issue_pipeline_false_closure")
    elif closure.get("no_priority_allowed"):
        raise ValueError("annual_issue_pipeline_false_no_priority")
    if (closure.get("closed") and closure.get("closure_method") == "pending") or (not closure.get("closed") and closure.get("closure_method") != "pending"):
        raise ValueError("annual_issue_pipeline_closure_method_invalid")
    if closure.get("no_priority_allowed") and (
        not closure.get("closed") or input_value.get("discovery_status") != "completed"
        or not input_value.get("discovery_coverage_complete") or closure.get("coverage_gaps")
        or any(item.get("disposition") == "pending" for item in decisions)
    ):
        raise ValueError("annual_issue_pipeline_false_no_priority")
    retrieval_coverage = value.get("retrieval_coverage")
    _validate_pipeline_retrieval(retrieval_coverage, source, supplements)
    _validate_pipeline_supplements(supplements, source, issue_by_id)
    _validate_pipeline_shards(
        shards, finding_ids, candidate_by_finding, decision_by_finding,
        issue_by_id, source, binding, input_value=input_value, closure=closure,
        supplements=supplements,
    )
    grouping_shards = [item for item in shards if isinstance(item, Mapping) and item.get("kind") == "grouping"]
    global_accepted = [item for item in shards if isinstance(item, Mapping) and item.get("kind") == "global" and item.get("status") == "accepted"]
    method = closure["closure_method"]
    if method == "empty_discovery" and (
        finding_ids or candidates or issues or decisions or grouping_shards or global_accepted
        or closure.get("input_issue_ids") or closure.get("pending_issue_ids")
    ):
        raise ValueError("annual_issue_pipeline_closure_method_invalid")
    if method == "single_group_shard" and (
        len(grouping_shards) != 1 or grouping_shards[0].get("status") != "accepted"
        or global_accepted or not closure.get("closed")
    ):
        raise ValueError("annual_issue_pipeline_closure_method_invalid")
    if method == "global_reconciliation" and (
        len(global_accepted) != 1 or not closure.get("closed")
        or closure.get("input_issue_ids") != global_accepted[0].get("input_issue_ids")
    ):
        raise ValueError("annual_issue_pipeline_closure_method_invalid")
    if method == "pending" and closure.get("closed"):
        raise ValueError("annual_issue_pipeline_closure_method_invalid")
    summary = value.get("audit_summary")
    if not isinstance(summary, Mapping) or set(summary) != {"attempt_count", "response_count", "reused_count", "model_called", "audit_artifacts"}:
        raise ValueError("annual_issue_pipeline_audit_summary_invalid")
    counts = [summary.get("attempt_count"), summary.get("response_count"), summary.get("reused_count")]
    if any(type(item) is not int or item < 0 for item in counts) or type(summary.get("model_called")) is not bool or summary.get("model_called") != (summary["attempt_count"] > 0) or not isinstance(summary.get("audit_artifacts"), list):
        raise ValueError("annual_issue_pipeline_audit_summary_invalid")
    if summary["attempt_count"] != sum(item.get("attempt_count", 0) for item in shards) or summary["response_count"] != sum(item.get("response_count", 0) for item in shards) or summary["reused_count"] != sum(item.get("reused") is True for item in shards):
        raise ValueError("annual_issue_pipeline_audit_summary_mismatch")
    if value.get("status") == "completed" and (
        not closure.get("closed") or input_value.get("discovery_status") != "completed"
        or retrieval_coverage.get("scope") == "not_started" or retrieval_coverage.get("complete") is not True
        or retrieval_coverage.get("role_assignments_pending") != 0
        or any(item.get("role_status") != "accepted" for item in supplements)
    ):
        raise ValueError("annual_issue_pipeline_false_completion")
    return dict(value)


def _validate_pipeline_text(value: Any, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ValueError("annual_issue_pipeline_text_invalid")


def _validate_phase_timings(value: Any) -> None:
    required = {"schema_version", "annual_issue_grouping_elapsed_seconds"}
    optional = {"grouping_elapsed_seconds", "global_reconciliation_elapsed_seconds", "supplements_elapsed_seconds"}
    if (
        not isinstance(value, Mapping) or not required.issubset(value)
        or set(value) - (required | optional)
        or value.get("schema_version") != "annual_issue_pipeline_phase_timings_v1"
    ):
        raise ValueError("annual_issue_pipeline_phase_timings_invalid")
    duration_fields = {"annual_issue_grouping_elapsed_seconds", *optional}
    for key in duration_fields:
        if key not in value:
            continue
        duration = value[key]
        if type(duration) not in {int, float} or not math.isfinite(float(duration)) or duration < 0:
            raise ValueError("annual_issue_pipeline_phase_timings_invalid")
    total = float(value["annual_issue_grouping_elapsed_seconds"])
    if any(float(value[key]) > total + 0.002 for key in optional if key in value):
        raise ValueError("annual_issue_pipeline_phase_timings_invalid")
    measured_subphases = sum(float(value[key]) for key in optional if key in value)
    if measured_subphases > total + 0.006:
        raise ValueError("annual_issue_pipeline_phase_timings_invalid")


def _validate_member_evidence_pairs(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        raise ValueError("annual_issue_pipeline_global_closure_invalid")
    pairs: list[tuple[str, str]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 2 or any(not isinstance(item, str) for item in row):
            raise ValueError("annual_issue_pipeline_global_closure_invalid")
        pairs.append((row[0], row[1]))
    if len(pairs) != len(set(pairs)):
        raise ValueError("annual_issue_pipeline_global_closure_invalid")
    return sorted(pairs)


def _validate_pipeline_retrieval(value: Any, source: Mapping[str, Any], supplements: Sequence[Mapping[str, Any]]) -> None:
    fields = {
        "source_document_id", "source_sha256", "scope", "requested_sections", "examined_sections",
        "total_pages", "total_text_blocks", "scanned_blocks", "matched_blocks", "matched_issue_block_pairs",
        "selected_blocks", "selected_issue_block_pairs", "unselected_issue_block_pairs", "scanned_chars",
        "selected_excerpt_chars", "selected_unique_source_chars", "unread_pages", "unread_blocks", "omitted_chars", "complete",
        "selection_algorithm_version", "max_terms_per_issue", "max_matches_per_issue", "max_excerpt_chars",
        "max_role_assignments", "role_assignments_total", "role_assignments_requested",
        "role_assignments_pending", "reason",
    }
    if not isinstance(value, Mapping) or set(value) != fields or value.get("source_document_id") != source["source_document_id"] or value.get("source_sha256") != source["source_sha256"]:
        raise ValueError("annual_issue_pipeline_retrieval_invalid")
    counters = (
        "total_pages", "total_text_blocks", "scanned_blocks", "matched_blocks", "matched_issue_block_pairs",
        "selected_blocks", "selected_issue_block_pairs", "unselected_issue_block_pairs", "scanned_chars",
        "selected_excerpt_chars", "selected_unique_source_chars", "unread_blocks", "omitted_chars", "max_terms_per_issue",
        "max_matches_per_issue", "max_excerpt_chars", "max_role_assignments", "role_assignments_total",
        "role_assignments_requested", "role_assignments_pending",
    )
    if any(type(value.get(key)) is not int or value[key] < 0 for key in counters) or type(value.get("complete")) is not bool:
        raise ValueError("annual_issue_pipeline_retrieval_invalid")
    requested_sections = value.get("requested_sections")
    examined_sections = value.get("examined_sections")
    unread_pages = value.get("unread_pages")
    if (
        not isinstance(requested_sections, list) or requested_sections not in ([], ["management_discussion", "financial_statements", "notes"])
        or not isinstance(examined_sections, list) or examined_sections != sorted(set(examined_sections))
        or any(item not in {"unknown", "management_discussion", "financial_statements", "notes"} for item in examined_sections)
        or not isinstance(unread_pages, list) or unread_pages != sorted(set(unread_pages))
        or any(type(item) is not int or item < 1 for item in unread_pages)
        or value.get("scope") not in {"not_started", "same_source_extracted_text_blocks"}
        or value.get("selection_algorithm_version") != "section_filter_then_ngram_overlap_v1"
        or value["max_terms_per_issue"] != 40 or value["max_matches_per_issue"] != 4
        or value["max_excerpt_chars"] != 500 or value["max_role_assignments"] != MAX_SUPPLEMENT_PAIRS
        or not isinstance(value.get("reason"), str) and value.get("reason") is not None
    ):
        raise ValueError("annual_issue_pipeline_retrieval_invalid")
    if value["scope"] == "not_started":
        if supplements or requested_sections or examined_sections or unread_pages or value["complete"] or value["reason"] != "supplement_not_started" or any(value[key] for key in counters if key not in {"max_terms_per_issue", "max_matches_per_issue", "max_excerpt_chars", "max_role_assignments"}):
            raise ValueError("annual_issue_pipeline_retrieval_invalid")
        return
    if (
        requested_sections != ["management_discussion", "financial_statements", "notes"]
        or value["scanned_blocks"] > value["total_text_blocks"]
        or value["matched_blocks"] < value["selected_blocks"]
        or value["matched_issue_block_pairs"] < value["selected_issue_block_pairs"]
        or value["selected_blocks"] != len({(item["pdf_page"], item["block_index"]) for item in supplements})
        or value["role_assignments_total"] != sum(len(item.get("issue_ids", [])) for item in supplements)
        or value["role_assignments_requested"] + value["role_assignments_pending"] != value["role_assignments_total"]
        or value["matched_issue_block_pairs"] != value["selected_issue_block_pairs"] + value["unselected_issue_block_pairs"]
        or value["selected_unique_source_chars"] != _selected_source_char_union_size(supplements)
        or value["selected_unique_source_chars"] > value["scanned_chars"]
        or value["selected_unique_source_chars"] > value["selected_excerpt_chars"]
        or value["omitted_chars"] != value["scanned_chars"] - value["selected_unique_source_chars"]
        or value["complete"] != (value["scanned_blocks"] == value["total_text_blocks"] and not unread_pages)
        or (not unread_pages and value["reason"] is not None)
        or (unread_pages and value["reason"] != "images_or_nontext_pages_not_ocr_scanned")
    ):
        raise ValueError("annual_issue_pipeline_retrieval_invalid")


def _validate_pipeline_supplements(supplements: Sequence[Any], source: Mapping[str, Any], issues: Mapping[str, Mapping[str, Any]]) -> None:
    fields = {"evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index", "start_char", "end_char", "bbox", "raw_text", "section", "selection_reason", "role", "issue_ids", "verification_status", "role_status", "independent_verification", "retrieval_is_verification"}
    roles = {"context", "possible_explanation", "unknown"}
    pairs: set[tuple[str, str]] = set()
    for item in supplements:
        if not isinstance(item, Mapping) or frozenset(item) not in {frozenset(fields), frozenset(fields | {"issue_roles"})}:
            raise ValueError("annual_issue_pipeline_supplement_invalid")
        if item.get("source_document_id") != source["source_document_id"] or item.get("source_sha256") != source["source_sha256"] or item.get("verification_status") != "unverified" or item.get("independent_verification") is not False or item.get("retrieval_is_verification") is not False or item.get("section") not in {"management_discussion", "financial_statements", "notes"} or item.get("selection_reason") != "same_source_keyword_context_match":
            raise ValueError("annual_issue_pipeline_supplement_invalid")
        if type(item.get("pdf_page")) is not int or item["pdf_page"] < 1 or type(item.get("block_index")) is not int or item["block_index"] < 0 or type(item.get("start_char")) is not int or type(item.get("end_char")) is not int or item["start_char"] < 0 or item["end_char"] <= item["start_char"] or not isinstance(item.get("raw_text"), str) or len(item["raw_text"]) != item["end_char"] - item["start_char"]:
            raise ValueError("annual_issue_pipeline_supplement_invalid")
        issue_ids = item.get("issue_ids")
        if not isinstance(issue_ids, list) or not issue_ids or len(issue_ids) != len(set(issue_ids)) or any(issue_id not in issues for issue_id in issue_ids):
            raise ValueError("annual_issue_pipeline_supplement_invalid")
        role_status = item.get("role_status")
        if role_status not in {"pending", "accepted"} or item.get("role") not in roles:
            raise ValueError("annual_issue_pipeline_supplement_invalid")
        issue_roles = item.get("issue_roles", [])
        if not isinstance(issue_roles, list):
            raise ValueError("annual_issue_pipeline_supplement_invalid")
        for role in issue_roles:
            if not isinstance(role, Mapping) or set(role) != {"issue_id", "role", "reason_code"} or role.get("issue_id") not in issue_ids or role.get("role") not in roles or (role.get("role"), role.get("reason_code")) not in {("context", "context_only"), ("possible_explanation", "plausible_alternative"), ("unknown", "relationship_unclear")}:
                raise ValueError("annual_issue_pipeline_supplement_invalid")
            pair = (str(role["issue_id"]), str(item["evidence_id"]))
            if pair in pairs:
                raise ValueError("annual_issue_pipeline_supplement_invalid")
            pairs.add(pair)
        if role_status == "accepted" and {row.get("issue_id") for row in issue_roles if isinstance(row, Mapping)} != set(issue_ids):
            raise ValueError("annual_issue_pipeline_supplement_invalid")


def _validate_pipeline_shards(
    shards: Sequence[Any],
    finding_ids: Sequence[str],
    candidates: Mapping[str, Mapping[str, Any]],
    final_decisions: Mapping[str, Mapping[str, Any]],
    issues: Mapping[str, Mapping[str, Any]],
    source: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    input_value: Mapping[str, Any],
    closure: Mapping[str, Any],
    supplements: Sequence[Mapping[str, Any]],
) -> None:
    seen_ids: set[str] = set()
    grouped_members: list[str] = []
    grouped_group_ids: list[str] = []
    accepted_group_decisions: dict[str, dict[str, Any]] = {}
    global_shards: list[Mapping[str, Any]] = []
    supplement_shards: list[Mapping[str, Any]] = []
    prompt_names = {"grouping": "annual_issue_grouping_v1", "global": "annual_issue_global_v1", "supplement": "annual_issue_supplement_v1"}
    expected_groups = {item["group_id"]: item for item in _local_exact_duplicate_groups(list(candidates.values()))}
    for shard in shards:
        if not isinstance(shard, Mapping) or shard.get("status") not in {"accepted", "pending"}:
            raise ValueError("annual_issue_pipeline_shard_invalid")
        status = shard["status"]
        base_fields = {"shard_id", "kind", "status", "semantic_input_sha256", "request_body_sha256", "input_finding_ids", "input_issue_ids", "input_members", "input", "result", "audit", "attempt_count", "response_count", "reused", "parent_run_id"}
        expected_fields = base_fields if status == "accepted" else base_fields | {"reason"}
        if set(shard) != expected_fields or shard.get("kind") not in prompt_names:
            raise ValueError("annual_issue_pipeline_shard_invalid")
        shard_id = shard.get("shard_id")
        kind = shard["kind"]
        if not isinstance(shard_id, str) or shard_id in seen_ids or (kind == "grouping") != shard_id.startswith("group:") or (kind == "global") != shard_id.startswith("global:") or (kind == "supplement") != shard_id.startswith("supplement:"):
            raise ValueError("annual_issue_pipeline_shard_invalid")
        seen_ids.add(shard_id)
        semantic = shard.get("semantic_input_sha256")
        wire_hash = shard.get("request_body_sha256")
        if not isinstance(semantic, str) or _SHA.fullmatch(semantic) is None or not isinstance(wire_hash, str) or _SHA.fullmatch(wire_hash) is None:
            raise ValueError("annual_issue_pipeline_shard_invalid")
        if type(shard.get("attempt_count")) is not int or shard["attempt_count"] < 0 or type(shard.get("response_count")) is not int or shard["response_count"] < 0 or type(shard.get("reused")) is not bool or shard.get("reused") and (shard["attempt_count"] or shard["response_count"] or not isinstance(shard.get("parent_run_id"), str)) or not shard.get("reused") and shard.get("parent_run_id") is not None:
            raise ValueError("annual_issue_pipeline_shard_invalid")
        input_value = shard.get("input")
        if not isinstance(input_value, Mapping) or _semantic_sha(input_value) != semantic:
            raise ValueError("annual_issue_pipeline_shard_invalid")
        prompt = _load_group_prompt({"grouping": "annual_issue_grouping_v1.md", "global": "annual_issue_global_v1.md", "supplement": "annual_issue_supplement_v1.md"}[kind])
        if _wire_hash(str(binding["model"]), prompt, input_value) != wire_hash:
            raise ValueError("annual_issue_pipeline_wire_hash_invalid")
        if kind == "grouping":
            members = shard.get("input_finding_ids")
            if members != input_value.get("input_member_finding_ids") or shard.get("input_issue_ids") != [] or shard.get("input_members") != input_value.get("groups"):
                raise ValueError("annual_issue_pipeline_shard_invalid")
            groups = input_value.get("groups")
            if not isinstance(groups, list) or not groups:
                raise ValueError("annual_issue_pipeline_shard_invalid")
            expected_group_rows = []
            expected_candidate_ids: set[str] = set()
            for group in groups:
                if not isinstance(group, Mapping) or set(group) != {"group_id", "representative_finding_id", "member_finding_ids"}:
                    raise ValueError("annual_issue_pipeline_shard_invalid")
                group_id = group.get("group_id")
                expected_full = expected_groups.get(group_id)
                expected_row = {
                    "group_id": expected_full["group_id"],
                    "representative_finding_id": expected_full["representative_finding_id"],
                    "member_finding_ids": expected_full["member_finding_ids"],
                } if expected_full is not None else None
                if expected_row is None or dict(group) != expected_row:
                    raise ValueError("annual_issue_pipeline_shard_invalid")
                expected_candidate_ids.update(group["member_finding_ids"])
                expected_group_rows.append(expected_row)
                grouped_group_ids.append(str(group_id))
            if (
                set(input_value) != {"schema_version", "source_identity", "groups", "candidate_pool", "input_member_finding_ids"}
                or input_value.get("schema_version") != "annual_issue_group_shard_input_v1"
                or input_value.get("source_identity") != {key: source[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")}
                or input_value.get("input_member_finding_ids") != sorted(expected_candidate_ids)
                or members != sorted(expected_candidate_ids)
                or input_value.get("candidate_pool") != [dict(candidates[key]) for key in sorted(expected_candidate_ids)]
            ):
                raise ValueError("annual_issue_pipeline_shard_invalid")
            grouped_members.extend(members)
        elif kind == "global":
            ids = shard.get("input_issue_ids")
            if ids != input_value.get("input_issue_ids") or shard.get("input_finding_ids") != [] or shard.get("input_members") != []:
                raise ValueError("annual_issue_pipeline_shard_invalid")
            global_shards.append(shard)
        else:
            if shard.get("input_finding_ids") != [] or shard.get("input_issue_ids") != [] or shard.get("input_members") != []:
                raise ValueError("annual_issue_pipeline_shard_invalid")
            supplement_shards.append(shard)
        audit = shard.get("audit")
        if audit is not None and (not isinstance(audit, Mapping) or set(audit) != {"audit_dir", "request_path", "response_path", "failure_path", "status", "prompt_version", "request_body_sha256", "semantic_input_sha256"} or audit.get("prompt_version") != prompt_names[kind] or audit.get("request_body_sha256") != wire_hash or audit.get("semantic_input_sha256") != semantic):
            raise ValueError("annual_issue_pipeline_shard_invalid")
        if status == "accepted":
            if not isinstance(shard.get("result"), Mapping) or not isinstance(audit, Mapping) or audit.get("status") != "succeeded" or shard.get("response_count", 0) < 1 and not shard.get("reused"):
                raise ValueError("annual_issue_pipeline_shard_invalid")
            strict = parse_typed_result(json.dumps(shard["result"], ensure_ascii=False), kind=kind, input_value=input_value)
            if strict != shard["result"]:
                raise ValueError("annual_issue_pipeline_shard_result_invalid")
            if kind == "grouping":
                for decision in strict["decisions"]:
                    finding_id = decision["finding_id"]
                    if finding_id in accepted_group_decisions:
                        raise ValueError("annual_issue_pipeline_shard_member_invalid")
                    accepted_group_decisions[finding_id] = decision
        elif shard.get("result") is not None or not isinstance(shard.get("reason"), str) or not shard["reason"]:
            raise ValueError("annual_issue_pipeline_shard_invalid")
    has_grouping = any(item.get("kind") == "grouping" for item in shards)
    if (
        any(member not in finding_ids for member in grouped_members)
        or len(grouped_members) != len(set(grouped_members))
        or len(grouped_group_ids) != len(set(grouped_group_ids))
        or has_grouping and set(grouped_members) != set(finding_ids)
        or has_grouping and set(grouped_group_ids) != set(expected_groups)
    ):
        raise ValueError("annual_issue_pipeline_shard_member_invalid")
    if len(global_shards) > 1:
        raise ValueError("annual_issue_pipeline_global_shard_invalid")

    # Reconstruct the complete local disposition ledger from accepted typed
    # grouping results. A pending/invalid shard contributes no decisions.
    local_decisions, local_issues = _reconstruct_local_grouping(
        finding_ids, candidates, accepted_group_decisions, source,
        pending_reason="shard_pending" if has_grouping else "awaiting_grouping",
    )
    if global_shards:
        global_shard = global_shards[0]
        global_input = global_shard.get("input")
        if not isinstance(global_input, Mapping):
            raise ValueError("annual_issue_pipeline_global_input_invalid")
        provisional = [item for item in local_issues if item.get("status") == "provisional"]
        issue_ids = sorted(item["issue_id"] for item in provisional)
        expected_issue_members = []
        for item in provisional:
            roots = [row["finding_id"] for row in item.get("member_sources", []) if row.get("disposition") == "retain"]
            if len(roots) != 1:
                raise ValueError("annual_issue_pipeline_global_input_invalid")
            expected_issue_members.append({
                "issue_id": item["issue_id"],
                "representative_finding_id": roots[0],
                "member_finding_ids": list(item["member_finding_ids"]),
            })
        expected_issue_members.sort(key=lambda item: item["issue_id"])
        if (
            set(global_input) != {"schema_version", "source_identity", "input_issue_ids", "issue_members", "candidate_pool", "member_decisions", "source_candidate_count"}
            or global_input.get("schema_version") != "annual_issue_global_input_v1"
            or global_input.get("source_identity") != {key: source[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")}
            or global_input.get("input_issue_ids") != issue_ids
            or global_input.get("issue_members") != expected_issue_members
            or global_input.get("candidate_pool") != [dict(item) for item in sorted(candidates.values(), key=lambda row: str(row.get("finding_id", "")))]
            or global_input.get("member_decisions") != local_decisions
            or global_input.get("source_candidate_count") != len(candidates)
            or len(local_decisions) != len(finding_ids)
        ):
            raise ValueError("annual_issue_pipeline_global_input_invalid")
        if global_shard.get("status") == "accepted":
            simulated = {
                "input": {"finding_ids": list(finding_ids)},
                "candidates": list(candidates.values()),
                "member_decisions": [dict(item) for item in local_decisions],
                "issues": [dict(item) for item in provisional],
                "global_closure": {"coverage_gaps": list(closure.get("coverage_gaps", []))},
            }
            _apply_global_decisions(simulated, global_shard["result"], provisional)
            if simulated["member_decisions"] != [dict(final_decisions[key]) for key in sorted(final_decisions)] or simulated["issues"] != _finalize_issue_order(issues.values(), candidates.values(), finding_ids):
                raise ValueError("annual_issue_pipeline_global_output_mismatch")
            simulated_closure = simulated["global_closure"]
            for key in (
                "closed", "closure_method", "scope", "input_issue_ids", "decision_count", "pending_issue_ids", "excluded_finding_ids",
                "relation_gaps", "input_finding_ids", "retained_finding_ids", "pending_finding_ids",
                "input_member_evidence", "retained_member_evidence", "excluded_member_evidence", "excluded_issue_account", "reason",
            ):
                if simulated_closure.get(key) != closure.get(key):
                    raise ValueError("annual_issue_pipeline_global_closure_mismatch")
        else:
            if (
                local_decisions != [dict(final_decisions[key]) for key in sorted(final_decisions)]
                or local_issues != _finalize_issue_order(issues.values(), candidates.values(), finding_ids)
                or closure.get("closed") is True
                or closure.get("decision_count") != 0
                or closure.get("pending_issue_ids") != issue_ids
            ):
                raise ValueError("annual_issue_pipeline_global_pending_mismatch")
    else:
        # Before global reconciliation, the visible issue/member ledger must
        # be exactly the deterministic projection of all accepted group shards.
        # This includes initial discovery checkpoints with no shard yet and
        # locally closed empty sets which need no global request.
        if closure.get("closure_method") == "single_group_shard":
            simulated = {
                "input": {"finding_ids": list(finding_ids)},
                "candidates": list(candidates.values()),
                "member_decisions": [dict(item) for item in local_decisions],
                "issues": [dict(item) for item in local_issues],
                "global_closure": {"coverage_gaps": list(closure.get("coverage_gaps", []))},
            }
            _close_single_group_shard_scope(simulated, local_issues, closure.get("coverage_gaps", []))
            if simulated["member_decisions"] != [dict(final_decisions[key]) for key in sorted(final_decisions)] or simulated["issues"] != _finalize_issue_order(issues.values(), candidates.values(), finding_ids):
                raise ValueError("annual_issue_pipeline_single_shard_output_mismatch")
            for key in (
                "closed", "closure_method", "scope", "input_issue_ids", "decision_count", "pending_issue_ids",
                "excluded_finding_ids", "relation_gaps", "coverage_gaps", "input_finding_ids", "retained_finding_ids",
                "pending_finding_ids", "input_member_evidence", "retained_member_evidence", "excluded_member_evidence",
                "excluded_issue_account", "reason",
            ):
                if simulated["global_closure"].get(key) != closure.get(key):
                    raise ValueError("annual_issue_pipeline_single_shard_closure_mismatch")
        elif (
            local_decisions != [dict(final_decisions[key]) for key in sorted(final_decisions)]
            or local_issues != _finalize_issue_order(issues.values(), candidates.values(), finding_ids)
        ):
            raise ValueError("annual_issue_pipeline_member_decision_rebuild_failed")

    # Rebuild the bounded supplement request from the complete accepted issue
    # ledger and validated retrieval pool. This makes role assignment reuse
    # depend on exact issue text, primary quotes, source locators, and pair set.
    if len(supplement_shards) > 1 or bool(supplements) != bool(supplement_shards):
        raise ValueError("annual_issue_pipeline_supplement_shard_invalid")
    if supplement_shards:
        supplement_shard = supplement_shards[0]
        expected_input = _supplement_input(
            [dict(item) for item in issues.values()], supplements, source,
        )
        if supplement_shard.get("input") != expected_input:
            raise ValueError("annual_issue_pipeline_supplement_input_invalid")
        submitted_pairs = {
            (row["issue_id"], row["evidence_id"])
            for row in expected_input["evidence_assignments"]
        }
        if supplement_shard.get("status") == "accepted":
            simulated_supplements = [dict(item) for item in supplements]
            _apply_supplement_roles(simulated_supplements, supplement_shard["result"])
            for item in simulated_supplements:
                assigned = {
                    (issue_id, item["evidence_id"])
                    for issue_id in item.get("issue_ids", [])
                }
                item["role_status"] = "accepted" if assigned and assigned.issubset(submitted_pairs) else "pending"
            output_fields = (
                "role", "issue_roles", "role_status", "independent_verification",
                "retrieval_is_verification",
            )
            for actual, expected in zip(supplements, simulated_supplements, strict=True):
                if any(actual.get(key) != expected.get(key) for key in output_fields):
                    raise ValueError("annual_issue_pipeline_supplement_output_mismatch")
        elif any(item.get("issue_roles") or item.get("role_status") != "pending" for item in supplements):
            raise ValueError("annual_issue_pipeline_supplement_pending_mismatch")


def _reconstruct_local_grouping(
    finding_ids: Sequence[str],
    candidates: Mapping[str, Mapping[str, Any]],
    accepted_decisions: Mapping[str, Mapping[str, Any]],
    source: Mapping[str, Any],
    *,
    pending_reason: str = "shard_pending",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_candidate = dict(candidates)
    roots: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for finding_id in finding_ids:
        decision = accepted_decisions.get(finding_id)
        if decision is None:
            rows.append({"finding_id": finding_id, "disposition": "pending", "issue_id": None, "target_finding_id": None, "reason_code": pending_reason, "reason_evidence_id": None, "reason_quote": None, "source_finding_id": finding_id})
        elif decision["disposition"] == "retain":
            issue_id = _issue_id(source, finding_id)
            roots[finding_id] = _make_issue(issue_id, by_candidate[finding_id], [finding_id], "provisional")
            rows.append({"finding_id": finding_id, "disposition": "retain", "issue_id": issue_id, "target_finding_id": None, "reason_code": decision["reason_code"], "reason_evidence_id": None, "reason_quote": None, "source_finding_id": finding_id})
        elif decision["disposition"] == "merge":
            target = decision["target_finding_id"]
            rows.append({"finding_id": finding_id, "disposition": "merge", "issue_id": _issue_id(source, target), "target_finding_id": target, "reason_code": decision["reason_code"], "reason_evidence_id": None, "reason_quote": None, "source_finding_id": finding_id})
        else:
            rows.append({"finding_id": finding_id, "disposition": "exclude", "issue_id": None, "target_finding_id": None, "reason_code": decision["reason_code"], "reason_evidence_id": decision["reason_evidence_id"], "reason_quote": decision["reason_quote"], "source_finding_id": finding_id})
    for row in rows:
        if row["disposition"] == "merge":
            issue = roots.get(str(row["target_finding_id"]))
            if issue is None:
                row.update(disposition="pending", issue_id=None, target_finding_id=None, reason_code="merge_target_unavailable", reason_evidence_id=None, reason_quote=None)
                continue
            issue["member_finding_ids"].append(row["finding_id"])
            candidate = by_candidate[row["finding_id"]]
            issue["evidence_ids"] = sorted(set(issue["evidence_ids"]) | set(candidate["evidence_ids"]))
            issue["citations"] = _dedupe_citations([*issue["citations"], *candidate["citations"]])
            issue["member_sources"].append({"finding_id": row["finding_id"], "disposition": "merge", "reason_code": row["reason_code"]})
    return sorted(rows, key=lambda item: item["finding_id"]), _finalize_issue_order(roots.values(), by_candidate.values(), finding_ids)


def _finalize_issue_order(
    issues: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]] | Any,
    finding_ids: Sequence[str],
) -> list[dict[str, Any]]:
    order = {str(finding_id): index for index, finding_id in enumerate(finding_ids)}
    finalized: list[dict[str, Any]] = []
    for raw in issues:
        issue = dict(raw)
        citations = [cite for cite in issue.get("citations", []) if isinstance(cite, Mapping)]
        locator_complete = bool(citations) and all(
            type(cite.get("pdf_page")) is int and type(cite.get("block_index")) is int
            and type(cite.get("start_char")) is int and isinstance(cite.get("bbox"), Mapping)
            and set(cite["bbox"]) == {"x0", "y0", "x1", "y1"}
            for cite in citations
        )
        source_order = min((order.get(member, len(order)) for member in issue.get("member_finding_ids", [])), default=len(order))
        explicit_follow_up = isinstance(issue.get("follow_up"), str) and bool(issue["follow_up"].strip())
        issue["review_order_reason"] = (
            f"review_pending; locator={'complete' if locator_complete else 'incomplete'}; "
            f"follow_up={'explicit' if explicit_follow_up else 'missing'}; source_order={source_order}; priority_unverified"
        )
        finalized.append(issue)

    def sort_key(issue: Mapping[str, Any]) -> tuple[bool, bool, int, str]:
        citations = [cite for cite in issue.get("citations", []) if isinstance(cite, Mapping)]
        locator_complete = bool(citations) and all(
            type(cite.get("pdf_page")) is int and type(cite.get("block_index")) is int
            and type(cite.get("start_char")) is int and isinstance(cite.get("bbox"), Mapping)
            and set(cite["bbox"]) == {"x0", "y0", "x1", "y1"}
            for cite in citations
        )
        followup_present = isinstance(issue.get("follow_up"), str) and bool(issue["follow_up"].strip())
        source_order = min((order.get(member, len(order)) for member in issue.get("member_finding_ids", [])), default=len(order))
        return not locator_complete, not followup_present, source_order, str(issue.get("issue_id", ""))

    return sorted(finalized, key=sort_key)


def build_issue_display_order(
    issue_pipeline: Mapping[str, Any],
    model_review: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a presentation-only order from validated review references and locators.

    The result is deliberately kept outside the issue pipeline and review semantic
    input. A completed review can surface the candidates it references and asks to
    investigate; locator completeness and explicit follow-up then break ties. The
    model's raw priority label is never used as a ranking signal.
    """

    raw_issues = issue_pipeline.get("issues")
    raw_candidates = issue_pipeline.get("candidates")
    issues = [dict(item) for item in raw_issues if isinstance(item, Mapping)] if isinstance(raw_issues, list) else []
    candidates = [dict(item) for item in raw_candidates if isinstance(item, Mapping)] if isinstance(raw_candidates, list) else []
    review_is_usable = (
        isinstance(model_review, Mapping)
        and model_review.get("status") == "completed"
        and model_review.get("source_binding_status", "matched") == "matched"
    )
    reason_refs: set[str] = set()
    follow_up_refs: set[str] = set()

    def string_items(value: Any) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    if review_is_usable:
        reasons = model_review.get("reasons")
        if isinstance(reasons, list):
            reason_refs = {
                reference
                for item in reasons if isinstance(item, Mapping)
                for reference in string_items(item.get("evidence_ids"))
            }
        follow_ups = model_review.get("follow_up_items")
        if isinstance(follow_ups, list):
            follow_up_refs = {
                reference
                for item in follow_ups if isinstance(item, Mapping)
                for reference in [*string_items(item.get("evidence_ids")), item.get("object")]
                if isinstance(reference, str)
            }

    candidate_by_id = {
        str(item.get("finding_id")): item
        for item in candidates if isinstance(item.get("finding_id"), str)
    }
    finding_order = {
        str(item.get("finding_id")): index
        for index, item in enumerate(candidates) if isinstance(item.get("finding_id"), str)
    }

    def evidence_refs(item: Mapping[str, Any]) -> set[str]:
        references = set(string_items(item.get("evidence_ids")))
        for citation in item.get("citations", []) if isinstance(item.get("citations"), list) else []:
            if isinstance(citation, Mapping) and isinstance(citation.get("evidence_id"), str):
                references.add(citation["evidence_id"])
        return references

    def has_complete_locators(item: Mapping[str, Any]) -> bool:
        citations = item.get("citations")
        return bool(citations) and isinstance(citations, list) and all(
            isinstance(citation, Mapping)
            and type(citation.get("pdf_page")) is int
            and type(citation.get("block_index")) is int
            and type(citation.get("start_char")) is int
            and isinstance(citation.get("bbox"), Mapping)
            and set(citation["bbox"]) == {"x0", "y0", "x1", "y1"}
            for citation in citations
        )

    def make_reason(item: Mapping[str, Any], member_ids: set[str]) -> tuple[tuple[int, int, int, int, int], str]:
        references = evidence_refs(item) | member_ids
        item_id = item.get("issue_id", item.get("finding_id"))
        if isinstance(item_id, str):
            references.add(item_id)
        cited_by_reason = bool(references & reason_refs)
        cited_by_follow_up = bool(references & follow_up_refs)
        explicit_follow_up = isinstance(item.get("follow_up"), str) and bool(item["follow_up"].strip())
        locator_complete = has_complete_locators(item)
        source_index = min((finding_order.get(member, len(finding_order)) for member in member_ids), default=len(finding_order))
        reason_parts = []
        if cited_by_reason:
            reason_parts.append("评审理由引用命中")
        if cited_by_follow_up:
            reason_parts.append("评审后续核查引用命中")
        if locator_complete:
            reason_parts.append("来源定位完整")
        if explicit_follow_up:
            reason_parts.append("存在明确后续核查动作")
        if not reason_parts:
            reason_parts.append("没有评审引用命中")
        reason_parts.append(f"同级按候选账稳定顺序第{source_index + 1}位")
        return (
            (-int(cited_by_reason), -int(cited_by_follow_up), -int(locator_complete), -int(explicit_follow_up), source_index),
            "；".join(reason_parts),
        )

    issue_scored: list[tuple[tuple[int, int, int, int, int], int, dict[str, Any], str]] = []
    candidate_to_issue: dict[str, str] = {}
    for source_index, issue in enumerate(issues):
        issue_id = issue.get("issue_id")
        if not isinstance(issue_id, str):
            continue
        member_ids = set(string_items(issue.get("member_finding_ids")))
        key, reason = make_reason(issue, member_ids)
        issue_scored.append((key, source_index, issue, reason))
        for member in member_ids:
            candidate_to_issue[member] = issue_id
    issue_scored.sort(key=lambda row: (row[0], row[1], str(row[2].get("issue_id", ""))))
    issue_order = [
        {"issue_id": str(issue["issue_id"]), "reason": reason}
        for _, _, issue, reason in issue_scored
    ]
    standalone: list[tuple[tuple[int, int, int, int, int], int, dict[str, Any], str]] = []
    for source_index, candidate in enumerate(candidates):
        finding_id = candidate.get("finding_id")
        if not isinstance(finding_id, str) or finding_id in candidate_to_issue:
            continue
        key, reason = make_reason(candidate, {finding_id})
        standalone.append((key, source_index, candidate, reason))
    standalone.sort(key=lambda row: (row[0], row[1], str(row[2].get("finding_id", ""))))
    candidate_order: list[dict[str, str]] = []
    for issue in issue_order:
        issue_id = issue["issue_id"]
        members = next(
            (entry.get("member_finding_ids", []) for entry in issues if entry.get("issue_id") == issue_id),
            [],
        )
        for member in members if isinstance(members, list) else []:
            if isinstance(member, str) and member in candidate_by_id:
                candidate_order.append({"finding_id": member, "reason": issue["reason"]})
    candidate_order.extend(
        {"finding_id": str(candidate["finding_id"]), "reason": reason}
        for _, _, candidate, reason in standalone
        if isinstance(candidate.get("finding_id"), str)
    )
    return {
        "schema_version": "annual_issue_display_order_v1",
        "issue_order": issue_order,
        "candidate_order": candidate_order,
    }


def _validated_findings(discovery: Mapping[str, Any], source: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if discovery.get("kind") != "fintrace_annual_discovery" or any(discovery.get(key) != source[key] for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256")):
        return [], {}
    raw_findings = discovery.get("findings")
    raw_evidences = discovery.get("evidences")
    if not isinstance(raw_findings, list) or not isinstance(raw_evidences, list):
        return [], {}
    evidences: dict[str, dict[str, Any]] = {}
    for item in raw_evidences:
        if not isinstance(item, Mapping) or item.get("source_document_id") != source["source_document_id"] or item.get("source_sha256") != source["source_sha256"]:
            continue
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not isinstance(item.get("raw_text"), str) or type(item.get("pdf_page")) is not int:
            continue
        evidences[evidence_id] = dict(item)
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_findings:
        if not isinstance(raw, Mapping):
            continue
        finding_id = raw.get("finding_id")
        cites = raw.get("citations")
        if not isinstance(finding_id, str) or not finding_id.startswith(f"disc:{source['source_sha256'][:12]}-") or finding_id in seen:
            continue
        if raw.get("classification") != "unverified_model_finding" or raw.get("verification_status") != "unverified" or not isinstance(cites, list) or not cites:
            continue
        normalized_cites = []
        valid = True
        for cite in cites:
            if not isinstance(cite, Mapping):
                valid = False
                break
            evidence_id, quote = cite.get("evidence_id"), cite.get("quote")
            evidence = evidences.get(evidence_id) if isinstance(evidence_id, str) else None
            has_finding_ids = evidence is not None and "finding_ids" in evidence
            finding_ids = evidence.get("finding_ids") if has_finding_ids else None
            if (
                evidence is None or not isinstance(quote, str) or not quote.strip()
                or quote not in evidence["raw_text"]
                or has_finding_ids and (not isinstance(finding_ids, list) or finding_id not in finding_ids)
            ):
                valid = False
                break
            normalized_cites.append({"evidence_id": evidence_id, "quote": quote})
        if not valid:
            continue
        finding = {key: raw.get(key) for key in ("finding_id", "title", "observation", "why_it_matters", "follow_up", "priority", "classification", "verification_status", "evidence_ids", "citations", "batch_index")}
        finding["citations"] = normalized_cites
        finding["evidence_ids"] = list(dict.fromkeys(item["evidence_id"] for item in normalized_cites))
        findings.append(finding)
        seen.add(finding_id)
    return findings, evidences


def _candidate_from_finding(finding: Mapping[str, Any], evidences: Mapping[str, Mapping[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    finding_id = str(finding["finding_id"])
    citations = []
    for cite in finding.get("citations", []):
        evidence = evidences[cite["evidence_id"]]
        citations.append({
            "evidence_id": cite["evidence_id"], "role": "primary_finding_quote", "quote": cite["quote"],
            "source_document_id": source["source_document_id"], "source_sha256": source["source_sha256"],
            "pdf_page": evidence["pdf_page"], "block_index": evidence.get("block_index"),
            "start_char": evidence.get("start_char"), "bbox": evidence.get("bbox"),
            "independent_verification": False,
        })
    return {
        "candidate_id": "candidate:" + hashlib.sha256((source["source_sha256"] + "\n" + finding_id).encode()).hexdigest()[:24],
        "origin": "annual_discovery_v3", "status": "qualified_unverified_finding",
        "finding_id": finding_id, "member_finding_ids": [finding_id],
        "title": finding["title"], "observation": finding["observation"],
        "why_it_matters": finding["why_it_matters"], "follow_up": finding["follow_up"],
        "priority": finding.get("priority") if finding.get("priority") in {"high", "medium", "low"} else "low",
        "evidence_ids": list(finding["evidence_ids"]), "citations": citations,
        "source_identity": {key: source[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")},
    }


def _local_exact_duplicate_groups(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Only exact observation/why/follow-up/citation matches may coalesce locally."""

    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for item in candidates:
        exact = {
            "title": item.get("title"),
            "observation": item.get("observation"), "why_it_matters": item.get("why_it_matters"),
            "follow_up": item.get("follow_up"),
            "evidence_ids": sorted(item.get("evidence_ids", [])),
            "citations": sorted((
                {key: cite.get(key) for key in ("evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index", "start_char", "bbox", "quote")}
                for cite in item.get("citations", []) if isinstance(cite, Mapping)
            ), key=lambda cite: (str(cite.get("evidence_id")), str(cite.get("quote")))),
        }
        buckets.setdefault(_sha_json(exact), []).append(item)
    result = []
    for group_id, rows in buckets.items():
        # Priority and title do not make the observations/citations identical;
        # retain title as the representative while preserving every raw item.
        representative = rows[0]
        result.append({
            "group_id": "local:" + group_id[:24],
            "representative_finding_id": representative["finding_id"],
            "member_finding_ids": sorted(str(row["finding_id"]) for row in rows),
            "member_candidates": [dict(row) for row in sorted(rows, key=lambda item: str(item["finding_id"]))],
            "representative": dict(representative),
        })
    return result


def _build_group_shards(groups: Sequence[Mapping[str, Any]], evidences: Mapping[str, Mapping[str, Any]], source: Mapping[str, Any], model: str, prompt: str) -> list[dict[str, Any]]:
    result = []
    current: list[Mapping[str, Any]] = []
    for group in groups:
        candidate = dict(group)
        trial = [*current, candidate]
        value = _group_input(trial, source)
        trial_member_count = sum(len(item.get("member_finding_ids", [])) for item in trial)
        if current and (trial_member_count > MAX_FINDINGS_PER_SHARD or _wire_chars(model, prompt, value) > MAX_SHARD_REQUEST_CHARS):
            result.append(_seal_input(_group_input(current, source), model, prompt))
            current = [candidate]
        else:
            current = trial
    if current:
        result.append(_seal_input(_group_input(current, source), model, prompt))
    return result


def _group_input(groups: Sequence[Mapping[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    member_ids = sorted(member for group in groups for member in group["member_finding_ids"])
    candidate_by_id: dict[str, dict[str, Any]] = {}
    group_rows = []
    for group in groups:
        for candidate in group["member_candidates"]:
            finding_id = str(candidate["finding_id"])
            prior = candidate_by_id.get(finding_id)
            if prior is not None and prior != dict(candidate):
                raise ValueError("annual_issue_pipeline_group_input_invalid")
            candidate_by_id[finding_id] = dict(candidate)
        group_rows.append({
            "group_id": group["group_id"],
            "representative_finding_id": group["representative_finding_id"],
            "member_finding_ids": list(group["member_finding_ids"]),
        })
    if set(candidate_by_id) != set(member_ids):
        raise ValueError("annual_issue_pipeline_group_input_invalid")
    return {
        "schema_version": "annual_issue_group_shard_input_v1",
        "source_identity": {key: source[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")},
        "groups": group_rows,
        "candidate_pool": [candidate_by_id[key] for key in sorted(candidate_by_id)],
        "input_member_finding_ids": member_ids,
    }


def _build_global_input(issues: Sequence[Mapping[str, Any]], decisions: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    issue_members = []
    for item in issues:
        roots = [row["finding_id"] for row in item.get("member_sources", []) if row.get("disposition") == "retain"]
        if len(roots) != 1:
            raise ValueError("annual_issue_pipeline_global_input_invalid")
        issue_members.append({
            "issue_id": item["issue_id"],
            "representative_finding_id": roots[0],
            "member_finding_ids": list(item["member_finding_ids"]),
        })
    issue_members.sort(key=lambda item: item["issue_id"])
    candidate_pool = [dict(item) for item in sorted(candidates, key=lambda row: str(row.get("finding_id", "")))]
    member_decisions = [dict(item) for item in sorted(decisions, key=lambda row: str(row.get("finding_id", "")))]
    return {
        "schema_version": "annual_issue_global_input_v1",
        "source_identity": {key: source[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")},
        "input_issue_ids": sorted(str(item["issue_id"]) for item in issues),
        "issue_members": issue_members,
        "candidate_pool": candidate_pool,
        "member_decisions": member_decisions,
        "source_candidate_count": len(candidates),
    }


def _apply_group_decisions(state: dict[str, Any], decisions: Mapping[str, Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]], source: Mapping[str, Any]) -> None:
    candidate_by_finding = {str(member): item for item in candidates for member in item.get("member_finding_ids", [])}
    decision_rows: list[dict[str, Any]] = []
    retained_issues: dict[str, dict[str, Any]] = {}
    for finding_id in state["input"]["finding_ids"]:
        candidate = candidate_by_finding[finding_id]
        item = decisions.get(finding_id)
        if item is None:
            decision_rows.append({"finding_id": finding_id, "disposition": "pending", "issue_id": None, "target_finding_id": None, "reason_code": "shard_pending", "reason_evidence_id": None, "reason_quote": None, "source_finding_id": finding_id})
            continue
        disposition = item["disposition"]
        if disposition == "retain":
            issue_id = _issue_id(source, finding_id)
            retained_issues[finding_id] = _make_issue(issue_id, candidate, [finding_id], "provisional")
            decision_rows.append({"finding_id": finding_id, "disposition": "retain", "issue_id": issue_id, "target_finding_id": None, "reason_code": item["reason_code"], "reason_evidence_id": None, "reason_quote": None, "source_finding_id": finding_id})
        elif disposition == "merge":
            target = item["target_finding_id"]
            issue_id = _issue_id(source, target)
            decision_rows.append({"finding_id": finding_id, "disposition": "merge", "issue_id": issue_id, "target_finding_id": target, "reason_code": item["reason_code"], "reason_evidence_id": None, "reason_quote": None, "source_finding_id": finding_id})
        else:
            decision_rows.append({"finding_id": finding_id, "disposition": "exclude", "issue_id": None, "target_finding_id": None, "reason_code": item["reason_code"], "reason_evidence_id": item["reason_evidence_id"], "reason_quote": item["reason_quote"], "source_finding_id": finding_id})
    for row in decision_rows:
        if row["disposition"] == "merge":
            issue = retained_issues.get(str(row["target_finding_id"]))
            if issue is None:
                row.update(disposition="pending", issue_id=None, target_finding_id=None, reason_code="merge_target_unavailable", reason_evidence_id=None, reason_quote=None)
                continue
            issue["member_finding_ids"].append(row["finding_id"])
            candidate = candidate_by_finding[row["finding_id"]]
            issue["evidence_ids"] = sorted(set(issue["evidence_ids"]) | set(candidate["evidence_ids"]))
            issue["citations"] = _dedupe_citations([*issue["citations"], *candidate["citations"]])
            issue["member_sources"].append({"finding_id": row["finding_id"], "disposition": "merge", "reason_code": row["reason_code"]})
    state["member_decisions"] = sorted(decision_rows, key=lambda item: item["finding_id"])
    state["issues"] = _finalize_issue_order(retained_issues.values(), candidates, state["input"]["finding_ids"])


def _make_issue(issue_id: str, candidate: Mapping[str, Any], members: list[str], status: str) -> dict[str, Any]:
    return {
        "issue_id": issue_id, "origin": "annual_discovery_v3", "status": status,
        "title": candidate["title"], "observation": candidate["observation"], "why_it_matters": candidate["why_it_matters"],
        "follow_up": candidate["follow_up"], "priority": candidate["priority"],
        "member_finding_ids": list(members), "evidence_ids": list(candidate["evidence_ids"]),
        "citations": list(candidate["citations"]), "member_sources": [{"finding_id": member, "disposition": "retain", "reason_code": "awaiting_global_reconciliation"} for member in members],
        "verification_status": "unverified", "independent_verification": False,
        "review_order_reason": "review_pending; locator=unknown; follow_up=unknown; source_order=unknown; priority_unverified",
    }


def _apply_global_decisions(state: dict[str, Any], result: Mapping[str, Any], issues: Sequence[Mapping[str, Any]]) -> None:
    decision_map = {item["issue_id"]: item for item in result.get("decisions", [])}
    issue_map = {item["issue_id"]: dict(item) for item in issues}
    expected = set(issue_map)
    if set(decision_map) != expected:
        state["global_closure"] = _pending_global_closure(state, issues, "global_result_membership_invalid")
        return
    retained = {key: value for key, value in decision_map.items() if value["disposition"] == "retain"}
    current_decisions = {item["finding_id"]: dict(item) for item in state["member_decisions"]}
    relation_gaps: list[str] = []
    pending_issues: list[str] = []
    excluded_finding_ids: list[str] = [item["finding_id"] for item in state["member_decisions"] if item["disposition"] == "exclude"]
    final_issues: list[dict[str, Any]] = []
    member_union_before = sorted(member for issue in issues for member in issue["member_finding_ids"])
    resolved_members: list[str] = []
    for issue_id, decision in decision_map.items():
        issue = issue_map[issue_id]
        if decision["disposition"] == "retain":
            issue["status"] = "pending_global_relation" if decision["reason_code"] == "relation_unclear" else "accepted"
            issue["global_relation"] = {"disposition": "retain", "target_issue_id": None, "reason_code": decision["reason_code"]}
            final_issues.append(issue)
            if decision["reason_code"] == "relation_unclear":
                relation_gaps.append(f"relation_unclear:{issue_id}")
                pending_issues.append(issue_id)
            resolved_members.extend(issue["member_finding_ids"])
            root_member = next((row["finding_id"] for row in issue.get("member_sources", []) if row.get("disposition") == "retain"), issue["member_finding_ids"][0])
            for member in issue["member_finding_ids"]:
                if member == root_member:
                    current_decisions[member].update(disposition="retain", issue_id=issue_id, target_finding_id=None, reason_code=decision["reason_code"], reason_evidence_id=None, reason_quote=None)
        elif decision["disposition"] == "merge":
            target = decision["target_issue_id"]
            target_issue = next((row for row in final_issues if row["issue_id"] == target), None)
            if target not in retained or target == issue_id:
                relation_gaps.append(f"invalid_target:{issue_id}")
                pending_issues.append(issue_id)
                continue
            # Targets may appear later in lexical order; locate the complete
            # retained target directly from the original issue table.
            target_issue = issue_map[target]
            target_issue.setdefault("global_merged_issue_ids", [])
            target_issue["global_merged_issue_ids"].append(issue_id)
            target_issue["member_finding_ids"] = sorted(set(target_issue["member_finding_ids"]) | set(issue["member_finding_ids"]))
            target_issue["evidence_ids"] = sorted(set(target_issue["evidence_ids"]) | set(issue["evidence_ids"]))
            target_issue["citations"] = _dedupe_citations([*target_issue["citations"], *issue["citations"]])
            target_issue["member_sources"] = [
                *target_issue.get("member_sources", []),
                *[
                    {**dict(member_source), "disposition": "merge", "reason_code": decision["reason_code"]}
                    for member_source in issue.get("member_sources", [])
                ],
            ]
            target_root = next((r["finding_id"] for r in target_issue["member_sources"] if r["disposition"] == "retain"), target)
            for member in issue["member_finding_ids"]:
                current_decisions[member].update(disposition="merge", issue_id=target, target_finding_id=target_root, reason_code=decision["reason_code"], reason_evidence_id=None, reason_quote=None)
            resolved_members.extend(issue["member_finding_ids"])
        else:
            issue["status"] = "excluded"
            issue["global_relation"] = {"disposition": "exclude", "target_issue_id": None, "reason_code": decision["reason_code"], "reason_evidence_id": decision["reason_evidence_id"], "reason_quote": decision["reason_quote"]}
            excluded_finding_ids.extend(issue["member_finding_ids"])
            for member in issue["member_finding_ids"]:
                current_decisions[member].update(disposition="exclude", issue_id=None, target_finding_id=None, reason_code=decision["reason_code"], reason_evidence_id=decision["reason_evidence_id"], reason_quote=decision["reason_quote"])
            resolved_members.extend(issue["member_finding_ids"])
            final_issues.append(issue)
    final_issues = [issue_map[key] for key, decision in decision_map.items() if decision["disposition"] in {"retain", "exclude"}]
    final_issue_ids = {item["issue_id"] for item in final_issues if item.get("status") == "accepted"}
    for issue_id, decision in decision_map.items():
        if decision["disposition"] == "merge":
            target = decision["target_issue_id"]
            if target not in final_issue_ids:
                relation_gaps.append(f"target_missing:{issue_id}")
                pending_issues.append(issue_id)
    output_members = sorted(member for issue in final_issues for member in issue["member_finding_ids"])
    # Excluded members remain in the explicit account and are part of the
    # conservation equation; retained issues never silently absorb them.
    excluded_unique = sorted(set(excluded_finding_ids))
    all_input_members = sorted(state["input"]["finding_ids"])
    if sorted([*output_members, *excluded_unique]) != all_input_members or len([*output_members, *excluded_unique]) != len(set([*output_members, *excluded_unique])):
        relation_gaps.append("member_union_conservation_failed")
    state["member_decisions"] = sorted(current_decisions.values(), key=lambda item: item["finding_id"])
    state["issues"] = _finalize_issue_order(final_issues, state.get("candidates", []), state.get("input", {}).get("finding_ids", []))
    closed = not relation_gaps and not pending_issues
    candidate_by_finding = {item["finding_id"]: item for item in state["candidates"]}
    input_member_evidence = sorted(
        (finding_id, evidence_id)
        for finding_id, candidate in candidate_by_finding.items()
        for evidence_id in candidate.get("evidence_ids", [])
    )
    retained_member_set = set(output_members)
    excluded_member_set = set(excluded_unique)
    retained_member_evidence = [pair for pair in input_member_evidence if pair[0] in retained_member_set]
    excluded_member_evidence = [pair for pair in input_member_evidence if pair[0] in excluded_member_set]
    excluded_issue_account = [
        {
            "issue_id": issue_id,
            "member_finding_ids": sorted(issue_map[issue_id]["member_finding_ids"]),
            "reason_code": decision["reason_code"],
            "reason_evidence_id": decision["reason_evidence_id"],
            "reason_quote": decision["reason_quote"],
        }
        for issue_id, decision in decision_map.items() if decision["disposition"] == "exclude"
    ]
    pending_member_ids = sorted(set(all_input_members) - retained_member_set - excluded_member_set)
    state["global_closure"] = {
        "closed": closed, "closure_method": "global_reconciliation", "scope": "current_known_findings", "input_issue_ids": sorted(expected),
        "decision_count": len(decision_map), "pending_issue_ids": sorted(set(pending_issues)),
        "excluded_finding_ids": excluded_unique, "relation_gaps": sorted(set(relation_gaps)),
        "input_finding_ids": all_input_members,
        "retained_finding_ids": sorted(retained_member_set),
        "pending_finding_ids": pending_member_ids,
        "input_member_evidence": [list(pair) for pair in input_member_evidence],
        "retained_member_evidence": [list(pair) for pair in retained_member_evidence],
        "excluded_member_evidence": [list(pair) for pair in excluded_member_evidence],
        "excluded_issue_account": excluded_issue_account,
        "coverage_gaps": list(state["global_closure"].get("coverage_gaps", [])),
        "discovery_coverage_complete": False,
        "no_priority_allowed": False,
        "reason": None if closed else "global_relation_unclosed",
    }


def _close_single_group_shard_scope(
    state: dict[str, Any],
    issues: Sequence[Mapping[str, Any]],
    coverage_gaps: Sequence[str],
) -> None:
    """Close only relationships fully observed inside the single grouping shard."""

    decisions = {item["finding_id"]: item for item in state["member_decisions"]}
    # Keep discovery order when deriving presentation/source-order metadata.
    # The validator rebuilds from input.finding_ids, whose original order is
    # the discovery order; set-valued closure fields are sorted independently.
    finding_ids = list(state["input"]["finding_ids"])
    excluded = sorted(
        finding_id for finding_id, row in decisions.items()
        if row.get("disposition") == "exclude"
    )
    pending_members = sorted(
        finding_id for finding_id in finding_ids
        if decisions.get(finding_id, {}).get("disposition") == "pending"
    )
    relation_gaps: list[str] = []
    closed_issues: list[dict[str, Any]] = []
    issue_ids: list[str] = []
    for raw_issue in issues:
        issue = dict(raw_issue)
        issue_id = str(issue["issue_id"])
        issue_ids.append(issue_id)
        member_statuses = {decisions.get(member, {}).get("disposition") for member in issue.get("member_finding_ids", [])}
        if member_statuses <= {"retain", "merge"} and all(member in decisions for member in issue.get("member_finding_ids", [])):
            issue["status"] = "accepted"
            issue["global_relation"] = {
                "disposition": "retain",
                "target_issue_id": None,
                "reason_code": "single_group_shard_complete",
            }
            closed_issues.append(issue)
        else:
            issue["status"] = "provisional"
            relation_gaps.append(f"member_path_pending:{issue_id}")
            closed_issues.append(issue)
    candidates = {
        item["finding_id"]: item for item in state.get("candidates", [])
        if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
    }
    input_member_evidence = sorted(
        (finding_id, evidence_id)
        for finding_id, candidate in candidates.items()
        for evidence_id in candidate.get("evidence_ids", [])
    )
    excluded_set = set(excluded)
    pending_set = set(pending_members)
    retained_members = set(finding_ids) - excluded_set - pending_set
    retained_member_evidence = [pair for pair in input_member_evidence if pair[0] in retained_members]
    excluded_member_evidence = [pair for pair in input_member_evidence if pair[0] in excluded_set]
    pending_issue_ids = sorted(
        issue_id for issue_id, issue in zip(issue_ids, closed_issues)
        if issue.get("status") != "accepted"
    )
    closed = not pending_members and not relation_gaps and not pending_issue_ids
    state["issues"] = _finalize_issue_order(closed_issues, candidates.values(), finding_ids)
    state["global_closure"] = {
        "closed": closed,
        "closure_method": "single_group_shard" if closed else "pending",
        "scope": "current_known_findings",
        "input_issue_ids": sorted(issue_ids),
        "decision_count": len(issue_ids) if closed else 0,
        "pending_issue_ids": pending_issue_ids,
        "excluded_finding_ids": excluded,
        "relation_gaps": sorted(relation_gaps),
        "coverage_gaps": list(coverage_gaps),
        "input_finding_ids": finding_ids,
        "retained_finding_ids": sorted(retained_members),
        "pending_finding_ids": pending_members,
        "input_member_evidence": [list(pair) for pair in input_member_evidence],
        "retained_member_evidence": [list(pair) for pair in retained_member_evidence],
        "excluded_member_evidence": [list(pair) for pair in excluded_member_evidence],
        "excluded_issue_account": [],
        "reason": None if closed else "grouping_members_pending",
        "discovery_coverage_complete": False,
        "no_priority_allowed": False,
    }


def _pending_global_closure(state: Mapping[str, Any], issues: Sequence[Mapping[str, Any]], reason: str) -> dict[str, Any]:
    candidates = state.get("candidates", [])
    candidate_by_finding = {
        item["finding_id"]: item for item in candidates
        if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
    }
    finding_ids = sorted(state.get("input", {}).get("finding_ids", []))
    excluded = sorted(item["finding_id"] for item in state.get("member_decisions", []) if item.get("disposition") == "exclude")
    excluded_set = set(excluded)
    pairs = sorted(
        (finding_id, evidence_id)
        for finding_id, candidate in candidate_by_finding.items()
        for evidence_id in candidate.get("evidence_ids", [])
    )
    return {
        "closed": False, "closure_method": "pending", "scope": "current_known_findings",
        "input_issue_ids": sorted(str(item["issue_id"]) for item in issues), "decision_count": 0,
        "pending_issue_ids": sorted(str(item["issue_id"]) for item in issues),
        "excluded_finding_ids": excluded, "relation_gaps": [],
        "coverage_gaps": list(state.get("global_closure", {}).get("coverage_gaps", [])),
        "input_finding_ids": finding_ids, "retained_finding_ids": [],
        "pending_finding_ids": sorted(set(finding_ids) - excluded_set),
        "input_member_evidence": [list(pair) for pair in pairs], "retained_member_evidence": [],
        "excluded_member_evidence": [list(pair) for pair in pairs if pair[0] in excluded_set],
        "excluded_issue_account": [],
        "reason": reason, "discovery_coverage_complete": False, "no_priority_allowed": False,
    }


def _supplement_input(issues: Sequence[Mapping[str, Any]], supplements: Sequence[Mapping[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    assignments: list[dict[str, Any]] = []
    for item in supplements:
        for issue_id in item["issue_ids"]:
            if len(assignments) >= MAX_SUPPLEMENT_PAIRS:
                break
            assignments.append({"issue_id": issue_id, "evidence_id": item["evidence_id"], "role": "unknown"})
        if len(assignments) >= MAX_SUPPLEMENT_PAIRS:
            break
    selected_issue_ids = {item["issue_id"] for item in assignments}
    selected_evidence_ids = {item["evidence_id"] for item in assignments}
    issue_by_id = {str(item["issue_id"]): item for item in issues}
    if not selected_issue_ids.issubset(issue_by_id):
        raise ValueError("annual_issue_pipeline_supplement_input_invalid")
    issue_rows = []
    for issue_id in sorted(selected_issue_ids):
        item = issue_by_id[issue_id]
        issue_rows.append({
            "issue_id": issue_id,
            "member_finding_ids": list(item["member_finding_ids"]),
            "title": item["title"],
            "observation": item["observation"],
            "why_it_matters": item["why_it_matters"],
            "primary_finding_quotes": [
                {"evidence_id": citation["evidence_id"], "quote": citation["quote"]}
                for citation in sorted(item.get("citations", []), key=lambda row: (row["evidence_id"], row["quote"]))
            ],
        })
    evidence_rows = []
    seen_evidence_ids: set[str] = set()
    for item in supplements:
        if item["evidence_id"] not in selected_evidence_ids or item["evidence_id"] in seen_evidence_ids:
            continue
        seen_evidence_ids.add(item["evidence_id"])
        evidence_rows.append({key: item[key] for key in (
            "evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index",
            "start_char", "end_char", "bbox", "raw_text", "section", "selection_reason",
        )})
    if any(item["evidence_id"] not in seen_evidence_ids for item in assignments):
        raise ValueError("annual_issue_pipeline_supplement_input_invalid")
    return {
        "schema_version": "annual_issue_supplement_input_v1",
        "source_identity": {key: source[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")},
        "issues": issue_rows,
        "supplements": evidence_rows,
        "evidence_assignments": assignments,
        "role_semantics": {
            "primary_finding_quote": "Original discovery citation already attached to the finding; fixed role and not independent verification. It is not an assignable retrieval role.",
            "context": "Retrieved same-source context only; not verification or a counterexample.",
            "possible_explanation": "Possible alternative explanation to investigate; not established fact or causation.",
            "unknown": "Relationship to the issue is unclear.",
        },
    }


def _evidence_refs(input_value: Mapping[str, Any]) -> list[EvidenceRef]:
    pages: set[tuple[str, int]] = set()
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            doc, page = value.get("source_document_id"), value.get("pdf_page")
            if isinstance(doc, str) and type(page) is int and page > 0:
                pages.add((doc, page))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(input_value)
    return [EvidenceRef(doc, page) for doc, page in sorted(pages)]


def _audit_record(audit_dir: Path, artifacts_root: Path, wire_hash: str, semantic_hash: str, kind: str) -> dict[str, Any]:
    relative = lambda path: path.resolve().relative_to(artifacts_root.resolve()).as_posix()
    return {
        "audit_dir": relative(audit_dir), "request_path": relative(audit_dir / "request.json"),
        "response_path": relative(audit_dir / "response.json"), "failure_path": None,
        "status": "succeeded", "prompt_version": {"grouping": GROUP_PROMPT_VERSION, "global": "annual_issue_global_v1", "supplement": "annual_issue_supplement_v1"}[kind],
        "request_body_sha256": wire_hash, "semantic_input_sha256": semantic_hash,
    }


def _latest_audit(run_dir: Path, artifacts_root: Path, prompt_version: str, wire_hash: str, semantic_hash: str, kind: str) -> dict[str, Any] | None:
    prompt_name = {
        "grouping": "annual_issue_grouping_v1.md",
        "global": "annual_issue_global_v1.md",
        "supplement": "annual_issue_supplement_v1.md",
    }.get(kind)
    if prompt_name is None:
        return None
    try:
        expected_prompt = _load_group_prompt(prompt_name)
    except (OSError, ValueError):
        return None
    for path in sorted(run_dir.iterdir(), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        request = path / "request.json"
        if not path.is_dir() or not request.is_file():
            continue
        try:
            if request.stat().st_size > 2 * 1024 * 1024:
                continue
            record = json.loads(request.read_text(encoding="utf-8"))
            messages = record.get("messages")
            if (
                not isinstance(record, Mapping) or record.get("prompt_version") != prompt_version
                or not isinstance(record.get("model"), str) or record.get("stream") is not False
                or not isinstance(messages, list) or len(messages) != 2
                or messages[0] != {"role": "system", "content": expected_prompt}
                or not isinstance(messages[1], Mapping) or messages[1].get("role") != "user"
                or not isinstance(messages[1].get("content"), str)
            ):
                continue
            parsed_envelope = json.loads(messages[1]["content"], object_pairs_hook=_unique_object_pairs)
            parsed_input = decode_annual_issue_input_wire(parsed_envelope)
            if (
                not isinstance(parsed_input, Mapping)
                or _semantic_sha(parsed_input) != semantic_hash
                or _issue_wire_content(parsed_input) != messages[1]["content"]
                or messages != _wire_message_dicts(expected_prompt, parsed_input)
            ):
                continue
            wire = serialize_chat_body(record["model"], [ChatMessage(role=row["role"], content=row["content"]) for row in messages])
            actual_wire_hash = hashlib.sha256(wire).hexdigest()
            if actual_wire_hash != wire_hash:
                continue
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
        response_path, failure_path = path / "response.json", path / "failure.json"
        if response_path.is_file() == failure_path.is_file():
            continue
        try:
            if response_path.is_file():
                response = json.loads(response_path.read_text(encoding="utf-8"))
                if not isinstance(response, Mapping) or response.get("status") != "succeeded" or not isinstance(response.get("text"), str):
                    continue
            else:
                failure = json.loads(failure_path.read_text(encoding="utf-8"))
                if not isinstance(failure, Mapping) or failure.get("status") != "failed" or not isinstance(failure.get("category"), str):
                    continue
            relative = lambda item: item.resolve().relative_to(artifacts_root.resolve()).as_posix()
            return {
                "audit_dir": relative(path), "request_path": relative(request),
                "response_path": relative(response_path) if response_path.is_file() else None,
                "failure_path": relative(failure_path) if failure_path.is_file() else None,
                "status": "succeeded" if response_path.is_file() else "failed", "prompt_version": prompt_version,
                "request_body_sha256": actual_wire_hash, "semantic_input_sha256": _semantic_sha(parsed_input),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return None


def _try_reuse_shard(old: Mapping[str, Any], shard: Mapping[str, Any], source: Mapping[str, Any], artifacts_root: Path, kind: str) -> bool:
    if old.get("kind") != kind or old.get("status") != "accepted" or old.get("semantic_input_sha256") != shard.get("semantic_sha256") or old.get("request_body_sha256") != shard.get("wire_body_sha256"):
        return False
    result, audit = old.get("result"), old.get("audit")
    if not isinstance(result, Mapping) or not isinstance(audit, Mapping) or audit.get("status") != "succeeded" or audit.get("request_body_sha256") != shard.get("wire_body_sha256"):
        return False
    if not _reconstruct_audit_result(audit, shard, artifacts_root, kind, result):
        return False
    try:
        strict = parse_typed_result(_read_audit_response(audit, artifacts_root), kind=kind, input_value=shard["input"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
    return strict == dict(result)


def _reconstruct_audit_result(audit: Mapping[str, Any], shard: Mapping[str, Any], artifacts_root: Path, kind: str, result: Mapping[str, Any]) -> bool:
    try:
        request_path = (artifacts_root / str(audit["request_path"])).resolve(strict=True)
        response_path = (artifacts_root / str(audit["response_path"])).resolve(strict=True)
        root = artifacts_root.resolve(strict=True)
        if not request_path.is_relative_to(root) or not response_path.is_relative_to(root):
            return False
        record = json.loads(request_path.read_text(encoding="utf-8"))
        messages = record.get("messages")
        prompt_name = {
            "grouping": "annual_issue_grouping_v1.md",
            "global": "annual_issue_global_v1.md",
            "supplement": "annual_issue_supplement_v1.md",
        }[kind]
        prompt_version = {"grouping": GROUP_PROMPT_VERSION, "global": "annual_issue_global_v1", "supplement": "annual_issue_supplement_v1"}[kind]
        if (
            not isinstance(messages, list) or len(messages) != 2
            or record.get("prompt_version") != prompt_version
            or messages[0] != {"role": "system", "content": _load_group_prompt(prompt_name)}
            or not isinstance(messages[1], Mapping)
            or messages[1].get("role") != "user"
            or not isinstance(messages[1].get("content"), str)
        ):
            return False
        envelope = json.loads(messages[1]["content"], object_pairs_hook=_unique_object_pairs)
        decoded_input = decode_annual_issue_input_wire(envelope)
        if (
            _semantic_sha(decoded_input) != shard.get("semantic_sha256")
            or _semantic_sha(decoded_input) != _semantic_sha(shard["input"])
            or _issue_wire_content(decoded_input) != messages[1]["content"]
            or messages != _wire_message_dicts(_load_group_prompt(prompt_name), decoded_input)
        ):
            return False
        wire = _serialize_chat_messages(record.get("model"), messages)
        if (
            hashlib.sha256(wire).hexdigest() != shard.get("wire_body_sha256")
            or hashlib.sha256(wire).hexdigest() != audit.get("request_body_sha256")
            or _semantic_sha(shard["input"]) != audit.get("semantic_input_sha256")
        ):
            return False
        raw = json.loads(response_path.read_text(encoding="utf-8"))
        return raw.get("status") == "succeeded" and isinstance(raw.get("text"), str)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _read_audit_response(audit: Mapping[str, Any], artifacts_root: Path) -> str:
    path = (artifacts_root / str(audit["response_path"])).resolve(strict=True)
    if not path.is_relative_to(artifacts_root.resolve(strict=True)):
        raise ValueError("audit_path_invalid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    text = payload.get("text")
    if payload.get("status") != "succeeded" or not isinstance(text, str):
        raise ValueError("audit_response_invalid")
    return text


def _parent_shards(parent: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(parent, Mapping) or not isinstance(parent.get("shards"), list):
        return {}
    return {str(item["shard_id"]): item for item in parent["shards"] if isinstance(item, Mapping) and isinstance(item.get("shard_id"), str)}


def _accepted_shard(shard_id: str, shard: Mapping[str, Any], result: Mapping[str, Any], audit: Mapping[str, Any], *, reused: bool, parent_run_id: str | None, attempt_count: int = 0, response_count: int = 0) -> dict[str, Any]:
    return {
        "shard_id": shard_id, "kind": "global" if shard_id.startswith("global:") else "supplement" if shard_id.startswith("supplement:") else "grouping",
        "status": "accepted", "semantic_input_sha256": shard["semantic_sha256"], "request_body_sha256": shard["wire_body_sha256"],
        "input_finding_ids": list(shard["input"].get("input_member_finding_ids", [])),
        "input_issue_ids": list(shard["input"].get("input_issue_ids", [])),
        "input_members": [dict(item) for item in shard["input"].get("groups", [])] if isinstance(shard["input"].get("groups"), list) else [],
        "input": dict(shard["input"]), "result": dict(result), "audit": dict(audit),
        "attempt_count": attempt_count, "response_count": response_count, "reused": reused,
        "parent_run_id": parent_run_id if reused else None,
    }


def _pending_shard(shard_id: str, shard: Mapping[str, Any], reason: str, *, audit: Mapping[str, Any] | None = None, attempts: int = 0, responses: int = 0, kind: str | None = None) -> dict[str, Any]:
    return {
        "shard_id": shard_id, "kind": kind or ("global" if shard_id.startswith("global:") else "supplement" if shard_id.startswith("supplement:") else "grouping"), "status": "pending", "reason": reason,
        "semantic_input_sha256": shard.get("semantic_sha256"), "request_body_sha256": shard.get("wire_body_sha256"),
        "input_finding_ids": list(shard.get("input", {}).get("input_member_finding_ids", [])),
        "input_issue_ids": list(shard.get("input", {}).get("input_issue_ids", [])),
        "input_members": [dict(item) for item in shard.get("input", {}).get("groups", [])] if isinstance(shard.get("input", {}).get("groups"), list) else [],
        "input": dict(shard["input"]), "result": None, "audit": dict(audit) if isinstance(audit, Mapping) else None,
        "attempt_count": attempts, "response_count": responses, "reused": False, "parent_run_id": None,
    }


def _apply_supplement_roles(supplements: list[dict[str, Any]], result: Mapping[str, Any]) -> None:
    role_map = {(row["issue_id"], row["evidence_id"]): row for row in result.get("roles", [])}
    for supplement in supplements:
        roles = [role_map[(issue_id, supplement["evidence_id"])] for issue_id in supplement.get("issue_ids", []) if (issue_id, supplement["evidence_id"]) in role_map]
        supplement["issue_roles"] = [{"issue_id": row["issue_id"], "role": row["role"], "reason_code": row["reason_code"]} for row in roles]
        supplement["role"] = roles[0]["role"] if roles and len({row["role"] for row in roles}) == 1 else "unknown"
        supplement["independent_verification"] = False
        supplement["retrieval_is_verification"] = False


def _audit_summary_from_shards(shards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    attempts = sum(item.get("attempt_count", 0) for item in shards)
    responses = sum(item.get("response_count", 0) for item in shards)
    reused = sum(item.get("reused") is True for item in shards)
    audits = [dict(audit) for item in shards if isinstance((audit := item.get("audit")), Mapping)]
    return {
        "attempt_count": attempts,
        "response_count": responses,
        "reused_count": reused,
        "model_called": attempts > 0,
        "audit_artifacts": audits,
    }


def _publish(value: Mapping[str, Any], callback: Callable[[Mapping[str, Any]], None] | None) -> None:
    if callback is not None:
        callback(value)


def _stop_reason(deadline: float | None, main_deadline: float | None, cancel: Callable[[], bool] | None) -> str | None:
    if cancel is not None and cancel():
        return "cancelled"
    now = time.monotonic()
    if main_deadline is not None and now >= main_deadline:
        return "workflow_deadline"
    if deadline is not None and now >= deadline:
        return "stage_deadline"
    return None


def _wire_hash(model: str, prompt: str, input_value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_wire_body(model, prompt, input_value)).hexdigest()


def _wire_chars(model: str, prompt: str, input_value: Mapping[str, Any]) -> int:
    return len(_wire_body(model, prompt, input_value).decode("utf-8"))


def _issue_wire_content(input_value: Mapping[str, Any]) -> str:
    envelope = encode_annual_issue_input_wire(input_value)
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _wire_message_dicts(prompt: str, input_value: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": _issue_wire_content(input_value)},
    ]


def _wire_messages(prompt: str, input_value: Mapping[str, Any]) -> list[ChatMessage]:
    return [ChatMessage(role=item["role"], content=item["content"]) for item in _wire_message_dicts(prompt, input_value)]


def _serialize_chat_messages(model: str, messages: Sequence[Mapping[str, str]]) -> bytes:
    return serialize_chat_body(
        model,
        [ChatMessage(role=item["role"], content=item["content"]) for item in messages],
    )


def _wire_body(model: str, prompt: str, input_value: Mapping[str, Any]) -> bytes:
    return _serialize_chat_messages(model, _wire_message_dicts(prompt, input_value))


def _seal_input(input_value: Mapping[str, Any], model: str, prompt: str) -> dict[str, Any]:
    return {"input": dict(input_value), "semantic_sha256": _semantic_sha(input_value), "wire_body_sha256": _wire_hash(model, prompt, input_value)}


def _semantic_sha(value: Mapping[str, Any]) -> str:
    # run_id, audit paths, current attempt counts and reuse state are excluded.
    return _sha_json(value)


def _sha_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _source_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    source = {
        "run_id": value.get("run_id"), "company_id": value.get("company_id"), "report_year": value.get("report_year"),
        "report_period": value.get("report_period"), "source_document_id": value.get("source_document_id", value.get("document_id")),
        "source_sha256": str(value.get("source_sha256", value.get("sha256", ""))).lower(),
    }
    if not isinstance(source["run_id"], str) or not isinstance(source["company_id"], str) or type(source["report_year"]) is not int or not isinstance(source["report_period"], str) or not isinstance(source["source_document_id"], str) or _SHA.fullmatch(source["source_sha256"]) is None:
        raise ValueError("annual_issue_pipeline_identity_invalid")
    return source


def _issue_id(source: Mapping[str, Any], finding_id: str) -> str:
    digest = hashlib.sha256((source["source_sha256"] + "\n" + finding_id).encode()).hexdigest()[:24]
    return f"issue:{source['source_sha256'][:12]}:{digest}"


def _empty_global_closure(reason: str) -> dict[str, Any]:
    return {
        "closed": False, "closure_method": "pending", "scope": "current_known_findings", "input_issue_ids": [], "decision_count": 0,
        "pending_issue_ids": [], "excluded_finding_ids": [], "relation_gaps": [], "coverage_gaps": [],
        "input_finding_ids": [], "retained_finding_ids": [], "pending_finding_ids": [],
        "input_member_evidence": [], "retained_member_evidence": [], "excluded_member_evidence": [], "excluded_issue_account": [],
        "reason": reason, "discovery_coverage_complete": False, "no_priority_allowed": False,
    }


def _empty_retrieval_coverage(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_document_id": source["source_document_id"], "source_sha256": source["source_sha256"],
        "scope": "not_started", "requested_sections": [], "examined_sections": [], "total_pages": 0,
        "total_text_blocks": 0, "scanned_blocks": 0, "matched_blocks": 0,
        "matched_issue_block_pairs": 0, "selected_blocks": 0, "selected_issue_block_pairs": 0,
        "unselected_issue_block_pairs": 0, "scanned_chars": 0, "selected_excerpt_chars": 0,
        "selected_unique_source_chars": 0,
        "unread_pages": [], "unread_blocks": 0, "omitted_chars": 0, "complete": False,
        "selection_algorithm_version": "section_filter_then_ngram_overlap_v1", "max_terms_per_issue": 40,
        "max_matches_per_issue": 4, "max_excerpt_chars": 500, "max_role_assignments": MAX_SUPPLEMENT_PAIRS,
        "role_assignments_total": 0, "role_assignments_requested": 0, "role_assignments_pending": 0,
        "reason": "supplement_not_started",
    }


def _load_group_prompt(name: str) -> str:
    path = _PROMPT_PATH.with_name(name)
    return path.read_text(encoding="utf-8")


def _limited(value: Any, maximum: int) -> str:
    return value if isinstance(value, str) and len(value) <= maximum else str(value or "")[:maximum]


def _safe_count(exc: BaseException, name: str) -> int:
    value = getattr(exc, name, 0)
    return value if type(value) is int and 0 <= value <= 64 else 0


def _dedupe_citations(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for item in items:
        key = (item.get("evidence_id"), item.get("quote"))
        unique[key] = dict(item)
    return [unique[key] for key in sorted(unique)]
