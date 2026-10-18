import type { QuarterlyAnalysisResult } from "../types/quarterlyAnalysis";

export class QuarterlyApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | null = null,
  ) {
    super(message);
    this.name = "QuarterlyApiError";
  }
}

export async function getLatestQuarterlyAnalysis(): Promise<QuarterlyAnalysisResult> {
  const response = await fetch("/api/v1/quarterly-analyses/latest", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new QuarterlyApiError(
      `获取最新季度分析失败: HTTP ${response.status}`,
      response.status,
    );
  }
  return response.json();
}

export async function getQuarterlyAnalysis(runId: string): Promise<QuarterlyAnalysisResult> {
  const response = await fetch(`/api/v1/quarterly-analyses/${encodeURIComponent(runId)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new QuarterlyApiError(
      `获取季度分析记录失败: HTTP ${response.status}`,
      response.status,
    );
  }
  return response.json();
}

export async function runQuarterlyAnalysis(params?: {
  year?: number;
  quarter?: "Q1" | "Q2" | "Q3" | "Q4";
  company_id?: string;
}): Promise<QuarterlyAnalysisResult> {
  const response = await fetch("/api/v1/quarterly-analyses/run", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      year: params?.year ?? 2024,
      quarter: params?.quarter ?? "Q2",
      company_id: params?.company_id,
    }),
  });
  if (!response.ok) {
    throw new QuarterlyApiError(
      `执行季度同比/环比推导失败: HTTP ${response.status}`,
      response.status,
    );
  }
  return response.json();
}
