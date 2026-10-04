import type {
  AnnualAnalysisManifest,
  AnnualAnalysisModelReview,
  AnnualAnalysisReport,
  AnnualAnalysisResponse,
  AnnualReportRecord,
  AnnualSourceProvenance,
  CandidateSignal,
  ConfirmedCalculation,
  ConfirmedMetric,
  ClaimVerification,
  MetricVerification,
  M3AnnualScreening,
  M3AnnualScreeningIssue,
  M3AnnualScreeningRule,
  PdfBox,
  VerificationEvidence,
  VerifiedClaim,
} from "../types/annualReport";
import type {
  AnnualDiscoveryAuditArtifact,
  AnnualDiscoveryBatchResult,
  AnnualDiscoveryCoverage,
  AnnualDiscoveryEvidence,
  AnnualDiscoveryEvidenceIndex,
  AnnualDiscoveryFinding,
  AnnualModelDiscovery,
} from "../types/discoveryEvidence";

const ANNUAL_ANALYSIS_PATH = "/api/v1/annual-analyses";
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;
const EVIDENCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];
const DISCOVERY_INTERPRETATION_LABEL = "年报原文定位，模型发现未经独立语义核验";
const DISCOVERY_MAX_CHARS = 1_200_000;

export interface EvidencePreviewRegion {
  bbox: PdfBox;
  coordinateSpace: "unrotated_pdf_points";
}

export interface EvidencePreviewImage {
  blob: Blob;
  pageNumber: number;
  mode: "focus" | "full_page";
  region: EvidencePreviewRegion;
}

export type AnnualReportExportFormat = "json" | "markdown" | "html";
export interface AnnualReportExportDownload {
  blob: Blob;
  filename: string;
}

export class AnnualReportApiError extends Error {
  readonly status: number | null;
  readonly code: string | null;

  constructor(message: string, status: number | null = null, code: string | null = null) {
    super(message);
    this.name = "AnnualReportApiError";
    this.status = status;
    this.code = code;
  }
}

export async function loadAnnualAnalysis(runId: string): Promise<AnnualAnalysisResponse> {
  const checkedRunId = runId.trim();
  if (!RUN_ID_PATTERN.test(checkedRunId)) {
    throw new AnnualReportApiError("run_id 只能包含字母、数字、点、下划线和短横线，长度不超过 121 个字符。");
  }

  const response = await fetch(ANNUAL_ANALYSIS_PATH + "/" + encodeURIComponent(checkedRunId), {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualReportApiError("无法连接本地年度分析服务。请确认后端已启动，并由页面通过 /api 访问。");
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const failure = failureMessage(response.status, payload, "读取年度分析报告");
    throw new AnnualReportApiError(failure.message, response.status, failure.code);
  }
  return readAnnualAnalysisResponse(payload, checkedRunId);
}

export async function downloadAnnualAnalysisExport(
  runId: string,
  format: AnnualReportExportFormat,
): Promise<AnnualReportExportDownload> {
  const checkedRunId = runId.trim();
  if (!RUN_ID_PATTERN.test(checkedRunId)) {
    throw new AnnualReportApiError("run_id 格式无效，无法下载报告。");
  }
  const response = await fetch(
    ANNUAL_ANALYSIS_PATH + "/" + encodeURIComponent(checkedRunId) + "/export?format=" + format,
    {
      method: "GET",
      headers: { Accept: exportMediaType(format) },
      cache: "no-store",
      credentials: "omit",
    },
  ).catch(() => {
    throw new AnnualReportApiError("无法连接本地报告导出服务。请确认后端仍在运行。");
  });
  if (!response.ok) {
    const payload = await readJson(response);
    const failure = failureMessage(response.status, payload, "下载年度报告");
    throw new AnnualReportApiError(failure.message, response.status, failure.code);
  }
  const actualType = (response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
  if (actualType !== exportMediaType(format)) {
    throw new AnnualReportApiError("报告导出格式与请求不一致，已停止下载。", response.status);
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const filename = /(?:^|;)\s*filename="(fintrace-annual-[A-Za-z0-9._-]{1,121}\.(?:json|md|html))"(?:;|$)/i.exec(disposition)?.[1];
  const expectedExtension = format === "markdown" ? ".md" : "." + format;
  if (filename === undefined || !filename.endsWith(expectedExtension) || !filename.startsWith("fintrace-annual-" + checkedRunId)) {
    throw new AnnualReportApiError("报告文件名不符合本机安全下载约定，已停止下载。", response.status);
  }
  return { blob: await response.blob(), filename };
}

function exportMediaType(format: AnnualReportExportFormat): string {
  return format === "json" ? "application/json" : format === "markdown" ? "text/markdown" : "text/html";
}

export async function loadVerificationEvidencePreview(
  runId: string,
  evidence: VerificationEvidence,
  expectedReportSha256: string,
  focus = true,
): Promise<EvidencePreviewImage> {
  if (!RUN_ID_PATTERN.test(runId) || !EVIDENCE_ID_PATTERN.test(evidence.evidence_id)) {
    throw new AnnualReportApiError("运行 ID 或核验证据 ID 格式无效。");
  }
  const url =
    ANNUAL_ANALYSIS_PATH +
    "/" +
    encodeURIComponent(runId) +
    "/evidence/" +
    encodeURIComponent(evidence.evidence_id) +
    "/preview.png?focus=" + String(focus);
  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "image/png" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualReportApiError("无法连接本地证据预览服务。请确认后端仍在运行。");
  });

  if (!response.ok) {
    const payload = await readJson(response);
    const failure = failureMessage(response.status, payload, "读取核验证据预览");
    throw new AnnualReportApiError(failure.message, response.status, failure.code);
  }
  if ((response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase() !== "image/png") {
    throw new AnnualReportApiError("证据预览响应不是 PNG 图片，已停止显示。", response.status);
  }
  return validateEvidencePreviewResponse(response, evidence.pdf_page, evidence.value_region.bbox, expectedReportSha256, "verified_financial_value", focus);
}

async function validateEvidencePreviewResponse(
  response: Response,
  expectedPage: number,
  expectedBox: PdfBox,
  expectedReportSha256: string,
  expectedKind: "verified_financial_value" | "narrative_context",
  focus: boolean,
): Promise<EvidencePreviewImage> {
  const pageHeader = response.headers.get("X-PDF-Page");
  const pageNumber = pageHeader === null ? NaN : Number(pageHeader);
  if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber !== expectedPage) {
    throw new AnnualReportApiError("证据预览页码与证据记录不一致，已停止显示。", response.status);
  }
  if (response.headers.get("X-Evidence-Kind") !== expectedKind) {
    throw new AnnualReportApiError("证据预览类型与证据白名单不一致，已停止显示。", response.status);
  }
  const mode = response.headers.get("X-Preview-Mode");
  const expectedMode = focus ? "focus" : "full_page";
  if (mode !== expectedMode) {
    throw new AnnualReportApiError("证据预览范围与请求不一致，已停止显示。", response.status);
  }
  const regionHeader = response.headers.get("X-Preview-Region");
  const region = parsePreviewRegion(regionHeader, expectedBox, response.status);
  const reportSha256 = response.headers.get("X-Report-SHA256");
  if (
    reportSha256 === null ||
    !SHA256_PATTERN.test(reportSha256) ||
    reportSha256.toLowerCase() !== expectedReportSha256.toLowerCase()
  ) {
    throw new AnnualReportApiError("证据页图对应的报告版本与当前已加载报告不一致，已停止显示。", response.status);
  }
  const blob = await response.blob();
  const signature = new Uint8Array(await blob.slice(0, PNG_SIGNATURE.length).arrayBuffer());
  if (blob.size <= PNG_SIGNATURE.length || PNG_SIGNATURE.some((byte, index) => signature[index] !== byte)) {
    throw new AnnualReportApiError("证据预览内容无法验证为 PNG，已停止显示。", response.status);
  }
  return { blob, pageNumber, mode: expectedMode, region };
}

function parsePreviewRegion(
  header: string | null,
  expectedBox: PdfBox,
  status: number,
): EvidencePreviewRegion {
  if (header === null) throw new AnnualReportApiError("证据预览缺少区域元数据，已停止显示。", status);
  try {
    const value: unknown = JSON.parse(header);
    const record = optionalRecord(value);
    const bbox = optionalRecord(record?.bbox);
    if (record === null || bbox === null || record.coordinate_space !== "unrotated_pdf_points") throw new Error();
    const parsed = {
      x0: finiteNumber(bbox.x0),
      y0: finiteNumber(bbox.y0),
      x1: finiteNumber(bbox.x1),
      y1: finiteNumber(bbox.y1),
    };
    if (
      parsed.x0 < 0 || parsed.y0 < 0 || parsed.x1 <= parsed.x0 || parsed.y1 <= parsed.y0 ||
      Object.keys(parsed).some((key) => Math.abs(parsed[key as keyof typeof parsed] - expectedBox[key as keyof PdfBox]) > 0.01)
    ) throw new Error();
    return { bbox: parsed, coordinateSpace: "unrotated_pdf_points" };
  } catch {
    throw new AnnualReportApiError("证据预览区域与证据定位不一致，已停止显示。", status);
  }
}

function finiteNumber(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error();
  return value;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.trim() === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new AnnualReportApiError("服务返回的内容不是有效 JSON（HTTP " + response.status + "）。", response.status);
  }
}

