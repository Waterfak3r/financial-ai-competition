"""从原始 PDF 的“主要会计数据”表定位扣非归母净利润。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from finagent.ingestion.errors import PdfInputError
from finagent.schemas.financial_fact import decimal_to_str
from finagent.schemas.financial_fact_v2 import (
    FREQUENCY_ANNUAL,
    RESTATEMENT_UNKNOWN,
    ROLE_COMPARATIVE,
    ROLE_CURRENT,
    SCOPE_UNKNOWN,
    FinancialFactV2,
    SourceRegion,
    TableCellEvidence,
)
from finagent.schemas.text_pdf import COORDINATE_SYSTEM, ParsedTextPdf, PdfBBox

_METRIC_ID = "net_profit_parent_ex_nonrecurring"
_ROW_LABEL = "归属于上市公司股东的扣除非经常性损益的净利润"
_UNIT_MULTIPLIERS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}
_UNIT_RE = re.compile(r"单位[:：]\s*(亿元|万元|元)")
_CURRENCY_RE = re.compile(r"币种[:：]\s*(人民币|美元|港币|港元|欧元|日元|英镑|新加坡元)")
_YEAR_RE = re.compile(r"^(20\d{2})年$")
_AMOUNT_RE = re.compile(
    r"^[（(]?\s*[-＋+－−–]?\s*(?:\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+\.\d+)\s*[）)]?$"
)
_SECTION_ONE = {"(一)主要会计数据", "（一）主要会计数据"}
_SECTION_TWO = {"(二)主要财务指标", "（二）主要财务指标"}
_MAX_LABEL_LINES = 4
_MAX_LABEL_VERTICAL_GAP = 28.0
_PERCENT_COLUMN_MARGIN = 3.0


@dataclass(frozen=True, slots=True)
class KeyFinancialDataExtractionIssue:
    metric_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class KeyFinancialDataV2Extraction:
    facts: tuple[FinancialFactV2, ...]
    evidence: tuple[TableCellEvidence, ...]
    issues: tuple[KeyFinancialDataExtractionIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class _Line:
    page: int
    y0: float
    words: tuple

    @property
    def y1(self) -> float:
        return max((word[3] for word in self.words), default=self.y0)

    @property
    def text(self) -> str:
        return " ".join(str(word[4]) for word in self.words)


@dataclass(frozen=True, slots=True)
class _Context:
    section_title: _Line
    header: _Line
    header_centers: tuple[tuple[int, float], ...]
    percentage_left: float
    percentage_right: float
    unit: str
    multiplier: Decimal
    currency: str
    currency_confirmed: bool
    unit_line: _Line
    body: tuple[_Line, ...]


def extract_v2_key_financial_data_facts(
    parsed: ParsedTextPdf,
    *,
    source_pdf_path: Path,
    company_id: str,
    report_year: int,
) -> KeyFinancialDataV2Extraction:
    """读取原 PDF 的表头和文字坐标，提取报告年及比较年两条金额事实。

    源 PDF 必须与 ParsedTextPdf 的哈希相同。年份列通过原 PDF 字词坐标定位；
    百分比列被独立表头定位并排除，2022 年历史金额只用于验证列对齐，不输出为事实。
    """

    if not isinstance(parsed, ParsedTextPdf):
        raise TypeError("parsed 必须是 ParsedTextPdf。")
    if parsed.coordinate_system != COORDINATE_SYSTEM:
        raise PdfInputError("ParsedTextPdf 坐标系不受支持。")
    if not isinstance(company_id, str) or company_id.strip() == "" or company_id != company_id.strip():
        raise PdfInputError("company_id 必须是非空且首尾无空白的字符串。")
    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise PdfInputError("report_year 必须是 1900 到 2100 之间的整数。")

    pdf_path = Path(source_pdf_path)
    try:
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    except OSError:
        return _failed("source_pdf_unreadable", "无法读取原始 PDF。")
    if digest.lower() != parsed.source_sha256.lower():
        return _failed("source_sha256_mismatch", "传入的原始 PDF 哈希与 ParsedTextPdf 不一致。")

    try:
        import pymupdf

        document = pymupdf.open(pdf_path)
    except Exception:
        return _failed("source_pdf_unreadable", "原始文件无法作为 PDF 打开。")
    try:
        lines = _document_lines(document)
    finally:
        document.close()

    context, issue = _context(lines, report_year)
    if issue is not None:
        return _failed(*issue)
    assert context is not None

    label_limit = _label_column_limit(context)
    labels = _label_rows(context.body, label_limit)
    if len(labels) != 1:
        code = "row_not_found" if not labels else "ambiguous_row"
        return _failed(code, "未能唯一定位“归属于上市公司股东的扣除非经常性损益的净利润”行。")
    label_line = labels[0]
    label_words = label_line.words
    row_top = min(word[1] for word in label_words)
    row_bottom = max(word[3] for word in label_words)
    row_page = label_line.page
    row_lines = [line for line in context.body if line.page == row_page and row_top - 2 <= line.y1 and line.y0 <= row_bottom + 2]
    values = _row_amounts(row_lines, row_top, row_bottom, label_limit, context)
    if values is None:
        return _failed(
            "amount_alignment",
            "无法按 2024、2023、2022 年金额列分别定位金额；百分比列或缺失值不作为金额补位。",
        )

    if not context.currency_confirmed:
        currency = "未披露"
        currency_issue = _issue("currency_undisclosed", "单位行未明确披露币种；事实币种标为未披露，独立核验将弃权。")
        issues = (currency_issue,)
    else:
        currency = context.currency
        issues = ()

    facts: list[FinancialFactV2] = []
    evidences: list[TableCellEvidence] = []
    for year in (report_year, report_year - 1):
        raw_value, value_words = values[year]
        try:
            amount = _parse_amount(raw_value)
        except (InvalidOperation, ValueError):
            issues += (_issue("amount_unreadable", f"{year} 年金额无法按精确 Decimal 确认。"),)
            continue
        role = ROLE_CURRENT if year == report_year else ROLE_COMPARATIVE
        fact_id = f"{parsed.document_id}:{_METRIC_ID}:{year}"
        limitations = [
            "原文位于近三年主要会计数据表，不是合并利润表；报表 scope 保持 unknown。",
            "追溯调整状态未知，未推断两年可比。",
        ]
        if not context.currency_confirmed:
            limitations.append("原文未披露币种，独立核验应弃权。")
        fact = FinancialFactV2(
            fact_id=fact_id,
            metric_id=_METRIC_ID,
            company_id=company_id,
            label_raw=_ROW_LABEL,
            raw_value=raw_value,
            normalized_value=decimal_to_str(amount * context.multiplier),
            currency=currency,
            unit_multiplier=decimal_to_str(context.multiplier),
            unit=context.unit,
            report_year=report_year,
            period_start=date(year, 1, 1),
            period_end=date(year, 12, 31),
            period_type="duration",
            frequency=FREQUENCY_ANNUAL,
            statement_type="key_financial_data",
            scope=SCOPE_UNKNOWN,
            comparison_role=role,
            restatement_status=RESTATEMENT_UNKNOWN,
            source_document_id=parsed.document_id,
            source_sha256=digest,
            evidence_ids=(f"extract-{fact_id}",),
            extraction_method="raw_pdf_key_financial_data_row_aligned_to_year_header_words",
            limitations=tuple(limitations),
        )
        facts.append(fact)
        evidences.append(_evidence(fact, context, label_line, value_words, year))
    return KeyFinancialDataV2Extraction(tuple(facts), tuple(evidences), issues)


def _document_lines(document) -> list[_Line]:
    result: list[_Line] = []
    for page_index, page in enumerate(document):
        grouped: dict[int, list] = {}
        for word in page.get_text("words"):
            grouped.setdefault(round(word[1]), []).append(word)
        for y0 in sorted(grouped):
            words = tuple(sorted(grouped[y0], key=lambda item: item[0]))
            result.append(_Line(page_index + 1, float(y0), words))
    return result


def _context(lines: list[_Line], report_year: int) -> tuple[_Context | None, tuple[str, str] | None]:
    section_titles = [line for line in lines if _normalize_section(line.text) in _SECTION_ONE]
    if len(section_titles) != 1:
        code = "section_not_found" if not section_titles else "ambiguous_section"
        return None, (code, "未找到唯一的“（一）主要会计数据”小节。")
    section_title = section_titles[0]
    tail = lines[lines.index(section_title) + 1 :]
    section_end = next((index for index, line in enumerate(tail) if _normalize_section(line.text) in _SECTION_TWO), len(tail))
    section_lines = tail[:section_end]
    headers: list[tuple[_Line, tuple[tuple[int, float], ...]]] = []
    for line in section_lines:
        centers = _header_year_centers(line)
        if _compact(line.text).startswith("主要会计数据") and tuple(year for year, _ in centers) == (
            report_year,
            report_year - 1,
            report_year - 2,
        ):
            headers.append((line, centers))
    if len(headers) != 1:
        code = "year_header_not_found" if not headers else "ambiguous_year_header"
        return None, (code, "未能唯一确认按报告年、上一年及再上一年排列的三列年度表头。")
    header, centers = headers[0]
    percent_lines = [
        line
        for line in section_lines
        if abs(line.y0 - header.y0) <= 24
        and ("本期比上年同期" in _compact(line.text) or ("增减" in _compact(line.text) and "%" in _compact(line.text)))
    ]
    percent_text = _compact("".join(line.text for line in percent_lines))
    if "本期比上年同期" not in percent_text or "增减" not in percent_text or "%" not in percent_text or not percent_lines:
        return None, ("percentage_column_not_found", "表头未能独立确认夹在年度金额列之间的同比百分比列。")
    percentage_words = tuple(word for line in percent_lines for word in line.words)
    header_words = _header_year_words(header)
    percentage_left = header_words[report_year - 1][2]
    percentage_right = header_words[report_year - 2][0]
    visible_percent_left = min(word[0] for word in percentage_words)
    visible_percent_right = max(word[2] for word in percentage_words)
    if visible_percent_left < percentage_left or visible_percent_right > percentage_right:
        return None, ("percentage_column_alignment", "同比百分比表头未落在 2023 年与 2022 年金额列之间。")

    unit_lines = [line for line in section_lines if _compact(line.text).startswith("单位：")]
    parsed_units = [_parse_unit(line.text) for line in unit_lines]
    if not parsed_units or any(item is None for item in parsed_units):
        return None, ("unit_not_found", "表格单位缺失或无法识别。")
    valid_units = [item for item in parsed_units if item is not None]
    if any(item != valid_units[0] for item in valid_units[1:]):
        return None, ("unit_conflict", "主要会计数据表中的单位或币种披露相互矛盾。")
    unit, currency, multiplier = valid_units[0]
    unit_line = unit_lines[-1]
    return (
        _Context(
            section_title=section_title,
            header=header,
            header_centers=centers,
            percentage_left=percentage_left,
            percentage_right=percentage_right,
            unit=unit,
            multiplier=multiplier,
            currency=currency or "未披露",
            currency_confirmed=currency is not None,
            unit_line=unit_line,
            body=tuple(section_lines),
        ),
        None,
    )


def _header_year_centers(line: _Line) -> tuple[tuple[int, float], ...]:
    years = []
    for word in line.words:
        match = _YEAR_RE.fullmatch(str(word[4]))
        if match:
            years.append((int(match.group(1)), (word[0] + word[2]) / 2))
    return tuple(sorted(years, key=lambda item: item[1]))


def _header_year_words(line: _Line) -> dict[int, tuple]:
    result = {}
    for word in line.words:
        match = _YEAR_RE.fullmatch(str(word[4]))
        if match:
            result[int(match.group(1))] = word
    return result


def _label_rows(lines: tuple[_Line, ...], label_limit: float) -> list[_Line]:
    candidates = []
    for start, line in enumerate(lines):
        if line.text.startswith("主要会计数据"):
            continue
        words = _left_column_words(line, label_limit)
        fragment = _compact("".join(str(word[4]) for word in words))
        if not fragment or not _ROW_LABEL.startswith(fragment):
            continue
        collected: list = []
        joined = ""
        previous_y = line.y0
        for candidate in lines[start : start + _MAX_LABEL_LINES]:
            if candidate.page != line.page or candidate.y0 - previous_y > _MAX_LABEL_VERTICAL_GAP:
                break
            previous_y = candidate.y0
            part_words = _left_column_words(candidate, label_limit)
            part = _compact("".join(str(word[4]) for word in part_words))
            if not part:
                continue
            if not _ROW_LABEL.startswith(joined + part):
                break
            joined += part
            collected.extend(part_words)
            if joined == _ROW_LABEL:
                candidates.append(
                    _Line(
                        page=line.page,
                        y0=min(word[1] for word in collected),
                        words=tuple(sorted(collected, key=lambda word: (word[1], word[0]))),
                    )
                )
                break
    unique = {}
    for candidate in candidates:
        unique[(candidate.page, candidate.y0)] = candidate
    return list(unique.values())


def _left_column_words(line: _Line, right_limit: float) -> tuple:
    return tuple(word for word in line.words if word[2] <= right_limit)


def _label_column_limit(context: _Context) -> float:
    first_header_cell = next(word for word in context.header.words if _compact(str(word[4])) == "主要会计数据")
    first_year_center = context.header_centers[0][1]
    return (first_header_cell[2] + first_year_center) / 2


def _row_amounts(
    lines: list[_Line],
    row_top: float,
    row_bottom: float,
    label_limit: float,
    context: _Context,
) -> dict[int, tuple[str, tuple]] | None:
    tokens: list[tuple[str, tuple]] = []
    for line in lines:
        for word in line.words:
            center_y = (word[1] + word[3]) / 2
            center_x = (word[0] + word[2]) / 2
            if not (row_top - 2 <= center_y <= row_bottom + 2) or center_x <= label_limit:
                continue
            if context.percentage_left - _PERCENT_COLUMN_MARGIN <= center_x <= context.percentage_right + _PERCENT_COLUMN_MARGIN:
                continue
            raw = str(word[4]).strip()
            if _AMOUNT_RE.fullmatch(raw):
                tokens.append((raw, (word,)))
    return _align_values(tokens, context.header_centers)


def _align_values(tokens: list[tuple[str, tuple]], centers: tuple[tuple[int, float], ...]) -> dict[int, tuple[str, tuple]] | None:
    aligned: dict[int, list[tuple[str, tuple]]] = {year: [] for year, _ in centers}
    center_map = dict(centers)
    for token in tokens:
        words = token[1]
        x_center = (min(word[0] for word in words) + max(word[2] for word in words)) / 2
        year = min(center_map, key=lambda item: abs(center_map[item] - x_center))
        if abs(center_map[year] - x_center) > 58:
            return None
        aligned[year].append(token)
    if any(len(values) != 1 for values in aligned.values()):
        return None
    return {year: values[0] for year, values in aligned.items()}


def _parse_unit(text: str) -> tuple[str, str | None, Decimal] | None:
    compact = _compact(text)
    unit_match = _UNIT_RE.search(compact)
    if unit_match is None:
        return None
    currencies = _CURRENCY_RE.findall(compact)
    if len(set(currencies)) > 1:
        return None
    unit = unit_match.group(1)
    return unit, currencies[0] if currencies else None, _UNIT_MULTIPLIERS[unit]


def _parse_amount(raw: str) -> Decimal:
    value = raw.strip().replace("，", "").replace(",", "").replace("－", "-").replace("−", "-").replace("–", "-")
    if value.startswith(("（", "(")) and value.endswith(("）", ")")):
        inner = value[1:-1].strip()
        parsed = Decimal(inner)
        if parsed.is_signed():
            raise ValueError("金额不能同时使用括号和负号。")
        return -parsed
    return Decimal(value)


def _evidence(
    fact: FinancialFactV2,
    context: _Context,
    label_line: _Line,
    value_words: tuple,
    year: int,
) -> TableCellEvidence:
    value_text = str(value_words[0][4])
    return TableCellEvidence(
        evidence_id=f"extract-{fact.fact_id}",
        document_id=fact.source_document_id,
        source_sha256=fact.source_sha256,
        pdf_page=label_line.page,
        printed_page=None,
        table_title="主要会计数据",
        row_label=_ROW_LABEL,
        column_label=f"{year}年",
        value_raw=value_text,
        value_normalized=fact.normalized_value,
        unit=context.unit,
        currency=context.currency,
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        period_type="duration",
        value_region=_word_region(value_words, label_line.page),
        row_region=_word_region(label_line.words, label_line.page),
        column_region=_word_region(tuple(word for word in context.header.words if _header_word_for_year(word, year)), context.header.page),
        title_region=_word_region(context.section_title.words, context.section_title.page),
        unit_region=_word_region(context.unit_line.words, context.unit_line.page),
        extraction_method="raw_pdf_key_financial_data_row_aligned_to_year_header_words",
        surrounding_text=None,
        limitations=(
            "报告年与比较年金额按原 PDF 年份表头的水平坐标定位；同比百分比列已按其表头区域排除。",
            "原文是主要会计数据表，不能据此证明合并利润表 scope；scope 保持 unknown。",
            "未知追溯调整状态不表示两年已证实可比。",
        ),
    )


def _header_word_for_year(word, year: int) -> bool:
    match = _YEAR_RE.fullmatch(str(word[4]))
    return match is not None and int(match.group(1)) == year


def _word_region(words: tuple, page: int) -> SourceRegion:
    text = " ".join(str(word[4]) for word in words)
    return SourceRegion(
        text=text,
        page=page,
        bbox=PdfBBox(
            x0=min(word[0] for word in words),
            y0=min(word[1] for word in words),
            x1=max(word[2] for word in words),
            y1=max(word[3] for word in words),
        ),
    )


def _normalize_section(text: str) -> str:
    return _compact(text).replace("（", "(").replace("）", ")")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace(":", "：")


def _issue(code: str, message: str) -> KeyFinancialDataExtractionIssue:
    return KeyFinancialDataExtractionIssue(metric_id=_METRIC_ID, code=code, message=message)


def _failed(code: str, message: str) -> KeyFinancialDataV2Extraction:
    return KeyFinancialDataV2Extraction((), (), (_issue(code, message),))
