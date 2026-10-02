import type {
  AnnualAnalysisHistoryIssue,
  AnnualAnalysisHistoryItem,
  AnnualAnalysisHistoryResponse,
  AnnualHistoryMode,
} from "../types/annualAnalysisHistory";

const HISTORY_PATH = "/api/v1/annual-analysis-history";
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;
const JOB_ID_PATTERN = /^annual-job-[0-9a-f]{32}$/;

export class AnnualAnalysisHistoryApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "AnnualAnalysisHistoryApiError";
    this.status = status;
  }
}

export async function loadAnnualAnalysisHistory(
  limit = 20,
  offset = 0,
): Promise<AnnualAnalysisHistoryResponse> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 50 || !Number.isInteger(offset) || offset < 0) {
    throw new AnnualAnalysisHistoryApiError("历史列表分页参数无效。");
  }
  const response = await fetch(`${HISTORY_PATH}?limit=${limit}&offset=${offset}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualAnalysisHistoryApiError("无法连接本机历史列表服务。请确认后端已启动。");
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const detail = objectValue(payload)?.detail;
    const message = typeof objectValue(detail)?.message === "string"
      ? String(objectValue(detail)?.message)
      : typeof detail === "string"
        ? detail
        : `读取本机历史列表失败（HTTP ${response.status}）。`;
    throw new AnnualAnalysisHistoryApiError(message, response.status);
  }
  const history = parseHistoryResponse(payload);
  if (history.limit !== limit || history.offset !== offset) {
    throw invalidResponse("历史列表分页位置与请求不一致。");
  }
  return history;
}

async function readJson(response: Response): Promise<unknown> {
  const body = await response.text();
  if (body.trim() === "") return null;
  try {
    return JSON.parse(body) as unknown;
  } catch {
    throw new AnnualAnalysisHistoryApiError(`服务返回的历史列表不是有效 JSON（HTTP ${response.status}）。`, response.status);
  }
}

function parseHistoryResponse(value: unknown): AnnualAnalysisHistoryResponse {
  const record = requireObject(value, "历史列表");
  const limit = requireInteger(record.limit, "limit");
  const offset = requireInteger(record.offset, "offset");
  const total = requireInteger(record.total, "total");
  if (limit < 1 || limit > 50 || offset < 0 || total < 0 || !Array.isArray(record.items)) {
    throw invalidResponse();
  }
  const items = record.items.map(parseItem);
  const ids = new Set<string>();
  for (const item of items) {
    if (ids.has(item.item_id)) throw invalidResponse("历史列表包含重复 item_id。");
    ids.add(item.item_id);
  }
  if (items.length > limit || items.length > Math.max(0, total - offset)) throw invalidResponse();
  return { items, total, limit, offset };
}

function parseItem(value: unknown): AnnualAnalysisHistoryItem {
  const item = requireObject(value, "历史条目");
  const itemId = requireString(item.item_id, "item_id");
  const jobId = nullableString(item.job_id, "job_id");
  const runId = nullableString(item.run_id, "run_id");
  const sourceDocumentId = requireString(item.source_document_id, "source_document_id");
  const companyCode = requireString(item.company_code, "company_code");
  const sourceId = requireString(item.source_id, "source_id");
  const reportType = requireString(item.report_type, "report_type");
  if (
    !ID_PATTERN.test(itemId) ||
    (jobId !== null && !JOB_ID_PATTERN.test(jobId)) ||
    (runId !== null && !ID_PATTERN.test(runId)) ||
    !ID_PATTERN.test(sourceDocumentId) ||
    !ID_PATTERN.test(companyCode) ||
    reportType !== "annual" ||
    (jobId === null && runId === null)
  ) {
    throw invalidResponse();
  }
  const createdAt = nullableString(item.created_at, "created_at");
  const sourceName = nullableString(item.source_name, "source_name");
  const jobStatus = nullableString(item.job_status, "job_status");
  const runStatus = nullableString(item.run_status, "run_status");
  const runMode = nullableMode(item.run_mode, "run_mode");
  const issues = requireArray(item.issues, "issues").map(parseIssue);
  return {
    item_id: itemId,
    company_code: companyCode,
    year: requireInteger(item.year, "year"),
    report_type: "annual",
    source_document_id: sourceDocumentId,
    source_id: sourceId,
    source_name: sourceName,
    status: requireString(item.status, "status"),
    job_status: jobStatus,
    run_status: runStatus,
    mode: requireMode(item.mode, "mode"),
    run_mode: runMode,
    replay: requireBoolean(item.replay, "replay"),
    created_at: createdAt,
    job_id: jobId,
    run_id: runId,
    result_available: requireBoolean(item.result_available, "result_available"),
    issues,
  };
}

function parseIssue(value: unknown): AnnualAnalysisHistoryIssue {
  const issue = requireObject(value, "issue");
  return { code: requireString(issue.code, "issue.code"), message: requireString(issue.message, "issue.message") };
}

function requireMode(value: unknown, path: string): AnnualHistoryMode {
  const mode = requireString(value, path);
  if (["deterministic", "m3_screening", "model_investigation", "unknown"].includes(mode)) {
    return mode as AnnualHistoryMode;
  }
  return "unknown";
}

function nullableMode(value: unknown, path: string): AnnualHistoryMode | null {
  return value === null ? null : requireMode(value, path);
}

function requireObject(value: unknown, path: string): Record<string, unknown> {
  const object = objectValue(value);
  if (object === null) throw invalidResponse(`服务返回的${path}格式无效。`);
  return object;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function requireArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw invalidResponse(`服务返回的${path}格式无效。`);
  return value;
}

function requireInteger(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) throw invalidResponse(`服务返回的${path}格式无效。`);
  return value;
}

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw invalidResponse(`服务返回的${path}格式无效。`);
  return value;
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== "string") throw invalidResponse(`服务返回的${path}格式无效。`);
  return value;
}

function nullableString(value: unknown, path: string): string | null {
  if (value === null) return null;
  return requireString(value, path);
}

function invalidResponse(message = "服务返回的本机历史列表格式无效，未显示该数据。"): AnnualAnalysisHistoryApiError {
  return new AnnualAnalysisHistoryApiError(message);
}
