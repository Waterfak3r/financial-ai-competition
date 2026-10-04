import type { EvidencePreviewImage } from "./annualReports";
import { AnnualReportApiError } from "./annualReports";
import type { PdfBox } from "../types/annualReport";
import type { AnnualDiscoveryEvidenceIndexItem } from "../types/discoveryEvidence";

const PREVIEW_PATH = "/api/v1/annual-analyses";
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const EVIDENCE_ID_PATTERN = /^adisc-[0-9a-f]{12}-p[1-9][0-9]*-b[0-9]+-c[0-9]+-[0-9a-f]{12}$/;
const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];
const INTERPRETATION_CODE = "annual_report_text_location_unverified_model_finding";

export async function loadDiscoveryEvidencePreview(
  runId: string,
  evidence: AnnualDiscoveryEvidenceIndexItem,
  expectedReportSha256: string,
  focus: boolean,
): Promise<EvidencePreviewImage> {
  if (!RUN_ID_PATTERN.test(runId) || !EVIDENCE_ID_PATTERN.test(evidence.evidence_id) || !SHA256_PATTERN.test(expectedReportSha256)) {
    throw new AnnualReportApiError("运行 ID、发现原文 ID 或报告版本格式无效。", null);
  }
  const url = `${PREVIEW_PATH}/${encodeURIComponent(runId)}/discovery-evidences/${encodeURIComponent(evidence.evidence_id)}/preview.png?focus=${String(focus)}`;
  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "image/png" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualReportApiError("无法连接年报待核查线索页图服务。请确认本地后端仍在运行。");
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null) as unknown;
    const record = asRecord(detail);
    const errorRecord = asRecord(record?.detail);
    const message = typeof errorRecord?.message === "string" ? errorRecord.message :
      typeof record?.detail === "string" ? record.detail : `读取年报待核查线索页图失败（HTTP ${response.status}）。`;
    throw new AnnualReportApiError(message, response.status, typeof errorRecord?.code === "string" ? errorRecord.code : null);
  }
  if ((response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase() !== "image/png") {
    throw new AnnualReportApiError("待核查线索页图不是 PNG 图片，已停止显示。", response.status);
  }
  const pageHeader = response.headers.get("X-PDF-Page");
  const pageNumber = pageHeader === null ? NaN : Number(pageHeader);
  if (!Number.isInteger(pageNumber) || pageNumber !== evidence.pdf_page) {
    throw new AnnualReportApiError("待核查线索页图页码与报告引用不一致，已停止显示。", response.status);
  }
  if (response.headers.get("X-Evidence-Kind") !== "annual_discovery") {
    throw new AnnualReportApiError("服务未将此页图标记为年度待核查线索，已停止显示。", response.status);
  }
  if (response.headers.get("X-Evidence-Interpretation-Code") !== INTERPRETATION_CODE) {
    throw new AnnualReportApiError("服务未返回待核查线索的解释性质标记，已停止显示。", response.status);
  }
  const mode = focus ? "focus" : "full_page";
  if (response.headers.get("X-Preview-Mode") !== mode) {
    throw new AnnualReportApiError("待核查线索页图范围与请求不一致，已停止显示。", response.status);
  }
  const reportSha256 = response.headers.get("X-Report-SHA256");
  if (
    reportSha256 === null || !SHA256_PATTERN.test(reportSha256) ||
    reportSha256.toLowerCase() !== expectedReportSha256.toLowerCase()
  ) throw new AnnualReportApiError("待核查线索页图对应的报告版本与当前报告不一致，已停止显示。", response.status);
  const region = parseRegion(response.headers.get("X-Preview-Region"), evidence.bbox, response.status);
  const blob = await response.blob();
  const signature = new Uint8Array(await blob.slice(0, PNG_SIGNATURE.length).arrayBuffer());
  if (blob.size <= PNG_SIGNATURE.length || PNG_SIGNATURE.some((byte, index) => signature[index] !== byte)) {
    throw new AnnualReportApiError("待核查线索预览内容无法验证为 PNG，已停止显示。", response.status);
  }
  return { blob, pageNumber, mode, region };
}

function parseRegion(header: string | null, expected: PdfBox, status: number) {
  if (header === null) throw new AnnualReportApiError("待核查线索页图缺少区域元数据，已停止显示。", status);
  try {
    const value: unknown = JSON.parse(header);
    const record = asRecord(value);
    const rawBox = asRecord(record?.bbox);
    if (record === null || rawBox === null || record.coordinate_space !== "unrotated_pdf_points") throw new Error();
    const bbox = {
      x0: finiteNumber(rawBox.x0),
      y0: finiteNumber(rawBox.y0),
      x1: finiteNumber(rawBox.x1),
      y1: finiteNumber(rawBox.y1),
    };
    if (
      bbox.x0 < 0 || bbox.y0 < 0 || bbox.x1 <= bbox.x0 || bbox.y1 <= bbox.y0 ||
      !(["x0", "y0", "x1", "y1"] as const).every((key) => Math.abs(bbox[key] - expected[key]) <= 0.01)
    ) throw new Error();
    return { bbox, coordinateSpace: "unrotated_pdf_points" as const };
  } catch {
    throw new AnnualReportApiError("待核查线索页图区域与来源索引不一致，已停止显示。", status);
  }
}

function finiteNumber(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error();
  return value;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
