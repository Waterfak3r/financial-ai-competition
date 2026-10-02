"""只读读取 v2 年度分析报告与独立核验 PDF 证据预览。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from finagent.core.safe_paths import PathBoundaryError, validate_run_id

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_REPORT_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_PDF_BYTES = 256 * 1024 * 1024
_MAX_PREVIEW_PIXELS = 12_000_000
_MAX_NARRATIVE_EVIDENCES = 8
_MAX_NARRATIVE_EVIDENCE_CHARS = 4000
_MAX_NARRATIVE_SNIPPET_CHARS = 1200
_REPARSE_POINT_ATTRIBUTE = 0x400
NARRATIVE_EVIDENCE_INTERPRETATION = "原文片段定位/模型观点引用，非独立因果核验"


class AnnualReportReadError(Exception):
    """可映射为安全 HTTP 错误的归档读取问题。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AnnualReportArchive:
    run_id: str
    report: dict[str, Any]
    manifest_summary: dict[str, Any]
    verification_evidences: tuple[dict[str, Any], ...]
    narrative_evidences: tuple[dict[str, Any], ...]
    source_pdf_bytes: bytes


def load_annual_report(project_root: Path, run_id: str) -> AnnualReportArchive:
    """读取正式 v2 归档，并逐次校验归档标识及原文 SHA256。"""

    checked_run_id = _checked_run_id(run_id)
    root = Path(project_root).resolve()
    _safe_archive_path(root, ("artifacts", "runs"), directory=True, missing_code="run_not_found")
    _safe_archive_path(
        root,
        ("artifacts", "runs", checked_run_id),
        directory=True,
        missing_code="run_not_found",
    )
    _safe_archive_path(root, ("artifacts", "reports"), directory=True, missing_code="report_not_found")
    _safe_archive_path(
        root,
        ("artifacts", "reports", checked_run_id),
        directory=True,
        missing_code="report_not_found",
    )

    manifest_path = _safe_archive_path(
        root,
        ("artifacts", "runs", checked_run_id, "manifest.json"),
        directory=False,
        missing_code="archive_incomplete",
    )
    source_path = _safe_archive_path(
        root,
        ("artifacts", "runs", checked_run_id, "source.pdf"),
        directory=False,
        missing_code="archive_incomplete",
    )
    report_path = _safe_archive_path(
        root,
        ("artifacts", "reports", checked_run_id, "report.json"),
        directory=False,
        missing_code="report_not_found",
    )
    manifest_bytes = _read_checked_file(manifest_path, _MAX_MANIFEST_BYTES, "archive_incomplete")
    source_pdf_bytes = _read_checked_file(source_path, _MAX_SOURCE_PDF_BYTES, "archive_incomplete")
    report_bytes = _read_checked_file(report_path, _MAX_REPORT_BYTES, "report_not_found")
    manifest = _parse_json_object(manifest_bytes, "manifest_invalid")
    report = _parse_json_object(report_bytes, "report_invalid")

    if report.get("kind") != "fintrace_annual_analysis_report":
        raise AnnualReportReadError(422, "report_invalid", "report.json 不是受支持的 FINTRACE 年度分析报告。")

    if not source_pdf_bytes.startswith(b"%PDF-"):
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 source.pdf 不是有效的 PDF 文件。")
    actual_source_sha256 = hashlib.sha256(source_pdf_bytes).hexdigest()
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    inputs = manifest.get("inputs")
    outputs = manifest.get("outputs")
    if not isinstance(inputs, Mapping) or not isinstance(outputs, Mapping):
        raise AnnualReportReadError(422, "manifest_invalid", "年度分析 manifest 缺少必要字段。")
    manifest_source_sha256 = inputs.get("source_pdf_sha256")
    if not isinstance(manifest_source_sha256, str) or _SHA256.fullmatch(manifest_source_sha256) is None:
        raise AnnualReportReadError(422, "manifest_invalid", "年度分析 manifest 的来源哈希格式无效。")

    expected_run_path = f"runs/{checked_run_id}"
    expected_report_dir = f"reports/{checked_run_id}"
    expected_source_path = f"{expected_run_path}/source.pdf"
    expected_report_path = f"{expected_report_dir}/report.json"
    if (
        manifest.get("run_id") != checked_run_id
        or manifest.get("status") not in {"completed", "completed_with_issues"}
        or inputs.get("source_pdf_archive_path") != expected_source_path
        or outputs.get("run_dir") != expected_run_path
        or outputs.get("report_dir") != expected_report_dir
        or outputs.get("report_json") != expected_report_path
    ):
        raise AnnualReportReadError(409, "archive_identity_mismatch", "manifest 与请求的年度分析归档标识或固定路径不一致。")

    if actual_source_sha256 != manifest_source_sha256.lower():
        raise AnnualReportReadError(409, "source_sha256_mismatch", "归档 PDF 与 manifest 记录的来源哈希不一致。")

    if (
        report.get("run_id") != checked_run_id
        or report.get("source_sha256") != manifest_source_sha256.lower()
        or report.get("company_id") != manifest.get("company_id")
        or report.get("report_year") != manifest.get("report_year")
        or report.get("source_document_id") != manifest.get("document_id")
        or not isinstance(manifest.get("company_id"), str)
        or not isinstance(manifest.get("document_id"), str)
        or type(manifest.get("report_year")) is not int
    ):
        raise AnnualReportReadError(409, "report_identity_mismatch", "报告与 manifest 的 run_id、来源哈希或文档元数据不一致。")

    verification_evidences = _collect_verified_evidences(report, manifest_source_sha256.lower())
    narrative_evidences = _collect_narrative_evidences(
        report,
        source_pdf_bytes,
        expected_document_id=manifest["document_id"],
        expected_sha256=manifest_source_sha256.lower(),
        verified_evidence_ids={item["evidence_id"] for item in verification_evidences},
    )
    raw_mode = manifest.get("mode")
    supported_modes = {"deterministic", "m3_screening", "model_investigation", "integrated_analysis"}
    manifest_mode = raw_mode if isinstance(raw_mode, str) and raw_mode in supported_modes else None
    if raw_mode is not None and manifest_mode is None:
        manifest_mode = "unknown"
    manifest_summary = {
        "run_id": checked_run_id,
        "status": manifest["status"],
        "mode": manifest_mode,
        "company_id": manifest["company_id"],
        "report_year": manifest["report_year"],
        "document_id": manifest["document_id"],
        "source_pdf_archive_path": expected_source_path,
        "report_json_path": expected_report_path,
        "source_pdf_sha256": actual_source_sha256,
        # Existing manifests do not persist a report-file hash. This is the
        # hash of the exact report.json bytes returned by this request.
        "report_sha256": report_sha256,
    }
    return AnnualReportArchive(
        run_id=checked_run_id,
        report=report,
        manifest_summary=manifest_summary,
        verification_evidences=verification_evidences,
        narrative_evidences=narrative_evidences,
        source_pdf_bytes=source_pdf_bytes,
    )


