"""Atomic, fail-closed provider admission and cancellation gate.

The optional goal ledger is configured through FINTRACE_MODEL_CALL_LEDGER_CONFIG.
Per-job cancellation gates use FINTRACE_CALL_GATE_DB and FINTRACE_CALL_GATE_KEY.
When both are active, they must share one SQLite database so quota admission and
cancel have a single transaction order. No prompt, key, response, or URL is saved.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_ENV = "FINTRACE_MODEL_CALL_LEDGER_CONFIG"
_SOURCE_ENV = "FINTRACE_ACTIVE_SOURCE_SHA256"
_GATE_DB_ENV = "FINTRACE_CALL_GATE_DB"
_GATE_KEY_ENV = "FINTRACE_CALL_GATE_KEY"
_TASK_LIMIT_ENV = "FINTRACE_CALL_GATE_TASK_LIMIT"
_STAGE_LIMITS_ENV = "FINTRACE_CALL_GATE_STAGE_LIMITS"
_REPARSE_POINT = 0x400
_ANNUAL_CALL_STAGES = frozenset({
    "annual_discovery", "annual_investigation", "annual_issue_grouping", "annual_review",
})


class ModelCallAdmissionError(Exception):
    """Safe pre-network rejection from quota, context, or cancellation gate."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelCallLedgerReadError(Exception):
    """The requested ledger could not be read without creating or repairing it."""


