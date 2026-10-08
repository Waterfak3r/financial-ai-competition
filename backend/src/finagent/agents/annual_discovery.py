"""Open-ended, source-bound discovery of unverified annual-report leads.

This workflow is independent of fixed FINTRACE candidates and confirmed facts.
It scans bounded batches of all extractable text from one original PDF, checks
every model citation against the exact submitted fragment, and never upgrades a
model observation into an independently verified fact or fraud conclusion.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Event
from typing import Literal, TypedDict

from finagent.audit import EvidenceRef, audited_complete_chat
from finagent.audit.audited_chat import repository_runs_root
from finagent.core.annual_work_budget import (
    public_budget_contract,
    DISCOVERY_BUDGET_SECONDS,
)
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage, ModelResponseError
from finagent.retrieval.annual_discovery_context import (
    COMPACT_PLAN_VERSION,
    LEGACY_PLAN_VERSION,
    MAX_BATCHES,
    MAX_SERIALIZED_INPUT_CHARS,
    AnnualDiscoveryContext,
    AnnualDiscoveryInputError,
    AnnualDiscoverySourceError,
    DiscoveryBatch,
    DiscoveryFragment,
    build_annual_discovery_context,
    annual_discovery_plan_projection,
    coverage_is_complete,
    empty_coverage,
    serialize_batch_content,
    serialized_request_size,
    validate_annual_discovery_identity,
)

PROMPT_VERSION = "annual_discovery_v3"
LEGACY_PROMPT_VERSION = "annual_discovery_v2"
SUPPORTED_PROMPT_VERSIONS = frozenset({"annual_discovery_v1", "annual_discovery_v2", "annual_discovery_v3"})
MAX_FINDINGS_PER_BATCH = 6
MAX_FINDINGS_PER_BATCH_V3 = 3
MAX_CITATIONS_PER_FINDING = 4
MAX_QUOTE_CHARS = 300
MAX_MODEL_RESPONSE_CHARS = 80000
MAX_WORKFLOW_SECONDS = 1800.0
DEFAULT_MAX_CONCURRENCY = 3
MAX_CONCURRENCY = 3
CONCURRENCY_ENV = "FINTRACE_DISCOVERY_CONCURRENCY"
_monotonic_clock = time.monotonic
_PROMPT_ROOT = Path(__file__).resolve().parents[4] / "config" / "prompts"
_DIGIT_RE = re.compile(r"[0-9]")
_RUN_REPARSE_POINT = 0x400


class LangGraphUnavailableError(RuntimeError):
    """Raised when the optional LangGraph dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class AuditArtifact:
    batch_index: int
    audit_dir: str | None
    request_path: str | None
    response_path: str | None
    failure_path: str | None
    status: str
    prompt_version: str

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_index": self.batch_index,
            "audit_dir": self.audit_dir,
            "request_path": self.request_path,
            "response_path": self.response_path,
            "failure_path": self.failure_path,
            "status": self.status,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True, slots=True)
class AnnualDiscoveryResult:
    run_id: str
    company_id: str
    report_year: int
    source_document_id: str
    source_sha256: str
    status: Literal["completed", "partial", "failed", "not_called"]
    prompt_version: str
    model_called: bool
    model_call_count: int
    model_call_attempt_count: int
    coverage: Mapping[str, object]
    findings: tuple[Mapping[str, object], ...]
    evidences: tuple[Mapping[str, object], ...]
    audit_artifacts: tuple[AuditArtifact, ...]
    limitations: tuple[str, ...]
    reason: str | None
    execution_contract: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": "fintrace_annual_discovery",
            "run_id": self.run_id,
            "company_id": self.company_id,
            "report_year": self.report_year,
            "source_document_id": self.source_document_id,
            "source_sha256": self.source_sha256,
            "status": self.status,
            "prompt_version": self.prompt_version,
            "model_called": self.model_called,
            "model_call_count": self.model_call_count,
            "model_call_attempt_count": self.model_call_attempt_count,
            "coverage": dict(self.coverage),
            "findings": [dict(item) for item in self.findings],
            "evidences": [dict(item) for item in self.evidences],
            "audit_artifacts": [item.to_dict() for item in self.audit_artifacts],
            "limitations": list(self.limitations),
            "reason": self.reason,
            "fraud_conclusion": None,
        }
        if self.execution_contract is not None:
            value["execution_contract"] = dict(self.execution_contract)
        return value


