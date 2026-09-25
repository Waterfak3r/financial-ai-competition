"""文档解析。当前提供文本型 PDF 逐页文字、上传归档，以及四项年度事实提取。"""

from finagent.ingestion.errors import PdfFormatError, PdfInputError, PdfParseError
from finagent.ingestion.extract_annual_facts import extract_annual_financial_facts
from finagent.ingestion.parse_text_pdf import parse_text_pdf
from finagent.ingestion.upload_text_pdf import MAX_UPLOAD_BYTES, UploadRejected, save_text_pdf_upload

__all__ = [
    "MAX_UPLOAD_BYTES",
    "PdfFormatError",
    "PdfInputError",
    "PdfParseError",
    "UploadRejected",
    "extract_annual_financial_facts",
    "parse_text_pdf",
    "save_text_pdf_upload",
]
