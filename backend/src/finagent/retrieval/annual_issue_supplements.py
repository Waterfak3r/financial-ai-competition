"""Deterministic same-source retrieval for annual issue supplements.

This module scans already parsed text blocks and records source locations. It
does not make semantic verification claims; model role assignment and durable
stage auditing remain in the annual issue pipeline orchestrator.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from finagent.schemas.text_pdf import ParsedTextPdf


_STOP = frozenset({
    "以及", "因为", "所以", "但是", "可能", "相关", "公司", "报告", "存在", "问题", "情况",
    "其中", "有关", "进行", "需要", "当前", "说明", "风险", "事项", "年度", "期间",
})
_REQUESTED_SECTIONS = ("management_discussion", "financial_statements", "notes")
_MAX_TERMS_PER_ISSUE = 40
_MAX_MATCHES_PER_ISSUE = 4
_MAX_EXCERPT_CHARS = 500
_SELECTION_ALGORITHM = "section_filter_then_ngram_overlap_v1"


def retrieve_annual_issue_supplements(
    issues: Sequence[Mapping[str, Any]],
    parsed: ParsedTextPdf,
    source: Mapping[str, Any],
    *,
    max_role_assignments: int = 32,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Find bounded same-document text windows with auditable PDF locations."""

    if type(max_role_assignments) is not int or max_role_assignments < 0:
        raise ValueError("annual_issue_retrieval_limit_invalid")
    section = "unknown"
    all_blocks: list[dict[str, Any]] = []
    seen_sections: set[str] = set()
    unread_pages: list[int] = []
    unread_blocks = 0
    for page in parsed.pages:
        if page.status != "extracted":
            unread_pages.append(page.page_number)
            unread_blocks += len(page.blocks)
        for block in page.blocks:
            detected = section_from_heading(block.text)
            if detected is not None:
                section = detected
            seen_sections.add(section)
            all_blocks.append({
                "page": page.page_number,
                "block_index": block.block_index,
                "bbox": block.bbox,
                "text": block.text,
                "section": section,
            })

    assignments: dict[str, dict[str, Any]] = {}
    matched_pairs: set[tuple[str, int, int]] = set()
    for issue in issues:
        issue_id = issue.get("issue_id")
        if not isinstance(issue_id, str):
            continue
        primary_locations = {
            (cite.get("pdf_page"), cite.get("block_index"))
            for cite in issue.get("citations", []) if isinstance(cite, Mapping)
        }
        terms = _terms(" ".join(str(issue.get(key, "")) for key in ("title", "observation", "why_it_matters", "follow_up")))[:_MAX_TERMS_PER_ISSUE]
        scored = []
        for block in all_blocks:
            if (block["page"], block["block_index"]) in primary_locations:
                continue
            section_name = block["section"]
            if section_name not in _REQUESTED_SECTIONS:
                continue
            compact = re.sub(r"\s+", "", block["text"])
            score = sum(1 for term in terms if term in compact)
            if score <= 0:
                continue
            match = _match_window(block["text"], terms)
            if match is None:
                continue
            matched_pairs.add((issue_id, block["page"], block["block_index"]))
            start, excerpt = match
            end = min(len(block["text"]), start + len(excerpt))
            evidence_id = supplement_evidence_id(source["source_sha256"], block["page"], block["block_index"], start)
            supplement = {
                "evidence_id": evidence_id,
                "source_document_id": source["source_document_id"],
                "source_sha256": source["source_sha256"],
                "pdf_page": block["page"],
                "block_index": block["block_index"],
                "start_char": start,
                "end_char": end,
                "bbox": {
                    "x0": block["bbox"].x0,
                    "y0": block["bbox"].y0,
                    "x1": block["bbox"].x1,
                    "y1": block["bbox"].y1,
                },
                "raw_text": excerpt,
                "section": section_name,
                "selection_reason": "same_source_keyword_context_match",
                "role": "unknown",
                "issue_ids": [issue_id],
                "verification_status": "unverified",
                "role_status": "pending",
                "independent_verification": False,
                "retrieval_is_verification": False,
            }
            scored.append((score, evidence_id, supplement))
        for _score, evidence_id, supplement in sorted(scored, reverse=True)[:_MAX_MATCHES_PER_ISSUE]:
            current = assignments.get(evidence_id)
            if current is None:
                assignments[evidence_id] = supplement
            else:
                current["issue_ids"] = sorted(set(current["issue_ids"]) | {issue_id})

    supplements = sorted(assignments.values(), key=lambda item: (item["pdf_page"], item["block_index"], item["start_char"]))
    complete = len(all_blocks) == sum(len(page.blocks) for page in parsed.pages) and not unread_pages
    selected_pairs = {
        (issue_id, item["pdf_page"], item["block_index"])
        for item in supplements for issue_id in item["issue_ids"]
    }
    scanned_chars = sum(len(block["text"]) for block in all_blocks)
    selected_excerpt_chars = sum(len(item["raw_text"]) for item in supplements)
    selected_unique_source_chars = selected_source_char_union_size(supplements)
    role_pairs = [(issue_id, item["evidence_id"]) for item in supplements for issue_id in item["issue_ids"]]
    coverage = {
        "source_document_id": source["source_document_id"],
        "source_sha256": source["source_sha256"],
        "scope": "same_source_extracted_text_blocks",
        "requested_sections": list(_REQUESTED_SECTIONS),
        "examined_sections": sorted(seen_sections),
        "total_pages": parsed.page_count,
        "total_text_blocks": len(all_blocks),
        "scanned_blocks": len(all_blocks),
        "matched_blocks": len({(page, block) for _issue, page, block in matched_pairs}),
        "matched_issue_block_pairs": len(matched_pairs),
        "selected_blocks": len({(page, block) for _issue, page, block in selected_pairs}),
        "selected_issue_block_pairs": len(selected_pairs),
        "unselected_issue_block_pairs": len(matched_pairs - selected_pairs),
        "scanned_chars": scanned_chars,
        "selected_excerpt_chars": selected_excerpt_chars,
        "selected_unique_source_chars": selected_unique_source_chars,
        "unread_pages": unread_pages,
        "unread_blocks": unread_blocks,
        "omitted_chars": max(0, scanned_chars - selected_unique_source_chars),
        "complete": complete,
        "selection_algorithm_version": _SELECTION_ALGORITHM,
        "max_terms_per_issue": _MAX_TERMS_PER_ISSUE,
        "max_matches_per_issue": _MAX_MATCHES_PER_ISSUE,
        "max_excerpt_chars": _MAX_EXCERPT_CHARS,
        "max_role_assignments": max_role_assignments,
        "reason": "images_or_nontext_pages_not_ocr_scanned" if unread_pages else None,
        "role_assignments_total": len(role_pairs),
        "role_assignments_requested": min(len(role_pairs), max_role_assignments),
        "role_assignments_pending": max(0, len(role_pairs) - max_role_assignments),
    }
    return supplements, coverage


