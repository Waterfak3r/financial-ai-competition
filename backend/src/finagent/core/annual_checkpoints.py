"""Append-only, source-bound checkpoints for annual analysis reports.

Checkpoint files live beside the final report, while a small pointer in the
run directory selects the current immutable snapshot. Readers only follow the
pointer after validating its exact schema, fixed relative path, digest, and the
manifest identity projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from finagent.core.safe_paths import PathBoundaryError, validate_run_id

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 48 * 1024 * 1024
_MAX_POINTER_BYTES = 64 * 1024
_MAX_SOURCE_PDF_BYTES = 256 * 1024 * 1024
_MAX_SEQUENCE = 99_999_999
_REPARSE_POINT_ATTRIBUTE = 0x400
_ATOMIC_REPLACE_MAX_ATTEMPTS = 6
_ATOMIC_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
_ATOMIC_REPLACE_RETRYABLE_WINERRORS = frozenset({5, 32, 33})
_SNAPSHOT_KIND = "fintrace_annual_analysis_checkpoint"
_POINTER_KIND = "fintrace_annual_analysis_checkpoint_pointer"
_SNAPSHOT_FIELDS = {
    "kind", "schema_version", "run_id", "sequence", "state", "is_partial",
    "parent_run_id", "source_sha256", "report_sha256", "manifest_binding_sha256", "report",
}
_POINTER_FIELDS = {
    "kind", "schema_version", "run_id", "sequence", "snapshot_path", "snapshot_sha256",
    "source_sha256", "report_sha256", "manifest_binding_sha256",
}
_CHECKPOINT_STATES = frozenset({
    "awaiting_model", "running", "cancelled", "interrupted", "failed", "partial",
    "completed", "completed_with_issues",
})
_SEALED_CHECKPOINT_STATES = frozenset({"completed", "completed_with_issues"})


class AnnualCheckpointError(ValueError):
    """A safe checkpoint storage or integrity failure."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AnnualCheckpointRecord:
    run_id: str
    sequence: int
    state: str
    is_partial: bool
    parent_run_id: str | None
    source_sha256: str
    report_sha256: str
    snapshot_sha256: str
    manifest_binding_sha256: str
    report: dict[str, Any]

    @property
    def metadata(self) -> dict[str, Any]:
        """The deliberately small API snapshot descriptor."""

        return {
            "state": self.state,
            "sequence": self.sequence,
            "is_partial": self.is_partial,
            "parent_run_id": self.parent_run_id,
        }


