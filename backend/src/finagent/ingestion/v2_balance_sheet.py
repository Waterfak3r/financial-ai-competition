"""从已解析 PDF 中定位合并资产负债表的应收账款净额和存货净额。

提取结果只是候选事实；正式使用前仍须由 independent_fact 独立重读原始 PDF。
本模块不读取旧版年度事实字段，也不把空白金额解释为零。
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

_STATEMENT_TITLE = "合并资产负债表"
_METRICS = {
    "accounts_receivable_net": "应收账款",
    "inventory_net": "存货",
}
_UNIT_MULTIPLIERS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}
_AMOUNT_RE = re.compile(
    r"[（(]\s*[-－−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?\s*[）)]"
    r"|[（(]\s*[-－−]?\d+\.\d+\s*[）)]"
    r"|(?<![\d.])[-－−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?"
    r"|(?<![\d.])[-－−]?\d+\.\d+(?![\d.])"
)
_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_CURRENCY_RE = re.compile(r"币种\s*[:：]\s*(人民币|美元|港币|港元|欧元|日元|英镑|新加坡元)")


@dataclass(frozen=True, slots=True)
class BalanceSheetExtractionIssue:
    metric_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BalanceSheetV2Extraction:
    facts: tuple[FinancialFactV2, ...]
    evidence: tuple[TableCellEvidence, ...]
    issues: tuple[BalanceSheetExtractionIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class _Line:
    page_number: int
    block: PdfTextBlock
    text: str


@dataclass(frozen=True, slots=True)
class _Context:
    header: _Line
    years: tuple[int, ...]
    unit: str
    multiplier: Decimal
    currency: str
    currency_confirmed: bool
    unit_line: _Line


def extract_v2_balance_sheet_facts(
    parsed: ParsedTextPdf,
    *,
    company_id: str,
    report_year: int,
) -> BalanceSheetV2Extraction:
    """定位报告年及比较年的两项合并资产负债表时点事实。

    需要唯一表题、明确的 12 月 31 日双年度列表头、单位以及对应行的两个金额。
    单位已知但币种未披露时可以保留候选值，币种写为“未披露”，由独立核验弃权。
    """

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
        message = "未找到唯一的合并资产负债表表题。"
        return BalanceSheetV2Extraction((), (), tuple(_issue(metric, code, message) for metric in _METRICS))
    title_index = titles[0]
    title = lines[title_index]
    body = _table_body(lines, title_index)

    context, context_issue = _table_context(body, report_year)
    if context_issue is not None:
        return BalanceSheetV2Extraction(
            (), (), tuple(_issue(metric, context_issue[0], context_issue[1]) for metric in _METRICS)
        )
    assert context is not None

    facts: list[FinancialFactV2] = []
    evidences: list[TableCellEvidence] = []
    issues: list[BalanceSheetExtractionIssue] = []
    for metric_id, row_name in _METRICS.items():
        candidates = _row_candidates(body, row_name)
        if not candidates:
            issues.append(_issue(metric_id, "row_not_found", f"合并资产负债表中未找到唯一的“{row_name}”金额行。"))
            continue
        if len(candidates) != 1:
            issues.append(_issue(metric_id, "ambiguous_row", f"“{row_name}”金额行重复，无法确认。"))
            continue
        row, amounts = candidates[0]
        if len(amounts) != len(context.years) or len(amounts) != 2:
            issues.append(
                _issue(metric_id, "amount_alignment", "行金额数量与两个年末列不一致；缺失值不按零处理。")
            )
            continue
        if not context.currency_confirmed:
            issues.append(_issue(metric_id, "currency_undisclosed", "表头未披露币种；事实将保持未确认，不能推断为人民币。"))
        for year in (report_year, report_year - 1):
            try:
                amount_index = context.years.index(year)
                raw_value = amounts[amount_index]
                numeric_value = _parse_amount(raw_value)
            except (ValueError, InvalidOperation):
                issues.append(_issue(metric_id, "amount_unreadable", f"{year} 年金额无法按 Decimal 确认。"))
                continue
            role = ROLE_CURRENT if year == report_year else ROLE_COMPARATIVE
            fact_id = f"{parsed.document_id}:{metric_id}:{year}"
            limitations = ["资产负债表余额是 instant 时点事实。", "追溯调整状态未知，未推断可比性。"]
            if not context.currency_confirmed:
                limitations.append("表头没有明确披露币种；币种标记为“未披露”，需独立核验弃权。")
            fact = FinancialFactV2(
                fact_id=fact_id,
                metric_id=metric_id,
                company_id=company_id,
                label_raw=row_name,
                raw_value=raw_value,
                normalized_value=decimal_to_str(numeric_value * context.multiplier),
                currency=context.currency,
                unit_multiplier=decimal_to_str(context.multiplier),
                unit=context.unit,
                report_year=report_year,
                period_start=None,
                period_end=date(year, 12, 31),
                period_type="instant",
                frequency=FREQUENCY_ANNUAL,
                statement_type="balance_sheet",
                scope=SCOPE_CONSOLIDATED,
                comparison_role=role,
                restatement_status=RESTATEMENT_UNKNOWN,
                source_document_id=parsed.document_id,
                source_sha256=parsed.source_sha256,
                evidence_ids=(f"extract-{fact_id}",),
                extraction_method="parsed_pdf_balance_sheet_row_and_date_columns",
                limitations=tuple(limitations),
            )
            facts.append(fact)
            evidences.append(
                _evidence(
                    fact,
                    title,
                    row,
                    amounts[amount_index],
                    context,
                    context.years[amount_index],
                )
            )
    return BalanceSheetV2Extraction(tuple(facts), tuple(evidences), tuple(issues))


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


def _table_context(body: list[_Line], report_year: int) -> tuple[_Context | None, tuple[str, str] | None]:
    grouped: dict[tuple[int, int], list[_Line]] = {}
    for line in body:
        grouped.setdefault((line.page_number, line.block.block_index), []).append(line)
    header_candidates: list[tuple[_Line, tuple[int, ...]]] = []
    for group in grouped.values():
        joined = _compact("".join(line.text for line in group))
        dates = tuple((int(year), int(month), int(day)) for year, month, day in _DATE_RE.findall(joined))
        years = tuple(year for year, _, _ in dates)
        if len(dates) == 2 and len(set(years)) == 2 and all((month, day) == (12, 31) for _, month, day in dates):
            header_candidates.append((group[0], years))
    signatures = [years for _, years in header_candidates]
    if not signatures or any(signature != signatures[0] for signature in signatures[1:]):
        return None, ("missing_or_conflicting_year_header", "无法独立定位两个一致的 12 月 31 日年末列。")
    years = signatures[0]
    if set(years) != {report_year, report_year - 1}:
        return None, ("missing_year_header", "表头没有同时披露报告年和上一年的年末列。")

    unit_lines = [line for line in body if re.search(r"^单位\s*[:：]", _compact(line.text))]
    parsed_units = [_parse_unit(line.text) for line in unit_lines]
    if not parsed_units or any(item is None for item in parsed_units):
        return None, ("missing_unit", "表头缺少明确单位，无法规范化金额。")
    unit_signatures = [item for item in parsed_units if item is not None]
    if any(item != unit_signatures[0] for item in unit_signatures[1:]):
        return None, ("conflicting_unit", "表内单位或币种披露相互矛盾。")
    unit, currency, multiplier = unit_signatures[0]
    header = header_candidates[-1][0]
    unit_line = unit_lines[-1]
    return (
        _Context(
            header=header,
            years=years,
            unit=unit,
            multiplier=multiplier,
            currency=currency or "未披露",
            currency_confirmed=currency is not None,
            unit_line=unit_line,
        ),
        None,
    )


def _parse_unit(text: str) -> tuple[str, str | None, Decimal] | None:
    compact = _compact(text)
    unit_match = re.search(r"单位[:：](亿元|万元|元)", compact)
    if unit_match is None:
        return None
    currency_matches = _CURRENCY_RE.findall(compact)
    if len(set(currency_matches)) > 1:
        return None
    unit = unit_match.group(1)
    currency = currency_matches[0] if currency_matches else None
    return unit, currency, _UNIT_MULTIPLIERS[unit]


def _row_candidates(body: list[_Line], row_name: str) -> list[tuple[_Line, tuple[str, ...]]]:
    candidates: list[tuple[_Line, tuple[str, ...]]] = []
    seen_blocks: set[tuple[int, int]] = set()
    for line in body:
        key = (line.page_number, line.block.block_index)
        if key in seen_blocks:
            continue
        seen_blocks.add(key)
        row = _Line(line.page_number, line.block, line.block.text)
        amounts = tuple(match.group(0).strip() for match in _AMOUNT_RE.finditer(row.text))
        if not amounts:
            continue
        compact = _compact(row.text)
        if not _has_exact_row_prefix(compact, row_name):
            continue
        candidates.append((row, amounts))
    return candidates


def _has_exact_row_prefix(compact: str, row_name: str) -> bool:
    if not compact.startswith(row_name):
        return False
    suffix = compact[len(row_name) :]
    if not suffix:
        return True
    if re.match(r"^(?:[一二三四五六七八九十]+、\d+|(?:附注)?\d+)", suffix):
        return True
    return suffix.startswith(("（净额）", "(净额)", "（净值）", "(净值)"))


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
    value_region = _region(row)
    row_region = _region(row)
    return TableCellEvidence(
        evidence_id=f"extract-{fact.fact_id}",
        document_id=fact.source_document_id,
        source_sha256=fact.source_sha256,
        pdf_page=row.page_number,
        printed_page=None,
        table_title=_STATEMENT_TITLE,
        row_label=fact.label_raw,
        column_label=f"{year}年12月31日",
        value_raw=raw_value,
        value_normalized=fact.normalized_value,
        unit=context.unit,
        currency=context.currency,
        period_start=None,
        period_end=date(year, 12, 31),
        period_type="instant",
        value_region=value_region,
        row_region=row_region,
        column_region=_region(context.header),
        title_region=_region(title),
        unit_region=_region(context.unit_line),
        extraction_method="parsed_pdf_balance_sheet_row_and_date_columns",
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


def _issue(metric_id: str, code: str, message: str) -> BalanceSheetExtractionIssue:
    return BalanceSheetExtractionIssue(metric_id=metric_id, code=code, message=message)


def _is_statement_title(text: str) -> bool:
    compact = _compact(text)
    return bool(re.fullmatch(r"(?:[一二三四五六七八九十百千万]+、)?(?:合并|母公司).{0,12}(?:资产负债表|利润表|现金流量表|所有者权益变动表)", compact))


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace(":", "：")
