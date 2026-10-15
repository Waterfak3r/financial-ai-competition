"""Whole-document, bounded text context planning for annual discovery.

The planner preserves every extractable text block in PDF order until an
explicit character, request-size, or batch-count budget is reached. It does
not select by section heading or financial keywords and never performs OCR.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from finagent.ingestion.parse_text_pdf import parse_text_pdf
from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox

MAX_FRAGMENT_CHARS = 1200
MAX_BATCH_RAW_CHARS = 12000
MAX_SERIALIZED_INPUT_CHARS = 24000
MAX_BATCHES = 128
MAX_TOTAL_TEXT_CHARS = 1_200_000
LEGACY_PLAN_VERSION = "annual_discovery_plan_v2"
COMPACT_PLAN_VERSION = "annual_discovery_plan_v3"
PERIOD_HEADER_PLAN_VERSION = "annual_discovery_plan_v4"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEADER_DATE = (
    r"(?:[0-9]{4}年(?:度|[0-9]{1,2}(?:[-－—–~～至][0-9]{1,2})?月"
    r"(?:[0-9]{1,2}日)?)?|[0-9]{4}[-./][0-9]{1,2}(?:[-./][0-9]{1,2})?)"
)
_STANDALONE_PERIOD_HEADER_RE = re.compile(
    rf"(?:{_HEADER_DATE}(?:[-－—–~～至]{_HEADER_DATE})?[,，、;；|]?)+"
)


class AnnualDiscoveryInputError(ValueError):
    """Report/source metadata failed its strict identity checks."""


class AnnualDiscoverySourceError(Exception):
    """A local source PDF could not be safely parsed or matched."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DiscoveryFragment:
    evidence_id: str
    pdf_page: int
    block_index: int
    start_char: int
    bbox: tuple[float, float, float, float]
    raw_text: str

    @property
    def char_count(self) -> int:
        return len(self.raw_text)

    def model_value(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "pdf_page": self.pdf_page,
            "raw_text": self.raw_text,
        }

    def evidence_value(self, document_id: str, source_sha256: str) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "source_document_id": document_id,
            "source_sha256": source_sha256,
            "pdf_page": self.pdf_page,
            "block_index": self.block_index,
            "start_char": self.start_char,
            "bbox": {
                "x0": self.bbox[0],
                "y0": self.bbox[1],
                "x1": self.bbox[2],
                "y1": self.bbox[3],
            },
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    batch_index: int
    fragments: tuple[DiscoveryFragment, ...]
    raw_chars: int
    serialized_input_chars: int
    plan_version: str = LEGACY_PLAN_VERSION
    aliases: tuple[tuple[str, str], ...] = ()

    period_headers: tuple[DiscoveryFragment, ...] = ()
    period_header_aliases: tuple[tuple[str, str], ...] = ()

    @property
    def evidence_fragments(self) -> tuple[DiscoveryFragment, ...]:
        """Unique citable sources; additional context is not primary coverage."""
        by_id = {item.evidence_id: item for item in self.fragments}
        for header in self.period_headers:
            previous = by_id.setdefault(header.evidence_id, header)
            if previous != header:
                raise ValueError("annual_discovery_source_fragment_collision")
        return tuple(by_id.values())

    @property
    def evidence_aliases(self) -> tuple[tuple[str, str], ...]:
        if self.plan_version == PERIOD_HEADER_PLAN_VERSION:
            validate_batch_sources(self)
        primary_ids = {item.evidence_id for item in self.fragments}
        return self.aliases + tuple(
            pair for pair in self.period_header_aliases if pair[0] not in primary_ids
        )
    @property
    def alias_by_evidence_id(self) -> dict[str, str]:
        return dict(self.aliases)

    @property
    def evidence_id_by_alias(self) -> dict[str, str]:
        return {alias: evidence_id for evidence_id, alias in self.evidence_aliases}


