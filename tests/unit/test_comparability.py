"""年度可比性核验：原始 PDF 证据必须独立定位且绑定来源和报告年度。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from finagent.verification.comparability import verify_annual_comparability


def _pdf(
    path: Path,
    *,
    contradiction: bool = False,
    complete: bool = True,
    report_year: int = 2024,
    opening_amount: str = "0",
    missing_opening_report_header: bool = False,
) -> str:
    document = pymupdf.open()
    page = document.new_page()
    _header(page, report_year)
    if complete:
        _text(page, 80, 110, "(1).重要会计政策变更")
        _text(page, 80, 130, "□适用   √不适用")
        _text(page, 80, 170, "(2).重要会计估计变更")
        _text(page, 80, 190, "□适用   √不适用")
        _text(
            page,
            80,
            230,
            f"(3).{report_year}年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表",
        )
        _text(page, 80, 250, "□适用   √不适用")

        page = document.new_page()
        if not missing_opening_report_header:
            _header(page, report_year)
        causes = (
            f"1、由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润{opening_amount}元。",
            f"2、由于会计政策变更，影响期初未分配利润{opening_amount}元。",
            f"3、由于重大会计差错更正，影响期初未分配利润{opening_amount}元。",
            f"4、由于同一控制导致的合并范围变更，影响期初未分配利润{opening_amount}元。",
            f"5、其他调整合计影响期初未分配利润{opening_amount}元。",
        )
        for index, line in enumerate(causes):
            _text(page, 80, 100 + index * 22, line, fontsize=9)

        page = document.new_page()
        _header(page, report_year)
        _text(page, 80, 620, "十八、其他重要事项")
        _text(page, 80, 640, "1、前期会计差错更正")
        _text(page, 80, 660, "(1).追溯重述法")
        _text(page, 80, 680, "□适用   √不适用")

    if contradiction:
        page = document.new_page()
        _header(page, report_year)
        _text(page, 80, 110, "本公司已对2023年比较数据进行追溯调整。")

    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _header(page, report_year: int) -> None:
    _text(page, 80, 35, f"佛山市海天调味食品股份有限公司{report_year} 年年度报告")


def _text(page, x: int, y: int, text: str, *, fontsize: int = 10) -> None:
    page.insert_text((x, y), text, fontname="china-s", fontsize=fontsize)


def _verify(path: Path, sha256: str, report_year: int = 2024):
    return verify_annual_comparability(
        path,
        document_id="doc-haitian-2024",
        source_sha256=sha256,
        report_year=report_year,
    )


def test_wrong_hash_does_not_produce_comparability_proof(tmp_path: Path) -> None:
    pdf = tmp_path / "wrong-hash.pdf"
    _pdf(pdf)

    checked = _verify(pdf, "b" * 64)

    assert checked.status == "insufficient_evidence"
    assert checked.restatement_status == "unknown"
    assert checked.evidence == ()
    assert "SHA256" in checked.limitations[0]


def test_missing_disclosures_abstain(tmp_path: Path) -> None:
    pdf = tmp_path / "missing.pdf"
    sha256 = _pdf(pdf, complete=False)

    checked = _verify(pdf, sha256)

    assert checked.status == "insufficient_evidence"
    assert checked.restatement_status == "unknown"
    assert any("追溯重述法" in item for item in checked.limitations)
    assert any("期初未分配利润" in item for item in checked.limitations)


def test_conflicting_restated_disclosure_fails_closed(tmp_path: Path) -> None:
    pdf = tmp_path / "contradiction.pdf"
    sha256 = _pdf(pdf, contradiction=True)

    checked = _verify(pdf, sha256)

    assert checked.status == "conflict"
    assert checked.restatement_status == "unknown"
    assert any("比较数据进行追溯调整" in item for item in checked.conflicts)
    assert any(item.topic == "contradictory_restatement_disclosure" for item in checked.evidence)


@pytest.mark.parametrize("opening_amount", ["0.00", "0.000", "-0.00"])
def test_decimal_zero_opening_adjustments_are_not_conflicts(tmp_path: Path, opening_amount: str) -> None:
    pdf = tmp_path / f"opening-zero-{opening_amount.replace('.', '-')}.pdf"
    sha256 = _pdf(pdf, opening_amount=opening_amount)

    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.conflicts == ()
    opening_evidence = next(
        item for item in checked.evidence if item.topic == "opening_retained_earnings_adjustments_zero"
    )
    assert opening_amount in opening_evidence.text


def test_nonzero_opening_adjustment_remains_a_conflict(tmp_path: Path) -> None:
    pdf = tmp_path / "opening-nonzero.pdf"
    sha256 = _pdf(pdf, opening_amount="0.01")

    checked = _verify(pdf, sha256)

    assert checked.status == "conflict"
    assert any("0.01 元" in item for item in checked.conflicts)


def test_missing_report_year_header_is_insufficient_evidence_not_a_conflict(tmp_path: Path) -> None:
    pdf = tmp_path / "missing-opening-year-header.pdf"
    sha256 = _pdf(pdf, missing_opening_report_header=True)

    checked = _verify(pdf, sha256)

    assert checked.status == "insufficient_evidence"
    assert checked.conflicts == ()
    assert any("缺少报告年份抬头" in item for item in checked.limitations)


def test_explicit_zero_restatement_with_decimal_zeros_is_not_a_conflict(tmp_path: Path) -> None:
    pdf = tmp_path / "zero-restatement.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 100, "本公司已对2023年比较数据进行追溯调整，影响期初未分配利润0.00元。")
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert not any("明确的追溯调整披露" in item for item in checked.conflicts)


def test_reliable_complete_disclosures_produce_bound_proof(tmp_path: Path) -> None:
    pdf = tmp_path / "complete.pdf"
    sha256 = _pdf(pdf)

    checked = _verify(pdf, sha256)

    assert checked.status == "verified"
    assert checked.restatement_status == "not_restated"
    assert checked.current_year == 2024
    assert checked.comparative_year == 2023
    assert checked.source_sha256 == sha256
    topics = {item.topic for item in checked.evidence}
    assert topics == {
        "major_accounting_policy_change",
        "major_accounting_estimate_change",
        "first_adoption_opening_balance_adjustment",
        "prior_error_retrospective_restatement",
        "opening_retained_earnings_adjustments_zero",
    }
    assert all(item.document_id == "doc-haitian-2024" for item in checked.evidence)
    assert all(item.source_sha256 == sha256 for item in checked.evidence)
    assert all(item.report_year == 2024 and item.comparative_year == 2023 for item in checked.evidence)
    assert all(item.pdf_page > 0 and item.bbox.x1 > item.bbox.x0 for item in checked.evidence)
    assert all("2024" in item.report_year_text and item.report_year_page == item.pdf_page for item in checked.evidence)
    assert any("上一年度已披露报告" in item for item in checked.limitations)


def test_report_year_specific_first_adoption_heading_is_parameterized(tmp_path: Path) -> None:
    pdf = tmp_path / "annual-2025.pdf"
    sha256 = _pdf(pdf, report_year=2025)

    checked = _verify(pdf, sha256, report_year=2025)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.report_year == checked.current_year == 2025
    assert checked.comparative_year == 2024
    first_adoption = next(
        item for item in checked.evidence if item.topic == "first_adoption_opening_balance_adjustment"
    )
    assert "2025年起首次执行" in first_adoption.text


def test_haitian_2024_source_contains_explicit_restated_comparability_disclosures() -> None:
    root = Path(__file__).resolve().parents[2]
    pdf = root / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "1222994233.PDF"
    if not pdf.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")

    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.restatement_status == "not_restated"
    assert checked.source_sha256 == sha256
    evidence_by_topic = {item.topic: item for item in checked.evidence}
    assert evidence_by_topic["major_accounting_policy_change"].pdf_page == 116
    assert evidence_by_topic["opening_retained_earnings_adjustments_zero"].pdf_page == 163
    assert evidence_by_topic["prior_error_retrospective_restatement"].pdf_page == 197
    assert "√不适用" in evidence_by_topic["prior_error_retrospective_restatement"].text
    assert "期初未分配利润0 元" in evidence_by_topic["opening_retained_earnings_adjustments_zero"].text
