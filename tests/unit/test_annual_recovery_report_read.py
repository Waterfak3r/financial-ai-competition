from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from finagent.api.annual_analysis_history import list_annual_analysis_history
from finagent.api.annual_report_export import export_annual_report
from finagent.api.annual_report_read import (
    AnnualReportReadError,
    _m3_semantic_binding_sha256 as _reader_m3_semantic_binding_sha256,
    _validate_discovery_coverage,
    _validate_v3_execution_contract,
    _validate_v4_execution_contract,
    load_annual_report,
)
from finagent.core.annual_recovery import _m3_semantic_binding_sha256 as _execution_m3_semantic_binding_sha256
from finagent.core.annual_recovery import (
    recovery_identity_sha256,
    validate_annual_recovery_parent,
)
from finagent.core.annual_work_budget import archived_budget_contract_v1
import finagent.core.annual_work_budget as live_annual_work_budget
from finagent.core.annual_checkpoints import AnnualCheckpointError, save_annual_checkpoint
from finagent.core.model_settings import ModelSettings
from finagent.retrieval.annual_discovery_context import AnnualDiscoveryContext, build_annual_discovery_context

_RUN_ID = "annual-analysis-00000000-0000-0000-0000-000000000101"
_COMPANY = "603288"
_DOCUMENT = "doc-recovery-fixture"
_YEAR = 2024
_MODE = "model_investigation"
_WORDS = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey", "yankee",
)


