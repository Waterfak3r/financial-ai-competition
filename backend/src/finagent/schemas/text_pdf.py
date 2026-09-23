"""文本型 PDF 逐页解析结果。

只描述可从 PDF 直接提取的文字块和位置，不包含财务字段或风险结论。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ParsedTextPdf:
        """从解析 JSON 对象重建结果。缺字段或类型不对时拒绝，不补默认页。"""

        if not isinstance(data, Mapping):
            raise ValueError("解析结果必须是 JSON 对象。")
        required = (
            "document_id",
            "source_sha256",
            "source_filename",
            "page_count",
            "coordinate_system",
            "coordinate_unit",
            "coordinate_note",
            "ocr_applied",
            "extractor",
            "extractor_version",
            "pages",
        )
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError("解析结果缺少字段：" + "、".join(missing))
        pages_raw = data["pages"]
        if isinstance(pages_raw, str) or not isinstance(pages_raw, Sequence):
            raise ValueError("pages 必须是数组。")
        pages = tuple(_page_from_dict(item, index) for index, item in enumerate(pages_raw))
        page_count = _as_int(data["page_count"], "page_count")
        if page_count != len(pages):
            raise ValueError("page_count 与 pages 长度不一致。")
        if not isinstance(data["ocr_applied"], bool):
            raise ValueError("ocr_applied 必须是布尔值。")
        return cls(
            document_id=_as_text(data["document_id"], "document_id"),
            source_sha256=_as_sha256(data["source_sha256"]),
            source_filename=_as_text(data["source_filename"], "source_filename"),
            page_count=page_count,
            coordinate_system=_as_text(data["coordinate_system"], "coordinate_system"),
            coordinate_unit=_as_text(data["coordinate_unit"], "coordinate_unit"),
            coordinate_note=_as_text(data["coordinate_note"], "coordinate_note"),
            ocr_applied=data["ocr_applied"],
            extractor=_as_text(data["extractor"], "extractor"),
            extractor_version=_as_text(data["extractor_version"], "extractor_version"),
            pages=pages,
        )


def _page_from_dict(data: Mapping[str, Any], index: int) -> PdfPageText:
    if not isinstance(data, Mapping):
        raise ValueError(f"pages[{index}] 必须是对象。")
    status = data.get("status")
    if status not in ("extracted", "no_extractable_text"):
        raise ValueError(f"pages[{index}].status 无法识别。")
    blocks_raw = data.get("blocks")
    if isinstance(blocks_raw, str) or not isinstance(blocks_raw, Sequence):
        raise ValueError(f"pages[{index}].blocks 必须是数组。")
    limitation = data.get("limitation")
    if limitation is not None and not isinstance(limitation, str):
        raise ValueError(f"pages[{index}].limitation 必须是字符串或 null。")
    return PdfPageText(
        page_number=_as_int(data.get("page_number"), f"pages[{index}].page_number", minimum=1),
        width=_as_float(data.get("width"), f"pages[{index}].width"),
        height=_as_float(data.get("height"), f"pages[{index}].height"),
        rotation=_as_int(data.get("rotation"), f"pages[{index}].rotation"),
        status=status,
        image_block_count=_as_int(data.get("image_block_count"), f"pages[{index}].image_block_count"),
        limitation=limitation,
        blocks=tuple(_block_from_dict(item, index, block_index) for block_index, item in enumerate(blocks_raw)),
    )


def _block_from_dict(data: Mapping[str, Any], page_index: int, block_index: int) -> PdfTextBlock:
    label = f"pages[{page_index}].blocks[{block_index}]"
    if not isinstance(data, Mapping):
        raise ValueError(f"{label} 必须是对象。")
    bbox = data.get("bbox")
    if not isinstance(bbox, Mapping):
        raise ValueError(f"{label}.bbox 必须是对象。")
    return PdfTextBlock(
        block_index=_as_int(data.get("block_index"), f"{label}.block_index"),
        text=_as_text(data.get("text"), f"{label}.text"),
        bbox=PdfBBox(
            x0=_as_float(bbox.get("x0"), f"{label}.bbox.x0"),
            y0=_as_float(bbox.get("y0"), f"{label}.bbox.y0"),
            x1=_as_float(bbox.get("x1"), f"{label}.bbox.x1"),
            y1=_as_float(bbox.get("y1"), f"{label}.bbox.y1"),
        ),
    )


def _as_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{name} 必须是非空字符串。")
    return value


def _as_sha256(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("source_sha256 必须是 64 位小写十六进制字符串。")
    return value


def _as_int(value: Any, name: str, minimum: int | None = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数。")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}。")
    return value


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字。")
    return float(value)
