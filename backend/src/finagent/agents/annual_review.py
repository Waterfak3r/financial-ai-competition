"""One bounded, audited model opinion for a v2 annual analysis report."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from finagent.audit import EvidenceRef, audited_complete_chat
from finagent.audit.audited_chat import repository_runs_root
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage

PROMPT_VERSION = "annual_review_v1"
MAX_REQUEST_CHARS = 16_000
MAX_RESPONSE_CHARS = 12_000
MAX_FACTS = 40
MAX_CALCULATIONS = 40
MAX_SIGNALS = 30
MAX_PENDING_ITEMS = 60
MAX_INVESTIGATION_ITEMS = 30
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_review_v1.md"

ASSESSMENTS = frozenset(
    {
        "prioritize_review",
        "no_priority_issue_identified_within_scope",
        "insufficient_evidence",
    }
)


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
    try:
        review_input, allowed_ids, evidence_refs, no_priority_allowed = _build_review_input(
            report, investigation, identity
        )
        serialized = json.dumps(review_input, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > MAX_REQUEST_CHARS:
            return result
        prompt = _load_prompt()
        audited = audited_complete_chat(
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(role="user", content=serialized),
            ],
            settings,
            run_dir=run_dir,
            evidence_refs=evidence_refs,
            prompt_version=PROMPT_VERSION,
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
            "call_count": 1,
            "call_attempt_count": 1,
            "audit_artifacts": audit_artifacts,
            "source_identity": identity,
        }
    except Exception as exc:
        audits, attempts = _new_audits(run_dir, before, artifacts_root)
        response_present = any(a.get("response_path") for a in audits)
        known_response_reasons = {
            "model_review_response_invalid",
            "model_review_numeric_claim_rejected",
            "model_review_no_priority_not_supported",
            "model_review_priority_without_reason",
            "model_review_no_priority_without_reason",
        }
        safe_reason = str(exc) if isinstance(exc, ValueError) and str(exc) in known_response_reasons else None
        result.update(
            reason=safe_reason or ("model_review_response_invalid" if response_present else "model_review_call_failed"),
            model_called=attempts > 0,
            call_count=attempts,
            call_attempt_count=attempts,
            audit_artifacts=audits,
        )
        return result


def parse_model_review(
    text: str,
    *,
    allowed_ids: set[str],
    no_priority_allowed: bool,
) -> dict[str, Any]:
    """Require an exact small schema and reject unsupported references or figures."""

    if not isinstance(text, str) or not text.strip() or len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("model_review_response_invalid")
    try:
        payload = json.loads(text)
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
            "narrative_evidence_ids": _id_list(item.get("narrative_evidence_ids"), 12),
        })
        allowed.add(review_id)

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
        "schema_version": "annual_model_review_v1",
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
            "comparability_status": comparability_status,
            "comparability_identity_matches": comparability_matches,
            "unvalidated_confirmed_record_count": unvalidated_confirmed_count,
            "scope_gaps": safe_gaps,
            "truncated": truncated,
        },
        "verified_facts": verified_facts,
        "verified_calculations": verified_calculations,
        "candidate_signals": candidate_signals,
        "unresolved_review_items": pending_items,
        "investigation_items": investigation_items,
    }
    # Keep the full message within a small bound. Drop tail records in a fixed
    # order, keep coverage counts as counts of the supplied subset, and block
    # no-priority conclusions whenever a record had to be omitted.
    lists = [
        "investigation_items", "unresolved_review_items", "candidate_signals",
        "verified_calculations", "verified_facts",
    ]
    while len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))) > MAX_REQUEST_CHARS and any(data[key] for key in lists):
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
    }
    evidence_pages = {
        page for item in data["verified_facts"] for page in item.get("pdf_pages", [])
    }
    scope_gaps_are_only_candidate_notice = bool(safe_gaps) and all(
        gap == "候选异常只在待核查区列出，不写成舞弊结论。" for gap in safe_gaps
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
        and data["coverage"]["comparability_identity_matches"]
        and (
            (investigation_status == "completed" and abstained_count == 0)
            or (investigation_status == "abstained" and investigation_reason == "no_supported_candidate_signal" and not investigation_items)
        )
        and comparability_allows_priority
        and not substantive_scope_gaps
    )
    data["coverage"]["no_priority_eligible"] = no_priority_allowed
    return data, allowed, pages, no_priority_allowed


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


def _id_list(value: object, maximum_count: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value[:maximum_count] if isinstance(item, str) and _ID_RE.fullmatch(item)]


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


