"""安全地从已校验年度归档生成 JSON、Markdown 与离线 HTML 下载。"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from finagent.api.annual_report_read import AnnualReportArchive, load_annual_report
from finagent.core.sample_provenance import resolve_sample_provenance
from finagent.reports.annual_report import render_markdown

_FORMATS = {"json", "markdown", "html"}
_ASCII_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_ROOT_FIELDS = (
    "kind", "title", "run_id", "company_id", "report_year", "source_document_id",
    "source_sha256", "fraud_conclusion", "scope", "comparability", "confirmed",
    "verified_claims", "interpretations", "pending_review", "limitations", "model_called",
    "m3_screening", "model_investigation", "model_review", "model_discovery",
)
_EVIDENCE_FIELDS = (
    "evidence_id", "document_id", "source_sha256", "pdf_page", "printed_page",
    "table_title", "row_label", "column_label", "value_raw", "value_normalized", "unit",
    "currency", "period_start", "period_end", "period_type", "value_region", "row_region",
    "column_region", "title_region", "unit_region", "legacy_text_region", "extraction_method",
    "surrounding_text", "limitations", "coordinate_system",
)
_REGION_FIELDS = ("text", "page", "bbox")
_BOX_FIELDS = ("x0", "y0", "x1", "y1")
_VERIFICATION_FIELDS = (
    "verification_id", "target_type", "target_id", "status", "checks", "conflicts",
    "evidence_ids", "limitations", "verified_at",
)
_METRIC_FIELDS = (
    "fact_id", "metric_id", "company_id", "label_raw", "raw_value", "normalized_value",
    "currency", "unit_multiplier", "unit", "report_year", "period_start", "period_end",
    "period_type", "frequency", "statement_type", "scope", "comparison_role",
    "restatement_status", "source_document_id", "source_sha256", "evidence_ids",
    "extraction_method", "limitations", "schema_version", "placement", "placement_reasons",
)
_CALCULATION_FIELDS = (
    "calculation_id", "formula_id", "formula_expression", "input_fact_ids", "output_value",
    "unit", "status", "failure_reason", "rule_version", "placement", "placement_reasons",
)
_CLAIM_FIELDS = (
    "claim_id", "claim_type", "text", "supporting_fact_ids", "supporting_evidence_ids",
    "calculation_ids", "verification_status", "limitations", "alternative_explanations",
    "follow_up_items", "placement", "placement_reasons",
)
_CANDIDATE_FIELDS = (
    "signal_id", "title", "status", "reason", "input_fact_ids", "calculation_ids",
    "left_difference", "right_difference", "formula", "value", "fraud_conclusion",
    "placement", "placement_reasons",
)


@dataclass(frozen=True, slots=True)
class AnnualReportExport:
    content: bytes
    media_type: str
    filename: str
    content_security_policy: str | None = None


class AnnualReportExportError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def export_annual_report(project_root: Path, run_id: str, format: str) -> AnnualReportExport:
    """Only read through the archive validator; never open arbitrary report paths."""

    if format not in _FORMATS:
        raise AnnualReportExportError(422, "unsupported_export_format", "format 只支持 json、markdown 或 html。")
    archive = load_annual_report(project_root, run_id)
    payload = project_export(archive)
    filename_id = _safe_filename_id(archive.run_id)
    if format == "json":
        content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        return AnnualReportExport(content, "application/json; charset=utf-8", f"fintrace-annual-{filename_id}.json")
    if format == "markdown":
        markdown = render_safe_markdown(payload)
        return AnnualReportExport(markdown.encode("utf-8"), "text/markdown; charset=utf-8", f"fintrace-annual-{filename_id}.md")
    return AnnualReportExport(
        render_html(payload).encode("utf-8"),
        "text/html; charset=utf-8",
        f"fintrace-annual-{filename_id}.html",
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    )


def project_export(archive: AnnualReportArchive) -> dict[str, Any]:
    """Project only the reviewed annual report and append-only layer contracts."""

    summary = archive.manifest_summary
    identity = {
        "run_id": summary["run_id"],
        "company_id": summary["company_id"],
        "report_year": summary["report_year"],
        "document_id": summary["document_id"],
        "source_sha256": summary["source_pdf_sha256"],
    }
    verified = {item["evidence_id"]: item for item in archive.verification_evidences}
    narrative = {item["evidence_id"]: item for item in archive.narrative_evidences}
    discovery = {item["evidence_id"]: item for item in archive.discovery_evidences}
    evidence_ids = set(verified) | set(narrative) | set(discovery)
    report = _project_report(archive.report, identity, verified, narrative, discovery, evidence_ids)
    manifest = _pick(summary, (
        "run_id", "status", "mode", "company_id", "report_year", "document_id",
        "source_pdf_archive_path", "report_json_path", "source_pdf_sha256", "report_sha256",
    ))
    provenance = resolve_sample_provenance(
        summary["source_pdf_sha256"],
        company_id=summary["company_id"],
        report_year=summary["report_year"],
        document_id=summary["document_id"],
    )
    return {
        "schema_version": 1,
        "export_kind": "fintrace_annual_report_export",
        "report": report,
        "manifest": manifest,
        "source_provenance": provenance,
        "evidence_index": {
            "verification_evidences": [_project_verified_index_evidence(item) for item in archive.verification_evidences],
            "narrative_evidences": [_project_narrative_evidence(item) for item in archive.narrative_evidences],
            "discovery_evidences": [_project_discovery_index_evidence(item) for item in archive.discovery_evidences],
        },
        "export_limitations": [
            "导出保留归档各层状态，不会提升事实、计算或因果核验等级。",
            "M3 分数为未校准规则试行分数，不是概率。",
            "AI 调查与最终评审属于模型观点，不是独立核验或因果结论。",
            "候选线索不是舞弊结论。",
        ],
    }


def render_safe_markdown(payload: Mapping[str, Any]) -> str:
    """Reuse the official annual renderer after neutralizing untrusted Markdown/HTML."""

    report = _markdown_tree(payload["report"])
    provenance = _markdown_tree(payload["source_provenance"])
    text = "# 来源性质\n\n**" + str(provenance["label"]) + "**\n\n"
    text += "\n".join("- " + str(item) for item in (provenance.get("use_conditions") or [],) if item)
    rendered_report = render_markdown(report).rstrip()
    comparability = report.get("comparability")
    if isinstance(comparability, Mapping) and isinstance(comparability.get("source_binding_status"), str):
        heading = "## 年度可比性核验"
        if heading in rendered_report:
            rendered_report = rendered_report.replace(
                heading,
                f"{heading}\n\n- 来源绑定状态：`{comparability['source_binding_status']}`",
                1,
            )
    text += "\n\n" + rendered_report
    discovery_markdown = _render_discovery_markdown(report.get("model_discovery"))
    if discovery_markdown:
        text += "\n\n" + discovery_markdown
    investigation = report.get("model_investigation")
    if isinstance(investigation, Mapping):
        from finagent.reports.investigation_append import render_investigation_markdown

        text += "\n\n" + render_investigation_markdown(investigation).rstrip()
    elif isinstance(report.get("model_review"), Mapping):
        text += "\n\n" + _render_review_markdown(report["model_review"])
    screening = report.get("m3_screening")
    if isinstance(screening, Mapping):
        from finagent.reports.m3_annual_append import render_report_with_m3_screening

        text = render_report_with_m3_screening(text, screening)
    return text.rstrip() + "\n"


def render_html(payload: Mapping[str, Any]) -> str:
    report = payload["report"]
    manifest = payload["manifest"]
    provenance = payload["source_provenance"]
    company = report.get("company_id", "未提供")
    year = report.get("report_year", "未提供")
    facts = [
        *((fact, "已核实") for fact in report["confirmed"]["metrics"]),
        *((fact, "待核查") for fact in report["pending_review"]["facts"]),
    ]
    calculations = [
        *((item, "已核实") for item in report["confirmed"]["analyses"]),
        *((item, "待核查") for item in report["pending_review"]["calculations"]),
    ]
    rows = [
        ("运行 ID", manifest.get("run_id")), ("归档状态", manifest.get("status")),
        ("运行模式", manifest.get("mode")), ("来源文档 ID", manifest.get("document_id")),
        ("来源 SHA256", manifest.get("source_pdf_sha256")),
    ]
    body = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\">",
        "<title>FINTRACE 年度分析报告</title><style>",
        "*{box-sizing:border-box}body{font:16px/1.65 system-ui,'Microsoft YaHei',sans-serif;color:#18212f;margin:0;background:#f4f6f8}",
        "main{max-width:1100px;margin:0 auto;padding:32px 22px;background:#fff}h1,h2,h3{line-height:1.25}h1{font-size:2rem;margin:0 0 8px}h2{font-size:1.35rem;border-bottom:1px solid #d7dee8;padding-bottom:8px;margin-top:36px}",
        ".notice{border:2px solid #a44b00;background:#fff6e8;padding:14px 18px;margin:18px 0;font-weight:700}.muted{color:#526176}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;margin:14px 0;font-size:.93rem}th,td{border:1px solid #cbd5e1;padding:9px 10px;text-align:left;vertical-align:top}th{background:#eef2f7}td{overflow-wrap:anywhere}.entry{border-left:3px solid #9aa9bc;padding:8px 14px;margin:12px 0;background:#f8fafc}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:inherit}.pill{display:inline-block;border:1px solid #9aa9bc;border-radius:4px;padding:0 7px}.no-break{break-inside:avoid}",
        "@page{size:A4 landscape;margin:12mm}@media(max-width:600px){main{padding:20px 14px}body{font-size:15px}h1{font-size:1.65rem}table{font-size:.86rem}th,td{padding:7px}}@media print{body{background:#fff;font-size:9pt}main{max-width:none;padding:0;margin:0}.notice{break-inside:avoid}h2{break-after:avoid}table{font-size:8pt;table-layout:fixed}th,td{padding:4px 5px;overflow-wrap:anywhere;word-break:break-word}thead{display:table-header-group}tr,.entry{break-inside:avoid}a{color:inherit;text-decoration:none}}",
        "</style></head><body><main>",
        "<h1>FINTRACE 年度分析报告</h1>",
        f"<p>{_h(company)} · {_h(year)} 年</p>",
        f"<div class=\"notice\">{_h(provenance.get('label', '来源性质未确认'))}。候选线索不是确认舞弊。</div>",
        _html_table(("字段", "归档值"), ((key, value) for key, value in rows)),
        "<p class=\"muted\">本导出只呈现已校验归档中的受控字段。导出不提升任何事实、计算、模型观点或因果核验等级。下载 HTML 后可离线查看，也可用浏览器打印成 PDF。</p>",
    ]
    body.extend(_render_source_nature(provenance))
    body.extend(_render_comparability(report.get("comparability")))
    body.append("<h2>指标事实与核验</h2>")
    fact_rows = []
    for fact, placement in facts:
        citations = _citation_text(fact.get("verification_evidences", []), fact.get("evidences", []))
        states = ", ".join(str(item.get("status", "未提供")) for item in fact.get("verifications", [])) or "未提供"
        fact_rows.append((
            placement, fact.get("label_raw", fact.get("metric_id", "未提供")),
            fact.get("raw_value"), fact.get("normalized_value"), fact.get("unit_multiplier"),
            fact.get("unit"), fact.get("currency"),
            f"{fact.get('period_start')} 至 {fact.get('period_end')} ({fact.get('period_type')})",
            fact.get("scope"), states, citations,
        ))
    body.append(_html_table(("分区", "指标", "原始金额", "规范金额", "单位倍率", "单位", "币种", "期间", "口径", "核验状态", "页码 / 证据 ID"), fact_rows))
    body.append("<h2>计算与独立重算</h2>")
    calc_rows = []
    for item, placement in calculations:
        independent = item.get("independent_verification") or {}
        check_state = independent.get("status", "未提供")
        check_value = independent.get("recomputed_value")
        calc_rows.append((placement, item.get("formula_id"), item.get("formula_expression"), item.get("output_value"), item.get("unit"), item.get("status"), check_state, check_value, ", ".join(item.get("input_fact_ids", []))))
    body.append(_html_table(("分区", "公式", "表达式", "结果", "单位", "计算状态", "独立核验", "重算值", "输入事实"), calc_rows))
    body.extend(_render_claims("已核实主张与推论假设", [*report.get("verified_claims", []), *report.get("interpretations", [])]))
    body.extend(_render_candidates(report.get("pending_review", {}).get("candidate_signals", [])))
    body.extend(_render_m3(report.get("m3_screening")))
    body.extend(_render_model_layer(report.get("model_investigation"), report.get("model_review")))
    body.extend(_render_discovery(report.get("model_discovery")))
    body.append("<h2>范围、缺口与限制</h2>")
    body.extend(_render_text_list(report.get("scope", {}).get("gaps", []), "范围缺口"))
    body.extend(_render_text_list(report.get("limitations", []), "报告限制"))
    body.extend(_render_text_list(payload.get("export_limitations", []), "导出说明"))
    body.append("</main></body></html>")
    return "".join(body)


def _project_report(
    source: Mapping[str, Any],
    identity: Mapping[str, Any],
    verified: Mapping[str, Mapping[str, Any]],
    narrative: Mapping[str, Mapping[str, Any]],
    discovery_evidence: Mapping[str, Mapping[str, Any]],
    valid_evidence_ids: set[str],
) -> dict[str, Any]:
    report = _pick(source, _ROOT_FIELDS)
    for optional_layer in ("m3_screening", "model_investigation", "model_review"):
        report.pop(optional_layer, None)
    report.pop("model_discovery", None)
    report["scope"] = _pick(_mapping(source.get("scope")), ("note", "gaps"))
    report["comparability"] = _project_comparability(source.get("comparability"), identity, valid_evidence_ids)
    confirmed = _mapping(source.get("confirmed"))
    report["confirmed"] = {
        "metrics": [_project_metric(item, valid_evidence_ids) for item in _list_maps(confirmed.get("metrics"))],
        "analyses": [_project_calculation(item) for item in _list_maps(confirmed.get("analyses"))],
    }
    report["verified_claims"] = [_project_claim(item, valid_evidence_ids) for item in _list_maps(source.get("verified_claims"))]
    report["interpretations"] = [_project_claim(item, valid_evidence_ids) for item in _list_maps(source.get("interpretations"))]
    pending = _mapping(source.get("pending_review"))
    report["pending_review"] = {
        "facts": [_project_metric(item, valid_evidence_ids) for item in _list_maps(pending.get("facts"))],
        "calculations": [_project_calculation(item) for item in _list_maps(pending.get("calculations"))],
        "claims": [_project_claim(item, valid_evidence_ids) for item in _list_maps(pending.get("claims"))],
        "candidate_signals": [_pick(item, _CANDIDATE_FIELDS) for item in _list_maps(pending.get("candidate_signals"))],
        "uncited_evidences": [
            {**_pick(item, ("evidence_id", "source_sha256")), "status": "uncited"}
            for item in _list_maps(pending.get("uncited_evidences"))
            if item.get("source_sha256") == identity["source_sha256"]
        ],
    }
    if isinstance(source.get("m3_screening"), Mapping):
        report["m3_screening"] = _project_m3(source["m3_screening"], identity)
    projected_discovery, discovery_finding_ids = _project_discovery(
        source.get("model_discovery"), identity, discovery_evidence
    )
    if projected_discovery is not None:
        report["model_discovery"] = projected_discovery
    review_reference_ids = _collect_review_reference_ids(
        report,
        valid_evidence_ids=valid_evidence_ids,
        discovery_finding_ids=discovery_finding_ids,
        investigation_source=source.get("model_investigation"),
        identity=identity,
    )
    top_level_review = source.get("model_review")
    top_level_review_present = isinstance(top_level_review, Mapping)
    projected_review = (
        _project_review(top_level_review, identity, review_reference_ids)
        if top_level_review_present
        else None
    )
    if isinstance(source.get("model_investigation"), Mapping):
        investigation = _project_investigation(source["model_investigation"], identity, review_reference_ids)
        if top_level_review_present:
            if projected_review is None:
                investigation.pop("final_review", None)
            else:
                investigation["final_review"] = projected_review
        report["model_investigation"] = investigation
    if projected_review is not None:
        report["model_review"] = projected_review
    return report


def _collect_review_reference_ids(
    report: Mapping[str, Any],
    *,
    valid_evidence_ids: set[str],
    discovery_finding_ids: set[str],
    investigation_source: Any,
    identity: Mapping[str, Any],
) -> set[str]:
    """Rebuild review references from records actually retained in the export."""

    allowed = set(valid_evidence_ids) | set(discovery_finding_ids)
    confirmed = _mapping(report.get("confirmed"))
    for item in _list_maps(confirmed.get("metrics")):
        value = item.get("fact_id")
        if _safe_review_reference(value):
            allowed.add(value)
    for item in _list_maps(confirmed.get("analyses")):
        value = item.get("calculation_id")
        if _safe_review_reference(value):
            allowed.add(value)
    pending = _mapping(report.get("pending_review"))
    for section, id_field, prefix in (
        ("facts", "fact_id", "pending_fact:"),
        ("calculations", "calculation_id", "pending_calculation:"),
        ("claims", "claim_id", "pending_claim:"),
    ):
        for item in _list_maps(pending.get(section)):
            value = item.get(id_field)
            if _safe_review_reference(value):
                allowed.add(prefix + value)
    for item in _list_maps(pending.get("candidate_signals")):
        value = item.get("signal_id")
        if _safe_review_reference(value):
            allowed.add(value)
    if isinstance(investigation_source, Mapping) and _layer_binding(investigation_source, identity) == "matched":
        for item in _list_maps(investigation_source.get("items")):
            value = item.get("signal_id")
            if _safe_review_reference(value):
                allowed.add("investigation:" + value)
    return allowed


def _safe_review_reference(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is not None


def _project_comparability(value: Any, identity: Mapping[str, Any], evidence_ids: set[str]) -> Any:
    if not isinstance(value, Mapping):
        return None
    fields = {"document_id": "document_id", "source_sha256": "source_sha256", "report_year": "report_year"}
    required = set(fields)
    has_mismatch = any(key in value and value[key] != identity[field] for key, field in fields.items())
    matches = all(value.get(key) == identity[field] for key, field in fields.items())
    binding_status = "mismatch" if has_mismatch else "matched" if matches and required.issubset(value) else "unverified"
    if binding_status == "mismatch":
        return _unbound_comparability("source_mismatch", "mismatch")
    if binding_status == "unverified":
        return _unbound_comparability("unverified", "unverified")
    allowed = ("verification_id", "document_id", "source_sha256", "report_year", "current_year", "comparative_year", "status", "restatement_status", "checks", "conflicts", "limitations")
    result = _pick(value, allowed)
    result["source_binding_status"] = "matched"
    result["evidence"] = []
    for item in _list_maps(value.get("evidence")):
        if item.get("document_id") != identity["document_id"] or item.get("source_sha256") != identity["source_sha256"] or type(item.get("pdf_page")) is not int:
            continue
        if any(item.get(key) != expected for key, expected in (("report_year", identity["report_year"]), ("current_year", identity["report_year"]), ("comparative_year", identity["report_year"] - 1)) if key in item):
            continue
        evidence = _pick(item, ("evidence_id", "topic", "document_id", "source_sha256", "report_year", "current_year", "comparative_year", "pdf_page", "text", "report_year_page", "report_year_text"))
        for key in ("bbox", "report_year_bbox"):
            if isinstance(item.get(key), Mapping):
                evidence[key] = _pick(item[key], _BOX_FIELDS)
        result["evidence"].append(evidence)
    return result


def _unbound_comparability(status: str, binding_status: str) -> dict[str, Any]:
    return {
        "status": status,
        "source_binding_status": binding_status,
        "restatement_status": "unknown",
        "current_year": None,
        "comparative_year": None,
        "verification_id": None,
        "evidence": [],
        "checks": [],
        "conflicts": [],
        "limitations": ["可比性记录未能绑定到当前归档来源，未采纳其结论或证据。"],
    }


def _project_metric(value: Mapping[str, Any], evidence_ids: set[str]) -> dict[str, Any]:
    result = _pick(value, _METRIC_FIELDS)
    result["evidences"] = [_project_report_evidence(item) for item in _list_maps(value.get("evidences")) if item.get("evidence_id") in evidence_ids]
    result["verification_evidences"] = [_project_report_evidence(item) for item in _list_maps(value.get("verification_evidences")) if item.get("evidence_id") in evidence_ids]
    result["verifications"] = [_pick(item, _VERIFICATION_FIELDS) for item in _list_maps(value.get("verifications"))]
    for key in ("evidence_ids",):
        if isinstance(value.get(key), list):
            result[key] = [item for item in value[key] if item in evidence_ids]
    return result


def _project_report_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick(value, _EVIDENCE_FIELDS)
    for region_name in ("value_region", "row_region", "column_region", "title_region", "unit_region", "legacy_text_region"):
        region = value.get(region_name)
        if isinstance(region, Mapping):
            projected = _pick(region, _REGION_FIELDS)
            if isinstance(region.get("bbox"), Mapping):
                projected["bbox"] = _pick(region["bbox"], _BOX_FIELDS)
            result[region_name] = projected
    return result


def _project_calculation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick(value, _CALCULATION_FIELDS)
    result["verifications"] = [_pick(item, _VERIFICATION_FIELDS) for item in _list_maps(value.get("verifications"))]
    if isinstance(value.get("independent_verification"), Mapping):
        result["independent_verification"] = _pick(value["independent_verification"], ("calculation_id", "status", "recomputed_value", "reason", "checks"))
    return result


def _project_claim(value: Mapping[str, Any], evidence_ids: set[str]) -> dict[str, Any]:
    result = _pick(value, _CLAIM_FIELDS)
    result["supporting_evidence_ids"] = [item for item in value.get("supporting_evidence_ids", []) if item in evidence_ids] if isinstance(value.get("supporting_evidence_ids"), list) else []
    result["verifications"] = [_pick(item, _VERIFICATION_FIELDS) for item in _list_maps(value.get("verifications"))]
    result["evidences"] = [_project_report_evidence(item) for item in _list_maps(value.get("evidences")) if item.get("evidence_id") in evidence_ids]
    if isinstance(value.get("independent_verification"), Mapping):
        result["independent_verification"] = _pick(value["independent_verification"], ("claim_id", "status", "reason", "expected_text"))
    return result


def _project_m3(value: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    status = _layer_binding(value, identity)
    result: dict[str, Any] = {"status": _pick(value, ("status",)).get("status", "unknown"), "source_binding_status": status}
    if status != "matched":
        if value.get("source_sha256") is not None or value.get("source_document_id") is not None:
            result["status"] = "source_mismatch" if status == "mismatch" else result["status"]
        return result
    for field in ("kind", "run_id", "company_id", "report_year", "source_document_id", "source_sha256", "rule_version", "model_called", "model_call_count", "model_call_attempt_count"):
        if field in value:
            result[field] = _pick(value, (field,)).get(field)
    screening = _mapping(value.get("screening"))
    result["screening"] = _pick(screening, ("status", "rule_version", "total_score", "maximum_score"))
    result["screening"]["rules"] = []
    for item in _list_maps(screening.get("rules")):
        rule = _pick(item, ("rule_id", "rule_version", "formula", "threshold", "points_if_triggered", "points", "status", "triggered", "calculated_value", "input_fact_ids"))
        rule["issues"] = [_pick(issue, ("code", "message")) for issue in _list_maps(item.get("issues"))]
        result["screening"]["rules"].append(rule)
    for key in ("extraction", "explicit_unit_facts"):
        layer = _mapping(value.get(key))
        result[key] = _pick(layer, ("status", "fact_count", "new_fact_count", "expected_new_fact_count", "issues"))
        result[key]["issues"] = [_pick(item, ("metric_id", "code", "message")) for item in _list_maps(layer.get("issues"))]
    verification = _mapping(value.get("verification"))
    result["verification"] = _pick(verification, ("status", "result_count", "errors"))
    result["verification"]["errors"] = [_pick(item, ("fact_id", "error_type", "message")) for item in _list_maps(verification.get("errors"))]
    for key in ("comparability_proof_audit", "parent_profit_semantic_mapping_audit"):
        result[key] = _pick(_mapping(value.get(key)), ("status", "reason", "limitations", "mapping_version"))
    if isinstance(value.get("failure"), Mapping):
        result["failure"] = _pick(value["failure"], ("code", "reason"))
    result["limitations"] = _string_list(value.get("limitations"))
    return result


def _project_investigation(value: Mapping[str, Any], identity: Mapping[str, Any], evidence_ids: set[str]) -> dict[str, Any]:
    status = _layer_binding(value, identity)
    result: dict[str, Any] = {"status": _pick(value, ("status",)).get("status", "unknown"), "source_binding_status": status, "opinion_label": "模型观点，未独立核验"}
    if status != "matched":
        if status == "mismatch":
            result["status"] = "source_mismatch"
        return result
    for key in ("kind", "run_id", "company_id", "report_year", "source_document_id", "source_sha256", "model_called", "model_call_count", "model_call_attempt_count", "investigation_call_count", "investigation_call_attempt_count", "discovery_call_count", "discovery_call_attempt_count", "model_review_call_count", "model_review_call_attempt_count", "supplementary_retrieval_used", "reason", "reason_detail", "fraud_conclusion"):
        if key in value:
            safe_value = _pick(value, (key,))
            if key in safe_value:
                result[key] = safe_value[key]
    for key in (
        "model_call_count", "model_call_attempt_count", "investigation_call_count",
        "investigation_call_attempt_count", "discovery_call_count",
        "discovery_call_attempt_count", "model_review_call_count",
        "model_review_call_attempt_count",
    ):
        if key in result and not _is_nonnegative_int(result[key]):
            result.pop(key)
    result["items"] = []
    for item in _list_maps(value.get("items")):
        record = _pick(item, ("signal_id", "status", "explanation", "reason", "alternative_explanations", "limitations"))
        record["narrative_evidence_ids"] = [evidence_id for evidence_id in item.get("narrative_evidence_ids", []) if evidence_id in evidence_ids] if isinstance(item.get("narrative_evidence_ids"), list) else []
        result["items"].append(record)
    review = value.get("final_review")
    if isinstance(review, Mapping):
        result["final_review"] = _project_review(review, identity, evidence_ids)
    return result


def _project_review(value: Mapping[str, Any], identity: Mapping[str, Any], evidence_ids: set[str]) -> dict[str, Any]:
    source = _mapping(value.get("source_identity"))
    binding_fields = {"run_id": "run_id", "company_id": "company_id", "report_year": "report_year", "source_document_id": "document_id", "source_sha256": "source_sha256"}
    required = set(binding_fields)
    matches = all(source.get(key) == identity[field] for key, field in binding_fields.items())
    has_mismatch = any(key in source and source[key] != identity[field] for key, field in binding_fields.items())
    binding_status = "matched" if matches and required.issubset(source) else "mismatch" if has_mismatch else "unverified"
    result = {"status": _pick(value, ("status",)).get("status", "unknown"), "source_binding_status": binding_status, "opinion_label": "模型最终评审观点，未独立核验"}
    if binding_status != "matched":
        if binding_status == "mismatch":
            result["status"] = "source_mismatch"
        return result
    for key in ("reason", "assessment", "summary", "model_called", "call_count", "call_attempt_count"):
        if key in value:
            safe_value = _pick(value, (key,))
            if key in safe_value:
                result[key] = safe_value[key]
    for key in ("call_count", "call_attempt_count"):
        if key in result and not _is_nonnegative_int(result[key]):
            result.pop(key)
    result["reasons"] = []
    for item in _list_maps(value.get("reasons")):
        result["reasons"].append({**_pick(item, ("text",)), "evidence_ids": [entry for entry in item.get("evidence_ids", []) if entry in evidence_ids] if isinstance(item.get("evidence_ids"), list) else []})
    result["follow_up_items"] = []
    for item in _list_maps(value.get("follow_up_items")):
        result["follow_up_items"].append({**_pick(item, ("object", "action")), "evidence_ids": [entry for entry in item.get("evidence_ids", []) if entry in evidence_ids] if isinstance(item.get("evidence_ids"), list) else []})
    result["limitations"] = _string_list(value.get("limitations"))
    if matches:
        result["source_identity"] = _pick(source, ("run_id", "company_id", "report_year", "source_document_id", "source_sha256"))
    return result


def _project_discovery(
    value: Any,
    identity: Mapping[str, Any],
    evidence_records: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, set[str]]:
    if value is None:
        return None, set()
    if not isinstance(value, Mapping):
        return {"status": "limited", "source_binding_status": "invalid", "findings": []}, set()
    expected = {
        "run_id": identity["run_id"],
        "company_id": identity["company_id"],
        "report_year": identity["report_year"],
        "source_document_id": identity["document_id"],
        "source_sha256": identity["source_sha256"],
    }
    if value.get("kind") != "fintrace_annual_discovery" or any(value.get(key) != item for key, item in expected.items()):
        return {
            "kind": "fintrace_annual_discovery",
            "status": "limited",
            "source_binding_status": "mismatch",
            "opinion_label": "模型发现，未独立语义核验",
            "findings": [],
            "limitations": ["开放式发现来源身份未能绑定到当前报告，已省略动态发现内容。"],
        }, set()
    status = value.get("status")
    called = value.get("model_called")
    call_count = value.get("model_call_count")
    attempt_count = value.get("model_call_attempt_count")
    coverage = value.get("coverage")
    findings = value.get("findings")
    limitations = value.get("limitations")
    if (
        not isinstance(status, str) or status not in {"completed", "partial", "failed", "not_called"}
        or not isinstance(called, bool)
        or not _is_nonnegative_int(call_count)
        or not _is_nonnegative_int(attempt_count)
        or attempt_count < call_count
        or called != (attempt_count > 0)
        or not isinstance(coverage, Mapping)
        or not isinstance(findings, list) or len(findings) > 768
        or not isinstance(limitations, list) or len(limitations) > 1100
        or any(not isinstance(item, str) or len(item) > 400 for item in limitations)
    ):
        return {"status": "limited", "source_binding_status": "matched_but_invalid", "findings": []}, set()

    projected_findings: list[dict[str, Any]] = []
    finding_ids: set[str] = set()
    for finding in findings:
        if not isinstance(finding, Mapping):
            return {"status": "limited", "source_binding_status": "matched_but_invalid", "findings": []}, set()
        finding_id = finding.get("finding_id")
        priority = finding.get("priority")
        title = finding.get("title")
        observation = finding.get("observation")
        why = finding.get("why_it_matters")
        follow_up = finding.get("follow_up")
        evidence_ids = finding.get("evidence_ids")
        citations = finding.get("citations")
        if (
            not isinstance(finding_id, str) or not re.fullmatch(r"disc:[0-9a-f]{12}-[0-9a-f]{20}", finding_id)
            or not finding_id.startswith(f"disc:{str(identity['source_sha256']).lower()[:12]}-")
            or finding_id in finding_ids
            or not isinstance(priority, str) or priority not in {"high", "medium", "low"}
            or not isinstance(title, str) or len(title) > 100
            or not isinstance(observation, str) or len(observation) > 600
            or not isinstance(why, str) or len(why) > 400
            or not isinstance(follow_up, str) or len(follow_up) > 400
            or finding.get("classification") != "unverified_model_finding"
            or finding.get("verification_status") != "unverified"
            or not isinstance(evidence_ids, list) or any(not isinstance(item, str) for item in evidence_ids)
            or not isinstance(citations, list) or not 1 <= len(citations) <= 4
        ):
            return {"status": "limited", "source_binding_status": "matched_but_invalid", "findings": []}, set()
        finding_ids.add(finding_id)
        citation_locations: list[dict[str, Any]] = []
        citation_ids: list[str] = []
        for citation in citations:
            if not isinstance(citation, Mapping):
                return {"status": "limited", "source_binding_status": "matched_but_invalid", "findings": []}, set()
            evidence_id = citation.get("evidence_id")
            evidence = evidence_records.get(evidence_id) if isinstance(evidence_id, str) else None
            if (
                evidence is None
                or not isinstance(citation.get("quote"), str)
                or finding_id not in evidence.get("finding_ids", [])
                or evidence.get("source_document_id") != identity["document_id"]
                or evidence.get("source_sha256") != identity["source_sha256"]
                or type(evidence.get("pdf_page")) is not int
            ):
                return {"status": "limited", "source_binding_status": "matched_but_invalid", "findings": []}, set()
            citation_ids.append(evidence_id)
            citation_locations.append({
                "evidence_id": evidence_id,
                "source_document_id": identity["document_id"],
                "source_sha256": identity["source_sha256"],
                "pdf_page": evidence["pdf_page"],
                "block_index": evidence.get("block_index"),
                "start_char": evidence.get("start_char"),
            })
        if set(citation_ids) != set(evidence_ids) or len(citation_ids) != len(set(citation_ids)):
            return {"status": "limited", "source_binding_status": "matched_but_invalid", "findings": []}, set()
        projected_findings.append({
            "finding_id": finding_id,
            "title": title,
            "observation": observation,
            "why_it_matters": why,
            "follow_up": follow_up,
            "priority": priority,
            "classification": "unverified_model_finding",
            "verification_status": "unverified",
            "evidence_ids": list(evidence_ids),
            "citations": citation_locations,
        })
    projected_coverage = _project_discovery_coverage(coverage)
    if projected_coverage is None:
        return {"status": "limited", "source_binding_status": "matched_but_invalid", "findings": []}, set()
    result: dict[str, Any] = {
        "kind": "fintrace_annual_discovery",
        **expected,
        "status": status,
        "source_binding_status": "matched",
        "opinion_label": "模型发现，未经独立语义核验",
        "prompt_version": "annual_discovery_v1",
        "model_called": called,
        "model_call_count": call_count,
        "model_call_attempt_count": attempt_count,
        "coverage": projected_coverage,
        "findings": projected_findings,
        "limitations": list(limitations),
        "reason": value.get("reason") if isinstance(value.get("reason"), str) else None,
        "fraud_conclusion": None,
    }
    return result, finding_ids


def _project_discovery_coverage(value: Mapping[str, Any]) -> dict[str, Any] | None:
    fields = (
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches",
        "processed_batches", "successful_batches", "failed_batches", "processed_chars",
        "attempted_chars", "unattempted_batches", "unattempted_chars", "planned_chars",
        "omitted_chars", "unread_pages", "failed_pages", "no_text_pages", "image_pages",
        "unread_image_pages", "truncated", "complete", "source_status", "content_status",
        "time_budget_exhausted", "text_only_scope",
    )
    result = _pick(value, fields)
    if set(result) != set(fields):
        return None
    batch_results = value.get("batch_results")
    if not isinstance(batch_results, list) or len(batch_results) > 128:
        return None
    result["batch_results"] = []
    for item in batch_results:
        if not isinstance(item, Mapping):
            return None
        row = _pick(item, ("batch_index", "status", "reason", "pages", "raw_chars"))
        if set(row) != {"batch_index", "status", "reason", "pages", "raw_chars"}:
            return None
        result["batch_results"].append(row)
    return result


def _layer_binding(value: Mapping[str, Any], identity: Mapping[str, Any]) -> str:
    fields = {"run_id": "run_id", "company_id": "company_id", "report_year": "report_year", "source_document_id": "document_id", "source_sha256": "source_sha256"}
    checks = [(key, field) for key, field in fields.items() if key in value]
    if not checks:
        return "unverified"
    if any(value[key] != identity[field] for key, field in checks):
        return "mismatch"
    if not {key for key, _ in checks} >= {"source_document_id", "source_sha256"}:
        return "unverified"
    return "matched"


def _project_verified_index_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick(value, ("evidence_id", "document_id", "source_sha256", "pdf_page", "printed_page", "table_title", "row_label", "column_label", "value_raw", "value_normalized", "unit", "currency", "period_start", "period_end", "period_type", "limitations"))
    return result


def _project_narrative_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    return _pick(value, ("evidence_id", "source_document_id", "source_sha256", "signal_ids", "section_label", "pdf_page", "raw_text", "selection_reason"))


def _project_discovery_index_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Export only source coordinates and finding links, never quoted source text."""

    result = _pick(
        value,
        (
            "evidence_id", "source_document_id", "source_sha256", "pdf_page",
            "block_index", "start_char", "finding_ids",
        ),
    )
    bbox = value.get("bbox")
    if isinstance(bbox, Mapping):
        coordinates = _pick(bbox, _BOX_FIELDS)
        if set(coordinates) == set(_BOX_FIELDS) and all(
            isinstance(coordinates[key], (int, float)) and not isinstance(coordinates[key], bool)
            and math.isfinite(float(coordinates[key]))
            for key in _BOX_FIELDS
        ):
            result["bbox"] = coordinates
    return result


