"""确定性年度候选筛查。使用构造事实，不读取真实财报。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from finagent.finance.annual_change import AnnualChangeResult, calculate_annual_changes
from finagent.finance.annual_signals import (
    NON_RECURRING,
    OPERATING_CASH,
    PARENT_PROFIT,
    REVENUE,
    screen_annual_signals,
)
from finagent.schemas.financial_fact import PERIOD_TYPE_ANNUAL, FactHit, FinancialFact


def _hit(page: int = 4) -> FactHit:
    return FactHit(page_number=page, block_index=1, text="金额", x0=1, y0=2, x1=3, y1=4)


def _fact(name: str, year: int, value: str, **overrides) -> FinancialFact:
    payload = {
        "document_id": "doc-1",
        "source_sha256": "abc",
        "company_id": "603288",
        "indicator_name": name,
        "table_name": "表",
        "raw_value": value,
        "normalized_value": value,
        "report_year": year,
        "period_label": f"{year}年度",
        "period_type": PERIOD_TYPE_ANNUAL,
        "column_role": "current" if year == 2024 else "comparative",
        "restatement_status": "unknown",
        "unit_multiplier": "1",
        "currency": "人民币",
        "statement_scope": "合并",
        "extraction_method": "test",
        "hits": (_hit(),),
    }
    payload.update(overrides)
    return FinancialFact(**payload)


def _pack(facts: list[FinancialFact], statuses: dict | None = None):
    changes = calculate_annual_changes(facts, 2024)
    results = []
    for fact in facts:
        status = "passed" if statuses is None else statuses.get((fact.indicator_name, fact.report_year), "passed")
        results.append(
            {
                "indicator_name": fact.indicator_name,
                "report_year": fact.report_year,
                "column_role": fact.column_role,
                "status": status,
            }
        )
    verification = {"results": results}
    return screen_annual_signals(facts, changes, verification, 2024)


def _by_id(screen: dict, signal_id: str) -> dict:
    return next(item for item in screen["items"] if item["signal_id"] == signal_id)


def _base() -> list[FinancialFact]:
    return [
        _fact(REVENUE, 2024, "150"),
        _fact(REVENUE, 2023, "100"),
        _fact(PARENT_PROFIT, 2024, "80"),
        _fact(PARENT_PROFIT, 2023, "50"),
        _fact(OPERATING_CASH, 2024, "20"),
        _fact(OPERATING_CASH, 2023, "40"),
        _fact(NON_RECURRING, 2024, "5", statement_scope="披露表格口径", table_name="非经常性损益"),
    ]


def test_triggered_candidate_keeps_decimals_and_coordinates() -> None:
    screen = _pack(_base())
    item = _by_id(screen, "parent_profit_up_operating_cash_down")
    assert item["status"] == "candidate"
    assert item["statement_kind"] == "inference"
    assert item["value"]["left_difference"] == "30"
    assert item["value"]["right_difference"] == "-20"
    assert item["inputs"][0]["role"] == "fact"
    assert item["inputs"][0]["hits"][0]["page_number"] == 4
    assert "不是确认舞弊" in item["limitation"]
    assert "独立验证" in item["limitation"]
    revenue = _by_id(screen, "revenue_up_operating_cash_down")
    assert revenue["status"] == "candidate"
    ratio = _by_id(screen, "operating_cash_to_parent_profit_current")
    assert ratio["status"] == "calculated"
    assert Decimal(ratio["value"]["ratio"]) == Decimal("0.25")
    non_recurring = _by_id(screen, "non_recurring_to_parent_profit")
    assert non_recurring["status"] == "abstained"
    assert non_recurring["value"] is None
    assert "披露表格口径" in non_recurring["reason"]


def test_not_triggered_when_cash_also_rises() -> None:
    facts = _base()
    facts = [fact if fact.indicator_name != OPERATING_CASH or fact.report_year != 2024 else _fact(OPERATING_CASH, 2024, "60") for fact in facts]
    screen = _pack(facts)
    assert _by_id(screen, "parent_profit_up_operating_cash_down")["status"] == "not_triggered"
    assert _by_id(screen, "revenue_up_operating_cash_down")["status"] == "not_triggered"


def test_missing_is_not_zero() -> None:
    facts = [fact for fact in _base() if fact.indicator_name != OPERATING_CASH]
    screen = _pack(facts)
    item = _by_id(screen, "parent_profit_up_operating_cash_down")
    assert item["status"] == "abstained"
    assert "缺少" in item["reason"]
    assert "零" in item["reason"]
    assert item["value"] is None


def test_conflict_abstains() -> None:
    facts = _base() + [_fact(PARENT_PROFIT, 2024, "80", company_id="000001")]
    screen = _pack(facts)
    item = _by_id(screen, "parent_profit_up_operating_cash_down")
    assert item["status"] == "abstained"
    assert "冲突" in item["reason"] or "重复" in item["reason"]


def test_scope_or_currency_mismatch_abstains() -> None:
    facts = [
        _fact(name, year, value, currency="美元") if name == OPERATING_CASH else _fact(name, year, value)
        for name, year, value in (
            (REVENUE, 2024, "150"),
            (REVENUE, 2023, "100"),
            (PARENT_PROFIT, 2024, "80"),
            (PARENT_PROFIT, 2023, "50"),
            (OPERATING_CASH, 2024, "20"),
            (OPERATING_CASH, 2023, "40"),
        )
    ]
    screen = _pack(facts)
    assert "币种" in _by_id(screen, "revenue_up_operating_cash_down")["reason"]


def test_verification_failed_or_abstained() -> None:
    facts = _base()
    failed = _pack(facts, {(PARENT_PROFIT, 2024): "failed"})
    assert "不是通过" in _by_id(failed, "parent_profit_up_operating_cash_down")["reason"]
    held = _pack(facts, {(OPERATING_CASH, 2023): "abstained"})
    assert _by_id(held, "revenue_up_operating_cash_down")["status"] == "abstained"
    assert "abstained" in _by_id(held, "revenue_up_operating_cash_down")["reason"]


def test_zero_denominator_and_negative_prior() -> None:
    facts = [
        _fact(PARENT_PROFIT, 2024, "0"),
        _fact(PARENT_PROFIT, 2023, "-100"),
        _fact(OPERATING_CASH, 2024, "10"),
        _fact(OPERATING_CASH, 2023, "-40"),
        _fact(REVENUE, 2024, "10"),
        _fact(REVENUE, 2023, "8"),
    ]
    screen = _pack(facts)
    current = _by_id(screen, "operating_cash_to_parent_profit_current")
    prior = _by_id(screen, "operating_cash_to_parent_profit_prior")
    assert current["status"] == "abstained"
    assert "小于或等于零" in current["reason"]
    assert prior["status"] == "abstained"
    assert prior["value"] is None
    assert "小于或等于零" in prior["reason"]


def _screen(facts: list[FinancialFact], changes: AnnualChangeResult) -> dict:
    results = [
        {
            "indicator_name": fact.indicator_name,
            "report_year": fact.report_year,
            "column_role": fact.column_role,
            "status": "passed",
        }
        for fact in facts
    ]
    return screen_annual_signals(facts, changes, {"results": results}, 2024)


def test_missing_change_does_not_recompute_candidate() -> None:
    facts = _base()
    changes = calculate_annual_changes(facts, 2024)
    stripped = AnnualChangeResult(
        changes=tuple(change for change in changes.changes if change.indicator_name != PARENT_PROFIT),
        issues=changes.issues,
    )
    item = _by_id(_screen(facts, stripped), "parent_profit_up_operating_cash_down")
    assert item["status"] == "abstained"
    assert item["value"] is None
    assert "不能自行重算" in item["reason"]


def test_tampered_change_abstains() -> None:
    facts = _base()
    changes = calculate_annual_changes(facts, 2024)
    tampered = AnnualChangeResult(
        changes=tuple(
            replace(change, difference="1", current_value="999")
            if change.indicator_name == OPERATING_CASH
            else change
            for change in changes.changes
        ),
        issues=changes.issues,
    )
    item = _by_id(_screen(facts, tampered), "revenue_up_operating_cash_down")
    assert item["status"] == "abstained"
    assert "不一致" in item["reason"]


def test_zero_prior_uses_absolute_difference_without_growth_rate() -> None:
    facts = [
        _fact(PARENT_PROFIT, 2024, "10"),
        _fact(PARENT_PROFIT, 2023, "0"),
        _fact(OPERATING_CASH, 2024, "-5"),
        _fact(OPERATING_CASH, 2023, "8"),
        _fact(REVENUE, 2024, "12"),
        _fact(REVENUE, 2023, "9"),
    ]
    screen = _pack(facts)
    item = _by_id(screen, "parent_profit_up_operating_cash_down")
    assert item["status"] == "candidate"
    assert item["value"]["left_difference"] == "10"
    assert "不表示通常意义的同比增速" in item["note"]
    assert _by_id(screen, "operating_cash_to_parent_profit_prior")["status"] == "abstained"


def test_parent_scope_not_consolidated_abstains() -> None:
    facts = [_fact(PARENT_PROFIT, 2024, "1", statement_scope="母公司"), _fact(OPERATING_CASH, 2024, "1")]
    screen = _pack(facts)
    assert "口径" in _by_id(screen, "operating_cash_to_parent_profit_current")["reason"]
