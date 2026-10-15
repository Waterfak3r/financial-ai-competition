from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

import finagent.core.annual_checkpoints as checkpoints
from finagent.core.annual_checkpoints import (
    AnnualCheckpointError,
    load_current_annual_checkpoint,
    save_annual_checkpoint,
)

_RUN_ID = "annual-analysis-00000000-0000-0000-0000-000000000001"


def _archive(root: Path) -> tuple[bytes, dict[str, object]]:
    run_dir = root / "artifacts" / "runs" / _RUN_ID
    report_dir = root / "artifacts" / "reports" / _RUN_ID
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    source = b"%PDF-1.7\nfixture-source\n"
    source_sha256 = hashlib.sha256(source).hexdigest()
    (run_dir / "source.pdf").write_bytes(source)
    manifest = {
        "run_id": _RUN_ID,
        "status": "running",
        "company_id": "603288",
        "report_year": 2024,
        "document_id": "doc-fixture",
        "inputs": {
            "source_pdf_archive_path": f"runs/{_RUN_ID}/source.pdf",
            "source_pdf_sha256": source_sha256,
        },
        "outputs": {
            "run_dir": f"runs/{_RUN_ID}",
            "report_dir": f"reports/{_RUN_ID}",
            "report_json": f"reports/{_RUN_ID}/report.json",
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report: dict[str, object] = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": _RUN_ID,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": "doc-fixture",
        "source_sha256": source_sha256,
        "confirmed": {"metrics": [], "analyses": []},
        "pending_review": {"facts": [], "calculations": [], "claims": [], "candidate_signals": []},
    }
    return source, {
        "state": "awaiting_model",
        "is_partial": True,
        "parent_run_id": None,
        "source_sha256": source_sha256,
        "report": report,
    }


def _pointer_path(root: Path) -> Path:
    return root / "artifacts" / "runs" / _RUN_ID / "checkpoint-current.json"


def _snapshot_path(root: Path, pointer: dict[str, object]) -> Path:
    return root / Path(str(pointer["snapshot_path"]))


def _save_checkpoint_in_process(root_text: str, snapshot: dict[str, object], start, output) -> None:
    start.wait(10)
    try:
        record = save_annual_checkpoint(Path(root_text), _RUN_ID, snapshot)
        output.put(("ok", record.sequence))
    except Exception as exc:  # pragma: no cover - surfaced through the parent assertion
        output.put(("error", type(exc).__name__, getattr(exc, "code", None)))


def test_checkpoints_are_immutable_append_only_and_pointer_is_latest(tmp_path: Path) -> None:
    _, snapshot = _archive(tmp_path)

    first = save_annual_checkpoint(tmp_path, _RUN_ID, snapshot)
    pointer_after_first = json.loads(_pointer_path(tmp_path).read_text(encoding="utf-8"))
    first_path = _snapshot_path(tmp_path, pointer_after_first)
    first_bytes = first_path.read_bytes()
    assert first.sequence == 1
    assert first.metadata == {
        "state": "awaiting_model",
        "sequence": 1,
        "is_partial": True,
        "parent_run_id": None,
    }

    second_snapshot = {**snapshot, "state": "running"}
    second = save_annual_checkpoint(tmp_path, _RUN_ID, second_snapshot)
    current = load_current_annual_checkpoint(tmp_path, _RUN_ID)

    assert second.sequence == 2
    assert current is not None
    assert current.sequence == 2
    assert current.report == second_snapshot["report"]
    assert first_path.read_bytes() == first_bytes
    assert _snapshot_path(tmp_path, json.loads(_pointer_path(tmp_path).read_text(encoding="utf-8"))) != first_path


def test_pointer_publication_is_atomic_and_a_failed_publish_keeps_previous_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, snapshot = _archive(tmp_path)
    first = save_annual_checkpoint(tmp_path, _RUN_ID, snapshot)
    previous_pointer = _pointer_path(tmp_path).read_bytes()

    def fail_replace(*_: object, **__: object) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(checkpoints.os, "replace", fail_replace)
    with pytest.raises(AnnualCheckpointError, match="无法安全保存"):
        save_annual_checkpoint(tmp_path, _RUN_ID, {**snapshot, "state": "running"})

    assert _pointer_path(tmp_path).read_bytes() == previous_pointer
    current = load_current_annual_checkpoint(tmp_path, _RUN_ID)
    assert current is not None and current.sequence == first.sequence


def _winerror(code: int) -> PermissionError:
    error = PermissionError(f"simulated Windows sharing violation {code}")
    error.winerror = code  # type: ignore[attr-defined]
    return error


def test_atomic_replace_retries_only_bounded_windows_sharing_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "temporary.json"
    destination = tmp_path / "report.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    original_replace = os.replace
    calls = 0
    delays: list[float] = []

    def transient_replace(src: object, dst: object) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _winerror(5)
        original_replace(src, dst)

    monkeypatch.setattr(checkpoints.os, "replace", transient_replace)
    monkeypatch.setattr(checkpoints.time, "sleep", delays.append)
    checkpoints.replace_with_bounded_retry(source, destination)

    assert calls == 3
    assert delays == [0.01, 0.02]
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_atomic_replace_persistent_windows_lock_is_bounded_and_other_errors_are_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "temporary.json"
    destination = tmp_path / "report.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    calls = 0
    delays: list[float] = []

    def persistent_windows_lock(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        raise _winerror(32)

    monkeypatch.setattr(checkpoints.os, "replace", persistent_windows_lock)
    monkeypatch.setattr(checkpoints.time, "sleep", delays.append)
    with pytest.raises(PermissionError):
        checkpoints.replace_with_bounded_retry(source, destination)
    assert calls == 6
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16]
    assert source.read_text(encoding="utf-8") == "new"
    assert destination.read_text(encoding="utf-8") == "old"

    calls = 0

    def non_windows_permission(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError("ordinary permission denied")

    monkeypatch.setattr(checkpoints.os, "replace", non_windows_permission)
    with pytest.raises(PermissionError):
        checkpoints.replace_with_bounded_retry(source, destination)
    assert calls == 1


def test_cross_process_writers_allocate_monotonic_unique_sequences(tmp_path: Path) -> None:
    _, snapshot = _archive(tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    snapshots = [
        {**snapshot, "state": "running"},
        {**snapshot, "state": "partial"},
    ]
    workers = [
        context.Process(
            target=_save_checkpoint_in_process,
            args=(str(tmp_path), item, start, output),
        )
        for item in snapshots
    ]
    for worker in workers:
        worker.start()
    start.set()
    results = [output.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
    try:
        assert [worker.exitcode for worker in workers] == [0, 0]
        assert all(result[0] == "ok" for result in results), results
        assert sorted(result[1] for result in results) == [1, 2]
        current = load_current_annual_checkpoint(tmp_path, _RUN_ID)
        assert current is not None and current.sequence == 2
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)


def test_lock_file_initialization_by_another_process_does_not_reject_same_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, snapshot = _archive(tmp_path)
    lock_path = tmp_path / "artifacts" / "runs" / _RUN_ID / ".checkpoint-writer.lock"
    lock_path.touch()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    worker = context.Process(
        target=_save_checkpoint_in_process,
        args=(str(tmp_path), {**snapshot, "state": "running"}, start, output),
    )
    worker.start()
    original_open = checkpoints.os.open
    other_result = []

    def open_after_other_writer(path, flags, *args, **kwargs):
        if Path(path) == lock_path and not start.is_set():
            # Model the legacy initialization window without replacing the inode.
            with lock_path.open("r+b") as lock_file:
                lock_file.truncate(1)
            start.set()
            other_result.append(output.get(timeout=15))
            assert other_result == [("ok", 1)]
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(checkpoints.os, "open", open_after_other_writer)
    try:
        record = save_annual_checkpoint(tmp_path, _RUN_ID, {**snapshot, "state": "partial"})
        assert record.sequence == 2
        current = load_current_annual_checkpoint(tmp_path, _RUN_ID)
        assert current is not None and current.sequence == 2
    finally:
        worker.join(timeout=15)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
    assert worker.exitcode == 0


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda pointer: pointer.update(unexpected=True), "checkpoint_pointer_invalid"),
        (lambda pointer: pointer.update(snapshot_path="artifacts/reports/../../escape.json"), "checkpoint_pointer_invalid"),
        (lambda pointer: pointer.update(sequence=pointer["sequence"] + 1), "checkpoint_pointer_invalid"),
    ],
)
def test_unknown_or_forged_pointer_is_rejected(
    tmp_path: Path,
    mutate,
    code: str,
) -> None:
    _, snapshot = _archive(tmp_path)
    save_annual_checkpoint(tmp_path, _RUN_ID, snapshot)
    pointer = json.loads(_pointer_path(tmp_path).read_text(encoding="utf-8"))
    mutate(pointer)
    _pointer_path(tmp_path).write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(AnnualCheckpointError) as captured:
        load_current_annual_checkpoint(tmp_path, _RUN_ID)
    assert captured.value.code == code


