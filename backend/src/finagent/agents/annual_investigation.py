"""Bounded, evidence-linked interpretation of v2 annual candidate signals.

This workflow only handles the two supported annual divergence candidates. It
uses local retrieval against the report's own source PDF, sends bounded text
snippets through the existing audited Chat Completions wrapper, and returns
unverified interpretations. It does not modify or verify the archived report.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, TypedDict

from finagent.audit import EvidenceRef, audited_complete_chat
from finagent.audit.audited_chat import repository_runs_root
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage
from finagent.retrieval.annual_context import (
    HARD_MAX_SNIPPETS,
    HARD_MAX_SNIPPET_CHARS,
    HARD_MAX_TOTAL_CHARS,
    AnnualContextResult,
    NarrativeSnippet,
    retrieve_annual_context,
)

PROMPT_VERSION = "annual_investigation_v1"
SUPPORTED_SIGNAL_IDS = frozenset({"profit_up_cash_down", "revenue_up_cash_down"})
MAX_MODEL_CALLS = 2
INITIAL_MAX_SNIPPETS = 4
INITIAL_MAX_SNIPPET_CHARS = 900
INITIAL_MAX_TOTAL_CHARS = 2600

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "config" / "prompts" / "annual_investigation_v1.md"


class LangGraphUnavailableError(RuntimeError):
    """Raised when the optional LangGraph dependency is not installed."""


@dataclass(frozen=True, slots=True)
class InvestigationItem:
    """One candidate's interpretation or explicit abstention."""

    signal_id: str
    status: Literal["interpretation", "abstained"]
    explanation: str | None
    alternative_explanations: tuple[str, ...]
    limitations: tuple[str, ...]
    narrative_evidence_ids: tuple[str, ...]
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "signal_id": self.signal_id,
            "status": self.status,
            "explanation": self.explanation,
            "alternative_explanations": list(self.alternative_explanations),
            "limitations": list(self.limitations),
            "narrative_evidence_ids": list(self.narrative_evidence_ids),
        }
        if self.status == "interpretation":
            result["claim_type"] = "interpretation"
            result["verification_status"] = "unverified"
        else:
            result["reason"] = self.reason or "insufficient_evidence"
        return result


@dataclass(frozen=True, slots=True)
class AuditArtifact:
    """Relative paths for one audited model attempt."""

    audit_dir: str | None
    request_path: str | None
    response_path: str | None
    failure_path: str | None
    status: str
    prompt_version: str

    def to_dict(self) -> dict[str, object]:
        return {
            "audit_dir": self.audit_dir,
            "request_path": self.request_path,
            "response_path": self.response_path,
            "failure_path": self.failure_path,
            "status": self.status,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True, slots=True)
class AnnualInvestigationResult:
    """JSON-serializable outcome, separate from the archived annual report."""

    run_id: str
    company_id: str
    report_year: int
    report_period: str
    source_document_id: str
    source_sha256: str
    status: Literal["completed", "abstained"]
    prompt_version: str
    model_call_count: int
    model_call_attempt_count: int
    supplementary_retrieval_used: bool
    items: tuple[InvestigationItem, ...]
    audit_artifacts: tuple[AuditArtifact, ...]
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": "fintrace_annual_investigation",
            "run_id": self.run_id,
            "company_id": self.company_id,
            "report_year": self.report_year,
            "report_period": self.report_period,
            "source_document_id": self.source_document_id,
            "source_sha256": self.source_sha256,
            "status": self.status,
            "prompt_version": self.prompt_version,
            "model_called": self.model_call_count > 0,
            "model_call_count": self.model_call_count,
            "model_call_attempt_count": self.model_call_attempt_count,
            "supplementary_retrieval_used": self.supplementary_retrieval_used,
            "items": [item.to_dict() for item in self.items],
            "audit_artifacts": [item.to_dict() for item in self.audit_artifacts],
            "fraud_conclusion": None,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True, slots=True)
class _ParsedInterpretation:
    signal_id: str
    explanation: str
    alternative_explanations: tuple[str, ...]
    limitations: tuple[str, ...]
    narrative_evidence_ids: tuple[str, ...]
    request_more_context: bool


class AnnualInvestigationState(TypedDict, total=False):
    report: Mapping[str, object]
    source_pdf_path: str
    source_record: Mapping[str, object]
    settings: ModelSettings
    run_dir: str
    prompt_text: str
    run_id: str
    company_id: str
    report_year: int
    report_period: str
    source_document_id: str
    source_sha256: str
    candidate_signals: list[dict[str, object]]
    current_index: int
    model_call_count: int
    model_call_attempt_count: int
    supplementary_retrieval_used: bool
    initial_context_result: AnnualContextResult | None
    context_snippets: list[NarrativeSnippet]
    current_candidate: dict[str, object] | None
    current_snippets: list[NarrativeSnippet]
    current_citations_valid: bool
    supplementary_evidence_ids: list[str]
    parsed_interpretation: _ParsedInterpretation | None
    response_text: str
    response_error: str | None
    pending_reason: str | None
    request_supplement: bool
    items: list[InvestigationItem]
    audit_artifacts: list[AuditArtifact]
    overall_reason: str | None


