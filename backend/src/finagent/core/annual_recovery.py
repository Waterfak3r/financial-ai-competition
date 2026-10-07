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
from finagent.retrieval.annual_discovery_context import build_annual_discovery_context

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODES = {"model_investigation", "integrated_analysis"}
_PROMPTS = (
    "annual_discovery_v2.md",
    "annual_investigation_v1.md",
    "annual_review_v1.md",
    "annual_review_v2.md",
)
_PARSER_VERSION = "annual-discovery-parser-v2-execution-v3"


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
) -> dict[str, Any]:
    """Strictly validate a saved parent and return its current binding.

    ``request`` is the already validated, normalized four-mode job request.
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
        )
        layer = archive.report.get("model_discovery")
        if not isinstance(layer, Mapping):
            return {"can_resume": False, "reason": "recovery_discovery_missing", "binding": None}
        contract = layer.get("execution_contract")
        if not isinstance(contract, Mapping):
            return {"can_resume": False, "reason": "recovery_binding_missing", "binding": None}
        if contract.get("binding") != binding:
            return {"can_resume": False, "reason": "recovery_binding_mismatch", "binding": binding}
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
        unfinished = isinstance(batches, list) and any(
            isinstance(row, Mapping) and row.get("state") != "succeeded" for row in batches
        )
        if not unfinished:
            review = archive.report.get("model_review")
            unfinished = not isinstance(review, Mapping) or review.get("status") != "completed"
        if not unfinished:
            investigation = archive.report.get("model_investigation")
            unfinished = _investigation_has_unfinished_items(investigation)
        if not unfinished:
            return {"can_resume": False, "reason": "recovery_nothing_to_resume", "binding": binding}
        return {"can_resume": True, "reason": None, "binding": binding}
    except AnnualReportReadError as exc:
        return {
            "can_resume": False,
            "reason": "recovery_parent_invalid" if exc.code else "recovery_parent_invalid",
            "binding": None,
        }
    except (ModelConfigError, ModelCallAdmissionError, OSError, ValueError, KeyError, TypeError):
        return {"can_resume": False, "reason": "recovery_binding_unavailable", "binding": None}


def build_annual_recovery_binding(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the exact semantic identity used by CLI and job capability checks."""

    mode = request.get("mode")
    if mode not in _MODES:
        raise ValueError("recovery_mode_invalid")
    source_sha = str(request.get("sha256", "")).lower()
    if _SHA256.fullmatch(source_sha) is None:
        raise ValueError("recovery_source_invalid")
    source_record = _source_record(Path(project_root), request)
    prompt_path = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_discovery_v2.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    context = build_annual_discovery_context(
        report,
        _source_pdf_path(Path(project_root), request),
        source_record,
        system_prompt=prompt,
        model_name=settings.model,
    )
    plan_projection = [
        {
            "batch_index": batch.batch_index,
            "fragments": [
                {
                    "evidence_id": item.evidence_id,
                    "pdf_page": item.pdf_page,
                    "block_index": item.block_index,
                    "start_char": item.start_char,
                    "char_count": item.char_count,
                }
                for item in batch.fragments
            ],
        }
        for batch in context.batches
    ]
    plan_sha256 = _sha256_json(plan_projection)
    m3_binding_sha256 = (
        _m3_semantic_binding_sha256(report.get("m3_screening"), request)
        if mode == "integrated_analysis"
        else None
    )
    binding = {
        "source_sha256": source_sha,
        "company_id": request["company_id"],
        "report_year": request["report_year"],
        "source_document_id": request["document_id"],
        "analysis_mode": mode,
        "provider_model_fingerprint": provider_model_fingerprint(settings.base_url, settings.model),
        "model": settings.model,
        "prompts_sha256": _prompts_sha256(),
        "parser_version": _PARSER_VERSION,
        "plan_sha256": plan_sha256,
        "m3_binding_sha256": m3_binding_sha256,
    }
    return binding


def recovery_identity_sha256(binding: Mapping[str, Any]) -> str:
    return _sha256_json(binding)


def annual_discovery_plan_sha256(
    project_root: Path,
    request: Mapping[str, Any],
    *,
    settings: ModelSettings,
    report: Mapping[str, Any],
) -> str:
    return str(build_annual_recovery_binding(
        project_root, request, settings=settings, report=report
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


def _prompts_sha256() -> str:
    root = Path(__file__).resolve().parents[4]
    material: dict[str, str] = {}
    for name in _PROMPTS:
        content = (root / "config" / "prompts" / name).read_text(encoding="utf-8")
        material[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
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
