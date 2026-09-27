import type {
  AmountMatch,
  AnnualChange,
  AnnualChangeCalculation,
  AnnualPrecheckRecord,
  AnnualPrecheckRequest,
  AnnualScreening,
  AnnualScreeningItem,
  AnnualScreeningStatus,
  TextPdfUploadReceipt,
  FactColumnRole,
  FactExtraction,
  FactHit,
  FinancialFact,
  PrecheckCodeSnapshot,
  PrecheckInputs,
  PrecheckIssue,
  PrecheckIssueGroups,
  ScreeningFactHit,
  ScreeningFactInput,
  ScreeningValue,
  SourceAmountEvidence,
  SourceAmountResult,
  SourceAmountVerification,
} from "../types/precheck";

const PRECHECK_PATH = "/api/v1/annual-prechecks";
const UPLOAD_PATH = "/api/v1/text-pdf-uploads";
const MAX_TEXT_PDF_BYTES = 32 * 1024 * 1024;
const UPLOAD_COMPANY = /^[A-Za-z0-9][A-Za-z0-9._-]{0,64}$/;
const UPLOAD_DOCUMENT = /^upload-[0-9a-f]{32}$/;
const SHA256_HEX = /^[0-9a-f]{64}$/;

export interface TextPdfUploadIssues {
  file?: string;
  companyId?: string;
  reportYear?: string;
}

const REQUEST_FIELDS = {
  parsed_path: "解析结果相对路径",
  source_pdf_path: "原始 PDF 相对路径",
  company_id: "company_id",
  report_year: "report_year",
} as const;

export class PrecheckApiError extends Error {
  readonly status: number | null;
  readonly code: string | null;

  constructor(message: string, status: number | null = null, code: string | null = null) {
    super(message);
    this.name = "PrecheckApiError";
    this.status = status;
    this.code = code;
  }
}

export async function createAnnualPrecheck(
  body: AnnualPrecheckRequest,
): Promise<AnnualPrecheckRecord> {
  return requestPrecheck(PRECHECK_PATH, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json; charset=utf-8",
    },
    body: JSON.stringify(requestBody(body)),
  });
}

export function textPdfUploadIssues(
  file: File | null,
  companyId: string,
  reportYear: string,
): TextPdfUploadIssues {
  const issues: TextPdfUploadIssues = {};
  if (file === null) {
    issues.file = "请选择要上传的 PDF。";
  } else if (file.size === 0) {
    issues.file = "上传文件为空。";
  } else if (file.size > MAX_TEXT_PDF_BYTES) {
    issues.file = "上传超过 32 MiB 上限，未发送请求。";
  }
  if (companyId === "") {
    issues.companyId = "请填写 company_id。";
  } else if (!UPLOAD_COMPANY.test(companyId) || companyId.includes("..")) {
    issues.companyId = "company_id 只能包含字母、数字、点、下划线和短横线。";
  }
  if (!/^\d+$/.test(reportYear)) {
    issues.reportYear = "report_year 需要是 1900 到 2100 的整数。";
  } else {
    const year = Number(reportYear);
    if (!Number.isInteger(year) || year < 1900 || year > 2100) {
      issues.reportYear = "report_year 需要是 1900 到 2100 的整数。";
    }
  }
  return issues;
}

export async function uploadTextPdf(
  file: File,
  companyId: string,
  reportYear: string,
): Promise<TextPdfUploadReceipt> {
  const issues = textPdfUploadIssues(file, companyId, reportYear);
  const problem = issues.file ?? issues.companyId ?? issues.reportYear;
  if (problem !== undefined) {
    throw new PrecheckApiError(problem);
  }
  const body = new FormData();
  body.append("company_id", companyId);
  body.append("report_year", reportYear);
  body.append("file", file, "source.pdf");
  let response: Response;
  try {
    response = await fetch(UPLOAD_PATH, {
      method: "POST",
      headers: { Accept: "application/json" },
      body,
      cache: "no-store",
      credentials: "omit",
    });
  } catch {
    throw new PrecheckApiError(
      "无法连接本地预检服务。请确认后端已在 127.0.0.1:8000 启动，并由页面通过 /api 访问。",
    );
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const failure = messageFromFailure(response.status, payload, "上传");
    throw new PrecheckApiError(failure.message, response.status, failure.code);
  }
  return readUploadReceipt(payload, companyId, reportYear);
}

