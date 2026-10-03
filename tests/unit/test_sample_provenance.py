from __future__ import annotations

from finagent.core.sample_provenance import resolve_sample_provenance


_CURRENT_HASH = "f193e7a497fe483d3ab70b7bde2880f715ff1127b1d8b14cc3f4fda678ba0bf7"


def test_registered_sample_keeps_nature_after_web_upload_assigns_new_document_id() -> None:
    result = resolve_sample_provenance(
        _CURRENT_HASH,
        company_id="SIM-ANNUAL-001",
        report_year=2025,
        document_id="upload-7c79b6ae13dc4d8b9ea192ea995f823a",
    )

    assert result["status"] == "registered_synthetic"
    assert result["data_nature"] == "original_synthetic"
    assert result["label"] == "原创模拟、非真实公司披露"
    assert result["registered_document_id"] == "sim-annual-001-2025"
    assert result["fraud_label"] is False


def test_unknown_hash_and_identity_mismatch_never_inherit_sample_identity() -> None:
    unknown_hash = resolve_sample_provenance(
        "0" * 64,
        company_id="SIM-ANNUAL-001",
        report_year=2025,
        document_id="sim-annual-001-2025",
    )
    wrong_company = resolve_sample_provenance(
        _CURRENT_HASH,
        company_id="REAL-ISSUER-001",
        report_year=2025,
        document_id="upload-7c79b6ae13dc4d8b9ea192ea995f823a",
    )
    wrong_year = resolve_sample_provenance(
        _CURRENT_HASH,
        company_id="SIM-ANNUAL-001",
        report_year=2024,
        document_id="upload-7c79b6ae13dc4d8b9ea192ea995f823a",
    )

    for result in (unknown_hash, wrong_company, wrong_year):
        assert result["status"] == "unconfirmed"
        assert result["data_nature"] == "unconfirmed"
        assert result["label"] == "来源性质未确认"
        assert result["company_name"] is None
        assert result["use_conditions"] is None


def test_invalid_hash_shape_is_unconfirmed() -> None:
    result = resolve_sample_provenance(
        "not-a-hash",
        company_id="SIM-ANNUAL-001",
        report_year=2025,
        document_id="sim-annual-001-2025",
    )
    assert result["status"] == "unconfirmed"
