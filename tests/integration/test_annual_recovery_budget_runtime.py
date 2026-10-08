"""R3 offline budget gates using real local HTTP and CLI/API subprocesses.

The identity-sensitive cases are opt-in. They require an explicit source
freeze from the backend owner, copy only protocol source into a task-owned
temporary project, and direct every model URL to a 127.0.0.1 fixture. No local
model configuration is read and no external provider is contacted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

import pymupdf
import pytest

from finagent.api.annual_report_export import export_annual_report
from finagent.api.annual_report_read import load_annual_report
from finagent.core.annual_checkpoints import load_current_annual_checkpoint
from finagent.core.annual_runtime_identity import source_manifest_fingerprint
from finagent.core.model_settings_store import save_local_settings
from finagent.core.model_call_ledger import read_gate_stage_admission_counts
from finagent.api.annual_analysis import analyze_annual_pdf
from finagent.retrieval.annual_discovery_context import (
    COMPACT_PLAN_VERSION,
    build_annual_discovery_context,
)


_ROOT = Path(__file__).resolve().parents[2]
_TMP_BASE = _ROOT / "artifacts" / "tmp" / "recovery-r3-runtime-20261007"
_EVIDENCE_BASE = (
    _ROOT / "artifacts" / "evaluations" / "annual-recovery-r3-20261006"
    / "runtime-budget"
)
_FREEZE_ENV = "FINTRACE_R3_SOURCE_FREEZE_SHA256"
_EVIDENCE_ENV = "FINTRACE_R3_EVIDENCE_DIR"
_TMP_ENV = "FINTRACE_R3_TMP_DIR"
_MODEL_NAME = "fintrace-r3-loopback-runtime-fixture"
_LOCAL_API_KEY = "synthetic-r3-loopback-only-key"
_MAX_TEST_PDF_PAGES = 220

pytestmark = pytest.mark.skipif(
    not os.environ.get(_FREEZE_ENV),
    reason=f"等待 backend 明确 R3 source freeze；设置 {_FREEZE_ENV} 后才运行实际 CLI/API。",
)


@dataclass(frozen=True)
class ProjectFixture:
    root: Path
    source_pdf: Path
    source_record: Path
    source_sha256: str
    company_id: str
    report_year: int
    document_id: str
    runtime_source_sha256: str
    runtime_file_count: int


@dataclass(frozen=True)
class MockReply:
    status: int = 200
    content: str = "{}"
    delay_seconds: float = 0.0
    block: bool = False


class LoopbackChatFixture:
    """OpenAI-compatible fixture on loopback; captures safe request metadata."""

    def __init__(self, responder: Callable[[dict[str, Any], object], MockReply] | None = None) -> None:
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._active = 0
        self._peak = 0
        self._release_blocked = threading.Event()
        self._blocked = threading.Event()
        self._three_discovery_requests = threading.Event()
        self._responder = responder or self._success_reply
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP API
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    stage, user_payload = _classify_request(payload)
                except Exception:
                    self.send_error(400)
                    return
                with owner._lock:
                    sequence = len(owner._events) + 1
                    owner._active += 1
                    owner._peak = max(owner._peak, owner._active)
                    event = {
                        "sequence": sequence,
                        "stage": stage,
                        "batch_index": _batch_index(user_payload),
                        "received_at": _utc_now(),
                        "received_monotonic": time.monotonic(),
                        "auth_header_present": bool(self.headers.get("Authorization")),
                        "request_body_sha256": hashlib.sha256(
                            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                        ).hexdigest(),
                        "request_body_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                        "candidate_signal_id": _candidate_signal_id(user_payload),
                    }
                    owner._events.append(event)
                    discovery_count = sum(item["stage"] == "annual_discovery" for item in owner._events)
                    if discovery_count >= 3:
                        owner._three_discovery_requests.set()
                reply = owner._responder(event, user_payload)
                if reply.block:
                    owner._blocked.set()
                    owner._release_blocked.wait(timeout=180)
                if reply.delay_seconds > 0:
                    time.sleep(reply.delay_seconds)
                body = _chat_response(reply.content, sequence)
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    with owner._lock:
                        event["response_finished_at"] = _utc_now()
                        event["response_finished_monotonic"] = time.monotonic()
                        event["status"] = reply.status
                except (BrokenPipeError, ConnectionResetError, OSError):
                    with owner._lock:
                        event["client_disconnected_before_response"] = True
                        event["status"] = reply.status
                finally:
                    with owner._lock:
                        owner._active -= 1
                    self.close_connection = True

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="r3-loopback-chat-fixture",
        )
        self._started = False

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "LoopbackChatFixture":
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def events(self, stage: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = [dict(item) for item in self._events]
        return values if stage is None else [item for item in values if item["stage"] == stage]

    def set_responder(self, responder: Callable[[dict[str, Any], object], MockReply]) -> None:
        with self._lock:
            self._responder = responder

    @property
    def actual_http_peak(self) -> int:
        with self._lock:
            return self._peak

    def wait_for(self, predicate: Callable[[list[dict[str, Any]]], bool], timeout: float = 20) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()
            if predicate(events):
                return events
            time.sleep(0.025)
        raise TimeoutError("loopback_http_event_not_observed")

    def wait_blocked(self, timeout: float = 20) -> None:
        if not self._blocked.wait(timeout):
            raise TimeoutError("loopback_provider_request_not_blocked")

    def wait_for_three_discovery_requests(self, timeout: float = 20) -> None:
        if not self._three_discovery_requests.wait(timeout):
            raise TimeoutError("three_concurrent_discovery_http_requests_not_observed")

    def release(self) -> None:
        self._release_blocked.set()

    def close(self) -> None:
        self.release()
        if self._started:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._started = False

    @staticmethod
    def _success_reply(event: dict[str, Any], user_payload: object) -> MockReply:
        if event["stage"] == "annual_discovery":
            content = json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
        elif event["stage"] == "annual_investigation":
            signal_id = _candidate_signal_id(user_payload) or "profit_up_cash_down"
            evidence_rows = (
                user_payload.get("narrative_evidence", [])
                if isinstance(user_payload, Mapping)
                else []
            )
            evidence_ids = [
                item["evidence_id"]
                for item in evidence_rows
                if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
            ][:8]
            content = json.dumps({
                "signal_id": signal_id,
                "explanation": "材料记录相关项目的变化，但没有足够信息确认它们之间的因果关系。",
                "alternative_explanations": [],
                "limitations": ["人工构造的软件测试材料不支持业务判断。"],
                "narrative_evidence_ids": evidence_ids,
                "request_more_context": False,
            }, ensure_ascii=False)
        else:
            content = json.dumps({
                "assessment": "insufficient_evidence",
                "summary": "本次仅验证离线运行链路。",
                "reasons": [],
                "follow_up_items": [],
                "limitations": ["输入为人工构造的软件测试材料。"],
            }, ensure_ascii=False)
        return MockReply(content=content)


@dataclass
class CliAttempt:
    attempt_id: str
    run_id: str
    job_id: str | None
    attempt_dir: Path
    command: list[str]
    process: subprocess.Popen[bytes]
    stdout_handle: Any
    stderr_handle: Any
    started_at: str
    started_monotonic: float
    fixture: ProjectFixture
    model_event_count_at_start: int


def _case_paths(name: str) -> tuple[str, Path, Path]:
    run_id = os.environ.get("FINTRACE_R3_RUN_ID", uuid.uuid4().hex)
    attempt_id = os.environ.get("FINTRACE_R3_ATTEMPT_ID", uuid.uuid4().hex)
    tmp_root = Path(os.environ.get(_TMP_ENV, _TMP_BASE / run_id / attempt_id)).resolve()
    evidence_root = Path(os.environ.get(_EVIDENCE_ENV, _EVIDENCE_BASE / run_id / "attempts" / attempt_id)).resolve()
    tmp_root.mkdir(parents=True, exist_ok=False)
    evidence_root.mkdir(parents=True, exist_ok=False)
    _write_json_new(evidence_root / "attempt-start.json", {
        "test_case": name,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "started_at": _utc_now(),
        "source_freeze_sha256": os.environ.get(_FREEZE_ENV),
        "provider_policy": "loopback only; no credentials read from the workspace",
        "actual_external_provider_requests": 0,
    })
    _assert_source_freeze()
    return attempt_id, tmp_root, evidence_root


def _assert_source_freeze() -> str:
    expected = os.environ.get(_FREEZE_ENV)
    if not isinstance(expected, str) or len(expected) != 64:
        pytest.skip(f"缺少有效的 {_FREEZE_ENV}；当前运行身份不可验收。")
    current, _count = source_manifest_fingerprint(_ROOT)
    if current != expected:
        pytest.fail("工作区已偏离明确的 R3 source freeze；不得用非冻结代码冒称通过。")
    return expected


def _new_project(tmp_case: Path, *, pages: int, evidence_root: Path, name: str) -> ProjectFixture:
    if not 1 <= pages <= _MAX_TEST_PDF_PAGES:
        raise ValueError("controlled_pdf_page_count_invalid")
    project = tmp_case / "project"
    project.mkdir(parents=True, exist_ok=False)
    (project / "backend" / "src").mkdir(parents=True)
    shutil.copytree(_ROOT / "backend" / "src" / "finagent", project / "backend" / "src" / "finagent")
    (project / "scripts").mkdir()
    shutil.copy2(_ROOT / "scripts" / "analyze_annual.py", project / "scripts" / "analyze_annual.py")
    (project / "config").mkdir()
    shutil.copytree(_ROOT / "config" / "prompts", project / "config" / "prompts")
    (project / "artifacts").mkdir()

    company_id = f"SIM-R3-{uuid.uuid4().hex[:8]}"
    report_year = 2025
    document_id = f"r3-runtime-{uuid.uuid4().hex[:8]}"
    source_pdf = project / "data" / "raw" / company_id / str(report_year) / document_id / "source.pdf"
    source_pdf.parent.mkdir(parents=True)
    _write_controlled_pdf(source_pdf, pages=pages)
    source_sha256 = _sha256(source_pdf)
    source_record = source_pdf.parent / "source.json"
    _write_json_new(source_record, {
        "company_id": company_id,
        "document_id": document_id,
        "report_type": "年度报告（原创虚构软件测试材料）",
        "report_period": f"{report_year}-12-31",
        "local_path": f"data/raw/{company_id}/{report_year}/{document_id}/source.pdf",
        "sha256": source_sha256,
        "source_nature": "本次运行时由验收 harness 原创构造；非真实公司披露、非财务黄金答案、非异常/舞弊标签",
        "construction": "虚构财务数值只用于触发确定性候选；附加正文只用于生成全文批次；不含真实公司资料",
    })
    formal_input = evidence_root / "inputs" / f"{name}-{source_sha256[:12]}.pdf"
    formal_input.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, formal_input)
    source_hash, source_file_count = source_manifest_fingerprint(project)
    expected_source_hash = _assert_source_freeze()
    if source_hash != expected_source_hash:
        raise AssertionError("copied_runtime_source_does_not_match_explicit_freeze")
    _write_json_new(evidence_root / "controlled-input.json", {
        "input_nature": "synthetic software-test PDF generated for this attempt",
        "financial_golden_answers_copied": False,
        "external_provider_requests": 0,
        "formal_input_pdf": str(formal_input),
        "fixture_project_pdf": str(source_pdf),
        "source_sha256": source_sha256,
        "source_registration": str(source_record),
        "page_count": pages,
        "construction": "first page contains original invented metrics for candidate-stage runtime only; later pages contain artificial narrative lines; no real company data",
        "runtime_source_manifest_sha256": source_hash,
        "runtime_source_file_count": source_file_count,
    })
    return ProjectFixture(
        project, source_pdf, source_record, source_sha256, company_id,
        report_year, document_id, source_hash, source_file_count,
    )


def _new_project_from_annual_sample(tmp_case: Path, *, evidence_root: Path, name: str) -> ProjectFixture:
    """Derive an isolated synthetic PDF with narrative sections from the registered sample."""
    sample_dir = _ROOT / "data" / "samples" / "annual"
    sample_manifest_path = sample_dir / "source_manifest.json"
    manifest = json.loads(sample_manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], Mapping):
        raise AssertionError("registered_original_annual_sample_manifest_invalid")
    sample_record = records[0]
    sample_pdf = sample_dir / str(sample_record.get("file"))
    company_id = str(sample_record.get("company_id"))
    report_year = int(sample_record.get("report_year"))
    document_id = str(sample_record.get("document_id"))
    expected_sha256 = str(sample_record.get("sha256"))
    source_sha256 = _sha256(sample_pdf)
    if source_sha256 != expected_sha256:
        raise AssertionError("registered_original_annual_sample_hash_mismatch")
    base_document = pymupdf.open(sample_pdf)
    try:
        base_page_count = len(base_document)
    finally:
        base_document.close()

    project = tmp_case / "project"
    project.mkdir(parents=True, exist_ok=False)
    (project / "backend" / "src").mkdir(parents=True)
    shutil.copytree(_ROOT / "backend" / "src" / "finagent", project / "backend" / "src" / "finagent")
    (project / "scripts").mkdir()
    shutil.copy2(_ROOT / "scripts" / "analyze_annual.py", project / "scripts" / "analyze_annual.py")
    (project / "config").mkdir()
    shutil.copytree(_ROOT / "config" / "prompts", project / "config" / "prompts")
    (project / "artifacts").mkdir()

    source_pdf = project / "data" / "raw" / company_id / str(report_year) / document_id / "source.pdf"
    source_pdf.parent.mkdir(parents=True)
    base_pdf = pymupdf.open(sample_pdf)
    try:
        mda_page = base_pdf.new_page()
        mda_lines = (
            "第三节 管理层讨论与分析",
            "本年度营业收入及净利润较上年同期增加。",
            "经营活动现金流量净额较上年同期下降，回款节奏存在变化。",
            "经营性应收项目增加。",
            "以上内容是控制测试加入的虚构说明，不构成实际披露或原因认定。",
            "第四节 公司治理",
        )
        for index, line in enumerate(mda_lines):
            mda_page.insert_text((45, 65 + index * 28), line, fontsize=9, fontname="china-s")
        notes_page = base_pdf.new_page()
        notes_lines = (
            "第十节 财务报告",
            "七、合并财务报表项目注释",
            "将净利润调节为经营活动现金流量，经营性应收项目增加。",
            "该段为控制测试附加的虚构说明，不构成实际披露或原因认定。",
        )
        for index, line in enumerate(notes_lines):
            notes_page.insert_text((45, 65 + index * 30), line, fontsize=9, fontname="china-s")
        base_pdf.set_metadata({
            "title": "受控离线软件测试：原创模拟年报派生材料",
            "subject": "保留注册模拟样例并追加虚构章节，仅用于固定调查预算验收",
            "creator": "tests/integration/test_annual_recovery_budget_runtime.py",
        })
        base_pdf.save(source_pdf, garbage=3, deflate=True)
    finally:
        base_pdf.close()
    source_sha256 = _sha256(source_pdf)
    source_record = source_pdf.parent / "source.json"
    _write_json_new(source_record, {
        "company_id": company_id,
        "document_id": document_id,
        "report_type": "年度报告（FINTRACE原创模拟样例）",
        "report_period": f"{report_year}-12-31",
        "local_path": f"data/raw/{company_id}/{report_year}/{document_id}/source.pdf",
        "sha256": source_sha256,
        "metadata_origin": "controlled_derivative_of_repository_registered_original_synthetic_sample",
        "nature": "原创模拟衍生软件测试输入；原始样例以外的章节为本次人工虚构添加",
        "use_condition": "仅本机离线软件验收；不对应真实公司披露，不作投资、舞弊或模型训练标签",
        "base_sample_id": sample_record.get("sample_id"),
        "base_sample_sha256": expected_sha256,
        "derivative_registration": "test-owned input; not an exact-hash product sample registration",
        "fraud_label": False,
        "model_training": False,
    })
    formal_input_dir = evidence_root / "inputs"
    formal_input_dir.mkdir(parents=True, exist_ok=True)
    formal_base_input = formal_input_dir / f"{name}-registered-base-{expected_sha256[:12]}.pdf"
    shutil.copy2(sample_pdf, formal_base_input)
    formal_input = formal_input_dir / f"{name}-synthetic-derivative-{source_sha256[:12]}.pdf"
    shutil.copy2(source_pdf, formal_input)
    formal_manifest = formal_input_dir / f"{name}-source-manifest.json"
    shutil.copy2(sample_manifest_path, formal_manifest)
    derivative_manifest = formal_input_dir / f"{name}-derivative-manifest.json"
    _write_json_new(derivative_manifest, {
        "input_nature": "原创模拟衍生材料；未注册为产品样例",
        "base_sample": {
            "sample_id": sample_record.get("sample_id"),
            "path": str(sample_pdf),
            "sha256": expected_sha256,
            "source_manifest": str(sample_manifest_path),
        },
        "derived_pdf_sha256": source_sha256,
        "appended_pages": [
            {"pdf_page": base_page_count + 1, "section": "虚构管理层讨论与分析", "content_origin": "test-owned synthetic text"},
            {"pdf_page": base_page_count + 2, "section": "虚构财务报表项目注释", "content_origin": "test-owned synthetic text"},
        ],
        "limitations": [
            "基础样例哈希与注册记录一致；派生文件因添加虚构章节而具有新哈希。",
            "产品读取器应将派生文件标为 unconfirmed，不应误称 exact-hash registered sample。",
            "全部模型响应由 loopback fixture 提供，未使用真实provider。",
        ],
    })
    formal_use_conditions = formal_input_dir / f"{name}-useconditions.md"
    shutil.copy2(sample_dir / "useconditions.md", formal_use_conditions)
    source_hash, source_file_count = source_manifest_fingerprint(project)
    expected_source_hash = _assert_source_freeze()
    if source_hash != expected_source_hash:
        raise AssertionError("copied_runtime_source_does_not_match_explicit_freeze")
    sample_document = pymupdf.open(source_pdf)
    try:
        page_count = len(sample_document)
    finally:
        sample_document.close()
    _write_json_new(evidence_root / "controlled-input.json", {
        "input_nature": "derivative of repository-registered original synthetic annual sample with test-authored synthetic MDA/notes pages",
        "financial_golden_answers_copied": False,
        "external_provider_requests": 0,
        "source_pdf": str(formal_input),
        "registered_base_pdf": str(formal_base_input),
        "source_manifest": str(formal_manifest),
        "derivative_manifest": str(derivative_manifest),
        "use_conditions": str(formal_use_conditions),
        "fixture_project_pdf": str(source_pdf),
        "source_sha256": source_sha256,
        "source_registration": str(source_record),
        "company_id": company_id,
        "document_id": document_id,
        "report_year": report_year,
        "page_count": page_count,
        "sample_id": sample_record.get("sample_id"),
        "base_sample_sha256": expected_sha256,
        "construction": "unchanged registered sample copied as base; two original fictional section pages appended in isolated project; no real issuer data, no fraud label, no online provider",
        "runtime_source_manifest_sha256": source_hash,
        "runtime_source_file_count": source_file_count,
    })
    return ProjectFixture(
        project, source_pdf, source_record, source_sha256, company_id,
        report_year, document_id, source_hash, source_file_count,
    )


def _write_controlled_pdf(destination: Path, *, pages: int) -> None:
    document = pymupdf.open()
    try:
        document.set_metadata({
            "title": "R3 runtime budget fixture — fictional and synthetic",
            "subject": "Original software-test input; no real issuer or evaluation labels",
            "creator": "tests/integration/test_annual_recovery_budget_runtime.py",
        })
        for page_index in range(pages):
            page = document.new_page()
            page.insert_text((32, 28), "ORIGINAL FICTIONAL SOFTWARE TEST MATERIAL - NOT A REAL REPORT", fontsize=7)
            page.insert_text((32, 48), f"测试材料页 {page_index + 1}；原创虚构，不是实际公司披露、黄金答案或舞弊标签。", fontname="china-s", fontsize=8)
            if page_index == 0:
                page.insert_text((32, 82), "营业收入：2025年度1,550,000,000元；2024年度1,230,000,000元。", fontname="china-s", fontsize=8)
                page.insert_text((32, 102), "归属于上市公司股东的净利润：2025年度168,000,000元；2024年度132,000,000元。", fontname="china-s", fontsize=8)
                page.insert_text((32, 122), "经营活动产生的现金流量净额：2025年度-12,000,000元；2024年度88,000,000元。", fontname="china-s", fontsize=8)
                page.insert_text((32, 150), "本页为构造数值，只用于触发本机确定性软件路径，不代表任何真实财务事项。", fontname="china-s", fontsize=8)
            else:
                for line_index in range(52):
                    text = (
                        f"原创软件测试正文 R3 页{page_index + 1:03d} 段{line_index + 1:03d}："
                        "人工构造的经营说明和合同履约背景，仅用于全文批次、请求准入、乱序完成与预算截止验收。"
                    )
                    page.insert_text((32, 70 + line_index * 13), text, fontname="china-s", fontsize=6.3)
        document.save(destination, garbage=3, deflate=True)
    finally:
        document.close()


def _runtime_identity(fixture: ProjectFixture, evidence_root: Path, name: str) -> dict[str, Any]:
    attempt_dir = evidence_root / f"identity-{name}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(fixture.root / "scripts" / "analyze_annual.py"), "--runtime-identity"]
    process = subprocess.Popen(
        command,
        cwd=fixture.root,
        env=_minimal_environment(fixture.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    started = time.monotonic()
    termination = None
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        termination = "terminate_owned_runtime_identity_probe"
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
    _write_bytes_new(attempt_dir / "stdout.raw", stdout)
    _write_bytes_new(attempt_dir / "stderr.raw", stderr)
    record = {
        "argv": command,
        "pid": process.pid,
        "returncode": process.returncode,
        "exit_observed": process.poll() is not None,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "termination_action": termination,
        "finished_at": _utc_now(),
    }
    _write_json_new(attempt_dir / "process-exit.json", record)
    if termination is not None or process.returncode != 0:
        raise AssertionError("frozen_cli_runtime_identity_probe_failed")
    value = json.loads(stdout.decode("utf-8"))
    if value.get("source_manifest_sha256") != fixture.runtime_source_sha256 or value.get("loaded_source_match") is not True:
        raise AssertionError("copied_cli_runtime_identity_does_not_match_frozen_source")
    _write_json_new(attempt_dir / "runtime-identity.json", value)
    return value


def _minimal_environment(project_root: Path, *, model_url: str | None = None) -> dict[str, str]:
    env = {
        name: value for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(name))
    }
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(project_root / "backend" / "src"),
        "NO_PROXY": "127.0.0.1,localhost",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
    })
    if model_url is not None:
        env.update({
            "MODEL_BASE_URL": model_url,
            "MODEL_API_KEY": _LOCAL_API_KEY,
            "MODEL_NAME": _MODEL_NAME,
        })
    return env


def _begin_cli(
    fixture: ProjectFixture,
    evidence_root: Path,
    label: str,
    model: LoopbackChatFixture,
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    concurrency: int | None = None,
    cancel_file: Path | None = None,
    main_soft_deadline: float | None = None,
    main_hard_deadline: float | None = None,
    main_limit_seconds: int | None = None,
    stage_time_limits: Mapping[str, int] | None = None,
) -> CliAttempt:
    attempt_id = f"{label}-{uuid.uuid4().hex}"
    attempt_dir = evidence_root / "cli-attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    chosen_run_id = run_id or _new_run_id()
    job_id = None
    command = [
        sys.executable,
        str(fixture.root / "scripts" / "analyze_annual.py"),
        "--source-pdf", str(fixture.source_pdf),
        "--company-id", fixture.company_id,
        "--report-year", str(fixture.report_year),
        "--document-id", fixture.document_id,
        "--expected-sha256", fixture.source_sha256,
        "--expected-runtime-fingerprint", str(_runtime_identity(fixture, evidence_root, attempt_id)["fingerprint"]),
        "--run-id", chosen_run_id,
        "--with-model",
        "--source-record", str(fixture.source_record),
        "--request-mode", "model_investigation",
    ]
    # These invocations intentionally exercise the standalone CLI's own
    # run-local admission gate. API-owned gate propagation is covered through
    # the separately spawned local API process below.
    if parent_run_id is not None:
        command.extend(["--resume-parent-run-id", parent_run_id])
    env = _minimal_environment(fixture.root, model_url=model.base_url)
    if concurrency is not None:
        env["FINTRACE_DISCOVERY_CONCURRENCY"] = str(concurrency)
    if cancel_file is not None:
        env["FINTRACE_CALL_CANCEL_FILE"] = str(cancel_file)
    stopped_file = attempt_dir / "call-stopped.txt"
    env["FINTRACE_CALL_STOPPED_FILE"] = str(stopped_file)
    supplied_deadline_values = (main_soft_deadline, main_hard_deadline, main_limit_seconds, stage_time_limits)
    if any(value is not None for value in supplied_deadline_values):
        if not all(value is not None for value in supplied_deadline_values):
            raise ValueError("controlled_cli_deadline_requires_complete_contract")
        env["FINTRACE_ANNUAL_MAIN_SOFT_DEADLINE_MONOTONIC"] = format(float(main_soft_deadline), ".17g")
        env["FINTRACE_ANNUAL_MAIN_HARD_DEADLINE_MONOTONIC"] = format(float(main_hard_deadline), ".17g")
        env["FINTRACE_ANNUAL_MAIN_LIMIT_SECONDS"] = str(main_limit_seconds)
        env["FINTRACE_ANNUAL_STAGE_TIME_LIMITS_SECONDS"] = json.dumps(dict(stage_time_limits), sort_keys=True)
    stdout_handle = (attempt_dir / "stdout.raw").open("xb")
    stderr_handle = (attempt_dir / "stderr.raw").open("xb")
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    model_event_count_at_start = len(model.events())
    process = subprocess.Popen(
        command,
        cwd=fixture.root,
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        shell=False,
    )
    process_creation = _process_creation_metadata(process.pid)
    capture = CliAttempt(
        attempt_id, chosen_run_id, job_id, attempt_dir, command, process,
        stdout_handle, stderr_handle, started_at, started_monotonic, fixture,
        model_event_count_at_start,
    )
    _write_json_new(attempt_dir / "command.json", {
        "argv": command,
        "cwd": str(fixture.root),
        "shell": False,
        "pid": process.pid,
        "process_creation_metadata": process_creation,
        "run_id": chosen_run_id,
        "job_id": job_id,
        "resume_parent_run_id": parent_run_id,
        "provider": "loopback only",
        "model_name": _MODEL_NAME,
        "api_key": "synthetic value omitted",
        "budget_environment_overrides": {
            "concurrency": concurrency,
            "main_soft_deadline_monotonic": main_soft_deadline,
            "main_hard_deadline_monotonic": main_hard_deadline,
            "main_limit_seconds": main_limit_seconds,
            "stage_time_limits_seconds": dict(stage_time_limits) if stage_time_limits is not None else None,
            "task_attempt_limit_injected_by_harness": False,
            "stage_attempt_limits_injected_by_harness": False,
            "stage_attempt_limits": None,
            "standalone_cli_gate_expected": True,
            "stop_marker_path": str(stopped_file),
        },
        "started_at": started_at,
        "started_monotonic": started_monotonic,
    })
    return capture


def _finish_cli(capture: CliAttempt, *, timeout: float = 45) -> int:
    termination: str | None = None
    try:
        returncode = capture.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        termination = "terminate_only_this_owned_cli_after_test_timeout"
        capture.process.terminate()
        try:
            returncode = capture.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            termination = "kill_only_this_owned_cli_after_terminate_timeout"
            capture.process.kill()
            returncode = capture.process.wait(timeout=5)
    finally:
        capture.stdout_handle.close()
        capture.stderr_handle.close()
    _write_json_new(capture.attempt_dir / "process-exit.json", {
        "pid": capture.process.pid,
        "returncode": returncode,
        "exit_observed": capture.process.poll() is not None,
        "started_at": capture.started_at,
        "finished_at": _utc_now(),
        "elapsed_seconds": round(time.monotonic() - capture.started_monotonic, 6),
        "termination_action": termination,
    })
    if termination is not None:
        raise AssertionError(f"owned_cli_did_not_exit_naturally:{termination}")
    return returncode


def _capture_cli_evidence(capture: CliAttempt, evidence_root: Path, model: LoopbackChatFixture, label: str) -> None:
    project = capture.fixture.root
    for source, kind in (
        (project / "artifacts" / "runs" / capture.run_id, "runs"),
        (project / "artifacts" / "reports" / capture.run_id, "reports"),
    ):
        if source.is_dir():
            destination = evidence_root / "runtime-artifacts" / label / kind / capture.run_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
    progress = project / "artifacts" / "annual-analysis-jobs" / capture.job_id if capture.job_id else None
    if progress is not None and progress.is_dir():
        destination = evidence_root / "runtime-artifacts" / label / "job" / capture.job_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(progress, destination)
    gate_database = project / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3"
    if gate_database.is_file():
        gate_evidence = evidence_root / "ledgers" / label
        gate_evidence.mkdir(parents=True, exist_ok=False)
        shutil.copy2(gate_database, gate_evidence / "call-gate.sqlite3")
        _write_json_new(gate_evidence / "stage-admission-counts.json", {
            "gate_key": capture.run_id,
            "database_sha256": _sha256(gate_database),
            "stage_admission_counts": read_gate_stage_admission_counts(gate_database, capture.run_id),
            "reads_are_read_only": True,
        })
    stopped_file = capture.attempt_dir / "call-stopped.txt"
    if stopped_file.is_file():
        _write_bytes_new(evidence_root / f"call-stopped-{label}.txt", stopped_file.read_bytes())
    attempt_events = [
        event for event in model.events()
        if int(event.get("sequence", 0)) > capture.model_event_count_at_start
    ]
    _write_json_new(evidence_root / f"http-events-{label}.json", {
        "provider": "127.0.0.1 loopback fixture",
        "provider_base_url": model.base_url,
        "provider_port": model.port,
        "actual_http_peak": model.actual_http_peak,
        "events": attempt_events,
        "actual_http_requests_for_attempt": len(attempt_events),
    })


def _read_report(fixture: ProjectFixture, run_id: str) -> Any:
    return load_annual_report(fixture.root, run_id)


def _read_latest_discovery_checkpoint(
    fixture: ProjectFixture, run_id: str
) -> tuple[int, Mapping[str, Any]] | None:
    pointer_path = fixture.root / "artifacts" / "runs" / run_id / "checkpoint-current.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        relative_snapshot = pointer.get("snapshot_path")
        if not isinstance(relative_snapshot, str):
            return None
        snapshot_path = (fixture.root / relative_snapshot).resolve()
        try:
            snapshot_path.relative_to(fixture.root.resolve())
        except ValueError:
            return None
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        report = snapshot.get("report")
        if not isinstance(report, Mapping):
            return None
        return int(snapshot.get("sequence", pointer.get("sequence", 0))), _discovery_from_report(report)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AssertionError):
        return None


def _wait_for_stage_quota_close_checkpoint(
    fixture: ProjectFixture,
    capture: CliAttempt,
    *,
    timeout: float = 20,
    expected_inflight: int = 2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if capture.process.poll() is not None:
            raise AssertionError("cli_exited_before_held_quota_checkpoint_was_observed")
        latest = _read_latest_discovery_checkpoint(fixture, capture.run_id)
        if latest is not None:
            sequence, discovery = latest
            contract = discovery.get("execution_contract")
            scheduler = contract.get("scheduler") if isinstance(contract, Mapping) else None
            if isinstance(scheduler, Mapping) and (
                scheduler.get("dispatch_closed_reason") == "discovery_stage_budget_exhausted"
                and scheduler.get("time_deadline_reason") is None
                and scheduler.get("inflight_count") == expected_inflight
                and scheduler.get("inflight_drained") is False
            ):
                return {
                    "sequence": sequence,
                    "observed_at": _utc_now(),
                    "observed_monotonic": time.monotonic(),
                    "scheduler": dict(scheduler),
                    "coverage": dict(discovery.get("coverage", {})),
                }
        time.sleep(0.025)
    raise TimeoutError("held_stage_quota_close_checkpoint_not_observed")


def _wait_for_stage_deadline_checkpoint(
    fixture: ProjectFixture,
    capture: CliAttempt,
    *,
    after_sequence: int,
    expected_reason: str = "discovery_stage_budget_exhausted",
    timeout: float = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = _read_latest_discovery_checkpoint(fixture, capture.run_id)
        if latest is not None:
            sequence, discovery = latest
            contract = discovery.get("execution_contract")
            scheduler = contract.get("scheduler") if isinstance(contract, Mapping) else None
            if (
                sequence > after_sequence
                and isinstance(scheduler, Mapping)
                and scheduler.get("dispatch_closed_reason") == expected_reason
                and scheduler.get("time_deadline_reason") == expected_reason
            ):
                return {
                    "sequence": sequence,
                    "observed_at": _utc_now(),
                    "observed_monotonic": time.monotonic(),
                    "scheduler": dict(scheduler),
                    "coverage": dict(discovery.get("coverage", {})),
                }
        time.sleep(0.025)
    raise TimeoutError("actual_stage_deadline_checkpoint_upgrade_not_observed")


def _wait_for_cli_http_boundary(
    model: LoopbackChatFixture,
    capture: CliAttempt,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    timeout: float = 30,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = model.events()
        if predicate(events):
            return events
        if capture.process.poll() is not None:
            raise AssertionError(
                "cli_exited_before_expected_quota_http_boundary; inspect owned CLI stdout/stderr and HTTP evidence"
            )
        time.sleep(0.025)
    raise TimeoutError("expected_quota_http_boundary_not_observed")


def _save_exports(fixture: ProjectFixture, run_id: str, evidence_root: Path, label: str) -> dict[str, Any]:
    export_dir = evidence_root / "exports" / label
    export_dir.mkdir(parents=True, exist_ok=False)
    output: dict[str, Any] = {}
    for format_name in ("json", "markdown", "html"):
        exported = export_annual_report(fixture.root, run_id, format_name)
        path = export_dir / f"{format_name}{'.json' if format_name == 'json' else '.md' if format_name == 'markdown' else '.html'}"
        _write_bytes_new(path, exported.content)
        output[format_name] = {
            "filename": exported.filename,
            "media_type": exported.media_type,
            "bytes": len(exported.content),
            "sha256": hashlib.sha256(exported.content).hexdigest(),
            "snapshot": exported.snapshot,
        }
    _write_json_new(export_dir / "export-manifest.json", output)
    return output


def _classify_request(payload: object) -> tuple[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("chat_payload_invalid")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("chat_messages_invalid")
    user_message = next(
        (item for item in messages if isinstance(item, Mapping) and item.get("role") == "user"),
        None,
    )
    if not isinstance(user_message, Mapping) or not isinstance(user_message.get("content"), str):
        raise ValueError("chat_user_message_invalid")
    try:
        user_payload = json.loads(user_message["content"])
    except json.JSONDecodeError:
        user_payload = {}
    if isinstance(user_payload, Mapping) and type(user_payload.get("batch_index")) is int:
        return "annual_discovery", user_payload
    if isinstance(user_payload, Mapping) and isinstance(user_payload.get("candidate"), Mapping):
        return "annual_investigation", user_payload
    return "annual_review", user_payload


def _batch_index(payload: object) -> int | None:
    if isinstance(payload, Mapping) and type(payload.get("batch_index")) is int:
        return int(payload["batch_index"])
    return None


def _candidate_signal_id(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    candidate = payload.get("candidate")
    if isinstance(candidate, Mapping) and isinstance(candidate.get("signal_id"), str):
        return str(candidate["signal_id"])
    return None


def _finding_reply(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("discovery_request_invalid")
    fragments = payload.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        raise ValueError("discovery_fragments_missing")
    first = fragments[0]
    if not isinstance(first, list) or len(first) != 3 or not all(isinstance(item, str) for item in first[:1]):
        raise ValueError("discovery_fragment_alias_invalid")
    alias, _page, source_text = first
    if not isinstance(source_text, str) or len(source_text) < 8:
        raise ValueError("discovery_fragment_text_invalid")
    return json.dumps({
        "findings": [{
            "title": "人工构造的软件测试线索",
            "observation": "该原创文本用于验证批次内引用映射。",
            "why_it_matters": "此记录只验证引用与检查点路径。",
            "follow_up": "检查来源坐标与批次顺序。",
            "priority": "low",
            "citations": [{"fragment_alias": alias, "quote": source_text[:12]}],
        }],
        "limitations": [],
    }, ensure_ascii=False)


def _discovery_from_report(report: Mapping[str, Any]) -> Mapping[str, Any]:
    investigation = report.get("model_investigation")
    if isinstance(investigation, Mapping):
        discovery = investigation.get("discovery")
        if isinstance(discovery, Mapping):
            return discovery
    discovery = report.get("model_discovery")
    if isinstance(discovery, Mapping):
        return discovery
    raise AssertionError("strict_archive_missing_discovery")


def _planned_batches(fixture: ProjectFixture, evidence_root: Path, label: str) -> dict[str, Any]:
    base = analyze_annual_pdf(
        fixture.source_pdf,
        run_id=_new_run_id(),
        company_id=fixture.company_id,
        report_year=fixture.report_year,
        document_id=fixture.document_id,
    )
    prompt = (fixture.root / "config" / "prompts" / "annual_discovery_v3.md").read_text(encoding="utf-8")
    source_record = json.loads(fixture.source_record.read_text(encoding="utf-8"))
    context = build_annual_discovery_context(
        base.report,
        fixture.source_pdf,
        source_record,
        system_prompt=prompt,
        model_name=_MODEL_NAME,
        plan_version=COMPACT_PLAN_VERSION,
        parsed_pdf=base.parsed_pdf,
    )
    result = {
        "planned_batches": len(context.batches),
        "total_chars": context.coverage.get("total_chars"),
        "planned_chars": context.coverage.get("planned_chars"),
        "truncated": context.coverage.get("truncated"),
        "preflight": "deterministic source parsing and batch planning only; zero model calls",
    }
    _write_json_new(evidence_root / f"preflight-plan-{label}.json", result)
    return result


def _success_response(event: dict[str, Any], user_payload: object) -> MockReply:
    return LoopbackChatFixture._success_reply(event, user_payload)


def _finding_response(event: dict[str, Any], user_payload: object) -> MockReply:
    if event.get("stage") != "annual_discovery":
        return _success_response(event, user_payload)
    return MockReply(content=_finding_reply(user_payload))


def _contract_rows(discovery: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    contract = discovery.get("execution_contract")
    if not isinstance(contract, Mapping):
        raise AssertionError("v4_execution_contract_missing")
    rows = contract.get("batches")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise AssertionError("v4_batch_rows_invalid")
    return list(rows)


def _export_common_value(snapshot: object, key: str) -> object:
    if not isinstance(snapshot, Mapping):
        return None
    if key in snapshot:
        return snapshot.get(key)
    report = snapshot.get("report")
    if isinstance(report, Mapping) and key in report:
        return report.get(key)
    return None


def _assert_export_snapshots_consistent(exports: Mapping[str, Any]) -> dict[str, Any]:
    required_formats = {"json", "markdown", "html"}
    if set(exports) != required_formats:
        raise AssertionError("three_export_formats_missing")
    snapshots = {
        name: value.get("snapshot")
        for name, value in exports.items()
        if isinstance(value, Mapping)
    }
    if set(snapshots) != required_formats or any(not isinstance(value, Mapping) for value in snapshots.values()):
        raise AssertionError("export_archive_snapshot_missing")
    common: dict[str, Any] = {}
    for key in ("state", "sequence", "is_partial", "parent_run_id"):
        values = {name: snapshot.get(key) for name, snapshot in snapshots.items()}
        if len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values.values()}) != 1:
            raise AssertionError(f"export_common_snapshot_mismatch:{key}")
        common[key] = next(iter(values.values()))
    return common


def _assert_export_content_consistent(
    export_dir: Path,
    discovery: Mapping[str, Any],
) -> dict[str, Any]:
    json_export = json.loads((export_dir / "json.json").read_text(encoding="utf-8"))
    json_report = json_export.get("report")
    if not isinstance(json_report, Mapping):
        raise AssertionError("json_export_report_missing")
    json_discovery = json_report.get("model_discovery")
    if not isinstance(json_discovery, Mapping):
        raise AssertionError("json_export_discovery_missing")
    coverage = discovery.get("coverage")
    if not isinstance(coverage, Mapping):
        raise AssertionError("discovery_coverage_missing")
    json_coverage = json_discovery.get("coverage")
    if not isinstance(json_coverage, Mapping) or dict(json_coverage) != dict(coverage):
        raise AssertionError("json_export_discovery_coverage_mismatch")
    execution_contract = json_discovery.get("execution_contract")
    if not isinstance(execution_contract, Mapping):
        raise AssertionError("json_export_v4_execution_contract_missing")
    scheduler = execution_contract.get("scheduler")
    phase_budget = execution_contract.get("phase_budget")
    if not isinstance(scheduler, Mapping) or not isinstance(phase_budget, Mapping):
        raise AssertionError("json_export_v4_budget_projections_missing")

    markdown = (export_dir / "markdown.md").read_text(encoding="utf-8")
    html = (export_dir / "html.html").read_text(encoding="utf-8")
    text_page_line = f"- 覆盖：文字页 {coverage['text_pages']} / 总页 {coverage['total_pages']}；"
    raw_text_line = (
        f"原始文字块 {coverage['total_blocks']}；已处理字符 {coverage['processed_chars']} / "
        f"全部可提取字符 {coverage['total_chars']}；"
    )
    batch_line = (
        f"已结束批次 {coverage['processed_batches']} / 计划 {coverage['planned_batches']}"
        f"（成功 {coverage['successful_batches']}、部分 {coverage['partial_batches']}、"
        f"失败 {coverage['failed_batches']}）；"
    )
    complete_line = f"完整={coverage['complete']}，截断={coverage['truncated']}。"
    for value in (text_page_line, raw_text_line, batch_line, complete_line):
        if value not in markdown:
            raise AssertionError("markdown_discovery_coverage_mismatch")
    html_status_coverage_row = (
        f"<td>{discovery.get('status')}</td>"
        f"<td>{json_discovery.get('source_binding_status')}</td>"
        f"<td>{discovery.get('model_called')}</td>"
        f"<td>{coverage['complete']}</td>"
        f"<td>{coverage['text_pages']} / {coverage['total_pages']}</td>"
        f"<td>{coverage['processed_batches']} / {coverage['planned_batches']}</td>"
        f"<td>{coverage['successful_batches']} / {coverage['partial_batches']} / {coverage['failed_batches']}</td>"
        f"<td>{coverage['processed_chars']} / {coverage['total_chars']}</td>"
        f"<td>{coverage['truncated']}</td>"
    )
    if html_status_coverage_row not in html:
        raise AssertionError("html_discovery_coverage_mismatch")
    status = discovery.get("status")
    binding = json_discovery.get("source_binding_status")
    model_called = discovery.get("model_called")
    if f"- 状态：`{status}`；来源绑定：`{binding}`；模型调用：`{model_called}`" not in markdown:
        raise AssertionError("markdown_discovery_status_mismatch")
    if f"<td>{status}</td><td>{binding}</td><td>{model_called}</td>" not in html:
        raise AssertionError("html_discovery_status_mismatch")
    return {
        "json_discovery_status": json_discovery.get("status"),
        "json_coverage_matches_reader": True,
        "markdown_status_and_coverage_match_reader": True,
        "html_status_and_coverage_match_reader": True,
        "json_v4_scheduler": dict(scheduler),
        "json_v1_phase_budget": dict(phase_budget),
    }


def test_cli_healthy_slow_http_uses_three_workers_and_stable_batch_checkpoints() -> None:
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("healthy_slow_http_stable_order")
    model = LoopbackChatFixture()
    fixture = _new_project(tmp_case, pages=36, evidence_root=evidence_root, name="healthy-slow-http")
    plan = _planned_batches(fixture, evidence_root, "healthy-slow-http")
    assert plan["planned_batches"] >= 3

    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event["stage"] == "annual_discovery":
            model.wait_for_three_discovery_requests(timeout=15)
            batch = int(event["batch_index"])
            delay = {1: 0.8, 2: 0.18, 3: 0.36}.get(batch, 0.22)
            return MockReply(
                content=_finding_reply(user_payload),
                delay_seconds=delay,
                block=batch in {1, 2, 3},
            )
        return _success_response(event, user_payload)

    # Hold the first real HTTP responses open until Windows reports the actual
    # chat_worker child PIDs; this avoids racing a sub-second response window.
    model._responder = responder
    model.start()
    capture: CliAttempt | None = None
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        capture = _begin_cli(fixture, evidence_root, "healthy-slow-http", model)
        first_three = model.wait_for(
            lambda events: len([item for item in events if item["stage"] == "annual_discovery"]) >= 3,
            timeout=25,
        )
        model.wait_blocked(timeout=15)
        workers = _wait_for_workers(capture.process.pid, expected=3, timeout=12)
        worker_pids = [int(item["pid"]) for item in workers]
        worker_pids_observed_monotonic = time.monotonic()
        worker_pids_observed_at = _utc_now()
        model.release()
        returncode = _finish_cli(capture, timeout=120)
        assert returncode in {0, 2}
        assert _wait_for_worker_exit(worker_pids, timeout=10)
        worker_pids_exited_at = _utc_now()
        worker_pids_exit_elapsed_seconds = round(
            time.monotonic() - worker_pids_observed_monotonic, 6
        )
        worker_exit_observations = [
            {"pid": pid, "exit_observed": not _pid_exists(pid), "observed_at": worker_pids_exited_at}
            for pid in worker_pids
        ]
        assert all(item["exit_observed"] for item in worker_exit_observations)
        _capture_cli_evidence(capture, evidence_root, model, "healthy-slow-http")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        coverage = discovery["coverage"]
        contract = discovery["execution_contract"]
        scheduler = contract["scheduler"]
        discovery_events = [item for item in model.events("annual_discovery")]
        assert coverage["planned_batches"] == plan["planned_batches"]
        assert coverage["successful_batches"] == coverage["planned_batches"]
        assert coverage["truncated"] is False
        assert coverage["time_budget_exhausted"] is False
        assert scheduler["dispatch_closed_reason"] is None
        assert scheduler["time_deadline_reason"] is None
        assert scheduler["checkpoint_coordinator_count"] == 1
        assert scheduler["inflight_drained"] is True
        assert scheduler["inflight_count"] == 0
        assert scheduler["requested_max_concurrency"] == 3
        assert scheduler["effective_max_concurrency"] == 3
        assert scheduler["inflight_peak"] <= 3
        assert 2 <= model.actual_http_peak <= 3
        assert len(discovery_events) == coverage["planned_batches"]
        assert all(
            event.get("response_finished_monotonic", 0) - event["received_monotonic"] >= 0.1
            for event in discovery_events
        )
        first_batch_events = sorted(discovery_events, key=lambda item: item["sequence"])[:3]
        first_batch_finish_order = [
            item["batch_index"]
            for item in sorted(first_batch_events, key=lambda item: item["response_finished_monotonic"])
        ]
        assert first_batch_finish_order != sorted(first_batch_finish_order)
        findings = discovery.get("findings")
        assert isinstance(findings, list) and findings
        finding_batches = [int(item["batch_index"]) for item in findings if isinstance(item, Mapping)]
        assert finding_batches == sorted(finding_batches)
        rows = _contract_rows(discovery)
        assert [row.get("batch_index") for row in rows] == list(range(1, plan["planned_batches"] + 1))

        phase = contract["phase_budget"]
        assert phase["main_report_limit_seconds"] == 300
        assert phase["work_deadline_seconds"] == 285
        assert phase["finalization_reserve_seconds"] == 15
        assert phase["scope"] == "new_run"
        assert phase["task_provider_attempt_limit"] == 30
        assert phase["stage_provider_attempt_limits"] == {
            "annual_discovery": 18,
            "annual_investigation": 2,
            "annual_issue_grouping": 8,
            "annual_review": 2,
        }
        exports = _save_exports(fixture, capture.run_id, evidence_root, "healthy-slow-http")
        export_projection = _assert_export_snapshots_consistent(exports)
        export_content_projection = _assert_export_content_consistent(
            evidence_root / "exports" / "healthy-slow-http", discovery
        )
        review = report.get("model_review")
        assert isinstance(review, Mapping)
        review_events = model.events("annual_review")
        assert len(review_events) == 1
        assert review_events[0].get("status") == 200
        assert review.get("status") == "completed"
        assert review.get("assessment") == "insufficient_evidence"
        investigation = report.get("model_investigation")
        investigation_call_count = (
            investigation.get("investigation_call_count", 0)
            if isinstance(investigation, Mapping) else 0
        )
        assert len(model.events("annual_investigation")) == investigation_call_count == 0
        _write_json_new(evidence_root / "healthy-slow-http-result.json", {
            "case": "actual standalone CLI Popen; actual HTTP over 127.0.0.1",
            "run_id": capture.run_id,
            "returncode": returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "planned_batches": coverage["planned_batches"],
            "actual_discovery_http_requests": len(discovery_events),
            "actual_review_http_requests": len(review_events),
            "review_status": review.get("status"),
            "review_assessment": review.get("assessment"),
            "investigation_http_requests": len(model.events("annual_investigation")),
            "investigation_call_count": investigation_call_count,
            "actual_http_peak": model.actual_http_peak,
            "worker_pids": workers,
            "worker_pids_observed_at": worker_pids_observed_at,
            "worker_pids_exited_at": worker_pids_exited_at,
            "worker_pids_exit_elapsed_seconds": worker_pids_exit_elapsed_seconds,
            "worker_exit_observations": worker_exit_observations,
            "first_three_batch_finish_order": first_batch_finish_order,
            "scheduler": scheduler,
            "phase_budget": phase,
            "coverage": coverage,
            "finding_batch_order": finding_batches,
            "export_common_projection": export_projection,
            "export_content_projection": export_content_projection,
            "exports": exports,
            "external_provider_requests": 0,
        })
    finally:
        model.release()
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=5)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            # Preserve failed-attempt evidence too; only write once.
            event_path = evidence_root / "http-events-healthy-slow-http.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "healthy-slow-http")
        model.close()


def test_cli_discovery_attempt_cap_is_atomic_and_keeps_review_quota() -> None:
    """Real CLI retries stop at the standalone discovery cap without marking time exhausted."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("discovery_attempt_cap_atomic")
    model = LoopbackChatFixture()
    fixture = _new_project(tmp_case, pages=36, evidence_root=evidence_root, name="discovery-attempt-cap")
    plan = _planned_batches(fixture, evidence_root, "discovery-attempt-cap")
    assert plan["planned_batches"] >= 7
    per_batch_attempts: dict[int, int] = {}

    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event["stage"] == "annual_discovery":
            if event["batch_index"] == 1:
                return MockReply(content=_finding_reply(user_payload), delay_seconds=0.12)
            batch = int(event["batch_index"])
            per_batch_attempts[batch] = per_batch_attempts.get(batch, 0) + 1
            if per_batch_attempts[batch] == 1:
                # A failed first HTTP call followed by a successful retry keeps
                # the scheduler's transport-failure streak below its independent
                # stop threshold while consuming two real gate admissions.
                status = 429 if batch % 2 == 0 else 503
                return MockReply(
                    status=status,
                    content='{"error":"synthetic retry fixture"}',
                    block=(batch == 2),
                )
            return MockReply(content=_finding_reply(user_payload))
        return _success_response(event, user_payload)

    model._responder = responder
    model.start()
    capture: CliAttempt | None = None
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        # Serial dispatch makes the 18-admission boundary deterministic; the
        # separate healthy case already covers default three-worker dispatch.
        capture = _begin_cli(fixture, evidence_root, "discovery-attempt-cap", model, concurrency=1)
        model.wait_blocked(timeout=25)
        workers = _wait_for_workers(capture.process.pid, expected=1, timeout=12)
        worker_pids = [int(item["pid"]) for item in workers]
        worker_pids_observed_at = _utc_now()
        worker_observed_monotonic = time.monotonic()
        model.release()
        returncode = _finish_cli(capture, timeout=120)
        assert returncode in {0, 2}
        assert _wait_for_worker_exit(worker_pids, timeout=10)
        worker_pids_exited_at = _utc_now()
        worker_lifetime_after_observation = round(time.monotonic() - worker_observed_monotonic, 6)
        _capture_cli_evidence(capture, evidence_root, model, "discovery-attempt-cap")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        coverage = discovery["coverage"]
        contract = discovery["execution_contract"]
        scheduler = contract["scheduler"]
        events = model.events("annual_discovery")
        retry_events = [event for event in events if event.get("status") in {429, 503}]
        ledger_path = fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3"
        ledger_counts = read_gate_stage_admission_counts(ledger_path, capture.run_id)
        assert ledger_counts["annual_discovery"] == 18
        assert len(events) == ledger_counts["annual_discovery"]
        assert len(retry_events) == 9
        assert coverage["successful_batches"] >= 1
        assert any(item.get("batch_index") == 1 for item in discovery.get("findings", []))
        assert coverage["time_budget_exhausted"] is False
        assert scheduler["time_deadline_reason"] is None
        assert scheduler["dispatch_closed_reason"] is not None
        assert scheduler["inflight_drained"] is True
        assert scheduler["inflight_count"] == 0
        assert scheduler["checkpoint_coordinator_count"] == 1
        assert scheduler["requested_max_concurrency"] == 1
        assert scheduler["effective_max_concurrency"] == 1

        # The discovery admission cap is stage-local: review still spends its
        # own default attempt and reaches a real loopback response.
        review_events = model.events("annual_review")
        review = report.get("model_review")
        assert len(review_events) == 1
        assert review_events[0].get("status") == 200
        assert isinstance(review, Mapping) and review.get("status") == "completed"
        assert ledger_counts["annual_review"] == 1
        investigation = report.get("model_investigation")
        investigation_call_count = (
            investigation.get("investigation_call_count", 0)
            if isinstance(investigation, Mapping) else 0
        )
        assert len(model.events("annual_investigation")) == investigation_call_count

        exports = _save_exports(fixture, capture.run_id, evidence_root, "discovery-attempt-cap")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "discovery-attempt-cap", discovery
        )
        _write_json_new(evidence_root / "discovery-attempt-cap-result.json", {
            "case": "real standalone CLI Popen; atomic default stage gate; actual HTTP retries over loopback",
            "controlled_override": {"discovery_concurrency": 1, "reason": "deterministic serial boundary; healthy case separately tests concurrency three"},
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "planned_batches": coverage["planned_batches"],
            "actual_discovery_http_requests": len(events),
            "actual_retry_http_requests": len(retry_events),
            "actual_retry_statuses": {str(status): sum(item.get("status") == status for item in retry_events) for status in (429, 503)},
            "actual_review_http_requests": len(review_events),
            "review_status": review.get("status") if isinstance(review, Mapping) else None,
            "review_response_status": review_events[0].get("status"),
            "actual_investigation_http_requests": len(model.events("annual_investigation")),
            "investigation_call_count": investigation_call_count,
            "durable_stage_admission_counts": ledger_counts,
            "coverage": coverage,
            "scheduler": scheduler,
            "worker_pids": workers,
            "worker_pids_observed_at": worker_pids_observed_at,
            "worker_pids_exited_at": worker_pids_exited_at,
            "worker_lifetime_after_observation_seconds": worker_lifetime_after_observation,
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        model.release()
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=5)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-discovery-attempt-cap.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "discovery-attempt-cap")
        model.close()