export async function loadAnnualPrecheck(runId: string): Promise<AnnualPrecheckRecord> {
  if (runId === "") {
    throw new PrecheckApiError("请填写 run_id。");
  }
  return requestPrecheck(`${PRECHECK_PATH}/${encodeURIComponent(runId)}`, {
    method: "GET",
    headers: { Accept: "application/json" },
  });
}

function requestBody(body: AnnualPrecheckRequest): AnnualPrecheckRequest {
  return {
    parsed_path: body.parsed_path,
    source_pdf_path: body.source_pdf_path,
    company_id: body.company_id,
    report_year: body.report_year,
  };
}

async function requestPrecheck(url: string, init: RequestInit): Promise<AnnualPrecheckRecord> {
  let response: Response;
  try {
    response = await fetch(url, { ...init, cache: "no-store", credentials: "omit" });
  } catch {
    throw new PrecheckApiError(
      "无法连接本地预检服务。请确认后端已在 127.0.0.1:8000 启动，并由页面通过 /api 访问。",
    );
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const failure = messageFromFailure(response.status, payload, "预检请求");
    throw new PrecheckApiError(failure.message, response.status, failure.code);
  }
  return readRecord(payload);
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.trim() === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new PrecheckApiError(
      `服务返回的内容不是 JSON（HTTP ${response.status}）。`,
      response.status,
    );
  }
}

function messageFromFailure(
  status: number,
  payload: unknown,
  action: string,
): { message: string; code: string | null } {
  if (!isRecord(payload) || !("detail" in payload)) {
    return { message: `${action}失败（HTTP ${status}）。`, code: null };
  }
  const detail = payload.detail;
  if (isRecord(detail)) {
    const code = typeof detail.code === "string" ? detail.code : null;
    if (typeof detail.message === "string" && detail.message.trim() !== "") {
      return { message: detail.message, code };
    }
    if (code !== null) {
      return { message: `${action}失败（HTTP ${status}，${code}）。`, code };
    }
    return { message: `${action}失败（HTTP ${status}）。`, code: null };
  }
  if (Array.isArray(detail)) {
    const parts = detail.map(validationText).filter((item) => item !== "");
    const message =
      parts.length > 0
        ? `请求字段未通过校验：${parts.join("；")}`
        : `请求字段未通过校验（HTTP ${status}）。`;
    return { message, code: null };
  }
  if (typeof detail === "string" && detail.trim() !== "") {
    return { message: detail, code: null };
  }
  return { message: `${action}失败（HTTP ${status}）。`, code: null };
}

function validationText(item: unknown): string {
  if (!isRecord(item)) {
    return "";
  }
  const message = typeof item.msg === "string" ? item.msg : "";
  const location = Array.isArray(item.loc)
    ? item.loc
        .filter((part): part is string | number => typeof part === "string" || typeof part === "number")
        .map((part) => fieldLabel(String(part)))
        .join(".")
    : "";
  if (location !== "" && message !== "") {
    return `${location}：${message}`;
  }
  return message || location;
}

function fieldLabel(name: string): string {
  if (name in REQUEST_FIELDS) {
    return REQUEST_FIELDS[name as keyof typeof REQUEST_FIELDS];
  }
  return name;
}

function readUploadReceipt(
  payload: unknown,
  companyId: string,
  reportYear: string,
): TextPdfUploadReceipt {
  const record = expectRecord(payload, "上传结果");
  const documentId = expectString(record, "document_id");
  const sourcePdfPath = expectString(record, "source_pdf_path");
  const parsedPath = expectString(record, "parsed_path");
  const sha256 = expectString(record, "sha256");
  const pageCount = expectNumber(record, "page_count");
  const sourceExpected = `${companyId}/${reportYear}/${documentId}/source.pdf`;
  const parsedExpected = `${companyId}/${reportYear}/${documentId}/text_pdf.json`;
  if (
    !UPLOAD_DOCUMENT.test(documentId) ||
    !SHA256_HEX.test(sha256) ||
    !Number.isInteger(pageCount) ||
    pageCount < 1 ||
    sourcePdfPath !== sourceExpected ||
    parsedPath !== parsedExpected
  ) {
    throw new PrecheckApiError("上传响应与本次公司、年度或文本 PDF 结果不一致，未填入路径。");
  }
  return {
    document_id: documentId,
    source_pdf_path: sourcePdfPath,
    parsed_path: parsedPath,
    sha256,
    page_count: pageCount,
  };
}

