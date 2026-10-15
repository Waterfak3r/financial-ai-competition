from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from finagent.agents import annual_issue_pipeline as issue_pipeline
from finagent.agents.annual_issue_pipeline import (
    build_issue_display_order,
    _apply_group_decisions,
    _build_group_shards,
    _build_global_input,
    _candidate_from_finding,
    _close_single_group_shard_scope,
    _group_input,
    _local_exact_duplicate_groups,
    _source_identity,
    _supplement_input,
    _wire_chars,
    _wire_body,
    _validated_findings,
    _wire_hash,
    checkpoint_issue_pipeline,
    parse_typed_result,
    run_annual_issue_pipeline,
    validate_pipeline,
    ISSUE_WIRE_VERSION,
    MAX_SHARD_REQUEST_CHARS,
)
from finagent.core.annual_work_budget import (
    BUDGET_CONTRACT_VERSION,
    GLOBAL_REQUEST_LIMIT_CHARS,
    MODEL_CALL_TIMEOUT_SECONDS,
    REVIEW_REQUEST_LIMIT_CHARS,
    archived_budget_contract_v2,
    archived_budget_contract_v3,
)
from finagent.agents.annual_review_wire import (
    decode_annual_issue_input_wire,
    encode_annual_issue_input_wire,
    V3_INPUT_PROJECTION_VERSION,
    V3_WIRE_SCHEMA,
)
from finagent.api.annual_report_read import AnnualReportReadError, _validate_issue_shard_audits
from finagent.core.model_settings import ModelSettings
from finagent.core.annual_recovery import _annual_issue_pipeline_has_executable_work
from finagent.llm.chat_completion import ChatMessage, serialize_chat_body
from finagent.retrieval.annual_issue_supplements import retrieve_annual_issue_supplements
from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox, PdfPageText, PdfTextBlock


SOURCE_SHA = "a" * 64
SOURCE_IDENTITY = {
    "run_id": "annual-analysis-test-run",
    "company_id": "600000",
    "report_year": 2025,
    "report_period": "2025年度",
    "source_document_id": "test-annual-pdf",
    "source_sha256": SOURCE_SHA,
}


def _finding(index: int, *, observation: str, quote: str = "年报原文") -> dict:
    finding_id = f"disc:{SOURCE_SHA[:12]}-{index:020x}"
    evidence_id = f"adisc-{SOURCE_SHA[:12]}-p1-b0-c0-{index:08x}"
    return {
        "finding_id": finding_id,
        "title": f"候选 {index}",
        "_prompt_version": "annual_discovery_v3",
        "observation": observation,
        "why_it_matters": "需要核对口径与期间。",
        "follow_up": "检查附注解释和现金流。",
        "priority": "medium",
        "classification": "unverified_model_finding",
        "verification_status": "unverified",
        "evidence_ids": [evidence_id],
        "citations": [{"evidence_id": evidence_id, "quote": quote}],
        "batch_index": 1,
    }, {
        "evidence_id": evidence_id,
        "source_document_id": SOURCE_IDENTITY["source_document_id"],
        "source_sha256": SOURCE_SHA,
        "pdf_page": 1,
        "block_index": 0,
        "start_char": 0,
        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 100.0, "y1": 20.0},
        "raw_text": quote,
        "finding_ids": [finding_id],
    }


def _binding() -> dict:
    discovery = {
        "source_sha256": SOURCE_SHA,
        "company_id": SOURCE_IDENTITY["company_id"],
        "report_year": SOURCE_IDENTITY["report_year"],
        "source_document_id": SOURCE_IDENTITY["source_document_id"],
        "analysis_mode": "model_investigation",
        "provider_model_fingerprint": "provider-model-test",
        "model": "offline-mock-model",
        "plan_version": "annual-discovery-plan-v3",
        "plan_sha256": "b" * 64,
        "m3_binding_sha256": None,
    }
    return {
        "kind": "fintrace_annual_execution_binding_v5",
        "execution_version": "execution_v5",
        **{key: discovery[key] for key in (
            "source_sha256", "company_id", "report_year", "source_document_id",
            "analysis_mode", "provider_model_fingerprint", "model",
        )},
        "discovery_binding": discovery,
        "prompts_sha256": "c" * 64,
        "parser_version": "annual-issue-pipeline-parser-v1-execution-v5",
        "issue_pipeline_version": "annual_issue_pipeline_v1",
        "issue_wire_version": ISSUE_WIRE_VERSION,
        "grouping_prompt_version": "annual_issue_grouping_v1",
        "review_prompt_version": "annual_review_v3",
        "review_wire_version": V3_WIRE_SCHEMA,
        "review_input_projection_version": V3_INPUT_PROJECTION_VERSION,
        "plan_version": discovery["plan_version"],
        "plan_sha256": discovery["plan_sha256"],
        "m3_binding_sha256": discovery["m3_binding_sha256"],
    }


def _binding_v6() -> dict:
    archived = archived_budget_contract_v2(scope="new_run")
    return {
        **_binding(),
        "kind": "fintrace_annual_execution_binding_v6",
        "execution_version": "execution_v6",
        "budget_contract_version": archived["schema_version"],
        "review_request_limit_chars": archived["review_request_limit_chars"],
        "model_call_timeout_seconds": archived["model_call_timeout_seconds"],
    }


def _binding_v7() -> dict:
    archived = archived_budget_contract_v3(scope="new_run")
    return {
        **_binding(),
        "kind": "fintrace_annual_execution_binding_v7",
        "execution_version": "execution_v7",
        "budget_contract_version": archived["schema_version"],
        "review_request_limit_chars": archived["review_request_limit_chars"],
        "model_call_timeout_seconds": archived["model_call_timeout_seconds"],
    }


def _binding_v8() -> dict:
    binding = _binding_v7()
    binding.update(kind="fintrace_annual_execution_binding_v8", execution_version="execution_v8", supplement_retrieval_version="source_anchor_then_ngram_v2")
    binding["discovery_binding"].update(prompt_version="annual_discovery_v4", prompt_sha256="d" * 64)
    return binding


def _binding_v9() -> dict:
    binding = _binding_v8()
    binding.update(
        kind="fintrace_annual_execution_binding_v9", execution_version="execution_v9",
        budget_contract_version=BUDGET_CONTRACT_VERSION,
        global_request_limit_chars=GLOBAL_REQUEST_LIMIT_CHARS,
        review_request_limit_chars=REVIEW_REQUEST_LIMIT_CHARS,
        model_call_timeout_seconds=MODEL_CALL_TIMEOUT_SECONDS,
    )
    return binding


def _binding_v10() -> dict:
    binding = _binding_v9()
    binding.update(
        kind="fintrace_annual_execution_binding_v10", execution_version="execution_v10",
        grouping_shard_plan_version="member_bounded_v2", grouping_shard_max_members=3,
    )
    return binding



def _pending_pipeline() -> dict:
    finding, evidence = _finding(1, observation="经营性现金流明显低于收入增长。")
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in (
            "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
        )},
        "status": "running",
        "prompt_version": "annual_discovery_v3",
        "coverage": {"complete": False},
        "findings": [finding],
        "evidences": [evidence],
    }
    return checkpoint_issue_pipeline(SOURCE_IDENTITY, discovery, execution_binding=_binding())


@pytest.mark.parametrize(
    "mutation",
    ["missing_wire", "missing_projection", "unknown_wire", "unknown_projection"],
)
def test_execution_v5_binding_requires_the_exact_review_protocol_versions(mutation: str) -> None:
    pipeline = _pending_pipeline()
    binding = pipeline["execution_binding"]
    assert isinstance(binding, dict)
    if mutation == "missing_wire":
        binding.pop("review_wire_version")
    elif mutation == "missing_projection":
        binding.pop("review_input_projection_version")
    elif mutation == "unknown_wire":
        binding["review_wire_version"] = "future_unknown_wire"
    else:
        binding["review_input_projection_version"] = "future_unknown_projection"

    with pytest.raises(ValueError, match="annual_issue_pipeline_binding_invalid"):
        validate_pipeline(pipeline, identity=SOURCE_IDENTITY)


def test_execution_v6_binding_requires_exact_budget_fields_and_keeps_v5_readable() -> None:
    v5_pipeline = _pending_pipeline()
    validate_pipeline(v5_pipeline, identity=SOURCE_IDENTITY)

    finding, evidence = _finding(1, observation="经营性现金流明显低于收入增长。")
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in (
            "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
        )},
        "status": "running",
        "prompt_version": "annual_discovery_v3",
        "coverage": {"complete": False},
        "findings": [finding],
        "evidences": [evidence],
    }
    v6_pipeline = checkpoint_issue_pipeline(SOURCE_IDENTITY, discovery, execution_binding=_binding_v6())
    validate_pipeline(v6_pipeline, identity=SOURCE_IDENTITY)

    missing = copy.deepcopy(v6_pipeline)
    missing["execution_binding"].pop("review_request_limit_chars")
    with pytest.raises(ValueError, match="annual_issue_pipeline_binding_invalid"):
        validate_pipeline(missing, identity=SOURCE_IDENTITY)

    int_timeout = copy.deepcopy(v6_pipeline)
    timeout_value = int_timeout["execution_binding"]["model_call_timeout_seconds"]
    int_timeout["execution_binding"]["model_call_timeout_seconds"] = int(timeout_value)
    with pytest.raises(ValueError, match="annual_issue_pipeline_binding_invalid"):
        validate_pipeline(int_timeout, identity=SOURCE_IDENTITY)

    extra_v5 = copy.deepcopy(v5_pipeline)
    extra_v5["execution_binding"]["budget_contract_version"] = "annual_analysis_budget_v2"
    with pytest.raises(ValueError, match="annual_issue_pipeline_binding_invalid"):
        validate_pipeline(extra_v5, identity=SOURCE_IDENTITY)

