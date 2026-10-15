from __future__ import annotations

import hashlib
import json
import urllib.request

import pytest
import pymupdf

from finagent.ingestion.parse_text_pdf import parse_text_pdf
from finagent.retrieval.annual_discovery_context import (
    MAX_FRAGMENT_CHARS,
    AnnualDiscoveryInputError,
    AnnualDiscoverySourceError,
    build_annual_discovery_context,
    coverage_is_complete,
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


def _pdf(path, pages: list[str | None], *, image_pages: set[int] | None = None) -> str:
    image_pages = image_pages or set()
    document = pymupdf.open()
    for page_index, text in enumerate(pages, start=1):
        page = document.new_page(width=1200, height=6000)
        if text:
            lines = "\n".join(text[index : index + 100] for index in range(0, len(text), 100))
            page.insert_textbox(
                pymupdf.Rect(40, 40, 1160, 5900), lines, fontsize=10, fontname="china-s"
            )
        if page_index in image_pages:
            page.insert_image(pymupdf.Rect(60, 80, 90, 110), stream=PNG_1X1)
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(path, digest: str, *, run_id: str = "annual-discovery-test"):
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": run_id,
        "company_id": "600123",
        "report_year": 2025,
        "source_document_id": "cninfo-test-discovery",
        "source_sha256": digest,
        "pending_review": {"candidate_signals": []},
        "confirmed": {"metrics": []},
    }
    source_record = {
        "company_id": "600123",
        "document_id": "cninfo-test-discovery",
        "report_period": "2025-12-31",
        "sha256": digest,
    }
    return report, source_record


def _build(path, digest: str, **limits):
    report, source_record = _identity(path, digest)
    return build_annual_discovery_context(
        report,
        path,
        source_record,
        system_prompt="安全测试提示词",
        model_name="mock-model",
        **limits,
    )


def test_context_includes_every_nonempty_block_without_heading_or_keyword_filter(tmp_path) -> None:
    path = tmp_path / "arbitrary.pdf"
    final_text = "董事会说明：客户组合发生变化，相关安排仍需结合合同和回款记录核查。"
    digest = _pdf(
        path,
        [
            "第一部分正文没有任何现金流关键词，也没有标准章节标题。",
            "不存在管理层讨论标题的附注正文仍须纳入计划。",
            final_text,
        ],
    )

    context = _build(path, digest)

    received = "".join(fragment.raw_text for fragment in context.fragments)
    assert final_text in received
    assert "第一部分正文没有任何现金流关键词" in received
    assert context.coverage["total_pages"] == 3
    assert context.coverage["text_pages"] == 3
    assert context.coverage["total_blocks"] >= 3
    assert context.coverage["planned_chars"] == context.coverage["total_chars"]
    assert context.coverage["planned_batches"] >= 1
    assert context.coverage["complete"] is False  # planning alone is not processing


def test_long_block_is_split_at_exact_offsets_and_ids_are_ascii_stable(tmp_path) -> None:
    path = tmp_path / "long.pdf"
    source_text = "A" * 2500
    digest = _pdf(path, [source_text])

    first = _build(path, digest)
    second = _build(path, digest)
    parsed_block = parse_text_pdf(path, "cninfo-test-discovery").pages[0].blocks[0].text

    fragments = [item for item in first.fragments if item.pdf_page == 1]
    assert len(fragments) >= 3
    assert all(len(item.raw_text) <= MAX_FRAGMENT_CHARS for item in fragments)
    for item in fragments:
        source_start = item.start_char
        assert item.raw_text == parsed_block[source_start : source_start + len(item.raw_text)]
        assert item.block_index == 0
        assert item.evidence_id.isascii()
    assert "".join(item.raw_text for item in fragments) == parsed_block
    assert [(x.evidence_id, x.start_char) for x in fragments] == [
        (x.evidence_id, x.start_char) for x in second.fragments if x.pdf_page == 1
    ]


def test_batch_raw_and_serialized_limits_are_enforced_and_excess_is_visible(tmp_path) -> None:
    path = tmp_path / "limited.pdf"
    digest = _pdf(path, ["甲" * 80, "乙" * 80])

    context = _build(
        path,
        digest,
        max_fragment_chars=20,
        max_batch_raw_chars=30,
        max_serialized_chars=900,
        max_batches=1,
    )

    assert len(context.batches) <= 1
    assert all(batch.raw_chars <= 30 for batch in context.batches)
    assert all(batch.serialized_input_chars <= 900 for batch in context.batches)
    assert context.coverage["truncated"] is True
    assert context.coverage["planned_chars"] < context.coverage["total_chars"]
    assert context.coverage["unread_pages"]
    assert context.coverage["complete"] is False


