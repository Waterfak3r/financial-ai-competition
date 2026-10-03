"""从受控 PDF 来源清单提取季度/半年/前三季/全年累计值。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import pymupdf
except ImportError:  # pragma: no cover - 项目依赖通常以 pymupdf 名称安装
    import fitz as pymupdf

from finagent.ingestion.parse_text_pdf import parse_text_pdf
from finagent.schemas.financial_fact_v2 import SourceRegion, TableCellEvidence
from finagent.schemas.quarterly import CumulativeObservation, QuarterlySourceDocument
from finagent.schemas.text_pdf import PdfBBox

class QuarterlyIngestionError(ValueError):
    pass


_ROOT = Path(__file__).resolve().parents[4]
_AMOUNT = re.compile(r"^[+-]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?$|^\((?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?\)$")
_PERIOD_SUFFIX = {"Q1": "第一季度", "H1": "半年度", "9M": "前三季度", "FY": "年度"}
_METRICS = {
    "revenue": ("income_statement", "营业收入"),
    "net_profit_parent": ("income_statement", "归属于母公司股东的净利润"),
    "operating_cash_flow": ("cash_flow_statement", "经营活动产生的现金流量净额"),
}
_STATEMENT_TITLES = (
    "合并资产负债表", "合并利润表", "合并现金流量表",
    "母公司资产负债表", "母公司利润表", "母公司现金流量表",
)


@dataclass(frozen=True, slots=True)
class QuarterlyIngestion:
    manifest_path: str
    manifest: dict[str, Any]
    documents: tuple[QuarterlySourceDocument, ...]
    document_paths: dict[str, Path]
    observations: tuple[CumulativeObservation, ...]
    parsed_pdfs: dict[str, dict[str, Any]]
    issues: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_path": self.manifest_path,
            "dataset_id": self.manifest.get("dataset_id"),
            "nature": self.manifest.get("nature"),
            "use_conditions": self.manifest.get("use_conditions"),
            "documents": [item.to_dict() for item in self.documents],
            "observations": [item.to_dict() for item in self.observations],
            "issues": list(self.issues),
        }


def load_quarterly_manifest(manifest_path: Path, *, project_root: Path = _ROOT) -> QuarterlyIngestion:
    project_root = project_root.resolve()
    candidate = manifest_path.expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    candidate = Path(os.path.abspath(candidate))
    data_root = project_root / "data"
    if not _inside(candidate, data_root):
        raise QuarterlyIngestionError("季度 manifest 必须位于 data/ 内且不能是符号链接。")
    cursor = project_root
    for part in candidate.relative_to(project_root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise QuarterlyIngestionError("季度 manifest 必须位于 data/ 内且不能是符号链接。")
    manifest_path = candidate.resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarterlyIngestionError(f"无法读取季度 manifest：{type(exc).__name__}。") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise QuarterlyIngestionError("季度 manifest schema_version 必须是 1。")
    if not isinstance(manifest.get("documents"), list) or not manifest["documents"]:
        raise QuarterlyIngestionError("季度 manifest 必须含非空 documents。")
    company_id = _required_text(manifest.get("company_id"), "company_id")
    dataset_nature = _required_text(manifest.get("nature"), "nature")
    dataset_use = _required_text(manifest.get("use_conditions"), "use_conditions")
    ids: set[str] = set()
    hashes: set[str] = set()
    docs: list[QuarterlySourceDocument] = []
    paths: dict[str, Path] = {}
    observations: list[CumulativeObservation] = []
    parsed: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, str]] = []
    for raw in manifest["documents"]:
        document = _parse_manifest_document(raw, project_root=project_root, manifest_company=company_id)
        if document.document_id in ids or document.source_sha256.lower() in hashes:
            raise QuarterlyIngestionError("manifest 含重复 document_id 或重复 PDF 来源哈希。")
        ids.add(document.document_id)
        hashes.add(document.source_sha256.lower())
        pdf_path = _resolve_source_path(document.source_pdf, project_root=project_root)
        actual_hash = _sha256(pdf_path)
        if actual_hash.lower() != document.source_sha256.lower():
            raise QuarterlyIngestionError(f"來源哈希不匹配：{document.document_id}。")
        docs.append(document)
        paths[document.document_id] = pdf_path
        extracted, parsed_pdf, found_issues = extract_quarterly_pdf(pdf_path, document)
        observations.extend(extracted)
        parsed[document.document_id] = parsed_pdf
        issues.extend(found_issues)
    return QuarterlyIngestion(
        manifest_path=_relative(manifest_path, project_root),
        manifest={**manifest, "nature": dataset_nature, "use_conditions": dataset_use},
        documents=tuple(docs),
        document_paths=paths,
        observations=tuple(observations),
        parsed_pdfs=parsed,
        issues=tuple(issues),
    )


def extract_quarterly_pdf(
    pdf_path: Path,
    document: QuarterlySourceDocument,
) -> tuple[tuple[CumulativeObservation, ...], dict[str, Any], tuple[dict[str, str], ...]]:
    """提取合并利润表和现金流量表中的累计列；支持范围由实际表格证据约束。"""

    parsed_pdf = parse_text_pdf(pdf_path, document.document_id)
    if parsed_pdf.source_sha256.lower() != document.source_sha256.lower():
        raise QuarterlyIngestionError(f"PDF 哈希在解析时发生变化：{document.document_id}。")
    doc = pymupdf.open(pdf_path)
    try:
        first_page = doc[0].get_text("text")
        _validate_document_identity(first_page, document)
        observations: list[CumulativeObservation] = []
        issues: list[dict[str, str]] = []
        statement_specs = (("income_statement", "合并利润表", ("revenue", "net_profit_parent")),
                           ("cash_flow_statement", "合并现金流量表", ("operating_cash_flow",)))
        for statement_type, title, metric_ids in statement_specs:
            table = _find_statement(doc, title, document)
            if table is None:
                for metric_id in metric_ids:
                    issues.append(_issue(document.document_id, metric_id, "missing_consolidated_statement"))
                continue
            start_page, title_rect, end = table
            header_text = f"{document.report_year}年{_PERIOD_SUFFIX[document.report_period]}"
            prior_header = f"{document.report_year - 1}年{_PERIOD_SUFFIX[document.report_period]}"
            current_header = _find_header(doc[start_page], header_text, title_rect)
            comparative_header = _find_header(doc[start_page], prior_header, title_rect)
            if current_header is None or comparative_header is None:
                for metric_id in metric_ids:
                    issues.append(_issue(document.document_id, metric_id, "missing_current_or_comparative_column_header"))
                continue
            unit_info = _find_unit(doc[start_page], title_rect)
            if unit_info is None:
                for metric_id in metric_ids:
                    issues.append(_issue(document.document_id, metric_id, "missing_currency_or_unit"))
                continue
            unit, currency, multiplier, unit_rect = unit_info
            title_region = _region(doc[start_page], title_rect, title, start_page + 1)
            unit_region = _region(doc[start_page], unit_rect, unit, start_page + 1)
            for metric_id in metric_ids:
                row_query = _METRICS[metric_id][1]
                matches = _find_metric_rows(doc, row_query, start_page, title_rect, end)
                resolved = _observations_for_metric(
                    matches=matches,
                    metric_id=metric_id,
                    statement_type=statement_type,
                    document=document,
                    doc=doc,
                    headers={"current": current_header, "comparative": comparative_header},
                    unit=unit,
                    currency=currency,
                    multiplier=multiplier,
                    title_region=title_region,
                    unit_region=unit_region,
                )
                if not resolved:
                    issues.append(_issue(document.document_id, metric_id, "metric_row_or_unique_amount_not_found"))
                else:
                    observations.extend(resolved)
        return tuple(observations), parsed_pdf.to_dict(), tuple(issues)
    finally:
        doc.close()


def _parse_manifest_document(raw: object, *, project_root: Path, manifest_company: str) -> QuarterlySourceDocument:
    if not isinstance(raw, dict):
        raise QuarterlyIngestionError("documents 项必须是对象。")
    company_id = _required_text(raw.get("company_id"), "document.company_id")
    if company_id != manifest_company:
        raise QuarterlyIngestionError("同一 manifest 不能混合不同公司。")
    period = raw.get("report_period")
    if period not in _PERIOD_SUFFIX:
        raise QuarterlyIngestionError("report_period 只接受 Q1/H1/9M/FY。")
    sha = _required_text(raw.get("source_sha256"), "source_sha256")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        raise QuarterlyIngestionError("source_sha256 必须是 64 位十六进制文本。")
    try:
        pub_date = date.fromisoformat(_required_text(raw.get("publication_date"), "publication_date"))
    except ValueError as exc:
        raise QuarterlyIngestionError("publication_date 必须使用 YYYY-MM-DD。") from exc
    report_year = raw.get("report_year")
    if type(report_year) is not int or not 1900 <= report_year <= 2100:
        raise QuarterlyIngestionError("report_year 必须是 1900 到 2100 的整数。")
    source_pdf = _required_text(raw.get("source_pdf"), "source_pdf")
    _resolve_source_path(source_pdf, project_root=project_root)
    return QuarterlySourceDocument(
        document_id=_required_text(raw.get("document_id"), "document_id"),
        company_id=company_id,
        source_pdf=source_pdf.replace("\\", "/"),
        source_sha256=sha.lower(),
        source_url=_required_text(raw.get("source_url"), "source_url"),
        publication_date=pub_date,
        report_year=report_year,
        report_period=period,
        nature=_required_text(raw.get("nature"), "nature") or _required_text(raw.get("report_type"), "report_type"),
        use_conditions=_required_text(raw.get("use_conditions"), "use_conditions"),
    )


def _resolve_source_path(value: str, *, project_root: Path) -> Path:
    raw = Path(value.replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts:
        raise QuarterlyIngestionError("source_pdf 必须使用工作区内的相对路径且不能含 ..。")
    resolved = (project_root / raw).resolve()
    allowed_roots = (project_root / "data" / "raw", project_root / "data" / "samples" / "quarterly")
    if not any(_inside(resolved, candidate.resolve()) for candidate in allowed_roots):
        raise QuarterlyIngestionError("source_pdf 必须位于 data/raw/ 或 data/samples/quarterly/。")
    current = project_root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise QuarterlyIngestionError("source_pdf 路径不能经过符号链接。")
    if not resolved.is_file() or resolved.suffix.lower() != ".pdf":
        raise QuarterlyIngestionError("source_pdf 必须指向存在的 PDF 文件。")
    return resolved


def _validate_document_identity(text: str, document: QuarterlySourceDocument) -> None:
    period_title = {
        "Q1": "第一季度报告",
        "H1": "半年度报告",
        "9M": "第三季度报告",
        "FY": "年度报告",
    }[document.report_period]
    if str(document.report_year) not in text or period_title not in text:
        raise QuarterlyIngestionError(f"PDF 首页未能独立确认报告年份/类型：{document.document_id}。")
    if document.company_id not in text:
        raise QuarterlyIngestionError(f"PDF 首页未能独立确认 company_id：{document.document_id}。")


def _find_statement(doc: Any, title: str, document: QuarterlySourceDocument):
    headings: list[tuple[int, Any, str]] = []
    for page_index, page in enumerate(doc):
        for name in _STATEMENT_TITLES:
            for rect in page.search_for(name):
                headings.append((page_index, rect, name))
    headings.sort(key=lambda item: (item[0], item[1].y0))
    target = [(index, rect) for index, rect, name in headings if name == title]
    if not target:
        return None
    start_page, title_rect = target[0]
    following = [
        (index, rect)
        for index, rect, _ in headings
        if index > start_page or (index == start_page and rect.y0 > title_rect.y0)
    ]
    end = following[0] if following else (len(doc), None)
    return start_page, title_rect, end


def _find_header(page: Any, text: str, title_rect: Any):
    found = [rect for rect in page.search_for(text) if rect.y0 > title_rect.y1]
    if found:
        return min(found, key=lambda rect: rect.y0)
    return _search_normalized_text_rect(page, text, min_y=title_rect.y1)


def _find_unit(page: Any, title_rect: Any):
    for query, unit, multiplier in (("单位：元", "元", "1"), ("单位：万元", "万元", "10000"), ("单位：亿元", "亿元", "100000000")):
        found = [rect for rect in page.search_for(query) if rect.y0 >= title_rect.y0 - 12 and rect.y0 < title_rect.y1 + 70]
        if not found:
            continue
        unit_rect = min(found, key=lambda rect: abs(rect.y0 - title_rect.y0))
        page_text = page.get_text("text")
        currency = "CNY" if "币种：人民币" in page_text or "币种:人民币" in page_text else None
        if currency is not None:
            return unit, currency, multiplier, unit_rect
    return None


def _find_metric_rows(doc: Any, query: str, start_page: int, title_rect: Any, end):
    end_page, end_rect = end
    matches: list[tuple[int, Any]] = []
    for page_index in range(start_page, min(end_page + 1, len(doc))):
        page = doc[page_index]
        for rect in _search_text_rects(page, query):
            if page_index == start_page and rect.y0 < title_rect.y0:
                continue
            if page_index == end_page and end_rect is not None and rect.y0 >= end_rect.y0:
                continue
            matches.append((page_index, rect))
    return matches


def _observations_for_metric(*, matches, metric_id, statement_type, document, doc, headers, unit, currency, multiplier, title_region, unit_region):
    candidates: dict[str, list[tuple[str, int, Any, Any, str]]] = {"current": [], "comparative": []}
    for page_index, row_rect in matches:
        page = doc[page_index]
        for role, header_rect in headers.items():
            amount = _amount_near_row(page, row_rect, header_rect)
            if amount is not None:
                candidates[role].append((amount[0], page_index, row_rect, amount[1], amount[2]))
    resolved: list[CumulativeObservation] = []
    for role, report_year in (("current", document.report_year), ("comparative", document.report_year - 1)):
        rows = candidates[role]
        values = {item[0] for item in rows}
        if not rows or len(values) != 1:
            return ()
        raw, page_index, row_rect, value_rect, row_text = rows[0]
        page = doc[page_index]
        parsed_value = _amount_decimal(raw) * Decimal(multiplier)
        start, end = _period_dates(report_year, document.report_period)
        evidence_id = f"qev-{document.document_id}-{metric_id}-{report_year}-{role}"
        row_region = _region(page, row_rect, row_text or row_rect_text(page, row_rect), page_index + 1)
        value_region = _region(page, value_rect, raw, page_index + 1)
        column_region = _region(page, headers[role], page.get_text("text", clip=headers[role]).strip(), page_index + 1)
        evidence = TableCellEvidence(
            evidence_id=evidence_id,
            document_id=document.document_id,
            source_sha256=document.source_sha256,
            pdf_page=page_index + 1,
            printed_page=None,
            table_title=title_region.text,
            row_label=row_region.text,
            column_label=column_region.text,
            value_raw=raw,
            value_normalized=format(parsed_value, "f"),
            unit=unit,
            currency=currency,
            period_start=start,
            period_end=end,
            period_type="duration",
            value_region=value_region,
            row_region=row_region,
            column_region=column_region,
            title_region=title_region,
            unit_region=unit_region,
            extraction_method="pymupdf_text_search_and_row_column_geometry",
            surrounding_text=row_text,
        )
        resolved.append(CumulativeObservation(
            observation_id=f"qobs-{document.document_id}-{metric_id}-{report_year}-{role}",
            metric_id=metric_id,
            company_id=document.company_id,
            report_year=document.report_year,
            report_period=document.report_period,
            observation_year=report_year,
            column_role=role,
            basis="year_to_date",
            statement_type=statement_type,
            scope="consolidated",
            unit_multiplier=multiplier,
            evidence=evidence,
        ))
    return tuple(resolved)


def _amount_near_row(page: Any, row_rect: Any, header_rect: Any):
    center_y = (row_rect.y0 + row_rect.y1) / 2
    header_x = (header_rect.x0 + header_rect.x1) / 2
    words = page.get_text("words")
    candidates = []
    for word in words:
        x0, y0, x1, y1, token = word[:5]
        if not _AMOUNT.fullmatch(token) or abs((y0 + y1) / 2 - center_y) > 12.0:
            continue
        if x1 < row_rect.x1 - 5:
            continue
        candidates.append((abs((x0 + x1) / 2 - header_x), token, (x0, y0, x1, y1)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    if candidates[0][0] > 70 or (len(candidates) > 1 and abs(candidates[1][0] - candidates[0][0]) < 0.1):
        return None
    return candidates[0][1], _rect(candidates[0][2]), row_rect_text(page, row_rect)


def _region(page: Any, rect: Any, text: str, page_number: int) -> SourceRegion:
    return SourceRegion(text=text or "原文区域", page=page_number, bbox=PdfBBox(*[round(float(value), 2) for value in rect]))


def row_rect_text(page: Any, rect: Any) -> str:
    return " ".join(page.get_text("text", clip=rect).split())


def _search_text_rects(page: Any, query: str):
    found = page.search_for(query)
    if found:
        return found
    rect = _search_normalized_text_rect(page, query)
    return [rect] if rect is not None else []


def _search_normalized_text_rect(page: Any, query: str, *, min_y: float = -1.0):
    target = re.sub(r"\s+", "", query)
    candidates = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            chars: list[str] = []
            boxes: list[Any] = []
            for span in line.get("spans", []):
                box = pymupdf.Rect(*span["bbox"])
                for char in span.get("text", ""):
                    if char.isspace():
                        continue
                    chars.append(char)
                    boxes.append(box)
            normalized = "".join(chars)
            index = normalized.find(target)
            line_rect = pymupdf.Rect(*line["bbox"])
            if index < 0 or line_rect.y0 <= min_y:
                continue
            selected = boxes[index:index + len(target)]
            if selected:
                candidates.append((line_rect.y0, pymupdf.Rect(
                    min(box.x0 for box in selected), min(box.y0 for box in selected),
                    max(box.x1 for box in selected), max(box.y1 for box in selected),
                )))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _rect(values):
    return pymupdf.Rect(*values)


def _period_dates(year: int, period: str):
    end = {"Q1": date(year, 3, 31), "H1": date(year, 6, 30), "9M": date(year, 9, 30), "FY": date(year, 12, 31)}[period]
    return date(year, 1, 1), end


def _amount_decimal(value: str) -> Decimal:
    cleaned = value.replace(",", "").replace("，", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return Decimal(cleaned)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise QuarterlyIngestionError("路径必须位于项目工作区内。")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuarterlyIngestionError(f"{name} 必须是非空字符串。")
    return value.strip()


def _issue(document_id: str, metric_id: str, code: str) -> dict[str, str]:
    return {"document_id": document_id, "metric_id": metric_id, "code": code}