function failureMessage(
  status: number,
  payload: unknown,
  action: string,
): { message: string; code: string | null } {
  const outer = optionalRecord(payload);
  const detail = outer?.detail;
  const detailRecord = optionalRecord(detail);
  const code = optionalString(detailRecord?.code);
  const message = optionalString(detailRecord?.message) ?? optionalString(detail);
  if (message !== null && message.trim() !== "") {
    return { message, code };
  }
  const knownMessages: Record<string, string> = {
    run_not_found: "找不到该年度分析运行，请核对 run_id。",
    report_not_found: "找不到该运行的年度分析报告。",
    archive_incomplete: "年度分析归档不完整，报告暂时无法读取。",
    invalid_run_id: "run_id 格式无效。",
    evidence_not_found: "该独立核验证据不存在或未通过核验。",
  };
  if (code !== null && knownMessages[code] !== undefined) {
    return { message: knownMessages[code], code };
  }
  return { message: action + "失败（HTTP " + status + (code === null ? "" : "，" + code) + "）。", code };
}

function readAnnualAnalysisResponse(value: unknown, expectedRunId: string): AnnualAnalysisResponse {
  const payload = requireRecord(value, "年度分析响应");
  const rawReport = requireRecord(payload.report, "report");
  const rawManifest = requireRecord(payload.manifest, "manifest");
  const report = readReport(rawReport, "report");
  const manifest = readManifest(rawManifest, "manifest");
  if (
    report.run_id !== expectedRunId ||
    manifest.run_id !== expectedRunId ||
    report.company_id !== manifest.company_id ||
    report.report_year !== manifest.report_year ||
    report.source_document_id !== manifest.document_id ||
    report.source_sha256.toLowerCase() !== manifest.source_pdf_sha256.toLowerCase()
  ) {
    throw new AnnualReportApiError("报告、manifest 与请求的 run_id 或来源绑定不一致，未显示该响应。");
  }
  if (!Array.isArray(payload.verification_evidences)) {
    throw new AnnualReportApiError("年度分析响应缺少核验证据索引，未显示该响应。");
  }
  const verificationEvidences = payload.verification_evidences.map((item, index) =>
    readEvidence(item, "verification_evidences[" + index + "]", report.source_document_id, report.source_sha256),
  );
  const ids = new Set<string>();
  for (const evidence of verificationEvidences) {
    if (ids.has(evidence.evidence_id)) {
      throw new AnnualReportApiError("年度分析响应包含重复的核验证据 ID，未显示该响应。");
    }
    ids.add(evidence.evidence_id);
  }
  const sourceProvenance = readSourceProvenance(payload.source_provenance, manifest.source_pdf_sha256);
  const rawInvestigation = optionalRecord(rawReport.model_investigation);
  const nestedDiscoveryPresent = rawInvestigation !== null && Object.prototype.hasOwnProperty.call(rawInvestigation, "discovery");
  const topDiscoveryPresent = Object.prototype.hasOwnProperty.call(rawReport, "model_discovery");
  if (nestedDiscoveryPresent && !topDiscoveryPresent) {
    throw new AnnualReportApiError("模型调查附录含开放式发现，但报告缺少顶层来源绑定记录，未显示该报告。");
  }
  if (topDiscoveryPresent && nestedDiscoveryPresent && !sameJsonValue(rawReport.model_discovery, rawInvestigation?.discovery)) {
    throw new AnnualReportApiError("顶层开放式发现与模型调查附录不一致，未显示该报告。");
  }
  const discoveryEvidenceIndex = payload.discovery_evidences === undefined
    ? undefined
    : readDiscoveryEvidenceIndex(
        payload.discovery_evidences,
        report.model_discovery ?? null,
        report.run_id,
        report.source_document_id,
        report.source_sha256,
      );
  if (report.model_discovery !== undefined && report.model_discovery !== null && discoveryEvidenceIndex === undefined) {
    throw new AnnualReportApiError("开放式发现缺少对应的原文定位索引，未显示该报告。");
  }
  return {
    report,
    manifest,
    source_provenance: sourceProvenance,
    verification_evidences: verificationEvidences,
    ...(discoveryEvidenceIndex === undefined ? {} : { discovery_evidences: discoveryEvidenceIndex }),
  };
}

function readModelDiscovery(
  value: Record<string, unknown>,
  path: string,
  expected: { runId: string; companyId: string; reportYear: number; sourceDocumentId: string; sourceSha256: string },
): AnnualModelDiscovery {
  exactKeys(value, [
    "kind", "run_id", "company_id", "report_year", "source_document_id", "source_sha256",
    "status", "prompt_version", "model_called", "model_call_count", "model_call_attempt_count",
    "coverage", "findings", "evidences", "audit_artifacts", "limitations", "reason", "fraud_conclusion",
  ], path);
  const sourceSha256 = requireString(value.source_sha256, `${path}.source_sha256`).toLowerCase();
  const status = requireString(value.status, `${path}.status`);
  if (
    value.kind !== "fintrace_annual_discovery" || value.prompt_version !== "annual_discovery_v1" ||
    status !== "completed" && status !== "partial" && status !== "failed" && status !== "not_called" ||
    requireString(value.run_id, `${path}.run_id`) !== expected.runId ||
    requireString(value.company_id, `${path}.company_id`) !== expected.companyId ||
    requireInteger(value.report_year, `${path}.report_year`) !== expected.reportYear ||
    requireString(value.source_document_id, `${path}.source_document_id`) !== expected.sourceDocumentId ||
    sourceSha256 !== expected.sourceSha256.toLowerCase() || !SHA256_PATTERN.test(sourceSha256) ||
    value.fraud_conclusion !== null
  ) throw invalidResponse(`${path} 的来源身份或状态无效`);
  const modelCallCount = safeNonNegativeInteger(value.model_call_count, `${path}.model_call_count`);
  const modelCallAttemptCount = safeNonNegativeInteger(value.model_call_attempt_count, `${path}.model_call_attempt_count`);
  const modelCalled = requireBoolean(value.model_called, `${path}.model_called`);
  if (modelCallAttemptCount < modelCallCount || modelCalled !== (modelCallAttemptCount > 0)) {
    throw invalidResponse(`${path} 的模型调用计数不一致`);
  }
  const reason = nullableString(value.reason, `${path}.reason`);
  if (reason !== null && !/^[a-z][a-z0-9_]{0,99}$/.test(reason)) throw invalidResponse(`${path}.reason`);
  const coverage = readDiscoveryCoverage(value.coverage, `${path}.coverage`, status);
  if (
    status === "not_called" && (modelCallAttemptCount !== 0 || modelCallCount !== 0) ||
    coverage.source_status === "parsed" && (
      modelCallAttemptCount !== coverage.processed_batches ||
      coverage.successful_batches > modelCallCount || modelCallCount > modelCallAttemptCount
    ) ||
    status === "completed" && (
      modelCallCount !== coverage.planned_batches || modelCallAttemptCount !== coverage.planned_batches
    )
  ) throw invalidResponse(`${path} 的状态与调用/批次计数不一致`);
  const evidences = requireArray(value.evidences, `${path}.evidences`).map((item, index) =>
    readDiscoveryEvidence(item, `${path}.evidences[${index}]`, expected.sourceDocumentId, sourceSha256, coverage.total_pages),
  );
  const evidenceById = new Map<string, AnnualDiscoveryEvidence>();
  for (const evidence of evidences) {
    if (evidenceById.has(evidence.evidence_id)) throw invalidResponse(`${path}.evidences 包含重复 ID`);
    evidenceById.set(evidence.evidence_id, evidence);
  }
  const findings = requireArray(value.findings, `${path}.findings`).map((item, index) =>
    readDiscoveryFinding(item, `${path}.findings[${index}]`, sourceSha256, evidenceById),
  );
  const findingIds = new Set<string>();
  const citedEvidenceIds = new Set<string>();
  let totalOpinionChars = 0;
  let totalQuoteChars = 0;
  for (const finding of findings) {
    if (findingIds.has(finding.finding_id)) throw invalidResponse(`${path}.findings 包含重复 finding_id`);
    findingIds.add(finding.finding_id);
    totalOpinionChars += finding.title.length + finding.observation.length + finding.why_it_matters.length + finding.follow_up.length;
    for (const citation of finding.citations) {
      totalQuoteChars += citation.quote.length;
      citedEvidenceIds.add(citation.evidence_id);
    }
  }
  if (
    findings.length > 768 || evidences.length > 3072 || totalOpinionChars > 1_200_000 ||
    totalQuoteChars > 768 * 4 * 300 || [...evidenceById.keys()].some((id) => !citedEvidenceIds.has(id)) ||
    citedEvidenceIds.size !== evidenceById.size ||
    coverage.successful_batches === 0 && (findings.length > 0 || evidences.length > 0)
  ) throw invalidResponse(`${path} 的发现数量、意见、引用或证据不一致`);
  const auditArtifacts = requireArray(value.audit_artifacts, `${path}.audit_artifacts`).map((item, index) =>
    readDiscoveryAuditArtifact(item, `${path}.audit_artifacts[${index}]`),
  );
  if (auditArtifacts.length > 128 || new Set(auditArtifacts.map((item) => item.batch_index)).size !== auditArtifacts.length) {
    throw invalidResponse(`${path}.audit_artifacts 数量或批次重复`);
  }
  const limitations = requireArray(value.limitations, `${path}.limitations`).map((item, index) => {
    const text = requireString(item, `${path}.limitations[${index}]`);
    if (text.length > 400) throw invalidResponse(`${path}.limitations[${index}]`);
    return text;
  });
  if (limitations.length > 1100 || limitations.reduce((sum, item) => sum + item.length, 0) > 1100 * 400) {
    throw invalidResponse(`${path}.limitations 数量或字符数越界`);
  }
  return {
    kind: "fintrace_annual_discovery",
    run_id: expected.runId,
    company_id: expected.companyId,
    report_year: expected.reportYear,
    source_document_id: expected.sourceDocumentId,
    source_sha256: sourceSha256,
    status: status as AnnualModelDiscovery["status"],
    prompt_version: "annual_discovery_v1",
    model_called: modelCalled,
    model_call_count: modelCallCount,
    model_call_attempt_count: modelCallAttemptCount,
    coverage,
    findings,
    evidences,
    audit_artifacts: auditArtifacts,
    limitations,
    reason,
    fraud_conclusion: null,
  };
}

