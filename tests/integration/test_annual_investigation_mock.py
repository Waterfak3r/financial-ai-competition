from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")

import finagent.audit.audited_chat as audited_chat
import finagent.agents.annual_investigation as annual_investigation
from finagent.core.model_settings import ModelSettings
from finagent.retrieval.annual_context import AnnualContextResult, NarrativeSnippet, SignalContextResult

SOURCE_ID = "cninfo-mock"
SOURCE_SHA = "a" * 64
RUN_ID = "annual-investigation-mock-run"


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


PROFIT_CALC = "calc:annual_difference:net_profit_parent"
CASH_CALC = "calc:annual_difference:operating_cash_flow"
REVENUE_CALC = "calc:annual_difference:revenue"
PROFIT_INPUTS = ["cninfo-mock:profit:current", "cninfo-mock:profit:comparative"]
CASH_INPUTS = ["cninfo-mock:cash:current", "cninfo-mock:cash:comparative"]
REVENUE_INPUTS = ["cninfo-mock:revenue:current", "cninfo-mock:revenue:comparative"]
REPORT = {
    "kind": "fintrace_annual_analysis_report",
    "run_id": RUN_ID,
    "company_id": "603288",
    "report_year": 2024,
    "source_document_id": SOURCE_ID,
    "source_sha256": SOURCE_SHA,
    "pending_review": {
        "candidate_signals": [
            {
                "signal_id": "profit_up_cash_down",
                "status": "candidate",
                "left_difference": "10.0",
                "right_difference": "-2.0",
                "input_fact_ids": PROFIT_INPUTS + CASH_INPUTS,
                "calculation_ids": [PROFIT_CALC, CASH_CALC],
            },
            {
                "signal_id": "revenue_up_cash_down",
                "status": "candidate",
                "left_difference": "8.0",
                "right_difference": "-2.0",
                "input_fact_ids": REVENUE_INPUTS + CASH_INPUTS,
                "calculation_ids": [REVENUE_CALC, CASH_CALC],
            },
        ]
    },
    "confirmed": {
        "evaluation_label": "must not be sent to model",
        "analyses": [
            _analysis(PROFIT_CALC, PROFIT_INPUTS, "10.0"),
            _analysis(CASH_CALC, CASH_INPUTS, "-2.0"),
            _analysis(REVENUE_CALC, REVENUE_INPUTS, "8.0"),
        ]
    },
}
SOURCE_RECORD = {
    "document_id": SOURCE_ID,
    "sha256": SOURCE_SHA,
    "company_id": "603288",
    "report_period": "2024-12-31",
}


def _snippet(evidence_id: str, signal_id: str, page: int, text: str) -> NarrativeSnippet:
    return NarrativeSnippet(
        evidence_id=evidence_id,
        source_document_id=SOURCE_ID,
        source_sha256=SOURCE_SHA,
        signal_ids=(signal_id,),
        section_label="mda",
        pdf_page=page,
        bbox=(1.0, 2.0, 3.0, 4.0),
        raw_text=text,
        selection_reason="mock local retrieval",
    )


