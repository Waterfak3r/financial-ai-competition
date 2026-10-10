"""Lossless typed-array wire format for the enumerated annual-review purpose projection."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from typing import Any

V3_WIRE_SCHEMA = "annual_model_review_v3_compact_v4"
V3_INPUT_PROJECTION_VERSION = "annual_review_v3_purpose_projection_v1"
ISSUE_INPUT_WIRE_SCHEMA = "annual_issue_input_compact_v1"
_TAGS = frozenset({"d", "l", "c", "r", "s", "o", "m"})
_CONTAINER_TAGS = frozenset({"d", "l", "c", "r"})
_NUMERIC_TAGS = {"d": 0, "l": 1, "c": 2, "r": 3, "s": 4, "o": 5, "m": 6}
_STRING_TAGS = {value: key for key, value in _NUMERIC_TAGS.items()}
_MISSING = object()
_CLAIM_EVIDENCE_REGION_KEYS = (
    "title_region", "row_region", "value_region", "unit_region", "column_region",
)
_CLAIM_EVIDENCE_OMIT_ALWAYS = frozenset({
    "coordinate_system", "coordinate_unit", "coordinate_note", "rotation", "width", "height",
    "image_block_count", "extractor", "extractor_version", "ocr_applied", "extraction_method",
})
_CLAIM_EVIDENCE_OMIT_NULL_ONLY = frozenset({"printed_page", "legacy_text_region", "surrounding_text"})


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _outer_json_cost(value: Any) -> int:
    """Measure compact JSON after it becomes escaped Chat message content."""

    content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(json.dumps(content, ensure_ascii=False))


def project_annual_review_v3_input(value: Mapping[str, Any]) -> dict[str, Any]:
    """Drop only enumerated mechanical fields from known source-evidence paths."""

    if not isinstance(value, Mapping):
        raise ValueError("annual_review_v3_projection_object_invalid")
    projected = copy.deepcopy(dict(value))

    claim_evidence = projected.get("claim_evidence_pool")
    if isinstance(claim_evidence, list):
        for row in claim_evidence:
            if not isinstance(row, dict):
                continue
            for key in _CLAIM_EVIDENCE_OMIT_ALWAYS:
                row.pop(key, None)
            for key in _CLAIM_EVIDENCE_OMIT_NULL_ONLY:
                if row.get(key, _MISSING) is None:
                    row.pop(key, None)
            for region_key in _CLAIM_EVIDENCE_REGION_KEYS:
                region = row.get(region_key)
                if isinstance(region, dict):
                    region.pop("bbox", None)

    comparability = projected.get("comparability")
    if isinstance(comparability, dict):
        _project_evidence_rows(comparability.get("evidence"), remove_bbox=True)

    _project_evidence_rows(projected.get("narrative_evidences"), remove_bbox=True)
    investigation_items = projected.get("investigation_items")
    if isinstance(investigation_items, list):
        for item in investigation_items:
            if isinstance(item, dict):
                _project_evidence_rows(item.get("citations"), remove_bbox=True)

    issue_hierarchy = projected.get("issue_hierarchy")
    if isinstance(issue_hierarchy, dict):
        candidates = issue_hierarchy.get("candidate_pool")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    _project_evidence_rows(candidate.get("citations"), remove_bbox=True)
        _project_evidence_rows(issue_hierarchy.get("supplement_pool"), remove_bbox=True)

    _alias_internal_verification_references(projected)
    return projected


def _project_evidence_rows(rows: Any, *, remove_bbox: bool) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if isinstance(row, dict) and remove_bbox:
            row.pop("bbox", None)


def _alias_internal_verification_references(value: dict[str, Any]) -> None:
    pool_specs = (
        ("claim_verification_pool", "cv"),
        ("independent_verification_pool", "iv"),
    )
    aliases: dict[str, dict[str, str]] = {}
    for pool_key, prefix in pool_specs:
        rows = value.get(pool_key)
        if not isinstance(rows, list):
            raise ValueError("annual_review_v3_projection_pool_invalid")
        mapping: dict[str, str] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("reference_id"), str):
                raise ValueError("annual_review_v3_projection_pool_invalid")
            original = row["reference_id"]
            if original in mapping:
                raise ValueError("annual_review_v3_projection_reference_collision")
            alias = f"{prefix}:{index}"
            if alias in mapping.values():
                raise ValueError("annual_review_v3_projection_alias_collision")
            mapping[original] = alias
            row["reference_id"] = alias
        aliases[pool_key] = mapping

    verification_ids: dict[str, str] = {}
    verification_ordinal = 0
    for pool_key, _prefix in pool_specs:
        for row in value[pool_key]:
            record = row.get("record")
            if not isinstance(record, dict):
                raise ValueError("annual_review_v3_projection_pool_invalid")
            if pool_key == "claim_verification_pool":
                record.pop("verified_at", None)
            verification_id = record.get("verification_id")
            if verification_id is not None:
                if not isinstance(verification_id, str) or verification_id in verification_ids:
                    raise ValueError("annual_review_v3_projection_verification_collision")
                alias = f"ver:{verification_ordinal}"
                if alias in verification_ids.values():
                    raise ValueError("annual_review_v3_projection_alias_collision")
                verification_ids[verification_id] = alias
                record["verification_id"] = alias
            verification_ordinal += 1

    for section_key in ("verified_claims", "interpretations"):
        section = value.get(section_key)
        if not isinstance(section, list):
            raise ValueError("annual_review_v3_projection_claims_invalid")
        for claim in section:
            if not isinstance(claim, dict):
                raise ValueError("annual_review_v3_projection_claims_invalid")
            references = claim.get("verification_refs")
            if not isinstance(references, list):
                raise ValueError("annual_review_v3_projection_claims_invalid")
            rebound: list[str] = []
            for reference in references:
                if not isinstance(reference, str) or reference not in aliases["claim_verification_pool"]:
                    raise ValueError("annual_review_v3_projection_reference_unmapped")
                rebound.append(aliases["claim_verification_pool"][reference])
            claim["verification_refs"] = rebound
            independent_reference = claim.get("independent_verification_ref")
            if independent_reference is not None:
                if (
                    not isinstance(independent_reference, str)
                    or independent_reference not in aliases["independent_verification_pool"]
                ):
                    raise ValueError("annual_review_v3_projection_reference_unmapped")
                claim["independent_verification_ref"] = aliases["independent_verification_pool"][independent_reference]


def _translate_review_wire_tags(
    value: Any, *, to_numeric: bool, _active: set[int] | None = None,
) -> Any:
    """Translate only typed-node tag positions, preserving integer payloads."""

    if value is None or type(value) in {bool, int, float, str}:
        return value
    if not isinstance(value, list) or not value:
        raise ValueError("annual_review_v3_wire_invalid")
    active = set() if _active is None else _active
    marker = id(value)
    if marker in active:
        raise ValueError("annual_review_v3_wire_cycle_invalid")
    active.add(marker)
    try:
        return _translate_review_wire_node(value, to_numeric=to_numeric, active=active)
    finally:
        active.remove(marker)


def _translate_review_wire_node(value: list[Any], *, to_numeric: bool, active: set[int]) -> Any:
    tag = value[0]
    if to_numeric:
        if not isinstance(tag, str) or tag not in _NUMERIC_TAGS:
            raise ValueError("annual_review_v3_wire_invalid")
        normalized_tag = tag
    else:
        if type(tag) is not int or tag not in _STRING_TAGS:
            raise ValueError("annual_review_v3_wire_invalid")
        normalized_tag = _STRING_TAGS[tag]

    output_tag: str | int = _NUMERIC_TAGS[normalized_tag] if to_numeric else normalized_tag
    if normalized_tag in {"s", "o"}:
        if len(value) != 2:
            raise ValueError("annual_review_v3_wire_invalid")
        return [output_tag, value[1]]
    if normalized_tag == "m":
        if len(value) != 1:
            raise ValueError("annual_review_v3_wire_invalid")
        return [output_tag]
    if normalized_tag == "d":
        if (len(value) - 1) % 2:
            raise ValueError("annual_review_v3_wire_invalid")
        translated = [output_tag]
        for offset in range(1, len(value), 2):
            translated.extend((value[offset], _translate_review_wire_tags(value[offset + 1], to_numeric=to_numeric, _active=active)))
        return translated
    if normalized_tag == "l":
        return [output_tag, *(_translate_review_wire_tags(child, to_numeric=to_numeric, _active=active) for child in value[1:])]
    if normalized_tag == "r":
        if len(value) != 3:
            raise ValueError("annual_review_v3_wire_invalid")
        return [output_tag, value[1], _translate_review_wire_tags(value[2], to_numeric=to_numeric, _active=active)]
    if normalized_tag == "c":
        if len(value) != 3 or not isinstance(value[1], list) or not isinstance(value[2], list):
            raise ValueError("annual_review_v3_wire_invalid")
        return [output_tag, value[1], [_translate_review_wire_tags(vector, to_numeric=to_numeric, _active=active) for vector in value[2]]]
    raise ValueError("annual_review_v3_wire_invalid")


def encode_annual_review_v3_wire(value: Mapping[str, Any]) -> dict[str, Any]:
    """Encode a purpose-projected review input into the numeric v4 envelope."""

    return _encode_typed_wire(value, schema_version=V3_WIRE_SCHEMA, review_wire=True)


def encode_annual_issue_input_wire(value: Mapping[str, Any]) -> dict[str, Any]:
    """Encode a complete issue-stage input without changing its business schema."""

    return _encode_typed_wire(value, schema_version=ISSUE_INPUT_WIRE_SCHEMA)


def _encode_typed_wire(
    value: Mapping[str, Any], *, schema_version: str, review_wire: bool = False,
) -> dict[str, Any]:

    if not isinstance(value, Mapping):
        raise ValueError("annual_review_v3_wire_object_invalid")

    key_counts: dict[str, int] = {}
    string_counts: dict[str, int] = {}
    active: set[int] = set()

    def collect(item: Any) -> None:
        if isinstance(item, Mapping):
            marker = id(item)
            if marker in active:
                raise ValueError("annual_review_v3_wire_cycle_invalid")
            active.add(marker)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("annual_review_v3_wire_object_invalid")
                    key_counts[key] = key_counts.get(key, 0) + 1
                    collect(child)
            finally:
                active.remove(marker)
        elif isinstance(item, list):
            marker = id(item)
            if marker in active:
                raise ValueError("annual_review_v3_wire_cycle_invalid")
            active.add(marker)
            try:
                for child in item:
                    collect(child)
            finally:
                active.remove(marker)
        elif isinstance(item, str):
            string_counts[item] = string_counts.get(item, 0) + 1
        elif type(item) is float and not math.isfinite(item):
            raise ValueError("annual_review_v3_wire_number_invalid")
        elif item is not None and type(item) not in {bool, int, float}:
            raise ValueError("annual_review_v3_wire_value_invalid")

    collect(value)
    key_pool = sorted(key_counts)
    key_index = {key: index for index, key in enumerate(key_pool)}
    string_pool = sorted(
        (item for item, count in string_counts.items() if count >= 2 and len(item) >= 16),
        key=lambda item: (-len(item), item),
    )
    string_index = {item: index for index, item in enumerate(string_pool)}

    def build_candidate(*, pool_strings: bool, pool_objects: bool) -> dict[str, Any]:
        active_encode: set[int] = set()

        def encode_node(
            item: Any, *, allow_column: bool = True, vector_root: bool = False,
            allow_missing: bool = False,
        ) -> Any:
            if item is _MISSING:
                if review_wire and allow_missing:
                    return ["m"]
                raise ValueError("annual_review_v3_wire_missing_invalid")
            if isinstance(item, Mapping):
                marker = id(item)
                if marker in active_encode:
                    raise ValueError("annual_review_v3_wire_cycle_invalid")
                active_encode.add(marker)
                try:
                    pairs: list[Any] = ["d"]
                    for key in sorted(item, key=key_index.__getitem__):
                        pairs.extend((key_index[key], encode_node(item[key])))
                    return pairs
                finally:
                    active_encode.remove(marker)
            if isinstance(item, list):
                marker = id(item)
                if marker in active_encode:
                    raise ValueError("annual_review_v3_wire_cycle_invalid")
                active_encode.add(marker)
                try:
                    children = [
                        encode_node(child, allow_missing=review_wire and vector_root)
                        for child in item
                    ]
                    ordinary = ["l", *children]
                    if review_wire and len(item) >= 2:
                        signatures = [
                            ("missing",) if child is _MISSING else
                            ("value", _compact_json(_semantic_value(child)))
                            for child in item
                        ]
                        if len(set(signatures)) == 1:
                            repeated = ["r", len(item), children[0]]
                            if _outer_json_cost(repeated) < _outer_json_cost(ordinary):
                                ordinary = repeated
                    if allow_column and len(item) >= 2 and all(isinstance(row, Mapping) for row in item):
                        columns = sorted(
                            {key for row in item for key in row},
                            key=key_index.__getitem__,
                        )
                        if columns:
                            if review_wire:
                                vectors = [
                                    encode_node(
                                        [row[key] if key in row else _MISSING for row in item],
                                        vector_root=True,
                                    )
                                    for key in columns
                                ]
                                columnar = ["c", [key_index[key] for key in columns], vectors]
                            else:
                                columnar = [
                                    "c",
                                    [key_index[key] for key in columns],
                                    [
                                        [encode_node(row[key]) if key in row else ["m"] for key in columns]
                                        for row in item
                                    ],
                                ]
                            if _outer_json_cost(columnar) < _outer_json_cost(ordinary):
                                return columnar
                    return ordinary
                finally:
                    active_encode.remove(marker)
            if isinstance(item, str) and pool_strings and item in string_index:
                return ["s", string_index[item]]
            if type(item) is float and not math.isfinite(item):
                raise ValueError("annual_review_v3_wire_number_invalid")
            if item is None or type(item) in {bool, int, float, str}:
                return item
            raise ValueError("annual_review_v3_wire_value_invalid")

        typed_data = encode_node(value)
        object_pool: list[Any] = []
        object_index: dict[str, int] = {}

        def is_container_node(item: Any) -> bool:
            return isinstance(item, list) and bool(item) and item[0] in _CONTAINER_TAGS

        def encoded_signature(item: Any) -> str:
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

        def semantic_children(item: list[Any]) -> list[Any]:
            if item[0] == "d":
                return item[2::2]
            if item[0] == "l":
                return item[1:]
            if item[0] == "c":
                return item[2] if review_wire else [cell for row in item[2] for cell in row]
            if item[0] == "r":
                return [item[2]]
            return []

        encoded_counts: dict[str, int] = {}
        seen_encoded: set[int] = set()

        def count_encoded(item: Any) -> None:
            if is_container_node(item):
                marker = id(item)
                if marker in seen_encoded:
                    return
                seen_encoded.add(marker)
                signature = encoded_signature(item)
                encoded_counts[signature] = encoded_counts.get(signature, 0) + 1
                for child in semantic_children(item):
                    count_encoded(child)

        if pool_objects:
            count_encoded(typed_data)
            selected_encoded = {
                signature
                for signature, count in encoded_counts.items()
                if count > 1
            }

            def intern(item: Any) -> Any:
                if not is_container_node(item):
                    return item
                signature = encoded_signature(item)
                tag = item[0]
                if tag == "d":
                    rebuilt = ["d"]
                    for offset in range(1, len(item), 2):
                        rebuilt.extend((item[offset], intern(item[offset + 1])))
                elif tag == "l":
                    rebuilt = ["l", *(intern(child) for child in item[1:])]
                elif tag == "c" and review_wire:
                    rebuilt = ["c", item[1], [intern(vector) for vector in item[2]]]
                elif tag == "c":
                    rebuilt = [
                        "c", item[1],
                        [[intern(cell) for cell in row] for row in item[2]],
                    ]
                else:
                    rebuilt = ["r", item[1], intern(item[2])]
                if signature not in selected_encoded:
                    return rebuilt
                prior = object_index.get(signature)
                if prior is not None:
                    return ["o", prior]
                index = len(object_pool)
                object_index[signature] = index
                object_pool.append(rebuilt)
                return ["o", index]

            typed_data = intern(typed_data)

        return {
            "schema_version": schema_version,
            "key_pool": key_pool,
            "string_pool": string_pool if pool_strings else [],
            "object_pool": object_pool,
            "data": typed_data,
        }

    candidates = [
        build_candidate(pool_strings=pool_strings, pool_objects=pool_objects)
        for pool_strings in (False, True)
        for pool_objects in (False, True)
    ]
    selected = min(
        candidates,
        key=lambda item: (
            _outer_json_cost(item),
            len(item["object_pool"]) + len(item["string_pool"]),
        ),
    )
    if review_wire:
        selected = {
            **selected,
            "data": _translate_review_wire_tags(selected["data"], to_numeric=True),
            "object_pool": [
                _translate_review_wire_tags(item, to_numeric=True)
                for item in selected["object_pool"]
            ],
        }
        decoded = decode_annual_review_v3_wire(selected)
    else:
        decoded = _decode_typed_wire(selected, expected_schema=schema_version)
    canonical_input = _compact_json(_semantic_value(value))
    if canonical_input != _compact_json(decoded):
        raise ValueError("annual_review_v3_wire_roundtrip_invalid")
    return selected


def _semantic_value(item: Any) -> Any:
    if isinstance(item, Mapping):
        return {key: _semantic_value(child) for key, child in item.items()}
    if isinstance(item, list):
        return [_semantic_value(child) for child in item]
    return item


def decode_annual_review_v3_wire(envelope: Any) -> dict[str, Any]:
    """Strictly decode numeric tags and restore only the purpose projection."""

    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != {"schema_version", "key_pool", "string_pool", "object_pool", "data"}
        or envelope.get("schema_version") != V3_WIRE_SCHEMA
    ):
        raise ValueError("annual_review_v3_wire_invalid")
    raw_objects = envelope.get("object_pool")
    normalized = {
        **envelope,
        "data": _translate_review_wire_tags(envelope.get("data"), to_numeric=False),
        "object_pool": [
            _translate_review_wire_tags(item, to_numeric=False) for item in raw_objects
        ] if isinstance(raw_objects, list) else raw_objects,
    }
    return _decode_typed_wire(normalized, expected_schema=V3_WIRE_SCHEMA, review_wire=True)


def decode_annual_issue_input_wire(envelope: Any) -> dict[str, Any]:
    """Strictly decode a complete issue-stage input envelope."""

    return _decode_typed_wire(envelope, expected_schema=ISSUE_INPUT_WIRE_SCHEMA)


def _decode_typed_wire(
    envelope: Any, *, expected_schema: str, review_wire: bool = False,
) -> dict[str, Any]:

    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != {"schema_version", "key_pool", "string_pool", "object_pool", "data"}
        or envelope.get("schema_version") != expected_schema
    ):
        raise ValueError("annual_review_v3_wire_invalid")
    key_pool = envelope.get("key_pool")
    string_pool = envelope.get("string_pool")
    object_pool = envelope.get("object_pool")
    if (
        not isinstance(key_pool, list)
        or any(not isinstance(item, str) for item in key_pool)
        or len(key_pool) != len(set(key_pool))
        or not isinstance(string_pool, list)
        or any(not isinstance(item, str) for item in string_pool)
        or len(string_pool) != len(set(string_pool))
        or not isinstance(object_pool, list)
    ):
        raise ValueError("annual_review_v3_wire_invalid")

    used_keys: set[int] = set()
    used_strings: set[int] = set()
    used_objects: set[int] = set()
    decoding: set[int] = set()
    resolving_objects: set[int] = set()
    missing = object()
    tags = _TAGS if review_wire else frozenset({"d", "l", "c", "s", "o", "m"})
    container_tags = _CONTAINER_TAGS if review_wire else frozenset({"d", "l", "c"})

    def valid_index(index: Any, length: int) -> bool:
        return type(index) is int and 0 <= index < length

    def resolve_object(index: Any, defining_index: int | None) -> Any:
        if not valid_index(index, len(object_pool)) or (
            defining_index is not None and index >= defining_index
        ):
            raise ValueError("annual_review_v3_wire_invalid")
        if not (
            isinstance(object_pool[index], list)
            and object_pool[index]
            and isinstance(object_pool[index][0], str)
            and object_pool[index][0] in container_tags
        ):
            raise ValueError("annual_review_v3_wire_invalid")
        if index in resolving_objects:
            raise ValueError("annual_review_v3_wire_cycle_invalid")
        resolving_objects.add(index)
        used_objects.add(index)
        try:
            return decode(object_pool[index], defining_index=index)
        finally:
            resolving_objects.remove(index)

    def decode(item: Any, *, defining_index: int | None = None, allow_missing: bool = False) -> Any:
        if isinstance(item, list):
            marker = id(item)
            if marker in decoding:
                raise ValueError("annual_review_v3_wire_cycle_invalid")
            decoding.add(marker)
            try:
                if not item or not isinstance(item[0], str) or item[0] not in tags:
                    raise ValueError("annual_review_v3_wire_invalid")
                tag = item[0]
                if tag == "s":
                    if len(item) != 2 or not valid_index(item[1], len(string_pool)):
                        raise ValueError("annual_review_v3_wire_invalid")
                    used_strings.add(item[1])
                    return string_pool[item[1]]
                if tag == "o":
                    if len(item) != 2:
                        raise ValueError("annual_review_v3_wire_invalid")
                    return copy.deepcopy(resolve_object(item[1], defining_index))
                if tag == "m":
                    if len(item) != 1 or not allow_missing:
                        raise ValueError("annual_review_v3_wire_invalid")
                    return missing
                if tag == "l":
                    return [decode(child, defining_index=defining_index) for child in item[1:]]
                if tag == "r" and review_wire:
                    if len(item) != 3 or type(item[1]) is not int or item[1] < 2:
                        raise ValueError("annual_review_v3_wire_invalid")
                    repeated = decode(item[2], defining_index=defining_index)
                    return [copy.deepcopy(repeated) for _ in range(item[1])]
                if tag == "d":
                    if (len(item) - 1) % 2:
                        raise ValueError("annual_review_v3_wire_invalid")
                    result: dict[str, Any] = {}
                    for offset in range(1, len(item), 2):
                        index = item[offset]
                        if not valid_index(index, len(key_pool)):
                            raise ValueError("annual_review_v3_wire_invalid")
                        key = key_pool[index]
                        if key in result:
                            raise ValueError("annual_review_v3_wire_invalid")
                        used_keys.add(index)
                        result[key] = decode(item[offset + 1], defining_index=defining_index)
                    return result
                if tag == "c":
                    if len(item) != 3:
                        raise ValueError("annual_review_v3_wire_invalid")
                    columns, rows = item[1], item[2]
                    if review_wire:
                        if (
                            not isinstance(columns, list)
                            or any(not valid_index(index, len(key_pool)) for index in columns)
                            or len(columns) != len(set(columns))
                            or not columns
                            or not isinstance(rows, list)
                            or len(rows) != len(columns)
                        ):
                            raise ValueError("annual_review_v3_wire_invalid")
                        keys = [key_pool[index] for index in columns]
                        used_keys.update(columns)
                        vectors = [decode_vector(vector, defining_index=defining_index) for vector in rows]
                        vector_lengths = {len(vector) for vector in vectors}
                        if len(vector_lengths) != 1 or not vector_lengths or next(iter(vector_lengths)) < 2:
                            raise ValueError("annual_review_v3_wire_invalid")
                        return [
                            {
                                key: cell
                                for key, cell in zip(keys, row, strict=True)
                                if cell is not missing
                            }
                            for row in zip(*vectors, strict=True)
                        ]
                    if (
                        not isinstance(columns, list)
                        or any(not valid_index(index, len(key_pool)) for index in columns)
                        or len(columns) != len(set(columns))
                        or not isinstance(rows, list)
                        or len(rows) < 2
                    ):
                        raise ValueError("annual_review_v3_wire_invalid")
                    keys = [key_pool[index] for index in columns]
                    used_keys.update(columns)
                    decoded_rows: list[dict[str, Any]] = []
                    for row in rows:
                        if not isinstance(row, list) or len(row) != len(columns):
                            raise ValueError("annual_review_v3_wire_invalid")
                        decoded_cells = [
                            decode(cell, defining_index=defining_index, allow_missing=True)
                            for cell in row
                        ]
                        decoded_rows.append({
                            key: cell
                            for key, cell in zip(keys, decoded_cells, strict=True)
                            if cell is not missing
                        })
                    return decoded_rows
                raise ValueError("annual_review_v3_wire_invalid")
            finally:
                decoding.remove(marker)
        if isinstance(item, Mapping):
            raise ValueError("annual_review_v3_wire_invalid")
        if type(item) is float and not math.isfinite(item):
            raise ValueError("annual_review_v3_wire_invalid")
        if item is None or type(item) in {bool, int, float, str}:
            return item
        raise ValueError("annual_review_v3_wire_invalid")

    def decode_vector(item: Any, *, defining_index: int | None) -> list[Any]:
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            raise ValueError("annual_review_v3_wire_invalid")
        if item[0] == "o":
            if (
                len(item) != 2 or not valid_index(item[1], len(object_pool))
                or defining_index is not None and item[1] >= defining_index
            ):
                raise ValueError("annual_review_v3_wire_invalid")
            index = item[1]
            pooled = object_pool[index]
            if not isinstance(pooled, list) or not pooled or pooled[0] not in {"l", "r", "c"}:
                raise ValueError("annual_review_v3_wire_invalid")
            if index in resolving_objects:
                raise ValueError("annual_review_v3_wire_cycle_invalid")
            used_objects.add(index)
            resolving_objects.add(index)
            try:
                if pooled[0] == "c":
                    decoded = decode(pooled, defining_index=index)
                    if not isinstance(decoded, list):
                        raise ValueError("annual_review_v3_wire_invalid")
                    return decoded
                return decode_vector(pooled, defining_index=index)
            finally:
                resolving_objects.remove(index)
        if item[0] == "l":
            return [
                decode(child, defining_index=defining_index, allow_missing=True)
                for child in item[1:]
            ]
        if item[0] == "r":
            if len(item) != 3 or type(item[1]) is not int or item[1] < 2:
                raise ValueError("annual_review_v3_wire_invalid")
            repeated = decode(item[2], defining_index=defining_index, allow_missing=True)
            return [copy.deepcopy(repeated) for _ in range(item[1])]
        if item[0] == "c":
            decoded = decode(item, defining_index=defining_index)
            if not isinstance(decoded, list):
                raise ValueError("annual_review_v3_wire_invalid")
            return decoded
        raise ValueError("annual_review_v3_wire_invalid")

    result = decode(envelope.get("data"))
    if (
        not isinstance(result, dict)
        or used_keys != set(range(len(key_pool)))
        or used_strings != set(range(len(string_pool)))
        or used_objects != set(range(len(object_pool)))
    ):
        raise ValueError("annual_review_v3_wire_invalid_pool_usage")
    return result
