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


@dataclass(frozen=True, slots=True)
class _Amount:
    raw: str
    words: tuple

    @property
    def center(self) -> float:
        return (min(word[0] for word in self.words) + max(word[2] for word in self.words)) / 2


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
    titles = [line for line in lines if _canonical_title(line.text) == _canonical_title(title_name)]
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
        return _finish(fact, INSUFFICIENT, ("unit",), ("单位无法从原文独立确认。",), (), None)
    unit_name, currency_name, multiplier = parsed_unit
    if currency_name is None:
        reason = "原文表头只披露单位，未明确币种；不能按人民币或事实中的其他币种推断。"
        if fact.currency != _UNDISCLOSED_CURRENCY:
            reason += " 事实币种字段也未标记为“未披露”。"
        return _finish(fact, INSUFFICIENT, ("unit", "currency"), (reason,), (), None)
    if not _same_unit(fact, unit_name, currency_name, multiplier):
        return _finish(fact, CONFLICT, ("unit",), ("单位或币种与原文相反。",), (), None)
    period = _parse_period(header, window, fact.period_end.year)
    if period is None:
        return _finish(fact, INSUFFICIENT, ("period_end",), ("无法从表头独立确认期间。",), (), None)
    period_type, period_start, period_end = period
    if fact.period_type != period_type or fact.period_start != period_start or fact.period_end != period_end:
        return _finish(fact, CONFLICT, ("period_end",), ("事实期间与表头原文不一致。",), (), None)
    rows = _match_rows(window, row_name)
    if len(rows) == 0:
        return _finish(fact, INSUFFICIENT, ("row",), ("找不到对应行标签。",), (), None)
    if len(rows) > 1:
        return _finish(fact, INSUFFICIENT, ("row",), ("同一表内有重复行，不能确定金额。",), (), None)
    row, amounts = rows[0]
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
    return _finish(
        fact,
        VERIFIED,
        ("source_sha256", "table_title", "row", "column", "unit", "value", "period_end", "scope"),
        (),
        (
            "未判断追溯调整，不把 restatement_status 当成已知。",
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


def _is_report_furniture(text: str) -> bool:
    compact = _compact(text)
    return "公司" in compact and compact.endswith(("年度报告", "年度报告全文"))


def _unit_line(lines: list[_Line], title: _Line) -> _Line | None:
    hits = [
        line
        for line in lines
        if line.page >= title.page and re.search(r"单位\s*[:：]", line.text)
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
    prefix_match = re.match(r"^(?:\d+[.．、]|[（(][一二三四五六七八九十]+[）)])", text)
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
        table_title=title.text,
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
