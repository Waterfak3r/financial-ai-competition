"""Optional M3 annual-investigation CLI integration and report appendix."""

from __future__ import annotations

import hashlib
import importlib
import json
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from finagent.core.model_settings import ModelConfigError, ModelSettings, load_model_settings
from finagent.agents.annual_review import not_called_model_review


def run_annual_investigation_appendix(
    report: Mapping[str, Any],
    *,
    archived_pdf_path: Path,
    original_pdf_path: Path,
    source_record_path: Path,
    run_dir: Path,
    artifacts_root: Path,
) -> dict[str, Any]:
    """Run M3 when explicitly requested and always return a serializable state.

    Configuration is read only inside this function, which the CLI calls only
    for ``--with-model``. Input or runtime failures are represented in the
    investigation artifact so they do not discard the deterministic report.
    """

    try:
        identity = _report_identity(report)
    except Exception:
        return {
            "kind": "fintrace_annual_investigation",
            "status": "failed",
            "model_called": False,
            "model_call_count": 0,
            "model_call_attempt_count": 0,
            "items": [],
            "audit_artifacts": [],
            "fraud_conclusion": None,
            "reason": "annual_report_identity_invalid",
            "final_review": {
                "status": "not_called",
                "reason": "annual_report_identity_invalid",
                "assessment": None,
                "summary": None,
                "reasons": [],
                "follow_up_items": [],
                "limitations": [],
                "model_called": False,
                "call_count": 0,
                "call_attempt_count": 0,
                "audit_artifacts": [],
                "source_identity": None,
            },
            "archive_path": _relative_or_absolute(run_dir / "investigation.json", artifacts_root),
        }
    archive_path = _relative_or_absolute(run_dir / "investigation.json", artifacts_root)
    state: dict[str, Any] = {
        "kind": "fintrace_annual_investigation",
        **identity,
        "status": "failed",
        "model_called": False,
        "model_call_count": 0,
        "model_call_attempt_count": 0,
        "supplementary_retrieval_used": False,
        "items": [],
        "audit_artifacts": [],
        "fraud_conclusion": None,
        "archive_path": archive_path,
        "final_review": not_called_model_review(report, "model_review_not_started"),
    }

    formal_runs_root = (_repository_root() / "artifacts" / "runs").resolve()
    if run_dir.resolve().parent != formal_runs_root:
        state.update(status="failed", reason="model_requires_repository_artifacts_root")
        state["final_review"] = not_called_model_review(report, "model_requires_repository_artifacts_root")
        return state

    try:
        record, record_sha256 = _load_source_record(source_record_path)
        binding = _validate_source_binding(
            report,
            record,
            record_sha256=record_sha256,
            source_record_path=source_record_path,
            original_pdf_path=original_pdf_path,
            archived_pdf_path=archived_pdf_path,
        )
        state["source_binding"] = binding
    except _SourceBindingError as exc:
        state.update(status="failed", reason=exc.code)
        state["final_review"] = not_called_model_review(report, exc.code)
        return state
    except Exception:
        state.update(status="failed", reason="source_binding_validation_failed")
        state["final_review"] = not_called_model_review(report, "source_binding_validation_failed")
        return state

    try:
        _ensure_langgraph_available()
    except _OptionalDependencyUnavailable:
        state.update(
            status="failed",
            reason="langgraph_unavailable",
            reason_detail="年度调查图需要可选依赖 langgraph>=1.2,<1.3；本机未安装该依赖。",
        )
        state["final_review"] = not_called_model_review(report, "langgraph_unavailable")
        return state

    try:
        settings = load_model_settings()
    except ModelConfigError as exc:
        state.update(
            status="abstained",
            reason="model_configuration_unavailable",
            reason_detail=str(exc),
        )
        state["final_review"] = not_called_model_review(report, "model_configuration_unavailable")
        return state
    except Exception:
        state.update(status="failed", reason="model_configuration_load_failed")
        state["final_review"] = not_called_model_review(report, "model_configuration_load_failed")
        return state

    state["model_name"] = settings.model
    before = _run_children(run_dir)
    try:
        result = _invoke_agent(
            report,
            archived_pdf_path=archived_pdf_path,
            source_record=record,
            settings=settings,
            run_dir=run_dir,
        )
        payload = result.to_dict()
        _validate_result_identity(payload, identity)
        attempts = _nonnegative_int(payload.get("model_call_attempt_count"))
        calls = _nonnegative_int(payload.get("model_call_count"))
        payload.update(
            model_called=calls > 0,
            model_call_count=calls,
            model_call_attempt_count=attempts,
            archive_path=archive_path,
            source_binding=binding,
            model_name=settings.model,
        )
    except Exception as exc:
        audits, attempts, calls = _discover_new_audits(run_dir, before, artifacts_root)
        error_name = type(exc).__name__
        if error_name == "LangGraphUnavailableError":
            state.update(
                status="failed",
                reason="langgraph_unavailable",
                reason_detail="年度调查图需要可选依赖 langgraph>=1.2,<1.3；本机未安装该依赖。",
            )
        else:
            state.update(status="failed", reason="annual_investigation_failed")
        state.update(
            model_called=calls > 0,
            model_call_count=calls,
            model_call_attempt_count=attempts,
            audit_artifacts=audits,
        )
        payload = state

    try:
        review = _invoke_review(
            report,
            payload,
            settings=settings,
            run_dir=run_dir,
            artifacts_root=artifacts_root,
        )
    except Exception:
        review = not_called_model_review(report, "model_review_call_failed")
        review["status"] = "failed"
    return _attach_review_outcome(payload, review)


