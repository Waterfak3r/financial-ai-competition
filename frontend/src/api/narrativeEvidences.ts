import type { EvidencePreviewImage } from "./annualReports";
import { AnnualReportApiError } from "./annualReports";
import type { PdfBox } from "../types/annualReport";
import {
  NARRATIVE_EVIDENCE_INTERPRETATION_CODE,
  NARRATIVE_EVIDENCE_INTERPRETATION_LABEL,
} from "../types/narrativeEvidence";
import type {
  NarrativeEvidence,
  NarrativeEvidenceResponse,
  NarrativeReadingContext,
  NarrativeReadingSourceBlock,
} from "../types/narrativeEvidence";

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
  const readingContext = record.reading_context === undefined
    ? undefined
    : parseReadingContext(record.reading_context, `${path}.reading_context`, page, sourceSha256, rawText, record.bbox);
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
    ...(readingContext === undefined ? {} : { reading_context: readingContext }),
  };
}

function parseReadingContext(
  value: unknown,
  path: string,
  expectedPage: number,
  expectedSourceSha256: string,
  citationText: string,
  citationBoxValue: unknown,
): NarrativeReadingContext {
  const record = requireRecord(value, path);
  if (
    record.purpose !== "display_only" ||
    requireInteger(record.pdf_page, `${path}.pdf_page`) !== expectedPage ||
    requireString(record.source_pdf_sha256, `${path}.source_pdf_sha256`).toLowerCase() !== expectedSourceSha256.toLowerCase()
  ) throw invalidResponse(`${path} 的来源或展示用途无效。`);
  const text = requireString(record.text, `${path}.text`);
  const bbox = readBox(record.bbox, `${path}.bbox`);
  const citationBox = readBox(citationBoxValue, `${path}.citation bbox`);
  const sourceBlocks = requireArray(record.source_blocks, `${path}.source_blocks`).map((block, index) =>
    parseReadingSourceBlock(block, `${path}.source_blocks[${index}]`));
  const citationBlockIndex = requireInteger(record.citation_block_index, `${path}.citation_block_index`);
  if (
    text.trim() === "" || text.length > 1800 || citationBlockIndex < 0 ||
    sourceBlocks.length < 1 || sourceBlocks.length > 5 ||
    sourceBlocks.reduce((total, block) => total + block.raw_text.length, 0) > 1800 ||
    !boxContains(bbox, citationBox, 0.1) || sourceBlocks.some((block) => !boxContains(bbox, block.bbox))
  ) throw invalidResponse(`${path} 的文本范围或坐标覆盖无效。`);
  const citationBlock = sourceBlocks.find((block) => block.block_index === citationBlockIndex);
  if (
    citationBlock === undefined || citationBlock.raw_text.trim() !== citationText.trim() ||
    !boxesMatch(citationBlock.bbox, citationBox, 0.1)
  ) throw invalidResponse(`${path} 未保留与原始引用相同的 PDF 文本块定位。`);
  return {
    purpose: "display_only",
    source_pdf_sha256: expectedSourceSha256.toLowerCase(),
    pdf_page: expectedPage,
    text,
    bbox,
    expanded: requireBoolean(record.expanded, `${path}.expanded`),
    limited: requireBoolean(record.limited, `${path}.limited`),
    limit_reason: readNullableString(record.limit_reason, `${path}.limit_reason`),
    boundary_reason: readNullableString(record.boundary_reason, `${path}.boundary_reason`),
    citation_block_index: citationBlockIndex,
    source_blocks: sourceBlocks,
  };
}

function parseReadingSourceBlock(value: unknown, path: string): NarrativeReadingSourceBlock {
  const record = requireRecord(value, path);
  const blockIndex = requireInteger(record.block_index, `${path}.block_index`);
  const rawText = requireString(record.raw_text, `${path}.raw_text`);
  if (blockIndex < 0 || rawText.trim() === "" || rawText.length > 1200) {
    throw invalidResponse(`${path} 的 PDF 文本块记录无效。`);
  }
  return { block_index: blockIndex, bbox: readBox(record.bbox, `${path}.bbox`), raw_text: rawText };
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

function boxesMatch(left: PdfBox, right: PdfBox, tolerance = 0.01): boolean {
  return (["x0", "y0", "x1", "y1"] as const).every((key) => Math.abs(left[key] - right[key]) <= tolerance);
}

function boxContains(outer: PdfBox, inner: PdfBox, tolerance = 0.02): boolean {
  return inner.x0 >= outer.x0 - tolerance && inner.y0 >= outer.y0 - tolerance &&
    inner.x1 <= outer.x1 + tolerance && inner.y1 <= outer.y1 + tolerance;
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

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw invalidResponse(`${path}格式无效。`);
  return value;
}

function readNullableString(value: unknown, path: string): string | null {
  if (value === null) return null;
  return requireString(value, path);
}

function requireFiniteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw invalidResponse(`${path}格式无效。`);
  return value;
}

function invalidResponse(message = "服务返回的原文片段格式无效，未显示该数据。"): AnnualReportApiError {
  return new AnnualReportApiError(message);
}
