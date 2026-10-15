from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pymupdf
import pytest

from finagent.agents.annual_issue_pipeline import (
    _supplement_input,
    _validate_pipeline_retrieval,
    _validate_pipeline_supplements,
    _wire_body,
)
from finagent.agents.annual_review import _review_pipeline_semantics
from finagent.agents.annual_review_wire import decode_annual_issue_input_wire
from finagent.api.annual_report_read import AnnualReportReadError, SourceReadContext, _validate_issue_supplement_locations
from finagent.api.annual_report_export import _project_issue_pipeline
from finagent.ingestion.parse_text_pdf import parse_text_pdf
from finagent.retrieval.annual_issue_supplements import (
    LEGACY_RETRIEVAL_VERSION,
    RETRIEVAL_VERSION,
    retrieve_annual_issue_supplements,
)
from finagent.schemas.text_pdf import ParsedTextPdf, PdfBBox, PdfPageText, PdfTextBlock


SOURCE = {
    "run_id": "annual-analysis-source-context-fixture",
    "company_id": "609821", "report_year": 2031, "report_period": "2031-12-31",
    "source_document_id": "constructed-context-pdf", "source_sha256": "d" * 64,
}


def _parsed(pages: dict[int, list[PdfTextBlock]]) -> ParsedTextPdf:
    return ParsedTextPdf(
        document_id=SOURCE["source_document_id"], source_sha256=SOURCE["source_sha256"],
        source_filename="constructed-context.pdf", page_count=max(pages),
        coordinate_system="pymupdf_page_top_left", coordinate_unit="pdf_point",
        coordinate_note="Constructed source selection regression, not financial verification",
        ocr_applied=False, extractor="constructed", extractor_version="1",
        pages=tuple(PdfPageText(
            page_number=number, width=700.0, height=900.0, rotation=0,
            status="extracted", image_block_count=0, limitation=None,
            blocks=tuple(pages.get(number, [])),
        ) for number in range(1, max(pages) + 1)),
    )


def _block(index: int, text: str, x: float = 30, y: float = 300, width: float = 250) -> PdfTextBlock:
    return PdfTextBlock(index, text, PdfBBox(x, y, x + width, y + 12))


def _issue(page: int, block: PdfTextBlock, *, quote: str | None = None, source: dict = SOURCE) -> dict:
    return {
        "issue_id": "issue:disclosed-change", "member_finding_ids": ["finding:original"],
        "title": "资产变动待核查",
        "observation": "原文披露资产变动。",
        # Long generalized model wording deliberately squeezed the short source
        # metric out of v1's forty longest/lexicographically sorted ngrams.
        "why_it_matters": "会计处理政策测试报告判断偏差经营安排交易过程" * 20,
        "follow_up": "核对会计处理及相关政策安排。",
        "citations": [{
            "evidence_id": "source:original", "quote": block.text if quote is None else quote,
            "source_document_id": source["source_document_id"], "source_sha256": source["source_sha256"],
            "pdf_page": page, "block_index": block.block_index, "start_char": 0,
            "bbox": asdict(block.bbox),
        }],
    }


def _assert_reconstructable(parsed: ParsedTextPdf, items: list[dict], coverage: dict) -> None:
    blocks = {(page.page_number, block.block_index): block for page in parsed.pages for block in page.blocks}
    for item in items:
        block = blocks[item["pdf_page"], item["block_index"]]
        assert block.text[item["start_char"]:item["end_char"]] == item["raw_text"]
        assert asdict(block.bbox) == item["bbox"]
        assert len(item["raw_text"]) <= 500
        assert item["source_sha256"] == parsed.source_sha256
        assert item["independent_verification"] is False
    assert coverage["scanned_chars"] == sum(len(block.text) for block in blocks.values())
    assert coverage["selected_excerpt_chars"] == sum(len(item["raw_text"]) for item in items)
    assert coverage["omitted_chars"] == coverage["scanned_chars"] - coverage["selected_unique_source_chars"]
    assert coverage["matched_issue_block_pairs"] == coverage["selected_issue_block_pairs"] + coverage["unselected_issue_block_pairs"]
    _validate_pipeline_retrieval(coverage, SOURCE, items)