function readDiscoveryCoverage(value: unknown, path: string, status: string): AnnualDiscoveryCoverage {
  const record = requireRecord(value, path);
  exactKeys(record, [
    "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches", "processed_batches",
    "successful_batches", "failed_batches", "batch_results", "processed_chars", "attempted_chars",
    "unattempted_batches", "unattempted_chars", "planned_chars", "omitted_chars", "unread_pages",
    "failed_pages", "no_text_pages", "image_pages", "unread_image_pages", "truncated", "complete",
    "source_status", "content_status", "time_budget_exhausted", "text_only_scope",
  ], path);
  const countNames = [
    "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches", "processed_batches",
    "successful_batches", "failed_batches", "processed_chars", "attempted_chars", "unattempted_batches",
    "unattempted_chars", "planned_chars", "omitted_chars",
  ] as const;
  const counts = Object.fromEntries(countNames.map((key) => [key, safeNonNegativeInteger(record[key], `${path}.${key}`)])) as Record<(typeof countNames)[number], number>;
  const truncated = requireBoolean(record.truncated, `${path}.truncated`);
  const complete = requireBoolean(record.complete, `${path}.complete`);
  const timeBudgetExhausted = requireBoolean(record.time_budget_exhausted, `${path}.time_budget_exhausted`);
  const sourceStatus = requireString(record.source_status, `${path}.source_status`);
  const contentStatus = requireString(record.content_status, `${path}.content_status`);
  const textOnlyScope = requireString(record.text_only_scope, `${path}.text_only_scope`);
  if (
    sourceStatus !== "parsed" && sourceStatus !== "unavailable" ||
    contentStatus !== "text_available" && contentStatus !== "no_extractable_text" && contentStatus !== "unavailable" ||
    textOnlyScope.length > 200 || counts.total_pages > 100_000 || counts.text_pages > counts.total_pages ||
    counts.planned_batches > 128 || counts.processed_batches > counts.planned_batches || counts.total_chars < 0
  ) throw invalidResponse(`${path} 的范围或来源状态无效`);
  const pageLists = {} as Record<"unread_pages" | "failed_pages" | "no_text_pages" | "image_pages" | "unread_image_pages", number[]>;
  for (const field of ["unread_pages", "failed_pages", "no_text_pages", "image_pages", "unread_image_pages"] as const) {
    const items = requireArray(record[field], `${path}.${field}`).map((page, index) =>
      safeNonNegativeInteger(page, `${path}.${field}[${index}]`),
    );
    if (
      items.length > counts.total_pages || items.some((page) => page < 1 || page > counts.total_pages) ||
      new Set(items).size !== items.length || items.some((page, index) => index > 0 && items[index - 1] >= page)
    ) throw invalidResponse(`${path}.${field} 页码列表无效`);
    pageLists[field] = items;
  }
  const pageSet = (field: keyof typeof pageLists) => new Set(pageLists[field]);
  const noText = pageSet("no_text_pages");
  const imagePages = pageSet("image_pages");
  const unreadImagePages = pageSet("unread_image_pages");
  const failedPages = pageSet("failed_pages");
  const unreadPages = pageSet("unread_pages");
  if (
    counts.text_pages + pageLists.no_text_pages.length !== counts.total_pages ||
    [...unreadImagePages].some((page) => !imagePages.has(page) || !noText.has(page)) ||
    [...failedPages].some((page) => !unreadPages.has(page)) ||
    (contentStatus === "text_available" && counts.total_chars === 0) ||
    (contentStatus === "no_extractable_text" && counts.total_chars !== 0)
  ) throw invalidResponse(`${path} 页码或内容计数关系无效`);
  const rawBatchResults = requireArray(record.batch_results, `${path}.batch_results`);
  const batchResults = rawBatchResults.map((item, index) => readDiscoveryBatchResult(item, `${path}.batch_results[${index}]`, counts.total_pages));
  if (sourceStatus === "unavailable") {
    if (
      contentStatus !== "unavailable" || countNames.some((field) => counts[field] !== 0) ||
      Object.values(pageLists).some((items) => items.length > 0) || complete || truncated || timeBudgetExhausted ||
      batchResults.length > 0 || status !== "failed" && status !== "not_called"
    ) throw invalidResponse(`${path} 不可用来源不能包含虚构覆盖`);
  } else {
    if (
      contentStatus === "unavailable" || counts.processed_batches !== counts.successful_batches + counts.failed_batches ||
      counts.unattempted_batches !== counts.planned_batches - counts.processed_batches ||
      counts.planned_chars > counts.total_chars || counts.planned_chars > DISCOVERY_MAX_CHARS ||
      counts.attempted_chars > counts.planned_chars || counts.unattempted_chars > counts.planned_chars ||
      counts.processed_chars > counts.attempted_chars || counts.processed_chars > counts.planned_chars ||
      counts.omitted_chars !== Math.max(counts.total_chars - counts.planned_chars, 0) + counts.unattempted_chars ||
      batchResults.length !== counts.planned_batches
    ) throw invalidResponse(`${path} 批次/字符计数关系无效`);
    const seen = new Set<number>();
    const chars = { succeeded: 0, failed: 0, unattempted: 0 };
    const batchPageSets = { succeeded: new Set<number>(), failed: new Set<number>(), unattempted: new Set<number>() };
    for (const batch of batchResults) {
      if (seen.has(batch.batch_index) || batch.batch_index > counts.planned_batches) throw invalidResponse(`${path} 批次编号重复或越界`);
      seen.add(batch.batch_index);
      chars[batch.status] += batch.raw_chars;
      batch.pages.forEach((page) => batchPageSets[batch.status].add(page));
      if ((batch.batch_index <= counts.processed_batches) === (batch.status === "unattempted")) {
        throw invalidResponse(`${path} 批次顺序与结束状态不一致`);
      }
    }
    const successCount = batchResults.filter((batch) => batch.status === "succeeded").length;
    const failedCount = batchResults.filter((batch) => batch.status === "failed").length;
    const unattemptedCount = batchResults.filter((batch) => batch.status === "unattempted").length;
    if (
      seen.size !== counts.planned_batches || chars.succeeded + chars.failed + chars.unattempted !== counts.planned_chars ||
      chars.succeeded !== counts.processed_chars || chars.succeeded + chars.failed !== counts.attempted_chars ||
      chars.unattempted !== counts.unattempted_chars || successCount !== counts.successful_batches ||
      failedCount !== counts.failed_batches || unattemptedCount !== counts.unattempted_batches ||
      [...batchPageSets.failed].some((page) => !failedPages.has(page) || !unreadPages.has(page)) ||
      [...batchPageSets.unattempted].some((page) => !unreadPages.has(page)) ||
      batchPageSets.failed.size !== failedPages.size ||
      [...failedPages].some((page) => !batchPageSets.failed.has(page)) ||
      truncated !== Boolean(counts.omitted_chars || counts.unattempted_batches || counts.unattempted_chars)
    ) throw invalidResponse(`${path} 批次结果与汇总不一致`);
  }
  const expectedComplete = sourceStatus === "parsed" && contentStatus === "text_available" && counts.total_chars > 0 &&
    !truncated && pageLists.unread_image_pages.length === 0 && counts.planned_chars === counts.total_chars &&
    counts.processed_batches === counts.planned_batches && counts.failed_batches === 0 &&
    counts.successful_batches === counts.planned_batches && counts.processed_chars === counts.planned_chars &&
    pageLists.unread_pages.length === 0;
  if (
    complete !== expectedComplete || status === "completed" && !expectedComplete ||
    status === "partial" && (expectedComplete || counts.successful_batches === 0) ||
    status === "failed" && sourceStatus === "parsed" && (counts.failed_batches === 0 || counts.successful_batches !== 0) ||
    status === "not_called" && sourceStatus === "parsed" && counts.processed_batches !== 0
  ) throw invalidResponse(`${path} 完成状态与覆盖不一致`);
  return {
    ...counts,
    batch_results: batchResults,
    unread_pages: pageLists.unread_pages,
    failed_pages: pageLists.failed_pages,
    no_text_pages: pageLists.no_text_pages,
    image_pages: pageLists.image_pages,
    unread_image_pages: pageLists.unread_image_pages,
    truncated,
    complete,
    source_status: sourceStatus as AnnualDiscoveryCoverage["source_status"],
    content_status: contentStatus as AnnualDiscoveryCoverage["content_status"],
    time_budget_exhausted: timeBudgetExhausted,
    text_only_scope: textOnlyScope,
  };
}

