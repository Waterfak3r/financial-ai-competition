"""从真实海天原始 PDF 验收完整 v2 CLI 归档。"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "analyze_annual.py"
_PDF = _ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "1222994233.PDF"
_RUN_ID = re.compile(r"\brun_id=(annual-analysis-[0-9a-f-]+)\b")


def test_cli_archives_real_pdf_through_confirmed_v2_report(tmp_path: Path) -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")
    source_sha256 = _sha256(_PDF)
    artifacts = tmp_path / "artifacts"
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--source-pdf",
            str(_PDF),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--document-id",
            "cninfo-1222994233",
            "--artifacts-root",
            str(artifacts),
            "--expected-sha256",
            source_sha256,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    match = _RUN_ID.search(completed.stdout)
    assert match is not None, completed.stdout
    run_id = match.group(1)
    run_dir = artifacts / "runs" / run_id
    report_dir = artifacts / "reports" / run_id
    manifest = _read_json(run_dir / "manifest.json")
    analysis = _read_json(run_dir / "analysis.json")
    report = _read_json(report_dir / "report.json")

    assert manifest["run_id"] == report["run_id"] == run_id
    assert "m3_screening" not in analysis
    assert "m3_screening" not in report
    assert manifest["status"] == "completed"
    assert manifest["inputs"]["source_pdf_sha256"] == source_sha256
    assert manifest["code"]["git_head"]
    assert "scripts/analyze_annual.py" in manifest["code"]["source_file_sha256"]
    assert _sha256(run_dir / "source.pdf") == source_sha256
    assert _sha256(_PDF) == source_sha256
    parsed = _read_json(run_dir / "parsed_text_pdf.json")
    assert parsed["document_id"] == "cninfo-1222994233"
    assert parsed["source_sha256"] == source_sha256

    assert analysis["run_id"] == run_id
    assert analysis["model_called"] is False
    assert len(analysis["facts"]) == 8
    assert len(analysis["evidences"]) >= 16
    fact_verifications = [
        item
        for item in analysis["verifications"]
        if item["target_type"] == "financial_fact"
    ]
    assert len(fact_verifications) == 8
    assert all(item["status"] == "verified" for item in fact_verifications)
    assert len(report["confirmed"]["metrics"]) == 8

    assert len(analysis["calculations"]) == 8
    assert len(analysis["calculation_checks"]) == 8
    assert all(item["status"] == "verified" for item in analysis["calculation_checks"])
    assert len(report["confirmed"]["analyses"]) == 8
    assert len(analysis["screening"]) == 2
    assert all(item["status"] == "candidate" for item in analysis["screening"])
    assert all(
        item["status"] == "candidate"
        for item in report["pending_review"]["candidate_signals"]
    )

    comparability = analysis["comparability"]
    assert comparability["status"] == "verified"
    assert comparability["restatement_status"] == "not_restated"
    assert comparability["evidence"]
    assert report["comparability"]["evidence"]
    markdown = (report_dir / "report.md").read_text(encoding="utf-8")
    assert "## 年度可比性核验" in markdown
    assert "追溯调整状态：`not_restated`" in markdown
    assert "PDF 第" in markdown
    assert "舞弊结论：无" in markdown

    fact_by_id = {item["fact_id"]: item for item in analysis["facts"]}
    verification_by_fact = {item["target_id"]: item for item in fact_verifications}
    claim_by_id = {item["claim_id"]: item for item in analysis["claims"]}
    assert len(analysis["claim_checks"]) == len(analysis["claims"]) == 16
    assert all(item["status"] == "verified" for item in analysis["claim_checks"])
    for fact_id, fact in fact_by_id.items():
        claim = claim_by_id[f"claim:fact:{fact_id}"]
        assert claim["supporting_evidence_ids"] == verification_by_fact[fact_id]["evidence_ids"]
    calculation_by_id = {item["calculation_id"]: item for item in analysis["calculations"]}
    for calculation in analysis["calculations"]:
        claim = claim_by_id[f"claim:calculation:{calculation['calculation_id']}"]
        expected_evidence = {
            evidence_id
            for fact_id in calculation["input_fact_ids"]
            for evidence_id in verification_by_fact[fact_id]["evidence_ids"]
        }
        assert set(claim["supporting_evidence_ids"]) == expected_evidence
        assert claim["calculation_ids"] == [calculation["calculation_id"]]
    assert len(calculation_by_id) == 8


def test_cli_archives_hash_failure_and_rejects_raw_or_traversal_output(tmp_path: Path) -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")
    source_sha256 = _sha256(_PDF)
    artifacts = tmp_path / "hash-failure"
    failed = _invoke(
        tmp_path,
        artifacts,
        _PDF,
        expected_sha256="0" * 64,
    )
    assert failed.returncode == 2
    assert "SHA256 与 --expected-sha256 不一致" in failed.stderr
    match = _RUN_ID.search(failed.stderr)
    assert match is not None, failed.stderr
    run_id = match.group(1)
    run_failure = _read_json(artifacts / "runs" / run_id / "failure.json")
    report_failure = _read_json(artifacts / "reports" / run_id / "failure.json")
    assert run_failure == report_failure
    assert run_failure["status"] == "failed"
    assert run_failure["expected_sha256"] == "0" * 64
    assert run_failure["source_sha256"] == source_sha256
    assert _sha256(artifacts / "runs" / run_id / "source.pdf") == source_sha256

    unsafe = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--source-pdf",
            str(_PDF),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--document-id",
            "cninfo-1222994233",
            "--artifacts-root",
            str(_ROOT / "artifacts" / ".." / "data" / "raw"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unsafe.returncode == 2
    assert "不能包含 .. 路径遍历片段" in unsafe.stderr
    assert _sha256(_PDF) == source_sha256


def test_cli_explicit_model_mode_keeps_deterministic_report_when_audit_root_is_custom(
    tmp_path: Path,
) -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")
    source_sha256 = _sha256(_PDF)
    artifacts = tmp_path / "artifacts"
    source_record = _ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "source.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--source-pdf",
            str(_PDF),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--document-id",
            "cninfo-1222994233",
            "--artifacts-root",
            str(artifacts),
            "--expected-sha256",
            source_sha256,
            "--with-model",
            "--source-record",
            str(source_record),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2, completed.stdout + completed.stderr
    match = _RUN_ID.search(completed.stdout)
    assert match is not None, completed.stdout
    run_id = match.group(1)
    run_dir = artifacts / "runs" / run_id
    report_dir = artifacts / "reports" / run_id
    manifest = _read_json(run_dir / "manifest.json")
    analysis = _read_json(run_dir / "analysis.json")
    report = _read_json(report_dir / "report.json")
    investigation = _read_json(run_dir / "investigation.json")

    assert manifest["status"] == "completed_with_issues"
    assert manifest["model_called"] is False
    assert manifest["model_investigation_status"] == "failed"
    assert manifest["model_investigation"]["output_path"] == f"runs/{run_id}/investigation.json"
    assert manifest["outputs"]["investigation_json"] == f"runs/{run_id}/investigation.json"
    assert investigation["reason"] == "model_requires_repository_artifacts_root"
    assert investigation["model_call_count"] == 0
    assert analysis["model_called"] is False
    assert analysis["scope"]["note"] == report["scope"]["note"]
    assert report["model_called"] is False
    assert report["model_investigation"]["status"] == "failed"
    assert len(report["confirmed"]["metrics"]) == 8
    assert len(report["confirmed"]["analyses"]) == 8
    markdown = (report_dir / "report.md").read_text(encoding="utf-8")
    assert "## M3 年报文本调查" in markdown
    assert "报告不调用模型" not in markdown


def test_cli_explicit_m3_screening_archives_real_pdf_and_read_api_accepts_report(
    tmp_path: Path,
) -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")
    from finagent.api.annual_report_read import load_annual_report

    source_sha256 = _sha256(_PDF)
    artifacts = tmp_path / "artifacts"
    completed = _invoke(
        tmp_path,
        artifacts,
        _PDF,
        expected_sha256=source_sha256,
        extra_args=("--with-m3-screening",),
    )

    assert completed.returncode == 0, completed.stderr
    match = _RUN_ID.search(completed.stdout)
    assert match is not None, completed.stdout
    run_id = match.group(1)
    run_dir = artifacts / "runs" / run_id
    report_dir = artifacts / "reports" / run_id
    manifest = _read_json(run_dir / "manifest.json")
    analysis = _read_json(run_dir / "analysis.json")
    report = _read_json(report_dir / "report.json")
    m3 = _read_json(run_dir / "m3_screening.json")
    markdown = (report_dir / "report.md").read_text(encoding="utf-8")

    assert manifest["status"] == "completed"
    assert manifest["model_called"] is False
    assert manifest["m3_screening"]["status"] == "completed"
    assert manifest["m3_screening"]["total_score"] == 0
    assert manifest["rules"]["m3_screening_version"] == "m3-four-annual-rules-v1.0.0-trial"
    assert manifest["outputs"]["m3_screening_json"] == f"runs/{run_id}/m3_screening.json"
    assert _sha256(run_dir / "source.pdf") == source_sha256
    assert _sha256(_PDF) == source_sha256

    # 默认 M2 的对象数量和结构保持原样，M3 结果仅放在独立附加区。
    assert len(analysis["facts"]) == 8
    assert len(analysis["calculations"]) == 8
    assert len(analysis["claims"]) == 16
    assert len(report["confirmed"]["metrics"]) == 8
    assert len(report["confirmed"]["analyses"]) == 8
    assert report["m3_screening"] == analysis["m3_screening"] == m3

    assert m3["status"] == "completed"
    assert m3["source_sha256"] == source_sha256
    assert m3["source_document_id"] == "cninfo-1222994233"
    assert m3["model_called"] is False
    assert m3["extraction"]["expected_new_fact_count"] == 10
    assert m3["extraction"]["new_fact_count"] == 10
    assert m3["explicit_unit_facts"]["fact_count"] == 8
    assert not m3["extraction"]["issues"]
    assert not m3["explicit_unit_facts"]["issues"]
    assert m3["comparability_proof_audit"]["status"] == "verified"
    assert m3["parent_profit_semantic_mapping_audit"]["status"] == "verified"
    assert m3["screening"]["status"] == "completed"
    assert m3["screening"]["total_score"] == 0
    assert len(m3["screening"]["rules"]) == 4
    assert all(rule["status"] == "calculable" for rule in m3["screening"]["rules"])
    assert all(rule["triggered"] is False and rule["points"] == 0 for rule in m3["screening"]["rules"])

    expected_metrics = {
        "accounts_receivable_net",
        "inventory_net",
        "net_profit_consolidated",
        "cost_of_goods_sold",
        "net_profit_parent_ex_nonrecurring",
    }
    assert {fact["metric_id"] for fact in m3["extraction"]["facts"]} == expected_metrics
    assert all(fact["source_sha256"] == source_sha256 for fact in m3["extraction"]["facts"])
    assert len(m3["verification"]["results"]) == 18
    assert all(item["status"] == "verified" for item in m3["verification"]["results"])
    nonrecurring_copies = [
        fact for fact in m3["explicit_unit_facts"]["facts"] if fact["metric_id"] == "non_recurring_total"
    ]
    assert len(nonrecurring_copies) == 2
    used_fact_ids = {item for rule in m3["screening"]["rules"] for item in rule["input_fact_ids"]}
    assert all(fact["fact_id"] not in used_fact_ids for fact in nonrecurring_copies)
    facts_by_id = {
        fact["fact_id"]: fact
        for key in ("extraction", "explicit_unit_facts")
        for fact in m3[key]["facts"]
    }
    evidence_by_id = {
        evidence["evidence_id"]: evidence
        for evidence in (
            *m3["extraction"]["evidences"],
            *m3["explicit_unit_facts"]["evidences"],
            *m3["verification"]["independent_evidences"],
        )
    }
    for verification in m3["verification"]["results"]:
        fact = facts_by_id[verification["target_id"]]
        assert not set(fact["evidence_ids"]).intersection(verification["evidence_ids"])
        assert all(evidence_by_id[item]["source_sha256"] == source_sha256 for item in verification["evidence_ids"])

    screening = m3["screening"]
    assert f"汇总分数：`{screening['total_score']}/{screening['maximum_score']}`" in markdown
    assert "M3 状态：`completed`" in markdown
    assert "最多保留 6 位小数；筛查判定使用未舍入值，JSON 保留计算原值" in markdown
    assert all(rule["rule_id"] in markdown for rule in screening["rules"])
    assert "风险线索不是确认舞弊" in markdown
    ratio_values = {
        rule["rule_id"]: rule["calculated_value"]
        for rule in screening["rules"]
        if rule["rule_id"] in {
            "deducted_parent_profit_share",
            "receivables_growth_gap",
            "inventory_growth_gap",
        }
    }
    assert "`0.043301`" in markdown
    assert "`-0.009701`" in markdown
    assert "`-0.093088`" in markdown
    assert all(value in json.dumps(screening, ensure_ascii=False) for value in ratio_values.values())
    assert all(value not in markdown for value in ratio_values.values())

    # 只读报告 API 允许额外的 M3 报告区，仍按现有规则验证 M2 confirmed evidence。
    archive = load_annual_report(tmp_path, run_id)
    assert archive.report["m3_screening"]["screening"]["total_score"] == 0
    assert archive.verification_evidences

    from fastapi.testclient import TestClient

    from finagent.api.app import create_app

    response = TestClient(create_app(tmp_path)).get(f"/v1/annual-analyses/{run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["report"]["m3_screening"]["screening"]["total_score"] == 0


def test_cli_m3_source_hash_failure_is_explicitly_archived(tmp_path: Path) -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")
    artifacts = tmp_path / "artifacts"
    failed = _invoke(
        tmp_path,
        artifacts,
        _PDF,
        expected_sha256="0" * 64,
        extra_args=("--with-m3-screening",),
    )
    assert failed.returncode == 2
    match = _RUN_ID.search(failed.stderr)
    assert match is not None, failed.stderr
    run_id = match.group(1)
    run_dir = artifacts / "runs" / run_id
    manifest = _read_json(run_dir / "manifest.json")
    failure = _read_json(run_dir / "failure.json")
    m3 = _read_json(run_dir / "m3_screening.json")
    assert manifest["status"] == "failed"
    assert manifest["m3_screening"]["status"] == "failed"
    assert failure["m3_screening_status"] == "failed"
    assert m3["status"] == "failed"
    assert "--expected-sha256" in m3["failure"]["reason"]
    assert m3["screening"]["total_score"] is None


def test_cli_rejects_model_and_deterministic_m3_in_one_run(tmp_path: Path) -> None:
    if not _PDF.is_file():
        pytest.skip("本机没有海天 2024 原始 PDF。")
    source_record = _ROOT / "data" / "raw" / "603288" / "2024" / "cninfo-1222994233" / "source.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--source-pdf",
            str(_PDF),
            "--company-id",
            "603288",
            "--report-year",
            "2024",
            "--document-id",
            "cninfo-1222994233",
            "--artifacts-root",
            str(tmp_path / "artifacts"),
            "--with-model",
            "--source-record",
            str(source_record),
            "--with-m3-screening",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "--with-model 与 --with-m3-screening 暂不支持同次运行" in completed.stderr
    assert not (tmp_path / "artifacts" / "runs").exists()


def _invoke(
    cwd: Path,
    artifacts: Path,
    source_pdf: Path,
    *,
    expected_sha256: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(_SCRIPT),
        "--source-pdf",
        str(source_pdf),
        "--company-id",
        "603288",
        "--report-year",
        "2024",
        "--document-id",
        "cninfo-1222994233",
        "--artifacts-root",
        str(artifacts),
    ]
    if expected_sha256 is not None:
        command.extend(("--expected-sha256", expected_sha256))
    command.extend(extra_args)
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
