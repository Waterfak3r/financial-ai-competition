#!/usr/bin/env python3
"""Typeset the editable final-report Markdown as a Chinese PDF.

This is an authoring utility, not part of the FINTRACE application runtime.
"""

from __future__ import annotations

import argparse
import html
import math
import re
import sys
from pathlib import Path
from typing import Any

import mistune
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    LongTable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    TableStyle,
)
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "submission" / "final" / "FINTRACE-决赛技术报告.md"
DEFAULT_OUTPUT = ROOT / "submission" / "final" / "FINTRACE-决赛技术报告.pdf"
DEFAULT_REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")
DEFAULT_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")

INK = colors.HexColor("#1d2b3a")
MUTED = colors.HexColor("#526477")
ACCENT = colors.HexColor("#285d87")
PALE = colors.HexColor("#edf3f7")
RULE = colors.HexColor("#c8d2dc")
ROW_ALT = colors.HexColor("#f6f8fa")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将 FINTRACE 决赛报告 Markdown 排版为 PDF。")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--font-regular", type=Path, default=DEFAULT_REGULAR)
    parser.add_argument("--font-bold", type=Path, default=DEFAULT_BOLD)
    return parser.parse_args()


def register_fonts(regular: Path, bold: Path) -> None:
    missing = [str(path) for path in (regular, bold) if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少 Windows 系统字体文件：" + "；".join(missing))
    pdfmetrics.registerFont(TTFont("YaHei", str(regular), subfontIndex=0))
    pdfmetrics.registerFont(TTFont("YaHei-Bold", str(bold), subfontIndex=0))
    pdfmetrics.registerFontFamily(
        "YaHei",
        normal="YaHei",
        bold="YaHei-Bold",
        italic="YaHei",
        boldItalic="YaHei-Bold",
    )


def make_styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "BodyCN",
        fontName="YaHei",
        fontSize=9.3,
        leading=15.2,
        textColor=INK,
        alignment=TA_LEFT,
        spaceAfter=6.0,
        allowWidows=0,
        allowOrphans=0,
    )
    return {
        "title": ParagraphStyle(
            "CoverTitle",
            parent=body,
            fontName="YaHei-Bold",
            fontSize=29,
            leading=39,
            textColor=INK,
            alignment=TA_CENTER,
            spaceBefore=118,
            spaceAfter=14,
        ),
        "subtitle": ParagraphStyle(
            "CoverSubtitle",
            parent=body,
            fontSize=14,
            leading=23,
            textColor=ACCENT,
            alignment=TA_CENTER,
            spaceAfter=18,
        ),
        "cover_meta": ParagraphStyle(
            "CoverMeta",
            parent=body,
            fontSize=10.3,
            leading=18,
            textColor=MUTED,
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "h1": ParagraphStyle(
            "SectionH1",
            parent=body,
            fontName="YaHei-Bold",
            fontSize=19,
            leading=27,
            textColor=INK,
            spaceBefore=8,
            spaceAfter=12,
            keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "SectionH2",
            parent=body,
            fontName="YaHei-Bold",
            fontSize=13.2,
            leading=19,
            textColor=ACCENT,
            spaceBefore=11,
            spaceAfter=6,
            keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "SectionH3",
            parent=body,
            fontName="YaHei-Bold",
            fontSize=10.7,
            leading=16,
            textColor=INK,
            spaceBefore=8,
            spaceAfter=4,
            keepWithNext=True,
        ),
        "body": body,
        "small": ParagraphStyle(
            "SmallCN",
            parent=body,
            fontSize=8.1,
            leading=12,
            textColor=MUTED,
            spaceAfter=4,
        ),
        "table": ParagraphStyle(
            "TableCN",
            parent=body,
            fontSize=7.8,
            leading=11.3,
            spaceAfter=0,
        ),
        "table_head": ParagraphStyle(
            "TableHeadCN",
            parent=body,
            fontName="YaHei-Bold",
            fontSize=7.9,
            leading=11.4,
            textColor=colors.white,
            alignment=TA_LEFT,
            spaceAfter=0,
        ),
        "table_right": ParagraphStyle(
            "TableRightCN",
            parent=body,
            fontSize=7.8,
            leading=11.3,
            alignment=TA_RIGHT,
            spaceAfter=0,
        ),
        "quote": ParagraphStyle(
            "QuoteCN",
            parent=body,
            leftIndent=8,
            rightIndent=8,
            fontSize=8.7,
            leading=14,
            textColor=MUTED,
            spaceAfter=0,
        ),
        "code": ParagraphStyle(
            "CodeCN",
            parent=body,
            fontName="YaHei",
            fontSize=8.2,
            leading=12,
            leftIndent=5,
            rightIndent=5,
            spaceAfter=0,
        ),
    }


def inline_markup(nodes: list[dict[str, Any]]) -> str:
    output: list[str] = []
    for node in nodes:
        kind = node.get("type")
        children = node.get("children", [])
        if kind in {"text", "codespan"}:
            raw = html.escape(node.get("raw", ""))
            output.append(f"<font name='YaHei'>{raw}</font>" if kind == "codespan" else raw)
        elif kind in {"strong", "emphasis", "strikethrough"}:
            tag = {"strong": "b", "emphasis": "i", "strikethrough": "strike"}[kind]
            output.append(f"<{tag}>{inline_markup(children)}</{tag}>")
        elif kind == "link":
            href = html.escape(node.get("attrs", {}).get("url", ""), quote=True)
            output.append(
                f"<link href='{href}' color='#285d87'>{inline_markup(children)}</link>"
            )
        elif kind == "image":
            output.append(html.escape("".join(child.get("raw", "") for child in children)))
        elif kind in {"softbreak", "linebreak"}:
            output.append("<br/>")
        elif kind == "inline_html":
            output.append(html.escape(node.get("raw", "")))
        else:
            output.append(inline_markup(children))
    return "".join(output)


def plain_text(nodes: list[dict[str, Any]]) -> str:
    pieces: list[str] = []
    for node in nodes:
        if "raw" in node:
            pieces.append(node["raw"])
        else:
            pieces.append(plain_text(node.get("children", [])))
    return "".join(pieces)


def make_table(node: dict[str, Any], width: float, styles: dict[str, ParagraphStyle]) -> LongTable:
    rows: list[list[Paragraph]] = []
    header_cells: list[dict[str, Any]] = []
    for group in node.get("children", []):
        if group.get("type") not in {"table_head", "table_body"}:
            continue
        is_header = group.get("type") == "table_head"
        if is_header:
            header_cells = group.get("children", [])
            rows.append(
                [
                    Paragraph(
                        inline_markup(cell.get("children", [])),
                        styles["table_head"],
                    )
                    for cell in header_cells
                ]
            )
            continue
        row_nodes = group.get("children", [])
        for row in row_nodes:
            cells: list[Paragraph] = []
            for index, cell in enumerate(row.get("children", [])):
                alignment = cell.get("attrs", {}).get("align")
                cell_style = (
                    styles["table_right"]
                    if alignment == "right"
                    else styles["table"]
                )
                cells.append(
                    Paragraph(
                        inline_markup(cell.get("children", [])),
                        cell_style,
                    )
                )
            rows.append(cells)
    if not rows:
        return LongTable([[""]], colWidths=[width], repeatRows=1)

    column_count = max(len(row) for row in rows)
    for row in rows:
        row.extend([Paragraph("", styles["table"])] * (column_count - len(row)))
    if column_count == 6:
        # The quarterly comparison table has short period columns and long
        # delta/rate cells; give the last two columns room to avoid wrapping
        # percentages onto a separate line.
        proportions = [0.15, 0.13, 0.13, 0.13, 0.23, 0.23]
        col_widths = [width * item for item in proportions]
    else:
        col_widths = [width / column_count] * column_count
    table = LongTable(rows, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    commands: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, RULE),
        ("BOX", (0, 0), (-1, -1), 0.55, RULE),
    ]
    for row_index in range(1, len(rows)):
        if row_index % 2 == 0:
            commands.append(("BACKGROUND", (0, row_index), (-1, row_index), ROW_ALT))
    table.setStyle(TableStyle(commands))
    table.spaceBefore = 4
    table.spaceAfter = 9
    return table


