from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

import finagent.reports.investigation_append as appendix
from finagent.core.model_settings import ModelConfigError, ModelSettings


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, dict[str, Any]]:
    repository = tmp_path / "repository"
    artifacts_root = repository / "artifacts"
    run_id = "annual-analysis-appendix-test"
    run_dir = artifacts_root / "runs" / run_id
    source_dir = repository / "data" / "raw" / "603288" / "2024" / "mock-document"
    source_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    original_pdf = source_dir / "source.pdf"
    archived_pdf = run_dir / "source.pdf"
    pdf_bytes = b"mock annual report pdf bytes"
    original_pdf.write_bytes(pdf_bytes)
    archived_pdf.write_bytes(pdf_bytes)
    import hashlib

    source_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    source_record_path = source_dir / "source.json"
    source_record = {
        "document_id": "mock-document",
        "company_id": "603288",
        "report_period": "2024-12-31",
        "sha256": source_sha256,
        "local_path": "data/raw/603288/2024/mock-document/source.pdf",
    }
    source_record_path.write_text(json.dumps(source_record), encoding="utf-8")
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": run_id,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": "mock-document",
        "source_sha256": source_sha256,
        "scope": {"note": "确定性检查。报告不调用模型。", "gaps": []},
        "confirmed": {"metrics": [{"fact_id": "fact-1"}], "analyses": [{"calculation_id": "calc-1"}]},
    }
    return repository, artifacts_root, run_dir, original_pdf, source_record_path, report


def _prepare_root(monkeypatch: pytest.MonkeyPatch, repository: Path) -> None:
    monkeypatch.setattr(appendix, "_repository_root", lambda: repository)
    monkeypatch.setattr(appendix, "_invoke_review", _successful_review)


def _successful_review(report: dict[str, Any], _investigation: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "reason": None,
        "assessment": "insufficient_evidence",
        "summary": "现有材料不足以形成优先核查判断。",
        "reasons": [],
        "follow_up_items": [],
        "limitations": ["评审为模型观点，未独立核验。"],
        "model_called": True,
        "call_count": 1,
        "call_attempt_count": 1,
        "audit_artifacts": [{
            "audit_dir": "runs/mock-review",
            "request_path": "runs/mock-review/request.json",
            "response_path": "runs/mock-review/response.json",
            "failure_path": None,
            "status": "succeeded",
            "prompt_version": "annual_review_v1",
        }],
        "source_identity": {
            "run_id": report["run_id"],
            "company_id": report["company_id"],
            "report_year": report["report_year"],
            "source_document_id": report["source_document_id"],
            "source_sha256": report["source_sha256"],
        },
    }


def _settings() -> ModelSettings:
    return ModelSettings("https://example.invalid/v1", "local-test-secret", "test-model", 1)


def _successful_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "fintrace_annual_investigation",
        "run_id": report["run_id"],
        "company_id": report["company_id"],
        "report_year": report["report_year"],
        "source_document_id": report["source_document_id"],
        "source_sha256": report["source_sha256"],
        "status": "completed",
        "model_called": True,
        "model_call_count": 1,
        "model_call_attempt_count": 1,
        "supplementary_retrieval_used": False,
        "items": [
            {
                "signal_id": "profit_up_cash_down",
                "status": "interpretation",
                "explanation": "原因仍需核查。",
                "alternative_explanations": [],
                "limitations": ["只使用本年度报告片段。"],
                "narrative_evidence_ids": ["nctx-1"],
                "claim_type": "interpretation",
                "verification_status": "unverified",
            }
        ],
        "audit_artifacts": [
            {
                "audit_dir": "artifacts/runs/run/uuid",
                "request_path": "artifacts/runs/run/uuid/request.json",
                "response_path": "artifacts/runs/run/uuid/response.json",
                "failure_path": None,
                "status": "succeeded",
                "prompt_version": "annual_investigation_v1",
            }
        ],
        "fraud_conclusion": None,
    }


def _run_appendix(
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Path, Path, Path, Path, Path, dict[str, Any]],
) -> dict[str, Any]:
    repository, artifacts_root, run_dir, original_pdf, record_path, report = fixture
    archived_pdf = run_dir / "source.pdf"
    _prepare_root(monkeypatch, repository)
    monkeypatch.setattr(appendix, "_ensure_langgraph_available", lambda: None)
    monkeypatch.setattr(appendix, "load_model_settings", _settings)
    monkeypatch.setattr(
        appendix,
        "_invoke_agent",
        lambda report, **kwargs: _FakeResult(_successful_payload(report)),
    )
    return appendix.run_annual_investigation_appendix(
        report,
        archived_pdf_path=archived_pdf,
        original_pdf_path=original_pdf,
        source_record_path=record_path,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )


