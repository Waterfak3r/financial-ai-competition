"""Open-ended, source-bound discovery of unverified annual-report leads.

This workflow is independent of fixed FINTRACE candidates and confirmed facts.
It scans bounded batches of all extractable text from one original PDF, checks
every model citation against the exact submitted fragment, and never upgrades a
model observation into an independently verified fact or fraud conclusion.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from finagent.audit import EvidenceRef, audited_complete_chat
from finagent.audit.audited_chat import repository_runs_root
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage
from finagent.retrieval.annual_discovery_context import (
    MAX_BATCHES,
    MAX_SERIALIZED_INPUT_CHARS,
    AnnualDiscoveryContext,
    AnnualDiscoveryInputError,
    AnnualDiscoverySourceError,
    DiscoveryBatch,
    DiscoveryFragment,
    build_annual_discovery_context,
    coverage_is_complete,
    empty_coverage,
    serialize_batch_content,
    serialized_request_size,
    validate_annual_discovery_identity,
)

PROMPT_VERSION = "annual_discovery_v1"
MAX_FINDINGS_PER_BATCH = 6
MAX_CITATIONS_PER_FINDING = 4
MAX_QUOTE_CHARS = 300
MAX_MODEL_RESPONSE_CHARS = 80000
MAX_WORKFLOW_SECONDS = 1800.0
_monotonic_clock = time.monotonic
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_discovery_v1.md"
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

    def to_dict(self) -> dict[str, object]:
        return {
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


@dataclass(frozen=True, slots=True)
class _ParsedFinding:
    title: str
    observation: str
    why_it_matters: str
    follow_up: str
    priority: str
    citations: tuple[tuple[str, str], ...]

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
    run_dir: str
    identity: Mapping[str, object]
    prompt_text: str
    progress_callback: Callable[[dict[str, object]], None] | None
    deadline: float
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


def run_annual_discovery(
    report: Mapping[str, object],
    source_pdf_path: str | Path,
    source_record: Mapping[str, object],
    settings: ModelSettings,
    *,
    run_dir: Path,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> AnnualDiscoveryResult:
    """Scan one source-bound annual PDF for open-ended unverified leads.

    Settings are supplied by the caller. This entry point does not read .env,
    contact a model until a planned batch is processed, or mutate the report.
    The supplied run directory must be the exact repository run for report.run_id.
    """

    identity = validate_annual_discovery_identity(report, source_record)
    _validate_run_dir(Path(run_dir), str(identity["run_id"]))
    prompt_text = _load_prompt()
    graph = build_annual_discovery_graph()
    state = graph.invoke(
        {
            "report": report,
            "source_pdf_path": str(Path(source_pdf_path)),
            "source_record": source_record,
            "settings": settings,
            "run_dir": str(Path(run_dir).resolve()),
            "identity": identity,
            "prompt_text": prompt_text,
            "progress_callback": progress_callback,
            "deadline": _monotonic_clock() + MAX_WORKFLOW_SECONDS,
            "current_index": 0,
            "model_call_count": 0,
            "model_call_attempt_count": 0,
            "processed_batches": 0,
            "successful_batches": 0,
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


def build_annual_discovery_graph():
    """Compile the LangGraph plan → batch call → check → advance workflow."""

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise LangGraphUnavailableError(
            "年度开放发现图需要可选依赖 langgraph>=1.2,<1.3。"
        ) from exc

    workflow = StateGraph(AnnualDiscoveryState)
    workflow.add_node("plan", _plan_node)
    workflow.add_node("select_batch", _select_batch_node)
    workflow.add_node("batch_call", _batch_call_node)
    workflow.add_node("parse_check", _parse_check_node)
    workflow.add_node("advance", _advance_node)
    workflow.add_node("finish", _finish_node)
    workflow.add_edge(START, "plan")
    workflow.add_edge("plan", "select_batch")
    workflow.add_conditional_edges(
        "select_batch",
        _after_select_batch,
        {"call": "batch_call", "finish": "finish"},
    )
    workflow.add_conditional_edges(
        "batch_call",
        _after_batch_call,
        {"check": "parse_check", "finish": "finish"},
    )
    workflow.add_edge("parse_check", "advance")
    workflow.add_edge("advance", "select_batch")
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


def _plan_node(state: AnnualDiscoveryState) -> dict[str, object]:
    try:
        context = build_annual_discovery_context(
            state["report"],
            state["source_pdf_path"],
            state["source_record"],
            system_prompt=state["prompt_text"],
            model_name=state["settings"].model,
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


def _select_batch_node(state: AnnualDiscoveryState) -> dict[str, object]:
    batches = state.get("batches", [])
    index = state.get("current_index", 0)
    if index >= len(batches):
        return {"current_batch": None}
    batch = batches[index]
    return {
        "current_batch": batch,
        "response_text": "",
        "call_succeeded": False,
        "failure_code": None,
    }


def _after_select_batch(state: AnnualDiscoveryState) -> str:
    if state.get("current_batch") is None:
        return "finish"
    if state.get("overall_status") == "not_called":
        return "finish"
    return "call"


def _after_batch_call(state: AnnualDiscoveryState) -> str:
    return "finish" if state.get("budget_exhausted", False) else "check"


def _batch_call_node(state: AnnualDiscoveryState) -> dict[str, object]:
    batch = state.get("current_batch")
    if batch is None:
        return {"call_succeeded": False, "failure_code": "batch_missing"}
    identity = state["identity"]
    user_content = serialize_batch_content(identity, batch.batch_index, batch.fragments)
    size = serialized_request_size(state["settings"].model, state["prompt_text"], user_content)
    if size > MAX_SERIALIZED_INPUT_CHARS:
        return {
            "model_call_attempt_count": state.get("model_call_attempt_count", 0),
            "call_succeeded": False,
            "failure_code": "serialized_input_limit_breached",
        }

    remaining_seconds = float(state["deadline"]) - _monotonic_clock()
    if remaining_seconds <= 0:
        batches = state.get("batches", [])
        index = state.get("current_index", 0)
        unattempted = batches[index:]
        coverage = dict(state.get("coverage", {}))
        omitted_chars = sum(item.raw_chars for item in unattempted)
        unread_pages = set(coverage.get("unread_pages", []))
        unread_pages.update(
            fragment.pdf_page for pending in unattempted for fragment in pending.fragments
        )
        prior_results = list(coverage.get("batch_results", []))
        known_indices = {
            item.get("batch_index")
            for item in prior_results
            if isinstance(item, Mapping)
        }
        for pending in unattempted:
            if pending.batch_index not in known_indices:
                prior_results.append(
                    _batch_result(pending, "unattempted", "workflow_time_budget_exhausted")
                )
        coverage.update(
            {
                "unattempted_batches": len(unattempted),
                "unattempted_chars": omitted_chars,
                "omitted_chars": int(coverage.get("omitted_chars", 0)) + omitted_chars,
                "unread_pages": sorted(unread_pages),
                "batch_results": prior_results,
                "truncated": True,
                "time_budget_exhausted": True,
            }
        )
        coverage["complete"] = coverage_is_complete(coverage)
        attempts = state.get("model_call_attempt_count", 0)
        limitations = list(state.get("limitations", []))
        if "总体发现时间预算耗尽；未尝试批次和页面已列入 coverage。" not in limitations:
            limitations.append("总体发现时间预算耗尽；未尝试批次和页面已列入 coverage。")
        terminal_status = "partial" if attempts > 0 else "not_called"
        warning = _emit_progress(
            state.get("progress_callback"),
            _progress_event(
                "budget_exhausted",
                status=terminal_status,
                batch_index=batch.batch_index,
                planned_batches=len(batches),
                processed_batches=state.get("processed_batches", 0),
                failed_batches=state.get("failed_batches", 0),
                total_pages=coverage.get("total_pages", 0),
                total_chars=coverage.get("total_chars", 0),
            ),
        )
        if warning and "progress_callback_failed" not in limitations:
            limitations.append("progress_callback_failed")
        return {
            "budget_exhausted": True,
            "coverage": coverage,
            "reason": "workflow_time_budget_exhausted",
            "overall_status": "budget_exhausted",
            "not_called_reason": "workflow_time_budget_exhausted" if attempts == 0 else None,
            "limitations": limitations,
            "progress_warning": state.get("progress_warning", False) or warning,
        }

    messages = [
        ChatMessage(role="system", content=state["prompt_text"]),
        ChatMessage(role="user", content=user_content),
    ]
    pages = sorted({item.pdf_page for item in batch.fragments})
    before = _run_dir_children(Path(state["run_dir"]))
    event = _progress_event(
        "batch_started",
        status="running",
        batch_index=batch.batch_index,
        planned_batches=len(state.get("batches", [])),
        processed_batches=state.get("processed_batches", 0),
        failed_batches=state.get("failed_batches", 0),
        total_pages=state.get("coverage", {}).get("total_pages", 0),
        total_chars=state.get("coverage", {}).get("total_chars", 0),
    )
    callback_warning = _emit_progress(state.get("progress_callback"), event)

    attempts = state.get("model_call_attempt_count", 0) + 1
    try:
        result = audited_complete_chat(
            messages,
            state["settings"],
            run_dir=Path(state["run_dir"]),
            evidence_refs=[
                EvidenceRef(str(identity["source_document_id"]), page) for page in pages
            ],
            prompt_version=PROMPT_VERSION,
            timeout_seconds=min(state["settings"].timeout_seconds, remaining_seconds),
        )
    except Exception:
        artifacts = _audit_artifacts_for_new_children(
            Path(state["run_dir"]), before, batch.batch_index
        )
        if not artifacts:
            artifacts = [
                AuditArtifact(batch.batch_index, None, None, None, None, "failed", PROMPT_VERSION)
            ]
        return {
            "model_call_attempt_count": attempts,
            "audit_artifacts": state.get("audit_artifacts", []) + artifacts,
            "call_succeeded": False,
            "failure_code": "model_call_failed",
            "attempted_chars": state.get("attempted_chars", 0) + batch.raw_chars,
            "progress_warning": state.get("progress_warning", False) or callback_warning,
        }

    return {
        "model_call_attempt_count": attempts,
        "model_call_count": state.get("model_call_count", 0) + 1,
        "audit_artifacts": state.get("audit_artifacts", [])
        + [_audit_artifact_from_dir(result.audit_dir, batch.batch_index, "succeeded")],
        "response_text": result.completion.text,
        "call_succeeded": True,
        "failure_code": None,
        "attempted_chars": state.get("attempted_chars", 0) + batch.raw_chars,
        "progress_warning": state.get("progress_warning", False) or callback_warning,
    }


def _parse_check_node(state: AnnualDiscoveryState) -> dict[str, object]:
    batch = state.get("current_batch")
    if batch is None:
        return _finish_failed_batch(state, "batch_missing")
    if not state.get("call_succeeded", False):
        return _finish_failed_batch(state, state.get("failure_code") or "model_call_failed")

    try:
        parsed_findings, limitations = parse_annual_discovery_response(
            state.get("response_text"), batch
        )
    except (ValueError, TypeError, KeyError):
        return _finish_failed_batch(state, "invalid_model_response")

    findings = list(state.get("findings", []))
    finding_keys = list(state.get("finding_keys", []))
    for parsed in parsed_findings:
        key = parsed.duplicate_key()
        if key in finding_keys:
            continue
        finding_keys.append(key)
        finding_id = _finding_id(str(state["identity"]["source_sha256"]), key)
        evidence_ids = [evidence_id for evidence_id, _quote in parsed.citations]
        findings.append(
            {
                "finding_id": finding_id,
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
    for limitation in limitations:
        if limitation not in merged_limitations:
            merged_limitations.append(limitation)
    coverage = dict(state.get("coverage", {}))
    processed = state.get("processed_batches", 0) + 1
    chars = state.get("processed_chars", 0) + batch.raw_chars
    coverage.update(
        {
            "processed_batches": processed,
            "successful_batches": state.get("successful_batches", 0) + 1,
            "failed_batches": state.get("failed_batches", 0),
            "processed_chars": chars,
            "attempted_chars": state.get("attempted_chars", 0),
            "batch_results": list(state.get("coverage", {}).get("batch_results", []))
            + [_batch_result(batch, "succeeded", None)],
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
        "successful_batches": state.get("successful_batches", 0) + 1,
        "processed_chars": chars,
        "coverage": coverage,
        "reason": state.get("reason"),
        "progress_warning": state.get("progress_warning", False) or warning,
    }


def _finish_failed_batch(state: AnnualDiscoveryState, failure_code: str) -> dict[str, object]:
    batch = state.get("current_batch")
    processed = state.get("processed_batches", 0) + 1
    failed = state.get("failed_batches", 0) + 1
    coverage = dict(state.get("coverage", {}))
    batch_results = list(coverage.get("batch_results", []))
    if batch is not None:
        batch_results.append(_batch_result(batch, "failed", failure_code))
    coverage.update(
        {
            "processed_batches": processed,
            "successful_batches": state.get("successful_batches", 0),
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
        if failure_code == "model_call_failed"
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
        "failed_batches": failed,
        "processed_chars": state.get("processed_chars", 0),
        "coverage": coverage,
        "limitations": limitations,
        "reason": state.get("reason") or failure_code,
        "progress_warning": state.get("progress_warning", False) or warning,
    }


def _advance_node(state: AnnualDiscoveryState) -> dict[str, object]:
    return {"current_index": state.get("current_index", 0) + 1}


def _finish_node(state: AnnualDiscoveryState) -> dict[str, object]:
    coverage = dict(state.get("coverage", empty_coverage()))
    coverage.update(
        {
            "processed_batches": state.get("processed_batches", 0),
            "successful_batches": state.get("successful_batches", 0),
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
        elif state.get("successful_batches", 0) == 0 and state.get("failed_batches", 0) > 0:
            status = "failed"
        else:
            status = "partial"
        reason = "workflow_time_budget_exhausted"
    elif state.get("processed_batches", 0) == 0 and state.get("failed_batches", 0) > 0:
        status = "failed"
        reason = state.get("reason") or "all_batches_failed"
    elif state.get("failed_batches", 0) == len(state.get("batches", [])) and state.get("batches", []):
        status = "failed"
        reason = state.get("reason") or "all_batches_failed"
    elif state.get("failed_batches", 0) > 0 or not coverage.get("complete"):
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
        prompt_version=PROMPT_VERSION,
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


def _load_prompt() -> str:
    try:
        prompt = _PROMPT_PATH.read_text(encoding="utf-8")
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
        "successful_batches": max(processed_batches - failed_batches, 0),
        "failed_batches": failed_batches,
        "total_pages": total_pages,
        "total_chars": total_chars,
    }


def _batch_result(batch: DiscoveryBatch, status: str, reason: str | None) -> dict[str, object]:
    return {
        "batch_index": batch.batch_index,
        "status": status,
        "reason": reason,
        "pages": sorted({fragment.pdf_page for fragment in batch.fragments}),
        "raw_chars": batch.raw_chars,
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


def _run_dir_children(run_dir: Path) -> set[str]:
    try:
        return {item.name for item in run_dir.iterdir() if item.is_dir()}
    except OSError:
        return set()


def _audit_artifacts_for_new_children(
    run_dir: Path, before: set[str], batch_index: int
) -> list[AuditArtifact]:
    found: list[AuditArtifact] = []
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return found
    for child in children:
        if not child.is_dir() or child.name in before:
            continue
        try:
            uuid.UUID(child.name)
        except ValueError:
            continue
        found.append(_audit_artifact_from_dir(child, batch_index, _audit_dir_status(child)))
    return found


def _audit_artifact_from_dir(path: Path, batch_index: int, status: str) -> AuditArtifact:
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
        prompt_version=PROMPT_VERSION,
    )


def _audit_dir_status(path: Path) -> str:
    if (path / "response.json").is_file():
        return "succeeded"
    if (path / "failure.json").is_file():
        return "failed"
    if (path / "request.json").is_file():
        return "started"
    return "incomplete"