def _write_run(root: Path, suffix: int = 101, *, mode: str = _MODE) -> dict[str, Any]:
    run_id = f"annual-analysis-00000000-0000-0000-0000-{suffix:012d}"
    run_dir = root / "artifacts" / "runs" / run_id
    report_dir = root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)

    document = pymupdf.open()
    for page_index in range(4):
        page = document.new_page(width=612, height=792)
        source_text = (f"Annual disclosure page alpha beta gamma delta {page_index} ".replace(str(page_index), "alpha"))
        source_text += "company report evidence " * 28
        page.insert_textbox(pymupdf.Rect(36, 36, 576, 756), source_text, fontsize=8)
    source = document.tobytes()
    document.close()
    (run_dir / "source.pdf").write_bytes(source)
    source_sha = hashlib.sha256(source).hexdigest()

    manifest = {
        "run_id": run_id,
        "mode": mode,
        "status": "running",
        "company_id": _COMPANY,
        "report_year": _YEAR,
        "document_id": _DOCUMENT,
        "inputs": {
            "source_pdf_archive_path": f"runs/{run_id}/source.pdf",
            "source_pdf_sha256": source_sha,
        },
        "outputs": {
            "run_dir": f"runs/{run_id}",
            "report_dir": f"reports/{run_id}",
            "report_json": f"reports/{run_id}/report.json",
        },
    }
    _write_json(run_dir / "manifest.json", manifest)
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": run_id,
        "company_id": _COMPANY,
        "report_year": _YEAR,
        "source_document_id": _DOCUMENT,
        "source_sha256": source_sha,
        "scope": {"note": "Constructed annual recovery fixture.", "gaps": []},
        "confirmed": {"metrics": [], "analyses": []},
        "verified_claims": [],
        "interpretations": [],
        "pending_review": {
            "facts": [], "calculations": [], "claims": [], "candidate_signals": [],
            "uncited_evidences": [],
        },
        "limitations": [],
    }
    source_record = {
        "company_id": _COMPANY,
        "document_id": _DOCUMENT,
        "report_period": "2024-12-31",
        "sha256": source_sha,
        "local_path": "data/raw/fixture.pdf",
    }
    context = build_annual_discovery_context(
        report,
        run_dir / "source.pdf",
        source_record,
        system_prompt="constructed recovery test prompt",
        model_name="local-fixture-model",
        max_fragment_chars=240,
        max_batch_raw_chars=240,
        max_batches=32,
        max_total_chars=20_000,
    )
    assert len(context.batches) >= 4
    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "report_dir": report_dir,
        "manifest": manifest,
        "report": report,
        "mode": mode,
        "source_sha": source_sha,
        "source_record": source_record,
        "context": context,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _canonical_sha(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binding(run: MappingLike, plan_sha: str) -> dict[str, Any]:
    mode = run["mode"]
    m3_screening = run["report"].get("m3_screening")
    m3_binding_sha256 = None
    if mode == "integrated_analysis":
        m3_binding_sha256 = _execution_m3_semantic_binding_sha256(
            m3_screening,
            {
                "sha256": run["source_sha"],
                "company_id": _COMPANY,
                "report_year": _YEAR,
                "document_id": _DOCUMENT,
                "mode": mode,
            },
        )
    return {
        "source_sha256": run["source_sha"],
        "company_id": _COMPANY,
        "report_year": _YEAR,
        "source_document_id": _DOCUMENT,
        "analysis_mode": mode,
        "provider_model_fingerprint": "a" * 64,
        "model": "local-fixture-model",
        "prompts_sha256": "b" * 64,
        "parser_version": "annual-discovery-parser-v2-execution-v3",
        "plan_sha256": plan_sha,
        "m3_binding_sha256": m3_binding_sha256,
    }


def _share_source(parent: MappingLike, child: MappingLike) -> None:
    """Give a child run byte-identical source material and matching manifest binding."""

    source_bytes = parent["run_dir"].joinpath("source.pdf").read_bytes()
    child["run_dir"].joinpath("source.pdf").write_bytes(source_bytes)
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    child["manifest"]["inputs"]["source_pdf_sha256"] = source_sha
    _write_json(child["run_dir"] / "manifest.json", child["manifest"])
    child["source_sha"] = source_sha
    child["source_record"] = {**parent["source_record"], "sha256": source_sha}
    child["context"] = parent["context"]
    child["report"] = copy.deepcopy(parent["report"])
    child["report"].update({"run_id": child["run_id"], "source_sha256": source_sha})


MappingLike = dict[str, Any]


def _plan_sha(context: AnnualDiscoveryContext) -> str:
    plan = [
        {
            "batch_index": batch.batch_index,
            "pages": sorted({item.pdf_page for item in batch.fragments}),
            "raw_chars": batch.raw_chars,
            "fragments": [
                {
                    "evidence_id": item.evidence_id,
                    "pdf_page": item.pdf_page,
                    "block_index": item.block_index,
                    "start_char": item.start_char,
                    "raw_text_sha256": hashlib.sha256(item.raw_text.encode("utf-8")).hexdigest(),
                }
                for item in batch.fragments
            ],
        }
        for batch in context.batches
    ]
    return _canonical_sha(plan)


def _attempt(accepted: int, *, index: int = 1, failure: str | None = None) -> dict[str, Any]:
    if failure is None:
        return {
            "attempt_index": index,
            "audit_id": str(uuid.uuid4()),
            "admission_id": str(uuid.uuid4()),
            "provider_admitted": True,
            "status": "succeeded",
            "response_accepted": True,
            "accepted_findings": accepted,
            "reason": None,
        }
    return {
        "attempt_index": index,
        "audit_id": str(uuid.uuid4()),
        "admission_id": str(uuid.uuid4()),
        "provider_admitted": True,
        "status": "failed",
        "response_accepted": False,
        "accepted_findings": 0,
        "reason": failure,
    }


def _finding(source_sha: str, fragment: Any, word: str, batch_index: int) -> dict[str, Any]:
    finding_id = f"disc:{source_sha[:12]}-{hashlib.sha256(f'{word}:{batch_index}'.encode()).hexdigest()[:20]}"
    quote_text = fragment.raw_text[: min(42, len(fragment.raw_text))].strip()
    return {
        "finding_id": finding_id,
        "title": f"Constructed finding {word}",
        "observation": "The source describes a business arrangement for review.",
        "why_it_matters": "The disclosure may affect how readers understand operations.",
        "follow_up": "Review the related agreement and approval record.",
        "priority": "medium",
        "classification": "unverified_model_finding",
        "verification_status": "unverified",
        "evidence_ids": [fragment.evidence_id],
        "citations": [{"evidence_id": fragment.evidence_id, "quote": quote_text}],
        "batch_index": batch_index,
    }


def _base_rows(context: AnnualDiscoveryContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    coverage_rows: list[dict[str, Any]] = []
    contract_rows: list[dict[str, Any]] = []
    for batch in context.batches:
        coverage_rows.append({
            "batch_index": batch.batch_index,
            "status": "unattempted",
            "reason": "awaiting_model",
            "pages": sorted({fragment.pdf_page for fragment in batch.fragments}),
            "raw_chars": batch.raw_chars,
            "accepted_findings": 0,
            "rejected_findings": 0,
            "restored_quotes": 0,
            "resolution": "unattempted",
            "inherited_findings": 0,
        })
        contract_rows.append({
            "batch_index": batch.batch_index,
            "state": "unattempted",
            "resolution": "unattempted",
            "reason": "awaiting_model",
            "attempts": [],
        })
    return coverage_rows, contract_rows


def _coverage(
    context: AnnualDiscoveryContext,
    coverage_rows: list[dict[str, Any]],
    contract_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    value = dict(context.coverage)
    counts = {key: 0 for key in ("succeeded", "partial", "failed", "unattempted")}
    result_pages = {key: set() for key in counts}
    processed_chars = attempted_chars = unattempted_chars = 0
    for row, contract in zip(coverage_rows, contract_rows):
        state = row["status"]
        raw_chars = row["raw_chars"]
        counts[state] += 1
        result_pages[state].update(row["pages"])
        if state == "succeeded":
            processed_chars += raw_chars
        if state == "unattempted":
            unattempted_chars += raw_chars
        if any(attempt["provider_admitted"] for attempt in contract["attempts"]):
            attempted_chars += raw_chars
    processed = counts["succeeded"] + counts["partial"] + counts["failed"]
    planned_chars = sum(row["raw_chars"] for row in coverage_rows)
    omitted = max(value["total_chars"] - planned_chars, 0) + unattempted_chars
    unread_pages = set(value["unread_pages"])
    for state in ("partial", "failed", "unattempted"):
        unread_pages.update(result_pages[state])
    value.update({
        "processed_batches": processed,
        "successful_batches": counts["succeeded"],
        "partial_batches": counts["partial"],
        "failed_batches": counts["failed"],
        "batch_results": coverage_rows,
        "processed_chars": processed_chars,
        "attempted_chars": attempted_chars,
        "unattempted_batches": counts["unattempted"],
        "unattempted_chars": unattempted_chars,
        "planned_chars": planned_chars,
        "omitted_chars": omitted,
        "unread_pages": sorted(unread_pages),
        "failed_pages": sorted(result_pages["failed"]),
        "truncated": bool(omitted),
        "complete": (
            value["source_status"] == "parsed"
            and value["content_status"] == "text_available"
            and value["total_chars"] > 0
            and not bool(omitted)
            and not value["unread_image_pages"]
            and planned_chars == value["total_chars"]
            and processed == len(coverage_rows)
            and counts["partial"] == 0
            and counts["failed"] == 0
            and counts["succeeded"] == len(coverage_rows)
            and processed_chars == planned_chars
            and not unread_pages
        ),
        "time_budget_exhausted": any(
            row["status"] == "unattempted" and row["reason"] == "workflow_time_budget_exhausted"
            for row in coverage_rows
        ),
    })
    return value


def _discovery(
    run: MappingLike,
    *,
    parent_run_id: str | None = None,
    parent_admissions: int = 0,
    batch_modes: dict[int, str] | None = None,
    empty_response: bool = False,
    retry_failure_count: int = 0,
) -> dict[str, Any]:
    context: AnnualDiscoveryContext = run["context"]
    source_sha = run["source_sha"]
    coverage_rows, contract_rows = _base_rows(context)
    batch_modes = batch_modes or {}
    findings: list[dict[str, Any]] = []
    first_fragment = context.batches[0].fragments[0]
    for batch in context.batches:
        index = batch.batch_index
        mode = batch_modes.get(index, "unattempted")
        if mode == "partial_parent":
            attempt = _attempt(5)
            contract_rows[index - 1].update({
                "state": "partial", "resolution": "executed", "reason": "some_findings_rejected", "attempts": [attempt],
            })
            coverage_rows[index - 1].update({
                "status": "partial", "resolution": "executed", "reason": "some_findings_rejected",
                "accepted_findings": 5, "rejected_findings": 1,
            })
            findings.extend(_finding(source_sha, first_fragment, word, index) for word in _WORDS[:5])
        elif mode == "success_parent":
            accepted = 2
            attempt = _attempt(accepted)
            contract_rows[index - 1].update({"state": "succeeded", "resolution": "executed", "reason": None, "attempts": [attempt]})
            coverage_rows[index - 1].update({
                "status": "succeeded", "resolution": "executed", "reason": None, "accepted_findings": accepted,
            })
            findings.extend(_finding(source_sha, batch.fragments[0], word, index) for word in _WORDS[5:7])
        elif mode == "new_success":
            inherited = 5
            attempt = _attempt(6)
            contract_rows[index - 1].update({"state": "succeeded", "resolution": "executed", "reason": None, "attempts": [attempt]})
            coverage_rows[index - 1].update({
                "status": "succeeded", "resolution": "executed", "reason": None,
                "accepted_findings": inherited + 6, "inherited_findings": inherited,
            })
            findings.extend(_finding(source_sha, first_fragment, word, index) for word in _WORDS[:11])
        elif mode == "new_success_v4":
            attempt = _attempt(2)
            contract_rows[index - 1].update({"state": "succeeded", "resolution": "executed", "reason": None, "attempts": [attempt]})
            coverage_rows[index - 1].update({
                "status": "succeeded", "resolution": "executed", "reason": None,
                "accepted_findings": 2,
            })
            findings.extend(_finding(source_sha, batch.fragments[0], word, index) for word in _WORDS[:2])
        elif mode == "reuse":
            contract_rows[index - 1].update({"state": "succeeded", "resolution": "reused", "reason": None, "attempts": []})
            coverage_rows[index - 1].update({
                "status": "succeeded", "resolution": "reused", "reason": None,
                "accepted_findings": 2, "inherited_findings": 2,
            })
            findings.extend(_finding(source_sha, batch.fragments[0], word, index) for word in _WORDS[5:7])
        elif mode in {"all_failed", "empty_response"}:
            if mode == "all_failed":
                attempts = [_attempt(0, index=1, failure="http_429"), _attempt(0, index=2, failure="timeout")]
                batch_reason = "model_call_failed"
            else:
                attempts = [_attempt(0)]
                batch_reason = "batch_missing"
            contract_rows[index - 1].update({
                "state": "failed", "resolution": "executed", "reason": batch_reason, "attempts": attempts,
            })
            coverage_rows[index - 1].update({
                "status": "failed", "resolution": "executed", "reason": batch_reason,
                "accepted_findings": 5, "inherited_findings": 5,
            })
            if mode == "empty_response":
                empty_response = True
        elif mode == "time_unattempted":
            coverage_rows[index - 1]["reason"] = "workflow_time_budget_exhausted"
            contract_rows[index - 1]["reason"] = "workflow_time_budget_exhausted"
        elif mode == "unattempted":
            pass
        else:
            raise AssertionError(f"unsupported batch fixture mode: {mode}")

    if parent_run_id is not None and batch_modes.get(1) != "new_success":
        # Failed retries and accepted-empty responses retain the complete
        # parent's finding union, including findings from reused batches.
        new_v4_findings = [
            finding for finding in findings
            if batch_modes.get(finding.get("batch_index")) == "new_success_v4"
        ]
        findings = copy.deepcopy(run["parent_findings"]) + new_v4_findings

    coverage = _coverage(context, coverage_rows, contract_rows)
    attempts = [attempt for row in contract_rows for attempt in row["attempts"]]
    current_admissions = sum(attempt["provider_admitted"] is True for attempt in attempts)
    response_count = sum(attempt["response_accepted"] is True for attempt in attempts)
    model_status = "not_called" if not attempts else "partial"
    model_reason = "awaiting_model" if not attempts else "recovery_pending"
    if coverage["complete"] and coverage["failed_batches"] == 0 and coverage["partial_batches"] == 0:
        model_status = "completed"
        model_reason = None
    if coverage["successful_batches"] + coverage["partial_batches"] == 0 and coverage["failed_batches"] > 0:
        model_status = "failed"
        model_reason = "model_call_failed"
    plan_sha = _plan_sha(context)
    binding = _binding(run, plan_sha)
    contract = {
        "kind": "fintrace_annual_discovery_execution_v3",
        "plan_sha256": plan_sha,
        "identity_sha256": _canonical_sha(binding),
        "binding": binding,
        "parent_run_id": parent_run_id,
        "provider_admission_count": current_admissions,
        "parent_provider_admission_count": parent_admissions,
        "cumulative_provider_admission_count": parent_admissions + current_admissions,
        "response_count": response_count,
        "reused_batches": [row["batch_index"] for row in contract_rows if row["resolution"] == "reused"],
        "batches": contract_rows,
    }
    fragment_by_id = {fragment.evidence_id: fragment for fragment in context.fragments}
    evidence_ids = {evidence_id for finding in findings for evidence_id in finding["evidence_ids"]}
    return {
        "kind": "fintrace_annual_discovery",
        "run_id": run["run_id"],
        "company_id": _COMPANY,
        "report_year": _YEAR,
        "source_document_id": _DOCUMENT,
        "source_sha256": source_sha,
        "status": model_status,
        "prompt_version": "annual_discovery_v2",
        "model_called": current_admissions > 0,
        "model_call_count": response_count,
        "model_call_attempt_count": current_admissions,
        "coverage": coverage,
        "findings": findings,
        "evidences": [
            fragment_by_id[evidence_id].evidence_value(_DOCUMENT, source_sha)
            for evidence_id in sorted(evidence_ids)
        ],
        "audit_artifacts": [],
        "limitations": [],
        "reason": model_reason,
        "fraud_conclusion": None,
        "execution_contract": contract,
    }


def _as_v4_contract(discovery: MappingLike) -> dict[str, Any]:
    contract = copy.deepcopy(discovery["execution_contract"])
    binding = dict(contract["binding"])
    binding.update({
        "plan_version": "annual_discovery_plan_v3",
        "execution_version": "execution_v4",
        "parser_version": "annual-discovery-parser-v3-execution-v4",
    })
    contract.update({
        "kind": "fintrace_annual_discovery_execution_v4",
        "binding": binding,
        "identity_sha256": _canonical_sha(binding),
        "scheduler": {
            "requested_max_concurrency": 3,
            "effective_max_concurrency": 3,
            "inflight_peak": 0,
            "checkpoint_coordinator_count": 1,
            "checkpoint_publications": 0,
            "dispatch_closed_reason": None,
            "time_deadline_reason": None,
            "inflight_drained": True,
            "inflight_count": 0,
        },
        "phase_budget": archived_budget_contract_v1(
            scope="resume" if contract.get("parent_run_id") is not None else "new_run"
        ),
        "timing": {
            "source_parse_seconds": 0.0,
            "planning_seconds": 0.0,
            "source_parse_reused": False,
            "discovery_elapsed_seconds": 0.0,
            "main_work_remaining_seconds": 120.0,
        },
    })
    return contract


def _v4_discovery(
    run: MappingLike,
    *,
    parent_run_id: str | None = None,
    parent_admissions: int = 0,
    batch_modes: dict[int, str] | None = None,
    binding_model: str | None = None,
) -> dict[str, Any]:
    discovery = _discovery(
        run,
        parent_run_id=parent_run_id,
        parent_admissions=parent_admissions,
        batch_modes=batch_modes,
    )
    discovery["prompt_version"] = "annual_discovery_v3"
    contract = _as_v4_contract(discovery)
    if binding_model is not None:
        contract["binding"]["model"] = binding_model
        contract["identity_sha256"] = _canonical_sha(contract["binding"])
    discovery["execution_contract"] = contract
    return discovery


def _validate_test_v4(run: MappingLike, discovery: MappingLike, contract: MappingLike | None = None) -> dict[str, Any]:
    value = contract or _as_v4_contract(discovery)
    normalized = _validate_v4_execution_contract(
        value,
        expected_run_id=run["run_id"],
        expected_company_id=_COMPANY,
        expected_report_year=_YEAR,
        expected_document_id=_DOCUMENT,
        expected_sha256=run["source_sha"],
        expected_analysis_mode=run["mode"],
        m3_screening=None,
        coverage=discovery["coverage"],
    )
    _validate_discovery_coverage(
        discovery["coverage"],
        discovery["status"],
        discovery["prompt_version"],
        execution_contract=value,
    )
    return normalized


def test_v4_strict_limits_match_three_findings_and_leave_legacy_v3_cap_unchanged(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 180)
    discovery = _discovery(run, batch_modes={1: "success_parent"})
    contract = _as_v4_contract(discovery)
    contract["batches"][0]["attempts"][0]["accepted_findings"] = 3
    discovery["coverage"]["batch_results"][0]["accepted_findings"] = 3
    _validate_test_v4(run, discovery, contract)

    wrong_kind = copy.deepcopy(contract)
    wrong_kind["kind"] = "fintrace_annual_discovery_execution_v3"
    with pytest.raises(AnnualReportReadError) as error:
        _validate_test_v4(run, discovery, wrong_kind)
    assert error.value.code == "annual_discovery_invalid"

    over_three = copy.deepcopy(contract)
    over_three["batches"][0]["attempts"][0]["accepted_findings"] = 4
    discovery_four_accepted = copy.deepcopy(discovery)
    discovery_four_accepted["coverage"]["batch_results"][0]["accepted_findings"] = 4
    with pytest.raises(AnnualReportReadError) as error:
        _validate_test_v4(run, discovery_four_accepted, over_three)
    assert error.value.code == "annual_discovery_invalid"

    all_rejected = _discovery(run, batch_modes={1: "success_parent"})
    rejected_contract = _as_v4_contract(all_rejected)
    rejected_contract["batches"][0].update({"state": "failed", "reason": "all_findings_rejected"})
    rejected_contract["batches"][0]["attempts"][0]["accepted_findings"] = 0
    rejected_coverage = all_rejected["coverage"]["batch_results"][0]
    rejected_coverage.update({
        "status": "failed", "reason": "all_findings_rejected",
        "accepted_findings": 0, "rejected_findings": 4,
    })
    with pytest.raises(AnnualReportReadError) as error:
        _validate_test_v4(run, all_rejected, rejected_contract)
    assert error.value.code == "annual_discovery_invalid"

    # The current strict v3 reader intentionally retains its historical six
    # finding limit; R3's tighter cap applies only to execution_v4.
    legacy_discovery = _discovery(run, batch_modes={1: "success_parent"})
    legacy_discovery["execution_contract"]["batches"][0]["attempts"][0]["accepted_findings"] = 5
    legacy_discovery["coverage"]["batch_results"][0]["accepted_findings"] = 5
    _validate_v3_execution_contract(
        legacy_discovery["execution_contract"],
        expected_run_id=run["run_id"],
        expected_company_id=_COMPANY,
        expected_report_year=_YEAR,
        expected_document_id=_DOCUMENT,
        expected_sha256=run["source_sha"],
        expected_analysis_mode=run["mode"],
        m3_screening=None,
        coverage=legacy_discovery["coverage"],
    )


@pytest.mark.parametrize(
    ("deadline_reason", "dispatch_reason", "coverage_exhausted"),
    [
        (None, "discovery_stage_budget_exhausted", False),
        ("discovery_stage_budget_exhausted", "discovery_stage_budget_exhausted", True),
        ("workflow_time_budget_exhausted", "workflow_time_budget_exhausted", True),
    ],
)
def test_v4_distinguishes_stage_attempt_cap_from_real_deadline(
    tmp_path: Path,
    deadline_reason: str | None,
    dispatch_reason: str,
    coverage_exhausted: bool,
) -> None:
    run = _write_run(tmp_path, 181)
    discovery = _discovery(run, batch_modes={1: "time_unattempted"})
    contract = _as_v4_contract(discovery)
    contract["scheduler"]["dispatch_closed_reason"] = dispatch_reason
    contract["scheduler"]["time_deadline_reason"] = deadline_reason
    contract["batches"][0]["reason"] = dispatch_reason
    discovery["coverage"]["batch_results"][0]["reason"] = dispatch_reason
    discovery["coverage"]["time_budget_exhausted"] = coverage_exhausted
    _validate_test_v4(run, discovery, contract)

    forged_coverage = copy.deepcopy(discovery)
    forged_coverage["coverage"]["time_budget_exhausted"] = not coverage_exhausted
    with pytest.raises(AnnualReportReadError) as error:
        _validate_test_v4(run, forged_coverage, contract)
    assert error.value.code == "annual_discovery_invalid"


def test_v4_archive_budget_v1_reader_is_independent_of_live_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _write_run(tmp_path, 182)
    discovery = _discovery(run, batch_modes={1: "success_parent"})
    contract = _as_v4_contract(discovery)
    frozen_archive = copy.deepcopy(contract["phase_budget"])

    monkeypatch.setattr(live_annual_work_budget, "MAIN_REPORT_BUDGET_SECONDS", 90)
    monkeypatch.setattr(live_annual_work_budget, "FINALIZATION_RESERVE_SECONDS", 5)
    monkeypatch.setattr(live_annual_work_budget, "WORK_DEADLINE_OFFSET_SECONDS", 85)
    monkeypatch.setattr(live_annual_work_budget, "DISCOVERY_BUDGET_SECONDS", 30)
    monkeypatch.setattr(live_annual_work_budget, "FIXED_INVESTIGATION_BUDGET_SECONDS", 20)
    monkeypatch.setattr(live_annual_work_budget, "ISSUE_GROUPING_RESERVED_SECONDS", 15)
    monkeypatch.setattr(live_annual_work_budget, "FINAL_REVIEW_BUDGET_SECONDS", 15)
    monkeypatch.setattr(live_annual_work_budget, "NEW_RUN_ATTEMPT_BUDGET", 9)
    monkeypatch.setattr(live_annual_work_budget, "RESUME_ATTEMPT_BUDGET", 8)
    monkeypatch.setattr(live_annual_work_budget, "NEW_RUN_STAGE_ATTEMPT_BUDGETS", {})
    monkeypatch.setattr(live_annual_work_budget, "RESUME_STAGE_ATTEMPT_BUDGETS", {})

    assert archived_budget_contract_v1(scope="new_run") == frozen_archive
    _validate_test_v4(run, discovery, contract)


def test_v4_budget_scope_must_match_parent_run_presence(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 191)
    discovery = _discovery(run, batch_modes={1: "success_parent"})
    contract = _as_v4_contract(discovery)
    contract["phase_budget"] = archived_budget_contract_v1(scope="resume")

    with pytest.raises(AnnualReportReadError) as error:
        _validate_test_v4(run, discovery, contract)
    assert error.value.code == "annual_discovery_invalid"


def _save_checkpoint(
    root: Path,
    run: MappingLike,
    *,
    modes: dict[int, str] | None = None,
    parent_run_id: str | None = None,
    parent_admissions: int = 0,
    state: str = "running",
    is_partial: bool = True,
    empty_response: bool = False,
) -> dict[str, Any]:
    report = copy.deepcopy(run["report"])
    report["model_discovery"] = _discovery(
        run,
        parent_run_id=parent_run_id,
        parent_admissions=parent_admissions,
        batch_modes=modes,
        empty_response=empty_response,
    )
    report["model_discovery"]["model_call_attempt_count"] = report["model_discovery"]["execution_contract"]["provider_admission_count"]
    snapshot = {
        "state": state,
        "is_partial": is_partial,
        "parent_run_id": parent_run_id,
        "source_sha256": run["source_sha"],
        "report": report,
    }
    record = save_annual_checkpoint(root, run["run_id"], snapshot)
    if not is_partial:
        _write_json(run["report_dir"] / "report.json", report)
        run["manifest"]["status"] = state
        _write_json(run["run_dir"] / "manifest.json", run["manifest"])
    return {"snapshot": record, "report": report}


def _save_v4_checkpoint(
    root: Path,
    run: MappingLike,
    *,
    modes: dict[int, str] | None = None,
    parent_run_id: str | None = None,
    parent_admissions: int = 0,
    state: str = "running",
    is_partial: bool = True,
    binding_model: str | None = None,
) -> dict[str, Any]:
    report = copy.deepcopy(run["report"])
    report["model_discovery"] = _v4_discovery(
        run,
        parent_run_id=parent_run_id,
        parent_admissions=parent_admissions,
        batch_modes=modes,
        binding_model=binding_model,
    )
    discovery = report["model_discovery"]
    discovery["model_call_attempt_count"] = discovery["execution_contract"]["provider_admission_count"]
    snapshot = {
        "state": state,
        "is_partial": is_partial,
        "parent_run_id": parent_run_id,
        "source_sha256": run["source_sha"],
        "report": report,
    }
    record = save_annual_checkpoint(root, run["run_id"], snapshot)
    if not is_partial:
        _write_json(run["report_dir"] / "report.json", report)
        run["manifest"]["status"] = state
        _write_json(run["run_dir"] / "manifest.json", run["manifest"])
    return {"snapshot": record, "report": report}


def _rewrite_checkpoint_report(root: Path, run: MappingLike, update) -> None:
    pointer_path = run["run_dir"] / "checkpoint-current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    snapshot_path = root / pointer["snapshot_path"]
    envelope = json.loads(snapshot_path.read_text(encoding="utf-8"))
    report = envelope["report"]
    update(report)
    report_bytes = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    envelope["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    snapshot_bytes = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    pointer["report_sha256"] = envelope["report_sha256"]
    pointer["snapshot_sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()
    snapshot_path.write_bytes(snapshot_bytes)
    pointer_path.write_text(
        json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    report_path = run["report_dir"] / "report.json"
    if report_path.exists():
        _write_json(report_path, report)


def _unattempted_modes(context: AnnualDiscoveryContext) -> dict[int, str]:
    return {batch.batch_index: "unattempted" for batch in context.batches}


def _review_investigation(run: MappingLike, discovery: MappingLike) -> dict[str, Any]:
    return {
        "kind": "fintrace_annual_investigation",
        "run_id": run["run_id"],
        "company_id": _COMPANY,
        "report_year": _YEAR,
        "source_document_id": _DOCUMENT,
        "source_sha256": run["source_sha"],
        "status": "abstained",
        "reason": "no_supported_candidate_signal",
        "model_name": "local-fixture-model",
        "model_called": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "items": [],
        "narrative_evidences": [],
        "discovery": discovery,
    }


def _original_review_with_audit(run: MappingLike, report: dict[str, Any]) -> dict[str, Any]:
    from finagent.agents.annual_review import _report_identity, parse_model_review
    from finagent.reports.investigation_append import _review_reuse_request_material

    investigation = _review_investigation(run, report["model_discovery"])
    report["model_investigation"] = investigation
    material = _review_reuse_request_material(
        report,
        investigation,
        _report_identity(report),
        model_name="local-fixture-model",
    )
    text = json.dumps({
        "assessment": "insufficient_evidence",
        "summary": "The available evidence is incomplete.",
        "reasons": [],
        "follow_up_items": [],
        "limitations": [],
    }, ensure_ascii=False, separators=(",", ":"))
    parsed = parse_model_review(
        text,
        allowed_ids=set(material["allowed_ids"]),
        no_priority_allowed=material["no_priority_allowed"],
    )
    audit_id = str(uuid.uuid4())
    admission_id = str(uuid.uuid4())
    attempt_audit_id = str(uuid.uuid4())
    audit_dir = run["run_dir"] / audit_id
    attempt_dir = audit_dir / "attempt-01"
    attempt_dir.mkdir(parents=True)
    messages = [
        {"role": "system", "content": material["system_prompt"]},
        {"role": "user", "content": material["user_content"]},
    ]
    refs = material["evidence_refs"]
    _write_json(audit_dir / "request.json", {
        "audit_id": audit_id,
        "status": "started",
        "started_at": "2026-10-07T12:00:00+00:00",
        "finished_at": None,
        "duration_seconds": None,
        "prompt_version": "annual_review_v2",
        "evidence_refs": refs,
        "model": "local-fixture-model",
        "timeout_seconds": 10.0,
        "messages": messages,
        "stream": False,
    })
    _write_json(attempt_dir / "request.json", {
        "status": "started",
        "attempt_index": 1,
        "audit_id": attempt_audit_id,
        "started_at": "2026-10-07T12:00:00+00:00",
        "prompt_version": "annual_review_v2",
        "evidence_refs": refs,
        "model": "local-fixture-model",
        "timeout_seconds": 10.0,
        "messages": messages,
        "stream": False,
    })
    response_id = "chatcmpl-local-fixture"
    response_model = "local-fixture-model-snapshot"
    usage = {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}
    _write_json(attempt_dir / "response.json", {
        "status": "succeeded",
        "attempt_index": 1,
        "audit_id": attempt_audit_id,
        "finished_at": "2026-10-07T12:00:01+00:00",
        "text": text,
        "model": response_model,
        "response_id": response_id,
        "usage": usage,
        "provider_admitted": True,
        "admission_id": admission_id,
    })
    _write_json(audit_dir / "response.json", {
        "status": "succeeded",
        "started_at": "2026-10-07T12:00:00+00:00",
        "finished_at": "2026-10-07T12:00:01+00:00",
        "duration_seconds": 1.0,
        "text": text,
        "model": response_model,
        "response_id": response_id,
        "usage": usage,
        "provider_admission_count": 1,
        "response_count": 1,
        "attempt_records": [{
            "attempt_index": 1,
            "audit_id": attempt_audit_id,
            "admission_id": admission_id,
            "provider_admitted": True,
            "response_accepted": True,
            "accepted_findings": 0,
            "status": "succeeded",
            "reason": None,
        }],
    })
    return {
        "status": "completed",
        "reason": None,
        **parsed,
        "model_called": True,
        "call_count": 1,
        "call_attempt_count": 1,
        "audit_artifacts": [{
            "audit_dir": f"runs/{run['run_id']}/{audit_id}",
            "request_path": f"runs/{run['run_id']}/{audit_id}/request.json",
            "response_path": f"runs/{run['run_id']}/{audit_id}/response.json",
            "failure_path": None,
            "status": "succeeded",
            "prompt_version": "annual_review_v2",
        }],
        "source_identity": _report_identity(report),
    }


def _seal_review_run(
    root: Path,
    run: MappingLike,
    *,
    parent_run_id: str | None = None,
    parent_admissions: int = 0,
    parent_report: MappingLike | None = None,
) -> dict[str, Any]:
    from finagent.reports.investigation_append import _reuse_parent_review

    report = copy.deepcopy(run["report"])
    report["model_discovery"] = _discovery(
        run,
        parent_run_id=parent_run_id,
        parent_admissions=parent_admissions,
    )
    investigation = _review_investigation(run, report["model_discovery"])
    report["model_investigation"] = investigation
    if parent_report is None:
        review = _original_review_with_audit(run, report)
    else:
        parent_discovery = parent_report["model_discovery"]
        parent_investigation = parent_report["model_investigation"]
        review = _reuse_parent_review(
            parent_report,
            report,
            parent_investigation,
            investigation,
            parent_discovery,
            report["model_discovery"],
            current_model_name="local-fixture-model",
        )
        assert review is not None
    report["model_review"] = review
    _write_json(run["report_dir"] / "report.json", report)
    run["manifest"]["status"] = "completed_with_issues"
    _write_json(run["run_dir"] / "manifest.json", run["manifest"])
    save_annual_checkpoint(root, run["run_id"], {
        "state": "completed_with_issues",
        "is_partial": False,
        "parent_run_id": parent_run_id,
        "source_sha256": run["source_sha"],
        "report": report,
    })
    return report


def test_base_v3_checkpoint_is_complete_unattempted_plan_with_zero_calls(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    saved = _save_checkpoint(tmp_path, run)

    archive = load_annual_report(tmp_path, run["run_id"])

    discovery = archive.report["model_discovery"]
    assert archive.snapshot == {
        "state": "running", "sequence": 1, "is_partial": True, "parent_run_id": None,
    }
    assert discovery["status"] == "not_called"
    assert discovery["model_call_attempt_count"] == discovery["model_call_count"] == 0
    assert discovery["coverage"]["unattempted_batches"] == len(run["context"].batches)
    assert all(row["reason"] == "awaiting_model" for row in discovery["coverage"]["batch_results"])
    assert saved["snapshot"].sequence == 1


def test_review_reuse_strictly_preserves_original_audit_through_parent_child_grandchild(
    tmp_path: Path,
) -> None:
    parent = _write_run(tmp_path, 171)
    parent_report = _seal_review_run(tmp_path, parent)

    child = _write_run(tmp_path, 172)
    _share_source(parent, child)
    child["parent_findings"] = parent_report["model_discovery"]["findings"]
    child_report = _seal_review_run(
        tmp_path,
        child,
        parent_run_id=parent["run_id"],
        parent_admissions=0,
        parent_report=parent_report,
    )

    grandchild = _write_run(tmp_path, 173)
    _share_source(parent, grandchild)
    grandchild["parent_findings"] = child_report["model_discovery"]["findings"]
    grandchild_report = _seal_review_run(
        tmp_path,
        grandchild,
        parent_run_id=child["run_id"],
        parent_admissions=0,
        parent_report=child_report,
    )

    parent_archive = load_annual_report(tmp_path, parent["run_id"])
    child_archive = load_annual_report(tmp_path, child["run_id"])
    grandchild_archive = load_annual_report(tmp_path, grandchild["run_id"])
    original_audits = parent_archive.report["model_review"]["audit_artifacts"]
    assert original_audits
    original_response = json.loads(
        parent["run_dir"].joinpath(*original_audits[0]["response_path"].split("/")[2:])
        .read_text(encoding="utf-8")
    )
    original_attempt_response_path = (
        parent["run_dir"].joinpath(*original_audits[0]["response_path"].split("/")[2:]).parent
        / "attempt-01"
        / "response.json"
    )
    original_attempt_response = json.loads(original_attempt_response_path.read_text(encoding="utf-8"))
    assert original_response["model"] == "local-fixture-model-snapshot"
    assert original_attempt_response["model"] == original_response["model"]
    for archive, source_report in (
        (child_archive, child_report),
        (grandchild_archive, grandchild_report),
    ):
        review = archive.report["model_review"]
        assert review["reason"] == "review_reused_for_matching_inputs"
        assert review["model_called"] is False
        assert review["call_count"] == review["call_attempt_count"] == 0
        assert review["audit_artifacts"] == original_audits
        assert review["source_identity"]["run_id"] == source_report["run_id"]


@pytest.mark.parametrize("tampering", ["request_messages", "request_model", "attempt_response_model"])
def test_review_reuse_rejects_audit_tampering(tmp_path: Path, tampering: str) -> None:
    parent = _write_run(tmp_path, 174)
    parent_report = _seal_review_run(tmp_path, parent)
    audit = parent_report["model_review"]["audit_artifacts"][0]
    request_path = parent["run_dir"].joinpath(*audit["request_path"].split("/")[2:])
    if tampering == "request_messages":
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["messages"][1]["content"] += " altered"
        _write_json(request_path, request)
    elif tampering == "request_model":
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["model"] = "different-request-model"
        _write_json(request_path, request)
    else:
        response_path = parent["run_dir"].joinpath(*audit["response_path"].split("/")[2:])
        attempt_response_path = response_path.parent / "attempt-01" / "response.json"
        attempt_response = json.loads(attempt_response_path.read_text(encoding="utf-8"))
        attempt_response["model"] = "different-response-model"
        _write_json(attempt_response_path, attempt_response)

    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, parent["run_id"])
    assert error.value.code == "annual_review_reuse_invalid"


def test_review_reuse_rejects_child_with_changed_review_input(tmp_path: Path) -> None:
    parent = _write_run(tmp_path, 175)
    parent_report = _seal_review_run(tmp_path, parent)
    child = _write_run(tmp_path, 176)
    _share_source(parent, child)
    child["parent_findings"] = parent_report["model_discovery"]["findings"]
    child_report = copy.deepcopy(child["report"])
    child_report["scope"]["gaps"] = ["The scope now contains an additional limitation."]
    child_report["model_discovery"] = _discovery(
        child,
        parent_run_id=parent["run_id"],
        parent_admissions=0,
    )
    child_report["model_investigation"] = _review_investigation(
        child,
        child_report["model_discovery"],
    )
    reused_review = copy.deepcopy(parent_report["model_review"])
    reused_review.update(
        reason="review_reused_for_matching_inputs",
        model_called=False,
        call_count=0,
        call_attempt_count=0,
        source_identity={
            "run_id": child["run_id"],
            "company_id": _COMPANY,
            "report_year": _YEAR,
            "source_document_id": _DOCUMENT,
            "source_sha256": child["source_sha"],
        },
    )
    child_report["model_review"] = reused_review
    save_annual_checkpoint(tmp_path, child["run_id"], {
        "state": "running",
        "is_partial": True,
        "parent_run_id": parent["run_id"],
        "source_sha256": child["source_sha"],
        "report": child_report,
    })

    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, child["run_id"])
    assert error.value.code == "annual_review_reuse_invalid"


def test_v3_resume_unions_more_than_six_parent_findings_and_reuses_without_calls(tmp_path: Path) -> None:
    parent = _write_run(tmp_path, 101)
    parent_modes = _unattempted_modes(parent["context"])
    parent_modes.update({1: "partial_parent", 3: "success_parent"})
    parent_saved = _save_checkpoint(
        tmp_path,
        parent,
        modes=parent_modes,
        state="completed_with_issues",
        is_partial=False,
    )
    child = _write_run(tmp_path, 102)
    _share_source(parent, child)
    child["parent_findings"] = parent_saved["report"]["model_discovery"]["findings"]
    child_modes = _unattempted_modes(child["context"])
    child_modes.update({1: "new_success", 3: "reuse"})
    child_saved = _save_checkpoint(
        tmp_path,
        child,
        modes=child_modes,
        parent_run_id=parent["run_id"],
        parent_admissions=2,
        state="completed_with_issues",
        is_partial=False,
    )

    parent_archive = load_annual_report(tmp_path, parent["run_id"])
    child_archive = load_annual_report(tmp_path, child["run_id"])

    assert parent_archive.snapshot["state"] == "completed_with_issues"
    assert child_archive.snapshot == {
        "state": "completed_with_issues", "sequence": 1, "is_partial": False,
        "parent_run_id": parent["run_id"],
    }
    contract = child_archive.report["model_discovery"]["execution_contract"]
    coverage = child_archive.report["model_discovery"]["coverage"]
    assert contract["batches"][0]["attempts"][0]["accepted_findings"] == 6
    assert coverage["batch_results"][0]["inherited_findings"] == 5
    assert coverage["batch_results"][0]["accepted_findings"] == 11
    assert len(child_archive.report["model_discovery"]["findings"]) == 13
    assert contract["reused_batches"] == [3]
    reused_row = coverage["batch_results"][2]
    assert reused_row["resolution"] == "reused"
    assert reused_row["accepted_findings"] == reused_row["inherited_findings"] == 2
    assert reused_row["raw_chars"] <= coverage["processed_chars"]
    assert coverage["attempted_chars"] == coverage["batch_results"][0]["raw_chars"]
    assert coverage["unattempted_batches"] >= 2
    assert coverage["batch_results"][1]["status"] == "unattempted"
    assert coverage["batch_results"][1]["batch_index"] == 2
    assert parent_saved["snapshot"].sequence == child_saved["snapshot"].sequence == 1


def test_v4_resume_accepts_only_matching_parent_and_preserves_budget_scope(tmp_path: Path) -> None:
    parent = _write_run(tmp_path, 183)
    parent_modes = _unattempted_modes(parent["context"])
    parent_modes[1] = "success_parent"
    parent_saved = _save_v4_checkpoint(
        tmp_path, parent, modes=parent_modes, state="running", is_partial=True,
    )
    parent_archive = load_annual_report(tmp_path, parent["run_id"])

    child = _write_run(tmp_path, 184)
    _share_source(parent, child)
    child["parent_findings"] = parent_archive.report["model_discovery"]["findings"]
    child_modes = _unattempted_modes(child["context"])
    child_modes[1] = "reuse"
    child_modes[2] = "new_success_v4"
    child_saved = _save_v4_checkpoint(
        tmp_path,
        child,
        modes=child_modes,
        parent_run_id=parent["run_id"],
        parent_admissions=1,
    )

    child_archive = load_annual_report(tmp_path, child["run_id"])
    parent_contract = parent_archive.report["model_discovery"]["execution_contract"]
    child_contract = child_archive.report["model_discovery"]["execution_contract"]

    assert parent_contract["kind"] == child_contract["kind"] == "fintrace_annual_discovery_execution_v4"
    assert parent_contract["phase_budget"]["scope"] == "new_run"
    assert child_contract["phase_budget"]["scope"] == "resume"
    assert child_contract["parent_run_id"] == parent["run_id"]
    assert child_contract["reused_batches"] == [1]
    child_findings = child_archive.report["model_discovery"]["findings"]
    assert all(finding in child_findings for finding in parent_saved["report"]["model_discovery"]["findings"])
    assert len(child_findings) == 4
    assert parent_saved["snapshot"].sequence == child_saved["snapshot"].sequence == 1


def test_v4_reader_rejects_unknown_parent_execution_kind(tmp_path: Path) -> None:
    parent = _write_run(tmp_path, 185)
    modes = _unattempted_modes(parent["context"])
    modes[1] = "success_parent"
    parent_saved = _save_v4_checkpoint(tmp_path, parent, modes=modes)
    child = _write_run(tmp_path, 186)
    _share_source(parent, child)
    child["parent_findings"] = parent_saved["report"]["model_discovery"]["findings"]
    child_modes = _unattempted_modes(child["context"])
    child_modes.update({1: "reuse", 2: "new_success_v4"})
    _save_v4_checkpoint(
        tmp_path, child, modes=child_modes,
        parent_run_id=parent["run_id"], parent_admissions=1,
    )

    _rewrite_checkpoint_report(
        tmp_path,
        parent,
        lambda report: report["model_discovery"]["execution_contract"].update(
            kind="fintrace_annual_discovery_execution_v9"
        ),
    )

    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, child["run_id"])
    assert error.value.code == "annual_discovery_parent_invalid"


def test_v4_reader_rejects_cross_version_parent_child_resume(tmp_path: Path) -> None:
    parent = _write_run(tmp_path, 187)
    parent_modes = _unattempted_modes(parent["context"])
    parent_modes[1] = "success_parent"
    parent_saved = _save_v4_checkpoint(tmp_path, parent, modes=parent_modes)
    child = _write_run(tmp_path, 188)
    _share_source(parent, child)
    child["parent_findings"] = parent_saved["report"]["model_discovery"]["findings"]
    child_modes = _unattempted_modes(child["context"])
    child_modes.update({1: "new_success", 3: "reuse"})
    _save_checkpoint(
        tmp_path, child, modes=child_modes,
        parent_run_id=parent["run_id"], parent_admissions=1,
    )

    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, child["run_id"])
    assert error.value.code == "annual_discovery_parent_invalid"


def test_v4_reader_rejects_parent_binding_mismatch(tmp_path: Path) -> None:
    parent = _write_run(tmp_path, 189)
    parent_modes = _unattempted_modes(parent["context"])
    parent_modes[1] = "success_parent"
    parent_saved = _save_v4_checkpoint(tmp_path, parent, modes=parent_modes)
    child = _write_run(tmp_path, 190)
    _share_source(parent, child)
    child["parent_findings"] = parent_saved["report"]["model_discovery"]["findings"]
    child_modes = _unattempted_modes(child["context"])
    child_modes[1] = "reuse"
    child_modes[2] = "new_success_v4"
    _save_v4_checkpoint(
        tmp_path,
        child,
        modes=child_modes,
        parent_run_id=parent["run_id"],
        parent_admissions=1,
        binding_model="different-fixture-model",
    )

    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, child["run_id"])
    assert error.value.code == "annual_discovery_parent_invalid"


@pytest.mark.parametrize("empty_response", [False, True])
def test_parent_findings_survive_child_retry_failure_or_legal_empty_response(
    tmp_path: Path,
    empty_response: bool,
) -> None:
    parent = _write_run(tmp_path, 111)
    modes = _unattempted_modes(parent["context"])
    modes.update({1: "partial_parent", 3: "success_parent"})
    _save_checkpoint(tmp_path, parent, modes=modes, state="completed_with_issues", is_partial=False)
    child = _write_run(tmp_path, 112)
    _share_source(parent, child)
    parent_archive = load_annual_report(tmp_path, parent["run_id"])
    child["parent_findings"] = parent_archive.report["model_discovery"]["findings"]
    child_modes = _unattempted_modes(child["context"])
    child_modes.update({1: "empty_response" if empty_response else "all_failed", 3: "reuse"})
    _save_checkpoint(
        tmp_path,
        child,
        modes=child_modes,
        parent_run_id=parent["run_id"],
        parent_admissions=2,
        state="running",
        is_partial=True,
        empty_response=empty_response,
    )

    archive = load_annual_report(tmp_path, child["run_id"])

    assert archive.snapshot["is_partial"] is True
    assert len(archive.report["model_discovery"]["findings"]) == 7
    assert archive.report["model_discovery"]["coverage"]["batch_results"][0]["accepted_findings"] == 5
    attempts = archive.report["model_discovery"]["execution_contract"]["batches"][0]["attempts"]
    if empty_response:
        assert attempts[0]["response_accepted"] is True
        assert attempts[0]["accepted_findings"] == 0
        assert archive.report["model_discovery"]["model_call_count"] == 1
    else:
        assert len(attempts) == 2
        assert attempts[0]["audit_id"] != attempts[1]["audit_id"]
        assert archive.report["model_discovery"]["model_call_count"] == 0


@pytest.mark.parametrize("contract_version", ["v3", "v4"])
@pytest.mark.parametrize("tamper", ["source_pdf", "parent_quote"])
def test_child_load_revalidates_immutable_parent_source_and_quotes(
    tmp_path: Path, tamper: str, contract_version: str,
) -> None:
    parent = _write_run(tmp_path, 161)
    parent_modes = _unattempted_modes(parent["context"])
    if contract_version == "v4":
        parent_modes.update({1: "success_parent", 3: "success_parent"})
        parent_saved = _save_v4_checkpoint(
            tmp_path, parent, modes=parent_modes,
            state="completed_with_issues", is_partial=False,
        )
        parent_admissions = 2
    else:
        parent_modes.update({1: "partial_parent", 3: "success_parent"})
        parent_saved = _save_checkpoint(
            tmp_path, parent, modes=parent_modes,
            state="completed_with_issues", is_partial=False,
        )
        parent_admissions = 2
        with pytest.raises(AnnualCheckpointError) as sealed:
            save_annual_checkpoint(tmp_path, parent["run_id"], {
                "state": "running", "is_partial": True, "parent_run_id": None,
                "source_sha256": parent["source_sha"], "report": parent_saved["report"],
            })
        assert getattr(sealed.value, "code", None) == "checkpoint_already_sealed"

    child = _write_run(tmp_path, 162)
    _share_source(parent, child)
    child["parent_findings"] = parent_saved["report"]["model_discovery"]["findings"]
    child_modes = _unattempted_modes(child["context"])
    if contract_version == "v4":
        child_modes.update({1: "reuse", 2: "new_success_v4", 3: "reuse"})
        _save_v4_checkpoint(
            tmp_path, child, modes=child_modes,
            parent_run_id=parent["run_id"], parent_admissions=parent_admissions,
        )
    else:
        child_modes.update({1: "new_success", 3: "reuse"})
        _save_checkpoint(
            tmp_path, child, modes=child_modes,
            parent_run_id=parent["run_id"], parent_admissions=parent_admissions,
        )

    if tamper == "source_pdf":
        source_path = parent["run_dir"] / "source.pdf"
        source_path.write_bytes(source_path.read_bytes() + b"parent source changed")
    else:
        pointer_path = parent["run_dir"] / "checkpoint-current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        snapshot_path = tmp_path / pointer["snapshot_path"]
        envelope = json.loads(snapshot_path.read_text(encoding="utf-8"))
        tampered_report = envelope["report"]
        tampered_report["model_discovery"]["findings"][0]["citations"][0]["quote"] = "not present in the source PDF"
        report_bytes = json.dumps(tampered_report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
        snapshot_bytes = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        pointer["report_sha256"] = envelope["report_sha256"]
        pointer["snapshot_sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()
        snapshot_path.write_bytes(snapshot_bytes)
        pointer_path.write_text(json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        _write_json(parent["report_dir"] / "report.json", tampered_report)

    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, child["run_id"])
    assert error.value.code == "annual_discovery_parent_invalid"


def test_v3_parent_lineage_depth_is_bounded(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 163)
    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, run["run_id"], _lineage=("parent-placeholder",) * 32)
    assert error.value.code == "annual_discovery_parent_invalid"


def test_legacy_v3_checkpoint_remains_readable_but_cannot_resume_under_v4_identity(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 164)
    request = {
        "company_id": _COMPANY,
        "report_year": _YEAR,
        "document_id": _DOCUMENT,
        "source_pdf_path": f"{_COMPANY}/{_YEAR}/{_DOCUMENT}/fixture.pdf",
        "mode": _MODE,
        "sha256": run["source_sha"],
    }
    raw_dir = tmp_path / "data" / "raw" / _COMPANY / str(_YEAR) / _DOCUMENT
    raw_dir.mkdir(parents=True)
    (raw_dir / "fixture.pdf").write_bytes((run["run_dir"] / "source.pdf").read_bytes())
    _write_json(raw_dir / "source.json", {
        "company_id": _COMPANY,
        "document_id": _DOCUMENT,
        "report_period": "2024-12-31",
        "sha256": run["source_sha"],
    })
    settings = ModelSettings(
        base_url="https://fixture.invalid/v1",
        api_key="constructed-test-only",
        model="local-fixture-model",
        timeout_seconds=1.0,
    )
    report = copy.deepcopy(run["report"])
    report["model_discovery"] = _discovery(run)
    # This fixture represents an archived execution_v3 payload. Construct its
    # original v3 binding explicitly instead of rebuilding it with the current
    # v4 plan/parser, which must remain incompatible for resume.
    binding = _binding(run, _plan_sha(run["context"]))
    contract = report["model_discovery"]["execution_contract"]
    contract["plan_sha256"] = binding["plan_sha256"]
    contract["identity_sha256"] = recovery_identity_sha256(binding)
    contract["binding"] = binding
    running = save_annual_checkpoint(tmp_path, run["run_id"], {
        "state": "running", "is_partial": True, "parent_run_id": None,
        "source_sha256": run["source_sha"], "report": report,
    })
    _write_json(run["report_dir"] / "report.json", report)
    run["manifest"]["status"] = "completed"
    _write_json(run["run_dir"] / "manifest.json", run["manifest"])

    first_archive = load_annual_report(tmp_path, run["run_id"])
    assert first_archive.snapshot is not None and first_archive.snapshot["is_partial"] is True
    assert first_archive.manifest_summary["status"] == "completed"
    resume_check = validate_annual_recovery_parent(
        tmp_path, run["run_id"], request, settings=settings, archive=first_archive,
    )
    assert resume_check["can_resume"] is False
    assert resume_check["reason"] == "recovery_binding_mismatch"

    interrupted = save_annual_checkpoint(tmp_path, run["run_id"], {
        "state": "interrupted", "is_partial": True, "parent_run_id": None,
        "source_sha256": run["source_sha"], "report": report,
    })
    second_archive = load_annual_report(tmp_path, run["run_id"])
    assert second_archive.snapshot == {
        "state": "interrupted", "sequence": interrupted.sequence,
        "is_partial": True, "parent_run_id": None,
    }
    assert second_archive.manifest_summary["status"] == "completed"
    resume_check = validate_annual_recovery_parent(
        tmp_path, run["run_id"], request, settings=settings, archive=second_archive,
    )
    assert resume_check["can_resume"] is False
    assert resume_check["reason"] == "recovery_binding_mismatch"
    assert running.sequence + 1 == interrupted.sequence


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (lambda report: report["model_discovery"]["execution_contract"].update(extra=True), "annual_discovery_invalid"),
        (lambda report: report["model_discovery"]["execution_contract"]["batches"][0]["attempts"][0].update(audit_id="bad"), "annual_discovery_invalid"),
        (lambda report: report["model_discovery"]["findings"][0]["citations"][0].update(quote="not in source"), "annual_discovery_quote_mismatch"),
    ],
)
def test_v3_unknown_fields_ids_and_invalid_quotes_fail_closed(
    tmp_path: Path,
    mutator: Any,
    expected_code: str,
) -> None:
    run = _write_run(tmp_path, 121)
    modes = _unattempted_modes(run["context"])
    modes[1] = "new_success"
    saved = _save_checkpoint(tmp_path, run, modes=modes)
    report = copy.deepcopy(saved["report"])
    mutator(report)
    # Re-publishing through the storage helper binds the altered report hash;
    # the strict reader must still reject its v3 shape/quote semantics.
    save_annual_checkpoint(tmp_path, run["run_id"], {
        "state": "running", "is_partial": True, "parent_run_id": None,
        "source_sha256": run["source_sha"], "report": report,
    })
    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, run["run_id"])
    assert error.value.code == expected_code


