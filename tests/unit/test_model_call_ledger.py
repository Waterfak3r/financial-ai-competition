"""跨线程/重启可读的 provider admission ledger 与 cancel gate。"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from finagent.core.model_call_ledger import (
    ModelCallAdmissionError,
    ModelCallLedgerReadError,
    close_admission_gate,
    initialize_provider_call_ledger,
    ledger_attempt_count,
    open_admission_gate,
    provider_model_fingerprint,
    read_gate_admission_count,
    read_shared_goal_budget,
    reserve_provider_attempt,
)

_BASE_URL = "http://127.0.0.1:9187/v1"
_MODEL = "local-fault-model"
_SOURCE = "a" * 64


def _context(gate_key: str, *, batch_index: int = 1) -> dict[str, object]:
    return {
        "run_id": "annual-analysis-test-run",
        "stage": "annual_discovery",
        "batch_index": batch_index,
        "audit_id": str(uuid.uuid4()),
        "gate_key": gate_key,
    }


def _enable_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    limit: int | None = 8,
    gate_key: str = "annual-job-test",
) -> tuple[Path, Path]:
    database = tmp_path / "isolated-ledger.sqlite3"
    config_path = tmp_path / "ledger-config.json"
    open_admission_gate(database, gate_key)
    _write_config(config_path, database, limit=limit)
    monkeypatch.setenv("FINTRACE_MODEL_CALL_LEDGER_CONFIG", str(config_path))
    monkeypatch.setenv("FINTRACE_ACTIVE_SOURCE_SHA256", _SOURCE)
    monkeypatch.setenv("FINTRACE_CALL_GATE_DB", str(database))
    monkeypatch.setenv("FINTRACE_CALL_GATE_KEY", gate_key)
    return database, config_path


def _write_config(config_path: Path, database: Path, *, limit: int | None) -> None:
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "database_path": str(database),
                "limit": limit,
                "provider_model_fingerprint": provider_model_fingerprint(_BASE_URL, _MODEL),
                "authorized_source_sha256s": [_SOURCE],
            }
        ),
        encoding="utf-8",
    )


def _reserve(gate_key: str = "annual-job-test", *, batch_index: int = 1) -> str:
    return reserve_provider_attempt(
        _BASE_URL,
        _MODEL,
        call_context=_context(gate_key, batch_index=batch_index),
    )


def test_pinned_policy_rejects_limit_change_and_keeps_persisted_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, config_path = _enable_ledger(tmp_path, monkeypatch, limit=2)
    _reserve()
    assert ledger_attempt_count(database) == 1

    _write_config(config_path, database, limit=20)
    with pytest.raises(ModelCallAdmissionError, match="ledger_policy_mismatch") as rejected:
        _reserve()
    assert rejected.value.code == "ledger_policy_mismatch"
    assert ledger_attempt_count(database) == 1


def test_unlimited_policy_preserves_existing_attempts_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, config_path = _enable_ledger(tmp_path, monkeypatch, limit=2)
    original = {_reserve(batch_index=1), _reserve(batch_index=2)}
    _write_config(config_path, database, limit=None)
    with pytest.raises(ModelCallAdmissionError, match="ledger_policy_mismatch"):
        _reserve(batch_index=3)
    assert ledger_attempt_count(database) == 2

    # Operator authorization changes only policy, never the attempt history.
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE provider_policy SET quota_limit = 0 WHERE singleton = 1")
    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted = list(pool.map(lambda index: _reserve(batch_index=index), range(3, 7)))
    assert len(set(admitted)) == 4
    assert ledger_attempt_count(database) == 6
    with sqlite3.connect(database) as connection:
        persisted = {
            row[0] for row in connection.execute("SELECT admission_id FROM provider_attempts")
        }
    assert persisted == original | set(admitted)

    close_admission_gate(database, "annual-job-test")
    with pytest.raises(ModelCallAdmissionError, match="call_gate_closed"):
        _reserve(batch_index=7)
    assert ledger_attempt_count(database) == 6


def test_shared_budget_counts_goal_attempts_only_without_mutating_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, config_path = _enable_ledger(tmp_path, monkeypatch, limit=4)
    _reserve(batch_index=1)
    _reserve(batch_index=2)
    monkeypatch.delenv("FINTRACE_MODEL_CALL_LEDGER_CONFIG")
    monkeypatch.setenv("FINTRACE_CALL_GATE_KEY", "local-only")
    open_admission_gate(database, "local-only")
    _reserve("local-only")
    monkeypatch.setenv("FINTRACE_MODEL_CALL_LEDGER_CONFIG", str(config_path))
    before = database.read_bytes()

    assert read_shared_goal_budget() == (4, 2)
    assert database.read_bytes() == before
    assert ledger_attempt_count(database) == 2
    assert read_gate_admission_count(database, "local-only") == 1


def test_shared_budget_does_not_treat_unpinned_null_as_authorized_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, config_path = _enable_ledger(tmp_path, monkeypatch, limit=2)
    original = {_reserve(batch_index=1), _reserve(batch_index=2)}
    assert read_shared_goal_budget() == (2, 0)
    _write_config(config_path, database, limit=None)
    before = database.read_bytes()
    with pytest.raises(ModelCallAdmissionError, match="ledger_policy_mismatch"):
        read_shared_goal_budget()
    assert database.read_bytes() == before

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE provider_policy SET quota_limit = 0 WHERE singleton = 1")
    assert read_shared_goal_budget() == (None, None)
    with sqlite3.connect(database) as connection:
        assert {row[0] for row in connection.execute("SELECT admission_id FROM provider_attempts")} == original


@pytest.mark.parametrize("damage", ["missing_database", "missing_table", "missing_used_policy"])
def test_shared_budget_rejects_damaged_used_ledger_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    database, _ = _enable_ledger(tmp_path, monkeypatch)
    _reserve()
    if damage == "missing_database":
        database.unlink()
    else:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "DROP TABLE provider_attempts" if damage == "missing_table" else "DELETE FROM provider_policy"
            )
    before = database.read_bytes() if database.exists() else None
    with pytest.raises(ModelCallLedgerReadError):
        read_shared_goal_budget()
    assert (database.read_bytes() if database.exists() else None) == before


def test_missing_call_context_fails_closed_without_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _enable_ledger(tmp_path, monkeypatch)
    with pytest.raises(ModelCallAdmissionError) as rejected:
        reserve_provider_attempt(_BASE_URL, _MODEL)
    assert rejected.value.code == "ledger_source_context_missing_or_unapproved"
    assert ledger_attempt_count(database) == 0


def test_quota_reservations_are_atomic_across_concurrent_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _enable_ledger(tmp_path, monkeypatch, limit=5)
    outcomes: list[str] = []
    lock = threading.Lock()

    def call(index: int) -> None:
        try:
            value = _reserve(batch_index=index + 1)
        except ModelCallAdmissionError as exc:
            value = exc.code
        with lock:
            outcomes.append(value)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(call, range(12)))
    admitted = [value for value in outcomes if value not in {"ledger_budget_exhausted"}]
    assert len(admitted) == 5
    assert len(set(admitted)) == 5
    assert outcomes.count("ledger_budget_exhausted") == 7
    assert ledger_attempt_count(database) == 5


def test_cancel_gate_rejects_every_new_admission_after_persisted_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _enable_ledger(tmp_path, monkeypatch, limit=8)
    first_admission = _reserve()
    close_admission_gate(database, "annual-job-test")
    with pytest.raises(ModelCallAdmissionError) as rejected:
        _reserve()
    assert rejected.value.code == "call_gate_closed"
    assert first_admission
    assert ledger_attempt_count(database) == 1
    with sqlite3.connect(database) as connection:
        statuses = connection.execute(
            "SELECT status FROM provider_attempts WHERE gate_key = ?", ("annual-job-test",)
        ).fetchall()
    assert statuses == [("reserved",)]


def test_task_and_stage_caps_are_atomic_and_count_each_retry_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "stage-budget.sqlite3"
    gate_key = "annual-job-budget"
    open_admission_gate(database, gate_key)
    monkeypatch.delenv("FINTRACE_MODEL_CALL_LEDGER_CONFIG", raising=False)
    monkeypatch.setenv("FINTRACE_CALL_GATE_DB", str(database))
    monkeypatch.setenv("FINTRACE_CALL_GATE_KEY", gate_key)
    monkeypatch.setenv("FINTRACE_CALL_GATE_TASK_LIMIT", "3")
    monkeypatch.setenv(
        "FINTRACE_CALL_GATE_STAGE_LIMITS",
        json.dumps({
            "annual_discovery": 2,
            "annual_investigation": 1,
            "annual_issue_grouping": 0,
            "annual_review": 0,
        }),
    )
    _reserve(gate_key, batch_index=1)
    _reserve(gate_key, batch_index=2)

    with pytest.raises(ModelCallAdmissionError) as stage_rejected:
        _reserve(gate_key, batch_index=3)
    assert stage_rejected.value.code == "stage_budget_exhausted"

    investigation_context = _context(gate_key, batch_index=1)
    investigation_context["stage"] = "annual_investigation"
    reserve_provider_attempt(_BASE_URL, _MODEL, call_context=investigation_context)
    with pytest.raises(ModelCallAdmissionError) as task_rejected:
        reserve_provider_attempt(_BASE_URL, _MODEL, call_context=investigation_context)
    assert task_rejected.value.code == "task_budget_exhausted"
    assert read_gate_admission_count(database, gate_key) == 3


def test_stage_cap_is_shared_atomically_by_concurrent_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "concurrent-stage-budget.sqlite3"
    gate_key = "annual-job-concurrent-budget"
    open_admission_gate(database, gate_key)
    monkeypatch.delenv("FINTRACE_MODEL_CALL_LEDGER_CONFIG", raising=False)
    monkeypatch.setenv("FINTRACE_CALL_GATE_DB", str(database))
    monkeypatch.setenv("FINTRACE_CALL_GATE_KEY", gate_key)
    monkeypatch.setenv("FINTRACE_CALL_GATE_TASK_LIMIT", "3")
    monkeypatch.setenv(
        "FINTRACE_CALL_GATE_STAGE_LIMITS",
        json.dumps({
            "annual_discovery": 2,
            "annual_investigation": 1,
            "annual_issue_grouping": 0,
            "annual_review": 0,
        }),
    )
    outcomes: list[str] = []
    lock = threading.Lock()

    def call(index: int) -> None:
        try:
            value = _reserve(gate_key, batch_index=index + 1)
        except ModelCallAdmissionError as exc:
            value = exc.code
        with lock:
            outcomes.append(value)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(call, range(12)))
    assert len([value for value in outcomes if value not in {"stage_budget_exhausted"}]) == 2
    assert outcomes.count("stage_budget_exhausted") == 10
    assert read_gate_admission_count(database, gate_key) == 2


def test_existing_partial_schema_is_not_recreated_as_empty_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _enable_ledger(tmp_path, monkeypatch)
    _reserve()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE provider_attempts")

    with pytest.raises(ModelCallAdmissionError) as rejected:
        _reserve()
    assert rejected.value.code == "ledger_unavailable"
    with sqlite3.connect(database) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "provider_attempts" not in names
    with pytest.raises(ModelCallLedgerReadError):
        ledger_attempt_count(database)


def test_missing_ledger_database_is_an_error_not_zero(tmp_path: Path) -> None:
    with pytest.raises(ModelCallLedgerReadError):
        ledger_attempt_count(tmp_path / "missing.sqlite3")


def test_missing_configured_used_ledger_cannot_be_recreated_by_runtime_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _enable_ledger(tmp_path, monkeypatch, limit=2)
    _reserve()
    assert ledger_attempt_count(database) == 1
    database.unlink()

    operations = (
        lambda: _reserve(),
        lambda: open_admission_gate(database, "annual-job-test"),
        lambda: close_admission_gate(database, "annual-job-test"),
    )
    for operation in operations:
        with pytest.raises(ModelCallAdmissionError) as rejected:
            operation()
        assert rejected.value.code == "ledger_unavailable"
        assert not database.exists()
        with pytest.raises(ModelCallLedgerReadError):
            ledger_attempt_count(database)


def test_explicit_initialization_supports_first_configured_shared_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "first-shared-ledger.sqlite3"
    config_path = tmp_path / "first-shared-ledger-config.json"
    _write_config(config_path, database, limit=2)
    monkeypatch.setenv("FINTRACE_MODEL_CALL_LEDGER_CONFIG", str(config_path))
    monkeypatch.setenv("FINTRACE_ACTIVE_SOURCE_SHA256", _SOURCE)
    monkeypatch.setenv("FINTRACE_CALL_GATE_DB", str(database))
    monkeypatch.setenv("FINTRACE_CALL_GATE_KEY", "annual-job-test")

    initialize_provider_call_ledger(database)
    assert ledger_attempt_count(database) == 0
    open_admission_gate(database, "annual-job-test")
    _reserve()
    assert ledger_attempt_count(database) == 1


def test_explicit_initialization_does_not_repair_an_existing_empty_file(tmp_path: Path) -> None:
    database = tmp_path / "damaged-empty-ledger.sqlite3"
    database.touch()

    with pytest.raises(ModelCallAdmissionError) as rejected:
        initialize_provider_call_ledger(database)

    assert rejected.value.code == "ledger_unavailable"
    with pytest.raises(ModelCallLedgerReadError):
        ledger_attempt_count(database)