def _markdown_tree(value: Any) -> Any:
    if isinstance(value, str):
        text = value.replace("\r", " ").replace("\n", " ").replace("`", "ˋ")
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for char in ("[", "]", "(", ")", "!", "|", "\\"):
            text = text.replace(char, "\\" + char)
        return text
    if isinstance(value, Mapping):
        return {key: _markdown_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_markdown_tree(item) for item in value]
    return value


def _render_source_nature(provenance: Mapping[str, Any]) -> list[str]:
    lines = ["<h2>来源性质与使用条件</h2>", f"<p><strong>{_h(provenance.get('label', '来源性质未确认'))}</strong></p>"]
    if provenance.get("use_conditions"):
        lines.append(f"<p>{_h(provenance['use_conditions'])}</p>")
    if provenance.get("company_name"):
        lines.append(f"<p>登记样例主体：{_h(provenance['company_name'])}</p>")
    return lines


def _render_comparability(value: Any) -> list[str]:
    lines = ["<h2>年度可比性核验</h2>"]
    if not isinstance(value, Mapping):
        return [*lines, "<p>未提供可比性核验记录。</p>"]
    rows = [(key, value.get(key)) for key in ("status", "source_binding_status", "restatement_status", "current_year", "comparative_year", "verification_id")]
    lines.append(_html_table(("字段", "记录"), rows))
    for label in ("checks", "conflicts", "limitations"):
        lines.extend(_render_text_list(value.get(label, []), label))
    evidence_rows = [(e.get("pdf_page"), e.get("evidence_id"), e.get("topic"), e.get("text")) for e in value.get("evidence", [])]
    lines.append(_html_table(("PDF 页码", "证据 ID", "主题", "核对摘录"), evidence_rows))
    return lines


