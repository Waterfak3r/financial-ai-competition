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
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from finagent.schemas.text_pdf import PdfBBox

ComparabilityStatus = Literal["verified", "conflict", "insufficient_evidence"]
RestatementStatus = Literal["not_restated", "unknown"]

_SIGNING_KEY = secrets.token_bytes(32)
_CHECKBOX_NOT_APPLICABLE = "□适用√不适用"
_CHECKBOX_APPLICABLE = "√适用□不适用"
_REQUIRED_CAUSES = (
    "准则追溯调整",
    "会计政策变更",
    "重大会计差错更正",
    "同一控制合并范围变更",
    "其他调整",
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


def verify_annual_comparability(
    pdf_path: str | Path,
    *,
    document_id: str,
    source_sha256: str,
    report_year: int,
) -> AnnualComparabilityCheck:
    """从实际 PDF 检查本期及上期金额是否有明确追溯调整披露。

    当前规则只在年报逐项披露关键追溯调整原因、期初未分配利润影响为零，
    且相关会计政策、估计、首次执行调整和追溯重述法均明确勾选“不适用”时，
    才确认 ``not_restated``。缺少任一项或出现冲突文字均不会确认。
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

    policy, issue = _section_checkbox(
        pages,
        report_year,
        section="重要会计政策变更",
        subheading="重要会计政策变更",
        topic="major_accounting_policy_change",
        document_id=document_id,
        source_sha256=actual_sha256,
    )
    _record_checkbox(policy, issue, "重要会计政策变更未明确勾选不适用", evidence, checks, missing, conflicts)

    estimate, issue = _section_checkbox(
        pages,
        report_year,
        section="重要会计估计变更",
        subheading="重要会计估计变更",
        topic="major_accounting_estimate_change",
        document_id=document_id,
        source_sha256=actual_sha256,
    )
    _record_checkbox(estimate, issue, "重要会计估计变更未明确勾选不适用", evidence, checks, missing, conflicts)

    first_adoption_heading = (
        f"{report_year}年起首次执行新会计准则或准则解释等涉及调整首次执行当年年初的财务报表"
    )
    first_adoption, issue = _section_checkbox(
        pages,
        report_year,
        section=first_adoption_heading,
        subheading=first_adoption_heading,
        topic="first_adoption_opening_balance_adjustment",
        document_id=document_id,
        source_sha256=actual_sha256,
    )
    _record_checkbox(
        first_adoption,
        issue,
        "首次执行新准则对年初财务报表的调整披露未明确勾选不适用",
        evidence,
        checks,
        missing,
        conflicts,
    )

    prior_error, issue = _prior_error_checkbox(
        pages,
        report_year,
        document_id=document_id,
        source_sha256=actual_sha256,
    )
    _record_checkbox(prior_error, issue, "前期差错追溯重述法未明确勾选不适用", evidence, checks, missing, conflicts)

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
    if not opening_missing and not opening_conflicts:
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


def _section_checkbox(
    pages: tuple[tuple[_Line, ...], ...],
    report_year: int,
    *,
    section: str,
    subheading: str,
    topic: str,
    document_id: str,
    source_sha256: str,
) -> tuple[ComparabilityEvidence | None, str | None]:
    heading_norm = _normalize(subheading)
    for page_index, lines in enumerate(pages):
        for line_index, line in enumerate(lines):
            normalized = _normalize(line.text)
            if heading_norm not in normalized:
                continue
            # 找与该标题垂直距离最近的选择项，不跨页，也不接受无关的通用“不适用”。
            for candidate in lines[line_index + 1 :]:
                if candidate.bbox.y0 - line.bbox.y1 > 36:
                    break
                choice = _normalize(candidate.text)
                if choice in {_CHECKBOX_NOT_APPLICABLE, _CHECKBOX_APPLICABLE}:
                    header = _report_year_line(lines, report_year)
                    if header is None:
                        return None, f"{topic} 所在页面缺少报告年份抬头。"
                    evidence = _make_evidence(
                        topic=topic,
                        document_id=document_id,
                        source_sha256=source_sha256,
                        report_year=report_year,
                        page_index=page_index,
                        lines=(line, candidate),
                        report_header=header,
                    )
                    if choice == _CHECKBOX_APPLICABLE:
                        return evidence, f"{topic} 明确勾选适用，可能影响比较数据。"
                    return evidence, None
            return None, f"{topic} 标题存在，但没有相邻的明确选择项。"
    return None, f"未找到与 {section} 精确关联的不适用披露。"


def _prior_error_checkbox(
    pages: tuple[tuple[_Line, ...], ...],
    report_year: int,
    *,
    document_id: str,
    source_sha256: str,
) -> tuple[ComparabilityEvidence | None, str | None]:
    for page_index, lines in enumerate(pages):
        for line_index, line in enumerate(lines):
            if "追溯重述法" not in _normalize(line.text):
                continue
            context = lines[max(0, line_index - 3) : line_index]
            if not any("前期会计差错更正" in _normalize(item.text) for item in context):
                continue
            for candidate in lines[line_index + 1 :]:
                if candidate.bbox.y0 - line.bbox.y1 > 36:
                    break
                choice = _normalize(candidate.text)
                if choice in {_CHECKBOX_NOT_APPLICABLE, _CHECKBOX_APPLICABLE}:
                    header = _report_year_line(lines, report_year)
                    if header is None:
                        return None, "追溯重述披露所在页面缺少报告年份抬头。"
                    evidence = _make_evidence(
                        topic="prior_error_retrospective_restatement",
                        document_id=document_id,
                        source_sha256=source_sha256,
                        report_year=report_year,
                        page_index=page_index,
                        lines=(line, candidate),
                        report_header=header,
                    )
                    if choice == _CHECKBOX_APPLICABLE:
                        return evidence, "前期会计差错的追溯重述法明确勾选适用。"
                    return evidence, None
            return None, "追溯重述法标题存在，但没有相邻的明确选择项。"
    return None, "未找到与前期会计差错更正关联的追溯重述法披露。"


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
                topic="opening_retained_earnings_adjustments_zero",
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
            has_period = any(token in text for token in ("上期", "前期", "比较数据", "比较报表", "2023年", "以前年度"))
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
                # 没有页眉不能单独构成冲突；如果文本本身确实披露了调整，
                # 上面的冲突仍保留，同时不把缺少页眉重复记成第二个冲突。
    return tuple(evidence), tuple(conflicts)


def _report_year_line(lines: tuple[_Line, ...], report_year: int) -> _Line | None:
    expected = f"{report_year}年年度报告"
    return next((line for line in lines if expected in _normalize(line.text)), None)


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
        evidence_id=f"comp-{source_sha256[:12]}-{report_year}-{topic}-p{page_index + 1}",
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


def _record_checkbox(
    evidence_item: ComparabilityEvidence | None,
    issue: str | None,
    default_missing: str,
    evidence: list[ComparabilityEvidence],
    checks: list[str],
    missing: list[str],
    conflicts: list[str],
) -> None:
    if evidence_item is not None:
        evidence.append(evidence_item)
        if issue:
            conflicts.append(issue)
        else:
            checks.append(f"{evidence_item.topic} 已从原文定位并明确勾选不适用。")
    elif issue:
        if "明确勾选适用" in issue or "勾选适用" in issue:
            conflicts.append(issue)
        else:
            missing.append(default_missing + "（" + issue + "）。")


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


def _union_bbox(boxes: tuple[PdfBBox, ...]) -> PdfBBox:
    return PdfBBox(
        x0=min(box.x0 for box in boxes),
        y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes),
        y1=max(box.y1 for box in boxes),
    )


def _bbox_dict(value: PdfBBox) -> dict[str, float]:
    return {"x0": value.x0, "y0": value.y0, "x1": value.x1, "y1": value.y1}