@dataclass(frozen=True, slots=True)
class AnnualDiscoveryContext:
    identity: Mapping[str, object]
    batches: tuple[DiscoveryBatch, ...]
    fragments: tuple[DiscoveryFragment, ...]
    coverage: Mapping[str, object]
    plan_version: str = LEGACY_PLAN_VERSION
    parse_seconds: float = 0.0
    planning_seconds: float = 0.0


    @property
    def evidence_fragments(self) -> tuple[DiscoveryFragment, ...]:
        by_id = {item.evidence_id: item for item in self.fragments}
        for batch in self.batches:
            for item in batch.evidence_fragments:
                previous = by_id.setdefault(item.evidence_id, item)
                if previous != item:
                    raise ValueError("annual_discovery_source_fragment_collision")
        return tuple(by_id.values())

def validate_annual_discovery_identity(
    report: Mapping[str, object], source_record: Mapping[str, object]
) -> dict[str, object]:
    """Validate report/source identity without reading the PDF or loading config."""

    if not isinstance(report, Mapping) or not isinstance(source_record, Mapping):
        raise AnnualDiscoveryInputError("report_and_source_record_must_be_mappings")
    if report.get("kind") != "fintrace_annual_analysis_report":
        raise AnnualDiscoveryInputError("unsupported_report_kind")

    run_id = report.get("run_id")
    company_id = report.get("company_id")
    report_year = report.get("report_year")
    document_id = report.get("source_document_id")
    source_sha = report.get("source_sha256")
    report_period = source_record.get("report_period")

    if not isinstance(run_id, str) or not _ID_RE.fullmatch(run_id) or ".." in run_id:
        raise AnnualDiscoveryInputError("report_run_id_missing_or_invalid")
    if not isinstance(company_id, str) or not _ID_RE.fullmatch(company_id):
        raise AnnualDiscoveryInputError("report_company_id_missing_or_invalid")
    if type(report_year) is not int or not 1900 <= report_year <= 2200:
        raise AnnualDiscoveryInputError("report_year_missing_or_invalid")
    if not isinstance(document_id, str) or not _ID_RE.fullmatch(document_id):
        raise AnnualDiscoveryInputError("report_source_document_id_missing_or_invalid")
    if not isinstance(source_sha, str) or not _SHA256_RE.fullmatch(source_sha):
        raise AnnualDiscoveryInputError("report_source_sha256_missing_or_invalid")
    source_sha = source_sha.lower()

    if not isinstance(report_period, str) or not _PERIOD_RE.fullmatch(report_period):
        raise AnnualDiscoveryInputError("source_record_report_period_missing_or_invalid")
    try:
        period_date = date.fromisoformat(report_period)
    except ValueError:
        raise AnnualDiscoveryInputError("source_record_report_period_missing_or_invalid") from None
    if period_date.year != report_year:
        raise AnnualDiscoveryInputError("report_period_year_mismatch")
    if source_record.get("document_id") != document_id:
        raise AnnualDiscoveryInputError("source_record_document_id_mismatch")
    if source_record.get("company_id") != company_id:
        raise AnnualDiscoveryInputError("source_record_company_id_mismatch")
    record_sha = source_record.get("sha256")
    if not isinstance(record_sha, str) or not _SHA256_RE.fullmatch(record_sha):
        raise AnnualDiscoveryInputError("source_record_sha256_missing_or_invalid")
    if record_sha.lower() != source_sha:
        raise AnnualDiscoveryInputError("source_record_sha256_mismatch")

    return {
        "run_id": run_id,
        "company_id": company_id,
        "report_year": report_year,
        "report_period": report_period,
        "source_document_id": document_id,
        "source_sha256": source_sha,
    }


