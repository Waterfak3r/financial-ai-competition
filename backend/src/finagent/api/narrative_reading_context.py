"""Bounded, source-PDF-only paragraph context for narrative evidence display."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_MAX_BLOCKS = 5
_MAX_CONTEXT_CHARS = 1800
_MAX_NEIGHBOR_CHARS = 700
_MAX_VERTICAL_GAP = 12.0
_MAX_LEFT_EDGE_DELTA = 28.0
_MIN_HORIZONTAL_OVERLAP = 0.72
_INDENT_DELTA = 12.0
_MIN_PROSE_CHARS = 24
_TERMINAL_PUNCTUATION = ("。", "！", "？", "!", "?", ".", ";", "；")
_NUMBER_RUN = re.compile(r"\d+(?:[,，]\d{3})*(?:\.\d+)?%?")
_HEADING_PREFIX = re.compile(r"^(?:第[一二三四五六七八九十百\d]+节|[一二三四五六七八九十百\d]+[、.．])")


def build_reading_context(
    blocks: Sequence[Sequence[Any]],
    citation_block_index: int,
    *,
    pdf_page: int,
    page_width: float,
    source_sha256: str,
) -> dict[str, Any]:
    """Join only nearby, same-column prose blocks around a verified source block.

    The caller must first bind the archived citation text and bbox to this exact
    PDF. Every returned block is copied from that PDF, never from report JSON.
    """

    target = _read_block(blocks, citation_block_index)
    if target is None:
        raise ValueError("citation block is not a text block")

    selected: dict[int, tuple[float, float, float, float, str]] = {citation_block_index: target}
    limited_reason: str | None = None
    boundary_reason: str | None = None

    # Walk backward and forward independently so a paragraph boundary on one
    # side does not prevent recovering safe context from the other side.
    for direction in (-1, 1):
        current_index = citation_block_index
        current = target
        for _ in range(_MAX_BLOCKS + 1):
            candidate_index = current_index + direction
            if candidate_index < 0 or candidate_index >= len(blocks):
                boundary_reason = boundary_reason or "page_edge"
                break
            candidate = _read_block(blocks, candidate_index)
            if candidate is None:
                if limited_reason is None and _nearby_vertical(blocks[candidate_index], current, direction):
                    limited_reason = "unsafe_neighbor"
                else:
                    boundary_reason = boundary_reason or "non_text_boundary"
                break
            if len(candidate[4]) > _MAX_NEIGHBOR_CHARS:
                limited_reason = "character_limit"
                break

            relation = _neighbor_relation(candidate, current, direction, page_width)
            if relation == "paragraph_boundary":
                boundary_reason = boundary_reason or "paragraph_boundary"
                break
            if relation != "continuation":
                if relation == "unsafe" and limited_reason is None:
                    limited_reason = "unsafe_neighbor"
                else:
                    boundary_reason = boundary_reason or "safe_boundary"
                break

            candidate_text = _normalize_pdf_text(candidate[4])
            current_text = _normalize_pdf_text(current[4])
            # A terminal sentence in a separate PDF text block is a reliable
            # stopping point; do not concatenate the next paragraph by guess.
            previous_text = candidate_text if direction == -1 else current_text
            if previous_text.endswith(_TERMINAL_PUNCTUATION):
                boundary_reason = boundary_reason or "sentence_boundary"
                break

            if candidate_index not in selected and len(selected) >= _MAX_BLOCKS:
                limited_reason = "block_limit"
                break
            proposed_text = _join_block_texts(
                [block[4] for _, block in sorted({**selected, candidate_index: candidate}.items())]
            )
            proposed_raw_chars = sum(len(block[4]) for block in [*selected.values(), candidate])
            if len(proposed_text) > _MAX_CONTEXT_CHARS or proposed_raw_chars > _MAX_CONTEXT_CHARS:
                limited_reason = "character_limit"
                break

            previous_current = current
            selected[candidate_index] = candidate
            current_index = candidate_index
            current = candidate

            # A modest first-line inset marks the start of a paragraph. Include
            # that block when walking backward, then stop before its predecessor.
            if direction == -1 and candidate[0] > previous_current[0] + _INDENT_DELTA:
                boundary_reason = "paragraph_start"
                break
            # When walking forward, do not include an indented next paragraph.
            if direction == 1 and candidate[0] > target[0] + _INDENT_DELTA:
                del selected[candidate_index]
                boundary_reason = "paragraph_start"
                break

    ordered = [(index, block) for index, block in sorted(selected.items())]
    context_text = _join_block_texts([block[4] for _, block in ordered])
    x0 = min(block[0] for _, block in ordered)
    y0 = min(block[1] for _, block in ordered)
    x1 = max(block[2] for _, block in ordered)
    y1 = max(block[3] for _, block in ordered)
    expanded = len(ordered) > 1
    return {
        "purpose": "display_only",
        "source_pdf_sha256": source_sha256,
        "pdf_page": pdf_page,
        "text": context_text,
        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "expanded": expanded,
        "limited": limited_reason is not None,
        "limit_reason": limited_reason,
        "boundary_reason": boundary_reason,
        "citation_block_index": citation_block_index,
        "source_blocks": [
            {
                "block_index": index,
                "bbox": {"x0": block[0], "y0": block[1], "x1": block[2], "y1": block[3]},
                "raw_text": block[4],
            }
            for index, block in ordered
        ],
    }


def _read_block(
    blocks: Sequence[Sequence[Any]], index: int
) -> tuple[float, float, float, float, str] | None:
    block = blocks[index]
    if len(block) < 5 or (len(block) > 6 and block[6] != 0):
        return None
    text = block[4]
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in block[:4])
    except (TypeError, ValueError):
        return None
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1, text)


def _neighbor_relation(
    candidate: tuple[float, float, float, float, str],
    current: tuple[float, float, float, float, str],
    direction: int,
    page_width: float,
) -> str:
    if not _is_body_prose(candidate[4]) or not _is_body_prose(current[4]):
        return "unsafe"
    gap = candidate[1] - current[3] if direction == 1 else current[1] - candidate[3]
    if gap < -1.5 or gap > _MAX_VERTICAL_GAP:
        return "boundary"
    if abs(candidate[0] - current[0]) > _MAX_LEFT_EDGE_DELTA:
        return "unsafe"
    overlap = max(0.0, min(candidate[2], current[2]) - max(candidate[0], current[0]))
    shorter_width = min(candidate[2] - candidate[0], current[2] - current[0])
    if shorter_width <= 0 or overlap / shorter_width < _MIN_HORIZONTAL_OVERLAP:
        return "unsafe"
    if min(candidate[2] - candidate[0], current[2] - current[0]) < page_width * 0.34:
        return "unsafe"

    if direction == -1 and current[0] > candidate[0] + _INDENT_DELTA:
        return "paragraph_boundary"
    if direction == 1 and candidate[0] > current[0] + _INDENT_DELTA:
        return "paragraph_boundary"
    return "continuation"


def _is_body_prose(text: str) -> bool:
    normalized = _normalize_pdf_text(text)
    compact = re.sub(r"\s+", "", normalized)
    if len(compact) < _MIN_PROSE_CHARS or _HEADING_PREFIX.match(compact):
        return False
    letters = sum(char.isalpha() or _is_han(char) for char in compact)
    if letters / max(1, len(compact)) < 0.58:
        return False
    if len(_NUMBER_RUN.findall(compact)) >= 2 and not any(mark in compact for mark in _TERMINAL_PUNCTUATION):
        return False
    return True


def _normalize_pdf_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = ""
    for line in lines:
        result = _join_boundary(result, line)
    return result


def _join_block_texts(texts: Sequence[str]) -> str:
    result = ""
    for text in texts:
        normalized = _normalize_pdf_text(text)
        if normalized:
            result = _join_boundary(result, normalized)
    return result


def _join_boundary(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    previous = left[-1]
    following = right[0]
    if _is_han(previous) and _is_han(following):
        return left + right
    if previous.isascii() and previous.isalnum() and following.isascii() and following.isalnum():
        return left + " " + right
    # Keep original punctuation and ordinary language spacing. An English
    # word at a PDF line boundary needs a visible separator; CJK text does not.
    if previous.isascii() and previous.isalpha() and following.isascii() and following.isalpha():
        return left + " " + right
    if previous in ".,;:!?)]}" and following.isascii() and following.isalnum():
        return left + " " + right
    return left + right


def _is_han(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _nearby_vertical(raw_block: Sequence[Any], current: tuple[float, float, float, float, str], direction: int) -> bool:
    if len(raw_block) < 4:
        return False
    try:
        y0, y1 = float(raw_block[1]), float(raw_block[3])
    except (TypeError, ValueError):
        return False
    gap = y0 - current[3] if direction == 1 else current[1] - y1
    return -1.5 <= gap <= _MAX_VERTICAL_GAP