class _FakeResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def test_optional_investigation_binds_source_and_preserves_unverified_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _fixture(tmp_path)
    investigation = _run_appendix(monkeypatch, fixture)

    assert investigation["status"] == "completed"
    assert investigation["model_called"] is True
    assert investigation["investigation_call_count"] == 1
    assert investigation["model_review_call_count"] == 1
    assert investigation["model_call_count"] == investigation["model_call_attempt_count"] == 2
    assert investigation["source_binding"]["document_id"] == "mock-document"
    assert investigation["source_binding"]["company_id"] == "603288"
    assert investigation["source_binding"]["report_period"] == "2024-12-31"
    assert investigation["source_binding"]["source_sha256"] == fixture[-1]["source_sha256"]
    assert investigation["items"][0]["verification_status"] == "unverified"
    assert "local-test-secret" not in json.dumps(investigation)


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    (
        ("document_id", "other-document", "source_record_identity_mismatch"),
        ("company_id", "600000", "source_record_identity_mismatch"),
        ("report_period", "2024-01-01", "source_record_identity_mismatch"),
        ("sha256", "0" * 64, "source_record_identity_mismatch"),
        ("local_path", "data/raw/603288/2024/other.pdf", "source_record_pdf_path_mismatch"),
    ),
)
def test_source_record_mismatch_fails_before_reading_model_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
    expected_reason: str,
) -> None:
    repository, artifacts_root, run_dir, original_pdf, record_path, report = _fixture(tmp_path)
    _prepare_root(monkeypatch, repository)
    source_record = json.loads(record_path.read_text(encoding="utf-8"))
    source_record[field] = value
    record_path.write_text(json.dumps(source_record), encoding="utf-8")
    monkeypatch.setattr(
        appendix,
        "load_model_settings",
        lambda: pytest.fail("模型配置不应在来源绑定失败前读取。"),
    )

    investigation = appendix.run_annual_investigation_appendix(
        report,
        archived_pdf_path=run_dir / "source.pdf",
        original_pdf_path=original_pdf,
        source_record_path=record_path,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )

    assert investigation["status"] == "failed"
    assert investigation["reason"] == expected_reason
    assert investigation["model_called"] is False


def test_missing_model_configuration_is_an_abstention_without_a_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, artifacts_root, run_dir, original_pdf, record_path, report = _fixture(tmp_path)
    _prepare_root(monkeypatch, repository)
    monkeypatch.setattr(appendix, "_ensure_langgraph_available", lambda: None)

    def no_settings() -> ModelSettings:
        raise ModelConfigError("缺少模型配置：MODEL_API_KEY")

    monkeypatch.setattr(appendix, "load_model_settings", no_settings)
    investigation = appendix.run_annual_investigation_appendix(
        report,
        archived_pdf_path=run_dir / "source.pdf",
        original_pdf_path=original_pdf,
        source_record_path=record_path,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )

    assert investigation["status"] == "abstained"
    assert investigation["reason"] == "model_configuration_unavailable"
    assert investigation["model_called"] is False
    assert investigation["model_call_count"] == 0
    assert "MODEL_API_KEY" in investigation["reason_detail"]
    assert "local-test-secret" not in json.dumps(investigation)


def test_langgraph_unavailable_is_failed_without_claiming_a_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, artifacts_root, run_dir, original_pdf, record_path, report = _fixture(tmp_path)
    _prepare_root(monkeypatch, repository)

    def unavailable() -> None:
        raise appendix._OptionalDependencyUnavailable

    monkeypatch.setattr(appendix, "_ensure_langgraph_available", unavailable)
    monkeypatch.setattr(
        appendix,
        "load_model_settings",
        lambda: pytest.fail("模型配置不应在 LangGraph 缺失时读取。"),
    )
    investigation = appendix.run_annual_investigation_appendix(
        report,
        archived_pdf_path=run_dir / "source.pdf",
        original_pdf_path=original_pdf,
        source_record_path=record_path,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )

    assert investigation["status"] == "failed"
    assert investigation["reason"] == "langgraph_unavailable"
    assert investigation["model_called"] is False
    assert "需要可选依赖" in investigation["reason_detail"]


