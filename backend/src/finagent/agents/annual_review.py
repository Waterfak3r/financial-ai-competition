"""One bounded, audited model opinion for a v2 annual analysis report."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from finagent.audit import EvidenceRef, audited_complete_chat
from finagent.audit.audited_chat import repository_runs_root
from finagent.core.annual_work_budget import (
    BUDGET_CONTRACT_VERSION,
    GLOBAL_REQUEST_LIMIT_CHARS,
    MODEL_CALL_TIMEOUT_SECONDS,
    REVIEW_REQUEST_LIMIT_CHARS,
)
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage
from finagent.llm.chat_completion import serialize_chat_body
from finagent.agents.annual_review_wire import (
    V3_INPUT_PROJECTION_VERSION,
    V3_WIRE_SCHEMA,
    decode_annual_review_v3_wire,
    encode_annual_review_v3_wire,
    project_annual_review_v3_input,
)

PROMPT_VERSION = "annual_review_v2"
V3_PROMPT_VERSION = "annual_review_v3"
V3_REVIEW_WIRE_VERSION = V3_WIRE_SCHEMA
V3_REVIEW_INPUT_PROJECTION_VERSION = V3_INPUT_PROJECTION_VERSION
MAX_REQUEST_CHARS = 16_000
V3_MAX_REQUEST_CHARS = 24_000
MAX_RESPONSE_CHARS = 12_000
MAX_FACTS = 40
MAX_CALCULATIONS = 40
MAX_SIGNALS = 30
MAX_PENDING_ITEMS = 60
MAX_INVESTIGATION_ITEMS = 30
MAX_DISCOVERY_ITEMS = 12
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_review_v2.md"
_V3_PROMPT_PATH = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_review_v3.md"

ASSESSMENTS = frozenset(
    {
        "prioritize_review",
        "no_priority_issue_identified_within_scope",
        "insufficient_evidence",
    }
)


def build_annual_review_v3_request(
    review_input: Mapping[str, Any], *, model: str, system_prompt: str,
) -> dict[str, Any]:
    """Build one projection-bound typed input and the exact official Chat body."""

    if not isinstance(review_input, Mapping) or not isinstance(model, str) or not model or not isinstance(system_prompt, str):
        raise ValueError("annual_review_v3_request_invalid")
    projected_input = project_annual_review_v3_input(review_input)
    envelope = encode_annual_review_v3_wire(projected_input)
    user_content = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_content),
    ]
    body = serialize_chat_body(model, messages)
    semantic_json = json.dumps(review_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    projected_json = json.dumps(projected_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "projected_input": projected_input,
        "envelope": envelope,
        "user_content": user_content,
        "messages": messages,
        "body": body,
        "request_chars": len(body.decode("utf-8")),
        "semantic_input_sha256": hashlib.sha256(semantic_json.encode("utf-8")).hexdigest(),
        "projected_input_sha256": hashlib.sha256(projected_json.encode("utf-8")).hexdigest(),
        "request_body_sha256": hashlib.sha256(body).hexdigest(),
        "review_wire_version": V3_REVIEW_WIRE_VERSION,
        "review_input_projection_version": V3_REVIEW_INPUT_PROJECTION_VERSION,
    }


def not_called_model_review(report: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Return a stable, opinion-free state when a review could not start."""

    identity = _report_identity(report)
    return {
        "status": "not_called",
        "reason": reason,
        "assessment": None,
        "summary": None,
        "reasons": [],
        "follow_up_items": [],
        "limitations": [],
        "model_called": False,
        "call_count": 0,
        "call_attempt_count": 0,
        "audit_artifacts": [],
        "source_identity": identity,
    }


def run_annual_model_review(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    settings: ModelSettings,
    *,
    run_dir: Path,
    artifacts_root: Path,
) -> dict[str, Any]:
    """Make exactly one audited call and validate every returned reference."""

    identity = _report_identity(report)
    result = not_called_model_review(report, "model_review_not_started")
    result["status"] = "failed"
    result["reason"] = "model_review_input_invalid"
    expected_runs_root = repository_runs_root().resolve()
    try:
        checked_run = run_dir.resolve(strict=True)
        checked_artifacts = artifacts_root.resolve(strict=True)
    except OSError:
        result["reason"] = "model_review_run_binding_invalid"
        return result
    if (
        checked_run.parent != expected_runs_root
        or checked_run.name != identity["run_id"]
        or checked_artifacts != expected_runs_root.parent
    ):
        result["reason"] = "model_review_run_binding_invalid"
        return result
    before = _run_children(run_dir)
    audited = None
    try:
        prompt = _load_prompt()
        review_input, allowed_ids, evidence_refs, no_priority_allowed = _build_review_input(
            report,
            investigation,
            identity,
            request_model=settings.model,
            system_prompt=prompt,
        )
        serialized = json.dumps(review_input, ensure_ascii=False, separators=(",", ":"))
        if _actual_request_chars(settings.model, prompt, serialized) > MAX_REQUEST_CHARS:
            return result
        audited = audited_complete_chat(
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(role="user", content=serialized),
            ],
            settings,
            run_dir=run_dir,
            evidence_refs=evidence_refs,
            prompt_version=PROMPT_VERSION,
            call_stage="annual_review",
        )
        audit_artifacts = [_audit_artifact(audited.audit_dir, artifacts_root, "succeeded")]
        parsed = parse_model_review(
            audited.completion.text,
            allowed_ids=allowed_ids,
            no_priority_allowed=no_priority_allowed,
        )
        return {
            "status": "completed",
            "reason": None,
            **parsed,
            "model_called": True,
            "call_count": audited.response_count,
            "call_attempt_count": audited.provider_admission_count,
            "audit_artifacts": audit_artifacts,
            "source_identity": identity,
        }
    except Exception as exc:
        audits, _attempts_from_audit_dirs = _new_audits(run_dir, before, artifacts_root)
        if audited is not None:
            attempts = audited.provider_admission_count
            responses = audited.response_count
        else:
            attempts = _safe_exception_count(exc, "provider_admission_count")
            responses = _safe_exception_count(exc, "response_count")
        response_present = any(a.get("response_path") for a in audits)
        known_response_reasons = {
            "model_review_response_invalid", "model_review_numeric_claim_rejected",
            "model_review_no_priority_not_supported", "model_review_priority_without_reason",
            "model_review_no_priority_without_reason",
        }
        safe_reason = str(exc) if isinstance(exc, ValueError) and str(exc) in known_response_reasons else None
        result.update(
            reason=safe_reason or ("model_review_response_invalid" if response_present else "model_review_call_failed"),
            model_called=attempts > 0, call_count=responses,
            call_attempt_count=attempts, audit_artifacts=audits,
        )
        return result

def run_streamlined_annual_review(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    settings: ModelSettings,
    *,
    run_dir: Path,
    artifacts_root: Path,
) -> dict[str, Any]:
    """Run deterministic-bound single-call model review for streamlined analysis."""
    return run_annual_model_review(
        report,
        investigation,
        settings,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )


def not_called_model_review_v3(report: Mapping[str, Any], reason: str, *, status: str = "not_called") -> dict[str, Any]:
    """Execution_v5 review state, separate from the immutable v2 shape."""

    if status not in {"not_called", "abstained", "failed"}:
        raise ValueError("annual_review_v3_status_invalid")
    return {
        "status": status, "reason": reason, "assessment": None, "summary": None,
        "reasons": [], "follow_up_items": [], "limitations": [], "model_called": False,
        "call_count": 0, "call_attempt_count": 0, "audit_artifacts": [],
        "source_identity": _report_identity(report), "prompt_version": V3_PROMPT_VERSION,
    }


def run_annual_model_review_v3(
    report: Mapping[str, Any], investigation: Mapping[str, Any], settings: ModelSettings,
    *, run_dir: Path, artifacts_root: Path,
) -> dict[str, Any]:
    """Review the entire issue-pipeline candidate path under the activity v9 bound."""

    identity = _report_identity(report)
    result = not_called_model_review_v3(report, "annual_review_v3_input_invalid", status="failed")
    expected_runs_root = repository_runs_root().resolve()
    try:
        checked_run = run_dir.resolve(strict=True)
        checked_artifacts = artifacts_root.resolve(strict=True)
    except OSError:
        result["reason"] = "model_review_run_binding_invalid"
        return result
    if checked_run.parent != expected_runs_root or checked_run.name != identity["run_id"] or checked_artifacts != expected_runs_root.parent:
        result["reason"] = "model_review_run_binding_invalid"
        return result
    before = _run_children(run_dir)
    audited = None
    review_request = None
    try:
        prompt = _V3_PROMPT_PATH.read_text(encoding="utf-8")
        limit = _activity_review_request_limit_chars(investigation)
        review_input, allowed_ids, evidence_refs, no_priority_allowed = _build_review_input_v3(
            report, investigation, identity, request_model=settings.model,
            system_prompt=prompt, max_request_chars=limit,
        )
        review_request = build_annual_review_v3_request(review_input, model=settings.model, system_prompt=prompt)
        messages = review_request["messages"]
        if review_request["request_chars"] > limit:
            return not_called_model_review_v3(report, "annual_review_v3_request_budget_exceeded", status="abstained")
        audited = audited_complete_chat(
            messages, settings, run_dir=run_dir, evidence_refs=evidence_refs,
            prompt_version=V3_PROMPT_VERSION, call_stage="annual_review",
            timeout_seconds=MODEL_CALL_TIMEOUT_SECONDS,
        )
        audit = _audit_artifact_v3(
            audited.audit_dir, artifacts_root, "succeeded", messages=messages,
            model=settings.model, request_binding=review_request,
        )
        parsed = parse_model_review_v3(
            audited.completion.text, allowed_ids=allowed_ids,
            no_priority_allowed=no_priority_allowed,
            pipeline=investigation.get("model_issue_pipeline"),
            limitations_required=review_input["limitations_required"],
        )
        return {
            "status": "completed", "reason": None, **parsed,
            "model_called": audited.provider_admission_count > 0,
            "call_count": audited.response_count,
            "call_attempt_count": audited.provider_admission_count,
            "audit_artifacts": [audit], "source_identity": identity,
            "prompt_version": V3_PROMPT_VERSION,
        }
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc) == "annual_review_v3_execution_binding_invalid":
            return not_called_model_review_v3(report, "annual_review_v3_execution_binding_invalid", status="failed")
        if isinstance(exc, ValueError) and str(exc) in {"annual_review_request_budget_exceeded", "annual_review_v3_request_budget_exceeded"}:
            return not_called_model_review_v3(report, "annual_review_v3_request_budget_exceeded", status="abstained")
        if audited is not None:
            attempts, responses = audited.provider_admission_count, audited.response_count
            audits = [_audit_artifact_v3(
                audited.audit_dir, artifacts_root, "succeeded",
                request_binding=review_request,
            )]
        else:
            audits, _attempts_from_dirs = _new_audits(run_dir, before, artifacts_root)
            if review_request is not None:
                audits = [
                    _bind_v3_audit_request(item, review_request)
                    for item in audits
                ]
            attempts = _safe_exception_count(exc, "provider_admission_count")
            responses = _safe_exception_count(exc, "response_count")
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
            "model_review_response_invalid", "model_review_numeric_claim_rejected",
            "model_review_no_priority_not_supported", "model_review_priority_without_reason",
            "model_review_no_priority_without_reason", "model_review_unverified_claim_rejected", "model_review_limitations_incomplete",
        } else "model_review_response_invalid" if any(item.get("response_path") for item in audits) else "model_review_call_failed"
        result.update(reason=reason, model_called=attempts > 0, call_count=responses,
                      call_attempt_count=attempts, audit_artifacts=audits)
        return result