function readDiscoveryBatchResult(value: unknown, path: string, totalPages: number): AnnualDiscoveryBatchResult {
  const record = requireRecord(value, path);
  exactKeys(record, ["batch_index", "status", "reason", "pages", "raw_chars"], path);
  const index = safeNonNegativeInteger(record.batch_index, `${path}.batch_index`);
  const status = requireString(record.status, `${path}.status`);
  const reason = nullableString(record.reason, `${path}.reason`);
  const rawChars = safeNonNegativeInteger(record.raw_chars, `${path}.raw_chars`);
  const pages = requireArray(record.pages, `${path}.pages`).map((page, pageIndex) => safeNonNegativeInteger(page, `${path}.pages[${pageIndex}]`));
  if (
    index < 1 || status !== "succeeded" && status !== "failed" && status !== "unattempted" ||
    rawChars < 1 || rawChars > 12_000 || pages.length === 0 ||
    pages.some((page) => page < 1 || page > totalPages) || new Set(pages).size !== pages.length ||
    pages.some((page, position) => position > 0 && pages[position - 1] >= page) ||
    status === "succeeded" && reason !== null ||
    status !== "succeeded" && (reason === null || !/^[a-z][a-z0-9_]{0,99}$/.test(reason))
  ) throw invalidResponse(`${path} 字段无效`);
  return { batch_index: index, status, reason, pages, raw_chars: rawChars };
}

function readDiscoveryEvidence(
  value: unknown,
  path: string,
  expectedDocumentId: string,
  expectedSha256: string,
  totalPages: number,
): AnnualDiscoveryEvidence {
  const record = requireRecord(value, path);
  exactKeys(record, ["evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index", "start_char", "bbox", "raw_text"], path);
  const evidenceId = requireString(record.evidence_id, `${path}.evidence_id`);
  const sourceDocumentId = requireString(record.source_document_id, `${path}.source_document_id`);
  const sourceSha256 = requireString(record.source_sha256, `${path}.source_sha256`).toLowerCase();
  const page = safeNonNegativeInteger(record.pdf_page, `${path}.pdf_page`);
  const blockIndex = safeNonNegativeInteger(record.block_index, `${path}.block_index`);
  const startChar = safeNonNegativeInteger(record.start_char, `${path}.start_char`);
  const rawText = requireString(record.raw_text, `${path}.raw_text`);
  const idMatch = evidenceId.match(new RegExp(`^adisc-${expectedSha256.slice(0, 12)}-p([1-9][0-9]*)-b([0-9]+)-c([0-9]+)-[0-9a-f]{12}$`));
  if (
    sourceDocumentId !== expectedDocumentId || sourceSha256 !== expectedSha256.toLowerCase() ||
    !SHA256_PATTERN.test(sourceSha256) || page < 1 || page > totalPages || rawText.trim() === "" || rawText.length > 1200 ||
    idMatch === null || Number(idMatch[1]) !== page || Number(idMatch[2]) !== blockIndex || Number(idMatch[3]) !== startChar
  ) throw invalidResponse(`${path} 来源、文本或坐标 ID 无效`);
  return {
    evidence_id: evidenceId,
    source_document_id: sourceDocumentId,
    source_sha256: sourceSha256,
    pdf_page: page,
    block_index: blockIndex,
    start_char: startChar,
    bbox: readBox(record.bbox, `${path}.bbox`),
    raw_text: rawText,
  };
}

function readDiscoveryFinding(
  value: unknown,
  path: string,
  sourceSha256: string,
  evidenceById: Map<string, AnnualDiscoveryEvidence>,
): AnnualDiscoveryFinding {
  const record = requireRecord(value, path);
  exactKeys(record, ["finding_id", "title", "observation", "why_it_matters", "follow_up", "priority", "classification", "verification_status", "evidence_ids", "citations"], path);
  const findingId = requireString(record.finding_id, `${path}.finding_id`);
  const priority = requireString(record.priority, `${path}.priority`);
  if (
    !new RegExp(`^disc:${sourceSha256.slice(0, 12)}-[0-9a-f]{20}$`).test(findingId) ||
    priority !== "high" && priority !== "medium" && priority !== "low" ||
    record.classification !== "unverified_model_finding" || record.verification_status !== "unverified"
  ) throw invalidResponse(`${path} 标识、优先级或核验状态无效`);
  const opinion = {} as Pick<AnnualDiscoveryFinding, "title" | "observation" | "why_it_matters" | "follow_up">;
  for (const [field, limit] of [["title", 100], ["observation", 600], ["why_it_matters", 400], ["follow_up", 400]] as const) {
    const text = requireString(record[field], `${path}.${field}`);
    if (!text.trim() || text.length > limit || /[0-9]/.test(text) || /[\u0000-\u001f\u007f]/.test(text)) {
      throw invalidResponse(`${path}.${field} 不符合观点文本限制`);
    }
    opinion[field] = text;
  }
  const evidenceIds = requireArray(record.evidence_ids, `${path}.evidence_ids`).map((item, index) => requireString(item, `${path}.evidence_ids[${index}]`));
  const citations = requireArray(record.citations, `${path}.citations`).map((item, index) => {
    const citationPath = `${path}.citations[${index}]`;
    const citation = requireRecord(item, citationPath);
    exactKeys(citation, ["evidence_id", "quote"], citationPath);
    const evidenceId = requireString(citation.evidence_id, `${citationPath}.evidence_id`);
    const quote = requireString(citation.quote, `${citationPath}.quote`);
    const evidence = evidenceById.get(evidenceId);
    if (!evidence || !quote.trim() || quote.trim().length < 2 || quote.length > 300 || !evidence.raw_text.includes(quote)) {
      throw invalidResponse(`${citationPath} 不是本批原文中的有效引用`);
    }
    return { evidence_id: evidenceId, quote };
  });
  if (
    citations.length < 1 || citations.length > 4 || evidenceIds.length !== citations.length ||
    new Set(evidenceIds).size !== evidenceIds.length ||
    [...new Set(citations.map((citation) => citation.evidence_id))].length !== citations.length ||
    evidenceIds.some((id) => !citations.some((citation) => citation.evidence_id === id))
  ) throw invalidResponse(`${path} 引用 ID 与 evidence_ids 不一致`);
  return {
    finding_id: findingId,
    ...opinion,
    priority,
    classification: "unverified_model_finding",
    verification_status: "unverified",
    evidence_ids: evidenceIds,
    citations,
  };
}

