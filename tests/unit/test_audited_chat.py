"""审计包装器：本地 HTTP 或替身网络，不访问云端。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finagent.audit.audited_chat import (
    AuditPersistError,
    EvidenceRef,
    audited_complete_chat,
)
from finagent.audit import audited_chat as audit_module
from finagent.core.model_settings import ModelSettings
from finagent.core.safe_paths import PathBoundaryError
from finagent.llm.chat_completion import ChatCompletion, ChatMessage, ModelResponseError, TokenUsage

_SECRET = "sk-audit-secret-should-not-leak"


def _settings() -> ModelSettings:
    return ModelSettings(
        base_url=f"http://127.0.0.1/v1?token={_SECRET}",
        api_key=_SECRET,
        model=f"model-{_SECRET}",
        timeout_seconds=2,
    )


def _refs() -> list[EvidenceRef]:
    return [EvidenceRef(document_id=f"doc-{_SECRET}", page=12)]


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> tuple[Path, Path]:
    runs_root = tmp_path / "artifacts" / "runs"
    run_dir = runs_root / "run-a"
    run_dir.mkdir(parents=True)
    if monkeypatch is not None:
        monkeypatch.setattr(audit_module, "repository_runs_root", lambda: runs_root)
    return runs_root, run_dir


def _patch_chat(monkeypatch: pytest.MonkeyPatch, behavior):
    calls = {"n": 0}

    def fake(messages, settings, *, timeout_seconds=None):
        calls["n"] += 1
        return behavior(messages, settings, timeout_seconds)

    monkeypatch.setattr(audit_module, "complete_chat", fake)
    return calls


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_no_secret(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            assert _SECRET not in path.read_text(encoding="utf-8")


def test_local_http_success_redacts_and_omits_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_chat_completion import _serve

    runs_root, run_dir = _run(tmp_path, monkeypatch)

    def behavior(_path: str):
        return 200, {
            "id": f"id-{_SECRET}",
            "model": "returned-model",
            "choices": [{"message": {"role": "assistant", "content": f"答案 {_SECRET}"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    server, thread = _serve(behavior)
    settings = ModelSettings(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key=_SECRET,
        model="example-model",
        timeout_seconds=2,
    )
    try:
        result = audited_complete_chat(
            [ChatMessage("user", f"请看 {_SECRET}")],
            settings,
            run_dir=run_dir,
            evidence_refs=_refs(),
            prompt_version="prompt-v1",
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    request = _read(result.audit_dir / "request.json")
    response = _read(result.audit_dir / "response.json")
    assert request["status"] == "started"
    assert request["finished_at"] is None
    assert request["evidence_refs"] == [{"document_id": f"doc-{_REDACTED()}", "page": 12}]
    assert "base_url" not in request
    assert response["status"] == "succeeded"
    assert response["text"] == f"答案 {_REDACTED()}"
    assert response["response_id"] == f"id-{_REDACTED()}"
    assert response["started_at"].endswith("+00:00")
    assert response["finished_at"].endswith("+00:00")
    assert response["duration_seconds"] >= 0
    assert result.completion.text == f"答案 {_SECRET}"
    _assert_no_secret(result.audit_dir)


def _REDACTED() -> str:
    return "[REDACTED]"


def test_failure_records_category_and_keeps_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs_root, run_dir = _run(tmp_path, monkeypatch)

    def behavior(messages, settings, timeout_seconds):
        raise ModelResponseError("模型服务返回 HTTP 503。")

    calls = _patch_chat(monkeypatch, behavior)
    with pytest.raises(ModelResponseError):
        audited_complete_chat(
            [ChatMessage("user", "你好")],
            _settings(),
            run_dir=run_dir,
            evidence_refs=[{"document_id": "doc-1", "page": 3}],
            prompt_version="prompt-v1",
        )
    audit_dirs = [path for path in run_dir.iterdir() if path.is_dir()]
    assert calls["n"] == 1
    assert len(audit_dirs) == 1
    request = _read(audit_dirs[0] / "request.json")
    failure = _read(audit_dirs[0] / "failure.json")
    assert request["status"] == "started"
    assert failure["status"] == "failed"
    assert failure["category"] == "http_error"
    assert failure["started_at"] == request["started_at"]
    assert failure["finished_at"].endswith("+00:00")
    assert failure["duration_seconds"] >= 0
    assert "base_url" not in request
    assert not (audit_dirs[0] / "response.json").exists()
    _assert_no_secret(audit_dirs[0])


def test_keyboard_interrupt_keeps_started_without_failure_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_root, run_dir = _run(tmp_path, monkeypatch)

    def behavior(messages, settings, timeout_seconds):
        raise KeyboardInterrupt

    calls = _patch_chat(monkeypatch, behavior)
    with pytest.raises(KeyboardInterrupt):
        audited_complete_chat(
            [ChatMessage("user", "你好")],
            _settings(),
            run_dir=run_dir,
            evidence_refs=_refs(),
            prompt_version="prompt-v1",
        )
    audit_dir = next(path for path in run_dir.iterdir() if path.is_dir())
    assert calls["n"] == 1
    assert _read(audit_dir / "request.json")["status"] == "started"
    assert not (audit_dir / "failure.json").exists()
    assert not (audit_dir / "response.json").exists()


def test_request_write_failure_does_not_call_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_root, run_dir = _run(tmp_path, monkeypatch)
    calls = _patch_chat(
        monkeypatch,
        lambda messages, settings, timeout_seconds: ChatCompletion("不会发生", "m", "id"),
    )

    def broken(path: Path, payload: object, secret: str) -> None:
        raise OSError("disk")

    monkeypatch.setattr(audit_module, "_write_json", broken)
    with pytest.raises(AuditPersistError, match="未调用模型") as caught:
        audited_complete_chat(
            [ChatMessage("user", "你好")],
            _settings(),
            run_dir=run_dir,
            evidence_refs=_refs(),
            prompt_version="prompt-v1",
        )
    assert calls["n"] == 0
    assert caught.value.__cause__ is None


def test_response_write_failure_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_root, run_dir = _run(tmp_path, monkeypatch)
    calls = _patch_chat(
        monkeypatch,
        lambda messages, settings, timeout_seconds: ChatCompletion(
            f"正文{_SECRET}",
            "returned",
            "resp-1",
            TokenUsage(1, 1, 2),
        ),
    )
    real_write = audit_module._write_json

    def flaky(path: Path, payload: object, secret: str) -> None:
        if path.name == "response.json":
            raise OSError("disk")
        real_write(path, payload, secret)

    monkeypatch.setattr(audit_module, "_write_json", flaky)
    with pytest.raises(AuditPersistError, match="不能记为成功") as caught:
        audited_complete_chat(
            [ChatMessage("user", "你好")],
            _settings(),
            run_dir=run_dir,
            evidence_refs=_refs(),
            prompt_version="prompt-v1",
        )
    audit_dir = next(path for path in run_dir.iterdir() if path.is_dir())
    assert calls["n"] == 1
    assert caught.value.__cause__ is None
    assert _read(audit_dir / "request.json")["status"] == "started"
    assert not (audit_dir / "response.json").exists()
    _assert_no_secret(audit_dir)


def test_two_calls_use_distinct_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs_root, run_dir = _run(tmp_path, monkeypatch)
    calls = _patch_chat(
        monkeypatch,
        lambda messages, settings, timeout_seconds: ChatCompletion("好", "m", "id-1"),
    )
    first = audited_complete_chat(
        [ChatMessage("user", "一")],
        _settings(),
        run_dir=run_dir,
        evidence_refs=_refs(),
        prompt_version="prompt-v1",
    )
    second = audited_complete_chat(
        [ChatMessage("user", "二")],
        _settings(),
        run_dir=run_dir,
        evidence_refs=_refs(),
        prompt_version="prompt-v1",
    )
    assert calls["n"] == 2
    assert first.audit_dir != second.audit_dir
    assert first.audit_dir.parent == run_dir.resolve()
    assert _read(first.audit_dir / "request.json")["messages"][0]["content"] == "一"
    assert _read(second.audit_dir / "request.json")["messages"][0]["content"] == "二"


def test_rejects_paths_outside_direct_run_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_root, run_dir = _run(tmp_path)
    nested = run_dir / "nested"
    nested.mkdir()
    elsewhere = tmp_path / "other" / "artifacts" / "runs" / "same-name"
    elsewhere.mkdir(parents=True)
    calls = _patch_chat(
        monkeypatch,
        lambda messages, settings, timeout_seconds: ChatCompletion("不会发生", "m", "id"),
    )
    with pytest.raises(PathBoundaryError):
        audited_complete_chat(
            [ChatMessage("user", "你好")],
            _settings(),
            run_dir=run_dir,
            evidence_refs=_refs(),
            prompt_version="prompt-v1",
        )
    monkeypatch.setattr(audit_module, "repository_runs_root", lambda: runs_root)
    for candidate in (nested, elsewhere, runs_root):
        with pytest.raises(PathBoundaryError):
            audited_complete_chat(
                [ChatMessage("user", "你好")],
                _settings(),
                run_dir=candidate,
                evidence_refs=_refs(),
                prompt_version="prompt-v1",
            )
    assert calls["n"] == 0
    assert list(run_dir.iterdir()) == [nested]


def test_rejects_symlink_run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs_root, run_dir = _run(tmp_path, monkeypatch)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    link = runs_root / "linked-run"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import subprocess

        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            pytest.skip("当前环境不能创建目录符号链接或联接。")
    calls = _patch_chat(
        monkeypatch,
        lambda messages, settings, timeout_seconds: ChatCompletion("不会发生", "m", "id"),
    )
    with pytest.raises(PathBoundaryError):
        audited_complete_chat(
            [ChatMessage("user", "你好")],
            _settings(),
            run_dir=link,
            evidence_refs=_refs(),
            prompt_version="prompt-v1",
        )
    assert calls["n"] == 0


@pytest.mark.parametrize(
    "refs",
    [
        "doc-1",
        [{"document_id": "doc-1"}],
        [{"document_id": "  ", "page": 1}],
        [{"document_id": "doc-1", "page": 0}],
        [{"document_id": "doc-1", "page": True}],
        [{"document_id": "doc-1", "page": 1, "note": "extra"}],
        [object()],
    ],
)
def test_rejects_invalid_evidence_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refs: object
) -> None:
    runs_root, run_dir = _run(tmp_path, monkeypatch)
    calls = _patch_chat(
        monkeypatch,
        lambda messages, settings, timeout_seconds: ChatCompletion("不会发生", "m", "id"),
    )
    with pytest.raises(ValueError):
        audited_complete_chat(
            [ChatMessage("user", "你好")],
            _settings(),
            run_dir=run_dir,
            evidence_refs=refs,  # type: ignore[arg-type]
            prompt_version="prompt-v1",
        )
    assert calls["n"] == 0
    assert list(run_dir.iterdir()) == []


def test_timeout_records_category_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_chat_completion import _serve

    _runs_root, run_dir = _run(tmp_path, monkeypatch)
    hits = {"n": 0}

    def behavior(_path: str):
        hits["n"] += 1
        import time

        time.sleep(2)
        return 200, {"id": "late", "model": "m", "choices": [{"message": {"content": "晚了"}}]}

    server, thread = _serve(behavior)
    settings = ModelSettings(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key=_SECRET,
        model="example-model",
        timeout_seconds=0.2,
    )
    try:
        with pytest.raises(ModelResponseError):
            audited_complete_chat(
                [ChatMessage("user", f"含密钥 {_SECRET}")],
                settings,
                run_dir=run_dir,
                evidence_refs=_refs(),
                prompt_version="prompt-v1",
            )
    finally:
        server.shutdown()
        thread.join(timeout=3)
    audit_dir = next(path for path in run_dir.iterdir() if path.is_dir())
    failure = _read(audit_dir / "failure.json")
    assert hits["n"] == 1
    assert failure["category"] == "timeout"
    assert failure["status"] == "failed"
    assert not (audit_dir / "response.json").exists()
    _assert_no_secret(audit_dir)


def test_malformed_response_records_invalid_response_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_chat_completion import _serve

    _runs_root, run_dir = _run(tmp_path, monkeypatch)
    hits = {"n": 0}

    def behavior(_path: str):
        hits["n"] += 1
        return 200, f"not-json {_SECRET}".encode("utf-8")

    server, thread = _serve(behavior)
    settings = ModelSettings(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key=_SECRET,
        model="example-model",
        timeout_seconds=2,
    )
    try:
        with pytest.raises(ModelResponseError):
            audited_complete_chat(
                [ChatMessage("user", f"含密钥 {_SECRET}")],
                settings,
                run_dir=run_dir,
                evidence_refs=_refs(),
                prompt_version="prompt-v1",
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    audit_dir = next(path for path in run_dir.iterdir() if path.is_dir())
    failure = _read(audit_dir / "failure.json")
    assert hits["n"] == 1
    assert failure["category"] == "invalid_response"
    assert failure["status"] == "failed"
    assert not (audit_dir / "response.json").exists()
    _assert_no_secret(audit_dir)