@pytest.mark.parametrize("page,entity", [(3, "青枫有限公司"), (9, "星湾有限公司"), (14, "远松有限公司")])
def test_source_short_metric_and_two_right_column_reasons_reach_real_consumers(page: int, entity: str) -> None:
    row = _block(1, f"{entity} 2031年度 商誉 123.45 234.56", width=260)
    reason_a = _block(2, "主要是本期对收购形成的商誉", x=310, y=300, width=310)
    reason_b = _block(3, "计提减值所致，公司披露销售价格下降。", x=310, y=314, width=310)
    note = _block(1, f"{entity} 2031年度 商誉减值计提 111.11，本期金额。", y=120)
    note_reason = _block(2, f"{entity} 2031年度 商誉由于销售价格下滑计提减值。", y=180)
    parsed = _parsed({
        1: [_block(0, "财务报表附注", y=30), *[
            _block(index, "会计处理政策测试报告判断偏差经营安排交易过程 商誉减值测试报告", y=100 + index * 25)
            for index in range(1, 9)
        ]],
        page: [row, reason_a, reason_b], page + 2: [note, note_reason],
    })
    issue = _issue(page, row)
    before = copy.deepcopy(issue)
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, SOURCE)
    assert {(item["pdf_page"], item["block_index"]) for item in items} == {
        (page, 2), (page, 3), (page + 2, 1), (page + 2, 2),
    }
    _assert_reconstructable(parsed, items, coverage)
    assert issue == before  # Retrieval cannot silently rewrite old discovery prose.
    legacy, _ = retrieve_annual_issue_supplements([issue], parsed, SOURCE, retrieval_version=LEGACY_RETRIEVAL_VERSION)
    assert not any(item["pdf_page"] == page for item in legacy)

    body_input = _supplement_input([issue], items, SOURCE)
    body = json.loads(_wire_body("local-fixture-model", "constructed supplement consumer", body_input))
    decoded = decode_annual_issue_input_wire(json.loads(body["messages"][1]["content"]))
    assert decoded == body_input
    sent = {item["raw_text"] for item in decoded["supplements"]}
    assert reason_a.text in sent and reason_b.text in sent and note.text in sent
    assert decoded["issues"][0]["primary_finding_quotes"][0]["quote"] == row.text
    pipeline = {
        "kind": "annual_issue_pipeline_v1", "status": "partial", "reason": "fixture",
        "source_identity": SOURCE, "input": {"finding_ids": ["finding:original"]},
        "candidates": [{"finding_id": "finding:original", "citations": issue["citations"]}],
        "issues": [issue], "member_decisions": [], "supplements": items,
        "retrieval_coverage": coverage, "shards": [], "global_closure": {},
    }
    review = _review_pipeline_semantics(pipeline)
    assert review is not None
    assert {item["raw_text"] for item in review["supplement_pool"]} == sent
    assert review["candidate_pool"][0]["citations"] == issue["citations"]


def test_generic_cause_peer_rows_cannot_evict_metric_and_subject_notes() -> None:
    row = _block(1, "商誉 112,937,041.73 210,428,426.24 -46.33", width=260)
    cause = _block(2, "主要是本期对收购青枫形成的商誉", x=310, width=310)
    continuation = _block(3, "计提减值所致", x=310, y=314, width=310)
    note = _block(1, "收购青枫及其子公司\n97,491,384.51\n97,491,384.51", y=120)
    explanation = _block(1, "青枫所处市场发生不利变化，产品销售价格及毛利下滑。", y=120)
    parsed = _parsed({
        1: [_block(0, "合并资产负债表", y=20)],
        8: [row, cause, continuation,
            _block(4, "应交税费 414,104,240.03 主要是本期应交所得税增加所致", y=420),
            _block(5, "长期借款 33,200,000.00 主要是本期归还长期借款所致", y=440)],
        12: [_block(1, "商誉 七、27 112,937,041.73 210,428,426.24", y=120)],
        16: [_block(0, "财务报表附注\n27、商誉减值准备", y=20), note], 19: [explanation],
        24: [_block(1, "十一、商誉减值损失\n97,491,384.51", y=120)],
        25: [_block(1, "非流动资产处置损益，包括已计提资产减值准备的冲销部分\n8,865,327.35", y=120)],
        26: [_block(1, "2、长期股权投资\n青枫 592,808,000.00 97,491,384.51 502,508,615.49", y=120)],
    })
    issue = _issue(8, row)
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, SOURCE)
    assert {(item["pdf_page"], item["block_index"]) for item in items} == {(8, 2), (8, 3), (16, 1), (19, 1)}
    _assert_reconstructable(parsed, items, coverage)
    _assert_context_consumers(issue, items, coverage)


