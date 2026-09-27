"""Deterministic, local-only narrative context retrieval for v2 annual signals.

This module accepts only candidate v2 signal IDs from a FINTRACE annual report.
It verifies the report/source-record identity and the original PDF SHA-256 before
reading text blocks. Retrieval is keyword based and returns source context only;
it does not infer or verify causal explanations and never accesses the Internet.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import pymupdf

SUPPORTED_SIGNAL_IDS = frozenset({"profit_up_cash_down", "revenue_up_cash_down"})
REPORT_KIND = "fintrace_annual_analysis_report"

DEFAULT_MAX_SNIPPETS = 4
DEFAULT_MAX_SNIPPET_CHARS = 900
DEFAULT_MAX_TOTAL_CHARS = 2600
HARD_MAX_SNIPPETS = 8
HARD_MAX_SNIPPET_CHARS = 1200
HARD_MAX_TOTAL_CHARS = 4000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPORT_PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOP_LEVEL_SECTION_RE = re.compile(r"^第(?:[一二三四五六七八九十百]+|\d+)节")
_MDA_TITLE_RE = re.compile(r"^(?:第[一二三四五六七八九十百\d]+节)?管理层讨论与分析$")
_NOTES_TITLE_RE = re.compile(
    r"^(?:(?:第[一二三四五六七八九十百\d]+节)?(?:合并)?财务报表(?:项目)?附注|"
    r"(?:[一二三四五六七八九十百\d]+[、.．])?(?:合并)?财务报表项目注释)$"
)

_CASH_TERMS = (
    "经营活动产生的现金流量净额",
    "经营活动现金流",
    "现金流量表",
    "现金流量",
    "现金流",
)
_OPERATING_CASH_TERMS = (
    "经营活动",
    "经营性应收",
    "经营性应付",
    "经营现金流",
)
_NON_OPERATING_CASH_TERMS = ("投资活动", "筹资活动")
_REVENUE_TERMS = ("营业收入", "销售收入", "收入确认", "收入合同", "合同負債", "合同负债")
_PROFIT_TERMS = ("净利润", "净利率", "归属于上市公司股东的净利润")
_CONTEXT_TERMS = ("本期", "本年", "上年同期", "报告期", "同比", "较上期")
_EXPLANATION_TERMS = ("原因", "主要是", "由于", "变动说明", "增加", "减少", "上升", "下降")
_CASH_DETAIL_TERMS = (
    "现金流入",
    "现金流出",
    "经营活动产生的现金流量净额",
    "收到",
    "支付",
    "将净利润调节为经营活动现金流量",
    "经营性应收",
    "经营性应付",
)


@dataclass(frozen=True, slots=True)
class NarrativeSnippet:
    """A verbatim text block with its PDF location and deterministic selection reason."""

    evidence_id: str
    source_document_id: str
    source_sha256: str
    signal_ids: tuple[str, ...]
    section_label: str
    pdf_page: int
    bbox: tuple[float, float, float, float]
    raw_text: str
    selection_reason: str


@dataclass(frozen=True, slots=True)
class SignalContextResult:
    signal_id: str
    status: Literal["retrieved", "abstained"]
    evidence_ids: tuple[str, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AnnualContextResult:
    status: Literal["retrieved", "abstained"]
    source_document_id: str | None
    source_sha256: str | None
    signal_results: tuple[SignalContextResult, ...]
    sections_found: tuple[str, ...]
    snippets: tuple[NarrativeSnippet, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Block:
    page: int
    ordinal: int
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True, slots=True)
class _Section:
    label: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _ScoredBlock:
    block: _Block
    section_label: str
    score: int
    matched_signals: tuple[str, ...]
    reason: str
    evidence_id: str


def retrieve_annual_context(
    report: Mapping[str, object],
    source_pdf_path: str | Path,
    source_record: Mapping[str, object],
    *,
    max_snippets: int = DEFAULT_MAX_SNIPPETS,
    max_chars_per_snippet: int = DEFAULT_MAX_SNIPPET_CHARS,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
) -> AnnualContextResult:
    """Retrieve bounded context for current candidate signals from one local PDF.

    Args:
        report: Current ``fintrace_annual_analysis_report`` JSON object.
        source_pdf_path: Explicit local path to the original PDF named by the report.
        source_record: Matching ``source.json`` metadata for that original document.
        max_snippets: Maximum number of returned text blocks.
        max_chars_per_snippet: Maximum raw text characters in each returned block.
        max_total_chars: Maximum aggregate raw text characters returned.

    A source mismatch, missing heading, or lack of relevant blocks returns an
    explicit abstention. File, PDF and report reads stay local and read-only.
    """

    limits = (max_snippets, max_chars_per_snippet, max_total_chars)
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in limits):
        raise ValueError("检索结果数量和文本上限必须为正整数。")
    if (
        max_snippets > HARD_MAX_SNIPPETS
        or max_chars_per_snippet > HARD_MAX_SNIPPET_CHARS
        or max_total_chars > HARD_MAX_TOTAL_CHARS
    ):
        raise ValueError("检索结果超过本地上下文硬上限。")

    source_document_id, source_sha256, source_identity_error = _report_source_identity(report)
    signal_ids = _candidate_signal_ids(report)
    if not signal_ids:
        return _abstain(
            "no_candidate_signal",
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )
    if not _is_supported_current_report(report):
        return _abstain(
            "unsupported_report_kind",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )
    report_fields_error = _report_fields_error(report)
    if report_fields_error:
        return _abstain(
            report_fields_error,
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )
    if source_identity_error:
        return _abstain(
            source_identity_error,
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )
    if not source_document_id or not source_sha256:
        return _abstain(
            "report_source_identity_missing_or_invalid",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )

    identity_error = _source_identity_error(report, source_record, source_document_id, source_sha256)
    if identity_error:
        return _abstain(
            identity_error,
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )

    source_path = Path(source_pdf_path)
    if not source_path.is_file():
        return _abstain(
            "source_pdf_missing",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )
    try:
        actual_sha256 = _hash_file(source_path)
    except OSError:
        return _abstain(
            "source_pdf_unreadable",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )
    if actual_sha256 != source_sha256:
        return _abstain(
            "source_pdf_hash_mismatch",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )

    try:
        with pymupdf.open(source_path) as document:
            blocks = _read_blocks(document)
    except Exception:
        return _abstain(
            "source_pdf_unreadable",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )
    if not blocks:
        return _abstain(
            "source_pdf_has_no_text_blocks",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
        )

    sections = _locate_sections(blocks)
    found_labels = tuple(section.label for section in sections)
    section_kinds = {label for label in found_labels}
    missing_sections = tuple(
        label for label in ("mda", "financial_notes") if label not in section_kinds
    )
    if missing_sections:
        reason = "required_section_heading_missing:" + ",".join(missing_sections)
        return _abstain(
            reason,
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
            sections_found=found_labels,
        )

    candidates = _score_blocks(
        blocks,
        sections,
        signal_ids,
        source_document_id,
        source_sha256,
        report.get("report_year") if isinstance(report.get("report_year"), int) else None,
        max_chars_per_snippet,
    )
    if not candidates:
        return _abstain(
            "no_relevant_snippets_within_per_snippet_limit",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
            sections_found=found_labels,
        )

    selected = _select_bounded(candidates, max_snippets, max_total_chars)
    if not selected:
        return _abstain(
            "no_relevant_snippets_within_total_budget",
            signal_ids=signal_ids,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
            sections_found=found_labels,
        )

    snippets = tuple(
        NarrativeSnippet(
            evidence_id=item.evidence_id,
            source_document_id=source_document_id,
            source_sha256=source_sha256,
            signal_ids=item.matched_signals,
            section_label=item.section_label,
            pdf_page=item.block.page,
            bbox=item.block.bbox,
            raw_text=item.block.text,
            selection_reason=item.reason,
        )
        for item in selected
    )
    selected_ids = {item.evidence_id for item in selected}
    signal_results: list[SignalContextResult] = []
    for signal_id in signal_ids:
        evidence_ids = tuple(
            item.evidence_id
            for item in selected
            if item.evidence_id in selected_ids and signal_id in item.matched_signals
        )
        if evidence_ids:
            signal_results.append(SignalContextResult(signal_id, "retrieved", evidence_ids))
        else:
            reason = (
                "snippet_budget_exhausted"
                if any(signal_id in candidate.matched_signals for candidate in candidates)
                else "no_relevant_snippet_for_signal"
            )
            signal_results.append(SignalContextResult(signal_id, "abstained", (), reason))

    return AnnualContextResult(
        status="retrieved",
        source_document_id=source_document_id,
        source_sha256=source_sha256,
        signal_results=tuple(signal_results),
        sections_found=found_labels,
        snippets=snippets,
    )


def _candidate_signal_ids(report: Mapping[str, object]) -> tuple[str, ...]:
    pending_review = report.get("pending_review")
    signals = (
        pending_review.get("candidate_signals")
        if isinstance(pending_review, Mapping)
        else report.get("screening")
    )
    if not isinstance(signals, list):
        return ()
    accepted: list[str] = []
    for signal in signals:
        if not isinstance(signal, Mapping):
            continue
        signal_id = signal.get("signal_id")
        if not isinstance(signal_id, str):
            continue
        if signal.get("status") == "candidate" and signal_id in SUPPORTED_SIGNAL_IDS and signal_id not in accepted:
            accepted.append(signal_id)
    return tuple(accepted)


def _is_supported_current_report(report: Mapping[str, object]) -> bool:
    if report.get("kind") == REPORT_KIND:
        return True
    # The archived deterministic CLI analysis.json predates the rendered report
    # envelope. Its source identity is carried by extracted facts and candidate
    # signals live in ``screening``; accept that exact current-run shape too.
    return (
        report.get("status") == "completed"
        and isinstance(report.get("screening"), list)
        and isinstance(report.get("extraction"), Mapping)
    )


def _report_fields_error(report: Mapping[str, object]) -> str | None:
    company_id = _nonempty_str(report.get("company_id"))
    if company_id is None:
        return "report_company_id_missing_or_invalid"
    report_year = report.get("report_year")
    if (
        not isinstance(report_year, int)
        or isinstance(report_year, bool)
        or not 1900 <= report_year <= 2200
    ):
        return "report_year_missing_or_invalid"
    return None


def _report_source_identity(
    report: Mapping[str, object],
) -> tuple[str | None, str | None, str | None]:
    document_id = _nonempty_str(report.get("source_document_id"))
    source_sha256 = _normalized_sha(report.get("source_sha256"))
    if "source_document_id" in report or "source_sha256" in report:
        if document_id is None or source_sha256 is None:
            return document_id, source_sha256, "report_source_identity_missing_or_invalid"
        return document_id, source_sha256, None

    extraction = report.get("extraction")
    facts = extraction.get("facts") if isinstance(extraction, Mapping) else None
    if not isinstance(facts, list) or not facts:
        return None, None, "analysis_source_facts_missing_or_invalid"

    report_company_id = _nonempty_str(report.get("company_id"))
    document_ids: set[str] = set()
    source_hashes: set[str] = set()
    for fact in facts:
        if not isinstance(fact, Mapping):
            return None, None, "analysis_fact_source_identity_missing_or_invalid"
        fact_document_id = _nonempty_str(fact.get("document_id"))
        fact_source_sha256 = _normalized_sha(fact.get("source_sha256"))
        if fact_document_id is None or fact_source_sha256 is None:
            return None, None, "analysis_fact_source_identity_missing_or_invalid"
        document_ids.add(fact_document_id)
        source_hashes.add(fact_source_sha256)

        if "company_id" in report:
            fact_company_id = _nonempty_str(fact.get("company_id"))
            if fact_company_id is None:
                return None, None, "analysis_fact_company_id_missing_or_invalid"
            if report_company_id is None or fact_company_id != report_company_id:
                return None, None, "analysis_fact_company_id_mismatch"

    if len(document_ids) != 1 or len(source_hashes) != 1:
        return None, None, "analysis_facts_source_identity_mismatch"
    return next(iter(document_ids)), next(iter(source_hashes)), None


def _source_identity_error(
    report: Mapping[str, object],
    source_record: Mapping[str, object],
    document_id: str,
    expected_sha256: str,
) -> str | None:
    record_id = _nonempty_str(source_record.get("document_id"))
    record_sha = _normalized_sha(source_record.get("sha256"))
    if record_id != document_id:
        return "source_document_id_mismatch"
    if record_sha != expected_sha256:
        return "source_record_hash_mismatch"
    report_company = _nonempty_str(report.get("company_id"))
    record_company = _nonempty_str(source_record.get("company_id"))
    if record_company is None:
        return "source_record_company_id_missing_or_invalid"
    if report_company != record_company:
        return "source_company_id_mismatch"
    report_year = report.get("report_year")
    report_period = _nonempty_str(source_record.get("report_period"))
    if not report_period or not _REPORT_PERIOD_RE.fullmatch(report_period):
        return "source_record_report_period_missing_or_invalid"
    try:
        parsed_period = date.fromisoformat(report_period)
    except ValueError:
        return "source_record_report_period_missing_or_invalid"
    if parsed_period.year != report_year:
        return "source_report_period_mismatch"
    return None


def _read_blocks(document: pymupdf.Document) -> tuple[_Block, ...]:
    blocks: list[_Block] = []
    for page_number, page in enumerate(document, start=1):
        for ordinal, item in enumerate(page.get_text("blocks")):
            if len(item) < 5 or not isinstance(item[4], str) or item[4] == "":
                continue
            bbox = tuple(float(value) for value in item[:4])
            blocks.append(_Block(page_number, ordinal, bbox, item[4]))
    return tuple(blocks)


def _locate_sections(blocks: tuple[_Block, ...]) -> tuple[_Section, ...]:
    mda_start = next((index for index, block in enumerate(blocks) if _is_mda_heading(block.text)), None)
    notes_start = next((index for index, block in enumerate(blocks) if _is_notes_heading(block.text)), None)
    section_specs: list[tuple[str, int]] = []
    if mda_start is not None:
        section_specs.append(("mda", mda_start))
    if notes_start is not None:
        section_specs.append(("financial_notes", notes_start))

    located: list[_Section] = []
    for label, start in section_specs:
        end = len(blocks)
        for index in range(start + 1, len(blocks)):
            if _is_top_level_section_heading(blocks[index].text):
                end = index
                break
        located.append(_Section(label, start + 1, end))
    return tuple(located)


def _is_mda_heading(text: str) -> bool:
    return bool(_MDA_TITLE_RE.fullmatch(_compact(text)))


def _is_notes_heading(text: str) -> bool:
    return bool(_NOTES_TITLE_RE.fullmatch(_compact(text)))


def _is_top_level_section_heading(text: str) -> bool:
    return bool(_TOP_LEVEL_SECTION_RE.match(_compact(text)))


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).strip()


def _score_blocks(
    blocks: tuple[_Block, ...],
    sections: tuple[_Section, ...],
    signal_ids: tuple[str, ...],
    document_id: str,
    source_sha256: str,
    report_year: int | None,
    max_chars: int,
) -> tuple[_ScoredBlock, ...]:
    scored: list[_ScoredBlock] = []
    context_terms = _CONTEXT_TERMS + (
        (str(report_year), str(report_year - 1)) if report_year is not None else ()
    )
    for section in sections:
        for index in range(section.start, section.end):
            block = blocks[index]
            if len(block.text) > max_chars:
                continue
            normalized = _compact(block.text)
            has_cash = _contains_any(normalized, _CASH_TERMS)
            has_operating_cash = _contains_any(normalized, _OPERATING_CASH_TERMS)
            has_nonoperating_cash = _contains_any(normalized, _NON_OPERATING_CASH_TERMS)
            cash_is_relevant = has_cash and (has_operating_cash or not has_nonoperating_cash)
            has_context = _contains_any(normalized, context_terms)
            has_explanation = _contains_any(normalized, _EXPLANATION_TERMS)
            has_cash_detail = _contains_any(normalized, _CASH_DETAIL_TERMS)

            matched_signals: list[str] = []
            score = 0
            for signal_id in signal_ids:
                target_terms = _PROFIT_TERMS if signal_id == "profit_up_cash_down" else _REVENUE_TERMS
                has_target = _contains_any(normalized, target_terms)
                if not cash_is_relevant and not (has_target and (has_context or has_explanation)):
                    continue
                matched_signals.append(signal_id)
                score = max(score, 4 * int(cash_is_relevant) + 3 * int(has_target))

            if not matched_signals:
                continue
            score += 2 * int(cash_is_relevant and _contains_any(normalized, _OPERATING_CASH_TERMS))
            score += int(has_context) + int(has_explanation) + int(has_cash_detail)
            if section.label == "mda":
                score += 1
            reason = _selection_reason(cash_is_relevant, has_target_any=any(
                _contains_any(normalized, _PROFIT_TERMS if signal_id == "profit_up_cash_down" else _REVENUE_TERMS)
                for signal_id in matched_signals
            ), has_explanation=has_explanation)
            evidence_id = _evidence_id(document_id, source_sha256, section.label, block)
            scored.append(
                _ScoredBlock(
                    block,
                    section.label,
                    score,
                    tuple(matched_signals),
                    reason,
                    evidence_id,
                )
            )
    return tuple(
        sorted(
            scored,
            key=lambda item: (
                -item.score,
                item.block.page,
                item.block.bbox[1],
                item.block.bbox[0],
                item.evidence_id,
            ),
        )
    )


def _selection_reason(cash_relevant: bool, *, has_target_any: bool, has_explanation: bool) -> str:
    matched: list[str] = []
    if cash_relevant:
        matched.append("经营现金流词项")
    if has_target_any:
        matched.append("利润或营业收入词项")
    if has_explanation:
        matched.append("披露说明词项")
    joined = "、".join(matched)
    return f"本地确定性词项匹配：{joined}；仅保留原文上下文，不表示因果解释或核验结论。"


def _select_bounded(
    candidates: tuple[_ScoredBlock, ...],
    max_snippets: int,
    max_total_chars: int,
) -> tuple[_ScoredBlock, ...]:
    selected: list[_ScoredBlock] = []
    selected_ids: set[str] = set()
    total_chars = 0

    # Keep both located sections visible when relevant material exists in both.
    for section_label in ("mda", "financial_notes"):
        section_candidate = next((item for item in candidates if item.section_label == section_label), None)
        if section_candidate is not None:
            if len(selected) >= max_snippets or total_chars + len(section_candidate.block.text) > max_total_chars:
                continue
            selected.append(section_candidate)
            selected_ids.add(section_candidate.evidence_id)
            total_chars += len(section_candidate.block.text)

    for item in candidates:
        if item.evidence_id in selected_ids:
            continue
        if len(selected) >= max_snippets:
            break
        if total_chars + len(item.block.text) > max_total_chars:
            continue
        selected.append(item)
        selected_ids.add(item.evidence_id)
        total_chars += len(item.block.text)
    return tuple(selected)


def _evidence_id(document_id: str, source_sha256: str, section: str, block: _Block) -> str:
    canonical_bbox = ",".join(repr(value) for value in block.bbox)
    material = "\n".join((document_id, source_sha256, section, str(block.page), canonical_bbox, block.text))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"nctx-{source_sha256[:12]}-p{block.page}-{digest}"


def _abstain(
    reason: str,
    *,
    signal_ids: tuple[str, ...] = (),
    source_document_id: str | None = None,
    source_sha256: str | None = None,
    sections_found: tuple[str, ...] = (),
) -> AnnualContextResult:
    signal_results = tuple(SignalContextResult(signal_id, "abstained", (), reason) for signal_id in signal_ids)
    return AnnualContextResult(
        status="abstained",
        source_document_id=source_document_id,
        source_sha256=source_sha256,
        signal_results=signal_results,
        sections_found=sections_found,
        snippets=(),
        reason=reason,
    )


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _nonempty_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalized_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _SHA256_RE.fullmatch(normalized) else None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
