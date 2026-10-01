from __future__ import annotations

import importlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from finagent.api.app import create_app

_HOST = "127.0.0.1:8000"
_DEV_HEADERS = {"Host": _HOST, "Origin": "http://127.0.0.1:5173"}
_BASE_URL = "https://model.example.invalid/v1"
_MODEL = "private-model"
_SECRET = "mock-secret-do-not-return"


@contextmanager
def _completion_server(*, status: int = 200, text: str = "OK") -> Iterator[tuple[str, list[dict[str, object]]]]:
    captured: list[dict[str, object]] = []
    body = (
        json.dumps(
            {
                "id": "mock-response-id",
                "model": "mock-model",
                "choices": [{"message": {"role": "assistant", "content": text}}],
            }
        ).encode("utf-8")
        if status == 200
        else json.dumps({"error": {"message": "provider raw response must remain private"}}).encode("utf-8")
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            captured.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": payload,
                }
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_get_put_and_delete_settings_never_return_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_BASE_URL", "https://environment.example.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "environment-secret")
    monkeypatch.setenv("MODEL_NAME", "environment-model")
    client = TestClient(create_app(tmp_path))

    initial = client.get("/v1/model-settings", headers={"Host": _HOST})
    assert initial.status_code == 200
    assert initial.headers["cache-control"] == "no-store"
    assert initial.json() == {
        "base_url": "https://environment.example.invalid/v1",
        "model_name": "environment-model",
        "api_key_configured": True,
        "configured": True,
        "source": "environment",
    }
    assert "environment-secret" not in initial.text

    saved = client.put(
        "/v1/model-settings",
        headers=_DEV_HEADERS,
        json={"base_url": _BASE_URL, "model_name": _MODEL, "api_key": _SECRET},
    )
    assert saved.status_code == 200
    assert saved.json()["source"] == "local"
    assert _SECRET not in saved.text
    assert saved.headers["cache-control"] == "no-store"

    blank_key = client.put(
        "/v1/model-settings",
        headers=_DEV_HEADERS,
        json={"base_url": _BASE_URL, "model_name": "updated-model", "api_key": ""},
    )
    assert blank_key.status_code == 200
    assert blank_key.json()["model_name"] == "updated-model"

    original = (tmp_path / ".env.model").read_bytes()
    changed_address = client.put(
        "/v1/model-settings",
        headers=_DEV_HEADERS,
        json={"base_url": "https://other.example.invalid/v1", "model_name": _MODEL, "api_key": ""},
    )
    assert changed_address.status_code == 400
    assert changed_address.json()["detail"]["code"] == "model_api_key_required_for_endpoint_change"
    assert _SECRET not in changed_address.text
    assert (tmp_path / ".env.model").read_bytes() == original

    cleared = client.delete("/v1/model-settings", headers=_DEV_HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["source"] == "environment"
    assert not (tmp_path / ".env.model").exists()


def test_invalid_json_fields_types_lengths_and_urls_do_not_echo_input(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    cases = [
        {"base_url": _BASE_URL, "model_name": _MODEL, "api_key": _SECRET, "unexpected": _SECRET},
        {"base_url": _BASE_URL, "model_name": _MODEL, "api_key": 7},
        {"base_url": _BASE_URL, "model_name": "x" * 257, "api_key": _SECRET},
        {"base_url": "https://user:password@private.example.invalid/v1", "model_name": _MODEL, "api_key": _SECRET},
    ]

    for body in cases:
        response = client.put("/v1/model-settings", headers=_DEV_HEADERS, json=body)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_model_settings"
        assert _SECRET not in response.text
        assert "private.example.invalid" not in response.text
    assert not (tmp_path / ".env.model").exists()


def test_streamed_oversized_settings_body_is_rejected_without_content_length(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    response = client.put(
        "/v1/model-settings",
        headers={**_DEV_HEADERS, "Content-Type": "application/json"},
        content=iter([b" " * (12 * 1024), b" "]),
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_model_settings"
    assert "超过允许长度" in response.json()["detail"]["message"]
    assert not (tmp_path / ".env.model").exists()


def test_host_and_origin_checks_reject_nonlocal_settings_changes(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    body = {"base_url": _BASE_URL, "model_name": _MODEL, "api_key": _SECRET}

    nonlocal_host = client.put(
        "/v1/model-settings",
        headers={"Host": "testserver", "Origin": "http://localhost:5173"},
        json=body,
    )
    foreign_origin = client.put(
        "/v1/model-settings",
        headers={"Host": _HOST, "Origin": "https://attacker.example"},
        json=body,
    )
    missing_origin = client.put(
        "/v1/model-settings",
        headers={"Host": _HOST},
        json=body,
    )
    assert nonlocal_host.status_code == 403
    assert foreign_origin.status_code == 403
    assert missing_origin.status_code == 403
    assert _SECRET not in nonlocal_host.text + foreign_origin.text + missing_origin.text
    assert not (tmp_path / ".env.model").exists()


def test_bad_saved_file_fails_closed_but_can_be_cleared_or_replaced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_BASE_URL", "https://environment.example.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "environment-secret")
    monkeypatch.setenv("MODEL_NAME", "environment-model")
    path = tmp_path / ".env.model"
    path.write_text("broken secret=" + _SECRET, encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    failed = client.get("/v1/model-settings", headers={"Host": _HOST})
    assert failed.status_code == 500
    assert failed.json()["detail"]["code"] == "model_settings_file_invalid"
    assert _SECRET not in failed.text
    assert "environment.example.invalid" not in failed.text

    repaired = client.put(
        "/v1/model-settings",
        headers=_DEV_HEADERS,
        json={"base_url": _BASE_URL, "model_name": _MODEL, "api_key": _SECRET},
    )
    assert repaired.status_code == 200
    assert repaired.json()["source"] == "local"
    assert _SECRET not in repaired.text


def test_connection_test_uses_audited_local_mock_and_does_not_save_or_leak(
    tmp_path: Path, monkeypatch
) -> None:
    audit_module = importlib.import_module("finagent.audit.audited_chat")
    monkeypatch.setattr(audit_module, "repository_runs_root", lambda: tmp_path / "artifacts" / "runs")
    client = TestClient(create_app(tmp_path))

    with _completion_server(status=200, text="provider-raw-output") as (base_url, captured):
        response = client.post(
            "/v1/model-settings/test",
            headers=_DEV_HEADERS,
            json={"base_url": base_url, "model_name": _MODEL, "api_key": _SECRET},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "code": "connection_succeeded",
        "message": "模型连接成功。",
    }
    assert _SECRET not in response.text
    assert "provider-raw-output" not in response.text
    assert len(captured) == 1
    assert captured[0]["path"] == "/v1/chat/completions"
    assert captured[0]["authorization"] == f"Bearer {_SECRET}"
    payload = captured[0]["payload"]
    assert set(payload) == {"model", "messages", "stream"}
    assert payload["messages"] == [{"role": "user", "content": "请只回复 OK。"}]
    assert not (tmp_path / ".env.model").exists()
    audit_files = list((tmp_path / "artifacts" / "runs").rglob("*.json"))
    assert audit_files
    assert all(_SECRET not in path.read_text(encoding="utf-8") for path in audit_files)
    assert all(base_url not in path.read_text(encoding="utf-8") for path in audit_files)


def test_connection_failure_hides_provider_error_and_records_safe_category(
    tmp_path: Path, monkeypatch
) -> None:
    audit_module = importlib.import_module("finagent.audit.audited_chat")
    monkeypatch.setattr(audit_module, "repository_runs_root", lambda: tmp_path / "artifacts" / "runs")
    client = TestClient(create_app(tmp_path))

    with _completion_server(status=401) as (base_url, _captured):
        response = client.post(
            "/v1/model-settings/test",
            headers=_DEV_HEADERS,
            json={"base_url": base_url, "model_name": _MODEL, "api_key": _SECRET},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "code": "model_connection_failed",
        "message": "未能连接模型服务，请检查设置后重试。",
    }
    assert _SECRET not in response.text
    assert base_url not in response.text
    assert "provider raw response" not in response.text
    records = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "artifacts" / "runs").rglob("*.json")]
    assert any(record.get("category") == "http_error" for record in records)
    assert all(_SECRET not in json.dumps(record) for record in records)
    assert all("provider raw response" not in json.dumps(record) for record in records)