class _MockServer:
    def __init__(self, replies: list[tuple[int, Any]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._replies = replies
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.requests.append(request)
                status, content = owner._replies[len(owner.requests) - 1]
                if callable(content):
                    content = content(request)
                if status == 200:
                    payload = {
                        "id": f"mock-response-{len(owner.requests)}",
                        "model": "local-mock-model",
                        "choices": [{"message": {"role": "assistant", "content": content}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    }
                else:
                    payload = {"error": {"message": "local simulated failure"}}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
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

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _answer_from_request(request: dict[str, Any]) -> str:
    payload = json.loads(request["messages"][1]["content"])
    candidate = payload["candidate"]
    evidence = payload["narrative_evidence"][0]["evidence_id"]
    return json.dumps(
        {
            "signal_id": candidate["signal_id"],
            "explanation": "原因无法确认：当前片段没有披露背离形成原因。",
            "alternative_explanations": [],
            "limitations": ["仅检索了本年度报告中的有限片段。"],
            "narrative_evidence_ids": [evidence],
            "request_more_context": False,
        },
        ensure_ascii=False,
    )


def _install_retrieval(monkeypatch: pytest.MonkeyPatch, *, include_supplement: bool = False) -> list[int]:
    calls: list[int] = []
    initial = [
        _snippet("nctx-profit-p15", "profit_up_cash_down", 15, "经营活动现金流量净额下降。"),
        _snippet("nctx-revenue-p21", "revenue_up_cash_down", 21, "经营活动现金流入与流出列示。"),
    ]
    extra = _snippet("nctx-profit-p172", "profit_up_cash_down", 172, "将净利润调节为经营活动现金流量。")

    def retrieve(report: object, source_path: object, source_record: object, **kwargs: int) -> AnnualContextResult:
        calls.append(kwargs["max_snippets"])
        snippets = list(initial)
        if kwargs["max_snippets"] > 4 and include_supplement:
            snippets.append(extra)
        return AnnualContextResult(
            status="retrieved",
            source_document_id=SOURCE_ID,
            source_sha256=SOURCE_SHA,
            signal_results=(
                SignalContextResult("profit_up_cash_down", "retrieved", tuple(x.evidence_id for x in snippets if "profit_up_cash_down" in x.signal_ids)),
                SignalContextResult("revenue_up_cash_down", "retrieved", tuple(x.evidence_id for x in snippets if "revenue_up_cash_down" in x.signal_ids)),
            ),
            sections_found=("mda",),
            snippets=tuple(snippets),
        )

    monkeypatch.setattr(annual_investigation, "retrieve_annual_context", retrieve)
    return calls


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server: _MockServer,
    *,
    report: dict[str, Any] = REPORT,
    checkpoint_callback: Any | None = None,
    reusable_items: tuple[dict[str, Any], ...] = (),
    reusable_narrative_evidences: tuple[NarrativeSnippet, ...] = (),
) -> tuple[annual_investigation.AnnualInvestigationResult, Path]:
    for name in (
        "FINTRACE_MODEL_CALL_LEDGER_CONFIG",
        "FINTRACE_CALL_GATE_DB",
        "FINTRACE_CALL_GATE_KEY",
        "FINTRACE_CALL_CANCEL_FILE",
        "FINTRACE_CALL_LIVENESS_FILE",
        "FINTRACE_ACTIVE_SOURCE_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)
    runs_root = tmp_path / "artifacts" / "runs"
    run_dir = runs_root / RUN_ID
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(annual_investigation, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(audited_chat, "repository_runs_root", lambda: runs_root)
    settings = ModelSettings(
        base_url=server.base_url,
        api_key="test-local-secret",
        model="local-mock-model",
        timeout_seconds=3,
    )
    result = annual_investigation.run_annual_investigation(
        report,
        tmp_path / "source.pdf",
        SOURCE_RECORD,
        settings,
        run_dir=run_dir,
        reusable_items=reusable_items,
        reusable_narrative_evidences=reusable_narrative_evidences,
        checkpoint_callback=checkpoint_callback,
    )
    return result, run_dir


def test_resume_preserves_parent_supplement_citations_through_multiple_generations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_retrieval(monkeypatch)
    parent_snippet = _snippet(
        "nctx-profit-p172", "profit_up_cash_down", 172,
        "将净利润调节为经营活动现金流量。",
    )
    parent_item = annual_investigation.InvestigationItem(
        signal_id="profit_up_cash_down", status="interpretation",
        explanation="原因仍需核查。", alternative_explanations=(),
        limitations=("未核验模型观点。",),
        narrative_evidence_ids=(parent_snippet.evidence_id,),
    ).to_dict()
    checkpoints: list[dict[str, Any]] = []
    with _MockServer([(200, _answer_from_request)]) as server:
        first, _ = _run(
            monkeypatch, tmp_path / "child", server,
            reusable_items=(parent_item,),
            reusable_narrative_evidences=(parent_snippet,),
            checkpoint_callback=checkpoints.append,
        )
        payload = first.to_dict()
        assert payload["model_call_attempt_count"] == 1
        assert [item["signal_id"] for item in payload["items"]] == [
            "profit_up_cash_down", "revenue_up_cash_down",
        ]
        assert json.loads(server.requests[0]["messages"][1]["content"])["candidate"]["signal_id"] == "revenue_up_cash_down"
        for snapshot in [*checkpoints, payload]:
            pool = {item["evidence_id"]: item for item in snapshot["narrative_evidences"]}
            for item in snapshot["items"]:
                for evidence_id in item.get("narrative_evidence_ids", []):
                    assert item["signal_id"] in pool[evidence_id]["signal_ids"]
            assert pool[parent_snippet.evidence_id]["raw_text"] == parent_snippet.raw_text
        second, _ = _run(
            monkeypatch, tmp_path / "grandchild", server,
            reusable_items=tuple(payload["items"]),
            reusable_narrative_evidences=first.narrative_evidences,
        )
        assert second.model_call_attempt_count == 0
        assert second.to_dict()["items"] == payload["items"]
        assert second.to_dict()["narrative_evidences"] == payload["narrative_evidences"]
        assert len(server.requests) == 1


@pytest.mark.parametrize("mutation", ["missing", "wrong_source", "wrong_signal"])
def test_resume_rejects_unbound_parent_citation_before_provider_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    from dataclasses import replace

    _install_retrieval(monkeypatch)
    evidence = _snippet("nctx-profit-p172", "profit_up_cash_down", 172, "经营现金流说明。")
    item = annual_investigation.InvestigationItem(
        signal_id="profit_up_cash_down", status="interpretation",
        explanation="原因仍需核查。", alternative_explanations=(),
        limitations=("未核验模型观点。",),
        narrative_evidence_ids=(evidence.evidence_id,),
    ).to_dict()
    evidences = () if mutation == "missing" else (
        replace(evidence, source_sha256="b" * 64) if mutation == "wrong_source"
        else replace(evidence, signal_ids=("revenue_up_cash_down",)),
    )
    with _MockServer([]) as server:
        with pytest.raises(ValueError, match="annual_investigation_reusable_evidence_missing"):
            _run(
                monkeypatch, tmp_path, server,
                reusable_items=(item,), reusable_narrative_evidences=evidences,
            )
        assert server.requests == []


def test_graph_uses_local_http_and_audits_two_candidate_interpretations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval_calls = _install_retrieval(monkeypatch)
    with _MockServer([(200, _answer_from_request), (200, _answer_from_request)]) as server:
        result, run_dir = _run(monkeypatch, tmp_path, server)

    payload = result.to_dict()
    assert payload["run_id"] == RUN_ID
    assert payload["model_call_count"] == 2
    assert payload["model_call_attempt_count"] == 2
    assert [item["status"] for item in payload["items"]] == ["interpretation", "interpretation"]
    assert [item["explanation"] for item in payload["items"]] == [
        "原因无法确认：当前片段没有披露背离形成原因。",
        "原因无法确认：当前片段没有披露背离形成原因。",
    ]
    assert [item["narrative_evidence_ids"] for item in payload["items"]] == [
        ["nctx-profit-p15"],
        ["nctx-revenue-p21"],
    ]
    assert all(item["verification_status"] == "unverified" for item in payload["items"])
    assert payload["fraud_conclusion"] is None
    assert retrieval_calls == [4]
    assert len(server.requests) == 2
    for request in server.requests:
        user_payload = json.loads(request["messages"][1]["content"])
        assert set(user_payload) == {"document_binding", "candidate", "narrative_evidence"}
        assert "confirmed" not in request["messages"][1]["content"]
        assert "evaluation_label" not in request["messages"][1]["content"]
    for audit in payload["audit_artifacts"]:
        audit_dir = run_dir / Path(audit["audit_dir"]).name
        record = json.loads((audit_dir / "request.json").read_text(encoding="utf-8"))
        assert record["prompt_version"] == "annual_investigation_v1"
        assert (audit_dir / "request.json").is_file()
        assert (audit_dir / "response.json").is_file()
        assert "test-local-secret" not in (audit_dir / "request.json").read_text(encoding="utf-8")


def test_authentication_failure_stops_later_candidates_and_checkpoints_them_unattempted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_retrieval(monkeypatch)
    checkpoints: list[dict[str, object]] = []
    with _MockServer([(401, {"error": "unauthorized"})]) as server:
        result, _run_dir = _run(
            monkeypatch,
            tmp_path,
            server,
            checkpoint_callback=checkpoints.append,
        )

    payload = result.to_dict()
    assert len(server.requests) == 1
    assert payload["model_call_attempt_count"] == 1
    assert payload["model_call_count"] == 0
    assert payload["reason"] == "http_401"
    assert [item["signal_id"] for item in payload["items"]] == [
        "profit_up_cash_down",
        "revenue_up_cash_down",
    ]
    assert [item["reason"] for item in payload["items"]] == ["http_401", "http_401"]
    assert checkpoints
    assert [item["reason"] for item in checkpoints[-1]["items"]] == [
        "http_401",
        "http_401",
    ]


def test_graph_reserves_one_call_for_each_candidate_before_supplementary_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval_calls = _install_retrieval(monkeypatch, include_supplement=True)
    first = json.dumps(
        {
            "signal_id": "profit_up_cash_down",
            "explanation": "原因无法确认：现有片段没有披露形成原因。",
            "alternative_explanations": [],
            "limitations": ["仅检索了本年度报告中的有限片段。"],
            "narrative_evidence_ids": ["nctx-profit-p15"],
            "request_more_context": True,
        },
        ensure_ascii=False,
    )

    with _MockServer([(200, first), (200, _answer_from_request)]) as server:
        result, _ = _run(monkeypatch, tmp_path, server)

    items = result.to_dict()["items"]
    assert result.model_call_count == 2
    assert result.model_call_attempt_count == 2
    assert result.supplementary_retrieval_used is False
    assert retrieval_calls == [4]
    assert [item["status"] for item in items] == ["interpretation", "interpretation"]
    assert items[0]["narrative_evidence_ids"] == ["nctx-profit-p15"]
    assert any("预算已到上限" in item for item in items[0]["limitations"])
    assert items[1]["narrative_evidence_ids"] == ["nctx-revenue-p21"]
    assert [
        json.loads(request["messages"][1]["content"])["candidate"]["signal_id"]
        for request in server.requests
    ] == ["profit_up_cash_down", "revenue_up_cash_down"]


def test_graph_abstains_without_valid_citation_and_reserves_the_next_candidate_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval_calls = _install_retrieval(monkeypatch, include_supplement=True)
    uncited = json.dumps(
        {
            "signal_id": "profit_up_cash_down",
            "explanation": "原因无法确认。",
            "alternative_explanations": [],
            "limitations": ["当前回答没有可绑定的原文片段。"],
            "narrative_evidence_ids": [],
            "request_more_context": True,
        },
        ensure_ascii=False,
    )

    with _MockServer([(200, uncited), (200, _answer_from_request)]) as server:
        result, _ = _run(monkeypatch, tmp_path, server)

    payload = result.to_dict()
    items = payload["items"]
    assert payload["model_call_count"] == payload["model_call_attempt_count"] == 2
    assert retrieval_calls == [4]
    assert items[0]["status"] == "abstained"
    assert items[0]["reason"] == "model_call_budget_exhausted_before_supplement"
    assert items[0]["narrative_evidence_ids"] == []
    assert items[1]["status"] == "interpretation"
    assert items[1]["narrative_evidence_ids"] == ["nctx-revenue-p21"]


def test_graph_accepts_valid_same_source_citation_after_supplement_without_new_id_requirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval_calls = _install_retrieval(monkeypatch, include_supplement=True)
    first = json.dumps(
        {
            "signal_id": "profit_up_cash_down",
            "explanation": "现有片段不足以说明原因。",
            "alternative_explanations": [],
            "limitations": ["需要查看同一报告中的进一步解释。"],
            "narrative_evidence_ids": [],
            "request_more_context": True,
        },
        ensure_ascii=False,
    )
    second = json.dumps(
        {
            "signal_id": "profit_up_cash_down",
            "explanation": "原因无法确认：现金流片段未披露背离形成原因。",
            "alternative_explanations": [],
            "limitations": ["补充检索后仍只有有限的同源原文片段。"],
            "narrative_evidence_ids": ["nctx-profit-p15"],
            "request_more_context": False,
        },
        ensure_ascii=False,
    )
    report = dict(REPORT)
    report["pending_review"] = {
        "candidate_signals": [REPORT["pending_review"]["candidate_signals"][0]]
    }

    with _MockServer([(200, first), (200, second)]) as server:
        result, _ = _run(monkeypatch, tmp_path, server, report=report)

    payload = result.to_dict()
    assert payload["model_call_count"] == payload["model_call_attempt_count"] == 2
    assert payload["supplementary_retrieval_used"] is True
    assert retrieval_calls == [4, 8]
    assert len(server.requests) == 2
    supplemental_payload = json.loads(server.requests[1]["messages"][1]["content"])
    assert "nctx-profit-p172" in {
        evidence["evidence_id"] for evidence in supplemental_payload["narrative_evidence"]
    }
    assert payload["items"][0]["status"] == "interpretation"
    assert payload["items"][0]["narrative_evidence_ids"] == ["nctx-profit-p15"]
    assert payload["items"][0]["verification_status"] == "unverified"


def test_graph_stops_after_transport_retries_consume_actual_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_retrieval(monkeypatch)
    with _MockServer([(503, "ignored")] * 3) as server:
        result, run_dir = _run(monkeypatch, tmp_path, server)

    payload = result.to_dict()
    # Retried provider failures each consume the run's admission budget; none is a ChatCompletion.
    assert result.model_call_count == 0
    assert result.model_call_attempt_count == 3
    assert len(server.requests) == 3
    assert [item["status"] for item in payload["items"]] == ["abstained", "abstained"]
    failed = next(item for item in payload["audit_artifacts"] if item["status"] == "failed")
    audit_dir = run_dir / Path(failed["audit_dir"]).name
    failure = json.loads((audit_dir / "failure.json").read_text(encoding="utf-8"))
    assert failed["request_path"].endswith("request.json")
    assert failed["failure_path"].endswith("failure.json")
    assert failure["category"] == "http_5xx"