def render_evidence_preview(
    archive: AnnualReportArchive,
    evidence_id: str,
    *,
    focus: bool = False,
) -> tuple[bytes, int]:
    """只渲染 value_region 所在页，并仅标出该值的 bbox。页码从 1 开始。"""

    if not isinstance(evidence_id, str) or len(evidence_id) > 256:
        raise AnnualReportReadError(404, "evidence_not_found", "该独立核验证据不存在。")
    evidence = next(
        (item for item in archive.verification_evidences if item.get("evidence_id") == evidence_id),
        None,
    )
    if evidence is None:
        raise AnnualReportReadError(404, "evidence_not_found", "该独立核验证据不存在。")

    region = evidence.get("value_region")
    if not isinstance(region, Mapping):
        raise AnnualReportReadError(422, "evidence_region_invalid", "证据缺少有效的 value_region 坐标。")
    page_number = region.get("page")
    bbox = region.get("bbox")
    pdf_page = evidence.get("pdf_page")
    if (
        type(page_number) is not int
        or page_number < 1
        or type(pdf_page) is not int
        or pdf_page != page_number
        or not isinstance(bbox, Mapping)
    ):
        raise AnnualReportReadError(422, "evidence_region_invalid", "证据页码或 value_region 坐标无效。")
    coordinates: list[float] = []
    for key in ("x0", "y0", "x1", "y1"):
        value = bbox.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise AnnualReportReadError(422, "evidence_region_invalid", "证据 value_region 坐标无效。")
        coordinates.append(float(value))
    x0, y0, x1, y1 = coordinates
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise AnnualReportReadError(422, "evidence_region_invalid", "证据 value_region 坐标超出页面范围。")
    if focus:
        return _render_page_region_preview(
            archive.source_pdf_bytes,
            page_number,
            (x0, y0, x1, y1),
            focus=True,
            narrative=False,
        )
    return _render_page_region_preview(
        archive.source_pdf_bytes,
        page_number,
        (x0, y0, x1, y1),
        focus=False,
        narrative=False,
    )