def parse_model_review_v3(
    text: str, *, allowed_ids: set[str], no_priority_allowed: bool, pipeline: Any,
    limitations_required: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("model_review_response_invalid")
    try:
        json.loads(text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError):
        raise ValueError("model_review_response_invalid") from None
    parsed = parse_model_review(text, allowed_ids=allowed_ids, no_priority_allowed=no_priority_allowed)
    closure = pipeline.get("global_closure") if isinstance(pipeline, Mapping) else None
    if not isinstance(closure, Mapping):
        raise ValueError("model_review_response_invalid")
    if (closure.get("closed") is not True or closure.get("pending_issue_ids") or closure.get("relation_gaps") or closure.get("coverage_gaps")) and parsed["assessment"] == "no_priority_issue_identified_within_scope":
        raise ValueError("model_review_no_priority_not_supported")
    required_limits = limitations_required if limitations_required is not None else _required_review_limitations(pipeline)
    for marker in required_limits:
        if not isinstance(marker, str) or marker not in parsed["limitations"]:
            raise ValueError("model_review_limitations_incomplete")
    if required_limits and parsed["assessment"] == "no_priority_issue_identified_within_scope":
        raise ValueError("model_review_no_priority_not_supported")
    texts = [parsed["summary"], *(item["text"] for item in parsed["reasons"]), *(item["action"] for item in parsed["follow_up_items"]), *parsed["limitations"]]
    banned_assertions = re.compile(r"确认舞弊|属于舞弊|证明舞弊|证实舞弊|已经造假|确认造假|证明造假|证实造假|确认虚增|确认挪用|事实反证|足以证明")
    banned_causality = re.compile(r"导致|造成|引发|证明了|证实了|说明了|表明了|因此可知|由于.{0,30}所以|因.{1,30}而")
    if any(_contains_new_numeric_claim(value) or banned_assertions.search(value) or banned_causality.search(value) for value in texts):
        raise ValueError("model_review_unverified_claim_rejected")
    return parsed


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


_CN_NUMERAL = "零〇一二两三四五六七八九十百千万亿兆壹贰叁肆伍陆柒捌玖拾佰仟"
_CN_MEASURE = re.compile(
    rf"(?:百分之[{_CN_NUMERAL}]+|[{_CN_NUMERAL}]+(?:人民币|万元|亿元|万港元|亿元人民币|港元|美元|元|圆|角|分|整|%|％|‰|个百分点|百分点|倍|次|项|个|家|笔|年|月|日|季度|天|周|期|股|吨|人|份|起|条|种|户|科目))"
)
_ARABIC_MEASURE = re.compile(r"[0-9０-９]+(?:[.,，．][0-9０-９]+)?(?:%|％|‰|元|万元|亿元|美元|港元|倍|次|项|个|家|笔|年|月|日|季度|天|周|期|股|吨|人|份|起|条|种|户|科目)?")


def _contains_new_numeric_claim(text: str) -> bool:
    if any(unicodedata.category(char) == "Nd" or unicodedata.numeric(char, None) is not None and char not in _CN_NUMERAL for char in text):
        return True
    return bool(_CN_MEASURE.search(text) or _ARABIC_MEASURE.search(text))


def _safe_exception_count(exc: BaseException, field: str) -> int:
    value = getattr(exc, field, 0)
    return value if type(value) is int and 0 <= value <= 64 else 0


def parse_model_review(
    text: str,
    *,
    allowed_ids: set[str],
    no_priority_allowed: bool,
) -> dict[str, Any]:
    """Require an exact small schema and reject unsupported references or figures."""

    if not isinstance(text, str) or not text.strip() or len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("model_review_response_invalid")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        raise ValueError("model_review_response_invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "assessment", "summary", "reasons", "follow_up_items", "limitations"
    }:
        raise ValueError("model_review_response_invalid")
    assessment = payload.get("assessment")
    if assessment not in ASSESSMENTS:
        raise ValueError("model_review_response_invalid")
    summary = _bounded_text(payload.get("summary"), 800)
    reasons_raw = payload.get("reasons")
    followups_raw = payload.get("follow_up_items")
    limitations_raw = payload.get("limitations")
    if not isinstance(reasons_raw, list) or len(reasons_raw) > 6:
        raise ValueError("model_review_response_invalid")
    if not isinstance(followups_raw, list) or len(followups_raw) > 8:
        raise ValueError("model_review_response_invalid")
    if not isinstance(limitations_raw, list) or len(limitations_raw) > 8:
        raise ValueError("model_review_response_invalid")

    reasons: list[dict[str, Any]] = []
    for item in reasons_raw:
        if not isinstance(item, dict) or set(item) != {"text", "evidence_ids"}:
            raise ValueError("model_review_response_invalid")
        refs = _validated_refs(item.get("evidence_ids"), allowed_ids, required=assessment != "insufficient_evidence")
        reasons.append({"text": _bounded_text(item.get("text"), 500), "evidence_ids": refs})

    followups: list[dict[str, Any]] = []
    for item in followups_raw:
        if not isinstance(item, dict) or set(item) != {"object", "action", "evidence_ids"}:
            raise ValueError("model_review_response_invalid")
        target = item.get("object")
        if not isinstance(target, str) or target not in allowed_ids:
            raise ValueError("model_review_response_invalid")
        refs = _validated_refs(item.get("evidence_ids"), allowed_ids, required=True)
        if target not in refs:
            raise ValueError("model_review_response_invalid")
        followups.append({
            "object": target,
            "action": _bounded_text(item.get("action"), 400),
            "evidence_ids": refs,
        })

    limitations = [_bounded_text(item, 400) for item in limitations_raw]
    for value in [summary, *(item["text"] for item in reasons), *(item["action"] for item in followups), *limitations]:
        # Prevent new Arabic-numeral claims in model-authored opinion text. IDs are
        # carried separately and must already exist in the deterministic report.
        if re.search(r"\d", value):
            raise ValueError("model_review_numeric_claim_rejected")
    if assessment == "no_priority_issue_identified_within_scope" and not no_priority_allowed:
        raise ValueError("model_review_no_priority_not_supported")
    if assessment == "prioritize_review" and not reasons:
        raise ValueError("model_review_priority_without_reason")
    if assessment == "no_priority_issue_identified_within_scope" and not reasons:
        raise ValueError("model_review_no_priority_without_reason")
    return {
        "assessment": assessment,
        "summary": summary,
        "reasons": reasons,
        "follow_up_items": followups,
        "limitations": limitations,
    }


def _build_review_input(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    request_model: str | None = None,
    system_prompt: str | None = None,
    max_serialized_chars: int = MAX_REQUEST_CHARS,
) -> tuple[dict[str, Any], set[str], list[EvidenceRef], bool]:
    confirmed = report.get("confirmed")
    if not isinstance(confirmed, Mapping):
        raise ValueError("confirmed_section_missing")
    verified_facts: list[dict[str, Any]] = []
    verified_calculations: list[dict[str, Any]] = []
    pending_items: list[dict[str, Any]] = []
    allowed: set[str] = set()
    evidence_pages: set[int] = set()
    truncated = False
    truncation_state = {"value": False}

    def mark_truncated() -> None:
        truncation_state["value"] = True
    unvalidated_confirmed_count = 0

    metric_records = confirmed.get("metrics")
    if not isinstance(metric_records, list):
        metric_records = []
        unvalidated_confirmed_count += 1
    for record in metric_records:
        prepared = _verified_fact_input(record, identity, evidence_pages)
        if prepared is None:
            unvalidated_confirmed_count += 1
            pending_items.append(_unvalidated_item("fact", record))
            continue
        verified_facts.append(prepared)
        allowed.add(prepared["fact_id"])

    analysis_records = confirmed.get("analyses")
    if not isinstance(analysis_records, list):
        analysis_records = []
        unvalidated_confirmed_count += 1
    verified_fact_ids = {item["fact_id"] for item in verified_facts}
    for record in analysis_records:
        prepared = _verified_calculation_input(record, verified_fact_ids)
        if prepared is None:
            unvalidated_confirmed_count += 1
            pending_items.append(_unvalidated_item("calculation", record))
            continue
        verified_calculations.append(prepared)
        allowed.add(prepared["calculation_id"])

    pending = report.get("pending_review")
    if not isinstance(pending, Mapping):
        pending = {}
        unvalidated_confirmed_count += 1
    for key, kind in (("facts", "fact"), ("calculations", "calculation"), ("claims", "claim")):
        entries = pending.get(key, [])
        if not isinstance(entries, list):
            truncated = True
            continue
        for record in entries:
            item = _pending_item(kind, record)
            if item is None:
                truncated = True
                continue
            pending_items.append(item)
            allowed.add(item["review_id"])

    candidate_records = pending.get("candidate_signals", [])
    candidate_signals: list[dict[str, Any]] = []
    if not isinstance(candidate_records, list):
        candidate_records = []
        unvalidated_confirmed_count += 1
    confirmed_calculation_ids = {item["calculation_id"] for item in verified_calculations}
    for record in candidate_records:
        if not isinstance(record, Mapping):
            truncated = True
            continue
        signal_id = record.get("signal_id")
        if not isinstance(signal_id, str) or _ID_RE.fullmatch(signal_id) is None:
            truncated = True
            continue
        refs = record.get("calculation_ids", [])
        valid_refs = (
            [item for item in refs if isinstance(item, str) and item in confirmed_calculation_ids]
            if isinstance(refs, list)
            else []
        )
        signal_status = record.get("status")
        if signal_status not in {"candidate", "abstained", "not_triggered"}:
            signal_status = "unknown"
            truncated = True
        candidate_signals.append({
            "signal_id": signal_id,
            "status": signal_status,
            "calculation_ids": valid_refs,
            "binding_status": "candidate_signal_unverified",
        })
        allowed.add(signal_id)

    investigation_identity_matches = _identity_matches(investigation, identity)
    investigation_items: list[dict[str, Any]] = []
    raw_items = investigation.get("items", []) if investigation_identity_matches else []
    if not isinstance(raw_items, list):
        raw_items = []
        unvalidated_confirmed_count += 1
    for item in raw_items:
        if not isinstance(item, Mapping):
            truncated = True
            continue
        signal_id = item.get("signal_id")
        if not isinstance(signal_id, str) or _ID_RE.fullmatch(signal_id) is None:
            truncated = True
            continue
        review_id = f"investigation:{signal_id}"
        if len(review_id) > 200:
            truncated = True
            continue
        investigation_items.append({
            "review_id": review_id,
            "status": item.get("status") if item.get("status") in {"interpretation", "abstained"} else "unknown",
            "classification": "unverified_model_material",
            "explanation": _limited_optional_text(item.get("explanation"), 350, mark_truncated),
            "reason": _limited_optional_text(item.get("reason"), 120, mark_truncated),
            "limitations": _text_list(item.get("limitations"), 3, 160),
        })
        evidence_ids = item.get("narrative_evidence_ids", [])
        if isinstance(evidence_ids, (list, tuple)):
            for eid in evidence_ids:
                if isinstance(eid, str) and _ID_RE.fullmatch(eid):
                    allowed.add(eid)
        allowed.add(review_id)
        allowed.add(signal_id)

    raw_discovery = investigation.get("discovery")
    if raw_discovery is None and investigation.get("status") in {"completed", "abstained"}:
        discovery_identity_matches = True
        discovery_status = "completed"
        discovery_coverage = {"complete": True, "truncated": False, "partial_batches": 0}
        raw_discovery_findings = []
        raw_discovery_evidences = []
    else:
        discovery_identity_matches = (
            raw_discovery.get("kind") == "fintrace_annual_discovery"
            and _identity_matches(raw_discovery, identity)
            if isinstance(raw_discovery, Mapping)
            else False
        )
        discovery_status = (
            raw_discovery.get("status") if discovery_identity_matches else "identity_mismatch"
        )
        discovery_coverage = (
            raw_discovery.get("coverage")
            if discovery_identity_matches and isinstance(raw_discovery.get("coverage"), Mapping)
            else {}
        )
        raw_discovery_findings = (
            list(raw_discovery.get("findings", []))
            if discovery_identity_matches and isinstance(raw_discovery.get("findings", []), list)
            else raw_discovery.get("findings", []) if discovery_identity_matches else []
        )
        raw_discovery_evidences = (
            raw_discovery.get("evidences", []) if discovery_identity_matches else []
        )
    discovery_items: list[dict[str, Any]] = []
    discovery_evidence_records: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_discovery_evidences, list):
        for evidence in raw_discovery_evidences:
            if (
                isinstance(evidence, Mapping)
                and evidence.get("source_document_id") == identity["source_document_id"]
                and evidence.get("source_sha256") == identity["source_sha256"]
                and isinstance(evidence.get("evidence_id"), str)
                and type(evidence.get("pdf_page")) is int
                and evidence["pdf_page"] > 0
            ):
                discovery_evidence_records[evidence["evidence_id"]] = evidence
    discovery_omitted_count = 0
    if not isinstance(raw_discovery_findings, list):
        raw_discovery_findings = []
        discovery_omitted_count = 1
    discovery_total_count = len(raw_discovery_findings)
    priority_order = {"high": 0, "medium": 1, "low": 2}
    raw_discovery_findings.sort(
        key=lambda item: priority_order.get(item.get("priority"), 3)
        if isinstance(item, Mapping) and isinstance(item.get("priority"), str)
        else 4
    )
    if len(raw_discovery_findings) > MAX_DISCOVERY_ITEMS:
        discovery_omitted_count += len(raw_discovery_findings) - MAX_DISCOVERY_ITEMS
        raw_discovery_findings = raw_discovery_findings[:MAX_DISCOVERY_ITEMS]
    for finding in raw_discovery_findings:
        if not isinstance(finding, Mapping):
            discovery_omitted_count += 1
            continue
        finding_id = finding.get("finding_id")
        evidence_ids = finding.get("evidence_ids", [])
        if (
            not isinstance(finding_id, str)
            or _ID_RE.fullmatch(finding_id) is None
            or not finding_id.startswith(f"disc:{identity['source_sha256'][:12]}-")
            or finding.get("classification") != "unverified_model_finding"
            or finding.get("verification_status") != "unverified"
            or not isinstance(evidence_ids, list)
            or any(not isinstance(value, str) or len(value) > 200 for value in evidence_ids)
        ):
            discovery_omitted_count += 1
            continue
        if not evidence_ids or any(
            evidence_id not in discovery_evidence_records
            or (
                isinstance(discovery_evidence_records[evidence_id].get("finding_ids"), list)
                and finding_id not in discovery_evidence_records[evidence_id]["finding_ids"]
            )
            for evidence_id in evidence_ids
        ):
            discovery_omitted_count += 1
            continue
        priority = finding.get("priority")
        if not isinstance(priority, str) or priority not in {"high", "medium", "low"}:
            priority = "low"
            discovery_omitted_count += 1
        citation_pages = sorted({
            int(discovery_evidence_records[evidence_id]["pdf_page"])
            for evidence_id in evidence_ids
            if evidence_id in discovery_evidence_records
        })[:8]
        discovery_items.append({
            "finding_id": finding_id,
            "classification": "unverified_model_finding",
            "verification_status": "unverified",
            "title": _limited_optional_text(finding.get("title"), 100, mark_truncated),
            "observation": _limited_optional_text(finding.get("observation"), 360, mark_truncated),
            "why_it_matters": _limited_optional_text(finding.get("why_it_matters"), 240, mark_truncated),
            "follow_up": _limited_optional_text(finding.get("follow_up"), 240, mark_truncated),
            "priority": priority,
            "evidence_ids": list(dict.fromkeys(evidence_ids))[:4],
            "citation_pages": citation_pages,
        })
        allowed.add(finding_id)

    initial_candidate_count = sum(item.get("status") == "candidate" for item in candidate_signals)
    initial_signal_abstentions = sum(item.get("status") in {"abstained", "unknown"} for item in candidate_signals)

    # Make hard bounds explicit and visible to the model. No list is silently
    # truncated: the final assessment gate sees the flag and blocks optimism.
    if len(verified_facts) > MAX_FACTS:
        verified_facts = verified_facts[:MAX_FACTS]
        truncated = True
    if len(verified_calculations) > MAX_CALCULATIONS:
        verified_calculations = verified_calculations[:MAX_CALCULATIONS]
        truncated = True
    if len(candidate_signals) > MAX_SIGNALS:
        candidate_signals = candidate_signals[:MAX_SIGNALS]
        truncated = True
    if len(pending_items) > MAX_PENDING_ITEMS:
        pending_items = pending_items[:MAX_PENDING_ITEMS]
        truncated = True
    if len(investigation_items) > MAX_INVESTIGATION_ITEMS:
        investigation_items = investigation_items[:MAX_INVESTIGATION_ITEMS]
        truncated = True

    gaps = report.get("scope", {}).get("gaps", []) if isinstance(report.get("scope"), Mapping) else []
    if not isinstance(gaps, list):
        gaps = ["scope_gap_record_invalid"]
        unvalidated_confirmed_count += 1
    safe_gaps = [_limited_optional_text(item, 180, mark_truncated) for item in gaps[:20] if isinstance(item, str)]
    if len(gaps) > 20:
        truncated = True

    investigation_status = investigation.get("status") if investigation_identity_matches else "identity_mismatch"
    investigation_reason = investigation.get("reason") if investigation_identity_matches else "investigation_source_identity_mismatch"
    abstained_count = sum(item.get("status") != "interpretation" for item in investigation_items)
    signal_abstention_count = sum(item.get("status") in {"abstained", "unknown"} for item in candidate_signals)
    comparability = report.get("comparability")
    comparability_status = comparability.get("status") if isinstance(comparability, Mapping) else None
    comparability_matches = comparability is None or (
        isinstance(comparability, Mapping)
        and comparability.get("document_id") == identity["source_document_id"]
        and isinstance(comparability.get("source_sha256"), str)
        and comparability["source_sha256"].lower() == identity["source_sha256"]
        and comparability.get("report_year") == identity["report_year"]
        and comparability_status in {"verified", "conflict", "insufficient_evidence"}
    )

    data = {
        "schema_version": "annual_model_review_v2",
        "source_identity": dict(identity),
        "coverage": {
            "verified_fact_count": len(verified_facts),
            "verified_calculation_count": len(verified_calculations),
            "pending_object_count": len(pending_items),
            "candidate_signal_count": len(candidate_signals),
            "candidate_count": sum(item.get("status") == "candidate" for item in candidate_signals),
            "signal_abstention_count": signal_abstention_count,
            "investigation_status": investigation_status,
            "investigation_reason": investigation_reason,
            "investigation_abstention_count": abstained_count,
            "investigation_identity_matches": investigation_identity_matches,
            "discovery_status": discovery_status,
            "discovery_identity_matches": discovery_identity_matches,
            "discovery_findings_count": discovery_total_count,
            "discovery_omitted_count": discovery_omitted_count,
            "discovery_input_omitted_count": 0,
            "discovery_sent_finding_ids": [item["finding_id"] for item in discovery_items],
            "discovery_coverage_complete": discovery_coverage.get("complete") is True,
            "discovery_truncated": discovery_coverage.get("truncated") is True,
            "discovery_coverage": {
                key: discovery_coverage.get(key, 0 if key == "partial_batches" else None)
                for key in (
                    "total_pages", "text_pages", "total_blocks", "total_chars",
                    "planned_batches", "processed_batches", "successful_batches",
                    "partial_batches", "failed_batches", "processed_chars", "unread_pages", "failed_pages",
                    "truncated", "complete", "no_text_pages", "image_pages",
                    "unread_image_pages", "text_only_scope",
                )
            },
            "comparability_status": comparability_status,
            "comparability_identity_matches": comparability_matches,
            "unvalidated_confirmed_record_count": unvalidated_confirmed_count,
            "scope_gaps": safe_gaps,
            "truncated": truncated,
            # Reserved before the request-size loop so its exact JSON cost is
            # included even when the value changes from false to true later.
            "no_priority_eligible": False,
        },
        "verified_facts": verified_facts,
        "verified_calculations": verified_calculations,
        "candidate_signals": candidate_signals,
        "unresolved_review_items": pending_items,
        "investigation_items": investigation_items,
        "discovery_items": discovery_items,
    }
    # Keep the full message within a small bound. Drop tail records in a fixed
    # order, keep coverage counts as counts of the supplied subset, and block
    # no-priority conclusions whenever a record had to be omitted.
    lists = [
        "investigation_items", "unresolved_review_items", "candidate_signals",
        "verified_calculations", "verified_facts", "discovery_items",
    ]
    def request_size() -> int:
        user_content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if request_model is None or system_prompt is None:
            return len(user_content)
        return _actual_request_chars(request_model, system_prompt, user_content)

    while request_size() > max_serialized_chars and any(data[key] for key in lists):
        for key in lists:
            if data[key]:
                removed = data[key].pop()
                if key == "verified_facts":
                    allowed.discard(str(removed.get("fact_id")))
                elif key == "verified_calculations":
                    allowed.discard(str(removed.get("calculation_id")))
                elif key == "candidate_signals":
                    allowed.discard(str(removed.get("signal_id")))
                elif key == "unresolved_review_items":
                    allowed.discard(str(removed.get("review_id")))
                elif key == "discovery_items":
                    allowed.discard(str(removed.get("finding_id")))
                    data["coverage"]["discovery_omitted_count"] += 1
                    data["coverage"]["discovery_input_omitted_count"] += 1
                    data["coverage"]["discovery_sent_finding_ids"] = [
                        item["finding_id"] for item in data["discovery_items"]
                    ]
                else:
                    allowed.discard(str(removed.get("review_id")))
                data["coverage"]["truncated"] = True
                break
    sent_fact_ids = {item["fact_id"] for item in data["verified_facts"]}
    filtered_calculations = [
        item for item in data["verified_calculations"]
        if all(fact_id in sent_fact_ids for fact_id in item["input_fact_ids"])
    ]
    if len(filtered_calculations) != len(data["verified_calculations"]):
        data["coverage"]["truncated"] = True
        data["verified_calculations"] = filtered_calculations
    sent_calculation_ids = {item["calculation_id"] for item in data["verified_calculations"]}
    for signal in data["candidate_signals"]:
        signal["calculation_ids"] = [
            item for item in signal["calculation_ids"] if item in sent_calculation_ids
        ]
    evidence_pages = {
        page for item in data["verified_facts"] for page in item.get("pdf_pages", [])
    }
    evidence_pages.update(
        page
        for item in data["discovery_items"]
        for page in item.get("citation_pages", [])
    )
    pages = [EvidenceRef(str(identity["source_document_id"]), page) for page in sorted(evidence_pages)[:80]]
    if len(evidence_pages) > 80:
        data["coverage"]["truncated"] = True
    if truncation_state["value"]:
        data["coverage"]["truncated"] = True
    data["coverage"]["verified_fact_count"] = len(data["verified_facts"])
    data["coverage"]["verified_calculation_count"] = len(data["verified_calculations"])
    data["coverage"]["pending_object_count"] = len(data["unresolved_review_items"])
    data["coverage"]["candidate_signal_count"] = len(data["candidate_signals"])
    data["coverage"]["candidate_count"] = sum(item.get("status") == "candidate" for item in data["candidate_signals"])
    data["coverage"]["signal_abstention_count"] = sum(item.get("status") in {"abstained", "unknown"} for item in data["candidate_signals"])
    data["coverage"]["discovery_sent_finding_ids"] = [
        item["finding_id"] for item in data["discovery_items"]
    ]
    # Rebuild the citation whitelist from precisely the records that were sent.
    allowed = {
        item["fact_id"] for item in data["verified_facts"]
    } | {
        item["calculation_id"] for item in data["verified_calculations"]
    } | {
        item["signal_id"] for item in data["candidate_signals"]
    } | {
        item["review_id"] for item in data["unresolved_review_items"]
    } | {
        item["review_id"] for item in data["investigation_items"]
    } | {
        item["finding_id"] for item in data["discovery_items"]
    }
    evidence_pages = {
        page for item in data["verified_facts"] for page in item.get("pdf_pages", [])
    }
    scope_gaps_are_only_candidate_notice = bool(safe_gaps) and all(
        ("候选" in gap or "财务背离" in gap) for gap in safe_gaps
    )
    substantive_scope_gaps = [] if scope_gaps_are_only_candidate_notice else safe_gaps
    needs_comparability = any(
        item.get("formula_id") == "annual_difference" for item in data["verified_calculations"]
    )
    comparability_allows_priority = (
        comparability_status == "verified" if needs_comparability else comparability_status in {None, "verified"}
    )
    no_priority_allowed = (
        not data["coverage"]["truncated"]
        and not data["coverage"]["unvalidated_confirmed_record_count"]
        and data["coverage"]["verified_fact_count"] >= 2
        and data["coverage"]["verified_calculation_count"] >= 1
        and data["coverage"]["pending_object_count"] == 0
        and data["coverage"]["candidate_count"] == 0
        and initial_candidate_count == 0
        and data["coverage"]["signal_abstention_count"] == 0
        and initial_signal_abstentions == 0
        and data["coverage"]["investigation_identity_matches"]
        and data["coverage"]["discovery_identity_matches"]
        and data["coverage"]["discovery_status"] == "completed"
        and data["coverage"]["discovery_coverage_complete"]
        and data["coverage"]["discovery_coverage"].get("partial_batches", 0) == 0
        and not data["coverage"]["discovery_truncated"]
        and data["coverage"]["discovery_findings_count"] == 0
        and data["coverage"]["discovery_omitted_count"] == 0
        and data["coverage"]["comparability_identity_matches"]
        and (
            (investigation_status == "completed" and abstained_count == 0)
            or (investigation_status == "abstained" and investigation_reason == "no_supported_candidate_signal" and not investigation_items)
        )
        and comparability_allows_priority
        and not substantive_scope_gaps
    )
    data["coverage"]["no_priority_eligible"] = no_priority_allowed
    if request_size() > max_serialized_chars:
        raise ValueError("annual_review_request_budget_exceeded")
    return data, allowed, pages, no_priority_allowed