function readRecord(payload: unknown): AnnualPrecheckRecord {
  const record = expectRecord(payload, "预检记录");
  const parsed: AnnualPrecheckRecord = {
    run_id: expectString(record, "run_id"),
    status: expectString(record, "status"),
    created_at: expectString(record, "created_at"),
    inputs: readInputs(record.inputs),
    model_called: expectBoolean(record, "model_called"),
    independently_verified: expectBoolean(record, "independently_verified"),
    facts: readFacts(record.facts),
    calculation: readCalculation(record.calculation),
    issues: readIssueGroups(record.issues),
  };
  if ("code" in record) {
    parsed.code = readCode(record.code);
  }
  if ("note" in record) {
    parsed.note = expectString(record, "note");
  }
  if ("formula" in record) {
    parsed.formula = expectString(record, "formula");
  }
  if ("verification" in record) {
    parsed.verification = readVerification(record.verification);
  }
  if ("screening" in record) {
    parsed.screening = readScreening(record.screening);
  }
  return parsed;
}

function readInputs(value: unknown): PrecheckInputs {
  const record = expectRecord(value, "inputs");
  return {
    parsed_path: expectString(record, "parsed_path"),
    source_pdf_path: expectString(record, "source_pdf_path"),
    parsed_file_sha256: expectString(record, "parsed_file_sha256"),
    source_pdf_sha256: expectString(record, "source_pdf_sha256"),
    parsed_source_sha256: expectString(record, "parsed_source_sha256"),
    hashes_match: expectBoolean(record, "hashes_match"),
    company_id: expectString(record, "company_id"),
    report_year: expectNumber(record, "report_year"),
    document_id: expectString(record, "document_id"),
  };
}

function readCode(value: unknown): PrecheckCodeSnapshot {
  const record = expectRecord(value, "code");
  return {
    git_available: expectBoolean(record, "git_available"),
    git_head: expectNullableString(record, "git_head"),
    git_dirty: expectNullableBoolean(record, "git_dirty"),
    source_file_sha256: readStringMap(record.source_file_sha256, "source_file_sha256"),
    missing_source_files: expectStringArray(record, "missing_source_files"),
  };
}

function readFacts(value: unknown): FactExtraction {
  const record = expectRecord(value, "facts");
  return {
    facts: expectArray(record.facts, "facts.facts").map(readFact),
    issues: expectArray(record.issues, "facts.issues").map(readIssue),
  };
}

function readFact(value: unknown): FinancialFact {
  const record = expectRecord(value, "fact");
  return {
    document_id: expectString(record, "document_id"),
    source_sha256: expectString(record, "source_sha256"),
    company_id: expectString(record, "company_id"),
    indicator_name: expectString(record, "indicator_name"),
    table_name: expectString(record, "table_name"),
    raw_value: expectString(record, "raw_value"),
    normalized_value: expectString(record, "normalized_value"),
    report_year: expectNumber(record, "report_year"),
    period_label: expectString(record, "period_label"),
    period_type: expectString(record, "period_type"),
    column_role: expectColumnRole(record.column_role),
    restatement_status: expectString(record, "restatement_status"),
    unit_multiplier: expectString(record, "unit_multiplier"),
    currency: expectString(record, "currency"),
    statement_scope: expectString(record, "statement_scope"),
    extraction_method: expectString(record, "extraction_method"),
    hits: expectArray(record.hits, "hits").map(readHit),
    limitations: expectStringList(record.limitations, "limitations"),
  };
}

