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


def _make_archive(root: Path, run_id: str, *, source_text: str = "source alpha") -> dict[str, object]:
    run_dir = root / "artifacts" / "runs" / run_id
    report_dir = root / "artifacts" / "reports" / run_id
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)

    pdf = pymupdf.open()
    first = pdf.new_page(width=400, height=500)
    first.insert_text((40, 80), f"TABLE {source_text} TITLE AND COLUMN HEADER")
    value_page = pdf.new_page(width=600, height=800)
    value_page.insert_text((70, 130), "INDEPENDENT VERIFIED VALUE")
    value_rect = value_page.search_for("INDEPENDENT VERIFIED VALUE")[0]
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
    assert "D:\\private\\source.pdf" not in response.text


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
