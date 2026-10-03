from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pymupdf
import pytest
from fastapi.testclient import TestClient

from finagent.api.annual_report_export import AnnualReportExportError, export_annual_report
from finagent.api.annual_report_read import AnnualReportReadError
from finagent.api.app import create_app


def _archive(root: Path, run_id: str = "export-test-001", *, mode: str = "integrated_analysis") -> dict[str, object]:
    run_dir = root / "artifacts" / "runs" / run_id
    report_dir = root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    pdf = pymupdf.open()
    page = pdf.new_page(width=420, height=540)
    page.insert_text((42, 90), "模拟年报：营业收入 1000 万元")
    source = pdf.tobytes()
    pdf.close()
    (run_dir / "source.pdf").write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    evidence = {
        "evidence_id": "ev-export-001",
        "document_id": "doc-export-001",
        "source_sha256": digest,
        "pdf_page": 1,
        "printed_page": None,
        "value_raw": "1000",
        "value_normalized": "10000000",
        "unit": "万元",
        "currency": "人民币",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "period_type": "annual",
        "value_region": {"page": 1, "bbox": {"x0": 40, "y0": 60, "x1": 250, "y1": 100}, "text": "1000"},
        "row_region": {"page": 1, "bbox": {"x0": 40, "y0": 60, "x1": 250, "y1": 100}, "text": "营业收入"},
        "column_region": {"page": 1, "bbox": {"x0": 40, "y0": 60, "x1": 250, "y1": 100}, "text": "2025 年"},
        "title_region": {"page": 1, "bbox": {"x0": 40, "y0": 60, "x1": 250, "y1": 100}, "text": "合并利润表"},
        "unit_region": {"page": 1, "bbox": {"x0": 40, "y0": 60, "x1": 250, "y1": 100}, "text": "万元"},
        "limitations": [],
    }
    manifest = {
        "run_id": run_id,
        "mode": mode,
        "status": "completed_with_issues",
        "company_id": "TEST-COMPANY-001",
        "report_year": 2025,
        "document_id": "doc-export-001",
        "inputs": {
            "source_pdf_original_path": "D:\\private\\never-export-this.pdf",
            "source_pdf_archive_path": f"runs/{run_id}/source.pdf",
            "source_pdf_sha256": digest,
        },
        "outputs": {
            "run_dir": f"runs/{run_id}",
            "report_dir": f"reports/{run_id}",
            "report_json": f"reports/{run_id}/report.json",
        },
    }
    metric = {
        "fact_id": "fact-revenue-2025",
        "metric_id": "revenue",
        "label_raw": "營業收入",
        "raw_value": "1000",
        "normalized_value": "10000000",
        "currency": "人民幣",
        "unit_multiplier": "10000",
        "unit": "萬元",
        "report_year": 2025,
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "period_type": "annual",
        "scope": "consolidated",
        "comparison_role": "current",
        "source_document_id": "doc-export-001",
        "source_sha256": digest,
        "evidences": [evidence],
        "verification_evidences": [evidence],
        "verifications": [{
            "verification_id": "verify-revenue-001",
            "target_type": "financial_fact",
            "target_id": "fact-revenue-2025",
            "status": "verified",
            "evidence_ids": ["ev-export-001"],
            "checks": ["值与原文相符"],
            "conflicts": [],
            "limitations": [],
        }],
        "limitations": [],
        "placement_reasons": [],
        "api_key": "must-not-export-from-extension",
    }
    report: dict[str, object] = {
        "kind": "fintrace_annual_analysis_report",
        "title": "FINTRACE 年度财报分析报告",
        "run_id": run_id,
        "company_id": "TEST-COMPANY-001",
        "report_year": 2025,
        "source_document_id": "doc-export-001",
        "source_sha256": digest,
        "fraud_conclusion": None,
        "scope": {"note": "核验记录；未确认因果关系。", "gaps": ["比较期证据不足。"]},
        "comparability": {
            "verification_id": "comparability-2025-export",
            "document_id": "doc-export-001",
            "source_sha256": digest,
            "report_year": 2025,
            "current_year": 2025,
            "comparative_year": 2024,
            "status": "verified",
            "restatement_status": "not_restated",
            "evidence": [{
                "evidence_id": "comp-export-001",
                "topic": "会计政策变更",
                "document_id": "doc-export-001",
                "source_sha256": digest,
                "report_year": 2025,
                "current_year": 2025,
                "comparative_year": 2024,
                "pdf_page": 1,
                "bbox": {"x0": 35, "y0": 110, "x1": 300, "y1": 160},
                "text": "当前归档原文中的会计政策变更说明。",
                "report_year_page": 1,
                "report_year_bbox": {"x0": 35, "y0": 20, "x1": 300, "y1": 50},
                "report_year_text": "2025 年年度报告",
            }],
            "checks": ["本期及比较期均有原文声明。"],
            "conflicts": [],
            "limitations": ["可比性不证明因果。"],
        },
        "confirmed": {"metrics": [metric], "analyses": []},
        "verified_claims": [],
        "interpretations": [],
        "pending_review": {
            "facts": [], "calculations": [], "claims": [],
            "candidate_signals": [{
                "signal_id": "candidate-001", "status": "candidate",
                "title": "<script>alert(1)</script> [open](javascript:alert(1))",
                "reason": "未确认成因。", "input_fact_ids": ["fact-revenue-2025"],
                "calculation_ids": [], "left_difference": None, "right_difference": None,
                "fraud_conclusion": None, "placement_reasons": ["待进一步核查。"],
            }],
            "uncited_evidences": [],
        },
        "limitations": ["候选线索不是确认舞弊。"],
        "model_called": True,
        "provider_request": {"api_key": "must-not-export"},
        "audit_request_body": {"prompt": "must-not-export"},
    }
    if mode in {"m3_screening", "integrated_analysis"}:
        report["m3_screening"] = {
            "kind": "fintrace_m3_annual_screening", "run_id": run_id,
            "company_id": "TEST-COMPANY-001", "report_year": 2025,
            "source_document_id": "doc-export-001", "source_sha256": digest,
            "status": "completed_with_issues", "rule_version": "trial-v1",
            "model_called": False,
            "screening": {"status": "abstained", "rule_version": "trial-v1", "total_score": None, "maximum_score": 100, "rules": [
                {"rule_id": "rule-1", "status": "insufficient_evidence", "triggered": None, "points": None,
                 "calculated_value": None, "threshold": "trial", "formula": "known formula", "input_fact_ids": [],
                 "issues": [{"code": "missing", "message": "资料缺失。", "api_key": "hidden nested secret"}],
                 "provider_response": {"secret": "not copied"}}
            ]},
            "failure": None,
        }
    if mode in {"model_investigation", "integrated_analysis"}:
        source_identity = {
            "run_id": run_id, "company_id": "TEST-COMPANY-001", "report_year": 2025,
            "source_document_id": "doc-export-001", "source_sha256": digest,
        }
        report["model_investigation"] = {
            "kind": "fintrace_annual_investigation", **source_identity,
            "status": "completed_with_issues", "model_called": True, "model_call_count": 3,
            "model_call_attempt_count": 4, "investigation_call_count": 2,
            "investigation_call_attempt_count": 3, "model_review_call_count": 1,
            "model_review_call_attempt_count": 1,
            "items": [{"signal_id": "candidate-001", "status": "interpretation", "explanation": "这是未经独立核验的模型观点。", "narrative_evidence_ids": []}],
            "audit_artifacts": [{"request_path": "private/audit.json", "request_body": {"api_key": "not-exported"}}],
            "final_review": {
                "status": "completed", "assessment": "insufficient_evidence", "summary": "最终评审仍是模型观点。",
                "source_identity": source_identity, "model_called": True, "call_count": 1,
                "call_attempt_count": 1, "reasons": [{"text": "现有证据不足。", "evidence_ids": ["ev-export-001"]}],
                "follow_up_items": [], "limitations": ["未作独立因果核验。"],
                "audit_artifacts": [{"request_body": "must-not-export"}],
            },
        }
        report["model_review"] = report["model_investigation"]["final_review"]
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return {"run_id": run_id, "digest": digest, "report": report, "metric": metric}


