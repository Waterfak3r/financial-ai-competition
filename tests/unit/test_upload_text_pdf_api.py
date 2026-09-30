"""文本 PDF 上传接口。使用临时目录和内存 PDF，不读取真实财报，也不调用模型。"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

import finagent.ingestion.upload_text_pdf as upload_module
from finagent.api.app import create_app


def _pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "upload sample")
    payload = document.tobytes()
    document.close()
    return payload


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root))


def _post(client: TestClient, payload: bytes, **fields):
    data = {"company_id": "603288", "report_year": "2024"}
    data.update(fields)
    return client.post(
        "/v1/text-pdf-uploads",
        data=data,
        files={"file": ("note.pdf", payload, "application/pdf")},
    )


def test_upload_saves_hash_and_precheck_can_use_paths(tmp_path: Path) -> None:
    payload = _pdf()
    client = _client(tmp_path)
    created = _post(client, payload)
    assert created.status_code == 201
    body = created.json()
    assert body["page_count"] == 1
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["source_pdf_path"].endswith("/source.pdf")
    assert body["parsed_path"].endswith("/text_pdf.json")
    raw = tmp_path / "data" / "raw" / body["source_pdf_path"]
    source_record = json.loads((raw.parent / "source.json").read_text(encoding="utf-8"))
    parsed = tmp_path / "data" / "processed" / body["parsed_path"]
    assert raw.read_bytes() == payload
    assert body["sha256"] in parsed.read_text(encoding="utf-8")
    assert source_record["document_id"] == body["document_id"]
    assert source_record["company_id"] == "603288"
    assert source_record["report_period"] == "2024-12-31"
    assert source_record["local_path"] == f"data/raw/{body['source_pdf_path']}"
    assert source_record["sha256"] == body["sha256"]
    assert "未独立核实" in source_record["nature"]
    precheck = client.post(
        "/v1/annual-prechecks",
        json={
            "parsed_path": body["parsed_path"],
            "source_pdf_path": body["source_pdf_path"],
            "company_id": "603288",
            "report_year": 2024,
        },
    )
    assert precheck.status_code == 201
    assert precheck.json()["inputs"]["hashes_match"] is True
    assert precheck.json()["inputs"]["source_pdf_sha256"] == body["sha256"]
    assert precheck.json()["model_called"] is False


def _image_only_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4), 1)
    pixmap.clear_with(255)
    page.insert_image(page.rect, pixmap=pixmap)
    payload = document.tobytes()
    document.close()
    return payload


def test_success_writes_processing_note(tmp_path: Path) -> None:
    payload = _pdf()
    created = _post(_client(tmp_path), payload, company_id="603288")
    assert created.status_code == 201
    body = created.json()
    note_path = (tmp_path / "data" / "processed" / body["parsed_path"]).with_name("processing.md")
    note = note_path.read_text(encoding="utf-8")
    raw_relative = f"data/raw/{body['source_pdf_path']}"
    assert raw_relative in note
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["sha256"] in note
    assert f"pymupdf {pymupdf.version[0]}" in note
    assert "OCR：未执行" in note
    assert "文本型 PDF" in note
    assert "note.pdf" not in note
    assert str(tmp_path) not in note
    assert note_path.parent == (tmp_path / "data" / "processed" / body["parsed_path"]).parent


def test_image_only_pdf_returns_422_and_rolls_back(tmp_path: Path) -> None:
    marker = tmp_path / "data" / "raw" / "603288" / "2024" / "marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("stay", encoding="utf-8")
    failed = _post(_client(tmp_path), _image_only_pdf())
    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "unsupported_text_pdf"
    assert "没有可提取文字" in failed.json()["detail"]["message"]
    assert marker.read_text(encoding="utf-8") == "stay"
    assert list(marker.parent.iterdir()) == [marker]
    assert not (tmp_path / "data" / "processed").exists()


def test_second_upload_does_not_overwrite(tmp_path: Path) -> None:
    kept = tmp_path / "data" / "raw" / "603288" / "2024" / "kept.txt"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"keep-me")
    client = _client(tmp_path)
    first = _post(client, _pdf())
    second = _post(client, _pdf())
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["document_id"] != second.json()["document_id"]
    assert kept.read_bytes() == b"keep-me"
    assert (tmp_path / "data" / "raw" / first.json()["source_pdf_path"]).is_file()
    assert (tmp_path / "data" / "raw" / second.json()["source_pdf_path"]).is_file()


def test_rejects_empty_non_pdf_year_company_and_oversize(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    empty = _post(client, b"")
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "empty_file"
    not_pdf = _post(client, b"hello")
    assert not_pdf.status_code == 400
    assert not_pdf.json()["detail"]["code"] == "invalid_pdf"
    bad_company = _post(client, _pdf(), company_id="../raw")
    assert bad_company.status_code == 400
    assert bad_company.json()["detail"]["code"] == "invalid_company"
    bad_year = _post(client, _pdf(), report_year="1899")
    assert bad_year.status_code == 400
    assert bad_year.json()["detail"]["code"] == "invalid_year"
    monkeypatch.setattr(upload_module, "MAX_UPLOAD_BYTES", 8)
    huge = _post(client, b"%PDF" + b"x" * 16)
    assert huge.status_code == 413
    assert huge.json()["detail"]["code"] == "payload_too_large"
    assert list((tmp_path / "data").glob("**/*")) == []


def test_parse_failure_rolls_back_new_files(tmp_path: Path) -> None:
    marker = tmp_path / "data" / "raw" / "603288" / "2024" / "marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("stay", encoding="utf-8")
    client = _client(tmp_path)
    failed = _post(client, b"%PDF-1.4\nbroken")
    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "parse_failed"
    assert marker.read_text(encoding="utf-8") == "stay"
    assert list(marker.parent.iterdir()) == [marker]
    assert not (tmp_path / "data" / "processed").exists()


def _link_dir(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not link.exists():
            pytest.skip("当前环境不能创建目录符号链接或联接")


def test_rejects_data_symlink_that_resolves_outside_project(tmp_path: Path) -> None:
    outside_raw = tmp_path / "outside-raw"
    outside_processed = tmp_path / "outside-processed"
    outside_raw.mkdir()
    outside_processed.mkdir()
    (outside_raw / "marker.txt").write_text("stay-raw", encoding="utf-8")
    (outside_processed / "marker.txt").write_text("stay-processed", encoding="utf-8")
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    _link_dir(project / "data" / "raw", outside_raw)
    _link_dir(project / "data" / "processed", outside_processed)
    failed = _post(_client(project), _pdf())
    assert failed.status_code == 400
    assert failed.json()["detail"]["code"] == "invalid_path"
    assert (outside_raw / "marker.txt").read_text(encoding="utf-8") == "stay-raw"
    assert (outside_processed / "marker.txt").read_text(encoding="utf-8") == "stay-processed"
    assert {path.name for path in outside_raw.iterdir()} == {"marker.txt"}
    assert {path.name for path in outside_processed.iterdir()} == {"marker.txt"}


def test_partial_write_failure_removes_new_file_only(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "data" / "raw" / "603288" / "2024" / "marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"keep-me")
    real_open = Path.open

    def open_and_fail_new_pdf(self: Path, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if self.name == "source.pdf" and "x" in str(mode):
            original = handle.write

            def fail(data):
                original(data[:1] if data else data)
                raise OSError("simulated partial write")

            handle.write = fail
        return handle

    monkeypatch.setattr(Path, "open", open_and_fail_new_pdf)
    with pytest.raises(OSError, match="simulated partial write"):
        upload_module.save_text_pdf_upload(
            tmp_path,
            company_id="603288",
            report_year=2024,
            stream=io.BytesIO(_pdf()),
        )
    assert marker.read_bytes() == b"keep-me"
    assert list(marker.parent.iterdir()) == [marker]
    assert not (tmp_path / "data" / "processed").exists()
