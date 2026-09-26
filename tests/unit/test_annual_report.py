"""FINTRACE 年度财报分析报告的分区、引用校验和 Markdown。不落盘，不调用模型。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from finagent.reports.annual_report import ReportBuildError, build_annual_report, render_markdown
from finagent.schemas.financial_fact_v2 import (
    CalculationResult,
    Claim,
    FinancialFactV2,
    SourceRegion,
    TableCellEvidence,
    VerificationResult,
)
from finagent.schemas.text_pdf import PdfBBox

SHA = "ab" * 32
OTHER_SHA = "cd" * 32
WHEN = datetime(2026, 9, 25, 12, 0, 0)


def _region(text: str, page: int = 81) -> SourceRegion:
    return SourceRegion(text, page, PdfBBox(10.5, 20.25, 80.5, 40.75))


def _evidence(evidence_id: str, text: str, *, value: str | None = "100.10") -> TableCellEvidence:
    region = _region(text)
    has_value = value is not None and value in text
    return TableCellEvidence(
        evidence_id=evidence_id,
        document_id="doc-1",
        source_sha256=SHA,
        pdf_page=81,
        printed_page=79,
        table_title="合并利润表",
        row_label="营业收入",
        column_label="2024年度",
        value_raw=value if has_value else None,
        value_normalized=value if has_value else None,
        unit="元",
        currency="人民币",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_type="duration",
        value_region=region if has_value else None,
        row_region=_region("营业收入", 82) if has_value else None,
        column_region=_region("2024年度", 81) if has_value else None,
        title_region=_region("合并利润表", 80) if has_value else None,
        unit_region=_region("单位：元", 81) if has_value else None,
        extraction_method="test",
        surrounding_text=text,
        legacy_text_region=region,
    )


def _fact(
    fact_id: str,
    evidence_id: str,
    *,
    label: str = "营业收入",
    raw: str = "100.10",
    unit: str | None = "元",
    metric_id: str | None = None,
    scope: str = "consolidated",
) -> FinancialFactV2:
    return FinancialFactV2(
        fact_id=fact_id,
        metric_id=metric_id or ("revenue" if label == "营业收入" else "other"),
        company_id="603288",
        label_raw=label,
        raw_value=raw,
        normalized_value=raw,
        currency="人民币",
        unit_multiplier="1",
        unit=unit,
        report_year=2024,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_type="duration",
        frequency="annual",
        statement_type="income_statement",
        scope=scope,
        comparison_role="current",
        restatement_status="unknown",
        source_document_id="doc-1",
        source_sha256=SHA,
        evidence_ids=(evidence_id,),
        extraction_method="test",
        limitations=("事实限制",),
    )


def _verification(verification_id: str, target_id: str, status: str, evidence_id: str, *, target_type: str = "financial_fact") -> VerificationResult:
    return VerificationResult(
        verification_id=verification_id,
        target_type=target_type,
        target_id=target_id,
        status=status,
        checks=(f"{status} 分项",),
        conflicts=() if status != "conflict" else ("金额与原文不一致",),
        evidence_ids=(evidence_id,),
        limitations=("核验限制",),
        verified_at=WHEN,
    )


def _calculation(calculation_id: str, fact_id: str, status: str = "succeeded") -> CalculationResult:
    return CalculationResult(
        calculation_id=calculation_id,
        formula_id="yoy",
        input_fact_ids=(fact_id,),
        formula_expression="(current-prior)/prior",
        output_value="0.10" if status == "succeeded" else None,
        unit="1",
        status=status,
        failure_reason=None if status == "succeeded" else "缺少上期",
        rule_version="v1",
    )


def _build(**overrides):
    payload = {
        "run_id": "annual-report-test",
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": "doc-1",
        "source_sha256": SHA,
        "facts": (),
        "evidences": (),
        "verifications": (),
        "calculations": (),
        "claims": (),
        "candidate_signals": (),
        "limitations": (),
    }
    payload.update(overrides)
    return build_annual_report(**payload)


def test_verified_items_are_separated_from_conflict_and_candidates() -> None:
    good_evidence = _evidence("ev-good", "营业收入 100.10")
    weak_evidence = _evidence("ev-weak", "合并利润表", value=None)
    conflict_evidence = _evidence("ev-conflict", "营业收入 80.00", value="80.00")
    missing_verification_evidence = _evidence("ev-open", "营业收入 50.00", value="50.00")
    good = _fact("fact-good", "ev-good")
    conflicted = _fact("fact-conflict", "ev-conflict", raw="80.00")
    insufficient = _fact("fact-insufficient", "ev-weak", label="其他指标", raw="1.00")
    unverified = _fact("fact-open", "ev-open", raw="50.00")
    calculation = _calculation("calc-good", "fact-good")
    open_calculation = _calculation("calc-open", "fact-open")
    fact_claim = Claim(
        claim_id="claim-fact",
        claim_type="fact",
        text="营业收入为 100.10 元。",
        supporting_fact_ids=("fact-good",),
        supporting_evidence_ids=("ev-good",),
        calculation_ids=(),
        verification_status="verified",
        limitations=("只复述已核验事实。",),
    )
    inference = Claim(
        claim_id="claim-inference",
        claim_type="inference",
        text="利润与经营现金流变化存在背离。",
        supporting_fact_ids=("fact-good",),
        supporting_evidence_ids=("ev-good",),
        calculation_ids=("calc-good",),
        verification_status="insufficient_evidence",
        limitations=("推论限制，尚未排除口径差异。",),
        alternative_explanations=("也可能只是季节性回款。",),
        follow_up_items=("核查应收账款附注。",),
    )
    report = _build(
        facts=(good, conflicted, insufficient, unverified),
        evidences=(good_evidence, weak_evidence, conflict_evidence, missing_verification_evidence),
        verifications=(
            _verification("ver-good", "fact-good", "verified", "ev-good"),
            _verification("ver-calc", "calc-good", "verified", "ev-good", target_type="calculation"),
            _verification("ver-conflict", "fact-conflict", "conflict", "ev-conflict"),
            _verification("ver-insufficient", "fact-insufficient", "insufficient_evidence", "ev-weak"),
            _verification("ver-claim", "claim-fact", "verified", "ev-good", target_type="claim"),
        ),
        calculations=(calculation, open_calculation),
        claims=(fact_claim, inference),
        candidate_signals=(
            {"signal_id": "revenue_cash", "title": "收入升而经营现金流降", "status": "candidate", "formula": "差额>0"},
        ),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == ["fact-good"]
    assert report["confirmed"]["analyses"] == []
    assert [item["calculation_id"] for item in report["pending_review"]["calculations"]] == ["calc-good", "calc-open"]
    pending_ids = [item["fact_id"] for item in report["pending_review"]["facts"]]
    assert pending_ids == ["fact-conflict", "fact-insufficient", "fact-open"]
    assert any("公式重算" in reason for reason in report["pending_review"]["calculations"][0]["placement_reasons"])
    assert report["verified_claims"][0]["claim_id"] == "claim-fact"
    assert report["pending_review"]["claims"] == []
    assert report["interpretations"][0]["alternative_explanations"] == ["也可能只是季节性回款。"]
    assert report["interpretations"][0]["follow_up_items"] == ["核查应收账款附注。"]
    assert report["interpretations"][0]["limitations"] == ["推论限制，尚未排除口径差异。"]
    assert "model_called" not in report
    assert report["pending_review"]["candidate_signals"][0]["signal_id"] == "revenue_cash"
    assert report["pending_review"]["candidate_signals"][0]["fraud_conclusion"] is None
    assert report["fraud_conclusion"] is None
    assert report["confirmed"]["metrics"][0]["normalized_value"] == "100.10"
    assert report["confirmed"]["metrics"][0]["verifications"][0]["checks"] == ["verified 分项"]
    assert report["confirmed"]["metrics"][0]["evidences"][0]["row_label"] == "营业收入"
    assert report["pending_review"]["calculations"][0]["formula_expression"] == "(current-prior)/prior"
    assert report["pending_review"]["calculations"][0]["output_value"] == "0.10"
    text = render_markdown(report)
    assert text.startswith("# FINTRACE 年度财报分析报告 603288 2024")
    assert "模型调用" not in text.split("## 范围与缺口", 1)[0]
    assert "[第81页](#pdf-page-81)" in text
    assert "bbox (10.5, 20.25, 80.5, 40.75)" in text
    assert "也可能只是季节性回款。" in text
    assert "核查应收账款附注。" in text
    assert "推论限制，尚未排除口径差异。" in text
    assert "不是确认舞弊" in text
    assert "fact-conflict" in text
    assert "revenue_cash" in text.split("## 待核查", 1)[1]
    assert "revenue_cash" not in text.split("## 待核查", 1)[0]
    marked = _build(model_called=True)
    assert marked["model_called"] is True
    assert "模型调用：是" in render_markdown(marked)


def test_independent_evidence_id_can_confirm_when_semantics_match() -> None:
    extracted = _evidence("ev-extract", "营业收入 100.10")
    independent = _evidence("ev-independent", "营业收入 100.10")
    fact = _fact("fact-good", "ev-extract")
    report = _build(
        facts=(fact,),
        evidences=(extracted, independent),
        verifications=(_verification("ver-good", "fact-good", "verified", "ev-independent"),),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == ["fact-good"]
    assert report["confirmed"]["metrics"][0]["verification_evidences"][0]["evidence_id"] == "ev-independent"


def test_legacy_or_incomplete_citation_stays_pending() -> None:
    extracted = _evidence("ev-extract", "营业收入 100.10")
    legacy = _evidence("ev-legacy", "合并利润表", value=None)
    value_only = TableCellEvidence(
        evidence_id="ev-value-only",
        document_id="doc-1",
        source_sha256=SHA,
        pdf_page=81,
        printed_page=None,
        table_title=None,
        row_label=None,
        column_label=None,
        value_raw="100.10",
        value_normalized="100.10",
        unit="元",
        currency="人民币",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_type="duration",
        value_region=_region("营业收入 100.10"),
        row_region=None,
        column_region=None,
        title_region=None,
        unit_region=None,
        extraction_method="test",
        surrounding_text="营业收入 100.10",
    )
    fact = _fact("fact-good", "ev-extract")
    legacy_report = _build(
        facts=(fact,),
        evidences=(extracted, legacy),
        verifications=(_verification("ver-legacy", "fact-good", "verified", "ev-legacy"),),
    )
    value_report = _build(
        facts=(fact,),
        evidences=(extracted, value_only),
        verifications=(_verification("ver-value", "fact-good", "verified", "ev-value-only"),),
    )
    assert legacy_report["confirmed"]["metrics"] == []
    assert value_report["confirmed"]["metrics"] == []
    assert any("旧文字块" in reason for reason in legacy_report["pending_review"]["facts"][0]["placement_reasons"])
    assert any("旧文字块" in reason for reason in value_report["pending_review"]["facts"][0]["placement_reasons"])


def test_missing_reference_and_wrong_hash_are_rejected() -> None:
    evidence = _evidence("ev-good", "营业收入 100.10")
    fact = _fact("fact-good", "ev-missing")
    with pytest.raises(ReportBuildError, match="引用缺失"):
        _build(facts=(fact,), evidences=(evidence,))
    from dataclasses import replace

    bad_hash = replace(fact := _fact("fact-good", "ev-good"), source_sha256=OTHER_SHA)
    with pytest.raises(ReportBuildError, match="哈希不一致"):
        _build(facts=(bad_hash,), evidences=(evidence,))


def test_shared_fact_evidence_does_not_verify_calculation() -> None:
    current = _evidence("ev-current", "营业收入 100.10")
    prior = _evidence("ev-prior", "营业收入 90.00", value="90.00")
    current_fact = _fact("fact-current", "ev-current")
    prior_fact = _fact("fact-prior", "ev-prior", raw="90.00")
    calculation = CalculationResult(
        calculation_id="calc-yoy",
        formula_id="yoy",
        input_fact_ids=("fact-current", "fact-prior"),
        formula_expression="(current-prior)/prior",
        output_value="0.10",
        unit="1",
        status="succeeded",
        failure_reason=None,
        rule_version="v1",
    )
    shared = VerificationResult(
        verification_id="ver-calc",
        target_type="calculation",
        target_id="calc-yoy",
        status="verified",
        checks=("公式重算：(current-prior)/prior = 0.10",),
        conflicts=(),
        evidence_ids=("ev-current",),
        limitations=(),
        verified_at=WHEN,
    )
    report = _build(
        facts=(current_fact, prior_fact),
        evidences=(current, prior),
        verifications=(
            _verification("ver-current", "fact-current", "verified", "ev-current"),
            _verification("ver-prior", "fact-prior", "verified", "ev-prior"),
            shared,
        ),
        calculations=(calculation,),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == ["fact-current", "fact-prior"]
    assert report["confirmed"]["analyses"] == []
    assert report["pending_review"]["calculations"][0]["calculation_id"] == "calc-yoy"


def test_claim_follows_confirmed_supports_not_evidence_ids() -> None:
    evidence = _evidence("ev-good", "营业收入 100.10")
    conflict_evidence = _evidence("ev-conflict", "营业收入 80.00", value="80.00")
    good = _fact("fact-good", "ev-good")
    conflicted = _fact("fact-conflict", "ev-conflict", raw="80.00")
    claim = Claim(
        claim_id="claim-fact",
        claim_type="fact",
        text="把未确认事实说成已核实。",
        supporting_fact_ids=("fact-conflict",),
        supporting_evidence_ids=("ev-good",),
        calculation_ids=(),
        verification_status="verified",
    )
    report = _build(
        facts=(good, conflicted),
        evidences=(evidence, conflict_evidence),
        verifications=(
            _verification("ver-good", "fact-good", "verified", "ev-good"),
            _verification("ver-conflict", "fact-conflict", "conflict", "ev-conflict"),
            _verification("ver-claim", "claim-fact", "verified", "ev-good", target_type="claim"),
        ),
        claims=(claim,),
    )
    assert report["verified_claims"] == []
    assert report["pending_review"]["claims"][0]["claim_id"] == "claim-fact"
    assert "fact-good" in [item["fact_id"] for item in report["confirmed"]["metrics"]]


def test_calculation_claim_stays_pending_until_calculation_is_confirmed() -> None:
    evidence = _evidence("ev-good", "营业收入 100.10")
    fact = _fact("fact-good", "ev-good")
    calculation = _calculation("calc-open", "fact-good")
    claim = Claim(
        claim_id="claim-calc",
        claim_type="calculation",
        text="同比为 0.10。",
        supporting_fact_ids=("fact-good",),
        supporting_evidence_ids=("ev-good",),
        calculation_ids=("calc-open",),
        verification_status="verified",
    )
    report = _build(
        facts=(fact,),
        evidences=(evidence,),
        verifications=(
            _verification("ver-good", "fact-good", "verified", "ev-good"),
            _verification("ver-claim", "claim-calc", "verified", "ev-good", target_type="claim"),
        ),
        calculations=(calculation,),
        claims=(claim,),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == ["fact-good"]
    assert report["confirmed"]["analyses"] == []
    assert report["verified_claims"] == []
    assert report["pending_review"]["claims"][0]["claim_id"] == "claim-calc"


def test_empty_inputs_state_the_gap_without_a_conclusion() -> None:
    report = _build()
    assert report["confirmed"] == {"metrics": [], "analyses": []}
    assert report["fraud_conclusion"] is None
    assert any("未提供财务事实" in gap for gap in report["scope"]["gaps"])
    text = render_markdown(report)
    assert "不补写结论" in text
    assert "舞弊结论：无" in text


def test_adapted_blank_unit_and_non_recurring_scope_can_be_confirmed() -> None:
    revenue = _evidence("ev-revenue", "100.10")
    revenue_fact = _fact("fact-revenue", "ev-revenue", unit=None, label="营业收入")
    wrapped = _evidence("ev-profit", "6344125969.00", value="6344125969.00")
    wrapped = _replace_regions(
        wrapped,
        row_text="归属于母公司\n股东的净利润",
        title_text="合并利润表",
    )
    profit = _fact(
        "fact-profit",
        "ev-profit",
        unit=None,
        label="披露用归母净利润",
        raw="6344125969.00",
        metric_id="net_profit_parent",
    )
    non_recurring = _evidence("ev-nr", "274709462.33", value="274709462.33")
    non_recurring = _replace_regions(
        non_recurring,
        row_text="合计",
        title_text="非经常性损益项目和金额",
    )
    non_recurring_fact = _fact(
        "fact-nr",
        "ev-nr",
        unit=None,
        label="披露的非经常性损益合计",
        raw="274709462.33",
        metric_id="non_recurring_total",
        scope="unknown",
    )
    merged_title = _replace_regions(non_recurring, row_text="合计", title_text="合并利润表", evidence_id="ev-nr-merged")
    merged_fact = _fact(
        "fact-nr-merged",
        "ev-nr-merged",
        unit=None,
        label="披露的非经常性损益合计",
        raw="274709462.33",
        metric_id="non_recurring_total",
        scope="unknown",
    )
    wrong_unit = _replace_regions(
        revenue,
        row_text="营业收入",
        title_text="合并利润表",
        unit_text="单位：万元",
        unit="万元",
        evidence_id="ev-unit",
    )
    wrong_unit_fact = _fact("fact-unit", "ev-unit", unit=None, raw="100.10")
    report = _build(
        facts=(revenue_fact, profit, non_recurring_fact, merged_fact, wrong_unit_fact),
        evidences=(revenue, wrapped, non_recurring, merged_title, wrong_unit),
        verifications=(
            _verification("ver-revenue", "fact-revenue", "verified", "ev-revenue"),
            _verification("ver-profit", "fact-profit", "verified", "ev-profit"),
            _verification("ver-nr", "fact-nr", "verified", "ev-nr"),
            _verification("ver-merged", "fact-nr-merged", "verified", "ev-nr-merged"),
            _verification("ver-unit", "fact-unit", "verified", "ev-unit"),
        ),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == [
        "fact-nr",
        "fact-profit",
        "fact-revenue",
    ]
    pending = [item["fact_id"] for item in report["pending_review"]["facts"]]
    assert pending == ["fact-nr-merged", "fact-unit"]


def _replace_regions(
    evidence,
    *,
    row_text: str,
    title_text: str,
    unit_text: str | None = None,
    unit: str | None = None,
    evidence_id: str | None = None,
):
    from dataclasses import replace

    return replace(
        evidence,
        evidence_id=evidence_id or evidence.evidence_id,
        row_label=row_text,
        row_region=_region(row_text, 82),
        table_title=title_text,
        title_region=_region(title_text, 80),
        unit=unit or evidence.unit,
        unit_region=_region(unit_text, 81) if unit_text is not None else evidence.unit_region,
    )


def test_amount_text_is_not_rewritten() -> None:
    evidence = _evidence("ev-good", "营业收入 26900977516.70", value="26900977516.70")
    fact = _fact("fact-good", "ev-good", raw="26900977516.70")
    report = _build(
        facts=(fact,),
        evidences=(evidence,),
        verifications=(_verification("ver-good", "fact-good", "verified", "ev-good"),),
    )
    assert report["confirmed"]["metrics"][0]["raw_value"] == "26900977516.70"
    assert report["confirmed"]["metrics"][0]["normalized_value"] == "26900977516.70"
    assert "26900977516.70" in render_markdown(report)
    with pytest.raises(ReportBuildError, match="浮点数"):
        _build(candidate_signals=({"signal_id": "bad", "value": 0.1},))
