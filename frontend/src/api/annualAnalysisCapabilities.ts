export type ModelInvestigationCapabilityStatus = "ready" | "not_configured" | "dependency_unavailable";

export interface AnnualAnalysisCapabilities {
  model_investigation: {
    available: boolean;
    status: ModelInvestigationCapabilityStatus;
  };
}

const CAPABILITIES_PATH = "/api/v1/annual-analysis-capabilities";

export class AnnualAnalysisCapabilitiesApiError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AnnualAnalysisCapabilitiesApiError";
  }
}

export async function loadAnnualAnalysisCapabilities(): Promise<AnnualAnalysisCapabilities> {
  const response = await fetch(CAPABILITIES_PATH, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "omit",
  }).catch(() => {
    throw new AnnualAnalysisCapabilitiesApiError("暂时无法确认 AI 服务状态。确定性分析仍可使用。");
  });
  const payload = await response.json().catch(() => null) as unknown;
  if (!response.ok) {
    throw new AnnualAnalysisCapabilitiesApiError("暂时无法确认 AI 服务状态。确定性分析仍可使用。");
  }
  const outer = asRecord(payload);
  const rawCapability = outer === null ? null : asRecord(outer.model_investigation);
  const available = rawCapability?.available;
  const status = rawCapability?.status;
  if (
    typeof available !== "boolean" ||
    (status !== "ready" && status !== "not_configured" && status !== "dependency_unavailable") ||
    available !== (status === "ready")
  ) {
    throw new AnnualAnalysisCapabilitiesApiError("暂时无法确认 AI 服务状态。确定性分析仍可使用。");
  }
  return { model_investigation: { available, status } };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}
