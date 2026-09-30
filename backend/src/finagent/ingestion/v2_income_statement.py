"""从已解析 PDF 定位合并利润表的合并净利润和营业成本。

结果仅是候选事实。独立使用前必须由 verification.independent_fact 重读原始 PDF。
本模块不读取旧年度事实，也不从其他指标推导金额。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from finagent.ingestion.errors import PdfInputError
from finagent.schemas.financial_fact import decimal_to_str
from finagent.schemas.financial_fact_v2 import (
    FREQUENCY_ANNUAL,
    RESTATEMENT_UNKNOWN,
    ROLE_COMPARATIVE,
    ROLE_CURRENT,
    SCOPE_CONSOLIDATED,
    FinancialFactV2,
    SourceRegion,
    TableCellEvidence,
)
from finagent.schemas.text_pdf import COORDINATE_SYSTEM, ParsedTextPdf, PdfBBox, PdfTextBlock

_STATEMENT_TITLE = "合并利润表"
_ROW_LABELS = {
    "net_profit_consolidated": "净利润",
    "cost_of_goods_sold": "其中：营业成本",
}
_FACT_LABELS = {
    "net_profit_consolidated": "五、净利润",
    "cost_of_goods_sold": "其中：营业成本",
}
_UNIT_MULTIPLIERS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}
_AMOUNT_RE = re.compile(
    r"[（(]\s*[-－−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?\s*[）)]"
    r"|[（(]\s*[-－−]?\d+\.\d+\s*[）)]"
    r"|(?<![\d.])[-－−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?"
    r"|(?<![\d.])[-－−]?\d+\.\d+(?![\d.])"
)
_ANNUAL_COLUMN_RE = re.compile(r"(\d{4})年(度|金额)")
_CURRENCY_RE = re.compile(r"币种\s*[:：]\s*(人民币|美元|港币|港元|欧元|日元|英镑|新加坡元)")


@dataclass(frozen=True, slots=True)
class IncomeStatementExtractionIssue:
    metric_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class IncomeStatementV2Extraction:
    facts: tuple[FinancialFactV2, ...]
    evidence: tuple[TableCellEvidence, ...]
    issues: tuple[IncomeStatementExtractionIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class _Line:
    page_number: int
    block: PdfTextBlock
    text: str


@dataclass(frozen=True, slots=True)
class _Context:
    header: _Line
    years: tuple[int, int]
    unit: str
    multiplier: Decimal
    currency: str
    currency_confirmed: bool
    unit_line: _Line


def extract_v2_income_statement_facts(
    parsed: ParsedTextPdf,
    *,
    company_id: str,
    report_year: int,
) -> IncomeStatementV2Extraction:
    """提取报告年和比较年的合并利润表年度流量事实。"""

    if not isinstance(parsed, ParsedTextPdf):
        raise TypeError("parsed 必须是 ParsedTextPdf。")
    if not isinstance(company_id, str) or company_id.strip() == "" or company_id != company_id.strip():
        raise PdfInputError("company_id 必须是非空且首尾无空白的字符串。")
    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise PdfInputError("report_year 必须是 1900 到 2100 之间的整数。")
    if parsed.coordinate_system != COORDINATE_SYSTEM:
        raise PdfInputError("ParsedTextPdf 坐标系不受支持。")

    lines = _document_lines(parsed)
    titles = [index for index, line in enumerate(lines) if _compact(line.text) == _STATEMENT_TITLE]
    if len(titles) != 1:
        code = "table_not_found" if not titles else "ambiguous_table"
        issue = tuple(_issue(metric, code, "未找到唯一的合并利润表表题。") for metric in _ROW_LABELS)
        return IncomeStatementV2Extraction((), (), issue)

    title_index = titles[0]
    title = lines[title_index]
    body = _table_body(lines, title_index)
    context, context_issue = _table_context(body, report_year)
    if context_issue is not None:
        issue = tuple(_issue(metric, context_issue[0], context_issue[1]) for metric in _ROW_LABELS)
        return IncomeStatementV2Extraction((), (), issue)
    assert context is not None

    facts: list[FinancialFactV2] = []
    evidences: list[TableCellEvidence] = []
    issues: list[IncomeStatementExtractionIssue] = []
    for metric_id, row_name in _ROW_LABELS.items():
        candidates = _row_candidates(body, metric_id, row_name)
        if not candidates:
            issues.append(_issue(metric_id, "row_not_found", f"合并利润表中未找到唯一的“{row_name}”金额行。"))
            continue
        if len(candidates) != 1:
            issues.append(_issue(metric_id, "ambiguous_row", f"“{row_name}”金额行重复，无法确认。"))
            continue
        row, amounts = candidates[0]
        if len(amounts) != 2:
            issues.append(
                _issue(metric_id, "amount_alignment", "行金额数量与报告年及比较年两个年度列不一致；缺失值不按零处理。")
            )
            continue
        if not context.currency_confirmed:
            issues.append(_issue(metric_id, "currency_undisclosed", "表头未披露币种；事实币种标为“未披露”，不推断为人民币。"))

        for year in (report_year, report_year - 1):
            try:
                amount_index = context.years.index(year)
                raw_value = amounts[amount_index]
                normalized_value = decimal_to_str(_parse_amount(raw_value) * context.multiplier)
            except (ValueError, InvalidOperation):
                issues.append(_issue(metric_id, "amount_unreadable", f"{year} 年金额无法按 Decimal 确认。"))
                continue
            role = ROLE_CURRENT if year == report_year else ROLE_COMPARATIVE
            fact_id = f"{parsed.document_id}:{metric_id}:{year}"
            limitations = ["利润表金额是 annual duration 流量事实。", "追溯调整状态未知，未推断两年可比。"]
            if not context.currency_confirmed:
                limitations.append("表头没有明确披露币种；独立核验应弃权。")
            fact = FinancialFactV2(
                fact_id=fact_id,
                metric_id=metric_id,
                company_id=company_id,
                label_raw=_FACT_LABELS[metric_id],
                raw_value=raw_value,
                normalized_value=normalized_value,
                currency=context.currency,
                unit_multiplier=decimal_to_str(context.multiplier),
                unit=context.unit,
                report_year=report_year,
                period_start=date(year, 1, 1),
                period_end=date(year, 12, 31),
                period_type="duration",
                frequency=FREQUENCY_ANNUAL,
                statement_type="income_statement",
                scope=SCOPE_CONSOLIDATED,
                comparison_role=role,
                restatement_status=RESTATEMENT_UNKNOWN,
                source_document_id=parsed.document_id,
                source_sha256=parsed.source_sha256,
                evidence_ids=(f"extract-{fact_id}",),
                extraction_method="parsed_pdf_income_statement_row_and_annual_columns",
                limitations=tuple(limitations),
            )
            facts.append(fact)
            evidences.append(
                _evidence(fact, title, row, raw_value, context, year)
            )
    return IncomeStatementV2Extraction(tuple(facts), tuple(evidences), tuple(issues))


def _document_lines(parsed: ParsedTextPdf) -> list[_Line]:
    return [
        _Line(page.page_number, block, piece)
        for page in parsed.pages
        for block in page.blocks
        for piece in (block.text.splitlines() or [""])
    ]


def _table_body(lines: list[_Line], title_index: int) -> list[_Line]:
    body: list[_Line] = []
    title_page = lines[title_index].page_number
    for line in lines[title_index:]:
        if body and _is_statement_title(line.text) and _compact(line.text) != _STATEMENT_TITLE:
            break
        if line.page_number > title_page + 2:
            break
        body.append(line)
    return body


def _table_context(
    body: list[_Line], report_year: int
) -> tuple[_Context | None, tuple[str, str] | None]:
    grouped: dict[tuple[int, int], list[_Line]] = {}
    for line in body:
        grouped.setdefault((line.page_number, round(line.block.bbox.y0)), []).append(line)
    headers: list[tuple[_Line, tuple[int, ...]]] = []
    for group in grouped.values():
        merged = _merge_lines(group)
        joined = _compact(merged.text)
        years = tuple(int(year) for year, _kind in _ANNUAL_COLUMN_RE.findall(joined))
        if len(years) == 2 and len(set(years)) == 2:
            headers.append((merged, years))
    signatures = [years for _, years in headers]
    if not signatures or any(signature != signatures[0] for signature in signatures[1:]):
        return None, ("missing_or_conflicting_year_header", "无法从合并利润表确认一致的两个年度列。")
    years = signatures[0]
    if years != (report_year, report_year - 1):
        return None, ("missing_year_header", "年度列没有按报告年、比较年顺序同时披露。")

    unit_lines = [line for line in body if re.search(r"^单位\s*[:：]", _compact(line.text))]
    parsed_units = [_parse_unit(line.text) for line in unit_lines]
    if not parsed_units or any(item is None for item in parsed_units):
        return None, ("missing_unit", "合并利润表表头缺少明确单位，无法规范化金额。")
    unit_signatures = [item for item in parsed_units if item is not None]
    if any(item != unit_signatures[0] for item in unit_signatures[1:]):
        return None, ("conflicting_unit", "合并利润表中的单位或币种披露相互矛盾。")
    unit, currency, multiplier = unit_signatures[0]
    return (
        _Context(
            header=headers[-1][0],
            years=(years[0], years[1]),
            unit=unit,
            multiplier=multiplier,
            currency=currency or "未披露",
            currency_confirmed=currency is not None,
            unit_line=unit_lines[-1],
        ),
        None,
    )


def _parse_unit(text: str) -> tuple[str, str | None, Decimal] | None:
    compact = _compact(text)
    match = re.search(r"单位[:：](亿元|万元|元)", compact)
    if match is None:
        return None
    currencies = _CURRENCY_RE.findall(compact)
    if len(set(currencies)) > 1:
        return None
    unit = match.group(1)
    return unit, currencies[0] if currencies else None, _UNIT_MULTIPLIERS[unit]


def _row_candidates(body: list[_Line], metric_id: str, row_name: str) -> list[tuple[_Line, tuple[str, ...]]]:
    candidates: list[tuple[_Line, tuple[str, ...]]] = []
    grouped: dict[tuple[int, int], list[_Line]] = {}
    for line in body:
        grouped.setdefault((line.page_number, round(line.block.bbox.y0)), []).append(line)
    for group in grouped.values():
        row = _merge_lines(group)
        compact = _compact(row.text)
        if not _has_exact_row_prefix(compact, metric_id, row_name):
            continue
        amounts = tuple(match.group(0).strip() for match in _AMOUNT_RE.finditer(row.text))
        candidates.append((row, amounts))
    return candidates


def _merge_lines(lines: list[_Line]) -> _Line:
    """Join same-baseline table fragments while retaining their combined bounds."""

    blocks_by_index = {line.block.block_index: line.block for line in lines}
    blocks = sorted(blocks_by_index.values(), key=lambda block: block.bbox.x0)
    text = " ".join(block.text.replace("\n", " ") for block in blocks)
    bbox = PdfBBox(
        min(block.bbox.x0 for block in blocks),
        min(block.bbox.y0 for block in blocks),
        max(block.bbox.x1 for block in blocks),
        max(block.bbox.y1 for block in blocks),
    )
    merged = PdfTextBlock(blocks[0].block_index, text, bbox)
    return _Line(lines[0].page_number, merged, text)


def _has_exact_row_prefix(compact: str, metric_id: str, row_name: str) -> bool:
    if metric_id == "net_profit_consolidated" and compact.startswith("五、"):
        compact = compact[2:]
    if not compact.startswith(row_name):
        return False
    suffix = compact[len(row_name) :]
    if not suffix:
        return True
    if metric_id == "net_profit_consolidated" and suffix.startswith(("（净", "(净", "（亏损", "(亏损")):
        return True
    return bool(re.match(r"^(?:[一二三四五六七八九十]+、\d+|(?:附注)?\d+)", suffix))


def _parse_amount(token: str) -> Decimal:
    text = token.strip().replace("，", "").replace(",", "").replace("－", "-").replace("−", "-")
    if text.startswith(("（", "(")) and text.endswith(("）", ")")):
        inner = text[1:-1].strip()
        value = Decimal(inner)
        if value.is_signed():
            raise ValueError("括号与负号同时出现。")
        return -value
    return Decimal(text)


def _evidence(
    fact: FinancialFactV2,
    title: _Line,
    row: _Line,
    raw_value: str,
    context: _Context,
    year: int,
) -> TableCellEvidence:
    return TableCellEvidence(
        evidence_id=f"extract-{fact.fact_id}",
        document_id=fact.source_document_id,
        source_sha256=fact.source_sha256,
        pdf_page=row.page_number,
        printed_page=None,
        table_title=_STATEMENT_TITLE,
        row_label=fact.label_raw,
        column_label=f"{year}年度",
        value_raw=raw_value,
        value_normalized=fact.normalized_value,
        unit=context.unit,
        currency=context.currency,
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        period_type="duration",
        value_region=_region(row),
        row_region=_region(row),
        column_region=_region(context.header),
        title_region=_region(title),
        unit_region=_region(context.unit_line),
        extraction_method="parsed_pdf_income_statement_row_and_annual_columns",
        surrounding_text=row.block.text,
        limitations=(
            "定位坐标来自 ParsedTextPdf 文字块，值框包含整行文字块；须重新读取原始 PDF 独立核验。",
            "未知追溯调整状态不表示两列已证实可比。",
        ),
    )


def _region(line: _Line) -> SourceRegion:
    block = line.block
    return SourceRegion(
        text=block.text,
        page=line.page_number,
        bbox=PdfBBox(block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1),
    )


def _issue(metric_id: str, code: str, message: str) -> IncomeStatementExtractionIssue:
    return IncomeStatementExtractionIssue(metric_id=metric_id, code=code, message=message)


def _is_statement_title(text: str) -> bool:
    compact = _compact(text)
    return bool(
        re.fullmatch(
            r"(?:[一二三四五六七八九十百千万]+、)?(?:合并|母公司).{0,12}(?:资产负债表|利润表|现金流量表|所有者权益变动表)",
            compact,
        )
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace(":", "：")