def _invoke_review(
    report: Mapping[str, Any],
    investigation: Mapping[str, Any],
    *,
    settings: ModelSettings,
    run_dir: Path,
    artifacts_root: Path,
) -> dict[str, Any]:
    agent = importlib.import_module("finagent.agents.annual_review")
    return agent.run_annual_model_review(
        report,
        investigation,
        settings,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
    )


def _attach_review_outcome(
    investigation: Mapping[str, Any], review: Mapping[str, Any]
) -> dict[str, Any]:
    payload = dict(investigation)
    base_calls = _nonnegative_int(payload.get("model_call_count"))
    base_attempts = _nonnegative_int(payload.get("model_call_attempt_count"))
    review_calls = _nonnegative_int(review.get("call_count"))
    review_attempts = _nonnegative_int(review.get("call_attempt_count"))
    payload["investigation_call_count"] = base_calls
    payload["investigation_call_attempt_count"] = base_attempts
    payload["model_review_call_count"] = review_calls
    payload["model_review_call_attempt_count"] = review_attempts
    payload["model_call_count"] = base_calls + review_calls
    payload["model_call_attempt_count"] = base_attempts + review_attempts
    payload["model_called"] = payload["model_call_count"] > 0
    audits = payload.get("audit_artifacts")
    review_audits = review.get("audit_artifacts")
    payload["audit_artifacts"] = [
        *(audits if isinstance(audits, list) else []),
        *(review_audits if isinstance(review_audits, list) else []),
    ]
    payload["final_review"] = dict(review)
    return payload


