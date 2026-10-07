import type {
  AnnualAnalysisManifest,
  AnnualAnalysisModelReview,
  AnnualAnalysisReport,
  AnnualAnalysisResponse,
  AnnualAnalysisSnapshot,
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
  AnnualDiscoveryExecutionContract,
  AnnualDiscoveryExecutionBinding,
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
const SAFE_DISCOVERY_FAILURE_REASONS = new Set([
  "all_findings_rejected",
  "batch_missing",
  "definitive_conclusion_forbidden",
  "findings_invalid",
  "invalid_model_json",
  "invalid_model_response",
  "invalid_model_schema",
  "limitations_invalid",
  "limitation_contains_control_character",
  "limitation_contains_unquoted_digit",
  "limitation_invalid",
  "model_call_failed",
  "model_call_timeout",
  "model_response_too_large",
  "serialized_input_limit_breached",
]);
const SAFE_EXECUTION_ATTEMPT_REASONS = new Set([
  "http_401", "http_403", "http_429", "http_5xx", "http_4xx", "connection_error", "timeout",
  "invalid_response", "transport_error", "cancelled", "ledger_budget_exhausted",
  "ledger_provider_mismatch", "ledger_source_context_missing_or_unapproved", "ledger_unavailable",
  "ledger_policy_mismatch", "call_gate_closed", "call_deadline_exhausted", "audit_write_failed", "call_failed",
  "worker_error", "invalid_request", "invalid_timeout",
]);
const SAFE_UNATTEMPTED_BATCH_REASONS = new Set([
  "awaiting_model", "workflow_time_budget_exhausted", "user_cancelled", "provider_budget_exhausted",
  "provider_admission_rejected", "authentication_failure_stopped", "consecutive_transport_failures_stopped",
  "parent_call_gate_closed",
]);
const SAFE_V3_BATCH_FAILURE_REASONS = new Set([
  "all_findings_rejected", "batch_missing", "definitive_conclusion_forbidden", "findings_invalid",
  "invalid_model_json", "invalid_model_response", "invalid_model_schema", "limitations_invalid",
  "limitation_contains_control_character", "limitation_contains_unquoted_digit", "limitation_invalid",
  "model_call_failed", "model_call_timeout", "model_response_too_large", "serialized_input_limit_breached",
  "audit_write_failed",
]);

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
  snapshot?: AnnualAnalysisSnapshot;
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
  const snapshot = readOptionalExportSnapshot(response.headers);
  return { blob: await response.blob(), filename, ...(snapshot === undefined ? {} : { snapshot }) };
}

function readOptionalExportSnapshot(headers: Headers): AnnualAnalysisSnapshot | undefined {
  const state = headers.get("X-Fintrace-Snapshot-State");
  const sequence = headers.get("X-Fintrace-Snapshot-Sequence");
  const isPartial = headers.get("X-Fintrace-Snapshot-Is-Partial");
  const parentRunIdHeader = headers.get("X-Fintrace-Snapshot-Parent-Run-Id");
  const present = [state, sequence, isPartial, parentRunIdHeader].filter((value) => value !== null).length;
  if (present === 0) return undefined;
  if (present !== 4 || state === null || sequence === null || isPartial === null || parentRunIdHeader === null) {
    throw new AnnualReportApiError("报告导出缺少完整快照状态头，已停止下载。", null, "invalid_export_snapshot");
  }
  const parsedSequence = Number(sequence);
  const partial = isPartial === "true" ? true : isPartial === "false" ? false : null;
  const states = new Set<AnnualAnalysisSnapshot["state"]>([
    "running", "cancelled", "interrupted", "failed", "completed", "completed_with_issues",
  ]);
  const parentRunId = parentRunIdHeader === "" ? null : parentRunIdHeader;
  if (
    !states.has(state as AnnualAnalysisSnapshot["state"]) ||
    !Number.isSafeInteger(parsedSequence) || parsedSequence < 1 ||
    partial === null ||
    (!partial && state !== "completed" && state !== "completed_with_issues") ||
    (partial && state === "completed") ||
    (parentRunId !== null && !RUN_ID_PATTERN.test(parentRunId))
  ) throw new AnnualReportApiError("报告导出快照状态头无效，已停止下载。", null, "invalid_export_snapshot");
  return {
    state: state as AnnualAnalysisSnapshot["state"],
    sequence: parsedSequence,
    is_partial: partial,
    parent_run_id: parentRunId,
  };
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
  const snapshot = payload.snapshot === undefined ? undefined : readSnapshot(payload.snapshot);
  const rawReport = requireRecord(payload.report, "report");
  const rawManifest = requireRecord(payload.manifest, "manifest");
  const report = readReport(rawReport, "report");
  const manifest = readManifest(rawManifest, "manifest", snapshot?.is_partial === true);
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
  if (
    report.model_discovery?.execution_contract !== undefined &&
    manifest.mode !== report.model_discovery.execution_contract.binding.analysis_mode
  ) throw new AnnualReportApiError("AI 执行绑定与原始分析模式不一致，未显示该报告。");
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
  if (snapshot !== undefined) {
    const partialManifestMatches = snapshot.state === "running"
      ? manifest.status === "running" || manifest.status === "completed" || manifest.status === "completed_with_issues"
      : snapshot.state === "cancelled" || snapshot.state === "interrupted"
        ? manifest.status === "interrupted" || manifest.status === "completed" || manifest.status === "completed_with_issues"
        : snapshot.state === "failed"
          ? manifest.status === "failed" || manifest.status === "interrupted" || manifest.status === "running" || manifest.status === "completed" || manifest.status === "completed_with_issues"
        : snapshot.state === "completed_with_issues" && manifest.status === "completed_with_issues";
    if (
      snapshot.is_partial && !partialManifestMatches ||
      !snapshot.is_partial && (snapshot.state !== "completed" && snapshot.state !== "completed_with_issues" || manifest.status !== snapshot.state) ||
      report.model_discovery?.execution_contract === undefined ||
      report.model_discovery.execution_contract.parent_run_id !== snapshot.parent_run_id
    ) throw invalidResponse("snapshot 未与部分执行契约和父运行绑定");
  }
  return {
    report,
    manifest,
    source_provenance: sourceProvenance,
    verification_evidences: verificationEvidences,
    ...(snapshot === undefined ? {} : { snapshot }),
    ...(discoveryEvidenceIndex === undefined ? {} : { discovery_evidences: discoveryEvidenceIndex }),
  };
}

