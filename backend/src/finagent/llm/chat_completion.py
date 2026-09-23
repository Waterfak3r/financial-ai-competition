"""供应方中立的同步 Chat Completions 文本调用。

请求只包含 model、messages 和 stream=false。不发送财报文件，也不接智能体接口。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from finagent.core.model_settings import ModelSettings

ChatRole = Literal["system", "user", "assistant"]


class ModelResponseError(Exception):
    """调用失败或响应不可用。消息不包含密钥和请求正文。"""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条纯文本对话消息。"""

    role: ChatRole
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("消息角色只能是 system、user 或 assistant。")
        if not isinstance(self.content, str):
            raise ValueError("消息内容必须是文本。")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """供应商返回的用量。缺失的计数保持为空。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    """一次非流式文本响应。"""

    text: str
    model: str
    response_id: str
    usage: TokenUsage | None = None


def complete_chat(
    messages: Sequence[ChatMessage],
    settings: ModelSettings,
    *,
    timeout_seconds: float | None = None,
) -> ChatCompletion:
    """向 settings 的根地址追加 /chat/completions 并等待文本响应。"""

    timeout = settings.timeout_seconds if timeout_seconds is None else timeout_seconds
    if timeout <= 0:
        raise ModelResponseError("超时时间必须大于 0。")
    payload = _request_payload(messages, settings.model)
    request = Request(
        settings.chat_completions_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        raise ModelResponseError(f"模型服务返回 HTTP {exc.code}。") from None
    except TimeoutError:
        raise ModelResponseError("模型服务请求超时。") from None
    except URLError:
        raise ModelResponseError("无法连接模型服务。") from None
    return _parse_response(raw)


def _request_payload(messages: Sequence[ChatMessage], model: str) -> dict[str, object]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        raise ValueError("至少需要一条文本消息。")
    prepared = []
    for message in messages:
        if not isinstance(message, ChatMessage):
            raise ValueError("消息必须是 ChatMessage。")
        prepared.append({"role": message.role, "content": message.content})
    return {"model": model, "messages": prepared, "stream": False}


def _parse_response(raw: bytes) -> ChatCompletion:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ModelResponseError("模型响应不是 JSON。") from None
    if not isinstance(payload, dict):
        raise ModelResponseError("模型响应不是 JSON 对象。")
    text = _text_content(payload)
    model = payload.get("model")
    response_id = payload.get("id")
    if not isinstance(model, str) or model.strip() == "":
        raise ModelResponseError("模型响应缺少模型标识。")
    if not isinstance(response_id, str) or response_id.strip() == "":
        raise ModelResponseError("模型响应缺少 ID。")
    return ChatCompletion(text=text, model=model, response_id=response_id, usage=_usage(payload))


def _text_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelResponseError("模型响应没有文本内容。")
    message = choices[0].get("message")
    if not isinstance(message, dict) or "content" not in message:
        raise ModelResponseError("模型响应没有文本内容。")
    content = message.get("content")
    if not isinstance(content, str):
        raise ModelResponseError("模型响应的内容不是文本。")
    if content.strip() == "":
        raise ModelResponseError("模型响应没有文本内容。")
    return content


def _usage(payload: dict[str, object]) -> TokenUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None

    def count(name: str) -> int | None:
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    usage = TokenUsage(
        prompt_tokens=count("prompt_tokens"),
        completion_tokens=count("completion_tokens"),
        total_tokens=count("total_tokens"),
    )
    if usage.prompt_tokens is None and usage.completion_tokens is None and usage.total_tokens is None:
        return None
    return usage


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None
