"""FinancialFact v2 类型约束，以及从旧事实的显式适配。不跑提取或核验。"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from finagent.schemas import FinancialFactV2, adapt_legacy_financial_fact, assert_evidence_can_support_verified
from finagent.schemas.financial_fact import FinancialFact, FactHit
from finagent.schemas.financial_fact_v2 import (
    NON_RECURRING_LABEL,
    CalculationResult,
    Claim,
    TableCellEvidence,
    VerificationResult,
)
from finagent.schemas.text_pdf import PdfBBox

SHA = "a" * 64


def _hit() -> FactHit:
    return FactHit(page_number=81, block_index=2, text="营业收入 100.00", x0=10.5, y0=20.25, x1=80.5, y1=40.75)


def _legacy(indicator: str, **overrides) -> FinancialFact:
    payload = {
        "document_id": "doc-1",
        "source_sha256": SHA,
        "company_id": "603288",
        "indicator_name": indicator,
        "table_name": "合并利润表",
        "raw_value": "100.00",
        "normalized_value": "100.00",
        "report_year": 2024,
        "period_label": "2024年度",
        "period_type": "annual",
        "column_role": "current",
        "restatement_status": "unknown",
        "unit_multiplier": "1",
        "currency": "人民币",
        "statement_scope": "合并",
        "extraction_method": "legacy",
        "hits": (_hit(),),
        "limitations": ("旧限制",),
    }
    payload.update(overrides)
    return FinancialFact(**payload)


def _fact(**overrides) -> FinancialFactV2:
    payload = {
        "fact_id": "fact-1",
        "metric_id": "revenue",
        "company_id": "603288",
        "label_raw": "营业收入",
        "raw_value": "100.00",
        "normalized_value": "100.00",
        "currency": "人民币",
        "unit_multiplier": "1",
        "unit": "元",
        "report_year": 2024,
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 12, 31),
        "period_type": "duration",
        "frequency": "annual",
        "statement_type": "income_statement",
        "scope": "consolidated",
        "comparison_role": "current",
        "restatement_status": "unknown",
        "source_document_id": "doc-1",
        "source_sha256": SHA,
        "evidence_ids": ("ev-1",),
        "extraction_method": "test",
        "limitations": (),
    }
    payload.update(overrides)
    return FinancialFactV2(**payload)


def test_schema_version_and_exact_decimal_dict() -> None:
    fact = _fact(normalized_value="100.50", unit_multiplier="10000")
    assert fact.schema_version == 2
    assert fact.normalized_value == "100.50"
    assert fact.unit_multiplier == "10000"
    payload = fact.to_dict()
    assert payload == fact.to_dict()
    json.dumps(payload, ensure_ascii=False)
    assert payload["period_start"] == "2024-01-01"
    assert payload["period_end"] == "2024-12-31"
    assert payload["schema_version"] == 2


@pytest.mark.parametrize("value", ["nan", "inf", "abc", ""])
def test_rejects_non_finite_decimal(value: str) -> None:
    with pytest.raises(ValueError):
        _fact(normalized_value=value)


def test_duration_and_instant_constraints() -> None:
    with pytest.raises(ValueError, match="晚于"):
        _fact(period_start=date(2024, 12, 31), period_end=date(2024, 1, 1))
    instant = _fact(
        metric_id="assets",
        label_raw="资产总计",
        period_type="instant",
        period_start=None,
        period_end=date(2024, 12, 31),
        frequency=None,
        statement_type="balance_sheet",
    )
    assert instant.period_end == date(2024, 12, 31)
    with pytest.raises(ValueError, match="instant"):
        _fact(period_type="instant", period_start=date(2024, 1, 1), period_end=date(2024, 12, 31))
    with pytest.raises(ValueError, match="annual"):
        _fact(frequency="quarter")


def test_non_recurring_scope_must_stay_unknown() -> None:
    with pytest.raises(ValueError, match="unknown"):
        _fact(label_raw=NON_RECURRING_LABEL, metric_id="non_recurring_total", scope="consolidated")
    kept = _fact(
        label_raw=NON_RECURRING_LABEL,
        metric_id="non_recurring_total",
        scope="unknown",
        statement_type="non_recurring",
    )
    assert kept.scope == "unknown"


def test_evidence_bbox_and_cross_page_regions() -> None:
    from finagent.schemas.financial_fact_v2 import SourceRegion

    value = SourceRegion("100.00", 81, PdfBBox(1, 2, 3, 4))
    row = SourceRegion("营业收入", 82, PdfBBox(1, 2, 9, 4))
    evidence = TableCellEvidence(
        evidence_id="ev-1",
        document_id="doc-1",
        source_sha256=SHA,
        pdf_page=81,
        printed_page=None,
        table_title="合并利润表",
        row_label="营业收入",
        column_label="2024年度",
        value_raw="100.00",
        value_normalized="100.00",
        unit="元",
        currency="人民币",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_type="duration",
        value_region=value,
        row_region=row,
        column_region=None,
        title_region=None,
        unit_region=None,
        extraction_method="test",
        surrounding_text="合并利润表",
    )
    assert evidence.coordinate_system == "pymupdf_page_top_left"
    assert evidence.row_region is not None and evidence.row_region.page == 82
    dumped = evidence.to_dict()
    assert dumped == evidence.to_dict()
    assert dumped["printed_page"] is None
    assert dumped["period_start"] == "2024-01-01"
    assert dumped["period_end"] == "2024-12-31"
    assert dumped["value_region"]["bbox"] == {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}
    assert dumped["row_region"]["page"] == 82
    assert dumped["row_region"]["bbox"] == {"x0": 1.0, "y0": 2.0, "x1": 9.0, "y1": 4.0}
    with pytest.raises(ValueError, match="宽度"):
        SourceRegion("x", 1, PdfBBox(5, 1, 4, 2))
    with pytest.raises(ValueError, match="1"):
        TableCellEvidence(
            evidence_id="ev-2",
            document_id="doc-1",
            source_sha256=SHA,
            pdf_page=0,
            printed_page=None,
            table_title=None,
            row_label=None,
            column_label=None,
            value_raw="1",
            value_normalized=None,
            unit=None,
            currency=None,
            period_start=None,
            period_end=None,
            period_type=None,
            value_region=None,
            row_region=None,
            column_region=None,
            title_region=None,
            unit_region=None,
            extraction_method="test",
            surrounding_text=None,
        )


def test_verification_calculation_and_claim_do_not_default_verified() -> None:
    with pytest.raises(ValueError, match="evidence_ids"):
        VerificationResult(
            verification_id="ver-1",
            target_type="financial_fact",
            target_id="fact-1",
            status="verified",
            checks=("金额存在",),
            conflicts=(),
            evidence_ids=(),
            limitations=(),
            verified_at=datetime(2026, 9, 25, 12, 0, 0),
        )
    pending = VerificationResult(
        verification_id="ver-2",
        target_type="financial_fact",
        target_id="fact-1",
        status="insufficient_evidence",
        checks=(),
        conflicts=(),
        evidence_ids=(),
        limitations=("尚未独立核验行和列。",),
        verified_at=datetime(2026, 9, 25, 12, 0, 0),
    )
    assert pending.to_dict()["status"] == "insufficient_evidence"
    done = CalculationResult(
        calculation_id="calc-1",
        formula_id="yoy",
        input_fact_ids=("fact-1", "fact-0"),
        formula_expression="(current-prior)/prior",
        output_value="0.10",
        unit="1",
        status="succeeded",
        failure_reason=None,
        rule_version="v1",
    )
    assert done.output_value == "0.10"
    with pytest.raises(ValueError, match="failure_reason"):
        CalculationResult(
            calculation_id="calc-2",
            formula_id="yoy",
            input_fact_ids=("fact-1",),
            formula_expression="(current-prior)/prior",
            output_value="1",
            unit=None,
            status="failed",
            failure_reason=None,
            rule_version="v1",
        )
    claim = Claim(
        claim_id="claim-1",
        claim_type="hypothesis",
        text="差异可能与回款节奏有关。",
        supporting_fact_ids=("fact-1",),
        supporting_evidence_ids=(),
        calculation_ids=(),
        verification_status="insufficient_evidence",
        alternative_explanations=("也可能来自口径变化。",),
        follow_up_items=("核查应收账款附注。",),
    )
    assert claim.claim_type == "hypothesis"
    with pytest.raises(ValueError):
        Claim(
            claim_id="claim-2",
            claim_type="opinion",
            text="不能使用未定义类型。",
            supporting_fact_ids=(),
            supporting_evidence_ids=(),
            calculation_ids=(),
            verification_status="insufficient_evidence",
        )


def test_adapt_known_flow_uses_period_end_not_annual_alias() -> None:
    adapted, evidence = adapt_legacy_financial_fact(_legacy("营业收入"), report_year=2024)
    assert adapted.schema_version == 2
    assert adapted.metric_id == "revenue"
    assert adapted.report_year == 2024
    assert adapted.period_type == "duration"
    assert adapted.frequency == "annual"
    assert adapted.period_end == date(2024, 12, 31)
    assert adapted.scope == "consolidated"
    assert adapted.comparison_role == "current"
    assert adapted.restatement_status == "unknown"
    assert adapted.unit is None
    assert evidence[0].value_region is not None
    assert evidence[0].value_raw == "100.00"
    assert evidence[0].legacy_text_region is not None
    assert evidence[0].legacy_text_region.bbox.x0 == 10.5
    assert evidence[0].row_region is None
    assert evidence[0].column_region is None
    assert evidence[0].title_region is None
    assert evidence[0].table_title is None
    assert evidence[0].printed_page is None
    assert "不视为已核验" in " ".join(adapted.limitations)
    assert all(item.period_type is None for item in evidence)
    region = evidence[0].to_dict()["value_region"]["bbox"]
    assert region == {"x0": 10.5, "y0": 20.25, "x1": 80.5, "y1": 40.75}


def test_adapt_comparative_year_and_unknown_indicator() -> None:
    adapted, _evidence = adapt_legacy_financial_fact(
        _legacy("营业收入", report_year=2023, column_role="comparative", statement_scope="母公司"),
        report_year=2024,
    )
    assert adapted.report_year == 2024
    assert adapted.period_end == date(2023, 12, 31)
    assert adapted.comparison_role == "comparative"
    assert adapted.scope == "parent"
    unknown, unknown_evidence = adapt_legacy_financial_fact(
        _legacy("其他指标", table_name="附注", statement_scope="披露表格口径"),
        report_year=2024,
    )
    assert unknown.period_type is None
    assert unknown.period_end is None
    assert unknown.frequency is None
    assert unknown.scope == "unknown"
    assert unknown_evidence[0].row_label is None
    assert "不根据 annual" in " ".join(unknown.limitations)


def test_title_hit_without_amount_is_not_a_value_region() -> None:
    title = FactHit(page_number=80, block_index=0, text="合并利润表", x0=12, y0=16, x1=120, y1=36)
    amount = FactHit(page_number=81, block_index=1, text="一、营业收入\n100.00", x0=12, y0=48, x1=160, y1=80)
    longer = FactHit(page_number=81, block_index=2, text="1100.00", x0=12, y0=90, x1=80, y1=110)
    adapted, evidence = adapt_legacy_financial_fact(
        _legacy("营业收入", hits=(title, amount, longer)),
        report_year=2024,
    )
    title_evidence, amount_evidence, longer_evidence = evidence
    assert title_evidence.value_region is None
    assert title_evidence.value_raw is None
    assert title_evidence.value_normalized is None
    assert title_evidence.legacy_text_region is not None
    assert title_evidence.legacy_text_region.text == "合并利润表"
    assert title_evidence.legacy_text_region.page == 80
    assert any("不能用于 verified" in item for item in title_evidence.limitations)
    assert amount_evidence.value_region is not None
    assert amount_evidence.value_region.text == "一、营业收入\n100.00"
    assert longer_evidence.value_region is None
    assert longer_evidence.value_normalized is None
    assert adapted.evidence_ids == tuple(item.evidence_id for item in evidence)
    with pytest.raises(ValueError, match="value_region"):
        assert_evidence_can_support_verified((title_evidence, longer_evidence))
    assert_evidence_can_support_verified((title_evidence, amount_evidence))


def test_adapt_non_recurring_scope_stays_unknown() -> None:
    adapted, _evidence = adapt_legacy_financial_fact(
        _legacy(NON_RECURRING_LABEL, table_name="非经常性损益项目和金额", statement_scope="披露表格口径"),
        report_year=2024,
    )
    assert adapted.metric_id == "non_recurring_total"
    assert adapted.scope == "unknown"
    assert adapted.period_type == "duration"
    assert "不映射为合并或母公司" in " ".join(adapted.limitations)
    forced, _evidence = adapt_legacy_financial_fact(
        _legacy(NON_RECURRING_LABEL, statement_scope="合并"),
        report_year=2024,
    )
    assert forced.scope == "unknown"


def test_legacy_financial_fact_constructor_is_unchanged() -> None:
    fact = _legacy("营业收入")
    assert fact.period_type == "annual"
    assert fact.statement_scope == "合并"
    assert fact.column_role == "current"
