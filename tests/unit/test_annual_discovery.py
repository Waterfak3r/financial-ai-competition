from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import urllib.request

import pytest
import pymupdf

from finagent.agents import annual_discovery
from finagent.agents.annual_discovery import (
    parse_annual_discovery_response,
    parse_annual_discovery_response_v2,
    run_annual_discovery,
)
from finagent.audit import audited_chat
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatCompletion, ModelResponseError
from finagent.retrieval.annual_discovery_context import (
    DiscoveryBatch,
    DiscoveryFragment,
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

    def mocked_complete(messages, settings, *, timeout_seconds=None):
        response_text = responder(messages, settings)
        return ChatCompletion(response_text, settings.model, "local-mock-response")

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
        quote = fragment["raw_text"].strip()[:16]
        return _valid_response(fragment["evidence_id"], quote)

    _patch_local_audit(monkeypatch, runs_root, responder)
    progress: list[dict[str, object]] = []
    result, run_dir = _run(path, digest, runs_root, report, record, progress.append)
    payload = result.to_dict()

    assert report == original_report
    assert report["pending_review"]["candidate_signals"] == []
    assert payload["kind"] == "fintrace_annual_discovery"
    assert payload["status"] == "completed", payload
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
    request_text = request_path.read_text(encoding="utf-8")
    assert "unit-test-secret" not in request_text
    assert "https://model.invalid" not in request_text
    response = response_path.read_text(encoding="utf-8")
    assert "unit-test-secret" not in response


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
                _valid_response(fragment["evidence_id"], fragment["raw_text"].strip()[:14])
            )
            invalid["findings"][0]["priority"] = []
            return json.dumps(invalid, ensure_ascii=False)
        if index == 3:
            raise ModelResponseError("safe local failure")
        fragment = user["fragments"][0]
        title = "首批渠道线索" if index == 1 else "末批诉讼线索"
        return _valid_response(fragment["evidence_id"], fragment["raw_text"].strip()[:14], title=title)

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
        response = _valid_response(fragment["evidence_id"], fragment["raw_text"].strip()[:14])
        clock["now"] = 1901.0
        return response

    _patch_local_audit(monkeypatch, runs_root, responder)
    monkeypatch.setattr(annual_discovery, "_monotonic_clock", lambda: clock["now"])
    result, _ = _run(path, digest, runs_root, report, record, max_concurrency=1)
    payload = result.to_dict()

    assert seen == [1]
    assert payload["status"] == "partial"
    assert payload["reason"] == "workflow_time_budget_exhausted"
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
        clock["now"] = 1851.0
        raise ModelResponseError("safe first-batch failure")

    _patch_local_audit(monkeypatch, runs_root, responder)
    monkeypatch.setattr(annual_discovery, "_monotonic_clock", lambda: clock["now"])
    result, _ = _run(path, digest, runs_root, report, record, max_concurrency=1)
    payload = result.to_dict()

    assert seen == [1]
    assert payload["status"] == "failed"
    assert payload["reason"] == "workflow_time_budget_exhausted"
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
        raise ModelResponseError("private provider text must not be returned")

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
