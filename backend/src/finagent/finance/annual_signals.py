"""确定性年度候选异常筛查。

只根据已有事实、同比差额和原文金额复核结果给出候选或弃权。
不把缺失当作零，不确认舞弊，也不声称已独立验证完整财报事实。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
from typing import Mapping, Sequence

from finagent.finance.annual_change import AnnualChangeResult
from finagent.ingestion.errors import PdfInputError
from finagent.schemas.financial_fact import PERIOD_TYPE_ANNUAL, FinancialFact, decimal_to_str

REVENUE = "营业收入"
PARENT_PROFIT = "归属于母公司股东的净利润"
OPERATING_CASH = "经营活动产生的现金流量净额"
NON_RECURRING = "披露的非经常性损益合计"

_LIMITATION = (
    "这是候选线索或描述性计算，不是确认舞弊，也不是对年度列、表头口径或完整财报事实的独立验证。"
)
_NON_RECURRING_REASON = "非经常性损益来自披露表格口径，与合并报表口径不同，不计算其对归母净利润的比值。"


def screen_annual_signals(
    facts: Sequence[FinancialFact],
    changes: AnnualChangeResult,
    verification: Mapping[str, object] | None,
    report_year: int,
) -> dict:
    """筛查两条利润或收入与经营现金流背离线索，并计算现金流对归母净利润的比值。"""

    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise PdfInputError("report_year 必须是 1900 到 2100 之间的整数。")
    if not isinstance(changes, AnnualChangeResult):
        raise PdfInputError("screening 需要 calculate_annual_changes 的结果。")
    grouped = _group(facts)
    checks = _verification_index(verification)
    profit_cash = _divergence(
        "parent_profit_up_operating_cash_down",
        "归母净利润差额大于 0 且经营现金流差额小于 0",
        PARENT_PROFIT,
        OPERATING_CASH,
        grouped,
        changes,
        checks,
        report_year,
    )
    revenue_cash = _divergence(
        "revenue_up_operating_cash_down",
        "营业收入差额大于 0 且经营现金流差额小于 0",
        REVENUE,
        OPERATING_CASH,
        grouped,
        changes,
        checks,
        report_year,
    )
    current_ratio = _ratio(
        "operating_cash_to_parent_profit_current",
        "当期经营现金流 / 当期归母净利润",
        grouped,
        checks,
        report_year,
        report_year,
    )
    prior_ratio = _ratio(
        "operating_cash_to_parent_profit_prior",
        "上期经营现金流 / 上期归母净利润",
        grouped,
        checks,
        report_year - 1,
        report_year,
    )
    return {
        "kind": "deterministic_annual_candidate_screen",
        "role": "candidate_input_for_later_agent_or_report",
        "limitation": _LIMITATION,
        "items": [profit_cash, revenue_cash, current_ratio, prior_ratio, _non_recurring_item(grouped)],
    }


def _divergence(
    signal_id: str,
    title: str,
    left_name: str,
    right_name: str,
    grouped: dict,
    changes: AnnualChangeResult,
    checks: dict,
    report_year: int,
) -> dict:
    formula = f"{left_name}差额>0 且 {right_name}差额<0；差额=本期规范值-上期规范值"
    needed = (
        (left_name, report_year),
        (left_name, report_year - 1),
        (right_name, report_year),
        (right_name, report_year - 1),
    )
    ready, reason = _prepare(needed, grouped, checks, require_consolidated=True)
    if reason is not None:
        return _item(signal_id, title, "abstained", "inference", formula, ready, None, reason)
    left_diff, left_reason, left_note = _difference(left_name, grouped, changes, report_year)
    right_diff, right_reason, right_note = _difference(right_name, grouped, changes, report_year)
    if left_reason or right_reason:
        return _item(
            signal_id,
            title,
            "abstained",
            "inference",
            formula,
            ready,
            None,
            left_reason or right_reason,
        )
    assert left_diff is not None and right_diff is not None
    triggered = left_diff > 0 and right_diff < 0
    value = {
        "role": "calculation",
        "left_difference": decimal_to_str(left_diff),
        "right_difference": decimal_to_str(right_diff),
    }
    status = "candidate" if triggered else "not_triggered"
    kind = "inference" if triggered else "calculation"
    note = " ".join(part for part in (left_note, right_note) if part)
    return _item(signal_id, title, status, kind, formula, ready, value, None, note or None)


def _ratio(
    signal_id: str,
    title: str,
    grouped: dict,
    checks: dict,
    year: int,
    report_year: int,
) -> dict:
    formula = f"{year}年{OPERATING_CASH}规范值 / {year}年{PARENT_PROFIT}规范值"
    needed = ((OPERATING_CASH, year), (PARENT_PROFIT, year))
    ready, reason = _prepare(needed, grouped, checks, require_consolidated=True)
    if reason is not None:
        return _item(signal_id, title, "abstained", "calculation", formula, ready, None, reason)
    cash = _single(grouped[(OPERATING_CASH, year)])
    profit = _single(grouped[(PARENT_PROFIT, year)])
    assert cash is not None and profit is not None
    denominator = Decimal(profit.normalized_value)
    numerator = Decimal(cash.normalized_value)
    if denominator <= 0:
        return _item(
            signal_id,
            title,
            "abstained",
            "calculation",
            formula,
            ready,
            None,
            "分母归母净利润规范值小于或等于零，不计算现金流与利润的比值，避免把它读成现金转化。",
        )
    value = {
        "role": "calculation",
        "year": year,
        "report_year": report_year,
        "numerator": decimal_to_str(numerator),
        "denominator": decimal_to_str(denominator),
        "ratio": decimal_to_str(_divide(numerator, denominator)),
    }
    return _item(signal_id, title, "calculated", "calculation", formula, ready, value, None)


def _non_recurring_item(grouped: dict) -> dict:
    cites = []
    for key, facts in sorted(grouped.items()):
        if key[0] == NON_RECURRING and len(facts) == 1:
            cites.append(_cite(facts[0]))
    return _item(
        "non_recurring_to_parent_profit",
        "非经常性损益合计 / 归母净利润",
        "abstained",
        "inference",
        "不计算。披露表格口径与合并归母净利润口径不同。",
        cites,
        None,
        _NON_RECURRING_REASON,
    )


def _prepare(
    needed: tuple[tuple[str, int], ...],
    grouped: dict,
    checks: dict,
    *,
    require_consolidated: bool,
) -> tuple[list[dict], str | None]:
    selected: list[FinancialFact] = []
    for key in needed:
        facts = grouped.get(key, [])
        if not facts:
            return [], f"缺少{key[1]}年{key[0]}，缺失不当作零。"
        if len(facts) != 1:
            return [_cite(fact) for fact in facts], f"{key[1]}年{key[0]}存在重复或冲突事实。"
        selected.append(facts[0])
    reason = _compatible(selected, require_consolidated=require_consolidated)
    cites = [_cite(fact) for fact in selected]
    if reason is not None:
        return cites, reason
    for fact in selected:
        verdict = _verdict(checks, fact)
        if verdict != "passed":
            return cites, f"{fact.report_year}年{fact.indicator_name}的原文金额复核不是通过：{verdict}。"
    return cites, None


def _compatible(facts: list[FinancialFact], *, require_consolidated: bool) -> str | None:
    first = facts[0]
    for fact in facts[1:]:
        if fact.document_id != first.document_id or fact.source_sha256 != first.source_sha256:
            return "来源文档或文件哈希不一致。"
        if fact.company_id != first.company_id:
            return "公司标识不一致。"
        if fact.currency != first.currency:
            return "币种不一致。"
        if fact.period_type != PERIOD_TYPE_ANNUAL or first.period_type != PERIOD_TYPE_ANNUAL:
            return "期间类型不是年度。"
        if fact.statement_scope != first.statement_scope:
            return "报表口径不一致。"
    if any(fact.period_type != PERIOD_TYPE_ANNUAL for fact in facts):
        return "期间类型不是年度。"
    if require_consolidated and any(fact.statement_scope != "合并" for fact in facts):
        return "所用事实不是合并口径。"
    return None


def _difference(
    indicator_name: str,
    grouped: dict,
    changes: AnnualChangeResult,
    report_year: int,
) -> tuple[Decimal | None, str | None, str | None]:
    current = _single(grouped[(indicator_name, report_year)])
    prior = _single(grouped[(indicator_name, report_year - 1)])
    if current is None or prior is None:
        return None, f"缺少{indicator_name}的当期或上期事实。", None
    try:
        difference = Decimal(current.normalized_value) - Decimal(prior.normalized_value)
    except Exception:
        return None, f"{indicator_name}的规范值不能解释为十进制数。", None
    matched = [
        change
        for change in changes.changes
        if change.indicator_name == indicator_name and change.report_year == report_year
    ]
    if len(matched) > 1:
        return None, f"{indicator_name}的同比结果冲突。", None
    if len(matched) == 1:
        conflict = _change_conflict(matched[0], current, prior, difference)
        if conflict:
            return None, conflict, None
        return difference, None, None
    issues = [issue for issue in changes.issues if issue.indicator_name == indicator_name]
    zero_prior = [issue for issue in issues if issue.code == "zero_prior"]
    if zero_prior and len(issues) == len(zero_prior):
        return (
            difference,
            None,
            f"{indicator_name}的上期规范值为零，这里只记录绝对差额，不表示通常意义的同比增速。",
        )
    if issues:
        return None, issues[0].message, None
    return None, f"{indicator_name}没有可用的同比结果，不能自行重算差额并形成候选。", None


def _change_conflict(change, current: FinancialFact, prior: FinancialFact, difference: Decimal) -> str | None:
    checks = (
        (Decimal(change.current_value) != Decimal(current.normalized_value), "本期规范值"),
        (Decimal(change.prior_value) != Decimal(prior.normalized_value), "上期规范值"),
        (Decimal(change.difference) != difference, "差额"),
        (change.document_id != current.document_id or change.document_id != prior.document_id, "document_id"),
        (change.source_sha256 != current.source_sha256 or change.source_sha256 != prior.source_sha256, "文件哈希"),
        (change.company_id != current.company_id or change.company_id != prior.company_id, "公司"),
        (change.currency != current.currency or change.currency != prior.currency, "币种"),
        (change.statement_scope != current.statement_scope or change.statement_scope != prior.statement_scope, "口径"),
        (change.period_type != current.period_type or change.period_type != prior.period_type, "期间"),
    )
    failed = [name for bad, name in checks if bad]
    if not failed:
        return None
    return f"{current.indicator_name}的同比结果与所选事实不一致：{'、'.join(failed)}。"


def _verdict(checks: dict, fact: FinancialFact) -> str:
    found = checks.get((fact.indicator_name, fact.report_year, fact.column_role), [])
    if len(found) != 1:
        return "missing_or_ambiguous"
    return str(found[0].get("status"))


def _verification_index(verification: Mapping[str, object] | None) -> dict:
    if not isinstance(verification, Mapping):
        return {}
    results = verification.get("results")
    if not isinstance(results, list):
        return {}
    grouped: dict[tuple, list] = defaultdict(list)
    for item in results:
        if not isinstance(item, Mapping):
            continue
        key = (item.get("indicator_name"), item.get("report_year"), item.get("column_role"))
        grouped[key].append(item)
    return grouped


def _group(facts: Sequence[FinancialFact]) -> dict[tuple[str, int], list[FinancialFact]]:
    grouped: dict[tuple[str, int], list[FinancialFact]] = defaultdict(list)
    for fact in facts:
        if not isinstance(fact, FinancialFact):
            raise PdfInputError("screening 只接受 FinancialFact。")
        grouped[(fact.indicator_name, fact.report_year)].append(fact)
    return grouped


def _single(facts: list[FinancialFact]) -> FinancialFact | None:
    if len(facts) != 1:
        return None
    return facts[0]


def _cite(fact: FinancialFact) -> dict:
    return {
        "role": "fact",
        "indicator_name": fact.indicator_name,
        "report_year": fact.report_year,
        "column_role": fact.column_role,
        "normalized_value": fact.normalized_value,
        "document_id": fact.document_id,
        "source_sha256": fact.source_sha256,
        "company_id": fact.company_id,
        "currency": fact.currency,
        "statement_scope": fact.statement_scope,
        "period_type": fact.period_type,
        "hits": [
            {
                "page_number": hit.page_number,
                "block_index": hit.block_index,
                "x0": hit.x0,
                "y0": hit.y0,
                "x1": hit.x1,
                "y1": hit.y1,
            }
            for hit in fact.hits
        ],
    }


def _item(
    signal_id: str,
    title: str,
    status: str,
    statement_kind: str,
    formula: str,
    inputs: list[dict],
    value: dict | None,
    reason: str | None,
    note: str | None = None,
) -> dict:
    return {
        "signal_id": signal_id,
        "title": title,
        "status": status,
        "statement_kind": statement_kind,
        "formula": formula,
        "inputs": inputs,
        "value": value,
        "reason": reason,
        "note": note,
        "limitation": _LIMITATION,
    }


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    digits = len(numerator.as_tuple().digits) + len(denominator.as_tuple().digits) + 8
    with localcontext() as ctx:
        ctx.prec = max(28, digits)
        return numerator / denominator