def test_json_export_projects_four_bound_layers_and_strips_extensions(tmp_path: Path) -> None:
    created = _archive(tmp_path)
    exported = export_annual_report(tmp_path, str(created["run_id"]), "json")
    payload = json.loads(exported.content)
    report = payload["report"]

    assert payload["manifest"]["status"] == "completed_with_issues"
    assert report["confirmed"]["metrics"][0]["raw_value"] == "1000"
    assert report["confirmed"]["metrics"][0]["verification_evidences"][0]["evidence_id"] == "ev-export-001"
    assert payload["evidence_index"]["verification_evidences"][0]["pdf_page"] == 1
    assert report["m3_screening"]["source_binding_status"] == "matched"
    assert report["m3_screening"]["screening"]["total_score"] is None
    assert report["m3_screening"]["screening"]["rules"][0]["status"] == "insufficient_evidence"
    assert report["model_investigation"]["source_binding_status"] == "matched"
    assert report["model_investigation"]["status"] == "completed_with_issues"
    assert report["model_investigation"]["model_call_count"] == 3
    assert report["model_investigation"]["model_call_attempt_count"] == 4
    assert report["model_investigation"]["investigation_call_count"] == 2
    assert report["model_investigation"]["investigation_call_attempt_count"] == 3
    assert report["model_investigation"]["model_review_call_count"] == 1
    assert report["model_investigation"]["model_review_call_attempt_count"] == 1
    assert report["model_review"]["source_binding_status"] == "matched"
    assert report["model_review"]["assessment"] == "insufficient_evidence"
    assert report["comparability"]["status"] == "verified"
    assert report["comparability"]["source_binding_status"] == "matched"
    assert report["comparability"]["evidence"][0]["evidence_id"] == "comp-export-001"
    assert "api_key" not in exported.content.decode("utf-8")
    assert "provider_request" not in exported.content.decode("utf-8")
    assert "audit_artifacts" not in exported.content.decode("utf-8")
    assert "request_body" not in exported.content.decode("utf-8")
    assert "D:\\private" not in exported.content.decode("utf-8")


