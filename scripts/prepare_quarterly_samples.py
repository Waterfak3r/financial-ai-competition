"""生成版本内原创季度模拟 PDF 与可复现来源清单。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

try:
    import pymupdf
except ImportError:  # 与项目其余 PDF 入口使用同一个依赖名
    import fitz as pymupdf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "samples" / "quarterly"
COMPANY_ID = "SIM-QUARTERLY-001"
COMPANY_NAME = "示例食味有限公司"
NOTICE_LINES = (
    "原创人工构造的模拟报告，不对应任何真实公司、证券代码或真实披露。",
    "可随 FINTRACE 比赛提交件和团队演示包分发；不得作为真实财务或舞弊标签。",
)
COMPARABILITY_LINES = (
    "口径说明：营业收入、归母净利润及经营活动产生的现金流量净额均为合并报表人民币元。",
    "本报告相关本期累计数与上年同期比较数未作追溯调整或前期差错更正，期间可比。",
)

REPORTS = (
    {
        "period": "Q1", "year": 2023, "publication_date": "2023-04-28",
        "current": {"revenue": "1000000", "net_profit_parent": "100000", "operating_cash_flow": "-20000"},
        "comparative": {"revenue": "900000", "net_profit_parent": "90000", "operating_cash_flow": "-15000"},
    },
    {
        "period": "H1", "year": 2023, "publication_date": "2023-08-28",
        "current": {"revenue": "2400000", "net_profit_parent": "260000", "operating_cash_flow": "80000"},
        "comparative": {"revenue": "2100000", "net_profit_parent": "230000", "operating_cash_flow": "50000"},
    },
    {
        "period": "Q1", "year": 2024, "publication_date": "2024-04-28",
        "current": {"revenue": "1200000", "net_profit_parent": "120000", "operating_cash_flow": "-20000"},
        "comparative": {"revenue": "1000000", "net_profit_parent": "100000", "operating_cash_flow": "-20000"},
    },
    {
        "period": "H1", "year": 2024, "publication_date": "2024-08-28",
        "current": {"revenue": "2700000", "net_profit_parent": "300000", "operating_cash_flow": "120000"},
        "comparative": {"revenue": "2400000", "net_profit_parent": "260000", "operating_cash_flow": "80000"},
    },
)

METRICS = (
    ("revenue", "营业收入"),
    ("net_profit_parent", "归属于母公司股东的净利润"),
    ("operating_cash_flow", "经营活动产生的现金流量净额"),
)


def _text(page, point: tuple[float, float], value: str, *, size: float = 10) -> None:
    page.insert_text(point, value, fontname="china-s", fontsize=size, color=(0, 0, 0))


def _text_block(page, rect: tuple[float, float, float, float], value: str, *, size: float = 9) -> None:
    overflow = page.insert_textbox(pymupdf.Rect(*rect), value, fontname="china-s", fontsize=size, color=(0, 0, 0), lineheight=1.2)
    if overflow < 0:
        raise ValueError(f"模拟报告声明超出可见文本框：{value}")


def _period_text(year: int, period: str) -> tuple[str, str]:
    if period == "Q1":
        return f"{year}年第一季度报告", f"{year}年第一季度"
    return f"{year}年半年度报告", f"{year}年半年度"


def _make_pdf(report: dict[str, object]) -> bytes:
    year = int(report["year"])
    period = str(report["period"])
    title, column_suffix = _period_text(year, period)
    document = pymupdf.open()
    document.set_metadata({
        "title": f"{COMPANY_NAME}{title}（原创模拟）",
        "author": "FINTRACE 团队原创模拟数据",
        "subject": "人工构造季度累计财务数值；非真实公司披露",
        "keywords": "原创模拟，季度分析",
        "creationDate": "D:20240101000000Z",
        "modDate": "D:20240101000000Z",
    })

    cover = document.new_page(width=612, height=792)
    _text(cover, (54, 62), f"{COMPANY_NAME} {title}", size=16)
    _text(cover, (54, 91), f"公司代码：{COMPANY_ID}")
    _text(cover, (54, 113), f"报告期：{year} 年 {period}")
    for index, segment in enumerate((
        "口径说明：仅列示合并报表流量指标的年初至报告期末累计数。",
        "本期金额和上年同期金额均以人民币元列示；空缺不代表零。",
        "此布局是季度解析器的受控演示样例，不代表通用财报识别能力。",
    )):
        _text(cover, (54, 195 + index * 22), segment)
    # 该声明在 PDF 原文中供比较核验器独立定位，不能由 manifest 的布尔值代替。
    for index, line in enumerate(NOTICE_LINES):
        _text_block(cover, (54, 139 + index * 17, 558, 158 + index * 17), line)
    for index, line in enumerate(COMPARABILITY_LINES):
        _text_block(cover, (54, 270 + index * 24, 558, 300 + index * 24), line)

    income = document.new_page(width=612, height=792)
    _text(income, (54, 30), "原创模拟报告 · 非真实公司披露", size=8)
    _text(income, (54, 82), "合并利润表", size=14)
    _text(income, (54, 108), f"{year}年1—{3 if period == 'Q1' else 6}月")
    _text(income, (54, 130), "单位：元   币种：人民币")
    label_suffix = "第一季度" if period == "Q1" else "半年度"
    _text(income, (321, 165), f"{year}年{label_suffix}")
    _text(income, (438, 165), f"{year - 1}年{label_suffix}")
    for index, (metric, label) in enumerate(METRICS[:2]):
        y = 220 + index * 55
        _text(income, (54, y), label)
        _text(income, (330, y), str(report["current"][metric]))
        _text(income, (445, y), str(report["comparative"][metric]))
    _text(income, (54, 770), "FINTRACE 可分发模拟样例 · 不构成真实财务或舞弊结论", size=8)

    cashflow = document.new_page(width=612, height=792)
    _text(cashflow, (54, 30), "原创模拟报告 · 非真实公司披露", size=8)
    _text(cashflow, (54, 82), "合并现金流量表", size=14)
    _text(cashflow, (54, 108), f"{year}年1—{3 if period == 'Q1' else 6}月")
    _text(cashflow, (54, 130), "单位：元   币种：人民币")
    _text(cashflow, (321, 165), f"{year}年{label_suffix}")
    _text(cashflow, (438, 165), f"{year - 1}年{label_suffix}")
    metric, label = METRICS[2]
    _text(cashflow, (54, 220), label)
    _text(cashflow, (330, 220), str(report["current"][metric]))
    _text(cashflow, (445, 220), str(report["comparative"][metric]))
    _text(cashflow, (54, 770), "FINTRACE 可分发模拟样例 · 不构成真实财务或舞弊结论", size=8)
    for page in document:
        page.set_rotation(0)
    return document.tobytes(garbage=4, deflate=True, no_new_id=True)


def prepare() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    docs: list[dict[str, object]] = []
    for report in REPORTS:
        period = str(report["period"])
        year = int(report["year"])
        document_id = f"sim-{year}-{period.lower()}"
        filename = f"{document_id}.pdf"
        pdf_path = OUT / filename
        content = _make_pdf(report)
        pdf_path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        title, _ = _period_text(year, period)
        docs.append({
            "document_id": document_id,
            "company_id": COMPANY_ID,
            "source_pdf": f"data/samples/quarterly/{filename}",
            "source_sha256": digest,
            "source_url": f"original-synthetic://fintrace/{document_id}",
            "publication_date": report["publication_date"],
            "report_year": year,
            "report_period": period,
            "report_type": "quarterly" if period == "Q1" else "half_year",
            "report_title": title,
            "nature": "原创人工构造模拟报告；不对应真实公司或真实财务披露",
            "use_conditions": "允许随FINTRACE比赛提交件和团队演示包分发；不得当作真实财务事实或舞弊标签",
        })

    manifest = {
        "schema_version": 1,
        "dataset_id": "fintrace-quarterly-synthetic-v1",
        "company_id": COMPANY_ID,
        "company_name": COMPANY_NAME,
        "nature": "原创人工构造模拟样例，明确不是任何真实公司的财报",
        "use_conditions": "可随FINTRACE比赛提交件和团队演示包分发；不可作为真实财务或舞弊结论",
        "generator": "scripts/prepare_quarterly_samples.py",
        "documents": docs,
    }
    manifest_path = OUT / "source_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme = (
        "# 季度分析原创模拟样例\n\n"
        f"本目录由 `{manifest['generator']}` 生成。PDF 封面及每个财务表页均内含原创性质、用途及口径声明；"
        "公司名和代码均为虚构标识，不对应真实发行人。\n\n"
        "样例覆盖 2023/2024 Q1 与 H1 累计值，可推导两个年度 Q2，并展示同比与环比。"
        "2024Q1 经营现金流为负，用来演示保留差额且不计算增率。\n\n"
        "来源、SHA256、期间和使用条件见 [source_manifest.json](source_manifest.json)。"
        "重新生成会覆盖本目录中的同名原创样例文件。\n"
    )
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    return manifest_path


if __name__ == "__main__":
    print(prepare().relative_to(ROOT).as_posix())