@dataclass(frozen=True, slots=True)
class _ParsedFinding:
    title: str
    observation: str
    why_it_matters: str
    follow_up: str
    priority: str
    citations: tuple[tuple[str, str], ...]
    restored_quotes: int = 0

    def duplicate_key(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "observation": self.observation,
                "why_it_matters": self.why_it_matters,
                "follow_up": self.follow_up,
                "priority": self.priority,
                "citations": list(self.citations),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class AnnualDiscoveryState(TypedDict, total=False):
    report: Mapping[str, object]
    source_pdf_path: str
    source_record: Mapping[str, object]
    settings: ModelSettings
    max_concurrency: int
    run_dir: str
    identity: Mapping[str, object]
    prompt_text: str
    prompt_version: str
    parsed_pdf: object
    progress_callback: Callable[[dict[str, object]], None] | None
    deadline: float
    discovery_deadline: float
    discovery_started_at: float
    phase_budget: Mapping[str, object]
    main_soft_deadline: float | None
    context: AnnualDiscoveryContext | None
    batches: list[DiscoveryBatch]
    fragments_by_id: dict[str, DiscoveryFragment]
    current_index: int
    current_batch: DiscoveryBatch | None
    response_text: str
    call_succeeded: bool
    failure_code: str | None
    model_call_count: int
    model_call_attempt_count: int
    processed_batches: int
    successful_batches: int
    partial_batches: int
    failed_batches: int
    processed_chars: int
    attempted_chars: int
    findings: list[dict[str, object]]
    finding_keys: list[str]
    limitations: list[str]
    audit_artifacts: list[AuditArtifact]
    coverage: dict[str, object]
    reason: str | None
    overall_status: str
    progress_warning: bool
    not_called_reason: str | None
    budget_exhausted: bool
    execution_binding: Mapping[str, object]
    parent_discovery: Mapping[str, object] | None
    parent_provider_admission_count: int
    checkpoint_callback: Callable[[dict[str, object]], None] | None
    cancel_check: Callable[[], bool] | None
    execution_contract: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AnnualDiscoveryParseResult:
    findings: tuple[_ParsedFinding, ...]
    limitations: tuple[str, ...]
    rejected_findings: int
    restored_quotes: int


@dataclass(frozen=True, slots=True)
class _BatchCallOutcome:
    batch: DiscoveryBatch
    response_text: str
    call_succeeded: bool
    failure_code: str | None
    attempted: bool
    response_returned: bool
    audit_artifact: AuditArtifact | None
    completed_at: float
    unattempted: bool = False
    attempt_records: tuple[Mapping[str, object], ...] = ()


class _OrderedCallGate:
    """Keep admission ordered while allowing admitted calls to overlap afterward."""

    def __init__(self, first_batch_index: int = 1) -> None:
        self._condition = Condition()
        self._next_batch_index = first_batch_index
        self._closed = False

    def enter(self, batch_index: int, deadline: float) -> float | None:
        with self._condition:
            while batch_index != self._next_batch_index and not self._closed:
                self._condition.wait()
            if self._closed:
                return None
            remaining = float(deadline) - _monotonic_clock()
            if remaining <= 0:
                self._closed = True
                self._condition.notify_all()
                return None
            self._next_batch_index += 1
            self._condition.notify_all()
            return remaining

    def skip(self, batch_index: int, deadline: float | None = None) -> bool:
        """Advance the ordered gate over a strictly verified reused batch."""

        with self._condition:
            while batch_index != self._next_batch_index and not self._closed:
                remaining = None if deadline is None else float(deadline) - _monotonic_clock()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            if self._closed or batch_index != self._next_batch_index:
                return False
            self._next_batch_index += 1
            self._condition.notify_all()
            return True

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

_SAFE_CONTAINER_FAILURES = frozenset(
    {
        "invalid_model_json",
        "model_response_too_large",
        "invalid_model_schema",
        "findings_invalid",
        "limitations_invalid",
        "all_findings_rejected",
        "invalid_model_response",
        "limitation_invalid",
        "limitation_contains_unquoted_digit",
        "limitation_contains_control_character",
        "definitive_conclusion_forbidden",
    }
)

_SAFE_BATCH_FAILURES = frozenset(
    {
        *_SAFE_CONTAINER_FAILURES,
        "batch_missing",
        "model_call_failed",
        "model_call_timeout",
        "serialized_input_limit_breached",
    }
)


def run_annual_discovery(
    report: Mapping[str, object],
    source_pdf_path: str | Path,
    source_record: Mapping[str, object],
    settings: ModelSettings,
    *,
    run_dir: Path,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    max_concurrency: int | None = None,
    execution_binding: Mapping[str, object] | None = None,
    parent_discovery: Mapping[str, object] | None = None,
    parent_provider_admission_count: int = 0,
    checkpoint_callback: Callable[[dict[str, object]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    parsed_pdf: object | None = None,
    main_soft_deadline: float | None = None,
    discovery_deadline: float | None = None,
) -> AnnualDiscoveryResult:
    """Scan one source-bound annual PDF for open-ended unverified leads.

    Settings are supplied by the caller. This entry point does not read .env,
    contact a model until a planned batch is processed, or mutate the report.
    The supplied run directory must be the exact repository run for report.run_id.
    """

    identity = validate_annual_discovery_identity(report, source_record)
    _validate_run_dir(Path(run_dir), str(identity["run_id"]))
    resolved_concurrency = _resolve_max_concurrency(max_concurrency)
    v4_execution = _is_v4_execution_binding(execution_binding)
    prompt_version = PROMPT_VERSION if v4_execution else LEGACY_PROMPT_VERSION
    prompt_text = _load_prompt(prompt_version)
    graph = build_annual_discovery_graph()
    plan_started = _monotonic_clock()
    discovery_stage_deadline = discovery_deadline
    if discovery_stage_deadline is None:
        discovery_stage_deadline = plan_started + (
            DISCOVERY_BUDGET_SECONDS if v4_execution else MAX_WORKFLOW_SECONDS
        )
    stage_deadline = discovery_stage_deadline
    if main_soft_deadline is not None:
        stage_deadline = min(stage_deadline, main_soft_deadline)
    state = graph.invoke(
        {
            "report": report,
            "source_pdf_path": str(Path(source_pdf_path)),
            "source_record": source_record,
            "settings": settings,
            "max_concurrency": resolved_concurrency,
            "run_dir": str(Path(run_dir).resolve()),
            "identity": identity,
            "prompt_text": prompt_text,
            "prompt_version": prompt_version,
            "parsed_pdf": parsed_pdf,
            "progress_callback": progress_callback,
            "execution_binding": dict(execution_binding) if execution_binding is not None else None,
            "parent_discovery": parent_discovery,
            "parent_provider_admission_count": parent_provider_admission_count,
            "checkpoint_callback": checkpoint_callback,
            "cancel_check": cancel_check,
            "deadline": stage_deadline,
            "discovery_deadline": discovery_stage_deadline,
            "main_soft_deadline": main_soft_deadline,
            "discovery_started_at": plan_started,
            "phase_budget": public_budget_contract(resumed=parent_discovery is not None),
            "current_index": 0,
            "model_call_count": 0,
            "model_call_attempt_count": 0,
            "processed_batches": 0,
            "successful_batches": 0,
            "partial_batches": 0,
            "failed_batches": 0,
            "processed_chars": 0,
            "attempted_chars": 0,
            "findings": [],
            "finding_keys": [],
            "limitations": [],
            "audit_artifacts": [],
            "progress_warning": False,
        },
        config={"recursion_limit": _recursion_limit(MAX_BATCHES)},
    )
    return _result_from_state(state)


def build_annual_discovery_initial_snapshot(
    report: Mapping[str, object],
    source_pdf_path: str | Path,
    source_record: Mapping[str, object],
    settings: ModelSettings,
    *,
    run_dir: Path,
    execution_binding: Mapping[str, object],
    parsed_pdf: object | None = None,
) -> dict[str, object]:
    """Build the source-bound zero-call v4 projection without dispatching a model."""

    discovery_started_at = _monotonic_clock()
    identity = validate_annual_discovery_identity(report, source_record)
    _validate_run_dir(Path(run_dir), str(identity["run_id"]))
    context = build_annual_discovery_context(
        report,
        source_pdf_path,
        source_record,
        system_prompt=_load_prompt(PROMPT_VERSION),
        model_name=settings.model,
        plan_version=COMPACT_PLAN_VERSION,
        parsed_pdf=parsed_pdf,
    )
    plan_projection = annual_discovery_plan_projection(context)
    plan_sha256 = hashlib.sha256(json.dumps(
        plan_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    if execution_binding.get("plan_sha256") != plan_sha256:
        raise ValueError("annual_discovery_binding_plan_mismatch")
    state: AnnualDiscoveryState = {
        "identity": identity,
        "limitations": [],
        "parsed_pdf": parsed_pdf,
        "discovery_started_at": discovery_started_at,
        "phase_budget": public_budget_contract(resumed=False),
    }
    rows, coverage_rows, existing_findings, limitations, audit_artifacts = (
        _initialize_v3_execution_projection(
            state,
            context,
            execution_binding,
            parent_discovery=None,
            parent_count=0,
            batches=list(context.batches),
        )
    )
    return _v4_discovery_snapshot(
        state,
        context,
        execution_binding,
        None,
        0,
        rows,
        coverage_rows,
        existing_findings,
        limitations,
        audit_artifacts,
        scheduler={
            "requested_max_concurrency": DEFAULT_MAX_CONCURRENCY,
            "effective_max_concurrency": DEFAULT_MAX_CONCURRENCY,
            "inflight_peak": 0,
            "checkpoint_coordinator_count": 1,
            "checkpoint_publications": 0,
            "dispatch_closed_reason": None,
            "time_deadline_reason": None,
            "inflight_drained": True,
            "inflight_count": 0,
        },
        time_exhausted=False,
    )


def build_annual_discovery_graph():
    """Compile the LangGraph plan → bounded scan → finish workflow."""

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise LangGraphUnavailableError(
            "年度开放发现图需要可选依赖 langgraph>=1.2,<1.3。"
        ) from exc

    workflow = StateGraph(AnnualDiscoveryState)
    workflow.add_node("plan", _plan_node)
    workflow.add_node("scan_batches", _scan_batches_node)
    workflow.add_node("finish", _finish_node)
    workflow.add_edge(START, "plan")
    workflow.add_edge("plan", "scan_batches")
    workflow.add_edge("scan_batches", "finish")
    workflow.add_edge("finish", END)
    return workflow.compile()


def parse_annual_discovery_response(
    response_text: object, batch: DiscoveryBatch
) -> tuple[_ParsedFinding, tuple[str, ...]]:
    """Strictly validate one batch response and its exact quotation bindings."""

    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("invalid_model_json")
    if len(response_text) > MAX_MODEL_RESPONSE_CHARS:
        raise ValueError("model_response_too_large")
    try:
        payload = json.loads(response_text, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, _DuplicateJsonKey):
        raise ValueError("invalid_model_json") from None
    if not isinstance(payload, dict) or set(payload) != {"findings", "limitations"}:
        raise ValueError("invalid_model_schema")

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS_PER_BATCH:
        raise ValueError("findings_invalid")
    raw_limitations = payload.get("limitations")
    if not isinstance(raw_limitations, list) or len(raw_limitations) > 8:
        raise ValueError("limitations_invalid")

    fragments = {item.evidence_id: item for item in batch.fragments}
    parsed: list[_ParsedFinding] = []
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict) or set(raw_finding) != {
            "title",
            "observation",
            "why_it_matters",
            "follow_up",
            "priority",
            "citations",
        }:
            raise ValueError("finding_schema_invalid")
        title = _bounded_opinion_text(raw_finding.get("title"), "title", 100)
        observation = _bounded_opinion_text(raw_finding.get("observation"), "observation", 600)
        why_it_matters = _bounded_opinion_text(
            raw_finding.get("why_it_matters"), "why_it_matters", 400
        )
        follow_up = _bounded_opinion_text(raw_finding.get("follow_up"), "follow_up", 400)
        _reject_definitive_conclusion((title, observation, why_it_matters, follow_up))
        priority = raw_finding.get("priority")
        if not isinstance(priority, str) or priority not in {"high", "medium", "low"}:
            raise ValueError("priority_invalid")

        citations_raw = raw_finding.get("citations")
        if not isinstance(citations_raw, list) or not 1 <= len(citations_raw) <= MAX_CITATIONS_PER_FINDING:
            raise ValueError("citations_invalid")
        citations: list[tuple[str, str]] = []
        citation_ids: set[str] = set()
        citation_pairs: set[tuple[str, str]] = set()
        for citation in citations_raw:
            if not isinstance(citation, dict) or set(citation) != {"evidence_id", "quote"}:
                raise ValueError("citation_schema_invalid")
            evidence_id = citation.get("evidence_id")
            quote = citation.get("quote")
            if not isinstance(evidence_id, str) or evidence_id not in fragments:
                raise ValueError("citation_evidence_id_invalid")
            if evidence_id in citation_ids:
                raise ValueError("citation_evidence_id_repeated")
            citation_ids.add(evidence_id)
            if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
                raise ValueError("citation_quote_invalid")
            if len(quote.strip()) < 2 or quote not in fragments[evidence_id].raw_text:
                raise ValueError("citation_quote_not_exact_source_substring")
            pair = (evidence_id, quote)
            if pair in citation_pairs:
                raise ValueError("citation_repeated")
            citation_pairs.add(pair)
            citations.append(pair)
        parsed.append(
            _ParsedFinding(
                title=title,
                observation=observation,
                why_it_matters=why_it_matters,
                follow_up=follow_up,
                priority=priority,
                citations=tuple(citations),
            )
        )

    limitations: list[str] = []
    for raw_limitation in raw_limitations:
        limitation = _bounded_opinion_text(raw_limitation, "limitation", 400)
        _reject_definitive_conclusion((limitation,))
        if limitation not in limitations:
            limitations.append(limitation)
    return tuple(parsed), tuple(limitations)


def parse_annual_discovery_response_v2(
    response_text: object, batch: DiscoveryBatch
) -> AnnualDiscoveryParseResult:
    """Validate a v2 container strictly while retaining independently valid findings."""

    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("invalid_model_json")
    if len(response_text) > MAX_MODEL_RESPONSE_CHARS:
        raise ValueError("model_response_too_large")
    try:
        payload = json.loads(response_text, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, _DuplicateJsonKey):
        raise ValueError("invalid_model_json") from None
    if not isinstance(payload, dict) or set(payload) != {"findings", "limitations"}:
        raise ValueError("invalid_model_schema")

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS_PER_BATCH:
        raise ValueError("findings_invalid")
    raw_limitations = payload.get("limitations")
    if not isinstance(raw_limitations, list) or len(raw_limitations) > 8:
        raise ValueError("limitations_invalid")

    fragments = {item.evidence_id: item for item in batch.fragments}
    parsed: list[_ParsedFinding] = []
    rejected = 0
    for raw_finding in raw_findings:
        try:
            parsed.append(_parse_v2_finding(raw_finding, fragments))
        except (ValueError, TypeError, KeyError):
            rejected += 1

    limitations: list[str] = []
    for raw_limitation in raw_limitations:
        limitation = _bounded_opinion_text(raw_limitation, "limitation", 400)
        _reject_definitive_conclusion((limitation,))
        if limitation not in limitations:
            limitations.append(limitation)

    return AnnualDiscoveryParseResult(
        findings=tuple(parsed),
        limitations=tuple(limitations),
        rejected_findings=rejected,
        restored_quotes=sum(item.restored_quotes for item in parsed),
    )


def parse_annual_discovery_response_v3(
    response_text: object, batch: DiscoveryBatch
) -> AnnualDiscoveryParseResult:
    """Resolve only this batch's namespaced aliases, then verify exact quotes."""

    if batch.plan_version != COMPACT_PLAN_VERSION or not batch.aliases:
        raise ValueError("annual_discovery_alias_plan_mismatch")
    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("invalid_model_json")
    if len(response_text) > MAX_MODEL_RESPONSE_CHARS:
        raise ValueError("model_response_too_large")
    try:
        payload = json.loads(response_text, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, _DuplicateJsonKey):
        raise ValueError("invalid_model_json") from None
    if not isinstance(payload, dict) or set(payload) != {"findings", "limitations"}:
        raise ValueError("invalid_model_schema")
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS_PER_BATCH_V3:
        raise ValueError("findings_invalid")
    raw_limitations = payload.get("limitations")
    if not isinstance(raw_limitations, list) or len(raw_limitations) > 8:
        raise ValueError("limitations_invalid")

    aliases = batch.evidence_id_by_alias
    fragments = {item.evidence_id: item for item in batch.fragments}
    parsed: list[_ParsedFinding] = []
    rejected = 0
    for raw_finding in raw_findings:
        try:
            if not isinstance(raw_finding, dict) or set(raw_finding) != {
                "title", "observation", "why_it_matters", "follow_up", "priority", "citations",
            }:
                raise ValueError("finding_schema_invalid")
            citations = raw_finding.get("citations")
            if not isinstance(citations, list):
                raise ValueError("citations_invalid")
            restored_citations: list[dict[str, object]] = []
            for citation in citations:
                if not isinstance(citation, dict) or set(citation) != {"fragment_alias", "quote"}:
                    raise ValueError("citation_schema_invalid")
                alias = citation.get("fragment_alias")
                if not isinstance(alias, str) or alias not in aliases:
                    raise ValueError("citation_fragment_alias_invalid")
                restored_citations.append({
                    "evidence_id": aliases[alias],
                    "quote": citation.get("quote"),
                })
            normalized = dict(raw_finding)
            normalized["citations"] = restored_citations
            parsed.append(_parse_v2_finding(normalized, fragments))
        except (ValueError, TypeError, KeyError):
            rejected += 1

    limitations: list[str] = []
    for raw_limitation in raw_limitations:
        limitation = _bounded_opinion_text(raw_limitation, "limitation", 400)
        _reject_definitive_conclusion((limitation,))
        if limitation not in limitations:
            limitations.append(limitation)
    return AnnualDiscoveryParseResult(
        findings=tuple(parsed),
        limitations=tuple(limitations),
        rejected_findings=rejected,
        restored_quotes=sum(item.restored_quotes for item in parsed),
    )


def _parse_v2_finding(
    raw_finding: object, fragments: Mapping[str, DiscoveryFragment]
) -> _ParsedFinding:
    if not isinstance(raw_finding, dict) or set(raw_finding) != {
        "title",
        "observation",
        "why_it_matters",
        "follow_up",
        "priority",
        "citations",
    }:
        raise ValueError("finding_schema_invalid")
    title = _bounded_opinion_text(raw_finding.get("title"), "title", 100)
    observation = _bounded_opinion_text(raw_finding.get("observation"), "observation", 600)
    why_it_matters = _bounded_opinion_text(
        raw_finding.get("why_it_matters"), "why_it_matters", 400
    )
    follow_up = _bounded_opinion_text(raw_finding.get("follow_up"), "follow_up", 400)
    _reject_definitive_conclusion((title, observation, why_it_matters, follow_up))
    priority = raw_finding.get("priority")
    if not isinstance(priority, str) or priority not in {"high", "medium", "low"}:
        raise ValueError("priority_invalid")

    citations_raw = raw_finding.get("citations")
    if not isinstance(citations_raw, list) or not 1 <= len(citations_raw) <= MAX_CITATIONS_PER_FINDING:
        raise ValueError("citations_invalid")
    citations: list[tuple[str, str]] = []
    evidence_ids: set[str] = set()
    citation_pairs: set[tuple[str, str]] = set()
    restored_quotes = 0
    for citation in citations_raw:
        if not isinstance(citation, dict) or set(citation) != {"evidence_id", "quote"}:
            raise ValueError("citation_schema_invalid")
        evidence_id = citation.get("evidence_id")
        quote = citation.get("quote")
        if not isinstance(evidence_id, str) or evidence_id not in fragments:
            raise ValueError("citation_evidence_id_invalid")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
            raise ValueError("citation_quote_invalid")
        if len(quote.strip()) < 2:
            raise ValueError("citation_quote_not_exact_source_substring")
        source_text = fragments[evidence_id].raw_text
        if quote not in source_text:
            quote = _restore_whitespace_only_quote(quote, source_text)
            if quote is None:
                raise ValueError("citation_quote_not_exact_source_substring")
            restored_quotes += 1
        pair = (evidence_id, quote)
        if pair in citation_pairs:
            raise ValueError("citation_repeated")
        citation_pairs.add(pair)
        evidence_ids.add(evidence_id)
        citations.append(pair)
    return _ParsedFinding(
        title=title,
        observation=observation,
        why_it_matters=why_it_matters,
        follow_up=follow_up,
        priority=priority,
        citations=tuple(citations),
        restored_quotes=restored_quotes,
    )


def _restore_whitespace_only_quote(quote: str, source_text: str) -> str | None:
    """Recover one uniquely located exact substring after removing Unicode whitespace."""

    normalized_quote = "".join(char for char in quote if not char.isspace())
    if len(normalized_quote) < 2:
        return None
    normalized_source: list[str] = []
    source_positions: list[int] = []
    for index, char in enumerate(source_text):
        if not char.isspace():
            normalized_source.append(char)
            source_positions.append(index)
    normalized_text = "".join(normalized_source)
    matches: list[int] = []
    cursor = 0
    while True:
        position = normalized_text.find(normalized_quote, cursor)
        if position < 0:
            break
        matches.append(position)
        if len(matches) > 1:
            return None
        cursor = position + 1
    if len(matches) != 1:
        return None
    start = matches[0]
    end = start + len(normalized_quote)
    recovered = source_text[source_positions[start] : source_positions[end - 1] + 1]
    if (
        len(recovered) > MAX_QUOTE_CHARS
        or len(recovered.strip()) < 2
        or recovered not in source_text
        or "".join(char for char in recovered if not char.isspace()) != normalized_quote
    ):
        return None
    return recovered


def _is_v4_execution_binding(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("execution_version") == "execution_v4"


def _plan_node(state: AnnualDiscoveryState) -> dict[str, object]:
    try:
        compact = _is_v4_execution_binding(state.get("execution_binding"))
        context = build_annual_discovery_context(
            state["report"],
            state["source_pdf_path"],
            state["source_record"],
            system_prompt=state["prompt_text"],
            model_name=state["settings"].model,
            plan_version=COMPACT_PLAN_VERSION if compact else LEGACY_PLAN_VERSION,
            parsed_pdf=state.get("parsed_pdf"),
        )
    except AnnualDiscoverySourceError as exc:
        code = exc.code if exc.code in {"source_pdf_unreadable", "source_pdf_hash_mismatch"} else "source_pdf_unreadable"
        event = _progress_event(
            "planned", status="not_called", batch_index=0, planned_batches=0,
            processed_batches=0, failed_batches=0, total_pages=0, total_chars=0,
        )
        warning = _emit_progress(state.get("progress_callback"), event)
        limitations = ["来源 PDF 无法读取或与报告哈希不匹配；本次未调用模型。"]
        if warning:
            limitations.append("progress_callback_failed")
        return {
            "context": None,
            "batches": [],
            "fragments_by_id": {},
            "coverage": empty_coverage(),
            "reason": code,
            "not_called_reason": code,
            "overall_status": "not_called",
            "limitations": state.get("limitations", []) + limitations,
            "progress_warning": warning,
        }
    except Exception:
        warning = _emit_progress(
            state.get("progress_callback"),
            _progress_event(
                "planned", status="not_called", batch_index=0, planned_batches=0,
                processed_batches=0, failed_batches=0, total_pages=0, total_chars=0,
            ),
        )
        limitations = ["来源 PDF 解析或批次规划失败；本次未调用模型。"]
        if warning:
            limitations.append("progress_callback_failed")
        return {
            "context": None,
            "batches": [],
            "fragments_by_id": {},
            "coverage": empty_coverage(),
            "reason": "source_or_planning_failed",
            "not_called_reason": "source_or_planning_failed",
            "overall_status": "not_called",
            "limitations": state.get("limitations", []) + limitations,
            "progress_warning": warning,
        }

    coverage = dict(context.coverage)
    batches = list(context.batches)
    reason: str | None = None
    status = "running"
    not_called_reason: str | None = None
    limitations = list(state.get("limitations", []))
    if not batches:
        status = "not_called"
        if coverage["total_chars"] == 0:
            reason = "no_extractable_text"
            not_called_reason = reason
            limitations.append("来源 PDF 没有可提取文字；本次未执行 OCR，也未调用模型。")
        else:
            reason = "input_limits_prevented_planning"
            not_called_reason = reason
            limitations.append("输入上限未能容纳任何文字批次；覆盖缺口已在 coverage 中列出。")
    if coverage.get("image_pages"):
        limitations.append("仅处理 PDF 可提取文字；图片文字未执行 OCR。")
    limitations.append("模型发现、原文引用和词法匹配均未经过独立语义或因果核验。")
    limitations.append("本模块不修改既有报告、核验事实、计算结果或 M3 筛查。")
    if coverage.get("truncated"):
        limitations.append("受全文或批次上限影响，coverage 中标记的页面/字符尚未扫描。")

    warning = _emit_progress(
        state.get("progress_callback"),
        _progress_event(
            "planned",
            status=status,
            batch_index=0,
            planned_batches=len(batches),
            processed_batches=0,
            failed_batches=0,
            total_pages=coverage.get("total_pages", 0),
            total_chars=coverage.get("total_chars", 0),
        ),
    )
    if warning and "progress_callback_failed" not in limitations:
        limitations.append("progress_callback_failed")
    return {
        "context": context,
        "batches": batches,
        "fragments_by_id": {item.evidence_id: item for item in context.fragments},
        "coverage": coverage,
        "reason": reason,
        "not_called_reason": not_called_reason,
        "overall_status": status,
        "limitations": limitations,
        "progress_warning": warning,
    }


def _resolve_max_concurrency(value: int | None) -> int:
    if value is None:
        configured = os.environ.get(CONCURRENCY_ENV)
        if configured is None:
            return DEFAULT_MAX_CONCURRENCY
        if not re.fullmatch(r"[1-3]", configured):
            raise ValueError(f"{CONCURRENCY_ENV} 必须是 1、2 或 3。")
        value = int(configured)
    if type(value) is not int or not 1 <= value <= MAX_CONCURRENCY:
        raise ValueError("年报发现并发数必须是 1、2 或 3。")
    return value


def _write_execution_metadata(
    run_dir: Path, identity: Mapping[str, object], concurrency: int
) -> None:
    path = run_dir / "discovery-execution.json"
    payload = {
        "kind": "fintrace_annual_discovery_execution_v1",
        "prompt_version": LEGACY_PROMPT_VERSION,
        "scheduler": "bounded_rolling",
        "max_in_flight": concurrency,
        "source_sha256": identity["source_sha256"],
    }
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise RuntimeError("年报发现执行元数据已存在，拒绝覆盖。") from None
    except OSError:
        raise RuntimeError("年报发现执行元数据写入失败；未启动批次调用。") from None


def _scan_batches_node_v3(state: AnnualDiscoveryState) -> dict[str, object]:
    """Run checkpointed v3 batches serially so no later call outruns its snapshot."""

    batches = list(state.get("batches", []))
    context = state.get("context")
    binding = state.get("execution_binding")
    identity = state.get("identity")
    if (
        not isinstance(context, AnnualDiscoveryContext)
        or not isinstance(binding, Mapping)
        or not isinstance(identity, Mapping)
    ):
        raise RuntimeError("annual_discovery_v3_context_unavailable")
    parent_id = state.get("parent_discovery")
    parent_count = state.get("parent_provider_admission_count", 0)
    if type(parent_count) is not int or parent_count < 0:
        raise RuntimeError("annual_discovery_v3_parent_count_invalid")
    rows, coverage_rows, existing_findings, limitations, audit_artifacts = (
        _initialize_v3_execution_projection(
            state,
            context,
            binding,
            parent_discovery=parent_id if isinstance(parent_id, Mapping) else None,
            parent_count=parent_count,
            batches=batches,
        )
    )
    inherited_findings: dict[int, list[dict[str, object]]] = {}
    for finding in existing_findings.values():
        index = finding.get("batch_index")
        if type(index) is int:
            inherited_findings.setdefault(index, []).append(finding)

    def publish_snapshot(*, time_exhausted: bool = False) -> dict[str, object]:
        return _v3_discovery_snapshot(
            state, context, binding, parent_id, parent_count, rows, coverage_rows,
            existing_findings, limitations, audit_artifacts,
            time_exhausted=time_exhausted,
        )

    # Persist a complete, source-bound zero-call projection before the first
    # admission. A failed checkpoint callback therefore prevents all calls.
    initial = publish_snapshot()
    callback = state.get("checkpoint_callback")
    if callback is not None:
        callback(initial)
    progress_warning = bool(state.get("progress_warning", False))
    progress_callback = state.get("progress_callback")
    progress_warning = _emit_progress(
        progress_callback,
        _progress_event(
            "planned",
            status="running" if batches else "not_called",
            batch_index=0,
            planned_batches=len(batches),
            processed_batches=0,
            partial_batches=0,
            failed_batches=0,
            total_pages=context.coverage.get("total_pages", 0),
            total_chars=context.coverage.get("total_chars", 0),
        ),
    ) or progress_warning

    stopped_reason: str | None = None
    consecutive_transport_failures = 0
    call_attempts = 0
    response_count = 0
    for batch in batches:
        row = rows[batch.batch_index]
        if row["resolution"] == "reused":
            continue
        if stopped_reason is not None:
            _set_v3_unattempted(row, coverage_rows[batch.batch_index], stopped_reason)
            continue
        cancel_check = state.get("cancel_check")
        if callable(cancel_check) and cancel_check():
            stopped_reason = "user_cancelled"
            _set_v3_unattempted(row, coverage_rows[batch.batch_index], stopped_reason)
            continue
        if float(state["deadline"]) <= _monotonic_clock():
            stopped_reason = "workflow_time_budget_exhausted"
            _set_v3_unattempted(row, coverage_rows[batch.batch_index], stopped_reason)
            continue

        progress_warning = _emit_progress(
            progress_callback,
            _progress_event(
                "batch_started",
                status="running",
                batch_index=batch.batch_index,
                planned_batches=len(batches),
                processed_batches=sum(item["state"] != "unattempted" for item in rows.values()),
                partial_batches=sum(item["state"] == "partial" for item in rows.values()),
                failed_batches=sum(item["state"] == "failed" for item in rows.values()),
                total_pages=context.coverage.get("total_pages", 0),
                total_chars=context.coverage.get("total_chars", 0),
            ),
        ) or progress_warning

        audit_id = str(uuid.uuid4())
        outcome = _call_batch(
            state,
            batch,
            audit_id,
            _OrderedCallGate(batch.batch_index),
        )
        if outcome.audit_artifact is not None:
            audit_artifacts.append(outcome.audit_artifact)
        attempts = _v3_attempt_rows(outcome.attempt_records)
        any_admitted = any(item["provider_admitted"] is True for item in attempts)
        # A started audit request is not a provider attempt. Keep rejected
        # admission evidence in the audit directory, but represent the batch
        # as unattempted when no call crossed the durable admission boundary.
        if not any_admitted:
            attempts = []
        call_attempts += sum(item["provider_admitted"] is True for item in attempts)
        response_count += sum(item["response_accepted"] is True for item in attempts)
        retained = inherited_findings.get(batch.batch_index, [])
        accepted_new: list[dict[str, object]] = []
        rejected = 0
        restored = 0
        failure_reason: str | None = None
        if outcome.response_returned:
            try:
                parsed = parse_annual_discovery_response_v2(outcome.response_text, batch)
                rejected = parsed.rejected_findings
                restored = parsed.restored_quotes
                seen_ids: set[str] = set()
                for parsed_finding in parsed.findings:
                    key = parsed_finding.duplicate_key()
                    finding_id = _finding_id(str(identity["source_sha256"]), key)
                    if finding_id in seen_ids:
                        continue
                    seen_ids.add(finding_id)
                    evidence_ids = list(dict.fromkeys(evidence_id for evidence_id, _quote in parsed_finding.citations))
                    accepted_new.append({
                        "finding_id": finding_id,
                        "batch_index": batch.batch_index,
                        "title": parsed_finding.title,
                        "observation": parsed_finding.observation,
                        "why_it_matters": parsed_finding.why_it_matters,
                        "follow_up": parsed_finding.follow_up,
                        "priority": parsed_finding.priority,
                        "classification": "unverified_model_finding",
                        "verification_status": "unverified",
                        "evidence_ids": evidence_ids,
                        "citations": [
                            {"evidence_id": evidence_id, "quote": quote}
                            for evidence_id, quote in parsed_finding.citations
                        ],
                    })
                for limitation in parsed.limitations:
                    if limitation not in limitations:
                        limitations.append(limitation)
                for attempt in attempts:
                    if attempt["response_accepted"] is True:
                        attempt["accepted_findings"] = len(accepted_new)
            except (ValueError, TypeError, KeyError) as exc:
                failure_reason = str(exc)
                if failure_reason not in _SAFE_CONTAINER_FAILURES:
                    failure_reason = "invalid_model_response"

        for finding in accepted_new:
            existing_findings.setdefault(str(finding["finding_id"]), finding)
        retained_union = [
            item for item in existing_findings.values()
            if item.get("batch_index") == batch.batch_index
        ]
        inherited_count = len(retained)
        accepted_count = len(retained_union)
        if outcome.response_returned and failure_reason is not None:
            row_state, reason = "failed", failure_reason
        elif outcome.response_returned:
            if rejected and accepted_new:
                row_state, reason = "partial", "some_findings_rejected"
            elif rejected and not accepted_new:
                row_state, reason = "failed", "all_findings_rejected"
            else:
                row_state, reason = "succeeded", None
        elif not any_admitted:
            row_state, reason = "unattempted", (
                "workflow_time_budget_exhausted"
                if outcome.failure_code == "model_call_timeout" and float(state["deadline"]) <= _monotonic_clock()
                else "provider_admission_rejected"
            )
        else:
            row_state = "failed"
            fail_codes = [str(item.get("reason")) for item in attempts]
            reason = "audit_write_failed" if "audit_write_failed" in fail_codes else (
                "model_call_timeout" if "timeout" in fail_codes else "model_call_failed"
            )
        row.update(
            state=row_state,
            resolution="unattempted" if row_state == "unattempted" else "executed",
            reason=reason,
            attempts=attempts,
        )
        coverage_rows[batch.batch_index] = _v3_coverage_row(
            batch, row_state, reason, str(row["resolution"]),
            accepted_count, inherited_count,
            rejected if row_state in {"partial", "failed"} else 0,
            restored if row_state in {"succeeded", "partial"} else 0,
        )
        progress_warning = _emit_progress(
            progress_callback,
            _progress_event(
            "batch_finished",
            status={
                "succeeded": "completed",
                "partial": "partial",
                "failed": "failed",
                "unattempted": "running",
            }[row_state],
                batch_index=batch.batch_index,
                planned_batches=len(batches),
                processed_batches=sum(item["state"] != "unattempted" for item in rows.values()),
                partial_batches=sum(item["state"] == "partial" for item in rows.values()),
                failed_batches=sum(item["state"] == "failed" for item in rows.values()),
                total_pages=context.coverage.get("total_pages", 0),
                total_chars=context.coverage.get("total_chars", 0),
            ),
        ) or progress_warning
        if progress_warning and "progress_callback_failed" not in limitations:
            limitations.append("progress_callback_failed")
        attempt_reasons = {item.get("reason") for item in _v3_attempt_rows(outcome.attempt_records)}
        if any(item.get("reason") in {"http_401", "http_403"} for item in attempts):
            stopped_reason = "authentication_failure_stopped"
        elif any(item.get("reason") in {"ledger_budget_exhausted", "call_gate_closed"} for item in attempts):
            stopped_reason = "provider_budget_exhausted"
        elif any(item.get("reason") == "call_deadline_exhausted" for item in attempts):
            stopped_reason = "workflow_time_budget_exhausted"
        elif attempt_reasons & {
            "ledger_provider_mismatch", "ledger_source_context_missing_or_unapproved",
            "ledger_unavailable", "ledger_policy_mismatch", "audit_write_failed",
        }:
            stopped_reason = "provider_admission_rejected"
        elif outcome.response_returned:
            consecutive_transport_failures = 0
        elif attempts and all(
            item.get("reason") in {"http_429", "http_5xx", "connection_error", "timeout"}
            for item in attempts
        ):
            consecutive_transport_failures += sum(item.get("provider_admitted") is True for item in attempts)
            if consecutive_transport_failures >= 3:
                stopped_reason = "consecutive_transport_failures_stopped"
        elif row_state == "succeeded":
            consecutive_transport_failures = 0

        snapshot = publish_snapshot(
            time_exhausted=stopped_reason == "workflow_time_budget_exhausted"
        )
        if callback is not None:
            callback(snapshot)
        if callable(cancel_check) and cancel_check():
            stopped_reason = "user_cancelled"

    final = publish_snapshot(time_exhausted=stopped_reason == "workflow_time_budget_exhausted")
    if callback is not None:
        callback(final)
    progress_warning = _emit_progress(
        progress_callback,
        _progress_event(
            "batch_finished" if final["coverage"]["processed_batches"] else "planned",
            status=final["status"],
            batch_index=final["coverage"]["processed_batches"],
            planned_batches=len(batches),
            processed_batches=final["coverage"]["processed_batches"],
            partial_batches=final["coverage"]["partial_batches"],
            failed_batches=final["coverage"]["failed_batches"],
            total_pages=context.coverage.get("total_pages", 0),
            total_chars=context.coverage.get("total_chars", 0),
        ),
    ) or progress_warning
    return {
        "findings": list(final["findings"]),
        "finding_keys": [],
        "limitations": list(final["limitations"]),
        "audit_artifacts": audit_artifacts,
        "coverage": dict(final["coverage"]),
        "reason": final["reason"],
        "overall_status": final["status"],
        "model_call_count": response_count,
        "model_call_attempt_count": call_attempts,
        "successful_batches": final["coverage"]["successful_batches"],
        "partial_batches": final["coverage"]["partial_batches"],
        "failed_batches": final["coverage"]["failed_batches"],
        "processed_batches": final["coverage"]["processed_batches"],
        "processed_chars": final["coverage"]["processed_chars"],
        "attempted_chars": final["coverage"]["attempted_chars"],
        "execution_contract": final["execution_contract"],
        "not_called_reason": final["reason"] if final["status"] == "not_called" else None,
        "budget_exhausted": final["coverage"]["time_budget_exhausted"],
        "progress_warning": progress_warning,
    }


def _set_v3_unattempted(
    contract_row: dict[str, object], coverage_row: dict[str, object], reason: str
) -> None:
    safe_reason = reason if reason in {
        "awaiting_model", "workflow_time_budget_exhausted", "user_cancelled",
        "discovery_stage_budget_exhausted", "provider_budget_exhausted", "provider_admission_rejected",
        "authentication_failure_stopped", "consecutive_transport_failures_stopped",
        "parent_call_gate_closed",
    } else "awaiting_model"
    contract_row.update(
        state="unattempted", resolution="unattempted", reason=safe_reason, attempts=[]
    )
    coverage_row.update(
        status="unattempted", resolution="unattempted", reason=safe_reason,
        accepted_findings=coverage_row["inherited_findings"],
        rejected_findings=0, restored_quotes=0,
    )


def _v3_attempt_rows(records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    safe_failures = {
        "http_401", "http_403", "http_429", "http_5xx", "connection_error", "timeout",
        "invalid_response", "transport_error", "cancelled", "ledger_budget_exhausted",
        "ledger_provider_mismatch", "ledger_source_context_missing_or_unapproved",
        "ledger_unavailable", "ledger_policy_mismatch", "call_gate_closed",
        "call_deadline_exhausted", "audit_write_failed", "call_failed",
        "stage_budget_exhausted", "task_budget_exhausted",
    }
    for expected_index, raw in enumerate(records, 1):
        audit_id = raw.get("audit_id")
        admission_id = raw.get("admission_id")
        admitted = raw.get("provider_admitted")
        accepted = raw.get("response_accepted")
        if (
            type(raw.get("attempt_index")) is not int
            or raw.get("attempt_index") != expected_index
            or not _is_uuid4_text(audit_id)
            or type(admitted) is not bool
            or type(accepted) is not bool
            or (admitted and not _is_uuid4_text(admission_id))
            or (not admitted and admission_id is not None)
        ):
            raise RuntimeError("annual_discovery_v3_attempt_record_invalid")
        reason = raw.get("reason")
        if accepted:
            if not admitted or raw.get("status") != "succeeded" or reason is not None:
                raise RuntimeError("annual_discovery_v3_accepted_attempt_invalid")
            status = "succeeded"
            safe_reason = None
        else:
            status = "failed" if admitted else "rejected"
            safe_reason = reason if isinstance(reason, str) and reason in safe_failures else "call_failed"
        rows.append({
            "attempt_index": expected_index,
            "audit_id": str(audit_id),
            "admission_id": str(admission_id) if admitted else None,
            "provider_admitted": admitted,
            "status": status,
            "response_accepted": accepted,
            "accepted_findings": 0,
            "reason": safe_reason,
        })
    return rows


def _is_uuid4_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _initialize_v3_execution_projection(
    state: Mapping[str, object],
    context: AnnualDiscoveryContext,
    binding: Mapping[str, object],
    *,
    parent_discovery: Mapping[str, object] | None,
    parent_count: int,
    batches: Sequence[DiscoveryBatch],
) -> tuple[
    dict[int, dict[str, object]],
    dict[int, dict[str, object]],
    dict[str, dict[str, object]],
    list[str],
    list[AuditArtifact],
]:
    """Create the exact zero-call state shared by plan-only and scan paths."""

    if type(parent_count) is not int or parent_count < 0:
        raise RuntimeError("annual_discovery_v3_parent_count_invalid")
    parent_contract = (
        parent_discovery.get("execution_contract")
        if isinstance(parent_discovery, Mapping)
        else None
    )
    parent_rows = {
        item.get("batch_index"): item
        for item in parent_contract.get("batches", [])
        if isinstance(item, Mapping) and type(item.get("batch_index")) is int
    } if isinstance(parent_contract, Mapping) and isinstance(parent_contract.get("batches"), list) else {}

    inherited_findings: dict[int, list[dict[str, object]]] = {}
    existing_findings: dict[str, dict[str, object]] = {}
    if isinstance(parent_discovery, Mapping):
        raw_findings = parent_discovery.get("findings", [])
        if not isinstance(raw_findings, list):
            raise RuntimeError("annual_discovery_v3_parent_findings_invalid")
        for raw in raw_findings:
            if not isinstance(raw, Mapping) or type(raw.get("batch_index")) is not int:
                raise RuntimeError("annual_discovery_v3_parent_finding_binding_invalid")
            finding = dict(raw)
            index = int(finding["batch_index"])
            inherited_findings.setdefault(index, []).append(finding)
            existing_findings[str(finding.get("finding_id"))] = finding

    # Reuse is possible only after the caller has strictly verified the parent.
    rows: dict[int, dict[str, object]] = {}
    coverage_rows: dict[int, dict[str, object]] = {}
    for batch in batches:
        parent = parent_rows.get(batch.batch_index)
        retained = inherited_findings.get(batch.batch_index, [])
        if isinstance(parent, Mapping) and parent.get("state") == "succeeded":
            rows[batch.batch_index] = {
                "batch_index": batch.batch_index,
                "state": "succeeded",
                "resolution": "reused",
                "reason": None,
                "attempts": [],
            }
            coverage_rows[batch.batch_index] = _v3_coverage_row(
                batch, "succeeded", None, "reused", len(retained), len(retained), 0, 0
            )
        else:
            rows[batch.batch_index] = {
                "batch_index": batch.batch_index,
                "state": "unattempted",
                "resolution": "unattempted",
                "reason": "awaiting_model",
                "attempts": [],
            }
            coverage_rows[batch.batch_index] = _v3_coverage_row(
                batch,
                "unattempted",
                "awaiting_model",
                "unattempted",
                len(retained),
                len(retained),
                0,
                0,
            )

    raw_limitations = state.get("limitations", [])
    limitations = list(raw_limitations) if isinstance(raw_limitations, list) else []
    for limitation in (
        "模型发现、原文引用和词法匹配均未经过独立语义或因果核验。",
        "本模块不修改既有报告、核验事实、计算结果或 M3 筛查。",
    ):
        if limitation not in limitations:
            limitations.append(limitation)
    if context.coverage.get("image_pages"):
        limitation = "仅处理 PDF 可提取文字；图片文字未执行 OCR。"
        if limitation not in limitations:
            limitations.append(limitation)
    return rows, coverage_rows, existing_findings, limitations, []


def _v3_coverage_row(
    batch: DiscoveryBatch,
    status: str,
    reason: str | None,
    resolution: str,
    accepted: int,
    inherited: int,
    rejected: int,
    restored: int,
) -> dict[str, object]:
    return {
        "batch_index": batch.batch_index,
        "status": status,
        "reason": reason,
        "pages": sorted({fragment.pdf_page for fragment in batch.fragments}),
        "raw_chars": batch.raw_chars,
        "accepted_findings": accepted,
        "rejected_findings": rejected,
        "restored_quotes": restored,
        "resolution": resolution,
        "inherited_findings": inherited,
    }


def _v3_discovery_snapshot(
    state: AnnualDiscoveryState,
    context: AnnualDiscoveryContext,
    binding: Mapping[str, object],
    parent_discovery: Mapping[str, object] | None,
    parent_count: int,
    rows: Mapping[int, Mapping[str, object]],
    coverage_rows: Mapping[int, Mapping[str, object]],
    findings_by_id: Mapping[str, Mapping[str, object]],
    limitations: Sequence[str],
    audit_artifacts: Sequence[AuditArtifact],
    *,
    time_exhausted: bool,
    findings_by_batch_order: bool = False,
) -> dict[str, object]:
    batches = [dict(rows[index]) for index in sorted(rows)]
    cov_batches = [dict(coverage_rows[index]) for index in sorted(coverage_rows)]
    counts = {name: sum(row["state"] == name for row in batches) for name in ("succeeded", "partial", "failed", "unattempted")}
    admitted = sum(
        attempt.get("provider_admitted") is True
        for row in batches for attempt in row.get("attempts", [])
    )
    responses = sum(
        attempt.get("response_accepted") is True
        for row in batches for attempt in row.get("attempts", [])
    )
    attempts = [attempt for row in batches for attempt in row.get("attempts", [])]
    # The contract is lineage-counted for discovery. The job service separately
    # reads its all-stage gate count from the shared ledger.
    plan_hash = str(binding.get("plan_sha256"))
    identity_hash = hashlib.sha256(json.dumps(
        dict(binding), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()
    parent_contract = parent_discovery.get("execution_contract") if isinstance(parent_discovery, Mapping) else None
    parent_id = parent_contract.get("parent_run_id") if isinstance(parent_contract, Mapping) else None
    if isinstance(parent_discovery, Mapping):
        parent_id = parent_discovery.get("run_id")
    contract = {
        "kind": "fintrace_annual_discovery_execution_v3",
        "plan_sha256": plan_hash,
        "identity_sha256": identity_hash,
        "binding": dict(binding),
        "parent_run_id": parent_id,
        "provider_admission_count": admitted,
        "parent_provider_admission_count": parent_count,
        "cumulative_provider_admission_count": parent_count + admitted,
        "response_count": responses,
        "reused_batches": [row["batch_index"] for row in batches if row["resolution"] == "reused"],
        "batches": batches,
    }
    coverage = dict(context.coverage)
    total_chars = int(coverage.get("total_chars", 0))
    planned_chars = sum(int(row["raw_chars"]) for row in cov_batches)
    unattempted_chars = sum(int(row["raw_chars"]) for row in cov_batches if row["status"] == "unattempted")
    processed_chars = sum(int(row["raw_chars"]) for row in cov_batches if row["status"] == "succeeded")
    attempted_chars = sum(
        int(coverage_rows[index]["raw_chars"])
        for index, contract_row in rows.items()
        if any(item.get("provider_admitted") is True for item in contract_row.get("attempts", []))
    )
    processed = counts["succeeded"] + counts["partial"] + counts["failed"]
    failed_pages = sorted({page for row in cov_batches if row["status"] == "failed" for page in row["pages"]})
    unread_pages = set(coverage.get("unread_pages", []))
    for row in cov_batches:
        if row["status"] in {"partial", "failed", "unattempted"}:
            unread_pages.update(row["pages"])
    omitted = max(total_chars - planned_chars, 0) + unattempted_chars
    coverage.update({
        "processed_batches": processed,
        "successful_batches": counts["succeeded"],
        "partial_batches": counts["partial"],
        "failed_batches": counts["failed"],
        "unattempted_batches": counts["unattempted"],
        "unattempted_chars": unattempted_chars,
        "planned_chars": planned_chars,
        "omitted_chars": omitted,
        "processed_chars": processed_chars,
        "attempted_chars": attempted_chars,
        "batch_results": cov_batches,
        "failed_pages": failed_pages,
        "unread_pages": sorted(unread_pages),
        "truncated": bool(omitted),
        "time_budget_exhausted": time_exhausted,
    })
    coverage["complete"] = bool(
        coverage.get("source_status") == "parsed"
        and coverage.get("content_status") == "text_available"
        and total_chars > 0
        and not omitted
        and not coverage.get("unread_image_pages")
        and planned_chars == total_chars
        and processed == len(batches)
        and counts["partial"] == 0
        and counts["failed"] == 0
        and counts["succeeded"] == len(batches)
        and processed_chars == planned_chars
        and not unread_pages
    )
    if len(batches) == 0 or processed == 0:
        status = "not_called"
    elif counts["failed"] and not counts["succeeded"] and not counts["partial"]:
        status = "failed"
    elif counts["partial"] or counts["failed"] or counts["unattempted"] or not coverage["complete"]:
        status = "partial" if counts["succeeded"] or counts["partial"] else "failed"
    else:
        status = "completed"
    if status == "failed" and counts["failed"] == 0:
        status = "not_called"
    reason = None
    if status == "not_called":
        reason = "workflow_time_budget_exhausted" if time_exhausted else "awaiting_model"
    elif status == "failed":
        reason = next((str(row["reason"]) for row in batches if row["state"] == "failed"), "model_call_failed")
    elif status == "partial":
        reason = next((str(row["reason"]) for row in batches if row["state"] in {"partial", "failed", "unattempted"}), "coverage_incomplete")
    if findings_by_batch_order:
        findings = [
            dict(item)
            for _, item in sorted(
                findings_by_id.items(),
                key=lambda pair: (
                    pair[1].get("batch_index") if type(pair[1].get("batch_index")) is int else 0,
                    pair[0],
                ),
            )
        ]
    else:
        findings = [dict(item) for _, item in sorted(findings_by_id.items())]
    evidence_ids = {
        evidence_id for finding in findings
        for evidence_id in finding.get("evidence_ids", []) if isinstance(evidence_id, str)
    }
    identity_value = state["identity"]
    fragments = {item.evidence_id: item for item in context.fragments}
    evidences = [
        fragments[evidence_id].evidence_value(str(identity_value["source_document_id"]), str(identity_value["source_sha256"]))
        for evidence_id in sorted(evidence_ids) if evidence_id in fragments
    ]
    return {
        "kind": "fintrace_annual_discovery",
        "run_id": identity_value["run_id"],
        "company_id": identity_value["company_id"],
        "report_year": identity_value["report_year"],
        "source_document_id": identity_value["source_document_id"],
        "source_sha256": identity_value["source_sha256"],
        "status": status,
        "prompt_version": str(state.get("prompt_version", PROMPT_VERSION)),
        "model_called": admitted > 0,
        "model_call_count": responses,
        "model_call_attempt_count": admitted,
        "coverage": coverage,
        "findings": findings,
        "evidences": evidences,
        "audit_artifacts": [item.to_dict() for item in audit_artifacts],
        "limitations": list(dict.fromkeys(limitations)),
        "reason": reason,
        "fraud_conclusion": None,
        "execution_contract": contract,
    }


def _v4_discovery_snapshot(
    state: AnnualDiscoveryState,
    context: AnnualDiscoveryContext,
    binding: Mapping[str, object],
    parent_discovery: Mapping[str, object] | None,
    parent_count: int,
    rows: Mapping[int, Mapping[str, object]],
    coverage_rows: Mapping[int, Mapping[str, object]],
    findings_by_id: Mapping[str, Mapping[str, object]],
    limitations: Sequence[str],
    audit_artifacts: Sequence[AuditArtifact],
    *,
    scheduler: Mapping[str, object],
    time_exhausted: bool,
) -> dict[str, object]:
    phase_budget = state.get("phase_budget")
    discovery_started_at = state.get("discovery_started_at")
    if (
        not isinstance(phase_budget, Mapping)
        or type(discovery_started_at) not in {int, float}
        or not math.isfinite(float(discovery_started_at))
    ):
        raise RuntimeError("annual_discovery_v4_timing_state_missing")
    now = _monotonic_clock()
    snapshot = _v3_discovery_snapshot(
        state,
        context,
        binding,
        parent_discovery,
        parent_count,
        rows,
        coverage_rows,
        findings_by_id,
        limitations,
        audit_artifacts,
        time_exhausted=time_exhausted,
        findings_by_batch_order=True,
    )
    contract = dict(snapshot["execution_contract"])
    contract.update(
        kind="fintrace_annual_discovery_execution_v4",
        scheduler=dict(scheduler),
        phase_budget=dict(phase_budget),
        timing={
            "source_parse_seconds": round(context.parse_seconds, 6),
            "planning_seconds": round(context.planning_seconds, 6),
            "source_parse_reused": state.get("parsed_pdf") is not None,
            "discovery_elapsed_seconds": round(
                max(0.0, now - float(discovery_started_at)),
                6,
            ),
            "main_work_remaining_seconds": round(
                max(0.0, float(state["main_soft_deadline"]) - now), 6
            ) if isinstance(state.get("main_soft_deadline"), (int, float)) else None,
        },
    )
    snapshot["execution_contract"] = contract
    closed_reason = scheduler.get("dispatch_closed_reason")
    if closed_reason is not None and snapshot.get("status") in {"not_called", "partial", "failed"}:
        snapshot["reason"] = closed_reason
    return snapshot


def _v4_expired_deadline_reason(state: Mapping[str, object]) -> str | None:
    """Return the precise deadline whose monotonic bound has elapsed."""

    now = _monotonic_clock()
    main_deadline = state.get("main_soft_deadline")
    if isinstance(main_deadline, (int, float)) and now >= float(main_deadline):
        return "workflow_time_budget_exhausted"
    stage_deadline = state.get("discovery_deadline")
    if isinstance(stage_deadline, (int, float)) and now >= float(stage_deadline):
        return "discovery_stage_budget_exhausted"
    effective_deadline = state.get("deadline")
    if isinstance(effective_deadline, (int, float)) and now >= float(effective_deadline):
        return "workflow_time_budget_exhausted"
    return None


def _scan_batches_node(state: AnnualDiscoveryState) -> dict[str, object]:
    if _is_v4_execution_binding(state.get("execution_binding")):
        return _scan_batches_node_v4(state)
    if isinstance(state.get("execution_binding"), Mapping):
        return _scan_batches_node_v3(state)
    batches = list(state.get("batches", []))
    if not batches or state.get("overall_status") == "not_called":
        return {}

    concurrency = int(state.get("max_concurrency", DEFAULT_MAX_CONCURRENCY))
    _write_execution_metadata(Path(state["run_dir"]), state["identity"], concurrency)
    base_coverage = dict(state.get("coverage", {}))
    processed = successful = partial = failed = 0
    callback_warning = bool(state.get("progress_warning", False))
    parsed_by_index: dict[int, dict[str, object]] = {}
    audit_ids: dict[int, str] = {}
    next_index = 0
    stop_dispatch = False
    call_gate = _OrderedCallGate(batches[0].batch_index)
    max_workers = concurrency

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="annual-discovery") as pool:
        in_flight: dict[Future[_BatchCallOutcome], int] = {}

        def submit_one() -> bool:
            nonlocal next_index, callback_warning, stop_dispatch
            if next_index >= len(batches):
                return False
            if float(state["deadline"]) - _monotonic_clock() <= 0:
                stop_dispatch = True
                return False
            batch = batches[next_index]
            next_index += 1
            audit_id = str(uuid.uuid4())
            audit_ids[batch.batch_index] = audit_id
            callback_warning = _emit_progress(
                state.get("progress_callback"),
                _progress_event(
                    "batch_started",
                    status="running",
                    batch_index=batch.batch_index,
                    planned_batches=len(batches),
                    processed_batches=processed,
                    partial_batches=partial,
                    failed_batches=failed,
                    total_pages=base_coverage.get("total_pages", 0),
                    total_chars=base_coverage.get("total_chars", 0),
                ),
            ) or callback_warning
            future = pool.submit(_call_batch, state, batch, audit_id, call_gate)
            in_flight[future] = batch.batch_index
            return True

        while len(in_flight) < concurrency and submit_one():
            pass

        while in_flight:
            remaining = float(state["deadline"]) - _monotonic_clock()
            if remaining > 0:
                done, _pending = wait(
                    tuple(in_flight), timeout=remaining, return_when=FIRST_COMPLETED
                )
                if not done:
                    stop_dispatch = True
                    continue
            else:
                stop_dispatch = True
                done, _pending = wait(tuple(in_flight), return_when=FIRST_COMPLETED)

            completed = sorted(
                done,
                key=lambda future: _future_completion_key(
                    future, in_flight[future]
                ),
            )
            future = completed[0]
            batch_index = in_flight.pop(future)
            try:
                outcome = future.result()
            except Exception:
                batch = next(item for item in batches if item.batch_index == batch_index)
                audit_id = audit_ids[batch_index]
                audit_dir = Path(state["run_dir"]) / audit_id
                artifact = (
                    _audit_artifact_from_dir(audit_dir, batch_index, _audit_dir_status(audit_dir), str(state.get("prompt_version", LEGACY_PROMPT_VERSION)))
                    if audit_dir.is_dir()
                    else AuditArtifact(batch_index, None, None, None, None, "failed", str(state.get("prompt_version", LEGACY_PROMPT_VERSION)))
                )
                outcome = _BatchCallOutcome(
                    batch=batch,
                    response_text="",
                    call_succeeded=False,
                    failure_code="model_call_failed",
                    attempted=audit_dir.is_dir() and (audit_dir / "request.json").is_file(),
                    response_returned=False,
                    audit_artifact=artifact,
                    completed_at=time.perf_counter(),
                )

            if outcome.unattempted:
                stop_dispatch = True
            else:
                local = _process_batch_outcome(state, outcome)
                parsed_by_index[batch_index] = local
                result_row = local["coverage"]["batch_results"][-1]
                result_status = result_row["status"]
                processed += 1
                if result_status == "succeeded":
                    successful += 1
                elif result_status == "partial":
                    partial += 1
                else:
                    failed += 1
                callback_warning = _emit_progress(
                    state.get("progress_callback"),
                    _progress_event(
                        "batch_finished",
                        status={
                            "succeeded": "completed",
                            "partial": "partial",
                            "failed": "failed",
                        }[result_status],
                        batch_index=batch_index,
                        planned_batches=len(batches),
                        processed_batches=processed,
                        partial_batches=partial,
                        failed_batches=failed,
                        total_pages=base_coverage.get("total_pages", 0),
                        total_chars=base_coverage.get("total_chars", 0),
                    ),
                ) or callback_warning

            if not stop_dispatch:
                while len(in_flight) < concurrency and submit_one():
                    pass

    results = [parsed_by_index[index] for index in sorted(parsed_by_index)]
    outcomes_by_index = dict(parsed_by_index)
    # Preserve completion-order counters for progress, then normalize all durable
    # output in the original PDF batch order.
    batch_results = [
        row
        for result in results
        for row in result["coverage"].get("batch_results", [])
    ]
    attempted_indices = set(outcomes_by_index)
    unattempted_batches = [batch for batch in batches if batch.batch_index not in attempted_indices]
    budget_exhausted = bool(unattempted_batches)
    coverage = dict(base_coverage)
    failed_pages = set(coverage.get("failed_pages", []))
    unread_pages = set(coverage.get("unread_pages", []))
    for result in results:
        result_coverage = result.get("coverage", {})
        if isinstance(result_coverage, Mapping):
            failed_pages.update(result_coverage.get("failed_pages", []))
            unread_pages.update(result_coverage.get("unread_pages", []))
    if budget_exhausted:
        omitted_chars = sum(batch.raw_chars for batch in unattempted_batches)
        unread_pages.update(
            fragment.pdf_page
            for pending in unattempted_batches
            for fragment in pending.fragments
        )
        batch_results.extend(
            _batch_result(batch, "unattempted", "workflow_time_budget_exhausted")
            for batch in unattempted_batches
        )
        coverage.update(
            {
                "unattempted_batches": len(unattempted_batches),
                "unattempted_chars": omitted_chars,
                "omitted_chars": int(coverage.get("omitted_chars", 0)) + omitted_chars,
                "unread_pages": sorted(unread_pages),
                "truncated": True,
                "time_budget_exhausted": True,
            }
        )
    attempted_chars = sum(
        batch.raw_chars
        for batch in batches
        if batch.batch_index in outcomes_by_index
        and outcomes_by_index[batch.batch_index].get("attempted") is True
    )
    response_count = sum(
        1
        for batch in batches
        if batch.batch_index in outcomes_by_index
        and outcomes_by_index[batch.batch_index].get("response_returned") is True
    )
    attempt_count = sum(
        1
        for batch in batches
        if batch.batch_index in outcomes_by_index
        and outcomes_by_index[batch.batch_index].get("attempted") is True
    )
    processed_chars = sum(
        batch.raw_chars
        for batch in batches
        if batch.batch_index in outcomes_by_index
        and outcomes_by_index[batch.batch_index]["coverage"]["batch_results"][-1]["status"] == "succeeded"
    )
    findings: list[dict[str, object]] = []
    seen_finding_ids: set[str] = set()
    limitations = list(state.get("limitations", []))
    audits: list[AuditArtifact] = []
    for batch in batches:
        local = outcomes_by_index.get(batch.batch_index)
        if local is None:
            continue
        for finding in local.get("findings", []):
            if not isinstance(finding, dict):
                continue
            finding_id = finding.get("finding_id")
            if isinstance(finding_id, str) and finding_id in seen_finding_ids:
                continue
            if isinstance(finding_id, str):
                seen_finding_ids.add(finding_id)
            findings.append(finding)
        for limitation in local.get("limitations", []):
            if isinstance(limitation, str) and limitation not in limitations:
                limitations.append(limitation)
        artifact = local.get("audit_artifact")
        if isinstance(artifact, AuditArtifact):
            audits.append(artifact)
    for batch in unattempted_batches:
        if "总体发现时间预算耗尽；未尝试批次和页面已列入 coverage。" not in limitations:
            limitations.append("总体发现时间预算耗尽；未尝试批次和页面已列入 coverage。")
        break
    if callback_warning and "progress_callback_failed" not in limitations:
        limitations.append("progress_callback_failed")

    coverage.update(
        {
            "processed_batches": len(parsed_by_index),
            "successful_batches": successful,
            "partial_batches": partial,
            "failed_batches": failed,
            "processed_chars": processed_chars,
            "attempted_chars": attempted_chars,
            "batch_results": batch_results,
            "failed_pages": sorted(failed_pages),
            "unread_pages": sorted(unread_pages),
        }
    )
    coverage["complete"] = coverage_is_complete(coverage)
    warning = callback_warning
    terminal_status = "budget_exhausted" if budget_exhausted else "running"
    if budget_exhausted:
        warning = _emit_progress(
            state.get("progress_callback"),
            _progress_event(
                "budget_exhausted",
                status="partial" if attempt_count else "not_called",
                batch_index=unattempted_batches[0].batch_index,
                planned_batches=len(batches),
                processed_batches=len(parsed_by_index),
                partial_batches=partial,
                failed_batches=failed,
                total_pages=coverage.get("total_pages", 0),
                total_chars=coverage.get("total_chars", 0),
            ),
        ) or warning
        if warning and "progress_callback_failed" not in limitations:
            limitations.append("progress_callback_failed")

    return {
        "model_call_attempt_count": attempt_count,
        "model_call_count": response_count,
        "processed_batches": len(parsed_by_index),
        "successful_batches": successful,
        "partial_batches": partial,
        "failed_batches": failed,
        "processed_chars": processed_chars,
        "attempted_chars": attempted_chars,
        "findings": findings,
        "finding_keys": [],
        "limitations": limitations,
        "audit_artifacts": sorted(audits, key=lambda item: item.batch_index),
        "coverage": coverage,
        "reason": "workflow_time_budget_exhausted" if budget_exhausted else state.get("reason"),
        "overall_status": terminal_status,
        "not_called_reason": "workflow_time_budget_exhausted" if budget_exhausted and attempt_count == 0 else None,
        "budget_exhausted": budget_exhausted,
        "progress_warning": warning,
    }


def _scan_batches_node_v4(state: AnnualDiscoveryState) -> dict[str, object]:
    """Run compact-plan batches with bounded workers and one checkpoint writer."""

    batches = list(state.get("batches", []))
    context = state.get("context")
    binding = state.get("execution_binding")
    identity = state.get("identity")
    if not isinstance(context, AnnualDiscoveryContext) or not isinstance(binding, Mapping) or not isinstance(identity, Mapping):
        raise RuntimeError("annual_discovery_v4_context_unavailable")
    parent_value = state.get("parent_discovery")
    parent = parent_value if isinstance(parent_value, Mapping) else None
    parent_count = state.get("parent_provider_admission_count", 0)
    if type(parent_count) is not int or parent_count < 0:
        raise RuntimeError("annual_discovery_v4_parent_count_invalid")

    rows, coverage_rows, findings_by_id, limitations, audit_artifacts = (
        _initialize_v3_execution_projection(
            state, context, binding, parent_discovery=parent,
            parent_count=parent_count, batches=batches,
        )
    )
    inherited_by_batch: dict[int, list[dict[str, object]]] = {}
    for finding in findings_by_id.values():
        index = finding.get("batch_index")
        if type(index) is int:
            inherited_by_batch.setdefault(index, []).append(finding)

    concurrency = min(max(int(state.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)), 1), MAX_CONCURRENCY)
    call_gate = _OrderedCallGate(batches[0].batch_index if batches else 1)
    scan_abort_event = Event()
    callback = state.get("checkpoint_callback")
    progress_callback = state.get("progress_callback")
    progress_warning = bool(state.get("progress_warning", False))
    checkpoint_publications = 0
    inflight_peak = 0
    dispatch_closed_reason: str | None = None
    time_deadline_reason: str | None = None
    consecutive_transport_failures = 0
    next_index = 0
    future_to_batch: dict[Future[_BatchCallOutcome], DiscoveryBatch] = {}

    def publish() -> dict[str, object]:
        scheduler = {
            "requested_max_concurrency": int(state.get("max_concurrency", concurrency)),
            "effective_max_concurrency": concurrency,
            "inflight_peak": inflight_peak,
            "checkpoint_coordinator_count": 1,
            "checkpoint_publications": checkpoint_publications + (1 if callback is not None else 0),
            "dispatch_closed_reason": dispatch_closed_reason,
            "time_deadline_reason": time_deadline_reason,
            "inflight_drained": not future_to_batch,
            "inflight_count": len(future_to_batch),
        }
        return _v4_discovery_snapshot(
            state, context, binding, parent, parent_count, rows, coverage_rows,
            findings_by_id, limitations, audit_artifacts,
            scheduler=scheduler,
            time_exhausted=time_deadline_reason is not None,
        )

    def publish_checkpoint() -> None:
        nonlocal checkpoint_publications
        if callback is None:
            return
        callback(publish())
        checkpoint_publications += 1

    initial = publish()
    if callback is not None:
        callback(initial)
        checkpoint_publications += 1
    progress_warning = _emit_progress(
        progress_callback,
        _progress_event(
            "planned", status="running" if batches else "not_called", batch_index=0,
            planned_batches=len(batches), processed_batches=0, partial_batches=0,
            failed_batches=0, total_pages=context.coverage.get("total_pages", 0),
            total_chars=context.coverage.get("total_chars", 0),
        ),
    ) or progress_warning

    def next_is_cancelled() -> bool:
        cancel = state.get("cancel_check")
        return callable(cancel) and bool(cancel())

    def stop_dispatch(reason: str, *, time_reason: str | None = None) -> None:
        nonlocal dispatch_closed_reason, time_deadline_reason
        quota_reasons = {"provider_budget_exhausted", "discovery_stage_budget_exhausted"}
        quota_only_closed = (
            dispatch_closed_reason in quota_reasons and time_deadline_reason is None
        )
        requested_quota_close = reason in quota_reasons and time_reason is None
        if dispatch_closed_reason is None or quota_only_closed and not requested_quota_close:
            dispatch_closed_reason = reason
        if time_reason is not None:
            time_deadline_reason = time_reason
            # The v4 archive binds a non-null deadline reason to the final
            # dispatch reason. A later actual deadline supersedes an earlier
            # quota-only close; quota reason strings alone never imply time.
            dispatch_closed_reason = time_reason
        call_gate.close()
        if not requested_quota_close:
            scan_abort_event.set()

    def quota_close_is_soft() -> bool:
        return (
            dispatch_closed_reason in {
                "provider_budget_exhausted", "discovery_stage_budget_exhausted",
            }
            and time_deadline_reason is None
        )

    def monitor_soft_close() -> None:
        """Upgrade a quota-only close if user cancellation or real time passes."""

        if dispatch_closed_reason is not None and not quota_close_is_soft():
            return
        prior_close = dispatch_closed_reason
        prior_time_reason = time_deadline_reason
        if next_is_cancelled():
            stop_dispatch("user_cancelled")
        else:
            expired = _v4_expired_deadline_reason(state)
            if expired is not None:
                stop_dispatch(expired, time_reason=expired)
        if (
            dispatch_closed_reason != prior_close
            or time_deadline_reason != prior_time_reason
        ):
            # Publish a real stop upgrade while admitted calls are still
            # draining so observers can distinguish the deadline from the
            # earlier quota-only close.
            publish_checkpoint()

    def process(outcome: _BatchCallOutcome) -> None:
        nonlocal progress_warning, consecutive_transport_failures, checkpoint_publications
        batch = outcome.batch
        row = rows[batch.batch_index]
        coverage_row = coverage_rows[batch.batch_index]
        if outcome.audit_artifact is not None:
            audit_artifacts.append(outcome.audit_artifact)
        attempt_rows = _v3_attempt_rows(outcome.attempt_records)
        admitted = any(item["provider_admitted"] is True for item in attempt_rows)
        attempt_reason_codes = {item.get("reason") for item in attempt_rows}
        if not admitted:
            attempt_rows = []
        retained = inherited_by_batch.get(batch.batch_index, [])
        accepted_new: list[dict[str, object]] = []
        rejected = restored = 0
        failure_reason: str | None = None
        if outcome.response_returned:
            try:
                parsed = parse_annual_discovery_response_v3(outcome.response_text, batch)
                rejected, restored = parsed.rejected_findings, parsed.restored_quotes
                seen_ids: set[str] = set()
                for parsed_finding in parsed.findings:
                    key = parsed_finding.duplicate_key()
                    finding_id = _finding_id(str(identity["source_sha256"]), key)
                    if finding_id in seen_ids:
                        continue
                    seen_ids.add(finding_id)
                    accepted_new.append({
                        "finding_id": finding_id,
                        "batch_index": batch.batch_index,
                        "title": parsed_finding.title,
                        "observation": parsed_finding.observation,
                        "why_it_matters": parsed_finding.why_it_matters,
                        "follow_up": parsed_finding.follow_up,
                        "priority": parsed_finding.priority,
                        "classification": "unverified_model_finding",
                        "verification_status": "unverified",
                        "evidence_ids": list(dict.fromkeys(evidence_id for evidence_id, _ in parsed_finding.citations)),
                        "citations": [
                            {"evidence_id": evidence_id, "quote": quote}
                            for evidence_id, quote in parsed_finding.citations
                        ],
                    })
                for limitation in parsed.limitations:
                    if limitation not in limitations:
                        limitations.append(limitation)
                for attempt in attempt_rows:
                    if attempt["response_accepted"] is True:
                        attempt["accepted_findings"] = len(accepted_new)
            except (ValueError, TypeError, KeyError) as exc:
                failure_reason = str(exc)
                if failure_reason not in _SAFE_CONTAINER_FAILURES:
                    failure_reason = "invalid_model_response"

        for finding in accepted_new:
            findings_by_id.setdefault(str(finding["finding_id"]), finding)
        retained_union = [
            item for item in findings_by_id.values() if item.get("batch_index") == batch.batch_index
        ]
        if outcome.response_returned and failure_reason is not None:
            row_state, reason = "failed", failure_reason
        elif outcome.response_returned:
            if rejected and accepted_new:
                row_state, reason = "partial", "some_findings_rejected"
            elif rejected and not accepted_new:
                row_state, reason = "failed", "all_findings_rejected"
            else:
                row_state, reason = "succeeded", None
        elif not admitted:
            gate_reason = None
            if "stage_budget_exhausted" in attempt_reason_codes:
                gate_reason = "discovery_stage_budget_exhausted"
            elif attempt_reason_codes & {"task_budget_exhausted", "ledger_budget_exhausted"}:
                gate_reason = "provider_budget_exhausted"
            elif "call_gate_closed" in attempt_reason_codes:
                gate_reason = "provider_admission_rejected"
            else:
                gate_reason = _v4_expired_deadline_reason(state)
            row_state, reason = "unattempted", (
                gate_reason
                or ("workflow_time_budget_exhausted" if outcome.failure_code == "model_call_timeout" and float(state["deadline"]) <= _monotonic_clock() else None)
                or "provider_admission_rejected"
            )
        else:
            row_state = "failed"
            fail_codes = [str(item.get("reason")) for item in attempt_rows]
            reason = "audit_write_failed" if "audit_write_failed" in fail_codes else (
                "model_call_timeout" if "timeout" in fail_codes else "model_call_failed"
            )

        row.update(state=row_state, resolution="unattempted" if row_state == "unattempted" else "executed", reason=reason, attempts=attempt_rows)
        coverage_rows[batch.batch_index] = _v3_coverage_row(
            batch, row_state, reason, str(row["resolution"]), len(retained_union),
            len(retained), rejected if row_state in {"partial", "failed"} else 0,
            restored if row_state in {"succeeded", "partial"} else 0,
        )
        if row_state in {"failed", "partial"}:
            limitation = "至少一个批次未通过完整引用与结构验证。"
            if limitation not in limitations:
                limitations.append(limitation)
        progress_warning = _emit_progress(
            progress_callback,
            _progress_event(
                "batch_finished", status={"succeeded": "completed", "partial": "partial", "failed": "failed", "unattempted": "running"}[row_state],
                batch_index=batch.batch_index, planned_batches=len(batches),
                processed_batches=sum(item["state"] != "unattempted" for item in rows.values()),
                partial_batches=sum(item["state"] == "partial" for item in rows.values()),
                failed_batches=sum(item["state"] == "failed" for item in rows.values()),
                total_pages=context.coverage.get("total_pages", 0), total_chars=context.coverage.get("total_chars", 0),
            ),
        ) or progress_warning
        reasons = attempt_reason_codes
        if reasons & {"http_401", "http_403"}:
            stop_dispatch("authentication_failure_stopped")
        elif "stage_budget_exhausted" in reasons:
            stop_dispatch("discovery_stage_budget_exhausted")
        elif reasons & {"ledger_budget_exhausted", "task_budget_exhausted"}:
            stop_dispatch("provider_budget_exhausted")
        elif "call_gate_closed" in reasons:
            stop_dispatch("provider_admission_rejected")
        elif reasons & {"call_deadline_exhausted"}:
            expired = _v4_expired_deadline_reason(state)
            if expired is not None:
                stop_dispatch(expired, time_reason=expired)
            else:
                stop_dispatch("provider_admission_rejected")
        elif reasons & {"ledger_provider_mismatch", "ledger_source_context_missing_or_unapproved", "ledger_unavailable", "ledger_policy_mismatch", "audit_write_failed"}:
            stop_dispatch("provider_admission_rejected")
        elif outcome.response_returned:
            consecutive_transport_failures = 0
        elif attempt_rows and all(item.get("reason") in {"http_429", "http_5xx", "connection_error", "timeout"} for item in attempt_rows):
            consecutive_transport_failures += sum(item.get("provider_admitted") is True for item in attempt_rows)
            if consecutive_transport_failures >= 3:
                stop_dispatch("consecutive_transport_failures_stopped")

        snapshot = publish()
        if callback is not None:
            callback(snapshot)
            checkpoint_publications += 1

    def submit_available(pool: ThreadPoolExecutor) -> None:
        nonlocal next_index, inflight_peak, progress_warning
        while dispatch_closed_reason is None and len(future_to_batch) < concurrency and next_index < len(batches):
            if next_is_cancelled():
                stop_dispatch("user_cancelled")
                return
            expired = _v4_expired_deadline_reason(state)
            if expired is not None:
                stop_dispatch(expired, time_reason=expired)
                return
            batch = batches[next_index]
            next_index += 1
            if rows[batch.batch_index]["resolution"] == "reused":
                if not call_gate.skip(batch.batch_index, float(state["deadline"])):
                    expired = _v4_expired_deadline_reason(state)
                    if expired is not None:
                        stop_dispatch(expired, time_reason=expired)
                    else:
                        stop_dispatch("provider_budget_exhausted")
                    return
                continue
            audit_id = str(uuid.uuid4())
            future = pool.submit(
                _call_batch, state, batch, audit_id, call_gate, scan_abort_event
            )
            future_to_batch[future] = batch
            inflight_peak = max(inflight_peak, len(future_to_batch))
            progress_warning = _emit_progress(
                progress_callback,
                _progress_event(
                    "batch_started", status="running", batch_index=batch.batch_index,
                    planned_batches=len(batches),
                    processed_batches=sum(item["state"] != "unattempted" for item in rows.values()),
                    partial_batches=sum(item["state"] == "partial" for item in rows.values()),
                    failed_batches=sum(item["state"] == "failed" for item in rows.values()),
                    total_pages=context.coverage.get("total_pages", 0), total_chars=context.coverage.get("total_chars", 0),
                ),
            ) or progress_warning

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="annual-discovery-v4") as pool:
        submit_available(pool)
        while future_to_batch:
            monitor_soft_close()
            remaining = float(state["deadline"]) - _monotonic_clock()
            done, _pending = wait(
                tuple(future_to_batch),
                timeout=min(0.1, max(0.0, remaining)),
                return_when=FIRST_COMPLETED,
            )
            # Check the deadline and user stop after wait as well, including
            # when a future became done at the same instant as the deadline.
            monitor_soft_close()
            if not done:
                continue
            for future in sorted(done, key=lambda item: future_to_batch[item].batch_index):
                batch = future_to_batch.pop(future)
                try:
                    outcome = future.result()
                except Exception:
                    outcome = _BatchCallOutcome(
                        batch, "", False, "model_call_failed", False, False,
                        AuditArtifact(batch.batch_index, None, None, None, None, "failed", PROMPT_VERSION),
                        completed_at=time.perf_counter(),
                    )
                if outcome.unattempted:
                    monitor_soft_close()
                    expired = _v4_expired_deadline_reason(state)
                    if dispatch_closed_reason is None and expired is not None:
                        stop_dispatch(expired, time_reason=expired)
                    unattempted_reason = dispatch_closed_reason or expired or "provider_budget_exhausted"
                    coverage_rows[batch.batch_index].update(status="unattempted", resolution="unattempted", reason=unattempted_reason)
                    rows[batch.batch_index].update(state="unattempted", resolution="unattempted", reason=unattempted_reason, attempts=[])
                    if dispatch_closed_reason is None:
                        stop_dispatch(unattempted_reason)
                else:
                    process(outcome)
            if dispatch_closed_reason is None:
                submit_available(pool)

    # Flush every remaining planned row through the one coordinator after all
    # admitted/started workers have drained.
    for batch in batches:
        if rows[batch.batch_index]["state"] == "unattempted" and rows[batch.batch_index]["reason"] == "awaiting_model":
            monitor_soft_close()
            expired = _v4_expired_deadline_reason(state) if dispatch_closed_reason is None else None
            stop_reason = dispatch_closed_reason or expired or "provider_budget_exhausted"
            if dispatch_closed_reason is None:
                stop_dispatch(
                    stop_reason,
                    time_reason=expired if expired is not None else None,
                )
            _set_v3_unattempted(rows[batch.batch_index], coverage_rows[batch.batch_index], stop_reason)
    if dispatch_closed_reason == "user_cancelled":
        limitation = "用户停止后未启动的新批次已保留为未尝试；已启动批次已收束并写入检查点。"
    elif dispatch_closed_reason == "workflow_time_budget_exhausted":
        limitation = "发现阶段时间预算耗尽；未尝试批次和页面已列入 coverage。"
    elif dispatch_closed_reason == "discovery_stage_budget_exhausted":
        limitation = "发现阶段自身预算耗尽；仅剩余发现批次停止，后续调查与评审预算仍独立可用。"
    elif dispatch_closed_reason == "provider_budget_exhausted":
        limitation = "发现阶段调用额度耗尽；未尝试批次已列入 coverage，后续阶段额度保持独立。"
    else:
        limitation = None
    if limitation and limitation not in limitations:
        limitations.append(limitation)
    if progress_warning and "progress_callback_failed" not in limitations:
        limitations.append("progress_callback_failed")

    final = publish()
    if callback is not None:
        callback(final)
        checkpoint_publications += 1
    final_contract = dict(final["execution_contract"])
    final["execution_contract"] = final_contract
    return {
        "findings": list(final["findings"]), "finding_keys": [],
        "limitations": list(final["limitations"]), "audit_artifacts": audit_artifacts,
        "coverage": dict(final["coverage"]), "reason": final["reason"],
        "overall_status": final["status"],
        "model_call_count": final["model_call_count"],
        "model_call_attempt_count": final["model_call_attempt_count"],
        "successful_batches": final["coverage"]["successful_batches"],
        "partial_batches": final["coverage"]["partial_batches"],
        "failed_batches": final["coverage"]["failed_batches"],
        "processed_batches": final["coverage"]["processed_batches"],
        "processed_chars": final["coverage"]["processed_chars"],
        "attempted_chars": final["coverage"]["attempted_chars"],
        "execution_contract": final_contract,
        "not_called_reason": final["reason"] if final["status"] == "not_called" else None,
        "budget_exhausted": dispatch_closed_reason in {"discovery_stage_budget_exhausted", "workflow_time_budget_exhausted"},
        "progress_warning": progress_warning,
    }


def _call_batch(
    state: AnnualDiscoveryState,
    batch: DiscoveryBatch,
    audit_id: str,
    gate: _OrderedCallGate,
    abort_event: Event | None = None,
) -> _BatchCallOutcome:
    identity = state["identity"]
    try:
        user_content = serialize_batch_content(
            identity,
            batch.batch_index,
            batch.fragments,
            plan_version=batch.plan_version,
            aliases=batch.aliases,
        )
        size = serialized_request_size(state["settings"].model, state["prompt_text"], user_content)
    except Exception:
        if gate.enter(batch.batch_index, float(state["deadline"])) is None:
            return _unattempted_outcome(batch)
        return _BatchCallOutcome(
            batch, "", False, "invalid_model_response", False, False, None,
            completed_at=time.perf_counter(),
        )
    if size > MAX_SERIALIZED_INPUT_CHARS:
        if gate.enter(batch.batch_index, float(state["deadline"])) is None:
            return _unattempted_outcome(batch)
        return _BatchCallOutcome(
            batch, "", False, "serialized_input_limit_breached", False, False, None,
            completed_at=time.perf_counter(),
        )
    remaining_seconds = gate.enter(batch.batch_index, float(state["deadline"]))
    if remaining_seconds is None:
        return _unattempted_outcome(batch)

    messages = [
        ChatMessage(role="system", content=state["prompt_text"]),
        ChatMessage(role="user", content=user_content),
    ]
    pages = sorted({item.pdf_page for item in batch.fragments})
    audit_dir = Path(state["run_dir"]) / audit_id
    try:
        result = audited_complete_chat(
            messages,
            state["settings"],
            run_dir=Path(state["run_dir"]),
            evidence_refs=[
                EvidenceRef(str(identity["source_document_id"]), page) for page in pages
            ],
            prompt_version=str(state.get("prompt_version", LEGACY_PROMPT_VERSION)),
            timeout_seconds=min(state["settings"].timeout_seconds, remaining_seconds),
            audit_id=audit_id,
            call_stage="annual_discovery",
            batch_index=batch.batch_index,
            abort_event=abort_event,
        )
    except Exception as exc:
        artifact = (
            _audit_artifact_from_dir(audit_dir, batch.batch_index, _audit_dir_status(audit_dir), str(state.get("prompt_version", LEGACY_PROMPT_VERSION)))
            if audit_dir.is_dir()
            else AuditArtifact(batch.batch_index, None, None, None, None, "failed", str(state.get("prompt_version", LEGACY_PROMPT_VERSION)))
        )
        return _BatchCallOutcome(
            batch=batch,
            response_text="",
            call_succeeded=False,
            failure_code="model_call_timeout" if _is_timeout_exception(exc) else "model_call_failed",
            attempted=True,
            response_returned=False,
            audit_artifact=artifact,
            completed_at=time.perf_counter(),
            attempt_records=tuple(
                item for item in getattr(exc, "attempt_records", ()) if isinstance(item, Mapping)
            ),
        )

    return _BatchCallOutcome(
        batch=batch,
        response_text=result.completion.text,
        call_succeeded=True,
        failure_code=None,
        attempted=True,
        response_returned=True,
        audit_artifact=_audit_artifact_from_dir(
            result.audit_dir, batch.batch_index, "succeeded", str(state.get("prompt_version", LEGACY_PROMPT_VERSION))
        ),
        completed_at=time.perf_counter(),
        attempt_records=tuple(result.attempt_records),
    )


def _unattempted_outcome(batch: DiscoveryBatch) -> _BatchCallOutcome:
    return _BatchCallOutcome(
        batch=batch,
        response_text="",
        call_succeeded=False,
        failure_code=None,
        attempted=False,
        response_returned=False,
        audit_artifact=None,
        completed_at=time.perf_counter(),
        unattempted=True,
    )


def _future_completion_key(future: Future[_BatchCallOutcome], batch_index: int) -> tuple[float, int]:
    try:
        return future.result().completed_at, batch_index
    except Exception:
        return float("inf"), batch_index


def _process_batch_outcome(
    base_state: AnnualDiscoveryState, outcome: _BatchCallOutcome
) -> dict[str, object]:
    batch = outcome.batch
    local_state: dict[str, object] = dict(base_state)
    local_state.update(
        {
            "current_batch": batch,
            "response_text": outcome.response_text,
            "call_succeeded": outcome.call_succeeded,
            "failure_code": outcome.failure_code,
            "processed_batches": 0,
            "successful_batches": 0,
            "partial_batches": 0,
            "failed_batches": 0,
            "processed_chars": 0,
            "attempted_chars": batch.raw_chars if outcome.attempted else 0,
            "findings": [],
            "finding_keys": [],
            "audit_artifacts": [],
            "coverage": dict(base_state.get("coverage", {})),
            "progress_callback": None,
            "progress_warning": False,
        }
    )
    local_state["coverage"]["batch_results"] = []
    updates = _parse_check_node(local_state)  # type: ignore[arg-type]
    parsed = {**local_state, **updates}
    parsed["audit_artifact"] = outcome.audit_artifact
    parsed["attempted"] = outcome.attempted
    parsed["response_returned"] = outcome.response_returned
    return parsed


def _parse_check_node(state: AnnualDiscoveryState) -> dict[str, object]:
    batch = state.get("current_batch")
    if batch is None:
        return _finish_failed_batch(state, "batch_missing")
    if not state.get("call_succeeded", False):
        return _finish_failed_batch(state, state.get("failure_code") or "model_call_failed")

    try:
        parsed_result = (
            parse_annual_discovery_response_v3(state.get("response_text"), batch)
            if batch.plan_version == COMPACT_PLAN_VERSION
            else parse_annual_discovery_response_v2(state.get("response_text"), batch)
        )
    except (ValueError, TypeError, KeyError) as exc:
        code = str(exc)
        safe_code = code if code in _SAFE_CONTAINER_FAILURES else "invalid_model_response"
        return _finish_failed_batch(state, safe_code)
    if not parsed_result.findings and parsed_result.rejected_findings:
        return _finish_failed_batch(
            state,
            "all_findings_rejected",
            rejected_findings=parsed_result.rejected_findings,
        )

    findings = list(state.get("findings", []))
    finding_keys = list(state.get("finding_keys", []))
    for parsed in parsed_result.findings:
        key = parsed.duplicate_key()
        if key in finding_keys:
            continue
        finding_keys.append(key)
        finding_id = _finding_id(str(state["identity"]["source_sha256"]), key)
        evidence_ids = list(dict.fromkeys(evidence_id for evidence_id, _quote in parsed.citations))
        findings.append(
            {
                "finding_id": finding_id,
                **({"batch_index": batch.batch_index} if state.get("execution_binding") is not None else {}),
                "title": parsed.title,
                "observation": parsed.observation,
                "why_it_matters": parsed.why_it_matters,
                "follow_up": parsed.follow_up,
                "priority": parsed.priority,
                "classification": "unverified_model_finding",
                "verification_status": "unverified",
                "evidence_ids": evidence_ids,
                "citations": [
                    {"evidence_id": evidence_id, "quote": quote}
                    for evidence_id, quote in parsed.citations
                ],
            }
        )

    merged_limitations = list(state.get("limitations", []))
    for limitation in parsed_result.limitations:
        if limitation not in merged_limitations:
            merged_limitations.append(limitation)
    if parsed_result.rejected_findings:
        return _finish_partial_batch(
            state,
            findings=findings,
            finding_keys=finding_keys,
            limitations=merged_limitations,
            accepted_findings=len(parsed_result.findings),
            rejected_findings=parsed_result.rejected_findings,
            restored_quotes=parsed_result.restored_quotes,
        )

    coverage = dict(state.get("coverage", {}))
    processed = state.get("processed_batches", 0) + 1
    chars = state.get("processed_chars", 0) + batch.raw_chars
    successful = state.get("successful_batches", 0) + 1
    coverage.update(
        {
            "processed_batches": processed,
            "successful_batches": successful,
            "partial_batches": state.get("partial_batches", 0),
            "failed_batches": state.get("failed_batches", 0),
            "processed_chars": chars,
            "attempted_chars": state.get("attempted_chars", 0),
            "batch_results": list(state.get("coverage", {}).get("batch_results", []))
            + [
                _batch_result(
                    batch,
                    "succeeded",
                    None,
                    accepted_findings=len(parsed_result.findings),
                    restored_quotes=parsed_result.restored_quotes,
                )
            ],
        }
    )
    coverage["complete"] = coverage_is_complete(coverage)
    warning = _emit_progress(
        state.get("progress_callback"),
        _progress_event(
            "batch_finished",
            status="completed",
            batch_index=batch.batch_index,
            planned_batches=len(state.get("batches", [])),
            processed_batches=processed,
            partial_batches=state.get("partial_batches", 0),
            failed_batches=state.get("failed_batches", 0),
            total_pages=coverage.get("total_pages", 0),
            total_chars=coverage.get("total_chars", 0),
        ),
    )
    if warning and "progress_callback_failed" not in merged_limitations:
        merged_limitations.append("progress_callback_failed")
    return {
        "findings": findings,
        "finding_keys": finding_keys,
        "limitations": merged_limitations,
        "processed_batches": processed,
        "successful_batches": successful,
        "processed_chars": chars,
        "coverage": coverage,
        "reason": state.get("reason"),
        "progress_warning": state.get("progress_warning", False) or warning,
    }


def _finish_partial_batch(
    state: AnnualDiscoveryState,
    *,
    findings: list[dict[str, object]],
    finding_keys: list[str],
    limitations: list[str],
    accepted_findings: int,
    rejected_findings: int,
    restored_quotes: int,
) -> dict[str, object]:
    batch = state.get("current_batch")
    if batch is None:
        return _finish_failed_batch(state, "batch_missing")
    processed = state.get("processed_batches", 0) + 1
    partial = state.get("partial_batches", 0) + 1
    coverage = dict(state.get("coverage", {}))
    coverage.update(
        {
            "processed_batches": processed,
            "successful_batches": state.get("successful_batches", 0),
            "partial_batches": partial,
            "failed_batches": state.get("failed_batches", 0),
            "processed_chars": state.get("processed_chars", 0),
            "attempted_chars": state.get("attempted_chars", 0),
            "batch_results": list(coverage.get("batch_results", []))
            + [
                _batch_result(
                    batch,
                    "partial",
                    "some_findings_rejected",
                    accepted_findings=accepted_findings,
                    rejected_findings=rejected_findings,
                    restored_quotes=restored_quotes,
                )
            ],
        }
    )
    unread_pages = set(coverage.get("unread_pages", []))
    unread_pages.update(fragment.pdf_page for fragment in batch.fragments)
    coverage["unread_pages"] = sorted(unread_pages)
    coverage["complete"] = coverage_is_complete(coverage)
    partial_limitation = "部分发现条目未通过严格字段或原文引用检查；该批次不计入完整处理字符。"
    if partial_limitation not in limitations:
        limitations.append(partial_limitation)
    warning = _emit_progress(
        state.get("progress_callback"),
        _progress_event(
            "batch_finished",
            status="partial",
            batch_index=batch.batch_index,
            planned_batches=len(state.get("batches", [])),
            processed_batches=processed,
            partial_batches=partial,
            failed_batches=state.get("failed_batches", 0),
            total_pages=coverage.get("total_pages", 0),
            total_chars=coverage.get("total_chars", 0),
        ),
    )
    if warning and "progress_callback_failed" not in limitations:
        limitations.append("progress_callback_failed")
    return {
        "findings": findings,
        "finding_keys": finding_keys,
        "limitations": limitations,
        "processed_batches": processed,
        "successful_batches": state.get("successful_batches", 0),
        "partial_batches": partial,
        "failed_batches": state.get("failed_batches", 0),
        "processed_chars": state.get("processed_chars", 0),
        "coverage": coverage,
        "reason": state.get("reason") or "some_findings_rejected",
        "progress_warning": state.get("progress_warning", False) or warning,
    }


def _finish_failed_batch(
    state: AnnualDiscoveryState,
    failure_code: str,
    *,
    rejected_findings: int = 0,
) -> dict[str, object]:
    batch = state.get("current_batch")
    safe_failure_code = failure_code if failure_code in _SAFE_BATCH_FAILURES else "invalid_model_response"
    processed = state.get("processed_batches", 0) + 1
    failed = state.get("failed_batches", 0) + 1
    coverage = dict(state.get("coverage", {}))
    batch_results = list(coverage.get("batch_results", []))
    if batch is not None:
        batch_results.append(
            _batch_result(
                batch,
                "failed",
                safe_failure_code,
                rejected_findings=rejected_findings,
            )
        )
    coverage.update(
        {
            "processed_batches": processed,
            "successful_batches": state.get("successful_batches", 0),
            "partial_batches": state.get("partial_batches", 0),
            "failed_batches": failed,
            "processed_chars": state.get("processed_chars", 0),
            "attempted_chars": state.get("attempted_chars", 0),
            "batch_results": batch_results,
        }
    )
    failed_pages = set(coverage.get("failed_pages", []))
    if batch is not None:
        failed_pages.update(fragment.pdf_page for fragment in batch.fragments)
    coverage["failed_pages"] = sorted(failed_pages)
    unread_pages = set(coverage.get("unread_pages", []))
    unread_pages.update(failed_pages)
    coverage["unread_pages"] = sorted(unread_pages)
    coverage["complete"] = coverage_is_complete(coverage)
    limitations = list(state.get("limitations", []))
    safe_limitation = (
        "至少一个批次的模型调用失败。"
        if safe_failure_code in {"model_call_failed", "model_call_timeout"}
        else "至少一个批次的模型回应未通过严格结构或引用检查。"
    )
    if safe_limitation not in limitations:
        limitations.append(safe_limitation)
    warning = _emit_progress(
        state.get("progress_callback"),
        _progress_event(
            "batch_finished",
            status="failed",
            batch_index=batch.batch_index if batch else state.get("current_index", 0) + 1,
            planned_batches=len(state.get("batches", [])),
            processed_batches=processed,
            partial_batches=state.get("partial_batches", 0),
            failed_batches=failed,
            total_pages=coverage.get("total_pages", 0),
            total_chars=coverage.get("total_chars", 0),
        ),
    )
    if warning and "progress_callback_failed" not in limitations:
        limitations.append("progress_callback_failed")
    return {
        "processed_batches": processed,
        "successful_batches": state.get("successful_batches", 0),
        "partial_batches": state.get("partial_batches", 0),
        "failed_batches": failed,
        "processed_chars": state.get("processed_chars", 0),
        "coverage": coverage,
        "limitations": limitations,
        "reason": state.get("reason") or safe_failure_code,
        "progress_warning": state.get("progress_warning", False) or warning,
    }


def _finish_node(state: AnnualDiscoveryState) -> dict[str, object]:
    if isinstance(state.get("execution_contract"), Mapping):
        # The v3 scanner has already reconciled per-attempt rows, retained
        # parent findings, and full batch coverage. Preserve that exact state;
        # legacy prefix-based finishing rules apply only to v1/v2.
        return {
            "coverage": dict(state.get("coverage", empty_coverage())),
            "overall_status": state.get("overall_status", "failed"),
            "reason": state.get("reason"),
            "limitations": list(state.get("limitations", [])),
        }
    coverage = dict(state.get("coverage", empty_coverage()))
    coverage.update(
        {
            "processed_batches": state.get("processed_batches", 0),
            "successful_batches": state.get("successful_batches", 0),
            "partial_batches": state.get("partial_batches", 0),
            "failed_batches": state.get("failed_batches", 0),
            "processed_chars": state.get("processed_chars", 0),
            "attempted_chars": state.get("attempted_chars", 0),
        }
    )
    coverage["complete"] = coverage_is_complete(coverage)
    if state.get("overall_status") == "not_called":
        status = "not_called"
        reason = state.get("not_called_reason") or state.get("reason") or "not_called"
    elif state.get("overall_status") == "budget_exhausted":
        if state.get("model_call_attempt_count", 0) == 0:
            status = "not_called"
        elif (
            state.get("successful_batches", 0) == 0
            and state.get("partial_batches", 0) == 0
            and not state.get("findings", [])
        ):
            status = "failed"
        else:
            status = "partial"
        reason = "workflow_time_budget_exhausted"
    elif (
        state.get("successful_batches", 0) == 0
        and state.get("partial_batches", 0) == 0
        and state.get("failed_batches", 0) > 0
    ):
        status = "failed"
        reason = state.get("reason") or "all_batches_failed"
    elif (
        state.get("partial_batches", 0) > 0
        or state.get("failed_batches", 0) > 0
        or not coverage.get("complete")
    ):
        status = "partial"
        reason = state.get("reason") or (
            "batch_failures" if state.get("failed_batches", 0) else "coverage_incomplete"
        )
    else:
        status = "completed"
        reason = None

    limitations = list(state.get("limitations", []))
    if status in {"partial", "failed"} and "coverage.complete 只表示所有可提取文字批次已成功处理。" not in limitations:
        limitations.append("coverage.complete 只表示所有可提取文字批次已成功处理。")
    return {
        "coverage": coverage,
        "overall_status": status,
        "reason": reason,
        "limitations": limitations,
    }


def _result_from_state(state: Mapping[str, object]) -> AnnualDiscoveryResult:
    identity = state["identity"]
    assert isinstance(identity, Mapping)
    fragments_by_id = state.get("fragments_by_id", {})
    if not isinstance(fragments_by_id, Mapping):
        fragments_by_id = {}
    findings = tuple(item for item in state.get("findings", []) if isinstance(item, Mapping))
    referenced_ids = {
        evidence_id
        for finding in findings
        for evidence_id in finding.get("evidence_ids", [])
        if isinstance(evidence_id, str)
    }
    evidences = tuple(
        fragments_by_id[evidence_id].evidence_value(
            str(identity["source_document_id"]), str(identity["source_sha256"])
        )
        for evidence_id in sorted(referenced_ids)
        if evidence_id in fragments_by_id and isinstance(fragments_by_id[evidence_id], DiscoveryFragment)
    )
    status = state.get("overall_status", "not_called")
    if status not in {"completed", "partial", "failed", "not_called"}:
        status = "failed"
    return AnnualDiscoveryResult(
        run_id=str(identity["run_id"]),
        company_id=str(identity["company_id"]),
        report_year=int(identity["report_year"]),
        source_document_id=str(identity["source_document_id"]),
        source_sha256=str(identity["source_sha256"]),
        status=status,  # type: ignore[arg-type]
        prompt_version=str(state.get("prompt_version", LEGACY_PROMPT_VERSION)),
        model_called=int(state.get("model_call_attempt_count", 0)) > 0,
        model_call_count=int(state.get("model_call_count", 0)),
        model_call_attempt_count=int(state.get("model_call_attempt_count", 0)),
        coverage=state.get("coverage", empty_coverage()),
        findings=findings,
        evidences=evidences,
        audit_artifacts=tuple(
            item for item in state.get("audit_artifacts", []) if isinstance(item, AuditArtifact)
        ),
        limitations=tuple(
            item for item in state.get("limitations", []) if isinstance(item, str)
        ),
        reason=state.get("reason") if isinstance(state.get("reason"), str) else None,
        execution_contract=(
            state.get("execution_contract")
            if isinstance(state.get("execution_contract"), Mapping)
            else None
        ),
    )


def _bounded_opinion_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field}_invalid")
    normalized = value.strip()
    if _DIGIT_RE.search(normalized):
        raise ValueError(f"{field}_contains_unquoted_digit")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{field}_contains_control_character")
    return normalized


def _reject_definitive_conclusion(values: Sequence[str]) -> None:
    # Keep fields separate so a suffix in one field cannot combine with a
    # prefix in another to manufacture a forbidden phrase.
    for value in values:
        normalized = re.sub(r"\s+", "", value).lower()
        for match in re.finditer(r"(?<![a-z])verified(?![a-z])", value, flags=re.IGNORECASE):
            prefix = _preceding_clause(value, match.start(), english=True)[-24:].lower()
            if not re.search(
                r"\b(?:not|never|without|cannot|can't|unable\s+to|not\s+able\s+to)\b",
                prefix,
            ):
                raise ValueError("definitive_conclusion_forbidden")
        english_fraud = re.compile(
            r"\b(?:confirmed\s+fraud|fraud\s+(?:(?:is|was|has\s+been)\s+)?(?:confirmed|proven)|"
            r"no\s+(?:evidence\s+of\s+|confirmed\s+)?fraud|fraud\s+(?:(?:is|was|has\s+been)\s+)?ruled\s+out|"
            r"ruled\s+out\s+fraud|fraud\s+(?:(?:is|was)\s+)?excluded)\b",
            flags=re.IGNORECASE,
        )
        for match in english_fraud.finditer(value):
            prefix = _preceding_clause(value, match.start(), english=True)[-32:].lower()
            if not re.search(
                r"\b(?:not|never|cannot|can't|unable\s+to|not\s+able\s+to)\b", prefix
            ):
                raise ValueError("definitive_conclusion_forbidden")

        affirmative = (
            "已核实",
            "经核实",
            "独立核实",
            "已验证",
            "核验通过",
            "确认存在舞弊",
            "确认舞弊",
            "存在舞弊",
            "构成舞弊",
            "证实舞弊",
            "财务造假已确认",
            "不存在舞弊",
            "没有舞弊",
            "排除舞弊",
            "排除财务造假",
            "舞弊已排除",
            "财务造假已排除",
        )
        if any(_has_unnegated_phrase(normalized, phrase) for phrase in affirmative):
            raise ValueError("definitive_conclusion_forbidden")


def _has_unnegated_phrase(text: str, phrase: str) -> bool:
    negators = ("尚不能", "未能", "无法", "不能", "未经", "并未", "未", "不表示", "不", "没有")
    cursor = 0
    while True:
        index = text.find(phrase, cursor)
        if index < 0:
            return False
        prefix = _preceding_clause(text, index, english=False)[-12:]
        if not any(negator in prefix for negator in negators):
            return True
        cursor = index + len(phrase)


def _preceding_clause(text: str, index: int, *, english: bool) -> str:
    """Return the local prefix after the most recent punctuation/contrast boundary."""

    prefix = text[:index]
    separators = (
        r"[,，;；。！？\n]|\b(?:but|however|yet|now)\b"
        if english
        else r"[,，;；。！？\n]|但是|然而|不过|但"
    )
    matches = list(re.finditer(separators, prefix, flags=re.IGNORECASE if english else 0))
    if not matches:
        return prefix
    return prefix[matches[-1].end() :]


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey("duplicate_json_key")
        result[key] = value
    return result


def _finding_id(source_sha256: str, duplicate_key: str) -> str:
    digest = hashlib.sha256((source_sha256 + "\n" + duplicate_key).encode("utf-8")).hexdigest()[:20]
    return f"disc:{source_sha256[:12]}-{digest}"


def _load_prompt(version: str = LEGACY_PROMPT_VERSION) -> str:
    if version not in {LEGACY_PROMPT_VERSION, PROMPT_VERSION}:
        raise RuntimeError("年度开放发现提示词版本不受支持。")
    prompt_path = _PROMPT_ROOT / f"{version}.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError:
        raise RuntimeError("年度开放发现提示词文件不可读取。") from None
    if not prompt.strip():
        raise RuntimeError("年度开放发现提示词文件为空。")
    return prompt


def _validate_run_dir(run_dir: Path, run_id: str) -> None:
    root = repository_runs_root()
    if not isinstance(root, Path) or _is_link(run_dir) or not run_dir.is_dir():
        raise ValueError("run_dir 必须是 report.run_id 对应的已存在 artifacts/runs 直接子目录。")
    try:
        root_resolved = root.resolve()
        if (
            run_dir.name != run_id
            or run_dir.parent.resolve() != root_resolved
            or run_dir.resolve() != root_resolved / run_id
        ):
            raise ValueError("run_dir 必须与 report.run_id 对应的 artifacts/runs 子目录一致。")
    except OSError:
        raise ValueError("run_dir 不可访问。") from None


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & _RUN_REPARSE_POINT)
    except OSError:
        return False


def _recursion_limit(planned_batches: int) -> int:
    bounded = min(max(planned_batches, 0), MAX_BATCHES)
    return max(25, bounded * 5 + 10)


def _progress_event(
    event: str,
    *,
    status: str,
    batch_index: int,
    planned_batches: int,
    processed_batches: int,
    partial_batches: int = 0,
    failed_batches: int,
    total_pages: object,
    total_chars: object,
) -> dict[str, object]:
    # No source text, evidence quotes, paths, model settings, or exception data.
    return {
        "event": event,
        "status": status,
        "batch_index": batch_index,
        "planned_batches": planned_batches,
        "processed_batches": processed_batches,
        "successful_batches": max(processed_batches - partial_batches - failed_batches, 0),
        "partial_batches": partial_batches,
        "failed_batches": failed_batches,
        "total_pages": total_pages,
        "total_chars": total_chars,
    }


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    # ModelResponseError messages are fixed by the local transport adapter and
    # never include provider response bodies, request URLs, or exception text.
    return isinstance(exc, ModelResponseError) and str(exc) == "模型服务请求超时。"


def _batch_result(
    batch: DiscoveryBatch,
    status: str,
    reason: str | None,
    *,
    accepted_findings: int = 0,
    rejected_findings: int = 0,
    restored_quotes: int = 0,
) -> dict[str, object]:
    return {
        "batch_index": batch.batch_index,
        "status": status,
        "reason": reason,
        "pages": sorted({fragment.pdf_page for fragment in batch.fragments}),
        "raw_chars": batch.raw_chars,
        "accepted_findings": accepted_findings,
        "rejected_findings": rejected_findings,
        "restored_quotes": restored_quotes,
    }


def _emit_progress(
    callback: Callable[[dict[str, object]], None] | None, event: dict[str, object]
) -> bool:
    if callback is None:
        return False
    try:
        callback(dict(event))
    except Exception:
        return True
    return False


def _audit_artifact_from_dir(
    path: Path,
    batch_index: int,
    status: str,
    prompt_version: str = LEGACY_PROMPT_VERSION,
) -> AuditArtifact:
    root = repository_runs_root()
    try:
        relative = path.resolve().relative_to(root.resolve().parent.parent).as_posix()
    except (OSError, ValueError):
        relative = (Path("artifacts") / "runs" / path.parent.name / path.name).as_posix()
    base = Path(relative)
    request = (path / "request.json").is_file()
    response = (path / "response.json").is_file()
    failure = (path / "failure.json").is_file()
    return AuditArtifact(
        batch_index=batch_index,
        audit_dir=relative,
        request_path=(base / "request.json").as_posix() if request else None,
        response_path=(base / "response.json").as_posix() if response else None,
        failure_path=(base / "failure.json").as_posix() if failure else None,
        status="succeeded" if response else ("failed" if failure else status),
        prompt_version=prompt_version,
    )


def _audit_dir_status(path: Path) -> str:
    if (path / "response.json").is_file():
        return "succeeded"
    if (path / "failure.json").is_file():
        return "failed"
    if (path / "request.json").is_file():
        return "started"
    return "incomplete"
