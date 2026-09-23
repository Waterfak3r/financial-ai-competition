"""从已解析的文本型 PDF 中提取四项年度事实。

只认表标题、年度表头、单位和币种。不使用固定页码或金额。
缺字段或无法对齐时弃权，不把空单元格当成零。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from finagent.ingestion.errors import PdfInputError
from finagent.schemas.financial_fact import (
    COLUMN_ROLE_COMPARATIVE,
    COLUMN_ROLE_CURRENT,
    PERIOD_TYPE_ANNUAL,
    RESTATEMENT_STATUS_UNKNOWN,
    ExtractionIssue,
    ExtractionResult,
    FactHit,
    FinancialFact,
    decimal_to_str,
)
from finagent.schemas.text_pdf import ParsedTextPdf, PdfTextBlock

_STATEMENT_TITLES = (
    "合并资产负债表",
    "母公司资产负债表",
    "合并利润表",
    "母公司利润表",
    "合并现金流量表",
    "母公司现金流量表",
    "合并所有者权益变动表",
    "母公司所有者权益变动表",
)
_NON_RECURRING_TITLE = "非经常性损益项目和金额"
_UNIT_MULTIPLIERS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}
_AMOUNT_RE = re.compile(
    r"[（(]\s*[-－−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*[）)]"
    r"|[（(]\s*[-－−]?\d+\.\d+\s*[）)]"
    r"|(?<![\d.])[-－−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|(?<![\d.])[-－−]?\d+\.\d+(?![\d.])"
)
_REVENUE_MAIN = "一、营业收入"
_REVENUE_DETAIL_LIMIT = "合并利润表没有“一、营业收入”主行，改用“其中：营业收入”。"
_YEAR_RE = re.compile(r"(\d{4})年(?:度|金额)")
_NOTE_RE = re.compile(r"^[一二三四五六七八九十]+、\d+[（(]?\d*[）)]?$")
_SECTION_RE = re.compile(r"^\s*[一二三四五六七八九十百千]+、\s*\S")
_NOTE_SECTION_RE = re.compile(r"^\s*[一二三四五六七八九十]+、\s*\d")
_HARD_ROW_RE = re.compile(
    r"^(?:其中[:：]|加[:：]|减[:：]|\d+[\.．](?!\d)|[（(][一二三四五六七八九十]+[）)])"
)
_METHOD = "table_title_and_year_header"


@dataclass(frozen=True, slots=True)
class _Line:
    page_number: int
    block_index: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    block_text: str


@dataclass(frozen=True, slots=True)
class _Target:
    indicator_name: str
    table_title: str
    stop_at_section: bool
    match: str


_TARGETS = (
    _Target("营业收入", "合并利润表", False, "revenue"),
    _Target("归属于母公司股东的净利润", "合并利润表", False, "parent_profit"),
    _Target("经营活动产生的现金流量净额", "合并现金流量表", False, "operating_cash"),
    _Target("披露的非经常性损益合计", _NON_RECURRING_TITLE, True, "non_recurring_total"),
)


def extract_annual_financial_facts(
    parsed: ParsedTextPdf,
    company_id: str,
    report_year: int,
) -> ExtractionResult:
    """提取当年与上一年各四项事实。报告年度来自参数，不写死在规则里。"""

    if not isinstance(company_id, str) or company_id.strip() == "" or company_id != company_id.strip():
        raise PdfInputError("company_id 必须是非空且首尾无空白的字符串。")
    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise PdfInputError("report_year 必须是 1900 到 2100 之间的整数。")
    lines = _document_lines(parsed)
    facts: list[FinancialFact] = []
    issues: list[ExtractionIssue] = []
    for target in _TARGETS:
        found, problem = _extract_target(parsed, lines, company_id, report_year, target)
        facts.extend(found)
        if problem is not None:
            issues.append(problem)
    return ExtractionResult(facts=tuple(facts), issues=tuple(issues))


def _extract_target(
    parsed: ParsedTextPdf,
    lines: list[_Line],
    company_id: str,
    report_year: int,
    target: _Target,
) -> tuple[list[FinancialFact], ExtractionIssue | None]:
    title_at = [index for index, line in enumerate(lines) if _is_title(line.text, target.table_title)]
    if not title_at:
        return [], _issue(target, "table_not_found", f"未找到表标题“{target.table_title}”。")
    if len(title_at) > 1:
        return [], _issue(target, "ambiguous_table", f"表标题“{target.table_title}”出现多次，无法确定采用哪一张表。")
    body = _table_body(lines, title_at[0], target)
    context, context_issue = _table_context(body, target, report_year)
    if context_issue is not None:
        return [], context_issue
    rows = _rows([line for line in body if not _is_meta_line(line.text) and not _is_furniture(line.text)])
    matched, select_issue, extra_limits = _select_rows(target, rows)
    if select_issue is not None:
        return [], select_issue
    amounts = _row_amounts(matched[0])
    if len(amounts) != len(context.years):
        return [], _issue(
            target,
            "amount_alignment",
            "金额个数与年度表头列数不一致，空缺位置无法判断，不猜测数值。",
        )
    built: list[FinancialFact] = []
    hits = _hits(matched[0])
    limitations = _limitations(target, hits, extra_limits)
    for year in (report_year, report_year - 1):
        token = amounts[context.years.index(year)]
        try:
            normalized = decimal_to_str(_parse_amount(token) * context.unit_multiplier)
        except (InvalidOperation, ValueError):
            return [], _issue(target, "amount_unreadable", f"{year} 年金额无法按十进制解析。")
        built.append(
            FinancialFact(
                document_id=parsed.document_id,
                source_sha256=parsed.source_sha256,
                company_id=company_id,
                indicator_name=target.indicator_name,
                table_name=target.table_title,
                raw_value=token,
                normalized_value=normalized,
                report_year=year,
                period_label=_period_label(year, context.period_style),
                period_type=PERIOD_TYPE_ANNUAL,
                column_role=COLUMN_ROLE_CURRENT if year == report_year else COLUMN_ROLE_COMPARATIVE,
                restatement_status=RESTATEMENT_STATUS_UNKNOWN,
                unit_multiplier=decimal_to_str(context.unit_multiplier),
                currency=context.currency,
                statement_scope=context.scope,
                extraction_method=_METHOD,
                hits=hits,
                limitations=limitations,
            )
        )
    return built, None


@dataclass(frozen=True, slots=True)
class _Context:
    years: tuple[int, ...]
    period_style: str
    unit_multiplier: Decimal
    currency: str
    scope: str


def _table_context(
    body: list[_Line],
    target: _Target,
    report_year: int,
) -> tuple[_Context | None, ExtractionIssue | None]:
    meta = [line.text for line in body if _is_meta_line(line.text)]
    meta_text = "\n".join(meta)
    units = re.findall(r"单位[:：]\s*(亿元|万元|元)", meta_text)
    currencies = re.findall(r"币种[:：]\s*(人民币|美元|港元|港币|欧元|日元)", meta_text)
    years: list[int] = []
    styles: dict[int, str] = {}
    for text in meta:
        for match in re.finditer(r"(\d{4})年(度|金额)", _compact(text)):
            year = int(match.group(1))
            style = match.group(2)
            previous = styles.get(year)
            if previous is not None and previous != style:
                return None, _issue(target, "conflicting_year_header", f"{year} 年的表头期间写法不一致。")
            if year not in styles:
                years.append(year)
            styles[year] = style
    if not units:
        return None, _issue(target, "missing_unit", "表头区域没有可识别的单位。")
    if len(set(units)) > 1:
        return None, _issue(target, "conflicting_unit", "表头区域的单位互相矛盾。")
    if not currencies:
        return None, _issue(target, "missing_currency", "表头区域没有可识别的币种。")
    if len(set(currencies)) > 1:
        return None, _issue(target, "conflicting_currency", "表头区域的币种互相矛盾。")
    if report_year not in years or report_year - 1 not in years:
        return None, _issue(target, "missing_year_header", "年度表头未同时给出报告年度和上一年度。")
    style = styles[report_year]
    if styles[report_year - 1] != style:
        return None, _issue(target, "conflicting_year_header", "报告年度与上一年度的表头期间写法不一致。")
    scope = "合并" if "合并" in target.table_title else "披露表格口径"
    return _Context(tuple(years), style, _UNIT_MULTIPLIERS[units[0]], currencies[0], scope), None


def _table_body(lines: list[_Line], title_index: int, target: _Target) -> list[_Line]:
    start = lines[title_index]
    body: list[_Line] = []
    last_page = start.page_number
    for line in lines[title_index + 1 :]:
        if line.page_number > last_page + 1:
            break
        last_page = max(last_page, line.page_number)
        if target.stop_at_section and _is_later_section(line.text):
            break
        other = _statement_title(line.text)
        if other is not None and other != target.table_title:
            break
        body.append(line)
    return body


def _rows(lines: list[_Line]) -> list[list[_Line]]:
    rows: list[list[_Line]] = []
    current: list[_Line] = []
    for line in lines:
        if not line.text.strip():
            if current:
                current.append(line)
            continue
        if current and _starts_new_row(line.text, current):
            rows.append(current)
            current = [line]
            continue
        current.append(line)
    if current:
        rows.append(current)
    return rows


def _starts_new_row(text: str, current: list[_Line]) -> bool:
    if _unmatched_open(_row_text(current)):
        return False
    stripped = text.strip()
    compact = _compact(stripped)
    if compact in {"合计", "小计"} or _HARD_ROW_RE.match(stripped):
        return True
    if re.match(r"^[一二三四五六七八九十]+、", stripped) and not _NOTE_SECTION_RE.match(stripped):
        return True
    if not _has_amount(current):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", stripped)) and not _is_amount_only(stripped)


def _select_rows(
    target: _Target,
    rows: list[list[_Line]],
) -> tuple[list[list[_Line]], ExtractionIssue | None, tuple[str, ...]]:
    if target.match == "revenue":
        return _select_revenue_rows(target, rows)
    matched = [row for row in rows if _row_matches(target.match, _row_label(row))]
    if not matched:
        return [], _issue(target, "row_not_found", f"在“{target.table_title}”中未找到对应行。"), ()
    if len(matched) > 1:
        return [], _issue(target, "ambiguous_row", f"在“{target.table_title}”中找到多行同名项目，已弃权。"), ()
    return matched, None, ()


def _select_revenue_rows(
    target: _Target,
    rows: list[list[_Line]],
) -> tuple[list[list[_Line]], ExtractionIssue | None, tuple[str, ...]]:
    main = [row for row in rows if _row_label(row) == _REVENUE_MAIN]
    if len(main) > 1:
        return [], _issue(target, "ambiguous_row", "合并利润表中“一、营业收入”主行重复，已弃权。"), ()
    if len(main) == 1:
        return main, None, ()
    detail = [row for row in rows if _is_revenue_detail(_row_label(row))]
    if len(detail) > 1:
        return [], _issue(target, "ambiguous_row", "合并利润表中“其中：营业收入”重复，且没有唯一主行，已弃权。"), ()
    if len(detail) == 1:
        return detail, None, (_REVENUE_DETAIL_LIMIT,)
    return [], _issue(target, "row_not_found", "在“合并利润表”中未找到“一、营业收入”或“其中：营业收入”。"), ()


def _is_revenue_detail(label: str) -> bool:
    return label.startswith("其中") and re.sub(r"^其中[:：]", "", label) == "营业收入"


def _row_matches(kind: str, label: str) -> bool:
    if kind == "parent_profit":
        return "归属于母公司股东的净利润" in label and "扣除" not in label
    if kind == "operating_cash":
        return "经营活动产生的现金流量净额" in label and "小计" not in label
    if kind == "non_recurring_total":
        return label == "合计"
    return False


def _row_label(row: list[_Line]) -> str:
    parts: list[str] = []
    for line in row:
        stripped = line.text.strip()
        if not stripped or _is_amount_only(stripped) or _NOTE_RE.fullmatch(_compact(stripped)):
            continue
        parts.append(stripped)
    return _compact("".join(parts))


def _row_amounts(row: list[_Line]) -> list[str]:
    tokens: list[str] = []
    for line in row:
        tokens.extend(match.group(0).strip() for match in _AMOUNT_RE.finditer(line.text))
    return tokens


def _row_text(row: list[_Line]) -> str:
    return "".join(line.text for line in row)


def _hits(row: list[_Line]) -> tuple[FactHit, ...]:
    seen: set[tuple[int, int]] = set()
    hits: list[FactHit] = []
    for line in row:
        if not line.text.strip():
            continue
        key = (line.page_number, line.block_index)
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            FactHit(
                page_number=line.page_number,
                block_index=line.block_index,
                text=line.block_text,
                x0=line.x0,
                y0=line.y0,
                x1=line.x1,
                y1=line.y1,
            )
        )
    return tuple(hits)


def _limitations(
    target: _Target,
    hits: tuple[FactHit, ...],
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    notes: list[str] = list(extra)
    if len(hits) > 1:
        notes.append("行名或金额分布在相邻文字块中，已按阅读顺序拼接。")
    if target.match == "non_recurring_total":
        notes.append("表题未说明合并或母公司，口径记为披露表格口径。")
    notes.append("本结果只来自文字块定位，不是独立原文核验。")
    return tuple(notes)


def _document_lines(parsed: ParsedTextPdf) -> list[_Line]:
    lines: list[_Line] = []
    for page in parsed.pages:
        for block in page.blocks:
            pieces = block.text.splitlines() or [""]
            for piece in pieces:
                lines.append(_line_from(page.page_number, block, piece))
    return lines


def _line_from(page_number: int, block: PdfTextBlock, text: str) -> _Line:
    return _Line(
        page_number=page_number,
        block_index=block.block_index,
        text=text,
        x0=block.bbox.x0,
        y0=block.bbox.y0,
        x1=block.bbox.x1,
        y1=block.bbox.y1,
        block_text=block.text,
    )


def _is_title(text: str, title: str) -> bool:
    compact = _compact(text)
    if compact == title:
        return True
    if compact.endswith(title) and len(compact) - len(title) <= 6:
        prefix = compact[: -len(title)]
        return bool(re.fullmatch(r"[一二三四五六七八九十百千]+、", prefix))
    return False


def _statement_title(text: str) -> str | None:
    for title in _STATEMENT_TITLES:
        if _is_title(text, title):
            return title
    return None


def _is_later_section(text: str) -> bool:
    if _NOTE_SECTION_RE.match(text):
        return False
    if not _SECTION_RE.match(text):
        return False
    return _NON_RECURRING_TITLE not in _compact(text)


def _is_meta_line(text: str) -> bool:
    compact = _compact(text)
    if not compact or _AMOUNT_RE.search(text):
        return False
    if compact.startswith("单位:") or compact.startswith("单位：") or "单位：" in compact or "单位:" in compact:
        return True
    if "币种：" in compact or "币种:" in compact:
        return True
    if _YEAR_RE.search(compact):
        return True
    if re.fullmatch(r"\d{4}年1[—\-–]12月", compact):
        return True
    if "适用" in compact and "不适用" in compact and len(compact) <= 20:
        return True
    return False


def _is_furniture(text: str) -> bool:
    stripped = text.strip()
    if re.fullmatch(r"\d+\s*/\s*\d+", stripped):
        return True
    compact = _compact(stripped)
    return compact.endswith("年度报告") and "公司" in compact and len(compact) <= 80


def _is_amount_only(text: str) -> bool:
    return bool(_AMOUNT_RE.search(text)) and not re.search(r"[\u4e00-\u9fff]", text)


def _has_amount(row: list[_Line]) -> bool:
    return any(_AMOUNT_RE.search(line.text) for line in row)


def _unmatched_open(text: str) -> bool:
    return text.count("（") + text.count("(") > text.count("）") + text.count(")") or text.count("“") > text.count("”")


def _parse_amount(token: str) -> Decimal:
    text = token.strip().replace("，", "").replace(",", "").replace("－", "-").replace("−", "-")
    parentheses = (text.startswith("（") and text.endswith("）")) or (text.startswith("(") and text.endswith(")"))
    if parentheses:
        text = text[1:-1].strip()
    value = Decimal(text)
    if parentheses:
        if value.is_signed():
            raise ValueError("括号与负号同时出现。")
        value = -value
    return value


def _period_label(year: int, style: str) -> str:
    if style == "度":
        return f"{year}年度"
    return f"{year}年金额"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _issue(target: _Target, code: str, message: str) -> ExtractionIssue:
    return ExtractionIssue(
        code=code,
        message=message,
        indicator_name=target.indicator_name,
        table_name=target.table_title,
    )
