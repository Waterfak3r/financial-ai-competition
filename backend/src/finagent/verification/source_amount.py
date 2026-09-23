"""从原始 PDF 的引用坐标复核金额，并独立复算单位换算。

不读取解析 JSON，也不使用 FactHit.text 作为证据。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from finagent.schemas.financial_fact import FinancialFact

_SCOPE_NOTE = (
    "仅复核原始金额是否出现在被引用的页面坐标内，以及 raw_value 乘 unit_multiplier "
    "是否等于 normalized_value。尚未独立确认年度列、表头口径或完整财报事实。"
)


def verify_source_amounts(pdf_path: Path, facts: tuple[FinancialFact, ...] | list[FinancialFact]) -> dict:
    """逐条核验。金额必须落在该事实自己的引用区域内。"""

    if not facts:
        return _report("abstained", [])
    results = []
    document = _open_pdf(pdf_path)
    try:
        for fact in facts:
            if document is None:
                results.append(_result(fact, "abstained", False, None, "无法读取原始 PDF。", [], None))
                continue
            results.append(_verify_fact(document, fact))
    finally:
        if document is not None:
            document.close()
    failed = sum(item["status"] == "failed" for item in results)
    abstained = sum(item["status"] == "abstained" for item in results)
    if failed:
        status = "failed"
    elif abstained or not results:
        status = "abstained"
    else:
        status = "passed"
    return _report(status, results)


def _verify_fact(document, fact: FinancialFact) -> dict:
    calculation_ok, calculation_reason = _calculation_ok(fact)
    if not fact.hits:
        return _result(fact, "abstained", False, calculation_ok, "事实没有引用坐标，无法定位原文金额。", [], None)
    evidence = []
    readable = []
    for hit in fact.hits:
        clip_text, problem = _clip_text(document, hit)
        evidence.append(
            {
                "page_number": hit.page_number,
                "block_index": hit.block_index,
                "x0": hit.x0,
                "y0": hit.y0,
                "x1": hit.x1,
                "y1": hit.y1,
                "extracted_text": "" if clip_text is None else clip_text,
                "amount_in_this_clip": _find_amount(clip_text, fact.raw_value) is not None if clip_text is not None else False,
                "problem": problem,
            }
        )
        if clip_text is not None:
            readable.append(clip_text)
    if len(readable) != len(fact.hits):
        return _result(
            fact,
            "abstained",
            False,
            calculation_ok,
            "存在无法读取或无效的引用坐标，不能因其他引用块含有金额而通过。",
            evidence,
            None,
        )
    amount_match = _find_amount("\n".join(readable), fact.raw_value)
    located = amount_match is not None
    if not located:
        return _result(fact, "failed", False, calculation_ok, "原始金额不在引用的页面坐标内。", evidence, None)
    if calculation_ok is False:
        return _result(fact, "failed", True, False, calculation_reason, evidence, amount_match)
    if calculation_ok is None:
        return _result(fact, "abstained", True, None, calculation_reason, evidence, amount_match)
    return _result(fact, "passed", True, True, None, evidence, amount_match)


def _clip_text(document, hit) -> tuple[str | None, str | None]:
    if hit.page_number < 1 or hit.page_number > document.page_count:
        return None, "页码超出原始 PDF。"
    if hit.x1 <= hit.x0 or hit.y1 <= hit.y0:
        return None, "引用坐标不是有效区域。"
    page = document[hit.page_number - 1]
    try:
        import pymupdf

        rect = pymupdf.Rect(hit.x0, hit.y0, hit.x1, hit.y1)
        text = page.get_text("text", clip=rect) or ""
    except Exception:
        return None, "无法按引用坐标读取原始 PDF。"
    return text, None


def _open_pdf(pdf_path: Path):
    try:
        import pymupdf

        return pymupdf.open(pdf_path)
    except Exception:
        return None


_BEFORE_CONTINUES_AMOUNT = set("0123456789,，.．-－−(（")
_AFTER_CONTINUES_AMOUNT = set("0123456789,，.．)）")
# 同一行的空格或制表符可以连着这些符号；单独的数字跨空白视为另一列，换行则停止。
_PREFIX_ACROSS_SPACE = set(",，.．-－−(（")
_SUFFIX_ACROSS_SPACE = set(",，.．)）")


def _find_amount(text: str, raw_value: str) -> dict[str, str] | None:
    """匹配完整金额 token。

    token 内部允许空白和换行。同一行的空格或制表符会连着负号、括号和千分位判断边界；
    换行则断开，避免把上一列的数字当成当前金额的前缀。
    """

    compact_raw = _compact(raw_value)
    if compact_raw == "":
        return None
    body = r"\s*".join(re.escape(char) for char in compact_raw)
    for match in re.finditer(body, text):
        if _boundary_before(text, match.start()) and _boundary_after(text, match.end()):
            start = max(0, match.start() - 12)
            end = min(len(text), match.end() + 12)
            return {"matched_text": match.group(0), "context": text[start:end]}
    return None


def _boundary_before(text: str, index: int) -> bool:
    if index <= 0:
        return True
    previous = text[index - 1]
    if previous in "\n\r":
        return True
    if previous not in " \t":
        return previous not in _BEFORE_CONTINUES_AMOUNT
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    if cursor < 0 or text[cursor] in "\n\r":
        return True
    return text[cursor] not in _PREFIX_ACROSS_SPACE


def _boundary_after(text: str, index: int) -> bool:
    if index >= len(text):
        return True
    following = text[index]
    if following in "\n\r":
        return True
    if following not in " \t":
        return following not in _AFTER_CONTINUES_AMOUNT
    cursor = index
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    if cursor >= len(text) or text[cursor] in "\n\r":
        return True
    return text[cursor] not in _SUFFIX_ACROSS_SPACE


def _calculation_ok(fact: FinancialFact) -> tuple[bool | None, str | None]:
    amount = _parse_amount(fact.raw_value)
    if amount is None:
        return None, "原始金额无法按十进制解释。"
    try:
        multiplier = Decimal(fact.unit_multiplier)
        normalized = Decimal(fact.normalized_value)
    except InvalidOperation:
        return None, "单位倍率或规范数值无法按十进制解释。"
    if amount * multiplier != normalized:
        return False, "raw_value 乘 unit_multiplier 与 normalized_value 不一致。"
    return True, None


def _parse_amount(token: str) -> Decimal | None:
    text = token.strip().replace("，", "").replace(",", "").replace("－", "-").replace("−", "-")
    text = _compact(text)
    parentheses = (text.startswith("（") and text.endswith("）")) or (text.startswith("(") and text.endswith(")"))
    if parentheses:
        text = text[1:-1]
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if parentheses:
        if value.is_signed():
            return None
        value = -value
    return value


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _report(status: str, results: list) -> dict:
    return {
        "kind": "pdf_clip_amount_and_normalization",
        "scope_note": _SCOPE_NOTE,
        "status": status,
        "passed_count": sum(item["status"] == "passed" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "abstained_count": sum(item["status"] == "abstained" for item in results),
        "results": results,
    }


def _result(
    fact: FinancialFact,
    status: str,
    located: bool,
    calculation_ok: bool | None,
    reason: str | None,
    evidence: list,
    amount_match: dict[str, str] | None,
) -> dict:
    return {
        "indicator_name": fact.indicator_name,
        "report_year": fact.report_year,
        "column_role": fact.column_role,
        "status": status,
        "amount_located": located,
        "calculation_ok": calculation_ok,
        "reason": reason,
        "amount_match": amount_match,
        "evidence": evidence,
    }