function readSnapshot(value: unknown): AnnualAnalysisSnapshot {
  const record = requireRecord(value, "snapshot");
  exactKeys(record, ["state", "sequence", "is_partial", "parent_run_id"], "snapshot");
  const state = requireString(record.state, "snapshot.state");
  const states = new Set(["running", "cancelled", "interrupted", "failed", "completed", "completed_with_issues"]);
  const sequence = safeNonNegativeInteger(record.sequence, "snapshot.sequence");
  const isPartial = requireBoolean(record.is_partial, "snapshot.is_partial");
  const parentRunId = nullableString(record.parent_run_id, "snapshot.parent_run_id");
  if (
    !states.has(state) || sequence < 1 ||
    (!isPartial && state !== "completed" && state !== "completed_with_issues") ||
    (isPartial && state === "completed") ||
    (parentRunId !== null && !RUN_ID_PATTERN.test(parentRunId))
  ) throw invalidResponse("snapshot 状态、序号或父运行无效");
  return { state: state as AnnualAnalysisSnapshot["state"], sequence, is_partial: isPartial, parent_run_id: parentRunId };
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
    ...(Object.prototype.hasOwnProperty.call(value, "execution_contract") ? ["execution_contract"] : []),
  ], path);
  const sourceSha256 = requireString(value.source_sha256, `${path}.source_sha256`).toLowerCase();
  const status = requireString(value.status, `${path}.status`);
  const promptVersion = requireString(value.prompt_version, `${path}.prompt_version`);
  if (
    value.kind !== "fintrace_annual_discovery" ||
    promptVersion !== "annual_discovery_v1" && promptVersion !== "annual_discovery_v2" ||
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
  const executionV3 = Object.prototype.hasOwnProperty.call(value, "execution_contract");
  if (executionV3 && promptVersion !== "annual_discovery_v2") {
    throw invalidResponse(`${path}.execution_contract 只能绑定 annual_discovery_v2`);
  }
  const coverage = readDiscoveryCoverage(value.coverage, `${path}.coverage`, status, promptVersion, executionV3);
  const executionContract = executionV3
    ? readExecutionContract(value.execution_contract, `${path}.execution_contract`, coverage, expected)
    : undefined;
  if (executionContract === undefined) {
    if (
      status === "not_called" && (modelCallAttemptCount !== 0 || modelCallCount !== 0) ||
      coverage.source_status === "parsed" && (
        modelCallAttemptCount !== coverage.processed_batches ||
        coverage.successful_batches + (promptVersion === "annual_discovery_v2" ? coverage.partial_batches ?? 0 : 0) > modelCallCount ||
        modelCallCount > modelCallAttemptCount
      ) ||
      status === "completed" && (
        modelCallCount !== coverage.planned_batches || modelCallAttemptCount !== coverage.planned_batches
      )
    ) throw invalidResponse(`${path} 的状态与调用/批次计数不一致`);
  } else {
    if (
      modelCallAttemptCount !== executionContract.provider_admission_count ||
      modelCallCount !== executionContract.response_count ||
      modelCalled !== (executionContract.provider_admission_count > 0) ||
      (status === "not_called" && (modelCallAttemptCount !== 0 || modelCallCount !== 0))
    ) throw invalidResponse(`${path} 的 v3 provider 调用计数不一致`);
    const discoveryAttemptedChars = coverage.batch_results.reduce((sum, batch) => {
      const executionBatch = executionContract.batches[batch.batch_index - 1];
      return sum + (executionBatch.resolution === "executed" && executionBatch.attempts.some((attempt) => attempt.provider_admitted)
        ? batch.raw_chars
        : 0);
    }, 0);
    if (coverage.attempted_chars !== discoveryAttemptedChars) {
      throw invalidResponse(`${path}.coverage.attempted_chars 与 provider 准入批次不一致`);
    }
  }
  const evidences = requireArray(value.evidences, `${path}.evidences`).map((item, index) =>
    readDiscoveryEvidence(item, `${path}.evidences[${index}]`, expected.sourceDocumentId, sourceSha256, coverage.total_pages),
  );
  const evidenceById = new Map<string, AnnualDiscoveryEvidence>();
  for (const evidence of evidences) {
    if (evidenceById.has(evidence.evidence_id)) throw invalidResponse(`${path}.evidences 包含重复 ID`);
    evidenceById.set(evidence.evidence_id, evidence);
  }
  const findings = requireArray(value.findings, `${path}.findings`).map((item, index) =>
    readDiscoveryFinding(item, `${path}.findings[${index}]`, sourceSha256, evidenceById, promptVersion, executionContract !== undefined),
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
    coverage.successful_batches + (promptVersion === "annual_discovery_v2" ? coverage.partial_batches ?? 0 : 0) === 0 &&
      (findings.length > 0 || evidences.length > 0) ||
    promptVersion === "annual_discovery_v2" &&
      findings.length > coverage.batch_results.reduce((sum, batch) => sum + (batch.accepted_findings ?? 0), 0)
  ) throw invalidResponse(`${path} 的发现数量、意见、引用或证据不一致`);
  if (executionContract !== undefined) {
    const findingsByBatch = new Map<number, number>();
    for (const finding of findings) {
      const batchIndex = finding.batch_index;
      if (batchIndex === undefined || batchIndex > coverage.planned_batches) {
        throw invalidResponse(`${path}.findings 缺少有效的 v3 批次绑定`);
      }
      findingsByBatch.set(batchIndex, (findingsByBatch.get(batchIndex) ?? 0) + 1);
    }
    if (coverage.batch_results.some((batch) => batch.accepted_findings !== (findingsByBatch.get(batch.batch_index) ?? 0))) {
      throw invalidResponse(`${path}.findings 的 v3 批次绑定与 accepted_findings 不一致`);
    }
  }
  const auditArtifacts = requireArray(value.audit_artifacts, `${path}.audit_artifacts`).map((item, index) =>
    readDiscoveryAuditArtifact(item, `${path}.audit_artifacts[${index}]`, promptVersion),
  );
  if (auditArtifacts.length > 128 || new Set(auditArtifacts.map((item) => item.batch_index)).size !== auditArtifacts.length) {
    throw invalidResponse(`${path}.audit_artifacts 数量或批次重复`);
  }
  if (executionContract !== undefined) {
    const expectedAuditBatches = new Set(executionContract.batches
      .filter((batch) => batch.resolution === "executed" && batch.attempts.length > 0)
      .map((batch) => batch.batch_index));
    const actualAuditBatches = new Set(auditArtifacts.map((artifact) => artifact.batch_index));
    if (
      expectedAuditBatches.size !== actualAuditBatches.size ||
      [...expectedAuditBatches].some((batchIndex) => !actualAuditBatches.has(batchIndex))
    ) throw invalidResponse(`${path}.audit_artifacts 与 v3 执行批次不一致`);
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
    prompt_version: promptVersion,
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
    ...(executionContract === undefined ? {} : { execution_contract: executionContract }),
  };
}