def replace_with_bounded_retry(source: Path, destination: Path) -> None:
    """Atomically replace a file, retrying only brief Windows sharing locks.

    The same temporary source is used for every attempt. Retries are bounded
    to six total calls and at most 310 ms of backoff; a persistent lock or
    permission error remains a hard publication failure.
    """

    for attempt in range(_ATOMIC_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            retryable = winerror in _ATOMIC_REPLACE_RETRYABLE_WINERRORS
            if not retryable or attempt >= len(_ATOMIC_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_ATOMIC_REPLACE_RETRY_DELAYS[attempt])
    raise AssertionError("atomic_replace_retry_loop_exhausted")


def save_annual_checkpoint(
    project_root: Path,
    run_id: str,
    snapshot: Mapping[str, Any],
) -> AnnualCheckpointRecord:
    """Append a checkpoint while holding a bounded cross-process writer lock."""

    checked_run_id = _checked_run_id(run_id)
    root = _project_root(project_root)
    run_dir = _safe_path(root, ("artifacts", "runs", checked_run_id), directory=True)
    with _checkpoint_writer_lock(run_dir):
        return _save_annual_checkpoint_locked(root, checked_run_id, snapshot)


def _save_annual_checkpoint_locked(
    project_root: Path,
    run_id: str,
    snapshot: Mapping[str, Any],
) -> AnnualCheckpointRecord:
    """Append an immutable report snapshot and atomically publish its pointer.

    The caller supplies exactly ``state``, ``is_partial``, ``parent_run_id``,
    ``source_sha256`` and ``report``. The sequence is allocated from the
    current validated pointer and cannot be supplied by the caller.
    """

    checked_run_id = _checked_run_id(run_id)
    if not isinstance(snapshot, Mapping) or set(snapshot) != {
        "state", "is_partial", "parent_run_id", "source_sha256", "report",
    }:
        raise AnnualCheckpointError("checkpoint_input_invalid", "年度检查点输入字段集合无效。", status_code=422)
    state = snapshot.get("state")
    is_partial = snapshot.get("is_partial")
    if (
        not isinstance(state, str)
        or state not in _CHECKPOINT_STATES
        or type(is_partial) is not bool
        or is_partial and state in _SEALED_CHECKPOINT_STATES
        or not is_partial and state not in _SEALED_CHECKPOINT_STATES
    ):
        raise AnnualCheckpointError("checkpoint_input_invalid", "年度检查点状态无效。", status_code=422)
    parent_run_id = snapshot.get("parent_run_id")
    if parent_run_id is not None:
        parent_run_id = _checked_run_id(parent_run_id)
        if parent_run_id == checked_run_id:
            raise AnnualCheckpointError("checkpoint_input_invalid", "年度检查点不能引用自身为父运行。", status_code=422)
    source_sha256 = snapshot.get("source_sha256")
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        raise AnnualCheckpointError("checkpoint_input_invalid", "年度检查点来源哈希无效。", status_code=422)
    report_value = snapshot.get("report")
    if not isinstance(report_value, Mapping):
        raise AnnualCheckpointError("checkpoint_input_invalid", "年度检查点报告结构无效。", status_code=422)
    report = dict(report_value)

    root = _project_root(project_root)
    manifest, binding, manifest_binding_sha256 = _load_manifest_binding(root, checked_run_id)
    if source_sha256 != binding["source_pdf_sha256"]:
        raise AnnualCheckpointError("checkpoint_source_mismatch", "年度检查点来源哈希与 manifest 不一致。")
    _require_report_identity(report, checked_run_id, manifest, source_sha256)
    actual_source_sha256 = _hash_checked_file(
        _safe_path(root, ("artifacts", "runs", checked_run_id, "source.pdf"), directory=False),
        _MAX_SOURCE_PDF_BYTES,
        missing_code="checkpoint_source_missing",
    )
    if actual_source_sha256 != source_sha256:
        raise AnnualCheckpointError("checkpoint_source_mismatch", "年度检查点原 PDF 与来源哈希不一致。")

    report_bytes = _canonical_json_bytes(report)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()

    run_dir = _safe_path(root, ("artifacts", "runs", checked_run_id), directory=True)
    report_dir = _safe_path(root, ("artifacts", "reports", checked_run_id), directory=True)
    checkpoints_dir = report_dir / "checkpoints"
    try:
        checkpoints_dir.mkdir(exist_ok=True)
    except OSError:
        raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点目录无法安全创建。") from None
    _safe_path(root, ("artifacts", "reports", checked_run_id, "checkpoints"), directory=True)

    pointer_parts = ("artifacts", "runs", checked_run_id, "checkpoint-current.json")
    current = _load_current_annual_checkpoint_with_verified_source(root, checked_run_id, source_sha256)
    if current is not None and not current.is_partial:
        raise AnnualCheckpointError("checkpoint_already_sealed", "年度检查点已封存，不能继续追加。")
    sequence = 1 if current is None else current.sequence + 1
    if sequence > _MAX_SEQUENCE:
        raise AnnualCheckpointError("checkpoint_sequence_exhausted", "年度检查点序号超出允许范围。")
    snapshot_nonce = uuid.uuid4().hex
    snapshot_path_rel = _snapshot_relative_path(checked_run_id, sequence, snapshot_nonce)
    snapshot_path = report_dir / "checkpoints" / f"snapshot-{sequence:08d}-{snapshot_nonce}.json"
    _safe_path(root, tuple(snapshot_path_rel.split("/")), directory=False, allow_missing=True)

    envelope = {
        "kind": _SNAPSHOT_KIND,
        "schema_version": 1,
        "run_id": checked_run_id,
        "sequence": sequence,
        "state": state,
        "is_partial": is_partial,
        "parent_run_id": parent_run_id,
        "source_sha256": source_sha256,
        "report_sha256": report_sha256,
        "manifest_binding_sha256": manifest_binding_sha256,
        "report": report,
    }
    snapshot_bytes = _canonical_json_bytes(envelope)
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    snapshot_temp = checkpoints_dir / f".snapshot-{uuid.uuid4().hex}.tmp"
    pointer_temp: Path | None = None
    try:
        _write_new_file(snapshot_temp, snapshot_bytes)
        # A hard link gives the immutable final name an atomic no-overwrite
        # publish step. The pointer is not made visible until this succeeds.
        try:
            os.link(snapshot_temp, snapshot_path)
        except FileExistsError:
            raise AnnualCheckpointError("checkpoint_snapshot_exists", "年度检查点快照不可覆盖。") from None
        except OSError:
            raise AnnualCheckpointError("checkpoint_snapshot_publish_failed", "年度检查点快照无法原子发布。") from None
        _unlink_owned_file(snapshot_temp)
        _read_checked_file(snapshot_path, _MAX_SNAPSHOT_BYTES, "checkpoint_snapshot_missing")

        pointer = {
            "kind": _POINTER_KIND,
            "schema_version": 1,
            "run_id": checked_run_id,
            "sequence": sequence,
            "snapshot_path": snapshot_path_rel,
            "snapshot_sha256": snapshot_sha256,
            "source_sha256": source_sha256,
            "report_sha256": report_sha256,
            "manifest_binding_sha256": manifest_binding_sha256,
        }
        pointer_bytes = _canonical_json_bytes(pointer)
        pointer_path = _safe_path(root, pointer_parts, directory=False, allow_missing=True)
        if pointer_path.exists():
            info = pointer_path.lstat()
            if _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
                raise AnnualCheckpointError("checkpoint_pointer_invalid", "年度检查点指针文件类型无效。")
        pointer_temp = run_dir / f".checkpoint-current-{uuid.uuid4().hex}.tmp"
        _write_new_file(pointer_temp, pointer_bytes)
        replace_with_bounded_retry(pointer_temp, pointer_path)
        pointer_temp = None
        _fsync_directory(run_dir)
    except AnnualCheckpointError:
        if pointer_temp is not None:
            _unlink_owned_file(pointer_temp)
        _unlink_owned_file(snapshot_temp)
        raise
    except OSError:
        if pointer_temp is not None:
            _unlink_owned_file(pointer_temp)
        _unlink_owned_file(snapshot_temp)
        raise AnnualCheckpointError("checkpoint_write_failed", "年度检查点无法安全保存。") from None

    return AnnualCheckpointRecord(
        run_id=checked_run_id,
        sequence=sequence,
        state=state,
        is_partial=is_partial,
        parent_run_id=parent_run_id,
        source_sha256=source_sha256,
        report_sha256=report_sha256,
        snapshot_sha256=snapshot_sha256,
        manifest_binding_sha256=manifest_binding_sha256,
        report=report,
    )


def load_current_annual_checkpoint(project_root: Path, run_id: str) -> AnnualCheckpointRecord | None:
    """Load and verify the checkpoint and independently hash its original PDF."""

    return _load_current_annual_checkpoint(project_root, run_id, verified_source_sha256=None)


def _load_current_annual_checkpoint_with_verified_source(
    project_root: Path,
    run_id: str,
    verified_source_sha256: str,
) -> AnnualCheckpointRecord | None:
    """Internal fast path for a reader that hashed the opened PDF itself."""

    if not isinstance(verified_source_sha256, str) or _SHA256.fullmatch(verified_source_sha256) is None:
        raise AnnualCheckpointError("checkpoint_source_mismatch", "年度检查点来源哈希无效。")
    return _load_current_annual_checkpoint(
        project_root,
        run_id,
        verified_source_sha256=verified_source_sha256,
    )


def _load_current_annual_checkpoint(
    project_root: Path,
    run_id: str,
    *,
    verified_source_sha256: str | None,
) -> AnnualCheckpointRecord | None:
    """Shared pointer reader; a digest shortcut is kept private to core readers."""

    checked_run_id = _checked_run_id(run_id)
    root = _project_root(project_root)
    _safe_path(root, ("artifacts", "runs", checked_run_id), directory=True)
    _safe_path(root, ("artifacts", "reports", checked_run_id), directory=True)
    manifest, binding, manifest_binding_sha256 = _load_manifest_binding(root, checked_run_id)
    source_sha256 = binding["source_pdf_sha256"]
    if verified_source_sha256 is not None and verified_source_sha256 != source_sha256:
        raise AnnualCheckpointError("checkpoint_source_mismatch", "年度检查点来源哈希与当前 PDF 不一致。")
    pointer_parts = ("artifacts", "runs", checked_run_id, "checkpoint-current.json")
    try:
        pointer_path = _safe_path(root, pointer_parts, directory=False, allow_missing=True)
    except AnnualCheckpointError:
        raise
    if not pointer_path.exists():
        return None
    pointer = _parse_json_object(_read_checked_file(pointer_path, _MAX_POINTER_BYTES, "checkpoint_pointer_missing"))
    if set(pointer) != _POINTER_FIELDS:
        raise AnnualCheckpointError("checkpoint_pointer_invalid", "年度检查点指针字段集合无效。", status_code=422)
    sequence = pointer.get("sequence")
    if (
        pointer.get("kind") != _POINTER_KIND
        or pointer.get("schema_version") != 1
        or pointer.get("run_id") != checked_run_id
        or type(sequence) is not int
        or not 1 <= sequence <= _MAX_SEQUENCE
        or not _is_allowed_snapshot_path(pointer.get("snapshot_path"), checked_run_id, sequence)
        or pointer.get("source_sha256") != source_sha256
        or pointer.get("manifest_binding_sha256") != manifest_binding_sha256
        or any(
            not isinstance(pointer.get(field), str) or _SHA256.fullmatch(pointer[field]) is None
            for field in ("snapshot_sha256", "source_sha256", "report_sha256", "manifest_binding_sha256")
        )
    ):
        raise AnnualCheckpointError("checkpoint_pointer_invalid", "年度检查点指针与当前归档绑定无效。", status_code=422)

    snapshot_parts = tuple(str(pointer["snapshot_path"]).split("/"))
    snapshot_path = _safe_path(root, snapshot_parts, directory=False)
    snapshot_bytes = _read_checked_file(snapshot_path, _MAX_SNAPSHOT_BYTES, "checkpoint_snapshot_missing")
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    if snapshot_sha256 != pointer["snapshot_sha256"]:
        raise AnnualCheckpointError("checkpoint_snapshot_hash_mismatch", "年度检查点快照哈希不匹配。")
    envelope = _parse_json_object(snapshot_bytes)
    if set(envelope) != _SNAPSHOT_FIELDS:
        raise AnnualCheckpointError("checkpoint_snapshot_invalid", "年度检查点快照字段集合无效。", status_code=422)
    state = envelope.get("state")
    parent_run_id = envelope.get("parent_run_id")
    if parent_run_id is not None:
        parent_run_id = _checked_run_id(parent_run_id)
    if (
        envelope.get("kind") != _SNAPSHOT_KIND
        or envelope.get("schema_version") != 1
        or envelope.get("run_id") != checked_run_id
        or envelope.get("sequence") != sequence
        or type(envelope.get("sequence")) is not int
        or not isinstance(state, str)
        or state not in _CHECKPOINT_STATES
        or type(envelope.get("is_partial")) is not bool
        or envelope.get("is_partial") and state in _SEALED_CHECKPOINT_STATES
        or not envelope.get("is_partial") and state not in _SEALED_CHECKPOINT_STATES
        or parent_run_id == checked_run_id
        or envelope.get("source_sha256") != source_sha256
        or envelope.get("report_sha256") != pointer["report_sha256"]
        or envelope.get("manifest_binding_sha256") != manifest_binding_sha256
    ):
        raise AnnualCheckpointError("checkpoint_snapshot_invalid", "年度检查点快照与当前归档绑定无效。", status_code=422)
    report = envelope.get("report")
    if not isinstance(report, dict):
        raise AnnualCheckpointError("checkpoint_snapshot_invalid", "年度检查点报告结构无效。", status_code=422)
    report_sha256 = hashlib.sha256(_canonical_json_bytes(report)).hexdigest()
    if report_sha256 != envelope["report_sha256"]:
        raise AnnualCheckpointError("checkpoint_report_hash_mismatch", "年度检查点报告哈希不匹配。")
    _require_report_identity(report, checked_run_id, manifest, source_sha256)

    # Validate the original PDF here for standalone callers. The strict report
    # reader supplies its already verified digest to avoid a second full read.
    if verified_source_sha256 is None:
        actual_source_sha256 = _hash_checked_file(
            _safe_path(root, ("artifacts", "runs", checked_run_id, "source.pdf"), directory=False),
            _MAX_SOURCE_PDF_BYTES,
            missing_code="checkpoint_source_missing",
        )
        if actual_source_sha256 != source_sha256:
            raise AnnualCheckpointError("checkpoint_source_mismatch", "年度检查点原 PDF 与来源哈希不一致。")
    return AnnualCheckpointRecord(
        run_id=checked_run_id,
        sequence=sequence,
        state=state,
        is_partial=envelope["is_partial"],
        parent_run_id=parent_run_id,
        source_sha256=source_sha256,
        report_sha256=report_sha256,
        snapshot_sha256=snapshot_sha256,
        manifest_binding_sha256=manifest_binding_sha256,
        report=report,
    )


def _checked_run_id(run_id: object) -> str:
    try:
        checked = validate_run_id(run_id)  # type: ignore[arg-type]
    except (PathBoundaryError, TypeError):
        raise AnnualCheckpointError("checkpoint_run_id_invalid", "年度检查点 run_id 格式无效。", status_code=400) from None
    stem = checked.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if checked.endswith(".") or stem in reserved:
        raise AnnualCheckpointError("checkpoint_run_id_invalid", "年度检查点 run_id 格式无效。", status_code=400)
    return checked


def _project_root(project_root: Path) -> Path:
    try:
        return Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AnnualCheckpointError("checkpoint_root_invalid", "年度检查点项目目录无法安全访问。") from None


def _load_manifest_binding(root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, str], str]:
    manifest_path = _safe_path(root, ("artifacts", "runs", run_id, "manifest.json"), directory=False)
    manifest = _parse_json_object(_read_checked_file(manifest_path, _MAX_MANIFEST_BYTES, "checkpoint_manifest_missing"))
    inputs = manifest.get("inputs")
    outputs = manifest.get("outputs")
    source_sha256 = inputs.get("source_pdf_sha256") if isinstance(inputs, Mapping) else None
    if (
        manifest.get("run_id") != run_id
        or manifest.get("status") not in {"running", "completed", "completed_with_issues", "failed", "interrupted"}
        or not isinstance(manifest.get("company_id"), str)
        or not manifest["company_id"]
        or type(manifest.get("report_year")) is not int
        or not 1900 <= manifest["report_year"] <= 2100
        or not isinstance(manifest.get("document_id"), str)
        or not manifest["document_id"]
        or not isinstance(inputs, Mapping)
        or not isinstance(outputs, Mapping)
        or inputs.get("source_pdf_archive_path") != f"runs/{run_id}/source.pdf"
        or outputs.get("run_dir") != f"runs/{run_id}"
        or outputs.get("report_dir") != f"reports/{run_id}"
        or outputs.get("report_json") != f"reports/{run_id}/report.json"
        or not isinstance(source_sha256, str)
        or _SHA256.fullmatch(source_sha256) is None
    ):
        raise AnnualCheckpointError("checkpoint_manifest_invalid", "年度检查点 manifest 身份或固定路径无效。", status_code=422)
    binding = {
        "run_id": run_id,
        "company_id": manifest["company_id"],
        "report_year": str(manifest["report_year"]),
        "document_id": manifest["document_id"],
        "source_pdf_archive_path": f"runs/{run_id}/source.pdf",
        "source_pdf_sha256": source_sha256,
        "run_dir": f"runs/{run_id}",
        "report_dir": f"reports/{run_id}",
        "report_json": f"reports/{run_id}/report.json",
    }
    return manifest, binding, hashlib.sha256(_canonical_json_bytes(binding)).hexdigest()