def test_cli_discovery_stage_deadline_stops_worker_and_ignores_late_http_response() -> None:
    """A real blocked worker is stopped at its short test-only stage ceiling."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("discovery_stage_deadline_worker_stop")
    model = LoopbackChatFixture()
    fixture = _new_project(tmp_case, pages=36, evidence_root=evidence_root, name="discovery-stage-deadline")
    plan = _planned_batches(fixture, evidence_root, "discovery-stage-deadline")
    assert plan["planned_batches"] >= 3

    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event["stage"] == "annual_discovery":
            if event["batch_index"] == 1:
                return MockReply(content=_finding_reply(user_payload), delay_seconds=0.12)
            # Keep a genuine HTTP request open past the controlled stage deadline;
            # release its late success only after the CLI and worker have exited.
            return MockReply(content=_finding_reply(user_payload), block=True)
        return _success_response(event, user_payload)

    model._responder = responder
    model.start()
    capture: CliAttempt | None = None
    worker_pids: list[int] = []
    late_released = False
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        stage_limits = {
            "annual_discovery": 3,
            "annual_investigation": 2,
            "annual_issue_grouping": 2,
            "annual_review": 2,
        }
        # The API owner is 60 seconds in production; only the discovery stage is
        # shortened here to exercise a real bounded Popen/socket edge quickly.
        soft_deadline = time.monotonic() + 45
        capture = _begin_cli(
            fixture,
            evidence_root,
            "discovery-stage-deadline",
            model,
            concurrency=1,
            main_soft_deadline=soft_deadline,
            main_hard_deadline=soft_deadline + 15,
            main_limit_seconds=60,
            stage_time_limits=stage_limits,
        )
        batch_two_events = model.wait_for(
            lambda events: any(
                event.get("stage") == "annual_discovery" and event.get("batch_index") == 2
                for event in events
            ),
            timeout=25,
        )
        model.wait_blocked(timeout=10)
        blocked_event = next(
            event for event in batch_two_events
            if event.get("stage") == "annual_discovery" and event.get("batch_index") == 2
        )
        workers = _wait_for_workers(capture.process.pid, expected=1, timeout=12)
        worker_pids = [int(item["pid"]) for item in workers]
        worker_pids_observed_at = _utc_now()
        worker_observed_monotonic = time.monotonic()
        batch_two_checkpoint_hash_before_deadline = _hash_run_and_report(fixture, capture.run_id)

        returncode = _finish_cli(capture, timeout=25)
        assert returncode in {0, 2}
        cli_finished_monotonic = time.monotonic()
        cli_finished_at = _utc_now()
        worker_exited = _wait_for_worker_exit(worker_pids, timeout=8)
        worker_pids_exited_at = _utc_now()
        assert worker_exited is True

        terminal_hash_before_release = _hash_run_and_report(fixture, capture.run_id)
        model.release()
        late_released = True
        model.wait_for(
            lambda events: any(
                event.get("stage") == "annual_discovery"
                and event.get("batch_index") == 2
                and event.get("client_disconnected_before_response") is True
                for event in events
            ),
            timeout=10,
        )
        # Allow the fixture thread to finish writing its event before exporting
        # evidence, then prove a post-terminal late HTTP success changed nothing.
        time.sleep(0.1)
        terminal_hash_after_late_response = _hash_run_and_report(fixture, capture.run_id)
        assert terminal_hash_after_late_response == terminal_hash_before_release
        _capture_cli_evidence(capture, evidence_root, model, "discovery-stage-deadline")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        coverage = discovery["coverage"]
        contract = discovery["execution_contract"]
        scheduler = contract["scheduler"]
        events = model.events("annual_discovery")
        review_events = model.events("annual_review")
        review = report.get("model_review")
        ledger_path = fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3"
        ledger_counts = read_gate_stage_admission_counts(ledger_path, capture.run_id)
        assert coverage["time_budget_exhausted"] is True
        assert scheduler["time_deadline_reason"] == "discovery_stage_budget_exhausted"
        assert scheduler["dispatch_closed_reason"] == "discovery_stage_budget_exhausted"
        assert scheduler["inflight_drained"] is True
        assert scheduler["inflight_count"] == 0
        assert scheduler["checkpoint_coordinator_count"] == 1
        assert coverage["successful_batches"] >= 1
        assert any(item.get("batch_index") == 1 for item in discovery.get("findings", []))
        assert len(events) == 2
        assert events[1].get("status") == 200
        assert events[1].get("client_disconnected_before_response") is True
        assert ledger_counts["annual_discovery"] == 2
        assert len(review_events) == ledger_counts["annual_review"] == 1
        assert review_events[0].get("status") == 200
        assert isinstance(review, Mapping) and review.get("status") == "completed"

        exports = _save_exports(fixture, capture.run_id, evidence_root, "discovery-stage-deadline")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "discovery-stage-deadline", discovery
        )
        _write_json_new(evidence_root / "discovery-stage-deadline-result.json", {
            "case": "actual standalone CLI Popen + blocked loopback HTTP + real chat_worker stop at a stage deadline",
            "controlled_owner_limit_seconds": 60,
            "controlled_stage_time_limits_seconds": stage_limits,
            "deadline_scope": "short test-only override; not a claim about default 150-second discovery performance",
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "actual_discovery_http_requests": len(events),
            "actual_review_http_requests": len(review_events),
            "review_status": review.get("status") if isinstance(review, Mapping) else None,
            "durable_stage_admission_counts": ledger_counts,
            "coverage": coverage,
            "scheduler": scheduler,
            "blocked_batch_http_event": blocked_event,
            "worker_pids": workers,
            "worker_pids_observed_at": worker_pids_observed_at,
            "worker_pids_exited_at": worker_pids_exited_at,
            "worker_exit_observed": worker_exited,
            "worker_lifetime_after_observation_seconds": round(cli_finished_monotonic - worker_observed_monotonic, 6),
            "cli_finished_at": cli_finished_at,
            "batch_two_checkpoint_hash_before_deadline": batch_two_checkpoint_hash_before_deadline,
            "terminal_hash_before_late_release": terminal_hash_before_release,
            "terminal_hash_after_late_http_response": terminal_hash_after_late_response,
            "late_success_disconnected_after_worker_stop": True,
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        if not late_released:
            model.release()
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=5)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-discovery-stage-deadline.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "discovery-stage-deadline")
        model.close()


def test_cli_stage_quota_drains_admitted_calls_and_keeps_review_quota() -> None:
    """A stage quota closes new dispatch but drains successful admitted calls."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("stage_quota_drain_admitted_calls")
    fixture = _new_project(
        tmp_case, pages=90, evidence_root=evidence_root, name="stage-quota-drain"
    )
    plan = _planned_batches(fixture, evidence_root, "stage-quota-drain")
    assert plan["planned_batches"] >= 20
    admitted_scan_limit = 18
    admitted_batch_indexes = set(range(1, admitted_scan_limit + 1))
    held_batches = {2, 3}

    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event.get("stage") == "annual_discovery":
            if int(event.get("batch_index") or 0) in held_batches:
                return MockReply(content=_finding_reply(user_payload), block=True)
            return _finding_response(event, user_payload)
        return _success_response(event, user_payload)

    model = LoopbackChatFixture(responder).start()
    capture: CliAttempt | None = None
    worker_pids: list[int] = []
    try:
        identity = _runtime_identity(fixture, evidence_root, f"{attempt_id}-quota-drain")
        capture = _begin_cli(
            fixture,
            evidence_root,
            "stage-quota-drain",
            model,
            concurrency=3,
        )

        def quota_boundary(events: list[dict[str, Any]]) -> bool:
            discovery_events = [item for item in events if item.get("stage") == "annual_discovery"]
            by_batch = {
                int(item["batch_index"]): item
                for item in discovery_events
                if type(item.get("batch_index")) is int
            }
            return (
                len(discovery_events) == admitted_scan_limit
                and set(by_batch) == admitted_batch_indexes
                and by_batch[1].get("status") == 200
                and all(
                    by_batch[index].get("status") == 200
                    for index in admitted_batch_indexes - held_batches
                )
                and all("status" not in by_batch[index] for index in held_batches)
            )

        held_events = _wait_for_cli_http_boundary(model, capture, quota_boundary, timeout=30)
        quota_closed = _wait_for_stage_quota_close_checkpoint(
            fixture, capture, timeout=30, expected_inflight=2
        )
        worker_rows = _wait_for_workers(capture.process.pid, expected=2, timeout=15)
        worker_pids = [int(item["pid"]) for item in worker_rows]
        time.sleep(0.25)
        still_alive = [pid for pid in worker_pids if _pid_exists(pid)]
        assert len(still_alive) >= 2
        assert capture.process.poll() is None
        held_before_release = model.events()
        assert quota_closed["scheduler"]["dispatch_closed_reason"] == "discovery_stage_budget_exhausted"
        assert quota_closed["scheduler"]["time_deadline_reason"] is None
        assert quota_closed["scheduler"]["inflight_count"] == 2
        assert model.actual_http_peak == 3
        assert len([item for item in held_events if item.get("stage") == "annual_discovery"]) == admitted_scan_limit
        _write_json_new(evidence_root / "stage-quota-close-before-drain.json", {
            "quota_close_checkpoint": quota_closed,
            "held_http_events_before_release": held_before_release,
            "actual_http_peak": model.actual_http_peak,
            "worker_processes_still_alive": [item for item in worker_rows if int(item["pid"]) in still_alive],
            "cli_process_still_alive": capture.process.poll() is None,
        })

        model.release()
        returncode = _finish_cli(capture, timeout=90)
        assert returncode in {0, 2}
        assert _wait_for_worker_exit(worker_pids, timeout=12) is True
        _capture_cli_evidence(capture, evidence_root, model, "stage-quota-drain")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        coverage = discovery["coverage"]
        contract = discovery["execution_contract"]
        scheduler = contract["scheduler"]
        phase_budget = contract["phase_budget"]
        rows = _contract_rows(discovery)
        discovery_events = model.events("annual_discovery")
        review_events = model.events("annual_review")
        review = report.get("model_review")
        ledger_path = fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3"
        ledger = read_gate_stage_admission_counts(ledger_path, capture.run_id)
        event_batches = [int(item["batch_index"]) for item in discovery_events]
        row_by_batch = {int(item["batch_index"]): item for item in rows}

        assert archive.snapshot is not None and archive.snapshot.get("parent_run_id") is None
        assert identity["loaded_source_match"] is True
        assert identity["source_manifest_sha256"] == fixture.runtime_source_sha256
        assert coverage["planned_batches"] == plan["planned_batches"]
        assert coverage["successful_batches"] == admitted_scan_limit
        assert coverage["failed_batches"] == coverage["partial_batches"] == 0
        assert coverage["unattempted_batches"] == plan["planned_batches"] - admitted_scan_limit
        assert coverage["time_budget_exhausted"] is False
        assert {index for index, row in row_by_batch.items() if row.get("state") == "succeeded"} == admitted_batch_indexes
        assert all(
            row_by_batch[index].get("reason") == "discovery_stage_budget_exhausted"
            for index in range(admitted_scan_limit + 1, plan["planned_batches"] + 1)
        )
        assert sorted(event_batches) == list(range(1, admitted_scan_limit + 1))
        assert all(item.get("status") == 200 for item in discovery_events)
        assert len(discovery_events) == ledger["annual_discovery"] == admitted_scan_limit
        assert phase_budget["scope"] == "new_run"
        assert phase_budget["task_provider_attempt_limit"] == 30
        assert phase_budget["stage_provider_attempt_limits"]["annual_discovery"] == admitted_scan_limit
        assert sum(ledger.values()) <= 30
        assert scheduler["dispatch_closed_reason"] == "discovery_stage_budget_exhausted"
        assert scheduler["time_deadline_reason"] is None
        assert scheduler["inflight_drained"] is True and scheduler["inflight_count"] == 0
        assert scheduler["inflight_peak"] == scheduler["effective_max_concurrency"] == 3
        assert scheduler["requested_max_concurrency"] == 3
        assert model.actual_http_peak == 3
        assert len(review_events) == ledger["annual_review"] == 1
        assert review_events[0].get("status") == 200
        assert isinstance(review, Mapping) and review.get("status") == "completed"

        exports = _save_exports(fixture, capture.run_id, evidence_root, "stage-quota-drain")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "stage-quota-drain", discovery
        )
        _write_json_new(evidence_root / "stage-quota-drain-result.json", {
            "case": "actual standalone CLI; stage quota rejection with two held admitted requests; drain then independent review",
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "standard_new_run_discovery_attempt_limit": admitted_scan_limit,
            "stage_attempt_limit_override_injected_by_harness": False,
            "phase_budget": phase_budget,
            "controlled_concurrency": 3,
            "planned_batches": plan["planned_batches"],
            "held_batch_indexes": sorted(held_batches),
            "quota_close_checkpoint": quota_closed,
            "source_pdf_sha256": fixture.source_sha256,
            "source_record_sha256": _sha256(fixture.source_record),
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "actual_discovery_http_batch_indexes": event_batches,
            "actual_discovery_http_requests": len(discovery_events),
            "actual_review_http_requests": len(review_events),
            "durable_stage_admission_counts": ledger,
            "coverage": coverage,
            "scheduler": scheduler,
            "worker_pids": worker_rows,
            "worker_exit_observed": True,
            "actual_http_peak": model.actual_http_peak,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        model.release()
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=10)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-stage-quota-drain.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "stage-quota-drain")
        model.close()


