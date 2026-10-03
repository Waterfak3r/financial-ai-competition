"""季度源值、派生值、期间可比性和比较值的独立重开/重算核验。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

from finagent.schemas.quarterly import (
    CumulativeObservation,
    DerivedQuarter,
    QuarterlyComparison,
    QuarterlyProof,
    QuarterlyVerification,
)

_PROOFS: dict[str, tuple[QuarterlyProof, str]] = {}
_ROOT = Path(__file__).resolve().parents[4]
_RATE_PRECISION = 50
_SUPPORTED_CURRENCIES = {"CNY", "USD", "HKD", "EUR", "JPY", "GBP"}
_UNIT_MULTIPLIERS = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}
_NUMBER = re.compile(r"^[+-]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?$|^\((?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?\)$")
_METRIC = {
    "revenue": ("合并利润表", "营业收入"),
    "net_profit_parent": ("合并利润表", "归属于母公司股东的净利润"),
    "operating_cash_flow": ("合并现金流量表", "经营活动产生的现金流量净额"),
}
_METRIC_STATEMENTS = {"revenue": "income_statement", "net_profit_parent": "income_statement", "operating_cash_flow": "cash_flow_statement"}
_PERIOD_SUFFIX = {"Q1": "第一季度", "H1": "半年度", "9M": "前三季度", "FY": "年度"}
_PERIOD_END = {"Q1": (3, 31), "H1": (6, 30), "9M": (9, 30), "FY": (12, 31)}


class _EvidenceUnavailable(ValueError):
    """Required source bindings are missing or non-unique, so verification must abstain."""


def is_current_quarterly_proof(
    value: object,
    *,
    proof_type: str | None = None,
    target_id: str | None = None,
    bound_value: Any = None,
) -> bool:
    """只承认本进程 verifier 创建并登记的同一对象；审计 JSON 不可恢复授权。"""

    if not isinstance(value, QuarterlyProof) or bound_value is None:
        return False
    registered = _PROOFS.get(value.proof_id)
    actual_binding = _canonical_sha256(bound_value)
    return (
        registered is not None
        and registered[0] is value
        and registered[1] == actual_binding
        and value.binding_sha256 == actual_binding
        and (proof_type is None or value.proof_type == proof_type)
        and (target_id is None or value.target_id == target_id)
        and value.status == "verified"
    )


def verify_cumulative_observation(pdf_path: Path, observation: CumulativeObservation) -> tuple[QuarterlyVerification, QuarterlyProof | None]:
    checks: list[str] = []
    conflicts: list[str] = []
    limitations: list[str] = []
    details: dict[str, Any] = {"source_document_id": observation.source_document_id}
    try:
        try:
            _assert_observation_context(observation)
        except ValueError as exc:
            limitations.append(f"observation 缺少受支持的完整口径字段：{exc}")
        path = _safe_pdf_path(pdf_path)
        actual_sha = _sha256(path)
        if actual_sha.lower() != observation.source_sha256.lower():
            conflicts.append("PDF_SHA256 与 observation 来源哈希不一致。")
        else:
            checks.append("重新打开源 PDF 并核对完整 SHA256。")
        doc = pymupdf.open(path)
        try:
            first = doc[0].get_text("text")
            company_markers = re.findall(r"(?:公司代码|股票代码|证券代码)\s*[:：]?\s*([A-Za-z0-9-]+)", first)
            if observation.company_id in first:
                checks.append("从 PDF 首页重新定位公司标识。")
            elif company_markers:
                conflicts.append("PDF 首页公司标识与 observation 不一致。")
            else:
                limitations.append("PDF 首页未能定位公司标识。")
            title, row_query = _METRIC.get(observation.metric_id, ("", ""))
            period_state = _identity_period_state(first, observation)
            if period_state == "mismatch":
                conflicts.append("PDF 首页报告年份或类型与 observation 不一致。")
            elif period_state == "missing":
                limitations.append("PDF 首页缺少足以確認报告年份和类型的文本。")
            else:
                checks.append("从 PDF 首页确认报告年份和报告类型。")
            table = _independent_statement_region(doc, title)
            if table is None:
                limitations.append(f"未能独立定位{title}。")
            else:
                start_page, title_rect, end = table
                headers = _independent_headers(doc[start_page], title_rect, observation)
                role = observation.column_role
                if headers is None or role not in headers:
                    limitations.append(f"未能在表头重新定位 observation 所需的 {role} 期间列。")
                else:
                    unit = _independent_unit(doc[start_page], title_rect)
                    if unit is None:
                        limitations.append("未能从原文定位币种和单位。")
                    else:
                        unit_text, currency, multiplier, unit_rect = unit
                        rows = _independent_rows(doc, row_query, start_page, title_rect, end)
                        cells = _independent_row_cells(doc, rows, headers)
                        role_cells = cells.get(role, [])
                        distinct_values = {cell[0] for cell in role_cells}
                        if not role_cells:
                            limitations.append("未能从原表行列重新定位 observation 所需指标金额。")
                        elif len(distinct_values) > 1:
                            conflicts.append("原表同一指标期间列出现相互矛盾的金额。")
                        elif len(role_cells) != 1:
                            limitations.append("原表存在多个相同金额候选单元格，无法唯一定位。")
                        else:
                            raw, page_index, value_bbox, row_bbox = role_cells[0]
                            normalized = _exact_multiply(_decimal(raw), Decimal(multiplier))
                            expected_period = _period_dates(observation.observation_year, observation.report_period)
                            if (observation.period_start, observation.period_end) != expected_period:
                                conflicts.append("observation 期间与原文报告期间不一致。")
                            if observation.scope == "parent":
                                conflicts.append("原文表题为合并报表，但 observation 标注为母公司口径。")
                            elif observation.scope != "consolidated":
                                limitations.append("季度切片仅接受明确的 consolidated 报表口径。")
                            expected_statement = _METRIC_STATEMENTS.get(observation.metric_id)
                            if observation.statement_type != expected_statement:
                                conflicts.append("原文报表表题对应类型与 observation 声明类型不一致。")
                            if currency is None or observation.currency is None or observation.unit is None:
                                limitations.append("原文或 observation 缺少可识别的币种/单位字段。")
                            elif (
                                observation.currency != currency
                                or observation.unit != unit_text
                                or Decimal(observation.unit_multiplier) != Decimal(multiplier)
                            ):
                                conflicts.append("原文币种、单位或倍率与 observation 不一致。")
                            if raw != observation.evidence.value_raw or normalized != observation.value:
                                conflicts.append("原文重新定位数值与 observation 的原始/规范值不一致。")
                            if not conflicts and not limitations:
                                checks.extend((
                                    "从 PDF 表题重新确认合并报表类型。",
                                    "忽略提取器坐标，按原文指标行及期间列重新定位单元格。",
                                    "按原文单位倍率将原始值重新标准化为 Decimal。",
                                ))
                            details["relocated_cells"] = [{
                                "pdf_page": page_index + 1, "raw_value": raw, "row_label": row_query,
                                "column_label": headers[role][0], "unit": unit_text, "currency": currency,
                                "value_bbox": list(value_bbox), "row_bbox": list(row_bbox),
                                "table_title_bbox": list(title_rect), "unit_bbox": list(unit_rect),
                            }]
                            details["recomputed_normalized_value"] = format(normalized, "f")
        finally:
            doc.close()
    except Exception as exc:
        limitations.append(f"独立源 PDF 定位失败：{type(exc).__name__}。")
    status = "conflict" if conflicts else "insufficient_evidence" if limitations else "verified"
    verification = _verification("source_observation", observation.observation_id, status, checks, conflicts, (observation.evidence.evidence_id,) if status == "verified" else (), limitations, details)
    proof = _issue_proof("cumulative_observation", observation.observation_id, observation, (observation,), (observation.evidence.evidence_id,)) if status == "verified" else None
    return verification, proof


def verify_derived_quarter(
    derived: DerivedQuarter,
    observations: Sequence[CumulativeObservation],
    source_proofs: Sequence[QuarterlyProof],
    pdf_paths: dict[str, Path],
) -> tuple[QuarterlyVerification, QuarterlyProof | None]:
    checks: list[str] = []
    conflicts: list[str] = []
    limitations: list[str] = []
    selected: list[CumulativeObservation] = []
    for obs_id in derived.input_observation_ids:
        found = [item for item in observations if item.observation_id == obs_id]
        obs = found[0] if len(found) == 1 else None
        proof = _proof_for(source_proofs, "cumulative_observation", obs_id, obs)
        if len(found) != 1 or proof is None:
            limitations.append("派生季度的源累计 observation 缺失、重复或没有当前进程独立核验 proof。")
            continue
        assert obs is not None
        path = pdf_paths.get(obs.source_document_id)
        if path is None:
            limitations.append("派生季度源 PDF 路径缺失。")
            continue
        recheck, _ = verify_cumulative_observation(path, obs)
        _propagate_source_recheck(recheck, "派生复核", conflicts, limitations)
        selected.append(obs)
    if not limitations and not conflicts:
        try:
            if derived.quarter == "Q1":
                if len(selected) != 1 or selected[0].report_period != "Q1":
                    raise ValueError("Q1 必须只绑定 Q1 累计源值。")
                _assert_observation_context(selected[0])
                _assert_derived_matches_source(derived, selected[0])
                if derived.year != selected[0].observation_year or derived.metric_id != selected[0].metric_id:
                    raise ValueError("派生季度年份或指标与Q1源累计值不一致。")
                recomputed = selected[0].value
                formula = "Q1 = Q1_ytd"
            else:
                if len(selected) != 2:
                    raise ValueError("Q2/Q3/Q4 必须绑定两个相邻年初累计值。")
                expected = {"Q2": ("H1", "Q1"), "Q3": ("9M", "H1"), "Q4": ("FY", "9M")}[derived.quarter]
                end = next((item for item in selected if item.report_period == expected[0]), None)
                start = next((item for item in selected if item.report_period == expected[1]), None)
                if end is None or start is None or len({end.report_period, start.report_period}) != 2:
                    raise ValueError("累计源期间不连续或顺序不完整。")
                _assert_obs_context(end, start)
                _assert_derived_matches_source(derived, end)
                if end.period_start != start.period_start or end.period_end is None or start.period_end is None or end.period_end <= start.period_end:
                    raise ValueError("累计源期间起始日或结束顺序不正确。")
                if derived.year != end.observation_year or derived.metric_id != end.metric_id:
                    raise ValueError("派生季度年份或指标与累计输入不一致。")
                recomputed = _independent_subtract(end.value, start.value)
                formula = f"{derived.quarter} = {expected[0]}_ytd - {expected[1]}_ytd"
            if derived.formula_expression != formula or derived.formula_id != f"derive_{derived.quarter.lower()}_from_ytd":
                conflicts.append("派生公式标识或表达式与受控公式不一致。")
            if recomputed != derived.value:
                conflicts.append("派生季度候选值与 verifier 独立 Decimal 重算不一致。")
            if tuple(item.source_document_id for item in selected) != derived.input_source_document_ids:
                conflicts.append("派生季度来源文档次序与计算输入不一致。")
            checks.append("独立重新打开每份源 PDF，重定位累计值并重新计算单季差额。")
        except (ValueError, InvalidOperation, KeyError) as exc:
            conflicts.append(f"派生季度独立重算失败：{exc}")
    status = "conflict" if conflicts else "insufficient_evidence" if limitations else "verified"
    details = {"recomputed_value": format(recomputed, "f") if not conflicts and not limitations and "recomputed" in locals() else None,
               "source_observation_ids": list(derived.input_observation_ids)}
    verification = _verification("derived_quarter", derived.derived_id, status, checks, conflicts, tuple(x.evidence.evidence_id for x in selected) if status == "verified" else (), limitations, details)
    proof = _issue_proof("derived_quarter", derived.derived_id, derived, tuple(selected), tuple(x.evidence.evidence_id for x in selected)) if status == "verified" else None
    return verification, proof


def verify_comparability(
    current: DerivedQuarter,
    prior: DerivedQuarter,
    relation: str,
    observations: Sequence[CumulativeObservation],
    source_proofs: Sequence[QuarterlyProof],
    derived_proofs: Sequence[QuarterlyProof],
    pdf_paths: dict[str, Path],
) -> tuple[QuarterlyVerification, QuarterlyProof | None]:
    checks: list[str] = []
    conflicts: list[str] = []
    limitations: list[str] = []
    comparability_source_proofs: list[QuarterlyProof] = []
    comparability_evidence_ids: list[str] = []
    current_proof = _proof_for(derived_proofs, "derived_quarter", current.derived_id, current)
    prior_proof = _proof_for(derived_proofs, "derived_quarter", prior.derived_id, prior)
    if current_proof is None or prior_proof is None:
        limitations.append("比较需要两个当前进程签发的派生季度 proof。")
    try:
        _assert_derived_context(current, prior)
        current_inputs = _require_observation_bindings(current, observations)
        if relation == "year_over_year":
            if current.year != prior.year + 1 or current.quarter != prior.quarter:
                conflicts.append("同比期间不是上一年度同一季度。")
            else:
                prior_inputs = _require_observation_bindings(prior, observations)
                if len(current_inputs) != len(prior_inputs):
                    raise ValueError("同比两侧派生季度未绑定相同数量的累计源输入。")
                paired: list[tuple[CumulativeObservation, CumulativeObservation]] = []
                for prior_input in prior_inputs:
                    matches = [x for x in observations if x.metric_id == prior.metric_id and x.observation_year == prior_input.observation_year
                               and x.report_period == prior_input.report_period and x.column_role == "comparative"
                               and x.report_year == current.year and x.company_id == current.company_id]
                    if len(matches) != 1:
                        limitations.append("缺少当前年度报告中对上一年同期间的独立比较列。")
                        continue
                    comparative = matches[0]
                    proof_a = _proof_for(source_proofs, "cumulative_observation", prior_input.observation_id, prior_input)
                    proof_b = _proof_for(source_proofs, "cumulative_observation", comparative.observation_id, comparative)
                    if proof_a is None or proof_b is None:
                        limitations.append("同比可比性来源 observation 未全部通过当前进程独立核验。")
                    for source_proof in (proof_a, proof_b):
                        if source_proof is not None:
                            comparability_source_proofs.append(source_proof)
                            comparability_evidence_ids.extend(source_proof.evidence_ids)
                    prior_path = pdf_paths.get(prior_input.source_document_id)
                    if prior_path is None:
                        limitations.append("上一年度原累计 observation 的 PDF 路径缺失。")
                    else:
                        prior_check, _ = verify_cumulative_observation(prior_path, prior_input)
                        _propagate_source_recheck(prior_check, "同比上一年度原累计", conflicts, limitations)
                    path = pdf_paths.get(comparative.source_document_id)
                    if path is None:
                        limitations.append("同比比较列来源 PDF 缺失。")
                    else:
                        check, _ = verify_cumulative_observation(path, comparative)
                        _propagate_source_recheck(check, "同比本年报告比较列", conflicts, limitations)
                    if prior_input.value != comparative.value:
                        conflicts.append("原披露的上一年累计数与本年报告比较列不一致，存在口径/追溯冲突。")
                    if not _obs_compatible(prior_input, comparative):
                        conflicts.append("上一年度原披露值与本年比较列的单位、币种或合并口径不一致。")
                    paired.append((prior_input, comparative))
                if paired and not limitations and not conflicts:
                    checks.append("逐项核对上一年原报告累计数与本年报告同期间比较列；数值、单位、币种及合并口径一致。")
        elif relation == "quarter_over_quarter":
            prior_inputs = _require_observation_bindings(prior, observations)
            if not _adjacent_quarters(current, prior):
                conflicts.append("环比期间不是相邻季度；Q1 必须使用上一年度 Q4。")
            for obs in (*current_inputs, *prior_inputs):
                proof = _proof_for(source_proofs, "cumulative_observation", obs.observation_id, obs)
                path = pdf_paths.get(obs.source_document_id)
                if proof is None or path is None:
                    limitations.append("环比来源累计值缺少已核验 proof 或原始 PDF。")
                    continue
                check, _ = verify_cumulative_observation(path, obs)
                _propagate_source_recheck(check, "环比累计", conflicts, limitations)
                comparability_source_proofs.append(proof)
                comparability_evidence_ids.extend(proof.evidence_ids)
                if not _has_metric_restatement_statement(path, obs.metric_id):
                    limitations.append("来源 PDF 没有针对本指标明确说明累计数据未追溯调整/更正的原文证据。")
            if not limitations and not conflicts:
                checks.append("逐个来源 PDF 核对本指标的未追溯调整声明，并确认公司、期间、单位、币种和口径。")
        else:
            conflicts.append("不支持的季度比较关系。")
    except _EvidenceUnavailable as exc:
        limitations.append(f"期间可比性证据不足：{exc}")
    except (ValueError, InvalidOperation) as exc:
        conflicts.append(f"期间可比性重核失败：{exc}")
    status = "conflict" if conflicts else "insufficient_evidence" if limitations else "verified"
    verification = _verification("quarter_comparability", f"{current.derived_id}:{prior.derived_id}:{relation}", status, checks, conflicts, (), limitations,
                                 {"relation": relation, "basis": "subsequent_comparative_values" if relation == "year_over_year" else "source_restatement_declarations"})
    proof_sources = (current_proof, prior_proof, *comparability_source_proofs)
    proof_evidence_ids = [
        evidence_id
        for proof_source in (current_proof, prior_proof)
        if proof_source is not None
        for evidence_id in proof_source.evidence_ids
    ]
    proof_evidence_ids.extend(comparability_evidence_ids)
    proof = _issue_proof(
        "quarter_comparability", verification.target_id, (current, prior, relation), proof_sources,
        tuple(proof_evidence_ids),
    ) if status == "verified" else None
    return verification, proof


def verify_quarter_comparison(
    comparison: QuarterlyComparison,
    current: DerivedQuarter,
    prior: DerivedQuarter,
    derived_proofs: Sequence[QuarterlyProof],
    comparability_proof: QuarterlyProof | None,
    comparability_verification: QuarterlyVerification | None = None,
) -> tuple[QuarterlyVerification, QuarterlyProof | None]:
    checks: list[str] = []
    conflicts: list[str] = []
    limitations: list[str] = []
    current_derived_proof = _proof_for(derived_proofs, "derived_quarter", current.derived_id, current)
    prior_derived_proof = _proof_for(derived_proofs, "derived_quarter", prior.derived_id, prior)
    if current_derived_proof is None or prior_derived_proof is None:
        limitations.append("比较结果缺少当前进程签发的派生季度 proof。")
    if not is_current_quarterly_proof(
        comparability_proof,
        proof_type="quarter_comparability",
        target_id=f"{current.derived_id}:{prior.derived_id}:{comparison.relation}",
        bound_value=(current, prior, comparison.relation),
    ):
        if comparability_verification is not None and comparability_verification.status == "conflict":
            conflicts.extend(comparability_verification.conflicts)
        else:
            limitations.append("比较结果缺少当前进程签发的期间可比性 proof。")
    if comparison.current_id != current.derived_id or comparison.prior_id != prior.derived_id or comparison.metric_id != current.metric_id:
        conflicts.append("比较输入 ID 或 metric_id 不匹配。")
    if comparison.relation not in {"year_over_year", "quarter_over_quarter"}:
        conflicts.append("比较关系不受支持。")
    try:
        _assert_derived_context(current, prior)
        _assert_temporal_relation(current, prior, comparison.relation)
        if Decimal(comparison.current_value) != current.value or Decimal(comparison.prior_value) != prior.value:
            conflicts.append("比较记录的当前值或比较值与派生季度不一致。")
        if comparison.status == "failed":
            if not comparison.failure_reason or comparison.difference is not None or comparison.growth_rate is not None:
                conflicts.append("弃权比较必须说明原因且不得附带差额或增长率。")
            if comparison.limitations:
                limitations.extend(comparison.limitations)
            else:
                limitations.append("比较因期间可比性证据不足而弃权。")
            if comparability_verification is not None:
                conflicts.extend(comparability_verification.conflicts)
                limitations.extend(comparability_verification.limitations)
        elif comparison.status == "calculated":
            expected_difference = _independent_subtract(current.value, prior.value)
            if comparison.difference is None or Decimal(comparison.difference) != expected_difference:
                conflicts.append("比较差额与独立重算不一致。")
            if prior.value <= 0:
                if comparison.growth_rate is not None:
                    conflicts.append("比较基数非正时必须放弃增长率。")
                else:
                    checks.append("比较基数非正，仅核验差额并弃权增长率。")
            else:
                with localcontext() as context:
                    context.prec = _RATE_PRECISION
                    expected_rate = expected_difference / prior.value
                if comparison.growth_rate is None or Decimal(comparison.growth_rate) != expected_rate:
                    conflicts.append("正比较基数的增长率与独立 Decimal 重算不一致。")
                else:
                    checks.append("独立重新计算季度差额和增长率。")
        else:
            conflicts.append("比较结果状态必须是 calculated 或 failed。")
    except (ValueError, InvalidOperation) as exc:
        conflicts.append(f"季度比较独立重算失败：{exc}")
    status = "conflict" if conflicts else "insufficient_evidence" if limitations else "verified"
    verification = _verification("quarter_comparison", comparison.comparison_id, status, checks, conflicts, (), limitations,
                                 {"recomputed_difference": format(_independent_subtract(current.value, prior.value), "f") if comparison.status == "calculated" and not conflicts else None})
    proof_sources = tuple(item for item in (current_derived_proof, prior_derived_proof, comparability_proof) if item is not None)
    proof = _issue_proof("quarter_comparison", comparison.comparison_id, comparison, proof_sources, ()) if status == "verified" else None
    return verification, proof


def _independent_statement_region(doc: Any, target: str):
    titles = (
        "合并资产负债表", "合并利润表", "合并现金流量表",
        "母公司资产负债表", "母公司利润表", "母公司现金流量表",
    )
    occurrences = []
    for index, page in enumerate(doc):
        for title in titles:
            found = page.search_for(title)
            if not found:
                rect = _independent_text_rect(page, title)
                found = [rect] if rect is not None else []
            occurrences.extend((index, rect, title) for rect in found)
    occurrences.sort(key=lambda item: (item[0], item[1].y0))
    wanted = [(i, r) for i, r, label in occurrences if label == target]
    if not wanted:
        return None
    start, title_rect = wanted[0]
    next_title = next(((i, r) for i, r, _ in occurrences if i > start or (i == start and r.y0 > title_rect.y0)), (len(doc), None))
    return start, title_rect, next_title


def _independent_headers(page: Any, title_rect: Any, observation: CumulativeObservation):
    suffix = _PERIOD_SUFFIX[observation.report_period]
    labels = {"current": f"{observation.report_year}年{suffix}", "comparative": f"{observation.report_year - 1}年{suffix}"}
    result = {}
    for role, label in labels.items():
        boxes = [rect for rect in page.search_for(label) if rect.y0 > title_rect.y1]
        if not boxes:
            normalized = _independent_text_rect(page, label, after_y=title_rect.y1)
            boxes = [normalized] if normalized is not None else []
        if not boxes:
            continue
        rect = min(boxes, key=lambda value: value.y0)
        result[role] = (label, rect)
    return result


def _independent_unit(page: Any, title_rect: Any):
    for query, unit, multiplier in (("单位：元", "元", "1"), ("单位：万元", "万元", "10000"), ("单位：亿元", "亿元", "100000000")):
        boxes = [rect for rect in page.search_for(query) if title_rect.y0 - 12 <= rect.y0 < title_rect.y1 + 70]
        if boxes:
            rect = min(boxes, key=lambda value: abs(value.y0 - title_rect.y0))
            text = page.get_text("text")
            match = re.search(r"币种\s*[:：]\s*([^\s，,；;]+)", text)
            currency = None
            if match:
                raw_currency = match.group(1).strip()
                currency = {
                    "人民币": "CNY", "人民币元": "CNY", "CNY": "CNY",
                    "美元": "USD", "USD": "USD", "港币": "HKD", "港元": "HKD", "HKD": "HKD",
                    "欧元": "EUR", "EUR": "EUR", "日元": "JPY", "JPY": "JPY", "GBP": "GBP",
                }.get(raw_currency, raw_currency.upper() if re.fullmatch(r"[A-Za-z]{3}", raw_currency) else None)
            return unit, currency, multiplier, rect
    return None


def _independent_rows(doc: Any, query: str, start: int, title_rect: Any, end):
    end_page, end_rect = end
    rows = []
    for index in range(start, min(len(doc), end_page + 1)):
        page = doc[index]
        found = page.search_for(query)
        if not found:
            rect = _independent_text_rect(page, query)
            found = [rect] if rect is not None else []
        for rect in found:
            if index == start and rect.y0 < title_rect.y0:
                continue
            if index == end_page and end_rect is not None and rect.y0 >= end_rect.y0:
                continue
            rows.append((index, rect))
    return rows


def _independent_text_rect(page: Any, query: str, *, after_y: float = -1.0):
    wanted = re.sub(r"\s+", "", query)
    matches = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            compact_chars: list[str] = []
            geometry: list[Any] = []
            for span in line.get("spans", []):
                span_box = pymupdf.Rect(*span["bbox"])
                for character in span.get("text", ""):
                    if character.isspace():
                        continue
                    compact_chars.append(character)
                    geometry.append(span_box)
            compact = "".join(compact_chars)
            offset = compact.find(wanted)
            block_box = pymupdf.Rect(*line["bbox"])
            if offset < 0 or block_box.y0 <= after_y:
                continue
            chosen = geometry[offset:offset + len(wanted)]
            if not chosen:
                continue
            box = pymupdf.Rect(
                min(item.x0 for item in chosen), min(item.y0 for item in chosen),
                max(item.x1 for item in chosen), max(item.y1 for item in chosen),
            )
            matches.append((block_box.y0, box))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def _independent_row_cells(doc: Any, rows, headers):
    values = {"current": [], "comparative": []}
    for page_index, row in rows:
        words = doc[page_index].get_text("words")
        for role, (label, header) in headers.items():
            center_y = (row.y0 + row.y1) / 2
            center_x = (header.x0 + header.x1) / 2
            possible = []
            for word in words:
                x0, y0, x1, y1, token = word[:5]
                if not _NUMBER.fullmatch(token) or abs((y0 + y1) / 2 - center_y) > 12.0 or x1 < row.x1 - 5:
                    continue
                possible.append((abs((x0 + x1) / 2 - center_x), token, (x0, y0, x1, y1)))
            if possible:
                possible.sort(key=lambda item: item[0])
                if possible[0][0] <= 70 and (len(possible) == 1 or abs(possible[1][0] - possible[0][0]) >= 0.1):
                    values[role].append((possible[0][1], page_index, possible[0][2], tuple(row)))
    return values


def _identity_period_state(text: str, observation: CumulativeObservation) -> str:
    suffix = {"Q1": "第一季度报告", "H1": "半年度报告", "9M": "第三季度报告", "FY": "年度报告"}[observation.report_period]
    if str(observation.report_year) in text and suffix in text:
        return "matched"
    titles = ("第一季度报告", "半年度报告", "第三季度报告", "年度报告")
    years = {int(value) for value in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text)}
    has_report_title = any(title in text for title in titles)
    if has_report_title and (any(year != observation.report_year for year in years) or any(title != suffix for title in titles if title in text)):
        return "mismatch"
    return "missing"


def _has_metric_restatement_statement(pdf_path: Path, metric_id: str) -> bool:
    doc = pymupdf.open(_safe_pdf_path(pdf_path))
    try:
        texts = [page.get_text("text") for page in doc]
        normalized = re.sub(r"\s+", "", "\n".join(texts))
        phrase = {
            "revenue": "营业收入",
            "net_profit_parent": "归母净利润",
            "operating_cash_flow": "经营活动产生的现金流量净额",
        }[metric_id]
        # 针对样例和正式来源均要求原文同时绑定指标、期间与明确否认追溯调整。
        metric_pos = normalized.find(phrase)
        if metric_pos < 0:
            return False
        neighborhood = normalized[max(0, metric_pos - 150):metric_pos + 300]
        return (
            ("未作追溯调整" in neighborhood or "未追溯调整" in neighborhood or "没有追溯调整" in neighborhood)
            and ("本报告" in neighborhood or "本期" in neighborhood)
            and ("累计数" in neighborhood or "比较数" in neighborhood)
            and ("前期差错更正" in neighborhood or "会计差错更正" in neighborhood)
            and not any(word in neighborhood for word in ("已作追溯调整", "已经追溯调整", "存在追溯调整"))
        )
    finally:
        doc.close()


def _safe_pdf_path(value: Path) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = _ROOT / candidate
    candidate = Path(os.path.abspath(candidate))
    if not _inside(candidate, _ROOT):
        raise ValueError("独立核验 PDF 必须位于项目工作区内。")
    cursor = _ROOT
    for part in candidate.relative_to(_ROOT).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("独立核验 PDF 不能经由符号链接。")
    path = candidate.resolve()
    allowed = (_ROOT / "data" / "raw", _ROOT / "data" / "samples" / "quarterly")
    if not path.is_file() or path.suffix.lower() != ".pdf" or not any(_inside(path, root.resolve()) for root in allowed):
        raise ValueError("独立核验 PDF 必须位于 data/raw 或 data/samples/quarterly。")
    return path


def _assert_obs_context(left: CumulativeObservation, right: CumulativeObservation) -> None:
    _assert_observation_context(left)
    _assert_observation_context(right)
    names = ("company_id", "metric_id", "observation_year", "scope", "currency", "unit", "unit_multiplier", "statement_type", "basis")
    if any(getattr(left, name) != getattr(right, name) for name in names) or left.basis != "year_to_date":
        raise ValueError("派生源累计值的期间、公司、指标、口径或单位不相容。")


def _obs_compatible(left: CumulativeObservation, right: CumulativeObservation) -> bool:
    try:
        _assert_observation_context(left)
        _assert_observation_context(right)
    except ValueError:
        return False
    return all(getattr(left, name) == getattr(right, name) for name in ("company_id", "metric_id", "observation_year", "scope", "currency", "unit", "unit_multiplier", "statement_type", "report_period"))


def _assert_derived_context(left: DerivedQuarter, right: DerivedQuarter) -> None:
    _assert_single_derived_context(left)
    _assert_single_derived_context(right)
    names = ("company_id", "metric_id", "currency", "unit", "unit_multiplier", "statement_type", "scope")
    if any(getattr(left, name) != getattr(right, name) for name in names):
        raise ValueError("派生季度公司、指标、币种、单位倍率或报表口径不一致。")


def _assert_observation_context(item: CumulativeObservation) -> None:
    expected_statement = _METRIC_STATEMENTS.get(item.metric_id)
    if expected_statement is None or item.statement_type != expected_statement:
        raise ValueError("累计 observation 报表类型未登记或与指标不匹配。")
    if item.scope not in {"consolidated", "parent"}:
        raise ValueError("累计 observation 必须明确标注 consolidated 或 parent 口径。")
    if not isinstance(item.currency, str) or item.currency.upper() not in _SUPPORTED_CURRENCIES:
        raise ValueError("累计 observation 缺少受支持的明确币种。")
    if item.unit not in _UNIT_MULTIPLIERS:
        raise ValueError("累计 observation 缺少受支持的明确单位。")
    multiplier = Decimal(item.unit_multiplier)
    if not multiplier.is_finite() or multiplier <= 0 or multiplier != _UNIT_MULTIPLIERS[item.unit]:
        raise ValueError("累计 observation 单位倍率缺失、非正数或与单位不匹配。")
    if item.basis != "year_to_date":
        raise ValueError("季度流量 observation 必须是 year_to_date 累计值。")


def _assert_single_derived_context(item: DerivedQuarter) -> None:
    expected_statement = _METRIC_STATEMENTS.get(item.metric_id)
    if expected_statement is None or item.statement_type != expected_statement:
        raise ValueError("派生季度报表类型未登记或与指标不匹配。")
    if item.scope not in {"consolidated", "parent"}:
        raise ValueError("派生季度必须明确标注 consolidated 或 parent 口径。")
    if not isinstance(item.currency, str) or item.currency.upper() not in _SUPPORTED_CURRENCIES:
        raise ValueError("派生季度缺少受支持的明确币种。")
    if item.unit not in _UNIT_MULTIPLIERS:
        raise ValueError("派生季度缺少受支持的明确单位。")
    multiplier = Decimal(item.unit_multiplier)
    if not multiplier.is_finite() or multiplier <= 0 or multiplier != _UNIT_MULTIPLIERS[item.unit]:
        raise ValueError("派生季度单位倍率缺失、非正数或与单位不匹配。")


def _assert_derived_matches_source(derived: DerivedQuarter, source: CumulativeObservation) -> None:
    _assert_observation_context(source)
    names = ("company_id", "metric_id", "currency", "unit", "unit_multiplier", "statement_type", "scope")
    mismatches = [name for name in names if getattr(derived, name) != getattr(source, name)]
    if mismatches:
        raise ValueError("派生季度与其源累计输入的" + "、".join(mismatches) + "不一致。")


def _require_observation_bindings(derived: DerivedQuarter, observations: Sequence[CumulativeObservation]) -> list[CumulativeObservation]:
    if len(set(derived.input_observation_ids)) != len(derived.input_observation_ids):
        raise ValueError("派生季度含重复源 observation ID。")
    selected: list[CumulativeObservation] = []
    for observation_id in derived.input_observation_ids:
        matches = [item for item in observations if item.observation_id == observation_id]
        if len(matches) != 1:
            raise _EvidenceUnavailable("派生季度每个源 observation ID 都必须唯一存在。")
        selected.append(matches[0])
    return selected


def _assert_temporal_relation(current: DerivedQuarter, prior: DerivedQuarter, relation: str) -> None:
    if relation == "year_over_year":
        valid = current.year == prior.year + 1 and current.quarter == prior.quarter
    elif relation == "quarter_over_quarter":
        expected = (current.year - 1, "Q4") if current.quarter == "Q1" else (current.year, f"Q{int(current.quarter[1]) - 1}")
        valid = (prior.year, prior.quarter) == expected
    else:
        valid = False
    if not valid:
        raise ValueError("比较期间与声明的同比/环比关系不匹配。")


def _adjacent_quarters(current: DerivedQuarter, prior: DerivedQuarter) -> bool:
    if current.quarter == "Q1":
        return prior.year == current.year - 1 and prior.quarter == "Q4"
    return prior.year == current.year and prior.quarter == f"Q{int(current.quarter[1]) - 1}"


def _period_dates(year: int, period: str):
    month, day = _PERIOD_END[period]
    return date(year, 1, 1), date(year, month, day)


def _decimal(value: str) -> Decimal:
    cleaned = value.replace(",", "").replace("，", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return Decimal(cleaned)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _proof_for(proofs: Sequence[QuarterlyProof], proof_type: str, target_id: str, bound_value: Any):
    if bound_value is None:
        return None
    matches = [proof for proof in proofs if is_current_quarterly_proof(
        proof, proof_type=proof_type, target_id=target_id, bound_value=bound_value
    )]
    return matches[0] if len(matches) == 1 else None


def _propagate_source_recheck(
    result: QuarterlyVerification,
    context: str,
    conflicts: list[str],
    limitations: list[str],
) -> None:
    if result.status == "verified":
        return
    if result.status == "conflict":
        reasons = result.conflicts or ("原文与累计 observation 不一致。",)
        conflicts.extend(f"{context}源累计 observation 冲突：{reason}" for reason in reasons)
        return
    reasons = result.limitations or ("无法从 PDF 独立复核累计 observation。",)
    limitations.extend(f"{context}源累计 observation 证据不足：{reason}" for reason in reasons)


def _issue_proof(proof_type: str, target_id: str, bound_value: Any, sources: Sequence[Any], evidence_ids: Sequence[str]) -> QuarterlyProof:
    source_hashes: set[str] = set()
    all_evidence_ids = set(evidence_ids)
    for source in sources:
        if isinstance(source, CumulativeObservation):
            source_hashes.add(source.source_sha256.lower())
        elif isinstance(source, QuarterlyProof):
            source_hashes.update(value.lower() for value in source.source_sha256s)
            all_evidence_ids.update(source.evidence_ids)
        elif isinstance(source, str) and re.fullmatch(r"[0-9a-fA-F]{64}", source):
            source_hashes.add(source.lower())
    binding_sha256 = _canonical_sha256(bound_value)
    proof = QuarterlyProof(
        f"qproof-{uuid4()}", proof_type, target_id, tuple(sorted(source_hashes)),
        tuple(sorted(all_evidence_ids)), binding_sha256, datetime.now(timezone.utc), "verified"
    )
    _PROOFS[proof.proof_id] = (proof, binding_sha256)
    return proof


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _independent_subtract(left: Decimal, right: Decimal) -> Decimal:
    # 与 finance 实现隔离；此处自行按完整位宽计算精度。
    exponent_floor = min(left.as_tuple().exponent, right.as_tuple().exponent)
    magnitude_ceiling = max(left.adjusted(), right.adjusted())
    required_precision = magnitude_ceiling - exponent_floor + 2
    with localcontext() as context:
        context.prec = max(28, required_precision)
        return left - right


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    required_precision = len(left.as_tuple().digits) + len(right.as_tuple().digits) + 4
    with localcontext() as context:
        context.prec = max(28, required_precision)
        return left * right


def _verification(target_type, target_id, status, checks, conflicts, evidence_ids, limitations, details):
    return QuarterlyVerification(
        verification_id=f"qverify-{uuid4()}", target_type=target_type, target_id=target_id, status=status,
        checks=tuple(checks), conflicts=tuple(conflicts), evidence_ids=tuple(evidence_ids),
        limitations=tuple(dict.fromkeys(limitations)), verified_at=datetime.now(timezone.utc), details=details,
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