def _render_claims(title: str, claims: list[Mapping[str, Any]]) -> list[str]:
    lines = [f"<h2>{_h(title)}</h2>"]
    if not claims:
        return [*lines, "<p>没有记录。</p>"]
    for claim in claims:
        citations = ", ".join(claim.get("supporting_evidence_ids", [])) or "无已核验引用"
        lines.extend(["<article class=\"entry no-break\">", f"<p><strong>{_h(claim.get('placement', '未提供'))} · {_h(claim.get('verification_status', '未提供'))}</strong></p>", f"<p>{_h(claim.get('text', ''))}</p>", f"<p>证据 ID：{_h(citations)}</p>"])
        lines.extend(_render_text_list(claim.get("limitations", []), "限制"))
        lines.append("</article>")
    return lines


def _render_candidates(items: list[Mapping[str, Any]]) -> list[str]:
    lines = ["<h2>待核查候选</h2>"]
    if not items:
        return [*lines, "<p>没有候选线索。</p>"]
    rows = [(item.get("signal_id"), item.get("status"), item.get("title"), item.get("reason"), item.get("fraud_conclusion")) for item in items]
    return [*lines, "<p>候选或未验证线索只用于进一步核查，不是确认舞弊。</p>", _html_table(("候选 ID", "状态", "标题", "筛查原因", "舞弊结论字段"), rows)]