def architecture_drawing() -> Drawing:
    """Draw the architecture with ReportLab text so Chinese fonts embed in the PDF."""
    drawing = Drawing(800, 420)
    ink = colors.HexColor("#172637")
    muted = colors.HexColor("#536477")
    border = colors.HexColor("#c9d3dd")
    line_color = colors.HexColor("#536477")
    blue = colors.HexColor("#2563a6")

    def box(x: float, y: float, width: float, height: float, fill: str, stroke: str) -> None:
        drawing.add(
            Rect(
                x,
                y,
                width,
                height,
                rx=10,
                ry=10,
                fillColor=colors.HexColor(fill),
                strokeColor=colors.HexColor(stroke),
                strokeWidth=1.1,
            )
        )

    def label(
        x: float,
        y: float,
        text: str,
        *,
        size: float = 9.5,
        bold: bool = False,
        color: colors.Color = ink,
    ) -> None:
        drawing.add(
            String(
                x,
                y,
                text,
                fontName="YaHei-Bold" if bold else "YaHei",
                fontSize=size,
                fillColor=color,
            )
        )

    def arrow(x1: float, y1: float, x2: float, y2: float, color: colors.Color = line_color) -> None:
        drawing.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=1.5))
        angle = math.atan2(y2 - y1, x2 - x1)
        back_x = x2 - 7 * math.cos(angle)
        back_y = y2 - 7 * math.sin(angle)
        side = 3.4
        points = [
            x2,
            y2,
            back_x + side * math.sin(angle),
            back_y - side * math.cos(angle),
            back_x - side * math.sin(angle),
            back_y + side * math.cos(angle),
        ]
        drawing.add(Polygon(points, fillColor=color, strokeColor=color, strokeWidth=0.5))

    # Outer local boundary.
    box(8, 35, 552, 318, "#ffffff", "#c9d3dd")
    label(25, 332, "本机工作区", size=12, bold=True)
    label(22, 392, "FINTRACE｜本机财报分析与证据链", size=18, bold=True)
    label(24, 374, "本地计算与归档为主；显式模型流程才发送必要文本片段", size=9, color=muted)

    # Local modules.
    box(27, 235, 130, 91, "#eaf2f8", "#8eabc2")
    label(40, 300, "年度 Web", size=11, bold=True)
    label(40, 279, "任务与结果浏览", size=9)
    label(40, 260, "本机产品交互入口", size=8, color=muted)

    box(180, 235, 166, 91, "#eaf2f8", "#8eabc2")
    label(194, 300, "FastAPI 与流程", size=11, bold=True)
    label(194, 279, "任务状态、归档读取", size=8.5)
    label(194, 260, "年报任务 / 历史 / 导出", size=8.5)

    box(369, 235, 166, 91, "#eaf5f1", "#72a897")
    label(383, 300, "确定性核心", size=11, bold=True)
    label(383, 279, "PDF 解析 / 来源绑定", size=8.5)
    label(383, 260, "Decimal 计算与重算核验", size=8.2)
    label(383, 244, "证据不足时弃权", size=8, color=muted)

    box(27, 75, 150, 115, "#eaf2f8", "#8eabc2")
    label(41, 164, "本机证据与归档", size=10.5, bold=True)
    label(41, 142, "PDF 哈希、页码、行列", size=8.3)
    label(41, 122, "基础 / M3 / AI 分层报告", size=8.3)
    label(41, 98, "真实原文仅本地保存", size=8, color=muted)

    box(194, 75, 150, 115, "#f7f2e8", "#c0a46d")
    label(207, 164, "M3 规则层", size=10.5, bold=True)
    label(207, 142, "四条试行筛查规则", size=8.3)
    label(207, 122, "独立保存规则审计", size=8.3)
    label(207, 98, "未校准分数，不是概率", size=7.7, color=muted)

    box(361, 75, 174, 115, "#f1eef8", "#9d8ac4")
    label(375, 164, "AI 调查与评审层", size=10.2, bold=True)
    label(375, 142, "LangGraph 唯一编排", size=8.3)
    label(375, 122, "只读基础确定性报告", size=8.3)
    label(375, 98, "模型观点，不升级核验", size=8, color=muted)

    # Optional cloud and deliverables.
    box(586, 237, 202, 104, "#eef4fb", "#7b9ec2")
    label(600, 315, "操作者配置的云端 API", size=10.2, bold=True)
    label(600, 292, "显式请求 / 返回模型观点", size=8.2)
    label(600, 273, "必要片段 / 纯文本接口", size=8.4)
    label(600, 251, "供应方与型号不内置", size=8, color=muted)

    box(586, 82, 202, 112, "#ffffff", "#c9d3dd")
    label(600, 167, "最终产物", size=10.5, bold=True)
    label(600, 145, "分层年度报告与审计记录", size=8.2)
    label(600, 125, "季度来源 / 派生 / 比较链", size=8.2)
    label(600, 103, "JSON / Markdown / 离线 HTML", size=8, color=muted)

    # Flow lines sit between, not over, module labels.
    arrow(157, 280, 174, 280)
    arrow(346, 280, 363, 280)
    arrow(92, 196, 92, 231)
    drawing.add(Line(390, 235, 390, 213, strokeColor=line_color, strokeWidth=1.5))
    drawing.add(Line(390, 213, 329, 213, strokeColor=line_color, strokeWidth=1.5))
    arrow(329, 213, 329, 190)
    arrow(450, 235, 450, 190)
    arrow(194, 132, 177, 132)
    drawing.add(Line(400, 235, 400, 225, strokeColor=line_color, strokeWidth=1.5))
    drawing.add(Line(400, 225, 150, 225, strokeColor=line_color, strokeWidth=1.5))
    arrow(150, 225, 150, 190)
    drawing.add(Line(400, 190, 400, 210, strokeColor=line_color, strokeWidth=1.5))
    drawing.add(Line(400, 210, 170, 210, strokeColor=line_color, strokeWidth=1.5))
    arrow(170, 210, 170, 190)
    drawing.add(Line(110, 75, 110, 43, strokeColor=line_color, strokeWidth=1.5))
    drawing.add(Line(110, 43, 560, 43, strokeColor=line_color, strokeWidth=1.5))
    drawing.add(Line(560, 43, 560, 108, strokeColor=line_color, strokeWidth=1.5))
    arrow(560, 108, 579, 108)
    label(340, 57, "本机分层归档与白名单导出", size=7.5, color=muted)
    arrow(535, 178, 579, 263, blue)
    arrow(579, 245, 535, 157, blue)

    label(25, 17, "年度走 Web/API；季度同比环比由独立 CLI 运行，不经 Web/API。基础、M3、AI 观点分别归档；AI 只读基础报告，不接收 M3。", size=7.4, color=muted)
    return drawing


