import type {
  AmountMatch,
  AnnualChange,
  AnnualChangeCalculation,
  AnnualPrecheckRecord,
  AnnualPrecheckRequest,
  FactColumnRole,
  FactExtraction,
  FactHit,
  FinancialFact,
  PrecheckCodeSnapshot,
  PrecheckInputs,
  PrecheckIssue,
  PrecheckIssueGroups,
  SourceAmountEvidence,
  SourceAmountResult,
  SourceAmountVerification,
} from "../types/precheck";

const PRECHECK_PATH = "/api/v1/annual-prechecks";

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
    const failure = messageFromFailure(response.status, payload);
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

function messageFromFailure(status: number, payload: unknown): { message: string; code: string | null } {
  if (!isRecord(payload) || !("detail" in payload)) {
    return { message: `预检请求失败（HTTP ${status}）。`, code: null };
  }
  const detail = payload.detail;
  if (isRecord(detail)) {
    const code = typeof detail.code === "string" ? detail.code : null;
    if (typeof detail.message === "string" && detail.message.trim() !== "") {
      return { message: detail.message, code };
    }
    if (code !== null) {
      return { message: `预检请求失败（HTTP ${status}，${code}）。`, code };
    }
    return { message: `预检请求失败（HTTP ${status}）。`, code: null };
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
  return { message: `预检请求失败（HTTP ${status}）。`, code: null };
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
