"""Build an internal, source-linked review packet under artifacts/evaluations only."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend" / "src"))

from scripts.evaluate_multicompany import (  # noqa: E402
    EvaluationInputError,
    PROTOCOL_PATH,
    read_json,
    reserve_evaluation_directory,
    sha256_file,
)


def _new_packet_id() -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"human-review-v2-{now}-{uuid.uuid4().hex[:8]}"


def _safe_evaluation_root(evaluation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", evaluation_id):
        raise EvaluationInputError("evaluation_id 格式无效。")
    root = (ROOT / "artifacts" / "evaluations" / evaluation_id).resolve()
    evaluations_root = (ROOT / "artifacts" / "evaluations").resolve()
    if root.parent != evaluations_root or not root.is_dir():
        raise EvaluationInputError("指定的评测归档不存在或路径不安全。")
    return root


def _load_sources() -> dict[str, Mapping[str, Any]]:
    protocol = read_json(PROTOCOL_PATH)
    entries = protocol.get("sources")
    if not isinstance(entries, list):
        raise EvaluationInputError("多公司协议缺少来源登记。")
    return {str(item["company_id"]): item for item in entries}


def validate_result_registration(
    result_companies: Any,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """Reject archived company/fact records not bound to this protocol's source."""
    if not isinstance(result_companies, dict) or set(result_companies) != set(sources_by_id):
        raise EvaluationInputError("评测结果公司集合与来源登记不一致。")

    for company_id, source in sources_by_id.items():
        company_summary = result_companies[company_id]
        if not isinstance(company_summary, dict):
            raise EvaluationInputError(f"评测公司归档结构无效：{company_id}")
        result_source = company_summary.get("source")
        if not isinstance(result_source, dict):
            raise EvaluationInputError(f"评测归档缺少来源绑定：{company_id}")
        expected_source_identity = {
            "company_id": company_id,
            "document_id": source.get("document_id"),
            "sha256": source.get("sha256"),
            "report_year": source.get("report_year"),
        }
        for key, expected in expected_source_identity.items():
            actual = result_source.get(key)
            if key == "sha256" and isinstance(actual, str) and isinstance(expected, str):
                matches = actual.lower() == expected.lower()
            else:
                matches = actual == expected
            if not matches:
                raise EvaluationInputError(f"评测结果来源登记冲突：{company_id} {key}")

        records = company_summary.get("facts")
        if not isinstance(records, list) or not records:
            raise EvaluationInputError(f"评测归档缺少复核事实：{company_id}")
        for record in records:
            fact = record.get("fact") if isinstance(record, dict) else None
            if not isinstance(fact, dict):
                raise EvaluationInputError(f"评测事实缺少结构化来源字段：{company_id}")
            expected_fact_identity = {
                "company_id": company_id,
                "source_document_id": source.get("document_id"),
                "source_sha256": source.get("sha256"),
                "report_year": source.get("report_year"),
            }
            for key, expected in expected_fact_identity.items():
                actual = fact.get(key)
                if key in {"source_sha256"} and isinstance(actual, str) and isinstance(expected, str):
                    matches = actual.lower() == expected.lower()
                else:
                    matches = actual == expected
                if not matches:
                    raise EvaluationInputError(f"评测事实来源登记冲突：{company_id} {key}")


