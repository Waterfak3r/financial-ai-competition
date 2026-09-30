"""M3 规则 2 的专属跨表语义映射核验。

“归属于上市公司股东”来自主要会计数据表，不能因为数值已核验就自动视为
合并利润表口径。本模块重新读取第 7 页直接披露的归母净利润，并要求其与
合并利润表两年归母净利润逐期完全相等。返回的映射 proof 仅在签发进程有效。
当前重定位逻辑只针对海天味业 603288 的 2024 年报物理第 7 页版式，不是跨公司
或跨报告期的通用映射。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from finagent.schemas.financial_fact import decimal_to_str
from finagent.schemas.financial_fact_v2 import FinancialFactV2, SourceRegion, VerificationResult
from finagent.schemas.text_pdf import PdfBBox

_SIGNING_KEY = secrets.token_bytes(32)
_KEY_METRIC = "net_profit_parent_ex_nonrecurring"
_KEY_LABEL = "归属于上市公司股东的扣除非经常性损益的净利润"
_PARENT_METRIC = "net_profit_parent"
_PARENT_LABEL = "归属于母公司股东的净利润"
_DIRECT_LABEL = "归属于上市公司股东的净利润"
_YEAR_HEADER_RE = re.compile(r"^(20\d{2})年$")
_AMOUNT_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_CURRENCY_UNKNOWN = {"未披露", "未知", "unknown", "undisclosed"}


@dataclass(frozen=True, slots=True)
class ParentProfitMappingEvidence:
    """从主要会计数据表第 7 页重新定位的一年金额证据。"""

    year: int
    row_label: str
    amount_raw: str
    amount_normalized: str
    page: int
    row_region: SourceRegion
    value_region: SourceRegion
    year_header_region: SourceRegion
    unit_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "year": self.year,
            "row_label": self.row_label,
            "amount_raw": self.amount_raw,
            "amount_normalized": self.amount_normalized,
            "page": self.page,
            "row_region": _region_dict(self.row_region),
            "value_region": _region_dict(self.value_region),
            "year_header_region": _region_dict(self.year_header_region),
            "unit_text": self.unit_text,
        }


@dataclass(frozen=True, slots=True)
class ParentProfitSemanticMapping:
    """绑定同源两年事实与第 7 页交叉核对证据的进程内 proof。"""

    mapping_id: str
    company_id: str
    document_id: str
    source_sha256: str
    report_year: int
    key_fact_ids: tuple[str, str]
    key_fact_values: tuple[str, str]
    parent_fact_ids: tuple[str, str]
    parent_fact_values: tuple[str, str]
    direct_disclosure_values: tuple[str, str]
    evidence: tuple[ParentProfitMappingEvidence, ParentProfitMappingEvidence]
    _nonce: str = field(default="", repr=False, compare=False)
    _signature: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        """返回审计内容；序列化副本不能重新变为可用 proof。"""

        return {
            "mapping_id": self.mapping_id,
            "company_id": self.company_id,
            "document_id": self.document_id,
            "source_sha256": self.source_sha256,
            "report_year": self.report_year,
            "key_fact_ids": list(self.key_fact_ids),
            "key_fact_values": list(self.key_fact_values),
            "parent_fact_ids": list(self.parent_fact_ids),
            "parent_fact_values": list(self.parent_fact_values),
            "direct_disclosure_values": list(self.direct_disclosure_values),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class ParentProfitMappingCheck:
    """映射核验的 proof 或失败原因。"""

    proof: ParentProfitSemanticMapping | None
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _WordLine:
    words: tuple[tuple, ...]

    @property
    def text(self) -> str:
        return "".join(str(word[4]) for word in self.words)

    @property
    def y0(self) -> float:
        return min(float(word[1]) for word in self.words)

    @property
    def y1(self) -> float:
        return max(float(word[3]) for word in self.words)


def verify_parent_profit_semantic_mapping(
    pdf_path: str | Path,
    *,
    key_facts: Sequence[FinancialFactV2],
    parent_facts: Sequence[FinancialFactV2],
    verifications: Sequence[VerificationResult],
    report_year: int,
) -> ParentProfitMappingCheck:
    """对两年扣非事实定义唯一的 ``scope=unknown`` 规则专属语义映射。

    要求 key-data 事实具有准确 metric_id 和完整原文标签；扣非事实和合并利润表
    归母净利润均须有唯一的独立 verified 结果。随后从原始 PDF 物理第 7 页按
    年度表头重定位“归属于上市公司股东的净利润”，并与利润表事实逐年精确对值。
    """

    if type(report_year) is not int or not 1900 <= report_year <= 2100:
        return _failed("report_year 必须是 1900 到 2100 之间的整数。")
    key_pair, key_issues = _fact_pair(key_facts, report_year, role="key")
    parent_pair, parent_issues = _fact_pair(parent_facts, report_year, role="parent")
    issues = [*key_issues, *parent_issues]
    if issues:
        return ParentProfitMappingCheck(None, tuple(dict.fromkeys(issues)))
    assert key_pair is not None and parent_pair is not None

    if report_year != 2024 or key_pair[0].company_id != "603288":
        return _failed("规则二语义映射目前仅支持海天味业 603288 的 2024 年报第 7 页版式。")

    aligned = (*key_pair, *parent_pair)
    issues.extend(_check_shared_metadata(aligned))
    issues.extend(_check_verifications(aligned, verifications))
    if issues:
        return ParentProfitMappingCheck(None, tuple(dict.fromkeys(issues)))

    source_sha256 = key_pair[0].source_sha256.lower()
    try:
        payload = Path(pdf_path).read_bytes()
    except OSError:
        return _failed("无法读取原始 PDF，不能建立跨表语义映射。")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual_sha256, source_sha256):
        return _failed("原始 PDF SHA256 与规则二事实来源不一致。")

    try:
        import pymupdf

        document = pymupdf.open(stream=payload, filetype="pdf")
    except Exception:
        return _failed("原始文件无法作为 PDF 打开，不能建立跨表语义映射。")
    try:
        if len(document) < 7:
            return _failed("原始 PDF 不足 7 页，无法重读主要会计数据表。")
        evidence, page_issues = _locate_direct_parent_profit(document[6], report_year)
    finally:
        document.close()
    if page_issues:
        return ParentProfitMappingCheck(None, page_issues)
    assert evidence is not None

    direct_values = tuple(item.amount_normalized for item in evidence)
    parent_values = tuple(item.normalized_value for item in parent_pair)
    if not _same_decimal_values(direct_values, parent_values):
        return _failed(
            "第 7 页“归属于上市公司股东的净利润”与合并利润表归母净利润至少一年金额不一致。"
        )

    proof = ParentProfitSemanticMapping(
        mapping_id=f"m3-parent-profit-map-{secrets.token_hex(12)}",
        company_id=key_pair[0].company_id,
        document_id=key_pair[0].source_document_id,
        source_sha256=actual_sha256,
        report_year=report_year,
        key_fact_ids=(key_pair[0].fact_id, key_pair[1].fact_id),
        key_fact_values=(key_pair[0].normalized_value, key_pair[1].normalized_value),
        parent_fact_ids=(parent_pair[0].fact_id, parent_pair[1].fact_id),
        parent_fact_values=(parent_pair[0].normalized_value, parent_pair[1].normalized_value),
        direct_disclosure_values=direct_values,
        evidence=evidence,
        _nonce=secrets.token_hex(24),
    )
    proof = _signed(proof)
    return ParentProfitMappingCheck(proof, ())


def is_verified_parent_profit_semantic_mapping(value: object) -> bool:
    """只接受本进程中由上述原文核验函数签发、内容未被改动的 proof。"""

    if not isinstance(value, ParentProfitSemanticMapping) or not value._nonce or not value._signature:
        return False
    expected = hmac.new(_SIGNING_KEY, _signed_payload(value), hashlib.sha256).hexdigest()
    return hmac.compare_digest(value._signature, expected)


def _fact_pair(
    facts: Sequence[FinancialFactV2], report_year: int, *, role: str
) -> tuple[tuple[FinancialFactV2, FinancialFactV2] | None, tuple[str, ...]]:
    if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
        return None, (f"规则二{role}事实必须是 FinancialFactV2 序列。",)
    items = tuple(facts)
    if any(not isinstance(item, FinancialFactV2) for item in items):
        return None, (f"规则二{role}事实包含非 FinancialFactV2 对象。",)
    metric = _KEY_METRIC if role == "key" else _PARENT_METRIC
    label = _KEY_LABEL if role == "key" else _PARENT_LABEL
    statement = "key_financial_data" if role == "key" else "income_statement"
    scope = "unknown" if role == "key" else "consolidated"
    expected_years = (report_year, report_year - 1)
    if len(items) != 2:
        return None, (f"规则二{role}事实必须恰有报告年和上一年度两条记录，实际为 {len(items)} 条。",)
    by_year: dict[int, list[FinancialFactV2]] = defaultdict(list)
    issues: list[str] = []
    for fact in items:
        if fact.metric_id != metric:
            issues.append(f"规则二{role}事实 metric_id 必须精确为 {metric}。")
        if fact.label_raw != label:
            issues.append(f"规则二{role}事实原文标签必须完整且精确为“{label}”。")
        if fact.statement_type != statement or fact.scope != scope:
            issues.append(f"规则二{role}事实的 statement_type/scope 与专属语义映射要求不一致。")
        if fact.period_type != "duration" or fact.frequency != "annual":
            issues.append(f"规则二{role}事实必须是 annual duration。")
        if fact.report_year != report_year or fact.period_end is None:
            issues.append(f"规则二{role}事实缺少正确报告年或期间结束日。")
            continue
        year = fact.period_end.year
        if year not in expected_years:
            issues.append(f"规则二{role}事实期间必须是 {report_year} 或 {report_year - 1} 年。")
            continue
        expected_start = date(year, 1, 1)
        expected_end = date(year, 12, 31)
        expected_role = "current" if year == report_year else "comparative"
        if fact.period_start != expected_start or fact.period_end != expected_end:
            issues.append(f"规则二{role}事实必须覆盖完整自然年 {year}。")
        if fact.comparison_role != expected_role:
            issues.append(f"规则二{role}事实 {year} 年比较角色必须是 {expected_role}。")
        if fact.restatement_status == "restated":
            issues.append(f"规则二{role}事实 {year} 年标记为已追溯调整，不能使用。")
        by_year[year].append(fact)
    for year in expected_years:
        count = len(by_year.get(year, ()))
        if count != 1:
            word = "缺少" if count == 0 else "重复"
            issues.append(f"规则二{role}事实 {year} 年{word}：必须恰有一条。")
    if issues:
        return None, tuple(dict.fromkeys(issues))
    return (by_year[report_year][0], by_year[report_year - 1][0]), ()


def _check_shared_metadata(facts: Sequence[FinancialFactV2]) -> tuple[str, ...]:
    issues: list[str] = []
    for field_name, label in (
        ("company_id", "公司"),
        ("source_document_id", "来源文档"),
        ("source_sha256", "来源 SHA256"),
        ("currency", "币种"),
        ("unit", "单位"),
        ("unit_multiplier", "单位倍率"),
    ):
        values = {getattr(item, field_name) for item in facts}
        if len(values) != 1:
            issues.append(f"规则二跨表映射事实的{label}不一致。")
    first = facts[0]
    if first.unit is None or not first.unit.strip():
        issues.append("规则二跨表映射缺少明确单位。")
    if first.currency.strip().casefold() in _CURRENCY_UNKNOWN:
        issues.append("规则二跨表映射缺少明确币种。")
    return tuple(issues)


def _check_verifications(
    facts: Sequence[FinancialFactV2], verifications: Sequence[VerificationResult]
) -> tuple[str, ...]:
    if isinstance(verifications, (str, bytes)) or not isinstance(verifications, Sequence):
        return ("规则二缺少独立事实核验序列。",)
    valid_verifications = tuple(item for item in verifications if isinstance(item, VerificationResult))
    issues: list[str] = []
    for fact in facts:
        matches = [item for item in valid_verifications if item.target_id == fact.fact_id]
        if len(matches) != 1:
            word = "缺少" if not matches else "存在重复"
            issues.append(f"规则二事实 {fact.fact_id} {word}唯一独立核验结果。")
            continue
        result = matches[0]
        if result.target_type != "financial_fact" or result.status != "verified" or not result.evidence_ids:
            issues.append(f"规则二事实 {fact.fact_id} 未由独立原文核验为 verified。")
    if len(valid_verifications) != len(verifications):
        issues.append("规则二核验序列包含非 VerificationResult 对象。")
    return tuple(issues)


def _locate_direct_parent_profit(page, report_year: int):
    page_text = page.get_text("text")
    compact_text = _compact(page_text).replace("（", "(").replace("）", ")")
    if "七、近三年主要会计数据和财务指标" not in compact_text:
        return None, ("PDF 第 7 页缺少预期的主要会计数据章节标题。",)
    if "(一)主要会计数据" not in compact_text:
        return None, ("PDF 第 7 页缺少唯一可定位的“主要会计数据”表。",)
    unit_lines = [
        line.strip()
        for line in page_text.splitlines()
        if "单位" in line and "币种" in line
    ]
    if len(unit_lines) != 1 or not _has_cny_yuan_unit(unit_lines[0]):
        return None, ("PDF 第 7 页主要会计数据表缺少唯一的“单位：元、币种：人民币”披露。",)
    unit_text = unit_lines[0]

    words = tuple(page.get_text("words"))
    header_words: dict[int, tuple] = {}
    for year in (report_year, report_year - 1):
        candidates = [
            word
            for word in words
            if 230 <= float(word[1]) <= 275
            and _YEAR_HEADER_RE.fullmatch(str(word[4]))
            and int(_YEAR_HEADER_RE.fullmatch(str(word[4])).group(1)) == year
        ]
        if len(candidates) != 1:
            return None, (f"PDF 第 7 页无法唯一定位 {year} 年金额列的年份表头。",)
        header_words[year] = candidates[0]

    # 主表数据区位于年份表头下方、年末资产表头上方。
    data_header_y = min(float(item[1]) for item in header_words.values())
    label_lines = _visual_lines(
        tuple(
            word
            for word in words
            if float(word[0]) < 170 and data_header_y + 10 < float(word[1]) < 405
        )
    )
    row_candidates: list[tuple[tuple[_WordLine, ...], str]] = []
    for start in range(len(label_lines)):
        collected: list[_WordLine] = []
        for line in label_lines[start : start + 3]:
            if collected and line.y0 - collected[-1].y1 > 25:
                break
            collected.append(line)
            if _compact("".join(item.text for item in collected)) == _DIRECT_LABEL:
                row_candidates.append((tuple(collected), "".join(item.text for item in collected)))
                break
            if not _DIRECT_LABEL.startswith(_compact("".join(item.text for item in collected))):
                break
    if len(row_candidates) != 1:
        return None, (
            "PDF 第 7 页无法唯一定位完整原文行“归属于上市公司股东的净利润”。",
        )

    label_group, row_label = row_candidates[0]
    row_y0 = min(line.y0 for line in label_group)
    row_y1 = max(line.y1 for line in label_group)
    amounts_by_year: dict[int, tuple[str, tuple]] = {}
    for year, header in header_words.items():
        header_center = (float(header[0]) + float(header[2])) / 2
        candidates = []
        for word in words:
            if not _AMOUNT_RE.fullmatch(str(word[4])):
                continue
            word_y0, word_y1 = float(word[1]), float(word[3])
            center = (float(word[0]) + float(word[2])) / 2
            if row_y0 - 2 <= word_y0 and word_y1 <= row_y1 + 2 and abs(center - header_center) <= 25:
                candidates.append((str(word[4]), word))
        if len(candidates) != 1:
            return None, (f"PDF 第 7 页完整原文行无法唯一定位 {year} 年金额列。",)
        amounts_by_year[year] = candidates[0]

    evidence: list[ParentProfitMappingEvidence] = []
    for year in (report_year, report_year - 1):
        raw_value, value_word = amounts_by_year[year]
        try:
            normalized = _amount(raw_value)
        except (InvalidOperation, ValueError):
            return None, (f"PDF 第 7 页 {year} 年归母净利润金额无法解析为 Decimal。",)
        label_words = tuple(word for line in label_group for word in line.words)
        evidence.append(
            ParentProfitMappingEvidence(
                year=year,
                row_label=row_label,
                amount_raw=raw_value,
                amount_normalized=normalized,
                page=7,
                row_region=_region(label_words, 7),
                value_region=_region((value_word,), 7),
                year_header_region=_region((header_words[year],), 7),
                unit_text=unit_text,
            )
        )
    return tuple(evidence), ()


def _visual_lines(words: Sequence[tuple]) -> tuple[_WordLine, ...]:
    ordered = sorted(words, key=lambda word: (float(word[1]), float(word[0])))
    lines: list[list[tuple]] = []
    for word in ordered:
        if not lines or abs(float(word[1]) - float(lines[-1][0][1])) > 2.5:
            lines.append([word])
        else:
            lines[-1].append(word)
    return tuple(_WordLine(tuple(line)) for line in lines)


def _region(words: Sequence[tuple], page: int) -> SourceRegion:
    return SourceRegion(
        text=" ".join(str(word[4]) for word in words),
        page=page,
        bbox=PdfBBox(
            x0=min(float(word[0]) for word in words),
            y0=min(float(word[1]) for word in words),
            x1=max(float(word[2]) for word in words),
            y1=max(float(word[3]) for word in words),
        ),
    )


def _has_cny_yuan_unit(line: str) -> bool:
    compact = _compact(line).replace(":", "：")
    return bool(re.search(r"单位：元", compact) and re.search(r"币种：人民币", compact))


def _amount(value: str) -> str:
    return decimal_to_str(Decimal(value.replace(",", "")))


def _same_decimal_values(left: Sequence[str], right: Sequence[str]) -> bool:
    try:
        return len(left) == len(right) and all(Decimal(a) == Decimal(b) for a, b in zip(left, right, strict=True))
    except (InvalidOperation, ValueError):
        return False


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _region_dict(region: SourceRegion) -> dict[str, object]:
    return {
        "text": region.text,
        "page": region.page,
        "bbox": {
            "x0": region.bbox.x0,
            "y0": region.bbox.y0,
            "x1": region.bbox.x1,
            "y1": region.bbox.y1,
        },
    }


def _signed(value: ParentProfitSemanticMapping) -> ParentProfitSemanticMapping:
    signature = hmac.new(_SIGNING_KEY, _signed_payload(value), hashlib.sha256).hexdigest()
    return ParentProfitSemanticMapping(
        mapping_id=value.mapping_id,
        company_id=value.company_id,
        document_id=value.document_id,
        source_sha256=value.source_sha256,
        report_year=value.report_year,
        key_fact_ids=value.key_fact_ids,
        key_fact_values=value.key_fact_values,
        parent_fact_ids=value.parent_fact_ids,
        parent_fact_values=value.parent_fact_values,
        direct_disclosure_values=value.direct_disclosure_values,
        evidence=value.evidence,
        _nonce=value._nonce,
        _signature=signature,
    )


def _signed_payload(value: ParentProfitSemanticMapping) -> bytes:
    payload = value.to_dict()
    payload["nonce"] = value._nonce
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _failed(*messages: str) -> ParentProfitMappingCheck:
    return ParentProfitMappingCheck(None, tuple(messages))
