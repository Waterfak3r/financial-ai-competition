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
_REPARSE_POINT_ATTRIBUTE = 0x400


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
    manifest_summary = {
        "run_id": checked_run_id,
        "status": manifest["status"],
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
        source_pdf_bytes=source_pdf_bytes,
    )


def render_evidence_preview(archive: AnnualReportArchive, evidence_id: str) -> tuple[bytes, int]:
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

    try:
        import pymupdf

        document = pymupdf.open(stream=archive.source_pdf_bytes, filetype="pdf")
    except Exception:
        raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 无法打开。") from None

    try:
        if document.needs_pass:
            raise AnnualReportReadError(422, "source_pdf_invalid", "归档 PDF 受密码保护，无法生成预览。")
        if page_number > document.page_count:
            raise AnnualReportReadError(422, "evidence_region_invalid", "证据页码超出 PDF 页数。")
        page = document[page_number - 1]
        rect = pymupdf.Rect(x0, y0, x1, y1)
        page_bounds = page.rect
        if rect.x1 > page_bounds.x1 or rect.y1 > page_bounds.y1:
            raise AnnualReportReadError(422, "evidence_region_invalid", "证据 value_region 坐标超出 PDF 页面范围。")

        # Independent evidence records may also carry earlier-page title or
        # column-header coordinates. The preview intentionally uses only the
        # value bbox and its 1-based page number above.
        page.draw_rect(
            rect,
            color=(0.82, 0.12, 0.12),
            fill=(1.0, 0.92, 0.25),
            fill_opacity=0.35,
            width=1.4,
            overlay=True,
        )
        width, height = float(page.rect.width), float(page.rect.height)
        if width <= 0 or height <= 0:
            raise AnnualReportReadError(422, "source_pdf_invalid", "PDF 页面尺寸无效。")
        scale = min(1.5, math.sqrt(_MAX_PREVIEW_PIXELS / (width * height)))
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True)
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
