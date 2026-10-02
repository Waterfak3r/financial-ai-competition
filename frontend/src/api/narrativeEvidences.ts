import type { EvidencePreviewImage } from "./annualReports";
import { AnnualReportApiError } from "./annualReports";
import type { PdfBox } from "../types/annualReport";
import {
  NARRATIVE_EVIDENCE_INTERPRETATION_CODE,
  NARRATIVE_EVIDENCE_INTERPRETATION_LABEL,
} from "../types/narrativeEvidence";
import type { NarrativeEvidence, NarrativeEvidenceResponse } from "../types/narrativeEvidence";

const ANNUAL_ANALYSIS_PATH = "/api/v1/annual-analyses";
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;
const EVIDENCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

export async function loadNarrativeEvidences(
  runId: string,
  expectedDocumentId: string,
  expectedSourceSha256: string,
): Promise<NarrativeEvidenceResponse> {
  if (!RUN_ID_PATTERN.test(runId)) throw new AnnualReportApiError("运行 ID 格式无效。");
  const response = await fetch(`${ANNUAL_ANALYSIS_PATH}/${encodeURIComponent(runId)}/narrative-evidences`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualReportApiError("无法连接原文片段定位服务。请确认本地后端仍在运行。");
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const failure = detailMessage(response.status, payload, "读取原文片段定位");
    throw new AnnualReportApiError(failure);
  }
  const record = requireRecord(payload, "原文片段响应");
  const responseRunId = requireString(record.run_id, "run_id");
  const sourceDocumentId = requireString(record.source_document_id, "source_document_id");
  const sourceSha256 = requireString(record.source_sha256, "source_sha256");
  const interpretationLabel = requireString(record.interpretation_label, "interpretation_label");
  if (
    responseRunId !== runId ||
    sourceDocumentId !== expectedDocumentId ||
    sourceSha256.toLowerCase() !== expectedSourceSha256.toLowerCase() ||
    !SHA256_PATTERN.test(sourceSha256) ||
    interpretationLabel !== NARRATIVE_EVIDENCE_INTERPRETATION_LABEL
  ) {
    throw new AnnualReportApiError("原文片段定位的运行、来源或解释性质与当前报告不一致，未显示该响应。");
  }
  if (!Array.isArray(record.items)) throw invalidResponse("原文片段列表格式无效。");
  const items = record.items.map((item, index) => parseNarrativeEvidence(
    item,
    `items[${index}]`,
    sourceDocumentId,
    sourceSha256,
  ));
  const ids = new Set<string>();
  for (const item of items) {
    if (ids.has(item.evidence_id)) throw invalidResponse("原文片段列表包含重复 evidence_id。");
    ids.add(item.evidence_id);
  }
  return {
    run_id: responseRunId,
    source_document_id: sourceDocumentId,
    source_sha256: sourceSha256.toLowerCase(),
    interpretation_label: NARRATIVE_EVIDENCE_INTERPRETATION_LABEL,
    items,
  };
}

export async function loadNarrativeEvidencePreview(
  runId: string,
  evidence: NarrativeEvidence,
  expectedReportSha256: string,
  focus: boolean,
): Promise<EvidencePreviewImage> {
  if (!RUN_ID_PATTERN.test(runId) || !EVIDENCE_ID_PATTERN.test(evidence.evidence_id)) {
    throw new AnnualReportApiError("运行 ID 或原文片段 ID 格式无效。");
  }
  const url = `${ANNUAL_ANALYSIS_PATH}/${encodeURIComponent(runId)}/narrative-evidences/${encodeURIComponent(evidence.evidence_id)}/preview.png?focus=${String(focus)}`;
  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "image/png" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualReportApiError("无法连接原文片段页图服务。请确认本地后端仍在运行。");
  });
  if (!response.ok) {
    const payload = await readJson(response);
    throw new AnnualReportApiError(detailMessage(response.status, payload, "读取原文片段页图"), response.status);
  }
  if ((response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase() !== "image/png") {
    throw new AnnualReportApiError("原文片段预览响应不是 PNG 图片，已停止显示。", response.status);
  }
  const pageHeader = response.headers.get("X-PDF-Page");
  const pageNumber = pageHeader === null ? NaN : Number(pageHeader);
  if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber !== evidence.pdf_page) {
    throw new AnnualReportApiError("原文片段页图页码与定位记录不一致，已停止显示。", response.status);
  }
  if (response.headers.get("X-Evidence-Kind") !== "narrative_context") {
    throw new AnnualReportApiError("服务未将此页图标记为叙述片段，已停止显示。", response.status);
  }
  const expectedMode = focus ? "focus" : "full_page";
  if (response.headers.get("X-Preview-Mode") !== expectedMode) {
    throw new AnnualReportApiError("原文片段页图范围与请求不一致，已停止显示。", response.status);
  }
  if (response.headers.get("X-Evidence-Interpretation-Code") !== NARRATIVE_EVIDENCE_INTERPRETATION_CODE) {
    throw new AnnualReportApiError("服务未返回原文片段的解释性质标记，已停止显示。", response.status);
  }
  const reportSha256 = response.headers.get("X-Report-SHA256");
  if (
    reportSha256 === null || !SHA256_PATTERN.test(reportSha256) ||
    reportSha256.toLowerCase() !== expectedReportSha256.toLowerCase()
  ) {
    throw new AnnualReportApiError("原文片段页图对应的报告版本与当前报告不一致，已停止显示。", response.status);
  }
  const region = parseRegion(response.headers.get("X-Preview-Region"), evidence.bbox, response.status);
  const blob = await response.blob();
  const signature = new Uint8Array(await blob.slice(0, PNG_SIGNATURE.length).arrayBuffer());
  if (blob.size <= PNG_SIGNATURE.length || PNG_SIGNATURE.some((byte, index) => signature[index] !== byte)) {
    throw new AnnualReportApiError("原文片段预览内容无法验证为 PNG，已停止显示。", response.status);
  }
  return { blob, pageNumber, mode: expectedMode, region };
}