def test_cli_stage_quota_drains_until_stage_deadline_then_freezes() -> None:
    """Quota closure drains held calls until a real stage deadline aborts them."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("stage_quota_deadline_cancels_inflight")
    fixture = _new_project(
        tmp_case, pages=90, evidence_root=evidence_root, name="stage-quota-deadline"
    )
    plan = _planned_batches(fixture, evidence_root, "stage-quota-deadline")
    assert plan["planned_batches"] >= 20
    admitted_scan_limit = 18
    admitted_batch_indexes = set(range(1, admitted_scan_limit + 1))
    held_batches = {2, 3}
    expected_completed_batches = admitted_batch_indexes - held_batches

    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event.get("stage") == "annual_discovery":
            if int(event.get("batch_index") or 0) in held_batches:
                return MockReply(content=_finding_reply(user_payload), block=True)
            return _finding_response(event, user_payload)
        return _success_response(event, user_payload)

    model = LoopbackChatFixture(responder).start()
    capture: CliAttempt | None = None
    worker_pids: list[int] = []
    late_released = False
    quota_closed_at: float | None = None
    try:
        identity = _runtime_identity(fixture, evidence_root, f"{attempt_id}-quota-deadline")
        stage_limit_seconds = 20
        stage_time_limits = {
            "annual_discovery": stage_limit_seconds,
            "annual_investigation": 12,
            "annual_issue_grouping": 8,
            "annual_review": 5,
        }
        main_soft_deadline = time.monotonic() + 45
        main_hard_deadline = main_soft_deadline + 15
        capture = _begin_cli(
            fixture,
            evidence_root,
            "stage-quota-deadline",
            model,
            concurrency=3,
            main_soft_deadline=main_soft_deadline,
            main_hard_deadline=main_hard_deadline,
            main_limit_seconds=60,
            stage_time_limits=stage_time_limits,
        )
        cli_command_evidence = json.loads(
            (capture.attempt_dir / "command.json").read_text(encoding="utf-8")
        )
        cli_budget_environment = cli_command_evidence["budget_environment_overrides"]
        assert cli_budget_environment["main_limit_seconds"] == 60
        assert cli_budget_environment["stage_time_limits_seconds"] == stage_time_limits
        assert cli_budget_environment["stage_attempt_limits_injected_by_harness"] is False
        assert cli_budget_environment["task_attempt_limit_injected_by_harness"] is False
        hard_remaining_at_popen = main_hard_deadline - capture.started_monotonic
        soft_remaining_at_popen = main_soft_deadline - capture.started_monotonic
        assert 0 < hard_remaining_at_popen <= 60
        assert 0 < soft_remaining_at_popen <= 45
        assert abs(hard_remaining_at_popen - soft_remaining_at_popen - 15) < 1e-6
        owner_budget_observed_monotonic = time.monotonic()
        hard_remaining_at_observation = main_hard_deadline - owner_budget_observed_monotonic
        soft_remaining_at_observation = main_soft_deadline - owner_budget_observed_monotonic
        assert 0 < hard_remaining_at_observation <= 60
        assert 0 < soft_remaining_at_observation <= 45
        assert abs(hard_remaining_at_observation - soft_remaining_at_observation - 15) < 1e-6
        assert sum(stage_time_limits.values()) == 45

        def quota_boundary(events: list[dict[str, Any]]) -> bool:
            discovery_events = [item for item in events if item.get("stage") == "annual_discovery"]
            by_batch = {
                int(item["batch_index"]): item
                for item in discovery_events
                if type(item.get("batch_index")) is int
            }
            return (
                len(discovery_events) == admitted_scan_limit
                and set(by_batch) == admitted_batch_indexes
                and by_batch[1].get("status") == 200
                and all(
                    by_batch[index].get("status") == 200
                    for index in admitted_batch_indexes - held_batches
                )
                and all("status" not in by_batch[index] for index in held_batches)
            )

        _wait_for_cli_http_boundary(model, capture, quota_boundary, timeout=35)
        quota_closed = _wait_for_stage_quota_close_checkpoint(
            fixture, capture, timeout=30, expected_inflight=2
        )
        quota_closed_at = time.monotonic()
        worker_rows = _wait_for_workers(capture.process.pid, expected=2, timeout=15)
        time.sleep(0.3)
        worker_rows_before_deadline = _process_children(capture.process.pid)
        worker_pids = [int(item["pid"]) for item in worker_rows_before_deadline]
        assert len(worker_pids) >= 2
        assert sum(_pid_exists(pid) for pid in worker_pids) >= 2
        assert capture.process.poll() is None
        held_before_deadline = model.events()
        assert all(
            "status" not in event
            for event in held_before_deadline
            if event.get("stage") == "annual_discovery" and event.get("batch_index") in held_batches
        )
        _write_json_new(evidence_root / "stage-quota-held-until-deadline.json", {
            "quota_close_checkpoint": quota_closed,
            "quota_close_observed_monotonic": quota_closed_at,
            "controlled_stage_deadline_seconds": stage_limit_seconds,
            "main_owner_limit_seconds": 60,
            "main_work_soft_seconds": 45,
            "workers_still_alive_before_deadline": worker_rows_before_deadline,
            "held_http_events_before_deadline": held_before_deadline,
            "actual_http_peak": model.actual_http_peak,
        })

        deadline_checkpoint = _wait_for_stage_deadline_checkpoint(
            fixture,
            capture,
            after_sequence=int(quota_closed["sequence"]),
            expected_reason="discovery_stage_budget_exhausted",
            timeout=35,
        )
        quota_close_to_deadline_checkpoint_seconds = round(
            deadline_checkpoint["observed_monotonic"] - quota_closed_at, 6
        )
        assert quota_close_to_deadline_checkpoint_seconds > 0
        _write_json_new(evidence_root / "stage-quota-deadline-checkpoint.json", {
            "quota_close_checkpoint": quota_closed,
            "deadline_upgrade_checkpoint": deadline_checkpoint,
            "quota_close_to_deadline_checkpoint_seconds": quota_close_to_deadline_checkpoint_seconds,
            "late_requests_still_held": [
                event for event in model.events()
                if event.get("stage") == "annual_discovery" and event.get("batch_index") in held_batches
            ],
        })

        returncode = _finish_cli(capture, timeout=30)
        cli_finished_monotonic = time.monotonic()
        cli_finished_at = _utc_now()
        assert returncode in {0, 2}
        assert quota_closed_at is not None
        drain_until_deadline_seconds = round(cli_finished_monotonic - quota_closed_at, 6)
        assert drain_until_deadline_seconds > 0
        worker_exit_observed = _wait_for_worker_exit(worker_pids, timeout=12)
        assert worker_exit_observed is True
        terminal_hash_before_late_response = _hash_run_and_report(fixture, capture.run_id)
        model.release()
        late_released = True
        model.wait_for(
            lambda events: all(
                event.get("client_disconnected_before_response") is True
                for event in events
                if event.get("stage") == "annual_discovery"
                and event.get("batch_index") in held_batches
            ),
            timeout=12,
        )
        time.sleep(0.1)
        terminal_hash_after_late_response = _hash_run_and_report(fixture, capture.run_id)
        _write_json_new(evidence_root / "late-response-terminal-hash-comparison.json", {
            "terminal_hash_before_late_response": terminal_hash_before_late_response,
            "terminal_hash_after_late_response": terminal_hash_after_late_response,
            "unchanged": terminal_hash_after_late_response == terminal_hash_before_late_response,
            "late_response_events": [
                event for event in model.events()
                if event.get("stage") == "annual_discovery"
                and event.get("batch_index") in held_batches
            ],
        })
        assert terminal_hash_after_late_response == terminal_hash_before_late_response
        _capture_cli_evidence(capture, evidence_root, model, "stage-quota-deadline")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        coverage = discovery["coverage"]
        scheduler = discovery["execution_contract"]["scheduler"]
        phase_budget = discovery["execution_contract"]["phase_budget"]
        rows = _contract_rows(discovery)
        discovery_events = model.events("annual_discovery")
        review_events = model.events("annual_review")
        review = report.get("model_review")
        ledger_path = fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3"
        ledger = read_gate_stage_admission_counts(ledger_path, capture.run_id)
        exports = _save_exports(fixture, capture.run_id, evidence_root, "stage-quota-deadline")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "stage-quota-deadline", discovery
        )
        event_batches = [int(item["batch_index"]) for item in discovery_events]
        row_by_batch = {int(item["batch_index"]): item for item in rows}
        blocked_events = [
            event for event in discovery_events
            if event.get("batch_index") in held_batches
        ]

        assert archive.snapshot is not None and archive.snapshot.get("parent_run_id") is None
        assert identity["loaded_source_match"] is True
        assert identity["source_manifest_sha256"] == fixture.runtime_source_sha256
        assert coverage["planned_batches"] == plan["planned_batches"]
        assert coverage["time_budget_exhausted"] is True
        assert coverage["successful_batches"] == len(expected_completed_batches)
        assert {index for index, row in row_by_batch.items() if row.get("state") == "succeeded"} == expected_completed_batches
        assert coverage["unattempted_batches"] == plan["planned_batches"] - admitted_scan_limit
        assert all(
            event.get("client_disconnected_before_response") is True
            and "response_finished_monotonic" not in event
            for event in blocked_events
        )
        assert len(blocked_events) == 2
        assert sorted(event_batches) == list(range(1, admitted_scan_limit + 1))
        assert len(discovery_events) == ledger["annual_discovery"] == admitted_scan_limit
        assert scheduler["time_deadline_reason"] == "discovery_stage_budget_exhausted"
        assert scheduler["dispatch_closed_reason"] == "discovery_stage_budget_exhausted"
        assert deadline_checkpoint["sequence"] > quota_closed["sequence"]
        assert deadline_checkpoint["scheduler"]["time_deadline_reason"] == "discovery_stage_budget_exhausted"
        assert deadline_checkpoint["scheduler"]["dispatch_closed_reason"] == "discovery_stage_budget_exhausted"
        assert scheduler["inflight_drained"] is True and scheduler["inflight_count"] == 0
        assert scheduler["requested_max_concurrency"] == scheduler["effective_max_concurrency"] == 3
        assert model.actual_http_peak == 3
        assert phase_budget["scope"] == "new_run"
        # The archive retains its immutable V1 defaults. The actual short
        # deadlines are evidenced by the Popen environment and monotonic clock.
        assert phase_budget["main_report_limit_seconds"] == 300
        assert phase_budget["work_deadline_seconds"] == 285
        assert phase_budget["finalization_reserve_seconds"] == 15
        assert phase_budget["stage_time_limits_seconds"] == {
            "annual_discovery": 150,
            "annual_investigation": 60,
            "annual_issue_grouping": 35,
            "annual_review": 25,
        }
        assert phase_budget["task_provider_attempt_limit"] == 30
        assert phase_budget["stage_provider_attempt_limits"]["annual_discovery"] == admitted_scan_limit
        assert sum(ledger.values()) <= 30
        assert len(review_events) == ledger["annual_review"] == 1
        assert review_events[0].get("status") == 200
        assert isinstance(review, Mapping) and review.get("status") == "completed"

        _write_json_new(evidence_root / "stage-quota-deadline-result.json", {
            "case": "actual standalone CLI; stage quota closes dispatch, held admitted calls drain until stage deadline cancels them",
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "standard_new_run_discovery_attempt_limit": admitted_scan_limit,
            "stage_attempt_limit_override_injected_by_harness": False,
            "cli_budget_environment_overrides": cli_budget_environment,
            "archive_phase_budget_is_immutable_v1_baseline": phase_budget,
            "phase_budget": phase_budget,
            "controlled_stage_time_limits_seconds": stage_time_limits,
            "controlled_owner_main_limit_seconds": 60,
            "controlled_work_soft_deadline_seconds": 45,
            "owner_budget_observation_monotonic": owner_budget_observed_monotonic,
            "owner_soft_remaining_at_popen_seconds": soft_remaining_at_popen,
            "owner_hard_remaining_at_popen_seconds": hard_remaining_at_popen,
            "owner_soft_remaining_at_observation_seconds": soft_remaining_at_observation,
            "owner_hard_remaining_at_observation_seconds": hard_remaining_at_observation,
            "quota_close_checkpoint": quota_closed,
            "deadline_upgrade_checkpoint": deadline_checkpoint,
            "quota_close_to_deadline_checkpoint_seconds": quota_close_to_deadline_checkpoint_seconds,
            "quota_close_to_cli_exit_seconds": drain_until_deadline_seconds,
            "actual_cli_elapsed_seconds": round(cli_finished_monotonic - capture.started_monotonic, 6),
            "cli_finished_at": cli_finished_at,
            "source_pdf_sha256": fixture.source_sha256,
            "source_record_sha256": _sha256(fixture.source_record),
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "actual_discovery_http_batch_indexes": event_batches,
            "actual_discovery_http_requests": len(discovery_events),
            "actual_review_http_requests": len(review_events),
            "durable_stage_admission_counts": ledger,
            "coverage": coverage,
            "scheduler": scheduler,
            "blocked_http_events": blocked_events,
            "worker_pids": worker_rows_before_deadline,
            "worker_exit_observed": worker_exit_observed,
            "terminal_hash_before_late_response": terminal_hash_before_late_response,
            "terminal_hash_after_late_response": terminal_hash_after_late_response,
            "late_response_terminal_hash_evidence": str(evidence_root / "late-response-terminal-hash-comparison.json"),
            "actual_http_peak": model.actual_http_peak,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        if not late_released:
            model.release()
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=10)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-stage-quota-deadline.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "stage-quota-deadline")
        model.close()


def test_cli_synthetic_derivative_uses_independent_investigation_and_review_caps() -> None:
    """A controlled synthetic derivative reaches real fixed-investigation and review calls."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("original_sample_stage_caps")
    fixture = _new_project_from_annual_sample(
        tmp_case,
        evidence_root=evidence_root,
        name="original-annual-sample-stage-caps",
    )
    controlled_input = json.loads((evidence_root / "controlled-input.json").read_text(encoding="utf-8"))
    preflight = analyze_annual_pdf(
        fixture.source_pdf,
        run_id=_new_run_id(),
        company_id=fixture.company_id,
        report_year=fixture.report_year,
        document_id=fixture.document_id,
    )
    candidate_rows = [
        {"signal_id": item.get("signal_id"), "status": item.get("status")}
        for item in preflight.screening
        if item.get("status") == "candidate"
    ]
    assert candidate_rows, "synthetic_derivative_must_yield_a_source_verified_candidate"
    _write_json_new(evidence_root / "deterministic-candidate-preflight.json", {
        "preflight": "deterministic extraction, independent source verification, calculation and candidate screening; zero model calls; base is registered synthetic sample and appended narrative is controlled test-authored synthetic text",
        "source_pdf_sha256": fixture.source_sha256,
        "comparability_status": preflight.comparability.status,
        "candidate_signal_ids": [item["signal_id"] for item in candidate_rows],
        "candidate_count": len(candidate_rows),
        "source_manifest_sha256": fixture.runtime_source_sha256,
        "model_calls": 0,
    })
    plan = _planned_batches(fixture, evidence_root, "original-annual-sample-stage-caps")
    model = LoopbackChatFixture()
    model.start()
    capture: CliAttempt | None = None
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        # No gate or attempt limit is injected: this Popen must enforce its own
        # standalone new-run defaults while all HTTP stays on loopback.
        capture = _begin_cli(fixture, evidence_root, "original-sample-stage-caps", model)
        returncode = _finish_cli(capture, timeout=120)
        assert returncode in {0, 2}
        _capture_cli_evidence(capture, evidence_root, model, "original-sample-stage-caps")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        investigation = report.get("model_investigation")
        review = report.get("model_review")
        assert isinstance(investigation, Mapping)
        assert isinstance(review, Mapping)
        discovery_events = model.events("annual_discovery")
        investigation_events = model.events("annual_investigation")
        review_events = model.events("annual_review")
        ledger_path = fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3"
        ledger_counts = read_gate_stage_admission_counts(ledger_path, capture.run_id)
        contract = discovery["execution_contract"]
        phase = contract["phase_budget"]
        assert phase["scope"] == "new_run"
        assert phase["task_provider_attempt_limit"] == 30
        assert phase["stage_provider_attempt_limits"] == {
            "annual_discovery": 18,
            "annual_investigation": 2,
            "annual_issue_grouping": 8,
            "annual_review": 2,
        }
        assert len(discovery_events) == ledger_counts["annual_discovery"] <= 18
        assert len(investigation_events) == ledger_counts["annual_investigation"]
        assert 1 <= ledger_counts["annual_investigation"] <= 2
        assert investigation.get("investigation_call_count") == len(investigation_events)
        assert review.get("call_count") == len(review_events) == ledger_counts["annual_review"] == 1
        assert review_events[0].get("status") == 200
        assert review.get("status") == "completed"
        assert investigation.get("status") in {"completed", "abstained"}
        assert investigation.get("reason") != "no_supported_candidate_signal"
        assert any(
            item.get("signal_id") in {row["signal_id"] for row in candidate_rows}
            and item.get("status") == "candidate"
            for item in report.get("pending_review", {}).get("candidate_signals", [])
        )
        assert report.get("source_provenance", {}).get("status") == "unconfirmed"
        assert report.get("source_provenance", {}).get("data_nature") == "unconfirmed"
        exports = _save_exports(fixture, capture.run_id, evidence_root, "original-sample-stage-caps")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "original-sample-stage-caps", discovery
        )
        _write_json_new(evidence_root / "original-sample-stage-caps-result.json", {
            "case": "real standalone CLI Popen with controlled synthetic derivative of registered annual sample and mocked loopback provider",
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "registered_base_pdf_sha256": controlled_input["base_sample_sha256"],
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "planned_batches": plan["planned_batches"],
            "actual_discovery_http_requests": len(discovery_events),
            "actual_investigation_http_requests": len(investigation_events),
            "investigation_status": investigation.get("status"),
            "investigation_reason": investigation.get("reason"),
            "investigation_response_count": investigation.get("investigation_call_count"),
            "candidate_signal_ids_from_base_financial_text_and_deterministic_calculations": [row["signal_id"] for row in candidate_rows],
            "actual_review_http_requests": len(review_events),
            "review_status": review.get("status"),
            "review_response_count": review.get("call_count"),
            "durable_stage_admission_counts": ledger_counts,
            "archived_phase_budget": phase,
            "source_provenance": report.get("source_provenance"),
            "coverage": discovery.get("coverage"),
            "scheduler": contract.get("scheduler"),
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=5)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-original-sample-stage-caps.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "original-sample-stage-caps")
        model.close()