def test_http_failure_audit_counts_persisted_request_as_actual_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, artifacts_root, run_dir, original_pdf, record_path, report = _fixture(tmp_path)
    _prepare_root(monkeypatch, repository)
    monkeypatch.setattr(appendix, "_ensure_langgraph_available", lambda: None)
    monkeypatch.setattr(appendix, "load_model_settings", _settings)

    def request_failed(*args: object, **kwargs: object) -> Any:
        audit_dir = run_dir / str(uuid.uuid4())
        audit_dir.mkdir()
        (audit_dir / "request.json").write_text('{"status":"started"}', encoding="utf-8")
        (audit_dir / "failure.json").write_text('{"category":"http_error"}', encoding="utf-8")
        raise RuntimeError("network exception may include sensitive details")

    monkeypatch.setattr(appendix, "_invoke_agent", request_failed)
    investigation = appendix.run_annual_investigation_appendix(
        report,
        archived_pdf_path=run_dir / "source.pdf",
        original_pdf_path=original_pdf,
        source_record_path=record_path,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )

    assert investigation["status"] == "failed"
    assert investigation["reason"] == "annual_investigation_failed"
    assert investigation["model_called"] is True
    assert investigation["investigation_call_count"] == 1
    assert investigation["model_review_call_count"] == 1
    assert investigation["model_call_count"] == investigation["model_call_attempt_count"] == 2
    assert investigation["audit_artifacts"][0]["status"] == "failed"
    assert "sensitive details" not in json.dumps(investigation)


def test_pre_request_failure_is_not_reported_as_an_actual_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, artifacts_root, run_dir, original_pdf, record_path, report = _fixture(tmp_path)
    _prepare_root(monkeypatch, repository)
    monkeypatch.setattr(appendix, "_ensure_langgraph_available", lambda: None)
    monkeypatch.setattr(appendix, "load_model_settings", _settings)
    payload = _successful_payload(report)
    payload.update(
        status="abstained",
        model_called=False,
        model_call_count=0,
        model_call_attempt_count=1,
        items=[],
        audit_artifacts=[],
        reason="prompt_build_failed",
    )
    monkeypatch.setattr(
        appendix,
        "_invoke_agent",
        lambda report, **kwargs: _FakeResult(payload),
    )

    investigation = appendix.run_annual_investigation_appendix(
        report,
        archived_pdf_path=run_dir / "source.pdf",
        original_pdf_path=original_pdf,
        source_record_path=record_path,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )

    assert investigation["status"] == "abstained"
    assert investigation["investigation_call_attempt_count"] == 1
    assert investigation["model_call_attempt_count"] == 2
    assert investigation["investigation_call_count"] == 0
    assert investigation["model_review_call_count"] == 1
    assert investigation["model_call_count"] == 1
    assert investigation["model_called"] is True


def test_report_appendix_updates_scope_and_model_flag_without_moving_verified_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _fixture(tmp_path)
    investigation = _run_appendix(monkeypatch, fixture)
    report = dict(fixture[-1])
    report.update(
        confirmed={"metrics": [], "analyses": []},
        verified_claims=[],
        interpretations=[],
        comparability=None,
        limitations=[],
        pending_review={
            "facts": [],
            "calculations": [],
            "claims": [],
            "candidate_signals": [],
            "uncited_evidences": [],
        },
    )
    analysis = {"model_called": False, "facts": [{"fact_id": "fact-1"}]}
    original_confirmed = report["confirmed"]

    updated_report, updated_analysis = appendix.attach_investigation_to_report(
        report,
        analysis,
        investigation,
        investigation_path="runs/run/investigation.json",
    )
    markdown = appendix.render_report_with_investigation(updated_report, investigation)

    assert updated_report["confirmed"] == original_confirmed
    assert updated_report["model_called"] is True
    assert updated_analysis["model_called"] is True
    assert updated_analysis["scope"]["note"] == updated_report["scope"]["note"]
    assert "报告不调用模型" not in updated_report["scope"]["note"]
    assert updated_report["model_investigation"]["status"] == "completed"
    assert updated_report["model_review"]["status"] == "completed"
    assert updated_analysis["model_review"]["assessment"] == "insufficient_evidence"
    assert "M3 年报文本调查" in markdown
    assert "未核实模型解释" in markdown
    assert "response.json" in markdown