def test_snapshot_tamper_and_source_tamper_fail_closed(tmp_path: Path) -> None:
    _, snapshot = _archive(tmp_path)
    save_annual_checkpoint(tmp_path, _RUN_ID, snapshot)
    pointer = json.loads(_pointer_path(tmp_path).read_text(encoding="utf-8"))
    snap_path = _snapshot_path(tmp_path, pointer)
    snap_path.write_bytes(snap_path.read_bytes() + b" ")
    with pytest.raises(AnnualCheckpointError) as captured:
        load_current_annual_checkpoint(tmp_path, _RUN_ID)
    assert captured.value.code == "checkpoint_snapshot_hash_mismatch"

    # Restore an intact checkpoint, then prove that source changes are bound to
    # the manifest and the original PDF bytes.
    _pointer_path(tmp_path).unlink()
    _, snapshot = _archive_second(tmp_path)
    save_annual_checkpoint(tmp_path, _RUN_ID, snapshot)
    source_path = tmp_path / "artifacts" / "runs" / _RUN_ID / "source.pdf"
    source_path.write_bytes(source_path.read_bytes() + b"tampered")
    with pytest.raises(AnnualCheckpointError) as source_error:
        load_current_annual_checkpoint(tmp_path, _RUN_ID)
    assert source_error.value.code == "checkpoint_source_mismatch"


