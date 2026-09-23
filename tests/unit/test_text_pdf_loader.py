"""ParsedTextPdf JSON 往返与拒绝残缺对象。"""

from __future__ import annotations

import json

import pytest

from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox, PdfPageText, PdfTextBlock


def _parsed() -> ParsedTextPdf:
    page = PdfPageText(
        page_number=1,
        width=300,
        height=200,
        rotation=0,
        status="extracted",
        image_block_count=0,
        limitation=None,
        blocks=(PdfTextBlock(0, "营业收入", PdfBBox(1, 2, 3, 4)),),
    )
    return ParsedTextPdf(
        document_id="doc-1",
        source_sha256="a" * 64,
        source_filename="sample.pdf",
        page_count=1,
        coordinate_system="pymupdf_page_top_left",
        coordinate_unit="pdf_point",
        coordinate_note="note",
        ocr_applied=False,
        extractor="pymupdf",
        extractor_version="1.27.2.3",
        pages=(page,),
    )


def test_from_dict_roundtrip_keeps_text_and_bbox() -> None:
    original = _parsed()
    loaded = ParsedTextPdf.from_dict(json.loads(original.to_json()))
    assert loaded == original
    assert loaded.pages[0].blocks[0].bbox.x0 == 1
    assert loaded.pages[0].blocks[0].text == "营业收入"


def test_from_dict_rejects_incomplete_or_inconsistent_payload() -> None:
    payload = _parsed().to_dict()
    payload["page_count"] = 2
    with pytest.raises(ValueError, match="page_count"):
        ParsedTextPdf.from_dict(payload)
    payload = _parsed().to_dict()
    payload["pages"][0]["status"] = "ocr"
    with pytest.raises(ValueError, match="status"):
        ParsedTextPdf.from_dict(payload)
    payload = _parsed().to_dict()
    del payload["pages"][0]["blocks"][0]["bbox"]
    with pytest.raises(ValueError, match="bbox"):
        ParsedTextPdf.from_dict(payload)
    payload = _parsed().to_dict()
    payload["source_sha256"] = "ABC"
    with pytest.raises(ValueError, match="source_sha256"):
        ParsedTextPdf.from_dict(payload)
