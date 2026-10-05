from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from finagent.agents import annual_discovery
from finagent.agents.annual_discovery import _finish_node, _resolve_max_concurrency, _scan_batches_node
from finagent.core.model_settings import ModelSettings
from finagent.llm.chat_completion import ChatCompletion, ModelResponseError
from finagent.retrieval.annual_discovery_context import DiscoveryBatch, DiscoveryFragment, empty_coverage

SOURCE_SHA = "a" * 64
MODEL_SETTINGS = ModelSettings("https://example.invalid/v1", "unit-test-secret", "mock-model", 5)


def _batches(count: int) -> list[DiscoveryBatch]:
    result = []
    for index in range(1, count + 1):
        text = f"第{index}批披露的业务安排出现变化，后续仍需核对。"
        fragment = DiscoveryFragment(
            evidence_id=f"adisc-{SOURCE_SHA[:12]}-p{index}-b0-c0-{index:012x}",
            pdf_page=index,
            block_index=0,
            start_char=0,
            bbox=(1.0, 2.0, 3.0, 4.0),
            raw_text=text,
        )
        result.append(DiscoveryBatch(index, (fragment,), len(text), 500))
    return result


def _response(batch: DiscoveryBatch, *, partial: bool = False) -> str:
    fragment = batch.fragments[0]
    finding = {
        "title": "业务安排变化线索",
        "observation": "原文披露的业务安排出现变化，仍需结合其他材料核对。",
        "why_it_matters": "该变化可能影响对经营过程的理解。",
        "follow_up": "建议结合合同和回款材料进一步核对。",
        "priority": "medium",
        "citations": [{"evidence_id": fragment.evidence_id, "quote": fragment.raw_text}],
    }
    findings = [finding]
    if partial:
        invalid = json.loads(json.dumps(finding, ensure_ascii=False))
        invalid["citations"][0]["quote"] = "这段内容不是原文"
        findings.append(invalid)
    return json.dumps({"findings": findings, "limitations": []}, ensure_ascii=False)


def _state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    *,
    concurrency: int = 3,
    deadline: float | None = None,
    progress: Callable[[dict[str, object]], None] | None = None,
    run_id: str = "annual-discovery-concurrency",
) -> dict[str, Any]:
    batches = _batches(count)
    runs_root = tmp_path / "artifacts" / "runs"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(annual_discovery, "repository_runs_root", lambda: runs_root)
    coverage = empty_coverage()
    coverage.update(
        {
            "total_pages": count,
            "text_pages": count,
            "total_blocks": count,
            "total_chars": sum(batch.raw_chars for batch in batches),
            "planned_batches": count,
            "planned_chars": sum(batch.raw_chars for batch in batches),
            "unread_pages": [],
            "source_status": "parsed",
            "content_status": "text_available",
        }
    )
    return {
        "report": {},
        "source_pdf_path": "unused-by-node-test.pdf",
        "source_record": {},
        "settings": MODEL_SETTINGS,
        "max_concurrency": concurrency,
        "run_dir": str(run_dir),
        "identity": {
            "run_id": run_id,
            "company_id": "600519",
            "report_year": 2025,
            "source_document_id": "maotai-2025",
            "source_sha256": SOURCE_SHA,
        },
        "prompt_text": "fixed offline test prompt",
        "progress_callback": progress,
        "deadline": deadline if deadline is not None else time.monotonic() + 30,
        "batches": batches,
        "fragments_by_id": {
            fragment.evidence_id: fragment
            for batch in batches
            for fragment in batch.fragments
        },
        "coverage": coverage,
        "overall_status": "running",
        "reason": None,
        "limitations": [],
        "progress_warning": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "processed_batches": 0,
        "successful_batches": 0,
        "partial_batches": 0,
        "failed_batches": 0,
        "processed_chars": 0,
        "attempted_chars": 0,
        "findings": [],
        "finding_keys": [],
        "audit_artifacts": [],
    }


