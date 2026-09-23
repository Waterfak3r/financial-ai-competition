"""可追溯财务事实与提取结果。

规范数值以精确十进制字符串写入 JSON，不使用二进制浮点。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


PERIOD_TYPE_ANNUAL = "annual"
COLUMN_ROLE_CURRENT = "current"
COLUMN_ROLE_COMPARATIVE = "comparative"
RESTATEMENT_STATUS_UNKNOWN = "unknown"


def decimal_to_str(value: Decimal) -> str:
    """把有限 Decimal 写成不使用科学计数法的精确字符串。"""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("规范数值必须是有限 Decimal。")
    return format(value, "f")


def _exact_decimal_text(value: str, field_name: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} 必须是精确十进制字符串。") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} 必须是有限 Decimal。")
    return format(parsed, "f")


@dataclass(frozen=True, slots=True)
class FactHit:
    """一个命中的文字块及其原文坐标。"""

    page_number: int
    block_index: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True, slots=True)
class FinancialFact:
    """一项已定位的财务事实。比较期使用另一条事实，各自保留出处。"""

    document_id: str
    source_sha256: str
    company_id: str
    indicator_name: str
    table_name: str
    raw_value: str
    normalized_value: str
    report_year: int
    period_label: str
    period_type: str
    column_role: str
    restatement_status: str
    unit_multiplier: str
    currency: str
    statement_scope: str
    extraction_method: str
    hits: tuple[FactHit, ...]
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "normalized_value",
            _exact_decimal_text(self.normalized_value, "normalized_value"),
        )
        object.__setattr__(
            self,
            "unit_multiplier",
            _exact_decimal_text(self.unit_multiplier, "unit_multiplier"),
        )
        if self.period_type.strip() == "":
            raise ValueError("period_type 不能为空。")
        if self.column_role not in {COLUMN_ROLE_CURRENT, COLUMN_ROLE_COMPARATIVE}:
            raise ValueError("column_role 必须是 current 或 comparative。")
        if not isinstance(self.restatement_status, str) or self.restatement_status.strip() == "":
            raise ValueError("restatement_status 不能为空。")


@dataclass(frozen=True, slots=True)
class ExtractionIssue:
    """未能形成事实时的弃权原因。"""

    code: str
    message: str
    indicator_name: str | None = None
    table_name: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """一次字段提取的事实与弃权记录。"""

    facts: tuple[FinancialFact, ...]
    issues: tuple[ExtractionIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
