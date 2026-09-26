"""海天 2024 公开年报上的四项指标、两个年度列，共 8 条事实。"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from finagent.schemas.financial_fact_v2 import FinancialFactV2
from finagent.verification.independent_fact import verify_financial_fact

_PDF = Path("data/raw/603288/2024/cninfo-1222994233/1222994233.PDF")


def _fact(sha: str, metric: str, fact_id: str, year: int, raw: str, role: str, scope: str) -> FinancialFactV2:
    return FinancialFactV2(
        fact_id=fact_id,
        metric_id=metric,
        company_id="603288",
        label_raw=metric,
        raw_value=raw,
        normalized_value=raw.replace(",", ""),
        currency="CNY",
        unit_multiplier="1",
        unit="元",
        report_year=2024,
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        period_type="duration",
        frequency="annual",
        statement_type=None,
        scope=scope,  # type: ignore[arg-type]
        comparison_role=role,  # type: ignore[arg-type]
        restatement_status="unknown",
        source_document_id="cninfo-1222994233",
        source_sha256=sha,
        evidence_ids=("not-used",),
        extraction_method="test",
    )


def test_haitian_eight_facts() -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")
    sha = hashlib.sha256(_PDF.read_bytes()).hexdigest()
    cases = (
        ("revenue", "rev-2024", 2024, "26,900,977,516.70", "current", "consolidated", 81),
        ("revenue", "rev-2023", 2023, "24,559,312,356.59", "comparative", "consolidated", 81),
        ("net_profit_parent", "profit-2024", 2024, "6,344,125,969.00", "current", "consolidated", 82),
        ("net_profit_parent", "profit-2023", 2023, "5,626,626,091.97", "comparative", "consolidated", 82),
        ("operating_cash_flow", "cash-2024", 2024, "6,843,710,887.07", "current", "consolidated", 86),
        ("operating_cash_flow", "cash-2023", 2023, "7,355,650,997.74", "comparative", "consolidated", 86),
        ("non_recurring_total", "nr-2024", 2024, "274,709,462.33", "current", "unknown", 9),
        ("non_recurring_total", "nr-2023", 2023, "231,962,157.80", "comparative", "unknown", 9),
    )
    for metric, fact_id, year, raw, role, scope, page in cases:
        checked = verify_financial_fact(_PDF, _fact(sha, metric, fact_id, year, raw, role, scope))
        assert checked.result.status == "verified", (metric, year, checked.result.limitations)
        evidence = checked.evidence[0]
        assert evidence.value_region is not None
        assert evidence.value_region.page == page
        assert evidence.unit == "元"
        assert evidence.currency == "人民币"
        assert evidence.period_end == date(year, 12, 31)
        assert evidence.value_normalized == raw.replace(",", "")
        assert evidence.evidence_id != "not-used"
