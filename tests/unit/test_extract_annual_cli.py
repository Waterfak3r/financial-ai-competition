"""年度事实命令：加载解析 JSON、核对原文哈希、独占写入运行目录。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox, PdfPageText, PdfTextBlock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "extract_annual_facts.py"


def _cli():
    spec = importlib.util.spec_from_file_location("extract_annual_facts_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _parsed(sha256: str) -> ParsedTextPdf:
    page = PdfPageText(
        page_number=1,
        width=300,
        height=200,
        rotation=0,
        status="extracted",
        image_block_count=0,
        limitation=None,
        blocks=(
            PdfTextBlock(0, "合并利润表", PdfBBox(0, 0, 40, 10)),
            PdfTextBlock(1, "2024 年1—12 月", PdfBBox(0, 12, 80, 22)),
            PdfTextBlock(2, "单位：元  币种：人民币", PdfBBox(0, 24, 100, 34)),
            PdfTextBlock(3, "项目 2024 年度 2023 年度", PdfBBox(0, 36, 140, 46)),
            PdfTextBlock(4, "一、营业收入\n150.00 100.00", PdfBBox(0, 48, 160, 70)),
        ),
    )
    return ParsedTextPdf(
        document_id="doc-cli",
        source_sha256=sha256,
        source_filename="sample.pdf",
        page_count=1,
        coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point",
        coordinate_note="test",
        ocr_applied=False,
        extractor="test",
        extractor_version="0",
        pages=(page,),
    )


def test_cli_writes_three_files_and_checks_source_hash(tmp_path: Path) -> None:
    pdf_bytes = b"%PDF-1.4 synthetic"
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(pdf_bytes)
    parsed_path = tmp_path / "text_pdf.json"
    parsed_path.write_text(_parsed(sha256).to_json(), encoding="utf-8")
    run_root = tmp_path / "runs"
    code = _cli().main(
        [
            "--parsed",
            str(parsed_path),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--run-root",
            str(run_root),
            "--source-pdf",
            str(pdf_path),
        ]
    )
    assert code == 0
    run_dirs = list(run_root.iterdir())
    assert len(run_dirs) == 1
    summary = json.loads((run_dirs[0] / "summary.json").read_text(encoding="utf-8"))
    facts = json.loads((run_dirs[0] / "facts.json").read_text(encoding="utf-8"))
    calculation = json.loads((run_dirs[0] / "calculation.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed_with_issues"
    assert summary["inputs"]["source_pdf_matches_parsed"] is True
    assert summary["inputs"]["source_pdf_sha256"] == sha256
    assert summary["formula"].startswith("(本期规范值-上期规范值)")
    assert len(summary["code"]["git_head"]) >= 7
    assert summary["code"]["source_file_sha256"]["scripts/extract_annual_facts.py"]
    assert facts["facts"][0]["normalized_value"] == "150.00"
    assert calculation["changes"][0]["difference"] == "50.00"
    assert "不是独立原文核验" in summary["note"]
    before = (run_dirs[0] / "facts.json").read_bytes()
    again = _cli().main(
        [
            "--parsed",
            str(parsed_path),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--run-root",
            str(run_root),
        ]
    )
    assert again == 0
    assert (run_dirs[0] / "facts.json").read_bytes() == before
    assert len(list(run_root.iterdir())) == 2


def test_cli_rejects_mismatched_pdf_without_facts(tmp_path: Path) -> None:
    sha256 = "b" * 64
    pdf_path = tmp_path / "other.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 other")
    parsed_path = tmp_path / "text_pdf.json"
    parsed_path.write_text(_parsed(sha256).to_json(), encoding="utf-8")
    run_root = tmp_path / "runs"
    code = _cli().main(
        [
            "--parsed",
            str(parsed_path),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--run-root",
            str(run_root),
            "--source-pdf",
            str(pdf_path),
        ]
    )
    assert code == 2
    run_dir = next(run_root.iterdir())
    assert not (run_dir / "facts.json").exists()
    assert not (run_dir / "calculation.json").exists()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["inputs"]["source_pdf_matches_parsed"] is False


def test_cli_refuses_run_directory_inside_raw(tmp_path: Path) -> None:
    parsed_path = tmp_path / "text_pdf.json"
    parsed_path.write_text(_parsed("c" * 64).to_json(), encoding="utf-8")
    code = _cli().main(
        [
            "--parsed",
            str(parsed_path),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--run-root",
            str(ROOT / "data" / "raw" / "cli-should-not-write"),
        ]
    )
    assert code == 2
    assert not (ROOT / "data" / "raw" / "cli-should-not-write").exists()
