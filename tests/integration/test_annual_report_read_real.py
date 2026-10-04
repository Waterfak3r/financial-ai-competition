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
_NARRATIVE_RUN_ID = "annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce"
_NARRATIVE_EVIDENCE_ID = "nctx-5a97b1353443-p9-aaf639bcc7e460cd4dd4"


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


@pytest.mark.skipif(
    not (_ROOT / "artifacts" / "runs" / _NARRATIVE_RUN_ID / "source.pdf").is_file(),
    reason="本地未归档海天 2024 年报叙述阅读样例",
)
def test_narrative_reading_context_restores_page_nine_paragraph_without_changing_citation() -> None:
    client = TestClient(create_app(_ROOT))

    response = client.get(f"/v1/annual-analyses/{_NARRATIVE_RUN_ID}/narrative-evidences")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_sha256"] == "5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee"
    item = next(record for record in payload["items"] if record["evidence_id"] == _NARRATIVE_EVIDENCE_ID)
    assert item["pdf_page"] == 9
    assert item["raw_text"] == "展”的决心更加坚定。2024 年，公司营业收入及归属上市公司股东净利润双增长。 \n"
    assert item["bbox"] == {
        "x0": 63.82500076293945,
        "y0": 654.2445068359375,
        "x1": 441.92498779296875,
        "y1": 665.8679809570312,
    }
    reading = item["reading_context"]
    assert reading["purpose"] == "display_only"
    assert reading["source_pdf_sha256"] == payload["source_sha256"]
    assert reading["pdf_page"] == 9
    assert reading["text"] == (
        "2024 年是公司四五规划的开局首年，是承压破局的一年。面对外部环境的不确定性，公司坚守产品品质优先，"
        "坚持刀刃向内的变革，以用户至上的价值导向牵引各业务快速转型。变革有效激发了组织与人员活力，使部分工作"
        "的成效在2024 年的体感更实，也让我们对企业“长期主义发展”的决心更加坚定。2024 年，公司营业收入及归属"
        "上市公司股东净利润双增长。"
    )
    assert reading["expanded"] is True
    assert reading["limited"] is False
    assert [block["block_index"] for block in reading["source_blocks"]] == [27, 28, 29, 30]
    assert all(
        abs(reading["bbox"][key] - expected) <= 0.02
        for key, expected in {
            "x0": 63.825,
            "y0": 600.19,
            "x1": 506.49,
            "y1": 665.868,
        }.items()
    )
