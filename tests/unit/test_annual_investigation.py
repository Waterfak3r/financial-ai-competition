from __future__ import annotations

import json

import pytest

from finagent.agents.annual_investigation import (
    AnnualInvestigationResult,
    AuditArtifact,
    InvestigationItem,
    _build_messages,
    _call_model_node,
    _candidate_signals,
    _merge_bounded_snippets,
    _require_report_identity,
    _safe_candidate_for_prompt,
    _select_candidate_node,
    _after_select_candidate,
    _result_from_state,
    _same_source_snippets,
    parse_model_interpretation,
)
from finagent.retrieval.annual_context import NarrativeSnippet


SOURCE_ID = "cninfo-test"
SOURCE_SHA = "a" * 64


def _analysis(calculation_id: str, input_fact_ids: list[str], output_value: str) -> dict[str, object]:
    return {
        "calculation_id": calculation_id,
        "formula_id": "annual_difference",
        "input_fact_ids": input_fact_ids,
        "output_value": output_value,
        "status": "succeeded",
        "failure_reason": None,
        "independent_verification": {
            "calculation_id": calculation_id,
            "status": "verified",
            "recomputed_value": output_value,
        },
    }


def _report_with_verified_profit_candidate() -> dict[str, object]:
    profit_id = "calc:annual_difference:net_profit_parent"
    cash_id = "calc:annual_difference:operating_cash_flow"
    profit_inputs = ["cninfo-test:profit:current", "cninfo-test:profit:comparative"]
    cash_inputs = ["cninfo-test:cash:current", "cninfo-test:cash:comparative"]
    return {
        "kind": "fintrace_annual_analysis_report",
        "source_document_id": SOURCE_ID,
        "pending_review": {
            "candidate_signals": [
                {
                    "signal_id": "profit_up_cash_down",
                    "status": "candidate",
                    "left_difference": "12.5",
                    "right_difference": "-2.5",
                    "calculation_ids": [profit_id, cash_id],
                    "input_fact_ids": profit_inputs + cash_inputs,
                }
            ]
        },
        "confirmed": {
            "analyses": [
                _analysis(profit_id, profit_inputs, "12.5"),
                _analysis(cash_id, cash_inputs, "-2.5"),
            ]
        },
    }


def test_parse_model_interpretation_requires_exact_schema_and_valid_fields() -> None:
    text = json.dumps(
        {
            "signal_id": "profit_up_cash_down",
            "explanation": "原因无法确认，当前片段没有披露背离形成原因。",
            "alternative_explanations": [],
            "limitations": ["现有原文片段不包含因果说明。"],
            "narrative_evidence_ids": ["nctx-test-p15"],
            "request_more_context": False,
        },
        ensure_ascii=False,
    )

    parsed = parse_model_interpretation(text, expected_signal_id="profit_up_cash_down")

    assert parsed.signal_id == "profit_up_cash_down"
    assert parsed.narrative_evidence_ids == ("nctx-test-p15",)
    assert parsed.request_more_context is False


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "verified"},
        {"signal_id": "revenue_up_cash_down"},
        {"explanation": "已核实存在舞弊。"},
        {"request_more_context": "false"},
    ],
)
def test_parse_model_interpretation_rejects_untrusted_or_malformed_output(change: dict[str, object]) -> None:
    value: dict[str, object] = {
        "signal_id": "profit_up_cash_down",
        "explanation": "原因无法确认。",
        "alternative_explanations": [],
        "limitations": ["缺少原因说明。"],
        "narrative_evidence_ids": [],
        "request_more_context": False,
    }
    if "extra" in change:
        value[str(change["extra"])] = "untrusted"
    else:
        value.update(change)

    with pytest.raises(ValueError):
        parse_model_interpretation(
            json.dumps(value, ensure_ascii=False),
            expected_signal_id="profit_up_cash_down",
        )