def build_annual_discovery_context(
    report: Mapping[str, object],
    source_pdf_path: str | Path,
    source_record: Mapping[str, object],
    *,
    system_prompt: str,
    model_name: str,
    max_fragment_chars: int = MAX_FRAGMENT_CHARS,
    max_batch_raw_chars: int = MAX_BATCH_RAW_CHARS,
    max_serialized_chars: int = MAX_SERIALIZED_INPUT_CHARS,
    max_batches: int = MAX_BATCHES,
    max_total_chars: int = MAX_TOTAL_TEXT_CHARS,
    plan_version: str = LEGACY_PLAN_VERSION,
    parsed_pdf: ParsedTextPdf | None = None,
) -> AnnualDiscoveryContext:
    """Parse and plan the full extractable PDF text into bounded model batches.

    ``max_serialized_chars`` applies to the exact JSON request body shape used by
    the current Chat Completions connector (model, two messages, stream=false).
    User content, system prompt, and model name therefore all count against it.
    The public defaults are hard limits; lower values are useful for local tests.
    """

    _check_limits(
        max_fragment_chars=max_fragment_chars,
        max_batch_raw_chars=max_batch_raw_chars,
        max_serialized_chars=max_serialized_chars,
        max_batches=max_batches,
        max_total_chars=max_total_chars,
    )
    if plan_version not in {LEGACY_PLAN_VERSION, COMPACT_PLAN_VERSION, PERIOD_HEADER_PLAN_VERSION}:
        raise ValueError("annual_discovery_plan_version_unsupported")
    identity = validate_annual_discovery_identity(report, source_record)
    parse_seconds = 0.0
    if parsed_pdf is None:
        parse_started = time.perf_counter()
        try:
            parsed = parse_text_pdf(source_pdf_path, str(identity["source_document_id"]))
        except Exception:
            raise AnnualDiscoverySourceError("source_pdf_unreadable") from None
        parse_seconds = time.perf_counter() - parse_started
    else:
        parsed = parsed_pdf
        if (
            not isinstance(parsed, ParsedTextPdf)
            or parsed.document_id != identity["source_document_id"]
        ):
            raise AnnualDiscoverySourceError("source_parse_identity_mismatch")
    if parsed.source_sha256 != identity["source_sha256"]:
        raise AnnualDiscoverySourceError("source_pdf_hash_mismatch")
    planning_started = time.perf_counter()

    all_text_blocks = [
        (page.page_number, block.block_index, block.bbox, block.text)
        for page in parsed.pages
        for block in page.blocks
        if block.text.strip()
    ]
    total_chars = sum(len(block[3]) for block in all_text_blocks)
    total_blocks = len(all_text_blocks)
    text_page_numbers = {page.page_number for page in parsed.pages if page.blocks}
    no_text_pages = [page.page_number for page in parsed.pages if not page.blocks]
    image_pages = [page.page_number for page in parsed.pages if page.image_block_count > 0]
    unread_image_pages = [
        page.page_number for page in parsed.pages if not page.blocks and page.image_block_count > 0
    ]

    candidate_fragments: list[DiscoveryFragment] = []
    remaining_chars = max_total_chars
    limit_omitted_pages: set[int] = set()
    for page_number, block_index, bbox, raw_text in all_text_blocks:
        if remaining_chars <= 0:
            limit_omitted_pages.add(page_number)
            continue
        take = min(len(raw_text), remaining_chars)
        if take < len(raw_text):
            limit_omitted_pages.add(page_number)
        for start in range(0, take, max_fragment_chars):
            fragment_text = raw_text[start : min(start + max_fragment_chars, take)]
            if not fragment_text:
                continue
            candidate_fragments.append(
                DiscoveryFragment(
                    evidence_id=_evidence_id(
                        str(identity["source_sha256"]), page_number, block_index, start
                    ),
                    pdf_page=page_number,
                    block_index=block_index,
                    start_char=start,
                    bbox=_bbox_tuple(bbox),
                    raw_text=fragment_text,
                )
            )
        remaining_chars -= take

    headers_by_page: dict[int, tuple[DiscoveryFragment, ...]] = {}
    if plan_version == PERIOD_HEADER_PLAN_VERSION:
        primary_by_position = {
            (item.pdf_page, item.block_index, item.start_char): item
            for item in candidate_fragments
        }
        page_headers: dict[int, list[DiscoveryFragment]] = {}
        for page_number, block_index, bbox, raw_text in all_text_blocks:
            if not _STANDALONE_PERIOD_HEADER_RE.fullmatch(re.sub(r"\s+", "", raw_text)):
                continue
            headers = page_headers.setdefault(page_number, [])
            start = 0
            while start < len(raw_text):
                primary = primary_by_position.get((page_number, block_index, start))
                header = primary or DiscoveryFragment(
                    evidence_id=_evidence_id(str(identity["source_sha256"]), page_number, block_index, start),
                    pdf_page=page_number,
                    block_index=block_index,
                    start_char=start,
                    bbox=_bbox_tuple(bbox),
                    raw_text=raw_text[start : start + max_fragment_chars],
                )
                headers.append(header)
                start += header.char_count
        headers_by_page = {page: tuple(items) for page, items in page_headers.items()}

    batches: list[DiscoveryBatch] = []
    planned_fragments: list[DiscoveryFragment] = []
    unplanned_pages: set[int] = set(limit_omitted_pages)
    current: list[DiscoveryFragment] = []
    current_raw = 0
    current_headers: list[DiscoveryFragment] = []
    current_pages: set[int] = set()
    current_header_aliases: dict[str, str] = {}
    def empty_request_size(batch_index: int) -> int:
        payload = serialize_batch_content(identity, batch_index, [], plan_version=plan_version)
        return serialized_request_size(model_name, system_prompt, payload)

    def fragment_request_size(fragment: DiscoveryFragment, ordinal: int) -> int:
        value = (
            [f"b{_base36(len(batches) + 1)}-{_short_alias(ordinal)}", fragment.pdf_page, fragment.raw_text]
            if plan_version in {COMPACT_PLAN_VERSION, PERIOD_HEADER_PLAN_VERSION} else fragment.model_value()
        )
        row = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        # User content is itself a JSON string in the official Chat body.
        # Include its second escaping layer, but not the wrapping two quotes.
        return len(json.dumps(row, ensure_ascii=False)) - 2

    def header_request_delta(fragment: DiscoveryFragment, ordinal: int) -> int:
        if plan_version != PERIOD_HEADER_PLAN_VERSION:
            return 0
        primary_alias = f"b{_base36(len(batches) + 1)}-{_short_alias(ordinal)}"
        if fragment.pdf_page not in current_pages:
            delta = 0
            for index, header in enumerate(headers_by_page.get(fragment.pdf_page, ())):
                alias = (
                    primary_alias if header == fragment
                    else f"b{_base36(len(batches) + 1)}-h-{_short_alias(len(current_headers) + index)}"
                )
                delta += _compact_row_request_size(header, alias) + bool(current_headers or index)
            return delta
        previous_alias = current_header_aliases.get(fragment.evidence_id)
        if previous_alias is not None:
            return len(primary_alias) - len(previous_alias)
        return 0

    current_serialized = empty_request_size(1)

    def finalize_current() -> None:
        nonlocal current, current_raw, current_serialized, current_headers
        if not current:
            return
        batch_index = len(batches) + 1
        aliases = _batch_aliases(current, batch_index) if plan_version in {COMPACT_PLAN_VERSION, PERIOD_HEADER_PLAN_VERSION} else ()
        header_aliases = _period_header_aliases(current_headers, aliases, batch_index)
        payload = serialize_batch_content(
            identity, batch_index, current, plan_version=plan_version, aliases=aliases,
            period_headers=tuple(current_headers), period_header_aliases=header_aliases,
        )
        size = serialized_request_size(model_name, system_prompt, payload)
        if size != current_serialized:
            raise RuntimeError("annual_discovery_incremental_size_mismatch")
        batches.append(
            DiscoveryBatch(
                batch_index=batch_index,
                fragments=tuple(current),
                raw_chars=current_raw,
                serialized_input_chars=size,
                plan_version=plan_version,
                aliases=aliases,
                period_headers=tuple(current_headers),
                period_header_aliases=header_aliases,
            )
        )
        planned_fragments.extend(current)
        current = []
        current_raw = 0
        current_headers = []
        current_pages.clear()
        current_header_aliases.clear()
        current_serialized = empty_request_size(len(batches) + 1)

    for index, fragment in enumerate(candidate_fragments):
        if len(batches) >= max_batches:
            unplanned_pages.update(item.pdf_page for item in candidate_fragments[index:])
            break

        candidate_raw = current_raw + fragment.char_count
        candidate_serialized = (
            current_serialized + fragment_request_size(fragment, len(current)) + bool(current)
            + header_request_delta(fragment, len(current))
        )
        would_overflow_raw = candidate_raw > max_batch_raw_chars
        would_overflow_serialized = candidate_serialized > max_serialized_chars
        if current and (would_overflow_raw or would_overflow_serialized):
            finalize_current()
            if len(batches) >= max_batches:
                unplanned_pages.update(item.pdf_page for item in candidate_fragments[index:])
                break
            candidate_raw = fragment.char_count
            candidate_serialized = current_serialized + fragment_request_size(fragment, 0) + header_request_delta(fragment, 0)
            would_overflow_serialized = candidate_serialized > max_serialized_chars
        if candidate_raw > max_batch_raw_chars or would_overflow_serialized:
            unplanned_pages.update(item.pdf_page for item in candidate_fragments[index:])
            break
        if plan_version == PERIOD_HEADER_PLAN_VERSION:
            primary_alias = f"b{_base36(len(batches) + 1)}-{_short_alias(len(current))}"
            if fragment.pdf_page not in current_pages:
                for header in headers_by_page.get(fragment.pdf_page, ()):
                    alias = (
                        primary_alias if header == fragment
                        else f"b{_base36(len(batches) + 1)}-h-{_short_alias(len(current_headers))}"
                    )
                    current_headers.append(header)
                    current_header_aliases[header.evidence_id] = alias
                current_pages.add(fragment.pdf_page)
            elif fragment.evidence_id in current_header_aliases:
                current_header_aliases[fragment.evidence_id] = primary_alias
        current.append(fragment)
        current_raw = candidate_raw
        current_serialized = candidate_serialized
    finalize_current()

    planned_chars = sum(item.char_count for item in planned_fragments)
    unread_pages = sorted(unplanned_pages.union(unread_image_pages))
    truncated = bool(unplanned_pages) or planned_chars < min(total_chars, max_total_chars)
    coverage: dict[str, object] = {
        "total_pages": parsed.page_count,
        "text_pages": len(text_page_numbers),
        "total_blocks": total_blocks,
        "total_chars": total_chars,
        "planned_batches": len(batches),
        "processed_batches": 0,
        "successful_batches": 0,
        "partial_batches": 0,
        "failed_batches": 0,
        "batch_results": [],
        "processed_chars": 0,
        "attempted_chars": 0,
        "unattempted_batches": 0,
        "unattempted_chars": 0,
        "planned_chars": planned_chars,
        "omitted_chars": max(total_chars - planned_chars, 0),
        "unread_pages": unread_pages,
        "failed_pages": [],
        "no_text_pages": no_text_pages,
        "image_pages": image_pages,
        "unread_image_pages": unread_image_pages,
        "truncated": truncated,
        "complete": False,
        "source_status": "parsed",
        "content_status": "text_available" if total_chars > 0 else "no_extractable_text",
        "time_budget_exhausted": False,
        "text_only_scope": "all_extractable_text_blocks; no OCR; image-only content is outside scope",
    }
    coverage["complete"] = coverage_is_complete(coverage)

    # Defensive assertion protects future prompt/schema edits from silently
    # exceeding the actual connector request-size ceiling.
    for batch in batches:
        if batch.serialized_input_chars > max_serialized_chars:
            raise RuntimeError("annual_discovery_serialization_limit_breached")
        if batch.raw_chars > max_batch_raw_chars:
            raise RuntimeError("annual_discovery_raw_batch_limit_breached")

    return AnnualDiscoveryContext(
        identity=identity,
        batches=tuple(batches),
        fragments=tuple(planned_fragments),
        coverage=coverage,
        plan_version=plan_version,
        parse_seconds=parse_seconds,
        planning_seconds=time.perf_counter() - planning_started,
    )