def test_source_hash_and_checkpoint_tamper_are_rejected(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 131)
    _save_checkpoint(tmp_path, run)
    source_path = run["run_dir"] / "source.pdf"
    source_path.write_bytes(source_path.read_bytes() + b"tamper")
    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, run["run_id"])
    assert error.value.code == "source_sha256_mismatch"


def test_cancelled_checkpoint_requires_interrupted_manifest(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 141)
    _save_checkpoint(tmp_path, run, state="cancelled")
    run["manifest"]["status"] = "interrupted"
    _write_json(run["run_dir"] / "manifest.json", run["manifest"])
    archive = load_annual_report(tmp_path, run["run_id"])
    assert archive.snapshot["state"] == "cancelled"
    assert archive.snapshot["is_partial"] is True

    run["manifest"]["status"] = "failed"
    _write_json(run["run_dir"] / "manifest.json", run["manifest"])
    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, run["run_id"])
    assert error.value.code == "checkpoint_state_mismatch"


def test_two_file_terminal_publication_windows_remain_readable_and_do_not_invent_a_final_seal(
    tmp_path: Path,
) -> None:
    run = _write_run(tmp_path, 146)
    initial = _save_checkpoint(tmp_path, run)

    # Simulate a force-cancel process stopping after checkpoint publication but
    # before the manifest's running -> interrupted replacement.
    cancelled = save_annual_checkpoint(tmp_path, run["run_id"], {
        "state": "cancelled", "is_partial": True, "parent_run_id": None,
        "source_sha256": run["source_sha"], "report": initial["report"],
    })
    mid_cancel = load_annual_report(tmp_path, run["run_id"])
    assert mid_cancel.snapshot == {
        "state": "cancelled", "sequence": cancelled.sequence,
        "is_partial": True, "parent_run_id": None,
    }
    assert mid_cancel.manifest_summary["status"] == "running"
    run["manifest"]["status"] = "interrupted"
    _write_json(run["run_dir"] / "manifest.json", run["manifest"])
    assert load_annual_report(tmp_path, run["run_id"]).snapshot["state"] == "cancelled"
    job_id = "annual-job-" + "1" * 32
    job_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / job_id
    job_dir.mkdir(parents=True)
    _write_json(job_dir / "job.json", {
        "job_id": job_id,
        "status": "cancelled",
        "run_id": run["run_id"],
        "request": {
            "company_id": _COMPANY,
            "report_year": _YEAR,
            "document_id": _DOCUMENT,
            "source_pdf_path": f"{_COMPANY}/{_YEAR}/{_DOCUMENT}/fixture.pdf",
            "mode": _MODE,
            "sha256": run["source_sha"],
        },
    })
    cancelled_row = next(
        item for item in list_annual_analysis_history(tmp_path)["items"]
        if item["job_id"] == job_id
    )
    assert cancelled_row["job_status"] == "cancelled"
    assert cancelled_row["result_available"] is True
    assert cancelled_row["partial_checkpoint_available"] is True
    assert cancelled_row["snapshot_state"] == "cancelled"
    assert "job_cancelled" in {issue["code"] for issue in cancelled_row["issues"]}

    # Simulate CLI's report.json + terminal manifest becoming durable before
    # the final immutable checkpoint pointer. The reader must return the old
    # verified partial, not the unsealed candidate report.
    cli_run = _write_run(tmp_path, 150)
    initial = _save_checkpoint(tmp_path, cli_run)
    final_candidate = copy.deepcopy(cli_run["report"])
    final_candidate["model_discovery"] = _discovery(
        cli_run,
        batch_modes={batch.batch_index: "success_parent" for batch in cli_run["context"].batches},
    )
    _write_json(cli_run["report_dir"] / "report.json", final_candidate)
    cli_run["manifest"]["status"] = "completed"
    _write_json(cli_run["run_dir"] / "manifest.json", cli_run["manifest"])
    mid_final = load_annual_report(tmp_path, cli_run["run_id"])
    assert mid_final.snapshot == {
        "state": "running", "sequence": initial["snapshot"].sequence,
        "is_partial": True, "parent_run_id": None,
    }
    assert mid_final.manifest_summary["status"] == "completed"
    assert mid_final.report == initial["report"]
    # Restart coordinator stops the orphaned finalizing run by appending an
    # interrupted pointer. The completed manifest remains untouched and the
    # partial v3 snapshot stays readable for a later child resume.
    restarted = save_annual_checkpoint(tmp_path, cli_run["run_id"], {
        "state": "interrupted", "is_partial": True, "parent_run_id": None,
        "source_sha256": cli_run["source_sha"], "report": initial["report"],
    })
    after_restart = load_annual_report(tmp_path, cli_run["run_id"])
    assert after_restart.snapshot == {
        "state": "interrupted", "sequence": restarted.sequence,
        "is_partial": True, "parent_run_id": None,
    }
    assert after_restart.manifest_summary["status"] == "completed"
    assert after_restart.report == initial["report"]
    cli_history_row = next(
        item for item in list_annual_analysis_history(tmp_path)["items"]
        if item["run_id"] == cli_run["run_id"]
    )
    assert cli_history_row["run_status"] == "completed"
    assert cli_history_row["result_available"] is True
    assert cli_history_row["partial_checkpoint_available"] is True
    assert cli_history_row["snapshot_state"] == "interrupted"