def validate_provisional_case(case: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """Validate a provisional case before using its page/table strings as review hints."""
    company_id = str(source["company_id"])
    if case.get("review_status") != "agent_provisional_pending_human" or case.get("human_review_status") != "pending":
        raise EvaluationInputError(f"人工复核候选案例已非 pending：{company_id}")
    if case.get("company", {}).get("company_id") != company_id:
        raise EvaluationInputError(f"人工复核候选案例公司冲突：{company_id}")
    if case.get("report", {}).get("report_year") != source.get("report_year"):
        raise EvaluationInputError(f"人工复核候选案例报告年度冲突：{company_id}")
    case_source = case.get("source")
    if not isinstance(case_source, dict):
        raise EvaluationInputError(f"人工复核候选案例缺少来源：{company_id}")
    identity = {
        "document_id": source.get("document_id"),
        "source_url": source.get("source_url"),
        "local_path": source.get("source_path"),
        "sha256": source.get("sha256"),
    }
    for key, expected in identity.items():
        actual = case_source.get(key)
        if key == "sha256" and isinstance(actual, str) and isinstance(expected, str):
            matches = actual.lower() == expected.lower()
        else:
            matches = actual == expected
        if not matches:
            raise EvaluationInputError(f"人工复核候选案例来源冲突：{company_id} {key}")
    return str(case.get("case_id", ""))


def load_provisional_case_for_review(company_id: str, source: Mapping[str, Any]) -> Mapping[str, Any] | None:
    case_path = ROOT / "evaluation" / "cases" / f"{company_id}-2024-provisional.json"
    if not case_path.exists():
        return None
    case = read_json(case_path)
    validate_provisional_case(case, source)
    return case


def provisional_review_locators(case: Mapping[str, Any] | None, fact: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return explicitly unverified candidate locators; never used as verifier evidence."""
    if case is None:
        return []
    metric_id = fact.get("metric_id")
    period_end = str(fact.get("period_end") or "")
    if len(period_end) < 4 or not period_end[:4].isdigit():
        return []
    year = period_end[:4]
    matches = [item for item in case.get("labels", []) if item.get("metric_id") == metric_id]
    if len(matches) != 1:
        if matches:
            raise EvaluationInputError(f"人工复核候选指标重复：{case.get('case_id')} {metric_id}")
        return []
    period = matches[0].get("period_values", {}).get(year)
    if not isinstance(period, dict):
        return []
    if period.get("value_status") != "agent_provisional_pending_human":
        raise EvaluationInputError(f"候选财务事实不是 provisional pending：{case.get('case_id')} {metric_id} {year}")
    if str(period.get("raw_value")) != str(fact.get("raw_value")) or str(period.get("normalized_value")) != str(fact.get("normalized_value")):
        raise EvaluationInputError(f"人工复核候选值与本轮评测事实冲突：{case.get('case_id')} {metric_id} {year}")

    locators: list[dict[str, Any]] = []
    for index, evidence in enumerate(period.get("evidence", [])):
        if not isinstance(evidence, dict) or not isinstance(evidence.get("pdf_page"), int):
            raise EvaluationInputError(f"人工复核候选页码无效：{case.get('case_id')} {metric_id} {year}")
        locators.append(
            {
                "candidate_id": f"{case.get('case_id')}:{metric_id}:{year}:{index}",
                "provenance": "agent_provisional_case_candidate_not_independent_evidence",
                "candidate_status": "unverified_until_human_review",
                "pdf_page": evidence["pdf_page"],
                "printed_page": evidence.get("printed_page"),
                "table_title": evidence.get("table"),
                "row_label": evidence.get("row"),
                "column_label": evidence.get("column"),
                "raw_value": period.get("raw_value"),
                "normalized_value": period.get("normalized_value"),
                "unit": matches[0].get("unit", {}).get("value"),
                "currency": matches[0].get("currency", {}).get("value"),
            }
        )
    return locators


def _unit_and_currency(fact: Mapping[str, Any], evidence: list[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    units = [str(item["unit"]) for item in evidence if item.get("unit")]
    currencies = [str(item["currency"]) for item in evidence if item.get("currency")]
    unit = units[0] if units and len(set(units)) == 1 else fact.get("unit")
    currency = currencies[0] if currencies and len(set(currencies)) == 1 else fact.get("currency")
    return (None if unit is None else str(unit), None if currency is None else str(currency))


def _render_page_images(
    source_path: Path,
    company_id: str,
    pages: set[int],
    output_dir: Path,
) -> dict[int, str]:
    try:
        import pymupdf

        document = pymupdf.open(source_path)
    except Exception as exc:
        raise EvaluationInputError(f"无法读取复核原文 PDF：{company_id}") from exc
    images: dict[int, str] = {}
    try:
        for page_number in sorted(pages):
            if page_number < 1 or page_number > document.page_count:
                raise EvaluationInputError(f"复核页超出 PDF 页数：{company_id} p.{page_number}")
            relative = f"pages/{company_id}-p{page_number:03d}.png"
            destination = output_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise EvaluationInputError(f"拒绝覆盖既有页图：{destination.name}")
            pixmap = document[page_number - 1].get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), alpha=False)
            pixmap.save(destination)
            images[page_number] = relative
    finally:
        document.close()
    return images


def _build_fact_item(
    company_id: str,
    source: Mapping[str, Any],
    record: Mapping[str, Any],
    page_images: Mapping[int, str],
    provisional_locators: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    fact = dict(record["fact"])
    evidence = [dict(item) for item in record.get("independent_evidence", [])]
    unit, currency = _unit_and_currency(fact, evidence)
    located = []
    for item in evidence:
        page = int(item["pdf_page"])
        located.append(
            {
                "evidence_id": item.get("evidence_id"),
                "pdf_page": page,
                "printed_page": item.get("printed_page"),
                "table_title": item.get("table_title"),
                "row_label": item.get("row_label"),
                "column_label": item.get("column_label"),
                "raw_value": item.get("value_raw"),
                "normalized_value": item.get("value_normalized"),
                "unit": item.get("unit"),
                "currency": item.get("currency"),
                "bbox": item.get("value_region", {}).get("bbox") if isinstance(item.get("value_region"), dict) else None,
                "page_image": page_images.get(page),
            }
        )
    candidates = []
    for candidate in provisional_locators or []:
        page = int(candidate["pdf_page"])
        candidates.append({**dict(candidate), "page_image": page_images.get(page)})
    return {
        "company_id": company_id,
        "company_name": source["company_name"],
        "report_year": fact.get("report_year"),
        "metric_id": fact.get("metric_id"),
        "indicator_name": fact.get("label_raw"),
        "raw_value": fact.get("raw_value"),
        "normalized_value": fact.get("normalized_value"),
        "unit": unit,
        "unit_multiplier": fact.get("unit_multiplier"),
        "currency": currency,
        "statement_scope": fact.get("scope"),
        "period_start": fact.get("period_start"),
        "period_end": fact.get("period_end"),
        "period_type": fact.get("period_type"),
        "comparison_role": fact.get("comparison_role"),
        "restatement_status_on_fact": fact.get("restatement_status"),
        "source_document_id": fact.get("source_document_id"),
        "source_sha256": fact.get("source_sha256"),
        "source_evidence": located,
        "provisional_reference_locators": candidates,
        "independent_reverification": {
            "status": record.get("verifier_status"),
            "result": record.get("verifier_result"),
            "elapsed_ms": record.get("elapsed_ms"),
        },
        "factual_review_status": "agent_provisional_pending_human",
        "human_review": {
            "status": "pending",
            "conclusion": None,
            "reviewer_name": None,
            "review_date": None,
            "supporting_basis": None,
            "notes": None,
        },
    }


def _markdown_packet(packet: Mapping[str, Any]) -> str:
    lines = [
        "# FINTRACE 多公司人工复核包",
        "",
        f"- 复核包 ID：`{packet['packet_id']}`",
        f"- 来源评测 ID：`{packet['evaluation_id']}`",
        "- 资料性质：三家公司的真实官方披露 2024 年年报；表内事实由 agent provisional 转录，尚待人工复核。",
        "- 评测标签：不评定舞弊或无舞弊；异常线索不等于舞弊结论。",
        "- 再分发边界：PDF、原文片段及页图仅本地开发/评测，未确认再分发许可。此目录属于本地 `artifacts/evaluations/`，不要提交或公开分发。",
        "- 人工 gate：`pending`；复核人、日期、结论和依据均留空。真人复核人及其他电脑/网络规则尚未确定。",
        "",
        "## 复核流程",
        "",
        "1. 用来源 URL、SHA256 和 PDF 页码确认当前复核页对应登记的原始报告。",
        "2. 回到页图和原始 PDF 核对表题、行、列、原始金额、单位/倍率、币种、报告期和合并/母公司口径；不要把空白值当零。",
        "3. 对追溯调整、会计政策/估计变化、重大会计差错更正分别记录原文页码和支持；“未找到”或系统 `insufficient_evidence` 保持 unknown。",
        "4. 将人工结论、复核人、日期与原文依据填入结构化 JSON 对应事实的 `human_review` 字段。证据冲突与证据不足要分开记录；未知不能转成通过。",
        "5. 复核完成后仍不得把财务事实复核等同于舞弊判断或 PDF 再分发许可。",
        "",
        "## 来源与复核事实",
        "",
    ]
    for source in packet["sources"]:
        cid = source["company_id"]
        lines.extend(
            [
                f"### {source['company_name']}（{cid}）",
                "",
                f"- 报告：{source['report_year']} 年年度报告；披露日 {source['disclosure_date']}。",
                f"- 来源：[{source['source_url']}]({source['source_url']})。",
                f"- SHA256：`{source['sha256']}`；本地仓库相对来源：`{source['source_path']}`。",
                f"- 用途：{source['use_condition']}",
                f"- 评测角色：`{source['company_role']}`；{source['previous_exposure']}",
                "",
                "| 指标 | 期间/角色 | 原文值 | 规范值 | 单位 | 币种 | 口径 | 独立核验 | 页码 / 表 / 行 / 列 | 人工结论 |",
                "|---|---|---:|---:|---|---|---|---|---|---|",
            ]
        )
        for item in packet["facts"]:
            if item["company_id"] != cid:
                continue
            direct_locators = item["source_evidence"]
            locator_parts = [
                f"PDF {loc['pdf_page']} / {loc.get('table_title') or '表名待复核'} / {loc.get('row_label') or item['indicator_name']} / {loc.get('column_label') or item['comparison_role']}"
                for loc in direct_locators
            ]
            locator_parts.extend(
                f"候选定位（agent provisional，未核实）PDF {loc['pdf_page']} / {loc.get('table_title') or '表名待复核'} / {loc.get('row_label') or item['indicator_name']} / {loc.get('column_label') or item['comparison_role']}"
                for loc in item.get("provisional_reference_locators", [])
            )
            locator_text = "；".join(locator_parts) or "原文定位不足，待复核"
            lines.append(
                f"| {item['indicator_name']} | {item['period_end']} / {item['comparison_role']} | {item['raw_value']} | {item['normalized_value']} | {item['unit'] or 'unknown'} | {item['currency'] or 'unknown'} | {item['statement_scope']} | {item['independent_reverification']['status']} | {locator_text} | pending |"
            )
        lines.extend(["", "会计变化与追溯披露（原文记录仍为 provisional）：", ""])
        for disclosure in source["provisional_disclosures"]:
            page_image = packet["page_images"].get(cid, {}).get(str(disclosure["pdf_page"]), "")
            image_note = f"；页图：`{page_image}`" if page_image else ""
            lines.append(
                f"- PDF 第 {disclosure['pdf_page']} 页，{disclosure['topic']}：{disclosure['provisional_note']}{image_note}"
            )
        company = packet["evaluation_companies"][cid]
        comp = company["comparability"]
        lines.extend(
            [
                f"- 系统比较/追溯核验：`{comp.get('status')}` / `{comp.get('restatement_status', 'unknown')}`；不得用报告中的片段覆盖该独立核验状态。",
                "",
                "页图索引：",
                "",
            ]
        )
        for page, image in sorted(packet["page_images"].get(cid, {}).items(), key=lambda pair: int(pair[0])):
            lines.append(f"- PDF 第 {page} 页：`{image}`")
        lines.append("")
    return "\n".join(lines)


def _html_packet(packet: Mapping[str, Any]) -> str:
    rows = []
    for source in packet["sources"]:
        cid = source["company_id"]
        rows.append(f"<h2>{html.escape(source['company_name'])}（{html.escape(cid)}）</h2>")
        source_url = html.escape(str(source["source_url"]), quote=True)
        rows.append("<p>真实官方披露年报；所有事实保持 agent provisional，人工状态 pending。</p>")
        rows.append(
            "<dl class=source-meta>"
            f"<dt>报告披露日</dt><dd>{html.escape(str(source['disclosure_date']))}</dd>"
            f"<dt>来源 URL</dt><dd><a href=\"{source_url}\">{source_url}</a></dd>"
            f"<dt>SHA256</dt><dd><code>{html.escape(str(source['sha256']))}</code></dd>"
            f"<dt>用途与许可边界</dt><dd>{html.escape(str(source['use_condition']))}</dd>"
            "</dl>"
        )
        rows.append("<table><thead><tr><th>指标 / 期间</th><th>原值 → 规范值</th><th>单位 / 币种</th><th>口径</th><th>独立核验</th><th>行列定位</th><th>人工复核</th></tr></thead><tbody>")
        for item in packet["facts"]:
            if item["company_id"] != cid:
                continue
            direct_locator = "<br>".join(
                html.escape(
                    f"PDF {loc['pdf_page']} · {loc.get('table_title') or ''} · {loc.get('row_label') or item['indicator_name']} · {loc.get('column_label') or item['comparison_role']}"
                )
                for loc in item["source_evidence"]
            )
            candidate_locator = "<br>".join(
                "<em>候选定位（agent provisional，未核实）</em> "
                + html.escape(
                    f"PDF {loc['pdf_page']} · {loc.get('table_title') or ''} · {loc.get('row_label') or item['indicator_name']} · {loc.get('column_label') or item['comparison_role']}"
                )
                for loc in item.get("provisional_reference_locators", [])
            )
            locator = "<br>".join(part for part in (direct_locator, candidate_locator) if part) or "原文定位不足，待复核"
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(item['indicator_name']))}<br>{html.escape(str(item['period_end']))} / {html.escape(str(item['comparison_role']))}</td>"
                f"<td>{html.escape(str(item['raw_value']))} → {html.escape(str(item['normalized_value']))}</td>"
                f"<td>{html.escape(str(item['unit'] or 'unknown'))} / {html.escape(str(item['currency'] or 'unknown'))}</td>"
                f"<td>{html.escape(str(item['statement_scope']))}</td>"
                f"<td>{html.escape(str(item['independent_reverification']['status']))}</td>"
                f"<td>{locator}</td><td>pending<br>复核人：____ 日期：____ 依据：____</td></tr>"
            )
        rows.append("</tbody></table>")
        rows.append("<h3>会计变化与追溯披露（provisional）</h3><ul>")
        for disclosure in source["provisional_disclosures"]:
            page = str(disclosure["pdf_page"])
            image = packet["page_images"].get(cid, {}).get(page)
            image_html = f'<p><img src="{html.escape(image)}" alt="{html.escape(cid)} PDF page {page}"></p>' if image else ""
            rows.append(
                f"<li>PDF 第 {page} 页：{html.escape(disclosure['topic'])}；{html.escape(disclosure['provisional_note'])}{image_html}</li>"
            )
        rows.append("</ul>")
        rows.append(
            f"<p>系统可比性 / 追溯核验：<code>{html.escape(str(packet['evaluation_companies'][cid]['comparability'].get('status')))}</code> / <code>{html.escape(str(packet['evaluation_companies'][cid]['comparability'].get('restatement_status', 'unknown')))}</code>。</p>"
        )
        rows.append("<h3>原文页图</h3>")
        for page, image in sorted(packet["page_images"].get(cid, {}).items(), key=lambda pair: int(pair[0])):
            rows.append(f'<figure><figcaption>PDF 第 {html.escape(str(page))} 页</figcaption><img src="{html.escape(image)}" alt="{html.escape(cid)} page {html.escape(str(page))}"></figure>')
    return """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FINTRACE 多公司人工复核包</title>
<style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#18202b;line-height:1.55}table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{border:1px solid #bac3ce;padding:.5rem;text-align:left;vertical-align:top}th{background:#edf1f5}img{max-width:100%;height:auto;border:1px solid #bac3ce}figure{margin:1rem 0}code{overflow-wrap:anywhere}.gate{background:#fff2d8;padding:1rem;border-left:4px solid #b46d00}.source-meta{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem}.source-meta dt{font-weight:700}</style>
<body><h1>FINTRACE 多公司人工复核包</h1>
<div class="gate"><strong>人工 gate：pending。</strong> 复核人、日期、结论及依据留空；真人复核人及其他设备/网络规则尚未确定。真实披露事实不是舞弊标签，页图只供本地核验。</div>
<p>来源评测：""" + html.escape(str(packet["evaluation_id"])) + """。资料为真实官方年报，许可边界为仅本地开发与评测，未确认原文及页图再分发许可。错例样本与真实事实分开；异常线索不等于舞弊结论。</p>
""" + "\n".join(rows) + "\n</body></html>\n"


def build_human_review_packet(evaluation_id: str, packet_id: str | None = None) -> tuple[Path, dict[str, Any]]:
    evaluation_root = _safe_evaluation_root(evaluation_id)
    result = read_json(evaluation_root / "result.json")
    if result.get("protocol_id") != "fintrace-multicompany-evaluation-v2":
        raise EvaluationInputError("评测归档不是受支持的 C-01 v2 结果。")
    if result.get("human_review_gate", {}).get("status") != "pending":
        raise EvaluationInputError("人工 gate 已非 pending，拒绝自动生成已通过复核包。")
    sources_by_id = _load_sources()
    result_companies = result.get("companies")
    validate_result_registration(result_companies, sources_by_id)

    prepared: dict[str, tuple[Mapping[str, Any], Path, list[dict[str, Any]], set[int]]] = {}
    facts: list[dict[str, Any]] = []
    for company_id, source in sources_by_id.items():
        pdf_path = (ROOT / str(source["source_path"])).resolve()
        raw_root = (ROOT / "data" / "raw").resolve()
        if not pdf_path.is_relative_to(raw_root) or not pdf_path.is_file():
            raise EvaluationInputError(f"复核原文不存在或不在 data/raw 内：{company_id}")
        if sha256_file(pdf_path).lower() != str(source["sha256"]).lower():
            raise EvaluationInputError(f"复核原文 SHA256 已变化：{company_id}")
        company_summary = result_companies[company_id]
        records = company_summary.get("facts")
        provisional_case = load_provisional_case_for_review(company_id, source)
        pages = {int(item["pdf_page"]) for disclosure in source["provisional_disclosures"] for item in [disclosure]}
        for record in records:
            if record.get("factual_review_status") != "agent_provisional_pending_human":
                raise EvaluationInputError("事实不是 pending 状态，复核包拒绝标成已通过。")
            if record.get("verifier_status") not in {"verified", "conflict", "insufficient_evidence"}:
                raise EvaluationInputError("事实缺少有效独立核验状态。")
            for evidence in record.get("independent_evidence", []):
                pages.add(int(evidence["pdf_page"]))
            candidates = provisional_review_locators(provisional_case, record["fact"])
            pages.update(int(item["pdf_page"]) for item in candidates)
            facts.append(
                {
                    "company_id": company_id,
                    "record": record,
                    "source": source,
                    "provisional_locators": candidates,
                }
            )
        prepared[company_id] = (source, pdf_path, records, pages)

    selected_packet_id = packet_id or _new_packet_id()
    output_dir = reserve_evaluation_directory(ROOT / "artifacts" / "evaluations", selected_packet_id)
    images: dict[str, dict[str, str]] = {}
    for company_id, (source, pdf_path, _records, pages) in prepared.items():
        rendered = _render_page_images(pdf_path, company_id, pages, output_dir)
        images[company_id] = {str(page): path for page, path in rendered.items()}

    items = [
        _build_fact_item(
            item["company_id"],
            item["source"],
            item["record"],
            {int(page): relative for page, relative in images[item["company_id"]].items()},
            provisional_locators=item["provisional_locators"],
        )
        for item in facts
    ]
    packet = {
        "schema_version": 1,
        "packet_id": selected_packet_id,
        "evaluation_id": evaluation_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packet_nature": "internal_source_review_material",
        "data_nature": "real_official_annual_reports; agent_provisional_pending_human",
        "fraud_or_misconduct_label": {"status": "not_assessed", "value": None},
        "human_review_gate": {
            "status": "pending",
            "reviewer_name": None,
            "review_date": None,
            "other_computer_or_network_rules": "unconfirmed",
        },
        "redistribution_boundary": "仅本地开发/评测；PDF、原文片段与复核页图未确认再分发许可，不提交公开 Git。",
        "sources": [_source_for_packet(sources_by_id[cid]) for cid in sorted(sources_by_id)],
        "facts": items,
        "evaluation_companies": result_companies,
        "page_images": images,
        "review_procedure": [
            "确认报告来源 URL、SHA256 与页码。",
            "逐项核对原文表题、行列、金额、单位/倍率、币种、口径与期间。",
            "会计变化、差错更正、追溯与比较性分别依据原文页码复核；证据不足保持 unknown。",
            "将真人结论、复核人、日期及证据写入相应 human_review 空字段。",
            "不得把事实复核解释为舞弊判断或 PDF 再分发许可。",
        ],
    }
    _write_json_new(output_dir / "review_packet.json", packet)
    _write_text_new(output_dir / "review_packet.md", _markdown_packet(packet))
    _write_text_new(output_dir / "index.html", _html_packet(packet))
    return output_dir, packet


def _source_for_packet(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: source.get(key)
        for key in (
            "company_id",
            "company_name",
            "company_role",
            "adaptation_status",
            "previous_exposure",
            "report_type",
            "report_year",
            "document_id",
            "source_path",
            "source_url",
            "disclosure_date",
            "sha256",
            "byte_size",
            "pdf_pages",
            "use_condition",
            "provisional_disclosures",
        )
    }


def _write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, ensure_ascii=False, indent=2, default=str)
            target.write("\n")
    except FileExistsError as exc:
        raise EvaluationInputError(f"拒绝覆盖既有复核文件：{path}") from exc


def _write_text_new(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as target:
            target.write(value)
    except FileExistsError as exc:
        raise EvaluationInputError(f"拒绝覆盖既有复核文件：{path}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从本地原文与 C-01 v2 归档生成内部人工复核包。")
    parser.add_argument("--evaluation-id", required=True, help="已存在的 C-01 评测归档 ID")
    parser.add_argument("--packet-id", default=None, help="可选唯一复核包 ID；已存在时失败。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output_dir, packet = build_human_review_packet(args.evaluation_id, args.packet_id)
    except (EvaluationInputError, OSError, ValueError) as exc:
        print(f"status=failed reason={exc}", file=sys.stderr)
        return 2
    print(f"status=completed packet_id={packet['packet_id']} facts={len(packet['facts'])}")
    print(f"output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