def block_flowables(
    nodes: list[dict[str, Any]],
    width: float,
    styles: dict[str, ParagraphStyle],
    source_parent: Path,
) -> list[Any]:
    output: list[Any] = []
    image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    for node in nodes:
        kind = node.get("type")
        children = node.get("children", [])
        if kind == "heading":
            level = node.get("attrs", {}).get("level", 2)
            style = styles["h1"] if level == 1 else styles["h2"] if level == 2 else styles["h3"]
            output.append(Paragraph(inline_markup(children), style))
        elif kind == "paragraph":
            image_nodes = [child for child in children if child.get("type") == "image"]
            if image_nodes:
                for image_node in image_nodes:
                    src = image_node.get("attrs", {}).get("url", "")
                    image_path = (source_parent / src).resolve()
                    if not image_path.is_file():
                        raise FileNotFoundError(f"报告图片不存在：{image_path}")
                    if image_path.suffix.lower() == ".svg":
                        drawing = (
                            architecture_drawing()
                            if image_path.name == "architecture.svg"
                            else None
                        )
                        if drawing is None:
                            raise ValueError(
                                f"PDF 报告不支持直接嵌入 SVG 字体：{image_path}; "
                                "请为该图注册嵌入字体的矢量绘制逻辑。"
                            )
                        if drawing is None or drawing.width <= 0 or drawing.height <= 0:
                            raise ValueError(f"无法解析 SVG：{image_path}")
                        # Architecture diagrams get their own landscape page for legibility.
                        output.extend([NextPageTemplate("landscape"), PageBreak()])
                        figure_width = landscape(A4)[0] - 34 * mm
                        scale = min(figure_width / drawing.width, 430 / drawing.height)
                        drawing.scale(scale, scale)
                        drawing.width *= scale
                        drawing.height *= scale
                        output.extend([drawing, NextPageTemplate("portrait"), PageBreak()])
                    else:
                        image = Image(str(image_path))
                        scale = min(width / image.drawWidth, 430 / image.drawHeight, 1)
                        image.drawWidth *= scale
                        image.drawHeight *= scale
                        output.append(image)
                other_text = "".join(
                    child.get("raw", "")
                    for child in children
                    if child.get("type") != "image"
                ).strip()
                if other_text:
                    output.append(Paragraph(html.escape(other_text), styles["small"]))
            else:
                output.append(Paragraph(inline_markup(children), styles["body"]))
        elif kind == "block_quote":
            inner = block_flowables(children, width - 14 * mm, styles, source_parent)
            quote_table = LongTable(
                [[inner]],
                colWidths=[width],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), PALE),
                        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                        ("LINEBEFORE", (0, 0), (0, -1), 2.2, ACCENT),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                ),
            )
            output.extend([quote_table, Spacer(1, 5)])
        elif kind == "list":
            ordered = node.get("attrs", {}).get("ordered", False)
            start = node.get("attrs", {}).get("start", 1)
            for index, item in enumerate(children):
                item_nodes = item.get("children", [])
                item_markup = " ".join(
                    inline_markup(child.get("children", []))
                    for child in item_nodes
                    if child.get("type") in {"block_text", "paragraph"}
                )
                bullet = f"{start + index}." if ordered else "•"
                output.append(
                    Paragraph(
                        f"<font color='#285d87'><b>{html.escape(bullet)}</b></font> "
                        f"{item_markup}",
                        ParagraphStyle(
                            "ListItemCN",
                            parent=styles["body"],
                            leftIndent=13,
                            firstLineIndent=-11,
                            spaceAfter=3,
                        ),
                    )
                )
        elif kind == "table":
            output.append(make_table(node, width, styles))
        elif kind == "thematic_break":
            output.append(Spacer(1, 5))
            output.append(
                LongTable(
                    [[""]],
                    colWidths=[width],
                    rowHeights=[0.7],
                    style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), RULE)]),
                )
            )
            output.append(Spacer(1, 5))
        elif kind == "block_code":
            raw = html.escape(node.get("raw", "").rstrip())
            code_block = LongTable(
                [[Paragraph(raw.replace("\n", "<br/>"), styles["code"])]],
                colWidths=[width],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), ROW_ALT),
                        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            code_block.spaceBefore = 3
            code_block.spaceAfter = 7
            output.append(code_block)
        elif kind in {"block_html", "inline_html"}:
            raw = node.get("raw", "").strip()
            if raw == "<!-- pagebreak -->":
                output.append(PageBreak())
            else:
                for alt, src in image_pattern.findall(raw):
                    image_path = (source_parent / src).resolve()
                    if image_path.is_file():
                        image = Image(str(image_path))
                        scale = min(width / image.drawWidth, 430 / image.drawHeight, 1)
                        image.drawWidth *= scale
                        image.drawHeight *= scale
                        output.append(image)
        elif children:
            output.extend(block_flowables(children, width, styles, source_parent))
    return output


