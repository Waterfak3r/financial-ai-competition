"""R4 issue-pipeline recovery and admission checks through real local processes.

The opt-in runner freezes an allowlisted copy of the current source, starts the
normal Uvicorn API, and directs every model request to a 127.0.0.1 OpenAI
fixture. Fixtures contain only original synthetic annual-report text. The
strict reader and ledger observer run against the copied runtime.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from typing import Any, Mapping
from ctypes import wintypes
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pymupdf
import pytest


_ROOT = Path(__file__).resolve().parents[2]
_ATTEMPT_ENV = "FINTRACE_V11_RUNTIME_ATTEMPT_DIR"
_FREEZE_ENV = "FINTRACE_V11_RUNTIME_SOURCE_FREEZE_SHA256"
_STABLE_ENV = "FINTRACE_V11_RUNTIME_SOURCE_STABLE"
_MODEL = "fintrace-r4-runtime-fault-reuse-loopback"
_DUMMY_KEY = "r4-runtime-fault-reuse-synthetic-loopback-key"
_SEMANTIC_DRIFT_SOURCE_TEXT = "附注说明，合同回款的后续结算时点需要与账龄分类逐项对照。"
_FIXTURE_SHARED_QUOTE = "合同回款台账和账龄核对只用于检验原文定位"
_REQUIRED_STAGE_COUNTS = {"grouping", "global", "supplement", "review"}
_API_JOB_POLL_INDEX = count()
_API_RUNTIME_IDENTITY: dict[str, Any] | None = None
_POPEN_EVENTS_PATH: Path | None = None
_ACTUAL_CLI_IDENTITY_CAPTURE_DIR: Path | None = None
pytestmark = pytest.mark.skipif(
    not os.environ.get(_ATTEMPT_ENV),
    reason=f"由 artifacts/tmp runner 设置 {_ATTEMPT_ENV} 后运行真实 API/CLI。",
)


class _HarnessOwnedPopen(subprocess.Popen):
    """Pass this exact harness Popen object's identity into its child startup."""

    def __init__(self, args, *positional, **keywords):
        raw_env = keywords.get("env")
        child_env = dict(os.environ if raw_env is None else raw_env)
        child_env["FINTRACE_R4_RUNTIME_PARENT_POPEN_OBJECT_IDENTITY"] = hex(id(self))
        child_env["FINTRACE_R4_RUNTIME_PARENT_ROLE"] = "harness"
        child_env["FINTRACE_R4_RUNTIME_PARENT_PID"] = str(os.getpid())
        keywords["env"] = child_env
        super().__init__(args, *positional, **keywords)


def test_r4_issue_reuse_and_shared_gate_faults_use_standard_api(tmp_path: Path) -> None:
    """Keep every formal attempt immutable and fail closed on source drift."""

    attempt = Path(os.environ[_ATTEMPT_ENV]).resolve()
    if not attempt.is_dir():
        raise AssertionError("formal_attempt_directory_missing")
    _validate_attempt_location(attempt)
    expected_freeze = os.environ.get(_FREEZE_ENV, "")
    if os.environ.get(_STABLE_ENV) != "1" or len(expected_freeze) != 64:
        raise AssertionError("mainline_source_stability_confirmation_missing")

    snapshot_root = attempt / "snapshot" / "project"
    evidence_root = attempt / "cases"
    evidence_root.mkdir(parents=True, exist_ok=False)
    _write_json_new(attempt / "attempt-start.json", {
        "task": "R4 issue runtime fault and reuse",
        "attempt_id": attempt.name,
        "started_at_utc": _utc_now(),
        "started_monotonic": time.monotonic(),
        "confirmed_source_freeze_sha256": expected_freeze,
        "stable_source_confirmation": True,
        "provider_policy": "loopback only; synthetic key only; no workspace model settings read",
        "external_provider_calls": 0,
    })

    source_before = _source_manifest(_ROOT)
    _write_json_new(attempt / "source-manifest-before.json", source_before)
    if source_before["sha256"] != expected_freeze:
        raise AssertionError("workspace_source_does_not_match_confirmed_freeze")
    _capture_allowlist(snapshot_root)
    source_after_copy = _source_manifest(_ROOT)
    snapshot_manifest = _source_manifest(snapshot_root)
    _write_json_new(attempt / "source-manifest-after-copy.json", source_after_copy)
    _write_json_new(attempt / "snapshot-source-manifest-before.json", snapshot_manifest)
    if source_before != source_after_copy or source_before != snapshot_manifest:
        raise AssertionError("allowlist_snapshot_changed_during_copy")

    runtime_src = snapshot_root / "backend" / "src"
    codec = _load_snapshot_codec(runtime_src / "finagent" / "agents" / "annual_review_wire.py")
    provider = _LoopbackProvider(codec)
    api_process: subprocess.Popen[bytes] | None = None
    api_stdout = (attempt / "api.stdout.raw").open("xb")
    api_stderr = (attempt / "api.stderr.raw").open("xb")
    outcome: dict[str, Any] = {"cases": {}, "failures": {}}
    try:
        fixtures = _create_controlled_sources(snapshot_root, evidence_root)
        source_hashes = sorted({item["sha256"] for item in fixtures.values()})
        provider.start()
        model_file = snapshot_root / ".env.model"
        _write_dummy_model_settings(model_file, _MODEL, provider.base_url)
        ledger_path = snapshot_root / "artifacts" / "provider-call-ledger.sqlite3"
        ledger_config_path = attempt / "provider-call-ledger-config.json"
        _write_json_new(ledger_config_path, {
            "version": 1,
            "database_path": str(ledger_path),
            "limit": 80,
            "provider_model_fingerprint": _provider_fingerprint(provider.base_url, _MODEL),
            "authorized_source_sha256s": source_hashes,
        })
        _initialize_ledger(snapshot_root, ledger_config_path)
        cli_identity_capture_dir = evidence_root / "actual-cli-identities"
        cli_identity_capture_dir.mkdir(parents=True, exist_ok=False)
        _write_json_new(attempt / "provider-policy.json", {
            "provider": "OpenAI-compatible fake bound to 127.0.0.1",
            "base_url": provider.base_url,
            "model": _MODEL,
            "limit": 80,
            "authorized_source_sha256s": source_hashes,
            "ledger_path": str(ledger_path),
            "dummy_api_key_only": True,
            "snapshot_local_dummy_model_settings_sha256": _sha256(model_file),
            "actual_cli_identity_capture_directory": str(cli_identity_capture_dir),
            "actual_cli_identity_capture_method": "snapshot sitecustomize sys.setprofile return event from capture_cli_runtime_identity in the real CLI Popen child",
            "loopback_input_decoder_module": str(runtime_src / "finagent" / "agents" / "annual_review_wire.py"),
            "loopback_input_decoder_sha256": _sha256(runtime_src / "finagent" / "agents" / "annual_review_wire.py"),
            "real_workspace_model_configuration_read": False,
            "external_provider_calls": 0,
        })

        popen_events = attempt / "api-owned-cli-popen.jsonl"
        global _POPEN_EVENTS_PATH
        _POPEN_EVENTS_PATH = popen_events
        _write_popen_observer(snapshot_root / "backend" / "src" / "sitecustomize.py", popen_events)
        _append_actual_cli_identity_profile(snapshot_root / "backend" / "src" / "sitecustomize.py")
        api_env = _minimal_api_environment(snapshot_root, ledger_config_path, popen_events)
        api_env["FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR"] = str(cli_identity_capture_dir)
        api_env["FINTRACE_R4_RUNTIME_SOURCE_SHA256"] = snapshot_manifest["sha256"]
        api_port = _free_loopback_port()
        base_url = f"http://127.0.0.1:{api_port}"
        api_argv = [
            sys.executable, "-m", "uvicorn", "finagent.api.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(api_port),
            "--log-level", "warning", "--no-access-log",
        ]
        api_start_monotonic = time.monotonic()
        api_start_utc = _utc_now()
        api_process = _HarnessOwnedPopen(
            api_argv,
            cwd=snapshot_root,
            env=api_env,
            stdout=api_stdout,
            stderr=api_stderr,
            shell=False,
        )
        api_owned_handle_identity = _native_popen_identity(api_process)
        _write_json_new(attempt / "api-process-start.json", {
            "argv": api_argv,
            "cwd": str(snapshot_root),
            "role": "api",
            "parent_role": "harness",
            "parent_pid": os.getpid(),
            "pid": api_process.pid,
            "started_at_utc": api_start_utc,
            "started_monotonic": api_start_monotonic,
            "loopback_base_url": base_url,
            "process_creation": _process_creation_metadata(api_process.pid),
            "popen_object_identity": hex(id(api_process)),
            "owned_handle_identity": api_owned_handle_identity,
            "process_owner_is_harness_popen_object": True,
            "runtime_source_manifest_sha256": snapshot_manifest["sha256"],
        })
        _wait_api_ready(base_url, api_process, timeout=30)
        runtime_identity = _request_json(base_url, "GET", "/v1/runtime-identity", None, attempt / "api-http-runtime-identity")
        api_identity = runtime_identity["json"]
        _assert_runtime_identity(api_identity.get("api"), "api", snapshot_manifest)
        _assert_runtime_identity(api_identity.get("cli_at_startup"), "cli", snapshot_manifest)
        if api_identity["api"].get("process_id") != api_process.pid:
            raise AssertionError("actual_api_runtime_identity_does_not_match_harness_owned_popen")
        global _API_RUNTIME_IDENTITY
        global _ACTUAL_CLI_IDENTITY_CAPTURE_DIR
        _API_RUNTIME_IDENTITY = dict(api_identity)
        _ACTUAL_CLI_IDENTITY_CAPTURE_DIR = cli_identity_capture_dir
        _API_RUNTIME_IDENTITY["actual_cli_identity_capture_directory"] = str(cli_identity_capture_dir)

        # First exercise the real phases to prove each controlled response is
        # reachable before we target a fault at its admission boundary.
        provider.configure(fixtures["preflight"]["document_id"], {"mode": "retry_grouping_many"})
        preflight_initial = _run_new_job(base_url, fixtures["preflight"], evidence_root / "preflight", provider, snapshot_root, ledger_path)
        outcome["cases"]["preflight"] = preflight_initial
        _assert_retry_attempts(preflight_initial, "grouping")
        provider.configure(fixtures["preflight"]["document_id"], {"mode": "success_many"})
        preflight = _complete_issue_resume_chain(
            base_url, preflight_initial, evidence_root / "preflight-resume",
            provider, snapshot_root, ledger_path, outcome["cases"], "preflight_resume",
        )
        _assert_completed_issue_stages(preflight)
        _assert_preflight_semantic_pressure(preflight, fixtures["preflight"], provider)
        preflight_stage_events = _events_by_stage(preflight_initial["resume_chain"]["all_provider_http_events"])
        if not all(preflight_stage_events.get(stage) for stage in _REQUIRED_STAGE_COUNTS):
            raise AssertionError("r4_phase_preflight_not_reachable")

        if os.environ.get("FINTRACE_R4_RUNTIME_PREFLIGHT_ONLY") == "1":
            all_provider_events = provider.events()
            sqlite_snapshot = _capture_ledger_read_only(ledger_path, attempt / "final-goal-ledger")
            _write_json_new(attempt / "provider-http-events-index.json", {
                "events_file": str(attempt / "provider-http-events.jsonl"),
                "event_count": len(all_provider_events),
                "actual_http_peak": provider.actual_http_peak,
                "only_loopback_clients": all(item.get("peer") == "127.0.0.1" for item in all_provider_events),
                "only_dummy_authorization": all(item.get("auth_is_dummy") is True for item in all_provider_events),
                "external_provider_calls": 0,
            })
            _write_bytes_new(attempt / "provider-http-events.jsonl", provider.raw_event_lines())
            _write_json_new(attempt / "final-goal-ledger-read-only.json", sqlite_snapshot)
            _write_popen_process_identity(popen_events, attempt, outcome["cases"], _API_RUNTIME_IDENTITY or {}, snapshot_manifest)
            _write_json_new(attempt / "runtime-cli-popen-events.json", _read_jsonl(popen_events))
            _write_json_new(attempt / "source-manifest-after-run.json", _source_manifest(_ROOT))
            _write_json_new(attempt / "snapshot-source-manifest-after-run.json", _source_manifest(snapshot_root))
            if _source_manifest(_ROOT) != source_before or _source_manifest(snapshot_root) != source_before:
                raise AssertionError("runtime_or_workspace_source_changed_during_attempt")
            outcome.update({
                "status": "preflight_only_passed",
                "attempted_case_names": list(outcome["cases"]),
                "provider_http_request_count": len(all_provider_events),
                "sqlite_goal_attempt_count": sqlite_snapshot["goal_attempt_count"],
                "external_provider_calls": 0,
                "source_manifest_sha256_before_after": source_before["sha256"],
                "snapshot_manifest_sha256_before_after": source_before["sha256"],
            })
            return

        # First recoverable parent: one real discovery batch returns a valid
        # HTTP envelope containing an invalid business result. All other work,
        # including the review, reaches successful durable audit artifacts.
        resume_fixture = fixtures["resume"]
        provider.configure(resume_fixture["document_id"], {"mode": "invalid_batch_2_many"})
        resume_parent = _run_new_job(base_url, resume_fixture, evidence_root / "reuse-parent", provider, snapshot_root, ledger_path)
        outcome["cases"]["reuse_parent"] = resume_parent
        _assert_recoverable_issue_parent(resume_parent)
        parent_hashes = _hash_run_and_report(snapshot_root, resume_parent["run_id"])

        # A same-input, same-category discovery retry makes real progress at
        # the provider boundary but leaves the issue/review semantic inputs
        # byte-for-byte equivalent. Successful R4 phase audits must be reused.
        provider.configure(resume_fixture["document_id"], {"mode": "invalid_batch_2_many"})
        reuse_child = _resume_job(base_url, resume_parent, evidence_root / "reuse-child", provider, snapshot_root, ledger_path)
        outcome["cases"]["reuse_child"] = reuse_child
        _assert_zero_call_issue_reuse(resume_parent, reuse_child, provider, resume_fixture["document_id"])
        reuse_parent_hashes_after = _hash_run_and_report(snapshot_root, resume_parent["run_id"])
        reuse_child["parent_archive_hash_check"] = {
            "run_id": resume_parent["run_id"],
            "before": parent_hashes,
            "after": reuse_parent_hashes_after,
            "unchanged": reuse_parent_hashes_after == parent_hashes,
        }
        _write_json_new(Path(reuse_child["case_directory"]) / "parent-archive-hash-check.json", reuse_child["parent_archive_hash_check"])
        if reuse_parent_hashes_after != parent_hashes:
            raise AssertionError("parent_run_changed_after_same_input_resume")

        # The next legal child resolves that unfinished discovery batch with a
        # new cited member. Changed coverage/member inputs invalidate the
        # affected grouping/global/supplement/review result and create new audits.
        provider.configure(resume_fixture["document_id"], {"mode": "resolve_batch_2_retry_grouping_many"})
        drift_child = _resume_job(base_url, reuse_child, evidence_root / "business-input-drift-child", provider, snapshot_root, ledger_path)
        outcome["cases"]["business_input_drift_child"] = drift_child
        _assert_business_input_drift_not_reused(reuse_child, drift_child)
        drift_parent_hashes_after = _hash_run_and_report(snapshot_root, resume_parent["run_id"])
        drift_child["original_parent_archive_hash_check"] = {
            "run_id": resume_parent["run_id"],
            "before": parent_hashes,
            "after": drift_parent_hashes_after,
            "unchanged": drift_parent_hashes_after == parent_hashes,
        }
        _write_json_new(Path(drift_child["case_directory"]) / "original-parent-archive-hash-check.json", drift_child["original_parent_archive_hash_check"])
        if drift_parent_hashes_after != parent_hashes:
            raise AssertionError("original_parent_run_changed_after_business_progress")

        # The same pending-input contract may require several legal children:
        # every child keeps the production two-admission issue gate.
        provider.configure(resume_fixture["document_id"], {"mode": "resolve_batch_2_many"})
        drift_continuation = _complete_issue_resume_chain(
            base_url, drift_child, evidence_root / "business-input-drift-continuation",
            provider, snapshot_root, ledger_path, outcome["cases"], "business_input_drift_continuation",
            ancestor_hashes={resume_parent["run_id"]: parent_hashes, reuse_child["run_id"]: reuse_child["artifact_hashes"]},
        )
        _assert_changed_issue_inputs_continued(reuse_child, drift_child, drift_continuation)

        # The API-owned process cancellation closes the common R4 gate while a
        # grouping request is already in flight. Its late reply cannot alter
        # the sealed partial run or admit any later phase.
        group_fixture = fixtures["cancel_group"]
        provider.configure(group_fixture["document_id"], {"mode": "block_grouping"})
        group_cancel = _run_cancel_at_stage(base_url, group_fixture, evidence_root / "cancel-group", provider, snapshot_root, ledger_path, "grouping")
        outcome["cases"]["cancel_group"] = group_cancel

        # The shared issue-phase deadline is exercised only after preflight
        # proved global is reachable. The fake server releases a valid late
        # response after the copied CLI reaches its bounded phase cutoff.
        global_fixture = fixtures["deadline_global"]
        global_cutoff = _run_delayed_phase_cutoff(base_url, global_fixture, evidence_root / "deadline-global", provider, snapshot_root, ledger_path, "global")
        outcome["cases"]["deadline_global"] = global_cutoff

        # A real provider 401 at supplement must be counted in the same goal
        # ledger and prevent a later review dispatch after the terminal gate.
        supplement_fixture = fixtures["http401_supplement"]
        provider.configure(supplement_fixture["document_id"], {"mode": "http401_supplement_many"})
        supplement_401 = _run_fault_job(base_url, supplement_fixture, evidence_root / "http401-supplement", provider, snapshot_root, ledger_path)
        outcome["cases"]["http401_supplement"] = supplement_401
        _assert_supplement_401_closed_gate(supplement_401, provider, supplement_fixture["document_id"])

        # End the actual API job owner while its child CLI and HTTP worker are
        # blocked. Stop only the child processes whose Popen handles and
        # creation identities were recorded by that API/CLI parent chain.
        owner_fixture = fixtures["owner_hard_exit"]
        provider.configure(owner_fixture["document_id"], {"mode": "block_grouping"})
        owner_exit = _run_owner_hard_exit(
            base_url, owner_fixture, evidence_root / "owner-hard-exit", provider,
            snapshot_root, ledger_path, api_process, _API_RUNTIME_IDENTITY or {}, popen_events,
        )
        outcome["cases"]["owner_hard_exit"] = owner_exit

        all_provider_events = provider.events()
        sqlite_snapshot = _capture_ledger_read_only(ledger_path, attempt / "final-goal-ledger")
        _write_json_new(attempt / "provider-http-events-index.json", {
            "events_file": str(attempt / "provider-http-events.jsonl"),
            "event_count": len(all_provider_events),
            "actual_http_peak": provider.actual_http_peak,
            "only_loopback_clients": all(item.get("peer") == "127.0.0.1" for item in all_provider_events),
            "only_dummy_authorization": all(item.get("auth_is_dummy") is True for item in all_provider_events),
            "external_provider_calls": 0,
        })
        _write_bytes_new(attempt / "provider-http-events.jsonl", provider.raw_event_lines())
        _write_json_new(attempt / "final-goal-ledger-read-only.json", sqlite_snapshot)
        if sqlite_snapshot["goal_attempt_count"] > 80:
            raise AssertionError("synthetic_goal_ledger_exceeded_80")
        if not all(item.get("job_owner_monotonic") for item in outcome["cases"].values()):
            raise AssertionError("case_missing_job_owner_monotonic_boundary")
        _write_popen_process_identity(popen_events, attempt, outcome["cases"], _API_RUNTIME_IDENTITY or {}, snapshot_manifest)
        _write_json_new(attempt / "runtime-cli-popen-events.json", _read_jsonl(popen_events))
        _write_json_new(attempt / "source-manifest-after-run.json", _source_manifest(_ROOT))
        _write_json_new(attempt / "snapshot-source-manifest-after-run.json", _source_manifest(snapshot_root))
        if _source_manifest(_ROOT) != source_before or _source_manifest(snapshot_root) != source_before:
            raise AssertionError("runtime_or_workspace_source_changed_during_attempt")
        outcome.update({
            "status": "passed",
            "attempted_case_names": list(outcome["cases"]),
            "provider_http_request_count": len(all_provider_events),
            "sqlite_goal_attempt_count": sqlite_snapshot["goal_attempt_count"],
            "external_provider_calls": 0,
            "source_manifest_sha256_before_after": source_before["sha256"],
            "snapshot_manifest_sha256_before_after": source_before["sha256"],
        })
    except Exception as exc:
        outcome["failures"]["runtime"] = {"type": type(exc).__name__, "message": str(exc)}
        outcome["status"] = "failed"
        with (attempt / "provider-http-events.partial.jsonl").open("xb") as output:
            output.write(provider.raw_event_lines())
        raise
    finally:
        provider.release_all()
        api_stop_action = "owner_hard_exit_kill" if "owner_hard_exit" in outcome.get("cases", {}) else "already_exited"
        start_record = _read_json(attempt / "api-process-start.json") if (attempt / "api-process-start.json").is_file() else {}
        expected_api_handle = start_record.get("owned_handle_identity")
        api_handle_before_stop = _native_popen_identity(api_process) if api_process is not None else None
        api_owned_identity_ok = (
            api_process is not None
            and start_record.get("pid") == api_process.pid
            and start_record.get("popen_object_identity") == hex(id(api_process))
            and start_record.get("process_owner_is_harness_popen_object") is True
            and isinstance(expected_api_handle, Mapping)
            and api_handle_before_stop == expected_api_handle
        )
        if api_process is not None and api_process.poll() is None:
            if api_owned_identity_ok:
                api_stop_action = "terminate_owned_popen_handle"
                api_process.terminate()
                try:
                    api_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    api_handle_before_kill = _native_popen_identity(api_process)
                    if api_handle_before_kill != expected_api_handle:
                        api_stop_action = "identity_changed_refused_kill"
                    else:
                        api_stop_action = "kill_after_timeout_on_verified_popen_handle"
                        api_process.kill()
                        api_process.wait(timeout=5)
            else:
                api_stop_action = "identity_not_verified_refused_stop"
        if api_process is not None:
            runtime_api = (_API_RUNTIME_IDENTITY or {}).get("api", {})
            api_handle_after_wait = _native_popen_identity(api_process)
            api_identity_match = (
                start_record.get("pid") == api_process.pid
                and start_record.get("process_owner_is_harness_popen_object") is True
                and start_record.get("popen_object_identity") == hex(id(api_process))
                and isinstance(expected_api_handle, Mapping)
                and api_handle_before_stop == expected_api_handle
                and api_handle_after_wait == expected_api_handle
                and isinstance(runtime_api, Mapping)
                and runtime_api.get("process_id") == api_process.pid
                and api_process.poll() is not None
            )
            _write_json_new(attempt / "api-process-exit.json", {
                "pid": api_process.pid,
                "returncode": api_process.returncode,
                "exit_observed": api_process.poll() is not None,
                "popen_object_identity": hex(id(api_process)),
                "owner_stop_action": api_stop_action,
                "owned_handle_identity_before_stop": api_handle_before_stop,
                "owned_handle_identity_after_wait": api_handle_after_wait,
                "actual_api_runtime_process_id": runtime_api.get("process_id") if isinstance(runtime_api, Mapping) else None,
                "api_popen_identity_verified": api_identity_match,
                "finished_at_utc": _utc_now(),
                "finished_monotonic": time.monotonic(),
            })
            if not api_identity_match:
                outcome["failures"]["api_popen_identity"] = {"type": "AssertionError", "message": "api_popen_identity_or_stop_not_verified"}
                outcome["status"] = "failed"
        _write_json_new(attempt / "attempt-result.json", outcome)
        provider.close()
        api_stdout.close()
        api_stderr.close()
    if outcome.get("failures", {}).get("api_popen_identity"):
        raise AssertionError("api_popen_identity_or_stop_not_verified")


def _run_new_job(base_url: str, fixture: Mapping[str, Any], case_dir: Path, provider: "_LoopbackProvider", root: Path, ledger_path: Path) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=False)
    provider_sequence_start = provider.last_sequence()
    request_value = _job_request(fixture)
    created = _request_json(base_url, "POST", "/v1/annual-analysis-jobs", request_value, case_dir / "api-create")
    if created["status"] != 202:
        raise AssertionError(f"job_create_status:{created['status']}")
    created_value = created["json"]
    return _wait_for_job(base_url, str(created_value["job_id"]), case_dir, fixture, provider, root, ledger_path, created=created_value, provider_sequence_start=provider_sequence_start)


def _resume_job(base_url: str, parent: Mapping[str, Any], case_dir: Path, provider: "_LoopbackProvider", root: Path, ledger_path: Path) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=False)
    provider_sequence_start = provider.last_sequence()
    api_parent_job_id = str(parent["job_id"])
    body = {"parent_run_id": parent["run_id"]}
    created = _request_json(base_url, "POST", f"/v1/annual-analysis-jobs/{api_parent_job_id}/resume", body, case_dir / "api-resume")
    if created["status"] != 202:
        raise AssertionError(f"resume_create_status:{created['status']}:{created.get('json')}")
    value = created["json"]
    fixture = parent["fixture"]
    return _wait_for_job(base_url, str(value["job_id"]), case_dir, fixture, provider, root, ledger_path, created=value, parent=parent, provider_sequence_start=provider_sequence_start)


