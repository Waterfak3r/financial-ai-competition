from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import threading
import urllib.request
import uuid

import pytest
import pymupdf

from finagent.agents import annual_discovery
from finagent.agents.annual_discovery import (
    parse_annual_discovery_response,
    parse_annual_discovery_response_v2,
    parse_annual_discovery_response_v3,
    run_annual_discovery,
)
from finagent.audit import audited_chat
from finagent.core.annual_work_budget import BUDGET_CONTRACT_VERSION
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatCompletion, ModelResponseError
from finagent.retrieval.annual_discovery_context import (
    COMPACT_PLAN_VERSION,
    DiscoveryBatch,
    DiscoveryFragment,
    _batch_aliases,
    annual_discovery_plan_projection,
    build_annual_discovery_context,
)

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def deny_all_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network access is disabled in annual discovery unit tests")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)


def _make_pdf(path: Path, pages: list[str | None], *, image_pages: set[int] | None = None) -> str:
    image_pages = image_pages or set()
    document = pymupdf.open()
    for page_index, text in enumerate(pages, start=1):
        page = document.new_page(width=1200, height=6000)
        if text:
            lines = "\n".join(text[index : index + 100] for index in range(0, len(text), 100))
            page.insert_textbox(
                pymupdf.Rect(40, 40, 1160, 5900),
                lines,
                fontsize=10,
                fontname="china-s",
            )
        if page_index in image_pages:
            page.insert_image(pymupdf.Rect(60, 80, 90, 110), stream=PNG_1X1)
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(path: Path, digest: str, run_id: str = "annual-discovery-test"):
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": run_id,
        "company_id": "600123",
        "report_year": 2025,
        "source_document_id": "cninfo-discovery-test",
        "source_sha256": digest,
        "pending_review": {"candidate_signals": []},
        "confirmed": {"metrics": []},
    }
    record = {
        "company_id": "600123",
        "document_id": "cninfo-discovery-test",
        "report_period": "2025-12-31",
        "sha256": digest,
    }
    return report, record


def _valid_response(evidence_id: str, quote: str, *, title: str = "未预设的渠道政策变化") -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "title": title,
                    "observation": "原文披露的渠道安排出现变化，仍需逐项核对其执行过程。",
                    "why_it_matters": "该变化可能影响交易安排和收入确认的判断。",
                    "follow_up": "结合合同、发货与回款材料核对变化的实际影响。",
                    "priority": "medium",
                    "citations": [{"evidence_id": evidence_id, "quote": quote}],
                }
            ],
            "limitations": [],
        },
        ensure_ascii=False,
    )


def _compact_response(alias: str, quote: str, *, title: str = "未预设的渠道政策变化") -> str:
    value = json.loads(_valid_response(alias, quote, title=title))
    for finding in value["findings"]:
        for citation in finding["citations"]:
            citation["fragment_alias"] = citation.pop("evidence_id")
    return json.dumps(value, ensure_ascii=False)


def _active_binding(report, record, *, plan_sha256="c" * 64):
    return {
        "source_sha256": record["sha256"], "company_id": report["company_id"],
        "report_year": report["report_year"], "source_document_id": record["document_id"],
        "analysis_mode": "model_investigation", "provider_model_fingerprint": "a" * 64,
        "model": "local-mock", "prompts_sha256": "b" * 64,
        "parser_version": "annual-discovery-parser-v3-execution-v4",
        "plan_sha256": plan_sha256, "m3_binding_sha256": None,
        "plan_version": COMPACT_PLAN_VERSION, "execution_version": "execution_v4",
        "prompt_version": "annual_discovery_v4",
        "prompt_sha256": hashlib.sha256((annual_discovery._PROMPT_ROOT / "annual_discovery_v4.md").read_bytes()).hexdigest(),
    }


def _batch(batch_index: int = 1, raw_text: str = "客户甲在2025年度调整了交易安排，金额为120万元。") -> DiscoveryBatch:
    fragment = DiscoveryFragment(
        evidence_id=f"adisc-{'a' * 12}-p1-b0-c0-{batch_index:012x}",
        pdf_page=1,
        block_index=0,
        start_char=0,
        bbox=(1.0, 2.0, 3.0, 4.0),
        raw_text=raw_text,
    )
    return DiscoveryBatch(batch_index, (fragment,), len(raw_text), 1000)


def test_parser_accepts_dynamic_unknown_title_and_digits_only_in_exact_quote() -> None:
    batch = _batch()
    quote = "客户甲在2025年度调整了交易安排"

    findings, limitations = parse_annual_discovery_response(
        _valid_response(batch.fragments[0].evidence_id, quote), batch
    )

    assert findings[0].title == "未预设的渠道政策变化"
    assert findings[0].citations == ((batch.fragments[0].evidence_id, quote),)
    assert limitations == ()


def test_v2_parser_retains_valid_findings_when_another_finding_is_invalid() -> None:
    batch = _batch()
    evidence_id = batch.fragments[0].evidence_id
    valid = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))["findings"][0]
    invalid = copy.deepcopy(valid)
    invalid["citations"][0]["evidence_id"] = "adisc-not-in-this-batch"
    response = json.dumps({"findings": [valid, invalid], "limitations": []}, ensure_ascii=False)

    parsed = parse_annual_discovery_response_v2(response, batch)

    assert len(parsed.findings) == 1
    assert parsed.rejected_findings == 1
    assert parsed.restored_quotes == 0


def test_v2_parser_counts_all_invalid_findings_and_accepts_empty_findings() -> None:
    batch = _batch()
    invalid = json.loads(_valid_response(batch.fragments[0].evidence_id, "客户甲在2025年度调整了交易安排"))["findings"][0]
    invalid["citations"][0]["quote"] = "改写的虚构引文"
    all_invalid = parse_annual_discovery_response_v2(
        json.dumps({"findings": [invalid, invalid], "limitations": []}, ensure_ascii=False),
        batch,
    )
    empty = parse_annual_discovery_response_v2('{"findings":[],"limitations":[]}', batch)

    assert all_invalid.findings == ()
    assert all_invalid.rejected_findings == 2
    assert empty.findings == ()
    assert empty.rejected_findings == 0


def test_v2_parser_allows_distinct_quotes_from_one_fragment_but_rejects_duplicate_pairs() -> None:
    batch = _batch(raw_text="客户甲在2025年度调整了交易安排，金额为120万元。")
    evidence_id = batch.fragments[0].evidence_id
    finding = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))["findings"][0]
    finding["citations"].append({"evidence_id": evidence_id, "quote": "金额为120万元"})
    distinct = parse_annual_discovery_response_v2(
        json.dumps({"findings": [finding], "limitations": []}, ensure_ascii=False), batch
    )
    finding["citations"].append({"evidence_id": evidence_id, "quote": "金额为120万元"})
    duplicate_pair = parse_annual_discovery_response_v2(
        json.dumps({"findings": [finding], "limitations": []}, ensure_ascii=False), batch
    )

    assert len(distinct.findings) == 1
    assert distinct.findings[0].citations == (
        (evidence_id, "客户甲在2025年度调整了交易安排"),
        (evidence_id, "金额为120万元"),
    )
    assert duplicate_pair.findings == ()
    assert duplicate_pair.rejected_findings == 1


def test_v2_parser_restores_only_unique_whitespace_variants_and_counts_them() -> None:
    batch = _batch(raw_text="甲　乙在2025年度调整了交易安排")
    evidence_id = batch.fragments[0].evidence_id
    finding = json.loads(_valid_response(evidence_id, "甲乙在2025年度调整了交易安排"))["findings"][0]
    parsed = parse_annual_discovery_response_v2(
        json.dumps({"findings": [finding], "limitations": []}, ensure_ascii=False), batch
    )

    assert len(parsed.findings) == 1
    assert parsed.findings[0].citations == ((evidence_id, "甲　乙在2025年度调整了交易安排"),)
    assert parsed.restored_quotes == 1


