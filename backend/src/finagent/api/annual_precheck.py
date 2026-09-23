"""本地年度财务预检。只调用已有提取和同比，不调用模型。"""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from finagent.core.safe_paths import PathBoundaryError, resolve_inside, run_directory, validate_run_id
from finagent.finance.annual_change import ANNUAL_CHANGE_FORMULA, calculate_annual_changes
from finagent.ingestion.errors import PdfInputError
from finagent.ingestion.extract_annual_facts import extract_annual_financial_facts
from finagent.schemas.text_pdf import ParsedTextPdf
from finagent.verification.source_amount import verify_source_amounts

PRECHECK_NOTE = (
    "本预检在哈希一致后，从原始 PDF 的引用页和坐标重新读取文字，复核原始金额是否位于这些区域内，"
    "并用独立 Decimal 计算复核 raw_value 乘 unit_multiplier 是否等于 normalized_value。"
    "尚未独立确认年度列、表头口径或完整财报事实。未调用模型。它不是舞弊结论。"
)
_SNAPSHOT_MODULES = (
    "finagent.api.annual_precheck",
    "finagent.api.app",
    "finagent.core.safe_paths",
    "finagent.ingestion.extract_annual_facts",
    "finagent.finance.annual_change",
    "finagent.schemas.financial_fact",
    "finagent.schemas.text_pdf",
    "finagent.verification.source_amount",
)


class HashMismatchError(Exception):
    def __init__(self, source_pdf_sha256: str, parsed_source_sha256: str) -> None:
        super().__init__("原始 PDF 的 SHA256 与解析结果不一致，未生成事实。")
        self.source_pdf_sha256 = source_pdf_sha256
        self.parsed_source_sha256 = parsed_source_sha256


@dataclass(frozen=True, slots=True)
class PrecheckPaths:
    project_root: Path
    processed_root: Path
    raw_root: Path
    runs_root: Path


def precheck_paths(project_root: Path) -> PrecheckPaths:
    root = project_root.resolve()
    return PrecheckPaths(
        project_root=root,
        processed_root=root / "data" / "processed",
        raw_root=root / "data" / "raw",
        runs_root=root / "artifacts" / "runs",
    )


def create_annual_precheck(
    project_root: Path,
    *,
    parsed_path: str,
    source_pdf_path: str,
    company_id: str,
    report_year: int,
) -> dict:
    """同步完成一次预检。哈希不一致时不写运行目录。"""

    roots = precheck_paths(project_root)
    parsed_file = resolve_inside(roots.processed_root, parsed_path)
    source_file = resolve_inside(roots.raw_root, source_pdf_path)
    if not parsed_file.is_file():
        raise PathBoundaryError("找不到 data/processed 中的解析文件。")
    if not source_file.is_file():
        raise PathBoundaryError("找不到 data/raw 中的原始 PDF。")
    source_pdf_sha256 = _sha256(source_file)
    parsed_file_sha256 = _sha256(parsed_file)
    try:
        parsed = ParsedTextPdf.from_dict(json.loads(parsed_file.read_text(encoding="utf-8")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PathBoundaryError("解析文件不是可用的 text_pdf.json。") from exc
    if source_pdf_sha256 != parsed.source_sha256:
        raise HashMismatchError(source_pdf_sha256, parsed.source_sha256)
    extracted = extract_annual_financial_facts(parsed, company_id, report_year)
    calculated = calculate_annual_changes(extracted, report_year)
    verification = verify_source_amounts(source_file, extracted.facts)
    if verification["failed_count"]:
        status = "verification_failed"
    elif not extracted.issues and not calculated.issues and verification["status"] == "passed":
        status = "completed"
    else:
        status = "completed_with_issues"
    run_id, run_dir = _allocate_run_dir(roots.runs_root, company_id, report_year)
    record = {
        "run_id": run_id,
        "status": status,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "inputs": {
            "parsed_path": parsed_path,
            "source_pdf_path": source_pdf_path,
            "parsed_file_sha256": parsed_file_sha256,
            "source_pdf_sha256": source_pdf_sha256,
            "parsed_source_sha256": parsed.source_sha256,
            "hashes_match": True,
            "company_id": company_id,
            "report_year": report_year,
            "document_id": parsed.document_id,
        },
        "model_called": False,
        "independently_verified": False,
        "code": _code_snapshot(roots.project_root),
        "note": PRECHECK_NOTE,
        "formula": ANNUAL_CHANGE_FORMULA,
        "verification": verification,
        "facts": extracted.to_dict(),
        "calculation": calculated.to_dict(),
        "issues": {
            "extraction": [_public_issue(issue) for issue in extracted.issues],
            "calculation": [_public_issue(issue) for issue in calculated.issues],
        },
    }
    _write_new(run_dir / "precheck.json", record)
    _write_new(run_dir / "facts.json", extracted.to_dict())
    _write_new(run_dir / "calculation.json", calculated.to_dict())
    return record


def load_annual_precheck(project_root: Path, run_id: str) -> dict:
    roots = precheck_paths(project_root)
    checked = validate_run_id(run_id)
    run_dir = run_directory(roots.runs_root, checked)
    record_path = (run_dir / "precheck.json").resolve()
    if not record_path.is_file() or not record_path.is_relative_to(run_dir):
        raise FileNotFoundError(checked)
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("run_id") != checked:
        raise FileNotFoundError(checked)
    return payload


def _allocate_run_dir(runs_root: Path, company_id: str, report_year: int) -> tuple[str, Path]:
    if "/" in company_id or "\\" in company_id or company_id.strip() in {"", ".", ".."}:
        raise PdfInputError("company_id 不能作为运行目录名。")
    runs_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"annual-precheck-{company_id}-{report_year}-{stamp}"
    validate_run_id(base)
    for suffix in ("", "-2", "-3", "-4", "-5"):
        run_id = f"{base}{suffix}"
        path = runs_root / run_id
        try:
            path.mkdir(parents=False, exist_ok=False)
            return run_id, path
        except FileExistsError:
            continue
    raise FileExistsError("运行目录已存在，未覆盖已有运行。")


def _public_issue(issue: object) -> dict[str, str | None]:
    return {
        "code": getattr(issue, "code", None),
        "message": getattr(issue, "message", None),
        "indicator_name": getattr(issue, "indicator_name", None),
    }


def _write_new(path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _code_snapshot(project_root: Path) -> dict:
    git_state = _git_state(project_root)
    hashes: dict[str, str] = {}
    missing: list[str] = []
    for module_name in _SNAPSHOT_MODULES:
        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
        if not module_file:
            missing.append(module_name)
            continue
        path = Path(module_file).resolve()
        if not path.is_file():
            missing.append(module_name)
            continue
        hashes[module_name] = _sha256(path)
    git_state["source_file_sha256"] = hashes
    git_state["missing_source_files"] = missing
    return git_state


def _git_state(project_root: Path) -> dict:
    if not (project_root / ".git").exists():
        return {"git_available": False, "git_head": None, "git_dirty": None}
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return {"git_available": False, "git_head": None, "git_dirty": None}
    head_text = head.stdout.strip()
    if not head_text:
        return {"git_available": False, "git_head": None, "git_dirty": None}
    return {
        "git_available": True,
        "git_head": head_text,
        "git_dirty": bool(dirty.stdout.strip()),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