def test_activity_issue_pipeline_rejects_historical_execution_binding(tmp_path: Path) -> None:
    finding, evidence = _finding(1, observation="经营性现金流明显低于收入增长。")
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in (
            "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
        )},
        "status": "running",
        "prompt_version": "annual_discovery_v3",
        "coverage": {"complete": False},
        "findings": [finding],
        "evidences": [evidence],
    }
    parsed = ParsedTextPdf(
        document_id=SOURCE_IDENTITY["source_document_id"], source_sha256=SOURCE_SHA,
        source_filename="constructed-empty.pdf", page_count=1, coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point", coordinate_note="constructed fixture", ocr_applied=False,
        extractor="fixture", extractor_version="1", pages=(PdfPageText(
            page_number=1, width=595.0, height=842.0, rotation=0, status="extracted",
            image_block_count=0, limitation=None, blocks=(),
        ),),
    )
    (tmp_path / "runs" / SOURCE_IDENTITY["run_id"]).mkdir(parents=True)
    settings = ModelSettings("http://127.0.0.1", "offline-test-token", "offline-mock-model")
    for binding in (_binding(), _binding_v6(), _binding_v7(), _binding_v8(), _binding_v9()):
        with pytest.raises(ValueError, match="annual_issue_pipeline_binding_invalid"):
            run_annual_issue_pipeline(
                discovery,
                identity=SOURCE_IDENTITY,
                parsed_pdf=parsed,
                settings=settings,
                run_dir=tmp_path / "runs" / SOURCE_IDENTITY["run_id"],
                artifacts_root=tmp_path / "artifacts",
                execution_binding=binding,
            )


@pytest.mark.parametrize("tamper", [None, "retrieval", "missing_retrieval", "prompt", "missing_prompt_hash", "input", "origin", "coverage", "budget"])
def test_v8_consumer_rejects_old_or_missing_semantic_binding(tamper: str | None) -> None:
    finding, evidence = _finding(1, observation="经营性现金流与收入的期间及口径待核对。")
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256")},
        "status": "running", "prompt_version": "annual_discovery_v4",
        "coverage": {"complete": False}, "findings": [finding], "evidences": [evidence],
    }
    pipeline = checkpoint_issue_pipeline(SOURCE_IDENTITY, discovery, execution_binding=_binding_v8())
    binding = pipeline["execution_binding"]
    if tamper == "retrieval":
        binding["supplement_retrieval_version"] = "section_filter_then_ngram_overlap_v1"
    elif tamper == "missing_retrieval":
        binding.pop("supplement_retrieval_version")
    elif tamper == "prompt":
        binding["discovery_binding"]["prompt_version"] = "annual_discovery_v3"
    elif tamper == "missing_prompt_hash":
        binding["discovery_binding"].pop("prompt_sha256")
    elif tamper == "input":
        pipeline["input"]["discovery_prompt_version"] = "annual_discovery_v3"
    elif tamper == "origin":
        pipeline["candidates"][0]["origin"] = "annual_discovery_v3"
    elif tamper == "coverage":
        pipeline["retrieval_coverage"]["selection_algorithm_version"] = "section_filter_then_ngram_overlap_v1"
    elif tamper == "budget":
        binding["budget_contract_version"] = "annual_analysis_budget_v2"
    if tamper is None:
        validate_pipeline(pipeline, identity=SOURCE_IDENTITY)
    else:
        with pytest.raises(ValueError, match="annual_issue_pipeline_"):
            validate_pipeline(pipeline, identity=SOURCE_IDENTITY)


def test_v6_archive_binding_does_not_follow_live_budget_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    finding, evidence = _finding(1, observation="经营性现金流明显低于收入增长。")
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in (
            "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
        )},
        "status": "running",
        "prompt_version": "annual_discovery_v3",
        "coverage": {"complete": False},
        "findings": [finding],
        "evidences": [evidence],
    }
    v6_pipeline = checkpoint_issue_pipeline(SOURCE_IDENTITY, discovery, execution_binding=_binding_v6())
    v7_pipeline = checkpoint_issue_pipeline(SOURCE_IDENTITY, discovery, execution_binding=_binding_v7())
    validate_pipeline(v6_pipeline, identity=SOURCE_IDENTITY)
    validate_pipeline(v7_pipeline, identity=SOURCE_IDENTITY)

    monkeypatch.setattr(issue_pipeline, "BUDGET_CONTRACT_VERSION", "annual_analysis_budget_v9")
    monkeypatch.setattr(issue_pipeline, "MODEL_CALL_TIMEOUT_SECONDS", 1.0)
    validate_pipeline(v6_pipeline, identity=SOURCE_IDENTITY)
    validate_pipeline(v7_pipeline, identity=SOURCE_IDENTITY)





def test_local_duplicate_requires_all_observation_fields_and_same_locator() -> None:
    first, _ = _finding(1, observation="同一事项的完整观察", quote="原文片段")
    duplicate, _ = _finding(2, observation="同一事项的完整观察", quote="原文片段")
    shared_citation_only, _ = _finding(3, observation="另一个章节的不同事项", quote="原文片段")
    candidates = [
        {"finding_id": first["finding_id"], "observation": first["observation"], "why_it_matters": first["why_it_matters"], "follow_up": first["follow_up"], "citations": [{"source_document_id": "doc", "source_sha256": SOURCE_SHA, "pdf_page": 1, "block_index": 0, "start_char": 1, "quote": "原文片段"}]},
        {"finding_id": duplicate["finding_id"], "observation": duplicate["observation"], "why_it_matters": duplicate["why_it_matters"], "follow_up": duplicate["follow_up"], "citations": [{"source_document_id": "doc", "source_sha256": SOURCE_SHA, "pdf_page": 1, "block_index": 0, "start_char": 1, "quote": "原文片段"}]},
        {"finding_id": shared_citation_only["finding_id"], "observation": shared_citation_only["observation"], "why_it_matters": shared_citation_only["why_it_matters"], "follow_up": shared_citation_only["follow_up"], "citations": [{"source_document_id": "doc", "source_sha256": SOURCE_SHA, "pdf_page": 1, "block_index": 0, "start_char": 1, "quote": "原文片段"}]},
    ]
    groups = _local_exact_duplicate_groups(candidates)
    assert len(groups) == 2
    assert sorted(len(group["member_finding_ids"]) for group in groups) == [1, 2]


@pytest.mark.parametrize("mutate", ["foreign", "duplicate", "missing"])
def test_grouping_result_requires_exactly_one_decision_per_input_member(mutate: str) -> None:
    ids = [f"disc:{SOURCE_SHA[:12]}-{index:020x}" for index in (1, 2)]
    rows = [
        {"finding_id": ids[0], "disposition": "retain", "target_finding_id": None, "reason_code": "distinct_issue"},
        {"finding_id": ids[1], "disposition": "retain", "target_finding_id": None, "reason_code": "distinct_issue"},
    ]
    if mutate == "foreign":
        rows[1]["finding_id"] = f"disc:{SOURCE_SHA[:12]}-00000000000000000003"
    elif mutate == "duplicate":
        rows[1]["finding_id"] = ids[0]
    else:
        rows.pop()
    with pytest.raises(ValueError, match="typed_response_invalid"):
        parse_typed_result(
            json.dumps({"schema_version": "annual_issue_group_shard_result_v1", "decisions": rows}),
            kind="grouping",
            input_value={"input_member_finding_ids": ids},
        )


def test_supplement_roles_are_typed_per_exact_issue_evidence_pair() -> None:
    input_value = {"evidence_assignments": [{"issue_id": "issue:a", "evidence_id": "evidence:a"}]}
    accepted = parse_typed_result(json.dumps({
        "schema_version": "annual_issue_supplement_result_v1",
        "roles": [{"issue_id": "issue:a", "evidence_id": "evidence:a", "role": "possible_explanation", "reason_code": "plausible_alternative"}],
    }), kind="supplement", input_value=input_value)
    assert accepted["roles"][0]["role"] == "possible_explanation"
    with pytest.raises(ValueError, match="typed_response_invalid"):
        parse_typed_result(json.dumps({
            "schema_version": "annual_issue_supplement_result_v1",
            "roles": [{"issue_id": "issue:a", "evidence_id": "evidence:foreign", "role": "support", "reason_code": "directly_relevant"}],
        }), kind="supplement", input_value=input_value)


def test_supplement_input_separates_fixed_finding_quotes_from_retrieval_roles() -> None:
    issue = {
        "issue_id": "issue:a", "member_finding_ids": ["finding:a"], "title": "原候选",
        "observation": "原观察", "why_it_matters": "原关注点", "evidence_ids": ["evidence:primary"],
        "citations": [{"evidence_id": "evidence:primary", "quote": "原发现引文"}],
    }
    supplement = {
        "evidence_id": "evidence:supplement", "source_document_id": SOURCE_IDENTITY["source_document_id"],
        "source_sha256": SOURCE_SHA, "pdf_page": 2, "block_index": 4, "start_char": 10,
        "end_char": 20, "bbox": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
        "raw_text": "同源补充片段", "section": "notes", "selection_reason": "same_source_keyword_context_match",
        "issue_ids": ["issue:a"],
    }
    value = _supplement_input([issue], [supplement], SOURCE_IDENTITY)
    assert value["issues"][0]["primary_finding_quotes"] == [
        {"evidence_id": "evidence:primary", "quote": "原发现引文"},
    ]
    assert "support" not in value["role_semantics"]
    assert "primary_finding_quote" in value["role_semantics"]
    assert set(value["role_semantics"]) == {"primary_finding_quote", "context", "possible_explanation", "unknown"}
    assert [item["issue_id"] for item in value["issues"]] == ["issue:a"]
    assert "issue_ids" not in value["supplements"][0]


