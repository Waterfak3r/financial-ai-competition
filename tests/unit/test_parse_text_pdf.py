"""用代码生成的小型 PDF 验证逐页解析，不使用真实财报。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from finagent.ingestion import PdfFormatError, PdfInputError, parse_text_pdf
from finagent.ingestion.parse_text_pdf import _extractor_version

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _pymupdf():
    return pytest.importorskip("pymupdf")


def _save(document, path: Path) -> Path:
    document.save(path)
    document.close()
    return path


def test_pages_text_coordinates_hash_and_json(tmp_path: Path) -> None:
    pymupdf = _pymupdf()
    path = tmp_path / "pages.pdf"
    document = pymupdf.open()
    first = document.new_page(width=300, height=200)
    first.insert_text((72, 80), "Revenue 100", fontsize=12)
    document.new_page(width=300, height=200)
    image_page = document.new_page(width=300, height=200)
    image_page.insert_image(pymupdf.Rect(10, 10, 40, 40), stream=PNG_1X1)
    _save(document, path)
    original = path.read_bytes()

    parsed = parse_text_pdf(path, "sample-pages")

    assert path.read_bytes() == original
    assert parsed.document_id == "sample-pages"
    assert parsed.source_filename == "pages.pdf"
    assert parsed.source_sha256 == hashlib.sha256(original).hexdigest()
    assert len(parsed.source_sha256) == 64
    assert parsed.page_count == 3
    assert [page.page_number for page in parsed.pages] == [1, 2, 3]
    assert parsed.ocr_applied is False
    assert parsed.extractor == "pymupdf"
    assert parsed.coordinate_system == "pymupdf_page_top_left"

    text_page = parsed.pages[0]
    assert text_page.status == "extracted"
    assert text_page.width == 300
    assert text_page.height == 200
    assert text_page.limitation is None
    assert len(text_page.blocks) == 1
    block = text_page.blocks[0]
    assert block.block_index == 0
    assert block.text == "Revenue 100"
    assert block.bbox.x0 == pytest.approx(72.0, abs=0.2)
    assert block.bbox.y0 == pytest.approx(67.1, abs=0.5)
    assert block.bbox.y1 == pytest.approx(83.588, abs=0.5)
    assert block.bbox.y0 < 80 < block.bbox.y1
    assert block.bbox.x1 > block.bbox.x0

    blank = parsed.pages[1]
    assert blank.status == "no_extractable_text"
    assert blank.blocks == ()
    assert blank.image_block_count == 0
    assert blank.limitation is not None
    assert "OCR" in blank.limitation
    assert "不编造" in blank.limitation

    image_only = parsed.pages[2]
    assert image_only.status == "no_extractable_text"
    assert image_only.blocks == ()
    assert image_only.image_block_count == 1
    assert image_only.limitation is not None
    assert "图片" in image_only.limitation

    payload = json.loads(parsed.to_json())
    assert payload["source_sha256"] == parsed.source_sha256
    assert payload["pages"][0]["blocks"][0]["text"] == "Revenue 100"
    assert payload["pages"][1]["blocks"] == []
    assert payload["ocr_applied"] is False


def test_block_order_and_chinese_text(tmp_path: Path) -> None:
    pymupdf = _pymupdf()
    path = tmp_path / "blocks.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((40, 50), "First", fontsize=12)
    page.insert_text((40, 90), "Second", fontsize=12)
    cjk = document.new_page(width=300, height=200)
    cjk.insert_text((50, 60), "营业收入100", fontname="china-s", fontsize=12)
    _save(document, path)

    parsed = parse_text_pdf(str(path), "样例-001")

    blocks = parsed.pages[0].blocks
    assert [block.text for block in blocks] == ["First", "Second"]
    assert [block.block_index for block in blocks] == [0, 1]
    assert blocks[1].bbox.y0 > blocks[0].bbox.y0
    assert blocks[0].bbox.x0 == pytest.approx(40.0, abs=0.2)

    cjk_block = parsed.pages[1].blocks[0]
    assert cjk_block.text == "营业收入100"
    assert cjk_block.bbox.x0 == pytest.approx(50.0, abs=0.2)
    assert cjk_block.bbox.y0 < 60 < cjk_block.bbox.y1


def test_whitespace_and_mixed_image_are_not_invented(tmp_path: Path) -> None:
    pymupdf = _pymupdf()
    path = tmp_path / "mixed.pdf"
    document = pymupdf.open()
    spaces = document.new_page(width=200, height=200)
    spaces.insert_text((72, 72), "   ", fontsize=12)
    mixed = document.new_page(width=200, height=200)
    mixed.insert_text((30, 40), "HasText", fontsize=12)
    mixed.insert_image(pymupdf.Rect(10, 80, 40, 110), stream=PNG_1X1)
    _save(document, path)

    parsed = parse_text_pdf(path, "mixed-001")

    assert parsed.pages[0].status == "no_extractable_text"
    assert parsed.pages[0].blocks == ()
    assert parsed.pages[1].status == "extracted"
    assert parsed.pages[1].blocks[0].text == "HasText"
    assert parsed.pages[1].image_block_count == 1
    assert parsed.pages[1].limitation is not None
    assert "OCR" in parsed.pages[1].limitation


@pytest.mark.parametrize(
    ("document_id", "message"),
    [
        ("", "不能为空"),
        ("   ", "不能为空"),
        (" doc", "首尾"),
        ("a/b", "路径分隔符"),
        ("a\\b", "路径分隔符"),
        ("..", "不能是"),
        ("x" * 129, "128"),
    ],
)
def test_rejects_invalid_document_id(tmp_path: Path, document_id: str, message: str) -> None:
    with pytest.raises(PdfInputError, match=message):
        parse_text_pdf(tmp_path / "unused.pdf", document_id)


def test_rejects_non_string_document_id(tmp_path: Path) -> None:
    with pytest.raises(PdfInputError, match="字符串"):
        parse_text_pdf(tmp_path / "unused.pdf", 12)  # type: ignore[arg-type]


def test_rejects_missing_directory_empty_and_non_pdf(tmp_path: Path) -> None:
    with pytest.raises(PdfInputError, match="找不到"):
        parse_text_pdf(tmp_path / "missing.pdf", "doc-1")
    with pytest.raises(PdfInputError, match="目录"):
        parse_text_pdf(tmp_path, "doc-1")
    with pytest.raises(PdfInputError, match="文件路径"):
        parse_text_pdf(b"%PDF-1.4", "doc-1")  # type: ignore[arg-type]

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(PdfFormatError, match="为空"):
        parse_text_pdf(empty, "doc-1")

    garbage = tmp_path / "notes.pdf"
    original = b"this is not a pdf"
    garbage.write_bytes(original)
    with pytest.raises(PdfFormatError, match="不是 PDF"):
        parse_text_pdf(garbage, "doc-1")
    assert garbage.read_bytes() == original


def test_extractor_version_uses_version_tuple_index_zero(tmp_path: Path) -> None:
    fake = SimpleNamespace(version=("package-version", "mupdf-version", None))
    assert _extractor_version(fake) == "package-version"
    assert _extractor_version(fake) != "mupdf-version"

    pymupdf = _pymupdf()
    path = tmp_path / "version.pdf"
    document = pymupdf.open()
    document.new_page(width=100, height=100)
    _save(document, path)

    parsed = parse_text_pdf(path, "version-001")
    assert parsed.extractor_version == str(pymupdf.version[0])


def test_rotated_page_bbox_stays_in_unrotated_space(tmp_path: Path) -> None:
    pymupdf = _pymupdf()
    path = tmp_path / "rotated.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((72, 80), "Revenue 100", fontsize=12)
    page.set_rotation(90)
    _save(document, path)

    parsed = parse_text_pdf(path, "rotated-001")
    result = parsed.pages[0]
    block = result.blocks[0]

    assert result.rotation == 90
    assert result.width == 200
    assert result.height == 300
    assert block.text == "Revenue 100"
    assert block.bbox.x0 == pytest.approx(72.0, abs=0.2)
    assert block.bbox.y0 == pytest.approx(67.1, abs=0.5)
    assert block.bbox.x1 == pytest.approx(143.376, abs=0.5)
    assert "未旋转" in parsed.coordinate_note
    assert "page.rect" in parsed.coordinate_note


def test_rejects_encrypted_pdf_without_treating_it_as_blank(tmp_path: Path) -> None:
    pymupdf = _pymupdf()
    path = tmp_path / "locked.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "secret", fontsize=12)
    document.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="pw",
        owner_pw="ownerpw",
    )
    document.close()
    original = path.read_bytes()

    with pytest.raises(PdfFormatError, match="加密"):
        parse_text_pdf(path, "locked-001")
    assert path.read_bytes() == original
