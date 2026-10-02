from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
from pathlib import Path
from urllib.parse import quote

import pymupdf
import pytest
from fastapi.testclient import TestClient

from finagent.api.app import create_app


def _make_archive(
    root: Path,
    run_id: str,
    *,
    source_text: str = "source alpha",
    include_narrative: bool = False,
    rotate_value_page: bool = False,
) -> dict[str, object]:
    run_dir = root / "artifacts" / "runs" / run_id
    report_dir = root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)

    pdf = pymupdf.open()
    first = pdf.new_page(width=400, height=500)
    first.insert_text((40, 80), f"TABLE {source_text} TITLE AND COLUMN HEADER")
    narrative_text = "Contract income follows performance."
    narrative_rect = None
    if include_narrative:
        first.insert_text((40, 200), narrative_text)
        narrative_rect = first.search_for(narrative_text)[0]
    value_page = pdf.new_page(width=600, height=800)
    value_page.insert_text((70, 130), "INDEPENDENT VERIFIED VALUE")
    value_rect = value_page.search_for("INDEPENDENT VERIFIED VALUE")[0]
    if rotate_value_page:
        value_page.set_rotation(90)
    source_pdf_bytes = pdf.tobytes()
    pdf.close()
    (run_dir / "source.pdf").write_bytes(source_pdf_bytes)
    source_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
    evidence = {
        "evidence_id": "ev-independent",
        "document_id": "doc-1",
        "source_sha256": source_sha256,
        "pdf_page": 2,
        "value_region": {
            "text": "INDEPENDENT VERIFIED VALUE",
            "page": 2,
            "bbox": {
                "x0": value_rect.x0,
                "y0": value_rect.y0,
                "x1": value_rect.x1,
                "y1": value_rect.y1,
            },
        },
        # These earlier-page coordinates must not affect the preview page.
        "column_region": {"page": 1, "bbox": {"x0": 20, "y0": 60, "x1": 200, "y1": 90}},
        "title_region": {"page": 1, "bbox": {"x0": 20, "y0": 20, "x1": 200, "y1": 40}},
    }
    legacy_evidence = {
        "evidence_id": "ev-legacy-extraction",
        "document_id": "doc-1",
        "source_sha256": source_sha256,
        "pdf_page": 2,
        "value_region": evidence["value_region"],
    }
    manifest = {
        "run_id": run_id,
        "status": "completed",
        "company_id": "603288",
        "report_year": 2024,
        "document_id": "doc-1",
        "inputs": {
            "source_pdf_original_path": "D:\\private\\source.pdf",
            "source_pdf_archive_path": f"runs/{run_id}/source.pdf",
            "source_pdf_sha256": source_sha256,
        },
        "outputs": {
            "run_dir": f"runs/{run_id}",
            "report_dir": f"reports/{run_id}",
            "report_json": f"reports/{run_id}/report.json",
        },
    }
    report = {
        "kind": "fintrace_annual_analysis_report",
        "run_id": run_id,
        "company_id": "603288",
        "report_year": 2024,
        "source_document_id": "doc-1",
        "source_sha256": source_sha256,
        "confirmed": {
            "metrics": [
                {
                    "fact_id": "fact-1",
                    "source_document_id": "doc-1",
                    "source_sha256": source_sha256,
                    "evidences": [legacy_evidence],
                    "verification_evidences": [evidence],
                    "verifications": [
                        {
                            "target_type": "financial_fact",
                            "target_id": "fact-1",
                            "status": "verified",
                            "evidence_ids": ["ev-independent"],
                        }
                    ],
                }
            ],
            "analyses": [],
        },
        "pending_review": {
            "uncited_evidences": [
                {"evidence_id": "ev-pending", "source_sha256": source_sha256}
            ]
        },
        "metadata_note": source_text,
    }
    if include_narrative:
        report["model_investigation"] = {
            "items": [
                {
                    "signal_id": "revenue_up_cash_down",
                    "status": "interpretation",
                    "narrative_evidence_ids": ["nctx-test-note"],
                }
            ],
            "narrative_evidences": [
                {
                    "evidence_id": "nctx-test-note",
                    "source_document_id": "doc-1",
                    "source_sha256": source_sha256,
                    "signal_ids": ["revenue_up_cash_down"],
                    "section_label": "financial_notes",
                    "pdf_page": 1,
                    "bbox": {
                        "x0": narrative_rect.x0,
                        "y0": narrative_rect.y0,
                        "x1": narrative_rect.x1,
                        "y1": narrative_rect.y1,
                    },
                    "raw_text": narrative_text,
                    "selection_reason": "local-term-match",
                },
                {
                    "evidence_id": "nctx-uncited",
                    "source_document_id": "doc-1",
                    "source_sha256": source_sha256,
                    "signal_ids": ["revenue_up_cash_down"],
                    "section_label": "financial_notes",
                    "pdf_page": 1,
                    "bbox": {
                        "x0": narrative_rect.x0,
                        "y0": narrative_rect.y0,
                        "x1": narrative_rect.x1,
                        "y1": narrative_rect.y1,
                    },
                    "raw_text": narrative_text,
                    "selection_reason": "local-term-match",
                },
            ],
        }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (report_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return {
        "source_pdf_bytes": source_pdf_bytes,
        "source_sha256": source_sha256,
        "report": report,
        "evidence": evidence,
    }


def test_report_api_returns_manifest_binding_and_only_verified_evidence(tmp_path: Path) -> None:
    run_id = "synthetic-report-001"
    created = _make_archive(tmp_path, run_id)
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["report"]["run_id"] == run_id
    assert payload["manifest"]["source_pdf_sha256"] == created["source_sha256"]
    report_bytes = (tmp_path / "artifacts" / "reports" / run_id / "report.json").read_bytes()
    assert payload["manifest"]["report_sha256"] == hashlib.sha256(report_bytes).hexdigest()
    assert [item["evidence_id"] for item in payload["verification_evidences"]] == ["ev-independent"]
    assert payload["narrative_evidences"]["items"] == []
    assert "D:\\private\\source.pdf" not in response.text


@pytest.mark.parametrize("invalid_mode", [[], {}, ["integrated_analysis"], 3, "future_mode"])
def test_report_api_maps_non_string_or_unknown_manifest_mode_to_unknown(
    tmp_path: Path, invalid_mode: object
) -> None:
    run_id = "synthetic-unknown-mode-001"
    _make_archive(tmp_path, run_id)
    manifest_path = tmp_path / "artifacts" / "runs" / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["mode"] = invalid_mode
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 200
    assert response.json()["manifest"]["mode"] == "unknown"


def test_preview_is_png_and_uses_value_region_page_only(tmp_path: Path) -> None:
    run_id = "synthetic-preview-001"
    created = _make_archive(tmp_path, run_id)
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-pdf-page"] == "2"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    preview = pymupdf.open(stream=response.content, filetype="png")
    assert preview.page_count == 1
    preview.close()
    assert struct.unpack(">II", response.content[16:24]) == (900, 1200)
    assert response.headers["x-report-sha256"] == hashlib.sha256(
        (tmp_path / "artifacts" / "reports" / run_id / "report.json").read_bytes()
    ).hexdigest()
    assert response.headers["x-evidence-kind"] == "verified_financial_value"
    assert response.headers["x-preview-mode"] == "full_page"
    assert json.loads(response.headers["x-preview-region"])["coordinate_space"] == "unrotated_pdf_points"


def test_narrative_index_and_preview_are_source_bound_and_separate_from_verified_allowlist(tmp_path: Path) -> None:
    run_id = "synthetic-narrative-preview-001"
    created = _make_archive(tmp_path, run_id, include_narrative=True)
    source_pdf = pymupdf.open(stream=created["source_pdf_bytes"], filetype="pdf")
    try:
        source_block = next(
            block[4]
            for block in source_pdf[0].get_text("blocks")
            if len(block) >= 5 and "Contract income follows performance." in block[4]
        )
    finally:
        source_pdf.close()
    # The archived excerpt omits only block-edge whitespace, which some
    # PyMuPDF builds return as a trailing line break in get_text("blocks").
    assert source_block.strip() == "Contract income follows performance."
    assert source_block != "Contract income follows performance."
    client = TestClient(create_app(tmp_path))

    index = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")

    assert index.status_code == 200
    payload = index.json()
    assert payload["interpretation_label"] == "原文片段定位/模型观点引用，非独立因果核验"
    assert [item["evidence_id"] for item in payload["items"]] == ["nctx-test-note"]
    item = payload["items"][0]
    assert item["raw_text"] == "Contract income follows performance."
    assert item["cited_by_signal_ids"] == ["revenue_up_cash_down"]
    assert item["preview_url"].endswith("/nctx-test-note/preview.png")

    full = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences/nctx-test-note/preview.png")
    focus = client.get(
        f"/v1/annual-analyses/{run_id}/narrative-evidences/nctx-test-note/preview.png?focus=true"
    )
    assert full.status_code == focus.status_code == 200
    assert full.headers["content-type"] == focus.headers["content-type"] == "image/png"
    assert focus.headers["x-evidence-kind"] == "narrative_context"
    assert focus.headers["x-evidence-interpretation-code"] == (
        "original_text_location_model_opinion_not_independent_causal_verification"
    )
    assert focus.headers["x-preview-mode"] == "focus"
    assert focus.headers["x-report-sha256"] == hashlib.sha256(
        (tmp_path / "artifacts" / "reports" / run_id / "report.json").read_bytes()
    ).hexdigest()
    assert json.loads(focus.headers["x-preview-region"])["coordinate_space"] == "unrotated_pdf_points"
    full_width, full_height = struct.unpack(">II", full.content[16:24])
    focus_width, focus_height = struct.unpack(">II", focus.content[16:24])
    assert focus_height < full_height
    assert focus_width * focus_height <= 12_000_000
    assert focus_width > 0

    amount_with_narrative_id = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/nctx-test-note/preview.png"
    )
    narrative_with_amount_id = client.get(
        f"/v1/annual-analyses/{run_id}/narrative-evidences/ev-independent/preview.png"
    )
    assert amount_with_narrative_id.status_code == 404
    assert amount_with_narrative_id.json()["detail"]["code"] == "evidence_not_found"
    assert narrative_with_amount_id.status_code == 404
    assert narrative_with_amount_id.json()["detail"]["code"] == "narrative_evidence_not_found"


