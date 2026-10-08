from __future__ import annotations

import json
import copy
import uuid
from pathlib import Path
from typing import Any

import pytest

import finagent.reports.investigation_append as appendix
from finagent.core import annual_recovery
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


def test_invoke_agent_passes_existing_parsed_pdf_to_retrieval_graph(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    parsed_pdf = object()
    captured: dict[str, Any] = {}

    def run_annual_investigation(*_args: Any, **kwargs: Any) -> _FakeResult:
        captured.update(kwargs)
        return _FakeResult({})

    monkeypatch.setattr(
        appendix.importlib,
        "import_module",
        lambda _name: SimpleNamespace(run_annual_investigation=run_annual_investigation),
    )

    appendix._invoke_agent(
        {},
        archived_pdf_path=tmp_path / "source.pdf",
        source_record={},
        settings=_settings(),
        run_dir=tmp_path,
        parsed_pdf=parsed_pdf,
    )

    assert captured["parsed_pdf"] is parsed_pdf


def _review_reuse_fixture(
    tmp_path: Path,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    *_paths, base_report = _fixture(tmp_path)

    parent_report = copy.deepcopy(base_report)
    parent_report["run_id"] = "annual-analysis-review-parent"
    parent_identity = appendix._report_identity(parent_report)
    sha = parent_identity["source_sha256"]
    parent_report["confirmed"] = {
        "metrics": [
            {
                "fact_id": "fact-revenue-2024",
                "company_id": parent_identity["company_id"],
                "source_document_id": parent_identity["source_document_id"],
                "source_sha256": sha,
                "report_year": parent_identity["report_year"],
                "metric_id": "revenue",
                "label_raw": "营业收入",
                "normalized_value": "123.45",
                "unit": "亿元",
                "currency": "CNY",
                "period_start": "2024-01-01",
                "period_end": "2024-12-31",
                "period_type": "annual",
                "comparison_role": "current",
                "statement_type": "consolidated",
                "scope": "consolidated",
                "verifications": [
                    {
                        "status": "verified",
                        "target_type": "financial_fact",
                        "target_id": "fact-revenue-2024",
                        "evidence_ids": ["ev-revenue-1"],
                        "verified_at": "2026-10-06T08:00:00Z",
                    }
                ],
                "verification_evidences": [
                    {
                        "evidence_id": "ev-revenue-1",
                        "document_id": parent_identity["source_document_id"],
                        "source_sha256": sha,
                        "pdf_page": 18,
                    }
                ],
            }
        ],
        "analyses": [],
    }
    current_report = copy.deepcopy(parent_report)
    current_report["run_id"] = "annual-analysis-review-child"
    current_report["confirmed"]["metrics"][0]["verifications"][0]["verified_at"] = (
        "2026-10-07T02:30:00Z"
    )

    binding = {
        "source_sha256": sha,
        "company_id": parent_identity["company_id"],
        "report_year": parent_identity["report_year"],
        "source_document_id": parent_identity["source_document_id"],
        "analysis_mode": "model_investigation",
        "provider_model_fingerprint": "a" * 64,
        "model": "test-model",
        "prompts_sha256": "b" * 64,
        "parser_version": "annual-discovery-parser-v2",
        "plan_sha256": "c" * 64,
        "m3_binding_sha256": None,
    }

    def make_discovery(identity: dict[str, Any]) -> dict[str, Any]:
        finding_id = f"disc:{sha[:12]}-1"
        return {
            "kind": "fintrace_annual_discovery",
            **identity,
            "status": "completed",
            "coverage": {
                "complete": True,
                "truncated": False,
                "partial_batches": 0,
                "planned_batches": 1,
                "processed_batches": 1,
                "successful_batches": 1,
                "failed_batches": 0,
            },
            "findings": [
                {
                    "finding_id": finding_id,
                    "classification": "unverified_model_finding",
                    "verification_status": "unverified",
                    "title": "收入变化",
                    "observation": "收入增长但经营现金流下降。",
                    "why_it_matters": "需核对回款情况。",
                    "follow_up": "核对主要客户回款。",
                    "priority": "medium",
                    "evidence_ids": ["disc-ev-1"],
                }
            ],
            "evidences": [
                {
                    "evidence_id": "disc-ev-1",
                    "source_document_id": identity["source_document_id"],
                    "source_sha256": sha,
                    "pdf_page": 27,
                    "finding_ids": [finding_id],
                }
            ],
            "execution_contract": {"binding": dict(binding), "batches": []},
        }

    parent_discovery = make_discovery(parent_identity)
    current_identity = appendix._report_identity(current_report)
    current_discovery = make_discovery(current_identity)

    def make_investigation(identity: dict[str, Any], discovery: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "fintrace_annual_investigation",
            **identity,
            "status": "completed",
            "reason": None,
            "model_name": "test-model",
            "items": [
                {
                    "signal_id": "profit_up_cash_down",
                    "status": "interpretation",
                    "explanation": "收入变化仍需核查。",
                    "reason": None,
                    "limitations": ["模型观点未经独立验证。"],
                }
            ],
            "discovery": discovery,
        }

    parent_investigation = make_investigation(parent_identity, parent_discovery)
    current_investigation = make_investigation(current_identity, current_discovery)
    parent_report["model_review"] = {
        **_successful_review(parent_report, parent_investigation),
        "audit_artifacts": [
            {
                "audit_dir": "runs/annual-analysis-review-parent/review-audit",
                "request_path": "runs/annual-analysis-review-parent/review-audit/request.json",
                "response_path": "runs/annual-analysis-review-parent/review-audit/response.json",
                "failure_path": None,
                "status": "succeeded",
                "prompt_version": "annual_review_v2",
            }
        ],
    }
    return (
        parent_report,
        current_report,
        parent_investigation,
        current_investigation,
        parent_discovery,
        current_discovery,
    )


def test_parent_review_reuse_ignores_verification_time_and_rebinds_run_id(
    tmp_path: Path,
) -> None:
    (
        parent_report,
        current_report,
        parent_investigation,
        current_investigation,
        parent_discovery,
        current_discovery,
    ) = _review_reuse_fixture(tmp_path)

    reused = appendix._reuse_parent_review(
        parent_report,
        current_report,
        parent_investigation,
        current_investigation,
        parent_discovery,
        current_discovery,
        current_model_name="test-model",
    )

    assert reused is not None
    assert reused["reason"] == "review_reused_for_matching_inputs"
    assert reused["source_identity"]["run_id"] == current_report["run_id"]
    assert reused["model_called"] is False
    assert reused["call_count"] == reused["call_attempt_count"] == 0
    assert reused["audit_artifacts"] == parent_report["model_review"]["audit_artifacts"]

    # The child carries the original immutable review audit as provenance, so
    # an identical grandchild can reuse the same review without inventing a
    # new call or losing the auditable origin.
    child_report = copy.deepcopy(current_report)
    child_report["model_review"] = reused
    grandchild_report = copy.deepcopy(current_report)
    grandchild_report["run_id"] = "annual-analysis-review-grandchild"
    grandchild_report["confirmed"]["metrics"][0]["verifications"][0]["verified_at"] = (
        "2026-10-08T04:00:00Z"
    )
    grandchild_identity = appendix._report_identity(grandchild_report)
    grandchild_discovery = copy.deepcopy(current_discovery)
    grandchild_discovery["run_id"] = grandchild_identity["run_id"]
    grandchild_investigation = copy.deepcopy(current_investigation)
    grandchild_investigation["run_id"] = grandchild_identity["run_id"]
    grandchild_investigation["discovery"] = grandchild_discovery
    grandchild_review = appendix._reuse_parent_review(
        child_report,
        grandchild_report,
        current_investigation,
        grandchild_investigation,
        current_discovery,
        grandchild_discovery,
        current_model_name="test-model",
    )

    assert grandchild_review is not None
    assert grandchild_review["reason"] == "review_reused_for_matching_inputs"
    assert grandchild_review["source_identity"]["run_id"] == grandchild_report["run_id"]
    assert grandchild_review["call_count"] == grandchild_review["call_attempt_count"] == 0
    assert grandchild_review["audit_artifacts"] == parent_report["model_review"]["audit_artifacts"]


@pytest.mark.parametrize(
    "change",
    ("amount", "verification_status", "discovery", "model_binding", "prompt_binding"),
)
def test_parent_review_reuse_rejects_semantic_input_changes(
    tmp_path: Path,
    change: str,
) -> None:
    (
        parent_report,
        current_report,
        parent_investigation,
        current_investigation,
        parent_discovery,
        current_discovery,
    ) = _review_reuse_fixture(tmp_path)
    if change == "amount":
        current_report["confirmed"]["metrics"][0]["normalized_value"] = "123.46"
    elif change == "verification_status":
        current_report["confirmed"]["metrics"][0]["verifications"][0]["status"] = "rejected"
    elif change == "discovery":
        changed_discovery = copy.deepcopy(current_discovery)
        changed_discovery["findings"][0]["observation"] = "收入增长但经营现金流持续下降。"
        current_discovery = changed_discovery
        current_investigation["discovery"] = changed_discovery
    elif change == "model_binding":
        current_investigation["model_name"] = "different-model"
    else:
        current_discovery["execution_contract"]["binding"]["prompts_sha256"] = "d" * 64

    reused = appendix._reuse_parent_review(
        parent_report,
        current_report,
        parent_investigation,
        current_investigation,
        parent_discovery,
        current_discovery,
        current_model_name="test-model",
    )

    assert reused is None


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


def test_audit_start_without_durable_admission_is_not_counted_as_provider_call(
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
    assert investigation["model_called"] is True  # only the successful review was admitted
    assert investigation["investigation_call_count"] == 0
    assert investigation["investigation_call_attempt_count"] == 0
    assert investigation["model_review_call_count"] == 1
    assert investigation["model_call_count"] == 1
    assert investigation["model_call_attempt_count"] == 1
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


@pytest.mark.parametrize(
    ("with_response", "extra_user_field"),
    [(False, False), (True, False), (False, True)],
)
def test_discovery_exception_recovers_v2_audit_counts_without_inventing_coverage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_response: bool,
    extra_user_field: bool,
) -> None:
    _repository, artifacts_root, run_dir, source_pdf, _record_path, report = _fixture(tmp_path)
    audit_id = str(uuid.uuid4())

    class _FailingDiscoveryAgent:
        @staticmethod
        def run_annual_discovery(*_args: Any, run_dir: Path, **_kwargs: Any) -> Any:
            audit_dir = run_dir / audit_id
            audit_dir.mkdir()
            attempt_audit_id = str(uuid.uuid4())
            admission_id = str(uuid.uuid4())
            request_batch = {
                "company_id": report["company_id"],
                "report_year": report["report_year"],
                "source_document_id": report["source_document_id"],
                "source_sha256": report["source_sha256"],
                "batch_index": 1,
                "fragments": [],
            }
            if extra_user_field:
                request_batch["unexpected"] = "fail closed"
            (audit_dir / "request.json").write_text(
                json.dumps({
                    "status": "started",
                    "prompt_version": "annual_discovery_v2",
                    "messages": [
                        {"role": "system", "content": "synthetic prompt"},
                        {"role": "user", "content": json.dumps(request_batch)},
                    ],
                }),
                encoding="utf-8",
            )
            if with_response:
                (audit_dir / "response.json").write_text(
                    json.dumps({
                        "status": "succeeded", "text": "{}",
                        "provider_admission_count": 1, "response_count": 1,
                        "attempt_records": [{
                            "provider_admitted": True, "response_accepted": True,
                            "audit_id": attempt_audit_id, "admission_id": admission_id,
                        }],
                    }),
                    encoding="utf-8",
                )
            else:
                (audit_dir / "failure.json").write_text(
                    json.dumps({
                        "category": "timeout",
                        "provider_admission_count": 1, "response_count": 0,
                        "attempt_records": [{
                            "provider_admitted": True, "response_accepted": False,
                            "audit_id": attempt_audit_id, "admission_id": admission_id,
                        }],
                    }), encoding="utf-8"
                )
            raise RuntimeError("synthetic local failure after request audit")

    original_import = appendix.importlib.import_module

    def import_agent(name: str) -> Any:
        if name == "finagent.agents.annual_discovery":
            return _FailingDiscoveryAgent
        return original_import(name)

    monkeypatch.setattr(appendix.importlib, "import_module", import_agent)

    discovery = appendix._invoke_discovery(
        report,
        source_pdf_path=source_pdf,
        source_record={"document_id": "mock-document"},
        settings=_settings(),
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        progress_callback=None,
    )

    assert discovery["status"] == "failed"
    assert discovery["prompt_version"] == "annual_discovery_v2"
    assert discovery["model_call_attempt_count"] == 1
    assert discovery["model_call_count"] == int(with_response)
    assert discovery["model_called"] is True
    assert discovery["coverage"]["source_status"] == "unavailable"
    assert discovery["coverage"]["content_status"] == "unavailable"
    assert discovery["coverage"]["planned_batches"] == 0
    assert discovery["coverage"]["batch_results"] == []
    if extra_user_field:
        assert discovery["audit_artifacts"] == []
        assert any("未编造批次审计记录" in item for item in discovery["limitations"])
    else:
        relative_audit_dir = f"artifacts/runs/{report['run_id']}/{audit_id}"
        assert discovery["audit_artifacts"] == [
            {
                "batch_index": 1,
                "audit_dir": relative_audit_dir,
                "request_path": f"{relative_audit_dir}/request.json",
                "response_path": f"{relative_audit_dir}/response.json" if with_response else None,
                "failure_path": None if with_response else f"{relative_audit_dir}/failure.json",
                "status": "succeeded" if with_response else "failed",
                "prompt_version": "annual_discovery_v2",
            }
        ]


def test_not_called_discovery_uses_current_v2_prompt_identity(
    tmp_path: Path,
) -> None:
    _repository, _artifacts_root, _run_dir, _source_pdf, _record_path, report = _fixture(tmp_path)

    result = appendix._not_called_discovery(appendix._report_identity(report), "not_started")

    assert result["prompt_version"] == "annual_discovery_v2"
    assert result["model_call_attempt_count"] == result["model_call_count"] == 0
    assert result["coverage"]["planned_batches"] == 0


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
    assert "## 年报模型调查" in markdown
    assert "未核实模型解释" in markdown
    assert "response.json" in markdown


def test_recoverable_payload_keeps_discovery_and_sums_all_stages_in_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, artifacts_root, run_dir, source_pdf, record_path, report = _fixture(tmp_path)
    identity = appendix._report_identity(report)
    settings = _settings()
    monkeypatch.setattr(appendix, "_repository_root", lambda: repository)
    monkeypatch.setattr(
        annual_recovery, "build_annual_recovery_binding", lambda *_args, **_kwargs: {"binding": "stable"}
    )
    discovery = {
        "kind": "fintrace_annual_discovery",
        **identity,
        "status": "partial",
        "model_called": True,
        "model_call_count": 2,
        "model_call_attempt_count": 3,
        "coverage": {"planned_batches": 1},
        "findings": [],
        "evidences": [],
        "audit_artifacts": [{"audit_dir": "runs/discovery", "request_path": "runs/discovery/request.json"}],
        "execution_contract": {"batches": []},
    }

    class _Result:
        def to_dict(self) -> dict[str, Any]:
            return {
                "kind": "fintrace_annual_investigation",
                **identity,
                "status": "abstained",
                "model_called": True,
                "model_call_count": 4,
                "model_call_attempt_count": 5,
                "investigation_call_count": 4,
                "investigation_call_attempt_count": 5,
                "items": [],
                "narrative_evidences": [],
                "audit_artifacts": [{"audit_dir": "runs/fixed", "request_path": "runs/fixed/request.json"}],
                "reason": "model_call_failed",
            }

    monkeypatch.setattr(appendix, "_invoke_discovery", lambda *_args, **_kwargs: discovery)
    monkeypatch.setattr(appendix, "_invoke_agent", lambda *_args, **_kwargs: _Result())
    monkeypatch.setattr(appendix, "_run_children", lambda _run_dir: set())
    monkeypatch.setattr(appendix, "_call_control_requested", lambda: False)
    monkeypatch.setattr(appendix, "_invoke_review", _successful_review)
    checkpoints: list[dict[str, Any]] = []

    result = appendix._run_recoverable_appendix(
        report,
        state={
            "archive_path": "artifacts/runs/annual-analysis-appendix-test/investigation.json",
            "source_binding": {"source_record_sha256": "a" * 64},
        },
        identity=identity,
        binding_source_report=report,
        archived_pdf_path=source_pdf,
        source_record={"document_id": identity["source_document_id"]},
        settings=settings,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        request_mode="model_investigation",
        request_identity={"mode": "model_investigation"},
        resume_parent_run_id=None,
        progress_callback=None,
        checkpoint_callback=lambda snapshot, _state, _parent: checkpoints.append(snapshot),
    )

    assert result["discovery"] == discovery
    assert result["discovery_call_count"] == 2
    assert result["investigation_call_count"] == 4
    assert result["model_review_call_count"] == 1
    assert result["model_call_count"] == 7
    assert result["model_call_attempt_count"] == 9
    assert len(result["audit_artifacts"]) == 3
    assert checkpoints
    final = checkpoints[-1]
    assert final["model_called"] is True
    assert final["model_discovery"] == result["discovery"]
    assert final["model_investigation"]["model_call_count"] == result["model_call_count"]
    assert final["model_investigation"]["model_call_attempt_count"] == result["model_call_attempt_count"]
    assert final["model_review"] == result["final_review"]


def test_v3_provider_stop_prevents_fixed_and_review_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, artifacts_root, run_dir, source_pdf, _record_path, report = _fixture(tmp_path)
    identity = appendix._report_identity(report)
    settings = _settings()
    monkeypatch.setattr(appendix, "_repository_root", lambda: repository)
    monkeypatch.setattr(
        annual_recovery, "build_annual_recovery_binding", lambda *_args, **_kwargs: {"binding": "stable"}
    )
    discovery = {
        "kind": "fintrace_annual_discovery",
        **identity,
        "status": "failed",
        "model_called": True,
        "model_call_count": 0,
        "model_call_attempt_count": 1,
        "coverage": {"planned_batches": 2},
        "findings": [],
        "evidences": [],
        "audit_artifacts": [],
        "execution_contract": {
            "batches": [
                {"state": "failed", "attempts": [{"reason": "ledger_provider_mismatch"}]},
                {"state": "unattempted", "reason": "provider_admission_rejected", "attempts": []},
            ]
        },
    }
    monkeypatch.setattr(appendix, "_invoke_discovery", lambda *_args, **_kwargs: discovery)
    monkeypatch.setattr(appendix, "_call_control_requested", lambda: False)
    monkeypatch.setattr(appendix, "_invoke_agent", lambda *_args, **_kwargs: pytest.fail("fixed stage must stop"))
    monkeypatch.setattr(appendix, "_invoke_review", lambda *_args, **_kwargs: pytest.fail("review must stop"))
    checkpoints: list[dict[str, Any]] = []

    result = appendix._run_recoverable_appendix(
        report,
        state={"archive_path": "runs/investigation.json", "source_binding": {}},
        identity=identity,
        binding_source_report=report,
        archived_pdf_path=source_pdf,
        source_record={"document_id": identity["source_document_id"]},
        settings=settings,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        request_mode="model_investigation",
        request_identity={"mode": "model_investigation"},
        resume_parent_run_id=None,
        progress_callback=None,
        checkpoint_callback=lambda snapshot, _state, _parent: checkpoints.append(snapshot),
    )

    assert result["reason"] == "provider_admission_rejected"
    assert result["discovery_call_attempt_count"] == 1
    assert result["investigation_call_attempt_count"] == 0
    assert result["model_review_call_attempt_count"] == 0
    assert result["discovery"] == discovery
    assert checkpoints[-1]["model_discovery"] == discovery


def test_fatal_candidate_rejection_skips_review_and_remains_resumable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, artifacts_root, run_dir, source_pdf, _record_path, report = _fixture(tmp_path)
    identity = appendix._report_identity(report)
    settings = _settings()
    monkeypatch.setattr(appendix, "_repository_root", lambda: repository)
    monkeypatch.setattr(
        annual_recovery, "build_annual_recovery_binding", lambda *_args, **_kwargs: {"binding": "stable"}
    )
    discovery = {
        "kind": "fintrace_annual_discovery",
        **identity,
        "status": "completed",
        "model_called": True,
        "model_call_count": 1,
        "model_call_attempt_count": 1,
        "coverage": {"planned_batches": 1},
        "findings": [],
        "evidences": [],
        "audit_artifacts": [],
        "execution_contract": {"batches": []},
    }
    fixed = _successful_payload(report)
    fixed.update(
        status="abstained",
        reason="ledger_provider_mismatch",
        model_called=False,
        model_call_count=0,
        model_call_attempt_count=0,
        investigation_call_count=0,
        investigation_call_attempt_count=0,
        items=[{
            "signal_id": "profit_up_cash_down",
            "status": "abstained",
            "explanation": None,
            "alternative_explanations": [],
            "limitations": ["调用账本拒绝本次请求。"],
            "narrative_evidence_ids": [],
            "reason": "ledger_provider_mismatch",
        }],
    )
    monkeypatch.setattr(appendix, "_invoke_discovery", lambda *_args, **_kwargs: discovery)
    monkeypatch.setattr(appendix, "_invoke_agent", lambda *_args, **_kwargs: _FakeResult(fixed))
    monkeypatch.setattr(appendix, "_invoke_review", lambda *_args, **_kwargs: pytest.fail("review must not start"))
    monkeypatch.setattr(appendix, "_call_control_requested", lambda: False)
    checkpoints: list[dict[str, Any]] = []

    result = appendix._run_recoverable_appendix(
        report,
        state={"archive_path": "runs/investigation.json", "source_binding": {}},
        identity=identity,
        binding_source_report=report,
        archived_pdf_path=source_pdf,
        source_record={
            "document_id": identity["source_document_id"],
            "company_id": identity["company_id"],
            "report_period": "2024-12-31",
            "sha256": identity["source_sha256"],
        },
        settings=settings,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        request_mode="model_investigation",
        request_identity={"mode": "model_investigation"},
        resume_parent_run_id=None,
        progress_callback=None,
        checkpoint_callback=lambda snapshot, _state, _parent: checkpoints.append(snapshot),
    )

    assert result["reason"] == "ledger_provider_mismatch"
    assert result["model_call_attempt_count"] == 1
    assert result["investigation_call_attempt_count"] == 0
    assert result["model_review_call_attempt_count"] == 0
    assert result["final_review"]["status"] == "not_called"
    assert result["final_review"]["reason"] == "ledger_provider_mismatch"
    assert annual_recovery._investigation_has_unfinished_items(fixed) is True
    assert checkpoints[-1]["model_review"]["status"] == "not_called"


@pytest.mark.parametrize("stage_reason", ["stage_budget_exhausted", "call_deadline_exhausted"])
def test_fixed_stage_budget_exhaustion_keeps_review_reservation_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage_reason: str
) -> None:
    repository, artifacts_root, run_dir, source_pdf, _record_path, report = _fixture(tmp_path)
    identity = appendix._report_identity(report)
    monkeypatch.setattr(appendix, "_repository_root", lambda: repository)
    monkeypatch.setattr(
        annual_recovery, "build_annual_recovery_binding", lambda *_args, **_kwargs: {"binding": "stable"}
    )
    discovery = {
        "kind": "fintrace_annual_discovery",
        **identity,
        "status": "completed",
        "model_called": True,
        "model_call_count": 1,
        "model_call_attempt_count": 1,
        "coverage": {"planned_batches": 1},
        "findings": [],
        "evidences": [],
        "audit_artifacts": [],
        "execution_contract": {"batches": []},
    }
    fixed = _successful_payload(report)
    fixed.update(status="abstained", reason=stage_reason)
    review_calls: list[str] = []
    monkeypatch.setattr(appendix, "_invoke_discovery", lambda *_args, **_kwargs: discovery)
    monkeypatch.setattr(appendix, "_invoke_agent", lambda *_args, **_kwargs: _FakeResult(fixed))
    monkeypatch.setattr(appendix, "_call_control_requested", lambda: False)
    monkeypatch.setattr(
        appendix,
        "_invoke_review",
        lambda *args, **kwargs: (review_calls.append("review"), _successful_review(*args, **kwargs))[1],
    )

    result = appendix._run_recoverable_appendix(
        report,
        state={"archive_path": "runs/investigation.json", "source_binding": {}},
        identity=identity,
        binding_source_report=report,
        archived_pdf_path=source_pdf,
        source_record={"document_id": identity["source_document_id"]},
        settings=_settings(),
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        request_mode="model_investigation",
        request_identity={"mode": "model_investigation"},
        resume_parent_run_id=None,
        progress_callback=None,
        checkpoint_callback=None,
    )

    assert review_calls == ["review"]
    assert result["final_review"]["status"] == "completed"


def test_rejected_resume_persists_own_zero_call_v3_snapshot_without_parent_inheritance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    import finagent.api.annual_report_read as report_read
    import finagent.agents.annual_discovery as annual_discovery

    repository, artifacts_root, run_dir, source_pdf, _record_path, report = _fixture(tmp_path)
    identity = appendix._report_identity(report)
    settings = _settings()
    parent_run_id = "annual-analysis-parent-rejected"
    binding = {"analysis_mode": "integrated_analysis", "plan_sha256": "b" * 64}
    zero_call_discovery = {
        "kind": "fintrace_annual_discovery",
        **identity,
        "status": "not_called",
        "model_called": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "coverage": {"batch_results": [{"status": "unattempted"}]},
        "findings": [],
        "evidences": [],
        "audit_artifacts": [],
        "execution_contract": {"parent_run_id": None, "batches": []},
    }
    monkeypatch.setattr(appendix, "_repository_root", lambda: repository)
    monkeypatch.setattr(
        annual_recovery,
        "build_annual_recovery_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(
        report_read,
        "load_annual_report",
        lambda *_args, **_kwargs: SimpleNamespace(report={}, run_id=parent_run_id),
    )
    monkeypatch.setattr(
        annual_recovery,
        "validate_annual_recovery_parent",
        lambda *_args, **_kwargs: {
            "can_resume": False,
            "reason": "recovery_binding_mismatch",
            "binding": {"analysis_mode": "model_investigation"},
        },
    )
    monkeypatch.setattr(
        annual_discovery,
        "build_annual_discovery_initial_snapshot",
        lambda *_args, **_kwargs: zero_call_discovery,
    )
    monkeypatch.setattr(
        appendix,
        "_invoke_discovery",
        lambda *_args, **_kwargs: pytest.fail("rejected parent must not dispatch discovery"),
    )
    checkpoints: list[tuple[dict[str, Any], str, str | None]] = []

    with pytest.raises(appendix.AnnualRecoveryRejectedError) as captured:
        appendix._run_recoverable_appendix(
            report,
            state={"archive_path": "runs/investigation.json", "source_binding": {"sha256": "a" * 64}},
            identity=identity,
            binding_source_report=report,
            archived_pdf_path=source_pdf,
            source_record={
                "company_id": identity["company_id"],
                "document_id": identity["source_document_id"],
                "report_period": "2024-12-31",
                "sha256": identity["source_sha256"],
            },
            settings=settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
            request_mode="integrated_analysis",
            request_identity={"mode": "integrated_analysis"},
            resume_parent_run_id=parent_run_id,
            progress_callback=None,
            checkpoint_callback=lambda snapshot, state, parent: checkpoints.append(
                (snapshot, state, parent)
            ),
        )

    assert captured.value.reason == "recovery_binding_mismatch"
    assert captured.value.requested_parent_run_id == parent_run_id
    assert captured.value.investigation["model_call_count"] == 0
    assert captured.value.investigation["model_call_attempt_count"] == 0
    assert captured.value.investigation["discovery"]["execution_contract"]["parent_run_id"] is None
    assert checkpoints == [(captured.value.snapshot_report, "failed", None)]
