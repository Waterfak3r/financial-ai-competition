from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
from pathlib import Path
from urllib.parse import quote

import pymupdf
import pytest
from fastapi.testclient import TestClient

from finagent.api.app import create_app
import finagent.api.app as app_module
from finagent.api import annual_report_read
from finagent.agents.annual_issue_pipeline import ISSUE_WIRE_VERSION
from finagent.agents.annual_review_wire import V3_INPUT_PROJECTION_VERSION, V3_WIRE_SCHEMA
from finagent.core.model_settings import ModelSettings
import finagent.reports.investigation_append as investigation_append
from finagent.retrieval.annual_discovery_context import (
    build_annual_discovery_context,
    coverage_is_complete,
)


def test_issue_api_projection_uses_display_order_and_keeps_quotes_in_controlled_source_path_only() -> None:
    pipeline = {
        "kind": "annual_issue_pipeline_v1",
        "candidates": [{
            "finding_id": "finding:one", "evidence_ids": ["evidence:one"], "priority": "high",
            "citations": [{"evidence_id": "evidence:one", "quote": "API_ISSUE_QUOTE_SECRET"}],
        }],
        "issues": [{
            "issue_id": "issue:one", "member_finding_ids": ["finding:one"], "evidence_ids": ["evidence:one"],
            "follow_up": "复核附注。", "citations": [{"evidence_id": "evidence:one", "quote": "API_ISSUE_QUOTE_SECRET"}],
        }],
        "supplements": [{
            "evidence_id": "supp:one", "raw_text": "API_SUPPLEMENT_SECRET",
            "issue_roles": [{"issue_id": "issue:one", "role": "unknown", "reason_quote": "API_REASON_QUOTE_SECRET"}],
        }],
        "member_decisions": [{"finding_id": "finding:one", "reason_quote": "API_MEMBER_REASON_SECRET"}],
    }
    review = {
        "status": "completed", "reasons": [{"evidence_ids": ["evidence:one"]}],
        "follow_up_items": [{"object": "finding:one", "evidence_ids": ["evidence:one"]}],
    }

    projected = annual_report_read.redact_issue_pipeline_text_for_api({
        "model_issue_pipeline": pipeline,
        "model_review": review,
        "model_investigation": {"model_issue_pipeline": pipeline},
    })

    safe_pipeline = projected["model_issue_pipeline"]
    assert safe_pipeline["presentation_order"]["candidate_order"][0]["finding_id"] == "finding:one"
    assert safe_pipeline == projected["model_investigation"]["model_issue_pipeline"]
    serialized = json.dumps(projected, ensure_ascii=False)
    for secret in ("API_ISSUE_QUOTE_SECRET", "API_SUPPLEMENT_SECRET", "API_REASON_QUOTE_SECRET", "API_MEMBER_REASON_SECRET"):
        assert secret not in serialized