def _require_report_identity(report: Mapping[str, Any], run_id: str, manifest: Mapping[str, Any], source_sha256: str) -> None:
    if (
        report.get("kind") != "fintrace_annual_analysis_report"
        or report.get("run_id") != run_id
        or report.get("company_id") != manifest.get("company_id")
        or report.get("report_year") != manifest.get("report_year")
        or report.get("source_document_id") != manifest.get("document_id")
        or report.get("source_sha256") != source_sha256
    ):
        raise AnnualCheckpointError("checkpoint_report_identity_mismatch", "年度检查点报告身份与 manifest 不一致。")


def _snapshot_relative_path(run_id: str, sequence: int, nonce: str) -> str:
    return f"artifacts/reports/{run_id}/checkpoints/snapshot-{sequence:08d}-{nonce}.json"


def _is_allowed_snapshot_path(value: object, run_id: str, sequence: int) -> bool:
    return isinstance(value, str) and re.fullmatch(
        rf"artifacts/reports/{re.escape(run_id)}/checkpoints/snapshot-{sequence:08d}-[0-9a-f]{{32}}\.json",
        value,
    ) is not None


def _safe_path(
    root: Path,
    parts: tuple[str, ...],
    *,
    directory: bool,
    allow_missing: bool = False,
) -> Path:
    anchor = root.resolve(strict=True)
    current = anchor
    for index, part in enumerate(parts):
        if not part or part in {".", ".."} or "/" in part or "\\" in part or ":" in part:
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点路径组件无效。")
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing and index == len(parts) - 1:
                return current
            raise AnnualCheckpointError("checkpoint_path_missing", "年度检查点固定路径不存在。") from None
        except OSError:
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点路径无法安全访问。") from None
        if _is_reparse_point(info):
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点路径包含链接或 reparse point。")
        try:
            resolved = current.resolve(strict=True)
        except (OSError, RuntimeError):
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点路径无法安全解析。") from None
        if resolved == anchor or not resolved.is_relative_to(anchor):
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点路径超出项目目录。")
        is_final = index == len(parts) - 1
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点目录结构无效。")
        if is_final and directory and not stat.S_ISDIR(info.st_mode):
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点目录类型无效。")
        if is_final and not directory and not stat.S_ISREG(info.st_mode):
            raise AnnualCheckpointError("checkpoint_path_invalid", "年度检查点文件类型无效。")
    return current


