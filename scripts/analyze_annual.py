"""从原始文本年报 PDF 生成 v2 分析归档与 JSON/Markdown 报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from finagent.api.annual_analysis import analyze_annual_pdf  # noqa: E402
from finagent.finance.v2_calculation import RULE_VERSION  # noqa: E402
from finagent.reports.investigation_append import (  # noqa: E402
    attach_investigation_to_report,
    render_report_with_investigation,
    run_annual_investigation_appendix,
)

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_PROVENANCE_FILES = (
    "scripts/analyze_annual.py",
    "backend/src/finagent/api/annual_analysis.py",
    "backend/src/finagent/ingestion/parse_text_pdf.py",
    "backend/src/finagent/ingestion/extract_annual_facts.py",
    "backend/src/finagent/schemas/financial_fact.py",
    "backend/src/finagent/schemas/financial_fact_v2.py",
    "backend/src/finagent/finance/v2_calculation.py",
    "backend/src/finagent/finance/v2_screening.py",
    "backend/src/finagent/verification/independent_fact.py",
    "backend/src/finagent/verification/comparability.py",
    "backend/src/finagent/verification/independent_calculation.py",
    "backend/src/finagent/verification/claim.py",
    "backend/src/finagent/reports/annual_report.py",
    "backend/src/finagent/reports/investigation_append.py",
    "backend/src/finagent/agents/annual_investigation.py",
    "backend/src/finagent/retrieval/annual_context.py",
    "backend/src/finagent/audit/audited_chat.py",
    "backend/src/finagent/core/model_settings.py",
    "backend/src/finagent/llm/chat_completion.py",
    "config/prompts/annual_investigation_v1.md",
)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.with_model and args.source_record is None:
        parser.error("--with-model 需要同时提供 --source-record。")
    if args.source_record is not None and not args.with_model:
        parser.error("--source-record 仅能与 --with-model 一起使用。")
    try:
        return _run(args)
    except (OSError, ValueError) as exc:
        print(f"status=failed reason={exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从原始 PDF 运行确定性 FINTRACE v2 年度分析并归档报告。"
    )
    parser.add_argument("--source-pdf", required=True, type=Path, help="原始文本型年报 PDF")
    parser.add_argument("--company-id", required=True, help="公司标识，例如股票代码")
    parser.add_argument("--report-year", required=True, type=int, help="报告年度")
    parser.add_argument("--document-id", required=True, help="稳定的来源文档标识")
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=ROOT / "artifacts",
        help="归档根目录；默认为项目 artifacts，可用于测试的安全临时目录",
    )
    parser.add_argument(
        "--expected-sha256",
        default=None,
        help="可选的来源哈希断言；不匹配时仍归档输入和失败原因",
    )
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="显式启用 M3 年报调查；仅此选项会读取 MODEL_* 环境变量",
    )
    parser.add_argument(
        "--source-record",
        type=Path,
        default=None,
        help="绑定当前 PDF 的 source.json；仅与 --with-model 一起使用",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    source_pdf = args.source_pdf.expanduser().resolve()
    if not source_pdf.is_file():
        raise ValueError(f"找不到原始 PDF：{source_pdf}")
    if not args.company_id.strip():
        raise ValueError("company_id 不能为空。")
    if type(args.report_year) is not int or not 1900 <= args.report_year <= 2100:
        raise ValueError("report_year 必须是 1900 到 2100 之间的整数。")
    if args.expected_sha256 is not None and _SHA256.fullmatch(args.expected_sha256) is None:
        raise ValueError("expected-sha256 必须是 64 位十六进制字符串。")
    if args.with_model and args.source_record is None:
        raise ValueError("--with-model 需要同时提供 --source-record。")
    if args.source_record is not None and not args.with_model:
        raise ValueError("--source-record 仅能与 --with-model 一起使用。")

    artifacts_root = _safe_artifacts_root(args.artifacts_root)
    source_sha256 = _sha256(source_pdf)
    run_id, run_dir, report_dir = _reserve_run_dirs(artifacts_root)
    input_copy = run_dir / "source.pdf"
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    actual_copy_sha256: str | None = None
    investigation: dict[str, object] | None = None
    result = None
    try:
        actual_copy_sha256 = _copy_and_hash_new(source_pdf, input_copy)
        if actual_copy_sha256 != source_sha256:
            raise ValueError("原始 PDF 在归档期间发生变化，拷贝哈希与读取前哈希不一致。")
        if args.expected_sha256 is not None and actual_copy_sha256.lower() != args.expected_sha256.lower():
            raise ValueError(
                "原始 PDF SHA256 与 --expected-sha256 不一致："
                f"expected={args.expected_sha256.lower()} actual={actual_copy_sha256}。"
            )

        result = analyze_annual_pdf(
            input_copy,
            run_id=run_id,
            company_id=args.company_id,
            report_year=args.report_year,
            document_id=args.document_id,
        )
        if result.parsed_pdf.source_sha256.lower() != actual_copy_sha256.lower():
            raise ValueError("解析结果中的 source_sha256 与已归档 PDF 不一致。")

        analysis_payload = result.archive_dict()
        report_payload = result.report
        markdown = result.markdown
        if args.with_model:
            investigation = run_annual_investigation_appendix(
                report_payload,
                archived_pdf_path=input_copy,
                original_pdf_path=source_pdf,
                source_record_path=args.source_record.expanduser().resolve(),
                run_dir=run_dir,
                artifacts_root=artifacts_root,
            )
            _write_json_new(run_dir / "investigation.json", investigation)
            investigation_path = _relative_or_absolute(
                run_dir / "investigation.json", artifacts_root
            )
            report_payload, analysis_payload = attach_investigation_to_report(
                report_payload,
                analysis_payload,
                investigation,
                investigation_path=investigation_path,
            )
            markdown = render_report_with_investigation(report_payload, investigation)

        _write_json_new(run_dir / "parsed_text_pdf.json", result.parsed_pdf.to_dict())
        _write_json_new(run_dir / "analysis.json", analysis_payload)
        _write_json_new(report_dir / "report.json", report_payload)
        _write_text_new(report_dir / "report.md", markdown)
        completed_status = result.status
        if investigation is not None and investigation.get("status") != "completed":
            completed_status = "completed_with_issues"
        manifest = _manifest(
            status=completed_status,
            run_id=run_id,
            created_at=created_at,
            args=args,
            source_pdf=source_pdf,
            source_sha256=actual_copy_sha256,
            artifacts_root=artifacts_root,
            run_dir=run_dir,
            report_dir=report_dir,
            rule_version=RULE_VERSION,
            error=None,
        )
        if investigation is not None:
            _add_investigation_manifest(
                manifest,
                investigation,
                source_record_path=args.source_record.expanduser().resolve(),
                artifacts_root=artifacts_root,
                run_dir=run_dir,
            )
        _write_json_new(run_dir / "manifest.json", manifest)
        confirmed_facts = len(result.report["confirmed"]["metrics"])
        confirmed_calculations = len(result.report["confirmed"]["analyses"])
        candidates = sum(item.get("status") == "candidate" for item in result.screening)
        print(
            f"status={completed_status} run_id={run_id} facts={len(result.facts)} "
            f"confirmed_facts={confirmed_facts} calculations={len(result.calculations)} "
            f"confirmed_calculations={confirmed_calculations} candidate_signals={candidates}"
        )
        if investigation is not None:
            print(
                f"investigation={investigation.get('status')} "
                f"model_called={str(bool(investigation.get('model_called'))).lower()} "
                f"path={_relative_or_absolute(run_dir / 'investigation.json', artifacts_root)}"
            )
        print(f"run_dir={run_dir}")
        print(f"report_dir={report_dir}")
        return 2 if investigation is not None and investigation.get("status") != "completed" else 0
    except Exception as exc:
        deterministic_archived = (run_dir / "analysis.json").is_file() and (
            report_dir / "report.json"
        ).is_file()
        if args.with_model and investigation is None:
            investigation = {
                "kind": "fintrace_annual_investigation",
                "run_id": run_id,
                "company_id": args.company_id,
                "report_year": args.report_year,
                "source_document_id": args.document_id,
                "source_sha256": actual_copy_sha256 or source_sha256,
                "status": "failed",
                "model_called": False,
                "model_call_count": 0,
                "model_call_attempt_count": 0,
                "items": [],
                "audit_artifacts": [],
                "fraud_conclusion": None,
                "reason": "deterministic_analysis_failed_before_investigation",
                "archive_path": _relative_or_absolute(
                    run_dir / "investigation.json", artifacts_root
                ),
            }
            try:
                _write_json_new(run_dir / "investigation.json", investigation)
            except OSError:
                pass
        model_called = bool(investigation and investigation.get("model_called"))
        failure = {
            "run_id": run_id,
            "status": "failed",
            "model_called": model_called,
            "reason": str(exc),
            "error_type": type(exc).__name__,
            "source_sha256": actual_copy_sha256 or source_sha256,
            "expected_sha256": args.expected_sha256,
        }
        if investigation is not None:
            failure["model_investigation_status"] = investigation.get("status")
            failure["investigation_path"] = _relative_or_absolute(
                run_dir / "investigation.json", artifacts_root
            )
        try:
            _write_json_new(run_dir / "failure.json", failure)
            _write_json_new(report_dir / "failure.json", failure)
            failure_manifest = _manifest(
                status=("completed_with_issues" if deterministic_archived else "failed"),
                run_id=run_id,
                created_at=created_at,
                args=args,
                source_pdf=source_pdf,
                source_sha256=actual_copy_sha256 or source_sha256,
                artifacts_root=artifacts_root,
                run_dir=run_dir,
                report_dir=report_dir,
                rule_version=RULE_VERSION,
                error=str(exc),
            )
            if investigation is not None:
                _add_investigation_manifest(
                    failure_manifest,
                    investigation,
                    source_record_path=args.source_record.expanduser().resolve(),
                    artifacts_root=artifacts_root,
                    run_dir=run_dir,
                )
            _write_json_new(run_dir / "manifest.json", failure_manifest)
        except OSError as archive_exc:
            print(
                f"status=failed run_id={run_id} reason={exc}; "
                f"failure_archive_error={archive_exc}",
                file=sys.stderr,
            )
            return 2
        print(f"status=failed run_id={run_id} reason={exc}", file=sys.stderr)
        print(f"run_dir={run_dir}", file=sys.stderr)
        print(f"report_dir={report_dir}", file=sys.stderr)
        return 2


def _safe_artifacts_root(value: Path) -> Path:
    if ".." in value.parts:
        raise ValueError("artifacts-root 不能包含 .. 路径遍历片段。")
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    project_root = ROOT.resolve()
    formal_artifacts = (project_root / "artifacts").resolve()
    if _is_relative_to(resolved, project_root) and not _is_relative_to(resolved, formal_artifacts):
        raise ValueError("项目工作区内的归档根目录必须位于 artifacts/ 下。")
    raw_root = (project_root / "data" / "raw").resolve()
    if resolved == raw_root or _is_relative_to(resolved, raw_root):
        raise ValueError("归档根目录不能位于 data/raw/。")
    if resolved == project_root:
        raise ValueError("归档根目录不能是项目根目录。")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("artifacts-root 已存在但不是目录。")
    return resolved


def _reserve_run_dirs(artifacts_root: Path) -> tuple[str, Path, Path]:
    runs_root = artifacts_root / "runs"
    reports_root = artifacts_root / "reports"
    runs_root.mkdir(parents=True, exist_ok=True)
    reports_root.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        run_id = f"annual-analysis-{uuid4()}"
        run_dir = runs_root / run_id
        report_dir = reports_root / run_id
        if run_dir.exists() or report_dir.exists():
            continue
        try:
            run_dir.mkdir(exist_ok=False)
            try:
                report_dir.mkdir(exist_ok=False)
            except FileExistsError:
                run_dir.rmdir()
                continue
            return run_id, run_dir, report_dir
        except FileExistsError:
            continue
    raise FileExistsError("连续生成的唯一 run_id 均已存在；未覆盖任何运行目录。")


def _copy_and_hash_new(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(
    *,
    status: str,
    run_id: str,
    created_at: str,
    args: argparse.Namespace,
    source_pdf: Path,
    source_sha256: str,
    artifacts_root: Path,
    run_dir: Path,
    report_dir: Path,
    rule_version: str,
    error: str | None,
) -> dict[str, object]:
    head, dirty = _git_state()
    source_hashes: dict[str, str | None] = {}
    for relative in _PROVENANCE_FILES:
        source = ROOT / relative
        source_hashes[relative] = _sha256(source) if source.is_file() else None
    payload: dict[str, object] = {
        "run_id": run_id,
        "status": status,
        "created_at": created_at,
        "model_called": False,
        "company_id": args.company_id,
        "report_year": args.report_year,
        "document_id": args.document_id,
        "inputs": {
            "source_pdf_original_path": str(source_pdf),
            "source_pdf_original_filename": source_pdf.name,
            "source_pdf_archive_path": _relative_or_absolute(run_dir / "source.pdf", artifacts_root),
            "source_pdf_sha256": source_sha256,
            "source_pdf_expected_sha256": args.expected_sha256,
        },
        "code": {
            "git_head": head,
            "git_dirty": dirty,
            "source_file_sha256": source_hashes,
        },
        "rules": {"annual_calculation_version": rule_version},
        "outputs": {
            "run_dir": _relative_or_absolute(run_dir, artifacts_root),
            "report_dir": _relative_or_absolute(report_dir, artifacts_root),
            "report_json": _relative_or_absolute(report_dir / "report.json", artifacts_root),
            "report_markdown": _relative_or_absolute(report_dir / "report.md", artifacts_root),
        },
    }
    if error is not None:
        payload["failure_reason"] = error
    return payload


def _add_investigation_manifest(
    manifest: dict[str, object],
    investigation: dict[str, object],
    *,
    source_record_path: Path,
    artifacts_root: Path,
    run_dir: Path,
) -> None:
    outputs = manifest["outputs"]
    if isinstance(outputs, dict):
        outputs["investigation_json"] = _relative_or_absolute(
            run_dir / "investigation.json", artifacts_root
        )
    binding = investigation.get("source_binding")
    source_record_sha256 = (
        binding.get("source_record_sha256") if isinstance(binding, dict) else None
    )
    model_called = bool(investigation.get("model_called"))
    manifest["model_called"] = model_called
    manifest["model_investigation_status"] = investigation.get("status")
    manifest["model_investigation"] = {
        "requested": True,
        "status": investigation.get("status"),
        "model_called": model_called,
        "model_call_count": investigation.get("model_call_count", 0),
        "model_call_attempt_count": investigation.get("model_call_attempt_count", 0),
        "model_name": investigation.get("model_name"),
        "output_path": _relative_or_absolute(run_dir / "investigation.json", artifacts_root),
        "source_record_path": _relative_or_absolute(source_record_path, ROOT),
        "source_record_sha256": source_record_sha256,
    }


def _git_state() -> tuple[str, bool | None]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty_text = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown", None
    return head, bool(dirty_text.strip())


def _relative_or_absolute(path: Path, parent: Path) -> str:
    try:
        return path.relative_to(parent).as_posix()
    except ValueError:
        return str(path)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_new(path: Path, payload: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_text_new(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


if __name__ == "__main__":
    sys.exit(main())
