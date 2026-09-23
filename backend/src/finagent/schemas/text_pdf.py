"""文本型 PDF 逐页解析结果。

只描述可从 PDF 直接提取的文字块和位置，不包含财务字段或风险结论。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

COORDINATE_SYSTEM = "pymupdf_page_top_left"
COORDINATE_UNIT = "pdf_point"
COORDINATE_NOTE = (
    "文字块边界框来自 PyMuPDF get_text('blocks')。"
    "提取时页面旋转按 0 度处理，边界框使用未旋转页面坐标："
    "原点在左上角，x 向右、y 向下，单位为 PDF point（1/72 英寸）。"
    "结果中的 width 和 height 取当前 page.rect；旋转为 90 或 270 度时，"
    "显示宽高与未旋转页面对调，不能用这组宽高解释文字块边界框。"
)
NO_TEXT_LIMITATION = (
    "本页没有可直接提取的文字。空白页、矢量图形和图片都不会被转写；"
    "本增量不执行 OCR，也不编造文字。"
)
IMAGE_WITH_TEXT_LIMITATION = (
    "本页文字块已提取。图片未执行 OCR，图片中的文字不会写入文字块。"
)

PageTextStatus = Literal["extracted", "no_extractable_text"]


@dataclass(frozen=True, slots=True)
class PdfBBox:
    """页面坐标中的矩形边界框。"""

    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True, slots=True)
class PdfTextBlock:
    """一页中的一个文字块。block_index 只计保留下来的文字块，从 0 起。"""

    block_index: int
    text: str
    bbox: PdfBBox


@dataclass(frozen=True, slots=True)
class PdfPageText:
    """单页解析结果。page_number 是 PDF 的 1-based 页序号，不是印刷页码。"""

    page_number: int
    width: float
    height: float
    rotation: int
    status: PageTextStatus
    image_block_count: int
    limitation: str | None
    blocks: tuple[PdfTextBlock, ...]


@dataclass(frozen=True, slots=True)
class ParsedTextPdf:
    """一份文本型 PDF 的解析结果，可序列化为 JSON。"""

    document_id: str
    source_sha256: str
    source_filename: str
    page_count: int
    coordinate_system: str
    coordinate_unit: str
    coordinate_note: str
    ocr_applied: bool
    extractor: str
    extractor_version: str
    pages: tuple[PdfPageText, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