def test_complete_cited_raw_and_adjacent_table_headers_preserve_subject_period_and_direction() -> None:
    row = _block(2, "青枫有限公司 2031年度 吸收关联方存款 456.78。\n该项属于财务子公司负债。")
    header = _block(1, "项目 本期期末 上期期末 单位：元 负债", y=277)
    explanation = _block(3, "公司披露余额增加主要由于关联方存款增加。", y=325)
    wrong_subject = _block(4, "星湾有限公司 2031年度 吸收关联方存款主要由于业务变化。", x=310, y=300)
    wrong_period = _block(5, "青枫有限公司 2027年度 吸收关联方存款主要由于业务变化。", x=310, y=314)
    parsed = _parsed({1: [_block(0, "财务报表附注", y=30)], 7: [header, row, explanation, wrong_subject, wrong_period]})
    issue = _issue(7, row, quote="吸收关联方存款 456.78。")
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, SOURCE)
    texts = {item["raw_text"] for item in items}
    assert row.text in texts and header.text in texts and explanation.text in texts
    assert wrong_subject.text not in texts and wrong_period.text not in texts
    _assert_reconstructable(parsed, items, coverage)


def test_inventory_following_disclosure_is_not_replaced_by_wrong_page_generalized_risk() -> None:
    row = _block(1, "澄星产品 库存 53,529 44.69%", y=300)
    explanation = _block(2, "公司产品库存同比上升，主要是春节销售旺季", y=325)
    continuation = _block(3, "增加备货所致。", y=340)
    parsed = _parsed({
        1: [_block(0, "管理层讨论与分析", y=20), _block(1, "判断偏差 会计处理政策测试报告判断偏差", y=200)],
        6: [row, explanation, continuation],
    })
    items, coverage = retrieve_annual_issue_supplements([_issue(6, row)], parsed, SOURCE)
    texts = {item["raw_text"] for item in items}
    assert explanation.text in texts and continuation.text in texts
    assert not any(item["pdf_page"] == 1 for item in items)
    _assert_reconstructable(parsed, items, coverage)


@pytest.mark.parametrize("tamper", ["document", "sha", "quote", "offset", "version"])
def test_anchor_retrieval_rejects_unreconstructable_source_and_unknown_versions(tamper: str) -> None:
    row = _block(1, "商誉 123.45")
    parsed = _parsed({1: [row]})
    source = dict(SOURCE)
    issue = _issue(1, row)
    version = RETRIEVAL_VERSION
    if tamper == "document":
        source["source_document_id"] = "other-document"
    elif tamper == "sha":
        issue["citations"][0]["source_sha256"] = "e" * 64
    elif tamper == "quote":
        issue["citations"][0]["quote"] = "拼接的不存在的原文"
    elif tamper == "offset":
        issue["citations"][0]["start_char"] = len(row.text)
    else:
        version = "unknown-retrieval"
    with pytest.raises(ValueError, match="annual_issue_retrieval_"):
        retrieve_annual_issue_supplements([issue], parsed, source, retrieval_version=version)


