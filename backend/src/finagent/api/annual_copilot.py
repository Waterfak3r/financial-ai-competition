"""基于已独立核验的年度报告归档提供定向问答与交互式审计探查。"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from finagent.api.annual_report_read import AnnualReportArchive, AnnualReportReadError, load_annual_report
from finagent.core.model_settings_store import ModelConfigError, ModelSettingsStoreError, resolve_model_settings
from finagent.llm.chat_completion import ChatMessage, ModelResponseError, complete_chat

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_DEFAULT_SUGGESTIONS = (
    "深入分析经营现金流与净利润背离的具体成因",
    "本期非经常性损益主要由哪些项目构成？具有可持续性吗？",
    "针对当前指标变化，建议审计人员下一步重点调取哪些科目附注与凭证？",
)


class AnnualCopilotError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def build_copilot_context(archive: AnnualReportArchive) -> str:
    report = archive.report
    company_id = report.get("company_id") or "未知公司"
    year = report.get("report_year") or "2024"

    metrics = report.get("confirmed", {}).get("metrics", [])
    analyses = report.get("confirmed", {}).get("analyses", [])
    candidates = report.get("pending_review", {}).get("candidate_signals", [])
    review = report.get("model_review") or {}
    comparability = report.get("comparability") or {}

    lines = [
        f"【标的公司与期间】公司代码: {company_id}, 报告年度: {year} 年",
        f"【可比性状态】: {comparability.get('status', '未知')}",
        "\n【已独立核验的核心财务数据】:",
    ]
    for m in metrics:
        norm_val = m.get("normalized_value")
        val_str = f"{float(norm_val)/1e8:.2f} 亿元" if norm_val and norm_val.replace(".", "", 1).isdigit() else str(norm_val)
        lines.append(
            f"- {m.get('label_raw', m.get('metric_id'))} ({m.get('report_year')}年): "
            f"{val_str} (原值: {m.get('raw_value')}, 报表: {m.get('statement_type')}, 口径: {m.get('scope')})"
        )

    lines.append("\n【同比与变动差额测算（已验证）】:")
    for a in analyses:
        lines.append(f"- 指标/公式: {a.get('formula_id')}, 测算结果: {a.get('output_value')} ({a.get('unit', '')}), 状态: {a.get('status')}")

    lines.append("\n【财务异常与背离筛查信号】:")
    for c in candidates:
        lines.append(f"- 信号ID: {c.get('signal_id')}, 状态: {c.get('status')}, 说明: {c.get('reason') or '未特别说明'}")

    if review and isinstance(review, Mapping):
        lines.append("\n【AI 综合评审意见】:")
        lines.append(f"- 结论倾向: {review.get('assessment', '未提供')}")
        lines.append(f"- 执行摘要: {review.get('summary', '未提供')}")
        reasons = review.get("reasons") or []
        if reasons:
            lines.append("- 关键依据: " + "；".join(str(r.get("text", "")) for r in reasons if isinstance(r, Mapping)))
        follow_ups = review.get("follow_up_items") or []
        if follow_ups:
            lines.append("- 查账建议: " + "；".join(str(f.get("action", "")) for f in follow_ups if isinstance(f, Mapping)))

    limitations = report.get("limitations") or []
    if limitations:
        lines.append("\n【分析范围与局限性】:")
        for lim in limitations:
            lines.append(f"- {lim}")

    return "\n".join(lines)


def answer_copilot_question(
    project_root: Path,
    run_id: str,
    question: str,
    history: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    trimmed_id = run_id.strip()
    if not _RUN_ID_PATTERN.match(trimmed_id):
        raise AnnualCopilotError(400, "invalid_run_id", "run_id 格式无效。")

    clean_question = question.strip()
    if not clean_question:
        raise AnnualCopilotError(400, "empty_question", "提问内容不能为空。")
    if len(clean_question) > 500:
        raise AnnualCopilotError(400, "question_too_long", "提问内容过长，请控制在 500 字以内。")

    try:
        archive = load_annual_report(project_root, trimmed_id)
    except AnnualReportReadError as exc:
        raise AnnualCopilotError(exc.status_code, exc.code, exc.message) from None
    try:
        settings = resolve_model_settings(project_root)
    except (ModelConfigError, ModelSettingsStoreError):
        raise AnnualCopilotError(400, "model_not_configured", "未检测到已配置的模型服务，请先在左侧‘设置’页面填写服务地址与 API 密钥。") from None

    context_text = build_copilot_context(archive)
    system_instruction = (
        "你是由 FINTRACE 驱动的资深法务会计与财务审计专家助手。\n"
        "你的职责是基于当前年报已独立核验的财务数据、变动测算与评审意见，回答分析师或审计人员的具体业务疑问。\n\n"
        "【严守规则】\n"
        "1. 必须严格基于下方给出的已核实事实、变动差额与评审结论作答，绝不捏造未核实的数据或结论。\n"
        "2. 使用严谨、客观、专业的财会审计行话（如权责发生制、营运资金变动、存货减值、信用减值、销售返利计提等）。\n"
        "3. 若某项数据属于弃权或未提供，如实说明审计边界，绝不擅自脑补。\n"
        "4. 回答必须极其简明精练（控制在 350 字以内），条理分明，采用结构化项目符号展开；重点给出具体的科目附注调取与凭证穿透指引。"
    )

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=f"{system_instruction}\n\n{context_text}"),
    ]

    # Include recent history (up to last 4 turns)
    if history:
        for turn in history[-4:]:
            role = turn.get("role")
            content = turn.get("content", "").strip()
            if role in {"user", "assistant"} and content:
                messages.append(ChatMessage(role=role, content=content[:800]))

    messages.append(ChatMessage(role="user", content=clean_question))

    started = time.perf_counter()
    try:
        completion = complete_chat(messages, settings, timeout_seconds=60.0)
    except ModelResponseError as exc:
        raise AnnualCopilotError(502, "model_call_failed", f"模型服务调用未完成：{exc}") from None
    except Exception as exc:
        raise AnnualCopilotError(500, "copilot_internal_error", f"问答生成异常：{exc}") from None

    duration = round(time.perf_counter() - started, 2)

    return {
        "run_id": trimmed_id,
        "company_id": archive.report.get("company_id"),
        "report_year": archive.report.get("report_year"),
        "question": clean_question,
        "answer": completion.text.strip(),
        "model": settings.model,
        "duration_seconds": duration,
        "suggested_prompts": list(_DEFAULT_SUGGESTIONS),
    }
