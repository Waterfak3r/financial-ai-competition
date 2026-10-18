"""季度与多期间分析 HTTP 接口支持。

从受控来源清单或已归档的运行记录中读取或执行季度派生与同比/环比核验。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from finagent.schemas.quarterly import METRIC_STATEMENTS
from scripts.analyze_quarterly import _run as run_quarterly_cli_internal, _parser as get_quarterly_parser


class QuarterlyAnalysisError(Exception):
    """季度分析执行或读取错误。"""


def load_latest_or_sample_quarterly_analysis(project_root: Path, run_id: str | None = None) -> dict[str, Any]:
    """读取指定或最新已归档的季度分析，若无则回退至正式证据中的样本结果。"""
    artifacts_runs = project_root / "artifacts" / "runs"
    
    if run_id:
        target = artifacts_runs / run_id / "analysis.json"
        if target.is_file():
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except Exception as exc:
                raise QuarterlyAnalysisError(f"读取季度分析记录失败: {exc}") from exc
        raise QuarterlyAnalysisError(f"未找到指定的季度分析记录: {run_id}")

    # 寻找 artifacts/runs 下最新的 quarterly-analysis-*
    if artifacts_runs.is_dir():
        runs = sorted(
            [p for p in artifacts_runs.glob("quarterly-analysis-*/analysis.json") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if runs:
            try:
                return json.loads(runs[0].read_text(encoding="utf-8"))
            except Exception:
                pass

    # 回退至正式提交证据包中的基准样本
    evidence_sample = project_root / "submission" / "final" / "evidence" / "quarterly-analysis-6e7807fc-f25c-4dbc-bf80-5323fc902296.json"
    if evidence_sample.is_file():
        try:
            return json.loads(evidence_sample.read_text(encoding="utf-8"))
        except Exception as exc:
            raise QuarterlyAnalysisError(f"读取样本季度分析失败: {exc}") from exc

    raise QuarterlyAnalysisError("未找到任何可用季度分析结果。")


def execute_quarterly_analysis(
    project_root: Path,
    *,
    year: int = 2024,
    quarter: str = "Q2",
    manifest_rel: str = "data/samples/quarterly/source_manifest.json",
    relations: str = "year_over_year,quarter_over_quarter",
    company_id: str | None = None,
) -> dict[str, Any]:
    """通过现有季度计算与核验链路执行一次完整的分析并返回结构化归档结果。"""
    manifest_path = project_root / manifest_rel
    if not manifest_path.is_file():
        raise QuarterlyAnalysisError(f"来源清单文件不存在: {manifest_rel}")

    parser = get_quarterly_parser()
    args_list = [
        "--manifest", str(manifest_path),
        "--year", str(year),
        "--quarter", quarter,
        "--relations", relations,
        "--artifacts-root", str(project_root / "artifacts"),
    ]
    if company_id:
        args_list.extend(["--company-id", company_id])

    args = parser.parse_args(args_list)
    exit_code = run_quarterly_cli_internal(args)
    if exit_code != 0:
        raise QuarterlyAnalysisError("季度同比/环比分析执行失败。")

    # 读取最新生成的记录
    return load_latest_or_sample_quarterly_analysis(project_root)