def test_html_and_markdown_escape_untrusted_html_and_links(tmp_path: Path) -> None:
    _archive(tmp_path)
    markdown = export_annual_report(tmp_path, "export-test-001", "markdown").content.decode("utf-8")
    html = export_annual_report(tmp_path, "export-test-001", "html").content.decode("utf-8")

    assert "<script>" not in markdown
    assert "javascript:alert(1)" not in markdown or "\\[open\\]" in markdown
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "javascript:alert(1)" in html
    assert "<script" not in html
    assert "@media print" in html
    assert "A4 landscape" in html
    assert "Content-Security-Policy" in html or "default-src &#x27;none&#x27;" in html
    assert "https://" not in html
    assert "用浏览器打印成 PDF" in html
    assert "<td>已核实</td><td>營業收入</td>" in html
    assert "总调用次数（含最终评审）" in html
    assert "调查调用次数" in html and ">2</td>" in html
    assert "最终评审调用次数" in html and ">1</td>" in html
    assert "总调用尝试次数（含最终评审）" in html and ">4</td>" in html


def test_call_counter_projection_rejects_bool_negative_and_noninteger_values(tmp_path: Path) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    investigation = report["model_investigation"]
    investigation["model_call_count"] = True
    investigation["investigation_call_count"] = -1
    investigation["investigation_call_attempt_count"] = "3"
    investigation["model_review_call_count"] = {"api_key": "do-not-project"}
    investigation["model_review_call_attempt_count"] = 9_007_199_254_740_992
    investigation["model_call_attempt_count"] = 4
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    content = export_annual_report(tmp_path, str(created["run_id"]), "json").content.decode("utf-8")
    projected = json.loads(content)["report"]["model_investigation"]
    assert projected["model_call_attempt_count"] == 4
    assert "model_call_count" not in projected
    assert "investigation_call_count" not in projected
    assert "investigation_call_attempt_count" not in projected
    assert "model_review_call_count" not in projected
    assert "model_review_call_attempt_count" not in projected
    assert "api_key" not in content


def test_absent_call_counter_splits_stay_absent_in_json_and_html(tmp_path: Path) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    investigation = report["model_investigation"]
    for field in (
        "investigation_call_count", "investigation_call_attempt_count",
        "model_review_call_count", "model_review_call_attempt_count",
    ):
        investigation.pop(field)
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    payload = json.loads(export_annual_report(tmp_path, str(created["run_id"]), "json").content)
    projected = payload["report"]["model_investigation"]
    assert "model_call_count" in projected
    assert all(field not in projected for field in (
        "investigation_call_count", "investigation_call_attempt_count",
        "model_review_call_count", "model_review_call_attempt_count",
    ))
    html = export_annual_report(tmp_path, str(created["run_id"]), "html").content.decode("utf-8")
    assert "总调用次数（含最终评审）" in html
    assert "调查调用次数" not in html
    assert "最终评审调用次数" not in html


def test_investigation_only_html_does_not_claim_review_calls(tmp_path: Path) -> None:
    created = _archive(tmp_path, mode="model_investigation")
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("model_review")
    investigation = report["model_investigation"]
    investigation.pop("final_review")
    investigation["model_call_count"] = 2
    investigation["model_call_attempt_count"] = 2
    investigation.pop("model_review_call_count")
    investigation.pop("model_review_call_attempt_count")
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    html = export_annual_report(tmp_path, str(created["run_id"]), "html").content.decode("utf-8")
    assert "调查调用次数（调查层归档值）" in html
    assert "总调用次数（含最终评审）" not in html
    assert "最终评审调用次数" not in html


