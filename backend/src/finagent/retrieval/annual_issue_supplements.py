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
LEGACY_RETRIEVAL_VERSION = "section_filter_then_ngram_overlap_v1"
RETRIEVAL_VERSION = "source_anchor_then_ngram_v2"

_GENERIC_SOURCE_PHRASES = "本期 上期 期末 期初 主要是本期对收购形成的 主要由于本期增加减少所致 计提减值准备损失所致 原因 变动 同比 单位 项目 年度 有限 公司 集团 报告期 披露 情况 说明"


def _discriminative_source_term(term: str) -> bool:
    return (
        term not in _STOP and term not in _GENERIC_SOURCE_PHRASES
        and not re.fullmatch(r"(?:19|20)\d{2}(?:年|年度)?", term)
    )

def retrieve_annual_issue_supplements(
    issues: Sequence[Mapping[str, Any]],
    parsed: ParsedTextPdf,
    source: Mapping[str, Any],
    *,
    max_role_assignments: int = 32,
    retrieval_version: str = RETRIEVAL_VERSION,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Find bounded same-document text windows with auditable PDF locations."""

    if retrieval_version not in {LEGACY_RETRIEVAL_VERSION, RETRIEVAL_VERSION}:
        raise ValueError("annual_issue_retrieval_version_invalid")
    if parsed.document_id != source.get("source_document_id") or parsed.source_sha256 != source.get("source_sha256"):
        raise ValueError("annual_issue_retrieval_source_mismatch")
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
            compact = re.sub(r"\s+", "", block.text)
            detected = _section_from_compact(compact)
            if detected is not None:
                section = detected
            seen_sections.add(section)
            all_blocks.append({
                "page": page.page_number,
                "block_index": block.block_index,
                "bbox": block.bbox,
                "text": block.text,
                "compact": compact,
                "periods": (_periods(block.text) or None) if retrieval_version == RETRIEVAL_VERSION else None,
                "subjects": (_subjects(block.text) or None) if retrieval_version == RETRIEVAL_VERSION else None,
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
        anchored = _source_anchor_matches(issue, all_blocks, source) if retrieval_version == RETRIEVAL_VERSION else None
        scored = []
        for block in all_blocks:
            location = (block["page"], block["block_index"])
            section_name = block["section"]
            if anchored is not None:
                selected = anchored.get(location)
                if selected is None:
                    continue
                score, match, selection_reason = selected
            else:
                if location in primary_locations or section_name not in _REQUESTED_SECTIONS:
                    continue
                compact = block["compact"]
                score = sum(1 for term in terms if term in compact)
                if score <= 0:
                    continue
                match = _match_window(block["text"], terms, compact)
                if match is None:
                    continue
                selection_reason = "same_source_keyword_context_match"
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
                "selection_reason": selection_reason,
                "role": "unknown",
                "issue_ids": [issue_id],
                "verification_status": "unverified",
                "role_status": "pending",
                "independent_verification": False,
                "retrieval_is_verification": False,
            }
            scored.append((score, evidence_id, supplement))
        ranked = sorted(scored, reverse=True)
        if anchored is not None:
            local_rows = [row for row in ranked if row[0][0] >= 3]
            remote_rows = [row for row in ranked if row[0][0] < 3]
            note_pages: set[int] = set()
            distinct_notes = []
            remaining = []
            for row in remote_rows:
                page = row[2]["pdf_page"]
                if row[0][0] == 2 and page not in note_pages:
                    distinct_notes.append(row)
                    note_pages.add(page)
                else:
                    remaining.append(row)
            ranked = local_rows + distinct_notes + remaining
        for _score, evidence_id, supplement in ranked[:_MAX_MATCHES_PER_ISSUE]:
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
        "selection_algorithm_version": retrieval_version,
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


def retrieve_annual_issue_supplements_v1(
    issues: Sequence[Mapping[str, Any]], parsed: ParsedTextPdf, source: Mapping[str, Any],
    *, max_role_assignments: int = 32,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Frozen historical selection; never upgrades a missing archive version."""
    return retrieve_annual_issue_supplements(
        issues, parsed, source, max_role_assignments=max_role_assignments,
        retrieval_version=LEGACY_RETRIEVAL_VERSION,
    )


def _source_terms(text: str) -> list[str]:
    """Source-order short metrics/names first; model prose cannot evict them."""
    result: list[str] = []
    segments = re.findall(r"[\u3400-\u9fff]+|[A-Za-z]{3,}|-?\d[\d,]*(?:\.\d+)?%?", text)
    for size in (0, 2, 3, 4):
        for segment in segments:
            if size == 0:
                values = [segment] if not re.fullmatch(r"[\u3400-\u9fff]+", segment) or len(segment) <= 4 else []
            else:
                values = [segment[i:i + size] for i in range(len(segment) - size + 1)] if re.fullmatch(r"[\u3400-\u9fff]+", segment) else []
            for value in values:
                if value not in _STOP and value not in result:
                    result.append(value)
    return result


def _periods(text: str) -> set[str]:
    return set(re.findall(r"(?:19|20)\d{2}(?:年|年度)", text))


def _subjects(text: str) -> set[str]:
    # Explicit named subjects only; do not manufacture an entity from ngrams.
    return set(re.findall(r"[\u3400-\u9fffA-Za-z]{2,}(?:集团|有限公司|子公司)", text))


def _context_conflicts(
    block: Mapping[str, Any], reference_periods: set[str], reference_subjects: set[str],
) -> bool:
    periods, subjects = block["periods"], block["subjects"]
    return bool(
        periods and reference_periods and periods.isdisjoint(reference_periods)
        or subjects and reference_subjects and subjects.isdisjoint(reference_subjects)
    )


def _near_source(block: Mapping[str, Any], anchor: Mapping[str, Any]) -> bool:
    if block["page"] != anchor["page"] or block["block_index"] == anchor["block_index"]:
        return False
    box, origin = block["bbox"], anchor["bbox"]
    height = min(24.0, max(12.0, origin.y1 - origin.y0))
    vertical_overlap = min(box.y1, origin.y1) - max(box.y0, origin.y0)
    right_cell = box.x0 >= origin.x1 - 8 and vertical_overlap >= -height
    below = 0 <= box.y0 - origin.y1 <= max(48.0, height * 3)
    above = 0 <= origin.y0 - box.y1 <= max(36.0, height * 2)
    horizontal_overlap = min(box.x1, origin.x1) > max(box.x0, origin.x0)
    return right_cell or horizontal_overlap and (below or above)


def _source_anchor_matches(
    issue: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]], source: Mapping[str, Any],
) -> dict[tuple[int, int], tuple[tuple[int, int, int, int], tuple[int, str], str]]:
    by_location = {(block["page"], block["block_index"]): block for block in blocks}
    anchors: list[Mapping[str, Any]] = []
    quotes: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for citation in issue.get("citations", []):
        if not isinstance(citation, Mapping):
            raise ValueError("annual_issue_retrieval_citation_invalid")
        location = (citation.get("pdf_page"), citation.get("block_index"))
        block = by_location.get(location)
        quote, start = citation.get("quote"), citation.get("start_char")
        if (
            citation.get("source_document_id") != source.get("source_document_id")
            or citation.get("source_sha256") != source.get("source_sha256")
            or block is None or not isinstance(quote, str) or not quote.strip()
            or type(start) is not int or start < 0
        ):
            raise ValueError("annual_issue_retrieval_citation_invalid")
        offset = block["text"].find(quote, start)
        if offset < 0 or len(quote) > _MAX_EXCERPT_CHARS:
            raise ValueError("annual_issue_retrieval_citation_invalid")
        if location not in quotes:
            anchors.append(block)
            quotes[location] = []
        quotes[location].append((offset, quote))
    quoted_text = "\n".join(quote for rows in quotes.values() for _offset, quote in rows)
    reference = "\n".join(block["text"] for block in anchors)
    if source.get("report_year") is not None:
        reference += f"\n{source['report_year']}年度"
    reference_periods, reference_subjects = _periods(reference), _subjects(reference)
    quote_terms = _source_terms(quoted_text)
    # Generic period/entity suffixes do not identify a table metric. Numeric
    # peer rows must share a quoted metric, not merely 本期/主要/所致 or a year.
    subject_text = " ".join(_subjects(quoted_text))
    metric_terms = {
        term for term in quote_terms if re.fullmatch(r"[\u3400-\u9fff]{2,4}", term)
        and _discriminative_source_term(term)
        and term not in subject_text
    }
    local: dict[tuple[int, int], int] = {}
    peer_rows: set[tuple[int, int]] = set()
    for block in blocks:
        location = (block["page"], block["block_index"])
        text = block["text"]
        if location in quotes:
            # A cited block contributes context only when text beyond its
            # citations exists. Never concatenate separate quotes as an excerpt.
            cursor = 0
            has_context = False
            for offset, quote in sorted(quotes[location]):
                has_context = has_context or bool(text[cursor:offset].strip())
                cursor = max(cursor, offset + len(quote))
            if has_context or text[cursor:].strip():
                local[location] = 4
        elif not _context_conflicts(block, reference_periods, reference_subjects) and any(_near_source(block, anchor) for anchor in anchors):
            compact = block["compact"]
            header = bool(re.search(r"单位[：:]|项目.*(?:本期|上期|期末|期初)|(?:本期|上期)期末", text))
            if re.search(r"\d", text) and not header and not any(term in compact for term in metric_terms):
                peer_rows.add(location)
                continue
            explanatory = bool(re.search(r"主要|由于|所致|原因|because|due to", text))
            narrative = not header and not re.search(r"\d", text) and len(compact) >= 16
            local[location] = 3 if explanatory or narrative else 1
    adjacent_text = "\n".join(block["text"] for block in blocks if local.get((block["page"], block["block_index"])) == 3)
    raw_terms = _source_terms(reference)
    adjacent_terms = _source_terms(adjacent_text)
    # One forty-term budget: verified quotes first, then remaining raw source,
    # adjacent disclosure and model ngrams. Raw prefixes cannot evict quotes.
    # Seed short metrics and adjacent named disclosure before longer ngrams;
    # the same forty-term cap still applies to all remote matching.
    source_candidates = list(dict.fromkeys([
        *[term for term in quote_terms if term in metric_terms],
        *[term for term in adjacent_terms if re.fullmatch(r"[\u3400-\u9fff]{2,4}", term)],
        *quote_terms, *raw_terms, *adjacent_terms,
    ]))
    discriminative = [term for term in source_candidates if _discriminative_source_term(term)]
    terms = list(dict.fromkeys([
        *discriminative,
        *_terms(" ".join(str(issue.get(key, "")) for key in ("title", "observation", "why_it_matters", "follow_up"))),
    ]))[:_MAX_TERMS_PER_ISSUE]
    source_terms = set(discriminative)
    result = {}
    source_table_heading = ""
    heading_page = None
    for block in blocks:
        location = (block["page"], block["block_index"])
        text = block["text"]
        if block["page"] != heading_page:
            source_table_heading = ""
            heading_page = block["page"]
        if re.search(r"(?:^|\n)\s*(?:\(?\d+\)?[、.)．]|[一二三四五六七八九十]+[、.)．])(?=\s*[^\d\s])", text):
            source_table_heading = text
        if location in peer_rows or location in quotes and location not in local or _context_conflicts(block, reference_periods, reference_subjects):
            continue
        compact = block["compact"]
        hits = [term for term in terms if term in compact]
        source_hits = [term for term in hits if term in source_terms]
        rank = local.get(location, 0)
        if not rank and (block["section"] not in _REQUESTED_SECTIONS or not hits or anchors and not source_hits):
            continue
        metric_hits = [term for term in source_hits if term in metric_terms]
        subject_hits = [
            term for term in source_hits if term not in metric_terms
            and (term in subject_text or term in adjacent_text and len(term) >= 2 and not re.search(r"\d", term))
        ]
        numeric_hits = [term for term in source_hits if len(term.replace(",", "")) >= 5 and re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?%?", term)]
        if not rank and anchors and not (metric_hits or subject_hits or numeric_hits):
            continue
        if location in quotes:
            offset, quote = quotes[location][0]
            # A single continuous window, containing the entire primary quote
            # and up to 100 preceding characters where space permits.
            start = max(0, min(offset - 100, len(text) - _MAX_EXCERPT_CHARS))
            start = max(start, offset + len(quote) - _MAX_EXCERPT_CHARS)
            match = (start, text[start:start + _MAX_EXCERPT_CHARS])
            selection_reason = "source_anchor_context"
        elif rank:
            match = (0, text[:_MAX_EXCERPT_CHARS])
            selection_reason = "adjacent_source_context"
        else:
            match = _match_window(text, source_hits or hits, compact)
            selection_reason = "same_source_ngram_context_match"
        if match is None:
            continue
        concrete = bool(re.search(r"\d[\d,]*\.\d+|\d{5,}|下滑|备货|不利变化", text))
        policy = bool(re.search(r"会计政策|会计估计|会计处理|议案|测试报告", text))
        anchored_note = bool(metric_hits or subject_hits or numeric_hits)
        if not rank and anchored_note and concrete and not policy:
            rank = 2
        explanation = bool(re.search(r"下滑|备货|不利变化|销售价格|because|due to", text))
        # A repeated metric or balance is less specific than additional source
        # context (for example the acquired subject) retained in an adjacent
        # disclosure. Metric ngrams must not count again as subject anchors.
        specific_note = rank == 2 and block["section"] == "notes" and not policy and anchored_note
        matching_table_heading = specific_note and any(term in source_table_heading for term in metric_terms)
        score = (
            rank,
            int(specific_note) * 3 + int(concrete and not policy) + int(explanation) + int(bool(subject_hits)) * 2 + int(matching_table_heading) * 2,
            len(metric_hits) + len(subject_hits) + len(numeric_hits),
            len(source_hits),
        )
        result[location] = (score, match, selection_reason)
    return result


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
    return _section_from_compact(re.sub(r"\s+", "", text))


def _section_from_compact(compact: str) -> str | None:
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


def _match_window(text: str, terms: Sequence[str], compact: str) -> tuple[int, str] | None:
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
