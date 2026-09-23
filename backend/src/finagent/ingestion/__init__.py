"""文档解析。当前只提供文本型 PDF 的逐页文字与坐标。"""

from finagent.ingestion.errors import PdfFormatError, PdfInputError, PdfParseError
from finagent.ingestion.parse_text_pdf import parse_text_pdf

__all__ = [
    "PdfFormatError",
    "PdfInputError",
    "PdfParseError",
    "parse_text_pdf",
]