def run_annual_investigation(
    report: Mapping[str, object],
    source_pdf_path: str | Path,
    source_record: Mapping[str, object],
    settings: ModelSettings,
    *,
    run_dir: str | Path | None = None,
) -> AnnualInvestigationResult:
    """Run the compiled LangGraph against one archived report and its source PDF.

    ``settings`` is always supplied by the caller. The run directory is derived
    from ``report.run_id`` unless supplied explicitly, and must remain the
    matching direct child of the repository's ``artifacts/runs`` directory.
    """

    identity = _require_report_identity(report, source_record)
    expected_run_dir = repository_runs_root() / identity["run_id"]
    chosen_run_dir = Path(run_dir) if run_dir is not None else expected_run_dir
    _validate_run_dir(chosen_run_dir, identity["run_id"])
    if chosen_run_dir.resolve() != expected_run_dir.resolve():
        raise ValueError("run_dir 必须与 report.run_id 对应的 artifacts/runs 子目录一致。")
    prompt_text = _load_prompt()
    graph = build_annual_investigation_graph()
    state = graph.invoke(
        {
            "report": report,
            "source_pdf_path": str(Path(source_pdf_path)),
            "source_record": source_record,
            "settings": settings,
            "run_dir": str(chosen_run_dir.resolve()),
            "prompt_text": prompt_text,
            "run_id": identity["run_id"],
            "company_id": identity["company_id"],
            "report_year": identity["report_year"],
            "report_period": identity["report_period"],
            "source_document_id": identity["source_document_id"],
            "source_sha256": identity["source_sha256"],
            "current_index": 0,
            "model_call_count": 0,
            "model_call_attempt_count": 0,
            "supplementary_retrieval_used": False,
            "context_snippets": [],
            "supplementary_evidence_ids": [],
            "items": [],
            "audit_artifacts": [],
        }
    )
    return _result_from_state(state)


def build_annual_investigation_graph():
    """Compile the official LangGraph StateGraph used by this workflow."""

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise LangGraphUnavailableError(
            "年度调查图需要可选依赖 langgraph>=1.2,<1.3；本机未安装该依赖。"
        ) from exc

    workflow = StateGraph(AnnualInvestigationState)
    workflow.add_node("initial_retrieval", _initial_retrieval_node)
    workflow.add_node("select_candidate", _select_candidate_node)
    workflow.add_node("call_model", _call_model_node)
    workflow.add_node("parse_response", _parse_response_node)
    workflow.add_node("supplementary_retrieval", _supplementary_retrieval_node)
    workflow.add_node("advance_candidate", _advance_candidate_node)
    workflow.add_node("finish", _finish_node)
    workflow.add_edge(START, "initial_retrieval")
    workflow.add_edge("initial_retrieval", "select_candidate")
    workflow.add_conditional_edges(
        "select_candidate",
        _after_select_candidate,
        {"call": "call_model", "advance": "advance_candidate", "finish": "finish"},
    )
    workflow.add_edge("call_model", "parse_response")
    workflow.add_conditional_edges(
        "parse_response",
        _after_parse_response,
        {"supplement": "supplementary_retrieval", "advance": "advance_candidate"},
    )
    workflow.add_conditional_edges(
        "supplementary_retrieval",
        _after_supplementary_retrieval,
        {"call": "call_model", "advance": "advance_candidate"},
    )
    workflow.add_edge("advance_candidate", "select_candidate")
    workflow.add_edge("finish", END)
    return workflow.compile()


def _initial_retrieval_node(state: AnnualInvestigationState) -> dict[str, object]:
    candidates = _candidate_signals(state["report"])
    if not candidates:
        return {
            "candidate_signals": [],
            "initial_context_result": None,
            "overall_reason": "no_supported_candidate_signal",
        }
    try:
        context = retrieve_annual_context(
            state["report"],
            state["source_pdf_path"],
            state["source_record"],
            max_snippets=INITIAL_MAX_SNIPPETS,
            max_chars_per_snippet=INITIAL_MAX_SNIPPET_CHARS,
            max_total_chars=INITIAL_MAX_TOTAL_CHARS,
        )
    except Exception:
        return {
            "candidate_signals": candidates,
            "initial_context_result": None,
            "overall_reason": "initial_retrieval_failed",
        }
    snippets = _same_source_snippets(
        context.snippets,
        state["source_document_id"],
        state["source_sha256"],
    )
    return {
        "candidate_signals": candidates,
        "initial_context_result": context,
        "context_snippets": list(snippets),
        "overall_reason": None if snippets else (context.reason or "no_same_source_narrative_evidence"),
    }