def serialize_batch_content(
    identity: Mapping[str, object],
    batch_index: int,
    fragments: list[DiscoveryFragment] | tuple[DiscoveryFragment, ...],
    *,
    plan_version: str = LEGACY_PLAN_VERSION,
    aliases: tuple[tuple[str, str], ...] = (),
    period_headers: tuple[DiscoveryFragment, ...] = (),
    period_header_aliases: tuple[tuple[str, str], ...] = (),
) -> str:
    """Serialize the exact user message sent for one batch."""

    if plan_version in {COMPACT_PLAN_VERSION, PERIOD_HEADER_PLAN_VERSION}:
        aliases = aliases or _batch_aliases(fragments, batch_index)
        if plan_version == PERIOD_HEADER_PLAN_VERSION:
            period_header_aliases = period_header_aliases or _period_header_aliases(period_headers, aliases, batch_index)
            validate_batch_sources(DiscoveryBatch(
                batch_index, tuple(fragments), sum(item.char_count for item in fragments), 0,
                plan_version, aliases, period_headers, period_header_aliases,
            ))
        elif period_headers or period_header_aliases:
            raise ValueError("annual_discovery_alias_plan_mismatch")
        alias_by_id = dict(aliases)
        if set(alias_by_id) != {item.evidence_id for item in fragments}:
            raise ValueError("annual_discovery_alias_mapping_invalid")
        payload = {
            "company_id": identity["company_id"],
            "report_year": identity["report_year"],
            "source_document_id": identity["source_document_id"],
            "source_sha256": identity["source_sha256"],
            "batch_index": batch_index,
            "fragments": [
                [alias_by_id[item.evidence_id], item.pdf_page, item.raw_text]
                for item in fragments
            ],
        }
        if plan_version == PERIOD_HEADER_PLAN_VERSION:
            header_alias_by_id = dict(period_header_aliases)
            payload["period_headers"] = [
                [header_alias_by_id[item.evidence_id], item.pdf_page, item.raw_text]
                for item in period_headers
            ]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if plan_version != LEGACY_PLAN_VERSION:
        raise ValueError("annual_discovery_plan_version_unsupported")

    payload = {
        "company_id": identity["company_id"],
        "report_year": identity["report_year"],
        "source_document_id": identity["source_document_id"],
        "source_sha256": identity["source_sha256"],
        "batch_index": batch_index,
        "fragments": [item.model_value() for item in fragments],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _compact_row_request_size(fragment: DiscoveryFragment, alias: str) -> int:
    row = json.dumps([alias, fragment.pdf_page, fragment.raw_text], ensure_ascii=False, separators=(",", ":"))
    return len(json.dumps(row, ensure_ascii=False)) - 2


def _period_header_aliases(
    headers: list[DiscoveryFragment] | tuple[DiscoveryFragment, ...],
    primary_aliases: tuple[tuple[str, str], ...],
    batch_index: int,
) -> tuple[tuple[str, str], ...]:
    primary = dict(primary_aliases)
    return tuple(
        (item.evidence_id, primary.get(item.evidence_id, f"b{_base36(batch_index)}-h-{_short_alias(index)}"))
        for index, item in enumerate(headers)
    )


def validate_batch_sources(batch: DiscoveryBatch) -> None:
    """Reject foreign, overlapping, forged or nondeterministically aliased context."""
    if batch.plan_version == LEGACY_PLAN_VERSION:
        if batch.period_headers or batch.period_header_aliases:
            raise ValueError("annual_discovery_alias_plan_mismatch")
        return
    if batch.plan_version not in {COMPACT_PLAN_VERSION, PERIOD_HEADER_PLAN_VERSION}:
        raise ValueError("annual_discovery_plan_version_unsupported")
    if batch.aliases != _batch_aliases(batch.fragments, batch.batch_index):
        raise ValueError("annual_discovery_alias_mapping_invalid")
    if len({item.evidence_id for item in batch.fragments}) != len(batch.fragments):
        raise ValueError("annual_discovery_source_fragment_collision")
    if batch.plan_version == COMPACT_PLAN_VERSION:
        if batch.period_headers or batch.period_header_aliases:
            raise ValueError("annual_discovery_alias_plan_mismatch")
        return
    if batch.period_header_aliases != _period_header_aliases(batch.period_headers, batch.aliases, batch.batch_index):
        raise ValueError("annual_discovery_alias_mapping_invalid")
    pages = {item.pdf_page for item in batch.fragments}
    if any(item.pdf_page not in pages for item in batch.period_headers):
        raise ValueError("annual_discovery_period_header_page_mismatch")
    if len({item.evidence_id for item in batch.period_headers}) != len(batch.period_headers):
        raise ValueError("annual_discovery_source_fragment_collision")
    # Reusing a primary ID requires the entire original source interval, not
    # merely a matching start position or quote.
    batch.evidence_fragments


def _batch_aliases(
    fragments: list[DiscoveryFragment] | tuple[DiscoveryFragment, ...],
    batch_index: int,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item.evidence_id, f"b{_base36(batch_index)}-{_short_alias(index)}")
        for index, item in enumerate(fragments)
    )


