from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from finagent.agents import annual_review_wire as _ANNUAL_WIRE_MODULE
from finagent.agents.annual_review_wire import decode_annual_review_v3_wire
from finagent.api.annual_report_read import load_annual_report
from finagent.core.annual_runtime_identity import runtime_identity_is_valid
from finagent.ingestion.parse_text_pdf import parse_text_pdf

SAMPLE_DIR = ROOT / "data" / "samples" / "annual_issue_semantics_v1"
ISSUE_PDF = SAMPLE_DIR / "issue.pdf"
CLEAN_PDF = SAMPLE_DIR / "clean.pdf"
EXPECTED_HASHES = {
    "issue": "c31b9e59ec2a31c0b9afce81be231466b6783213fda80ecf2501221365f265e9",
    "clean": "256edec135d12906cd862ff9c0aba972d8bf997ea422dc174724f894b5abaa68",
}
RUN_ID_RE = re.compile(r"run_id=(annual-analysis-[0-9a-f-]{36})")
FIXTURE_MODEL_NAME = "r4-offline-semantic-fixture"
FREEZE_MANIFEST_PATH = ROOT / "artifacts" / "evaluations" / "annual-recovery-r4-20261010" / "r4-candidate-freeze-manifest-20261010T020439Z-978ec097" / "candidate-freeze-manifest.json"
FREEZE_MANIFEST_RAW_SHA256 = "53575e0277038bd96a4ed184284cf88013c43a7feb3846cf1763b32935c3deeb"
FREEZE_MANIFEST_INTERNAL_SHA256 = "321d309c073be1ce2393ece8b82b473c1e7fea5dc8e7118227d367598a26630c"
FROZEN_RUNTIME_SOURCE_SHA256 = "38592c3737fb1a36bbb0be9c9c3ac193a99b1c95f2050883bfb7e465a14fd201"
FROZEN_RUNTIME_SOURCE_FILE_COUNT = 81
FROZEN_BUSINESS_CONFIG_SHA256 = "c881cf2fcc3820fdd247b789ed57f933e1633bccb2dc1a7702fb8e1ffe526e92"
FROZEN_BUSINESS_CONFIG_FILE_COUNT = 10
FROZEN_FRONTEND_SHA256 = "7b00b0f4b3ee6859ff87c10d6c1f02584f14c89fe016fe383d6f592bc022a06c"
FROZEN_FRONTEND_FILE_COUNT = 23
FROZEN_REVIEW_PROMPT_SHA256 = "04bd401621dad72f070e149a23776cc9e78983f7d60751fa6250751fcfe12561"
_CATEGORIES = ("设备收入确认", "尾款结算安排", "设备与集成服务分界")

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
            json.dump({"process_id": os.getpid(), "argv": list(sys.argv), "runtime_identity": value}, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        return

sys.setprofile(_capture_actual_cli_identity)
'''


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_ANNUAL_WIRE_MODULE_PATH = Path(_ANNUAL_WIRE_MODULE.__file__).resolve()
_ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT = _sha256_file(_ANNUAL_WIRE_MODULE_PATH)


def _business_input_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(payload)


def _frozen_candidate_manifest(attempt: Path) -> dict[str, Any]:
    path = attempt / "candidate-freeze-manifest.json"
    raw = path.read_bytes()
    raw_sha = _sha256_bytes(raw)
    if raw_sha != FREEZE_MANIFEST_RAW_SHA256:
        raise AssertionError(f"attempt candidate-freeze manifest raw SHA differs: {raw_sha}")
    manifest = json.loads(raw.decode("utf-8"))
    payload = dict(manifest)
    claimed_internal = payload.pop("manifest_sha256", None)
    internal_raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    internal_sha = _sha256_bytes(internal_raw)
    if claimed_internal != internal_sha or internal_sha != FREEZE_MANIFEST_INTERNAL_SHA256:
        raise AssertionError("attempt candidate-freeze manifest internal digest does not match the approved freeze")
    if manifest.get("runtime_source_manifest_sha256") != FROZEN_RUNTIME_SOURCE_SHA256:
        raise AssertionError("attempt candidate-freeze runtime source digest differs from the approved freeze")
    if manifest.get("runtime_source_file_count") != FROZEN_RUNTIME_SOURCE_FILE_COUNT:
        raise AssertionError("attempt candidate-freeze runtime file count differs from the approved freeze")
    business_entries = manifest.get("business_config_entries")
    frontend_entries = manifest.get("frontend_entries")
    if (
        manifest.get("business_config_manifest_sha256") != FROZEN_BUSINESS_CONFIG_SHA256
        or not isinstance(business_entries, list)
        or len(business_entries) != FROZEN_BUSINESS_CONFIG_FILE_COUNT
    ):
        raise AssertionError("attempt candidate-freeze business config differs from the approved freeze")
    if (
        manifest.get("frontend_manifest_sha256") != FROZEN_FRONTEND_SHA256
        or not isinstance(frontend_entries, list)
        or len(frontend_entries) != FROZEN_FRONTEND_FILE_COUNT
    ):
        raise AssertionError("attempt candidate-freeze frontend differs from the approved freeze")
    if FREEZE_MANIFEST_PATH.read_bytes() != raw:
        raise AssertionError("attempt candidate-freeze manifest differs from the ROOT approved manifest")
    prompt = next((
        row for row in manifest.get("business_config_entries", [])
        if isinstance(row, dict) and row.get("path") == "config/prompts/annual_review_v3.md"
    ), None)
    if not isinstance(prompt, dict) or prompt.get("sha256") != FROZEN_REVIEW_PROMPT_SHA256:
        raise AssertionError("candidate freeze does not bind the reviewed annual_review_v3 prompt bytes")
    return {
        "manifest": manifest,
        "raw_sha256": raw_sha,
        "internal_sha256": internal_sha,
        "runtime_source_manifest_sha256": FROZEN_RUNTIME_SOURCE_SHA256,
        "runtime_source_file_count": FROZEN_RUNTIME_SOURCE_FILE_COUNT,
        "business_config_manifest_sha256": FROZEN_BUSINESS_CONFIG_SHA256,
        "business_config_file_count": FROZEN_BUSINESS_CONFIG_FILE_COUNT,
        "frontend_manifest_sha256": FROZEN_FRONTEND_SHA256,
        "frontend_file_count": FROZEN_FRONTEND_FILE_COUNT,
    }


def _capture_expected_cli_runtime_identity(attempt: Path) -> dict[str, Any]:
    """Capture a standard CLI-only identity; this exact fingerprint gates each processing CLI."""

    evidence_dir = attempt / "expected-runtime-identity"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    argv = [sys.executable, str(ROOT / "scripts" / "analyze_annual.py"), "--runtime-identity"]
    omitted_prefixes = ("MODEL_", "FINTRACE_CALL_GATE_")
    omitted_names = {
        "FINTRACE_ACTIVE_SOURCE_SHA256",
        "FINTRACE_CALL_CANCEL_FILE",
        "FINTRACE_CALL_LIVENESS_FILE",
        "FINTRACE_R4_ATTEMPT_DIR",
        "FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR",
        "PYTHONHOME",
        "PYTHONPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    environment = {
        name: os.environ[name]
        for name in os.environ
        if name not in omitted_names and not name.startswith(omitted_prefixes)
    }
    environment["PYTHONUTF8"] = "1"
    started_at = _utc_now()
    started = time.monotonic()
    process_error = None
    try:
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=60,
        )
        stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
    except Exception as exc:
        stdout = b""
        stderr = f"{type(exc).__name__}: {exc}".encode("utf-8", errors="replace")
        exit_code = None
        process_error = f"{type(exc).__name__}: {exc}"
    ended = time.monotonic()
    _write_new(evidence_dir / "stdout.raw.txt", stdout)
    _write_new(evidence_dir / "stderr.raw.txt", stderr)
    identity: dict[str, Any] | None = None
    parse_error = None
    try:
        parsed = json.loads(stdout.decode("utf-8"))
        if isinstance(parsed, dict):
            identity = parsed
        else:
            parse_error = "runtime identity stdout was not a JSON object"
    except (UnicodeError, json.JSONDecodeError) as exc:
        parse_error = f"{type(exc).__name__}: {exc}"
    record = {
        "argv": argv,
        "working_directory": str(ROOT),
        "shell": False,
        "started_at_utc": started_at,
        "ended_at_utc": _utc_now(),
        "started_monotonic": started,
        "ended_monotonic": ended,
        "elapsed_seconds": round(ended - started, 6),
        "exit_code": exit_code,
        "process_error": process_error,
        "parse_error": parse_error,
        "inherited_MODEL_values_read": False,
        "real_model_config_file_read": False,
        "runtime_identity": identity,
    }
    _write_json_new(evidence_dir / "runtime-identity-probe.json", record)
    if exit_code != 0 or identity is None or not runtime_identity_is_valid(identity):
        raise AssertionError("standard CLI runtime identity probe failed; see expected-runtime-identity raw evidence")
    if identity.get("role") != "cli":
        raise AssertionError("runtime identity probe did not report role=cli")
    if identity.get("source_manifest_sha256") != FROZEN_RUNTIME_SOURCE_SHA256 or identity.get("source_file_count") != FROZEN_RUNTIME_SOURCE_FILE_COUNT:
        raise AssertionError("runtime identity probe does not match the approved frozen source manifest")
    return record


def _write_new(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)


def _write_json_new(path: Path, value: Any) -> None:
    _write_new(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _attempt_root() -> Path:
    configured = os.environ.get("FINTRACE_R4_ATTEMPT_DIR")
    runner_value = os.environ.get("FINTRACE_R4_RUNNER_DIR")
    evaluation_value = os.environ.get("FINTRACE_R4_EVALUATION_DIR")
    if not configured or not runner_value or not evaluation_value:
        raise RuntimeError("R4 semantic test must be launched by its task-owned evidence runner")
    path = Path(configured).resolve()
    evaluation = Path(evaluation_value).resolve()
    runner = Path(runner_value).resolve()
    evaluation_root = (ROOT / "artifacts" / "evaluations" / "annual-recovery-r4-20261010").resolve()
    runner_root = (ROOT / "artifacts" / "tmp" / "recovery-r4-current-semantics-20261010").resolve()
    if (
        not evaluation.is_relative_to(evaluation_root)
        or evaluation.parent != evaluation_root
        or not evaluation.name.startswith("final-semantic-current-")
        or path != evaluation / "semantic"
        or not runner.is_relative_to(runner_root)
        or runner == runner_root
    ):
        raise RuntimeError("R4 semantic evidence paths are outside the approved unique task locations")
    return path


def _fresh_environment(*, model_url: str, source_sha: str) -> dict[str, str]:
    """Copy ordinary process settings without ever reading inherited MODEL_* values."""

    omitted_prefixes = ("MODEL_", "FINTRACE_CALL_GATE_")
    omitted_names = {
        "FINTRACE_ACTIVE_SOURCE_SHA256",
        "FINTRACE_CALL_CANCEL_FILE",
        "FINTRACE_CALL_LIVENESS_FILE",
        "FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR",
        "PYTHONHOME",
        "PYTHONPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    }
    result: dict[str, str] = {}
    for name in os.environ:
        if name in omitted_names or name.startswith(omitted_prefixes):
            continue
        result[name] = os.environ[name]
    result.update({
        "MODEL_BASE_URL": model_url,
        "MODEL_API_KEY": "synthetic-loopback-only-r4-test-key",
        "MODEL_NAME": FIXTURE_MODEL_NAME,
        "FINTRACE_DISCOVERY_CONCURRENCY": "1",
        "PYTHONUTF8": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "FINTRACE_ACTIVE_SOURCE_SHA256": source_sha,
    })
    return result


def _register_accepted_pdf(attempt: Path, *, case: str, original: Path, expected_sha: str) -> dict[str, Any]:
    if not original.is_file():
        raise FileNotFoundError(f"accepted controlled PDF is missing: {original}")
    original_bytes = original.read_bytes()
    actual_sha = _sha256_bytes(original_bytes)
    if actual_sha != expected_sha:
        raise AssertionError(f"accepted PDF hash changed for {case}: {actual_sha}")

    token = uuid.uuid4().hex[:12]
    company_id = f"R4-{case.upper()}-{token}"
    document_id = f"r4-{case}-{token}"
    relative_dir = Path(company_id) / "2025" / document_id
    raw_dir = ROOT / "data" / "raw" / relative_dir
    raw_dir.mkdir(parents=True, exist_ok=False)
    copied_pdf = raw_dir / "source.pdf"
    with copied_pdf.open("xb") as stream:
        stream.write(original_bytes)
    if _sha256_file(copied_pdf) != expected_sha:
        raise AssertionError(f"byte-for-byte source registration failed for {case}")
    source_record = {
        "document_id": document_id,
        "company_id": company_id,
        "report_type": "R4 受控原创模拟年度文本（非真实披露）",
        "report_period": "2025-12-31",
        "local_path": (Path("data") / "raw" / relative_dir / copied_pdf.name).as_posix(),
        "sha256": expected_sha,
        "metadata_origin": "accepted_r4_controlled_pdf_copy",
        "nature": "接受的 R4 原创模拟压力 PDF 的逐字节登记副本；不对应真实公司披露。",
        "use_condition": "仅供本机离线语义集成测试；不作为真实财务事实、舞弊标签或无舞弊标签。",
    }
    source_record_path = raw_dir / "source.json"
    _write_json_new(source_record_path, source_record)
    record = {
        "case": case,
        "company_id": company_id,
        "report_year": 2025,
        "document_id": document_id,
        "source_pdf_path": str(copied_pdf.resolve()),
        "source_record_path": str(source_record_path.resolve()),
        "source_record": source_record,
        "sha256": expected_sha,
        "accepted_input_path": str(original.resolve()),
        "accepted_input_sha256": actual_sha,
        "copy_is_byte_identical": True,
        "registration_time_utc": _utc_now(),
    }
    _write_json_new(attempt / "sources" / f"{case}-registration.json", record)
    return record


def _punctuation_span(raw_text: str, keyword: str) -> str | None:
    position = raw_text.find(keyword)
    if position < 0:
        return None
    left = max(
        raw_text.rfind("。", 0, position),
        raw_text.rfind("\n", 0, position),
    ) + 1
    ends = [raw_text.find(mark, position) for mark in ("。", "\n")]
    ends = [value for value in ends if value >= 0]
    right = min(ends) + (1 if ends and raw_text[min(ends)] == "。" else 0) if ends else len(raw_text)
    quote = raw_text[left:right].strip()
    if keyword not in quote:
        return None
    if len(quote) > 300:
        start = max(left, position - 100)
        quote = raw_text[start : min(len(raw_text), start + 300)]
    if len(quote) < 2:
        return None
    return quote


def _anchor_rows(payload: dict[str, Any]) -> list[tuple[str, int, str]]:
    rows = payload.get("fragments")
    if not isinstance(rows, list):
        raise ValueError("discovery fixture received malformed fragments")
    return [
        (row[0], row[1], row[2])
        for row in rows
        if isinstance(row, list)
        and len(row) == 3
        and isinstance(row[0], str)
        and type(row[1]) is int
        and isinstance(row[2], str)
    ]


def _find_quote(rows: list[tuple[str, int, str]], keyword: str) -> tuple[str, str] | None:
    for alias, _page, raw_text in rows:
        if "清渠园区二期" not in raw_text and "CL-25041" not in raw_text:
            continue
        quote = _punctuation_span(raw_text, keyword)
        if quote is not None:
            return alias, quote
    return None


def _finding(category: str, alias: str, quote: str) -> dict[str, Any]:
    text = {
        "设备收入确认": (
            "报告将设备收入确认与交付验收资料相连，系统整体联动仍待完成。",
            "收入确认依据与系统服务履约节点分别记录，仍需按各自合同条件核对。",
            "对照设备签收资料、验收单及对应会计记录。",
        ),
        "尾款结算安排": (
            "披露将合同尾款结算安排与全区联动验收节点相连。",
            "回款条件与设备收入确认依据不应仅凭同一表述归为一个事项。",
            "对照合同付款条款、验收记录及期后收款资料。",
        ),
        "设备与集成服务分界": (
            "报告把设备交付与另行承诺的系统集成服务分别记录。",
            "不同履约资料对应的确认时点和责任范围仍需分别理解。",
            "核对合同履约义务、现场进度及最终联动资料。",
        ),
        "设备交付签收": (
            "原文记录了该合同相关的设备交付或签收节点。",
            "单一交付记录不足以证明系统服务已完成或财务处理已独立核验。",
            "对照对应签收资料、合同节点及后续履约记录。",
        ),
        "项目履约记录": (
            "原文记录了该合同相关的履约过程或档案安排。",
            "项目留档本身不构成财务异常或履约完成的独立证明。",
            "按具体合同节点核对支持文件和后续状态。",
        ),
        "履约保函索赔披露": (
            "附注披露银行收到受益方的履约保函索赔通知，责任范围仍待核对。",
            "相关会计处理仍取决于完整协议、索赔理由及后续法律意见。",
            "核对完整保函、反担保文本、索赔理由与法律意见。",
        ),
    }[category]
    return {
        "title": category,
        "observation": text[0],
        "why_it_matters": text[1],
        "follow_up": text[2],
        "priority": "medium",
        "citations": [{"fragment_alias": alias, "quote": quote}],
    }


def _contract_anchor_findings(rows: list[tuple[str, int, str]]) -> list[tuple[str, str, str]]:
    """Derive three finding anchors from exact same-project clauses in this request."""

    anchors: list[tuple[str, str, str]] = []
    for alias, _page, raw_text in rows:
        if "清渠园区二期" not in raw_text and "CL-25041" not in raw_text:
            continue
        keyword = "清渠园区二期" if "清渠园区二期" in raw_text else "CL-25041"
        quote = _punctuation_span(raw_text, keyword)
        if quote is None:
            continue
        has_revenue = any(term in quote for term in ("设备收入", "销售收入", "交付验收单确认"))
        has_settlement = any(term in quote for term in ("尾款", "应收余额", "付款条件"))
        if has_revenue and has_settlement:
            # The same source clause explicitly distinguishes two business paths.
            # Preserve both interpretations as separate candidate members.
            anchors.append((_CATEGORIES[0], alias, quote))
            anchors.append((_CATEGORIES[1], alias, quote))
        else:
            anchors.append((_category_for_anchor(quote), alias, quote))
        if len(anchors) >= 3:
            break
    return anchors


def _category_for_anchor(quote: str) -> str:
    if any(term in quote for term in ("尾款", "应收余额", "付款条件")):
        return _CATEGORIES[1]
    if any(term in quote for term in ("系统集成", "整体联动", "整体验收", "联动验收")):
        return _CATEGORIES[2]
    if any(term in quote for term in ("设备收入", "销售收入", "交付验收单确认")):
        return _CATEGORIES[0]
    if any(term in quote for term in ("设备交付", "客户签收", "逐项签收")):
        return "设备交付签收"
    return "项目履约记录"


def _discovery_fixture(case: str, request: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    payload = json.loads(request["messages"][1]["content"])
    batch_index = payload.get("batch_index")
    if type(batch_index) is not int:
        raise ValueError("discovery fixture did not receive a numbered batch")
    rows = _anchor_rows(payload)
    if case == "clean":
        return 200, json.dumps({"findings": [], "limitations": ["仅依据本批原文；未取得独立财务核验结论。"]}, ensure_ascii=False), {
            "fixture_kind": "legal_empty_discovery",
            "batch_index": batch_index,
            "finding_count": 0,
        }

    if case == "partial" and batch_index == 6:
        return 503, "", {
            "fixture_kind": "controlled_discovery_transport_failure",
            "batch_index": batch_index,
            "finding_count": 0,
        }

    if 1 <= batch_index <= 5:
        results: list[dict[str, Any]] = []
        if batch_index == 1:
            shared = None
            for alias, _page, raw_text in rows:
                if "设备收入" in raw_text and "尾款" in raw_text:
                    quote = _punctuation_span(raw_text, "设备收入")
                    if quote is not None and "尾款" in quote:
                        shared = (alias, quote)
                        break
            if shared is None:
                raise ValueError("first discovery batch lost its accepted shared-quote pressure clause")
            results.append(_finding(_CATEGORIES[0], shared[0], shared[1]))
            results.append(_finding(_CATEGORIES[1], shared[0], shared[1]))
            third = _find_quote(rows, "系统集成")
            if third is None:
                raise ValueError("first discovery batch lacks a locatable system integration anchor")
            results.append(_finding(_CATEGORIES[2], third[0], third[1]))
        else:
            anchors = _contract_anchor_findings(rows)
            if len(anchors) != 3:
                raise ValueError(
                    f"batch {batch_index} has {len(anchors)} rather than three exact same-contract findings"
                )
            if case == "supplement":
                target_by_batch = {
                    2: _CATEGORIES[2],
                    3: "设备交付签收",
                    4: _CATEGORIES[2],
                    5: _CATEGORIES[1],
                }
                target = target_by_batch[batch_index]
                anchors = [row for row in anchors if row[0] == target][:1]
                if not anchors:
                    raise ValueError(f"supplement microcase batch {batch_index} lacks an exact {target} anchor")
            results.extend(
                _finding(category, alias, quote)
                for category, alias, quote in anchors
            )
        return 200, json.dumps({"findings": results, "limitations": []}, ensure_ascii=False), {
            "fixture_kind": "request_derived_supplement_microcase" if case == "supplement" else "request_derived_issue_findings",
            "batch_index": batch_index,
            "finding_count": len(results),
            "same_quote_distinct_matters": batch_index == 1,
            "mock_profile": "smaller request-derived candidate set for supplement reachability; synthetic response only" if case == "supplement" else "16-member issue-pressure profile",
            "citation_aliases": [row["citations"][0]["fragment_alias"] for row in results],
            "citation_sha256": [
                _sha256_bytes(row["citations"][0]["quote"].encode("utf-8")) for row in results
            ],
        }

    if batch_index == 6:
        page16_rows = [row for row in rows if row[1] == 16 and ("保函" in row[2] or "索赔" in row[2])]
        if not page16_rows:
            raise ValueError("last discovery batch lacks its page-sixteen-only accepted event")
        alias, _page, raw_text = page16_rows[0]
        quote = _punctuation_span(raw_text, "索赔") or _punctuation_span(raw_text, "保函")
        if quote is None:
            raise ValueError("page-sixteen item has no exact quote in the current request")
        finding = _finding("履约保函索赔披露", alias, quote)
        return 200, json.dumps({"findings": [finding], "limitations": []}, ensure_ascii=False), {
            "fixture_kind": "request_derived_late_unique_finding",
            "batch_index": batch_index,
            "finding_count": 1,
            "page": 16,
            "citation_alias": alias,
            "citation_sha256": _sha256_bytes(quote.encode("utf-8")),
        }
    raise ValueError(f"unexpected issue discovery batch index: {batch_index}")


def _group_fixture(
    request: dict[str, Any], *, global_stage: bool, business_input: dict[str, Any] | None = None,
) -> str:
    payload = business_input
    if payload is None:
        payload = json.loads(request["messages"][1]["content"])
    if not isinstance(payload, dict):
        raise ValueError("grouping fixture input is not an object")
    id_field = "issue_id" if global_stage else "finding_id"
    target_field = "target_issue_id" if global_stage else "target_finding_id"
    schema = "annual_issue_global_result_v1" if global_stage else "annual_issue_group_shard_result_v1"
    if global_stage:
        ids = payload.get("input_issue_ids")
        pool = {row.get("finding_id"): row for row in payload.get("candidate_pool", []) if isinstance(row, dict)}
        representative_by_issue = {
            row.get("issue_id"): row.get("representative_finding_id")
            for row in payload.get("issue_members", []) if isinstance(row, dict)
        }
        title_by_id = {issue_id: pool.get(member, {}).get("title") for issue_id, member in representative_by_issue.items()}
        keep_reason = "distinct_across_shards"
    else:
        ids = payload.get("input_member_finding_ids")
        pool = {row.get("finding_id"): row for row in payload.get("candidate_pool", []) if isinstance(row, dict)}
        title_by_id = {finding_id: pool.get(finding_id, {}).get("title") for finding_id in ids or []}
        keep_reason = "distinct_issue"
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
        raise ValueError("grouping fixture received an empty or malformed closed input set")
    category_for = lambda item: item if item == "履约保函索赔披露" else item if item in _CATEGORIES else f"独立事项:{item}"
    grouped: dict[str, list[str]] = {}
    for item_id in ids:
        grouped.setdefault(category_for(title_by_id.get(item_id)), []).append(item_id)
    decisions = []
    for category in sorted(grouped):
        rows = sorted(grouped[category])
        retained = rows[0]
        for item_id in rows:
            if item_id == retained:
                decisions.append({
                    id_field: item_id, "disposition": "retain", target_field: None,
                    "reason_code": keep_reason, "reason_evidence_id": None, "reason_quote": None,
                })
            else:
                decisions.append({
                    id_field: item_id, "disposition": "merge", target_field: retained,
                    "reason_code": "same_observed_event", "reason_evidence_id": None, "reason_quote": None,
                })
    return json.dumps({"schema_version": schema, "decisions": decisions}, ensure_ascii=False)


def _supplement_fixture(request: dict[str, Any], *, business_input: dict[str, Any] | None = None) -> str:
    payload = business_input
    if payload is None:
        payload = json.loads(request["messages"][1]["content"])
    assignments = payload.get("evidence_assignments")
    if not isinstance(assignments, list):
        raise ValueError("supplement fixture input lacks typed assignments")
    if len(assignments) < 3:
        raise ValueError("supplement microcase needs at least three actual request-derived role assignments")
    roles = ("context", "possible_explanation", "unknown")
    reasons = {
        "context": "context_only",
        "possible_explanation": "plausible_alternative",
        "unknown": "relationship_unclear",
    }
    result = [
        {
            "issue_id": row["issue_id"],
            "evidence_id": row["evidence_id"],
            "role": roles[index % len(roles)],
            "reason_code": reasons[roles[index % len(roles)]],
        }
        for index, row in enumerate(assignments)
    ]
    return json.dumps({"schema_version": "annual_issue_supplement_result_v1", "roles": result}, ensure_ascii=False)


def _review_fixture(case: str, request: dict[str, Any]) -> str:
    envelope = json.loads(request["messages"][1]["content"])
    review_input = decode_annual_review_v3_wire(envelope)
    limits = review_input.get("limitations_required", [])
    if not isinstance(limits, list) or any(not isinstance(item, str) for item in limits):
        raise ValueError("decoded review request has malformed required limitations")
    if case == "clean":
        return json.dumps({
            "assessment": "insufficient_evidence",
            "summary": "全文扫描没有返回合格发现，但现有基础分析不足以支持不存在事项的结论。",
            "reasons": [],
            "follow_up_items": [],
            "limitations": list(limits),
        }, ensure_ascii=False)

    hierarchy = review_input.get("issue_hierarchy")
    issue_nodes = hierarchy.get("issues") if isinstance(hierarchy, dict) else None
    candidates = hierarchy.get("candidate_pool") if isinstance(hierarchy, dict) else None
    if not isinstance(issue_nodes, list) or not isinstance(candidates, list) or not issue_nodes:
        raise ValueError("decoded review request does not expose complete issue members")
    late_candidates = [
        row for row in candidates
        if isinstance(row, dict) and row.get("title") == "履约保函索赔披露"
        and isinstance(row.get("finding_id"), str)
    ]
    late_finding_ids = {row["finding_id"] for row in late_candidates}
    wanted = [
        row for row in issue_nodes
        if isinstance(row, dict) and late_finding_ids.intersection(row.get("member_finding_ids", []))
    ]
    issue_nodes = [row for row in issue_nodes if isinstance(row, dict) and isinstance(row.get("issue_id"), str)]
    chosen = sorted(wanted or issue_nodes, key=lambda row: row["issue_id"])[0]
    issue_id = chosen["issue_id"]
    additional = []
    if not limits:
        additional = ["模型发现、归并和检索角色均未经过独立事实核验。"]
    return json.dumps({
        "assessment": "prioritize_review",
        "summary": "已接收的原文线索仍需人工核查；扫描覆盖、事项归并和补证角色不构成独立验证。",
        "reasons": [{
            "text": "该事项包含待核对的原文披露，建议回到合同、法律资料和后续处理记录逐项确认。",
            "evidence_ids": [issue_id],
        }],
        "follow_up_items": [{
            "object": issue_id,
            "action": "人工核对完整协议、相关业务资料及后续处理记录。",
            "evidence_ids": [issue_id],
        }],
        "limitations": [*limits, *additional],
    }, ensure_ascii=False)


def _fixed_investigation_fixture(request: dict[str, Any]) -> str:
    payload = json.loads(request["messages"][1]["content"])
    candidate = payload.get("candidate", {})
    evidence = payload.get("narrative_evidence", [])
    evidence_id = evidence[0].get("evidence_id") if evidence and isinstance(evidence[0], dict) else None
    return json.dumps({
        "signal_id": candidate.get("signal_id"),
        "explanation": "原因无法确认：当前片段没有提供足以确认形成原因的资料。",
        "alternative_explanations": [],
        "limitations": ["仅依据当前输入的少量年报片段，原因仍待核实。"],
        "narrative_evidence_ids": [evidence_id] if evidence_id else [],
        "request_more_context": False,
    }, ensure_ascii=False)


class _LoopbackFixture:
    def __init__(self, *, case: str, output_dir: Path) -> None:
        self.case = case
        self.output_dir = output_dir / "loopback"
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - standard handler API
                length = int(self.headers.get("Content-Length", "0"))
                raw_request = self.rfile.read(length)
                try:
                    request = json.loads(raw_request.decode("utf-8"))
                    status, response_text, category, fixture_meta = owner._respond(
                        request, raw_request_sha256=_sha256_bytes(raw_request)
                    )
                    response_body = (
                        json.dumps({"error": {"message": "controlled offline failure", "type": "server_error"}}, separators=(",", ":")).encode("utf-8")
                        if status != 200
                        else json.dumps({
                            "id": f"r4-local-{len(owner.events) + 1}",
                            "object": "chat.completion",
                            "model": "r4-offline-semantic-fixture",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    )
                except Exception as exc:
                    status = 500
                    category = "fixture_handler_error"
                    fixture_meta = {"error_type": type(exc).__name__, "error": str(exc)}
                    response_body = json.dumps({"error": {"message": "offline fixture contract error", "type": "server_error"}}, separators=(",", ":")).encode("utf-8")
                ordinal = owner._save_exchange(
                    raw_request=raw_request,
                    response_body=response_body,
                    request_path=self.path,
                    status=status,
                    request_headers={
                        "content_type": self.headers.get("Content-Type"),
                        "content_length": self.headers.get("Content-Length"),
                        "authorization_present": bool(self.headers.get("Authorization")),
                        "authorization": "[redacted]" if self.headers.get("Authorization") else None,
                    },
                    category=category,
                    fixture_meta=fixture_meta,
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(response_body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                with owner._lock:
                    owner.events[ordinal - 1]["response_sent"] = True

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> _LoopbackFixture:
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        _write_json_new(self.output_dir / "exchange-index.json", {
            "schema_version": "r4_loopback_exchange_index_v1",
            "kind": "offline_local_mock_only",
            "case": self.case,
            "external_network": False,
            "request_count": len(self.events),
            "requests": self.events,
        })

    def _respond(
        self, request: dict[str, Any], *, raw_request_sha256: str,
    ) -> tuple[int, str, str, dict[str, Any]]:
        messages = request.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            raise ValueError("Chat request did not contain system and user messages")
        system = messages[0].get("content", "") if isinstance(messages[0], dict) else ""
        user = messages[1].get("content", "") if isinstance(messages[1], dict) else ""
        if "annual_discovery_v4" in system:
            status, response, meta = _discovery_fixture(self.case, request)
            return status, response, "annual_discovery", meta
        try:
            user_value = json.loads(user)
        except (TypeError, json.JSONDecodeError):
            user_value = None
        schema = user_value.get("schema_version") if isinstance(user_value, dict) else None
        if schema == _ANNUAL_WIRE_MODULE.ISSUE_INPUT_WIRE_SCHEMA:
            business_input = _ANNUAL_WIRE_MODULE.decode_annual_issue_input_wire(user_value)
            business_schema = business_input.get("schema_version")
            wire_meta = {
                "schema_version": business_schema,
                "wire_schema_version": schema,
                "business_schema_version": business_schema,
                "decoder_module": _ANNUAL_WIRE_MODULE.__name__,
                "decoder_module_path": _ANNUAL_WIRE_MODULE_PATH.relative_to(ROOT).as_posix(),
                "decoder_module_sha256_at_import": _ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT,
                "request_body_sha256": raw_request_sha256,
                "business_input_sha256": _business_input_sha256(business_input),
            }
            if business_schema == "annual_issue_group_shard_input_v1":
                return 200, _group_fixture(request, global_stage=False, business_input=business_input), "annual_issue_grouping", wire_meta
            if business_schema == "annual_issue_global_input_v1":
                return 200, _group_fixture(request, global_stage=True, business_input=business_input), "annual_issue_global", wire_meta
            if business_schema == "annual_issue_supplement_input_v1":
                return 200, _supplement_fixture(request, business_input=business_input), "annual_issue_supplement", {
                    **wire_meta,
                    "assignment_count": len(business_input.get("evidence_assignments", [])),
                }
            raise ValueError(f"typed issue input decoder returned an unexpected business schema: {business_schema!r}")
        if isinstance(user_value, dict) and user_value.get("schema_version") == _ANNUAL_WIRE_MODULE.V3_WIRE_SCHEMA:
            review_input = _ANNUAL_WIRE_MODULE.decode_annual_review_v3_wire(user_value)
            return 200, _review_fixture(self.case, request), "annual_review", {
                "wire_schema_version": _ANNUAL_WIRE_MODULE.V3_WIRE_SCHEMA,
                "projection_version": _ANNUAL_WIRE_MODULE.V3_INPUT_PROJECTION_VERSION,
                "decoder_module": _ANNUAL_WIRE_MODULE.__name__,
                "decoder_module_path": _ANNUAL_WIRE_MODULE_PATH.relative_to(ROOT).as_posix(),
                "decoder_module_sha256_at_import": _ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT,
                "decoded_input_sha256": _business_input_sha256(review_input),
                "request_body_sha256": raw_request_sha256,
            }
        if "annual_investigation_v1" in system or "候选线索整理谨慎的文字解释" in system:
            return 200, _fixed_investigation_fixture(request), "annual_investigation", {"fixture_kind": "request_derived_abstaining_interpretation"}
        raise ValueError("unexpected production model stage; request preserved for diagnosis")

    def _save_exchange(
        self,
        *,
        raw_request: bytes,
        response_body: bytes,
        request_path: str,
        status: int,
        request_headers: dict[str, Any],
        category: str,
        fixture_meta: dict[str, Any],
    ) -> int:
        with self._lock:
            ordinal = len(self.events) + 1
            base = f"exchange-{ordinal:04d}"
            request_name = base + ".request.raw.json"
            response_name = base + ".response.raw.json"
            _write_new(self.output_dir / request_name, raw_request)
            _write_new(self.output_dir / response_name, response_body)
            metadata = {
                "ordinal": ordinal,
                "case": self.case,
                "category": category,
                "path": request_path,
                "status": status,
                "request_headers": request_headers,
                "request_body_file": request_name,
                "request_body_sha256": _sha256_bytes(raw_request),
                "response_body_file": response_name,
                "response_body_sha256": _sha256_bytes(response_body),
                "fixture_meta": fixture_meta,
                "captured_at_utc": _utc_now(),
            }
            _write_json_new(self.output_dir / (base + ".metadata.json"), metadata)
            self.events.append(metadata)
            return ordinal


def _copy_audit_originals(run_dir: Path, case_dir: Path) -> list[dict[str, Any]]:
    destination = case_dir / "audited-chat-originals"
    destination.mkdir(parents=True, exist_ok=False)
    result: list[dict[str, Any]] = []
    if not run_dir.is_dir():
        return result
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name not in {"request.json", "response.json", "failure.json"}:
            continue
        relative = path.relative_to(run_dir)
        copied = destination / relative
        _write_new(copied, path.read_bytes())
        result.append({
            "run_relative_path": relative.as_posix(),
            "attempt_relative_path": copied.relative_to(case_dir).as_posix(),
            "sha256": _sha256_file(path),
            "byte_length": path.stat().st_size,
        })
    _write_json_new(case_dir / "audited-chat-originals-index.json", {
        "schema_version": "r4_original_audited_chat_files_v1",
        "files": result,
    })
    return result


def _read_call_gate_read_only(run_dir: Path) -> dict[str, Any]:
    candidates = [run_dir / "call-gate.sqlite3", run_dir / "call_ledger.sqlite3"]
    database = next((path for path in candidates if path.is_file()), None)
    if database is None:
        return {"status": "missing", "database": None, "tables": {}}
    uri = f"file:{database.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        tables = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
            tables[name] = {"count": count, "columns": columns}
        return {"status": "read_only_opened", "database": str(database.resolve()), "tables": tables}
    finally:
        connection.close()


def _issue_ids_for_category(pipeline: dict[str, Any], category: str) -> list[str]:
    candidate_titles = {
        row.get("finding_id"): row.get("title")
        for row in pipeline.get("candidates", [])
        if isinstance(row, dict)
    }
    result = []
    for issue in pipeline.get("issues", []):
        if not isinstance(issue, dict):
            continue
        if any(candidate_titles.get(member) == category for member in issue.get("member_finding_ids", [])):
            result.append(issue.get("issue_id"))
    return [item for item in result if isinstance(item, str)]


def _verify_supplement_locations(
    original_pdf: Path,
    document_id: str,
    expected_sha: str,
    supplements: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed = parse_text_pdf(original_pdf, document_id)
    if parsed.source_sha256 != expected_sha:
        raise AssertionError("fresh parser did not reproduce the accepted PDF SHA")
    block_map = {
        (page.page_number, block.block_index): block
        for page in parsed.pages for block in page.blocks
    }
    checked = []
    for item in supplements:
        page = item.get("pdf_page")
        block_index = item.get("block_index")
        block = block_map.get((page, block_index))
        if block is None:
            raise AssertionError(f"supplement block is absent from fresh same-SHA parse: {page}/{block_index}")
        start = item.get("start_char")
        end = item.get("end_char")
        raw_text = item.get("raw_text")
        if type(start) is not int or type(end) is not int or not isinstance(raw_text, str):
            raise AssertionError("supplement character range is malformed")
        if start < 0 or end <= start or end > len(block.text) or block.text[start:end] != raw_text:
            raise AssertionError(f"supplement character span fails fresh same-SHA recheck: {page}/{block_index}")
        expected_bbox = {"x0": block.bbox.x0, "y0": block.bbox.y0, "x1": block.bbox.x1, "y1": block.bbox.y1}
        if item.get("bbox") != expected_bbox:
            raise AssertionError(f"supplement bbox differs from fresh same-SHA block: {page}/{block_index}")
        if item.get("source_sha256") != expected_sha:
            raise AssertionError("supplement source hash differs from accepted PDF")
        checked.append({
            "evidence_id": item.get("evidence_id"),
            "pdf_page": page,
            "block_index": block_index,
            "start_char": start,
            "end_char": end,
            "raw_text_sha256": _sha256_bytes(raw_text.encode("utf-8")),
            "bbox": expected_bbox,
        })
    return {
        "schema_version": "r4_fresh_source_location_check_v1",
        "source_sha256": expected_sha,
        "checked_supplement_count": len(checked),
        "checked_supplements": checked,
    }


def _run_cli_case(
    attempt: Path,
    source: dict[str, Any],
    *,
    case: str,
    expected_runtime_fingerprint: str | None,
) -> dict[str, Any]:
    case_dir = attempt / "scenarios" / case
    case_dir.mkdir(parents=True, exist_ok=False)
    observer_dir = case_dir / "actual-runtime-identity-observer"
    capture_dir = case_dir / "actual-runtime-identity-capture"
    observer_dir.mkdir(parents=True, exist_ok=False)
    capture_dir.mkdir(parents=True, exist_ok=False)
    observer_source = _ACTUAL_CLI_IDENTITY_OBSERVER.encode("utf-8")
    observer_path = observer_dir / "sitecustomize.py"
    _write_new(observer_path, observer_source)
    run_id = f"annual-analysis-{uuid.uuid4()}"
    argv = [
        sys.executable,
        str(ROOT / "scripts" / "analyze_annual.py"),
        "--source-pdf", source["source_pdf_path"],
        "--company-id", source["company_id"],
        "--report-year", str(source["report_year"]),
        "--document-id", source["document_id"],
        "--expected-sha256", source["sha256"],
        "--run-id", run_id,
        "--with-model",
        "--request-mode", "model_investigation",
        "--source-record", source["source_record_path"],
    ]
    if expected_runtime_fingerprint:
        argv.extend(["--expected-runtime-fingerprint", expected_runtime_fingerprint])
    started_at = _utc_now()
    started = time.monotonic()
    process_error: str | None = None
    try:
        with _LoopbackFixture(case=case, output_dir=case_dir) as server:
            environment = _fresh_environment(model_url=server.base_url, source_sha=source["sha256"])
            environment["PYTHONPATH"] = str(observer_dir.resolve())
            environment["FINTRACE_R4_ACTUAL_CLI_IDENTITY_CAPTURE_DIR"] = str(capture_dir.resolve())
            try:
                completed = subprocess.run(
                    argv,
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    check=False,
                    timeout=360,
                )
                stdout = completed.stdout
                stderr = completed.stderr
                exit_code: int | None = completed.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or b""
                stderr = exc.stderr or b""
                exit_code = None
                process_error = "cli_timeout_after_360_seconds"
        captured_identity_records = []
        for identity_path in sorted(capture_dir.glob("actual-cli-runtime-identity-*.json")):
            captured_identity_records.append(_read_json(identity_path))
        _write_json_new(case_dir / "actual-runtime-identity-capture-index.json", {
            "schema_version": "r4_actual_cli_runtime_identity_capture_v1",
            "observer_source_path": str(observer_path.resolve()),
            "observer_source_sha256": _sha256_bytes(observer_source),
            "expected_runtime_fingerprint": expected_runtime_fingerprint,
            "capture_count": len(captured_identity_records),
            "captures": captured_identity_records,
        })
        _write_new(case_dir / "stdout.raw.txt", stdout)
        _write_new(case_dir / "stderr.raw.txt", stderr)
    except Exception as exc:
        stdout = b""
        stderr = str(exc).encode("utf-8", errors="replace")
        exit_code = None
        process_error = f"{type(exc).__name__}: {exc}"
        _write_new(case_dir / "stdout.raw.txt", stdout)
        _write_new(case_dir / "stderr.raw.txt", stderr)
    ended = time.monotonic()
    elapsed = ended - started
    combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    match = RUN_ID_RE.search(combined)
    printed_run_id = match.group(1) if match else None
    _write_json_new(case_dir / "argv.json", {
        "argv": argv,
        "working_directory": str(ROOT),
        "shell": False,
        "artifacts_root_argument": "omitted; CLI default must resolve to repository ROOT/artifacts",
        "expected_run_id": run_id,
        "printed_run_id": printed_run_id,
        "request_mode": "model_investigation",
        "with_model": True,
        "source_record_path": source["source_record_path"],
        "expected_runtime_fingerprint": expected_runtime_fingerprint,
        "actual_runtime_identity_observer": {
            "source_path": str(observer_path.resolve()),
            "source_sha256": _sha256_bytes(observer_source),
            "capture_directory": str(capture_dir.resolve()),
            "captured_record_count": len(captured_identity_records) if "captured_identity_records" in locals() else 0,
        },
        "environment_policy": {
            "provider": "127.0.0.1 loopback only",
            "model_name": "r4-offline-semantic-fixture",
            "api_key": "synthetic local-only key; value omitted from evidence",
            "inherited_MODEL_values_read": False,
            "real_model_config_file_read": False,
            "proxy_environment_removed": True,
        },
    })
    _write_json_new(case_dir / "timing.json", {
        "started_at_utc": started_at,
        "ended_at_utc": _utc_now(),
        "started_monotonic": started,
        "ended_monotonic": ended,
        "elapsed_seconds": round(elapsed, 6),
        "exit_code": exit_code,
        "process_error": process_error,
    })
    run_dir = ROOT / "artifacts" / "runs" / run_id
    report_dir = ROOT / "artifacts" / "reports" / run_id
    audited_originals = _copy_audit_originals(run_dir, case_dir)
    gate = _read_call_gate_read_only(run_dir)
    _write_json_new(case_dir / "call-gate-read-only.json", gate)
    return {
        "case": case,
        "case_dir": str(case_dir.resolve()),
        "run_id": printed_run_id or run_id,
        "expected_run_id": run_id,
        "exit_code": exit_code,
        "process_error": process_error,
        "elapsed_seconds": round(elapsed, 6),
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "run_dir": run_dir,
        "report_dir": report_dir,
        "call_gate": gate,
        "audited_originals": audited_originals,
        "expected_runtime_fingerprint": expected_runtime_fingerprint,
        "actual_runtime_identity_captures": captured_identity_records if "captured_identity_records" in locals() else [],
    }


def _require(condition: bool, message: str, problems: list[str]) -> None:
    if not condition:
        problems.append(message)


def _check_final_review_boundary(
    *,
    case: str,
    case_dir: Path,
    report: dict[str, Any],
    investigation: dict[str, Any],
    pipeline: dict[str, Any],
    review: dict[str, Any],
    late_issue_ids: set[str],
    review_http_count: int,
    review_admission_count: int,
    problems: list[str],
) -> dict[str, Any]:
    """Accept a sourced review or prove that the complete Chat was honestly size-abstained."""

    if review.get("status") == "completed":
        payload = None
        try:
            payload = _first_request_payload(case_dir, "annual_review")
        except Exception as exc:
            problems.append(f"{case}: completed final review has no decodable original request: {type(exc).__name__}: {exc}")
        hierarchy = payload.get("issue_hierarchy") if isinstance(payload, dict) else None
        issues = hierarchy.get("issues") if isinstance(hierarchy, dict) else None
        candidates = hierarchy.get("candidate_pool") if isinstance(hierarchy, dict) else None
        allowed_ids = {
            item.get(key)
            for rows, key in ((issues, "issue_id"), (candidates, "finding_id"))
            if isinstance(rows, list)
            for item in rows if isinstance(item, dict) and isinstance(item.get(key), str)
        }
        cited_ids = {
            evidence_id
            for row in review.get("reasons", []) if isinstance(row, dict)
            for evidence_id in row.get("evidence_ids", []) if isinstance(evidence_id, str)
        }
        _require(review_http_count > 0 and review_admission_count > 0, f"{case}: completed final review has no matching provider admission", problems)
        _require(bool(cited_ids), f"{case}: completed final review has no source-path citation", problems)
        _require(cited_ids.issubset(allowed_ids), f"{case}: completed final review cited an ID outside the actual request whitelist", problems)
        _require(bool(cited_ids & late_issue_ids), f"{case}: completed final review did not cite the retained late-unique issue path", problems)
        return {
            "mode": "completed_with_request_whitelisted_late_issue_citation",
            "review_http_attempts": review_http_count,
            "review_admissions": review_admission_count,
            "cited_ids": sorted(cited_ids),
            "request_whitelist_ids": sorted(allowed_ids),
        }

    if review.get("status") == "abstained" and review.get("reason") == "annual_review_v3_request_budget_exceeded":
        _require(review.get("model_called") is False and review.get("call_count") == 0 and review.get("call_attempt_count") == 0 and review.get("audit_artifacts") == [], f"{case}: size-abstained review was represented as a provider call", problems)
        _require(not review.get("assessment") and not review.get("reasons"), f"{case}: size-abstained review exposed an unsupported assessment or opinion", problems)
        reconstruction: dict[str, Any] = {
            "case": case,
            "model": FIXTURE_MODEL_NAME,
            "provider_call_made": False,
            "provider_http_attempts": review_http_count,
            "sqlite_provider_admissions": review_admission_count,
        }
        try:
            from finagent.agents.annual_review import V3_MAX_REQUEST_CHARS, _build_review_input_v3
            from finagent.agents.annual_review_wire import encode_annual_review_v3_wire
            from finagent.llm.chat_completion import ChatMessage, serialize_chat_body

            identity = pipeline.get("source_identity")
            if not isinstance(identity, dict):
                raise ValueError("issue pipeline source_identity is missing")
            system_prompt = (ROOT / "config" / "prompts" / "annual_review_v3.md").read_text(encoding="utf-8")
            review_input, _allowed, _refs, _no_priority = _build_review_input_v3(
                report,
                investigation,
                identity,
                request_model=FIXTURE_MODEL_NAME,
                system_prompt=system_prompt,
                enforce_budget=False,
            )
            user_content = json.dumps(
                encode_annual_review_v3_wire(review_input), ensure_ascii=False, separators=(",", ":")
            )
            body = serialize_chat_body(
                FIXTURE_MODEL_NAME,
                [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=user_content)],
            )
            body_path = case_dir / "review-request-reconstruction.raw.json"
            _write_new(body_path, body)
            char_count = len(body.decode("utf-8"))
            reconstruction.update({
                "status": "reconstructed_from_strict_loaded_report_with_production_builder",
                "body_path": body_path.name,
                "body_sha256": _sha256_bytes(body),
                "body_chars": char_count,
                "max_chars": V3_MAX_REQUEST_CHARS,
                "exceeds_budget": char_count > V3_MAX_REQUEST_CHARS,
                "prompt_sha256": _sha256_bytes(system_prompt.encode("utf-8")),
            })
            _require(char_count > V3_MAX_REQUEST_CHARS, f"{case}: reconstructed complete Chat does not exceed the production review budget", problems)
            _require(review_http_count == 0 and review_admission_count == 0, f"{case}: size-abstained review still has HTTP or SQLite call attempts", problems)
        except Exception as exc:
            reconstruction.update({"status": "failed", "exception_type": type(exc).__name__, "exception": str(exc)})
            problems.append(f"{case}: could not reconstruct the complete budget-abstained Chat: {type(exc).__name__}: {exc}")
        _write_json_new(case_dir / "review-request-reconstruction.json", reconstruction)
        return reconstruction

    problems.append(f"{case}: final review reached neither a cited completion nor a verified request-budget abstention")
    return {"mode": "invalid_final_review_boundary", "status": review.get("status"), "reason": review.get("reason")}


def _validate_issue_case(outcome: dict[str, Any], source: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    case = outcome["case"]
    case_dir = Path(outcome["case_dir"])
    run_id = outcome["run_id"]
    summary: dict[str, Any] = {"case": case, "run_id": run_id, "exit_code": outcome["exit_code"]}
    expected_exits = {"issue": {0, 2}, "supplement": {0, 2}, "clean": {0}, "partial": {2}}
    _require(outcome["exit_code"] in expected_exits[case], f"{case}: standard CLI exit was {outcome['exit_code']}; see raw stderr", problems)
    _require(run_id == outcome["expected_run_id"], f"{case}: CLI did not print its unique expected run_id", problems)
    identity_captures = outcome.get("actual_runtime_identity_captures", [])
    _require(len(identity_captures) == 1, f"{case}: actual CLI loaded runtime identity was not captured exactly once", problems)
    actual_identity_record = identity_captures[0] if len(identity_captures) == 1 and isinstance(identity_captures[0], dict) else {}
    actual_identity = actual_identity_record.get("runtime_identity") if isinstance(actual_identity_record.get("runtime_identity"), dict) else {}
    _require(runtime_identity_is_valid(actual_identity), f"{case}: actual CLI runtime identity is missing or invalid", problems)
    _require(actual_identity.get("role") == "cli", f"{case}: captured runtime identity role is not cli", problems)
    _require(actual_identity.get("source_manifest_sha256") == FROZEN_RUNTIME_SOURCE_SHA256, f"{case}: actual CLI source manifest differs from the root-approved freeze", problems)
    _require(actual_identity.get("source_file_count") == FROZEN_RUNTIME_SOURCE_FILE_COUNT, f"{case}: actual CLI source file count differs from the root-approved freeze", problems)
    _require(actual_identity.get("loaded_source_match") is True, f"{case}: actual CLI loaded functions do not match their source files", problems)
    _require(actual_identity.get("fingerprint") == outcome.get("expected_runtime_fingerprint"), f"{case}: actual CLI loaded fingerprint differs from the standard CLI identity probe", problems)
    _require(actual_identity_record.get("process_id") == actual_identity.get("process_id"), f"{case}: runtime observer process ID differs from captured CLI identity", problems)
    actual_argv = actual_identity_record.get("argv", [])
    _require(
        isinstance(actual_argv, list)
        and len(actual_argv) >= 1
        and Path(str(actual_argv[0])).resolve() == (ROOT / "scripts" / "analyze_annual.py").resolve()
        and outcome["expected_run_id"] in actual_argv
        and (not outcome.get("expected_runtime_fingerprint") or outcome["expected_runtime_fingerprint"] in actual_argv),
        f"{case}: captured runtime identity does not belong to the expected standard CLI invocation",
        problems,
    )
    _require(outcome["run_dir"].is_dir() and outcome["report_dir"].is_dir(), f"{case}: standard ROOT artifacts run/report dirs were not created", problems)
    summary["actual_cli_runtime_identity"] = actual_identity
    summary["expected_runtime_fingerprint"] = outcome.get("expected_runtime_fingerprint")
    if not outcome["run_dir"].is_dir() or not outcome["report_dir"].is_dir():
        return summary

    try:
        archive = load_annual_report(ROOT, run_id)
    except Exception as exc:
        _write_json_new(case_dir / "strict-load-error.json", {
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        })
        problems.append(f"{case}: strict load_annual_report failed: {type(exc).__name__}: {exc}")
        return summary

    report = archive.report
    investigation = report.get("model_investigation") if isinstance(report.get("model_investigation"), dict) else {}
    discovery = investigation.get("discovery") if isinstance(investigation.get("discovery"), dict) else {}
    pipeline = investigation.get("model_issue_pipeline") if isinstance(investigation.get("model_issue_pipeline"), dict) else {}
    review = investigation.get("final_review") if isinstance(investigation.get("final_review"), dict) else {}
    binding = pipeline.get("execution_binding") if isinstance(pipeline.get("execution_binding"), dict) else {}
    _require(pipeline.get("kind") == "annual_issue_pipeline_v1", f"{case}: production issue pipeline was not attached", problems)
    _require(binding.get("execution_version") == "execution_v5", f"{case}: standard CLI did not bind the issue pipeline as execution_v5", problems)
    _require(archive.manifest_summary.get("status") in {"completed", "completed_with_issues"}, f"{case}: strict reader did not expose a terminal sealed report", problems)
    _require(archive.source_pdf_bytes and _sha256_bytes(archive.source_pdf_bytes) == source["sha256"], f"{case}: strict archive source PDF hash mismatch", problems)
    _require(archive.manifest_summary.get("company_id") == source["company_id"], f"{case}: strict manifest company binding differs from the registered controlled source", problems)
    _require(archive.manifest_summary.get("report_year") == source["report_year"], f"{case}: strict manifest report year differs from the registered controlled source", problems)
    _require(archive.manifest_summary.get("document_id") == source["document_id"], f"{case}: strict manifest document binding differs from the registered controlled source", problems)
    _require(archive.manifest_summary.get("source_pdf_sha256") == source["sha256"], f"{case}: strict manifest source hash differs from the accepted PDF identity", problems)
    _require(
        archive.manifest_summary.get("source_pdf_archive_path") == f"runs/{run_id}/source.pdf",
        f"{case}: strict manifest source path does not resolve inside this standard run",
        problems,
    )
    _require(archive.snapshot is not None, f"{case}: strict annual report reader did not expose verified checkpoint seal metadata", problems)
    _require(discovery.get("prompt_version") == "annual_discovery_v4", f"{case}: discovery did not use the current prompt", problems)
    contract = discovery.get("execution_contract") if isinstance(discovery.get("execution_contract"), dict) else {}
    contract_binding = contract.get("binding") if isinstance(contract.get("binding"), dict) else {}
    _require(contract_binding.get("plan_version") == "annual_discovery_plan_v3", f"{case}: compact v3 discovery plan was not used", problems)

    coverage = discovery.get("coverage") if isinstance(discovery.get("coverage"), dict) else {}
    requests = [row for row in _read_json(case_dir / "loopback" / "exchange-index.json").get("requests", []) if isinstance(row, dict)]
    stage_counts: dict[str, int] = {}
    for row in requests:
        category = row.get("category")
        if isinstance(category, str):
            stage_counts[category] = stage_counts.get(category, 0) + 1
    stage_admissions = _ledger_stage_counts(outcome["call_gate"])
    issue_wire_checks = _verify_loopback_issue_input_wires(
        case_dir,
        requests,
        pipeline,
        run_id,
        outcome["audited_originals"],
        problems,
    )
    review_wire_audit_check = _verify_loopback_review_v4(
        case=case,
        case_dir=case_dir,
        requests=requests,
        report=report,
        investigation=investigation,
        review=review,
        audited_originals=outcome["audited_originals"],
        stage_admissions=stage_admissions,
        problems=problems,
    )
    _write_json_new(case_dir / "loopback-issue-input-wire-check.json", {
        "schema_version": "r4_loopback_issue_input_wire_check_v1",
        "decoder_module": _ANNUAL_WIRE_MODULE.__name__,
        "decoder_module_path": _ANNUAL_WIRE_MODULE_PATH.relative_to(ROOT).as_posix(),
        "decoder_module_sha256_at_import": _ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT,
        "checked_request_count": len(issue_wire_checks),
        "checked_requests": issue_wire_checks,
    })
    audits = outcome["audited_originals"]
    _require(stage_counts.get("annual_issue_grouping", 0) + stage_counts.get("annual_issue_global", 0) + stage_counts.get("annual_issue_supplement", 0) <= 8, f"{case}: group/global/supplement loopback attempts exceeded the per-run cap of eight", problems)
    _require(stage_admissions.get("annual_issue_grouping", 0) <= 8, f"{case}: SQLite read-only stage ledger shows group/global/supplement attempts above eight", problems)
    _require(stage_counts.get("annual_discovery", 0) == stage_admissions.get("annual_discovery", 0), f"{case}: HTTP discovery attempts disagree with persisted admission ledger", problems)
    _require(stage_counts.get("annual_issue_grouping", 0) + stage_counts.get("annual_issue_global", 0) + stage_counts.get("annual_issue_supplement", 0) == stage_admissions.get("annual_issue_grouping", 0), f"{case}: issue stage HTTP attempts disagree with persisted admission ledger", problems)
    _require(stage_counts.get("annual_review", 0) == stage_admissions.get("annual_review", 0), f"{case}: review HTTP attempts disagree with persisted admission ledger", problems)
    _require(bool(audits), f"{case}: no original audited request/response files were copied", problems)
    issue_stage_attempts = sum(stage_counts.get(stage, 0) for stage in (
        "annual_issue_grouping", "annual_issue_global", "annual_issue_supplement",
    ))

    summary.update({
        "status": outcome["exit_code"],
        "manifest_status": archive.manifest_summary.get("status"),
        "checkpoint_seal_verified": archive.snapshot is not None,
        "strict_source_binding": {
            "company_id": archive.manifest_summary.get("company_id"),
            "report_year": archive.manifest_summary.get("report_year"),
            "document_id": archive.manifest_summary.get("document_id"),
            "source_pdf_sha256": archive.manifest_summary.get("source_pdf_sha256"),
            "source_pdf_archive_path": archive.manifest_summary.get("source_pdf_archive_path"),
            "source_record_path": source["source_record_path"],
            "strict_reader_source_bytes_sha256": _sha256_bytes(archive.source_pdf_bytes),
        },
        "issue_pipeline_version": binding.get("execution_version"),
        "discovery_status": discovery.get("status"),
        "discovery_coverage": coverage,
        "finding_count": (pipeline.get("input") or {}).get("finding_count"),
        "candidate_count": len(pipeline.get("candidates", [])) if isinstance(pipeline.get("candidates"), list) else None,
        "issue_count": len(pipeline.get("issues", [])) if isinstance(pipeline.get("issues"), list) else None,
        "shard_count": len(pipeline.get("shards", [])) if isinstance(pipeline.get("shards"), list) else None,
        "global_closure": pipeline.get("global_closure"),
        "review": review,
        "review_wire_audit_check": review_wire_audit_check,
        "loopback_issue_wire_checks": issue_wire_checks,
        "loopback_stage_attempts": stage_counts,
        "read_only_sqlite_stage_attempts": stage_admissions,
        "issue_stage_budget": {
            "initial_combined_limit": 8,
            "continuation_combined_limit": 2,
            "initial_attempts_including_http_retries": issue_stage_attempts,
            "continuation_attempts": 0,
            "resumed": False,
        },
        "audited_original_count": len(audits),
    })

    if case in {"issue", "partial", "supplement"}:
        expected_findings = {"issue": 16, "partial": 15, "supplement": 8}[case]
        _require(coverage.get("planned_batches") == 6, f"{case}: controlled issue PDF did not plan exactly six standard discovery batches", problems)
        _require((pipeline.get("input") or {}).get("finding_count") == expected_findings, f"{case}: qualifying discovery member count was not {expected_findings}", problems)
        candidates = pipeline.get("candidates") if isinstance(pipeline.get("candidates"), list) else []
        decisions = pipeline.get("member_decisions") if isinstance(pipeline.get("member_decisions"), list) else []
        input_ids = (pipeline.get("input") or {}).get("finding_ids", [])
        _require(len(candidates) == expected_findings, f"{case}: complete discovery candidate pool is missing members", problems)
        _require(len(decisions) == expected_findings, f"{case}: member disposition ledger is not one-to-one", problems)
        _require(set(input_ids) == {row.get("finding_id") for row in candidates}, f"{case}: candidate set does not close over all discovery IDs", problems)
        _require(set(input_ids) == {row.get("finding_id") for row in decisions}, f"{case}: member dispositions do not conserve all discovery IDs", problems)
        _require(not any(row.get("disposition") == "exclude" for row in decisions), f"{case}: controlled fixture unexpectedly excluded an original finding", problems)
        if case == "issue":
            group_shards = [row for row in pipeline.get("shards", []) if isinstance(row, dict) and row.get("kind", "grouping") == "grouping"]
            _require(len(group_shards) >= 2, "issue: >12 original front members did not exercise multiple local grouping shards", problems)

            by_category = {category: _issue_ids_for_category(pipeline, category) for category in _CATEGORIES}
            _require(all(by_category[category] for category in _CATEGORIES), "issue: one or more distinct business issue paths were not retained", problems)
            _require(len(by_category[_CATEGORIES[0]] + by_category[_CATEGORIES[1]]) >= 2, "issue: same-quote independent issue paths collapsed into one issue", problems)
            _require(pipeline.get("global_closure", {}).get("closed") is True or bool(pipeline.get("global_closure", {}).get("pending_issue_ids")), "issue: global member set was neither closed nor explicitly pending", problems)
            late = [
                candidate for candidate in candidates
                if candidate.get("title") == "履约保函索赔披露"
                or any(citation.get("pdf_page") == 16 for citation in candidate.get("citations", []) if isinstance(citation, dict))
            ]
            _require(len(late) == 1, "issue: the page-sixteen-only finding did not remain as one qualified member", problems)
            if late:
                late_id = late[0].get("finding_id")
                late_decision = next((row for row in decisions if row.get("finding_id") == late_id), {})
                late_issue_members = {
                    member for issue in pipeline.get("issues", []) if isinstance(issue, dict)
                    for member in issue.get("member_finding_ids", [])
                }
                pending_ids = pipeline.get("global_closure", {}).get("pending_finding_ids", [])
                _require(late_id in late_issue_members or late_id in pending_ids or late_decision.get("disposition") == "pending", "issue: late page-sixteen finding was dropped from issues and explicit pending path", problems)
                issue_ids_for_late = {
                    issue.get("issue_id") for issue in pipeline.get("issues", []) if isinstance(issue, dict)
                    and late_id in issue.get("member_finding_ids", [])
                }
                _check_final_review_boundary(
                    case=case,
                    case_dir=case_dir,
                    report=report,
                    investigation=investigation,
                    pipeline=pipeline,
                    review=review,
                    late_issue_ids={str(value) for value in issue_ids_for_late if isinstance(value, str)},
                    review_http_count=stage_counts.get("annual_review", 0),
                    review_admission_count=stage_admissions.get("annual_review", 0),
                    problems=problems,
                )

        if case == "issue":
            _require(coverage.get("complete") is True, "issue: full six-batch discovery coverage was not complete", problems)
            exact_same_quote_pair = _same_quote_distinct_issue_pair(pipeline)
            _require(exact_same_quote_pair, "issue: same exact evidence quote did not support two independently retained issue paths", problems)
            supplements = pipeline.get("supplements", []) if isinstance(pipeline.get("supplements"), list) else []
            closure = pipeline.get("global_closure", {})
            if closure.get("closed") is False:
                pending_members = closure.get("pending_issue_ids", []) or closure.get("pending_finding_ids", [])
                _require(bool(pending_members), "issue: open global closure did not identify explicit pending members", problems)
                _require(not supplements and stage_counts.get("annual_issue_supplement", 0) == 0, "issue: supplement retrieval ran while global closure remained pending", problems)
                _require(pipeline.get("retrieval_coverage", {}).get("reason") == "supplement_not_started", "issue: pending global closure did not expose why supplement retrieval was not started", problems)
            elif closure.get("closed") is True:
                _require(not closure.get("pending_issue_ids") and not closure.get("pending_finding_ids"), "issue: closed global result retained hidden pending members", problems)
                _require(set(closure.get("input_finding_ids", [])) == set(input_ids), "issue: closed global result did not account for the full discovery member set", problems)
                if supplements:
                    _require(all(item.get("verification_status") == "unverified" and item.get("independent_verification") is False and item.get("retrieval_is_verification") is False for item in supplements), "issue: completed closure promoted retrieved context above unverified material", problems)
                    location_check = _verify_supplement_locations(ISSUE_PDF, source["document_id"], source["sha256"], supplements)
                    _write_json_new(case_dir / "fresh-same-sha-location-check.json", location_check)
                    _require(location_check["checked_supplement_count"] == len(supplements), "issue: fresh PDF reparse did not check every retrieved item", problems)
            else:
                _require(False, "issue: global closure was neither explicitly closed nor pending", problems)
        elif case == "supplement":
            _require(coverage.get("complete") is True, "supplement: full-source six-batch discovery coverage was not complete", problems)
            batch_counts = [row.get("accepted_findings") for row in coverage.get("batch_results", []) if isinstance(row, dict)]
            _require(batch_counts == [3, 1, 1, 1, 1, 1], "supplement: the separately labeled smaller request-derived output profile changed", problems)
            group_shards = [row for row in pipeline.get("shards", []) if isinstance(row, dict) and row.get("kind", "grouping") == "grouping"]
            _require(bool(group_shards), "supplement: the smaller case did not enter production issue grouping", problems)
            closure = pipeline.get("global_closure", {})
            late = [
                candidate for candidate in candidates
                if candidate.get("title") == "履约保函索赔披露"
                or any(citation.get("pdf_page") == 16 for citation in candidate.get("citations", []) if isinstance(citation, dict))
            ]
            _require(len(late) == 1, "supplement: the page-sixteen-only finding did not remain a single qualified member", problems)
            supplements = pipeline.get("supplements", []) if isinstance(pipeline.get("supplements"), list) else []
            same_quote_pair = _same_quote_distinct_issue_pair(pipeline)
            _require(same_quote_pair, "supplement: same exact source quote did not preserve two separate issue paths", problems)
            if closure.get("closed") is False:
                pending_members = closure.get("pending_issue_ids", []) or closure.get("pending_finding_ids", [])
                _require(bool(pending_members), "supplement: open global closure did not identify explicit pending members", problems)
                _require(not supplements and stage_counts.get("annual_issue_supplement", 0) == 0, "supplement: retrieval ran while global closure remained pending", problems)
                _require(pipeline.get("retrieval_coverage", {}).get("reason") == "supplement_not_started", "supplement: pending global closure did not expose why retrieval was not started", problems)
                summary["supplement_boundary"] = {
                    "state": "pending_global_closure",
                    "pending_member_count": len(pending_members),
                    "retrieval_attempts": stage_counts.get("annual_issue_supplement", 0),
                    "retrieval_coverage": pipeline.get("retrieval_coverage"),
                }
                problems.append("supplement: global closure remained pending, so required three-role cross-section source evidence was not reached")
            elif closure.get("closed") is True:
                _require(not closure.get("pending_issue_ids") and not closure.get("pending_finding_ids"), "supplement: closed global result retained hidden pending members", problems)
                _require(set(closure.get("input_finding_ids", [])) == set(input_ids), "supplement: global closure did not account for the full discovery member set", problems)
                _require(not closure.get("excluded_finding_ids") and not closure.get("excluded_member_evidence"), "supplement: the fixture excluded a discovery member", problems)
                _require(all(row.get("disposition") in {"retain", "merge"} for row in decisions), "supplement: a member lacked a completed retain/merge path", problems)
                _require(len(pipeline.get("issues", [])) >= 4, "supplement: independent source-backed issue paths were collapsed too aggressively", problems)
                _require(stage_counts.get("annual_issue_grouping", 0) >= 1, "supplement: actual production grouping call was not captured", problems)
                _require(stage_counts.get("annual_issue_supplement", 0) >= 1, "supplement: actual production supplement call was not captured", problems)
                supplement_checks = [
                    row for row in issue_wire_checks
                    if row.get("category") == "annual_issue_supplement"
                ]
                assignment_fingerprints = {
                    value
                    for row in supplement_checks
                    for value in row.get("business_assignment_sha256s", [])
                    if isinstance(value, str)
                }
                _require(len(assignment_fingerprints) == 20, "supplement: strict-decoded production supplement inputs did not conserve all twenty distinct role assignments", problems)
                unique_source_blocks = {
                    (item.get("pdf_page"), item.get("block_index"))
                    for item in supplements if isinstance(item, dict)
                }
                _require(len(unique_source_blocks) == 17, "supplement: production output did not preserve the accepted seventeen unique source blocks", problems)
                roles = [
                    row.get("role") for item in supplements if isinstance(item, dict)
                    for row in item.get("issue_roles", []) if isinstance(row, dict)
                ]
                _require({"context", "possible_explanation", "unknown"}.issubset(set(roles)), "supplement: all three distinct relation roles were not retained", problems)
                _require(all(item.get("verification_status") == "unverified" and item.get("independent_verification") is False and item.get("retrieval_is_verification") is False for item in supplements), "supplement: retrieved context was promoted above unverified material", problems)
                _require(len({item.get("section") for item in supplements if isinstance(item, dict)}) >= 2, "supplement: selected evidence did not cross section context", problems)
                location_check = _verify_supplement_locations(ISSUE_PDF, source["document_id"], source["sha256"], supplements)
                _write_json_new(case_dir / "fresh-same-sha-location-check.json", location_check)
                _require(location_check["checked_supplement_count"] == len(supplements), "supplement: fresh PDF reparse did not check every retrieved item", problems)
                late_issue_ids = {
                    issue.get("issue_id") for issue in pipeline.get("issues", []) if isinstance(issue, dict)
                    and late and late[0].get("finding_id") in issue.get("member_finding_ids", [])
                }
                _require(bool(late_issue_ids), "supplement: global member closure lost the page-sixteen issue path", problems)
                _check_final_review_boundary(
                    case=case,
                    case_dir=case_dir,
                    report=report,
                    investigation=investigation,
                    pipeline=pipeline,
                    review=review,
                    late_issue_ids={str(value) for value in late_issue_ids if isinstance(value, str)},
                    review_http_count=stage_counts.get("annual_review", 0),
                    review_admission_count=stage_admissions.get("annual_review", 0),
                    problems=problems,
                )
                summary["supplement_boundary"] = {
                    "state": "global_members_closed",
                    "pipeline_status": pipeline.get("status"),
                    "assignment_count": len(assignment_fingerprints),
                    "returned_evidence_span_count": len(supplements),
                    "unique_source_block_count": len(unique_source_blocks),
                    "roles": sorted(set(roles)),
                    "source_location_check": location_check,
                }
            else:
                problems.append("supplement: global closure state is neither closed nor explicitly pending")
        else:
            closure = pipeline.get("global_closure", {})
            _require(pipeline.get("status") == "pending", "partial: incomplete discovery was not left pending", problems)
            _require(closure.get("closed") is False, "partial: incomplete discovery was represented as globally closed", problems)
            _require(set(closure.get("pending_finding_ids", [])) == set(input_ids), "partial: known findings are not all explicitly pending after failed discovery", problems)
            _require(all(row.get("disposition") == "pending" for row in decisions), "partial: a disposition was finalized despite incomplete discovery coverage", problems)
            _require(coverage.get("complete") is False, "partial: incomplete discovery was represented as complete", problems)
            _require((coverage.get("failed_batches", 0) + coverage.get("unattempted_batches", 0)) >= 1, "partial: failed/unattempted discovery was not exposed in coverage", problems)
            _require(review.get("assessment") != "no_priority_issue_identified_within_scope", "partial: review claimed no priority despite unknown scan coverage", problems)
            required = (decode_annual_review_v3_wire(_first_request_payload(case_dir, "annual_review"))).get("limitations_required", []) if stage_counts_for_case(case_dir).get("annual_review", 0) else []
            _require(all(marker in review.get("limitations", []) for marker in required), "partial: review omitted required unknown-coverage limitation markers", problems)

    if case == "clean":
        _require(coverage.get("planned_batches") == 1, "clean: legal clean fixture did not use one standard discovery batch", problems)
        _require(coverage.get("complete") is True, "clean: empty finding set lacks complete discovery coverage", problems)
        _require((pipeline.get("input") or {}).get("finding_count") == 0, "clean: pipeline padded the legal empty discovery with findings", problems)
        _require(pipeline.get("candidates") == [] and pipeline.get("member_decisions") == [], "clean: empty discovery manufactured candidate/member paths", problems)
        closure = pipeline.get("global_closure", {})
        _require(closure.get("closed") is True, "clean: empty known finding set was not closed", problems)
        _require(closure.get("no_priority_allowed") is False, "clean: empty finding set incorrectly created no-priority eligibility without verified financial comparability", problems)
        _require(review.get("assessment") != "no_priority_issue_identified_within_scope", "clean: model review asserted no-priority without eligibility", problems)
        _require(review.get("assessment") == "insufficient_evidence", "clean: legal empty scan did not remain an insufficient-evidence result", problems)

    return summary


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object at {path}")
    return value


def _stage_counts_for_events(case_dir: Path) -> dict[str, int]:
    path = case_dir / "loopback" / "exchange-index.json"
    if not path.is_file():
        return {}
    events = _read_json(path).get("requests", [])
    counts: dict[str, int] = {}
    for row in events:
        if isinstance(row, dict) and isinstance(row.get("category"), str):
            counts[row["category"]] = counts.get(row["category"], 0) + 1
    return counts


def stage_counts_for_case(case_dir: Path) -> dict[str, int]:
    return _stage_counts_for_events(case_dir)


def _first_request_payload(case_dir: Path, category: str) -> dict[str, Any]:
    index = _read_json(case_dir / "loopback" / "exchange-index.json")
    for row in index.get("requests", []):
        if isinstance(row, dict) and row.get("category") == category:
            request_path = case_dir / "loopback" / str(row["request_body_file"])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            return decode_annual_review_v3_wire(json.loads(request["messages"][1]["content"]))
    raise ValueError(f"no original request found for {category}")


def _ledger_stage_counts(gate: dict[str, Any]) -> dict[str, int]:
    tables = gate.get("tables", {})
    attempts = tables.get("provider_attempts") if isinstance(tables, dict) else None
    if not isinstance(attempts, dict):
        return {}
    columns = set(attempts.get("columns", []))
    if not {"stage", "status"}.issubset(columns):
        return {"provider_attempts_total": attempts.get("count", 0)}
    database = Path(str(gate.get("database", "")))
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT stage, COUNT(*) FROM provider_attempts GROUP BY stage ORDER BY stage"
        ).fetchall()
        return {str(stage): int(count) for stage, count in rows}
    finally:
        connection.close()


def _verify_loopback_issue_input_wires(
    case_dir: Path,
    requests: list[dict[str, Any]],
    pipeline: dict[str, Any],
    run_id: str,
    audited_originals: list[dict[str, Any]],
    problems: list[str],
) -> list[dict[str, Any]]:
    category_contracts = {
        "annual_issue_grouping": ("grouping", "annual_issue_group_shard_input_v1"),
        "annual_issue_global": ("global", "annual_issue_global_input_v1"),
        "annual_issue_supplement": ("supplement", "annual_issue_supplement_input_v1"),
    }
    shards = [row for row in pipeline.get("shards", []) if isinstance(row, dict)]
    checks: list[dict[str, Any]] = []
    for event in requests:
        category = event.get("category")
        if category not in category_contracts:
            continue
        kind, expected_business_schema = category_contracts[category]
        request_name = event.get("request_body_file")
        if not isinstance(request_name, str) or Path(request_name).name != request_name:
            problems.append(f"{category}: captured raw request filename is invalid")
            continue
        raw_path = case_dir / "loopback" / request_name
        try:
            raw = raw_path.read_bytes()
            raw_sha = _sha256_bytes(raw)
            _require(raw_sha == event.get("request_body_sha256"), f"{category}: archived HTTP bytes fail their captured body hash", problems)
            request = json.loads(raw.decode("utf-8"))
            messages = request.get("messages")
            if not isinstance(messages, list) or len(messages) != 2 or not isinstance(messages[1], dict):
                raise ValueError("raw Chat body does not contain exactly two captured messages")
            envelope = json.loads(messages[1].get("content", ""))
            business_input = _ANNUAL_WIRE_MODULE.decode_annual_issue_input_wire(envelope)
            business_schema = business_input.get("schema_version")
            input_sha = _business_input_sha256(business_input)
            assignments = business_input.get("evidence_assignments", [])
            assignment_sha256s = [
                _business_input_sha256(assignment)
                for assignment in assignments
                if isinstance(assignment, dict)
            ] if isinstance(assignments, list) else []
            fixture_meta = event.get("fixture_meta") if isinstance(event.get("fixture_meta"), dict) else {}
            _require(business_schema == expected_business_schema, f"{category}: strict typed decoder produced an unexpected business schema", problems)
            _require(fixture_meta.get("wire_schema_version") == _ANNUAL_WIRE_MODULE.ISSUE_INPUT_WIRE_SCHEMA, f"{category}: loopback fixture did not classify the typed wire envelope", problems)
            _require(fixture_meta.get("business_schema_version") == expected_business_schema and fixture_meta.get("schema_version") == expected_business_schema, f"{category}: fixture classification is not tied to the decoded business schema", problems)
            _require(fixture_meta.get("decoder_module") == _ANNUAL_WIRE_MODULE.__name__, f"{category}: loopback fixture used an unexpected decoder module", problems)
            _require(fixture_meta.get("decoder_module_path") == _ANNUAL_WIRE_MODULE_PATH.relative_to(ROOT).as_posix(), f"{category}: loopback fixture decoder path differs from the captured module", problems)
            _require(fixture_meta.get("decoder_module_sha256_at_import") == _ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT, f"{category}: loopback fixture decoder fingerprint differs from the captured module", problems)
            _require(fixture_meta.get("request_body_sha256") == raw_sha, f"{category}: decoded response fixture did not bind to the original HTTP body hash", problems)
            _require(fixture_meta.get("business_input_sha256") == input_sha, f"{category}: fixture business input hash differs from strict decoding of the original wire", problems)
            matching = [row for row in shards if row.get("kind") == kind and row.get("request_body_sha256") == raw_sha]
            _require(bool(matching), f"{category}: original HTTP body hash does not match a standard-pipeline shard record", problems)
            _require(any(isinstance(row.get("input"), dict) and _business_input_sha256(row["input"]) == input_sha for row in matching), f"{category}: decoded original HTTP business input differs from the shard's recorded input", problems)
            _require(any(isinstance(row.get("audit"), dict) and row["audit"].get("request_body_sha256") == raw_sha for row in matching), f"{category}: original HTTP body hash does not match the production audit record", problems)
            audited_message_checks = []
            audit_file_index = {
                row.get("run_relative_path"): row
                for row in audited_originals
                if isinstance(row, dict) and isinstance(row.get("run_relative_path"), str)
            }
            for shard in matching:
                audit = shard.get("audit") if isinstance(shard.get("audit"), dict) else {}
                audit_relative = str(audit.get("request_path", "")).replace("\\", "/")
                audit_parts = Path(audit_relative).parts
                if len(audit_parts) < 3 or audit_parts[:2] != ("runs", run_id) or ".." in audit_parts:
                    problems.append(f"{category}: production audit request path does not resolve under this unique run")
                    continue
                run_relative = Path(*audit_parts[2:]).as_posix()
                copied_relative = f"audited-chat-originals/{run_relative}"
                copied_path = case_dir / copied_relative
                indexed = audit_file_index.get(run_relative)
                if not copied_path.is_file() or indexed is None:
                    problems.append(f"{category}: production audit request original was not copied into the attempt")
                    continue
                audit_raw = copied_path.read_bytes()
                audit_sha = _sha256_bytes(audit_raw)
                _require(audit_sha == indexed.get("sha256"), f"{category}: copied production audit original differs from its evidence index", problems)
                try:
                    audit_request = json.loads(audit_raw.decode("utf-8"))
                    audit_messages = audit_request.get("messages")
                except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
                    problems.append(f"{category}: copied production audit request is not readable JSON: {type(exc).__name__}: {exc}")
                    continue
                _require(audit_messages == messages, f"{category}: original HTTP messages differ from the production audit request messages", problems)
                _require(audit.get("request_body_sha256") == raw_sha, f"{category}: production report audit request hash differs from captured HTTP bytes", problems)
                audited_message_checks.append({
                    "audit_request_path": audit_relative,
                    "attempt_copy_path": copied_relative,
                    "audit_original_file_sha256": audit_sha,
                    "messages_match_http_request": audit_messages == messages,
                    "audit_request_body_sha256": audit.get("request_body_sha256"),
                })
            checks.append({
                "ordinal": event.get("ordinal"),
                "category": category,
                "request_body_file": request_name,
                "request_body_sha256": raw_sha,
                "wire_schema_version": fixture_meta.get("wire_schema_version"),
                "business_schema_version": business_schema,
                "business_input_sha256": input_sha,
                "decoder_module": _ANNUAL_WIRE_MODULE.__name__,
                "decoder_module_path": _ANNUAL_WIRE_MODULE_PATH.relative_to(ROOT).as_posix(),
                "decoder_module_sha256_at_import": _ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT,
                "matching_report_shard_count": len(matching),
                "business_assignment_count": len(assignments) if isinstance(assignments, list) else None,
                "business_assignment_sha256s": assignment_sha256s,
                "audited_original_message_checks": audited_message_checks,
            })
        except Exception as exc:
            problems.append(f"{category}: captured raw HTTP request failed typed-wire/audit verification: {type(exc).__name__}: {exc}")
    return checks


def _verify_loopback_review_v4(
    *,
    case: str,
    case_dir: Path,
    requests: list[dict[str, Any]],
    report: dict[str, Any],
    investigation: dict[str, Any],
    review: dict[str, Any],
    audited_originals: list[dict[str, Any]],
    stage_admissions: dict[str, int],
    problems: list[str],
) -> dict[str, Any]:
    """Bind the real v4 review envelope to the v5 audit and local HTTP bytes."""

    result: dict[str, Any] = {
        "wire_schema_version": _ANNUAL_WIRE_MODULE.V3_WIRE_SCHEMA,
        "projection_version": _ANNUAL_WIRE_MODULE.V3_INPUT_PROJECTION_VERSION,
        "status": review.get("status"),
    }
    try:
        events = [row for row in requests if row.get("category") == "annual_review"]
        audits = review.get("audit_artifacts")
        if not isinstance(audits, list):
            raise ValueError("final review audit_artifacts is not a list")
        if review.get("status") == "completed":
            if len(events) != 1 or len(audits) != 1:
                raise ValueError("completed review must have exactly one captured HTTP request and audit")
        elif review.get("status") == "abstained" and review.get("reason") == "annual_review_v3_request_budget_exceeded":
            if events or audits:
                raise ValueError("budget-abstained review must have no HTTP request or audit")
            result.update({"mode": "honest_budget_abstention", "captured_http_requests": 0, "audit_count": 0})
            _write_json_new(case_dir / "review-v4-wire-audit-check.json", result)
            return result
        elif (
            case == "partial"
            and review.get("status") == "abstained"
            and review.get("reason") == "consecutive_transport_failures_stopped"
        ):
            if (
                review.get("model_called") is not False
                or review.get("call_count") != 0
                or review.get("call_attempt_count") != 0
                or events
                or audits
            ):
                raise ValueError("upstream transport-stop abstention must have no review call, HTTP event, or audit")

            discovery = investigation.get("discovery")
            discovery = discovery if isinstance(discovery, dict) else {}
            coverage = discovery.get("coverage")
            coverage = coverage if isinstance(coverage, dict) else {}
            batches = coverage.get("batch_results")
            batches = batches if isinstance(batches, list) else []
            failed_batches = [
                row for row in batches
                if isinstance(row, dict) and row.get("status") == "failed"
            ]
            discovery_events = [
                row for row in requests if row.get("category") == "annual_discovery"
            ]
            expected_statuses = [200, 200, 200, 200, 200, 503, 503, 503]
            if (
                discovery.get("status") != "partial"
                or coverage.get("complete") is not False
                or coverage.get("failed_batches") != 1
                or len(batches) != 6
                or len(failed_batches) != 1
                or failed_batches[0].get("batch_index") != 6
                or failed_batches[0].get("reason") != "model_call_failed"
                or [row.get("status") for row in discovery_events] != expected_statuses
                or stage_admissions.get("annual_discovery") != len(discovery_events)
                or stage_admissions.get("annual_discovery") != 8
                or stage_admissions.get("annual_review", 0) != 0
            ):
                raise ValueError("partial discovery and read-only SQLite ledger do not prove the expected upstream transport stop")

            retry_events = discovery_events[-3:]
            retry_hashes = {row.get("request_body_sha256") for row in retry_events}
            retry_request_payloads = []
            retry_response_hashes = []
            for row in retry_events:
                fixture_meta = row.get("fixture_meta")
                if (
                    not isinstance(fixture_meta, dict)
                    or fixture_meta.get("fixture_kind") != "controlled_discovery_transport_failure"
                    or fixture_meta.get("batch_index") != 6
                ):
                    raise ValueError("repeated 503 response is not bound to the controlled failed discovery fixture")
                request_name = row.get("request_body_file")
                response_name = row.get("response_body_file")
                if (
                    not isinstance(request_name, str)
                    or Path(request_name).name != request_name
                    or not isinstance(response_name, str)
                    or Path(response_name).name != response_name
                ):
                    raise ValueError("captured upstream transport retry has an invalid HTTP evidence filename")
                request_raw = (case_dir / "loopback" / request_name).read_bytes()
                response_raw = (case_dir / "loopback" / response_name).read_bytes()
                if (
                    _sha256_bytes(request_raw) != row.get("request_body_sha256")
                    or _sha256_bytes(response_raw) != row.get("response_body_sha256")
                ):
                    raise ValueError("captured upstream transport retry bytes do not match their recorded hashes")
                http_request = json.loads(request_raw.decode("utf-8"))
                messages = http_request.get("messages")
                if not isinstance(messages, list) or len(messages) != 2:
                    raise ValueError("captured upstream transport retry lacks the expected request messages")
                payload = json.loads(messages[1].get("content", ""))
                retry_request_payloads.append(payload.get("batch_index") if isinstance(payload, dict) else None)
                retry_response_hashes.append(row.get("response_body_sha256"))
            if (
                len(retry_hashes) != 1
                or retry_request_payloads != [6, 6, 6]
                or len(set(retry_response_hashes)) != 1
            ):
                raise ValueError("the repeated 503 responses do not bind to the same failed discovery batch")

            result.update({
                "mode": "upstream_transport_stop_no_review_call",
                "captured_http_requests": 0,
                "audit_count": 0,
                "upstream_transport_stop_proof": {
                    "discovery_status": discovery.get("status"),
                    "failed_batch_index": 6,
                    "failed_batch_reason": failed_batches[0].get("reason"),
                    "discovery_http_statuses": expected_statuses,
                    "repeated_failed_batch_indices": retry_request_payloads,
                    "retry_request_body_sha256": next(iter(retry_hashes)),
                    "retry_response_body_sha256s": retry_response_hashes,
                    "read_only_sqlite_annual_discovery_admissions": stage_admissions.get("annual_discovery"),
                    "read_only_sqlite_annual_review_admissions": stage_admissions.get("annual_review", 0),
                },
            })
            _write_json_new(case_dir / "review-v4-wire-audit-check.json", result)
            return result
        else:
            raise ValueError("final review was neither a completed request nor a budget abstention")

        event = events[0]
        request_name = event.get("request_body_file")
        if not isinstance(request_name, str) or Path(request_name).name != request_name:
            raise ValueError("captured review request filename is invalid")
        raw_path = case_dir / "loopback" / request_name
        raw = raw_path.read_bytes()
        raw_sha = _sha256_bytes(raw)
        if raw_sha != event.get("request_body_sha256"):
            raise ValueError("captured loopback review body does not match its event SHA")
        http_request = json.loads(raw.decode("utf-8"))
        messages = http_request.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            raise ValueError("captured review body does not have the exact system/user message pair")
        envelope = json.loads(messages[1].get("content", ""))
        if not isinstance(envelope, dict) or envelope.get("schema_version") != _ANNUAL_WIRE_MODULE.V3_WIRE_SCHEMA:
            raise ValueError("captured review message is not the current v4 wire envelope")
        decoded = _ANNUAL_WIRE_MODULE.decode_annual_review_v3_wire(envelope)
        fixture_meta = event.get("fixture_meta") if isinstance(event.get("fixture_meta"), dict) else {}
        if (
            fixture_meta.get("wire_schema_version") != _ANNUAL_WIRE_MODULE.V3_WIRE_SCHEMA
            or fixture_meta.get("projection_version") != _ANNUAL_WIRE_MODULE.V3_INPUT_PROJECTION_VERSION
            or fixture_meta.get("decoder_module") != _ANNUAL_WIRE_MODULE.__name__
            or fixture_meta.get("decoder_module_path") != _ANNUAL_WIRE_MODULE_PATH.relative_to(ROOT).as_posix()
            or fixture_meta.get("decoder_module_sha256_at_import") != _ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT
            or fixture_meta.get("decoded_input_sha256") != _business_input_sha256(decoded)
            or fixture_meta.get("request_body_sha256") != raw_sha
        ):
            raise ValueError("loopback review decoder metadata does not bind the captured v4 body")

        pipeline = investigation.get("model_issue_pipeline")
        identity = pipeline.get("source_identity") if isinstance(pipeline, dict) else None
        if not isinstance(identity, dict):
            raise ValueError("strict report issue pipeline lacks source identity for review reconstruction")
        prompt = (ROOT / "config" / "prompts" / "annual_review_v3.md").read_text(encoding="utf-8")
        from finagent.agents.annual_review import _build_review_input_v3, build_annual_review_v3_request

        semantic_input, _allowed, _refs, _no_priority = _build_review_input_v3(
            report,
            investigation,
            identity,
            request_model=FIXTURE_MODEL_NAME,
            system_prompt=prompt,
            enforce_budget=False,
        )
        expected = build_annual_review_v3_request(
            semantic_input,
            model=FIXTURE_MODEL_NAME,
            system_prompt=prompt,
        )
        if decoded != expected["projected_input"]:
            raise ValueError("captured review v4 input differs from the production purpose projection")
        if expected["request_body_sha256"] != raw_sha or expected["request_chars"] != len(raw.decode("utf-8")):
            raise ValueError("production review request helper does not reproduce the captured HTTP body")

        audit = audits[0]
        audit_path = audit.get("request_path") if isinstance(audit, dict) else None
        path_parts = audit_path.split("/") if isinstance(audit_path, str) else []
        run_id = report.get("run_id")
        if (
            len(path_parts) != 4
            or path_parts[:2] != ["runs", run_id]
            or path_parts[2] in {"", ".", ".."}
            or path_parts[3] != "request.json"
        ):
            raise ValueError("production review audit request path is outside the current run")
        run_relative = "/".join(path_parts[2:])
        copied_relative = Path("audited-chat-originals") / Path(*path_parts[2:])
        copied_path = case_dir / copied_relative
        index_row = next((
            row for row in audited_originals
            if isinstance(row, dict) and row.get("run_relative_path") == run_relative
        ), None)
        if not copied_path.is_file() or not isinstance(index_row, dict):
            raise ValueError("original strict review audit request was not preserved with the attempt")
        audit_raw = copied_path.read_bytes()
        if _sha256_bytes(audit_raw) != index_row.get("sha256"):
            raise ValueError("copied review audit request differs from its preserved original hash")
        audit_request = json.loads(audit_raw.decode("utf-8"))
        if audit_request.get("messages") != messages:
            raise ValueError("review audit messages differ from the actual loopback HTTP messages")

        required_limits = decoded.get("limitations_required")
        returned_limits = review.get("limitations")
        if not isinstance(required_limits, list) or not isinstance(returned_limits, list) or not set(required_limits).issubset(returned_limits):
            raise ValueError("completed review did not preserve every projected required limitation")
        audit_values = {
            "request_body_sha256": raw_sha,
            "semantic_input_sha256": expected["semantic_input_sha256"],
            "projected_input_sha256": expected["projected_input_sha256"],
            "review_wire_version": _ANNUAL_WIRE_MODULE.V3_WIRE_SCHEMA,
            "review_input_projection_version": _ANNUAL_WIRE_MODULE.V3_INPUT_PROJECTION_VERSION,
        }
        for name, expected_value in audit_values.items():
            if audit.get(name) != expected_value:
                raise ValueError(f"strict review audit field {name} does not match the actual v4 request")

        result.update({
            "mode": "actual_http_audit_projection_hash_roundtrip",
            "request_body_file": request_name,
            "request_body_sha256": raw_sha,
            "request_body_chars": expected["request_chars"],
            "semantic_input_sha256": expected["semantic_input_sha256"],
            "projected_input_sha256": expected["projected_input_sha256"],
            "audit_request_path": audit_path,
            "audit_request_file_sha256": index_row["sha256"],
            "audit_messages_match_http": True,
            "limitations_required_count": len(required_limits),
            **{key: audit.get(key) for key in (
                "review_wire_version", "review_input_projection_version", "request_body_sha256",
            )},
        })
    except Exception as exc:
        result.update({"mode": "failed", "exception_type": type(exc).__name__, "exception": str(exc)})
        problems.append(f"{case}: current review v4 wire/audit binding failed: {type(exc).__name__}: {exc}")
    _write_json_new(case_dir / "review-v4-wire-audit-check.json", result)
    return result


def _same_quote_distinct_issue_pair(pipeline: dict[str, Any]) -> bool:
    candidates = pipeline.get("candidates", [])
    candidate_by_id = {row.get("finding_id"): row for row in candidates if isinstance(row, dict)}
    issue_by_category: dict[str, list[dict[str, Any]]] = {category: [] for category in _CATEGORIES}
    for issue in pipeline.get("issues", []):
        if not isinstance(issue, dict):
            continue
        for category in _CATEGORIES[:2]:
            if any(candidate_by_id.get(member, {}).get("title") == category for member in issue.get("member_finding_ids", [])):
                issue_by_category[category].append(issue)
    for left in issue_by_category[_CATEGORIES[0]]:
        left_quotes = {
            (citation.get("evidence_id"), citation.get("quote"))
            for member in left.get("member_finding_ids", [])
            for citation in candidate_by_id.get(member, {}).get("citations", [])
            if isinstance(citation, dict)
        }
        for right in issue_by_category[_CATEGORIES[1]]:
            if left.get("issue_id") == right.get("issue_id"):
                continue
            right_quotes = {
                (citation.get("evidence_id"), citation.get("quote"))
                for member in right.get("member_finding_ids", [])
                for citation in candidate_by_id.get(member, {}).get("citations", [])
                if isinstance(citation, dict)
            }
            if left_quotes & right_quotes:
                return True
    return False


@pytest.mark.skipif(
    any(
        not os.environ.get(name)
        for name in (
            "FINTRACE_R4_ATTEMPT_DIR",
            "FINTRACE_R4_RUNNER_DIR",
            "FINTRACE_R4_EVALUATION_DIR",
        )
    ),
    reason=(
        "requires FINTRACE_R4_ATTEMPT_DIR, FINTRACE_R4_RUNNER_DIR, and "
        "FINTRACE_R4_EVALUATION_DIR for the controlled R4 semantic run"
    ),
)
def test_standard_cli_preserves_r4_issue_semantics_offline(tmp_path: Path) -> None:
    """Exercise the standard repository-root v5 path with accepted PDFs and loopback-only mock replies."""

    attempt = _attempt_root()
    attempt.mkdir(parents=True, exist_ok=True)
    frozen_candidate = _frozen_candidate_manifest(attempt)
    temp_harness = Path(os.environ["FINTRACE_R4_RUNNER_DIR"]).resolve()
    _write_json_new(attempt / "attempt-context.json", {
        "schema_version": "r4_semantic_pipeline_attempt_v1",
        "attempt_id": attempt.name,
        "started_at_utc": _utc_now(),
        "nature": "controlled synthetic PDF + request-derived local HTTP mock with independent 16-member pressure and smaller supplement-reachability profiles; no provider-quality or human-review claim",
        "input_paths": {
            "issue_pressure": str(ISSUE_PDF.resolve()),
            "supplement_microcase": str(ISSUE_PDF.resolve()),
            "clean": str(CLEAN_PDF.resolve()),
        },
        "expected_sha256": EXPECTED_HASHES,
        "candidate_freeze": {
            "source_manifest_path": str(FREEZE_MANIFEST_PATH.resolve()),
            "manifest_path": str((attempt / "candidate-freeze-manifest.json").resolve()),
            "manifest_raw_sha256": frozen_candidate["raw_sha256"],
            "manifest_internal_sha256": frozen_candidate["internal_sha256"],
            "runtime_source_manifest_sha256": frozen_candidate["runtime_source_manifest_sha256"],
            "runtime_source_file_count": frozen_candidate["runtime_source_file_count"],
        },
        "artifacts_root": str((ROOT / "artifacts").resolve()),
        "task_runner_dir": str(temp_harness),
        "strict_reader": "finagent.api.annual_report_read.load_annual_report(ROOT, run_id)",
        "captured_issue_input_wire_decoder": {
            "module": _ANNUAL_WIRE_MODULE.__name__,
            "path": _ANNUAL_WIRE_MODULE_PATH.relative_to(ROOT).as_posix(),
            "sha256_at_test_import": _ANNUAL_WIRE_MODULE_SHA256_AT_IMPORT,
        },
        "external_network": False,
        "protocol_or_quote_index_used_as_model_input": False,
    })
    source_before = {
        "issue": _sha256_file(ISSUE_PDF),
        "clean": _sha256_file(CLEAN_PDF),
    }
    _write_json_new(attempt / "accepted-pdf-hashes-before.json", source_before)

    problems: list[str] = []
    _require(source_before == EXPECTED_HASHES, "accepted source PDF hashes do not match the task-provided identities", problems)
    runtime_probe_record: dict[str, Any] | None = None
    try:
        runtime_probe_record = _capture_expected_cli_runtime_identity(attempt)
    except Exception as exc:
        problems.append(f"standard CLI runtime identity probe failed: {type(exc).__name__}: {exc}")
    expected_runtime_identity = runtime_probe_record.get("runtime_identity") if isinstance(runtime_probe_record, dict) else None
    expected_runtime_fingerprint = (
        expected_runtime_identity.get("fingerprint")
        if isinstance(expected_runtime_identity, dict) and runtime_identity_is_valid(expected_runtime_identity)
        else None
    )
    issue_source = _register_accepted_pdf(attempt, case="issue", original=ISSUE_PDF, expected_sha=EXPECTED_HASHES["issue"])
    supplement_source = _register_accepted_pdf(attempt, case="supplement", original=ISSUE_PDF, expected_sha=EXPECTED_HASHES["issue"])
    clean_source = _register_accepted_pdf(attempt, case="clean", original=CLEAN_PDF, expected_sha=EXPECTED_HASHES["clean"])

    outcomes: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for case, source in (("issue", issue_source), ("supplement", supplement_source), ("clean", clean_source), ("partial", issue_source)):
        try:
            outcome = _run_cli_case(
                attempt,
                source,
                case=case,
                expected_runtime_fingerprint=expected_runtime_fingerprint,
            )
            outcomes.append(outcome)
            summaries.append(_validate_issue_case(outcome, source, problems))
        except Exception as exc:
            case_dir = attempt / "scenarios" / case
            case_dir.mkdir(parents=True, exist_ok=True)
            if not (case_dir / "execution-error.json").exists():
                _write_json_new(case_dir / "execution-error.json", {
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                })
            problems.append(f"{case}: execution/validation raised {type(exc).__name__}: {exc}")

    source_after = {
        "issue": _sha256_file(ISSUE_PDF),
        "clean": _sha256_file(CLEAN_PDF),
        "registered_issue": _sha256_file(Path(issue_source["source_pdf_path"])),
        "registered_supplement": _sha256_file(Path(supplement_source["source_pdf_path"])),
        "registered_clean": _sha256_file(Path(clean_source["source_pdf_path"])),
    }
    _write_json_new(attempt / "accepted-pdf-hashes-after.json", source_after)
    _require(source_after["issue"] == source_before["issue"], "accepted issue PDF changed during the attempt", problems)
    _require(source_after["clean"] == source_before["clean"], "accepted clean PDF changed during the attempt", problems)
    _require(source_after["registered_issue"] == EXPECTED_HASHES["issue"], "registered issue source changed during the attempt", problems)
    _require(source_after["registered_supplement"] == EXPECTED_HASHES["issue"], "registered supplement source changed during the attempt", problems)
    _require(source_after["registered_clean"] == EXPECTED_HASHES["clean"], "registered clean source changed during the attempt", problems)

    final = {
        "schema_version": "r4_semantic_pipeline_result_v1",
        "attempt_id": attempt.name,
        "finished_at_utc": _utc_now(),
        "controlled_fixture": True,
        "provider_quality_claim": False,
        "human_review_claim": False,
        "source_before": source_before,
        "source_after": source_after,
        "candidate_freeze": {
            "manifest_raw_sha256": frozen_candidate["raw_sha256"],
            "manifest_internal_sha256": frozen_candidate["internal_sha256"],
            "runtime_source_manifest_sha256": frozen_candidate["runtime_source_manifest_sha256"],
            "runtime_source_file_count": frozen_candidate["runtime_source_file_count"],
        },
        "expected_cli_runtime_identity_probe": runtime_probe_record,
        "expected_runtime_fingerprint": expected_runtime_fingerprint,
        "cases": summaries,
        "problems": problems,
    }
    _write_json_new(attempt / "semantic-result.json", final)
    if problems:
        pytest.fail("R4 semantic CLI evidence did not meet every assertion:\n- " + "\n- ".join(problems))