def render_narrative_evidence_preview(
    archive: AnnualReportArchive,
    evidence_id: str,
    *,
    focus: bool = False,
) -> tuple[bytes, int]:
    """Render only a source-bound narrative block; this is not verification evidence."""

    if not isinstance(evidence_id, str) or len(evidence_id) > 256:
        raise AnnualReportReadError(404, "narrative_evidence_not_found", "该同源叙述证据不存在。")
    evidence = next(
        (item for item in archive.narrative_evidences if item.get("evidence_id") == evidence_id),
        None,
    )
    if evidence is None:
        raise AnnualReportReadError(404, "narrative_evidence_not_found", "该同源叙述证据不存在。")
    bbox = evidence.get("bbox")
    page_number = evidence.get("pdf_page")
    if type(page_number) is not int or not isinstance(bbox, Mapping):
        raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据的页码或坐标无效。")
    coordinates = tuple(bbox.get(key) for key in ("x0", "y0", "x1", "y1"))
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in coordinates):
        raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据坐标无效。")
    return _render_page_region_preview(
        archive.source_pdf_bytes,
        page_number,
        tuple(float(value) for value in coordinates),
        focus=focus,
        narrative=True,
    )


def _render_page_region_preview(
    source_pdf_bytes: bytes,
    page_number: int,
    coordinates: tuple[float, float, float, float],
    *,
    focus: bool,
    narrative: bool,
) -> tuple[bytes, int]:
    error_code = "narrative_evidence_region_invalid" if narrative else "evidence_region_invalid"
    try:
        import pymupdf

        document = pymupdf.open(stream=source_pdf_bytes, filetype="pdf")
    except Exception:
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 无法打开。") from None

    try:
        if document.needs_pass:
            raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 受密码保护，无法生成预览。")
        if page_number < 1 or page_number > document.page_count:
            raise AnnualReportReadError(422, error_code, "证据页码超出 PDF 页数。")
        page = document[page_number - 1]
        rect = pymupdf.Rect(*coordinates)
        visible_region = rect * page.rotation_matrix
        page_bounds = page.rect
        if (
            rect.x0 < 0
            or rect.y0 < 0
            or rect.x1 <= rect.x0
            or rect.y1 <= rect.y0
            or visible_region.x0 < -0.1
            or visible_region.y0 < -0.1
            or visible_region.x1 > page_bounds.x1 + 0.1
            or visible_region.y1 > page_bounds.y1 + 0.1
        ):
            raise AnnualReportReadError(422, error_code, "证据坐标与 PDF 页面范围不一致。")
        page.draw_rect(
            rect,
            color=(0.16, 0.31, 0.72) if narrative else (0.82, 0.12, 0.12),
            fill=(0.84, 0.89, 1.0) if narrative else (1.0, 0.92, 0.25),
            fill_opacity=0.22 if narrative else 0.35,
            width=1.4,
            overlay=True,
        )
        render_region = page_bounds
        if focus:
            margin_x = min(48.0, max(18.0, visible_region.width * 0.04))
            margin_y = min(36.0, max(14.0, visible_region.height * 0.8))
            padded_region = visible_region + (-margin_x, -margin_y, margin_x, margin_y)
            render_region = padded_region & page_bounds
        width, height = float(render_region.width), float(render_region.height)
        if width <= 0 or height <= 0:
            raise AnnualReportReadError(422, "source_pdf_invalid", "PDF 页面区域尺寸无效。")
        scale_ceiling = 3.0 if focus else 1.5
        scale = min(scale_ceiling, math.sqrt(_MAX_PREVIEW_PIXELS / (width * height)))
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=render_region if focus else None,
            alpha=False,
            annots=True,
        )
        return pixmap.tobytes("png"), page_number
    except AnnualReportReadError:
        raise
    except Exception:
        raise AnnualReportReadError(422, "preview_failed", "无法生成该证据的 PDF 页预览。") from None
    finally:
        document.close()