def test_global_shared_candidate_pool_preserves_member_quotes_and_exclusion_scope() -> None:
    first, first_evidence = _finding(1, observation="代表事项观察", quote="代表事项引文")
    second, second_evidence = _finding(2, observation="同事项另一成员观察", quote="成员排除依据引文")
    source = _source_identity(SOURCE_IDENTITY)
    candidates = [
        _candidate_from_finding(first, {first_evidence["evidence_id"]: first_evidence}, source),
        _candidate_from_finding(second, {second_evidence["evidence_id"]: second_evidence}, source),
    ]
    state = {
        "input": {"finding_ids": [first["finding_id"], second["finding_id"]]},
        "candidates": candidates, "member_decisions": [], "issues": [],
    }
    _apply_group_decisions(state, {
        first["finding_id"]: {"disposition": "retain", "reason_code": "distinct_issue"},
        second["finding_id"]: {
            "disposition": "merge", "target_finding_id": first["finding_id"],
            "reason_code": "same_observed_event",
        },
    }, candidates, source)
    input_value = _build_global_input(state["issues"], state["member_decisions"], candidates, source)
    assert set(input_value) == {
        "schema_version", "source_identity", "input_issue_ids", "issue_members",
        "candidate_pool", "member_decisions", "source_candidate_count",
    }
    assert input_value["issue_members"] == [{
        "issue_id": state["issues"][0]["issue_id"],
        "representative_finding_id": first["finding_id"],
        "member_finding_ids": sorted([first["finding_id"], second["finding_id"]]),
    }]
    assert len(input_value["candidate_pool"]) == 2
    assert len(input_value["member_decisions"]) == 2
    valid = {
        "schema_version": "annual_issue_global_result_v1",
        "decisions": [{
            "issue_id": state["issues"][0]["issue_id"], "disposition": "exclude",
            "target_issue_id": None, "reason_code": "citation_scope_mismatch",
            "reason_evidence_id": second_evidence["evidence_id"], "reason_quote": "成员排除依据引文",
        }],
    }
    assert parse_typed_result(json.dumps(valid, ensure_ascii=False), kind="global", input_value=input_value)["decisions"][0]["reason_evidence_id"] == second_evidence["evidence_id"]
    invalid = copy.deepcopy(valid)
    invalid["decisions"][0]["reason_quote"] = "无关候选的未授权引文"
    with pytest.raises(ValueError, match="typed_response_invalid"):
        parse_typed_result(json.dumps(invalid, ensure_ascii=False), kind="global", input_value=input_value)


def test_grouping_wire_stores_duplicate_members_once_with_full_member_content() -> None:
    finding, evidence = _finding(3, observation="完全相同观察", quote="同一原文")
    source = _source_identity(SOURCE_IDENTITY)
    candidate = _candidate_from_finding(finding, {evidence["evidence_id"]: evidence}, source)
    second = copy.deepcopy(candidate)
    second["finding_id"] = f"disc:{SOURCE_SHA[:12]}-{4:020x}"
    second["candidate_id"] = "candidate:" + hashlib.sha256(second["finding_id"].encode()).hexdigest()[:24]
    second["member_finding_ids"] = [second["finding_id"]]
    second["priority"] = "high"
    groups = _local_exact_duplicate_groups([candidate, second])
    assert len(groups) == 1

    value = _group_input(groups, source)
    assert set(value) == {
        "schema_version", "source_identity", "groups", "candidate_pool", "input_member_finding_ids",
    }
    assert value["groups"] == [{
        "group_id": groups[0]["group_id"],
        "representative_finding_id": candidate["finding_id"],
        "member_finding_ids": sorted([candidate["finding_id"], second["finding_id"]]),
    }]
    assert value["input_member_finding_ids"] == sorted([candidate["finding_id"], second["finding_id"]])
    assert value["candidate_pool"] == sorted([candidate, second], key=lambda item: item["finding_id"])
    assert value["candidate_pool"][0]["priority"] != value["candidate_pool"][1]["priority"]


def test_running_checkpoint_retains_full_candidate_ledger_as_pending() -> None:
    pipeline = _pending_pipeline()
    validated = validate_pipeline(pipeline, identity=SOURCE_IDENTITY)
    assert validated["input"]["finding_count"] == 1
    assert validated["member_decisions"][0]["disposition"] == "pending"
    assert validated["global_closure"]["closed"] is False
    assert validated["global_closure"]["no_priority_allowed"] is False
    changed = copy.deepcopy(pipeline)
    changed["candidates"].clear()
    with pytest.raises(ValueError, match="annual_issue_pipeline_member_conservation_failed"):
        validate_pipeline(changed, identity=SOURCE_IDENTITY)


def test_historical_discovery_evidence_association_rebuilds_from_exact_citations() -> None:
    finding, evidence = _finding(3, observation="历史合格 finding 的定位仍由原文引文约束。", quote="同源原文")
    evidence.pop("finding_ids")
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256")},
        "status": "partial", "prompt_version": "annual_discovery_v3",
        "coverage": {"complete": False}, "findings": [finding], "evidences": [evidence],
    }
    findings, evidence_map = _validated_findings(discovery, SOURCE_IDENTITY)
    assert [item["finding_id"] for item in findings] == [finding["finding_id"]]
    assert evidence_map[evidence["evidence_id"]]["raw_text"] == "同源原文"

    foreign = copy.deepcopy(discovery)
    foreign["findings"][0]["citations"][0]["evidence_id"] = "adisc-foreign-source"
    foreign["findings"][0]["evidence_ids"] = ["adisc-foreign-source"]
    assert _validated_findings(foreign, SOURCE_IDENTITY)[0] == []

    explicit_null = copy.deepcopy(discovery)
    explicit_null["evidences"][0]["finding_ids"] = None
    assert _validated_findings(explicit_null, SOURCE_IDENTITY)[0] == []

    wrong_membership = copy.deepcopy(discovery)
    wrong_membership["evidences"][0]["finding_ids"] = ["disc:foreign-source-00000000000000000000"]
    assert _validated_findings(wrong_membership, SOURCE_IDENTITY)[0] == []


def test_request_hash_is_the_actual_wire_body_serialization() -> None:
    prompt = "group prompt"
    body = {"schema_version": "test", "input_member_finding_ids": ["finding:a"]}
    encoded = encode_annual_issue_input_wire(body)
    messages = [ChatMessage(role="system", content=prompt), ChatMessage(role="user", content=json.dumps(encoded, ensure_ascii=False, separators=(",", ":")))]
    expected = hashlib.sha256(serialize_chat_body("offline-mock-model", messages)).hexdigest()
    assert _wire_hash("offline-mock-model", prompt, body) == expected


def test_typed_shard_call_uses_the_same_wire_body_and_hash_without_provider_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    model = "offline-mock-model"
    settings = ModelSettings("http://127.0.0.1:1", "offline-fake-key-unused", model, 1)
    prompt = "typed issue prompt"
    input_value = {
        "schema_version": "annual_issue_group_shard_input_v1",
        "source_identity": {
            key: SOURCE_IDENTITY[key]
            for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")
        },
        "groups": [],
        "candidate_pool": [],
        "input_member_finding_ids": [],
    }
    expected_body = _wire_body(model, prompt, input_value)
    expected_hash = hashlib.sha256(expected_body).hexdigest()
    shard = {"input": input_value, "semantic_sha256": "a" * 64, "wire_body_sha256": expected_hash}
    captured: dict[str, object] = {}

    def fake_audited_chat(messages, call_settings, **kwargs):
        captured["messages"] = messages
        captured["settings"] = call_settings
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            audit_dir=tmp_path / "artifacts" / "runs" / "offline-fake" / "audit",
            completion=SimpleNamespace(text=json.dumps({
                "schema_version": "annual_issue_group_shard_result_v1", "decisions": [],
            })),
            provider_admission_count=0,
            response_count=0,
        )

    monkeypatch.setattr(issue_pipeline, "audited_complete_chat", fake_audited_chat)
    result, audit, attempts, responses = issue_pipeline._call_typed_shard(
        shard,
        settings=settings,
        run_dir=tmp_path / "artifacts" / "runs" / "offline-fake",
        artifacts_root=tmp_path / "artifacts",
        prompt=prompt,
        prompt_version="annual_issue_grouping_v1",
        call_stage="annual_issue_grouping",
        kind="grouping",
    )

    sent_messages = captured["messages"]
    sent_body = serialize_chat_body(model, sent_messages)
    assert sent_messages == issue_pipeline._wire_messages(prompt, input_value)
    assert sent_body == expected_body
    assert audit["request_body_sha256"] == expected_hash == _wire_hash(model, prompt, input_value)
    assert _wire_chars(model, prompt, input_value) == len(expected_body.decode("utf-8"))
    assert result == {"schema_version": "annual_issue_group_shard_result_v1", "decisions": []}
    assert attempts == responses == 0


def test_issue_input_wire_is_strictly_lossless_and_does_not_reinterpret_marker_keys() -> None:
    original = {
        "schema_version": "annual_issue_group_shard_input_v1",
        "candidate_pool": [
            {"$s": ["ordinary", None], "$o": {"$d": "business value"}, "$c": "source text", "$t": 7, "$m": False, "$k0": "full field"},
            {"$s": [], "$o": None, "$c": None, "$t": None, "$m": None, "$k0": None},
        ],
        "absent_and_null": [{}, {"value": None}],
        "source_sha256": "a" * 64,
    }
    encoded = encode_annual_issue_input_wire(original)
    decoded = decode_annual_issue_input_wire(encoded)

    assert encoded["schema_version"] == ISSUE_WIRE_VERSION
    assert decoded == original
    assert "value" not in decoded["absent_and_null"][0]
    assert decoded["absent_and_null"][1]["value"] is None


def test_issue_input_wire_rejects_other_protocol_schema() -> None:
    encoded = encode_annual_issue_input_wire({"input_issue_ids": ["issue:1"]})
    encoded["schema_version"] = "annual_model_review_v3_compact_v3"

    with pytest.raises(ValueError):
        decode_annual_issue_input_wire(encoded)


def test_request_hash_is_stable_after_recursive_archive_key_ordering() -> None:
    prompt = "group prompt"
    original = {
        "schema_version": "test",
        "candidate_pool": [{"zeta": 3, "alpha": {"last": 2, "first": 1}}],
    }
    archived = json.loads(json.dumps(original, ensure_ascii=False, sort_keys=True))

    assert archived == original
    assert _wire_hash("offline-mock-model", prompt, archived) == _wire_hash("offline-mock-model", prompt, original)