def _select_candidate_node(state: AnnualInvestigationState) -> dict[str, object]:
    candidates = state.get("candidate_signals", [])
    index = state.get("current_index", 0)
    if index >= len(candidates):
        return {
            "current_candidate": None,
            "current_snippets": [],
            "pending_reason": None,
        }
    candidate = candidates[index]
    signal_id = candidate["signal_id"]
    snippets = [
        item for item in state.get("context_snippets", []) if signal_id in item.signal_ids
    ]
    reason = None
    if candidate.get("prevalidation_error") is not None:
        reason = str(candidate["prevalidation_error"])
    elif state.get("model_call_attempt_count", 0) >= MAX_MODEL_CALLS:
        reason = "model_call_budget_exhausted"
    elif not snippets:
        reason = "no_same_source_narrative_evidence"
    return {
        "current_candidate": candidate,
        "current_snippets": snippets,
        "current_citations_valid": False,
        "supplementary_evidence_ids": [],
        "parsed_interpretation": None,
        "response_error": None,
        "pending_reason": reason,
        "request_supplement": False,
    }


def _after_select_candidate(state: AnnualInvestigationState) -> str:
    if state.get("current_candidate") is None:
        return "finish"
    if state.get("pending_reason") is not None:
        return "advance"
    if state.get("model_call_attempt_count", 0) >= MAX_MODEL_CALLS:
        return "finish"
    return "call"


def _call_model_node(state: AnnualInvestigationState) -> dict[str, object]:
    candidate = state["current_candidate"]
    if candidate is None:
        return {"response_error": "candidate_missing", "pending_reason": "candidate_missing"}
    run_dir = Path(state["run_dir"])
    before = _run_dir_children(run_dir)
    try:
        messages, pages = _build_messages(state, candidate, state.get("current_snippets", []))
    except Exception:
        return {"response_error": "prompt_build_failed", "pending_reason": "prompt_build_failed"}
    try:
        result = audited_complete_chat(
            messages,
            state["settings"],
            run_dir=run_dir,
            evidence_refs=[EvidenceRef(state["source_document_id"], page) for page in pages],
            prompt_version=PROMPT_VERSION,
        )
    except Exception:
        new_artifacts = _audit_artifacts_for_new_children(run_dir, before)
        if not new_artifacts:
            new_artifacts = [
                AuditArtifact(None, None, None, None, "audit_or_call_failed", PROMPT_VERSION)
            ]
        return {
            "model_call_attempt_count": state.get("model_call_attempt_count", 0) + 1,
            "model_call_count": state.get("model_call_count", 0)
            + int(any(item.request_path is not None for item in new_artifacts)),
            "audit_artifacts": state.get("audit_artifacts", []) + new_artifacts,
            "response_error": "model_call_failed",
            "pending_reason": "model_call_failed",
            "parsed_interpretation": None,
        }
    artifact = _audit_artifact_from_dir(result.audit_dir, "succeeded")
    return {
        "model_call_attempt_count": state.get("model_call_attempt_count", 0) + 1,
        "model_call_count": state.get("model_call_count", 0) + 1,
        "audit_artifacts": state.get("audit_artifacts", []) + [artifact],
        "response_text": result.completion.text,
        "response_error": None,
        "pending_reason": None,
        "parsed_interpretation": None,
        "request_supplement": False,
    }


def _parse_response_node(state: AnnualInvestigationState) -> dict[str, object]:
    error = state.get("response_error")
    if error is not None:
        return {
            "parsed_interpretation": None,
            "current_citations_valid": False,
            "pending_reason": error,
            "request_supplement": False,
        }
    candidate = state.get("current_candidate")
    if candidate is None:
        return {
            "parsed_interpretation": None,
            "current_citations_valid": False,
            "pending_reason": "candidate_missing",
            "request_supplement": False,
        }
    try:
        parsed = parse_model_interpretation(
            state.get("response_text", ""),
            expected_signal_id=str(candidate["signal_id"]),
        )
    except ValueError as exc:
        return {
            "parsed_interpretation": None,
            "current_citations_valid": False,
            "pending_reason": _parse_error_reason(str(exc)),
            "request_supplement": False,
        }

    eligible_ids = {item.evidence_id for item in state.get("current_snippets", [])}
    citation_ids = set(parsed.narrative_evidence_ids)
    has_valid_citations = bool(citation_ids) and citation_ids.issubset(eligible_ids)
    needs_supplement = parsed.request_more_context or not has_valid_citations
    if needs_supplement and _supplementary_call_budget_available(state):
        return {
            "parsed_interpretation": parsed,
            "current_citations_valid": has_valid_citations,
            "pending_reason": (
                "model_requested_more_context"
                if parsed.request_more_context
                else "no_valid_same_source_citation"
            ),
            "request_supplement": True,
        }

    if not has_valid_citations:
        exhausted_reason = (
            "supplementary_retrieval_exhausted"
            if state.get("supplementary_retrieval_used", False)
            else "model_call_budget_exhausted_before_supplement"
        )
        return {
            "parsed_interpretation": None,
            "current_citations_valid": False,
            "pending_reason": exhausted_reason,
            "request_supplement": False,
        }

    # A request for more context is advisory once valid same-source citations
    # support the interpretation and the bounded workflow cannot expand again.
    return {
        "parsed_interpretation": parsed,
        "current_citations_valid": True,
        "pending_reason": None,
        "request_supplement": False,
    }


