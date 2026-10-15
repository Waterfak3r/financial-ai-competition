"""从原始年报独立核验年度本期数与比较数的追溯调整状态。

该模块只接受带报告年份、来源哈希、页码、边界框和原文的原始 PDF 证据。
返回的 proof 是进程内核验函数签发的对象；序列化副本只供审计，不能授权计算。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from finagent.schemas.text_pdf import PdfBBox

ComparabilityStatus = Literal["verified", "conflict", "insufficient_evidence"]
RestatementStatus = Literal["not_restated", "unknown"]
_TopicOutcome = Literal[
    "no_impact",
    "unconfirmed_applicable",
    "impact",
    "unresolved_comparative_impact",
    "choice_conflict",
    "association_ambiguous",
]
_CheckboxChoice = Literal["not_applicable", "applicable", "yes", "no"]
_ChoiceResult = Literal["not_applicable", "applicable", "yes", "no", "conflict"]
_HeadingSource = Literal["major", "summary"]

_SIGNING_KEY = secrets.token_bytes(32)
_REQUIRED_CAUSES = (
    "准则追溯调整",
    "会计政策变更",
    "重大会计差错更正",
    "同一控制合并范围变更",
    "其他调整",
)
_TOPIC_POLICY = "major_accounting_policy_change"
_TOPIC_ESTIMATE = "major_accounting_estimate_change"
_TOPIC_FIRST_ADOPTION = "first_adoption_opening_balance_adjustment"
_TOPIC_PRIOR_ERROR = "prior_error_retrospective_restatement"
_TOPIC_OPENING = "opening_retained_earnings_adjustments_zero"
_TOPIC_NO_RESTATEMENT = "no_prior_period_restatement_declared"
_REQUIRED_TOPICS = (
    _TOPIC_POLICY,
    _TOPIC_ESTIMATE,
    _TOPIC_FIRST_ADOPTION,
    _TOPIC_PRIOR_ERROR,
)
_TOPIC_LABELS = {
    _TOPIC_POLICY: "重要会计政策变更",
    _TOPIC_ESTIMATE: "重要会计估计变更",
    _TOPIC_FIRST_ADOPTION: "首次执行新准则对年初财务报表的调整披露",
    _TOPIC_PRIOR_ERROR: "前期差错追溯重述法",
    _TOPIC_NO_RESTATEMENT: "是否需追溯调整或重述以前年度会计数据",
}
_TOPIC_ASSOCIATION = {
    _TOPIC_POLICY: ("会计政策变更", "政策变更"),
    _TOPIC_ESTIMATE: ("会计估计变更", "估计变更"),
    _TOPIC_FIRST_ADOPTION: ("首次执行",),
    _TOPIC_PRIOR_ERROR: ("会计差错", "差错更正", "追溯重述"),
    _TOPIC_NO_RESTATEMENT: ("追溯调整或重述", "重述以前年度"),
}
_CHECKED_MARKS = frozenset(
    {
        "√",
        "✓",
        "✔",
        "☑",
        "■",
        "●",
        "\u221a",
        "\u2611",
        "\u25a0",
        "\u25cf",
        "\u2713",
        "\u2714",
        "\uf052",
        "\uf0d2",
        "\uf0fc",
    }
)
_EMPTY_MARKS = frozenset({"□", "☐", "○", "◻", "\u25a1", "\u25cb", "\u2610", "\uf0a8"})
_SUBJECT_TOKEN_RE = (
    r"(?:本期|本次|上述)?(?:"
    + "|".join(
        re.escape(cue)
        for cue in sorted(
            {item for cues in _TOPIC_ASSOCIATION.values() for item in cues},
            key=len,
            reverse=True,
        )
    )
    + ")"
)
_NO_IMPACT_PREDICATE_RE = (
    r"(?:"
    r"未对(?:本公司|本集团|公司)?的?(?:合并)?财务报表产生影响"
    r"|对(?:本公司|本集团|公司)?的?(?:合并)?财务报表无影响"
    r"|不影响(?:本公司|本集团|公司)?的?(?:合并)?财务报表"
    r"|未对比较数据产生影响"
    r"|不影响比较数据"
    r"|不需追溯调整"
    r")"
)
_EXPLICIT_NO_IMPACT_RE = re.compile(
    "(" + _SUBJECT_TOKEN_RE + "(?:[及和、]" + _SUBJECT_TOKEN_RE + ")*)(均)?" + _NO_IMPACT_PREDICATE_RE
)
_COMPARATIVE_IMPACT_PREDICATE_RE = r"(?:仍)?对比较数据(?:产生了?(?:重大)?影响|有(?:重大)?影响|进行了?调整)"
_EXPLICIT_COMPARATIVE_IMPACT_RE = re.compile(
    r"(?:本集团|本公司|公司)?"
    + "("
    + _SUBJECT_TOKEN_RE
    + "(?:[及和、]"
    + _SUBJECT_TOKEN_RE
    + ")*)(均)?"
    + _COMPARATIVE_IMPACT_PREDICATE_RE
)
_SUMMARY_BLOCKING_OUTCOMES = frozenset(
    {"impact", "unresolved_comparative_impact", "choice_conflict", "association_ambiguous"}
)
_UNRESOLVED_COMPARATIVE_OBJECT_RE = re.compile(r"比较数据|比较报表|以前年度会计数据")
_UNRESOLVED_COMPARATIVE_EFFECT_RE = re.compile(r"影响|调整|重述")
_CLAUSE_END_RE = re.compile(r"[。；;]")
_BODY_EVIDENCE_OUTCOMES = frozenset({"no_impact", "impact", "unresolved_comparative_impact"})

_TABLE_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(-?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+))(?![\d.,])"
)
_CHOICE_PATTERNS: tuple[tuple[_CheckboxChoice, str], ...] = (
    ("not_applicable", "□适用√不适用"),
    ("not_applicable", "√不适用□适用"),
    ("applicable", "√适用□不适用"),
    ("applicable", "□不适用√适用"),
    ("no", "□是√否"),
    ("no", "√否□是"),
    ("yes", "√是□否"),
    ("yes", "□否√是"),
)
_HEADING_PREFIX_PATTERN = (
    r"(?:"
    r"第[一二三四五六七八九十百0-9]+节"
    r"|[一二三四五六七八九十]+、"
    r"|[0-9]+、"
    r"|[（(][一二三四五六七八九十]+[)）]"
    r"|[（(][0-9]+[)）]"
    r"|[0-9]+(?:\.[0-9]+)+"
    r"|[0-9]+\.(?![0-9])"
    r")"
)
_CHAPTER_HEADING_RE = re.compile(r"^" + _HEADING_PREFIX_PATTERN)
_HEADING_PREFIX_FIND_RE = re.compile(_HEADING_PREFIX_PATTERN)
_COMBINED_HEADING_TOPICS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "会计政策、会计估计变更或重大会计差错更正的情况说明",
        frozenset({_TOPIC_POLICY, _TOPIC_ESTIMATE, _TOPIC_PRIOR_ERROR}),
    ),
    (
        "会计政策、会计估计变更原因及影响",
        frozenset({_TOPIC_POLICY, _TOPIC_ESTIMATE}),
    ),
)
_SEPARATE_HEADING_TITLES: tuple[tuple[str, str, _HeadingSource], ...] = (
    ("重要会计政策变更", _TOPIC_POLICY, "major"),
    ("重要会计估计变更", _TOPIC_ESTIMATE, "major"),
    ("重大会计差错更正原因及影响", _TOPIC_PRIOR_ERROR, "summary"),
    ("是否需追溯调整或重述以前年度会计数据", _TOPIC_NO_RESTATEMENT, "major"),
)
_OPENING_OTHER_ROW_TOKENS = (
    "调整后",
    "调整前",
    "加：",
    "减：",
    "期末未分配",
    "影响期初",
    "明细",
    "本期归属于",
)


@dataclass(frozen=True, slots=True)
class ComparabilityEvidence:
    """从原 PDF 重新定位的一条年度可比性证据。"""

    evidence_id: str
    topic: str
    document_id: str
    source_sha256: str
    report_year: int
    current_year: int
    comparative_year: int
    pdf_page: int
    bbox: PdfBBox
    text: str
    report_year_page: int
    report_year_bbox: PdfBBox
    report_year_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "topic": self.topic,
            "document_id": self.document_id,
            "source_sha256": self.source_sha256,
            "report_year": self.report_year,
            "current_year": self.current_year,
            "comparative_year": self.comparative_year,
            "pdf_page": self.pdf_page,
            "bbox": _bbox_dict(self.bbox),
            "text": self.text,
            "report_year_page": self.report_year_page,
            "report_year_bbox": _bbox_dict(self.report_year_bbox),
            "report_year_text": self.report_year_text,
        }


@dataclass(frozen=True, slots=True)
class AnnualComparabilityCheck:
    """年报本期数与比较数的可比性结论及证据。"""

    verification_id: str
    document_id: str
    source_sha256: str
    report_year: int
    current_year: int
    comparative_year: int
    status: ComparabilityStatus
    restatement_status: RestatementStatus
    evidence: tuple[ComparabilityEvidence, ...]
    checks: tuple[str, ...]
    conflicts: tuple[str, ...]
    limitations: tuple[str, ...]
    _nonce: str = field(default="", repr=False, compare=False)
    _signature: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        """返回审计记录。此字典及其重建对象不能作为计算授权 proof。"""

        return {
            "verification_id": self.verification_id,
            "document_id": self.document_id,
            "source_sha256": self.source_sha256,
            "report_year": self.report_year,
            "current_year": self.current_year,
            "comparative_year": self.comparative_year,
            "status": self.status,
            "restatement_status": self.restatement_status,
            "evidence": [item.to_dict() for item in self.evidence],
            "checks": list(self.checks),
            "conflicts": list(self.conflicts),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    bbox: PdfBBox


@dataclass(frozen=True, slots=True)
class _HeadingUnit:
    topics: frozenset[str]
    source: _HeadingSource


@dataclass(frozen=True, slots=True)
class _Hit:
    topic: str
    outcome: _TopicOutcome
    choice: _ChoiceResult
    evidence: ComparabilityEvidence
    source: _HeadingSource


def verify_annual_comparability(
    pdf_path: str | Path,
    *,
    document_id: str,
    source_sha256: str,
    report_year: int,
) -> AnnualComparabilityCheck:
    """从实际 PDF 检查本期及上期金额是否有明确追溯调整披露。

    当前规则只在年报逐项披露关键追溯调整原因、期初未分配利润影响为零，
    且相关会计政策、估计、首次执行调整和追溯重述均明确为不适用，
    或虽勾选适用但原文证实对财务报表无影响时，才确认 ``not_restated``。
    缺少任一项、空白金额、无法按年度列解释的非零合计或出现冲突文字均不会确认。
    """

    _validate_arguments(document_id, source_sha256, report_year)
    path = Path(pdf_path)
    try:
        payload = path.read_bytes()
    except OSError:
        return _result(
            document_id=document_id,
            expected_sha256=source_sha256,
            actual_sha256="",
            report_year=report_year,
            status="insufficient_evidence",
            evidence=(),
            checks=(),
            conflicts=(),
            limitations=("无法读取原始 PDF。",),
        )

    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual_sha256, source_sha256.lower()):
        return _result(
            document_id=document_id,
            expected_sha256=source_sha256,
            actual_sha256=actual_sha256,
            report_year=report_year,
            status="insufficient_evidence",
            evidence=(),
            checks=(),
            conflicts=(),
            limitations=("原始 PDF SHA256 与调用方绑定的来源哈希不一致。",),
        )

    try:
        import pymupdf

        document = pymupdf.open(stream=payload, filetype="pdf")
    except Exception:
        return _result(
            document_id=document_id,
            expected_sha256=source_sha256,
            actual_sha256=actual_sha256,
            report_year=report_year,
            status="insufficient_evidence",
            evidence=(),
            checks=(),
            conflicts=(),
            limitations=("原始文件无法作为 PDF 打开。",),
        )

    pages = tuple(_page_lines(document[index]) for index in range(len(document)))
    document.close()
    evidence: list[ComparabilityEvidence] = []
    checks: list[str] = []
    missing: list[str] = []
    conflicts: list[str] = []

    hits_by_topic = _collect_disclosure_hits(
        pages,
        report_year,
        document_id=document_id,
        source_sha256=actual_sha256,
    )
    if not hits_by_topic[_TOPIC_PRIOR_ERROR] and hits_by_topic[_TOPIC_NO_RESTATEMENT]:
        no_restatement = [
            replace(
                hit,
                topic=_TOPIC_PRIOR_ERROR,
                evidence=replace(
                    hit.evidence,
                    topic=_TOPIC_PRIOR_ERROR,
                    evidence_id=_evidence_id(
                        hit.evidence.source_sha256,
                        report_year,
                        _TOPIC_PRIOR_ERROR,
                        hit.evidence.pdf_page - 1,
                    ),
                ),
            )
            for hit in hits_by_topic[_TOPIC_NO_RESTATEMENT]
            if hit.outcome == "no_impact"
        ]
        hits_by_topic[_TOPIC_PRIOR_ERROR].extend(no_restatement)

    for topic in (*_REQUIRED_TOPICS, _TOPIC_NO_RESTATEMENT):
        _record_topic_hits(
            topic,
            hits_by_topic[topic],
            evidence,
            checks,
            missing,
            conflicts,
            required=topic in _REQUIRED_TOPICS,
        )

    opening, opening_missing, opening_conflicts = _opening_adjustment_evidence(
        pages,
        report_year,
        document_id=document_id,
        source_sha256=actual_sha256,
    )
    evidence.extend(opening)
    if opening_missing:
        missing.extend(opening_missing)
    if opening_conflicts:
        conflicts.extend(opening_conflicts)
    unexplained_totals = _unexplained_opening_totals(pages, report_year)
    if unexplained_totals:
        missing.extend(unexplained_totals)
    elif not opening_missing and not opening_conflicts:
        checks.append("五类期初未分配利润调整原因均有原文披露，金额为 0 元。")

    positive_evidence, positive_disclosures = _explicit_positive_restatement(
        pages,
        report_year,
        document_id=document_id,
        source_sha256=actual_sha256,
    )
    evidence.extend(positive_evidence)
    conflicts.extend(positive_disclosures)

    required_pages = {item.pdf_page - 1 for item in evidence}
    if not evidence:
        missing.append("缺少可定位的年度可比性证据。")
    for page_index in sorted(required_pages):
        if _report_year_line(pages[page_index], report_year) is None:
            missing.append(f"第 {page_index + 1} 页无法从年报抬头确认报告年份 {report_year}，相关证据不足。")

    if conflicts:
        status: ComparabilityStatus = "conflict"
        restatement_status: RestatementStatus = "unknown"
        limitations = ("原文含有相互矛盾或支持调整的披露；计算必须弃权。",)
    elif missing:
        status = "insufficient_evidence"
        restatement_status = "unknown"
        limitations = tuple(dict.fromkeys(missing))
    else:
        status = "verified"
        restatement_status = "not_restated"
        limitations = (
            "本结论依据本年度报告内的追溯调整披露，不代表已独立重算或勾稽上一年度已披露报告。",
        )

    return _result(
        document_id=document_id,
        expected_sha256=source_sha256,
        actual_sha256=actual_sha256,
        report_year=report_year,
        status=status,
        restatement_status=restatement_status,
        evidence=tuple(evidence),
        checks=tuple(checks),
        conflicts=tuple(dict.fromkeys(conflicts)),
        limitations=limitations,
    )


def is_verified_annual_comparability(value: object) -> bool:
    """确认 proof 由当前进程中的 PDF 核验函数签发且内容未被改写。"""

    if not isinstance(value, AnnualComparabilityCheck) or not value._nonce or not value._signature:
        return False
    expected = _signature(value, value._nonce)
    return hmac.compare_digest(value._signature, expected)


def comparability_mismatch_reason(
    value: object,
    *,
    document_id: str,
    source_sha256: str,
    report_year: int,
) -> str | None:
    """检查 proof 是否精确绑定到传入事实的来源和期间。"""

    if not is_verified_annual_comparability(value):
        return "缺少由原始 PDF 核验函数签发的年度可比性证明。"
    assert isinstance(value, AnnualComparabilityCheck)
    if value.status != "verified" or value.restatement_status != "not_restated":
        detail = "；".join(value.conflicts or value.limitations) or "核验状态不是 verified/not_restated。"
        return f"年度可比性核验未通过：{detail}"
    if value.document_id != document_id:
        return "年度可比性证明的来源 document_id 与事实不一致。"
    if value.source_sha256.lower() != source_sha256.lower():
        return "年度可比性证明的来源 SHA256 与事实不一致。"
    if value.report_year != report_year or value.current_year != report_year:
        return "年度可比性证明的报告年或本期年度与计算期间不一致。"
    if value.comparative_year != report_year - 1:
        return "年度可比性证明没有覆盖报告年的上一比较年度。"
    if not value.evidence or any(
        item.document_id != document_id
        or item.source_sha256.lower() != source_sha256.lower()
        or item.report_year != report_year
        or item.current_year != report_year
        or item.comparative_year != report_year - 1
        for item in value.evidence
    ):
        return "年度可比性证明的原文证据与事实来源或年度不一致。"
    return None


def _validate_arguments(document_id: str, source_sha256: str, report_year: int) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id 不能为空。")
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
        raise ValueError("source_sha256 必须是 64 位十六进制 SHA256。")
    if type(report_year) is not int or report_year < 1900 or report_year > 2100:
        raise ValueError("report_year 必须是 1900 到 2100 之间的整数。")


def _page_lines(page: object) -> tuple[_Line, ...]:
    lines: list[_Line] = []
    data = page.get_text("dict", sort=True)  # type: ignore[attr-defined]
    for block in data.get("blocks", ()):
        for line in block.get("lines", ()):
            spans = line.get("spans", ())
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            boxes = [span["bbox"] for span in spans if span.get("text", "").strip()]
            if not boxes:
                continue
            lines.append(
                _Line(
                    text=text,
                    bbox=PdfBBox(
                        x0=min(box[0] for box in boxes),
                        y0=min(box[1] for box in boxes),
                        x1=max(box[2] for box in boxes),
                        y1=max(box[3] for box in boxes),
                    ),
                )
            )
    return tuple(lines)


def _collect_disclosure_hits(
    pages: tuple[tuple[_Line, ...], ...],
    report_year: int,
    *,
    document_id: str,
    source_sha256: str,
) -> dict[str, list[_Hit]]:
    hits: dict[str, list[_Hit]] = defaultdict(list)
    for page_index, lines in enumerate(pages):
        header = _report_year_line(lines, report_year)
        rows = _visual_rows(lines)
        for row_index, row in enumerate(rows):
            units = _discrete_heading_units(row, rows, row_index, report_year)
            if not units:
                continue
            if len(units) > 1:
                if header is None:
                    continue
                _record_association_ambiguity(
                    hits,
                    topics=frozenset(topic for unit in units for topic in unit.topics),
                    row=row,
                    page_index=page_index,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    report_year=report_year,
                    header=header,
                    source="major" if any(unit.source == "major" for unit in units) else "summary",
                )
                continue
            unit = units[0]
            matched = _adjacent_choice(rows, row_index, report_year)
            if matched is None:
                continue
            choice, choice_lines, choice_index = matched
            if header is None:
                continue
            choice_y1 = max(line.bbox.y1 for line in rows[choice_index])
            for topic in unit.topics:
                body_lines = _topic_body_lines(rows, choice_index, choice_y1, report_year)
                body_outcome = _body_outcome(body_lines, topic)
                outcome = _choice_outcome(choice, body_outcome)
                classified_body = body_lines if body_outcome in _BODY_EVIDENCE_OUTCOMES else ()
                evidence_lines = tuple(dict.fromkeys(tuple(row) + tuple(choice_lines) + tuple(classified_body)))
                evidence = _make_evidence(
                    topic=topic,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    report_year=report_year,
                    page_index=page_index,
                    lines=evidence_lines,
                    report_header=header,
                )
                hits[topic].append(
                    _Hit(
                        topic=topic,
                        outcome=outcome,
                        choice=choice,
                        evidence=evidence,
                        source=unit.source,
                    )
                )
    return hits


def _preferred_topic_hits(hits: list[_Hit]) -> list[_Hit]:
    major = [hit for hit in hits if hit.source == "major"]
    if not major:
        return list(hits)
    extras = [
        hit for hit in hits if hit.source == "summary" and hit.outcome in _SUMMARY_BLOCKING_OUTCOMES
    ]
    return major + extras


def _record_topic_hits(
    topic: str,
    hits: list[_Hit],
    evidence: list[ComparabilityEvidence],
    checks: list[str],
    missing: list[str],
    conflicts: list[str],
    *,
    required: bool,
) -> None:
    hits = _preferred_topic_hits(hits)
    label = _TOPIC_LABELS.get(topic, topic)
    if not hits:
        if required:
            missing.append(f"{label}未明确勾选不适用（未找到与 {label} 精确关联的不适用披露。）。")
        return

    outcomes = {hit.outcome for hit in hits}
    for hit in hits:
        if all(
            item.evidence_id != hit.evidence.evidence_id
            or item.topic != hit.topic
            or item.text != hit.evidence.text
            for item in evidence
        ):
            evidence.append(hit.evidence)

    if "choice_conflict" in outcomes:
        conflicts.append(f"{label}的选择项存在相互矛盾或无法唯一归属的勾选。")
        return
    if "impact" in outcomes:
        conflicts.append(_impact_conflict_message(topic, hits))
        return
    if "association_ambiguous" in outcomes:
        missing.append(
            f"{label}与选择项无法唯一关联（同一视觉行存在多个分立标题，不能共用同一勾选）。"
        )
        return
    if "unresolved_comparative_impact" in outcomes:
        missing.append(
            f"{label}原文涉及比较数据、比较报表或以前年度会计数据的影响、调整或重述，但未能明确确认或排除。"
        )
        return
    if "unconfirmed_applicable" in outcomes:
        missing.append(f"{label}勾选适用，但未证实对比较数据或财务报表无影响。")
        return
    if outcomes == {"no_impact"}:
        checks.append(_no_impact_check_message(topic, hits[0].choice))


def _impact_conflict_message(topic: str, hits: list[_Hit]) -> str:
    label = _TOPIC_LABELS.get(topic, topic)
    if any(hit.choice == "yes" for hit in hits):
        return f"{label}明确勾选是，比较数据可能已追溯调整或重述。"
    if any(hit.outcome == "impact" for hit in hits):
        return f"{label}明确披露对比较数据产生影响。"
    return f"{label}明确勾选适用，且未排除对比较数据的影响。"


def _no_impact_check_message(topic: str, choice: _ChoiceResult) -> str:
    if choice == "no":
        return f"{topic} 已从原文定位并明确勾选否（不需追溯调整或重述）。"
    if choice == "applicable":
        return f"{topic} 已从原文定位并明确披露对财务报表无影响。"
    return f"{topic} 已从原文定位并明确勾选不适用。"


def _heading_topics(normalized: str, report_year: int) -> frozenset[str]:
    if _is_first_adoption_heading(normalized, report_year):
        return frozenset({_TOPIC_FIRST_ADOPTION})
    topics: set[str] = set()
    if "重要会计政策变更" in normalized:
        topics.add(_TOPIC_POLICY)
    if "重要会计估计变更" in normalized:
        topics.add(_TOPIC_ESTIMATE)
    if "会计政策、会计估计变更原因及影响" in normalized:
        topics.update({_TOPIC_POLICY, _TOPIC_ESTIMATE})
    if "重大会计差错更正原因及影响" in normalized:
        topics.add(_TOPIC_PRIOR_ERROR)
    if "会计政策、会计估计变更或重大会计差错更正的情况说明" in normalized:
        topics.update({_TOPIC_POLICY, _TOPIC_ESTIMATE, _TOPIC_PRIOR_ERROR})
    if "是否需追溯调整或重述以前年度会计数据" in normalized:
        topics.add(_TOPIC_NO_RESTATEMENT)
    return frozenset(topics)


def _discrete_heading_units(
    row: tuple[_Line, ...],
    rows: tuple[tuple[_Line, ...], ...],
    row_index: int,
    report_year: int,
) -> tuple[_HeadingUnit, ...]:
    units: list[_HeadingUnit] = []
    for line in row:
        units.extend(_heading_units_in_text(_normalize(line.text), report_year))
    if units:
        return tuple(units)
    if _is_retrospective_restatement_heading(rows, row_index):
        return (_HeadingUnit(topics=frozenset({_TOPIC_PRIOR_ERROR}), source="major"),)
    return ()


def _heading_units_in_text(normalized: str, report_year: int) -> list[_HeadingUnit]:
    if not normalized:
        return []
    units: list[_HeadingUnit] = []
    for part in _split_numbered_heading_parts(normalized):
        units.extend(_units_from_heading_part(part, report_year))
    return units


def _split_numbered_heading_parts(normalized: str) -> list[str]:
    matches = list(_HEADING_PREFIX_FIND_RE.finditer(normalized))
    if len(matches) < 2:
        return [normalized]
    parts: list[str] = []
    if matches[0].start() > 0:
        head = normalized[: matches[0].start()]
        if head:
            parts.append(head)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        part = normalized[match.start() : end]
        if part:
            parts.append(part)
    return parts


def _units_from_heading_part(normalized: str, report_year: int) -> list[_HeadingUnit]:
    units: list[_HeadingUnit] = []
    remainder = normalized
    for phrase, topics in _COMBINED_HEADING_TOPICS:
        if phrase in remainder:
            units.append(_HeadingUnit(topics=topics, source="summary"))
            remainder = remainder.replace(phrase, "", 1)
            break
    for title, topic, source in _SEPARATE_HEADING_TITLES:
        found = remainder.count(title)
        for _ in range(found):
            units.append(_HeadingUnit(topics=frozenset({topic}), source=source))
        if found:
            remainder = remainder.replace(title, "")
    if _is_first_adoption_heading(normalized, report_year):
        units.append(_HeadingUnit(topics=frozenset({_TOPIC_FIRST_ADOPTION}), source="major"))
    return units


def _record_association_ambiguity(
    hits: dict[str, list[_Hit]],
    *,
    topics: frozenset[str],
    row: tuple[_Line, ...],
    page_index: int,
    document_id: str,
    source_sha256: str,
    report_year: int,
    header: _Line,
    source: _HeadingSource,
) -> None:
    for topic in topics:
        evidence = _make_evidence(
            topic=topic,
            document_id=document_id,
            source_sha256=source_sha256,
            report_year=report_year,
            page_index=page_index,
            lines=row,
            report_header=header,
        )
        hits[topic].append(
            _Hit(
                topic=topic,
                outcome="association_ambiguous",
                choice="conflict",
                evidence=evidence,
                source=source,
            )
        )


def _is_first_adoption_heading(normalized: str, report_year: int) -> bool:
    if f"{report_year}年起首次执行" not in normalized:
        return False
    if "年初" not in normalized:
        return False
    return "财务报表" in normalized or "报表" in normalized


def _is_retrospective_restatement_heading(rows: tuple[tuple[_Line, ...], ...], row_index: int) -> bool:
    if "追溯重述法" not in _normalize(_row_text(rows[row_index])):
        return False
    context = rows[max(0, row_index - 3) : row_index]
    return any("前期会计差错更正" in _normalize(_row_text(row)) for row in context)


def _is_chapter_heading(normalized: str) -> bool:
    match = _CHAPTER_HEADING_RE.match(normalized)
    if match is None:
        return False
    rest = normalized[match.end() :]
    if rest.startswith(".") and len(rest) > 1 and not rest[1].isdigit() and rest[1] not in "+-.":
        rest = rest[1:]
    if not rest:
        return False
    if rest[0] in "+-.%％元" or rest[0].isdigit():
        return False
    if rest.endswith("%") or rest.endswith("％"):
        return False
    stripped = rest[:-1] if rest.endswith("元") else rest
    if not stripped or _TABLE_AMOUNT_RE.fullmatch(stripped) is not None:
        return False
    return re.match(r"[\u4e00-\u9fffA-Za-z]", rest) is not None


def _row_is_heading(rows: tuple[tuple[_Line, ...], ...], row_index: int, report_year: int) -> bool:
    normalized = _normalize(_row_text(rows[row_index]))
    if _heading_topics(normalized, report_year):
        return True
    if _is_retrospective_restatement_heading(rows, row_index):
        return True
    return _is_chapter_heading(normalized)


def _adjacent_choice(
    rows: tuple[tuple[_Line, ...], ...],
    heading_index: int,
    report_year: int,
) -> tuple[_ChoiceResult, tuple[_Line, ...], int] | None:
    heading = rows[heading_index]
    heading_y1 = max(line.bbox.y1 for line in heading)
    same_row_choice = _choice_from_text(_row_text(heading))
    if same_row_choice is not None:
        return same_row_choice, heading, heading_index

    for next_index in range(heading_index + 1, len(rows)):
        candidate = rows[next_index]
        candidate_y0 = min(line.bbox.y0 for line in candidate)
        if candidate_y0 - heading_y1 > 36:
            break
        if _row_is_heading(rows, next_index, report_year):
            break
        choice = _choice_from_text(_row_text(candidate))
        if choice is None:
            continue
        return choice, candidate, next_index
    return None


def _topic_body_lines(
    rows: tuple[tuple[_Line, ...], ...],
    start_index: int,
    start_y1: float,
    report_year: int,
) -> tuple[_Line, ...]:
    collected: list[_Line] = []
    for index in range(start_index + 1, min(start_index + 6, len(rows))):
        row = rows[index]
        y0 = min(line.bbox.y0 for line in row)
        if y0 - start_y1 > 96:
            break
        if _row_is_heading(rows, index, report_year):
            break
        collected.extend(row)
    return tuple(collected)


def _body_outcome(body: tuple[_Line, ...], topic: str) -> _TopicOutcome | None:
    if not body:
        return None
    window = "\n".join(line.text for line in body)
    if _explicit_subject_comparative_impact(window, topic):
        return "impact"
    if _explicit_subject_no_impact(window, topic):
        return "no_impact"
    if _unresolved_comparative_effect(window, topic):
        return "unresolved_comparative_impact"
    return None


def _normalized_clauses(text: str) -> list[str]:
    return [part for part in (_normalize(item) for item in _CLAUSE_END_RE.split(text)) if part]


def _explicit_subject_no_impact(text: str, topic: str) -> bool:
    clauses = _normalized_clauses(text)
    if not clauses:
        return False
    covered = False
    for clause in clauses:
        match = _EXPLICIT_NO_IMPACT_RE.fullmatch(clause)
        if match is None:
            return False
        subjects = _topics_from_subject_list(match.group(1))
        if topic not in subjects:
            continue
        if len(subjects) > 1 and match.group(2) != "均":
            return False
        covered = True
    return covered


def _explicit_subject_comparative_impact(text: str, topic: str) -> bool:
    for clause in _normalized_clauses(text):
        match = _EXPLICIT_COMPARATIVE_IMPACT_RE.fullmatch(clause)
        if match is None:
            continue
        subjects = _topics_from_subject_list(match.group(1))
        if topic not in subjects:
            continue
        if len(subjects) > 1 and match.group(2) != "均":
            continue
        return True
    return False


def _unresolved_comparative_effect(text: str, topic: str) -> bool:
    cues = _TOPIC_ASSOCIATION.get(topic, ())
    if not cues:
        return False
    for clause in _normalized_clauses(text):
        if (
            _explicit_subject_no_impact(clause, topic)
            or _explicit_subject_comparative_impact(clause, topic)
        ):
            continue
        if not any(cue in clause for cue in cues):
            continue
        if _UNRESOLVED_COMPARATIVE_OBJECT_RE.search(clause) is None:
            continue
        if _UNRESOLVED_COMPARATIVE_EFFECT_RE.search(clause) is None:
            continue
        return True
    return False


def _topics_from_subject_list(subject_list: str) -> frozenset[str]:
    topics: set[str] = set()
    for other, cues in _TOPIC_ASSOCIATION.items():
        if any(cue in subject_list for cue in cues):
            topics.add(other)
    return frozenset(topics)


def _choice_outcome(choice: _ChoiceResult, body_outcome: _TopicOutcome | None) -> _TopicOutcome:
    if choice == "conflict":
        return "choice_conflict"
    if body_outcome == "impact":
        return "impact"
    if choice == "yes":
        return "impact"
    if body_outcome == "unresolved_comparative_impact":
        return "unresolved_comparative_impact"
    if choice in {"not_applicable", "no"}:
        return "no_impact"
    if body_outcome == "no_impact":
        return "no_impact"
    return "unconfirmed_applicable"


def _choice_from_text(value: str) -> _ChoiceResult | None:
    compact = _normalize_marks(value)
    found: list[_CheckboxChoice] = []
    cursor = 0
    while cursor < len(compact):
        best: tuple[int, int, _CheckboxChoice] | None = None
        for choice, pattern in _CHOICE_PATTERNS:
            index = compact.find(pattern, cursor)
            if index < 0:
                continue
            if best is None or index < best[0]:
                best = (index, index + len(pattern), choice)
        if best is None:
            break
        found.append(best[2])
        cursor = best[1]
    if not found:
        return None
    unique = set(found)
    if len(unique) == 1:
        return next(iter(unique))
    return "conflict"


def _opening_adjustment_evidence(
    pages: tuple[tuple[_Line, ...], ...],
    report_year: int,
    *,
    document_id: str,
    source_sha256: str,
) -> tuple[tuple[ComparabilityEvidence, ...], tuple[str, ...], tuple[str, ...]]:
    found: dict[str, tuple[int, _Line, str]] = {}
    missing = list(_REQUIRED_CAUSES)
    missing_headers: list[str] = []
    conflicts: list[str] = []
    for page_index, lines in enumerate(pages):
        for line in lines:
            normalized = _normalize(line.text)
            if "影响期初未分配利润" not in normalized:
                continue
            cause = _opening_cause(normalized)
            if cause is None:
                continue
            amount_match = re.search(r"影响期初未分配利润(-?\d+(?:\.\d+)?)元", normalized)
            if amount_match is None:
                continue
            if not _is_zero_amount(amount_match.group(1)):
                conflicts.append(f"{cause}披露的期初未分配利润影响为 {amount_match.group(1)} 元。")
            found[cause] = (page_index, line, normalized)
    output: list[ComparabilityEvidence] = []
    by_page: dict[int, list[_Line]] = {}
    for cause, (page_index, line, _text) in found.items():
        by_page.setdefault(page_index, []).append(line)
        if cause in missing:
            missing.remove(cause)
    for page_index, cause_lines in sorted(by_page.items()):
        header = _report_year_line(pages[page_index], report_year)
        if header is None:
            missing_headers.append(f"第 {page_index + 1} 页的调整影响披露缺少报告年份抬头，无法绑定报告年度。")
            continue
        output.append(
            _make_evidence(
                topic=_TOPIC_OPENING,
                document_id=document_id,
                source_sha256=source_sha256,
                report_year=report_year,
                page_index=page_index,
                lines=tuple(cause_lines),
                report_header=header,
            )
        )
    absent = tuple(f"缺少“{cause}”对期初未分配利润影响为 0 元的明确披露。" for cause in missing)
    absent += tuple(missing_headers)
    return tuple(output), absent, tuple(conflicts)


def _unexplained_opening_totals(
    pages: tuple[tuple[_Line, ...], ...],
    report_year: int,
) -> tuple[str, ...]:
    limitations: list[str] = []
    for page_index, lines in enumerate(pages):
        rows = _visual_rows(lines)
        for row_index, row in enumerate(rows):
            compact = _normalize(_row_text(row))
            if "调整期初未分配利润合计" not in compact:
                continue
            if "影响期初未分配利润" in compact:
                continue
            amounts = _total_row_amounts(rows, row_index)
            nonzero = [item for item in amounts if not _is_zero_amount(item)]
            if not nonzero:
                continue
            header = _report_year_line(lines, report_year)
            if header is None:
                limitations.append(f"第 {page_index + 1} 页的调整期初未分配利润合计缺少报告年份抬头，无法绑定报告年度。")
                continue
            limitations.append(
                f"第 {page_index + 1} 页存在非零“调整期初未分配利润合计”金额，且无法按年度列唯一核验，不能当作无调整。"
            )
    return tuple(dict.fromkeys(limitations))


def _total_row_amounts(rows: tuple[tuple[_Line, ...], ...], row_index: int) -> list[str]:
    amounts = _row_table_amounts(rows[row_index])
    if amounts:
        return amounts
    if row_index + 1 >= len(rows):
        return []
    current_y1 = max(line.bbox.y1 for line in rows[row_index])
    nxt = rows[row_index + 1]
    next_y0 = min(line.bbox.y0 for line in nxt)
    if next_y0 - current_y1 > 16:
        return []
    compact = _normalize(_row_text(nxt))
    if any(token in compact for token in _OPENING_OTHER_ROW_TOKENS):
        return []
    return _row_table_amounts(nxt)


def _row_table_amounts(row: tuple[_Line, ...]) -> list[str]:
    amounts: list[str] = []
    for line in row:
        amounts.extend(_table_amounts(line.text))
    return amounts


def _table_amounts(text: str) -> list[str]:
    amounts: list[str] = []
    for match in _TABLE_AMOUNT_RE.finditer(text):
        token = match.group(1)
        if _is_year_or_note_amount(token, text, match.start(1), match.end(1)):
            continue
        amounts.append(token.replace(",", ""))
    return amounts


def _is_year_or_note_amount(token: str, context: str, start: int, end: int) -> bool:
    body = token.replace(",", "").lstrip("+-")
    if "." in token:
        return False
    suffix = _normalize(context[end : end + 4])
    if re.fullmatch(r"(?:19|20)\d{2}", body) and suffix.startswith("年"):
        return True
    compact = _normalize(context)
    return bool(re.fullmatch(r"\d{1,3}", body) and re.fullmatch(rf"(?:附)?注{body}", compact))


def _opening_cause(normalized_line: str) -> str | None:
    if "准则" in normalized_line and "追溯调整" in normalized_line:
        return _REQUIRED_CAUSES[0]
    if "会计政策变更" in normalized_line:
        return _REQUIRED_CAUSES[1]
    if "重大会计差错更正" in normalized_line:
        return _REQUIRED_CAUSES[2]
    if "同一控制" in normalized_line and "合并范围变更" in normalized_line:
        return _REQUIRED_CAUSES[3]
    if "其他调整合计" in normalized_line:
        return _REQUIRED_CAUSES[4]
    return None


def _explicit_positive_restatement(
    pages: tuple[tuple[_Line, ...], ...],
    report_year: int,
    *,
    document_id: str,
    source_sha256: str,
) -> tuple[tuple[ComparabilityEvidence, ...], tuple[str, ...]]:
    evidence: list[ComparabilityEvidence] = []
    conflicts: list[str] = []
    for page_index, lines in enumerate(pages):
        for line in lines:
            text = _normalize(line.text)
            # 泛指会计政策的理论性段落（如“对于同一控制合并，应调整比较报表”）不作
            # 公司已执行追溯调整的证据；要求披露句直接指向本公司和具体比较年度/数据。
            has_subject = any(token in text for token in ("本集团", "本公司", "公司已", "集团已"))
            has_period = any(
                token in text
                for token in ("上期", "前期", "比较数据", "比较报表", f"{report_year - 1}年", "以前年度")
            )
            has_action = any(token in text for token in ("追溯调整", "追溯重述", "重新列报", "比较数据调整"))
            opening_amount = re.search(r"影响期初未分配利润(-?\d+(?:\.\d+)?)元", text)
            is_explicit_zero = opening_amount is not None and _is_zero_amount(opening_amount.group(1))
            if has_subject and has_period and has_action and not is_explicit_zero:
                conflicts.append(f"第 {page_index + 1} 页存在明确的追溯调整披露：{line.text}")
                header = _report_year_line(lines, report_year)
                if header is not None:
                    evidence.append(
                        _make_evidence(
                            topic="contradictory_restatement_disclosure",
                            document_id=document_id,
                            source_sha256=source_sha256,
                            report_year=report_year,
                            page_index=page_index,
                            lines=(line,),
                            report_header=header,
                        )
                    )
    return tuple(evidence), tuple(conflicts)


def _report_year_line(lines: tuple[_Line, ...], report_year: int) -> _Line | None:
    needles = (
        f"{report_year}年年度报告",
        f"{report_year}年度报告全文",
        f"{report_year}年年度报告全文",
    )
    return next((line for line in lines if any(needle in _normalize(line.text) for needle in needles)), None)


def _is_zero_amount(value: str) -> bool:
    """仅把 Decimal 精确为零的金额视为零，覆盖 0.00、0.000、-0.00 等写法。"""

    try:
        return Decimal(value) == 0
    except (InvalidOperation, ValueError):
        # 无法解析的金额不能被当成零，调用方将按有影响/冲突处理。
        return False


def _make_evidence(
    *,
    topic: str,
    document_id: str,
    source_sha256: str,
    report_year: int,
    page_index: int,
    lines: tuple[_Line, ...],
    report_header: _Line,
) -> ComparabilityEvidence:
    bbox = _union_bbox(tuple(line.bbox for line in lines))
    return ComparabilityEvidence(
        evidence_id=_evidence_id(source_sha256, report_year, topic, page_index),
        topic=topic,
        document_id=document_id,
        source_sha256=source_sha256,
        report_year=report_year,
        current_year=report_year,
        comparative_year=report_year - 1,
        pdf_page=page_index + 1,
        bbox=bbox,
        text="\n".join(line.text for line in lines),
        report_year_page=page_index + 1,
        report_year_bbox=report_header.bbox,
        report_year_text=report_header.text,
    )


def _evidence_id(source_sha256: str, report_year: int, topic: str, page_index: int) -> str:
    return f"comp-{source_sha256[:12]}-{report_year}-{topic}-p{page_index + 1}"


def _result(
    *,
    document_id: str,
    expected_sha256: str,
    actual_sha256: str,
    report_year: int,
    status: ComparabilityStatus,
    evidence: tuple[ComparabilityEvidence, ...],
    checks: tuple[str, ...],
    conflicts: tuple[str, ...],
    limitations: tuple[str, ...],
    restatement_status: RestatementStatus = "unknown",
) -> AnnualComparabilityCheck:
    nonce = secrets.token_hex(16)
    value = AnnualComparabilityCheck(
        verification_id=f"comparability-{report_year}-{actual_sha256[:12] or 'unavailable'}",
        document_id=document_id,
        source_sha256=actual_sha256 or expected_sha256.lower(),
        report_year=report_year,
        current_year=report_year,
        comparative_year=report_year - 1,
        status=status,
        restatement_status=restatement_status,
        evidence=evidence,
        checks=checks,
        conflicts=conflicts,
        limitations=limitations,
        _nonce=nonce,
    )
    signature = _signature(value, nonce)
    return replace(value, _signature=signature)


def _signature(value: AnnualComparabilityCheck, nonce: str) -> str:
    payload = {
        "verification_id": value.verification_id,
        "document_id": value.document_id,
        "source_sha256": value.source_sha256,
        "report_year": value.report_year,
        "current_year": value.current_year,
        "comparative_year": value.comparative_year,
        "status": value.status,
        "restatement_status": value.restatement_status,
        "evidence": [item.to_dict() for item in value.evidence],
        "checks": value.checks,
        "conflicts": value.conflicts,
        "limitations": value.limitations,
        "nonce": nonce,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_SIGNING_KEY, body, hashlib.sha256).hexdigest()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _normalize_marks(value: str) -> str:
    mapped: list[str] = []
    for char in value:
        if char in _CHECKED_MARKS:
            mapped.append("√")
        elif char in _EMPTY_MARKS:
            mapped.append("□")
        else:
            mapped.append(char)
    return _normalize("".join(mapped))


def _visual_rows(lines: tuple[_Line, ...]) -> tuple[tuple[_Line, ...], ...]:
    if not lines:
        return ()
    rows: list[list[_Line]] = [[lines[0]]]
    for line in lines[1:]:
        previous = rows[-1][-1]
        overlap = min(previous.bbox.y1, line.bbox.y1) - max(previous.bbox.y0, line.bbox.y0)
        height = max(previous.bbox.y1 - previous.bbox.y0, line.bbox.y1 - line.bbox.y0, 1.0)
        if overlap > 0.4 * height or abs(line.bbox.y0 - previous.bbox.y0) <= 2.0:
            rows[-1].append(line)
        else:
            rows.append([line])
    return tuple(tuple(sorted(row, key=lambda item: item.bbox.x0)) for row in rows)


def _row_text(row: tuple[_Line, ...]) -> str:
    return "".join(line.text for line in row)


def _union_bbox(boxes: tuple[PdfBBox, ...]) -> PdfBBox:
    return PdfBBox(
        x0=min(box.x0 for box in boxes),
        y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes),
        y1=max(box.y1 for box in boxes),
    )


def _bbox_dict(value: PdfBBox) -> dict[str, float]:
    return {"x0": value.x0, "y0": value.y0, "x1": value.x1, "y1": value.y1}
