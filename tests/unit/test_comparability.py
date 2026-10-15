"""年度可比性核验：原始 PDF 证据必须独立定位且绑定来源和报告年度。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from finagent.verification.comparability import verify_annual_comparability

_OPENING_CAUSES = (
    "准则追溯调整",
    "会计政策变更",
    "重大会计差错更正",
    "同一控制合并范围变更",
    "其他调整",
)


def _pdf(
    path: Path,
    *,
    contradiction: bool = False,
    complete: bool = True,
    report_year: int = 2024,
    opening_amount: str = "0",
    missing_opening_report_header: bool = False,
    header_style: str = "年年度报告",
    company: str = "佛山市海天调味食品股份有限公司",
    checkbox: str = "not_applicable",
    split_checkbox: bool = False,
    policy_applicable_without_impact: bool = False,
    policy_applicable_with_no_impact: bool = False,
    use_yes_no_restatement: bool = False,
    unexplained_opening_total: str | None = None,
    opening_total_note: str | None = None,
    include_opening: bool | None = None,
) -> str:
    document = pymupdf.open()
    page = document.new_page()
    _header(page, report_year, style=header_style, company=company)
    if complete:
        _text(page, 80, 110, "(1).重要会计政策变更")
        if policy_applicable_with_no_impact:
            _checkbox(page, 80, 130, "applicable", split=split_checkbox)
            _text(page, 80, 148, "本次会计政策变更未对本公司的财务报表产生影响。")
        elif policy_applicable_without_impact:
            _checkbox(page, 80, 130, "applicable", split=split_checkbox)
        else:
            _checkbox(page, 80, 130, checkbox, split=split_checkbox)
        _text(page, 80, 170, "(2).重要会计估计变更")
        _checkbox(page, 80, 190, checkbox, split=split_checkbox)
        _text(
            page,
            80,
            230,
            f"(3).{report_year}年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表",
        )
        _checkbox(page, 80, 250, checkbox, split=split_checkbox)

        page = document.new_page()
        if not missing_opening_report_header:
            _header(page, report_year, style=header_style, company=company)
        if unexplained_opening_total is not None:
            _text(page, 80, 72, "调整期初未分配利润合计数（调增+，调减-）")
            if opening_total_note:
                _text(page, 360, 72, opening_total_note)
            if unexplained_opening_total:
                _text(page, 420, 72, unexplained_opening_total)
        if include_opening is not False:
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
        _header(page, report_year, style=header_style, company=company)
        if use_yes_no_restatement:
            _text(page, 80, 620, "公司是否需追溯调整或重述以前年度会计数据")
            _checkbox(page, 80, 640, "yes_no_no")
        else:
            _text(page, 80, 620, "十八、其他重要事项")
            _text(page, 80, 640, "1、前期会计差错更正")
            _text(page, 80, 660, "(1).追溯重述法")
            _checkbox(page, 80, 680, checkbox, split=split_checkbox)

    if contradiction:
        page = document.new_page()
        _header(page, report_year, style=header_style, company=company)
        _text(page, 80, 110, f"本公司已对{report_year - 1}年比较数据进行追溯调整。")

    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _header(page, report_year: int, *, style: str = "年年度报告", company: str = "佛山市海天调味食品股份有限公司") -> None:
    if style == "年度报告全文":
        _text(page, 80, 35, f"{company}{report_year} 年度报告全文")
        return
    _text(page, 80, 35, f"{company}{report_year} 年年度报告")


def _checkbox(page, x: int, y: int, kind: str, *, split: bool = False) -> None:
    if kind == "yes_no_no":
        _text(page, x, y, "□是 √否")
        return
    if kind == "applicable":
        _text(page, x, y, "√适用 □不适用")
        return
    if kind == "wingdings_not_applicable":
        _insert_wingdings_not_applicable(page, x, y)
        return
    if split:
        _text(page, x, y, "□适用")
        _text(page, x + 70, y, "√不适用")
        return
    _text(page, x, y, "□适用   √不适用")


def _text(page, x: int, y: int, text: str, *, fontsize: int = 10) -> None:
    page.insert_text((x, y), text, fontname="china-s", fontsize=fontsize)


def _insert_wingdings_not_applicable(page, x: int, y: int, *, fontsize: int = 10) -> None:
    _text(page, x, y, "□适用 ", fontsize=fontsize)
    _insert_wingdings_check(page, x + 36, y, fontsize=fontsize)
    _text(page, x + 52, y, "不适用", fontsize=fontsize)


def _insert_wingdings_check(page, x: float, y: float, *, fontsize: int = 10) -> None:
    """china-s 会丢掉 PUA 勾选；用带 ToUnicode 的 Type1 字把 \\uf052 写进提取文本。"""

    width = float(page.rect.width)
    height = float(page.rect.height)
    pdf_y = height - y
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\n"
        b"begincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /Adobe-Identity-UCS def\n"
        b"/CMapType 2 def\n"
        b"1 begincodespacerange\n"
        b"<00> <FF>\n"
        b"endcodespacerange\n"
        b"1 beginbfchar\n"
        b"<41> <F052>\n"
        b"endbfchar\n"
        b"endcmap\n"
        b"CMapName currentdict /CMap defineresource pop\n"
        b"end\n"
        b"end"
    )
    content = f"BT /F1 {fontsize} Tf {x:.2f} {pdf_y:.2f} Td (A) Tj ET".encode("ascii")
    body = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []

    def add(obj: bytes) -> None:
        offsets.append(len(body))
        body.extend(obj)
        body.extend(b"\n")

    add(b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj")
    add(b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj")
    add(
        (
            "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 "
            f"{width:.2f} {height:.2f}]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj"
        ).encode("ascii")
    )
    add(b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding/ToUnicode 6 0 R>>endobj")
    add(b"5 0 obj<</Length %d>>stream\n" % len(content) + content + b"\nendstream\nendobj")
    add(b"6 0 obj<</Length %d>>stream\n" % len(cmap) + cmap + b"\nendstream\nendobj")
    xref_at = len(body)
    body.extend(b"xref\n0 7\n0000000000 65535 f \n")
    for offset in offsets:
        body.extend(b"%010d 00000 n \n" % offset)
    body.extend(b"trailer<</Size 7/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % xref_at)
    overlay = pymupdf.open(stream=bytes(body), filetype="pdf")
    try:
        page.show_pdf_page(page.rect, overlay, 0, keep_proportion=False)
    finally:
        overlay.close()


def _verify(path: Path, sha256: str, report_year: int = 2024, document_id: str = "doc-haitian-2024"):
    return verify_annual_comparability(
        path,
        document_id=document_id,
        source_sha256=sha256,
        report_year=report_year,
    )


def _complete_opening_and_restatement(document, report_year: int = 2024) -> None:
    page = document.new_page()
    _header(page, report_year)
    causes = (
        "1、由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润0元。",
        "2、由于会计政策变更，影响期初未分配利润0元。",
        "3、由于重大会计差错更正，影响期初未分配利润0元。",
        "4、由于同一控制导致的合并范围变更，影响期初未分配利润0元。",
        "5、其他调整合计影响期初未分配利润0元。",
    )
    for index, line in enumerate(causes):
        _text(page, 80, 100 + index * 22, line, fontsize=9)

    page = document.new_page()
    _header(page, report_year)
    _text(page, 80, 620, "十八、其他重要事项")
    _text(page, 80, 640, "1、前期会计差错更正")
    _text(page, 80, 660, "(1).追溯重述法")
    _text(page, 80, 680, "□适用   √不适用")


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


def test_standard_full_report_header_and_split_checkbox_can_verify(tmp_path: Path) -> None:
    pdf = tmp_path / "full-header-split.pdf"
    sha256 = _pdf(
        pdf,
        report_year=2025,
        header_style="年度报告全文",
        company="贵州茅台酒股份有限公司",
        split_checkbox=True,
    )

    checked = _verify(pdf, sha256, report_year=2025, document_id="doc-moutai-2025")

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.document_id == "doc-moutai-2025"
    assert all("年度报告全文" in item.report_year_text for item in checked.evidence)


def test_wingdings_checkbox_and_yes_no_restatement_can_verify(tmp_path: Path) -> None:
    pdf = tmp_path / "yes-no-wingdings.pdf"
    sha256 = _pdf(
        pdf,
        report_year=2024,
        header_style="年度报告全文",
        company="宜宾五粮液股份有限公司",
        checkbox="wingdings_not_applicable",
        use_yes_no_restatement=True,
    )

    checked = _verify(pdf, sha256, document_id="doc-wuliangye-2024")

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.document_id == "doc-wuliangye-2024"
    assert any(item.topic == "prior_error_retrospective_restatement" for item in checked.evidence)


def test_policy_applicable_with_explicit_no_impact_is_not_a_conflict(tmp_path: Path) -> None:
    pdf = tmp_path / "policy-no-impact.pdf"
    sha256 = _pdf(pdf, policy_applicable_with_no_impact=True)

    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.conflicts == ()
    assert any("无影响" in item for item in checked.checks)


def test_policy_applicable_without_no_impact_stays_insufficient(tmp_path: Path) -> None:
    pdf = tmp_path / "policy-applicable.pdf"
    sha256 = _pdf(pdf, policy_applicable_without_impact=True)

    checked = _verify(pdf, sha256)

    assert checked.status == "insufficient_evidence"
    assert checked.restatement_status == "unknown"
    assert checked.conflicts == ()
    assert any("适用" in item and "无影响" in item for item in checked.limitations)


def test_blank_opening_amounts_emit_specific_gaps(tmp_path: Path) -> None:
    pdf = tmp_path / "blank-opening-amounts.pdf"
    sha256 = _pdf(pdf, opening_amount="")

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"
    for cause in _OPENING_CAUSES:
        assert any(cause in item for item in checked.limitations)


def test_missing_opening_amounts_emit_specific_gaps(tmp_path: Path) -> None:
    pdf = tmp_path / "missing-opening.pdf"
    sha256 = _pdf(pdf, include_opening=False)

    checked = _verify(pdf, sha256, document_id="doc-600519-2025")

    assert checked.status == "insufficient_evidence"
    assert checked.document_id == "doc-600519-2025"
    for cause in _OPENING_CAUSES:
        assert any(cause in item for item in checked.limitations)


def test_empty_opening_total_is_not_treated_as_zero(tmp_path: Path) -> None:
    pdf = tmp_path / "empty-opening-total.pdf"
    sha256 = _pdf(pdf, include_opening=False, unexplained_opening_total="")

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert not any("金额为 0 元" in item for item in checked.checks)
    for cause in _OPENING_CAUSES:
        assert any(cause in item for item in checked.limitations)


def test_unexplained_nonzero_opening_total_blocks_proof(tmp_path: Path) -> None:
    pdf = tmp_path / "opening-total.pdf"
    sha256 = _pdf(pdf, unexplained_opening_total="2,554,581.61")

    checked = _verify(pdf, sha256)

    assert checked.status == "insufficient_evidence"
    assert any("调整期初未分配利润合计" in item for item in checked.limitations)


def test_combined_sse_headings_on_later_page_can_verify(tmp_path: Path) -> None:
    pdf = tmp_path / "combined-headings.pdf"
    document = pymupdf.open()
    cover = document.new_page()
    _header(cover, 2025, company="贵州茅台酒股份有限公司")
    _text(cover, 80, 120, "封面页不承载会计政策勾选。")

    page = document.new_page()
    _header(page, 2025, company="贵州茅台酒股份有限公司")
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "□适用")
    _text(page, 150, 110, "√不适用")
    _text(page, 80, 150, "（二）公司对重大会计差错更正原因及影响的分析说明")
    _text(page, 80, 170, "□适用 √不适用")
    _text(page, 80, 210, "26、2025年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 230, "□适用√不适用")

    opening = document.new_page()
    _header(opening, 2025, company="贵州茅台酒股份有限公司")
    for index, cause in enumerate(
        (
            "1、由于《企业会计准则》及其相关新规定进行追溯调整，影响期初未分配利润0元。",
            "2、由于会计政策变更，影响期初未分配利润0元。",
            "3、由于重大会计差错更正，影响期初未分配利润0元。",
            "4、由于同一控制导致的合并范围变更，影响期初未分配利润0元。",
            "5、其他调整合计影响期初未分配利润0元。",
        )
    ):
        _text(opening, 80, 100 + index * 22, cause, fontsize=9)

    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256, report_year=2025, document_id="upload-demo-2025")

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.document_id == "upload-demo-2025"
    pages = {item.topic: item.pdf_page for item in checked.evidence}
    assert pages["major_accounting_policy_change"] == 2
    assert pages["opening_retained_earnings_adjustments_zero"] == 3


def test_wrong_year_header_does_not_bind_evidence(tmp_path: Path) -> None:
    pdf = tmp_path / "wrong-year-header.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024, company="贵州茅台酒股份有限公司")
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "□适用 √不适用")
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256, report_year=2025, document_id="doc-600519")

    assert checked.status == "insufficient_evidence"
    assert checked.evidence == () or all(item.report_year == 2025 for item in checked.evidence)
    assert any("抬头" in item or "未找到" in item or "期初未分配利润" in item for item in checked.limitations)


def test_company_id_like_document_id_does_not_grant_proof(tmp_path: Path) -> None:
    pdf = tmp_path / "id-is-not-proof.pdf"
    sha256 = _pdf(pdf, complete=False)

    checked = _verify(pdf, sha256, document_id="600519")

    assert checked.status == "insufficient_evidence"
    assert checked.document_id == "600519"
    assert checked.restatement_status == "unknown"


def test_policy_does_not_borrow_adjacent_estimate_checkbox(tmp_path: Path) -> None:
    pdf = tmp_path / "borrow-estimate-choice.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 128, "(2).重要会计估计变更")
    _text(page, 80, 145, "□适用 √不适用")
    _text(page, 80, 230, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 250, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_contradictory_checkbox_groups_fail_closed(tmp_path: Path) -> None:
    pdf = tmp_path / "mixed-checkboxes.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "□适用√不适用  √适用□不适用")
    _text(page, 80, 170, "(2).重要会计估计变更")
    _text(page, 80, 190, "□适用 √不适用")
    _text(page, 80, 230, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 250, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_other_topic_no_impact_does_not_confirm_policy(tmp_path: Path) -> None:
    pdf = tmp_path / "other-topic-no-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    _text(page, 80, 148, "另，本期会计估计变更未对本公司的财务报表产生影响。")
    _text(page, 80, 190, "(2).重要会计估计变更")
    _text(page, 80, 210, "□适用 √不适用")
    _text(page, 80, 250, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 270, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


@pytest.mark.parametrize("total", ["2554581", "-2554581"])
def test_ungrouped_integer_opening_total_blocks_proof(tmp_path: Path, total: str) -> None:
    pdf = tmp_path / f"ungrouped-total-{total.lstrip('-')}.pdf"
    sha256 = _pdf(pdf, unexplained_opening_total=total)

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_year_like_opening_total_blocks_proof(tmp_path: Path) -> None:
    pdf = tmp_path / "year-like-total.pdf"
    sha256 = _pdf(pdf, unexplained_opening_total="2024")

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_note_adjacent_opening_total_blocks_proof(tmp_path: Path) -> None:
    pdf = tmp_path / "note-adjacent-total.pdf"
    sha256 = _pdf(pdf, unexplained_opening_total="5", opening_total_note="附注5")

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_same_line_other_topic_no_impact_does_not_confirm_policy(tmp_path: Path) -> None:
    pdf = tmp_path / "same-line-mixed-no-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    _text(page, 80, 148, "本期会计政策变更情况；本期会计估计变更未对本公司的财务报表产生影响。")
    _text(page, 80, 190, "(2).重要会计估计变更")
    _text(page, 80, 210, "□适用 √不适用")
    _text(page, 80, 250, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 270, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


@pytest.mark.parametrize(
    "heading",
    ["1、其他事项", "（一）其他事项", "（1）其他事项", "(1)其他事项", "1.其他事项", "1.1其他事项", "1.1.1其他事项"],
)
def test_numbered_other_matter_checkbox_is_not_borrowed(tmp_path: Path, heading: str) -> None:
    pdf = tmp_path / "numbered-other-matter.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 128, heading)
    _text(page, 80, 145, "□适用 √不适用")
    _text(page, 80, 190, "(2).重要会计估计变更")
    _text(page, 80, 210, "□适用 √不适用")
    _text(page, 80, 250, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 270, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_cross_line_other_matter_no_impact_does_not_confirm_policy(tmp_path: Path) -> None:
    pdf = tmp_path / "cross-line-other-matter-no-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    _text(page, 80, 148, "本期会计政策变更情况")
    _text(page, 80, 166, "另，本期处置设备未对本公司的财务报表产生影响。")
    _text(page, 80, 210, "(2).重要会计估计变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 290, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_policy_no_impact_line_wrap_can_verify(tmp_path: Path) -> None:
    pdf = tmp_path / "policy-no-impact-wrap.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    _text(page, 80, 148, "本次会计政策变更")
    _text(page, 80, 166, "未对本公司的财务报表产生影响。")
    _text(page, 80, 210, "(2).重要会计估计变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 290, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.conflicts == ()
    assert any("无影响" in item for item in checked.checks)


def test_no_material_impact_does_not_confirm_policy(tmp_path: Path) -> None:
    pdf = tmp_path / "no-material-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    _text(page, 80, 148, "本次会计政策变更对本公司财务报表无重大影响。")
    _text(page, 80, 190, "(2).重要会计估计变更")
    _text(page, 80, 210, "□适用 √不适用")
    _text(page, 80, 250, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 270, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


@pytest.mark.parametrize(
    "statement",
    [
        "尚不能确认会计政策变更未对本公司的财务报表产生影响。",
        "若会计政策变更未对本公司的财务报表产生影响，则不调整比较数据。",
    ],
)
def test_unconfirmed_or_conditional_no_impact_does_not_confirm_policy(
    tmp_path: Path, statement: str
) -> None:
    pdf = tmp_path / "unconfirmed-or-conditional-no-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    _text(page, 80, 148, statement)
    _text(page, 80, 190, "(2).重要会计估计变更")
    _text(page, 80, 210, "□适用 √不适用")
    _text(page, 80, 250, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 270, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


@pytest.mark.parametrize(
    "statements",
    [
        ("本期会计政策变更未对本公司财务报表产生影响；本期会计政策变更仍对比较数据产生影响。",),
        (
            "本期会计政策变更未对本公司财务报表产生影响；",
            "本期会计政策变更仍对比较数据产生影响。",
        ),
    ],
)
def test_later_clause_comparative_impact_does_not_confirm_policy(
    tmp_path: Path, statements: tuple[str, ...]
) -> None:
    pdf = tmp_path / "later-clause-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    for index, statement in enumerate(statements):
        _text(page, 80, 148 + index * 18, statement)
    _text(page, 80, 210, "(2).重要会计估计变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 290, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_policy_no_impact_may_omit_company_entity(tmp_path: Path) -> None:
    pdf = tmp_path / "policy-no-impact-omit-entity.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "√适用 □不适用")
    _text(page, 80, 148, "本次会计政策变更对财务报表无影响。")
    _text(page, 80, 190, "(2).重要会计估计变更")
    _text(page, 80, 210, "□适用 √不适用")
    _text(page, 80, 250, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 270, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.conflicts == ()
    assert any("无影响" in item for item in checked.checks)


def test_unconfirmed_general_restatement_topic_blocks_proof(tmp_path: Path) -> None:
    pdf = tmp_path / "unconfirmed-general-restatement.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 80, 130, "□适用 √不适用")
    _text(page, 80, 170, "(2).重要会计估计变更")
    _text(page, 80, 190, "□适用 √不适用")
    _text(page, 80, 230, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 250, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    extra = document.new_page()
    _header(extra, 2024)
    _text(extra, 80, 110, "是否需追溯调整或重述以前年度会计数据")
    _text(extra, 80, 130, "√适用 □不适用")
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"
    assert any("追溯调整或重述以前年度会计数据" in item and "适用" in item for item in checked.limitations)


def test_combined_heading_uniform_no_impact_can_verify(tmp_path: Path) -> None:
    pdf = tmp_path / "combined-uniform-no-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "√适用 □不适用")
    _text(page, 80, 128, "会计政策变更及会计估计变更均未对本公司财务报表产生影响")
    _text(page, 80, 210, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 230, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    topics = {item.topic for item in checked.evidence}
    assert "major_accounting_policy_change" in topics
    assert "major_accounting_estimate_change" in topics
    assert any("无影响" in item for item in checked.checks)


def test_combined_heading_mixed_topics_without_uniform_predicate_abstains(tmp_path: Path) -> None:
    pdf = tmp_path / "combined-mixed-no-uniform.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "√适用 □不适用")
    _text(page, 80, 128, "会计政策变更情况，会计估计变更未对本公司财务报表产生影响")
    _text(page, 80, 210, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 230, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"


def test_sse_analysis_applicable_does_not_override_major_not_applicable(tmp_path: Path) -> None:
    pdf = tmp_path / "summary-vs-major.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "√适用 □不适用")
    _text(page, 80, 128, "采用上述规定未对本集团的财务状况及经营成果产生重大影响。")
    _text(page, 80, 210, "(1).重要会计政策变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "(2).重要会计估计变更")
    _text(page, 80, 290, "□适用 √不适用")
    _text(page, 80, 330, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 350, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    evidence_by_topic = {item.topic: item for item in checked.evidence}
    assert "重要会计政策变更" in evidence_by_topic["major_accounting_policy_change"].text
    assert "分析说明" not in evidence_by_topic["major_accounting_policy_change"].text
    assert "重要会计估计变更" in evidence_by_topic["major_accounting_estimate_change"].text
    assert "分析说明" not in evidence_by_topic["major_accounting_estimate_change"].text


def test_sse_analysis_immaterial_impact_is_not_no_impact(tmp_path: Path) -> None:
    pdf = tmp_path / "summary-immaterial.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "√适用 □不适用")
    _text(page, 80, 128, "采用上述规定未对本集团的财务状况及经营成果产生重大影响。")
    _text(page, 80, 210, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 230, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"
    assert any("重要会计政策变更" in item and "适用" in item for item in checked.limitations)
    assert any("重要会计估计变更" in item and "适用" in item for item in checked.limitations)


def test_sse_analysis_not_applicable_cannot_wipe_major_applicable(tmp_path: Path) -> None:
    pdf = tmp_path / "summary-cannot-wipe-major.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "□适用 √不适用")
    _text(page, 80, 150, "(1).重要会计政策变更")
    _text(page, 80, 170, "√适用 □不适用")
    _text(page, 80, 210, "(2).重要会计估计变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 290, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "insufficient_evidence"
    assert checked.restatement_status == "unknown"
    assert any("重要会计政策变更" in item and "适用" in item for item in checked.limitations)


@pytest.mark.parametrize(
    "statement",
    [
        "本集团本期会计政策变更对比较数据产生影响。",
        "本期会计政策变更仍对比较数据产生影响。",
        "本期会计政策变更对比较数据产生重大影响。",
        "本期会计政策变更对比较数据产生了重大影响。",
        "本期会计政策变更对比较数据有影响。",
        "本期会计政策变更对比较数据有重大影响。",
        "本期会计政策变更对比较数据进行了调整。",
    ],
)
def test_sse_analysis_explicit_comparative_impact_blocks_proof(tmp_path: Path, statement: str) -> None:
    pdf = tmp_path / "summary-explicit-comparative-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "√适用 □不适用")
    _text(page, 80, 128, statement)
    _text(page, 80, 210, "(1).重要会计政策变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "(2).重要会计估计变更")
    _text(page, 80, 290, "□适用 √不适用")
    _text(page, 80, 330, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 350, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "conflict"
    assert checked.restatement_status == "unknown"
    assert any("比较数据" in item and "影响" in item for item in checked.conflicts)
    assert any(statement in item.text for item in checked.evidence)


def test_sse_analysis_no_comparative_impact_does_not_conflict(tmp_path: Path) -> None:
    pdf = tmp_path / "summary-no-comparative-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "√适用 □不适用")
    _text(page, 80, 128, "本期会计政策变更未对比较数据产生影响。")
    _text(page, 80, 210, "(1).重要会计政策变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "(2).重要会计估计变更")
    _text(page, 80, 290, "□适用 √不适用")
    _text(page, 80, 330, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 350, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "verified", (checked.limitations, checked.conflicts)
    assert checked.restatement_status == "not_restated"
    assert checked.conflicts == ()
    evidence_by_topic = {item.topic: item for item in checked.evidence}
    assert "重要会计政策变更" in evidence_by_topic["major_accounting_policy_change"].text
    assert "对比较数据产生影响" not in evidence_by_topic["major_accounting_policy_change"].text


@pytest.mark.parametrize("checkbox", ["not_applicable", "no"])
def test_not_applicable_or_no_checkbox_cannot_drop_body_comparative_impact(
    tmp_path: Path, checkbox: str
) -> None:
    pdf = tmp_path / f"checkbox-{checkbox}-body-impact.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    if checkbox == "not_applicable":
        statement = "本期会计政策变更对比较数据产生影响。"
        _text(page, 80, 110, "(1).重要会计政策变更")
        _text(page, 80, 130, "□适用 √不适用")
        _text(page, 80, 148, statement)
        _text(page, 80, 190, "(2).重要会计估计变更")
        _text(page, 80, 210, "□适用 √不适用")
        _text(page, 80, 250, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
        _text(page, 80, 270, "□适用 √不适用")
        _complete_opening_and_restatement(document)
    else:
        statement = "本期追溯调整或重述对比较数据产生影响。"
        _text(page, 80, 110, "(1).重要会计政策变更")
        _text(page, 80, 130, "□适用 √不适用")
        _text(page, 80, 170, "(2).重要会计估计变更")
        _text(page, 80, 190, "□适用 √不适用")
        _text(page, 80, 230, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
        _text(page, 80, 250, "□适用 √不适用")
        _complete_opening_and_restatement(document)
        extra = document.new_page()
        _header(extra, 2024)
        _text(extra, 80, 110, "是否需追溯调整或重述以前年度会计数据")
        _text(extra, 80, 130, "□是 √否")
        _text(extra, 80, 148, statement)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "conflict"
    assert checked.restatement_status == "unknown"
    assert any("比较数据" in item and "影响" in item for item in checked.conflicts)
    assert any(statement in item.text for item in checked.evidence)


@pytest.mark.parametrize(
    "statement",
    [
        "本期会计政策变更可能对比较数据产生影响。",
        "本期会计政策变更涉及对比较数据的调整。",
        "若会计政策变更对比较数据产生影响，则调整比较报表。",
        "本期会计政策变更未对比较数据产生重大影响。",
        "本期会计政策变更及会计估计变更对比较数据产生影响。",
        "本期会计政策变更及会计估计变更未对比较数据产生影响。",
    ],
)
def test_sse_analysis_unresolved_comparative_effect_is_not_overridden_by_major(
    tmp_path: Path, statement: str
) -> None:
    pdf = tmp_path / "summary-unresolved-comparative.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "√适用 □不适用")
    _text(page, 80, 128, statement)
    _text(page, 80, 210, "(1).重要会计政策变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "(2).重要会计估计变更")
    _text(page, 80, 290, "□适用 √不适用")
    _text(page, 80, 330, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 350, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status == "insufficient_evidence"
    assert checked.restatement_status == "unknown"
    assert checked.conflicts == ()
    assert any("比较数据" in item or "比较报表" in item or "以前年度会计数据" in item for item in checked.limitations)
    assert any(statement in item.text for item in checked.evidence)


def test_sse_analysis_choice_conflict_blocks_major(tmp_path: Path) -> None:
    pdf = tmp_path / "summary-choice-conflict.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 80, 110, "□适用√不适用  √适用□不适用")
    _text(page, 80, 210, "(1).重要会计政策变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "(2).重要会计估计变更")
    _text(page, 80, 290, "□适用 √不适用")
    _text(page, 80, 330, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 350, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"
    assert any("相互矛盾" in item or "无法唯一归属" in item for item in checked.conflicts)


def test_sse_analysis_association_ambiguous_blocks_major(tmp_path: Path) -> None:
    pdf = tmp_path / "summary-association-ambiguous.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 330, 90, "（二）重大会计差错更正原因及影响")
    _text(page, 80, 110, "□适用 √不适用")
    _text(page, 80, 150, "(1).重要会计政策变更")
    _text(page, 80, 170, "□适用 √不适用")
    _text(page, 80, 210, "(2).重要会计估计变更")
    _text(page, 80, 230, "□适用 √不适用")
    _text(page, 80, 270, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 290, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"
    assert any("分立标题" in item and "无法唯一关联" in item for item in checked.limitations)


@pytest.mark.parametrize("extra_estimate_not_applicable", [False, True])
def test_side_by_side_separate_headings_cannot_share_one_checkbox(
    tmp_path: Path, extra_estimate_not_applicable: bool
) -> None:
    pdf = tmp_path / f"side-by-side-headings-{extra_estimate_not_applicable}.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 110, "(1).重要会计政策变更")
    _text(page, 330, 110, "(2).重要会计估计变更")
    _text(page, 80, 130, "□适用√不适用")
    if extra_estimate_not_applicable:
        _text(page, 80, 170, "(2).重要会计估计变更")
        _text(page, 80, 190, "□适用 √不适用")
    _text(page, 80, 230, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 250, "□适用 √不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"
    assert any(
        "重要会计政策变更" in item and "分立标题" in item and "无法唯一关联" in item
        for item in checked.limitations
    )
    assert any(
        "重要会计估计变更" in item and "分立标题" in item and "无法唯一关联" in item
        for item in checked.limitations
    )


def test_combined_heading_beside_separate_numbered_topic_is_ambiguous(tmp_path: Path) -> None:
    pdf = tmp_path / "combined-beside-separate.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _header(page, 2024)
    _text(page, 80, 90, "（一）公司对会计政策、会计估计变更原因及影响的分析说明")
    _text(page, 330, 90, "(2).重要会计估计变更")
    _text(page, 80, 110, "□适用 √不适用")
    _text(page, 80, 210, "2024年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表")
    _text(page, 80, 230, "□适用√不适用")
    _complete_opening_and_restatement(document)
    document.save(pdf)
    document.close()
    sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    checked = _verify(pdf, sha256)

    assert checked.status != "verified"
    assert checked.restatement_status == "unknown"
    assert any("分立标题" in item and "无法唯一关联" in item for item in checked.limitations)


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