@pytest.mark.parametrize("version", [5, 6, 7, 8])
@pytest.mark.parametrize("tamper", [None, "text", "coverage", "order", "algorithm", "pdf"])
def test_archive_consumer_rebuilds_versioned_selection_from_original_pdf(tmp_path: Path, version: int, tamper: str | None) -> None:
    run_id = SOURCE["run_id"]
    run_dir = tmp_path / "artifacts" / "runs" / run_id
    run_dir.mkdir(parents=True)
    pdf = pymupdf.open()
    page = pdf.new_page(width=700, height=900)
    page.insert_text((30, 40), "财务报表附注", fontname="china-s", fontsize=10)
    page.insert_text((30, 150), "青枫有限公司 商誉 123.45 234.56", fontname="china-s", fontsize=10)
    page.insert_text((360, 150), "主要是本期计提商誉减值所致。", fontname="china-s", fontsize=10)
    page.insert_text((30, 300), "青枫有限公司 商誉减值本期计提 111.11。", fontname="china-s", fontsize=10)
    page.insert_text((30, 400), "青枫有限公司 商誉减值主要由于销售价格下滑。", fontname="china-s", fontsize=10)
    path = run_dir / "source.pdf"
    pdf.save(path)
    pdf.close()
    parsed = parse_text_pdf(path, SOURCE["source_document_id"])
    source = dict(SOURCE, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    row = next(block for block in parsed.pages[0].blocks if "123.45" in block.text)
    issue = _issue(1, row, source=source)
    issue.update(title="商誉减值", observation="", why_it_matters="", follow_up="")
    algorithm = RETRIEVAL_VERSION if version == 8 else LEGACY_RETRIEVAL_VERSION
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, source, retrieval_version=algorithm)
    assert len(items) >= 2  # Order tampering must exercise a real multi-item read.
    pipeline = {
        "source_identity": source, "issues": [issue], "supplements": items,
        "retrieval_coverage": coverage,
        "execution_binding": {"kind": f"fintrace_annual_execution_binding_v{version}", "execution_version": f"execution_v{version}"},
    }
    if version == 8:
        pipeline["execution_binding"]["supplement_retrieval_version"] = RETRIEVAL_VERSION
    if tamper == "text":
        items[0]["raw_text"] += "不存在的解释"
    elif tamper == "coverage":
        coverage["omitted_chars"] += 1
    elif tamper == "order":
        items.reverse()
    elif tamper == "algorithm":
        coverage["selection_algorithm_version"] = LEGACY_RETRIEVAL_VERSION if version == 8 else RETRIEVAL_VERSION
    elif tamper == "pdf":
        path.write_bytes(path.read_bytes() + b"\n% altered original source\n")
    source_bytes = path.read_bytes()
    source_read = {
        "source_pdf_bytes": source_bytes,
        "actual_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_filename": path.name,
        "source_context": SourceReadContext(),
    }
    if tamper is None:
        _validate_issue_supplement_locations(pipeline, **source_read)
    else:
        with pytest.raises(AnnualReportReadError) as error:
            _validate_issue_supplement_locations(pipeline, **source_read)
        assert error.value.code == ("annual_issue_pipeline_supplement_invalid" if tamper == "pdf" else "annual_issue_pipeline_retrieval_mismatch")


def test_bounded_excerpt_and_role_tail_have_exact_omitted_character_account() -> None:
    row = _block(1, "商誉 123.45")
    long_reason = _block(2, "主要由于市场变化。" + "真实披露段落" * 180, x=310)
    parsed = _parsed({1: [_block(0, "财务报表附注", y=20), row, long_reason]})
    issues = [dict(_issue(1, row), issue_id=f"issue:{index}") for index in range(40)]
    items, coverage = retrieve_annual_issue_supplements(issues, parsed, SOURCE)
    assert len(items) == 1 and len(items[0]["raw_text"]) == 500
    assert coverage["role_assignments_total"] == 40
    assert coverage["role_assignments_requested"] == 32
    assert coverage["role_assignments_pending"] == 8
    assert coverage["omitted_chars"] == coverage["scanned_chars"] - 500
    assert coverage["complete"] is True  # Scan completion is not context completeness.
    _assert_reconstructable(parsed, items, coverage)


def _assert_context_consumers(issue: dict, items: list[dict], coverage: dict, source: dict = SOURCE) -> None:
    _validate_pipeline_supplements(items, source, {issue["issue_id"]: issue})
    supplement_input = _supplement_input([issue], items, source)
    body = json.loads(_wire_body("local-fixture-model", "constructed source consumer", supplement_input))
    decoded = decode_annual_issue_input_wire(json.loads(body["messages"][1]["content"]))
    assert decoded == supplement_input
    assert sorted(decoded["issues"][0]["primary_finding_quotes"], key=lambda item: (item["evidence_id"], item["quote"])) == sorted(
        [{"evidence_id": item["evidence_id"], "quote": item["quote"]} for item in issue["citations"]],
        key=lambda item: (item["evidence_id"], item["quote"]),
    )
    pipeline = {
        "kind": "annual_issue_pipeline_v1", "status": "partial", "reason": "fixture",
        "source_identity": source, "input": {"finding_ids": ["finding:original"]},
        "candidates": [{"finding_id": "finding:original", "citations": issue["citations"]}],
        "issues": [issue], "member_decisions": [], "supplements": items,
        "retrieval_coverage": coverage, "shards": [], "global_closure": {},
    }
    review = _review_pipeline_semantics(pipeline)
    assert review is not None
    assert review["candidate_pool"][0]["citations"] == issue["citations"]
    assert {item["evidence_id"]: item["raw_text"] for item in review["supplement_pool"]} == {item["evidence_id"]: item["raw_text"] for item in items}
    assert {item["evidence_id"]: (item["section"], item["selection_reason"]) for item in review["supplement_pool"]} == {item["evidence_id"]: (item["section"], item["selection_reason"]) for item in items}
    exported = _project_issue_pipeline(pipeline, dict(source, document_id=source["source_document_id"], sha256=source["source_sha256"]))
    assert exported is not None
    assert exported["retrieval_coverage"] == coverage
    assert {item["evidence_id"]: (item["section"], item["selection_reason"]) for item in exported["supplements"]} == {item["evidence_id"]: (item["section"], item["selection_reason"]) for item in items}