def _supplementary_call_budget_available(state: AnnualInvestigationState) -> bool:
    if state.get("supplementary_retrieval_used", False):
        return False
    attempts = state.get("model_call_attempt_count", 0)
    if attempts >= MAX_MODEL_CALLS:
        return False
    remaining_candidates = max(
        0,
        len(state.get("candidate_signals", [])) - state.get("current_index", 0) - 1,
    )
    remaining_calls = MAX_MODEL_CALLS - attempts
    return remaining_calls > remaining_candidates


def _after_parse_response(state: AnnualInvestigationState) -> str:
    return "supplement" if state.get("request_supplement", False) else "advance"


def _supplementary_retrieval_node(state: AnnualInvestigationState) -> dict[str, object]:
    candidate = state.get("current_candidate")
    if candidate is None:
        return {
            "pending_reason": "candidate_missing",
            "request_supplement": False,
            "supplementary_retrieval_used": True,
        }
    try:
        result = retrieve_annual_context(
            state["report"],
            state["source_pdf_path"],
            state["source_record"],
            max_snippets=HARD_MAX_SNIPPETS,
            max_chars_per_snippet=HARD_MAX_SNIPPET_CHARS,
            max_total_chars=HARD_MAX_TOTAL_CHARS,
        )
    except Exception:
        return {
            "supplementary_retrieval_used": True,
            "pending_reason": "supplementary_retrieval_failed",
            "request_supplement": False,
        }

    retrieved = _same_source_snippets(
        result.snippets,
        state["source_document_id"],
        state["source_sha256"],
    )
    merged = _merge_bounded_snippets(state.get("context_snippets", []), retrieved)
    old_ids = {item.evidence_id for item in state.get("context_snippets", [])}
    new_ids = {item.evidence_id for item in merged if item.evidence_id not in old_ids}
    signal_id = str(candidate["signal_id"])
    current = [item for item in merged if signal_id in item.signal_ids]
    current_new_ids = [item.evidence_id for item in current if item.evidence_id in new_ids]
    if not current_new_ids:
        return {
            "context_snippets": list(merged),
            "current_snippets": current,
            "supplementary_evidence_ids": [],
            "supplementary_retrieval_used": True,
            "pending_reason": "supplementary_retrieval_exhausted",
            "request_supplement": False,
        }
    return {
        "context_snippets": list(merged),
        "current_snippets": current,
        "supplementary_evidence_ids": current_new_ids,
        "supplementary_retrieval_used": True,
        "pending_reason": None,
        "request_supplement": False,
    }


def _after_supplementary_retrieval(state: AnnualInvestigationState) -> str:
    if state.get("pending_reason") is not None:
        return "advance"
    if state.get("model_call_attempt_count", 0) >= MAX_MODEL_CALLS:
        return "advance"
    return "call"


def _advance_candidate_node(state: AnnualInvestigationState) -> dict[str, object]:
    candidate = state.get("current_candidate")
    if candidate is None:
        return {"current_index": state.get("current_index", 0) + 1}
    signal_id = str(candidate["signal_id"])
    parsed = state.get("parsed_interpretation")
    reason = state.get("pending_reason")
    item: InvestigationItem
    preserve_reason = reason in {
        None,
        "supplementary_retrieval_exhausted",
        "supplementary_retrieval_failed",
    }
    if (
        parsed is not None
        and state.get("current_citations_valid", False)
        and preserve_reason
    ):
        limitations = list(parsed.limitations)
        if parsed.request_more_context:
            if reason == "supplementary_retrieval_failed":
                limit = "模型请求更多同源上下文，但补充检索未能取得更多原文；当前解释仍属未核实。"
            elif reason == "supplementary_retrieval_exhausted":
                limit = "模型请求更多同源上下文，但年报检索没有找到新增相关片段；当前解释仍属未核实。"
            else:
                limit = "模型请求更多同源上下文；本次有限检索或调用预算已到上限，当前解释仍属未核实。"
            limitations.append(limit)
        item = InvestigationItem(
            signal_id=signal_id,
            status="interpretation",
            explanation=parsed.explanation,
            alternative_explanations=parsed.alternative_explanations,
            limitations=tuple(limitations),
            narrative_evidence_ids=parsed.narrative_evidence_ids,
        )
    else:
        item = InvestigationItem(
            signal_id=signal_id,
            status="abstained",
            explanation=None,
            alternative_explanations=(),
            limitations=("模型解释未通过严格结构与同源引用要求。",),
            narrative_evidence_ids=(),
            reason=reason or "insufficient_evidence",
        )
    return {
        "items": state.get("items", []) + [item],
        "current_index": state.get("current_index", 0) + 1,
        "current_candidate": None,
        "current_snippets": [],
        "current_citations_valid": False,
        "supplementary_evidence_ids": [],
        "parsed_interpretation": None,
        "response_error": None,
        "pending_reason": None,
        "request_supplement": False,
        "response_text": "",
    }


