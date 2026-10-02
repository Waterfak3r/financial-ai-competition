from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import finagent.api.annual_analysis_history as history_module
from finagent.api.app import create_app
from finagent.api.annual_analysis_history import (
    AnnualAnalysisHistoryError,
    list_annual_analysis_history,
)


def _run_id(number: int) -> str:
    return f"annual-analysis-{UUID(int=number)}"


def _write_run(
    root: Path,
    number: int,
    *,
    status: str = "completed",
    created_at: str | None = "2026-09-30T12:00:00+00:00",
    document_id: str = "doc-1",
    report: bool = True,
    mode_fields: dict[str, object] | None = None,
) -> str:
    run_id = _run_id(number)
    run_dir = root / "artifacts" / "runs" / run_id
    report_dir = root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    source_pdf = b"%PDF-1.7\n"
    source_sha256 = hashlib.sha256(source_pdf).hexdigest()
    if report:
        report_dir.mkdir(parents=True)
        report_payload = {
            "kind": "fintrace_annual_analysis_report",
            "run_id": run_id,
            "company_id": "603288",
            "report_year": 2024,
            "source_document_id": document_id,
            "source_sha256": source_sha256,
        }
        (report_dir / "report.json").write_text(json.dumps(report_payload), encoding="utf-8")
    (run_dir / "source.pdf").write_bytes(source_pdf)
    manifest: dict[str, object] = {
        "run_id": run_id,
        "status": status,
        "company_id": "603288",
        "report_year": 2024,
        "document_id": document_id,
        "inputs": {
            "source_pdf_original_filename": "annual.pdf",
            "source_pdf_archive_path": f"runs/{run_id}/source.pdf",
            "source_pdf_sha256": source_sha256,
        },
        "outputs": {
            "run_dir": f"runs/{run_id}",
            "report_dir": f"reports/{run_id}",
            "report_json": f"reports/{run_id}/report.json",
        },
    }
    if created_at is not None:
        manifest["created_at"] = created_at
    if mode_fields:
        manifest.update(mode_fields)
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_id


def _write_job(
    root: Path,
    job_id: str,
    *,
    status: str,
    run_id: str | None = None,
    created_at: str | None = "2026-09-30T12:00:00+00:00",
    document_id: str = "doc-1",
    request_mode: str = "deterministic",
    source_sha256: str = hashlib.sha256(b"%PDF-1.7\n").hexdigest(),
) -> None:
    job_dir = root / "artifacts" / "annual-analysis-jobs" / job_id
    job_dir.mkdir(parents=True)
    payload: dict[str, object] = {
        "job_id": job_id,
        "status": status,
        "request": {
            "company_id": "603288",
            "report_year": 2024,
            "document_id": document_id,
            "source_pdf_path": f"603288/2024/{document_id}/annual.pdf",
            "mode": request_mode,
            "sha256": source_sha256,
        },
        "run_id": run_id,
    }
    if created_at is not None:
        payload["created_at"] = created_at
    (job_dir / "job.json").write_text(json.dumps(payload), encoding="utf-8")


def test_empty_history_has_default_pagination(tmp_path: Path) -> None:
    result = list_annual_analysis_history(tmp_path)

    assert result == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_job_and_cli_run_are_deduplicated_and_statuses_stay_independent(tmp_path: Path) -> None:
    successful_run = _write_run(
        tmp_path,
        1,
        status="completed",
        mode_fields={"m3_screening": {"requested": True}},
    )
    failed_run = _write_run(
        tmp_path,
        2,
        status="failed",
        document_id="doc-2",
        report=False,
        mode_fields={
            "model_called": False,
            "model_investigation": {"requested": True, "status": "failed"},
        },
    )
    cli_run = _write_run(
        tmp_path,
        3,
        status="completed_with_issues",
        document_id="doc-3",
        mode_fields={"mode": "deterministic"},
    )
    _write_job(
        tmp_path,
        "annual-job-" + "1" * 32,
        status="failed",
        run_id=successful_run,
        request_mode="model_investigation",
    )
    _write_job(
        tmp_path,
        "annual-job-" + "3" * 32,
        status="completed",
        run_id=successful_run,
        request_mode="m3_screening",
    )
    _write_job(
        tmp_path,
        "annual-job-" + "2" * 32,
        status="interrupted",
        run_id=failed_run,
        document_id="doc-2",
    )

    result = list_annual_analysis_history(tmp_path)

    assert result["total"] == 4
    rows = {row["item_id"]: row for row in result["items"]}
    failed_job = rows["annual-job-" + "1" * 32]
    assert failed_job["status"] == "failed"
    assert failed_job["job_status"] == "failed"
    assert failed_job["run_status"] == "completed"
    assert failed_job["result_available"] is True
    assert failed_job["mode"] == "model_investigation"
    assert failed_job["run_mode"] == "m3_screening"
    assert failed_job["replay"] is True
    assert {item["code"] for item in failed_job["issues"]} == {"job_failed", "mode_mismatch"}
    duplicate_job = rows["annual-job-" + "3" * 32]
    assert duplicate_job["run_id"] == successful_run
    assert duplicate_job["mode"] == "m3_screening"
    assert duplicate_job["run_mode"] == "m3_screening"
    assert sum(row["run_id"] == successful_run for row in result["items"]) == 2

    interrupted_job = rows["annual-job-" + "2" * 32]
    assert interrupted_job["job_status"] == "interrupted"
    assert interrupted_job["run_status"] == "failed"
    assert interrupted_job["result_available"] is False
    assert interrupted_job["mode"] == "deterministic"
    assert interrupted_job["run_mode"] == "model_investigation"
    assert {item["code"] for item in interrupted_job["issues"]} == {
        "run_failed",
        "job_interrupted",
        "mode_mismatch",
    }

    cli_row = rows[cli_run]
    assert cli_row["job_id"] is None
    assert cli_row["run_id"] == cli_run
    assert cli_row["status"] == "completed_with_issues"
    assert cli_row["mode"] == "deterministic"
    assert cli_row["result_available"] is True
    assert all(row["report_type"] == "annual" for row in result["items"])