@pytest.mark.parametrize("plan_version", ["annual_discovery_plan_v2", "annual_discovery_plan_v3"])
def test_exact_request_boundary_includes_both_json_escape_layers(tmp_path, plan_version) -> None:
    path = tmp_path / "escaped.pdf"
    digest = _pdf(path, ['有"引号"\\斜线测试' * 40])
    options = {"max_fragment_chars": 5, "plan_version": plan_version}
    full = _build(path, digest, **options)
    assert len(full.batches) == 1
    limit = full.batches[0].serialized_input_chars
    exact = _build(path, digest, max_serialized_chars=limit, **options)
    smaller = _build(path, digest, max_serialized_chars=limit - 1, **options)

    assert exact.batches[0].fragments == full.batches[0].fragments
    assert smaller.batches[0].fragments == full.batches[0].fragments[:-1]
    assert tuple(item for batch in smaller.batches for item in batch.fragments) == full.fragments
    assert smaller.coverage["truncated"] is False
    for batch in smaller.batches:
        from finagent.retrieval.annual_discovery_context import serialize_batch_content
        body = json.dumps({
            "model": "mock-model",
            "messages": [
                {"role": "system", "content": "安全测试提示词"},
                {"role": "user", "content": serialize_batch_content(
                    smaller.identity, batch.batch_index, batch.fragments,
                    plan_version=plan_version, aliases=batch.aliases,
                )},
            ],
            "stream": False,
        }, ensure_ascii=False)
        assert len(body) == batch.serialized_input_chars
        assert len(body) <= limit - 1


def test_serialized_input_limit_counts_the_system_prompt_and_can_refuse_a_fragment(tmp_path) -> None:
    path = tmp_path / "serialized.pdf"
    digest = _pdf(path, ["有文字，但过长提示词不应让请求越过硬上限。"])
    report, record = _identity(path, digest)

    context = build_annual_discovery_context(
        report,
        path,
        record,
        system_prompt="提示" * 1000,
        model_name="mock-model",
        max_serialized_chars=500,
    )

    assert context.batches == ()
    assert context.coverage["truncated"] is True
    assert context.coverage["unread_pages"] == [1]


def test_zero_text_and_image_only_pages_are_explicit_but_text_plus_logo_is_scannable(tmp_path) -> None:
    path = tmp_path / "images.pdf"
    digest = _pdf(path, [None, None, "仍可扫描的正文披露。"], image_pages={2, 3})

    context = _build(path, digest)

    assert context.coverage["no_text_pages"] == [1, 2]
    assert context.coverage["image_pages"] == [2, 3]
    assert context.coverage["unread_image_pages"] == [2]
    assert context.coverage["unread_pages"] == [2]
    assert "仍可扫描的正文披露" in "".join(item.raw_text for item in context.fragments)
    assert context.coverage["complete"] is False


def test_hash_mismatch_stops_before_context_is_planned(tmp_path) -> None:
    path = tmp_path / "changed.pdf"
    digest = _pdf(path, ["原文披露。"])
    report, record = _identity(path, "0" * 64)
    record["sha256"] = "0" * 64

    with pytest.raises(AnnualDiscoverySourceError, match="source_pdf_hash_mismatch"):
        build_annual_discovery_context(
            report,
            path,
            record,
            system_prompt="prompt",
            model_name="mock",
        )


def test_source_identity_mismatch_is_rejected(tmp_path) -> None:
    path = tmp_path / "identity.pdf"
    digest = _pdf(path, ["正文。"])
    report, record = _identity(path, digest)
    record["company_id"] = "another-company"

    with pytest.raises(AnnualDiscoveryInputError, match="source_record_company_id_mismatch"):
        build_annual_discovery_context(
            report,
            path,
            record,
            system_prompt="prompt",
            model_name="mock",
        )


def test_coverage_complete_requires_every_planned_text_batch_and_no_unread_images() -> None:
    coverage = {
        "source_status": "parsed",
        "truncated": False,
        "unread_image_pages": [],
        "planned_batches": 2,
        "processed_batches": 2,
        "successful_batches": 2,
        "partial_batches": 0,
        "failed_batches": 0,
        "planned_chars": 900,
        "total_chars": 900,
        "processed_chars": 900,
    }
    assert coverage_is_complete(coverage)
    assert not coverage_is_complete({**coverage, "failed_batches": 1})
    assert not coverage_is_complete({**coverage, "unread_image_pages": [4]})
    assert not coverage_is_complete({**coverage, "source_status": "unavailable"})


def test_model_user_payload_is_actual_strict_json() -> None:
    # The public context tests above cover the file-to-fragment path. This
    # checks the JSON representation stays parseable when raw text has quotes.
    from finagent.retrieval.annual_discovery_context import (
        DiscoveryFragment,
        serialize_batch_content,
    )

    identity = {
        "company_id": "600123",
        "report_year": 2025,
        "source_document_id": "doc",
        "source_sha256": "a" * 64,
    }
    fragment = DiscoveryFragment("adisc-a", 2, 4, 100, (1.0, 2.0, 3.0, 4.0), '原文含"引号"')
    value = json.loads(serialize_batch_content(identity, 1, (fragment,)))
    assert value["fragments"][0]["raw_text"] == '原文含"引号"'
