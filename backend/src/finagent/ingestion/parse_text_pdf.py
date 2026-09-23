"""把文本型 PDF 解析为逐页文字块。

不修改原文件，不执行 OCR，不抽取财务字段。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from finagent.ingestion.errors import PdfFormatError, PdfInputError, PdfParseError
from finagent.schemas.text_pdf import (
    COORDINATE_NOTE,
    COORDINATE_SYSTEM,
    COORDINATE_UNIT,
    IMAGE_WITH_TEXT_LIMITATION,
    NO_TEXT_LIMITATION,
    ParsedTextPdf,
    PdfBBox,
    PdfPageText,
    PdfTextBlock,
)

_MAX_DOCUMENT_ID_LENGTH = 128


def parse_text_pdf(path: str | Path, document_id: str) -> ParsedTextPdf:
    """解析文本型 PDF，返回可 JSON 序列化的逐页文字块。

    只读入文件字节并在内存中打开，不会回写原 PDF，也不会把结果写入
    data/processed/。空白页和图片页不会生成文字。
    """

    checked_id = _validate_document_id(document_id)
    file_path, data = _read_source(path)
    pymupdf = _load_pymupdf()
    document = _open_document(pymupdf, data)
    try:
        pages = tuple(
            _parse_page(pymupdf, document[index], index + 1)
            for index in range(document.page_count)
        )
    finally:
        document.close()
    return ParsedTextPdf(
        document_id=checked_id,
        source_sha256=hashlib.sha256(data).hexdigest(),
        source_filename=file_path.name,
        page_count=len(pages),
        coordinate_system=COORDINATE_SYSTEM,
        coordinate_unit=COORDINATE_UNIT,
        coordinate_note=COORDINATE_NOTE,
        ocr_applied=False,
        extractor="pymupdf",
        extractor_version=_extractor_version(pymupdf),
        pages=pages,
    )


def _validate_document_id(document_id: object) -> str:
    if not isinstance(document_id, str):
        raise PdfInputError("document_id 必须是字符串。")
    if document_id == "" or document_id.strip() == "":
        raise PdfInputError("document_id 不能为空。")
    if document_id != document_id.strip():
        raise PdfInputError("document_id 首尾不能包含空白。")
    if len(document_id) > _MAX_DOCUMENT_ID_LENGTH:
        raise PdfInputError("document_id 长度不能超过 128。")
    if any(ord(char) < 32 for char in document_id) or "/" in document_id or "\\" in document_id:
        raise PdfInputError("document_id 不能包含控制字符或路径分隔符。")
    if document_id in {".", ".."}:
        raise PdfInputError("document_id 不能是 . 或 ..。")
    return document_id


def _read_source(path: object) -> tuple[Path, bytes]:
    if isinstance(path, (bytes, bytearray)):
        raise PdfInputError("请传入 PDF 文件路径，不接受文件字节。")
    if not isinstance(path, (str, Path)):
        raise PdfInputError("path 必须是文件路径。")
    if isinstance(path, str) and path.strip() == "":
        raise PdfInputError("path 不能为空。")
    file_path = Path(path)
    try:
        if not file_path.exists():
            raise PdfInputError(f"找不到 PDF 文件：{file_path}")
        if file_path.is_dir():
            raise PdfInputError(f"路径是目录，不是 PDF 文件：{file_path}")
        if not file_path.is_file():
            raise PdfInputError(f"路径不是常规文件：{file_path}")
        data = file_path.read_bytes()
    except PdfInputError:
        raise
    except OSError as exc:
        raise PdfInputError(f"无法读取 PDF 文件：{file_path}") from exc
    return file_path, data


def _load_pymupdf() -> Any:
    try:
        import pymupdf
    except ImportError as exc:
        raise PdfParseError(
            "未安装 PyMuPDF（导入名 pymupdf）。请先按 README 安装 backend 依赖。"
        ) from exc
    return pymupdf


def _extractor_version(pymupdf: Any) -> str:
    """返回 PyMuPDF 自身版本。

    pymupdf.version 为 (PyMuPDF, MuPDF, None)，例如 ('1.27.2.3', '1.27.2', None)。
    只取下标 0，不取下标 1 的 MuPDF 版本。
    """

    version = getattr(pymupdf, "version", None)
    if isinstance(version, tuple) and version and version[0]:
        return str(version[0])
    if isinstance(version, str) and version:
        return version
    return "unknown"


def _open_document(pymupdf: Any, data: bytes) -> Any:
    if not data:
        raise PdfFormatError("文件为空，不是可解析的 PDF。")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfFormatError("无法作为 PDF 打开。文件可能已损坏或不是 PDF。") from exc
    if document.needs_pass:
        document.close()
        raise PdfFormatError(
            "PDF 已加密或需要打开密码。本增量不解密，也不会把加密页当成无文字页。"
        )
    return document


def _parse_page(pymupdf: Any, page: Any, page_number: int) -> PdfPageText:
    try:
        # blocks 只返回坐标、文字和 block_type，不把图片字节放进结果。
        # TEXT_PRESERVE_IMAGES 让图片块以类型 1 出现，便于计数。
        flags = pymupdf.TEXTFLAGS_BLOCKS | pymupdf.TEXT_PRESERVE_IMAGES
        raw_blocks = page.get_text("blocks", flags=flags)
    except Exception as exc:
        raise PdfFormatError(
            f"无法读取第 {page_number} 页。文件可能已加密或已损坏。"
        ) from exc
    image_block_count = 0
    blocks: list[PdfTextBlock] = []
    for raw_block in raw_blocks:
        if len(raw_block) < 7:
            raise PdfFormatError(f"第 {page_number} 页文字块结构无法识别。")
        x0, y0, x1, y1, text, _block_no, block_type = raw_block[:7]
        if block_type == 1:
            image_block_count += 1
            continue
        if block_type != 0:
            continue
        normalized = _normalize_block_text(text)
        if normalized.strip() == "":
            continue
        blocks.append(
            PdfTextBlock(
                block_index=len(blocks),
                text=normalized,
                bbox=PdfBBox(
                    x0=_round_point(x0),
                    y0=_round_point(y0),
                    x1=_round_point(x1),
                    y1=_round_point(y1),
                ),
            )
        )
    if blocks:
        status = "extracted"
        limitation = IMAGE_WITH_TEXT_LIMITATION if image_block_count else None
    else:
        status = "no_extractable_text"
        limitation = NO_TEXT_LIMITATION
    rect = page.rect
    return PdfPageText(
        page_number=page_number,
        width=_round_point(rect.width),
        height=_round_point(rect.height),
        rotation=int(page.rotation),
        status=status,
        image_block_count=image_block_count,
        limitation=limitation,
        blocks=tuple(blocks),
    )


def _normalize_block_text(text: object) -> str:
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip("\n")


def _round_point(value: float) -> float:
    return round(float(value), 4)