function readDiscoveryAuditArtifact(value: unknown, path: string): AnnualDiscoveryAuditArtifact {
  const record = requireRecord(value, path);
  exactKeys(record, ["batch_index", "audit_dir", "request_path", "response_path", "failure_path", "status", "prompt_version"], path);
  const batchIndex = safeNonNegativeInteger(record.batch_index, `${path}.batch_index`);
  const status = requireString(record.status, `${path}.status`);
  const promptVersion = requireString(record.prompt_version, `${path}.prompt_version`);
  const paths = {} as Pick<AnnualDiscoveryAuditArtifact, "audit_dir" | "request_path" | "response_path" | "failure_path">;
  for (const key of ["audit_dir", "request_path", "response_path", "failure_path"] as const) {
    const pathValue = nullableString(record[key], `${path}.${key}`);
    if (pathValue !== null && !safeAuditPath(pathValue)) throw invalidResponse(`${path}.${key} 不安全`);
    paths[key] = pathValue;
  }
  if (
    batchIndex < 1 || batchIndex > 128 ||
    status !== "started" && status !== "succeeded" && status !== "failed" && status !== "incomplete" ||
    promptVersion !== "annual_discovery_v1"
  ) throw invalidResponse(`${path} 状态无效`);
  return { batch_index: batchIndex, ...paths, status, prompt_version: "annual_discovery_v1" };
}

function readDiscoveryEvidenceIndex(
  value: unknown,
  layer: AnnualModelDiscovery | null,
  runId: string,
  sourceDocumentId: string,
  sourceSha256: string,
): AnnualDiscoveryEvidenceIndex {
  const record = requireRecord(value, "discovery_evidences");
  exactKeys(record, ["interpretation_label", "items"], "discovery_evidences");
  if (record.interpretation_label !== DISCOVERY_INTERPRETATION_LABEL) throw invalidResponse("discovery_evidences.interpretation_label");
  const items = requireArray(record.items, "discovery_evidences.items").map((value, index) => {
    const path = `discovery_evidences.items[${index}]`;
    const itemRecord = requireRecord(value, path);
    exactKeys(itemRecord, [
      "evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index", "start_char", "bbox", "raw_text",
      "finding_ids", "citations", "preview_url",
    ], path);
    if (layer === null) throw invalidResponse("无顶层发现时不能包含原文定位项目");
    const evidenceFields = ["evidence_id", "source_document_id", "source_sha256", "pdf_page", "block_index", "start_char", "bbox", "raw_text"];
    const evidenceValue = Object.fromEntries(evidenceFields.map((field) => [field, itemRecord[field]]));
    const evidence = readDiscoveryEvidence(evidenceValue, path, sourceDocumentId, sourceSha256, layer.coverage.total_pages);
    const findingIds = requireArray(itemRecord.finding_ids, `${path}.finding_ids`).map((id, idIndex) => requireString(id, `${path}.finding_ids[${idIndex}]`));
    const citations = requireArray(itemRecord.citations, `${path}.citations`).map((citationValue, citationIndex) => {
      const citationPath = `${path}.citations[${citationIndex}]`;
      const citation = requireRecord(citationValue, citationPath);
      exactKeys(citation, ["finding_id", "quote"], citationPath);
      return {
        finding_id: requireString(citation.finding_id, `${citationPath}.finding_id`),
        quote: requireString(citation.quote, `${citationPath}.quote`),
      };
    });
    const previewUrl = requireString(itemRecord.preview_url, `${path}.preview_url`);
    const expectedUrl = `/v1/annual-analyses/${runId}/discovery-evidences/${encodeURIComponent(evidence.evidence_id)}/preview.png`;
    if (
      previewUrl !== expectedUrl || findingIds.length === 0 ||
      new Set(findingIds).size !== findingIds.length || findingIds.some((id, idx) => idx > 0 && findingIds[idx - 1] >= id)
    ) throw invalidResponse(`${path} 的引用关系或预览路径无效`);
    return { ...evidence, finding_ids: findingIds, citations, preview_url: previewUrl };
  });
  if (layer === null) {
    if (items.length > 0) throw invalidResponse("旧报告不能包含未绑定的发现原文");
    return { interpretation_label: DISCOVERY_INTERPRETATION_LABEL, items: [] };
  }
  const expectedEvidenceById = new Map(layer.evidences.map((evidence) => [evidence.evidence_id, evidence]));
  const expectedCitationByEvidence = new Map<string, Array<{ finding_id: string; quote: string }>>();
  for (const finding of layer.findings) {
    for (const citation of finding.citations) {
      const refs = expectedCitationByEvidence.get(citation.evidence_id) ?? [];
      refs.push({ finding_id: finding.finding_id, quote: citation.quote });
      expectedCitationByEvidence.set(citation.evidence_id, refs);
    }
  }
  if (items.length !== expectedEvidenceById.size) throw invalidResponse("发现原文索引与报告证据数量不一致");
  const seen = new Set<string>();
  for (const item of items) {
    if (seen.has(item.evidence_id)) throw invalidResponse("发现原文索引包含重复 evidence_id");
    seen.add(item.evidence_id);
    const expected = expectedEvidenceById.get(item.evidence_id);
    const expectedRefs = expectedCitationByEvidence.get(item.evidence_id) ?? [];
    const expectedFindingIds = [...new Set(expectedRefs.map((item) => item.finding_id))].sort();
    if (
      expected === undefined || !sameDiscoveryEvidence(item, expected) ||
      !sameJsonValue(item.finding_ids, expectedFindingIds) ||
      !sameJsonValue([...item.citations].sort(compareDiscoveryCitation), [...expectedRefs].sort(compareDiscoveryCitation))
    ) throw invalidResponse("发现原文索引与本次模型发现引用不一致");
  }
  return { interpretation_label: DISCOVERY_INTERPRETATION_LABEL, items };
}

function sameDiscoveryEvidence(left: AnnualDiscoveryEvidence, right: AnnualDiscoveryEvidence): boolean {
  return left.evidence_id === right.evidence_id && left.source_document_id === right.source_document_id &&
    left.source_sha256 === right.source_sha256 && left.pdf_page === right.pdf_page &&
    left.block_index === right.block_index && left.start_char === right.start_char &&
    left.raw_text === right.raw_text && sameJsonValue(left.bbox, right.bbox);
}

function compareDiscoveryCitation(left: { finding_id: string; quote: string }, right: { finding_id: string; quote: string }): number {
  return left.finding_id.localeCompare(right.finding_id) || left.quote.localeCompare(right.quote);
}

function safeAuditPath(value: string): boolean {
  if (value.length > 500 || value.includes("\\") || value.startsWith("/")) return false;
  const parts = value.split("/");
  return parts.length >= 4 && parts[0] === "artifacts" && parts[1] === "runs" && parts.every((part) => part !== "" && part !== "." && part !== "..");
}

function exactKeys(record: Record<string, unknown>, expected: string[], path: string): void {
  if (record === null || Object.keys(record).length !== expected.length || expected.some((key) => !Object.prototype.hasOwnProperty.call(record, key))) {
    throw invalidResponse(`${path} 字段集合无效`);
  }
}

function safeNonNegativeInteger(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw invalidResponse(path);
  return value;
}

function sameJsonValue(left: unknown, right: unknown): boolean {
  const canonical = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(canonical);
    if (typeof value !== "object" || value === null) return value;
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]));
  };
  return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
}