def test_candidate_filter_only_keeps_supported_candidate_fields() -> None:
    report = _report_with_verified_profit_candidate()
    signal = report["pending_review"]["candidate_signals"][0]
    signal["placement_reasons"] = ["private metadata"]
    signal["fraud_conclusion"] = "must not be sent"
    report["pending_review"]["candidate_signals"].extend(
        [
            {"signal_id": "unsupported_signal", "status": "candidate"},
            {"signal_id": "revenue_up_cash_down", "status": "verified"},
        ]
    )
    report["confirmed"]["evaluation_label"] = "must never be sent"

    candidates = _candidate_signals(report)

    assert len(candidates) == 1
    assert candidates[0] == {
        "signal_id": "profit_up_cash_down",
        "status": "candidate",
        "left_difference": "12.5",
        "right_difference": "-2.5",
        "calculation_ids": [
            "calc:annual_difference:net_profit_parent",
            "calc:annual_difference:operating_cash_flow",
        ],
        "input_fact_ids": [
            "cninfo-test:profit:current",
            "cninfo-test:profit:comparative",
            "cninfo-test:cash:current",
            "cninfo-test:cash:comparative",
        ],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("left_difference", "999.0"),
        ("calculation_ids", ["calc:forged", "calc:annual_difference:operating_cash_flow"]),
        ("input_fact_ids", ["fact:forged"]),
    ],
)
def test_candidate_is_abstained_when_verified_calculation_binding_does_not_match(
    field: str,
    value: object,
) -> None:
    report = _report_with_verified_profit_candidate()
    candidate = report["pending_review"]["candidate_signals"][0]
    candidate[field] = value

    candidates = _candidate_signals(report)

    assert len(candidates) == 1
    assert candidates[0]["prevalidation_error"] == "candidate_calculation_binding_failed"
    assert "left_difference" not in candidates[0]
    assert "calculation_ids" not in candidates[0]


def test_candidate_binding_failure_routes_to_abstention_without_model_call() -> None:
    report = _report_with_verified_profit_candidate()
    report["pending_review"]["candidate_signals"][0]["right_difference"] = "forged"
    candidate = _candidate_signals(report)[0]
    state = {
        "candidate_signals": [candidate],
        "current_index": 0,
        "context_snippets": [
            NarrativeSnippet(
                "nctx-test-p15", SOURCE_ID, SOURCE_SHA, ("profit_up_cash_down",), "mda", 15,
                (0.0, 0.0, 1.0, 1.0), "snippet", "test",
            )
        ],
        "model_call_count": 0,
        "model_call_attempt_count": 0,
    }

    selected = _select_candidate_node(state)  # type: ignore[arg-type]
    routed = _after_select_candidate({**state, **selected})  # type: ignore[arg-type]

    assert selected["pending_reason"] == "candidate_calculation_binding_failed"
    assert routed == "advance"


@pytest.mark.parametrize("signal_id", [["profit_up_cash_down"], {"id": "profit_up_cash_down"}])
def test_malformed_signal_id_types_are_ignored_without_hash_errors(signal_id: object) -> None:
    report = _report_with_verified_profit_candidate()
    report["pending_review"]["candidate_signals"].append(
        {"signal_id": signal_id, "status": "candidate"}
    )

    candidates = _candidate_signals(report)

    assert [item["signal_id"] for item in candidates] == ["profit_up_cash_down"]
    with pytest.raises(ValueError, match="unsupported candidate"):
        _safe_candidate_for_prompt({"signal_id": signal_id, "status": "candidate"})


def test_only_formal_annual_reports_can_be_investigated() -> None:
    with pytest.raises(ValueError, match="正式 FINTRACE 年度分析报告"):
        _require_report_identity(
            {"kind": "untrusted", "run_id": "run-test"},
            {"document_id": SOURCE_ID},
        )


def test_prompt_contains_only_bound_candidate_fields_and_short_same_source_snippets() -> None:
    snippet = NarrativeSnippet(
        evidence_id="nctx-test-p15",
        source_document_id=SOURCE_ID,
        source_sha256=SOURCE_SHA,
        signal_ids=("profit_up_cash_down",),
        section_label="mda",
        pdf_page=15,
        bbox=(0.0, 0.0, 20.0, 10.0),
        raw_text="经营活动现金流量净额有所下降。",
        selection_reason="local retrieval",
    )
    state = {
        "run_id": "run-test",
        "company_id": "603288",
        "report_year": 2024,
        "report_period": "2024-12-31",
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "prompt_text": "system prompt",
    }
    candidate = {
        "signal_id": "profit_up_cash_down",
        "status": "candidate",
        "left_difference": "12.5",
        "right_difference": "-2.5",
        "evaluation_label": "should not be sent",
    }

    messages, pages = _build_messages(state, candidate, [snippet])  # type: ignore[arg-type]
    payload = json.loads(messages[1].content)

    assert pages == (15,)
    assert set(payload) == {"document_binding", "candidate", "narrative_evidence"}
    assert payload["document_binding"] == {
        "run_id": "run-test",
        "company_id": "603288",
        "report_year": 2024,
        "report_period": "2024-12-31",
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
    }
    assert "evaluation_label" not in messages[1].content
    assert "placement_reasons" not in messages[1].content
    assert payload["narrative_evidence"][0]["evidence_id"] == "nctx-test-p15"