def test_narrative_source_mismatch_and_invalid_section_label_are_safe_errors(tmp_path: Path) -> None:
    run_id = "synthetic-narrative-mismatch-001"
    _make_archive(tmp_path, run_id, include_narrative=True)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    original = json.loads(report_path.read_text(encoding="utf-8"))
    bad_text = json.loads(json.dumps(original))
    bad_text["model_investigation"]["narrative_evidences"][0]["raw_text"] = "forged disclosure"
    report_path.write_text(json.dumps(bad_text), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    mismatch = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "narrative_evidence_source_mismatch"

    bad_section = json.loads(json.dumps(original))
    bad_section["model_investigation"]["narrative_evidences"][0]["section_label"] = []
    report_path.write_text(json.dumps(bad_section), encoding="utf-8")
    malformed = client.get(f"/v1/annual-analyses/{run_id}/narrative-evidences")
    assert malformed.status_code == 422
    assert malformed.json()["detail"]["code"] == "report_invalid"


def test_focus_preview_handles_rotated_pdf_coordinates(tmp_path: Path) -> None:
    run_id = "synthetic-rotated-focus-001"
    _make_archive(tmp_path, run_id, rotate_value_page=True)
    client = TestClient(create_app(tmp_path))

    full = client.get(f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png")
    focus = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png?focus=true"
    )

    assert full.status_code == focus.status_code == 200
    assert struct.unpack(">II", full.content[16:24]) == (1200, 900)
    focus_width, focus_height = struct.unpack(">II", focus.content[16:24])
    assert 0 < focus_width < 1200
    assert 0 < focus_height < 900


def test_invalid_traversal_run_id_is_rejected(tmp_path: Path) -> None:
    _make_archive(tmp_path, "safe-run")
    client = TestClient(create_app(tmp_path))

    response = client.get("/v1/annual-analyses/" + quote("../outside", safe=""))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_run_id"


def test_unknown_run_id_returns_not_found(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    response = client.get("/v1/annual-analyses/valid-but-missing-run")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "run_not_found"


def test_unknown_or_forged_evidence_id_returns_not_found(tmp_path: Path) -> None:
    run_id = "synthetic-forgery-001"
    _make_archive(tmp_path, run_id)
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-forged/preview.png"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "evidence_not_found"


@pytest.mark.parametrize(
    ("target_type", "target_id"),
    [("calculation", "fact-1"), ("financial_fact", "some-other-object")],
)
def test_wrong_verified_target_cannot_unlock_an_evidence(
    tmp_path: Path,
    target_type: str,
    target_id: str,
) -> None:
    run_id = "synthetic-wrong-target-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    verification = report["confirmed"]["metrics"][0]["verifications"][0]
    verification["target_type"] = target_type
    verification["target_id"] = target_id
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "evidence_not_found"


def test_mismatched_pdf_page_and_value_region_page_are_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-mismatched-page-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["confirmed"]["metrics"][0]["verification_evidences"][0]["pdf_page"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(
        f"/v1/annual-analyses/{run_id}/evidence/ev-independent/preview.png"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "evidence_region_invalid"


def test_unsupported_report_kind_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-wrong-kind-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["kind"] = "legacy_annual_precheck"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "report_invalid"


def test_source_hash_corruption_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-corruption-001"
    _make_archive(tmp_path, run_id)
    source = tmp_path / "artifacts" / "runs" / run_id / "source.pdf"
    source_bytes = source.read_bytes()
    source.write_bytes(source_bytes[:-1] + bytes([source_bytes[-1] ^ 0x01]))
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_sha256_mismatch"


def test_report_from_another_source_is_rejected(tmp_path: Path) -> None:
    run_a = "synthetic-source-a"
    run_b = "synthetic-source-b"
    _make_archive(tmp_path, run_a, source_text="report A")
    _make_archive(tmp_path, run_b, source_text="report B")
    report_b = tmp_path / "artifacts" / "reports" / run_b / "report.json"
    report_a = tmp_path / "artifacts" / "reports" / run_a / "report.json"
    report_a.write_bytes(report_b.read_bytes())
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_a}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "report_identity_mismatch"


def test_report_source_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-report-hash-001"
    _make_archive(tmp_path, run_id)
    report_path = tmp_path / "artifacts" / "reports" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["source_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "report_identity_mismatch"


def test_pdf_from_another_source_is_rejected(tmp_path: Path) -> None:
    run_a = "synthetic-pdf-a"
    run_b = "synthetic-pdf-b"
    _make_archive(tmp_path, run_a, source_text="pdf A")
    _make_archive(tmp_path, run_b, source_text="pdf B")
    source_a = tmp_path / "artifacts" / "runs" / run_a / "source.pdf"
    source_b = tmp_path / "artifacts" / "runs" / run_b / "source.pdf"
    source_a.write_bytes(source_b.read_bytes())
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_a}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_sha256_mismatch"


def test_run_directory_symlink_is_rejected(tmp_path: Path) -> None:
    run_id = "synthetic-symlink-001"
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    (outside / run_id).mkdir(parents=True)
    runs_root = tmp_path / "artifacts" / "runs"
    runs_root.mkdir(parents=True)
    link = runs_root / run_id
    try:
        link.symlink_to(outside / run_id, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("当前平台不允许创建目录 symlink")
        # A Windows junction is a reparse point and can be created without the
        # elevated privilege required for a directory symlink.
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside / run_id)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not link.exists():
            pytest.skip("当前环境不能创建目录 symlink 或 junction")
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/v1/annual-analyses/{run_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "archive_path_invalid"
