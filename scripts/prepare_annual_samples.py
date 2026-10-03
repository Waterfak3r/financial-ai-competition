"""Generate FINTRACE's original, explicitly synthetic annual-report sample.

The generator only writes to data/samples/annual. Existing files are left intact;
re-running with a different renderer output fails instead of replacing evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "samples" / "annual"
PDF_NAME = "sim-annual-001-2025.pdf"
COMPANY_ID = "SIM-ANNUAL-001"
COMPANY_NAME = "澄湾云算股份有限公司（虚构）"
REPORT_YEAR = 2025
DOCUMENT_ID = "sim-annual-001-2025"
GENERATOR_VERSION = "1.2.0"
FONT_NAME = "Microsoft YaHei"
FONT_VERSION = "6.31"
FONT_SHA256 = "d79c55e68b1131eea0cc1c47be4f572d964f28c682e143db2ad09c1e4cb07a3f"
FONT_LICENSE = "Windows-supplied font; subject to the Windows license terms"
FONT_SOURCE_URL = "https://learn.microsoft.com/en-us/typography/font-list/microsoft-yahei"
FONT_LICENSE_URL = "https://learn.microsoft.com/en-us/typography/fonts/font-faq"
FONT_SPEC_URL = "https://learn.microsoft.com/en-us/typography/opentype/spec/os2"
PREVIOUS_MICROSOFT_SHA256 = "a9131e1c660bd38ed80a1560fe5e3f3c8dd4a25c35b561019e04d6fa444f0d68"
PREVIOUS_MICROSOFT_RUN_ID = "annual-analysis-990a774a-665a-4ad7-ab17-87c5997523e6"
PREVIOUS_NOTO_SHA256 = "ea215df01c6c37a51ccf609b2ee3977ff12adeb423e4407ab6af39fab1961440"
PREVIOUS_NOTO_RUN_ID = "annual-analysis-30ce07eb-ec17-45fb-baac-f1abbe15fa4b"
USE_CONDITIONS = (
    "本 PDF 为 FINTRACE 团队原创构造的模拟材料，不对应真实公司、证券代码或真实披露。"
    "可随 FINTRACE 比赛提交件及团队演示包分发和演示；不得作为真实公司事实、投资依据、"
    "舞弊/违规标签或模型训练标签。金额、期间、叙述和线索均为受控虚构数据。"
)
DISCLAIMER_SHORT = "原创模拟｜非真实披露｜不可作为舞弊标签"


def main() -> int:
    try:
        import pymupdf
    except ImportError:
        print("需要当前环境已安装的 PyMuPDF；本脚本不会安装依赖。", file=sys.stderr)
        return 2

    font_path = _font_path()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUTPUT_DIR / PDF_NAME
    pdf_bytes = _build_pdf(pymupdf, font_path)
    _write_new_or_identical(pdf_path, pdf_bytes)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    manifest = _manifest(digest)
    manifest_path = OUTPUT_DIR / "source_manifest.json"
    _write_new_or_identical(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    print(f"sample={pdf_path.relative_to(ROOT).as_posix()}")
    print(f"sha256={digest}")
    print(f"generator=prepare_annual_samples.py@{GENERATOR_VERSION}")
    print(f"font={FONT_NAME}@{FONT_VERSION} license={FONT_LICENSE}")
    return 0


def _font_path() -> Path:
    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
    )
    for candidate in candidates:
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if digest != FONT_SHA256:
                continue
            return candidate
    raise RuntimeError(
        f"需要与受控样例匹配的 {FONT_NAME} {FONT_VERSION} 字体（SHA256={FONT_SHA256}）；"
        "生成器不会下载、安装或使用其他字体。"
    )


def _build_pdf(pymupdf: Any, font_path: Path) -> bytes:
    document = pymupdf.open()
    metric_font = pymupdf.Font(fontfile=str(font_path))
    document.set_metadata(
        {
            "title": "FINTRACE 原创模拟年度报告 SIM-ANNUAL-001（非真实披露）",
            "author": "FINTRACE Team",
            "subject": "Synthetic demonstration data; not a real company disclosure",
            "keywords": "原创模拟,非真实披露,FINTRACE,不可作为舞弊标签",
            "creator": f"scripts/prepare_annual_samples.py {GENERATOR_VERSION}",
            "producer": "PyMuPDF",
            "creationDate": "D:20261002000000Z",
            "modDate": "D:20261002000000Z",
        }
    )

    _new_page(document, font_path, 1, "样例使用范围", "封面与许可")
    page = _new_page(document, font_path, 2, "财务报表摘录", "合并利润表")
    _line(page, 52, 150, "合并利润表", 17, bold=True)
    _line(page, 52, 181, "单位：元    币种：人民币", 10)
    _table_header(page, metric_font, "2025年度", "2024年度", 217, 10, (345, 480))
    _table_row(page, metric_font, "一、营业收入", "1,250,000,000.00", "1,000,000,000.00", 253, 10, (345, 480))
    _table_row(page, metric_font, "其中：营业收入", "1,250,000,000.00", "1,000,000,000.00", 284, 10, (345, 480))
    _table_row(page, metric_font, "五、净利润", "160,000,000.00", "130,000,000.00", 324, 10, (345, 480))
    _table_row(page, metric_font, "归属于母公司股东的净利润", "145,000,000.00", "118,000,000.00", 355, 10, (345, 480))
    _table_row(page, metric_font, "其中：营业成本", "650,000,000.00", "520,000,000.00", 386, 10, (345, 480))
    _line(page, 52, 448, "以上数字均为构造数据，未对应任何真实上市公司年报。", 10)

    page = _new_page(document, font_path, 3, "财务报表摘录", "合并现金流量表")
    _line(page, 52, 150, "合并现金流量表", 17, bold=True)
    _line(page, 52, 181, "单位：元    币种：人民币", 10)
    _table_header(page, metric_font, "2025年度", "2024年度", 217, 10, (345, 480))
    _table_row(page, metric_font, "经营活动产生的现金流量净额", "-10,000,000.00", "90,000,000.00", 253, 10, (345, 480))
    _line(page, 52, 306, "受控演示关系：归母净利润同比增加，经营活动现金流量净额同比减少。", 10)
    _line(page, 52, 337, "现有披露没有给出差异成因；不能确认具体原因或因果关系。", 10)
    _line(page, 52, 368, "该线索只用于核对提取、计算、引用和状态展示，不是舞弊或违规标签。", 10)

    page = _new_page(document, font_path, 4, "财务报表摘录", "合并资产负债表")
    _line(page, 52, 150, "合并资产负债表", 17, bold=True)
    _line(page, 52, 181, "单位：元    币种：人民币", 10)
    _table_header(page, metric_font, "2025年12月31日", "2024年12月31日", 217, 10, (345, 480))
    _table_row(page, metric_font, "应收账款", "82,000,000.00", "70,000,000.00", 253, 10, (345, 480))
    _table_row(page, metric_font, "存货", "95,000,000.00", "88,000,000.00", 284, 10, (345, 480))

    page = _new_page(document, font_path, 5, "财务报表摘录", "非经常性损益")
    _line(page, 52, 150, "非经常性损益项目和金额", 17, bold=True)
    _line(page, 52, 181, "单位：元    币种：人民币", 10)
    _table_header(page, metric_font, "2025年度金额", "2024年度金额", 217, 10, (345, 480))
    _table_row(page, metric_font, "合计", "6,000,000.00", "4,000,000.00", 253, 10, (345, 480))
    _line(page, 52, 306, "非经常性损益合计的披露范围未单独确认。", 10)

    page = _new_page(document, font_path, 6, "会计政策及追溯调整说明", "完整模拟声明")
    _line(page, 52, 130, "重要会计政策变更", 10, bold=True)
    _line(page, 52, 148, "□适用 √不适用", 10)
    _line(page, 52, 187, "重要会计估计变更", 10, bold=True)
    _line(page, 52, 205, "□适用 √不适用", 10)
    _line(
        page,
        52,
        244,
        "2025年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表",
        8.1,
    )
    _line(page, 52, 262, "□适用 √不适用", 10)
    _line(page, 52, 301, "前期会计差错更正", 10, bold=True)
    _line(page, 52, 319, "追溯重述法", 10)
    _line(page, 52, 337, "□适用 √不适用", 10)
    adjustment_disclosures = (
        "准则追溯调整影响期初未分配利润0元。",
        "会计政策变更影响期初未分配利润0元。",
        "重大会计差错更正影响期初未分配利润0元。",
        "同一控制合并范围变更影响期初未分配利润0元。",
        "其他调整合计影响期初未分配利润0元。",
    )
    for index, disclosure in enumerate(adjustment_disclosures):
        _line(page, 52, 390 + index * 26, disclosure, 9)
    _line(page, 52, 550, "以上为构造的完整声明，用于演示系统按原文复核可比性。", 9)
    _line(page, 52, 576, "它不证明任何真实公司未发生追溯调整。", 9)

    page = _new_page(document, font_path, 7, "管理层讨论与分析摘录", "候选线索的解释边界")
    _textbox(
        page,
        pymupdf.Rect(52, 150, 540, 390),
        "本模拟报告披露营业收入和归属于母公司股东的净利润较比较期增加，经营活动产生的现金流量净额较比较期减少。\n\n"
        "资料没有提供现金流入流出构成、应收款项与合同负债变动、回款安排或管理层对差异的解释。"
        "因此不能确认现金流变化的原因，也不能据此认定收入确认、现金收取或其他事项存在因果联系。\n\n"
        "本受控案例只演示发现方向差异、回看原文、展示状态和记录证据缺口。候选风险线索不是已核实事实，"
        "更不是舞弊、违规或无舞弊标签。样例不应用于模型训练或现实主体判断。",
        10,
    )
    _line(page, 52, 440, "本 PDF 为团队原创模拟内容；许可与哈希见同目录 README、useconditions.md 和 source_manifest.json。", 9)

    try:
        return document.tobytes(garbage=4, deflate=True, no_new_id=True)
    finally:
        document.close()


def _new_page(document: Any, font_path: Path, page_number: int, section: str, title: str) -> Any:
    page = document.new_page(width=595, height=842)
    page.insert_font(fontname="fintrace-sim", fontfile=str(font_path))
    _line(page, 52, 32, DISCLAIMER_SHORT, 8.5, color=(0.65, 0.08, 0.08), bold=True)
    page.draw_line((52, 42), (543, 42), color=(0.65, 0.08, 0.08), width=0.7)
    _line(page, 52, 70, f"{COMPANY_NAME}  {REPORT_YEAR}年年度报告", 10, bold=True)
    _line(page, 52, 94, f"{section} · {title}", 9, color=(0.28, 0.32, 0.38))
    page.draw_line((52, 785), (543, 785), color=(0.75, 0.77, 0.8), width=0.5)
    _line(page, 52, 805, f"第 {page_number} 页 / 7 页    SIM-ANNUAL-001", 8, color=(0.38, 0.4, 0.43))
    _line(page, 52, 824, "原创模拟；非真实披露；不可作为舞弊标签。", 8, color=(0.65, 0.08, 0.08), bold=True)

    if page_number == 1:
        _line(page, 52, 165, "FINTRACE 现场演示样例", 22, bold=True, color=(0.12, 0.2, 0.34))
        _line(page, 52, 207, "公司标识：SIM-ANNUAL-001（非证券代码）", 12, bold=True)
        _line(page, 52, 235, "报告期间：2025 年度；比较期间：2024 年度", 12)
        warning = (
            "本资料由 FINTRACE 团队原创构造。公司、经营活动、财务数字和叙述均为虚构，"
            "不对应任何真实公司披露。\n\n"
            "本 PDF 可随 FINTRACE 比赛提交件与团队演示包分发。不得用作真实公司事实、投资依据、"
            "舞弊/违规标签、无舞弊标签或模型训练标签。"
        )
        _textbox(page, pymupdf_rect(52, 280, 540, 438), warning, 10.5)
        _line(page, 52, 468, "受控演示线索：利润相关指标上升，经营活动现金流量净额下降。", 11, bold=True)
        _line(page, 52, 498, "原文明确说明成因未确认；不得从方向差异推断因果或舞弊。", 11)
        _line(page, 52, 548, f"生成器：scripts/prepare_annual_samples.py v{GENERATOR_VERSION}", 9)
        _line(page, 52, 572, f"字体：{FONT_NAME} {FONT_VERSION}；按系统字体许可条件嵌入文档。", 9)
    return page


def pymupdf_rect(x0: float, y0: float, x1: float, y1: float) -> Any:
    import pymupdf

    return pymupdf.Rect(x0, y0, x1, y1)


def _line(
    page: Any,
    x: float,
    y: float,
    value: str,
    size: float,
    *,
    color: tuple[float, float, float] = (0.12, 0.14, 0.17),
    bold: bool = False,
) -> None:
    # The embedded CJK font has one face. Bold emphasis uses color and size;
    # this keeps every visible glyph backed by the same licensed system font.
    page.insert_text(
        (x, y),
        value,
        fontname="fintrace-sim",
        fontsize=size + (0.35 if bold else 0.0),
        color=color,
        overlay=True,
    )


def _table_header(
    page: Any,
    font: Any,
    current: str,
    comparative: str,
    y: float,
    size: float,
    centers: tuple[float, float],
) -> None:
    _line(page, 52, y, "项目", size, bold=True)
    _centered(page, font, current, centers[0], y, size, bold=True)
    _centered(page, font, comparative, centers[1], y, size, bold=True)


def _table_row(
    page: Any,
    font: Any,
    label: str,
    current: str,
    comparative: str,
    y: float,
    size: float,
    centers: tuple[float, float],
) -> None:
    _line(page, 52, y, label, size)
    _centered(page, font, current, centers[0], y, size)
    _centered(page, font, comparative, centers[1], y, size)


def _centered(page: Any, font: Any, value: str, center_x: float, y: float, size: float, *, bold: bool = False) -> None:
    width = font.text_length(value, fontsize=size + (0.35 if bold else 0.0))
    _line(page, center_x - width / 2, y, value, size, bold=bold)


def _textbox(page: Any, rect: Any, value: str, size: float) -> None:
    spare = page.insert_textbox(
        rect,
        value,
        fontname="fintrace-sim",
        fontsize=size,
        lineheight=1.38,
        color=(0.12, 0.14, 0.17),
        overlay=True,
    )
    if spare < 0:
        raise RuntimeError("PDF 文本框高度不足，声明可能被裁剪；请调整布局。")


def _manifest(pdf_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "dataset_id": "fintrace-annual-synthetic-v1",
        "nature": "原创模拟",
        "data_nature": "original_synthetic",
        "use_conditions": USE_CONDITIONS,
        "distribution": {
            "competition_submission": True,
            "team_demonstration_bundle": True,
            "model_training": False,
            "fraud_label": False,
        },
        "generator": {
            "path": "scripts/prepare_annual_samples.py",
            "version": GENERATOR_VERSION,
            "tool": "PyMuPDF",
            "font": {
                "name": FONT_NAME,
                "version": FONT_VERSION,
                "file_sha256": FONT_SHA256,
                "source": FONT_SOURCE_URL,
                "license": FONT_LICENSE,
                "license_reference": FONT_LICENSE_URL,
                "embedding_specification": FONT_SPEC_URL,
                "copyright": "© 2024 Microsoft Corporation. All Rights Reserved. Portions © 2024 Beijing Founder Electronics Co. Ltd. All Rights Reserved.",
                "designer": "Founder",
                "vendor": "Microsoft Corp.; glyph portions licensed from Beijing Founder Electronics Co. Ltd.",
                "embedding": "The selected face reports OS/2 fsType 0x0008 (Editable embedding); 0x0100 (No subsetting) is clear. Embed only in documents as allowed by the Windows product license; do not distribute the font separately.",
                "os2_fsType": 8,
                "no_subsetting": False,
            },
        },
        "history": [
            {
                "status": "superseded_local_archive_only",
                "sha256": PREVIOUS_MICROSOFT_SHA256,
                "run_id": PREVIOUS_MICROSOFT_RUN_ID,
                "font": "Microsoft YaHei 6.31",
                "note": "首轮预览生成版本；仅保留在本机 CLI 归档中，不是当前生成器输出。",
            },
            {
                "status": "superseded_local_archive_only",
                "sha256": PREVIOUS_NOTO_SHA256,
                "run_id": PREVIOUS_NOTO_RUN_ID,
                "font": "Noto Sans SC 2.04 (SIL OFL 1.1)",
                "note": "在当前 PyMuPDF 版本上的实测试验版；兼容字符映射导致现有年度抽取管线弃权，保留其本机 CLI 归档用于追溯，不宣称此限制适用于其他版本或 Noto PDF。",
            },
        ],
        "records": [
            {
                "sample_id": "sim-annual-001-2025",
                "company_id": COMPANY_ID,
                "company_name": COMPANY_NAME,
                "report_year": REPORT_YEAR,
                "document_id": DOCUMENT_ID,
                "file": PDF_NAME,
                "sha256": pdf_sha256,
                "nature": "原创模拟",
                "use_conditions": USE_CONDITIONS,
                "license": "Team-authored synthetic content; redistribution permitted with this notice.",
                "not_a_fraud_label": True,
            }
        ],
    }


def _write_new_or_identical(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"目标不是普通文件，拒绝覆盖：{path}")
        if path.read_bytes() != payload:
            raise RuntimeError(f"目标已存在且内容不同，拒绝覆盖：{path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)


if __name__ == "__main__":
    raise SystemExit(main())
