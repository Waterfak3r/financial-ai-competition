"""受控的年度演示样例来源性质登记。

性质只由完整 PDF SHA256 与登记的文档身份联合决定。文件名、路径、用户输入标签、
模型输出和评测标签都不能把来源提升为“原创模拟”或“真实披露”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)

_SYNTHETIC_USE_CONDITIONS = (
    "FINTRACE 团队原创构造的模拟材料，不对应真实公司、证券代码或真实披露。可随 FINTRACE 比赛提交件及团队演示包分发和演示；"
    "不得作为真实公司事实、投资依据、舞弊/违规标签、无舞弊标签或模型训练标签。"
)


@dataclass(frozen=True, slots=True)
class _RegisteredSource:
    sample_id: str
    source_sha256: str
    company_id: str
    company_name: str
    report_year: int
    document_id: str
    revision: str


_CONTROLLED_ANNUAL_SAMPLES = (
    _RegisteredSource(
        sample_id="sim-annual-001-2025",
        source_sha256="f193e7a497fe483d3ab70b7bde2880f715ff1127b1d8b14cc3f4fda678ba0bf7",
        company_id="SIM-ANNUAL-001",
        company_name="澄湾云算股份有限公司（虚构）",
        report_year=2025,
        document_id="sim-annual-001-2025",
        revision="1.2.0-microsoft-yahei-6.31",
    ),
    _RegisteredSource(
        sample_id="sim-annual-001-2025",
        source_sha256="a9131e1c660bd38ed80a1560fe5e3f3c8dd4a25c35b561019e04d6fa444f0d68",
        company_id="SIM-ANNUAL-001",
        company_name="澄湾云算股份有限公司（虚构）",
        report_year=2025,
        document_id="sim-annual-001-2025",
        revision="1.0.0-microsoft-yahei-6.31-superseded",
    ),
    _RegisteredSource(
        sample_id="sim-annual-001-2025",
        source_sha256="ea215df01c6c37a51ccf609b2ee3977ff12adeb423e4407ab6af39fab1961440",
        company_id="SIM-ANNUAL-001",
        company_name="澄湾云算股份有限公司（虚构）",
        report_year=2025,
        document_id="sim-annual-001-2025",
        revision="1.1.0-noto-sans-sc-2.04-superseded",
    ),
)


def resolve_sample_provenance(
    source_sha256: object,
    *,
    company_id: object,
    report_year: object,
    document_id: object,
) -> dict[str, object]:
    """按 PDF SHA、company_id 与 report_year 返回展示元数据。

    document_id 是归档生成时分配的身份，不由 PDF 字节自然携带。Web 上传会生成
    upload-<uuid>，所以它不参与样例分类；归档读取器仍独立校验 run/document/hash 的绑定。
    """

    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        return _unknown_provenance()
    digest = source_sha256.lower()
    for record in _CONTROLLED_ANNUAL_SAMPLES:
        if (
            digest == record.source_sha256
            and company_id == record.company_id
            and report_year == record.report_year
        ):
            return {
                "status": "registered_synthetic",
                "data_nature": "original_synthetic",
                "label": "原创模拟、非真实公司披露",
                "sample_id": record.sample_id,
                "revision": record.revision,
                "registered_document_id": record.document_id,
                "company_name": record.company_name,
                "source_sha256": record.source_sha256,
                "use_conditions": _SYNTHETIC_USE_CONDITIONS,
                "fraud_label": False,
                "model_training": False,
            }
    return _unknown_provenance()


def registered_sample_hashes() -> tuple[str, ...]:
    """Return registered exact hashes for validation and evidence summaries."""

    return tuple(record.source_sha256 for record in _CONTROLLED_ANNUAL_SAMPLES)


def _unknown_provenance() -> dict[str, object]:
    return {
        "status": "unconfirmed",
        "data_nature": "unconfirmed",
        "label": "来源性质未确认",
        "sample_id": None,
        "revision": None,
        "registered_document_id": None,
        "company_name": None,
        "source_sha256": None,
        "use_conditions": None,
        "fraud_label": None,
        "model_training": None,
    }