def test_v3_aliases_remain_unambiguous_across_high_batch_and_fragment_indices() -> None:
    fragments = tuple(
        DiscoveryFragment(
            evidence_id=f"adisc-{index:016x}",
            pdf_page=1,
            block_index=index,
            start_char=index,
            bbox=(0.0, 0.0, 1.0, 1.0),
            raw_text=f"原文{index}",
        )
        for index in range(27)
    )
    batch_one = _batch_aliases(fragments, 1)
    batch_forty_six = _batch_aliases(fragments[:1], 46)
    batch_128 = _batch_aliases(fragments[:1], 128)

    assert batch_one[-1][1] == "b1-aa"
    assert batch_forty_six[0][1] == "b1a-a"
    assert batch_128[0][1] == "b3k-a"
    assert len({alias for _evidence_id, alias in (*batch_one, *batch_forty_six, *batch_128)}) == 29


def test_v3_parser_maps_only_current_batch_alias_and_validates_exact_quote() -> None:
    fragment = _batch(raw_text="客户甲在2025年度调整了交易安排。金额为120万元。").fragments[0]
    alias_one = _batch_aliases((fragment,), 1)[0][1]
    alias_46 = _batch_aliases((fragment,), 46)[0][1]
    batch = DiscoveryBatch(
        46,
        (fragment,),
        len(fragment.raw_text),
        1200,
        plan_version=COMPACT_PLAN_VERSION,
        aliases=((fragment.evidence_id, alias_46),),
    )
    base = json.loads(_valid_response(fragment.evidence_id, "客户甲在2025年度调整了交易安排"))["findings"][0]
    finding = {key: value for key, value in base.items() if key != "citations"}
    finding["citations"] = [{
        "fragment_alias": alias_46,
        "quote": "客户甲在2025年度调整了交易安排",
    }]
    parsed = parse_annual_discovery_response_v3(
        json.dumps({"findings": [finding], "limitations": []}, ensure_ascii=False), batch
    )
    cross_batch = copy.deepcopy(finding)
    cross_batch["citations"][0]["fragment_alias"] = alias_one
    rejected = parse_annual_discovery_response_v3(
        json.dumps({"findings": [cross_batch], "limitations": []}, ensure_ascii=False), batch
    )
    empty = parse_annual_discovery_response_v3('{"findings":[],"limitations":[]}', batch)

    assert len(parsed.findings) == 1
    assert parsed.findings[0].citations == ((fragment.evidence_id, "客户甲在2025年度调整了交易安排"),)
    assert rejected.findings == ()
    assert rejected.rejected_findings == 1
    assert empty.findings == ()
    assert empty.rejected_findings == 0


@pytest.mark.parametrize("field", ["title", "observation", "why_it_matters", "follow_up", "limitations"])
@pytest.mark.parametrize(
    "text",
    [
        "无法独立核实公司披露原因及是否存在舞弊。",
        "进一步核查是否存在舞弊。",
        "是否确认存在舞弊仍需核查。",
        "是否确认不存在舞弊仍需核查。",
        "是否确认没有舞弊仍需核查。",
        "是否确认排除舞弊仍需核查。",
        "是否不存在舞弊仍需核查。",
        "是否排除舞弊仍需核查。",
    ],
)
def test_v3_parser_retains_cited_finding_with_local_question(field: str, text: str) -> None:
    fragment = _batch().fragments[0]
    aliases = _batch_aliases((fragment,), 2)
    batch = DiscoveryBatch(
        2, (fragment,), len(fragment.raw_text), 1200,
        plan_version=COMPACT_PLAN_VERSION, aliases=aliases,
    )
    quote = "客户甲在2025年度调整了交易安排"
    value = json.loads(_compact_response(aliases[0][1], quote))
    if field == "limitations":
        value[field] = [text]
    else:
        value["findings"][0][field] = text

    parsed = parse_annual_discovery_response_v3(json.dumps(value, ensure_ascii=False), batch)

    assert len(parsed.findings) == 1
    assert parsed.rejected_findings == 0
    assert parsed.restored_quotes == 0
    assert parsed.findings[0].citations == ((fragment.evidence_id, quote),)
    assert parsed.limitations == tuple(value["limitations"])
    if field != "limitations":
        assert getattr(parsed.findings[0], field) == text


@pytest.mark.parametrize("field", ["title", "observation", "why_it_matters", "follow_up", "limitations"])
@pytest.mark.parametrize(
    "text",
    [
        "公司确认存在舞弊。",
        "公司确认不存在舞弊。",
        "是否存在舞弊仍需核查；公司确认存在舞弊。",
        "是否确认存在舞弊仍需核查但公司确认不存在舞弊。",
        "是否确认不存在舞弊仍需核查，公司排除舞弊。",
        "是否确认存在舞弊仍需核查且公司确认存在舞弊。",
        "是否确认不存在舞弊仍需核查且公司确认不存在舞弊。",
        "是否不存在舞弊且存在舞弊。",
        "是否确认没有舞弊且公司确认存在舞弊。",
        "是否确认排除舞弊且公司排除舞弊。",
        "是否影响采购定价及存在舞弊。",
        "是否影响采购定价及公司确认不存在舞弊。",
        "无法独立核实公司披露原因及交易过程的完整记录，存在舞弊。",
        "是否影响采购定价，但公司确认存在舞弊。",
    ],
)
def test_v3_parser_rejects_definitive_conclusion_outside_local_question(
    field: str, text: str
) -> None:
    fragment = _batch().fragments[0]
    aliases = _batch_aliases((fragment,), 2)
    batch = DiscoveryBatch(
        2, (fragment,), len(fragment.raw_text), 1200,
        plan_version=COMPACT_PLAN_VERSION, aliases=aliases,
    )
    value = json.loads(_compact_response(aliases[0][1], "客户甲在2025年度调整了交易安排"))
    if field == "limitations":
        value[field] = [text]
        with pytest.raises(ValueError, match="definitive_conclusion_forbidden"):
            parse_annual_discovery_response_v3(json.dumps(value, ensure_ascii=False), batch)
    else:
        value["findings"][0][field] = text
        parsed = parse_annual_discovery_response_v3(json.dumps(value, ensure_ascii=False), batch)
        assert parsed.findings == ()
        assert parsed.rejected_findings == 1


@pytest.mark.parametrize("prefix", ["是否", "无法"])
def test_v3_parser_does_not_extend_question_or_negation_across_fields(prefix: str) -> None:
    fragment = _batch().fragments[0]
    aliases = _batch_aliases((fragment,), 2)
    batch = DiscoveryBatch(
        2, (fragment,), len(fragment.raw_text), 1200,
        plan_version=COMPACT_PLAN_VERSION, aliases=aliases,
    )
    value = json.loads(_compact_response(aliases[0][1], "客户甲在2025年度调整了交易安排"))
    value["findings"][0]["title"] = "需要核查" + prefix
    value["findings"][0]["observation"] = "确认存在舞弊。"

    parsed = parse_annual_discovery_response_v3(json.dumps(value, ensure_ascii=False), batch)

    assert parsed.findings == ()
    assert parsed.rejected_findings == 1


def test_v3_parser_local_question_does_not_excuse_quote_from_wrong_alias() -> None:
    fragments = (_batch().fragments[0], _batch(2, "公司披露库存上升主要是增加备货所致。").fragments[0])
    aliases = _batch_aliases(fragments, 2)
    batch = DiscoveryBatch(
        2, fragments, sum(len(fragment.raw_text) for fragment in fragments), 1200,
        plan_version=COMPACT_PLAN_VERSION, aliases=aliases,
    )
    value = json.loads(_compact_response(aliases[0][1], fragments[1].raw_text))
    value["limitations"] = ["无法独立核实公司披露原因及是否存在舞弊。"]

    parsed = parse_annual_discovery_response_v3(json.dumps(value, ensure_ascii=False), batch)

    assert parsed.findings == ()
    assert parsed.rejected_findings == 1
    assert parsed.restored_quotes == 0
    assert parsed.limitations == tuple(value["limitations"])