function readExecutionContract(
  value: unknown,
  path: string,
  coverage: AnnualDiscoveryCoverage,
  expected: { runId: string; companyId: string; reportYear: number; sourceDocumentId: string; sourceSha256: string },
): AnnualDiscoveryExecutionContract {
  const record = requireRecord(value, path);
  exactKeys(record, [
    "kind", "plan_sha256", "identity_sha256", "binding", "parent_run_id", "provider_admission_count",
    "parent_provider_admission_count", "cumulative_provider_admission_count", "response_count",
    "reused_batches", "batches",
  ], path);
  const planSha256 = requireString(record.plan_sha256, `${path}.plan_sha256`).toLowerCase();
  const identitySha256 = requireString(record.identity_sha256, `${path}.identity_sha256`).toLowerCase();
  const binding = readExecutionBinding(record.binding, `${path}.binding`, expected);
  const parentRunId = nullableString(record.parent_run_id, `${path}.parent_run_id`);
  const providerAdmissionCount = safeNonNegativeInteger(record.provider_admission_count, `${path}.provider_admission_count`);
  const parentProviderAdmissionCount = safeNonNegativeInteger(record.parent_provider_admission_count, `${path}.parent_provider_admission_count`);
  const cumulativeProviderAdmissionCount = safeNonNegativeInteger(record.cumulative_provider_admission_count, `${path}.cumulative_provider_admission_count`);
  const responseCount = safeNonNegativeInteger(record.response_count, `${path}.response_count`);
  if (
    record.kind !== "fintrace_annual_discovery_execution_v3" ||
    !SHA256_PATTERN.test(planSha256) || !SHA256_PATTERN.test(identitySha256) ||
    planSha256 !== binding.plan_sha256.toLowerCase() ||
    (parentRunId !== null && (!RUN_ID_PATTERN.test(parentRunId) || parentRunId === expected.runId)) ||
    (parentRunId === null && parentProviderAdmissionCount !== 0) ||
    cumulativeProviderAdmissionCount !== parentProviderAdmissionCount + providerAdmissionCount ||
    responseCount > providerAdmissionCount || providerAdmissionCount > 4096 ||
    parentProviderAdmissionCount > 4096 || cumulativeProviderAdmissionCount > 8192
  ) throw invalidResponse(`${path} 执行身份或调用统计无效`);
  const auditIdsAcrossBatches = new Set<string>();
  const admissionIdsAcrossBatches = new Set<string>();
  const rawBatches = requireArray(record.batches, `${path}.batches`);
  const batches = rawBatches.map((rawBatch, batchIndex) => {
    const batchPath = `${path}.batches[${batchIndex}]`;
    const batch = requireRecord(rawBatch, batchPath);
    exactKeys(batch, ["batch_index", "state", "resolution", "reason", "attempts"], batchPath);
    const index = safeNonNegativeInteger(batch.batch_index, `${batchPath}.batch_index`);
    const state = requireString(batch.state, `${batchPath}.state`);
    const resolution = requireString(batch.resolution, `${batchPath}.resolution`);
    const reason = nullableString(batch.reason, `${batchPath}.reason`);
    if (
      index !== batchIndex + 1 ||
      state !== "succeeded" && state !== "partial" && state !== "failed" && state !== "unattempted" ||
      resolution !== "executed" && resolution !== "reused" && resolution !== "unattempted"
    ) throw invalidResponse(`${batchPath} 批次编号或状态无效`);
    let previousAttemptIndex = 0;
    const attempts = requireArray(batch.attempts, `${batchPath}.attempts`).map((rawAttempt, attemptIndex) => {
      const attemptPath = `${batchPath}.attempts[${attemptIndex}]`;
      const attempt = requireRecord(rawAttempt, attemptPath);
      exactKeys(attempt, ["attempt_index", "audit_id", "admission_id", "provider_admitted", "status", "response_accepted", "accepted_findings", "reason"], attemptPath);
      const attemptIndexValue = safeNonNegativeInteger(attempt.attempt_index, `${attemptPath}.attempt_index`);
      const auditId = requireString(attempt.audit_id, `${attemptPath}.audit_id`);
      const admissionId = nullableString(attempt.admission_id, `${attemptPath}.admission_id`);
      const providerAdmitted = requireBoolean(attempt.provider_admitted, `${attemptPath}.provider_admitted`);
      const attemptStatus = requireString(attempt.status, `${attemptPath}.status`);
      const responseAccepted = requireBoolean(attempt.response_accepted, `${attemptPath}.response_accepted`);
      const acceptedFindings = safeNonNegativeInteger(attempt.accepted_findings, `${attemptPath}.accepted_findings`);
      const reason = nullableString(attempt.reason, `${attemptPath}.reason`);
      if (
        attemptIndexValue !== previousAttemptIndex + 1 ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(auditId) ||
        (admissionId !== null && !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(admissionId)) ||
        attemptStatus !== "succeeded" && attemptStatus !== "failed" && attemptStatus !== "rejected" ||
        (attemptStatus === "succeeded" ? reason !== null : reason === null || !SAFE_EXECUTION_ATTEMPT_REASONS.has(reason)) ||
        (providerAdmitted !== (admissionId !== null)) ||
        (attemptStatus === "rejected") !== !providerAdmitted ||
        (attemptStatus === "succeeded") !== responseAccepted ||
        (!responseAccepted && acceptedFindings !== 0) || acceptedFindings > 6
      ) throw invalidResponse(`${attemptPath} 调用尝试字段无效`);
      if (auditIdsAcrossBatches.has(auditId)) {
        throw invalidResponse(`${attemptPath}.audit_id 在 execution_contract 中重复`);
      }
      auditIdsAcrossBatches.add(auditId);
      if (admissionId !== null && admissionIdsAcrossBatches.has(admissionId)) {
        throw invalidResponse(`${attemptPath}.admission_id 在 execution_contract 中重复`);
      }
      if (admissionId !== null) admissionIdsAcrossBatches.add(admissionId);
      previousAttemptIndex = attemptIndexValue;
      return {
        attempt_index: attemptIndexValue,
        audit_id: auditId,
        admission_id: admissionId,
        provider_admitted: providerAdmitted,
        status: attemptStatus as "succeeded" | "failed" | "rejected",
        response_accepted: responseAccepted,
        accepted_findings: acceptedFindings,
        reason,
      };
    });
    const admittedInBatch = attempts.some((attempt) => attempt.provider_admitted);
    const batchReasonValid = state === "succeeded"
      ? reason === null
      : state === "partial"
        ? reason === "some_findings_rejected"
        : state === "failed"
          ? SAFE_V3_BATCH_FAILURE_REASONS.has(reason ?? "")
          : SAFE_UNATTEMPTED_BATCH_REASONS.has(reason ?? "");
    if (
      (state === "unattempted") !== (resolution === "unattempted") ||
      resolution === "reused" && (state !== "succeeded" || attempts.length !== 0) ||
      resolution === "executed" && (state === "unattempted" || attempts.length === 0) ||
      resolution !== "executed" && attempts.length !== 0 ||
      resolution === "unattempted" && admittedInBatch ||
      !batchReasonValid ||
      state === "unattempted" && attempts.length !== 0
    ) throw invalidResponse(`${batchPath} 状态与执行来源不一致`);
    return {
      batch_index: index,
      state: state as AnnualDiscoveryExecutionContract["batches"][number]["state"],
      resolution: resolution as AnnualDiscoveryExecutionContract["batches"][number]["resolution"],
      reason,
      attempts,
    };
  });
  const reusedBatches = requireArray(record.reused_batches, `${path}.reused_batches`).map((item, index) =>
    safeNonNegativeInteger(item, `${path}.reused_batches[${index}]`),
  );
  const admittedCount = batches.reduce((sum, batch) => sum + batch.attempts.filter((attempt) => attempt.provider_admitted).length, 0);
  const acceptedResponseCount = batches.reduce((sum, batch) => sum + batch.attempts.filter((attempt) => attempt.response_accepted).length, 0);
  const expectedReused = batches.filter((batch) => batch.resolution === "reused").map((batch) => batch.batch_index);
  const coverageByIndex = new Map(coverage.batch_results.map((batch) => [batch.batch_index, batch]));
  if (
    batches.length !== coverage.planned_batches ||
    coverage.source_status === "unavailable" && batches.length !== 0 ||
    admittedCount !== providerAdmissionCount ||
    acceptedResponseCount !== responseCount ||
    !sameJsonValue(reusedBatches, expectedReused) ||
    new Set(reusedBatches).size !== reusedBatches.length ||
    reusedBatches.length > 0 && parentRunId === null ||
    reusedBatches.some((index, position) => index < 1 || index > coverage.planned_batches || (position > 0 && reusedBatches[position - 1] >= index)) ||
    batches.some((batch) => {
      const covered = coverageByIndex.get(batch.batch_index);
      const attemptAccepted = batch.attempts.reduce((sum, attempt) => sum + attempt.accepted_findings, 0);
      return covered === undefined || covered.status !== batch.state || covered.resolution !== batch.resolution ||
        covered.reason !== batch.reason || covered.accepted_findings === undefined || covered.inherited_findings === undefined ||
        covered.accepted_findings > covered.inherited_findings + attemptAccepted ||
        batch.resolution === "reused" && covered.accepted_findings !== covered.inherited_findings;
    })
  ) throw invalidResponse(`${path} 与覆盖记录中的批次或准入数不一致`);
  return {
    kind: "fintrace_annual_discovery_execution_v3",
    plan_sha256: planSha256,
    identity_sha256: identitySha256,
    binding,
    parent_run_id: parentRunId,
    provider_admission_count: providerAdmissionCount,
    parent_provider_admission_count: parentProviderAdmissionCount,
    cumulative_provider_admission_count: cumulativeProviderAdmissionCount,
    response_count: responseCount,
    reused_batches: reusedBatches,
    batches,
  };
}