def test_long_raw_prefix_cannot_evict_short_quoted_metric_from_remote_note_consumer() -> None:
    prefix = " ".join(f"rawprefix{chr(97 + index)}" for index in range(26))
    quote = "青枫有限公司 2031年度 商誉 123.45。"
    row = _block(1, prefix * 3 + "\n" + quote + "\n公司披露本期下降主要由于销售价格下滑。")
    note = _block(1, "青枫有限公司 2031年度 商誉减值计提金额 111.11。", y=120)
    parsed = _parsed({3: [row], 18: [_block(0, "财务报表附注", y=20), note]})
    issue = _issue(3, row, quote=quote)
    before = copy.deepcopy(issue)
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, SOURCE)
    assert row.text.index(quote) > 500
    assert any(item["pdf_page"] == 18 and item["raw_text"] == note.text and item["selection_reason"] == "same_source_ngram_context_match" for item in items)
    anchor = next(item for item in items if item["pdf_page"] == 3)
    assert quote in anchor["raw_text"]
    assert "主要由于销售价格下滑" in anchor["raw_text"]
    assert anchor["section"] == "unknown" and anchor["selection_reason"] == "source_anchor_context"
    assert "unknown" in coverage["examined_sections"]
    _assert_reconstructable(parsed, items, coverage)
    _assert_context_consumers(issue, items, coverage)
    assert issue == before


def test_late_multiple_quotes_use_one_continuous_window_without_rewriting_primary_quote() -> None:
    primary = "商誉 123.45，公司披露本期计提减值。"
    secondary = "公司披露有关计量方法的另一处说明。"
    text = "无关的源段落。" * 100 + primary + "主要由于销售价格下滑。" + "相隔的真实原文。" * 100 + secondary
    row = _block(1, text)
    issue = _issue(7, row, quote=primary)
    issue["citations"].append(dict(issue["citations"][0], quote=secondary))
    before = copy.deepcopy(issue)
    parsed = _parsed({7: [row]})
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, SOURCE)
    assert len(items) == 1
    assert text.index(primary) > 500
    assert primary in items[0]["raw_text"] and "主要由于销售价格下滑" in items[0]["raw_text"]
    assert secondary not in items[0]["raw_text"]
    assert items[0]["start_char"] > 0
    _assert_reconstructable(parsed, items, coverage)
    _assert_context_consumers(issue, items, coverage)
    assert issue == before