def test_existing_parser_keeps_separate_adjacent_explanation_citations_and_rejects_joined_quote() -> None:
    rows = ("商誉 123.45 234.56", "主要是本期计提商誉减值所致。", "公司披露销售价格下降。")
    fragments = tuple(DiscoveryFragment(
        evidence_id=f"adisc-{'a' * 12}-p7-b{index}-c0-{index:012x}",
        pdf_page=7, block_index=index, start_char=0,
        bbox=(30.0 + index * 150, 300.0, 150.0 + index * 150, 312.0), raw_text=text,
    ) for index, text in enumerate(rows))
    aliases = _batch_aliases(fragments, 4)
    batch = DiscoveryBatch(4, fragments, sum(map(len, rows)), 1400, plan_version=COMPACT_PLAN_VERSION, aliases=aliases)
    value = json.loads(_compact_response(aliases[0][1], rows[0], title="商誉余额变动待核查"))
    value["findings"][0]["citations"] = [{"fragment_alias": alias, "quote": text} for (_, alias), text in zip(aliases, rows, strict=True)]
    accepted = parse_annual_discovery_response_v3(json.dumps(value, ensure_ascii=False), batch)
    assert accepted.findings[0].citations == tuple((fragment.evidence_id, fragment.raw_text) for fragment in fragments)
    value["findings"][0]["citations"] = [{"fragment_alias": aliases[0][1], "quote": " ".join(rows)}]
    rejected = parse_annual_discovery_response_v3(json.dumps(value, ensure_ascii=False), batch)
    assert rejected.findings == () and rejected.rejected_findings == 1


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("甲甲甲", "甲 甲"),  # ambiguous overlapping matches
        ("甲 乙，丙丁", "甲乙丙丁"),  # omitted punctuation
        ("甲乙丙丁", "甲乙丙戊"),  # changed character
        ("甲 乙", "甲乙"),  # restoring would duplicate another citation pair
    ],
)
def test_v2_parser_rejects_ambiguous_or_changed_whitespace_recovery(
    source: str, quote: str
) -> None:
    batch = _batch(raw_text=source)
    evidence_id = batch.fragments[0].evidence_id
    finding = json.loads(_valid_response(evidence_id, quote))["findings"][0]
    if source == "甲 乙":
        finding["citations"].append({"evidence_id": evidence_id, "quote": "甲　乙"})

    parsed = parse_annual_discovery_response_v2(
        json.dumps({"findings": [finding], "limitations": []}, ensure_ascii=False), batch
    )

    assert parsed.findings == ()
    assert parsed.rejected_findings == 1


def test_v2_parser_rejects_wrong_fragment_and_restored_quotes_over_limit() -> None:
    first = _batch(raw_text="己庚辛壬癸")
    second = _batch(batch_index=2, raw_text="另一个片段")
    wrong_fragment_id = second.fragments[0].evidence_id
    wrong_id_finding = json.loads(_valid_response(wrong_fragment_id, "甲乙丙丁"))["findings"][0]
    wrong_fragment = parse_annual_discovery_response_v2(
        json.dumps({"findings": [wrong_id_finding], "limitations": []}, ensure_ascii=False), first
    )

    same_batch_other_fragment = DiscoveryBatch(
        first.batch_index,
        (first.fragments[0], DiscoveryFragment(
            evidence_id=wrong_fragment_id,
            pdf_page=1,
            block_index=1,
            start_char=len(first.fragments[0].raw_text),
            bbox=(1.0, 2.0, 3.0, 4.0),
            raw_text="甲乙丙丁戊己庚辛壬癸",
        )),
        len(first.fragments[0].raw_text) + len("甲乙丙丁戊己庚辛壬癸"),
        1000,
    )
    wrong_local_quote = json.loads(
        _valid_response(first.fragments[0].evidence_id, "甲乙丙丁")
    )["findings"][0]
    same_batch_wrong_fragment = parse_annual_discovery_response_v2(
        json.dumps({"findings": [wrong_local_quote], "limitations": []}, ensure_ascii=False),
        same_batch_other_fragment,
    )

    long_source = " ".join("甲" for _ in range(151))
    long_batch = _batch(raw_text=long_source)
    long_finding = json.loads(_valid_response(long_batch.fragments[0].evidence_id, "甲" * 151))["findings"][0]
    over_limit = parse_annual_discovery_response_v2(
        json.dumps({"findings": [long_finding], "limitations": []}, ensure_ascii=False), long_batch
    )

    assert wrong_fragment.findings == ()
    assert wrong_fragment.rejected_findings == 1
    assert same_batch_wrong_fragment.findings == ()
    assert same_batch_wrong_fragment.rejected_findings == 1
    assert over_limit.findings == ()
    assert over_limit.rejected_findings == 1


def test_v1_parser_remains_strict_for_repeated_fragment_ids_and_whitespace_variants() -> None:
    batch = _batch(raw_text="客户甲在2025年度调整了交易安排，金额为120万元。")
    evidence_id = batch.fragments[0].evidence_id
    finding = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))["findings"][0]
    finding["citations"].append({"evidence_id": evidence_id, "quote": "金额为120万元"})
    with pytest.raises(ValueError, match="citation_evidence_id_repeated"):
        parse_annual_discovery_response(
            json.dumps({"findings": [finding], "limitations": []}, ensure_ascii=False), batch
        )

    whitespace_batch = _batch(raw_text="客户甲　调整了安排")
    whitespace_finding = json.loads(
        _valid_response(whitespace_batch.fragments[0].evidence_id, "客户甲调整了安排")
    )
    with pytest.raises(ValueError, match="citation_quote_not_exact_source_substring"):
        parse_annual_discovery_response(json.dumps(whitespace_finding, ensure_ascii=False), whitespace_batch)


@pytest.mark.parametrize(
    "change",
    [
        "wrong_id",
        "fake_quote",
        "null_quote",
        "extra_field",
        "duplicate_citation_id",
        "digit_in_opinion",
        "overlong_title",
        "fraud_claim",
    ],
)
def test_parser_rejects_the_entire_batch_for_bad_ids_quotes_or_opinions(change: str) -> None:
    batch = _batch()
    evidence_id = batch.fragments[0].evidence_id
    value = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))
    finding = value["findings"][0]
    if change == "wrong_id":
        finding["citations"][0]["evidence_id"] = "adisc-not-in-this-batch"
    elif change == "fake_quote":
        finding["citations"][0]["quote"] = "公司没有披露这句话"
    elif change == "null_quote":
        finding["citations"][0]["quote"] = None
    elif change == "extra_field":
        finding["finding_id"] = "model-controlled"
    elif change == "duplicate_citation_id":
        finding["citations"].append(dict(finding["citations"][0]))
    elif change == "digit_in_opinion":
        finding["observation"] = "该安排影响金额为120万元。"
    elif change == "overlong_title":
        finding["title"] = "甲" * 101
    elif change == "fraud_claim":
        finding["observation"] = "原文已经确认存在舞弊。"

    with pytest.raises(ValueError):
        parse_annual_discovery_response(json.dumps(value, ensure_ascii=False), batch)


def test_parser_rejects_duplicate_json_keys_and_null_top_level() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="invalid_model_json"):
        parse_annual_discovery_response('{"findings":[],"findings":[],"limitations":[]}', batch)
    with pytest.raises(ValueError):
        parse_annual_discovery_response("null", batch)


def test_parser_preserves_negated_and_unverified_limitations() -> None:
    batch = _batch()
    evidence_id = batch.fragments[0].evidence_id
    value = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))
    value["limitations"] = ["尚不能确认舞弊，相关解释未经核实。"]

    findings, limitations = parse_annual_discovery_response(
        json.dumps(value, ensure_ascii=False), batch
    )

    assert len(findings) == 1
    assert limitations == ("尚不能确认舞弊，相关解释未经核实。",)


