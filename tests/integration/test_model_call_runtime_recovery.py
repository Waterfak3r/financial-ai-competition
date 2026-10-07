from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_SOURCE = _PROJECT_ROOT / "backend" / "src"
if str(_BACKEND_SOURCE) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SOURCE))

from finagent.agents.annual_discovery import parse_annual_discovery_response_v2
from finagent.audit import EvidenceRef, audited_complete_chat
from finagent.audit import audited_chat as audited_chat_module
from finagent.core.model_call_ledger import (
    ModelCallAdmissionError,
    ModelCallLedgerReadError,
    close_admission_gate,
    ledger_attempt_count,
    open_admission_gate,
    provider_model_fingerprint,
    reserve_provider_attempt,
)
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage, ModelResponseError, complete_chat
from finagent.retrieval.annual_discovery_context import DiscoveryBatch, DiscoveryFragment

_MODEL = "r2-local-fake-model"
_SOURCE_SHA = "d" * 64
_FAKE_API_KEY = "local-test-key-do-not-log"
_GATE_KEY = "r2-isolated-mock-gate"
_TEST_EVIDENCE_ENV = "FINTRACE_R2_EVIDENCE_DIR"
_MULTIPROCESS_BASE_URL = "http://127.0.0.1:9/v1"


class _LocalModelServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class _LocalModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        state = self.server.state  # type: ignore[attr-defined]
        with state["lock"]:
            state["requests"] += 1
        status, body = self.server.behavior()  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, format: str, *args: object) -> None:
        return