function readSourceProvenance(value: unknown, sourceSha256: string): AnnualSourceProvenance {
  const record = requireRecord(value, "source_provenance");
  const statusValue = requireString(record.status, "source_provenance.status");
  const natureValue = requireString(record.data_nature, "source_provenance.data_nature");
  let status: AnnualSourceProvenance["status"];
  let nature: AnnualSourceProvenance["data_nature"];
  if (statusValue === "registered_synthetic" && natureValue === "original_synthetic") {
    status = "registered_synthetic";
    nature = "original_synthetic";
  } else if (statusValue === "unconfirmed" && natureValue === "unconfirmed") {
    status = "unconfirmed";
    nature = "unconfirmed";
  } else {
    throw invalidResponse("source_provenance.status/data_nature");
  }
  const resolvedSha = nullableString(record.source_sha256, "source_provenance.source_sha256");
  if (status === "registered_synthetic" && resolvedSha?.toLowerCase() !== sourceSha256.toLowerCase()) {
    throw new AnnualReportApiError("样例来源性质与已校验 PDF 哈希不一致，未显示该性质。", null);
  }
  return {
    status,
    data_nature: nature,
    label: requireString(record.label, "source_provenance.label"),
    sample_id: nullableString(record.sample_id, "source_provenance.sample_id"),
    revision: nullableString(record.revision, "source_provenance.revision"),
    registered_document_id: nullableString(record.registered_document_id, "source_provenance.registered_document_id"),
    company_name: nullableString(record.company_name, "source_provenance.company_name"),
    source_sha256: resolvedSha,
    use_conditions: nullableString(record.use_conditions, "source_provenance.use_conditions"),
    fraud_label: nullableBoolean(record.fraud_label, "source_provenance.fraud_label"),
    model_training: nullableBoolean(record.model_training, "source_provenance.model_training"),
  };
}

function readManifest(value: Record<string, unknown>, path: string): AnnualAnalysisManifest {
  const rawMode = value.mode;
  const supportedModes = ["deterministic", "m3_screening", "model_investigation", "integrated_analysis"];
  let mode: AnnualAnalysisManifest["mode"] = null;
  if (rawMode !== undefined && rawMode !== null) {
    if (rawMode === "unknown" || (typeof rawMode === "string" && supportedModes.includes(rawMode))) {
      mode = rawMode as NonNullable<AnnualAnalysisManifest["mode"]>;
    } else {
      throw invalidResponse(path + ".mode");
    }
  }
  const manifest: AnnualAnalysisManifest = {
    run_id: requireString(value.run_id, path + ".run_id"),
    status: requireString(value.status, path + ".status"),
    mode,
    company_id: requireString(value.company_id, path + ".company_id"),
    report_year: requireInteger(value.report_year, path + ".report_year"),
    document_id: requireString(value.document_id, path + ".document_id"),
    source_pdf_archive_path: requireString(value.source_pdf_archive_path, path + ".source_pdf_archive_path"),
    report_json_path: requireString(value.report_json_path, path + ".report_json_path"),
    source_pdf_sha256: requireString(value.source_pdf_sha256, path + ".source_pdf_sha256"),
    report_sha256: requireString(value.report_sha256, path + ".report_sha256"),
  };
  if (
    !SHA256_PATTERN.test(manifest.source_pdf_sha256) ||
    !SHA256_PATTERN.test(manifest.report_sha256) ||
    (manifest.status !== "completed" && manifest.status !== "completed_with_issues")
  ) {
    throw invalidResponse(path);
  }
  return manifest;
}

function readReport(value: Record<string, unknown>, path: string): AnnualAnalysisReport {
  if (value.kind !== "fintrace_annual_analysis_report") {
    throw new AnnualReportApiError("服务返回的报告类型不受支持。");
  }
  const sourceSha256 = requireString(value.source_sha256, path + ".source_sha256");
  if (!SHA256_PATTERN.test(sourceSha256)) {
    throw invalidResponse(path + ".source_sha256");
  }
  const scope = requireRecord(value.scope, path + ".scope");
  const confirmed = requireRecord(value.confirmed, path + ".confirmed");
  const pendingReview = requireRecord(value.pending_review, path + ".pending_review");
  const companyId = requireString(value.company_id, path + ".company_id");
  const reportYear = requireInteger(value.report_year, path + ".report_year");
  const sourceDocumentId = requireString(value.source_document_id, path + ".source_document_id");
  const runId = requireString(value.run_id, path + ".run_id");
  const comparabilityValue = value.comparability;
  const comparability =
    comparabilityValue === null
      ? null
      : readComparability(requireRecord(comparabilityValue, path + ".comparability"), path + ".comparability");
  const modelReview =
    value.model_review === undefined
      ? undefined
      : value.model_review === null
        ? null
        : readModelReview(
            requireRecord(value.model_review, path + ".model_review"),
            path + ".model_review",
            { runId, companyId, reportYear, sourceDocumentId, sourceSha256 },
          );
  return {
    kind: "fintrace_annual_analysis_report",
    title: optionalString(value.title) ?? undefined,
    run_id: runId,
    company_id: companyId,
    report_year: reportYear,
    source_document_id: sourceDocumentId,
    source_sha256: sourceSha256.toLowerCase(),
    fraud_conclusion: nullableString(value.fraud_conclusion, path + ".fraud_conclusion"),
    scope: {
      note: requireString(scope.note, path + ".scope.note"),
      gaps: readStringArray(scope.gaps, path + ".scope.gaps"),
    },
    comparability,
    confirmed: {
      metrics: requireArray(confirmed.metrics, path + ".confirmed.metrics").map((item, index) =>
        readMetric(item, path + ".confirmed.metrics[" + index + "]", sourceSha256),
      ),
      analyses: requireArray(confirmed.analyses, path + ".confirmed.analyses").map((item, index) =>
        readCalculation(item, path + ".confirmed.analyses[" + index + "]"),
      ),
    },
    verified_claims: requireArray(value.verified_claims, path + ".verified_claims").map((item, index) =>
      readClaim(item, path + ".verified_claims[" + index + "]"),
    ),
    pending_review: {
      facts: readRecordArray(pendingReview.facts, path + ".pending_review.facts"),
      calculations: readRecordArray(pendingReview.calculations, path + ".pending_review.calculations"),
      claims: readRecordArray(pendingReview.claims, path + ".pending_review.claims"),
      candidate_signals: requireArray(pendingReview.candidate_signals, path + ".pending_review.candidate_signals").map(
        (item, index) => readCandidate(item, path + ".pending_review.candidate_signals[" + index + "]"),
      ),
      uncited_evidences: readRecordArray(pendingReview.uncited_evidences, path + ".pending_review.uncited_evidences"),
    },
    limitations: readStringArray(value.limitations, path + ".limitations"),
    model_called: requireBoolean(value.model_called, path + ".model_called"),
    m3_screening:
      value.m3_screening === undefined
        ? undefined
        : value.m3_screening === null
          ? null
          : readM3AnnualScreening(
              requireRecord(value.m3_screening, path + ".m3_screening"),
              path + ".m3_screening",
              companyId,
              reportYear,
              sourceDocumentId,
              sourceSha256,
            ),
    model_investigation:
      value.model_investigation === undefined
        ? undefined
        : value.model_investigation === null
          ? null
          : requireRecord(value.model_investigation, path + ".model_investigation"),
    model_discovery:
      value.model_discovery === undefined
        ? undefined
        : readModelDiscovery(requireRecord(value.model_discovery, path + ".model_discovery"), path + ".model_discovery", {
            runId,
            companyId,
            reportYear,
            sourceDocumentId,
            sourceSha256,
          }),
    model_review: modelReview,
  };
}