def _install_audited_mock(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Callable[[int, DiscoveryBatch], tuple[str, float, bool]],
) -> dict[str, Any]:
    lock = threading.Lock()
    metrics: dict[str, Any] = {"active": 0, "peak": 0, "calls": [], "active_at": {}}

    def mock(messages, settings, *, run_dir, evidence_refs, prompt_version, timeout_seconds, audit_id):
        payload = json.loads(messages[1].content)
        index = int(payload["batch_index"])
        batch = _batches(1)[0]
        fragment_payload = payload["fragments"][0]
        fragment = DiscoveryFragment(
            evidence_id=fragment_payload["evidence_id"],
            pdf_page=int(fragment_payload["pdf_page"]),
            block_index=0,
            start_char=0,
            bbox=(1.0, 2.0, 3.0, 4.0),
            raw_text=fragment_payload["raw_text"],
        )
        batch = DiscoveryBatch(index, (fragment,), len(fragment.raw_text), 500)
        response_text, delay, fail = behavior(index, batch)
        audit_dir = run_dir / audit_id
        audit_dir.mkdir()
        (audit_dir / "request.json").write_text(
            json.dumps(
                {
                    "status": "started",
                    "prompt_version": prompt_version,
                    "messages": [{"role": message.role, "content": message.content} for message in messages],
                    "evidence_refs": [
                        {"document_id": ref.document_id, "page": ref.page}
                        for ref in evidence_refs
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with lock:
            metrics["active"] += 1
            metrics["peak"] = max(metrics["peak"], metrics["active"])
            metrics["calls"].append(index)
            metrics["active_at"][index] = metrics["active"]
        time.sleep(delay)
        try:
            if fail:
                (audit_dir / "failure.json").write_text(
                    json.dumps({"status": "failed", "category": "timeout"}),
                    encoding="utf-8",
                )
                raise ModelResponseError("模型服务请求超时。")
            (audit_dir / "response.json").write_text(
                json.dumps({"status": "succeeded", "text": response_text}, ensure_ascii=False),
                encoding="utf-8",
            )
            return SimpleNamespace(
                completion=ChatCompletion(response_text, settings.model, f"mock-{index}"),
                audit_dir=audit_dir,
            )
        finally:
            with lock:
                metrics["active"] -= 1

    monkeypatch.setattr(annual_discovery, "audited_complete_chat", mock)
    return metrics


def _run_scan(state: dict[str, Any]) -> dict[str, Any]:
    state.update(_scan_batches_node(state))
    state.update(_finish_node(state))
    return state


def test_mock_23_batch_benchmark_rolling_concurrency_preserves_order_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serial_state = _state(tmp_path / "serial", monkeypatch, 23, concurrency=1, run_id="serial")
    serial_metrics = _install_audited_mock(
        monkeypatch,
        lambda _index, batch: (_response(batch), 0.025, False),
    )
    serial_started = time.perf_counter()
    serial = _run_scan(serial_state)
    serial_seconds = time.perf_counter() - serial_started
    serial_requests = {
        index: json.loads(
            (Path(serial_state["run_dir"]) / audit_id / "request.json").read_text(encoding="utf-8")
        )["messages"][1]["content"]
        for index, audit_id in (
            (artifact.batch_index, Path(artifact.audit_dir).name)
            for artifact in serial["audit_artifacts"]
        )
    }

    monkeypatch.setenv(annual_discovery.CONCURRENCY_ENV, "3")
    assert _resolve_max_concurrency(None) == 3
    concurrent_state = _state(
        tmp_path / "concurrent", monkeypatch, 23, concurrency=_resolve_max_concurrency(None), run_id="concurrent"
    )
    concurrent_metrics = _install_audited_mock(
        monkeypatch,
        lambda _index, batch: (_response(batch), 0.025, False),
    )
    progress: list[dict[str, object]] = []
    concurrent_state["progress_callback"] = progress.append
    concurrent_started = time.perf_counter()
    concurrent = _run_scan(concurrent_state)
    concurrent_seconds = time.perf_counter() - concurrent_started
    concurrent_requests = {
        index: json.loads(
            (Path(concurrent_state["run_dir"]) / audit_id / "request.json").read_text(encoding="utf-8")
        )["messages"][1]["content"]
        for index, audit_id in (
            (artifact.batch_index, Path(artifact.audit_dir).name)
            for artifact in concurrent["audit_artifacts"]
        )
    }

    assert serial_metrics["peak"] == 1
    assert concurrent_metrics["peak"] == 3
    assert concurrent["model_call_attempt_count"] == serial["model_call_attempt_count"] == 23
    assert concurrent["model_call_count"] == serial["model_call_count"] == 23
    assert concurrent["coverage"] == serial["coverage"]
    assert concurrent["findings"] == serial["findings"]
    assert concurrent_requests == serial_requests
    assert list(concurrent_requests) == list(range(1, 24))
    assert [row["batch_index"] for row in concurrent["coverage"]["batch_results"]] == list(range(1, 24))
    assert concurrent_seconds < serial_seconds * 0.7, {
        "serial_seconds": serial_seconds,
        "concurrent_seconds": concurrent_seconds,
    }
    metadata = json.loads((Path(concurrent_state["run_dir"]) / "discovery-execution.json").read_text())
    assert metadata == {
        "kind": "fintrace_annual_discovery_execution_v1",
        "prompt_version": "annual_discovery_v2",
        "scheduler": "bounded_rolling",
        "max_in_flight": 3,
        "source_sha256": SOURCE_SHA,
    }


def test_slow_first_batch_reports_completion_order_and_refills_without_wave_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def behavior(index: int, batch: DiscoveryBatch) -> tuple[str, float, bool]:
        return _response(batch), {1: 0.24, 2: 0.035, 3: 0.05}[index], False

    metrics = _install_audited_mock(monkeypatch, behavior)
    progress: list[dict[str, object]] = []
    state = _state(tmp_path, monkeypatch, 3, concurrency=2, progress=progress.append)

    result = _run_scan(state)

    finishes = [event["batch_index"] for event in progress if event["event"] == "batch_finished"]
    starts = [event["batch_index"] for event in progress if event["event"] == "batch_started"]
    assert finishes[:2] == [2, 3]
    assert starts == [1, 2, 3]
    assert metrics["peak"] == 2
    assert result["coverage"]["batch_results"] == sorted(
        result["coverage"]["batch_results"], key=lambda row: row["batch_index"]
    )
    expected_evidence_ids = [batch.fragments[0].evidence_id for batch in _batches(3)]
    assert [finding["citations"][0]["evidence_id"] for finding in result["findings"]] == expected_evidence_ids


def test_deadline_does_not_create_a_gap_after_a_batch_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_call_started = threading.Event()

    def behavior(index: int, batch: DiscoveryBatch) -> tuple[str, float, bool]:
        if index == 2:
            second_call_started.set()
            return _response(batch), 0.24, False
        return _response(batch), 0, False

    metrics = _install_audited_mock(monkeypatch, behavior)
    deadline = time.monotonic() + 0.12
    original_enter = annual_discovery._OrderedCallGate.enter

    def delayed_after_admission(
        gate: annual_discovery._OrderedCallGate,
        batch_index: int,
        absolute_deadline: float,
    ) -> float | None:
        remaining = original_enter(gate, batch_index, absolute_deadline)
        if batch_index == 1 and remaining is not None:
            assert second_call_started.wait(timeout=1)
            while time.monotonic() < absolute_deadline:
                time.sleep(0.002)
        return remaining

    monkeypatch.setattr(annual_discovery._OrderedCallGate, "enter", delayed_after_admission)
    state = _state(
        tmp_path,
        monkeypatch,
        3,
        concurrency=2,
        deadline=deadline,
    )

    result = _run_scan(state)

    assert set(metrics["calls"]) == {1, 2}
    assert result["model_call_attempt_count"] == 2
    assert result["processed_batches"] == 2
    assert result["coverage"]["unattempted_batches"] == 1
    assert [row["status"] for row in result["coverage"]["batch_results"]] == [
        "succeeded", "succeeded", "unattempted"
    ]


def test_deadline_closed_gate_keeps_later_local_validation_failure_unattempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = time.monotonic() + 0.12
    original_enter = annual_discovery._OrderedCallGate.enter
    original_serialize = annual_discovery.serialize_batch_content

    def expire_before_first_admission(
        gate: annual_discovery._OrderedCallGate,
        batch_index: int,
        absolute_deadline: float,
    ) -> float | None:
        if batch_index == 1:
            while time.monotonic() < absolute_deadline:
                time.sleep(0.002)
        return original_enter(gate, batch_index, absolute_deadline)

    def fail_second_batch_serialization(identity, batch_index, fragments):
        if batch_index == 2:
            raise ValueError("synthetic local serialization failure")
        return original_serialize(identity, batch_index, fragments)

    monkeypatch.setattr(annual_discovery._OrderedCallGate, "enter", expire_before_first_admission)
    monkeypatch.setattr(annual_discovery, "serialize_batch_content", fail_second_batch_serialization)
    metrics = _install_audited_mock(
        monkeypatch,
        lambda _index, batch: (_response(batch), 0, False),
    )
    state = _state(tmp_path, monkeypatch, 2, concurrency=2, deadline=deadline)

    result = _run_scan(state)

    assert metrics["calls"] == []
    assert result["model_call_attempt_count"] == 0
    assert result["processed_batches"] == 0
    assert result["coverage"]["unattempted_batches"] == 2
    assert [row["status"] for row in result["coverage"]["batch_results"]] == [
        "unattempted", "unattempted"
    ]


def test_mixed_results_keep_each_audit_bound_to_its_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def behavior(index: int, batch: DiscoveryBatch) -> tuple[str, float, bool]:
        if index == 2:
            return _response(batch, partial=True), 0.03, False
        if index == 3:
            return "", 0.05, True
        return _response(batch), 0.04, False

    _install_audited_mock(monkeypatch, behavior)
    state = _state(tmp_path, monkeypatch, 3, concurrency=3)

    result = _run_scan(state)

    assert result["coverage"]["successful_batches"] == 1
    assert result["coverage"]["partial_batches"] == 1
    assert result["coverage"]["failed_batches"] == 1
    assert result["model_call_count"] == 2
    assert result["model_call_attempt_count"] == 3
    artifacts = {artifact.batch_index: artifact for artifact in result["audit_artifacts"]}
    assert set(artifacts) == {1, 2, 3}
    for index, artifact in artifacts.items():
        audit_dir = Path(state["run_dir"]) / Path(artifact.audit_dir).name
        request = json.loads((audit_dir / "request.json").read_text(encoding="utf-8"))
        request_payload = json.loads(request["messages"][1]["content"])
        assert request_payload["batch_index"] == index
        assert artifact.status == ("failed" if index == 3 else "succeeded")
    timeout_artifact = artifacts[3]
    timeout_dir = Path(state["run_dir"]) / Path(timeout_artifact.audit_dir).name
    assert json.loads((timeout_dir / "failure.json").read_text())["category"] == "timeout"
    assert [row["status"] for row in result["coverage"]["batch_results"]] == [
        "succeeded", "partial", "failed"
    ]


def test_deadline_stops_refill_and_preserves_in_flight_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = _install_audited_mock(
        monkeypatch,
        lambda _index, batch: (_response(batch), 0.45, False),
    )
    state = _state(
        tmp_path,
        monkeypatch,
        4,
        concurrency=2,
        deadline=time.monotonic() + 0.25,
    )

    result = _run_scan(state)

    assert sorted(metrics["calls"]) == [1, 2]
    assert result["model_call_attempt_count"] == 2
    assert result["model_call_count"] == 2
    assert result["coverage"]["successful_batches"] == 2
    assert result["coverage"]["unattempted_batches"] == 2
    assert [row["status"] for row in result["coverage"]["batch_results"]] == [
        "succeeded", "succeeded", "unattempted", "unattempted"
    ]
    assert result["reason"] == "workflow_time_budget_exhausted"


@pytest.mark.parametrize("value", [0, 4, True, 1.0, "2"])
def test_concurrency_override_rejects_values_outside_one_through_three(value: object) -> None:
    with pytest.raises(ValueError):
        _resolve_max_concurrency(value)  # type: ignore[arg-type]


def test_environment_can_fall_back_to_serial_and_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(annual_discovery.CONCURRENCY_ENV, "1")
    assert _resolve_max_concurrency(None) == 1
    monkeypatch.setenv(annual_discovery.CONCURRENCY_ENV, "4")
    with pytest.raises(ValueError, match=annual_discovery.CONCURRENCY_ENV):
        _resolve_max_concurrency(None)


def test_progress_callback_failure_keeps_findings_and_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_audited_mock(monkeypatch, lambda _index, batch: (_response(batch), 0, False))

    def failing_progress(_event: dict[str, object]) -> None:
        raise RuntimeError("must not escape")

    state = _state(tmp_path, monkeypatch, 2, concurrency=2, progress=failing_progress)
    result = _run_scan(state)

    assert result["coverage"]["successful_batches"] == 2
    assert len(result["findings"]) == 2
    assert "progress_callback_failed" in result["limitations"]