def _json_response(text: str = "local response") -> bytes:
    return json.dumps(
        {
            "id": "r2-local-response",
            "model": _MODEL,
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _serve_local_model(behavior: Callable[[], tuple[int, bytes]]) -> tuple[_LocalModelServer, threading.Thread]:
    server = _LocalModelServer(("127.0.0.1", 0), _LocalModelHandler)
    server.state = {"requests": 0, "lock": threading.Lock()}  # type: ignore[attr-defined]
    server.behavior = behavior  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _serve_dripping_model() -> tuple[_LocalModelServer, threading.Thread]:
    class DripHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state = self.server.state  # type: ignore[attr-defined]
            with state["lock"]:
                state["requests"] += 1
            state["request_seen"].set()
            body = _json_response("x" * 4096)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for offset in range(0, len(body), 128):
                if state["stop"].is_set():
                    return
                try:
                    self.wfile.write(body[offset : offset + 128])
                    self.wfile.flush()
                except OSError:
                    return
                if state["stop"].wait(0.12):
                    return

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _LocalModelServer(("127.0.0.1", 0), DripHandler)
    server.state = {  # type: ignore[attr-defined]
        "requests": 0,
        "lock": threading.Lock(),
        "request_seen": threading.Event(),
        "stop": threading.Event(),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request_count(server: _LocalModelServer) -> int:
    with server.state["lock"]:  # type: ignore[attr-defined]
        return int(server.state["requests"])  # type: ignore[attr-defined]


def _close_local_model(server: _LocalModelServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def _settings(port: int, *, timeout: float = 4.0) -> ModelSettings:
    return ModelSettings(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=_FAKE_API_KEY,
        model=_MODEL,
        timeout_seconds=timeout,
    )


def _settings_for_url(base_url: str, *, timeout: float = 4.0) -> ModelSettings:
    return ModelSettings(
        base_url=base_url,
        api_key=_FAKE_API_KEY,
        model=_MODEL,
        timeout_seconds=timeout,
    )


def _make_mock_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    *,
    limit: int,
    gate_key: str = _GATE_KEY,
) -> tuple[Path, Path]:
    for name in (
        "FINTRACE_MODEL_CALL_LEDGER_CONFIG",
        "FINTRACE_ACTIVE_SOURCE_SHA256",
        "FINTRACE_CALL_GATE_DB",
        "FINTRACE_CALL_GATE_KEY",
        "FINTRACE_CALL_CANCEL_FILE",
        "FINTRACE_CALL_LIVENESS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "")
    monkeypatch.setenv("HTTPS_PROXY", "")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    database = tmp_path / "isolated-mock-provider-ledger.sqlite3"
    config_path = tmp_path / "isolated-mock-provider-policy.json"
    open_admission_gate(database, gate_key)
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "database_path": str(database),
                "limit": limit,
                "provider_model_fingerprint": provider_model_fingerprint(base_url, _MODEL),
                "authorized_source_sha256s": [_SOURCE_SHA],
            }
        ),
        encoding="utf-8",
    )
    _set_mock_ledger_environment(monkeypatch, database, config_path, gate_key)
    return database, config_path


def _set_mock_ledger_environment(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    config_path: Path,
    gate_key: str = _GATE_KEY,
) -> None:
    for name in (
        "FINTRACE_MODEL_CALL_LEDGER_CONFIG",
        "FINTRACE_ACTIVE_SOURCE_SHA256",
        "FINTRACE_CALL_GATE_DB",
        "FINTRACE_CALL_GATE_KEY",
        "FINTRACE_CALL_CANCEL_FILE",
        "FINTRACE_CALL_LIVENESS_FILE",
        "FINTRACE_R2_WORKER_PID_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FINTRACE_MODEL_CALL_LEDGER_CONFIG", str(config_path))
    monkeypatch.setenv("FINTRACE_ACTIVE_SOURCE_SHA256", _SOURCE_SHA)
    monkeypatch.setenv("FINTRACE_CALL_GATE_DB", str(database))
    monkeypatch.setenv("FINTRACE_CALL_GATE_KEY", gate_key)
    monkeypatch.setenv("HTTP_PROXY", "")
    monkeypatch.setenv("HTTPS_PROXY", "")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def _safe_subprocess_environment(
    *,
    database: Path | None = None,
    config_path: Path | None = None,
    gate_key: str = _GATE_KEY,
    liveness_file: Path | None = None,
    worker_pid_file: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment without inheriting model settings or ledger policy."""

    environment = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    environment["PYTHONPATH"] = str(_BACKEND_SOURCE)
    environment["PYTHONUTF8"] = "1"
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    if database is not None:
        environment["FINTRACE_CALL_GATE_DB"] = str(database)
        environment["FINTRACE_CALL_GATE_KEY"] = gate_key
    if config_path is not None:
        environment["FINTRACE_MODEL_CALL_LEDGER_CONFIG"] = str(config_path)
        environment["FINTRACE_ACTIVE_SOURCE_SHA256"] = _SOURCE_SHA
    if liveness_file is not None:
        environment["FINTRACE_CALL_LIVENESS_FILE"] = str(liveness_file)
    if worker_pid_file is not None:
        environment["FINTRACE_R2_WORKER_PID_FILE"] = str(worker_pid_file)
    if extra:
        environment.update(extra)
    return environment


def _call_context(*, gate_key: str = _GATE_KEY, stage: str = "annual_discovery") -> dict[str, object]:
    return {
        "run_id": "r2-isolated-provider-validation",
        "stage": stage,
        "batch_index": 1 if stage == "annual_discovery" else None,
        "audit_id": str(uuid.uuid4()),
        "gate_key": gate_key,
    }


def _mock_run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runs_root = tmp_path / "isolated-repo" / "artifacts" / "runs"
    runs_root.mkdir(parents=True)
    monkeypatch.setattr(audited_chat_module, "repository_runs_root", lambda: runs_root)
    run_dir = runs_root / "r2-provider-validation-run"
    run_dir.mkdir()
    return run_dir


def _write_case_evidence(name: str, payload: dict[str, object]) -> None:
    raw_directory = os.environ.get(_TEST_EVIDENCE_ENV)
    if not raw_directory:
        return
    directory = Path(raw_directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    if path.exists():
        raise FileExistsError("provider runtime evidence item already exists")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _database_attempts(database: Path, gate_key: str = _GATE_KEY) -> list[dict[str, object]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT admission_id, audit_id, status, goal_counted "
            "FROM provider_attempts WHERE gate_key = ? ORDER BY rowid",
            (gate_key,),
        ).fetchall()
    return [
        {
            "admission_id": row[0],
            "audit_id": row[1],
            "status": row[2],
            "goal_counted": bool(row[3]),
        }
        for row in rows
    ]


def _audited_call(
    messages: list[ChatMessage],
    settings: ModelSettings,
    run_dir: Path,
    *,
    timeout: float | None = None,
):
    return audited_complete_chat(
        messages,
        settings,
        run_dir=run_dir,
        evidence_refs=[EvidenceRef("local-mock-document", 1)],
        prompt_version="r2-local-fault-fixture-v1",
        timeout_seconds=timeout,
        call_stage="annual_discovery",
        batch_index=1,
    )


def _run_worker(database: Path, gate_key: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    environment = _safe_subprocess_environment(database=database, gate_key=gate_key)
    process = subprocess.Popen(
        [sys.executable, "-m", "finagent.llm.chat_worker"],
        cwd=_PROJECT_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        stdout, _ = process.communicate(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=5
        )
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=1)
        pytest.fail("test-owned chat worker did not exit within the bounded preflight window")
    output = json.loads(stdout.decode("utf-8"))
    assert isinstance(output, dict)
    return int(process.returncode or 0), output


def _start_caller_process(
    *,
    database: Path,
    config_path: Path,
    index: int,
    base_url: str = _MULTIPROCESS_BASE_URL,
    send_http_request: bool = False,
) -> subprocess.Popen[bytes]:
    program = f"""
import json, sys, uuid
from finagent.core.model_call_ledger import ModelCallAdmissionError, reserve_provider_attempt
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatMessage, ModelResponseError, complete_chat
sys.stdin.buffer.read(1)
context = {{
    "run_id": "r2-multiprocess-quota",
    "stage": "annual_discovery",
    "batch_index": {index + 1},
    "audit_id": str(uuid.uuid4()),
    "gate_key": {_GATE_KEY!r},
}}
try:
    if {send_http_request!r}:
        settings = ModelSettings({base_url!r}, {_FAKE_API_KEY!r}, {_MODEL!r}, 5.0)
        completion = complete_chat(
            [ChatMessage("user", "isolated multi-process HTTP quota fixture")],
            settings,
            call_context=context,
        )
        print(json.dumps({{"index": {index}, "outcome": "admitted", "admission_id": completion.admission_id, "audit_id": context["audit_id"], "response_id": completion.response_id}}))
    else:
        admission_id = reserve_provider_attempt({base_url!r}, {_MODEL!r}, call_context=context)
        print(json.dumps({{"index": {index}, "outcome": "admitted", "admission_id": admission_id, "audit_id": context["audit_id"]}}))
except ModelCallAdmissionError as exc:
    print(json.dumps({{"index": {index}, "outcome": "rejected", "reason": exc.code, "audit_id": context["audit_id"]}}))
except ModelResponseError as exc:
    print(json.dumps({{"index": {index}, "outcome": "rejected", "reason": exc.category, "admission_id": exc.admission_id, "audit_id": context["audit_id"]}}))
"""
    environment = _safe_subprocess_environment(
        database=database,
        config_path=config_path,
    )
    return subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=_PROJECT_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _finish_owned_callers(
    processes: list[subprocess.Popen[bytes]], timeout_seconds: float = 12.0
) -> list[tuple[int, bytes]]:
    deadline = time.monotonic() + timeout_seconds
    output: list[tuple[int, bytes]] = []
    try:
        for process in processes:
            remaining = max(0.0, deadline - time.monotonic())
            stdout, _ = process.communicate(timeout=remaining)
            output.append((int(process.returncode or 0), stdout))
    except subprocess.TimeoutExpired:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=1)
        raise AssertionError("a test-owned ledger caller missed the bounded completion deadline")
    return output


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        synchronize = 0x00100000
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _track_connector_worker_handles(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
    from finagent.llm import chat_completion as chat_module

    real_popen = chat_module.subprocess.Popen
    owned_workers: list[subprocess.Popen[bytes]] = []

    def tracked_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", [])
        if isinstance(command, (list, tuple)) and any(
            "finagent.llm.chat_worker" in str(part) for part in command
        ):
            owned_workers.append(process)
        return process

    monkeypatch.setattr(chat_module.subprocess, "Popen", tracked_popen)
    return owned_workers


def _assert_connector_workers_exited(
    owned_workers: list[subprocess.Popen[bytes]],
) -> list[dict[str, int | bool | None]]:
    assert owned_workers
    observed: list[dict[str, int | bool | None]] = []
    for process in owned_workers:
        process.wait(timeout=1)
        pid = int(process.pid)
        alive = _pid_is_alive(pid)
        assert process.poll() is not None
        assert not alive
        observed.append({"pid": pid, "return_code": process.returncode, "alive_after_wait": alive})
    return observed


def test_twelve_real_caller_processes_share_mock_quota_across_restart(tmp_path: Path) -> None:
    database = tmp_path / "isolated-mock-provider-ledger.sqlite3"
    config_path = tmp_path / "isolated-mock-provider-policy.json"
    open_admission_gate(database, _GATE_KEY)
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "database_path": str(database),
                "limit": 5,
                "provider_model_fingerprint": provider_model_fingerprint(
                    _MULTIPROCESS_BASE_URL, _MODEL
                ),
                "authorized_source_sha256s": [_SOURCE_SHA],
            }
        ),
        encoding="utf-8",
    )

    processes: list[subprocess.Popen[bytes]] = []
    restart_process: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    caller_count = 12
    try:
        for index in range(caller_count):
            processes.append(
                _start_caller_process(
                    database=database,
                    config_path=config_path,
                    index=index,
                )
            )
        for process in processes:
            assert process.stdin is not None
            process.stdin.write(b"G")
            process.stdin.flush()
            process.stdin.close()
            process.stdin = None
        outputs = _finish_owned_callers(processes)
        elapsed = time.monotonic() - started
        results = [json.loads(stdout.decode("utf-8")) for code, stdout in outputs]
        assert all(code == 0 for code, _ in outputs)
        assert len(results) == caller_count
        admitted = [item for item in results if item["outcome"] == "admitted"]
        rejected = [item for item in results if item["outcome"] == "rejected"]
        assert len(admitted) == 5
        assert len({item["admission_id"] for item in admitted}) == 5
        assert len(rejected) == 7
        assert {item["reason"] for item in rejected} == {"ledger_budget_exhausted"}
        assert ledger_attempt_count(database) == 5
        attempts = _database_attempts(database)
        assert len(attempts) == 5
        assert all(item["goal_counted"] for item in attempts)
        assert len({item["audit_id"] for item in attempts}) == 5

        restart_process = _start_caller_process(
            database=database,
            config_path=config_path,
            index=caller_count,
        )
        processes.append(restart_process)
        assert restart_process.stdin is not None
        restart_process.stdin.write(b"R")
        restart_process.stdin.flush()
        restart_process.stdin.close()
        restart_process.stdin = None
        restart_output = _finish_owned_callers([restart_process])[0]
        restart_result = json.loads(restart_output[1].decode("utf-8"))
        assert restart_output[0] == 0
        assert restart_result["outcome"] == "rejected"
        assert restart_result["reason"] == "ledger_budget_exhausted"
        assert ledger_attempt_count(database) == 5
        assert all(process.poll() is not None for process in processes)
        _write_case_evidence(
            "multi-process-shared-quota",
            {
                "scope": "real independent local Python caller processes; isolated mock quota policy",
                "caller_processes": caller_count,
                "quota_limit": 5,
                "admitted": len(admitted),
                "rejected_by_category": {"ledger_budget_exhausted": len(rejected)},
                "restart_process": restart_result["outcome"],
                "restart_rejection": restart_result["reason"],
                "ledger_final_count": ledger_attempt_count(database),
                "ledger_rows": attempts,
                "local_http_request_count": 0,
                "elapsed_seconds": elapsed,
                "child_handles_closed": all(
                    process.poll() is not None for process in processes
                ),
                "real_goal_ledger_read": False,
                "real_provider_request": False,
            },
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=1)
        if restart_process is not None and restart_process not in processes and restart_process.poll() is None:
            restart_process.terminate()
            restart_process.wait(timeout=1)


def test_twelve_connector_processes_share_mock_http_quota_across_restart(
    tmp_path: Path,
) -> None:
    server, server_thread = _serve_local_model(lambda: (200, _json_response()))
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    database = tmp_path / "isolated-mock-http-provider-ledger.sqlite3"
    config_path = tmp_path / "isolated-mock-http-provider-policy.json"
    open_admission_gate(database, _GATE_KEY)
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "database_path": str(database),
                "limit": 5,
                "provider_model_fingerprint": provider_model_fingerprint(base_url, _MODEL),
                "authorized_source_sha256s": [_SOURCE_SHA],
            }
        ),
        encoding="utf-8",
    )

    processes: list[subprocess.Popen[bytes]] = []
    started = time.monotonic()
    try:
        for index in range(12):
            processes.append(
                _start_caller_process(
                    database=database,
                    config_path=config_path,
                    index=index,
                    base_url=base_url,
                    send_http_request=True,
                )
            )
        for process in processes:
            assert process.stdin is not None
            process.stdin.write(b"G")
            process.stdin.flush()
            process.stdin.close()
            process.stdin = None
        outputs = _finish_owned_callers(processes)
        results = [json.loads(stdout.decode("utf-8")) for code, stdout in outputs]
        assert all(code == 0 for code, _ in outputs)
        assert len(results) == 12
        admitted = [item for item in results if item["outcome"] == "admitted"]
        rejected = [item for item in results if item["outcome"] == "rejected"]
        assert len(admitted) == 5
        assert len({item["admission_id"] for item in admitted}) == 5
        assert len(rejected) == 7
        assert {item["reason"] for item in rejected} == {"ledger_budget_exhausted"}
        assert _request_count(server) == 5
        assert ledger_attempt_count(database) == 5
        attempts = _database_attempts(database)
        assert len(attempts) == 5
        admitted_pairs = {(item["admission_id"], item["audit_id"]) for item in admitted}
        persisted_pairs = {(item["admission_id"], item["audit_id"]) for item in attempts}
        assert admitted_pairs == persisted_pairs
        assert all(item["goal_counted"] for item in attempts)

        restart = _start_caller_process(
            database=database,
            config_path=config_path,
            index=12,
            base_url=base_url,
            send_http_request=True,
        )
        processes.append(restart)
        assert restart.stdin is not None
        restart.stdin.write(b"R")
        restart.stdin.flush()
        restart.stdin.close()
        restart.stdin = None
        restart_code, restart_stdout = _finish_owned_callers([restart])[0]
        restart_result = json.loads(restart_stdout.decode("utf-8"))
        assert restart_code == 0
        assert restart_result["outcome"] == "rejected"
        assert restart_result["reason"] == "ledger_budget_exhausted"
        assert _request_count(server) == 5
        assert ledger_attempt_count(database) == 5
        elapsed = time.monotonic() - started
        _write_case_evidence(
            "multi-process-connector-http-quota",
            {
                "scope": "12 independent complete_chat caller processes, one loopback fake HTTP server, isolated mock policy",
                "caller_processes": 12,
                "quota_limit": 5,
                "admitted": len(admitted),
                "rejected_by_category": {"ledger_budget_exhausted": len(rejected)},
                "admission_audit_pairs_match_ledger": admitted_pairs == persisted_pairs,
                "ledger_final_count": ledger_attempt_count(database),
                "local_http_request_count": _request_count(server),
                "restart_process_outcome": restart_result["outcome"],
                "restart_rejection": restart_result["reason"],
                "request_count_after_restart": _request_count(server),
                "elapsed_seconds": elapsed,
                "caller_and_restart_handles_closed": all(
                    process.poll() is not None for process in processes
                ),
                "real_goal_ledger_read": False,
                "real_provider_request": False,
            },
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=1)
        _close_local_model(server, server_thread)


def test_gate_close_ack_rejects_stale_worker_and_later_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, thread = _serve_local_model(lambda: (200, _json_response()))
    settings = _settings(server.server_port)
    database, _config_path = _make_mock_policy(
        tmp_path, monkeypatch, settings.base_url, limit=4
    )
    context = _call_context()
    admission_id = reserve_provider_attempt(
        settings.base_url,
        settings.model,
        call_context=context,
    )
    close_started = time.monotonic()
    close_admission_gate(database, _GATE_KEY, timeout_seconds=2.0)
    close_ack_elapsed = time.monotonic() - close_started
    try:
        worker_exit, worker_result = _run_worker(
            database,
            _GATE_KEY,
            {
                "url": settings.chat_completions_url(),
                "api_key": _FAKE_API_KEY,
                "model": settings.model,
                "messages": [{"role": "user", "content": "local gate fixture"}],
                "timeout_seconds": 2.0,
                "admission_id": admission_id,
            },
        )
        assert worker_exit == 0
        assert worker_result == {
            "ok": False,
            "kind": "admission",
            "code": "call_gate_closed",
        }
        with pytest.raises(ModelResponseError) as later_call:
            complete_chat(
                [ChatMessage("user", "local gate fixture")],
                settings,
                timeout_seconds=1.0,
                call_context=_call_context(),
            )
        assert later_call.value.category == "call_gate_closed"
        assert ledger_attempt_count(database) == 1
        attempts = _database_attempts(database)
        assert len(attempts) == 1
        assert attempts[0]["status"] == "reserved"
        assert _request_count(server) == 0
        _write_case_evidence(
            "gate-close-ack",
            {
                "scope": "isolated mock policy; actual chat_worker subprocess preflight and connector admission",
                "admission_before_close": True,
                "gate_close_acknowledged": True,
                "close_ack_elapsed_seconds": close_ack_elapsed,
                "worker_result_category": worker_result["code"],
                "later_admission_category": later_call.value.category,
                "persisted_attempt_count": ledger_attempt_count(database),
                "persisted_attempt_status": attempts[0]["status"],
                "local_http_request_count": _request_count(server),
                "worker_popen_cleanup": "exited",
            },
        )
    finally:
        _close_local_model(server, thread)


def test_stopped_owner_heartbeat_ends_blocked_request_worker(
    tmp_path: Path,
) -> None:
    release_response = threading.Event()
    request_seen = threading.Event()

    def blocked_behavior() -> tuple[int, bytes]:
        request_seen.set()
        release_response.wait(timeout=12)
        return 200, _json_response()

    server, thread = _serve_local_model(blocked_behavior)
    heartbeat = tmp_path / "owner-heartbeat.txt"
    worker_pid_path = tmp_path / "owned-request-worker.pid"
    heartbeat.write_text("owner alive\n", encoding="utf-8")
    owner_program = f"""
import json, subprocess, sys
from pathlib import Path
from finagent.core.model_settings import ModelSettings
from finagent.llm import chat_completion as chat_module
from finagent.llm.chat_completion import ChatMessage
real_popen = chat_module.subprocess.Popen
def tracked_popen(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    command = args[0] if args else kwargs.get("args", [])
    if isinstance(command, (list, tuple)) and any("finagent.llm.chat_worker" in str(part) for part in command):
        Path({str(worker_pid_path)!r}).write_text(str(process.pid), encoding="ascii")
    return process
chat_module.subprocess.Popen = tracked_popen
settings = ModelSettings({f'http://127.0.0.1:{server.server_port}/v1'!r}, {_FAKE_API_KEY!r}, {_MODEL!r}, 20.0)
try:
    chat_module.complete_chat([ChatMessage("user", "blocked local request")], settings, timeout_seconds=20.0)
except Exception as exc:
    print(json.dumps({{"category": getattr(exc, "category", "call_failed")}}))
else:
    print(json.dumps({{"category": "unexpected_success"}}))
"""
    environment = _safe_subprocess_environment(
        liveness_file=heartbeat,
        worker_pid_file=worker_pid_path,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", owner_program],
        cwd=_PROJECT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()
    try:
        assert request_seen.wait(timeout=4), "local fake provider never received the owned request"
        stdout, _ = process.communicate(timeout=8)
        elapsed = time.monotonic() - started
        owner_result = json.loads(stdout.decode("utf-8"))
        assert process.returncode == 0
        assert owner_result == {"category": "cancelled"}
        assert worker_pid_path.is_file()
        worker_pid = int(worker_pid_path.read_text(encoding="ascii"))
        assert not _pid_is_alive(worker_pid)
        assert elapsed < 8
        assert elapsed >= 4.5
        _write_case_evidence(
            "owner-heartbeat-stop",
            {
                "scope": "actual connector owner and actual blocked local HTTP request worker",
                "request_observed_by_local_server": request_seen.is_set(),
                "owner_completion_category": owner_result["category"],
                "worker_process_id_created_by_owner_popen": worker_pid,
                "worker_exited": not _pid_is_alive(worker_pid),
                "elapsed_seconds": elapsed,
                "owner_handle_cleanup": "exited",
                "local_http_request_count": _request_count(server),
            },
        )
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        raise AssertionError("test-owned connector owner did not observe the stopped heartbeat by deadline")
    finally:
        release_response.set()
        _close_local_model(server, thread)


@pytest.mark.parametrize(
    ("damage", "expected_category"),
    [
        ("deleted_existing_file", "ledger_unavailable"),
        ("corrupt_database", "ledger_unavailable"),
        ("partial_schema", "ledger_unavailable"),
        ("changed_policy", "ledger_policy_mismatch"),
    ],
)
def test_broken_existing_ledger_is_not_zeroed_and_rejected_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
    expected_category: str,
) -> None:
    server, thread = _serve_local_model(lambda: (200, _json_response()))
    settings = _settings(server.server_port)
    database, config_path = _make_mock_policy(
        tmp_path, monkeypatch, settings.base_url, limit=5
    )
    first_context = _call_context()
    reserve_provider_attempt(
        settings.base_url, settings.model, call_context=first_context
    )
    count_before_damage = ledger_attempt_count(database)

    if damage == "deleted_existing_file":
        database.unlink()
    elif damage == "corrupt_database":
        database.write_bytes(b"not a sqlite database\x00r2")
    elif damage == "partial_schema":
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE provider_attempts")
    elif damage == "changed_policy":
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["limit"] = 6
        config_path.write_text(json.dumps(config), encoding="utf-8")
    else:
        raise AssertionError("unknown isolated ledger damage fixture")

    try:
        count_reader_error_before_call: str | None = None
        count_before_call: int | None = None
        try:
            count_before_call = ledger_attempt_count(database)
        except Exception as exc:
            count_reader_error_before_call = type(exc).__name__
        try:
            complete_chat(
                [ChatMessage("user", "ledger fault fixture")],
                settings,
                timeout_seconds=1.5,
                call_context=_call_context(),
            )
            call_result = {"outcome": "returned", "category": None}
        except Exception as exc:
            category = getattr(exc, "category", None)
            call_result = {
                "outcome": "raised",
                "exception_type": type(exc).__name__,
                "category": category if isinstance(category, str) else None,
            }
        database_exists_after_check = database.exists()
        count_after_check: int | None = None
        count_reader_error: str | None = None
        try:
            count_after_check = ledger_attempt_count(database)
        except ModelCallLedgerReadError as exc:
            count_reader_error = type(exc).__name__
        direct_runtime_entrypoints: dict[str, str] = {}
        if damage == "deleted_existing_file":
            direct_operations = {
                "reserve": lambda: reserve_provider_attempt(
                    settings.base_url,
                    settings.model,
                    call_context=_call_context(),
                ),
                "open_gate": lambda: open_admission_gate(database, _GATE_KEY),
                "close_gate": lambda: close_admission_gate(database, _GATE_KEY),
            }
            for operation_name, operation in direct_operations.items():
                try:
                    operation()
                    direct_runtime_entrypoints[operation_name] = "returned"
                except ModelCallAdmissionError as exc:
                    direct_runtime_entrypoints[operation_name] = exc.code
        request_count = _request_count(server)
        _write_case_evidence(
            f"ledger-fail-closed-{damage}",
            {
                "scope": "isolated mock policy; local HTTP fake used only to count any outbound request",
                "damage_case": damage,
                "count_before_damage": count_before_damage,
                "count_before_call": count_before_call,
                "count_reader_error_before_call": count_reader_error_before_call,
                "call_result": call_result,
                "local_http_request_count": request_count,
                "database_exists_after_check": database_exists_after_check,
                "count_after_check": count_after_check,
                "count_reader_error_type": count_reader_error,
                "direct_runtime_entrypoint_results": direct_runtime_entrypoints,
                "database_exists_after_direct_runtime_entrypoints": database.exists(),
            },
        )
        assert call_result["outcome"] == "raised"
        assert call_result["exception_type"] == "ModelResponseError"
        assert call_result["category"] == expected_category
        assert request_count == 0
        if damage in {"deleted_existing_file", "corrupt_database", "partial_schema"}:
            assert count_reader_error == "ModelCallLedgerReadError"
        if damage == "deleted_existing_file":
            assert not database_exists_after_check, "missing established ledger was recreated as a fresh database file"
            assert direct_runtime_entrypoints == {
                "reserve": "ledger_unavailable",
                "open_gate": "ledger_unavailable",
                "close_gate": "ledger_unavailable",
            }
            assert not database.exists()
        elif damage in {"corrupt_database", "partial_schema"}:
            with pytest.raises(ModelCallLedgerReadError):
                ledger_attempt_count(database)
        else:
            assert ledger_attempt_count(database) == 1
    finally:
        _close_local_model(server, thread)


@pytest.mark.parametrize(
    ("status_code", "expected_category", "expected_attempts"),
    [
        (401, "http_401", 1),
        (403, "http_403", 1),
        (429, "http_429", 3),
        (503, "http_5xx", 3),
    ],
)
def test_http_fault_retries_are_bounded_and_mapped_to_real_audit_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_category: str,
    expected_attempts: int,
) -> None:
    server, thread = _serve_local_model(
        lambda: (status_code, b'{"error":"isolated local provider fault"}')
    )
    settings = _settings(server.server_port, timeout=5.0)
    database, _config_path = _make_mock_policy(
        tmp_path, monkeypatch, settings.base_url, limit=10
    )
    run_dir = _mock_run_dir(monkeypatch, tmp_path)
    started = time.monotonic()
    try:
        with pytest.raises(ModelResponseError) as rejected:
            _audited_call(
                [ChatMessage("user", "HTTP status fixture")],
                settings,
                run_dir,
                timeout=5.0,
            )
        elapsed = time.monotonic() - started
        assert rejected.value.category == expected_category
        records = list(getattr(rejected.value, "attempt_records"))
        assert len(records) == expected_attempts
        assert getattr(rejected.value, "provider_admission_count") == expected_attempts
        assert getattr(rejected.value, "response_count") == 0
        assert _request_count(server) == expected_attempts
        assert elapsed < 5.0

        attempts = _database_attempts(database)
        assert len(attempts) == expected_attempts
        assert len({item["admission_id"] for item in attempts}) == expected_attempts
        assert len({item["audit_id"] for item in attempts}) == expected_attempts
        request_ids: list[str] = []
        audit_dirs = list(run_dir.iterdir())
        assert len(audit_dirs) == 1
        for attempt_index in range(1, expected_attempts + 1):
            attempt_dir = audit_dirs[0] / f"attempt-{attempt_index:02d}"
            request = json.loads((attempt_dir / "request.json").read_text(encoding="utf-8"))
            failure = json.loads((attempt_dir / "failure.json").read_text(encoding="utf-8"))
            request_ids.append(request["audit_id"])
            assert request["attempt_index"] == attempt_index
            assert failure["audit_id"] == request["audit_id"]
            assert failure["category"] == expected_category
        assert len(set(request_ids)) == expected_attempts
        assert {item["audit_id"] for item in attempts} == set(request_ids)
        assert ledger_attempt_count(database) == expected_attempts
        _write_case_evidence(
            f"http-fault-{status_code}",
            {
                "scope": "real local HTTP request workers with isolated mock policy and durable audit files",
                "http_status": status_code,
                "safe_failure_category": rejected.value.category,
                "local_http_request_count": _request_count(server),
                "admission_count": getattr(rejected.value, "provider_admission_count"),
                "durable_accepted_response_count": getattr(rejected.value, "response_count"),
                "attempt_records": records,
                "attempt_audit_ids": request_ids,
                "ledger_attempt_count": ledger_attempt_count(database),
                "elapsed_seconds": elapsed,
                "retry_limit_respected": expected_attempts <= 3,
            },
        )
    finally:
        _close_local_model(server, thread)


def test_unreachable_local_endpoint_is_bounded_and_counted_without_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    settings = _settings_for_url(f"http://127.0.0.1:{closed_port}/v1", timeout=5.0)
    database, _config_path = _make_mock_policy(
        tmp_path, monkeypatch, settings.base_url, limit=5
    )
    run_dir = _mock_run_dir(monkeypatch, tmp_path)
    started = time.monotonic()
    try:
        with pytest.raises(ModelResponseError) as rejected:
            _audited_call(
                [ChatMessage("user", "connection refusal fixture")],
                settings,
                run_dir,
                timeout=5.0,
            )
        elapsed = time.monotonic() - started
        records = list(getattr(rejected.value, "attempt_records"))
        admission_count = int(getattr(rejected.value, "provider_admission_count"))
        response_count = int(getattr(rejected.value, "response_count"))
        ledger_count = ledger_attempt_count(database)
        _write_case_evidence(
            "unreachable-loopback-endpoint",
            {
                "scope": "reserved local loopback port held without an HTTP listener; isolated mock policy",
                "safe_failure_category": rejected.value.category,
                "attempt_records": records,
                "admission_count": admission_count,
                "response_count": response_count,
                "ledger_attempt_count": ledger_count,
                "elapsed_seconds": elapsed,
                "retry_limit_respected": len(records) <= 3,
                "response_received": False,
                "external_network": False,
            },
        )
        assert rejected.value.category in {
            "connection_error",
            "timeout",
            "call_deadline_exhausted",
        }
        assert 1 <= len(records) <= 3
        assert 1 <= admission_count <= len(records)
        assert response_count == 0
        assert ledger_count == admission_count
        assert elapsed < 5.8
    finally:
        probe.close()


def test_blocked_local_response_obeys_deadline_and_worker_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_response = threading.Event()
    request_seen = threading.Event()

    def blocked_behavior() -> tuple[int, bytes]:
        request_seen.set()
        release_response.wait(timeout=5)
        return 200, _json_response()

    server, thread = _serve_local_model(blocked_behavior)
    settings = _settings(server.server_port, timeout=0.45)
    database, _config_path = _make_mock_policy(
        tmp_path, monkeypatch, settings.base_url, limit=5
    )
    run_dir = _mock_run_dir(monkeypatch, tmp_path)
    owned_workers = _track_connector_worker_handles(monkeypatch)
    started = time.monotonic()
    try:
        with pytest.raises(ModelResponseError) as rejected:
            _audited_call(
                [ChatMessage("user", "blocked local response fixture")],
                settings,
                run_dir,
                timeout=0.45,
            )
        elapsed = time.monotonic() - started
        assert request_seen.is_set()
        assert rejected.value.category == "timeout"
        assert getattr(rejected.value, "provider_admission_count") == 1
        assert getattr(rejected.value, "response_count") == 0
        assert ledger_attempt_count(database) == 1
        assert 0.35 <= elapsed < 2.0
        worker_exits = _assert_connector_workers_exited(owned_workers)
        assert len(worker_exits) == 1
        _write_case_evidence(
            "blocked-http-deadline",
            {
                "scope": "actual connector worker and blocked local HTTP handler with bounded caller deadline",
                "local_http_request_count": _request_count(server),
                "safe_failure_category": rejected.value.category,
                "elapsed_seconds": elapsed,
                "admission_count": getattr(rejected.value, "provider_admission_count"),
                "durable_response_count": getattr(rejected.value, "response_count"),
                "ledger_attempt_count": ledger_attempt_count(database),
                "owned_worker_processes": worker_exits,
                "caller_worker_handle_cleanup": "actual connector Popen waited; PID exited",
            },
        )
    finally:
        release_response.set()
        _close_local_model(server, thread)


def test_dripping_local_response_obeys_deadline_and_worker_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, thread = _serve_dripping_model()
    settings = _settings(server.server_port, timeout=0.45)
    database, _config_path = _make_mock_policy(
        tmp_path, monkeypatch, settings.base_url, limit=5
    )
    run_dir = _mock_run_dir(monkeypatch, tmp_path)
    owned_workers = _track_connector_worker_handles(monkeypatch)
    started = time.monotonic()
    try:
        with pytest.raises(ModelResponseError) as rejected:
            _audited_call(
                [ChatMessage("user", "dripping local response fixture")],
                settings,
                run_dir,
                timeout=0.45,
            )
        elapsed = time.monotonic() - started
        assert server.state["request_seen"].is_set()  # type: ignore[attr-defined]
        assert rejected.value.category == "timeout"
        assert getattr(rejected.value, "provider_admission_count") == 1
        assert getattr(rejected.value, "response_count") == 0
        assert ledger_attempt_count(database) == 1
        assert 0.35 <= elapsed < 2.0
        worker_exits = _assert_connector_workers_exited(owned_workers)
        assert len(worker_exits) == 1
        _write_case_evidence(
            "dripping-http-deadline",
            {
                "scope": "actual connector worker reading an incomplete, steadily dripping local HTTP response",
                "local_http_request_count": _request_count(server),
                "safe_failure_category": rejected.value.category,
                "elapsed_seconds": elapsed,
                "admission_count": getattr(rejected.value, "provider_admission_count"),
                "durable_response_count": getattr(rejected.value, "response_count"),
                "ledger_attempt_count": ledger_attempt_count(database),
                "owned_worker_processes": worker_exits,
                "caller_worker_handle_cleanup": "actual connector Popen waited; PID exited",
            },
        )
    finally:
        server.state["stop"].set()  # type: ignore[attr-defined]
        _close_local_model(server, thread)


def test_durable_response_stays_counted_after_discovery_semantic_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_semantic_text = "not a valid discovery JSON response"
    server, thread = _serve_local_model(lambda: (200, _json_response(invalid_semantic_text)))
    settings = _settings(server.server_port)
    database, _config_path = _make_mock_policy(
        tmp_path, monkeypatch, settings.base_url, limit=5
    )
    run_dir = _mock_run_dir(monkeypatch, tmp_path)
    try:
        result = _audited_call(
            [ChatMessage("user", "semantic response fixture")], settings, run_dir
        )
        fragment = DiscoveryFragment(
            evidence_id="local:evidence:1",
            pdf_page=1,
            block_index=0,
            start_char=0,
            bbox=(0.0, 0.0, 100.0, 20.0),
            raw_text="Only an isolated synthetic source fragment.",
        )
        batch = DiscoveryBatch(
            batch_index=1,
            fragments=(fragment,),
            raw_chars=len(fragment.raw_text),
            serialized_input_chars=64,
        )
        with pytest.raises(ValueError) as semantic_rejection:
            parse_annual_discovery_response_v2(result.completion.text, batch)
        assert semantic_rejection.value.args[0] == "invalid_model_json"
        assert result.provider_admission_count == 1
        assert result.response_count == 1
        assert result.attempt_records[0]["provider_admitted"] is True
        assert result.attempt_records[0]["response_accepted"] is True
        assert ledger_attempt_count(database) == 1
        assert _request_count(server) == 1
        audit_record = json.loads(
            (result.audit_dir / "response.json").read_text(encoding="utf-8")
        )
        assert audit_record["status"] == "succeeded"
        assert audit_record["provider_admission_count"] == 1
        assert audit_record["response_count"] == 1
        _write_case_evidence(
            "durable-success-semantic-rejection",
            {
                "scope": "real local provider response, durable audit, isolated mock ledger, existing discovery parser",
                "local_http_request_count": _request_count(server),
                "semantic_rejection_category": semantic_rejection.value.args[0],
                "durable_audit_status": audit_record["status"],
                "admission_count": audit_record["provider_admission_count"],
                "response_count": audit_record["response_count"],
                "ledger_attempt_count": ledger_attempt_count(database),
                "response_body_archived_in_evidence": False,
            },
        )
    finally:
        _close_local_model(server, thread)