def _finish_node(state: AnnualInvestigationState) -> dict[str, object]:
    items = list(state.get("items", []))
    recorded = {item.signal_id for item in items}
    for candidate in state.get("candidate_signals", []):
        signal_id = str(candidate["signal_id"])
        if signal_id not in recorded:
            items.append(
                InvestigationItem(
                    signal_id=signal_id,
                    status="abstained",
                    explanation=None,
                    alternative_explanations=(),
                    limitations=("本次运行的模型调用预算已耗尽，未完成调查。",),
                    narrative_evidence_ids=(),
                    reason=(state.get("pending_reason") or "model_call_budget_exhausted"),
                )
            )
            recorded.add(signal_id)
    return {"items": items}


def _candidate_signals(report: Mapping[str, object]) -> list[dict[str, object]]:
    pending = report.get("pending_review")
    records = pending.get("candidate_signals") if isinstance(pending, Mapping) else None
    if not isinstance(records, list):
        records = report.get("screening")
    if not isinstance(records, list):
        return []
    prepared: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        signal_id = record.get("signal_id")
        if (
            not isinstance(signal_id, str)
            or record.get("status") != "candidate"
            or signal_id not in SUPPORTED_SIGNAL_IDS
            or signal_id in seen
        ):
            continue
        candidate: dict[str, object] = {"signal_id": signal_id, "status": "candidate"}
        calculation_binding = _verified_candidate_calculation_binding(report, record, signal_id)
        if calculation_binding is None:
            candidate["prevalidation_error"] = "candidate_calculation_binding_failed"
            prepared.append(candidate)
            seen.add(signal_id)
            continue
        candidate.update(calculation_binding)
        prepared.append(candidate)
        seen.add(signal_id)
    return prepared


def _verified_candidate_calculation_binding(
    report: Mapping[str, object],
    candidate: Mapping[str, object],
    signal_id: str,
) -> dict[str, object] | None:
    """Accept a candidate only when both annual differences match verified analyses."""

    expected_ids = {
        "profit_up_cash_down": (
            "calc:annual_difference:net_profit_parent",
            "calc:annual_difference:operating_cash_flow",
        ),
        "revenue_up_cash_down": (
            "calc:annual_difference:revenue",
            "calc:annual_difference:operating_cash_flow",
        ),
    }.get(signal_id)
    if expected_ids is None:
        return None
    confirmed = report.get("confirmed")
    analyses = confirmed.get("analyses") if isinstance(confirmed, Mapping) else None
    if not isinstance(analyses, list):
        return None
    by_id: dict[str, Mapping[str, object]] = {}
    for analysis in analyses:
        if not isinstance(analysis, Mapping):
            continue
        calculation_id = analysis.get("calculation_id")
        if isinstance(calculation_id, str):
            if calculation_id in by_id:
                return None
            by_id[calculation_id] = analysis
    left = by_id.get(expected_ids[0])
    right = by_id.get(expected_ids[1])
    if left is None or right is None:
        return None
    for calculation_id, analysis in ((expected_ids[0], left), (expected_ids[1], right)):
        independent = analysis.get("independent_verification")
        if (
            analysis.get("status") != "succeeded"
            or analysis.get("formula_id") != "annual_difference"
            or analysis.get("failure_reason") is not None
            or not isinstance(independent, Mapping)
            or independent.get("status") != "verified"
            or independent.get("calculation_id") != calculation_id
            or independent.get("recomputed_value") != analysis.get("output_value")
        ):
            return None
    left_output = left.get("output_value")
    right_output = right.get("output_value")
    if not isinstance(left_output, str) or not isinstance(right_output, str):
        return None
    if candidate.get("calculation_ids") != list(expected_ids):
        return None
    if candidate.get("left_difference") != left_output or candidate.get("right_difference") != right_output:
        return None
    left_inputs = left.get("input_fact_ids")
    right_inputs = right.get("input_fact_ids")
    if (
        not isinstance(left_inputs, list)
        or not isinstance(right_inputs, list)
        or len(left_inputs) != 2
        or len(right_inputs) != 2
        or any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in (*left_inputs, *right_inputs))
    ):
        return None
    source_document_id = report.get("source_document_id")
    if not isinstance(source_document_id, str):
        return None
    if any(not item.startswith(source_document_id + ":") for item in (*left_inputs, *right_inputs)):
        return None
    expected_inputs = [*left_inputs, *right_inputs]
    if candidate.get("input_fact_ids") != expected_inputs:
        return None
    safe_inputs = list(expected_inputs[:12])
    if len(safe_inputs) != len(expected_inputs):
        return None
    return {
        "left_difference": left_output,
        "right_difference": right_output,
        "calculation_ids": list(expected_ids),
        "input_fact_ids": safe_inputs,
    }


