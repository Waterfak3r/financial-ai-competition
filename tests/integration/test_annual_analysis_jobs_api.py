from __future__ import annotations

import hashlib
import json
import os
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
from finagent.core.model_settings_store import save_local_settings


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


def _write_success_archive(
    project_root: Path,
    request: dict[str, object],
    *,
    status: str = "completed",
) -> str:
    run_id = f"annual-analysis-{uuid4()}"
    run_dir = project_root / "artifacts" / "runs" / run_id
    report_dir = project_root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    source = project_root / "data" / "raw" / str(request["source_pdf_path"])
    (run_dir / "source.pdf").write_bytes(source.read_bytes())
    manifest = {
        "run_id": run_id,
        "status": status,
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
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (report_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return run_id


def _successful_runner(project_root: Path, _artifacts_root: Path, request: dict[str, object]) -> CliResult:
    run_id = _write_success_archive(project_root, request)
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
        lambda _project_root=None: {"model_investigation": {"available": False, "status": "not_configured"}},
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
    expected = {"model_investigation": {"available": False, "status": "not_configured"}}
    monkeypatch.setattr(jobs_module, "get_annual_analysis_capabilities", lambda _project_root=None: expected)
    client = TestClient(create_app(tmp_path))

    response = client.get("/v1/annual-analysis-capabilities")

    assert response.status_code == 200
    assert response.json() == expected
    assert "mock-secret-value" not in response.text
    assert "private.example.invalid" not in response.text


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
        _request(source),
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
        _request(source, mode="model_investigation"),
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
        _request(source, mode="model_investigation"),
    )
    deterministic_result = jobs_module._invoke_cli(
        tmp_path,
        tmp_path / "artifacts",
        _request(source, mode="deterministic"),
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


def test_invalid_cli_timeout_configuration_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS", "901")

    with pytest.raises(RuntimeError, match="60 到 900"):
        jobs_module._configured_cli_timeout_seconds()


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
