"""从已解析的 text_pdf.json 提取四项年度事实并计算同比。

不修改原始 PDF，不把程序计算写成独立原文核验。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from finagent.finance.annual_change import ANNUAL_CHANGE_FORMULA, calculate_annual_changes
from finagent.ingestion.errors import PdfInputError
from finagent.ingestion.extract_annual_facts import extract_annual_financial_facts
from finagent.schemas.text_pdf import ParsedTextPdf

_SOURCE_FILES = (
    "scripts/extract_annual_facts.py",
    "backend/src/finagent/schemas/text_pdf.py",
    "backend/src/finagent/schemas/financial_fact.py",
    "backend/src/finagent/ingestion/extract_annual_facts.py",
    "backend/src/finagent/finance/annual_change.py",
)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except (OSError, ValueError, PdfInputError, json.JSONDecodeError) as exc:
        print(f"error={exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提取四项年度事实并计算同比。")
    parser.add_argument("--parsed", required=True, type=Path, help="已有 text_pdf.json")
    parser.add_argument("--company-id", required=True, help="公司标识，例如股票代码")
    parser.add_argument("--report-year", required=True, type=int, help="报告年度")
    parser.add_argument("--run-root", required=True, type=Path, help="运行产物根目录，其下新建唯一 run_id")
    parser.add_argument("--source-pdf", type=Path, default=None, help="可选原始 PDF，用于核对 SHA256")
    return parser


def _run(args: argparse.Namespace) -> int:
    parsed_path = args.parsed
    if not parsed_path.is_file():
        raise ValueError(f"找不到解析文件：{parsed_path}")
    _reject_raw_target(args.run_root)
    parsed = ParsedTextPdf.from_dict(json.loads(parsed_path.read_text(encoding="utf-8")))
    source_pdf_sha256 = None
    if args.source_pdf is not None:
        if not args.source_pdf.is_file():
            raise ValueError(f"找不到原始 PDF：{args.source_pdf}")
        source_pdf_sha256 = _sha256(args.source_pdf)
        if source_pdf_sha256 != parsed.source_sha256:
            run_dir = _create_run_dir(args.run_root, args.company_id, args.report_year)
            _write_new(
                run_dir / "summary.json",
                _summary(
                    run_dir.name,
                    "failed",
                    args,
                    parsed,
                    parsed_path,
                    source_pdf_sha256,
                    False,
                    None,
                    None,
                    "原始 PDF 的 SHA256 与解析结果中的 source_sha256 不一致，未提取事实。",
                ),
            )
            print(f"status=failed run_id={run_dir.name}")
            return 2
    extracted = extract_annual_financial_facts(parsed, args.company_id, args.report_year)
    calculated = calculate_annual_changes(extracted, args.report_year)
    status = "completed" if not extracted.issues and not calculated.issues else "completed_with_issues"
    run_dir = _create_run_dir(args.run_root, args.company_id, args.report_year)
    _write_new(run_dir / "facts.json", extracted.to_json(indent=2) + "\n")
    _write_new(run_dir / "calculation.json", calculated.to_json(indent=2) + "\n")
    _write_new(
        run_dir / "summary.json",
        _summary(
            run_dir.name,
            status,
            args,
            parsed,
            parsed_path,
            source_pdf_sha256,
            True if source_pdf_sha256 is not None else None,
            extracted,
            calculated,
            "本运行是程序提取和同比计算，不是独立原文核验。",
        ),
    )
    print(f"status={status} run_id={run_dir.name} facts={len(extracted.facts)} changes={len(calculated.changes)}")
    return 0


def _summary(run_id, status, args, parsed, parsed_path, source_pdf_sha256, matches, extracted, calculated, note):
    head, dirty = _git_state()
    payload = {
        "run_id": run_id,
        "status": status,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "inputs": {
            "parsed_path": parsed_path.as_posix(),
            "parsed_file_sha256": _sha256(parsed_path),
            "document_id": parsed.document_id,
            "source_sha256": parsed.source_sha256,
            "company_id": args.company_id,
            "report_year": args.report_year,
            "source_pdf": None if args.source_pdf is None else args.source_pdf.as_posix(),
            "source_pdf_sha256": source_pdf_sha256,
            "source_pdf_matches_parsed": matches,
        },
        "code": {
            "git_head": head,
            "git_dirty": dirty,
            "source_file_sha256": {relative: _sha256(ROOT / relative) for relative in _SOURCE_FILES},
        },
        "issues": {
            "extraction": [] if extracted is None else [_issue_dict(issue) for issue in extracted.issues],
            "calculation": [] if calculated is None else [_issue_dict(issue) for issue in calculated.issues],
        },
        "formula": ANNUAL_CHANGE_FORMULA,
        "results": [] if calculated is None else [_change_brief(item) for item in calculated.changes],
        "fact_limitations": [] if extracted is None else sorted({note for fact in extracted.facts for note in fact.limitations}),
        "note": note,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _issue_dict(issue) -> dict:
    payload = {
        "code": issue.code,
        "message": issue.message,
        "indicator_name": issue.indicator_name,
    }
    table_name = getattr(issue, "table_name", None)
    if table_name is not None:
        payload["table_name"] = table_name
    return payload


def _change_brief(item) -> dict:
    return {
        "indicator_name": item.indicator_name,
        "report_year": item.report_year,
        "prior_year": item.prior_year,
        "current_value": item.current_value,
        "prior_value": item.prior_value,
        "difference": item.difference,
        "rate": item.rate,
        "currency": item.currency,
        "statement_scope": item.statement_scope,
        "source_sha256": item.source_sha256,
    }


def _create_run_dir(run_root: Path, company_id: str, report_year: int) -> Path:
    if "/" in company_id or "\\" in company_id or company_id.strip() in {"", ".", ".."}:
        raise ValueError("company_id 不能作为运行目录名。")
    run_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"annual-facts-{company_id}-{report_year}-{stamp}"
    for suffix in ("", "-2", "-3", "-4", "-5"):
        path = run_root / f"{base}{suffix}"
        try:
            path.mkdir(parents=False, exist_ok=False)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"运行目录已存在且未覆盖：{base}")


def _write_new(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _reject_raw_target(run_root: Path) -> None:
    raw = (ROOT / "data" / "raw").resolve()
    target = run_root.resolve()
    if target == raw or raw in target.parents:
        raise ValueError("run 目录不能位于 data/raw。")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_state() -> tuple[str, bool | None]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown", None
    return head, bool(dirty)


if __name__ == "__main__":
    sys.exit(main())