def test_parser_keeps_cautious_or_negative_language_but_rejects_affirmative_conclusions() -> None:
    batch = _batch()
    evidence_id = batch.fragments[0].evidence_id
    for text in (
        "尚不能据此确认存在舞弊。",
        "目前未发现舞弊，仍需复核。",
        "不能排除舞弊。",
        "无法认定不存在舞弊。",
        "unverified pending review",
    ):
        value = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))
        value["limitations"] = [text]
        findings, limitations = parse_annual_discovery_response(
            json.dumps(value, ensure_ascii=False), batch
        )
        assert findings
        assert limitations == (text,)

    affirmative = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))
    affirmative["limitations"] = ["相关结论已经独立核实。"]
    with pytest.raises(ValueError, match="definitive_conclusion_forbidden"):
        parse_annual_discovery_response(json.dumps(affirmative, ensure_ascii=False), batch)


@pytest.mark.parametrize(
    "text",
    [
        "不存在舞弊。",
        "没有舞弊。",
        "排除舞弊。",
        "已排除财务造假。",
        "No fraud.",
        "Fraud ruled out.",
        "June result verified.",
        "先前未核实，但现已核实。",
    ],
)
def test_parser_rejects_definitive_exclusion_or_later_positive_verification(text: str) -> None:
    batch = _batch()
    evidence_id = batch.fragments[0].evidence_id
    value = json.loads(_valid_response(evidence_id, "客户甲在2025年度调整了交易安排"))
    value["limitations"] = [text]

    with pytest.raises(ValueError, match="definitive_conclusion_forbidden"):
        parse_annual_discovery_response(json.dumps(value, ensure_ascii=False), batch)


@pytest.mark.parametrize("priority", [None, [], {}, 1, True])
def test_parser_rejects_malformed_priority_as_a_safe_validation_error(priority) -> None:
    batch = _batch()
    value = json.loads(
        _valid_response(batch.fragments[0].evidence_id, "客户甲在2025年度调整了交易安排")
    )
    value["findings"][0]["priority"] = priority

    with pytest.raises(ValueError, match="priority_invalid"):
        parse_annual_discovery_response(json.dumps(value, ensure_ascii=False), batch)


def test_parser_rejects_oversized_response_before_json_decode() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="model_response_too_large"):
        parse_annual_discovery_response("x" * 80001, batch)