def _activity_review_request_limit_chars(investigation: Mapping[str, Any]) -> int:
    """Admit activity review only from the v11 same-page source-context contract."""

    pipeline = investigation.get("model_issue_pipeline")
    binding = pipeline.get("execution_binding") if isinstance(pipeline, Mapping) else None
    if (
        not isinstance(binding, Mapping)
        or binding.get("kind") != "fintrace_annual_execution_binding_v11"
        or binding.get("execution_version") != "execution_v11"
        or binding.get("budget_contract_version") != BUDGET_CONTRACT_VERSION
        or type(binding.get("global_request_limit_chars")) is not int
        or binding.get("global_request_limit_chars") != GLOBAL_REQUEST_LIMIT_CHARS
        or type(binding.get("review_request_limit_chars")) is not int
        or binding.get("review_request_limit_chars") != REVIEW_REQUEST_LIMIT_CHARS
        or type(binding.get("model_call_timeout_seconds")) is not float
        or binding.get("model_call_timeout_seconds") != MODEL_CALL_TIMEOUT_SECONDS
        or binding.get("grouping_shard_plan_version") != "member_bounded_v2"
        or type(binding.get("grouping_shard_max_members")) is not int
        or binding.get("grouping_shard_max_members") != 3
    ):
        raise ValueError("annual_review_v3_execution_binding_invalid")
    return REVIEW_REQUEST_LIMIT_CHARS