function readHit(value: unknown): FactHit {
  const record = expectRecord(value, "hit");
  return {
    page_number: expectNumber(record, "page_number"),
    block_index: expectNumber(record, "block_index"),
    text: expectString(record, "text"),
    x0: expectNumber(record, "x0"),
    y0: expectNumber(record, "y0"),
    x1: expectNumber(record, "x1"),
    y1: expectNumber(record, "y1"),
  };
}

function readCalculation(value: unknown): AnnualChangeCalculation {
  const record = expectRecord(value, "calculation");
  return {
    changes: expectArray(record.changes, "calculation.changes").map(readChange),
    issues: expectArray(record.issues, "calculation.issues").map(readIssue),
  };
}

function readChange(value: unknown): AnnualChange {
  const record = expectRecord(value, "change");
  return {
    indicator_name: expectString(record, "indicator_name"),
    report_year: expectNumber(record, "report_year"),
    prior_year: expectNumber(record, "prior_year"),
    document_id: expectString(record, "document_id"),
    source_sha256: expectString(record, "source_sha256"),
    company_id: expectString(record, "company_id"),
    currency: expectString(record, "currency"),
    unit_multiplier: expectString(record, "unit_multiplier"),
    statement_scope: expectString(record, "statement_scope"),
    period_type: expectString(record, "period_type"),
    current_value: expectString(record, "current_value"),
    prior_value: expectString(record, "prior_value"),
    difference: expectString(record, "difference"),
    rate: expectString(record, "rate"),
    formula: expectString(record, "formula"),
  };
}

function readIssueGroups(value: unknown): PrecheckIssueGroups {
  const record = expectRecord(value, "issues");
  return {
    extraction: expectArray(record.extraction, "issues.extraction").map(readIssue),
    calculation: expectArray(record.calculation, "issues.calculation").map(readIssue),
  };
}

function readIssue(value: unknown): PrecheckIssue {
  const record = expectRecord(value, "issue");
  const issue: PrecheckIssue = {
    code: expectNullableString(record, "code"),
    message: expectNullableString(record, "message"),
    indicator_name: expectNullableString(record, "indicator_name"),
  };
  if ("table_name" in record) {
    issue.table_name = expectNullableString(record, "table_name");
  }
  return issue;
}

function readVerification(value: unknown): SourceAmountVerification {
  const record = expectRecord(value, "verification");
  return {
    kind: expectString(record, "kind"),
    scope_note: expectString(record, "scope_note"),
    status: expectString(record, "status"),
    passed_count: expectNumber(record, "passed_count"),
    failed_count: expectNumber(record, "failed_count"),
    abstained_count: expectNumber(record, "abstained_count"),
    results: expectArray(record.results, "verification.results").map(readVerificationResult),
  };
}

function readVerificationResult(value: unknown): SourceAmountResult {
  const record = expectRecord(value, "verification result");
  return {
    indicator_name: expectString(record, "indicator_name"),
    report_year: expectNumber(record, "report_year"),
    column_role: expectColumnRole(record.column_role),
    status: expectString(record, "status"),
    amount_located: expectBoolean(record, "amount_located"),
    calculation_ok: expectNullableBoolean(record, "calculation_ok"),
    reason: expectNullableString(record, "reason"),
    amount_match: readAmountMatch(record.amount_match),
    evidence: expectArray(record.evidence, "evidence").map(readEvidence),
  };
}

function readScreening(value: unknown): AnnualScreening {
  const path = "screening";
  const record = screeningRecord(value, path);
  if (record.kind !== "deterministic_annual_candidate_screen") {
    throw screeningError(`${path}.kind`);
  }
  if (record.role !== "candidate_input_for_later_agent_or_report") {
    throw screeningError(`${path}.role`);
  }
  return {
    kind: "deterministic_annual_candidate_screen",
    role: "candidate_input_for_later_agent_or_report",
    limitation: screeningString(record.limitation, `${path}.limitation`),
    items: screeningArray(record.items, `${path}.items`).map((item, index) =>
      readScreeningItem(item, `${path}.items[${index}]`),
    ),
  };
}

