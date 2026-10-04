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

from finagent.core.sample_provenance import resolve_sample_provenance
from finagent.core.safe_paths import PathBoundaryError, validate_run_id
from finagent.api.narrative_reading_context import build_reading_context
from finagent.ingestion.parse_text_pdf import parse_text_pdf

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_REPORT_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_PDF_BYTES = 256 * 1024 * 1024
_MAX_PREVIEW_PIXELS = 12_000_000
_MAX_NARRATIVE_EVIDENCES = 8
_MAX_NARRATIVE_EVIDENCE_CHARS = 4000
_MAX_NARRATIVE_SNIPPET_CHARS = 1200
_MAX_DISCOVERY_FINDINGS = 768
_MAX_DISCOVERY_EVIDENCES = 3072
_MAX_DISCOVERY_EVIDENCE_CHARS = 4_000_000
_MAX_DISCOVERY_OPINION_CHARS = 1_200_000
_MAX_DISCOVERY_LIMITATIONS = 1_100
_MAX_DISCOVERY_QUOTE_CHARS = 300
_REPARSE_POINT_ATTRIBUTE = 0x400
NARRATIVE_EVIDENCE_INTERPRETATION = "原文片段定位/模型观点引用，非独立因果核验"
DISCOVERY_EVIDENCE_INTERPRETATION = "年报原文定位，模型发现未经独立语义核验"


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
    source_provenance: dict[str, object]
    # Appended with a default so existing positional constructors remain valid.
    discovery_evidences: tuple[dict[str, Any], ...] = ()


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
    discovery_evidences = _collect_discovery_evidences(
        report,
        source_path,
        expected_run_id=checked_run_id,
        expected_company_id=str(manifest["company_id"]),
        expected_report_year=manifest["report_year"],
        expected_document_id=manifest["document_id"],
        expected_sha256=manifest_source_sha256.lower(),
        nested_discovery=(
            report.get("model_investigation", {}).get("discovery")
            if isinstance(report.get("model_investigation"), Mapping)
            else None
        ),
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
    source_provenance = resolve_sample_provenance(
        actual_source_sha256,
        company_id=manifest["company_id"],
        report_year=manifest["report_year"],
        document_id=manifest["document_id"],
    )
    return AnnualReportArchive(
        run_id=checked_run_id,
        report=report,
        manifest_summary=manifest_summary,
        verification_evidences=verification_evidences,
        narrative_evidences=narrative_evidences,
        discovery_evidences=discovery_evidences,
        source_pdf_bytes=source_pdf_bytes,
        source_provenance=source_provenance,
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


def render_discovery_evidence_preview(
    archive: AnnualReportArchive,
    evidence_id: str,
    *,
    focus: bool = False,
) -> tuple[bytes, int]:
    """Render only an exact source-bound block cited by an unverified finding."""

    if not isinstance(evidence_id, str) or len(evidence_id) > 256:
        raise AnnualReportReadError(404, "discovery_evidence_not_found", "该开放式发现证据不存在。")
    evidence = next(
        (item for item in archive.discovery_evidences if item.get("evidence_id") == evidence_id),
        None,
    )
    if evidence is None:
        raise AnnualReportReadError(404, "discovery_evidence_not_found", "该开放式发现证据不存在。")
    bbox = evidence.get("bbox")
    page_number = evidence.get("pdf_page")
    if type(page_number) is not int or not isinstance(bbox, Mapping):
        raise AnnualReportReadError(422, "discovery_evidence_region_invalid", "发现证据的页码或坐标无效。")
    coordinates = tuple(bbox.get(key) for key in ("x0", "y0", "x1", "y1"))
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in coordinates
    ):
        raise AnnualReportReadError(422, "discovery_evidence_region_invalid", "发现证据坐标无效。")
    return _render_page_region_preview(
        archive.source_pdf_bytes,
        page_number,
        tuple(float(value) for value in coordinates),
        focus=focus,
        narrative=False,
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


def _collect_discovery_evidences(
    report: Mapping[str, Any],
    source_pdf_path: Path,
    *,
    expected_run_id: str,
    expected_company_id: str,
    expected_report_year: int,
    expected_document_id: str,
    expected_sha256: str,
    nested_discovery: object,
) -> tuple[dict[str, Any], ...]:
    """Rebuild cited discovery evidence from the exact archived source PDF."""

    top_present = "model_discovery" in report
    investigation = report.get("model_investigation")
    nested_present = isinstance(investigation, Mapping) and "discovery" in investigation
    if not top_present:
        if nested_present:
            raise AnnualReportReadError(
                409,
                "annual_discovery_layer_mismatch",
                "年度开放式发现缺少顶层来源绑定记录。",
            )
        return ()
    layer = report.get("model_discovery")
    if not isinstance(layer, Mapping):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现结构无效。")
    layer_fields = {
        "kind", "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
        "status", "prompt_version", "model_called", "model_call_count",
        "model_call_attempt_count", "coverage", "findings", "evidences", "audit_artifacts",
        "limitations", "reason", "fraud_conclusion",
    }
    if set(layer) != layer_fields:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现字段集合无效。")
    if nested_present and nested_discovery != layer:
        raise AnnualReportReadError(
            409,
            "annual_discovery_layer_mismatch",
            "模型调查附录与顶层开放式发现不一致。",
        )
    expected_identity = {
        "run_id": expected_run_id,
        "company_id": expected_company_id,
        "report_year": expected_report_year,
        "source_document_id": expected_document_id,
        "source_sha256": expected_sha256,
    }
    if layer.get("kind") != "fintrace_annual_discovery" or any(
        layer.get(key) != value for key, value in expected_identity.items()
    ):
        raise AnnualReportReadError(
            409,
            "annual_discovery_identity_mismatch",
            "年度开放式发现与当前归档来源身份不一致。",
        )
    if layer.get("prompt_version") != "annual_discovery_v1":
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现提示词版本无效。")
    status = layer.get("status")
    if not isinstance(status, str) or status not in {"completed", "partial", "failed", "not_called"}:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现状态无效。")
    reason = layer.get("reason")
    if reason is not None and (
        not isinstance(reason, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,99}", reason) is None
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现原因代码无效。")
    coverage = layer.get("coverage")
    if not isinstance(coverage, Mapping):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖记录无效。")
    _validate_discovery_coverage(coverage, status)
    if layer.get("fraud_conclusion") is not None:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现不能包含舞弊结论。")
    called = layer.get("model_called")
    call_count = layer.get("model_call_count")
    attempt_count = layer.get("model_call_attempt_count")
    if (
        not isinstance(called, bool)
        or type(call_count) is not int
        or type(attempt_count) is not int
        or call_count < 0
        or attempt_count < call_count
        or called != (attempt_count > 0)
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现模型调用计数无效。")
    if status == "not_called" and attempt_count != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "未调用状态不能包含模型请求。")
    if status == "not_called" and call_count != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "未调用状态不能包含模型响应。")
    if coverage["source_status"] == "parsed" and (
        attempt_count != coverage["processed_batches"]
        or coverage["successful_batches"] > call_count
        or call_count > attempt_count
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "已解析来源的请求/响应数与结束批次不一致。")
    if status == "completed" and (
        call_count != coverage["planned_batches"]
        or attempt_count != coverage["planned_batches"]
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "完成状态的模型调用数与批次数不一致。")

    raw_findings = layer.get("findings")
    raw_evidences = layer.get("evidences")
    limitations = layer.get("limitations")
    if (
        not isinstance(raw_findings, list)
        or len(raw_findings) > _MAX_DISCOVERY_FINDINGS
        or not isinstance(raw_evidences, list)
        or len(raw_evidences) > _MAX_DISCOVERY_EVIDENCES
        or not isinstance(limitations, list)
        or len(limitations) > _MAX_DISCOVERY_LIMITATIONS
        or any(not isinstance(item, str) or len(item) > 400 for item in limitations)
        or sum(len(item) for item in limitations if isinstance(item, str)) > _MAX_DISCOVERY_LIMITATIONS * 400
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现条目超过结构或字符上限。")
    if coverage["successful_batches"] == 0 and (raw_findings or raw_evidences):
        raise AnnualReportReadError(
            422,
            "annual_discovery_invalid",
            "没有成功处理的批次时不能归档模型发现或引文证据。",
        )

    audit_artifacts = layer.get("audit_artifacts")
    if not isinstance(audit_artifacts, list) or len(audit_artifacts) > 128:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计索引无效。")
    for audit in audit_artifacts:
        if not isinstance(audit, Mapping) or set(audit) != {
            "batch_index", "audit_dir", "request_path", "response_path", "failure_path", "status", "prompt_version"
        }:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计记录字段无效。")
        batch_index = audit.get("batch_index")
        audit_status = audit.get("status")
        if (
            type(batch_index) is not int or batch_index < 1 or batch_index > 128
            or not isinstance(audit_status, str)
            or audit_status not in {"started", "succeeded", "failed", "incomplete"}
            or audit.get("prompt_version") != "annual_discovery_v1"
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计记录值无效。")
        audit_dir = audit.get("audit_dir")
        if audit_dir is not None and (
            not isinstance(audit_dir, str) or not _safe_audit_relative_path(audit_dir)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计路径无效。")
        for path_field in ("request_path", "response_path", "failure_path"):
            value = audit.get(path_field)
            if value is not None and (not isinstance(value, str) or not _safe_audit_relative_path(value)):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现审计文件路径无效。")

    evidence_records: dict[str, Mapping[str, Any]] = {}
    total_chars = 0
    for record in raw_evidences:
        if not isinstance(record, Mapping):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据结构无效。")
        if set(record) != {
            "evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index",
            "start_char", "bbox", "raw_text",
        }:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据字段集合无效。")
        evidence_id = record.get("evidence_id")
        raw_text = record.get("raw_text")
        bbox = record.get("bbox")
        if (
            not isinstance(evidence_id, str)
            or not re.fullmatch(r"adisc-[0-9a-f]{12}-p[1-9][0-9]*-b[0-9]+-c[0-9]+-[0-9a-f]{12}", evidence_id)
            or evidence_id in evidence_records
            or record.get("source_document_id") != expected_document_id
            or record.get("source_sha256") != expected_sha256
            or type(record.get("pdf_page")) is not int
            or record["pdf_page"] < 1
            or type(record.get("block_index")) is not int
            or record["block_index"] < 0
            or type(record.get("start_char")) is not int
            or record["start_char"] < 0
            or not isinstance(raw_text, str)
            or not raw_text.strip()
            or len(raw_text) > 1200
            or not isinstance(bbox, Mapping)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据字段无效。")
        coordinates: dict[str, float] = {}
        for key in ("x0", "y0", "x1", "y1"):
            value = bbox.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据坐标无效。")
            coordinates[key] = float(value)
        if coordinates["x0"] < 0 or coordinates["y0"] < 0 or coordinates["x1"] <= coordinates["x0"] or coordinates["y1"] <= coordinates["y0"]:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据坐标范围无效。")
        if evidence_id != _annual_discovery_evidence_id(
            expected_sha256,
            record["pdf_page"],
            record["block_index"],
            record["start_char"],
        ):
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现证据 ID 与来源坐标不一致。")
        total_chars += len(raw_text)
        if total_chars > _MAX_DISCOVERY_EVIDENCE_CHARS:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现证据超过专用字符上限。")
        evidence_records[evidence_id] = record

    cited_by: dict[str, set[str]] = {}
    quotes_by_evidence: dict[str, list[dict[str, str]]] = {}
    finding_ids: set[str] = set()
    total_opinion_chars = 0
    total_quote_chars = 0
    for finding in raw_findings:
        if not isinstance(finding, Mapping):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现条目结构无效。")
        if set(finding) != {
            "finding_id", "title", "observation", "why_it_matters", "follow_up", "priority",
            "classification", "verification_status", "evidence_ids", "citations",
        }:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现字段集合无效。")
        finding_id = finding.get("finding_id")
        priority = finding.get("priority")
        if (
            not isinstance(finding_id, str)
            or not re.fullmatch(r"disc:[0-9a-f]{12}-[0-9a-f]{20}", finding_id)
            or not finding_id.startswith(f"disc:{expected_sha256[:12]}-")
            or finding_id in finding_ids
            or finding.get("classification") != "unverified_model_finding"
            or finding.get("verification_status") != "unverified"
            or not isinstance(priority, str)
            or priority not in {"high", "medium", "low"}
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现条目标识或状态无效。")
        finding_ids.add(finding_id)
        for field, limit in (("title", 100), ("observation", 600), ("why_it_matters", 400), ("follow_up", 400)):
            value = finding.get(field)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > limit
                or re.search(r"[0-9]", value)
                or any(ord(char) < 32 for char in value)
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现文本超过字段上限。")
            total_opinion_chars += len(value)
        if total_opinion_chars > _MAX_DISCOVERY_OPINION_CHARS:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现文本超过专用总字符上限。")
        citations = finding.get("citations")
        evidence_ids = finding.get("evidence_ids")
        if (
            not isinstance(citations, list)
            or not 1 <= len(citations) <= 4
            or not isinstance(evidence_ids, list)
            or len(evidence_ids) != len(citations)
            or any(not isinstance(item, str) for item in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现引用列表无效。")
        cited_ids: set[str] = set()
        for citation in citations:
            if not isinstance(citation, Mapping) or set(citation) != {"evidence_id", "quote"}:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现原文引用结构无效。")
            evidence_id = citation.get("evidence_id")
            quote = citation.get("quote")
            source = evidence_records.get(evidence_id) if isinstance(evidence_id, str) else None
            if (
                source is None
                or not isinstance(quote, str)
                or not quote.strip()
                or len(quote) > _MAX_DISCOVERY_QUOTE_CHARS
                or len(quote.strip()) < 2
                or quote not in str(source.get("raw_text", ""))
            ):
                raise AnnualReportReadError(409, "annual_discovery_quote_mismatch", "开放式发现引用与当前 PDF 原文不一致。")
            total_quote_chars += len(quote)
            if total_quote_chars > _MAX_DISCOVERY_FINDINGS * 4 * _MAX_DISCOVERY_QUOTE_CHARS:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现引用超过专用字符上限。")
            cited_ids.add(evidence_id)
            cited_by.setdefault(evidence_id, set()).add(finding_id)
            quotes_by_evidence.setdefault(evidence_id, []).append(
                {"finding_id": finding_id, "quote": quote}
            )
        if cited_ids != set(evidence_ids):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现引用 ID 与证据 ID 列表不一致。")
    if set(evidence_records) != set(cited_by):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "开放式发现包含未被实际引用的证据。")
    if not evidence_records:
        return ()

    try:
        parsed = parse_text_pdf(source_pdf_path, expected_document_id)
    except Exception:
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 无法复核开放式发现证据。") from None
    if parsed.source_sha256 != expected_sha256:
        raise AnnualReportReadError(409, "source_sha256_mismatch", "重新解析 PDF 的来源哈希与归档记录不一致。")
    pages = {page.page_number: page for page in parsed.pages}
    normalized: list[dict[str, Any]] = []
    for evidence_id, record in evidence_records.items():
        page = pages.get(record["pdf_page"])
        block_index = record["block_index"]
        if page is None or block_index >= len(page.blocks):
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现证据页码或文字块不存在。")
        block = page.blocks[block_index]
        raw_text = str(record["raw_text"])
        start_char = int(record["start_char"])
        if block.block_index != block_index or block.text[start_char : start_char + len(raw_text)] != raw_text:
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现片段与解析后的原文位置不一致。")
        bbox = record["bbox"]
        expected_box = (block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1)
        actual_box = tuple(float(bbox[key]) for key in ("x0", "y0", "x1", "y1"))
        if any(abs(actual - expected) > 0.01 for actual, expected in zip(actual_box, expected_box)):
            raise AnnualReportReadError(409, "annual_discovery_evidence_mismatch", "开放式发现坐标与 PDF 解析结果不一致。")
        normalized.append({
            "evidence_id": evidence_id,
            "source_document_id": expected_document_id,
            "source_sha256": expected_sha256,
            "pdf_page": record["pdf_page"],
            "block_index": block_index,
            "start_char": start_char,
            "bbox": {key: actual_box[index] for index, key in enumerate(("x0", "y0", "x1", "y1"))},
            "raw_text": raw_text,
            "finding_ids": sorted(cited_by[evidence_id]),
            "citations": sorted(quotes_by_evidence[evidence_id], key=lambda item: (item["finding_id"], item["quote"])),
        })
    return tuple(sorted(normalized, key=lambda item: item["evidence_id"]))


def _annual_discovery_evidence_id(source_sha256: str, page: int, block_index: int, start_char: int) -> str:
    material = f"{source_sha256}\n{page}\n{block_index}\n{start_char}".encode("ascii")
    digest = hashlib.sha256(material).hexdigest()[:12]
    return f"adisc-{source_sha256[:12]}-p{page}-b{block_index}-c{start_char}-{digest}"


def _validate_discovery_coverage(coverage: Mapping[str, Any], status: str) -> None:
    fields = {
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches",
        "processed_batches", "successful_batches", "failed_batches", "batch_results",
        "processed_chars", "attempted_chars", "unattempted_batches", "unattempted_chars",
        "planned_chars", "omitted_chars", "unread_pages", "failed_pages", "no_text_pages",
        "image_pages", "unread_image_pages", "truncated", "complete", "source_status",
        "content_status", "time_budget_exhausted", "text_only_scope",
    }
    if set(coverage) != fields:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖字段集合无效。")
    integer_fields = (
        "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches",
        "processed_batches", "successful_batches", "failed_batches", "processed_chars",
        "attempted_chars", "unattempted_batches", "unattempted_chars", "planned_chars",
        "omitted_chars",
    )
    numbers: dict[str, int] = {}
    for field in integer_fields:
        value = coverage.get(field)
        if type(value) is not int or value < 0:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖计数无效。")
        numbers[field] = value
    for field in ("truncated", "complete", "time_budget_exhausted"):
        if type(coverage.get(field)) is not bool:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖状态无效。")
    source_status = coverage.get("source_status")
    content_status = coverage.get("content_status")
    if not isinstance(source_status, str) or source_status not in {"parsed", "unavailable"}:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现来源状态无效。")
    if not isinstance(content_status, str) or content_status not in {"text_available", "no_extractable_text", "unavailable"}:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现内容状态无效。")
    if not isinstance(coverage.get("text_only_scope"), str) or len(coverage["text_only_scope"]) > 200:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现范围说明无效。")

    page_list_fields = ("unread_pages", "failed_pages", "no_text_pages", "image_pages", "unread_image_pages")
    pages: dict[str, list[int]] = {}
    for field in page_list_fields:
        value = coverage.get(field)
        if (
            not isinstance(value, list)
            or len(value) > numbers["total_pages"]
            or any(type(page) is not int or page < 1 or page > numbers["total_pages"] for page in value)
            or len(set(value)) != len(value)
            or value != sorted(value)
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖页码列表无效。")
        pages[field] = value
    if (
        numbers["text_pages"] > numbers["total_pages"]
        or numbers["text_pages"] + len(pages["no_text_pages"]) != numbers["total_pages"]
        or not set(pages["unread_image_pages"]).issubset(pages["image_pages"])
        or not set(pages["unread_image_pages"]).issubset(pages["no_text_pages"])
        or not set(pages["failed_pages"]).issubset(pages["unread_pages"])
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现覆盖页面关系无效。")

    if numbers["planned_batches"] > 128 or numbers["processed_batches"] > numbers["planned_batches"]:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现批次计数越界。")
    if (
        numbers["successful_batches"] + numbers["failed_batches"] != numbers["processed_batches"]
        or numbers["unattempted_batches"] != numbers["planned_batches"] - numbers["processed_batches"]
        or numbers["unattempted_chars"] > numbers["planned_chars"]
        or numbers["attempted_chars"] > numbers["planned_chars"]
        or numbers["processed_chars"] > numbers["attempted_chars"]
        or numbers["processed_chars"] > numbers["planned_chars"]
        or numbers["planned_chars"] > numbers["total_chars"]
        or numbers["omitted_chars"] != max(numbers["total_chars"] - numbers["planned_chars"], 0) + numbers["unattempted_chars"]
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "年度开放式发现批次与字符计数关系无效。")
    if content_status == "text_available" and numbers["total_chars"] <= 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "可提取文字状态与字符计数不一致。")
    if content_status == "no_extractable_text" and numbers["total_chars"] != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "无可提取文字状态与字符计数不一致。")
    if source_status == "unavailable":
        if (
            content_status != "unavailable"
            or any(numbers[field] != 0 for field in integer_fields)
            or any(pages[field] for field in page_list_fields)
            or coverage["complete"]
            or coverage["truncated"]
            or coverage["time_budget_exhausted"]
            or coverage.get("batch_results") != []
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "不可用来源不能包含虚构的批次覆盖。")
        if status not in {"failed", "not_called"}:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "不可用来源与发现状态不一致。")
    else:
        if content_status == "unavailable":
            raise AnnualReportReadError(422, "annual_discovery_invalid", "已解析来源不能使用不可用内容状态。")
        batch_results = coverage.get("batch_results")
        if not isinstance(batch_results, list) or len(batch_results) != numbers["planned_batches"]:
            raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖批次结果数与计划不一致。")
        seen_indices: set[int] = set()
        result_chars = {"succeeded": 0, "failed": 0, "unattempted": 0}
        result_pages: dict[str, set[int]] = {key: set() for key in result_chars}
        for result in batch_results:
            if not isinstance(result, Mapping) or set(result) != {"batch_index", "status", "reason", "pages", "raw_chars"}:
                raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖批次结果字段无效。")
            index = result.get("batch_index")
            batch_status = result.get("status")
            raw_chars = result.get("raw_chars")
            batch_pages = result.get("pages")
            reason = result.get("reason")
            if (
                type(index) is not int or index < 1 or index > numbers["planned_batches"]
                or index in seen_indices
                or not isinstance(batch_status, str) or batch_status not in result_chars
                or type(raw_chars) is not int or raw_chars <= 0 or raw_chars > 12_000
                or not isinstance(batch_pages, list) or not batch_pages
                or any(type(page) is not int or page < 1 or page > numbers["total_pages"] for page in batch_pages)
                or len(set(batch_pages)) != len(batch_pages)
                or batch_pages != sorted(batch_pages)
                or (batch_status == "succeeded" and reason is not None)
                or (
                    batch_status != "succeeded"
                    and (
                        not isinstance(reason, str)
                        or re.fullmatch(r"[a-z][a-z0-9_]{0,99}", reason) is None
                    )
                )
            ):
                raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖批次结果值无效。")
            seen_indices.add(index)
            result_chars[batch_status] += raw_chars
            result_pages[batch_status].update(batch_pages)
        if (
            seen_indices != set(range(1, numbers["planned_batches"] + 1))
            or sum(result_chars.values()) != numbers["planned_chars"]
            or result_chars["succeeded"] != numbers["processed_chars"]
            or result_chars["unattempted"] != numbers["unattempted_chars"]
            or len([r for r in batch_results if r["status"] == "succeeded"]) != numbers["successful_batches"]
            or len([r for r in batch_results if r["status"] == "failed"]) != numbers["failed_batches"]
            or len([r for r in batch_results if r["status"] == "unattempted"]) != numbers["unattempted_batches"]
            or not result_pages["failed"].issubset(pages["failed_pages"])
            or not result_pages["failed"].issubset(pages["unread_pages"])
            or not result_pages["unattempted"].issubset(pages["unread_pages"])
            or result_pages["failed"] != set(pages["failed_pages"])
            or numbers["attempted_chars"] != result_chars["succeeded"] + result_chars["failed"]
            or any(
                (index <= numbers["processed_batches"]) == (result["status"] == "unattempted")
                for index, result in ((item["batch_index"], item) for item in batch_results)
            )
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "批次结果与覆盖汇总不一致。")
        if coverage["truncated"] != bool(
            numbers["omitted_chars"] or numbers["unattempted_batches"] or numbers["unattempted_chars"]
        ):
            raise AnnualReportReadError(422, "annual_discovery_invalid", "截断标志与字符/图片页缺口不一致。")

    expected_complete = (
        source_status == "parsed"
        and content_status == "text_available"
        and numbers["total_chars"] > 0
        and not coverage["truncated"]
        and not pages["unread_image_pages"]
        and numbers["planned_chars"] == numbers["total_chars"]
        and numbers["processed_batches"] == numbers["planned_batches"]
        and numbers["failed_batches"] == 0
        and numbers["successful_batches"] == numbers["planned_batches"]
        and numbers["processed_chars"] == numbers["planned_chars"]
        and not pages["unread_pages"]
    )
    if coverage["complete"] != expected_complete:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "覆盖完整标志与批次/字符/页面计数不一致。")
    if status == "completed" and not expected_complete:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "完成状态与全文覆盖记录不一致。")
    if status == "partial" and (expected_complete or numbers["successful_batches"] == 0):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "部分完成状态缺少成功批次或覆盖缺口。")
    if status == "failed" and source_status == "parsed" and (
        numbers["failed_batches"] == 0 or numbers["successful_batches"] != 0
    ):
        raise AnnualReportReadError(422, "annual_discovery_invalid", "失败状态与已尝试批次结果不一致。")
    if status == "not_called" and source_status == "parsed" and numbers["processed_batches"] != 0:
        raise AnnualReportReadError(422, "annual_discovery_invalid", "未调用状态不能包含已结束的批次。")


def _safe_audit_relative_path(value: str) -> bool:
    if len(value) > 500 or "\\" in value or value.startswith("/"):
        return False
    parts = value.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "artifacts"
        and parts[1] == "runs"
        and all(part not in {"", ".", ".."} for part in parts)
    )


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
            citation_block_index: int | None = None
            source_blocks = page.get_text("blocks", sort=True)
            for block_index, block in enumerate(source_blocks):
                if len(block) < 5 or str(block[4]).strip() != raw_text.strip():
                    continue
                if all(abs(float(actual) - expected) <= 0.1 for actual, expected in zip(block[:4], coordinates)):
                    source_block_matches = True
                    citation_block_index = block_index
                    break
            if not source_block_matches:
                raise AnnualReportReadError(409, "narrative_evidence_source_mismatch", "叙述文本或坐标与归档 PDF 不一致。")
            assert citation_block_index is not None
            total_chars += len(raw_text)
            if total_chars > _MAX_NARRATIVE_EVIDENCE_CHARS:
                raise AnnualReportReadError(422, "report_invalid", "模型调查的叙述证据超过文本上限。")
            reading_context = build_reading_context(
                source_blocks,
                citation_block_index,
                pdf_page=page_number,
                page_width=float(page.mediabox.width),
                source_sha256=expected_sha256,
            )
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
                # This is rebuilt from the hash-checked PDF on every read. Any
                # similarly named field inside report.json is ignored.
                "reading_context": reading_context,
            }
            existing = evidence_by_id.get(evidence_id)
            if existing is not None and existing != normalized:
                raise AnnualReportReadError(409, "evidence_id_conflict", "报告中存在内容不一致的重复叙述证据 ID。")
            evidence_by_id[evidence_id] = normalized
    finally:
        document.close()

    return tuple(evidence_by_id[key] for key in sorted(evidence_by_id))
