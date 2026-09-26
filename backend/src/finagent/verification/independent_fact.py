"""独立重读原始 PDF，核验一条 FinancialFactV2。

不使用 fact.evidence_ids，也不沿用提取阶段给出的坐标。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from finagent.schemas.financial_fact import decimal_to_str
from finagent.schemas.financial_fact_v2 import (
    CONFLICT,
    INSUFFICIENT,
    VERIFIED,
    FinancialFactV2,
    SourceRegion,
    TableCellEvidence,
    VerificationResult,
)
from finagent.schemas.text_pdf import PdfBBox

_AMOUNT = re.compile(r"^[(（]?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?[)）]?$|^[(（]?-?\d+\.\d+[)）]?$")
_METRICS = {
    "revenue": ("合并利润表", "其中：营业收入", "consolidated"),
    "net_profit_parent": ("合并利润表", "归属于母公司股东的净利润", "consolidated"),
    "operating_cash_flow": ("合并现金流量表", "经营活动产生的现金流量净额", "consolidated"),
    "non_recurring_total": ("非经常性损益项目和金额", "合计", "unknown"),
}


@dataclass(frozen=True, slots=True)
class IndependentFactCheck:
    """一次独立核验。evidence 只含本函数重新定位的单元格。"""

    result: VerificationResult
    evidence: tuple[TableCellEvidence, ...]


@dataclass(frozen=True, slots=True)
class _Line:
    page: int
    y0: float
    text: str
    words: tuple


def verify_financial_fact(pdf_path: Path, fact: FinancialFactV2) -> IndependentFactCheck:
    """按原文哈希、表题、行、列、单位、金额、期间和口径核验。不清楚则弃权。"""

    if not isinstance(fact, FinancialFactV2):
        raise TypeError("fact 必须是 FinancialFactV2。")
    path = Path(pdf_path)
    digest = _sha256(path)
    if digest is None:
        return _finish(fact, INSUFFICIENT, ("source_sha256",), ("无法读取原始 PDF。",), (), None)
    if digest.lower() != fact.source_sha256.lower():
        return _finish(fact, CONFLICT, ("source_sha256",), ("原文 SHA256 与事实不一致。",), (), None)
    spec = _METRICS.get(fact.metric_id)
    if spec is None or fact.period_end is None:
        return _finish(fact, INSUFFICIENT, ("metric",), ("指标或 period_end 不足以独立定位。",), (), None)
    title_name, row_name, scope = spec
    try:
        import pymupdf

        document = pymupdf.open(path)
    except Exception:
        return _finish(fact, INSUFFICIENT, ("pdf",), ("无法作为 PDF 打开。",), (), None)
    try:
        lines = _lines(document)
    finally:
        document.close()
    titles = [line for line in lines if _compact(title_name) in _compact(line.text)]
    if len(titles) != 1:
        return _finish(fact, INSUFFICIENT, ("table_title",), ("表题缺失或重复，不能确定表格。",), (), None)
    title = titles[0]
    window = _window(lines, title, title_name)
    if fact.scope != scope:
        return _finish(fact, CONFLICT, ("scope",), (f"表题对应口径是 {scope}，与事实不一致。",), (), None)
    header = _header(window, fact.period_end.year)
    if header is None:
        return _finish(fact, INSUFFICIENT, ("column",), ("缺少对应年度列表头。",), (), None)
    unit_line = _unit_line(window, title)
    if unit_line is None:
        return _finish(fact, INSUFFICIENT, ("unit",), ("缺少单位或币种。",), (), None)
    parsed_unit = _parse_unit(unit_line.text)
    if parsed_unit is None:
        return _finish(fact, INSUFFICIENT, ("unit",), ("单位或币种无法从原文独立确认。",), (), None)
    unit_name, currency_name, multiplier = parsed_unit
    if not _same_unit(fact, unit_name, currency_name, multiplier):
        return _finish(fact, CONFLICT, ("unit",), ("单位或币种与原文相反。",), (), None)
    period = _parse_period(header, window, fact.period_end.year)
    if period is None:
        return _finish(fact, INSUFFICIENT, ("period_end",), ("无法从表头独立确认期间。",), (), None)
    period_type, period_start, period_end = period
    if fact.period_type != period_type or fact.period_start != period_start or fact.period_end != period_end:
        return _finish(fact, CONFLICT, ("period_end",), ("事实期间与表头原文不一致。",), (), None)
    rows = _match_rows(window, row_name, title_name)
    if len(rows) == 0:
        return _finish(fact, INSUFFICIENT, ("row",), ("找不到对应行标签。",), (), None)
    if len(rows) > 1:
        return _finish(fact, INSUFFICIENT, ("row",), ("同一表内有重复行，不能确定金额。",), (), None)
    row, amounts = rows[0]
    chosen = _amount_for_header(amounts, header, fact.period_end.year)
    other = [item for item in amounts if item is not chosen]
    if chosen is None:
        return _finish(fact, INSUFFICIENT, ("value",), ("年度列下没有可定位的金额。",), (), None)
    if not _same_amount(chosen[4], fact.raw_value):
        if any(_same_amount(item[4], fact.raw_value) for item in other):
            reason = "金额出现在其他年度列，而不是 period_end 对应列。"
        else:
            reason = "定位到的金额与事实不一致。"
        return _finish(fact, CONFLICT, ("value",), (reason,), (), _evidence(fact, title, row, chosen, header, unit_line, parsed_unit, period))
    normalized = _number(chosen[4]) * multiplier
    if Decimal(fact.normalized_value) != normalized:
        return _finish(fact, CONFLICT, ("value",), ("规范值与原文金额乘独立单位倍率不一致。",), (), _evidence(fact, title, row, chosen, header, unit_line, parsed_unit, period))
    evidence = _evidence(fact, title, row, chosen, header, unit_line, parsed_unit, period)
    return _finish(
        fact,
        VERIFIED,
        ("source_sha256", "table_title", "row", "column", "unit", "value", "period_end", "scope"),
        (),
        ("未判断追溯调整，不把 restatement_status 当成已知。",),
        evidence,
    )


def _sha256(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _lines(document) -> list[_Line]:
    lines: list[_Line] = []
    for index, page in enumerate(document):
        grouped: dict[float, list] = {}
        for word in page.get_text("words"):
            grouped.setdefault(round(word[1], 0), []).append(word)
        for y0 in sorted(grouped):
            words = tuple(sorted(grouped[y0], key=lambda item: item[0]))
            lines.append(_Line(index + 1, y0, " ".join(item[4] for item in words), words))
    return lines


def _window(lines: list[_Line], title: _Line, title_name: str) -> list[_Line]:
    start = lines.index(title)
    chosen = []
    for line in lines[start : start + 80]:
        if chosen and line.page > title.page + 1:
            break
        if title_name == "非经常性损益项目和金额" and "采用公允价值计量" in line.text:
            break
        if chosen and line is not title and any(name in line.text for name in ("合并利润表", "合并现金流量表", "母公司利润表") if name != title_name):
            break
        chosen.append(line)
    return chosen


def _header(lines: list[_Line], year: int) -> _Line | None:
    token = str(year)
    hits = [
        line
        for line in lines
        if "年度报告" not in line.text and token in line.text and ("年度" in line.text or "年金额" in line.text)
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _unit_line(lines: list[_Line], title: _Line) -> _Line | None:
    hits = [line for line in lines if line.page >= title.page and "单位" in line.text and "币种" in line.text]
    if len(hits) != 1:
        return None
    return hits[0]


def _parse_unit(text: str) -> tuple[str, str, Decimal] | None:
    if "万元" in text:
        unit, multiplier = "万元", Decimal("10000")
    elif "亿元" in text:
        unit, multiplier = "亿元", Decimal("100000000")
    elif "元" in text:
        unit, multiplier = "元", Decimal("1")
    else:
        return None
    if "人民币" in text:
        currency = "人民币"
    elif "美元" in text:
        currency = "美元"
    else:
        return None
    return unit, currency, multiplier


def _same_unit(fact: FinancialFactV2, unit: str, currency: str, multiplier: Decimal) -> bool:
    aliases = {"人民币": {"人民币", "CNY", "RMB"}, "美元": {"美元", "USD"}}
    return fact.unit == unit and Decimal(fact.unit_multiplier) == multiplier and fact.currency in aliases[currency]


def _parse_period(header: _Line, window: list[_Line], year: int) -> tuple[str, object, object] | None:
    text = _compact(header.text)
    token = str(year)
    if f"{token}年度" not in text and f"{token}年金额" not in text:
        return None
    window_text = _compact("".join(line.text for line in window))
    if f"{token}年度" in text or f"{token}年金额" in text or f"{token}年1—12" in window_text or f"{token}年1-12" in window_text:
        return "duration", date(year, 1, 1), date(year, 12, 31)
    return None


def _label_words(line: _Line) -> tuple:
    amounts = [word for word in line.words if _AMOUNT.fullmatch(word[4])]
    if not amounts:
        return line.words
    left_edge = min(word[0] for word in amounts)
    return tuple(word for word in line.words if word[2] <= left_edge + 1)


def _match_rows(lines: list[_Line], row_name: str, title_name: str) -> list[tuple[_Line, tuple]]:
    target = _compact(row_name)
    found = []
    for index, line in enumerate(lines):
        amounts = tuple(word for word in line.words if _AMOUNT.fullmatch(word[4]))
        if not amounts:
            continue
        left = _label_words(line)
        if title_name == "非经常性损益项目和金额":
            matched = _compact("".join(word[4] for word in left)) == "合计"
            label_words = left
        else:
            parts = []
            if index > 0 and not any(_AMOUNT.fullmatch(word[4]) for word in lines[index - 1].words):
                parts.append(lines[index - 1].text)
            parts.append("".join(word[4] for word in left))
            if index + 1 < len(lines) and not any(_AMOUNT.fullmatch(word[4]) for word in lines[index + 1].words):
                parts.append(lines[index + 1].text)
            matched = target in _compact("".join(parts))
            label_words = lines[index - 1].words if parts and index > 0 and not left else left
        if matched and label_words:
            label = _Line(line.page, line.y0, "".join(word[4] for word in label_words), label_words)
            found.append((label, amounts))
    return found


def _amount_for_header(amounts: tuple, header: _Line, year: int):
    year_words = sorted(
        (word for word in header.words if word[4][:4].isdigit()),
        key=lambda word: word[0],
    )
    matches = [word for word in year_words if word[4].startswith(str(year))]
    if len(matches) != 1:
        return None
    year_word = matches[0]
    def center(word) -> float:
        return (word[0] + word[2]) / 2

    def nearest(amount):
        return min(year_words, key=lambda item: abs(center(amount) - center(item)))

    chosen = [word for word in amounts if nearest(word) == year_word]
    if len(chosen) != 1:
        return None
    return chosen[0]


def _same_amount(token: str, raw_value: str) -> bool:
    return _number(token) == _number(raw_value)


def _number(token: str) -> Decimal:
    text = token.replace(",", "").replace("，", "")
    if text.startswith(("(", "（")) and text.endswith((")", "）")):
        text = "-" + text[1:-1]
    return Decimal(text)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace(":", "：")


def _region(line_or_word, page: int | None = None) -> SourceRegion:
    if isinstance(line_or_word, _Line):
        words = line_or_word.words
        text = line_or_word.text
        page_number = line_or_word.page
    else:
        words = (line_or_word,)
        text = line_or_word[4]
        page_number = page or 0
    return SourceRegion(
        text=text,
        page=page_number,
        bbox=PdfBBox(
            x0=min(item[0] for item in words),
            y0=min(item[1] for item in words),
            x1=max(item[2] for item in words),
            y1=max(item[3] for item in words),
        ),
    )


def _evidence(fact, title, row, value_word, header, unit_line, parsed_unit, period) -> TableCellEvidence | None:
    if title is None or parsed_unit is None or period is None or value_word is None:
        return None
    unit_name, currency_name, multiplier = parsed_unit
    period_type, period_start, period_end = period
    value_region = _region(value_word, row.page if row else title.page)
    return TableCellEvidence(
        evidence_id=f"ind-{fact.fact_id}",
        document_id=fact.source_document_id,
        source_sha256=fact.source_sha256,
        pdf_page=value_region.page,
        printed_page=None,
        table_title=title.text,
        row_label=None if row is None else row.text,
        column_label=None if header is None else header.text,
        value_raw=value_word[4],
        value_normalized=decimal_to_str(_number(value_word[4]) * multiplier),
        unit=unit_name,
        currency=currency_name,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
        value_region=value_region,
        row_region=None if row is None else _region(row),
        column_region=None if header is None else _region(header),
        title_region=_region(title),
        unit_region=None if unit_line is None else _region(unit_line),
        extraction_method="independent_pymupdf_words",
        surrounding_text=None,
        limitations=("单位、币种和期间来自表头原文，不复制事实字段。",),
    )


def _finish(fact, status: str, checks: tuple[str, ...], conflicts: tuple[str, ...], limitations: tuple[str, ...], evidence) -> IndependentFactCheck:
    items = () if evidence is None else (evidence,)
    evidence_ids = tuple(item.evidence_id for item in items) if status == VERIFIED else tuple(item.evidence_id for item in items)
    if status != VERIFIED:
        evidence_ids = tuple(item.evidence_id for item in items if item.value_region is not None)
    result = VerificationResult(
        verification_id=f"verify-{fact.fact_id}",
        target_type="financial_fact",
        target_id=fact.fact_id,
        status=status,
        checks=checks,
        conflicts=conflicts if status == CONFLICT else (),
        evidence_ids=evidence_ids if status == VERIFIED else evidence_ids,
        limitations=limitations + (() if status == VERIFIED else ("未形成已核实结论。",)),
        verified_at=datetime.now(timezone.utc),
    )
    if status == INSUFFICIENT:
        result = VerificationResult(
            verification_id=result.verification_id,
            target_type="financial_fact",
            target_id=fact.fact_id,
            status=INSUFFICIENT,
            checks=checks,
            conflicts=(),
            evidence_ids=(),
            limitations=limitations + conflicts + ("证据不足。",),
            verified_at=result.verified_at,
        )
        items = ()
    return IndependentFactCheck(result=result, evidence=items)
