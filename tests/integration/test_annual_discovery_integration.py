from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi.testclient import TestClient

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "backend" / "src"))

from finagent.agents import annual_discovery, annual_review
from finagent.audit import audited_chat
from finagent.agents.annual_review_wire import decode_annual_issue_input_wire, decode_annual_review_v3_wire
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatCompletion
from finagent.reports import investigation_append
from scripts import analyze_annual
from finagent.api.app import create_app

_DUMMY_SETTINGS = ModelSettings(
    "https://annual-discovery-mock.invalid/v1",
    "dummy-test-only-key-never-sent",
    "local-annual-discovery-mock",
    30,
)


@pytest.fixture(autouse=True)
def deny_all_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args, **_kwargs):
        raise AssertionError("annual discovery integration must not open network connections")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)


def _synthetic_pdf(path: Path, topic: str) -> str:
    pdf = pymupdf.open()
    first = pdf.new_page(width=620, height=820)
    first.insert_text((42, 100), "公司在多个地区持续推进日常业务安排。", fontname="china-s", fontsize=12)
    second = pdf.new_page(width=620, height=820)
    second.insert_text((42, 100), "年度经营环境说明与行业背景简述。", fontname="china-s", fontsize=12)
    third = pdf.new_page(width=620, height=820)
    if topic == "related_party":
        sentence = "关联交易涉及持续采购安排，披露说明仍需补充核对。"
    else:
        sentence = "主要客户集中度偏高，部分回款条款仍需进一步核验。"
    # This body text intentionally has no section heading or MD&A marker.
    third.insert_text((42, 100), sentence, fontname="china-s", fontsize=12)
    raw = pdf.tobytes()
    pdf.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _finding_for(batch: dict[str, object], topic: str) -> dict[str, object]:
    if topic == "related_party":
        quote = "关联交易涉及持续采购安排，披露说明仍需补充核对。"
        second_quote = "披露说明仍需补充核对"
        title = "关联采购安排披露"
        observation = "报告披露关联采购安排，其交易背景和定价依据仍需单独核对。"
        why = "相关安排可能影响对业务往来和治理风险的理解。"
        follow = "建议核对相关合同、定价依据及审批记录。"
    else:
        quote = "主要客户集中度偏高，部分回款条款仍需进一步核验。"
        second_quote = "部分回款条款仍需进一步核验"
        title = "客户集中与回款安排"
        observation = "报告披露主要客户集中度偏高，相关回款条款仍需核对。"
        why = "客户结构和回款安排可能影响对经营稳定性的理解。"
        follow = "建议核对主要客户合同、结算条件及期后回款。"
    selected = next(
        fragment
        for fragment in batch["fragments"]
        if quote in fragment[2]
    )
    return {
        "title": title,
        "observation": observation,
        "why_it_matters": why,
        "follow_up": follow,
        "priority": "medium",
        "citations": [
            {"fragment_alias": selected[0], "quote": quote},
            {"fragment_alias": selected[0], "quote": second_quote},
        ],
    }


