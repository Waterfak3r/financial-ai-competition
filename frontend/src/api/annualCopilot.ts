/**
 * API client for interacting with the bounded report copilot.
 */

export interface CopilotMessage {
  role: "user" | "assistant";
  content: string;
}

export interface CopilotResponse {
  run_id: string;
  company_id: string;
  report_year: number;
  question: string;
  answer: string;
  model: string;
  duration_seconds: number;
  suggested_prompts: string[];
}

export class CopilotApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status = 500, code: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function askAnnualCopilot(
  runId: string,
  question: string,
  history: CopilotMessage[] = [],
): Promise<CopilotResponse> {
  const cleanRunId = runId.trim();
  const cleanQuestion = question.trim();

  const response = await fetch(
    `/api/v1/annual-analyses/${encodeURIComponent(cleanRunId)}/copilot`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: cleanQuestion,
        history,
      }),
    },
  ).catch(() => {
    throw new CopilotApiError("无法连接到本地探查助手服务，请确认后端仍在运行。", 503);
  });

  if (!response.ok) {
    let errCode: string | null = null;
    let errMsg = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = await response.json();
      if (payload?.detail) {
        if (typeof payload.detail === "object") {
          errCode = payload.detail.code ?? null;
          errMsg = payload.detail.message ?? errMsg;
        } else if (typeof payload.detail === "string") {
          errMsg = payload.detail;
        }
      }
    } catch {
      // ignore
    }
    throw new CopilotApiError(errMsg, response.status, errCode);
  }

  return response.json();
}
