import type {
  AnnualAnalysisManifest,
  AnnualAnalysisReport,
  AnnualAnalysisResponse,
  AnnualReportRecord,
  CandidateSignal,
  ConfirmedCalculation,
  ConfirmedMetric,
  ClaimVerification,
  MetricVerification,
  M3AnnualScreening,
  M3AnnualScreeningRule,
  PdfBox,
  VerificationEvidence,
  VerifiedClaim,
} from "../types/annualReport";

const ANNUAL_ANALYSIS_PATH = "/api/v1/annual-analyses";
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;
const EVIDENCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

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

export async function loadVerificationEvidencePreview(
  runId: string,
  evidence: VerificationEvidence,
  expectedReportSha256: string,
): Promise<{ blob: Blob; pageNumber: number }> {
  if (!RUN_ID_PATTERN.test(runId) || !EVIDENCE_ID_PATTERN.test(evidence.evidence_id)) {
    throw new AnnualReportApiError("运行 ID 或核验证据 ID 格式无效。");
  }
  const url =
    ANNUAL_ANALYSIS_PATH +
    "/" +
    encodeURIComponent(runId) +
    "/evidence/" +
    encodeURIComponent(evidence.evidence_id) +
    "/preview.png";
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
  const pageHeader = response.headers.get("X-PDF-Page");
  const pageNumber = pageHeader === null ? NaN : Number(pageHeader);
  if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber !== evidence.value_region.page) {
    throw new AnnualReportApiError("证据预览页码与核验证据记录不一致，已停止显示。", response.status);
  }
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
  return { blob, pageNumber };
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
  return { report, manifest, verification_evidences: verificationEvidences };
}

function readManifest(value: Record<string, unknown>, path: string): AnnualAnalysisManifest {
  const manifest: AnnualAnalysisManifest = {
    run_id: requireString(value.run_id, path + ".run_id"),
    status: requireString(value.status, path + ".status"),
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
  const comparabilityValue = value.comparability;
  const comparability =
    comparabilityValue === null
      ? null
      : readComparability(requireRecord(comparabilityValue, path + ".comparability"), path + ".comparability");
  return {
    kind: "fintrace_annual_analysis_report",
    title: optionalString(value.title) ?? undefined,
    run_id: requireString(value.run_id, path + ".run_id"),
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
    issues: readStringArray(record.issues, path + ".issues"),
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