def selected_source_char_union_size(items: Sequence[Mapping[str, Any]]) -> int:
    """Count unique source characters covered by selected windows per block."""

    intervals: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for item in items:
        page = item.get("pdf_page")
        block = item.get("block_index")
        start = item.get("start_char")
        end = item.get("end_char")
        if (
            type(page) is not int or type(block) is not int
            or type(start) is not int or type(end) is not int or start < 0 or end <= start
        ):
            raise ValueError("annual_issue_retrieval_location_invalid")
        intervals.setdefault((page, block), []).append((start, end))
    total = 0
    for rows in intervals.values():
        ordered = sorted(rows)
        current_start, current_end = ordered[0]
        for start, end in ordered[1:]:
            if start <= current_end:
                current_end = max(current_end, end)
            else:
                total += current_end - current_start
                current_start, current_end = start, end
        total += current_end - current_start
    return total


def section_from_heading(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if any(word in compact for word in ("管理层讨论与分析", "经营情况讨论与分析", "董事会报告", "经营情况回顾")):
        return "management_discussion"
    if any(word in compact for word in ("合并资产负债表", "合并利润表", "合并现金流量表", "主要会计数据和财务指标")):
        return "financial_statements"
    if any(word in compact for word in ("财务报表附注", "重要会计政策及会计估计", "财务报表项目注释", "关联方及关联交易")):
        return "notes"
    return None


def supplement_evidence_id(source_sha: str, page: int, block: int, start: int) -> str:
    suffix = hashlib.sha256(f"{source_sha}\n{page}\n{block}\n{start}".encode()).hexdigest()[:16]
    return f"aisup-{source_sha[:12]}-p{page}-b{block}-c{start}-{suffix}"


def _terms(value: str) -> list[str]:
    compact = re.sub(r"[\s，。；、：:（）()\[\]【】/\\]+", " ", value)
    english = [item.lower() for item in re.findall(r"[A-Za-z]{3,}", compact)]
    chinese: list[str] = []
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", compact):
        for size in (4, 3, 2):
            chinese.extend(segment[index:index + size] for index in range(max(0, len(segment) - size + 1)))
    counts: dict[str, int] = {}
    for term in [*english, *chinese]:
        if term not in _STOP:
            counts[term] = counts.get(term, 0) + 1
    return [term for term, _ in sorted(counts.items(), key=lambda pair: (-len(pair[0]), pair[0]))]


def _match_window(text: str, terms: Sequence[str]) -> tuple[int, str] | None:
    compact = re.sub(r"\s+", "", text)
    offsets = []
    for term in terms:
        index = compact.find(term)
        if index >= 0:
            offsets.append(index)
    if not offsets:
        return None
    start = max(0, min(offsets) - 100)
    seen = 0
    source_start = 0
    for pos, char in enumerate(text):
        if not char.isspace():
            if seen >= start:
                source_start = pos
                break
            seen += 1
    source_start = max(0, source_start - 100)
    return source_start, text[source_start:source_start + _MAX_EXCERPT_CHARS]