def parse_model_interpretation(text: str, *, expected_signal_id: str) -> _ParsedInterpretation:
    """Parse the exact JSON response shape; reject prose, extra fields and claims."""

    if (
        not isinstance(text, str)
        or len(text) > 8000
        or not isinstance(expected_signal_id, str)
        or expected_signal_id not in SUPPORTED_SIGNAL_IDS
    ):
        raise ValueError("invalid_model_response")
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("invalid_json") from None
    if not isinstance(value, dict):
        raise ValueError("invalid_json_object")
    required = {
        "signal_id",
        "explanation",
        "alternative_explanations",
        "limitations",
        "narrative_evidence_ids",
        "request_more_context",
    }
    if set(value) != required:
        raise ValueError("invalid_json_fields")
    if value.get("signal_id") != expected_signal_id:
        raise ValueError("signal_id_mismatch")
    explanation = _required_model_text(value.get("explanation"), "explanation", 1600)
    alternatives = _model_text_list(value.get("alternative_explanations"), "alternative_explanations", 6, 600)
    limitations = _model_text_list(value.get("limitations"), "limitations", 8, 600)
    if not limitations:
        raise ValueError("limitations_required")
    evidence_ids = _model_id_list(value.get("narrative_evidence_ids"), "narrative_evidence_ids", 8)
    request_more = value.get("request_more_context")
    if not isinstance(request_more, bool):
        raise ValueError("request_more_context_must_be_boolean")
    all_text = (explanation, *alternatives, *limitations)
    if any(_contains_forbidden_conclusion(item) for item in all_text):
        raise ValueError("prohibited_verified_or_fraud_conclusion")
    return _ParsedInterpretation(
        signal_id=expected_signal_id,
        explanation=explanation,
        alternative_explanations=alternatives,
        limitations=limitations,
        narrative_evidence_ids=evidence_ids,
        request_more_context=request_more,
    )