def _checked_run_id(run_id: str) -> str:
    try:
        checked = validate_run_id(run_id)
    except PathBoundaryError:
        raise AnnualReportReadError(400, "invalid_run_id", "run_id 格式无效。") from None
    # Reject names that normalize specially on Windows filesystems.
    stem = checked.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if checked.endswith(".") or stem in reserved:
        raise AnnualReportReadError(400, "invalid_run_id", "run_id 格式无效。")
    return checked


def _safe_archive_path(root: Path, parts: tuple[str, ...], *, directory: bool, missing_code: str) -> Path:
    anchor = root.resolve()
    current = anchor
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            _raise_missing(missing_code)
        except OSError:
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档路径无法安全读取。") from None
        if _is_reparse_point(info):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档包含符号链接或 reparse point。")
        try:
            resolved = current.resolve(strict=True)
        except OSError:
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档路径无法安全读取。") from None
        if resolved == anchor or not resolved.is_relative_to(anchor):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档路径超出项目目录。")
        is_final = index == len(parts) - 1
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档目录结构无效。")
        if is_final and directory and not stat.S_ISDIR(info.st_mode):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档目录结构无效。")
        if is_final and not directory and not stat.S_ISREG(info.st_mode):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件类型无效。")
    return current


def _raise_missing(code: str) -> None:
    if code == "run_not_found":
        raise AnnualReportReadError(404, code, "找不到该年度分析运行。")
    if code == "report_not_found":
        raise AnnualReportReadError(404, code, "找不到该运行的年度分析报告。")
    raise AnnualReportReadError(409, code, "年度分析归档不完整。")


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _read_checked_file(path: Path, limit: int, missing_code: str) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError:
        _raise_missing(missing_code)
    except OSError:
        raise AnnualReportReadError(409, "archive_file_unreadable", "年度分析归档文件无法读取。") from None
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件类型无效。")
    if before.st_size > limit:
        raise AnnualReportReadError(422, "archive_file_too_large", "年度分析归档文件超过允许大小。")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件在读取期间发生变化。")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = None
            data = stream.read(limit + 1)
        after = path.lstat()
        if _is_reparse_point(after) or _file_identity(before) != _file_identity(after):
            raise AnnualReportReadError(409, "archive_path_invalid", "年度分析归档文件在读取期间发生变化。")
    except AnnualReportReadError:
        raise
    except FileNotFoundError:
        _raise_missing(missing_code)
    except OSError:
        raise AnnualReportReadError(409, "archive_file_unreadable", "年度分析归档文件无法读取。") from None
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > limit:
        raise AnnualReportReadError(422, "archive_file_too_large", "年度分析归档文件超过允许大小。")
    return data


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _parse_json_object(data: bytes, error_code: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AnnualReportReadError(422, error_code, "年度分析归档 JSON 格式无效。") from None
    if not isinstance(payload, dict):
        raise AnnualReportReadError(422, error_code, "年度分析归档 JSON 顶层必须是对象。")
    return payload


def _collect_verified_evidences(report: Mapping[str, Any], expected_sha256: str) -> tuple[dict[str, Any], ...]:
    confirmed = report.get("confirmed")
    if not isinstance(confirmed, Mapping) or not isinstance(confirmed.get("metrics"), list):
        raise AnnualReportReadError(422, "report_invalid", "年度分析报告缺少有效的 confirmed 指标区。")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for metric in confirmed["metrics"]:
        if not isinstance(metric, Mapping):
            raise AnnualReportReadError(422, "report_invalid", "年度分析报告的 confirmed 指标结构无效。")
        if metric.get("source_sha256") != expected_sha256 or metric.get("source_document_id") != report.get("source_document_id"):
            raise AnnualReportReadError(409, "evidence_source_mismatch", "confirmed 指标与报告来源不一致。")
        verifications = metric.get("verifications", [])
        records = metric.get("verification_evidences", [])
        if not isinstance(verifications, list) or not isinstance(records, list):
            raise AnnualReportReadError(422, "report_invalid", "年度分析报告的核验引用结构无效。")
        verified_ids: set[str] = set()
        fact_id = metric.get("fact_id")
        for verification in verifications:
            if not isinstance(verification, Mapping):
                raise AnnualReportReadError(422, "report_invalid", "年度分析报告的核验记录结构无效。")
            ids = verification.get("evidence_ids", [])
            if not isinstance(ids, list):
                raise AnnualReportReadError(422, "report_invalid", "年度分析报告的核验证据引用结构无效。")
            if (
                verification.get("status") == "verified"
                and verification.get("target_type") == "financial_fact"
                and isinstance(fact_id, str)
                and verification.get("target_id") == fact_id
            ):
                verified_ids.update(item for item in ids if isinstance(item, str))
        for evidence in records:
            if not isinstance(evidence, Mapping):
                raise AnnualReportReadError(422, "report_invalid", "年度分析报告的 verification_evidences 结构无效。")
            evidence_id = evidence.get("evidence_id")
            # Extraction/legacy evidence and any unverified references never
            # enter this index, even if they are present in the archived JSON.
            if not isinstance(evidence_id, str) or evidence_id not in verified_ids:
                continue
            if evidence.get("source_sha256") != expected_sha256 or evidence.get("document_id") != report.get("source_document_id"):
                raise AnnualReportReadError(409, "evidence_source_mismatch", "独立核验证据与报告来源不一致。")
            normalized = dict(evidence)
            existing = evidence_by_id.get(evidence_id)
            if existing is not None and existing != normalized:
                raise AnnualReportReadError(409, "evidence_id_conflict", "报告中存在内容不一致的重复 evidence_id。")
            evidence_by_id[evidence_id] = normalized
    return tuple(evidence_by_id[key] for key in sorted(evidence_by_id))


def _collect_narrative_evidences(
    report: Mapping[str, Any],
    source_pdf_bytes: bytes,
    *,
    expected_document_id: str,
    expected_sha256: str,
    verified_evidence_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    """Return only cited narrative blocks that still match their archived PDF."""

    investigation = report.get("model_investigation")
    if not isinstance(investigation, Mapping):
        return ()
    # Old reports never stored the block text needed for safe narrative preview.
    if "narrative_evidences" not in investigation:
        return ()
    records = investigation.get("narrative_evidences")
    items = investigation.get("items")
    if not isinstance(records, list) or len(records) > _MAX_NARRATIVE_EVIDENCES:
        raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据归档结构无效。")
    if not isinstance(items, list):
        raise AnnualReportReadError(422, "report_invalid", "模型调查的引用列表结构无效。")

    cited_by: dict[str, set[str]] = {}
    for item in items:
        if not isinstance(item, Mapping) or item.get("status") != "interpretation":
            continue
        signal_id = item.get("signal_id")
        evidence_ids = item.get("narrative_evidence_ids")
        if not isinstance(signal_id, str) or not isinstance(evidence_ids, list):
            continue
        for evidence_id in evidence_ids:
            if isinstance(evidence_id, str) and evidence_id.startswith("nctx-"):
                cited_by.setdefault(evidence_id, set()).add(signal_id)
    if not cited_by:
        return ()

    try:
        import pymupdf

        document = pymupdf.open(stream=source_pdf_bytes, filetype="pdf")
    except Exception:
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 无法复核叙述证据。") from None

    evidence_by_id: dict[str, dict[str, Any]] = {}
    total_chars = 0
    try:
        if document.needs_pass:
            raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 受密码保护，无法复核叙述证据。")
        for record in records:
            if not isinstance(record, Mapping):
                raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据记录无效。")
            evidence_id = record.get("evidence_id")
            if not isinstance(evidence_id, str) or evidence_id not in cited_by:
                continue
            if evidence_id in verified_evidence_ids:
                raise AnnualReportReadError(409, "evidence_id_conflict", "叙述证据 ID 与独立核验证据 ID 冲突。")
            if record.get("source_document_id") != expected_document_id or record.get("source_sha256") != expected_sha256:
                raise AnnualReportReadError(409, "narrative_evidence_source_mismatch", "叙述证据与归档原文来源不一致。")
            if not evidence_id.startswith("nctx-"):
                raise AnnualReportReadError(422, "report_invalid", "叙述证据 ID 不属于叙述证据命名空间。")
            page_number = record.get("pdf_page")
            raw_text = record.get("raw_text")
            selection_reason = record.get("selection_reason")
            section_label = record.get("section_label")
            bbox = record.get("bbox")
            signal_ids = record.get("signal_ids")
            if (
                type(page_number) is not int
                or page_number < 1
                or page_number > document.page_count
                or not isinstance(raw_text, str)
                or not raw_text.strip()
                or len(raw_text) > _MAX_NARRATIVE_SNIPPET_CHARS
                or not isinstance(selection_reason, str)
                or not selection_reason.strip()
                or len(selection_reason) > 500
                or not isinstance(section_label, str)
                or section_label not in {"mda", "financial_notes"}
                or not isinstance(bbox, Mapping)
                or not isinstance(signal_ids, list)
                or any(not isinstance(value, str) for value in signal_ids)
            ):
                raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据字段无效。")
            coordinates: list[float] = []
            for key in ("x0", "y0", "x1", "y1"):
                value = bbox.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据坐标无效。")
                coordinates.append(float(value))
            x0, y0, x1, y1 = coordinates
            if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
                raise AnnualReportReadError(422, "narrative_evidence_region_invalid", "叙述证据坐标超出页面范围。")
            page = document[page_number - 1]
            source_block_matches = False
            for block in page.get_text("blocks"):
                if len(block) < 5 or str(block[4]).strip() != raw_text.strip():
                    continue
                if all(abs(float(actual) - expected) <= 0.1 for actual, expected in zip(block[:4], coordinates)):
                    source_block_matches = True
                    break
            if not source_block_matches:
                raise AnnualReportReadError(409, "narrative_evidence_source_mismatch", "叙述文本或坐标与归档 PDF 不一致。")
            total_chars += len(raw_text)
            if total_chars > _MAX_NARRATIVE_EVIDENCE_CHARS:
                raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据超过文本上限。")
            normalized = {
                "evidence_id": evidence_id,
                "source_document_id": expected_document_id,
                "source_sha256": expected_sha256,
                "signal_ids": list(dict.fromkeys(signal_ids)),
                "section_label": section_label,
                "pdf_page": page_number,
                "bbox": {key: coordinates[index] for index, key in enumerate(("x0", "y0", "x1", "y1"))},
                "raw_text": raw_text,
                "selection_reason": selection_reason,
                "cited_by_signal_ids": sorted(cited_by[evidence_id]),
            }
            existing = evidence_by_id.get(evidence_id)
            if existing is not None and existing != normalized:
                raise AnnualReportReadError(409, "evidence_id_conflict", "报告中存在内容不一致的重复叙述证据 ID。")
            evidence_by_id[evidence_id] = normalized
    finally:
        document.close()

    return tuple(evidence_by_id[key] for key in sorted(evidence_by_id))
