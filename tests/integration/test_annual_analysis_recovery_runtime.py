"""Production CLI/runtime recovery checks against a bounded local HTTP fixture.

Identity-sensitive cases are opt-in and require an explicit source-freeze
manifest hash in ``FINTRACE_R2_SOURCE_FREEZE_SHA256``.  They run a copied,
hash-verified checkout under ``artifacts/tmp`` so every source, raw PDF, run,
and audit record stays isolated from the user's working ``data/raw`` tree.
No provider credentials or external network access are used.
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
import urllib.request
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import pymupdf
import pytest

from finagent.core.annual_runtime_identity import source_manifest_fingerprint
from finagent.core.model_call_ledger import (
    close_admission_gate,
    initialize_provider_call_ledger,
    open_admission_gate,
)
from finagent.api.annual_report_read import load_annual_report
from finagent.retrieval.annual_context import retrieve_annual_context


_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
_SCRATCH_ROOT = _WORKSPACE_ROOT / "artifacts" / "tmp" / "recovery-r2-annual-runtime-20261006"
_CONTROLLED_SOURCE_ROOT = _SCRATCH_ROOT / "controlled-source-r2-runtime-fixture-20261006"
_CANDIDATE_CONTROLLED_SOURCE_ROOT = _SCRATCH_ROOT / "controlled-source-r2-candidate-fixture-20261007b"
_EVIDENCE_ROOT = _WORKSPACE_ROOT / "artifacts" / "evaluations" / "annual-recovery-r2-20261006"
_FIXTURE_COMPANY_ID = "SIM-ANNUAL-001"
_FIXTURE_REPORT_YEAR = 2025
_FIXTURE_DOCUMENT_ID = "r2-runtime-fixture-20261006"
_CANDIDATE_FIXTURE_DOCUMENT_ID = "r2-runtime-candidate-fixture-20261007b"
_FIXTURE_RELATIVE_PDF = (
    f"{_FIXTURE_COMPANY_ID}/{_FIXTURE_REPORT_YEAR}/{_FIXTURE_DOCUMENT_ID}/source.pdf"
)
_FAKE_MODEL_NAME = "fintrace-r2-loopback-runtime-fixture"
_LOCAL_API_KEY = "synthetic-loopback-only-key"
_SOURCE_FREEZE_ENV = "FINTRACE_R2_SOURCE_FREEZE_SHA256"


@dataclass(frozen=True)
class RuntimeFixture:
    """A copied production checkout with one explicitly synthetic PDF source."""

    project_root: Path
    source_pdf: Path
    source_record: Path
    source_sha256: str
    source_pdf_relative: str
    company_id: str
    report_year: int
    document_id: str
    source_registration: str
    base_sample_sha256: str


@dataclass(frozen=True)
class ControlledAnnualSource:
    """Canonical synthetic PDF and registration shared by the CLI/API harnesses."""

    source_root: Path
    source_pdf: Path
    source_record: Path
    source_sha256: str
    base_sample_sha256: str


def build_runtime_fixture(
    project_root: Path,
    *,
    frozen_workspace: Path,
    freeze_sha256: str,
    source_variant: str = "base",
) -> RuntimeFixture:
    """Copy frozen runtime inputs plus the canonical controlled derived PDF.

    ``project_root`` must be new and below the task-owned recovery tmp root.
    The selected source PDF is derived from the repository's fictional annual
    sample, then extended only with clearly marked software-test narrative
    pages.  Its changed SHA and document ID prevent it from being represented
    as the unchanged registered sample.
    """

    root = Path(project_root).resolve()
    workspace = Path(frozen_workspace).resolve()
    scratch = _SCRATCH_ROOT.resolve()
    if not _is_relative_to(root, scratch) or root == scratch:
        raise ValueError("runtime_fixture_project_must_be_inside_task_tmp")
    if root.exists():
        raise FileExistsError("runtime_fixture_project_already_exists")
    if len(freeze_sha256) != 64 or any(char not in "0123456789abcdef" for char in freeze_sha256):
        raise ValueError("runtime_fixture_freeze_hash_invalid")

    before_sha, _before_count = source_manifest_fingerprint(workspace)
    if before_sha != freeze_sha256:
        raise ValueError("runtime_fixture_workspace_does_not_match_explicit_freeze")

    if source_variant == "base":
        controlled_source = prepare_controlled_source(workspace)
        document_id = _FIXTURE_DOCUMENT_ID
        source_registration = "unconfirmed_derived_copy_of_synthetic_sample"
    elif source_variant == "candidate":
        controlled_source = prepare_candidate_controlled_source(workspace)
        document_id = _CANDIDATE_FIXTURE_DOCUMENT_ID
        source_registration = "synthetic_sample_with_artificial_candidate_retrieval_disclosure"
    else:
        raise ValueError("runtime_fixture_source_variant_invalid")
    source_pdf_relative = f"{_FIXTURE_COMPANY_ID}/{_FIXTURE_REPORT_YEAR}/{document_id}/source.pdf"
    base_sample_sha256 = controlled_source.base_sample_sha256

    root.mkdir(parents=True)
    (root / "backend" / "src").mkdir(parents=True)
    shutil.copytree(workspace / "backend" / "src" / "finagent", root / "backend" / "src" / "finagent")
    (root / "scripts").mkdir()
    shutil.copy2(workspace / "scripts" / "analyze_annual.py", root / "scripts" / "analyze_annual.py")
    (root / "config").mkdir()
    shutil.copytree(workspace / "config" / "prompts", root / "config" / "prompts")
    (root / "artifacts").mkdir()

    raw_dir = root / "data" / "raw" / _FIXTURE_COMPANY_ID / str(_FIXTURE_REPORT_YEAR) / document_id
    raw_dir.mkdir(parents=True)
    source_pdf = raw_dir / "source.pdf"
    shutil.copy2(controlled_source.source_pdf, source_pdf)
    source_sha256 = controlled_source.source_sha256
    source_record = raw_dir / "source.json"
    shutil.copy2(controlled_source.source_record, source_record)

    after_workspace_sha, _after_count = source_manifest_fingerprint(workspace)
    scratch_sha, _scratch_count = source_manifest_fingerprint(root)
    if after_workspace_sha != freeze_sha256 or scratch_sha != freeze_sha256:
        raise ValueError("runtime_fixture_source_changed_during_copy")
    return RuntimeFixture(
        project_root=root,
        source_pdf=source_pdf,
        source_record=source_record,
        source_sha256=source_sha256,
        source_pdf_relative=source_pdf_relative,
        company_id=_FIXTURE_COMPANY_ID,
        report_year=_FIXTURE_REPORT_YEAR,
        document_id=document_id,
        source_registration=source_registration,
        base_sample_sha256=base_sample_sha256,
    )


def prepare_controlled_source(workspace_root: Path = _WORKSPACE_ROOT) -> ControlledAnnualSource:
    """Create or verify the one reusable software-test-only annual source.

    The canonical PDF lives outside ``data/raw`` and outside the workspace's
    provider/runtime state so the API and CLI harnesses can upload/copy the
    exact same source bytes without registering into the real project.
    """

    workspace = Path(workspace_root).resolve()
    controlled_root = _CONTROLLED_SOURCE_ROOT.resolve()
    if not _is_relative_to(controlled_root, _SCRATCH_ROOT.resolve()):
        raise ValueError("controlled_source_must_be_inside_task_tmp")
    sample = workspace / "data" / "samples" / "annual" / "sim-annual-001-2025.pdf"
    if not sample.is_file():
        raise FileNotFoundError("registered_synthetic_annual_sample_missing")
    base_sample_sha256 = _sha256(sample)
    raw_dir = controlled_root / "data" / "raw" / _FIXTURE_COMPANY_ID / str(_FIXTURE_REPORT_YEAR) / _FIXTURE_DOCUMENT_ID
    source_pdf = raw_dir / "source.pdf"
    source_record = raw_dir / "source.json"
    if not controlled_root.exists():
        raw_dir.mkdir(parents=True, exist_ok=False)
        _write_derived_controlled_pdf(sample, source_pdf)
        source_sha256 = _sha256(source_pdf)
        _write_json_new(source_record, {
            "company_id": _FIXTURE_COMPANY_ID,
            "document_id": _FIXTURE_DOCUMENT_ID,
            "report_type": "年度报告（受控软件测试派生件）",
            "report_period": f"{_FIXTURE_REPORT_YEAR}-12-31",
            "local_path": f"data/raw/{_FIXTURE_RELATIVE_PDF}",
            "sha256": source_sha256,
            "source_nature": "由仓库原创虚构样例派生；新增页仅为人工构造软件测试内容；不是真实披露或标签",
            "base_sample_sha256": base_sample_sha256,
        })
    if not source_pdf.is_file() or not source_record.is_file():
        raise FileNotFoundError("canonical_controlled_source_incomplete; preserve the existing scratch directory")
    record = json.loads(source_record.read_text(encoding="utf-8"))
    source_sha256 = _sha256(source_pdf)
    if (
        record.get("company_id") != _FIXTURE_COMPANY_ID
        or record.get("document_id") != _FIXTURE_DOCUMENT_ID
        or record.get("local_path") != f"data/raw/{_FIXTURE_RELATIVE_PDF}"
        or record.get("sha256") != source_sha256
        or record.get("base_sample_sha256") != base_sample_sha256
        or not str(record.get("source_nature", "")).startswith("由仓库原创虚构样例派生")
    ):
        raise ValueError("canonical_controlled_source_identity_or_hash_mismatch")
    return ControlledAnnualSource(controlled_root, source_pdf, source_record, source_sha256, base_sample_sha256)


def prepare_candidate_controlled_source(workspace_root: Path = _WORKSPACE_ROOT) -> ControlledAnnualSource:
    """Create a second synthetic PDF with real source-bound retrieval context.

    The original controlled source is retained unchanged.  This variant adds
    one clearly marked artificial management-discussion paragraph whose terms
    satisfy the real annual-context retriever for the already verified
    profit/cash and revenue/cash screening candidates.
    """

    workspace = Path(workspace_root).resolve()
    base = prepare_controlled_source(workspace)
    controlled_root = _CANDIDATE_CONTROLLED_SOURCE_ROOT.resolve()
    if not _is_relative_to(controlled_root, _SCRATCH_ROOT.resolve()):
        raise ValueError("candidate_controlled_source_must_be_inside_task_tmp")
    raw_dir = controlled_root / "data" / "raw" / _FIXTURE_COMPANY_ID / str(_FIXTURE_REPORT_YEAR) / _CANDIDATE_FIXTURE_DOCUMENT_ID
    source_pdf = raw_dir / "source.pdf"
    source_record = raw_dir / "source.json"
    if not controlled_root.exists():
        raw_dir.mkdir(parents=True, exist_ok=False)
        _write_candidate_narrative_pdf(base.source_pdf, source_pdf)
        source_sha256 = _sha256(source_pdf)
        _write_json_new(source_record, {
            "company_id": _FIXTURE_COMPANY_ID,
            "document_id": _CANDIDATE_FIXTURE_DOCUMENT_ID,
            "report_type": "年度报告（受控软件测试原创变体）",
            "report_period": f"{_FIXTURE_REPORT_YEAR}-12-31",
            "local_path": f"data/raw/{_FIXTURE_COMPANY_ID}/{_FIXTURE_REPORT_YEAR}/{_CANDIDATE_FIXTURE_DOCUMENT_ID}/source.pdf",
            "sha256": source_sha256,
            "source_nature": "由仓库原创虚构样例及旧受控 PDF 派生；新增中文管理层讨论与财务附注为人工构造软件测试内容，不是真实披露、结论或标签",
            "base_sample_sha256": base.base_sample_sha256,
            "base_controlled_pdf_sha256": base.source_sha256,
        })
    if not source_pdf.is_file() or not source_record.is_file():
        raise FileNotFoundError("candidate_controlled_source_incomplete; preserve the existing scratch directory")
    record = json.loads(source_record.read_text(encoding="utf-8"))
    source_sha256 = _sha256(source_pdf)
    if (
        record.get("company_id") != _FIXTURE_COMPANY_ID
        or record.get("document_id") != _CANDIDATE_FIXTURE_DOCUMENT_ID
        or record.get("local_path") != f"data/raw/{_FIXTURE_COMPANY_ID}/{_FIXTURE_REPORT_YEAR}/{_CANDIDATE_FIXTURE_DOCUMENT_ID}/source.pdf"
        or record.get("sha256") != source_sha256
        or record.get("base_sample_sha256") != base.base_sample_sha256
        or record.get("base_controlled_pdf_sha256") != base.source_sha256
        or "不是真实披露" not in str(record.get("source_nature", ""))
    ):
        raise ValueError("candidate_controlled_source_identity_or_hash_mismatch")
    return ControlledAnnualSource(controlled_root, source_pdf, source_record, source_sha256, base.base_sample_sha256)


def _write_derived_controlled_pdf(sample: Path, destination: Path) -> None:
    """Retain the fictional financial statements and append test-only text."""

    document = pymupdf.open(sample)
    try:
        document.set_metadata({
            **document.metadata,
            "title": "受控软件测试派生年报｜虚构，不是真实披露",
            "subject": "Derived from FINTRACE's fictional sample; appended pages are artificial test content",
            "creator": "tests/integration/test_annual_analysis_recovery_runtime.py",
        })
        for page_index in range(1, 11):
            page = document.new_page()
            page.insert_text((36, 24), "CONTROLLED FINTRACE SOFTWARE TEST PAGE - FICTIONAL CONTENT", fontsize=8)
            page.insert_text((36, 38), "Derived from an original fictional sample. No real company disclosure or label.", fontsize=7)
            for line_index in range(70):
                line = (
                    f"R2 fixture p{page_index:02d} l{line_index + 1:03d}: Artificial annual narrative "
                    "about revenue cash collection contract balances receivable ageing and disclosure controls."
                )
                page.insert_text((36, 54 + line_index * 10), line, fontsize=6.2)
        document.save(destination, garbage=3, deflate=True)
    finally:
        document.close()


def _write_candidate_narrative_pdf(base_pdf: Path, destination: Path) -> None:
    """Append synthetic MDA quotes without changing the retained source."""

    document = pymupdf.open(base_pdf)
    try:
        document.set_metadata({
            **document.metadata,
            "title": "受控软件测试年报变体｜原创虚构披露，不是真实资料",
            "subject": "Original fictional FINTRACE sample plus one artificial retrieval-only narrative page",
            "creator": "tests/integration/test_annual_analysis_recovery_runtime.py",
        })
        page = document.new_page()
        page.insert_text((36, 24), "CONTROLLED FINTRACE SOFTWARE TEST PAGE - FICTIONAL CONTENT", fontsize=8)
        page.insert_text((36, 39), "原创人工构造披露｜非真实公司资料｜非舞弊标签", fontname="china-s", fontsize=9)
        page.insert_text((36, 72), "管理层讨论与分析", fontname="china-s", fontsize=14)
        lines = (
            "本页披露为原创人工构造软件测试材料，不代表真实公司经营情况、舞弊结论或标签。",
            "本年营业收入同比增加，但经营活动产生的现金流量净额同比下降；部分合同款项于期后收到。",
            "归属于上市公司股东的净利润同比增加，但经营活动产生的现金流量净额同比下降；回款时点仍待核对。",
            "营业收入：2025年度1,250,000,000元；2024年度1,000,000,000元。",
            "归属于上市公司股东的净利润：2025年度145,000,000元；2024年度118,000,000元。",
            "经营活动产生的现金流量净额：2025年度-10,000,000元；2024年度90,000,000元。",
        )
        for index, line in enumerate(lines):
            page.insert_text((36, 106 + index * 25), line, fontname="china-s", fontsize=8.5)
        page.insert_text((36, 300), "合并财务报表附注", fontname="china-s", fontsize=12)
        page.insert_textbox(
            pymupdf.Rect(36, 325, 559, 365),
            "本附注标题用于满足同源叙述检索的年度报告章节范围；内容仍为人工构造软件测试材料。",
            fontname="china-s",
            fontsize=9,
        )
        document.save(destination, garbage=3, deflate=True)
    finally:
        document.close()


class LoopbackAnnualModel:
    """OpenAI-compatible mock endpoint bound to 127.0.0.1 and an ephemeral port."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._phase = "idle"
        self._events: list[dict[str, Any]] = []
        self._block_phase: str | None = None
        self._block_stage: str | None = None
        self._block_batch_index: int | None = None
        self._block_candidate_signal_id: str | None = None
        self._block_hold_timeout = 30.0
        self._block_entered = threading.Event()
        self._block_release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    stage, user_payload = _classify_model_request(payload)
                except Exception:
                    self.send_error(400)
                    return
                with owner._lock:
                    phase = owner._phase
                    sequence = len(owner._events) + 1
                batch_index = user_payload.get("batch_index") if isinstance(user_payload, Mapping) else None
                candidate_signal_id = None
                if stage == "annual_investigation" and isinstance(user_payload, Mapping):
                    candidate = user_payload.get("candidate")
                    if isinstance(candidate, Mapping) and isinstance(candidate.get("signal_id"), str):
                        candidate_signal_id = candidate["signal_id"]
                status, content = owner._response_for(phase, stage, user_payload)
                event = {
                    "sequence": sequence,
                    "phase": phase,
                    "stage": stage,
                    "batch_index": batch_index if type(batch_index) is int else None,
                    "candidate_signal_id": candidate_signal_id,
                    "status": status,
                    "received_at": _utc_now(),
                }
                with owner._lock:
                    owner._events.append(event)
                    should_block = (
                        owner._block_phase == phase
                        and owner._block_stage == stage
                        and (
                            owner._block_batch_index is None
                            or owner._block_batch_index == event["batch_index"]
                        )
                        and (
                            owner._block_candidate_signal_id is None
                            or owner._block_candidate_signal_id == event["candidate_signal_id"]
                        )
                    )
                    block_hold_timeout = owner._block_hold_timeout
                if should_block:
                    owner._block_entered.set()
                    owner._block_release.wait(timeout=block_hold_timeout)
                body = (
                    json.dumps({"error": {"message": "controlled local fixture response"}}).encode("utf-8")
                    if status >= 400
                    else _openai_chat_response(content, sequence)
                )
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    with owner._lock:
                        event["response_finished_at"] = _utc_now()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    with owner._lock:
                        event["client_disconnected_before_response"] = True
                finally:
                    self.close_connection = True

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="r2-loopback-model")
        self._started = False

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "LoopbackAnnualModel":
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def set_phase(self, name: str) -> None:
        with self._lock:
            self._phase = name

    def events(self, phase: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = [dict(item) for item in self._events]
        return values if phase is None else [item for item in values if item["phase"] == phase]

    def block_batch(
        self,
        phase: str,
        stage: str,
        batch_index: int | None = None,
        *,
        candidate_signal_id: str | None = None,
        hold_timeout: float = 30.0,
    ) -> None:
        """Block one request, optionally narrowed by batch/candidate and timeout."""
        if (
            isinstance(hold_timeout, bool)
            or not isinstance(hold_timeout, (int, float))
            or not 0 < float(hold_timeout) <= 300
        ):
            raise ValueError("loopback_block_hold_timeout_invalid")
        with self._lock:
            self._block_phase = phase
            self._block_stage = stage
            self._block_batch_index = batch_index
            self._block_candidate_signal_id = candidate_signal_id
            self._block_hold_timeout = float(hold_timeout)
        self._block_entered.clear()
        self._block_release.clear()

    def wait_for_event(
        self,
        *,
        phase: str,
        stage: str | None = None,
        batch_index: int | None = None,
        candidate_signal_id: str | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events(phase):
                if stage is not None and event["stage"] != stage:
                    continue
                if batch_index is not None and event["batch_index"] != batch_index:
                    continue
                if candidate_signal_id is not None and event.get("candidate_signal_id") != candidate_signal_id:
                    continue
                return event
            time.sleep(0.01)
        raise TimeoutError("loopback_fixture_request_not_observed")

    def wait_for_response(
        self,
        *,
        phase: str,
        stage: str | None = None,
        batch_index: int | None = None,
        candidate_signal_id: str | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        """Wait until a matching request received a response or its client disconnected."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events(phase):
                if stage is not None and event["stage"] != stage:
                    continue
                if batch_index is not None and event["batch_index"] != batch_index:
                    continue
                if candidate_signal_id is not None and event.get("candidate_signal_id") != candidate_signal_id:
                    continue
                if "response_finished_at" in event or event.get("client_disconnected_before_response"):
                    return event
            time.sleep(0.01)
        raise TimeoutError("loopback_fixture_response_not_observed")

    def wait_for_block(self, timeout: float = 20) -> None:
        if not self._block_entered.wait(timeout):
            raise TimeoutError("loopback_fixture_block_not_reached")

    def release_block(self) -> None:
        self._block_release.set()

    def close(self) -> None:
        self._block_release.set()
        if self._started:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._started = False

    def _response_for(
        self,
        phase: str,
        stage: str,
        user_payload: object,
    ) -> tuple[int, str]:
        if stage == "annual_discovery":
            index = user_payload.get("batch_index") if isinstance(user_payload, Mapping) else None
            if type(index) is not int:
                return 200, json.dumps({"findings": [], "limitations": []})
            if phase == "auth_stop" and index == 1:
                return 401, ""
            if phase == "candidate_auth_stop":
                return 200, json.dumps({"findings": [], "limitations": []})
            if phase == "all_503":
                return 503, ""
            if phase == "invalid_container" and index == 1:
                return 200, json.dumps({"findings": {"invalid": True}, "limitations": []})
            if phase == "parent_partial":
                if index == 1:
                    return 200, _discovery_response(user_payload, title="受控父项一")
                if index == 2:
                    return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
                if index == 3:
                    return 200, _discovery_response(user_payload, title="受控父项保留", add_invalid_citation=True)
                if index == 4:
                    return 503, ""
                return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
            if phase == "parent_semantic_partial":
                if index == 1:
                    return 200, _discovery_response(user_payload, title="受控父项一")
                if index == 2:
                    return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
                if index == 3:
                    return 200, _discovery_response(user_payload, title="受控父项保留", add_invalid_citation=True)
                if index == 4:
                    return 200, json.dumps({"findings": {"invalid": True}, "limitations": []})
                return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
            if phase == "resume_child":
                if index == 3:
                    return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
                if index == 4:
                    return 200, _discovery_response(user_payload, title="续跑新增父项")
                return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
            if phase in {"resume_child_same_partial", "resume_grandchild_same_partial"}:
                if index == 3:
                    return 200, _discovery_response(
                        user_payload, title="受控父项保留", add_invalid_citation=True
                    )
                if index == 4:
                    return 200, json.dumps({"findings": {"invalid": True}, "limitations": []})
                return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
            if phase == "empty":
                return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
            # The interrupted first call gets a real success response if the
            # worker is still alive; the CLI's cancel marker prevents later
            # dispatch either way.
            return 200, _discovery_response(user_payload, title="中断前已持久化线索")

        if stage == "annual_investigation":
            if phase == "candidate_auth_stop":
                return 401, ""
            if phase in {"all_503", "auth_stop", "invalid_container"}:
                return 200, _investigation_response(user_payload)
            return 200, _investigation_response(user_payload)

        if stage == "annual_review":
            if phase == "candidate_auth_stop":
                # Any review after the first candidate's 401 would violate the
                # intended stop boundary; returning 200 makes that visible.
                return 200, _review_response()
            return 200, _review_response()

        return 200, json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)


def _classify_model_request(payload: object) -> tuple[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("openai_payload_invalid")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("openai_messages_invalid")
    user = next((item for item in messages if isinstance(item, Mapping) and item.get("role") == "user"), None)
    if not isinstance(user, Mapping) or not isinstance(user.get("content"), str):
        raise ValueError("openai_user_message_invalid")
    try:
        user_payload = json.loads(user["content"])
    except json.JSONDecodeError:
        user_payload = {}
    if isinstance(user_payload, Mapping) and type(user_payload.get("batch_index")) is int and isinstance(user_payload.get("fragments"), list):
        return "annual_discovery", user_payload
    if isinstance(user_payload, Mapping) and isinstance(user_payload.get("candidate"), Mapping) and isinstance(user_payload.get("narrative_evidence"), list):
        return "annual_investigation", user_payload
    if isinstance(user_payload, Mapping):
        return "annual_review", user_payload
    return "annual_review", {}


def _discovery_response(payload: object, *, title: str, add_invalid_citation: bool = False) -> str:
    fragments = payload.get("fragments") if isinstance(payload, Mapping) else None
    if not isinstance(fragments, list) or not fragments:
        raise ValueError("discovery_fixture_fragments_missing")
    fragment = next((item for item in fragments if isinstance(item, Mapping) and isinstance(item.get("raw_text"), str) and isinstance(item.get("evidence_id"), str)), None)
    if not isinstance(fragment, Mapping):
        raise ValueError("discovery_fixture_fragment_invalid")
    source = str(fragment["raw_text"]).strip()
    quote = source[: min(72, len(source))]
    valid = {
        "title": title,
        "observation": "受控模拟文本记录了相关披露措辞",
        "why_it_matters": "该线索可供后续人工复核",
        "follow_up": "核对报告中的解释与支持材料",
        "priority": "low",
        "citations": [{"evidence_id": fragment["evidence_id"], "quote": quote}],
    }
    findings: list[dict[str, object]] = [valid]
    if add_invalid_citation:
        findings.append({
            **valid,
            "title": "拒绝的错误引用线索",
            "citations": [{"evidence_id": "unknown-fixture-evidence", "quote": quote}],
        })
    return json.dumps({"findings": findings, "limitations": []}, ensure_ascii=False, separators=(",", ":"))


def _investigation_response(payload: object) -> str:
    candidate = payload.get("candidate") if isinstance(payload, Mapping) else None
    evidence = payload.get("narrative_evidence") if isinstance(payload, Mapping) else None
    if not isinstance(candidate, Mapping) or not isinstance(candidate.get("signal_id"), str) or not isinstance(evidence, list):
        raise ValueError("investigation_fixture_payload_invalid")
    evidence_id = next((item.get("evidence_id") for item in evidence if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)), None)
    if evidence_id is None:
        raise ValueError("investigation_fixture_evidence_missing")
    return json.dumps({
        "signal_id": candidate["signal_id"],
        "explanation": "受控模拟来源中的叙述线索需要人工复核",
        "alternative_explanations": [],
        "limitations": ["本回答是软件测试输出，未经独立核验"],
        "narrative_evidence_ids": [evidence_id],
        "request_more_context": False,
    }, ensure_ascii=False, separators=(",", ":"))


def _review_response() -> str:
    return json.dumps({
        "assessment": "insufficient_evidence",
        "summary": "受控测试输出不足以支持范围内判断",
        "reasons": [],
        "follow_up_items": [],
        "limitations": ["所有模型意见均为本地构造测试响应"],
    }, ensure_ascii=False, separators=(",", ":"))


def _openai_chat_response(content: str, sequence: int) -> bytes:
    payload = {
        "id": f"chatcmpl-r2-fixture-{sequence:05d}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _FAKE_MODEL_NAME,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@dataclass
class CliProcess:
    label: str
    attempt_dir: Path
    argv: list[str]
    process: subprocess.Popen[bytes]
    stdout_handle: Any
    stderr_handle: Any
    started_at: str
    env_summary: dict[str, object]


def _begin_cli(
    fixture: RuntimeFixture,
    evidence_dir: Path,
    label: str,
    *,
    runtime_fingerprint: str,
    model: LoopbackAnnualModel | None = None,
    phase: str | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    parent_run_id: str | None = None,
    model_name: str = _FAKE_MODEL_NAME,
    request_mode: str | None = "model_investigation",
    with_m3_screening: bool = False,
    cancel_file: Path | None = None,
    deterministic: bool = False,
) -> CliProcess:
    attempt_dir = evidence_dir / "attempts" / f"{label}-{uuid.uuid4().hex}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    chosen_run_id = run_id or _new_run_id()
    chosen_job_id = job_id or _new_job_id()
    command = [
        sys.executable,
        str(fixture.project_root / "scripts" / "analyze_annual.py"),
        "--source-pdf", str(fixture.source_pdf),
        "--company-id", fixture.company_id,
        "--report-year", str(fixture.report_year),
        "--document-id", fixture.document_id,
        "--expected-sha256", fixture.source_sha256,
        "--expected-runtime-fingerprint", runtime_fingerprint,
        "--run-id", chosen_run_id,
    ]
    if not deterministic:
        command.extend([
            "--with-model",
            "--source-record", str(fixture.source_record),
            "--discovery-progress-job-id", chosen_job_id,
        ])
        if request_mode is not None:
            command.extend(["--request-mode", request_mode])
        if with_m3_screening:
            command.append("--with-m3-screening")
        if parent_run_id is not None:
            command.extend(["--resume-parent-run-id", parent_run_id])
        job_dir = fixture.project_root / "artifacts" / "annual-analysis-jobs" / chosen_job_id
        job_dir.mkdir(parents=True, exist_ok=False)

    env = _minimal_environment(fixture.project_root)
    if not deterministic:
        if model is None:
            raise ValueError("runtime_model_fixture_required")
        if phase is not None:
            model.set_phase(phase)
        gate_db = fixture.project_root / "artifacts" / "annual-analysis-jobs" / chosen_job_id / "call-gate.sqlite3"
        gate_key = f"r2-runtime-{chosen_job_id}"
        initialize_provider_call_ledger(gate_db)
        open_admission_gate(gate_db, gate_key)
        env.update({
            "MODEL_BASE_URL": model.base_url,
            "MODEL_API_KEY": _LOCAL_API_KEY,
            "MODEL_NAME": model_name,
            "FINTRACE_CALL_GATE_DB": str(gate_db),
            "FINTRACE_CALL_GATE_KEY": gate_key,
            "FINTRACE_ACTIVE_SOURCE_SHA256": fixture.source_sha256,
            "FINTRACE_DISCOVERY_CONCURRENCY": "1",
        })
        env_summary: dict[str, object] = {
            "inherited_environment": False,
            "provider": "127.0.0.1 loopback only",
            "base_url": model.base_url,
            "model_name": model_name,
            "api_key": "synthetic loopback key; raw value omitted",
            "call_gate": "job-local sqlite ledger; admission opened for this CLI attempt",
        }
    else:
        env_summary = {
            "inherited_environment": False,
            "provider": "not configured; deterministic CLI",
            "model_name": None,
            "api_key": None,
            "call_gate": None,
        }
    if cancel_file is not None:
        env["FINTRACE_CALL_CANCEL_FILE"] = str(cancel_file)

    stdout_path = attempt_dir / "stdout.txt"
    stderr_path = attempt_dir / "stderr.txt"
    stdout_handle = stdout_path.open("xb")
    stderr_handle = stderr_path.open("xb")
    process = subprocess.Popen(
        command,
        cwd=fixture.project_root,
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        shell=False,
    )
    started_at = _utc_now()
    _write_json_new(attempt_dir / "command.json", {
        "argv": command,
        "cwd": str(fixture.project_root),
        "shell": False,
        "run_id": chosen_run_id,
        "job_id": None if deterministic else chosen_job_id,
        "resume_parent_run_id": parent_run_id,
        "phase": phase,
        "runtime_fingerprint": runtime_fingerprint,
        "environment_policy": env_summary,
        "started_at": started_at,
        "pid": process.pid,
    })
    return CliProcess(label, attempt_dir, command, process, stdout_handle, stderr_handle, started_at, env_summary)


def _finish_cli(capture: CliProcess, *, timeout: float = 150) -> int:
    termination: str | None = None
    try:
        return_code = capture.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        termination = "terminate_own_cli_process_after_timeout"
        capture.process.terminate()
        try:
            return_code = capture.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            termination = "kill_own_cli_process_after_terminate_timeout"
            capture.process.kill()
            return_code = capture.process.wait(timeout=5)
    finally:
        capture.stdout_handle.close()
        capture.stderr_handle.close()
    _write_json_new(capture.attempt_dir / "process-exit.json", {
        "pid": capture.process.pid,
        "returncode": capture.process.returncode,
        "poll_after_wait": capture.process.poll(),
        "started_at": capture.started_at,
        "finished_at": _utc_now(),
        "termination_action": termination,
        "exit_observed": capture.process.poll() is not None,
    })
    if termination is not None:
        raise AssertionError(f"CLI did not exit naturally; own Popen handle used {termination}")
    return return_code


def _run_cli(*args: Any, **kwargs: Any) -> tuple[CliProcess, int]:
    capture = _begin_cli(*args, **kwargs)
    fixture = args[0]
    evidence_dir = args[1]
    model = kwargs.get("model")
    phase = kwargs.get("phase")
    try:
        return_code = _finish_cli(capture)
    finally:
        try:
            _close_attempt_gate_from_capture(capture)
        finally:
            _capture_cli_outputs(fixture, evidence_dir, capture, model, phase)
    return capture, return_code


def _minimal_environment(project_root: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(project_root / "backend" / "src"),
        "NO_PROXY": "127.0.0.1,localhost",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
    })
    return env


def _runtime_identity(fixture: RuntimeFixture, evidence_dir: Path) -> dict[str, Any]:
    attempt_dir = evidence_dir / "identity-probe"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(fixture.project_root / "scripts" / "analyze_annual.py"), "--runtime-identity"]
    process = subprocess.Popen(
        command,
        cwd=fixture.project_root,
        env=_minimal_environment(fixture.project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    termination: str | None = None
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        termination = "terminate_own_identity_probe_after_timeout"
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            termination = "kill_own_identity_probe_after_terminate_timeout"
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    stdout_path = attempt_dir / "stdout.txt"
    stderr_path = attempt_dir / "stderr.txt"
    _write_bytes_new(stdout_path, stdout)
    _write_bytes_new(stderr_path, stderr)
    _write_json_new(attempt_dir / "process-exit.json", {
        "argv": command,
        "cwd": str(fixture.project_root),
        "pid": process.pid,
        "returncode": process.returncode,
        "poll_after_wait": process.poll(),
        "exit_observed": process.poll() is not None,
        "termination_action": termination,
        "finished_at": _utc_now(),
    })
    if termination is not None:
        raise AssertionError(f"runtime identity probe failed to exit naturally: {termination}")
    if process.returncode != 0:
        raise AssertionError("runtime identity probe exited unsuccessfully")
    value = json.loads(stdout.decode("utf-8"))
    freeze = os.environ.get(_SOURCE_FREEZE_ENV)
    if value.get("source_manifest_sha256") != freeze or value.get("loaded_source_match") is not True:
        raise AssertionError("copied CLI runtime identity does not match the explicit source freeze")
    _write_json_new(attempt_dir / "runtime-identity.json", value)
    return value


def _required_frozen_case(case_name: str) -> tuple[RuntimeFixture, Path, Path, dict[str, Any]]:
    freeze = os.environ.get(_SOURCE_FREEZE_ENV)
    if not freeze:
        pytest.skip(f"等待 main 明确 production source freeze；设置 {_SOURCE_FREEZE_ENV} 后才运行身份敏感 CLI。")
    case_id = f"{case_name}-{uuid.uuid4().hex}"
    source_variant = "candidate" if case_name in {
        "candidate-source-preflight", "parent-resume", "invalid-container", "candidate-auth-stop",
        "candidate-stage-cancel", "binding-drift",
    } else "base"
    scratch_case = _SCRATCH_ROOT / case_id
    evidence_dir = _EVIDENCE_ROOT / f"annual-runtime-{case_id}"
    scratch_case.mkdir(parents=True, exist_ok=False)
    evidence_dir.mkdir(parents=True, exist_ok=False)
    _write_json_new(evidence_dir / "case-start.json", {
        "case_id": case_id,
        "case_name": case_name,
        "source_variant": source_variant,
        "explicit_source_freeze_sha256": freeze,
        "workspace_root": str(_WORKSPACE_ROOT),
        "scratch_case": str(scratch_case),
        "evidence_dir": str(evidence_dir),
        "started_at": _utc_now(),
        "cli_started": False,
    })
    workspace_sha, _count = source_manifest_fingerprint(_WORKSPACE_ROOT)
    _write_json_new(evidence_dir / "source-preflight.json", {
        "explicit_source_freeze_sha256": freeze,
        "workspace_source_manifest_sha256": workspace_sha,
        "matches_freeze": workspace_sha == freeze,
        "checked_at": _utc_now(),
    })
    if workspace_sha != freeze:
        pytest.fail(f"workspace source manifest no longer matches the explicitly supplied freeze SHA; evidence: {evidence_dir}")
    try:
        fixture = build_runtime_fixture(
            scratch_case / "project",
            frozen_workspace=_WORKSPACE_ROOT,
            freeze_sha256=freeze,
            source_variant=source_variant,
        )
    except Exception as error:
        _write_json_new(evidence_dir / "fixture-build-failure.json", {
            "error_type": type(error).__name__,
            "error": str(error),
            "failed_at": _utc_now(),
        })
        raise
    fixture_manifest = {
        "case_id": case_id,
        "source_freeze_sha256": freeze,
        "workspace_source_manifest_sha256": workspace_sha,
        "project_root": str(fixture.project_root),
        "source_pdf": str(fixture.source_pdf),
        "source_pdf_sha256": fixture.source_sha256,
        "source_pdf_relative": fixture.source_pdf_relative,
        "source_record": str(fixture.source_record),
        "company_id": fixture.company_id,
        "report_year": fixture.report_year,
        "document_id": fixture.document_id,
        "source_variant": source_variant,
        "source_registration": fixture.source_registration,
        "base_sample_sha256": fixture.base_sample_sha256,
        "base_sample_nature": "registered FINTRACE original fictional sample; source PDF is modified in this fixture",
        "added_content_nature": (
            "artificial text generated only for this runtime software test; not a real disclosure or label"
            if source_variant == "base"
            else "original synthetic MDA disclosure text added to exercise real candidate retrieval; not a real disclosure or label"
        ),
        "artifacts_root": str(fixture.project_root / "artifacts"),
        "raw_source_root": str(fixture.project_root / "data" / "raw"),
        "runtime_code_copied": ["scripts/analyze_annual.py", "backend/src/finagent/**", "config/prompts/**"],
        "harness_test_file": str(Path(__file__).resolve()),
        "harness_test_file_sha256": _sha256(Path(__file__).resolve()),
        "provider_policy": "local loopback only; synthetic API key; no real quota/provider configured",
        "created_at": _utc_now(),
    }
    _write_json_new(evidence_dir / "fixture_manifest.json", fixture_manifest)
    shutil.copy2(fixture.source_pdf, evidence_dir / "source.pdf")
    shutil.copy2(fixture.source_record, evidence_dir / "source.json")
    identity = _runtime_identity(fixture, evidence_dir)
    return fixture, scratch_case, evidence_dir, identity


def _build_binding_drift_fixture(
    parent_fixture: RuntimeFixture,
    scratch_case: Path,
    *,
    freeze_sha256: str,
    parent_run_id: str,
    variant: str,
) -> tuple[RuntimeFixture, dict[str, Any]]:
    """Copy an exact frozen clone, then alter one recovery input inside tmp only."""

    variant_root = scratch_case / f"binding-drift-{variant}" / "project"
    fixture = build_runtime_fixture(
        variant_root,
        frozen_workspace=_WORKSPACE_ROOT,
        freeze_sha256=freeze_sha256,
        source_variant="candidate",
    )
    if (
        fixture.source_sha256 != parent_fixture.source_sha256
        or _sha256(fixture.source_record) != _sha256(parent_fixture.source_record)
    ):
        raise ValueError("binding_drift_clone_source_mismatch")
    for area in ("runs", "reports"):
        source = parent_fixture.project_root / "artifacts" / area / parent_run_id
        destination = fixture.project_root / "artifacts" / area / parent_run_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)

    pristine_sha, _pristine_count = source_manifest_fingerprint(fixture.project_root)
    if pristine_sha != freeze_sha256:
        raise ValueError("binding_drift_clone_did_not_match_explicit_freeze_before_mutation")
    if variant == "prompt":
        target = fixture.project_root / "config" / "prompts" / "annual_discovery_v2.md"
        original = target.read_bytes()
        target.write_bytes(original + b"\n<!-- isolated R2 recovery binding drift test -->\n")
        field = "prompts_sha256"
    elif variant == "parser":
        target = fixture.project_root / "backend" / "src" / "finagent" / "core" / "annual_recovery.py"
        lines = target.read_text(encoding="utf-8").splitlines()
        matches = [index for index, line in enumerate(lines) if line.startswith("_PARSER_VERSION = ")]
        if len(matches) != 1:
            raise ValueError("binding_drift_parser_version_constant_not_unique")
        parser_line_index = matches[0]
        old_value = lines[parser_line_index].split("=", 1)[1].strip()
        if old_value == '"r2-controlled-parser-drift"':
            raise ValueError("binding_drift_parser_variant_already_applied")
        lines[parser_line_index] = '_PARSER_VERSION = "r2-controlled-parser-drift"'
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        field = "parser_version"
    elif variant == "plan":
        target = fixture.project_root / "backend" / "src" / "finagent" / "retrieval" / "annual_discovery_context.py"
        lines = target.read_text(encoding="utf-8").splitlines()
        matches = [index for index, line in enumerate(lines) if line.startswith("MAX_BATCH_RAW_CHARS = ")]
        if len(matches) != 1:
            raise ValueError("binding_drift_batch_limit_constant_not_unique")
        plan_line_index = matches[0]
        old_value = int(lines[plan_line_index].split("=", 1)[1].strip())
        new_value = max(1024, old_value // 2)
        if new_value == old_value:
            raise ValueError("binding_drift_batch_limit_variant_unchanged")
        lines[plan_line_index] = f"MAX_BATCH_RAW_CHARS = {new_value}"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        field = "plan_sha256"
    else:
        raise ValueError("binding_drift_variant_invalid")

    changed_sha, changed_count = source_manifest_fingerprint(fixture.project_root)
    if changed_sha == pristine_sha:
        raise ValueError("binding_drift_runtime_manifest_did_not_change")
    return fixture, {
        "variant": variant,
        "changed_binding_field": field,
        "modified_relative_path": target.relative_to(fixture.project_root).as_posix(),
        "modified_file_sha256": _sha256(target),
        "pristine_source_manifest_sha256": pristine_sha,
        "changed_source_manifest_sha256": changed_sha,
        "changed_source_manifest_file_count": changed_count,
        "freeze_sha256_before_controlled_tmp_mutation": freeze_sha256,
        "mutation_scope": "isolated tmp runtime clone only; workspace and original source freeze remain unchanged",
        "source_pdf_sha256": fixture.source_sha256,
        "source_record_sha256": _sha256(fixture.source_record),
        "source_bytes_match_parent": fixture.source_sha256 == parent_fixture.source_sha256,
    }


def _probe_modified_clone_runtime_identity(
    fixture: RuntimeFixture,
    evidence_dir: Path,
    *,
    label: str,
) -> dict[str, Any]:
    """Run the actual CLI identity probe on a deliberately changed tmp clone."""

    attempt_dir = evidence_dir / "variant-identity-probes" / f"{label}-{uuid.uuid4().hex}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(fixture.project_root / "scripts" / "analyze_annual.py"), "--runtime-identity"]
    process = subprocess.Popen(
        command,
        cwd=fixture.project_root,
        env=_minimal_environment(fixture.project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    started_at = _utc_now()
    termination: str | None = None
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        termination = "terminate_owned_modified_clone_identity_probe"
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            termination = "kill_owned_modified_clone_identity_probe"
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    _write_bytes_new(attempt_dir / "stdout.txt", stdout)
    _write_bytes_new(attempt_dir / "stderr.txt", stderr)
    _write_json_new(attempt_dir / "command.json", {
        "argv": command,
        "cwd": str(fixture.project_root),
        "shell": False,
        "provider": "not configured; runtime identity only",
        "started_at": started_at,
    })
    _write_json_new(attempt_dir / "process-exit.json", {
        "pid": process.pid,
        "returncode": process.returncode,
        "poll_after_wait": process.poll(),
        "finished_at": _utc_now(),
        "termination_action": termination,
        "exit_observed": process.poll() is not None,
    })
    if process.returncode != 0 or termination is not None:
        raise AssertionError(f"modified clone runtime identity probe failed; evidence: {attempt_dir}")
    identity = json.loads(stdout.decode("utf-8"))
    if identity.get("loaded_source_match") is not True:
        raise AssertionError(f"modified clone did not load its own runtime inputs; evidence: {attempt_dir}")
    _write_json_new(attempt_dir / "runtime-identity.json", identity)
    return identity


def _probe_parent_binding_from_clone(
    fixture: RuntimeFixture,
    evidence_dir: Path,
    *,
    label: str,
    parent_run_id: str,
    model: LoopbackAnnualModel,
) -> dict[str, Any]:
    """Recompute the actual parent binding from a modified clone without HTTP."""

    attempt_dir = evidence_dir / "binding-probes" / f"{label}-{uuid.uuid4().hex}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    request = {
        "company_id": fixture.company_id,
        "report_year": fixture.report_year,
        "document_id": fixture.document_id,
        "source_pdf_path": fixture.source_pdf_relative,
        "sha256": fixture.source_sha256,
        "mode": "model_investigation",
    }
    probe_code = (
        "import json,sys; from pathlib import Path; "
        "from finagent.core.annual_recovery import validate_annual_recovery_parent; "
        "from finagent.core.model_settings import ModelSettings; "
        "root=Path(sys.argv[1]); parent=sys.argv[2]; request=json.loads(sys.argv[3]); "
        "settings=ModelSettings(base_url=sys.argv[4],api_key=sys.argv[5],model=sys.argv[6]); "
        "value=validate_annual_recovery_parent(root,parent,request,settings=settings); "
        "print(json.dumps(value,ensure_ascii=False,separators=(',',':')))"
    )
    command = [
        sys.executable,
        "-c",
        probe_code,
        str(fixture.project_root),
        parent_run_id,
        json.dumps(request, ensure_ascii=False, separators=(",", ":")),
        model.base_url,
        _LOCAL_API_KEY,
        _FAKE_MODEL_NAME,
    ]
    process = subprocess.Popen(
        command,
        cwd=fixture.project_root,
        env=_minimal_environment(fixture.project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    started_at = _utc_now()
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
            termination = "terminate_owned_binding_probe"
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            termination = "kill_owned_binding_probe"
    else:
        termination = None
    _write_bytes_new(attempt_dir / "stdout.txt", stdout)
    _write_bytes_new(attempt_dir / "stderr.txt", stderr)
    _write_json_new(attempt_dir / "command.json", {
        "argv": command,
        "cwd": str(fixture.project_root),
        "shell": False,
        "provider": "not configured; local semantic binding calculation only",
        "api_key": "synthetic loopback key; raw value omitted",
        "started_at": started_at,
    })
    _write_json_new(attempt_dir / "process-exit.json", {
        "pid": process.pid,
        "returncode": process.returncode,
        "poll_after_wait": process.poll(),
        "finished_at": _utc_now(),
        "termination_action": termination,
        "exit_observed": process.poll() is not None,
    })
    if process.returncode != 0 or termination is not None:
        raise AssertionError(f"modified clone parent binding probe failed; evidence: {attempt_dir}")
    result = json.loads(stdout.decode("utf-8"))
    _write_json_new(attempt_dir / "parent-binding-result.json", result)
    return result


def _source_identity_variant_fixture(fixture: RuntimeFixture) -> RuntimeFixture:
    """Add a distinct, locally registered source PDF for parent binding checks."""

    document_id = f"r2-source-mismatch-{uuid.uuid4().hex[:12]}"
    relative = f"{fixture.company_id}/{fixture.report_year}/{document_id}/source.pdf"
    raw_dir = fixture.project_root / "data" / "raw" / fixture.company_id / str(fixture.report_year) / document_id
    raw_dir.mkdir(parents=True, exist_ok=False)
    source_pdf = raw_dir / "source.pdf"
    with pymupdf.open(fixture.source_pdf) as document:
        page = document.new_page()
        page.insert_text((36, 36), "CONTROLLED SOURCE IDENTITY VARIANT - LOCAL TEST ONLY", fontsize=10)
        document.save(source_pdf, garbage=3, deflate=True)
    source_sha256 = _sha256(source_pdf)
    source_record = raw_dir / "source.json"
    record = json.loads(fixture.source_record.read_text(encoding="utf-8"))
    record.update(
        document_id=document_id,
        local_path=f"data/raw/{relative}",
        sha256=source_sha256,
        source_nature=(
            str(record.get("source_nature", ""))
            + "; separate PDF with an appended synthetic page, used only for local source-binding mismatch testing"
        ),
    )
    _write_json_new(source_record, record)
    return replace(
        fixture,
        source_pdf=source_pdf,
        source_record=source_record,
        source_sha256=source_sha256,
        source_pdf_relative=relative,
        document_id=document_id,
        source_registration="synthetic new source identity used only for parent-mismatch rejection",
    )


def _attempt_gate_db(fixture: RuntimeFixture, attempt: CliProcess) -> Path:
    payload = json.loads((attempt.attempt_dir / "command.json").read_text(encoding="utf-8"))
    job_id = payload.get("job_id")
    if not isinstance(job_id, str):
        raise ValueError("cli_attempt_job_id_missing")
    return fixture.project_root / "artifacts" / "annual-analysis-jobs" / job_id / "call-gate.sqlite3"


def _close_attempt_gate(fixture: RuntimeFixture, attempt: CliProcess) -> None:
    payload = json.loads((attempt.attempt_dir / "command.json").read_text(encoding="utf-8"))
    job_id = str(payload["job_id"])
    close_admission_gate(
        _attempt_gate_db(fixture, attempt),
        f"r2-runtime-{job_id}",
    )


def _close_attempt_gate_from_capture(attempt: CliProcess) -> None:
    payload_path = attempt.attempt_dir / "command.json"
    if not payload_path.is_file():
        return
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    job_id = payload.get("job_id")
    if not isinstance(job_id, str):
        return
    # command.json records the isolated cwd and job id; derive the ledger path
    # without consulting or modifying the shared workspace.
    project_root = Path(str(payload["cwd"]))
    close_admission_gate(
        project_root / "artifacts" / "annual-analysis-jobs" / job_id / "call-gate.sqlite3",
        f"r2-runtime-{job_id}",
    )


def _reader_summary(fixture: RuntimeFixture, run_id: str, evidence_dir: Path, label: str) -> dict[str, Any]:
    attempt_dir = evidence_dir / "readers" / f"{label}-{uuid.uuid4().hex}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    code = (
        "import json,sys; from pathlib import Path; "
        "from finagent.api.annual_report_read import load_annual_report; "
        "a=load_annual_report(Path(sys.argv[1]),sys.argv[2]); r=a.report; "
        "d=r.get('model_discovery',{}); c=d.get('execution_contract',{}); "
        "i=r.get('model_investigation',{}); "
        "v={'run_id':a.run_id,'snapshot':a.snapshot,'manifest_summary':a.manifest_summary,"
        "'discovery_status':d.get('status'),'coverage':d.get('coverage'),"
        "'batch_states':[{'batch_index':x.get('batch_index'),'state':x.get('state'),'reason':x.get('reason')} for x in c.get('batches',[]) if isinstance(x,dict)],"
        "'planned_batches':d.get('coverage',{}).get('planned_batches'),"
        "'findings':d.get('findings',[]),'finding_titles':[x.get('title') for x in d.get('findings',[]) if isinstance(x,dict)],"
        "'model_call_attempt_count':i.get('model_call_attempt_count'),"
        "'model_call_count':i.get('model_call_count'),'model_investigation_status':i.get('status'),"
        "'model_review_status':r.get('model_review',{}).get('status'),"
        "'parent_run_id':a.snapshot.get('parent_run_id') if isinstance(a.snapshot,dict) else None}; "
        "print(json.dumps(v,ensure_ascii=False,separators=(',',':')))"
    )
    command = [sys.executable, "-c", code, str(fixture.project_root), run_id]
    process = subprocess.Popen(
        command,
        cwd=fixture.project_root,
        env=_minimal_environment(fixture.project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        stdout, stderr = process.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
            termination = "terminate_own_reader_probe_after_timeout"
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            termination = "kill_own_reader_probe_after_terminate_timeout"
    else:
        termination = None
    _write_bytes_new(attempt_dir / "stdout.txt", stdout)
    _write_bytes_new(attempt_dir / "stderr.txt", stderr)
    _write_json_new(attempt_dir / "process-exit.json", {
        "argv": command,
        "cwd": str(fixture.project_root),
        "pid": process.pid,
        "returncode": process.returncode,
        "poll_after_wait": process.poll(),
        "exit_observed": process.poll() is not None,
        "termination_action": termination,
        "finished_at": _utc_now(),
    })
    if termination is not None:
        raise AssertionError(f"strict reader probe failed to exit naturally: {termination}")
    if process.returncode != 0:
        raise AssertionError(f"strict annual reader rejected runtime output: {stderr.decode('utf-8', errors='replace')}")
    result = json.loads(stdout.decode("utf-8"))
    _write_json_new(attempt_dir / "summary.json", result)
    return result


def _ledger_rows(database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT admission_id, gate_key, run_id, stage, batch_index, audit_id, status, goal_counted "
            "FROM provider_attempts ORDER BY rowid"
        ).fetchall()
    return [dict(row) for row in rows]


def _audit_attempt_records(run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in run_dir.rglob("response.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("attempt_records"), list):
            records.extend(item for item in payload["attempt_records"] if isinstance(item, Mapping))
    for path in run_dir.rglob("failure.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("attempt_records"), list):
            records.extend(item for item in payload["attempt_records"] if isinstance(item, Mapping))
    return [dict(item) for item in records]


def _assert_ledger_audit_alignment(fixture: RuntimeFixture, attempt: CliProcess, run_id: str) -> dict[str, Any]:
    rows = _ledger_rows(_attempt_gate_db(fixture, attempt))
    records = _audit_attempt_records(fixture.project_root / "artifacts" / "runs" / run_id)
    admitted = [item for item in records if item.get("provider_admitted") is True]
    audit_keys = Counter((item.get("admission_id"), item.get("audit_id")) for item in admitted)
    ledger_keys = Counter((item["admission_id"], item["audit_id"]) for item in rows)
    assert audit_keys == ledger_keys
    assert all(row["goal_counted"] == 0 for row in rows)
    return {
        "ledger_rows": rows,
        "ledger_admission_count": len(rows),
        "audit_attempt_record_count": len(records),
        "audit_admitted_attempt_count": len(admitted),
        "admission_audit_uuid_pairs_aligned": True,
        "real_goal_counted_attempts": sum(int(row["goal_counted"]) for row in rows),
    }


def _assert_rejected_parent_resume(
    fixture: RuntimeFixture,
    attempt: CliProcess,
    summary: Mapping[str, Any],
    *,
    requested_parent_run_id: str,
) -> dict[str, Any]:
    """Assert a refused resume is a strict failed child with no parent seal or calls."""

    run_id = str(summary["run_id"])
    assert summary["manifest_summary"]["status"] == "failed"
    assert summary["snapshot"]["state"] == "failed"
    assert summary["snapshot"]["is_partial"] is True
    assert summary["snapshot"]["parent_run_id"] is None
    assert summary["parent_run_id"] is None
    assert summary["discovery_status"] == "not_called"
    assert summary["model_investigation_status"] == "failed"
    assert summary["model_review_status"] == "not_called"
    assert summary["model_call_count"] == 0
    assert summary["model_call_attempt_count"] == 0
    failure_path = fixture.project_root / "artifacts" / "runs" / run_id / "failure.json"
    assert failure_path.is_file()
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert isinstance(failure.get("reason"), str) and failure["reason"]
    assert failure.get("requested_resume_parent_run_id") == requested_parent_run_id
    assert failure.get("model_call_audit_status") == "confirmed_zero_admissions_before_resume_validation"
    audit_alignment = _assert_ledger_audit_alignment(fixture, attempt, run_id)
    assert audit_alignment["ledger_admission_count"] == 0
    assert audit_alignment["audit_attempt_record_count"] == 0
    assert audit_alignment["audit_admitted_attempt_count"] == 0
    return failure


def _hash_run_and_report(fixture: RuntimeFixture, run_id: str) -> dict[str, str]:
    roots = [
        fixture.project_root / "artifacts" / "runs" / run_id,
        fixture.project_root / "artifacts" / "reports" / run_id,
    ]
    result: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                relative = path.relative_to(fixture.project_root).as_posix()
                result[relative] = _sha256(path)
    return result


def _copy_run_and_report_evidence(fixture: RuntimeFixture, evidence_dir: Path, run_id: str, label: str) -> None:
    destination = evidence_dir / "durable-archives" / f"{label}-{run_id}"
    destination.mkdir(parents=True, exist_ok=False)
    for area in ("runs", "reports"):
        relative = Path("artifacts") / area / run_id
        source = fixture.project_root / relative
        if source.is_dir():
            shutil.copytree(source, destination / area / run_id)


def _capture_cli_outputs(
    fixture: RuntimeFixture,
    evidence_dir: Path,
    attempt: CliProcess,
    model: LoopbackAnnualModel | None,
    phase: str | None,
) -> None:
    """Preserve output evidence before assertions can fail or abort a case."""

    command_path = attempt.attempt_dir / "command.json"
    if not command_path.is_file():
        return
    command = json.loads(command_path.read_text(encoding="utf-8"))
    run_id = command.get("run_id")
    capture_errors: list[dict[str, str]] = []
    process_exit_path = attempt.attempt_dir / "process-exit.json"
    if not process_exit_path.is_file() and attempt.process.poll() is not None:
        return_code = attempt.process.wait(timeout=0)
        if not attempt.stdout_handle.closed:
            attempt.stdout_handle.close()
        if not attempt.stderr_handle.closed:
            attempt.stderr_handle.close()
        _write_json_new(process_exit_path, {
            "pid": attempt.process.pid,
            "returncode": return_code,
            "poll_after_wait": attempt.process.poll(),
            "started_at": attempt.started_at,
            "finished_at": _utc_now(),
            "termination_action": None,
            "exit_observed": True,
            "observation": "Popen.poll plus wait(timeout=0); request-loop did not reach its expected wait point",
        })
    run_hashes: dict[str, str] = {}
    ledger_rows: list[dict[str, Any]] = []
    audit_records: list[dict[str, Any]] = []
    http_event_count: int | None = None
    if model is not None and phase is not None:
        try:
            http_event_count = len(model.events(phase))
            _save_http_events(model, evidence_dir, phase)
        except Exception as error:
            capture_errors.append({"artifact": "http_journal", "error_type": type(error).__name__, "error": str(error)})
    if isinstance(run_id, str):
        try:
            _copy_run_and_report_evidence(fixture, evidence_dir, run_id, attempt.label)
        except Exception as error:
            capture_errors.append({"artifact": "durable_archive", "error_type": type(error).__name__, "error": str(error)})
        try:
            run_hashes = _hash_run_and_report(fixture, run_id)
        except Exception as error:
            capture_errors.append({"artifact": "run_report_hashes", "error_type": type(error).__name__, "error": str(error)})
        if isinstance(command.get("job_id"), str):
            try:
                ledger_rows = _ledger_rows(_attempt_gate_db(fixture, attempt))
                audit_records = _audit_attempt_records(fixture.project_root / "artifacts" / "runs" / run_id)
            except Exception as error:
                capture_errors.append({"artifact": "ledger_audit", "error_type": type(error).__name__, "error": str(error)})
    summary = {
        "attempt_label": attempt.label,
        "run_id": run_id,
        "job_id": command.get("job_id"),
        "parent_run_id": command.get("resume_parent_run_id"),
        "phase": phase,
        "http_event_count": http_event_count,
        "pid": attempt.process.pid,
        "returncode": attempt.process.returncode,
        "poll_after_wait": attempt.process.poll(),
        "exit_observed": attempt.process.poll() is not None,
        "runtime_fingerprint": command.get("runtime_fingerprint"),
        "run_report_hashes": run_hashes,
        "ledger_rows": ledger_rows,
        "ledger_attempt_count": len(ledger_rows),
        "audit_attempt_records": audit_records,
        "audit_attempt_record_count": len(audit_records),
        "ledger_audit_admission_pairs_aligned": Counter(
            (item.get("admission_id"), item.get("audit_id"))
            for item in audit_records if item.get("provider_admitted") is True
        ) == Counter((item.get("admission_id"), item.get("audit_id")) for item in ledger_rows),
        "capture_errors": capture_errors,
        "captured_at": _utc_now(),
    }
    _write_json_new(attempt.attempt_dir / "capture-summary.json", summary)


def _save_http_events(model: LoopbackAnnualModel, evidence_dir: Path, phase: str) -> None:
    phase_dir = evidence_dir / "http-journals"
    phase_dir.mkdir(exist_ok=True)
    _write_json_new(phase_dir / f"{phase}-{uuid.uuid4().hex}.json", {
        "phase": phase,
        "listener_host": "127.0.0.1",
        "listener_port": model.port,
        "events": model.events(phase),
        "saved_at": _utc_now(),
        "request_bodies_and_authorization_headers_retained": False,
    })


def _run_fingerprint_assertions(fixture: RuntimeFixture, evidence_dir: Path, identity: Mapping[str, Any]) -> None:
    workspace_sha, _workspace_count = source_manifest_fingerprint(_WORKSPACE_ROOT)
    scratch_sha, _scratch_count = source_manifest_fingerprint(fixture.project_root)
    _write_json_new(evidence_dir / f"source-hash-check-{uuid.uuid4().hex}.json", {
        "explicit_freeze_sha256": os.environ.get(_SOURCE_FREEZE_ENV),
        "workspace_source_manifest_sha256_after_execution": workspace_sha,
        "scratch_source_manifest_sha256_after_execution": scratch_sha,
        "runtime_identity_fingerprint": identity.get("fingerprint"),
        "workspace_matches_freeze": workspace_sha == os.environ.get(_SOURCE_FREEZE_ENV),
        "scratch_matches_freeze": scratch_sha == os.environ.get(_SOURCE_FREEZE_ENV),
        "checked_at": _utc_now(),
    })
    assert workspace_sha == scratch_sha == os.environ.get(_SOURCE_FREEZE_ENV)


def _new_run_id() -> str:
    return f"annual-analysis-{uuid.uuid4()}"


def _new_job_id() -> str:
    return f"annual-job-{uuid.uuid4().hex}"


def _write_json_new(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_bytes_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def test_controlled_fixture_pdf_is_derived_synthetic_and_does_not_change_the_sample() -> None:
    sample = _WORKSPACE_ROOT / "data" / "samples" / "annual" / "sim-annual-001-2025.pdf"
    before = _sha256(sample)
    controlled = prepare_controlled_source()
    destination = controlled.source_pdf
    assert _sha256(sample) == before
    assert controlled.base_sample_sha256 == before
    assert _sha256(destination) == controlled.source_sha256
    assert controlled.source_root.parent == _SCRATCH_ROOT
    document = pymupdf.open(destination)
    try:
        assert document.page_count == 17
        appended_text = "\n".join(document[index].get_text() for index in range(7, document.page_count))
        assert "CONTROLLED FINTRACE SOFTWARE TEST PAGE" in appended_text
        assert "no real company disclosure or label" in appended_text.lower()
        assert "R2 fixture p01 l001" in appended_text
    finally:
        document.close()


def test_loopback_model_is_ephemeral_and_returns_only_controlled_local_events() -> None:
    model = LoopbackAnnualModel().start()
    try:
        assert model.base_url.startswith("http://127.0.0.1:")
        assert model.port > 0
        model.set_phase("probe")
        model.block_batch("probe", "annual_discovery", 1)
        user_payload = {
            "batch_index": 1,
            "fragments": [{"evidence_id": "controlled-fixture-evidence", "raw_text": "controlled local response test"}],
        }
        request_body = json.dumps({
            "messages": [{"role": "user", "content": json.dumps(user_payload)}],
        }).encode("utf-8")
        response_result: list[tuple[int, bytes]] = []

        def send_probe() -> None:
            request = urllib.request.Request(
                model.base_url + "/chat/completions",
                data=request_body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer synthetic-loopback-only-key"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                response_result.append((response.status, response.read()))

        sender = threading.Thread(target=send_probe, daemon=True, name="r2-loopback-probe")
        sender.start()
        model.wait_for_block()
        received = model.wait_for_event(phase="probe", stage="annual_discovery", batch_index=1)
        assert "response_finished_at" not in received
        model.release_block()
        finished = model.wait_for_response(phase="probe", stage="annual_discovery", batch_index=1)
        sender.join(timeout=5)
        assert not sender.is_alive()
        assert len(response_result) == 1 and response_result[0][0] == 200
        assert json.loads(response_result[0][1])["choices"][0]["message"]["content"]
        assert finished["status"] == 200 and finished.get("response_finished_at")
        assert finished["phase"] == "probe" and finished["batch_index"] == 1

        # Candidate investigation requests do not carry discovery batch IDs;
        # ``None`` blocks the first request at an exact model stage.
        model.set_phase("probe_investigation")
        model.block_batch(
            "probe_investigation",
            "annual_investigation",
            candidate_signal_id="profit_up_cash_down",
            hold_timeout=90,
        )
        investigation_payload = {
            "candidate": {"signal_id": "profit_up_cash_down"},
            "narrative_evidence": [{"evidence_id": "controlled-local-narrative"}],
        }
        investigation_body = json.dumps({
            "messages": [{"role": "user", "content": json.dumps(investigation_payload)}],
        }).encode("utf-8")
        investigation_result: list[tuple[int, bytes]] = []

        def send_investigation_probe() -> None:
            request = urllib.request.Request(
                model.base_url + "/chat/completions",
                data=investigation_body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer synthetic-loopback-only-key"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                investigation_result.append((response.status, response.read()))

        investigation_sender = threading.Thread(
            target=send_investigation_probe, daemon=True, name="r2-loopback-stage-probe"
        )
        investigation_sender.start()
        model.wait_for_block()
        stage_event = model.wait_for_event(
            phase="probe_investigation",
            stage="annual_investigation",
            candidate_signal_id="profit_up_cash_down",
        )
        assert stage_event["batch_index"] is None
        assert stage_event["candidate_signal_id"] == "profit_up_cash_down"
        assert "response_finished_at" not in stage_event
        model.release_block()
        stage_finished = model.wait_for_response(
            phase="probe_investigation",
            stage="annual_investigation",
            candidate_signal_id="profit_up_cash_down",
        )
        investigation_sender.join(timeout=5)
        assert not investigation_sender.is_alive()
        assert len(investigation_result) == 1 and investigation_result[0][0] == 200
        assert stage_finished["status"] == 200 and stage_finished.get("response_finished_at")
    finally:
        model.close()


def test_candidate_source_real_deterministic_run_retrieves_both_screening_signals() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("candidate-source-preflight")
    capture, exit_code = _run_cli(
        fixture, evidence_dir, "candidate-source-deterministic-baseline",
        runtime_fingerprint=str(identity["fingerprint"]),
        deterministic=True,
    )
    command = json.loads((capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
    run_id = str(command["run_id"])
    assert exit_code == 0
    summary = _reader_summary(fixture, run_id, evidence_dir, "candidate-source-deterministic")
    assert summary["manifest_summary"]["status"] == "completed"
    assert summary["manifest_summary"]["source_pdf_sha256"] == fixture.source_sha256
    archive = load_annual_report(fixture.project_root, run_id)
    report = archive.report
    assert report.get("model_called") is False
    screening = report.get("pending_review", {}).get("candidate_signals", [])
    candidate_ids = {
        item.get("signal_id") for item in screening
        if isinstance(item, Mapping) and item.get("status") == "candidate"
    }
    assert candidate_ids == {"profit_up_cash_down", "revenue_up_cash_down"}
    source_record = json.loads(fixture.source_record.read_text(encoding="utf-8"))
    context = retrieve_annual_context(report, fixture.source_pdf, source_record)
    context_ids = {item.signal_id for item in context.signal_results if item.status == "retrieved"}
    assert context.status == "retrieved"
    assert context.source_document_id == fixture.document_id
    assert context.source_sha256 == fixture.source_sha256
    assert context.sections_found == ("mda", "financial_notes")
    assert context_ids == candidate_ids
    assert context.snippets
    signal_targets = {
        "profit_up_cash_down": "净利润",
        "revenue_up_cash_down": "营业收入",
    }
    cash_term = "经营活动产生的现金流量净额"
    for signal_id, target_term in signal_targets.items():
        assert any(
            signal_id in snippet.signal_ids
            and target_term in snippet.raw_text
            and cash_term in snippet.raw_text
            for snippet in context.snippets
        )
    base_source = prepare_controlled_source()
    assert source_record.get("base_controlled_pdf_sha256") == base_source.source_sha256
    with pymupdf.open(base_source.source_pdf) as base_pdf, pymupdf.open(fixture.source_pdf) as candidate_pdf:
        assert len(candidate_pdf) == len(base_pdf) + 1
        assert [page.get_text() for page in candidate_pdf[:-1]] == [page.get_text() for page in base_pdf]
        disclosure_text = candidate_pdf[-1].get_text()
    assert all(
        term in disclosure_text
        for term in (
            "管理层讨论与分析", "营业收入", "归属于上市公司股东的净利润",
            "经营活动产生的现金流量净额", "合并财务报表附注",
        )
    )
    raw_analysis = json.loads(
        (fixture.project_root / "artifacts" / "runs" / run_id / "analysis.json").read_text(encoding="utf-8")
    )
    fact_values = {
        (item.get("metric_id"), item.get("period_end")): item.get("raw_value")
        for item in raw_analysis.get("facts", []) if isinstance(item, Mapping)
    }
    expected_fact_values = {
        ("revenue", "2025-12-31"): "1,250,000,000.00",
        ("revenue", "2024-12-31"): "1,000,000,000.00",
        ("net_profit_parent", "2025-12-31"): "145,000,000.00",
        ("net_profit_parent", "2024-12-31"): "118,000,000.00",
        ("operating_cash_flow", "2025-12-31"): "-10,000,000.00",
        ("operating_cash_flow", "2024-12-31"): "90,000,000.00",
    }
    assert {key: fact_values.get(key) for key in expected_fact_values} == expected_fact_values
    calculation_values = {
        item.get("calculation_id"): item.get("output_value")
        for item in raw_analysis.get("calculations", []) if isinstance(item, Mapping)
    }
    assert calculation_values.get("calc:annual_difference:revenue") == "250000000.00"
    assert calculation_values.get("calc:annual_difference:net_profit_parent") == "27000000.00"
    assert calculation_values.get("calc:annual_difference:operating_cash_flow") == "-100000000.00"
    _write_bytes_new(evidence_dir / "source-hash.txt", (fixture.source_sha256 + "\n").encode("ascii"))
    _write_json_new(evidence_dir / "candidate-source-retrieval-preflight.json", {
        "run_id": run_id,
        "source_pdf_sha256": fixture.source_sha256,
        "source_record_sha256": _sha256(fixture.source_record),
        "manifest_status": summary["manifest_summary"]["status"],
        "model_called": report.get("model_called"),
        "verified_screening_candidate_ids": sorted(candidate_ids),
        "verified_financial_facts": [
            {"metric_id": metric, "period_end": period, "raw_value": value}
            for (metric, period), value in sorted(expected_fact_values.items())
        ],
        "verified_calculation_differences": {
            "revenue": calculation_values["calc:annual_difference:revenue"],
            "net_profit_parent": calculation_values["calc:annual_difference:net_profit_parent"],
            "operating_cash_flow": calculation_values["calc:annual_difference:operating_cash_flow"],
        },
        "retrieval_status": context.status,
        "retrieval_sections_found": list(context.sections_found),
        "retrieved_signal_ids": sorted(context_ids),
        "narrative_evidence_ids": [item.evidence_id for item in context.snippets],
        "narrative_pdf_pages": sorted({item.pdf_page for item in context.snippets}),
        "narrative_text": [item.raw_text for item in context.snippets],
        "source_nature": source_record.get("source_nature"),
        "completed_at": _utc_now(),
    })
    _run_fingerprint_assertions(fixture, evidence_dir, identity)


def test_cli_parent_failure_resume_uses_only_unresolved_batches_and_preserves_parent() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("parent-resume")
    run_fingerprint = str(identity["fingerprint"])
    model = LoopbackAnnualModel().start()
    try:
        # A real deterministic CLI pass establishes that the controlled source
        # parses without touching the local provider or call ledger.
        baseline, baseline_exit = _run_cli(
            fixture, evidence_dir, "deterministic-baseline",
            runtime_fingerprint=run_fingerprint,
            deterministic=True,
        )
        assert baseline_exit == 0
        baseline_run = str(json.loads((baseline.attempt_dir / "command.json").read_text(encoding="utf-8"))["run_id"])
        baseline_summary = _reader_summary(fixture, baseline_run, evidence_dir, "baseline")
        assert baseline_summary["manifest_summary"]["status"] == "completed"
        assert not (fixture.project_root / "artifacts" / "annual-analysis-jobs").exists()
        assert model.events() == []

        # This semantic partial parent keeps scanning after a rejected citation
        # and an invalid response container, allowing real downstream stages
        # to finish. The separate all-503 case covers fatal transport stopping.
        parent_capture, parent_exit = _run_cli(
            fixture, evidence_dir, "partial-parent",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="parent_semantic_partial",
        )
        parent_cmd = json.loads((parent_capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        parent_run_id = str(parent_cmd["run_id"])
        assert parent_exit == 2
        parent_summary = _reader_summary(fixture, parent_run_id, evidence_dir, "parent-before-resume")
        assert parent_summary["manifest_summary"]["status"] == "completed_with_issues"
        assert parent_summary["discovery_status"] == "partial"
        assert parent_summary["planned_batches"] >= 5
        assert any(row["batch_index"] == 3 and row["state"] == "partial" for row in parent_summary["batch_states"])
        assert any(row["batch_index"] == 4 and row["state"] == "failed" for row in parent_summary["batch_states"])
        assert "受控父项保留" in parent_summary["finding_titles"]
        assert "拒绝的错误引用线索" not in parent_summary["finding_titles"]
        parent_hashes_before = _hash_run_and_report(fixture, parent_run_id)
        assert parent_hashes_before
        _write_json_new(evidence_dir / f"parent-before-hashes-{uuid.uuid4().hex}.json", parent_hashes_before)
        _assert_ledger_audit_alignment(fixture, parent_capture, parent_run_id)
        parent_http = model.events("parent_semantic_partial")
        discovery_batch_calls = [event for event in parent_http if event["stage"] == "annual_discovery"]
        assert Counter(event["batch_index"] for event in discovery_batch_calls)[4] == 1
        assert any(
            row["batch_index"] == 4 and row["state"] == "failed" and row["reason"] == "findings_invalid"
            for row in parent_summary["batch_states"]
        )
        assert any(event["stage"] == "annual_investigation" for event in parent_http), (
            "the synthetic source should exercise at least one real candidate interpretation call"
        )
        assert any(event["stage"] == "annual_review" for event in parent_http)
        assert parent_summary["model_investigation_status"] == "completed"
        assert parent_summary["model_review_status"] == "completed"

        # A provider/model binding mismatch must reject the child before any
        # loopback HTTP admission and without changing any parent archive byte.
        mismatch, mismatch_exit = _run_cli(
            fixture, evidence_dir, "provider-binding-mismatch",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="mismatch_must_not_call",
            parent_run_id=parent_run_id,
            model_name=_FAKE_MODEL_NAME + "-mismatch",
        )
        assert mismatch_exit == 2
        assert model.events("mismatch_must_not_call") == []
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        mismatch_run_id = str(
            json.loads((mismatch.attempt_dir / "command.json").read_text(encoding="utf-8"))["run_id"]
        )
        mismatch_summary = _reader_summary(
            fixture, mismatch_run_id, evidence_dir, "provider-binding-mismatch"
        )
        _assert_rejected_parent_resume(
            fixture, mismatch, mismatch_summary, requested_parent_run_id=parent_run_id
        )
        _write_json_new(mismatch.attempt_dir / "network-check.json", {
            "http_request_count": len(model.events("mismatch_must_not_call")),
            "expected": 0,
            "manifest_terminal_status": mismatch_summary["manifest_summary"]["status"],
            "parent_run_id_after_rejection": mismatch_summary["snapshot"]["parent_run_id"],
            "parent_archive_unchanged": True,
        })

        # Full-mode (M3 + model investigation) is a different recovery binding.
        # M3 is deterministic and local; parent mismatch must stop before any
        # provider request while leaving the parent archive byte-identical.
        mode_mismatch, mode_mismatch_exit = _run_cli(
            fixture, evidence_dir, "integrated-mode-binding-mismatch",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="integrated_mode_must_not_call",
            parent_run_id=parent_run_id,
            request_mode="integrated_analysis",
            with_m3_screening=True,
        )
        assert mode_mismatch_exit == 2
        assert model.events("integrated_mode_must_not_call") == []
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        mode_summary = _reader_summary(
            fixture,
            str(json.loads((mode_mismatch.attempt_dir / "command.json").read_text(encoding="utf-8"))["run_id"]),
            evidence_dir,
            "integrated-mode-binding-mismatch",
        )
        _assert_rejected_parent_resume(
            fixture, mode_mismatch, mode_summary, requested_parent_run_id=parent_run_id
        )
        _write_json_new(mode_mismatch.attempt_dir / "network-check.json", {
            "http_request_count": len(model.events("integrated_mode_must_not_call")),
            "expected": 0,
            "manifest_terminal_status": mode_summary["manifest_summary"]["status"],
            "checkpoint_terminal_state": mode_summary["snapshot"]["state"],
            "parent_run_id_after_rejection": mode_summary["snapshot"]["parent_run_id"],
            "parent_archive_unchanged": True,
        })

        # A separately registered PDF with a different SHA/document ID must
        # also be rejected before model admission when it names this parent.
        changed_source_fixture = _source_identity_variant_fixture(fixture)
        source_mismatch, source_mismatch_exit = _run_cli(
            changed_source_fixture, evidence_dir, "source-binding-mismatch",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="source_must_not_call",
            parent_run_id=parent_run_id,
        )
        source_cmd = json.loads((source_mismatch.attempt_dir / "command.json").read_text(encoding="utf-8"))
        source_run_id = str(source_cmd["run_id"])
        assert source_mismatch_exit == 2
        assert model.events("source_must_not_call") == []
        source_summary = _reader_summary(
            changed_source_fixture, source_run_id, evidence_dir, "source-binding-mismatch"
        )
        _assert_rejected_parent_resume(
            changed_source_fixture,
            source_mismatch,
            source_summary,
            requested_parent_run_id=parent_run_id,
        )
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        _write_json_new(source_mismatch.attempt_dir / "source-identity-variant.json", {
            "source_document_id": changed_source_fixture.document_id,
            "source_pdf_sha256": changed_source_fixture.source_sha256,
            "source_pdf_relative": changed_source_fixture.source_pdf_relative,
            "source_record_sha256": _sha256(changed_source_fixture.source_record),
            "parent_run_id": parent_run_id,
            "provider_http_requests": len(model.events("source_must_not_call")),
            "parent_archive_unchanged": True,
        })

        # Retrying the same partial and failed batches with the same durable
        # findings keeps the review-relevant inputs unchanged. Investigation
        # and review must therefore be reused from the real completed parent.
        reuse_capture, reuse_exit = _run_cli(
            fixture, evidence_dir, "same-review-input-reuse-child",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="resume_child_same_partial",
            parent_run_id=parent_run_id,
        )
        reuse_cmd = json.loads((reuse_capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        reuse_run_id = str(reuse_cmd["run_id"])
        assert reuse_exit == 2
        reuse_summary = _reader_summary(fixture, reuse_run_id, evidence_dir, "same-review-input-reuse-child")
        assert reuse_summary["manifest_summary"]["status"] == "completed_with_issues"
        assert reuse_summary["discovery_status"] == "partial"
        assert reuse_summary["snapshot"]["parent_run_id"] == parent_run_id
        assert reuse_summary["model_investigation_status"] == parent_summary["model_investigation_status"]
        assert reuse_summary["model_review_status"] == parent_summary["model_review_status"]
        assert "受控父项保留" in reuse_summary["finding_titles"]
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        reuse_http = model.events("resume_child_same_partial")
        reuse_discovery_batches = [
            event["batch_index"] for event in reuse_http if event["stage"] == "annual_discovery"
        ]
        assert reuse_discovery_batches == [3, 4]
        assert all(event["stage"] not in {"annual_investigation", "annual_review"} for event in reuse_http)
        _assert_ledger_audit_alignment(fixture, reuse_capture, reuse_run_id)

        # A grandchild may reuse the same completed review while its parent is
        # still partial and has the same unfinished semantic batches. A fully
        # sealed completed parent is intentionally tested as a separate reject
        # case below; it is not a valid source for another resume.
        reuse_hashes_before_grandchild = _hash_run_and_report(fixture, reuse_run_id)
        grandchild_capture, grandchild_exit = _run_cli(
            fixture, evidence_dir, "same-review-input-reuse-grandchild",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="resume_grandchild_same_partial",
            parent_run_id=reuse_run_id,
        )
        grandchild_cmd = json.loads(
            (grandchild_capture.attempt_dir / "command.json").read_text(encoding="utf-8")
        )
        grandchild_run_id = str(grandchild_cmd["run_id"])
        assert grandchild_exit == 2
        grandchild_summary = _reader_summary(
            fixture, grandchild_run_id, evidence_dir, "same-review-input-reuse-grandchild"
        )
        assert grandchild_summary["manifest_summary"]["status"] == "completed_with_issues"
        assert grandchild_summary["discovery_status"] == "partial"
        assert grandchild_summary["snapshot"]["parent_run_id"] == reuse_run_id
        assert grandchild_summary["model_investigation_status"] == parent_summary["model_investigation_status"]
        assert grandchild_summary["model_review_status"] == parent_summary["model_review_status"]
        assert "受控父项保留" in grandchild_summary["finding_titles"]
        grandchild_http = model.events("resume_grandchild_same_partial")
        grandchild_discovery_batches = [
            event["batch_index"] for event in grandchild_http if event["stage"] == "annual_discovery"
        ]
        assert grandchild_discovery_batches == [3, 4]
        assert all(
            event["stage"] not in {"annual_investigation", "annual_review"}
            for event in grandchild_http
        )
        assert _hash_run_and_report(fixture, reuse_run_id) == reuse_hashes_before_grandchild
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        _assert_ledger_audit_alignment(fixture, grandchild_capture, grandchild_run_id)
        _write_json_new(evidence_dir / "partial-three-generation-review-reuse.json", {
            "generation_1_partial_parent_run_id": parent_run_id,
            "generation_2_same_semantic_partial_child_run_id": reuse_run_id,
            "generation_3_same_semantic_partial_grandchild_run_id": grandchild_run_id,
            "generation_1_parent_hashes_unchanged": True,
            "generation_2_child_hashes_before_and_after_grandchild": reuse_hashes_before_grandchild,
            "generation_2_http_stage_counts": dict(Counter(event["stage"] for event in reuse_http)),
            "generation_3_http_stage_counts": dict(Counter(event["stage"] for event in grandchild_http)),
            "generation_2_and_3_reused_investigation_and_review": True,
            "generation_2_and_3_retried_only_batches": [3, 4],
            "recorded_at": _utc_now(),
        })

        # A changed discovery result is review-semantic input. Branching from
        # the original partial parent retries only unfinished batches and
        # must run a fresh review exactly once for the genuinely new finding.
        child_capture, child_exit = _run_cli(
            fixture, evidence_dir, "resolved-child",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="resume_child",
            parent_run_id=parent_run_id,
        )
        child_cmd = json.loads((child_capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        child_run_id = str(child_cmd["run_id"])
        assert child_exit == 0
        child_summary = _reader_summary(fixture, child_run_id, evidence_dir, "child")
        assert child_summary["manifest_summary"]["status"] == "completed"
        assert child_summary["snapshot"]["parent_run_id"] == parent_run_id
        assert "受控父项保留" in child_summary["finding_titles"]
        assert "续跑新增父项" in child_summary["finding_titles"]
        child_http = model.events("resume_child")
        child_discovery_batches = [
            event["batch_index"] for event in child_http if event["stage"] == "annual_discovery"
        ]
        assert child_discovery_batches == [3, 4]
        assert all(event["stage"] != "annual_investigation" for event in child_http)
        assert sum(event["stage"] == "annual_review" for event in child_http) == 1
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        _assert_ledger_audit_alignment(fixture, child_capture, child_run_id)
        _write_json_new(evidence_dir / f"parent-after-hashes-{uuid.uuid4().hex}.json", _hash_run_and_report(fixture, parent_run_id))

        # A terminal completed report with no unfinished stages is not a
        # resumable parent. Preserve that exact refusal as a strict failed,
        # partial/null-parent child with no HTTP admission.
        child_hashes_before_rejection = _hash_run_and_report(fixture, child_run_id)
        completed_parent_rejection, completed_parent_rejection_exit = _run_cli(
            fixture, evidence_dir, "completed-parent-resume-must-not-call",
            runtime_fingerprint=run_fingerprint,
            model=model,
            phase="completed_parent_resume_must_not_call",
            parent_run_id=child_run_id,
        )
        completed_rejection_cmd = json.loads(
            (completed_parent_rejection.attempt_dir / "command.json").read_text(encoding="utf-8")
        )
        completed_rejection_run_id = str(completed_rejection_cmd["run_id"])
        assert completed_parent_rejection_exit == 2
        assert model.events("completed_parent_resume_must_not_call") == []
        completed_rejection_summary = _reader_summary(
            fixture, completed_rejection_run_id, evidence_dir, "completed-parent-resume-rejection"
        )
        completed_rejection_failure = _assert_rejected_parent_resume(
            fixture,
            completed_parent_rejection,
            completed_rejection_summary,
            requested_parent_run_id=child_run_id,
        )
        assert completed_rejection_failure["reason"] == "recovery_parent_not_terminal"
        assert _hash_run_and_report(fixture, child_run_id) == child_hashes_before_rejection
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        _write_json_new(completed_parent_rejection.attempt_dir / "network-check.json", {
            "requested_parent_run_id": child_run_id,
            "failure_reason": completed_rejection_failure["reason"],
            "http_request_count": len(model.events("completed_parent_resume_must_not_call")),
            "expected": 0,
            "rejected_child_manifest_status": completed_rejection_summary["manifest_summary"]["status"],
            "rejected_child_checkpoint_state": completed_rejection_summary["snapshot"]["state"],
            "rejected_child_parent_run_id": completed_rejection_summary["snapshot"]["parent_run_id"],
            "completed_parent_archive_unchanged": True,
        })
        _run_fingerprint_assertions(fixture, evidence_dir, identity)
    finally:
        model.close()


def test_cli_prompt_parser_and_plan_drift_are_rejected_before_provider_admission() -> None:
    fixture, scratch_case, evidence_dir, identity = _required_frozen_case("binding-drift")
    freeze_sha256 = str(identity["source_manifest_sha256"])
    model = LoopbackAnnualModel().start()
    try:
        parent_capture, parent_exit = _run_cli(
            fixture,
            evidence_dir,
            "binding-drift-parent",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="parent_semantic_partial",
        )
        parent_command = json.loads((parent_capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        parent_run_id = str(parent_command["run_id"])
        assert parent_exit == 2
        parent_summary = _reader_summary(fixture, parent_run_id, evidence_dir, "binding-drift-parent")
        assert parent_summary["manifest_summary"]["status"] == "completed_with_issues"
        assert parent_summary["discovery_status"] == "partial"
        assert parent_summary["model_investigation_status"] == "completed"
        assert parent_summary["model_review_status"] == "completed"
        parent_archive = load_annual_report(fixture.project_root, parent_run_id)
        parent_discovery = parent_archive.report.get("model_discovery")
        assert isinstance(parent_discovery, Mapping)
        parent_contract = parent_discovery.get("execution_contract")
        assert isinstance(parent_contract, Mapping)
        parent_binding = parent_contract.get("binding")
        assert isinstance(parent_binding, Mapping)
        parent_hashes_before = _hash_run_and_report(fixture, parent_run_id)
        assert parent_hashes_before
        _assert_ledger_audit_alignment(fixture, parent_capture, parent_run_id)

        for variant in ("prompt", "parser", "plan"):
            changed_fixture, mutation = _build_binding_drift_fixture(
                fixture,
                scratch_case,
                freeze_sha256=freeze_sha256,
                parent_run_id=parent_run_id,
                variant=variant,
            )
            changed_identity = _probe_modified_clone_runtime_identity(
                changed_fixture,
                evidence_dir,
                label=f"{variant}-drift",
            )
            assert changed_identity["source_manifest_sha256"] == mutation["changed_source_manifest_sha256"]
            assert changed_identity["source_manifest_sha256"] != freeze_sha256
            copied_parent_summary = _reader_summary(
                changed_fixture, parent_run_id, evidence_dir, f"{variant}-strict-parent-copy"
            )
            assert copied_parent_summary["manifest_summary"]["status"] == "completed_with_issues"
            validation = _probe_parent_binding_from_clone(
                changed_fixture,
                evidence_dir,
                label=variant,
                parent_run_id=parent_run_id,
                model=model,
            )
            assert validation.get("can_resume") is False
            assert validation.get("reason") == "recovery_binding_mismatch"
            changed_binding = validation.get("binding")
            assert isinstance(changed_binding, Mapping)
            changed_field = str(mutation["changed_binding_field"])
            assert changed_binding.get(changed_field) != parent_binding.get(changed_field)
            for field in ("prompts_sha256", "parser_version", "plan_sha256"):
                if field != changed_field:
                    assert changed_binding.get(field) == parent_binding.get(field)

            phase = f"{variant}_drift_must_not_call"
            child_capture, child_exit = _run_cli(
                changed_fixture,
                evidence_dir,
                f"{variant}-drift-child",
                runtime_fingerprint=str(changed_identity["fingerprint"]),
                model=model,
                phase=phase,
                parent_run_id=parent_run_id,
            )
            child_command = json.loads((child_capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
            child_run_id = str(child_command["run_id"])
            assert child_exit == 2
            assert model.events(phase) == []
            child_summary = _reader_summary(
                changed_fixture, child_run_id, evidence_dir, f"{variant}-drift-rejected-child"
            )
            child_failure = _assert_rejected_parent_resume(
                changed_fixture,
                child_capture,
                child_summary,
                requested_parent_run_id=parent_run_id,
            )
            assert child_failure.get("reason") == "recovery_binding_mismatch"
            assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
            final_variant_sha, _variant_count = source_manifest_fingerprint(changed_fixture.project_root)
            assert final_variant_sha == changed_identity["source_manifest_sha256"]
            _write_json_new(child_capture.attempt_dir / "binding-drift-evidence.json", {
                "variant": variant,
                "changed_binding_field": changed_field,
                "parent_binding_value": parent_binding.get(changed_field),
                "child_binding_value": changed_binding.get(changed_field),
                "parent_run_id": parent_run_id,
                "child_run_id": child_run_id,
                "source_pdf_sha256_same_as_parent": changed_fixture.source_sha256 == fixture.source_sha256,
                "source_record_sha256_same_as_parent": _sha256(changed_fixture.source_record) == _sha256(fixture.source_record),
                "pristine_clone_matched_explicit_freeze": mutation["pristine_source_manifest_sha256"] == freeze_sha256,
                "modified_clone_source_manifest_sha256": final_variant_sha,
                "modified_clone_runtime_fingerprint": changed_identity["fingerprint"],
                "binding_probe_reason": validation.get("reason"),
                "cli_failure_reason": child_failure.get("reason"),
                "provider_http_requests": len(model.events(phase)),
                "parent_run_report_hashes_unchanged": _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before,
            })

        _run_fingerprint_assertions(fixture, evidence_dir, identity)
    finally:
        model.close()


def test_cli_authentication_failure_stops_discovery_before_later_batches() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("auth-stop")
    model = LoopbackAnnualModel().start()
    try:
        capture, exit_code = _run_cli(
            fixture, evidence_dir, "auth-stop-cli",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="auth_stop",
        )
        command = json.loads((capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        run_id = str(command["run_id"])
        assert exit_code == 2
        summary = _reader_summary(fixture, run_id, evidence_dir, "auth-stop")
        assert summary["manifest_summary"]["status"] == "completed_with_issues"
        events = model.events("auth_stop")
        assert len(events) == 1
        assert events[0]["stage"] == "annual_discovery" and events[0]["batch_index"] == 1
        assert events[0]["status"] == 401
        assert summary["coverage"]["unattempted_batches"] > 0
        _assert_ledger_audit_alignment(fixture, capture, run_id)
        _run_fingerprint_assertions(fixture, evidence_dir, identity)
    finally:
        model.close()


def test_cli_all_transport_failures_are_bounded_and_strictly_checkpointed() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("all-failures")
    model = LoopbackAnnualModel().start()
    try:
        capture, exit_code = _run_cli(
            fixture, evidence_dir, "all-503-cli",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="all_503",
        )
        command = json.loads((capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        run_id = str(command["run_id"])
        assert exit_code == 2
        summary = _reader_summary(fixture, run_id, evidence_dir, "all-503")
        events = model.events("all_503")
        assert events
        assert all(event["status"] == 503 and event["stage"] == "annual_discovery" for event in events)
        assert summary["manifest_summary"]["status"] == "completed_with_issues"
        assert len(events) % 3 == 0
        attempted_batch_counts = Counter(event["batch_index"] for event in events)
        assert all(count == 3 for count in attempted_batch_counts.values())
        planned_batches = summary["planned_batches"]
        assert type(planned_batches) is int and planned_batches > 0
        assert len(attempted_batch_counts) <= planned_batches
        _assert_ledger_audit_alignment(fixture, capture, run_id)
        _run_fingerprint_assertions(fixture, evidence_dir, identity)
    finally:
        model.close()


def test_cli_invalid_container_and_empty_successes_remain_auditable() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("invalid-container")
    model = LoopbackAnnualModel().start()
    try:
        capture, exit_code = _run_cli(
            fixture, evidence_dir, "invalid-container-cli",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="invalid_container",
        )
        command = json.loads((capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        run_id = str(command["run_id"])
        assert exit_code == 2
        summary = _reader_summary(fixture, run_id, evidence_dir, "invalid-container")
        discovery_events = [event for event in model.events("invalid_container") if event["stage"] == "annual_discovery"]
        assert len(discovery_events) >= 4
        assert summary["discovery_status"] == "partial"
        first_batch = summary["coverage"]["batch_results"][0]
        assert summary["batch_states"][0]["batch_index"] == 1
        assert summary["batch_states"][0]["state"] == "failed"
        assert summary["batch_states"][0]["reason"] == "findings_invalid"
        assert first_batch["batch_index"] == 1
        assert first_batch["status"] == "failed"
        assert first_batch["reason"] == "findings_invalid"
        assert first_batch["accepted_findings"] == 0
        assert all(event["status"] == 200 for event in discovery_events)
        assert len(summary["findings"]) > 0
        assert "中断前已持久化线索" in summary["finding_titles"]
        assert any(item["status"] == "succeeded" for item in summary["coverage"]["batch_results"][1:])
        audit_alignment = _assert_ledger_audit_alignment(fixture, capture, run_id)
        all_events = model.events("invalid_container")
        assert audit_alignment["audit_attempt_record_count"] == len(all_events)
        assert audit_alignment["audit_admitted_attempt_count"] == len(all_events)
        assert all(event["status"] == 200 for event in all_events)
        _run_fingerprint_assertions(fixture, evidence_dir, identity)
    finally:
        model.close()


def test_cli_cancellation_during_real_loopback_call_leaves_readable_partial_parent() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("cli-cancel")
    model = LoopbackAnnualModel().start()
    cancel_file = fixture.project_root / "artifacts" / "tmp" / "cancel-requested"
    cancel_file.parent.mkdir(parents=True)
    model.block_batch("cancel_first", "annual_discovery", 1)
    capture: CliProcess | None = None
    captured = False
    try:
        capture = _begin_cli(
            fixture, evidence_dir, "cancel-during-first-http",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="cancel_first",
            cancel_file=cancel_file,
        )
        model.wait_for_block(timeout=30)
        assert model.wait_for_event(phase="cancel_first", stage="annual_discovery", batch_index=1)
        cancel_file.write_text("cancel", encoding="ascii")
        model.release_block()
        exit_code = _finish_cli(capture, timeout=90)
        _close_attempt_gate(fixture, capture)
        _capture_cli_outputs(fixture, evidence_dir, capture, model, "cancel_first")
        captured = True
        command = json.loads((capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        run_id = str(command["run_id"])
        assert exit_code == 2
        summary = _reader_summary(fixture, run_id, evidence_dir, "cancelled-parent")
        assert summary["manifest_summary"]["status"] in {"completed_with_issues", "interrupted"}
        assert summary["snapshot"]["is_partial"] is False
        assert summary["coverage"]["unattempted_batches"] > 0
        assert len(model.events("cancel_first")) == 1
        _assert_ledger_audit_alignment(fixture, capture, run_id)
        _run_fingerprint_assertions(fixture, evidence_dir, identity)
    finally:
        model.release_block()
        if capture is not None and not captured:
            process_exit_path = capture.attempt_dir / "process-exit.json"
            if not process_exit_path.is_file():
                try:
                    _finish_cli(capture, timeout=5)
                except Exception as error:
                    _write_json_new(capture.attempt_dir / f"process-finish-recovery-{uuid.uuid4().hex}.json", {
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "pid": capture.process.pid,
                        "returncode_after_recovery": capture.process.poll(),
                        "observed_at": _utc_now(),
                    })
        if capture is not None and not captured and capture.process.poll() is not None:
            try:
                _close_attempt_gate(fixture, capture)
                _capture_cli_outputs(fixture, evidence_dir, capture, model, "cancel_first")
            except Exception as error:
                _write_json_new(capture.attempt_dir / f"capture-recovery-failure-{uuid.uuid4().hex}.json", {
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "failed_at": _utc_now(),
                })
        model.close()


def test_cli_candidate_authentication_failure_stops_before_second_candidate_and_review() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("candidate-auth-stop")
    model = LoopbackAnnualModel().start()
    try:
        capture, exit_code = _run_cli(
            fixture, evidence_dir, "candidate-auth-stop-cli",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="candidate_auth_stop",
        )
        command = json.loads((capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        run_id = str(command["run_id"])
        assert exit_code == 2
        summary = _reader_summary(fixture, run_id, evidence_dir, "candidate-auth-stop")
        events = model.events("candidate_auth_stop")
        discovery_events = [event for event in events if event["stage"] == "annual_discovery"]
        candidate_events = [event for event in events if event["stage"] == "annual_investigation"]
        assert len(discovery_events) >= 4
        assert len(candidate_events) == 1
        assert candidate_events[0]["status"] == 401
        assert not any(event["stage"] == "annual_review" for event in events)
        assert summary["model_investigation_status"] in {"abstained", "failed"}
        _assert_ledger_audit_alignment(fixture, capture, run_id)
        _run_fingerprint_assertions(fixture, evidence_dir, identity)
    finally:
        model.close()


def test_cli_candidate_stage_cancel_preserves_completed_item_and_resumes_only_pending_candidate() -> None:
    fixture, _scratch_case, evidence_dir, identity = _required_frozen_case("candidate-stage-cancel")
    model = LoopbackAnnualModel().start()
    cancel_file = fixture.project_root / "artifacts" / "tmp" / "candidate-stage-cancel-requested"
    cancel_file.parent.mkdir(parents=True, exist_ok=True)
    model.block_batch(
        "candidate_stage_cancel_parent",
        "annual_investigation",
        candidate_signal_id="revenue_up_cash_down",
    )
    parent_capture: CliProcess | None = None
    parent_captured = False
    try:
        parent_capture = _begin_cli(
            fixture,
            evidence_dir,
            "candidate-stage-cancel-parent",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="candidate_stage_cancel_parent",
            cancel_file=cancel_file,
        )
        model.wait_for_block(timeout=60)
        first_candidate_response = model.wait_for_response(
            phase="candidate_stage_cancel_parent",
            stage="annual_investigation",
            candidate_signal_id="profit_up_cash_down",
            timeout=20,
        )
        blocked_candidate = model.wait_for_event(
            phase="candidate_stage_cancel_parent",
            stage="annual_investigation",
            candidate_signal_id="revenue_up_cash_down",
            timeout=20,
        )
        assert first_candidate_response.get("response_finished_at")
        assert blocked_candidate.get("response_finished_at") is None

        parent_command = json.loads((parent_capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        parent_run_id = str(parent_command["run_id"])
        active_parent_hashes = _hash_run_and_report(fixture, parent_run_id)
        assert active_parent_hashes
        active_parent_summary = _reader_summary(
            fixture, parent_run_id, evidence_dir, "candidate-stage-active-parent"
        )
        assert active_parent_summary["manifest_summary"]["status"] == "running"
        assert active_parent_summary["snapshot"]["parent_run_id"] is None

        # A second real CLI cannot branch from the live parent checkpoint while
        # the parent owns an in-flight candidate call.  It must reject before
        # making its own loopback request and leave the active parent bytes as-is.
        active_reject_capture, active_reject_exit = _run_cli(
            fixture,
            evidence_dir,
            "active-parent-must-not-call",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="active_parent_must_not_call",
            parent_run_id=parent_run_id,
        )
        active_reject_command = json.loads(
            (active_reject_capture.attempt_dir / "command.json").read_text(encoding="utf-8")
        )
        active_reject_run_id = str(active_reject_command["run_id"])
        assert active_reject_exit == 2
        assert model.events("active_parent_must_not_call") == []
        active_reject_summary = _reader_summary(
            fixture, active_reject_run_id, evidence_dir, "active-parent-rejected-child"
        )
        active_reject_failure = _assert_rejected_parent_resume(
            fixture,
            active_reject_capture,
            active_reject_summary,
            requested_parent_run_id=parent_run_id,
        )
        assert _hash_run_and_report(fixture, parent_run_id) == active_parent_hashes
        model.set_phase("candidate_stage_cancel_parent")

        # Keep the actual model connector worker inside its bounded polling loop
        # until the cancel marker has been observed; then release only our
        # loopback server request and wait on this test's own CLI Popen handle.
        cancel_file.write_text("cancel", encoding="ascii")
        time.sleep(0.25)
        model.release_block()
        model.wait_for_response(
            phase="candidate_stage_cancel_parent",
            stage="annual_investigation",
            candidate_signal_id="revenue_up_cash_down",
            timeout=10,
        )
        parent_exit = _finish_cli(parent_capture, timeout=90)
        _close_attempt_gate(fixture, parent_capture)
        _capture_cli_outputs(
            fixture,
            evidence_dir,
            parent_capture,
            model,
            "candidate_stage_cancel_parent",
        )
        parent_captured = True
        assert parent_exit == 2
        parent_summary = _reader_summary(fixture, parent_run_id, evidence_dir, "candidate-stage-cancel-parent")
        assert parent_summary["manifest_summary"]["status"] == "completed_with_issues"
        assert parent_summary["manifest_summary"]["source_pdf_sha256"] == fixture.source_sha256
        parent_archive = load_annual_report(fixture.project_root, parent_run_id)
        parent_report = parent_archive.report
        parent_investigation = parent_report.get("model_investigation")
        assert isinstance(parent_investigation, Mapping)
        parent_items = parent_investigation.get("items")
        assert isinstance(parent_items, list)
        parent_item_by_id = {
            item.get("signal_id"): item for item in parent_items if isinstance(item, Mapping)
        }
        assert parent_item_by_id.get("profit_up_cash_down", {}).get("status") == "interpretation"
        pending_item = parent_item_by_id.get("revenue_up_cash_down", {})
        assert pending_item.get("status") == "abstained"
        assert pending_item.get("reason") in {"cancelled", "call_gate_closed"}
        parent_review = parent_report.get("model_review")
        assert isinstance(parent_review, Mapping)
        assert parent_review.get("status") == "not_called"
        assert parent_review.get("reason") in {"user_cancelled", "cancelled"}
        parent_hashes_before = _hash_run_and_report(fixture, parent_run_id)
        assert parent_hashes_before
        parent_http = model.events("candidate_stage_cancel_parent")
        parent_investigation_events = [
            event for event in parent_http if event["stage"] == "annual_investigation"
        ]
        assert [event["candidate_signal_id"] for event in parent_investigation_events] == [
            "profit_up_cash_down",
            "revenue_up_cash_down",
        ]
        assert parent_investigation_events[0].get("response_finished_at")
        _assert_ledger_audit_alignment(fixture, parent_capture, parent_run_id)

        child_capture, child_exit = _run_cli(
            fixture,
            evidence_dir,
            "candidate-stage-cancel-child-resume",
            runtime_fingerprint=str(identity["fingerprint"]),
            model=model,
            phase="candidate_stage_cancel_child_resume",
            parent_run_id=parent_run_id,
        )
        child_command = json.loads((child_capture.attempt_dir / "command.json").read_text(encoding="utf-8"))
        child_run_id = str(child_command["run_id"])
        assert child_exit == 0
        child_summary = _reader_summary(fixture, child_run_id, evidence_dir, "candidate-stage-cancel-child")
        assert child_summary["manifest_summary"]["status"] == "completed"
        assert child_summary["snapshot"]["parent_run_id"] == parent_run_id
        child_archive = load_annual_report(fixture.project_root, child_run_id)
        child_report = child_archive.report
        child_investigation = child_report.get("model_investigation")
        assert isinstance(child_investigation, Mapping)
        child_items = child_investigation.get("items")
        assert isinstance(child_items, list)
        child_item_by_id = {
            item.get("signal_id"): item for item in child_items if isinstance(item, Mapping)
        }
        assert child_item_by_id.get("profit_up_cash_down", {}).get("status") == "interpretation"
        assert child_item_by_id.get("revenue_up_cash_down", {}).get("status") == "interpretation"
        child_http = model.events("candidate_stage_cancel_child_resume")
        assert not any(event["stage"] == "annual_discovery" for event in child_http)
        assert [
            event["candidate_signal_id"]
            for event in child_http if event["stage"] == "annual_investigation"
        ] == ["revenue_up_cash_down"]
        assert sum(event["stage"] == "annual_review" for event in child_http) == 1
        assert _hash_run_and_report(fixture, parent_run_id) == parent_hashes_before
        _assert_ledger_audit_alignment(fixture, child_capture, child_run_id)
        _run_fingerprint_assertions(fixture, evidence_dir, identity)
        _write_json_new(evidence_dir / "candidate-stage-cancel-recovery.json", {
            "parent_run_id": parent_run_id,
            "child_run_id": child_run_id,
            "active_parent_rejected_child_run_id": active_reject_run_id,
            "active_parent_rejection_reason": active_reject_failure.get("reason"),
            "source_document_id": fixture.document_id,
            "source_pdf_sha256": fixture.source_sha256,
            "parent_run_report_hashes_before_and_after_child": parent_hashes_before,
            "active_parent_hashes_before_and_after_rejection": active_parent_hashes,
            "parent_investigation_items": [
                {
                    "signal_id": item.get("signal_id"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                }
                for item in parent_items if isinstance(item, Mapping)
            ],
            "child_investigation_items": [
                {"signal_id": item.get("signal_id"), "status": item.get("status")}
                for item in child_items if isinstance(item, Mapping)
            ],
            "parent_http_events": parent_http,
            "child_http_events": child_http,
            "cancel_marker_written_at": _utc_now(),
        })
    finally:
        model.release_block()
        if parent_capture is not None and not parent_captured:
            if not (parent_capture.attempt_dir / "process-exit.json").is_file():
                try:
                    _finish_cli(parent_capture, timeout=5)
                except Exception as error:
                    _write_json_new(parent_capture.attempt_dir / f"process-finish-recovery-{uuid.uuid4().hex}.json", {
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "pid": parent_capture.process.pid,
                        "returncode_after_recovery": parent_capture.process.poll(),
                        "observed_at": _utc_now(),
                    })
            if parent_capture.process.poll() is not None:
                try:
                    _close_attempt_gate(fixture, parent_capture)
                    _capture_cli_outputs(
                        fixture,
                        evidence_dir,
                        parent_capture,
                        model,
                        "candidate_stage_cancel_parent",
                    )
                except Exception as error:
                    _write_json_new(parent_capture.attempt_dir / f"capture-recovery-failure-{uuid.uuid4().hex}.json", {
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "failed_at": _utc_now(),
                    })
        model.close()