def test_single_complete_group_shard_records_its_relation_closure() -> None:
    later_lexically = f"disc:{SOURCE_SHA[:12]}-{1:020x}"
    first_in_source = f"disc:{SOURCE_SHA[:12]}-{2:020x}"
    state = {
        "input": {"finding_ids": [first_in_source, later_lexically]},
        "member_decisions": [
            {"finding_id": first_in_source, "disposition": "retain"},
            {"finding_id": later_lexically, "disposition": "retain"},
        ],
        "candidates": [],
    }
    provisional = [
        {"issue_id": "issue:lexically-first", "member_finding_ids": [later_lexically], "citations": [], "follow_up": "复核来源"},
        {"issue_id": "issue:source-first", "member_finding_ids": [first_in_source], "citations": [], "follow_up": "复核来源"},
    ]

    _close_single_group_shard_scope(state, provisional, [])

    assert state["global_closure"]["closed"] is True
    assert state["global_closure"]["input_finding_ids"] == [first_in_source, later_lexically]
    assert [issue["issue_id"] for issue in state["issues"]] == ["issue:source-first", "issue:lexically-first"]
    assert [issue["review_order_reason"].split("source_order=")[1].split(";")[0] for issue in state["issues"]] == ["0", "1"]
    assert state["issues"][0]["status"] == "accepted"
    assert state["issues"][0]["global_relation"] == {
        "disposition": "retain",
        "target_issue_id": None,
        "reason_code": "single_group_shard_complete",
    }


def test_optional_phase_timings_are_strict_and_separate_from_old_pipeline_shape() -> None:
    pipeline = _pending_pipeline()
    validate_pipeline(pipeline, identity=SOURCE_IDENTITY)
    pipeline["phase_timings"] = {
        "schema_version": "annual_issue_pipeline_phase_timings_v1",
        "annual_issue_grouping_elapsed_seconds": 3.125,
        "grouping_elapsed_seconds": 1.0,
        "global_reconciliation_elapsed_seconds": 0.5,
        "supplements_elapsed_seconds": 0.5,
    }
    assert validate_pipeline(pipeline, identity=SOURCE_IDENTITY)["phase_timings"]["annual_issue_grouping_elapsed_seconds"] == 3.125

    bad_total = copy.deepcopy(pipeline)
    bad_total["phase_timings"]["grouping_elapsed_seconds"] = 3.2
    with pytest.raises(ValueError, match="annual_issue_pipeline_phase_timings_invalid"):
        validate_pipeline(bad_total, identity=SOURCE_IDENTITY)

    bad_key = copy.deepcopy(pipeline)
    bad_key["phase_timings"]["parent_audit_seconds"] = 99.0
    with pytest.raises(ValueError, match="annual_issue_pipeline_phase_timings_invalid"):
        validate_pipeline(bad_key, identity=SOURCE_IDENTITY)

    bad_sum = copy.deepcopy(pipeline)
    bad_sum["phase_timings"]["global_reconciliation_elapsed_seconds"] = 2.0
    with pytest.raises(ValueError, match="annual_issue_pipeline_phase_timings_invalid"):
        validate_pipeline(bad_sum, identity=SOURCE_IDENTITY)


def test_same_source_retrieval_records_all_hits_and_bounded_selection_separately() -> None:
    blocks = [
        PdfTextBlock(0, "管理层讨论与分析", PdfBBox(0.0, 0.0, 100.0, 20.0)),
        *[
            PdfTextBlock(index, f"应收账款增长异常事项解释段落编号{index}。", PdfBBox(0.0, index * 25.0, 200.0, index * 25.0 + 20.0))
            for index in range(1, 7)
        ],
    ]
    parsed = ParsedTextPdf(
        document_id=SOURCE_IDENTITY["source_document_id"], source_sha256=SOURCE_SHA,
        source_filename="constructed.pdf", page_count=1, coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point", coordinate_note="constructed fixture", ocr_applied=False,
        extractor="fixture", extractor_version="1", pages=(PdfPageText(
            page_number=1, width=595.0, height=842.0, rotation=0, status="extracted",
            image_block_count=0, limitation=None, blocks=tuple(blocks),
        ),),
    )
    issue = {
        "issue_id": "issue:receivables-growth", "title": "应收账款增长",
        "observation": "应收账款增长异常", "why_it_matters": "收入确认风险需核查。",
        "follow_up": "查看应收账款附注。", "citations": [],
    }
    supplements, coverage = retrieve_annual_issue_supplements([issue], parsed, {
        "source_document_id": SOURCE_IDENTITY["source_document_id"], "source_sha256": SOURCE_SHA,
    })
    assert coverage["complete"] is True
    assert coverage["matched_issue_block_pairs"] == 6
    assert coverage["selected_issue_block_pairs"] == 4
    assert coverage["unselected_issue_block_pairs"] == 2
    assert coverage["matched_blocks"] == 6
    assert coverage["selected_blocks"] == len(supplements) == 4
    assert coverage["max_terms_per_issue"] == 40
    assert coverage["max_matches_per_issue"] == 4


def test_retrieval_omitted_chars_count_source_interval_union_for_overlapping_windows() -> None:
    body = "x" * 180 + "alpha" + "x" * 160 + "beta" + "x" * 700
    blocks = (
        PdfTextBlock(0, "财务报表附注", PdfBBox(0.0, 0.0, 100.0, 20.0)),
        PdfTextBlock(1, body, PdfBBox(0.0, 25.0, 300.0, 45.0)),
    )
    parsed = ParsedTextPdf(
        document_id=SOURCE_IDENTITY["source_document_id"], source_sha256=SOURCE_SHA,
        source_filename="constructed.pdf", page_count=1, coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point", coordinate_note="constructed fixture", ocr_applied=False,
        extractor="fixture", extractor_version="1", pages=(PdfPageText(
            page_number=1, width=595.0, height=842.0, rotation=0, status="extracted",
            image_block_count=0, limitation=None, blocks=blocks,
        ),),
    )
    issues = [
        {"issue_id": "issue:alpha", "title": "alpha", "observation": "", "why_it_matters": "", "follow_up": "", "citations": []},
        {"issue_id": "issue:beta", "title": "beta", "observation": "", "why_it_matters": "", "follow_up": "", "citations": []},
    ]
    source = {"source_document_id": SOURCE_IDENTITY["source_document_id"], "source_sha256": SOURCE_SHA}
    supplements, coverage = retrieve_annual_issue_supplements(issues, parsed, source)
    intervals = sorted((item["start_char"], item["end_char"]) for item in supplements)
    assert len(supplements) == 2
    assert intervals[0][1] > intervals[1][0]
    assert coverage["selected_excerpt_chars"] == sum(len(item["raw_text"]) for item in supplements)
    assert coverage["selected_excerpt_chars"] > coverage["selected_unique_source_chars"]
    assert coverage["selected_unique_source_chars"] == intervals[0][1] - intervals[0][0] + intervals[1][1] - intervals[0][1]
    assert coverage["omitted_chars"] == coverage["scanned_chars"] - coverage["selected_unique_source_chars"]
    assert coverage["selected_blocks"] == 1
    assert coverage["matched_blocks"] == 1
    from finagent.agents.annual_issue_pipeline import _validate_pipeline_retrieval
    _validate_pipeline_retrieval(coverage, source, supplements)

    altered = dict(coverage, omitted_chars=coverage["omitted_chars"] + 1)
    with pytest.raises(ValueError, match="annual_issue_pipeline_retrieval_invalid"):
        _validate_pipeline_retrieval(altered, source, supplements)


def test_display_order_uses_review_references_without_mutating_business_order() -> None:
    def issue(issue_id: str, finding_id: str, evidence_id: str) -> dict:
        citation = {
            "evidence_id": evidence_id, "pdf_page": 2, "block_index": 3,
            "start_char": 14, "bbox": {"x0": 0.0, "y0": 0.0, "x1": 20.0, "y1": 10.0},
        }
        return {
            "issue_id": issue_id, "member_finding_ids": [finding_id], "evidence_ids": [evidence_id],
            "citations": [citation], "follow_up": "检查相关附注。", "priority": "high",
        }

    first = issue("issue:first", "finding:first", "evidence:first")
    second = issue("issue:second", "finding:second", "evidence:second")
    pipeline = {
        "issues": [first, second],
        "candidates": [
            {**first, "finding_id": "finding:first"},
            {**second, "finding_id": "finding:second", "priority": "low"},
        ],
    }
    before = copy.deepcopy(pipeline)
    review = {
        "status": "completed", "source_binding_status": "matched",
        "reasons": [{"text": "有待核查事项", "evidence_ids": ["issue:second"]}],
        "follow_up_items": [{"object": "issue:second", "action": "核对附注", "evidence_ids": ["issue:second"]}],
    }

    order = build_issue_display_order(pipeline, review)

    assert [item["issue_id"] for item in order["issue_order"]] == ["issue:second", "issue:first"]
    assert [item["finding_id"] for item in order["candidate_order"]] == ["finding:second", "finding:first"]
    assert "评审理由引用命中" in order["issue_order"][0]["reason"]
    assert "评审后续核查引用命中" in order["issue_order"][0]["reason"]
    assert pipeline == before