def test_manifest_without_explicit_mode_is_unknown_even_if_job_request_has_mode(tmp_path: Path) -> None:
    run_id = _write_run(tmp_path, 10, status="completed")
    _write_job(
        tmp_path,
        "annual-job-" + "a" * 32,
        status="running",
        run_id=run_id,
        request_mode="deterministic",
    )

    row = list_annual_analysis_history(tmp_path)["items"][0]

    assert row["mode"] == "deterministic"
    assert row["run_mode"] == "unknown"
    assert row["run_status"] == "completed"
    assert row["result_available"] is True


def test_pagination_uses_created_at_stably_and_reports_real_total(tmp_path: Path) -> None:
    old_id = _write_run(tmp_path, 20, created_at="2026-01-01T00:00:00+00:00")
    newest_id = _write_run(tmp_path, 21, created_at="2026-03-01T00:00:00+00:00", document_id="doc-2")
    middle_id = _write_run(tmp_path, 22, created_at="2026-02-01T00:00:00+00:00", document_id="doc-3")

    first = list_annual_analysis_history(tmp_path, limit=1, offset=0)
    second = list_annual_analysis_history(tmp_path, limit=1, offset=1)
    repeat = list_annual_analysis_history(tmp_path, limit=1, offset=1)

    assert first["total"] == second["total"] == 3
    assert first["items"][0]["item_id"] == newest_id
    assert second["items"][0]["item_id"] == middle_id
    assert repeat == second
    assert old_id not in {first["items"][0]["item_id"], second["items"][0]["item_id"]}
    assert "_sort_created" not in second["items"][0]


def test_pagination_checks_report_readability_only_for_returned_rows(tmp_path: Path, monkeypatch) -> None:
    _write_run(tmp_path, 23, created_at="2026-01-01T00:00:00+00:00")
    newest_id = _write_run(tmp_path, 24, created_at="2026-02-01T00:00:00+00:00", document_id="doc-2")
    checked: list[str] = []
    original = history_module._result_archive_available

    def track_result_check(root: Path, run_id: str, manifest: dict[str, object], source_sha256: str) -> bool:
        checked.append(run_id)
        return original(root, run_id, manifest, source_sha256)

    monkeypatch.setattr(history_module, "_result_archive_available", track_result_check)

    result = list_annual_analysis_history(tmp_path, limit=1)

    assert result["total"] == 2
    assert result["items"][0]["run_id"] == newest_id
    assert checked == [newest_id]


def test_bad_json_and_incomplete_report_do_not_break_the_list(tmp_path: Path) -> None:
    incomplete_run = _write_run(tmp_path, 30, status="completed", report=False)
    invalid_run = _run_id(31)
    invalid_dir = tmp_path / "artifacts" / "runs" / invalid_run
    invalid_dir.mkdir(parents=True)
    (invalid_dir / "manifest.json").write_text("{broken", encoding="utf-8")
    invalid_status_run = _write_run(tmp_path, 32)
    status_manifest_path = tmp_path / "artifacts" / "runs" / invalid_status_run / "manifest.json"
    status_manifest = json.loads(status_manifest_path.read_text(encoding="utf-8"))
    status_manifest["status"] = []
    status_manifest_path.write_text(json.dumps(status_manifest), encoding="utf-8")
    bad_job_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / ("annual-job-" + "b" * 32)
    bad_job_dir.mkdir(parents=True)
    (bad_job_dir / "job.json").write_text("not json", encoding="utf-8")
    bad_shape_job_dir = tmp_path / "artifacts" / "annual-analysis-jobs" / ("annual-job-" + "d" * 32)
    bad_shape_job_dir.mkdir(parents=True)
    bad_shape_job = {
        "job_id": bad_shape_job_dir.name,
        "status": [],
        "request": {
            "company_id": "603288",
            "report_year": 2024,
            "document_id": "doc-1",
            "sha256": hashlib.sha256(b"%PDF-1.7\n").hexdigest(),
        },
    }
    (bad_shape_job_dir / "job.json").write_text(json.dumps(bad_shape_job), encoding="utf-8")

    result = list_annual_analysis_history(tmp_path)

    assert result["total"] == 1
    row = result["items"][0]
    assert row["run_id"] == incomplete_run
    assert row["result_available"] is False
    assert row["issues"][0]["code"] == "result_archive_unavailable"


