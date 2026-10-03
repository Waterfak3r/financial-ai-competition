"""从受控季度来源 manifest 运行并归档同比/环比证据链。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from finagent.finance.quarterly import (  # noqa: E402
    QuarterlyCalculationError,
    compare_quarters,
    derive_quarter,
)
from finagent.ingestion.quarterly import load_quarterly_manifest  # noqa: E402
from finagent.reports.quarterly_analysis import render_quarterly_markdown  # noqa: E402
from finagent.schemas.quarterly import METRIC_STATEMENTS, QuarterlyComparison  # noqa: E402
from finagent.verification.quarterly import (  # noqa: E402
    verify_comparability,
    verify_cumulative_observation,
    verify_derived_quarter,
    verify_quarter_comparison,
)

_PROVENANCE_FILES = (
    "scripts/analyze_quarterly.py",
    "scripts/prepare_quarterly_samples.py",
    "backend/src/finagent/schemas/quarterly.py",
    "backend/src/finagent/ingestion/quarterly.py",
    "backend/src/finagent/finance/quarterly.py",
    "backend/src/finagent/verification/quarterly.py",
    "backend/src/finagent/reports/quarterly_analysis.py",
)
_QUARTERS = ("Q1", "Q2", "Q3", "Q4")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (OSError, ValueError, QuarterlyCalculationError) as exc:
        print(f"status=failed reason={type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从季度/半年报文本 PDF 生成可审计的同比/环比分析包。")
    parser.add_argument("--manifest", type=Path, default=Path("data/samples/quarterly/source_manifest.json"), help="data/ 下受控来源清单")
    parser.add_argument("--company-id", default=None, help="目标公司标识；默认采用来源清单标识")
    parser.add_argument("--year", type=int, required=True, help="目标年度")
    parser.add_argument("--quarter", choices=_QUARTERS, required=True, help="目标季度")
    parser.add_argument("--relations", default="year_over_year,quarter_over_quarter", help="year_over_year,quarter_over_quarter 的逗号分隔子集")
    parser.add_argument("--artifacts-root", type=Path, default=ROOT / "artifacts", help="归档根目录，默认项目 artifacts")
    return parser


def _run(args: argparse.Namespace) -> int:
    relations = tuple(part.strip() for part in args.relations.split(",") if part.strip())
    if not relations or len(set(relations)) != len(relations) or any(item not in {"year_over_year", "quarter_over_quarter"} for item in relations):
        raise ValueError("--relations 只能包含不重复的 year_over_year 和 quarter_over_quarter。")
    if not 1900 < args.year <= 2100:
        raise ValueError("--year 超出支持范围。")

    ingestion = load_quarterly_manifest(args.manifest, project_root=ROOT)
    company_id = args.company_id or ingestion.manifest["company_id"]
    if company_id != ingestion.manifest["company_id"]:
        raise ValueError("--company-id 必须与来源清单公司一致。")

    source_records: list[dict] = []
    source_proofs = []
    for observation in ingestion.observations:
        path = ingestion.document_paths[observation.source_document_id]
        verification, proof = verify_cumulative_observation(path, observation)
        if proof is not None:
            source_proofs.append(proof)
        source_records.append({
            **observation.to_dict(),
            "source_document_id": observation.source_document_id,
            "verification": verification.to_dict(),
            "proof": proof.to_dict() if proof is not None else None,
        })

    needed_periods = {(args.year, args.quarter)}
    for relation in relations:
        if relation == "year_over_year":
            needed_periods.add((args.year - 1, args.quarter))
        else:
            needed_periods.add(_prior_period(args.year, args.quarter))
    derived_records: list[dict] = []
    derived_values = {}
    derived_proofs = []
    limitations: list[str] = [
        f"{item.get('document_id')} / {item.get('metric_id')}：{item.get('code')}"
        for item in ingestion.issues
    ]
    for year, quarter in sorted(needed_periods):
        for metric_id in METRIC_STATEMENTS:
            try:
                candidate = derive_quarter(
                    ingestion.observations,
                    company_id=company_id,
                    metric_id=metric_id,
                    year=year,
                    quarter=quarter,
                )
            except (QuarterlyCalculationError, ValueError) as exc:
                limitations.append(f"{metric_id} {year} {quarter} 无法派生：{exc}")
                continue
            verification, proof = verify_derived_quarter(candidate, ingestion.observations, source_proofs, ingestion.document_paths)
            if proof is not None:
                derived_proofs.append(proof)
                derived_values[(year, quarter, metric_id)] = candidate
            else:
                limitations.extend(verification.limitations)
                limitations.extend(verification.conflicts)
            derived_records.append({
                **candidate.to_dict(),
                "verification": verification.to_dict(),
                "proof": proof.to_dict() if proof is not None else None,
            })

    comparison_records: list[dict] = []
    for relation in relations:
        prior_year, prior_quarter = (args.year - 1, args.quarter) if relation == "year_over_year" else _prior_period(args.year, args.quarter)
        for metric_id in METRIC_STATEMENTS:
            current = derived_values.get((args.year, args.quarter, metric_id))
            prior = derived_values.get((prior_year, prior_quarter, metric_id))
            if current is None or prior is None:
                reason = "当前或比较季度未能从已核验源 observation 派生。"
                comparison_records.append({
                    "metric_id": metric_id, "relation": relation, "status": "abstained",
                    "current_value": None, "prior_value": None, "difference": None, "growth_rate": None,
                    "limitations": [reason],
                    "comparability": {"status": "insufficient_evidence", "limitations": [reason]},
                    "verification": {"status": "insufficient_evidence", "limitations": [reason]},
                })
                limitations.append(f"{metric_id} {relation}：{reason}")
                continue

            comparability, comparability_proof = verify_comparability(
                current, prior, relation, ingestion.observations, source_proofs, derived_proofs, ingestion.document_paths
            )
            if comparability_proof is None:
                failed = QuarterlyComparison(
                    comparison_id=f"abstained-{current.derived_id}-{prior.derived_id}-{relation}",
                    metric_id=metric_id,
                    relation=relation,  # type: ignore[arg-type]
                    current_id=current.derived_id,
                    prior_id=prior.derived_id,
                    current_value=current.normalized_value,
                    prior_value=prior.normalized_value,
                    difference=None,
                    growth_rate=None,
                    status="failed",
                    failure_reason="缺少足以支持该期间比较的独立可比性证据。",
                    limitations=tuple(comparability.limitations + comparability.conflicts),
                )
                failed_verification, failed_proof = verify_quarter_comparison(
                    failed, current, prior, derived_proofs, comparability_proof, comparability
                )
                comparison_records.append({
                    **failed.to_dict(),
                    "status": "abstained",
                    "comparability": comparability.to_dict(),
                    "verification": failed_verification.to_dict(),
                    "proof": failed_proof.to_dict() if failed_proof is not None else None,
                })
                limitations.extend(f"{metric_id} {relation}：{item}" for item in failed.limitations)
                continue

            comparison = compare_quarters(current, prior, relation=relation)
            verification, proof = verify_quarter_comparison(comparison, current, prior, derived_proofs, comparability_proof, comparability)
            if verification.status != "verified":
                limitations.extend(verification.limitations)
                limitations.extend(verification.conflicts)
            comparison_records.append({
                **comparison.to_dict(),
                "comparability": comparability.to_dict(),
                "verification": verification.to_dict(),
                "proof": proof.to_dict() if proof is not None else None,
            })

    run_id, run_dir, report_dir = _reserve_run_dirs(args.artifacts_root)
    all_comparison_verified = bool(comparison_records) and all(item.get("status") == "calculated" and item.get("verification", {}).get("status") == "verified" for item in comparison_records)
    status = "completed" if all_comparison_verified else "completed_with_abstentions"
    source_documents = [item.to_dict() for item in ingestion.documents]
    for document in source_documents:
        document["source_pdf"] = document["source_pdf"].replace("\\", "/")
    archive = {
        "schema_version": "quarterly-analysis-v1",
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "company_id": company_id,
        "target_year": args.year,
        "target_quarter": args.quarter,
        "requested_relations": list(relations),
        "dataset": {
            "dataset_id": ingestion.manifest.get("dataset_id"),
            "nature": ingestion.manifest.get("nature"),
            "use_conditions": ingestion.manifest.get("use_conditions"),
            "manifest_path": ingestion.manifest_path,
        },
        "source_documents": source_documents,
        "source_observations": source_records,
        "source_issues": list(ingestion.issues),
        "derived_quarters": derived_records,
        "comparisons": comparison_records,
        "limitations": list(dict.fromkeys(item for item in limitations if item)),
        "provenance": {
            "engine": "quarterly-deterministic-v1",
            "code_sha256": {relative: _sha256(ROOT / relative) for relative in _PROVENANCE_FILES},
            "input_manifest_sha256": _sha256(ROOT / ingestion.manifest_path),
            "python_version": sys.version.split()[0],
        },
    }
    (run_dir / "analysis.json").write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (report_dir / "quarterly-analysis.md").write_text(render_quarterly_markdown(archive), encoding="utf-8")
    print(
        f"status={status} run_id={run_id} documents={len(source_documents)} observations={len(source_records)} "
        f"derived={len(derived_records)} comparisons={len(comparison_records)}"
    )
    print(f"archive={_display_relative(run_dir / 'analysis.json', args.artifacts_root)}")
    print(f"report={_display_relative(report_dir / 'quarterly-analysis.md', args.artifacts_root)}")
    return 0


def _prior_period(year: int, quarter: str) -> tuple[int, str]:
    if quarter == "Q1":
        return year - 1, "Q4"
    return year, f"Q{int(quarter[1]) - 1}"


def _reserve_run_dirs(artifacts_root: Path) -> tuple[str, Path, Path]:
    runs_root = artifacts_root / "runs"
    reports_root = artifacts_root / "reports"
    runs_root.mkdir(parents=True, exist_ok=True)
    reports_root.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        run_id = f"quarterly-analysis-{uuid4()}"
        run_dir = runs_root / run_id
        report_dir = reports_root / run_id
        if run_dir.exists() or report_dir.exists():
            continue
        run_dir.mkdir()
        report_dir.mkdir()
        return run_id, run_dir, report_dir
    raise FileExistsError("无法保留唯一季度分析 run_id；未覆盖既有产物。")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