def test_runtime_generated_phase_timings_pass_pipeline_reader(tmp_path) -> None:
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in (
            "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
        )},
        "status": "completed",
        "prompt_version": "annual_discovery_v4",
        "coverage": {"complete": True},
        "findings": [],
        "evidences": [],
    }
    parsed = ParsedTextPdf(
        document_id=SOURCE_IDENTITY["source_document_id"], source_sha256=SOURCE_SHA,
        source_filename="constructed-empty.pdf", page_count=1, coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point", coordinate_note="constructed fixture", ocr_applied=False,
        extractor="fixture", extractor_version="1", pages=(PdfPageText(
            page_number=1, width=595.0, height=842.0, rotation=0, status="extracted",
            image_block_count=0, limitation=None, blocks=(),
        ),),
    )
    generated = run_annual_issue_pipeline(
        discovery,
        identity=SOURCE_IDENTITY,
        parsed_pdf=parsed,
        settings=ModelSettings("http://127.0.0.1", "offline-test-token", "offline-mock-model"),
        run_dir=tmp_path / "runs" / SOURCE_IDENTITY["run_id"],
        artifacts_root=tmp_path / "artifacts",
        execution_binding=_binding_v10(),
    )

    validated = validate_pipeline(generated, identity=SOURCE_IDENTITY)
    timings = validated["phase_timings"]
    assert timings["schema_version"] == "annual_issue_pipeline_phase_timings_v1"
    assert timings["annual_issue_grouping_elapsed_seconds"] >= 0
    assert timings["grouping_elapsed_seconds"] >= 0
    assert timings["supplements_elapsed_seconds"] >= 0
    assert "global_reconciliation_elapsed_seconds" not in timings


def _multi_shard_reuse_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    parent_identity = dict(SOURCE_IDENTITY, run_id="annual-analysis-parent-fixture")
    findings = []
    evidences = []
    for index in range(1, 5):
        finding, evidence = _finding(index, observation=f"不同的观察事项 {index}", quote=f"原文引文{index}")
        findings.append(finding)
        evidences.append(evidence)

    def discovery(identity: dict) -> dict:
        return {
            "kind": "fintrace_annual_discovery",
            **{key: identity[key] for key in (
                "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
            )},
            "status": "completed",
            "prompt_version": "annual_discovery_v4",
            "coverage": {"complete": True},
            "findings": findings,
            "evidences": evidences,
        }

    parsed_pdf = ParsedTextPdf(
        document_id=SOURCE_IDENTITY["source_document_id"], source_sha256=SOURCE_SHA,
        source_filename="constructed-reuse.pdf", page_count=1, coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point", coordinate_note="constructed fixture", ocr_applied=False,
        extractor="fixture", extractor_version="1", pages=(PdfPageText(
            page_number=1, width=595.0, height=842.0, rotation=0, status="extracted",
            image_block_count=0, limitation=None, blocks=(),
        ),),
    )
    settings = ModelSettings("http://127.0.0.1:1", "offline-fake-key-unused", "offline-mock-model", 1)
    call_log: list[tuple[str, str]] = []

    def fake_retrieve(issues, _parsed_pdf, _source, **_kwargs):
        issue_id = issues[0]["issue_id"]
        supplement = {
            "evidence_id": "supp:fixture-evidence",
            "issue_ids": [issue_id],
            "source_document_id": SOURCE_IDENTITY["source_document_id"],
            "source_sha256": SOURCE_SHA,
            "pdf_page": 1,
            "block_index": 0,
            "start_char": 0,
            "end_char": 8,
            "bbox": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
            "raw_text": "附注片段",
            "section": "notes",
            "selection_reason": "same_source_keyword_context_match",
        }
        return [supplement], {"scope": "same_source_extracted_text_blocks", "complete": True}

    def fake_call(shard, *, kind, run_dir, **_kwargs):
        input_value = shard["input"]
        shard_id = (
            f"group:{len(call_log):03d}" if kind == "grouping"
            else f"{kind}:{len(call_log):03d}"
        )
        call_log.append((kind, shard_id))
        if kind == "grouping":
            decisions = [{
                "finding_id": finding_id, "disposition": "retain", "target_finding_id": None,
                "reason_code": "distinct_issue", "reason_evidence_id": None, "reason_quote": None,
            } for finding_id in input_value["input_member_finding_ids"]]
            result = {"schema_version": "annual_issue_group_shard_result_v1", "decisions": decisions}
        elif kind == "global":
            decisions = [{
                "issue_id": issue_id, "disposition": "retain", "target_issue_id": None,
                "reason_code": "distinct_across_shards", "reason_evidence_id": None, "reason_quote": None,
            } for issue_id in input_value["input_issue_ids"]]
            result = {"schema_version": "annual_issue_global_result_v1", "decisions": decisions}
        else:
            roles = [{
                "issue_id": item["issue_id"], "evidence_id": item["evidence_id"],
                "role": "context", "reason_code": "context_only",
            } for item in input_value["evidence_assignments"]]
            result = {"schema_version": "annual_issue_supplement_result_v1", "roles": roles}
        audit_id = f"fixture-audit-{len(call_log):03d}"
        prompt_version = {
            "grouping": "annual_issue_grouping_v1",
            "global": "annual_issue_global_v1",
            "supplement": "annual_issue_supplement_v1",
        }[kind]
        audit_dir = f"runs/{run_dir.name}/{audit_id}"
        audit = {
            "audit_dir": audit_dir,
            "status": "succeeded",
            "request_path": f"{audit_dir}/request.json",
            "response_path": f"{audit_dir}/response.json",
            "prompt_version": prompt_version,
            "request_body_sha256": shard["wire_body_sha256"],
            "semantic_input_sha256": shard["semantic_sha256"],
        }
        return result, audit, 2, 1

    monkeypatch.setattr(issue_pipeline, "_retrieve_supplements", fake_retrieve)
    monkeypatch.setattr(issue_pipeline, "_call_typed_shard", fake_call)
    parent = run_annual_issue_pipeline(
        discovery(parent_identity), identity=parent_identity, parsed_pdf=parsed_pdf,
        settings=settings, run_dir=tmp_path / "runs" / parent_identity["run_id"],
        artifacts_root=tmp_path / "artifacts", execution_binding=_binding_v10(),
    )
    assert [item["kind"] for item in parent["shards"]] == ["grouping", "grouping", "global", "supplement"]
    assert len(call_log) == 4
    return parent_identity, discovery, parsed_pdf, settings, parent, call_log


def test_all_reused_shards_keep_all_audits_and_reader_rejects_corrupt_summary(monkeypatch, tmp_path) -> None:
    parent_identity, discovery, parsed_pdf, settings, parent, call_log = _multi_shard_reuse_fixture(monkeypatch, tmp_path)
    child_identity = dict(SOURCE_IDENTITY, run_id="annual-analysis-child-all-reused")
    call_log.clear()
    monkeypatch.setattr(issue_pipeline, "_try_reuse_shard", lambda *_args, **_kwargs: True)
    child = run_annual_issue_pipeline(
        discovery(child_identity), identity=child_identity, parsed_pdf=parsed_pdf,
        settings=settings, run_dir=tmp_path / "runs" / child_identity["run_id"],
        artifacts_root=tmp_path / "artifacts", execution_binding=_binding_v10(),
        parent_pipeline=parent, parent_run_id=parent_identity["run_id"],
    )

    assert call_log == []
    assert len(child["shards"]) == 4
    assert child["audit_summary"] == {
        "attempt_count": 0,
        "response_count": 0,
        "reused_count": 4,
        "model_called": False,
        "audit_artifacts": [item["audit"] for item in child["shards"]],
    }
    parent_archive = SimpleNamespace(
        run_id=parent_identity["run_id"], report={"model_issue_pipeline": parent},
    )
    _validate_issue_shard_audits(tmp_path / "unused-root", child_identity["run_id"], child, parent_archive)

    corrupted = copy.deepcopy(child)
    corrupted["audit_summary"]["audit_artifacts"].pop()
    with pytest.raises(AnnualReportReadError) as exc_info:
        _validate_issue_shard_audits(tmp_path / "unused-root", child_identity["run_id"], corrupted, parent_archive)
    assert exc_info.value.code == "annual_issue_pipeline_audit_invalid"


def test_mixed_reuse_checkpoint_audit_summaries_follow_shard_state(monkeypatch, tmp_path) -> None:
    parent_identity, discovery, parsed_pdf, settings, parent, call_log = _multi_shard_reuse_fixture(monkeypatch, tmp_path)
    child_identity = dict(SOURCE_IDENTITY, run_id="annual-analysis-child-mixed-reuse")
    call_log.clear()

    def reuse_first_group_and_global(old, _shard, _source, _root, kind):
        return (kind == "global" or kind == "grouping" and old["shard_id"].startswith("group:000:"))

    monkeypatch.setattr(issue_pipeline, "_try_reuse_shard", reuse_first_group_and_global)
    checkpoints = []
    child = run_annual_issue_pipeline(
        discovery(child_identity), identity=child_identity, parsed_pdf=parsed_pdf,
        settings=settings, run_dir=tmp_path / "runs" / child_identity["run_id"],
        artifacts_root=tmp_path / "artifacts", execution_binding=_binding_v10(),
        parent_pipeline=parent, parent_run_id=parent_identity["run_id"],
        checkpoint_callback=lambda state: checkpoints.append(copy.deepcopy(state)),
    )

    assert [kind for kind, _shard_id in call_log] == ["grouping", "supplement"]
    for checkpoint in checkpoints:
        shards = checkpoint["shards"]
        summary = checkpoint["audit_summary"]
        assert summary["attempt_count"] == sum(item["attempt_count"] for item in shards)
        assert summary["response_count"] == sum(item["response_count"] for item in shards)
        assert summary["reused_count"] == sum(item["reused"] is True for item in shards)
        assert summary["model_called"] is (summary["attempt_count"] > 0)
        assert summary["audit_artifacts"] == [item["audit"] for item in shards if item.get("audit") is not None]

    global_checkpoint = next(
        checkpoint for checkpoint in checkpoints
        if any(item["kind"] == "global" for item in checkpoint["shards"])
    )
    assert len(global_checkpoint["shards"]) == 3
    assert global_checkpoint["audit_summary"]["attempt_count"] == 2
    assert global_checkpoint["audit_summary"]["response_count"] == 1
    assert global_checkpoint["audit_summary"]["reused_count"] == 2
    assert len(global_checkpoint["audit_summary"]["audit_artifacts"]) == 3
    assert len(child["audit_summary"]["audit_artifacts"]) == 4
    assert child["audit_summary"]["attempt_count"] == 4
    assert child["audit_summary"]["response_count"] == 2
    assert child["audit_summary"]["reused_count"] == 2