def provider_model_fingerprint(base_url: str, model: str) -> str:
    """Return a non-secret stable fingerprint for endpoint and model identity."""

    try:
        parts = urlsplit(base_url)
        material = json.dumps(
            {
                "scheme": parts.scheme.lower(),
                "host": (parts.hostname or "").lower(),
                "port": parts.port,
                "path": parts.path.rstrip("/"),
                "model": model,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise ModelCallAdmissionError("ledger_config_invalid") from None
    return hashlib.sha256(material).hexdigest()


def reserve_provider_attempt(
    base_url: str,
    model: str,
    *,
    call_context: Mapping[str, Any] | None = None,
    deadline_monotonic: float | None = None,
) -> str:
    """Atomically reserve budget/gate before starting the provider worker.

    A configured goal ledger requires source, run, stage, batch, audit and gate
    context. When only a per-job gate exists, the row is still persisted but is
    marked non-goal so local fault simulations never become goal attempts.
    """

    _check_deadline(deadline_monotonic)
    config = _read_ledger_config()
    raw_gate_db = os.environ.get(_GATE_DB_ENV)
    raw_gate_key = os.environ.get(_GATE_KEY_ENV)
    if config is not None and (not raw_gate_db or not raw_gate_key):
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    if raw_gate_db and not raw_gate_key:
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    if raw_gate_key and not raw_gate_db:
        raise ModelCallAdmissionError("ledger_unavailable")
    if config is None and not raw_gate_db:
        # Compatibility mode remains available only when no persistent goal
        # ledger is active. This UUID is attached to the exact completion.
        return str(uuid.uuid4())

    context = _validate_call_context(call_context, required=True)
    gate_key = raw_gate_key or context["gate_key"]
    if gate_key != context["gate_key"]:
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    active_fingerprint = provider_model_fingerprint(base_url, model)
    source_sha = _active_source(config)
    if config is not None and active_fingerprint != config["provider_model_fingerprint"]:
        raise ModelCallAdmissionError("ledger_provider_mismatch")

    if config is not None:
        database_path = config["database_path"]
    else:
        database_path = _absolute_safe_path(Path(raw_gate_db), create_parent=True)
    if raw_gate_db:
        gate_database_path = _absolute_safe_path(Path(raw_gate_db), create_parent=True)
        if _normalized_path(gate_database_path) != _normalized_path(database_path):
            raise ModelCallAdmissionError("ledger_config_invalid")

    admission_id = str(uuid.uuid4())
    call_limits = _call_limits_from_environment()
    _with_immediate_transaction(
        database_path,
        deadline_monotonic,
        lambda connection, allow_schema_creation: _reserve_in_transaction(
            connection,
            allow_schema_creation=allow_schema_creation,
            config=config,
            active_fingerprint=active_fingerprint,
            source_sha=source_sha,
            gate_key=gate_key if raw_gate_db else None,
            context=context,
            admission_id=admission_id,
            call_limits=call_limits,
        ),
        create_parent=True,
    )
    return admission_id


def open_admission_gate(database_path: Path, gate_key: str, *, timeout_seconds: float = 10.0) -> None:
    """Persist an open per-job gate before starting its CLI process."""

    database = _absolute_safe_path(database_path, create_parent=True)
    config = _read_ledger_config()
    is_configured_shared_ledger = (
        config is not None
        and _normalized_path(config["database_path"]) == _normalized_path(database)
    )
    deadline = time.monotonic() + _positive_timeout(timeout_seconds)

    def open_gate(connection: sqlite3.Connection, allow_schema_creation: bool) -> None:
        _ensure_schema(connection, allow_create=allow_schema_creation)
        row = connection.execute(
            "SELECT closed FROM call_gates WHERE gate_key = ?", (gate_key,)
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO call_gates(gate_key, closed, updated_at) VALUES (?, 0, ?)",
                (gate_key, _now()),
            )
        elif int(row[0]) != 0:
            raise ModelCallAdmissionError("call_gate_closed")

    _with_immediate_transaction(
        database,
        deadline,
        open_gate,
        create_parent=True,
        allow_schema_creation=not is_configured_shared_ledger and not database.exists(),
    )


def initialize_provider_call_ledger(database_path: Path, *, timeout_seconds: float = 10.0) -> None:
    """Explicitly create a new ledger schema before runtime admission begins.

    Runtime operations never initialize a configured shared ledger. This
    setup-only entry point creates a schema for first-time provisioning and
    validates an existing ledger without repairing partial or damaged tables.
    """

    database = _absolute_safe_path(database_path, create_parent=True)
    deadline = time.monotonic() + _positive_timeout(timeout_seconds)

    def initialize(connection: sqlite3.Connection, allow_schema_creation: bool) -> None:
        _ensure_schema(connection, allow_create=allow_schema_creation)

    _with_immediate_transaction(
        database,
        deadline,
        initialize,
        create_parent=True,
        allow_schema_creation=not database.exists(),
    )


def close_admission_gate(database_path: Path, gate_key: str, *, timeout_seconds: float = 10.0) -> None:
    """Persist cancellation under the same lock used by provider admissions."""

    database = _absolute_safe_path(database_path, create_parent=False)
    deadline = time.monotonic() + _positive_timeout(timeout_seconds)

    def close_gate(connection: sqlite3.Connection, _allow_schema_creation: bool) -> None:
        _require_schema(connection)
        cursor = connection.execute(
            "UPDATE call_gates SET closed = 1, updated_at = ? WHERE gate_key = ?",
            (_now(), gate_key),
        )
        if cursor.rowcount != 1:
            raise ModelCallAdmissionError("ledger_unavailable")

    _with_immediate_transaction(database, deadline, close_gate, create_parent=False)


def mark_provider_request_started(database_path: Path | None, gate_key: str | None, admission_id: str) -> None:
    """Linearize child network start against cancellation, before opening HTTP."""

    if database_path is None:
        return
    if not gate_key:
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    database = _absolute_safe_path(database_path, create_parent=False)

    def mark_started(connection: sqlite3.Connection, _allow_schema_creation: bool) -> None:
        _require_schema(connection)
        gate = connection.execute(
            "SELECT closed FROM call_gates WHERE gate_key = ?", (gate_key,)
        ).fetchone()
        if gate is None or int(gate[0]) != 0:
            raise ModelCallAdmissionError("call_gate_closed")
        cursor = connection.execute(
            "UPDATE provider_attempts SET status = 'network_started' "
            "WHERE admission_id = ? AND gate_key = ? AND status = 'reserved'",
            (admission_id, gate_key),
        )
        if cursor.rowcount != 1:
            raise ModelCallAdmissionError("call_gate_closed")

    _with_immediate_transaction(database, time.monotonic() + 3.0, mark_started, create_parent=False)


def admission_gate_is_open(database_path: Path | None, gate_key: str | None, admission_id: str) -> bool:
    """Read-only worker preflight; it never creates an empty ledger."""

    if database_path is None:
        return True
    if not gate_key:
        return False
    database = _absolute_safe_path(database_path, create_parent=False)
    if not database.is_file():
        return False
    try:
        with closing(sqlite3.connect(database, timeout=0.1)) as connection:
            _require_schema(connection)
            gate = connection.execute(
                "SELECT closed FROM call_gates WHERE gate_key = ?", (gate_key,)
            ).fetchone()
            attempt = connection.execute(
                "SELECT status FROM provider_attempts WHERE admission_id = ? AND gate_key = ?",
                (admission_id, gate_key),
            ).fetchone()
            return gate is not None and int(gate[0]) == 0 and attempt is not None
    except (OSError, sqlite3.Error, ModelCallLedgerReadError):
        return False


def ledger_attempt_count(database_path: Path) -> int:
    """Read a real persisted count; missing/corrupt databases are errors."""

    database = _absolute_safe_path(database_path, create_parent=False)
    if not database.is_file():
        raise ModelCallLedgerReadError("ledger database is missing")
    try:
        with closing(
            sqlite3.connect(f"file:{_sqlite_uri_path(database)}?mode=ro", uri=True, timeout=0.5)
        ) as connection:
            _require_schema(connection)
            row = connection.execute(
                "SELECT COUNT(*) FROM provider_attempts WHERE goal_counted = 1"
            ).fetchone()
            if row is None or type(row[0]) is not int:
                raise ModelCallLedgerReadError("ledger count is invalid")
            return row[0]
    except ModelCallLedgerReadError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise ModelCallLedgerReadError("ledger database is unreadable") from None


def select_call_gate_database(
    project_root: Path,
    job_dir: Path,
    *,
    allow_external_job_dir: bool = False,
) -> Path:
    """Select the configured shared goal ledger or a job-local isolated DB."""

    config = _read_ledger_config()
    if config is not None:
        return Path(config["database_path"])
    try:
        root = Path(project_root).resolve(strict=True)
        job = Path(job_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ModelCallAdmissionError("ledger_config_invalid") from None
    if not job.is_relative_to(root) and not allow_external_job_dir:
        raise ModelCallAdmissionError("ledger_config_invalid")
    return job / "call-gate.sqlite3"


def read_gate_admission_count(database_path: Path, gate_key: str) -> int:
    """Return one gate's all-stage admitted attempts, failing closed on damage."""

    if not isinstance(gate_key, str) or not gate_key or len(gate_key) > 256:
        raise ModelCallAdmissionError("ledger_config_invalid")
    database = _absolute_safe_path(Path(database_path), create_parent=False)
    if not database.is_file():
        raise ModelCallLedgerReadError("ledger database is missing")
    try:
        with closing(
            sqlite3.connect(f"file:{_sqlite_uri_path(database)}?mode=ro", uri=True, timeout=1.0)
        ) as connection:
            _require_schema(connection)
            row = connection.execute(
                "SELECT COUNT(*) FROM provider_attempts WHERE gate_key = ?",
                (gate_key,),
            ).fetchone()
            if row is None or type(row[0]) is not int or row[0] < 0:
                raise ModelCallLedgerReadError("ledger count is invalid")
            return row[0]
    except (ModelCallAdmissionError, ModelCallLedgerReadError):
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise ModelCallLedgerReadError("ledger database is unreadable") from None


def read_gate_stage_admission_counts(database_path: Path, gate_key: str) -> dict[str, int]:
    """Return the durable per-stage admission counts for one task gate."""

    if not isinstance(gate_key, str) or not gate_key or len(gate_key) > 256:
        raise ModelCallAdmissionError("ledger_config_invalid")
    database = _absolute_safe_path(Path(database_path), create_parent=False)
    if not database.is_file():
        raise ModelCallLedgerReadError("ledger database is missing")
    try:
        with closing(
            sqlite3.connect(f"file:{_sqlite_uri_path(database)}?mode=ro", uri=True, timeout=1.0)
        ) as connection:
            _require_schema(connection)
            rows = connection.execute(
                "SELECT stage, COUNT(*) FROM provider_attempts WHERE gate_key = ? GROUP BY stage",
                (gate_key,),
            ).fetchall()
            counts = {
                "annual_discovery": 0,
                "annual_investigation": 0,
                "annual_issue_grouping": 0,
                "annual_review": 0,
            }
            for stage, count in rows:
                if stage not in counts or type(count) is not int or count < 0:
                    raise ModelCallLedgerReadError("ledger stage count is invalid")
                counts[stage] = count
            return counts
    except (ModelCallAdmissionError, ModelCallLedgerReadError):
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise ModelCallLedgerReadError("ledger database is unreadable") from None


def _read_ledger_config() -> dict[str, Any] | None:
    raw_path = os.environ.get(_CONFIG_ENV)
    if not raw_path:
        return None
    try:
        path = _absolute_safe_path(Path(raw_path).expanduser(), create_parent=False)
        if not path.is_file() or path.stat().st_size > 32 * 1024:
            raise ModelCallAdmissionError("ledger_config_invalid")
        config = json.loads(path.read_text(encoding="utf-8"))
    except ModelCallAdmissionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ModelCallAdmissionError("ledger_config_invalid") from None
    expected = {
        "version", "database_path", "limit", "provider_model_fingerprint", "authorized_source_sha256s"
    }
    if not isinstance(config, dict) or set(config) != expected:
        raise ModelCallAdmissionError("ledger_config_invalid")
    if (
        type(config.get("version")) is not int
        or config["version"] != 1
        or type(config.get("limit")) is not int
        or not 1 <= config["limit"] <= 1_000_000
    ):
        raise ModelCallAdmissionError("ledger_config_invalid")
    fingerprint = config.get("provider_model_fingerprint")
    if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
        raise ModelCallAdmissionError("ledger_config_invalid")
    sources = config.get("authorized_source_sha256s")
    if (
        not isinstance(sources, list)
        or not sources
        or len(sources) > 128
        or any(not isinstance(item, str) or _SHA256.fullmatch(item.lower()) is None for item in sources)
    ):
        raise ModelCallAdmissionError("ledger_config_invalid")
    normalized_sources = sorted({item.lower() for item in sources})
    if len(normalized_sources) != len(sources):
        raise ModelCallAdmissionError("ledger_config_invalid")
    raw_database = config.get("database_path")
    if not isinstance(raw_database, str) or not raw_database or "\x00" in raw_database:
        raise ModelCallAdmissionError("ledger_config_invalid")
    database_path = _absolute_safe_path(Path(raw_database).expanduser(), create_parent=True)
    return {
        "version": 1,
        "database_path": database_path,
        "limit": config["limit"],
        "provider_model_fingerprint": fingerprint,
        "authorized_source_sha256s": normalized_sources,
    }


def _active_source(config: Mapping[str, Any] | None) -> str | None:
    if config is None:
        return None
    source_sha = os.environ.get(_SOURCE_ENV, "").lower()
    if (
        not source_sha
        or _SHA256.fullmatch(source_sha) is None
        or source_sha not in config["authorized_source_sha256s"]
    ):
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    return source_sha


def _call_limits_from_environment() -> dict[str, Any] | None:
    raw_task = os.environ.get(_TASK_LIMIT_ENV)
    raw_stages = os.environ.get(_STAGE_LIMITS_ENV)
    if raw_task is None and raw_stages is None:
        return None
    if raw_task is None or raw_stages is None or re.fullmatch(r"[1-9][0-9]{0,3}", raw_task) is None:
        raise ModelCallAdmissionError("call_budget_config_invalid")
    try:
        stage_limits = json.loads(raw_stages)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ModelCallAdmissionError("call_budget_config_invalid") from None
    if (
        not isinstance(stage_limits, dict)
        or set(stage_limits) != _ANNUAL_CALL_STAGES
        or any(type(limit) is not int or not 0 <= limit <= int(raw_task) for limit in stage_limits.values())
        or sum(stage_limits.values()) != int(raw_task)
    ):
        raise ModelCallAdmissionError("call_budget_config_invalid")
    return {"task_limit": int(raw_task), "stage_limits": stage_limits}


def _validate_call_context(call_context: Mapping[str, Any] | None, *, required: bool) -> dict[str, Any]:
    if call_context is None and not required:
        return {}
    if not isinstance(call_context, Mapping):
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    required_fields = {"run_id", "stage", "batch_index", "audit_id", "gate_key"}
    if set(call_context) != required_fields:
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    run_id = call_context.get("run_id")
    stage = call_context.get("stage")
    gate_key = call_context.get("gate_key")
    audit_id = call_context.get("audit_id")
    batch_index = call_context.get("batch_index")
    if (
        not isinstance(run_id, str) or not run_id or len(run_id) > 128
        or not isinstance(stage, str) or stage not in {
            "annual_discovery", "annual_investigation", "annual_issue_grouping", "annual_review", "connection_test"
        }
        or not isinstance(gate_key, str) or not gate_key or len(gate_key) > 256
        or not _valid_uuid4(audit_id)
        or (batch_index is not None and (type(batch_index) is not int or batch_index < 1))
        or (stage == "annual_discovery" and batch_index is None)
    ):
        raise ModelCallAdmissionError("ledger_source_context_missing_or_unapproved")
    return {
        "run_id": run_id,
        "stage": stage,
        "batch_index": batch_index,
        "audit_id": audit_id,
        "gate_key": gate_key,
    }


def _reserve_in_transaction(
    connection: sqlite3.Connection,
    *,
    allow_schema_creation: bool,
    config: Mapping[str, Any] | None,
    active_fingerprint: str,
    source_sha: str | None,
    gate_key: str | None,
    context: Mapping[str, Any],
    admission_id: str,
    call_limits: Mapping[str, Any] | None,
) -> None:
    _ensure_schema(connection, allow_create=allow_schema_creation)
    if config is not None:
        _pin_and_check_policy(connection, config)
    if gate_key is not None:
        gate = connection.execute(
            "SELECT closed FROM call_gates WHERE gate_key = ?", (gate_key,)
        ).fetchone()
        if gate is None or int(gate[0]) != 0:
            raise ModelCallAdmissionError("call_gate_closed")
        if call_limits is not None:
            task_count = int(connection.execute(
                "SELECT COUNT(*) FROM provider_attempts WHERE gate_key = ?", (gate_key,)
            ).fetchone()[0])
            if task_count >= call_limits["task_limit"]:
                raise ModelCallAdmissionError("task_budget_exhausted")
            stage_limit = call_limits["stage_limits"].get(context["stage"])
            if stage_limit is not None:
                stage_count = int(connection.execute(
                    "SELECT COUNT(*) FROM provider_attempts WHERE gate_key = ? AND stage = ?",
                    (gate_key, context["stage"]),
                ).fetchone()[0])
                if stage_count >= stage_limit:
                    raise ModelCallAdmissionError("stage_budget_exhausted")
    if config is not None:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM provider_attempts WHERE goal_counted = 1"
            ).fetchone()[0]
        )
        if count >= config["limit"]:
            raise ModelCallAdmissionError("ledger_budget_exhausted")
    connection.execute(
        "INSERT INTO provider_attempts("
        "admission_id, admitted_at, provider_model_fingerprint, source_sha256, goal_counted, "
        "gate_key, run_id, stage, batch_index, audit_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')",
        (
            admission_id,
            _now(),
            active_fingerprint,
            source_sha,
            1 if config is not None else 0,
            gate_key,
            context["run_id"],
            context["stage"],
            context["batch_index"],
            context["audit_id"],
        ),
    )


def _pin_and_check_policy(connection: sqlite3.Connection, config: Mapping[str, Any]) -> None:
    sources_json = json.dumps(config["authorized_source_sha256s"], separators=(",", ":"))
    row = connection.execute(
        "SELECT version, quota_limit, provider_model_fingerprint, authorized_source_sha256s "
        "FROM provider_policy WHERE singleton = 1"
    ).fetchone()
    expected = (
        config["version"],
        config["limit"],
        config["provider_model_fingerprint"],
        sources_json,
    )
    if row is None:
        connection.execute(
            "INSERT INTO provider_policy(singleton, version, quota_limit, provider_model_fingerprint, "
            "authorized_source_sha256s) VALUES (1, ?, ?, ?, ?)",
            expected,
        )
    elif tuple(row) != expected:
        raise ModelCallAdmissionError("ledger_policy_mismatch")


def _ensure_schema(connection: sqlite3.Connection, *, allow_create: bool) -> None:
    try:
        table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error:
        raise ModelCallLedgerReadError("ledger schema cannot be read") from None
    existing = {row[0] for row in table_rows if row and isinstance(row[0], str)}
    expected = {"call_gates", "provider_policy", "provider_attempts"}
    if existing:
        # Never repair an existing partial ledger into an empty accounting store.
        if not expected.issubset(existing):
            raise ModelCallLedgerReadError("ledger schema is incomplete")
        _require_schema(connection)
        return
    if not allow_create:
        raise ModelCallLedgerReadError("ledger schema is missing")
    connection.execute(
        "CREATE TABLE call_gates ("
        "gate_key TEXT PRIMARY KEY, closed INTEGER NOT NULL CHECK(closed IN (0, 1)), updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE provider_policy ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), version INTEGER NOT NULL, "
        "quota_limit INTEGER NOT NULL, provider_model_fingerprint TEXT NOT NULL, "
        "authorized_source_sha256s TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE provider_attempts ("
        "admission_id TEXT PRIMARY KEY, admitted_at TEXT NOT NULL, "
        "provider_model_fingerprint TEXT NOT NULL, source_sha256 TEXT, goal_counted INTEGER NOT NULL, "
        "gate_key TEXT, run_id TEXT NOT NULL, stage TEXT NOT NULL, batch_index INTEGER, "
        "audit_id TEXT NOT NULL, status TEXT NOT NULL)"
    )


def _require_schema(connection: sqlite3.Connection) -> None:
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error:
        raise ModelCallLedgerReadError("ledger schema cannot be read") from None
    names = {row[0] for row in rows if isinstance(row, tuple) and row}
    if not {"call_gates", "provider_policy", "provider_attempts"}.issubset(names):
        raise ModelCallLedgerReadError("ledger schema is incomplete")
    expected_columns = {
        "admission_id", "admitted_at", "provider_model_fingerprint", "source_sha256", "goal_counted",
        "gate_key", "run_id", "stage", "batch_index", "audit_id", "status",
    }
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_attempts)").fetchall()}
    except sqlite3.Error:
        raise ModelCallLedgerReadError("ledger schema is invalid") from None
    if columns != expected_columns:
        raise ModelCallLedgerReadError("ledger schema is incompatible")
    expected_table_columns = {
        "call_gates": {"gate_key", "closed", "updated_at"},
        "provider_policy": {
            "singleton", "version", "quota_limit", "provider_model_fingerprint", "authorized_source_sha256s"
        },
    }
    for table, expected in expected_table_columns.items():
        try:
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            raise ModelCallLedgerReadError("ledger schema is invalid") from None
        if actual != expected:
            raise ModelCallLedgerReadError("ledger schema is incompatible")