def _render_m3(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    screening = _mapping(value.get("screening"))
    lines = ["<h2>M3 四规则试行筛查</h2>", "<p><strong>M3 分数是未校准试行分数，不是概率。</strong>本层状态独立保留。</p>"]
    lines.append(_html_table(("M3 状态", "来源绑定", "筛查状态", "试行分数", "满分"), [(value.get("status"), value.get("source_binding_status"), screening.get("status"), screening.get("total_score"), screening.get("maximum_score"))]))
    rules = screening.get("rules", [])
    lines.append(_html_table(("规则", "状态", "触发", "分数", "计算值", "阈值", "公式"), [(r.get("rule_id"), r.get("status"), r.get("triggered"), r.get("points"), r.get("calculated_value"), r.get("threshold"), r.get("formula")) for r in rules]))
    return lines


def _render_model_layer(investigation: Any, review: Any) -> list[str]:
    if not isinstance(investigation, Mapping) and not isinstance(review, Mapping):
        return []
    lines = ["<h2>AI 调查与最终评审</h2>", "<p><strong>模型解释和最终评审均属观点，未独立核验；引用原文不证明因果。</strong></p>"]
    if isinstance(investigation, Mapping):
        has_review_accounting = (
            isinstance(review, Mapping)
            or isinstance(investigation.get("final_review"), Mapping)
            or any(field in investigation for field in ("model_review_call_count", "model_review_call_attempt_count"))
        )
        total_call_label = "总调用次数（含最终评审）" if has_review_accounting else "调查调用次数（调查层归档值）"
        total_attempt_label = "总调用尝试次数（含最终评审）" if has_review_accounting else "调查调用尝试次数（调查层归档值）"
        lines.append(_html_table(("调查状态", "来源绑定", "模型已调用", total_call_label, "状态原因"), [(investigation.get("status"), investigation.get("source_binding_status"), investigation.get("model_called"), investigation.get("model_call_count"), investigation.get("reason"))]))
        call_rows = []
        for label, field in (
            ("调查调用次数", "investigation_call_count"),
            ("调查调用尝试次数", "investigation_call_attempt_count"),
            ("最终评审调用次数", "model_review_call_count"),
            ("最终评审调用尝试次数", "model_review_call_attempt_count"),
        ):
            if field in investigation:
                call_rows.append((label, investigation[field]))
        if "model_call_attempt_count" in investigation:
            call_rows.insert(0, (total_attempt_label, investigation["model_call_attempt_count"]))
        if call_rows:
            lines.append("<h3>模型调用拆分</h3>")
            lines.append(_html_table(("调用统计", "次数"), call_rows))
        for item in investigation.get("items", []):
            lines.append(f"<article class=\"entry no-break\"><p><strong>{_h(item.get('signal_id'))} · {_h(item.get('status'))}</strong></p><p>{_h(item.get('explanation') or item.get('reason'))}</p><p>叙述证据 ID：{_h(', '.join(item.get('narrative_evidence_ids', [])))}</p></article>")
        if not isinstance(review, Mapping) and isinstance(investigation.get("final_review"), Mapping):
            review = investigation["final_review"]
    if isinstance(review, Mapping):
        lines.append("<h3>最终评审（模型观点）</h3>")
        lines.append(_html_table(("状态", "评审意见", "来源绑定", "模型调用"), [(review.get("status"), review.get("assessment"), review.get("source_binding_status"), review.get("model_called"))]))
        if review.get("summary"):
            lines.append(f"<p>{_h(review['summary'])}</p>")
        for item in review.get("reasons", []):
            lines.append(f"<p>理由：{_h(item.get('text'))}（证据 ID：{_h(', '.join(item.get('evidence_ids', [])))}）</p>")
        for item in review.get("follow_up_items", []):
            lines.append(f"<p>后续核查：{_h(item.get('object'))}；{_h(item.get('action'))}（证据 ID：{_h(', '.join(item.get('evidence_ids', [])))}）</p>")
        lines.extend(_render_text_list(review.get("limitations", []), "最终评审限制"))
    return lines


def _render_discovery(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    coverage = _mapping(value.get("coverage"))
    lines = [
        "<h2>年报全文开放式发现</h2>",
        "<p><strong>模型提出的公司线索未经独立语义核验，不参与基础事实/计算核验。引文定位不证明因果。</strong></p>",
    ]
    lines.append(_html_table(
        ("状态", "来源绑定", "模型调用", "覆盖状态", "文字页 / 总页", "已处理 / 计划批次", "成功 / 失败批次", "已处理 / 总文字字符", "截断"),
        [(
            value.get("status"), value.get("source_binding_status"), value.get("model_called"),
            coverage.get("complete"), f"{coverage.get('text_pages')} / {coverage.get('total_pages')}",
            f"{coverage.get('processed_batches')} / {coverage.get('planned_batches')}",
            f"{coverage.get('successful_batches')} / {coverage.get('failed_batches')}",
            f"{coverage.get('processed_chars')} / {coverage.get('total_chars')}", coverage.get("truncated"),
        )],
    ))
    rows = []
    for finding in value.get("findings", []) if isinstance(value.get("findings"), list) else []:
        if not isinstance(finding, Mapping):
            continue
        locations = finding.get("citations") if isinstance(finding.get("citations"), list) else []
        page_refs = "; ".join(
            f"PDF 第{item.get('pdf_page')}页 / {item.get('evidence_id')}"
            for item in locations if isinstance(item, Mapping)
        )
        rows.append((
            finding.get("priority"), finding.get("title"), finding.get("observation"),
            finding.get("why_it_matters"), finding.get("follow_up"), page_refs,
            finding.get("verification_status"),
        ))
    lines.append(_html_table(("优先级", "动态标题", "观察", "关注原因", "后续核查", "PDF 页码 / evidence ID", "核验状态"), rows))
    lines.extend(_render_text_list(value.get("limitations"), "开放式发现限制"))
    unread = coverage.get("unread_pages")
    if isinstance(unread, list) and unread:
        lines.extend(_render_text_list(["、".join(str(page) for page in unread)], "未完整处理页码"))
    return lines


def _render_discovery_markdown(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    coverage = _mapping(value.get("coverage"))
    lines = [
        "## 年报全文开放式发现",
        "",
        "模型提出的公司线索未经独立语义核验，不参与基础事实、计算和主张核验；引用定位不证明因果。",
        "",
        f"- 状态：`{value.get('status')}`；来源绑定：`{value.get('source_binding_status')}`；模型调用：`{value.get('model_called')}`",
        f"- 覆盖：文字页 {coverage.get('text_pages')} / 总页 {coverage.get('total_pages')}；",
        f"原始文字块 {coverage.get('total_blocks')}；已处理字符 {coverage.get('processed_chars')} / 全部可提取字符 {coverage.get('total_chars')}；",
        f"已结束批次 {coverage.get('processed_batches')} / 计划 {coverage.get('planned_batches')}（成功 {coverage.get('successful_batches')}、失败 {coverage.get('failed_batches')}）；",
        f"完整={coverage.get('complete')}，截断={coverage.get('truncated')}。",
    ]
    unread = coverage.get("unread_pages")
    if isinstance(unread, list) and unread:
        lines.append("- 未完整处理页码：" + "、".join(str(page) for page in unread))
    findings = value.get("findings")
    if not isinstance(findings, list) or not findings:
        lines.extend(["", "本次没有通过引用校验的开放式发现。"])
    else:
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            lines.extend([
                "", f"### {finding.get('title', '未命名发现')}（{finding.get('priority', '未提供')}，未核实）", "",
                f"**观察：**{finding.get('observation', '')}",
                f"- 关注原因：{finding.get('why_it_matters', '')}",
                f"- 后续核查：{finding.get('follow_up', '')}",
            ])
            refs = finding.get("citations") if isinstance(finding.get("citations"), list) else []
            citations = [
                f"PDF 第{item.get('pdf_page')}页 / {item.get('evidence_id')}"
                for item in refs if isinstance(item, Mapping)
            ]
            lines.append("- 原文定位：" + ("；".join(citations) if citations else "无有效引用定位"))
    limitations = value.get("limitations")
    if isinstance(limitations, list):
        lines.extend("- 开放式发现限制：" + str(item) for item in limitations)
    return "\n".join(lines)


def _render_review_markdown(value: Mapping[str, Any]) -> str:
    lines = [
        "## 最终评审（模型观点）",
        "",
        "该评审未经独立核验。",
        "",
        f"- 状态：`{value.get('status')}`；来源绑定：`{value.get('source_binding_status')}`",
    ]
    if value.get("summary"):
        lines.append(str(value["summary"]))
    for item in value.get("reasons", []) if isinstance(value.get("reasons"), list) else []:
        if isinstance(item, Mapping):
            refs = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
            lines.append(f"- 理由：{item.get('text', '')}（引用：{', '.join(str(ref) for ref in refs)}）")
    for item in value.get("follow_up_items", []) if isinstance(value.get("follow_up_items"), list) else []:
        if isinstance(item, Mapping):
            refs = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
            lines.append(f"- 后续核查：{item.get('object', '')}；{item.get('action', '')}（引用：{', '.join(str(ref) for ref in refs)}）")
    lines.extend(f"- 评审限制：{item}" for item in value.get("limitations", []) if isinstance(item, str))
    return "\n".join(lines)


def _render_text_list(values: Any, label: str) -> list[str]:
    items = values if isinstance(values, list) else []
    if not items:
        return []
    return [f"<h3>{_h(label)}</h3><ul>" + "".join(f"<li>{_h(value)}</li>" for value in items) + "</ul>"]


def _citation_text(*groups: Any) -> str:
    citations: dict[str, int] = {}
    for group in groups:
        for item in group if isinstance(group, list) else []:
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str) and type(item.get("pdf_page")) is int:
                citations[item["evidence_id"]] = item["pdf_page"]
    return "; ".join(f"PDF 第{page}页 / {evidence_id}" for evidence_id, page in citations.items()) or "无已核验引用"


def _html_table(headers: tuple[str, ...], rows: Any) -> str:
    head = "<thead><tr>" + "".join(f"<th scope=\"col\">{_h(value)}</th>" for value in headers) + "</tr></thead>"
    body = []
    for row in rows:
        values = tuple(row)
        body.append("<tr>" + "".join(f"<td>{_h(_display(value))}</td>" for value in values) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan=\"{len(headers)}\">无记录</td></tr>")
    return "<div class=\"table-wrap\"><table>" + head + "<tbody>" + "".join(body) + "</tbody></table></div>"


def _display(value: Any) -> str:
    if value is None:
        return "未提供"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _h(value: Any) -> str:
    return html.escape(_display(value), quote=True)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_maps(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _pick(source: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        if field not in source:
            continue
        value = source[field]
        if value is None or isinstance(value, (str, bool, int)):
            result[field] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[field] = value
        elif isinstance(value, list) and all(
            item is None
            or isinstance(item, (str, bool, int))
            or isinstance(item, float) and math.isfinite(item)
            for item in value
        ):
            result[field] = value
    return result


def _is_nonnegative_int(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 9_007_199_254_740_991


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _safe_filename_id(run_id: str) -> str:
    if not _ASCII_RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise AnnualReportExportError(400, "invalid_run_id", "run_id 格式无效。")
    return run_id