def test_recovery_capability_denies_fixed_supplement_tail_after_accepted_first_slice() -> None:
    issue_id = "issue:known"
    supplements = []
    assignments = []
    accepted_roles = []
    for index in range(40):
        evidence_id = f"supp:evidence-{index:02d}"
        supplements.append({
            "evidence_id": evidence_id,
            "issue_ids": [issue_id],
            "role_status": "accepted" if index < 32 else "pending",
            "source_document_id": SOURCE_IDENTITY["source_document_id"],
            "source_sha256": SOURCE_SHA,
            "pdf_page": 1,
            "block_index": index,
            "start_char": index * 8,
            "end_char": index * 8 + 8,
            "bbox": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
            "raw_text": f"同源检索片段{index:02d}",
            "section": "notes",
            "selection_reason": "same_source_keyword_context_match",
            "issue_roles": ([{"issue_id": issue_id, "role": "context", "reason_code": "same_section_context"}] if index < 32 else []),
        })
        if index < 32:
            assignments.append({"issue_id": issue_id, "evidence_id": evidence_id, "role": "context"})
            accepted_roles.append({"issue_id": issue_id, "evidence_id": evidence_id, "role": "context", "reason_code": "same_section_context"})
    pipeline = {
        "kind": "annual_issue_pipeline_v1",
        "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "status": "partial",
        "global_closure": {"closed": True, "pending_issue_ids": [], "relation_gaps": []},
        "issues": [{"issue_id": issue_id, "member_finding_ids": ["finding:known"], "title": "事项", "observation": "观察", "why_it_matters": "关注", "citations": []}],
        "supplements": supplements,
        "retrieval_coverage": {
            "scope": "same_source_extracted_text_blocks",
            "role_assignments_total": 40,
            "role_assignments_requested": 32,
            "role_assignments_pending": 8,
        },
        "shards": [{
            "kind": "supplement", "status": "accepted",
            "input": {"evidence_assignments": assignments},
            "result": {"roles": accepted_roles},
        }],
    }
    root = Path(__file__).resolve().parents[2]
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is False


def test_recovery_capability_keeps_retryable_in_cap_supplement_work() -> None:
    issue_id = "issue:known"
    issue = {
        "issue_id": issue_id, "member_finding_ids": ["finding:known"],
        "title": "事项标题", "observation": "观察内容", "why_it_matters": "关注原因",
        "citations": [],
    }
    supplement = {
        "evidence_id": "supp:one", "issue_ids": [issue_id], "role_status": "pending",
        "source_document_id": SOURCE_IDENTITY["source_document_id"], "source_sha256": SOURCE_SHA,
        "pdf_page": 1, "block_index": 0, "start_char": 0, "end_char": 8,
        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        "raw_text": "附注片段", "section": "notes", "selection_reason": "same_source_keyword_context_match",
        "issue_roles": [],
    }
    exact_input = _supplement_input([issue], [supplement], SOURCE_IDENTITY)
    pipeline = {
        "kind": "annual_issue_pipeline_v1",
        "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "status": "partial",
        "global_closure": {"closed": True, "pending_issue_ids": [], "relation_gaps": []},
        "issues": [issue],
        "supplements": [supplement],
        "retrieval_coverage": {
            "scope": "same_source_extracted_text_blocks",
            "role_assignments_total": 1,
            "role_assignments_requested": 1,
            "role_assignments_pending": 0,
        },
        "shards": [{
            "kind": "supplement", "status": "pending", "reason": "supplement_role_pending",
            "input": exact_input,
        }],
    }
    # The coverage ledger reports one requested pair with no unselected tail;
    # this failed submitted slice itself remains retryable.
    root = Path(__file__).resolve().parents[2]
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is True


def test_recovery_capability_uses_typed_wire_when_plain_input_exceeds_limit() -> None:
    root = Path(__file__).resolve().parents[2]
    model = "offline-mock-model"
    observation = "现金流变动需要依据附注披露核对。" * 420
    quote = "年报原文片段用于支持同一候选事项。" * 70
    finding, evidence = _finding(1, observation=observation, quote=quote)
    candidates = []
    for index in range(1, 4):
        member = copy.deepcopy(finding)
        member["finding_id"] = f"disc:{SOURCE_SHA[:12]}-{index:020x}"
        candidates.append(_candidate_from_finding(
            member, {evidence["evidence_id"]: evidence}, SOURCE_IDENTITY,
        ))

    prompt = (root / "config" / "prompts" / "annual_issue_grouping_v1.md").read_text(encoding="utf-8")
    planned = _build_group_shards(
        _local_exact_duplicate_groups(candidates),
        {evidence["evidence_id"]: evidence}, SOURCE_IDENTITY, model, prompt,
        execution_binding=_binding_v10(),
    )
    assert len(planned) == 1
    input_value = planned[0]["input"]

    # Reconstruct the legacy plain-JSON body independently to prove that this
    # is a real boundary case rather than a self-consistent helper assertion.
    plain_content = json.dumps(input_value, ensure_ascii=False, separators=(",", ":"))
    plain_body = serialize_chat_body(model, [
        ChatMessage(role="system", content=prompt),
        ChatMessage(role="user", content=plain_content),
    ])
    assert len(plain_body.decode("utf-8")) > MAX_SHARD_REQUEST_CHARS
    assert _wire_chars(model, prompt, input_value) <= MAX_SHARD_REQUEST_CHARS

    pipeline = {
        "kind": "annual_issue_pipeline_v1",
        "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "global_closure": {"closed": False, "pending_issue_ids": [], "relation_gaps": []},
        "candidates": candidates,
        "shards": [],
    }
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is True