function readModelReview(
  value: Record<string, unknown>,
  path: string,
  expected: {
    runId: string;
    companyId: string;
    reportYear: number;
    sourceDocumentId: string;
    sourceSha256: string;
  },
): AnnualAnalysisModelReview {
  const status = requireString(value.status, path + ".status");
  if (status !== "completed" && status !== "failed" && status !== "not_called") {
    throw invalidResponse(path + ".status");
  }
  const assessmentValue = nullableString(value.assessment, path + ".assessment");
  const assessments = new Set([
    "prioritize_review",
    "no_priority_issue_identified_within_scope",
    "insufficient_evidence",
  ]);
  if ((assessmentValue !== null && !assessments.has(assessmentValue)) || (status === "completed") !== (assessmentValue !== null)) {
    throw invalidResponse(path + ".assessment");
  }
  const assessment = assessmentValue as AnnualAnalysisModelReview["assessment"];
  const summary = nullableString(value.summary, path + ".summary");
  const modelCalled = requireBoolean(value.model_called, path + ".model_called");
  const callCount = requireInteger(value.call_count, path + ".call_count");
  const callAttemptCount = requireInteger(value.call_attempt_count, path + ".call_attempt_count");
  const reason = nullableString(value.reason, path + ".reason");
  const identity = requireRecord(value.source_identity, path + ".source_identity");
  const sourceIdentity = {
    run_id: requireString(identity.run_id, path + ".source_identity.run_id"),
    company_id: requireString(identity.company_id, path + ".source_identity.company_id"),
    report_year: requireInteger(identity.report_year, path + ".source_identity.report_year"),
    source_document_id: requireString(identity.source_document_id, path + ".source_identity.source_document_id"),
    source_sha256: requireString(identity.source_sha256, path + ".source_identity.source_sha256"),
  };
  if (
    sourceIdentity.run_id !== expected.runId ||
    sourceIdentity.company_id !== expected.companyId ||
    sourceIdentity.report_year !== expected.reportYear ||
    sourceIdentity.source_document_id !== expected.sourceDocumentId ||
    !SHA256_PATTERN.test(sourceIdentity.source_sha256) ||
    sourceIdentity.source_sha256.toLowerCase() !== expected.sourceSha256.toLowerCase() ||
    callCount < 0 ||
    callAttemptCount < callCount ||
    (status === "completed" && (!modelCalled || summary === null || summary.trim() === "")) ||
    (status !== "completed" && reason === null) ||
    (status === "not_called" && modelCalled)
  ) {
    throw invalidResponse(path);
  }
  const reasons = requireArray(value.reasons, path + ".reasons").map((item, index) => {
    const entry = requireRecord(item, path + ".reasons[" + index + "]");
    return {
      text: requireString(entry.text, path + ".reasons[" + index + "].text"),
      evidence_ids: readStringArray(entry.evidence_ids, path + ".reasons[" + index + "].evidence_ids"),
    };
  });
  const followUpItems = requireArray(value.follow_up_items, path + ".follow_up_items").map((item, index) => {
    const entry = requireRecord(item, path + ".follow_up_items[" + index + "]");
    return {
      object: requireString(entry.object, path + ".follow_up_items[" + index + "].object"),
      action: requireString(entry.action, path + ".follow_up_items[" + index + "].action"),
      evidence_ids: readStringArray(entry.evidence_ids, path + ".follow_up_items[" + index + "].evidence_ids"),
    };
  });
  return {
    status,
    reason,
    assessment,
    summary,
    reasons,
    follow_up_items: followUpItems,
    limitations: readStringArray(value.limitations, path + ".limitations"),
    model_called: modelCalled,
    call_count: callCount,
    call_attempt_count: callAttemptCount,
    audit_artifacts: readRecordArray(value.audit_artifacts, path + ".audit_artifacts"),
    source_identity: sourceIdentity,
  };
}

function readM3AnnualScreening(
  value: Record<string, unknown>,
  path: string,
  companyId: string,
  reportYear: number,
  sourceDocumentId: string,
  sourceSha256: string,
): M3AnnualScreening {
  const nested = requireRecord(value.screening, path + ".screening");
  const screeningYear = requireInteger(nested.report_year, path + ".screening.report_year");
  if (
    requireString(value.company_id, path + ".company_id") !== companyId ||
    screeningYear !== reportYear ||
    requireString(value.source_document_id, path + ".source_document_id") !== sourceDocumentId ||
    requireString(value.source_sha256, path + ".source_sha256").toLowerCase() !== sourceSha256.toLowerCase()
  ) {
    throw invalidResponse(path + " source binding");
  }
  const screeningSourceSha256 = requireString(value.source_sha256, path + ".source_sha256");
  if (!SHA256_PATTERN.test(screeningSourceSha256)) {
    throw invalidResponse(path + ".source_sha256");
  }
  return {
    kind: requireString(value.kind, path + ".kind"),
    rule_version: requireString(nested.rule_version, path + ".screening.rule_version"),
    status: requireString(value.status, path + ".status"),
    screening_status: requireString(nested.status, path + ".screening.status"),
    total_score: nested.total_score === null ? null : requireInteger(nested.total_score, path + ".screening.total_score"),
    maximum_score: requireInteger(nested.maximum_score, path + ".screening.maximum_score"),
    rules: requireArray(nested.rules, path + ".screening.rules").map((item, index) =>
      readM3AnnualScreeningRule(item, path + ".screening.rules[" + index + "]"),
    ),
    limitations: readStringArray(nested.limitations, path + ".screening.limitations"),
  };
}

function readM3AnnualScreeningRule(value: unknown, path: string): M3AnnualScreeningRule {
  const record = requireRecord(value, path);
  const triggeredValue = record.triggered;
  if (triggeredValue !== null && typeof triggeredValue !== "boolean") {
    throw invalidResponse(path + ".triggered");
  }
  const pointsValue = record.points;
  if (pointsValue !== null && (typeof pointsValue !== "number" || !Number.isInteger(pointsValue))) {
    throw invalidResponse(path + ".points");
  }
  return {
    rule_id: requireString(record.rule_id, path + ".rule_id"),
    rule_version: requireString(record.rule_version, path + ".rule_version"),
    formula: requireString(record.formula, path + ".formula"),
    threshold: nullableString(record.threshold, path + ".threshold"),
    points_if_triggered: requireInteger(record.points_if_triggered, path + ".points_if_triggered"),
    points: pointsValue as number | null,
    status: requireString(record.status, path + ".status"),
    triggered: triggeredValue,
    calculated_value: nullableString(record.calculated_value, path + ".calculated_value"),
    issues: requireArray(record.issues, path + ".issues").map((item, index) =>
      readM3AnnualScreeningIssue(item, path + ".issues[" + index + "]"),
    ),
  };
}

function readM3AnnualScreeningIssue(value: unknown, path: string): M3AnnualScreeningIssue {
  if (typeof value === "string") {
    return { code: null, message: value };
  }
  const record = requireRecord(value, path);
  return {
    code: requireString(record.code, path + ".code"),
    message: requireString(record.message, path + ".message"),
  };
}

function readMetric(value: unknown, path: string, expectedSha256: string): ConfirmedMetric {
  const record = requireRecord(value, path);
  const factId = requireString(record.fact_id, path + ".fact_id");
  const documentId = requireString(record.source_document_id, path + ".source_document_id");
  const sourceSha256 = requireString(record.source_sha256, path + ".source_sha256");
  if (sourceSha256.toLowerCase() !== expectedSha256.toLowerCase()) {
    throw invalidResponse(path + ".source_sha256");
  }
  return {
    fact_id: factId,
    metric_id: optionalString(record.metric_id) ?? undefined,
    company_id: optionalString(record.company_id) ?? undefined,
    label_raw: requireString(record.label_raw, path + ".label_raw"),
    raw_value: requireScalarText(record.raw_value, path + ".raw_value"),
    normalized_value: requireScalarText(record.normalized_value, path + ".normalized_value"),
    currency: requireString(record.currency, path + ".currency"),
    unit_multiplier: requireScalarText(record.unit_multiplier, path + ".unit_multiplier"),
    unit: nullableString(record.unit, path + ".unit"),
    report_year: requireInteger(record.report_year, path + ".report_year"),
    period_start: nullableString(record.period_start, path + ".period_start"),
    period_end: nullableString(record.period_end, path + ".period_end"),
    period_type: optionalString(record.period_type) ?? undefined,
    statement_type: optionalString(record.statement_type) ?? undefined,
    scope: requireString(record.scope, path + ".scope"),
    comparison_role: requireString(record.comparison_role, path + ".comparison_role"),
    restatement_status: requireString(record.restatement_status, path + ".restatement_status"),
    source_document_id: documentId,
    source_sha256: sourceSha256.toLowerCase(),
    verification_evidences: requireArray(record.verification_evidences, path + ".verification_evidences").map(
      (item, index) => readEvidence(item, path + ".verification_evidences[" + index + "]", documentId, expectedSha256),
    ),
    verifications: requireArray(record.verifications, path + ".verifications").map((item, index) =>
      readMetricVerification(item, path + ".verifications[" + index + "]"),
    ),
    limitations: readStringArray(record.limitations, path + ".limitations"),
  };
}

