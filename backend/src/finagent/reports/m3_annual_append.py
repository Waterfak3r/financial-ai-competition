"""把可选 M3 年度规则结果附加到 v2 报告。"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Mapping


def attach_m3_screening_to_report(
    report: Mapping[str, Any], m3_result: Mapping[str, Any]
) -> dict[str, Any]:
    """保留 M2 报告字段，只新增独立的 M3 可选结果区。"""

    if report.get("kind") != "fintrace_annual_analysis_report":
        raise ValueError("M3 结果只能附加到 FINTRACE 年度分析报告。")
    result = deepcopy(dict(report))
    result["m3_screening"] = deepcopy(dict(m3_result))
    if m3_result.get("status") != "completed":
        limitations = list(result.get("limitations", []))
        note = f"M3 四规则筛查状态为 {m3_result.get('status', 'unknown')}；结果或部分规则弃权，详见 m3_screening。"
        if note not in limitations:
            limitations.append(note)
        result["limitations"] = limitations
    return result


def render_report_with_m3_screening(
    markdown: str, m3_result: Mapping[str, Any]
) -> str:
    """在现有 Markdown 报告后追加与 JSON 结果一致的 M3 规则表。"""

    screening = m3_result.get("screening")
    if not isinstance(screening, Mapping):
        raise ValueError("M3 归档对象缺少 screening 结果。")
    total_score = screening.get("total_score")
    score_text = "弃权（总分未知）" if total_score is None else f"{total_score}/{screening.get('maximum_score', 100)}"
    lines = [
        "## M3 四条年度规则筛查",
        "",
        f"- M3 状态：`{m3_result.get('status', 'unknown')}`",
        f"- 筛查状态：`{screening.get('status', 'unknown')}`",
        f"- 规则版本：`{screening.get('rule_version', m3_result.get('rule_version', 'unknown'))}`",
        f"- 汇总分数：`{score_text}`",
        f"- 同源文档：`{m3_result.get('source_document_id', 'unknown')}`",
        f"- 来源 SHA256：`{m3_result.get('source_sha256', 'unknown')}`",
        f"- 新提取事实：{m3_result.get('extraction', {}).get('new_fact_count', 0)}/"
        f"{m3_result.get('extraction', {}).get('expected_new_fact_count', 10)} 条",
        f"- 显式单位复核事实：{m3_result.get('explicit_unit_facts', {}).get('fact_count', 0)} 条",
        "- 这些规则只用于提示进一步核查，风险线索不是确认舞弊。",
        "- 计算值仅作展示并最多保留 6 位小数；筛查判定使用未舍入值，JSON 保留计算原值。",
        "",
        "| 规则 | 状态 | 触发 | 分数 | 计算值 | 阈值 |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    rules = screening.get("rules", [])
    if isinstance(rules, list) and rules:
        for rule in rules:
            if not isinstance(rule, Mapping):
                continue
            triggered = rule.get("triggered")
            trigger_text = "是" if triggered is True else "否" if triggered is False else "未知"
            points = rule.get("points")
            points_text = "弃权" if points is None else str(points)
            calculated = rule.get("calculated_value")
            threshold = rule.get("threshold")
            lines.append(
                f"| `{rule.get('rule_id', 'unknown')}` | `{rule.get('status', 'unknown')}` | "
                f"{trigger_text} | {points_text} | `{_format_calculated_value(calculated)}` | "
                f"`{threshold if threshold is not None else '无'}` |"
            )
    else:
        lines.append("| 四规则结果 | `abstained` | 未形成 | 弃权 | 未知 | 未知 |")

    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, Mapping):
            continue
        lines.extend(
            [
                "",
                f"### `{rule.get('rule_id', 'unknown')}`",
                "",
                f"- 公式：{rule.get('formula', '未提供')}",
                f"- 输入事实：{', '.join(f'`{item}`' for item in rule.get('input_fact_ids', [])) or '无'}",
            ]
        )
        issues = rule.get("issues", [])
        if issues:
            lines.append("- 弃权或核查原因：")
            for issue in issues:
                if isinstance(issue, Mapping):
                    lines.append(f"  - `{issue.get('code', 'unknown')}`：{issue.get('message', '未提供')}")
        elif rule.get("status") == "calculable":
            lines.append("- 规则计算完成；未触发表示该条试行筛查条件不成立。")

    extraction_issues = m3_result.get("extraction", {}).get("issues", [])
    explicit_issues = m3_result.get("explicit_unit_facts", {}).get("issues", [])
    verification_errors = m3_result.get("verification", {}).get("errors", [])
    proof = m3_result.get("comparability_proof_audit", {})
    mapping = m3_result.get("parent_profit_semantic_mapping_audit", {})
    lines.extend(
        [
            "",
            f"- 年度可比性 proof：`{proof.get('status', 'unknown')}`（审计副本不可用于新进程授权）",
            f"- 规则二语义 proof：`{mapping.get('status', 'unknown')}`（仅支持已说明的海天样例版式）",
        ]
    )
    if extraction_issues or explicit_issues or verification_errors:
        lines.extend(["", "### 字段与独立核验问题", ""])
        for issue in (*extraction_issues, *explicit_issues):
            if isinstance(issue, Mapping):
                lines.append(
                    f"- `{issue.get('metric_id', 'unknown')}` / `{issue.get('code', 'unknown')}`："
                    f"{issue.get('message', '未提供')}"
                )
        for issue in verification_errors:
            if isinstance(issue, Mapping):
                lines.append(
                    f"- `{issue.get('fact_id', 'unknown')}` 核验异常 `{issue.get('error_type', 'unknown')}`："
                    f"{issue.get('message', '未提供')}"
                )
    failure = m3_result.get("failure")
    if isinstance(failure, Mapping):
        lines.extend(["", f"- M3 失败原因：{failure.get('reason', '未提供')}"])
    lines.extend(["", "阈值为试行值，尚待隔离评测验证；本节不构成舞弊结论。", ""])
    return markdown.rstrip() + "\n\n" + "\n".join(lines)


def _format_calculated_value(value: Any) -> str:
    if value is None:
        return "未知"
    text = str(value)
    parts = text.split(";")
    formatted: list[str] = []
    for part in parts:
        label, separator, raw_value = part.partition("=")
        raw = raw_value.strip() if separator else part.strip()
        try:
            number = Decimal(raw)
        except (InvalidOperation, ValueError):
            formatted.append(part)
            continue
        with localcontext() as context:
            context.prec = max(28, len(number.as_tuple().digits) + 8)
            rounded = number.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        shown = format(rounded, "f").rstrip("0").rstrip(".")
        if "." not in shown:
            shown = shown or "0"
        elif shown.startswith("-."):
            shown = shown.replace("-.", "-0.", 1)
        elif shown.startswith("."):
            shown = "0" + shown
        formatted.append(f"{label}{separator}{shown}" if separator else shown)
    return ";".join(formatted)
