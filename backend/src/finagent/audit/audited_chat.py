"""在已有 Chat Completions 调用外包一层本地审计。

run_dir 必须是本仓库 artifacts/runs 的直接子目录。
先把 status=started 的脱敏请求落盘，然后才调用 complete_chat。
成功记录写完才返回；响应写失败会抛错。失败只记安全类别。
不保存 base_url、请求头或异常原文。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from finagent.core.model_settings import ModelSettings
from finagent.core.safe_paths import PathBoundaryError
from finagent.llm.chat_completion import ChatCompletion, ChatMessage, ModelResponseError, complete_chat

_REDACTED = "[REDACTED]"
_REPARSE_POINT = 0x400


class AuditPersistError(Exception):
    """审计文件没有按要求写完。消息不包含密钥、请求正文或异常原文。"""


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """允许外发的一条证据位置。页码是 PDF 的 1-based 页序号。"""

    document_id: str
    page: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.document_id, str)
            or self.document_id.strip() == ""
            or self.document_id != self.document_id.strip()
        ):
            raise ValueError("document_id 必须是去掉首尾空白的非空字符串。")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise ValueError("PDF 页码必须是从 1 开始的整数。")


@dataclass(frozen=True, slots=True)
class AuditedChatResult:
    """响应记录已落盘的一次文本调用。audit_dir 是本次 UUID 子目录。"""

    completion: ChatCompletion
    audit_dir: Path


def audited_complete_chat(
    messages: Sequence[ChatMessage],
    settings: ModelSettings,
    *,
    run_dir: Path,
    evidence_refs: Sequence[EvidenceRef | Mapping[str, object]],
    prompt_version: str,
    timeout_seconds: float | None = None,
) -> AuditedChatResult:
    """包装 complete_chat。started 落盘后才联网；响应落盘后才返回。"""

    checked_run = _require_run_dir(run_dir)
    refs = _require_evidence_refs(evidence_refs)
    version = _require_prompt_version(prompt_version)
    started_at = _utc_now()
    audit_dir = checked_run / str(uuid.uuid4())
    audit_dir.mkdir(parents=False, exist_ok=False)
    secret = settings.api_key
    request_record = {
        "status": "started",
        "started_at": started_at,
        "finished_at": None,
        "duration_seconds": None,
        "prompt_version": version,
        "evidence_refs": refs,
        "model": settings.model,
        "timeout_seconds": settings.timeout_seconds if timeout_seconds is None else timeout_seconds,
        "messages": [{"role": message.role, "content": message.content} for message in messages],
        "stream": False,
    }
    try:
        _write_json(audit_dir / "request.json", request_record, secret)
    except Exception as exc:
        raise AuditPersistError("审计请求写入失败，未调用模型。") from None
    try:
        completion = complete_chat(messages, settings, timeout_seconds=timeout_seconds)
    except Exception as exc:
        finished_at = _utc_now()
        failure_record = {
            "status": "failed",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration_seconds(started_at, finished_at),
            "category": _failure_category(exc),
        }
        try:
            _write_json(audit_dir / "failure.json", failure_record, secret)
        except Exception as write_exc:
            raise AuditPersistError("审计失败记录写入失败。") from None
        raise
    finished_at = _utc_now()
    usage = None
    if completion.usage is not None:
        usage = {
            "prompt_tokens": completion.usage.prompt_tokens,
            "completion_tokens": completion.usage.completion_tokens,
            "total_tokens": completion.usage.total_tokens,
        }
    response_record = {
        "status": "succeeded",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration_seconds(started_at, finished_at),
        "text": completion.text,
        "model": completion.model,
        "response_id": completion.response_id,
        "usage": usage,
    }
    try:
        _write_json(audit_dir / "response.json", response_record, secret)
    except Exception as exc:
        raise AuditPersistError("审计响应写入失败，本次调用不能记为成功。") from None
    return AuditedChatResult(completion=completion, audit_dir=audit_dir)


def repository_runs_root() -> Path:
    """本文件所在仓库的 artifacts/runs。"""

    return Path(__file__).resolve().parents[4] / "artifacts" / "runs"


def _require_run_dir(run_dir: Path) -> Path:
    if not isinstance(run_dir, Path):
        raise PathBoundaryError("run_dir 必须是路径。")
    root = repository_runs_root()
    if not isinstance(root, Path):
        raise PathBoundaryError("run_dir 必须是 artifacts/runs 下已存在的直接子目录。")
    root_resolved = root.resolve()
    if _is_link(run_dir) or not run_dir.is_dir():
        raise PathBoundaryError("run_dir 必须是 artifacts/runs 下已存在的直接子目录。")
    if run_dir.parent.resolve() != root_resolved:
        raise PathBoundaryError("run_dir 必须是 artifacts/runs 下已存在的直接子目录。")
    if run_dir.name in {"", ".", ".."} or run_dir.resolve() != root_resolved / run_dir.name:
        raise PathBoundaryError("run_dir 必须是 artifacts/runs 下已存在的直接子目录。")
    return run_dir.resolve()


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _require_evidence_refs(
    evidence_refs: Sequence[EvidenceRef | Mapping[str, object]],
) -> list[dict[str, object]]:
    if isinstance(evidence_refs, (str, bytes)) or not isinstance(evidence_refs, Sequence):
        raise ValueError("evidence_refs 必须是证据引用序列。")
    prepared: list[dict[str, object]] = []
    for item in evidence_refs:
        if isinstance(item, EvidenceRef):
            ref = item
        elif isinstance(item, Mapping):
            if set(item) != {"document_id", "page"}:
                raise ValueError("证据引用只能包含 document_id 和 page。")
            ref = EvidenceRef(document_id=item["document_id"], page=item["page"])  # type: ignore[arg-type]
        else:
            raise ValueError("证据引用必须包含 document_id 和 page。")
        prepared.append({"document_id": ref.document_id, "page": ref.page})
    return prepared


def _require_prompt_version(prompt_version: str) -> str:
    if not isinstance(prompt_version, str) or prompt_version.strip() == "":
        raise ValueError("prompt_version 必须是非空字符串。")
    return prompt_version


def _redact(value: object, secret: str) -> object:
    if isinstance(value, str):
        if secret:
            return value.replace(secret, _REDACTED)
        return value
    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            safe_key = _redact(key, secret) if isinstance(key, str) else key
            redacted[safe_key] = _redact(item, secret)
        return redacted
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _failure_category(exc: BaseException) -> str:
    if isinstance(exc, ModelResponseError):
        message = str(exc)
        if message.startswith("超时时间必须大于 0"):
            return "invalid_timeout"
        if message.startswith("模型服务返回 HTTP"):
            return "http_error"
        if message == "模型服务请求超时。":
            return "timeout"
        if message == "无法连接模型服务。":
            return "connection_error"
        if message.startswith("模型响应"):
            return "invalid_response"
        return "call_failed"
    if isinstance(exc, ValueError):
        return "invalid_request"
    return "call_failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_seconds(started_at: str, finished_at: str) -> float:
    started = datetime.fromisoformat(started_at)
    finished = datetime.fromisoformat(finished_at)
    return (finished - started).total_seconds()


def _write_json(path: Path, payload: object, secret: str) -> None:
    data = json.dumps(_redact(payload, secret), ensure_ascii=False, indent=2).encode("utf-8")
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
