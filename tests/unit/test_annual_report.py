"""FINTRACE 年度财报分析报告的分区、引用校验和 Markdown。不落盘，不调用模型。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest
import pymupdf

from finagent.finance.v2_calculation import calculate_v2_annual_changes
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
from finagent.verification.claim import (
    expected_calculation_claim_text,
    expected_fact_claim_text,
)
from finagent.verification.comparability import verify_annual_comparability

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
    currency: str = "人民币",
) -> FinancialFactV2:
    return FinancialFactV2(
        fact_id=fact_id,
        metric_id=metric_id or ("revenue" if label == "营业收入" else "other"),
        company_id="603288",
        label_raw=label,
        raw_value=raw,
        normalized_value=raw,
        currency=currency,
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


def _comparability_proof(tmp_path: Path):
    pdf_path = tmp_path / "annual-comparability.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _insert_pdf_text(page, 60, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")
    _insert_pdf_text(page, 60, 110, "(1).重要会计政策变更")
    _insert_pdf_text(page, 60, 130, "□适用   √不适用")
    _insert_pdf_text(page, 60, 160, "(2).重要会计估计变更")
    _insert_pdf_text(page, 60, 180, "□适用   √不适用")
    _insert_pdf_text(
        page,
        60,
        210,
        "(3).2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表",
    )
    _insert_pdf_text(page, 60, 230, "□适用   √不适用")
    page = document.new_page()
    _insert_pdf_text(page, 60, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")
    for index, text in enumerate((
        "1、由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润0元。",
        "2、由于会计政策变更，影响期初未分配利润0元。",
        "3、由于重大会计差错更正，影响期初未分配利润0元。",
        "4、由于同一控制导致的合并范围变更，影响期初未分配利润0元。",
        "5、其他调整合计影响期初未分配利润0元。",
    )):
        _insert_pdf_text(page, 60, 110 + index * 20, text, fontsize=9)
    page = document.new_page()
    _insert_pdf_text(page, 60, 35, "佛山市海天调味食品股份有限公司2024 年年度报告")
    _insert_pdf_text(page, 60, 620, "十八、其他重要事项")
    _insert_pdf_text(page, 60, 640, "1、前期会计差错更正")
    _insert_pdf_text(page, 60, 660, "(1).追溯重述法")
    _insert_pdf_text(page, 60, 680, "□适用   √不适用")
    document.save(pdf_path)
    document.close()
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    return verify_annual_comparability(
        pdf_path,
        document_id="doc-1",
        source_sha256=digest,
        report_year=2024,
    )


def _insert_pdf_text(page, x: int, y: int, text: str, *, fontsize: int = 10) -> None:
    page.insert_text((x, y), text, fontname="china-s", fontsize=fontsize)


def _annual_report_inputs(proof):
    current_evidence = replace(
        _evidence("ev-current", "营业收入 100.10"),
        source_sha256=proof.source_sha256,
    )
    prior_evidence = replace(
        _evidence("ev-prior", "营业收入 90.00", value="90.00"),
        source_sha256=proof.source_sha256,
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        column_label="2023年度",
        column_region=_region("2023年度", 81),
    )
    current_fact = replace(_fact("fact-current", "ev-current"), source_sha256=proof.source_sha256)
    prior_fact = replace(
        _fact("fact-prior", "ev-prior", raw="90.00"),
        source_sha256=proof.source_sha256,
        comparison_role="comparative",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
    )
    fact_verifications = (
        _verification("ver-current", "fact-current", "verified", "ev-current"),
        _verification("ver-prior", "fact-prior", "verified", "ev-prior"),
    )
    calculations, issues = calculate_v2_annual_changes(
        (current_fact, prior_fact),
        fact_verifications,
        2024,
        comparability=proof,
    )
    assert issues == ()
    return (
        (current_fact, prior_fact),
        (current_evidence, prior_evidence),
        fact_verifications,
        calculations,
    )


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
        text=expected_fact_claim_text(good),
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
        calculation_ids=(),
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
    assert any("独立计算核验未通过" in reason for reason in report["pending_review"]["calculations"][0]["placement_reasons"])
    assert report["verified_claims"][0]["claim_id"] == "claim-fact"
    assert report["pending_review"]["claims"] == []
    assert report["interpretations"][0]["alternative_explanations"] == ["也可能只是季节性回款。"]
    assert report["interpretations"][0]["independent_verification"]["status"] == "interpretation"
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
    assert "独立重算受支持的年度差额和同比率" in report["scope"]["note"]
    assert "不重新计算" not in report["scope"]["note"]
    assert "PDF 第81页" in text
    assert "#pdf-page-81" not in text
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


def test_forged_verified_record_cannot_confirm_wrong_fact_claim_text() -> None:
    evidence = _evidence("ev-good", "营业收入 100.10")
    fact = _fact("fact-good", "ev-good")
    claim = Claim(
        claim_id="claim-forged-fact",
        claim_type="fact",
        text="营业收入为 999999.99 元。",
        supporting_fact_ids=("fact-good",),
        supporting_evidence_ids=("ev-good",),
        calculation_ids=(),
        verification_status="verified",
    )
    report = _build(
        facts=(fact,),
        evidences=(evidence,),
        verifications=(
            _verification("ver-good", "fact-good", "verified", "ev-good"),
            _verification("ver-forged-claim", "claim-forged-fact", "verified", "ev-good", target_type="claim"),
        ),
        claims=(claim,),
    )
    assert report["verified_claims"] == []
    pending = report["pending_review"]["claims"][0]
    assert pending["claim_id"] == "claim-forged-fact"
    assert "结构化事实的受控表示" in pending["independent_verification"]["reason"]


def test_forged_calculation_check_cannot_confirm_wrong_result(tmp_path: Path) -> None:
    proof = _comparability_proof(tmp_path)
    facts, evidences, fact_verifications, calculations = _annual_report_inputs(proof)
    wrong = next(item for item in calculations if item.formula_id == "annual_difference")
    wrong = replace(wrong, output_value="99999")
    calculations = tuple(wrong if item.calculation_id == wrong.calculation_id else item for item in calculations)
    forged_check = VerificationResult(
        verification_id="ver-forged-calculation",
        target_type="calculation",
        target_id=wrong.calculation_id,
        status="verified",
        checks=("结果校验：99999", "公式重算通过"),
        conflicts=(),
        evidence_ids=("ev-current", "ev-prior"),
        limitations=(),
        verified_at=WHEN,
    )

    report = _build(
        facts=facts,
        evidences=evidences,
        verifications=(*fact_verifications, forged_check),
        calculations=calculations,
        comparability=proof,
        source_sha256=proof.source_sha256,
    )

    assert all(item["calculation_id"] != wrong.calculation_id for item in report["confirmed"]["analyses"])
    pending = next(
        item for item in report["pending_review"]["calculations"]
        if item["calculation_id"] == wrong.calculation_id
    )
    assert pending["independent_verification"]["status"] == "conflict"
    assert "与独立重算值" in pending["independent_verification"]["reason"]
    markdown = render_markdown(report)
    assert "结果校验：99999" in markdown
    assert "独立核验原因：计算输出 99999 与独立重算值 10.10 不一致。" in markdown


def test_calculation_claim_text_must_match_recomputed_result(tmp_path: Path) -> None:
    proof = _comparability_proof(tmp_path)
    facts, evidences, fact_verifications, calculations = _annual_report_inputs(proof)
    calculation = next(item for item in calculations if item.formula_id == "annual_difference")
    bad_claim = Claim(
        claim_id="claim-forged-calculation",
        claim_type="calculation",
        text="revenue 年度差额为 999999.99。",
        supporting_fact_ids=calculation.input_fact_ids,
        supporting_evidence_ids=("ev-current", "ev-prior"),
        calculation_ids=(calculation.calculation_id,),
        verification_status="verified",
    )
    claim_check = _verification(
        "ver-forged-calculation-claim",
        "claim-forged-calculation",
        "verified",
        "ev-current",
        target_type="claim",
    )

    report = _build(
        facts=facts,
        evidences=evidences,
        verifications=(*fact_verifications, claim_check),
        calculations=calculations,
        claims=(bad_claim,),
        comparability=proof,
        source_sha256=proof.source_sha256,
    )

    assert any(item["calculation_id"] == calculation.calculation_id for item in report["confirmed"]["analyses"])
    assert report["verified_claims"] == []
    pending = report["pending_review"]["claims"][0]
    assert "结构化计算结果的受控表示" in pending["independent_verification"]["reason"]


def test_valid_proof_recomputes_calculation_and_confirms_canonical_claims(tmp_path: Path) -> None:
    proof = _comparability_proof(tmp_path)
    facts, evidences, fact_verifications, calculations = _annual_report_inputs(proof)
    current_fact = facts[0]
    difference = next(item for item in calculations if item.formula_id == "annual_difference")
    fact_claim = Claim(
        claim_id="claim-revenue-fact",
        claim_type="fact",
        text=expected_fact_claim_text(current_fact),
        supporting_fact_ids=(current_fact.fact_id,),
        supporting_evidence_ids=("ev-current",),
        calculation_ids=(),
        verification_status="verified",
    )
    calculation_claim = Claim(
        claim_id="claim-revenue-difference",
        claim_type="calculation",
        text=expected_calculation_claim_text(difference, {fact.fact_id: fact for fact in facts}),
        supporting_fact_ids=difference.input_fact_ids,
        supporting_evidence_ids=("ev-current", "ev-prior"),
        calculation_ids=(difference.calculation_id,),
        verification_status="verified",
    )

    report = _build(
        facts=facts,
        evidences=evidences,
        verifications=fact_verifications,
        calculations=calculations,
        claims=(fact_claim, calculation_claim),
        comparability=proof,
        source_sha256=proof.source_sha256,
    )

    assert {item["calculation_id"] for item in report["confirmed"]["analyses"]} == {
        item.calculation_id for item in calculations
    }
    assert {item["claim_id"] for item in report["verified_claims"]} == {
        "claim-revenue-fact",
        "claim-revenue-difference",
    }
    assert all(item["independent_verification"]["status"] == "verified" for item in report["confirmed"]["analyses"])


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


def test_numbered_non_recurring_title_is_accepted_but_unrelated_title_is_rejected() -> None:
    numbered = _replace_regions(
        _evidence("ev-nr-numbered", "274709462.33", value="274709462.33"),
        row_text="合计",
        title_text="十、 非经常性损益项目和金额",
    )
    unrelated = _replace_regions(
        _evidence("ev-nr-unrelated", "274709462.33", value="274709462.33"),
        row_text="合计",
        title_text="附注：非经常性损益项目和金额",
    )
    accepted_fact = _fact(
        "fact-nr-numbered",
        "ev-nr-numbered",
        label="披露的非经常性损益合计",
        raw="274709462.33",
        metric_id="non_recurring_total",
        scope="unknown",
    )
    rejected_fact = _fact(
        "fact-nr-unrelated",
        "ev-nr-unrelated",
        label="披露的非经常性损益合计",
        raw="274709462.33",
        metric_id="non_recurring_total",
        scope="unknown",
    )
    report = _build(
        facts=(accepted_fact, rejected_fact),
        evidences=(numbered, unrelated),
        verifications=(
            _verification("ver-nr-numbered", "fact-nr-numbered", "verified", "ev-nr-numbered"),
            _verification("ver-nr-unrelated", "fact-nr-unrelated", "verified", "ev-nr-unrelated"),
        ),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == ["fact-nr-numbered"]
    assert [item["fact_id"] for item in report["pending_review"]["facts"]] == ["fact-nr-unrelated"]


def test_operating_cash_row_matches_when_label_wraps_across_lines() -> None:
    evidence = _replace_regions(
        _evidence("ev-cash", "100.10"),
        row_text="经营活动产生的现金流量\n净额",
        title_text="合并现金流量表",
    )
    fact = _fact(
        "fact-cash",
        "ev-cash",
        label="经营活动产生的现金流量净额",
        metric_id="operating_cash_flow",
    )
    report = _build(
        facts=(fact,),
        evidences=(evidence,),
        verifications=(_verification("ver-cash", "fact-cash", "verified", "ev-cash"),),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == ["fact-cash"]


@pytest.mark.parametrize(
    ("fact_currency", "evidence_currency"),
    (("CNY", "人民币"), ("人民币", "CNY")),
)
def test_cny_and_rmb_currency_labels_are_equivalent(fact_currency: str, evidence_currency: str) -> None:
    evidence = replace(_evidence("ev-currency", "100.10"), currency=evidence_currency)
    fact = _fact("fact-currency", "ev-currency", currency=fact_currency)
    report = _build(
        facts=(fact,),
        evidences=(evidence,),
        verifications=(_verification("ver-currency", "fact-currency", "verified", "ev-currency"),),
    )
    assert [item["fact_id"] for item in report["confirmed"]["metrics"]] == ["fact-currency"]


def test_markdown_shows_verification_calculation_and_screening_gaps_with_citations() -> None:
    evidence = _evidence("ev-markdown", "营业收入 100.10")
    fact = _fact("fact-markdown", "ev-markdown")
    failed = CalculationResult(
        calculation_id="calc-failed",
        formula_id="annual_difference",
        input_fact_ids=("fact-markdown",),
        formula_expression="current - prior",
        output_value=None,
        unit="元",
        status="failed",
        failure_reason="追溯调整状态未知，不能比较。",
        rule_version="v2-test",
    )
    report = _build(
        facts=(fact,),
        evidences=(evidence,),
        verifications=(_verification("ver-markdown", "fact-markdown", "verified", "ev-markdown"),),
        calculations=(failed,),
        candidate_signals=(
            {
                "signal_id": "profit_up_cash_down",
                "status": "abstained",
                "reason": "缺少已成功且可比的年度差额。",
                "left_difference": None,
                "right_difference": "-2.00",
                "input_fact_ids": ("fact-markdown",),
                "calculation_ids": ("calc-failed",),
            },
        ),
    )
    markdown = render_markdown(report)
    assert "核验限制：核验限制" in markdown
    assert "失败原因：追溯调整状态未知，不能比较。" in markdown
    assert "筛查原因：缺少已成功且可比的年度差额。" in markdown
    assert "左侧差额：`未提供`" in markdown
    assert "右侧差额：`-2.00`" in markdown
    assert "引用 `ev-markdown`：PDF 第81页" in markdown
    assert "坐标 bbox (10.5, 20.25, 80.5, 40.75)" in markdown
    assert "#pdf-page-" not in markdown


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
