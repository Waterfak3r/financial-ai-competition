"""季度分析审计包的 Markdown 渲染。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def render_quarterly_markdown(archive: dict[str, Any]) -> str:
    dataset = archive.get("dataset", {})
    archive_link = f"../../runs/{archive.get('run_id', '')}/analysis.json"
    lines = [
        f"# 季度同比/环比分析 · {archive.get('company_id', 'unknown')}",
        "",
        f"- 运行标识：`{archive.get('run_id', '')}`",
        f"- 目标期间：{archive.get('target_year')} {archive.get('target_quarter')}",
        f"- 运行状态：`{archive.get('status', '')}`",
        f"- 数据性质：{dataset.get('nature', '未声明')}",
        f"- 使用条件：{dataset.get('use_conditions', '未声明')}",
        "",
        "> 本报告展示财务期间比较，不构成舞弊确认。未通过原文、计算或可比性核验的结果均弃权。",
        "",
        "## 来源材料与原始累计 observation",
        "",
        "| 文档 | 报告期间 | SHA256 | Nature | 可用观察 |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for document in archive.get("source_documents", []):
        verified = sum(
            1 for observation in archive.get("source_observations", [])
            if observation.get("source_document_id") == document.get("document_id")
            and observation.get("verification", {}).get("status") == "verified"
        )
        lines.append(
            f"| `{document.get('document_id')}` | {document.get('report_year')} {document.get('report_period')} "
            f"| `{document.get('source_sha256')}` | {document.get('nature')} | {verified} |"
        )
    lines.extend(("", f"原始累计值保留报告列角色、报表类型、合并/母公司口径、原始金额、币种、单位、倍率和 PDF 行列坐标。每个 observation 的定位核验结果及当前进程 proof 见 [结构化运行记录]({archive_link})。", ""))

    lines.extend(("## 派生季度", "", "| 指标 | 期间 | 单季金额 | 口径 | 核验状态 | 公式 |", "| --- | --- | ---: | --- | --- | --- |"))
    for item in archive.get("derived_quarters", []):
        lines.append(
            f"| {item.get('metric_id')} | {item.get('year')} {item.get('quarter')} "
            f"| {item.get('normalized_value')} {item.get('unit')} ({item.get('currency')}) "
            f"| {item.get('scope')} | `{item.get('verification', {}).get('status')}` "
            f"| `{item.get('formula_expression')}` |"
        )
    lines.append("")

    lines.extend(("## 同比与环比", "", "| 指标 | 关系 | 当前值 | 比较值 | 差额 | 增率 | 结果状态 | 可比性/独立核验 |", "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |"))
    comparison_notes: list[str] = []
    for item in archive.get("comparisons", []):
        lines.append(
            f"| {item.get('metric_id')} | {item.get('relation')} | {item.get('current_value')} | {item.get('prior_value')} "
            f"| {item.get('difference') if item.get('difference') is not None else '—'} "
            f"| {_format_growth_rate(item.get('growth_rate'))} "
            f"| `{item.get('status')}` | `{item.get('comparability', {}).get('status')}` / `{item.get('verification', {}).get('status')}` |"
        )
        notes = dict.fromkeys(
            tuple(item.get("limitations", ())) + tuple(item.get("comparability", {}).get("limitations", ()))
        )
        if notes:
            comparison_notes.append(f"- `{item.get('metric_id')} · {item.get('relation')}`：" + "；".join(notes))
    lines.extend(("", "增长率以百分数显示，按差额/比较基数计算。差额按已核验的单季值独立重算。比较基数非正时保留有依据的差额并放弃增长率；期间可比性缺少证据时差额与增率一并弃权。", ""))
    if comparison_notes:
        lines.extend(("比较可比性限制：", "", *comparison_notes, ""))

    limitations = list(archive.get("limitations", []))
    if limitations:
        lines.extend(("## 限制与弃权", ""))
        lines.extend(f"- {item}" for item in limitations)
        lines.append("")
    else:
        lines.extend(("## 限制与弃权", "", "本次链路未发现额外材料不足；具体口径与适用范围仍受来源清单和样例声明约束。", ""))

    lines.extend(("## 复现与溯源", "", f"- 完整结构化记录：[analysis.json]({archive_link})，位于 artifacts/runs/{archive.get('run_id', '')}/。", "- 输入清单：仓库内相对路径，不包含开发机绝对路径。", "- 文件 SHA256、解析/核验器代码指纹、源累计/派生/比较核验结果、proof 和 limitation 分层保存在运行归档。", "- Proof 仅对生成它的当前 Python 进程内同一目标内容有效；归档 proof JSON 只用于审计，不可恢复授权。", ""))
    return "\n".join(lines)


def _format_growth_rate(value: str | None) -> str:
    if value is None:
        return "弃权"
    percentage = format(Decimal(value) * Decimal(100), "f")
    if "." in percentage:
        percentage = percentage.rstrip("0").rstrip(".")
    return f"{percentage}%"