def test_same_source_context_filter_and_merge_obey_hard_budget() -> None:
    valid = NarrativeSnippet(
        "nctx-valid", SOURCE_ID, SOURCE_SHA, ("profit_up_cash_down",), "mda", 15,
        (0.0, 0.0, 1.0, 1.0), "x" * 1100, "reason",
    )
    wrong_hash = NarrativeSnippet(
        "nctx-wrong", SOURCE_ID, "b" * 64, ("profit_up_cash_down",), "mda", 16,
        (0.0, 0.0, 1.0, 1.0), "y", "reason",
    )
    selected = _same_source_snippets([valid, wrong_hash], SOURCE_ID, SOURCE_SHA)
    added = NarrativeSnippet(
        "nctx-added", SOURCE_ID, SOURCE_SHA, ("profit_up_cash_down",), "financial_notes", 172,
        (0.0, 0.0, 1.0, 1.0), "z" * 1500, "reason",
    )

    merged = _merge_bounded_snippets(selected, [added])

    assert [item.evidence_id for item in selected] == ["nctx-valid"]
    assert [item.evidence_id for item in merged] == ["nctx-valid", "nctx-added"]
    assert sum(len(item.raw_text) for item in merged) <= 4000


def test_result_to_dict_is_json_serializable_and_never_verified() -> None:
    result = AnnualInvestigationResult(
        run_id="run-test",
        company_id="603288",
        report_year=2024,
        report_period="2024-12-31",
        source_document_id=SOURCE_ID,
        source_sha256=SOURCE_SHA,
        status="completed",
        prompt_version="annual_investigation_v1",
        model_call_count=1,
        model_call_attempt_count=1,
        supplementary_retrieval_used=False,
        items=(
            InvestigationItem(
                signal_id="profit_up_cash_down",
                status="interpretation",
                explanation="原因无法确认。",
                alternative_explanations=(),
                limitations=("缺少解释性披露。",),
                narrative_evidence_ids=("nctx-test-p15",),
            ),
        ),
        audit_artifacts=(
            AuditArtifact(
                "artifacts/runs/run-test/audit-id",
                "artifacts/runs/run-test/audit-id/request.json",
                "artifacts/runs/run-test/audit-id/response.json",
                None,
                "succeeded",
                "annual_investigation_v1",
            ),
        ),
    )

    encoded = json.dumps(result.to_dict(), ensure_ascii=False)
    decoded = json.loads(encoded)

    assert decoded["items"][0]["claim_type"] == "interpretation"
    assert decoded["items"][0]["verification_status"] == "unverified"
    assert decoded["fraud_conclusion"] is None
    assert decoded["model_call_attempt_count"] == 1
    assert decoded["audit_artifacts"][0]["response_path"].endswith("response.json")


def test_request_persistence_failure_does_not_claim_model_was_called(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from finagent.agents import annual_investigation
    from finagent.core.model_settings import ModelSettings

    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    snippet = NarrativeSnippet(
        "nctx-test-p15", SOURCE_ID, SOURCE_SHA, ("profit_up_cash_down",), "mda", 15,
        (0.0, 0.0, 1.0, 1.0), "经营活动现金流量净额下降。", "test",
    )

    def fail_before_request(*args, **kwargs):
        raise RuntimeError("request could not be written")

    monkeypatch.setattr(annual_investigation, "audited_complete_chat", fail_before_request)
    state = {
        "run_id": "run-test",
        "company_id": "603288",
        "report_year": 2024,
        "report_period": "2024-12-31",
        "source_document_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "prompt_text": "system",
        "run_dir": str(run_dir),
        "settings": ModelSettings("http://127.0.0.1:1/v1", "secret", "mock"),
        "current_candidate": _candidate_signals(_report_with_verified_profit_candidate())[0],
        "current_snippets": [snippet],
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "audit_artifacts": [],
    }

    update = _call_model_node(state)  # type: ignore[arg-type]
    result = _result_from_state({**state, **update})

    assert update["model_call_attempt_count"] == 1
    assert update["model_call_count"] == 0
    assert result.to_dict()["model_called"] is False
    assert result.to_dict()["audit_artifacts"][0]["request_path"] is None