def _complete_issue_resume_chain(
    base_url: str, initial: dict[str, Any], case_prefix: Path,
    provider: "_LoopbackProvider", root: Path, ledger_path: Path,
    cases: dict[str, Any], case_name: str, *,
    ancestor_hashes: Mapping[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Finish the full member plan using bounded, independently audited API children."""
    report = initial["strict_reader"]["report"]
    pipeline = report["model_issue_pipeline"]
    shards = pipeline["shards"]
    grouping_count = sum(item.get("kind") == "grouping" for item in shards)
    pending_shard_count = sum(item.get("status") != "accepted" for item in shards)
    pending_member_count = sum(item.get("disposition") == "pending" for item in pipeline["member_decisions"])
    pending_role_count = pipeline["retrieval_coverage"]["role_assignments_pending"]
    # One round per existing shard/group/pending item is conservative even
    # when global and supplement have not yet been materialized. The final
    # three units cover global, supplement and review; no budget is enlarged.
    max_rounds = grouping_count + len(shards) + pending_shard_count + pending_member_count + pending_role_count + 3
    archived_hashes = dict(ancestor_hashes or {})
    archived_hashes[initial["run_id"]] = _hash_run_and_report(root, initial["run_id"])
    chain: dict[str, Any] = {
        "initial": {"run_id": initial["run_id"], "job_id": initial["job_id"], "case_directory": initial["case_directory"]},
        "round_bound_basis": {
            "grouping_count": grouping_count, "shard_count": len(shards),
            "pending_shard_count": pending_shard_count, "pending_member_count": pending_member_count,
            "pending_role_count": pending_role_count, "unmaterialized_stage_allowance": 3,
        },
        "max_resume_rounds": max_rounds,
        "children": [],
        "all_provider_http_events": list(initial["provider_http_events"]),
        "current_run_id": initial["run_id"],
    }
    initial["resume_chain"] = chain
    current = initial
    run_ids = {initial["run_id"]}
    job_ids = {initial["job_id"]}
    for round_index in range(1, max_rounds + 1):
        current_report = current["strict_reader"]["report"]
        if current_report["model_issue_pipeline"].get("status") == "completed" and current_report.get("model_review", {}).get("status") == "completed":
            break
        if current["terminal_job"].get("recovery_capability", {}).get("can_resume") is not True:
            raise AssertionError("pending_issue_chain_not_legally_resumable")
        before = {run_id: _hash_run_and_report(root, run_id) for run_id in archived_hashes}
        if before != archived_hashes:
            raise AssertionError("issue_chain_ancestor_changed_before_resume")
        child = _resume_job(
            base_url, current, case_prefix.with_name(f"{case_prefix.name}-{round_index:03d}"),
            provider, root, ledger_path,
        )
        cases[f"{case_name}_{round_index:03d}"] = child
        chain["children"].append(child)
        chain["all_provider_http_events"].extend(child["provider_http_events"])
        chain["current_run_id"] = child["run_id"]
        if child["run_id"] in run_ids or child["job_id"] in job_ids:
            raise AssertionError("issue_chain_repeated_run_or_job_identity")
        run_ids.add(child["run_id"])
        job_ids.add(child["job_id"])
        if (
            child["parent_run_id"] != current["run_id"]
            or child["parent"] != {"run_id": current["run_id"], "job_id": current["job_id"]}
            or child["terminal_job"].get("recovery", {}).get("parent_run_id") != current["run_id"]
            or child["strict_reader"].get("loaded") is not True
            or child["strict_reader"].get("snapshot", {}).get("parent_run_id") != current["run_id"]
            or child["document_id"] != initial["document_id"]
            or child["source_pdf_sha256"] != initial["source_pdf_sha256"]
            or child["source_manifest"] != initial["source_manifest"]
        ):
            raise AssertionError("issue_chain_exact_source_or_parent_identity_changed")
        budget = child["terminal_job"]["analysis_budget"]
        rows = child["sqlite_read_only"]["attempt_rows"]
        issue_rows = [item for item in rows if item.get("stage") == "annual_issue_grouping"]
        issue_events = [item for item in child["provider_http_events"] if item.get("stage") in {"grouping", "global", "supplement"}]
        if (
            budget["scope"] != "resume"
            or budget["provider_attempt_limits"]["task_effective"] != 20
            or budget["stages"]["annual_issue_grouping"]["attempt_limit"] != 2
            or len(rows) > 20
            or len(issue_rows) != len(issue_events)
            or len(issue_rows) > 2
        ):
            raise AssertionError("issue_chain_resume_exceeded_production_admission_contract")
        if issue_rows:
            _assert_terminal_issue_gate_closed(child, issue_rows)
        child_report = child["strict_reader"]["report"]
        child_pipeline = child_report["model_issue_pipeline"]
        if (
            child_pipeline["execution_binding"] != pipeline["execution_binding"]
            or child_report["model_discovery"]["execution_contract"]["binding"] != report["model_discovery"]["execution_contract"]["binding"]
            or child_report["model_discovery"]["findings"] != report["model_discovery"]["findings"]
            or child_pipeline["candidates"] != pipeline["candidates"]
            or child_pipeline["input"] != pipeline["input"]
        ):
            raise AssertionError("issue_chain_changed_original_members_quotes_or_execution_binding")
        input_ids = child_pipeline["input"]["finding_ids"]
        decisions = child_pipeline["member_decisions"]
        decision_ids = [item.get("finding_id") for item in decisions]
        if len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != set(input_ids):
            raise AssertionError("issue_chain_dropped_or_duplicated_member_decision")
        grouped_ids = [
            finding_id for shard in child_pipeline["shards"] if shard.get("kind") == "grouping"
            for finding_id in shard["input_finding_ids"]
        ]
        if len(grouped_ids) != len(set(grouped_ids)) or set(grouped_ids) != set(input_ids):
            raise AssertionError("issue_chain_group_plan_dropped_or_duplicated_member")
        candidate_by_id = {item["finding_id"]: item for item in child_pipeline["candidates"]}
        for shard in child_pipeline["shards"]:
            if shard.get("kind") != "grouping":
                continue
            if not 1 <= len(shard["input_finding_ids"]) <= 3 or shard["input"]["candidate_pool"] != [
                candidate_by_id[finding_id] for finding_id in sorted(shard["input_finding_ids"])
            ]:
                raise AssertionError("issue_chain_group_shard_changed_raw_members_or_exceeded_three")
        after = {run_id: _hash_run_and_report(root, run_id) for run_id in archived_hashes}
        hash_check = {"before": before, "after": after, "unchanged": before == after}
        child["ancestor_archive_hash_check"] = hash_check
        _write_json_new(Path(child["case_directory"]) / "ancestor-archive-hash-check.json", hash_check)
        _write_json_new(Path(initial["case_directory"]) / f"resume-chain-round-{round_index:03d}.json", chain)
        if before != after:
            raise AssertionError("issue_chain_resume_mutated_ancestor_archive_bytes")
        archived_hashes[child["run_id"]] = _hash_run_and_report(root, child["run_id"])
        current = child
    _assert_completed_issue_stages(current)
    chain["current_final_result"] = current if current is not initial else {
        "run_id": current["run_id"], "strict_reader": current["strict_reader"],
        "terminal_job": current["terminal_job"], "case_directory": current["case_directory"],
    }
    chain["stage_http_counts"] = dict(Counter(item["stage"] for item in chain["all_provider_http_events"]))
    chain["http_ledger_admission_count"] = sum(
        len(run["sqlite_read_only"]["attempt_rows"]) for run in [initial, *chain["children"]]
    )
    if chain["http_ledger_admission_count"] != len(chain["all_provider_http_events"]):
        raise AssertionError("issue_chain_aggregate_http_not_equal_to_ledger_admissions")
    _write_json_new(Path(initial["case_directory"]) / "resume-chain.json", chain)
    return current


def _run_cancel_at_stage(base_url: str, fixture: Mapping[str, Any], case_dir: Path, provider: "_LoopbackProvider", root: Path, ledger_path: Path, stage: str) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=False)
    provider_sequence_start = provider.last_sequence()
    created = _request_json(base_url, "POST", "/v1/annual-analysis-jobs", _job_request(fixture), case_dir / "api-create")
    job_id = str(created["json"]["job_id"])
    _wait_job_stage_running(base_url, job_id, case_dir, timeout=20)
    event = provider.wait_blocked(fixture["document_id"], stage, timeout=50)
    cancel_start = time.monotonic()
    cancelled = _request_json(base_url, "POST", f"/v1/annual-analysis-jobs/{job_id}/cancel", {}, case_dir / "api-cancel")
    terminal = _wait_for_job_terminal(base_url, job_id, case_dir, timeout=25)
    report_before_late = _strict_read_job(root, terminal, case_dir, ledger_path)
    hash_before = _hash_run_and_report(root, terminal["run_id"])
    provider.release_event(event["sequence"])
    late = provider.wait_response_finished(int(event["sequence"]), timeout=15)
    hash_after = _hash_run_and_report(root, terminal["run_id"])
    ledger = _capture_job_ledger(ledger_path, str(terminal.get("run_id", "")), case_dir / "ledger-after")
    result = _case_result(fixture, terminal, report_before_late, provider, job_id, case_dir, ledger, created=created["json"], cancel=cancelled["json"], provider_sequence_start=provider_sequence_start)
    result["cancel_boundary"] = {"stage": stage, "blocked_request": event, "cancel_started_monotonic": cancel_start, "late_response": late, "parent_archive_unchanged_after_late_response": hash_before == hash_after}
    if terminal.get("status") != "cancelled" or not result["cancel_boundary"]["parent_archive_unchanged_after_late_response"]:
        raise AssertionError(f"cancel_or_late_response_seal_failed:{stage}")
    if any(row.get("stage") in {"global", "supplement", "review"} for row in provider.events_for(fixture["document_id"])):
        raise AssertionError(f"provider_dispatched_after_group_cancel:{stage}")
    return result


def _run_delayed_phase_cutoff(base_url: str, fixture: Mapping[str, Any], case_dir: Path, provider: "_LoopbackProvider", root: Path, ledger_path: Path, stage: str) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=False)
    provider_sequence_start = provider.last_sequence()
    # A fast worker may reach global before POST returns its owner budget.
    # Hold only that response's configuration lookup until the budget is known.
    delay_ready = threading.Event()
    provider.configure(fixture["document_id"], {"mode": "delay_global_many", "delay_ready": delay_ready})
    created = _request_json(base_url, "POST", "/v1/annual-analysis-jobs", _job_request(fixture), case_dir / "api-create")
    job_id = str(created["json"]["job_id"])
    # Read the actual API owner's runtime-fit budget, not the policy defaults:
    # global reconciliation shares the annual_issue_grouping phase deadline.
    owner_budget = created["json"]["analysis_budget"]
    phase_limit_seconds = float(owner_budget["stages"]["annual_issue_grouping"]["time_limit_seconds"])
    owner_limit_seconds = float(owner_budget["main_report_limit_seconds"])
    delay_seconds = phase_limit_seconds + 15.0
    fault_configuration = {
        "mode": "delay_global_many",
        "delay_seconds": delay_seconds,
        "budget_schema_version": owner_budget["schema_version"],
        "budget_stage": "annual_issue_grouping",
        "phase_time_limit_seconds": phase_limit_seconds,
        "main_report_limit_seconds": owner_limit_seconds,
        "late_response_margin_seconds": 15.0,
    }
    provider.configure(fixture["document_id"], fault_configuration)
    _write_json_new(case_dir / "deadline-fault-configuration.json", fault_configuration)
    delay_ready.set()
    started = _wait_job_stage_running(base_url, job_id, case_dir, timeout=20)
    event = provider.wait_received(fixture["document_id"], stage, timeout=owner_limit_seconds + 15.0)
    terminal = _wait_for_job_terminal(base_url, job_id, case_dir, timeout=owner_limit_seconds + 15.0)
    report_before_late = _strict_read_job(root, terminal, case_dir, ledger_path)
    hash_before = _hash_run_and_report(root, terminal["run_id"])
    archive_seal_observed_monotonic = time.monotonic()
    late = provider.wait_response_finished(int(event["sequence"]), timeout=delay_seconds + 15.0)
    hash_after = _hash_run_and_report(root, terminal["run_id"])
    ledger = _capture_job_ledger(ledger_path, str(terminal.get("run_id", "")), case_dir / "ledger-after")
    result = _case_result(fixture, terminal, report_before_late, provider, job_id, case_dir, ledger, created=created["json"], provider_sequence_start=provider_sequence_start)
    result["deadline_boundary"] = {
        "stage": stage,
        "job_running_state": started,
        "fault_configuration": fault_configuration,
        "blocked_phase_http_request": event,
        # The shared phase began before global HTTP admission. Even this
        # conservative latest possible cutoff must precede the late response.
        "latest_possible_phase_cutoff_monotonic": float(event["request_monotonic"]) + phase_limit_seconds,
        "late_response": late,
        "actual_http_delay_seconds": float(late["response_finished_monotonic"]) - float(event["request_monotonic"]),
        "terminal_observed_monotonic": terminal["test_harness_terminal_observed_monotonic"],
        "parent_archive_unchanged_after_late_response": hash_before == hash_after,
        "archive_seal_observed_monotonic": archive_seal_observed_monotonic,
    }
    _write_json_new(case_dir / "deadline-boundary.json", result["deadline_boundary"])
    if float(late["response_finished_monotonic"]) <= max(
        result["deadline_boundary"]["latest_possible_phase_cutoff_monotonic"],
        float(terminal["test_harness_terminal_observed_monotonic"]),
        archive_seal_observed_monotonic,
    ):
        raise AssertionError("injected_global_response_did_not_cross_phase_cutoff_and_terminal_seal")
    if not result["deadline_boundary"]["parent_archive_unchanged_after_late_response"]:
        raise AssertionError("late_phase_response_changed_sealed_archive")
    if terminal.get("status") not in {"completed_with_issues", "failed", "interrupted", "cancelled"}:
        raise AssertionError(f"phase_cutoff_missing_terminal_state:{terminal.get('status')}")
    if any(row.get("stage") == "supplement" for row in provider.events_for(fixture["document_id"])):
        raise AssertionError("provider_dispatched_after_global_phase_cutoff")
    return result


def _run_fault_job(base_url: str, fixture: Mapping[str, Any], case_dir: Path, provider: "_LoopbackProvider", root: Path, ledger_path: Path) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=False)
    provider_sequence_start = provider.last_sequence()
    created = _request_json(base_url, "POST", "/v1/annual-analysis-jobs", _job_request(fixture), case_dir / "api-create")
    terminal = _wait_for_job_terminal(base_url, str(created["json"]["job_id"]), case_dir, timeout=45)
    strict = _strict_read_job(root, terminal, case_dir, ledger_path)
    ledger = _capture_job_ledger(ledger_path, str(terminal.get("run_id", "")), case_dir / "ledger-after")
    return _case_result(fixture, terminal, strict, provider, str(terminal["job_id"]), case_dir, ledger, created=created["json"], provider_sequence_start=provider_sequence_start)


def _run_owner_hard_exit(base_url: str, fixture: Mapping[str, Any], case_dir: Path, provider: "_LoopbackProvider", root: Path, ledger_path: Path, api_process: subprocess.Popen[bytes] | None, api_runtime_identity: Mapping[str, Any], popen_events: Path) -> dict[str, Any]:
    if api_process is None or api_process.poll() is not None:
        raise AssertionError("owner_hard_exit_api_popen_not_running")
    case_dir.mkdir(parents=True, exist_ok=False)
    provider_sequence_start = provider.last_sequence()
    created = _request_json(base_url, "POST", "/v1/annual-analysis-jobs", _job_request(fixture), case_dir / "api-create")
    job_id = str(created["json"]["job_id"])
    event = provider.wait_blocked(fixture["document_id"], "grouping", timeout=50)
    running = _wait_for_job_run_id(base_url, job_id, case_dir, timeout=20)
    run_id = str(running["run_id"])
    current_popen_rows = _read_jsonl(popen_events)
    cli_rows = [item for item in current_popen_rows if item.get("event") == "popen_returned" and item.get("role") == "api_owned_cli" and item.get("document_id") == fixture["document_id"]]
    if len(cli_rows) != 1:
        raise AssertionError("owner_hard_exit_actual_cli_popen_identity_missing")
    cli_popen = cli_rows[0]
    cli_native_identity = cli_popen.get("owned_handle_identity")
    cli_parent_creation_identity = {
        "process_id": cli_popen.get("pid"),
        "creation_filetime": cli_native_identity.get("creation_filetime") if isinstance(cli_native_identity, Mapping) else None,
    }
    exited_worker_keys = {
        _exit_process_identity_key(item)
        for item in current_popen_rows
        if item.get("event") == "popen_exit_observed"
    }
    worker_rows = [
        item for item in current_popen_rows
        if item.get("event") == "popen_returned"
        and item.get("role") == "cli_owned_worker"
        and item.get("parent_role") == "cli"
        and item.get("parent_pid") == cli_popen.get("pid")
        and item.get("parent_process_popen_object_identity") == cli_popen.get("popen_object_identity")
        and item.get("parent_process_creation_identity") == cli_parent_creation_identity
        and _popen_process_identity_key(item) not in exited_worker_keys
    ]
    if not worker_rows:
        raise AssertionError("owner_hard_exit_actual_worker_popen_identity_missing")
    process_manifest = _source_manifest(root)
    if _ACTUAL_CLI_IDENTITY_CAPTURE_DIR is None:
        raise AssertionError("owner_hard_exit_actual_cli_identity_capture_directory_missing")
    cli_capture_path, cli_capture = _actual_cli_identity_capture(_ACTUAL_CLI_IDENTITY_CAPTURE_DIR, cli_popen)
    actual_cli_identity = cli_capture.get("runtime_identity")
    _assert_runtime_identity(actual_cli_identity, "cli", process_manifest)
    if (
        cli_capture.get("process_id") != cli_popen.get("pid")
        or cli_capture.get("parent_pid") != cli_popen.get("parent_pid")
        or cli_capture.get("parent_popen_object_identity") != cli_popen.get("popen_object_identity")
        or cli_capture.get("cwd") != cli_popen.get("cwd")
        or actual_cli_identity.get("process_id") != cli_popen.get("pid")
        or cli_capture.get("argv") != cli_popen.get("argv", [])[1:]
    ):
        raise AssertionError("owner_hard_exit_actual_cli_profile_popen_identity_mismatch")
    if (
        event.get("wire_envelope_schema_version") != "annual_issue_input_compact_v1"
        or event.get("wire_decoder") != "snapshot.decode_annual_issue_input_wire"
    ):
        raise AssertionError("owner_hard_exit_grouping_request_did_not_use_frozen_common_compact_wire_decoder")

    owner_kill_started = time.monotonic()
    api_native_identity = _native_popen_identity(api_process)
    api_start_record_path = case_dir.parent.parent / "api-process-start.json"
    api_start_record = _read_json(api_start_record_path)
    if (
        api_runtime_identity.get("api", {}).get("process_id") != api_process.pid
        or api_native_identity is None
        or api_start_record.get("owned_handle_identity") != api_native_identity
        or api_start_record.get("popen_object_identity") != hex(id(api_process))
        or api_start_record.get("parent_pid") != os.getpid()
    ):
        raise AssertionError("owner_hard_exit_harness_api_popen_identity_not_verified")
    api_process.kill()
    api_returncode = api_process.wait(timeout=12)
    api_native_identity_after_wait = _native_popen_identity(api_process)
    if api_native_identity_after_wait != api_native_identity:
        raise AssertionError("owner_hard_exit_api_owned_handle_identity_changed_after_wait")
    api_kill_observed = time.monotonic()

    requested_stops = [cli_popen, *worker_rows]
    external_stops: list[dict[str, Any]] = []
    for item in requested_stops:
        try:
            external_stops.append(_stop_observed_popen_process(item, timeout=8))
        except Exception as exc:
            external_stops.append({
                "popen_object_identity": item.get("popen_object_identity"),
                "role": item.get("role"),
                "parent_role": item.get("parent_role"),
                "pid": item.get("pid"),
                "parent_pid": item.get("parent_pid"),
                "argv": item.get("argv"),
                "cwd": item.get("cwd"),
                "parent_process_popen_object_identity": item.get("parent_process_popen_object_identity"),
                "parent_process_creation_identity": item.get("parent_process_creation_identity"),
                "expected_owned_handle_identity": item.get("owned_handle_identity"),
                "identity_verified": False,
                "stop_observed": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            })
    stable_hashes_before_late = _hash_owner_state(root, job_id, run_id)
    provider.release_event(int(event["sequence"]))
    late_response = provider.wait_response_finished(int(event["sequence"]), timeout=15)
    time.sleep(0.25)
    events_after = [item for item in provider.events_for(fixture["document_id"]) if int(item.get("sequence", 0)) > int(event["sequence"])]
    stable_hashes_after_late = _hash_owner_state(root, job_id, run_id)
    ledger = _capture_job_ledger(ledger_path, run_id, case_dir / "ledger-after-owner-exit")
    strict_result: dict[str, Any] | None = None
    strict_error: dict[str, str] | None = None
    try:
        strict_result = _strict_read_job(root, {"run_id": run_id, "job_id": job_id}, case_dir, ledger_path)
    except Exception as exc:
        strict_error = {"type": type(exc).__name__, "message": str(exc)}
    if strict_error is not None:
        # Preserve the exact strict-reader failure in the case evidence and
        # report it as an acceptance failure rather than substituting helpers.
        _write_json_new(case_dir / "owner-hard-exit-strict-reader-error.json", strict_error)
    result = {
        "document_id": fixture["document_id"],
        "fixture": {key: (str(value) if isinstance(value, Path) else value) for key, value in fixture.items()},
        "source_pdf_sha256": fixture["sha256"],
        "job_id": job_id,
        "run_id": run_id,
        "parent_run_id": None,
        "api_created_job": created["json"],
        "terminal_job": {**running, "status": "owner_process_hard_exited", "terminal_state_observed": False},
        "job_owner_monotonic": {
            "api_internal_start": running.get("_running_started_monotonic"),
            "phase_http_request_start": event.get("request_monotonic"),
            "owner_hard_kill_started": owner_kill_started,
            "owner_hard_kill_observed": api_kill_observed,
        },
        "phase_http_monotonic": _phase_http_boundaries([event]),
        "source_manifest": process_manifest,
        "runtime_loaded_identity": {
            "api": api_runtime_identity.get("api"),
            "cli_startup_probe": api_runtime_identity.get("cli_at_startup"),
            "cli_actual_process_identity": actual_cli_identity,
            "strict_reader_process_identity": strict_result.get("runtime_identity") if isinstance(strict_result, Mapping) else None,
        },
        "actual_cli_runtime_capture": {"capture_path": str(cli_capture_path), "capture_sha256": _sha256(cli_capture_path), "profile_capture": cli_capture},
        "cli_popen_owned_identity": [{"popen_returned": cli_popen, "external_verified_stop": external_stops[0]}],
        "worker_popen_owned_identity": [{"popen_returned": row, "external_verified_stop": stop} for row, stop in zip(worker_rows, external_stops[1:])],
        "manifest": _read_json(root / "artifacts" / "runs" / run_id / "manifest.json") if (root / "artifacts" / "runs" / run_id / "manifest.json").is_file() else None,
        "strict_reader": ({"loaded": strict_result.get("loaded"), "snapshot": strict_result.get("snapshot"), "report": strict_result.get("report"), "manifest_status": strict_result.get("manifest_summary", {}).get("status")} if isinstance(strict_result, Mapping) else {"loaded": False, "error": strict_error}),
        "provider_http_events": [item for item in provider.events_for(fixture["document_id"]) if int(item.get("sequence", 0)) > provider_sequence_start],
        "sqlite_read_only": ledger,
        "goal_ledger_goal_attempt_count": ledger.get("goal_attempt_count"),
        "artifact_hashes_before_late_response": stable_hashes_before_late,
        "artifact_hashes_after_late_response": stable_hashes_after_late,
        "popen_events_file": str(popen_events),
        "case_directory": str(case_dir),
        "error_category": "owner_hard_exit",
        "owner_hard_exit": {
            "api_process_id": api_process.pid,
            "api_process_returncode": api_returncode,
            "api_popen_object_identity": hex(id(api_process)),
            "api_native_owned_handle_identity": api_native_identity,
            "api_native_owned_handle_identity_after_wait": api_native_identity_after_wait,
            "api_runtime_process_id": api_runtime_identity.get("api", {}).get("process_id"),
            "api_hard_exit_observed": api_process.poll() is not None,
            "blocked_grouping_http_event": event,
            "late_http_response": late_response,
            "no_additional_provider_request_after_owner_exit": not events_after,
            "artifact_state_unchanged_after_late_response": stable_hashes_before_late == stable_hashes_after_late,
            "external_verified_child_stops": external_stops,
        },
    }
    _write_json_new(case_dir / "owner-hard-exit-boundary.json", result["owner_hard_exit"])
    _write_json_new(case_dir / "owner-hard-exit-case-result.json", result)
    if not all(item.get("identity_verified") is True and item.get("stop_observed") is True for item in external_stops):
        raise AssertionError("owner_hard_exit_could_not_stop_exact_api_owned_child_processes")
    if strict_error is not None:
        raise AssertionError(f"owner_hard_exit_partial_archive_not_strictly_readable:{strict_error['type']}:{strict_error['message']}")
    if not result["owner_hard_exit"]["artifact_state_unchanged_after_late_response"] or events_after:
        raise AssertionError("owner_hard_exit_late_response_mutated_archive_or_dispatched_again")
    return result


def _wait_for_job(base_url: str, job_id: str, case_dir: Path, fixture: Mapping[str, Any], provider: "_LoopbackProvider", root: Path, ledger_path: Path, *, created: Mapping[str, Any], parent: Mapping[str, Any] | None = None, provider_sequence_start: int) -> dict[str, Any]:
    terminal = _wait_for_job_terminal(base_url, job_id, case_dir, timeout=65)
    strict = _strict_read_job(root, terminal, case_dir, ledger_path)
    ledger = _capture_job_ledger(ledger_path, str(terminal.get("run_id", "")), case_dir / "ledger-after")
    result = _case_result(fixture, terminal, strict, provider, job_id, case_dir, ledger, created=created, parent=parent, provider_sequence_start=provider_sequence_start)
    return result


def _wait_for_job_terminal(base_url: str, job_id: str, case_dir: Path, *, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + timeout
    transitions: list[dict[str, Any]] = []
    terminal_states = {"completed", "completed_with_issues", "failed", "interrupted", "cancelled"}
    while time.monotonic() < deadline:
        poll_index = next(_API_JOB_POLL_INDEX)
        response = _request_json(base_url, "GET", f"/v1/annual-analysis-jobs/{job_id}", None, case_dir / f"api-job-{poll_index:05d}")
        state = response["json"]
        if not transitions or state.get("status") != transitions[-1].get("status") or state.get("stage") != transitions[-1].get("stage"):
            transitions.append({"monotonic": time.monotonic(), "status": state.get("status"), "stage": state.get("stage"), "job": state})
        if state.get("status") in terminal_states:
            state["test_harness_terminal_observed_monotonic"] = time.monotonic()
            state["test_harness_job_owner_elapsed_monotonic"] = max(0.0, state["test_harness_terminal_observed_monotonic"] - started)
            _write_json_new(case_dir / "job-transitions.json", {"poll_started_monotonic": started, "poll_finished_monotonic": state["test_harness_terminal_observed_monotonic"], "transitions": transitions})
            return state
        time.sleep(0.075)
    raise TimeoutError(f"annual_api_job_timeout:{job_id}")


def _wait_job_stage_running(base_url: str, job_id: str, case_dir: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _request_json(base_url, "GET", f"/v1/annual-analysis-jobs/{job_id}", None, case_dir / f"api-stage-{uuid.uuid4().hex}")["json"]
        if value.get("status") == "running":
            return value
        if value.get("status") in {"failed", "interrupted", "cancelled", "completed", "completed_with_issues"}:
            raise AssertionError(f"job_terminal_before_fault_stage:{value.get('status')}:{value.get('stage')}")
        time.sleep(0.05)
    raise TimeoutError("annual_api_job_never_started")


def _wait_for_job_run_id(base_url: str, job_id: str, case_dir: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _request_json(base_url, "GET", f"/v1/annual-analysis-jobs/{job_id}", None, case_dir / f"api-running-{uuid.uuid4().hex}")["json"]
        if value.get("status") == "running" and isinstance(value.get("run_id"), str):
            return value
        if value.get("status") in {"failed", "interrupted", "cancelled", "completed", "completed_with_issues"}:
            raise AssertionError(f"owner_hard_exit_job_terminal_before_blocked_stage:{value.get('status')}:{value.get('stage')}")
        time.sleep(0.05)
    raise TimeoutError("owner_hard_exit_job_run_id_not_observed")


def _case_result(fixture: Mapping[str, Any], terminal: Mapping[str, Any], strict: Mapping[str, Any], provider: "_LoopbackProvider", job_id: str, case_dir: Path, ledger: Mapping[str, Any], *, created: Mapping[str, Any], parent: Mapping[str, Any] | None = None, cancel: Mapping[str, Any] | None = None, provider_sequence_start: int) -> dict[str, Any]:
    run_id = terminal.get("run_id")
    if not isinstance(run_id, str):
        raise AssertionError("terminal_job_missing_run_id")
    recovery = terminal.get("recovery")
    parent_run_id = recovery.get("parent_run_id") if isinstance(recovery, Mapping) else None
    events = [item for item in provider.events_for(str(fixture["document_id"])) if int(item["sequence"]) > provider_sequence_start]
    manifest = _read_json(Path(strict["run_directory"]) / "manifest.json")
    cli_events = []
    for item in _read_jsonl(Path(strict["popen_events_file"])):
        if (
            item.get("event") != "popen_returned"
            or item.get("role") != "api_owned_cli"
            or item.get("document_id") != fixture["document_id"]
        ):
            continue
        argv = item.get("argv")
        if not isinstance(argv, list) or not all(isinstance(token, str) for token in argv):
            raise AssertionError("case_cli_popen_argv_invalid_for_run_job_binding")

        def _exactly_one_option_value(option: str) -> str:
            values: list[str] = []
            index = 0
            while index < len(argv):
                token = argv[index]
                if token.startswith(f"{option}="):
                    raise AssertionError(f"case_cli_popen_inline_option_rejected:{option}")
                if token == option:
                    value_index = index + 1
                    if value_index >= len(argv) or not argv[value_index] or argv[value_index].startswith("--"):
                        raise AssertionError(f"case_cli_popen_option_value_missing:{option}")
                    values.append(argv[value_index])
                    index += 2
                    continue
                index += 1
            if len(values) != 1:
                raise AssertionError(f"case_cli_popen_option_not_unique:{option}")
            return values[0]

        event_run_id = _exactly_one_option_value("--run-id")
        event_job_id = _exactly_one_option_value("--discovery-progress-job-id")
        if event_run_id == run_id and event_job_id == job_id:
            cli_events.append(item)
    job_state_path = Path(strict["job_directory"]) / "job.json"
    disk_state = _read_json(job_state_path)
    source_identity = _source_manifest(Path(strict["project_root"]))
    if _API_RUNTIME_IDENTITY is None:
        raise AssertionError("actual_api_runtime_identity_was_not_recorded")
    _assert_runtime_identity(_API_RUNTIME_IDENTITY.get("api"), "api", source_identity)
    exit_events = [
        item for item in _read_jsonl(Path(strict["popen_events_file"]))
        if item.get("event") == "popen_exit_observed"
    ]
    exits_by_identity = _unique_process_identity_index(exit_events, event="popen_exit_observed")
    cli_identity_pairs = []
    for item in cli_events:
        identity_key = _popen_process_identity_key(item)
        exit_event = exits_by_identity.get(identity_key)
        if exit_event is None:
            raise AssertionError("case_cli_process_lacks_owned_popen_identity_and_wait_stop_evidence")
        _assert_popen_exit_pair(item, exit_event)
        cli_identity_pairs.append({"popen_returned": item, "popen_exit_observed": exit_event})
    if not cli_events:
        raise AssertionError("actual_cli_popen_process_identity_missing")
    actual_cli_captures = []
    for item in cli_events:
        if _ACTUAL_CLI_IDENTITY_CAPTURE_DIR is None:
            raise AssertionError("actual_cli_identity_capture_directory_missing")
        identity_path, captured = _actual_cli_identity_capture(_ACTUAL_CLI_IDENTITY_CAPTURE_DIR, item)
        runtime_identity = captured.get("runtime_identity")
        _assert_runtime_identity(runtime_identity, "cli", source_identity)
        argv_from_profile = captured.get("argv")
        argv_from_popen = item.get("argv")
        if (
            captured.get("process_id") != item.get("pid")
            or captured.get("parent_pid") != item.get("parent_pid")
            or captured.get("parent_popen_object_identity") != item.get("popen_object_identity")
            or captured.get("cwd") != item.get("cwd")
            or not isinstance(argv_from_profile, list)
            or not isinstance(argv_from_popen, list)
            or argv_from_profile != argv_from_popen[1:]
            or runtime_identity.get("process_id") != item.get("pid")
        ):
            raise AssertionError("actual_cli_capture_pid_argv_does_not_match_owned_popen")
        actual_cli_captures.append({
            "capture_path": str(identity_path),
            "capture_sha256": _sha256(identity_path),
            "profile_capture": captured,
            "popen_returned": item,
        })
    if len(actual_cli_captures) != 1:
        raise AssertionError("case_expected_exactly_one_actual_cli_runtime_capture")
    actual_cli_identity = actual_cli_captures[0]["profile_capture"]["runtime_identity"]
    result = {
        "document_id": fixture["document_id"],
        "fixture": {key: (str(value) if isinstance(value, Path) else value) for key, value in fixture.items()},
        "source_pdf_sha256": fixture["sha256"],
        "job_id": job_id,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "api_created_job": dict(created),
        "cancel_response": dict(cancel) if isinstance(cancel, Mapping) else None,
        "terminal_job": dict(terminal),
        "job_owner_monotonic": {
            "api_internal_start": disk_state.get("_running_started_monotonic"),
            "test_poll_start": _transition_start(case_dir),
            "terminal_observed": terminal.get("test_harness_terminal_observed_monotonic"),
            "owner_elapsed_observed": terminal.get("test_harness_job_owner_elapsed_monotonic"),
        },
        "phase_http_monotonic": _phase_http_boundaries(events),
        "source_manifest": source_identity,
        "runtime_loaded_identity": {
            "api": _API_RUNTIME_IDENTITY.get("api"),
            "cli_startup_probe": _API_RUNTIME_IDENTITY.get("cli_at_startup"),
            "cli_actual_process_identity": actual_cli_identity,
            "strict_reader_process_identity": strict.get("runtime_identity"),
        },
        "actual_cli_runtime_capture": actual_cli_captures,
        "cli_popen_owned_identity": cli_identity_pairs,
        "manifest": {key: manifest.get(key) for key in ("status", "run_id", "source_pdf_sha256", "error_type")},
        "strict_reader": {
            "loaded": strict.get("loaded"),
            "snapshot": strict.get("snapshot"),
            "manifest_status": strict.get("manifest_summary", {}).get("status"),
            "report": strict.get("report"),
        },
        "provider_http_events": events,
        "sqlite_read_only": ledger,
        "goal_ledger_goal_attempt_count": ledger.get("goal_attempt_count"),
        "artifact_hashes": _hash_run_and_report(Path(strict["project_root"]), str(run_id)),
        "popen_events_file": strict["popen_events_file"],
        "case_directory": str(case_dir),
        "parent": ({"run_id": parent.get("run_id"), "job_id": parent.get("job_id")} if parent else None),
        "error_category": terminal.get("error"),
    }
    _capture_api_report(Path(strict["project_root"]), run_id, case_dir)
    _verify_case_http_ledger_and_audits(result, Path(strict["project_root"]))
    issue_rows = [item for item in ledger.get("attempt_rows", []) if item.get("stage") == "annual_issue_grouping"]
    issue_events = [item for item in events if item.get("stage") in {"grouping", "global", "supplement"}]
    if len(issue_rows) != len(issue_events):
        raise AssertionError("case_issue_shared_gate_sqlite_rows_differ_from_actual_http_attempts")
    if issue_rows:
        _assert_terminal_issue_gate_closed(result, issue_rows)
    _write_json_new(case_dir / "case-result.json", result)
    return result


def _verify_case_http_ledger_and_audits(result: dict[str, Any], project_root: Path) -> None:
    """Join captured HTTP bytes to durable audit attempts and ledger admissions."""

    report = result.get("strict_reader", {}).get("report")
    if not isinstance(report, Mapping):
        raise AssertionError("strict_report_missing_for_http_audit_linkage")
    events = result.get("provider_http_events")
    ledger = result.get("sqlite_read_only")
    if not isinstance(events, list) or not isinstance(ledger, Mapping):
        raise AssertionError("http_or_sqlite_evidence_missing")
    run_id = str(result.get("run_id", ""))
    if not run_id:
        raise AssertionError("http_audit_linkage_run_id_missing")

    event_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("peer") != "127.0.0.1" or event.get("auth_is_dummy") is not True:
            raise AssertionError("provider_request_was_not_loopback_dummy_authorized")
        raw = base64.b64decode(str(event.get("request_body_base64", "")), validate=True)
        if (
            hashlib.sha256(raw).hexdigest() != event.get("request_body_sha256")
            or len(raw) != event.get("request_body_bytes")
            or raw.decode("utf-8") != event.get("request_body_utf8")
        ):
            raise AssertionError("captured_provider_request_raw_bytes_do_not_match_utf8_and_sha")
        response = base64.b64decode(str(event.get("response_body_base64", "")), validate=True)
        if (
            hashlib.sha256(response).hexdigest() != event.get("response_body_sha256")
            or response.decode("utf-8") != event.get("response_body_utf8")
            or type(event.get("response_status")) is not int
            or type(event.get("request_monotonic")) not in {int, float}
            or type(event.get("response_finished_monotonic")) not in {int, float}
        ):
            raise AssertionError("captured_provider_response_raw_bytes_do_not_match_utf8_sha_or_timing")
        if event.get("stage") in {"grouping", "global", "supplement"} and (
            event.get("wire_envelope_schema_version") != "annual_issue_input_compact_v1"
            or event.get("wire_decoder") != "snapshot.decode_annual_issue_input_wire"
        ):
            raise AssertionError("actual_issue_request_did_not_use_frozen_common_compact_wire_decoder")
        event_by_key.setdefault((str(event.get("stage")), str(event.get("request_body_sha256"))), []).append(event)
    for rows in event_by_key.values():
        rows.sort(key=lambda item: int(item["sequence"]))

    canonical_candidates: list[tuple[str, Mapping[str, Any]]] = []
    discovery = report.get("model_discovery")
    if isinstance(discovery, Mapping):
        canonical_candidates.extend(("discovery", item) for item in _audit_reference_list(discovery, "audit_artifacts", "discovery"))
    elif discovery is not None:
        raise AssertionError("discovery_audit_owner_is_not_an_object")
    pipeline = report.get("model_issue_pipeline")
    if isinstance(pipeline, Mapping):
        shards = pipeline.get("shards", [])
        if not isinstance(shards, list):
            raise AssertionError("issue_pipeline_shards_are_not_a_list")
        for shard in shards:
            if not isinstance(shard, Mapping):
                raise AssertionError("issue_pipeline_shard_is_not_an_object")
            kind = str(shard.get("kind", ""))
            if kind not in {"grouping", "global", "supplement"}:
                continue
            audit = shard.get("audit")
            if audit is None:
                continue
            if not isinstance(audit, Mapping):
                raise AssertionError("issue_pipeline_shard_audit_is_not_an_object")
            canonical_candidates.append((kind, audit))
    elif pipeline is not None:
        raise AssertionError("issue_pipeline_audit_owner_is_not_an_object")
    review = report.get("model_review")
    if isinstance(review, Mapping):
        canonical_candidates.extend(("review", item) for item in _audit_reference_list(review, "audit_artifacts", "review"))
    elif review is not None:
        raise AssertionError("review_audit_owner_is_not_an_object")

    investigation = report.get("model_investigation")
    if investigation is not None and not isinstance(investigation, Mapping):
        raise AssertionError("investigation_audit_owner_is_not_an_object")
    rollup = _audit_reference_list(investigation, "audit_artifacts", "investigation") if isinstance(investigation, Mapping) else []
    canonical_by_path: dict[str, tuple[str, Mapping[str, Any], dict[str, Any]]] = {}
    for kind, artifact in canonical_candidates:
        normalized = _normalize_audit_reference(project_root, artifact, kind)
        request_path = normalized["request_path"]
        if request_path in canonical_by_path:
            raise AssertionError(f"canonical_audit_request_path_has_ambiguous_stage_origin:{request_path}")
        canonical_by_path[request_path] = (kind, artifact, normalized)

    aliases: list[dict[str, Any]] = []
    independent_investigation: list[tuple[str, Mapping[str, Any]]] = []
    rollup_counts: Counter[str] = Counter()
    for artifact in rollup:
        normalized = _normalize_audit_reference(project_root, artifact, "investigation_rollup")
        request_path = normalized["request_path"]
        rollup_counts[request_path] += 1
        if rollup_counts[request_path] > 1:
            raise AssertionError(f"investigation_rollup_request_path_is_duplicated:{request_path}")
        canonical = canonical_by_path.get(request_path)
        if canonical is not None:
            kind, _canonical_artifact, canonical_normalized = canonical
            if normalized != canonical_normalized:
                raise AssertionError(f"investigation_rollup_alias_metadata_conflicts_with_stage:{request_path}")
            aliases.append({
                "request_path": artifact["request_path"],
                "canonical_request_path": request_path,
                "canonical_stage": kind,
                "metadata_exact_after_supported_path_normalization": True,
                "request_body_sha256": artifact.get("request_body_sha256"),
            })
            continue
        if artifact.get("prompt_version") != "annual_investigation_v1":
            raise AssertionError(f"investigation_rollup_entry_has_no_known_stage_origin:{request_path}")
        binding = _verify_independent_investigation_binding(
            project_root, result, report, investigation, artifact, request_path,
        )
        independent_investigation.append(("investigation", artifact))
        aliases.append({
            "request_path": artifact["request_path"],
            "canonical_request_path": None,
            "canonical_stage": "investigation",
            "metadata_exact_after_supported_path_normalization": True,
            "independent_item_signal_binding": binding,
        })
    missing_rollup_aliases = sorted(set(canonical_by_path) - set(rollup_counts))
    if missing_rollup_aliases:
        raise AssertionError(f"investigation_rollup_missing_canonical_stage_aliases:{len(missing_rollup_aliases)}")
    audit_candidates = [*canonical_candidates, *independent_investigation]

    stage_by_kind = {
        "discovery": "discovery",
        "grouping": "grouping",
        "global": "global",
        "supplement": "supplement",
        "review": "review",
    }
    audit_rows_by_attempt_id: dict[str, dict[str, Any]] = {}
    audited_body_keys: Counter[tuple[str, str]] = Counter()
    audit_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    artifacts_root = (project_root / "artifacts").resolve()
    for kind, artifact in audit_candidates:
        request_rel = artifact.get("request_path")
        if not isinstance(request_rel, str) or not request_rel:
            raise AssertionError(f"audit_request_path_missing:{kind}")
        request_path = _resolve_audit_artifact_path(project_root, request_rel)
        if request_path.as_posix() in seen_paths:
            raise AssertionError(f"audit_request_path_has_multiple_stage_owners:{request_rel}")
        seen_paths.add(request_path.as_posix())
        if not request_path.is_file():
            raise AssertionError(f"referenced_audit_request_missing:{request_rel}")
        request_record = _read_json(request_path)
        model = request_record.get("model")
        messages = request_record.get("messages")
        if not isinstance(model, str) or not isinstance(messages, list):
            raise AssertionError("audit_request_record_model_or_messages_invalid")
        exact_body = json.dumps({"model": model, "messages": messages, "stream": False}, ensure_ascii=False).encode("utf-8")
        body_sha = hashlib.sha256(exact_body).hexdigest()
        expected_stage = stage_by_kind[kind]
        matches = event_by_key.get((expected_stage, body_sha), [])
        declared_hash = artifact.get("request_body_sha256")
        if isinstance(declared_hash, str) and declared_hash != body_sha:
            raise AssertionError("audit_declared_wire_hash_differs_from_request_json_bytes")
        response_path = _resolve_audit_artifact_path(project_root, artifact["response_path"]) if isinstance(artifact.get("response_path"), str) else request_path.parent / "response.json"
        failure_path = _resolve_audit_artifact_path(project_root, artifact["failure_path"]) if isinstance(artifact.get("failure_path"), str) else request_path.parent / "failure.json"
        top_record: Mapping[str, Any] = {}
        if response_path.is_file():
            loaded = _read_json(response_path)
            if isinstance(loaded, Mapping):
                top_record = loaded
        elif failure_path.is_file():
            loaded = _read_json(failure_path)
            if isinstance(loaded, Mapping):
                top_record = loaded
        attempt_records = top_record.get("attempt_records", [])
        if not isinstance(attempt_records, list):
            raise AssertionError("audit_attempt_records_not_a_list")
        admitted = [item for item in attempt_records if isinstance(item, Mapping) and item.get("provider_admitted") is True and isinstance(item.get("admission_id"), str)]
        if not admitted:
            # A process stop can occur after durable request/admission but
            # before the parent audit's final response/failure index is sealed.
            # Keep the actual per-attempt request and SQLite row as evidence;
            # do not invent a provider outcome when its audit file is absent.
            for attempt_dir in sorted(request_path.parent.glob("attempt-*")):
                attempt_request_path = attempt_dir / "request.json"
                if not attempt_request_path.is_file():
                    continue
                attempt_request = _read_json(attempt_request_path)
                audit_id = attempt_request.get("audit_id")
                ledger_row = next((item for item in ledger.get("attempt_rows", []) if item.get("audit_id") == audit_id), None)
                if isinstance(ledger_row, Mapping) and ledger_row.get("run_id") == run_id and isinstance(ledger_row.get("admission_id"), str):
                    admitted.append({
                        "audit_id": audit_id,
                        "admission_id": ledger_row["admission_id"],
                        "provider_admitted": True,
                        "response_accepted": (attempt_dir / "response.json").is_file(),
                        "status": "succeeded" if (attempt_dir / "response.json").is_file() else "outcome_unsealed",
                        "reason": None,
                    })
        if admitted and len(admitted) != len(matches):
            # Reused parent audits are preserved in the child report but are
            # deliberately not present in this child's run-scoped HTTP trace.
            current = [item for item in admitted if any(row.get("audit_id") == item.get("audit_id") for row in ledger.get("attempt_rows", []))]
            if len(current) != len(matches):
                raise AssertionError(f"audit_attempt_count_does_not_match_exact_http_body:{expected_stage}:{len(admitted)}:{len(matches)}")
            admitted = current
        elif not admitted and matches:
            raise AssertionError(f"http_request_has_no_durable_audit_attempt:{expected_stage}:{body_sha}")
        for attempt in admitted:
            audit_id = str(attempt.get("audit_id", ""))
            if not audit_id:
                raise AssertionError("admitted_audit_attempt_missing_id")
            prior = audit_rows_by_attempt_id.get(audit_id)
            row = {
                "audit_id": audit_id,
                "admission_id": attempt.get("admission_id"),
                "provider_admitted": attempt.get("provider_admitted"),
                "status": attempt.get("status"),
                "reason": attempt.get("reason"),
                "kind": kind,
                "stage": expected_stage,
                "request_body_sha256": body_sha,
                "audit_request_path": request_rel,
            }
            if prior is not None and prior != row:
                raise AssertionError("duplicate_audit_attempt_id_mapped_to_conflicting_request")
            audit_rows_by_attempt_id[audit_id] = row
            audited_body_keys[(expected_stage, body_sha)] += 1
        audit_files.append({
            "kind": kind,
            "stage": expected_stage,
            "request_path": request_rel,
            "request_sha256": _sha256(request_path),
            "wire_body_sha256": body_sha,
            "declared_wire_body_sha256": declared_hash,
            "response_path": response_path.relative_to(artifacts_root).as_posix() if response_path.is_file() else None,
            "failure_path": failure_path.relative_to(artifacts_root).as_posix() if failure_path.is_file() else None,
            "provider_admitted_attempt_count": len(admitted),
            "http_match_count": len(matches),
            "attempt_outcomes_sealed": len(admitted) == 0 or all(item.get("status") != "outcome_unsealed" for item in admitted),
        })

    # Some durable refs are intentionally inherited from a parent run. For
    # the current job, every captured network attempt must still match at least
    # one current-run audit attempt and one ledger admission.
    ledger_rows = [item for item in ledger.get("attempt_rows", []) if isinstance(item, Mapping) and item.get("run_id") == run_id]
    by_admission = {str(item.get("admission_id")): item for item in ledger_rows if isinstance(item.get("admission_id"), str)}
    audit_current = {audit_id: item for audit_id, item in audit_rows_by_attempt_id.items() if any(row.get("audit_id") == audit_id for row in ledger_rows)}
    if len(events) != len(ledger_rows):
        raise AssertionError(f"current_run_http_and_sqlite_attempt_counts_differ:{len(events)}:{len(ledger_rows)}")
    for event in events:
        key = (str(event.get("stage")), str(event.get("request_body_sha256")))
        if audited_body_keys[key] <= 0:
            raise AssertionError(f"provider_http_body_has_no_audit_request:{key[0]}:{key[1]}")
    unmatched_audit_ids: list[str] = []
    for audit_id, audit_attempt in audit_current.items():
        admission_id = str(audit_attempt.get("admission_id", ""))
        row = by_admission.get(admission_id)
        if row is None or row.get("audit_id") != audit_id:
            unmatched_audit_ids.append(audit_id)
            continue
        if row.get("stage") not in {
            "annual_discovery", "annual_investigation", "annual_issue_grouping", "annual_review",
        }:
            raise AssertionError("sqlite_admission_has_unexpected_stage")
    if unmatched_audit_ids:
        raise AssertionError("audit_admission_ids_missing_from_current_run_sqlite_rows")

    result["http_audit_ledger_linkage"] = {
        "raw_http_bytes_validated": True,
        "loopback_and_dummy_authorization_validated": True,
        "http_request_count": len(events),
        "sqlite_current_run_admission_count": len(ledger_rows),
        "audit_request_files": audit_files,
        "investigation_audit_rollup": {
            "aggregate_reference_count": len(rollup),
            "canonical_stage_reference_count": len(canonical_candidates),
            "exact_stage_alias_count": sum(1 for item in aliases if item.get("canonical_request_path")),
            "independent_investigation_reference_count": len(independent_investigation),
            "references": aliases,
        },
        "audit_attempt_admission_ids": sorted(audit_current),
        "sqlite_admission_ids": sorted(by_admission),
        "attempt_rows": [audit_current[key] for key in sorted(audit_current)],
        "status": "linked",
    }
    _write_json_new(Path(result["case_directory"]) / "http-audit-ledger-linkage.json", result["http_audit_ledger_linkage"])


def _resolve_audit_artifact_path(project_root: Path, reference: str) -> Path:
    """Resolve either supported audit path form beneath snapshot artifacts."""
    relative = Path(reference)
    parts = relative.parts
    if relative.is_absolute() or not parts or ".." in parts:
        raise AssertionError("audit_artifact_path_escaped_snapshot_artifacts")
    if len(parts) >= 3 and parts[:2] == ("artifacts", "runs"):
        parts = parts[1:]
    elif parts[0] != "runs":
        raise AssertionError("audit_artifact_path_has_invalid_root")

    artifacts_root = (project_root / "artifacts").resolve()
    try:
        resolved = artifacts_root.joinpath(*parts).resolve()
        resolved.relative_to(artifacts_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AssertionError("audit_artifact_path_escaped_snapshot_artifacts") from exc
    return resolved


def _audit_reference_list(owner: Mapping[str, Any], field: str, owner_name: str) -> list[Mapping[str, Any]]:
    value = owner.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise AssertionError(f"{owner_name}_audit_references_are_not_a_list")
    if any(not isinstance(item, Mapping) for item in value):
        raise AssertionError(f"{owner_name}_audit_reference_is_not_an_object")
    return value


def _normalize_audit_reference(
    project_root: Path, artifact: Mapping[str, Any], owner_name: str,
) -> dict[str, Any]:
    request_rel = artifact.get("request_path")
    if not isinstance(request_rel, str) or not request_rel:
        raise AssertionError(f"{owner_name}_audit_request_path_missing")
    normalized: dict[str, Any] = {}
    for key, value in artifact.items():
        if key in {"request_path", "response_path", "failure_path", "audit_dir"} and value is not None:
            if not isinstance(value, str) or not value:
                raise AssertionError(f"{owner_name}_audit_{key}_is_invalid")
            normalized[key] = _resolve_audit_artifact_path(project_root, value).as_posix()
        else:
            normalized[key] = value
    if not isinstance(normalized.get("request_path"), str):
        raise AssertionError(f"{owner_name}_audit_request_path_missing")
    return normalized


def _verify_independent_investigation_binding(
    project_root: Path,
    result: Mapping[str, Any],
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    artifact: Mapping[str, Any],
    normalized_request_path: str,
) -> dict[str, Any]:
    """Require an independent investigation audit to bind to a real result item and its source evidence."""

    if artifact.get("prompt_version") != "annual_investigation_v1":
        raise AssertionError("independent_investigation_prompt_version_missing")
    declared_body_sha = artifact.get("request_body_sha256")
    if not isinstance(declared_body_sha, str) or len(declared_body_sha) != 64:
        raise AssertionError("independent_investigation_declared_body_hash_missing")
    request_path = _resolve_audit_artifact_path(project_root, str(artifact["request_path"]))
    audit_dir_ref = artifact.get("audit_dir")
    response_ref = artifact.get("response_path")
    failure_ref = artifact.get("failure_path")
    if not isinstance(audit_dir_ref, str) or not audit_dir_ref:
        raise AssertionError("independent_investigation_audit_directory_missing")
    audit_dir_path = _resolve_audit_artifact_path(project_root, audit_dir_ref)
    if audit_dir_path != request_path.parent:
        raise AssertionError("independent_investigation_audit_directory_conflicts_with_request")
    if not isinstance(response_ref, str) or not response_ref or failure_ref is not None:
        raise AssertionError("independent_investigation_success_response_reference_missing_or_conflicting")
    response_path = _resolve_audit_artifact_path(project_root, response_ref)
    if response_path.parent != request_path.parent or not response_path.is_file():
        raise AssertionError("independent_investigation_response_reference_is_not_local_to_request")
    if artifact.get("status") != "succeeded":
        raise AssertionError("independent_investigation_audit_status_is_not_succeeded")
    request_record = _read_json(request_path)
    model = request_record.get("model")
    messages = request_record.get("messages")
    if not isinstance(model, str) or not isinstance(messages, list):
        raise AssertionError("independent_investigation_request_model_or_messages_invalid")
    exact_body = json.dumps({"model": model, "messages": messages, "stream": False}, ensure_ascii=False).encode("utf-8")
    actual_body_sha = hashlib.sha256(exact_body).hexdigest()
    if declared_body_sha != actual_body_sha:
        raise AssertionError("independent_investigation_request_body_hash_mismatch")
    user_messages = [
        item.get("content") for item in messages
        if isinstance(item, Mapping) and item.get("role") == "user" and isinstance(item.get("content"), str)
    ]
    if len(user_messages) != 1:
        raise AssertionError("independent_investigation_request_user_payload_is_ambiguous")
    try:
        payload = json.loads(user_messages[0])
    except json.JSONDecodeError as exc:
        raise AssertionError("independent_investigation_request_user_payload_is_not_json") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"document_binding", "candidate", "narrative_evidence"}:
        raise AssertionError("independent_investigation_request_payload_shape_invalid")
    binding = payload.get("document_binding")
    candidate = payload.get("candidate")
    request_evidence = payload.get("narrative_evidence")
    if not isinstance(binding, Mapping) or not isinstance(candidate, Mapping) or not isinstance(request_evidence, list) or not request_evidence:
        raise AssertionError("independent_investigation_request_binding_or_evidence_missing")

    source_fields = ("company_id", "report_year", "report_period", "source_document_id", "source_sha256")
    expected_binding = {key: investigation.get(key) for key in source_fields}
    if any(expected_binding[key] is None for key in source_fields):
        raise AssertionError("independent_investigation_report_source_binding_incomplete")
    if any(binding.get(key) != expected_binding[key] for key in source_fields):
        raise AssertionError("independent_investigation_request_source_binding_differs_from_report")

    relative = request_path.relative_to((project_root / "artifacts").resolve())
    path_parts = relative.parts
    if len(path_parts) < 4 or path_parts[0] != "runs" or path_parts[-1] != "request.json":
        raise AssertionError("independent_investigation_request_path_has_no_run_binding")
    path_run_id = path_parts[1]
    request_run_id = binding.get("run_id")
    if not isinstance(request_run_id, str) or request_run_id != path_run_id:
        raise AssertionError("independent_investigation_request_run_id_differs_from_audit_path")
    allowed_run_ids = {
        value for value in (
            result.get("run_id"), result.get("parent_run_id"), report.get("parent_run_id"),
            investigation.get("parent_run_id"),
        ) if isinstance(value, str) and value
    }
    if request_run_id not in allowed_run_ids:
        raise AssertionError("independent_investigation_request_run_id_is_not_current_or_direct_parent")

    signal_id = candidate.get("signal_id")
    items = investigation.get("items")
    if not isinstance(signal_id, str) or not isinstance(items, list):
        raise AssertionError("independent_investigation_candidate_or_result_items_missing")
    matching_items = [item for item in items if isinstance(item, Mapping) and item.get("signal_id") == signal_id]
    all_item_ids = [item.get("signal_id") for item in items if isinstance(item, Mapping)]
    if len(all_item_ids) != len(items) or len(all_item_ids) != len(set(all_item_ids)) or len(matching_items) != 1:
        raise AssertionError("independent_investigation_candidate_does_not_bind_to_exactly_one_result_item")
    result_item = matching_items[0]
    cited_ids = result_item.get("narrative_evidence_ids")
    if not isinstance(cited_ids, list) or not cited_ids or any(not isinstance(value, str) for value in cited_ids):
        raise AssertionError("independent_investigation_result_item_has_no_evidence_citations")

    evidence_by_id: dict[str, Mapping[str, Any]] = {}
    for item in request_evidence:
        if not isinstance(item, Mapping):
            raise AssertionError("independent_investigation_request_evidence_is_not_an_object")
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or evidence_id in evidence_by_id:
            raise AssertionError("independent_investigation_request_evidence_id_is_missing_or_duplicated")
        if (
            item.get("source_document_id") != expected_binding["source_document_id"]
            or item.get("source_sha256") != expected_binding["source_sha256"]
            or type(item.get("pdf_page")) is not int
            or item.get("pdf_page", 0) < 1
            or not isinstance(item.get("raw_text"), str)
            or not item.get("raw_text")
        ):
            raise AssertionError("independent_investigation_request_evidence_source_binding_invalid")
        evidence_by_id[evidence_id] = item
    cited_set = set(cited_ids)
    request_evidence_ids = set(evidence_by_id)
    if not cited_set.issubset(request_evidence_ids):
        raise AssertionError("independent_investigation_result_cites_evidence_outside_bound_request")
    report_evidence = investigation.get("narrative_evidences")
    if not isinstance(report_evidence, list):
        raise AssertionError("independent_investigation_result_narrative_evidence_missing")
    report_evidence_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for item in report_evidence:
        if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str):
            report_evidence_by_id.setdefault(item["evidence_id"], []).append(item)
    for evidence_id in cited_set:
        request_item = evidence_by_id[evidence_id]
        matches = report_evidence_by_id.get(evidence_id, [])
        if len(matches) != 1:
            raise AssertionError("independent_investigation_cited_evidence_not_unique_in_report")
        report_item = matches[0]
        signal_ids = report_item.get("signal_ids")
        if (
            report_item.get("source_document_id") != request_item.get("source_document_id")
            or report_item.get("source_sha256") != request_item.get("source_sha256")
            or report_item.get("pdf_page") != request_item.get("pdf_page")
            or report_item.get("raw_text") != request_item.get("raw_text")
            or not isinstance(signal_ids, list)
            or signal_id not in signal_ids
        ):
            raise AssertionError("independent_investigation_cited_evidence_does_not_bind_to_item_and_source")

    return {
        "request_path": normalized_request_path,
        "request_run_id": request_run_id,
        "signal_id": signal_id,
        "result_item_status": result_item.get("status"),
        "cited_evidence_ids": sorted(cited_set),
        "source_document_id": expected_binding["source_document_id"],
        "source_sha256": expected_binding["source_sha256"],
        "request_body_sha256": actual_body_sha,
        "bound_to_exactly_one_result_item": True,
    }


def _strict_read_job(root: Path, terminal: Mapping[str, Any], case_dir: Path, ledger_path: Path) -> dict[str, Any]:
    run_id = terminal.get("run_id")
    if not isinstance(run_id, str):
        raise AssertionError("strict_read_run_id_missing")
    driver = case_dir / "strict-reader-driver.py"
    result_path = case_dir / "strict-reader-result.json"
    _write_bytes_new(driver, _STRICT_READER_DRIVER.encode("utf-8"))
    env = _minimal_child_environment(root)
    completed = subprocess.run(
        [sys.executable, "-u", str(driver), str(root), run_id, str(result_path), str(ledger_path), str(terminal.get("job_id", ""))],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=25,
        check=False,
    )
    (case_dir / "strict-reader.stdout.raw").write_bytes(completed.stdout)
    (case_dir / "strict-reader.stderr.raw").write_bytes(completed.stderr)
    if completed.returncode != 0 or not result_path.is_file():
        raise AssertionError(f"snapshot_strict_reader_failed:{completed.returncode}:{completed.stderr[-3000:]!r}")
    value = _read_json(result_path)
    value["project_root"] = str(root)
    value["popen_events_file"] = str(_POPEN_EVENTS_PATH) if _POPEN_EVENTS_PATH is not None else ""
    return value


def _capture_api_report(root: Path, run_id: str, case_dir: Path) -> None:
    manifest = _read_json(root / "artifacts" / "runs" / run_id / "manifest.json")
    _write_json_new(case_dir / "run-manifest.json", manifest)
    _copy_runtime_artifacts(root, {"run_id": run_id}, case_dir / "formal-artifacts")


def _copy_runtime_artifacts(root: Path, value: Mapping[str, Any], target: Path) -> None:
    run_id = str(value["run_id"])
    target.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, list[dict[str, Any]]] = {}
    for kind, source in (
        ("run", root / "artifacts" / "runs" / run_id),
        ("report", root / "artifacts" / "reports" / run_id),
    ):
        if not source.is_dir():
            hashes[kind] = []
            continue
        destination = target / kind / source.name
        destination.parent.mkdir(parents=True, exist_ok=False)
        source_hashes = _tree_file_hashes(source)
        shutil.copytree(source, destination)
        copied_hashes = _tree_file_hashes(destination)
        if source_hashes != copied_hashes:
            raise AssertionError(f"formal_artifact_copy_hash_mismatch:{kind}")
        hashes[kind] = copied_hashes
    _write_json_new(target / "copied-artifact-hashes.json", hashes)


def _assert_completed_issue_stages(result: Mapping[str, Any]) -> None:
    report = result["strict_reader"]["report"]
    pipeline = report.get("model_issue_pipeline")
    if not isinstance(pipeline, Mapping) or pipeline.get("status") != "completed":
        raise AssertionError(f"preflight_issue_pipeline_incomplete:{pipeline.get('reason') if isinstance(pipeline, Mapping) else 'missing'}")
    binding = pipeline.get("execution_binding")
    discovery = report.get("model_discovery")
    contract = discovery.get("execution_contract") if isinstance(discovery, Mapping) else None
    if (
        not isinstance(binding, Mapping)
        or binding.get("kind") != "fintrace_annual_execution_binding_v11"
        or binding.get("execution_version") != "execution_v11"
        or binding.get("grouping_shard_plan_version") != "member_bounded_v2"
        or type(binding.get("grouping_shard_max_members")) is not int
        or binding.get("grouping_shard_max_members") != 3
        or not isinstance(contract, Mapping)
        or contract.get("kind") != "fintrace_annual_discovery_execution_v11"
    ):
        raise AssertionError("preflight_did_not_use_active_v11_member_bounded_identity")
    shard_kinds = {item.get("kind") for item in pipeline.get("shards", []) if isinstance(item, Mapping) and item.get("status") == "accepted"}
    if not {"grouping", "global", "supplement"}.issubset(shard_kinds):
        raise AssertionError(f"preflight_missing_durable_phase:{sorted(shard_kinds)}")
    review = report.get("model_review")
    if not isinstance(review, Mapping) or review.get("status") != "completed" or not _successful_audits(review):
        raise AssertionError("preflight_review_durable_audit_missing")
    for kind in ("grouping", "global", "supplement"):
        if not any(item.get("kind") == kind and item.get("status") == "accepted" and _successful_audits(item) for item in pipeline.get("shards", []) if isinstance(item, Mapping)):
            raise AssertionError(f"preflight_{kind}_durable_audit_missing")
    if result["strict_reader"].get("loaded") is not True:
        raise AssertionError("preflight_strict_reader_not_loaded")
    if result["terminal_job"].get("recovery_capability", {}).get("can_resume") is not False:
        raise AssertionError("completed_full_report_must_not_offer_resume")
    input_ids = pipeline["input"]["finding_ids"]
    closure = pipeline["global_closure"]
    decisions = pipeline["member_decisions"]
    if (
        closure.get("closed") is not True
        or closure.get("pending_finding_ids") != []
        or closure.get("pending_issue_ids") != []
        or sorted(closure.get("retained_finding_ids", [])) != sorted(input_ids)
        or sorted(item["finding_id"] for item in decisions) != sorted(input_ids)
        or any(item.get("disposition") == "pending" for item in decisions)
    ):
        raise AssertionError("completed_issue_chain_lost_or_left_pending_original_member")
    candidate_by_id = {item["finding_id"]: item for item in pipeline["candidates"]}
    issue_members = []
    for issue in pipeline["issues"]:
        issue_members.extend(issue["member_finding_ids"])
        for finding_id in issue["member_finding_ids"]:
            if any(citation not in issue["citations"] for citation in candidate_by_id[finding_id]["citations"]):
                raise AssertionError("completed_issue_chain_dropped_original_member_citation")
    if sorted(issue_members) != sorted(input_ids):
        raise AssertionError("completed_issue_chain_member_issue_assignment_not_exact")


def _assert_preflight_semantic_pressure(
    result: Mapping[str, Any], fixture: Mapping[str, Any], provider: "_LoopbackProvider",
) -> None:
    if fixture.get("content_page_count") != 16 or fixture.get("pdf_page_count") != 17:
        raise AssertionError("preflight_fixture_page_pressure_was_reduced")
    with pymupdf.open(fixture["pdf"]) as document:
        if document.page_count != 17:
            raise AssertionError("preflight_pdf_page_count_does_not_match_fixture_contract")

    report = result["strict_reader"]["report"]
    discovery = report.get("model_discovery")
    pipeline = report.get("model_issue_pipeline")
    findings = discovery.get("findings") if isinstance(discovery, Mapping) else None
    input_value = pipeline.get("input") if isinstance(pipeline, Mapping) else None
    input_ids = input_value.get("finding_ids") if isinstance(input_value, Mapping) else None
    if not isinstance(findings, list) or len(findings) <= 12 or not isinstance(input_ids, list) or len(input_ids) <= 12:
        raise AssertionError("preflight_did_not_reach_more_than_twelve_qualified_findings")
    finding_ids = [item.get("finding_id") for item in findings if isinstance(item, Mapping)]
    if len(finding_ids) != len(findings) or len(finding_ids) != len(set(finding_ids)) or set(finding_ids) != set(input_ids):
        raise AssertionError("preflight_discovery_and_pipeline_finding_sets_differ")

    events = provider.events_for(str(fixture["document_id"]))
    grouping_successes = [item for item in events if item.get("stage") == "grouping" and item.get("response_status") == 200]
    distinct_group_requests = {item.get("request_body_sha256") for item in grouping_successes}
    if len(distinct_group_requests) < 2:
        raise AssertionError("preflight_did_not_reach_two_distinct_semantic_group_shards")
    for stage in ("global", "supplement", "review"):
        if not any(item.get("stage") == stage and item.get("response_status") == 200 for item in events):
            raise AssertionError(f"preflight_did_not_reach_successful_{stage}_request")

    candidates = pipeline.get("candidates") if isinstance(pipeline, Mapping) else None
    issues = pipeline.get("issues") if isinstance(pipeline, Mapping) else None
    decisions = pipeline.get("member_decisions") if isinstance(pipeline, Mapping) else None
    shards = pipeline.get("shards") if isinstance(pipeline, Mapping) else None
    if not isinstance(candidates, list) or not isinstance(issues, list) or not isinstance(decisions, list) or not isinstance(shards, list):
        raise AssertionError("preflight_issue_pipeline_member_evidence_missing")
    candidate_by_id = {
        str(item["finding_id"]): item
        for item in candidates
        if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
    }
    decision_ids = [item.get("finding_id") for item in decisions if isinstance(item, Mapping)]
    if len(decision_ids) != len(decisions) or len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != set(input_ids):
        raise AssertionError("preflight_did_not_preserve_exactly_one_decision_per_finding")

    grouping_shards = [item for item in shards if isinstance(item, Mapping) and item.get("kind") == "grouping" and item.get("status") == "accepted"]
    if len(grouping_shards) < 2:
        raise AssertionError("preflight_did_not_accept_two_semantic_group_shards")
    observed_grouping_merge = False
    for shard in grouping_shards:
        shard_input_ids = shard.get("input_finding_ids")
        if not isinstance(shard_input_ids, list) or not 1 <= len(shard_input_ids) <= 3:
            raise AssertionError("preflight_v10_grouping_exceeded_three_original_members")
        shard_input = shard.get("input")
        if not isinstance(shard_input, Mapping):
            raise AssertionError("preflight_v10_grouping_input_missing")
        raw_candidates = shard_input.get("candidate_pool")
        if not isinstance(raw_candidates, list) or {item.get("finding_id") for item in raw_candidates if isinstance(item, Mapping)} != set(shard_input_ids):
            raise AssertionError("preflight_v10_grouping_dropped_raw_candidates")
        if any(
            not isinstance(group, Mapping)
            or group.get("representative_finding_id") not in group.get("member_finding_ids", [])
            for group in shard_input.get("groups", [])
        ):
            raise AssertionError("preflight_v10_group_representative_not_contained")
        if any(item != candidate_by_id.get(str(item.get("finding_id"))) for item in raw_candidates):
            raise AssertionError("preflight_v10_grouping_changed_member_fields_or_quotes")
        shard_result = shard.get("result")
        shard_decisions = shard_result.get("decisions") if isinstance(shard_result, Mapping) else None
        result_ids = [item.get("finding_id") for item in shard_decisions if isinstance(item, Mapping)] if isinstance(shard_decisions, list) else []
        if not isinstance(shard_input_ids, list) or len(result_ids) != len(shard_decisions or []) or len(result_ids) != len(set(result_ids)) or set(result_ids) != set(shard_input_ids):
            raise AssertionError("preflight_grouping_shard_did_not_decide_every_input_once")
        observed_grouping_merge = observed_grouping_merge or any(item.get("disposition") == "merge" for item in shard_decisions if isinstance(item, Mapping))
    if not observed_grouping_merge:
        raise AssertionError("preflight_grouping_did_not_merge_complete_semantic_duplicates")
    global_shards = [item for item in shards if isinstance(item, Mapping) and item.get("kind") == "global" and item.get("status") == "accepted"]
    if len(global_shards) != 1:
        raise AssertionError("preflight_global_semantic_reconciliation_missing_or_duplicated")
    global_input_ids = global_shards[0].get("input_issue_ids")
    global_result = global_shards[0].get("result")
    global_decisions = global_result.get("decisions") if isinstance(global_result, Mapping) else None
    global_decision_ids = [item.get("issue_id") for item in global_decisions if isinstance(item, Mapping)] if isinstance(global_decisions, list) else []
    if (
        not isinstance(global_input_ids, list)
        or len(global_decision_ids) != len(global_decisions or [])
        or len(global_decision_ids) != len(set(global_decision_ids))
        or set(global_decision_ids) != set(global_input_ids)
        or not any(item.get("disposition") == "merge" for item in global_decisions if isinstance(item, Mapping))
    ):
        raise AssertionError("preflight_global_did_not_decide_and_merge_exact_semantic_groups")
    retrieval = pipeline.get("retrieval_coverage")
    if (
        not isinstance(retrieval, Mapping)
        or retrieval.get("max_role_assignments") != 32
        or retrieval.get("role_assignments_total", 33) > 32
        or retrieval.get("role_assignments_pending") != 0
        or retrieval.get("complete") is not True
    ):
        raise AssertionError("preflight_supplement_roles_exceeded_or_failed_the_existing_32_pair_contract")

    observed_semantic_merge = False
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise AssertionError("preflight_issue_record_invalid")
        member_ids = issue.get("member_finding_ids")
        citations = issue.get("citations")
        if not isinstance(member_ids, list) or not isinstance(citations, list):
            raise AssertionError("preflight_issue_member_citations_missing")
        if len(member_ids) > 1:
            observed_semantic_merge = True
        retained_citations = {
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in citations
            if isinstance(item, Mapping)
        }
        for finding_id in member_ids:
            candidate = candidate_by_id.get(str(finding_id))
            if candidate is None:
                raise AssertionError("preflight_issue_member_candidate_missing")
            for citation in candidate.get("citations", []):
                if not isinstance(citation, Mapping) or json.dumps(citation, ensure_ascii=False, sort_keys=True, separators=(",", ":")) not in retained_citations:
                    raise AssertionError("preflight_semantic_merge_dropped_original_citation")
        issue_keys = {_fixture_semantic_key(candidate_by_id.get(str(member))) for member in member_ids}
        if None in issue_keys or len(issue_keys) != 1:
            raise AssertionError("preflight_issue_combined_distinct_semantic_members")
    if not observed_semantic_merge:
        raise AssertionError("preflight_fake_provider_did_not_exercise_semantic_merge")


def _assert_recoverable_issue_parent(result: Mapping[str, Any]) -> None:
    report = result["strict_reader"]["report"]
    if result["strict_reader"].get("loaded") is not True or result["terminal_job"].get("recovery_capability", {}).get("can_resume") is not True:
        raise AssertionError("durable_parent_not_strictly_resumable")
    discovery = report.get("model_discovery")
    if not isinstance(discovery, Mapping) or not isinstance(discovery.get("coverage"), Mapping) or discovery["coverage"].get("complete") is not False:
        raise AssertionError("recoverable_parent_discovery_coverage_not_partial")
    batches = discovery.get("execution_contract", {}).get("batches", []) if isinstance(discovery.get("execution_contract"), Mapping) else []
    if not any(isinstance(item, Mapping) and item.get("state") != "succeeded" for item in batches):
        raise AssertionError("recoverable_parent_missing_real_incomplete_discovery_batch")
    pipeline = report.get("model_issue_pipeline")
    if not isinstance(pipeline, Mapping) or pipeline.get("status") != "partial" or pipeline.get("reason") != "discovery_coverage_partial":
        raise AssertionError("recoverable_parent_issue_pipeline_not_expected_partial")
    pipeline_input = pipeline.get("input")
    if (
        not isinstance(pipeline_input, Mapping)
        or pipeline_input.get("discovery_status") != "partial"
        or pipeline_input.get("discovery_coverage_complete") is not False
    ):
        raise AssertionError("recoverable_parent_issue_pipeline_input_coverage_mismatch")
    findings = discovery.get("findings")
    finding_ids = [item.get("finding_id") for item in findings if isinstance(item, Mapping)] if isinstance(findings, list) else []
    input_ids = pipeline_input.get("finding_ids")
    candidates = pipeline.get("candidates")
    candidate_ids = [item.get("finding_id") for item in candidates if isinstance(item, Mapping)] if isinstance(candidates, list) else []
    decisions = pipeline.get("member_decisions")
    decision_ids = [item.get("finding_id") for item in decisions if isinstance(item, Mapping)] if isinstance(decisions, list) else []
    if (
        not isinstance(input_ids, list)
        or not isinstance(findings, list)
        or not isinstance(candidates, list)
        or not isinstance(decisions, list)
        or not finding_ids
        or len(finding_ids) != len(findings)
        or any(not isinstance(item, str) or not item for item in finding_ids)
        or len(finding_ids) != len(set(finding_ids))
        or len(candidate_ids) != len(candidates)
        or any(not isinstance(item, str) or not item for item in candidate_ids)
        or len(candidate_ids) != len(set(candidate_ids))
        or len(decision_ids) != len(decisions)
        or any(not isinstance(item, str) or not item for item in decision_ids)
        or len(decision_ids) != len(set(decision_ids))
        or any(not isinstance(item, str) or not item for item in input_ids)
        or len(input_ids) != len(set(input_ids))
        or set(finding_ids) != set(input_ids)
        or set(candidate_ids) != set(input_ids)
        or set(decision_ids) != set(input_ids)
        or pipeline_input.get("finding_count") != len(input_ids)
        or pipeline_input.get("candidate_count") != len(input_ids)
    ):
        raise AssertionError("recoverable_parent_discovery_member_ledger_incomplete")
    closure = pipeline.get("global_closure")
    if not isinstance(closure, Mapping) or closure.get("closed") is not True or closure.get("discovery_coverage_complete") is not False:
        raise AssertionError("recoverable_parent_global_closure_state_mismatch")
    closure_input_ids = closure.get("input_finding_ids")
    retained_ids = closure.get("retained_finding_ids")
    if (
        not isinstance(closure_input_ids, list)
        or not isinstance(retained_ids, list)
        or any(not isinstance(item, str) or not item for item in closure_input_ids)
        or any(not isinstance(item, str) or not item for item in retained_ids)
        or len(closure_input_ids) != len(set(closure_input_ids))
        or len(retained_ids) != len(set(retained_ids))
        or set(closure_input_ids) != set(input_ids)
        or set(retained_ids) != set(input_ids)
        or closure.get("pending_finding_ids") != []
        or closure.get("pending_issue_ids") != []
        or closure.get("excluded_finding_ids") != []
        or closure.get("excluded_issue_account") != []
        or closure.get("excluded_member_evidence") != []
        or closure.get("relation_gaps") != []
    ):
        raise AssertionError("recoverable_parent_global_closure_member_ledger_incomplete")
    input_member_evidence = closure.get("input_member_evidence")
    retained_member_evidence = closure.get("retained_member_evidence")
    if (
        not isinstance(input_member_evidence, list)
        or not isinstance(retained_member_evidence, list)
        or len(input_member_evidence) != len(input_ids)
        or len(retained_member_evidence) != len(input_ids)
        or any(not isinstance(item, list) or len(item) != 2 or any(not isinstance(member, str) or not member for member in item) for item in input_member_evidence)
        or any(not isinstance(item, list) or len(item) != 2 or any(not isinstance(member, str) or not member for member in item) for item in retained_member_evidence)
        or {item[0] for item in input_member_evidence} != set(input_ids)
        or {item[0] for item in retained_member_evidence} != set(input_ids)
        or len(input_member_evidence) != len(set(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in input_member_evidence))
        or len(retained_member_evidence) != len(set(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in retained_member_evidence))
        or {json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in input_member_evidence}
        != {json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in retained_member_evidence}
    ):
        raise AssertionError("recoverable_parent_global_closure_member_evidence_incomplete")
    for kind in ("grouping", "global", "supplement"):
        if not any(item.get("kind") == kind and item.get("status") == "accepted" and _successful_audits(item) for item in pipeline.get("shards", []) if isinstance(item, Mapping)):
            raise AssertionError(f"recoverable_parent_{kind}_audit_missing")
    review = report.get("model_review")
    if not isinstance(review, Mapping) or review.get("status") != "completed" or not _successful_audits(review):
        raise AssertionError("recoverable_parent_review_audit_missing")


def _assert_zero_call_issue_reuse(parent: Mapping[str, Any], child: Mapping[str, Any], provider: "_LoopbackProvider", document_id: str) -> None:
    parent_report = parent["strict_reader"]["report"]
    child_report = child["strict_reader"]["report"]
    parent_pipeline = parent_report["model_issue_pipeline"]
    child_pipeline = child_report["model_issue_pipeline"]
    for kind in ("grouping", "global", "supplement"):
        parent_accepted = [item for item in parent_pipeline["shards"] if item.get("kind") == kind and item.get("status") == "accepted"]
        child_accepted = [item for item in child_pipeline["shards"] if item.get("kind") == kind and item.get("status") == "accepted"]
        if not parent_accepted or not child_accepted or not all(item.get("reused") is True and item.get("parent_run_id") == parent["run_id"] for item in child_accepted):
            raise AssertionError(f"successful_{kind}_audit_not_reused_from_parent")
    child_review = child_report.get("model_review")
    if not isinstance(child_review, Mapping) or child_review.get("reason") != "review_reused_for_matching_inputs" or child_review.get("model_called") is not False:
        raise AssertionError("successful_review_not_reused_for_identical_business_input")
    if parent.get("run_id") == child.get("run_id") or parent.get("job_id") == child.get("job_id"):
        raise AssertionError("resume_child_must_have_new_job_and_run_identity")
    terminal_job = child.get("terminal_job")
    terminal_recovery = terminal_job.get("recovery") if isinstance(terminal_job, Mapping) else None
    if child.get("parent_run_id") != parent.get("run_id") or not isinstance(terminal_recovery, Mapping) or terminal_recovery.get("parent_run_id") != parent.get("run_id"):
        raise AssertionError("resume_child_parent_binding_mismatch")
    strict_reader = child.get("strict_reader")
    checkpoint_snapshot = strict_reader.get("snapshot") if isinstance(strict_reader, Mapping) else None
    if not isinstance(checkpoint_snapshot, Mapping) or checkpoint_snapshot.get("parent_run_id") != parent.get("run_id"):
        raise AssertionError("resume_child_checkpoint_parent_binding_mismatch")
    child_events = child.get("provider_http_events", [])
    if any(item.get("stage") in {"grouping", "global", "supplement", "review"} for item in child_events):
        raise AssertionError("issue_phase_http_call_made_during_zero_call_reuse_child")
    if not any(item.get("stage") == "discovery" and item.get("document_id") == document_id for item in child_events):
        raise AssertionError("legal_discovery_retry_missing_real_provider_http")
    parent_shards = {
        (item.get("kind"), item.get("shard_id")): item
        for item in parent_pipeline.get("shards", [])
        if isinstance(item, Mapping) and item.get("status") == "accepted"
    }
    child_shards = [item for item in child_pipeline.get("shards", []) if isinstance(item, Mapping) and item.get("status") == "accepted" and item.get("reused") is True]
    for item in child_shards:
        original = parent_shards.get((item.get("kind"), item.get("shard_id")))
        if original is None or item.get("audit") != original.get("audit"):
            raise AssertionError("reused_shard_lost_original_success_audit_index")
    parent_review_audits = parent_report.get("model_review", {}).get("audit_artifacts", [])
    child_review_audits = child_review.get("audit_artifacts", [])
    if not parent_review_audits or child_review_audits != parent_review_audits:
        raise AssertionError("reused_review_did_not_preserve_parent_success_audit_index")
    if parent["terminal_job"].get("run_id") == child["terminal_job"].get("run_id"):
        raise AssertionError("resume_run_id_was_not_new")


def _assert_business_input_drift_not_reused(parent: Mapping[str, Any], child: Mapping[str, Any]) -> None:
    parent_report = parent["strict_reader"]["report"]
    child_report = child["strict_reader"]["report"]
    parent_pipeline = parent_report["model_issue_pipeline"]
    child_pipeline = child_report["model_issue_pipeline"]
    parent_findings = parent_report.get("model_discovery", {}).get("findings", [])
    child_findings = child_report.get("model_discovery", {}).get("findings", [])
    if len(child_findings) <= len(parent_findings):
        raise AssertionError("business_drift_child_added_no_discovery_member")
    child_findings_by_id = {item["finding_id"]: item for item in child_findings}
    if any(child_findings_by_id.get(item["finding_id"]) != item for item in parent_findings):
        raise AssertionError("business_drift_dropped_or_changed_original_finding_or_citation")
    if child_report.get("model_discovery", {}).get("coverage", {}).get("complete") is not True:
        raise AssertionError("business_drift_child_coverage_not_updated")
    parent_rows = [item for item in parent_pipeline.get("shards", []) if isinstance(item, Mapping) and item.get("status") == "accepted"]
    child_rows = [item for item in child_pipeline.get("shards", []) if isinstance(item, Mapping)]
    parent_semantics = {
        kind: {item.get("semantic_input_sha256") for item in parent_rows if item.get("kind") == kind}
        for kind in ("grouping", "global", "supplement")
    }
    changed_group_rows = []
    unchanged_group_reuse = []
    for row in child_rows:
        kind = row.get("kind")
        if kind not in {"grouping", "global", "supplement"}:
            continue
        same_semantics = row.get("semantic_input_sha256") in parent_semantics[kind]
        if kind == "grouping" and same_semantics:
            prior = next(item for item in parent_rows if item.get("kind") == kind and item.get("semantic_input_sha256") == row.get("semantic_input_sha256"))
            if row.get("status") == "accepted" and row.get("reused") is True and row.get("audit") == prior.get("audit"):
                unchanged_group_reuse.append(row)
            elif row.get("status") == "accepted" and row.get("reused") is not True:
                raise AssertionError("unchanged_grouping_input_was_recalled_instead_of_reused")
        elif not same_semantics:
            if row.get("reused") is True:
                raise AssertionError(f"changed_{kind}_business_input_erroneously_reused")
            if kind == "grouping":
                changed_group_rows.append(row)
    if not unchanged_group_reuse:
        raise AssertionError("business_drift_case_did_not_preserve_any_unchanged_grouping_shard")
    if not changed_group_rows:
        raise AssertionError("business_drift_case_did_not_exercise_changed_grouping_input")
    review = child_report.get("model_review")
    if not isinstance(review, Mapping) or review.get("reason") == "review_reused_for_matching_inputs" or review.get("reused") is True:
        raise AssertionError("changed_coverage_or_members_did_not_invalidate_review")
    child_events = child.get("provider_http_events", [])
    grouping_events = [item for item in child_events if item.get("stage") == "grouping"]
    if [item.get("response_status") for item in grouping_events] != [429, 500]:
        raise AssertionError("resumed_two_attempt_issue_gate_did_not_count_429_and_5xx_retry")
    rows = [item for item in child.get("sqlite_read_only", {}).get("attempt_rows", []) if item.get("stage") == "annual_issue_grouping"]
    if len(rows) != 2 or child.get("sqlite_read_only", {}).get("run_stage_counts", {}).get("annual_issue_grouping") != 2:
        raise AssertionError("resumed_shared_issue_gate_did_not_count_both_retry_attempts")
    if len({item.get("gate_key") for item in rows}) != 1:
        raise AssertionError("resumed_issue_retries_did_not_share_one_gate_key")
    budget = child["terminal_job"]["analysis_budget"]
    if (
        budget["scope"] != "resume"
        or budget["provider_attempt_limits"]["task_effective"] != 20
        or budget["stages"]["annual_issue_grouping"]["attempt_limit"] != 2
        or len(child["sqlite_read_only"]["attempt_rows"]) > 20
    ):
        raise AssertionError("initial_business_drift_child_changed_resume_admission_budget")


def _assert_changed_issue_inputs_continued(
    before_drift: Mapping[str, Any], initial_drift: Mapping[str, Any], child: Mapping[str, Any],
) -> None:
    chain = initial_drift["resume_chain"]
    children = chain["children"]
    if not children or chain["current_run_id"] != child["run_id"]:
        raise AssertionError("changed_issue_continuation_chain_missing_final_child")
    events = chain["all_provider_http_events"]
    for stage in ("grouping", "global", "supplement", "review"):
        if not any(item.get("stage") == stage and item.get("response_status") == 200 for item in events):
            raise AssertionError(f"changed_issue_chain_did_not_recompute_{stage}")
    old_report = before_drift["strict_reader"]["report"]
    old_rows = [item for item in old_report["model_issue_pipeline"]["shards"] if item.get("status") == "accepted"]
    old_review_paths = {item.get("request_path") for item in old_report["model_review"]["audit_artifacts"]}
    unchanged_shard = next(item for item in old_rows if item.get("kind") == "grouping" and str(item.get("shard_id", "")).startswith("group:000:"))
    prior = before_drift
    for run in [initial_drift, *children]:
        run_report = run["strict_reader"]["report"]
        rows = run_report["model_issue_pipeline"]["shards"]
        retained_shard = next((item for item in rows if item.get("shard_id") == unchanged_shard["shard_id"]), None)
        if (
            not isinstance(retained_shard, Mapping)
            or retained_shard.get("status") != "accepted"
            or retained_shard.get("reused") is not True
            or retained_shard.get("audit") != unchanged_shard.get("audit")
            or any(item.get("stage") == "grouping" and item.get("request_body_sha256") == unchanged_shard["request_body_sha256"] for item in run["provider_http_events"])
        ):
            raise AssertionError("changed_issue_chain_recalled_or_lost_unchanged_shard_zero")
        prior_rows = prior["strict_reader"]["report"]["model_issue_pipeline"]["shards"]
        for row in rows:
            kind = row.get("kind")
            if kind not in {"global", "supplement"} or row.get("status") != "accepted":
                continue
            if any(
                old.get("kind") == kind and (
                    row.get("semantic_input_sha256") == old.get("semantic_input_sha256")
                    or row.get("audit") == old.get("audit")
                )
                for old in old_rows
            ):
                raise AssertionError(f"changed_{kind}_reused_before_drift_business_input_audit")
            if row.get("reused") is True and not any(
                previous.get("kind") == kind and previous.get("status") == "accepted"
                and previous.get("semantic_input_sha256") == row.get("semantic_input_sha256")
                and previous.get("audit") == row.get("audit")
                and row.get("parent_run_id") == prior["run_id"]
                for previous in prior_rows
            ):
                raise AssertionError(f"changed_{kind}_reuse_not_bound_to_recomputed_direct_parent")
        review = run_report["model_review"]
        if any(item.get("request_path") in old_review_paths for item in review.get("audit_artifacts", [])):
            raise AssertionError("changed_review_reused_before_drift_business_input_audit")
        if review.get("reason") == "review_reused_for_matching_inputs" and review.get("audit_artifacts") != prior["strict_reader"]["report"]["model_review"].get("audit_artifacts"):
            raise AssertionError("changed_review_reuse_not_bound_to_recomputed_direct_parent")
        prior = run
    _assert_completed_issue_stages(child)
    report = child["strict_reader"]["report"]
    pipeline = report["model_issue_pipeline"]
    retrieval = pipeline["retrieval_coverage"]
    if (
        retrieval.get("max_role_assignments") != 32
        or retrieval.get("role_assignments_total", 33) > 32
        or retrieval.get("role_assignments_pending") != 0
        or retrieval.get("complete") is not True
        or not pipeline["supplements"]
        or any(item.get("role_status") != "accepted" for item in pipeline["supplements"])
    ):
        raise AssertionError("changed_issue_chain_supplement_roles_not_complete")
    findings = report.get("model_discovery", {}).get("findings", [])
    drift_findings = [
        item for item in findings
        if isinstance(item, Mapping)
        and any(
            isinstance(citation, Mapping)
            and _SEMANTIC_DRIFT_SOURCE_TEXT in str(citation.get("quote", ""))
            for citation in item.get("citations", [])
        )
    ]
    if len(drift_findings) != 1:
        raise AssertionError("changed_continuation_semantic_drift_finding_missing_or_duplicated")
    drift_finding_id = drift_findings[0].get("finding_id")
    drift_decisions = [item for item in pipeline.get("member_decisions", []) if isinstance(item, Mapping) and item.get("finding_id") == drift_finding_id]
    if len(drift_decisions) != 1 or drift_decisions[0].get("disposition") != "retain":
        raise AssertionError("later_semantic_drift_was_not_kept_distinct")
    drift_issue_id = drift_decisions[0].get("issue_id")
    drift_issue = next((item for item in pipeline.get("issues", []) if isinstance(item, Mapping) and item.get("issue_id") == drift_issue_id), None)
    if not isinstance(drift_issue, Mapping) or drift_finding_id not in drift_issue.get("member_finding_ids", []):
        raise AssertionError("later_semantic_drift_issue_identity_was_not_preserved")
    candidate_by_id = {
        item.get("finding_id"): item
        for item in pipeline.get("candidates", [])
        if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
    }
    drift_key = _fixture_semantic_key(candidate_by_id.get(drift_finding_id))
    if drift_key is None or any(
        _fixture_semantic_key(candidate_by_id.get(member)) != drift_key
        for member in drift_issue.get("member_finding_ids", [])
    ):
        raise AssertionError("later_semantic_drift_was_merged_with_distinct_issue")


def _assert_supplement_401_closed_gate(result: Mapping[str, Any], provider: "_LoopbackProvider", document_id: str) -> None:
    events = provider.events_for(document_id)
    supplement_events = [item for item in events if item.get("stage") == "supplement"]
    if not supplement_events or not any(item.get("response_status") == 401 for item in supplement_events):
        raise AssertionError("supplement_401_not_observed")
    unauthorized = next(item for item in supplement_events if item.get("response_status") == 401)
    later = [item for item in events if item["sequence"] > unauthorized["sequence"]]
    if later:
        raise AssertionError("provider_http_dispatched_after_supplement_401")
    attempts = [item for item in result["sqlite_read_only"]["attempt_rows"] if item.get("stage") == "annual_issue_grouping"]
    if not attempts:
        raise AssertionError("issue_attempts_missing_from_shared_goal_ledger")
    issue_events = [item for item in result["provider_http_events"] if item.get("stage") in {"grouping", "global", "supplement"}]
    if len(attempts) != len(issue_events) or len(attempts) != 8:
        raise AssertionError("supplement_401_shared_issue_gate_missed_attempt_or_retry_charge")
    _assert_terminal_issue_gate_closed(result, attempts)
    if result["goal_ledger_goal_attempt_count"] != result["sqlite_read_only"]["goal_attempt_count"]:
        raise AssertionError("goal_ledger_count_projection_mismatch")
    report = result["strict_reader"]["report"]
    pipeline = report["model_issue_pipeline"]
    if result["strict_reader"].get("loaded") is not True or pipeline.get("status") != "failed" or pipeline.get("reason") != "http_401":
        raise AssertionError("supplement_401_not_exposed_as_durable_fatal_pipeline_failure")
    review = report["model_review"]
    # The existing authentication stop represents a not-called review as
    # abstained; ordinary supplement_role_pending may still receive review.
    if (
        review.get("status") != "abstained" or review.get("reason") != "http_401"
        or review.get("model_called") is not False
        or review.get("call_count") != 0 or review.get("call_attempt_count") != 0
        or review.get("audit_artifacts") != []
        or any(item.get("stage") == "annual_review" for item in result["sqlite_read_only"]["attempt_rows"])
    ):
        raise AssertionError("supplement_401_did_not_abstain_without_review_http_or_admission")
    shards = pipeline["shards"]
    supplement_shards = [item for item in shards if item.get("kind") == "supplement"]
    if len(supplement_shards) != 1:
        raise AssertionError("supplement_401_pending_shard_missing_or_duplicated")
    failed = supplement_shards[0]
    audit = failed.get("audit", {})
    linkage = result["http_audit_ledger_linkage"]
    failed_attempts = [item for item in linkage["attempt_rows"] if item.get("kind") == "supplement"]
    if (
        failed.get("status") != "pending" or failed.get("reason") != "http_401"
        or failed.get("attempt_count") != 1 or failed.get("response_count") != 0
        or audit.get("status") != "failed" or not audit.get("failure_path")
        or not audit.get("request_path") or audit.get("response_path") is not None
        or audit.get("request_body_sha256") != unauthorized["request_body_sha256"]
        or pipeline["audit_summary"].get("attempt_count") != 8
        or linkage.get("status") != "linked"
        or linkage.get("raw_http_bytes_validated") is not True
        or linkage.get("loopback_and_dummy_authorization_validated") is not True
        or linkage["http_request_count"] != linkage["sqlite_current_run_admission_count"]
        or len(failed_attempts) != 1
        or failed_attempts[0].get("reason") != "http_401"
        or failed_attempts[0].get("status") != "failed"
        or failed_attempts[0].get("provider_admitted") is not True
        or failed_attempts[0].get("request_body_sha256") != unauthorized["request_body_sha256"]
    ):
        raise AssertionError("supplement_401_failure_audit_or_eight_admission_account_was_lost")
    candidate_by_id = {item["finding_id"]: item for item in pipeline["candidates"]}
    finding_ids = [item["finding_id"] for item in report["model_discovery"]["findings"]]
    input_ids = pipeline["input"]["finding_ids"]
    decision_ids = [item["finding_id"] for item in pipeline["member_decisions"]]
    closure = pipeline["global_closure"]
    if (
        len(input_ids) <= 12 or len(input_ids) != len(set(input_ids))
        or sorted(finding_ids) != sorted(input_ids) or sorted(candidate_by_id) != sorted(input_ids)
        or len(pipeline["candidates"]) != len(input_ids)
        or sorted(decision_ids) != sorted(input_ids)
        or any(item.get("disposition") == "pending" for item in pipeline["member_decisions"])
        or closure.get("closed") is not True
        or sorted(closure.get("retained_finding_ids", [])) != sorted(input_ids)
        or closure.get("pending_finding_ids") != [] or closure.get("pending_issue_ids") != []
    ):
        raise AssertionError("supplement_401_erased_previously_accepted_member_coverage")
    grouping_shards = [item for item in shards if item.get("kind") == "grouping"]
    if len([item for item in shards if item.get("kind") == "global"]) != 1:
        raise AssertionError("supplement_401_erased_or_duplicated_successful_global_shard")
    grouped_ids = [finding_id for item in grouping_shards for finding_id in item["input_finding_ids"]]
    if sorted(grouped_ids) != sorted(input_ids):
        raise AssertionError("supplement_401_erased_original_group_shard_members")
    for shard in shards:
        if shard.get("kind") not in {"grouping", "global"}:
            continue
        if shard.get("status") != "accepted" or not _successful_audits(shard):
            raise AssertionError("supplement_401_erased_successful_group_or_global_audit")
        if shard.get("kind") == "grouping" and (
            not 1 <= len(shard["input_finding_ids"]) <= 3
            or shard["input"]["candidate_pool"] != [candidate_by_id[key] for key in sorted(shard["input_finding_ids"])]
        ):
            raise AssertionError("supplement_401_changed_original_raw_group_member_fields")
    issue_members = []
    for issue in pipeline["issues"]:
        issue_members.extend(issue["member_finding_ids"])
        if any(citation not in issue["citations"] for key in issue["member_finding_ids"] for citation in candidate_by_id[key]["citations"]):
            raise AssertionError("supplement_401_erased_original_member_citation")
    if sorted(issue_members) != sorted(input_ids):
        raise AssertionError("supplement_401_erased_accepted_member_issue_assignments")
    supplements = pipeline["supplements"]
    pending_pairs = sorted((issue_id, item["evidence_id"]) for item in supplements for issue_id in item["issue_ids"])
    requested_pairs = sorted((item["issue_id"], item["evidence_id"]) for item in failed["input"]["evidence_assignments"])
    if not supplements or any(item.get("role_status") != "pending" for item in supplements) or not pending_pairs or pending_pairs != requested_pairs:
        raise AssertionError("supplement_401_erased_or_falsely_accepted_pending_role_assignments")


def _assert_retry_attempts(result: Mapping[str, Any], stage: str) -> None:
    events = [item for item in result["provider_http_events"] if item.get("stage") == stage]
    retried = [item for item in events if item.get("response_status") in {429, 500, 502, 503, 504}]
    if not any(item.get("response_status") == 429 for item in retried) or not any(item.get("response_status") in {500, 502, 503, 504} for item in retried):
        raise AssertionError("429_and_5xx_retry_http_attempts_missing")
    if not any(item.get("response_status") == 200 for item in events):
        raise AssertionError("retried_stage_never_succeeded")
    rows = [item for item in result["sqlite_read_only"]["attempt_rows"] if item.get("stage") == "annual_issue_grouping"]
    issue_events = [item for item in result["provider_http_events"] if item.get("stage") in {"grouping", "global", "supplement"}]
    if len(rows) != len(issue_events):
        raise AssertionError("sqlite_shared_issue_gate_did_not_count_every_http_attempt")
    if len(rows) != 8:
        raise AssertionError("first_issue_gate_exceeded_eight_including_retries")
    _assert_terminal_issue_gate_closed(result, rows)
    budget = result["terminal_job"]["analysis_budget"]
    if (
        budget["scope"] != "new_run"
        or budget["provider_attempt_limits"]["task_effective"] != 30
        or budget["stages"]["annual_issue_grouping"]["attempt_limit"] != 8
        or len(result["sqlite_read_only"]["attempt_rows"]) > 30
    ):
        raise AssertionError("initial_preflight_changed_production_admission_budget")
    if len(events) < 3:
        raise AssertionError("retry_pair_did_not_reuse_same_provider_phase")


def _assert_terminal_issue_gate_closed(result: Mapping[str, Any], attempts: list[Mapping[str, Any]]) -> None:
    keys = {str(item.get("gate_key")) for item in attempts if isinstance(item.get("gate_key"), str)}
    if len(keys) != 1:
        raise AssertionError("issue_attempts_did_not_share_one_real_job_gate")
    rows = result.get("sqlite_read_only", {}).get("gate_rows", [])
    gate = [item for item in rows if item.get("gate_key") in keys]
    if len(gate) != 1 or gate[0].get("closed") is not True:
        raise AssertionError("terminal_job_did_not_close_the_actual_issue_admission_gate")


def _phase_http_boundaries(events: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for stage in sorted({str(item.get("stage")) for item in events}):
        rows = [item for item in events if item.get("stage") == stage]
        starts = [float(item["request_monotonic"]) for item in rows if isinstance(item.get("request_monotonic"), (int, float))]
        ends = [float(item["response_finished_monotonic"]) for item in rows if isinstance(item.get("response_finished_monotonic"), (int, float))]
        if starts:
            result[stage] = {"http_start_monotonic": min(starts), "http_end_monotonic": max(ends) if ends else max(starts), "http_request_count": len(rows)}
    return result


def _transition_start(case_dir: Path) -> float | None:
    path = case_dir / "job-transitions.json"
    if not path.is_file():
        return None
    value = _read_json(path)
    return value.get("poll_started_monotonic")


def _events_by_stage(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in events:
        result.setdefault(str(item.get("stage")), []).append(item)
    return result


def _create_controlled_sources(root: Path, evidence_root: Path) -> dict[str, dict[str, Any]]:
    names = ("preflight", "resume", "cancel_group", "deadline_global", "http401_supplement", "owner_hard_exit")
    result: dict[str, dict[str, Any]] = {}
    bytes_by_page_variant: dict[tuple[int, bool], tuple[bytes, str]] = {}
    input_root = evidence_root / "controlled-inputs"
    input_root.mkdir(parents=True, exist_ok=False)
    for name in names:
        page_count = 16 if name in {"preflight", "resume", "deadline_global", "http401_supplement"} else 1
        include_semantic_difference = name == "resume"
        page_variant = (page_count, include_semantic_difference)
        if page_variant not in bytes_by_page_variant:
            source_pdf_bytes = _build_synthetic_pdf(
                page_count=page_count,
                include_semantic_difference=include_semantic_difference,
            )
            bytes_by_page_variant[page_variant] = (source_pdf_bytes, hashlib.sha256(source_pdf_bytes).hexdigest())
        source_pdf_bytes, source_pdf_digest = bytes_by_page_variant[page_variant]
        company_id = f"SIM-R4-{name.upper()}-{uuid.uuid4().hex[:6]}"
        document_id = f"r4runtime-{name}-{uuid.uuid4().hex[:8]}"
        report_year = 2025
        relative = f"{company_id}/{report_year}/{document_id}/source.pdf"
        source_dir = root / "data" / "raw" / company_id / str(report_year) / document_id
        source_dir.mkdir(parents=True, exist_ok=False)
        pdf_path = source_dir / "source.pdf"
        _write_bytes_new(pdf_path, source_pdf_bytes)
        source_path = source_dir / "source.json"
        _write_json_new(source_path, {
            "company_id": company_id,
            "document_id": document_id,
            "report_type": "年度报告（原创虚构运行测试材料）",
            "report_period": f"{report_year}-12-31",
            "local_path": f"data/raw/{relative}",
            "sha256": source_pdf_digest,
            "source_nature": "本次R4运行测试原创构造的虚构软件验收资料，不是现实公司披露、财务黄金答案、异常标签或舞弊标签",
            "test_only": True,
        })
        evidence_input = input_root / f"{name}-source.pdf"
        shutil.copy2(pdf_path, evidence_input)
        if _sha256(evidence_input) != source_pdf_digest:
            raise AssertionError("controlled_pdf_copy_hash_mismatch")
        result[name] = {
            "company_id": company_id,
            "report_year": report_year,
            "content_page_count": page_count,
            "pdf_page_count": page_count + 1,
            "document_id": document_id,
            "source_pdf_path": relative,
            "pdf": pdf_path,
            "source_record": source_path,
            "sha256": source_pdf_digest,
            "evidence_pdf": evidence_input,
        }
    _write_json_new(input_root / "source-index.json", {
        "source_pdf_sha256": source_pdf_digest,
        "source_pdf_bytes": len(source_pdf_bytes),
        "source_pdf_hashes": sorted({item["sha256"] for item in result.values()}),
        "generated_from_blank_document": True,
        "source_nature": "fictional software test fixture",
        "fixtures": {name: {key: str(value[key]) if isinstance(value[key], Path) else value[key] for key in ("company_id", "report_year", "content_page_count", "pdf_page_count", "document_id", "source_pdf_path", "sha256", "evidence_pdf")} for name, value in result.items()},
    })
    return result


def _build_synthetic_pdf(*, page_count: int = 6, include_semantic_difference: bool = False) -> bytes:
    document = pymupdf.open()
    try:
        # Several repeated narrative pages create more than one real discovery
        # batch while staying small enough for a bounded local integration run.
        for page_index in range(page_count):
            page = document.new_page(width=595, height=842)
            page.insert_text((34, 28), f"管理层讨论与分析（虚构测试页{page_index + 1}）", fontsize=7, fontname="china-s")
            repeated = "本页为软件运行验收专用的虚构经营文字。合同回款台账和账龄核对只用于检验原文定位、成员归并与同源补证流程，不代表现实披露或财务结论。"
            # Keep the duplicate right-column padding inside the physical
            # page; implicit PDF clipping previously left a bbox beyond x595.
            right_text = repeated[:int((page.rect.width - 310 - 4) / 5.5)]
            for line_index in range(36):
                y = 45 + line_index * 21
                page.insert_text((28, y), repeated, fontsize=5.5, fontname="china-s")
                page.insert_text((310, y), right_text, fontsize=5.5, fontname="china-s")
            if include_semantic_difference and page_index == 15:
                page.insert_text((28, 812), _SEMANTIC_DRIFT_SOURCE_TEXT, fontsize=7, fontname="china-s")
        note_page = document.new_page(width=595, height=842)
        note_page.insert_text((34, 30), "财务报表附注（虚构测试）", fontsize=8, fontname="china-s")
        note = "合同回款账龄披露核对仅为本地软件测试构造的同源附注情境。"
        for index in range(8):
            note_page.insert_text((32, 75 + index * 34), note, fontsize=7, fontname="china-s")
        document.set_metadata({
            "title": "FINTRACE R4 原创虚构运行时测试材料",
            "subject": "仅用于本地软件运行边界测试",
            "creator": "tests/integration/test_annual_issue_recovery_runtime.py",
        })
        return document.tobytes(garbage=3, deflate=True)
    finally:
        document.close()


def _write_dummy_model_settings(path: Path, model: str, base_url: str) -> None:
    lines = [
        f"MODEL_BASE_URL={json.dumps(base_url, ensure_ascii=False)}",
        f"MODEL_NAME={json.dumps(model, ensure_ascii=False)}",
        f"MODEL_API_KEY={json.dumps(_DUMMY_KEY, ensure_ascii=False)}",
    ]
    _write_bytes_new(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _job_request(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company_id": fixture["company_id"],
        "report_year": fixture["report_year"],
        "document_id": fixture["document_id"],
        "source_pdf_path": fixture["source_pdf_path"],
        "sha256": fixture["sha256"],
        "mode": "model_investigation",
    }


def _source_manifest(root: Path) -> dict[str, Any]:
    paths = [root / "scripts" / "analyze_annual.py"]
    paths.extend((root / "backend" / "src" / "finagent").rglob("*.py"))
    paths.extend((root / "config" / "prompts").rglob("*.md"))
    rows = []
    for path in sorted({item.resolve() for item in paths}, key=lambda item: item.relative_to(root.resolve()).as_posix()):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            raise AssertionError("source_allowlist_nonregular_entry")
        rows.append({"path": path.relative_to(root.resolve()).as_posix(), "sha256": _sha256(path)})
    entries = [[item["path"], item["sha256"]] for item in rows]
    material = json.dumps(entries, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return {"sha256": hashlib.sha256(material).hexdigest(), "file_count": len(rows), "files": rows}


def _assert_runtime_identity(value: Any, role: str, source_manifest: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping):
        raise AssertionError(f"actual_{role}_runtime_identity_missing")
    if (
        value.get("kind") != "fintrace_annual_runtime_identity_v1"
        or value.get("protocol_version") != "annual-analysis-cli-v1"
        or value.get("role") != role
        or value.get("loaded_source_match") is not True
        or value.get("source_manifest_sha256") != source_manifest.get("sha256")
        or value.get("source_file_count") != source_manifest.get("file_count")
        or not isinstance(value.get("fingerprint"), str)
        or len(str(value.get("fingerprint"))) != 64
        or not isinstance(value.get("process_instance_id"), str)
        or not value.get("process_instance_id")
    ):
        raise AssertionError(f"actual_{role}_runtime_identity_does_not_match_frozen_source")


def _actual_cli_identity_capture(directory: Path, popen_event: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    expected_identity_key = _popen_process_identity_key(popen_event)
    matches = []
    for path in directory.glob("actual-cli-runtime-identity-*.json"):
        value = _read_json(path)
        if value.get("process_id") == popen_event.get("pid"):
            actual_identity_key = _popen_process_identity_key({
                "parent_pid": value.get("parent_pid"),
                "pid": value.get("process_id"),
                "popen_object_identity": value.get("parent_popen_object_identity"),
                "owned_handle_identity": value.get("process_creation_identity"),
            })
            if actual_identity_key == expected_identity_key:
                matches.append((path, value))
    if len(matches) != 1:
        raise AssertionError(f"actual_cli_profile_capture_count_for_owned_process_identity:{expected_identity_key}:{len(matches)}")
    return matches[0]


def _append_actual_cli_identity_profile(path: Path) -> None:
    source = r'''
import ctypes
from ctypes import wintypes

def _r4_current_process_identity():
    if os.name != "nt":
        return None
    process_id = os.getpid()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel.GetProcessId.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, process_id)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return {
            "process_id": int(kernel.GetProcessId(handle)),
            "creation_filetime": (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime),
        }
    finally:
        kernel.CloseHandle(handle)

def _capture_actual_cli_identity(frame, event, value):
    if (
        event == "return"
        and frame.f_code.co_name == "run_annual_investigation_appendix"
        and frame.f_globals.get("__name__") == "finagent.reports.investigation_append"
    ):
        cancel_file = os.environ.get("FINTRACE_CALL_CANCEL_FILE")
        if cancel_file and os.path.exists(cancel_file):
            time.sleep(5.0)
        return
    if event != "return" or frame.f_code.co_name != "capture_cli_runtime_identity":
        return
    if frame.f_globals.get("__name__") != "finagent.core.annual_runtime_identity":
        return
    if Path(sys.argv[0]).name != "analyze_annual.py" or sys.argv[1:2] == ["--runtime-identity"]:
        return
    directory = os.environ.get("FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR")
    if not directory or not isinstance(value, dict):
        return
    target = Path(directory) / ("actual-cli-runtime-identity-" + str(os.getpid()) + "-" + __import__("uuid").uuid4().hex + ".json")
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({
                "process_id": os.getpid(),
                "parent_pid": os.getppid(),
                "parent_popen_object_identity": os.environ.get("FINTRACE_R4_RUNTIME_PARENT_POPEN_OBJECT_IDENTITY"),
                "process_creation_identity": _r4_current_process_identity(),
                "argv": list(sys.argv),
                "cwd": str(Path.cwd().resolve()),
                "runtime_identity": value,
            }, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        return

if _startup_role == "cli" and "--with-model" in _original_argv:
    sys.setprofile(_capture_actual_cli_identity)
'''
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(source)


def _validate_attempt_location(attempt: Path) -> None:
    evaluations_root = (_ROOT / "artifacts" / "evaluations" / "annual-recovery-r4-20261010").resolve()
    if attempt.parent != evaluations_root or not attempt.name.startswith("runtime-acceptance-"):
        raise AssertionError("formal_attempt_is_outside_unique_r4_evaluation_namespace")


def _capture_allowlist(snapshot_root: Path) -> None:
    if snapshot_root.exists():
        raise FileExistsError("snapshot_root_must_be_unique")
    (snapshot_root / "backend" / "src").mkdir(parents=True)
    shutil.copytree(_ROOT / "backend" / "src" / "finagent", snapshot_root / "backend" / "src" / "finagent")
    (snapshot_root / "scripts").mkdir()
    shutil.copy2(_ROOT / "scripts" / "analyze_annual.py", snapshot_root / "scripts" / "analyze_annual.py")
    (snapshot_root / "config" / "prompts").mkdir(parents=True)
    for prompt in (_ROOT / "config" / "prompts").rglob("*.md"):
        target = snapshot_root / "config" / "prompts" / prompt.relative_to(_ROOT / "config" / "prompts")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prompt, target)
    (snapshot_root / "artifacts").mkdir()
    (snapshot_root / "data" / "raw").mkdir(parents=True)


def _load_snapshot_codec(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("_r4_snapshot_wire_codec", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("snapshot_wire_codec_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _initialize_ledger(root: Path, config_path: Path) -> None:
    script = "from pathlib import Path; import sys; from finagent.core.model_call_ledger import initialize_provider_call_ledger; initialize_provider_call_ledger(Path(sys.argv[1]))"
    env = _minimal_child_environment(root)
    env["FINTRACE_MODEL_CALL_LEDGER_CONFIG"] = str(config_path)
    result = subprocess.run([sys.executable, "-c", script, str(root / "artifacts" / "provider-call-ledger.sqlite3")], cwd=root, env=env, capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        raise AssertionError(f"goal_ledger_init_failed:{result.stderr[-2000:]!r}")


def _minimal_api_environment(root: Path, ledger_config: Path, popen_events: Path) -> dict[str, str]:
    env = _minimal_child_environment(root)
    env.update({
        "FINTRACE_MODEL_CALL_LEDGER_CONFIG": str(ledger_config),
        "FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS": "60",
        "FINTRACE_R4_RUNTIME_POPEN_EVENTS": str(popen_events),
        "FINTRACE_R4_RUNTIME_SOURCE_ROOT": str(root.resolve()),
        "PYTHONPATH": str(root / "backend" / "src"),
    })
    return env


def _minimal_child_environment(root: Path) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC")
    env = {key: os.environ[key] for key in keep if os.environ.get(key)}
    env.update({
        "PYTHONPATH": str(root / "backend" / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    if any(key.upper().startswith("MODEL_") for key in env) or any(key.upper().endswith("_API_KEY") for key in env):
        raise AssertionError("workspace_provider_configuration_leaked_into_test_process")
    return env


def _observer_role_for_argv(argv: Any, cwd: str | Path, source_root: str | Path) -> str:
    """Classify only exact Python module forms or the frozen CLI script path."""

    if not isinstance(argv, (list, tuple)) or len(argv) < 2:
        return "helper"
    values = [str(value) for value in argv]
    if len(values) >= 3 and values[1:3] == ["-m", "uvicorn"]:
        return "api"
    if len(values) >= 3 and values[1:3] == ["-m", "finagent.llm.chat_worker"]:
        return "worker"
    index = 1
    no_argument_options = {"-B", "-b", "-E", "-I", "-O", "-OO", "-s", "-S", "-u", "-v", "-vv", "-vvv", "-q"}
    while index < len(values):
        token = values[index]
        if token == "--":
            index += 1
            break
        if token in {"-m", "-c"}:
            return "helper"
        if token in no_argument_options:
            index += 1
            continue
        if token in {"-W", "-X"}:
            if index + 1 >= len(values):
                return "helper"
            index += 2
            continue
        if token.startswith(("-W", "-X")) and len(token) > 2:
            index += 1
            continue
        if token.startswith("-"):
            return "helper"
        break
    if index >= len(values):
        return "helper"
    candidate = Path(values[index])
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    expected = Path(source_root) / "scripts" / "analyze_annual.py"
    try:
        return "cli" if candidate.resolve(strict=False) == expected.resolve(strict=False) else "helper"
    except (OSError, RuntimeError, ValueError):
        return "helper"


def _write_popen_observer(path: Path, event_path: Path) -> None:
    source = '''\
import builtins
import ctypes
import errno
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes

_capture_path = os.environ.get("FINTRACE_R4_RUNTIME_POPEN_EVENTS")
_source_sha = os.environ.get("FINTRACE_R4_RUNTIME_SOURCE_SHA256")
_source_root = os.environ.get("FINTRACE_R4_RUNTIME_SOURCE_ROOT")
_original_popen = subprocess.Popen
_original_import = builtins.__import__
_seen_modules = set()
_append_thread_lock = threading.RLock()
_append_timeout_seconds = 10.0

def _write_append_error_marker(error):
    marker_path = (
        _capture_path + ".observer-error-" + str(os.getpid()) + "-"
        + str(threading.get_ident()) + "-" + str(time.time_ns()) + "-"
        + uuid.uuid4().hex + ".json"
    )
    marker = {
        "event": "observer_append_error",
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        "monotonic": time.monotonic(),
        "error_type": type(error).__name__,
        "error": str(error),
    }
    encoded = (json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\\n").encode("utf-8")
    descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if not written:
                raise OSError("observer error marker write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _append(value):
    if not _capture_path:
        return
    try:
        deadline = time.monotonic() + _append_timeout_seconds
        if not _append_thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise TimeoutError("timed out waiting for the observer thread lock")
        try:
            lock_path = _capture_path + ".lock"
            row = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\\n").encode("utf-8")
            with open(lock_path, "a+b", buffering=0) as lock_file:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\\0")
                locked = False
                while not locked:
                    lock_file.seek(0)
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("timed out waiting for the observer process lock") from exc
                        time.sleep(min(0.002, remaining))
                try:
                    with open(_capture_path, "ab", buffering=0) as output:
                        remaining_row = memoryview(row)
                        while remaining_row:
                            written = output.write(remaining_row)
                            if not written:
                                raise OSError("observer append made no progress")
                            remaining_row = remaining_row[written:]
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            _append_thread_lock.release()
    except Exception as exc:
        _write_append_error_marker(exc)
        raise

def _process_role(argv, cwd=None):
    if not isinstance(argv, (list, tuple)) or len(argv) < 2:
        return "helper"
    values = [str(value) for value in argv]
    if len(values) >= 3 and values[1:3] == ["-m", "uvicorn"]:
        return "api"
    if len(values) >= 3 and values[1:3] == ["-m", "finagent.llm.chat_worker"]:
        return "worker"
    index = 1
    no_argument_options = {"-B", "-b", "-E", "-I", "-O", "-OO", "-s", "-S", "-u", "-v", "-vv", "-vvv", "-q"}
    while index < len(values):
        token = values[index]
        if token == "--":
            index += 1
            break
        if token in {"-m", "-c"}:
            return "helper"
        if token in no_argument_options:
            index += 1
            continue
        if token in {"-W", "-X"}:
            if index + 1 >= len(values):
                return "helper"
            index += 2
            continue
        if token.startswith(("-W", "-X")) and len(token) > 2:
            index += 1
            continue
        if token.startswith("-"):
            return "helper"
        break
    if index >= len(values) or not isinstance(_source_root, str) or not _source_root:
        return "helper"
    candidate = Path(values[index])
    effective_cwd = Path(cwd or os.getcwd())
    if not candidate.is_absolute():
        candidate = effective_cwd / candidate
    expected = Path(_source_root) / "scripts" / "analyze_annual.py"
    try:
        return "cli" if candidate.resolve(strict=False) == expected.resolve(strict=False) else "helper"
    except (OSError, RuntimeError, ValueError):
        return "helper"

def _current_process_identity():
    if os.name != "nt":
        return None
    process_id = os.getpid()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel.GetProcessId.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, process_id)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return {
            "process_id": int(kernel.GetProcessId(handle)),
            "creation_filetime": (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime),
        }
    finally:
        kernel.CloseHandle(handle)

_sitecustomize_argv = [str(value) for value in sys.argv]
_original_argv = [str(value) for value in getattr(sys, "orig_argv", sys.argv)]
_startup_cwd = str(Path.cwd().resolve())
_startup_role = _process_role(_original_argv, cwd=_startup_cwd)
_startup_process_identity = _current_process_identity()
_parent_popen_object_identity = os.environ.get("FINTRACE_R4_RUNTIME_PARENT_POPEN_OBJECT_IDENTITY")
_parent_role = os.environ.get("FINTRACE_R4_RUNTIME_PARENT_ROLE")
_parent_pid_from_popen = os.environ.get("FINTRACE_R4_RUNTIME_PARENT_PID")
_append({
    "event": "python_process_startup", "role": _startup_role,
    "pid": os.getpid(), "parent_pid": os.getppid(), "argv": _sitecustomize_argv,
    "parent_role": _parent_role,
    "parent_pid_from_parent_popen": int(_parent_pid_from_popen) if _parent_pid_from_popen and _parent_pid_from_popen.isdigit() else None,
    "parent_popen_object_identity": _parent_popen_object_identity,
    "process_creation_identity": _startup_process_identity,
    "original_argv": _original_argv, "sitecustomize_argv": _sitecustomize_argv,
    "cwd": _startup_cwd, "source_root": str(Path(_source_root).resolve()) if _source_root else None,
    "python_executable": sys.executable, "source_manifest_sha256": _source_sha,
    "startup_monotonic": time.monotonic(),
})

def _record_loaded_module(name):
    if _startup_role == "worker" and name != "finagent.llm.chat_worker":
        return
    if not (name == "finagent" or name.startswith("finagent.")) or name in _seen_modules:
        return
    module = sys.modules.get(name)
    if module is None:
        main_module = sys.modules.get("__main__")
        if getattr(getattr(main_module, "__spec__", None), "name", None) == name:
            module = main_module
    filename = getattr(module, "__file__", None)
    if not isinstance(filename, str):
        return
    source = Path(filename)
    try:
        resolved = source.resolve(strict=True)
        digest = __import__("hashlib").sha256(resolved.read_bytes()).hexdigest()
    except (OSError, RuntimeError):
        return
    _seen_modules.add(name)
    _append({
        "event": "loaded_finagent_module", "role": _startup_role,
        "pid": os.getpid(), "parent_pid": os.getppid(), "module": name,
        "parent_role": _parent_role,
        "parent_pid_from_parent_popen": int(_parent_pid_from_popen) if _parent_pid_from_popen and _parent_pid_from_popen.isdigit() else None,
        "parent_popen_object_identity": _parent_popen_object_identity,
        "process_creation_identity": _startup_process_identity,
        "source_path": str(resolved), "source_sha256": digest,
        "source_manifest_sha256": _source_sha,
    })

def _observed_import(name, globals=None, locals=None, fromlist=(), level=0):
    if _startup_role == "worker":
        result = _original_import(name, globals, locals, fromlist, level)
        main_spec_name = getattr(getattr(sys.modules.get("__main__"), "__spec__", None), "name", None)
        if isinstance(main_spec_name, str):
            _record_loaded_module(main_spec_name)
            if main_spec_name in _seen_modules:
                builtins.__import__ = _original_import
        return result
    before = set(sys.modules)
    result = _original_import(name, globals, locals, fromlist, level)
    for module_name in set(sys.modules) - before:
        _record_loaded_module(module_name)
    if isinstance(name, str):
        _record_loaded_module(name)
    return result

builtins.__import__ = _observed_import

def _native_identity(process):
    if os.name != "nt" or not hasattr(process, "_handle"):
        return None
    try:
        handle = wintypes.HANDLE(int(process._handle))
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel.GetProcessId.restype = wintypes.DWORD
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(wintypes.FILETIME()), ctypes.byref(wintypes.FILETIME())):
            return None
        return {
            "process_id_from_owned_handle": int(kernel.GetProcessId(handle)),
            "creation_filetime": (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime),
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return None

class _ObservedPopen(_original_popen):
    def __init__(self, args, *positional, **keywords):
        values = list(args) if isinstance(args, (list, tuple)) else [args]
        argv = [str(value) for value in values]
        effective_cwd = str(Path(keywords.get("cwd") or os.getcwd()).resolve())
        command_role = _process_role(argv, cwd=effective_cwd)
        if command_role == "cli" and "--with-model" in argv:
            role = "api_owned_cli"
        elif command_role == "cli" and "--runtime-identity" in argv:
            role = "api_runtime_identity_probe"
        elif command_role == "worker":
            role = "cli_owned_worker"
        else:
            role = None
        raw_env = keywords.get("env")
        child_env = dict(os.environ if raw_env is None else raw_env)
        if role and _capture_path:
            child_env["FINTRACE_R4_RUNTIME_POPEN_EVENTS"] = _capture_path
            child_env["FINTRACE_R4_RUNTIME_PARENT_POPEN_OBJECT_IDENTITY"] = hex(id(self))
            child_env["FINTRACE_R4_RUNTIME_PARENT_ROLE"] = _startup_role
            child_env["FINTRACE_R4_RUNTIME_PARENT_PID"] = str(os.getpid())
            if _source_sha:
                child_env["FINTRACE_R4_RUNTIME_SOURCE_SHA256"] = _source_sha
            if _source_root:
                child_env["FINTRACE_R4_RUNTIME_SOURCE_ROOT"] = _source_root
            keywords["env"] = child_env
        super().__init__(args, *positional, **keywords)
        self._r4_capture = bool(role and _capture_path)
        if not self._r4_capture:
            return
        self._r4_identity = {
            "event": "popen_returned",
            "role": role,
            "parent_role": _startup_role,
            "popen_object_identity": hex(id(self)),
            "pid": self.pid,
            "parent_pid": os.getpid(),
            "parent_process_popen_object_identity": _parent_popen_object_identity,
            "parent_process_creation_identity": _startup_process_identity,
            "popen_returned_monotonic": time.monotonic(),
            "argv": argv,
            "cwd": effective_cwd,
            "expected_child_startup_role": command_role,
            "owned_handle_identity": _native_identity(self),
            "document_id": next((argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "--document-id"), None),
            "environment": {key: child_env[key] for key in (
                "FINTRACE_ANNUAL_MAIN_SOFT_DEADLINE_MONOTONIC",
                "FINTRACE_ANNUAL_MAIN_HARD_DEADLINE_MONOTONIC",
                "FINTRACE_ANNUAL_MAIN_LIMIT_SECONDS",
                "FINTRACE_ANNUAL_STAGE_TIME_LIMITS_SECONDS",
                "FINTRACE_CALL_GATE_TASK_LIMIT",
                "FINTRACE_CALL_GATE_STAGE_LIMITS",
                "FINTRACE_ACTIVE_SOURCE_SHA256",
                "MODEL_BASE_URL",
                "MODEL_NAME",
            ) if key in child_env},
            "model_api_key_present": bool(child_env.get("MODEL_API_KEY")),
            "process_owner_is_parent_popen_object": True,
        }
        _append(self._r4_identity)

    def _record_r4_exit(self, result, observed_via):
        if (
            not getattr(self, "_r4_capture", False)
            or getattr(self, "_r4_exit_written", False)
            or result is None
        ):
            return
        self._r4_exit_written = True
        before_identity = self._r4_identity.get("owned_handle_identity")
        after_identity = _native_identity(self)
        _append({
            "event": "popen_exit_observed",
            "role": self._r4_identity["role"],
            "parent_role": self._r4_identity["parent_role"],
            "popen_object_identity": self._r4_identity["popen_object_identity"],
            "pid": self.pid,
            "parent_pid": os.getpid(),
            "parent_process_popen_object_identity": _parent_popen_object_identity,
            "parent_process_creation_identity": _startup_process_identity,
            "observed_via": observed_via,
            "returncode": int(result),
            "exit_observed_monotonic": time.monotonic(),
            "owned_handle_identity_before_observation": before_identity,
            "owned_handle_identity_after_observation": after_identity,
            "handle_identity_same_before_after": before_identity == after_identity,
            "process_owner_is_parent_popen_object": True,
        })

    def poll(self):
        result = super().poll()
        self._record_r4_exit(result, "poll")
        return result

    def wait(self, *args, **kwargs):
        result = super().wait(*args, **kwargs)
        self._record_r4_exit(result, "wait")
        return result

subprocess.Popen = _ObservedPopen
'''
    _write_bytes_new(path, source.encode("utf-8"))


def _request_json(base_url: str, method: str, path: str, payload: object | None, evidence_prefix: Path) -> dict[str, Any]:
    evidence_prefix.parent.mkdir(parents=True, exist_ok=True)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    request = Request(base_url + path, data=body, headers=headers, method=method)
    started = time.monotonic()
    try:
        with urlopen(request, timeout=15) as response:
            status = int(response.status)
            raw = response.read()
            response_headers = dict(response.headers.items())
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
        response_headers = dict(exc.headers.items()) if exc.headers else {}
    except URLError as exc:
        raise RuntimeError(f"loopback_api_unreachable:{method}:{path}:{type(exc.reason).__name__}") from None
    ended = time.monotonic()
    record = {
        "method": method,
        "path": path,
        "request_body_utf8": body.decode("utf-8") if body is not None else None,
        "request_body_sha256": hashlib.sha256(body or b"").hexdigest(),
        "request_monotonic": started,
        "response_status": status,
        "response_body_utf8": raw.decode("utf-8", errors="replace"),
        "response_body_sha256": hashlib.sha256(raw).hexdigest(),
        "response_headers": response_headers,
        "response_monotonic": ended,
    }
    _write_json_new(evidence_prefix.with_suffix(".json"), record)
    result: dict[str, Any] = {"status": status, "raw": raw, "headers": response_headers, "monotonic": {"start": started, "end": ended}}
    try:
        result["json"] = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["json"] = None
    return result


def _wait_api_ready(base_url: str, process: subprocess.Popen[bytes], *, timeout: float) -> None:
    deadline = time.monotonic() + max(timeout, 90.0)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"uvicorn_exited_before_ready:{process.returncode}")
        try:
            with urlopen(base_url + "/v1/runtime-identity", timeout=5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError("snapshot_uvicorn_api_ready_timeout")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _stop_observed_popen_process(popen_event: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
    """Stop an orphan only after matching its full Popen/native process identity."""

    if os.name != "nt":
        raise RuntimeError("owner_hard_exit_identity_stop_requires_windows_process_handle")
    identity_key = _popen_process_identity_key(popen_event)
    expected = popen_event.get("owned_handle_identity")
    pid = popen_event.get("pid")
    argv = popen_event.get("argv")
    cwd = popen_event.get("cwd")
    role = popen_event.get("role")
    parent_role = popen_event.get("parent_role")
    parent_creation = popen_event.get("parent_process_creation_identity")
    expected_child_role = {"api_owned_cli": "cli", "api_runtime_identity_probe": "cli", "cli_owned_worker": "worker"}.get(str(role))
    if (
        expected_child_role is None
        or parent_role != ("cli" if role == "cli_owned_worker" else "api")
        or not isinstance(argv, list)
        or not all(isinstance(value, str) for value in argv)
        or not isinstance(cwd, str)
        or not cwd
        or popen_event.get("expected_child_startup_role") != expected_child_role
        or _observer_role_for_argv(argv, cwd, cwd) != expected_child_role
        or popen_event.get("process_owner_is_parent_popen_object") is not True
        or not isinstance(popen_event.get("parent_process_popen_object_identity"), str)
        or not popen_event.get("parent_process_popen_object_identity")
        or not isinstance(parent_creation, Mapping)
        or parent_creation.get("process_id") != popen_event.get("parent_pid")
        or type(parent_creation.get("creation_filetime")) is not int
        or parent_creation.get("creation_filetime", 0) <= 0
    ):
        raise AssertionError("observed_popen_event_role_parent_argv_or_cwd_identity_invalid")
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    handle_value: int | None = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel.GetProcessId.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess_for_observed_popen_identity_failed")
    handle_value = int(handle)
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        actual_pid = int(kernel.GetProcessId(handle))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise OSError(ctypes.get_last_error(), "GetProcessTimes_for_observed_popen_identity_failed")
        actual_creation = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        identity_verified = (
            actual_pid == pid
            and actual_creation == expected.get("creation_filetime")
            and expected.get("process_id_from_owned_handle") == pid
        )
        if not identity_verified:
            raise AssertionError("refusing_to_stop_pid_with_different_creation_identity")
        stop_started = time.monotonic()
        terminated = bool(kernel.TerminateProcess(handle, 0xE4))
        terminate_error = ctypes.get_last_error() if not terminated else None
        wait_result = int(kernel.WaitForSingleObject(handle, max(1, int(timeout * 1000))))
        if wait_result not in {WAIT_OBJECT_0}:
            if wait_result == WAIT_TIMEOUT:
                raise TimeoutError("observed_popen_process_stop_wait_timeout")
            raise OSError(ctypes.get_last_error(), f"WaitForSingleObject_failed:{wait_result}")
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess_for_observed_popen_identity_failed")
        return {
            "popen_object_identity": popen_event.get("popen_object_identity"),
            "role": popen_event.get("role"),
            "parent_role": popen_event.get("parent_role"),
            "pid": pid,
            "parent_pid": popen_event.get("parent_pid"),
            "argv": list(argv),
            "cwd": cwd,
            "parent_process_popen_object_identity": popen_event.get("parent_process_popen_object_identity"),
            "parent_process_creation_identity": dict(parent_creation),
            "process_identity_key": list(identity_key),
            "expected_owned_handle_identity": dict(expected),
            "actual_handle_identity_before_stop": {"process_id_from_open_handle": actual_pid, "creation_filetime": actual_creation},
            "identity_verified": identity_verified,
            "terminate_process_called_on_verified_handle": terminated,
            "terminate_process_error": terminate_error,
            "stop_observed": True,
            "wait_result": wait_result,
            "exit_code": int(exit_code.value),
            "stop_started_monotonic": stop_started,
            "stop_observed_monotonic": time.monotonic(),
            "stopped_by_verified_handle": True,
        }
    finally:
        if handle_value is not None:
            kernel.CloseHandle(wintypes.HANDLE(handle_value))


def _process_creation_metadata(pid: int) -> dict[str, Any]:
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
        "if ($p) { $p | Select-Object @{n='pid';e={$_.ProcessId}}, "
        "@{n='parent_pid';e={$_.ParentProcessId}}, CreationDate | ConvertTo-Json -Compress }"
    )
    completed = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("api_popen_process_creation_identity_unavailable")
    return json.loads(completed.stdout.strip())


def _native_popen_identity(process: subprocess.Popen[Any]) -> dict[str, Any] | None:
    if os.name != "nt" or not hasattr(process, "_handle"):
        return None
    try:
        handle = wintypes.HANDLE(int(process._handle))
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel.GetProcessId.restype = wintypes.DWORD
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(wintypes.FILETIME()), ctypes.byref(wintypes.FILETIME())):
            return None
        return {
            "process_id_from_owned_handle": int(kernel.GetProcessId(handle)),
            "creation_filetime": (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime),
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _read_api_process_identity(pid: int) -> dict[str, Any] | None:
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
        "if ($p) { $p | Select-Object @{n='pid';e={$_.ProcessId}}, "
        "@{n='parent_pid';e={$_.ParentProcessId}}, CreationDate | ConvertTo-Json -Compress }"
    )
    completed = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return json.loads(completed.stdout.strip())


def _popen_process_identity_key(value: Mapping[str, Any], *, native_field: str = "owned_handle_identity") -> tuple[int, int, str, int]:
    parent_pid = value.get("parent_pid")
    process_id = value.get("pid")
    popen_object_identity = value.get("popen_object_identity")
    native = value.get(native_field)
    if (
        type(parent_pid) is not int
        or parent_pid <= 0
        or type(process_id) is not int
        or process_id <= 0
        or not isinstance(popen_object_identity, str)
        or not popen_object_identity.strip()
        or not isinstance(native, Mapping)
    ):
        raise AssertionError("popen_process_identity_key_fields_missing")
    native_process_id = next((native.get(key) for key in ("process_id_from_owned_handle", "process_id_from_open_handle", "process_id") if key in native), None)
    creation_filetime = native.get("creation_filetime")
    if (
        type(native_process_id) is not int
        or native_process_id != process_id
        or type(creation_filetime) is not int
        or creation_filetime <= 0
    ):
        raise AssertionError("popen_native_handle_pid_or_creation_identity_invalid")
    return parent_pid, process_id, popen_object_identity, creation_filetime


def _startup_process_identity_key(value: Mapping[str, Any]) -> tuple[int, int, str, int]:
    return _popen_process_identity_key({
        "parent_pid": value.get("parent_pid"),
        "pid": value.get("pid"),
        "popen_object_identity": value.get("parent_popen_object_identity"),
        "owned_handle_identity": value.get("process_creation_identity"),
    })


def _exit_process_identity_key(value: Mapping[str, Any]) -> tuple[int, int, str, int]:
    before = value.get("owned_handle_identity_before_observation")
    after = value.get("owned_handle_identity_after_observation")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping) or dict(before) != dict(after):
        raise AssertionError("popen_exit_handle_identity_changed_or_missing")
    row = dict(value)
    row["owned_handle_identity"] = before
    return _popen_process_identity_key(row)


def _external_stop_process_identity_key(value: Mapping[str, Any]) -> tuple[int, int, str, int]:
    expected = value.get("expected_owned_handle_identity")
    actual = value.get("actual_handle_identity_before_stop")
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        raise AssertionError("external_stop_native_identity_missing")
    if (
        actual.get("process_id_from_open_handle") != expected.get("process_id_from_owned_handle")
        or actual.get("creation_filetime") != expected.get("creation_filetime")
    ):
        raise AssertionError("external_stop_native_identity_does_not_match_popen_handle")
    row = dict(value)
    row["owned_handle_identity"] = expected
    return _popen_process_identity_key(row)


def _unique_process_identity_index(rows: list[Mapping[str, Any]], *, event: str) -> dict[tuple[int, int, str, int], dict[str, Any]]:
    index: dict[tuple[int, int, str, int], dict[str, Any]] = {}
    key_for_event = {
        "popen_returned": _popen_process_identity_key,
        "popen_exit_observed": _exit_process_identity_key,
        "external_verified_stop": _external_stop_process_identity_key,
        "python_process_startup": _startup_process_identity_key,
        "loaded_finagent_module": _startup_process_identity_key,
    }.get(event)
    if key_for_event is None:
        raise AssertionError(f"unsupported_process_identity_event:{event}")
    for value in rows:
        if not isinstance(value, Mapping) or value.get("event") not in {None, event}:
            raise AssertionError(f"process_identity_event_row_invalid:{event}")
        key = key_for_event(value)
        if key in index:
            raise AssertionError(f"duplicate_process_identity_event:{event}:{key}")
        index[key] = dict(value)
    return index


def _assert_popen_exit_pair(popen: Mapping[str, Any], exit_event: Mapping[str, Any]) -> tuple[int, int, str, int]:
    popen_key = _popen_process_identity_key(popen)
    exit_key = _exit_process_identity_key(exit_event)
    if popen_key != exit_key:
        raise AssertionError("popen_exit_process_identity_key_mismatch")
    for field in ("role", "parent_role", "parent_pid", "pid", "popen_object_identity", "parent_process_popen_object_identity", "parent_process_creation_identity"):
        if popen.get(field) != exit_event.get(field):
            raise AssertionError(f"popen_exit_context_mismatch:{field}")
    native = popen.get("owned_handle_identity")
    if (
        exit_event.get("event") != "popen_exit_observed"
        or exit_event.get("observed_via") not in {"poll", "wait"}
        or type(exit_event.get("returncode")) is not int
        or exit_event.get("handle_identity_same_before_after") is not True
        or exit_event.get("process_owner_is_parent_popen_object") is not True
        or not isinstance(native, Mapping)
        or exit_event.get("owned_handle_identity_before_observation") != native
        or exit_event.get("owned_handle_identity_after_observation") != native
    ):
        raise AssertionError("popen_exit_wait_or_handle_consistency_not_verified")
    return popen_key


def _assert_popen_external_stop_pair(popen: Mapping[str, Any], stop: Mapping[str, Any]) -> tuple[int, int, str, int]:
    popen_key = _popen_process_identity_key(popen)
    stop_key = _external_stop_process_identity_key(stop)
    if popen_key != stop_key:
        raise AssertionError("external_stop_process_identity_key_mismatch")
    for field in ("role", "parent_role", "parent_pid", "pid", "popen_object_identity", "argv", "cwd", "parent_process_popen_object_identity", "parent_process_creation_identity"):
        if popen.get(field) != stop.get(field):
            raise AssertionError(f"external_stop_context_mismatch:{field}")
    if (
        stop.get("identity_verified") is not True
        or stop.get("stop_observed") is not True
        or stop.get("stopped_by_verified_handle") is not True
        or stop.get("wait_result") != 0
        or type(stop.get("exit_code")) is not int
    ):
        raise AssertionError("external_stop_not_verified_on_owned_process_handle")
    return popen_key


def test_r4_popen_identity_pairing_rejects_reused_object_address_drift() -> None:
    """Offline positive/negative diagnostics for strong process identity joins."""

    def popen(parent_pid: int, process_id: int, creation_filetime: int, *, object_id: str = "0x9b47d50") -> dict[str, Any]:
        return {
            "event": "popen_returned",
            "role": "cli_owned_worker",
            "parent_role": "cli",
            "parent_pid": parent_pid,
            "pid": process_id,
            "popen_object_identity": object_id,
            "owned_handle_identity": {"process_id_from_owned_handle": process_id, "creation_filetime": creation_filetime},
            "argv": ["python.exe", "-m", "finagent.llm.chat_worker"],
            "cwd": "C:/frozen/project",
            "parent_process_popen_object_identity": "0xparentcli",
            "parent_process_creation_identity": {"process_id": parent_pid, "creation_filetime": parent_pid * 1000},
            "process_owner_is_parent_popen_object": True,
        }

    def exit_event(row: Mapping[str, Any]) -> dict[str, Any]:
        native = dict(row["owned_handle_identity"])
        return {
            "event": "popen_exit_observed",
            "role": row["role"],
            "parent_role": row["parent_role"],
            "parent_pid": row["parent_pid"],
            "pid": row["pid"],
            "popen_object_identity": row["popen_object_identity"],
            "parent_process_popen_object_identity": row["parent_process_popen_object_identity"],
            "parent_process_creation_identity": row["parent_process_creation_identity"],
            "owned_handle_identity_before_observation": native,
            "owned_handle_identity_after_observation": native,
            "handle_identity_same_before_after": True,
            "process_owner_is_parent_popen_object": True,
            "observed_via": "wait",
            "returncode": 0,
        }

    same_parent_first = popen(60072, 61276, 134360331781704090)
    same_parent_reused_object = popen(60072, 58300, 134360331792593070)
    same_pid_reused_object = popen(60072, 61276, 134360331792593070)
    cross_parent_reused_object = popen(14916, 61276, 134360331781704090)
    same_parent_index = _unique_process_identity_index(
        [exit_event(same_parent_first), exit_event(same_parent_reused_object), exit_event(same_pid_reused_object)],
        event="popen_exit_observed",
    )
    cross_parent_index = _unique_process_identity_index(
        [exit_event(same_parent_first), exit_event(cross_parent_reused_object)],
        event="popen_exit_observed",
    )
    if len(same_parent_index) != 3 or len(cross_parent_index) != 2:
        raise AssertionError("popen_identity_positive_reuse_cases_collapsed")
    for returned, observed in (
        (same_parent_first, exit_event(same_parent_first)),
        (same_parent_reused_object, exit_event(same_parent_reused_object)),
        (same_pid_reused_object, exit_event(same_pid_reused_object)),
        (cross_parent_reused_object, exit_event(cross_parent_reused_object)),
    ):
        _assert_popen_exit_pair(returned, observed)

    def external_stop(row: Mapping[str, Any]) -> dict[str, Any]:
        native = dict(row["owned_handle_identity"])
        return {
            "role": row["role"],
            "parent_role": row["parent_role"],
            "parent_pid": row["parent_pid"],
            "pid": row["pid"],
            "popen_object_identity": row["popen_object_identity"],
            "argv": list(row["argv"]),
            "cwd": row["cwd"],
            "parent_process_popen_object_identity": row["parent_process_popen_object_identity"],
            "parent_process_creation_identity": row["parent_process_creation_identity"],
            "expected_owned_handle_identity": native,
            "actual_handle_identity_before_stop": {
                "process_id_from_open_handle": row["pid"],
                "creation_filetime": native["creation_filetime"],
            },
            "identity_verified": True,
            "stop_observed": True,
            "stopped_by_verified_handle": True,
            "wait_result": 0,
            "exit_code": 0,
        }

    for returned in (same_parent_first, same_parent_reused_object, same_pid_reused_object):
        _assert_popen_external_stop_pair(returned, external_stop(returned))

    def must_reject(label: str, returned: Mapping[str, Any], observed: Mapping[str, Any]) -> None:
        try:
            _assert_popen_exit_pair(returned, observed)
        except AssertionError:
            return
        raise AssertionError(f"popen_identity_negative_drift_accepted:{label}")

    valid_exit = exit_event(same_parent_first)
    drifted = dict(valid_exit)
    drifted["parent_pid"] += 1
    must_reject("parent_pid", same_parent_first, drifted)
    drifted = dict(valid_exit)
    drifted["pid"] += 1
    must_reject("child_pid", same_parent_first, drifted)
    drifted = dict(valid_exit)
    drifted["owned_handle_identity_before_observation"] = {**valid_exit["owned_handle_identity_before_observation"], "creation_filetime": valid_exit["owned_handle_identity_before_observation"]["creation_filetime"] + 1}
    drifted["owned_handle_identity_after_observation"] = dict(drifted["owned_handle_identity_before_observation"])
    must_reject("creation_filetime", same_parent_first, drifted)
    drifted = dict(valid_exit)
    drifted["popen_object_identity"] = "0xother"
    must_reject("popen_object_identity", same_parent_first, drifted)
    drifted = dict(valid_exit)
    drifted["role"] = "api_owned_cli"
    must_reject("role", same_parent_first, drifted)
    valid_stop = external_stop(same_parent_first)
    drifted_stop = dict(valid_stop)
    drifted_stop["parent_pid"] += 1
    try:
        _assert_popen_external_stop_pair(same_parent_first, drifted_stop)
    except AssertionError:
        pass
    else:
        raise AssertionError("external_stop_parent_pid_drift_accepted")
    drifted_stop = dict(valid_stop)
    drifted_stop["actual_handle_identity_before_stop"] = {
        **valid_stop["actual_handle_identity_before_stop"],
        "creation_filetime": valid_stop["actual_handle_identity_before_stop"]["creation_filetime"] + 1,
    }
    try:
        _assert_popen_external_stop_pair(same_parent_first, drifted_stop)
    except AssertionError:
        pass
    else:
        raise AssertionError("external_stop_native_creation_drift_accepted")
    drifted_stop = dict(valid_stop)
    drifted_stop["cwd"] += "/different"
    try:
        _assert_popen_external_stop_pair(same_parent_first, drifted_stop)
    except AssertionError:
        pass
    else:
        raise AssertionError("external_stop_cwd_drift_accepted")
    duplicate_rejected = False
    try:
        _unique_process_identity_index([valid_exit, valid_exit], event="popen_exit_observed")
    except AssertionError:
        duplicate_rejected = True
    if not duplicate_rejected:
        raise AssertionError("duplicate_exit_for_full_process_identity_was_accepted")


def test_r4_cli_run_mapping_accepts_pid_reuse_only_with_distinct_creation_identity() -> None:
    """Offline run mapping accepts sequential PID reuse while rejecting aliases."""

    def case_for(run_id: str, creation_filetime: int) -> tuple[dict[str, Any], dict[str, Any]]:
        argv = [
            "python.exe", "scripts/analyze_annual.py", "--with-model",
            "--run-id", run_id, "--document-id", f"doc-{run_id}",
        ]
        popen = {
            "event": "popen_returned",
            "role": "api_owned_cli",
            "parent_role": "api",
            "parent_pid": 60072,
            "pid": 61276,
            "popen_object_identity": "0x9b47d50",
            "owned_handle_identity": {
                "process_id_from_owned_handle": 61276,
                "creation_filetime": creation_filetime,
            },
            "argv": argv,
            "cwd": "C:/frozen/project",
            "document_id": f"doc-{run_id}",
        }
        capture = {
            "process_id": 61276,
            "parent_pid": 60072,
            "parent_popen_object_identity": "0x9b47d50",
            "process_creation_identity": {
                "process_id": 61276,
                "creation_filetime": creation_filetime,
            },
            "argv": argv[1:],
            "cwd": "C:/frozen/project",
        }
        case = {
            "run_id": run_id,
            "manifest": {"run_id": run_id},
            "document_id": f"doc-{run_id}",
            "source_pdf_sha256": "a" * 64,
            "runtime_loaded_identity": {
                "cli_actual_process_identity": {"role": "cli", "process_id": 61276},
            },
            "cli_popen_owned_identity": [{"popen_returned": popen}],
            "actual_cli_runtime_capture": [{"profile_capture": capture}],
        }
        return case, popen

    first, first_popen = case_for("run-a", 134360331781704090)
    second, second_popen = case_for("run-b", 134360331792593070)
    accepted = _assert_exact_case_cli_run_mapping(
        {"first": first, "second": second},
        [first_popen, second_popen],
    )
    if len(accepted) != 2 or accepted["run-a"]["process_identity_key"] == accepted["run-b"]["process_identity_key"]:
        raise AssertionError("sequential_pid_reuse_with_distinct_creation_was_not_preserved")

    bad_creation_case = json.loads(json.dumps(second))
    bad_creation_case["actual_cli_runtime_capture"][0]["profile_capture"]["process_creation_identity"]["creation_filetime"] += 1
    try:
        _assert_exact_case_cli_run_mapping(
            {"first": first, "second": bad_creation_case},
            [first_popen, second_popen],
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("run_mapping_accepted_profile_with_drifted_native_creation")

    reused_true_identity_case, reused_true_identity_popen = case_for("run-b", 134360331781704090)
    try:
        _assert_exact_case_cli_run_mapping(
            {"first": first, "second": reused_true_identity_case},
            [first_popen, reused_true_identity_popen],
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("one_full_cli_process_identity_was_allowed_to_claim_multiple_runs")


def _assert_exact_case_cli_run_mapping(cases: Mapping[str, Any], cli_popen: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Bind exactly one real business CLI Popen/profile identity to every run."""

    if not isinstance(cases, Mapping) or not cases:
        raise AssertionError("case_run_identity_mapping_missing")
    expected_by_run: dict[str, dict[str, Any]] = {}
    identity_run_by_key: dict[tuple[int, int, str, int], str] = {}
    for case_name, case in cases.items():
        if not isinstance(case_name, str) or not case_name or not isinstance(case, Mapping):
            raise AssertionError("case_run_identity_mapping_invalid")
        run_id = case.get("run_id")
        manifest = case.get("manifest")
        identity = case.get("runtime_loaded_identity")
        actual_identity = identity.get("cli_actual_process_identity") if isinstance(identity, Mapping) else None
        process_id = actual_identity.get("process_id") if isinstance(actual_identity, Mapping) else None
        pairs = case.get("cli_popen_owned_identity")
        if (
            not isinstance(run_id, str)
            or not run_id
            or run_id.strip() != run_id
            or not isinstance(manifest, Mapping)
            or manifest.get("run_id") != run_id
            or not isinstance(actual_identity, Mapping)
            or actual_identity.get("role") != "cli"
            or type(process_id) is not int
            or process_id <= 0
            or not isinstance(pairs, list)
            or len(pairs) != 1
            or not isinstance(pairs[0], Mapping)
        ):
            raise AssertionError(f"case_run_manifest_or_actual_cli_identity_missing:{case_name}")
        popen = pairs[0].get("popen_returned")
        if (
            not isinstance(popen, Mapping)
            or popen.get("event") != "popen_returned"
            or popen.get("role") != "api_owned_cli"
            or popen.get("pid") != process_id
        ):
            raise AssertionError(f"case_cli_profile_not_bound_to_actual_api_popen:{case_name}")
        identity_key = _popen_process_identity_key(popen)
        capture_bundle = case.get("actual_cli_runtime_capture")
        if isinstance(capture_bundle, list) and len(capture_bundle) == 1 and isinstance(capture_bundle[0], Mapping):
            profile_capture = capture_bundle[0].get("profile_capture")
        elif isinstance(capture_bundle, Mapping):
            profile_capture = capture_bundle.get("profile_capture")
        else:
            profile_capture = None
        if not isinstance(profile_capture, Mapping):
            raise AssertionError(f"case_cli_profile_capture_missing:{case_name}")
        capture_key = _popen_process_identity_key({
            "parent_pid": profile_capture.get("parent_pid"),
            "pid": profile_capture.get("process_id"),
            "popen_object_identity": profile_capture.get("parent_popen_object_identity"),
            "owned_handle_identity": profile_capture.get("process_creation_identity"),
        })
        argv = popen.get("argv")
        if (
            identity_key != capture_key
            or not isinstance(argv, list)
            or not all(isinstance(item, str) for item in argv)
            or profile_capture.get("argv") != argv[1:]
            or profile_capture.get("cwd") != popen.get("cwd")
            or process_id != profile_capture.get("process_id")
            or actual_identity.get("process_id") != process_id
        ):
            raise AssertionError(f"case_cli_profile_capture_not_bound_to_native_popen_identity:{case_name}")
        run_id_flags = [index for index, item in enumerate(argv) if item == "--run-id"]
        inline_run_id_flags = [item for item in argv if item.startswith("--run-id=")]
        if (
            len(run_id_flags) != 1
            or run_id_flags[0] + 1 >= len(argv)
            or argv[run_id_flags[0] + 1] != run_id
            or inline_run_id_flags
        ):
            raise AssertionError(f"case_cli_popen_run_id_does_not_match_manifest:{case_name}")
        document_id = case.get("document_id")
        if not isinstance(document_id, str) or not document_id or popen.get("document_id") != document_id:
            raise AssertionError(f"case_cli_popen_document_binding_mismatch:{case_name}")
        row = {
            "run_id": run_id,
            "process_id": process_id,
            "popen_object_identity": popen["popen_object_identity"],
            "process_identity_key": list(identity_key),
            "argv": list(argv),
            "cwd": popen.get("cwd"),
            "document_id": document_id,
            "source_pdf_sha256": case.get("source_pdf_sha256"),
        }
        previous = expected_by_run.get(run_id)
        if previous is not None and previous != row:
            raise AssertionError(f"case_alias_run_identity_conflict:{run_id}:{case_name}")
        expected_by_run[run_id] = row
        previous_identity_run = identity_run_by_key.get(identity_key)
        if previous_identity_run is not None and previous_identity_run != run_id:
            raise AssertionError(f"one_true_cli_process_identity_claimed_multiple_runs:{identity_key}")
        identity_run_by_key[identity_key] = run_id
    if not expected_by_run:
        raise AssertionError("case_run_identity_mapping_empty")

    actual_by_run: dict[str, dict[str, Any]] = {}
    actual_identity_run_by_key: dict[tuple[int, int, str, int], str] = {}
    for popen in cli_popen:
        if not isinstance(popen, Mapping) or popen.get("event") != "popen_returned" or popen.get("role") != "api_owned_cli":
            raise AssertionError("business_cli_popen_identity_row_invalid")
        identity_key = _popen_process_identity_key(popen)
        argv = popen.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise AssertionError("business_cli_popen_argv_invalid")
        run_id_flags = [index for index, item in enumerate(argv) if item == "--run-id"]
        inline_run_id_flags = [item for item in argv if item.startswith("--run-id=")]
        if len(run_id_flags) != 1 or run_id_flags[0] + 1 >= len(argv) or inline_run_id_flags:
            raise AssertionError("business_cli_popen_run_id_argument_invalid")
        run_id = argv[run_id_flags[0] + 1]
        process_id = popen.get("pid")
        if not isinstance(run_id, str) or not run_id or type(process_id) is not int or process_id <= 0:
            raise AssertionError("business_cli_popen_run_or_handle_identity_invalid")
        if run_id in actual_by_run:
            raise AssertionError(f"duplicate_business_cli_popen_for_run:{run_id}")
        previous_identity_run = actual_identity_run_by_key.get(identity_key)
        if previous_identity_run is not None and previous_identity_run != run_id:
            raise AssertionError(f"one_true_business_cli_popen_identity_claimed_multiple_runs:{identity_key}")
        actual_identity_run_by_key[identity_key] = run_id
        actual_by_run[run_id] = {
            "run_id": run_id,
            "process_id": process_id,
            "popen_object_identity": popen.get("popen_object_identity"),
            "process_identity_key": list(identity_key),
            "argv": list(argv),
            "cwd": popen.get("cwd"),
            "document_id": popen.get("document_id"),
        }
    if len(cli_popen) != len(expected_by_run) or set(actual_by_run) != set(expected_by_run):
        raise AssertionError("business_cli_popen_run_set_or_count_does_not_match_cases")
    for run_id, expected in expected_by_run.items():
        actual = actual_by_run[run_id]
        if any(actual[key] != expected[key] for key in ("process_id", "popen_object_identity", "process_identity_key", "argv", "cwd", "document_id")):
            raise AssertionError(f"business_cli_popen_manifest_profile_identity_mismatch:{run_id}")
    return expected_by_run


def _write_popen_process_identity(popen_events: Path, evidence_root: Path, cases: Mapping[str, Any], api_identity: Mapping[str, Any], source_manifest: Mapping[str, Any]) -> None:
    rows = _read_jsonl(popen_events)
    returned = [item for item in rows if item.get("event") == "popen_returned"]
    exits = [item for item in rows if item.get("event") == "popen_exit_observed"]
    startups = [item for item in rows if item.get("event") == "python_process_startup"]
    loaded_modules = [item for item in rows if item.get("event") == "loaded_finagent_module"]
    api_runtime = api_identity.get("api") if isinstance(api_identity.get("api"), Mapping) else {}
    api_process_id = api_runtime.get("process_id")
    source_sha = api_runtime.get("source_manifest_sha256")
    api_start_record = _read_json(evidence_root / "api-process-start.json")
    source_root = Path(str(api_start_record.get("cwd", ""))).resolve()
    api_start_popen = {
        "parent_pid": api_start_record.get("parent_pid"),
        "pid": api_start_record.get("pid"),
        "popen_object_identity": api_start_record.get("popen_object_identity"),
        "owned_handle_identity": api_start_record.get("owned_handle_identity"),
    }
    api_key = _popen_process_identity_key(api_start_popen)
    if (
        type(api_process_id) is not int
        or api_start_record.get("pid") != api_process_id
        or api_start_record.get("process_owner_is_harness_popen_object") is not True
        or api_start_record.get("role") != "api"
        or api_start_record.get("parent_role") != "harness"
        or api_start_record.get("parent_pid") != api_key[0]
        or api_key[1] != api_process_id
        or api_start_record.get("runtime_source_manifest_sha256") != source_manifest.get("sha256")
        or source_sha != source_manifest.get("sha256")
        or not source_root.is_dir()
    ):
        raise AssertionError("actual_api_popen_launch_identity_or_frozen_source_missing")
    startup_by_identity = _unique_process_identity_index(startups, event="python_process_startup")
    api_startup = startup_by_identity.get(api_key)
    api_creation = api_start_record.get("process_creation")
    if (
        not isinstance(api_startup, Mapping)
        or not isinstance(api_creation, Mapping)
        or api_creation.get("parent_pid") != api_start_record.get("parent_pid")
        or api_startup.get("role") != "api"
        or api_startup.get("parent_role") != "harness"
        or api_startup.get("parent_popen_object_identity") != api_start_record.get("popen_object_identity")
        or api_startup.get("parent_pid_from_parent_popen") != api_start_record.get("parent_pid")
        or api_startup.get("original_argv") != api_start_record.get("argv")
        or api_startup.get("sitecustomize_argv") != api_startup.get("argv")
        or api_startup.get("cwd") != str(source_root)
        or api_startup.get("source_root") != str(source_root)
        or api_startup.get("source_manifest_sha256") != source_sha
        or _observer_role_for_argv(api_startup.get("original_argv"), api_startup.get("cwd", ""), source_root) != "api"
    ):
        raise AssertionError("actual_api_startup_identity_argv_cwd_parent_or_role_mismatch")
    api_startup_native = api_startup.get("process_creation_identity")
    if (
        not isinstance(api_startup_native, Mapping)
        or api_startup_native.get("process_id") != api_process_id
        or api_startup_native.get("creation_filetime") != api_key[3]
        or api_runtime.get("process_id") != api_key[1]
    ):
        raise AssertionError("actual_api_startup_creation_differs_from_owned_popen_handle")

    returned_by_identity = _unique_process_identity_index(returned, event="popen_returned")
    exit_by_identity = _unique_process_identity_index(exits, event="popen_exit_observed")
    cli_popen = [item for item in returned if item.get("role") == "api_owned_cli"]
    probe_popen = [item for item in returned if item.get("role") == "api_runtime_identity_probe"]
    worker_popen = [item for item in returned if item.get("role") == "cli_owned_worker"]
    if not cli_popen or not worker_popen:
        raise AssertionError("actual_api_cli_worker_popen_chain_missing")
    expected_cli_by_run = _assert_exact_case_cli_run_mapping(cases, cli_popen)
    cli_by_process_identity = {_popen_process_identity_key(item): item for item in cli_popen}

    for item in startups:
        identity_key = _startup_process_identity_key(item)
        original_argv = item.get("original_argv")
        sitecustomize_argv = item.get("sitecustomize_argv")
        cwd = item.get("cwd")
        item_source_root = item.get("source_root")
        process_creation = item.get("process_creation_identity")
        if (
            not isinstance(original_argv, list)
            or not isinstance(sitecustomize_argv, list)
            or item.get("argv") != sitecustomize_argv
            or not isinstance(cwd, str)
            or not isinstance(item_source_root, str)
            or Path(item_source_root).resolve() != source_root
            or item.get("role") != _observer_role_for_argv(original_argv, cwd, source_root)
            or item.get("source_manifest_sha256") != source_sha
            or not isinstance(process_creation, Mapping)
            or process_creation.get("process_id") != identity_key[1]
        ):
            raise AssertionError(f"python_startup_identity_argv_role_or_source_mismatch:{item.get('pid')}:{identity_key}")

    owner_case = cases.get("owner_hard_exit")
    owner_details = owner_case.get("owner_hard_exit") if isinstance(owner_case, Mapping) else None
    external_stops = owner_details.get("external_verified_child_stops", []) if isinstance(owner_details, Mapping) else []
    if not isinstance(external_stops, list):
        raise AssertionError("owner_hard_exit_external_stop_list_invalid")
    external_stop_by_identity = _unique_process_identity_index(external_stops, event="external_verified_stop") if external_stops else {}

    expected_hashes = {
        str(item.get("path")): str(item.get("sha256"))
        for item in source_manifest.get("files", [])
        if isinstance(item, Mapping) and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str)
    }
    source_module_rows: dict[str, list[dict[str, Any]]] = {}
    loaded_by_identity: dict[tuple[int, int, str, int], list[dict[str, Any]]] = {}
    for item in loaded_modules:
        role = str(item.get("role", ""))
        identity_key = _startup_process_identity_key(item)
        startup = startup_by_identity.get(identity_key)
        if (
            not isinstance(startup, Mapping)
            or startup.get("role") != role
            or startup.get("parent_pid") != item.get("parent_pid")
            or startup.get("parent_role") != item.get("parent_role")
            or startup.get("parent_popen_object_identity") != item.get("parent_popen_object_identity")
            or startup.get("process_creation_identity") != item.get("process_creation_identity")
            or item.get("source_manifest_sha256") != source_sha
        ):
            raise AssertionError(f"loaded_module_identity_not_bound_to_startup:{role}:{item.get('module')}:{identity_key}")
        source_path = str(item.get("source_path", "")).replace("\\", "/")
        manifest_path = next((path for path in expected_hashes if source_path.endswith("/" + path)), None)
        if manifest_path is None or item.get("source_sha256") != expected_hashes[manifest_path]:
            raise AssertionError(f"actual_loaded_module_differs_from_frozen_manifest:{role}:{item.get('module')}")
        source_module_rows.setdefault(role, []).append({**item, "manifest_path": manifest_path, "frozen_sha256": expected_hashes[manifest_path], "source_match": True})
        loaded_by_identity.setdefault(identity_key, []).append(item)
    for role in ("api", "cli", "worker"):
        if not source_module_rows.get(role):
            raise AssertionError(f"actual_{role}_loaded_module_hashes_not_observed")

    identities = []
    for identity_key, item in returned_by_identity.items():
        native = item.get("owned_handle_identity")
        startup = startup_by_identity.get(identity_key)
        exit_event = exit_by_identity.get(identity_key)
        external_stop = external_stop_by_identity.get(identity_key)
        if not isinstance(startup, Mapping):
            raise AssertionError(f"captured_popen_has_no_unique_startup_for_full_identity:{item.get('role')}:{identity_key}")
        if (
            item.get("parent_role") not in {"api", "cli"}
            or item.get("parent_pid") != startup.get("parent_pid")
            or item.get("parent_role") != startup.get("parent_role")
            or item.get("popen_object_identity") != startup.get("parent_popen_object_identity")
            or item.get("expected_child_startup_role") != startup.get("role")
            or item.get("argv") != startup.get("original_argv")
            or item.get("cwd") != startup.get("cwd")
            or item.get("parent_pid") != startup.get("parent_pid_from_parent_popen")
        ):
            raise AssertionError(f"captured_popen_and_startup_full_identity_disagree:{item.get('role')}:{identity_key}")
        if _observer_role_for_argv(item.get("argv"), item.get("cwd", ""), source_root) != item.get("expected_child_startup_role"):
            raise AssertionError(f"captured_popen_argv_has_wrong_exact_process_role:{item.get('role')}:{identity_key}")

        parent_creation = item.get("parent_process_creation_identity")
        parent_creation_pid = parent_creation.get("process_id") if isinstance(parent_creation, Mapping) else None
        parent_popen_object = item.get("parent_process_popen_object_identity")
        if (
            not isinstance(parent_creation, Mapping)
            or parent_creation_pid != item.get("parent_pid")
            or type(parent_creation.get("creation_filetime")) is not int
            or parent_creation.get("creation_filetime", 0) <= 0
            or not isinstance(parent_popen_object, str)
            or not parent_popen_object
        ):
            raise AssertionError(f"captured_popen_parent_native_identity_missing:{item.get('role')}:{identity_key}")
        if item.get("parent_role") == "api":
            if (
                item.get("parent_pid") != api_process_id
                or parent_popen_object != api_start_record.get("popen_object_identity")
                or dict(parent_creation) != dict(api_startup_native)
            ):
                raise AssertionError(f"api_owned_child_parent_full_identity_mismatch:{item.get('role')}:{identity_key}")
        else:
            parent_key = (
                api_process_id,
                item.get("parent_pid"),
                parent_popen_object,
                parent_creation.get("creation_filetime"),
            )
            parent_cli = cli_by_process_identity.get(parent_key)
            parent_native = parent_cli.get("owned_handle_identity") if isinstance(parent_cli, Mapping) else None
            if (
                not isinstance(parent_cli, Mapping)
                or not isinstance(parent_native, Mapping)
                or parent_native.get("process_id_from_owned_handle") != item.get("parent_pid")
                or parent_native.get("creation_filetime") != parent_creation.get("creation_filetime")
                or parent_cli.get("role") != "api_owned_cli"
            ):
                raise AssertionError(f"worker_parent_is_not_exact_business_cli_identity:{identity_key}")

        if item.get("role") == "api_owned_cli" and "--with-model" not in item.get("argv", []):
            raise AssertionError("business_cli_popen_missing_explicit_model_mode")
        if item.get("role") == "api_runtime_identity_probe" and (
            "--runtime-identity" not in item.get("argv", []) or "--with-model" in item.get("argv", [])
        ):
            raise AssertionError("runtime_identity_probe_popen_command_is_not_exact_probe")
        if item.get("role") == "api_runtime_identity_probe" and (
            "--run-id" in item.get("argv", []) or any(str(value).startswith("--run-id=") for value in item.get("argv", []))
        ):
            raise AssertionError("runtime_identity_probe_must_not_claim_business_run_id")
        if item.get("role") == "cli_owned_worker" and item.get("argv", [])[1:3] != ["-m", "finagent.llm.chat_worker"]:
            raise AssertionError("chat_worker_exact_module_mismatch")

        if exit_event is not None:
            _assert_popen_exit_pair(item, exit_event)
        if external_stop is not None:
            _assert_popen_external_stop_pair(item, external_stop)
        if exit_event is not None and external_stop is not None and external_stop.get("exit_code") != exit_event.get("returncode"):
            raise AssertionError(f"popen_exit_and_external_stop_exit_code_conflict:{identity_key}")
        native_valid = (
            isinstance(native, Mapping)
            and native.get("process_id_from_owned_handle") == item.get("pid")
            and type(native.get("creation_filetime")) is int
            and native.get("creation_filetime", 0) > 0
            and item.get("process_owner_is_parent_popen_object") is True
        )
        stop_or_exit_verified = exit_event is not None or external_stop is not None
        if not native_valid or not stop_or_exit_verified:
            raise AssertionError(f"popen_owned_process_identity_or_unique_exit_not_verified:{item.get('role')}:{identity_key}")
        identities.append({
            **item,
            "process_identity_key": list(identity_key),
            "popen_identity_verified": True,
            "paired_exit": exit_event,
            "external_verified_stop": external_stop,
        })

    returned_identity_keys = set(returned_by_identity)
    if set(exit_by_identity) - returned_identity_keys:
        raise AssertionError("unmatched_popen_exit_observation_for_unknown_identity")
    if set(external_stop_by_identity) - returned_identity_keys:
        raise AssertionError("unmatched_external_stop_for_unknown_popen_identity")

    expected_role_by_popen = {
        "api_owned_cli": "cli",
        "api_runtime_identity_probe": "cli",
        "cli_owned_worker": "worker",
    }
    for item in returned:
        expected_child_role = expected_role_by_popen.get(str(item.get("role")))
        if expected_child_role is None or item.get("expected_child_startup_role") != expected_child_role:
            raise AssertionError(f"unexpected_captured_popen_role:{item.get('role')}")
        identity_key = _popen_process_identity_key(item)
        startup = startup_by_identity.get(identity_key)
        if not isinstance(startup, Mapping) or startup.get("role") != expected_child_role:
            raise AssertionError(f"captured_popen_has_no_unique_python_startup:{item.get('role')}:{identity_key}")
        if item.get("role") == "api_owned_cli" and item.get("document_id") is None:
            raise AssertionError("business_cli_popen_document_binding_missing")
        if item.get("role") == "cli_owned_worker" and not any(
            module.get("module") == "finagent.llm.chat_worker"
            for module in loaded_by_identity.get(identity_key, [])
        ):
            raise AssertionError("chat_worker_loaded_module_identity_missing")

    actual_cli_keys = {tuple(row["process_identity_key"]) for row in expected_cli_by_run.values()}
    expected_actual_profile_keys = set()
    for case in cases.values():
        capture_bundle = case.get("actual_cli_runtime_capture") if isinstance(case, Mapping) else None
        if isinstance(capture_bundle, list) and len(capture_bundle) == 1 and isinstance(capture_bundle[0], Mapping):
            profile_capture = capture_bundle[0].get("profile_capture")
        elif isinstance(capture_bundle, Mapping):
            profile_capture = capture_bundle.get("profile_capture")
        else:
            profile_capture = None
        if not isinstance(profile_capture, Mapping):
            raise AssertionError("case_actual_cli_profile_capture_missing_for_final_mapping")
        expected_actual_profile_keys.add(_popen_process_identity_key({
            "parent_pid": profile_capture.get("parent_pid"),
            "pid": profile_capture.get("process_id"),
            "popen_object_identity": profile_capture.get("parent_popen_object_identity"),
            "owned_handle_identity": profile_capture.get("process_creation_identity"),
        }))
    if actual_cli_keys != expected_actual_profile_keys or actual_cli_keys != {
        _popen_process_identity_key(item) for item in cli_popen
    }:
        raise AssertionError("case_profile_and_actual_cli_popen_full_identity_sets_differ")

    _write_json_new(evidence_root / "popen-identities-verified.json", {
        "api_process_identity_key": list(api_key),
        "api_process_id": api_process_id,
        "api_popen_start_record": api_start_record,
        "api_startup_identity": api_startup,
        "source_manifest_sha256": source_sha,
        "popen_owned_processes": identities,
        "python_process_startups": startups,
        "loaded_finagent_modules": source_module_rows,
        "actual_cli_runs_from_cases": expected_cli_by_run,
        "actual_cli_process_identity_keys_from_profile_captures": sorted([list(key) for key in expected_actual_profile_keys]),
        "actual_cli_process_identity_keys_from_api_popen": sorted([list(key) for key in actual_cli_keys]),
        "actual_cli_run_ids_from_api_popen": sorted(item["run_id"] for item in expected_cli_by_run.values()),
        "actual_api_runtime_identity_probe_process_ids": sorted(int(item["pid"]) for item in probe_popen),
        "actual_worker_process_identity_keys": sorted([list(_popen_process_identity_key(item)) for item in worker_popen]),
    })


def _capture_job_ledger(database: Path, run_id: str, target: Path) -> dict[str, Any]:
    if not run_id:
        raise AssertionError("job_ledger_run_id_missing")
    value = _capture_ledger_read_only(database, target, run_id=run_id)
    value["run_id"] = run_id
    value["gate_keys"] = sorted({str(item["gate_key"]) for item in value["attempt_rows"] if item.get("gate_key")})
    value["gate_stage_attempts"] = dict(value["run_stage_counts"])
    return value


def _capture_ledger_read_only(database: Path, target: Path, *, run_id: str | None = None) -> dict[str, Any]:
    target.mkdir(parents=True, exist_ok=False)
    snapshot = target / "provider-call-ledger.sqlite3"
    source_uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=1) as source:
        source.execute("PRAGMA query_only=ON")
        with sqlite3.connect(snapshot) as destination:
            source.backup(destination)
        total = source.execute("SELECT COUNT(*) FROM provider_attempts WHERE goal_counted=1").fetchone()[0]
        grouped = source.execute("SELECT stage, COUNT(*) FROM provider_attempts WHERE goal_counted=1 GROUP BY stage").fetchall()
        rows = source.execute("SELECT admission_id, gate_key, run_id, stage, batch_index, audit_id, status, goal_counted, source_sha256 FROM provider_attempts ORDER BY admitted_at, admission_id").fetchall()
        gates = source.execute("SELECT gate_key, closed FROM call_gates ORDER BY gate_key").fetchall()
    all_attempt_rows = [dict(zip(("admission_id", "gate_key", "run_id", "stage", "batch_index", "audit_id", "status", "goal_counted", "source_sha256"), row)) for row in rows]
    attempt_rows = [item for item in all_attempt_rows if run_id is None or item.get("run_id") == run_id]
    run_grouped: Counter[str] = Counter(str(item["stage"]) for item in attempt_rows if item.get("goal_counted") == 1)
    gate_keys = {str(item["gate_key"]) for item in attempt_rows if item.get("gate_key")}
    relevant_gates = [{"gate_key": row[0], "closed": bool(row[1])} for row in gates if row[0] in gate_keys]
    result = {
        "database": str(database),
        "database_snapshot": str(snapshot),
        "database_sha256": _sha256(database),
        "snapshot_sha256": _sha256(snapshot),
        "reads_are_read_only": True,
        "goal_attempt_count": int(total),
        "goal_stage_counts": {str(stage): int(count) for stage, count in grouped},
        "run_id": run_id,
        "run_attempt_count": len(attempt_rows),
        "run_stage_counts": dict(run_grouped),
        "gate_stage_counts": dict(run_grouped),
        "gate_rows": relevant_gates,
        "all_gate_rows": [{"gate_key": row[0], "closed": bool(row[1])} for row in gates],
        "attempt_rows": attempt_rows,
    }
    _write_json_new(target / "read-only-counts.json", {key: result[key] for key in ("database_sha256", "snapshot_sha256", "reads_are_read_only", "goal_attempt_count", "goal_stage_counts", "run_id", "run_attempt_count", "run_stage_counts", "gate_rows", "all_gate_rows")})
    return result


