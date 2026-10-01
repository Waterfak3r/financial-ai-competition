from __future__ import annotations

import contextlib
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

import finagent.agents.annual_investigation as annual_investigation
import finagent.agents.annual_review as annual_review
import finagent.audit.audited_chat as audited_chat
import finagent.reports.investigation_append as investigation_append
import finagent.api.annual_analysis_jobs as jobs_module
from finagent.api.annual_analysis_jobs import CliResult
from finagent.api.app import create_app
from finagent.retrieval.annual_context import (
    AnnualContextResult,
    NarrativeSnippet,
    SignalContextResult,
)
from scripts import analyze_annual as annual_cli


class _MockProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.requests.append(request)
                payload = json.loads(request["messages"][1]["content"])
                if "candidate" in payload:
                    candidate = payload["candidate"]
                    signal_id = candidate["signal_id"]
                    evidence_id = payload["narrative_evidence"][0]["evidence_id"]
                    answer = json.dumps({
                        "signal_id": signal_id,
                        "explanation": "现有片段未说明该背离形成原因。",
                        "alternative_explanations": [],
                        "limitations": ["仅使用本年报中的有限片段。"],
                        "narrative_evidence_ids": [evidence_id],
                        "request_more_context": False,
                    }, ensure_ascii=False)
                else:
                    signal_id = "profit_up_cash_down"
                    answer = json.dumps({
                        "assessment": "prioritize_review",
                        "summary": "建议优先复核候选线索及相关披露。",
                        "reasons": [{
                            "text": "候选线索仍需回到相关披露核对。",
                            "evidence_ids": [signal_id],
                        }],
                        "follow_up_items": [{
                            "object": signal_id,
                            "action": "核对相关披露及背离形成原因。",
                            "evidence_ids": [signal_id],
                        }],
                        "limitations": ["评审属于模型观点，未独立核验。"],
                    }, ensure_ascii=False)
                response = {
                    "id": f"mock-model-response-{len(owner.requests)}",
                    "model": "local-mock-model",
                    "choices": [{"message": {"role": "assistant", "content": answer}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                }
                raw = json.dumps(response).encode("utf-8")
                self.send_response(200)
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

    def __enter__(self) -> _MockProvider:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _source(project_root: Path) -> dict[str, Any]:
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Mock annual report text for model-review integration.")
    payload = pdf.tobytes()
    pdf.close()
    digest = hashlib.sha256(payload).hexdigest()
    relative = "603288/2024/model-review-test/annual.pdf"
    directory = project_root / "data" / "raw" / "603288" / "2024" / "model-review-test"
    directory.mkdir(parents=True)
    (directory / "annual.pdf").write_bytes(payload)
    (directory / "source.json").write_text(json.dumps({
        "company_id": "603288",
        "document_id": "model-review-test",
        "report_type": "年度报告",
        "report_period": "2024-12-31",
        "local_path": f"data/raw/{relative}",
        "sha256": digest,
    }), encoding="utf-8")
    return {
        "company_id": "603288",
        "report_year": 2024,
        "document_id": "model-review-test",
        "source_pdf_path": relative,
        "sha256": digest,
        "mode": "model_investigation",
    }


def _wait_terminal(client: TestClient, job_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/annual-analysis-jobs/{job_id}")
        assert response.status_code == 200
        state = response.json()
        if state["status"] in {"completed", "completed_with_issues", "failed", "interrupted"}:
            return state
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def _candidate_records(document_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    profit_inputs = [f"{document_id}:profit:current", f"{document_id}:profit:comparative"]
    cash_inputs = [f"{document_id}:cash:current", f"{document_id}:cash:comparative"]
    revenue_inputs = [f"{document_id}:revenue:current", f"{document_id}:revenue:comparative"]
    specifications = [
        ("calc:annual_difference:net_profit_parent", profit_inputs, "10.0"),
        ("calc:annual_difference:operating_cash_flow", cash_inputs, "-2.0"),
        ("calc:annual_difference:revenue", revenue_inputs, "8.0"),
    ]
    analyses = [{
        "calculation_id": calculation_id,
        "formula_id": "annual_difference",
        "formula_expression": "current - comparative",
        "input_fact_ids": inputs,
        "output_value": output,
        "unit": "元",
        "status": "succeeded",
        "failure_reason": None,
        "verifications": [],
        "placement_reasons": [],
        "limitations": [],
        "independent_verification": {
            "calculation_id": calculation_id,
            "status": "verified",
            "recomputed_value": output,
            "reason": None,
        },
    } for calculation_id, inputs, output in specifications]
    candidates = [
        {
            "signal_id": "profit_up_cash_down",
            "status": "candidate",
            "reason": None,
            "left_difference": "10.0",
            "right_difference": "-2.0",
            "input_fact_ids": profit_inputs + cash_inputs,
            "calculation_ids": [specifications[0][0], specifications[1][0]],
            "fraud_conclusion": None,
            "placement_reasons": ["候选异常只在待核查区列出，不写成舞弊结论。"],
        },
        {
            "signal_id": "revenue_up_cash_down",
            "status": "candidate",
            "reason": None,
            "left_difference": "8.0",
            "right_difference": "-2.0",
            "input_fact_ids": revenue_inputs + cash_inputs,
            "calculation_ids": [specifications[2][0], specifications[1][0]],
            "fraud_conclusion": None,
            "placement_reasons": ["候选异常只在待核查区列出，不写成舞弊结论。"],
        },
    ]
    return analyses, candidates


def _install_mock_analysis(monkeypatch: pytest.MonkeyPatch, source: dict[str, Any]) -> None:
    analyses, candidates = _candidate_records(source["document_id"])

    def analyze(source_pdf: Path, *, run_id: str, company_id: str, report_year: int, document_id: str) -> Any:
        digest = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
        report = {
            "kind": "fintrace_annual_analysis_report",
            "run_id": run_id,
            "company_id": company_id,
            "report_year": report_year,
            "source_document_id": document_id,
            "source_sha256": digest,
            "scope": {"note": "Mock deterministic input.", "gaps": []},
            "confirmed": {"metrics": [], "analyses": analyses},
            "comparability": {
                "status": "verified",
                "restatement_status": "unknown",
                "current_year": report_year,
                "comparative_year": report_year - 1,
                "document_id": document_id,
                "report_year": report_year,
                "source_sha256": digest,
                "checks": [],
                "conflicts": [],
                "limitations": ["集成测试构造的可比性记录。"],
                "evidence": [],
            },
            "verified_claims": [],
            "interpretations": [],
            "fraud_conclusion": None,
            "pending_review": {
                "facts": [], "calculations": [], "claims": [],
                "candidate_signals": candidates, "uncited_evidences": [],
            },
            "limitations": ["集成测试使用构造的确定性报告。"],
        }
        parsed = SimpleNamespace(
            source_sha256=digest,
            to_dict=lambda: {"source_sha256": digest, "text": "mock"},
        )
        return SimpleNamespace(
            parsed_pdf=parsed,
            report=report,
            markdown="# Mock annual report\n",
            status="completed",
            facts=[],
            evidences=[],
            fact_verifications=[],
            calculations=[],
            screening=candidates,
            archive_dict=lambda: {"model_called": False, "facts": [], "calculations": []},
        )

    monkeypatch.setattr(annual_cli, "analyze_annual_pdf", analyze)


def _install_mock_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    def retrieve(report: dict[str, Any], _source_path: Path, _source_record: dict[str, Any], **_kwargs: Any):
        document_id = report["source_document_id"]
        digest = report["source_sha256"]
        signal_ids = ("profit_up_cash_down", "revenue_up_cash_down")
        snippets = tuple(NarrativeSnippet(
            evidence_id=f"nctx-{signal_id}",
            source_document_id=document_id,
            source_sha256=digest,
            signal_ids=(signal_id,),
            section_label="mda",
            pdf_page=index + 1,
            bbox=(1.0, 2.0, 3.0, 4.0),
            raw_text="年报中的候选线索相关叙述片段。",
            selection_reason="local mock retrieval",
        ) for index, signal_id in enumerate(signal_ids))
        return AnnualContextResult(
            status="retrieved",
            source_document_id=document_id,
            source_sha256=digest,
            signal_results=tuple(
                SignalContextResult(signal_id, "retrieved", (f"nctx-{signal_id}",))
                for signal_id in signal_ids
            ),
            sections_found=("mda",),
            snippets=snippets,
        )

    monkeypatch.setattr(annual_investigation, "retrieve_annual_context", retrieve)


def test_model_job_runs_cli_and_final_review_then_returns_archived_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    runs_root = tmp_path / "artifacts" / "runs"
    monkeypatch.setattr(annual_investigation, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(annual_review, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(audited_chat, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(investigation_append, "_repository_root", lambda: tmp_path)
    _install_mock_analysis(monkeypatch, source)
    _install_mock_retrieval(monkeypatch)

    def invoke_cli(project_root: Path, artifacts_root: Path, request: dict[str, Any]) -> CliResult:
        pdf_path = project_root / "data" / "raw" / Path(*request["source_pdf_path"].split("/"))
        source_record = pdf_path.parent / "source.json"
        argv = [
            "--source-pdf", str(pdf_path),
            "--company-id", str(request["company_id"]),
            "--report-year", str(request["report_year"]),
            "--document-id", str(request["document_id"]),
            "--expected-sha256", str(request["sha256"]),
            "--artifacts-root", str(artifacts_root),
        ]
        assert request["mode"] == "model_investigation"
        argv.extend(["--with-model", "--source-record", str(source_record)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return_code = annual_cli.main(argv)
        return CliResult(return_code, stdout.getvalue(), stderr.getvalue())

    monkeypatch.setattr(jobs_module, "_invoke_cli", invoke_cli)

    with _MockProvider() as provider:
        monkeypatch.setenv("MODEL_BASE_URL", provider.base_url)
        monkeypatch.setenv("MODEL_API_KEY", "local-e2e-mock-key")
        monkeypatch.setenv("MODEL_NAME", "local-mock-model")
        client = TestClient(create_app(tmp_path))
        created = client.post("/v1/annual-analysis-jobs", json=source)
        assert created.status_code == 202, created.text
        state = _wait_terminal(client, created.json()["job_id"], timeout=10)
        assert state["status"] == "completed", json.dumps({"state": state, "requests": provider.requests}, ensure_ascii=False)
        assert len(provider.requests) == 3

        response = client.get(state["result_url"])
        assert response.status_code == 200, response.text
        archived = response.json()

    report = archived["report"]
    review = report["model_review"]
    investigation = report["model_investigation"]
    assert review["status"] == "completed"
    assert review["assessment"] == "prioritize_review"
    assert review["model_called"] is True
    assert review["call_count"] == 1
    assert review["source_identity"]["run_id"] == state["run_id"]
    assert report["model_called"] is True
    assert investigation["model_call_count"] == 3
    assert investigation["investigation_call_count"] == 2
    assert investigation["model_review_call_count"] == 1
    assert investigation["model_call_attempt_count"] == 3
    assert report["fraud_conclusion"] is None
    manifest_summary = archived["manifest"]
    run_manifest = json.loads(
        (tmp_path / "artifacts" / "runs" / state["run_id"] / "manifest.json").read_text(encoding="utf-8")
    )
    assert run_manifest["model_called"] is True
    assert run_manifest["model_investigation"]["model_call_count"] == 3
    assert run_manifest["model_review"]["call_count"] == 1

    task_id = f"model-review-job-e2e-{state['run_id']}"
    sample_dir = Path(__file__).resolve().parents[2] / "artifacts" / "tmp" / task_id
    sample_dir.mkdir(parents=True, exist_ok=False)
    (sample_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sample_dir / "manifest-summary.json").write_text(
        json.dumps(manifest_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sample_dir / "manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sample_dir / "README.txt").write_text(
        "本目录为自动生成的集成测试渲染样例；模型响应由本机 HTTP mock 构造，不是线上评审或真实公司结论。\n",
        encoding="utf-8",
    )