def test_cli_investigation_retry_consumes_own_stage_cap_without_closing_review() -> None:
    """A real investigation retry consumes the second stage slot; review remains independent."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("investigation_retry_stage_cap")
    fixture = _new_project_from_annual_sample(
        tmp_case,
        evidence_root=evidence_root,
        name="investigation-retry-stage-cap",
    )
    controlled_input = json.loads((evidence_root / "controlled-input.json").read_text(encoding="utf-8"))
    preflight = analyze_annual_pdf(
        fixture.source_pdf,
        run_id=_new_run_id(),
        company_id=fixture.company_id,
        report_year=fixture.report_year,
        document_id=fixture.document_id,
    )
    candidate_ids = [
        str(item.get("signal_id"))
        for item in preflight.screening
        if item.get("status") == "candidate"
    ]
    assert candidate_ids == ["profit_up_cash_down", "revenue_up_cash_down"]
    model = LoopbackChatFixture()
    investigation_attempts_by_signal: dict[str, int] = {}

    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event["stage"] == "annual_investigation":
            signal_id = str(event.get("candidate_signal_id"))
            count = investigation_attempts_by_signal.get(signal_id, 0) + 1
            investigation_attempts_by_signal[signal_id] = count
            if sum(investigation_attempts_by_signal.values()) == 1:
                return MockReply(status=429, content="{}")
        return _success_response(event, user_payload)

    model._responder = responder
    model.start()
    capture: CliAttempt | None = None
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        capture = _begin_cli(fixture, evidence_root, "investigation-retry-stage-cap", model)
        returncode = _finish_cli(capture, timeout=120)
        assert returncode in {0, 2}
        _capture_cli_evidence(capture, evidence_root, model, "investigation-retry-stage-cap")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        contract = discovery["execution_contract"]
        phase = contract["phase_budget"]
        investigation = report.get("model_investigation")
        review = report.get("model_review")
        assert isinstance(investigation, Mapping)
        assert isinstance(review, Mapping)
        discovery_events = model.events("annual_discovery")
        investigation_events = model.events("annual_investigation")
        review_events = model.events("annual_review")
        ledger_counts = read_gate_stage_admission_counts(
            fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3",
            capture.run_id,
        )
        assert phase["scope"] == "new_run"
        assert phase["task_provider_attempt_limit"] == 30
        assert phase["stage_provider_attempt_limits"]["annual_investigation"] == 2
        assert len(discovery_events) == ledger_counts["annual_discovery"] == 1
        assert len(investigation_events) == ledger_counts["annual_investigation"] == 2
        assert [event.get("status") for event in investigation_events] == [429, 200]
        assert len({event.get("candidate_signal_id") for event in investigation_events}) == 1
        assert investigation_events[0].get("candidate_signal_id") == "profit_up_cash_down"
        assert investigation.get("investigation_call_attempt_count") == 2
        assert investigation.get("investigation_call_count") == 1
        assert len(review_events) == ledger_counts["annual_review"] == 1
        assert review_events[0].get("status") == 200
        assert review.get("status") == "completed"
        assert discovery["coverage"]["time_budget_exhausted"] is False
        assert contract["scheduler"]["time_deadline_reason"] is None
        assert contract["scheduler"]["dispatch_closed_reason"] is None
        assert report.get("source_provenance", {}).get("status") == "unconfirmed"
        exports = _save_exports(fixture, capture.run_id, evidence_root, "investigation-retry-stage-cap")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "investigation-retry-stage-cap", discovery
        )
        _write_json_new(evidence_root / "investigation-retry-stage-cap-result.json", {
            "case": "real standalone CLI Popen; synthetic derivative; loopback investigation 429 retry consumes two admissions before review",
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "registered_base_pdf_sha256": controlled_input["base_sample_sha256"],
            "runtime_source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "candidate_signal_ids_from_deterministic_preflight": candidate_ids,
            "actual_discovery_http_requests": len(discovery_events),
            "investigation_http_events": investigation_events,
            "actual_investigation_error_responses": sum(
                1 for event in investigation_events if event.get("status") in {429, 503}
            ),
            "actual_investigation_successful_retry_http_requests": sum(
                1 for event in investigation_events if event.get("status") == 200
            ),
            "investigation_status": investigation.get("status"),
            "investigation_reason": investigation.get("reason"),
            "investigation_response_count": investigation.get("investigation_call_count"),
            "investigation_attempt_count": investigation.get("investigation_call_attempt_count"),
            "actual_review_http_requests": len(review_events),
            "review_status": review.get("status"),
            "review_response_count": review.get("call_count"),
            "durable_stage_admission_counts": ledger_counts,
            "archived_phase_budget": phase,
            "coverage": discovery["coverage"],
            "scheduler": contract["scheduler"],
            "source_provenance": report.get("source_provenance"),
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=5)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-investigation-retry-stage-cap.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "investigation-retry-stage-cap")
        model.close()


def test_cli_auth_failure_drains_three_inflight_workers_without_retry_or_late_publish() -> None:
    """A real auth failure closes dispatch and drains two hanging HTTP workers promptly."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("auth_failure_three_inflight_drain")
    model = LoopbackChatFixture()
    fixture = _new_project(tmp_case, pages=36, evidence_root=evidence_root, name="auth-three-inflight")
    plan = _planned_batches(fixture, evidence_root, "auth-three-inflight")
    assert plan["planned_batches"] >= 4

    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event["stage"] == "annual_discovery":
            model.wait_for_three_discovery_requests(timeout=15)
            if event.get("batch_index") == 1:
                # The short delay is a test barrier so the actual child PIDs
                # are recorded while all three provider calls are still open.
                return MockReply(status=401, content='{"error":"synthetic unauthorized"}', delay_seconds=2.5)
            return MockReply(content=_finding_reply(user_payload), block=True)
        return _success_response(event, user_payload)

    model._responder = responder
    model.start()
    capture: CliAttempt | None = None
    workers: list[dict[str, Any]] = []
    late_responses_released = False
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        capture = _begin_cli(fixture, evidence_root, "auth-three-inflight", model)
        initial_events = model.wait_for(
            lambda events: len([
                item for item in events
                if item.get("stage") == "annual_discovery"
            ]) >= 3,
            timeout=20,
        )
        model.wait_blocked(timeout=10)
        workers = _wait_for_workers(capture.process.pid, expected=3, timeout=12)
        worker_pids = [int(item["pid"]) for item in workers]
        workers_observed_at = _utc_now()
        workers_observed_monotonic = time.monotonic()
        assert not any(
            item.get("stage") == "annual_discovery" and item.get("status") == 401
            for item in model.events()
        )
        three_initial_events = [
            item for item in initial_events
            if item.get("stage") == "annual_discovery"
        ][:3]
        assert sorted(item.get("batch_index") for item in three_initial_events) == [1, 2, 3]
        assert model.actual_http_peak == 3
        model.wait_for(
            lambda events: any(
                item.get("stage") == "annual_discovery"
                and item.get("batch_index") == 1
                and item.get("status") == 401
                for item in events
            ),
            timeout=10,
        )
        auth_finished_monotonic = next(
            item["response_finished_monotonic"]
            for item in model.events("annual_discovery")
            if item.get("batch_index") == 1 and item.get("status") == 401
        )
        blocked_before_stop = [
            item for item in model.events("annual_discovery")
            if item.get("batch_index") in {2, 3}
        ]
        assert len(blocked_before_stop) == 2
        assert all("status" not in item for item in blocked_before_stop)

        returncode = _finish_cli(capture, timeout=18)
        cli_finished_monotonic = time.monotonic()
        cli_finished_at = _utc_now()
        worker_exit_observed = _wait_for_worker_exit(worker_pids, timeout=10)
        worker_pids_exited_at = _utc_now()
        assert returncode == 2
        assert worker_exit_observed is True
        assert cli_finished_monotonic - auth_finished_monotonic < 18

        terminal_hash_before_late_response = _hash_run_and_report(fixture, capture.run_id)
        model.release()
        late_responses_released = True
        model.wait_for(
            lambda events: all(
                any(
                    item.get("stage") == "annual_discovery"
                    and item.get("batch_index") == batch
                    and item.get("client_disconnected_before_response") is True
                    for item in events
                )
                for batch in (2, 3)
            ),
            timeout=12,
        )
        time.sleep(0.1)
        terminal_hash_after_late_response = _hash_run_and_report(fixture, capture.run_id)
        assert terminal_hash_after_late_response == terminal_hash_before_late_response
        assert _wait_for_worker_exit(worker_pids, timeout=1) is True
        _capture_cli_evidence(capture, evidence_root, model, "auth-three-inflight")

        archive = _read_report(fixture, capture.run_id)
        report = archive.report
        discovery = _discovery_from_report(report)
        contract = discovery["execution_contract"]
        scheduler = contract["scheduler"]
        coverage = discovery["coverage"]
        events = model.events("annual_discovery")
        ledger_counts = read_gate_stage_admission_counts(
            fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3",
            capture.run_id,
        )
        assert len(events) == 3
        assert {item.get("batch_index") for item in events} == {1, 2, 3}
        assert sum(item.get("status") == 401 for item in events) == 1
        assert sum(item.get("client_disconnected_before_response") is True for item in events) == 2
        assert ledger_counts["annual_discovery"] == len(events) == 3
        assert ledger_counts["annual_investigation"] == 0
        assert ledger_counts["annual_review"] == 0
        assert scheduler["requested_max_concurrency"] == 3
        assert scheduler["effective_max_concurrency"] == 3
        assert scheduler["inflight_peak"] == 3
        assert scheduler["checkpoint_coordinator_count"] == 1
        assert scheduler["dispatch_closed_reason"] == "authentication_failure_stopped"
        assert scheduler["time_deadline_reason"] is None
        assert scheduler["inflight_drained"] is True and scheduler["inflight_count"] == 0
        assert coverage["time_budget_exhausted"] is False
        assert coverage["unattempted_batches"] == plan["planned_batches"] - 3
        assert len(model.events("annual_review")) == 0
        exports = _save_exports(fixture, capture.run_id, evidence_root, "auth-three-inflight")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "auth-three-inflight", discovery
        )
        _write_json_new(evidence_root / "auth-three-inflight-result.json", {
            "case": "actual standalone CLI Popen with one loopback HTTP 401 and two blocked loopback HTTP requests; real worker drain and late-response no-publish check",
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "runtime_source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "planned_batches": plan["planned_batches"],
            "actual_discovery_http_requests": len(events),
            "actual_http_peak": model.actual_http_peak,
            "error_http_responses": sum(item.get("status") == 401 for item in events),
            "controlled_auth_response_observation_barrier_seconds": 2.5,
            "retry_http_requests": max(0, len(events) - len({item.get("batch_index") for item in events})),
            "review_http_requests": len(model.events("annual_review")),
            "durable_stage_admission_counts": ledger_counts,
            "coverage": coverage,
            "scheduler": scheduler,
            "first_auth_response_finished_monotonic": auth_finished_monotonic,
            "cli_finished_at": cli_finished_at,
            "auth_to_cli_exit_seconds": round(cli_finished_monotonic - auth_finished_monotonic, 6),
            "worker_pids": workers,
            "worker_pids_observed_at": workers_observed_at,
            "worker_pids_exited_at": worker_pids_exited_at,
            "worker_exit_observed": worker_exit_observed,
            "worker_lifetime_after_observation_seconds": round(cli_finished_monotonic - workers_observed_monotonic, 6),
            "terminal_hash_before_late_response": terminal_hash_before_late_response,
            "terminal_hash_after_late_response": terminal_hash_after_late_response,
            "late_requests_disconnected": True,
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        if not late_responses_released:
            model.release()
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=5)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-auth-three-inflight.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "auth-three-inflight")
        model.close()