def test_html_uses_report_section_for_fact_and_calculation_status_without_placement(tmp_path: Path) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["confirmed"]["analyses"].append({
        "calculation_id": "calc-confirmed-001", "formula_id": "revenue-change",
        "formula_expression": "(本期-比较期)/比较期", "input_fact_ids": ["fact-revenue-2025"],
        "output_value": "25%", "unit": None, "status": "verified",
        "independent_verification": {"status": "verified", "recomputed_value": "25%"},
    })
    report["pending_review"]["facts"].append({
        **report["confirmed"]["metrics"][0], "fact_id": "fact-pending-001", "metric_id": "pending_metric",
        "label_raw": "待核查事实",
    })
    report["pending_review"]["calculations"].append({
        "calculation_id": "calc-pending-001", "formula_id": "pending-formula",
        "formula_expression": "待核查表达式", "input_fact_ids": [], "output_value": None,
        "unit": None, "status": "insufficient_evidence",
    })
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    html = export_annual_report(tmp_path, str(created["run_id"]), "html").content.decode("utf-8")
    assert "<td>已核实</td><td>營業收入</td>" in html
    assert "<td>待核查</td><td>待核查事实</td>" in html
    assert "<td>已核实</td><td>revenue-change</td>" in html
    assert "<td>待核查</td><td>pending-formula</td>" in html


def test_allowlisted_keys_do_not_allow_nested_secret_objects(tmp_path: Path) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["title"] = {"api_key": "hidden title object"}
    report["scope"]["note"] = {"request_body": "hidden note object"}
    report["scope"]["gaps"].append({"response": {"secret": "hidden nested gap"}})
    report["m3_screening"]["screening"]["rules"][0]["issues"][0]["api_key"] = "another hidden key"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    content = export_annual_report(tmp_path, str(created["run_id"]), "json").content.decode("utf-8")
    payload = json.loads(content)

    assert "title" not in payload["report"]
    assert "note" not in payload["report"]["scope"]
    assert "api_key" not in content
    assert "request_body" not in content
    assert "hidden nested gap" not in content


def test_mismatched_layer_identity_keeps_failure_state_without_stale_opinion(tmp_path: Path) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["m3_screening"]["source_sha256"] = "0" * 64
    report["model_review"]["source_identity"]["source_document_id"] = "different-document"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    exported = json.loads(export_annual_report(tmp_path, str(created["run_id"]), "json").content)
    assert exported["report"]["m3_screening"]["status"] == "source_mismatch"
    assert "screening" not in exported["report"]["m3_screening"]
    assert exported["report"]["model_review"]["status"] == "source_mismatch"
    assert "summary" not in exported["report"]["model_review"]
    assert exported["report"]["model_investigation"]["status"] == "completed_with_issues"


