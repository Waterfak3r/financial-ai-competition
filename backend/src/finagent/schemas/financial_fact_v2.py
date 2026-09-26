"""FinancialFact v2、表格证据和尚未执行的核验/计算/主张类型。

本模块只定义结构和旧事实适配，不提取、不核验、不生成报告。
规范数值沿用精确十进制字符串。未知用 None，并写入 limitations。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from finagent.schemas.financial_fact import FinancialFact, decimal_to_str
from finagent.schemas.text_pdf import COORDINATE_SYSTEM, PdfBBox

SCHEMA_VERSION_V2 = 2

PERIOD_INSTANT: Literal["instant"] = "instant"
PERIOD_DURATION: Literal["duration"] = "duration"
FREQUENCY_ANNUAL: Literal["annual"] = "annual"

SCOPE_CONSOLIDATED: Literal["consolidated"] = "consolidated"
SCOPE_PARENT: Literal["parent"] = "parent"
SCOPE_UNKNOWN: Literal["unknown"] = "unknown"

ROLE_CURRENT: Literal["current"] = "current"
ROLE_COMPARATIVE: Literal["comparative"] = "comparative"
ROLE_UNKNOWN: Literal["unknown"] = "unknown"

RESTATEMENT_NOT: Literal["not_restated"] = "not_restated"
RESTATEMENT_YES: Literal["restated"] = "restated"
RESTATEMENT_UNKNOWN: Literal["unknown"] = "unknown"

VERIFIED: Literal["verified"] = "verified"
CONFLICT: Literal["conflict"] = "conflict"
INSUFFICIENT: Literal["insufficient_evidence"] = "insufficient_evidence"

CLAIM_FACT: Literal["fact"] = "fact"
CLAIM_CALCULATION: Literal["calculation"] = "calculation"
CLAIM_INFERENCE: Literal["inference"] = "inference"
CLAIM_HYPOTHESIS: Literal["hypothesis"] = "hypothesis"

CALC_SUCCEEDED: Literal["succeeded"] = "succeeded"
CALC_FAILED: Literal["failed"] = "failed"

NON_RECURRING_LABEL = "披露的非经常性损益合计"

_PERIOD_TYPES = {PERIOD_INSTANT, PERIOD_DURATION}
_SCOPES = {SCOPE_CONSOLIDATED, SCOPE_PARENT, SCOPE_UNKNOWN}
_ROLES = {ROLE_CURRENT, ROLE_COMPARATIVE, ROLE_UNKNOWN}
_RESTATEMENTS = {RESTATEMENT_NOT, RESTATEMENT_YES, RESTATEMENT_UNKNOWN}
_VERIFICATION_STATUSES = {VERIFIED, CONFLICT, INSUFFICIENT}
_CLAIM_TYPES = {CLAIM_FACT, CLAIM_CALCULATION, CLAIM_INFERENCE, CLAIM_HYPOTHESIS}
_CALC_STATUSES = {CALC_SUCCEEDED, CALC_FAILED}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

# 已知年度流量指标才标 duration。annual 本身不自动变成 duration。
_FLOW_METRICS = {
    "营业收入": ("revenue", "income_statement"),
    "归属于母公司股东的净利润": ("net_profit_parent", "income_statement"),
    "经营活动产生的现金流量净额": ("operating_cash_flow", "cash_flow_statement"),
    NON_RECURRING_LABEL: ("non_recurring_total", "non_recurring"),
}
_SCOPE_TEXT = {"合并": SCOPE_CONSOLIDATED, "母公司": SCOPE_PARENT}


def exact_decimal_text(value: str, field_name: str) -> str:
    """接受十进制文本，拒绝无穷和非数字，输出不用科学计数法。"""

    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} 必须是精确十进制字符串。")
    return decimal_to_str(Decimal(value)) if _is_exact_decimal(value) else _reject_decimal(field_name)


def _is_exact_decimal(value: str) -> bool:
    try:
        parsed = Decimal(value)
    except Exception:
        return False
    return parsed.is_finite()


def _reject_decimal(field_name: str) -> str:
    raise ValueError(f"{field_name} 必须是有限的精确十进制字符串。")


@dataclass(frozen=True, slots=True)
class SourceRegion:
    """原文中的一块区域。page 是 PDF 1-based 页，可与其他 region 不同。"""

    text: str
    page: int
    bbox: PdfBBox

    def __post_init__(self) -> None:
        _require_text(self.text, "SourceRegion.text")
        _require_page(self.page, "SourceRegion.page")
        _require_bbox(self.bbox)


@dataclass(frozen=True, slots=True)
class FinancialFactV2:
    """一条 v2 财务事实。事实自身年份看 period_end，report_year 是报告年份。"""

    fact_id: str
    metric_id: str
    company_id: str
    label_raw: str
    raw_value: str
    normalized_value: str
    currency: str
    unit_multiplier: str
    unit: str | None
    report_year: int | None
    period_start: date | None
    period_end: date | None
    period_type: Literal["instant", "duration"] | None
    frequency: Literal["annual"] | None
    statement_type: str | None
    scope: Literal["consolidated", "parent", "unknown"]
    comparison_role: Literal["current", "comparative", "unknown"]
    restatement_status: Literal["not_restated", "restated", "unknown"]
    source_document_id: str
    source_sha256: str
    evidence_ids: tuple[str, ...]
    extraction_method: str
    limitations: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION_V2

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION_V2:
            raise ValueError("schema_version 必须是 2。")
        _require_id(self.fact_id, "fact_id")
        _require_id(self.metric_id, "metric_id")
        _require_text(self.company_id, "company_id")
        _require_text(self.label_raw, "label_raw")
        _require_text(self.raw_value, "raw_value")
        _require_text(self.currency, "currency")
        _require_text(self.source_document_id, "source_document_id")
        _require_sha(self.source_sha256)
        _require_text(self.extraction_method, "extraction_method")
        object.__setattr__(self, "normalized_value", exact_decimal_text(self.normalized_value, "normalized_value"))
        object.__setattr__(self, "unit_multiplier", exact_decimal_text(self.unit_multiplier, "unit_multiplier"))
        _optional_text(self.unit, "unit")
        _optional_text(self.statement_type, "statement_type")
        if self.report_year is not None:
            _require_year(self.report_year, "report_year")
        if self.period_type is not None and self.period_type not in _PERIOD_TYPES:
            raise ValueError("period_type 只能是 instant、duration 或空。")
        if self.frequency is not None and self.frequency != FREQUENCY_ANNUAL:
            raise ValueError("frequency 只能是 annual 或空。")
        if self.scope not in _SCOPES:
            raise ValueError("scope 只能是 consolidated、parent 或 unknown。")
        if self.comparison_role not in _ROLES:
            raise ValueError("comparison_role 只能是 current、comparative 或 unknown。")
        if self.restatement_status not in _RESTATEMENTS:
            raise ValueError("restatement_status 只能是 not_restated、restated 或 unknown。")
        _check_period(self.period_type, self.period_start, self.period_end)
        object.__setattr__(self, "evidence_ids", _id_tuple(self.evidence_ids, "evidence_ids"))
        object.__setattr__(self, "limitations", _text_tuple(self.limitations, "limitations"))
        if self.label_raw == NON_RECURRING_LABEL and self.scope != SCOPE_UNKNOWN:
            raise ValueError("披露的非经常性损益合计的 scope 必须保持 unknown。")

    def to_dict(self) -> dict[str, Any]:
        return _public_dict(self)


@dataclass(frozen=True, slots=True)
class TableCellEvidence:
    """一个表格单元格证据。行、列、表题、单位和值可以落在不同页。"""

    evidence_id: str
    document_id: str
    source_sha256: str
    pdf_page: int
    printed_page: int | None
    table_title: str | None
    row_label: str | None
    column_label: str | None
    value_raw: str | None
    value_normalized: str | None
    unit: str | None
    currency: str | None
    period_start: date | None
    period_end: date | None
    period_type: Literal["instant", "duration"] | None
    value_region: SourceRegion | None
    row_region: SourceRegion | None
    column_region: SourceRegion | None
    title_region: SourceRegion | None
    unit_region: SourceRegion | None
    extraction_method: str
    surrounding_text: str | None
    legacy_text_region: SourceRegion | None = None
    limitations: tuple[str, ...] = ()
    coordinate_system: str = COORDINATE_SYSTEM

    def __post_init__(self) -> None:
        _require_id(self.evidence_id, "evidence_id")
        _require_text(self.document_id, "document_id")
        _require_sha(self.source_sha256)
        _require_page(self.pdf_page, "pdf_page")
        if self.printed_page is not None:
            _require_page(self.printed_page, "printed_page")
        _require_text(self.extraction_method, "extraction_method")
        if self.coordinate_system != COORDINATE_SYSTEM:
            raise ValueError("coordinate_system 必须是 pymupdf_page_top_left。")
        if self.value_region is None:
            if self.value_raw is not None or self.value_normalized is not None:
                raise ValueError("没有 value_region 时不能填写 value_raw 或 value_normalized。")
            note = "此证据没有 value_region，只保留文字块候选，不能用于 verified。"
            if note not in self.limitations:
                object.__setattr__(self, "limitations", (*self.limitations, note))
        else:
            if not isinstance(self.value_raw, str) or self.value_raw.strip() == "":
                raise ValueError("有 value_region 时必须填写 value_raw。")
            if not contains_complete_raw_token(self.value_region.text, self.value_raw):
                raise ValueError("value_region 原文必须包含完整 raw_value token。")
            if self.value_normalized is None:
                raise ValueError("有 value_region 时必须填写 value_normalized。")
            object.__setattr__(
                self,
                "value_normalized",
                exact_decimal_text(self.value_normalized, "value_normalized"),
            )
        for name in ("table_title", "row_label", "column_label", "unit", "currency", "surrounding_text"):
            _optional_text(getattr(self, name), name)
        if self.period_type is not None and self.period_type not in _PERIOD_TYPES:
            raise ValueError("period_type 只能是 instant、duration 或空。")
        _check_period(self.period_type, self.period_start, self.period_end)
        if self.value_region is not None and self.value_region.page != self.pdf_page:
            raise ValueError("value_region.page 必须与 pdf_page 相同。")
        object.__setattr__(self, "limitations", _text_tuple(self.limitations, "limitations"))

    def to_dict(self) -> dict[str, Any]:
        return _public_dict(self)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """核验结果容器。本模块不执行核验，调用方不得把未检查写成 verified。"""

    verification_id: str
    target_type: str
    target_id: str
    status: Literal["verified", "conflict", "insufficient_evidence"]
    checks: tuple[str, ...]
    conflicts: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    verified_at: datetime

    def __post_init__(self) -> None:
        _require_id(self.verification_id, "verification_id")
        _require_text(self.target_type, "target_type")
        _require_id(self.target_id, "target_id")
        if self.status not in _VERIFICATION_STATUSES:
            raise ValueError("status 只能是 verified、conflict 或 insufficient_evidence。")
        if not isinstance(self.verified_at, datetime):
            raise ValueError("verified_at 必须是 datetime。")
        object.__setattr__(self, "checks", _text_tuple(self.checks, "checks"))
        object.__setattr__(self, "conflicts", _text_tuple(self.conflicts, "conflicts"))
        object.__setattr__(self, "evidence_ids", _id_tuple(self.evidence_ids, "evidence_ids"))
        object.__setattr__(self, "limitations", _text_tuple(self.limitations, "limitations"))
        if self.status == VERIFIED and self.evidence_ids == ():
            raise ValueError("verified 必须引用 evidence_ids，不能空证通过。")

    def to_dict(self) -> dict[str, Any]:
        return _public_dict(self)


@dataclass(frozen=True, slots=True)
class CalculationResult:
    """确定性计算的结果容器。本模块不计算公式。"""

    calculation_id: str
    formula_id: str
    input_fact_ids: tuple[str, ...]
    formula_expression: str
    output_value: str | None
    unit: str | None
    status: Literal["succeeded", "failed"]
    failure_reason: str | None
    rule_version: str

    def __post_init__(self) -> None:
        _require_id(self.calculation_id, "calculation_id")
        _require_id(self.formula_id, "formula_id")
        _require_text(self.formula_expression, "formula_expression")
        _require_text(self.rule_version, "rule_version")
        if self.status not in _CALC_STATUSES:
            raise ValueError("status 只能是 succeeded 或 failed。")
        object.__setattr__(self, "input_fact_ids", _id_tuple(self.input_fact_ids, "input_fact_ids"))
        _optional_text(self.unit, "unit")
        if self.status == CALC_SUCCEEDED:
            if self.output_value is None or self.failure_reason is not None:
                raise ValueError("succeeded 必须有 output_value，且 failure_reason 为空。")
            object.__setattr__(self, "output_value", exact_decimal_text(self.output_value, "output_value"))
        else:
            if self.output_value is not None or not isinstance(self.failure_reason, str) or self.failure_reason.strip() == "":
                raise ValueError("failed 必须有 failure_reason，且不能带 output_value。")

    def to_dict(self) -> dict[str, Any]:
        return _public_dict(self)


@dataclass(frozen=True, slots=True)
class Claim:
    """分开存放事实、计算、推论和假设。新建主张默认不是已核验。"""

    claim_id: str
    claim_type: Literal["fact", "calculation", "inference", "hypothesis"]
    text: str
    supporting_fact_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    calculation_ids: tuple[str, ...]
    verification_status: Literal["verified", "conflict", "insufficient_evidence"]
    limitations: tuple[str, ...] = ()
    alternative_explanations: tuple[str, ...] = ()
    follow_up_items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id(self.claim_id, "claim_id")
        if self.claim_type not in _CLAIM_TYPES:
            raise ValueError("claim_type 只能是 fact、calculation、inference 或 hypothesis。")
        _require_text(self.text, "text")
        if self.verification_status not in _VERIFICATION_STATUSES:
            raise ValueError("verification_status 只能是 verified、conflict 或 insufficient_evidence。")
        object.__setattr__(self, "supporting_fact_ids", _id_tuple(self.supporting_fact_ids, "supporting_fact_ids"))
        object.__setattr__(
            self,
            "supporting_evidence_ids",
            _id_tuple(self.supporting_evidence_ids, "supporting_evidence_ids"),
        )
        object.__setattr__(self, "calculation_ids", _id_tuple(self.calculation_ids, "calculation_ids"))
        object.__setattr__(self, "limitations", _text_tuple(self.limitations, "limitations"))
        object.__setattr__(
            self,
            "alternative_explanations",
            _text_tuple(self.alternative_explanations, "alternative_explanations"),
        )
        object.__setattr__(self, "follow_up_items", _text_tuple(self.follow_up_items, "follow_up_items"))
        if self.verification_status == VERIFIED and self.supporting_evidence_ids == ():
            raise ValueError("verified 主张必须引用 supporting_evidence_ids。")

    def to_dict(self) -> dict[str, Any]:
        return _public_dict(self)


def adapt_legacy_financial_fact(
    fact: FinancialFact,
    *,
    report_year: int,
    fact_id: str | None = None,
) -> tuple[FinancialFactV2, tuple[TableCellEvidence, ...]]:
    """把旧 FinancialFact 显式改写成 v2。

    report_year 是报告年份。旧事实上的 report_year 是金额所属年份，写入 period_end。
    没有行、列或追溯调整证据时保持未知，不生成 verified。
    """

    if not isinstance(fact, FinancialFact):
        raise TypeError("只能适配 FinancialFact。")
    _require_year(report_year, "report_year")
    known = _FLOW_METRICS.get(fact.indicator_name)
    limitations: list[str] = list(fact.limitations)
    if known is None:
        metric_id = "unknown"
        statement_type = None
        period_type = None
        period_start = None
        period_end = None
        frequency = None
        limitations.append("未知指标，不根据 annual 推断 instant 或 duration。")
    else:
        metric_id, statement_type = known
        if fact.period_type != "annual":
            period_type = None
            period_start = None
            period_end = None
            frequency = None
            limitations.append("旧 period_type 不是 annual，不推断完整自然年。")
        else:
            period_type = PERIOD_DURATION
            period_start = date(fact.report_year, 1, 1)
            period_end = date(fact.report_year, 12, 31)
            frequency = FREQUENCY_ANNUAL
            limitations.append("按已知流量指标标为 duration，不是因为 period_type 写成了 annual。")
    if fact.indicator_name == NON_RECURRING_LABEL:
        scope = SCOPE_UNKNOWN
        limitations.append("披露的非经常性损益合计不映射为合并或母公司口径。")
    else:
        scope = _SCOPE_TEXT.get(fact.statement_scope, SCOPE_UNKNOWN)
        if scope == SCOPE_UNKNOWN:
            limitations.append(f"旧口径“{fact.statement_scope}”不能映射为 consolidated 或 parent。")
    role = fact.column_role if fact.column_role in {ROLE_CURRENT, ROLE_COMPARATIVE} else ROLE_UNKNOWN
    if fact.restatement_status == "unknown":
        restatement = RESTATEMENT_UNKNOWN
        limitations.append("旧事实没有追溯调整证据，restatement_status 保持 unknown。")
    else:
        restatement = RESTATEMENT_UNKNOWN
        limitations.append("旧 restatement_status 不是已确认的追溯调整结论，保持 unknown。")
    if fact.report_year != report_year and role == ROLE_CURRENT:
        limitations.append("旧事实年份与报告年份不同，事实年份以 period_end 为准。")
    resolved_id = fact_id or f"{fact.document_id}:{metric_id}:{fact.report_year}:{role}"
    evidence = tuple(_legacy_hit_evidence(fact, resolved_id, index, hit) for index, hit in enumerate(fact.hits))
    limitations.append("旧文字块只作来源候选，不确认行、列、表题或单位，不视为已核验。")
    if not any(item.value_region is not None for item in evidence):
        limitations.append("没有任何旧文字块包含完整 raw_value token，这些证据不能用于 verified。")
    adapted = FinancialFactV2(
        fact_id=resolved_id,
        metric_id=metric_id,
        company_id=fact.company_id,
        label_raw=fact.indicator_name,
        raw_value=fact.raw_value,
        normalized_value=fact.normalized_value,
        currency=fact.currency,
        unit_multiplier=fact.unit_multiplier,
        unit=None,
        report_year=report_year,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
        frequency=frequency,
        statement_type=statement_type,
        scope=scope,
        comparison_role=role,
        restatement_status=restatement,
        source_document_id=fact.document_id,
        source_sha256=fact.source_sha256,
        evidence_ids=tuple(item.evidence_id for item in evidence),
        extraction_method=fact.extraction_method,
        limitations=tuple(dict.fromkeys(limitations)),
    )
    return adapted, evidence


def assert_evidence_can_support_verified(evidences: Sequence[TableCellEvidence]) -> None:
    """verified 至少要有一条带 value_region 的证据。纯文字候选不能充数。"""

    if isinstance(evidences, TableCellEvidence) or not isinstance(evidences, Sequence):
        raise TypeError("evidences 必须是 TableCellEvidence 序列。")
    if not any(isinstance(item, TableCellEvidence) and item.value_region is not None for item in evidences):
        raise ValueError("没有 value_region 的证据不能用于 verified。")


def _legacy_hit_evidence(fact: FinancialFact, fact_id: str, index: int, hit: Any) -> TableCellEvidence:
    region = SourceRegion(
        text=hit.text,
        page=hit.page_number,
        bbox=PdfBBox(x0=hit.x0, y0=hit.y0, x1=hit.x1, y1=hit.y1),
    )
    has_value = contains_complete_raw_token(hit.text, fact.raw_value)
    return TableCellEvidence(
        evidence_id=f"{fact_id}:hit:{index}",
        document_id=fact.document_id,
        source_sha256=fact.source_sha256,
        pdf_page=hit.page_number,
        printed_page=None,
        table_title=None,
        row_label=None,
        column_label=None,
        value_raw=fact.raw_value if has_value else None,
        value_normalized=fact.normalized_value if has_value else None,
        unit=None,
        currency=None,
        period_start=None,
        period_end=None,
        period_type=None,
        value_region=region if has_value else None,
        row_region=None,
        column_region=None,
        title_region=None,
        unit_region=None,
        extraction_method=fact.extraction_method,
        surrounding_text=hit.text,
        legacy_text_region=region,
        limitations=("旧文字块只作来源候选，不确认行、列、表题或单位。",),
    )


def contains_complete_raw_token(text: str, raw_value: str) -> bool:
    """判断原文是否包含完整 raw_value token。

    粘连在更长数字里的片段不算。token 内部允许空白；同一行的空格可以连着负号、
    括号和千分位，换行则断开前缀。
    """

    if not isinstance(text, str) or not isinstance(raw_value, str):
        return False
    compact = re.sub(r"\s+", "", raw_value)
    if compact == "":
        return False
    body = r"\s*".join(re.escape(char) for char in compact)
    return any(
        _token_boundary_before(text, match.start()) and _token_boundary_after(text, match.end())
        for match in re.finditer(body, text)
    )


_BEFORE_CONTINUES_AMOUNT = set("0123456789,，.．-－−(（")
_AFTER_CONTINUES_AMOUNT = set("0123456789,，.．)）")
_PREFIX_ACROSS_SPACE = set(",，.．-－−(（")
_SUFFIX_ACROSS_SPACE = set(",，.．)）")


def _token_boundary_before(text: str, index: int) -> bool:
    if index <= 0:
        return True
    previous = text[index - 1]
    if previous in "\n\r":
        return True
    if previous not in " \t":
        return previous not in _BEFORE_CONTINUES_AMOUNT
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    if cursor < 0 or text[cursor] in "\n\r":
        return True
    return text[cursor] not in _PREFIX_ACROSS_SPACE


def _token_boundary_after(text: str, index: int) -> bool:
    if index >= len(text):
        return True
    following = text[index]
    if following in "\n\r":
        return True
    if following not in " \t":
        return following not in _AFTER_CONTINUES_AMOUNT
    cursor = index
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    if cursor >= len(text) or text[cursor] in "\n\r":
        return True
    return text[cursor] not in _SUFFIX_ACROSS_SPACE


def _check_period(period_type: str | None, start: date | None, end: date | None) -> None:
    for value, name in ((start, "period_start"), (end, "period_end")):
        if value is not None and not isinstance(value, date):
            raise ValueError(f"{name} 必须是日期。")
        if isinstance(value, datetime):
            raise ValueError(f"{name} 必须是日期，不能是 datetime。")
    if period_type is None:
        if start is not None or end is not None:
            raise ValueError("period_type 为空时不能填写期间日期。")
        return
    if end is None:
        raise ValueError("instant 和 duration 都要有 period_end。")
    if period_type == PERIOD_INSTANT:
        if start is not None and start != end:
            raise ValueError("instant 的 period_start 必须为空或等于 period_end。")
        return
    if start is None:
        raise ValueError("duration 必须有 period_start。")
    if start > end:
        raise ValueError("period_start 不能晚于 period_end。")


def _require_id(value: object, field_name: str) -> None:
    if not isinstance(value, str) or value.strip() == "" or any(char.isspace() for char in value):
        raise ValueError(f"{field_name} 必须是不含空白的标识。")


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} 不能为空。")


def _optional_text(value: object, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} 必须是非空字符串或空。")


def _require_year(value: object, field_name: str) -> None:
    if type(value) is not int or value < 1900 or value > 2100:
        raise ValueError(f"{field_name} 必须是 1900 到 2100 的整数。")


def _require_page(value: object, field_name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} 必须是从 1 开始的整数。")


def _require_sha(value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("source_sha256 必须是 64 位十六进制字符串。")


def _require_bbox(bbox: object) -> None:
    if not isinstance(bbox, PdfBBox):
        raise ValueError("bbox 必须是 PdfBBox。")
    for name in ("x0", "y0", "x1", "y1"):
        number = getattr(bbox, name)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("bbox 坐标必须是有限数字。")
        if not Decimal(str(number)).is_finite():
            raise ValueError("bbox 坐标必须是有限数字。")
    if bbox.x1 <= bbox.x0 or bbox.y1 <= bbox.y0:
        raise ValueError("bbox 必须有正的宽度和高度。")


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} 必须是字符串序列。")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or item.strip() == "":
            raise ValueError(f"{field_name} 中的每一项都必须是非空字符串。")
        items.append(item)
    return tuple(items)


def _id_tuple(value: object, field_name: str) -> tuple[str, ...]:
    items = _text_tuple(value, field_name)
    for item in items:
        _require_id(item, field_name)
    return items


def _public_dict(value: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in getattr(value, "__dataclass_fields__"):
        payload[name] = _jsonable(getattr(value, name))
    return payload


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _public_dict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def dumps_v2(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value.to_dict(), ensure_ascii=False, indent=indent)
