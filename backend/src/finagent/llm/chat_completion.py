"""供应方中立的同步 Chat Completions 文本调用。

请求只包含 model、messages 和 stream=false。不发送财报文件，也不接智能体接口。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import base64
import uuid
import threading
import queue
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from pathlib import Path

from finagent.core.model_settings import ModelSettings
from finagent.core.model_call_ledger import ModelCallAdmissionError, reserve_provider_attempt

ChatRole = Literal["system", "user", "assistant"]


class ModelResponseError(Exception):
    """调用失败或响应不可用。消息不包含密钥和请求正文。"""

    def __init__(
        self,
        message: str,
        *,
        category: str = "call_failed",
        status_code: int | None = None,
        provider_admitted: bool = False,
        provider_response_returned: bool = False,
        admission_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.provider_admitted = provider_admitted
        self.provider_response_returned = provider_response_returned
        self.admission_id = admission_id


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
    provider_admitted: bool = False
    admission_id: str | None = None


def complete_chat(
    messages: Sequence[ChatMessage],
    settings: ModelSettings,
    *,
    timeout_seconds: float | None = None,
    call_context: dict[str, object] | None = None,
    abort_event: object | None = None,
) -> ChatCompletion:
    """向 settings 的根地址追加 /chat/completions 并等待文本响应。"""

    timeout = settings.timeout_seconds if timeout_seconds is None else timeout_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ModelResponseError("超时时间必须大于 0。", category="invalid_timeout")
    deadline = time.monotonic() + float(timeout)
    if _call_cancelled() or _abort_event_is_set(abort_event):
        raise ModelResponseError("模型调用已取消。", category="cancelled")
    payload = _request_payload(messages, settings.model)
    if time.monotonic() >= deadline:
        raise ModelResponseError("模型调用总时限已到。", category="timeout")
    try:
        admission_id = reserve_provider_attempt(
            settings.base_url,
            settings.model,
            call_context=call_context,
            deadline_monotonic=deadline,
        )
    except ModelCallAdmissionError as exc:
        raise ModelResponseError(
            "模型请求在联网前被共享调用账本拒绝。",
            category=exc.code,
            provider_admitted=False,
        ) from None
    if admission_id is None:
        admission_id = str(uuid.uuid4())

    request_payload = {
        "url": settings.chat_completions_url(),
        "api_key": settings.api_key,
        "model": settings.model,
        "messages": payload["messages"],
        "timeout_seconds": max(0.001, deadline - time.monotonic()),
        "admission_id": admission_id,
    }
    command = [sys.executable, "-m", "finagent.llm.chat_worker"]
    process: subprocess.Popen[bytes] | None = None
    admitted = admission_id is not None
    persisted_admission = bool(
        os.environ.get("FINTRACE_MODEL_CALL_LEDGER_CONFIG") or os.environ.get("FINTRACE_CALL_GATE_DB")
    )
    admitted = persisted_admission
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[4],
            env=_worker_environment(),
        )
        # A persisted ledger reservation is the admission point. Without a
        # configured goal ledger, successful worker creation is the point.
        if not persisted_admission:
            admitted = True
        assert process.stdin is not None
        writer_errors: queue.Queue[BaseException] = queue.Queue(maxsize=1)
        output_result: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)
        writer = threading.Thread(
            target=_write_worker_input,
            args=(process, json.dumps(request_payload, ensure_ascii=False).encode("utf-8"), writer_errors),
            daemon=True,
            name="fintrace-chat-worker-stdin",
        )
        assert process.stdout is not None
        reader = threading.Thread(
            target=_read_worker_output,
            args=(process.stdout, output_result),
            daemon=True,
            name="fintrace-chat-worker-stdout",
        )
        writer.start()
        reader.start()
        while process.poll() is None:
            if _call_cancelled() or _abort_event_is_set(abort_event):
                _stop_worker(process)
                raise ModelResponseError(
                    "模型调用已取消。",
                    category="cancelled",
                    provider_admitted=admitted,
                    admission_id=admission_id,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_worker(process)
                raise ModelResponseError(
                    "模型服务请求超时。",
                    category="timeout",
                    provider_admitted=admitted,
                    admission_id=admission_id,
                )
            if not writer_errors.empty():
                _stop_worker(process)
                raise ModelResponseError(
                    "无法输送模型服务请求。", category="worker_error",
                    provider_admitted=admitted, admission_id=admission_id,
                ) from None
            time.sleep(min(0.02, remaining))
        writer.join(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
        if writer.is_alive():
            _stop_worker(process)
            raise ModelResponseError(
                "模型服务请求超时。", category="timeout", provider_admitted=admitted,
                admission_id=admission_id,
            )
        if not writer_errors.empty():
            raise ModelResponseError(
                "无法输送模型服务请求。", category="worker_error",
                provider_admitted=admitted, admission_id=admission_id,
            ) from None
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            _stop_worker(process)
            raise ModelResponseError(
                "模型服务请求超时。", category="timeout", provider_admitted=admitted,
                admission_id=admission_id,
            ) from None
        if output_result.empty():
            raise ModelResponseError(
                "模型连接器工作进程未返回有效状态。", category="worker_error",
                provider_admitted=admitted, admission_id=admission_id,
            )
        output_value = output_result.get_nowait()
        if isinstance(output_value, BaseException):
            raise ModelResponseError(
                "无法读取模型连接器响应。", category="worker_error",
                provider_admitted=admitted, admission_id=admission_id,
            ) from None
        output = output_value
        if process.returncode == 75:
            raise ModelResponseError(
                "模型调用已取消或所属服务已退出。",
                category="cancelled",
                provider_admitted=admitted,
                admission_id=admission_id,
            )
        try:
            result = json.loads(output.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ModelResponseError(
                "模型连接器工作进程未返回有效状态。",
                category="worker_error",
                provider_admitted=admitted,
                admission_id=admission_id,
            ) from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            kind = result.get("kind") if isinstance(result, dict) else None
            if kind == "http":
                status_code = result.get("status")
                if type(status_code) is not int:
                    status_code = None
                retry_category = (
                    "http_401" if status_code == 401
                    else "http_403" if status_code == 403
                    else "http_429" if status_code == 429
                    else "http_5xx" if status_code is not None and 500 <= status_code <= 599
                    else "http_4xx"
                )
                raise ModelResponseError(
                    f"模型服务返回 HTTP {status_code or 0}。",
                    category=retry_category,
                    status_code=status_code,
                    provider_admitted=admitted,
                    admission_id=admission_id,
                )
            if kind == "timeout":
                raise ModelResponseError(
                    "模型服务请求超时。", category="timeout", provider_admitted=admitted,
                    admission_id=admission_id,
                )
            if kind == "connection":
                raise ModelResponseError(
                    "无法连接模型服务。", category="connection_error", provider_admitted=admitted,
                    admission_id=admission_id,
                )
            if kind == "admission":
                code = result.get("code")
                if code not in {
                    "call_gate_closed", "ledger_unavailable", "ledger_budget_exhausted",
                    "stage_budget_exhausted", "task_budget_exhausted", "call_budget_config_invalid",
                }:
                    code = "ledger_unavailable"
                raise ModelResponseError(
                    "模型请求在联网前被共享调用账本拒绝。", category=code,
                    provider_admitted=admitted, admission_id=admission_id if admitted else None,
                )
            raise ModelResponseError(
                "无法完成模型服务请求。", category="transport_error", provider_admitted=admitted,
                admission_id=admission_id,
            )
        raw_b64 = result.get("raw_b64")
        if not isinstance(raw_b64, str):
            raise ModelResponseError(
                "模型连接器工作进程未返回有效响应。", category="worker_error",
                provider_admitted=admitted, admission_id=admission_id,
            )
        try:
            raw = base64.b64decode(raw_b64, validate=True)
        except (ValueError, TypeError):
            raise ModelResponseError(
                "模型连接器工作进程未返回有效响应。", category="worker_error",
                provider_admitted=admitted, admission_id=admission_id,
            ) from None
        try:
            completion = _parse_response(raw)
        except ModelResponseError as exc:
            exc.provider_admitted = admitted
            exc.provider_response_returned = True
            exc.admission_id = admission_id
            raise
        return ChatCompletion(
            text=completion.text,
            model=completion.model,
            response_id=completion.response_id,
            usage=completion.usage,
            provider_admitted=admitted,
            admission_id=admission_id,
        )
    except ModelResponseError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise ModelResponseError(
            "无法完成模型服务请求。", category="worker_error", provider_admitted=admitted,
            admission_id=admission_id,
        ) from None
    finally:
        if process is not None and process.poll() is None:
            _stop_worker(process)


def _worker_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    source_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = source_root
    for name in (
        "FINTRACE_CALL_CANCEL_FILE", "FINTRACE_CALL_LIVENESS_FILE", "FINTRACE_CALL_GATE_DB",
        "FINTRACE_CALL_GATE_KEY",
    ):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _call_cancelled() -> bool:
    cancel_path = os.environ.get("FINTRACE_CALL_CANCEL_FILE")
    if cancel_path and os.path.exists(cancel_path):
        return True
    heartbeat_path = os.environ.get("FINTRACE_CALL_LIVENESS_FILE")
    if heartbeat_path:
        try:
            return time.time() - os.stat(heartbeat_path).st_mtime > 5.0
        except OSError:
            return True
    return False


def _abort_event_is_set(value: object | None) -> bool:
    checker = getattr(value, "is_set", None)
    return bool(checker()) if callable(checker) else False


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _write_worker_input(
    process: subprocess.Popen[bytes], payload: bytes, errors: queue.Queue[BaseException]
) -> None:
    stream = process.stdin
    if stream is None:
        errors.put(BrokenPipeError())
        return
    try:
        stream.write(payload)
        stream.flush()
    except BaseException as exc:
        try:
            errors.put_nowait(exc)
        except queue.Full:
            pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
        process.stdin = None


def _read_worker_output(stream, result: queue.Queue[bytes | BaseException]) -> None:
    try:
        result.put(stream.read())
    except BaseException as exc:
        try:
            result.put_nowait(exc)
        except queue.Full:
            pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


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
        raise ModelResponseError("模型响应不是 JSON。", category="invalid_response") from None
    if not isinstance(payload, dict):
        raise ModelResponseError("模型响应不是 JSON 对象。", category="invalid_response")
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
        raise ModelResponseError("模型响应没有文本内容。", category="invalid_response")
    message = choices[0].get("message")
    if not isinstance(message, dict) or "content" not in message:
        raise ModelResponseError("模型响应没有文本内容。", category="invalid_response")
    content = message.get("content")
    if not isinstance(content, str):
        raise ModelResponseError("模型响应的内容不是文本。", category="invalid_response")
    if content.strip() == "":
        raise ModelResponseError("模型响应没有文本内容。", category="invalid_response")
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
