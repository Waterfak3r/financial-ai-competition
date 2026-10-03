"""季度累计提取、差分、proof 防篡改及比较弃权测试。"""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from finagent.finance.quarterly import QuarterlyCalculationError, compare_quarters, derive_quarter
from finagent.ingestion.quarterly import QuarterlyIngestionError, load_quarterly_manifest
from finagent.schemas.quarterly import DerivedQuarter, QuarterlyProof, QuarterlySourceDocument
from finagent.verification import quarterly as verifier
from finagent.verification.quarterly import (
    is_current_quarterly_proof,
    verify_comparability,
    verify_cumulative_observation,
    verify_derived_quarter,
    verify_quarter_comparison,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "samples" / "quarterly" / "source_manifest.json"
METRICS = ("revenue", "net_profit_parent", "operating_cash_flow")


@pytest.fixture(scope="module")
def sample_chain():
    ingestion = load_quarterly_manifest(MANIFEST, project_root=ROOT)
    source_proofs = []
    for observation in ingestion.observations:
        result, proof = verify_cumulative_observation(
            ingestion.document_paths[observation.source_document_id], observation
        )
        assert result.status == "verified", result.to_dict()
        assert proof is not None
        source_proofs.append(proof)

    derived = {}
    derived_proofs = []
    for year, quarter in ((2023, "Q2"), (2024, "Q1"), (2024, "Q2")):
        for metric in METRICS:
            candidate = derive_quarter(
                ingestion.observations,
                company_id=ingestion.manifest["company_id"],
                metric_id=metric,
                year=year,
                quarter=quarter,
            )
            result, proof = verify_derived_quarter(candidate, ingestion.observations, source_proofs, ingestion.document_paths)
            assert result.status == "verified", result.to_dict()
            assert proof is not None
            derived[(year, quarter, metric)] = candidate
            derived_proofs.append(proof)
    return SimpleNamespace(
        ingestion=ingestion,
        source_proofs=tuple(source_proofs),
        derived=derived,
        derived_proofs=tuple(derived_proofs),
    )


def _derived(year: int, quarter: str, value: str, *, metric: str = "revenue", **overrides) -> DerivedQuarter:
    payload = {
        "derived_id": f"test-{year}-{quarter}-{metric}-{value}",
        "metric_id": metric,
        "company_id": "SIM-QUARTERLY-001",
        "year": year,
        "quarter": quarter,
        "normalized_value": value,
        "currency": "CNY",
        "unit": "元",
        "unit_multiplier": "1",
        "statement_type": "cash_flow_statement" if metric == "operating_cash_flow" else "income_statement",
        "scope": "consolidated",
        "formula_id": "test",
        "formula_expression": "test",
        "input_observation_ids": ("test-observation",),
        "input_source_document_ids": ("test-document",),
    }
    payload.update(overrides)
    return DerivedQuarter(**payload)


def _observation(sample_chain, *, year: int = 2024, period: str = "H1", metric: str = "revenue", role: str = "current"):
    observation_id = f"qobs-sim-{year}-{period.lower()}-{metric}-{year if role == 'current' else year - 1}-{role}"
    return next(item for item in sample_chain.ingestion.observations if item.observation_id == observation_id)


def test_sample_manifest_and_source_rows_are_verified(sample_chain) -> None:
    assert len(sample_chain.ingestion.documents) == 4
    assert len(sample_chain.ingestion.observations) == 24
    assert len(sample_chain.source_proofs) == 24
    assert all(len(proof.source_sha256s) == 1 for proof in sample_chain.source_proofs)
    assert all(observation.scope == "consolidated" for observation in sample_chain.ingestion.observations)


@pytest.mark.parametrize("field,value", [
    ("scope", "unknown"),
    ("statement_type", "unknown"),
    ("unit_multiplier", "0"),
])
def test_derive_rejects_unknown_or_invalid_measurement_context(sample_chain, field: str, value: str) -> None:
    original = _observation(sample_chain)
    altered = replace(original, **{field: value})
    observations = tuple(altered if item.observation_id == original.observation_id else item for item in sample_chain.ingestion.observations)
    with pytest.raises(QuarterlyCalculationError):
        derive_quarter(observations, company_id=original.company_id, metric_id=original.metric_id, year=2024, quarter="Q2")


def test_derive_rejects_unknown_currency_and_unit(sample_chain) -> None:
    original = _observation(sample_chain)
    no_currency = replace(original, evidence=replace(original.evidence, currency=None))
    no_unit = replace(original, evidence=replace(original.evidence, unit=None))
    for altered in (no_currency, no_unit):
        observations = tuple(altered if item.observation_id == original.observation_id else item for item in sample_chain.ingestion.observations)
        with pytest.raises(QuarterlyCalculationError):
            derive_quarter(observations, company_id=original.company_id, metric_id=original.metric_id, year=2024, quarter="Q2")


def test_instant_values_and_missing_cumulative_periods_are_rejected(sample_chain) -> None:
    original = _observation(sample_chain, period="Q1")
    instant_evidence = replace(original.evidence, period_start=None, period_type="instant")
    instant = replace(original, basis="instant", evidence=instant_evidence)
    values = tuple(instant if item.observation_id == original.observation_id else item for item in sample_chain.ingestion.observations)
    with pytest.raises(QuarterlyCalculationError, match="时点 observation"):
        derive_quarter(values, company_id=original.company_id, metric_id=original.metric_id, year=2024, quarter="Q1")

    missing_q1 = tuple(
        item for item in sample_chain.ingestion.observations
        if not (item.observation_year == 2024 and item.report_period == "Q1" and item.metric_id == "revenue" and item.column_role == "current")
    )
    with pytest.raises(QuarterlyCalculationError, match="缺少上一个连续累计期间"):
        derive_quarter(missing_q1, company_id=original.company_id, metric_id="revenue", year=2024, quarter="Q2")


@pytest.mark.parametrize(("base", "expected_growth"), [("0", None), ("-20", None)])
def test_nonpositive_base_keeps_difference_and_waives_growth(base: str, expected_growth: None) -> None:
    prior = _derived(2023, "Q2", base)
    current = _derived(2024, "Q2", "30")
    result = compare_quarters(current, prior, relation="year_over_year")
    assert result.difference == str(Decimal("30") - Decimal(base))
    assert result.growth_rate is expected_growth
    assert result.limitations


def test_decimal_difference_uses_precision_above_default_context() -> None:
    current_value = "1" + "0" * 80 + ".01"
    prior_value = "0.01"
    comparison = compare_quarters(_derived(2024, "Q2", current_value), _derived(2023, "Q2", prior_value), relation="year_over_year")
    assert Decimal(comparison.difference) == Decimal("1e80")


def test_quarter_over_quarter_q1_uses_prior_year_q4() -> None:
    current = _derived(2024, "Q1", "120")
    prior = _derived(2023, "Q4", "100")
    assert compare_quarters(current, prior, relation="quarter_over_quarter").difference == "20"
    with pytest.raises(QuarterlyCalculationError, match="Q1 对上一年 Q4"):
        compare_quarters(current, _derived(2023, "Q1", "100"), relation="quarter_over_quarter")


def test_source_verifier_distinguishes_missing_evidence_from_conflict(sample_chain, monkeypatch) -> None:
    observation = _observation(sample_chain)
    path = sample_chain.ingestion.document_paths[observation.source_document_id]

    monkeypatch.setattr(verifier, "_independent_unit", lambda *_args: None)
    result, proof = verify_cumulative_observation(path, observation)
    assert result.status == "insufficient_evidence"
    assert proof is None

    monkeypatch.undo()
    wrong_raw = replace(observation, evidence=replace(observation.evidence, value_normalized="999999999"))
    result, proof = verify_cumulative_observation(path, wrong_raw)
    assert result.status == "conflict"
    assert proof is None


def test_derived_recheck_preserves_source_insufficiency_and_conflict(sample_chain, monkeypatch) -> None:
    derived = sample_chain.derived[(2024, "Q2", "revenue")]
    target = next(item for item in sample_chain.ingestion.observations if item.observation_id == derived.input_observation_ids[0])
    original_recheck = verifier.verify_cumulative_observation
    source_result, _ = original_recheck(sample_chain.ingestion.document_paths[target.source_document_id], target)
    insufficient = replace(
        source_result,
        status="insufficient_evidence",
        conflicts=(),
        limitations=("PDF 原文缺少可唯一定位的金额单元格。",),
    )

    def return_insufficient(path, observation):
        if observation.observation_id == target.observation_id:
            return insufficient, None
        return original_recheck(path, observation)

    monkeypatch.setattr(verifier, "verify_cumulative_observation", return_insufficient)
    result, proof = verify_derived_quarter(
        derived,
        sample_chain.ingestion.observations,
        sample_chain.source_proofs,
        sample_chain.ingestion.document_paths,
    )
    assert result.status == "insufficient_evidence"
    assert result.conflicts == ()
    assert any("缺少可唯一定位" in item for item in result.limitations)
    assert proof is None

    conflict = replace(
        source_result,
        status="conflict",
        conflicts=("PDF 原文金额与 observation 不一致。",),
        limitations=(),
    )

    def return_conflict(path, observation):
        if observation.observation_id == target.observation_id:
            return conflict, None
        return original_recheck(path, observation)

    monkeypatch.setattr(verifier, "verify_cumulative_observation", return_conflict)
    result, proof = verify_derived_quarter(
        derived,
        sample_chain.ingestion.observations,
        sample_chain.source_proofs,
        sample_chain.ingestion.document_paths,
    )
    assert result.status == "conflict"
    assert any("金额与 observation 不一致" in item for item in result.conflicts)
    assert proof is None


def test_source_hash_or_currency_mismatch_never_gets_proof(sample_chain) -> None:
    observation = _observation(sample_chain)
    path = sample_chain.ingestion.document_paths[observation.source_document_id]
    bad_hash = replace(observation, evidence=replace(observation.evidence, source_sha256="0" * 64))
    bad_currency = replace(observation, evidence=replace(observation.evidence, currency="USD"))
    for altered in (bad_hash, bad_currency):
        result, proof = verify_cumulative_observation(path, altered)
        assert result.status == "conflict"
        assert proof is None


def test_source_and_derived_proofs_bind_full_target_content(sample_chain) -> None:
    observation = _observation(sample_chain)
    source_proof = next(proof for proof in sample_chain.source_proofs if proof.target_id == observation.observation_id)
    copied_from_json = json.loads(json.dumps(source_proof.to_dict(), ensure_ascii=False))
    reconstructed = QuarterlyProof(
        proof_id=copied_from_json["proof_id"],
        proof_type=copied_from_json["proof_type"],
        target_id=copied_from_json["target_id"],
        source_sha256s=tuple(copied_from_json["source_sha256s"]),
        evidence_ids=tuple(copied_from_json["evidence_ids"]),
        binding_sha256=copied_from_json["binding_sha256"],
        issued_at=datetime.fromisoformat(copied_from_json["issued_at"]),
        status=copied_from_json["status"],
    )
    assert not is_current_quarterly_proof(reconstructed, proof_type="cumulative_observation", target_id=observation.observation_id, bound_value=observation)

    derived = sample_chain.derived[(2024, "Q2", "revenue")]
    derived_proof = next(proof for proof in sample_chain.derived_proofs if proof.target_id == derived.derived_id)
    source_hashes = {item.source_sha256 for item in sample_chain.ingestion.observations if item.observation_id in derived.input_observation_ids}
    assert set(derived_proof.source_sha256s) == source_hashes
    tampered = replace(derived, normalized_value="999999")
    result, proof = verify_derived_quarter(
        tampered,
        sample_chain.ingestion.observations,
        sample_chain.source_proofs,
        sample_chain.ingestion.document_paths,
    )
    assert result.status != "verified"
    assert proof is None


def test_comparability_proof_preserves_all_hashes_and_rejects_missing_or_duplicate_sources(sample_chain) -> None:
    current = sample_chain.derived[(2024, "Q2", "revenue")]
    prior = sample_chain.derived[(2023, "Q2", "revenue")]
    result, proof = verify_comparability(
        current, prior, "year_over_year", sample_chain.ingestion.observations,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "verified"
    assert proof is not None
    expected_hashes = {
        item.source_sha256 for item in sample_chain.ingestion.observations
        if item.observation_id in set(current.input_observation_ids + prior.input_observation_ids)
    }
    assert set(proof.source_sha256s) == expected_hashes

    omitted = tuple(item for item in sample_chain.ingestion.observations if item.observation_id not in prior.input_observation_ids)
    result, no_proof = verify_comparability(
        current, prior, "year_over_year", omitted,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "insufficient_evidence"
    assert no_proof is None

    duplicate = (*sample_chain.ingestion.observations, next(item for item in sample_chain.ingestion.observations if item.observation_id == prior.input_observation_ids[0]))
    result, no_proof = verify_comparability(
        current, prior, "year_over_year", duplicate,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "insufficient_evidence"
    assert no_proof is None


def test_yoy_comparability_proof_includes_distinct_comparative_source_hash_and_evidence(sample_chain, tmp_path, monkeypatch) -> None:
    original = next(
        item for item in sample_chain.ingestion.observations
        if item.report_year == 2024 and item.report_period == "H1" and item.metric_id == "revenue" and item.column_role == "comparative"
    )
    original_path = sample_chain.ingestion.document_paths[original.source_document_id]
    cloned_path = tmp_path / "sim-2024-h1-comparative-copy.pdf"
    doc = pymupdf.open(original_path)
    metadata = dict(doc.metadata)
    metadata["author"] = "FINTRACE distinct comparative evidence test"
    doc.set_metadata(metadata)
    doc.save(cloned_path)
    doc.close()

    cloned_id = "sim-2024-h1-comparative-copy"
    cloned_sha256 = hashlib.sha256(cloned_path.read_bytes()).hexdigest()
    assert cloned_sha256 != original.source_sha256
    cloned = replace(
        original,
        observation_id=f"{original.observation_id}-copy",
        evidence=replace(
            original.evidence,
            evidence_id=f"{original.evidence.evidence_id}-copy",
            document_id=cloned_id,
            source_sha256=cloned_sha256,
        ),
    )
    monkeypatch.setattr(verifier, "_safe_pdf_path", lambda value: Path(value).resolve())
    source_result, cloned_proof = verify_cumulative_observation(cloned_path, cloned)
    assert source_result.status == "verified", source_result.to_dict()
    assert cloned_proof is not None

    observations = tuple(item for item in sample_chain.ingestion.observations if item.observation_id != original.observation_id) + (cloned,)
    source_proofs = tuple(
        proof for proof in sample_chain.source_proofs if proof.target_id != original.observation_id
    ) + (cloned_proof,)
    pdf_paths = {**sample_chain.ingestion.document_paths, cloned_id: cloned_path}
    result, comparability_proof = verify_comparability(
        sample_chain.derived[(2024, "Q2", "revenue")],
        sample_chain.derived[(2023, "Q2", "revenue")],
        "year_over_year",
        observations,
        source_proofs,
        sample_chain.derived_proofs,
        pdf_paths,
    )
    assert result.status == "verified", result.to_dict()
    assert comparability_proof is not None
    assert cloned_sha256 in comparability_proof.source_sha256s
    assert cloned.evidence.evidence_id in comparability_proof.evidence_ids


def test_restatement_declaration_missing_abstains_and_comparator_conflict_fails(sample_chain, monkeypatch) -> None:
    current = sample_chain.derived[(2024, "Q2", "revenue")]
    prior = sample_chain.derived[(2023, "Q2", "revenue")]
    monkeypatch.setattr(verifier, "_has_metric_restatement_statement", lambda *_args: False)
    result, proof = verify_comparability(
        current, sample_chain.derived[(2024, "Q1", "revenue")], "quarter_over_quarter",
        sample_chain.ingestion.observations, sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "insufficient_evidence"
    assert proof is None
    monkeypatch.undo()

    original = next(item for item in sample_chain.ingestion.observations if item.report_year == 2024 and item.report_period == "H1" and item.metric_id == "revenue" and item.column_role == "comparative")
    altered = replace(original, evidence=replace(original.evidence, value_normalized="2400001"))
    observations = tuple(altered if item.observation_id == original.observation_id else item for item in sample_chain.ingestion.observations)
    result, proof = verify_comparability(
        current, prior, "year_over_year", observations,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "conflict"
    assert proof is None


def test_comparability_recheck_preserves_source_insufficiency_and_conflict(sample_chain, monkeypatch) -> None:
    original_recheck = verifier.verify_cumulative_observation
    current = sample_chain.derived[(2024, "Q2", "revenue")]
    prior = sample_chain.derived[(2023, "Q2", "revenue")]
    prior_source = next(
        item for item in sample_chain.ingestion.observations
        if item.observation_id == prior.input_observation_ids[0]
    )
    verified, _ = original_recheck(sample_chain.ingestion.document_paths[prior_source.source_document_id], prior_source)
    insufficient = replace(
        verified,
        status="insufficient_evidence",
        conflicts=(),
        limitations=("PDF 中缺少可唯一定位的金额表格。",),
    )

    def recheck_prior_as_insufficient(path, observation):
        if observation.observation_id == prior_source.observation_id:
            return insufficient, None
        return original_recheck(path, observation)

    monkeypatch.setattr(verifier, "verify_cumulative_observation", recheck_prior_as_insufficient)
    result, proof = verify_comparability(
        current, prior, "year_over_year", sample_chain.ingestion.observations,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "insufficient_evidence"
    assert result.conflicts == ()
    assert any("缺少可唯一定位" in item for item in result.limitations)
    assert proof is None

    comparative = next(
        item for item in sample_chain.ingestion.observations
        if item.report_year == 2024 and item.report_period == "H1" and item.metric_id == "revenue" and item.column_role == "comparative"
    )
    verified_comparative, _ = original_recheck(
        sample_chain.ingestion.document_paths[comparative.source_document_id], comparative
    )
    conflict = replace(
        verified_comparative,
        status="conflict",
        conflicts=("原文金额与比较列 observation 不一致。",),
        limitations=(),
    )

    def recheck_comparative_as_conflict(path, observation):
        if observation.observation_id == comparative.observation_id:
            return conflict, None
        return original_recheck(path, observation)

    monkeypatch.setattr(verifier, "verify_cumulative_observation", recheck_comparative_as_conflict)
    result, proof = verify_comparability(
        current, prior, "year_over_year", sample_chain.ingestion.observations,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "conflict"
    assert any("金额与比较列 observation 不一致" in item for item in result.conflicts)
    assert proof is None

    qoq_prior = sample_chain.derived[(2024, "Q1", "revenue")]
    qoq_target = next(
        item for item in sample_chain.ingestion.observations
        if item.observation_id == current.input_observation_ids[0]
    )
    qoq_source_result, _ = original_recheck(
        sample_chain.ingestion.document_paths[qoq_target.source_document_id], qoq_target
    )
    qoq_insufficient = replace(
        qoq_source_result,
        status="insufficient_evidence",
        conflicts=(),
        limitations=("来源 PDF 的季度表头不能唯一定位。",),
    )

    def recheck_qoq_as_insufficient(path, observation):
        if observation.observation_id == qoq_target.observation_id:
            return qoq_insufficient, None
        return original_recheck(path, observation)

    monkeypatch.setattr(verifier, "verify_cumulative_observation", recheck_qoq_as_insufficient)
    result, proof = verify_comparability(
        current, qoq_prior, "quarter_over_quarter", sample_chain.ingestion.observations,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert result.status == "insufficient_evidence"
    assert result.conflicts == ()
    assert any("季度表头不能唯一定位" in item for item in result.limitations)
    assert proof is None


@pytest.mark.parametrize("field,value", [
    ("normalized_value", "999999"),
    ("currency", "USD"),
    ("unit", "万元"),
    ("scope", "unknown"),
])
def test_old_derived_proof_cannot_authorize_replaced_target(sample_chain, field: str, value: str) -> None:
    current = sample_chain.derived[(2024, "Q2", "revenue")]
    prior = sample_chain.derived[(2023, "Q2", "revenue")]
    tampered = replace(current, **{field: value})
    comparison = compare_quarters(current, prior, relation="year_over_year")
    _, comparability_proof = verify_comparability(
        current, prior, "year_over_year", sample_chain.ingestion.observations,
        sample_chain.source_proofs, sample_chain.derived_proofs, sample_chain.ingestion.document_paths,
    )
    assert comparability_proof is not None
    result, proof = verify_quarter_comparison(
        comparison, tampered, prior, sample_chain.derived_proofs, comparability_proof,
    )
    assert result.status != "verified"
    assert proof is None


def test_duplicate_manifest_sources_are_rejected_in_isolated_project_root(sample_chain, tmp_path: Path) -> None:
    data_root = tmp_path / "data" / "samples" / "quarterly"
    data_root.mkdir(parents=True)
    original_path = ROOT / "data" / "samples" / "quarterly" / "sim-2023-q1.pdf"
    copied_path = data_root / original_path.name
    shutil.copyfile(original_path, copied_path)
    source = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document = source["documents"][0]
    source["documents"] = [
        {**document, "document_id": "duplicate-one", "source_pdf": f"data/samples/quarterly/{copied_path.name}"},
        {**document, "document_id": "duplicate-two", "source_pdf": f"data/samples/quarterly/{copied_path.name}"},
    ]
    manifest_path = data_root / "source_manifest.json"
    manifest_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(QuarterlyIngestionError, match="重复 document_id 或重复 PDF 来源哈希"):
        load_quarterly_manifest(manifest_path, project_root=tmp_path)


@pytest.mark.parametrize("field,value", [("document_id", "../escape"), ("company_id", "SIM/escape")])
def test_source_document_identifiers_are_path_safe(sample_chain, field: str, value: str) -> None:
    document = sample_chain.ingestion.documents[0]
    with pytest.raises(ValueError, match="安全 ASCII 标识"):
        replace(document, **{field: value})


def test_quarterly_cli_runs_full_synthetic_chain_and_archives(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "analyze_quarterly.py"),
            "--manifest", "data/samples/quarterly/source_manifest.json",
            "--year", "2024", "--quarter", "Q2",
            "--artifacts-root", str(tmp_path / "artifacts"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    match = re.search(r"run_id=(quarterly-analysis-[0-9a-f-]+)", completed.stdout)
    assert match, completed.stdout
    run_id = match.group(1)
    run = tmp_path / "artifacts" / "runs" / run_id / "analysis.json"
    report = tmp_path / "artifacts" / "reports" / run_id / "quarterly-analysis.md"
    archive = json.loads(run.read_text(encoding="utf-8"))
    assert archive["status"] == "completed"
    assert len(archive["comparisons"]) == 6
    assert all(item["verification"]["status"] == "verified" for item in archive["comparisons"])
    assert all(item["comparability"]["status"] == "verified" for item in archive["comparisons"])
    assert all(item["proof"] is not None and item["proof"]["evidence_ids"] for item in archive["comparisons"])
    assert all(Path(item["source_pdf"]).is_relative_to(Path("data")) for item in archive["source_documents"])
    assert report.is_file()
    report_text = report.read_text(encoding="utf-8")
    relative_archive_link = f"../../runs/{run_id}/analysis.json"
    assert f"]({relative_archive_link})" in report_text
    assert (report.parent / relative_archive_link).resolve() == run.resolve()
    assert "可随FINTRACE比赛提交件和团队演示包分发" in report_text