function parseNarrativeEvidence(
  value: unknown,
  path: string,
  expectedDocumentId: string,
  expectedSourceSha256: string,
): NarrativeEvidence {
  const record = requireRecord(value, path);
  const evidenceId = requireString(record.evidence_id, `${path}.evidence_id`);
  const documentId = requireString(record.source_document_id, `${path}.source_document_id`);
  const sourceSha256 = requireString(record.source_sha256, `${path}.source_sha256`);
  const page = requireInteger(record.pdf_page, `${path}.pdf_page`);
  const rawText = requireString(record.raw_text, `${path}.raw_text`);
  if (
    !EVIDENCE_ID_PATTERN.test(evidenceId) ||
    documentId !== expectedDocumentId ||
    sourceSha256.toLowerCase() !== expectedSourceSha256.toLowerCase() ||
    page < 1 || rawText.trim() === ""
  ) throw invalidResponse(`${path} 的来源绑定或正文格式无效。`);
  return {
    evidence_id: evidenceId,
    source_document_id: documentId,
    source_sha256: sourceSha256.toLowerCase(),
    signal_ids: readStringArray(record.signal_ids, `${path}.signal_ids`),
    section_label: requireString(record.section_label, `${path}.section_label`),
    pdf_page: page,
    bbox: readBox(record.bbox, `${path}.bbox`),
    raw_text: rawText,
    selection_reason: requireString(record.selection_reason, `${path}.selection_reason`),
    cited_by_signal_ids: readStringArray(record.cited_by_signal_ids, `${path}.cited_by_signal_ids`),
    preview_url: requireString(record.preview_url, `${path}.preview_url`),
  };
}

function parseRegion(header: string | null, expected: PdfBox, status: number) {
  if (header === null) throw new AnnualReportApiError("原文片段预览缺少区域元数据，已停止显示。", status);
  try {
    const record = requireRecord(JSON.parse(header) as unknown, "preview region");
    const bbox = readBox(record.bbox, "preview region.bbox");
    if (record.coordinate_space !== "unrotated_pdf_points" || !boxesMatch(bbox, expected)) throw new Error();
    return { bbox, coordinateSpace: "unrotated_pdf_points" as const };
  } catch {
    throw new AnnualReportApiError("原文片段预览区域与定位记录不一致，已停止显示。", status);
  }
}

function boxesMatch(left: PdfBox, right: PdfBox): boolean {
  return (["x0", "y0", "x1", "y1"] as const).every((key) => Math.abs(left[key] - right[key]) <= 0.01);
}

function readBox(value: unknown, path: string): PdfBox {
  const record = requireRecord(value, path);
  const box = {
    x0: requireFiniteNumber(record.x0, `${path}.x0`),
    y0: requireFiniteNumber(record.y0, `${path}.y0`),
    x1: requireFiniteNumber(record.x1, `${path}.x1`),
    y1: requireFiniteNumber(record.y1, `${path}.y1`),
  };
  if (box.x0 < 0 || box.y0 < 0 || box.x1 <= box.x0 || box.y1 <= box.y0) throw invalidResponse(`${path} 坐标无效。`);
  return box;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.trim() === "") return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new AnnualReportApiError(`服务返回的内容不是有效 JSON（HTTP ${response.status}）。`, response.status);
  }
}

function detailMessage(status: number, payload: unknown, action: string): string {
  const detail = objectValue(payload)?.detail;
  const detailRecord = objectValue(detail);
  if (typeof detailRecord?.message === "string" && detailRecord.message.trim() !== "") return detailRecord.message;
  if (typeof detail === "string" && detail.trim() !== "") return detail;
  return `${action}失败（HTTP ${status}）。`;
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  const record = objectValue(value);
  if (record === null) throw invalidResponse(`${path}格式无效。`);
  return record;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== "string") throw invalidResponse(`${path}格式无效。`);
  return value;
}

function requireArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw invalidResponse(`${path}格式无效。`);
  return value;
}

function readStringArray(value: unknown, path: string): string[] {
  return requireArray(value, path).map((item, index) => requireString(item, `${path}[${index}]`));
}

function requireInteger(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) throw invalidResponse(`${path}格式无效。`);
  return value;
}

function requireFiniteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw invalidResponse(`${path}格式无效。`);
  return value;
}

function invalidResponse(message = "服务返回的原文片段格式无效，未显示该数据。"): AnnualReportApiError {
  return new AnnualReportApiError(message);
}