@pytest.mark.parametrize(
    ("topic", "company_id", "document_id", "expected_title", "quote", "expected_discovery_status"),
    [
        (
            "related_party",
            "SYNTH-ASSOC",
            "synthetic-annual-assoc",
            "关联采购安排披露",
            "关联交易涉及持续采购安排，披露说明仍需补充核对。",
            "partial",
        ),
        (
            "customer_concentration",
            "SYNTH-CUSTOMER",
            "synthetic-annual-customer",
            "客户集中与回款安排",
            "主要客户集中度偏高，部分回款条款仍需进一步核验。",
            "completed",
        ),
    ],
)
def test_synthetic_pdf_cli_mock_get_preview_and_three_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    topic: str,
    company_id: str,
    document_id: str,
    expected_title: str,
    quote: str,
    expected_discovery_status: str,
) -> None:
    project_root = tmp_path / "isolated-project"
    source_pdf = project_root / "data" / "raw" / company_id.lower() / "2025" / document_id / "annual.pdf"
    source_sha = _synthetic_pdf(source_pdf, topic)
    source_record_path = source_pdf.parent / "source.json"
    source_record_path.write_text(json.dumps({
        "company_id": company_id,
        "document_id": document_id,
        "report_period": "2025-12-31",
        "sha256": source_sha,
        "local_path": str(source_pdf),
    }, ensure_ascii=False), encoding="utf-8")
    runs_root = project_root / "artifacts" / "runs"
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(investigation_append, "_repository_root", lambda: project_root)
    monkeypatch.setattr(investigation_append, "load_model_settings", lambda: _DUMMY_SETTINGS)
    monkeypatch.setattr(investigation_append, "_ensure_langgraph_available", lambda: None)
    monkeypatch.setattr(annual_discovery, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(annual_review, "repository_runs_root", lambda: runs_root)
    monkeypatch.setattr(audited_chat, "repository_runs_root", lambda: runs_root)

    def abstained_fixed_candidates(report, **_kwargs):
        identity = {
            key: report[key]
            for key in (
                "run_id", "company_id", "report_year", "source_document_id", "source_sha256"
            )
        }
        return SimpleNamespace(to_dict=lambda: {
            "kind": "fintrace_annual_investigation",
            **identity,
            "status": "abstained",
            "reason": "no_supported_candidate_signal",
            "model_called": False,
            "model_call_count": 0,
            "model_call_attempt_count": 0,
            "items": [],
            "audit_artifacts": [],
            "fraud_conclusion": None,
        })

    monkeypatch.setattr(investigation_append, "_invoke_agent", abstained_fixed_candidates)

    prompt_root = _PROJECT_ROOT / "config" / "prompts"
    prompts = {name: (prompt_root / f"{name}.md").read_text(encoding="utf-8") for name in (
        "annual_discovery_v4", "annual_issue_grouping_v1", "annual_issue_global_v1", "annual_issue_supplement_v1", "annual_review_v3",
    )}
    fixture_prompt_root = project_root / "config" / "prompts"
    fixture_prompt_root.mkdir(parents=True)
    for prompt_path in prompt_root.glob("*.md"):
        (fixture_prompt_root / prompt_path.name).write_bytes(prompt_path.read_bytes())

    def mock_complete_chat(messages, settings, **_kwargs):
        assert settings == _DUMMY_SETTINGS
        prompt_version = next(name for name, text in prompts.items() if text == messages[0].content)
        user_content = messages[1].content
        if prompt_version == "annual_discovery_v4":
            batch = json.loads(user_content)
            finding = _finding_for(batch, topic)
            findings = [finding]
            if expected_discovery_status == "partial":
                invalid = copy.deepcopy(finding)
                invalid["citations"][0]["quote"] = "并非来源中的原文"
                findings.append(invalid)
            response = {"findings": findings, "limitations": []}
        elif prompt_version == "annual_issue_grouping_v1":
            grouping = decode_annual_issue_input_wire(json.loads(user_content))
            response = {"schema_version": "annual_issue_group_shard_result_v1", "decisions": [{
                "finding_id": finding_id, "disposition": "retain", "target_finding_id": None,
                "reason_code": "distinct_issue", "reason_evidence_id": None, "reason_quote": None,
            } for finding_id in grouping["input_member_finding_ids"]]}
        elif prompt_version == "annual_issue_supplement_v1":
            supplement = decode_annual_issue_input_wire(json.loads(user_content))
            response = {"schema_version": "annual_issue_supplement_result_v1", "roles": [{
                "issue_id": item["issue_id"], "evidence_id": item["evidence_id"],
                "role": "unknown", "reason_code": "relationship_unclear",
            } for item in supplement["evidence_assignments"]]}
        elif prompt_version == "annual_review_v3":
            review_input = decode_annual_review_v3_wire(json.loads(user_content))
            assert review_input["verified_facts"] == []
            assert review_input["coverage"]["candidate_count"] == 1
            candidate = review_input["issue_hierarchy"]["candidate_pool"][0]
            finding_id = candidate["finding_id"]
            assert {citation["pdf_page"] for citation in candidate["citations"]} == {3}
            response = {
                "assessment": "prioritize_review", "summary": "建议对年报披露事项进一步复核。",
                "reasons": [{"text": "该披露事项值得进一步关注。", "evidence_ids": [finding_id]}],
                "follow_up_items": [], "limitations": review_input["limitations_required"],
            }
        else:
            raise AssertionError(f"unexpected mock prompt version: {prompt_version}")
        calls.append({"prompt_version": prompt_version, "request": {"messages": [{"role": item.role, "content": item.content} for item in messages]}})
        return ChatCompletion(
            text=json.dumps(response, ensure_ascii=False), model=settings.model,
            response_id=f"mock-{len(calls)}", provider_admitted=True, admission_id=str(uuid.uuid4()),
        )

    monkeypatch.setattr(audited_chat, "complete_chat", mock_complete_chat)

    result = analyze_annual.main([
        "--source-pdf", str(source_pdf),
        "--company-id", company_id,
        "--report-year", "2025",
        "--document-id", document_id,
        "--artifacts-root", str(project_root / "artifacts"),
        "--expected-sha256", source_sha,
        "--with-model",
        "--source-record", str(source_record_path),
    ])
    output = capsys.readouterr()

    assert result == (2 if expected_discovery_status == "partial" else 0), output.out + output.err
    run_match = re.search(r"run_id=(annual-analysis-[0-9a-f-]{36})", output.out)
    assert run_match is not None, output.out
    run_id = run_match.group(1)
    run_dir = project_root / "artifacts" / "runs" / run_id
    report_dir = project_root / "artifacts" / "reports" / run_id
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    analysis = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    discovery = report["model_discovery"]

    assert report["confirmed"]["metrics"] == []
    assert report["model_investigation"]["status"] == "abstained"
    assert report["model_investigation"]["items"] == []
    assert discovery["status"] == expected_discovery_status
    assert discovery["findings"][0]["title"] == expected_title
    assert discovery["findings"][0]["finding_id"].startswith(f"disc:{source_sha[:12]}-")
    assert discovery["findings"][0]["classification"] == "unverified_model_finding"
    assert discovery["findings"][0]["evidence_ids"] == [discovery["findings"][0]["citations"][0]["evidence_id"]]
    assert len(discovery["findings"][0]["citations"]) == 2
    assert len({citation["evidence_id"] for citation in discovery["findings"][0]["citations"]}) == 1
    assert discovery["fraud_conclusion"] is None
    assert report["model_review"]["status"] == "completed"
    assert report["model_review"]["reasons"][0]["evidence_ids"] == [
        discovery["findings"][0]["finding_id"]
    ]
    assert analysis["model_discovery"]["status"] == expected_discovery_status
    assert manifest["model_discovery_status"] == expected_discovery_status
    if expected_discovery_status == "partial":
        assert discovery["coverage"]["successful_batches"] == 0
        assert discovery["coverage"]["partial_batches"] == 1
        assert discovery["coverage"]["failed_batches"] == 0
        assert discovery["coverage"]["processed_chars"] == 0
        assert discovery["coverage"]["complete"] is False
        assert discovery["coverage"]["failed_pages"] == []
        assert discovery["coverage"]["unread_pages"] == discovery["coverage"]["batch_results"][0]["pages"]
        assert 3 in discovery["coverage"]["unread_pages"]
        assert discovery["coverage"]["batch_results"][0]["status"] == "partial"
        assert discovery["coverage"]["batch_results"][0]["accepted_findings"] == 1
        assert discovery["coverage"]["batch_results"][0]["rejected_findings"] == 1
    else:
        assert discovery["coverage"]["successful_batches"] == 1
        assert discovery["coverage"]["partial_batches"] == 0
        assert discovery["coverage"]["complete"] is True
    assert manifest["status"] == "completed_with_issues"
    assert calls[0]["prompt_version"] == "annual_discovery_v4"
    assert calls[-1]["prompt_version"] == "annual_review_v3"
    assert "annual_issue_grouping_v1" in [call["prompt_version"] for call in calls]
    discovery_request = calls[0]["request"]["messages"][1]["content"]
    serialized_batch = json.loads(discovery_request)
    assert any(
        quote in item[2]
        for item in serialized_batch["fragments"]
    )
    assert all(
        not any(marker in item[2] for marker in ("cash flow", "MD&A"))
        for item in serialized_batch["fragments"]
    )
    assert expected_title in (report_dir / "report.md").read_text(encoding="utf-8")

    client = TestClient(create_app(project_root))
    main_report = client.get(f"/v1/annual-analyses/{run_id}")
    assert main_report.status_code == 200, main_report.text
    main_payload = main_report.json()
    assert main_payload["discovery_evidences"]["interpretation_label"] == "年报原文定位，模型发现未经独立语义核验"
    evidence = main_payload["discovery_evidences"]["items"][0]
    assert evidence["pdf_page"] == 3
    assert quote in evidence["raw_text"]
    assert evidence["finding_ids"] == [discovery["findings"][0]["finding_id"]]
    assert "preview_url" in evidence

    index = client.get(f"/v1/annual-analyses/{run_id}/discovery-evidences")
    assert index.status_code == 200, index.text
    assert index.json()["items"][0]["evidence_id"] == evidence["evidence_id"]
    indexed_citations = index.json()["items"][0]["citations"]
    assert len(indexed_citations) == 2
    assert {item["quote"] for item in indexed_citations} == {
        citation["quote"] for citation in discovery["findings"][0]["citations"]
    }
    preview = client.get(
        f"/v1/annual-analyses/{run_id}/discovery-evidences/{evidence['evidence_id']}/preview.png"
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["x-evidence-kind"] == "annual_discovery"
    assert preview.headers["x-pdf-page"] == "3"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")

    exported_json = client.get(f"/v1/annual-analyses/{run_id}/export?format=json")
    exported_markdown = client.get(f"/v1/annual-analyses/{run_id}/export?format=markdown")
    exported_html = client.get(f"/v1/annual-analyses/{run_id}/export?format=html")
    assert [item.status_code for item in (exported_json, exported_markdown, exported_html)] == [200, 200, 200]
    json_text = exported_json.text
    markdown_text = exported_markdown.text
    html_text = exported_html.text
    for text in (json_text, markdown_text, html_text):
        assert expected_title in text
        assert evidence["evidence_id"] in text
        assert discovery["findings"][0]["finding_id"] in text
        assert quote not in text
        assert all(citation["quote"] not in text for citation in discovery["findings"][0]["citations"])
        assert "raw_text" not in text
        assert '"quote"' not in text
    json_payload = json.loads(json_text)
    assert json_payload["report"]["model_discovery"]["findings"][0]["verification_status"] == "unverified"
    assert json_payload["report"]["model_discovery"]["findings"][0]["citations"][0]["pdf_page"] == 3
    assert len(json_payload["report"]["model_discovery"]["findings"][0]["citations"]) == 2
    assert json_payload["report"]["model_discovery"]["coverage"]["partial_batches"] == (
        1 if expected_discovery_status == "partial" else 0
    )
    assert "PDF 第3页" in markdown_text
    assert "PDF 第3页" in html_text
    assert "discovery" not in json_payload["report"]["model_investigation"]