class FinalReportDocument(BaseDocTemplate):
    def __init__(self, filename: Path, title: str) -> None:
        self.report_title = title
        super().__init__(
            str(filename),
            pagesize=A4,
            leftMargin=19 * mm,
            rightMargin=19 * mm,
            topMargin=19 * mm,
            bottomMargin=17 * mm,
            title=title,
            author="FINTRACE 项目组",
            subject="决赛技术报告",
            pageCompression=1,
        )
        portrait_width, portrait_height = A4
        landscape_width, landscape_height = landscape(A4)
        portrait_frame = Frame(
            19 * mm,
            17 * mm,
            portrait_width - 38 * mm,
            portrait_height - 38 * mm,
            id="portrait",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        landscape_frame = Frame(
            17 * mm,
            15 * mm,
            landscape_width - 34 * mm,
            landscape_height - 30 * mm,
            id="landscape",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates(
            [
                PageTemplate(id="portrait", frames=[portrait_frame], onPage=self._decorate_page),
                PageTemplate(
                    id="landscape",
                    pagesize=landscape(A4),
                    frames=[landscape_frame],
                    onPage=self._decorate_page,
                ),
            ]
        )

    def _decorate_page(self, canvas: Any, doc: Any) -> None:
        page_width, page_height = doc.pagesize
        canvas.saveState()
        if doc.page > 1:
            canvas.setStrokeColor(RULE)
            canvas.setLineWidth(0.45)
            canvas.line(19 * mm, page_height - 13 * mm, page_width - 19 * mm, page_height - 13 * mm)
            canvas.setFont("YaHei", 7.2)
            canvas.setFillColor(MUTED)
            canvas.drawString(19 * mm, page_height - 10.7 * mm, "FINTRACE  ·  决赛技术报告")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.45)
        canvas.line(19 * mm, 12 * mm, page_width - 19 * mm, 12 * mm)
        canvas.setFont("YaHei", 7.2)
        canvas.setFillColor(MUTED)
        canvas.drawString(19 * mm, 7.5 * mm, "2026 年 10 月 3 日  ·  FINTRACE 决赛技术报告")
        canvas.drawRightString(page_width - 19 * mm, 7.5 * mm, str(doc.page))
        canvas.restoreState()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"报告 Markdown 不存在：{source}")
    register_fonts(args.font_regular.resolve(), args.font_bold.resolve())
    styles = make_styles()
    parser = mistune.create_markdown(renderer="ast", plugins=["table", "strikethrough"])
    ast = parser(source.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = FinalReportDocument(output, "FINTRACE 决赛技术报告")
    story = block_flowables(ast, doc.width, styles, source.parent)
    doc.build(story)
    print(f"PDF 已生成：{output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        raise
