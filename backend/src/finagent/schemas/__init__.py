"""跨模块数据结构。当前只包含文本型 PDF 解析结果。"""

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

__all__ = [
    "COORDINATE_NOTE",
    "COORDINATE_SYSTEM",
    "COORDINATE_UNIT",
    "IMAGE_WITH_TEXT_LIMITATION",
    "NO_TEXT_LIMITATION",
    "ParsedTextPdf",
    "PdfBBox",
    "PdfPageText",
    "PdfTextBlock",
]