def attach_investigation_to_report(
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    investigation: Mapping[str, Any],
    *,
    investigation_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add the unverified M3 appendix without changing verified report sections."""

    updated_report = dict(report)
    scope = dict(report.get("scope", {}))
    original_note = str(scope.get("note", ""))
    scope["note"] = _model_scope_note(original_note)
    updated_report["scope"] = scope
    updated_report["model_called"] = bool(investigation.get("model_called"))
    updated_report["model_investigation"] = dict(investigation)
    final_review = investigation.get("final_review")
    if isinstance(final_review, Mapping):
        updated_report["model_review"] = dict(final_review)

    updated_analysis = dict(analysis)
    updated_analysis["model_called"] = bool(investigation.get("model_called"))
    updated_analysis["scope"] = {"note": scope["note"]}
    if isinstance(final_review, Mapping):
        updated_analysis["model_review"] = dict(final_review)
    updated_analysis["investigation"] = {
        "status": investigation.get("status"),
        "model_called": bool(investigation.get("model_called")),
        "model_call_count": investigation.get("model_call_count", 0),
        "model_call_attempt_count": investigation.get("model_call_attempt_count", 0),
        "output_path": investigation_path,
    }
    return updated_report, updated_analysis


def render_investigation_markdown(investigation: Mapping[str, Any]) -> str:
    """Render model interpretations as explicitly unverified material."""

    lines = ["## M3 年报文本调查", "", "本节仅为模型提出的未核实解释或弃权，不属于已核实事实、计算或结论。", ""]
    lines.append(f"- 调查状态：`{investigation.get('status', 'failed')}`")
    lines.append(f"- 实际模型调用：{'是' if investigation.get('model_called') else '否'}")
    lines.append(f"- 调查归档：`{investigation.get('archive_path', 'investigation.json')}`")
    if investigation.get("reason"):
        lines.append(f"- 状态说明：`{investigation['reason']}`")
    if investigation.get("reason_detail"):
        lines.append(f"- 详情：{investigation['reason_detail']}")

    review = investigation.get("final_review")
    if isinstance(review, Mapping):
        lines.extend(["", "## M3 最终评审（模型观点，未独立核验）", ""])
        lines.append(f"- 评审状态：`{review.get('status', 'failed')}`")
        lines.append(f"- 实际模型调用：{'是' if review.get('model_called') else '否'}")
        if review.get("reason"):
            lines.append(f"- 状态说明：`{review['reason']}`")
        if review.get("assessment"):
            lines.append(f"- 评审意见：`{review['assessment']}`")
        if review.get("summary"):
            lines.extend(["", str(review["summary"])])
        for item in review.get("reasons", []) if isinstance(review.get("reasons"), list) else []:
            if isinstance(item, Mapping):
                lines.append(f"- 理由：{item.get('text', '')}（依据：{', '.join(item.get('evidence_ids', []))}）")
        for item in review.get("follow_up_items", []) if isinstance(review.get("follow_up_items"), list) else []:
            if isinstance(item, Mapping):
                lines.append(f"- 后续核查：{item.get('object', '')}；{item.get('action', '')}；依据：{', '.join(item.get('evidence_ids', []))}")
        for item in review.get("limitations", []) if isinstance(review.get("limitations"), list) else []:
            lines.append(f"- 评审限制：{item}")

    items = investigation.get("items")
    if not isinstance(items, list) or not items:
        lines.extend(["", "没有可用的模型解释；本次状态和原因已写入 investigation.json。"])
    else:
        for item in items:
            if not isinstance(item, Mapping):
                continue
            signal_id = item.get("signal_id", "unknown")
            status = item.get("status", "abstained")
            lines.extend(["", f"### `{signal_id}`（`{status}`）", ""])
            if status == "interpretation":
                lines.append("**未核实模型解释：**" + str(item.get("explanation") or "未提供。"))
            else:
                lines.append("**弃权：**" + str(item.get("reason") or "证据不足。"))
            for label, key in (
                ("替代解释", "alternative_explanations"),
                ("限制", "limitations"),
                ("同源叙述证据 ID", "narrative_evidence_ids"),
            ):
                values = item.get(key)
                if isinstance(values, list) and values:
                    lines.append(f"- {label}：" + "；".join(str(value) for value in values))

    audits = investigation.get("audit_artifacts")
    if isinstance(audits, list) and audits:
        lines.extend(["", "### 调用审计路径", ""])
        for audit in audits:
            if not isinstance(audit, Mapping):
                continue
            audit_dir = audit.get("audit_dir")
            if audit_dir:
                lines.append(f"- `{audit_dir}`（`{audit.get('status', 'unknown')}`）")
            for field in ("request_path", "response_path", "failure_path"):
                value = audit.get(field)
                if value:
                    lines.append(f"  - {field}: `{value}`")
    return "\n".join(lines).rstrip() + "\n"


def render_report_with_investigation(
    report: Mapping[str, Any], investigation: Mapping[str, Any]
) -> str:
    """Re-render the standard report and append its separate M3 section."""

    from finagent.reports.annual_report import render_markdown

    return render_markdown(report).rstrip() + "\n\n" + render_investigation_markdown(investigation)


class _SourceBindingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _OptionalDependencyUnavailable(RuntimeError):
    pass


def _ensure_langgraph_available() -> None:
    try:
        importlib.import_module("langgraph.graph")
    except ImportError as exc:
        raise _OptionalDependencyUnavailable from exc


def _load_source_record(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
    except OSError:
        raise _SourceBindingError("source_record_unreadable") from None
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _SourceBindingError("source_record_invalid_json") from None
    if not isinstance(record, dict):
        raise _SourceBindingError("source_record_invalid_shape")
    return record, hashlib.sha256(payload).hexdigest()


def _validate_source_binding(
    report: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    record_sha256: str,
    source_record_path: Path,
    original_pdf_path: Path,
    archived_pdf_path: Path,
) -> dict[str, Any]:
    identity = _report_identity(report)
    report_year = identity["report_year"]
    period = record.get("report_period")
    if (
        record.get("document_id") != identity["source_document_id"]
        or record.get("company_id") != identity["company_id"]
        or not isinstance(period, str)
        or not _is_matching_period(period, report_year)
        or not isinstance(record.get("sha256"), str)
        or record["sha256"].lower() != identity["source_sha256"]
    ):
        raise _SourceBindingError("source_record_identity_mismatch")

    local_path = record.get("local_path")
    if not isinstance(local_path, str) or not local_path.strip():
        raise _SourceBindingError("source_record_pdf_path_missing")
    declared_path = Path(local_path.strip()).expanduser()
    if not declared_path.is_absolute():
        declared_path = _repository_root() / declared_path
    try:
        if declared_path.resolve() != original_pdf_path.resolve():
            raise _SourceBindingError("source_record_pdf_path_mismatch")
        original_hash = _sha256(original_pdf_path)
        archived_hash = _sha256(archived_pdf_path)
    except OSError:
        raise _SourceBindingError("source_pdf_unreadable") from None
    if original_hash != identity["source_sha256"] or archived_hash != identity["source_sha256"]:
        raise _SourceBindingError("source_pdf_hash_mismatch")

    return {
        "document_id": identity["source_document_id"],
        "company_id": identity["company_id"],
        "report_period": period,
        "source_sha256": identity["source_sha256"],
        "source_record_sha256": record_sha256,
        "source_record_path": _display_path(source_record_path),
        "source_pdf_path": _display_path(original_pdf_path),
        "archived_pdf_path": archived_pdf_path.name,
    }


def _report_identity(report: Mapping[str, Any]) -> dict[str, str | int]:
    run_id = report.get("run_id")
    company_id = report.get("company_id")
    report_year = report.get("report_year")
    document_id = report.get("source_document_id")
    source_sha256 = report.get("source_sha256")
    if (
        not isinstance(run_id, str)
        or not isinstance(company_id, str)
        or type(report_year) is not int
        or not isinstance(document_id, str)
        or not isinstance(source_sha256, str)
    ):
        raise ValueError("年度报告缺少调查所需的来源身份字段。")
    return {
        "run_id": run_id,
        "company_id": company_id,
        "report_year": report_year,
        "source_document_id": document_id,
        "source_sha256": source_sha256.lower(),
    }


def _validate_result_identity(payload: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    for key in ("run_id", "company_id", "report_year", "source_document_id", "source_sha256"):
        actual = payload.get(key)
        expected = identity[key]
        if key == "source_sha256" and isinstance(actual, str):
            actual = actual.lower()
        if actual != expected:
            raise ValueError("调查返回的来源身份与年度报告不一致。")
    if payload.get("status") not in {"completed", "abstained"}:
        raise ValueError("调查返回了未知状态。")


def _invoke_agent(
    report: Mapping[str, Any],
    *,
    archived_pdf_path: Path,
    source_record: Mapping[str, Any],
    settings: ModelSettings,
    run_dir: Path,
) -> Any:
    agent = importlib.import_module("finagent.agents.annual_investigation")
    return agent.run_annual_investigation(
        report,
        archived_pdf_path,
        source_record,
        settings,
        run_dir=run_dir,
    )


def _discover_new_audits(
    run_dir: Path,
    before: set[str],
    artifacts_root: Path,
) -> tuple[list[dict[str, Any]], int, int]:
    audits: list[dict[str, Any]] = []
    persisted_attempts = 0
    for child in _new_uuid_children(run_dir, before):
        request = child / "request.json"
        response = child / "response.json"
        failure = child / "failure.json"
        if request.exists():
            persisted_attempts += 1
        try:
            audit_dir = child.resolve().relative_to(artifacts_root.resolve()).as_posix()
        except (OSError, ValueError):
            audit_dir = child.name
        audits.append(
            {
                "audit_dir": audit_dir,
                "request_path": f"{audit_dir}/request.json" if request.exists() else None,
                "response_path": f"{audit_dir}/response.json" if response.exists() else None,
                "failure_path": f"{audit_dir}/failure.json" if failure.exists() else None,
                "status": "succeeded" if response.exists() else ("failed" if failure.exists() else "started"),
                "prompt_version": "annual_investigation_v1",
            }
        )
    # AnnualInvestigationResult.model_call_count counts requests whose audit
    # record was persisted, including HTTP failures. A response file separately
    # indicates successful completion and is already visible in each artifact.
    return audits, persisted_attempts, persisted_attempts


def _new_uuid_children(run_dir: Path, before: set[str]) -> list[Path]:
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for child in children:
        if not child.is_dir() or child.name in before:
            continue
        try:
            uuid.UUID(child.name)
        except ValueError:
            continue
        found.append(child)
    return found


def _run_children(run_dir: Path) -> set[str]:
    try:
        return {item.name for item in run_dir.iterdir() if item.is_dir()}
    except OSError:
        return set()


def _nonnegative_int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _is_matching_period(value: str, year: int) -> bool:
    if value != f"{year:04d}-12-31":
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _model_scope_note(note: str) -> str:
    sentence = (
        "显式启用的 M3 年报调查与最终评审会调用模型：调查只提出未核实解释或弃权，"
        "最终评审只提供模型观点；两者都不参与事实、计算和主张核验，也不确认或排除舞弊。"
    )
    for original in ("报告不调用模型，也不确认舞弊。", "报告不调用模型。"):
        if original in note:
            return note.replace(original, sentence)
    if sentence not in note:
        return f"{note.rstrip()} {sentence}".strip()
    return note


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(_repository_root().resolve()).as_posix()
    except (OSError, ValueError):
        return str(path.resolve())


def _relative_or_absolute(path: Path, parent: Path) -> str:
    try:
        return path.relative_to(parent).as_posix()
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