def test_final_checkpoint_seal_can_precede_the_terminal_manifest_update(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 147)
    modes = {batch.batch_index: "success_parent" for batch in run["context"].batches}
    report = copy.deepcopy(run["report"])
    report["model_discovery"] = _discovery(run, batch_modes=modes)
    _write_json(run["report_dir"] / "report.json", report)
    sealed = save_annual_checkpoint(tmp_path, run["run_id"], {
        "state": "completed", "is_partial": False, "parent_run_id": None,
        "source_sha256": run["source_sha"], "report": report,
    })

    archive = load_annual_report(tmp_path, run["run_id"])

    assert archive.snapshot == {
        "state": "completed", "sequence": sealed.sequence,
        "is_partial": False, "parent_run_id": None,
    }
    assert archive.manifest_summary["status"] == "running"
    assert archive.report == report


def _integrated_m3_fixture(run: MappingLike) -> dict[str, Any]:
    run["mode"] = "integrated_analysis"
    run["manifest"]["mode"] = "integrated_analysis"
    _write_json(run["run_dir"] / "manifest.json", run["manifest"])
    run["report"]["m3_screening"] = {
        "kind": "fintrace_m3_annual_screening",
        "run_id": run["run_id"],
        "rule_version": "m3-rule-fixture-v1",
        "extraction": {
            "facts": [{
                "fact_id": "fact-random-1001",
                "metric_id": "revenue",
                "source_document_id": _DOCUMENT,
                "source_sha256": run["source_sha"],
                "company_id": _COMPANY,
                "report_year": _YEAR,
                "period_start": "2024-01-01",
                "period_end": "2024-12-31",
                "period_type": "annual",
                "frequency": "annual",
                "comparison_role": "current",
                "raw_value": "1,200",
                "normalized_value": "1200",
                "unit_multiplier": 1,
                "unit": "CNY",
                "currency": "CNY",
                "scope": "consolidated",
                "statement_type": "income_statement",
                "restatement_status": "not_restated",
                "verification_id": "volatile-verification-id",
            }],
        },
        "explicit_unit_facts": {"facts": []},
    }
    return run["report"]["m3_screening"]


