"""用本地 HTTP 服务验收 Chat Completions 连接器，不访问云端。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from finagent.core.model_settings import ModelConfigError, ModelSettings, load_model_settings
from finagent.llm.chat_completion import ChatMessage, ModelResponseError, complete_chat

_SECRET = "sk-test-secret-should-not-leak"
_USER_TEXT = "请只回复一个词"


class _CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captured = {  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        }
        status, payload = self.server.behavior(self.path)  # type: ignore[attr-defined]
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def _serve(behavior):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    server.behavior = behavior  # type: ignore[attr-defined]
    server.captured = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _settings(port: int, path: str = "/v1") -> ModelSettings:
    return ModelSettings(
        base_url=f"http://127.0.0.1:{port}{path}",
        api_key=_SECRET,
        model="example-model",
        timeout_seconds=2,
    )


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage("system", "你是助手"),
        ChatMessage("user", _USER_TEXT),
        ChatMessage("assistant", "上一轮回复"),
    ]


def test_posts_shared_chat_completions_fields_and_reads_text() -> None:
    def behavior(_path: str):
        return 200, {
            "id": "chatcmpl-local",
            "model": "example-model-returned",
            "choices": [{"message": {"role": "assistant", "content": "好的"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    server, thread = _serve(behavior)
    try:
        result = complete_chat(_messages(), _settings(server.server_port))
    finally:
        server.shutdown()
        thread.join(timeout=2)
    captured = server.captured  # type: ignore[attr-defined]
    assert captured["path"] == "/v1/chat/completions"
    assert captured["authorization"] == f"Bearer {_SECRET}"
    body = json.loads(captured["body"].decode("utf-8"))
    assert body == {
        "model": "example-model",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": _USER_TEXT},
            {"role": "assistant", "content": "上一轮回复"},
        ],
        "stream": False,
    }
    assert set(body) == {"model", "messages", "stream"}
    assert result.text == "好的"
    assert result.model == "example-model-returned"
    assert result.response_id == "chatcmpl-local"
    assert result.usage is not None
    assert result.usage.total_tokens == 4


def test_appends_chat_path_once_when_base_url_has_trailing_slash() -> None:
    def behavior(_path: str):
        return 200, {
            "id": "id-1",
            "model": "m",
            "choices": [{"message": {"content": "文本"}}],
        }

    server, thread = _serve(behavior)
    try:
        result = complete_chat(
            [ChatMessage("user", "你好")],
            _settings(server.server_port, "/compatible-mode/v1/"),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert server.captured["path"] == "/compatible-mode/v1/chat/completions"  # type: ignore[attr-defined]
    assert result.usage is None


def test_missing_config_and_http_error_do_not_reveal_secret_or_prompt() -> None:
    with pytest.raises(ModelConfigError, match="MODEL_BASE_URL") as missing:
        load_model_settings({"MODEL_API_KEY": _SECRET, "MODEL_NAME": "example-model"})
    assert _SECRET not in str(missing.value)
    assert _USER_TEXT not in str(missing.value)

    def behavior(_path: str):
        return 401, {"error": {"message": _SECRET, "echo": _USER_TEXT}}

    server, thread = _serve(behavior)
    try:
        with pytest.raises(ModelResponseError, match="HTTP 401") as failed:
            complete_chat([ChatMessage("user", _USER_TEXT)], _settings(server.server_port))
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert _SECRET not in str(failed.value)
    assert _USER_TEXT not in str(failed.value)


@pytest.mark.parametrize(
    "base_url",
    ["ftp://example.com/v1", "http:///missing-host", "https://user:secret@example.com/v1", "https://example.com/v1/chat/completions"],
)
def test_rejects_unusable_base_url_without_calling_network(base_url: str) -> None:
    with pytest.raises(ModelConfigError) as error:
        load_model_settings(
            {"MODEL_BASE_URL": base_url, "MODEL_API_KEY": _SECRET, "MODEL_NAME": "example-model"}
        )
    assert _SECRET not in str(error.value)


def test_non_text_or_missing_content_is_rejected() -> None:
    def behavior(_path: str):
        return 200, {
            "id": "id-2",
            "model": "m",
            "choices": [{"message": {"content": [{"type": "text", "text": _SECRET}]}}],
        }

    server, thread = _serve(behavior)
    try:
        with pytest.raises(ModelResponseError, match="不是文本") as failed:
            complete_chat([ChatMessage("user", "你好")], _settings(server.server_port))
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert _SECRET not in str(failed.value)


def test_timeout_message_has_no_secret() -> None:
    def behavior(_path: str):
        import time

        time.sleep(1.5)
        return 200, {"id": "late", "model": "m", "choices": [{"message": {"content": "晚了"}}]}

    server, thread = _serve(behavior)
    try:
        with pytest.raises(ModelResponseError, match="超时") as failed:
            complete_chat(
                [ChatMessage("user", _USER_TEXT)],
                _settings(server.server_port),
                timeout_seconds=0.2,
            )
    finally:
        server.shutdown()
        thread.join(timeout=3)
    assert _SECRET not in str(failed.value)
    assert _USER_TEXT not in str(failed.value)
