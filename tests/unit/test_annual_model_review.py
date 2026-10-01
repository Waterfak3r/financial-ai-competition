from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import finagent.agents.annual_review as annual_review
import finagent.audit.audited_chat as audited_chat
from finagent.core.model_settings import ModelSettings

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
    with _MockServer([(200, _response())]) as server:
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


def test_no_candidate_can_receive_scoped_no_priority_only_with_complete_verified_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _report(candidate=False)
    investigation = _investigation(report)
    answer = _response(
        assessment="no_priority_issue_identified_within_scope",
        reference=FACT_1,
        summary="当前已核验覆盖范围内暂未发现需优先核查事项。",
    )
    with _MockServer([(200, answer)]) as server:
        review, _ = _run(monkeypatch, tmp_path, report, investigation, server)

    assert review["status"] == "completed"
    assert review["assessment"] == "no_priority_issue_identified_within_scope"
    assert json.loads(server.requests[0]["messages"][1]["content"])["coverage"]["no_priority_eligible"] is True


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
    with _MockServer([(503, "ignored")]) as server:
        review, _ = _run(monkeypatch, tmp_path, report, _investigation(report), server)

    assert len(server.requests) == 1
    assert review["status"] == "failed"
    assert review["reason"] == "model_review_call_failed"
    assert review["model_called"] is True
    assert review["call_count"] == review["call_attempt_count"] == 1
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