def test_integrated_m3_binding_matches_execution_projection_and_tracks_financial_inputs(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 148, mode="integrated_analysis")
    m3_screening = _integrated_m3_fixture(run)
    binding = _binding(run, _plan_sha(run["context"]))
    request = {
        "sha256": run["source_sha"], "company_id": _COMPANY,
        "report_year": _YEAR, "document_id": _DOCUMENT,
        "mode": "integrated_analysis",
    }

    execution_hash = _execution_m3_semantic_binding_sha256(m3_screening, request)
    assert binding["m3_binding_sha256"] == execution_hash
    assert _reader_m3_semantic_binding_sha256(m3_screening, binding) == execution_hash
    changed_rule_projection = copy.deepcopy(m3_screening)
    changed_rule_projection["rule_version"] = "m3-rule-fixture-v2"
    assert _execution_m3_semantic_binding_sha256(changed_rule_projection, request) != execution_hash
    assert _reader_m3_semantic_binding_sha256(changed_rule_projection, binding) != execution_hash

    modes = _unattempted_modes(run["context"])
    modes[1] = "new_success"
    saved = _save_checkpoint(tmp_path, run, modes=modes)
    archive = load_annual_report(tmp_path, run["run_id"])
    assert archive.report["model_discovery"]["execution_contract"]["binding"]["m3_binding_sha256"] == execution_hash

    # Incidental archive IDs do not affect the semantic binding.
    stable_report = copy.deepcopy(saved["report"])
    stable_report["m3_screening"]["run_id"] = "annual-analysis-random-run-label"
    stable_report["m3_screening"]["extraction"]["facts"][0]["fact_id"] = "fact-random-9002"
    save_annual_checkpoint(tmp_path, run["run_id"], {
        "state": "running", "is_partial": True, "parent_run_id": None,
        "source_sha256": run["source_sha"], "report": stable_report,
    })
    assert load_annual_report(tmp_path, run["run_id"]).report["m3_screening"]["run_id"] == "annual-analysis-random-run-label"