def _build_messages(
    state: AnnualInvestigationState,
    candidate: Mapping[str, object],
    snippets: Sequence[NarrativeSnippet],
) -> tuple[list[ChatMessage], tuple[int, ...]]:
    identity = {
        "run_id": state["run_id"],
        "company_id": state["company_id"],
        "report_year": state["report_year"],
        "report_period": state["report_period"],
        "source_document_id": state["source_document_id"],
        "source_sha256": state["source_sha256"],
    }
    candidate_fields = _safe_candidate_for_prompt(candidate)
    evidence = []
    total_chars = 0
    for snippet in snippets:
        if (
            snippet.source_document_id != state["source_document_id"]
            or snippet.source_sha256 != state["source_sha256"]
            or candidate_fields["signal_id"] not in snippet.signal_ids
        ):
            continue
        if len(snippet.raw_text) > HARD_MAX_SNIPPET_CHARS:
            continue
        if total_chars + len(snippet.raw_text) > HARD_MAX_TOTAL_CHARS:
            continue
        evidence.append(
            {
                "evidence_id": snippet.evidence_id,
                "source_document_id": snippet.source_document_id,
                "source_sha256": snippet.source_sha256,
                "section": snippet.section_label,
                "pdf_page": snippet.pdf_page,
                "raw_text": snippet.raw_text,
            }
        )
        total_chars += len(snippet.raw_text)
    if not evidence:
        raise ValueError("no eligible narrative evidence")
    payload = {
        "document_binding": identity,
        "candidate": candidate_fields,
        "narrative_evidence": evidence,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    pages = tuple(sorted({item["pdf_page"] for item in evidence}))
    return [
        ChatMessage(role="system", content=state["prompt_text"]),
        ChatMessage(role="user", content=content),
    ], pages


def _safe_candidate_for_prompt(candidate: Mapping[str, object]) -> dict[str, object]:
    signal_id = candidate.get("signal_id")
    if (
        not isinstance(signal_id, str)
        or signal_id not in SUPPORTED_SIGNAL_IDS
        or candidate.get("status") != "candidate"
        or candidate.get("prevalidation_error") is not None
    ):
        raise ValueError("unsupported candidate")
    result: dict[str, object] = {"signal_id": signal_id, "status": "candidate"}
    for field in ("left_difference", "right_difference", "formula", "value"):
        value = candidate.get(field)
        if isinstance(value, str) and len(value) <= 200:
            result[field] = value
    for field in ("input_fact_ids", "calculation_ids"):
        values = candidate.get(field)
        if isinstance(values, list):
            filtered = [item for item in values[:12] if isinstance(item, str) and _ID_RE.fullmatch(item)]
            if filtered:
                result[field] = filtered
    reason = candidate.get("reason")
    if isinstance(reason, str) and reason.strip() and len(reason) <= 300:
        result["reason"] = reason.strip()
    return result


def _same_source_snippets(
    snippets: Sequence[NarrativeSnippet],
    document_id: str,
    source_sha256: str,
) -> tuple[NarrativeSnippet, ...]:
    seen: set[str] = set()
    selected: list[NarrativeSnippet] = []
    total = 0
    for item in snippets:
        if (
            not isinstance(item, NarrativeSnippet)
            or item.source_document_id != document_id
            or item.source_sha256 != source_sha256
            or not isinstance(item.evidence_id, str)
            or item.evidence_id in seen
            or not item.signal_ids
            or len(item.raw_text) > HARD_MAX_SNIPPET_CHARS
            or len(selected) >= HARD_MAX_SNIPPETS
            or total + len(item.raw_text) > HARD_MAX_TOTAL_CHARS
        ):
            continue
        selected.append(item)
        seen.add(item.evidence_id)
        total += len(item.raw_text)
    return tuple(selected)


def _merge_bounded_snippets(
    existing: Sequence[NarrativeSnippet],
    added: Sequence[NarrativeSnippet],
) -> tuple[NarrativeSnippet, ...]:
    merged: list[NarrativeSnippet] = []
    seen: set[str] = set()
    total = 0
    for item in (*existing, *added):
        if item.evidence_id in seen:
            continue
        if len(merged) >= HARD_MAX_SNIPPETS or total + len(item.raw_text) > HARD_MAX_TOTAL_CHARS:
            continue
        merged.append(item)
        seen.add(item.evidence_id)
        total += len(item.raw_text)
    return tuple(merged)


def _require_report_identity(
    report: Mapping[str, object],
    source_record: Mapping[str, object],
) -> dict[str, str | int]:
    if not isinstance(report, Mapping) or not isinstance(source_record, Mapping):
        raise ValueError("report 和 source_record 必须是映射对象。")
    if report.get("kind") != "fintrace_annual_analysis_report":
        raise ValueError("只接受正式 FINTRACE 年度分析报告。")
    run_id = report.get("run_id")
    company_id = report.get("company_id")
    report_year = report.get("report_year")
    source_document_id = report.get("source_document_id")
    source_sha256 = report.get("source_sha256")
    report_period = source_record.get("report_period")
    if not isinstance(run_id, str) or not _ID_RE.fullmatch(run_id):
        raise ValueError("report.run_id 缺失或不合法。")
    if not isinstance(company_id, str) or not _ID_RE.fullmatch(company_id):
        raise ValueError("report.company_id 缺失或不合法。")
    if type(report_year) is not int or not 1900 <= report_year <= 2200:
        raise ValueError("report.report_year 缺失或不合法。")
    if not isinstance(source_document_id, str) or not _ID_RE.fullmatch(source_document_id):
        raise ValueError("report.source_document_id 缺失或不合法。")
    if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256.lower()):
        raise ValueError("report.source_sha256 缺失或不合法。")
    if not isinstance(report_period, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_period):
        raise ValueError("source_record.report_period 缺失或不合法。")
    try:
        period_date = date.fromisoformat(report_period)
    except ValueError:
        raise ValueError("source_record.report_period 缺失或不合法。") from None
    if period_date.year != report_year:
        raise ValueError("report 与 source_record 的报告期不一致。")
    if source_record.get("document_id") != source_document_id:
        raise ValueError("report 与 source_record 的 document_id 不一致。")
    if source_record.get("company_id") != company_id:
        raise ValueError("report 与 source_record 的 company_id 不一致。")
    record_hash = source_record.get("sha256")
    if not isinstance(record_hash, str) or record_hash.lower() != source_sha256.lower():
        raise ValueError("report 与 source_record 的 SHA256 不一致。")
    return {
        "run_id": run_id,
        "company_id": company_id,
        "report_year": report_year,
        "report_period": report_period,
        "source_document_id": source_document_id,
        "source_sha256": source_sha256.lower(),
    }