def _hash_run_and_report(fixture: ProjectFixture, run_id: str) -> dict[str, str]:
    base = fixture.root / "artifacts"
    paths = {
        "run_archive": base / "runs" / run_id,
        "report_archive": base / "reports" / run_id,
    }
    result: dict[str, str] = {}
    for key, path in paths.items():
        if not path.is_dir():
            continue
        digest = hashlib.sha256()
        for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
            if item.is_file() and not item.is_symlink():
                digest.update(item.relative_to(path).as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(_sha256(item).encode("ascii"))
                digest.update(b"\n")
        result[key] = digest.hexdigest()
    return result


def _wait_until(predicate: Callable[[], Any], *, timeout: float = 20, interval: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise TimeoutError("owned_runtime_condition_not_observed")


def _chat_response(content: str, sequence: int) -> bytes:
    return json.dumps({
        "id": f"chatcmpl-r3-loopback-{sequence:05d}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _MODEL_NAME,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {},
    }, ensure_ascii=False).encode("utf-8")


def _new_run_id() -> str:
    return f"annual-analysis-{uuid.uuid4()}"


def _new_job_id() -> str:
    return f"annual-job-{uuid.uuid4().hex}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _write_bytes_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _process_children(parent_pid: int) -> list[dict[str, Any]]:
    if os.name != "nt":
        pytest.fail("R3 runtime PID acceptance requires Windows child-process observation")
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ParentProcessId = {int(parent_pid)}'; "
        "$p | Where-Object { $_.CommandLine -match 'finagent\\.llm\\.chat_worker' } | "
        "Select-Object @{n='pid';e={$_.ProcessId}},@{n='parent_pid';e={$_.ParentProcessId}},CreationDate | "
        "ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError("unable_to_observe_owned_chat_worker_pids")
    raw = completed.stdout.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    values = parsed if isinstance(parsed, list) else [parsed]
    return [value for value in values if isinstance(value, Mapping)]


def _wait_for_workers(parent_pid: int, *, expected: int = 1, timeout: float = 15) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        workers = _process_children(parent_pid)
        if len(workers) >= expected:
            return workers
        time.sleep(0.05)
    raise TimeoutError("owned_chat_worker_pid_not_observed")


def _wait_for_worker_exit(pids: list[int], *, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_pid_exists(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return not any(_pid_exists(pid) for pid in pids)


def _pid_exists(pid: int) -> bool:
    completed = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            f"if (Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}') {{ exit 0 }} else {{ exit 1 }}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return completed.returncode == 0


def _process_creation_metadata(pid: int) -> dict[str, Any]:
    if os.name != "nt":
        pytest.fail("R3 runtime process creation evidence requires Windows process observation")
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
        "if ($p) { $p | Select-Object @{n='pid';e={$_.ProcessId}}, "
        "@{n='parent_pid';e={$_.ParentProcessId}}, CreationDate | ConvertTo-Json -Compress }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=10,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("unable_to_capture_owned_process_creation_metadata")
    value = json.loads(completed.stdout.strip())
    if not isinstance(value, Mapping) or int(value.get("pid", -1)) != int(pid):
        raise RuntimeError("owned_process_creation_metadata_identity_mismatch")
    return dict(value)


def _api_request(base_url: str, method: str, path: str, *, body: bytes | None = None,
                 headers: Mapping[str, str] | None = None, timeout: float = 15) -> tuple[int, bytes, Mapping[str, str]]:
    request = urllib.request.Request(base_url.rstrip("/") + path, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers.items())


def _wait_api_ready(base_url: str, process: subprocess.Popen[bytes], timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("owned_api_process_exited_before_ready")
        try:
            status, _body, _headers = _api_request(base_url, "GET", "/v1/runtime-identity", timeout=2)
            if status == 200:
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(0.1)
    raise TimeoutError("owned_api_process_not_ready")


def _multipart_pdf(company_id: str, report_year: int, pdf: bytes) -> tuple[str, bytes]:
    boundary = f"----FintraceR3{uuid.uuid4().hex}"
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"company_id\"\r\n\r\n{company_id}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"report_year\"\r\n\r\n{report_year}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"controlled-r3.pdf\"\r\nContent-Type: application/pdf\r\n\r\n".encode(),
        pdf,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return boundary, b"".join(chunks)


def _assert_no_external_model_configuration_read() -> None:
    # The pytest runner and every owned child use an allowlisted environment.
    # Never inspect a credential value or read a project settings file here.
    assert "MODEL_API_KEY" not in os.environ


def test_cli_resume_scope20_reuses_later_batches_and_preserves_parent() -> None:
    """A real failed first batch is retried while successful later batches are reused."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("resume_scope20_reuses_later_batches")
    fixture = _new_project(tmp_case, pages=36, evidence_root=evidence_root, name="resume-scope20")
    plan = _planned_batches(fixture, evidence_root, "resume-scope20")
    assert plan["planned_batches"] >= 4

    def resume_responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event.get("stage") == "annual_discovery" and event.get("batch_index") == 1:
            # Keep parent batch 1 unresolved even if the client retries it.
            return MockReply(content="[]")
        return _finding_response(event, user_payload)

    model = LoopbackChatFixture(resume_responder).start()
    parent: CliAttempt | None = None
    child: CliAttempt | None = None
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        parent = _begin_cli(fixture, evidence_root, "resume-parent", model)
        parent_returncode = _finish_cli(parent, timeout=90)
        _capture_cli_evidence(parent, evidence_root, model, "resume-parent")
        parent_events = model.events()
        parent_archive = _read_report(fixture, parent.run_id)
        parent_discovery = _discovery_from_report(parent_archive.report)
        parent_contract = parent_discovery["execution_contract"]
        parent_phase = parent_contract["phase_budget"]
        parent_rows = _contract_rows(parent_discovery)
        parent_successful = {
            int(row["batch_index"])
            for row in parent_rows
            if row.get("state") == "succeeded"
        }
        parent_unresolved = {
            int(row["batch_index"])
            for row in parent_rows
            if row.get("state") in {"failed", "partial", "pending"}
        }
        assert parent_returncode == 2
        assert parent_phase["scope"] == "new_run"
        assert parent_phase["task_provider_attempt_limit"] == 30
        assert parent_phase["stage_provider_attempt_limits"] == {
            "annual_discovery": 18,
            "annual_investigation": 2,
            "annual_issue_grouping": 8,
            "annual_review": 2,
        }
        assert 1 in parent_unresolved
        assert any(index > 1 for index in parent_successful)
        assert parent_discovery["coverage"]["complete"] is False
        parent_hashes = _hash_run_and_report(fixture, parent.run_id)
        assert parent_hashes
        _write_json_new(evidence_root / "resume-parent-hashes-before.json", parent_hashes)

        parent_event_batches = [
            item.get("batch_index")
            for item in parent_events
            if item.get("stage") == "annual_discovery"
            if item.get("status") == 200
        ]
        assert parent_event_batches.count(1) == 1
        assert parent_successful.issubset({int(index) for index in parent_event_batches if index is not None})

        model.set_responder(_finding_response)
        child_start_sequence = len(parent_events)
        child = _begin_cli(
            fixture,
            evidence_root,
            "resume-child",
            model,
            parent_run_id=parent.run_id,
        )
        child_returncode = _finish_cli(child, timeout=90)
        child_events = model.events()[child_start_sequence:]
        _capture_cli_evidence(child, evidence_root, model, "resume-child")
        child_archive = _read_report(fixture, child.run_id)
        child_discovery = _discovery_from_report(child_archive.report)
        child_contract = child_discovery["execution_contract"]
        child_phase = child_contract["phase_budget"]
        child_rows = _contract_rows(child_discovery)
        child_attempted_batches = [
            int(item["batch_index"])
            for item in child_events
            if item.get("stage") == "annual_discovery"
            and type(item.get("batch_index")) is int
        ]
        child_ledger = read_gate_stage_admission_counts(
            fixture.root / "artifacts" / "runs" / child.run_id / "call-gate.sqlite3",
            child.run_id,
        )
        assert child_returncode == 0
        assert child_archive.snapshot is not None
        assert child_archive.snapshot.get("parent_run_id") == parent.run_id
        assert child_phase["scope"] == "resume"
        assert child_phase["task_provider_attempt_limit"] == 20
        assert child_phase["stage_provider_attempt_limits"] == {
            "annual_discovery": 14,
            "annual_investigation": 2,
            "annual_issue_grouping": 2,
            "annual_review": 2,
        }
        assert child_attempted_batches == sorted(parent_unresolved)
        assert not (parent_successful & set(child_attempted_batches))
        assert child_discovery["coverage"]["complete"] is True
        assert child_ledger["annual_discovery"] == len(child_attempted_batches)
        assert child_ledger["annual_discovery"] <= 14
        assert sum(child_ledger.values()) <= 20
        assert _hash_run_and_report(fixture, parent.run_id) == parent_hashes

        exports = _save_exports(fixture, child.run_id, evidence_root, "resume-child")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "resume-child", child_discovery
        )
        _write_json_new(evidence_root / "resume-scope20-result.json", {
            "case": "actual standalone CLI parent and child Popen; loopback-only model calls",
            "parent_run_id": parent.run_id,
            "child_run_id": child.run_id,
            "parent_returncode": parent_returncode,
            "child_returncode": child_returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "parent_actual_http_requests": len(parent_events),
            "child_actual_http_requests": len(child_events),
            "combined_actual_http_requests": len(parent_events) + len(child_events),
            "parent_successful_batch_indexes": sorted(parent_successful),
            "parent_unresolved_batch_indexes": sorted(parent_unresolved),
            "child_actual_discovery_http_batch_indexes": child_attempted_batches,
            "child_stage_admissions": child_ledger,
            "parent_scope_and_limits": {
                "scope": parent_phase["scope"],
                "task_limit": parent_phase["task_provider_attempt_limit"],
                "stage_limits": parent_phase["stage_provider_attempt_limits"],
            },
            "child_scope_and_limits": {
                "scope": child_phase["scope"],
                "task_limit": child_phase["task_provider_attempt_limit"],
                "stage_limits": child_phase["stage_provider_attempt_limits"],
            },
            "child_coverage": child_discovery["coverage"],
            "parent_archive_hashes_before": parent_hashes,
            "parent_archive_unchanged_after_child": True,
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        if parent is not None and parent.process.poll() is None:
            try:
                _finish_cli(parent, timeout=10)
            except Exception:
                pass
        if child is not None and child.process.poll() is None:
            try:
                _finish_cli(child, timeout=10)
            except Exception:
                pass
        model.close()


def test_cli_whole_work_deadline_marks_all_admitted_plan_as_time_exhausted() -> None:
    """Whole-work cutoff is distinct from stage cutoff when all planned calls started."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("whole_work_deadline_all_admitted")
    model = LoopbackChatFixture(
        lambda event, payload: MockReply(content=_finding_reply(payload), block=True)
        if event.get("stage") == "annual_discovery"
        else _success_response(event, payload)
    ).start()
    fixture = _new_project(tmp_case, pages=3, evidence_root=evidence_root, name="whole-work-deadline")
    plan = _planned_batches(fixture, evidence_root, "whole-work-deadline")
    assert 1 <= plan["planned_batches"] <= 3
    capture: CliAttempt | None = None
    late_release = False
    workers: list[dict[str, Any]] = []
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        stage_limits = {
            "annual_discovery": 40,
            "annual_investigation": 1,
            "annual_issue_grouping": 1,
            "annual_review": 1,
        }
        soft_deadline = time.monotonic() + 5
        capture = _begin_cli(
            fixture,
            evidence_root,
            "whole-work-deadline",
            model,
            concurrency=3,
            main_soft_deadline=soft_deadline,
            main_hard_deadline=soft_deadline + 15,
            main_limit_seconds=60,
            stage_time_limits=stage_limits,
        )
        model.wait_for(
            lambda events: len([item for item in events if item.get("stage") == "annual_discovery"])
            == plan["planned_batches"],
            timeout=15,
        )
        model.wait_blocked(timeout=10)
        workers = _wait_for_workers(capture.process.pid, expected=plan["planned_batches"], timeout=10)
        worker_pids = [int(item["pid"]) for item in workers]
        assert model.actual_http_peak <= 3
        assert len(model.events("annual_discovery")) == plan["planned_batches"]
        assert all("status" not in item for item in model.events("annual_discovery"))

        returncode = _finish_cli(capture, timeout=20)
        cli_finished_monotonic = time.monotonic()
        worker_exit_observed = _wait_for_worker_exit(worker_pids, timeout=10)
        assert returncode in {0, 2}
        assert worker_exit_observed is True
        terminal_hash_before_release = _hash_run_and_report(fixture, capture.run_id)
        model.release()
        late_release = True
        model.wait_for(
            lambda events: all(
                item.get("client_disconnected_before_response") is True
                for item in events
                if item.get("stage") == "annual_discovery"
            ),
            timeout=12,
        )
        time.sleep(0.1)
        terminal_hash_after_release = _hash_run_and_report(fixture, capture.run_id)
        assert terminal_hash_after_release == terminal_hash_before_release
        assert _wait_for_worker_exit(worker_pids, timeout=1) is True
        _capture_cli_evidence(capture, evidence_root, model, "whole-work-deadline")

        archive = _read_report(fixture, capture.run_id)
        discovery = _discovery_from_report(archive.report)
        coverage = discovery["coverage"]
        contract = discovery["execution_contract"]
        scheduler = contract["scheduler"]
        ledger = read_gate_stage_admission_counts(
            fixture.root / "artifacts" / "runs" / capture.run_id / "call-gate.sqlite3",
            capture.run_id,
        )
        assert coverage["time_budget_exhausted"] is True
        assert coverage["unattempted_batches"] == 0
        assert scheduler["time_deadline_reason"] == "workflow_time_budget_exhausted"
        assert scheduler["dispatch_closed_reason"] == "workflow_time_budget_exhausted"
        assert scheduler["inflight_drained"] is True
        assert scheduler["inflight_count"] == 0
        assert scheduler["checkpoint_coordinator_count"] == 1
        assert ledger["annual_discovery"] == plan["planned_batches"]
        assert ledger["annual_review"] == 0
        assert cli_finished_monotonic >= soft_deadline
        assert cli_finished_monotonic < soft_deadline + 15

        exports = _save_exports(fixture, capture.run_id, evidence_root, "whole-work-deadline")
        export_common = _assert_export_snapshots_consistent(exports)
        export_content = _assert_export_content_consistent(
            evidence_root / "exports" / "whole-work-deadline", discovery
        )
        _write_json_new(evidence_root / "whole-work-deadline-result.json", {
            "case": "actual standalone CLI Popen and blocked loopback HTTP workers at whole-work soft deadline",
            "controlled_override": {
                "owner_main_limit_seconds": 60,
                "soft_deadline_seconds_from_harness_launch": 5,
                "stage_limits_seconds": stage_limits,
                "planned_batches": plan["planned_batches"],
            },
            "run_id": capture.run_id,
            "cli_returncode": returncode,
            "source_pdf_sha256": fixture.source_sha256,
            "runtime_source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_cli_identity": identity,
            "actual_discovery_http_requests": len(model.events("annual_discovery")),
            "actual_http_peak": model.actual_http_peak,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "durable_stage_admission_counts": ledger,
            "coverage": coverage,
            "scheduler": scheduler,
            "worker_pids": workers,
            "worker_exit_observed": worker_exit_observed,
            "worker_pids_exited_at": _utc_now(),
            "late_http_responses_disconnected": True,
            "terminal_hash_before_late_release": terminal_hash_before_release,
            "terminal_hash_after_late_release": terminal_hash_after_release,
            "export_common_projection": export_common,
            "export_content_projection": export_content,
            "external_provider_requests": 0,
        })
    finally:
        if not late_release:
            model.release()
        if capture is not None and capture.process.poll() is None:
            try:
                _finish_cli(capture, timeout=5)
            except Exception:
                pass
        if capture is not None and capture.process.poll() is not None:
            event_path = evidence_root / "http-events-whole-work-deadline.json"
            if not event_path.exists():
                _capture_cli_evidence(capture, evidence_root, model, "whole-work-deadline")
        model.close()


def test_api_queue_running_deadlines_and_terminal_elapsed_are_real() -> None:
    """A real loopback API queues, starts its CLI child, and freezes terminal timing."""
    _assert_no_external_model_configuration_read()
    attempt_id, tmp_case, evidence_root = _case_paths("api_queue_running_terminal_timing")
    fixture = _new_project_from_annual_sample(
        tmp_case, evidence_root=evidence_root, name="api-queue-timing"
    )
    def responder(event: dict[str, Any], user_payload: object) -> MockReply:
        if event.get("sequence") == 1:
            return MockReply(content=_finding_reply(user_payload) if event.get("stage") == "annual_discovery" else _success_response(event, user_payload).content, block=True)
        return _success_response(event, user_payload)

    model = LoopbackChatFixture(responder).start()
    api_process: subprocess.Popen[bytes] | None = None
    stdout_handle = (evidence_root / "api.stdout.raw").open("xb")
    stderr_handle = (evidence_root / "api.stderr.raw").open("xb")
    created_jobs: list[str] = []
    try:
        identity = _runtime_identity(fixture, evidence_root, attempt_id)
        settings = save_local_settings(
            fixture.root,
            base_url=model.base_url,
            model_name=_MODEL_NAME,
            api_key=_LOCAL_API_KEY,
        )
        assert settings.configured is True
        observer_path = evidence_root / "api-cli-popen-observer.jsonl"
        instrumentation_dir = fixture.root / "backend" / "src"
        sitecustomize = instrumentation_dir / "sitecustomize.py"
        observer_source = '''\
import json
import os
import subprocess
import time

_capture_key = "FINTRACE_R3_API_PROCESS_CAPTURE"
_capture_path = os.environ.get(_capture_key)
_original_popen = subprocess.Popen

class _ObservedPopen(_original_popen):
    def __init__(self, args, *positional, **keywords):
        values = list(args) if isinstance(args, (list, tuple)) else [args]
        argv = [str(value) for value in values]
        is_model_cli = any(value.endswith("analyze_annual.py") for value in argv) and "--with-model" in argv
        raw_env = keywords.get("env")
        child_env = dict(os.environ if raw_env is None else raw_env)
        if is_model_cli and _capture_path:
            child_env.pop(_capture_key, None)
            keywords["env"] = child_env
        super().__init__(args, *positional, **keywords)
        if not is_model_cli or not _capture_path:
            return
        popen_wall_time = time.time()
        popen_monotonic = time.monotonic()
        allowed = (
            "FINTRACE_ANNUAL_MAIN_SOFT_DEADLINE_MONOTONIC",
            "FINTRACE_ANNUAL_MAIN_HARD_DEADLINE_MONOTONIC",
            "FINTRACE_ANNUAL_MAIN_LIMIT_SECONDS",
            "FINTRACE_ANNUAL_STAGE_TIME_LIMITS_SECONDS",
            "FINTRACE_CALL_GATE_TASK_LIMIT",
            "FINTRACE_CALL_GATE_STAGE_LIMITS",
            "FINTRACE_ACTIVE_SOURCE_SHA256",
            "MODEL_BASE_URL",
            "MODEL_NAME",
        )
        record = {
            "pid": self.pid,
            "parent_pid": os.getpid(),
            "observed_at": popen_wall_time,
            "popen_returned_at": popen_wall_time,
            "popen_monotonic": popen_monotonic,
            "argv": argv,
            "environment": {key: child_env[key] for key in allowed if key in child_env},
            "model_api_key_present": bool(child_env.get("MODEL_API_KEY")),
        }
        with open(_capture_path, "a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\\n")
            output.flush()

subprocess.Popen = _ObservedPopen
'''
        _write_bytes_new(sitecustomize, observer_source.encode("utf-8"))
        observer_copy = evidence_root / "api-cli-popen-observer.py"
        shutil.copy2(sitecustomize, observer_copy)
        api_env = _minimal_environment(fixture.root)
        api_env.update({
            "FINTRACE_ANNUAL_JOB_TIMEOUT_SECONDS": "60",
            "FINTRACE_R3_API_PROCESS_CAPTURE": str(observer_path),
        })
        api_port = _free_loopback_port()
        base_url = f"http://127.0.0.1:{api_port}"
        command = [
            sys.executable, "-m", "uvicorn", "finagent.api.app:app",
            "--host", "127.0.0.1", "--port", str(api_port),
            "--log-level", "warning", "--no-access-log",
        ]
        api_started_at = _utc_now()
        api_process = subprocess.Popen(
            command,
            cwd=fixture.root,
            env=api_env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            shell=False,
        )
        api_popen_returned_at = _utc_now()
        api_process_creation = _process_creation_metadata(api_process.pid)
        _write_json_new(evidence_root / "api-process.json", {
            "argv": command,
            "cwd": str(fixture.root),
            "pid": api_process.pid,
            "loopback_base_url": base_url,
            "started_at": api_started_at,
            "popen_returned_at": api_popen_returned_at,
            "process_creation_metadata": api_process_creation,
            "owner_timeout_seconds": 60,
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "source_file_count": fixture.runtime_file_count,
            "loaded_cli_identity_probe": identity,
            "model_configuration_source": "synthetic loopback only; isolated project model_settings.json",
            "api_capture_instrumentation": str(observer_copy),
        })
        _wait_api_ready(base_url, api_process, timeout=30)
        identity_status, identity_raw, _ = _api_request(base_url, "GET", "/v1/runtime-identity")
        assert identity_status == 200
        api_identity = json.loads(identity_raw.decode("utf-8"))
        loaded_api = api_identity["api"]
        loaded_cli_startup = api_identity["cli_at_startup"]
        assert loaded_api["process_id"] == api_process.pid
        assert loaded_api["loaded_source_match"] is True
        assert loaded_api["source_manifest_sha256"] == fixture.runtime_source_sha256
        assert loaded_cli_startup["loaded_source_match"] is True
        assert loaded_cli_startup["source_manifest_sha256"] == fixture.runtime_source_sha256
        assert loaded_cli_startup["fingerprint"] == identity["fingerprint"]

        source_relative = fixture.source_pdf.relative_to(fixture.root / "data" / "raw").as_posix()
        request_value = {
            "company_id": fixture.company_id,
            "report_year": fixture.report_year,
            "document_id": fixture.document_id,
            "source_pdf_path": source_relative,
            "sha256": fixture.source_sha256,
            "mode": "model_investigation",
        }

        def create_job() -> dict[str, Any]:
            status, raw, _headers = _api_request(
                base_url,
                "POST",
                "/v1/annual-analysis-jobs",
                body=json.dumps(request_value).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            if status != 202:
                raise AssertionError(f"annual_job_create_failed:{status}:{raw[:1000]!r}")
            value = json.loads(raw.decode("utf-8"))
            created_jobs.append(str(value["job_id"]))
            return value

        def get_job(job_id: str) -> dict[str, Any]:
            status, raw, _headers = _api_request(
                base_url, "GET", f"/v1/annual-analysis-jobs/{job_id}"
            )
            if status != 200:
                raise AssertionError(f"annual_job_read_failed:{status}:{raw[:1000]!r}")
            return json.loads(raw.decode("utf-8"))

        first_created = create_job()
        first_id = str(first_created["job_id"])
        model.wait_blocked(timeout=35)
        first_running = _wait_until(
            lambda: (state if (state := get_job(first_id))["status"] == "running" else None),
            timeout=20,
        )
        assert first_running["analysis_budget"]["elapsed_running_seconds"] > 0
        second_created = create_job()
        second_id = str(second_created["job_id"])
        second_queued_1 = get_job(second_id)
        time.sleep(0.3)
        second_queued_2 = get_job(second_id)
        assert second_queued_1["status"] == second_queued_2["status"] == "queued"
        assert second_queued_1["analysis_budget"]["elapsed_running_seconds"] == 0.0
        assert second_queued_2["analysis_budget"]["elapsed_running_seconds"] == 0.0
        assert second_queued_2["analysis_budget"]["queue_seconds"] > second_queued_1["analysis_budget"]["queue_seconds"]
        assert second_queued_2["analysis_budget"]["provider_attempt_limits"]["task_effective"] == 30

        model.release()
        first_transitions = [first_created, first_running]
        second_transitions = [second_created, second_queued_1, second_queued_2]
        terminal_statuses = {"completed", "completed_with_issues", "failed", "interrupted", "cancelled"}
        first_terminal = _wait_until(
            lambda: (state if (state := get_job(first_id))["status"] in terminal_statuses else None),
            timeout=50,
            interval=0.1,
        )
        assert first_terminal["status"] in {"completed", "completed_with_issues"}
        second_running = _wait_until(
            lambda: (state if (state := get_job(second_id))["status"] == "running" else None),
            timeout=15,
            interval=0.05,
        )
        second_transitions.append(second_running)
        second_terminal = _wait_until(
            lambda: (state if (state := get_job(second_id))["status"] in terminal_statuses else None),
            timeout=50,
            interval=0.1,
        )
        assert second_terminal["status"] in {"completed", "completed_with_issues"}
        second_transitions.append(second_terminal)
        time.sleep(0.4)
        first_terminal_again = get_job(first_id)
        second_terminal_again = get_job(second_id)
        assert first_terminal_again["analysis_budget"]["elapsed_running_seconds"] == first_terminal["analysis_budget"]["elapsed_running_seconds"]
        assert second_terminal_again["analysis_budget"]["elapsed_running_seconds"] == second_terminal["analysis_budget"]["elapsed_running_seconds"]

        cli_records = [
            json.loads(line)
            for line in observer_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        model_cli_records = [
            item for item in cli_records if "--with-model" in item.get("argv", [])
        ]
        assert len(model_cli_records) == 2
        expected_fingerprint = loaded_cli_startup["fingerprint"]
        expected_stage_limits = {
            stage_name: value["time_limit_seconds"]
            for stage_name, value in second_running["analysis_budget"]["stages"].items()
        }
        for item in model_cli_records:
            env = item["environment"]
            assert item["pid"] > 0
            assert env["MODEL_BASE_URL"] == model.base_url
            assert env["MODEL_NAME"] == _MODEL_NAME
            assert item["model_api_key_present"] is True
            assert env["FINTRACE_ANNUAL_MAIN_LIMIT_SECONDS"] == "60"
            hard_remaining = (
                float(env["FINTRACE_ANNUAL_MAIN_HARD_DEADLINE_MONOTONIC"])
                - float(item["popen_monotonic"])
            )
            soft_remaining = (
                float(env["FINTRACE_ANNUAL_MAIN_SOFT_DEADLINE_MONOTONIC"])
                - float(item["popen_monotonic"])
            )
            assert 0 < hard_remaining <= 60
            assert 0 < soft_remaining <= 45
            assert abs(hard_remaining - soft_remaining - 15.0) < 1e-6
            assert json.loads(env["FINTRACE_ANNUAL_STAGE_TIME_LIMITS_SECONDS"]) == expected_stage_limits
            assert env["FINTRACE_CALL_GATE_TASK_LIMIT"] == "30"
            assert json.loads(env["FINTRACE_CALL_GATE_STAGE_LIMITS"]) == {
                "annual_discovery": 18,
                "annual_investigation": 2,
                "annual_issue_grouping": 8,
                "annual_review": 2,
            }
            fingerprint_args = item["argv"]
            fingerprint = fingerprint_args[fingerprint_args.index("--expected-runtime-fingerprint") + 1]
            assert fingerprint == expected_fingerprint
        assert all(not _pid_exists(int(item["pid"])) for item in model_cli_records)

        for value in (first_terminal, second_terminal):
            budget = value["analysis_budget"]
            assert budget["main_report_limit_seconds"] == 60
            assert budget["work_deadline_seconds"] == 45
            assert budget["finalization_reserve_seconds"] == 15
            assert budget["scope"] == "new_run"
            assert budget["provider_attempt_limits"]["task_effective"] == 30
            assert budget["elapsed_running_seconds"] > 0
            assert budget["queue_seconds"] >= 0

        provider_events = model.events()
        assert provider_events
        assert all(event.get("status") == 200 for event in provider_events)
        api_job_evidence: dict[str, Any] = {}
        for label, terminal_state in (
            ("first", first_terminal),
            ("second", second_terminal),
        ):
            job_id = str(terminal_state["job_id"])
            run_id = str(terminal_state["run_id"])
            job_dir = fixture.root / "artifacts" / "annual-analysis-jobs" / job_id
            run_dir = fixture.root / "artifacts" / "runs" / run_id
            report_dir = fixture.root / "artifacts" / "reports" / run_id
            artifact_hashes: dict[str, list[dict[str, Any]]] = {}
            for kind, source_dir in (
                ("job", job_dir),
                ("run", run_dir),
                ("report", report_dir),
            ):
                if not source_dir.is_dir():
                    raise AssertionError(f"api_runtime_artifact_missing:{kind}")
                destination = evidence_root / "runtime-artifacts" / label / kind / source_dir.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_dir, destination)
                artifact_hashes[kind] = [
                    {
                        "relative_path": path.relative_to(destination).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                    for path in sorted(destination.rglob("*"))
                    if path.is_file()
                ]

            gate_database = job_dir / "call-gate.sqlite3"
            if not gate_database.is_file():
                raise AssertionError("api_job_call_gate_database_missing")
            gate_dir = evidence_root / "ledgers" / label
            gate_dir.mkdir(parents=True, exist_ok=False)
            gate_copy = gate_dir / "call-gate.sqlite3"
            shutil.copy2(gate_database, gate_copy)
            gate_database_sha256 = _sha256(gate_database)
            gate_copy_sha256 = _sha256(gate_copy)
            assert gate_copy_sha256 == gate_database_sha256
            stage_counts = read_gate_stage_admission_counts(gate_database, job_id)
            _write_json_new(gate_dir / "stage-admission-counts.json", {
                "gate_key": job_id,
                "database_sha256": gate_database_sha256,
                "database_copy_sha256": gate_copy_sha256,
                "stage_admission_counts": stage_counts,
                "reads_are_read_only": True,
            })
            api_job_evidence[label] = {
                "job_id": job_id,
                "run_id": run_id,
                "artifact_hashes": artifact_hashes,
                "stage_admission_counts": stage_counts,
            }
        source_record_copy = evidence_root / "inputs" / f"{fixture.source_sha256[:12]}-api-source.json"
        shutil.copy2(fixture.source_record, source_record_copy)
        _write_json_new(evidence_root / "http-events-api.json", {
            "provider": "127.0.0.1 loopback fixture",
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "actual_http_peak": model.actual_http_peak,
            "events": provider_events,
            "actual_http_requests": len(provider_events),
            "external_provider_requests": 0,
        })
        api_event = {
            "api_status": api_identity,
            "jobs": {
                first_id: {
                    "run_id": first_terminal["run_id"],
                    "queued": first_created,
                    "running": first_running,
                    "terminal": first_terminal,
                    "terminal_rechecked": first_terminal_again,
                },
                second_id: {
                    "run_id": second_terminal["run_id"],
                    "queued_initial": second_queued_1,
                    "queued_after_wait": second_queued_2,
                    "running": second_running,
                    "terminal": second_terminal,
                    "terminal_rechecked": second_terminal_again,
                },
            },
            "actual_cli_popen_records": model_cli_records,
            "loopback_http_events": provider_events,
        }
        _write_json_new(evidence_root / "api-queue-timing-result.json", {
            "case": "actual Uvicorn API Popen + queued/running/terminal jobs + actual API-owned CLI Popen",
            "api_pid": api_process.pid,
            "api_port": api_port,
            "source_pdf_sha256": fixture.source_sha256,
            "source_manifest_sha256": fixture.runtime_source_sha256,
            "loaded_api_identity": loaded_api,
            "loaded_cli_startup_identity": loaded_cli_startup,
            "owner_timeout_seconds": 60,
            "api_work_deadline_seconds": 45,
            "api_finalization_reserve_seconds": 15,
            "jobs": api_event["jobs"],
            "formal_runtime_artifacts": api_job_evidence,
            "process_creation_metadata": api_process_creation,
            "source_record_sha256": _sha256(source_record_copy),
            "actual_cli_popen_records": model_cli_records,
            "actual_provider_http_requests": len(provider_events),
            "actual_http_peak": model.actual_http_peak,
            "provider_base_url": model.base_url,
            "provider_port": model.port,
            "terminal_elapsed_frozen": True,
            "external_provider_requests": 0,
        })
    finally:
        model.release()
        api_termination_action = None
        if api_process is not None and api_process.poll() is None:
            api_termination_action = "terminate_only_owned_loopback_api_after_case"
            for job_id in created_jobs:
                try:
                    _api_request(
                        f"http://127.0.0.1:{api_port}",
                        "POST",
                        f"/v1/annual-analysis-jobs/{job_id}/cancel",
                        body=b"{}",
                        headers={"Content-Type": "application/json"},
                        timeout=3,
                    )
                except Exception:
                    pass
            api_process.terminate()
            try:
                api_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                api_termination_action = "kill_only_owned_loopback_api_after_terminate_timeout"
                api_process.kill()
                api_process.wait(timeout=5)
        model.close()
        stdout_handle.close()
        stderr_handle.close()
        if api_process is not None:
            _write_json_new(evidence_root / "api-process-exit.json", {
                "pid": api_process.pid,
                "returncode": api_process.returncode,
                "exit_observed": api_process.poll() is not None,
                "termination_action": api_termination_action,
                "finished_at": _utc_now(),
            })
