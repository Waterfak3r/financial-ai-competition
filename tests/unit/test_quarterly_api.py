"""季度分析 HTTP API 端点测试。"""

from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from finagent.api.app import create_app


def test_get_latest_quarterly_analysis():
    client = TestClient(create_app())
    response = client.get("/v1/quarterly-analyses/latest")
    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == "quarterly-analysis-v1"
    assert "derived_quarters" in data
    assert "comparisons" in data
    assert len(data["comparisons"]) == 6
    assert data["target_year"] == 2024
    assert data["target_quarter"] == "Q2"


def test_run_quarterly_analysis_endpoint():
    client = TestClient(create_app())
    response = client.post("/v1/quarterly-analyses/run", json={"year": 2024, "quarter": "Q2"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in {"completed", "completed_with_abstentions"}
    assert len(data["source_documents"]) == 4
    assert len(data["source_observations"]) == 24
    assert len(data["derived_quarters"]) == 9