def _read_checked_file(path: Path, limit: int, missing_code: str) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise AnnualCheckpointError(missing_code, "年度检查点文件不存在。") from None
    except OSError:
        raise AnnualCheckpointError("checkpoint_file_unreadable", "年度检查点文件无法读取。") from None
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise AnnualCheckpointError("checkpoint_file_invalid", "年度检查点文件类型或大小无效.", status_code=422)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
                raise AnnualCheckpointError("checkpoint_file_changed", "年度检查点文件在读取期间发生变化。")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                content = handle.read(limit + 1)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after) or len(content) > limit:
            raise AnnualCheckpointError("checkpoint_file_changed", "年度检查点文件在读取期间发生变化。")
    except AnnualCheckpointError:
        raise
    except OSError:
        raise AnnualCheckpointError("checkpoint_file_unreadable", "年度检查点文件无法读取。") from None
    return content


def _hash_checked_file(path: Path, limit: int, missing_code: str) -> str:
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise AnnualCheckpointError(missing_code, "年度检查点来源文件不存在。") from None
    except OSError:
        raise AnnualCheckpointError("checkpoint_source_unreadable", "年度检查点来源文件无法读取。") from None
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise AnnualCheckpointError("checkpoint_source_invalid", "年度检查点来源文件类型或大小无效。")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
                raise AnnualCheckpointError("checkpoint_source_changed", "年度检查点来源文件在读取期间发生变化。")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise AnnualCheckpointError("checkpoint_source_invalid", "年度检查点来源文件超过大小上限。")
                    digest.update(chunk)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after):
            raise AnnualCheckpointError("checkpoint_source_changed", "年度检查点来源文件在读取期间发生变化。")
    except AnnualCheckpointError:
        raise
    except OSError:
        raise AnnualCheckpointError("checkpoint_source_unreadable", "年度检查点来源文件无法读取。") from None
    return digest.hexdigest()