def _with_immediate_transaction(
    database: Path,
    deadline_monotonic: float | None,
    operation,
    *,
    create_parent: bool,
    allow_schema_creation: bool = False,
) -> None:
    if create_parent:
        _absolute_safe_path(database, create_parent=True)
    try:
        if not database.exists() and not allow_schema_creation:
            raise ModelCallLedgerReadError("ledger database is missing")
        target = (
            database
            if allow_schema_creation
            else f"file:{_sqlite_uri_path(database)}?mode=rw"
        )
        with closing(sqlite3.connect(target, timeout=0, isolation_level=None, uri=not allow_schema_creation)) as connection:
            connection.execute("PRAGMA busy_timeout=0")
            while True:
                _check_deadline(deadline_monotonic)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    time.sleep(min(0.01, _remaining(deadline_monotonic)))
            try:
                operation(connection, allow_schema_creation)
                _commit_with_retry(connection, deadline_monotonic)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
    except ModelCallAdmissionError:
        raise
    except TimeoutError:
        raise ModelCallAdmissionError("call_deadline_exhausted") from None
    except (OSError, sqlite3.Error, ModelCallLedgerReadError):
        raise ModelCallAdmissionError("ledger_unavailable") from None


def _commit_with_retry(connection: sqlite3.Connection, deadline_monotonic: float | None) -> None:
    while True:
        _check_deadline(deadline_monotonic)
        try:
            connection.execute("COMMIT")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            time.sleep(min(0.01, _remaining(deadline_monotonic)))


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and _remaining(deadline_monotonic) <= 0:
        raise TimeoutError("deadline")


def _remaining(deadline_monotonic: float | None) -> float:
    return 3600.0 if deadline_monotonic is None else max(deadline_monotonic - time.monotonic(), 0.0)


def _positive_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    return float(value)


def _absolute_safe_path(path: Path, *, create_parent: bool) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ModelCallAdmissionError("ledger_config_invalid")
    candidate = Path(os.path.abspath(path))
    parts = candidate.parents
    for ancestor in reversed(parts):
        if ancestor.exists() and _is_reparse_or_symlink(ancestor):
            raise ModelCallAdmissionError("ledger_config_invalid")
    if candidate.exists() and _is_reparse_or_symlink(candidate):
        raise ModelCallAdmissionError("ledger_config_invalid")
    parent = candidate.parent
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True)
    elif not parent.is_dir():
        raise ModelCallAdmissionError("ledger_unavailable")
    if candidate.exists() and not candidate.is_file():
        raise ModelCallAdmissionError("ledger_config_invalid")
    return candidate


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _sqlite_uri_path(path: Path) -> str:
    return quote(path.as_posix(), safe="/:")


def _valid_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