def test_sha_mismatch_does_not_claim_or_hide_valid_cli_run(tmp_path: Path) -> None:
    run_id = _write_run(tmp_path, 35, status="completed")
    _write_job(
        tmp_path,
        "annual-job-" + "c" * 32,
        status="failed",
        run_id=run_id,
        source_sha256="b" * 64,
    )

    result = list_annual_analysis_history(tmp_path)

    assert result["total"] == 2
    job = next(row for row in result["items"] if row["job_id"] is not None)
    cli = next(row for row in result["items"] if row["job_id"] is None)
    assert job["run_status"] is None
    assert job["result_available"] is False
    assert "run_archive_identity_mismatch" in {issue["code"] for issue in job["issues"]}
    assert cli["run_id"] == run_id
    assert cli["result_available"] is True


def test_reparse_point_run_is_ignored_and_invalid_root_link_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    (root / "artifacts" / "runs").mkdir(parents=True)
    outside.mkdir()
    run_id = _run_id(40)
    try:
        (root / "artifacts" / "runs" / run_id).symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前 Windows 环境未允许创建目录符号链接。")

    assert list_annual_analysis_history(root)["total"] == 0

    linked_root = tmp_path / "linked-project"
    linked_root.mkdir()
    (linked_root / "artifacts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AnnualAnalysisHistoryError) as error:
        list_annual_analysis_history(linked_root)
    assert error.value.code == "history_path_invalid"


def test_non_directory_artifact_root_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "artifacts").write_text("not a directory", encoding="utf-8")

    with pytest.raises(AnnualAnalysisHistoryError) as error:
        list_annual_analysis_history(tmp_path)

    assert error.value.code == "history_path_invalid"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"limit": 0}, "invalid_limit"),
        ({"limit": 51}, "invalid_limit"),
        ({"offset": -1}, "invalid_offset"),
        ({"offset": True}, "invalid_offset"),
    ],
)
def test_invalid_pagination_is_rejected(tmp_path: Path, kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(AnnualAnalysisHistoryError) as error:
        list_annual_analysis_history(tmp_path, **kwargs)  # type: ignore[arg-type]
    assert error.value.status_code == 400
    assert error.value.code == code


def test_history_http_route_paginates_and_validates_query(tmp_path: Path) -> None:
    run_id = _write_run(tmp_path, 50)
    client = TestClient(create_app(tmp_path))

    page = client.get("/v1/annual-analysis-history", params={"limit": 1, "offset": 0})
    empty = client.get("/v1/annual-analysis-history", params={"limit": 1, "offset": 99})

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 1
    assert payload["limit"] == 1
    assert payload["offset"] == 0
    assert payload["items"][0]["item_id"] == run_id
    assert payload["items"][0]["source_name"] == "annual.pdf"
    assert payload["items"][0]["result_available"] is True
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "total": 1, "limit": 1, "offset": 99}

    for limit in (0, 51):
        invalid_limit = client.get("/v1/annual-analysis-history", params={"limit": limit})
        assert invalid_limit.status_code == 422
    assert client.get("/v1/annual-analysis-history", params={"offset": -1}).status_code == 422


def test_history_http_route_shows_manager_recovered_interrupted_job(tmp_path: Path) -> None:
    job_id = "annual-job-" + "e" * 32
    _write_job(tmp_path, job_id, status="running", run_id=None)

    # Constructing an app for this isolated project root creates a fresh job
    # service, which applies the normal restart recovery behavior to job.json.
    client = TestClient(create_app(tmp_path))
    response = client.get("/v1/annual-analysis-history")

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["item_id"] == job_id
    assert row["status"] == "interrupted"
    assert row["job_status"] == "interrupted"
    assert row["run_status"] is None
    assert row["result_available"] is False
    assert "job_interrupted" in {issue["code"] for issue in row["issues"]}
    saved_state = json.loads(
        (tmp_path / "artifacts" / "annual-analysis-jobs" / job_id / "job.json").read_text(encoding="utf-8")
    )
    assert saved_state["status"] == "interrupted"