def _parse_json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise AnnualCheckpointError("checkpoint_json_invalid", "年度检查点 JSON 格式无效。", status_code=422) from None
    if not isinstance(value, dict):
        raise AnnualCheckpointError("checkpoint_json_invalid", "年度检查点 JSON 顶层必须是对象。", status_code=422)
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise AnnualCheckpointError("checkpoint_json_invalid", "年度检查点包含无法安全归档的值。", status_code=422) from None


@contextmanager
def _checkpoint_writer_lock(run_dir: Path) -> Iterator[None]:
    """Serialize sequence allocation and pointer publication across processes."""

    lock_path = run_dir / ".checkpoint-writer.lock"
    try:
        before = lock_path.lstat()
    except FileNotFoundError:
        before = None
    except OSError:
        raise AnnualCheckpointError("checkpoint_writer_lock_failed", "年度检查点写入锁无法安全访问。") from None
    if before is not None and (_is_reparse_point(before) or not stat.S_ISREG(before.st_mode)):
        raise AnnualCheckpointError("checkpoint_writer_lock_failed", "年度检查点写入锁类型无效。")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        opened = os.fstat(descriptor)
        if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode):
            os.close(descriptor)
            raise AnnualCheckpointError("checkpoint_writer_lock_failed", "年度检查点写入锁类型无效。")
        if before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(descriptor)
            raise AnnualCheckpointError("checkpoint_writer_lock_failed", "年度检查点写入锁在打开期间发生变化。")
        # Lock-file contents are not data. Windows and POSIX both permit locking
        # beyond EOF, so initialization must not truncate another writer's file.
        linked = lock_path.lstat()
        if (
            _is_reparse_point(linked) or not stat.S_ISREG(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            os.close(descriptor)
            raise AnnualCheckpointError("checkpoint_writer_lock_failed", "年度检查点写入锁在打开期间发生变化。")
    except AnnualCheckpointError:
        raise
    except OSError:
        raise AnnualCheckpointError("checkpoint_writer_lock_failed", "年度检查点写入锁无法打开。") from None

    acquired = False
    deadline = time.monotonic() + 3.0
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise AnnualCheckpointError(
                        "checkpoint_writer_busy",
                        "另一个年度检查点写入仍在进行，请稍后重试。",
                        status_code=409,
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _write_new_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass
            raise
    except FileExistsError:
        raise AnnualCheckpointError("checkpoint_temp_exists", "年度检查点临时文件名冲突。") from None


def _unlink_owned_file(path: Path) -> None:
    try:
        info = path.lstat()
        if not _is_reparse_point(info) and stat.S_ISREG(info.st_mode):
            path.unlink()
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _is_reparse_point(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
