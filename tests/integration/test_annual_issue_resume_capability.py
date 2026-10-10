"""Strict R4 resume-capability checks through the standard API/CLI pipeline.

The test captures a source-only allowlist into a unique formal attempt, writes a
new dummy local model configuration, and sends every model request to a local
OpenAI-compatible fixture.  Reports are produced by AnnualAnalysisJobService
and must pass the production archive reader before capability assertions.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

import pymupdf
import pytest


_ROOT = Path(__file__).resolve().parents[2]
_ATTEMPT_ENV = "FINTRACE_R4_STRICT_ATTEMPT_DIR"
_FREEZE_MANIFEST_PATH = _ROOT / "artifacts" / "evaluations" / "annual-recovery-r4-20261010" / "r4-candidate-freeze-manifest-20261010T020439Z-978ec097" / "candidate-freeze-manifest.json"
_FREEZE_MANIFEST_RAW_SHA256 = "53575e0277038bd96a4ed184284cf88013c43a7feb3846cf1763b32935c3deeb"
_FREEZE_MANIFEST_SHA256 = "321d309c073be1ce2393ece8b82b473c1e7fea5dc8e7118227d367598a26630c"
_FREEZE_RUNTIME_SOURCE_SHA256 = "38592c3737fb1a36bbb0be9c9c3ac193a99b1c95f2050883bfb7e465a14fd201"
_FREEZE_RUNTIME_FILE_COUNT = 81
_FREEZE_BUSINESS_CONFIG_SHA256 = "c881cf2fcc3820fdd247b789ed57f933e1633bccb2dc1a7702fb8e1ffe526e92"
_FREEZE_BUSINESS_CONFIG_FILE_COUNT = 10
_FREEZE_FRONTEND_SHA256 = "7b00b0f4b3ee6859ff87c10d6c1f02584f14c89fe016fe383d6f592bc022a06c"
_FREEZE_FRONTEND_FILE_COUNT = 23
_FREEZE_REVIEW_PROMPT_SHA256 = "04bd401621dad72f070e149a23776cc9e78983f7d60751fa6250751fcfe12561"
_MODEL = "fintrace-r4-loopback-fixture"
_DUMMY_KEY = "r4-strict-capability-loopback-dummy-key"
_LONG_SOURCE_PAGES = 30
_GROUP_MISSING_STAGE = {
    "r4local": "grouping",
    "r4global": "global",
    "r4supp": "supplement",
}
def test_standard_v5_archives_drive_strict_resume_capability(tmp_path: Path) -> None:
    """Exercise false/true gates against durable API-published v5 archives."""

    attempt_value = os.environ.get(_ATTEMPT_ENV)
    evaluation_value = os.environ.get("FINTRACE_R4_EVALUATION_DIR")
    runner_value = os.environ.get("FINTRACE_R4_RUNNER_DIR")
    if not attempt_value or not evaluation_value or not runner_value:
        pytest.skip(f"需要由 task-owned evidence runner 设置 {_ATTEMPT_ENV}、FINTRACE_R4_EVALUATION_DIR 和 FINTRACE_R4_RUNNER_DIR。")
    attempt = Path(attempt_value).resolve()
    evaluation = Path(evaluation_value).resolve()
    runner = Path(runner_value).resolve()
    evaluation_root = (_ROOT / "artifacts" / "evaluations" / "annual-recovery-r4-20261010").resolve()
    runner_root = (_ROOT / "artifacts" / "tmp" / "recovery-r4-final-semantic-20261009").resolve()
    if (
        evaluation.parent != evaluation_root
        or not evaluation.name.startswith("final-semantic-strict-")
        or attempt != evaluation / "strict"
        or not runner.is_relative_to(runner_root)
        or runner == runner_root
    ):
        raise AssertionError("strict_attempt_is_outside_the_approved_unique_evidence_locations")
    if not attempt.is_dir():
        raise AssertionError("formal_attempt_directory_missing")
    freeze_verification = _verify_candidate_freeze(attempt)
    case_root = attempt / "cases"
    case_root.mkdir(parents=True, exist_ok=False)
    project_root, source_manifest = _capture_source_snapshot(attempt)
    if source_manifest["file_count"] != _FREEZE_RUNTIME_FILE_COUNT:
        raise AssertionError("captured_snapshot_file_count_does_not_match_candidate_freeze")
    snapshot_python_path = str(project_root / "backend" / "src")
    sys.path.insert(0, snapshot_python_path)
    from finagent.agents import annual_review_wire as issue_wire_codec

    codec_module_path = Path(issue_wire_codec.__file__).resolve()
    expected_codec_path = (project_root / "backend" / "src" / "finagent" / "agents" / "annual_review_wire.py").resolve()
    if codec_module_path != expected_codec_path:
        raise AssertionError("loopback_decoder_must_use_captured_snapshot_codec")
    model_file = project_root / ".env.model"
    provider = _LoopbackProvider(
        issue_input_decoder=issue_wire_codec.decode_annual_issue_input_wire,
        issue_wire_schema=issue_wire_codec.ISSUE_INPUT_WIRE_SCHEMA,
        review_input_decoder=issue_wire_codec.decode_annual_review_v3_wire,
        review_wire_schema=issue_wire_codec.V3_WIRE_SCHEMA,
        review_projection_version=issue_wire_codec.V3_INPUT_PROJECTION_VERSION,
        codec_module_path=codec_module_path,
        output_dir=attempt / "loopback-http",
    )
    _write_dummy_model_settings(model_file, "r4-dummy-model-a", provider.base_url)
    observer_path = project_root / "backend" / "src" / "sitecustomize.py"
    observer_path.write_text(_ACTUAL_CLI_IDENTITY_OBSERVER, encoding="utf-8", newline="\n")
    _write_json_new(attempt / "provider-policy.json", {
        "provider": "fully local OpenAI-compatible test fixture",
        "bind_host": "127.0.0.1",
        "base_url": provider.base_url,
        "dummy_api_key": _DUMMY_KEY,
        "external_provider_calls": 0,
        "real_model_configuration_read": False,
        "source_manifest_sha256": source_manifest["sha256"],
        "source_file_count": source_manifest["file_count"],
        "loopback_input_decoder_module": str(codec_module_path),
        "loopback_input_decoder_sha256": _sha256(codec_module_path),
        "actual_cli_runtime_observer_path": str(observer_path),
        "actual_cli_runtime_observer_sha256": _sha256(observer_path),
        "actual_cli_observer_role": "sys.setprofile return-event capture of production capture_cli_runtime_identity in actual Popen child; no production files patched",
    })

    try:
        fixtures = {
            "tail": _create_source(project_root, "r4tail", finding_count=10, retrieval_notes=True),
            "review_oversize": _create_source(project_root, "r4over", finding_count=24, retrieval_notes=False),
            "in_cap": _create_source(project_root, "r4incap", finding_count=2, retrieval_notes=False),
            "local_plan_without_shard": _create_source(project_root, "r4local", finding_count=2, retrieval_notes=False),
            "global_plan_without_shard": _create_source(project_root, "r4global", finding_count=13, retrieval_notes=False),
            "supplement_plan_without_shard": _create_source(project_root, "r4supp", finding_count=13, retrieval_notes=True),
        }
        _write_json_new(attempt / "controlled-sources.json", {
            key: {
                "source_pdf": str(value["pdf"]),
                "source_record": str(value["record"]),
                "source_sha256_before": value["sha256"],
                "nature": "本次测试从空白 PDF 原创构造的虚构软件测试输入；非真实披露、非黄金答案、非异常或舞弊标签",
                "base_repository_source_files_used": [],
            }
            for key, value in fixtures.items()
        })

        outcomes: dict[str, dict[str, Any]] = {}
        execution_errors: dict[str, dict[str, str]] = {}
        for name, fixture in fixtures.items():
            expected_block = _GROUP_MISSING_STAGE.get(_case_prefix(str(fixture["document_id"])))
            try:
                outcome = _run_api_case(
                    project_root,
                    fixture,
                    case_root / name,
                    provider,
                    cancel_at_stage=expected_block,
                )
                outcome["provider_events"] = provider.events_for(str(fixture["document_id"]))
                outcome["sqlite_read_only_counts"] = _read_only_sqlite_counts(project_root / "artifacts")
                _write_json_new(case_root / name / "result.json", outcome)
                outcomes[name] = outcome
            except Exception as exc:
                execution_errors[name] = {"type": type(exc).__name__, "message": str(exc)}
                _write_json_new(case_root / name / "case-error.json", execution_errors[name])
            finally:
                _write_json_new(
                    case_root / name / "provider-events.json",
                    provider.events_for(str(fixture["document_id"])),
                )
                _write_json_new(
                    case_root / name / "sqlite-read-only-counts.json",
                    _read_only_sqlite_counts(project_root / "artifacts"),
                )

        assertion_errors: dict[str, dict[str, str]] = {}
        checks = {
            "tail": lambda: _assert_durable_tail_false(outcomes["tail"]),
            "review_oversize": lambda: _assert_durable_review_budget_boundary(outcomes["review_oversize"]),
            "in_cap": lambda: _assert_durable_in_cap_true(outcomes["in_cap"]),
            "local_plan_without_shard": lambda: _assert_durable_missing_plan_true(outcomes["local_plan_without_shard"], "grouping"),
            "global_plan_without_shard": lambda: _assert_durable_missing_plan_true(outcomes["global_plan_without_shard"], "global"),
            "supplement_plan_without_shard": lambda: _assert_durable_missing_plan_true(outcomes["supplement_plan_without_shard"], "supplement"),
        }
        for name, check in checks.items():
            if name not in outcomes:
                continue
            try:
                check()
            except Exception as exc:
                assertion_errors[name] = {"type": type(exc).__name__, "message": str(exc)}

        drift: dict[str, Any] | None = None
        if "in_cap" in outcomes:
            try:
                drift = _run_binding_drift_checks(
                    project_root,
                    outcomes["in_cap"],
                    fixtures["in_cap"],
                    model_file,
                    provider,
                    attempt,
                )
                _write_json_new(attempt / "binding-drift-checks.json", drift)
            except Exception as exc:
                assertion_errors["binding_drift"] = {"type": type(exc).__name__, "message": str(exc)}
        source_after = _source_manifest(_ROOT)
        _write_json_new(attempt / "source-manifest-after.json", source_after)
        if source_after != source_manifest:
            assertion_errors["source_manifest"] = {"type": "AssertionError", "message": "repository_allowlist_changed_after_snapshot_capture"}
        clone_after = _source_manifest(project_root)
        _write_json_new(attempt / "runtime-copy-manifest-after.json", clone_after)
        if clone_after != source_manifest:
            assertion_errors["runtime_copy_manifest"] = {"type": "AssertionError", "message": "runtime_copy_source_changed_during_case_execution"}
        source_hashes_after: dict[str, dict[str, str]] = {}
        for name, fixture in fixtures.items():
            after_hash = _sha256(Path(fixture["pdf"]))
            source_hashes_after[name] = {"before": fixture["sha256"], "after": after_hash}
            if after_hash != fixture["sha256"]:
                assertion_errors[f"source_hash:{name}"] = {"type": "AssertionError", "message": "controlled_source_hash_changed"}
        _write_json_new(attempt / "controlled-source-hashes-after.json", source_hashes_after)
        failures = {**execution_errors, **assertion_errors}
        _write_json_new(attempt / "attempt-result.json", {
            "status": "passed" if not failures else "failed",
            "attempted_case_count": len(fixtures),
            "completed_case_count": len(outcomes),
            "case_names": list(outcomes),
            "failures": failures,
            "candidate_freeze_verification": freeze_verification,
            "binding_drift_checks_captured": drift is not None,
            "source_manifest_sha256_before_after": source_manifest["sha256"],
            "runtime_copy_manifest_sha256_before_after": clone_after["sha256"],
            "external_provider_calls": 0,
            "provider_event_count": len(provider.events()),
            "original_generated_parent_source_sha256_before_after": {
                name: {"before": value["sha256"], "after": _sha256(Path(value["pdf"]))}
                for name, value in fixtures.items()
            },
        })
        if failures:
            raise AssertionError("strict_capability_case_failures:" + json.dumps(failures, ensure_ascii=False, sort_keys=True))
    finally:
        provider.close()


def _capture_source_snapshot(attempt: Path) -> tuple[Path, dict[str, Any]]:
    before = _source_manifest(_ROOT)
    _write_json_new(attempt / "source-manifest-before.json", before)
    project_root = attempt / "snapshot" / "project"
    if project_root.exists():
        raise FileExistsError("formal_snapshot_project_already_exists")
    project_root.mkdir(parents=True)
    (project_root / "backend" / "src").mkdir(parents=True)
    shutil.copytree(_ROOT / "backend" / "src" / "finagent", project_root / "backend" / "src" / "finagent")
    (project_root / "scripts").mkdir()
    shutil.copy2(_ROOT / "scripts" / "analyze_annual.py", project_root / "scripts" / "analyze_annual.py")
    (project_root / "config" / "prompts").mkdir(parents=True)
    for prompt in (_ROOT / "config" / "prompts").rglob("*.md"):
        relative = prompt.relative_to(_ROOT / "config" / "prompts")
        target = project_root / "config" / "prompts" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prompt, target)
    (project_root / "artifacts").mkdir()
    after = _source_manifest(_ROOT)
    clone = _source_manifest(project_root)
    _write_json_new(attempt / "source-manifest-after-copy.json", after)
    _write_json_new(attempt / "runtime-copy-manifest-before.json", clone)
    if before != after or before != clone:
        raise AssertionError("allowlist_source_copy_manifest_mismatch")
    return project_root, before


def _verify_candidate_freeze(attempt: Path) -> dict[str, Any]:
    raw = _FREEZE_MANIFEST_PATH.read_bytes()
    attempt_copy = attempt / "candidate-freeze-manifest.json"
    if not attempt_copy.is_file() or attempt_copy.read_bytes() != raw:
        raise AssertionError("attempt_freeze_manifest_copy_does_not_match_ROOT_source")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    payload_without_digest = dict(payload)
    declared_manifest_sha256 = payload_without_digest.pop("manifest_sha256", None)
    computed_manifest_sha256 = _digest_json(payload_without_digest)
    runtime_entries = payload.get("runtime_source_entries")
    business_config_entries = payload.get("business_config_entries")
    frontend_entries = payload.get("frontend_entries")
    mismatches: list[str] = []

    if raw_sha256 != _FREEZE_MANIFEST_RAW_SHA256:
        mismatches.append("freeze_manifest_raw_sha256")
    if declared_manifest_sha256 != _FREEZE_MANIFEST_SHA256 or computed_manifest_sha256 != _FREEZE_MANIFEST_SHA256:
        mismatches.append("freeze_manifest_internal_sha256")
    if payload.get("runtime_source_manifest_sha256") != _FREEZE_RUNTIME_SOURCE_SHA256:
        mismatches.append("freeze_runtime_source_sha256")
    if payload.get("runtime_source_file_count") != _FREEZE_RUNTIME_FILE_COUNT:
        mismatches.append("freeze_runtime_source_file_count")

    current = _source_manifest(_ROOT)
    expected_runtime_rows = [
        {"path": item.get("path"), "sha256": item.get("sha256")}
        for item in runtime_entries if isinstance(item, Mapping)
    ] if isinstance(runtime_entries, list) else []
    if current["files"] != expected_runtime_rows:
        mismatches.append("current_runtime_path_or_sha256")
    for item in runtime_entries if isinstance(runtime_entries, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            mismatches.append("malformed_runtime_entry")
            continue
        source_path = _ROOT / Path(*item["path"].split("/"))
        if not source_path.is_file() or source_path.stat().st_size != item.get("bytes"):
            mismatches.append("current_runtime_byte_count:" + item["path"])

    config_pairs: list[tuple[str, str]] = []
    runtime_map = {row["path"]: row["sha256"] for row in expected_runtime_rows}
    for item in business_config_entries if isinstance(business_config_entries, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            mismatches.append("malformed_business_config_entry")
            continue
        relative = item["path"]
        expected_sha = item.get("sha256")
        config_pairs.append((relative, expected_sha))
        config_path = _ROOT / Path(*relative.split("/"))
        if runtime_map.get(relative) != expected_sha:
            mismatches.append("business_config_not_in_runtime_manifest:" + relative)
        if (
            not config_path.is_file()
            or config_path.stat().st_size != item.get("bytes")
            or _sha256(config_path) != expected_sha
        ):
            mismatches.append("current_business_config_entry:" + relative)
    computed_runtime_sha = _digest_json([
        (item["path"], item["sha256"]) for item in expected_runtime_rows
    ])
    computed_config_sha = _digest_json(config_pairs)
    if computed_runtime_sha != _FREEZE_RUNTIME_SOURCE_SHA256:
        mismatches.append("freeze_runtime_manifest_reconstruction")
    if (
        computed_config_sha != _FREEZE_BUSINESS_CONFIG_SHA256
        or payload.get("business_config_manifest_sha256") != _FREEZE_BUSINESS_CONFIG_SHA256
        or not isinstance(business_config_entries, list)
        or len(business_config_entries) != _FREEZE_BUSINESS_CONFIG_FILE_COUNT
    ):
        mismatches.append("freeze_business_config_manifest_reconstruction")
    frontend_pairs: list[tuple[str, str]] = []
    for item in frontend_entries if isinstance(frontend_entries, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            mismatches.append("malformed_frontend_entry")
            continue
        relative = item["path"]
        expected_sha = item.get("sha256")
        frontend_pairs.append((relative, expected_sha))
        frontend_path = _ROOT / Path(*relative.split("/"))
        if (
            not frontend_path.is_file()
            or frontend_path.is_symlink()
            or frontend_path.stat().st_size != item.get("bytes")
            or _sha256(frontend_path) != expected_sha
        ):
            mismatches.append("current_frontend_entry:" + relative)
    computed_frontend_sha = _digest_json(frontend_pairs)
    if (
        computed_frontend_sha != _FREEZE_FRONTEND_SHA256
        or payload.get("frontend_manifest_sha256") != _FREEZE_FRONTEND_SHA256
        or not isinstance(frontend_entries, list)
        or len(frontend_entries) != _FREEZE_FRONTEND_FILE_COUNT
    ):
        mismatches.append("freeze_frontend_manifest_reconstruction")
    review_prompt = next((
        item for item in business_config_entries
        if isinstance(item, Mapping) and item.get("path") == "config/prompts/annual_review_v3.md"
    ), None) if isinstance(business_config_entries, list) else None
    if not isinstance(review_prompt, Mapping) or review_prompt.get("sha256") != _FREEZE_REVIEW_PROMPT_SHA256:
        mismatches.append("freeze_review_prompt_sha256")

    result = {
        "freeze_manifest_path": str(_FREEZE_MANIFEST_PATH),
        "freeze_manifest_raw_sha256": raw_sha256,
        "attempt_freeze_manifest_sha256": _sha256(attempt_copy),
        "freeze_manifest_declared_sha256": declared_manifest_sha256,
        "freeze_manifest_recomputed_sha256": computed_manifest_sha256,
        "runtime_source_manifest_sha256": computed_runtime_sha,
        "runtime_source_file_count": len(expected_runtime_rows),
        "business_config_manifest_sha256": computed_config_sha,
        "business_config_file_count": len(config_pairs),
        "frontend_manifest_sha256": computed_frontend_sha,
        "frontend_file_count": len(frontend_pairs),
        "review_prompt_sha256": review_prompt.get("sha256") if isinstance(review_prompt, Mapping) else None,
        "current_runtime_matches_freeze": "current_runtime_path_or_sha256" not in mismatches
        and not any(item.startswith("current_runtime_byte_count:") for item in mismatches),
        "current_business_config_matches_freeze": not any(
            item.startswith("current_business_config_entry:") for item in mismatches
        ),
        "current_frontend_matches_freeze": not any(
            item.startswith("current_frontend_entry:") for item in mismatches
        ),
        "mismatches": mismatches,
    }
    _write_json_new(attempt / "candidate-freeze-verification.json", result)
    if mismatches:
        raise AssertionError("candidate_freeze_verification_failed:" + json.dumps(mismatches, ensure_ascii=False))
    return result


def _digest_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _source_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    paths = [root / "scripts" / "analyze_annual.py"]
    backend = root / "backend" / "src" / "finagent"
    prompts = root / "config" / "prompts"
    paths.extend(path for path in backend.rglob("*.py") if "__pycache__" not in path.parts)
    paths.extend(prompts.rglob("*.md"))
    rows: list[dict[str, str]] = []
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path.is_symlink():
            raise AssertionError(f"source_allowlist_entry_not_regular:{path.relative_to(root).as_posix()}")
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256(path),
        })
    if not rows:
        raise AssertionError("source_allowlist_empty")
    material = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(material).hexdigest(), "file_count": len(rows), "files": rows}


def _create_source(
    project_root: Path,
    prefix: str,
    *,
    finding_count: int,
    retrieval_notes: bool,
) -> dict[str, Any]:
    company_id = "SIM-R4-STRICT"
    report_year = 2025
    document_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
    source_rel = f"{company_id}/{report_year}/{document_id}/source.pdf"
    pdf_path = project_root / "data" / "raw" / company_id / str(report_year) / document_id / "source.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=False)
    document = pymupdf.open()
    try:
        for page_index in range(_LONG_SOURCE_PAGES):
            page = document.new_page(width=595, height=842)
            page.insert_text((35, 30), "管理层讨论与分析", fontsize=7, fontname="china-s")
            repeated = "人工构造内容用于年度文本路径验证，经营记录关于台账边界和流程说明的文字仅属合成片段。"
            for line_index in range(39):
                y = 48 + line_index * 19
                page.insert_text((30, y), repeated, fontsize=5.5, fontname="china-s")
                page.insert_text((310, y), repeated, fontsize=5.5, fontname="china-s")
        notes = document.new_page(width=595, height=842)
        notes.insert_text((35, 40), "财务报表附注", fontsize=8, fontname="china-s")
        if retrieval_notes:
            context = "合同回款账龄披露核对仅为本地软件测试构造的附注情境。"
        else:
            context = "本段仅为本地软件测试构造的附注内容，不含业务披露。"
        for index in range(4):
            notes.insert_text((35, 80 + index * 32), context, fontsize=7, fontname="china-s")
        document.set_metadata({
            "title": "本地软件验收用虚构年度文本",
            "subject": "人工构造输入，不代表任何真实发行人",
            "creator": "tests/integration/test_annual_issue_resume_capability.py",
        })
        document.save(pdf_path, garbage=3, deflate=True)
    finally:
        document.close()

    digest = _sha256(pdf_path)
    record_path = pdf_path.parent / "source.json"
    _write_json_new(record_path, {
        "company_id": company_id,
        "document_id": document_id,
        "report_type": "年度报告（虚构软件测试材料）",
        "report_period": f"{report_year}-12-31",
        "local_path": f"data/raw/{source_rel}",
        "sha256": digest,
        "source_nature": "本次测试从空白 PDF 原创构造；不含真实公司披露、财务黄金答案、异常标签或舞弊标签",
        "test_only": True,
    })
    return {
        "company_id": company_id,
        "report_year": report_year,
        "document_id": document_id,
        "source_pdf_path": source_rel,
        "pdf": pdf_path,
        "record": record_path,
        "sha256": digest,
        "finding_count": finding_count,
    }


def _run_api_case(
    project_root: Path,
    fixture: Mapping[str, Any],
    case_dir: Path,
    provider: "_LoopbackProvider",
    *,
    cancel_at_stage: str | None,
) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=False)
    source_before = _sha256(Path(fixture["pdf"]))
    request = {
        key: fixture[key]
        for key in ("company_id", "report_year", "document_id", "source_pdf_path", "sha256")
    }
    request["mode"] = "model_investigation"
    control_path = case_dir / "control.json"
    _write_json_new(control_path, {
        "project_root": str(project_root),
        "request": request,
        "case_dir": str(case_dir),
        "cancel_at_stage": cancel_at_stage,
        "model_name": _MODEL,
        "expected_runtime_source_manifest_sha256": _FREEZE_RUNTIME_SOURCE_SHA256,
        "expected_runtime_source_file_count": _FREEZE_RUNTIME_FILE_COUNT,
    })
    driver_path = case_dir / "api-driver.py"
    driver_path.write_text(_API_DRIVER, encoding="utf-8", newline="\n")
    (case_dir / "cancel.signal").unlink(missing_ok=True)
    environment = _isolated_environment(project_root, provider.base_url)
    environment["FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR"] = str(case_dir)
    started = time.monotonic()
    _write_json_new(case_dir / "driver-argv.json", {
        "argv": [sys.executable, "-u", str(driver_path), str(control_path)],
        "cwd": str(project_root),
        "environment_allowlist": sorted(environment),
        "removed": ["MODEL_*", provider.credentials_removed_marker],
        "provider_base_url": provider.base_url,
    })
    process = subprocess.Popen(
        [sys.executable, "-u", str(driver_path), str(control_path)],
        cwd=project_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    blocked_stage = None
    stdout = b""
    stderr = b""
    try:
        if cancel_at_stage is not None:
            job_started_path = case_dir / "job-started.json"
            start_deadline = time.monotonic() + 30
            while not job_started_path.is_file() and process.poll() is None and time.monotonic() < start_deadline:
                time.sleep(0.025)
            if not job_started_path.is_file():
                raise TimeoutError("annual_api_job_start_not_published")
            job_id = json.loads(job_started_path.read_text(encoding="utf-8"))["job_id"]
            job_state_path = project_root / "artifacts" / "annual-analysis-jobs" / str(job_id) / "job.json"
            blocked_stage = provider.wait_blocked(
                str(fixture["document_id"]), timeout=90, job_state_path=job_state_path,
            )
            if blocked_stage != cancel_at_stage:
                raise AssertionError(f"unexpected_loopback_block_stage:{blocked_stage}")
            (case_dir / "cancel.signal").write_text("cancel requested by integration harness\n", encoding="utf-8")
        deadline = time.monotonic() + 900
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            raise TimeoutError("api_driver_timeout")
        stdout, stderr = process.communicate(timeout=10)
    finally:
        provider.release_document(str(fixture["document_id"]))
        if process.poll() is None:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
    (case_dir / "driver.stdout.raw").write_bytes(stdout)
    (case_dir / "driver.stderr.raw").write_bytes(stderr)
    elapsed = time.monotonic() - started
    if process.returncode != 0:
        raise AssertionError(
            f"api_driver_failed:{fixture['document_id']}:{process.returncode}; "
            f"stdout={stdout.decode('utf-8', errors='replace')[-2000:]}; "
            f"stderr={stderr.decode('utf-8', errors='replace')[-4000:]}"
        )
    result_path = case_dir / "driver-result.json"
    if not result_path.is_file():
        raise AssertionError("api_driver_result_missing")
    outcome = json.loads(result_path.read_text(encoding="utf-8"))
    outcome["test_harness"] = {
        "case_directory": str(case_dir),
        "cancel_stage": cancel_at_stage,
        "observed_block_stage": blocked_stage,
        "monotonic_elapsed_seconds": round(elapsed, 3),
        "external_provider_requests": 0,
    }
    if source_before != _sha256(Path(fixture["pdf"])):
        raise AssertionError(f"registered_parent_source_hash_changed:{fixture['document_id']}")
    return outcome


def _isolated_environment(project_root: Path, base_url: str) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if os.environ.get(key)
    }
    environment.update({
        "PYTHONPATH": str(project_root / "backend" / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "FINTRACE_R4_LOOPBACK_BASE_URL": base_url,
    })
    if any(key.upper().startswith("MODEL_") for key in environment):
        raise AssertionError("real_model_environment_leaked_into_isolated_api_driver")
    return environment


def _write_dummy_model_settings(path: Path, model_name: str, base_url: str) -> None:
    values = {
        "MODEL_BASE_URL": base_url,
        "MODEL_NAME": model_name,
        "MODEL_API_KEY": _DUMMY_KEY,
    }
    payload = "\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items()) + "\n"
    if path.exists():
        raise FileExistsError("dummy_model_configuration_must_be_new")
    path.write_text(payload, encoding="utf-8", newline="\n")


def _assert_durable_tail_false(outcome: Mapping[str, Any]) -> None:
    report = outcome["strict_archive_report"]
    pipeline = report["model_issue_pipeline"]
    coverage = pipeline["retrieval_coverage"]
    assert outcome["strict_reader"]["loaded"] is True
    assert outcome["core_capability"] == {"can_resume": False, "reason": "recovery_nothing_to_resume"}
    assert outcome["public_capability"]["can_resume"] is False
    assert outcome["public_capability"]["reason"] == "recovery_not_available"
    assert outcome["public_direct_capability"] == {"can_resume": False, "reason": "recovery_not_available"}
    assert outcome["terminal_state"] == "completed_with_issues"
    assert coverage["role_assignments_total"] == 40
    assert coverage["role_assignments_requested"] == 32
    assert coverage["role_assignments_pending"] == 8
    supplements = [item for item in pipeline["shards"] if item.get("kind") == "supplement"]
    assert len(supplements) == 1 and supplements[0]["status"] == "accepted"
    requested_pairs = [
        (item["issue_id"], item["evidence_id"])
        for item in supplements[0]["input"]["evidence_assignments"]
    ]
    returned_pairs = [
        (item["issue_id"], item["evidence_id"])
        for item in supplements[0]["result"]["roles"]
    ]
    returned_roles = [
        (item["issue_id"], item["evidence_id"], item["role"])
        for item in supplements[0]["result"]["roles"]
    ]
    persisted_roles = [
        (role["issue_id"], item["evidence_id"], role["role"])
        for item in pipeline["supplements"]
        for role in item["issue_roles"]
    ]
    all_pairs = [
        (issue_id, item["evidence_id"])
        for item in pipeline["supplements"]
        for issue_id in item["issue_ids"]
    ]
    assert len(all_pairs) == 40 and len(set(all_pairs)) == 40
    assert len(requested_pairs) == len(returned_pairs) == 32
    assert len(set(returned_pairs)) == 32 and set(returned_pairs) == set(requested_pairs)
    assert len(returned_roles) == len(persisted_roles) == 32
    assert all(role == "context" for _issue_id, _evidence_id, role in returned_roles)
    assert set(returned_roles) == set(persisted_roles)
    assert requested_pairs == all_pairs[:32]
    assert len(all_pairs[32:]) == 8 and not set(all_pairs[32:]) & set(requested_pairs)
    assert report["model_discovery"]["status"] == "completed"
    assert outcome["review_request_limit_chars"] == 24_000
    review_events = [item for item in outcome["provider_events"] if item.get("stage") == "review"]
    if report["model_review"]["status"] == "completed":
        _assert_review_v4_audit_roundtrip(outcome)
        assert len(review_events) == outcome["call_gate_stage_admissions"].get("annual_review") == 1
        assert outcome["review_request_chars"] <= outcome["review_request_limit_chars"]
    else:
        assert report["model_review"]["status"] == "abstained"
        assert report["model_review"]["reason"] == "annual_review_v3_request_budget_exceeded"
        assert outcome["review_request_chars"] > outcome["review_request_limit_chars"]
        assert not review_events
        assert outcome["call_gate_stage_admissions"].get("annual_review", 0) == 0
        assert outcome["review_audit_roundtrip"]["mode"] == "honest_budget_abstention"
    assert outcome["resume_rejection"]["child_created"] is False
    supplement_events = [item for item in outcome["provider_events"] if item.get("stage") == "supplement"]
    assert len(supplement_events) == 1


def _assert_review_v4_audit_roundtrip(outcome: Mapping[str, Any]) -> None:
    report = outcome["strict_archive_report"]
    review = report["model_review"]
    proof = outcome["review_audit_roundtrip"]
    events = [item for item in outcome["provider_events"] if item.get("stage") == "review"]
    admissions = outcome["call_gate_stage_admissions"].get("annual_review", 0)
    assert review["status"] == "completed"
    assert review["model_called"] is True
    assert review["call_count"] == 1 and review["call_attempt_count"] == 1
    assert len(review["audit_artifacts"]) == 1
    assert len(events) == admissions == 1
    assert proof["mode"] == "actual_http_audit_projection_hash_roundtrip"
    assert proof["strict_reader_loaded"] is True
    assert proof["wire_schema_version"] == "annual_model_review_v3_compact_v4"
    assert proof["projection_version"] == "annual_review_v3_purpose_projection_v1"
    assert proof["request_body_sha256"] == outcome["review_request_body_sha256"]
    assert events[0]["request_body_sha256"] == proof["request_body_sha256"]
    assert events[0]["input_envelope_schema"] == proof["wire_schema_version"]
    assert events[0]["review_projection_version"] == proof["projection_version"]
    assert proof["request_body_sha256"] == review["audit_artifacts"][0]["request_body_sha256"]
    assert proof["semantic_input_sha256"] == review["audit_artifacts"][0]["semantic_input_sha256"]
    assert proof["projected_input_sha256"] == review["audit_artifacts"][0]["projected_input_sha256"]
    assert proof["limitations_required_preserved"] is True


def _assert_durable_review_budget_boundary(outcome: Mapping[str, Any]) -> None:
    report = outcome["strict_archive_report"]
    review = report["model_review"]
    review_events = [item for item in outcome["provider_events"] if item.get("stage") == "review"]
    review_admissions = outcome["call_gate_stage_admissions"].get("annual_review", 0)
    assert outcome["strict_reader"]["loaded"] is True
    assert outcome["review_request_limit_chars"] == 24_000
    assert outcome["core_capability"] == {"can_resume": False, "reason": "recovery_nothing_to_resume"}
    assert outcome["public_capability"]["can_resume"] is False
    assert outcome["public_direct_capability"] == {"can_resume": False, "reason": "recovery_not_available"}
    assert outcome["resume_rejection"]["child_created"] is False
    assert report["model_discovery"]["status"] == "completed"
    pending = [item for item in report["model_issue_pipeline"]["shards"] if item.get("status") == "pending"]
    pending_sizes = outcome["model_issue_pending_wire_sizes"]
    assert len(pending) == len(pending_sizes)
    assert all(item.get("reason") == "request_size_exceeded" for item in pending)
    assert all(item["request_chars"] > item["limit_chars"] for item in pending_sizes)
    if outcome["review_request_chars"] <= outcome["review_request_limit_chars"]:
        _assert_review_v4_audit_roundtrip(outcome)
        assert review["status"] == "completed"
        assert review_admissions == 1 and len(review_events) == 1
    else:
        assert outcome["review_request_chars"] > outcome["review_request_limit_chars"]
        assert review["status"] == "abstained"
        assert review["reason"] == "annual_review_v3_request_budget_exceeded"
        assert review["model_called"] is False
        assert review["call_count"] == 0 and review["call_attempt_count"] == 0
        assert review["audit_artifacts"] == []
        assert not review_events and review_admissions == 0
        assert outcome["review_audit_roundtrip"]["mode"] == "honest_budget_abstention"
        assert outcome["review_audit_roundtrip"]["strict_reader_loaded"] is True


def _assert_durable_in_cap_true(outcome: Mapping[str, Any]) -> None:
    pipeline = outcome["strict_archive_report"]["model_issue_pipeline"]
    assert outcome["strict_reader"]["loaded"] is True
    assert outcome["core_capability"]["can_resume"] is True
    assert outcome["public_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
    assert outcome["public_direct_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
    assert any(
        item.get("kind") == "grouping" and item.get("status") == "pending"
        and item.get("reason") == "typed_response_invalid"
        for item in pipeline["shards"]
    )
    assert pipeline["global_closure"]["closed"] is False
    assert outcome["terminal_state"] == "completed_with_issues"
    assert outcome["review_request_chars"] <= outcome["review_request_limit_chars"]
    _assert_review_v4_audit_roundtrip(outcome)


def _assert_durable_missing_plan_true(outcome: Mapping[str, Any], kind: str) -> None:
    report = outcome["strict_archive_report"]
    checkpoint = outcome["cancel_checkpoint"]
    expected_stage = {"grouping": "grouping", "global": "global", "supplement": "supplement"}[kind]
    expected_schema = {
        "grouping": "annual_issue_group_shard_input_v1",
        "global": "annual_issue_global_input_v1",
        "supplement": "annual_issue_supplement_input_v1",
    }[kind]
    assert checkpoint["confirmed"] is True
    assert checkpoint["expected_stage"] == expected_stage
    assert checkpoint["job_status"] == "cancelled" and checkpoint["job_stage"] == "cancelled"
    assert checkpoint["manifest_status"] == "interrupted"
    assert checkpoint["archive_snapshot_state"] == "cancelled"
    assert checkpoint["archive_snapshot_is_partial"] is True
    assert checkpoint["safe_partial_report"] is True
    assert checkpoint["issue_pipeline_persisted"] is True
    assert checkpoint["model_review_present"] is True
    assert checkpoint["review_not_started"] is True
    assert checkpoint["review_status"] == "not_called"
    assert checkpoint["review_reason"] == "model_review_not_started"
    assert checkpoint["review_audit_artifact_count"] == 0
    assert checkpoint["review_admission_count"] == 0
    assert checkpoint["review_audit_reference_count"] == 0
    assert outcome["review_audit_roundtrip"]["mode"] == "explicit_stage_cancellation_before_review"
    assert outcome["review_request_chars"] is None
    assert outcome["review_request_body_sha256"] is None
    review_events = [item for item in outcome["provider_events"] if item.get("stage") == "review"]
    target_events = [item for item in outcome["provider_events"] if item.get("stage") == expected_stage]
    assert not review_events
    assert outcome["call_gate_stage_admissions"].get("annual_review", 0) == 0
    assert outcome["review_audit_roundtrip"]["review_audit_artifact_count"] == 0
    assert len(target_events) == 1
    assert target_events[0].get("decoded_business_schema_version") == expected_schema
    assert target_events[0].get("input_envelope_schema") == "annual_issue_input_compact_v1"
    assert outcome["test_harness"]["cancel_stage"] == expected_stage
    assert outcome["test_harness"]["observed_block_stage"] == expected_stage
    pipeline = report["model_issue_pipeline"]
    assert outcome["strict_reader"]["loaded"] is True
    assert outcome["core_capability"]["can_resume"] is True
    assert outcome["public_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
    assert outcome["public_direct_capability"] == {"can_resume": True, "reason": "recoverable_batches"}
    assert outcome["terminal_state"] == "cancelled"
    planned = [item for item in pipeline["shards"] if item.get("kind") == kind]
    assert not any(item.get("status") == "accepted" for item in planned)
    assert all(item.get("status") == "pending" and item.get("result") is None for item in planned)
    if kind == "grouping":
        assert pipeline["candidates"] and pipeline["global_closure"]["closed"] is False
    elif kind == "global":
        groups = [item for item in pipeline["shards"] if item.get("kind") == "grouping"]
        assert len(groups) >= 2 and all(item.get("status") == "accepted" for item in groups)
        assert pipeline["global_closure"]["closed"] is False
    else:
        assert pipeline["global_closure"]["closed"] is True
        assert pipeline["retrieval_coverage"]["role_assignments_requested"] > 0
        assert any(item.get("role_status") == "pending" for item in pipeline["supplements"])


def _run_binding_drift_checks(
    project_root: Path,
    outcome: Mapping[str, Any],
    fixture: Mapping[str, Any],
    model_file: Path,
    provider: "_LoopbackProvider",
    attempt: Path,
) -> dict[str, Any]:
    cli_capture_dir = attempt / "binding-drift-cli-capture"
    cli_capture_dir.mkdir(parents=True, exist_ok=False)
    control = {
        "project_root": str(project_root),
        "job_id": outcome["job_id"],
        "run_id": outcome["run_id"],
        "request": {key: fixture[key] for key in (
            "company_id", "report_year", "document_id", "source_pdf_path", "sha256",
        )} | {"mode": "model_investigation"},
        "model_file": str(model_file),
        "expected_model_name": "r4-dummy-model-a",
        "alt_model_name": "r4-dummy-model-b",
        "provider_base_url": provider.base_url,
        "cli_capture_dir": str(cli_capture_dir),
    }
    control_path = project_root.parent.parent / "binding-drift-control.json"
    result_path = project_root.parent.parent / "binding-drift-result.json"
    _write_json_new(control_path, control)
    provider_event_count_before = len(provider.events())
    environment = _isolated_environment(project_root, provider.base_url)
    environment["FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR"] = str(cli_capture_dir)
    process = subprocess.run(
        [sys.executable, "-u", "-c", _DRIFT_DRIVER, str(control_path), str(result_path)],
        cwd=project_root,
        env=environment,
        capture_output=True,
        check=False,
        timeout=240,
    )
    provider_event_count_after = len(provider.events())
    if process.returncode != 0 or not result_path.is_file():
        raise AssertionError(
            "binding_drift_driver_failed:"
            + process.stderr.decode("utf-8", errors="replace")[-4000:]
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["provider_event_count_before"] = provider_event_count_before
    result["provider_event_count_after"] = provider_event_count_after
    result["provider_events_unchanged"] = provider_event_count_before == provider_event_count_after
    result["actual_cli_identity_capture_files"] = sorted(path.name for path in cli_capture_dir.glob("*.json"))
    if not result["provider_events_unchanged"]:
        raise AssertionError("binding_drift_api_resume_made_a_loopback_provider_request")
    if result["actual_cli_identity_capture_files"]:
        raise AssertionError("binding_drift_api_resume_started_an_actual_analysis_CLI_child")
    expected_drifts = {
        "source", "model", "review_prompt", "grouping_prompt",
        "review_wire_version", "review_projection_version",
    }
    assert set(result.get("drifts", {})) == expected_drifts
    assert result["baseline"]["strict_reader"]["loaded"] is True
    assert result["baseline"]["core_capability"]["can_resume"] is True
    assert result["baseline"]["api_get_status_code"] == 200
    assert result["baseline"]["api_get_capability"]["can_resume"] is True
    for name, row in result.get("drifts", {}).items():
        assert isinstance(row["strict_reader"].get("loaded"), bool), name
        assert row["api_get"]["status_code"] == 200
        assert row["api_get"]["recovery_capability"]["can_resume"] is False, name
        assert row["core_capability"]["can_resume"] is False, name
        assert row["api_resume"]["status_code"] == 409, name
        assert row["api_resume"]["child_job_directories_unchanged"] is True, name
        assert row["side_effects"]["job_directories_unchanged"] is True, name
        assert row["side_effects"]["call_gate_counts_unchanged"] is True, name
        assert row["side_effects"]["cli_capture_unchanged"] is True, name
    assert result["parent_archive_files_unchanged"] is True
    assert result["snapshot_source_manifest_restored"] is True
    assert result["parent_job_state_restored"] is True
    assert result["parent_job_state_sha256_before"] == result["parent_job_state_sha256_after"]
    assert result["source_sha256_expected"] == fixture["sha256"]
    assert result["source_sha256_before"] == fixture["sha256"]
    assert result["restored_source_sha256"] == fixture["sha256"]
    assert result["source_pdf_path"] == "data/raw/" + fixture["source_pdf_path"]
    return result


def _read_only_sqlite_counts(artifacts_root: Path) -> list[dict[str, Any]]:
    counts = []
    for path in sorted(artifacts_root.rglob("*.sqlite3")):
        uri = "file:" + quote(path.resolve().as_posix(), safe="/:\\") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            table_counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tables
            }
            counts.append({"path": str(path), "table_row_counts": table_counts})
        finally:
            connection.close()
    return counts


def _write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"evidence_must_be_unique:{path.name}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _write_new(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _case_prefix(document_id: str) -> str:
    return document_id.split("-", 1)[0]


_ACTUAL_CLI_IDENTITY_OBSERVER = r'''from __future__ import annotations
import json, os, sys
from pathlib import Path

def _capture_actual_cli_identity(frame, event, value):
    if event != "return" or frame.f_code.co_name != "capture_cli_runtime_identity":
        return
    if frame.f_globals.get("__name__") != "finagent.core.annual_runtime_identity":
        return
    if Path(sys.argv[0]).name != "analyze_annual.py" or sys.argv[1:2] == ["--runtime-identity"]:
        return
    directory = os.environ.get("FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR")
    if not directory or not isinstance(value, dict):
        return
    target = Path(directory) / ("actual-cli-runtime-identity-" + str(os.getpid()) + ".json")
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({
                "process_id": os.getpid(),
                "argv": list(sys.argv),
                "runtime_identity": value,
            }, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        return

sys.setprofile(_capture_actual_cli_identity)
'''


class _LoopbackProvider:
    """Controlled OpenAI-compatible fake bound only to 127.0.0.1."""

    credentials_removed_marker = "provider credential and proxy variables stripped"

    def __init__(
        self,
        *,
        issue_input_decoder: Callable[[object], dict[str, Any]],
        issue_wire_schema: str,
        review_input_decoder: Callable[[object], dict[str, Any]],
        review_wire_schema: str,
        review_projection_version: str,
        codec_module_path: Path,
        output_dir: Path,
    ) -> None:
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._blocks: dict[str, threading.Event] = {}
        self._release: dict[str, threading.Event] = {}
        self._issue_input_decoder = issue_input_decoder
        self._issue_wire_schema = issue_wire_schema
        self._review_input_decoder = review_input_decoder
        self._review_wire_schema = review_wire_schema
        self._review_projection_version = review_projection_version
        self.codec_module_path = codec_module_path
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=False)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    payload = json.loads(raw.decode("utf-8"))
                    event, user_value = _classify_chat_payload(
                        payload,
                        len(raw),
                        issue_input_decoder=owner._issue_input_decoder,
                        issue_wire_schema=owner._issue_wire_schema,
                        review_input_decoder=owner._review_input_decoder,
                        review_wire_schema=owner._review_wire_schema,
                        review_projection_version=owner._review_projection_version,
                        request_body_sha256=hashlib.sha256(raw).hexdigest(),
                    )
                except Exception:
                    self.send_error(400)
                    return
                with owner._lock:
                    event["sequence"] = len(owner._events) + 1
                    event["peer"] = self.client_address[0]
                    event["auth_header_present"] = bool(self.headers.get("Authorization"))
                    owner._events.append(dict(event))
                    block = _GROUP_MISSING_STAGE.get(_case_prefix(str(event.get("document_id", ""))))
                if block == event["stage"] and event.get("stage") in {"grouping", "global", "supplement"}:
                    owner._event_for(str(event.get("document_id"))).set()
                    release = owner._release_for(str(event.get("document_id")))
                    release.wait(timeout=300)
                response_text = _fake_model_response(event, user_value)
                body = json.dumps({
                    "id": f"chatcmpl-r4-{event['sequence']}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": _MODEL,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop"}],
                }, ensure_ascii=False).encode("utf-8")
                owner._save_exchange(int(event["sequence"]), raw, body)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    owner._mark_response(int(event["sequence"]), 200)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    owner._mark_response(int(event["sequence"]), "client_disconnected")
                finally:
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def _event_for(self, document_id: str) -> threading.Event:
        with self._lock:
            return self._blocks.setdefault(document_id, threading.Event())

    def _release_for(self, document_id: str) -> threading.Event:
        with self._lock:
            return self._release.setdefault(document_id, threading.Event())

    def wait_blocked(self, document_id: str, *, timeout: float, job_state_path: Path) -> str:
        event = self._event_for(document_id)
        deadline = time.monotonic() + timeout
        terminal_states = {"completed", "completed_with_issues", "failed", "interrupted", "cancelled"}
        while time.monotonic() < deadline:
            if event.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
                break
            try:
                job_state = json.loads(job_state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = job_state.get("status")
            if status in terminal_states:
                raise RuntimeError(
                    f"loopback_request_not_reached_before_job_terminal:{document_id}:{status}"
                )
        else:
            raise TimeoutError(f"local_model_request_not_blocked:{document_id}")
        with self._lock:
            for item in self._events:
                if item.get("document_id") == document_id and _GROUP_MISSING_STAGE.get(_case_prefix(document_id)) == item.get("stage"):
                    return str(item["stage"])
        raise AssertionError("blocked_request_event_missing")

    def release_document(self, document_id: str) -> None:
        self._release_for(document_id).set()

    def events_for(self, document_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._events if item.get("document_id") == document_id]

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._events]

    def _mark_response(self, sequence: int, status: object) -> None:
        with self._lock:
            for item in self._events:
                if item.get("sequence") == sequence:
                    item["response_status"] = status
                    break

    def _save_exchange(self, sequence: int, request_body: bytes, response_body: bytes) -> None:
        stem = f"exchange-{sequence:04d}"
        request_name = stem + ".request.raw.json"
        response_name = stem + ".response.raw.json"
        _write_new(self.output_dir / request_name, request_body)
        _write_new(self.output_dir / response_name, response_body)
        with self._lock:
            for item in self._events:
                if item.get("sequence") == sequence:
                    item["request_body_file"] = request_name
                    item["response_body_file"] = response_name
                    item["response_body_sha256"] = hashlib.sha256(response_body).hexdigest()
                    break

    def close(self) -> None:
        for document_id in list(self._blocks):
            self.release_document(document_id)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        _write_json_new(self.output_dir / "exchange-index.json", {
            "schema_version": "r4_strict_loopback_http_exchange_index_v1",
            "bind_host": "127.0.0.1",
            "external_provider_calls": 0,
            "request_count": len(self.events()),
            "requests": self.events(),
        })


def _classify_chat_payload(
    payload: object,
    body_bytes: int,
    *,
    issue_input_decoder: Callable[[object], dict[str, Any]],
    issue_wire_schema: str,
    review_input_decoder: Callable[[object], dict[str, Any]],
    review_wire_schema: str,
    review_projection_version: str,
    request_body_sha256: str,
) -> tuple[dict[str, Any], object]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("messages"), list):
        raise ValueError("chat_payload_invalid")
    messages = payload["messages"]
    user = next(item for item in messages if isinstance(item, Mapping) and item.get("role") == "user")
    content = user.get("content")
    if not isinstance(content, str):
        raise ValueError("chat_user_content_invalid")
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        value = {}
    envelope_schema = value.get("schema_version") if isinstance(value, Mapping) else None
    if envelope_schema == issue_wire_schema:
        value = issue_input_decoder(value)
    elif envelope_schema == review_wire_schema:
        value = review_input_decoder(value)
    stage = "unknown"
    if isinstance(value, Mapping) and type(value.get("batch_index")) is int:
        stage = "discovery"
    elif isinstance(value, Mapping) and value.get("schema_version") == "annual_issue_group_shard_input_v1":
        stage = "grouping"
    elif isinstance(value, Mapping) and value.get("schema_version") == "annual_issue_global_input_v1":
        stage = "global"
    elif isinstance(value, Mapping) and value.get("schema_version") == "annual_issue_supplement_input_v1":
        stage = "supplement"
    elif envelope_schema == review_wire_schema:
        stage = "review"
    elif isinstance(value, Mapping) and isinstance(value.get("candidate"), Mapping):
        stage = "investigation"
    document_id = _find_string(value, lambda item: item.startswith("r4") and "-" in item)
    event = {
        "stage": stage,
        "document_id": document_id,
        "model": payload.get("model"),
        "request_body_sha256": request_body_sha256,
        "request_body_bytes": body_bytes,
        "batch_index": value.get("batch_index") if isinstance(value, Mapping) else None,
        "input_envelope_schema": envelope_schema,
        "review_projection_version": review_projection_version if envelope_schema == review_wire_schema else None,
        "decoded_business_schema_version": value.get("schema_version") if isinstance(value, Mapping) else None,
    }
    return event, value


def _find_string(value: object, predicate: Any) -> str | None:
    if isinstance(value, str):
        return value if predicate(value) else None
    if isinstance(value, Mapping):
        for key, child in value.items():
            found = _find_string(key, predicate)
            if found is not None:
                return found
            found = _find_string(child, predicate)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_string(child, predicate)
            if found is not None:
                return found
    return None


def _fake_model_response(event: Mapping[str, Any], payload: object) -> str:
    stage = event.get("stage")
    prefix = _case_prefix(str(event.get("document_id", "")))
    if stage == "discovery":
        if not isinstance(payload, Mapping):
            return json.dumps({"findings": [], "limitations": []})
        count = _finding_count_for_batch(prefix, int(payload.get("batch_index", 0)))
        fragments = payload.get("fragments")
        if not isinstance(fragments, list) or not fragments:
            return json.dumps({"findings": [], "limitations": []})
        first = fragments[0]
        if not isinstance(first, list) or len(first) != 3:
            return json.dumps({"findings": [], "limitations": []})
        alias, _page, source_text = first
        rows = []
        for index in range(count):
            ordinal = _chinese_ordinal(int(payload.get("batch_index", 0)) * 3 + index + 1)
            if prefix == "r4tail":
                title = f"合同回款账龄披露核对{ordinal}"
            elif prefix == "r4supp":
                title = f"合同核对{ordinal}"
            else:
                title = f"人工构造的流程线索{ordinal}"
            if prefix == "r4global":
                title = f"测试事项{ordinal}"
            observation = f"人工构造的台账边界观察片段{ordinal}。"
            why = "记录口径需要与来源文字逐项核对。"
            follow_up = "核查相关记录的形成过程和适用范围。"
            if prefix in {"r4global", "r4supp"}:
                observation = "虚构线索。"
                why = "仍需核实。"
                follow_up = "核对原文。"
            if prefix == "r4over":
                observation = observation + "文字记录仅用于检查完整输入保留。" * 7
                why = why + "同一来源文本不提供因果证明。" * 4
                follow_up = follow_up + "请按来源坐标核对期间、边界、口径和形成过程。" * 3
            rows.append({
                "title": title,
                "observation": observation,
                "why_it_matters": why,
                "follow_up": follow_up,
                "priority": "low",
                "citations": [{"fragment_alias": alias, "quote": str(source_text)[:4 if prefix in {"r4global", "r4supp"} else 12]}],
            })
        return json.dumps({"findings": rows, "limitations": []}, ensure_ascii=False)
    if stage == "investigation":
        candidate = payload.get("candidate", {}) if isinstance(payload, Mapping) else {}
        return json.dumps({
            "signal_id": candidate.get("signal_id", "unknown"),
            "explanation": "本次只测试本地模型响应解析。",
            "alternative_explanations": [],
            "limitations": ["输入为人工构造的软件测试材料。"],
            "narrative_evidence_ids": [],
            "request_more_context": False,
        }, ensure_ascii=False)
    if stage == "grouping" and prefix == "r4incap":
        return json.dumps({"schema_version": "wrong_local_schema", "decisions": []}, ensure_ascii=False)
    if stage in {"grouping", "global"} and isinstance(payload, Mapping):
        field = "input_member_finding_ids" if stage == "grouping" else "input_issue_ids"
        id_field = "finding_id" if stage == "grouping" else "issue_id"
        target_field = "target_finding_id" if stage == "grouping" else "target_issue_id"
        reason = "distinct_issue" if stage == "grouping" else "distinct_across_shards"
        decisions = [{
            id_field: item,
            "disposition": "retain",
            target_field: None,
            "reason_code": reason,
            "reason_evidence_id": None,
            "reason_quote": None,
        } for item in payload.get(field, [])]
        return json.dumps({
            "schema_version": "annual_issue_group_shard_result_v1" if stage == "grouping" else "annual_issue_global_result_v1",
            "decisions": decisions,
        }, ensure_ascii=False)
    if stage == "supplement" and isinstance(payload, Mapping):
        roles = [{
            "issue_id": item["issue_id"],
            "evidence_id": item["evidence_id"],
            "role": "context",
            "reason_code": "context_only",
        } for item in payload.get("evidence_assignments", [])]
        return json.dumps({"schema_version": "annual_issue_supplement_result_v1", "roles": roles}, ensure_ascii=False)
    if stage == "review":
        required = payload.get("limitations_required", []) if isinstance(payload, Mapping) else None
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            return json.dumps({
                "assessment": "insufficient_evidence",
                "summary": "受控评审请求的必需限制字段无效。",
                "reasons": [],
                "follow_up_items": [],
                "limitations": [],
            }, ensure_ascii=False)
        return json.dumps({
            "assessment": "insufficient_evidence",
            "summary": "本次只检查合成软件测试材料。",
            "reasons": [],
            "follow_up_items": [],
            "limitations": required,
        }, ensure_ascii=False)
    return json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)


def _finding_count_for_batch(prefix: str, batch_index: int) -> int:
    counts: dict[str, list[int]] = {
        "r4tail": [3, 3, 3, 1],
        "r4over": [3, 3, 3, 3, 3, 3, 3, 3],
        "r4incap": [2],
        "r4local": [2],
        "r4global": [3, 3, 3, 3, 1],
        "r4supp": [3, 3, 3, 3, 1],
    }
    selected = counts.get(prefix, [])
    return selected[batch_index - 1] if 1 <= batch_index <= len(selected) else 0


def _chinese_ordinal(value: int) -> str:
    names = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
    index = max(0, value - 1)
    if index < len(names):
        return names[index]
    return names[index // len(names)] + names[index % len(names)]


_API_DRIVER = r'''from __future__ import annotations
import hashlib, json, os, sqlite3, subprocess, sys, threading, time
from pathlib import Path
from finagent.api.annual_analysis_jobs import AnnualAnalysisJobError, AnnualAnalysisJobService, _result_availability_projection
from finagent.api.annual_report_read import annual_issue_supplement_evidence_index, load_annual_report
from finagent.core.annual_recovery import validate_annual_recovery_parent
from finagent.core.model_settings_store import resolve_model_settings
from finagent.core.annual_runtime_identity import runtime_identity_is_valid

control = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(control["project_root"]).resolve()
case_dir = Path(control["case_dir"]).resolve()
service = AnnualAnalysisJobService(root)
service_dir = root / "artifacts" / "annual-analysis-jobs"
original_invoke = __import__("finagent.api.annual_analysis_jobs", fromlist=["_invoke_cli"])._invoke_cli
job_module = __import__("finagent.api.annual_analysis_jobs", fromlist=["_invoke_cli"])
real_popen = subprocess.Popen
argv_rows = []
class RecordingPopen(real_popen):
    def __init__(self, args, *positional, **kwargs):
        values = [str(item) for item in args] if isinstance(args, (list, tuple)) else [str(args)]
        # The API service also launches the same script for startup identity probes.
        # Count only the Popen that actually starts an analysis with a source PDF.
        is_annual_cli = "--source-pdf" in values
        super().__init__(args, *positional, **kwargs)
        if is_annual_cli:
            argv_rows.append({"argv": values, "pid": self.pid})
            (case_dir / "cli-argv.json").write_text(json.dumps({"processes": argv_rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
job_module.subprocess.Popen = RecordingPopen
def recording_invoke(*args, **kwargs):
    result = original_invoke(*args, **kwargs)
    (case_dir / "cli.stdout.raw").write_bytes(result.stdout.encode("utf-8"))
    (case_dir / "cli.stderr.raw").write_bytes(result.stderr.encode("utf-8"))
    (case_dir / "cli-exit.json").write_text(json.dumps({"returncode": result.returncode}, indent=2) + "\n", encoding="utf-8")
    return result
job_module._invoke_cli = recording_invoke
cancel_signal = case_dir / "cancel.signal"
cancel_started = case_dir / "cancel-started.json"
watcher_done = threading.Event()
job_id = None
cancel_error = None
def cancel_watcher():
    global cancel_error
    while not watcher_done.is_set():
        if cancel_signal.exists() and job_id:
            cancel_started.write_text(json.dumps({"job_id": job_id, "monotonic": time.monotonic()}), encoding="utf-8")
            try:
                service.cancel(job_id)
            except Exception as exc:
                cancel_error = type(exc).__name__
            return
        time.sleep(0.025)
request = control["request"]
job = service.create(request)
job_id = str(job["job_id"])
(case_dir / "job-started.json").write_text(json.dumps({"job_id": job_id}, indent=2) + "\n", encoding="utf-8")
watcher = None
if control.get("cancel_at_stage"):
    watcher = threading.Thread(target=cancel_watcher, daemon=True)
    watcher.start()
deadline = time.monotonic() + 850
terminal = {"completed", "completed_with_issues", "failed", "interrupted", "cancelled"}
while time.monotonic() < deadline:
    state_path = service_dir / job_id / "job.json"
    try:
        current = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        time.sleep(0.05); continue
    if current.get("status") in terminal:
        break
    time.sleep(0.075)
else:
    raise TimeoutError("annual_api_job_timeout")
watcher_done.set()
if watcher is not None:
    watcher.join(timeout=5)
final = service.get(job_id)
run_id = final.get("run_id")
if not isinstance(run_id, str):
    raise RuntimeError("terminal_api_job_missing_run_id")
run_dir = root / "artifacts" / "runs" / run_id
manifest_path = run_dir / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
runtime_api = service.api_runtime_identity
runtime_cli_probe = service.cli_startup_identity
run_runtime = manifest.get("runtime_identity")
observer_paths = sorted(case_dir.glob("actual-cli-runtime-identity-*.json"))
actual_cli_capture = json.loads(observer_paths[0].read_text(encoding="utf-8")) if len(observer_paths) == 1 else None
(case_dir / "runtime-loaded-identities.json").write_text(json.dumps({
    "api_process": runtime_api,
    "cli_startup_probe": runtime_cli_probe,
    "actual_cli_archive_manifest": run_runtime,
    "actual_cli_observer_capture": actual_cli_capture,
    "actual_cli_observer_capture_files": [str(path) for path in observer_paths],
    "actual_cli_popen_records": argv_rows,
}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
investigation_path = run_dir / "investigation.json"
(case_dir / "archive-paths-and-hashes.json").write_text(json.dumps({
    "run_id": run_id,
    "run_directory": str(run_dir),
    "manifest": {"path": str(manifest_path), "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()},
    "report_directory": str(root / "artifacts" / "reports" / run_id),
    "job_directory": str(service_dir / job_id),
    "investigation": {
        "path": str(investigation_path),
        "exists": investigation_path.is_file(),
        "sha256": hashlib.sha256(investigation_path.read_bytes()).hexdigest() if investigation_path.is_file() else None,
    },
}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
archive = load_annual_report(root, run_id)
try:
    supplement_index_probe = {"status": "returned", "result": annual_issue_supplement_evidence_index(archive)}
except Exception as exc:
    supplement_index_probe = {
        "status": "error",
        "exception_type": type(exc).__name__,
        "error_code": getattr(exc, "code", None),
        "http_status": getattr(exc, "status", getattr(exc, "status_code", None)),
        "message": str(exc),
    }
(case_dir / "supplement-index-read.json").write_text(json.dumps(supplement_index_probe, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
normalized = job_module.validate_job_request(root, request)
settings = resolve_model_settings(root)
core = validate_annual_recovery_parent(root, run_id, normalized, settings=settings, archive=archive, require_issue_pipeline_v5=True)
raw_state = json.loads((service_dir / job_id / "job.json").read_text(encoding="utf-8"))
public_direct = service._recovery_capability_projection(raw_state, final["result_availability"])
report = archive.report
manifest_path = root / "artifacts" / "runs" / run_id / "manifest.json"
if not runtime_identity_is_valid(runtime_api) or not runtime_identity_is_valid(runtime_cli_probe):
    raise RuntimeError("runtime_identity_capture_invalid")
if isinstance(run_runtime, dict) and not runtime_identity_is_valid(run_runtime):
    raise RuntimeError("actual_cli_runtime_identity_invalid")
expected_source_sha = control["expected_runtime_source_manifest_sha256"]
expected_source_count = control["expected_runtime_source_file_count"]
runtime_identity_errors = []
for label, identity in (("api", runtime_api), ("cli_startup_probe", runtime_cli_probe)):
    if (
        identity.get("source_manifest_sha256") != expected_source_sha
        or identity.get("source_file_count") != expected_source_count
    ):
        runtime_identity_errors.append(label + "_source_fingerprint_mismatch")
if len(argv_rows) != 1:
    runtime_identity_errors.append("actual_cli_popen_count_not_one")
else:
    cli_argv = argv_rows[0]["argv"]
    if (
        len(cli_argv) < 2
        or Path(cli_argv[1]).resolve() != (root / "scripts" / "analyze_annual.py").resolve()
        or "--source-pdf" not in cli_argv
        or "--runtime-identity" in cli_argv
    ):
        runtime_identity_errors.append("actual_cli_popen_path_not_snapshot")
if not isinstance(actual_cli_capture, dict):
    runtime_identity_errors.append("actual_cli_runtime_observer_capture_missing_or_ambiguous")
else:
    actual_cli_identity = actual_cli_capture.get("runtime_identity")
    if (
        not runtime_identity_is_valid(actual_cli_identity)
        or actual_cli_identity.get("role") != "cli"
        or actual_cli_identity.get("source_manifest_sha256") != expected_source_sha
        or actual_cli_identity.get("source_file_count") != expected_source_count
    ):
        runtime_identity_errors.append("actual_cli_loaded_identity_not_frozen_source")
    if len(argv_rows) == 1 and actual_cli_capture.get("process_id") != argv_rows[0].get("pid"):
        runtime_identity_errors.append("actual_cli_identity_pid_does_not_match_popen_child")
from finagent.agents.annual_review import _build_review_input_v3, build_annual_review_v3_request, V3_MAX_REQUEST_CHARS
from finagent.agents.annual_review_wire import decode_annual_review_v3_wire, V3_WIRE_SCHEMA, V3_INPUT_PROJECTION_VERSION
from finagent.llm.chat_completion import ChatMessage, serialize_chat_body
actual_review = report.get("model_review")
cancel_mode = control.get("cancel_at_stage")
archive_snapshot = archive.snapshot if isinstance(archive.snapshot, dict) else {}
cancel_checkpoint = None
review_chars = None
review_body_sha256 = None
review_audit_roundtrip = {
    "strict_reader_loaded": True,
    "wire_schema_version": V3_WIRE_SCHEMA,
    "projection_version": V3_INPUT_PROJECTION_VERSION,
}
if cancel_mode is not None:
    expected_stage_schemas = {
        "grouping": "annual_issue_group_shard_input_v1",
        "global": "annual_issue_global_input_v1",
        "supplement": "annual_issue_supplement_input_v1",
    }
    if cancel_mode not in expected_stage_schemas:
        raise RuntimeError("strict_cancel_mode_has_unknown_stage")
    cancel_started_path = case_dir / "cancel-started.json"
    cancel_started = json.loads(cancel_started_path.read_text(encoding="utf-8")) if cancel_started_path.is_file() else {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    confirmed = (
        cancel_signal.is_file()
        and cancel_started.get("job_id") == job_id
        and cancel_error is None
        and final.get("status") == "cancelled"
        and raw_state.get("status") == "cancelled"
        and raw_state.get("stage") == "cancelled"
        and manifest.get("status") == "interrupted"
        and archive_snapshot.get("state") == "cancelled"
        and archive_snapshot.get("is_partial") is True
    )
    if not confirmed:
        raise RuntimeError("strict_cancel_checkpoint_not_confirmed")
    confirmed_block = report.get("confirmed")
    issue_pipeline = report.get("model_issue_pipeline")
    review_audits = actual_review.get("audit_artifacts") if isinstance(actual_review, dict) else None
    review_audit_count = len(review_audits) if isinstance(review_audits, list) else None
    review_status = actual_review.get("status") if isinstance(actual_review, dict) else None
    review_reason = actual_review.get("reason") if isinstance(actual_review, dict) else None
    review_not_started = (
        isinstance(actual_review, dict)
        and review_status == "not_called"
        and review_reason == "model_review_not_started"
        and actual_review.get("model_called") is False
        and actual_review.get("call_count") == 0
        and actual_review.get("call_attempt_count") == 0
        and review_audits == []
    )
    safe_partial_report = (
        report.get("kind") == "fintrace_annual_analysis_report"
        and report.get("run_id") == run_id
        and report.get("source_document_id") == request["document_id"]
        and report.get("source_sha256") == request["sha256"]
        and report.get("model_called") is True
        and report.get("fraud_conclusion") is None
        and isinstance(confirmed_block, dict)
        and confirmed_block.get("analyses") == []
        and confirmed_block.get("metrics") == []
        and isinstance(report.get("pending_review"), dict)
        and isinstance(issue_pipeline, dict)
        and review_not_started
    )
    review_audit_roundtrip.update({
        "mode": "explicit_stage_cancellation_before_review",
        "review_status": review_status,
        "review_reason": review_reason,
        "review_model_called": actual_review.get("model_called") if isinstance(actual_review, dict) else None,
        "review_call_count": actual_review.get("call_count") if isinstance(actual_review, dict) else None,
        "review_call_attempt_count": actual_review.get("call_attempt_count") if isinstance(actual_review, dict) else None,
        "review_audit_artifact_count": review_audit_count,
    })
    cancel_checkpoint = {
        "confirmed": confirmed,
        "expected_stage": cancel_mode,
        "expected_wire_schema": expected_stage_schemas[cancel_mode],
        "archive_snapshot_state": archive_snapshot.get("state"),
        "archive_snapshot_is_partial": archive_snapshot.get("is_partial"),
        "cancel_signal_seen": cancel_signal.is_file(),
        "cancel_started_job_id_matches": cancel_started.get("job_id") == job_id,
        "cancel_request_error": cancel_error,
        "job_status": raw_state.get("status"),
        "job_stage": raw_state.get("stage"),
        "manifest_status": manifest.get("status"),
        "safe_partial_report": safe_partial_report,
        "issue_pipeline_persisted": isinstance(issue_pipeline, dict),
        "model_review_present": isinstance(actual_review, dict),
        "review_not_started": review_not_started,
        "review_status": review_status,
        "review_reason": review_reason,
        "review_model_called": actual_review.get("model_called") if isinstance(actual_review, dict) else None,
        "review_call_count": actual_review.get("call_count") if isinstance(actual_review, dict) else None,
        "review_call_attempt_count": actual_review.get("call_attempt_count") if isinstance(actual_review, dict) else None,
        "review_audit_artifact_count": review_audit_count,
    }
else:
    investigation = report["model_investigation"]
    prompt_path = root / "config" / "prompts" / "annual_review_v3.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    identity = report["model_issue_pipeline"]["source_identity"]
    review_value, _allowed, _refs, _no_priority = _build_review_input_v3(
        report, investigation, identity,
        request_model=settings.model, system_prompt=prompt, enforce_budget=False,
    )
    review_request = build_annual_review_v3_request(review_value, model=settings.model, system_prompt=prompt)
    review_chars = review_request["request_chars"]
    review_body_sha256 = review_request["request_body_sha256"]
    review_audit_roundtrip["request_body_sha256"] = review_body_sha256
    if not isinstance(actual_review, dict):
        raise RuntimeError("strict_archive_model_review_missing")
    if actual_review.get("status") == "completed":
        review_audits = actual_review.get("audit_artifacts")
        if not isinstance(review_audits, list) or len(review_audits) != 1:
            raise RuntimeError("completed_review_audit_count_invalid")
        audit = review_audits[0]
        audit_parts = str(audit.get("audit_dir", "")).split("/")
        if len(audit_parts) != 3 or audit_parts[:2] != ["runs", run_id]:
            raise RuntimeError("review_audit_directory_not_bound_to_current_run")
        audit_request_path = run_dir / audit_parts[2] / "request.json"
        audit_request = json.loads(audit_request_path.read_text(encoding="utf-8"))
        messages = audit_request.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            raise RuntimeError("strict_review_audit_messages_invalid")
        expected_messages = [{"role": message.role, "content": message.content} for message in review_request["messages"]]
        if messages != expected_messages or audit_request.get("model") != settings.model:
            raise RuntimeError("strict_review_audit_messages_differ_from_production_review_helper")
        envelope = json.loads(messages[1]["content"])
        decoded_projection = decode_annual_review_v3_wire(envelope)
        if envelope.get("schema_version") != V3_WIRE_SCHEMA or decoded_projection != review_request["projected_input"]:
            raise RuntimeError("strict_review_audit_wire_projection_roundtrip_failed")
        actual_body = serialize_chat_body(
            str(audit_request["model"]),
            [ChatMessage(role=item["role"], content=item["content"]) for item in messages],
        )
        actual_body_sha256 = hashlib.sha256(actual_body).hexdigest()
        required_limits = decoded_projection.get("limitations_required")
        returned_limits = actual_review.get("limitations")
        limitations_preserved = isinstance(required_limits, list) and isinstance(returned_limits, list) and set(required_limits).issubset(returned_limits)
        if (
            actual_body_sha256 != review_request["request_body_sha256"]
            or actual_body_sha256 != audit.get("request_body_sha256")
            or audit.get("semantic_input_sha256") != review_request["semantic_input_sha256"]
            or audit.get("projected_input_sha256") != review_request["projected_input_sha256"]
            or audit.get("review_wire_version") != V3_WIRE_SCHEMA
            or audit.get("review_input_projection_version") != V3_INPUT_PROJECTION_VERSION
            or not limitations_preserved
        ):
            raise RuntimeError("strict_review_audit_hash_or_version_binding_failed")
        review_audit_roundtrip.update({
            "mode": "actual_http_audit_projection_hash_roundtrip",
            "request_body_sha256": actual_body_sha256,
            "semantic_input_sha256": audit.get("semantic_input_sha256"),
            "projected_input_sha256": audit.get("projected_input_sha256"),
            "audit_request_path": str(audit_request_path),
            "audit_request_file_sha256": hashlib.sha256(audit_request_path.read_bytes()).hexdigest(),
            "limitations_required_count": len(required_limits),
            "limitations_required_preserved": True,
        })
    elif actual_review.get("status") == "abstained" and actual_review.get("reason") == "annual_review_v3_request_budget_exceeded":
        if actual_review.get("audit_artifacts") != []:
            raise RuntimeError("budget_abstained_review_has_audit")
        if review_chars <= V3_MAX_REQUEST_CHARS or actual_review.get("model_called") is not False:
            raise RuntimeError("budget_abstention_does_not_match_reconstructed_request_size")
        review_audit_roundtrip.update({"mode": "honest_budget_abstention", "limitations_required_preserved": None})
    else:
        raise RuntimeError("strict_model_review_is_neither_completed_nor_budget_abstained")
(case_dir / "review-audit-roundtrip.json").write_text(json.dumps(review_audit_roundtrip, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
call_gate_path = service_dir / job_id / "call-gate.sqlite3"
call_gate_stage_admissions = {}
if call_gate_path.is_file():
    connection = sqlite3.connect("file:" + call_gate_path.resolve().as_posix() + "?mode=ro", uri=True, timeout=3)
    try:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "provider_attempts" in table_names:
            columns = {row[1] for row in connection.execute('PRAGMA table_info("provider_attempts")')}
            if "stage" not in columns:
                raise RuntimeError("provider_attempts_stage_column_missing")
            call_gate_stage_admissions = {str(stage): int(count) for stage, count in connection.execute("SELECT stage, COUNT(*) FROM provider_attempts GROUP BY stage ORDER BY stage")}
    finally:
        connection.close()
if cancel_checkpoint is not None:
    cancel_checkpoint["review_admission_count"] = call_gate_stage_admissions.get("annual_review", 0)
    cancel_checkpoint["review_audit_reference_count"] = review_audit_roundtrip.get("review_audit_artifact_count", 0)
from finagent.agents import annual_issue_pipeline as issue_pipeline
pending_shard_wire_sizes = []
prompt_names = {
    "grouping": "annual_issue_grouping_v1.md",
    "global": "annual_issue_global_v1.md",
    "supplement": "annual_issue_supplement_v1.md",
}
request_limits = {
    "grouping": issue_pipeline.MAX_SHARD_REQUEST_CHARS,
    "global": issue_pipeline.MAX_GLOBAL_REQUEST_CHARS,
    "supplement": issue_pipeline.MAX_GLOBAL_REQUEST_CHARS,
}
for shard in report.get("model_issue_pipeline", {}).get("shards", []):
    kind = shard.get("kind") if isinstance(shard, dict) else None
    if not isinstance(shard, dict) or shard.get("status") != "pending" or kind not in prompt_names:
        continue
    prompt = (root / "config" / "prompts" / prompt_names[kind]).read_text(encoding="utf-8")
    request_input = shard.get("input")
    request_chars = issue_pipeline._wire_chars(settings.model, prompt, request_input)
    pending_shard_wire_sizes.append({
        "shard_id": shard.get("shard_id"), "kind": kind, "reason": shard.get("reason"),
        "request_chars": request_chars, "limit_chars": request_limits[kind],
        "request_body_sha256": issue_pipeline._wire_hash(settings.model, prompt, request_input),
    })
resume_rejection = None
if core.get("can_resume") is False:
    before_children = sorted(path.name for path in service_dir.iterdir() if path.is_dir())
    try:
        service.resume(job_id, run_id)
        created = True
        code = None
    except AnnualAnalysisJobError as exc:
        created = False
        code = exc.code
    after_children = sorted(path.name for path in service_dir.iterdir() if path.is_dir())
    resume_rejection = {"child_created": created, "error_code": code, "job_directories_unchanged": before_children == after_children}
service.shutdown()
def refs(value, output):
    if isinstance(value, dict):
        rows = value.get("audit_artifacts")
        if isinstance(rows, list):
            output.extend(dict(item) for item in rows if isinstance(item, dict))
        audit = value.get("audit")
        if isinstance(audit, dict): output.append(dict(audit))
        for child in value.values(): refs(child, output)
    elif isinstance(value, list):
        for child in value: refs(child, output)
audit_refs = []; refs(report, audit_refs)
seen = set(); audit_refs = [item for item in audit_refs if not ((item.get("audit_dir"), item.get("request_path")) in seen or seen.add((item.get("audit_dir"), item.get("request_path"))))]
result = {
    "job_id": job_id, "run_id": run_id, "terminal_state": final["status"],
    "strict_reader": {"loaded": True, "snapshot": archive.snapshot, "manifest_status": archive.manifest_summary.get("status")},
    "supplement_index_read": supplement_index_probe,
    "strict_archive_report": report,
    "core_capability": {key: core.get(key) for key in ("can_resume", "reason")},
    "public_capability": final["recovery_capability"],
    "public_direct_capability": public_direct,
    "result_availability": final["result_availability"],
    "resume_rejection": resume_rejection,
    "review_request_chars": review_chars,
    "review_request_limit_chars": V3_MAX_REQUEST_CHARS,
    "review_request_body_sha256": review_body_sha256,
    "review_audit_roundtrip": review_audit_roundtrip,
    "cancel_checkpoint": cancel_checkpoint,
    "call_gate_stage_admissions": call_gate_stage_admissions,
    "model_issue_pending_wire_sizes": pending_shard_wire_sizes,
    "audit_references": audit_refs,
    "run_directory": str(root / "artifacts" / "runs" / run_id),
    "report_directory": str(root / "artifacts" / "reports" / run_id),
    "job_directory": str(service_dir / job_id),
    "runtime_loaded": {
        "api": runtime_api,
        "cli_startup_probe": runtime_cli_probe,
        "actual_cli_archive_manifest": run_runtime,
        "actual_cli_observer_capture": actual_cli_capture,
    },
    "cli_argv_count": len(argv_rows),
    "cli_popen_records": argv_rows,
    "runtime_identity_errors": runtime_identity_errors,
    "cancel_error_type": cancel_error,
}
(case_dir / "driver-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if runtime_identity_errors:
    raise RuntimeError("runtime_identity_errors:" + ",".join(runtime_identity_errors))
'''


_DRIFT_DRIVER = r'''from __future__ import annotations
import hashlib, json, sqlite3, sys
from pathlib import Path
from urllib.parse import quote
from fastapi.testclient import TestClient
from finagent.api.annual_analysis_jobs import validate_job_request
from finagent.api.annual_report_read import load_annual_report
from finagent.api.app import create_app
from finagent.core.annual_recovery import validate_annual_recovery_parent
from finagent.core.model_settings_store import resolve_model_settings
from finagent.agents import annual_review, annual_review_wire
from finagent.core import annual_recovery

control = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); out = Path(sys.argv[2])
root = Path(control["project_root"]).resolve(); run_id = control["run_id"]; job_id = control["job_id"]
capture_dir = Path(control["cli_capture_dir"]).resolve()
app = create_app(project_root=root)
service = app.state.annual_analysis_jobs
service_dir = service.jobs_root
job_path = service_dir / job_id / "job.json"

def archive_hashes():
    values = {}
    for folder in (root / "artifacts" / "runs" / run_id, root / "artifacts" / "reports" / run_id):
        for path in sorted(folder.rglob("*")):
            if path.is_file(): values[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values

def read_only_gate_counts():
    result = {}
    for path in sorted(service_dir.glob("*/call-gate.sqlite3")):
        uri = "file:" + quote(path.resolve().as_posix(), safe="/:\\") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counts = {}
            for name in names:
                safe_name = name.replace('"', '""')
                counts[name] = int(connection.execute('SELECT COUNT(*) FROM "' + safe_name + '"').fetchone()[0])
                if name == "provider_attempts":
                    columns = {row[1] for row in connection.execute('PRAGMA table_info("provider_attempts")')}
                    if "stage" in columns:
                        counts["provider_attempts_by_stage"] = {
                            str(stage): int(total)
                            for stage, total in connection.execute("SELECT stage, COUNT(*) FROM provider_attempts GROUP BY stage ORDER BY stage")
                        }
            result[str(path.relative_to(root))] = counts
        finally:
            connection.close()
    return result

def snapshot_side_effects():
    return {
        "job_directories": sorted(path.name for path in service_dir.iterdir() if path.is_dir()),
        "call_gate_counts": read_only_gate_counts(),
        "cli_capture_files": sorted(path.name for path in capture_dir.glob("*.json")),
    }

def strict_and_core():
    strict = {"loaded": False, "error_type": None, "error_code": None}
    archive = None
    try:
        archive = load_annual_report(root, run_id)
        strict = {
            "loaded": True,
            "error_type": None,
            "error_code": None,
            "manifest_status": archive.manifest_summary.get("status"),
            "snapshot": archive.snapshot,
        }
    except Exception as exc:
        strict.update({"error_type": type(exc).__name__, "error_code": getattr(exc, "code", None)})
    core = {"can_resume": False, "reason": "strict_archive_unavailable"}
    try:
        state = json.loads(job_path.read_text(encoding="utf-8"))
        checked = validate_job_request(root, state["request"])
        settings = resolve_model_settings(root)
        capability = validate_annual_recovery_parent(
            root, run_id, checked, settings=settings, archive=archive, require_issue_pipeline_v5=True,
        )
        core = {key: capability.get(key) for key in ("can_resume", "reason")}
    except Exception as exc:
        core = {"can_resume": False, "reason": getattr(exc, "code", type(exc).__name__)}
    return strict, core

def exercise_api_rejection(client, name):
    before = snapshot_side_effects()
    strict, core = strict_and_core()
    get_response = client.get("/v1/annual-analysis-jobs/" + job_id)
    get_value = get_response.json()
    post_response = client.post(
        "/v1/annual-analysis-jobs/" + job_id + "/resume",
        json={"parent_run_id": run_id},
    )
    if post_response.status_code != 409:
        # A bad acceptance is captured immediately; the newly created job directory
        # is itself proof of the failure, and service shutdown below cancels it.
        post_body = post_response.json()
    else:
        post_body = post_response.json()
    after = snapshot_side_effects()
    return {
        "strict_reader": strict,
        "core_capability": core,
        "api_get": {
            "status_code": get_response.status_code,
            "recovery_capability": get_value.get("recovery_capability", {}),
            "result_availability": get_value.get("result_availability", {}),
        },
        "api_resume": {
            "status_code": post_response.status_code,
            "body": post_body,
            "child_job_directories_unchanged": before["job_directories"] == after["job_directories"],
        },
        "side_effects": {
            "job_directories_unchanged": before["job_directories"] == after["job_directories"],
            "call_gate_counts_unchanged": before["call_gate_counts"] == after["call_gate_counts"],
            "cli_capture_unchanged": before["cli_capture_files"] == after["cli_capture_files"],
            "before": before,
            "after": after,
        },
    }

def source_manifest():
    paths = [root / "scripts" / "analyze_annual.py"]
    paths.extend(path for path in (root / "backend" / "src" / "finagent").rglob("*.py") if "__pycache__" not in path.parts)
    paths.extend((root / "config" / "prompts").rglob("*.md"))
    rows = []
    for path in sorted(set(paths), key=lambda value: value.relative_to(root).as_posix()):
        rows.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    material = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(material).hexdigest(), "file_count": len(rows), "files": rows}

before_archive = archive_hashes()
before_source = source_manifest()
original_job_state_bytes = job_path.read_bytes()
original_job_state_sha256 = hashlib.sha256(original_job_state_bytes).hexdigest()
drifts = {}
model_file = Path(control["model_file"])
review_prompt_path = root / "config" / "prompts" / "annual_review_v3.md"
group_prompt_path = root / "config" / "prompts" / "annual_issue_grouping_v1.md"
source_request = control["request"]
source_file = root / "data" / "raw" / Path(*source_request["source_pdf_path"].split("/"))
if source_file.name != "source.pdf":
    source_file = source_file.parent / "source.pdf"
source_sha256_expected = source_request["sha256"]
source_sha256_before = hashlib.sha256(source_file.read_bytes()).hexdigest()
if source_sha256_before != source_sha256_expected:
    raise RuntimeError("binding_drift_parent_source_sha256_does_not_match_control")

def exercise_source_binding_drift(client):
    original_state = job_path.read_bytes()
    state = json.loads(original_state.decode("utf-8"))
    request = dict(state["request"])
    old_document_id = request["document_id"]
    new_document_id = old_document_id + "-binding-drift"
    new_source_rel = request["source_pdf_path"].replace(old_document_id, new_document_id)
    original_pdf = source_file
    original_record = original_pdf.parent / "source.json"
    alternate_dir = root / "data" / "raw" / request["company_id"] / str(request["report_year"]) / new_document_id
    alternate_dir.mkdir(parents=True, exist_ok=False)
    import shutil
    shutil.copy2(original_pdf, alternate_dir / "source.pdf")
    source_record = json.loads(original_record.read_text(encoding="utf-8"))
    source_record["document_id"] = new_document_id
    source_record["local_path"] = "data/raw/" + new_source_rel
    (alternate_dir / "source.json").write_text(json.dumps(source_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    request["document_id"] = new_document_id
    request["source_pdf_path"] = new_source_rel
    state["request"] = request
    try:
        job_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = exercise_api_rejection(client, "source")
        result["alternate_source"] = {
            "document_id": new_document_id,
            "source_pdf_sha256": hashlib.sha256((alternate_dir / "source.pdf").read_bytes()).hexdigest(),
            "source_record_path": str((alternate_dir / "source.json").relative_to(root)),
        }
        drifts["source"] = result
    finally:
        job_path.write_bytes(original_state)

def exercise_file_drift(client, name, path, changed_bytes):
    original = path.read_bytes()
    try:
        path.write_bytes(changed_bytes)
        drifts[name] = exercise_api_rejection(client, name)
    finally:
        path.write_bytes(original)

def exercise_version_drift(client, name, changes):
    originals = [(module, attribute, getattr(module, attribute)) for module, attribute, _value in changes]
    try:
        for module, attribute, value in changes:
            setattr(module, attribute, value)
        drifts[name] = exercise_api_rejection(client, name)
    finally:
        for module, attribute, value in originals:
            setattr(module, attribute, value)

try:
    with TestClient(app) as client:
        baseline_strict, baseline_core = strict_and_core()
        baseline_get = client.get("/v1/annual-analysis-jobs/" + job_id)
        baseline_public = baseline_get.json().get("recovery_capability", {})
        if not baseline_strict.get("loaded") or baseline_core.get("can_resume") is not True or baseline_public.get("can_resume") is not True:
            raise RuntimeError("resumable_baseline_is_not_consistent_across_strict_core_and_public_API")
        exercise_source_binding_drift(client)
        model_payload = (
            "MODEL_BASE_URL=" + json.dumps(control["provider_base_url"]) + "\n"
            + "MODEL_NAME=" + json.dumps(control["alt_model_name"]) + "\n"
            + "MODEL_API_KEY=" + json.dumps("r4-strict-capability-loopback-dummy-key") + "\n"
        ).encode("utf-8")
        exercise_file_drift(client, "model", model_file, model_payload)
        exercise_file_drift(client, "review_prompt", review_prompt_path, review_prompt_path.read_bytes() + b"\n<!-- controlled review prompt binding drift -->\n")
        exercise_file_drift(client, "grouping_prompt", group_prompt_path, group_prompt_path.read_bytes() + b"\n<!-- controlled issue prompt binding drift -->\n")
        exercise_version_drift(client, "review_wire_version", [
            (annual_review_wire, "V3_WIRE_SCHEMA", annual_review_wire.V3_WIRE_SCHEMA + "_drift"),
            (annual_review, "V3_REVIEW_WIRE_VERSION", annual_review.V3_REVIEW_WIRE_VERSION + "_drift"),
            (annual_recovery, "V3_WIRE_SCHEMA", annual_recovery.V3_WIRE_SCHEMA + "_drift"),
        ])
        exercise_version_drift(client, "review_projection_version", [
            (annual_review_wire, "V3_INPUT_PROJECTION_VERSION", annual_review_wire.V3_INPUT_PROJECTION_VERSION + "_drift"),
            (annual_review, "V3_REVIEW_INPUT_PROJECTION_VERSION", annual_review.V3_REVIEW_INPUT_PROJECTION_VERSION + "_drift"),
            (annual_recovery, "V3_INPUT_PROJECTION_VERSION", annual_recovery.V3_INPUT_PROJECTION_VERSION + "_drift"),
        ])
finally:
    service.shutdown()

after_archive = archive_hashes()
after_source = source_manifest()
restored_job_state_sha256 = hashlib.sha256(job_path.read_bytes()).hexdigest()
restored_source_sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()
if restored_source_sha256 != source_sha256_before or restored_source_sha256 != source_sha256_expected:
    raise RuntimeError("binding_drift_parent_source_sha256_not_restored_exactly")
result = {
    "baseline": {
        "strict_reader": baseline_strict,
        "core_capability": baseline_core,
        "api_get_status_code": baseline_get.status_code,
        "api_get_capability": baseline_public,
    },
    "drifts": drifts,
    "parent_archive_files_unchanged": before_archive == after_archive,
    "archive_file_count": len(before_archive),
    "snapshot_source_manifest_restored": before_source == after_source,
    "parent_job_state_restored": original_job_state_bytes == job_path.read_bytes(),
    "parent_job_state_sha256_before": original_job_state_sha256,
    "parent_job_state_sha256_after": restored_job_state_sha256,
    "source_manifest_before": before_source,
    "source_manifest_after": after_source,
    "source_pdf_path": source_file.relative_to(root).as_posix(),
    "source_sha256_expected": source_sha256_expected,
    "source_sha256_before": source_sha256_before,
    "restored_source_sha256": restored_source_sha256,
    "restored_review_prompt_sha256": hashlib.sha256(review_prompt_path.read_bytes()).hexdigest(),
    "restored_grouping_prompt_sha256": hashlib.sha256(group_prompt_path.read_bytes()).hexdigest(),
    "restored_model_file_sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
}
out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
'''
