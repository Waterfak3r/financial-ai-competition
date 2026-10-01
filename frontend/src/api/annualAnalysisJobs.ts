import type {
  AnnualAnalysisJob,
  AnnualAnalysisJobMode,
  AnnualAnalysisJobRequest,
  AnnualAnalysisJobStatus,
} from "../types/annualAnalysisJob";

const JOBS_PATH = "/api/v1/annual-analysis-jobs";
const JOB_ID_PATTERN = /^annual-job-[0-9a-f]{32}$/;
const RUN_ID_PATTERN = /^annual-analysis-[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const STATUSES = new Set<AnnualAnalysisJobStatus>([
  "queued",
  "running",
  "completed",
  "completed_with_issues",
  "failed",
  "interrupted",
]);
const MODES = new Set<AnnualAnalysisJobMode>(["deterministic", "m3_screening", "model_investigation"]);

export class AnnualAnalysisJobApiError extends Error {
  readonly status: number | null;
  readonly code: string | null;

  constructor(message: string, status: number | null = null, code: string | null = null) {
    super(message);
    this.name = "AnnualAnalysisJobApiError";
    this.status = status;
    this.code = code;
  }
}

export async function createAnnualAnalysisJob(
  request: AnnualAnalysisJobRequest,
): Promise<AnnualAnalysisJob> {
  validateRequest(request);
  const response = await fetch(JOBS_PATH, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json; charset=utf-8",
    },
    body: JSON.stringify(request),
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualAnalysisJobApiError("无法连接本地年度分析服务。请确认后端已启动，并由页面通过 /api 访问。");
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const failure = failureMessage(response.status, payload);
    throw new AnnualAnalysisJobApiError(failure.message, response.status, failure.code);
  }
  return readJob(payload, undefined, request);
}

export async function loadAnnualAnalysisJob(jobId: string): Promise<AnnualAnalysisJob> {
  if (!JOB_ID_PATTERN.test(jobId)) {
    throw new AnnualAnalysisJobApiError("任务编号格式无效，无法读取任务状态。");
  }
  const response = await fetch(JOBS_PATH + "/" + encodeURIComponent(jobId), {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualAnalysisJobApiError("无法连接本地年度分析服务。请确认后端仍在运行。");
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const failure = failureMessage(response.status, payload);
    throw new AnnualAnalysisJobApiError(failure.message, response.status, failure.code);
  }
  return readJob(payload, jobId);
}

function validateRequest(request: AnnualAnalysisJobRequest) {
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,64}$/.test(request.company_id) ||
    request.company_id.includes("..") ||
    !Number.isInteger(request.report_year) ||
    request.report_year < 1900 ||
    request.report_year > 2100 ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(request.document_id) ||
    request.document_id.includes("..") ||
    !SHA256_PATTERN.test(request.sha256) ||
    !MODES.has(request.mode)
  ) {
    throw new AnnualAnalysisJobApiError("年度分析任务请求信息无效，未提交任务。");
  }
  const parts = request.source_pdf_path.split("/");
  if (
    parts.length !== 4 ||
    parts[0] !== request.company_id ||
    parts[1] !== String(request.report_year) ||
    parts[2] !== request.document_id ||
    parts.some((part) => !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(part) || part.includes("..")) ||
    !parts[3]?.toLowerCase().endsWith(".pdf")
  ) {
    throw new AnnualAnalysisJobApiError("PDF 来源路径与公司、年度或文档标识不一致，未提交任务。");
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.trim() === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new AnnualAnalysisJobApiError("服务返回的内容不是有效 JSON（HTTP " + response.status + "）。", response.status);
  }
}

function failureMessage(status: number, payload: unknown): { message: string; code: string | null } {
  const outer = asRecord(payload);
  const detail = outer?.detail;
  const detailRecord = asRecord(detail);
  const code = typeof detailRecord?.code === "string" ? detailRecord.code : null;
  const message = typeof detailRecord?.message === "string" ? detailRecord.message : typeof detail === "string" ? detail : null;
  if (message !== null && message.trim() !== "") {
    return { message, code };
  }
  const knownMessages: Record<string, string> = {
    job_capacity_reached: "任务队列已满，请稍后重试。",
    source_not_found: "找不到此 PDF，请检查本机原始文件是否仍在 data/raw 目录。",
    source_record_not_found: "找不到此 PDF 的来源记录，无法安全启动分析。",
    source_sha256_mismatch: "PDF 内容与来源记录中的 SHA256 不一致，未启动分析。",
    source_identity_mismatch: "PDF 路径中的公司、年度或文档标识不一致，未启动分析。",
    mode_unsupported: "此入口仅支持确定性分析和实验性 M3 筛查。",
    job_not_found: "找不到该分析任务。",
  };
  if (code !== null && knownMessages[code] !== undefined) {
    return { message: knownMessages[code], code };
  }
  return { message: "年度分析任务请求失败（HTTP " + status + (code === null ? "" : "，" + code) + "）。", code };
}

