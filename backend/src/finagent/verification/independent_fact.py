"""独立重读原始 PDF，核验一条 FinancialFactV2。

不使用 fact.evidence_ids，也不沿用提取阶段给出的坐标。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
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

_NUMBER_BODY = r"(?:\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+\.\d+)"
_MAGNITUDE = re.compile(rf"^{_NUMBER_BODY}$")
_SIGNS = {"+", "＋", "-", "－", "−", "–"}
_OPEN_PARENS = {"(", "（"}
_CLOSE_PARENS = {")", "）"}
_PUNCTUATION_GAP = 8.0
_ROW_FRAGMENT_DISTANCE = 12.0
_UNDISCLOSED_CURRENCY = "未披露"
_KEY_DATA_YEAR_RE = re.compile(r"^(20\d{2})年$")
_METRICS = {
    "revenue": ("合并利润表", "其中：营业收入", "consolidated"),
    "net_profit_parent": ("合并利润表", "归属于母公司股东的净利润", "consolidated"),
    "net_profit_consolidated": ("合并利润表", "净利润", "consolidated"),
    "cost_of_goods_sold": ("合并利润表", "其中：营业成本", "consolidated"),
    "operating_cash_flow": ("合并现金流量表", "经营活动产生的现金流量净额", "consolidated"),
    "non_recurring_total": ("非经常性损益项目和金额", "合计", "unknown"),
    "accounts_receivable_net": ("合并资产负债表", "应收账款", "consolidated"),
    "inventory_net": ("合并资产负债表", "存货", "consolidated"),
    "net_profit_parent_ex_nonrecurring": (
        "主要会计数据",
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "unknown",
    ),
}
_INSTANT_METRICS = {"accounts_receivable_net", "inventory_net"}
_M3_INCOME_METRICS = {"net_profit_consolidated", "cost_of_goods_sold"}
_M3_KEY_FINANCIAL_DATA_METRICS = {"net_profit_parent_ex_nonrecurring"}


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


@dataclass(frozen=True, slots=True)
class _Amount:
    raw: str
    words: tuple

    @property
    def center(self) -> float:
        return (min(word[0] for word in self.words) + max(word[2] for word in self.words)) / 2


@dataclass(frozen=True, slots=True)
class _KeyFinancialDataContext:
    section_title: _Line
    window: tuple[_Line, ...]
    header: _Line
    header_centers: tuple[tuple[int, float], ...]
    percentage_left: float
    percentage_right: float
    unit_line: _Line


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
    except (InvalidOperation, ValueError):
        return _finish(fact, INSUFFICIENT, ("pdf",), ("无法作为 PDF 打开。",), (), None)
    try:
        lines = _lines(document)
    finally:
        document.close()
    key_data_context = None
    if fact.metric_id in _M3_KEY_FINANCIAL_DATA_METRICS:
        if fact.report_year is None:
            return _finish(fact, INSUFFICIENT, ("report_year",), ("缺少报告年度，无法确认当前年和比较年。",), (), None)
        key_data_context = _key_financial_data_context(lines, fact.report_year)
        if key_data_context is None:
            return _finish(
                fact,
                INSUFFICIENT,
                ("table_title", "column"),
                ("主要会计数据小节、三年表头或同比百分比列缺失、重复或位置不一致。",),
                (),
                None,
            )
        title = key_data_context.section_title
        window = list(key_data_context.window)
    else:
        titles = [line for line in lines if _canonical_title(line.text) == _canonical_title(title_name)]
        if len(titles) != 1:
            return _finish(fact, INSUFFICIENT, ("table_title",), ("表题缺失或重复，不能确定表格。",), (), None)
        title = titles[0]
        window = _window(lines, title, title_name)
    if fact.scope != scope:
        return _finish(fact, CONFLICT, ("scope",), (f"表题对应口径是 {scope}，与事实不一致。",), (), None)
    instant_metric = fact.metric_id in _INSTANT_METRICS
    if instant_metric and fact.statement_type != "balance_sheet":
        return _finish(fact, CONFLICT, ("statement_type",), ("事实报表类型与资产负债表原文不一致。",), (), None)
    if fact.metric_id in _M3_INCOME_METRICS and fact.statement_type != "income_statement":
        return _finish(fact, CONFLICT, ("statement_type",), ("事实报表类型与合并利润表原文不一致。",), (), None)
    if fact.metric_id in _M3_KEY_FINANCIAL_DATA_METRICS:
        if fact.statement_type != "key_financial_data":
            return _finish(fact, CONFLICT, ("statement_type",), ("原文来自主要会计数据表，不能标为合并利润表。",), (), None)
        if fact.label_raw != row_name:
            return _finish(fact, CONFLICT, ("label_raw",), ("事实行标签与主要会计数据表的原文披露不一致。",), (), None)
        if fact.frequency != "annual":
            return _finish(fact, CONFLICT, ("frequency",), ("事实频率不是年度。",), (), None)
        if fact.restatement_status != "unknown":
            return _finish(fact, CONFLICT, ("restatement_status",), ("原文未独立证明追溯调整状态；事实必须保留 unknown。",), (), None)
    header = (
        key_data_context.header
        if key_data_context is not None
        else _header(window, fact.period_end.year, instant=instant_metric)
    )
    if header is None:
        return _finish(fact, INSUFFICIENT, ("column",), ("缺少对应年度列表头。",), (), None)
    unit_line = key_data_context.unit_line if key_data_context is not None else _unit_line(window, title)
    if unit_line is None:
        return _finish(fact, INSUFFICIENT, ("unit",), ("缺少单位或币种。",), (), None)
    parsed_unit = _parse_unit(unit_line.text)
    if parsed_unit is None:
        return _finish(fact, INSUFFICIENT, ("unit",), ("单位无法从原文独立确认。",), (), None)
    unit_name, currency_name, multiplier = parsed_unit
    if currency_name is None:
        reason = "原文表头只披露单位，未明确币种；不能按人民币或事实中的其他币种推断。"
        if fact.currency != _UNDISCLOSED_CURRENCY:
            reason += " 事实币种字段也未标记为“未披露”。"
        return _finish(fact, INSUFFICIENT, ("unit", "currency"), (reason,), (), None)
    if not _same_unit(fact, unit_name, currency_name, multiplier):
        return _finish(fact, CONFLICT, ("unit",), ("单位或币种与原文相反。",), (), None)
    period = (
        _key_financial_data_period(key_data_context, fact.period_end.year)
        if key_data_context is not None
        else (
            _parse_instant_period(header, fact.period_end.year)
            if instant_metric
            else _parse_period(header, window, fact.period_end.year)
        )
    )
    if period is None:
        return _finish(fact, INSUFFICIENT, ("period_end",), ("无法从表头独立确认期间。",), (), None)
    period_type, period_start, period_end = period
    if fact.period_type != period_type or fact.period_start != period_start or fact.period_end != period_end:
        return _finish(fact, CONFLICT, ("period_end",), ("事实期间与表头原文不一致。",), (), None)
    if instant_metric:
        if fact.report_year is None:
            return _finish(fact, INSUFFICIENT, ("comparison_role",), ("缺少报告年份，无法独立确认本期或比较期角色。",), (), None)
        header_years = tuple(
            int(item[0])
            for item in re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", _compact(header.text))
        )
        if header_years != (fact.report_year, fact.report_year - 1):
            return _finish(fact, CONFLICT, ("report_year", "column"), ("年末列未按报告年度及上一年度排列。",), (), None)
        if period_end.year == header_years[0]:
            expected_role = "current"
        elif period_end.year == header_years[1]:
            expected_role = "comparative"
        else:
            return _finish(fact, CONFLICT, ("report_year",), ("年末列不属于报告年度或其上一年度。",), (), None)
        if fact.comparison_role != expected_role:
            return _finish(fact, CONFLICT, ("comparison_role",), ("事实的 current/comparative 角色与报告年度及年末列不一致。",), (), None)
    if fact.metric_id in _M3_KEY_FINANCIAL_DATA_METRICS:
        context_years = tuple(year for year, _center in key_data_context.header_centers)
        if context_years != (fact.report_year, fact.report_year - 1, fact.report_year - 2):
            return _finish(fact, CONFLICT, ("report_year", "column"), ("表头年度列与报告年、比较年和历史年不一致。",), (), None)
        expected_role = "current" if fact.period_end.year == fact.report_year else "comparative"
        if fact.period_end.year not in context_years[:2] or fact.comparison_role != expected_role:
            return _finish(fact, CONFLICT, ("comparison_role",), ("事实期间或 current/comparative 角色与年度表头不一致。",), (), None)
    if fact.metric_id in _M3_INCOME_METRICS:
        if fact.report_year is None:
            return _finish(fact, INSUFFICIENT, ("report_year",), ("缺少报告年度，无法确认 current/comparative 角色。",), (), None)
        annual_years = tuple(
            int(item[0])
            for item in re.findall(r"(\d{4})年(度|金额)", _compact(header.text))
        )
        if annual_years != (fact.report_year, fact.report_year - 1):
            return _finish(fact, CONFLICT, ("report_year", "column"), ("年度列未按报告年、比较年顺序排列。",), (), None)
        expected_role = "current" if fact.period_end.year == fact.report_year else "comparative"
        if fact.period_end.year not in annual_years or fact.comparison_role != expected_role:
            return _finish(fact, CONFLICT, ("comparison_role",), ("事实的期间或比较角色与年度列不一致。",), (), None)
        if fact.frequency != "annual":
            return _finish(fact, CONFLICT, ("frequency",), ("事实频率不是年度。",), (), None)
        if fact.restatement_status != "unknown":
            return _finish(fact, CONFLICT, ("restatement_status",), ("原文未独立证明追溯调整状态；事实必须保留 unknown。",), (), None)
    rows = _match_rows(window, row_name)
    if len(rows) == 0:
        return _finish(fact, INSUFFICIENT, ("row",), ("找不到对应行标签。",), (), None)
    if len(rows) > 1:
        return _finish(fact, INSUFFICIENT, ("row",), ("同一表内有重复行，不能确定金额。",), (), None)
    row, amounts = rows[0]
    if key_data_context is not None:
        aligned = _amount_for_key_financial_data(amounts, key_data_context)
        chosen = None if aligned is None else aligned.get(fact.period_end.year, [None])[0]
    else:
        chosen = _amount_for_header(amounts, header, fact.period_end.year)
    other = [item for item in amounts if item is not chosen]
    if chosen is None:
        return _finish(fact, INSUFFICIENT, ("value",), ("年度列下没有可定位的金额。",), (), None)
    if not _same_amount(chosen, fact.raw_value):
        if any(_same_amount(item, fact.raw_value) for item in other):
            reason = "金额出现在其他年度列，而不是 period_end 对应列。"
        else:
            reason = "定位到的金额与事实不一致。"
        return _finish(fact, CONFLICT, ("value",), (reason,), (), _evidence(fact, title, row, chosen, header, unit_line, parsed_unit, period))
    normalized = _number(chosen.raw) * multiplier
    if Decimal(fact.normalized_value) != normalized:
        return _finish(fact, CONFLICT, ("value",), ("规范值与原文金额乘独立单位倍率不一致。",), (), _evidence(fact, title, row, chosen, header, unit_line, parsed_unit, period))
    evidence = _evidence(fact, title, row, chosen, header, unit_line, parsed_unit, period)
    checks = ("source_sha256", "table_title", "row", "column", "unit", "value", "period_end", "scope")
    if instant_metric:
        checks += ("statement_type", "report_year", "comparison_role")
    if fact.metric_id in _M3_INCOME_METRICS:
        checks += ("statement_type", "report_year", "comparison_role", "frequency", "restatement_status")
    if fact.metric_id in _M3_KEY_FINANCIAL_DATA_METRICS:
        checks += (
            "statement_type",
            "report_year",
            "comparison_role",
            "frequency",
            "restatement_status",
            "scope_unknown_preserved",
            "percentage_column_excluded",
        )
    return _finish(
        fact,
        VERIFIED,
        checks,
        (),
        (
            "未判断追溯调整，不把 restatement_status 当成已知。",
            *(("披露来自主要会计数据表；已核对该披露行，但未由合并利润表证明报表 scope，scope 保持 unknown。",) if fact.metric_id in _M3_KEY_FINANCIAL_DATA_METRICS else ()),
            *(("事实未提供单位；已依据 PDF 单位和 unit_multiplier 核验。",) if fact.unit is None else ()),
        ),
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
    other_statement_titles = (
        "合并资产负债表",
        "母公司资产负债表",
        "合并利润表",
        "母公司利润表",
        "合并现金流量表",
        "母公司现金流量表",
        "合并所有者权益变动表",
        "母公司所有者权益变动表",
    )
    for line in lines[start : start + 80]:
        if chosen and line.page > title.page + 1:
            break
        if title_name == "非经常性损益项目和金额" and "采用公允价值计量" in line.text:
            break
        if chosen and line is not title and any(
            name in line.text for name in other_statement_titles if name != title_name
        ):
            break
        chosen.append(line)
    return chosen


def _key_financial_data_context(lines: list[_Line], report_year: int) -> _KeyFinancialDataContext | None:
    """Locate the key financial data subsection and its independently mapped columns."""

    section_titles = [line for line in lines if _normalize_key_data_section(line.text) == "(一)主要会计数据"]
    if len(section_titles) != 1:
        return None
    section_title = section_titles[0]
    tail = lines[lines.index(section_title) + 1 :]
    end = next(
        (index for index, line in enumerate(tail) if _normalize_key_data_section(line.text) == "(二)主要财务指标"),
        len(tail),
    )
    window = tuple(tail[:end])
    headers: list[tuple[_Line, tuple[tuple[int, float], ...]]] = []
    for line in window:
        centers = _key_data_header_centers(line)
        if _compact(line.text).startswith("主要会计数据") and tuple(year for year, _ in centers) == (
            report_year,
            report_year - 1,
            report_year - 2,
        ):
            headers.append((line, centers))
    if len(headers) != 1:
        return None
    header, centers = headers[0]

    percentage_lines = [
        line
        for line in window
        if line.page == header.page
        and abs(line.y0 - header.y0) <= 24
        and ("本期比上年同期" in _compact(line.text) or ("增减" in _compact(line.text) and "%" in _compact(line.text)))
    ]
    percentage_text = _compact("".join(line.text for line in percentage_lines))
    if (
        not percentage_lines
        or "本期比上年同期" not in percentage_text
        or "增减" not in percentage_text
        or "%" not in percentage_text
    ):
        return None
    percentage_words = tuple(word for line in percentage_lines for word in line.words)
    header_words = _key_data_header_words(header)
    percentage_left = header_words[report_year - 1][2]
    percentage_right = header_words[report_year - 2][0]
    visible_percent_left = min(word[0] for word in percentage_words)
    visible_percent_right = max(word[2] for word in percentage_words)
    if visible_percent_left < percentage_left or visible_percent_right > percentage_right:
        return None

    unit_lines = [line for line in window if re.search(r"^单位\s*[:：]", _compact(line.text))]
    parsed_units = [_parse_unit(line.text) for line in unit_lines]
    if not parsed_units or parsed_units[0] is None or any(unit != parsed_units[0] for unit in parsed_units[1:]):
        return None
    return _KeyFinancialDataContext(
        section_title=section_title,
        window=window,
        header=header,
        header_centers=centers,
        percentage_left=percentage_left,
        percentage_right=percentage_right,
        unit_line=unit_lines[-1],
    )


def _normalize_key_data_section(text: str) -> str:
    return _compact(text).replace("（", "(").replace("）", ")")


def _key_data_header_centers(line: _Line) -> tuple[tuple[int, float], ...]:
    values = []
    for word in line.words:
        match = _KEY_DATA_YEAR_RE.fullmatch(word[4])
        if match:
            values.append((int(match.group(1)), (word[0] + word[2]) / 2))
    return tuple(sorted(values, key=lambda item: item[1]))


def _key_data_header_words(line: _Line) -> dict[int, tuple]:
    result = {}
    for word in line.words:
        match = _KEY_DATA_YEAR_RE.fullmatch(word[4])
        if match:
            result[int(match.group(1))] = word
    return result


def _key_financial_data_period(context: _KeyFinancialDataContext, year: int):
    years = tuple(item[0] for item in context.header_centers)
    if year not in years[:2]:
        return None
    return "duration", date(year, 1, 1), date(year, 12, 31)


def _header(lines: list[_Line], year: int, *, instant: bool = False) -> _Line | None:
    if instant:
        return _instant_header(lines, year)
    token = str(year)
    hits = [
        line
        for line in lines
        if not _is_report_furniture(line.text)
        and token in line.text
        and ("年度" in line.text or "年金额" in line.text)
    ]
    if not hits:
        return None
    signatures = [
        tuple(re.findall(r"(\d{4})年(度|金额)", _compact(line.text)))
        for line in hits
    ]
    # A statement continued on the next page may repeat the same annual
    # columns. Accept that only when every repeated header declares the same
    # ordered years and styles, then use the last local header for coordinates.
    if not signatures[0] or any(signature != signatures[0] for signature in signatures[1:]):
        return None
    return hits[-1]


def _instant_header(lines: list[_Line], year: int) -> _Line | None:
    """Find a two-column 12/31 header, not the statement title's lone date."""

    hits: list[tuple[_Line, tuple[tuple[str, str, str], ...]]] = []
    for line in lines:
        if _is_report_furniture(line.text):
            continue
        dates = tuple(re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", _compact(line.text)))
        years = tuple(item[0] for item in dates)
        if (
            len(dates) == 2
            and len(set(years)) == 2
            and str(year) in years
            and all(month == "12" and day == "31" for _, month, day in dates)
        ):
            hits.append((line, dates))
    if not hits or any(signature != hits[0][1] for _, signature in hits[1:]):
        return None
    return hits[-1][0]


def _is_report_furniture(text: str) -> bool:
    compact = _compact(text)
    return "公司" in compact and compact.endswith(("年度报告", "年度报告全文"))


def _unit_line(lines: list[_Line], title: _Line) -> _Line | None:
    hits = [
        line
        for line in lines
        if line.page >= title.page and re.search(r"^单位\s*[:：]", _compact(line.text))
    ]
    if not hits:
        return None
    signatures = [_parse_unit(line.text) for line in hits]
    if signatures[0] is None or any(signature != signatures[0] for signature in signatures[1:]):
        return None
    return hits[-1]


def _parse_unit(text: str) -> tuple[str, str | None, Decimal] | None:
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
        currency = None
    return unit, currency, multiplier


def _canonical_title(text: str) -> str:
    """Recognize numbered statement headings and the common 及/和 title variant."""

    compact = _compact(text).replace("非经常性损益项目及金额", "非经常性损益项目和金额")
    return re.sub(r"^(?:[一二三四五六七八九十百千万]+|\d+)[、．.]", "", compact)


def _same_unit(fact: FinancialFactV2, unit: str, currency: str, multiplier: Decimal) -> bool:
    aliases = {"人民币": {"人民币", "CNY", "RMB"}, "美元": {"美元", "USD"}}
    unit_matches = fact.unit is None or fact.unit == unit
    return unit_matches and Decimal(fact.unit_multiplier) == multiplier and fact.currency in aliases[currency]


def _parse_period(header: _Line, window: list[_Line], year: int) -> tuple[str, object, object] | None:
    text = _compact(header.text)
    token = str(year)
    if f"{token}年度" not in text and f"{token}年金额" not in text:
        return None
    window_text = _compact("".join(line.text for line in window))
    if f"{token}年度" in text or f"{token}年金额" in text or f"{token}年1—12" in window_text or f"{token}年1-12" in window_text:
        return "duration", date(year, 1, 1), date(year, 12, 31)
    return None


def _parse_instant_period(header: _Line, year: int) -> tuple[str, object, object] | None:
    dates = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", _compact(header.text))
    matches = [item for item in dates if item[0] == str(year)]
    if len(matches) != 1 or matches[0][1:] != ("12", "31"):
        return None
    return "instant", None, date(year, 12, 31)


def _line_amounts(line: _Line) -> tuple[_Amount, ...]:
    return _amounts(line.words)


def _amount_parts(token: str) -> tuple[str, str, str, str] | None:
    """Split a word into optional accounting punctuation and its numeric body."""

    text = token
    opener = text[0] if text[:1] in _OPEN_PARENS else ""
    if opener:
        text = text[1:]
    closer = text[-1] if text[-1:] in _CLOSE_PARENS else ""
    if closer:
        text = text[:-1]
    sign = text[0] if text[:1] in _SIGNS else ""
    if sign:
        text = text[1:]
    if not _MAGNITUDE.fullmatch(text):
        return None
    return opener, sign, text, closer


def _amounts(words: tuple) -> tuple[_Amount, ...]:
    """Read monetary tokens, joining a nearby separately extracted sign or parenthesis."""

    found: list[_Amount] = []
    index = 0
    while index < len(words):
        word = words[index]
        token = word[4]
        parts = _amount_parts(token)
        if parts is None:
            index += 1
            continue

        start = index
        end = index
        opener, sign, magnitude, closer = parts
        previous = words[index - 1] if index > 0 else None
        previous_two = words[index - 2] if index > 1 else None
        following = words[index + 1] if index + 1 < len(words) else None

        if not sign and previous is not None and previous[4] in _SIGNS:
            if not _near(previous, word):
                index += 1
                continue
            sign = previous[4]
            start = index - 1
            if previous_two is not None and previous_two[4] in _OPEN_PARENS:
                if not _near(previous_two, previous):
                    index += 1
                    continue
                opener = previous_two[4]
                start = index - 2
        elif not opener and previous is not None and previous[4] in _OPEN_PARENS:
            if not _near(previous, word):
                index += 1
                continue
            opener = previous[4]
            start = index - 1

        if not closer and following is not None and following[4] in _CLOSE_PARENS and _near(word, following):
            closer = following[4]
            end = index + 1

        # Unbalanced parentheses can change the sign. Do not reinterpret the
        # bare digits as a positive amount when either half is nearby.
        nearby_open = previous is not None and previous[4] in _OPEN_PARENS
        nearby_close = following is not None and following[4] in _CLOSE_PARENS
        if bool(opener) != bool(closer) or (nearby_open and not opener) or (nearby_close and not closer):
            index += 1
            continue

        raw = f"{opener}{sign}{magnitude}{closer}" if opener else f"{sign}{magnitude}"
        found.append(_Amount(raw, tuple(words[start : end + 1])))
        index += 1
    return tuple(found)


def _near(left, right) -> bool:
    return -1.0 <= right[0] - left[2] <= _PUNCTUATION_GAP


def _label_words(line: _Line, amounts: tuple[_Amount, ...] | None = None) -> tuple:
    amounts = _line_amounts(line) if amounts is None else amounts
    if not amounts:
        return line.words
    left_edge = min(min(word[0] for word in amount.words) for amount in amounts)
    return tuple(word for word in line.words if word[2] <= left_edge + 1)


def _match_rows(lines: list[_Line], row_name: str) -> list[tuple[_Line, tuple[_Amount, ...]]]:
    found = []
    for index, line in enumerate(lines):
        amounts = _line_amounts(line)
        if not amounts:
            continue
        label = _row_label(lines, index, row_name, amounts)
        if label is not None:
            found.append((label, amounts))
    return found


def _row_label(lines: list[_Line], index: int, target: str, amounts: tuple[_Amount, ...]) -> _Line | None:
    """Return only label fragments that form the complete requested label."""

    current = lines[index]
    amount_left = min(min(word[0] for word in amount.words) for amount in amounts)
    candidates: list[tuple[int, tuple, str]] = []
    for candidate_index in range(max(0, index - 1), min(len(lines), index + 2)):
        candidate = lines[candidate_index]
        if candidate.page != current.page or abs(candidate.y0 - current.y0) > _ROW_FRAGMENT_DISTANCE:
            continue
        if candidate_index == index:
            words = _label_words(candidate, amounts)
        else:
            if _line_amounts(candidate):
                continue
            words = candidate.words
            if words and max(word[2] for word in words) >= amount_left - 1:
                continue
        fragment = _longest_label_fragment(words, target)
        if fragment is not None:
            candidates.append((candidate_index, fragment[0], fragment[1]))

    candidates.sort(key=lambda item: item[0])
    # At most the row's own line and its immediate neighbors are considered.
    # Exact concatenation prevents neighboring prose or labels from becoming
    # part of the row name.
    for mask in range(1, 1 << len(candidates)):
        selected = [item for position, item in enumerate(candidates) if mask & (1 << position)]
        selected.sort(key=lambda item: item[0])
        if "".join(item[2] for item in selected) != target:
            continue
        region_words = tuple(word for item in selected for word in item[1])
        first_line = lines[selected[0][0]]
        region_text = "".join(word[4] for word in region_words)
        return _Line(
            page=first_line.page,
            y0=min(word[1] for word in region_words),
            text=region_text,
            words=region_words,
        )
    return None


def _longest_label_fragment(words: tuple, target: str) -> tuple[tuple, str] | None:
    matches = []
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            selected = tuple(words[start:end])
            text = _label_fragment("".join(word[4] for word in selected), target)
            if text is not None:
                matches.append((selected, text))
    if not matches:
        return None
    return max(matches, key=lambda item: (len(item[1]), len(item[0])))


def _label_fragment(raw: str, target: str) -> str | None:
    text = _compact(raw)
    prefix_match = re.match(
        r"^(?:\d+[.．、]|[一二三四五六七八九十]+[、．.]|[（(][一二三四五六七八九十]+[）)])",
        text,
    )
    if prefix_match:
        text = text[prefix_match.end() :]
    if text and text in target:
        return text
    if text.startswith(target):
        suffix = text[len(target) :]
        if not suffix or suffix.startswith(("（净", "(净", "（亏损", "(亏损")):
            return target
    return None


def _amount_for_header(amounts: tuple, header: _Line, year: int):
    year_words = sorted(
        (word for word in header.words if word[4][:4].isdigit()),
        key=lambda word: word[0],
    )
    matches = [word for word in year_words if word[4].startswith(str(year))]
    if len(matches) != 1:
        return None
    year_word = matches[0]

    def nearest(amount):
        return min(year_words, key=lambda item: abs(amount.center - (item[0] + item[2]) / 2))

    chosen = [word for word in amounts if nearest(word) == year_word]
    if len(chosen) != 1:
        return None
    return chosen[0]


def _amount_for_key_financial_data(amounts: tuple, context: _KeyFinancialDataContext):
    """Align every amount to one of the three year headings after excluding the % column."""

    centers = dict(context.header_centers)
    aligned: dict[int, list[_Amount]] = {year: [] for year in centers}
    for amount in amounts:
        if context.percentage_left - 3 <= amount.center <= context.percentage_right + 3:
            continue
        year = min(centers, key=lambda item: abs(centers[item] - amount.center))
        if abs(centers[year] - amount.center) > 58:
            return None
        aligned[year].append(amount)
    if any(len(items) != 1 for items in aligned.values()):
        return None
    return aligned


def _same_amount(token: _Amount | str, raw_value: str) -> bool:
    value = token.raw if isinstance(token, _Amount) else token
    try:
        return _number(value) == _number(raw_value)
    except Exception:
        return False


def _number(token: str) -> Decimal:
    text = (
        token.replace(",", "")
        .replace("，", "")
        .replace("−", "-")
        .replace("－", "-")
        .replace("–", "-")
        .replace("＋", "+")
    )
    if text.startswith(("(", "（")) and text.endswith((")", "）")):
        inner = text[1:-1]
        value = Decimal(inner)
        return value if value < 0 else -value
    return Decimal(text)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace(":", "：")


def _region(line_or_word, page: int | None = None) -> SourceRegion:
    if isinstance(line_or_word, _Line):
        words = line_or_word.words
        text = line_or_word.text
        page_number = line_or_word.page
    elif isinstance(line_or_word, _Amount):
        words = line_or_word.words
        text = line_or_word.raw
        page_number = page or 0
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


def _evidence(fact, title, row, value_amount, header, unit_line, parsed_unit, period) -> TableCellEvidence | None:
    if title is None or parsed_unit is None or period is None or value_amount is None:
        return None
    unit_name, currency_name, multiplier = parsed_unit
    period_type, period_start, period_end = period
    value_region = _region(value_amount, row.page if row else title.page)
    return TableCellEvidence(
        evidence_id=f"ind-{fact.fact_id}",
        document_id=fact.source_document_id,
        source_sha256=fact.source_sha256,
        pdf_page=value_region.page,
        printed_page=None,
        table_title=("主要会计数据" if fact.metric_id in _M3_KEY_FINANCIAL_DATA_METRICS else title.text),
        row_label=None if row is None else row.text,
        column_label=None if header is None else header.text,
        value_raw=value_amount.raw,
        value_normalized=decimal_to_str(_number(value_amount.raw) * multiplier),
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