function readExecutionBinding(
  value: unknown,
  path: string,
  expected: { companyId: string; reportYear: number; sourceDocumentId: string; sourceSha256: string },
): AnnualDiscoveryExecutionBinding {
  const record = requireRecord(value, path);
  exactKeys(record, [
    "source_sha256", "company_id", "report_year", "source_document_id", "analysis_mode",
    "provider_model_fingerprint", "model", "prompts_sha256", "parser_version", "plan_sha256",
    "m3_binding_sha256",
  ], path);
  const sourceSha256 = requireString(record.source_sha256, `${path}.source_sha256`).toLowerCase();
  const companyId = requireString(record.company_id, `${path}.company_id`);
  const reportYear = requireInteger(record.report_year, `${path}.report_year`);
  const sourceDocumentId = requireString(record.source_document_id, `${path}.source_document_id`);
  const analysisMode = requireString(record.analysis_mode, `${path}.analysis_mode`);
  const providerFingerprint = requireString(record.provider_model_fingerprint, `${path}.provider_model_fingerprint`).toLowerCase();
  const model = requireString(record.model, `${path}.model`);
  const promptsSha256 = requireString(record.prompts_sha256, `${path}.prompts_sha256`).toLowerCase();
  const parserVersion = requireString(record.parser_version, `${path}.parser_version`);
  const planSha256 = requireString(record.plan_sha256, `${path}.plan_sha256`).toLowerCase();
  const m3BindingSha256 = nullableString(record.m3_binding_sha256, `${path}.m3_binding_sha256`);
  if (
    sourceSha256 !== expected.sourceSha256.toLowerCase() || companyId !== expected.companyId ||
    reportYear !== expected.reportYear || sourceDocumentId !== expected.sourceDocumentId ||
    (analysisMode !== "model_investigation" && analysisMode !== "integrated_analysis") ||
    !SHA256_PATTERN.test(providerFingerprint) || !SHA256_PATTERN.test(promptsSha256) || !SHA256_PATTERN.test(planSha256) ||
    model.trim() === "" || model.length > 200 || /[\u0000-\u001f\u007f]/.test(model) ||
    parserVersion !== "annual-discovery-parser-v2-execution-v3" ||
    (analysisMode === "model_investigation" ? m3BindingSha256 !== null : m3BindingSha256 === null || !SHA256_PATTERN.test(m3BindingSha256))
  ) throw invalidResponse(`${path} 与原始年报或执行模式的绑定无效`);
  return {
    source_sha256: sourceSha256,
    company_id: companyId,
    report_year: reportYear,
    source_document_id: sourceDocumentId,
    analysis_mode: analysisMode,
    provider_model_fingerprint: providerFingerprint,
    model,
    prompts_sha256: promptsSha256,
    parser_version: parserVersion,
    plan_sha256: planSha256,
    m3_binding_sha256: m3BindingSha256,
  };
}