def _archive_second(root: Path) -> tuple[bytes, dict[str, object]]:
    """Restore the fixture after a deliberately corrupted snapshot file."""

    run_dir = root / "artifacts" / "runs" / _RUN_ID
    report_dir = root / "artifacts" / "reports" / _RUN_ID
    for path in (run_dir / "checkpoint-current.json",):
        if path.exists():
            path.unlink()
    for path in (report_dir / "checkpoints").glob("*"):
        path.unlink()
    source = b"%PDF-1.7\nfixture-source-restored\n"
    source_sha256 = hashlib.sha256(source).hexdigest()
    (run_dir / "source.pdf").write_bytes(source)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["source_pdf_sha256"] = source_sha256
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": _RUN_ID,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": "doc-fixture",
        "source_sha256": source_sha256,
        "confirmed": {"metrics": [], "analyses": []},
        "pending_review": {"facts": [], "calculations": [], "claims": [], "candidate_signals": []},
    }
    return source, {
        "state": "awaiting_model",
        "is_partial": True,
        "parent_run_id": None,
        "source_sha256": source_sha256,
        "report": report,
    }


def test_source_binding_and_sealed_final_checkpoint_rules(tmp_path: Path) -> None:
    _, snapshot = _archive(tmp_path)
    wrong = {**snapshot, "source_sha256": "0" * 64}
    with pytest.raises(AnnualCheckpointError) as mismatch:
        save_annual_checkpoint(tmp_path, _RUN_ID, wrong)
    assert mismatch.value.code == "checkpoint_source_mismatch"

    final = {**snapshot, "state": "completed_with_issues", "is_partial": False}
    record = save_annual_checkpoint(tmp_path, _RUN_ID, final)
    assert record.is_partial is False
    assert record.metadata["state"] == "completed_with_issues"
    with pytest.raises(AnnualCheckpointError) as sealed:
        save_annual_checkpoint(tmp_path, _RUN_ID, snapshot)
    assert sealed.value.code == "checkpoint_already_sealed"
