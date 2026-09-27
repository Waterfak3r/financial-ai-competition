from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pymupdf
import pytest
from fastapi.testclient import TestClient

from finagent.api.app import create_app


_ROOT = Path(__file__).resolve().parents[2]
_RUN_ID = "annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42"
_SOURCE = _ROOT / "artifacts" / "runs" / _RUN_ID / "source.pdf"


@pytest.mark.skipif(not _SOURCE.is_file(), reason="本地未归档海天 2024 正式年度分析运行")
def test_read_and_preview_formal_haitian_archive() -> None:
    client = TestClient(create_app(_ROOT))

    response = client.get(f"/v1/annual-analyses/{_RUN_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["report"]["run_id"] == _RUN_ID
    assert payload["report"]["company_id"] == "603288"
    assert payload["report"]["report_year"] == 2024
    assert payload["manifest"]["source_pdf_sha256"] == payload["report"]["source_sha256"]
    evidences = payload["verification_evidences"]
    assert evidences
    evidence = evidences[0]

    preview_response = client.get(
        f"/v1/annual-analyses/{_RUN_ID}/evidence/{quote(evidence['evidence_id'], safe='')}/preview.png"
    )

    assert preview_response.status_code == 200
    assert preview_response.headers["content-type"] == "image/png"
    assert preview_response.headers["x-pdf-page"] == str(evidence["value_region"]["page"])
    assert preview_response.content.startswith(b"\x89PNG\r\n\x1a\n")
    rendered = pymupdf.Pixmap(preview_response.content)
    preview = pymupdf.open(stream=preview_response.content, filetype="png")
    assert preview.page_count == 1
    preview.close()

    source = pymupdf.open(_SOURCE)
    source_page = source[evidence["value_region"]["page"] - 1]
    bbox = evidence["value_region"]["bbox"]
    scale = rendered.width / source_page.rect.width
    sample_x = int((bbox["x0"] + (bbox["x1"] - bbox["x0"]) * 0.1) * scale)
    sample_y = int((bbox["y0"] + (bbox["y1"] - bbox["y0"]) * 0.1) * scale)
    highlighted_pixel = rendered.pixel(sample_x, sample_y)
    outside_pixel = rendered.pixel(10, 10)
    source.close()
    assert highlighted_pixel[0] > 240 and highlighted_pixel[1] > 230 and highlighted_pixel[2] < 220
    assert not (outside_pixel[0] > 240 and outside_pixel[1] > 230 and outside_pixel[2] < 220)