@pytest.mark.parametrize("observation_chars, executable", [(26_000, True), (96_001, False)])
def test_recovery_capability_obeys_complete_global_wire_capacity(
    observation_chars: int, executable: bool,
) -> None:
    finding, evidence = _finding(1, observation="x" * observation_chars, quote="可追溯来源引文")
    discovery = {
        "kind": "fintrace_annual_discovery",
        **{key: SOURCE_IDENTITY[key] for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256")},
        "status": "completed", "prompt_version": "annual_discovery_v3",
        "coverage": {"complete": True}, "findings": [finding], "evidences": [evidence],
    }
    normalized_findings, evidence_map = _validated_findings(discovery, SOURCE_IDENTITY)
    candidate = _candidate_from_finding(normalized_findings[0], evidence_map, SOURCE_IDENTITY)
    state = {"input": {"finding_ids": [finding["finding_id"]]}, "candidates": [candidate], "member_decisions": [], "issues": []}
    decision = {
        "finding_id": finding["finding_id"], "disposition": "retain", "target_finding_id": None,
        "reason_code": "distinct_issue", "reason_evidence_id": None, "reason_quote": None,
    }
    _apply_group_decisions(state, {finding["finding_id"]: decision}, [candidate], SOURCE_IDENTITY)
    provisional = [row for row in state["issues"] if row["status"] == "provisional"]
    oversized_input = _build_global_input(provisional, state["member_decisions"], [candidate], SOURCE_IDENTITY)
    pipeline = {
        "kind": "annual_issue_pipeline_v1",
        "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "status": "partial",
        "global_closure": {"closed": False, "pending_issue_ids": [provisional[0]["issue_id"]], "relation_gaps": []},
        "candidates": [candidate], "issues": provisional,
        "member_decisions": state["member_decisions"],
        "retrieval_coverage": {
            "scope": "not_started",
            "role_assignments_total": 0,
            "role_assignments_requested": 0,
            "role_assignments_pending": 0,
        },
        "shards": [{
            "kind": "global", "status": "pending", "reason": "global_result_invalid",
            "input": oversized_input,
        }],
    }
    root = Path(__file__).resolve().parents[2]
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is executable


def test_recovery_capability_recognizes_published_supplement_plan_without_shard() -> None:
    issue_id = "issue:known"
    pipeline = {
        "kind": "annual_issue_pipeline_v1",
        "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "global_closure": {"closed": True, "pending_issue_ids": [], "relation_gaps": []},
        "issues": [{
            "issue_id": issue_id, "member_finding_ids": ["finding:one"], "title": "事项标题",
            "observation": "观察内容", "why_it_matters": "关注原因", "citations": [],
        }],
        "supplements": [{
            "evidence_id": "supp:one", "issue_ids": [issue_id], "role_status": "pending",
            "source_document_id": SOURCE_IDENTITY["source_document_id"], "source_sha256": SOURCE_SHA,
            "pdf_page": 1, "block_index": 0, "start_char": 0, "end_char": 8,
            "bbox": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
            "raw_text": "附注片段", "section": "notes", "selection_reason": "same_source_keyword_context_match",
        }],
        "retrieval_coverage": {
            "scope": "same_source_extracted_text_blocks", "role_assignments_total": 1,
            "role_assignments_requested": 1, "role_assignments_pending": 0,
        },
        "shards": [],
    }
    root = Path(__file__).resolve().parents[2]
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is True


def test_recovery_capability_keeps_unsharded_in_cap_role_rows_resumable() -> None:
    issue_id = "issue:known"
    issue = {
        "issue_id": issue_id, "member_finding_ids": ["finding:one"],
        "title": "事项标题", "observation": "观察内容", "why_it_matters": "关注原因",
        "citations": [],
    }
    supplements = [{
        "evidence_id": f"supp:{index:02d}", "issue_ids": [issue_id], "role_status": "pending",
        "source_document_id": SOURCE_IDENTITY["source_document_id"], "source_sha256": SOURCE_SHA,
        "pdf_page": 1, "block_index": index, "start_char": index * 8, "end_char": index * 8 + 8,
        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        "raw_text": f"附注片段{index:02d}", "section": "notes",
        "selection_reason": "same_source_keyword_context_match", "issue_roles": [],
    } for index in range(32)]
    pipeline = {
        "kind": "annual_issue_pipeline_v1",
        "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "status": "partial",
        "global_closure": {"closed": True, "pending_issue_ids": [], "relation_gaps": []},
        "issues": [issue], "supplements": supplements, "shards": [],
        "retrieval_coverage": {
            "scope": "same_source_extracted_text_blocks", "role_assignments_total": 32,
            "role_assignments_requested": 32, "role_assignments_pending": 0,
        },
    }
    root = Path(__file__).resolve().parents[2]
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is True


def test_recovery_capability_recognizes_group_plan_without_shard() -> None:
    candidate = {
        "candidate_id": "candidate:one", "finding_id": "finding:one", "member_finding_ids": ["finding:one"],
        "title": "事项标题", "observation": "观察内容", "why_it_matters": "关注原因", "follow_up": "核对附注",
        "priority": "medium", "evidence_ids": [], "citations": [], "source_identity": {
            key: SOURCE_IDENTITY[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")
        },
    }
    pipeline = {
        "kind": "annual_issue_pipeline_v1", "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "global_closure": {"closed": False, "pending_issue_ids": [], "relation_gaps": []},
        "candidates": [candidate], "issues": [], "shards": [],
    }
    root = Path(__file__).resolve().parents[2]
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is True


def test_recovery_capability_recognizes_global_plan_without_shard() -> None:
    candidates = []
    issues = []
    decisions = []
    for index in range(13):
        finding_id = f"finding:{index:02d}"
        issue_id = f"issue:{index:02d}"
        candidates.append({
            "candidate_id": f"candidate:{index:02d}", "finding_id": finding_id,
            "member_finding_ids": [finding_id], "title": f"事项标题{index}",
            "observation": f"观察内容{index}", "why_it_matters": "关注原因", "follow_up": "核对附注",
            "priority": "medium", "evidence_ids": [], "citations": [], "source_identity": {
                key: SOURCE_IDENTITY[key] for key in ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")
            },
        })
        issues.append({
            "issue_id": issue_id, "status": "provisional", "member_finding_ids": [finding_id],
            "member_sources": [{"finding_id": finding_id, "disposition": "retain"}],
        })
        decisions.append({"finding_id": finding_id, "disposition": "retain", "issue_id": issue_id})
    prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_issue_grouping_v1.md").read_text(encoding="utf-8")
    planned_groups = _build_group_shards(
        _local_exact_duplicate_groups(candidates), {}, SOURCE_IDENTITY, "offline-mock-model", prompt,
        execution_binding=_binding_v10(),
    )
    assert len(planned_groups) > 1
    group_shards = [
        {"kind": "grouping", "status": "accepted", "shard_id": f"group:{index:03d}:{item['semantic_sha256'][:16]}"}
        for index, item in enumerate(planned_groups)
    ]
    pipeline = {
        "kind": "annual_issue_pipeline_v1", "execution_binding": _binding_v10(),
        "source_identity": SOURCE_IDENTITY,
        "global_closure": {"closed": False, "pending_issue_ids": [item["issue_id"] for item in issues], "relation_gaps": []},
        "candidates": candidates, "issues": issues, "member_decisions": decisions, "shards": group_shards,
    }
    root = Path(__file__).resolve().parents[2]
    assert _annual_issue_pipeline_has_executable_work(root, pipeline) is True


def _member_bounded_runtime_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, count: int, *, exact_family: bool = False):
    """Exercise the production planner, typed call, audit reconstruction and closure."""
    artifacts = tmp_path / "artifacts"
    findings, evidences = [], []
    for index in range(1, count + 1):
        finding, evidence = _finding(
            index, observation="完整相同观察" if exact_family else f"完整独立观察 {index}",
            quote="完整同源引文" if exact_family else f"完整同源引文 {index}",
        )
        finding["priority"] = "high" if index % 2 else "low"
        if not exact_family:
            evidence_id = f"adisc-{SOURCE_SHA[:12]}-p1-b{index - 1}-c0-{index:08x}"
            evidence["evidence_id"] = evidence_id
            evidence["block_index"] = index - 1
            finding["citations"][0]["evidence_id"] = evidence_id
            finding["evidence_ids"] = [evidence_id]
        if exact_family:
            finding["title"] = "完整相同事项"
            if evidences:
                finding["citations"] = copy.deepcopy(findings[0]["citations"])
                finding["evidence_ids"] = list(findings[0]["evidence_ids"])
                evidences[0]["finding_ids"].append(finding["finding_id"])
            else:
                evidences.append(evidence)
        else:
            evidences.append(evidence)
        findings.append(finding)
    parsed = ParsedTextPdf(
        document_id=SOURCE_IDENTITY["source_document_id"], source_sha256=SOURCE_SHA,
        source_filename="constructed-member-bounded.pdf", page_count=1,
        coordinate_system="pymupdf_page_top_left", coordinate_unit="pdf_point",
        coordinate_note="original synthetic fixture", ocr_applied=False,
        extractor="fixture", extractor_version="1", pages=(PdfPageText(
            page_number=1, width=595.0, height=842.0, rotation=0, status="extracted",
            image_block_count=0, limitation=None, blocks=tuple(
                PdfTextBlock(evidence["block_index"], evidence["raw_text"], PdfBBox(**evidence["bbox"]))
                for evidence in evidences
            ),
        ),),
    )
    sent: list[dict] = []
    control = {"stop_after_group_calls": None, "exclude_finding_id": None, "snapshots": []}

    def audited_fixture(messages, settings, *, run_dir, prompt_version, **_kwargs):
        value = decode_annual_issue_input_wire(json.loads(messages[1].content))
        if (
            prompt_version == "annual_issue_grouping_v1"
            and control["stop_after_group_calls"] is not None
            and sum(row["prompt_version"] == prompt_version for row in sent) >= control["stop_after_group_calls"]
        ):
            error = RuntimeError("constructed exhausted stage allowance")
            error.category = "task_budget_exhausted"
            raise error
        if prompt_version == "annual_issue_grouping_v1":
            decisions = []
            for group in value["groups"]:
                representative = group["representative_finding_id"]
                for finding_id in group["member_finding_ids"]:
                    retained = finding_id == representative
                    if finding_id == control["exclude_finding_id"]:
                        candidate = next(row for row in value["candidate_pool"] if row["finding_id"] == finding_id)
                        citation = candidate["citations"][0]
                        decisions.append({
                            "finding_id": finding_id, "disposition": "exclude",
                            "target_finding_id": None, "reason_code": "not_financial_condition",
                            "reason_evidence_id": citation["evidence_id"], "reason_quote": citation["quote"],
                        })
                        continue
                    decisions.append({
                        "finding_id": finding_id, "disposition": "retain" if retained else "merge",
                        "target_finding_id": None if retained else representative,
                        "reason_code": "distinct_issue" if retained else "same_observed_event",
                        "reason_evidence_id": None, "reason_quote": None,
                    })
            result = {"schema_version": "annual_issue_group_shard_result_v1", "decisions": decisions}
        elif prompt_version == "annual_issue_global_v1":
            result = {
                "schema_version": "annual_issue_global_result_v1",
                "decisions": [{
                    "issue_id": issue_id,
                    "disposition": "merge" if exact_family and index else "retain",
                    "target_issue_id": value["input_issue_ids"][0] if exact_family and index else None,
                    "reason_code": "same_observed_event" if exact_family and index else "distinct_across_shards",
                    "reason_evidence_id": None, "reason_quote": None,
                } for index, issue_id in enumerate(value["input_issue_ids"])],
            }
        else:
            raise AssertionError(f"unexpected model stage: {prompt_version}")
        text = json.dumps(result, ensure_ascii=False)
        request = {
            "model": settings.model, "prompt_version": prompt_version,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
        }
        audit_dir = run_dir / f"constructed-audit-{len(sent):03d}"
        audit_dir.mkdir(parents=True)
        (audit_dir / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        (audit_dir / "response.json").write_text(
            json.dumps({"status": "succeeded", "text": text}, ensure_ascii=False), encoding="utf-8",
        )
        sent.append({
            "prompt_version": prompt_version, "input": value, "request": request,
            "body": serialize_chat_body(settings.model, messages),
        })
        return SimpleNamespace(
            audit_dir=audit_dir, completion=SimpleNamespace(text=text),
            provider_admission_count=1, response_count=1,
        )

    monkeypatch.setattr(issue_pipeline, "audited_complete_chat", audited_fixture)

    def record_checkpoint(state, identity):
        validate_pipeline(state, identity=identity)
        control["snapshots"].append(copy.deepcopy(state))

    def run(run_id: str, *, parent: dict | None = None, binding: dict | None = None):
        identity = dict(SOURCE_IDENTITY, run_id=run_id)
        discovery = {
            "kind": "fintrace_annual_discovery",
            **{key: identity[key] for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256")},
            "status": "completed", "prompt_version": "annual_discovery_v4",
            "coverage": {"complete": True}, "findings": copy.deepcopy(findings),
            "evidences": copy.deepcopy(evidences),
        }
        pipeline = run_annual_issue_pipeline(
            discovery, identity=identity, parsed_pdf=parsed,
            settings=ModelSettings("http://127.0.0.1:1", "unused-constructed-key", "offline-mock-model"),
            run_dir=artifacts / "runs" / run_id, artifacts_root=artifacts,
            execution_binding=binding or _binding_v10(), parent_pipeline=parent,
            parent_run_id=parent["source_identity"]["run_id"] if parent is not None else None,
            checkpoint_callback=lambda state: record_checkpoint(state, identity),
        )
        validate_pipeline(pipeline, identity=identity)
        return pipeline

    return run, findings, sent, control


def test_v10_local_checkpoint_keeps_provisional_and_excluded_global_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    run, findings, _sent, control = _member_bounded_runtime_fixture(monkeypatch, tmp_path, 4)
    excluded = findings[0]["finding_id"]
    control["exclude_finding_id"] = excluded
    control["stop_after_group_calls"] = 1
    run("annual-analysis-member-bounded-local-checkpoint")
    first = next(
        state for state in control["snapshots"]
        if sum(shard["status"] == "accepted" for shard in state["shards"]) == 1
    )
    expected_live = sorted(row["finding_id"] for row in findings if row["finding_id"] != excluded)
    assert first["global_closure"]["closed"] is False
    assert first["global_closure"]["pending_finding_ids"] == expected_live
    assert first["global_closure"]["excluded_finding_ids"] == [excluded]
    assert {tuple(row["member_finding_ids"]) for row in first["issues"]} == {
        (findings[1]["finding_id"],), (findings[2]["finding_id"],),
    }
    assert first["global_closure"]["pending_issue_ids"] == sorted(row["issue_id"] for row in first["issues"])
    assert first["global_closure"]["input_issue_ids"] == first["global_closure"]["pending_issue_ids"]
    pending_member = next(row for row in first["member_decisions"] if row["finding_id"] == findings[3]["finding_id"])
    assert pending_member["disposition"] == "pending"


@pytest.mark.parametrize("count, expected_sizes", [(3, [3]), (4, [3, 1]), (13, [3, 3, 3, 3, 1])])
@pytest.mark.parametrize("exact_family", [False, True])
def test_v10_production_shards_preserve_complete_raw_members_and_quotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, count: int, expected_sizes: list[int], exact_family: bool,
) -> None:
    run, findings, sent, _control = _member_bounded_runtime_fixture(monkeypatch, tmp_path, count, exact_family=exact_family)
    pipeline = run("annual-analysis-member-bounded-parent")
    group_shards = [row for row in pipeline["shards"] if row["kind"] == "grouping"]
    assert [len(row["input_finding_ids"]) for row in group_shards] == expected_sizes
    member_ids = [member for row in group_shards for member in row["input_finding_ids"]]
    assert len(member_ids) == len(set(member_ids)) == count
    assert set(member_ids) == {finding["finding_id"] for finding in findings}
    candidates = {row["finding_id"]: row for row in pipeline["candidates"]}
    original = {row["finding_id"]: row for row in findings}
    for shard in group_shards:
        for group in shard["input"]["groups"]:
            assert group["representative_finding_id"] in group["member_finding_ids"]
        for candidate in shard["input"]["candidate_pool"]:
            assert candidate == candidates[candidate["finding_id"]]
            finding = original[candidate["finding_id"]]
            for field in ("title", "observation", "why_it_matters", "follow_up", "priority"):
                assert candidate[field] == finding[field]
            assert [(row["evidence_id"], row["quote"]) for row in candidate["citations"]] == [
                (row["evidence_id"], row["quote"]) for row in finding["citations"]
            ]
            assert candidate["citations"][0]["source_sha256"] == SOURCE_SHA
            assert candidate["citations"][0]["start_char"] == 0
            assert candidate["citations"][0]["bbox"] == {"x0": 0.0, "y0": 0.0, "x1": 100.0, "y1": 20.0}
    for shard, request in zip(pipeline["shards"], sent, strict=True):
        assert shard["request_body_sha256"] == hashlib.sha256(request["body"]).hexdigest()
        assert shard["semantic_input_sha256"] == hashlib.sha256(json.dumps(
            request["input"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        assert len(request["body"].decode("utf-8")) <= (22_000 if shard["kind"] == "grouping" else 96_000)
    if count > 3:
        global_input = next(row["input"] for row in sent if row["prompt_version"] == "annual_issue_global_v1")
        assert global_input["candidate_pool"] == sorted(pipeline["candidates"], key=lambda row: row["finding_id"])
        assert len(global_input["member_decisions"]) == count
        assert pipeline["global_closure"]["closure_method"] == "global_reconciliation"
    else:
        assert pipeline["global_closure"]["closure_method"] == "single_group_shard"
    assert pipeline["global_closure"]["closed"] is True
    assert len(pipeline["member_decisions"]) == count
    assert sum(len(row["member_finding_ids"]) for row in pipeline["issues"]) == count


def test_v10_same_identity_reuses_real_success_audits_without_calls_and_rejects_business_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    run, findings, sent, _control = _member_bounded_runtime_fixture(monkeypatch, tmp_path, 7, exact_family=True)
    parent = run("annual-analysis-member-bounded-reuse-parent")
    parent_bytes = copy.deepcopy(parent)
    sent.clear()
    child = run("annual-analysis-member-bounded-reuse-child", parent=parent)
    assert sent == []
    assert child["audit_summary"]["attempt_count"] == child["audit_summary"]["response_count"] == 0
    assert child["audit_summary"]["reused_count"] == len(parent["shards"])
    assert [row["shard_id"] for row in child["shards"]] == [row["shard_id"] for row in parent["shards"]]
    assert parent == parent_bytes

    findings[0]["follow_up"] = "业务核查路径已经改变，不能复用原请求。"
    changed = run("annual-analysis-member-bounded-business-change", parent=parent)
    assert sent
    changed_hashes = {row["semantic_input_sha256"] for row in changed["shards"] if not row["reused"]}
    assert changed_hashes - {row["semantic_input_sha256"] for row in parent["shards"]}
    assert parent == parent_bytes
    sent.clear()
    legacy_parent = copy.deepcopy(parent)
    legacy_parent["execution_binding"] = _binding_v9()
    with pytest.raises(ValueError, match="annual_issue_pipeline_parent_binding_mismatch"):
        run("annual-analysis-member-bounded-legacy-parent", parent=legacy_parent)
    assert sent == []


def test_v10_budget_exhaustion_marks_every_remaining_shard_pending_without_global_closure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    run, findings, sent, control = _member_bounded_runtime_fixture(monkeypatch, tmp_path, 10)
    control["stop_after_group_calls"] = 1
    pipeline = run("annual-analysis-member-bounded-pending")
    assert [len(row["input_finding_ids"]) for row in pipeline["shards"]] == [3, 3, 3, 1]
    assert [row["status"] for row in pipeline["shards"]] == ["accepted", "pending", "pending", "pending"]
    assert all(row["reason"] == "task_budget_exhausted" for row in pipeline["shards"][1:])
    assert len(pipeline["candidates"]) == len(pipeline["member_decisions"]) == len(findings)
    assert sum(row["disposition"] == "pending" for row in pipeline["member_decisions"]) == 7
    assert [row["prompt_version"] for row in sent] == ["annual_issue_grouping_v1"]
    assert pipeline["global_closure"]["closed"] is False
    assert pipeline["global_closure"]["no_priority_allowed"] is False
    assert pipeline["status"] == "partial"


def test_v9_archive_shards_and_recovery_plan_keep_twelve_member_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run, _findings, _sent, control = _member_bounded_runtime_fixture(monkeypatch, tmp_path, 12)
    control["stop_after_group_calls"] = 0
    checkpoint = run("annual-analysis-legacy-policy-reader")
    checkpoint["execution_binding"] = _binding_v9()
    root = Path(__file__).resolve().parents[2]
    prompt = (root / "config" / "prompts" / "annual_issue_grouping_v1.md").read_text(encoding="utf-8")
    planned = _build_group_shards(
        _local_exact_duplicate_groups(checkpoint["candidates"]), {}, checkpoint["source_identity"],
        "offline-mock-model", prompt, execution_binding=checkpoint["execution_binding"],
    )
    assert len(planned) == 1 and len(planned[0]["input"]["input_member_finding_ids"]) == 12
    checkpoint["shards"] = [issue_pipeline._pending_shard(
        f"group:000:{planned[0]['semantic_sha256'][:16]}", planned[0], "request_size_exceeded",
    )]
    checkpoint["audit_summary"] = issue_pipeline._audit_summary_from_shards(checkpoint["shards"])
    validate_pipeline(checkpoint, identity=checkpoint["source_identity"])
    assert _annual_issue_pipeline_has_executable_work(root, checkpoint) is False
    current = copy.deepcopy(checkpoint)
    current["execution_binding"] = _binding_v10()
    with pytest.raises(ValueError, match="annual_issue_pipeline_shard_member_invalid"):
        validate_pipeline(current, identity=current["source_identity"])


@pytest.mark.parametrize("mutation", ["duplicate_shard", "duplicate_group", "foreign_representative"])
def test_v10_reader_rejects_duplicate_or_foreign_member_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str,
) -> None:
    run, _findings, _sent, _control = _member_bounded_runtime_fixture(monkeypatch, tmp_path, 7, exact_family=True)
    pipeline = run("annual-analysis-member-bounded-repeated-group")
    if mutation == "duplicate_shard":
        repeated = copy.deepcopy(pipeline["shards"][0])
        repeated["shard_id"] = "group:999:" + repeated["semantic_input_sha256"][:16]
        pipeline["shards"].insert(1, repeated)
        pipeline["audit_summary"] = issue_pipeline._audit_summary_from_shards(pipeline["shards"])
    else:
        shard = pipeline["shards"][0]
        if mutation == "duplicate_group":
            shard["input"]["groups"].append(copy.deepcopy(shard["input"]["groups"][0]))
        else:
            shard["input"]["groups"][0]["representative_finding_id"] = pipeline["shards"][1]["input_finding_ids"][0]
        prompt = (Path(__file__).resolve().parents[2] / "config" / "prompts" / "annual_issue_grouping_v1.md").read_text(encoding="utf-8")
        shard["semantic_input_sha256"] = hashlib.sha256(json.dumps(
            shard["input"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        shard["request_body_sha256"] = _wire_hash("offline-mock-model", prompt, shard["input"])
        shard["input_members"] = copy.deepcopy(shard["input"]["groups"])
        shard["audit"].update(
            semantic_input_sha256=shard["semantic_input_sha256"], request_body_sha256=shard["request_body_sha256"],
        )
    with pytest.raises(ValueError, match="annual_issue_pipeline_shard_"):
        validate_pipeline(pipeline, identity=pipeline["source_identity"])