@pytest.mark.parametrize("field", ["raw_value", "normalized_value", "unit_multiplier"])
def test_integrated_m3_financial_input_tampering_is_rejected(tmp_path: Path, field: str) -> None:
    run = _write_run(tmp_path, 149, mode="integrated_analysis")
    _integrated_m3_fixture(run)
    modes = _unattempted_modes(run["context"])
    modes[1] = "new_success"
    saved = _save_checkpoint(tmp_path, run, modes=modes)
    tampered = copy.deepcopy(saved["report"])
    fact = tampered["m3_screening"]["extraction"]["facts"][0]
    fact[field] = {"raw_value": "1,201", "normalized_value": "1201", "unit_multiplier": 1000}[field]
    save_annual_checkpoint(tmp_path, run["run_id"], {
        "state": "running", "is_partial": True, "parent_run_id": None,
        "source_sha256": run["source_sha"], "report": tampered,
    })
    with pytest.raises(AnnualReportReadError) as error:
        load_annual_report(tmp_path, run["run_id"])
    assert error.value.code == "annual_discovery_binding_mismatch"


def test_history_and_three_exports_share_one_strict_snapshot_and_omit_quotes_and_audit_ids(tmp_path: Path) -> None:
    run = _write_run(tmp_path, 151)
    modes = _unattempted_modes(run["context"])
    modes[1] = "new_success"
    saved = _save_checkpoint(tmp_path, run, modes=modes)
    expected_snapshot = saved["snapshot"].metadata

    exports = [export_annual_report(tmp_path, run["run_id"], kind) for kind in ("json", "markdown", "html")]

    assert all(item.snapshot == expected_snapshot for item in exports)
    json_text = exports[0].content.decode("utf-8")
    markdown_text = exports[1].content.decode("utf-8")
    html_text = exports[2].content.decode("utf-8")
    raw_quote = saved["report"]["model_discovery"]["findings"][0]["citations"][0]["quote"]
    audit_ids = [
        attempt["audit_id"]
        for batch in saved["report"]["model_discovery"]["execution_contract"]["batches"]
        for attempt in batch["attempts"]
    ]
    assert raw_quote not in json_text and raw_quote not in markdown_text and raw_quote not in html_text
    assert all(audit_id not in json_text + markdown_text + html_text for audit_id in audit_ids)
    assert "api_key" not in json_text and "api_key" not in markdown_text and "api_key" not in html_text
    assert '"snapshot"' in json_text
    assert "已结束批次" in markdown_text and "检查点序号" in html_text

    history = list_annual_analysis_history(tmp_path)
    row = next(item for item in history["items"] if item["run_id"] == run["run_id"])
    assert row["result_available"] is True
    assert row["partial_checkpoint_available"] is True
    assert row["snapshot_sequence"] == expected_snapshot["sequence"]
    assert row["snapshot_state"] == expected_snapshot["state"]