def _patch_local_audit(monkeypatch, runs_root: Path, responder):
    from finagent.llm import chat_completion

    runs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(annual_discovery, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(audited_chat, "repository_runs_root", lambda: runs_root)

    def mocked_complete(
        messages, settings, *, timeout_seconds=None, call_context=None, abort_event=None
    ):
        response_text = responder(messages, settings)
        return ChatCompletion(
            response_text,
            settings.model,
            "local-mock-response",
            provider_admitted=True,
            admission_id=str(uuid.uuid4()),
        )

    monkeypatch.setattr(chat_completion, "complete_chat", mocked_complete)
    monkeypatch.setattr(audited_chat, "complete_chat", mocked_complete)


def _run(
    path: Path,
    digest: str,
    runs_root: Path,
    report,
    record,
    callback=None,
    *,
    max_concurrency: int | None = None,
):
    run_dir = runs_root / str(report["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = ModelSettings("https://model.invalid/v1", "unit-test-secret", "local-mock")
    result = run_annual_discovery(
        report,
        path,
        record,
        settings,
        run_dir=run_dir,
        execution_binding=_active_binding(report, record),
        main_soft_deadline=annual_discovery._monotonic_clock() + 360,
        progress_callback=callback,
        max_concurrency=max_concurrency,
    )
    return result, run_dir


def test_graph_scans_without_fixed_candidates_and_audits_only_safe_source_refs(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "arbitrary.pdf"
    text = "正文没有固定候选字段，也没有现金流关键词；公司披露渠道政策调整并需要追查合同。"
    digest = _make_pdf(path, [text])
    report, record = _inputs(path, digest)
    original_report = copy.deepcopy(report)
    runs_root = tmp_path / "artifacts" / "runs"

    def responder(messages, settings):
        user = json.loads(messages[1].content)
        fragment = user["fragments"][0]
        quote = fragment[2].strip()[:16]
        return _compact_response(fragment[0], quote)

    _patch_local_audit(monkeypatch, runs_root, responder)
    progress: list[dict[str, object]] = []
    result, run_dir = _run(path, digest, runs_root, report, record, progress.append)
    payload = result.to_dict()

    assert report == original_report
    assert report["pending_review"]["candidate_signals"] == []
    assert payload["kind"] == "fintrace_annual_discovery"
    assert payload["status"] == "completed", payload
    assert payload["prompt_version"] == "annual_discovery_v4"
    contract = payload["execution_contract"]
    assert contract["binding"]["prompt_version"] == "annual_discovery_v4"
    assert contract["binding"]["plan_version"] == COMPACT_PLAN_VERSION
    assert contract["binding"]["parser_version"] == "annual-discovery-parser-v3-execution-v4"
    assert contract["binding"]["prompt_sha256"] == hashlib.sha256((annual_discovery._PROMPT_ROOT / "annual_discovery_v4.md").read_bytes()).hexdigest()
    assert payload["model_called"] is True
    assert payload["model_call_count"] == payload["model_call_attempt_count"] == 1
    assert payload["coverage"]["complete"] is True
    assert payload["fraud_conclusion"] is None
    assert payload["findings"][0]["finding_id"].startswith("disc:" + digest[:12])
    assert payload["findings"][0]["classification"] == "unverified_model_finding"
    assert payload["findings"][0]["verification_status"] == "unverified"
    assert payload["evidences"][0]["source_sha256"] == digest
    assert payload["evidences"][0]["raw_text"]
    assert any(event["event"] == "planned" for event in progress)
    assert any(event["event"] == "batch_finished" for event in progress)

    artifact = payload["audit_artifacts"][0]
    project_root = runs_root.resolve().parent.parent
    request_path = project_root / Path(artifact["request_path"])
    response_path = project_root / Path(artifact["response_path"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["evidence_refs"] == [{"document_id": record["document_id"], "page": 1}]
    assert request["prompt_version"] == "annual_discovery_v4"
    batch_input = json.loads(request["messages"][1]["content"])
    assert all(isinstance(fragment, list) and len(fragment) == 3 for fragment in batch_input["fragments"])
    request_text = request_path.read_text(encoding="utf-8")
    assert "unit-test-secret" not in request_text
    assert "https://model.invalid" not in request_text
    response = response_path.read_text(encoding="utf-8")
    assert "unit-test-secret" not in response


def test_v3_public_entry_emits_supported_initial_and_terminal_progress(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "v3-progress.pdf"
    digest = _make_pdf(path, ["公司披露渠道政策调整，需要结合合同核查。"])
    report, record = _inputs(path, digest, run_id="annual-discovery-v3-progress")
    runs_root = tmp_path / "artifacts" / "runs"
    run_dir = runs_root / report["run_id"]
    run_dir.mkdir(parents=True)
    settings = ModelSettings("https://model.invalid/v1", "unit-test-secret", "local-mock")
    binding = _active_binding(report, record)

    def responder(messages, _settings):
        user = json.loads(messages[1].content)
        fragment = user["fragments"][0]
        return _compact_response(fragment[0], fragment[2][:16])

    _patch_local_audit(monkeypatch, runs_root, responder)
    progress: list[dict[str, object]] = []
    checkpoints: list[dict[str, object]] = []
    result = run_annual_discovery(
        report,
        path,
        record,
        settings,
        run_dir=run_dir,
        execution_binding=binding,
        progress_callback=progress.append,
        checkpoint_callback=checkpoints.append,
    )
    payload = result.to_dict()

    expected_fields = {
        "event", "status", "batch_index", "planned_batches", "processed_batches",
        "successful_batches", "partial_batches", "failed_batches", "total_pages", "total_chars",
    }
    assert payload["status"] == "completed", payload
    assert payload["model_call_attempt_count"] == payload["model_call_count"] == 1
    assert len(payload["findings"]) == 1
    assert len(checkpoints) >= 3
    assert checkpoints[0]["execution_contract"]["provider_admission_count"] == 0
    assert progress
    assert all(set(event) == expected_fields for event in progress)
    assert all(event["event"] in {"planned", "batch_started", "batch_finished", "budget_exhausted"} for event in progress)
    assert all(event["status"] in {"not_called", "running", "completed", "partial", "failed", "budget_exhausted"} for event in progress)
    assert progress[0]["event"] == "planned"
    assert progress[0]["status"] == "running"
    assert progress[0]["batch_index"] == 0
    assert any(event["event"] == "batch_started" and event["batch_index"] == 1 for event in progress)
    assert any(event["event"] == "batch_finished" and event["batch_index"] == 1 and event["status"] == "completed" for event in progress)
    assert progress[-1]["event"] == "batch_finished"
    assert progress[-1]["status"] == "completed"
    assert progress[-1]["batch_index"] == 1
    assert "progress_callback_failed" not in payload["limitations"]


def test_v3_public_entry_reuses_parent_batch_and_retains_accepted_finding(tmp_path, monkeypatch) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "v3-parent-retained.pdf"
    digest = _make_pdf(path, ["公司披露渠道政策调整，需要结合合同核查。"])
    report, record = _inputs(path, digest, run_id="annual-discovery-v3-child-retained")
    runs_root = tmp_path / "artifacts" / "runs"
    run_dir = runs_root / report["run_id"]
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(annual_discovery, "repository_runs_root", lambda: runs_root)
    settings = ModelSettings("https://model.invalid/v1", "unit-test-secret", "local-mock")
    binding = _active_binding(report, record)
    parent_finding = {
        "finding_id": "disc:parent-accepted-finding",
        "batch_index": 1,
        "title": "父批次已接受的引用",
        "observation": "该发现已由父运行按原文引用检查接受。",
        "why_it_matters": "child 续跑不能删除父运行已接受的内容。",
        "follow_up": "后续核对原始来源。",
        "priority": "medium",
        "classification": "unverified_model_finding",
        "verification_status": "unverified",
        "evidence_ids": [],
        "citations": [],
    }
    parent_discovery = {
        "kind": "fintrace_annual_discovery",
        "run_id": "annual-discovery-v3-parent-retained",
        "findings": [parent_finding],
        "execution_contract": {
            "kind": "fintrace_annual_discovery_execution_v10",
            "binding": copy.deepcopy(binding),
            "batches": [{"batch_index": 1, "state": "succeeded"}],
        },
    }
    checkpoints: list[dict[str, object]] = []
    result = run_annual_discovery(
        report,
        path,
        record,
        settings,
        run_dir=run_dir,
        execution_binding=binding,
        parent_discovery=parent_discovery,
        parent_provider_admission_count=4,
        checkpoint_callback=checkpoints.append,
    ).to_dict()

    assert result["status"] == "completed"
    assert result["findings"] == [parent_finding]
    assert result["model_call_attempt_count"] == result["model_call_count"] == 0
    assert result["execution_contract"]["parent_run_id"] == parent_discovery["run_id"]
    assert result["execution_contract"]["parent_provider_admission_count"] == 4
    assert result["execution_contract"]["reused_batches"] == [1]
    assert checkpoints
    assert checkpoints[0]["findings"] == [parent_finding]


def test_v4_langgraph_preserves_resume_budget_scope_and_monotonic_timing(tmp_path, monkeypatch) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "v4-budget-state.pdf"
    digest = _make_pdf(path, ["公司调整部分交易安排，需结合合同及附注核查。"])
    runs_root = tmp_path / "artifacts" / "runs"
    settings = ModelSettings("https://model.invalid/v1", "unit-test-secret", "local-mock")
    clock = {"now": 100.0}
    monkeypatch.setattr(annual_discovery, "_monotonic_clock", lambda: clock["now"])

    def responder(_messages, _settings):
        clock["now"] += 2.375
        return json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)

    _patch_local_audit(monkeypatch, runs_root, responder)

    def binding_for(report, record):
        return _active_binding(report, record)

    new_report, new_record = _inputs(path, digest, run_id="annual-discovery-v4-new-budget")
    new_run_dir = runs_root / new_report["run_id"]
    new_run_dir.mkdir(parents=True)
    initial_context = build_annual_discovery_context(
        new_report,
        path,
        new_record,
        system_prompt=annual_discovery._load_prompt("annual_discovery_v4"),
        model_name=settings.model,
        plan_version=COMPACT_PLAN_VERSION,
    )
    plan_sha256 = hashlib.sha256(json.dumps(
        annual_discovery_plan_projection(initial_context),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    initial_binding = binding_for(new_report, new_record)
    initial_binding["plan_sha256"] = plan_sha256
    initial_snapshot = annual_discovery.build_annual_discovery_initial_snapshot(
        new_report,
        path,
        new_record,
        settings,
        run_dir=new_run_dir,
        execution_binding=initial_binding,
    )
    assert initial_snapshot["execution_contract"]["parent_run_id"] is None
    assert initial_snapshot["execution_contract"]["phase_budget"]["scope"] == "new_run"
    assert initial_snapshot["execution_contract"]["timing"]["discovery_elapsed_seconds"] == 0.0
    assert initial_snapshot["model_call_attempt_count"] == 0

    new_checkpoints: list[dict[str, object]] = []
    new_result = run_annual_discovery(
        new_report,
        path,
        new_record,
        settings,
        run_dir=new_run_dir,
        execution_binding=binding_for(new_report, new_record),
        checkpoint_callback=new_checkpoints.append,
        main_soft_deadline=clock["now"] + 30.0,
    ).to_dict()

    child_report, child_record = _inputs(path, digest, run_id="annual-discovery-v4-resume-budget")
    child_run_dir = runs_root / child_report["run_id"]
    child_run_dir.mkdir(parents=True)
    resumed_checkpoints: list[dict[str, object]] = []
    parent_discovery = {
        "kind": "fintrace_annual_discovery",
        "run_id": "annual-discovery-v4-parent-budget",
        "findings": [],
        "execution_contract": {
            "kind": "fintrace_annual_discovery_execution_v10",
            "binding": binding_for(child_report, child_record),
            "batches": [],
        },
    }
    resumed_result = run_annual_discovery(
        child_report,
        path,
        child_record,
        settings,
        run_dir=child_run_dir,
        execution_binding=binding_for(child_report, child_record),
        parent_discovery=parent_discovery,
        parent_provider_admission_count=4,
        checkpoint_callback=resumed_checkpoints.append,
        main_soft_deadline=clock["now"] + 30.0,
    ).to_dict()

    new_contract = new_result["execution_contract"]
    resumed_contract = resumed_result["execution_contract"]
    assert new_result["status"] == resumed_result["status"] == "completed"
    assert new_contract["kind"] == resumed_contract["kind"] == "fintrace_annual_discovery_execution_v10"
    assert new_contract["phase_budget"]["scope"] == "new_run"
    assert resumed_contract["parent_run_id"] == parent_discovery["run_id"]
    assert resumed_contract["phase_budget"]["scope"] == "resume"
    assert new_contract["timing"]["discovery_elapsed_seconds"] == 2.375
    assert resumed_contract["timing"]["discovery_elapsed_seconds"] == 2.375
    assert new_contract["timing"]["main_work_remaining_seconds"] == 27.625
    assert resumed_contract["timing"]["main_work_remaining_seconds"] == 27.625
    assert new_checkpoints[0]["execution_contract"]["phase_budget"]["scope"] == "new_run"
    assert new_checkpoints[0]["execution_contract"]["timing"]["discovery_elapsed_seconds"] == 0.0
    assert resumed_checkpoints[0]["execution_contract"]["phase_budget"]["scope"] == "resume"
    assert resumed_checkpoints[0]["execution_contract"]["timing"]["discovery_elapsed_seconds"] == 0.0


def _controlled_v4_inputs(tmp_path: Path, run_id: str, *, page_count: int = 6):
    pages = [
        f"第{index}页披露渠道政策变化，需要结合合同及附注核查。" + chr(0x4E00 + index) * 8800
        for index in range(1, page_count + 1)
    ]
    path = tmp_path / f"{run_id}.pdf"
    digest = _make_pdf(path, pages)
    report, record = _inputs(path, digest, run_id=run_id)
    runs_root = tmp_path / "artifacts" / "runs"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    settings = ModelSettings("https://model.invalid/v1", "unit-test-secret", "local-mock")
    context = build_annual_discovery_context(
        report,
        path,
        record,
        system_prompt=annual_discovery._load_prompt("annual_discovery_v4"),
        model_name=settings.model,
        plan_version=COMPACT_PLAN_VERSION,
    )
    plan_sha256 = hashlib.sha256(json.dumps(
        annual_discovery_plan_projection(context),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    binding = _active_binding(report, record, plan_sha256=plan_sha256)
    return path, digest, report, record, runs_root, run_dir, settings, binding, context


def _patch_controlled_v4_audit(monkeypatch, runs_root: Path, complete_chat):
    from finagent.llm import chat_completion

    runs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(annual_discovery, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(audited_chat, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(chat_completion, "complete_chat", complete_chat)
    monkeypatch.setattr(audited_chat, "complete_chat", complete_chat)


@pytest.mark.parametrize(
    "stop_mode",
    ["release", "cancel", "stage_deadline", "whole_deadline", "task_quota", "ledger_quota"],
)
def test_v4_quota_close_drains_admitted_calls_and_records_later_stops(
    tmp_path, monkeypatch, stop_mode
) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    (
        path, _digest, report, record, runs_root, run_dir, settings, binding, context,
    ) = _controlled_v4_inputs(tmp_path, f"annual-discovery-quota-{stop_mode}")
    assert len(context.batches) >= 4

    release_admitted = threading.Event()
    first_two_entered = threading.Event()
    quota_closed = threading.Event()
    later_stop_checkpoint = threading.Event()
    cancel_requested = threading.Event()
    lock = threading.Lock()
    call_indexes: list[int] = []
    aborted_indexes: list[int] = []
    checkpoints: list[dict[str, object]] = []
    clock = {"now": 100.0}
    quota_error_category = {
        "task_quota": "task_budget_exhausted",
        "ledger_quota": "ledger_budget_exhausted",
    }.get(stop_mode, "stage_budget_exhausted")
    expected_quota_close_reason = (
        "discovery_stage_budget_exhausted"
        if quota_error_category == "stage_budget_exhausted"
        else "provider_budget_exhausted"
    )

    def controlled_complete(
        messages, call_settings, *, timeout_seconds=None, call_context=None, abort_event=None
    ):
        user = json.loads(messages[1].content)
        batch_index = user["batch_index"]
        with lock:
            call_indexes.append(batch_index)
            if batch_index in {1, 2} and all(item in call_indexes for item in (1, 2)):
                first_two_entered.set()
        if batch_index in {1, 2}:
            while True:
                if abort_event is not None and abort_event.is_set():
                    with lock:
                        aborted_indexes.append(batch_index)
                    raise ModelResponseError(
                        "controlled request cancelled",
                        category="cancelled",
                        provider_admitted=True,
                        admission_id=str(uuid.uuid4()),
                    )
                if release_admitted.wait(0.005):
                    return ChatCompletion(
                        json.dumps({"findings": [], "limitations": []}),
                        call_settings.model,
                        "controlled-success",
                        provider_admitted=True,
                        admission_id=str(uuid.uuid4()),
                    )
        if batch_index == 3:
            if not first_two_entered.wait(3):
                raise RuntimeError("held calls did not enter the controlled connector")
            raise ModelResponseError(
                "controlled stage cap reached",
                category=quota_error_category,
                provider_admitted=False,
            )
        raise AssertionError(f"unexpected model dispatch for batch {batch_index}")

    def checkpoint(snapshot):
        checkpoints.append(snapshot)
        scheduler = snapshot["execution_contract"]["scheduler"]
        if (
            scheduler["dispatch_closed_reason"] == expected_quota_close_reason
            and scheduler["time_deadline_reason"] is None
            and scheduler["inflight_count"] == 2
        ):
            quota_closed.set()
        if scheduler["inflight_count"] == 2 and (
            scheduler["dispatch_closed_reason"] == "user_cancelled"
            or scheduler["time_deadline_reason"] in {
                "discovery_stage_budget_exhausted",
                "workflow_time_budget_exhausted",
            }
        ):
            later_stop_checkpoint.set()

    monkeypatch.setattr(annual_discovery, "_monotonic_clock", lambda: clock["now"])
    _patch_controlled_v4_audit(monkeypatch, runs_root, controlled_complete)
    result_holder: list[dict[str, object]] = []
    error_holder: list[BaseException] = []

    if stop_mode == "stage_deadline":
        discovery_deadline, main_deadline = 102.0, 130.0
    elif stop_mode == "whole_deadline":
        discovery_deadline, main_deadline = 130.0, 102.0
    else:
        discovery_deadline, main_deadline = 130.0, None

    def invoke_graph():
        try:
            result_holder.append(run_annual_discovery(
                report,
                path,
                record,
                settings,
                run_dir=run_dir,
                execution_binding=binding,
                max_concurrency=3,
                checkpoint_callback=checkpoint,
                cancel_check=cancel_requested.is_set,
                discovery_deadline=discovery_deadline,
                main_soft_deadline=main_deadline,
            ).to_dict())
        except BaseException as exc:
            error_holder.append(exc)

    worker = threading.Thread(target=invoke_graph, name="controlled-annual-discovery-test")
    worker.start()
    try:
        assert quota_closed.wait(5), "quota-only close checkpoint with two held calls was not published"
        if stop_mode in {"release", "task_quota", "ledger_quota"}:
            release_admitted.set()
        elif stop_mode == "cancel":
            cancel_requested.set()
            assert later_stop_checkpoint.wait(2), "user cancellation was not published during drain"
        else:
            clock["now"] = 103.0
            assert later_stop_checkpoint.wait(2), "actual deadline upgrade was not published during drain"
        worker.join(timeout=5)
        assert not worker.is_alive(), "LangGraph scan did not drain its controlled calls"
    finally:
        release_admitted.set()
        worker.join(timeout=5)

    assert not error_holder, error_holder
    assert result_holder
    payload = result_holder[0]
    scheduler = payload["execution_contract"]["scheduler"]
    assert quota_closed.is_set()
    assert scheduler["inflight_drained"] is True
    assert scheduler["inflight_count"] == 0
    assert payload["execution_contract"]["provider_admission_count"] == 2
    assert payload["coverage"]["time_budget_exhausted"] is (stop_mode in {"stage_deadline", "whole_deadline"})
    assert [
        snapshot["execution_contract"]["scheduler"]["checkpoint_publications"]
        for snapshot in checkpoints
    ] == sorted({
        snapshot["execution_contract"]["scheduler"]["checkpoint_publications"]
        for snapshot in checkpoints
    })
    assert call_indexes.count(1) == call_indexes.count(2) == 1
    assert call_indexes.count(3) == 1
    assert not any(index >= 4 for index in call_indexes)

    quota_row = next(
        item for item in payload["coverage"]["batch_results"] if item["batch_index"] == 3
    )
    assert quota_row["reason"] == expected_quota_close_reason
    if stop_mode in {"release", "task_quota", "ledger_quota"}:
        assert set(aborted_indexes) == set()
        assert scheduler["dispatch_closed_reason"] == expected_quota_close_reason
        assert scheduler["time_deadline_reason"] is None
        assert payload["model_call_count"] == payload["model_call_attempt_count"] == 2
        assert payload["coverage"]["successful_batches"] == 2
        assert payload["status"] == "partial"
        quota_checkpoint = next(
            snapshot for snapshot in checkpoints
            if snapshot["execution_contract"]["scheduler"]["dispatch_closed_reason"] == expected_quota_close_reason
            and snapshot["execution_contract"]["scheduler"]["time_deadline_reason"] is None
            and snapshot["execution_contract"]["scheduler"]["inflight_count"] == 2
        )
        assert all(
            item["reason"] == expected_quota_close_reason
            for item in payload["coverage"]["batch_results"]
            if item["status"] == "unattempted"
        )
    elif stop_mode == "cancel":
        assert set(aborted_indexes) == {1, 2}
        assert scheduler["dispatch_closed_reason"] == "user_cancelled"
        assert scheduler["time_deadline_reason"] is None
    elif stop_mode == "stage_deadline":
        assert set(aborted_indexes) == {1, 2}
        assert scheduler["dispatch_closed_reason"] == "discovery_stage_budget_exhausted"
        assert scheduler["time_deadline_reason"] == "discovery_stage_budget_exhausted"
    else:
        assert set(aborted_indexes) == {1, 2}
        assert scheduler["dispatch_closed_reason"] == "workflow_time_budget_exhausted"
        assert scheduler["time_deadline_reason"] == "workflow_time_budget_exhausted"
    if stop_mode not in {"release", "task_quota", "ledger_quota"}:
        assert later_stop_checkpoint.is_set()
        assert payload["model_call_attempt_count"] == 2
        quota_checkpoint = next(
            snapshot for snapshot in checkpoints
            if snapshot["execution_contract"]["scheduler"]["dispatch_closed_reason"] == expected_quota_close_reason
            and snapshot["execution_contract"]["scheduler"]["time_deadline_reason"] is None
            and snapshot["execution_contract"]["scheduler"]["inflight_count"] == 2
        )
    if stop_mode not in {"release", "task_quota", "ledger_quota"}:
        upgraded_checkpoint = next(
            snapshot for snapshot in checkpoints
            if snapshot["execution_contract"]["scheduler"]["inflight_count"] == 2
            and (
                snapshot["execution_contract"]["scheduler"]["dispatch_closed_reason"] == "user_cancelled"
                or snapshot["execution_contract"]["scheduler"]["time_deadline_reason"] is not None
            )
        )
        assert (
            upgraded_checkpoint["execution_contract"]["scheduler"]["checkpoint_publications"]
            > quota_checkpoint["execution_contract"]["scheduler"]["checkpoint_publications"]
        )


@pytest.mark.parametrize("failure_mode", ["authentication", "retry"])
def test_v4_fail_fast_and_retry_attempt_accounting_are_preserved(
    tmp_path, monkeypatch, failure_mode
) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    (
        path, _digest, report, record, runs_root, run_dir, settings, binding, context,
    ) = _controlled_v4_inputs(
        tmp_path,
        f"annual-discovery-{failure_mode}-accounting",
        page_count=5 if failure_mode == "authentication" else 1,
    )
    if failure_mode == "authentication":
        assert len(context.batches) > 1
    else:
        assert len(context.batches) == 1
    calls: list[int] = []

    def controlled_complete(
        messages, call_settings, *, timeout_seconds=None, call_context=None, abort_event=None
    ):
        batch_index = json.loads(messages[1].content)["batch_index"]
        calls.append(batch_index)
        if failure_mode == "authentication":
            raise ModelResponseError(
                "controlled authentication failure",
                category="http_401",
                status_code=401,
                provider_admitted=False,
            )
        if len(calls) == 1:
            raise ModelResponseError(
                "controlled transient failure",
                category="http_5xx",
                status_code=503,
                provider_admitted=True,
                admission_id=str(uuid.uuid4()),
            )
        return ChatCompletion(
            json.dumps({"findings": [], "limitations": []}),
            call_settings.model,
            "controlled-retry-success",
            provider_admitted=True,
            admission_id=str(uuid.uuid4()),
        )

    _patch_controlled_v4_audit(monkeypatch, runs_root, controlled_complete)
    result = run_annual_discovery(
        report,
        path,
        record,
        settings,
        run_dir=run_dir,
        execution_binding=binding,
        max_concurrency=1,
    ).to_dict()

    if failure_mode == "authentication":
        assert calls == [1]
        assert result["model_call_attempt_count"] == result["model_call_count"] == 0
        assert result["execution_contract"]["scheduler"]["dispatch_closed_reason"] == "authentication_failure_stopped"
    else:
        assert calls == [1, 1]
        assert result["model_call_attempt_count"] == 2
        assert result["model_call_count"] == 1
        attempts = result["execution_contract"]["batches"][0]["attempts"]
        assert [item["reason"] for item in attempts] == ["http_5xx", None]
        assert [item["provider_admitted"] for item in attempts] == [True, True]
        assert result["coverage"]["successful_batches"] == 1


def test_clean_mock_can_return_no_findings_without_fabricating_a_risk(tmp_path, monkeypatch) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "clean.pdf"
    digest = _make_pdf(path, ["本页为公司治理标准说明及免责表述，未提供具体异常事项。"])
    report, record = _inputs(path, digest, run_id="annual-discovery-clean")
    runs_root = tmp_path / "artifacts" / "runs"

    def clean_responder(messages, settings):
        return json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)

    _patch_local_audit(monkeypatch, runs_root, clean_responder)
    result, _ = _run(path, digest, runs_root, report, record)
    payload = result.to_dict()

    assert payload["status"] == "completed"
    assert payload["coverage"]["complete"] is True
    assert payload["findings"] == []
    assert payload["evidences"] == []
    assert payload["model_call_count"] == 1


def test_batch_failure_does_not_drop_successes_from_later_batches(tmp_path, monkeypatch) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "three-pages.pdf"
    texts = [
        "第一页披露渠道政策调整。" + "甲" * 8800,
        "第二页出现担保安排需要核查。" + "乙" * 8800,
        "第三页披露往来款项变化。" + "丙" * 8800,
        "最后一页披露诉讼进展。" + "丁" * 8800,
    ]
    digest = _make_pdf(path, texts)
    report, record = _inputs(path, digest, run_id="annual-discovery-partial")
    runs_root = tmp_path / "artifacts" / "runs"
    seen: list[int] = []

    def digit_free_responder(messages, settings):
        user = json.loads(messages[1].content)
        index = user["batch_index"]
        seen.append(index)
        if index == 2:
            fragment = user["fragments"][0]
            invalid = json.loads(
                _compact_response(fragment[0], fragment[2].strip()[:14])
            )
            invalid["findings"][0]["priority"] = []
            return json.dumps(invalid, ensure_ascii=False)
        if index == 3:
            raise ModelResponseError("safe local failure", provider_admitted=True, admission_id=str(uuid.uuid4()))
        fragment = user["fragments"][0]
        title = "首批渠道线索" if index == 1 else "末批诉讼线索"
        return _compact_response(fragment[0], fragment[2].strip()[:14], title=title)

    _patch_local_audit(monkeypatch, runs_root, digit_free_responder)
    result, run_dir = _run(path, digest, runs_root, report, record)
    payload = result.to_dict()

    assert len(seen) == 4, (len(seen), result.to_dict())
    assert payload["status"] == "partial"
    assert payload["model_call_count"] == 3
    assert payload["model_call_attempt_count"] == 4
    assert payload["coverage"]["planned_batches"] == 4
    assert payload["coverage"]["processed_batches"] == 4
    assert payload["coverage"]["successful_batches"] == 2
    assert payload["coverage"]["failed_batches"] == 2
    assert payload["coverage"]["complete"] is False
    failed_result_pages = {
        page
        for item in payload["coverage"]["batch_results"]
        if item["status"] == "failed"
        for page in item["pages"]
    }
    assert set(payload["coverage"]["failed_pages"]) == failed_result_pages
    assert failed_result_pages <= set(payload["coverage"]["unread_pages"])
    assert [item["status"] for item in payload["coverage"]["batch_results"]] == [
        "succeeded", "failed", "failed", "succeeded"
    ]
    assert [item["reason"] for item in payload["coverage"]["batch_results"]] == [
        None, "all_findings_rejected", "model_call_failed", None
    ]
    rejected_batch = payload["coverage"]["batch_results"][1]
    assert rejected_batch["accepted_findings"] == 0
    assert rejected_batch["rejected_findings"] == 1
    assert [item["title"] for item in payload["findings"]] == ["首批渠道线索", "末批诉讼线索"]
    assert len(payload["audit_artifacts"]) == 4
    assert sum(item["status"] == "failed" for item in payload["audit_artifacts"]) == 1
    assert payload["fraud_conclusion"] is None


def test_wall_clock_budget_preserves_success_and_marks_unattempted_pages(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "budget.pdf"
    digest = _make_pdf(path, ["渠道变化需要核对。" + "甲" * 8800, "诉讼进展需要核对。" + "乙" * 8800])
    report, record = _inputs(path, digest, run_id="annual-discovery-budget")
    runs_root = tmp_path / "artifacts" / "runs"
    seen: list[int] = []
    clock = {"now": 100.0}

    def responder(messages, settings):
        user = json.loads(messages[1].content)
        seen.append(user["batch_index"])
        fragment = user["fragments"][0]
        response = _compact_response(fragment[0], fragment[2].strip()[:14])
        clock["now"] += annual_discovery.DISCOVERY_BUDGET_SECONDS + 1
        return response

    _patch_local_audit(monkeypatch, runs_root, responder)
    monkeypatch.setattr(annual_discovery, "_monotonic_clock", lambda: clock["now"])
    result, _ = _run(path, digest, runs_root, report, record, max_concurrency=1)
    payload = result.to_dict()

    assert seen == [1]
    assert payload["status"] == "partial"
    assert payload["reason"] == "discovery_stage_budget_exhausted"
    assert payload["model_call_attempt_count"] == 1
    assert payload["findings"]
    assert payload["coverage"]["time_budget_exhausted"] is True
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["unattempted_batches"] == 1
    assert payload["coverage"]["unattempted_chars"] > 0
    assert payload["coverage"]["unread_pages"] == [2]
    assert payload["coverage"]["batch_results"][-1]["status"] == "unattempted"


def test_first_batch_failure_then_budget_end_is_failed_and_keeps_unread_pages(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "failed-budget.pdf"
    digest = _make_pdf(
        path, ["首批调用将失败。" + "甲" * 8800, "后续批次尚未调用。" + "乙" * 8800]
    )
    report, record = _inputs(path, digest, run_id="annual-discovery-failed-budget")
    runs_root = tmp_path / "artifacts" / "runs"
    seen: list[int] = []
    clock = {"now": 50.0}

    def responder(messages, settings):
        user = json.loads(messages[1].content)
        seen.append(user["batch_index"])
        clock["now"] += annual_discovery.DISCOVERY_BUDGET_SECONDS + 1
        raise ModelResponseError("safe first-batch failure", provider_admitted=True, admission_id=str(uuid.uuid4()))

    _patch_local_audit(monkeypatch, runs_root, responder)
    monkeypatch.setattr(annual_discovery, "_monotonic_clock", lambda: clock["now"])
    result, _ = _run(path, digest, runs_root, report, record, max_concurrency=1)
    payload = result.to_dict()

    assert seen == [1]
    assert payload["status"] == "failed"
    assert payload["reason"] == "discovery_stage_budget_exhausted"
    assert payload["model_call_attempt_count"] == 1
    assert payload["coverage"]["successful_batches"] == 0
    assert payload["coverage"]["failed_batches"] == 1
    assert payload["coverage"]["unattempted_batches"] == 1
    assert payload["coverage"]["unread_pages"] == [1, 2]
    assert [item["status"] for item in payload["coverage"]["batch_results"]] == [
        "failed",
        "unattempted",
    ]


def test_hash_mismatch_and_blank_pdf_never_call_audited_connector(tmp_path, monkeypatch) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "source.pdf"
    digest = _make_pdf(path, ["原始报告正文。"])
    report, record = _inputs(path, digest, run_id="annual-discovery-hash-mismatch")
    wrong_path = tmp_path / "different.pdf"
    _make_pdf(wrong_path, ["不同正文。"])
    runs_root = tmp_path / "artifacts" / "runs"
    calls: list[object] = []

    def responder(messages, settings):
        calls.append(messages)
        return json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)

    _patch_local_audit(monkeypatch, runs_root, responder)
    mismatch, _ = _run(wrong_path, digest, runs_root, report, record)
    assert mismatch.to_dict()["status"] == "not_called"
    assert mismatch.to_dict()["reason"] == "source_pdf_hash_mismatch"
    assert mismatch.to_dict()["model_call_attempt_count"] == 0
    assert calls == []

    blank_path = tmp_path / "blank.pdf"
    blank_digest = _make_pdf(blank_path, [None, None])
    blank_report, blank_record = _inputs(
        blank_path, blank_digest, run_id="annual-discovery-blank"
    )
    blank, _ = _run(blank_path, blank_digest, runs_root, blank_report, blank_record)
    blank_payload = blank.to_dict()
    assert blank_payload["status"] == "not_called"
    assert blank_payload["reason"] == "no_extractable_text"
    assert blank_payload["coverage"]["complete"] is False
    assert blank_payload["coverage"]["no_text_pages"] == [1, 2]
    assert blank_payload["model_call_attempt_count"] == 0
    assert calls == []


def test_all_batch_failures_are_failed_and_progress_error_is_only_a_safe_limitation(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("langgraph.graph", reason="LangGraph is an optional agent extra")
    path = tmp_path / "one-page.pdf"
    digest = _make_pdf(path, ["客户集中度披露仍需核查。"])
    report, record = _inputs(path, digest, run_id="annual-discovery-all-fail")
    runs_root = tmp_path / "artifacts" / "runs"

    def responder(messages, settings):
        raise ModelResponseError("private provider text must not be returned", provider_admitted=True, admission_id=str(uuid.uuid4()))

    _patch_local_audit(monkeypatch, runs_root, responder)

    def bad_callback(event):
        raise RuntimeError("callback source text should not leak")

    result, _ = _run(path, digest, runs_root, report, record, bad_callback)
    payload = result.to_dict()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "failed"
    assert payload["model_call_count"] == 0
    assert payload["model_call_attempt_count"] == 1
    assert payload["coverage"]["failed_batches"] == 1
    assert payload["coverage"]["complete"] is False
    assert "progress_callback_failed" in payload["limitations"]
    assert "private provider text" not in serialized
    assert "callback source text" not in serialized
    assert "unit-test-secret" not in serialized


def test_run_dir_must_be_exact_report_run_direct_child(tmp_path, monkeypatch) -> None:
    path = tmp_path / "path-check.pdf"
    digest = _make_pdf(path, ["安全检查。"])
    report, record = _inputs(path, digest, run_id="annual-discovery-path")
    runs_root = tmp_path / "artifacts" / "runs"
    runs_root.mkdir(parents=True)
    monkeypatch.setattr(annual_discovery, "repository_runs_root", lambda: runs_root)

    with pytest.raises(ValueError, match="report.run_id"):
        run_annual_discovery(
            report,
            path,
            record,
            ModelSettings("http://127.0.0.1:1/v1", "key", "model"),
            run_dir=runs_root / "another-run",
        )
