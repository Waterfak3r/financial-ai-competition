from __future__ import annotations

import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import finagent.agents.annual_review as annual_review
from finagent.agents.annual_issue_pipeline import checkpoint_issue_pipeline
import finagent.audit.audited_chat as audited_chat
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import serialize_chat_body


def _wire_object(node: Any, key_pool: list[str]) -> dict[str, Any]:
    assert isinstance(node, list) and node and node[0] == 0
    return {key_pool[node[index]]: node[index + 1] for index in range(1, len(node), 2)}


def _walk_wire(value: Any):
    if isinstance(value, list):
        yield value
        for child in value:
            yield from _walk_wire(child)


def _wire_envelope(
    data: Any,
    *,
    key_pool: list[str] | None = None,
    string_pool: list[str] | None = None,
    object_pool: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": annual_review.V3_WIRE_SCHEMA,
        "key_pool": [] if key_pool is None else key_pool,
        "string_pool": [] if string_pool is None else string_pool,
        "object_pool": [] if object_pool is None else object_pool,
        "data": data,
    }


def test_review_v3_typed_wire_is_lossless_deterministic_and_rejects_bad_nodes() -> None:
    repeated = "同源附注上下文需要继续核查。" * 12
    shared_phrase = "经营活动现金流量净额及其与上期披露的差异"
    shared_record = {"evidence_id": "evidence:shared", "kind": "source_quote", "quote": repeated}
    semantic = {
        "schema_version": "annual_model_review_v3",
        "coverage": {"discovery_status": "partial", "scope_gaps": ["后续章节未扫描"]},
        "candidate_pool": [{
            "finding_id": f"finding:{index}", "status": "unverified",
            "evidence_ids": [f"evidence:{index}"], "observation": repeated,
        } for index in range(12)],
        "literal_marker_key": {"$s": 3},
        "$k0": "original key resembling a wire tag",
        "literal_marker_object": {"$s": 3},
        "literal_column_marker_object": {"$c": [["left", "right"], [[1, 2], [3, 4]]]},
        "literal_object_marker_object": {"$o": 0},
        "literal_text_marker_object": {"$t": ["literal", 0]},
        "literal_missing_marker_object": {"$m": 0},
        "marker_rows": [repeated, repeated],
        "shared_records": [copy.deepcopy(shared_record) for _ in range(4)],
        "heterogeneous_records": [
            {"finding_id": "finding:a", "long_observation": repeated},
            {"finding_id": "finding:b", "long_follow_up": repeated, "explicit_null": None},
            {"finding_id": "finding:c", "long_observation": repeated, "long_follow_up": repeated},
        ],
        "text_variants": [
            f"重点核对{shared_phrase}，并检查期间口径。",
            f"补充说明{shared_phrase}与附注中的联系。",
            f"重新核实{shared_phrase}，保留原始页码{chr(0xE000)}备注。",
        ],
        "allowed_ids": ["issue:one", "finding:one", "evidence:one"],
    }
    first = annual_review.encode_annual_review_v3_wire(semantic)
    second = annual_review.encode_annual_review_v3_wire(semantic)
    assert first == second
    assert annual_review.decode_annual_review_v3_wire(first) == semantic
    raw_wire = json.dumps(semantic, ensure_ascii=False, separators=(",", ":"))
    packed_wire = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    assert len(packed_wire) < len(raw_wire)
    assert set(first) == {"schema_version", "key_pool", "string_pool", "object_pool", "data"}
    assert first["schema_version"] == "annual_model_review_v3_compact_v4"
    assert first["key_pool"] == sorted(set(first["key_pool"]))
    assert first["string_pool"]
    assert any(
        isinstance(node, list) and node and node[0] == 2
        for node in _walk_wire(first["data"])
    )
    assert "$k0" in first["key_pool"] and "$t" in first["key_pool"]
    assert "key_aliases" not in first and "text_pool" not in first

    wire_fields = _wire_object(first["data"], first["key_pool"])
    for field in (
        "literal_marker_object", "literal_column_marker_object", "literal_object_marker_object",
        "literal_text_marker_object", "literal_missing_marker_object",
    ):
        assert wire_fields[field][0] == 0
    assert annual_review.decode_annual_review_v3_wire(first)["literal_marker_object"] == {"$s": 3}
    assert annual_review.decode_annual_review_v3_wire(first)["literal_column_marker_object"] == {"$c": [["left", "right"], [[1, 2], [3, 4]]]}
    assert annual_review.decode_annual_review_v3_wire(first)["literal_object_marker_object"] == {"$o": 0}
    assert annual_review.decode_annual_review_v3_wire(first)["literal_text_marker_object"] == {"$t": ["literal", 0]}
    assert annual_review.decode_annual_review_v3_wire(first)["literal_missing_marker_object"] == {"$m": 0}
    assert "\ue000" in annual_review.decode_annual_review_v3_wire(first)["text_variants"][2]
    assert semantic["allowed_ids"] == annual_review.decode_annual_review_v3_wire(first)["allowed_ids"]

    missing_and_null = _wire_envelope(
        [0, 2, [2, [0, 1], [[1, 1, 2], [1, None, [6]]]]],
        key_pool=["a", "b", "rows"],
    )
    assert annual_review.decode_annual_review_v3_wire(missing_and_null) == {
        "rows": [{"a": 1, "b": None}, {"a": 2}],
    }

    invalid_index = copy.deepcopy(first)
    invalid_index["data"] = [4, len(invalid_index["string_pool"])]
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(invalid_index)

    boolean_string_index = _wire_envelope([4, True], string_pool=["full source string"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(boolean_string_index)

    boolean_object_index = _wire_envelope([5, True], object_pool=[[0]])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(boolean_object_index)

    for invalid_pool_node in ([{"tag": "d"}], [[0]], [1], ["unknown"]):
        invalid_object_pool_tag = _wire_envelope([5, 0], object_pool=[invalid_pool_node])
        with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
            annual_review.decode_annual_review_v3_wire(invalid_object_pool_tag)

    boolean_key_index = _wire_envelope([0, True, 1], key_pool=["key"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(boolean_key_index)

    duplicate_output_keys = _wire_envelope([0, 0, 1, 0, 2], key_pool=["same_key"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(duplicate_output_keys)

    invalid_tag = _wire_envelope(["unknown", 1])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(invalid_tag)

    unused_key = _wire_envelope([0], key_pool=["not-used"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid_pool_usage"):
        annual_review.decode_annual_review_v3_wire(unused_key)

    unused_string = _wire_envelope([0], string_pool=["unused complete string"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid_pool_usage"):
        annual_review.decode_annual_review_v3_wire(unused_string)

    duplicate_key_pool = _wire_envelope([0], key_pool=["duplicate", "duplicate"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(duplicate_key_pool)

    unknown_legacy_alias_field = _wire_envelope([0])
    unknown_legacy_alias_field["key_aliases"] = {}
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(unknown_legacy_alias_field)

    malformed_column = _wire_envelope([2, [0], [[1, 1], [1, 2, 3]]], key_pool=["column"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(malformed_column)

    duplicate_column_keys = _wire_envelope([2, [0, 0], [[1, 1, 2], [1, 3, 4]]], key_pool=["column"])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(duplicate_column_keys)

    cyclic_source: list[Any] = []
    cyclic_source.append(cyclic_source)
    with pytest.raises(ValueError, match="annual_review_v3_wire_cycle_invalid"):
        annual_review.encode_annual_review_v3_wire({"cycle": cyclic_source})

    cyclic_wire: list[Any] = [1]
    cyclic_wire.append(cyclic_wire)
    with pytest.raises(ValueError, match="annual_review_v3_wire_cycle_invalid"):
        annual_review.decode_annual_review_v3_wire(_wire_envelope(cyclic_wire))

    forward_reference = _wire_envelope(
        [5, 0], key_pool=["child"], object_pool=[[0, 0, [5, 0]]],
    )
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(forward_reference)

    for nonfinite in (float("inf"), float("nan"), float("-inf")):
        with pytest.raises(ValueError, match="annual_review_v3_wire_number_invalid"):
            annual_review.encode_annual_review_v3_wire({"bad": nonfinite})
        invalid_nonfinite = _wire_envelope([0, 0, nonfinite], key_pool=["bad"])
        with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
            annual_review.decode_annual_review_v3_wire(invalid_nonfinite)


def test_review_v3_typed_object_pool_keeps_repeated_parents_after_child_interning() -> None:
    child = {"long_payload": list(range(200))}
    parent = {"long_parent_field": "same", "nested_record": child}
    envelope = annual_review.encode_annual_review_v3_wire({
        "first_parent": copy.deepcopy(parent),
        "second_parent": copy.deepcopy(parent),
    })
    key_pool = envelope["key_pool"]
    data = _wire_object(envelope["data"], key_pool)
    first = data["first_parent"]
    second = data["second_parent"]
    assert first == second and first[0] == 5
    assert envelope["object_pool"]
    decoded = annual_review.decode_annual_review_v3_wire(envelope)
    assert decoded["first_parent"] == decoded["second_parent"] == parent


def test_review_v3_projection_only_removes_allowlisted_evidence_locators_and_aliases_pool_ids() -> None:
    bbox = {"x0": 1, "y0": 2, "x1": 3, "y1": 4}
    original = {
        "source_identity": {"source_sha256": "a" * 64},
        "verified_facts": [{"fact_id": "fact:revenue", "value": "100", "unit": "CNY", "period": "2025"}],
        "claim_evidence_pool": [{
            "evidence_id": "claim-evidence:1", "raw_text": "完整原文",
            "title_region": {"bbox": bbox, "expected_text": "标题", "custom": {"bbox": {"preserve": True}}},
            "row_region": {"bbox": bbox}, "value_region": {"bbox": bbox},
            "unit_region": {"bbox": bbox}, "column_region": {"bbox": bbox},
            "coordinate_system": "pdf_points", "extraction_method": "table_parser",
            "printed_page": None, "legacy_text_region": "旧版完整文本", "surrounding_text": "邻近披露原文",
            "bbox": {"business_field": "保留"}, "expected_text": "应保留字段",
        }, {
            "evidence_id": "claim-evidence:2", "printed_page": "印刷第 12 页",
            "legacy_text_region": "完整旧版文本", "surrounding_text": "完整邻近原文",
            "unknown_business_path": {"bbox": "保留的业务位置"},
        }],
        "comparability": {"evidence": [{"bbox": bbox, "quote": "比较证据原文", "metadata": {"bbox": "business"}}]},
        "narrative_evidences": [{"bbox": bbox, "raw_text": "叙述证据原文"}],
        "investigation_items": [{"citations": [{"bbox": bbox, "raw_text": "调查引用原文"}]}],
        "issue_hierarchy": {
            "candidate_pool": [{"citations": [{"bbox": bbox, "quote": "问题引用原文"}]}],
            "supplement_pool": [{"bbox": bbox, "raw_text": "补证原文"}],
            "business_metadata": {"bbox": "不在来源证据白名单路径"},
        },
        "claim_verification_pool": [{
            "reference_id": "claim-verification:long-source-reference",
            "record": {"verification_id": "verification:long-id", "verified_at": "2026-10-08T04:00:00Z", "expected_text": "核验预期文本"},
        }],
        "independent_verification_pool": [{
            "reference_id": "independent-verification:long-source-reference",
            "record": {"verification_id": "independent-verification:long-id", "checks": ["完整核验检查"]},
        }],
        "verified_claims": [{
            "claim_id": "claim:1", "verification_refs": ["claim-verification:long-source-reference"],
            "independent_verification_ref": "independent-verification:long-source-reference",
        }],
        "interpretations": [{"claim_id": "claim:2", "verification_refs": [], "independent_verification_ref": None}],
    }
    projected = annual_review.project_annual_review_v3_input(original)

    assert original["claim_evidence_pool"][0]["title_region"]["bbox"] == bbox
    evidence = projected["claim_evidence_pool"][0]
    assert all("bbox" not in evidence[key] for key in ("title_region", "row_region", "value_region", "unit_region", "column_region"))
    assert evidence["title_region"]["expected_text"] == "标题"
    assert evidence["title_region"]["custom"]["bbox"] == {"preserve": True}
    assert evidence["bbox"] == {"business_field": "保留"}
    assert "coordinate_system" not in evidence and "extraction_method" not in evidence
    assert "printed_page" not in evidence and evidence["legacy_text_region"] == "旧版完整文本"
    assert evidence["surrounding_text"] == "邻近披露原文" and evidence["expected_text"] == "应保留字段"
    assert projected["claim_evidence_pool"][1] == original["claim_evidence_pool"][1]
    assert "bbox" not in projected["comparability"]["evidence"][0]
    assert projected["comparability"]["evidence"][0]["metadata"]["bbox"] == "business"
    assert "bbox" not in projected["narrative_evidences"][0]
    assert "bbox" not in projected["investigation_items"][0]["citations"][0]
    assert "bbox" not in projected["issue_hierarchy"]["candidate_pool"][0]["citations"][0]
    assert "bbox" not in projected["issue_hierarchy"]["supplement_pool"][0]
    assert projected["issue_hierarchy"]["business_metadata"]["bbox"] == "不在来源证据白名单路径"
    claim_row = projected["claim_verification_pool"][0]
    independent_row = projected["independent_verification_pool"][0]
    assert claim_row["reference_id"] == "cv:0"
    assert independent_row["reference_id"] == "iv:0"
    assert claim_row["record"]["verification_id"] == "ver:0"
    assert independent_row["record"]["verification_id"] == "ver:1"
    assert "verified_at" not in claim_row["record"]
    assert projected["verified_claims"][0]["verification_refs"] == ["cv:0"]
    assert projected["verified_claims"][0]["independent_verification_ref"] == "iv:0"
    assert projected["verified_facts"] == original["verified_facts"]
    envelope = annual_review.encode_annual_review_v3_wire(projected)
    assert annual_review.decode_annual_review_v3_wire(envelope) == projected


def test_review_v3_compact_v4_uses_numeric_repeat_tag_and_rejects_boolean_tags() -> None:
    repeated = {"long_field_name": "同一完整业务记录" * 8, "amount": 123}
    envelope = annual_review.encode_annual_review_v3_wire({"items": [copy.deepcopy(repeated) for _ in range(6)]})
    items_node = _wire_object(envelope["data"], envelope["key_pool"])["items"]
    assert items_node[0] == 3
    assert annual_review.decode_annual_review_v3_wire(envelope) == {"items": [repeated for _ in range(6)]}

    boolean_tag = _wire_envelope([True])
    with pytest.raises(ValueError, match="annual_review_v3_wire_invalid"):
        annual_review.decode_annual_review_v3_wire(boolean_tag)


def test_review_v3_request_helper_projects_encodes_and_measures_the_official_chat_body() -> None:
    report = _report(candidate=False)
    identity = annual_review._report_identity(report)
    discovery = {
        "kind": "fintrace_annual_discovery", **identity, "status": "completed",
        "coverage": {"complete": True, "total_pages": 11, "unread_pages": []},
        "findings": [], "evidences": [],
    }
    pipeline = checkpoint_issue_pipeline({**identity, "report_period": "2024年度"}, discovery)
    investigation = {
        **identity, "status": "completed", "items": [], "narrative_evidences": [],
        "discovery": discovery, "model_issue_pipeline": pipeline,
    }
    prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_review_v3.md").read_text(encoding="utf-8")
    full_input, allowed_ids, _refs, _no_priority = annual_review._build_review_input_v3(
        report, investigation, identity, request_model="fixed-dummy-model", system_prompt=prompt,
    )
    allowed_before = set(allowed_ids)
    request = annual_review.build_annual_review_v3_request(
        full_input, model="fixed-dummy-model", system_prompt=prompt,
    )

    assert set(allowed_ids) == allowed_before
    assert set(request["envelope"]) == {"schema_version", "key_pool", "string_pool", "object_pool", "data"}
    assert request["review_wire_version"] == annual_review.V3_WIRE_SCHEMA
    assert request["review_input_projection_version"] == annual_review.V3_INPUT_PROJECTION_VERSION
    assert annual_review.decode_annual_review_v3_wire(request["envelope"]) == request["projected_input"]
    assert request["request_chars"] == len(request["body"].decode("utf-8"))
    assert request["body"] == serialize_chat_body("fixed-dummy-model", request["messages"])
    assert request["request_chars"] <= annual_review.V3_MAX_REQUEST_CHARS
    assert request["semantic_input_sha256"] == hashlib.sha256(
        json.dumps(full_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def test_review_v3_prompt_describes_the_current_projection_wire_contract() -> None:
    prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_review_v3.md").read_text(encoding="utf-8")

    assert annual_review.V3_WIRE_SCHEMA in prompt
    assert "key_pool" in prompt and "string_pool" in prompt and "object_pool" in prompt
    assert "`[0, key_index, encoded_value, ...]`" in prompt
    assert "`[6]`" in prompt
    assert "annual_review_v3_purpose_projection_v1" in prompt
    assert "text_pool" not in prompt
    assert "U+E000" not in prompt
    assert "显式 `null` 原样保留" in prompt
    assert "可能解释必须标为假设。" in prompt


SOURCE_SHA = "a" * 64
RUN_ID = "annual-analysis-review-test"
SOURCE_ID = "doc-review"
FACT_1 = "fact:revenue:2024"
FACT_2 = "fact:revenue:2023"
CALC_ID = "calc:annual_difference:revenue"


def _fact(fact_id: str, year: int, page: int, value: str) -> dict[str, Any]:
    evidence_id = f"verified-evidence-{year}"
    return {
        "fact_id": fact_id,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "metric_id": "revenue",
        "label_raw": "营业收入",
        "normalized_value": value,
        "unit": "元",
        "currency": "人民币",
        "period_start": f"{year}-01-01",
        "period_end": f"{year}-12-31",
        "period_type": "duration",
        "comparison_role": "current" if year == 2024 else "comparative",
        "statement_type": "income_statement",
        "scope": "consolidated",
        "verifications": [{
            "verification_id": f"verification-{year}",
            "target_type": "financial_fact",
            "target_id": fact_id,
            "status": "verified",
            "evidence_ids": [evidence_id],
        }],
        "evidences": [{
            "evidence_id": f"extraction-evidence-{year}",
            "document_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA,
            "pdf_page": 99,
        }],
        "verification_evidences": [{
            "evidence_id": evidence_id,
            "document_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA,
            "pdf_page": page,
        }],
    }


def _report(*, candidate: bool = True, pending: bool = False, valid_facts: bool = True) -> dict[str, Any]:
    metrics = [_fact(FACT_1, 2024, 10, "100"), _fact(FACT_2, 2023, 11, "80")] if valid_facts else []
    analyses = [{
        "calculation_id": CALC_ID,
        "formula_id": "annual_difference",
        "formula_expression": "current - comparative",
        "input_fact_ids": [FACT_1, FACT_2],
        "output_value": "20",
        "unit": "元",
        "status": "succeeded",
        "independent_verification": {
            "status": "verified",
            "calculation_id": CALC_ID,
            "recomputed_value": "20",
        },
    }] if valid_facts else []
    return {
        "kind": "fintrace_annual_analysis_report",
        "run_id": RUN_ID,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "confirmed": {"metrics": metrics, "analyses": analyses},
        "comparability": {
            "document_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA,
            "report_year": 2024,
            "status": "verified",
        },
        "scope": {"gaps": []},
        "pending_review": {
            "facts": [{"fact_id": "fact:missing:2024", "placement_reasons": ["币种未核实。"]}] if pending else [],
            "calculations": [],
            "claims": [],
            "candidate_signals": ([{
                "signal_id": "revenue_up_cash_down",
                "status": "candidate",
                "calculation_ids": [CALC_ID],
                "left_difference": "20",
                "right_difference": "-3",
            }] if candidate else []),
            "uncited_evidences": [],
        },
        "limitations": ["仅覆盖本年报中的有限指标。"],
    }


def _investigation(report: dict[str, Any], *, status: str | None = None, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "run_id": report["run_id"],
        "company_id": report["company_id"],
        "report_year": report["report_year"],
        "source_document_id": report["source_document_id"],
        "source_sha256": report["source_sha256"],
        "status": status or ("completed" if report["pending_review"]["candidate_signals"] else "abstained"),
        "reason": "no_supported_candidate_signal" if not report["pending_review"]["candidate_signals"] else None,
        "items": items or [],
    }


class _MockServer:
    def __init__(self, replies: list[tuple[int, Any]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies = replies
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(size).decode("utf-8"))
                owner.requests.append(request)
                status, answer = owner.replies[len(owner.requests) - 1]
                if callable(answer):
                    answer = answer(request)
                if status == 200:
                    response = {
                        "id": "mock-review-response",
                        "model": "local-mock-model",
                        "choices": [{"message": {"role": "assistant", "content": answer}}],
                    }
                else:
                    response = {"error": {"message": "local mock failure"}}
                raw = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> _MockServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _response(
    *,
    assessment: str = "prioritize_review",
    reference: str = CALC_ID,
    summary: str = "建议优先复核相关计算及其对应披露。",
) -> str:
    return json.dumps({
        "assessment": assessment,
        "summary": summary,
        "reasons": [{"text": "该项需要结合对应原文进一步核对。", "evidence_ids": [reference]}],
        "follow_up_items": [{"object": reference, "action": "回到对应披露核对形成原因。", "evidence_ids": [reference]}],
        "limitations": ["该评审是模型观点，仍需人工复核。"],
    }, ensure_ascii=False)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    report: dict[str, Any],
    investigation: dict[str, Any],
    server: _MockServer,
) -> tuple[dict[str, Any], Path]:
    artifacts_root = tmp_path / "artifacts"
    runs_root = artifacts_root / "runs"
    run_dir = runs_root / RUN_ID
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(annual_review, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(audited_chat, "repository_runs_root", lambda: runs_root)
    settings = ModelSettings(server.base_url, "mock-secret", "local-mock-model", 3)
    result = annual_review.run_annual_model_review(
        report,
        investigation,
        settings,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )
    return result, run_dir


def test_final_review_uses_one_audited_call_and_current_source_refs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _report()
    investigation = _investigation(report, items=[{
        "signal_id": "revenue_up_cash_down",
        "status": "interpretation",
        "explanation": "该背离原因无法由现有片段确认。",
        "limitations": ["模型解释尚未核实。"],
        "narrative_evidence_ids": ["nctx:report:17"],
    }])
    with _MockServer([(200, _response(reference="investigation:revenue_up_cash_down"))]) as server:
        review, run_dir = _run(monkeypatch, tmp_path, report, investigation, server)

    assert review["status"] == "completed"
    assert review["assessment"] == "prioritize_review"
    assert review["model_called"] is True
    assert review["call_count"] == review["call_attempt_count"] == 1
    assert len(review["audit_artifacts"]) == 1
    assert review["source_identity"]["source_sha256"] == SOURCE_SHA
    assert len(server.requests) == 1
    request = server.requests[0]
    body = json.loads(request["messages"][1]["content"])
    assert body["candidate_signals"][0]["status"] == "candidate"
    assert body["investigation_items"][0]["classification"] == "unverified_model_material"
    assert body["investigation_items"][0]["review_id"] == "investigation:revenue_up_cash_down"
    assert "narrative_evidence_ids" not in body["investigation_items"][0]
    assert "nctx:report:17" not in request["messages"][1]["content"]
    assert body["coverage"]["candidate_count"] == 1
    audit = run_dir / Path(review["audit_artifacts"][0]["audit_dir"]).name
    assert (audit / "request.json").is_file()
    assert (audit / "response.json").is_file()
    request_audit = json.loads((audit / "request.json").read_text(encoding="utf-8"))
    assert request_audit["evidence_refs"] == [
        {"document_id": SOURCE_ID, "page": 10},
        {"document_id": SOURCE_ID, "page": 11},
    ]
    assert "mock-secret" not in json.dumps(request_audit)


def test_review_v3_preserves_fixed_narrative_evidence_separate_from_discovery_ids() -> None:
    report = _report()
    identity = annual_review._report_identity(report)
    finding_id = f"disc:{SOURCE_SHA[:12]}-00000000000000000001"
    finding_evidence_id = "adisc-evidence-1"
    narrative_id = "nctx:report:17"
    citation = "经营活动现金流净额"
    bbox = {"x0": 1, "y0": 2, "x1": 3, "y1": 4}
    discovery = {
        "kind": "fintrace_annual_discovery", **identity, "status": "completed",
        "coverage": {"complete": True, "total_pages": 20, "unread_pages": []},
        "findings": [{
            "finding_id": finding_id, "title": "收入与现金流背离",
            "observation": "收入增长而经营现金流下降。", "why_it_matters": "需核对期间与结算。",
            "follow_up": "核查应收款项附注。", "priority": "high",
            "classification": "unverified_model_finding", "verification_status": "unverified",
            "evidence_ids": [finding_evidence_id],
            "citations": [{"evidence_id": finding_evidence_id, "quote": citation}], "batch_index": 1,
        }],
        "evidences": [{
            "evidence_id": finding_evidence_id, "source_document_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA, "pdf_page": 18, "block_index": 2,
            "start_char": 0, "bbox": bbox, "raw_text": citation, "finding_ids": [finding_id],
        }],
    }
    pipeline = checkpoint_issue_pipeline({**identity, "report_period": "2024年度"}, discovery)
    investigation = {
        **identity, "status": "completed", "model_name": "local-test-model",
        "items": [{
            "signal_id": "revenue_up_cash_down", "status": "interpretation",
            "explanation": "背离原因仍需外部资料核验。", "reason": None,
            "limitations": ["不代表舞弊"], "narrative_evidence_ids": [narrative_id],
        }],
        "narrative_evidences": [{
            "evidence_id": narrative_id, "source_document_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA, "signal_ids": ["revenue_up_cash_down"],
            "section_label": "管理层讨论", "pdf_page": 17,
            "bbox": {"x0": 10, "y0": 20, "x1": 100, "y1": 40},
            "raw_text": "公司称经营活动现金流受到季节因素影响。",
            "selection_reason": "与候选信号相关的原文片段",
        }],
        "discovery": discovery, "model_issue_pipeline": pipeline,
    }
    prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_review_v3.md").read_text(encoding="utf-8")

    payload, allowed_ids, evidence_refs, _ = annual_review._build_review_input_v3(
        report, investigation, identity, request_model="local-test-model", system_prompt=prompt,
    )

    assert narrative_id in allowed_ids
    assert finding_id in allowed_ids
    assert narrative_id != finding_id
    assert payload["investigation_items"][0]["citations"][0]["raw_text"] == "公司称经营活动现金流受到季节因素影响。"
    assert payload["investigation_items"][0]["narrative_evidence_ids"] == [narrative_id]
    assert {item.page for item in evidence_refs} == {10, 11, 17, 18}


@pytest.mark.parametrize("text", ["重点核查应收账款。", "进一步核查结算时点。", "请关注附注解释。"])
def test_v3_numeric_guard_allows_ordinary_chinese_review_language(text: str) -> None:
    assert annual_review._contains_new_numeric_claim(text) is False


@pytest.mark.parametrize("text", ["收入增加了3.5亿元。", "比例为百分之三十。", "第十二项仍未核实。", "金额１２万元。"])
def test_v3_numeric_guard_rejects_added_numeric_claims(text: str) -> None:
    assert annual_review._contains_new_numeric_claim(text) is True


def test_review_v3_prompt_and_parser_allow_issue_id_as_follow_up_object() -> None:
    prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_review_v3.md").read_text(encoding="utf-8")
    assert "`follow_up_items.object`" in prompt
    assert "`reference_id`" in prompt
    pipeline = {"global_closure": {"closed": True, "pending_issue_ids": [], "relation_gaps": [], "coverage_gaps": []}}
    allowed = {"issue:receivables", "evidence:receivables"}
    payload = {
        "assessment": "prioritize_review",
        "summary": "该事项值得继续核查。",
        "reasons": [{"text": "现有材料指向待解释差异。", "evidence_ids": ["evidence:receivables"]}],
        "follow_up_items": [{
            "object": "issue:receivables", "action": "核对相关附注与结算时点。",
            "evidence_ids": ["issue:receivables"],
        }],
        "limitations": [],
    }
    parsed = annual_review.parse_model_review_v3(
        json.dumps(payload, ensure_ascii=False), allowed_ids=allowed,
        no_priority_allowed=False, pipeline=pipeline, limitations_required=[],
    )
    assert parsed["follow_up_items"][0]["object"] == "issue:receivables"

    for references in (["issue:receivables"] * 2, [f"issue:{index}" for index in range(9)]):
        invalid = copy.deepcopy(payload)
        invalid["follow_up_items"][0]["evidence_ids"] = references
        with pytest.raises(ValueError, match="model_review_response_invalid"):
            annual_review.parse_model_review_v3(
                json.dumps(invalid, ensure_ascii=False), allowed_ids=allowed,
                no_priority_allowed=False, pipeline=pipeline, limitations_required=[],
            )


def test_supplement_prompt_does_not_offer_support_as_a_retrieval_role() -> None:
    prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_issue_supplement_v1.md").read_text(encoding="utf-8")
    assert "primary_finding_quote 是原发现自带并固定保留的引文" in prompt
    assert "不参与角色赋值或独立核验" in prompt
    assert "不得给检索片段赋 support" in prompt


def test_review_v3_deduplicates_repeated_claim_evidence_into_a_lossless_shared_pool() -> None:
    report = _report(candidate=False)
    long_fact_value = "value-token-" * 12
    long_calc_value = "calculated-token-" * 8
    long_formula = "complete-formula-expression-" * 9
    report["confirmed"]["metrics"][0]["normalized_value"] = long_fact_value
    report["confirmed"]["analyses"][0]["output_value"] = long_calc_value
    report["confirmed"]["analyses"][0]["formula_expression"] = long_formula
    report["confirmed"]["analyses"][0]["independent_verification"]["recomputed_value"] = long_calc_value
    shared_evidence_id = "verified-evidence-2024"
    shared_text = "已核验财务表格证据。" * 500

    def claim(claim_id: str) -> dict[str, Any]:
        return {
            "claim_id": claim_id, "claim_type": "fact", "text": "营业收入规范值已核实。",
            "placement": "verified_claims", "verification_status": "verified",
            "independent_verification": {"status": "verified", "reason": None, "expected_text": "营业收入规范值已核实。"},
            "supporting_fact_ids": [FACT_1], "calculation_ids": [],
            "supporting_evidence_ids": [shared_evidence_id],
            "evidences": [{
                "evidence_id": shared_evidence_id, "document_id": SOURCE_ID,
                "source_sha256": SOURCE_SHA, "pdf_page": 10, "raw_text": shared_text,
                "table_id": "table:revenue", "bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4},
            }],
            "verifications": [{
                "verification_id": f"verify:{claim_id}", "status": "verified",
                "target_type": "claim", "target_id": claim_id,
                "evidence_ids": [shared_evidence_id], "reason": None,
            }],
            "placement_reasons": [], "limitations": [], "alternative_explanations": [],
            "follow_up_items": [],
        }

    report["verified_claims"] = [claim("claim:one"), claim("claim:two")]
    identity = annual_review._report_identity(report)
    discovery = {
        "kind": "fintrace_annual_discovery", **identity, "status": "completed",
        "coverage": {"complete": True, "total_pages": 20, "unread_pages": []},
        "findings": [], "evidences": [],
    }
    pipeline = checkpoint_issue_pipeline({**identity, "report_period": "2024年度"}, discovery)
    investigation = {**identity, "status": "completed", "items": [], "narrative_evidences": [], "discovery": discovery, "model_issue_pipeline": pipeline}
    prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_review_v3.md").read_text(encoding="utf-8")

    payload, _allowed, _refs, _no_priority = annual_review._build_review_input_v3(
        report, investigation, identity, request_model="local-test-model", system_prompt=prompt,
    )

    assert len(payload["verified_claims"]) == 2
    assert payload["verified_facts"][0]["value"] == long_fact_value
    assert payload["verified_calculations"][0]["value"] == long_calc_value
    assert payload["verified_calculations"][0]["formula"] == long_formula
    assert len(payload["claim_evidence_pool"]) == 1
    assert payload["claim_evidence_pool"][0]["raw_text"] == shared_text
    assert payload["verified_claims"][0]["evidence_refs"] == [shared_evidence_id]
    assert payload["verified_claims"][1]["evidence_refs"] == [shared_evidence_id]
    assert all("evidences" not in item and "verifications" not in item for item in payload["verified_claims"])


def test_no_candidate_can_receive_scoped_no_priority_only_with_complete_verified_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _report(candidate=False)
    investigation = _investigation(report)
    investigation["discovery"] = {
        "kind": "fintrace_annual_discovery",
        "run_id": RUN_ID,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "status": "completed",
        "coverage": {
            "complete": True,
            "truncated": False,
            "planned_batches": 1,
            "processed_batches": 1,
            "successful_batches": 1,
            "failed_batches": 0,
            "total_pages": 1,
            "text_pages": 1,
            "total_blocks": 1,
            "total_chars": 50,
            "processed_chars": 50,
            "unread_pages": [],
        },
        "findings": [],
        "evidences": [],
    }
    answer = _response(
        assessment="no_priority_issue_identified_within_scope",
        reference=FACT_1,
        summary="当前已核验覆盖范围内暂未发现需优先核查事项。",
    )
    with _MockServer([(200, answer)]) as server:
        review, _ = _run(monkeypatch, tmp_path, report, investigation, server)

    assert review["status"] == "completed"
    assert review["assessment"] == "no_priority_issue_identified_within_scope"
    review_context = json.loads(server.requests[0]["messages"][1]["content"])
    assert review_context["coverage"]["no_priority_eligible"] is True
    assert review_context["coverage"]["discovery_coverage"]["partial_batches"] == 0


def test_review_context_preserves_partial_discovery_coverage_and_blocks_no_priority() -> None:
    report = _report(candidate=False)
    investigation = _investigation(report)
    investigation["discovery"] = {
        "kind": "fintrace_annual_discovery",
        "run_id": RUN_ID,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "status": "partial",
        "coverage": {
            "complete": False,
            "truncated": False,
            "planned_batches": 1,
            "processed_batches": 1,
            "successful_batches": 0,
            "partial_batches": 1,
            "failed_batches": 0,
            "processed_chars": 0,
            "unread_pages": [1],
        },
        "findings": [],
        "evidences": [],
    }

    data, _, _, no_priority = annual_review._build_review_input(
        report,
        investigation,
        annual_review._report_identity(report),
        request_model="review-test-model",
        system_prompt="review test prompt",
    )

    assert data["coverage"]["discovery_coverage"]["partial_batches"] == 1
    assert data["coverage"]["discovery_coverage_complete"] is False
    assert data["coverage"]["no_priority_eligible"] is False
    assert no_priority is False


def test_discovery_review_budget_counts_actual_payload_and_preserves_high_priority_tail() -> None:
    report = _report(candidate=False, valid_facts=False)
    investigation = _investigation(report)
    source_identity = {
        "run_id": RUN_ID,
        "company_id": report["company_id"],
        "report_year": report["report_year"],
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
    }
    findings = []
    evidences = []
    for index in range(1, 14):
        evidence_id = f"adisc-{SOURCE_SHA[:12]}-p{index}-b0-c0-{index:012x}"
        finding_id = f"disc:{SOURCE_SHA[:12]}-{index:020x}"
        priority = "high" if index == 13 else "low"
        findings.append({
            "finding_id": finding_id,
            "title": "动态核查主题",
            "observation": "该事项的年报描述需要结合相关业务记录进一步复核。" * 2,
            "why_it_matters": "相关披露可能影响对业务安排和风险敞口的理解。" * 2,
            "follow_up": "建议核对披露对应的合同及期后执行情况。" * 2,
            "priority": priority,
            "classification": "unverified_model_finding",
            "verification_status": "unverified",
            "evidence_ids": [evidence_id],
            "citations": [{"evidence_id": evidence_id, "quote": "年报原文引用片段"}],
        })
        evidences.append({
            "evidence_id": evidence_id,
            "source_document_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA,
            "pdf_page": index,
            "block_index": 0,
            "start_char": 0,
            "bbox": {"x0": 10.0, "y0": 10.0, "x1": 100.0, "y1": 25.0},
            "raw_text": "年报原文引用片段",
        })
    original_findings = list(findings)
    investigation["discovery"] = {
        "kind": "fintrace_annual_discovery",
        **source_identity,
        "status": "completed",
        "coverage": {"complete": True, "truncated": False, "total_pages": 13, "text_pages": 13},
        "findings": findings,
        "evidences": evidences,
    }

    model = "long-test-model-" + "x" * 1100
    system_prompt = "系统说明" * 900
    data, allowed, evidence_refs, no_priority = annual_review._build_review_input(
        report,
        investigation,
        annual_review._report_identity(report),
        request_model=model,
        system_prompt=system_prompt,
    )
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    assert len(investigation["discovery"]["findings"]) == len(original_findings)
    assert investigation["discovery"]["findings"] == original_findings
    assert len(data["discovery_items"]) == 12
    assert data["discovery_items"][0]["priority"] == "high"
    assert data["coverage"]["discovery_omitted_count"] == 1
    assert data["coverage"]["discovery_sent_finding_ids"] == [
        item["finding_id"] for item in data["discovery_items"]
    ]
    assert data["coverage"]["no_priority_eligible"] is False
    assert not no_priority
    actual_chars = annual_review._actual_request_chars(model, system_prompt, serialized)
    assert actual_chars <= annual_review.MAX_REQUEST_CHARS
    assert 12_000 <= actual_chars
    expected_pages = {
        page for item in data["discovery_items"] for page in item["citation_pages"]
    }
    assert {item.page for item in evidence_refs} == expected_pages
    assert data["discovery_items"][0]["finding_id"] in allowed
    assert data["discovery_items"][0]["evidence_ids"][0] not in allowed
    with pytest.raises(ValueError, match="model_review_response_invalid"):
        annual_review.parse_model_review(
            _response(reference=data["discovery_items"][0]["evidence_ids"][0]),
            allowed_ids=allowed,
            no_priority_allowed=False,
        )


@pytest.mark.parametrize(
    ("report", "investigation", "reference"),
    [
        (
            _report(candidate=False, pending=True),
            _investigation(_report(candidate=False, pending=True)),
            FACT_1,
        ),
        (
            _report(candidate=False, valid_facts=False, pending=True),
            _investigation(_report(candidate=False, valid_facts=False, pending=True)),
            "pending_fact:fact:missing:2024",
        ),
    ],
)
def test_no_priority_is_rejected_when_data_is_open_or_unverified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    report: dict[str, Any],
    investigation: dict[str, Any],
    reference: str,
) -> None:
    with _MockServer([(200, _response(
        assessment="no_priority_issue_identified_within_scope",
        reference=reference,
        summary="当前范围内暂未发现需优先核查事项。",
    ))]) as server:
        review, _ = _run(monkeypatch, tmp_path, report, investigation, server)

    assert review["status"] == "failed"
    assert review["reason"] == "model_review_no_priority_not_supported"
    assert review["assessment"] is None


def test_no_candidate_with_no_verified_evidence_can_return_insufficient_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _report(candidate=False, valid_facts=False)
    answer = json.dumps({
        "assessment": "insufficient_evidence",
        "summary": "现有材料不足以形成评审判断。",
        "reasons": [{"text": "没有可用的已核验对象。", "evidence_ids": []}],
        "follow_up_items": [],
        "limitations": ["本次评审没有可引用的已核验证据。"],
    }, ensure_ascii=False)
    with _MockServer([(200, answer)]) as server:
        review, _ = _run(monkeypatch, tmp_path, report, _investigation(report), server)

    assert review["status"] == "completed"
    assert review["assessment"] == "insufficient_evidence"
    assert review["model_called"] is True
    assert review["reasons"][0]["evidence_ids"] == []


def test_review_accepts_eight_distinct_supported_refs_per_item() -> None:
    references = [f"fact:ref:{index}" for index in range(1, 9)]
    answer = json.dumps({
        "assessment": "prioritize_review",
        "summary": "建议复核该候选线索。",
        "reasons": [{"text": "候选线索需要回到披露进一步核对。", "evidence_ids": references}],
        "follow_up_items": [{
            "object": references[0],
            "action": "核对该候选线索对应的披露。",
            "evidence_ids": references,
        }],
        "limitations": ["现有材料仍需人工复核。"],
    }, ensure_ascii=False)

    parsed = annual_review.parse_model_review(
        answer,
        allowed_ids=set(references),
        no_priority_allowed=False,
    )

    assert parsed["reasons"][0]["evidence_ids"] == references
    assert parsed["follow_up_items"][0]["evidence_ids"] == references


@pytest.mark.parametrize(
    "references",
    [
        [f"fact:ref:{index}" for index in range(1, 10)],
        ["fact:ref:1", "fact:ref:1"],
    ],
    ids=["nine-distinct-refs", "duplicate-ref"],
)
def test_review_rejects_more_than_eight_or_duplicate_refs_per_item(references: list[str]) -> None:
    answer = json.dumps({
        "assessment": "prioritize_review",
        "summary": "建议复核该候选线索。",
        "reasons": [{"text": "候选线索需要回到披露进一步核对。", "evidence_ids": references}],
        "follow_up_items": [{
            "object": references[0],
            "action": "核对该候选线索对应的披露。",
            "evidence_ids": references,
        }],
        "limitations": ["现有材料仍需人工复核。"],
    }, ensure_ascii=False)

    with pytest.raises(ValueError, match="^model_review_response_invalid$"):
        annual_review.parse_model_review(
            answer,
            allowed_ids=set(references),
            no_priority_allowed=False,
        )


@pytest.mark.parametrize(
    ("answer", "expected_reason"),
    [
        (_response(reference="unknown:forged"), "model_review_response_invalid"),
        (_response(summary="营业收入增长了25%。"), "model_review_numeric_claim_rejected"),
        ("not json", "model_review_response_invalid"),
    ],
)
def test_review_rejects_forged_ids_numeric_claims_and_bad_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    answer: str,
    expected_reason: str,
) -> None:
    report = _report()
    with _MockServer([(200, answer)]) as server:
        review, _ = _run(monkeypatch, tmp_path, report, _investigation(report), server)

    assert review["status"] == "failed"
    assert review["reason"] == expected_reason
    assert review["model_called"] is True
    assert review["call_count"] == review["call_attempt_count"] == 1


def test_review_failure_is_audited_and_contains_no_model_opinion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _report()
    with _MockServer([(503, "ignored"), (503, "ignored"), (503, "ignored")]) as server:
        review, _ = _run(monkeypatch, tmp_path, report, _investigation(report), server)

    assert len(server.requests) == 3
    assert review["status"] == "failed"
    assert review["reason"] == "model_review_call_failed"
    assert review["model_called"] is True
    assert review["call_count"] == 0
    assert review["call_attempt_count"] == 3
    assert review["summary"] is None
    assert review["audit_artifacts"][0]["status"] == "failed"


@pytest.mark.parametrize("wrong_artifacts_root", [False, True])
def test_review_rejects_run_binding_mismatch_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong_artifacts_root: bool
) -> None:
    runs_root = tmp_path / "artifacts" / "runs"
    run_dir = runs_root / "another-run"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(annual_review, "repository_runs_root", lambda: runs_root)
    report = _report()
    monkeypatch.setattr(
        annual_review,
        "audited_complete_chat",
        lambda *_args, **_kwargs: pytest.fail("来源绑定失败时不得调用模型。"),
    )

    if wrong_artifacts_root:
        run_dir = runs_root / RUN_ID
        run_dir.mkdir()
        artifacts_root = tmp_path / "other-artifacts"
        artifacts_root.mkdir()
        expected_reason = "model_review_run_binding_invalid"
    else:
        artifacts_root = runs_root.parent
        expected_reason = "model_review_run_binding_invalid"
    result = annual_review.run_annual_model_review(
        report,
        _investigation(report),
        ModelSettings("https://example.invalid/v1", "mock-secret", "model", 1),
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )

    assert result["status"] == "failed"
    assert result["reason"] == expected_reason
    assert result["call_count"] == 0
