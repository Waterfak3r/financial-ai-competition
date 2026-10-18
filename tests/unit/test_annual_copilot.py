"""Unit tests for the bounded report copilot endpoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from finagent.api.app import create_app
from finagent.llm.chat_completion import ChatCompletion, TokenUsage


def test_copilot_validation_rules(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    # Invalid run ID format (contains invalid characters)
    res = client.post("/v1/annual-analyses/invalid@id/copilot", json={"question": "测试提问"})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_run_id"

    # Empty question
    res = client.post("/v1/annual-analyses/valid-run-001/copilot", json={"question": ""})
    assert res.status_code == 422

    # Missing archive
    res = client.post("/v1/annual-analyses/valid-run-001/copilot", json={"question": "测试提问"})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "run_not_found"


def test_copilot_success_with_mock(tmp_path: Path):
    from tests.unit.test_annual_report_export import _archive

    _archive(tmp_path, "copilot-run-001", mode="deterministic")
    client = TestClient(create_app(tmp_path))

    mock_completion = ChatCompletion(
        text="经核验，本期营业收入与净利润保持稳健增长，但经营现金流出现阶段性下降，建议重点核对附注中的营运资金变动明细。",
        model="mock-model",
        response_id="mock-resp-001",
        usage=TokenUsage(prompt_tokens=150, completion_tokens=45, total_tokens=195),
        provider_admitted=True,
    )

    with patch("finagent.api.annual_copilot.resolve_model_settings") as mock_settings, \
         patch("finagent.api.annual_copilot.complete_chat", return_value=mock_completion) as mock_chat:
        from finagent.core.model_settings import ModelSettings
        mock_settings.return_value = ModelSettings(
            base_url="https://api.mock.com",
            api_key="mock-key",
            model="mock-model",
        )

        res = client.post(
            "/v1/annual-analyses/copilot-run-001/copilot",
            json={"question": "深入分析现金流与净利润背离的成因"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["run_id"] == "copilot-run-001"
        assert "营运资金变动明细" in data["answer"]
        assert len(data["suggested_prompts"]) > 0
        assert mock_chat.called
