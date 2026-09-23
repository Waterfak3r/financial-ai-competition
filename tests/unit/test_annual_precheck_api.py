"""本地预检 API。使用临时目录，不读取真实财报，也不调用云端。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

import finagent.api.annual_precheck as annual_precheck
from finagent.api.app import create_app
from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox, PdfPageText, PdfTextBlock


def _parsed(sha256: str) -> ParsedTextPdf:
    page = PdfPageText(
        page_number=1,
        width=200,
        height=200,
        rotation=0,
        status="extracted",
        image_block_count=0,
        limitation=None,
        blocks=(
            PdfTextBlock(0, "合并利润表", PdfBBox(0, 0, 20, 10)),
            PdfTextBlock(1, "2024 年1—12 月", PdfBBox(0, 12, 40, 22)),
            PdfTextBlock(2, "单位：元  币种：人民币", PdfBBox(0, 24, 80, 34)),
            PdfTextBlock(3, "项目 2024 年度 2023 年度", PdfBBox(0, 36, 100, 46)),
            PdfTextBlock(4, "一、营业收入\n150.00 100.00", PdfBBox(0, 48, 120, 70)),
        ),
    )
    return ParsedTextPdf(
        document_id="doc-precheck",
        source_sha256=sha256,
        source_filename="a.pdf",
        page_count=1,
        coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point",
        coordinate_note="test",
        ocr_applied=False,
        extractor="test",
        extractor_version="0",
        pages=(page,),
    )


def _layout(root: Path, pdf_bytes: bytes, *, parsed_sha: str | None = None) -> None:
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    raw = root / "data" / "raw" / "sample" / "a.pdf"
    processed = root / "data" / "processed" / "sample" / "text_pdf.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    processed.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(pdf_bytes)
    processed.write_text(_parsed(parsed_sha or digest).to_json(), encoding="utf-8")


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root))


def _post(client: TestClient, **overrides):
    body = {
        "parsed_path": "sample/text_pdf.json",
        "source_pdf_path": "sample/a.pdf",
        "company_id": "603288",
        "report_year": 2024,
    }
    body.update(overrides)
    return client.post("/v1/annual-prechecks", json=body)


def test_post_and_get_keep_local_facts_without_model_or_verification(tmp_path: Path) -> None:
    _layout(tmp_path, b"%PDF-1.4 precheck")
    client = _client(tmp_path)
    created = _post(client)
    assert created.status_code == 201
    body = created.json()
    assert body["model_called"] is False
    assert body["independently_verified"] is False
    assert "未调用模型" in body["note"]
    assert "已确认舞弊" not in created.text
    assert "已独立核验" not in created.text
    assert body["inputs"]["hashes_match"] is True
    code = body["code"]
    assert code["git_available"] is False
    assert code["git_head"] is None
    assert code["git_dirty"] is None
    recorded = code["source_file_sha256"]["finagent.api.annual_precheck"]
    source = Path(annual_precheck.__file__)
    assert recorded == hashlib.sha256(source.read_bytes()).hexdigest()
    assert code["missing_source_files"] == []
    assert body["facts"]["facts"][0]["normalized_value"] == "150.00"
    assert body["calculation"]["changes"][0]["difference"] == "50.00"
    run_id = body["run_id"]
    fetched = client.get(f"/v1/annual-prechecks/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id
    assert fetched.json()["facts"]["facts"][0]["indicator_name"] == "营业收入"
    stored = (tmp_path / "artifacts" / "runs" / run_id / "precheck.json").read_bytes()
    second = _post(client)
    assert second.status_code == 201
    assert second.json()["run_id"] != run_id
    assert (tmp_path / "artifacts" / "runs" / run_id / "precheck.json").read_bytes() == stored


def test_rejects_path_escape_and_hash_mismatch(tmp_path: Path) -> None:
    _layout(tmp_path, b"%PDF-1.4 match")
    client = _client(tmp_path)
    escaped = _post(client, parsed_path="../raw/sample/a.pdf")
    assert escaped.status_code == 400
    assert escaped.json()["detail"]["code"] == "invalid_path"
    absolute = _post(client, source_pdf_path=str(tmp_path / "data" / "raw" / "sample" / "a.pdf"))
    assert absolute.status_code == 400
    _layout(tmp_path, b"%PDF-1.4 other-bytes", parsed_sha="a" * 64)
    mismatched = _post(client)
    assert mismatched.status_code == 409
    assert mismatched.json()["detail"]["code"] == "source_sha256_mismatch"
    assert "facts" not in mismatched.json()["detail"]
    assert not list((tmp_path / "artifacts" / "runs").glob("*/facts.json"))


def test_get_rejects_illegal_or_missing_run_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    missing = client.get("/v1/annual-prechecks/missing-run")
    assert missing.status_code == 404
    illegal = client.get("/v1/annual-prechecks/bad..id")
    assert illegal.status_code == 400
    assert illegal.json()["detail"]["code"] == "invalid_run_id"
    spaced = client.get("/v1/annual-prechecks/bad id")
    assert spaced.status_code == 400