def _make_archive(
    root: Path,
    run_id: str,
    *,
    source_text: str = "source alpha",
    include_narrative: bool = False,
    rotate_value_page: bool = False,
) -> dict[str, object]:
    run_dir = root / "artifacts" / "runs" / run_id
    report_dir = root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)

    pdf = pymupdf.open()
    first = pdf.new_page(width=400, height=500)
    first.insert_text((40, 80), f"TABLE {source_text} TITLE AND COLUMN HEADER")
    narrative_text = "Contract income follows performance."
    narrative_rect = None
    if include_narrative:
        first.insert_text((40, 200), narrative_text)
        narrative_rect = first.search_for(narrative_text)[0]
    value_page = pdf.new_page(width=600, height=800)
    value_page.insert_text((70, 130), "INDEPENDENT VERIFIED VALUE")
    value_rect = value_page.search_for("INDEPENDENT VERIFIED VALUE")[0]
    if rotate_value_page:
        value_page.set_rotation(90)
    source_pdf_bytes = pdf.tobytes()
    pdf.close()
    (run_dir / "source.pdf").write_bytes(source_pdf_bytes)
    source_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
    evidence = {
        "evidence_id": "ev-independent",
        "document_id": "doc-1",
        "source_sha256": source_sha256,
        "pdf_page": 2,
        "value_region": {
            "text": "INDEPENDENT VERIFIED VALUE",
            "page": 2,
            "bbox": {
                "x0": value_rect.x0,
                "y0": value_rect.y0,
                "x1": value_rect.x1,
                "y1": value_rect.y1,
            },
        },
        # These earlier-page coordinates must not affect the preview page.
        "column_region": {"page": 1, "bbox": {"x0": 20, "y0": 60, "x1": 200, "y1": 90}},
        "title_region": {"page": 1, "bbox": {"x0": 20, "y0": 20, "x1": 200, "y1": 40}},
    }
    legacy_evidence = {
        "evidence_id": "ev-legacy-extraction",
        "document_id": "doc-1",
        "source_sha256": source_sha256,
        "pdf_page": 2,
        "value_region": evidence["value_region"],
    }
    manifest = {
        "run_id": run_id,
        "status": "completed",
        "company_id": "603288",
        "report_year": 2024,
        "document_id": "doc-1",
        "inputs": {
            "source_pdf_original_path": "D:\\private\\source.pdf",
            "source_pdf_archive_path": f"runs/{run_id}/source.pdf",
            "source_pdf_sha256": source_sha256,
        },
        "outputs": {
            "run_dir": f"runs/{run_id}",
            "report_dir": f"reports/{run_id}",
            "report_json": f"reports/{run_id}/report.json",
        },
    }
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": run_id,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": "doc-1",
        "source_sha256": source_sha256,
        "confirmed": {
            "metrics": [
                {
                    "fact_id": "fact-1",
                    "source_document_id": "doc-1",
                    "source_sha256": source_sha256,
                    "evidences": [legacy_evidence],
                    "verification_evidences": [evidence],
                    "verifications": [
                        {
                            "target_type": "financial_fact",
                            "target_id": "fact-1",
                            "status": "verified",
                            "evidence_ids": ["ev-independent"],
                        }
                    ],
                }
            ],
            "analyses": [],
        },
        "pending_review": {
            "uncited_evidences": [
                {"evidence_id": "ev-pending", "source_sha256": source_sha256}
            ]
        },
        "metadata_note": source_text,
    }
    if include_narrative:
        report["model_investigation"] = {
            "items": [
                {
                    "signal_id": "revenue_up_cash_down",
                    "status": "interpretation",
                    "narrative_evidence_ids": ["nctx-test-note"],
                }
            ],
            "narrative_evidences": [
                {
                    "evidence_id": "nctx-test-note",
                    "source_document_id": "doc-1",
                    "source_sha256": source_sha256,
                    "signal_ids": ["revenue_up_cash_down"],
                    "section_label": "financial_notes",
                    "pdf_page": 1,
                    "bbox": {
                        "x0": narrative_rect.x0,
                        "y0": narrative_rect.y0,
                        "x1": narrative_rect.x1,
                        "y1": narrative_rect.y1,
                    },
                    "raw_text": narrative_text,
                    "selection_reason": "local-term-match",
                },
                {
                    "evidence_id": "nctx-uncited",
                    "source_document_id": "doc-1",
                    "source_sha256": source_sha256,
                    "signal_ids": ["revenue_up_cash_down"],
                    "section_label": "financial_notes",
                    "pdf_page": 1,
                    "bbox": {
                        "x0": narrative_rect.x0,
                        "y0": narrative_rect.y0,
                        "x1": narrative_rect.x1,
                        "y1": narrative_rect.y1,
                    },
                    "raw_text": narrative_text,
                    "selection_reason": "local-term-match",
                },
            ],
        }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (report_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return {
        "source_pdf_bytes": source_pdf_bytes,
        "source_sha256": source_sha256,
        "report": report,
        "evidence": evidence,
    }


def _attach_valid_discovery(root: Path, run_id: str, created: dict[str, object]) -> tuple[str, str]:
    report_path = root / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    source_path = root / "artifacts" / "runs" / run_id / "source.pdf"
    source_record = {
        "company_id": report["company_id"],
        "document_id": report["source_document_id"],
        "report_period": "2024-12-31",
        "sha256": created["source_sha256"],
        "local_path": "data/raw/fixture.pdf",
    }
    context = build_annual_discovery_context(
        report,
        source_path,
        source_record,
        system_prompt="test prompt",
        model_name="local-mock",
    )
    batch = context.batches[0]
    fragment = batch.fragments[0]
    coverage = dict(context.coverage)
    coverage.pop("partial_batches", None)
    coverage.update({
        "processed_batches": len(context.batches),
        "successful_batches": len(context.batches),
        "failed_batches": 0,
        "processed_chars": coverage["planned_chars"],
        "attempted_chars": coverage["planned_chars"],
        "unattempted_batches": 0,
        "unattempted_chars": 0,
        "omitted_chars": coverage["total_chars"] - coverage["planned_chars"],
        "batch_results": [
            {
                "batch_index": item.batch_index,
                "status": "succeeded",
                "reason": None,
                "pages": sorted({part.pdf_page for part in item.fragments}),
                "raw_chars": item.raw_chars,
            }
            for item in context.batches
        ],
    })
    coverage["complete"] = coverage_is_complete(coverage)
    finding_id = f"disc:{created['source_sha256'][:12]}-{hashlib.sha256(b'fixture-finding').hexdigest()[:20]}"
    quote_text = fragment.raw_text[: min(30, len(fragment.raw_text))]
    evidence = fragment.evidence_value(str(report["source_document_id"]), str(created["source_sha256"]))
    report["model_discovery"] = {
        "kind": "fintrace_annual_discovery",
        "run_id": run_id,
        "company_id": report["company_id"],
        "report_year": report["report_year"],
        "source_document_id": report["source_document_id"],
        "source_sha256": created["source_sha256"],
        "status": "completed",
        "prompt_version": "annual_discovery_v1",
        "model_called": True,
        "model_call_count": len(context.batches),
        "model_call_attempt_count": len(context.batches),
        "coverage": coverage,
        "findings": [{
            "finding_id": finding_id,
            "title": "构造的业务安排线索",
            "observation": "报告原文披露一项待进一步核对的安排。",
            "why_it_matters": "相关安排可能影响对业务披露的理解。",
            "follow_up": "建议复核相关合同与审批材料。",
            "priority": "medium",
            "classification": "unverified_model_finding",
            "verification_status": "unverified",
            "evidence_ids": [fragment.evidence_id],
            "citations": [{"evidence_id": fragment.evidence_id, "quote": quote_text}],
        }],
        "evidences": [evidence],
        "audit_artifacts": [],
        "limitations": [],
        "reason": None,
        "fraud_conclusion": None,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return str(fragment.evidence_id), str(finding_id)


def test_report_api_returns_manifest_binding_and_only_verified_evidence(tmp_path: Path) -> None:
    run_id = "synthetic-report-001"
    created = _make_archive(tmp_path, run_id)
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["report"]["run_id"] == run_id
    assert payload["manifest"]["source_pdf_sha256"] == created["source_sha256"]
    report_bytes = (tmp_path / "artifacts" / "reports" / run_id / "report.json").read_bytes()
    assert payload["manifest"]["report_sha256"] == hashlib.sha256(report_bytes).hexdigest()
    assert [item["evidence_id"] for item in payload["verification_evidences"]] == ["ev-independent"]
    assert payload["narrative_evidences"]["items"] == []
    assert "D:\\private\\source.pdf" not in response.text


def test_v2_read_api_keeps_findings_from_partial_batches_with_no_full_success(tmp_path: Path) -> None:
    run_id = "synthetic-discovery-partial-v2"
    created = _make_archive(tmp_path, run_id)
    evidence_id, finding_id = _attach_valid_discovery(tmp_path, run_id, created)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    layer = report["model_discovery"]
    coverage = layer["coverage"]
    batch_result = coverage["batch_results"][0]
    coverage.update(
        successful_batches=0,
        partial_batches=1,
        failed_batches=0,
        processed_chars=0,
        attempted_chars=batch_result["raw_chars"],
        unread_pages=batch_result["pages"],
        complete=False,
        batch_results=[{
            "batch_index": batch_result["batch_index"],
            "status": "partial",
            "reason": "some_findings_rejected",
            "pages": batch_result["pages"],
            "raw_chars": batch_result["raw_chars"],
            "accepted_findings": 1,
            "rejected_findings": 1,
            "restored_quotes": 0,
        }],
    )
    layer.update(status="partial", prompt_version="annual_discovery_v2")
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    client = TestClient(create_app(tmp_path))
    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["report"]["model_discovery"]["status"] == "partial"
    assert payload["report"]["model_discovery"]["coverage"]["successful_batches"] == 0
    assert payload["report"]["model_discovery"]["coverage"]["partial_batches"] == 1
    assert payload["report"]["model_discovery"]["coverage"]["complete"] is False
    assert [item["finding_id"] for item in payload["report"]["model_discovery"]["findings"]] == [finding_id]
    index = client.get(f"/v1/annual-analyses/{run_id}/discovery-evidences")
    assert index.status_code == 200, index.text
    assert index.json()["items"][0]["evidence_id"] == evidence_id


def test_discovery_exception_fallback_is_accepted_by_strict_report_get(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "synthetic-discovery-exception-fallback"
    created = _make_archive(tmp_path, run_id)
    run_dir = tmp_path / "artifacts" / "runs" / run_id
    artifacts_root = tmp_path / "artifacts"
    report = created["report"]
    audit_id = "3f11c9d2-1f63-4f11-8bc8-63c87837c4ab"

    class _FailingDiscoveryAgent:
        @staticmethod
        def run_annual_discovery(*_args, run_dir: Path, **_kwargs):
            audit_dir = run_dir / audit_id
            audit_dir.mkdir()
            attempt_dir = audit_dir / "attempt-01"
            attempt_dir.mkdir()
            attempt_audit_id = "4f11c9d2-1f63-4f11-8bc8-63c87837c4ab"
            admission_id = "5f11c9d2-1f63-4f11-8bc8-63c87837c4ab"
            request_batch = {
                "company_id": report["company_id"],
                "report_year": report["report_year"],
                "source_document_id": report["source_document_id"],
                "source_sha256": report["source_sha256"],
                "batch_index": 1,
                "fragments": [],
            }
            (audit_dir / "request.json").write_text(
                json.dumps({
                    "audit_id": audit_id,
                    "status": "started",
                    "prompt_version": "annual_discovery_v2",
                    "messages": [
                        {"role": "system", "content": "synthetic"},
                        {"role": "user", "content": json.dumps(request_batch)},
                    ],
                }),
                encoding="utf-8",
            )
            (attempt_dir / "request.json").write_text(
                json.dumps({
                    "status": "started",
                    "attempt_index": 1,
                    "audit_id": attempt_audit_id,
                    "prompt_version": "annual_discovery_v2",
                    "model": "test-model",
                }),
                encoding="utf-8",
            )
            (attempt_dir / "failure.json").write_text(
                json.dumps({
                    "status": "failed",
                    "attempt_index": 1,
                    "audit_id": attempt_audit_id,
                    "category": "timeout",
                    "provider_admitted": True,
                }),
                encoding="utf-8",
            )
            (audit_dir / "failure.json").write_text(
                json.dumps({
                    "status": "failed",
                    "category": "timeout",
                    "provider_admission_count": 1,
                    "response_count": 0,
                    "attempt_records": [{
                        "attempt_index": 1,
                        "audit_id": attempt_audit_id,
                        "admission_id": admission_id,
                        "provider_admitted": True,
                        "response_accepted": False,
                        "accepted_findings": 0,
                        "status": "failed",
                        "reason": "timeout",
                    }],
                }),
                encoding="utf-8",
            )
            raise RuntimeError("synthetic post-audit failure")

    original_import = investigation_append.importlib.import_module

    def import_agent(name: str):
        if name == "finagent.agents.annual_discovery":
            return _FailingDiscoveryAgent
        return original_import(name)

    monkeypatch.setattr(investigation_append.importlib, "import_module", import_agent)
    discovery = investigation_append._invoke_discovery(
        report,
        source_pdf_path=run_dir / "source.pdf",
        source_record={
            "company_id": report["company_id"],
            "document_id": report["source_document_id"],
            "report_period": "2024-12-31",
            "sha256": report["source_sha256"],
        },
        settings=ModelSettings("https://example.invalid/v1", "local-test-secret", "test-model", 1),
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        progress_callback=None,
    )

    report["model_discovery"] = discovery
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    response = TestClient(create_app(tmp_path)).get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200, response.text
    layer = response.json()["report"]["model_discovery"]
    assert layer["status"] == "failed"
    assert layer["model_call_attempt_count"] == 1
    assert layer["model_call_count"] == 0
    assert layer["coverage"]["source_status"] == "unavailable"
    assert layer["coverage"]["batch_results"] == []
    assert layer["audit_artifacts"] == [
        {
            "batch_index": 1,
            "audit_dir": f"artifacts/runs/{run_id}/{audit_id}",
            "request_path": f"artifacts/runs/{run_id}/{audit_id}/request.json",
            "response_path": None,
            "failure_path": f"artifacts/runs/{run_id}/{audit_id}/failure.json",
            "status": "failed",
            "prompt_version": "annual_discovery_v2",
        }
    ]


@pytest.mark.parametrize("invalid_case", ["source_sha", "quote", "extra_field", "failed_with_findings", "unsafe_reason"])
def test_invalid_discovery_archive_is_rejected_by_all_read_and_export_routes(
    tmp_path: Path, invalid_case: str
) -> None:
    run_id = f"synthetic-discovery-invalid-{invalid_case}"
    created = _make_archive(tmp_path, run_id)
    evidence_id, _finding_id = _attach_valid_discovery(tmp_path, run_id, created)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    layer = report["model_discovery"]
    if invalid_case == "source_sha":
        layer["source_sha256"] = "0" * 64
    elif invalid_case == "quote":
        layer["findings"][0]["citations"][0]["quote"] = "not present in the source"
    elif invalid_case == "extra_field":
        layer["unexpected_raw_field"] = "must fail closed"
    elif invalid_case == "failed_with_findings":
        coverage = layer["coverage"]
        assert coverage["planned_batches"] == 1
        pages = coverage["batch_results"][0]["pages"]
        raw_chars = coverage["batch_results"][0]["raw_chars"]
        layer["status"] = "failed"
        layer["reason"] = "all_batches_failed"
        layer["model_call_count"] = 0
        layer["model_call_attempt_count"] = 1
        coverage.update({
            "processed_batches": 1,
            "successful_batches": 0,
            "failed_batches": 1,
            "processed_chars": 0,
            "attempted_chars": raw_chars,
            "failed_pages": pages,
            "unread_pages": pages,
            "complete": False,
            "batch_results": [{
                "batch_index": 1,
                "status": "failed",
                "reason": "model_call_failed",
                "pages": pages,
                "raw_chars": raw_chars,
            }],
        })
    else:
        layer["reason"] = "private error detail must never be persisted"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    client = TestClient(create_app(tmp_path))
    endpoints = [
        f"/v1/annual-analyses/{run_id}",
        f"/v1/annual-analyses/{run_id}/discovery-evidences",
        f"/v1/annual-analyses/{run_id}/discovery-evidences/{evidence_id}/preview.png",
        *(f"/v1/annual-analyses/{run_id}/export?format={format}" for format in ("json", "markdown", "html")),
    ]

    responses = [client.get(endpoint) for endpoint in endpoints]

    assert all(response.status_code in {409, 422} for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert all("private error detail" not in response.text for response in responses)


@pytest.mark.parametrize("invalid_mode", [[], {}, ["integrated_analysis"], 3, "future_mode"])
def test_report_api_maps_non_string_or_unknown_manifest_mode_to_unknown(
    tmp_path: Path, invalid_mode: object
) -> None:
    run_id = "synthetic-unknown-mode-001"
    _make_archive(tmp_path, run_id)
    manifest_path = tmp_path / "artifacts" / "runs" / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["mode"] = invalid_mode
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200
    assert response.json()["manifest"]["mode"] == "unknown"


def test_preview_is_png_and_uses_value_region_page_only(tmp_path: Path) -> None:
    run_id = "synthetic-preview-001"
    created = _make_archive(tmp_path, run_id)
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-pdf-page"] == "2"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    preview = pymupdf.open(stream=response.content, filetype="png")
    assert preview.page_count == 1
    preview.close()
    assert struct.unpack(">II", response.content[16:24]) == (900, 1200)
    assert response.headers["x-report-sha256"] == hashlib.sha256(
        (tmp_path / "artifacts" / "reports" / run_id / "report.json").read_bytes()
    ).hexdigest()
    assert response.headers["x-evidence-kind"] == "verified_financial_value"
    assert response.headers["x-preview-mode"] == "full_page"
    assert json.loads(response.headers["x-preview-region"])["coordinate_space"] == "unrotated_pdf_points"


def test_narrative_index_and_preview_are_source_bound_and_separate_from_verified_allowlist(tmp_path: Path) -> None:
    run_id = "synthetic-narrative-preview-001"
    created = _make_archive(tmp_path, run_id, include_narrative=True)
    source_pdf = pymupdf.open(stream=created["source_pdf_bytes"], filetype="pdf")
    try:
        source_block = next(
            block[4]
            for block in source_pdf[0].get_text("blocks")
            if len(block) >= 5 and "Contract income follows performance." in block[4]
        )
    finally:
        source_pdf.close()
    # The archived excerpt omits only block-edge whitespace, which some
    # PyMuPDF builds return as a trailing line break in get_text("blocks").
    assert source_block.strip() == "Contract income follows performance."
    assert source_block != "Contract income follows performance."
    client = TestClient(create_app(tmp_path))

    index = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")

    assert index.status_code == 200
    payload = index.json()
    assert payload["interpretation_label"] == "原文片段定位/模型观点引用，非独立因果核验"
    assert [item["evidence_id"] for item in payload["items"]] == ["nctx-test-note"]
    item = payload["items"][0]
    assert item["raw_text"] == "Contract income follows performance."
    assert item["cited_by_signal_ids"] == ["revenue_up_cash_down"]
    assert item["preview_url"].endswith("/nctx-test-note/preview.png")

    full = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences/nctx-test-note/preview.png")
    focus = client.get(
        f"/v1/annual-analyses/{run_id}/narrative-evidences/nctx-test-note/preview.png?focus=true"
    )
    assert full.status_code == focus.status_code == 200
    assert full.headers["content-type"] == focus.headers["content-type"] == "image/png"
    assert focus.headers["x-evidence-kind"] == "narrative_context"
    assert focus.headers["x-evidence-interpretation-code"] == (
        "original_text_location_model_opinion_not_independent_causal_verification"
    )
    assert focus.headers["x-preview-mode"] == "focus"
    assert focus.headers["x-report-sha256"] == hashlib.sha256(
        (tmp_path / "artifacts" / "reports" / run_id / "report.json").read_bytes()
    ).hexdigest()
    assert json.loads(focus.headers["x-preview-region"])["coordinate_space"] == "unrotated_pdf_points"
    full_width, full_height = struct.unpack(">II", full.content[16:24])
    focus_width, focus_height = struct.unpack(">II", focus.content[16:24])
    assert focus_height < full_height
    assert focus_width * focus_height <= 12_000_000
    assert focus_width > 0

    amount_with_narrative_id = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/nctx-test-note/preview.png"
    )
    narrative_with_amount_id = client.get(
        f"/v1/annual-analyses/{run_id}/narrative-evidences/ev-independent/preview.png"
    )
    assert amount_with_narrative_id.status_code == 404
    assert amount_with_narrative_id.json()["detail"]["code"] == "evidence_not_found"
    assert narrative_with_amount_id.status_code == 404
    assert narrative_with_amount_id.json()["detail"]["code"] == "narrative_evidence_not_found"


def test_narrative_source_mismatch_and_invalid_section_label_are_safe_errors(tmp_path: Path) -> None:
    run_id = "synthetic-narrative-mismatch-001"
    _make_archive(tmp_path, run_id, include_narrative=True)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    original = json.loads(report_path.read_text(encoding="utf-8"))
    bad_text = json.loads(json.dumps(original))
    bad_text["model_investigation"]["narrative_evidences"][0]["raw_text"] = "forged disclosure"
    report_path.write_text(json.dumps(bad_text), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    mismatch = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "narrative_evidence_source_mismatch"

    bad_section = json.loads(json.dumps(original))
    bad_section["model_investigation"]["narrative_evidences"][0]["section_label"] = []
    report_path.write_text(json.dumps(bad_section), encoding="utf-8")
    malformed = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")
    assert malformed.status_code == 422
    assert malformed.json()["detail"]["code"] == "report_invalid"


def test_reading_context_is_rebuilt_from_pdf_and_ignores_archived_context(tmp_path: Path) -> None:
    run_id = "synthetic-narrative-context-source-bound-001"
    _make_archive(tmp_path, run_id, include_narrative=True)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["model_investigation"]["narrative_evidences"][0]["reading_context"] = {
        "text": "forged text from report JSON",
        "bbox": {"x0": 0, "y0": 0, "x1": 9999, "y1": 9999},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["raw_text"] == "Contract income follows performance."
    assert item["reading_context"]["text"] == "Contract income follows performance."
    assert item["reading_context"]["bbox"] == item["bbox"]
    assert item["reading_context"]["source_blocks"][0]["raw_text"].strip() == item["raw_text"]

    exported = client.get(f"/v1/annual-analyses/{run_id}/export?format=json")
    assert exported.status_code == 200
    assert "reading_context" not in exported.text
    assert "forged text from report JSON" not in exported.text


def test_reading_context_accepts_existing_point_tolerance_but_rejects_real_bbox_mismatch(tmp_path: Path) -> None:
    run_id = "synthetic-narrative-context-bbox-tolerance-001"
    _make_archive(tmp_path, run_id, include_narrative=True)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    original = json.loads(report_path.read_text(encoding="utf-8"))
    bbox = original["model_investigation"]["narrative_evidences"][0]["bbox"]
    bbox["x0"] += 0.05
    report_path.write_text(json.dumps(original), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    compatible = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")
    assert compatible.status_code == 200

    bbox["x0"] += 0.06
    report_path.write_text(json.dumps(original), encoding="utf-8")
    mismatch = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "narrative_evidence_source_mismatch"


def test_focus_preview_handles_rotated_pdf_coordinates(tmp_path: Path) -> None:
    run_id = "synthetic-rotated-focus-001"
    _make_archive(tmp_path, run_id, rotate_value_page=True)
    client = TestClient(create_app(tmp_path))

    full = client.get(f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png")
    focus = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png?focus=true"
    )

    assert full.status_code == focus.status_code == 200
    assert struct.unpack(">II", full.content[16:24]) == (1200, 900)
    focus_width, focus_height = struct.unpack(">II", focus.content[16:24])
    assert 0 < focus_width < 1200
    assert 0 < focus_height < 900


def test_invalid_traversal_run_id_is_rejected(tmp_path: Path) -> None:
    _make_archive(tmp_path, "safe-run")
    client = TestClient(create_app(tmp_path))

    response = client.get("/v1/annual-analyses/" + quote("../outside", safe=""))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_run_id"


def test_unknown_run_id_returns_not_found(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    response = client.get("/v1/annual-analyses/valid-but-missing-run")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "run_not_found"


def test_unknown_or_forged_evidence_id_returns_not_found(tmp_path: Path) -> None:
    run_id = "synthetic-forgery-001"
    _make_archive(tmp_path, run_id)
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-forged/preview.png"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "evidence_not_found"


@pytest.mark.parametrize(
    ("target_type", "target_id"),
    [("calculation", "fact-1"), ("financial_fact", "some-other-object")],
)
def test_wrong_verified_target_cannot_unlock_an_evidence(
    tmp_path: Path,
    target_type: str,
    target_id: str,
) -> None:
    run_id = "synthetic-wrong-target-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    verification = report["confirmed"]["metrics"][0]["verifications"][0]
    verification["target_type"] = target_type
    verification["target_id"] = target_id
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "evidence_not_found"


def test_mismatched_pdf_page_and_value_region_page_are_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-mismatched-page-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["confirmed"]["metrics"][0]["verification_evidences"][0]["pdf_page"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "evidence_region_invalid"


def test_unsupported_report_kind_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-wrong-kind-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["kind"] = "legacy_annual_precheck"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "report_invalid"


def test_source_hash_corruption_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-corruption-001"
    _make_archive(tmp_path, run_id)
    source = tmp_path / "artifacts" / "runs" / run_id / "source.pdf"
    source_bytes = source.read_bytes()
    source.write_bytes(source_bytes[:-1] + bytes([source_bytes[-1] ^ 0x01]))
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_sha256_mismatch"


def test_report_from_another_source_is_rejected(tmp_path: Path) -> None:
    run_a = "synthetic-source-a"
    run_b = "synthetic-source-b"
    _make_archive(tmp_path, run_a, source_text="report A")
    _make_archive(tmp_path, run_b, source_text="report B")
    report_b = tmp_path / "artifacts" / "reports" / run_b / "report.json"
    report_a = tmp_path / "artifacts" / "reports" / run_a / "report.json"
    report_a.write_bytes(report_b.read_bytes())
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_a}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "report_identity_mismatch"


def test_report_source_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-report-hash-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["source_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "report_identity_mismatch"


def test_pdf_from_another_source_is_rejected(tmp_path: Path) -> None:
    run_a = "synthetic-pdf-a"
    run_b = "synthetic-pdf-b"
    _make_archive(tmp_path, run_a, source_text="pdf A")
    _make_archive(tmp_path, run_b, source_text="pdf B")
    source_a = tmp_path / "artifacts" / "runs" / run_a / "source.pdf"
    source_b = tmp_path / "artifacts" / "runs" / run_b / "source.pdf"
    source_a.write_bytes(source_b.read_bytes())
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_a}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_sha256_mismatch"


def test_run_directory_symlink_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-symlink-001"
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    (outside / run_id).mkdir(parents=True)
    runs_root = tmp_path / "artifacts" / "runs"
    runs_root.mkdir(parents=True)
    link = runs_root / run_id
    try:
        link.symlink_to(outside / run_id, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("当前平台不允许创建目录 symlink")
        # A Windows junction is a reparse point and can be created without the
        # elevated privilege required for a directory symlink.
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside / run_id)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not link.exists():
            pytest.skip("当前环境不能创建目录 symlink 或 junction")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "archive_path_invalid"


def test_report_api_projects_strict_archive_snapshot_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    import finagent.api.app as app_module
    from finagent.api.annual_report_read import load_annual_report

    run_id = "synthetic-api-snapshot-001"
    _make_archive(tmp_path, run_id)
    archive = load_annual_report(tmp_path, run_id)
    expected_snapshot = {
        "state": "running",
        "sequence": 2,
        "is_partial": True,
        "parent_run_id": "annual-analysis-parent-001",
    }
    monkeypatch.setattr(
        app_module,
        "load_annual_report",
        lambda _root, _run: replace(archive, snapshot=expected_snapshot),
    )

    response = TestClient(create_app(tmp_path)).get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200, response.text
    assert response.json()["snapshot"] == expected_snapshot


def test_legacy_report_api_does_not_invent_snapshot_metadata(tmp_path: Path) -> None:
    run_id = "synthetic-api-legacy-001"
    _make_archive(tmp_path, run_id)

    response = TestClient(create_app(tmp_path)).get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200, response.text
    assert "snapshot" not in response.json()


def test_v5_failed_audit_reader_rebuilds_attempt_ledger_and_request_binding(tmp_path: Path) -> None:
    run_id = "annual-analysis-failure-ledger-test"
    audit_id = "40000000-0000-4000-8000-000000000001"
    attempt_id = "40000000-0000-4000-8000-000000000002"
    audit_parts = ("artifacts", "runs", run_id, audit_id)
    attempt_dir = tmp_path.joinpath(*audit_parts, "attempt-01")
    attempt_dir.mkdir(parents=True)
    expected_messages = [
        {"role": "system", "content": "local prompt"},
        {"role": "user", "content": "{\"source\":\"known\"}"},
    ]
    (attempt_dir / "request.json").write_text(json.dumps({
        "status": "started", "attempt_index": 1, "audit_id": attempt_id,
        "started_at": "2026-10-08T08:00:00Z", "prompt_version": "annual_review_v3",
        "evidence_refs": [], "model": "local-model", "timeout_seconds": 2.0,
        "messages": expected_messages, "stream": False,
    }), encoding="utf-8")
    (attempt_dir / "failure.json").write_text(json.dumps({
        "status": "failed", "audit_id": attempt_id,
        "category": "call_deadline_exhausted", "provider_admitted": False,
    }), encoding="utf-8")
    failure = {
        "status": "failed", "started_at": "2026-10-08T08:00:00Z",
        "finished_at": "2026-10-08T08:00:00Z", "duration_seconds": 0.0,
        "category": "call_deadline_exhausted", "provider_admission_count": 0,
        "response_count": 0, "attempt_records": [{
            "attempt_index": 1, "audit_id": attempt_id, "admission_id": None,
            "provider_admitted": False, "response_accepted": False,
            "accepted_findings": 0, "status": "rejected", "reason": "call_deadline_exhausted",
        }],
    }

    annual_report_read._validate_audited_failure_record(
        tmp_path, audit_parts, failure, expected_messages=expected_messages,
        expected_model="local-model", expected_prompt_version="annual_review_v3",
        expected_evidence_refs=[], expected_admissions=0, expected_responses=0,
        code="annual_review_audit_invalid",
    )
    tampered = {**failure, "provider_admission_count": 1}
    with pytest.raises(annual_report_read.AnnualReportReadError):
        annual_report_read._validate_audited_failure_record(
            tmp_path, audit_parts, tampered, expected_messages=expected_messages,
            expected_model="local-model", expected_prompt_version="annual_review_v3",
            expected_evidence_refs=[], expected_admissions=0, expected_responses=0,
            code="annual_review_audit_invalid",
        )


def _v5_source_binding_reader_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    from finagent.agents.annual_issue_pipeline import checkpoint_issue_pipeline

    run_id = "annual-analysis-v5-source-binding-test"
    source_pdf = tmp_path / "original.pdf"
    archived_pdf = tmp_path / "source.pdf"
    record_path = tmp_path / "source-record.json"
    source_bytes = b"offline source bytes for source binding contract"
    source_pdf.write_bytes(source_bytes)
    archived_pdf.write_bytes(source_bytes)
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    report: dict[str, object] = {
        "kind": "fintrace_annual_analysis_report", "run_id": run_id,
        "company_id": "600000", "report_year": 2025,
        "source_document_id": "annual-report-2025", "source_sha256": source_sha,
    }
    record = {
        "document_id": report["source_document_id"],
        "company_id": report["company_id"], "report_period": "2025-12-31",
        "sha256": source_sha, "local_path": str(source_pdf),
    }
    record_bytes = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    record_path.write_bytes(record_bytes)
    source_binding = investigation_append._validate_source_binding(
        report, record, record_sha256=hashlib.sha256(record_bytes).hexdigest(),
        source_record_path=record_path, original_pdf_path=source_pdf,
        archived_pdf_path=archived_pdf,
    )
    identity = {
        "run_id": run_id, "company_id": "600000", "report_year": 2025,
        "report_period": "2025-12-31", "source_document_id": "annual-report-2025",
        "source_sha256": source_sha,
    }
    discovery_binding = {
        "source_sha256": source_sha, "company_id": "600000", "report_year": 2025,
        "source_document_id": "annual-report-2025", "analysis_mode": "model_investigation",
        "provider_model_fingerprint": "offline-fake-provider-model", "model": "local-fake-model",
        "plan_version": "annual-discovery-plan-v3", "plan_sha256": "b" * 64,
        "m3_binding_sha256": None,
    }
    execution_binding = {
        "kind": "fintrace_annual_execution_binding_v5", "execution_version": "execution_v5",
        **{key: discovery_binding[key] for key in (
            "source_sha256", "company_id", "report_year", "source_document_id", "analysis_mode",
            "provider_model_fingerprint", "model",
        )},
        "discovery_binding": discovery_binding, "prompts_sha256": "c" * 64,
        "parser_version": "annual-issue-pipeline-parser-v1-execution-v5",
        "issue_pipeline_version": "annual_issue_pipeline_v1",
        "issue_wire_version": ISSUE_WIRE_VERSION,
        "grouping_prompt_version": "annual_issue_grouping_v1",
        "review_prompt_version": "annual_review_v3",
        "review_wire_version": V3_WIRE_SCHEMA,
        "review_input_projection_version": V3_INPUT_PROJECTION_VERSION,
        "plan_version": discovery_binding["plan_version"],
        "plan_sha256": discovery_binding["plan_sha256"],
        "m3_binding_sha256": None,
    }
    discovery: dict[str, object] = {
        "kind": "fintrace_annual_discovery", "run_id": run_id,
        "company_id": "600000", "report_year": 2025,
        "source_document_id": "annual-report-2025", "source_sha256": source_sha,
        "status": "completed", "prompt_version": "annual_discovery_v3",
        "coverage": {"complete": True}, "findings": [], "evidences": [],
        "execution_contract": {
            "kind": "fintrace_annual_discovery_execution_v4",
            "binding": discovery_binding,
        },
    }
    pipeline = checkpoint_issue_pipeline(identity, discovery, execution_binding=execution_binding)
    report["model_issue_pipeline"] = pipeline
    report["model_investigation"] = {
        "source_binding": source_binding, "model_issue_pipeline": pipeline,
    }
    return report, discovery, pipeline, source_binding


def test_v5_issue_pipeline_reader_accepts_source_binding_producer_shape(tmp_path: Path) -> None:
    report, discovery, pipeline, source_binding = _v5_source_binding_reader_fixture(tmp_path)

    assert set(source_binding) == {
        "document_id", "company_id", "report_period", "source_sha256",
        "source_record_sha256", "source_record_path", "source_pdf_path", "archived_pdf_path",
    }
    annual_report_read._validate_v5_issue_pipeline(
        tmp_path, run_id=str(report["run_id"]), report=report,
        discovery=discovery, issue_pipeline=pipeline,
        discovery_evidences=(), parent_archive=None,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("document_id", "other-document"),
        ("company_id", "other-company"),
        ("report_period", "2024-12-31"),
        ("source_sha256", "0" * 64),
    ],
)
def test_v5_issue_pipeline_reader_rejects_source_binding_mismatch(
    tmp_path: Path, field: str, value: str,
) -> None:
    report, discovery, pipeline, source_binding = _v5_source_binding_reader_fixture(tmp_path)
    source_binding[field] = value

    with pytest.raises(annual_report_read.AnnualReportReadError) as exc_info:
        annual_report_read._validate_v5_issue_pipeline(
            tmp_path, run_id=str(report["run_id"]), report=report,
            discovery=discovery, issue_pipeline=pipeline,
            discovery_evidences=(), parent_archive=None,
        )
    assert exc_info.value.code == "annual_issue_pipeline_binding_invalid"


def test_issue_supplement_index_and_preview_are_source_bound_and_keep_raw_text_out_of_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "issue-preview-controlled"
    pdf = pymupdf.open()
    page = pdf.new_page(width=420, height=540)
    page.insert_text((40, 80), "Same source supplement context")
    source_bytes = pdf.tobytes()
    pdf.close()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    raw_text = "Same source supplement context"
    evidence_id = "supplement-test-001"
    source_identity = {
        "run_id": run_id, "company_id": "603288", "report_year": 2024, "report_period": "2024-12-31",
        "source_document_id": "document-test", "source_sha256": source_sha,
    }
    pipeline = {
        "kind": "annual_issue_pipeline_v1", "source_identity": source_identity,
        "supplements": [{
            "evidence_id": evidence_id, "source_document_id": "document-test", "source_sha256": source_sha,
            "pdf_page": 1, "block_index": 0, "start_char": 0, "end_char": len(raw_text),
            "bbox": {"x0": 38.0, "y0": 68.0, "x1": 240.0, "y1": 86.0}, "raw_text": raw_text,
            "section": "management_discussion", "role_status": "accepted",
            "issue_roles": [{"issue_id": "issue:test", "role": "possible_explanation", "reason_code": "states_timing"}],
        }],
    }
    report = {
        "run_id": run_id, "model_issue_pipeline": pipeline,
        "model_investigation": {"model_issue_pipeline": pipeline},
    }
    archive = annual_report_read.AnnualReportArchive(
        run_id=run_id,
        report=report,
        manifest_summary={"document_id": "document-test", "source_pdf_sha256": source_sha, "report_sha256": "b" * 64},
        verification_evidences=(), narrative_evidences=(), source_pdf_bytes=source_bytes, source_provenance={},
    )
    monkeypatch.setattr(app_module, "load_annual_report", lambda _root, _run: archive)
    client = TestClient(create_app(tmp_path))

    report_response = client.get(f"/v1/annual-analyses/{run_id}")
    assert report_response.status_code == 200
    assert raw_text not in report_response.text
    assert '"raw_text"' not in report_response.text
    index_response = client.get(f"/v1/annual-analyses/{run_id}/issue-evidences")
    assert index_response.status_code == 200
    index = index_response.json()
    assert index["source_sha256"] == source_sha
    assert index["report_sha256"] == "b" * 64
    assert index["items"][0]["raw_text"] == raw_text
    assert index["items"][0]["issue_roles"] == pipeline["supplements"][0]["issue_roles"]
    assert index["items"][0]["preview_url"] == f"/v1/annual-analyses/{run_id}/issue-evidences/{evidence_id}/preview.png"
    preview = client.get(f"/v1/annual-analyses/{run_id}/issue-evidences/{evidence_id}/preview.png?focus=true")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.headers["x-evidence-kind"] == "annual_issue_supplement"
    assert preview.headers["x-evidence-interpretation-code"] == "annual_issue_retrieval_unverified_context_possible_explanation_unknown"
    assert preview.headers["x-report-sha256"] == "b" * 64
    assert preview.headers["x-pdf-page"] == "1"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
    missing = client.get(f"/v1/annual-analyses/{run_id}/issue-evidences/foreign-evidence/preview.png")
    assert missing.status_code == 404

    pipeline["supplements"][0]["role_status"] = "pending"
    pipeline["supplements"][0]["issue_roles"] = []
    pending_index = client.get(f"/v1/annual-analyses/{run_id}/issue-evidences")
    assert pending_index.status_code == 200
    assert pending_index.json()["items"][0]["issue_roles"] == []
    assert pending_index.json()["items"][0]["role_status"] == "pending"

    pipeline["supplements"][0]["role_status"] = "accepted"
    accepted_empty_index = client.get(f"/v1/annual-analyses/{run_id}/issue-evidences")
    assert accepted_empty_index.status_code == 422