def _validate_run_dir(run_dir: Path, run_id: str) -> None:
    root = repository_runs_root()
    if run_dir.name != run_id or not run_dir.is_dir() or run_dir.is_symlink():
        raise ValueError("运行目录必须是 report.run_id 对应的已存在运行目录。")
    try:
        if run_dir.parent.resolve() != root.resolve() or run_dir.resolve() != root.resolve() / run_id:
            raise ValueError("运行目录必须是 artifacts/runs 下的直接子目录。")
    except OSError:
        raise ValueError("运行目录不可访问。") from None


def _load_prompt() -> str:
    try:
        prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        raise RuntimeError("年度调查提示词文件不可读取。") from None
    if not prompt.strip():
        raise RuntimeError("年度调查提示词文件为空。")
    return prompt


def _required_model_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field}_invalid")
    return value.strip()


def _model_text_list(value: object, field: str, max_items: int, max_chars: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{field}_invalid")
    prepared: list[str] = []
    for item in value:
        text = _required_model_text(item, field, max_chars)
        prepared.append(text)
    return tuple(prepared)


def _model_id_list(value: object, field: str, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{field}_invalid")
    prepared: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _ID_RE.fullmatch(item) or item in prepared:
            raise ValueError(f"{field}_invalid")
        prepared.append(item)
    return tuple(prepared)


def _contains_forbidden_conclusion(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).lower()
    forbidden = (
        "verified",
        "independentlyverified",
        "confirmedfraud",
        "fraudconfirmed",
        "已核实",
        "经核实",
        "独立核实",
        "已验证",
        "核验通过",
        "确认舞弊",
        "确认存在舞弊",
        "存在舞弊",
        "构成舞弊",
        "证实舞弊",
        "财务造假已确认",
    )
    if any(term in normalized for term in forbidden):
        return True
    return False


def _parse_error_reason(error: str) -> str:
    return "invalid_model_json" if error.startswith("invalid_json") else "invalid_model_response"


def _run_dir_children(run_dir: Path) -> set[str]:
    try:
        return {item.name for item in run_dir.iterdir() if item.is_dir()}
    except OSError:
        return set()


def _audit_artifacts_for_new_children(run_dir: Path, before: set[str]) -> list[AuditArtifact]:
    entries: list[AuditArtifact] = []
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return entries
    for child in children:
        if not child.is_dir() or child.name in before:
            continue
        try:
            uuid.UUID(child.name)
        except ValueError:
            continue
        entries.append(_audit_artifact_from_dir(child, _audit_dir_status(child)))
    return entries


def _audit_artifact_from_dir(path: Path, status: str) -> AuditArtifact:
    run_root = repository_runs_root()
    try:
        rel = path.resolve().relative_to(run_root.resolve().parent.parent).as_posix()
    except (OSError, ValueError):
        rel = (Path("artifacts") / "runs" / path.parent.name / path.name).as_posix()
    request = (path / "request.json").exists()
    response = (path / "response.json").exists()
    failure = (path / "failure.json").exists()
    base = Path(rel)
    return AuditArtifact(
        audit_dir=rel,
        request_path=(base / "request.json").as_posix() if request else None,
        response_path=(base / "response.json").as_posix() if response else None,
        failure_path=(base / "failure.json").as_posix() if failure else None,
        status="succeeded" if response else ("failed" if failure else status),
        prompt_version=PROMPT_VERSION,
    )


def _audit_dir_status(path: Path) -> str:
    if (path / "response.json").exists():
        return "succeeded"
    if (path / "failure.json").exists():
        return "failed"
    if (path / "request.json").exists():
        return "started"
    return "incomplete"


def _result_from_state(state: Mapping[str, object]) -> AnnualInvestigationResult:
    items = state.get("items", [])
    audits = state.get("audit_artifacts", [])
    return AnnualInvestigationResult(
        run_id=str(state["run_id"]),
        company_id=str(state["company_id"]),
        report_year=int(state["report_year"]),
        report_period=str(state["report_period"]),
        source_document_id=str(state["source_document_id"]),
        source_sha256=str(state["source_sha256"]),
        status="completed" if any(item.status == "interpretation" for item in items) else "abstained",
        prompt_version=PROMPT_VERSION,
        model_call_count=int(state.get("model_call_count", 0)),
        model_call_attempt_count=int(state.get("model_call_attempt_count", 0)),
        supplementary_retrieval_used=bool(state.get("supplementary_retrieval_used", False)),
        items=tuple(items),
        audit_artifacts=tuple(audits),
        reason=state.get("overall_reason") if isinstance(state.get("overall_reason"), str) else None,
    )
