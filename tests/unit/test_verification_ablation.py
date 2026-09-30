from pathlib import Path

import pytest

from finagent.verification.claim import verify_claim
from scripts.evaluate_verification_ablation import (
    EvaluationInputError,
    PROTOCOL_PATH,
    PRIMARY_ANALYSIS_PATH,
    ROOT,
    acceptance_from_unverified,
    claim_from_dict,
    compute_branch_metrics,
    fact_from_dict,
    read_json,
    validate_primary_pdf_text,
    validate_provisional_case,
)


def test_pdf_reference_fails_closed_if_row_column_or_amount_is_missing():
    protocol = read_json(PROTOCOL_PATH)
    reference = protocol["primary_reference"]
    good_page = (
        "81 / 208\n合并利润表\n2024 年度\n2023 年度\n单位：元 币种：人民币\n"
        "其中：营业收入\n七、61\n26,900,977,516.70\n24,559,312,356.59\n"
    )
    validate_primary_pdf_text(good_page, reference)
    with pytest.raises(EvaluationInputError, match="行/列/金额片段"):
        validate_primary_pdf_text(good_page.replace("2024 年度", "2022 年度"), reference)
    with pytest.raises(EvaluationInputError, match="行/列/金额片段"):
        validate_primary_pdf_text(good_page.replace("24,559,312,356.59", "24,559,312,356.58"), reference)


def test_metric_denominators_exclude_provisional_unknown_and_count_abstentions_as_uncovered():
    cases = [
        {
            "case_id": "clean",
            "label_scope": "document_grounded_known",
            "expected_disposition": "accepted",
            "is_injected_error": False,
            "branches": {"arm": {"disposition": "accepted"}},
        },
        {
            "case_id": "injection",
            "label_scope": "document_grounded_known",
            "expected_disposition": "rejected",
            "is_injected_error": True,
            "branches": {"arm": {"disposition": "abstained"}},
        },
        {
            "case_id": "provisional",
            "label_scope": "provisional_unknown",
            "branches": {},
        },
    ]
    result = compute_branch_metrics(cases, "arm")
    assert result["known_case_denominator"] == 2
    assert result["provisional_unknown_excluded_count"] == 1
    assert result["injected_error_denominator"] == 1
    assert result["injected_error_abstention"] == {"numerator": 1, "denominator": 1}
    assert result["known_case_decision_coverage"] == {"numerator": 1, "denominator": 2}


def test_metric_calculation_fails_closed_on_unclassified_case_or_empty_denominator():
    with pytest.raises(EvaluationInputError, match="标签范围"):
        compute_branch_metrics([{"label_scope": "unknown"}], "arm")
    with pytest.raises(EvaluationInputError, match="分母为空"):
        compute_branch_metrics([{"label_scope": "provisional_unknown"}], "arm")


def test_no_verification_arm_trusts_self_report_but_current_claim_verifier_rejects_forged_text():
    protocol = read_json(PROTOCOL_PATH)
    analysis = read_json(PRIMARY_ANALYSIS_PATH)
    fact_payload = next(
        fact
        for fact in analysis["facts"]
        if fact["fact_id"] == "cninfo-1222994233:revenue:2024:current"
    )
    fact = fact_from_dict(fact_payload)
    claim_payload = next(
        case["claim"]
        for case in protocol["known_cases"]
        if case["case_id"] == "inject_unsupported_claim_text"
    )
    claim = claim_from_dict(claim_payload)

    disposition, explanation = acceptance_from_unverified(claim)
    assert disposition == "accepted"
    assert "自报的 verified" in explanation

    checked = verify_claim(
        claim,
        {fact.fact_id: fact},
        {},
        verified_fact_ids={fact.fact_id},
        verified_calculation_ids=set(),
        verified_fact_evidence_ids={fact.fact_id: set(claim.supporting_evidence_ids)},
    )
    assert checked.status == "conflict"
    assert checked.expected_text is not None


def test_provisional_validation_checks_expected_company_not_case_file_self_value():
    protocol = read_json(PROTOCOL_PATH)
    definition = protocol["provisional_cross_company_comparisons"][0]
    case_file = read_json(ROOT / definition["case_path"])
    validate_provisional_case(case_file, definition["company_id"])
    with pytest.raises(EvaluationInputError, match="公司标识"):
        validate_provisional_case(case_file, "000858")


def test_all_protocol_dispositions_use_branch_result_vocabulary_and_five_error_families():
    protocol = read_json(PROTOCOL_PATH)
    expected = {item["expected_disposition"] for item in protocol["known_cases"]}
    assert expected <= {"accepted", "rejected", "abstained"}
    families = {item.get("error_family") for item in protocol["known_cases"] if item.get("error_family")}
    assert families == {
        "wrong_amount",
        "wrong_unit",
        "wrong_year_column_or_period",
        "wrong_statement_scope",
        "unsupported_claim",
    }
