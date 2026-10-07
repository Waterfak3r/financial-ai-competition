from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from fastapi.testclient import TestClient

import finagent.api.annual_analysis_jobs as jobs_module
from finagent.api.annual_analysis_jobs import CliResult
from finagent.api.app import create_app
from finagent.core.annual_runtime_identity import capture_cli_runtime_identity
from finagent.core.model_settings import ModelSettings
from finagent.core.model_settings_store import save_local_settings
from scripts import analyze_annual


def _make_source(root: Path, *, document_id: str = "doc-1") -> dict[str, object]:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Constructed annual report source")
    pdf_bytes = document.tobytes()
    document.close()

    relative = f"603288/2024/{document_id}/annual.pdf"
    source_dir = root / "data" / "raw" / "603288" / "2024" / document_id
    source_dir.mkdir(parents=True)
    (source_dir / "annual.pdf").write_bytes(pdf_bytes)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    source_record = {
        "company_id": "603288",
        "document_id": document_id,
        "report_type": "年度报告",
        "report_period": "2024-12-31",
        "local_path": f"data/raw/{relative}",
        "sha256": digest,
    }
    (source_dir / "source.json").write_text(
        json.dumps(source_record, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "company_id": "603288",
        "report_year": 2024,
        "document_id": document_id,
        "source_pdf_path": relative,
        "sha256": digest,
        "mode": "deterministic",
        "pdf_bytes": pdf_bytes,
    }


def _request(source: dict[str, object], **overrides: object) -> dict[str, object]:
    body = {key: value for key, value in source.items() if key != "pdf_bytes"}
    body.update(overrides)
    return body


def _write_strict_v3_recovery_fixture(
    project_root: Path,
    *,
    suffix: int,
    state: str,
    is_partial: bool,
    discovery_complete: bool = False,
) -> tuple[dict[str, object], dict[str, object], ModelSettings]:
    """Build an archive-reader-valid v3 report and matching raw source locally."""

    from tests.unit.test_annual_recovery_report_read import (
        _discovery,
        _original_review_with_audit,
        _review_investigation,
        _unattempted_modes,
        _write_run,
    )
    from finagent.core.annual_checkpoints import save_annual_checkpoint
    from finagent.core.annual_recovery import (
        build_annual_recovery_binding,
        recovery_identity_sha256,
    )

    run = _write_run(project_root, suffix, mode="model_investigation")
    relative_source = "603288/2024/doc-recovery-fixture/fixture.pdf"
    request: dict[str, object] = {
        "company_id": "603288",
        "report_year": 2024,
        "document_id": "doc-recovery-fixture",
        "source_pdf_path": relative_source,
        "mode": "model_investigation",
        "sha256": run["source_sha"],
    }
    raw_dir = project_root / "data" / "raw" / "603288" / "2024" / "doc-recovery-fixture"
    raw_dir.mkdir(parents=True)
    source_bytes = (run["run_dir"] / "source.pdf").read_bytes()
    (raw_dir / "fixture.pdf").write_bytes(source_bytes)
    (raw_dir / "source.json").write_text(
        json.dumps({
            "company_id": "603288",
            "document_id": "doc-recovery-fixture",
            "report_period": "2024-12-31",
            "local_path": f"data/raw/{relative_source}",
            "sha256": request["sha256"],
        }),
        encoding="utf-8",
    )
    settings = ModelSettings(
        base_url="http://127.0.0.1:9/v1",
        api_key="constructed-test-only",
        model="local-fixture-model",
        timeout_seconds=1.0,
    )
    report = copy.deepcopy(run["report"])
    batch_modes = _unattempted_modes(run["context"])
    if discovery_complete:
        batch_modes = {index: "success_parent" for index in batch_modes}
    report["model_discovery"] = _discovery(
        run,
        batch_modes=batch_modes,
    )
    if discovery_complete:
        report["model_investigation"] = _review_investigation(
            run,
            report["model_discovery"],
        )
    binding = build_annual_recovery_binding(
        project_root,
        request,
        settings=settings,
        report=report,
    )
    contract = report["model_discovery"]["execution_contract"]
    contract["plan_sha256"] = binding["plan_sha256"]
    contract["identity_sha256"] = recovery_identity_sha256(binding)
    contract["binding"] = binding
    if discovery_complete:
        report["model_review"] = _original_review_with_audit(run, report)

    save_annual_checkpoint(project_root, str(run["run_id"]), {
        "state": state,
        "is_partial": is_partial,
        "parent_run_id": None,
        "source_sha256": run["source_sha"],
        "report": report,
    })
    if not is_partial:
        (run["report_dir"] / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        run["manifest"]["status"] = state
        (run["run_dir"] / "manifest.json").write_text(
            json.dumps(run["manifest"], ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    return run, request, settings


def _write_persisted_job(
    project_root: Path,
    *,
    job_id: str,
    status: str,
    request: dict[str, object],
    run_id: str,
) -> Path:
    job_dir = project_root / "artifacts" / "annual-analysis-jobs" / job_id
    job_dir.mkdir(parents=True)
    job_path = job_dir / "job.json"
    job_path.write_text(json.dumps({
        "job_id": job_id,
        "status": status,
        "stage": status,
        "created_at": "2026-10-06T00:00:00+00:00",
        "updated_at": "2026-10-06T00:00:01+00:00",
        "request": request,
        "run_id": run_id,
        "result_url": f"/v1/annual-analyses/{run_id}",
        "error": None,
    }), encoding="utf-8")
    return job_path


def _write_success_archive(
    project_root: Path,
    request: dict[str, object],
    *,
    status: str = "completed",
    run_id: str | None = None,
) -> str:
    reserved_run_id = request.get("_reserved_run_id")
    run_id = run_id or (
        reserved_run_id if isinstance(reserved_run_id, str) else f"annual-analysis-{uuid4()}"
    )
    run_dir = project_root / "artifacts" / "runs" / run_id
    report_dir = project_root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    source = project_root / "data" / "raw" / str(request["source_pdf_path"])
    (run_dir / "source.pdf").write_bytes(source.read_bytes())
    manifest = {
        "run_id": run_id,
        "status": status,
        "mode": request.get("mode", "deterministic"),
        "company_id": request["company_id"],
        "report_year": request["report_year"],
        "document_id": request["document_id"],
        "inputs": {
            "source_pdf_archive_path": f"runs/{run_id}/source.pdf",
            "source_pdf_sha256": request["sha256"],
        },
        "outputs": {
            "run_dir": f"runs/{run_id}",
            "report_dir": f"reports/{run_id}",
            "report_json": f"reports/{run_id}/report.json",
        },
    }
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": run_id,
        "company_id": request["company_id"],
        "report_year": request["report_year"],
        "source_document_id": request["document_id"],
        "source_sha256": request["sha256"],
        "confirmed": {"metrics": [], "analyses": []},
    }
    mode = request.get("mode", "deterministic")
    if mode in {"m3_screening", "integrated_analysis"}:
        manifest["m3_screening"] = {"requested": True, "status": "completed"}
        report["m3_screening"] = {"status": "completed", "score": 25, "rules": []}
    if mode in {"model_investigation", "integrated_analysis"}:
        manifest["model_investigation"] = {"requested": True, "status": "completed"}
        manifest["model_called"] = True
        report["model_called"] = True
        report["model_investigation"] = {"status": "completed", "items": []}
        report["model_review"] = {"status": "completed", "assessment": "review", "reasons": []}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (report_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return run_id


def _successful_runner(project_root: Path, _artifacts_root: Path, request: dict[str, object]) -> CliResult:
    run_id = _write_success_archive(
        project_root,
        request,
        run_id=request.get("_reserved_run_id") if isinstance(request.get("_reserved_run_id"), str) else None,
    )
    return CliResult(0, f"status=completed run_id={run_id}", "")


def _wait_terminal(client: TestClient, job_id: str, *, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/annual-analysis-jobs/{job_id}")
        assert response.status_code == 200
        state = response.json()
        if state["status"] in {"completed", "completed_with_issues", "failed", "interrupted"}:
            return state
        time.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 未在 {timeout} 秒内结束。")


@pytest.fixture(autouse=True)
def _inject_test_cli_runtime_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # These API tests use isolated artifact roots without a scripts/ checkout.
    # Inject a real, validated identity snapshot instead of weakening production gating.
    identity = capture_cli_runtime_identity()
    monkeypatch.setattr(jobs_module, "_probe_cli_runtime_identity", lambda _root: identity)


def test_runtime_version_mismatch_is_rejected_before_queue_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source(tmp_path)
    app = create_app(tmp_path)
    service = app.state.annual_analysis_jobs
    service.api_runtime_identity = {
        **service.api_runtime_identity,
        "source_manifest_sha256": "0" * 64,
    }
    invoked = False

    def unexpected_runner(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("version mismatch must be rejected before CLI execution")

    monkeypatch.setattr(jobs_module, "_invoke_cli", unexpected_runner)
    response = TestClient(app).post("/v1/annual-analysis-jobs", json=_request(source))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runtime_version_mismatch"
    assert invoked is False
    assert not (tmp_path / "artifacts" / "annual-analysis-jobs").exists()


def test_runtime_diagnostics_keeps_loaded_identity_separate_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path)
    startup_fingerprint = app.state.annual_analysis_jobs.api_runtime_identity["source_manifest_sha256"]
    monkeypatch.setattr(jobs_module, "source_manifest_fingerprint", lambda _root: ("f" * 64, 68))

    response = TestClient(app).get("/v1/runtime-identity")

    assert response.status_code == 200
    diagnostics = response.json()
    assert diagnostics["api"]["source_manifest_sha256"] == startup_fingerprint
    assert diagnostics["disk_source_manifest_sha256"] == "f" * 64
    assert diagnostics["source_changed_since_api_start"] is True


def test_failed_job_exposes_only_a_read_only_strictly_valid_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source(tmp_path)
    request = _request(source)
    run_id = _write_success_archive(tmp_path, request)
    job_id = "annual-job-" + "b" * 32
    job_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / job_id
    job_dir.mkdir(parents=True)
    state_path = job_dir / "job.json"
    state_path.write_text(json.dumps({
        "job_id": job_id,
        "status": "failed",
        "stage": "failed",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:00:01+00:00",
        "request": {key: request[key] for key in (
            "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
        )},
        "run_id": run_id,
        "result_url": None,
        "error": {"code": "analysis_archive_invalid", "message": "Historical failure."},
    }), encoding="utf-8")
    paths = [
        state_path,
        tmp_path / "artifacts" / "runs" / run_id / "manifest.json",
        tmp_path / "artifacts" / "runs" / run_id / "source.pdf",
        tmp_path / "artifacts" / "reports" / run_id / "report.json",
    ]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    monkeypatch.setattr(jobs_module, "_invoke_cli", lambda *_args, **_kwargs: pytest.fail("GET must not execute CLI"))
    client = TestClient(create_app(tmp_path))

    job = client.get(f"/v1/annual-analysis-jobs/{job_id}")
    history = client.get("/v1/annual-analysis-history")

    assert job.status_code == history.status_code == 200
    job_payload = job.json()
    assert job_payload["status"] == "failed"
    assert job_payload["result_url"] is None
    assert job_payload["result_availability"]["status"] == "available"
    assert job_payload["result_availability"]["result_url"] == f"/v1/annual-analyses/{run_id}"
    row = next(item for item in history.json()["items"] if item["job_id"] == job_id)
    assert row["job_status"] == "failed"
    assert row["result_available"] is True
    assert "failed_job_archive_readable" in {issue["code"] for issue in row["issues"]}
    assert client.get(job_payload["result_availability"]["result_url"]).status_code == 200
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert after == before


def test_failed_job_projection_rejects_tampered_archived_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source(tmp_path)
    request = _request(source)
    run_id = _write_success_archive(tmp_path, request)
    job_id = "annual-job-" + "c" * 32
    job_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / job_id
    job_dir.mkdir(parents=True)
    state_path = job_dir / "job.json"
    state_path.write_text(json.dumps({
        "job_id": job_id,
        "status": "failed",
        "stage": "failed",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:00:01+00:00",
        "request": {key: request[key] for key in (
            "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
        )},
        "run_id": run_id,
        "result_url": None,
        "error": {"code": "analysis_archive_invalid", "message": "Historical failure."},
    }), encoding="utf-8")
    archived_source = tmp_path / "artifacts" / "runs" / run_id / "source.pdf"
    archived_source.write_bytes(archived_source.read_bytes() + b"tampered")
    monkeypatch.setattr(jobs_module, "_invoke_cli", lambda *_args, **_kwargs: pytest.fail("GET must not execute CLI"))

    client = TestClient(create_app(tmp_path))
    response = client.get(f"/v1/annual-analysis-jobs/{job_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["result_url"] is None
    assert payload["result_availability"] == {
        "status": "unavailable",
        "category": "source_validation_failed",
        "message": "原文来源或引用校验未通过，报告不可用。",
        "run_id": None,
        "result_url": None,
        "is_partial": False,
        "snapshot_state": None,
        "snapshot_sequence": 0,
    }


def test_invoke_cli_requires_expected_fingerprint_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source(tmp_path)
    invoked = False

    def unexpected_subprocess(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("invalid runtime identity must not launch a child process")

    monkeypatch.setattr(jobs_module.subprocess, "run", unexpected_subprocess)
    with pytest.raises(RuntimeError, match="runtime fingerprint"):
        jobs_module._invoke_cli(tmp_path, tmp_path / "artifacts", _request(source))

    assert invoked is False


def test_web_progress_cli_without_expected_fingerprint_stops_before_source_or_model(
    tmp_path: Path,
) -> None:
    script = Path(analyze_annual.__file__).resolve()
    artifacts = tmp_path / "artifacts"
    run = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source-pdf",
            str(tmp_path / "missing.pdf"),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--document-id",
            "doc-1",
            "--artifacts-root",
            str(artifacts),
            "--with-model",
            "--source-record",
            str(tmp_path / "missing-source.json"),
            "--discovery-progress-job-id",
            "annual-job-" + "a" * 32,
        ],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert run.returncode == 3
    assert "reason=runtime_version_mismatch" in run.stderr
    assert not artifacts.exists()


def test_successful_job_returns_existing_report_run_id(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    monkeypatch.setattr(jobs_module, "_invoke_cli", _successful_runner)
    client = TestClient(create_app(tmp_path))

    created = client.post("/v1/annual-analysis-jobs", json=_request(source))

    assert created.status_code == 202
    initial = created.json()
    assert initial["status"] == "queued"
    assert initial["stage"] == "queued"
    state = _wait_terminal(client, initial["job_id"])
    assert state["status"] == "completed"
    assert state["stage"] == "completed"
    assert state["run_id"].startswith("annual-analysis-")
    assert state["result_url"] == f"/v1/annual-analyses/{state['run_id']}"
    report = client.get(state["result_url"])
    assert report.status_code == 200
    assert report.json()["report"]["source_sha256"] == source["sha256"]
    state_file = tmp_path / "artifacts" / "annual-analysis-jobs" / initial["job_id"] / "job.json"
    assert json.loads(state_file.read_text(encoding="utf-8"))["status"] == "completed"


def test_integrated_mode_passes_http_contract_and_returns_both_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(tmp_path)
    monkeypatch.setattr(
        jobs_module,
        "get_annual_analysis_capabilities",
        lambda _project_root=None: {"model_investigation": {
            "available": True, "status": "ready", "annual_discovery_supported": True,
        }},
    )
    monkeypatch.setattr(jobs_module, "_invoke_cli", _successful_runner)
    client = TestClient(create_app(tmp_path))

    created = client.post(
        "/v1/annual-analysis-jobs",
        json=_request(source, mode="integrated_analysis"),
    )

    assert created.status_code == 202
    state = _wait_terminal(client, created.json()["job_id"])
    assert state["status"] == "completed"
    report_response = client.get(state["result_url"])
    assert report_response.status_code == 200
    archive = report_response.json()
    assert archive["manifest"]["mode"] == "integrated_analysis"
    assert archive["report"]["m3_screening"]["status"] == "completed"
    assert archive["report"]["model_investigation"]["status"] == "completed"
    assert archive["report"]["model_review"]["status"] == "completed"
    assert archive["report"]["model_called"] is True


@pytest.mark.parametrize(
    ("overrides", "status_code", "error_code"),
    [
        ({"source_pdf_path": "../../outside.pdf"}, 400, "invalid_source_path"),
        ({"company_id": "600000"}, 409, "source_identity_mismatch"),
        ({"document_id": "other-doc"}, 409, "source_identity_mismatch"),
        ({"report_year": 2023}, 409, "source_identity_mismatch"),
        ({"sha256": "0" * 64}, 409, "source_sha256_mismatch"),
    ],
)
def test_invalid_identity_path_and_hash_are_rejected(
    tmp_path: Path,
    overrides: dict[str, object],
    status_code: int,
    error_code: str,
) -> None:
    source = _make_source(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.post("/v1/annual-analysis-jobs", json=_request(source, **overrides))

    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == error_code
    assert not (tmp_path / "artifacts" / "annual-analysis-jobs").exists()


def test_source_file_symlink_is_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    original = tmp_path / "data" / "raw" / str(source["source_pdf_path"])
    outside = tmp_path.parent / f"{tmp_path.name}-outside.pdf"
    outside.write_bytes(original.read_bytes())
    original.unlink()
    try:
        original.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不能创建符号链接。")
    client = TestClient(create_app(tmp_path))

    response = client.post("/v1/annual-analysis-jobs", json=_request(source))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "source_path_invalid"


def test_model_investigation_requires_server_configuration_before_creating_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(tmp_path)
    monkeypatch.setattr(
        jobs_module,
        "get_annual_analysis_capabilities",
        lambda _project_root=None: {"model_investigation": {
            "available": False, "status": "not_configured", "annual_discovery_supported": True,
        }},
    )
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/v1/annual-analysis-jobs",
        json=_request(source, mode="model_investigation"),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "model_configuration_unavailable"
    assert not (tmp_path / "artifacts" / "annual-analysis-jobs").exists()


def test_capabilities_endpoint_returns_only_stable_readiness_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "mock-secret-value")
    monkeypatch.setenv("MODEL_BASE_URL", "https://private.example.invalid/v1")
    expected = {"model_investigation": {
        "available": False,
        "status": "not_configured",
        "annual_discovery_supported": True,
    }}
    monkeypatch.setattr(jobs_module, "get_annual_analysis_capabilities", lambda _project_root=None: expected)
    client = TestClient(create_app(tmp_path))

    response = client.get("/v1/annual-analysis-capabilities")

    assert response.status_code == 200
    assert response.json() == expected
    assert response.json()["model_investigation"]["annual_discovery_supported"] is True
    assert "mock-secret-value" not in response.text
    assert "private.example.invalid" not in response.text


@pytest.mark.parametrize(
    ("dependency_available", "settings_available", "expected_status"),
    [(False, False, "dependency_unavailable"), (True, False, "not_configured"), (True, True, "ready")],
)
def test_capability_code_support_flag_is_stable_without_loading_real_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependency_available: bool,
    settings_available: bool,
    expected_status: str,
) -> None:
    def import_mock(_name: str):
        if not dependency_available:
            raise ImportError("test dependency unavailable")
        return SimpleNamespace()

    monkeypatch.setattr(importlib, "import_module", import_mock)
    if settings_available:
        monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: object())
    else:
        def no_settings(_root):
            raise RuntimeError("mock settings unavailable")
        monkeypatch.setattr(jobs_module, "resolve_model_settings", no_settings)

    capability = jobs_module.get_annual_analysis_capabilities(tmp_path)["model_investigation"]

    assert capability == {
        "available": expected_status == "ready",
        "status": expected_status,
        "annual_discovery_supported": True,
    }


def test_failed_cli_archive_is_persisted_as_failed_job(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)

    def failed_runner(project_root: Path, _artifacts_root: Path, request: dict[str, object]) -> CliResult:
        run_id = f"annual-analysis-{uuid4()}"
        run_dir = project_root / "artifacts" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "source.pdf").write_bytes(
            (project_root / "data" / "raw" / str(request["source_pdf_path"])).read_bytes()
        )
        (run_dir / "manifest.json").write_text(
            json.dumps({
                "run_id": run_id,
                "status": "failed",
                "company_id": request["company_id"],
                "report_year": request["report_year"],
                "document_id": request["document_id"],
                "inputs": {},
                "outputs": {},
            }),
            encoding="utf-8",
        )
        (run_dir / "failure.json").write_text(
            json.dumps({"run_id": run_id, "error_type": "ValueError", "reason": "internal detail"}),
            encoding="utf-8",
        )
        return CliResult(2, f"status=failed run_id={run_id}", "internal detail")

    monkeypatch.setattr(jobs_module, "_invoke_cli", failed_runner)
    client = TestClient(create_app(tmp_path))
    created = client.post("/v1/annual-analysis-jobs", json=_request(source))

    state = _wait_terminal(client, created.json()["job_id"])

    assert state["status"] == "failed"
    assert state["run_id"].startswith("annual-analysis-")
    assert "ValueError" in state["error"]["message"]
    assert "internal detail" not in json.dumps(state)


def test_cli_timeout_is_persisted_without_subprocess_output(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)

    def timed_out_runner(_project_root: Path, _artifacts_root: Path, _request: dict[str, object]) -> CliResult:
        raise subprocess.TimeoutExpired("analyze_annual.py", timeout=900, output="secret-output", stderr="secret-error")

    monkeypatch.setattr(jobs_module, "_invoke_cli", timed_out_runner)
    client = TestClient(create_app(tmp_path))
    created = client.post("/v1/annual-analysis-jobs", json=_request(source))

    state = _wait_terminal(client, created.json()["job_id"])

    assert state["status"] == "failed"
    assert state["error"]["code"] == "analysis_timeout"
    assert "secret" not in json.dumps(state)


def test_cli_timeout_is_capped_configurable_and_credentials_are_removed(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "analyze_annual.py").write_text("# mock", encoding="utf-8")
    monkeypatch.setenv("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("MODEL_API_KEY", "test-secret")
    observed: dict[str, object] = {}

    def fake_run(command, *, cwd, env, timeout, **kwargs):
        observed.update(command=command, cwd=cwd, timeout=timeout, env=env, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="run_id=annual-analysis-00000000-0000-0000-0000-000000000000", stderr="")

    monkeypatch.setattr(jobs_module.subprocess, "run", fake_run)

    result = jobs_module._invoke_cli(
        tmp_path,
        tmp_path / "artifacts",
        _request(source, _expected_runtime_fingerprint="a" * 64),
    )

    assert result.returncode == 0
    assert observed["timeout"] == 120
    assert "MODEL_API_KEY" not in observed["env"]
    assert "test-secret" not in json.dumps(result.__dict__ if hasattr(result, "__dict__") else result.stdout)


def test_model_cli_receives_only_explicit_model_mode_and_adjacent_source_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "analyze_annual.py").write_text("# mock cli", encoding="utf-8")
    monkeypatch.setenv("MODEL_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("MODEL_API_KEY", "mock-api-key")
    monkeypatch.setenv("MODEL_NAME", "mock-model")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-provider-secret")
    observed: dict[str, object] = {}

    def fake_run(command, *, cwd, env, timeout, **kwargs):
        observed.update(command=command, cwd=cwd, timeout=timeout, env=env)
        return SimpleNamespace(returncode=0, stdout="run_id=annual-analysis-00000000-0000-0000-0000-000000000000", stderr="")

    monkeypatch.setattr(jobs_module.subprocess, "run", fake_run)

    result = jobs_module._invoke_cli(
        tmp_path,
        tmp_path / "artifacts",
        _request(source, mode="model_investigation", _expected_runtime_fingerprint="a" * 64),
    )

    command = observed["command"]
    environment = observed["env"]
    assert result.returncode == 0
    assert "--with-model" in command
    source_record_index = command.index("--source-record") + 1
    assert Path(command[source_record_index]) == (
        tmp_path / "data" / "raw" / str(source["source_pdf_path"])
    ).parent / "source.json"
    assert environment["MODEL_BASE_URL"] == "http://127.0.0.1:9999/v1"
    assert environment["MODEL_API_KEY"] == "mock-api-key"
    assert environment["MODEL_NAME"] == "mock-model"
    assert "OPENAI_API_KEY" not in environment
    assert "unrelated-provider-secret" not in json.dumps(environment)


def test_model_cli_uses_saved_settings_snapshot_and_screening_drops_all_model_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "analyze_annual.py").write_text("# mock cli", encoding="utf-8")
    save_local_settings(
        tmp_path,
        base_url="https://saved.example.invalid/v1",
        model_name="saved-model",
        api_key="saved-private-key",
    )
    monkeypatch.setenv("MODEL_BASE_URL", "https://environment.example.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "environment-private-key")
    monkeypatch.setenv("MODEL_NAME", "environment-model")
    observed: list[dict[str, object]] = []

    def fake_run(command, *, cwd, env, timeout, **kwargs):
        observed.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env})
        return SimpleNamespace(returncode=0, stdout="run_id=annual-analysis-00000000-0000-0000-0000-000000000000", stderr="")

    monkeypatch.setattr(jobs_module.subprocess, "run", fake_run)
    model_result = jobs_module._invoke_cli(
        tmp_path,
        tmp_path / "artifacts",
        _request(source, mode="model_investigation", _expected_runtime_fingerprint="a" * 64),
    )
    deterministic_result = jobs_module._invoke_cli(
        tmp_path,
        tmp_path / "artifacts",
        _request(source, mode="deterministic", _expected_runtime_fingerprint="a" * 64),
    )

    model_environment = observed[0]["env"]
    deterministic_environment = observed[1]["env"]
    assert model_result.returncode == deterministic_result.returncode == 0
    assert model_environment["MODEL_BASE_URL"] == "https://saved.example.invalid/v1"
    assert model_environment["MODEL_API_KEY"] == "saved-private-key"
    assert model_environment["MODEL_NAME"] == "saved-model"
    assert "saved-private-key" not in json.dumps(observed[0]["command"])
    assert "environment-private-key" not in json.dumps(model_environment)
    assert not any(key.startswith("MODEL_") for key in deterministic_environment)


def test_integrated_cli_combines_both_switches_and_injects_credentials_only_for_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "analyze_annual.py").write_text("# mock cli", encoding="utf-8")
    monkeypatch.setenv("MODEL_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "integrated-private-key")
    monkeypatch.setenv("MODEL_NAME", "integrated-model")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-provider-secret")
    observed: list[dict[str, object]] = []

    def fake_run(command, *, cwd, env, timeout, **kwargs):
        observed.append({"command": command, "env": env})
        return SimpleNamespace(returncode=0, stdout="run_id=annual-analysis-00000000-0000-0000-0000-000000000000", stderr="")

    monkeypatch.setattr(jobs_module.subprocess, "run", fake_run)

    jobs_module._invoke_cli(tmp_path, tmp_path / "artifacts", _request(source, mode="deterministic", _expected_runtime_fingerprint="a" * 64))
    jobs_module._invoke_cli(tmp_path, tmp_path / "artifacts", _request(source, mode="m3_screening", _expected_runtime_fingerprint="a" * 64))
    jobs_module._invoke_cli(tmp_path, tmp_path / "artifacts", _request(source, mode="integrated_analysis", _expected_runtime_fingerprint="a" * 64))

    deterministic_command, m3_command, integrated_command = [item["command"] for item in observed]
    deterministic_env, m3_env, integrated_env = [item["env"] for item in observed]
    assert "--with-model" not in deterministic_command and "--with-m3-screening" not in deterministic_command
    assert "--with-model" not in m3_command and "--with-m3-screening" in m3_command
    assert "--with-model" in integrated_command and "--with-m3-screening" in integrated_command
    assert not any(key.startswith("MODEL_") for env in (deterministic_env, m3_env) for key in env)
    assert integrated_env["MODEL_API_KEY"] == "integrated-private-key"
    assert integrated_env["MODEL_BASE_URL"] == "https://model.example.invalid/v1"
    assert integrated_env["MODEL_NAME"] == "integrated-model"
    assert "OPENAI_API_KEY" not in integrated_env
    assert "integrated-private-key" not in json.dumps(integrated_command)
    assert "unrelated-provider-secret" not in json.dumps(integrated_env)


def test_invalid_cli_timeout_configuration_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS", "901")

    with pytest.raises(RuntimeError, match="60 到 900"):
        jobs_module._configured_cli_timeout_seconds()


def test_model_and_deterministic_timeout_defaults_are_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS", raising=False)

    assert jobs_module._configured_cli_timeout_seconds("model_investigation") == 3600
    assert jobs_module._configured_cli_timeout_seconds("integrated_analysis") == 3600
    assert jobs_module._configured_cli_timeout_seconds("deterministic") == 900


def test_discovery_progress_accepts_full_source_counts_and_rejects_unknown_fields(tmp_path: Path) -> None:
    job_id = f"annual-job-{uuid4().hex}"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    state = {
        "request": {
            "company_id": "603288",
            "report_year": 2024,
            "document_id": "doc-progress",
            "sha256": "a" * 64,
            "mode": "model_investigation",
        }
    }
    record = {
        "kind": "fintrace_annual_discovery_progress_v1",
        "job_id": job_id,
        "run_id": f"annual-analysis-{uuid4()}",
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": "doc-progress",
        "source_sha256": "a" * 64,
        "event": "planned",
        "status": "running",
        "batch_index": 0,
        "planned_batches": 128,
        "processed_batches": 0,
        "successful_batches": 0,
        "failed_batches": 0,
        "total_pages": 208,
        "total_chars": 1_300_000,
        "updated_at": "2026-10-04T01:02:03+00:00",
    }
    progress_path = job_dir / "discovery-progress.json"
    progress_path.write_text(json.dumps(record), encoding="utf-8")

    progress = jobs_module._read_discovery_progress(job_dir, job_id, state)

    assert progress is not None
    assert progress["total_chars"] == 1_300_000
    assert progress["partial_batches"] == 0  # Old progress records normalize to zero.
    assert "raw_text" not in progress
    record.update(
        event="batch_finished",
        status="partial",
        batch_index=1,
        planned_batches=128,
        processed_batches=1,
        successful_batches=0,
        partial_batches=1,
        failed_batches=0,
    )
    progress_path.write_text(json.dumps(record), encoding="utf-8")
    progress = jobs_module._read_discovery_progress(job_dir, job_id, state)
    assert progress is not None
    assert progress["partial_batches"] == 1
    assert progress["successful_batches"] == 0
    record["partial_batches"] = 0
    progress_path.write_text(json.dumps(record), encoding="utf-8")
    assert jobs_module._read_discovery_progress(job_dir, job_id, state) is None
    record["raw_text"] = "must never be accepted or returned"
    progress_path.write_text(json.dumps(record), encoding="utf-8")
    assert jobs_module._read_discovery_progress(job_dir, job_id, state) is None


def test_discovery_progress_writer_persists_partial_as_incomplete_count_only_state(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "artifacts" / "runs" / "annual-analysis-progress-test"
    job_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / f"annual-job-{uuid4().hex}"
    run_dir.mkdir(parents=True)
    job_dir.mkdir(parents=True)
    writer = analyze_annual._progress_writer(
        run_dir,
        job_dir / "discovery-progress.json",
        run_id=run_dir.name,
        company_id="603288",
        report_year=2024,
        document_id="doc-progress",
        source_sha256="a" * 64,
        job_id=job_dir.name,
    )
    event = {
        "event": "batch_finished",
        "status": "partial",
        "batch_index": 1,
        "planned_batches": 2,
        "processed_batches": 1,
        "successful_batches": 0,
        "partial_batches": 1,
        "failed_batches": 0,
        "total_pages": 2,
        "total_chars": 100,
    }

    writer(event)

    for path in (run_dir / "discovery-progress.json", job_dir / "discovery-progress.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["partial_batches"] == 1
        assert record["successful_batches"] + record["partial_batches"] + record["failed_batches"] == 1
        assert "raw_text" not in record
    with pytest.raises(ValueError, match="relation invalid"):
        writer({**event, "successful_batches": 1})


def test_running_model_job_exposes_only_its_source_bound_discovery_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(tmp_path, document_id="progress-job")
    entered = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(
        jobs_module,
        "get_annual_analysis_capabilities",
        lambda _root=None: {"model_investigation": {
            "available": True, "status": "ready", "annual_discovery_supported": True,
        }},
    )

    def waiting_runner(project_root: Path, artifacts_root: Path, request: dict[str, object]) -> CliResult:
        job_id = str(request["_internal_job_id"])
        job_dir = artifacts_root / "annual-analysis-jobs" / job_id
        progress_record = {
            "kind": "fintrace_annual_discovery_progress_v1",
            "job_id": job_id,
            "run_id": f"annual-analysis-{uuid4()}",
            "company_id": request["company_id"],
            "report_year": request["report_year"],
            "source_document_id": request["document_id"],
            "source_sha256": request["sha256"],
            "event": "batch_finished",
            "status": "partial",
            "batch_index": 1,
            "planned_batches": 8,
            "processed_batches": 1,
            "successful_batches": 0,
            "partial_batches": 1,
            "failed_batches": 0,
            "total_pages": 208,
            "total_chars": 1_300_000,
            "updated_at": "2026-10-04T01:02:03+00:00",
        }
        (job_dir / "discovery-progress.json").write_text(
            json.dumps(progress_record), encoding="utf-8"
        )
        entered.set()
        assert release.wait(5)
        run_id = _write_success_archive(project_root, request)
        return CliResult(0, f"status=completed run_id={run_id}", "")

    monkeypatch.setattr(jobs_module, "_invoke_cli", waiting_runner)
    client = TestClient(create_app(tmp_path))
    created = client.post(
        "/v1/annual-analysis-jobs",
        json=_request(source, mode="model_investigation"),
    )
    assert created.status_code == 202
    job_id = str(created.json()["job_id"])
    assert entered.wait(2)
    try:
        state = client.get(f"/v1/annual-analysis-jobs/{job_id}")
        assert state.status_code == 200
        progress = state.json()["discovery_progress"]
        assert progress["event"] == "batch_finished"
        assert progress["planned_batches"] == 8
        assert progress["processed_batches"] == 1
        assert progress["successful_batches"] == 0
        assert progress["partial_batches"] == 1
        assert progress["total_chars"] == 1_300_000
        assert "source_sha256" not in progress
        assert "raw_text" not in progress
    finally:
        release.set()
    terminal = _wait_terminal(client, job_id)
    assert terminal["status"] == "completed"


def test_queue_is_bounded_and_successful_runs_do_not_overwrite(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    run_ids: list[str] = []

    def blocked_runner(project_root: Path, artifacts_root: Path, request: dict[str, object]) -> CliResult:
        entered.set()
        assert release.wait(5)
        result = _successful_runner(project_root, artifacts_root, request)
        with lock:
            run_ids.append(result.stdout.split("run_id=", 1)[1])
        return result

    monkeypatch.setattr(jobs_module, "_invoke_cli", blocked_runner)
    client = TestClient(create_app(tmp_path))
    original_bytes = (tmp_path / "data" / "raw" / str(source["source_pdf_path"])).read_bytes()
    created: list[dict[str, object]] = []
    for _ in range(4):
        response = client.post("/v1/annual-analysis-jobs", json=_request(source))
        assert response.status_code == 202
        created.append(response.json())
    assert entered.wait(2)
    full = client.post("/v1/annual-analysis-jobs", json=_request(source))
    assert full.status_code == 429
    assert full.json()["detail"]["code"] == "job_capacity_reached"

    release.set()
    states = [_wait_terminal(client, str(item["job_id"])) for item in created]

    assert all(item["status"] == "completed" for item in states)
    assert len({item["job_id"] for item in states}) == 4
    assert len(set(run_ids)) == 4
    assert (tmp_path / "data" / "raw" / str(source["source_pdf_path"])).read_bytes() == original_bytes
    assert len(list((tmp_path / "artifacts" / "reports").iterdir())) == 4


def test_queued_cancel_is_idempotent_and_releases_its_slot_once(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    invoked: list[str] = []

    def controlled_runner(project_root: Path, artifacts_root: Path, request: dict[str, object]) -> CliResult:
        job_id = str(request["_internal_job_id"])
        invoked.append(job_id)
        if len(invoked) == 1:
            entered.set()
            assert release.wait(5)
        return _successful_runner(project_root, artifacts_root, request)

    monkeypatch.setattr(jobs_module, "_invoke_cli", controlled_runner)
    service = jobs_module.AnnualAnalysisJobService(
        tmp_path,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    try:
        first = service.create(_request(source))
        assert entered.wait(2)
        queued = service.create(_request(source))

        cancelled = service.cancel(str(queued["job_id"]))
        repeated = service.cancel(str(queued["job_id"]))
        assert cancelled["status"] == repeated["status"] == "cancelled"

        third = service.create(_request(source))
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            first_state = service.get(str(first["job_id"]))
            third_state = service.get(str(third["job_id"]))
            if first_state["status"] == third_state["status"] == "completed":
                break
            time.sleep(0.01)
        assert first_state["status"] == "completed"
        assert third_state["status"] == "completed"
        assert service.get(str(queued["job_id"]))["status"] == "cancelled"
        assert invoked == [first["job_id"], third["job_id"]]
        assert service.active_job_count() == 0
    finally:
        release.set()
        service.shutdown()


def test_cancel_after_durable_v3_final_seal_preserves_completed_with_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.annual_recovery import validate_annual_recovery_parent
    from finagent.core.model_call_ledger import open_admission_gate

    run, request, settings = _write_strict_v3_recovery_fixture(
        tmp_path,
        suffix=193,
        state="completed_with_issues",
        is_partial=False,
    )
    run_id = str(run["run_id"])
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    archive_before = load_annual_report(tmp_path, run_id)
    assert archive_before.snapshot == {
        "state": "completed_with_issues",
        "sequence": 1,
        "is_partial": False,
        "parent_run_id": None,
    }
    assert validate_annual_recovery_parent(
        tmp_path,
        run_id,
        request,
        settings=settings,
        archive=archive_before,
    )["can_resume"] is True

    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    job_id = f"annual-job-{uuid4().hex}"
    service = jobs_module.AnnualAnalysisJobService(
        tmp_path,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    job_path = _write_persisted_job(
        tmp_path,
        job_id=job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )
    job_dir = job_path.parent
    gate_db = job_dir / "call-gate.sqlite3"
    open_admission_gate(gate_db, job_id)
    job_state = json.loads(job_path.read_text(encoding="utf-8"))
    job_state.update(
        stage="annual_analysis_cli",
        _cli_owner_expected=True,
        _call_gate_db=str(gate_db),
    )
    job_path.write_text(json.dumps(job_state), encoding="utf-8")
    checkpoint_path = run["run_dir"] / "checkpoint-current.json"
    manifest_path = run["run_dir"] / "manifest.json"
    report_path = run["report_dir"] / "report.json"
    durable_files_before = tuple(path.read_bytes() for path in (checkpoint_path, manifest_path, report_path))
    try:
        # This is the durable-seal / still-running-job race. Strict reader and
        # recovery helper validate the v3 seal. Completion remains visible
        # while resume stays closed until the exact runner stop marker arrives.
        service._finish_cancelled(job_id)
        projected = service.get(job_id)
        assert projected["status"] == "completed_with_issues"
        assert projected["error"]["code"] == "process_stop_unconfirmed"
        assert projected["result_availability"]["status"] == "available"
        assert projected["result_availability"]["snapshot_state"] == "completed_with_issues"
        assert projected["result_availability"]["snapshot_sequence"] == 1
        assert projected["result_availability"]["is_partial"] is False
        assert projected["recovery_capability"] == {"can_resume": False, "reason": "parent_active"}
        assert tuple(path.read_bytes() for path in (checkpoint_path, manifest_path, report_path)) == durable_files_before
        with pytest.raises(jobs_module.AnnualAnalysisJobError) as pending:
            service.resume(job_id, run_id)
        assert pending.value.code == "parent_active"

        (job_dir / "call-stopped").write_bytes(f"{run_id}\n".encode("utf-8"))
        projected = service.get(job_id)
        assert projected["status"] == "completed_with_issues"
        assert projected["error"] is None
        assert projected["recovery_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
        parent_job_bytes = job_path.read_bytes()
        assert json.loads(parent_job_bytes)["status"] == "completed_with_issues"

        child = service.resume(job_id, run_id)
        assert child["status"] == "queued"
        assert child["recovery"]["parent_run_id"] == run_id
        assert child["request"] == parent_request
        assert job_path.read_bytes() == parent_job_bytes
        assert tuple(path.read_bytes() for path in (checkpoint_path, manifest_path, report_path)) == durable_files_before
    finally:
        service.shutdown()


def test_service_cancel_checkpoint_is_strictly_readable_and_resumable_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.annual_recovery import validate_annual_recovery_parent

    run, request, settings = _write_strict_v3_recovery_fixture(
        tmp_path,
        suffix=194,
        state="running",
        is_partial=True,
    )
    run_id = str(run["run_id"])
    parent_job_id = f"annual-job-{uuid4().hex}"
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    service = jobs_module.AnnualAnalysisJobService(
        tmp_path,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    # The child remains queued for the duplicate-resume check; no CLI/provider
    # code runs in this test.
    service._execute = lambda _job_id: None
    parent_path = _write_persisted_job(
        tmp_path,
        job_id=parent_job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )
    try:
        service._finish_cancelled(parent_job_id)

        archive = load_annual_report(tmp_path, run_id)
        assert archive.snapshot == {
            "state": "cancelled",
            "sequence": 2,
            "is_partial": True,
            "parent_run_id": None,
        }
        assert archive.manifest_summary["status"] == "interrupted"
        capability = validate_annual_recovery_parent(
            tmp_path,
            run_id,
            request,
            settings=settings,
            archive=archive,
        )
        assert capability["can_resume"] is True

        projected = service.get(parent_job_id)
        assert projected["status"] == "cancelled"
        assert projected["error"] is None
        assert projected["result_availability"]["snapshot_state"] == "cancelled"
        assert projected["result_availability"]["snapshot_sequence"] == 2
        assert projected["result_availability"]["is_partial"] is True
        assert projected["recovery_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
        parent_bytes_after_cancel = parent_path.read_bytes()

        child = service.resume(parent_job_id, run_id)
        assert child["status"] == "queued"
        assert child["request"] == parent_request
        assert child["recovery_progress"]["parent_run_id"] == run_id
        assert child["recovery_progress"]["parent_provider_admission_count"] == 0
        child_path = tmp_path / "artifacts" / "annual-analysis-jobs" / str(child["job_id"]) / "job.json"
        child_state = json.loads(child_path.read_text(encoding="utf-8"))
        assert child_state["parent_job_id"] == parent_job_id
        assert child_state["parent_run_id"] == run_id
        assert parent_path.read_bytes() == parent_bytes_after_cancel

        with pytest.raises(jobs_module.AnnualAnalysisJobError) as duplicate:
            service.resume(parent_job_id, run_id)
        assert duplicate.value.code == "already_has_active_child"
    finally:
        service.shutdown()


def test_sealed_completed_with_issues_without_unfinished_ai_is_not_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.annual_recovery import validate_annual_recovery_parent

    run, request, settings = _write_strict_v3_recovery_fixture(
        tmp_path,
        suffix=195,
        state="completed_with_issues",
        is_partial=False,
        discovery_complete=True,
    )
    run_id = str(run["run_id"])
    archive = load_annual_report(tmp_path, run_id)
    discovery = archive.report["model_discovery"]
    assert archive.snapshot is not None and archive.snapshot["is_partial"] is False
    assert discovery["coverage"]["unattempted_batches"] == 0
    assert all(row["state"] == "succeeded" for row in discovery["execution_contract"]["batches"])
    capability = validate_annual_recovery_parent(
        tmp_path,
        run_id,
        request,
        settings=settings,
        archive=archive,
    )
    assert capability["can_resume"] is False
    assert capability["reason"] == "recovery_nothing_to_resume"
    parent_job_id = f"annual-job-{uuid4().hex}"
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    service = jobs_module.AnnualAnalysisJobService(
        tmp_path,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    parent_path = _write_persisted_job(
        tmp_path,
        job_id=parent_job_id,
        status="completed_with_issues",
        request=parent_request,
        run_id=run_id,
    )
    parent_bytes = parent_path.read_bytes()
    try:
        projected = service.get(parent_job_id)
        assert projected["recovery_capability"] == {
            "can_resume": False,
            "reason": "recovery_not_available",
        }
        with pytest.raises(jobs_module.AnnualAnalysisJobError) as rejected:
            service.resume(parent_job_id, run_id)
        assert rejected.value.code == "recovery_not_available"
        assert parent_path.read_bytes() == parent_bytes
        assert list((tmp_path / "artifacts" / "annual-analysis-jobs").iterdir()) == [parent_path.parent]
    finally:
        service.shutdown()


def test_windows_safe_heartbeat_refreshes_regular_file_without_stopping_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = f"annual-job-{uuid4().hex}"
    root = tmp_path.resolve()
    job_dir = root / "artifacts" / "annual-analysis-jobs" / job_id
    job_dir.mkdir(parents=True)
    heartbeat_path = job_dir / "call-liveness"
    service = jobs_module.AnnualAnalysisJobService(
        root,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    monkeypatch.setattr(jobs_module, "_CALL_LIVENESS_INTERVAL_SECONDS", 0.02)
    stop = service._start_heartbeat(job_id, heartbeat_path)
    try:
        old_ns = time.time_ns() - 10_000_000_000
        os.utime(heartbeat_path, ns=(old_ns, old_ns))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and heartbeat_path.stat().st_mtime_ns <= old_ns:
            time.sleep(0.01)
        thread = service._heartbeats[job_id][1]
        assert heartbeat_path.stat().st_mtime_ns > old_ns
        assert thread.is_alive()
        assert job_id not in service._liveness_failed
    finally:
        service._stop_heartbeat(job_id, stop)
        service.shutdown()


def test_failed_process_stop_keeps_partial_checkpoint_readable_and_not_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report

    root = tmp_path.resolve()
    run, request, settings = _write_strict_v3_recovery_fixture(
        root,
        suffix=196,
        state="running",
        is_partial=True,
    )
    run_id = str(run["run_id"])
    job_id = f"annual-job-{uuid4().hex}"
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    service = jobs_module.AnnualAnalysisJobService(
        root,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    _write_persisted_job(
        root,
        job_id=job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )

    class UnstoppableProcess:
        pid = 424242

        @staticmethod
        def poll() -> None:
            return None

    process = UnstoppableProcess()
    service._processes[job_id] = process  # type: ignore[assignment]
    monkeypatch.setattr(service, "_terminate_owned_process", lambda _process: False)
    checkpoint_path = run["run_dir"] / "checkpoint-current.json"
    manifest_path = run["run_dir"] / "manifest.json"
    report_path = run["report_dir"] / "report.json"
    before = tuple(path.read_bytes() for path in (checkpoint_path, manifest_path))

    try:
        service._finish_cancelled(job_id)
        current = service.get(job_id)
        archive = load_annual_report(root, run_id)
        assert current["status"] == "running"
        assert current["stage"] == "cancellation_requested"
        assert current["error"]["code"] == "process_stop_unconfirmed"
        assert current["recovery_capability"] == {"can_resume": False, "reason": "parent_active"}
        assert archive.snapshot == {
            "state": "running",
            "sequence": 1,
            "is_partial": True,
            "parent_run_id": None,
        }
        assert tuple(path.read_bytes() for path in (checkpoint_path, manifest_path)) == before
        assert not report_path.exists()
        with pytest.raises(jobs_module.AnnualAnalysisJobError) as error:
            service.resume(job_id, run_id)
        assert error.value.code == "parent_active"
    finally:
        service._processes.pop(job_id, None)
        service.shutdown()


def test_exact_stopped_marker_allows_terminal_partial_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.annual_recovery import validate_annual_recovery_parent

    root = tmp_path.resolve()
    run, request, settings = _write_strict_v3_recovery_fixture(
        root,
        suffix=197,
        state="running",
        is_partial=True,
    )
    run_id = str(run["run_id"])
    job_id = f"annual-job-{uuid4().hex}"
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    service = jobs_module.AnnualAnalysisJobService(
        root,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    _write_persisted_job(
        root,
        job_id=job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )
    job_path = tmp_path / "artifacts" / "annual-analysis-jobs" / job_id / "job.json"
    state = json.loads(job_path.read_text(encoding="utf-8"))
    state.update(stage="annual_analysis_cli", _cli_owner_expected=True)
    job_path.write_text(json.dumps(state), encoding="utf-8")
    (job_path.parent / "call-stopped").write_bytes(f"{run_id}\n".encode("utf-8"))
    try:
        service._finish_cancelled(job_id)
        archive = load_annual_report(root, run_id)
        projected = service.get(job_id)
        assert archive.snapshot == {
            "state": "cancelled",
            "sequence": 2,
            "is_partial": True,
            "parent_run_id": None,
        }
        assert archive.manifest_summary["status"] == "interrupted"
        assert projected["status"] == "cancelled"
        assert projected["error"] is None
        assert validate_annual_recovery_parent(
            root,
            run_id,
            request,
            settings=settings,
            archive=archive,
        )["can_resume"] is True
    finally:
        service.shutdown()


def test_terminate_owned_process_returns_false_when_kill_and_wait_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StuckProcess:
        pid = 424243

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def kill() -> None:
            raise OSError("test kill denied")

        @staticmethod
        def wait(timeout: float | None = None) -> None:
            raise subprocess.TimeoutExpired(["controlled-process"], timeout or 0)

    if os.name == "nt":
        monkeypatch.setattr(
            jobs_module.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
        )
    else:
        monkeypatch.setattr(jobs_module.os, "killpg", lambda *_args: (_ for _ in ()).throw(OSError("denied")))

    assert jobs_module.AnnualAnalysisJobService._terminate_owned_process(StuckProcess()) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("failure_kind", "expected_failure_code", "fixture_suffix"),
    [("timeout", "analysis_timeout", 205), ("worker", "analysis_worker_failed", 207)],
)
def test_timeout_keeps_partial_read_only_until_exact_runner_stop_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_failure_code: str,
    fixture_suffix: int,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.model_call_ledger import open_admission_gate

    root = tmp_path.resolve()
    run, request, settings = _write_strict_v3_recovery_fixture(
        root,
        suffix=fixture_suffix,
        state="running",
        is_partial=True,
    )
    run_id = str(run["run_id"])
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    service = jobs_module.AnnualAnalysisJobService(
        root,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    job_id = f"annual-job-{uuid4().hex}"
    job_path = _write_persisted_job(
        root,
        job_id=job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )
    job_dir = job_path.parent
    gate_db = job_dir / "call-gate.sqlite3"
    open_admission_gate(gate_db, job_id)
    state = json.loads(job_path.read_text(encoding="utf-8"))
    state.update(
        stage="annual_analysis_cli",
        _cli_owner_expected=True,
        _call_gate_db=str(gate_db),
    )
    job_path.write_text(json.dumps(state), encoding="utf-8")

    class UnstoppableProcess:
        pid = 424244

        @staticmethod
        def poll() -> None:
            return None

    process = UnstoppableProcess()
    service._processes[job_id] = process  # type: ignore[assignment]
    monkeypatch.setattr(service, "_terminate_owned_process", lambda _process: False)
    checkpoint_path = run["run_dir"] / "checkpoint-current.json"
    manifest_path = run["run_dir"] / "manifest.json"
    before = (checkpoint_path.read_bytes(), manifest_path.read_bytes())
    try:
        service._finish_cli_failure_after_stop(
            job_id,
            run_id,
            failure_kind=failure_kind,
            code=expected_failure_code,
            message=(
                "年度分析超过配置的运行时限，分析进程已终止；请检查 PDF 后重新提交。"
                if failure_kind == "timeout"
                else "年度分析任务执行失败；请检查运行归档并重新提交。"
            ),
        )
        pending = service.get(job_id)
        archive = load_annual_report(root, run_id)
        assert pending["status"] == "interrupted"
        assert pending["error"]["code"] == "process_stop_unconfirmed"
        assert pending["recovery_capability"] == {"can_resume": False, "reason": "parent_active"}
        assert archive.snapshot == {
            "state": "running",
            "sequence": 1,
            "is_partial": True,
            "parent_run_id": None,
        }
        assert (checkpoint_path.read_bytes(), manifest_path.read_bytes()) == before
        assert (job_dir / "call-cancel").read_bytes() == b"cancelled\n"
        with sqlite3.connect(gate_db) as connection:
            assert connection.execute(
                "SELECT closed FROM call_gates WHERE gate_key = ?", (job_id,)
            ).fetchone() == (1,)
        with pytest.raises(jobs_module.AnnualAnalysisJobError) as error:
            service.resume(job_id, run_id)
        assert error.value.code == "parent_active"

        (job_dir / "call-stopped").write_bytes(f"{run_id}\n".encode("utf-8"))
        settled = service.get(job_id)
        settled_archive = load_annual_report(root, run_id)
        assert settled["status"] == "failed"
        assert settled["error"]["code"] == expected_failure_code
        assert settled_archive.snapshot == {
            "state": "failed",
            "sequence": 2,
            "is_partial": True,
            "parent_run_id": None,
        }
        assert settled["recovery_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
    finally:
        service._processes.pop(job_id, None)
        service.shutdown()


def test_timeout_seals_failed_partial_only_after_owned_process_exit_is_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.model_call_ledger import open_admission_gate

    root = tmp_path.resolve()
    run, request, settings = _write_strict_v3_recovery_fixture(
        root,
        suffix=206,
        state="running",
        is_partial=True,
    )
    run_id = str(run["run_id"])
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    service = jobs_module.AnnualAnalysisJobService(
        root,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    job_id = f"annual-job-{uuid4().hex}"
    job_path = _write_persisted_job(
        root,
        job_id=job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )
    job_dir = job_path.parent
    gate_db = job_dir / "call-gate.sqlite3"
    open_admission_gate(gate_db, job_id)
    state = json.loads(job_path.read_text(encoding="utf-8"))
    state.update(
        stage="annual_analysis_cli",
        _cli_owner_expected=True,
        _call_gate_db=str(gate_db),
    )
    job_path.write_text(json.dumps(state), encoding="utf-8")

    class StoppedProcess:
        pid = 424245
        stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

    process = StoppedProcess()

    def observe_stop(_process: object) -> bool:
        process.stopped = True
        return True

    service._processes[job_id] = process  # type: ignore[assignment]
    monkeypatch.setattr(service, "_terminate_owned_process", observe_stop)
    try:
        service._finish_cli_failure_after_stop(
            job_id,
            run_id,
            failure_kind="timeout",
            code="analysis_timeout",
            message="年度分析超过配置的运行时限，分析进程已终止；请检查 PDF 后重新提交。",
        )
        settled = service.get(job_id)
        archive = load_annual_report(root, run_id)
        assert process.poll() == 0
        assert settled["status"] == "failed"
        assert settled["error"]["code"] == "analysis_timeout"
        assert settled["result_availability"]["status"] == "available"
        assert archive.snapshot == {
            "state": "failed",
            "sequence": 2,
            "is_partial": True,
            "parent_run_id": None,
        }
        assert archive.manifest_summary["status"] == "failed"
        assert settled["recovery_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
    finally:
        service._processes.pop(job_id, None)
        service.shutdown()


def test_restart_waits_for_exact_runner_stop_before_sealing_and_resuming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.annual_recovery import validate_annual_recovery_parent
    from finagent.core.model_call_ledger import open_admission_gate

    run, request, settings = _write_strict_v3_recovery_fixture(
        tmp_path,
        suffix=196,
        state="running",
        is_partial=True,
    )
    run_id = str(run["run_id"])
    parent_job_id = f"annual-job-{uuid4().hex}"
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    parent_path = _write_persisted_job(
        tmp_path,
        job_id=parent_job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )
    job_dir = parent_path.parent
    gate_db = job_dir / "call-gate.sqlite3"
    open_admission_gate(gate_db, parent_job_id)
    state = json.loads(parent_path.read_text(encoding="utf-8"))
    state.update(
        stage="annual_analysis_cli",
        _call_gate_db=str(gate_db),
        _runner_owner={"pid": 424242, "start_token": "test-owner-start-token"},
        recovery_progress={
            "contract_version": "fintrace_annual_recovery_v1",
            "parent_run_id": None,
            "parent_provider_admission_count": 0,
            "current_provider_admission_count": 0,
            "cumulative_provider_admission_count": 0,
            "reused_batch_count": 0,
            "updated_at": "2026-10-06T00:00:01+00:00",
        },
    )
    parent_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    monkeypatch.setattr(jobs_module, "_process_start_token_for_pid", lambda _pid: ("alive", "test-owner-start-token"))
    monkeypatch.setattr(jobs_module, "_RECOVERY_OWNER_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(jobs_module, "_RECOVERY_OWNER_POLL_INTERVAL_SECONDS", 0.001)
    service = jobs_module.AnnualAnalysisJobService(
        tmp_path,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    try:
        pending = service.get(parent_job_id)
        assert pending["status"] == "interrupted"
        assert pending["result_availability"]["status"] == "available"
        assert pending["result_availability"]["is_partial"] is True
        assert pending["result_availability"]["snapshot_state"] == "running"
        assert pending["recovery_capability"] == {"can_resume": False, "reason": "parent_active"}
        with pytest.raises(jobs_module.AnnualAnalysisJobError) as active_owner:
            service.resume(parent_job_id, run_id)
        assert active_owner.value.code == "parent_active"

        before_ack = load_annual_report(tmp_path, run_id)
        assert before_ack.snapshot is not None and before_ack.snapshot["state"] == "running"
        (job_dir / "call-stopped").write_bytes((run_id + "\n").encode("utf-8"))

        settled = service.get(parent_job_id)
        assert settled["status"] == "interrupted"
        assert settled["result_availability"]["snapshot_state"] == "interrupted"
        assert settled["recovery_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
        parent_bytes = parent_path.read_bytes()
        stable_archive = load_annual_report(tmp_path, run_id)
        capability = validate_annual_recovery_parent(
            tmp_path,
            run_id,
            request,
            settings=settings,
            archive=stable_archive,
        )
        assert capability["can_resume"] is True

        child = service.resume(parent_job_id, run_id)
        assert child["status"] == "queued"
        assert child["recovery"]["parent_run_id"] == run_id
        assert parent_path.read_bytes() == parent_bytes
        assert load_annual_report(tmp_path, run_id).snapshot == stable_archive.snapshot
    finally:
        service.shutdown()


def test_restart_preserves_failed_manifest_partial_pointer_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finagent.api.annual_report_read import load_annual_report
    from finagent.core.annual_recovery import validate_annual_recovery_parent
    from finagent.core.model_call_ledger import open_admission_gate

    run, request, settings = _write_strict_v3_recovery_fixture(
        tmp_path,
        suffix=197,
        state="running",
        is_partial=True,
    )
    run_id = str(run["run_id"])
    manifest_path = run["run_dir"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert load_annual_report(tmp_path, run_id).snapshot["state"] == "running"

    parent_job_id = f"annual-job-{uuid4().hex}"
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    parent_path = _write_persisted_job(
        tmp_path,
        job_id=parent_job_id,
        status="running",
        request=parent_request,
        run_id=run_id,
    )
    job_dir = parent_path.parent
    gate_db = job_dir / "call-gate.sqlite3"
    open_admission_gate(gate_db, parent_job_id)
    state = json.loads(parent_path.read_text(encoding="utf-8"))
    state.update(
        stage="annual_analysis_cli",
        _call_gate_db=str(gate_db),
        _runner_owner={"pid": 424243, "start_token": "test-owner-start-token"},
    )
    parent_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: settings)
    monkeypatch.setattr(jobs_module, "_process_start_token_for_pid", lambda _pid: ("alive", "test-owner-start-token"))
    monkeypatch.setattr(jobs_module, "_RECOVERY_OWNER_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(jobs_module, "_RECOVERY_OWNER_POLL_INTERVAL_SECONDS", 0.001)
    service = jobs_module.AnnualAnalysisJobService(
        tmp_path,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    try:
        pending = service.get(parent_job_id)
        assert pending["result_availability"]["snapshot_state"] == "running"
        assert pending["recovery_capability"] == {"can_resume": False, "reason": "parent_active"}

        (job_dir / "call-stopped").write_bytes((run_id + "\n").encode("utf-8"))
        settled = service.get(parent_job_id)
        archive = load_annual_report(tmp_path, run_id)
        assert archive.manifest_summary["status"] == "failed"
        assert archive.snapshot is not None and archive.snapshot["state"] == "failed"
        assert settled["status"] == "interrupted"
        assert settled["error"]["code"] == "service_interrupted"
        assert settled["result_availability"]["snapshot_state"] == "failed"
        assert settled["recovery_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
        assert validate_annual_recovery_parent(
            tmp_path,
            run_id,
            request,
            settings=settings,
            archive=archive,
        )["can_resume"] is True
        parent_bytes = parent_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        pointer_bytes = (run["run_dir"] / "checkpoint-current.json").read_bytes()

        child = service.resume(parent_job_id, run_id)
        assert child["status"] == "queued"
        assert parent_path.read_bytes() == parent_bytes
        assert manifest_path.read_bytes() == manifest_bytes
        assert (run["run_dir"] / "checkpoint-current.json").read_bytes() == pointer_bytes
    finally:
        service.shutdown()


def test_owner_witness_does_not_infer_stopped_from_reused_or_unknown_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = {"pid": 424244, "start_token": "old-process"}
    monkeypatch.setattr(jobs_module, "_process_start_token_for_pid", lambda _pid: ("alive", "new-process"))
    assert jobs_module._runner_owner_status(owner) == "unknown"

    monkeypatch.setattr(jobs_module, "_process_start_token_for_pid", lambda _pid: ("unknown", None))
    assert jobs_module._runner_owner_status(owner) == "unknown"

    monkeypatch.setattr(jobs_module, "_process_start_token_for_pid", lambda _pid: ("stopped", None))
    assert jobs_module._runner_owner_status(owner) == "stopped"


def test_owner_witness_tracks_only_a_process_created_by_this_test() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR"}
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    witness = None
    try:
        witness = jobs_module._runner_owner_witness(process)
        assert witness is not None
        assert witness["pid"] == process.pid
        assert jobs_module._runner_owner_status(witness) == "alive"
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert witness is not None
    assert jobs_module._runner_owner_status(witness) == "stopped"


def test_restart_owner_wait_is_bounded_and_stop_marker_is_run_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(jobs_module.AnnualAnalysisJobService)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    run_id = f"annual-analysis-{uuid4()}"
    owner = {"pid": 424245, "start_token": "unknown-owner"}
    monkeypatch.setattr(jobs_module, "_process_start_token_for_pid", lambda _pid: ("unknown", None))
    monkeypatch.setattr(jobs_module, "_RECOVERY_OWNER_WAIT_SECONDS", 0.04)
    monkeypatch.setattr(jobs_module, "_RECOVERY_OWNER_POLL_INTERVAL_SECONDS", 0.002)
    started = time.monotonic()
    assert service._wait_for_prior_runner_stop(job_dir, {"_runner_owner": owner}, run_id) is False
    elapsed = time.monotonic() - started
    assert 0.03 <= elapsed < 0.5

    (job_dir / "call-stopped").write_bytes((run_id + "-other\n").encode("utf-8"))
    assert jobs_module._call_stopped_marker_matches(job_dir, run_id) is False
    (job_dir / "call-stopped").write_bytes((run_id + "\n").encode("utf-8"))
    assert jobs_module._call_stopped_marker_matches(job_dir, run_id) is True


def test_service_exposes_and_creates_one_child_for_sealed_recoverable_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source(tmp_path)
    request = _request(source, mode="model_investigation")
    parent_run_id = f"annual-analysis-{uuid4()}"
    parent_job_id = f"annual-job-{uuid4().hex}"
    parent_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / parent_job_id
    parent_dir.mkdir(parents=True)
    parent_request = {key: request[key] for key in (
        "company_id", "report_year", "document_id", "source_pdf_path", "sha256", "mode",
    )}
    parent_state = {
        "job_id": parent_job_id,
        "status": "completed_with_issues",
        "stage": "completed_with_issues",
        "created_at": "2026-10-06T00:00:00+00:00",
        "updated_at": "2026-10-06T00:00:01+00:00",
        "request": parent_request,
        "run_id": parent_run_id,
        "result_url": f"/v1/annual-analyses/{parent_run_id}",
        "error": None,
    }
    parent_state_path = parent_dir / "job.json"
    parent_state_path.write_text(json.dumps(parent_state), encoding="utf-8")
    parent_bytes = parent_state_path.read_bytes()
    archive = SimpleNamespace(
        run_id=parent_run_id,
        report={
            "run_id": parent_run_id,
            "company_id": parent_request["company_id"],
            "report_year": parent_request["report_year"],
            "source_document_id": parent_request["document_id"],
            "source_sha256": parent_request["sha256"],
            "model_discovery": {
                "execution_contract": {
                    "provider_admission_count": 2,
                    "reused_batches": [1],
                    "parent_run_id": None,
                    "batches": [{"batch_index": 2, "state": "unattempted"}],
                },
            },
        },
        manifest_summary={
            "status": "completed_with_issues",
            "company_id": parent_request["company_id"],
            "report_year": parent_request["report_year"],
            "document_id": parent_request["document_id"],
            "source_pdf_sha256": parent_request["sha256"],
        },
        snapshot={"state": "completed_with_issues", "sequence": 2, "is_partial": False, "parent_run_id": None},
    )
    constructed_settings = ModelSettings(
        base_url="http://127.0.0.1:9/v1",
        api_key="constructed-test-only",
        model="local-fixture-model",
        timeout_seconds=1,
    )
    monkeypatch.setattr(jobs_module, "load_annual_report", lambda _root, _run: archive)
    monkeypatch.setattr(jobs_module, "resolve_model_settings", lambda _root: constructed_settings)
    validations: list[str] = []

    def validate_parent(_root, run_id, _request, *, settings=None, archive=None):
        validations.append(run_id)
        assert settings is constructed_settings
        assert archive is not None and archive.snapshot["is_partial"] is False
        return {"can_resume": True, "reason": None, "binding": {"fixture": True}}

    monkeypatch.setattr(jobs_module, "validate_annual_recovery_parent", validate_parent)
    service = jobs_module.AnnualAnalysisJobService(
        tmp_path,
        api_runtime_identity=capture_cli_runtime_identity(),
    )
    service._ensure_runtime_compatible = lambda: None
    service._execute = lambda _job_id: None
    try:
        parent = service.get(parent_job_id)
        assert parent["status"] == "completed_with_issues"
        assert parent["recovery_capability"] == {"can_resume": True, "reason": "recoverable_batches"}

        child = service.resume(parent_job_id, parent_run_id)
        assert child["job_id"] != parent_job_id
        assert child["request"] == parent_request
        assert child["recovery"]["parent_run_id"] == parent_run_id
        child_state_path = tmp_path / "artifacts" / "annual-analysis-jobs" / child["job_id"] / "job.json"
        assert json.loads(child_state_path.read_text(encoding="utf-8"))["parent_job_id"] == parent_job_id
        assert parent_state_path.read_bytes() == parent_bytes
        assert validations.count(parent_run_id) >= 2

        with pytest.raises(jobs_module.AnnualAnalysisJobError) as duplicate:
            service.resume(parent_job_id, parent_run_id)
        assert duplicate.value.code == "already_has_active_child"
    finally:
        service._executor.shutdown(wait=True, cancel_futures=True)


def test_restart_marks_running_job_interrupted(tmp_path: Path) -> None:
    job_id = f"annual-job-{uuid4().hex}"
    job_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / job_id
    job_dir.mkdir(parents=True)
    state = {
        "job_id": job_id,
        "status": "running",
        "stage": "annual_analysis_cli",
        "created_at": "2026-09-28T00:00:00+00:00",
        "updated_at": "2026-09-28T00:00:00+00:00",
        "request": {},
        "run_id": None,
        "result_url": None,
        "error": None,
    }
    (job_dir / "job.json").write_text(json.dumps(state), encoding="utf-8")

    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analysis-jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "interrupted"
    assert response.json()["stage"] == "interrupted"
    assert response.json()["error"]["code"] == "service_interrupted"


def test_importing_api_submodule_does_not_construct_app_or_recover_jobs(tmp_path: Path) -> None:
    backend_src = Path(jobs_module.__file__).resolve().parents[2]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(backend_src), existing_pythonpath) if part
    )
    check_import = (
        "import sys; "
        "import finagent.api.annual_report_read; "
        "import finagent.api.annual_analysis_jobs as jobs; "
        "assert 'finagent.api.app' not in sys.modules; "
        "assert not jobs._SERVICE_REGISTRY; "
        "from finagent.api import create_app; "
        "assert callable(create_app)"
    )

    result = subprocess.run(
        [sys.executable, "-c", check_import],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