def _short_alias(index: int) -> str:
    """Return a deterministic lowercase spreadsheet-style alias (a..z, aa...)."""

    if type(index) is not int or index < 0:
        raise ValueError("annual_discovery_alias_index_invalid")
    value = index + 1
    characters: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        characters.append(chr(ord("a") + remainder))
    return "".join(reversed(characters))


def _base36(value: int) -> str:
    if type(value) is not int or value < 1:
        raise ValueError("annual_discovery_alias_batch_invalid")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    characters: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        characters.append(alphabet[remainder])
    return "".join(reversed(characters))


def annual_discovery_plan_projection(context: AnnualDiscoveryContext) -> list[dict[str, object]] | dict[str, object]:
    """Return the canonical, version-specific execution binding projection."""

    if context.plan_version == LEGACY_PLAN_VERSION:
        return [
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
    if context.plan_version == PERIOD_HEADER_PLAN_VERSION:
        for batch in context.batches:
            validate_batch_sources(batch)
        def selectors(items: tuple[DiscoveryFragment, ...], aliases: tuple[tuple[str, str], ...]) -> list[dict[str, object]]:
            return [
                {
                    "alias": alias, "evidence_id": item.evidence_id,
                    "pdf_page": item.pdf_page, "block_index": item.block_index,
                    "start_char": item.start_char, "char_count": item.char_count,
                    "bbox": list(item.bbox),
                    "raw_text_sha256": hashlib.sha256(item.raw_text.encode("utf-8")).hexdigest(),
                }
                for item, (_evidence_id, alias) in zip(items, aliases, strict=True)
            ]
        return {
            "plan_version": PERIOD_HEADER_PLAN_VERSION,
            "batches": [
                {
                    "batch_index": batch.batch_index,
                    "fragments": selectors(batch.fragments, batch.aliases),
                    "period_headers": selectors(batch.period_headers, batch.period_header_aliases),
                }
                for batch in context.batches
            ],
        }
    if context.plan_version != COMPACT_PLAN_VERSION:
        raise ValueError("annual_discovery_plan_version_unsupported")
    return {
        "plan_version": COMPACT_PLAN_VERSION,
        "batches": [
            {
                "batch_index": batch.batch_index,
                "fragments": [
                    {
                        "alias": alias,
                        "evidence_id": fragment.evidence_id,
                        "pdf_page": fragment.pdf_page,
                        "block_index": fragment.block_index,
                        "start_char": fragment.start_char,
                        "char_count": fragment.char_count,
                        "bbox": list(fragment.bbox),
                    }
                    for fragment, (_evidence_id, alias) in zip(batch.fragments, batch.aliases, strict=True)
                ],
            }
            for batch in context.batches
        ],
    }


def serialized_request_size(model_name: str, system_prompt: str, user_content: str) -> int:
    """Count characters in the exact JSON body shape emitted by complete_chat."""

    request = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    return len(json.dumps(request, ensure_ascii=False))


def coverage_is_complete(coverage: Mapping[str, object]) -> bool:
    """Whether all extractable text was successfully processed within limits."""

    try:
        planned_batches = int(coverage["planned_batches"])
        processed_batches = int(coverage["processed_batches"])
        partial_batches = int(coverage.get("partial_batches", 0))
        successful_batches = int(coverage["successful_batches"])
        failed_batches = int(coverage["failed_batches"])
        planned_chars = int(coverage["planned_chars"])
        processed_chars = int(coverage["processed_chars"])
        total_chars = int(coverage["total_chars"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        coverage.get("source_status") == "parsed"
        and coverage.get("truncated") is False
        and total_chars > 0
        and not coverage.get("unread_image_pages")
        and planned_chars == total_chars
        and processed_batches == planned_batches
        and partial_batches == 0
        and successful_batches == planned_batches
        and failed_batches == 0
        and processed_chars == planned_chars
    )


def empty_coverage() -> dict[str, object]:
    """Safe coverage envelope when source parsing never produced page details."""

    return {
        "total_pages": 0,
        "text_pages": 0,
        "total_blocks": 0,
        "total_chars": 0,
        "planned_batches": 0,
        "processed_batches": 0,
        "successful_batches": 0,
        "partial_batches": 0,
        "failed_batches": 0,
        "batch_results": [],
        "processed_chars": 0,
        "attempted_chars": 0,
        "unattempted_batches": 0,
        "unattempted_chars": 0,
        "planned_chars": 0,
        "omitted_chars": 0,
        "unread_pages": [],
        "failed_pages": [],
        "no_text_pages": [],
        "image_pages": [],
        "unread_image_pages": [],
        "truncated": False,
        "complete": False,
        "source_status": "unavailable",
        "content_status": "unavailable",
        "time_budget_exhausted": False,
        "text_only_scope": "all_extractable_text_blocks; no OCR; image-only content is outside scope",
    }


def _check_limits(
    *,
    max_fragment_chars: int,
    max_batch_raw_chars: int,
    max_serialized_chars: int,
    max_batches: int,
    max_total_chars: int,
) -> None:
    values = (max_fragment_chars, max_batch_raw_chars, max_serialized_chars, max_batches, max_total_chars)
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("annual_discovery_limits_must_be_positive_integers")
    if (
        max_fragment_chars > MAX_FRAGMENT_CHARS
        or max_batch_raw_chars > MAX_BATCH_RAW_CHARS
        or max_serialized_chars > MAX_SERIALIZED_INPUT_CHARS
        or max_batches > MAX_BATCHES
        or max_total_chars > MAX_TOTAL_TEXT_CHARS
    ):
        raise ValueError("annual_discovery_limits_exceed_hard_maximum")


def _evidence_id(source_sha: str, page: int, block_index: int, start_char: int) -> str:
    # Every component is numeric or lowercase ASCII hex; IDs remain stable and
    # portable even when company/document identifiers contain non-ASCII text.
    material = f"{source_sha}\n{page}\n{block_index}\n{start_char}".encode("ascii")
    digest = hashlib.sha256(material).hexdigest()[:12]
    return f"adisc-{source_sha[:12]}-p{page}-b{block_index}-c{start_char}-{digest}"


def _bbox_tuple(bbox: PdfBBox) -> tuple[float, float, float, float]:
    return (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