def _hash_run_and_report(root: Path, run_id: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for folder in (root / "artifacts" / "runs" / run_id, root / "artifacts" / "reports" / run_id):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                hashes[path.relative_to(root).as_posix()] = _sha256(path)
    return hashes


def _hash_owner_state(root: Path, job_id: str, run_id: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    folders = (
        root / "artifacts" / "annual-analysis-jobs" / job_id,
        root / "artifacts" / "runs" / run_id,
        root / "artifacts" / "reports" / run_id,
    )
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                hashes[path.relative_to(root).as_posix()] = _sha256(path)
    return hashes


def _tree_file_hashes(root: Path) -> list[dict[str, Any]]:
    return [{"relative_path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)} for path in sorted(root.rglob("*")) if path.is_file()]


def _provider_fingerprint(base_url: str, model: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(base_url)
    material = json.dumps({"scheme": parts.scheme.lower(), "host": (parts.hostname or "").lower(), "port": parts.port, "path": parts.path.rstrip("/"), "model": model}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _successful_audits(value: Mapping[str, Any]) -> bool:
    rows = value.get("audit_artifacts")
    if isinstance(rows, list) and any(isinstance(item, Mapping) and item.get("status") == "succeeded" for item in rows):
        return True
    audit = value.get("audit")
    return isinstance(audit, Mapping) and audit.get("status") == "succeeded"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    observer_errors = sorted(path.parent.glob(path.name + ".observer-error-*.json"))
    if observer_errors:
        details = []
        for marker_path in observer_errors:
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                details.append(f"{marker_path.name}:{type(exc).__name__}")
            else:
                details.append(f"{marker_path.name}:{marker.get('error_type', 'unknown')}")
        raise AssertionError("popen_observer_append_failed:" + ";".join(details))
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_new(path: Path, value: object) -> None:
    _write_bytes_new(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _write_bytes_new(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _LoopbackProvider:
    """Record exact HTTP frames and respond only from each decoded request."""

    def __init__(self, codec: Any) -> None:
        self.codec = codec
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._raw_lines: list[bytes] = []
        self._behaviors: dict[str, dict[str, Any]] = {}
        self._stage_hits: Counter[tuple[str, str, str]] = Counter()
        self._blocked: dict[int, threading.Event] = {}
        self._release: dict[int, threading.Event] = {}
        self._active = 0
        self._peak = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    request = json.loads(raw.decode("utf-8"))
                    stage, user_payload, wire_evidence = owner._classify(request, raw)
                    document_id = _find_document_id(user_payload)
                except Exception:
                    self.send_error(400)
                    return
                with owner._lock:
                    sequence = len(owner._events) + 1
                    owner._active += 1
                    owner._peak = max(owner._peak, owner._active)
                    digest = hashlib.sha256(raw).hexdigest()
                    owner._stage_hits[(str(document_id), stage, digest)] += 1
                    event: dict[str, Any] = {
                        "sequence": sequence,
                        "stage": stage,
                        "document_id": document_id,
                        "batch_index": user_payload.get("batch_index") if isinstance(user_payload, Mapping) else None,
                        "peer": self.client_address[0],
                        "auth_is_dummy": self.headers.get("Authorization") == f"Bearer {_DUMMY_KEY}",
                        "request_monotonic": time.monotonic(),
                        "request_body_sha256": digest,
                        "request_body_bytes": len(raw),
                        "request_body_base64": base64.b64encode(raw).decode("ascii"),
                        "request_body_utf8": raw.decode("utf-8"),
                        **wire_evidence,
                        "model": request.get("model"),
                        "input_payload": user_payload,
                    }
                    owner._events.append(event)
                status, assistant_content, delay, behavior = owner._reply_for(event, user_payload)
                if behavior == "block":
                    owner._blocked.setdefault(sequence, threading.Event()).set()
                    owner._release.setdefault(sequence, threading.Event()).wait(timeout=180)
                if delay > 0:
                    time.sleep(delay)
                if status == 200:
                    response_body = json.dumps({
                        "id": f"chatcmpl-r4-runtime-{sequence}",
                        "object": "chat.completion",
                        "created": 0,
                        "model": request.get("model", _MODEL),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": assistant_content}, "finish_reason": "stop"}],
                    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                else:
                    response_body = json.dumps({"error": {"message": f"synthetic loopback HTTP {status}", "type": "synthetic_error", "code": f"http_{status}"}}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                event["response_status"] = status
                event["response_body_base64"] = base64.b64encode(response_body).decode("ascii")
                event["response_body_utf8"] = response_body.decode("utf-8")
                event["response_body_sha256"] = hashlib.sha256(response_body).hexdigest()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(response_body)
                    event["response_finished_monotonic"] = time.monotonic()
                    event["response_delivered"] = True
                except (BrokenPipeError, ConnectionResetError, OSError):
                    event["response_finished_monotonic"] = time.monotonic()
                    event["response_delivered"] = False
                    event["client_disconnected_before_response"] = True
                finally:
                    with owner._lock:
                        owner._active -= 1
                        owner._raw_lines.append((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                    self.close_connection = True

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="r4-runtime-loopback-provider")
        self._started = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    @property
    def actual_http_peak(self) -> int:
        with self._lock:
            return self._peak

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def configure(self, document_id: str, behavior: Mapping[str, Any]) -> None:
        with self._lock:
            self._behaviors[document_id] = dict(behavior)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._events]

    def events_for(self, document_id: str) -> list[dict[str, Any]]:
        return [item for item in self.events() if item.get("document_id") == document_id]

    def last_sequence(self) -> int:
        with self._lock:
            return len(self._events)

    def raw_event_lines(self) -> bytes:
        with self._lock:
            return b"".join(self._raw_lines)

    def wait_received(self, document_id: str, stage: str, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in self.events_for(document_id):
                if item.get("stage") == stage:
                    return item
            time.sleep(0.025)
        raise TimeoutError(f"loopback_stage_not_reached:{stage}")

    def wait_blocked(self, document_id: str, stage: str, *, timeout: float) -> dict[str, Any]:
        event = self.wait_received(document_id, stage, timeout=timeout)
        event_id = int(event["sequence"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            blocked = self._blocked.get(event_id)
            if blocked is not None and blocked.is_set():
                return event
            time.sleep(0.025)
        raise TimeoutError(f"loopback_stage_not_blocked:{stage}")

    def release_event(self, sequence: int) -> None:
        self._release.setdefault(sequence, threading.Event()).set()

    def release_all(self) -> None:
        with self._lock:
            for event_id in list(self._release):
                self._release[event_id].set()

    def wait_response_finished(self, sequence: int, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for item in self._events:
                    if item.get("sequence") == sequence and item.get("response_finished_monotonic") is not None:
                        return dict(item)
            time.sleep(0.025)
        raise TimeoutError("loopback_late_response_not_finished")

    def _classify(self, request: Mapping[str, Any], raw_body: bytes) -> tuple[str, Any, dict[str, Any]]:
        messages = request.get("messages")
        if not isinstance(messages, list):
            raise ValueError("chat_messages_invalid")
        user = next(item for item in messages if isinstance(item, Mapping) and item.get("role") == "user")
        content = user.get("content")
        if not isinstance(content, str):
            raise ValueError("chat_user_content_invalid")
        envelope = json.loads(content)
        decoder = "identity_json"
        if isinstance(envelope, Mapping) and envelope.get("schema_version") == "annual_issue_input_compact_v1":
            value = self.codec.decode_annual_issue_input_wire(envelope)
            decoder = "snapshot.decode_annual_issue_input_wire"
        elif isinstance(envelope, Mapping) and str(envelope.get("schema_version", "")).startswith("annual_model_review_v3_compact_"):
            value = self.codec.decode_annual_review_v3_wire(envelope)
            decoder = "snapshot.decode_annual_review_v3_wire"
        else:
            value = envelope
        if isinstance(value, Mapping) and type(value.get("batch_index")) is int:
            stage = "discovery"
        elif isinstance(value, Mapping) and value.get("schema_version") == "annual_issue_group_shard_input_v1":
            stage = "grouping"
        elif isinstance(value, Mapping) and value.get("schema_version") == "annual_issue_global_input_v1":
            stage = "global"
        elif isinstance(value, Mapping) and value.get("schema_version") == "annual_issue_supplement_input_v1":
            stage = "supplement"
        elif isinstance(value, Mapping) and value.get("schema_version") == "annual_model_review_v3":
            stage = "review"
        elif isinstance(value, Mapping) and isinstance(value.get("candidate"), Mapping):
            stage = "investigation"
        else:
            raise ValueError("chat_stage_not_recognized_from_actual_request")
        return stage, value, {
            "user_message_content_utf8": content,
            "user_message_content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "wire_envelope_schema_version": envelope.get("schema_version") if isinstance(envelope, Mapping) else None,
            "wire_decoder": decoder,
            "decoded_business_schema_version": value.get("schema_version") if isinstance(value, Mapping) else None,
            "decoded_payload_sha256": hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "wire_raw_body_sha256": hashlib.sha256(raw_body).hexdigest(),
            "wire_raw_body_bytes": len(raw_body),
        }

    def _reply_for(self, event: Mapping[str, Any], payload: Any) -> tuple[int, str, float, str | None]:
        document_id = str(event.get("document_id", ""))
        stage = str(event.get("stage", ""))
        mode = self._behaviors.get(document_id, {}).get("mode", "success")
        if stage == "discovery" and int(payload.get("batch_index", 0)) == 6 and mode.startswith("invalid_batch_2"):
            if mode.startswith("invalid_batch_2"):
                return 200, "{}", 0.0, None
        if mode == "block_grouping" and stage == "grouping":
            return 200, self._business_response(stage, payload), 0.0, "block"
        if mode.startswith("delay_global") and stage == "global":
            delay_ready = self._behaviors[document_id].get("delay_ready")
            if delay_ready is not None and not delay_ready.wait(timeout=20.0):
                raise TimeoutError("global_delay_owner_budget_not_configured")
            return 200, self._business_response(stage, payload), float(self._behaviors[document_id]["delay_seconds"]), None
        if mode.startswith("http401_supplement") and stage == "supplement":
            return 401, "", 0.0, None
        retry_stage = "grouping" if "retry_grouping" in mode else "global" if "retry_global" in mode else None
        if retry_stage is not None and stage == retry_stage:
            body_sha = str(event["request_body_sha256"])
            key = (document_id, stage, body_sha)
            with self._lock:
                attempt = self._stage_hits[key]
            if attempt == 1:
                return 429, "", 0.0, None
            if attempt == 2:
                return 500, "", 0.0, None
        return 200, self._business_response(
            stage,
            payload,
            mode=mode,
            findings_per_batch=3 if mode.endswith("_many") else 1,
        ), 0.0, None

    def _business_response(self, stage: str, payload: Any, *, mode: str = "success", findings_per_batch: int = 1) -> str:
        if stage == "discovery":
            fragments = payload.get("fragments", [])
            if not isinstance(fragments, list) or not fragments:
                return json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
            valid_fragments = [
                item for item in fragments
                if isinstance(item, list) and len(item) == 3
                and isinstance(item[0], str) and isinstance(item[2], str)
                and not any(character.isdigit() for character in item[2])
            ]
            if not valid_fragments:
                return json.dumps({"findings": [], "limitations": ["controlled_request_had_no_quoteable_fragment"]}, ensure_ascii=False)
            semantic_fragment = None
            if mode.startswith("resolve_batch_2") and payload.get("batch_index") == 6:
                semantic_fragment = next((
                    item for item in fragments
                    if isinstance(item, list) and len(item) == 3
                    and isinstance(item[0], str) and isinstance(item[2], str)
                    and _SEMANTIC_DRIFT_SOURCE_TEXT in item[2]
                ), None)
                if semantic_fragment is None:
                    return json.dumps({
                        "findings": [],
                        "limitations": ["controlled_semantic_drift_source_span_missing"],
                    }, ensure_ascii=False)
            shared_fragments = [
                item for item in valid_fragments
                if isinstance(item, list) and len(item) == 3
                and isinstance(item[0], str) and isinstance(item[2], str)
                and _FIXTURE_SHARED_QUOTE in item[2]
            ]
            if not shared_fragments:
                return json.dumps({
                    "findings": [],
                    "limitations": ["controlled_shared_semantics_source_span_missing"],
                }, ensure_ascii=False)
            findings = []
            for slot in range(max(1, min(3, findings_per_batch))):
                if semantic_fragment is not None and slot == 0:
                    alias = semantic_fragment[0]
                    text = _SEMANTIC_DRIFT_SOURCE_TEXT
                    finding = {
                        "title": "后部附注结算时点核对线索",
                        "observation": text,
                        "why_it_matters": "披露将回款结算时点与账龄分类关联，需沿同源材料核对口径。",
                        "follow_up": "沿本次原文位置对照合同台账与应收款项账龄的形成时点。",
                        "priority": "low",
                        "citations": [{"fragment_alias": alias, "quote": text}],
                    }
                else:
                    normal_slot = slot - 1 if semantic_fragment is not None else slot
                    alias, _page, _source_text = shared_fragments[normal_slot % len(shared_fragments)]
                    text = _FIXTURE_SHARED_QUOTE
                    finding = {
                        "title": "合同回款台账核对线索",
                        "observation": f"本次请求所引原文记载：{text}",
                        "why_it_matters": "需要结合所引原文的上下文与披露口径核对其含义。",
                        "follow_up": "沿本次引用位置核对合同回款和账龄披露的依据与口径。",
                        "priority": "low",
                        "citations": [{"fragment_alias": alias, "quote": text}],
                    }
                findings.append(finding)
            return json.dumps({"findings": findings, "limitations": []}, ensure_ascii=False)
        if stage == "investigation":
            candidate = payload.get("candidate", {})
            evidence = payload.get("narrative_evidence", [])
            evidence_ids = [item["evidence_id"] for item in evidence if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)] if isinstance(evidence, list) else []
            return json.dumps({
                "signal_id": candidate.get("signal_id"),
                "explanation": "同一合成材料中的合同文字可能需要对照形成过程；本答复不确认异常或因果关系。",
                "alternative_explanations": [],
                "limitations": ["仅为本次虚构软件验收材料的程序响应。"],
                "narrative_evidence_ids": evidence_ids[:4],
                "request_more_context": False,
            }, ensure_ascii=False)
        if stage == "grouping":
            candidates = {
                item["finding_id"]: item
                for item in payload.get("candidate_pool", [])
                if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
            }
            roots: dict[tuple[str, ...], str] = {}
            decisions = []
            for finding_id in payload.get("input_member_finding_ids", []):
                key = _fixture_semantic_key(candidates.get(finding_id))
                if key is None:
                    key = ("unique_finding", str(finding_id))
                root = roots.get(key)
                if root is None:
                    roots[key] = str(finding_id)
                    decisions.append({
                        "finding_id": finding_id,
                        "disposition": "retain",
                        "target_finding_id": None,
                        "reason_code": "distinct_issue",
                        "reason_evidence_id": None,
                        "reason_quote": None,
                    })
                else:
                    decisions.append({
                        "finding_id": finding_id,
                        "disposition": "merge",
                        "target_finding_id": root,
                        "reason_code": "same_observed_event",
                        "reason_evidence_id": None,
                        "reason_quote": None,
                    })
            return json.dumps({"schema_version": "annual_issue_group_shard_result_v1", "decisions": decisions}, ensure_ascii=False)
        if stage == "global":
            candidates = {
                item["finding_id"]: item
                for item in payload.get("candidate_pool", [])
                if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
            }
            roots: dict[tuple[str, ...], str] = {}
            decisions = []
            for issue in payload.get("issue_members", []):
                if not isinstance(issue, Mapping) or not isinstance(issue.get("issue_id"), str):
                    continue
                issue_id = issue["issue_id"]
                member_ids = issue.get("member_finding_ids", [])
                member_keys = [_fixture_semantic_key(candidates.get(member)) for member in member_ids]
                key = member_keys[0] if member_keys and member_keys[0] is not None and all(item == member_keys[0] for item in member_keys) else None
                if key is None:
                    key = ("unique_issue", issue_id)
                root = roots.get(key)
                if root is None:
                    roots[key] = issue_id
                    decisions.append({
                        "issue_id": issue_id,
                        "disposition": "retain",
                        "target_issue_id": None,
                        "reason_code": "distinct_across_shards",
                        "reason_evidence_id": None,
                        "reason_quote": None,
                    })
                else:
                    decisions.append({
                        "issue_id": issue_id,
                        "disposition": "merge",
                        "target_issue_id": root,
                        "reason_code": "same_underlying_issue",
                        "reason_evidence_id": None,
                        "reason_quote": None,
                    })
            return json.dumps({"schema_version": "annual_issue_global_result_v1", "decisions": decisions}, ensure_ascii=False)
        if stage == "supplement":
            roles = [{"issue_id": item["issue_id"], "evidence_id": item["evidence_id"], "role": "context", "reason_code": "context_only"} for item in payload.get("evidence_assignments", [])]
            return json.dumps({"schema_version": "annual_issue_supplement_result_v1", "roles": roles}, ensure_ascii=False)
        if stage == "review":
            limitations = payload.get("limitations_required")
            if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
                return json.dumps({"invalid_request": "review_limitations_missing"}, ensure_ascii=False)
            return json.dumps({
                "assessment": "insufficient_evidence",
                "summary": "本次复核仅依据当前报告输入检查待核事项与范围限制。",
                "reasons": [],
                "follow_up_items": [],
                "limitations": limitations,
            }, ensure_ascii=False)
        raise ValueError(f"response_stage_unhandled:{stage}")

    def close(self) -> None:
        self.release_all()
        if self._started:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._started = False


def _find_document_id(value: Any) -> str | None:
    candidates: list[str] = []
    def walk(item: Any) -> None:
        if isinstance(item, str):
            if item.startswith("r4runtime-"):
                candidates.append(item)
        elif isinstance(item, Mapping):
            for key, child in item.items():
                if isinstance(key, str) and key in {"source_document_id", "document_id"} and isinstance(child, str):
                    candidates.append(child)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    walk(value)
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else (unique[0] if unique else None)


def _fixture_semantic_key(value: Any) -> tuple[str, ...] | None:
    """Group only complete synthetic records with all original citations present."""
    if not isinstance(value, Mapping):
        return None
    fields = ("title", "observation", "why_it_matters", "follow_up")
    semantic_values = tuple(value.get(field) for field in fields)
    if any(not isinstance(item, str) or not item.strip() for item in semantic_values):
        return None
    citations = value.get("citations")
    if not isinstance(citations, list) or not citations or any(
        not isinstance(item, Mapping)
        or not isinstance(item.get("evidence_id"), str)
        or not isinstance(item.get("quote"), str)
        or not item.get("quote", "").strip()
        for item in citations
    ):
        return None
    return ("semantic", *semantic_values)


def _first_request_text(value: Any) -> str:
    prioritized: list[str] = []
    ordinary: list[str] = []
    preferred = {"quote", "raw_text", "observation", "why_it_matters", "title", "text", "summary"}
    def walk(item: Any, key: str | None = None) -> None:
        if isinstance(item, str):
            if len(item.strip()) >= 12:
                normalized = "".join(character for character in item.strip() if not character.isdigit())
                normalized = " ".join(normalized.split())[:96]
                if len(normalized) >= 12:
                    (prioritized if key in preferred else ordinary).append(normalized)
        elif isinstance(item, Mapping):
            for child_key, child in item.items():
                walk(child, str(child_key) if isinstance(child_key, str) else None)
        elif isinstance(item, list):
            for child in item:
                walk(child, key)
    walk(value)
    return (prioritized or ordinary or [""])[0]


_STRICT_READER_DRIVER = r'''from __future__ import annotations
import hashlib, json, sqlite3, sys
from pathlib import Path
from finagent.api.annual_report_read import load_annual_report
from finagent.core.annual_runtime_identity import capture_api_runtime_identity
root=Path(sys.argv[1]).resolve(); run_id=sys.argv[2]; output=Path(sys.argv[3]); ledger=Path(sys.argv[4]); job_id=sys.argv[5]
archive=load_annual_report(root, run_id)
with sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True) as connection:
    connection.execute("PRAGMA query_only=ON")
    total=connection.execute("SELECT COUNT(*) FROM provider_attempts WHERE goal_counted=1").fetchone()[0]
    goal_counts=connection.execute("SELECT stage, COUNT(*) FROM provider_attempts WHERE goal_counted=1 GROUP BY stage").fetchall()
    gate_counts=connection.execute("SELECT stage, COUNT(*) FROM provider_attempts WHERE run_id=? GROUP BY stage", (run_id,)).fetchall()
    rows=connection.execute("SELECT admission_id, gate_key, run_id, stage, batch_index, audit_id, status, goal_counted, source_sha256 FROM provider_attempts WHERE run_id=? ORDER BY admitted_at, admission_id", (run_id,)).fetchall()
runtime=capture_api_runtime_identity(root)
result={
  "loaded": True, "run_id": archive.run_id, "snapshot": archive.snapshot,
  "manifest_summary": archive.manifest_summary, "report": archive.report,
  "run_directory": str(root/"artifacts"/"runs"/run_id),
  "report_directory": str(root/"artifacts"/"reports"/run_id),
  "job_directory": str(root/"artifacts"/"annual-analysis-jobs"/job_id),
  "runtime_identity": runtime,
  "sqlite_read_only": {
    "reads_are_read_only": True, "goal_attempt_count": total,
    "goal_stage_counts": {str(stage): int(count) for stage,count in goal_counts},
    "gate_stage_counts": {str(stage): int(count) for stage,count in gate_counts},
    "attempt_rows": [dict(zip(("admission_id","gate_key","run_id","stage","batch_index","audit_id","status","goal_counted","source_sha256"),row)) for row in rows],
  },
}
output.write_bytes((json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)+"\n").encode("utf-8"))
'''