def _build_review_input_v3(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    request_model: str,
    system_prompt: str,
    enforce_budget: bool = True,
    max_request_chars: int = V3_MAX_REQUEST_CHARS,
) -> tuple[dict[str, Any], set[str], list[EvidenceRef], bool]:
    """Build an untruncated v3 semantic input; size failure abstains as a whole."""
    if type(max_request_chars) is not int or max_request_chars <= 0:
        raise ValueError("annual_review_v3_request_budget_invalid")
    if max_request_chars > REVIEW_REQUEST_LIMIT_CHARS:
        raise ValueError("annual_review_v3_request_budget_invalid")


    confirmed = report.get("confirmed")
    if not isinstance(confirmed, Mapping):
        raise ValueError("confirmed_section_missing")
    pipeline_raw = investigation.get("model_issue_pipeline")
    pipeline = _review_pipeline_semantics(pipeline_raw)
    if pipeline is None:
        raise ValueError("annual_review_v3_issue_pipeline_missing")

    facts: list[dict[str, Any]] = []
    calculations: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    allowed: set[str] = set()
    pages: set[int] = set()
    invalid_ordinal = 0
    metrics = confirmed.get("metrics", [])
    if not isinstance(metrics, list):
        raise ValueError("annual_review_v3_confirmed_facts_invalid")
    for record in metrics:
        item = _verified_fact_input_v3(record, identity, pages)
        if item is None:
            invalid_ordinal += 1
            pending.append({"review_id": f"pending_fact:unvalidated_{invalid_ordinal}", "kind": "fact", "status": "unverified", "reason": "confirmed_fact_not_independently_validated"})
            continue
        facts.append(item)
        allowed.add(item["fact_id"])

    verified_fact_ids = {item["fact_id"] for item in facts}
    analysis_records = confirmed.get("analyses", [])
    if not isinstance(analysis_records, list):
        raise ValueError("annual_review_v3_confirmed_calculations_invalid")
    for record in analysis_records:
        item = _verified_calculation_input_v3(record, verified_fact_ids)
        if item is None:
            invalid_ordinal += 1
            pending.append({"review_id": f"pending_calculation:unvalidated_{invalid_ordinal}", "kind": "calculation", "status": "unverified", "reason": "calculation_not_independently_recomputed"})
            continue
        calculations.append(item)
        allowed.add(item["calculation_id"])
    verified_calculation_ids = {item["calculation_id"] for item in calculations}

    verified_claims, interpretations = [], []
    claim_evidence_pool: dict[str, dict[str, Any]] = {}
    claim_verification_pool: dict[str, Any] = {}
    independent_verification_pool: dict[str, Any] = {}
    raw_verified_claims = report.get("verified_claims", [])
    raw_interpretations = report.get("interpretations", [])
    if not isinstance(raw_verified_claims, list) or not isinstance(raw_interpretations, list):
        raise ValueError("annual_review_v3_claims_invalid")
    for raw_claim in raw_verified_claims:
        claim = _v3_claim_input(
            raw_claim, verified_fact_ids, verified_calculation_ids, identity, pages,
            require_verified=True, evidence_pool=claim_evidence_pool,
            verification_pool=claim_verification_pool,
            independent_verification_pool=independent_verification_pool,
        )
        if claim is None:
            pending.append(_v3_pending_item("claim", raw_claim, "verified_claim_failed_projection"))
            continue
        verified_claims.append(claim)
        allowed.add(claim["claim_id"])
        allowed.update(claim["supporting_evidence_ids"])
    for raw_claim in raw_interpretations:
        claim = _v3_claim_input(
            raw_claim, verified_fact_ids, verified_calculation_ids, identity, pages,
            require_verified=False, evidence_pool=claim_evidence_pool,
            verification_pool=claim_verification_pool,
            independent_verification_pool=independent_verification_pool,
        )
        if claim is None:
            raise ValueError("annual_review_v3_interpretation_invalid")
        interpretations.append(claim)
        allowed.add(claim["claim_id"])
        allowed.update(claim["supporting_evidence_ids"])

    pending_section = report.get("pending_review", {})
    if not isinstance(pending_section, Mapping):
        raise ValueError("annual_review_v3_pending_section_invalid")
    for key, kind in (("facts", "fact"), ("calculations", "calculation"), ("claims", "claim")):
        records = pending_section.get(key, [])
        if not isinstance(records, list):
            raise ValueError("annual_review_v3_pending_records_invalid")
        for record in records:
            item = _v3_pending_item(kind, record, "pending_record_identity_invalid")
            pending.append(item)
            allowed.add(item["review_id"])

    signals_raw = pending_section.get("candidate_signals", [])
    if not isinstance(signals_raw, list):
        raise ValueError("annual_review_v3_candidate_signals_invalid")
    signals: list[dict[str, Any]] = []
    for record in signals_raw:
        if not isinstance(record, Mapping) or not isinstance(record.get("signal_id"), str) or _ID_RE.fullmatch(record["signal_id"]) is None:
            invalid_ordinal += 1
            pending.append({"review_id": f"pending_signal:invalid_{invalid_ordinal}", "kind": "signal", "status": "unverified", "reason": "candidate_signal_identity_invalid"})
            continue
        source_calculation_ids = record.get("calculation_ids", [])
        calculation_ids = [value for value in source_calculation_ids if isinstance(value, str) and value in verified_calculation_ids] if isinstance(source_calculation_ids, list) else []
        item = {
            "signal_id": record["signal_id"],
            "status": record.get("status") if record.get("status") in {"candidate", "abstained", "unknown", "not_triggered"} else "unknown",
            "summary": _v3_full_optional_text(record.get("summary")),
            "description": _v3_full_optional_text(record.get("description")),
            "reason": _v3_full_optional_text(record.get("reason")),
            "calculation_ids": calculation_ids,
            "evidence_ids": _v3_string_list(record.get("evidence_ids", []), "annual_review_v3_signal_evidence_invalid"),
            "limitations": _v3_string_list(record.get("limitations", []), "annual_review_v3_signal_limitations_invalid"),
            "placement": record.get("placement"),
            "placement_reasons": _v3_string_list(record.get("placement_reasons", []), "annual_review_v3_signal_placement_invalid"),
        }
        signals.append(item)
        allowed.update([item["signal_id"], *item["evidence_ids"]])

    raw_gaps = report.get("scope", {}).get("gaps", []) if isinstance(report.get("scope"), Mapping) else []
    if not isinstance(raw_gaps, list) or any(not isinstance(item, str) for item in raw_gaps):
        raw_gaps = ["report_scope_gap_record_invalid"]
    report_gaps = list(dict.fromkeys(item for item in raw_gaps if item))
    closure = pipeline["global_closure"]
    issue_ids: set[str] = set()
    finding_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for issue in pipeline["issues"]:
        issue_ids.add(issue["issue_id"])
        evidence_ids.update(issue.get("evidence_ids", []))
    for candidate in pipeline["candidate_pool"]:
        finding_ids.add(candidate["finding_id"])
        evidence_ids.update(candidate.get("evidence_ids", []))
        for citation in candidate.get("citations", []):
            if isinstance(citation, Mapping) and type(citation.get("pdf_page")) is int:
                pages.add(citation["pdf_page"])
    for supplement in pipeline["supplement_pool"]:
        evidence_ids.add(supplement["evidence_id"])
        if type(supplement.get("pdf_page")) is int:
            pages.add(supplement["pdf_page"])
    allowed.update(issue_ids | finding_ids | evidence_ids)
    investigation_identity_matches = _identity_matches(investigation, identity)
    raw_items = investigation.get("items", []) if investigation_identity_matches else []
    if not isinstance(raw_items, list):
        raise ValueError("annual_review_v3_investigation_items_invalid")
    raw_narrative = investigation.get("narrative_evidences", []) if investigation_identity_matches else []
    if not isinstance(raw_narrative, list):
        raise ValueError("annual_review_v3_narrative_evidences_invalid")
    narrative_by_id: dict[str, dict[str, Any]] = {}
    for raw_evidence in raw_narrative:
        if not isinstance(raw_evidence, Mapping):
            raise ValueError("annual_review_v3_narrative_evidence_invalid")
        evidence_id = raw_evidence.get("evidence_id")
        bbox = raw_evidence.get("bbox")
        signal_ids = _v3_string_list(raw_evidence.get("signal_ids", []), "annual_review_v3_narrative_evidence_invalid")
        raw_text = raw_evidence.get("raw_text")
        if (
            not isinstance(evidence_id, str) or _ID_RE.fullmatch(evidence_id) is None or evidence_id in narrative_by_id
            or raw_evidence.get("source_document_id") != identity["source_document_id"]
            or not isinstance(raw_evidence.get("source_sha256"), str)
            or raw_evidence["source_sha256"].lower() != identity["source_sha256"]
            or type(raw_evidence.get("pdf_page")) is not int or raw_evidence["pdf_page"] < 1
            or not isinstance(bbox, Mapping) or set(bbox) != {"x0", "y0", "x1", "y1"}
            or any(type(bbox[key]) not in {int, float} or not math.isfinite(float(bbox[key])) for key in bbox)
            or bbox["x1"] < bbox["x0"] or bbox["y1"] < bbox["y0"]
            or not signal_ids or any(_ID_RE.fullmatch(item) is None for item in signal_ids)
            or not isinstance(raw_evidence.get("section_label"), str) or not raw_evidence["section_label"].strip()
            or not isinstance(raw_evidence.get("selection_reason"), str) or not raw_evidence["selection_reason"].strip()
            or not isinstance(raw_text, str) or not raw_text.strip()
        ):
            raise ValueError("annual_review_v3_narrative_evidence_invalid")
        narrative_by_id[evidence_id] = {
            "evidence_id": evidence_id,
            "source_document_id": identity["source_document_id"],
            "source_sha256": identity["source_sha256"],
            "signal_ids": signal_ids,
            "section_label": raw_evidence["section_label"],
            "pdf_page": raw_evidence["pdf_page"],
            "bbox": {key: bbox[key] for key in ("x0", "y0", "x1", "y1")},
            "raw_text": raw_text,
            "selection_reason": raw_evidence["selection_reason"],
        }
        evidence_ids.add(evidence_id)
        pages.add(raw_evidence["pdf_page"])
    narrative_evidences = sorted(narrative_by_id.values(), key=lambda item: item["evidence_id"])
    investigation_items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping) or not isinstance(raw_item.get("signal_id"), str) or _ID_RE.fullmatch(raw_item["signal_id"]) is None:
            raise ValueError("annual_review_v3_investigation_item_invalid")
        status = raw_item.get("status") if raw_item.get("status") in {"interpretation", "abstained", "unknown"} else "unknown"
        evidence_list = _v3_string_list(raw_item.get("narrative_evidence_ids", []), "annual_review_v3_investigation_evidence_invalid")
        if any(item not in narrative_by_id or raw_item["signal_id"] not in narrative_by_id[item]["signal_ids"] for item in evidence_list):
            raise ValueError("annual_review_v3_investigation_evidence_invalid")
        investigation_item = {
            "review_id": f"investigation:{raw_item['signal_id']}",
            "signal_id": raw_item["signal_id"],
            "status": status,
            "classification": "unverified_model_material",
            "explanation": _v3_full_optional_text(raw_item.get("explanation")),
            "alternative_explanations": _v3_string_list(raw_item.get("alternative_explanations", []), "annual_review_v3_investigation_alternatives_invalid"),
            "limitations": _v3_string_list(raw_item.get("limitations", []), "annual_review_v3_investigation_limitations_invalid"),
            "narrative_evidence_ids": evidence_list,
            "citations": [dict(narrative_by_id[item]) for item in evidence_list],
            "claim_type": raw_item.get("claim_type"),
            "verification_status": raw_item.get("verification_status"),
            "reason": _v3_full_optional_text(raw_item.get("reason")),
            "failure_category": raw_item.get("reason") if status in {"abstained", "unknown"} else None,
        }
        investigation_items.append(investigation_item)
        allowed.update([investigation_item["review_id"], raw_item["signal_id"], *evidence_list])

    raw_discovery = investigation.get("discovery")
    discovery_identity_matches = (
        isinstance(raw_discovery, Mapping)
        and raw_discovery.get("kind") == "fintrace_annual_discovery"
        and _identity_matches(raw_discovery, identity)
    )
    discovery_coverage = raw_discovery.get("coverage") if discovery_identity_matches and isinstance(raw_discovery.get("coverage"), Mapping) else {}
    coverage_fields = (
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches", "processed_batches",
        "successful_batches", "partial_batches", "failed_batches", "unattempted_batches", "processed_chars",
        "unattempted_chars", "planned_chars", "omitted_chars", "unread_pages", "failed_pages",
        "no_text_pages", "image_pages", "unread_image_pages", "truncated", "complete", "source_status",
        "content_status", "time_budget_exhausted", "text_only_scope",
    )
    raw_batch_results = discovery_coverage.get("batch_results", []) if isinstance(discovery_coverage, Mapping) else []
    if not isinstance(raw_batch_results, list):
        raise ValueError("annual_review_v3_batch_coverage_invalid")
    stable_batch_fields = (
        "batch_index", "status", "reason", "pages", "raw_chars",
        "accepted_findings", "rejected_findings", "restored_quotes",
    )
    stable_batch_results: list[dict[str, Any]] = []
    for row in raw_batch_results:
        if not isinstance(row, Mapping):
            raise ValueError("annual_review_v3_batch_coverage_invalid")
        stable_batch_results.append({key: row[key] for key in stable_batch_fields if key in row})
    discovery_scope = {
        "status": raw_discovery.get("status") if discovery_identity_matches else "identity_mismatch",
        "identity_matches": bool(discovery_identity_matches),
        "coverage": {key: discovery_coverage[key] for key in coverage_fields if key in discovery_coverage},
        # These rows describe scanned source coverage and finding acceptance.
        # Execution attempt counts, audit paths, and reused-vs-new resolutions
        # stay in the discovery report and do not invalidate semantic review reuse.
        "batch_results": stable_batch_results,
        "finding_ids": sorted(candidate["finding_id"] for candidate in pipeline["candidate_pool"]),
    }
    investigation_status = investigation.get("status") if investigation_identity_matches else "identity_mismatch"
    comparability = report.get("comparability")
    if comparability is not None and not isinstance(comparability, Mapping):
        raise ValueError("annual_review_v3_comparability_invalid")
    comparability_fields = (
        "verification_id", "document_id", "source_sha256", "report_year", "current_year", "comparative_year",
        "status", "restatement_status", "evidence", "checks", "conflicts", "limitations",
    )
    comparability_input = None if comparability is None else {key: comparability[key] for key in comparability_fields if key in comparability}
    comparability_matches = comparability is None or (
        isinstance(comparability, Mapping)
        and comparability.get("document_id") == identity["source_document_id"]
        and isinstance(comparability.get("source_sha256"), str)
        and comparability["source_sha256"].lower() == identity["source_sha256"]
        and comparability.get("report_year") == identity["report_year"]
        and comparability.get("status") in {"verified", "conflict", "insufficient_evidence"}
    )
    needs_comparability = any(item.get("formula_id") == "annual_difference" for item in calculations)
    comparability_allows_priority = comparability_matches and (
        comparability.get("status") == "verified" if needs_comparability and isinstance(comparability, Mapping)
        else comparability is None or isinstance(comparability, Mapping) and comparability.get("status") == "verified"
    )
    scope_gaps = list(report_gaps)
    scope_gaps.extend(item for item in closure.get("coverage_gaps", []) if isinstance(item, str))
    if not discovery_identity_matches:
        scope_gaps.append("discovery_source_identity_unavailable")
    if isinstance(discovery_coverage, Mapping):
        scope_gaps.extend(item for item in discovery_coverage.get("unread_pages", []) if isinstance(item, str))
        if discovery_coverage.get("truncated") is True or discovery_coverage.get("omitted_chars", 0):
            scope_gaps.append("discovery_text_scope_incomplete")
    if not investigation_identity_matches:
        scope_gaps.append("investigation_source_identity_unavailable")
    if investigation_status not in {"completed", "abstained"}:
        scope_gaps.append(f"investigation_status:{investigation_status}")
    if not comparability_matches or needs_comparability and not comparability_allows_priority:
        scope_gaps.append("comparability_unverified_or_mismatched")
    if pipeline["retrieval_coverage"].get("complete") is not True:
        scope_gaps.append("supplement_retrieval_scope_incomplete")
    scope_gaps = list(dict.fromkeys(scope_gaps))
    limitations_required = _required_review_limitations(
        pipeline_raw, scope_gaps,
        investigation_status=investigation_status,
        investigation_items_complete=all(item.get("status") == "interpretation" for item in investigation_items),
        comparability_allows_priority=comparability_allows_priority,
        financial_pending=bool(pending),
    )
    for fact in facts:
        pages.update(fact.get("pdf_pages", []))

    review_input = {
        "schema_version": "annual_model_review_v3",
        "source_identity": {key: identity[key] for key in ("company_id", "report_year", "source_document_id", "source_sha256")},
        "coverage": {
            "verified_fact_count": len(facts),
            "verified_calculation_count": len(calculations),
            "verified_claim_count": len(verified_claims),
            "interpretation_count": len(interpretations),
            "unresolved_object_count": len(pending),
            "candidate_signal_count": len(signals),
            "investigation_item_count": len(investigation_items),
            "discovery_status": discovery_scope["status"],
            "discovery_coverage_complete": pipeline["input"]["discovery_coverage_complete"],
            "issue_count": len(pipeline["issues"]),
            "candidate_count": len(pipeline["candidate_pool"]),
            "member_decision_count": len(pipeline["member_paths"]),
            "issue_global_closed": closure["closed"],
            "investigation_identity_matches": investigation_identity_matches,
            "comparability_status": comparability.get("status") if isinstance(comparability, Mapping) else None,
            "comparability_identity_matches": comparability_matches,
            "scope_gaps": scope_gaps,
            "limitations_required": limitations_required,
            "no_priority_eligible": False,
        },
        "comparability": comparability_input,
        "verified_facts": facts,
        "verified_calculations": calculations,
        "verified_claims": verified_claims,
        "interpretations": interpretations,
        "claim_evidence_pool": [claim_evidence_pool[key] for key in sorted(claim_evidence_pool)],
        "claim_verification_pool": [
            {"reference_id": key, "record": claim_verification_pool[key]}
            for key in sorted(claim_verification_pool)
        ],
        "independent_verification_pool": [
            {"reference_id": key, "record": independent_verification_pool[key]}
            for key in sorted(independent_verification_pool)
        ],
        "candidate_signals": signals,
        "unresolved_review_items": pending,
        "investigation_items": investigation_items,
        "narrative_evidences": narrative_evidences,
        "discovery_scope": discovery_scope,
        "issue_hierarchy": pipeline,
        "scope_gaps": scope_gaps,
        "limitations_required": limitations_required,
    }
    supplement_rows = pipeline["supplement_member_paths"]
    all_issues_accounted = all(item.get("status") in {"accepted", "excluded"} for item in pipeline["issues"])
    no_priority_allowed = bool(
        not limitations_required
        and discovery_scope["identity_matches"] is True
        and discovery_scope["status"] == "completed"
        and pipeline["input"]["discovery_coverage_complete"]
        and closure["closed"] and not closure["pending_issue_ids"] and not closure["relation_gaps"]
        and not closure["coverage_gaps"] and all_issues_accounted
        and not any(item.get("status") == "accepted" for item in pipeline["issues"])
        and all(item["disposition"] != "pending" for item in pipeline["member_paths"])
        and all(item["role_status"] == "accepted" for item in supplement_rows)
        and not pending and not interpretations and not investigation_items
        and all(item["status"] in {"not_triggered"} for item in signals)
        and investigation_identity_matches and (
            investigation_status == "completed"
            or (investigation_status == "abstained" and investigation.get("reason") == "no_supported_candidate_signal" and not investigation_items)
        )
        and comparability_matches and comparability_allows_priority
        and bool(facts) and not scope_gaps
    )
    review_input["coverage"]["no_priority_eligible"] = no_priority_allowed
    request = build_annual_review_v3_request(review_input, model=request_model, system_prompt=system_prompt)
    if enforce_budget and request["request_chars"] > max_request_chars:
        raise ValueError("annual_review_v3_request_budget_exceeded")
    refs = [EvidenceRef(str(identity["source_document_id"]), page) for page in sorted(pages)]
    return review_input, allowed, refs, no_priority_allowed