function readJob(value: unknown, expectedJobId?: string, expectedRequest?: AnnualAnalysisJobRequest): AnnualAnalysisJob {
  const record = asRecord(value);
  if (record === null) {
    throw invalidResponse("响应不是对象");
  }
  const requestRecord = asRecord(record.request);
  if (requestRecord === null) {
    throw invalidResponse("缺少 request");
  }
  const jobRequest: AnnualAnalysisJobRequest = {
    company_id: stringField(requestRecord, "company_id"),
    report_year: integerField(requestRecord, "report_year"),
    document_id: stringField(requestRecord, "document_id"),
    source_pdf_path: stringField(requestRecord, "source_pdf_path"),
    sha256: stringField(requestRecord, "sha256"),
    mode: modeField(requestRecord, "mode"),
  };
  validateRequest(jobRequest);

  const jobId = stringField(record, "job_id");
  const status = statusField(record, "status");
  const stage = stringField(record, "stage");
  const runId = nullableStringField(record, "run_id");
  const resultUrl = nullableStringField(record, "result_url");
  const errorValue = record.error;
  const error = errorValue === null ? null : readError(errorValue);
  const successful = status === "completed" || status === "completed_with_issues";
  if (
    !JOB_ID_PATTERN.test(jobId) ||
    (expectedJobId !== undefined && jobId !== expectedJobId) ||
    stage === "" ||
    !Number.isFinite(Date.parse(stringField(record, "created_at"))) ||
    !Number.isFinite(Date.parse(stringField(record, "updated_at"))) ||
    (runId !== null && !RUN_ID_PATTERN.test(runId)) ||
    successful !== (runId !== null && resultUrl !== null) ||
    (successful && resultUrl !== "/v1/annual-analyses/" + runId) ||
    (!successful && resultUrl !== null) ||
    ((status === "failed" || status === "interrupted") !== (error !== null))
  ) {
    throw invalidResponse("任务状态字段不一致");
  }
  if (expectedRequest !== undefined && !sameRequest(jobRequest, expectedRequest)) {
    throw invalidResponse("任务来源或模式与提交请求不一致");
  }
  return {
    job_id: jobId,
    status,
    stage,
    created_at: stringField(record, "created_at"),
    updated_at: stringField(record, "updated_at"),
    request: jobRequest,
    run_id: runId,
    result_url: resultUrl,
    error,
  };
}

function readError(value: unknown): { code: string; message: string } {
  const record = asRecord(value);
  if (record === null) {
    throw invalidResponse("error");
  }
  return { code: stringField(record, "code"), message: stringField(record, "message") };
}

function sameRequest(left: AnnualAnalysisJobRequest, right: AnnualAnalysisJobRequest): boolean {
  return (
    left.company_id === right.company_id &&
    left.report_year === right.report_year &&
    left.document_id === right.document_id &&
    left.source_pdf_path === right.source_pdf_path &&
    left.sha256.toLowerCase() === right.sha256.toLowerCase() &&
    left.mode === right.mode
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string") {
    throw invalidResponse(key);
  }
  return value;
}

function nullableStringField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  if (value === null) {
    return null;
  }
  if (typeof value !== "string") {
    throw invalidResponse(key);
  }
  return value;
}

function integerField(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw invalidResponse(key);
  }
  return value;
}

function modeField(record: Record<string, unknown>, key: string): AnnualAnalysisJobMode {
  const value = stringField(record, key);
  if (!MODES.has(value as AnnualAnalysisJobMode)) {
    throw invalidResponse(key);
  }
  return value as AnnualAnalysisJobMode;
}

function statusField(record: Record<string, unknown>, key: string): AnnualAnalysisJobStatus {
  const value = stringField(record, key);
  if (!STATUSES.has(value as AnnualAnalysisJobStatus)) {
    throw invalidResponse(key);
  }
  return value as AnnualAnalysisJobStatus;
}

function invalidResponse(detail: string): AnnualAnalysisJobApiError {
  return new AnnualAnalysisJobApiError("年度分析服务返回的任务状态无法验证（" + detail + "），未采用该状态。");
}