def test_v2_unknown_section_and_real_selection_reasons_do_not_relabel_legacy_v1() -> None:
    row = _block(1, "青枫有限公司 2031年度 商誉 123.45。公司披露本期变动。")
    explanation = _block(2, "主要由于销售价格下滑计提减值。", y=325)
    note = _block(1, "青枫有限公司 2031年度 商誉减值计提 111.11。", y=120)
    parsed = _parsed({4: [row, explanation], 12: [_block(0, "财务报表附注", y=20), note]})
    issue = _issue(4, row, quote="商誉 123.45。")
    issue.update(title="商誉减值", observation="", why_it_matters="", follow_up="")
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, SOURCE)
    assert {(item["pdf_page"], item["section"], item["selection_reason"]) for item in items} == {
        (4, "unknown", "source_anchor_context"), (4, "unknown", "adjacent_source_context"),
        (12, "notes", "same_source_ngram_context_match"),
    }
    _assert_reconstructable(parsed, items, coverage)
    _assert_context_consumers(issue, items, coverage)
    legacy, legacy_coverage = retrieve_annual_issue_supplements([issue], parsed, SOURCE, retrieval_version=LEGACY_RETRIEVAL_VERSION)
    legacy_bytes = json.dumps((legacy, legacy_coverage), ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert legacy and all(item["section"] == "notes" and item["selection_reason"] == "same_source_keyword_context_match" for item in legacy)
    _validate_pipeline_supplements(legacy, SOURCE, {issue["issue_id"]: issue}, retrieval_version=LEGACY_RETRIEVAL_VERSION)
    _validate_pipeline_retrieval(legacy_coverage, SOURCE, legacy, retrieval_version=LEGACY_RETRIEVAL_VERSION)
    with pytest.raises(ValueError, match="annual_issue_pipeline_supplement_invalid"):
        _validate_pipeline_supplements(items, SOURCE, {issue["issue_id"]: issue}, retrieval_version=LEGACY_RETRIEVAL_VERSION)
    rebuilt = retrieve_annual_issue_supplements([issue], parsed, SOURCE, retrieval_version=LEGACY_RETRIEVAL_VERSION)
    assert json.dumps(rebuilt, ensure_ascii=False, sort_keys=True).encode("utf-8") == legacy_bytes


def test_original_pdf_rebuilds_late_quote_window_and_unknown_source_provenance(tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / "runs" / SOURCE["run_id"]
    run_dir.mkdir(parents=True)
    prefix = " ".join(f"rawprefix{chr(97 + index)}" for index in range(26))
    quote = "青枫有限公司 2031年度 商誉 123.45。"
    pdf = pymupdf.open()
    page = pdf.new_page(width=800, height=900)
    text = prefix * 3 + "\n" + quote + "\n公司披露本期变动主要由于销售价格下滑。"
    assert page.insert_textbox(pymupdf.Rect(30, 40, 770, 800), text, fontname="china-s", fontsize=10) >= 0
    for _index in range(6):
        pdf.new_page(width=800, height=900)
    note_page = pdf.new_page(width=800, height=900)
    note_page.insert_text((30, 40), "财务报表附注", fontname="china-s", fontsize=10)
    note_page.insert_text((30, 200), "青枫有限公司 2031年度 商誉减值计提金额 111.11。", fontname="china-s", fontsize=10)
    path = run_dir / "source.pdf"
    pdf.save(path)
    pdf.close()
    original = path.read_bytes()
    parsed = parse_text_pdf(path, SOURCE["source_document_id"])
    source = dict(SOURCE, source_sha256=hashlib.sha256(original).hexdigest())
    block = next(block for block in parsed.pages[0].blocks if quote in block.text)
    assert block.text.index(quote) > 500
    issue = _issue(1, block, quote=quote, source=source)
    items, coverage = retrieve_annual_issue_supplements([issue], parsed, source)
    anchor = next(item for item in items if item["pdf_page"] == 1)
    assert quote in anchor["raw_text"] and "主要由于销售价格下滑" in anchor["raw_text"]
    assert anchor["section"] == "unknown" and anchor["selection_reason"] == "source_anchor_context"
    assert any(item["pdf_page"] == 8 and "111.11" in item["raw_text"] for item in items)
    for item in items:
        original_block = next(block for block in parsed.pages[item["pdf_page"] - 1].blocks if block.block_index == item["block_index"])
        assert original_block.text[item["start_char"]:item["end_char"]] == item["raw_text"]
        assert len(item["raw_text"]) <= 500
    _validate_pipeline_retrieval(coverage, source, items)
    _assert_context_consumers(issue, items, coverage, source)
    pipeline = {
        "source_identity": source, "issues": [issue], "supplements": items,
        "retrieval_coverage": coverage, "execution_binding": {
            "kind": "fintrace_annual_execution_binding_v8", "execution_version": "execution_v8",
            "supplement_retrieval_version": RETRIEVAL_VERSION,
        },
    }
    source_read = {
        "source_pdf_bytes": original,
        "actual_source_sha256": hashlib.sha256(original).hexdigest(),
        "source_filename": path.name,
        "source_context": SourceReadContext(),
    }
    _validate_issue_supplement_locations(pipeline, **source_read)
    tampered = copy.deepcopy(pipeline)
    tampered["supplements"][0]["section"] = "financial_statements"
    with pytest.raises(AnnualReportReadError, match="页块|原文|关联"):
        _validate_issue_supplement_locations(tampered, **source_read)
    assert path.read_bytes() == original