def _v3_claim_input(
    raw: object,
    verified_fact_ids: set[str],
    verified_calculation_ids: set[str],
    identity: Mapping[str, Any],
    pages: set[int],
    *,
    require_verified: bool,
    evidence_pool: dict[str, dict[str, Any]],
    verification_pool: dict[str, Any],
    independent_verification_pool: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    claim_id = raw.get("claim_id")
    claim_type = raw.get("claim_type")
    placement = raw.get("placement")
    independent = raw.get("independent_verification")
    if (
        not isinstance(claim_id, str) or _ID_RE.fullmatch(claim_id) is None
        or claim_type not in {"fact", "calculation", "inference", "hypothesis"}
        or not isinstance(raw.get("text"), str) or not raw["text"].strip()
        or not isinstance(independent, Mapping)
    ):
        return None
    status = independent.get("status")
    if require_verified:
        if placement != "verified_claims" or raw.get("verification_status") != "verified" or status != "verified":
            return None
    elif placement != "interpretations" or claim_type not in {"inference", "hypothesis"} or status not in {"interpretation", "insufficient_evidence"}:
        return None
    supporting_facts = _v3_string_list(raw.get("supporting_fact_ids", []), "annual_review_v3_claim_fact_refs_invalid")
    supporting_calculations = _v3_string_list(raw.get("calculation_ids", []), "annual_review_v3_claim_calculation_refs_invalid")
    supporting_evidence = _v3_string_list(raw.get("supporting_evidence_ids", []), "annual_review_v3_claim_evidence_refs_invalid")
    if any(item not in verified_fact_ids for item in supporting_facts) or any(item not in verified_calculation_ids for item in supporting_calculations):
        return None
    evidences = raw.get("evidences", [])
    if not isinstance(evidences, list):
        return None
    evidence_refs: list[str] = []
    found_evidence_ids: set[str] = set()
    for evidence in evidences:
        if not isinstance(evidence, Mapping):
            return None
        evidence_id = evidence.get("evidence_id")
        document_id = evidence.get("document_id", evidence.get("source_document_id"))
        source_sha = evidence.get("source_sha256")
        page = evidence.get("pdf_page", evidence.get("page_number", evidence.get("page")))
        if (
            not isinstance(evidence_id, str) or _ID_RE.fullmatch(evidence_id) is None
            or document_id != identity["source_document_id"]
            or not isinstance(source_sha, str) or source_sha.lower() != identity["source_sha256"]
            or type(page) is not int or page < 1
        ):
            return None
        found_evidence_ids.add(evidence_id)
        pages.add(page)
        projected = _v3_json_value(evidence)
        prior = evidence_pool.get(evidence_id)
        if prior is not None and prior != projected:
            return None
        evidence_pool[evidence_id] = projected
        evidence_refs.append(evidence_id)
    if set(supporting_evidence) - found_evidence_ids:
        return None
    verifications = raw.get("verifications", [])
    if not isinstance(verifications, list) or any(not isinstance(item, Mapping) for item in verifications):
        return None
    if require_verified and not verifications:
        return None
    if require_verified and any(item.get("status") != "verified" or item.get("target_type") != "claim" or item.get("target_id") != claim_id for item in verifications):
        return None
    if require_verified and set(supporting_evidence) != found_evidence_ids:
        return None
    reasons = _v3_string_list(raw.get("placement_reasons", []), "annual_review_v3_claim_reasons_invalid")
    verification_refs = [
        _v3_pool_record("claim-verification", item, verification_pool)
        for item in verifications
    ]
    independent_ref = _v3_pool_record(
        "independent-verification", independent, independent_verification_pool,
    )
    return {
        "claim_id": claim_id,
        "claim_type": claim_type,
        "text": raw["text"],
        "placement": placement,
        "verification_status": raw.get("verification_status"),
        "independent_verification_ref": independent_ref,
        "supporting_fact_ids": supporting_facts,
        "calculation_ids": supporting_calculations,
        "supporting_evidence_ids": supporting_evidence,
        "evidence_refs": evidence_refs,
        "verification_refs": verification_refs,
        "placement_reasons": reasons,
        "limitations": _v3_string_list(raw.get("limitations", []), "annual_review_v3_claim_limitations_invalid"),
        "alternative_explanations": _v3_string_list(raw.get("alternative_explanations", []), "annual_review_v3_claim_alternatives_invalid"),
        "follow_up_items": _v3_string_list(raw.get("follow_up_items", []), "annual_review_v3_claim_followups_invalid"),
    }


def _v3_pool_record(prefix: str, value: Any, pool: dict[str, Any]) -> str:
    projected = _v3_json_value(value)
    canonical = json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    reference_id = f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"
    prior = pool.get(reference_id)
    if prior is not None and prior != projected:
        raise ValueError("annual_review_v3_claim_pool_collision")
    pool[reference_id] = projected
    return reference_id


def _v3_pending_item(kind: str, raw: object, default_reason: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("annual_review_v3_pending_record_invalid")
    id_key = {"fact": "fact_id", "calculation": "calculation_id", "claim": "claim_id"}[kind]
    raw_id = raw.get(id_key)
    if not isinstance(raw_id, str) or _ID_RE.fullmatch(raw_id) is None:
        raise ValueError("annual_review_v3_pending_record_invalid")
    reasons = _v3_string_list(raw.get("placement_reasons", []), "annual_review_v3_pending_reason_invalid")
    return {
        "review_id": f"pending_{kind}:{raw_id}",
        "kind": kind,
        "status": "unverified",
        "reason": reasons[0] if reasons else default_reason,
        "record": _v3_json_value(raw),
    }


def _v3_full_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise ValueError("annual_review_v3_text_invalid")
    return value if value.strip() else None


def _v3_string_list(value: object, error_code: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(error_code)
    if any(any(ord(char) < 32 and char not in "\t\n\r" for char in item) for item in value):
        raise ValueError(error_code)
    return list(value)


def _v3_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("annual_review_v3_json_number_invalid")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("annual_review_v3_json_object_invalid")
        return {key: _v3_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_v3_json_value(item) for item in value]
    raise ValueError("annual_review_v3_json_value_invalid")


def _required_review_limitations(
    pipeline: Any, report_gaps: list[str] | None = None, *,
    investigation_status: str | None = None,
    investigation_items_complete: bool | None = None,
    comparability_allows_priority: bool | None = None,
    financial_pending: bool = False,
) -> list[str]:
    if not isinstance(pipeline, Mapping):
        return ["issue_pipeline_unavailable"]
    result: list[str] = []
    input_value = pipeline.get("input", {})
    closure = pipeline.get("global_closure", {})
    retrieval = pipeline.get("retrieval_coverage", {})
    if not isinstance(input_value, Mapping) or input_value.get("discovery_status") != "completed" or input_value.get("discovery_coverage_complete") is not True:
        result.append("discovery_coverage_incomplete")
    if not isinstance(closure, Mapping) or closure.get("closed") is not True or closure.get("pending_finding_ids"):
        result.append("issue_path_pending")
    if isinstance(closure, Mapping) and (closure.get("pending_issue_ids") or closure.get("relation_gaps")):
        result.append("global_relations_unclosed")
    if isinstance(closure, Mapping) and closure.get("coverage_gaps") or report_gaps:
        result.append("scope_gaps_present")
    supplements = pipeline.get("supplements", [])
    if isinstance(retrieval, Mapping) and retrieval.get("complete") is not True:
        result.append("supplement_retrieval_incomplete")
    if isinstance(supplements, list) and any(not isinstance(item, Mapping) or item.get("role_status") != "accepted" for item in supplements):
        result.append("supplement_roles_pending")
    if investigation_status not in {None, "completed"} or investigation_items_complete is False:
        result.append("fixed_investigation_incomplete")
    if financial_pending or comparability_allows_priority is False:
        result.append("financial_or_comparability_pending")
    return list(dict.fromkeys(result))


def _actual_request_chars(model: str, system_prompt: str, user_content: str) -> int:
    """Measure the exact UTF-8 JSON string shape used by complete_chat."""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    return len(json.dumps(payload, ensure_ascii=False))


def _review_pipeline_semantics(value: Any) -> dict[str, Any] | None:
    """Build a lossless shared-pool projection of all issue-stage business paths."""

    if not isinstance(value, Mapping) or value.get("kind") != "annual_issue_pipeline_v1":
        return None
    required = ("status", "reason", "source_identity", "input", "candidates", "issues", "member_decisions", "supplements", "retrieval_coverage", "shards", "global_closure")
    if any(key not in value for key in required):
        return None
    source = value.get("source_identity")
    if not isinstance(source, Mapping):
        return None
    # A child run may rebind only run_id. No issue, citation, quote, member,
    # role, coverage or gap field is removed from the semantic review input.
    stable_source = {key: item for key, item in source.items() if key != "run_id"}
    candidates = value.get("candidates")
    issues = value.get("issues")
    decisions = value.get("member_decisions")
    supplements = value.get("supplements")
    shards = value.get("shards")
    input_value = value.get("input")
    if not all(isinstance(item, list) for item in (candidates, issues, decisions, supplements, shards)) or not isinstance(input_value, Mapping):
        return None

    candidate_ids = {item.get("finding_id") for item in candidates if isinstance(item, Mapping)}
    if len(candidate_ids) != len(candidates) or any(not isinstance(item, str) for item in candidate_ids):
        return None
    # Candidate records are the only full copy of discovery prose and primary
    # quote/location data. Member paths retain both the accepted grouping
    # disposition and the final disposition after any global reconciliation.
    grouping_paths = []
    global_shard: Mapping[str, Any] | None = None
    for shard in shards:
        if not isinstance(shard, Mapping):
            return None
        if shard.get("kind") == "grouping":
            shard_input = shard.get("input")
            if not isinstance(shard_input, Mapping):
                return None
            if shard.get("status") == "accepted" and isinstance(shard.get("result"), Mapping):
                stage_decisions = shard["result"].get("decisions")
                if not isinstance(stage_decisions, list):
                    return None
            elif shard.get("status") == "pending":
                stage_decisions = []
            else:
                return None
            grouping_paths.append({
                "input_member_finding_ids": list(shard_input.get("input_member_finding_ids", [])),
                "status": shard["status"],
                "reason": shard.get("reason") if shard["status"] == "pending" else None,
                "decisions": [dict(row) for row in stage_decisions],
            })
        elif shard.get("kind") == "global":
            if global_shard is not None:
                return None
            global_shard = shard
    grouping_paths.sort(key=lambda item: tuple(item["input_member_finding_ids"]))

    global_path: dict[str, Any]
    if global_shard is None:
        global_path = {"status": "not_required_or_not_started", "reason": None, "input_issue_ids": [], "issue_members": [], "decisions": []}
    else:
        shard_input = global_shard.get("input")
        if not isinstance(shard_input, Mapping):
            return None
        result = global_shard.get("result")
        if global_shard.get("status") == "accepted" and isinstance(result, Mapping):
            global_decisions = result.get("decisions")
            if not isinstance(global_decisions, list):
                return None
        elif global_shard.get("status") == "pending":
            global_decisions = []
        else:
            return None
        global_path = {
            "status": global_shard["status"],
            "reason": global_shard.get("reason") if global_shard["status"] == "pending" else None,
            "input_issue_ids": list(shard_input.get("input_issue_ids", [])),
            "issue_members": [dict(row) for row in shard_input.get("issue_members", []) if isinstance(row, Mapping)],
            "decisions": [dict(row) for row in global_decisions],
        }

    compact_issues = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            return None
        compact_issues.append({
            key: issue[key] for key in (
                "issue_id", "status", "priority", "member_finding_ids", "evidence_ids",
                "member_sources", "verification_status", "independent_verification",
                "review_order_reason", "global_relation", "global_merged_issue_ids",
            ) if key in issue
        })
    compact_issues.sort(key=lambda item: item["issue_id"])
    compact_decisions = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            return None
        compact_decisions.append(dict(decision))
    compact_decisions.sort(key=lambda item: item.get("finding_id", ""))

    supplement_pool = []
    supplement_member_paths = []
    supplement_shard: Mapping[str, Any] | None = None
    for shard in shards:
        if isinstance(shard, Mapping) and shard.get("kind") == "supplement":
            supplement_shard = shard
            break
    requested_supplement_pairs: set[tuple[str, str]] = set()
    supplement_assignments: list[dict[str, Any]] = []
    if supplement_shard is not None and isinstance(supplement_shard.get("input"), Mapping):
        raw_assignments = supplement_shard["input"].get("evidence_assignments", [])
        if not isinstance(raw_assignments, list) or any(
            not isinstance(row, Mapping) or set(row) != {"issue_id", "evidence_id", "role"}
            for row in raw_assignments
        ):
            return None
        supplement_assignments = [dict(row) for row in raw_assignments]
        requested_supplement_pairs = {
            (row["issue_id"], row["evidence_id"]) for row in supplement_assignments
        }
    supplement_output_roles: dict[tuple[str, str], dict[str, Any]] = {}
    if supplement_shard is not None and supplement_shard.get("status") == "accepted":
        result = supplement_shard.get("result")
        role_rows = result.get("roles") if isinstance(result, Mapping) else None
        if not isinstance(role_rows, list):
            return None
        supplement_output_roles = {
            (row["issue_id"], row["evidence_id"]): dict(row)
            for row in role_rows if isinstance(row, Mapping)
        }
    for supplement in supplements:
        if not isinstance(supplement, Mapping):
            return None
        supplement_pool.append({
            key: supplement[key] for key in (
                "evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index",
                "start_char", "end_char", "bbox", "raw_text", "section", "selection_reason",
                "verification_status", "independent_verification", "retrieval_is_verification",
            ) if key in supplement
        })
        roles = {
            row.get("issue_id"): row for row in supplement.get("issue_roles", [])
            if isinstance(row, Mapping) and isinstance(row.get("issue_id"), str)
        }
        for issue_id in supplement.get("issue_ids", []):
            role = roles.get(issue_id)
            supplement_member_paths.append({
                "issue_id": issue_id,
                "evidence_id": supplement.get("evidence_id"),
                "requested": (issue_id, supplement.get("evidence_id")) in requested_supplement_pairs,
                "status": "accepted" if role is not None else "pending",
                "role_status": supplement.get("role_status"),
                "role": role.get("role") if role is not None else None,
                "reason_code": role.get("reason_code") if role is not None else None,
            })
    supplement_pool.sort(key=lambda item: item.get("evidence_id", ""))
    supplement_member_paths.sort(key=lambda item: (str(item.get("issue_id")), str(item.get("evidence_id"))))

    closure = dict(value["global_closure"])
    compact_closure = {
        key: closure[key] for key in (
            "closed", "closure_method", "scope", "input_issue_ids", "pending_issue_ids",
            "relation_gaps", "coverage_gaps", "reason", "discovery_coverage_complete",
            "no_priority_allowed",
        ) if key in closure
    }
    if supplement_shard is None:
        supplement_path = {
            "status": "not_started" if value.get("retrieval_coverage", {}).get("scope") == "not_started" else "no_assignments",
            "reason": value.get("retrieval_coverage", {}).get("reason"),
            "assignments": [],
            "roles": [],
        }
    else:
        supplement_path = {
            "status": supplement_shard.get("status"),
            "reason": supplement_shard.get("reason") if supplement_shard.get("status") == "pending" else None,
            "assignments": supplement_assignments,
            "roles": [
                supplement_output_roles[key] for key in sorted(supplement_output_roles)
            ],
        }
    compact_input = {key: item for key, item in input_value.items() if key != "finding_ids"}
    return {
        "projection_version": "annual_issue_review_shared_pool_v1",
        "kind": "annual_issue_pipeline_v1",
        "status": value["status"],
        "reason": value["reason"],
        "source_identity": stable_source,
        "input": compact_input,
        "candidate_pool": candidates,
        "grouping_paths": grouping_paths,
        "global_path": global_path,
        "issues": compact_issues,
        "member_paths": compact_decisions,
        "supplement_pool": supplement_pool,
        "supplement_member_paths": supplement_member_paths,
        "supplement_path": supplement_path,
        "retrieval_coverage": value["retrieval_coverage"],
        "global_closure": compact_closure,
    }


def _verified_fact_input(
    record: object,
    identity: Mapping[str, Any],
    evidence_pages: set[int],
) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    fact_id = record.get("fact_id")
    verifications = record.get("verifications")
    source_sha = record.get("source_sha256")
    if (
        not isinstance(fact_id, str)
        or _ID_RE.fullmatch(fact_id) is None
        or not isinstance(verifications, list)
        or record.get("company_id") != identity["company_id"]
        or record.get("source_document_id") != identity["source_document_id"]
        or not isinstance(source_sha, str)
        or source_sha.lower() != identity["source_sha256"]
        or record.get("report_year") not in {None, identity["report_year"]}
    ):
        return None
    if not verifications or any(
        not isinstance(item, Mapping)
        or item.get("status") != "verified"
        or item.get("target_type") != "financial_fact"
        or item.get("target_id") != fact_id
        or not isinstance(item.get("evidence_ids"), list)
        or not item["evidence_ids"]
        for item in verifications
    ):
        return None
    verified_evidence_ids = {
        evidence_id
        for item in verifications if isinstance(item, Mapping)
        for evidence_id in item["evidence_ids"] if isinstance(evidence_id, str)
    }
    verified_pages = _matching_pages(record, identity, verified_evidence_ids)
    if len(verified_evidence_ids) == 0 or not verified_pages:
        return None
    value = record.get("normalized_value")
    if not isinstance(value, str):
        return None
    evidence_pages.update(verified_pages)
    return {
        "fact_id": fact_id,
        "metric_id": _plain_text(record.get("metric_id"), 100),
        "label": _plain_text(record.get("label_raw"), 140),
        "value": value[:80],
        "unit": _plain_text(record.get("unit"), 80),
        "currency": _plain_text(record.get("currency"), 80),
        "period_start": _plain_text(record.get("period_start"), 20),
        "period_end": _plain_text(record.get("period_end"), 20),
        "period_type": _plain_text(record.get("period_type"), 60),
        "comparison_role": _plain_text(record.get("comparison_role"), 60),
        "statement_type": _plain_text(record.get("statement_type"), 80),
        "scope": _plain_text(record.get("scope"), 80),
        "pdf_pages": verified_pages,
    }


def _verified_calculation_input(record: object, verified_fact_ids: set[str]) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    calculation_id = record.get("calculation_id")
    input_ids = record.get("input_fact_ids")
    output_value = record.get("output_value")
    independent = record.get("independent_verification")
    if (
        not isinstance(calculation_id, str)
        or _ID_RE.fullmatch(calculation_id) is None
        or not isinstance(input_ids, list)
        or any(not isinstance(item, str) or item not in verified_fact_ids for item in input_ids)
        or record.get("status") != "succeeded"
        or not isinstance(output_value, str)
        or not isinstance(independent, Mapping)
        or independent.get("status") != "verified"
        or independent.get("calculation_id") != calculation_id
        or independent.get("recomputed_value") != output_value
    ):
        return None
    return {
        "calculation_id": calculation_id,
        "formula_id": _plain_text(record.get("formula_id"), 100),
        "formula": _plain_text(record.get("formula_expression"), 180),
        "input_fact_ids": input_ids[:20],
        "value": output_value[:80],
        "unit": _plain_text(record.get("unit"), 80),
    }


def _verified_fact_input_v3(
    record: object, identity: Mapping[str, Any], evidence_pages: set[int],
) -> dict[str, Any] | None:
    """Use the v2 verifier, then restore every business string without clipping."""

    base = _verified_fact_input(record, identity, evidence_pages)
    if base is None or not isinstance(record, Mapping):
        return None
    fields = {
        "metric_id": "metric_id", "label": "label_raw", "unit": "unit",
        "currency": "currency", "period_start": "period_start", "period_end": "period_end",
        "period_type": "period_type", "comparison_role": "comparison_role",
        "statement_type": "statement_type", "scope": "scope",
    }
    for output_key, source_key in fields.items():
        value = record.get(source_key)
        if value is not None and not isinstance(value, str):
            return None
        if isinstance(value, str) and any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            return None
        base[output_key] = value
    normalized_value = record.get("normalized_value")
    if not isinstance(normalized_value, str) or not normalized_value:
        return None
    base["value"] = normalized_value
    return base


def _verified_calculation_input_v3(
    record: object, verified_fact_ids: set[str],
) -> dict[str, Any] | None:
    """Retain all calculation text and fact links; v3 never inherits v2 slices."""

    base = _verified_calculation_input(record, verified_fact_ids)
    if base is None or not isinstance(record, Mapping):
        return None
    for output_key, source_key in (("formula_id", "formula_id"), ("formula", "formula_expression"), ("unit", "unit")):
        value = record.get(source_key)
        if value is not None and not isinstance(value, str):
            return None
        if isinstance(value, str) and any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            return None
        base[output_key] = value
    input_ids = record.get("input_fact_ids")
    output_value = record.get("output_value")
    if not isinstance(input_ids, list) or any(not isinstance(item, str) for item in input_ids):
        return None
    if not isinstance(output_value, str) or not output_value:
        return None
    base["input_fact_ids"] = list(input_ids)
    base["value"] = output_value
    return base


def _pending_item(kind: str, record: object) -> dict[str, str] | None:
    if not isinstance(record, Mapping):
        return None
    raw_id = record.get({"fact": "fact_id", "calculation": "calculation_id", "claim": "claim_id"}[kind])
    if not isinstance(raw_id, str) or _ID_RE.fullmatch(raw_id) is None:
        return None
    reasons = record.get("placement_reasons", [])
    return {
        "review_id": f"pending_{kind}:{raw_id}",
        "kind": kind,
        "status": "unverified",
        "reason": _plain_text(reasons[0], 180) if isinstance(reasons, list) and reasons else "对象未进入已核验区。",
    }


def _unvalidated_item(kind: str, record: object) -> dict[str, str]:
    raw_id = None
    if isinstance(record, Mapping):
        raw_id = record.get("fact_id" if kind == "fact" else "calculation_id")
    if not isinstance(raw_id, str) or _ID_RE.fullmatch(raw_id) is None:
        raw_id = f"unknown_{kind}"
    return {
        "review_id": f"pending_{kind}:{raw_id}",
        "kind": kind,
        "status": "unverified",
        "reason": "已确认区记录未通过评审输入结构检查。",
    }


def _matching_pages(
    record: Mapping[str, Any], identity: Mapping[str, Any], required_evidence_ids: set[str]
) -> list[int]:
    pages: set[int] = set()
    values = record.get("verification_evidences")
    if not isinstance(values, list):
        return []
    found_ids: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        evidence_id = item.get("evidence_id")
        if evidence_id not in required_evidence_ids:
            continue
        if item.get("document_id") != identity["source_document_id"]:
            continue
        source_sha = item.get("source_sha256")
        if not isinstance(source_sha, str) or source_sha.lower() != identity["source_sha256"]:
            continue
        found_ids.add(str(evidence_id))
        page = item.get("pdf_page", item.get("page_number", item.get("page")))
        if type(page) is int and 1 <= page <= 10_000:
            pages.add(page)
    if found_ids != required_evidence_ids:
        return []
    return sorted(pages)


def _identity_matches(investigation: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    return all(
        investigation.get(key) == identity[key]
        or (key == "source_sha256" and isinstance(investigation.get(key), str) and investigation[key].lower() == identity[key])
        for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256")
    )


def _report_identity(report: Mapping[str, Any]) -> dict[str, Any]:
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
        or not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256)
    ):
        raise ValueError("annual_review_source_identity_invalid")
    return {
        "run_id": run_id,
        "company_id": company_id,
        "report_year": report_year,
        "source_document_id": document_id,
        "source_sha256": source_sha256.lower(),
    }


def _validated_refs(value: object, allowed_ids: set[str], *, required: bool) -> list[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("model_review_response_invalid")
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed_ids or item in refs:
            raise ValueError("model_review_response_invalid")
        refs.append(item)
    if required and not refs:
        raise ValueError("model_review_response_invalid")
    return refs


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("model_review_response_invalid")
    return value.strip()


def _plain_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]


def _limited_optional_text(value: object, maximum: int, mark_truncated) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > maximum:
        mark_truncated()
    return value.strip()[:maximum]


def _text_list(value: object, maximum_count: int, maximum_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip()[:maximum_chars] for item in value[:maximum_count] if isinstance(item, str) and item.strip()]


def _load_prompt() -> str:
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("model_review_prompt_missing")
    return text


def _audit_artifact(path: Path, artifacts_root: Path, status: str) -> dict[str, Any]:
    relative = path.resolve().relative_to(artifacts_root.resolve()).as_posix()
    return {
        "audit_dir": relative,
        "request_path": f"{relative}/request.json" if (path / "request.json").is_file() else None,
        "response_path": f"{relative}/response.json" if (path / "response.json").is_file() else None,
        "failure_path": f"{relative}/failure.json" if (path / "failure.json").is_file() else None,
        "status": status,
        "prompt_version": PROMPT_VERSION,
    }


def _audit_artifact_v3(
    path: Path, artifacts_root: Path, status: str, *,
    messages: list[ChatMessage] | None = None, model: str | None = None,
    request_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    relative = path.resolve().relative_to(artifacts_root.resolve()).as_posix()
    request_path = path / "request.json"
    wire_sha = None
    try:
        record = json.loads(request_path.read_text(encoding="utf-8"))
        raw_messages = record.get("messages")
        raw_model = record.get("model")
        if isinstance(raw_messages, list) and isinstance(raw_model, str):
            wire_sha = __import__("hashlib").sha256(serialize_chat_body(raw_model, raw_messages)).hexdigest()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        if messages is not None and model is not None:
            wire_sha = __import__("hashlib").sha256(serialize_chat_body(model, messages)).hexdigest()
    if wire_sha is None and isinstance(request_binding, Mapping):
        wire_sha = request_binding.get("request_body_sha256")
    result = {
        "audit_dir": relative,
        "request_path": f"{relative}/request.json" if request_path.is_file() else None,
        "response_path": f"{relative}/response.json" if (path / "response.json").is_file() else None,
        "failure_path": f"{relative}/failure.json" if (path / "failure.json").is_file() else None,
        "status": status,
        "prompt_version": V3_PROMPT_VERSION,
    }
    if wire_sha is not None:
        result["request_body_sha256"] = wire_sha
    if isinstance(request_binding, Mapping):
        result.update(_v3_audit_binding_fields(request_binding))
    return result


def _v3_audit_binding_fields(request_binding: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "semantic_input_sha256", "projected_input_sha256", "review_wire_version",
        "review_input_projection_version",
    )
    if any(not isinstance(request_binding.get(key), str) for key in keys):
        raise ValueError("annual_review_v3_request_binding_invalid")
    return {key: request_binding[key] for key in keys}


def _bind_v3_audit_request(audit: Mapping[str, Any], request_binding: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(audit)
    bound["prompt_version"] = V3_PROMPT_VERSION
    bound["request_body_sha256"] = request_binding.get("request_body_sha256")
    bound.update(_v3_audit_binding_fields(request_binding))
    return bound


def _new_audits(run_dir: Path, before: set[str], artifacts_root: Path) -> tuple[list[dict[str, Any]], int]:
    artifacts: list[dict[str, Any]] = []
    attempts = 0
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return artifacts, attempts
    for child in children:
        if not child.is_dir() or child.name in before:
            continue
        if not re.fullmatch(r"[0-9a-f-]{36}", child.name):
            continue
        request_exists = (child / "request.json").is_file()
        attempts += int(request_exists)
        if (child / "response.json").is_file():
            status = "succeeded"
        elif (child / "failure.json").is_file():
            status = "failed"
        else:
            status = "started"
        artifacts.append(_audit_artifact(child, artifacts_root, status))
    return artifacts, attempts


def _run_children(run_dir: Path) -> set[str]:
    try:
        return {item.name for item in run_dir.iterdir() if item.is_dir()}
    except OSError:
        return set()