@pytest.mark.parametrize("format", ["json", "markdown", "html"])
def test_comparability_source_mismatch_hides_bound_opinion_and_evidence(tmp_path: Path, format: str) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["comparability"]["source_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    exported = export_annual_report(tmp_path, str(created["run_id"]), format).content.decode("utf-8")
    if format == "json":
        comparability = json.loads(exported)["report"]["comparability"]
        assert comparability["status"] == "source_mismatch"
        assert comparability["source_binding_status"] == "mismatch"
        assert comparability["evidence"] == []
        assert comparability["verification_id"] is None
    else:
        assert "source_mismatch" in exported
        assert "comp-export-001" not in exported
        assert "当前归档原文中的会计政策变更说明" not in exported
        assert "本期及比较期均有原文声明。" not in exported


@pytest.mark.parametrize("format", ["json", "markdown", "html"])
def test_comparability_missing_source_binding_is_unverified(tmp_path: Path, format: str) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["comparability"] = {
        "status": "verified",
        "verification_id": "legacy-unbound-comparability",
        "evidence": [{"evidence_id": "unbound-proof", "pdf_page": 1, "text": "无绑定记录不能作为证据。"}],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    exported = export_annual_report(tmp_path, str(created["run_id"]), format).content.decode("utf-8")
    if format == "json":
        comparability = json.loads(exported)["report"]["comparability"]
        assert comparability["status"] == "unverified"
        assert comparability["source_binding_status"] == "unverified"
        assert comparability["evidence"] == []
        assert comparability["verification_id"] is None
    else:
        assert "unverified" in exported
        assert "unbound-proof" not in exported
        assert "无绑定记录不能作为证据。" not in exported


@pytest.mark.parametrize("format", ["json", "markdown", "html"])
def test_top_level_review_binding_is_authoritative_in_every_export(tmp_path: Path, format: str) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["model_review"]["source_identity"]["source_document_id"] = "different-document"
    # Keep the nested compatibility copy matched to prove it cannot mask the authoritative state.
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    exported = export_annual_report(tmp_path, str(created["run_id"]), format).content.decode("utf-8")
    if format == "json":
        projected = json.loads(exported)["report"]
        assert projected["model_review"]["status"] == "source_mismatch"
        assert projected["model_investigation"]["final_review"]["status"] == "source_mismatch"
        assert "summary" not in projected["model_investigation"]["final_review"]
    else:
        assert "source_mismatch" in exported
        assert "最终评审仍是模型观点。" not in exported


@pytest.mark.parametrize("format", ["json", "markdown", "html"])
def test_nested_review_is_compatibility_fallback_without_top_level_review(tmp_path: Path, format: str) -> None:
    created = _archive(tmp_path)
    report_path = tmp_path / "artifacts" / "reports" / str(created["run_id"]) / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("model_review")
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    exported = export_annual_report(tmp_path, str(created["run_id"]), format).content.decode("utf-8")
    if format == "json":
        projected = json.loads(exported)["report"]
        assert "model_review" not in projected
        assert projected["model_investigation"]["final_review"]["summary"] == "最终评审仍是模型观点。"
    else:
        assert "最终评审仍是模型观点。" in exported


@pytest.mark.parametrize("mode", ["deterministic", "m3_screening", "model_investigation", "integrated_analysis"])
def test_mode_layer_statuses_are_not_collapsed(tmp_path: Path, mode: str) -> None:
    created = _archive(tmp_path, f"export-{mode}", mode=mode)
    report = json.loads(export_annual_report(tmp_path, str(created["run_id"]), "json").content)["report"]
    assert (report.get("m3_screening") is not None) == (mode in {"m3_screening", "integrated_analysis"})
    assert (report.get("model_investigation") is not None) == (mode in {"model_investigation", "integrated_analysis"})


def test_api_download_headers_format_validation_and_missing_archives(tmp_path: Path) -> None:
    _archive(tmp_path, "export-http-001", mode="deterministic")
    client = TestClient(create_app(tmp_path))
    expectations = {
        "json": "application/json",
        "markdown": "text/markdown",
        "html": "text/html",
    }
    for format, content_type in expectations.items():
        response = client.get(f"/v1/annual-analyses/export-http-001/export?format={format}")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(content_type)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-disposition"].endswith(f".{ 'md' if format == 'markdown' else format}\"")
        if format == "html":
            assert "default-src 'none'" in response.headers["content-security-policy"]

    bad_format = client.get("/v1/annual-analyses/export-http-001/export?format=pdf")
    assert bad_format.status_code == 422
    assert bad_format.json()["detail"]["code"] == "unsupported_export_format"
    missing = client.get("/v1/annual-analyses/missing-export-run/export?format=json")
    assert missing.status_code == 404


def test_run_id_traversal_and_source_hash_changes_fail_closed(tmp_path: Path) -> None:
    _archive(tmp_path, "safe-export-001", mode="deterministic")
    with pytest.raises(AnnualReportReadError) as invalid:
        export_annual_report(tmp_path, "../outside", "json")
    assert invalid.value.status_code == 400

    source_path = tmp_path / "artifacts" / "runs" / "safe-export-001" / "source.pdf"
    source_path.write_bytes(source_path.read_bytes() + b"tampered")
    with pytest.raises(AnnualReportReadError) as changed:
        export_annual_report(tmp_path, "safe-export-001", "json")
    assert changed.value.code == "source_sha256_mismatch"


def test_missing_required_archive_is_not_exported(tmp_path: Path) -> None:
    with pytest.raises(AnnualReportReadError) as missing:
        export_annual_report(tmp_path, "nonexistent-export", "json")
    assert missing.value.status_code == 404


def test_unknown_format_is_rejected_before_archive_lookup(tmp_path: Path) -> None:
    with pytest.raises(AnnualReportExportError) as rejected:
        export_annual_report(tmp_path, "missing", "pdf")
    assert rejected.value.status_code == 422


def test_encoded_path_like_route_segment_cannot_export_another_archive(tmp_path: Path) -> None:
    _archive(tmp_path, "safe-http-export", mode="deterministic")
    client = TestClient(create_app(tmp_path))
    response = client.get("/v1/annual-analyses/" + quote("../safe-http-export", safe="") + "/export?format=json")
    assert response.status_code >= 400
