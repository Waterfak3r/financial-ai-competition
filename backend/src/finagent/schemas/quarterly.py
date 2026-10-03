"""独立的季度累计观察、派生季度和比较结果类型。

这些类型刻意不修改年度 FinancialFactV2。季度/半年/前三季/全年源值采用
year-to-date 口径，季度流量只能从相邻累计值差分；时点值另有类型，不能差分。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any, Literal

from finagent.schemas.financial_fact_v2 import TableCellEvidence, exact_decimal_text

Quarter = Literal["Q1", "Q2", "Q3", "Q4"]
YtdPeriod = Literal["Q1", "H1", "9M", "FY"]
ObservationBasis = Literal["year_to_date", "instant"]
ColumnRole = Literal["current", "comparative"]
Relation = Literal["year_over_year", "quarter_over_quarter"]
VerificationStatus = Literal["verified", "conflict", "insufficient_evidence"]

METRIC_STATEMENTS: dict[str, tuple[str, str]] = {
    "revenue": ("income_statement", "flow"),
    "net_profit_parent": ("income_statement", "flow"),
    "operating_cash_flow": ("cash_flow_statement", "flow"),
}

PERIOD_ORDER: dict[str, int] = {"Q1": 1, "H1": 2, "9M": 3, "FY": 4}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
QUARTER_SOURCE_PERIOD: dict[str, tuple[str, str | None]] = {
    "Q1": ("Q1", None),
    "Q2": ("H1", "Q1"),
    "Q3": ("9M", "H1"),
    "Q4": ("FY", "9M"),
}


@dataclass(frozen=True, slots=True)
class QuarterlySourceDocument:
    document_id: str
    company_id: str
    source_pdf: str
    source_sha256: str
    source_url: str
    publication_date: date
    report_year: int
    report_period: YtdPeriod
    nature: str
    use_conditions: str

    def __post_init__(self) -> None:
        for name in ("document_id", "company_id", "source_pdf", "source_url", "nature", "use_conditions"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空文本。")
        for name in ("document_id", "company_id"):
            identifier = getattr(self, name)
            if _SAFE_IDENTIFIER.fullmatch(identifier) is None or ".." in identifier:
                raise ValueError(f"{name} 必须使用安全 ASCII 标识，长度不超过 121，且不能包含 ..。")
        if len(self.source_sha256) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in self.source_sha256):
            raise ValueError("source_sha256 必须是 64 位十六进制文本。")
        if type(self.report_year) is not int or not 1900 <= self.report_year <= 2100:
            raise ValueError("report_year 必须是 1900 到 2100 的整数。")
        if self.report_period not in PERIOD_ORDER:
            raise ValueError("report_period 必须是 Q1/H1/9M/FY。")
        if not isinstance(self.publication_date, date):
            raise ValueError("publication_date 必须是日期。")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True, slots=True)
class CumulativeObservation:
    observation_id: str
    metric_id: str
    company_id: str
    report_year: int
    report_period: YtdPeriod
    observation_year: int
    column_role: ColumnRole
    basis: ObservationBasis
    statement_type: str
    scope: str
    unit_multiplier: str
    evidence: TableCellEvidence
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("observation_id", "metric_id", "company_id", "statement_type", "scope"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 不能为空。")
        if self.metric_id not in METRIC_STATEMENTS:
            raise ValueError("当前季度切片只支持 revenue、net_profit_parent、operating_cash_flow。")
        if type(self.report_year) is not int or type(self.observation_year) is not int:
            raise ValueError("report_year 和 observation_year 必须是整数。")
        if self.report_period not in PERIOD_ORDER:
            raise ValueError("report_period 必须是 Q1/H1/9M/FY。")
        if self.column_role not in {"current", "comparative"}:
            raise ValueError("column_role 必须是 current 或 comparative。")
        if self.basis not in {"year_to_date", "instant"}:
            raise ValueError("basis 必须是 year_to_date 或 instant。")
        if self.column_role == "current" and self.observation_year != self.report_year:
            raise ValueError("current observation 的 observation_year 必须等于 report_year。")
        if self.column_role == "comparative" and self.observation_year != self.report_year - 1:
            raise ValueError("comparative observation 必须属于上一年度。")
        if self.basis == "year_to_date":
            expected = _ytd_dates(self.observation_year, self.report_period)
            if (self.evidence.period_start, self.evidence.period_end) != expected:
                raise ValueError("累计 observation 的原文期间必须与 report_period 对应。")
            if self.evidence.period_type != "duration":
                raise ValueError("累计 observation 的证据 period_type 必须是 duration。")
        elif self.evidence.period_type != "instant":
            raise ValueError("instant observation 的证据 period_type 必须是 instant。")
        if self.evidence.document_id == "" or self.evidence.source_sha256 == "":
            raise ValueError("证据必须绑定来源文档和 SHA256。")
        if self.evidence.value_normalized is None:
            raise ValueError("季度源观察必须有规范数值。")
        object.__setattr__(self, "unit_multiplier", exact_decimal_text(self.unit_multiplier, "unit_multiplier"))
        if isinstance(self.limitations, str) or not all(isinstance(x, str) and x.strip() for x in self.limitations):
            raise ValueError("limitations 必须是非空文本序列。")

    @property
    def source_document_id(self) -> str:
        return self.evidence.document_id

    @property
    def source_sha256(self) -> str:
        return self.evidence.source_sha256

    @property
    def value(self) -> Decimal:
        assert self.evidence.value_normalized is not None
        return Decimal(self.evidence.value_normalized)

    @property
    def currency(self) -> str | None:
        return self.evidence.currency

    @property
    def unit(self) -> str | None:
        return self.evidence.unit

    @property
    def period_start(self) -> date | None:
        return self.evidence.period_start

    @property
    def period_end(self) -> date | None:
        return self.evidence.period_end

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True, slots=True)
class DerivedQuarter:
    derived_id: str
    metric_id: str
    company_id: str
    year: int
    quarter: Quarter
    normalized_value: str
    currency: str | None
    unit: str | None
    unit_multiplier: str
    statement_type: str
    scope: str
    formula_id: str
    formula_expression: str
    input_observation_ids: tuple[str, ...]
    input_source_document_ids: tuple[str, ...]
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.quarter not in {"Q1", "Q2", "Q3", "Q4"}:
            raise ValueError("quarter 必须是 Q1/Q2/Q3/Q4。")
        object.__setattr__(self, "normalized_value", exact_decimal_text(self.normalized_value, "normalized_value"))
        object.__setattr__(self, "unit_multiplier", exact_decimal_text(self.unit_multiplier, "unit_multiplier"))
        if not self.input_observation_ids or not self.input_source_document_ids:
            raise ValueError("派生季度必须保留源 observation 和文档标识。")

    @property
    def value(self) -> Decimal:
        return Decimal(self.normalized_value)

    @property
    def period_start(self) -> date:
        return date(self.year, ((int(self.quarter[1]) - 1) * 3) + 1, 1)

    @property
    def period_end(self) -> date:
        return _quarter_end(self.year, self.quarter)

    def to_dict(self) -> dict[str, Any]:
        payload = _plain(self)
        payload["period_start"] = self.period_start.isoformat()
        payload["period_end"] = self.period_end.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class QuarterlyComparison:
    comparison_id: str
    metric_id: str
    relation: Relation
    current_id: str
    prior_id: str
    current_value: str
    prior_value: str
    difference: str | None
    growth_rate: str | None
    status: Literal["calculated", "failed"]
    failure_reason: str | None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("current_value", "prior_value"):
            object.__setattr__(self, name, exact_decimal_text(getattr(self, name), name))
        if self.difference is not None:
            object.__setattr__(self, "difference", exact_decimal_text(self.difference, "difference"))
        if self.growth_rate is not None:
            object.__setattr__(self, "growth_rate", exact_decimal_text(self.growth_rate, "growth_rate"))
        if self.status == "calculated" and self.failure_reason is not None:
            raise ValueError("calculated comparison 不能带 failure_reason。")
        if self.status == "failed" and (self.failure_reason is None or self.difference is not None):
            raise ValueError("failed comparison 必须说明原因且不能带 difference。")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True, slots=True)
class QuarterlyVerification:
    verification_id: str
    target_type: str
    target_id: str
    status: VerificationStatus
    checks: tuple[str, ...]
    conflicts: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    verified_at: datetime
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True, slots=True)
class QuarterlyProof:
    """审计可序列化的 proof 载体；只有当前进程签发的同一对象可授权后续核验。"""

    proof_id: str
    proof_type: str
    target_id: str
    source_sha256s: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    binding_sha256: str
    issued_at: datetime
    status: Literal["verified", "insufficient_evidence", "conflict"]

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _ytd_dates(year: int, period: str) -> tuple[date, date]:
    ends = {"Q1": (3, 31), "H1": (6, 30), "9M": (9, 30), "FY": (12, 31)}
    month, day = ends[period]
    return date(year, 1, 1), date(year, month, day)


def _quarter_end(year: int, quarter: str) -> date:
    return {"Q1": date(year, 3, 31), "Q2": date(year, 6, 30), "Q3": date(year, 9, 30), "Q4": date(year, 12, 31)}[quarter]


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value