function readEvidence(value: unknown, path: string, documentId: string, sourceSha256: string): VerificationEvidence {
  const record = requireRecord(value, path);
  const evidenceId = requireString(record.evidence_id, path + ".evidence_id");
  const actualDocumentId = requireString(record.document_id, path + ".document_id");
  const actualSha256 = requireString(record.source_sha256, path + ".source_sha256");
  const pdfPage = requireInteger(record.pdf_page, path + ".pdf_page");
  const valueRegion = requireRecord(record.value_region, path + ".value_region");
  const valuePage = requireInteger(valueRegion.page, path + ".value_region.page");
  if (
    !EVIDENCE_ID_PATTERN.test(evidenceId) ||
    actualDocumentId !== documentId ||
    actualSha256.toLowerCase() !== sourceSha256.toLowerCase() ||
    pdfPage < 1 ||
    valuePage !== pdfPage
  ) {
    throw invalidResponse(path);
  }
  return {
    evidence_id: evidenceId,
    document_id: actualDocumentId,
    source_sha256: actualSha256.toLowerCase(),
    pdf_page: pdfPage,
    table_title: nullableOptionalString(record.table_title, path + ".table_title"),
    row_label: nullableOptionalString(record.row_label, path + ".row_label"),
    column_label: nullableOptionalString(record.column_label, path + ".column_label"),
    value_raw: nullableOptionalString(record.value_raw, path + ".value_raw"),
    value_normalized: nullableOptionalString(record.value_normalized, path + ".value_normalized"),
    unit: nullableOptionalString(record.unit, path + ".unit"),
    currency: nullableOptionalString(record.currency, path + ".currency"),
    value_region: {
      text: optionalString(valueRegion.text) ?? undefined,
      page: valuePage,
      bbox: readBox(valueRegion.bbox, path + ".value_region.bbox"),
    },
  };
}

function readBox(value: unknown, path: string): PdfBox {
  const record = requireRecord(value, path);
  const box = {
    x0: requireFiniteNumber(record.x0, path + ".x0"),
    y0: requireFiniteNumber(record.y0, path + ".y0"),
    x1: requireFiniteNumber(record.x1, path + ".x1"),
    y1: requireFiniteNumber(record.y1, path + ".y1"),
  };
  if (box.x0 < 0 || box.y0 < 0 || box.x1 <= box.x0 || box.y1 <= box.y0) {
    throw invalidResponse(path);
  }
  return box;
}

function readMetricVerification(value: unknown, path: string): MetricVerification {
  const record = requireRecord(value, path);
  return {
    target_type: requireString(record.target_type, path + ".target_type"),
    target_id: requireString(record.target_id, path + ".target_id"),
    status: requireString(record.status, path + ".status"),
    evidence_ids: readStringArray(record.evidence_ids, path + ".evidence_ids"),
  };
}

function readCalculation(value: unknown, path: string): ConfirmedCalculation {
  const record = requireRecord(value, path);
  const independent = record.independent_verification;
  return {
    calculation_id: requireString(record.calculation_id, path + ".calculation_id"),
    formula_id: requireString(record.formula_id, path + ".formula_id"),
    formula_expression: requireString(record.formula_expression, path + ".formula_expression"),
    input_fact_ids: readStringArray(record.input_fact_ids, path + ".input_fact_ids"),
    output_value: nullableScalar(record.output_value, path + ".output_value"),
    unit: nullableString(record.unit, path + ".unit"),
    status: requireString(record.status, path + ".status"),
    failure_reason: nullableString(record.failure_reason, path + ".failure_reason"),
    independent_verification:
      independent === null ? null : readCalculationVerification(independent, path + ".independent_verification"),
  };
}

function readCalculationVerification(value: unknown, path: string) {
  const record = requireRecord(value, path);
  return {
    status: requireString(record.status, path + ".status"),
    calculation_id: record.calculation_id === undefined ? undefined : requireString(record.calculation_id, path + ".calculation_id"),
    recomputed_value: nullableScalar(record.recomputed_value, path + ".recomputed_value"),
    reason: nullableString(record.reason, path + ".reason"),
    checks: record.checks === undefined ? undefined : readStringArray(record.checks, path + ".checks"),
  };
}

function readClaim(value: unknown, path: string): VerifiedClaim {
  const record = requireRecord(value, path);
  const claimId = requireString(record.claim_id, path + ".claim_id");
  const independent = record.independent_verification;
  return {
    claim_id: claimId,
    claim_type: requireString(record.claim_type, path + ".claim_type"),
    text: requireString(record.text, path + ".text"),
    supporting_fact_ids: readStringArray(record.supporting_fact_ids, path + ".supporting_fact_ids"),
    supporting_evidence_ids: readStringArray(record.supporting_evidence_ids, path + ".supporting_evidence_ids"),
    calculation_ids: readStringArray(record.calculation_ids, path + ".calculation_ids"),
    verification_status: requireString(record.verification_status, path + ".verification_status"),
    limitations: readStringArray(record.limitations, path + ".limitations"),
    alternative_explanations: readStringArray(record.alternative_explanations, path + ".alternative_explanations"),
    follow_up_items: readStringArray(record.follow_up_items, path + ".follow_up_items"),
    independent_verification:
      independent === null
        ? null
        : readClaimVerification(independent, path + ".independent_verification", claimId),
  };
}

function readClaimVerification(value: unknown, path: string, expectedClaimId: string): ClaimVerification {
  const record = requireRecord(value, path);
  const claimId = requireString(record.claim_id, path + ".claim_id");
  if (claimId !== expectedClaimId) {
    throw invalidResponse(path + ".claim_id");
  }
  return {
    claim_id: claimId,
    status: requireString(record.status, path + ".status"),
    reason: nullableString(record.reason, path + ".reason"),
    expected_text: nullableString(record.expected_text, path + ".expected_text"),
  };
}

function readCandidate(value: unknown, path: string): CandidateSignal {
  const record = requireRecord(value, path);
  return {
    signal_id: requireString(record.signal_id, path + ".signal_id"),
    status: requireString(record.status, path + ".status"),
    reason: nullableString(record.reason, path + ".reason"),
    input_fact_ids: readStringArray(record.input_fact_ids, path + ".input_fact_ids"),
    calculation_ids: readStringArray(record.calculation_ids, path + ".calculation_ids"),
    left_difference: nullableScalar(record.left_difference, path + ".left_difference"),
    right_difference: nullableScalar(record.right_difference, path + ".right_difference"),
    fraud_conclusion: nullableString(record.fraud_conclusion, path + ".fraud_conclusion"),
    placement_reasons: readStringArray(record.placement_reasons, path + ".placement_reasons"),
  };
}

function readComparability(value: Record<string, unknown>, path: string) {
  return {
    status: requireString(value.status, path + ".status"),
    restatement_status: requireString(value.restatement_status, path + ".restatement_status"),
    limitations: readStringArray(value.limitations, path + ".limitations"),
    document_id: nullableOptionalString(value.document_id, path + ".document_id"),
    source_sha256: nullableOptionalString(value.source_sha256, path + ".source_sha256"),
    report_year: nullableOptionalInteger(value.report_year, path + ".report_year"),
    current_year: nullableOptionalInteger(value.current_year, path + ".current_year"),
    comparative_year: nullableOptionalInteger(value.comparative_year, path + ".comparative_year"),
  };
}

function readRecordArray(value: unknown, path: string): AnnualReportRecord[] {
  return requireArray(value, path).map((item, index) => requireRecord(item, path + "[" + index + "]"));
}

function readStringArray(value: unknown, path: string): string[] {
  return requireArray(value, path).map((item, index) => requireString(item, path + "[" + index + "]"));
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  const record = optionalRecord(value);
  if (record === null) {
    throw invalidResponse(path);
  }
  return record;
}

function optionalRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function requireArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) {
    throw invalidResponse(path);
  }
  return value;
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== "string") {
    throw invalidResponse(path);
  }
  return value;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function nullableString(value: unknown, path: string): string | null {
  if (value === null || typeof value === "string") {
    return value;
  }
  throw invalidResponse(path);
}

function nullableBoolean(value: unknown, path: string): boolean | null {
  if (value === null || typeof value === "boolean") {
    return value;
  }
  throw invalidResponse(path);
}

function nullableOptionalString(value: unknown, path: string): string | null | undefined {
  if (value === undefined) {
    return undefined;
  }
  return nullableString(value, path);
}

function requireInteger(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw invalidResponse(path);
  }
  return value;
}

function nullableOptionalInteger(value: unknown, path: string): number | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  return requireInteger(value, path);
}

function requireFiniteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw invalidResponse(path);
  }
  return value;
}

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") {
    throw invalidResponse(path);
  }
  return value;
}

function requireScalarText(value: unknown, path: string): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  throw invalidResponse(path);
}

function nullableScalar(value: unknown, path: string): string | number | null {
  if (value === null) {
    return null;
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  throw invalidResponse(path);
}

function invalidResponse(path: string): AnnualReportApiError {
  return new AnnualReportApiError("年度分析响应字段 " + path + " 格式无效，未显示该响应。");
}