function readScreeningItem(value: unknown, path: string): AnnualScreeningItem {
  const record = screeningRecord(value, path);
  const statementKind = record.statement_kind;
  if (statementKind !== "inference" && statementKind !== "calculation") {
    throw screeningError(`${path}.statement_kind`);
  }
  return {
    signal_id: screeningString(record.signal_id, `${path}.signal_id`),
    title: screeningString(record.title, `${path}.title`),
    status: readScreeningStatus(record.status, `${path}.status`),
    statement_kind: statementKind,
    formula: screeningString(record.formula, `${path}.formula`),
    inputs: screeningArray(record.inputs, `${path}.inputs`).map((input, index) =>
      readScreeningFactInput(input, `${path}.inputs[${index}]`),
    ),
    value: readScreeningValue(record.value, `${path}.value`),
    reason: screeningNullableString(record.reason, `${path}.reason`),
    note: screeningNullableString(record.note, `${path}.note`),
    limitation: screeningString(record.limitation, `${path}.limitation`),
  };
}

function readScreeningStatus(value: unknown, path: string): AnnualScreeningStatus {
  if (
    value === "candidate" ||
    value === "not_triggered" ||
    value === "calculated" ||
    value === "abstained"
  ) {
    return value;
  }
  throw screeningError(path);
}

function readScreeningFactInput(value: unknown, path: string): ScreeningFactInput {
  const record = screeningRecord(value, path);
  if (record.role !== "fact") {
    throw screeningError(`${path}.role`);
  }
  const columnRole = record.column_role;
  if (columnRole !== "current" && columnRole !== "comparative") {
    throw screeningError(`${path}.column_role`);
  }
  return {
    role: "fact",
    indicator_name: screeningString(record.indicator_name, `${path}.indicator_name`),
    report_year: screeningInteger(record.report_year, `${path}.report_year`),
    column_role: columnRole,
    normalized_value: screeningDecimal(record.normalized_value, `${path}.normalized_value`),
    document_id: screeningString(record.document_id, `${path}.document_id`),
    source_sha256: screeningString(record.source_sha256, `${path}.source_sha256`),
    company_id: screeningString(record.company_id, `${path}.company_id`),
    currency: screeningString(record.currency, `${path}.currency`),
    statement_scope: screeningString(record.statement_scope, `${path}.statement_scope`),
    period_type: screeningString(record.period_type, `${path}.period_type`),
    hits: screeningArray(record.hits, `${path}.hits`).map((hit, index) =>
      readScreeningFactHit(hit, `${path}.hits[${index}]`),
    ),
  };
}

function readScreeningFactHit(value: unknown, path: string): ScreeningFactHit {
  const record = screeningRecord(value, path);
  const pageNumber = screeningInteger(record.page_number, `${path}.page_number`);
  const blockIndex = screeningInteger(record.block_index, `${path}.block_index`);
  if (pageNumber < 1 || blockIndex < 0) {
    throw screeningError(path);
  }
  return {
    page_number: pageNumber,
    block_index: blockIndex,
    x0: screeningNumber(record.x0, `${path}.x0`),
    y0: screeningNumber(record.y0, `${path}.y0`),
    x1: screeningNumber(record.x1, `${path}.x1`),
    y1: screeningNumber(record.y1, `${path}.y1`),
  };
}

function readScreeningValue(value: unknown, path: string): ScreeningValue | null {
  if (value === null) {
    return null;
  }
  const record = screeningRecord(value, path);
  if (record.role !== "calculation") {
    throw screeningError(`${path}.role`);
  }
  const hasDifferences = "left_difference" in record || "right_difference" in record;
  const hasRatio = "ratio" in record || "numerator" in record || "denominator" in record;
  if (hasDifferences === hasRatio) {
    throw screeningError(path);
  }
  if (hasDifferences) {
    return {
      role: "calculation",
      left_difference: screeningDecimal(record.left_difference, `${path}.left_difference`),
      right_difference: screeningDecimal(record.right_difference, `${path}.right_difference`),
    };
  }
  return {
    role: "calculation",
    year: screeningInteger(record.year, `${path}.year`),
    report_year: screeningInteger(record.report_year, `${path}.report_year`),
    numerator: screeningDecimal(record.numerator, `${path}.numerator`),
    denominator: screeningDecimal(record.denominator, `${path}.denominator`),
    ratio: screeningDecimal(record.ratio, `${path}.ratio`),
  };
}

function screeningRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw screeningError(path);
  }
  return value;
}

function screeningArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) {
    throw screeningError(path);
  }
  return value;
}

function screeningString(value: unknown, path: string): string {
  if (typeof value !== "string") {
    throw screeningError(path);
  }
  return value;
}

function screeningNullableString(value: unknown, path: string): string | null {
  if (value === null || typeof value === "string") {
    return value;
  }
  throw screeningError(path);
}

function screeningNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw screeningError(path);
  }
  return value;
}

function screeningInteger(value: unknown, path: string): number {
  const number = screeningNumber(value, path);
  if (!Number.isInteger(number)) {
    throw screeningError(path);
  }
  return number;
}

function screeningDecimal(value: unknown, path: string): string {
  const decimal = screeningString(value, path);
  if (!/^-?\d+(?:\.\d+)?$/.test(decimal)) {
    throw screeningError(path);
  }
  return decimal;
}

function screeningError(path: string): PrecheckApiError {
  return new PrecheckApiError(`预检记录的筛查字段 ${path} 格式错误，无法显示筛查结果。`);
}

function readAmountMatch(value: unknown): AmountMatch | null {
  if (value === null) {
    return null;
  }
  const record = expectRecord(value, "amount_match");
  return {
    matched_text: expectString(record, "matched_text"),
    context: expectString(record, "context"),
  };
}

function readEvidence(value: unknown): SourceAmountEvidence {
  const record = expectRecord(value, "evidence");
  return {
    page_number: expectNumber(record, "page_number"),
    block_index: expectNumber(record, "block_index"),
    x0: expectNumber(record, "x0"),
    y0: expectNumber(record, "y0"),
    x1: expectNumber(record, "x1"),
    y1: expectNumber(record, "y1"),
    extracted_text: expectString(record, "extracted_text"),
    amount_in_this_clip: expectBoolean(record, "amount_in_this_clip"),
    problem: expectNullableString(record, "problem"),
  };
}

function readStringMap(value: unknown, label: string): Record<string, string> {
  const record = expectRecord(value, label);
  const mapped: Record<string, string> = {};
  for (const [key, item] of Object.entries(record)) {
    if (typeof item !== "string") {
      throw new PrecheckApiError(`响应字段 ${label} 无法读取。`);
    }
    mapped[key] = item;
  }
  return mapped;
}

function expectColumnRole(value: unknown): FactColumnRole {
  if (value === "current" || value === "comparative") {
    return value;
  }
  throw new PrecheckApiError("响应中的 column_role 无法读取。");
}

function expectRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new PrecheckApiError(`响应字段 ${label} 无法读取。`);
  }
  return value;
}

function expectArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new PrecheckApiError(`响应字段 ${label} 无法读取。`);
  }
  return value;
}

function expectString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string") {
    throw new PrecheckApiError(`响应字段 ${key} 无法读取。`);
  }
  return value;
}

function expectNullableString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  if (value === null) {
    return null;
  }
  if (typeof value !== "string") {
    throw new PrecheckApiError(`响应字段 ${key} 无法读取。`);
  }
  return value;
}

function expectBoolean(record: Record<string, unknown>, key: string): boolean {
  const value = record[key];
  if (typeof value !== "boolean") {
    throw new PrecheckApiError(`响应字段 ${key} 无法读取。`);
  }
  return value;
}

function expectNullableBoolean(record: Record<string, unknown>, key: string): boolean | null {
  const value = record[key];
  if (value === null) {
    return null;
  }
  if (typeof value !== "boolean") {
    throw new PrecheckApiError(`响应字段 ${key} 无法读取。`);
  }
  return value;
}

function expectNumber(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new PrecheckApiError(`响应字段 ${key} 无法读取。`);
  }
  return value;
}

function expectStringArray(record: Record<string, unknown>, key: string): string[] {
  return expectStringList(record[key], key);
}

function expectStringList(value: unknown, label: string): string[] {
  return expectArray(value, label).map((item) => {
    if (typeof item !== "string") {
      throw new PrecheckApiError(`响应字段 ${label} 无法读取。`);
    }
    return item;
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