function readDiscoveryCoverage(
  value: unknown,
  path: string,
  status: string,
  promptVersion: AnnualModelDiscovery["prompt_version"],
  executionV3 = false,
): AnnualDiscoveryCoverage {
  const record = requireRecord(value, path);
  const isV2 = promptVersion === "annual_discovery_v2";
  const coverageFields = [
    "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches", "processed_batches",
    "successful_batches", "failed_batches", "batch_results", "processed_chars", "attempted_chars",
    "unattempted_batches", "unattempted_chars", "planned_chars", "omitted_chars", "unread_pages",
    "failed_pages", "no_text_pages", "image_pages", "unread_image_pages", "truncated", "complete",
    "source_status", "content_status", "time_budget_exhausted", "text_only_scope",
  ];
  if (isV2) coverageFields.splice(7, 0, "partial_batches");
  exactKeys(record, coverageFields, path);
  const countNames = [
    "total_pages", "text_pages", "total_blocks", "total_chars", "planned_batches", "processed_batches",
    "successful_batches", "failed_batches", "processed_chars", "attempted_chars", "unattempted_batches",
    "unattempted_chars", "planned_chars", "omitted_chars",
  ] as const;
  const counts = Object.fromEntries(countNames.map((key) => [key, safeNonNegativeInteger(record[key], `${path}.${key}`)])) as Record<(typeof countNames)[number], number>;
  const partialBatches = isV2 ? safeNonNegativeInteger(record.partial_batches, `${path}.partial_batches`) : 0;
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
  const batchResults = rawBatchResults.map((item, index) =>
    readDiscoveryBatchResult(item, `${path}.batch_results[${index}]`, counts.total_pages, promptVersion, executionV3),
  );
  if (sourceStatus === "unavailable") {
    if (
      contentStatus !== "unavailable" || countNames.some((field) => counts[field] !== 0) ||
      partialBatches !== 0 ||
      Object.values(pageLists).some((items) => items.length > 0) || complete || truncated || timeBudgetExhausted ||
      batchResults.length > 0 || status !== "failed" && status !== "not_called"
    ) throw invalidResponse(`${path} 不可用来源不能包含虚构覆盖`);
  } else {
    if (
      contentStatus === "unavailable" ||
      counts.processed_batches !== counts.successful_batches + partialBatches + counts.failed_batches ||
      counts.unattempted_batches !== counts.planned_batches - counts.processed_batches ||
      counts.planned_chars > counts.total_chars || counts.planned_chars > DISCOVERY_MAX_CHARS ||
      counts.attempted_chars > counts.planned_chars || counts.unattempted_chars > counts.planned_chars ||
      (!executionV3 && counts.processed_chars > counts.attempted_chars) || counts.processed_chars > counts.planned_chars ||
      counts.omitted_chars !== Math.max(counts.total_chars - counts.planned_chars, 0) + counts.unattempted_chars ||
      batchResults.length !== counts.planned_batches
    ) throw invalidResponse(`${path} 批次/字符计数关系无效`);
    const seen = new Set<number>();
    const chars: Record<string, number> = { succeeded: 0, failed: 0, unattempted: 0, ...(isV2 ? { partial: 0 } : {}) };
    const batchPageSets: Record<string, Set<number>> = Object.fromEntries(
      Object.keys(chars).map((key) => [key, new Set<number>()]),
    );
    for (const [batchPosition, batch] of batchResults.entries()) {
      if (seen.has(batch.batch_index) || batch.batch_index > counts.planned_batches) throw invalidResponse(`${path} 批次编号重复或越界`);
      if (executionV3 && batch.batch_index !== batchPosition + 1) throw invalidResponse(`${path} v3 批次记录未按稳定计划排序`);
      seen.add(batch.batch_index);
      chars[batch.status] += batch.raw_chars;
      batch.pages.forEach((page) => batchPageSets[batch.status].add(page));
      if (!executionV3 && (batch.batch_index <= counts.processed_batches) === (batch.status === "unattempted")) {
        throw invalidResponse(`${path} 批次顺序与结束状态不一致`);
      }
    }
    const successCount = batchResults.filter((batch) => batch.status === "succeeded").length;
    const partialCount = batchResults.filter((batch) => batch.status === "partial").length;
    const failedCount = batchResults.filter((batch) => batch.status === "failed").length;
    const unattemptedCount = batchResults.filter((batch) => batch.status === "unattempted").length;
    if (
      seen.size !== counts.planned_batches || chars.succeeded + chars.failed + (chars.partial ?? 0) + chars.unattempted !== counts.planned_chars ||
      chars.succeeded !== counts.processed_chars ||
      (!executionV3 && chars.succeeded + chars.failed + (chars.partial ?? 0) !== counts.attempted_chars) ||
      chars.unattempted !== counts.unattempted_chars || successCount !== counts.successful_batches ||
      isV2 && partialCount !== partialBatches ||
      failedCount !== counts.failed_batches || unattemptedCount !== counts.unattempted_batches ||
      [...batchPageSets.failed].some((page) => !failedPages.has(page) || !unreadPages.has(page)) ||
      [...batchPageSets.unattempted].some((page) => !unreadPages.has(page)) ||
      isV2 && [...batchPageSets.partial].some((page) => !unreadPages.has(page)) ||
      batchPageSets.failed.size !== failedPages.size ||
      [...failedPages].some((page) => !batchPageSets.failed.has(page)) ||
      truncated !== Boolean(counts.omitted_chars || counts.unattempted_batches || counts.unattempted_chars)
    ) throw invalidResponse(`${path} 批次结果与汇总不一致`);
  }
  const expectedComplete = sourceStatus === "parsed" && contentStatus === "text_available" && counts.total_chars > 0 &&
    !truncated && pageLists.unread_image_pages.length === 0 && counts.planned_chars === counts.total_chars &&
    counts.processed_batches === counts.planned_batches && counts.failed_batches === 0 &&
    partialBatches === 0 &&
    counts.successful_batches === counts.planned_batches && counts.processed_chars === counts.planned_chars &&
    pageLists.unread_pages.length === 0;
  if (
    complete !== expectedComplete || status === "completed" && !expectedComplete ||
    status === "partial" && (
      expectedComplete || counts.successful_batches + (isV2 ? partialBatches : 0) === 0
    ) ||
    status === "failed" && sourceStatus === "parsed" && (
      counts.failed_batches === 0 || !executionV3 && counts.successful_batches !== 0 ||
      !executionV3 && isV2 && partialBatches !== 0
    ) ||
    status === "not_called" && sourceStatus === "parsed" && !executionV3 && counts.processed_batches !== 0
  ) throw invalidResponse(`${path} 完成状态与覆盖不一致`);
  return {
    ...counts,
    ...(isV2 ? { partial_batches: partialBatches } : {}),
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

function readDiscoveryBatchResult(
  value: unknown,
  path: string,
  totalPages: number,
  promptVersion: AnnualModelDiscovery["prompt_version"],
  executionV3 = false,
): AnnualDiscoveryBatchResult {
  const record = requireRecord(value, path);
  const isV2 = promptVersion === "annual_discovery_v2";
  exactKeys(record, [
    "batch_index", "status", "reason", "pages", "raw_chars",
    ...(isV2 ? ["accepted_findings", "rejected_findings", "restored_quotes"] : []),
    ...(executionV3 ? ["resolution", "inherited_findings"] : []),
  ], path);
  const index = safeNonNegativeInteger(record.batch_index, `${path}.batch_index`);
  const status = requireString(record.status, `${path}.status`);
  const reason = nullableString(record.reason, `${path}.reason`);
  const rawChars = safeNonNegativeInteger(record.raw_chars, `${path}.raw_chars`);
  const accepted = isV2 ? safeNonNegativeInteger(record.accepted_findings, `${path}.accepted_findings`) : 0;
  const rejected = isV2 ? safeNonNegativeInteger(record.rejected_findings, `${path}.rejected_findings`) : 0;
  const restored = isV2 ? safeNonNegativeInteger(record.restored_quotes, `${path}.restored_quotes`) : 0;
  const resolution = executionV3 ? requireString(record.resolution, `${path}.resolution`) : undefined;
  const inherited = executionV3 ? safeNonNegativeInteger(record.inherited_findings, `${path}.inherited_findings`) : undefined;
  const pages = requireArray(record.pages, `${path}.pages`).map((page, pageIndex) => safeNonNegativeInteger(page, `${path}.pages[${pageIndex}]`));
  if (
    index < 1 ||
    status !== "succeeded" && status !== "partial" && status !== "failed" && status !== "unattempted" ||
    !isV2 && status === "partial" ||
    rawChars < 1 || rawChars > 12_000 || pages.length === 0 ||
    pages.some((page) => page < 1 || page > totalPages) || new Set(pages).size !== pages.length ||
    pages.some((page, position) => position > 0 && pages[position - 1] >= page) ||
    status === "succeeded" && reason !== null ||
    status !== "succeeded" && (reason === null || !/^[a-z][a-z0-9_]{0,99}$/.test(reason)) ||
    isV2 && !executionV3 && (
      (!executionV3 && (accepted > 6 || rejected > 6 || accepted + rejected > 6 || restored > 24 || restored > accepted * 4)) ||
      accepted > 6 || rejected > 6 || accepted + rejected > 6 || restored > 24 || restored > accepted * 4 ||
      status === "succeeded" && rejected !== 0 ||
      status === "partial" && (accepted < 1 || rejected < 1 || reason !== "some_findings_rejected") ||
      status === "failed" && (
        accepted !== 0 || !SAFE_DISCOVERY_FAILURE_REASONS.has(reason ?? "") ||
        (reason === "all_findings_rejected") !== (rejected > 0)
      ) ||
      status === "unattempted" && (
        accepted !== 0 || rejected !== 0 || restored !== 0 ||
        reason !== "workflow_time_budget_exhausted"
      ) ||
      status !== "partial" && status !== "failed" && rejected !== 0
    ) ||
    executionV3 && isV2 && (
      accepted > 768 || rejected > 24_576 || restored > 98_304 || (inherited ?? 0) > 768 ||
      status === "partial" && reason !== "some_findings_rejected" ||
      status === "failed" && !SAFE_V3_BATCH_FAILURE_REASONS.has(reason ?? "") ||
      status === "unattempted" && !SAFE_UNATTEMPTED_BATCH_REASONS.has(reason ?? "") ||
      (resolution === "reused" || resolution === "unattempted") && (rejected !== 0 || restored !== 0)
    ) ||
    executionV3 && (
      (resolution !== "executed" && resolution !== "reused" && resolution !== "unattempted") ||
      (status === "unattempted") !== (resolution === "unattempted") ||
      resolution === "reused" && status !== "succeeded" ||
      status === "succeeded" && resolution === "unattempted"
    )
  ) throw invalidResponse(`${path} 字段无效`);
  return {
    batch_index: index,
    status: status as AnnualDiscoveryBatchResult["status"],
    reason,
    pages,
    raw_chars: rawChars,
    ...(isV2 ? {
      accepted_findings: accepted,
      rejected_findings: rejected,
      restored_quotes: restored,
    } : {}),
    ...(inherited === undefined ? {} : { inherited_findings: inherited }),
    ...(resolution === undefined ? {} : { resolution: resolution as AnnualDiscoveryBatchResult["resolution"] }),
  };
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
  promptVersion: AnnualModelDiscovery["prompt_version"],
  executionV3 = false,
): AnnualDiscoveryFinding {
  const isV2 = promptVersion === "annual_discovery_v2";
  const record = requireRecord(value, path);
  exactKeys(record, ["finding_id", "title", "observation", "why_it_matters", "follow_up", "priority", "classification", "verification_status", "evidence_ids", "citations", ...(executionV3 ? ["batch_index"] : [])], path);
  const findingId = requireString(record.finding_id, `${path}.finding_id`);
  const batchIndex = executionV3 ? safeNonNegativeInteger(record.batch_index, `${path}.batch_index`) : undefined;
  const priority = requireString(record.priority, `${path}.priority`);
  if (
    !new RegExp(`^disc:${sourceSha256.slice(0, 12)}-[0-9a-f]{20}$`).test(findingId) ||
    executionV3 && (batchIndex === undefined || batchIndex < 1) ||
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
  const citationIds = citations.map((citation) => citation.evidence_id);
  const citationPairs = citations.map((citation) => JSON.stringify([citation.evidence_id, citation.quote]));
  if (
    citations.length < 1 || citations.length > 4 ||
    new Set(evidenceIds).size !== evidenceIds.length ||
    isV2 && (
      !sameJsonValue(evidenceIds, [...new Set(citationIds)]) ||
      new Set(citationPairs).size !== citationPairs.length
    ) ||
    !isV2 && (
      evidenceIds.length !== citations.length ||
      new Set(citationIds).size !== citationIds.length ||
      evidenceIds.some((id) => !citationIds.includes(id))
    )
  ) throw invalidResponse(`${path} 引用 ID 与 evidence_ids 不一致`);
  return {
    finding_id: findingId,
    ...(batchIndex === undefined ? {} : { batch_index: batchIndex }),
    ...opinion,
    priority,
    classification: "unverified_model_finding",
    verification_status: "unverified",
    evidence_ids: evidenceIds,
    citations,
  };
}

function readDiscoveryAuditArtifact(
  value: unknown,
  path: string,
  expectedPromptVersion: AnnualModelDiscovery["prompt_version"],
): AnnualDiscoveryAuditArtifact {
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
    promptVersion !== expectedPromptVersion
  ) throw invalidResponse(`${path} 状态无效`);
  return { batch_index: batchIndex, ...paths, status, prompt_version: expectedPromptVersion };
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

function readManifest(value: Record<string, unknown>, path: string, partialSnapshot = false): AnnualAnalysisManifest {
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
    (manifest.status !== "completed" && manifest.status !== "completed_with_issues" &&
      !(partialSnapshot && (manifest.status === "running" || manifest.status === "interrupted" || manifest.status === "failed")))
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
  const auditArtifacts = readRecordArray(value.audit_artifacts, path + ".audit_artifacts");
  const reusedForMatchingInputs = reason === "review_reused_for_matching_inputs";
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
    (status === "completed" && ((!reusedForMatchingInputs && !modelCalled) || summary === null || summary.trim() === "")) ||
    (reusedForMatchingInputs && (
      status !== "completed" || modelCalled || callCount !== 0 || callAttemptCount !== 0 ||
      !auditArtifacts.length || auditArtifacts.some((artifact, index) =>
        !isReusedAnnualReviewAudit(artifact, `${path}.audit_artifacts[${index}]`),
      )
    )) ||
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
    audit_artifacts: auditArtifacts,
    source_identity: sourceIdentity,
  };
}

function isReusedAnnualReviewAudit(value: AnnualReportRecord, path: string): boolean {
  exactKeys(value, ["audit_dir", "request_path", "response_path", "failure_path", "status", "prompt_version"], path);
  const auditDir = requireString(value.audit_dir, `${path}.audit_dir`);
  const runIdPattern = "annual-analysis-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}";
  const auditIdPattern = "[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}";
  if (
    !new RegExp(`^runs/${runIdPattern}/${auditIdPattern}$`).test(auditDir) ||
    value.request_path !== `${auditDir}/request.json` ||
    value.response_path !== `${auditDir}/response.json` ||
    value.failure_path !== null ||
    value.status !== "succeeded" ||
    value.prompt_version !== "annual_review_v2"
  ) {
    return false;
  }
  return true;
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
